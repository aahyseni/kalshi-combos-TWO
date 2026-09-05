"""LOOP-LAG PROBE + SLOW-CALLBACK RECORDER (2026-09-05) — unit proofs.

1. The probe measures a synchronous stall as lag; ``behind_ratio`` crosses
   1.0 exactly when the stall exceeds the probe period, and returns below
   it once the loop keeps up again. ``event_loop_lag`` is emitted per window.
2. The recorder's threshold is DERIVED (p99 of the observed callback
   durations, defined only once 100 samples exist), a blocking callback is
   attributed to its TASK NAME and suspension point, and the per-window
   ranking names it first. Install/uninstall restore ``Handle._run``.
3. The histogram's quantile is the bucket upper edge (factor-2 resolution).
"""

from __future__ import annotations

import asyncio
import time
from asyncio import events as _aio_events
from types import SimpleNamespace

import pytest
import structlog.testing

from combomaker.core.clock import SystemClock
from combomaker.ops.loop_lag import (
    BitHistogram,
    LoopLagProbe,
    SlowCallbackRecorder,
    describe_handle,
)
from combomaker.ops.metrics import Metrics


def test_bit_histogram_quantiles_are_bucket_edges() -> None:
    h = BitHistogram()
    for ns in (1, 2, 3, 4, 100, 1_000, 1_000_000):
        h.observe(ns)
    assert h.total == 7
    assert h.max_ns == 1_000_000
    assert h.quantile_ns(1.0) == 1_000_000  # top bucket resolves to the true max
    # p50 (target 3.5 samples): bit lengths 1,2,2,3,7,10,20 -> the 4th sample
    # (ns=4) sits in bucket k=3 = [4,8), whose upper edge is 8.
    assert h.quantile_ns(0.5) == 8
    assert BitHistogram().quantile_ns(0.99) == 0
    with pytest.raises(ValueError):
        h.quantile_ns(0.0)


def test_behind_ratio_is_lag_over_the_probe_period() -> None:
    """The bound is the period itself: one full cadence late == behind."""
    metrics = Metrics()
    probe = LoopLagProbe(SystemClock(), metrics, period_s=0.5, window_s=15.0)
    probe.observe(0.2)
    assert probe.behind_ratio() == pytest.approx(0.4)
    assert metrics.counter("loop.lag_over_bound") == 0
    probe.observe(0.5)  # exactly one period: not over the bound
    assert probe.behind_ratio() == pytest.approx(1.0)
    assert metrics.counter("loop.lag_over_bound") == 0
    probe.observe(2.6)
    assert probe.behind_ratio() == pytest.approx(5.2)
    assert metrics.counter("loop.lag_over_bound") == 1
    probe.observe(-0.001)  # a timer can never fire early; clamp, never negative
    assert probe.behind_ratio() == 0.0
    payload = probe.flush_window()
    assert payload["samples"] == 4
    assert payload["max_lag_ms"] == 2600.0
    assert payload["over_bound"] == 1
    assert payload["bound_ms"] == 500.0


async def test_probe_measures_a_synchronous_stall_and_recovers() -> None:
    metrics = Metrics()
    probe = LoopLagProbe(SystemClock(), metrics, period_s=0.02, window_s=60.0)
    task = asyncio.create_task(probe.run(), name="probe")
    try:
        await asyncio.sleep(0.1)  # a few clean periods
        assert probe.behind_ratio() < 1.0
        assert metrics.counter("loop.lag_over_bound") == 0
        time.sleep(0.12)  # noqa: ASYNC251 — THE STALL: 6 probe periods, nothing runs

        async def _late_sample_landed() -> None:
            while metrics.counter("loop.lag_over_bound") == 0:  # noqa: ASYNC110
                await asyncio.sleep(0.001)

        await asyncio.wait_for(_late_sample_landed(), timeout=1.0)
        await asyncio.sleep(0.1)  # loop keeps up again: the latest sample is clean
        assert probe.behind_ratio() < 1.0
        with structlog.testing.capture_logs() as logs:
            payload = probe.flush_window()
        assert payload["max_lag_ms"] >= 80.0  # the stall, minus at most one period
        assert payload["over_bound"] >= 1
        assert payload["bound_ms"] == 20.0
        assert [e["event"] for e in logs] == ["event_loop_lag"]
        assert (metrics.quantile_ms("loop.lag_ms", 1.0) or 0.0) >= 80.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_probe_rejects_non_positive_cadence() -> None:
    with pytest.raises(ValueError):
        LoopLagProbe(SystemClock(), Metrics(), period_s=0.0, window_s=1.0)
    with pytest.raises(ValueError):
        LoopLagProbe(SystemClock(), Metrics(), period_s=0.5, window_s=0.0)


async def test_recorder_derives_its_threshold_and_names_the_blocking_task() -> None:
    metrics = Metrics()
    rec = SlowCallbackRecorder(SystemClock(), metrics, name="loop")
    original = _aio_events.Handle._run
    rec.install()
    try:
        assert _aio_events.Handle._run is not original
        assert rec.threshold_ns is None  # undefined until 100 samples exist
        for _ in range(SlowCallbackRecorder.MIN_SAMPLES + 20):
            await asyncio.sleep(0)  # trivial steps: the baseline distribution
        assert rec.samples >= SlowCallbackRecorder.MIN_SAMPLES
        assert rec.threshold_ns is not None
        assert rec.threshold_ns < 20_000_000  # trivial steps: far under 20 ms

        async def blocker() -> None:
            time.sleep(0.02)  # noqa: ASYNC251 — a 20 ms synchronous stretch in a step
            await asyncio.sleep(0)

        with structlog.testing.capture_logs() as logs:
            await asyncio.create_task(blocker(), name="slow-task")
        # The p99 of trivial steps is a few µs, so the test's own step and the
        # task's completion step legitimately register too; the 20 ms block is
        # attributed to slow-task at the await that ended it (our file, not
        # asyncio's sleep), and once per name per window.
        slow = [e for e in logs if e["event"] == "slow_callback"]
        blocked = [
            e for e in slow if e["callback"].startswith("task:slow-task@test_loop_lag.py:blocker:")
        ]
        assert len(blocked) == 1
        assert blocked[0]["duration_ms"] >= 15.0
        assert metrics.counter("loop.slow_callbacks") >= 1
        top = rec.top_blockers()
        assert top[0]["callback"] == blocked[0]["callback"]  # ranked by blocked time
        assert top[0]["max_ms"] >= 15.0
        with structlog.testing.capture_logs() as logs:
            rows = rec.flush_window()
        assert rows == top
        summary = [e for e in logs if e["event"] == "slow_callbacks_window"]
        assert len(summary) == 1
        assert summary[0]["top"][0]["callback"] == blocked[0]["callback"]
        assert rec.top_blockers() == []  # window reset; histogram kept
        assert rec.samples > SlowCallbackRecorder.MIN_SAMPLES
    finally:
        rec.uninstall()
    assert _aio_events.Handle._run is original


async def test_recorder_install_replaces_and_uninstall_is_idempotent() -> None:
    a = SlowCallbackRecorder(SystemClock(), Metrics())
    b = SlowCallbackRecorder(SystemClock(), Metrics())
    original = _aio_events.Handle._run
    a.install()
    try:
        a.install()  # idempotent for the same instance
        hooked = _aio_events.Handle._run
        assert hooked is not original
        b.install()  # a new recorder REPLACES the old hook — never stacks
        assert _aio_events.Handle._run is not hooked
        assert _aio_events.Handle._run is not original
        a.uninstall()  # no longer the installed one: no effect
        assert _aio_events.Handle._run is not original
    finally:
        b.uninstall()
        b.uninstall()
    assert _aio_events.Handle._run is original


async def test_describe_handle_attributes_tasks_and_plain_callbacks() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = asyncio.Event()

    async def inner() -> None:
        started.set()
        await release.wait()

    async def outer() -> None:
        await inner()

    task = asyncio.create_task(outer(), name="named-task")
    await started.wait()
    # The loop runs a task through a step wrapper whose ``__self__`` is the
    # task (``_asyncio.TaskStepMethWrapper``); model that shape exactly.
    step = SimpleNamespace(**{"__self__": task})
    handle = SimpleNamespace(_callback=step)
    # Attribution walks to the deepest OUR-CODE coroutine (inner(), not the
    # asyncio Event.wait it is parked in): file:function:line.
    assert describe_handle(handle).startswith("task:named-task@test_loop_lag.py:inner:")
    release.set()
    await task
    assert describe_handle(handle) == "task:named-task@done"

    def plain() -> None:
        pass

    assert describe_handle(_aio_events.Handle(plain, (), loop)).endswith(".plain")
    assert describe_handle(SimpleNamespace(_callback=None)) == "cb:?"
