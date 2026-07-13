"""Reservation service wired INTO the QuoteLifecycle hot path (Phase 3).

Proves the confirm path RESERVES headroom before the round-trip and does the
right thing on each outcome — through the real ``QuoteLifecycle`` (not a unit of
the service), so it proves the WIRING, not just the service:

- happy path: confirm succeeds → the reservation commits → position booked once,
  and the reservation service is drained (no dangling headroom).
- confirm TIMEOUT: the confirm raises → the reservation is marked UNCONFIRMED
  (headroom held, assume-committed) — never released on a lost ack.
- execution after a timeout: ``on_quote_executed`` commits the held reservation
  → booked exactly once, service drained.
- ENFORCED denial: with caps flipped to enforce and a tiny bankroll, the
  reservation is denied → the lifecycle DECLINES (never confirms) — the last book
  of headroom went elsewhere.
- SHADOW mode (default): a tiny bankroll trips every %-cap but the reservation is
  granted (shadow split) → confirm proceeds exactly as in Phase 2.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.risk.reservation import RiskReservationService
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, accepted_msg, rfq
from tests.test_pricing_engine import seed_event
from tests.test_risk_shadow_mode import _FixedBankroll


def _build(
    h: Harness,
    store: Store,
    *,
    limits: LimitChecker,
    bankroll_cc: int | None,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook, RiskReservationService]:
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
        config=LifecycleConfig(quote_ttl_s=30.0, reprice_threshold_cc=100),
        balance_tracker=_FixedBankroll(bankroll_cc),  # type: ignore[arg-type]
        start_time_provider=rfq_filter.leg_start_time,
    )
    reservation = RiskReservationService(
        exposure=exposure, limits=limits, breach_splitter=lifecycle.partition_breaches
    )
    lifecycle.attach_reservation(reservation)
    return lifecycle, sender, exposure, reservation


@pytest.fixture()
async def harness(tmp_path: Path) -> tuple[Harness, Store]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


# A comfortable bankroll so no cap binds when caps are enforced but exposure tiny.
BIG_BANKROLL_CC = 100_000_000_000
TINY_BANKROLL_CC = 100  # $0.01 — trips every %-cap


async def test_happy_path_commits_reservation_and_books_once(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    lifecycle, sender, exposure, reservation = _build(
        h, store, limits=limits, bankroll_cc=BIG_BANKROLL_CC
    )
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == ["q1"]
    # Reservation committed at confirm → booked once, service drained.
    assert "fill:q1" in exposure.positions
    assert reservation.outstanding_count == 0
    # Execution after confirm is a harmless no-op (position already booked once).
    await lifecycle.on_quote_executed({"quote_id": "q1"})
    assert len(exposure.positions) == 1


async def test_confirm_timeout_marks_reservation_unconfirmed(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    lifecycle, sender, exposure, reservation = _build(
        h, store, limits=limits, bankroll_cc=BIG_BANKROLL_CC
    )
    sender.fail_confirm = True  # the confirm round-trip raises (timeout-like)
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    # The confirm failed: the reservation is HELD (assume-committed), NOT released,
    # and flagged unconfirmed pending exchange reconciliation.
    assert reservation.outstanding_count == 1
    assert reservation.is_unconfirmed("fill:q1") is True
    # The position is NOT in the book yet (only committed on execution/reconcile),
    # but the headroom is still consumed by the outstanding reservation.
    assert "fill:q1" not in exposure.positions


async def test_execution_after_timeout_commits_held_reservation(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    lifecycle, sender, exposure, reservation = _build(
        h, store, limits=limits, bankroll_cc=BIG_BANKROLL_CC
    )
    sender.fail_confirm = True
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert reservation.is_unconfirmed("fill:q1") is True
    # The fill really landed — the execution message arrives and commits the held
    # reservation exactly once.
    await lifecycle.on_quote_executed({"quote_id": "q1"})
    assert "fill:q1" in exposure.positions
    assert reservation.outstanding_count == 0
    assert len(exposure.positions) == 1


async def test_enforced_denial_at_confirm_only(
    harness: tuple[Harness, Store],
) -> None:
    """Isolate the CONFIRM-time denial: quote passes pre-quote (caps shadow so the
    quote goes out), then flip the SAME checker to enforce before the accept so the
    reservation is denied at confirm — the lifecycle declines, books nothing."""
    h, store = harness
    limits_obj = RiskLimits(caps_shadow_mode=True)
    limits = LimitChecker(limits_obj)
    lifecycle, sender, exposure, reservation = _build(
        h, store, limits=limits, bankroll_cc=TINY_BANKROLL_CC
    )
    await lifecycle.handle_rfq(rfq())  # shadow → quote goes out
    assert len(sender.created) == 1
    # Flip the checker's limits to ENFORCE for the confirm-time reservation.
    lifecycle._limits._limits = RiskLimits(caps_shadow_mode=False)  # noqa: SLF001
    reservation._limits._limits = RiskLimits(caps_shadow_mode=False)  # noqa: SLF001
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    # Reservation denied at confirm (tiny bankroll, now enforced) → declined.
    assert sender.confirmed == []
    assert exposure.positions == {}
    assert reservation.outstanding_count == 0
    assert lifecycle._metrics.counter(  # noqa: SLF001
        f"confirm.declined.{ReasonCode.DECLINE_RISK_LIMIT}"
    ) == 1


async def test_shadow_mode_reservation_grants_and_confirms(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # Default SHADOW: a tiny bankroll trips every %-cap but the shadow split drops
    # them, so the reservation is granted and the confirm proceeds — Phase-2
    # behaviour is unchanged by the reservation wiring.
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle, sender, exposure, reservation = _build(
        h, store, limits=limits, bankroll_cc=TINY_BANKROLL_CC
    )
    await lifecycle.handle_rfq(rfq())
    await lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert sender.confirmed == ["q1"]
    assert "fill:q1" in exposure.positions
    assert reservation.outstanding_count == 0
