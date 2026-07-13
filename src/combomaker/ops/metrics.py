"""In-process metrics: counters and latency histograms for the hot path.

Deliberately minimal — no exporter dependency. Everything is single-threaded
asyncio, so plain dicts are race-free. ``snapshot()`` feeds the daily report and
periodic log lines; the accept→confirm-decision latency histogram is the single
most important series here.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, field

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
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._histograms: dict[str, Histogram] = {}

    def inc(self, name: str, by: int = 1) -> None:
        self._counters[name] += by

    def observe_ms(self, name: str, value_ms: float) -> None:
        if name not in self._histograms:
            self._histograms[name] = Histogram()
        self._histograms[name].observe(value_ms)

    def counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def histogram_max_ms(self, name: str) -> float | None:
        """The worst observed latency for a series, or None if never observed.
        Used by the latency-spike circuit breaker (Phase 6) to sample the
        worst confirm/round-trip so far — a spike must not hide behind a good
        mean/median."""
        h = self._histograms.get(name)
        if h is None or h.total == 0:
            return None
        return h.max_ms

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
