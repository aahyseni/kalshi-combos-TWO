"""EVENT-LOOP LAG PROBE + SLOW-CALLBACK RECORDER (2026-09-05 reader isolation).

Why this exists
---------------
Every 2026-09-05 incident was a main-loop STALL wearing a different mask:
the exchange's code 25 ("Subscription buffer overflow" — we stopped reading),
three ``halt_confirm_timeouts`` whose nine failures were all ``HTTP 400
expired`` (the accept was SEEN late), two supervisor kills at the 60.5 s
maintenance wall. The tape could say THAT the loop stalled; nothing could say
WHICH callback held it. These two instruments close that gap and feed one
derived signal downstream (telemetry sampling under load).

``LoopLagProbe``
    A task that sleeps ``period_s`` and measures how late it woke. Lag is the
    purest loop-health number asyncio offers: a timer that fires late did so
    because the loop was running something else for that long. Per window it
    logs ``event_loop_lag`` (max/p50/p99, samples, samples over the bound) and
    feeds ``Metrics`` (``loop.lag_ms`` histogram, ``loop.lag_over_bound``).

    THE BOUND IS THE PROBE'S OWN PERIOD — no new number. A lag of one full
    period means the loop missed an ENTIRE cadence of its finest declared
    loop (the caller passes ``MAINTENANCE_TICK_INTERVAL_S``, the smallest
    interval any loop in the process declares): timers are running a whole
    tick late, i.e. the loop is behind by the loop's own definition.
    ``behind_ratio()`` = latest lag / period; ≥ 1.0 is "behind", and the
    ratio (not a flag) is what consumers derive from.

``SlowCallbackRecorder``
    Times every SYNCHRONOUS run of an asyncio callback — ``Handle._run``, the
    unit the loop cannot interrupt: one task step between two awaits, or one
    plain callback. That is exactly the quantity that blocks the reader,
    delays an accept, and ages the progress ledger; a coroutine's total
    duration (what a naive wrapper measures) includes awaited time and says
    nothing about blocking. asyncio's own debug mode measures the same thing
    but drags coroutine-origin tracking along (far too slow for a 500-frame/s
    process); this hook costs ~0.1 µs per callback (measured 2026-09-05:
    2.318 → 2.417 µs per trivial step).

    THE THRESHOLD IS DERIVED from the process's own distribution: the p99 of
    every callback duration seen so far (a log2-bucketed histogram, O(1) per
    observation), defined once the sample is large enough for a p99 to mean
    anything — ``ceil(1 / (1 - q))`` = 100 samples, a statistical fact, not a
    knob. A callback past it logs ``slow_callback`` once per name per window
    with the task name and the ``function:line`` it SUSPENDED at (the await
    that ended the blocking stretch), and each window logs
    ``slow_callbacks_window``: the names ranked by total blocked time. That
    is the line that answers "which callbacks block the reader".
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from asyncio import events as _aio_events
from collections.abc import Callable
from typing import Any

from combomaker.core.clock import Clock
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics

log = get_logger(__name__)


class BitHistogram:
    """log2-bucketed duration histogram in ns: bucket ``k`` holds durations
    with ``d.bit_length() == k`` (i.e. ``[2^(k-1), 2^k)`` ns). Observation is
    one ``bit_length`` + two increments; quantiles resolve to a factor of 2,
    which is all a threshold needs."""

    __slots__ = ("counts", "total", "max_ns")

    def __init__(self) -> None:
        self.counts = [0] * 64
        self.total = 0
        self.max_ns = 0

    def observe(self, ns: int) -> None:
        self.counts[ns.bit_length()] += 1
        self.total += 1
        if ns > self.max_ns:
            self.max_ns = ns

    def quantile_ns(self, q: float) -> int:
        """Upper edge (``2^k``) of the bucket holding the ``q`` quantile; the
        true max when it falls in the top bucket. 0 on an empty histogram."""
        if not 0.0 < q <= 1.0:
            raise ValueError(f"quantile out of range: {q}")
        if self.total == 0:
            return 0
        target = q * self.total
        seen = 0
        for k, count in enumerate(self.counts):
            seen += count
            if seen >= target:
                return min(1 << k, self.max_ns) if k else 0
        return self.max_ns

    def reset(self) -> None:
        self.counts = [0] * 64
        self.total = 0
        self.max_ns = 0


class LoopLagProbe:
    """See the module docstring. ``period_s`` is the finest cadence any loop
    in the process declares; ``window_s`` is the existing operator telemetry
    cadence (the status line) so lag lands beside it."""

    def __init__(
        self,
        clock: Clock,
        metrics: Metrics,
        *,
        period_s: float,
        window_s: float,
        recorder: SlowCallbackRecorder | None = None,
        name: str = "loop",
    ) -> None:
        if period_s <= 0.0:
            raise ValueError(f"period_s must be > 0, got {period_s}")
        if window_s <= 0.0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        self._clock = clock
        self._metrics = metrics
        self._period_s = period_s
        self._window_s = window_s
        self._recorder = recorder
        self._name = name
        self._latest_lag_s = 0.0
        self._window = BitHistogram()
        self._over_bound = 0
        self._windows_logged = 0

    @property
    def period_s(self) -> float:
        return self._period_s

    @property
    def latest_lag_s(self) -> float:
        return self._latest_lag_s

    def behind_ratio(self) -> float:
        """Latest lag as a multiple of the probe period. ``>= 1.0`` means the
        loop missed a whole cadence — the derived "behind" signal."""
        return self._latest_lag_s / self._period_s

    def observe(self, lag_s: float) -> None:
        """Record one probe sample (public so a replay harness can drive it)."""
        lag_s = max(lag_s, 0.0)
        self._latest_lag_s = lag_s
        self._window.observe(int(lag_s * 1e9))
        self._metrics.observe_ms(f"{self._name}.lag_ms", lag_s * 1e3)
        if lag_s > self._period_s:
            self._over_bound += 1
            self._metrics.inc(f"{self._name}.lag_over_bound")

    def flush_window(self) -> dict[str, Any]:
        """Log + reset the window aggregate; returns the logged payload."""
        hist = self._window
        payload: dict[str, Any] = {
            "name": self._name,
            "samples": hist.total,
            "max_lag_ms": round(hist.max_ns / 1e6, 2),
            "p50_lag_ms": round(hist.quantile_ns(0.5) / 1e6, 2),
            "p99_lag_ms": round(hist.quantile_ns(0.99) / 1e6, 2),
            "over_bound": self._over_bound,
            "bound_ms": round(self._period_s * 1e3, 1),
            "window_s": self._window_s,
        }
        log.info("event_loop_lag", **payload)
        self._windows_logged += 1
        hist.reset()
        self._over_bound = 0
        if self._recorder is not None:
            self._recorder.flush_window()
        return payload

    async def run(self) -> None:
        window_start = self._clock.monotonic_ns()
        window_ns = int(self._window_s * 1e9)
        while True:
            t0 = self._clock.monotonic_ns()
            await asyncio.sleep(self._period_s)
            elapsed_s = (self._clock.monotonic_ns() - t0) / 1e9
            try:
                self.observe(elapsed_s - self._period_s)
                if self._clock.monotonic_ns() - window_start >= window_ns:
                    window_start = self._clock.monotonic_ns()
                    self.flush_window()
            except Exception:  # instrumentation must never end its own task
                log.exception("event_loop_lag_probe_failed")


_ASYNCIO_DIR = os.path.dirname(os.path.abspath(asyncio.__file__))


def describe_handle(handle: Any) -> str:
    """Name the work a ``Handle`` ran. Task steps are attributed to the task's
    NAME plus the deepest OUR-CODE coroutine ``file:function:line`` the task
    is suspended at AFTER the step — the await that ended the blocking
    stretch (library frames under ``asyncio/`` — ``sleep``, ``Event.wait``,
    ``Queue.get`` — are skipped: they name the primitive, not the caller).
    Plain callbacks by ``__qualname__``."""
    cb = getattr(handle, "_callback", None)
    owner = getattr(cb, "__self__", None)
    if isinstance(owner, asyncio.Task):
        coro: Any = owner.get_coro()
        where = "done"
        depth = 0
        while coro is not None and depth < 64:
            frame = getattr(coro, "cr_frame", None)
            code = getattr(coro, "cr_code", None)
            if frame is None or code is None:
                break
            if not code.co_filename.startswith(_ASYNCIO_DIR):
                where = f"{os.path.basename(code.co_filename)}:{code.co_name}:{frame.f_lineno}"
            coro = getattr(coro, "cr_await", None)
            depth += 1
        return f"task:{owner.get_name()}@{where}"
    name = getattr(cb, "__qualname__", None)
    if name is None:
        name = type(cb).__name__ if cb is not None else "?"
    return f"cb:{name}"


class SlowCallbackRecorder:
    """See the module docstring. ``install()`` patches ``Handle._run`` for the
    process (idempotent; ``uninstall()`` restores it). Only handles of the
    loop the recorder was installed on are recorded — the WS reader thread
    runs its own loop through the same class and must not race these
    aggregates."""

    QUANTILE = 0.99
    # ceil(1 / (1 - q)): the smallest sample in which the q-quantile is a
    # real observation rather than the maximum — a statistical fact.
    MIN_SAMPLES = math.ceil(1.0 / (1.0 - QUANTILE))

    _installed: SlowCallbackRecorder | None = None
    _original_run: Callable[[Any], None] | None = None

    def __init__(
        self,
        clock: Clock,
        metrics: Metrics,
        *,
        name: str = "loop",
        report_top: int = 20,
    ) -> None:
        self._clock = clock
        self._metrics = metrics
        self._name = name
        self._report_top = report_top
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hist = BitHistogram()
        self._threshold_ns: int | None = None
        # per window: name -> [count, total_ns, max_ns]
        self._window: dict[str, list[int]] = {}
        self._logged_this_window: set[str] = set()
        self._recorded = 0

    @property
    def threshold_ns(self) -> int | None:
        return self._threshold_ns

    @property
    def samples(self) -> int:
        return self._hist.total

    def install(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Patch ``Handle._run`` for the process. Idempotent for the same
        recorder; a NEW recorder replaces a previously installed one (one
        process, one loop under measurement — the previous one is unhooked,
        never stacked)."""
        cls = SlowCallbackRecorder
        if cls._installed is self:
            return
        if cls._installed is not None:
            cls._installed.uninstall()
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        original = _aio_events.Handle._run
        recorder = self
        perf = time.perf_counter_ns

        def timed_run(handle: Any) -> None:
            t0 = perf()
            original(handle)
            dt = perf() - t0
            if handle._loop is recorder._loop:
                recorder._observe(handle, dt)

        cls._original_run = original
        cls._installed = self
        _aio_events.Handle._run = timed_run  # type: ignore[method-assign, assignment]

    def uninstall(self) -> None:
        cls = SlowCallbackRecorder
        if cls._installed is not self:
            return
        assert cls._original_run is not None
        _aio_events.Handle._run = cls._original_run  # type: ignore[method-assign, assignment]
        cls._installed = None
        cls._original_run = None

    def _observe(self, handle: Any, dt_ns: int) -> None:
        hist = self._hist
        hist.observe(dt_ns)
        threshold = self._threshold_ns
        if threshold is None:
            if hist.total >= self.MIN_SAMPLES:
                threshold = self._threshold_ns = hist.quantile_ns(self.QUANTILE)
            else:
                return
        if dt_ns <= threshold:
            return
        # Slow path: rare by construction (1% of callbacks at most, by the
        # threshold's definition), so attribution can afford the frame walk.
        try:
            name = describe_handle(handle)
        except Exception:  # pragma: no cover - attribution must never raise
            name = "?"
        entry = self._window.get(name)
        if entry is None:
            self._window[name] = [1, dt_ns, dt_ns]
        else:
            entry[0] += 1
            entry[1] += dt_ns
            if dt_ns > entry[2]:
                entry[2] = dt_ns
        self._recorded += 1
        self._metrics.inc(f"{self._name}.slow_callbacks")
        if name not in self._logged_this_window:
            self._logged_this_window.add(name)
            log.warning(
                "slow_callback",
                callback=name,
                duration_ms=round(dt_ns / 1e6, 2),
                threshold_ms=round(threshold / 1e6, 3),
            )

    def refresh_threshold(self) -> int | None:
        """Re-derive the p99 from everything seen so far (called per window
        so the threshold tracks the process, never a boot burst forever)."""
        if self._hist.total >= self.MIN_SAMPLES:
            self._threshold_ns = self._hist.quantile_ns(self.QUANTILE)
        return self._threshold_ns

    def top_blockers(self, limit: int | None = None) -> list[dict[str, Any]]:
        """The current window's slow callbacks ranked by total blocked time."""
        rows = [
            {
                "callback": name,
                "count": count,
                "total_ms": round(total / 1e6, 2),
                "max_ms": round(mx / 1e6, 2),
            }
            for name, (count, total, mx) in self._window.items()
        ]
        rows.sort(key=lambda r: float(str(r["total_ms"])), reverse=True)
        return rows[: (limit if limit is not None else self._report_top)]

    def flush_window(self) -> list[dict[str, Any]]:
        """Log the window's ranking (``slow_callbacks_window``) and reset the
        per-window aggregates; the cumulative histogram (the threshold's
        source) is kept."""
        rows = self.top_blockers()
        threshold = self.refresh_threshold()
        log.info(
            "slow_callbacks_window",
            name=self._name,
            threshold_ms=round(threshold / 1e6, 3) if threshold is not None else None,
            callbacks_seen=self._hist.total,
            slow_total=sum(int(r["count"]) for r in rows),
            p50_ms=round(self._hist.quantile_ns(0.5) / 1e6, 3),
            max_ms=round(self._hist.max_ns / 1e6, 2),
            top=rows,
        )
        self._window = {}
        self._logged_this_window = set()
        return rows
