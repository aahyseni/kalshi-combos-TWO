"""Tests for the R2 %-of-bankroll cap hierarchy + slate cap (Phase 2, SHADOW).

Each new cap fires at the right integer-exact threshold and does NOT fire just
under it; SHADOW breaches are log-only (verified behaviourally against the
lifecycle in ``test_risk_shadow_mode.py``); fail-closed on no/zero bankroll; the
slate roll-up incl. the UNKNOWN pool; the starvation watchdog fires + resets;
and the two money axes are never summed (the utilization backstop binds on the
notional axis, the game-loss cap on the loss axis, with the SAME positions).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

import pytest

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, OpenQuoteRisk
from combomaker.risk.limits import (
    UNKNOWN_SLATE_KEY,
    Breach,
    DailyPnl,
    HaltInputs,
    LimitChecker,
    RiskLimits,
    StarvationWatchdog,
    slate_key_for_start,
    threshold_cc,
)

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# Two legs in two DIFFERENT games (distinct game codes via the hyphen split).
MARGINALS = {"A": 0.5, "B": 0.5}


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


MARG = provider(MARGINALS)

LEG_A = (LegRef("A", "SER-GAME1", "yes"),)
LEG_B = (LegRef("B", "SER-GAME2", "yes"),)

# $2,000 bankroll in cc. Caps default to the researched START values: game 8%,
# per-combo 1%, directional 10%, slate 8%, daily 6%, drawdown 10%, hard 12%.
BANKROLL_2K = 20_000_000  # $2,000.00 in centi-cents


def make_position(
    pid: str,
    legs: tuple[LegRef, ...] = LEG_A,
    *,
    our_side: Side = Side.YES,
    contracts: int = 100,
    entry_price: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=our_side,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


def make_quote(
    qid: str,
    legs: tuple[LegRef, ...] = LEG_A,
    *,
    yes_bid: int = 2_000,
    no_bid: int = 2_000,
    contracts: int = 1_000,
) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=qid,
        rfq_id=f"rfq-{qid}",
        combo_ticker=f"COMBO-{qid}",
        collection=None,
        yes_bid_cc=CC(yes_bid),
        no_bid_cc=CC(no_bid),
        contracts=Q(contracts),
        legs=legs,
    )


def empty_book() -> ExposureBook:
    return ExposureBook(CONVENTIONS)


def r2(breaches: list[Breach]) -> list[Breach]:
    """The R2 (shadow) breaches only — the new %-cap layer."""
    return [b for b in breaches if b.shadow]


def r2_reasons(breaches: list[Breach]) -> list[ReasonCode]:
    return [b.reason for b in r2(breaches)]


# A checker whose caps are LOOSE except the one under test, so exactly one R2 cap
# fires. Base uses tiny fracs where we want no fire, big where we do. We build
# per-test to keep intent local.
def checker(**overrides: object) -> LimitChecker:
    return LimitChecker(RiskLimits(**overrides))  # type: ignore[arg-type]


# All-loose R2 fracs so a specific cap can be isolated; the enforced hard-dollar
# caps are left at defaults (huge relative to the tiny test books).
LOOSE: dict[str, object] = {
    "game_loss_frac": Fraction(99, 100),
    "per_combo_loss_frac": Fraction(99, 100),
    "directional_frac": Fraction(99, 100),
    "slate_loss_frac": Fraction(99, 100),
    "daily_loss_frac": Fraction(99, 100),
    "drawdown_frac": Fraction(99, 100),
    "hard_trip_frac": Fraction(99, 100),
    "portfolio_cvar_frac": Fraction(99, 100),
    "absolute_notional_multiple": 999,
}


@dataclass(frozen=True, slots=True)
class FakeBookRisk:
    """Minimal PortfolioRisk stand-in for the CVaR cap tests (a real
    ``BookRiskSnapshot`` is heavier to build; only ``usable`` +
    ``operative_es_99_cc`` are read by the cap)."""

    usable: bool
    operative_es_99_cc: float


class TestThresholdExactness:
    def test_threshold_is_integer_exact_no_float(self) -> None:
        # 8% of $2,000 = $160 = 1_600_000 cc, exactly, via integer arithmetic.
        assert threshold_cc(Fraction(8, 100), BANKROLL_2K) == 1_600_000
        # A frac that a float would fumble: 1/3 of an odd cc floors exactly.
        assert threshold_cc(Fraction(1, 3), 10_000_001) == 3_333_333
        # 1% of $2,000 = $20 = 200_000 cc.
        assert threshold_cc(Fraction(1, 100), BANKROLL_2K) == 200_000


class TestGameLossCap:
    def _one_game_book(self, loss_cc_target: int) -> ExposureBook:
        # A single position on GAME1 whose max_loss_cc == loss_cc_target.
        # max_loss = contracts * entry_price // 100. Use contracts=10_000 (=100.00
        # contracts) so max_loss_cc = 100 * entry_price.
        book = empty_book()
        entry = loss_cc_target // 100
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=entry))
        return book

    def test_fires_just_above_threshold(self) -> None:
        # game cap 8% of $2,000 = 1_600_000 cc. Loss = 1_600_100 > threshold.
        book = self._one_game_book(1_600_100)
        breaches = checker(game_loss_frac=Fraction(8, 100), **{
            k: v for k, v in LOOSE.items() if k != "game_loss_frac"
        }).check(book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL_2K)
        assert ReasonCode.SKIP_GAME_LOSS_CAP in r2_reasons(breaches)
        breach = next(b for b in r2(breaches) if b.reason is ReasonCode.SKIP_GAME_LOSS_CAP)
        assert breach.shadow is True

    def test_does_not_fire_at_or_just_under_threshold(self) -> None:
        # Loss EXACTLY at threshold does NOT fire (strict >).
        book = self._one_game_book(1_600_000)
        breaches = checker(game_loss_frac=Fraction(8, 100), **{
            k: v for k, v in LOOSE.items() if k != "game_loss_frac"
        }).check(book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL_2K)
        assert ReasonCode.SKIP_GAME_LOSS_CAP not in r2_reasons(breaches)


class TestPerComboLossCap:
    def test_fires_on_a_candidate_over_1pct(self) -> None:
        # per-combo 1% of $2,000 = 200_000 cc. Candidate max_loss = 200_100.
        cand = make_position("cand", LEG_A, contracts=10_000, entry_price=2_001)
        assert cand.max_loss_cc == 200_100
        breaches = checker(per_combo_loss_frac=Fraction(1, 100), **{
            k: v for k, v in LOOSE.items() if k != "per_combo_loss_frac"
        }).check(
            empty_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL_2K,
        )
        assert ReasonCode.SKIP_PER_COMBO_LOSS_CAP in r2_reasons(breaches)

    def test_does_not_fire_at_threshold(self) -> None:
        cand = make_position("cand", LEG_A, contracts=10_000, entry_price=2_000)
        assert cand.max_loss_cc == 200_000  # exactly 1%
        breaches = checker(per_combo_loss_frac=Fraction(1, 100), **{
            k: v for k, v in LOOSE.items() if k != "per_combo_loss_frac"
        }).check(
            empty_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL_2K,
        )
        assert ReasonCode.SKIP_PER_COMBO_LOSS_CAP not in r2_reasons(breaches)

    def test_binds_on_loss_not_notional(self) -> None:
        # A candidate whose $1 NOTIONAL is huge (100 contracts → $100 notional)
        # but whose LOSS (premium) is tiny (1¢ entry → $1 loss) must NOT trip the
        # per-combo LOSS cap: the cap is premium-at-risk, never the $1 notional.
        cand = make_position("cand", LEG_A, contracts=10_000, entry_price=100)
        assert cand.max_loss_cc == 10_000                       # $1 premium
        assert cand.gross_settlement_notional_cc == 1_000_000   # $100 notional
        breaches = checker(per_combo_loss_frac=Fraction(1, 100), **{
            k: v for k, v in LOOSE.items() if k != "per_combo_loss_frac"
        }).check(
            empty_book(), MARG, DailyPnl(),
            candidate_positions=[cand], risk_bankroll_cc=BANKROLL_2K,
        )
        assert ReasonCode.SKIP_PER_COMBO_LOSS_CAP not in r2_reasons(breaches)


class TestUtilizationBackstop:
    def test_binds_on_notional_axis_even_when_loss_is_tiny(self) -> None:
        # 100.00 contracts @ 1¢ → LOSS $1 (10_000cc) but NOTIONAL $100
        # (1_000_000cc). With a tiny bankroll the 3x-notional backstop trips on
        # the NOTIONAL axis while the game LOSS cap (on the SAME position) does
        # NOT — proof the two money axes are never summed nor confused.
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=100))
        small_bankroll = 200_000  # $20 → 3x = 600_000cc < 1_000_000cc notional
        breaches = checker(
            absolute_notional_multiple=3,
            game_loss_frac=Fraction(99, 100),   # loose: would need $19.80 loss
            **{k: v for k, v in LOOSE.items()
               if k not in ("absolute_notional_multiple", "game_loss_frac")},
        ).check(book, MARG, DailyPnl(), risk_bankroll_cc=small_bankroll)
        rs = r2_reasons(breaches)
        assert ReasonCode.SKIP_UTILIZATION_BACKSTOP in rs
        assert ReasonCode.SKIP_GAME_LOSS_CAP not in rs  # loss axis NOT tripped

    def test_backstop_binds_without_a_fresh_bankroll_is_skipped_but_fails_closed(
        self,
    ) -> None:
        # With NO bankroll the % caps fail closed (SKIP_BANKROLL_UNAVAILABLE); the
        # backstop needs a bankroll multiple, so it cannot compute either → the
        # single fail-closed breach stands in for the whole layer.
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=100))
        breaches = checker(**LOOSE).check(
            book, MARG, DailyPnl(), risk_bankroll_cc=None
        )
        assert r2_reasons(breaches) == [ReasonCode.SKIP_BANKROLL_UNAVAILABLE]


class TestDirectionalCap:
    def test_fires_when_net_directional_exposure_exceeds_cap(self) -> None:
        # 100.00 YES contracts on leg A at p=0.5 → delta_by_game[GAME1] = +100
        # (independence delta = contracts for a single-leg position). Directional
        # loss-equiv = 100 * $1 = 1_000_000cc. directional 10% of $2,000 =
        # 2_000_000cc, so make the bankroll small to trip: 10% of $9,000,000cc
        # ($900) = 900_000cc < 1_000_000cc.
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=100))
        breaches = checker(directional_frac=Fraction(10, 100), **{
            k: v for k, v in LOOSE.items() if k != "directional_frac"
        }).check(book, MARG, DailyPnl(), risk_bankroll_cc=9_000_000)
        assert ReasonCode.SKIP_DIRECTIONAL_CAP in r2_reasons(breaches)

    def test_does_not_fire_under_cap(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=100))
        # directional loss-equiv 1_000_000cc; 10% of $2,000 = 2_000_000cc > it.
        breaches = checker(directional_frac=Fraction(10, 100), **{
            k: v for k, v in LOOSE.items() if k != "directional_frac"
        }).check(book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL_2K)
        assert ReasonCode.SKIP_DIRECTIONAL_CAP not in r2_reasons(breaches)


class TestDailyLossCapPct:
    def test_fires_at_6pct_inclusive(self) -> None:
        # 6% of $2,000 = 1_200_000cc. total_cc = -1_200_000 → -total >= thr fires.
        breaches = checker(daily_loss_frac=Fraction(6, 100), **{
            k: v for k, v in LOOSE.items() if k != "daily_loss_frac"
        }).check(
            empty_book(), MARG, DailyPnl(realized_cc=-1_200_000),
            risk_bankroll_cc=BANKROLL_2K,
        )
        assert ReasonCode.HALT_DAILY_LOSS in r2_reasons(breaches)

    def test_just_under_does_not_fire(self) -> None:
        breaches = checker(daily_loss_frac=Fraction(6, 100), **{
            k: v for k, v in LOOSE.items() if k != "daily_loss_frac"
        }).check(
            empty_book(), MARG, DailyPnl(realized_cc=-1_199_999),
            risk_bankroll_cc=BANKROLL_2K,
        )
        assert ReasonCode.HALT_DAILY_LOSS not in r2_reasons(breaches)


class TestGiveBackHalts:
    def test_drawdown_fires_at_10pct(self) -> None:
        # give-back = peak - current = 2_000_000cc = 10% of $2,000.
        halt = HaltInputs(peak_equity_cc=20_000_000, current_equity_cc=18_000_000)
        breaches = self._run(
            halt, drawdown_frac=Fraction(10, 100), hard_trip_frac=Fraction(12, 100)
        )
        assert ReasonCode.HALT_DRAWDOWN in r2_reasons(breaches)
        # 10% give-back is below the 12% hard-trip → hard-trip NOT fired.
        assert ReasonCode.HALT_HARD_TRIP not in r2_reasons(breaches)

    def test_hard_trip_fires_at_12pct_and_also_drawdown(self) -> None:
        # give-back = 2_400_000cc = 12% → BOTH hard-trip and drawdown fire.
        halt = HaltInputs(peak_equity_cc=20_000_000, current_equity_cc=17_600_000)
        breaches = self._run(
            halt, drawdown_frac=Fraction(10, 100), hard_trip_frac=Fraction(12, 100)
        )
        rs = r2_reasons(breaches)
        assert ReasonCode.HALT_HARD_TRIP in rs
        assert ReasonCode.HALT_DRAWDOWN in rs

    def test_no_peak_no_halt(self) -> None:
        # Missing equity inputs ⇒ the give-back cannot be computed ⇒ no halt (we
        # never invent a give-back). Only the fail-closed-free clean layer remains.
        breaches = self._run(HaltInputs(), drawdown_frac=Fraction(1, 100))
        assert ReasonCode.HALT_DRAWDOWN not in r2_reasons(breaches)
        assert ReasonCode.HALT_HARD_TRIP not in r2_reasons(breaches)

    def _run(self, halt: HaltInputs, **fracs: object) -> list[Breach]:
        overrides = dict(LOOSE)
        overrides.update(fracs)
        return checker(**overrides).check(
            empty_book(), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K, halt_inputs=halt,
        )


class TestFailClosed:
    def test_none_bankroll_emits_single_fail_closed_shadow_breach(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=5_000))
        breaches = checker().check(book, MARG, DailyPnl(), risk_bankroll_cc=None)
        assert r2_reasons(breaches) == [ReasonCode.SKIP_BANKROLL_UNAVAILABLE]
        assert all(b.shadow for b in r2(breaches))

    def test_zero_bankroll_fails_closed(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=5_000))
        breaches = checker().check(book, MARG, DailyPnl(), risk_bankroll_cc=0)
        assert r2_reasons(breaches) == [ReasonCode.SKIP_BANKROLL_UNAVAILABLE]

    def test_negative_bankroll_fails_closed(self) -> None:
        breaches = checker().check(empty_book(), MARG, DailyPnl(), risk_bankroll_cc=-5)
        assert r2_reasons(breaches) == [ReasonCode.SKIP_BANKROLL_UNAVAILABLE]

    def test_fail_closed_is_shadow_in_phase2(self) -> None:
        # Default caps_shadow_mode=True ⇒ the fail-closed breach is log-only.
        breaches = checker().check(empty_book(), MARG, DailyPnl(), risk_bankroll_cc=None)
        fc = next(b for b in breaches if b.reason is ReasonCode.SKIP_BANKROLL_UNAVAILABLE)
        assert fc.shadow is True

    def test_fail_closed_is_enforced_when_shadow_off(self) -> None:
        # Operator flips to enforce: the fail-closed breach becomes real.
        breaches = checker(caps_shadow_mode=False).check(
            empty_book(), MARG, DailyPnl(), risk_bankroll_cc=None
        )
        fc = next(b for b in breaches if b.reason is ReasonCode.SKIP_BANKROLL_UNAVAILABLE)
        assert fc.shadow is False


class TestSlateKey:
    def test_et_calendar_day_bucket(self) -> None:
        # 2026-07-13 23:00 UTC = 19:00 ET → ET day 2026-07-13.
        start = datetime(2026, 7, 13, 23, 0, tzinfo=UTC)
        assert slate_key_for_start(start) == "2026-07-13"

    def test_et_day_rolls_back_across_utc_midnight(self) -> None:
        # 2026-07-14 02:00 UTC = 2026-07-13 22:00 ET → still the 13th's slate.
        start = datetime(2026, 7, 14, 2, 0, tzinfo=UTC)
        assert slate_key_for_start(start) == "2026-07-13"

    def test_none_start_is_unknown_pool(self) -> None:
        assert slate_key_for_start(None) == UNKNOWN_SLATE_KEY


class TestSlateCap:
    def _two_game_book(self, each_loss_cc: int) -> ExposureBook:
        # Two positions, GAME1 and GAME2, each max_loss == each_loss_cc.
        book = empty_book()
        entry = each_loss_cc // 100
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=entry))
        book.add_position(make_position("p2", LEG_B, contracts=10_000, entry_price=entry))
        return book

    def _same_day_provider(self) -> Callable[[str], datetime | None]:
        # Both games start the SAME ET evening → one slate; game cap is looser
        # than the slate SUM so only the slate cap fires.
        starts = {
            "A": datetime(2026, 7, 13, 23, 0, tzinfo=UTC),  # 19:00 ET
            "B": datetime(2026, 7, 13, 23, 30, tzinfo=UTC),  # 19:30 ET, same day
        }
        return lambda t: starts.get(t)

    def test_slate_sum_fires_when_two_games_pool(self) -> None:
        # Each game loss = 900_000cc (below the 8% game cap 1_600_000cc), but the
        # slate SUM = 1_800_000cc > slate cap 1_600_000cc → slate cap fires,
        # game cap does NOT.
        book = self._two_game_book(900_000)
        breaches = checker(
            game_loss_frac=Fraction(8, 100),
            slate_loss_frac=Fraction(8, 100),
            **{k: v for k, v in LOOSE.items()
               if k not in ("game_loss_frac", "slate_loss_frac")},
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            start_time_provider=self._same_day_provider(),
        )
        rs = r2_reasons(breaches)
        assert ReasonCode.SKIP_SLATE_CAP in rs
        assert ReasonCode.SKIP_GAME_LOSS_CAP not in rs
        breach = next(b for b in r2(breaches) if b.reason is ReasonCode.SKIP_SLATE_CAP)
        assert "2026-07-13" in breach.detail

    def test_two_different_slates_do_not_pool(self) -> None:
        # Same losses but games on DIFFERENT ET days → each slate = 900_000cc <
        # the slate cap → no slate breach.
        book = self._two_game_book(900_000)
        starts = {
            "A": datetime(2026, 7, 13, 23, 0, tzinfo=UTC),   # 19:00 ET Jul 13
            "B": datetime(2026, 7, 15, 23, 0, tzinfo=UTC),   # 19:00 ET Jul 15
        }
        breaches = checker(
            game_loss_frac=Fraction(8, 100),
            slate_loss_frac=Fraction(8, 100),
            **{k: v for k, v in LOOSE.items()
               if k not in ("game_loss_frac", "slate_loss_frac")},
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            start_time_provider=lambda t: starts.get(t),
        )
        assert ReasonCode.SKIP_SLATE_CAP not in r2_reasons(breaches)

    def test_unknown_start_games_pool_into_capped_unknown_bucket(self) -> None:
        # No start_time_provider → every game pools into the UNKNOWN slate, which
        # is itself capped: two 900_000cc games sum to 1_800_000cc > 1_600_000cc.
        book = self._two_game_book(900_000)
        breaches = checker(
            game_loss_frac=Fraction(99, 100),  # game cap loose
            slate_loss_frac=Fraction(8, 100),
            **{k: v for k, v in LOOSE.items()
               if k not in ("game_loss_frac", "slate_loss_frac")},
        ).check(book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL_2K)
        breach = next(
            (b for b in r2(breaches) if b.reason is ReasonCode.SKIP_SLATE_CAP), None
        )
        assert breach is not None
        assert UNKNOWN_SLATE_KEY in breach.detail

    def test_partial_unknown_pools_the_unknown_game_separately(self) -> None:
        # GAME1 has a known ET-day start; GAME2's start is None → GAME2 pools into
        # UNKNOWN. Each bucket holds one 900_000cc game < slate cap → no breach,
        # proving the UNKNOWN game does NOT contaminate the known slate.
        book = self._two_game_book(900_000)
        starts = {"A": datetime(2026, 7, 13, 23, 0, tzinfo=UTC)}  # B → None
        breaches = checker(
            game_loss_frac=Fraction(99, 100),
            slate_loss_frac=Fraction(8, 100),
            **{k: v for k, v in LOOSE.items()
               if k not in ("game_loss_frac", "slate_loss_frac")},
        ).check(
            book, MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            start_time_provider=lambda t: starts.get(t),
        )
        assert ReasonCode.SKIP_SLATE_CAP not in r2_reasons(breaches)

    def test_slate_rollup_includes_candidate_and_open_quote_games(self) -> None:
        # The slate cap must see BOTH a candidate fill's game and an open quote's
        # game (both fold into worst_case_loss_by_game under mass acceptance).
        book = empty_book()
        # Open quote on GAME1 worth ~$90 worst-case (1000/100 ct * $0.90).
        book.upsert_quote(
            make_quote("q1", LEG_A, yes_bid=9_000, no_bid=9_000, contracts=100_000)
        )
        # Candidate on GAME2 worth $90 loss.
        cand = make_position("cand", LEG_B, contracts=10_000, entry_price=9_000)
        starts = {
            "A": datetime(2026, 7, 13, 23, 0, tzinfo=UTC),
            "B": datetime(2026, 7, 13, 23, 30, tzinfo=UTC),
        }
        breaches = checker(
            game_loss_frac=Fraction(99, 100),
            slate_loss_frac=Fraction(8, 100),
            per_combo_loss_frac=Fraction(99, 100),
            **{k: v for k, v in LOOSE.items()
               if k not in ("game_loss_frac", "slate_loss_frac", "per_combo_loss_frac")},
        ).check(
            book, MARG, DailyPnl(),
            candidate_positions=[cand],
            risk_bankroll_cc=BANKROLL_2K,
            start_time_provider=lambda t: starts.get(t),
        )
        # $90 + $90 = $180 = 1_800_000cc > 1_600_000cc slate cap, both on the
        # SAME ET day → one slate breach.
        breach = next(
            (b for b in r2(breaches) if b.reason is ReasonCode.SKIP_SLATE_CAP), None
        )
        assert breach is not None
        assert "2026-07-13" in breach.detail


class TestStarvationWatchdog:
    def test_fires_after_threshold_consecutive_declines(self) -> None:
        wd = StarvationWatchdog(threshold=3)
        assert wd.record_risk_decline() is False
        assert wd.record_risk_decline() is False
        # Third decline crosses the threshold → returns True exactly once.
        assert wd.record_risk_decline() is True
        assert wd.starved is True
        assert wd.consecutive_declines == 3
        # Further declines do not re-fire the warning.
        assert wd.record_risk_decline() is False

    def test_quote_issued_resets(self) -> None:
        wd = StarvationWatchdog(threshold=2)
        wd.record_risk_decline()
        wd.record_quote_issued()
        assert wd.consecutive_declines == 0
        assert wd.starved is False
        # After reset it takes the full threshold again to warn.
        assert wd.record_risk_decline() is False
        assert wd.record_risk_decline() is True

    def test_threshold_must_be_positive(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="threshold"):
            StarvationWatchdog(threshold=0)


class TestShadowMarking:
    def test_all_r2_breaches_are_shadow_by_default(self) -> None:
        # Trip several R2 caps at once; every one must carry shadow=True.
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=5_000))
        breaches = checker(
            game_loss_frac=Fraction(1, 100),
            slate_loss_frac=Fraction(1, 100),
        ).check(
            book, MARG, DailyPnl(realized_cc=-5_000_000), risk_bankroll_cc=BANKROLL_2K
        )
        r2b = r2(breaches)
        assert r2b, "expected several R2 breaches"
        assert all(b.shadow for b in r2b)

    def test_r2_breaches_are_enforced_when_shadow_off(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=10_000, entry_price=5_000))
        breaches = checker(
            caps_shadow_mode=False,
            game_loss_frac=Fraction(1, 100),
        ).check(book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL_2K)
        game = next(b for b in breaches if b.reason is ReasonCode.SKIP_GAME_LOSS_CAP)
        assert game.shadow is False


class TestConfigFractionExactness:
    def test_config_parses_decimal_strings_to_exact_fractions(self) -> None:
        from combomaker.ops.config import RiskConfig

        rl = RiskConfig().to_risk_limits()
        # "0.08" → EXACTLY 8/100 = 2/25, never a binary-float approximation.
        assert rl.game_loss_frac == Fraction(8, 100)
        assert rl.per_combo_loss_frac == Fraction(1, 100)
        assert rl.directional_frac == Fraction(10, 100)
        assert rl.slate_loss_frac == Fraction(8, 100)
        assert rl.daily_loss_frac == Fraction(6, 100)
        assert rl.drawdown_frac == Fraction(10, 100)
        assert rl.hard_trip_frac == Fraction(12, 100)
        assert rl.absolute_notional_multiple == 3
        assert rl.caps_shadow_mode is True  # SHADOW by default in Phase 2

    def test_cap_fraction_out_of_range_rejected(self) -> None:
        # A percentage is a FRACTION of bankroll: "8" (typo for 8%) would parse
        # to Fraction(8) = 800% of bankroll and silently disable the cap; a
        # negative would breach everything. Both must be rejected at load.
        from pydantic import ValidationError

        from combomaker.ops.config import RiskConfig

        # "NaN"/"Infinity" parse as Decimals but are not finite fractions — they
        # must raise a clean ValidationError (a NaN range-compare would otherwise
        # raise an opaque decimal.InvalidOperation). Still fails closed either way.
        for bad in ("8", "1.5", "0", "-0.08", "NaN", "Infinity", "-Infinity"):
            with pytest.raises(ValidationError):
                RiskConfig(game_loss_frac=bad)
        # "1" (= 100% of bankroll) is the inclusive upper bound and is allowed.
        assert RiskConfig(game_loss_frac="1").to_risk_limits().game_loss_frac == Fraction(1)

    def test_cap_fraction_non_decimal_rejected(self) -> None:
        from pydantic import ValidationError

        from combomaker.ops.config import RiskConfig

        with pytest.raises(ValidationError):
            RiskConfig(hard_trip_frac="abc")

    def test_positive_int_knobs_rejected_below_one(self) -> None:
        from pydantic import ValidationError

        from combomaker.ops.config import RiskConfig

        for field in (
            "absolute_notional_multiple",
            "fill_velocity_max_fills",
            "starvation_threshold",
        ):
            with pytest.raises(ValidationError):
                RiskConfig(**{field: 0})


class TestPortfolioCvarCap:
    """The Phase-4 portfolio joint-tail cap: operative ES_0.99 vs %-of-bankroll.

    Reads the latest BookRiskSnapshot (a FakeBookRisk here); NEVER re-runs MC.
    Fires when operative ES exceeds the ceiling; fails closed on an unusable
    snapshot; not evaluated when no snapshot is supplied (None)."""

    def _check(
        self, book_risk: object | None, frac: Fraction
    ) -> list[ReasonCode]:
        # Isolate the CVaR cap: all other R2 fracs loose, this one under test.
        limits = {**LOOSE, "portfolio_cvar_frac": frac}
        breaches = LimitChecker(RiskLimits(**limits)).check(  # type: ignore[arg-type]
            empty_book(),
            MARG,
            DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            book_risk=book_risk,  # type: ignore[arg-type]
        )
        return r2_reasons(breaches)

    def test_fires_when_operative_es_over_ceiling(self) -> None:
        # 15% of $2,000 = $300 = 3_000_000 cc. ES 3_000_001 → fires.
        thr = threshold_cc(Fraction(15, 100), BANKROLL_2K)
        risk = FakeBookRisk(usable=True, operative_es_99_cc=float(thr + 1))
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in self._check(risk, Fraction(15, 100))

    def test_passes_at_or_below_ceiling(self) -> None:
        thr = threshold_cc(Fraction(15, 100), BANKROLL_2K)
        risk = FakeBookRisk(usable=True, operative_es_99_cc=float(thr))
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in self._check(risk, Fraction(15, 100))

    def test_unusable_snapshot_fails_closed(self) -> None:
        # An UNKNOWN/empty snapshot ⇒ breach regardless of the ES value.
        risk = FakeBookRisk(usable=False, operative_es_99_cc=0.0)
        assert ReasonCode.SKIP_PORTFOLIO_CVAR in self._check(risk, Fraction(15, 100))

    def test_no_snapshot_not_evaluated(self) -> None:
        # None (no MC yet) ⇒ the cap simply doesn't run; no CVaR breach.
        assert ReasonCode.SKIP_PORTFOLIO_CVAR not in self._check(None, Fraction(15, 100))

    def test_shadow_flag_set_in_shadow_mode(self) -> None:
        # In caps_shadow_mode (default True), the CVaR breach is log-only.
        thr = threshold_cc(Fraction(15, 100), BANKROLL_2K)
        risk = FakeBookRisk(usable=True, operative_es_99_cc=float(thr + 1))
        limits = RiskLimits(**{**LOOSE, "portfolio_cvar_frac": Fraction(15, 100)})  # type: ignore[arg-type]
        breaches = LimitChecker(limits).check(
            empty_book(),
            MARG,
            DailyPnl(),
            risk_bankroll_cc=BANKROLL_2K,
            book_risk=risk,
        )
        cvar = [b for b in breaches if b.reason is ReasonCode.SKIP_PORTFOLIO_CVAR]
        assert cvar and all(b.shadow for b in cvar)

    def test_config_wires_portfolio_cvar_frac(self) -> None:
        from combomaker.ops.config import RiskConfig

        limits = RiskConfig(portfolio_cvar_frac="0.20").to_risk_limits()
        assert limits.portfolio_cvar_frac == Fraction(20, 100)

    def test_config_rejects_bad_cvar_frac(self) -> None:
        from pydantic import ValidationError

        from combomaker.ops.config import RiskConfig

        with pytest.raises(ValidationError):
            RiskConfig(portfolio_cvar_frac="1.5")
