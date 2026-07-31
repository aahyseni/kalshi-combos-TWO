"""Hang watchdog (tools/ops/hang_watchdog.py) — the external supervisor that
watches WORK (log + decision-flow advance), not PIDs. These tests drive the
real detection/escalation state machine with fake probes and a fake clock;
the end-to-end scratch-tree proofs live in the 2026-07-31 report.

What is protected: the 45h 7/29 failure class (PID alive, log frozen), the
boot-loop class (instant config-error exit must NOT be relaunched), and the
no-false-positive property (a lull below the derived threshold, or ANY axis
advancing, never restarts a healthy bot)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import tools.ops.hang_watchdog as hw

# --------------------------------------------------------------------------
# Tape derivation
# --------------------------------------------------------------------------


def _write_json_log(path: Path, start: datetime, gaps_s: list[int]) -> None:
    """A synthetic live log: one JSON line per timestamp; consecutive lines
    separated by the given gaps."""
    t = start
    lines = [f'{{"event": "x", "ts": "{t.strftime("%Y-%m-%dT%H:%M:%S")}.000000Z"}}']
    for g in gaps_s:
        t = t + timedelta(seconds=g)
        lines.append(f'{{"event": "x", "ts": "{t.strftime("%Y-%m-%dT%H:%M:%S")}.000000Z"}}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_scan_log_gap_finds_longest_quiet_gap(tmp_path: Path) -> None:
    start = datetime(2026, 7, 30, 4, 0, tzinfo=UTC)
    p = tmp_path / "live_a.log"
    _write_json_log(p, start, [1, 1, 600, 1, 90, 1])
    rec = hw.scan_log_gap(p)
    assert rec["max_gap_s"] == 600
    assert rec["distinct_seconds"] == 7


def test_scan_log_gap_boot_only_log_has_no_phantom_gap(tmp_path: Path) -> None:
    """The 2026-07-31 09:00 config-error log: two human-format lines, no JSON.
    It must scan cleanly and contribute its true (tiny) gaps only."""
    p = tmp_path / "live_boot.log"
    p.write_text(
        "2026-07-31 09:00:14 [info     ] dotenv_loaded names=[...]\n"
        "config error: 1 validation error for AppConfig\n",
        encoding="utf-8",
    )
    rec = hw.scan_log_gap(p)
    assert rec["max_gap_s"] == 0


def test_derive_threshold_tape_term_and_floor(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    start = datetime(2026, 7, 30, 4, 0, tzinfo=UTC)
    # worst healthy lull 600s -> tape term 1200s beats the default floor
    _write_json_log(data / "live_20260730_0000.log", start, [1, 600, 1])
    _write_json_log(data / "live_20260730_0100.log", start, [1, 30, 1])
    result = hw.derive_threshold(tmp_path, data, lambda m: None)
    assert result["max_healthy_gap_s"] == 600
    assert result["threshold_s"] == pytest.approx(2 * 600)
    assert result["max_gap_log"] == "live_20260730_0000.log"
    # floor dominates when the tape is quiet-free (defaults: 2*(2*15+1)=62)
    for p in data.glob("live_*.log"):
        p.unlink()
    (data / hw.TAPE_CACHE).unlink()
    _write_json_log(data / "live_20260730_0200.log", start, [1, 1, 1])
    result = hw.derive_threshold(tmp_path, data, lambda m: None)
    assert result["threshold_s"] == pytest.approx(2 * (2 * 15.0 + 1.0))
    assert result["threshold_s"] > result["max_healthy_gap_s"] * 2


def test_derive_threshold_skips_active_log_and_caches(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    start = datetime(2026, 7, 30, 4, 0, tzinfo=UTC)
    _write_json_log(data / "live_done.log", start, [1, 200, 1])
    # the ACTIVE log carries a huge in-progress gap; it must not pollute the tape
    _write_json_log(data / "live_active.log", start, [1, 99999, 1])
    (data / hw.CURRENT_LOG_POINTER).write_text("data\\live_active.log\n", encoding="ascii")
    result = hw.derive_threshold(tmp_path, data, lambda m: None)
    assert result["max_healthy_gap_s"] == 200
    # cache: a second derive rescans nothing (scan log calls would be visible
    # via the log callback)
    calls: list[str] = []
    result2 = hw.derive_threshold(tmp_path, data, calls.append)
    assert result2["max_healthy_gap_s"] == 200
    assert not any("tape scan" in c for c in calls)


# --------------------------------------------------------------------------
# Watchdog state machine (fake probes, fake clock)
# --------------------------------------------------------------------------


class FakeProbes:
    def __init__(self) -> None:
        self.log = ("live.log", 100, 1)
        self.store: str | None = "2026-07-31T13:00:00+00:00"
        self.pids: list[int] | None = [4242]
        self.heartbeat = True

    def log_sig(self):
        return self.log

    def store_sig(self):
        return self.store

    def bot_pids(self):
        return self.pids

    def heartbeat_exists(self):
        return self.heartbeat


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_dog(tmp_path: Path, probes: FakeProbes, clock: Clock, threshold: float = 100.0):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    calls: list[str] = []

    dog = hw.Watchdog(
        root=tmp_path,
        data_dir=data,
        probes=probes,  # type: ignore[arg-type]
        threshold_s=threshold,
        poll_s=1.0,
        stop_cmd="STOP",
        start_cmd="START",
        monotonic=clock,
        sleep=lambda s: None,
        echo=lambda s: None,
    )

    def fake_run(cmd: str, timeout: float) -> int:
        calls.append(cmd)
        if cmd == "STOP":
            probes.pids = []  # the real stop path force-kills the tree
        if cmd == "START":
            probes.pids = [4243]  # the real start path spawns a fresh bot
        return 0

    dog._run = fake_run  # type: ignore[method-assign]
    return dog, calls


def test_healthy_advancing_log_never_escalates(tmp_path: Path) -> None:
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock)
    for i in range(50):
        clock.t += 60.0
        probes.log = ("live.log", 100 + i, i)  # log advances every poll
        assert dog.poll_once() == "ok"
    assert calls == []


def test_lull_below_threshold_is_not_a_stall(tmp_path: Path) -> None:
    """No-false-positive: freeze both axes for JUST under the threshold, then
    advance — never escalates."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    assert dog.poll_once() == "ok"
    clock.t += 99.0  # quiet, but inside the window
    assert dog.poll_once() == "ok"
    probes.log = ("live.log", 200, 2)
    assert dog.poll_once() == "ok"
    clock.t += 99.0
    assert dog.poll_once() == "ok"
    assert calls == []


def test_store_advance_alone_holds_off_stall(tmp_path: Path) -> None:
    """Progress on EITHER axis resets the stall clock — a frozen log with live
    decision flow is not a hang."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    assert dog.poll_once() == "ok"
    for i in range(10):
        clock.t += 60.0
        probes.store = f"2026-07-31T13:{i:02d}:00+00:00"  # store advances
        assert dog.poll_once() == "ok"
    assert calls == []


def test_frozen_log_pid_alive_restarts(tmp_path: Path) -> None:
    """THE 7/29 CLASS: PID alive, log + store frozen past the threshold ->
    stop, then relight through the start path."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    assert dog.poll_once() == "ok"
    clock.t += 101.0
    assert dog.poll_once() == "relit"
    assert calls == ["STOP", "START"]


def test_boot_failure_never_relights(tmp_path: Path) -> None:
    """THE 2026-07-31 09:00 CLASS: the run never reached liveness (no
    heartbeat) -> stop-sweep only, NO start, halt receipt written."""
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    dog.poll_once()
    out = dog.poll_once()  # fast path needs 2 consecutive clean zero-pid polls
    assert out == "halt_boot_loop"
    assert calls == ["STOP"]  # sweep yes, relight NO
    receipts = list((tmp_path / "data").glob("WATCHDOG_HALT_*.txt"))
    assert len(receipts) == 1
    assert "never reached liveness" in receipts[0].read_text(encoding="utf-8")
    # latched: stays down loud, runs nothing further
    clock.t += 10_000.0
    assert dog.poll_once() == "halted"
    assert calls == ["STOP"]


def test_two_short_relights_latch_flap(tmp_path: Path) -> None:
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    assert dog.poll_once() == "ok"
    # healthy for a long spell, then hang -> relight 1
    clock.t += 500.0
    probes.log = ("live.log", 500, 5)
    assert dog.poll_once() == "ok"
    clock.t += 101.0
    assert dog.poll_once() == "relit"
    # run 1 hangs immediately (one re-sighting poll, then zero progress)
    assert dog.poll_once() == "ok"
    clock.t += 101.0
    assert dog.poll_once() == "relit"
    # run 2 hangs immediately too -> flap latch, NOT a third relight
    assert dog.poll_once() == "ok"
    clock.t += 101.0
    assert dog.poll_once() == "halt_flap"
    assert calls == ["STOP", "START", "STOP", "START", "STOP"]
    state = json.loads((tmp_path / "data" / hw.STATE_FILE).read_text(encoding="utf-8"))
    assert state["halt"] is not None


def test_long_healthy_runs_between_hangs_keep_relighting(tmp_path: Path) -> None:
    """A hang after a LONG healthy run is a one-off, not a flap — the watchdog
    keeps restoring uptime."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    for i in range(4):
        # healthy span well beyond one detection window
        for j in range(5):
            clock.t += 60.0
            probes.log = ("live.log", 1000 * i + j, i * 10 + j)
            assert dog.poll_once() == "ok"
        clock.t += 101.0
        assert dog.poll_once() == "relit"
    assert calls.count("START") == 4


def test_start_refusal_retries_once_then_latches(tmp_path: Path) -> None:
    """2026-07-31 17:34 ET: a start refusal gets ONE full re-sweep + ONE retry
    before latching; a refusal that survives the clean re-sweep (human-gated
    KILL, real guard) is authoritative and latches exactly as before."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)

    def refuse(cmd: str, timeout: float) -> int:
        calls.append(cmd)
        if cmd == "STOP":
            probes.pids = []
        return 1 if cmd == "START" else 0

    dog._run = refuse  # type: ignore[method-assign]
    assert dog.poll_once() == "ok"  # first sighting of the signatures
    clock.t += 101.0
    assert dog.poll_once() == "halt_start_refused"
    assert calls == ["STOP", "START", "STOP", "START"]  # re-sweep + one retry


def test_start_refusal_cured_by_resweep_relights(tmp_path: Path) -> None:
    """The 17:34 cure at the unit level: the guard refuses while a survivor of
    the first sweep is present; the re-sweep clears it; the retry relights."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    survivor = {"present": True}

    def run(cmd: str, timeout: float) -> int:
        calls.append(cmd)
        if cmd == "STOP":
            probes.pids = []
            if calls.count("STOP") >= 2:
                survivor["present"] = False  # the FULL re-sweep clears it
            return 0
        return 1 if survivor["present"] else 0  # guard refuses on the survivor

    dog._run = run  # type: ignore[method-assign]
    assert dog.poll_once() == "ok"  # first sighting of the signatures
    clock.t += 101.0
    assert dog.poll_once() == "relit"
    assert calls == ["STOP", "START", "STOP", "START"]
    assert dog._halt is None  # no latch — recovery succeeded


def test_stop_failure_latches_loud(tmp_path: Path) -> None:
    """If the stop path cannot kill the hung tree, a human is needed — no
    relight over live PIDs."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)

    def stop_leaves_pids(cmd: str, timeout: float) -> int:
        calls.append(cmd)
        return 0  # claims success but the PIDs survive

    dog._run = stop_leaves_pids  # type: ignore[method-assign]
    assert dog.poll_once() == "ok"  # first sighting of the signatures
    clock.t += 101.0
    assert dog.poll_once() == "halt_stop_failed"
    assert calls == ["STOP"]


def test_probe_failure_is_not_an_exit(tmp_path: Path) -> None:
    """A broken process probe (None) must never fabricate a process-gone fast
    path — only clean zero-pid probes count."""
    probes, clock = FakeProbes(), Clock()
    probes.pids = None
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    for _ in range(10):
        clock.t += 10.0
        assert dog.poll_once() == "ok"
    assert calls == []


def test_operator_start_gets_fresh_episode(tmp_path: Path) -> None:
    """start_all.ps1 (non -Auto) deletes watchdog_state.json; a NEW watchdog
    over the same tree then starts un-latched."""
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, _ = make_dog(tmp_path, probes, clock, threshold=100.0)
    dog.poll_once()
    assert dog.poll_once() == "halt_boot_loop"
    # a restarted watchdog inherits the latch from disk
    dog2, _ = make_dog(tmp_path, probes, clock, threshold=100.0)
    assert dog2.poll_once() == "halted"
    # ...until the operator start purges the state file (start_all.ps1 hygiene)
    (tmp_path / "data" / hw.STATE_FILE).unlink()
    probes.heartbeat = True
    probes.pids = [4242]
    probes.log = ("live.log", 1, 1)
    dog3, _ = make_dog(tmp_path, probes, clock, threshold=100.0)
    assert dog3.poll_once() == "ok"
