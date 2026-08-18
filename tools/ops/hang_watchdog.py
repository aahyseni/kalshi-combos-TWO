"""HANG WATCHDOG — an EXTERNAL supervisor that watches WORK, not PIDs.

WHY (2026-07-29 → 07-31, the 45h outage): the in-tree supervisor detected a
maintenance-loop stall at 13:23:01Z, wrote the KILL file and started shutdown —
and the shutdown itself hung at ``book_ws_stop``. The PID stayed alive for 45
hours with the log frozen; everything that watched "is the process alive"
stayed green, and START_BOT.bat then refused to relaunch over the live PIDs.
The missing layer is a supervisor OUTSIDE the process tree that watches
OBSERVABLE PROGRESS and can force the stop/start cycle itself.

WHAT IT WATCHES (work, never process-aliveness):
  * the active bot log (from ``data/CURRENT_LOG.txt``): any (size, mtime)
    advance is progress;
  * decision flow in the store: the last ``decisions`` row's ``at`` value
    ADVANCING is progress. Deliberately NOT the WAL file mtime — the persistence
    writer runs ~an hour behind the event firehose (measured 2026-07-31: last
    visible row 68 min old while the WAL advanced), so after a hang the WAL
    keeps moving while the backlog drains; the row value freezes at the hang.
A stall is declared only when BOTH axes have been quiet longer than the
DERIVED threshold — any sign of work on either axis resets the clock, so a
quiet-but-working bot is never restarted.

THRESHOLD — DERIVED FROM THE TAPE, NEVER HAND-SET (north star):
    threshold_s = max(2 x longest quiet gap observed in the healthy tape,
                      2 x internal escalation chain)
  * The tape term: every completed ``data/live_*.log`` is scanned for its
    longest inter-line quiet gap (JSON ``ts`` + boot-format lines, distinct
    seconds; overnight lulls included). Log-line gaps upper-bound the joint
    (log AND store) quiet gap, so a threshold above the worst log-only lull can
    not false-positive on a lull that has ever been observed. The x2 margin
    means a lull must be TWICE as long as anything ever seen healthy before we
    act. Scans are cached per completed log (size-keyed) in
    ``data/watchdog_tape.json`` so each start folds in only the new logs.
  * The floor term: the in-tree chain gets to finish first — heartbeat stall
    bound (timeout + poll) + bounded shutdown (same timeout anchor, see
    quote_app shutdown), x2 margin: ``2 * (2*heartbeat_timeout_s + poll)``.
    Anchors are read from the live YAML config (never printed); defaults come
    from SupervisorConfig.

ESCALATION (bounded — REFUSES to flap):
  stall/exit detected -> capture evidence -> STOP the tree via the existing
  stop path (``stop_all.ps1 -NoPrompt``, keeping this process) -> relight via
  the existing start path (``start_all.ps1 -Auto``) — the start path keeps ALL
  of its own guards (mutex, single-instance, machine-KILL auto-clear,
  human-KILL refusal). Flap guards, in order:
    1. LIVENESS PROOF (same discriminator start_all.ps1 already uses, no
       invented number): a run that never wrote ``data/heartbeat.txt`` never
       reached liveness — relighting it is a boot loop (the 2026-07-31 09:00
       config-validation exit died in <1s, 0 relights is the right number).
       Latch a halt receipt and stay down LOUD.
    2. SHORT-RUN REPEAT: two CONSECUTIVE relights whose healthy span (last
       observed progress minus relight time) was below one detection window
       mean relighting is not producing a working bot. Latch, stay down loud.
    3. START REFUSED: on a non-zero start exit, ONE full re-sweep (the same
       stop path) + ONE retry — a refusal caused by our own incomplete sweep
       (2026-07-31 17:34 ET) is cured, and any refusal that survives a clean
       re-sweep (human-gated KILL, launch mutex, real duplicate) latches — the
       start path's own refusal reasons are authoritative.
  A latched halt writes ``data/WATCHDOG_HALT_<stamp>.txt`` (the receipt), keeps
  the tree DOWN, and keeps shouting to console + ``data/hang_watchdog.log``.
  Latch state persists in ``data/watchdog_state.json``; an OPERATOR start
  (non ``-Auto``) purges it — a human relight opens a fresh episode.

FIX-ISOLATION: this file lives in tools/ops, imports NOTHING from combomaker,
and touches the pricing path zero times. Read-only towards the store
(sqlite ``mode=ro``), read-only towards Kalshi (it never talks to Kalshi at
all). The only mutations it can perform are the operator's own stop/start
scripts. Wired in by ``start_all.ps1`` (operator starts only), so it is always
on after the next operator relight.

Test seams (proofs drive the REAL loop against a scratch tree): ``--root``,
``--probe-cmd``, ``--stop-cmd``, ``--start-cmd``, ``--max-cycles``,
``--poll-s``. Defaults are the live wiring.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

try:  # EST at the reporting boundary (operator rule); UTC fallback if tz db odd
    from zoneinfo import ZoneInfo

    _EST = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _EST = UTC  # type: ignore[assignment]

TAPE_CACHE = "watchdog_tape.json"
STATE_FILE = "watchdog_state.json"
WATCHDOG_LOG = "hang_watchdog.log"
CURRENT_LOG_POINTER = "CURRENT_LOG.txt"
HEARTBEAT = "heartbeat.txt"

# Log timestamp shapes (both appear in live logs): structured JSON lines carry
# UTC "ts"; the few pre-logging boot lines are local wall time.
_TS_JSON = re.compile(rb'"ts": "(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})')
_TS_HUMAN = re.compile(rb"(?:^|\n)(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[")
_SCAN_CHUNK = 64 * 1024 * 1024
_SCAN_OVERLAP = 80

# SupervisorConfig defaults (ops/config.py) — used only when the YAML carries
# no override; the YAML itself is never printed (secrets live next to it).
_DEFAULT_HB_TIMEOUT_S = 15.0
_DEFAULT_HB_POLL_S = 1.0

# The x2 safety margin used by BOTH threshold terms: act only when the quiet is
# TWICE as long as the worst the healthy tape (or the internal chain) explains.
_MARGIN = 2.0


def _now_utc() -> datetime:
    return datetime.now(UTC)


def est_stamp(dt: datetime | None = None) -> str:
    return (dt or _now_utc()).astimezone(_EST).strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------------------------------------------------------
# Tape derivation
# --------------------------------------------------------------------------


def scan_log_gap(path: Path) -> dict[str, object]:
    """Longest quiet gap (seconds) between logged lines of one completed log.

    Distinct-second resolution: a gap is time between consecutive distinct
    line-timestamp seconds. Human boot lines are local time; they are folded in
    by pairwise diffs within their own family only when JSON lines are absent
    (a config-error boot log has only human lines), otherwise JSON (UTC)
    dominates and the boot handoff is bridged by the first JSON second.
    """
    seconds: set[bytes] = set()
    human: set[str] = set()
    size = path.stat().st_size
    with open(path, "rb") as f:
        tail = b""
        while True:
            chunk = f.read(_SCAN_CHUNK)
            if not chunk:
                break
            buf = tail + chunk
            for m in _TS_JSON.finditer(buf):
                seconds.add(m.group(1))
            for m in _TS_HUMAN.finditer(buf):
                human.add(m.group(1).decode())
            tail = buf[-_SCAN_OVERLAP:]
    epochs: set[int] = set()
    for s in seconds:
        dt = datetime.strptime(s.decode(), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        epochs.add(int(dt.timestamp()))
    if not epochs and human:
        # boot-only log (e.g. instant config-error exit): local-time lines,
        # diffed among themselves — same clock, so gaps are exact.
        local = sorted(int(datetime.strptime(h, "%Y-%m-%d %H:%M:%S").timestamp()) for h in human)
        epochs = set(local)
    ordered = sorted(epochs)
    max_gap = 0
    max_at = None
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b - a > max_gap:
            max_gap = b - a
            max_at = a
    return {
        "size": size,
        "distinct_seconds": len(ordered),
        "span_s": (ordered[-1] - ordered[0]) if len(ordered) > 1 else 0,
        "max_gap_s": max_gap,
        "max_gap_at_utc": (datetime.fromtimestamp(max_at, UTC).isoformat() if max_at else None),
    }


def _read_supervisor_anchors(root: Path) -> tuple[float, float]:
    """(heartbeat_timeout_s, poll_interval_s) from the live YAML config if
    present, else SupervisorConfig defaults. Values only — the file is never
    echoed anywhere (it sits next to secrets)."""
    hb, poll = _DEFAULT_HB_TIMEOUT_S, _DEFAULT_HB_POLL_S
    candidates = sorted(root.glob("config/*.local.yaml")) + sorted(root.glob("config/*.yaml"))
    for cfg in candidates:
        try:
            import yaml  # available in the bot venv; watchdog runs from it

            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            sup = data.get("supervisor") or {}
            if isinstance(sup, dict) and ("heartbeat_timeout_s" in sup or "poll_interval_s" in sup):
                hb = float(sup.get("heartbeat_timeout_s", hb))
                poll = float(sup.get("poll_interval_s", poll))
                break
        except Exception:
            continue
    return hb, poll


def derive_threshold(root: Path, data_dir: Path, log: Callable[[str], None]) -> dict[str, object]:
    """Tape-derived stall threshold. Incremental: completed logs are scanned
    once and cached by (name, size)."""
    cache_path = data_dir / TAPE_CACHE
    cache: dict[str, dict[str, object]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")).get("logs", {})
        except Exception:
            cache = {}

    active: set[str] = set()
    pointer = data_dir / CURRENT_LOG_POINTER
    if pointer.exists():
        try:
            for line in pointer.read_text(encoding="ascii", errors="replace").splitlines():
                line = line.strip()
                if line:
                    active.add(Path(line).name)
        except OSError:
            pass

    logs = sorted(data_dir.glob("live_*.log"))
    for p in logs:
        if p.name in active:
            continue  # still growing — fold it in next start
        size = p.stat().st_size
        rec = cache.get(p.name)
        if rec is not None and rec.get("size") == size:
            continue
        log(f"tape scan: {p.name} ({size / 1e9:.2f} GB)")
        cache[p.name] = scan_log_gap(p)

    max_gap = 0.0
    max_log = None
    for name, rec in cache.items():
        g = float(rec.get("max_gap_s", 0) or 0)
        if g > max_gap:
            max_gap, max_log = g, name
    hb, poll = _read_supervisor_anchors(root)
    floor = _MARGIN * (2.0 * hb + poll)  # let the in-tree chain finish, x2
    threshold = max(_MARGIN * max_gap, floor)
    result = {
        "derived_at": _now_utc().isoformat(),
        "logs_measured": len(cache),
        "max_healthy_gap_s": max_gap,
        "max_gap_log": max_log,
        "internal_chain_floor_s": floor,
        "hb_timeout_s": hb,
        "hb_poll_s": poll,
        "margin": _MARGIN,
        "threshold_s": threshold,
    }
    try:
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"derivation": result, "logs": cache}, indent=1), "utf-8")
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return result


# --------------------------------------------------------------------------
# Probes (each with a command seam for the scratch proofs)
# --------------------------------------------------------------------------


@dataclass
class Probes:
    root: Path
    data_dir: Path
    probe_cmd: str | None = None  # test seam: shell cmd printing one PID per line

    def active_log(self) -> Path | None:
        pointer = self.data_dir / CURRENT_LOG_POINTER
        try:
            first = pointer.read_text(encoding="ascii", errors="replace").splitlines()[0].strip()
        except (OSError, IndexError):
            return None
        if not first:
            return None
        p = Path(first)
        return p if p.is_absolute() else self.root / p

    def log_sig(self) -> tuple[str, int, int] | None:
        p = self.active_log()
        if p is None:
            return None
        try:
            st = p.stat()
        except OSError:
            return None
        return (p.name, st.st_size, st.st_mtime_ns)

    def store_sig(self) -> str | None:
        """The last decisions row's ``at`` — the VALUE, advancing = progress."""
        try:
            dbs = [p for p in self.data_dir.glob("*.sqlite3") if not p.name.endswith((".tmp",))]
            if not dbs:
                return None

            def freshness(p: Path) -> float:
                m = p.stat().st_mtime
                wal = Path(str(p) + "-wal")
                if wal.exists():
                    m = max(m, wal.stat().st_mtime)
                return m

            db = max(dbs, key=freshness)
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2.0)
            try:
                row = con.execute("select at from decisions order by id desc limit 1").fetchone()
            finally:
                con.close()
            return None if row is None else str(row[0])
        except Exception:
            return None  # axis unknown — the log axis carries detection alone

    def bot_pids(self) -> list[int] | None:
        """PIDs of live bot pythons; ``None`` = probe failed (treat as
        "assume present" — never let a broken probe fabricate an exit)."""
        if self.probe_cmd is not None:
            try:
                out = subprocess.run(
                    self.probe_cmd, shell=True, capture_output=True, text=True, timeout=30
                )
            except Exception:
                return None
            if out.returncode != 0:
                return None
            return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
            "Where-Object { $_.CommandLine -match 'combomaker\\.ops\\.cli run' } | "
            "ForEach-Object { $_.ProcessId }"
        )
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            return None
        if out.returncode != 0:
            return None
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]

    def heartbeat_exists(self) -> bool:
        try:
            return (self.data_dir / HEARTBEAT).exists()
        except OSError:
            return True  # unreadable FS must not read as "never reached liveness"


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------


@dataclass
class Watchdog:
    root: Path
    data_dir: Path
    probes: Probes
    threshold_s: float
    poll_s: float
    stop_cmd: str
    start_cmd: str
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    echo: Callable[[str], None] = print

    _last_log_sig: tuple[str, int, int] | None = None
    _last_store_sig: str | None = None
    _last_log_advance: float = field(default=0.0)
    _last_store_advance: float = field(default=0.0)
    _episode_start: float = field(default=0.0)
    _zero_pid_polls: int = 0
    _frozen_log_polls: int = 0
    _halt: dict[str, object] | None = None
    _relights: list[dict[str, object]] = field(default_factory=list)
    _last_halt_shout: float = field(default=0.0)

    def __post_init__(self) -> None:
        now = self.monotonic()
        self._last_log_advance = now
        self._last_store_advance = now
        self._episode_start = now
        self._load_state()

    # -- logging ------------------------------------------------------------
    def log(self, level: str, msg: str) -> None:
        line = f"{est_stamp()} [{level:<5}] WATCHDOG {msg}"
        self.echo(line)
        try:
            with open(self.data_dir / WATCHDOG_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    # -- state --------------------------------------------------------------
    def _state_path(self) -> Path:
        return self.data_dir / STATE_FILE

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
            self._halt = data.get("halt")
            self._relights = list(data.get("relights", []))
        except Exception:
            self._halt = None
            self._relights = []

    def _save_state(self) -> None:
        try:
            tmp = self._state_path().with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "updated": _now_utc().isoformat(),
                        "halt": self._halt,
                        "relights": self._relights[-50:],
                    },
                    indent=1,
                ),
                "utf-8",
            )
            os.replace(tmp, self._state_path())
        except OSError:
            pass

    # -- escalation ---------------------------------------------------------
    def _corpse_human_kill_reason(self) -> str | None:
        """The dead bot's cause of death, IF it was a human-gated KILL.

        2026-08-17 23:39/23:43 ET incident: the -Auto relight path launched
        straight through a ``halt_hard_trip`` ("KILL, human-only clear")
        TWICE — the engine's kill-switch state is in-process only, so a
        corpse dead by KILL looks identical to a crash from out here. The
        engine self-describes human-gated kills in the halt event's detail
        ("human-only clear"), so the corpse's newest log IS the persisted
        receipt: if its last kill_switch_halt is human-gated, auto-relight
        must LATCH (the documented "only a human KILL latches" contract,
        finally enforced). An operator START (non -Auto) still purges the
        latch and opens a fresh episode — the human clear path unchanged."""
        try:
            logs = sorted(
                self.data_dir.glob("live_*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not logs:
                return None
            with logs[0].open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 262_144))
                tail = fh.read().decode("utf-8", errors="replace")
            last_halt = None
            for line in tail.splitlines():
                if '"event": "kill_switch_halt"' in line:
                    last_halt = line
            if last_halt and "human-only clear" in last_halt:
                m = re.search(r'"reason": "([a-z_]+)"', last_halt)
                return m.group(1) if m else "human_gated_kill"
        except OSError:
            pass  # unreadable log ⇒ no receipt ⇒ ordinary relight path
        return None

    def _latch_halt(self, reason: str, evidence: dict[str, object]) -> None:
        self._halt = {"at": _now_utc().isoformat(), "reason": reason, "evidence": evidence}
        self._save_state()
        stamp = _now_utc().astimezone(_EST).strftime("%Y%m%d_%H%M%S")
        receipt = self.data_dir / f"WATCHDOG_HALT_{stamp}.txt"
        body = [
            f"WATCHDOG HALT — staying DOWN. {est_stamp()}",
            f"reason: {reason}",
            "evidence:",
            json.dumps(evidence, indent=1, default=str),
            f"relight history (this episode): {json.dumps(self._relights[-5:], default=str)}",
            "",
            "RECOVERY: investigate, then run STOP_BOT.bat (sweep) and START_BOT.bat.",
            "An operator START clears watchdog state and opens a fresh episode.",
        ]
        try:
            receipt.write_text("\n".join(body), encoding="utf-8")
        except OSError:
            pass
        self.log("ERROR", f"HALT LATCHED — {reason} (receipt: {receipt.name})")

    def _run(self, cmd: str, timeout: float) -> int:
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log("ERROR", f"command timed out after {timeout:.0f}s: {cmd}")
            return -1
        except Exception as exc:
            self.log("ERROR", f"command failed to run: {cmd} ({exc!r})")
            return -1
        for stream in (out.stdout, out.stderr):
            for line in (stream or "").splitlines():
                if line.strip():
                    self.log("INFO", f"  | {line.rstrip()}")
        return out.returncode

    def _escalate(self, kind: str, detail: str) -> str:
        now = self.monotonic()
        hb = self.probes.heartbeat_exists()
        healthy_span = max(self._last_log_advance, self._last_store_advance) - self._episode_start
        evidence: dict[str, object] = {
            "kind": kind,
            "detail": detail,
            "log_quiet_s": round(now - self._last_log_advance, 1),
            "store_quiet_s": round(now - self._last_store_advance, 1),
            "threshold_s": self.threshold_s,
            "heartbeat_present": hb,
            "healthy_span_s": round(healthy_span, 1),
            "last_log_sig": self._last_log_sig,
            "last_store_sig": self._last_store_sig,
        }
        self.log("ERROR", f"STALL ESCALATION ({kind}): {detail}")
        self.log("ERROR", f"evidence: {json.dumps(evidence, default=str)}")

        # ALWAYS stop first — a hung tree must come down whether or not we
        # relight; for an already-exited tree this is an idempotent sweep.
        rc = self._run(self.stop_cmd, timeout=180)
        pids = self.probes.bot_pids()
        if pids:
            evidence["stop_rc"] = rc
            evidence["pids_still_alive"] = pids
            self._latch_halt("stop path could not kill the hung tree — human needed", evidence)
            return "halt_stop_failed"

        # Flap guard 1: liveness proof (start_all.ps1's own discriminator).
        if not hb:
            self._latch_halt(
                "run never reached liveness (no heartbeat) — relighting would boot-loop",
                evidence,
            )
            return "halt_boot_loop"

        # Flap guard 2 — REWORKED 2026-08-06 (operator ruling, verbatim: "if
        # bot crashes, restart, thats it"). The permanent flap LATCH twice kept
        # a healthy recovery down for HOURS after a transient outside cause
        # ended (Kalshi's Thu 03-05 ET maintenance 503s on 8/6; the daily halt
        # on 8/5) — the latch turned a bounded outage into an unbounded one.
        # Consecutive short-lived relights now BACK OFF instead of latching:
        # wait = threshold x streak, capped 900s (the documented ~2h
        # maintenance window is survivable in ~8 capped retries), retrying
        # forever. A HUMAN-gated start refusal still latches (guard below,
        # unchanged) and boot-loop guard 1 (no heartbeat) is unchanged.
        short = healthy_span < self.threshold_s
        if self._relights:
            self._relights[-1]["short_run"] = short
        streak = 0
        for r in reversed(self._relights):
            if bool(r.get("short_run")):
                streak += 1
            else:
                break
        if streak >= 2:
            backoff_s = min(self.threshold_s * streak, 900.0)
            self._save_state()
            self.log(
                "WARN",
                f"flap streak {streak}: backing off {backoff_s:.0f}s then "
                "relighting (retry-forever; only a human KILL latches)",
            )
            time.sleep(backoff_s)

        # HUMAN-GATED KILL RESPECT (2026-08-17 incident — see
        # ``_corpse_human_kill_reason``): a bot dead by a human-only-clear
        # KILL must stay down until the OPERATOR starts it. Checked at the
        # last instant before every -Auto relight so no earlier guard can
        # route around it.
        kill_reason = self._corpse_human_kill_reason()
        if kill_reason:
            evidence["human_gated_kill"] = kill_reason
            self._latch_halt(
                f"bot died by human-gated KILL ({kill_reason}) — auto-relight "
                "refused; operator START required",
                evidence,
            )
            return "halt_human_kill"

        self.log("WARN", "relighting via the operator start path (-Auto)")
        rc = self._run(self.start_cmd, timeout=300)
        if rc != 0:
            # START REFUSED → FULL RE-SWEEP → RETRY ONCE (2026-07-31 17:34 ET:
            # the guard refused over a process the first stop pass had left
            # behind, and the latch kept a healthy recovery down). The retry
            # runs the SAME full stop path, verifies nothing of the tree (but
            # this process) survived, and calls start once more. Exactly ONE
            # retry: a refusal that survives a clean re-sweep is authoritative
            # (human-gated KILL, launch mutex, a real duplicate) and latches
            # exactly as before — the fail-safe posture is unchanged, only a
            # refusal caused by our own incomplete sweep is cured.
            evidence["start_rc"] = rc
            self.log(
                "WARN",
                f"start path refused (rc={rc}) — full re-sweep, then ONE retry",
            )
            resweep_rc = self._run(self.stop_cmd, timeout=180)
            evidence["resweep_rc"] = resweep_rc
            pids = self.probes.bot_pids()
            if pids:
                evidence["pids_still_alive_after_resweep"] = pids
                self._latch_halt(
                    "re-sweep before the start retry could not clear the tree — human needed",
                    evidence,
                )
                return "halt_stop_failed"
            rc = self._run(self.start_cmd, timeout=300)
            if rc != 0:
                evidence["start_retry_rc"] = rc
                self._latch_halt(
                    f"start path refused the relight twice (rc={rc}) — its reason is authoritative",
                    evidence,
                )
                return "halt_start_refused"

        self._relights.append(
            {"at": _now_utc().isoformat(), "kind": kind, "healthy_span_s": round(healthy_span, 1)}
        )
        self._save_state()
        now = self.monotonic()
        self._episode_start = now
        self._last_log_advance = now
        self._last_store_advance = now
        self._last_log_sig = None
        self._last_store_sig = None
        self._zero_pid_polls = 0
        self._frozen_log_polls = 0
        self.log("WARN", f"relight complete (relights this episode: {len(self._relights)})")
        return "relit"

    # -- one poll -----------------------------------------------------------
    def poll_once(self) -> str:
        now = self.monotonic()
        if self._halt is not None:
            if now - self._last_halt_shout > 300:
                self._last_halt_shout = now
                self.log(
                    "ERROR",
                    f"STAYING DOWN (halt latched): {self._halt.get('reason')} — "
                    "operator START_BOT.bat opens a fresh episode",
                )
            return "halted"

        log_sig = self.probes.log_sig()
        if log_sig is not None and log_sig != self._last_log_sig:
            self._last_log_sig = log_sig
            self._last_log_advance = now
            self._frozen_log_polls = 0
        else:
            self._frozen_log_polls += 1

        store_sig = self.probes.store_sig()
        if store_sig is not None and store_sig != self._last_store_sig:
            self._last_store_sig = store_sig
            self._last_store_advance = now

        log_quiet = now - self._last_log_advance
        store_quiet = now - self._last_store_advance
        stale = log_quiet > self.threshold_s and store_quiet > self.threshold_s

        pids = self.probes.bot_pids()
        if pids is None:
            self._zero_pid_polls = 0  # probe failure: assume present
        elif pids:
            self._zero_pid_polls = 0
        else:
            self._zero_pid_polls += 1

        # Fast path: the process is GONE (2+ consecutive clean probes) and the
        # log is not being written (2+ polls) — an exited bot needs no full
        # quiet window to diagnose. Covers the instant config-error exit and
        # the in-tree supervisor's designed os._exit escalations.
        if self._zero_pid_polls >= 2 and self._frozen_log_polls >= 2:
            return self._escalate(
                "process_exited",
                f"no bot process on {self._zero_pid_polls} consecutive probes, "
                f"log quiet {log_quiet:.0f}s",
            )

        if stale:
            kind = "hung_process" if pids else "no_process"
            return self._escalate(
                kind,
                f"no observable work for > {self.threshold_s:.0f}s "
                f"(log quiet {log_quiet:.0f}s, store quiet {store_quiet:.0f}s, "
                f"pids={pids})",
            )
        return "ok"

    def run(self, max_cycles: int = 0) -> None:
        self.log(
            "INFO",
            f"armed: threshold={self.threshold_s:.1f}s poll={self.poll_s:.1f}s root={self.root}",
        )
        cycles = 0
        while True:
            try:
                self.poll_once()
            except Exception as exc:  # the watchdog itself must not die quiet
                self.log("ERROR", f"poll crashed (continuing): {exc!r}")
            cycles += 1
            if max_cycles and cycles >= max_cycles:
                return
            self.sleep(self.poll_s)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _acquire_single_instance(root: Path) -> object | None:
    """Named OS mutex per tree (same pattern as start_all.ps1's launch mutex).
    Returns the handle to hold for process lifetime, or None if another
    watchdog already runs for this root."""
    if os.name != "nt":  # pragma: no cover
        return object()
    tag = hashlib.md5(str(root.resolve()).lower().encode()).hexdigest()[:8]
    name = f"Global\\combomaker_hang_watchdog_{tag}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return object()  # cannot create a mutex — never block the watchdog on it
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd")
    for name in ("run", "derive", "status"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=None)
        if name == "run":
            p.add_argument("--poll-s", type=float, default=None)
            p.add_argument("--max-cycles", type=int, default=0)
            p.add_argument("--probe-cmd", default=None)
            p.add_argument("--stop-cmd", default=None)
            p.add_argument("--start-cmd", default=None)
    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return 2

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[2]
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    def echo(msg: str) -> None:
        print(msg, flush=True)

    if args.cmd == "derive":
        result = derive_threshold(root, data_dir, lambda m: echo(f"{est_stamp()} [INFO ] {m}"))
        echo(json.dumps(result, indent=1))
        return 0

    if args.cmd == "status":
        state_path = data_dir / STATE_FILE
        echo(state_path.read_text(encoding="utf-8") if state_path.exists() else "no state")
        cache = data_dir / TAPE_CACHE
        if cache.exists():
            echo(json.dumps(json.loads(cache.read_text("utf-8")).get("derivation"), indent=1))
        return 0

    handle = _acquire_single_instance(root)
    if handle is None:
        echo("REFUSING TO START — another hang watchdog already runs for this tree.")
        return 1

    derivation = derive_threshold(root, data_dir, lambda m: echo(f"{est_stamp()} [INFO ] {m}"))
    threshold = float(derivation["threshold_s"])
    # Poll at threshold/10 (detection latency <= 1.1x threshold), bounded so a
    # tiny scratch threshold cannot busy-spin and a huge one still polls.
    poll = args.poll_s if args.poll_s is not None else min(max(threshold / 10.0, 5.0), 60.0)

    me = os.getpid()
    ps = "powershell -NoProfile -ExecutionPolicy Bypass -File"
    stop_cmd = args.stop_cmd or (
        f'{ps} "{root / "tools" / "ops" / "stop_all.ps1"}" -NoPrompt -KeepPid {me}'
    )
    start_cmd = args.start_cmd or (
        f'{ps} "{root / "tools" / "ops" / "start_all.ps1"}" -Auto -CallerPid {me}'
    )

    probes = Probes(root=root, data_dir=data_dir, probe_cmd=args.probe_cmd)
    dog = Watchdog(
        root=root,
        data_dir=data_dir,
        probes=probes,
        threshold_s=threshold,
        poll_s=poll,
        stop_cmd=stop_cmd,
        start_cmd=start_cmd,
    )
    dog.log(
        "INFO",
        "derivation: "
        + json.dumps(
            {
                k: derivation[k]
                for k in (
                    "threshold_s",
                    "max_healthy_gap_s",
                    "max_gap_log",
                    "internal_chain_floor_s",
                    "logs_measured",
                )
            }
        ),
    )
    dog.run(max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
