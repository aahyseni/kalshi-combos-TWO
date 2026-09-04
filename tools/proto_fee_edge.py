"""Fee seam + fee-aware floor — PROTOTYPE and PARITY CHECK (hard rule 8).

The 2026-09-04 fee-seam build (docs/reports/2026-09-04-build-fee-seam-
rebate-floor.md) prototyped its two pieces of arithmetic against recorded
exchange data before porting them into the live modules; the review (S2)
asked for the prototype to be COMMITTED so the port stays reproducible.
This script is that artefact. It re-derives, in plain arithmetic and with
no import from the module under test, each rule the live code implements,
runs both on the same recorded inputs, and asserts equality to the
centi-cent:

  A. THE EXCHANGE'S REPORTED FEE (tests/fixtures/ground_truth/
     maker_fee_20260820.json + exchange_fills_uncharged_20260827.json —
     the whole 4,228-fill tape):
        fee_cost = ceil_cc(coef · C · P · (1−P)) + (ceil_cc(C · P) − C · P)
     — prototype: the residue is an integer-exact Fraction on every maker
     fill; it is the WHOLE fee_cost on every pre-onset fill (no maker fee
     charged) and a whole-cc fee remains on every post-onset fill.
     PARITY: exchange/fills.fee_observation_from_fill reports exactly the
     prototype's residue-free fee on every row.

  B. THE MAKER COEFFICIENT — prototype: least squares through the origin
     AND the exact feasible set ∩_i (charged_i − 1, charged_i] / X_i over
     the charged fills, pinned to the 1e-4 publication quantum.
     PARITY: pricing/fee_observer.fit_maker_coefficient returns the same
     Fraction; pricing/fees.FeeModel.trade_fee_cc at that coefficient
     equals the charged fee on 540/540 fills.

  C. THE FEE-AWARE FLOOR (review fix M3) — prototype: with F the whole-fill
     fee at the fee-maximising price of the plausible bid range,
        m_min = ⌈(F + 1) · 100 / qty⌉,  margin ← max(margin, m_min),
        rebate ≤ margin − m_min (− cell floor),  bid = fair_no − margin + rebate,
     snapped DOWN to the grid.
     PARITY: pricing/quote.construct_quote(fee_mode="floor") posts the
     prototype's bid on every point of a (fair × markup × skew × qty × cell
     floor) grid, and every one of those bids has a strictly positive fee-
     net confirm edge (rfq/edge.candidate_edge_cc with the whole-fill fee).

Usage:
    PYTHONPATH=src .venv/Scripts/python.exe -m tools.proto_fee_edge
"""

from __future__ import annotations

import json
import math
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.exchange.fills import fee_observation_from_fill
from combomaker.marketdata.grid import PriceGrid
from combomaker.pricing.fee_observer import fit_maker_coefficient
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import ConstructedQuote, QuoteParams, construct_quote
from combomaker.rfq.edge import candidate_edge_cc

ROOT = Path(__file__).resolve().parents[1]
GT = ROOT / "tests" / "fixtures" / "ground_truth"
ONSET = "2026-08-20T09:07"
QUANTUM = Fraction(1, 10_000)
COMBO = FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
JsonDict = dict[str, Any]


# ----------------------------------------------------------------- recorded tape


def whole_tape() -> list[JsonDict]:
    charged = json.loads((GT / "maker_fee_20260820.json").read_text(encoding="utf-8"))
    uncharged = json.loads(
        (GT / "exchange_fills_uncharged_20260827.json").read_text(encoding="utf-8")
    )
    rows = [dict(zip(uncharged["columns"], r, strict=True)) for r in uncharged["rows"]]
    rows += [
        {
            "fill_id": r["fill_id"], "created_time": r["created_time"], "ticker": r["ticker"],
            "side": "no", "count_fp": r["count_fp"], "no_price_dollars": r["no_price_dollars"],
            "fee_cost": r["fee_cost"], "is_taker": False,
        }
        for r in charged["charged_maker_fills"]
    ]
    return rows


# --------------------------------------------------------- A. residue prototype


def proto_row(row: JsonDict) -> tuple[Fraction, Fraction, Fraction, Fraction] | None:
    """(C contracts, P price, residue cc, fee_cost cc) in exact Fractions."""
    if row.get("is_taker") is not False:
        return None
    price = row.get("no_price_dollars") if row.get("side") == "no" else row.get("yes_price_dollars")
    if price is None:
        price = row.get("no_price_dollars") or row.get("yes_price_dollars")
    c = Fraction(Decimal(str(row["count_fp"])))
    p = Fraction(Decimal(str(price)))
    cost_cc = c * p * CC_PER_DOLLAR
    residue = math.ceil(cost_cc) - cost_cc
    fee_cost_cc = Fraction(Decimal(str(row.get("fee_cost") or "0"))) * CC_PER_DOLLAR
    return c, p, residue, fee_cost_cc


def section_a(rows: list[JsonDict]) -> list[tuple[Fraction, Fraction, int]]:
    """Returns the charged maker fills as (C, P, fee_cc)."""
    n_maker = n_int = n_pre_zero = n_post_pos = 0
    charged: list[tuple[Fraction, Fraction, int]] = []
    parity = 0
    for row in rows:
        r = proto_row(row)
        if r is None:
            continue
        c, p, residue, fee_cost_cc = r
        n_maker += 1
        net = fee_cost_cc - residue
        if net.denominator == 1:
            n_int += 1
        fee_cc = max(0, math.ceil(net))
        pre = row["created_time"] < ONSET
        if pre and fee_cc == 0:
            n_pre_zero += 1
        if not pre and fee_cc > 0:
            n_post_pos += 1
            charged.append((c, p, fee_cc))
        live = fee_observation_from_fill(row)
        assert live is not None, row
        if live.fee_cc == fee_cc:
            parity += 1
    print("A. reported fee = ceil_cc(fee) + cost residue")
    print(f"   maker fills {n_maker}: residue-free fee integer on {n_int}/{n_maker};"
          f" pre-onset uncharged {n_pre_zero}; post-onset charged {n_post_pos}")
    print(f"   PARITY exchange/fills.fee_observation_from_fill == prototype: {parity}/{n_maker}")
    assert n_int == n_maker and parity == n_maker
    n_pre_maker = sum(1 for r in rows if r.get("is_taker") is False and r["created_time"] < ONSET)
    assert n_pre_zero == n_pre_maker
    return charged


# ------------------------------------------------------ B. coefficient prototype


def section_b(charged: list[tuple[Fraction, Fraction, int]], rows: list[JsonDict]) -> Fraction:
    xs = [c * p * (1 - p) * CC_PER_DOLLAR for c, p, _ in charged]
    ys = [Fraction(fee) for _, _, fee in charged]
    ls = sum(x * y for x, y in zip(xs, ys, strict=True)) / sum(x * x for x in xs)
    lo = max((y - 1) / x for x, y in zip(xs, ys, strict=True))
    hi = min(y / x for x, y in zip(xs, ys, strict=True))
    first = math.floor(lo / QUANTUM) + 1
    last = math.floor(hi / QUANTUM)
    multiples = [Fraction(k) * QUANTUM for k in range(first, last + 1)]
    assert len(multiples) == 1, multiples
    pin = multiples[0]
    live_fit = fit_maker_coefficient(
        [o for o in (fee_observation_from_fill(r) for r in rows) if o is not None]
    )
    model = FeeModel(FeeSchedule(taker_coef=Fraction(7, 100), maker_coef=pin), CONV)
    exact = 0
    for c, p, fee in charged:
        live_fee = model.trade_fee_cc(
            price_cc=CentiCents(int(p * CC_PER_DOLLAR)), qty=CentiContracts(int(c * 100)),
            fee_type=COMBO,
        )
        if int(live_fee) == fee == math.ceil(pin * c * p * (1 - p) * CC_PER_DOLLAR):
            exact += 1
    print("B. maker coefficient")
    print(f"   LS through origin {float(ls):.7f}; feasible set ({float(lo):.6f}, {float(hi):.6f}]"
          f" holds exactly one quantum multiple: {pin} = {float(pin):.4f}")
    print("   PARITY pricing/fee_observer.fit_maker_coefficient == prototype pin:"
          f" {live_fit == pin}")
    print(f"   PARITY pricing/fees.FeeModel.trade_fee_cc == charged on {exact}/{len(charged)}")
    assert live_fit == pin and exact == len(charged)
    return pin


# ------------------------------------------------------------ C. floor prototype

CONV = Conventions(
    verified=True, source="proto", maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO, maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)
GRID = PriceGrid.from_market_payload(
    {"ticker": "P", "price_ranges": [{"start": "0.001", "end": "0.999", "step": "0.001"}]}
)
PARAMS = QuoteParams(
    base_width_cc=0, per_leg_width_cc=0, size_width_cc_per_100=0, time_width_cc=0,
    sell_parlays_only=True, free_money_margin_cc=100,
)


def proto_fee_total(coef: Fraction, price_cc: int, qty: int) -> int:
    p = Fraction(price_cc, CC_PER_DOLLAR)
    return math.ceil(coef * Fraction(qty, 100) * p * (1 - p) * CC_PER_DOLLAR)


def proto_floor_bid(
    coef: Fraction, fair_cc: int, markup: int, skew: int, qty: int, cell_floor: int | None
) -> int:
    """The prototype of construct_quote(fee_mode='floor') for a sell-only
    quote with no width components (margin == markup)."""
    no_fair = CC_PER_DOLLAR - fair_cc
    margin = markup
    # The pre-existing 8/16 cap (rebate <= margin // 2 of the TIER margin)
    # applies first and only when no measured cell floor is published; the
    # fee range is then probed with the clamped skew (the bid can move no
    # further than that).
    if cell_floor is None and skew > margin // 2:
        skew = margin // 2

    def m_min(fee_total: int) -> int:
        return -(-(fee_total + 1) * 100 // qty)

    probe = max(margin, m_min(proto_fee_total(coef, CC_PER_DOLLAR // 2, qty)))
    lower = max(0, no_fair - probe - abs(skew))
    nearest = min(max(CC_PER_DOLLAR // 2, lower), no_fair)
    fee_total = max(proto_fee_total(coef, no_fair, qty), proto_fee_total(coef, nearest, qty))
    floor = m_min(fee_total)
    margin = max(margin, floor)
    cap = margin - floor
    if cell_floor is not None:
        cap = min(cap, max(0, margin - floor - cell_floor))
    rebate = min(skew, cap) if skew > 0 else skew
    raw = no_fair - margin + rebate
    if raw <= 0:
        return 0
    return min(raw, CC_PER_DOLLAR) // 10 * 10


def section_c(coef: Fraction) -> None:
    model = FeeModel(FeeSchedule(taker_coef=Fraction(7, 100), maker_coef=coef), CONV)
    cases = parity = positive = 0
    for fair_cc in range(500, 9_600, 37):
        for markup in (60, 100, 200, 300):
            for skew in (-150, 0, 40, 300):
                for qty in (100, 120, 250, 1_000):
                    for cell in (None, 0, 15, 590):
                        cases += 1
                        q = construct_quote(
                            joint=JointEstimate(p=fair_cc / CC_PER_DOLLAR, uncertainty=0.0,
                                                frechet_lo=0.0, frechet_hi=1.0, notes=()),
                            n_legs=2, qty=CentiContracts(qty), grid=GRID, fee_model=model,
                            fee_type=COMBO, fee_multiplier=Fraction(1), time_to_close_s=36_000.0,
                            in_play=False, yes_cap_cc=CentiCents(CC_PER_DOLLAR),
                            no_cap_cc=CentiCents(CC_PER_DOLLAR), inventory_skew_cc=skew,
                            markup_cc=markup, params=PARAMS, fee_mode="floor",
                            retained_floor_cc=cell,
                        )
                        live = int(q.no_bid_cc) if isinstance(q, ConstructedQuote) else 0
                        proto = proto_floor_bid(coef, fair_cc, markup, skew, qty, cell)
                        if live == proto:
                            parity += 1
                        if live > 0:
                            fee = int(model.trade_fee_cc(
                                price_cc=CentiCents(live), qty=CentiContracts(qty), fee_type=COMBO
                            ))
                            edge = candidate_edge_cc(
                                fair_cc=fair_cc, bid_cc=live, qty_centi=qty, our_side=Side.NO,
                                complement_verified=True, fee_cc=fee,
                            )
                            if edge is not None and edge > 0:
                                positive += 1
                        else:
                            positive += 1  # no bid, nothing to win
    print("C. fee-aware floor (m_min = ceil((F + 1) * 100 / qty))")
    print(f"   PARITY pricing/quote.construct_quote(floor) == prototype bid: {parity}/{cases}")
    print(f"   confirm edge > 0 on every posted floor bid: {positive}/{cases}")
    assert parity == cases and positive == cases


def main() -> int:
    rows = whole_tape()
    print(f"recorded tape: {len(rows)} exchange fills")
    charged = section_a(rows)
    pin = section_b(charged, rows)
    section_c(pin)
    print("ALL PARITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
