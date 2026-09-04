"""QUOTE-PRODUCTION COUNTERFACTUAL for the fee seam repair (2026-09-04 build A,
validation gate "feedback_validate_caps_quote": a pricing change must be
proven to still QUOTE against real sizes on recorded tape before it goes
live; a tier going to zero quotes is a FAIL to explain).

Read-only (store opened ``mode=ro``); imports the LIVE ``construct_quote``,
``MarkupPolicy``, ``FeeModel`` and the fee-net edge function (hard rule 8 —
never a reimplementation). Three sections:

1. FILLS PARITY + FEE-NET EV on the post-onset store fills (id >= the first
   fill after 2026-08-20 09:07Z): the pure edge function with the fee the
   ledger BOOKED must reproduce ``fills.expected_edge_cc`` to the cent. The
   quote-time fair is not stored on the fill row; it is INVERTED from the
   ledger's own formula (expected_edge + booked fee = (side_fair − bid) ·
   qty // 100 — exact for qty > 1 contract, equivalent below it), and the
   markout tracker's fresh fair-at-fill is reported as a cross-check (it
   re-prices at execution, so it drifts). Then the same fills net of the
   MEASURED 0.035 fee: negatives (fills the bot should never have taken)
   and their premium.

2. FILLS UNDER THE FLOOR: per tier, fills whose retained margin was below
   the fee at their bid (the floor would have re-priced them).

3. QUOTE REPLAY on >= 50k ``quote_sent`` decisions since the onset (rowid
   range): fair, width and legs come from ``context_json``; the markup is
   re-derived with the live ``MarkupPolicy`` on the live yaml; the applied
   skew is reconstructed as the residual between the recorded bid and
   fair_no − margin (the recorded bid is ground truth for "today"). Each
   quote is re-constructed under {today (fee 0), floor, width} with the
   measured 0.035 combo maker schedule; per tier: bids moved (count, mean
   cc), NON-ZERO quote count. With ``--with-cell-floor`` the measured
   retained-edge floor table is estimated from the settled grade (live
   estimator on read-only SQL) and applied as well.

Usage:
    PYTHONPATH=src .venv/Scripts/python.exe -m tools.diagnostics.fee_floor_counterfactual \
        --config config/prod-live-wc.local.yaml \
        --db "file:D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3?mode=ro" \
        --decisions-from 132892717 --max-quotes 60000 --with-cell-floor
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Side, load_conventions
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.grid import PriceGrid
from combomaker.ops.config import load_config
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.markup import MarkupPolicy, _is_cross_game_ml_parlay
from combomaker.pricing.quote import ConstructedQuote, QuoteParams, construct_quote
from combomaker.pricing.retained_cell import CellKey, cell_key
from combomaker.rfq.edge import candidate_edge_cc
from combomaker.risk.exposure import LegRef
from combomaker.risk.retained_edge_floor import (
    FloorEstimate,
    GradeRow,
    estimate_retained_floor,
    grade_row_from_store,
    summarize,
)

JsonDict = dict[str, Any]
ONSET_ISO = "2026-08-20T09:07:00"
COMBO = FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
# The measured schedule (tests/fixtures/ground_truth/maker_fee_20260820.json
# pins 0.035 on 540/540 charged fills); taker 0.07 from the published PDF.
MEASURED = FeeSchedule(taker_coef=Fraction(7, 100), maker_coef=Fraction(35, 1000))
# Every combo market this run has quoted sits on a 0.1c grid (store: 5,000/
# 5,000 sampled quote_sent no_bids and 547/547 fill prices on multiples of
# 10cc); the live grid is fetched per collection, this is its shape.
GRID = PriceGrid.from_market_payload(
    {"ticker": "CF", "price_ranges": [{"start": "0.001", "end": "0.999", "step": "0.001"}]}
)


def dollars(cc: int | float) -> str:
    return f"${cc / 10_000:,.2f}"


def quote_params(cfg: Any) -> QuoteParams:
    # keep in sync with pricing/engine.py PricingEngine.__init__ (quote_fields)
    fields = {
        k: v
        for k, v in cfg.pricing.quote.model_dump().items()
        if k
        not in (
            "longshot_fair_threshold",
            "longshot_min_rel_uncertainty",
            "favorite_leg_threshold",
            "favorite_width_multiplier",
            "farm_impossible_combos",
            "farm_markup",
            "farm_max_contracts",
        )
    }
    return QuoteParams(**fields)


def tier_label(policy: MarkupPolicy, legs: list[str], fair_cc: int) -> tuple[str, int]:
    sport, markup = policy.markup_for(legs, fair_cc=fair_cc)
    razor = (
        policy.ml_parlay_cc > 0
        and fair_cc < policy.ml_parlay_fair_below_cc
        and _is_cross_game_ml_parlay(legs)
        and markup == policy.ml_parlay_cc
    )
    if razor:
        return f"{sport}:razor{markup}", markup
    if fair_cc >= 3_500:
        return f"{sport}:mains{markup}", markup
    return f"{sport}:ladder{markup}", markup


@dataclass
class Tally:
    n: int = 0
    nonzero_today: int = 0
    nonzero_floor: int = 0
    nonzero_width: int = 0
    nonzero_cell: int = 0
    moved_floor: int = 0
    moved_floor_cc: int = 0
    moved_width: int = 0
    moved_width_cc: int = 0
    moved_cell: int = 0
    moved_cell_cc: int = 0
    parity_today: int = 0
    rebate_quotes: int = 0


def build_quote(
    *,
    fair_cc: int,
    width: dict[str, int],
    markup_cc: int,
    skew_cc: int,
    fee_model: FeeModel,
    fee_type: FeeType,
    params: QuoteParams,
    mode: str,
    retained_floor_cc: int | None = None,
) -> ConstructedQuote | None:
    # width components are re-supplied through a zero-width params + the
    # joint's uncertainty so ``half`` reproduces the recorded width exactly.
    total_width = sum(int(v) for v in width.values())
    p = params.__class__(
        **{
            **{f: getattr(params, f) for f in params.__slots__},  # type: ignore[attr-defined]
            "base_width_cc": 0,
            "per_leg_width_cc": 0,
            "size_width_cc_per_100": 0,
            "time_width_cc": 0,
            "uncertainty_width_scale": 1.0,
        }
    )
    joint = JointEstimate(
        p=fair_cc / CC_PER_DOLLAR,
        uncertainty=total_width / CC_PER_DOLLAR,
        frechet_lo=0.0,
        frechet_hi=1.0,
        notes=(),
    )
    q = construct_quote(
        joint=joint,
        n_legs=2,
        qty=CentiContracts(1_000),
        grid=GRID,
        fee_model=fee_model,
        fee_type=fee_type,
        fee_multiplier=Fraction(1),
        time_to_close_s=10 * 3600.0,
        in_play=False,
        yes_cap_cc=CentiCents(CC_PER_DOLLAR),
        no_cap_cc=CentiCents(CC_PER_DOLLAR),
        inventory_skew_cc=skew_cc,
        markup_cc=markup_cc,
        params=p,
        fee_mode=mode,
        retained_floor_cc=retained_floor_cc,
    )
    return q if isinstance(q, ConstructedQuote) else None


def legs_from_json(raw: str) -> list[LegRef]:
    out = []
    for leg in json.loads(raw):
        out.append(
            LegRef(
                market_ticker=str(leg["market_ticker"]),
                event_ticker=leg.get("event_ticker"),
                side=str(leg["side"]),
            )
        )
    return out


# --------------------------------------------------------------- section 1+2


def fills_section(
    con: sqlite3.Connection, policy: MarkupPolicy, fee_model: FeeModel, conventions: Any
) -> tuple[dict[str, dict[str, int]], int]:
    first_id = con.execute("SELECT MIN(id) FROM fills WHERE at >= ?", (ONSET_ISO,)).fetchone()[0]
    rows = con.execute(
        "SELECT id, fill_ref, combo_ticker, our_side, contracts_centi, price_cc,"
        " COALESCE(fee_cc, 0), expected_edge_cc FROM fills WHERE id >= ? ORDER BY id",
        (first_id,),
    ).fetchall()
    fairs = dict(
        con.execute(
            "SELECT m.fill_ref, MIN(m.fair_at_fill_cc) FROM markouts m JOIN fills f"
            " ON f.fill_ref = m.fill_ref WHERE f.id >= ? AND m.fair_at_fill_cc IS NOT NULL"
            " GROUP BY m.fill_ref",
            (first_id,),
        ).fetchall()
    )
    legs_by_ticker = {}
    for ticker, legs_json in con.execute(
        "SELECT combo_ticker, MAX(legs_json) FROM position_ledger WHERE combo_ticker IN"
        f" ({','.join('?' * len(rows))}) GROUP BY combo_ticker",
        tuple(r[2] for r in rows),
    ).fetchall():
        legs_by_ticker[ticker] = legs_json
    parity_ok = parity_bad = 0
    markout_exact = markout_off = markout_missing = 0
    markout_diffs: list[int] = []
    net_negative = 0
    net_negative_premium = 0
    gross_total = net_total = fee_total = 0
    by_tier: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for _id, fill_ref, ticker, side_raw, qty, price, fee_booked, edge in rows:
        our_side = Side(side_raw)
        # THE LEDGER'S OWN FAIR, inverted from its formula: expected_edge =
        # (side_fair - bid) * qty // 100 - fee_booked. For qty > 100 the
        # floor division leaves exactly one integer side_fair; for smaller
        # fills any integer in the interval reproduces the same floored
        # value, so the parity below is exact by construction either way.
        per_ct = -(-(int(edge) + int(fee_booked)) * 100 // int(qty))
        side_fair = int(price) + per_ct
        fair_cc = CC_PER_DOLLAR - side_fair if our_side is Side.NO else side_fair
        recomputed = candidate_edge_cc(
            fair_cc=fair_cc,
            bid_cc=int(price),
            qty_centi=int(qty),
            our_side=our_side,
            complement_verified=conventions.combo_no_pays_complement,
            fee_cc=int(fee_booked),
        )
        if recomputed == int(edge):
            parity_ok += 1
        else:
            parity_bad += 1
        # Cross-check against the markout tracker's fair AT FILL (a fresh
        # re-price at execution, so it can differ from the quote-time fair
        # the ledger used - reported, not asserted).
        if fill_ref in fairs:
            m_fair = int(fairs[fill_ref])
            m_side_fair = CC_PER_DOLLAR - m_fair if our_side is Side.NO else m_fair
            m_edge = candidate_edge_cc(
                fair_cc=m_fair, bid_cc=int(price), qty_centi=int(qty), our_side=our_side,
                complement_verified=conventions.combo_no_pays_complement,
                fee_cc=int(fee_booked),
            )
            if m_edge == int(edge):
                markout_exact += 1
            else:
                markout_off += 1
                markout_diffs.append(m_side_fair - side_fair)
        else:
            markout_missing += 1
        gross = int(edge) + int(fee_booked)
        fee_035 = int(
            fee_model.trade_fee_cc(
                price_cc=CentiCents(int(price)), qty=CentiContracts(int(qty)), fee_type=COMBO
            )
        )
        net = gross - fee_035
        gross_total += gross
        net_total += net
        fee_total += fee_035
        legs_json = legs_by_ticker.get(ticker)
        if legs_json:
            legs = legs_from_json(legs_json)
            tier, _ = tier_label(policy, [leg.market_ticker for leg in legs], fair_cc)
        else:
            tier = "unmapped"
        premium = int(qty) * int(price) // 100
        t = by_tier[tier]
        t["fills"] += 1
        t["premium_cc"] += premium
        t["gross_cc"] += gross
        t["fee_cc"] += fee_035
        if net < 0:
            net_negative += 1
            net_negative_premium += premium
            t["negative"] += 1
            t["negative_premium_cc"] += premium
        # Would the floor have re-priced this fill? retained per contract at
        # the bid < the per-contract fee at the bid.
        retained_per_ct = side_fair - int(price)
        fee_per_ct = int(
            fee_model.fee_per_contract_cc(price_cc=CentiCents(int(price)), fee_type=COMBO)
        )
        if retained_per_ct < fee_per_ct:
            t["repriced_by_floor"] += 1
            t["repriced_premium_cc"] += premium
    print("=" * 78)
    print("1. FILLS PARITY + FEE-NET EV (post-onset store fills)")
    print("=" * 78)
    print(f"fills: {len(rows)} (first id {first_id})")
    print(f"parity of rfq/edge.candidate_edge_cc(fee=booked, ledger fair) vs"
          f" fills.expected_edge_cc: {parity_ok}/{len(rows)} exact, {parity_bad} off")
    md = sorted(markout_diffs)
    print(f"cross-check with markouts.fair_at_fill_cc (fresh re-price at execution):"
          f" {markout_exact} exact, {markout_off} differ, {markout_missing} missing;"
          f" side-fair drift cc (markout - ledger): min {md[0] if md else 0},"
          f" median {md[len(md) // 2] if md else 0}, max {md[-1] if md else 0}")
    print(f"gross modeled edge {dollars(gross_total)}; measured 0.035 fee {dollars(fee_total)}"
          f" ({fee_total / max(1, gross_total):.0%} of edge); net {dollars(net_total)}")
    print(f"NEGATIVE after fee: {net_negative} fills / {dollars(net_negative_premium)} premium")
    print()
    print("2. FILLS THE FLOOR WOULD HAVE RE-PRICED (retained/ct < fee/ct at the bid)")
    print(f"{'tier':<22}{'fills':>6}{'premium':>12}{'gross':>10}{'fee':>9}{'neg':>5}"
          f"{'neg $':>11}{'floor':>7}{'floor $':>11}")
    for tier in sorted(by_tier, key=lambda k: -by_tier[k]["premium_cc"]):
        t = by_tier[tier]
        print(f"{tier:<22}{t['fills']:>6}{dollars(t['premium_cc']):>12}{dollars(t['gross_cc']):>10}"
              f"{dollars(t['fee_cc']):>9}{t['negative']:>5}{dollars(t['negative_premium_cc']):>11}"
              f"{t['repriced_by_floor']:>7}{dollars(t['repriced_premium_cc']):>11}")
    total_repriced = sum(t["repriced_by_floor"] for t in by_tier.values())
    total_repriced_p = sum(t["repriced_premium_cc"] for t in by_tier.values())
    total_premium = sum(t["premium_cc"] for t in by_tier.values())
    print(f"{'TOTAL':<22}{len(rows):>6}{dollars(total_premium):>12}"
          f"{dollars(gross_total):>10}{dollars(fee_total):>9}{net_negative:>5}"
          f"{dollars(net_negative_premium):>11}{total_repriced:>7}{dollars(total_repriced_p):>11}")
    print()
    return by_tier, len(rows)


# ------------------------------------------------------------ cell floor


def cell_floor_table(con: sqlite3.Connection) -> tuple[dict[CellKey, int] | None, FloorEstimate]:
    # keep in sync with ops/persistence.py Store.settled_grade_rows (read-only
    # copy of the two SELECTs; the row -> GradeRow conversion is the LIVE
    # grade_row_from_store, never a copy)
    ledger = con.execute(
        "SELECT combo_ticker, SUM(contracts_centi), SUM(realized_pnl_cc),"
        " SUM(COALESCE(settlement_fee_cc, 0)), MIN(opened_at),"
        " MAX(COALESCE(reconciled_at, opened_at)), MAX(legs_json)"
        " FROM position_ledger WHERE status = 'settled' AND realized_pnl_cc IS NOT NULL"
        " GROUP BY combo_ticker"
    ).fetchall()
    fills = {
        str(r[0]): r
        for r in con.execute(
            "SELECT combo_ticker, SUM(contracts_centi), SUM(expected_edge_cc),"
            " SUM(COALESCE(fee_cc, 0)), SUM(expected_edge_cc IS NULL) FROM fills"
            " GROUP BY combo_ticker"
        ).fetchall()
    }
    rows: list[GradeRow] = []
    for ticker, ctr, realized, settle_fee, opened_at, settled_at, legs_json in ledger:
        f = fills.get(str(ticker))
        if f is None or f[2] is None or int(f[4] or 0) > 0 or not int(f[1] or 0):
            continue
        row = grade_row_from_store({
            "combo_ticker": str(ticker),
            "ledger_contracts_centi": int(ctr),
            "realized_pnl_cc": int(realized),
            "settlement_fee_cc": int(settle_fee or 0),
            "opened_at": str(opened_at),
            "settled_at": str(settled_at),
            "legs_json": str(legs_json or "[]"),
            "fill_contracts_centi": int(f[1]),
            "expected_edge_cc": int(f[2]),
            "fill_fee_cc": int(f[3] or 0),
        })
        if row is not None:
            rows.append(row)
    est = estimate_retained_floor(rows)
    print("=" * 78)
    print("CELL FLOOR (risk/retained_edge_floor.py on the settled grade, read-only)")
    print("=" * 78)
    print(f"grade rows: {len(rows)}; {json.dumps(summarize(est), default=str)}")
    if est.published:
        print(f"{'cell':<70}{'n':>5}{'G':>5}{'mean':>8}{'se':>7}{'w':>6}{'floor':>7} src")
        for c in sorted(est.cells, key=lambda c: -c.stats.contracts_centi)[:40]:
            se = "-" if c.stats.se_cc is None else f"{c.stats.se_cc:.1f}"
            print(f"{'|'.join(c.cell):<70}{c.stats.n_rows:>5}{c.stats.n_clusters:>5}"
                  f"{c.stats.mean_cc:>8.1f}{se:>7}{c.weight_on_cell:>6.2f}{c.floor_cc:>7}"
                  f" {c.source}")
    print()
    return (dict(est.table) if est.published else None), est


# ---------------------------------------------------------------- section 3


def quotes_section(
    con: sqlite3.Connection,
    policy: MarkupPolicy,
    fee_model: FeeModel,
    params: QuoteParams,
    *,
    decisions_from: int,
    max_quotes: int,
    floor_table: dict[tuple[str, ...], int] | None,
) -> dict[str, Tally]:
    cur = con.execute(
        "SELECT id, context_json FROM decisions WHERE kind = 'quote_sent' AND id >= ?"
        " ORDER BY id LIMIT ?",
        (decisions_from, max_quotes),
    )
    tallies: dict[str, Tally] = defaultdict(Tally)
    skipped = 0
    n = 0
    for _id, ctx in cur:
        d = json.loads(ctx)
        fair = int(d.get("fair_cc", -1))
        no_bid = int(d.get("no_bid_cc", 0))
        width = {k: int(v) for k, v in (d.get("width_cc") or {}).items()}
        mids = d.get("leg_mids_cc") or {}
        if fair < 0 or no_bid <= 0 or not mids:
            skipped += 1
            continue
        legs = list(mids.keys())
        tier, markup = tier_label(policy, legs, fair)
        half = sum(width.values()) // 2
        margin = max(half, markup)
        no_fair = CC_PER_DOLLAR - fair
        # The recorded bid is ground truth for "today": the applied skew is
        # the residual (rebate > 0 raises the bid; widen < 0 lowers it). The
        # grid snap-down residue (< 10cc) rides inside it identically across
        # modes, so per-mode DIFFERENCES are exact.
        skew = no_bid - (no_fair - margin)
        n += 1
        t = tallies[tier]
        t.n += 1
        if skew > 0:
            t.rebate_quotes += 1
        today = build_quote(
            fair_cc=fair, width=width, markup_cc=markup, skew_cc=skew, fee_model=fee_model,
            fee_type=FeeType.QUADRATIC, params=params, mode="width",
        )
        floor = build_quote(
            fair_cc=fair, width=width, markup_cc=markup, skew_cc=skew, fee_model=fee_model,
            fee_type=COMBO, params=params, mode="floor",
        )
        widen = build_quote(
            fair_cc=fair, width=width, markup_cc=markup, skew_cc=skew, fee_model=fee_model,
            fee_type=COMBO, params=params, mode="width",
        )
        cell_q = None
        if floor_table is not None:
            leg_refs = [
                LegRef(market_ticker=tk, event_ticker=tk.rsplit("-", 1)[0], side="yes")
                for tk in legs
            ]
            floor_cc = floor_table.get(cell_key(leg_refs))
            cell_q = build_quote(
                fair_cc=fair, width=width, markup_cc=markup, skew_cc=skew, fee_model=fee_model,
                fee_type=COMBO, params=params, mode="floor", retained_floor_cc=floor_cc,
            )
        today_bid = int(today.no_bid_cc) if today else 0
        if today_bid == no_bid:
            t.parity_today += 1
        if today_bid > 0:
            t.nonzero_today += 1
        for q, moved_attr, cc_attr, nz_attr in (
            (floor, "moved_floor", "moved_floor_cc", "nonzero_floor"),
            (widen, "moved_width", "moved_width_cc", "nonzero_width"),
            (cell_q, "moved_cell", "moved_cell_cc", "nonzero_cell"),
        ):
            if q is None:
                continue
            bid = int(q.no_bid_cc)
            if bid > 0:
                setattr(t, nz_attr, getattr(t, nz_attr) + 1)
            if bid != today_bid:
                setattr(t, moved_attr, getattr(t, moved_attr) + 1)
                setattr(t, cc_attr, getattr(t, cc_attr) + (bid - today_bid))
    print("=" * 78)
    print(f"3. QUOTE REPLAY: {n} quote_sent decisions from rowid {decisions_from}"
          f" (skipped {skipped})")
    print("=" * 78)
    hdr = (f"{'tier':<20}{'n':>7}{'rebate':>7}{'parity':>8}{'nz today':>9}{'nz floor':>9}"
           f"{'nz width':>9}{'mv floor':>9}{'mean cc':>8}{'mv width':>9}{'mean cc':>8}")
    if floor_table is not None:
        hdr += f"{'nz cell':>8}{'mv cell':>8}{'mean cc':>8}"
    print(hdr)
    for tier in sorted(tallies, key=lambda k: -tallies[k].n):
        t = tallies[tier]
        line = (f"{tier:<20}{t.n:>7}{t.rebate_quotes:>7}{t.parity_today:>8}{t.nonzero_today:>9}"
                f"{t.nonzero_floor:>9}{t.nonzero_width:>9}{t.moved_floor:>9}"
                f"{(t.moved_floor_cc / t.moved_floor if t.moved_floor else 0):>8.1f}"
                f"{t.moved_width:>9}"
                f"{(t.moved_width_cc / t.moved_width if t.moved_width else 0):>8.1f}")
        if floor_table is not None:
            line += (f"{t.nonzero_cell:>8}{t.moved_cell:>8}"
                     f"{(t.moved_cell_cc / t.moved_cell if t.moved_cell else 0):>8.1f}")
        print(line)
    zero_tiers = [k for k, t in tallies.items() if t.nonzero_floor == 0]
    print()
    print("GATE: tiers with ZERO non-zero quotes under floor:", zero_tiers or "none")
    return tallies


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default="config/prod-live-wc.local.yaml")
    ap.add_argument(
        "--db", default="file:D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3?mode=ro"
    )
    ap.add_argument("--decisions-from", type=int, default=132892717)
    ap.add_argument("--max-quotes", type=int, default=60_000)
    ap.add_argument("--with-cell-floor", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    cfg = load_config(Path(args.config))
    policy = MarkupPolicy.from_config(cfg.pricing.markup)
    conventions = load_conventions()
    fee_model = FeeModel(MEASURED, conventions)
    params = quote_params(cfg)
    print(f"config {args.config}: fee.mode={cfg.pricing.fee.mode}"
          f" sell_only={params.sell_parlays_only} ml_parlay_cc={policy.ml_parlay_cc}"
          f" conventions maker_is_taker={conventions.maker_is_taker_on_fill}")
    con = sqlite3.connect(args.db, uri=True)
    fills_section(con, policy, fee_model, conventions)
    floor_table = None
    if args.with_cell_floor:
        floor_table, _ = cell_floor_table(con)
    quotes_section(
        con, policy, fee_model, params,
        decisions_from=args.decisions_from, max_quotes=args.max_quotes, floor_table=floor_table,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
