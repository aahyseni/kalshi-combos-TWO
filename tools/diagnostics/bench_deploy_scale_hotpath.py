"""THROUGHPUT GUARD for the deployment scale (CLAUDE.md fix-isolation rule:
"no quote-path throughput regression — benchmark anything on it").

The quote path gained exactly two things:
  1. ``QuoteLifecycle.deploy_scale_for_check()`` — one bool + (armed only) a
     comparison against a generation-cached int;
  2. one ``deploy_scale <= 1.0`` branch at the top of ``LimitChecker.check``.

This times the REAL ``LimitChecker.check`` on a realistic resting book in three
modes — the pre-existing call shape, the new kwarg at its 1.0 default, and the
new kwarg at a solved scale — and reports checks/sec for each.

    .venv/Scripts/python.exe tools/diagnostics/bench_deploy_scale_hotpath.py
"""

from __future__ import annotations

import statistics
import sys
import time
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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
    verified=True,
    source="bench",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

N_POSITIONS = 50          # the live book measured 2026-07-27
N_QUOTES = 200            # max_open_quotes on the live config
N_GAMES = 12
BANKROLL = 2_301_0000     # live risk bankroll, cc

LIMITS = RiskLimits(
    caps_shadow_mode=False,
    per_combo_loss_frac=Fraction(5, 100),
    entity_loss_frac=Fraction(3, 100),
    game_loss_frac=Fraction(50, 100),
    slate_loss_frac=Fraction(65, 100),
    directional_frac=Fraction(40, 100),
    max_open_quotes=200,
)


def legs(i: int) -> tuple[LegRef, ...]:
    g = i % N_GAMES
    return (
        LegRef(f"KXMLBGAME-26JUL27{1800 + g}AAABBB-AAA", f"EV{g}", "yes"),
        LegRef(f"KXMLBKS-26JUL27{1800 + g}AAABBB-AAAP{i % 7}5-5", f"EK{g}", "yes"),
    )


MARG = {t.market_ticker: 0.5 for i in range(N_POSITIONS + N_QUOTES) for t in legs(i)}


def marginals(t: str) -> float | None:
    return MARG.get(t)


def build() -> ExposureBook:
    book = ExposureBook(CONV)
    for i in range(N_POSITIONS):
        book.add_position(
            OpenPosition(
                position_id=f"p{i}",
                combo_ticker=f"COMBO-{i}",
                collection=None,
                our_side=Side.NO,
                contracts=CentiContracts(1_200),
                entry_price_cc=CentiCents(8_000),
                legs=legs(i),
            )
        )
    for i in range(N_QUOTES):
        book.upsert_quote(
            OpenQuoteRisk(
                quote_id=f"q{i}",
                rfq_id=f"r{i}",
                combo_ticker=f"QCOMBO-{i}",
                collection=None,
                yes_bid_cc=CentiCents(0),
                no_bid_cc=CentiCents(8_000),
                contracts=CentiContracts(1_200),
                legs=legs(N_POSITIONS + i),
            )
        )
    return book


def bench(label: str, fn, n: int = 60) -> float:  # type: ignore[no-untyped-def]
    fn()                                          # warm
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    med = statistics.median(samples)
    print(
        f"  {label:<44} median {med * 1e3:7.3f} ms   "
        f"{1.0 / med:9.1f} checks/s"
    )
    return med


def main() -> int:
    book = build()
    checker = LimitChecker(LIMITS)
    pnl = DailyPnl(0, 0)
    cand = [
        OpenPosition(
            position_id="cand",
            combo_ticker="CAND",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(2_500),
            entry_price_cc=CentiCents(8_000),
            legs=legs(3),
        )
    ]
    kw = dict(
        candidate_positions=cand,
        adding_quote=True,
        risk_bankroll_cc=BANKROLL,
        apply_resting_haircut=True,
    )
    print(f"book: {N_POSITIONS} positions + {N_QUOTES} resting quotes, "
          f"{N_GAMES} games, bankroll ${BANKROLL / 10_000:,.2f}")
    print("LimitChecker.check on the quote-time call shape:")
    a = bench("pre-existing call (no kwarg)",
              lambda: checker.check(book, marginals, pnl, **kw))       # type: ignore[arg-type]
    b = bench("new kwarg at its 1.0 default (disarmed)",
              lambda: checker.check(book, marginals, pnl, deploy_scale=1.0, **kw))  # type: ignore[arg-type]
    c = bench("new kwarg at a solved scale (armed)",
              lambda: checker.check(book, marginals, pnl, deploy_scale=1.375, **kw))  # type: ignore[arg-type]
    print()
    print(f"  disarmed vs pre-existing : {100.0 * (b - a) / a:+.2f}%  "
          "(must be ~0 — the disarmed branch returns the SAME limits object)")
    print(f"  armed    vs pre-existing : {100.0 * (c - a) / a:+.2f}%  "
          "(one dataclasses.replace of 5 Fractions per check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
