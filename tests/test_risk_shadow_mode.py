"""SHADOW-mode behavioural guarantee (Phase 2): an R2 %-of-bankroll cap breach,
while ``caps_shadow_mode`` is True, is LOG-ONLY — it does NOT remove/block a
quote, does NOT decline a confirm, and does NOT trigger a halt. Only enforced
(shadow=False) breaches change behaviour. This is the test the plan requires:
"in shadow mode a new-cap breach does NOT remove a quote / does NOT halt."

Driven through the real ``QuoteLifecycle`` hot path (not a unit of the checker),
so it proves the WIRING is shadow-safe, not just the flag.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits, StarvationWatchdog
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, rfq
from tests.test_pricing_engine import seed_event


class _FixedBankroll:
    """Minimal stand-in for the balance tracker's non-raising bankroll accessor:
    the lifecycle only calls ``risk_bankroll_cc_or_none()``. Returns a fixed cc
    so the R2 caps compute (a real ``BalanceTracker`` is exercised in
    test_balance.py; here we isolate the shadow behaviour)."""

    def __init__(self, bankroll_cc: int | None) -> None:
        self._cc = bankroll_cc

    def risk_bankroll_cc_or_none(self) -> int | None:
        return self._cc


def _build_lifecycle(
    h: Harness,
    store: Store,
    *,
    limits: LimitChecker,
    bankroll_cc: int | None,
    watchdog: StarvationWatchdog | None = None,
) -> tuple[QuoteLifecycle, FakeSender, ExposureBook, Metrics]:
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
        starvation_watchdog=watchdog,
    )
    return lifecycle, sender, exposure, metrics


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


# A tiny bankroll ($0.01) so ANY exposure trips EVERY %-cap — the strongest test
# that shadow still lets the quote through.
TINY_BANKROLL_CC = 100


async def test_shadow_cap_breach_does_not_block_the_quote(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # Every R2 cap is set to fire (tiny bankroll), but caps_shadow_mode=True.
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle, sender, exposure, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=TINY_BANKROLL_CC
    )
    await lifecycle.handle_rfq(rfq())
    # The quote WAS sent and tracked — the shadow breaches were log-only.
    assert len(sender.created) == 1
    assert lifecycle.open_quote_count == 1
    assert "q1" in exposure.open_quotes


async def test_enforced_cap_breach_blocks_the_quote(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # SAME tiny bankroll, but caps_shadow_mode=False → the caps ENFORCE.
    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    lifecycle, sender, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=TINY_BANKROLL_CC
    )
    await lifecycle.handle_rfq(rfq())
    # Now the enforced %-cap breach blocked it: nothing sent.
    assert sender.created == []
    assert lifecycle.open_quote_count == 0


async def test_shadow_daily_loss_does_not_halt(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    # A 6% shadow daily-loss cap that WOULD fire (loss well over 6% of bankroll),
    # driven through maintenance_tick. Shadow ⇒ the killswitch must NOT halt.
    limits = LimitChecker(
        RiskLimits(
            caps_shadow_mode=True,
            daily_loss_frac=Fraction(6, 100),
            # Keep the ENFORCED hard-dollar daily cap far away so only the shadow
            # %-cap could (but must not) halt.
            max_daily_loss_dollars=1_000_000.0,
        )
    )
    lifecycle, _, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=20_000_000  # $2,000
    )
    # maintenance_tick recomputes daily_pnl from the realized ledger, so feed the
    # loss through the ledger (setting daily_pnl directly would be overwritten).
    lifecycle.record_realized_pnl(-5_000_000)  # -$500 >> 6% ($120)
    await lifecycle.maintenance_tick()
    assert not h.killswitch.halted


async def test_enforced_daily_loss_halts(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    limits = LimitChecker(
        RiskLimits(
            caps_shadow_mode=False,
            daily_loss_frac=Fraction(6, 100),
            max_daily_loss_dollars=1_000_000.0,  # enforced hard cap far away
        )
    )
    lifecycle, _, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=20_000_000
    )
    lifecycle.record_realized_pnl(-5_000_000)
    await lifecycle.maintenance_tick()
    assert h.killswitch.halted


async def test_watchdog_observes_shadow_would_be_declines(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    watchdog = StarvationWatchdog(threshold=2)
    # Tiny bankroll → every RFQ shadow-BREACHES (a would-be decline) but the
    # quote STILL goes out (shadow). The watchdog observes the shadow would-be
    # decline so a mis-set cap surfaces BEFORE the operator enforces — two in a
    # row fires the warning even though both quotes were issued.
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle, sender, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=TINY_BANKROLL_CC, watchdog=watchdog
    )
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1          # SHADOW: the quote still went out
    assert watchdog.consecutive_declines == 1
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 2          # both issued (shadow)
    assert watchdog.starved is True          # ...but the watchdog warned


async def test_watchdog_resets_on_a_clean_issue(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    watchdog = StarvationWatchdog(threshold=2)
    # A HUGE bankroll → no cap fires at all → a clean issue → the watchdog stays
    # at zero (a truly clean quote resets/keeps it un-starved).
    limits = LimitChecker(RiskLimits(caps_shadow_mode=True))
    lifecycle, sender, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=100_000_000_000, watchdog=watchdog
    )
    await lifecycle.handle_rfq(rfq())
    assert len(sender.created) == 1
    assert watchdog.consecutive_declines == 0
    assert watchdog.starved is False


async def test_watchdog_fires_when_enforced_caps_starve(
    harness: tuple[Harness, Store],
) -> None:
    h, store = harness
    watchdog = StarvationWatchdog(threshold=2)
    # Enforced tiny-bankroll caps → every RFQ is declined for real. Two in a row
    # → the watchdog fires (consecutive risk declines with zero quotes issued).
    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    lifecycle, sender, _, _ = _build_lifecycle(
        h, store, limits=limits, bankroll_cc=TINY_BANKROLL_CC, watchdog=watchdog
    )
    await lifecycle.handle_rfq(rfq())
    await lifecycle.handle_rfq(rfq())
    assert sender.created == []
    assert watchdog.starved is True


async def test_slate_provider_wired_from_filter_pregame(
    harness: tuple[Harness, Store],
) -> None:
    # The start_time_provider the app wires is filter.leg_start_time; assert it
    # returns a usable start for an embedded-start MLB ticker (the slate cap's
    # source) and None for an unknowable one — the exact PregameGate behaviour.
    h, store = harness
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0), h.feed, h.metadata, h.killswitch, h.clock
    )
    got = rfq_filter.leg_start_time("KXMLBGAME-26JUL101915BOSNYM-BOS")
    assert isinstance(got, datetime)
    assert got.astimezone(UTC) is not None
    # An unknowable ticker (no embedded start, no metadata) → None.
    assert rfq_filter.leg_start_time("ZZZ-NOSUCH") is None
