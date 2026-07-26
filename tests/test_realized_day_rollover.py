"""DAY ROLLOVER for the realized accumulator (2026-07-25).

``_realized_pnl_cc`` is the REALIZED half of DailyPnl — the daily-loss halt
and the give-back/KILL ladder bind on it. It was only zeroed in the
constructor, so a process running across midnight carried the prior day's
realized P&L into the new day's baseline: a PROFITABLE day silently loosened
the next day's daily-loss halt by exactly that profit. Rolls on the US/
Eastern calendar date — the SAME boundary the slate cap already uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from combomaker.core.clock import FakeClock
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
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender


async def _lifecycle(tmp_path: Path, clock: FakeClock) -> QuoteLifecycle:
    h = Harness()
    h.clock = clock
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, clock,
    )
    return QuoteLifecycle(
        clock=clock,
        sender=FakeSender(),
        engine=engine,
        rfq_filter=rfq_filter,
        limits=LimitChecker(RiskLimits()),
        exposure=ExposureBook(TEST_CONVENTIONS),
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(),
        start_time_provider=rfq_filter.leg_start_time,
    )


async def test_realized_resets_on_et_day_change(tmp_path: Path) -> None:
    # 2026-07-25 22:00 ET == 2026-07-26 02:00 UTC (still the 25th in ET).
    clock = FakeClock(datetime(2026, 7, 26, 2, 0, tzinfo=UTC))
    lc = await _lifecycle(tmp_path, clock)
    lc.record_realized_pnl(250_000)  # +$25 banked today
    lc._refresh_daily_pnl()  # noqa: SLF001
    assert lc.daily_pnl.realized_cc == 250_000

    # Same ET day, later: no reset.
    clock.advance(5_400)  # +1.5h, still the 25th in ET
    lc._refresh_daily_pnl()  # noqa: SLF001
    assert lc.daily_pnl.realized_cc == 250_000

    # Past ET midnight (2026-07-26 05:00 UTC == 01:00 ET on the 26th).
    clock.advance(5_400)  # past ET midnight (05:00Z = 01:00 ET on the 26th)
    lc._refresh_daily_pnl()  # noqa: SLF001
    assert lc.daily_pnl.realized_cc == 0  # fresh daily-loss baseline

    # New day's realized accrues from zero and does NOT re-reset.
    lc.record_realized_pnl(-40_000)
    lc._refresh_daily_pnl()  # noqa: SLF001
    assert lc.daily_pnl.realized_cc == -40_000


async def test_losing_day_also_rolls(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 26, 2, 0, tzinfo=UTC))
    lc = await _lifecycle(tmp_path, clock)
    lc.record_realized_pnl(-300_000)  # a -$30 day
    lc._refresh_daily_pnl()  # noqa: SLF001
    assert lc.daily_pnl.realized_cc == -300_000
    clock.advance(10_800)  # +3h ⇒ 05:00Z = 01:00 ET on the 26th (new ET day)
    lc._refresh_daily_pnl()  # noqa: SLF001
    assert lc.daily_pnl.realized_cc == 0
