"""COUNTERFACTUAL: the 2026-08-01 EVENING ruin-frozen tape under the MARGINAL
RUIN gate (``RiskLimits.ruin_gate_marginal``).

THE FREEZE, measured on the live tape (boot 2026-08-01 18:12Z, evening slate):
§(9)'s LEVEL form read p_ruin 0.2994 (== upper, z = 0) vs the 0.05 budget and
refused every candidate — ``skip_portfolio_ruin`` 1,044 rows in the measured
5-minute window, ``quote_sent`` = 0. This script replays the session's ruin
REFUSALS through the marginal admission test and answers:

  1. Of the refusals (rows and DISTINCT RFQs), how many ADMIT under the
     marginal form — split diversifier (allocated dES99 <= 0: a game the
     book's tail decomposition does not touch, or a hedge game) vs
     concentrator (tail-game share beyond the day's EV credit) vs
     EV-UNKNOWN (no candidate EV stored => level form, never admits).
  2. The projected book trajectory had the admitted flow filled: EV, p_ruin
     and P(KILL-night) of the grown book at the tape's measured acceptance
     rate and at the all-fill worst case.

Rule 8: IMPORTS AND DRIVES the live modules — ``build_book_model``,
``compute_book_risk``, ``rfq.eviction_value.allocate_des99_cc`` — and edits
nothing. Store + log opened READ-ONLY.

THE PROXIES, stated plainly (same doctrine as kill_anchor_live_book.py /
marginal_kill_gate_counterfactual.py):
  * Leg marginals come from the freshest ``leg_mids_cc`` on the decisions
    tape before the freeze instant (the frozen bot sent nothing DURING it);
    positions whose legs have no tape mid are RESERVED — exactly the live
    modeled/reserved split mechanism, which is itself most of the freeze
    (see the decomposition report).
  * The acceptance-tape CP-lower is taken as 0.0 for every bucket
    (fail-closed floor): the counterfactual admits EXACTLY the
    zero-marginal-tail flow (dES99 <= 0) and refuses every tail-game
    concentrator — the honest day-one floor of the marginal form.
  * Admitted candidates are grown into the book at their tape-implied fair
    (zero-edge pricing — conservative: the real quotes carried positive EV,
    reported separately from the stored ``candidate_ev_cc``).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np  # noqa: E402

from combomaker.core.conventions import Side  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.pricing.grouping import game_key  # noqa: E402
from combomaker.rfq.eviction_value import allocate_des99_cc  # noqa: E402
from combomaker.risk.exposure import LegRef, OpenPosition  # noqa: E402
from combomaker.sim.book_model import build_book_model  # noqa: E402
from combomaker.sim.book_risk import (  # noqa: E402
    compute_book_risk,
    modeled_cost_basis_cc,
)

_DATA_DIR = Path(os.environ.get("VITALS_DATA_DIR") or (REPO / "data"))
DB_DEFAULT = _DATA_DIR / "combomaker-prod-live-wc.sqlite3"
LOG_DEFAULT = _DATA_DIR / "live_20260801_1412.log"

# The freeze snapshot instant + measured account facts (decomposition report
# 2026-08-01): startup account_standing cash at 18:13:36Z, and the bankroll
# implied by the logged det_max_backstop_cc (= 0.70 x B).
T = "2026-08-01T22:28:12"
T0 = "2026-08-01T18:13:36"
CASH_T0_CC = 15_961_019
BACKSTOP_CC = 19_151_071
BANKROLL_CC = int(round(BACKSTOP_CC / 0.70))
RUIN_FLOOR_FRAC = 0.70
RUIN_BUDGET = 0.05
KILL_FRAC = 0.12
MEASURED_ACCEPT_RATE = 0.127          # the standing ~12.7% RFQ fill measure


def positions_asof(conn: sqlite3.Connection) -> list[OpenPosition]:
    rows = conn.execute(
        "select position_id, combo_ticker, collection_ticker, our_side,"
        " contracts_centi, entry_price_cc, legs_json from position_ledger"
        " where opened_at < ? and (status='open'"
        "   or (status='settled' and reconciled_at >= ?))",
        (T, T),
    ).fetchall()
    out = []
    for pid, ticker, coll, side, contracts, price, legs_json in rows:
        out.append(
            OpenPosition(
                position_id=pid, combo_ticker=ticker, collection=coll,
                our_side=Side.NO if side == "no" else Side.YES,
                contracts=CentiContracts(int(contracts)),
                entry_price_cc=CentiCents(int(price)),
                legs=tuple(
                    LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
                    for x in json.loads(legs_json)
                ),
            )
        )
    return out


def tape_marginals(conn: sqlite3.Connection) -> dict[str, float]:
    """Freshest YES mid per leg from quote_sent decisions before T (tail-scan
    by rowid — decisions has no ``at`` index)."""
    lo = "2026-08-01T18:28"
    mids: dict[str, float] = {}
    cur = conn.execute(
        "select at, kind, context_json from decisions order by rowid desc"
    )
    scanned = below = 0
    while True:
        rows = cur.fetchmany(50_000)
        if not rows:
            break
        scanned += len(rows)
        for at, kind, cj in rows:
            if at is None:
                continue
            if at < lo:
                below += 1
                continue
            below = 0
            if kind != "quote_sent" or at >= T:
                continue
            try:
                ctx = json.loads(cj)
            except (TypeError, ValueError):
                continue
            for ticker, cc in (ctx.get("leg_mids_cc") or {}).items():
                mids.setdefault(ticker, float(cc) / 10_000.0)
        if below > 100_000 or scanned > 6_000_000:
            break
    return mids


def cash_at_t(conn: sqlite3.Connection) -> int:
    prem, fees = conn.execute(
        "select coalesce(sum(price_cc*contracts_centi/100.0),0),"
        " coalesce(sum(coalesce(fee_cc,0)),0) from fills where at>=? and at<?",
        (T0, T),
    ).fetchone()
    credit, = conn.execute(
        "select coalesce(sum(cast(settled_value*contracts_centi*100 as"
        " integer)),0) from position_ledger where status='settled'"
        " and reconciled_at>=? and reconciled_at<?",
        (T0, T),
    ).fetchone()
    return int(round(CASH_T0_CC - prem - fees + credit))


def freeze_refusals(log: Path, lo: str, hi: str) -> list[dict]:
    """skip_portfolio_ruin risk_audit rows in [lo, hi) — streamed, read-only."""
    out = []
    with open(log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"skip_portfolio_ruin"' not in line or '"risk_audit"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = d.get("ts", "")
            if lo <= ts < hi:
                out.append(d)
    return out


def freeze_flow(conn: sqlite3.Connection, lo: str, hi: str) -> list[dict]:
    """The RFQ FLOW the frozen bot was refusing: every RFQ the exchange
    broadcast in [lo, hi) (the risk_audit rows carry an INTERNAL candidate id
    that does not join to the rfqs store, so the arriving-flow population is
    the honest measurable — during the freeze EVERY candidate was refused,
    so the flow IS the refusal set up to repricing multiplicity)."""
    out = []
    # PROXY: the store shows ZERO rfq arrivals recorded in [20:05, 22:28) —
    # the refused candidates were REPRICES of the standing active set. The
    # classifiable population is therefore the surrounding evening-slate
    # arrivals (same games, same shapes); rowid tail-scan (no seen_at index).
    cur = conn.execute(
        "select rfq_id, seen_at, legs_json, contracts_centi, target_cost_cc"
        " from rfqs order by rowid desc"
    )
    rows_all = []
    while True:
        batch = cur.fetchmany(20_000)
        if not batch:
            break
        stop = False
        for row in batch:
            if row[1] < "2026-08-01T18:30":
                stop = True
                break
            if lo <= row[1] < hi:
                rows_all.append(row)
        if stop:
            break
    for rid, seen, legs_json, contracts, target in rows_all:
        legs = json.loads(legs_json)
        games = {
            game_key(x["event_ticker"]) for x in legs if x.get("event_ticker")
        }
        out.append(dict(
            rfq_id=rid, seen_at=seen, legs=legs, games=games,
            contracts_centi=contracts, target_cost_cc=target,
        ))
    return out


def p_from_quantiles(quantiles, thr_cc: float) -> float:
    if not quantiles:
        return float("nan")
    return sum(1 for q in quantiles if q >= thr_cc) / (len(quantiles) - 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_DEFAULT)
    ap.add_argument("--log", type=Path, default=LOG_DEFAULT)
    ap.add_argument("--lo", default="2026-08-01T20:17:42")  # first skip_portfolio_ruin
    ap.add_argument("--hi", default=T)
    ap.add_argument("--samples", type=int, default=20_000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    book = positions_asof(conn)
    marg = tape_marginals(conn)
    cash = cash_at_t(conn)

    have = lambda t: marg.get(t)  # noqa: E731
    model = build_book_model(book, marginals=have, within_game_rho=None)
    equity = cash + int(round(modeled_cost_basis_cc(model)))
    snap = compute_book_risk(
        model, n_samples=args.samples, seed=args.seed, band="high",
        bankroll_cc=BANKROLL_CC, current_equity_cc=equity,
        ruin_floor_frac=RUIN_FLOOR_FRAC,
    )
    kill_line = KILL_FRAC * BANKROLL_CC
    print("=" * 76)
    print("MARGINAL RUIN GATE — COUNTERFACTUAL ON THE FROZEN EVENING TAPE")
    print("=" * 76)
    print(f"book as-of {T}: {len(book)} held / {len(model.positions)} modeled;"
          f" premium ${sum(p.max_loss_cc for p in book) / 1e4:,.2f}"
          f" (reserved ${model.reserved_loss_cc / 1e4:,.2f})")
    print(f"cash ${cash / 1e4:,.2f}; equity basis ${equity / 1e4:,.2f};"
          f" floor ${BACKSTOP_CC / 1e4:,.2f};"
          f" D ${(equity - BACKSTOP_CC) / 1e4:,.2f}")
    print(f"REBUILT snapshot: p_ruin {snap.p_ruin:.4f}  (live logged 0.2994)"
          f"  ev ${snap.ev_cc / 1e4:,.2f}  es99 ${snap.es_99_cc / 1e4:,.2f}"
          f"  P(KILL) {p_from_quantiles(snap.loss_quantiles_cc, kill_line):.4f}")

    # --- the book's per-game tail decomposition (the dES99 allocator input)
    tail_by_game: dict[str, float] = {}
    for tc in snap.per_game_tail_cc:
        tail_by_game[tc.key] = tail_by_game.get(tc.key, 0.0) + tc.loss_cc
    det_by_game: dict[str, float] = {}
    for pos in book:
        for leg in pos.legs:
            if leg.event_ticker:
                g = game_key(leg.event_ticker)
                det_by_game[g] = det_by_game.get(g, 0.0) + pos.max_loss_cc
                break
    tail_games = {g for g, t in tail_by_game.items() if t > 0}
    print(f"tail games (positive tail share): {len(tail_games)} of"
          f" {len(det_by_game)} book games")

    # --- the refusals (rows) + the arriving flow (classifiable population)
    rows = freeze_refusals(args.log, args.lo, args.hi)
    ids = {d.get("rfq_id") for d in rows if d.get("rfq_id")}
    evs = [d["candidate_ev_cc"] for d in rows
           if d.get("candidate_ev_cc") is not None]
    print(f"\nskip_portfolio_ruin risk_audit rows in "
          f"[{args.lo}, {args.hi}):"
          f" {len(rows)}  ({len(ids)} distinct candidates)")
    if evs:
        evs.sort()
        print(f"stored candidate EV on the refusals: median"
              f" ${evs[len(evs) // 2] / 1e4:,.2f}, mean"
              f" ${sum(evs) / len(evs) / 1e4:,.2f}, total"
              f" ${sum(evs) / 1e4:,.2f}")

    flow = freeze_flow(conn, "2026-08-01T19:00", "2026-08-01T22:50")
    verdict = Counter()
    admitted: list[dict] = []
    for meta in flow:
        if not meta["games"]:
            verdict["unknown_games_level_refuse"] += 1
            continue
        det_cc = meta["target_cost_cc"] or 0
        if not det_cc and meta["contracts_centi"]:
            det_cc = meta["contracts_centi"] * 50
        db2 = dict(det_by_game)
        for g in meta["games"]:
            db2[g] = db2.get(g, 0.0) + det_cc
        des99 = allocate_des99_cc(meta["games"], det_cc, tail_by_game, db2)
        # day-one floor: empty acceptance tape => CP-lower 0 => EV credit 0
        if des99 <= 0.0:
            verdict["ADMIT_diversifier"] += 1
            admitted.append(meta)
        else:
            verdict["refuse_concentrator_thin_tape"] += 1
    print(f"\narriving RFQ flow in the window: {len(flow)} RFQs")
    for k, v in verdict.most_common():
        print(f"  {k:38s} {v}")
    n_class = max(1, len(flow))
    admit_frac = verdict["ADMIT_diversifier"] / n_class
    print(f"marginal-form admit fraction of the flow: {admit_frac:.1%}"
          f" -> projected {admit_frac * len(rows):,.0f} of the"
          f" {len(rows)} refusal rows admit")

    # --- trajectory: grow the book with the admitted flow at tape-fair
    def grow(frac: float, tag: str) -> None:
        rng = np.random.default_rng(1)
        take = [a for a in admitted if rng.random() < frac] if frac < 1 else admitted
        newpos: list[OpenPosition] = []
        for i, meta in enumerate(take):
            probs = [marg.get(x["market_ticker"]) for x in meta["legs"]]
            if any(p is None for p in probs):
                continue
            fair_hit = float(np.prod([p for p in probs]))
            price_cc = max(100, min(9_900, int(round((1 - fair_hit) * 10_000))))
            det_cc = meta["target_cost_cc"] or 0
            if det_cc <= 0:
                continue
            contracts = max(100, det_cc * 100 // price_cc)
            newpos.append(
                OpenPosition(
                    position_id=f"cf-{tag}-{i}",
                    combo_ticker=f"CF-{tag}-{i}",
                    collection=None,
                    our_side=Side.NO,
                    contracts=CentiContracts(int(contracts)),
                    entry_price_cc=CentiCents(price_cc),
                    legs=tuple(
                        LegRef(x["market_ticker"], x.get("event_ticker"),
                               x["side"])
                        for x in meta["legs"]
                    ),
                )
            )
        grown_model = build_book_model(
            book + newpos, marginals=have, within_game_rho=None
        )
        # A fill moves cash -> cost basis by exactly the premium paid, so the
        # COST-BASIS equity is invariant to new fills (only settlements move
        # it): the trajectory isolates the DISTRIBUTION effect of the flow.
        g_snap = compute_book_risk(
            grown_model, n_samples=args.samples, seed=args.seed, band="high",
            bankroll_cc=BANKROLL_CC, current_equity_cc=equity,
            ruin_floor_frac=RUIN_FLOOR_FRAC,
        )
        print(f"  {tag:22s} +{len(newpos):4d} fills"
              f"  premium +${sum(p.max_loss_cc for p in newpos) / 1e4:>9,.2f}"
              f"  p_ruin {g_snap.p_ruin:.4f}"
              f"  ev ${g_snap.ev_cc / 1e4:>9,.2f}"
              f"  P(KILL) "
              f"{p_from_quantiles(g_snap.loss_quantiles_cc, kill_line):.4f}")

    print("\nPROJECTED TRAJECTORY (grown book at tape-fair, seed-fixed):")
    print(f"  {'frozen (actual)':22s} +   0 fills  premium +$     0.00"
          f"  p_ruin {snap.p_ruin:.4f}  ev ${snap.ev_cc / 1e4:>9,.2f}"
          f"  P(KILL) "
          f"{p_from_quantiles(snap.loss_quantiles_cc, kill_line):.4f}")
    grow(MEASURED_ACCEPT_RATE, "12.7%-accept")
    grow(1.0, "all-fill worst case")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
