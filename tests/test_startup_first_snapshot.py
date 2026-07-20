"""STARTUP SYNCHRONOUS FIRST SNAPSHOT (2026-07-16 warmup fix).

The live 2026-07-16 run declined its first 69 RFQs ``skip_portfolio_cvar``
purely because the rehydrated (non-empty) book had no book-risk snapshot yet —
the CVaR/det-max caps correctly fail CLOSED on a never-measured book, but the
first ~40s of warmup were pure fail-closed noise. ``QuoteApp.
_startup_book_risk_snapshot`` computes ONE snapshot synchronously (bounded,
via the EXISTING recompute machinery) after rehydration and before quote
processing. Covered here:

- startup with positions ⇒ the first RFQ is evaluated against a FRESH usable
  snapshot (quote issued; no skip_portfolio_cvar warmup decline);
- without the snapshot the same rig declines skip_portfolio_cvar (the exact
  warmup behaviour being killed — proves the test bites);
- snapshot ERROR ⇒ today's behaviour (startup proceeds, caps keep failing
  closed until the maintenance loop publishes one);
- snapshot TIMEOUT ⇒ bounded — returns promptly, startup proceeds as today;
- the machinery is REUSED: the method drives recompute_book_risk_offloop, not
  a duplicate MC path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import QuoteApp
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event
from tests.test_risk_shadow_mode import _FixedBankroll

BANKROLL_CC = 10_000_000  # $1,000 — every %-cap budget comfortably clears


class StartupRig:
    def __init__(self, h: Harness, store: Store) -> None:
        self.h = h
        self.store = store
        self.sender = FakeSender()
        self.exposure = ExposureBook(TEST_CONVENTIONS)
        self.metrics = Metrics()
        engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
        self.lifecycle = QuoteLifecycle(
            clock=h.clock,
            sender=self.sender,
            engine=engine,
            rfq_filter=RfqFilter(
                FiltersConfig(min_time_to_close_s=0.0).model_copy(
                    update={"allowed_leg_series_prefixes": None}
                ),
                h.feed, h.metadata, h.killswitch, h.clock,
            ),
            limits=LimitChecker(RiskLimits()),
            exposure=self.exposure,
            feed=h.feed,
            metadata=h.metadata,
            inplay=InPlayDetector(h.clock),
            killswitch=h.killswitch,
            conventions=TEST_CONVENTIONS,
            store=store,
            metrics=self.metrics,
            lastlook_policy=LastLookPolicy(),
            config=LifecycleConfig(book_risk_mc_samples=2_000),
            # A live bankroll ARMS the %-cap layer, so the CVaR fail-closed
            # path really binds pre-snapshot (the live warmup regime).
            balance_tracker=_FixedBankroll(  # type: ignore[arg-type]
                BANKROLL_CC, current_cc=BANKROLL_CC
            ),
        )


def _rehydrated_position() -> OpenPosition:
    """A small held position on markets with LIVE books (M1/M2), standing in
    for the rehydrated book at startup."""
    return OpenPosition(
        position_id="rehydrate:KXMVE-HELD",
        combo_ticker="KXMVE-HELD",
        collection="KXMVESPORTS",
        our_side=Side.NO,
        contracts=CentiContracts(1_000),
        entry_price_cc=CentiCents(5_000),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "yes")),
    )


async def _make_rig(tmp_path: Path, *, db: str) -> StartupRig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    rig = StartupRig(h, store)
    rig.exposure.add_position(_rehydrated_position())
    return rig


async def _skip_reasons(store: Store) -> dict[str, int]:
    return await store.decision_reason_counts()


async def test_without_snapshot_first_rfq_fails_closed(tmp_path: Path) -> None:
    """The exact warmup behaviour the fix kills — proves the fixture bites."""
    rig = await _make_rig(tmp_path, db="warmup.sqlite3")
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert rig.sender.created == []
    reasons = await _skip_reasons(rig.store)
    assert reasons.get(str(ReasonCode.SKIP_PORTFOLIO_CVAR), 0) >= 1


async def test_startup_snapshot_kills_warmup_declines(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, db="fixed.sqlite3")
    # The startup step (called unbound, like the rehydrate tests — it touches
    # only the lifecycle): ONE synchronous snapshot before quoting opens.
    await QuoteApp._startup_book_risk_snapshot(cast(Any, None), rig.lifecycle)
    # The very FIRST RFQ is now evaluated against a fresh, USABLE snapshot.
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert len(rig.sender.created) == 1  # quoted — no warmup fail-closed
    reasons = await _skip_reasons(rig.store)
    assert str(ReasonCode.SKIP_PORTFOLIO_CVAR) not in reasons
    assert str(ReasonCode.SKIP_PORTFOLIO_DET_MAX) not in reasons


async def test_snapshot_reuses_existing_recompute_machinery(tmp_path: Path) -> None:
    """Rule 8: the startup step drives recompute_book_risk_offloop — never a
    parallel MC implementation."""
    rig = await _make_rig(tmp_path, db="reuse.sqlite3")
    calls: list[str] = []
    real = rig.lifecycle.recompute_book_risk_offloop

    async def spying() -> None:
        calls.append("recompute")
        await real()

    rig.lifecycle.recompute_book_risk_offloop = spying  # type: ignore[method-assign]
    await QuoteApp._startup_book_risk_snapshot(cast(Any, None), rig.lifecycle)
    assert calls == ["recompute"]


async def test_snapshot_error_proceeds_as_today(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, db="err.sqlite3")

    async def boom() -> None:
        raise RuntimeError("mc pool crashed")

    rig.lifecycle.recompute_book_risk_offloop = boom  # type: ignore[method-assign]
    # Never raises out of startup…
    await QuoteApp._startup_book_risk_snapshot(cast(Any, None), rig.lifecycle)
    # …and behaviour is exactly today's: the unmeasured book keeps failing
    # closed until the maintenance loop publishes a snapshot.
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert rig.sender.created == []
    reasons = await _skip_reasons(rig.store)
    assert reasons.get(str(ReasonCode.SKIP_PORTFOLIO_CVAR), 0) >= 1


async def test_snapshot_timeout_is_bounded_and_proceeds(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, db="timeout.sqlite3")

    async def hangs() -> None:
        await asyncio.sleep(30.0)

    rig.lifecycle.recompute_book_risk_offloop = hangs  # type: ignore[method-assign]
    # Bounded: returns promptly at the deadline instead of blocking startup.
    await asyncio.wait_for(
        QuoteApp._startup_book_risk_snapshot(
            cast(Any, None), rig.lifecycle, deadline_s=0.05
        ),
        timeout=5.0,
    )
    # No snapshot landed ⇒ today's fail-closed warmup behaviour.
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert rig.sender.created == []
