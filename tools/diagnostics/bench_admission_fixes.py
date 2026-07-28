"""THROUGHPUT: ``LimitChecker.check`` before/after the entity + slate fixes.

Runs the SAME live-shaped book (rebuilt from ``position_ledger``) through
``check`` and reports us/call. Run it from the working tree and from a HEAD
worktree to get the before/after pair the throughput-never-regresses rule wants.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from combomaker.core.conventions import Conventions, Side  # noqa: E402
from combomaker.core.money import CentiCents  # noqa: E402
from combomaker.core.quantity import CentiContracts  # noqa: E402
from combomaker.risk.exposure import (  # noqa: E402
    ExposureBook,
    LegRef,
    OpenPosition,
    OpenQuoteRisk,
)
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits  # noqa: E402

DB = REPO / "data" / "combomaker-prod-live-wc.sqlite3"
CONV = Conventions(
    verified=True, source="bench",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def build() -> tuple[ExposureBook, list[OpenPosition]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "select position_id, combo_ticker, our_side, contracts_centi, "
        "entry_price_cc, legs_json from position_ledger where status='open'"
    ).fetchall()
    con.close()
    book = ExposureBook(CONV, is_me_event=lambda e: "GAME" in e)
    pos: list[OpenPosition] = []
    for pid, combo, side, contracts, price, legs_json in rows:
        legs = tuple(
            LegRef(x["market_ticker"], x.get("event_ticker"), x["side"])
            for x in json.loads(legs_json)
        )
        p = OpenPosition(
            position_id=pid, combo_ticker=combo, collection=None,
            our_side=Side.NO if side == "no" else Side.YES,
            contracts=CentiContracts(int(contracts)),
            entry_price_cc=CentiCents(int(price)), legs=legs,
        )
        pos.append(p)
        book.add_position(p)
    # 20 resting quotes taken from the same shapes (the live slot count).
    for i, p in enumerate(pos[:20]):
        book.upsert_quote(
            OpenQuoteRisk(
                quote_id=f"q{i}", rfq_id=f"r{i}", combo_ticker=f"Q-{i}",
                collection=None, contracts=p.contracts,
                yes_bid_cc=p.entry_price_cc, no_bid_cc=p.entry_price_cc,
                legs=p.legs,
            )
        )
    return book, pos


def main(n: int = 300) -> None:
    book, pos = build()
    cand = pos[0]
    limits = RiskLimits(
        caps_shadow_mode=False,
        entity_loss_frac=Fraction(3, 100),
        per_combo_loss_frac=Fraction(5, 100),
        game_loss_frac=Fraction(50, 100),
        slate_loss_frac=Fraction(1, 100),      # force the slate cap to breach
        directional_frac=Fraction(99, 100),
        resting_quote_weight=Fraction(2, 5),
        resting_floor_count=3,
    )
    seen: list[object] = []
    new_build = "entity_admission_observer" in LimitChecker.check.__code__.co_varnames

    def run(lim: RiskLimits, extra: dict[str, object]) -> float:
        checker = LimitChecker(lim)
        checker.check(book, lambda t: 0.5, DailyPnl(),
                      candidate_positions=[cand], risk_bankroll_cc=20_000_000,
                      apply_resting_haircut=True,
                      **extra)  # type: ignore[arg-type]
        t0 = time.perf_counter()
        for _ in range(n):
            checker.check(
                book, lambda t: 0.5, DailyPnl(),
                candidate_positions=[cand], risk_bankroll_cc=20_000_000,
                apply_resting_haircut=True, **extra,  # type: ignore[arg-type]
            )
        return (time.perf_counter() - t0) / n * 1e6

    print(f"positions={len(pos)} quotes={len(book.open_quotes)}")
    print(f"  DEFAULT (both levers off)   : {run(limits, {}):8.1f} us/call")
    if new_build:
        obs: dict[str, object] = {
            "entity_admission_observer": lambda *a: seen.append(a),
            "slate_partition_observer": lambda *a: seen.append(a),
        }
        shadow = replace(
            limits, entity_admission_enabled=True, slate_partition_enabled=True
        )
        armed = replace(
            limits, entity_admission_armed=True, slate_partition_armed=True
        )
        print(f"  SHADOW read-outs enabled    : {run(shadow, obs):8.1f} us/call")
        print(f"  ARMED                       : {run(armed, {}):8.1f} us/call")
        checker = LimitChecker(limits)
        breaches = checker.check(
            book, lambda t: 0.5, DailyPnl(), candidate_positions=[cand],
            risk_bankroll_cc=20_000_000, apply_resting_haircut=True,
        )
        print(f"  breaches={sorted({b.reason.value for b in breaches})}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
