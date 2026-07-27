"""PER-LOOP PROGRESS ledger — the half of out-of-process liveness that means
"WORKING", paired with the heartbeat's "ALIVE" (2026-07-26 20:12Z false kill).

Why this exists
---------------
Before this module the bot had ONE liveness signal: ``heartbeat.txt``, beaten
from inside the maintenance loop, i.e. by the same task that also does the
loop's WORK. That conflates two different questions:

  * is the process / event loop still breathing?  ("alive")
  * are the loops that matter still making progress? ("working")

Conflating them is wrong in BOTH directions:

  * FALSE POSITIVE (what killed a healthy bot at 2026-07-26T20:12:54Z): an
    end-of-game lifecycle wave quarantined 11 markets in one tick while a burst
    of resting quotes was withdrawn against an exchange that answered HTTP 404
    to 63 deletes over 27s. The event loop never stalled (max inter-log gap in
    the window: 0.70s; 559 RFQs priced, 3 fills) — but the maintenance loop sat
    in a non-beating await for ~30s, the beat aged past ``heartbeat_timeout_s``
    and the supervisor emergency-cancelled 27 resting quotes on a bot that was
    not wedged.
  * FALSE NEGATIVE (the reason "just beat from a dedicated task" is a NO-SHIP on
    its own): a dedicated beater proves only that the event loop is scheduling.
    A quote loop deadlocked on a lock, or a maintenance loop stuck forever in a
    hung await, would keep the process breathing and the beat fresh FOREVER. The
    supervisor would never fire again.

So: the heartbeat is beaten by a dedicated task whose ONLY job is to prove the
event loop schedules (``Heartbeat`` in ``risk/heartbeat.py``), and EVERY loop
that matters publishes a last-progress timestamp here. The supervisor escalates
on EITHER signal going stale. Detection is not removed — it is split in two and
made strictly more specific: a stalled loop is now named in the kill reason.

Thresholds: no hand-set numbers
-------------------------------
A loop's stall bound is DERIVED, never configured per loop:

    stall_after_s = wedge_timeout_s + interval_s

``wedge_timeout_s`` is the operator's existing single wedge-tolerance policy
anchor (``supervisor.heartbeat_timeout_s``); ``interval_s`` is the loop's OWN
declared cadence, so a loop that legitimately ticks slowly is not judged on a
fast loop's clock. For the maintenance loop (cadence 0.5s, timeout 30s) this
reproduces the pre-incident threshold to within its own tick — the loop that
used to carry the beat is still killed at the same age it always was.

Idle is not a stall
-------------------
Event-driven loops (the RFQ workers) block on an empty queue when the market is
quiet. That is not a wedge. A loop may register an ``idle`` predicate; while it
answers True the loop counts as progressing. So the quote path is judged
STALLED only when work is queued and it has not completed an iteration inside
its bound — no timers, no polling, nothing on the hot path.

Wire format + fail-closed
-------------------------
The publisher writes AGES (from its own monotonic clock) plus one wall stamp;
the reader adds the file's own wall age. Two processes share only wall time, and
a per-loop wall timestamp would let clock skew mask a stall — an age plus one
skew-checked file stamp cannot. Every parse failure is UNREADABLE, and the
reader LATCHES on first valid sighting: before the latch an absent file is
"bot still starting" (no escalation), after it an absent/corrupt/stale file is
WEDGED. Same latch shape as the feed-warm gate. All writes are atomic
(write-temp-then-rename, shared with the heartbeat). No secrets ever touch it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from combomaker.core.clock import Clock
from combomaker.ops.logging import get_logger
from combomaker.risk.heartbeat import _atomic_write

log = get_logger(__name__)

PROGRESS_FILENAME = "loop_progress.json"


def progress_path(data_dir: Path) -> Path:
    """The path the bot publishes and the supervisor reads. Stable filename so
    the two processes agree without passing paths between them."""
    return data_dir / PROGRESS_FILENAME


@dataclass(slots=True)
class _LoopState:
    name: str
    stall_after_s: float
    last_ns: int
    idle: Callable[[], bool] | None = None


@dataclass(slots=True)
class ProgressLedger:
    """Bot side. Loops ``mark()`` (O(1), hot-path safe); the dedicated liveness
    task ``publish()``es a snapshot to disk."""

    clock: Clock
    path: Path
    _loops: dict[str, _LoopState] = field(default_factory=dict)

    def register(
        self,
        name: str,
        *,
        interval_s: float,
        wedge_timeout_s: float,
        idle: Callable[[], bool] | None = None,
    ) -> None:
        """Declare a loop and DERIVE its stall bound from the operator's single
        wedge-tolerance anchor plus the loop's own cadence. Registering seeds
        the loop as progressing NOW, so a loop is never born stalled."""
        if interval_s < 0.0:
            raise ValueError(f"interval_s must be >= 0, got {interval_s}")
        if wedge_timeout_s <= 0.0:
            raise ValueError(f"wedge_timeout_s must be > 0, got {wedge_timeout_s}")
        self._loops[name] = _LoopState(
            name=name,
            stall_after_s=wedge_timeout_s + interval_s,
            last_ns=self.clock.monotonic_ns(),
            idle=idle,
        )

    def mark(self, name: str) -> None:
        """"I completed an iteration." One dict lookup and one integer store —
        cheap enough to sit inside the reprice sweep. An unregistered name is
        ignored (a mark is never worth raising from a worker loop)."""
        state = self._loops.get(name)
        if state is not None:
            state.last_ns = self.clock.monotonic_ns()

    def marker(self, name: str) -> Callable[[], None]:
        """A bound zero-arg mark for callbacks (the lifecycle's mid-loop hook)."""

        def _mark() -> None:
            self.mark(name)

        return _mark

    def snapshot(self) -> dict[str, object]:
        """The payload published to disk. Idle loops are counted as progressing
        at snapshot time (an empty work queue is not a wedge)."""
        now_ns = self.clock.monotonic_ns()
        loops: dict[str, object] = {}
        for state in self._loops.values():
            if state.idle is not None:
                try:
                    if state.idle():
                        state.last_ns = now_ns
                except Exception:  # noqa: BLE001 — a broken probe must not mask a stall
                    log.warning("progress_idle_probe_failed", loop=state.name, exc_info=True)
            loops[state.name] = {
                "age_s": max((now_ns - state.last_ns) / 1e9, 0.0),
                "stall_after_s": state.stall_after_s,
            }
        return {"written_at": self.clock.now().isoformat(), "loops": loops}

    def publish(self) -> None:
        """Atomically write the snapshot. Best-effort: a failed write logs and
        retries next cycle, and the file going stale is itself the fail-closed
        signal (the reader ages the ``written_at`` stamp)."""
        try:
            _atomic_write(self.path, json.dumps(self.snapshot()))
        except OSError as exc:  # pragma: no cover - disk failure path
            log.warning("progress_write_failed", path=str(self.path), error=repr(exc))


@dataclass(frozen=True, slots=True)
class StalledLoop:
    name: str
    age_s: float
    stall_after_s: float


class ProgressReader:
    """Supervisor side. Reads the ledger and names the loops that stopped.

    LATCHING: ``seen`` flips True on the first successfully-parsed read THAT
    CARRIES AT LEAST ONE LOOP. Before that a missing file — or a ledger with no
    registered loops, which is un-stallable by construction and would blind this
    axis forever — is "not established yet" (the bot may still be starting, and
    a bot built before this file existed must not be killed on sight). After
    that, a file that vanishes, corrupts, empties, or stops being refreshed is
    WEDGED — the publisher is part of the process it certifies.
    """

    # A ledger stamped implausibly far in the FUTURE is stale, not fresh — same
    # tamper/clock-jump rule (and constant) as the heartbeat reader.
    MAX_FUTURE_SKEW_S = 300.0

    def __init__(self, clock: Clock, path: Path) -> None:
        self._clock = clock
        self._path = path
        self._seen = False

    @property
    def seen(self) -> bool:
        return self._seen

    @property
    def path(self) -> Path:
        return self._path

    def _read(
        self, *, retries: int = 3, sleep: Callable[[float], None] = time.sleep
    ) -> dict[str, object] | None:
        raw: str | None = None
        for attempt in range(max(1, retries)):
            try:
                raw = self._path.read_text(encoding="utf-8").strip()
                break
            except OSError:
                if attempt < retries - 1:
                    sleep(0.01)
                    continue
                return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def file_age_s(self, payload: dict[str, object]) -> float | None:
        stamp = payload.get("written_at")
        if not isinstance(stamp, str):
            return None
        try:
            written_at = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if written_at.tzinfo is None:
            return None
        age = (self._clock.now() - written_at).total_seconds()
        if age < -self.MAX_FUTURE_SKEW_S:
            return None
        return max(age, 0.0)

    def stalled(self) -> tuple[StalledLoop, ...] | None:
        """Loops past their derived stall bound, or ``None`` when the ledger
        cannot be established. ``None`` before the latch means "not yet
        published" (no escalation); after the latch the caller MUST treat it as
        wedged — see ``wedged_detail``."""
        payload = self._read()
        if payload is None:
            return None
        loops = payload.get("loops")
        if not isinstance(loops, dict):
            return None
        file_age = self.file_age_s(payload)
        if file_age is None:
            return None
        if not loops:
            # EMPTY LEDGER = NOT ESTABLISHED, and it must never LATCH.
            # Staleness is evaluated PER LOOP, so a ledger carrying zero
            # registered loops can never read stalled at ANY age — backdate
            # ``written_at`` to 2020 and this still returned "healthy" with
            # ``seen=True``. Latching on one therefore blinds the whole
            # progress axis permanently while ``seen`` makes it look
            # established. quote_app publishes exactly this empty snapshot at
            # preflight (before any ``register()``, and again right after
            # ``_launch_supervisor``), so the supervisor reads it through the
            # whole of startup.
            #
            # WHY "don't latch" rather than "report wedged": the un-latched
            # branch is the only one that cannot FALSE-KILL. During legitimate
            # startup an empty ledger is simply "not published yet" — the same
            # no-escalation treatment an absent file already gets. Meanwhile a
            # bot that HAS registered loops can never legitimately go back to
            # empty, and if it somehow does, ``_seen`` is already True, so the
            # next read takes ``wedged_detail``'s unreadable branch and DOES
            # escalate. Fail-closed after establishment, never before it.
            return None
        self._seen = True
        stalled: list[StalledLoop] = []
        for name, entry in loops.items():
            if not isinstance(entry, dict):
                return None  # corrupt row: fail-closed via the unreadable branch
            try:
                # Total staleness = the age the publisher measured on its own
                # monotonic clock + how long ago it published (wall).
                age = float(entry["age_s"]) + file_age
                bound = float(entry["stall_after_s"])
            except (KeyError, TypeError, ValueError):
                return None
            if age > bound:
                stalled.append(StalledLoop(name=str(name), age_s=age, stall_after_s=bound))
        stalled.sort(key=lambda s: s.age_s, reverse=True)
        return tuple(stalled)

    def wedged_detail(self) -> str | None:
        """The supervisor's verdict from the PROGRESS axis, or ``None`` if the
        loops are all progressing (or the ledger was never established).

        Fail-closed after the latch: an unreadable/absent/future-skewed ledger
        from a bot that HAS published one reads as wedged."""
        stalled = self.stalled()
        if stalled is None:
            if not self._seen:
                return None
            return "loop progress ledger unreadable"
        if not stalled:
            return None
        worst = stalled[0]
        names = ", ".join(
            f"{s.name} age={s.age_s:.1f}s > {s.stall_after_s:.1f}s" for s in stalled
        )
        return f"loop stalled: {names}" if len(stalled) > 1 else (
            f"loop stalled: {worst.name} age={worst.age_s:.1f}s > "
            f"{worst.stall_after_s:.1f}s"
        )
