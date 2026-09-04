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
       reached liveness. WHY it died decides what happens next (2026-09-04
       build — the 8/27 02:13 ET latch that became an 8-day outage): the
       corpse log is CLASSIFIED (``classify_death``, markers derived from what
       the live code raises/logs). NETWORK (exchange unreachable, DNS, an
       exchange 5xx, feed lost) is NOT a boot loop — wait the same derived
       backoff guard 2 uses,
       gate on an exchange reach probe, retry forever. CONFIG_CODE (a boot
       refusal print, a traceback without a network root cause — the
       2026-07-31 09:00 config-validation exit died in <1s, 0 relights is the
       right number) and UNKNOWN (no readable cause) latch a halt receipt and
       stay down LOUD, exactly as before.
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
(sqlite ``mode=ro``), read-only towards Kalshi (its ONLY exchange call is the
unauthenticated ``GET /exchange/status`` reach probe, made only while a
NETWORK-class boot death keeps the tree down; never an authenticated or
mutating call). The only mutations it can perform are the operator's own
stop/start scripts. Wired in by ``start_all.ps1`` (operator starts only), so
it is always on after the next operator relight.

Test seams (proofs drive the REAL loop against a scratch tree): ``--root``,
``--probe-cmd``, ``--stop-cmd``, ``--start-cmd``, ``--reach-cmd``,
``--max-cycles``, ``--poll-s``. Defaults are the live wiring.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

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

# Exchange endpoint + per-request wall bound, MIRRORED (never imported — this
# file imports nothing from combomaker): ``ops/config.py`` ``_ENDPOINTS``
# (doc-verified; prod ``.com``, demo ``.co``) and ``exchange/rest.py``
# ``DEFAULT_REQUEST_TIMEOUT_S`` — in its own words "the per-request wall bound
# every REST call already runs under ... reuses this one number instead of
# inventing a second one that can drift away from it". The reach probe runs
# under that same bound. The YAML's ``env`` / ``endpoints.rest_base_url``
# select the URL (``_read_exchange_anchors``); nothing here is a new knob.
_REST_BASE_URLS = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}
_DEFAULT_REQUEST_TIMEOUT_S = 10.0
_REACH_STATUS_PATH = "/exchange/status"  # rest.py get_exchange_status, auth=False

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


def _read_exchange_anchors(root: Path) -> str:
    """The REST base URL the bot itself boots against: the LIVE YAML's
    (``config/*.local.yaml`` — the file start_all.ps1 passes as ``--config``)
    ``endpoints.rest_base_url`` if set, else the doc-verified default for its
    ``env`` (mirrors ``ops/config.py`` ``load_config``'s endpoints setdefault).
    Values only — the file is never echoed. The per-env TEMPLATES
    (``demo.yaml`` / ``prod.yaml``) are deliberately NOT consulted: each names
    its own env, so they cannot say which one is live. No live YAML ⇒ prod
    (start_all.ps1 launches the tree this watchdog supervises ``--env prod``)."""
    for cfg in sorted(root.glob("config/*.local.yaml")):
        try:
            import yaml

            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            endpoints = data.get("endpoints") or {}
            if isinstance(endpoints, dict) and endpoints.get("rest_base_url"):
                return str(endpoints["rest_base_url"])
            env = data.get("env")
            if isinstance(env, str) and env in _REST_BASE_URLS:
                return _REST_BASE_URLS[env]
        except Exception:
            continue
    return _REST_BASE_URLS["prod"]


# --------------------------------------------------------------------------
# Boot-death classification (2026-09-04 build — the 8/27 02:13 ET latch)
# --------------------------------------------------------------------------
#
# WHY: 8/27 02:08:45 ET the -Auto relit boot died 45s in on aiohttp
# ``ClientConnectorDNSError: Cannot connect to host external-api.kalshi.com:443
# ... [getaddrinfo failed]`` during a Wi-Fi flap (Windows network
# re-identifications 01:32..06:27 ET). It never wrote heartbeat.txt, so flap
# guard 1 latched "relighting would boot-loop" — and nothing retried after the
# network came back: an 8-day outage. A death the NETWORK caused is not a boot
# loop; it is the 2026-08-06-ruled "if the bot crashes, restart, that's it"
# class, and the 8/6 rework already retries it when it happens AFTER the
# heartbeat. This closes the pre-heartbeat gap.
#
# CLASSES: NETWORK (exchange unreachable / DNS / exchange 5xx / feed lost) →
# wait the derived backoff, gate on a reach probe, retry forever. CONFIG_CODE (a refusal the
# bot prints at boot, or a Python traceback whose terminal exception is not a
# transport error) and UNKNOWN (no readable cause) → latch, exactly as before
# (fail-closed on the genuinely unknown).
#
# MARKERS — each derived from what the live code raises/logs, none invented:
#  * transport exceptions: ``exchange/rest.py`` ``_request`` (l.339-390)
#    catches ONLY the JSON-decode errors, so every aiohttp connection-layer
#    exception propagates to the caller verbatim. The names are the installed
#    aiohttp's (3.14.1) ``ClientConnectionError`` family — enumerated from the
#    library (16 names), plus the stdlib causes they wrap (``socket.gaierror``
#    / "getaddrinfo failed", the Connection*Error OSErrors). The aiohttp names
#    and gaierror name the network BY THEMSELVES; a bare stdlib
#    ``ConnectionResetError`` / ``ConnectionRefusedError`` /
#    ``ConnectionAbortedError`` does not — on Windows a ``ProcessPoolExecutor``
#    worker dying at boot (ops/pricing_pool.py l.148 / l.670 — a code bug)
#    surfaces as ``ConnectionResetError: [WinError 10054]`` on the pool pipe —
#    so those three count only when attributed to an exchange call: gated on
#    the same traceback-frame test as TimeoutError (next bullet). Review fix
#    2026-09-04 (was: any bare Connection*Error read NETWORK → retry forever
#    on a pool crash).
#  * ``TimeoutError`` ON AN EXCHANGE CALL: ``rest.py`` l.314 runs every call
#    under ``aiohttp.ClientTimeout(total=request_timeout_s)``, which raises the
#    builtin TimeoutError (``asyncio.TimeoutError`` IS that class on 3.11+).
#    A bare TimeoutError counts as NETWORK only when it is attributed to an
#    exchange call — a traceback frame under ``combomaker/exchange/`` or
#    ``aiohttp/``, or the ``error`` field of an exchange-reach event (next
#    bullet). A pool / gate timeout elsewhere is CODE.
#  * exchange-reach events whose ``error=repr(exc)`` is transport/timeout:
#    ``startup_reconcile_failed`` (quote_app.py l.3238 + l.3267 — "a timeout
#    or a transport error is exactly the 'exchange unreachable' case"),
#    ``supervisor_exchange_unreachable`` (supervisor.py l.276),
#    ``exchange_status_failed`` (quote_app.py l.4886).
#    ``startup_reconcile_incomplete`` (quote_app.py l.3772) carries no error
#    of its own — its cause is the preceding startup_reconcile_failed line,
#    which is what is matched.
#  * exchange 5xx, on a reach event's ``error`` or as a terminal exception:
#    Kalshi answering "503 Service Temporarily Unavailable" (maintenance) is
#    the 2026-08-06 03:13/03:16 ET death shape on the tape (both corpses:
#    startup_reconcile_failed → REFUSING TO QUOTE on book_reconciled →
#    supervisor_exchange_unreachable, all on the 503). RECORD STRAIGHT
#    (review 2026-09-04): those two were POST-heartbeat deaths — the
#    watchdog's own escalations logged ``heartbeat_present: true,
#    healthy_span_s: 74.8 / 74.7`` (hang_watchdog.log 8/6 03:16:35 and
#    03:20:05 ET), and the 03:16:45 relight auto-cleared a machine KILL,
#    which start_all.ps1 only does when the previous run reached liveness
#    — i.e. flap guard 2's class, which the 8/6 rework already retries;
#    guard 1 never saw them. Structurally they CANNOT reach guard 1:
#    quote_app.py run() beats the heartbeat (l.2608) BEFORE launching the
#    supervisor (l.2617) and running the preflight (l.2621), and every
#    pre-beat exchange call catches ``KalshiApiError`` (slate count l.4621,
#    startup reconcile l.3232/3265, risk snapshot l.3731 — all catch
#    Exception; rehydrate l.3414 catches KalshiApiError ONLY, so a 5xx is
#    caught there too). So the 5xx, refusal and feed-loss rules below are
#    FORENSIC (the class is logged on every escalation) and DEFENSIVE (a
#    future pre-beat call that lets a 5xx escape). On the live code today
#    the ONLY guard-1-reachable NETWORK class is rule 1 — an uncaught
#    transport traceback: exactly the 8/27 shape, where rehydrate's
#    KalshiApiError-only catch let aiohttp's DNS error through (bot-side
#    fix owed in src/, separate build). The class is still derived, not
#    invented: the live code ITSELF files a 5xx as unreachable — supervisor.py l.273-279
#    catches any listing Exception as ``supervisor_exchange_unreachable``
#    ("cannot reach exchange") and logged ``exchange_reachable: false`` on
#    that 503; quote_app.py l.3775 calls the reconcile failure it caused
#    "exchange unreachable". ``rest.py`` l.37-40 renders every non-2xx as
#    ``KalshiApiError`` str "HTTP {status} {code}: {message}" (raised at
#    l.388-389; 429 → ``RateLimitedError``), so the marker is the STATUS
#    CLASS: 5xx only. A 4xx is OUR request (credentials, a bad parameter)
#    → CODE; a 429 at boot is ambiguous (throttled after rapid relights vs
#    our own budget misconfig) and stays CODE — fail-closed. The reach
#    probe already reads a 5xx as "reachable but not serving" and waits.
#  * feed loss: ``kill_switch_halt`` (risk/killswitch.py l.64) with
#    ``reason=halt_data_stale`` (core/reasons.py l.305; tripped by
#    risk/breakers.py ``detect_data_stale`` l.70 on rx-age None / seq gap).
#  * boot refusals the CLI prints (ops/cli.py): ``config error:`` (l.171,
#    any load/validation failure), ``REFUSING TO START:`` (l.168
#    ProdGuardError; l.184 ConventionsUnverifiedError / RuntimeError),
#    ``credentials error:`` (l.190), ``REFUSING TO QUOTE:`` (l.197
#    PreflightError). The last one is NETWORK when its red gate is
#    ``book_reconciled`` (ops/preflight.py l.71-72) AND a network-caused
#    startup_reconcile_failed precedes it — otherwise CONFIG_CODE. (Forensic
#    only: the preflight runs AFTER the heartbeat beat — quote_app.py l.2621
#    vs l.2608 — so this print is always a guard-2 death, never guard 1's; a
#    boot that survives rehydrate has already beaten.) PRECEDENCE (review fix
#    2026-09-04): a refusal print that appears AFTER the last traceback wins
#    over it — ops/cli.py prints the refusal as its last word after catching
#    the exception, so an earlier, non-fatal traceback (asyncio's "Task
#    exception was never retrieved" carrying a transport error) can never
#    turn a genuine refusal into a NETWORK retry loop. A traceback AFTER the
#    refusal is the newer word and still decides.
#  * boot boundary: classification is scoped to the newest boot — everything
#    from the last ``quote_app_starting`` line (quote_app.py l.1884) on — so a
#    warning from an earlier boot in the same tail can never name the cause.

DEATH_NETWORK = "NETWORK"
DEATH_CONFIG_CODE = "CONFIG_CODE"
DEATH_UNKNOWN = "UNKNOWN"

_AIOHTTP_EXC_NAMES = (
    # aiohttp.client_exceptions — ClientConnectionError family (3.14.1), all 16
    "ClientConnectorDNSError",
    "ClientConnectorCertificateError",
    "ClientConnectorSSLError",
    "ClientSSLError",
    "ServerFingerprintMismatch",
    "ClientConnectorError",
    "ClientProxyConnectionError",
    "UnixClientConnectorError",
    "ServerDisconnectedError",
    "ServerTimeoutError",
    "ServerConnectionError",
    "ConnectionTimeoutError",
    "SocketTimeoutError",
    "ClientOSError",
    "ClientConnectionResetError",
    "ClientConnectionError",
)
# stdlib causes the family wraps: DNS names the network by itself; the bare
# Connection*Error OSErrors do not (a pool pipe raises them too — see the
# marker notes) and are frame-gated in the traceback rule.
_STDLIB_DNS_NAMES = ("gaierror", "getaddrinfo failed")
_STDLIB_CONN_NAMES = ("ConnectionResetError", "ConnectionRefusedError", "ConnectionAbortedError")
_NETWORK_EXC_NAMES = _AIOHTTP_EXC_NAMES + _STDLIB_DNS_NAMES + _STDLIB_CONN_NAMES
# Reach-event ``error`` fields are exchange-call errors by construction (the
# event is only ever logged around an exchange call): the full list applies.
_NETWORK_EXC_RE = re.compile("|".join(re.escape(n) for n in _NETWORK_EXC_NAMES))
# Traceback terminal lines: transport BY NAME (unconditional) vs the bare
# stdlib Connection*Error, which needs an exchange / aiohttp frame in the
# traceback body (``\b`` so ``ClientConnectionResetError`` is not re-matched
# as the bare name).
_TRANSPORT_EXC_RE = re.compile(
    "|".join(re.escape(n) for n in _AIOHTTP_EXC_NAMES + _STDLIB_DNS_NAMES)
)
_STDLIB_CONN_RE = re.compile(r"\b(?:" + "|".join(_STDLIB_CONN_NAMES) + r")\b")
_TIMEOUT_RE = re.compile(r"\bTimeoutError\b")
# rest.py KalshiApiError str: "HTTP {status} {code}: {message}" — 5xx only.
_EXCHANGE_5XX_RE = re.compile(r"\bHTTP (5\d\d)\b")
_EXCHANGE_API_EXC = "KalshiApiError"
_EXCHANGE_FRAME_RE = re.compile(r"combomaker[\\/]exchange[\\/]|[\\/]aiohttp[\\/]")
_REACH_EVENTS = (
    "startup_reconcile_failed",
    "supervisor_exchange_unreachable",
    "exchange_status_failed",
)
_ERROR_FIELD_RE = re.compile(r'"error": "((?:[^"\\]|\\.)*)"')
_BOOT_MARKER = '"event": "quote_app_starting"'
_TRACEBACK_HEAD = "Traceback (most recent call last):"
_CHAIN_LINES = (
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
)
_REFUSAL_PREFIXES = (
    "config error:",
    "REFUSING TO START:",
    "credentials error:",
    "REFUSING TO QUOTE:",
)
# ``traceback.format_exception_only`` shape: ``[module.]Name[: message]`` at
# column 0. Frames, source echoes and ``~~^^`` carets are indented; JSON log
# lines start with ``{``; human boot lines start with a digit.
_EXC_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?::\s?.*)?$")


def _network_event(line: str) -> str | None:
    """The NETWORK marker carried by one structured log line, else None."""
    if '"event": "kill_switch_halt"' in line and '"reason": "halt_data_stale"' in line:
        return "halt_data_stale"
    for ev in _REACH_EVENTS:
        if f'"event": "{ev}"' in line:
            m = _ERROR_FIELD_RE.search(line)
            err = m.group(1) if m else ""
            hit = _NETWORK_EXC_RE.search(err)
            if hit:
                return f"{ev}: {hit.group(0)}"
            if _TIMEOUT_RE.search(err):
                return f"{ev}: TimeoutError"
            m5 = _EXCHANGE_5XX_RE.search(err)
            if m5:
                return f"{ev}: HTTP {m5.group(1)}"
            return None
    return None


def classify_death(tail: str) -> dict[str, object]:
    """Classify a dead bot's cause of death from its log tail (pure: text in,
    verdict out). Returns ``{"class", "marker", "line"}``; the marker
    derivation and precedence are documented in the section header above.
    Precedence: whichever of {the terminal exception of the last traceback,
    the CLI's own boot refusal print} comes LAST in the tail > an
    exchange-reach / feed-loss event > UNKNOWN."""
    boot = tail.rfind(_BOOT_MARKER)
    if boot >= 0:
        tail = tail[tail.rfind("\n", 0, boot) + 1 :]
    lines = tail.splitlines()

    def verdict(cls: str, marker: str | None, line: str) -> dict[str, object]:
        return {"class": cls, "marker": marker, "line": line.strip()[:240]}

    def transport_hit(text: str, body: str) -> str | None:
        """The transport marker in ``text``: aiohttp family / DNS by name; a
        bare stdlib Connection*Error only with an exchange frame in ``body``."""
        hit = _TRANSPORT_EXC_RE.search(text)
        if hit:
            return hit.group(0)
        hit = _STDLIB_CONN_RE.search(text)
        if hit and _EXCHANGE_FRAME_RE.search(body):
            return f"{hit.group(0)} on exchange call"
        return None

    # The CLI's own boot refusal print (ops/cli.py): printed AFTER the
    # exception it caught, so when it follows the last traceback it is the
    # last word and rule 1 is skipped for that (non-fatal) traceback.
    refusal_at = -1
    for i, ln in enumerate(lines):
        if ln.startswith(_REFUSAL_PREFIXES):
            refusal_at = i
    refusal: str | None = lines[refusal_at] if refusal_at >= 0 else None

    # 1. A Python traceback: the TERMINAL exception (the last one printed —
    #    after any "direct cause" chain) is the cause of death.
    tb_at = -1
    for i, ln in enumerate(lines):
        if ln.startswith(_TRACEBACK_HEAD):
            tb_at = i
    if tb_at >= 0 and tb_at > refusal_at:
        body = "\n".join(lines[tb_at:])
        terminal: str | None = None
        for ln in lines[tb_at + 1 :]:
            if not ln.strip() or ln[0].isspace() or ln[0].isdigit() or ln[0] == "{":
                continue
            if ln.startswith(_TRACEBACK_HEAD) or ln.strip() in _CHAIN_LINES:
                continue
            if _EXC_LINE_RE.match(ln):
                terminal = ln
        if terminal is None:
            marker = transport_hit(body, body)
            if marker:
                return verdict(DEATH_NETWORK, marker, body.splitlines()[-1])
            return verdict(DEATH_CONFIG_CODE, "traceback (terminal line unreadable)", body[-240:])
        marker = transport_hit(terminal, body)
        if marker:
            return verdict(DEATH_NETWORK, marker, terminal)
        if _TIMEOUT_RE.search(terminal) and _EXCHANGE_FRAME_RE.search(body):
            return verdict(DEATH_NETWORK, "TimeoutError on exchange call", terminal)
        if _EXCHANGE_API_EXC in terminal:
            m5 = _EXCHANGE_5XX_RE.search(terminal)
            if m5:
                return verdict(DEATH_NETWORK, f"{_EXCHANGE_API_EXC} HTTP {m5.group(1)}", terminal)
        return verdict(DEATH_CONFIG_CODE, terminal.split(":", 1)[0].strip(), terminal)

    # 2. The bot's own boot refusal print (ops/cli.py) — found above.
    # Every exchange-reach / feed-loss event of THIS boot, newest first.
    network_events: list[tuple[str, str]] = []
    for ln in reversed(lines):
        ev = _network_event(ln)
        if ev:
            network_events.append((ev, ln))
    if refusal is not None:
        prefix = refusal.split(":", 1)[0]
        if prefix == "REFUSING TO QUOTE" and "book_reconciled" in refusal:
            # The gate went red because the startup reconcile could not
            # reach the exchange — ANY such reconcile failure in this boot,
            # not only the newest network event (on 8/6 03:14 the
            # supervisor's own 503 event came AFTER the refusal print).
            reconcile = next(
                (m for m, _ln in network_events if m.startswith("startup_reconcile_failed")),
                None,
            )
            if reconcile:
                return verdict(DEATH_NETWORK, reconcile, refusal)
        return verdict(DEATH_CONFIG_CODE, prefix, refusal)

    # 3. An exchange-reach failure / feed loss with no traceback and no
    #    refusal print (the halt_data_stale deaths of 8/27 01:33-02:04 ET).
    if network_events:
        marker, line = network_events[0]
        return verdict(DEATH_NETWORK, marker, line)
    return verdict(DEATH_UNKNOWN, None, "")


def probe_exchange_reach(
    rest_base_url: str,
    timeout_s: float,
    *,
    resolve: Callable[..., object] = socket.getaddrinfo,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> dict[str, object]:
    """Cheap, unauthenticated reach probe: DNS-resolve the exchange host (the
    exact call that failed on 8/27 — ``getaddrinfo``), then ``GET
    /exchange/status`` (the same auth-free endpoint the bot's own status loop
    polls) under the bot's own per-request wall bound. ``ok`` on an HTTP 200
    with a JSON body, or on ANY 4xx (review fix 2026-09-04): a 4xx is the
    exchange's answer about OUR request — a moved path → 404, a WAF on this
    UA → 403 — not about the network, and the bot's own boot is the real
    test (the same posture as a crashed probe); before the fix a 403 held a
    healthy network down forever (STOP, then waits 242/484/726/900/900…
    with no START ever issued). A 5xx (exchange maintenance) is "reachable
    but not serving" and keeps the wait going — the 2026-08-06 maintenance
    class; a non-JSON 200 (a captive portal's login page) is not ok."""
    host = urlparse(rest_base_url).hostname or rest_base_url
    out: dict[str, object] = {"ok": False, "host": host, "stage": "dns"}
    started = time.monotonic()
    try:
        resolve(host, 443)
    except OSError as exc:  # socket.gaierror is an OSError
        out["error"] = repr(exc)
        out["elapsed_s"] = round(time.monotonic() - started, 2)
        return out
    out["stage"] = "http"
    req = urllib.request.Request(
        rest_base_url.rstrip("/") + _REACH_STATUS_PATH,
        headers={"User-Agent": "combomaker-hang-watchdog", "Accept": "application/json"},
    )
    try:
        with opener(req, timeout=timeout_s) as resp:  # type: ignore[union-attr]
            status = int(getattr(resp, "status", None) or resp.getcode())
            body = resp.read(65536)
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        out["error"] = f"HTTP {exc.code}"
        out["elapsed_s"] = round(time.monotonic() - started, 2)
        if 400 <= exc.code < 500:
            # The exchange ANSWERED (see the docstring): reachable. The
            # relight — the bot's own authenticated boot — is the real test.
            out["stage"] = "http-4xx"
            out["ok"] = True
        return out
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = repr(exc)
        out["elapsed_s"] = round(time.monotonic() - started, 2)
        return out
    out["status"] = status
    out["elapsed_s"] = round(time.monotonic() - started, 2)
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        out["error"] = "non-JSON body"
        return out
    if isinstance(payload, dict):
        out["exchange_active"] = payload.get("exchange_active")
        out["trading_active"] = payload.get("trading_active")
    out["ok"] = status == 200
    return out


def _reach_via_cmd(cmd: str) -> dict[str, object]:
    """Test seam (``--reach-cmd``): a shell command whose exit code 0 means
    "exchange reachable"; stdout is carried as detail."""
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return {"ok": False, "stage": "cmd", "error": repr(exc)}
    return {
        "ok": out.returncode == 0,
        "stage": "cmd",
        "rc": out.returncode,
        "detail": (out.stdout or "").strip()[:200],
    }


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
    # Exchange reach probe consulted before relighting a NETWORK-class boot
    # death (``probe_exchange_reach``); None ⇒ the live prod probe. Test seam.
    reach_probe: Callable[[], dict[str, object]] | None = None

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
        if self.reach_probe is None:
            # Same anchor the CLI passes: the LIVE yaml's env / endpoints,
            # prod when the tree has no local yaml (never a per-env template).
            url = _read_exchange_anchors(self.root)
            self.reach_probe = lambda: probe_exchange_reach(url, _DEFAULT_REQUEST_TIMEOUT_S)

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
    def _corpse_tail(self) -> str:
        """The last 256 KB of the corpse's own log — the newest ``live_*.log``
        by mtime (the pointer file may already have been rewritten by a start
        path; mtime is what identifies the run that just died), ties broken
        by name (Windows stamps file times off the ~15.6 ms system tick, so
        logs written in one burst can share an mtime; live_YYYYMMDD_HHMM
        names sort chronologically). Empty when unreadable. Shared by the
        human-KILL reader and the death classifier so both read the same
        receipt."""
        try:
            logs = sorted(
                self.data_dir.glob("live_*.log"),
                key=lambda p: (p.stat().st_mtime, p.name),
                reverse=True,
            )
            if not logs:
                return ""
            with logs[0].open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 262_144))
                return fh.read().decode("utf-8", errors="replace")
        except OSError:
            return ""  # unreadable log ⇒ no receipt

    def _corpse_death_class(self) -> dict[str, object]:
        """The dead bot's cause-of-death class from its log tail — see
        ``classify_death`` and the marker derivation above it. No readable
        log ⇒ UNKNOWN (fail-closed: latches exactly as before this build)."""
        tail = self._corpse_tail()
        if not tail:
            return {
                "class": DEATH_UNKNOWN,
                "marker": None,
                "line": "",
                "detail": "no readable live log",
            }
        return classify_death(tail)

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
        tail = self._corpse_tail()  # unreadable ⇒ "" ⇒ ordinary relight path
        last_halt = None
        for line in tail.splitlines():
            if '"event": "kill_switch_halt"' in line:
                last_halt = line
        if last_halt and "human-only clear" in last_halt:
            m = re.search(r'"reason": "([a-z_]+)"', last_halt)
            return m.group(1) if m else "human_gated_kill"
        return None

    def _probe_reach(self) -> dict[str, object]:
        """Run the reach probe; a probe that itself CRASHES (a bug, not a
        network verdict — real failures come back as ``ok: False``) reads as
        ``ok: None`` and does not hold a healthy network down (the same
        posture as ``bot_pids`` returning None: never let a broken probe
        fabricate a verdict)."""
        try:
            assert self.reach_probe is not None
            return dict(self.reach_probe())
        except Exception as exc:
            return {"ok": None, "stage": "probe", "error": repr(exc)}

    def _wait_for_exchange_reach(
        self, death: dict[str, object], streak: int
    ) -> list[dict[str, object]]:
        """NETWORK-class boot death: NO latch. Wait the SAME derived backoff
        flap guard 2 uses (threshold x streak, capped 900s — no new number),
        then gate the relight on the exchange being reachable (DNS + an HTTP
        answer — 200 JSON or any 4xx; a 5xx / no route / captive portal is
        not one); every failed probe extends the streak and waits again.
        Retry forever (2026-08-06 ruling, verbatim: "if bot crashes, restart,
        thats it"); a human KILL still latches (checked by the caller after
        this returns). ``time.sleep`` here mirrors guard 2's own wait below
        (the ``sleep`` seam is the poll cadence, not a backoff)."""
        waits: list[dict[str, object]] = []
        while True:
            n = len(waits) + 1
            # streak multiplier: at least 1 (this death), plus one per failed
            # probe — a probe failure IS one more short "run" of the network.
            mult = max(streak, 1) + len(waits)
            wait_s = min(self.threshold_s * mult, 900.0)
            self.log(
                "WARN",
                f"boot death class NETWORK ({death.get('marker')}) — no latch; wait #{n}: "
                f"{wait_s:.0f}s (threshold {self.threshold_s:.0f}s x streak {mult}, "
                "cap 900s) then probing exchange reach (retry-forever; only a human "
                "KILL latches)",
            )
            self._save_state()
            time.sleep(wait_s)
            reach = self._probe_reach()
            ok = reach.get("ok")
            self.log(
                "INFO" if ok else "WARN",
                f"exchange reach probe after wait #{n}: "
                + (
                    "REACHABLE"
                    if ok
                    else "probe crashed — relighting anyway"
                    if ok is None
                    else "UNREACHABLE"
                )
                + f" {json.dumps(reach, default=str)}",
            )
            waits.append({"wait_s": wait_s, "reach": reach})
            if ok is None or ok:
                return waits

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

        # Bookkeeping shared by flap guards 1 and 2: was the run that just
        # died SHORT (healthy span below one detection window), and how long
        # is the trailing streak of short runs. Marked on the previous relight
        # record so the streak survives a watchdog restart.
        short = healthy_span < self.threshold_s
        if self._relights:
            self._relights[-1]["short_run"] = short
        streak = 0
        for r in reversed(self._relights):
            if bool(r.get("short_run")):
                streak += 1
            else:
                break

        # Cause of death, read from the corpse log (forensics on EVERY
        # escalation; decisive only in guard 1 below).
        death = self._corpse_death_class()
        evidence["death_class"] = death
        self.log("INFO", f"corpse death class: {json.dumps(death, default=str)}")
        waited = False

        # Flap guard 1: liveness proof (start_all.ps1's own discriminator) —
        # a run that never wrote the heartbeat never reached liveness. WHICH
        # cause decides what happens next (2026-09-04 build; the 8/27 02:13 ET
        # latch — see the classification section): a NETWORK death is the
        # 8/6-ruled "restart, that's it" class and never latches; CONFIG_CODE
        # and UNKNOWN latch exactly as before (a config-validation exit that
        # dies in <1s IS a boot loop; a cause we cannot read stays fail-closed).
        if not hb:
            if death["class"] != DEATH_NETWORK:
                self._latch_halt(
                    "run never reached liveness (no heartbeat) — relighting would "
                    f"boot-loop [death class {death['class']}: {death.get('marker')}]",
                    evidence,
                )
                return "halt_boot_loop"
            waits = self._wait_for_exchange_reach(death, streak)
            evidence["reach_waits"] = len(waits)
            waited = True

        # Flap guard 2 — REWORKED 2026-08-06 (operator ruling, verbatim: "if
        # bot crashes, restart, thats it"). The permanent flap LATCH twice kept
        # a healthy recovery down for HOURS after a transient outside cause
        # ended (Kalshi's Thu 03-05 ET maintenance 503s on 8/6; the daily halt
        # on 8/5) — the latch turned a bounded outage into an unbounded one.
        # Consecutive short-lived relights now BACK OFF instead of latching:
        # wait = threshold x streak, capped 900s (the documented ~2h
        # maintenance window is survivable in ~8 capped retries), retrying
        # forever. A HUMAN-gated start refusal still latches (guard below,
        # unchanged). Guard 1's NETWORK path already waited this same derived
        # backoff (and more: it gated on the reach probe) — never wait twice.
        if streak >= 2 and not waited:
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

        record: dict[str, object] = {
            "at": _now_utc().isoformat(),
            "kind": kind,
            "healthy_span_s": round(healthy_span, 1),
        }
        if not hb:
            record["boot_death"] = death["class"]
            record["reach_waits"] = evidence.get("reach_waits")
        self._relights.append(record)
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
            p.add_argument("--reach-cmd", default=None)  # rc 0 = exchange reachable
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

    rest_base_url = _read_exchange_anchors(root)
    reach_cmd = getattr(args, "reach_cmd", None)

    def reach_probe() -> dict[str, object]:
        if reach_cmd:
            return _reach_via_cmd(reach_cmd)
        return probe_exchange_reach(rest_base_url, _DEFAULT_REQUEST_TIMEOUT_S)

    probes = Probes(root=root, data_dir=data_dir, probe_cmd=args.probe_cmd)
    dog = Watchdog(
        root=root,
        data_dir=data_dir,
        probes=probes,
        threshold_s=threshold,
        poll_s=poll,
        stop_cmd=stop_cmd,
        start_cmd=start_cmd,
        reach_probe=reach_probe,
    )
    dog.log(
        "INFO",
        "derivation: "
        + json.dumps(
            {
                **{
                    k: derivation[k]
                    for k in (
                        "threshold_s",
                        "max_healthy_gap_s",
                        "max_gap_log",
                        "internal_chain_floor_s",
                        "logs_measured",
                    )
                },
                "reach_host": urlparse(rest_base_url).hostname,
                "reach_timeout_s": _DEFAULT_REQUEST_TIMEOUT_S,
            }
        ),
    )
    dog.run(max_cycles=args.max_cycles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
