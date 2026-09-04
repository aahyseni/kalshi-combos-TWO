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
  P4 flap_backoff      — relights that immediately re-hang: NO latch (the
                         2026-08-06 ruling), backoff = threshold x streak,
                         relights keep coming.
  P5 start_refused_retry — the EXACT 2026-07-31 17:34 ET class: app dead,
                         helper processes alive, the start guard refuses ->
                         the watchdog now does ONE full re-sweep + ONE retry
                         and the relight SUCCEEDS (no latch).
  P6 kill_still_refuses — a human-gated KILL refusal survives the re-sweep:
                         start refuses BOTH times -> latch, receipt, stays
                         down (fail-safe posture unchanged).
  P7 ps1_guard_parity  — the SHIPPED stop_all/start_all predicate text,
                         extracted from the .ps1 files and evaluated against
                         the 17:34 process table: the stop sweep takes the
                         whole stack except the watchdog tree; the -Auto
                         start guard no longer refuses on the watchdog's venv
                         shim; a REAL leftover bot python still refuses.
  P8 network_boot_death — the 2026-08-27 02:13 ET class (real corpse log):
                         DNS death before any heartbeat -> NO latch, backoff
                         x streak, reach probe (fail, fail, ok), relight.
  P9 maintenance_boot_death — the 2026-08-06 03:13 ET class (real corpse
                         log): exchange 503s before any heartbeat -> NO
                         latch, backoff, probe (fail, ok), relight.

Run:  .venv/Scripts/python.exe -m tools.ops.prove_watchdog
"""

from __future__ import annotations

import json
import re
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


def run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles: int, reach_cmd=None):
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
    if reach_cmd is not None:
        cmd += ["--reach-cmd", f'cmd /c "{reach_cmd}"']
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
    # 2026-09-04 build: the latch now names the death CLASS — a config-error
    # boot is CONFIG_CODE (it still latches; only NETWORK deaths retry, P8).
    check(
        "death class CONFIG_CODE: config error" in str(state.get("halt", {}).get("reason", "")),
        "latch reason names the death class (CONFIG_CODE)",
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


def p4_flap_backoff(failures: list[str]) -> None:
    # REWRITTEN 2026-09-04 (build watchdog-network-boot-death): this proof
    # still pinned the pre-2026-08-06 permanent flap LATCH ("exactly two
    # relights then a final stop", "two consecutive relights" reason) and had
    # CRASHED on ``state["halt"] is None`` ever since 79d44ba converted the
    # flap streak to backoff-and-retry-forever (measured on the unmodified
    # tree before this build: AttributeError at the old check). It now pins
    # the SHIPPED 8/6 semantics: no latch, no receipt, backoff = threshold x
    # streak (5s x 2 = 10s here), relights keep coming.
    print("P4 flap_backoff (relit runs re-hang immediately: back off x streak, retry forever)")
    root, data, pid_file, actions, stop_cmd = build_scratch("p4", heartbeat=True, pids=[4242])
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=False)
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=35)
    acts = actions_list(actions)
    state = read_state(data)
    receipts = list(data.glob("WATCHDOG_HALT_*.txt"))
    check(
        acts.count("start") >= 3 and acts[:4] == ["stop", "start", "stop", "start"],
        f"keeps relighting through the flap streak (got {acts})",
        failures,
    )
    check(state.get("halt") is None, "NO flap latch (2026-08-06 ruling)", failures)
    check(not receipts, "no halt receipt", failures)
    check(
        "flap streak 2: backing off 10s" in out.stdout,
        "backoff = threshold(5s) x streak(2) named in the log",
        failures,
    )
    check(
        all(r.get("short_run") for r in state.get("relights", [])[:-1]),
        "every completed relight recorded as short_run (the streak input)",
        failures,
    )


def p5_start_refused_retry(failures: list[str]) -> None:
    print("P5 start_refused_retry (EXACT 17:34 ET class: app dead + helpers alive)")
    root, data, pid_file, actions, _default_stop = build_scratch(
        "p5", heartbeat=True, pids=[]
    )
    # HELPERS ALIVE: a marker the FIRST stop pass leaves behind (the 17:34
    # incomplete sweep) and only a SECOND, full re-sweep clears.
    helpers = root / "helpers.txt"
    helpers.write_text("python.exe tools\\ops\\hang_watchdog.py run (shim)\n", "ascii")
    swept_flag = root / "swept.flag"
    stop_cmd = root / "fake_stop_p5.cmd"
    stop_cmd.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'echo stop >> "{actions}"',
                f'type nul > "{pid_file}"',
                f'if exist "{swept_flag}" (',
                f'  del "{helpers}"',
                ") else (",
                f'  echo swept > "{swept_flag}"',
                ")",
            ]
        )
        + "\r\n",
        encoding="ascii",
    )
    # THE REAL start_all SEMANTICS, modeled: refuse rc=1 while a helper is
    # alive (the single-instance guard), start cleanly once it is gone.
    writer = root / "writer.py"
    writer.write_text(WRITER, encoding="utf-8")
    start_cmd = root / "fake_start_p5.cmd"
    start_cmd.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'if exist "{helpers}" (',
                "  echo REFUSING TO START - the bot/prober is already running:",
                f'  echo start_refused >> "{actions}"',
                "  exit /b 1",
                ")",
                f'echo start >> "{actions}"',
                f'echo 4243 > "{pid_file}"',
                f'"{PY}" "{writer}" --detach "{data / "live_now.log"}" 60 1.0',
            ]
        )
        + "\r\n",
        encoding="ascii",
    )
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=20)
    acts = actions_list(actions)
    state = read_state(data)
    check(
        acts == ["stop", "start_refused", "stop", "start"],
        f"stop -> refused -> RE-SWEEP -> retry start (got {acts})",
        failures,
    )
    check(state.get("halt") is None, "NO latch — the relight succeeded", failures)
    check(len(state.get("relights", [])) == 1, "one relight recorded", failures)
    check(not list(data.glob("WATCHDOG_HALT_*.txt")), "no halt receipt", failures)
    check("full re-sweep, then ONE retry" in out.stdout, "retry named in the log", failures)
    check("relight complete" in out.stdout, "relight completion logged", failures)


def p6_kill_still_refuses(failures: list[str]) -> None:
    print("P6 kill_still_refuses (human-gated KILL survives the re-sweep: stays down)")
    root, data, pid_file, actions, stop_cmd = build_scratch("p6", heartbeat=True, pids=[])
    start_cmd = root / "fake_start_p6.cmd"
    # start_all's -Auto KILL refusal: rc=1 EVERY time, re-sweep or not.
    start_cmd.write_text(
        "\r\n".join(
            [
                "@echo off",
                "echo AUTO MODE: KILL requires operator review - refusing to start.",
                f'echo start_refused >> "{actions}"',
                "exit /b 1",
            ]
        )
        + "\r\n",
        encoding="ascii",
    )
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=12)
    acts = actions_list(actions)
    state = read_state(data)
    receipts = list(data.glob("WATCHDOG_HALT_*.txt"))
    check(
        acts == ["stop", "start_refused", "stop", "start_refused"],
        f"exactly ONE retry after a re-sweep, never a third start (got {acts})",
        failures,
    )
    check(
        "refused the relight twice" in str(state.get("halt", {}).get("reason", "")),
        "latch reason: refused twice (authoritative)",
        failures,
    )
    check(len(receipts) == 1, "halt receipt written", failures)
    check("STAYING DOWN" in out.stdout or "HALT LATCHED" in out.stdout, "loud", failures)


CORPSE_8_27 = REPO / "tests" / "fixtures" / "watchdog" / "live_20260827_0207_tail.log"


def p8_network_boot_death(failures: list[str]) -> None:
    """2026-09-04 build. THE 8/27 02:13 ET CLASS: the relit boot died 45s in on
    ClientConnectorDNSError (Wi-Fi flap), never wrote heartbeat.txt, and flap
    guard 1 latched — an 8-day outage. The corpse log is the REAL one
    (tests/fixtures/watchdog, verbatim). The real watchdog CLI must classify
    it NETWORK, NOT latch, wait threshold x streak (5s, 10s, 15s), consult
    the reach probe each time (unreachable, unreachable, reachable) and
    relight once the exchange answers."""
    print("P8 network_boot_death (8/27 02:13 class: DNS death pre-heartbeat -> retry, no latch)")
    root, data, pid_file, actions, stop_cmd = build_scratch("p8", heartbeat=False, pids=[])
    (data / "live_now.log").write_text(CORPSE_8_27.read_text(encoding="utf-8"), encoding="utf-8")
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=True)
    flag1, flag2 = root / "reach1.flag", root / "reach2.flag"
    reach_cmd = root / "fake_reach.cmd"
    # unreachable twice (a counter on disk), reachable from the third probe on
    reach_cmd.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'if exist "{flag2}" (',
                "  echo reachable",
                f'  echo reach_ok >> "{actions}"',
                "  exit /b 0",
                ")",
                f'if exist "{flag1}" (',
                f'  echo. > "{flag2}"',
                ") else (",
                f'  echo. > "{flag1}"',
                ")",
                "echo unreachable",
                f'echo reach_fail >> "{actions}"',
                "exit /b 1",
            ]
        )
        + "\r\n",
        encoding="ascii",
    )
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=10, reach_cmd=reach_cmd)
    acts = actions_list(actions)
    state = read_state(data)
    check(
        acts == ["stop", "reach_fail", "reach_fail", "reach_ok", "start"],
        f"stop -> probe x3 (fail, fail, ok) -> relight (got {acts})",
        failures,
    )
    check(state.get("halt") is None, "NO latch on a NETWORK boot death", failures)
    check(not list(data.glob("WATCHDOG_HALT_*.txt")), "no halt receipt", failures)
    relights = state.get("relights", [])
    check(
        len(relights) == 1
        and relights[0].get("boot_death") == "NETWORK"
        and relights[0].get("reach_waits") == 3,
        f"relight record names the class + 3 reach waits (got {relights})",
        failures,
    )
    check(
        "boot death class NETWORK (ClientConnectorDNSError)" in out.stdout,
        "the corpse's DNS death named in the log",
        failures,
    )
    check(
        all(f"wait #{i}: {5 * i}s" in out.stdout for i in (1, 2, 3)),
        "waits = threshold(5s) x streak 1, 2, 3",
        failures,
    )
    check(
        out.stdout.count("UNREACHABLE") == 2 and ": REACHABLE {" in out.stdout,
        "every probe result logged (2 unreachable, then reachable)",
        failures,
    )
    check("relight complete" in out.stdout, "relight completion logged", failures)


CORPSE_8_6 = REPO / "tests" / "fixtures" / "watchdog" / "live_20260806_0313_tail.log"


def p9_maintenance_boot_death(failures: list[str]) -> None:
    """2026-09-04 build. THE 8/6 03:13 ET CLASS: Kalshi's maintenance 503s
    killed the boot before any heartbeat (startup_reconcile_failed on
    KalshiApiError('HTTP 503 ...') -> REFUSING TO QUOTE on book_reconciled ->
    supervisor_exchange_unreachable on the same 503). The corpse log is the
    REAL one (tests/fixtures/watchdog, verbatim). The real watchdog CLI must
    classify it NETWORK (the live code's own "cannot reach exchange"), NOT
    latch, wait threshold x streak (5s, 10s), consult the reach probe each
    time (not serving, then serving) and relight."""
    print(
        "P9 maintenance_boot_death (8/6 03:13 class: exchange 503 pre-heartbeat -> "
        "retry, no latch)"
    )
    root, data, pid_file, actions, stop_cmd = build_scratch("p9", heartbeat=False, pids=[])
    (data / "live_now.log").write_bytes(CORPSE_8_6.read_bytes())
    start_cmd = make_start_cmd(root, data, pid_file, actions, healthy=True)
    flag1 = root / "reach1.flag"
    reach_cmd = root / "fake_reach.cmd"
    # not serving once (a marker on disk), serving from the second probe on
    reach_cmd.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'if exist "{flag1}" (',
                "  echo reachable",
                f'  echo reach_ok >> "{actions}"',
                "  exit /b 0",
                ")",
                f'echo. > "{flag1}"',
                "echo HTTP 503",
                f'echo reach_fail >> "{actions}"',
                "exit /b 1",
            ]
        )
        + "\r\n",
        encoding="ascii",
    )
    out = run_watchdog(root, pid_file, stop_cmd, start_cmd, cycles=10, reach_cmd=reach_cmd)
    acts = actions_list(actions)
    state = read_state(data)
    check(
        acts == ["stop", "reach_fail", "reach_ok", "start"],
        f"stop -> probe x2 (not serving, ok) -> relight (got {acts})",
        failures,
    )
    check(state.get("halt") is None, "NO latch on the maintenance boot death", failures)
    check(not list(data.glob("WATCHDOG_HALT_*.txt")), "no halt receipt", failures)
    relights = state.get("relights", [])
    check(
        len(relights) == 1
        and relights[0].get("boot_death") == "NETWORK"
        and relights[0].get("reach_waits") == 2,
        f"relight record names the class + 2 reach waits (got {relights})",
        failures,
    )
    check(
        "boot death class NETWORK (startup_reconcile_failed: HTTP 503)" in out.stdout,
        "the corpse's 503 reconcile death named in the log",
        failures,
    )
    check(
        all(f"wait #{i}: {5 * i}s" in out.stdout for i in (1, 2)),
        "waits = threshold(5s) x streak 1, 2",
        failures,
    )
    check(
        out.stdout.count("UNREACHABLE") == 1 and ": REACHABLE {" in out.stdout,
        "every probe result logged (1 not serving, then reachable)",
        failures,
    )
    check("relight complete" in out.stdout, "relight completion logged", failures)


# The 2026-07-31 17:34 ET process table, verbatim from the watchdog log +
# receipt (PIDs real; the watchdog child 41593 is the -CallerPid / -KeepPid).
_T1734 = [
    (22652, "cmd.exe",
     r'"C:\Windows\system32\cmd.exe" /k title BOT (quote mode) && '
     r".venv\Scripts\python.exe -m combomaker.ops.cli run --env prod"),
    (37500, "cmd.exe",
     r'"C:\Windows\system32\cmd.exe" /k title FILL PROBER && '
     r".venv\Scripts\python.exe tools\diagnostics\fill_prober.py"),
    (31944, "powershell.exe",
     r"powershell.exe -NoExit -ExecutionPolicy Bypass -File "
     r"tools\ops\watch_prober.ps1 -Log data\fill_prober_20260731_1723.log"),
    (31900, "powershell.exe",
     r"powershell.exe -NoExit -ExecutionPolicy Bypass -File "
     r"tools\ops\watch_main.ps1 -Log data\live_20260731_1723.log"),
    (39052, "python.exe", r".venv\Scripts\python.exe  tools\diagnostics\fill_prober.py"),
    (34844, "python.exe", r".venv\Scripts\python.exe  tools\diagnostics\fill_prober.py"),
    (41000, "cmd.exe",
     r'"C:\Windows\system32\cmd.exe" /k title HANG WATCHDOG && '
     r".venv\Scripts\python.exe tools\ops\hang_watchdog.py run"),
    # venv SHIM — the 17:34 blocker — and the real interpreter (= the caller).
    (41592, "python.exe", r".venv\Scripts\python.exe  tools\ops\hang_watchdog.py run"),
    (41593, "python.exe", r".venv\Scripts\python.exe  tools\ops\hang_watchdog.py run"),
]
_CALLER = 41593


def _eval_ours(cmdlines: list[str]) -> list[bool]:
    """Evaluate the SHIPPED Test-CombomakerOurs (ours_predicate.ps1) against
    each command line — the real predicate code, dot-sourced, not a re-model."""
    ops = REPO / "tools" / "ops"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(cmdlines, f)
        rows_path = f.name
    script = (
        f". '{ops / 'ours_predicate.ps1'}';"
        f"$rows = Get-Content -Raw '{rows_path}' | ConvertFrom-Json;"
        "foreach ($cl in $rows) {"
        " if (Test-CombomakerOurs @{CommandLine=$cl}) {'1'} else {'0'} }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
    )
    Path(rows_path).unlink(missing_ok=True)
    verdicts = [x.strip() for x in out.stdout.splitlines() if x.strip() in ("0", "1")]
    if out.returncode != 0 or len(verdicts) != len(cmdlines):
        raise RuntimeError(f"ours predicate eval failed: rc={out.returncode} {out.stderr[:400]}")
    return [v == "1" for v in verdicts]


# NON-OURS decoys (2026-07-31 adversarial gate): each of these was — or models —
# a REAL process the old keyword sweep selected for kill on the live box
# (foreign decoy python, another project's bash shells, venv analysis shells at
# the 18:35 relight). None may ever be swept or block a start.
_DECOYS = [
    (90001, "python.exe",
     r'"C:\Users\aahys\AppData\Local\Programs\Python\Python313\python.exe"'
     r' -c "import time; time.sleep(300)" --tag-combomaker-notes'),
    (90002, "python.exe",
     r"C:\Users\aahys\kct-reanchor\.venv\Scripts\python.exe -m combomaker.ops.cli"
     r" run --env prod --mode quote"),
    (90003, "bash.exe",
     r'C:\Users\aahys\polymarket-bot\GIT\bin\bash.exe -c "grep combomaker src/x.py'
     r' && python tools/ops/hang_watchdog.py"'),
    (90004, "python.exe",
     r'C:\Users\aahys\kalshi-combos-TWO\.venv\Scripts\python.exe -c'
     r' "import sqlite3  # combomaker fill_prober watch_main analysis"'),
    (90005, "powershell.exe",
     r"powershell.exe -Command Select-String combomaker"
     r" C:\Users\aahys\kalshi-combos-TWO\src\combomaker\rfq\lifecycle.py"),
]


def p7_ps1_guard_parity(failures: list[str]) -> None:
    print("P7 ps1_guard_parity (shipped predicate code vs the 17:34 table + decoys)")
    ops = REPO / "tools" / "ops"
    stop_text = (ops / "stop_all.ps1").read_text(encoding="utf-8")
    start_text = (ops / "start_all.ps1").read_text(encoding="utf-8")

    # --- both scripts must judge membership through THE shared predicate.
    for name, text in (("stop_all", stop_text), ("start_all", start_text)):
        check(
            'ours_predicate.ps1"' in text and "Test-CombomakerOurs" in text,
            f"{name}: dot-sources ours_predicate.ps1 and calls Test-CombomakerOurs",
            failures,
        )
    keep_carveout = "-not ($KeepPid -ne 0 -and $_.CommandLine -match 'hang_watchdog')" in stop_text
    check(keep_carveout, "stop_all: -KeepPid spares the watchdog tree", failures)

    # --- evaluate the REAL predicate over the 17:34 table AND the decoys.
    table = _T1734 + _DECOYS
    ours = dict(zip([pid for pid, _n, _cl in table], _eval_ours([cl for _p, _n, cl in table]),
                    strict=True))

    def swept(pid: int, cmdline: str) -> bool:
        if not ours[pid]:
            return False
        if pid == _CALLER:  # -KeepPid
            return False
        return not re.search("hang_watchdog", cmdline, re.IGNORECASE)  # carve-out

    kill = {pid for pid, _n, cl in table if swept(pid, cl)}
    check(
        not any(pid in kill for pid, _n, _cl in _DECOYS),
        f"sweep NEVER takes a non-combomaker process (decoys spared; got {sorted(kill)})",
        failures,
    )
    check(
        kill == {22652, 37500, 31944, 31900, 39052, 34844},
        f"sweep takes bot+prober windows, prober pythons AND BOTH monitors (got {sorted(kill)})",
        failures,
    )
    check(
        not any(pid in kill for pid in (41000, 41592, _CALLER)),
        "sweep keeps the whole watchdog tree (host, shim, child)",
        failures,
    )

    # --- start_all single-instance guard in -Auto (post-sweep survivors =
    # exactly the watchdog tree): the $pythons refusal set must be EMPTY.
    auto_python_exempt = re.search(
        r"\$pythons\s*=\s*@\(\$matches_all\s*\|\s*Where-Object\s*\{[^}]*-not\s*\(\$Auto\s*-and\s*\$_\.CommandLine\s*-match\s*'hang_watchdog'\)",
        start_text,
    )
    check(
        bool(auto_python_exempt),
        "start_all: -Auto exempts watchdog-matching PYTHONS (the 17:34 fix)",
        failures,
    )

    def refuses(rows: list[tuple[int, str, str]], *, auto: bool) -> list[int]:
        verdicts = dict(
            zip([p for p, _n, _cl in rows], _eval_ours([cl for _p, _n, cl in rows]), strict=True)
        )
        out = []
        for pid, pname, cl in rows:
            if not verdicts[pid]:
                continue
            if pid == _CALLER:  # -CallerPid
                continue
            if not re.match("python", pname, re.IGNORECASE):
                continue
            if auto and re.search("hang_watchdog", cl, re.IGNORECASE):
                continue
            out.append(pid)
        return out

    check(
        refuses(_DECOYS, auto=True) == [] and refuses(_DECOYS, auto=False) == [],
        "a NON-ours python matching the old keywords never blocks a start",
        failures,
    )
    survivors = [row for row in _T1734 if row[0] in (41000, 41592, _CALLER)]
    check(
        refuses(survivors, auto=True) == [],
        "-Auto guard passes over the surviving watchdog tree (shim included)",
        failures,
    )
    check(
        refuses(survivors, auto=False) == [41592],
        "operator (non-Auto) start still refuses on a live watchdog python",
        failures,
    )
    leftover_bot = survivors + [
        (99999, "python.exe",
         r".venv\Scripts\python.exe -m combomaker.ops.cli run --env prod --mode quote")
    ]
    check(
        refuses(leftover_bot, auto=True) == [99999],
        "-Auto guard STILL refuses on a genuine leftover bot python",
        failures,
    )

    # --- the scripts must still parse (a broken sweep is a broken recovery).
    for ps1 in ("stop_all.ps1", "start_all.ps1", "ours_predicate.ps1"):
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "$t=$null;$e=$null;"
                "[System.Management.Automation.Language.Parser]::ParseFile("
                f"'{ops / ps1}',[ref]$t,[ref]$e)|Out-Null;"
                "if($e.Count -gt 0){$e|ForEach-Object{Write-Host $_.Message};exit 1};exit 0",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(proc.returncode == 0, f"{ps1} parses clean ({proc.stdout.strip() or 'ok'})", failures)


def main() -> int:
    failures: list[str] = []
    started = time.monotonic()
    for proof in (
        p1_frozen_log_hang,
        p2_boot_loop,
        p3_healthy_lull,
        p4_flap_backoff,
        p5_start_refused_retry,
        p6_kill_still_refuses,
        p7_ps1_guard_parity,
        p8_network_boot_death,
        p9_maintenance_boot_death,
    ):
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
