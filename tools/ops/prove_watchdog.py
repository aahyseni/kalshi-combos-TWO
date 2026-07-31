"""EXECUTED PROOFS for the hang watchdog (tools/ops/hang_watchdog.py).

Each proof builds a SCRATCH tree (synthetic tape, fake stop/start scripts,
fake process probe, real sqlite decisions store) and runs the REAL watchdog
CLI against it — the same code path the live tree gets, with only the
documented test seams overridden. Nothing here touches the live tree, the
live store, or Kalshi.

  P1 frozen_log_hang   — the 7/29 class: PID alive, log+store frozen -> the
                         watchdog stops the tree and relights it; the relit
                         (healthy) run is then left alone.
  P2 boot_loop         — the 2026-07-31 09:00 class: bot exits instantly at
                         boot (no heartbeat ever) -> NO relight at all, halt
                         receipt, stays down loud.
  P3 healthy_lull      — log quiet just inside the derived threshold
                         (overnight-lull shape) -> zero actions; and a frozen
                         log with LIVE decision flow (store axis) -> zero
                         actions.
  P4 flap_refusal      — relights that immediately re-hang: exactly two are
                         attempted, then the flap latch stays down loud.

Run:  .venv/Scripts/python.exe -m tools.ops.prove_watchdog
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WATCHDOG = REPO / "tools" / "ops" / "hang_watchdog.py"
PY = Path(sys.executable)

WRITER = r"""
import subprocess, sys, time

if sys.argv[1] == "--detach":
    # Re-spawn fully detached with DEVNULL handles so the long-lived writer
    # never inherits (and holds open) the watchdog's captured stdout pipe.
    subprocess.Popen(
        [sys.executable, __file__] + sys.argv[2:],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    )
    sys.exit(0)

path, n, interval = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
for i in range(n):
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"event": "work", "n": %d}\n' % i)
    time.sleep(interval)
"""

STORE_UPDATER = r"""
import sqlite3, sys, time
from datetime import datetime, timezone
db, n, interval = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
for i in range(n):
    con = sqlite3.connect(db, timeout=5)
    con.execute("insert into decisions(at, kind) values(?, 'no_quote')",
                (datetime.now(timezone.utc).isoformat(),))
    con.commit(); con.close()
    time.sleep(interval)
"""


def build_scratch(name: str, *, heartbeat: bool, pids: list[int], boot_only_log: bool = False):
    """A scratch tree with: a synthetic completed tape (max healthy gap 2s),
    a scratch supervisor config (floor 2*(2*1+0.5)=5s -> threshold 5s), the
    active log + pointer, a decisions store, fake pid/stop/start plumbing."""
    root = Path(tempfile.mkdtemp(prefix=f"wdproof_{name}_"))
    data = root / "data"
    data.mkdir()
    (root / "config").mkdir()
    (root / "config" / "scratch.yaml").write_text(
        "supervisor:\n  heartbeat_timeout_s: 1.0\n  poll_interval_s: 0.5\n", encoding="utf-8"
    )
    # completed tape: two runs, longest legitimate quiet gap = 2s
    t0 = datetime(2026, 7, 30, 4, 0, tzinfo=UTC)
    for fname, gaps in (("live_20260730_0400.log", [1, 2, 1]), ("live_20260730_0500.log", [1, 1])):
        t = t0
        lines = []
        for g in [0] + gaps:
            t += timedelta(seconds=g)
            stamp = t.strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f'{{"event": "x", "ts": "{stamp}.000000Z"}}')
        (data / fname).write_text("\n".join(lines) + "\n", encoding="utf-8")
    # active run
    active = data / "live_now.log"
    if boot_only_log:
        active.write_text(
            "2026-07-31 09:00:14 [info     ] dotenv_loaded names=[...]\n"
            "config error: 1 validation error for AppConfig\n",
            encoding="utf-8",
        )
    else:
        active.write_text('{"event": "quote_app_starting"}\n', encoding="utf-8")
    (data / "CURRENT_LOG.txt").write_text("data\\live_now.log\r\n", encoding="ascii")
    if heartbeat:
        (data / "heartbeat.txt").write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    # decisions store (schema mirrors ops/persistence.py's decisions table)
    db = data / "scratch.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "create table decisions (id integer primary key autoincrement,"
        " at text not null, kind text not null)"
    )
    con.execute(
        "insert into decisions(at, kind) values(?, 'no_quote')",
        (datetime.now(UTC).isoformat(),),
    )
    con.commit()
    con.close()
    # fake process probe + stop/start
    pid_file = data / "pids.txt"
    pid_file.write_text("".join(f"{p}\n" for p in pids), encoding="ascii")
    actions = root / "actions.log"
    stop_cmd = root / "fake_stop.cmd"
    stop_cmd.write_text(
        f'@echo off\r\necho stop >> "{actions}"\r\ntype nul > "{pid_file}"\r\n', encoding="ascii"
    )
    return root, data, pid_file, actions, stop_cmd


def make_start_cmd(root: Path, data: Path, pid_file: Path, actions: Path, *, healthy: bool):
    start_cmd = root / "fake_start.cmd"
    writer = root / "writer.py"
    writer.write_text(WRITER, encoding="utf-8")
    lines = [
        "@echo off",
        f'echo start >> "{actions}"',
        f'echo 4243 > "{pid_file}"',
    ]
    if healthy:
        # --detach: the writer re-spawns itself DETACHED with DEVNULL handles
        # (a cmd `start /b` child still inherits the watchdog's captured
        # stdout pipe and holds it open — measured: the start command then
        # blocks until the writer exits, starving the relit run of progress).
        lines.append(f'"{PY}" "{writer}" --detach "{data / "live_now.log"}" 60 1.0')
    start_cmd.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return start_cmd


def run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles: int):
    cmd = [
        str(PY),
        str(WATCHDOG),
        "run",
        "--root",
        str(root),
        "--poll-s",
        "1",
        "--max-cycles",
        str(cycles),
        "--probe-cmd",
        f'cmd /c type "{pid_file}"',
        "--stop-cmd",
        f'cmd /c "{stop_cmd}"',
        "--start-cmd",
        f'cmd /c "{start_cmd}"',
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=cycles * 3 + 120)
    return out


def read_state(data: Path):
    p = data / "watchdog_state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def actions_list(actions: Path) -> list[str]:
    if not actions.exists():
        return []
    return [x.strip() for x in actions.read_text(encoding="ascii").splitlines() if x.strip()]


def check(cond: bool, msg: str, failures: list[str]) -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {msg}")
    if not cond:
        failures.append(msg)


def p1_frozen_log_hang(failures: list[str]) -> None:
    print("P1 frozen_log_hang (the 7/29 class: PID alive, log frozen)")
    root, data, pid_file, actions, stop_cmd = build_scratch("p1", heartbeat=True, pids=[4242])
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=True)
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=20)
    acts = actions_list(actions)
    state = read_state(data)
    check(acts == ["stop", "start"], f"exactly one stop+start (got {acts})", failures)
    check(state.get("halt") is None, "no halt latched (relit run stayed healthy)", failures)
    check(len(state.get("relights", [])) == 1, "one relight recorded", failures)
    check("STALL ESCALATION (hung_process)" in out.stdout, "hang named in the log", failures)
    check(not list(data.glob("WATCHDOG_HALT_*.txt")), "no halt receipt", failures)


def p2_boot_loop(failures: list[str]) -> None:
    print("P2 boot_loop (the 2026-07-31 09:00 class: instant config-error exit)")
    root, data, pid_file, actions, stop_cmd = build_scratch(
        "p2", heartbeat=False, pids=[], boot_only_log=True
    )
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=True)
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=10)
    acts = actions_list(actions)
    state = read_state(data)
    receipts = list(data.glob("WATCHDOG_HALT_*.txt"))
    check(acts == ["stop"], f"stop-sweep only, NO relight (got {acts})", failures)
    check(state.get("halt") is not None, "halt latched", failures)
    check(
        "never reached liveness" in str(state.get("halt", {}).get("reason", "")),
        "latch reason names the liveness proof",
        failures,
    )
    check(len(receipts) == 1, "halt receipt written", failures)
    check("STAYING DOWN" in out.stdout or "HALT LATCHED" in out.stdout, "loud", failures)


def p3_healthy_lull(failures: list[str]) -> None:
    print("P3a healthy_lull (log quiet just inside the threshold — overnight shape)")
    root, data, pid_file, actions, stop_cmd = build_scratch("p3a", heartbeat=True, pids=[4242])
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=True)
    writer = root / "writer.py"
    # a lull-shaped writer: one line every 4s < threshold 5s
    w = subprocess.Popen([str(PY), str(writer), str(data / "live_now.log"), "10", "4.0"])
    try:
        run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=25)
    finally:
        w.wait(timeout=60)
    acts = actions_list(actions)
    state = read_state(data)
    check(acts == [], f"zero actions on a healthy lull (got {acts})", failures)
    check(state.get("halt") is None and not state.get("relights"), "no state mutations", failures)

    print("P3b store_axis (log frozen, decision flow alive)")
    root, data, pid_file, actions, stop_cmd = build_scratch("p3b", heartbeat=True, pids=[4242])
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=True)
    upd = root / "store_updater.py"
    upd.write_text(STORE_UPDATER, encoding="utf-8")
    u = subprocess.Popen([str(PY), str(upd), str(data / "scratch.sqlite3"), "15", "2.0"])
    try:
        run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=18)
    finally:
        u.wait(timeout=90)
    acts = actions_list(actions)
    check(acts == [], f"zero actions while the store advances (got {acts})", failures)


def p4_flap_refusal(failures: list[str]) -> None:
    print("P4 flap_refusal (relit runs re-hang immediately: two tries, then down)")
    root, data, pid_file, actions, stop_cmd = build_scratch("p4", heartbeat=True, pids=[4242])
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=False)
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=35)
    acts = actions_list(actions)
    state = read_state(data)
    receipts = list(data.glob("WATCHDOG_HALT_*.txt"))
    check(
        acts == ["stop", "start", "stop", "start", "stop"],
        f"exactly two relights then a final stop (got {acts})",
        failures,
    )
    check(
        "two consecutive relights" in str(state.get("halt", {}).get("reason", "")),
        "flap latch reason recorded",
        failures,
    )
    check(len(receipts) == 1, "halt receipt written", failures)
    check(out.stdout.count("relight complete") == 2, "two relights logged, not three", failures)


def main() -> int:
    failures: list[str] = []
    started = time.monotonic()
    for proof in (p1_frozen_log_hang, p2_boot_loop, p3_healthy_lull, p4_flap_refusal):
        proof(failures)
    took = time.monotonic() - started
    if failures:
        print(f"\nPROOFS FAILED ({len(failures)}) in {took:.0f}s:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\nALL PROOFS PASS in {took:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
