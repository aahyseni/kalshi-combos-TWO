"""Global kill switch: idempotent, race-safe halt of all quoting.

Anything can call ``halt()`` — daily-loss breach, error-rate spike, repeated
confirm timeouts, exchange status, clock skew, WS health, the KILL file watcher,
or a human via the CLI. The first caller wins; every subsequent halt() for any
reason is a no-op that still gets logged by the caller. Subscribers (cancel-all,
intake stop) fire exactly once per halt. Clearing is always a deliberate human
action via ``clear()``.

asyncio is single-threaded, so a plain flag is atomic between awaits; the only
subtlety is that on_halt callbacks scheduled as tasks must not re-enter halt()
in a way that double-fires subscribers — the ``_halted`` flag is set before any
callback runs, which guarantees that.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from combomaker.core.clock import Clock
from combomaker.core.reasons import ReasonCode
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

HaltCallback = Callable[["HaltEvent"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HaltEvent:
    reason: ReasonCode
    detail: str
    at_iso: str


class KillSwitch:
    def __init__(self, clock: Clock, kill_file: Path | None = None) -> None:
        self._clock = clock
        self._kill_file = kill_file
        self._halted: HaltEvent | None = None
        self._callbacks: list[HaltCallback] = []
        self._watch_task: asyncio.Task[None] | None = None

    @property
    def halted(self) -> bool:
        return self._halted is not None

    @property
    def halt_event(self) -> HaltEvent | None:
        return self._halted

    def on_halt(self, callback: HaltCallback) -> None:
        self._callbacks.append(callback)

    async def halt(self, reason: ReasonCode, detail: str = "") -> bool:
        """Trigger a halt. Returns True if this call newly halted the system."""
        if self._halted is not None:
            return False
        event = HaltEvent(reason=reason, detail=detail, at_iso=self._clock.now().isoformat())
        self._halted = event  # set BEFORE callbacks: re-entrant halt() is a no-op
        log.error("kill_switch_halt", reason=str(reason), detail=detail)
        for callback in self._callbacks:
            try:
                await callback(event)
            except Exception:
                # A failing subscriber must never prevent the others (e.g. a
                # failing metrics hook must not block cancel-all).
                log.exception("halt_callback_failed", reason=str(reason))
        return True

    def clear(self, actor: str) -> None:
        """Deliberate human reset. Never called by automation."""
        if self._halted is None:
            return
        log.warning("kill_switch_cleared", actor=actor, was=str(self._halted.reason))
        self._halted = None

    def start_kill_file_watch(self, interval_s: float = 1.0) -> None:
        if self._kill_file is None or self._watch_task is not None:
            return
        self._watch_task = asyncio.create_task(self._watch(interval_s), name="kill-file-watch")

    async def stop(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None

    async def _watch(self, interval_s: float) -> None:
        assert self._kill_file is not None
        while True:
            if self._kill_file.exists():
                await self.halt(ReasonCode.HALT_KILL_FILE, str(self._kill_file))
                return
            await asyncio.sleep(interval_s)
