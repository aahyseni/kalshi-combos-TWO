"""In-process metrics: counters and latency histograms for the hot path.

Deliberately minimal — no exporter dependency. Everything is single-threaded
asyncio, so plain dicts are race-free. ``snapshot()`` feeds the daily report and
periodic log lines; the accept→confirm-decision latency histogram is the single
most important series here.
"""

from __future__ import annotations

import bisect
from collections import defaultdict, deque
from dataclasses import dataclass, field

from combomaker.core.clock import Clock

_DEFAULT_BUCKETS_MS: tuple[float, ...] = (
    1, 2, 5, 10, 20, 50, 100, 200, 500, 1_000, 2_000, 3_000, 5_000,
)


@dataclass
class Histogram:
    """Latency histogram in milliseconds with fixed buckets."""

    buckets_ms: tuple[float, ...] = _DEFAULT_BUCKETS_MS
    counts: list[int] = field(default_factory=list)
    total: int = 0
    sum_ms: float = 0.0
    max_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets_ms) + 1)  # +1 = overflow bucket

    def observe(self, value_ms: float) -> None:
        idx = bisect.bisect_left(self.buckets_ms, value_ms)
        self.counts[idx] += 1
        self.total += 1
        self.sum_ms += value_ms
        self.max_ms = max(self.max_ms, value_ms)

    def quantile(self, q: float) -> float:
        """Approximate quantile: upper edge of the bucket containing it."""
        if not 0.0 < q <= 1.0:
            raise ValueError(f"quantile out of range: {q}")
        if self.total == 0:
            return 0.0
        target = q * self.total
        seen = 0
        for i, count in enumerate(self.counts):
            seen += count
            if seen >= target:
                return self.buckets_ms[i] if i < len(self.buckets_ms) else self.max_ms
        return self.max_ms


class Metrics:
    def __init__(self, clock: Clock | None = None) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, Histogram] = {}
        # Recent-window latency samples per series (monotonic-ns timestamp, ms).
        # Only populated when a clock is present — the recent-window breaker
        # sampler needs one; every other consumer uses the histogram.
        self._clock = clock
        self._recent_ms: dict[str, deque[tuple[int, float]]] = defaultdict(deque)

    def inc(self, name: str, by: int = 1) -> None:
        self._counters[name] += by

    def observe_ms(self, name: str, value_ms: float) -> None:
        if name not in self._histograms:
            self._histograms[name] = Histogram()
        self._histograms[name].observe(value_ms)
        if self._clock is not None:
            self._recent_ms[name].append((self._clock.monotonic_ns(), value_ms))

    def counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def histogram_max_ms(self, name: str) -> float | None:
        """The worst observed latency for a series, or None if never observed.
        This is the ALL-TIME max (never decays) — for reports/snapshots, NOT for
        the latency-spike breaker (a single historical slow round-trip would
        latch it forever). The breaker uses ``recent_max_ms``."""
        h = self._histograms.get(name)
        if h is None or h.total == 0:
            return None
        return h.max_ms

    def recent_max_ms(self, name: str, window_s: float) -> float | None:
        """Worst latency observed in the last ``window_s`` seconds, or None if no
        sample landed in the window. Used by the latency-spike circuit breaker
        (Phase 6): a CURRENT spike, not the all-time max — one historical slow
        confirm must not permanently latch the human-only kill switch. Requires a
        clock (fail-closed None when unmetered: no recent sample ⇒ nothing to
        judge, mirroring detect_latency_spike's no-measurement-clears contract).
        """
        if self._clock is None:
            return None
        events = self._recent_ms.get(name)
        if not events:
            return None
        cutoff = self._clock.monotonic_ns() - int(window_s * 1e9)
        while events and events[0][0] < cutoff:
            events.popleft()
        if not events:
            return None
        return max(value for _, value in events)

    def snapshot(self) -> dict[str, object]:
        return {
            "counters": dict(self._counters),
            "latencies_ms": {
                name: {
                    "count": h.total,
                    "mean": h.sum_ms / h.total if h.total else 0.0,
                    "p50": h.quantile(0.50),
                    "p95": h.quantile(0.95),
                    "p99": h.quantile(0.99),
                    "max": h.max_ms,
                }
                for name, h in self._histograms.items()
            },
        }
