"""CONFIRM-PATH LAST-LOOK MC WAIVER (handoff Problem A) — wiring tests.

The waiver lifts EXACTLY the two comonotone-overstated analytic per-game bounds
(game-loss + mutex-directional) at the confirm-path reservation denial, by exact
scoreline enumeration (sim/state_worst_case.py — its own 35 semantics tests),
mirroring the P0-2 candidate-gate atomicity pattern. Covered here:

- disabled (committed default) ⇒ byte-identical prior decline (no waiver code
  runs, no deferral, the last-look risk_breaches decline stands verbatim);
- a pure game-loss denial waived when certified under the SAME budget (real
  enumeration, real reservation retry through the real LimitChecker);
- a pure directional denial waived under the game-loss budget;
- NEVER waives when any other breach is present (parametrized over the gross /
  per-combo / daily / CVaR / det-max / ruin / notional / slate / size caps);
- over-budget state-consistent worst case ⇒ decline;
- uncertified game ⇒ decline (full on_quote_accepted round trip, real module);
- reservation version bump during the off-loop enumeration ⇒ exactly ONE
  rebuild, then fail-closed decline (and: a single bump ⇒ rebuild succeeds);
- 2026-07-16 adversarial-review findings 1+3: a QUOTE upsert/removal during the
  off-loop enumeration (which bumps ONLY the full book generation, never the
  position generation) invalidates the certificate — the ONE rebuild re-prices
  the churned-in quote (over budget ⇒ decline), and churn on every attempt
  fails closed;
- finding 2: an outstanding NON-CANDIDATE reservation's miss-side hedge credit
  can NOT certify the candidate (a later release would leave the committed
  book over the certified budget) — while the SAME hedge held as a COMMITTED
  position still nets fully and certifies;
- deadline exhausted / off-loop timeout ⇒ decline;
- the P0-1 candidate gate STILL runs after a granted waiver and its decline
  releases the waiver-retried reservation;
- QUOTE-TIME LimitChecker call sites byte-identical (open-quote admission
  unchanged by the waiver flag — quote-time callers pass no certificates);
- RiskConfig validation of the new keys.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import ValidationError

import combomaker.rfq.lifecycle as lifecycle_module
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig, RiskConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.pricing_pool import StateWorstCaseInputs, _worker_state_worst_case
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.quote import ConstructedQuote
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import (
    WAIVABLE_RESERVATION_BREACHES,
    LifecycleConfig,
    OpenQuoteState,
    QuoteLifecycle,
)
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, OpenQuoteRisk
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookDecision, LastLookPolicy
from combomaker.risk.limits import Breach, LimitChecker, RiskLimits
from combomaker.risk.reservation import RiskReservationService
from combomaker.sim.book_risk import BookRiskSnapshot
from combomaker.sim.state_worst_case import GameWorstCase
from combomaker.sim.structural_book import StructuralConfigView
from tests.test_feed import snapshot_env
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, accepted_msg, rfq
from tests.test_pricing_engine import combo, seed_event
from tests.test_risk_shadow_mode import _FixedBankroll
from tests.test_state_worst_case import (
    ADV_EV,
    ARG_ADV,
    ENG_ADV,
    GAME,
    TOT3,
    TOT_EV,
)

JsonDict = dict[str, Any]

# $100 bankroll in cc. All fractions below scale off this.
BANKROLL_CC = 1_000_000
# $1,000 bankroll for the M1/M2 end-to-end rigs.
BIG_BANKROLL_CC = 10_000_000

WAIVER_COUNTERS = (
    "attempted",
    "granted",
    "declined_uncertified",
    "declined_over_budget",
    "version_conflict",
    "timeout",
    "deferred_to_reservation",
)

# The KXWC direct-drive book: candidate ARG-advance NO parlay (premium 8000cc) vs
# a resting ENG-advance NO quote (premium 8000cc). Analytic (comonotone) game
# loss = 16000cc; the exact enumeration nets to 8000cc (the quote clamps to 0 in
# every ARG state). game_loss_frac 1% of $100 = 10_000cc sits BETWEEN them: the
# analytic cap breaches, the certified state-consistent bound fits.
WAIVER_LIMITS = RiskLimits(
    caps_shadow_mode=False,
    game_loss_frac=Fraction(1, 100),        # 10_000cc — the waived cap
    per_combo_loss_frac=Fraction(10, 100),  # loose: never binds here
    directional_frac=Fraction(50, 100),
    slate_loss_frac=Fraction(50, 100),
)

# Pure-DIRECTIONAL denial: any nonzero directional trips (thr 10cc), while the
# game-loss cap stays loose (500_000cc — also the waiver budget, which the 8000cc
# state-consistent worst case fits).
DIRECTIONAL_LIMITS = RiskLimits(
    caps_shadow_mode=False,
    game_loss_frac=Fraction(50, 100),
    per_combo_loss_frac=Fraction(10, 100),
    directional_frac=Fraction(1, 100_000),  # 10cc — any direction trips
    slate_loss_frac=Fraction(50, 100),
)

# Gate-after-waiver book adds this quote's OWN resting record (candidate double-
# counted adversarially): analytic 24000cc, state-consistent 16000cc. thr 2% of
# $100 = 20_000cc sits between them.
GATE_LIMITS = RiskLimits(
    caps_shadow_mode=False,
    game_loss_frac=Fraction(2, 100),        # 20_000cc
    per_combo_loss_frac=Fraction(10, 100),
    directional_frac=Fraction(50, 100),
    slate_loss_frac=Fraction(50, 100),
)

# M1/M2 end-to-end enforced flip: the 10-contract candidate (premium ~22_000cc)
# trips ONLY the game-loss cap ($10 of $1,000) on its two (uncertifiable) games.
E2E_ENFORCED = RiskLimits(
    caps_shadow_mode=False,
    game_loss_frac=Fraction(1, 1_000),      # 10_000cc of $1,000
    per_combo_loss_frac=Fraction(10, 100),
    directional_frac=Fraction(50, 100),
    slate_loss_frac=Fraction(50, 100),
)


def _build_rig(
    h: Harness,
    store: Store,
    *,
    limits: LimitChecker,
    bankroll_cc: int | None,
    config: LifecycleConfig | None = None,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook, RiskReservationService, Metrics]:
    sender = FakeSender()
    exposure = ExposureBook(TEST_CONVENTIONS)
    metrics = Metrics()
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, h.clock,
    )
    lifecycle = QuoteLifecycle(
        clock=h.clock,
        sender=sender,
        engine=engine,
        rfq_filter=rfq_filter,
        limits=limits,
        exposure=exposure,
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(h.clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=metrics,
        lastlook_policy=LastLookPolicy(),
        # Candidate gate off by default here (it has its own wiring tests) so
        # these tests isolate the WAIVER path; the gate-ordering test re-enables it.
        config=config
        or LifecycleConfig(
            candidate_gate_enabled=False, lastlook_mc_waiver_enabled=True
        ),
        balance_tracker=_FixedBankroll(bankroll_cc),  # type: ignore[arg-type]
        start_time_provider=rfq_filter.leg_start_time,
        structural_cfg=StructuralConfigView(),
    )
    reservation = RiskReservationService(
        exposure=exposure, limits=limits, breach_splitter=lifecycle.partition_breaches
    )
    lifecycle.attach_reservation(reservation)
    return lifecycle, sender, exposure, reservation, metrics


async def _reseed_book(h: Harness, ticker: str, p_yes: float, seq: int) -> None:
    """Re-snapshot a ticker's book so its microprice lands on ``p_yes`` (equal
    sizes ⇒ microprice = mid of best-yes-bid / implied-yes-ask = p_yes)."""
    env = snapshot_env(5, seq, ticker)
    env["msg"]["yes_dollars_fp"] = [[f"{p_yes - 0.01:.4f}", "50.00"]]
    env["msg"]["no_dollars_fp"] = [[f"{0.99 - p_yes:.4f}", "50.00"]]
    await h.ws.deliver(env)


@pytest.fixture()
async def kxwc(tmp_path: Path) -> tuple[Harness, Store]:
    """World-Cup knockout fixture game (the state_worst_case test book): books
    for both advance markets + the total, NO event metadata (the analytic bound
    stays comonotone — exactly the overstated regime the waiver targets). The
    advance pair is reseeded to CONSISTENT marginals (0.55 + 0.45 = 1) — the
    default 0.48/0.48 books cannot identify a structural plan (correctly
    fail-closed uncertified)."""
    h = Harness()
    await h.with_books([ARG_ADV, ENG_ADV, TOT3])
    await _reseed_book(h, ARG_ADV, 0.55, seq=4)
    await _reseed_book(h, ENG_ADV, 0.45, seq=5)
    for ticker in (ARG_ADV, ENG_ADV, TOT3, "KXMVE-WC"):
        h.with_meta(ticker)
    store = await Store.open(tmp_path / "wc.sqlite3", h.clock)
    return h, store


@pytest.fixture()
async def m1m2(tmp_path: Path) -> tuple[Harness, Store]:
    """The standard synthetic M1/M2 lifecycle harness — legs with NO structural
    plan (any waiver enumeration comes back UNCERTIFIED — the fail-closed path)."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


def _wc_rfq(legs: list[dict[str, str]] | None = None) -> Rfq:
    return combo(
        legs
        or [{"market_ticker": ARG_ADV, "side": "yes", "event_ticker": ADV_EV}],
        id="rfq_wc",
        market_ticker="KXMVE-WC",
        contracts_fp="1.00",
    )


def _wc_state(
    *,
    quote_id: str = "q1",
    no_bid_cc: int = 8000,
    rfq_obj: Rfq | None = None,
    contracts: int = 100,
) -> OpenQuoteState:
    """A hand-built accepted-quote state for the KXWC candidate (bypasses
    handle_rfq — pricing KXWC combos through the engine is not what these tests
    prove). Sell-only: NO side quoted at ``no_bid_cc``, pending fill on NO."""
    state = OpenQuoteState(
        quote_id=quote_id,
        rfq=rfq_obj or _wc_rfq(),
        constructed=ConstructedQuote(
            yes_bid_cc=CentiCents(0),
            no_bid_cc=CentiCents(no_bid_cc),
            fair_cc=CentiCents(8500),
            width_components_cc={},
        ),
        leg_mids_cc={},
        created_mono_ns=0,
        risk_qty=CentiContracts(contracts),
    )
    state.pending_fill = (Side.NO, CentiCents(no_bid_cc), CentiContracts(contracts))
    return state


def _resting_quote(
    quote_id: str,
    market: str,
    event: str,
    *,
    no_bid_cc: int = 8000,
    contracts: int = 100,
) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=quote_id,
        rfq_id=f"r:{quote_id}",
        combo_ticker=f"KXMVE-{quote_id}",
        collection=None,
        yes_bid_cc=CentiCents(0),
        no_bid_cc=CentiCents(no_bid_cc),
        contracts=CentiContracts(contracts),
        legs=(LegRef(market, event, "yes"),),
    )


def _assert_waiver_counters(metrics: Metrics, **expected: int) -> None:
    for name in WAIVER_COUNTERS:
        assert metrics.counter(f"lastlook_waiver.{name}") == expected.get(name, 0), (
            f"lastlook_waiver.{name}"
        )


# ---------------------------------------------------------------- reason set


def test_waivable_reason_code_set_is_exactly_the_per_game_caps() -> None:
    # 2026-07-17: SKIP_MASS_ACCEPTANCE_BREACH joined because the hard-dollar
    # per-game worst-case cap emits it WITH a game key (same game-loss
    # aggregate the waiver certifies); the DELTA family emits it with
    # game=None and stays fail-closed at the game-key check.
    assert WAIVABLE_RESERVATION_BREACHES == {
        ReasonCode.SKIP_GAME_LOSS_CAP,
        ReasonCode.SKIP_DIRECTIONAL_CAP,
        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
    }


# ------------------------------------------------- granted (real enumeration)


async def test_waives_pure_game_loss_denial_certified_under_budget(
    kxwc: tuple[Harness, Store],
) -> None:
    """The handoff-A scenario end to end on the real seams: opposing-advance
    resting quote overinflates the comonotone game-loss cap; the exact
    enumeration certifies the true bound under the SAME budget; the single
    reservation retry (real RiskReservationService + real LimitChecker
    certificate re-validation) grants and HOLDS the headroom."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    state = _wc_state()

    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    # Step-2 plumbing: the denial EXPOSES its breach reasons + per-game keys.
    assert {b.reason for b in denied.breaches} == {ReasonCode.SKIP_GAME_LOSS_CAP}
    assert [b.game for b in denied.breaches] == [GAME]

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    # The retried reservation is HELD (atomic with the certificates).
    assert reservation.is_outstanding("fill:q1")
    assert reservation.outstanding_count == 1
    _assert_waiver_counters(metrics, attempted=1, granted=1)
    # Observability record: granted, the certified bound, the game.
    assert lifecycle._waiver_audit == {  # noqa: SLF001
        "granted": True,
        "worst_case_cc": 8000,
        "games": [GAME],
    }


async def test_waives_pure_directional_denial_under_game_loss_budget(
    kxwc: tuple[Harness, Store],
) -> None:
    """A directional-cap-only denial (the 2026-07-16 live failure shape) is
    waived when the state-consistent worst case fits the GAME-LOSS budget — the
    same budget, never a raised one."""
    h, store = kxwc
    limits = LimitChecker(DIRECTIONAL_LIMITS)
    lifecycle, _sender, _exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    # Two structural legs so the game identifies a plan from the candidate alone.
    state = _wc_state(
        rfq_obj=_wc_rfq(
            [
                {"market_ticker": ARG_ADV, "side": "yes", "event_ticker": ADV_EV},
                {"market_ticker": TOT3, "side": "yes", "event_ticker": TOT_EV},
            ]
        )
    )
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    assert {b.reason for b in denied.breaches} == {ReasonCode.SKIP_DIRECTIONAL_CAP}
    assert [b.game for b in denied.breaches] == [GAME]

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1)


# ------------------------------------------------------ never-waived breaches


@pytest.mark.parametrize(
    "other",
    [
        ReasonCode.SKIP_PER_COMBO_LOSS_CAP,
        ReasonCode.HALT_DAILY_LOSS,               # daily loss
        ReasonCode.SKIP_PORTFOLIO_CVAR,
        ReasonCode.SKIP_PORTFOLIO_DET_MAX,
        ReasonCode.SKIP_PORTFOLIO_RUIN,
        ReasonCode.SKIP_UTILIZATION_BACKSTOP,     # notional backstop
        ReasonCode.SKIP_SIZE_ABOVE_MAX,
        ReasonCode.SKIP_BANKROLL_UNAVAILABLE,
        ReasonCode.SKIP_CLASSIFIER_UNKNOWN,
    ],
)
async def test_never_waives_when_any_other_breach_present(
    kxwc: tuple[Harness, Store], other: ReasonCode
) -> None:
    """ANY non-waivable enforced breach in the denial ⇒ no waiver attempt at
    all (the enumeration never runs), decline as today."""
    h, store = kxwc
    lifecycle, _sender, _exposure, reservation, metrics = _build_rig(
        h, store, limits=LimitChecker(WAIVER_LIMITS), bankroll_cc=BANKROLL_CC
    )
    breaches = [
        Breach(ReasonCode.SKIP_GAME_LOSS_CAP, "game over cap", game=GAME),
        Breach(other, "another cap"),
    ]
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", _wc_state(), "fill:q1", breaches
    )
    assert ok is False
    assert "non-waivable" in detail
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics)  # nothing ran — not even "attempted"


async def test_waivable_breach_without_game_key_fails_closed(
    kxwc: tuple[Harness, Store],
) -> None:
    h, store = kxwc
    lifecycle, _sender, _exposure, reservation, metrics = _build_rig(
        h, store, limits=LimitChecker(WAIVER_LIMITS), bankroll_cc=BANKROLL_CC
    )
    breaches = [Breach(ReasonCode.SKIP_GAME_LOSS_CAP, "no game key")]
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", _wc_state(), "fill:q1", breaches
    )
    assert ok is False and "game key" in detail
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics)


async def test_slate_only_denial_never_waives(
    kxwc: tuple[Harness, Store],
) -> None:
    # 2026-07-17: a slate breach is certificate-RESOLVABLE alongside per-game
    # breaches, but a slate-ONLY denial carries no game to certify — decline.
    h, store = kxwc
    lifecycle, _sender, _exposure, reservation, metrics = _build_rig(
        h, store, limits=LimitChecker(WAIVER_LIMITS), bankroll_cc=BANKROLL_CC
    )
    breaches = [Breach(ReasonCode.SKIP_SLATE_CAP, "slate over cap")]
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", _wc_state(), "fill:q1", breaches
    )
    assert ok is False and "non-waivable" in detail
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics)


async def test_slate_co_breach_resolved_by_certificates_end_to_end(
    kxwc: tuple[Harness, Store],
) -> None:
    # 2026-07-17 (the truncated-detail final boss): slate cap tightened to the
    # game budget so the analytic roll-up breaches it alongside the game cap;
    # the waiver arms anyway (slate rides along), and the retry's certificate-
    # aware slate substitution passes on the certified exact sum — GRANT.
    import dataclasses as _dc

    h, store = kxwc
    tight = _dc.replace(WAIVER_LIMITS, slate_loss_frac=WAIVER_LIMITS.game_loss_frac)
    limits = LimitChecker(tight)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    state = _wc_state()

    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    reasons = {b.reason for b in denied.breaches}
    assert ReasonCode.SKIP_SLATE_CAP in reasons  # the analytic slate co-breach

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1)


async def test_delta_style_mass_acceptance_breach_still_fails_closed(
    kxwc: tuple[Harness, Store],
) -> None:
    # 2026-07-17: the DELTA family shares the hard-dollar cap's reason code
    # but carries NO game key — the waiver must keep refusing it (fail-closed
    # at the game-key check), never enumerate.
    h, store = kxwc
    lifecycle, _sender, _exposure, reservation, metrics = _build_rig(
        h, store, limits=LimitChecker(WAIVER_LIMITS), bankroll_cc=BANKROLL_CC
    )
    breaches = [
        Breach(ReasonCode.SKIP_GAME_LOSS_CAP, "game over cap", game=GAME),
        Breach(ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH, "market delta -900 > 300"),
    ]
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", _wc_state(), "fill:q1", breaches
    )
    assert ok is False and "game key" in detail
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics)


async def test_hard_dollar_game_breach_arms_and_grants_the_waiver(
    kxwc: tuple[Harness, Store],
) -> None:
    # 2026-07-17 (the 40-declined-wins root cause): the hard-dollar per-game
    # cap's breach — game-KEYED under SKIP_MASS_ACCEPTANCE_BREACH — arms the
    # waiver exactly like the frac cap, and the retry's certificate
    # re-validation covers the hard branch too (_waiver_covers at hard_cc).
    import dataclasses as _dc

    h, store = kxwc
    # Tighten the HARD cap so the resting-quote fold genuinely breaches it —
    # the REAL denial then carries the game-keyed hard breach alongside the
    # frac breach, and the retry exercises the _waiver_covers suppression on
    # the hard branch end-to-end (certificate 8000cc <= the 8500cc hard
    # budget AND <= the frac budget).
    tight = _dc.replace(WAIVER_LIMITS, max_event_worst_case_loss_dollars=0.85)
    limits = LimitChecker(tight)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    state = _wc_state()

    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    reasons = {b.reason for b in denied.breaches}
    assert ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH in reasons  # the hard cap
    assert all(b.game == GAME for b in denied.breaches)

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1)


# ------------------------------------------------------------ over budget


async def test_over_budget_state_worst_case_declines(
    kxwc: tuple[Harness, Store],
) -> None:
    """CO-directional resting quote: the exact enumeration cannot net it (a
    resting quote never earns credit — E2 at confirm), so the certified worst
    case (16000cc) exceeds the 10_000cc budget ⇒ decline, no reservation."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:arg", ARG_ADV, ADV_EV))
    # A small opposing quote so the game still identifies a structural plan.
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV, no_bid_cc=1000))
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    assert {b.reason for b in denied.breaches} == {ReasonCode.SKIP_GAME_LOSS_CAP}

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "game-loss budget" in detail
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, declined_over_budget=1)
    audit = lifecycle._waiver_audit  # noqa: SLF001
    assert audit is not None
    assert audit["granted"] is False
    assert audit["worst_case_cc"] == 16000  # measured, over the 10_000cc budget


# -------------------- reservation hedge credit never certifies (finding 2)


async def test_outstanding_reservation_credit_cannot_certify_the_candidate(
    kxwc: tuple[Harness, Store],
) -> None:
    """Finding-2 regression (the release channel): a concurrent OPPOSING-advance
    reservation (NOT a real holding — a decline/lapse release vanishes it) must
    not supply the miss-side credit that certifies an over-budget candidate.
    Candidate premium 12000cc > the 10000cc budget on its own; the outstanding
    ENG reservation's −7000cc credit would have netted the ARG states to
    5000cc and certified. Clamped, the worst case is the candidate's own
    12000cc ⇒ over budget ⇒ decline — so a later release(R_eng) can never
    leave the committed book past the certified budget."""
    h, store = kxwc
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle, _sender, _exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    held = reservation.try_reserve(
        "resv:eng",
        OpenPosition(
            position_id="resv:eng",
            combo_ticker="KXMVE-ENG",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(3000),
            legs=(LegRef(ENG_ADV, ADV_EV, "yes"),),
        ),
        marginals=lifecycle._marginals,  # noqa: SLF001
        daily_pnl=lifecycle.daily_pnl,
    )
    assert held.granted  # shadow caps: the hedge reservation is outstanding
    limits._limits = WAIVER_LIMITS  # noqa: SLF001 — flip to enforce (test seam)
    state = _wc_state(contracts=150)  # premium 150 * 8000 // 100 = 12000cc

    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    assert {b.reason for b in denied.breaches} == {ReasonCode.SKIP_GAME_LOSS_CAP}

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "game-loss budget" in detail
    assert not reservation.is_outstanding("fill:q1")
    assert reservation.is_outstanding("resv:eng")  # the hedge is untouched
    _assert_waiver_counters(metrics, attempted=1, declined_over_budget=1)
    audit = lifecycle._waiver_audit  # noqa: SLF001
    assert audit is not None
    assert audit["worst_case_cc"] == 12000  # clamped: no reservation credit


async def test_committed_hedge_credit_still_certifies_the_candidate(
    kxwc: tuple[Harness, Store],
) -> None:
    """The committed-position counterpart of the test above, cent-identical
    book: the SAME opposing hedge held as a COMMITTED position (which cannot
    vanish without settling) still nets fully — worst case 5000cc ≤ the
    10000cc budget ⇒ certified ⇒ the retry grants and HOLDS the headroom. The
    finding-2 clamp is surgical: only non-holding reservations lose credit."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.add_position(
        OpenPosition(
            position_id="pos:eng",
            combo_ticker="KXMVE-ENG",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(3000),
            legs=(LegRef(ENG_ADV, ADV_EV, "yes"),),
        )
    )
    # A committed book needs a fresh, usable book-risk snapshot or the CVaR/
    # det-max caps fail closed (non-waivable) — supply a benign generation-
    # matched one so the denial isolates the game-loss cap.
    lifecycle._book_risk = BookRiskSnapshot(  # noqa: SLF001
        unknown=False,
        band="high",
        n_samples=20_000,
        seed=0,
        n_positions=1,
        input_generation=exposure.position_generation,
        deterministic_max_loss_cc=3000.0,
    )
    lifecycle._book_risk_mono_ns = h.clock.monotonic_ns()  # noqa: SLF001
    state = _wc_state(contracts=150)  # premium 12000cc — over budget alone

    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    assert {b.reason for b in denied.breaches} == {ReasonCode.SKIP_GAME_LOSS_CAP}

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True and detail == ""
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1)
    audit = lifecycle._waiver_audit  # noqa: SLF001
    assert audit is not None
    assert audit["worst_case_cc"] == 5000  # committed credit nets fully


# --------------------------------------------- uncertified (full round trip)


async def test_uncertified_game_declines_end_to_end(
    m1m2: tuple[Harness, Store],
) -> None:
    """Full on_quote_accepted round trip with the REAL enumeration: M1/M2 legs
    build no structural plan ⇒ every breached game is uncertified ⇒ decline
    DECLINE_RISK_LIMIT exactly as today (and the deferral + waiver metrics show
    the path was really taken)."""
    h, store = m1m2
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle, sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BIG_BANKROLL_CC
    )
    await lifecycle.handle_rfq(rfq())  # shadow caps ⇒ the quote goes out
    assert len(sender.created) == 1
    # Flip the SAME checker to enforce for the confirm (test seam, as in
    # test_reservation_lifecycle): only the game-loss cap trips now.
    limits._limits = E2E_ENFORCED  # noqa: SLF001
    with structlog.testing.capture_logs() as cap:
        await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == []
    assert exposure.positions == {}
    assert reservation.outstanding_count == 0
    assert metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_RISK_LIMIT}") == 1
    _assert_waiver_counters(
        metrics, attempted=1, declined_uncertified=1, deferred_to_reservation=1
    )
    # The confirm risk_audit line carries the waiver observability fields.
    lines = [
        e for e in cap if e.get("event") == "risk_audit" and e.get("phase") == "decline"
    ]
    assert len(lines) == 1
    assert lines[0]["waiver_attempted"] is True
    assert lines[0]["waiver_granted"] is False
    assert lines[0]["waiver_games"] == ["E1", "E2"]


# ----------------------------------------------------- disabled ⇒ byte-identical


async def test_disabled_waiver_declines_byte_identical(
    m1m2: tuple[Harness, Store],
) -> None:
    """Committed default OFF: the SAME denial declines exactly as before the
    waiver existed — at the last-look risk_breaches check, breach-detail text,
    no deferral, no waiver metrics, no reservation activity."""
    h, store = m1m2
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle, sender, exposure, reservation, metrics = _build_rig(
        h,
        store,
        limits=limits,
        bankroll_cc=BIG_BANKROLL_CC,
        config=LifecycleConfig(candidate_gate_enabled=False),  # waiver default OFF
    )
    decisions: list[tuple[str, tuple[str, ...], JsonDict]] = []
    orig_record = store.record_decision

    async def spy(kind: str, rfq_id: str, reasons: list[str], context: JsonDict) -> None:
        decisions.append((kind, tuple(reasons), dict(context)))
        await orig_record(kind, rfq_id, reasons, context)

    store.record_decision = spy  # type: ignore[method-assign]
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1
    limits._limits = E2E_ENFORCED  # noqa: SLF001
    with structlog.testing.capture_logs() as cap:
        await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == []
    assert exposure.positions == {}
    assert reservation.outstanding_count == 0
    assert metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_RISK_LIMIT}") == 1
    _assert_waiver_counters(metrics)  # every waiver counter untouched
    declines = [d for d in decisions if d[0] == "decline"]
    assert len(declines) == 1
    assert declines[0][1] == (str(ReasonCode.DECLINE_RISK_LIMIT),)
    # The prior decline path verbatim: decide_confirm's joined breach details
    # (the game-loss cap text), NOT the reservation/waiver detail.
    assert declines[0][2]["detail"].startswith("game ")
    assert "waiver" not in declines[0][2]["detail"]
    lines = [
        e for e in cap if e.get("event") == "risk_audit" and e.get("phase") == "decline"
    ]
    assert len(lines) == 1
    assert lines[0]["waiver_attempted"] is False
    assert lines[0]["waiver_granted"] is False
    assert lines[0]["waiver_worst_case_cc"] is None
    assert lines[0]["waiver_games"] is None


# ------------------------------------------------ version conflicts (P0-2)


class _BumpingPool:
    """Stub BookRiskPool: runs the REAL enumeration but bumps the reservation
    version during the first ``bumps`` off-loop calls (a concurrent accept's
    reserve/release racing the enumeration)."""

    def __init__(self, reservation: RiskReservationService, bumps: int) -> None:
        self._reservation = reservation
        self._bumps = bumps
        self.calls = 0

    async def run_state_worst_case(
        self, inputs: StateWorstCaseInputs, *, deadline_s: float
    ) -> dict[str, GameWorstCase]:
        self.calls += 1
        if self.calls <= self._bumps:
            self._reservation._version += 1  # noqa: SLF001 (test seam)
        return _worker_state_worst_case(inputs)


async def test_version_bump_during_mc_rebuilds_once_then_declines(
    kxwc: tuple[Harness, Store],
) -> None:
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _BumpingPool(reservation, bumps=2)  # every attempt conflicts
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "unstable" in detail
    assert pool.calls == 2  # ONE rebuild after the first conflict — never a third
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, version_conflict=2)


async def test_single_version_bump_rebuild_succeeds(
    kxwc: tuple[Harness, Store],
) -> None:
    """One conflict ⇒ one rebuild ⇒ the second enumeration is stable ⇒ granted."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _BumpingPool(reservation, bumps=1)
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    ok, _detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is True
    assert pool.calls == 2
    assert reservation.is_outstanding("fill:q1")
    _assert_waiver_counters(metrics, attempted=1, granted=1, version_conflict=1)


# -------------------- open-quote churn during the enumeration (findings 1+3)


class _QuoteChurnPool:
    """Stub BookRiskPool: runs the REAL enumeration but applies one book
    mutation (a quote upsert/removal — concurrent rfq workers / maintenance
    repricing while the confirm path awaits the waiver) per off-loop call until
    the mutations run out. Quote mutations bump ONLY ``ExposureBook.generation``
    (never the position generation) — exactly the churn the 2026-07-16
    adversarial review proved the position-generation stamp missed."""

    def __init__(self, mutations: list[Any]) -> None:
        self._pending = list(mutations)
        self.calls = 0

    async def run_state_worst_case(
        self, inputs: StateWorstCaseInputs, *, deadline_s: float
    ) -> dict[str, GameWorstCase]:
        self.calls += 1
        if self._pending:
            self._pending.pop(0)()
        return _worker_state_worst_case(inputs)


async def test_quote_upsert_during_enumeration_rebuild_prices_the_new_quote(
    kxwc: tuple[Harness, Store],
) -> None:
    """Findings 1+3 regression (the concrete admit): the book sits at the
    game-loss cap via a resting OPPOSING quote (certifiable at 8000cc); while
    the off-loop enumeration runs, a NEW CO-DIRECTIONAL quote lands via
    upsert_quote — bumping only the full book generation. The certificate must
    be invalidated (version_conflict) and the ONE rebuild must price the new
    quote: the rebuilt state-consistent worst case is 16000cc > the 10000cc
    budget ⇒ decline. Before the fix the stale 8000cc certificate passed the
    position-generation stamp and skipped the per-game caps on a book whose
    true bound the omitted quote strictly understated."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _QuoteChurnPool(
        [lambda: exposure.upsert_quote(_resting_quote("q:arg2", ARG_ADV, ADV_EV))]
    )
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    assert {b.reason for b in denied.breaches} == {ReasonCode.SKIP_GAME_LOSS_CAP}

    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "game-loss budget" in detail
    # ONE rebuild ran, and it SAW the churned-in quote (worst 16000, not 8000).
    assert pool.calls == 2
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(
        metrics, attempted=1, version_conflict=1, declined_over_budget=1
    )
    audit = lifecycle._waiver_audit  # noqa: SLF001
    assert audit is not None
    assert audit["granted"] is False
    assert audit["worst_case_cc"] == 16000  # the rebuilt, quote-inclusive bound


async def test_quote_churn_on_every_attempt_fails_closed(
    kxwc: tuple[Harness, Store],
) -> None:
    """Quote churn (an upsert, then a removal — BOTH bump only the full book
    generation) on every attempt: exactly ONE rebuild, then fail-closed decline
    — mirroring the reservation-version conflict behaviour."""
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _QuoteChurnPool(
        [
            lambda: exposure.upsert_quote(
                _resting_quote("q:x", ENG_ADV, ADV_EV, no_bid_cc=1000)
            ),
            lambda: exposure.remove_quote("q:x"),
        ]
    )
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "unstable" in detail
    assert pool.calls == 2  # one rebuild, never a third attempt
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, version_conflict=2)


# ------------------------------------------------------------- deadlines


class _TimeoutPool:
    """Stub pool whose off-loop enumeration exceeds the waiver deadline (the
    asyncio.wait_for timeout propagating out of run_state_worst_case)."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_state_worst_case(
        self, inputs: StateWorstCaseInputs, *, deadline_s: float
    ) -> dict[str, GameWorstCase]:
        self.calls += 1
        raise TimeoutError


async def test_offloop_timeout_declines(kxwc: tuple[Harness, Store]) -> None:
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _TimeoutPool()
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "timed out" in detail
    assert pool.calls == 1  # a timeout never retries (the window is spent)
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, timeout=1)


class _SlowBumpingPool:
    """Stub pool that consumes the whole wall budget AND bumps the version: the
    rebuild's pre-run deadline guard must refuse to start another enumeration."""

    def __init__(self, h: Harness, reservation: RiskReservationService) -> None:
        self._h = h
        self._reservation = reservation
        self.calls = 0

    async def run_state_worst_case(
        self, inputs: StateWorstCaseInputs, *, deadline_s: float
    ) -> dict[str, GameWorstCase]:
        self.calls += 1
        self._h.clock.advance(5.0)  # > lastlook_mc_waiver_deadline_s (1.0)
        self._reservation._version += 1  # noqa: SLF001 (test seam)
        return _worker_state_worst_case(inputs)


async def test_deadline_exhausted_before_rebuild_declines(
    kxwc: tuple[Harness, Store],
) -> None:
    h, store = kxwc
    limits = LimitChecker(WAIVER_LIMITS)
    lifecycle, _sender, exposure, reservation, metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    pool = _SlowBumpingPool(h, reservation)
    lifecycle._book_risk_pool = pool  # type: ignore[assignment]  # noqa: SLF001
    state = _wc_state()
    denied = lifecycle._reserve_headroom("fill:q1", "q1", state)  # noqa: SLF001
    assert denied is not None and not denied.granted
    ok, detail = await lifecycle._lastlook_mc_waiver(  # noqa: SLF001
        "q1", state, "fill:q1", denied.breaches
    )
    assert ok is False
    assert "deadline" in detail
    assert pool.calls == 1  # the rebuild was refused, not run over-budget
    assert reservation.outstanding_count == 0
    _assert_waiver_counters(metrics, attempted=1, version_conflict=1, timeout=1)


# ------------------------------------- candidate gate STILL runs after a waiver


async def test_candidate_gate_still_runs_after_granted_waiver_and_can_decline(
    kxwc: tuple[Harness, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full on_quote_accepted ordering: waiver GRANTED (real enumeration, real
    retry) ⇒ the P0-1 candidate gate still runs — with the waiver-retried
    reservation HELD — and its decline releases that reservation."""
    h, store = kxwc
    limits = LimitChecker(GATE_LIMITS)
    lifecycle, sender, exposure, reservation, metrics = _build_rig(
        h,
        store,
        limits=limits,
        bankroll_cc=BANKROLL_CC,
        config=LifecycleConfig(
            candidate_gate_enabled=True, lastlook_mc_waiver_enabled=True
        ),
    )
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    state = _wc_state()
    state.pending_fill = None  # on_quote_accepted sets it
    lifecycle._open["q1"] = state  # noqa: SLF001 (inject the accepted quote)
    lifecycle._by_rfq[state.rfq.rfq_id] = "q1"  # noqa: SLF001
    exposure.upsert_quote(
        lifecycle._quote_risk(  # noqa: SLF001
            state.rfq, state.constructed, quote_id="q1", qty=CentiContracts(100)
        )
    )
    # Isolate from pricing/freshness: last look CONFIRMS (its risk/waiver path
    # is exercised through the reservation below, not through repricing).
    monkeypatch.setattr(
        lifecycle_module,
        "decide_confirm",
        lambda _inputs, _policy: LastLookDecision(True, ReasonCode.CONFIRM_OK),
    )
    gate_calls: list[tuple[str, str | None, int, int]] = []

    async def fake_gate(
        quote_id: str, _state: OpenQuoteState, *, reservation_id: str | None
    ) -> tuple[bool, str]:
        gate_calls.append(
            (
                quote_id,
                reservation_id,
                reservation.outstanding_count,
                metrics.counter("lastlook_waiver.granted"),
            )
        )
        return False, "post-waiver gate decline (test)"

    monkeypatch.setattr(lifecycle, "_candidate_gate_verdict", fake_gate)

    with structlog.testing.capture_logs() as cap:
        await lifecycle.on_quote_accepted(
            {"quote_id": "q1", "accepted_side": "no", "contracts_accepted_fp": "1.00"}
        )

    # The waiver was granted FIRST, with the reservation held when the gate ran.
    assert gate_calls == [("q1", "fill:q1", 1, 1)]
    # deferred=1: the last-look advisory check itself breached game-loss-only and
    # deferred to the reservation (decide_confirm is patched, but its INPUTS ran).
    _assert_waiver_counters(
        metrics, attempted=1, granted=1, deferred_to_reservation=1
    )
    # The gate still declined the confirm and released the retried reservation.
    assert sender.confirmed == []
    assert (
        metrics.counter(f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}") == 1
    )
    assert reservation.outstanding_count == 0
    assert exposure.positions == {}
    lines = [
        e for e in cap if e.get("event") == "risk_audit" and e.get("phase") == "decline"
    ]
    assert len(lines) == 1
    assert lines[0]["waiver_attempted"] is True
    assert lines[0]["waiver_granted"] is True  # granted, then gate-declined
    assert lines[0]["waiver_worst_case_cc"] == 16000
    assert lines[0]["waiver_games"] == [GAME]


# ---------------------------------------------------- input builder mapping


async def test_input_builder_maps_book_reservations_candidate_and_stamps(
    kxwc: tuple[Harness, Store],
) -> None:
    """The picklable inputs carry committed + outstanding reservations + THE
    CANDIDATE as entities (in that order; reservations hedge-credit-clamped,
    committed + candidate netting fully — finding 2), every resting quote,
    feed-resolved marginals, and the stamps captured at the read: the FULL
    book generation (quote mutations included — findings 1+3) + the
    reservation version."""
    h, store = kxwc
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))  # shadow: all grants
    lifecycle, _sender, exposure, reservation, _metrics = _build_rig(
        h, store, limits=limits, bankroll_cc=BANKROLL_CC
    )
    exposure.add_position(
        OpenPosition(
            position_id="pos:eng",
            combo_ticker="KXMVE-ENG",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(8000),
            legs=(LegRef(ENG_ADV, ADV_EV, "yes"),),
        )
    )
    held = reservation.try_reserve(
        "resv:other",
        OpenPosition(
            position_id="resv:other",
            combo_ticker="KXMVE-OTHER",
            collection=None,
            our_side=Side.NO,
            contracts=CentiContracts(100),
            entry_price_cc=CentiCents(1000),
            legs=(LegRef(TOT3, TOT_EV, "yes"),),
        ),
        marginals=lifecycle._marginals,  # noqa: SLF001
        daily_pnl=lifecycle.daily_pnl,
    )
    assert held.granted
    exposure.upsert_quote(_resting_quote("q:eng", ENG_ADV, ADV_EV))
    state = _wc_state()

    inputs = lifecycle._build_state_worst_case_inputs(  # noqa: SLF001
        "q1", state, StructuralConfigView()
    )
    assert [e.entity_id for e in inputs.entities] == [
        "pos:eng",      # committed
        "resv:other",   # outstanding reservation
        "fill:q1",      # THE CANDIDATE
    ]
    # Finding 2: ONLY the outstanding reservation is hedge-credit-clamped.
    assert [e.earns_credit for e in inputs.entities] == [True, False, True]
    assert [q.quote_id for q in inputs.open_quotes] == ["q:eng"]
    assert set(inputs.marginals) == {ARG_ADV, ENG_ADV, TOT3}
    assert inputs.events is None
    # Findings 1+3: the stamp is the FULL book generation — the quote upsert
    # above bumped it PAST the position generation, and the waiver (whose input
    # set prices open quotes) must track quote churn, not just positions.
    assert inputs.book_generation == exposure.generation
    assert exposure.generation != exposure.position_generation
    assert inputs.reservation_version == reservation.version


# ------------------------------------------- quote-time call sites unchanged


@pytest.mark.parametrize("waiver_enabled", [False, True])
async def test_quote_time_admission_byte_identical_with_waiver_flag(
    m1m2: tuple[Harness, Store], waiver_enabled: bool
) -> None:
    """QUOTE-TIME regression: the waiver flag changes NOTHING before confirm.
    LimitChecker.check's quote-time callers pass no certificates (default None),
    so open-quote admission and quote-time risk declines are identical with the
    waiver on or off — and no waiver counter ever moves at quote time."""
    h, store = m1m2
    limits = LimitChecker(RiskLimits())  # enforced defaults
    lifecycle, sender, _exposure, _reservation, metrics = _build_rig(
        h,
        store,
        limits=limits,
        bankroll_cc=BIG_BANKROLL_CC,
        config=LifecycleConfig(
            candidate_gate_enabled=False, lastlook_mc_waiver_enabled=waiver_enabled
        ),
    )
    # An oversized RFQ is risk-declined at quote time (never sent) …
    await lifecycle.handle_rfq(combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E2"},
        ],
        contracts_fp="9999.00",
    ))
    assert sender.created == []
    # … and a normal RFQ is still admitted (open-quote admission unchanged).
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1
    _assert_waiver_counters(metrics)  # zero, whatever the flag


# ----------------------------------------------------------- config validation


def test_risk_config_waiver_defaults_and_validation() -> None:
    cfg = RiskConfig()
    assert cfg.lastlook_mc_waiver_enabled is False  # committed default OFF
    assert cfg.lastlook_mc_waiver_deadline_s == 1.0
    # Must fit inside the 3s confirm window: (0, 3] accepted, else rejected.
    assert RiskConfig(lastlook_mc_waiver_deadline_s=3.0).lastlook_mc_waiver_deadline_s == 3.0
    for bad in (0.0, -1.0, 3.5, float("nan")):
        with pytest.raises(ValidationError):
            RiskConfig(lastlook_mc_waiver_deadline_s=bad)


def test_lifecycle_config_waiver_defaults() -> None:
    cfg = LifecycleConfig()
    assert cfg.lastlook_mc_waiver_enabled is False
    assert cfg.lastlook_mc_waiver_deadline_s == 1.0
