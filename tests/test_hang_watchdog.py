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


def test_two_short_relights_back_off_and_keep_retrying(tmp_path: Path) -> None:
    """2026-08-06 operator ruling ("if bot crashes, restart, thats it"): a flap
    streak BACKS OFF (threshold x streak, capped 900s) and relights again -
    it never latches. Only a human-gated start refusal latches. The old
    permanent flap latch twice turned a bounded outage (exchange maintenance,
    a daily halt) into an unbounded one."""
    probes, clock = FakeProbes(), Clock()
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    slept: list[float] = []
    dog_time_sleep = hw.time.sleep
    hw.time.sleep = lambda s: slept.append(s)  # type: ignore[assignment]
    try:
        assert dog.poll_once() == "ok"
        clock.t += 500.0
        probes.log = ("live.log", 500, 5)
        assert dog.poll_once() == "ok"
        clock.t += 101.0
        assert dog.poll_once() == "relit"
        assert dog.poll_once() == "ok"
        clock.t += 101.0
        assert dog.poll_once() == "relit"
        # run 2 hangs immediately too -> BACKOFF then a THIRD relight
        assert dog.poll_once() == "ok"
        clock.t += 101.0
        assert dog.poll_once() == "relit"
        assert slept and slept[-1] == 200.0  # threshold(100) x streak(2)
        # never latched
        state = json.loads(
            (tmp_path / "data" / hw.STATE_FILE).read_text(encoding="utf-8")
        )
        assert state.get("halt") is None
        assert calls.count("START") == 3
        # streak of 8+ caps at 900s
        for _ in range(6):
            assert dog.poll_once() == "ok"
            clock.t += 101.0
            dog.poll_once()
        assert max(slept) <= 900.0
    finally:
        hw.time.sleep = dog_time_sleep


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


# --------------------------------------------------------------------------
# Boot-death classification + NETWORK-class retry (2026-09-04 build).
#
# THE 8/27 02:13 ET CLASS: the -Auto relit boot died 45s in on aiohttp
# ClientConnectorDNSError (getaddrinfo failed for external-api.kalshi.com,
# a Wi-Fi flap), never wrote heartbeat.txt, and flap guard 1 latched
# "relighting would boot-loop" — nothing retried once the network returned:
# an 8-day outage. The fixture is that corpse's log, verbatim (110 lines).
# --------------------------------------------------------------------------

CORPSE_8_27 = Path(__file__).parent / "fixtures" / "watchdog" / "live_20260827_0207_tail.log"

# The live watchdog's derived threshold at the 8/27 latch (the receipt's
# evidence: threshold_s 242.0) — used so the asserted waits are the real ones.
THRESHOLD_8_27 = 242.0


def _corpse_tail_text() -> str:
    return CORPSE_8_27.read_text(encoding="utf-8")


def test_classify_death_the_8_27_corpse_is_network() -> None:
    verdict = hw.classify_death(_corpse_tail_text())
    assert verdict["class"] == hw.DEATH_NETWORK
    assert verdict["marker"] == "ClientConnectorDNSError"
    assert "external-api.kalshi.com:443" in str(verdict["line"])
    assert "getaddrinfo failed" in str(verdict["line"])


@pytest.mark.parametrize(
    ("name", "tail", "marker"),
    [
        (
            "config error boot-only (the 2026-07-31 09:00 class)",
            "2026-07-31 09:00:14 [info     ] dotenv_loaded names=[...]\n"
            "config error: 1 validation error for AppConfig\n",
            "config error",
        ),
        (
            "ImportError traceback",
            '{"event": "quote_app_starting", "ts": "x"}\n'
            "Traceback (most recent call last):\n"
            '  File "cli.py", line 1, in <module>\n'
            "    import foo\n"
            "ImportError: cannot import name foo\n",
            "ImportError",
        ),
        (
            "pydantic ValidationError traceback",
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1, in <module>\n'
            "    load()\n"
            "pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig\n",
            "pydantic_core._pydantic_core.ValidationError",
        ),
        (
            "code KeyError AFTER an earlier network warning — the terminal cause wins",
            '{"event": "quote_app_starting"}\n'
            '{"error": "TimeoutError()", "event": "exchange_status_failed", "level": "warning"}\n'
            "Traceback (most recent call last):\n"
            '  File "quote_app.py", line 9, in run\n'
            '    x = d["k"]\n'
            "KeyError: 'k'\n",
            "KeyError",
        ),
        (
            "bare TimeoutError NOT on an exchange call (a pool timeout)",
            "Traceback (most recent call last):\n"
            '  File "C:\\x\\combomaker\\pricing\\pool.py", line 1, in call\n'
            "    await asyncio.wait_for(f, 2)\n"
            "TimeoutError\n",
            "TimeoutError",
        ),
        (
            "REFUSING TO QUOTE on a non-network red gate",
            '{"event": "quote_app_starting"}\n'
            "REFUSING TO QUOTE: prod go-live preflight failed — red gates: "
            "supervisor_heartbeat_established\n",
            "REFUSING TO QUOTE",
        ),
        (
            "REFUSING TO START (ProdGuardError / conventions)",
            "REFUSING TO START: conventions unverified\n",
            "REFUSING TO START",
        ),
        (
            "credentials error",
            "credentials error: KALSHI_API_KEY_ID unset\n",
            "credentials error",
        ),
    ],
)
def test_classify_death_config_and_code_corpses(name: str, tail: str, marker: str) -> None:
    verdict = hw.classify_death(tail)
    assert verdict["class"] == hw.DEATH_CONFIG_CODE, name
    assert verdict["marker"] == marker, name


@pytest.mark.parametrize(
    ("name", "tail", "marker"),
    [
        (
            "halt_data_stale (feed lost — the 8/27 01:33/01:52/01:59 deaths)",
            '{"event": "quote_app_starting"}\n'
            '{"detail": "feed rx-age unknown — cannot prove freshness", '
            '"event": "kill_switch_halt", "level": "error", "reason": "halt_data_stale"}\n'
            '{"event": "quote_app_stopped"}\n',
            "halt_data_stale",
        ),
        (
            "startup_reconcile_failed with the REST client's TimeoutError",
            '{"event": "quote_app_starting"}\n'
            '{"detail": "could not enumerate open quotes", "error": "TimeoutError()", '
            '"event": "startup_reconcile_failed", "level": "error", "phase": "enumerate"}\n'
            '{"event": "startup_reconcile_incomplete", "level": "error"}\n',
            "startup_reconcile_failed: TimeoutError",
        ),
        (
            "supervisor_exchange_unreachable (the 8/27 02:06 stall-kill shape)",
            '{"detail": "cannot reach exchange — writing KILL anyway (fail-closed)", '
            '"error": "TimeoutError()", "event": "supervisor_exchange_unreachable", '
            '"level": "error", "reason": "loop stalled: maintenance age=60.9s > 60.5s"}\n',
            "supervisor_exchange_unreachable: TimeoutError",
        ),
        (
            "exchange_status_failed carrying a DNS error repr",
            '{"error": "ClientConnectorDNSError(ConnectionKey(host=\'external-api.kalshi.com\'), '
            "gaierror(11001, 'getaddrinfo failed'))\", "
            '"event": "exchange_status_failed", "level": "warning"}\n',
            "exchange_status_failed: ClientConnectorDNSError",
        ),
        (
            "bare TimeoutError terminal WITH an exchange/aiohttp frame",
            "Traceback (most recent call last):\n"
            '  File "C:\\x\\src\\combomaker\\exchange\\rest.py", line 356, in _request\n'
            "    async with self._session.request(\n"
            '  File "C:\\x\\.venv\\Lib\\site-packages\\aiohttp\\client.py", line 856, in _request\n'
            "    resp = await handler(req)\n"
            "TimeoutError\n",
            "TimeoutError on exchange call",
        ),
        (
            "REFUSING TO QUOTE on book_reconciled after a network reconcile failure "
            "(the 8/27 boot had it survived rehydrate)",
            '{"event": "quote_app_starting"}\n'
            '{"error": "TimeoutError()", "event": "startup_reconcile_failed", '
            '"level": "error", "phase": "enumerate"}\n'
            '{"event": "startup_reconcile_incomplete"}\n'
            "REFUSING TO QUOTE: prod go-live preflight failed — red gates: book_reconciled\n",
            "startup_reconcile_failed: TimeoutError",
        ),
        (
            "ServerDisconnectedError terminal",
            "Traceback (most recent call last):\n"
            '  File "rest.py", line 356, in _request\n'
            "    async with self._session.request(\n"
            "aiohttp.client_exceptions.ServerDisconnectedError: Server disconnected\n",
            "ServerDisconnectedError",
        ),
    ],
)
def test_classify_death_network_corpses(name: str, tail: str, marker: str) -> None:
    verdict = hw.classify_death(tail)
    assert verdict["class"] == hw.DEATH_NETWORK, name
    assert verdict["marker"] == marker, name


def test_classify_death_is_scoped_to_the_newest_boot_and_unknown_otherwise() -> None:
    """A network warning from an EARLIER boot in the same tail can never name
    the cause of the newest one; no readable cause is UNKNOWN (fail-closed)."""
    tail = (
        '{"error": "ClientConnectorDNSError(...)", "event": "exchange_status_failed"}\n'
        '{"event": "quote_app_starting"}\n'
        '{"event": "pricing_stats"}\n'
    )
    assert hw.classify_death(tail)["class"] == hw.DEATH_UNKNOWN
    assert hw.classify_death("")["class"] == hw.DEATH_UNKNOWN
    assert hw.classify_death("just noise\n")["class"] == hw.DEATH_UNKNOWN


class ReachScript:
    """A scripted reach probe: pops verdicts in order, repeats the last one."""

    def __init__(self, verdicts: list[dict[str, object] | Exception]) -> None:
        self.verdicts = list(verdicts)
        self.calls = 0

    def __call__(self) -> dict[str, object]:
        self.calls += 1
        v = self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]
        if isinstance(v, Exception):
            raise v
        return v


def _plant_corpse(tmp_path: Path, text: str, name: str = "live_20260827_0207.log") -> Path:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    p = data / name
    p.write_text(text, encoding="utf-8")
    return p


def _patched_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(hw.time, "sleep", lambda s: slept.append(s))
    return slept


def test_network_boot_death_backs_off_probes_and_relights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE 8/27 02:13 CLASS, replayed at the state-machine level with the real
    corpse log and the real derived threshold (242s): the hung relit boot has
    no heartbeat -> NOT latched; wait threshold x streak (242, 484, 726 — a
    failed probe extends the streak), probe the exchange each time, relight
    once it is reachable; no receipt, no halt, the relight record names the
    class."""
    _plant_corpse(tmp_path, _corpse_tail_text())
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = [20708, 26552]  # the two hung interpreters of 8/27 02:12:59
    probes.log = ("live_20260827_0207.log", 10316, 1)
    probes.store = "2026-08-27T06:04:06.825548+00:00"
    dog, calls = make_dog(tmp_path, probes, clock, threshold=THRESHOLD_8_27)
    reach = ReachScript(
        [
            {"ok": False, "stage": "dns", "error": "gaierror(11001, 'getaddrinfo failed')"},
            {"ok": False, "stage": "http", "status": 503, "error": "HTTP 503"},
            {"ok": True, "stage": "http", "status": 200, "exchange_active": True},
        ]
    )
    dog.reach_probe = reach
    slept = _patched_sleep(monkeypatch)

    assert dog.poll_once() == "ok"  # first sighting of the signatures
    clock.t += THRESHOLD_8_27 + 3.0  # 02:08 -> 02:13: both axes quiet past 242s
    assert dog.poll_once() == "relit"

    assert calls == ["STOP", "START"]
    assert reach.calls == 3
    assert slept == [242.0, 484.0, 726.0]
    assert dog._halt is None
    assert not list((tmp_path / "data").glob("WATCHDOG_HALT_*.txt"))
    state = json.loads((tmp_path / "data" / hw.STATE_FILE).read_text(encoding="utf-8"))
    assert state["halt"] is None
    assert state["relights"][-1]["boot_death"] == hw.DEATH_NETWORK
    assert state["relights"][-1]["reach_waits"] == 3
    log = (tmp_path / "data" / hw.WATCHDOG_LOG).read_text(encoding="utf-8")
    assert "boot death class NETWORK (ClientConnectorDNSError)" in log
    assert log.count("UNREACHABLE") == 2
    assert ": REACHABLE {" in log and '"status": 200' in log
    assert "HALT LATCHED" not in log
    # forensics: the class is logged on every escalation
    assert 'corpse death class: {"class": "NETWORK", "marker": "ClientConnectorDNSError"' in log


def test_network_boot_death_streak_grows_and_never_latches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive NETWORK boot deaths (the Wi-Fi flap lasted 01:32-06:27 ET)
    grow the backoff with the streak (threshold x streak, capped 900s), never
    latch, and never wait twice per escalation (guard 2 defers to guard 1's
    wait)."""
    _plant_corpse(tmp_path, _corpse_tail_text())
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    reach = ReachScript([{"ok": True, "stage": "http", "status": 200}])
    dog.reach_probe = reach
    slept = _patched_sleep(monkeypatch)

    def one_death() -> str:
        probes.pids = []  # the relit boot dies again before its heartbeat
        dog.poll_once()
        return dog.poll_once()  # fast path: 2 clean zero-pid probes

    outcomes = [one_death() for _ in range(12)]
    assert outcomes == ["relit"] * 12
    assert calls.count("START") == 12
    assert reach.calls == 12
    assert len(slept) == 12  # exactly ONE wait per escalation (never doubled)
    assert slept[:4] == [100.0, 100.0, 200.0, 300.0]  # streak 0->1 then grows
    assert max(slept) == 900.0  # capped, like guard 2
    assert dog._halt is None
    assert not list((tmp_path / "data").glob("WATCHDOG_HALT_*.txt"))


def test_config_boot_death_still_latches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot that dies on a CONFIG/CODE traceback (ImportError here; a
    pydantic ValidationError is the same class) is a genuine boot loop:
    latch, receipt naming the class, the probe is never consulted."""
    _plant_corpse(
        tmp_path,
        '{"event": "quote_app_starting", "ts": "2026-09-04T12:00:00.000000Z"}\n'
        "Traceback (most recent call last):\n"
        '  File "C:\\x\\src\\combomaker\\ops\\cli.py", line 4, in <module>\n'
        "    from combomaker.ops.quote_app import QuoteApp\n"
        "ImportError: cannot import name 'QuoteApp' from 'combomaker.ops.quote_app'\n",
        name="live_20260904_1200.log",
    )
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    reach = ReachScript([{"ok": True}])
    dog.reach_probe = reach
    slept = _patched_sleep(monkeypatch)
    dog.poll_once()
    assert dog.poll_once() == "halt_boot_loop"
    assert calls == ["STOP"]
    assert reach.calls == 0 and slept == []
    receipts = list((tmp_path / "data").glob("WATCHDOG_HALT_*.txt"))
    assert len(receipts) == 1
    body = receipts[0].read_text(encoding="utf-8")
    assert "never reached liveness" in body
    assert "death class CONFIG_CODE: ImportError" in body
    assert dog.poll_once() == "halted"


def test_unknown_boot_death_still_latches(tmp_path: Path) -> None:
    """No readable corpse at all (no live log) is UNKNOWN: fail-closed, the
    pre-build behaviour exactly, with the class named on the receipt."""
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    dog.reach_probe = ReachScript([{"ok": True}])
    dog.poll_once()
    assert dog.poll_once() == "halt_boot_loop"
    assert calls == ["STOP"]
    receipt = next((tmp_path / "data").glob("WATCHDOG_HALT_*.txt"))
    assert "death class UNKNOWN" in receipt.read_text(encoding="utf-8")


def test_probe_crash_is_not_a_network_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that itself CRASHES (a bug) must not hold a healthy network
    down forever: one wait, logged loudly, then the relight proceeds — the
    bot's own boot is the ultimate probe (same posture as bot_pids None)."""
    _plant_corpse(tmp_path, _corpse_tail_text())
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    dog.reach_probe = ReachScript([RuntimeError("probe bug")])
    slept = _patched_sleep(monkeypatch)
    dog.poll_once()
    assert dog.poll_once() == "relit"
    assert calls == ["STOP", "START"]
    assert slept == [100.0]
    log = (tmp_path / "data" / hw.WATCHDOG_LOG).read_text(encoding="utf-8")
    assert "probe crashed — relighting anyway" in log


def test_human_gated_kill_corpse_still_latches(tmp_path: Path) -> None:
    """The 2026-08-17 contract, unchanged by this build: a corpse dead by a
    human-only-clear KILL latches before any -Auto relight (both corpse
    readers now share one tail read — this pins the reader)."""
    _plant_corpse(
        tmp_path,
        '{"event": "quote_app_starting"}\n'
        '{"detail": "KILL, human-only clear: drawdown 12% = 5 sigma", '
        '"event": "kill_switch_halt", "level": "error", "reason": "halt_hard_trip"}\n'
        '{"event": "quote_app_stopped"}\n',
        name="live_20260817_2339.log",
    )
    probes, clock = FakeProbes(), Clock()
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    dog.poll_once()
    assert dog.poll_once() == "halt_human_kill"
    assert calls == ["STOP"]
    assert dog._corpse_human_kill_reason() == "halt_hard_trip"
    receipt = next((tmp_path / "data").glob("WATCHDOG_HALT_*.txt"))
    assert "human-gated KILL (halt_hard_trip)" in receipt.read_text(encoding="utf-8")


def test_machine_kill_corpse_relights(tmp_path: Path) -> None:
    """A MACHINE kill (no 'human-only clear' in the halt detail) is the
    ordinary relight path — the reader must not over-match."""
    _plant_corpse(
        tmp_path,
        '{"event": "quote_app_starting"}\n'
        '{"detail": "feed rx-age unknown", "event": "kill_switch_halt", '
        '"level": "error", "reason": "halt_data_stale"}\n',
        name="live_20260827_0133.log",
    )
    probes, clock = FakeProbes(), Clock()
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    dog.poll_once()
    assert dog.poll_once() == "relit"
    assert dog._corpse_human_kill_reason() is None
    assert calls == ["STOP", "START"]


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_probe_exchange_reach_stages() -> None:
    """The probe's verdict ladder: DNS failure (the exact 8/27 cause) ->
    not ok at stage dns; HTTP 200 JSON -> ok with the status fields; a 5xx
    (maintenance) -> reachable-but-not-serving = not ok; URLError -> not ok."""
    import socket
    import urllib.error

    url = "https://external-api.kalshi.com/trade-api/v2"

    def dns_fail(host: str, port: int) -> None:
        raise socket.gaierror(11001, "getaddrinfo failed")

    out = hw.probe_exchange_reach(url, 1.0, resolve=dns_fail, opener=lambda *a, **k: None)
    assert out["ok"] is False and out["stage"] == "dns"
    assert "getaddrinfo failed" in str(out["error"])
    assert out["host"] == "external-api.kalshi.com"

    seen: dict[str, object] = {}

    def ok_open(req: object, timeout: float) -> _FakeResponse:
        seen["url"] = req.full_url  # type: ignore[attr-defined]
        seen["timeout"] = timeout
        return _FakeResponse(200, b'{"exchange_active": true, "trading_active": true}')

    out = hw.probe_exchange_reach(url, 10.0, resolve=lambda h, p: [], opener=ok_open)
    assert out["ok"] is True and out["status"] == 200 and out["exchange_active"] is True
    assert seen["url"] == url + "/exchange/status"
    assert seen["timeout"] == 10.0  # rest.py DEFAULT_REQUEST_TIMEOUT_S, mirrored

    def maint(req: object, timeout: float) -> _FakeResponse:
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)  # type: ignore[arg-type]

    out = hw.probe_exchange_reach(url, 1.0, resolve=lambda h, p: [], opener=maint)
    assert out["ok"] is False and out["status"] == 503

    def unreachable(req: object, timeout: float) -> _FakeResponse:
        raise urllib.error.URLError("timed out")

    out = hw.probe_exchange_reach(url, 1.0, resolve=lambda h, p: [], opener=unreachable)
    assert out["ok"] is False and out["stage"] == "http"


def test_exchange_anchor_reads_only_the_live_local_yaml(tmp_path: Path) -> None:
    """The probe host follows the LIVE config (config/*.local.yaml), never a
    per-env template: demo.yaml's ``env: demo`` must not steer a prod tree."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "demo.yaml").write_text("env: demo\n", encoding="utf-8")
    (cfg / "prod.yaml").write_text("env: prod\n", encoding="utf-8")
    assert hw._read_exchange_anchors(tmp_path) == hw._REST_BASE_URLS["prod"]
    (cfg / "prod-live.local.yaml").write_text("env: demo\n", encoding="utf-8")
    assert hw._read_exchange_anchors(tmp_path) == hw._REST_BASE_URLS["demo"]
    (cfg / "prod-live.local.yaml").write_text(
        "env: prod\nendpoints:\n  rest_base_url: https://x.example/trade-api/v2\n",
        encoding="utf-8",
    )
    assert hw._read_exchange_anchors(tmp_path) == "https://x.example/trade-api/v2"


# --------------------------------------------------------------------------
# Exchange 5xx at boot (2026-09-04 build, second fixture): the 2026-08-06
# 03:13/03:16 ET maintenance-window boot deaths. Kalshi answered every call
# HTTP 503; startup_reconcile_failed carried KalshiApiError('HTTP 503 ...'),
# the CLI printed REFUSING TO QUOTE on book_reconciled, and the supervisor's
# own supervisor_exchange_unreachable fired on the same 503 ("cannot reach
# exchange", exchange_reachable: false). The live code calls that unreachable;
# so does the classifier. The fixture is that corpse's log, verbatim (37
# lines, incl. the raw cp1252 em-dash byte the CLI printed).
# --------------------------------------------------------------------------

CORPSE_8_6 = Path(__file__).parent / "fixtures" / "watchdog" / "live_20260806_0313_tail.log"


def _corpse_8_6_text() -> str:
    return CORPSE_8_6.read_text(encoding="utf-8", errors="replace")


def test_classify_death_the_8_6_maintenance_corpse_is_network() -> None:
    verdict = hw.classify_death(_corpse_8_6_text())
    assert verdict["class"] == hw.DEATH_NETWORK
    assert verdict["marker"] == "startup_reconcile_failed: HTTP 503"
    assert str(verdict["line"]).startswith("REFUSING TO QUOTE")
    assert "book_reconciled" in str(verdict["line"])


_TB_REST_RAISE = (
    "Traceback (most recent call last):\n"
    '  File "C:\\x\\src\\combomaker\\exchange\\rest.py", line 389, in _request\n'
    "    raise err_cls(resp.status, code, message, details)\n"
)


@pytest.mark.parametrize(
    ("name", "tail", "cls", "marker"),
    [
        (
            "supervisor_exchange_unreachable on an exchange 503 (the 8/6 03:14 shape)",
            '{"event": "quote_app_starting"}\n'
            '{"detail": "cannot reach exchange \\u2014 writing KILL anyway (fail-closed)", '
            '"error": "KalshiApiError(\'HTTP 503 : <html>503 Service Temporarily '
            "Unavailable</html>')\", "
            '"event": "supervisor_exchange_unreachable", "level": "error"}\n',
            hw.DEATH_NETWORK,
            "supervisor_exchange_unreachable: HTTP 503",
        ),
        (
            "terminal KalshiApiError 5xx traceback (rest.py l.389 raise)",
            _TB_REST_RAISE + "combomaker.exchange.rest.KalshiApiError: HTTP 502 : bad gateway\n",
            hw.DEATH_NETWORK,
            "KalshiApiError HTTP 502",
        ),
        (
            "terminal KalshiApiError 503 whose HTML body spills over lines (the real str)",
            _TB_REST_RAISE + "combomaker.exchange.rest.KalshiApiError: HTTP 503 : <html>\n"
            "<head><title>503 Service Temporarily Unavailable</title></head>\n"
            "<body>\n"
            "<center><h1>503 Service Temporarily Unavailable</h1></center>\n"
            "</body>\n"
            "</html>\n",
            hw.DEATH_NETWORK,
            "KalshiApiError HTTP 503",
        ),
        (
            "terminal KalshiApiError 4xx traceback (OUR request is wrong: CODE)",
            _TB_REST_RAISE
            + "combomaker.exchange.rest.KalshiApiError: HTTP 400 bad_request: invalid subaccount\n",
            hw.DEATH_CONFIG_CODE,
            "combomaker.exchange.rest.KalshiApiError",
        ),
        (
            "terminal RateLimitedError 429 (ambiguous: stays fail-closed, CODE)",
            _TB_REST_RAISE
            + "combomaker.exchange.rest.RateLimitedError: HTTP 429 too_many_requests: too many\n",
            hw.DEATH_CONFIG_CODE,
            "combomaker.exchange.rest.RateLimitedError",
        ),
        (
            "REFUSING TO QUOTE on book_reconciled after a 401 reconcile failure (creds: CODE)",
            '{"event": "quote_app_starting"}\n'
            '{"error": "KalshiApiError(\'HTTP 401 unauthorized: bad signature\')", '
            '"event": "startup_reconcile_failed", "level": "error", "phase": "enumerate"}\n'
            "REFUSING TO QUOTE: prod go-live preflight failed \u2014 red gates: book_reconciled\n",
            hw.DEATH_CONFIG_CODE,
            "REFUSING TO QUOTE",
        ),
        (
            "REFUSING TO QUOTE on book_reconciled: 503 reconcile failure, then a LATER supervisor "
            "503 (the real 8/6 ordering) — the reconcile cause is still the verdict",
            '{"event": "quote_app_starting"}\n'
            '{"error": "KalshiApiError(\'HTTP 503 : <html>\')", '
            '"event": "startup_reconcile_failed", '
            '"level": "error", "phase": "enumerate"}\n'
            "REFUSING TO QUOTE: prod go-live preflight failed \u2014 red gates: book_reconciled\n"
            '{"error": "KalshiApiError(\'HTTP 503 : <html>\')", '
            '"event": "supervisor_exchange_unreachable", "level": "error"}\n',
            hw.DEATH_NETWORK,
            "startup_reconcile_failed: HTTP 503",
        ),
        (
            "exchange_status_failed on a 5xx from an EARLIER boot never names this boot",
            '{"error": "KalshiApiError(\'HTTP 503 : x\')", "event": "exchange_status_failed"}\n'
            '{"event": "quote_app_starting"}\n'
            '{"event": "pricing_stats"}\n',
            hw.DEATH_UNKNOWN,
            None,
        ),
    ],
)
def test_classify_death_exchange_5xx_is_network_4xx_is_code(
    name: str, tail: str, cls: str, marker: str | None
) -> None:
    verdict = hw.classify_death(tail)
    assert verdict["class"] == cls, name
    assert verdict["marker"] == marker, name


def test_maintenance_boot_death_waits_through_5xx_then_relights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 8/6 03:13 class end-to-end at the state-machine level with the
    real corpse: the boot died on exchange 503s before any heartbeat -> NOT
    latched; backoff threshold x streak (100, 200); the probe keeps waiting
    while the exchange still answers 503 (reachable-but-not-serving is not
    ok) and the relight fires on the first 200."""
    _plant_corpse(tmp_path, _corpse_8_6_text(), name="live_20260806_0313.log")
    probes, clock = FakeProbes(), Clock()
    probes.heartbeat = False
    probes.pids = []
    probes.log = None  # type: ignore[assignment]
    dog, calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    reach = ReachScript(
        [
            {"ok": False, "stage": "http", "status": 503, "error": "HTTP 503"},
            {"ok": True, "stage": "http", "status": 200, "exchange_active": True},
        ]
    )
    dog.reach_probe = reach
    slept = _patched_sleep(monkeypatch)
    dog.poll_once()
    assert dog.poll_once() == "relit"
    assert calls == ["STOP", "START"]
    assert reach.calls == 2
    assert slept == [100.0, 200.0]
    assert dog._halt is None
    assert not list((tmp_path / "data").glob("WATCHDOG_HALT_*.txt"))
    state = json.loads((tmp_path / "data" / hw.STATE_FILE).read_text(encoding="utf-8"))
    assert state["relights"][-1]["boot_death"] == hw.DEATH_NETWORK
    assert state["relights"][-1]["reach_waits"] == 2
    log = (tmp_path / "data" / hw.WATCHDOG_LOG).read_text(encoding="utf-8")
    assert "boot death class NETWORK (startup_reconcile_failed: HTTP 503)" in log
    assert log.count("UNREACHABLE") == 1 and ": REACHABLE {" in log


def test_corpse_tail_reads_the_newest_log_name_on_an_mtime_tie(tmp_path: Path) -> None:
    """Windows stamps file times off the system tick (~15.6 ms), so logs
    written in one burst can share an mtime; on a tie the corpse is the
    newest NAME (live_YYYYMMDD_HHMM.log names sort chronologically) — never
    a stale tape. Pinned after prove_watchdog P2 read a synthetic tape
    instead of the corpse on the unmodified tree (2026-09-04)."""
    import os

    data = tmp_path / "data"
    data.mkdir()
    old, new = data / "live_20260730_0400.log", data / "live_20260904_1200.log"
    old.write_text('{"event": "x"}\n', encoding="utf-8")
    new.write_text("config error: 1 validation error for AppConfig\n", encoding="utf-8")
    stamp = old.stat().st_mtime_ns
    os.utime(old, ns=(stamp, stamp))
    os.utime(new, ns=(stamp, stamp))
    assert old.stat().st_mtime_ns == new.stat().st_mtime_ns
    probes, clock = FakeProbes(), Clock()
    dog, _calls = make_dog(tmp_path, probes, clock, threshold=100.0)
    assert dog._corpse_tail().startswith("config error:")
    assert dog._corpse_death_class()["class"] == hw.DEATH_CONFIG_CODE


def test_default_reach_probe_follows_the_live_yaml_anchor(tmp_path: Path) -> None:
    """Watchdog() without an injected probe must aim at the same host the
    CLI derives (live local yaml -> env), never a hard-coded one."""
    import unittest.mock as um

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "prod-live.local.yaml").write_text(
        "env: prod\nendpoints:\n  rest_base_url: https://probe.example/trade-api/v2\n",
        encoding="utf-8",
    )
    seen: dict[str, object] = {}

    def fake_probe(url: str, timeout_s: float, **kw: object) -> dict[str, object]:
        seen["url"] = url
        seen["timeout"] = timeout_s
        return {"ok": True}

    with um.patch.object(hw, "probe_exchange_reach", fake_probe):
        probes, clock = FakeProbes(), Clock()
        dog, _calls = make_dog(tmp_path, probes, clock, threshold=100.0)
        assert dog.reach_probe is not None
        dog.reach_probe()
    assert seen == {
        "url": "https://probe.example/trade-api/v2",
        "timeout": hw._DEFAULT_REQUEST_TIMEOUT_S,
    }
