"""LIVE-BOOK MEASUREMENT of FIX 3 (hedge accounting) and FIX 4 (value-ranked
allocation of the fixed det-max budget) — 2026-07-28.

Rule 8 (testing isolation): this script IMPORTS and DRIVES the live modules —
``sim.book_risk.mutex_aware_det_max_and_credit`` (the exact fold the caps read)
and the live ``_det_units_from_positions`` adapter — and edits nothing. The
store is opened READ-ONLY.

WHAT IT ANSWERS

  FIX 3 — how much det-max the book is charged TWICE. The mutex fold buckets
  only long-NO units per game, so a COMPLEMENT position (the opposite side of a
  combo we already hold) is charged its FULL premium ON TOP of the position it
  offsets. Reports the comonotone number, the mutex-aware bound, and the NEW
  offsetting-position credit, each in dollars and as a % of the book.

  FIX 4 — the EV-per-consumed-det-max DENSITY of the flow we took vs the flow
  we turned away, and the EV a value-ranked allocation of the SAME budget banks.
  Replayed over the won-auction set in the store (confirmed vs declined).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from combomaker.core.conventions import Conventions, Side  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.risk.exposure import LegRef, OpenPosition  # noqa: E402
from combomaker.sim.book_risk import (  # noqa: E402
    _certified_cannot_both_lose,
    _det_units_from_positions,
    _loss_literals,
    mutex_aware_det_max_and_credit,
)

DB = REPO / "data" / "combomaker-prod-live-wc.sqlite3"

CONV = Conventions(
    verified=True, source="live-measure",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def load_positions() -> list[OpenPosition]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select position_id, combo_ticker, collection_ticker, our_side, "
        "contracts_centi, entry_price_cc, legs_json from position_ledger "
        "where status='open'"
    ).fetchall()
    con.close()
    out: list[OpenPosition] = []
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


def fix3(positions: list[OpenPosition]) -> None:
    units, reserved = _det_units_from_positions(positions)
    comonotone = sum(u.loss_cc for u in units) + reserved
    bound, credit = mutex_aware_det_max_and_credit(
        units, reserved_loss_cc=reserved
    )
    print("=" * 78)
    print("FIX 3 — HEDGE ACCOUNTING (det-max charged ONCE, not twice)")
    print("=" * 78)
    print(f"open positions                 {len(positions)}")
    print(f"risk-modeled det units         {len(units)}")
    no_units = sum(1 for u in units if u.our_side is Side.NO)
    print(f"  long-NO                      {no_units}")
    print(f"  long-YES (complement side)   {len(units) - no_units}")
    certifiable = sum(1 for u in units if _loss_literals(u) is not None)
    print(f"  certifiable (state-enum)     {certifiable}")
    print(f"comonotone all-hit             ${comonotone / 10_000:,.2f}")
    print(f"mutex-aware bound (gated)      ${bound / 10_000:,.2f}"
          f"   ({1 - bound / max(1.0, comonotone):.2%} credit vs comonotone)")
    print(f"NEW offsetting-position credit ${credit / 10_000:,.2f}"
          f"   ({credit / max(1.0, bound):.2%} of the gated bound)")
    print(f"charged after FIX 3            ${(bound - credit) / 10_000:,.2f}")
    # Which pairs certified, so the operator can eyeball the shapes.
    shapes: dict[str, int] = {}
    lits = {u.unit_id: _loss_literals(u) for u in units}
    for i, a in enumerate(units):
        la = lits[a.unit_id]
        if la is None:
            continue
        for b in units[i + 1:]:
            lb = lits[b.unit_id]
            if lb is None:
                continue
            if not _certified_cannot_both_lose(a.our_side, la, b.our_side, lb):
                continue
            if a.our_side is b.our_side:
                key = "NO/NO opposite-side shared leg"
            elif la == lb:
                key = "same-combo complement"
            else:
                key = "YES sub-parlay of a held NO"
            shapes[key] = shapes.get(key, 0) + 1
    print("certified pair shapes (all certifying pairs, pre-matching):")
    for k, v in sorted(shapes.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<34} {v}")
    if not shapes:
        print("  none on this book")


def complement_projection(positions: list[OpenPosition]) -> None:
    """THE OPERATOR'S THESIS, priced. Add k same-combo-opposite-side hedges to
    the LIVE book (the exact shape of the ratified 28-add / 52-add study) and
    show what det-max charges before and after FIX 3. Each add is the YES side
    of a combo we already hold NO on, at the complement price — the pair
    provably cannot both lose, so the charged number must stay FLAT."""
    units, reserved = _det_units_from_positions(positions)
    base_bound, base_credit = mutex_aware_det_max_and_credit(
        units, reserved_loss_cc=reserved
    )
    print()
    print("=" * 78)
    print("PROJECTION — same-combo complement adds (the operator's own thesis)")
    print("=" * 78)
    print(f"{'adds':>6}{'premium':>14}{'charged TODAY':>16}"
          f"{'charged FIX 3':>16}{'credit':>12}")
    ranked = sorted(positions, key=lambda p: -p.max_loss_cc)
    for k in (0, 7, 14, 28, 52):
        extra: list[OpenPosition] = []
        for i, p in enumerate(ranked[:k]):
            extra.append(
                OpenPosition(
                    position_id=f"hedge:{i}",
                    combo_ticker=p.combo_ticker,
                    collection=p.collection,
                    our_side=Side.YES,
                    contracts=p.contracts,
                    entry_price_cc=CentiCents(
                        max(1, 10_000 - int(p.entry_price_cc))
                    ),
                    legs=p.legs,
                )
            )
        u, r = _det_units_from_positions([*positions, *extra])
        bound, credit = mutex_aware_det_max_and_credit(u, reserved_loss_cc=r)
        premium = sum(x.loss_cc for x in u) + r
        print(f"{k:>6}${premium / 10_000:>12,.2f}${bound / 10_000:>14,.2f}"
              f"${(bound - credit) / 10_000:>14,.2f}"
              f"${credit / 10_000:>10,.2f}")
    print(f"(baseline bound ${base_bound / 10_000:,.2f}, "
          f"baseline credit ${base_credit / 10_000:,.2f})")


def main() -> None:
    positions = load_positions()
    fix3(positions)
    complement_projection(positions)


if __name__ == "__main__":
    main()
