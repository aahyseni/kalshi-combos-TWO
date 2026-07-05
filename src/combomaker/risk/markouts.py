"""Markout tracking: adverse-selection measurement on every fill AND every
declined confirm (dodged bullet or spurned profit — markouts tell you which).

Self-grading circularity defense (#5): markouts are computed against BOTH our
model fair and the raw Kalshi leg-mid product — a biased fair can't catch
itself, the raw mids can. Horizons default to +10s, +1m, +5m, +30m.

The snapshot provider is a sync in-memory callable (books are warm); the sink
is the persistence layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from combomaker.ops.logging import get_logger

log = get_logger(__name__)

DEFAULT_HORIZONS_S: tuple[float, ...] = (10.0, 60.0, 300.0, 1_800.0)

SnapshotProvider = Callable[[], tuple[int | None, int | None]]
"""() -> (our_fair_cc, raw_mid_product_cc) for the tracked combo, or Nones."""

MarkoutSink = Callable[..., Awaitable[None]]
"""Store.record_markout-compatible."""


@dataclass(frozen=True, slots=True)
class MarkoutSubject:
    fill_ref: str                 # fill id, or "declined:<quote_id>"
    fair_at_event_cc: int | None
    raw_mid_at_event_cc: int | None


class MarkoutTracker:
    def __init__(
        self,
        sink: MarkoutSink,
        *,
        horizons_s: tuple[float, ...] = DEFAULT_HORIZONS_S,
    ) -> None:
        self._sink = sink
        self._horizons = tuple(sorted(horizons_s))
        self._tasks: set[asyncio.Task[None]] = set()

    def track(self, subject: MarkoutSubject, provider: SnapshotProvider) -> None:
        task = asyncio.create_task(
            self._run(subject, provider), name=f"markout-{subject.fill_ref}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, subject: MarkoutSubject, provider: SnapshotProvider) -> None:
        elapsed = 0.0
        for horizon in self._horizons:
            await asyncio.sleep(horizon - elapsed)
            elapsed = horizon
            try:
                fair_now, mid_now = provider()
            except Exception:
                log.exception("markout_provider_failed", fill_ref=subject.fill_ref)
                fair_now, mid_now = None, None
            try:
                await self._sink(
                    subject.fill_ref,
                    horizon_s=horizon,
                    fair_at_fill_cc=subject.fair_at_event_cc,
                    fair_now_cc=fair_now,
                    raw_mid_at_fill_cc=subject.raw_mid_at_event_cc,
                    raw_mid_now_cc=mid_now,
                )
            except Exception:
                log.exception("markout_sink_failed", fill_ref=subject.fill_ref)

    async def drain(self) -> None:
        """Wait for in-flight markouts (tests / shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
