"""Cross-process liveness: a heartbeat file the bot writes and the external
supervisor reads, plus the ``needs_reconcile`` marker that blocks a restarted
bot from quoting until it has reconciled its book (RISK_BUILD_PLAN Phase 6).

The heartbeat is a tiny file with the last tick's monotonic-independent WALL
timestamp (ISO-8601 UTC). The bot ``beat()``s it every maintenance tick; the
supervisor ``read_age_s()``s it against a fake/real clock. A heartbeat older
than ``heartbeat_timeout_s`` means the bot is presumed WEDGED (crash, deadlock,
GIL stall, network partition) — the supervisor then emergency-cancels and writes
the KILL file. Wall time is the only clock two SEPARATE PROCESSES can share (they
have independent monotonic origins), so the heartbeat carries wall time and its
age is computed from the reader's wall clock; a large negative or positive skew
is itself treated as a stale heartbeat (fail-closed).

The ``needs_reconcile`` marker is a separate file. It is DROPPED whenever the bot
enters a state where a restart must NOT resume quoting (a hard-trip / supervisor
kill), and CLEARED only after a successful exchange-first book reconcile. Startup
checks it: present ⇒ refuse to quote until reconcile succeeds. Like the KILL file
it survives a process restart (it is on disk), so an auto-restarter can't skip it.

All writes are atomic (write-temp-then-rename) so a reader never sees a half-
written file, and every parse failure fails CLOSED (an unreadable/corrupt
heartbeat reads as INFINITELY OLD ⇒ presumed wedged; a corrupt marker reads as
PRESENT ⇒ block). No secrets ever touch these files.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from combomaker.core.clock import Clock
from combomaker.ops.logging import get_logger

log = get_logger(__name__)


def _atomic_write(path: Path, text: str) -> None:
    """Write-temp-then-rename so a concurrent reader never sees a partial file.

    ``os.replace`` is atomic on both POSIX and Windows for same-directory moves.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Heartbeat:
    """The bot's side: write a fresh wall timestamp every tick."""

    def __init__(self, clock: Clock, path: Path) -> None:
        self._clock = clock
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def beat(self) -> None:
        """Record the current wall time. Best-effort: a failed write logs and is
        retried next tick — a transient disk hiccup must not crash the hot path,
        and a genuinely stuck disk shows up to the supervisor as a stale beat
        (fail-closed) exactly as a wedged bot would."""
        try:
            _atomic_write(self._path, self._clock.now().isoformat())
        except OSError as exc:  # pragma: no cover - disk failure path
            log.warning("heartbeat_write_failed", path=str(self._path), error=repr(exc))


class HeartbeatReader:
    """The supervisor's side: read the beat's age against the reader's clock."""

    # A heartbeat timestamp far in the FUTURE (beyond this) is treated as stale,
    # not fresh: a clock jump or a tampered file must never make a wedged bot
    # look alive. The bound is generous (5 min) so ordinary sub-second skew
    # between two hosts' NTP-synced clocks never trips it.
    MAX_FUTURE_SKEW_S = 300.0

    def __init__(self, clock: Clock, path: Path) -> None:
        self._clock = clock
        self._path = path

    def read_age_s(self) -> float | None:
        """Seconds since the last beat, or ``None`` if the heartbeat is missing,
        unreadable, unparseable, or implausibly in the future. ``None`` means
        "cannot establish liveness" and the caller MUST treat it as wedged
        (fail-closed) — it is never "probably fine"."""
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        try:
            beat_at = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if beat_at.tzinfo is None:
            return None
        age = (self._clock.now() - beat_at).total_seconds()
        if age < -self.MAX_FUTURE_SKEW_S:
            # Beat is implausibly in the future — a clock jump or tamper.
            return None
        return max(age, 0.0)

    def is_wedged(self, timeout_s: float) -> bool:
        """True if the heartbeat is missing/stale beyond ``timeout_s`` (or
        unreadable). Fail-closed: an unreadable heartbeat is WEDGED."""
        age = self.read_age_s()
        return age is None or age > timeout_s


class ReconcileMarker:
    """The ``needs_reconcile`` gate: on disk so it survives a restart.

    Semantics mirror the KILL file: written by the failure path (hard-trip /
    supervisor kill), checked at startup, cleared only after a proven exchange
    reconcile. A present marker means the bot MUST NOT quote until it clears.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def is_set(self) -> bool:
        """True if the marker is present. Fail-closed: any error stat-ing the
        path is treated as PRESENT (a filesystem we can't read is one we can't
        trust to say 'no reconcile needed')."""
        try:
            return self._path.exists()
        except OSError:  # pragma: no cover - exotic FS failure
            return True

    def set(self, reason: str) -> None:
        """Drop the marker (idempotent — overwrites an existing one)."""
        try:
            _atomic_write(self._path, reason)
        except OSError as exc:  # pragma: no cover - disk failure path
            log.error("reconcile_marker_write_failed", path=str(self._path), error=repr(exc))

    def clear(self) -> None:
        """Remove the marker after a successful reconcile. Idempotent."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            log.error("reconcile_marker_clear_failed", path=str(self._path), error=repr(exc))
