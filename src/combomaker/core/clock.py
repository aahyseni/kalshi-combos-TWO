"""Injectable clock so staleness logic and confirm-window timing are testable.

Wall time (tz-aware UTC) is for timestamps we exchange with Kalshi and persist;
monotonic nanoseconds are for staleness ages, latencies, and deadlines. Never mix
the two: wall time can step, monotonic cannot.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall time, tz-aware UTC."""
        ...

    def monotonic_ns(self) -> int:
        """Monotonic nanoseconds for ages and deadlines."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class FakeClock:
    """Deterministic clock for tests; advance explicitly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._mono_ns = 1_000_000_000

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        return self._mono_ns

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)
        self._mono_ns += int(seconds * 1e9)
