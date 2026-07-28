"""SHADOW BYTE-IDENTITY HARNESS for the entity + slate admission fixes.

Emits a deterministic JSON dump of ``LimitChecker.check``'s FULL breach list
(reason, detail, shadow, game) over a pseudo-random sweep of books, candidates,
resting quotes, bankrolls and cap fractions. It constructs ``RiskLimits`` with
ONLY fields that exist both before and after the fix, so the SAME file runs
against a HEAD worktree and against the working tree; the two dumps must be
byte-identical while both new flags are unarmed.

    py tools/diagnostics/shadow_parity_admission.py > after.json
    PYTHONPATH=<HEAD-worktree>/src py tools/diagnostics/shadow_parity_admission.py > before.json
    cmp before.json after.json
"""

from __future__ import annotations

import json
import random
import sys
from datetime import UTC, datetime
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

CONV = Conventions(
    verified=True, source="parity",
    maker_side_on_yes_accept=Side.YES, maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True, maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)
GAMES = ["KXMLBGAME-26JUL27AAA", "KXMLBGAME-26JUL27BBB", "KXMLBSPREAD-26JUL27CCC"]
ENTS = ["P1", "P2", "P3", "P4"]
STARTS = {
    "KXMLBGAME-26JUL27AAA": datetime(2026, 7, 27, 23, 0, tzinfo=UTC),
    "KXMLBGAME-26JUL27BBB": datetime(2026, 7, 27, 23, 30, tzinfo=UTC),
    "KXMLBSPREAD-26JUL27CCC": datetime(2026, 7, 29, 23, 0, tzinfo=UTC),
}


def legs(rng: random.Random, n: int) -> tuple[LegRef, ...]:
    out = []
    for _ in range(n):
        g = rng.choice(GAMES)
        e = rng.choice(ENTS)
        out.append(LegRef(f"{g}-{e}", g, rng.choice(["yes", "no"])))
    # de-dup market tickers (the classifier rejects duplicate legs upstream)
    seen, uniq = set(), []
    for leg in out:
        if leg.market_ticker in seen:
            continue
        seen.add(leg.market_ticker)
        uniq.append(leg)
    return tuple(uniq)


def pos(rng: random.Random, pid: str) -> OpenPosition:
    return OpenPosition(
        position_id=pid, combo_ticker=f"C-{pid}", collection=None,
        our_side=Side.NO if rng.random() < 0.8 else Side.YES,
        contracts=CentiContracts(rng.choice([1_000, 5_000, 10_000, 30_000])),
        entry_price_cc=CentiCents(rng.choice([500, 2_500, 5_000, 9_000])),
        legs=legs(rng, rng.randint(1, 4)),
    )


def main(n_cases: int = 400) -> None:
    dump = []
    for case in range(n_cases):
        rng = random.Random(case)
        book = ExposureBook(
            CONV,
            is_me_event=(lambda e: True) if case % 3 == 0 else None,
        )
        for i in range(rng.randint(0, 12)):
            book.add_position(pos(rng, f"p{case}_{i}"))
        for i in range(rng.randint(0, 3)):
            q = pos(rng, f"q{case}_{i}")
            book.upsert_quote(
                OpenQuoteRisk(
                    quote_id=f"q{case}_{i}", rfq_id=f"r{case}_{i}",
                    combo_ticker=q.combo_ticker,
                    collection=None, contracts=q.contracts,
                    yes_bid_cc=q.entry_price_cc, no_bid_cc=q.entry_price_cc,
                    legs=q.legs,
                )
            )
        cands = [pos(rng, f"c{case}_{i}") for i in range(rng.randint(0, 2))]
        limits = RiskLimits(
            caps_shadow_mode=bool(case % 2),
            entity_loss_frac=Fraction(rng.choice([1, 3, 8]), 100),
            per_combo_loss_frac=Fraction(rng.choice([1, 5, 20]), 100),
            game_loss_frac=Fraction(rng.choice([5, 30, 90]), 100),
            slate_loss_frac=Fraction(rng.choice([5, 30, 90]), 100),
            directional_frac=Fraction(rng.choice([10, 90]), 100),
            resting_quote_weight=Fraction(rng.choice([1, 2, 5]), 5),
            resting_floor_count=rng.choice([0, 1, 3]),
        )
        bankroll = rng.choice([None, 5_000_000, 20_000_000, 25_945_331])
        prov = None if case % 4 == 0 else (
            lambda t: STARTS.get(t.rsplit("-", 1)[0])
        )
        breaches = LimitChecker(limits).check(
            book, lambda t: 0.5, DailyPnl(realized_cc=-rng.randint(0, 500_000)),
            candidate_positions=cands or None,
            adding_quote=bool(case % 5 == 0),
            risk_bankroll_cc=bankroll,
            start_time_provider=prov,
            apply_resting_haircut=bool(case % 7 == 0),
        )
        dump.append(
            [case, [[b.reason.value, b.detail, b.shadow, b.game] for b in breaches]]
        )
    json.dump(dump, sys.stdout, indent=0, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
