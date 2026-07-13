"""Fill-velocity governor (wire-live 2026-07-13).

Unit tests of the rolling-window tracker (record / prune / count) PLUS the
lifecycle wiring: a burst over the count or notional window DECLINEs further
confirms + cancels-all resting quotes; a hard-multiple burst HALTs
HALT_FILL_VELOCITY; normal flow is unaffected; and the COUNT limit binds even on
a stale bankroll (fail-closed).
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.fill_velocity import FillVelocityTracker
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event
from tests.test_risk_shadow_mode import _FixedBankroll


# --------------------------------------------------------------------------- #
# Tracker unit tests
# --------------------------------------------------------------------------- #
class TestFillVelocityTracker:
    def test_window_sums_and_counts_recent_only(self) -> None:
        clock = FakeClock()
        t = FillVelocityTracker(clock, window_s=2.0)
        t.record(1_000)
        clock.advance(0.5)
        t.record(2_000)
        s = t.state()
        assert s.committed_cc == 3_000
        assert s.count == 2
        # Advance past the window so the FIRST event (at t=0) ages out (2.0s old
        # relative to the second at 0.5s → the first is >2s behind after +1.6s).
        clock.advance(1.6)  # now 2.1s after the first event, 1.6s after the second
        s2 = t.state()
        assert s2.committed_cc == 2_000  # only the second event survives
        assert s2.count == 1

    def test_all_age_out(self) -> None:
        clock = FakeClock()
        t = FillVelocityTracker(clock, window_s=2.0)
        t.record(5_000)
        clock.advance(3.0)
        s = t.state()
        assert s.committed_cc == 0
        assert s.count == 0

    def test_zero_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="window_s"):
            FillVelocityTracker(FakeClock(), window_s=0.0)

    def test_nonpositive_commit_counts_but_adds_no_notional(self) -> None:
        clock = FakeClock()
        t = FillVelocityTracker(clock, window_s=2.0)
        t.record(0)
        t.record(-100)  # defensive: clamped to 0 notional but still a fill
        s = t.state()
        assert s.committed_cc == 0
        assert s.count == 2


# --------------------------------------------------------------------------- #
# Lifecycle wiring
# --------------------------------------------------------------------------- #
def _rfq(rfq_id: str):  # noqa: ANN202 (test helper; returns Rfq)
    # Cross-event 2-leg combo, contracts mode. Distinct id ⇒ distinct quote.
    return combo(CROSS_EVENT_LEGS, id=rfq_id)


def _accept(quote_id: str, *, contracts_fp: str = "10.00") -> dict[str, object]:
    return {
        "quote_id": quote_id,
        "rfq_id": "rfq",
        "accepted_side": "no",  # sell-only ⇒ the seller (NO) side fills
        "contracts_accepted_fp": contracts_fp,
    }


def _build(
    h: Harness,
    store: Store,
    *,
    bankroll_cc: int | None,
    soft_frac: str = "0.05",
    hard_frac: str = "0.10",
    max_fills: int = 8,
    window_s: float = 2.0,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook]:
    sender = FakeSender()
    exposure = ExposureBook(TEST_CONVENTIONS)
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, h.clock,
    )
    # All %-caps LOOSE so ONLY the fill-velocity governor can decline/halt.
    limits = LimitChecker(
        RiskLimits(
            caps_shadow_mode=False,
            game_loss_frac=Fraction(99, 100),
            per_combo_loss_frac=Fraction(99, 100),
            directional_frac=Fraction(99, 100),
            slate_loss_frac=Fraction(99, 100),
            daily_loss_frac=Fraction(99, 100),
            drawdown_frac=Fraction(99, 100),
            hard_trip_frac=Fraction(99, 100),
            portfolio_cvar_frac=Fraction(99, 100),
            absolute_notional_multiple=999_999,
            fill_velocity_window_s=window_s,
            fill_velocity_soft_frac=Fraction(int(float(soft_frac) * 1000), 1000),
            fill_velocity_hard_frac=Fraction(int(float(hard_frac) * 1000), 1000),
            fill_velocity_max_fills=max_fills,
        )
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
        metrics=Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(),
        balance_tracker=_FixedBankroll(bankroll_cc),  # type: ignore[arg-type]
        start_time_provider=rfq_filter.leg_start_time,
    )
    return lifecycle, sender, exposure


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


async def _quote_and_accept(
    lifecycle: QuoteLifecycle, rfq_id: str, quote_id: str
) -> None:
    await lifecycle.handle_rfq(_rfq(rfq_id))
    await lifecycle.on_quote_accepted(_accept(quote_id))
    # Arm a fresh book-risk snapshot after each fill so the portfolio-CVaR cap
    # (which fails closed on a non-empty book with no/stale snapshot) does NOT
    # interfere with the fill-velocity governor these tests isolate. The CVaR
    # ceiling itself is set loose (99%) in _build; only the fill-velocity limits
    # are meant to bite here. recompute_book_risk() runs the MC directly (off the
    # hot path in production; called explicitly here since we never tick the loop).
    lifecycle.recompute_book_risk()


async def test_normal_flow_unaffected(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    # A couple of accepts, huge bankroll, well under 8 fills ⇒ every confirm goes
    # through, no cancel-all, no halt.
    lifecycle, sender, _ = _build(h, store, bankroll_cc=100_000_000_000)
    await _quote_and_accept(lifecycle, "r1", "q1")
    await _quote_and_accept(lifecycle, "r2", "q2")
    assert sender.confirmed == ["q1", "q2"]
    assert not h.killswitch.halted
    assert sender.deleted == []  # no cancel-all fired


async def test_count_burst_declines_and_cancels_all(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # HUGE bankroll so the notional fracs never bind — isolate the COUNT limit
    # (max_fills=2). After 2 confirmed fills, the NEXT accept trips
    # DECLINE_FILL_VELOCITY, which cancels-all resting quotes.
    lifecycle, sender, exposure = _build(
        h, store, bankroll_cc=100_000_000_000, max_fills=2
    )
    await _quote_and_accept(lifecycle, "r1", "q1")
    await _quote_and_accept(lifecycle, "r2", "q2")
    assert len(sender.confirmed) == 2
    # Leave a RESTING (un-accepted) quote on the wire, then trip the count with a
    # 3rd accept — the DECLINE cancels-all, deleting the resting quote.
    await lifecycle.handle_rfq(_rfq("resting"))  # opens q3, left resting
    resting_id = sender.created[-1]["id"]
    assert lifecycle.has_open_quote("resting")
    # A 4th quote+accept → count 3 > 2 ⇒ DECLINE + cancel-all.
    await lifecycle.handle_rfq(_rfq("r4"))
    accepted_id = sender.created[-1]["id"]
    await lifecycle.on_quote_accepted(_accept(accepted_id))
    # The over-count fill did NOT confirm...
    assert accepted_id not in sender.confirmed
    assert len(sender.confirmed) == 2
    # ...and cancel-all deleted the RESTING quote (the DECLINE action).
    assert resting_id in sender.deleted
    assert not lifecycle.has_open_quote("resting")
    assert not h.killswitch.halted  # a SOFT decline, not a halt


async def test_hard_notional_burst_halts(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    # Drive the governor's verdict directly at the HARD threshold: a burst whose
    # committed notional in the window exceeds the 10% HARD frac of bankroll must
    # yield "halt". Bankroll $10 (100_000cc); record fills totalling > 10_000cc.
    lifecycle, _, _ = _build(
        h, store, bankroll_cc=100_000, soft_frac="0.05", hard_frac="0.10",
        max_fills=999,
    )
    # committed_cc = qty x bid // 100. qty=1_000 (10.00 ct), bid=400 ⇒ 4_000cc per
    # fill. 3 fills = 12_000cc > 10% of 100_000 (10_000cc) ⇒ halt.
    for _ in range(3):
        lifecycle._record_fill_velocity(CentiCents(400), CentiContracts(1_000))
    verdict, _detail = lifecycle._fill_velocity_verdict()
    assert verdict == "halt"
    # And the maintenance loop escalates it to the killswitch (HALT_FILL_VELOCITY).
    await lifecycle.maintenance_tick()
    assert h.killswitch.halted
    assert h.killswitch.halt_event is not None
    assert h.killswitch.halt_event.reason is ReasonCode.HALT_FILL_VELOCITY


async def test_soft_notional_burst_declines(harness: tuple[Harness, Store]) -> None:
    h, store = harness
    # Committed notional over the SOFT frac (5%) but under HARD (10%) ⇒ "decline".
    lifecycle, _, _ = _build(
        h, store, bankroll_cc=100_000, soft_frac="0.05", hard_frac="0.10",
        max_fills=999,
    )
    # qty=1_000, bid=600 ⇒ 6_000cc = 6% of 100_000: over soft (5_000) under hard
    # (10_000).
    lifecycle._record_fill_velocity(CentiCents(600), CentiContracts(1_000))
    verdict, _detail = lifecycle._fill_velocity_verdict()
    assert verdict == "decline"


async def test_stale_bankroll_count_limit_still_binds(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # STALE bankroll (None): the %-of-bankroll notional thresholds cannot compute,
    # but the COUNT limit is bankroll-free and STILL binds (fail-closed). Drive the
    # verdict directly: over max_fills with a None bankroll ⇒ "decline".
    lifecycle, _, _ = _build(h, store, bankroll_cc=None, max_fills=3)
    for _ in range(4):  # 4 fills > max 3
        lifecycle._record_fill_velocity(CentiCents(10_000), CentiContracts(1_000))
    verdict, detail = lifecycle._fill_velocity_verdict()
    assert verdict == "decline"
    assert "count" in detail
    # And a huge committed notional with a stale bankroll does NOT escalate to a
    # halt (the hard notional branch can't compute) — the count decline dominates.
    assert not h.killswitch.halted


async def test_window_decay_reallows_after_burst(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # After a count burst, advancing past the window clears the counter so new
    # confirms are allowed again (the governor self-clears; not a latch).
    lifecycle, sender, _ = _build(
        h, store, bankroll_cc=100_000_000_000, max_fills=2, window_s=2.0
    )
    await _quote_and_accept(lifecycle, "r1", "q1")
    await _quote_and_accept(lifecycle, "r2", "q2")
    assert len(sender.confirmed) == 2
    # 3rd would decline (count 3 > 2)...
    await _quote_and_accept(lifecycle, "r3", "q3")
    assert "q3" not in sender.confirmed
    # ...advance past the window so the earlier fills age out, then a new accept
    # confirms again.
    h.clock.advance(3.0)
    await _quote_and_accept(lifecycle, "r4", "q4")
    assert "q4" in sender.confirmed
