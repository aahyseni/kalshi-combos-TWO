"""AUTO-SCALING DELTA CAPS (operator directive 2026-07-19).

The directional delta caps (``max_market_delta_contracts`` /
``max_event_delta_contracts``) were the last ABSOLUTE numbers in the risk
stack. When the new ``max_market_delta_frac`` / ``max_event_delta_frac`` knobs
are armed, each cap's CONTRACT threshold derives per check from the SAME live
risk bankroll the loss budgets scale from:

    cap_contracts = threshold_cc(frac, bankroll_cc) / 10_000
                  = frac x bankroll-in-dollars   (1 contract ~ $1 max payout)

Covered here: (1) a frac-armed cap MOVES with the bankroll basis (exact
arithmetic at two bankrolls); (2) frac unset => byte-identical decisions and
detail strings vs the pre-existing absolute behaviour at the old hand-set
values; (3) frac set + absolute set => frac wins, and the ``delta_cap_mode``
startup log fires from the config->limits seam; (4) the config validator
rejects negative/zero/garbage/non-finite/absurd fracs while accepting the
suggested arming values ("0.80"/"1.30" — note the event frac legitimately
exceeds 1); (5) the SKIP_DIRECTIONAL_CAP reason + waiver coverage are
unchanged when the frac-derived delta cap binds: the directional breach still
carries its game key and is waived by a valid certificate, while the delta
breach stays game=None (never waivable) and survives the waiver.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

import pytest
import structlog
from pydantic import ValidationError

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import RiskConfig
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.limits import (
    Breach,
    DailyPnl,
    LimitChecker,
    RiskLimits,
    scaled_delta_cap_contracts,
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

MARGINALS = {"A": 0.5, "B": 0.5}


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


MARG = provider(MARGINALS)

LEG_A = (LegRef("A", "EV1", "yes"),)

# Bankroll bases in centi-cents ($1 = 10_000 cc).
BANKROLL_1910 = 19_100_000   # $1,910 — the operator's cash at design time
BANKROLL_1000 = 10_000_000   # $1,000
BANKROLL_500 = 5_000_000     # $500

# The suggested arming fracs: "0.80" -> 4/5, "1.30" -> 13/10.
MARKET_FRAC = Fraction(4, 5)
EVENT_FRAC = Fraction(13, 10)

# Loose settings for every non-delta cap so a bankroll-armed check isolates the
# delta family (RiskLimits is unvalidated — huge Fractions are legal here).
LOOSE_R2: dict[str, object] = {
    "game_loss_frac": Fraction(999),
    "per_combo_loss_frac": Fraction(999),
    "directional_frac": Fraction(999),
    "slate_loss_frac": Fraction(999),
    "daily_loss_frac": Fraction(999),
    "drawdown_frac": Fraction(999),
    "hard_trip_frac": Fraction(999),
    "portfolio_cvar_frac": Fraction(999),
    "portfolio_det_max_frac": Fraction(999),
    "absolute_notional_multiple": 999,
}


def make_position(
    pid: str,
    legs: tuple[LegRef, ...] = LEG_A,
    *,
    contracts: int = 100,
    entry_price: int = 100,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.YES,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


def book_with_delta(contracts_whole: int) -> ExposureBook:
    """One YES position on market A (game EV1) whose market AND game delta is
    exactly ``contracts_whole`` contracts (single leg at p=0.5, cheap entry so
    no loss-axis cap interferes)."""
    book = ExposureBook(CONVENTIONS)
    book.add_position(
        make_position("p1", contracts=contracts_whole * 100, entry_price=100)
    )
    return book


def delta_breaches(breaches: list[Breach]) -> list[Breach]:
    """The delta-family breaches only (market/game directional contract caps)."""
    return [
        b
        for b in breaches
        if b.reason is ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH
        and " delta " in b.detail
    ]


@dataclass(frozen=True)
class Cert:
    """Minimal WaiverCertificate stand-in (structural protocol)."""

    worst_case_cc: int
    certified: bool = True


# ---------------------------------------------------------------------------
# (1) frac-armed: the cap MOVES with the bankroll basis, exact arithmetic
# ---------------------------------------------------------------------------


class TestFracArmedScalesWithBankroll:
    def _checker(self) -> LimitChecker:
        return LimitChecker(
            RiskLimits(
                max_market_delta_contracts=1_500.0,   # ignored: frac wins
                max_event_delta_contracts=2_500.0,    # ignored: frac wins
                max_market_delta_frac=MARKET_FRAC,
                max_event_delta_frac=EVENT_FRAC,
                **LOOSE_R2,  # type: ignore[arg-type]
            )
        )

    def test_derivation_formula_exact(self) -> None:
        # cap = threshold_cc(frac, bankroll) / 10_000, integer-exact in cc.
        assert threshold_cc(MARKET_FRAC, BANKROLL_1910) == 15_280_000
        cap, note = scaled_delta_cap_contracts(MARKET_FRAC, 1_500.0, BANKROLL_1910)
        assert cap == 1_528.0
        assert note == " (4/5 x bankroll 19100000cc)"
        cap2, _ = scaled_delta_cap_contracts(EVENT_FRAC, 2_500.0, BANKROLL_1910)
        assert cap2 == 2_483.0  # 13*19_100_000//10 = 24_830_000 cc

    def test_same_book_passes_at_large_bankroll(self) -> None:
        # Delta 1000 vs market cap 0.80 x $1,910 = 1528.0 / event cap 2483.0.
        breaches = self._checker().check(
            book_with_delta(1_000), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_1910,
        )
        assert delta_breaches(breaches) == []

    def test_same_book_breaches_market_cap_at_smaller_bankroll(self) -> None:
        # Same book, bankroll $1,000: market cap 800.0 < 1000; event 1300.0 ok.
        breaches = delta_breaches(
            self._checker().check(
                book_with_delta(1_000), MARG, DailyPnl(),
                risk_bankroll_cc=BANKROLL_1000,
            )
        )
        assert len(breaches) == 1
        assert breaches[0].detail == (
            "market A delta 1000.0 > 800.0 (4/5 x bankroll 10000000cc)"
        )
        assert breaches[0].shadow is False
        assert breaches[0].game is None

    def test_event_cap_binds_too_at_even_smaller_bankroll(self) -> None:
        # Bankroll $500: market cap 400.0 AND event cap 650.0 both breached.
        breaches = delta_breaches(
            self._checker().check(
                book_with_delta(1_000), MARG, DailyPnl(),
                risk_bankroll_cc=BANKROLL_500,
            )
        )
        assert [b.detail for b in breaches] == [
            "market A delta 1000.0 > 400.0 (4/5 x bankroll 5000000cc)",
            "game EV1 delta 1000.0 > 650.0 (13/10 x bankroll 5000000cc)",
        ]

    def test_frac_armed_but_bankroll_unavailable_falls_back_to_absolute(
        self,
    ) -> None:
        # No usable bankroll => the absolute knob stands in (1000 < 1500 /
        # 2500: no delta breach) and the R2 layer fails closed on the missing
        # denominator exactly as before (SKIP_BANKROLL_UNAVAILABLE blocks).
        for bankroll in (None, 0, -1):
            breaches = self._checker().check(
                book_with_delta(1_000), MARG, DailyPnl(),
                risk_bankroll_cc=bankroll,
            )
            assert delta_breaches(breaches) == []
            assert [b.reason for b in breaches] == [
                ReasonCode.SKIP_BANKROLL_UNAVAILABLE
            ]


# ---------------------------------------------------------------------------
# (2) frac unset (None default): byte-identical old absolute behaviour
# ---------------------------------------------------------------------------


class TestFracUnsetByteIdenticalAbsolute:
    LIMITS = RiskLimits(
        max_market_delta_contracts=1_500.0,  # the operator's last hand-set values
        max_event_delta_contracts=2_500.0,
    )

    def test_default_fracs_are_none(self) -> None:
        assert RiskLimits().max_market_delta_frac is None
        assert RiskLimits().max_event_delta_frac is None

    def test_boundary_at_old_absolute_no_breach(self) -> None:
        # Exactly AT the cap: strict > does not fire (pre-existing semantics).
        breaches = LimitChecker(self.LIMITS).check(
            book_with_delta(1_500), MARG, DailyPnl(),
            risk_bankroll_cc=None, bankroll_source_configured=False,
        )
        assert delta_breaches(breaches) == []

    def test_just_over_old_absolute_breaches_with_identical_detail(self) -> None:
        # 1500.01 contracts (one centi-contract over): the breach fires and the
        # detail string is byte-identical to the pre-change formatting —
        # ``f"market {t} delta {d:.1f} > {max_market_delta_contracts}"``.
        book = ExposureBook(CONVENTIONS)
        book.add_position(make_position("p1", contracts=150_001, entry_price=100))
        breaches = delta_breaches(
            LimitChecker(self.LIMITS).check(
                book, MARG, DailyPnl(),
                risk_bankroll_cc=None, bankroll_source_configured=False,
            )
        )
        assert len(breaches) == 1
        assert breaches[0].detail == "market A delta 1500.0 > 1500.0"
        assert breaches[0].shadow is False
        assert breaches[0].game is None

    def test_absolute_mode_ignores_the_bankroll_entirely(self) -> None:
        # With fracs unset, a present bankroll must not move the delta caps:
        # the delta-family decisions are identical with and without one.
        limits = RiskLimits(
            max_market_delta_contracts=1_500.0,
            max_event_delta_contracts=2_500.0,
            **LOOSE_R2,  # type: ignore[arg-type]
        )
        book = ExposureBook(CONVENTIONS)
        book.add_position(make_position("p1", contracts=150_001, entry_price=100))
        with_bankroll = delta_breaches(
            LimitChecker(limits).check(
                book, MARG, DailyPnl(), risk_bankroll_cc=BANKROLL_500
            )
        )
        without = delta_breaches(
            LimitChecker(limits).check(
                book, MARG, DailyPnl(),
                risk_bankroll_cc=None, bankroll_source_configured=False,
            )
        )
        assert [(b.reason, b.detail, b.shadow, b.game) for b in with_bankroll] == [
            (b.reason, b.detail, b.shadow, b.game) for b in without
        ]
        assert [b.detail for b in with_bankroll] == [
            "market A delta 1500.0 > 1500.0"
        ]


# ---------------------------------------------------------------------------
# (3) frac set + absolute set: frac wins; the startup mode log fires
# ---------------------------------------------------------------------------


class TestFracWinsOverAbsolute:
    def test_tight_absolute_is_ignored_when_frac_armed(self) -> None:
        # Absolute 1.0 contract would breach instantly; the armed frac derives
        # 1528.0 from the live bankroll and admits the 1000-contract delta.
        limits = RiskLimits(
            max_market_delta_contracts=1.0,
            max_event_delta_contracts=1.0,
            max_market_delta_frac=MARKET_FRAC,
            max_event_delta_frac=EVENT_FRAC,
            **LOOSE_R2,  # type: ignore[arg-type]
        )
        breaches = LimitChecker(limits).check(
            book_with_delta(1_000), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_1910,
        )
        assert delta_breaches(breaches) == []

    def test_loose_absolute_is_ignored_when_frac_binds(self) -> None:
        # Absolute 99_999 would admit anything; the armed frac (1/20 of the
        # bankroll) breaches — frac wins in BOTH directions.
        limits = RiskLimits(
            max_market_delta_contracts=99_999.0,
            max_event_delta_contracts=99_999.0,
            max_market_delta_frac=Fraction(1, 20),
            **LOOSE_R2,  # type: ignore[arg-type]
        )
        breaches = delta_breaches(
            LimitChecker(limits).check(
                book_with_delta(1_000), MARG, DailyPnl(),
                risk_bankroll_cc=BANKROLL_1000,
            )
        )
        assert [b.detail for b in breaches] == [
            "market A delta 1000.0 > 50.0 (1/20 x bankroll 10000000cc)"
        ]

    def test_startup_mode_log_fires_with_frac_mode(self) -> None:
        cfg = RiskConfig(max_market_delta_frac="0.80", max_event_delta_frac="1.30")
        with structlog.testing.capture_logs() as cap:
            limits = cfg.to_risk_limits()
        recs = [e for e in cap if e["event"] == "delta_cap_mode"]
        assert len(recs) == 1
        assert recs[0]["market_mode"] == "frac"
        assert recs[0]["market_value"] == "0.80"
        assert recs[0]["event_mode"] == "frac"
        assert recs[0]["event_value"] == "1.30"
        # Exact Fractions land in RiskLimits ("0.80" is EXACTLY 4/5, no float).
        assert limits.max_market_delta_frac == MARKET_FRAC
        assert limits.max_event_delta_frac == EVENT_FRAC

    def test_startup_mode_log_reports_absolute_when_unarmed(self) -> None:
        with structlog.testing.capture_logs() as cap:
            limits = RiskConfig().to_risk_limits()
        recs = [e for e in cap if e["event"] == "delta_cap_mode"]
        assert len(recs) == 1
        assert recs[0]["market_mode"] == "absolute"
        assert recs[0]["market_value"] == 300.0
        assert recs[0]["event_mode"] == "absolute"
        assert recs[0]["event_value"] == 500.0
        assert limits.max_market_delta_frac is None
        assert limits.max_event_delta_frac is None


# ---------------------------------------------------------------------------
# (4) config validator: negative/garbage rejected, arming values accepted
# ---------------------------------------------------------------------------


class TestDeltaFracValidator:
    @pytest.mark.parametrize(
        "bad", ["-0.5", "0", "garbage", "NaN", "Infinity", "-Infinity", "11", ""]
    )
    def test_rejects_bad_fracs_on_both_knobs(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            RiskConfig(max_market_delta_frac=bad)
        with pytest.raises(ValidationError):
            RiskConfig(max_event_delta_frac=bad)

    def test_accepts_arming_values_and_none(self) -> None:
        cfg = RiskConfig(max_market_delta_frac="0.80", max_event_delta_frac="1.30")
        assert cfg.max_market_delta_frac == "0.80"
        # The event frac legitimately EXCEEDS 1 (1.30 x bankroll) — the delta
        # axis is a contract bound, not premium at risk; the loss-frac (0, 1]
        # bound deliberately does not apply.
        assert cfg.max_event_delta_frac == "1.30"
        assert RiskConfig().max_market_delta_frac is None
        assert RiskConfig().max_event_delta_frac is None


# ---------------------------------------------------------------------------
# (5) SKIP_DIRECTIONAL_CAP + waiver coverage unchanged when the cap binds
# ---------------------------------------------------------------------------


class TestDirectionalAndWaiverUnchanged:
    def _limits(self) -> RiskLimits:
        # Frac-armed EVENT delta cap that BINDS (1/20 x $1,000 = 50 contracts)
        # alongside a binding directional cap (10% of bankroll); every other
        # cap loose. Enforced mode (shadow off) — the live posture.
        overrides = dict(LOOSE_R2)
        overrides["directional_frac"] = Fraction(1, 10)
        return RiskLimits(
            caps_shadow_mode=False,
            max_market_delta_contracts=99_999.0,
            max_event_delta_contracts=99_999.0,
            max_event_delta_frac=Fraction(1, 20),
            **overrides,  # type: ignore[arg-type]
        )

    def test_directional_reason_and_game_key_unchanged(self) -> None:
        # Delta 1000 on EV1: directional 10_000_000cc > 1_000_000cc threshold
        # AND the frac-derived event delta cap (50.0) binds. The directional
        # breach keeps its reason + game key; the delta breach stays game=None.
        breaches = LimitChecker(self._limits()).check(
            book_with_delta(1_000), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_1000,
        )
        directional = [
            b for b in breaches if b.reason is ReasonCode.SKIP_DIRECTIONAL_CAP
        ]
        assert len(directional) == 1
        assert directional[0].game == "EV1"
        assert directional[0].shadow is False
        deltas = delta_breaches(breaches)
        assert [b.detail for b in deltas] == [
            "game EV1 delta 1000.0 > 50.0 (1/20 x bankroll 10000000cc)"
        ]
        assert deltas[0].game is None  # delta family: never waivable

    def test_waiver_still_covers_directional_but_never_the_delta_cap(self) -> None:
        # A valid certificate for EV1 waives the directional breach exactly as
        # before; the frac-derived delta breach SURVIVES the waiver unchanged
        # (waiver coverage of the delta axis: none, same as pre-change).
        waived = {"EV1": Cert(worst_case_cc=0, certified=True)}
        breaches = LimitChecker(self._limits()).check(
            book_with_delta(1_000), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_1000,
            waived_games=waived,
        )
        assert [
            b for b in breaches if b.reason is ReasonCode.SKIP_DIRECTIONAL_CAP
        ] == []
        deltas = delta_breaches(breaches)
        assert [b.detail for b in deltas] == [
            "game EV1 delta 1000.0 > 50.0 (1/20 x bankroll 10000000cc)"
        ]
        assert deltas[0].game is None

    def test_uncertified_waiver_does_not_cover_directional(self) -> None:
        # Fail-closed regression: an UNCERTIFIED certificate waives nothing.
        waived = {"EV1": Cert(worst_case_cc=0, certified=False)}
        breaches = LimitChecker(self._limits()).check(
            book_with_delta(1_000), MARG, DailyPnl(),
            risk_bankroll_cc=BANKROLL_1000,
            waived_games=waived,
        )
        assert [
            b.reason for b in breaches if b.reason is ReasonCode.SKIP_DIRECTIONAL_CAP
        ] == [ReasonCode.SKIP_DIRECTIONAL_CAP]
