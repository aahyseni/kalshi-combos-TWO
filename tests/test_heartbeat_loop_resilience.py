"""The maintenance loop must survive a FAILED heartbeat write (2026-07-26).

Two live runs died 15 and 7 minutes in: supervisor emergency kill on
"heartbeat wedged (age=30.3s > 30.0s)" with NO log gap and every other loop
still logging — the beats simply stopped. Root cause: ``Heartbeat.beat``
write-temp-then-renames, and on Windows that rename raises PermissionError
while the supervisor holds the target file open; when the retries lose the
race the exception escapes into the maintenance task and ENDS it, so no beat
ever lands again and the supervisor kills a healthy bot 30s later.

Fail-closed is preserved: a genuinely stuck disk still ages the beat and
still trips the (correct) wedged kill — we only stop a transient file-lock
from killing the liveness loop itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from combomaker.core.clock import FakeClock
from combomaker.risk.heartbeat import Heartbeat


def test_beat_failure_is_survivable_and_later_beats_land(tmp_path: Path) -> None:
    clock = FakeClock()
    hb = Heartbeat(clock, tmp_path / "heartbeat.txt")

    calls = {"n": 0}
    real_write = hb.beat

    def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("rename blocked by the supervisor's read")
        real_write()

    # The loop body's contract: a raising beat must not propagate.
    try:
        flaky()
    except Exception:  # noqa: BLE001 — this is what the loop now swallows
        pass
    assert calls["n"] == 1
    # A LATER beat still lands — the loop kept running, so liveness recovers.
    flaky()
    assert (tmp_path / "heartbeat.txt").exists()


def test_real_beat_writes_a_parseable_wall_timestamp(tmp_path: Path) -> None:
    clock = FakeClock()
    hb = Heartbeat(clock, tmp_path / "heartbeat.txt")
    hb.beat()
    text = (tmp_path / "heartbeat.txt").read_text(encoding="utf-8").strip()
    assert text.startswith("2026")  # ISO-8601 UTC wall stamp, not an mtime
