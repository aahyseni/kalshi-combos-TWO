"""RECORD-AFTER-PRICE FAST-LANE (throughput synthesis 2026-07-16, B6).

``handle_rfq_record_after`` moves the ``record_rfq`` tape write off the
pre-pricing critical path to AFTER pricing/dispatch. The invariant pinned here:
every RFQ that enters the pipeline is recorded EXACTLY ONCE, strictly after the
pricing path ran — on success, on a skip, and on EVERY error path (exception,
cancellation), with the exception still propagating to the worker's handler.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from combomaker.core.clock import FakeClock
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import handle_rfq_record_after
from tests.test_lifecycle import rfq


class Tape:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def handle_ok(self, _r: object) -> None:
        self.events.append("handled")

    async def record(self, _r: object) -> None:
        self.events.append("recorded")


async def test_success_records_exactly_once_after_pricing() -> None:
    tape = Tape()
    await handle_rfq_record_after(rfq(), handle=tape.handle_ok, record=tape.record)
    # Ordering is the fast-lane itself: the tape write no longer precedes pricing.
    assert tape.events == ["handled", "recorded"]


async def test_handler_exception_still_records_once_and_propagates() -> None:
    tape = Tape()

    class KalshiLikeError(RuntimeError):
        pass

    async def boom(_r: object) -> None:
        tape.events.append("handled")
        raise KalshiLikeError("pricing path exploded")

    with pytest.raises(KalshiLikeError):
        await handle_rfq_record_after(rfq(), handle=boom, record=tape.record)
    assert tape.events == ["handled", "recorded"]  # recorded despite the raise


async def test_adversarial_cancellation_mid_pricing_still_records() -> None:
    # ADVERSARIAL EDGE: the worker task is CANCELLED while pricing awaits (a
    # shutdown / task.cancel() race). The tape row must still land exactly once
    # — a cancelled RFQ must not silently vanish from the research denominator.
    tape = Tape()
    started = asyncio.Event()

    async def hangs(_r: object) -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(
        handle_rfq_record_after(rfq(), handle=hangs, record=tape.record)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tape.events == ["recorded"]


async def test_seen_at_keeps_pickup_semantics_despite_late_row(
    tmp_path: Path,
) -> None:
    """seen_at SEMANTICS (risk audit fix 2026-07-16): the fast-lane lands the
    tape row AFTER pricing, but ``rfqs.seen_at`` must still mean "worker
    pickup, pre-pricing" — the quote_app captures the pickup wall-clock before
    the handler runs and passes it through ``record_rfq(seen_at=...)``. This
    mirrors the exact quote_app wiring: a handler that dwells 2s (the pool
    deadline) must not shift the stamp. Without the pass-through, wire→pickup
    (created_ts→seen_at) inflates by the handling duration and pickup→post
    (seen_at→quote_sent.at) goes negative on the latency instruments."""
    clock = FakeClock()
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        r = rfq()
        picked_up_at = clock.now()  # captured BEFORE pricing (quote_app wiring)

        async def slow_pricing(_r: object) -> None:
            clock.advance(2.0)  # full pool-deadline dwell

        await handle_rfq_record_after(
            r,
            handle=slow_pricing,
            record=lambda x: store.record_rfq(x, source="ws", seen_at=picked_up_at),
        )
        async with store._db.execute("SELECT seen_at FROM rfqs") as cursor:  # noqa: SLF001
            rows = [row[0] async for row in cursor]
        assert rows == [picked_up_at.isoformat()]  # pickup time, NOT write time
        assert rows[0] != clock.now().isoformat()
    finally:
        await store.close()


async def test_never_double_records_on_reentrant_handler() -> None:
    # A handler that itself records (a future refactor bug) would double-write;
    # the helper contributes exactly ONE record call per invocation.
    tape = Tape()
    n = 5
    for _ in range(n):
        await handle_rfq_record_after(rfq(), handle=tape.handle_ok, record=tape.record)
    assert tape.events.count("recorded") == n
