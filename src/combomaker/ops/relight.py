"""AUTO-RELIGHT for LIFECYCLE-class halts — the process that OUTLIVES the bot.

Why this module exists
----------------------
A market's LIFECYCLE state moving (an exchange trading pause/unpause, a
``close_time`` rewrite) is answered by a market-scoped QUARANTINE, not a
whole-book kill (``risk/quarantine.py``). But a quarantine we could not ENFORCE
— our resting quotes could not be PROVEN off that market — is promoted to a
whole-bot halt on the next status tick, and that halt needed a human to clear
even though the cure for "we do not know what is resting on the exchange" is
exactly the startup exchange reconcile a restart already performs.

So this module restarts the bot for that ONE halt class, and refuses everything
else. It is the bot's PARENT, occupying the slot the bare ``combomaker.ops.cli
run`` occupied inside START_BOT's bot window, because:

* a parent cannot be reaped by its child's exit — it outlives the halt BY
  CONSTRUCTION, with no PID file, no lifetime assumption, no re-parenting. (The
  safety supervisor cannot do this job: it is the bot's CHILD and is terminated
  in the bot's shutdown ``finally`` — measured dead 32 ms after the 2026-07-26
  18:15Z metadata halt.)
* only the parent observes the child's EXIT CODE, which is load-bearing:
  ``cli.main`` returns 3 on a red preflight (a FAILED reconcile), 2 on a
  credentials error, 0 on a clean halt-stop.
* it holds NO exchange credential and opens NO socket. It cannot cancel, cannot
  quote, cannot 429. Its whole I/O surface is: spawn a process, and read/write
  files under ``data/``.

WHAT IT NEVER DOES
------------------
It NEVER reads a KILL file for attribution and NEVER deletes one. A lifecycle
halt provably writes no KILL (``QuoteApp.on_halt`` drops the reconcile marker
and stops; nothing in the bot writes KILL), so every KILL that can exist comes
from the supervisor, the ``combomaker halt`` CLI, or a human — all outside the
relightable class by construction. The rule is therefore "relight only when
there is NO KILL" (G5), not "clear the KILL I can attribute": there is nothing
to attribute and no attribution bug available.

Its total write set is ``{relight_ledger.jsonl, relight_status.json}`` plus
``unlink(halt_receipt.json)``. It cannot write or clear ``needs_reconcile`` or
``KILL``, so it cannot skip a reconcile — and G6 REQUIRES the reconcile marker
to be present, so it can only ever relight *into* a mandatory reconcile.

No tuned numbers
----------------
There is no retry count, no backoff schedule, and no boot timeout constant.

* **B2 (novelty)** — one relight per distinct ROOT-CAUSE SIGNATURE per session.
  The signature is computed at the failure site (the set of distinct withdrawal
  failure kinds), never chosen here. A second halt with the SAME signature means
  the bot has demonstrated it cannot self-cure that mode ⇒ human. The session
  total is bounded by the number of distinct exchange failure modes actually
  observed, each granted exactly one self-cure attempt.
* **B3 (amortization)** — grant relight *i+1* iff ``work_i >= cost_i``. "A
  relight must be paid for by observed productive work" has exactly ONE
  scale-free breakeven; any multiple (1.5x, 3x) would be a hand-set number. A
  true crash loop has ``work -> 0`` while ``cost`` stays at the measured boot
  time, so it is refused on the FIRST repeat: the mechanism can emit at most one
  unpaid relight, ever. Duty cycle can never fall below 50%.
* **the boot bound** is DELEGATED, not invented: once the child launches its
  supervisor, the supervisor's own derived bound kills a hung bot and writes
  KILL (⇒ G5 ⇒ terminal). Before the supervisor exists, this module applies the
  SAME single operator anchor — ``supervisor.heartbeat_timeout_s`` — to the
  bot's own heartbeat, with the same latch shape ``ProgressReader`` uses. One
  anchor, already owned by the operator, reused.

PRODUCTIVE is defined as: ``needs_reconcile`` ABSENT (the exchange reconcile
provably succeeded) AND the heartbeat is not wedged AND no registered loop is
stalled. So ``work_i`` cannot begin accruing until the reconcile has proven
itself — amortization and reconcile-proof are the same measurement.

Escalation is TERMINAL. On any refusal this process ledgers the decision, logs
``relight_terminal_escalation``, and exits non-zero. The BOT window is ``cmd
/k``, so it stays open with the reason on screen. There is no poll-and-hope mode.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from combomaker.core.clock import Clock, SystemClock
from combomaker.ops.logging import configure_logging, get_logger
from combomaker.risk.heartbeat import HeartbeatReader, ReconcileMarker
from combomaker.risk.progress import ProgressReader, progress_path

log = get_logger(__name__)

# ── Shared wire contract with the halting bot (ops/quote_app.py) ───────────── #

HALT_RECEIPT_FILENAME = "halt_receipt.json"
HALT_RECEIPT_SCHEMA_VERSION = 1
#: The nonce the relighter mints per child and passes by ENV ONLY. A receipt is
#: only ever this run's if it carries this run's nonce (gate G2) — a stale file,
#: a second stack, or a hand-written receipt carries a different one.
RUN_ID_ENV = "COMBOMAKER_RUN_ID"

RELIGHT_LEDGER_FILENAME = "relight_ledger.jsonl"
RELIGHT_STATUS_FILENAME = "relight_status.json"

#: The ONE relightable halt class. A frozenset in CODE, deliberately not config:
#: the same shape as ``quote_app._HARD_HALT_REASONS``, reviewable in a diff and
#: not tunable in YAML.
#:
#: ``lifecycle_quarantine_unenforced`` = a market's lifecycle state moved and we
#: could not PROVE our resting quotes came off it. The cure IS the startup
#: exchange reconcile, and that remedy fails closed. The two adjacent lifecycle
#: failures stay human: ``unarmed`` is a wiring bug (a restart reproduces it)
#: and an unmodelled status string is an exchange-semantics change (a restart
#: cannot know more than we did).
_RELIGHTABLE_HALT_CLASSES: frozenset[str] = frozenset({"lifecycle_quarantine_unenforced"})

#: The reason the receipt must carry, and the body ``mark_reconcile_on_hard_halt``
#: must independently have written into ``needs_reconcile`` (gate G6: two
#: separate code paths have to agree).
_RELIGHTABLE_REASON = "halt_metadata_change"


# ── The decision ──────────────────────────────────────────────────────────── #


@dataclass(frozen=True, slots=True)
class RelightDecision:
    """Grant or refuse, plus the gate that decided and why. Every field is
    ledgered, so any decision is reconstructible offline."""

    grant: bool
    gate: str
    detail: str
    signature: str | None = None
    halt_class: str | None = None
    tickers: tuple[str, ...] = ()


@dataclass(slots=True)
class RelightSession:
    """The relighter's OWN lifetime — i.e. the operator's launch->stop span, an
    existing operational boundary, not a new number. A human restart resets it,
    which is correct: a human looked."""

    granted_signatures: set[str] = field(default_factory=set)
    relights: int = 0
    dead_time_s: float = 0.0
    productive_time_s: float = 0.0


class RelightPolicy:
    """PURE decision function: receipt + exit code + timings + session state ->
    grant/refuse + gate name. No I/O, no clock, no filesystem — so every gate is
    a table-driven unit test.

    DEFAULT DENY. Any missing, unparseable, or wrongly-typed field is a refusal.
    """

    def decide(
        self,
        *,
        receipt: Any,
        expected_run_id: str,
        expected_pid: int,
        exit_code: int | None,
        kill_present: bool,
        reconcile_marker_present: bool,
        reconcile_marker_body: str | None,
        session: RelightSession,
        cost_s: float | None,
        work_s: float | None,
    ) -> RelightDecision:
        # G1 — the receipt exists and parses. The relighter unlinks the receipt
        # BEFORE every launch, so mere presence proves it was written during
        # THIS run.
        if receipt is None:
            return RelightDecision(
                False, "G1", "no halt receipt on disk (or unreadable) after child exit"
            )
        if not isinstance(receipt, dict):
            return RelightDecision(False, "G1", "halt receipt is not a JSON object")
        if receipt.get("schema_version") != HALT_RECEIPT_SCHEMA_VERSION:
            return RelightDecision(
                False,
                "G1",
                f"halt receipt schema_version={receipt.get('schema_version')!r}, "
                f"expected {HALT_RECEIPT_SCHEMA_VERSION}",
            )

        # G2 — the run nonce. Passed by env only, minted per child.
        if not expected_run_id or receipt.get("run_id") != expected_run_id:
            return RelightDecision(
                False,
                "G2",
                f"run_id mismatch: receipt={receipt.get('run_id')!r} "
                f"expected={expected_run_id!r}",
            )

        # G3 — the process we actually spawned. Independent of G2 (the nonce is
        # inherited by the whole subtree; this is a spawn-identity fact).
        #
        # WHY ``ppid`` COUNTS (measured 2026-07-27, not assumed): on Windows the
        # venv's ``python.exe`` is a LAUNCHER SHIM that re-spawns the real
        # interpreter as a child with an identical command line — the very
        # topology ``start_all.ps1`` compensates for when it counts bot ROOTS.
        # So the pid we spawned is the halting bot's PARENT. Comparing only
        # ``os.getpid()`` refuses EVERY real receipt, which would have made this
        # whole feature a silent no-op live. Exactly ONE shim level is accepted —
        # a deeper chain still refuses, loudly, rather than being waved through.
        if expected_pid not in (receipt.get("pid"), receipt.get("ppid")):
            return RelightDecision(
                False,
                "G3",
                f"spawn identity mismatch: receipt pid={receipt.get('pid')!r} "
                f"ppid={receipt.get('ppid')!r}, spawned={expected_pid}",
            )

        # G4 — a CLEAN halt-stop. 3 = red preflight (a FAILED reconcile),
        # 2 = credentials, anything else = crash/abnormal.
        if exit_code != 0:
            return RelightDecision(
                False, "G4", f"child exit code {exit_code} (a clean halt-stop exits 0)"
            )

        # G5 — no KILL on disk. Excludes EVERY kill writer in the tree
        # (supervisor stall/wedge kill, ``combomaker halt``, a human) with zero
        # attribution logic.
        if kill_present:
            return RelightDecision(
                False, "G5", "KILL file present — never relightable, a human owns it"
            )

        # G6 — the reconcile marker, written by a DIFFERENT code path
        # (``mark_reconcile_on_hard_halt``) than the receipt. Two independent
        # writers must agree, AND its presence is what guarantees the relight
        # runs INTO a mandatory exchange reconcile.
        if not reconcile_marker_present:
            return RelightDecision(
                False, "G6", "needs_reconcile marker absent — nothing would force a reconcile"
            )
        if (reconcile_marker_body or "").strip() != _RELIGHTABLE_REASON:
            return RelightDecision(
                False,
                "G6",
                f"needs_reconcile body {(reconcile_marker_body or '').strip()!r} "
                f"disagrees with the receipt reason {_RELIGHTABLE_REASON!r}",
            )

        # G7 — RE-DERIVE the claim from the receipt's own evidence. The reader
        # never trusts ``halt_class``: a receipt CLAIMING lifecycle while
        # carrying a moved settlement fingerprint is rejected by its contents.
        verdict = self._rederive(receipt)
        if verdict is not None:
            return verdict

        # G8 — policy: is this class relightable at all?
        halt_class = receipt.get("halt_class")
        if halt_class not in _RELIGHTABLE_HALT_CLASSES:
            return RelightDecision(
                False,
                "G8",
                f"halt_class {halt_class!r} is not in _RELIGHTABLE_HALT_CLASSES "
                f"{sorted(_RELIGHTABLE_HALT_CLASSES)}",
            )

        signature = receipt.get("root_cause_signature")
        if not isinstance(signature, str) or not signature:
            return RelightDecision(False, "G7", "root_cause_signature missing/blank")
        tickers = self._tickers(receipt)

        # B2 — NOVELTY. A second halt with the same root cause means the bot has
        # demonstrated it cannot self-cure that mode.
        if signature in session.granted_signatures:
            return RelightDecision(
                False,
                "B2",
                f"root cause {signature!r} already relit once this session — the bot "
                "has demonstrated it cannot self-cure this failure mode",
                signature=signature,
                halt_class=str(halt_class),
                tickers=tickers,
            )

        # B3 — AMORTIZATION. The relight must be PAID FOR by observed productive
        # work: the work it bought must be at least as long as the work it cost.
        # The first relight of a session has no prior run to amortize (cost_s is
        # None) and is granted on B1/B2 alone.
        if cost_s is not None:
            if work_s is None:
                return RelightDecision(
                    False,
                    "B3",
                    f"previous run never became PRODUCTIVE (cost {cost_s:.1f}s bought "
                    "0.0s of proven work) — this is a crash loop, not a self-cure",
                    signature=signature,
                    halt_class=str(halt_class),
                    tickers=tickers,
                )
            if work_s < cost_s:
                return RelightDecision(
                    False,
                    "B3",
                    f"unamortized: previous run cost {cost_s:.1f}s to boot and bought "
                    f"only {work_s:.1f}s of proven productive quoting",
                    signature=signature,
                    halt_class=str(halt_class),
                    tickers=tickers,
                )

        return RelightDecision(
            True,
            "grant",
            f"lifecycle quarantine could not be proven enforced ({signature}); the "
            "startup exchange reconcile IS the remedy and it fails closed",
            signature=signature,
            halt_class=str(halt_class),
            tickers=tickers,
        )

    # -- G7 internals -------------------------------------------------------- #

    @staticmethod
    def _tickers(receipt: dict[str, Any]) -> tuple[str, ...]:
        """The markets named in the halt's own detail string — parsed from the
        detail the BREAKER built, not from a field the receipt chose."""
        detail = receipt.get("verdict_detail")
        if not isinstance(detail, str) or ":" not in detail:
            return ()
        _, _, names = detail.partition(":")
        return tuple(n.strip() for n in names.split(",") if n.strip())

    def _rederive(self, receipt: dict[str, Any]) -> RelightDecision | None:
        """Return a REFUSAL if the receipt's evidence does not itself establish
        an unenforced-lifecycle-quarantine halt; ``None`` if it does."""
        reason = receipt.get("reason")
        if reason != _RELIGHTABLE_REASON:
            return RelightDecision(
                False, "G7", f"halt reason {reason!r} is not {_RELIGHTABLE_REASON!r}"
            )
        # The taxonomy tripwire shares this ReasonCode (breakers.detect_metadata_change
        # returns HALT_METADATA_CHANGE for it too) — reason alone can never attribute.
        if receipt.get("tripwire_hit") is not None:
            return RelightDecision(
                False,
                "G7",
                f"taxonomy tripwire hit ({receipt.get('tripwire_hit')!r}) — a pinned "
                "impossible shape became constructible; the validator changed",
            )
        evidence = receipt.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            return RelightDecision(False, "G7", "receipt carries no evidence")
        tickers = self._tickers(receipt)
        if not tickers:
            return RelightDecision(
                False, "G7", "no markets parseable from verdict_detail"
            )
        for ticker in tickers:
            entry = evidence.get(ticker)
            if not isinstance(entry, dict):
                return RelightDecision(
                    False, "G7", f"no evidence entry for halted market {ticker}"
                )
            checks: tuple[tuple[str, object, object], ...] = (
                ("origin", entry.get("origin"), "quarantine_unenforced"),
                ("status_class", entry.get("status_class"), "lifecycle"),
                ("regraded", entry.get("regraded"), False),
                ("settlement_moved", entry.get("settlement_moved"), False),
                ("quarantine_armed", entry.get("quarantine_armed"), True),
            )
            for name, got, want in checks:
                if got is not want and got != want:
                    return RelightDecision(
                        False,
                        "G7",
                        f"{ticker}: {name}={got!r}, a relightable lifecycle halt "
                        f"requires {want!r}",
                    )
            prior = entry.get("settlement_fp_prior")
            new = entry.get("settlement_fp_new")
            if not isinstance(prior, str) or not isinstance(new, str) or prior != new:
                return RelightDecision(
                    False,
                    "G7",
                    f"{ticker}: SETTLEMENT fingerprint moved — what the position PAYS "
                    "changed; this is never auto-relightable",
                )
        return None


# ── The runner ────────────────────────────────────────────────────────────── #


def build_child_argv(
    *, env: str, mode: str, confirm_live: bool, config: str | None
) -> list[str]:
    """Build the bot's argv HERE rather than carrying it on our own command line.

    ``start_all.ps1``'s post-launch duplicate check counts processes matching
    ``combomaker\\.ops\\.cli run`` whose PARENT is not itself in the matched set.
    If the relighter carried the bot argv it would match and be counted as a
    second root. Building it means: the relighter does not match; the bot child
    matches with a parent outside the set (counted); the venv shim child matches
    with a parent inside the set (excluded). ``bots.Count == 1``, unchanged.

    Same idiom as ``quote_app.supervisor_launch_cmd``, which forwards the bot's
    own ``--config`` for exactly the same reason."""
    argv = [sys.executable, "-m", "combomaker.ops.cli", "run", "--env", env, "--mode", mode]
    if confirm_live:
        argv.append("--confirm-live")
    if config:
        argv += ["--config", config]
    return argv


class Relighter:
    """Spawn, watch, decide, ledger. Never raises past ``run()``."""

    def __init__(
        self,
        *,
        data_dir: Path,
        kill_file: Path,
        child_argv: list[str],
        wedge_timeout_s: float,
        clock: Clock | None = None,
        spawn: Any = None,
        sleep: Any = None,
    ) -> None:
        self._data_dir = data_dir
        self._kill_file = kill_file
        self._child_argv = child_argv
        self._wedge_timeout_s = wedge_timeout_s
        self._clock = clock or SystemClock()
        self._spawn = spawn or (lambda argv, env: subprocess.Popen(argv, env=env))
        self._sleep = sleep or time.sleep
        self._receipt_path = data_dir / HALT_RECEIPT_FILENAME
        self._ledger_path = data_dir / RELIGHT_LEDGER_FILENAME
        self._status_path = data_dir / RELIGHT_STATUS_FILENAME
        self._marker = ReconcileMarker(data_dir / "needs_reconcile")
        self._heartbeat = HeartbeatReader(self._clock, data_dir / "heartbeat.txt")
        self._progress = ProgressReader(self._clock, progress_path(data_dir))
        self._policy = RelightPolicy()
        self._session = RelightSession()

    # -- filesystem (the ENTIRE write surface) ------------------------------- #

    def _clear_receipt(self) -> None:
        """G1's premise: unlink BEFORE every launch, so a receipt found after
        the child exits was written during THIS run."""
        try:
            self._receipt_path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - exotic FS failure
            log.error("relight_receipt_clear_failed", error=repr(exc))

    def _read_receipt(self) -> Any:
        try:
            raw = self._receipt_path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _kill_present(self) -> bool:
        """Fail-closed: a filesystem we cannot read is one we cannot trust to
        say 'no kill'."""
        try:
            return self._kill_file.exists()
        except OSError:  # pragma: no cover - exotic FS failure
            return True

    def _marker_body(self) -> str | None:
        try:
            return (self._data_dir / "needs_reconcile").read_text(encoding="utf-8")
        except OSError:
            return None

    def _ledger(self, record: dict[str, Any]) -> None:
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self._ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:  # pragma: no cover - disk failure path
            log.error("relight_ledger_write_failed", error=repr(exc))

    def _status(self, record: dict[str, Any]) -> None:
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            self._status_path.write_text(
                json.dumps(record, default=str, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - disk failure path
            log.error("relight_status_write_failed", error=repr(exc))

    # -- productivity ------------------------------------------------------- #

    def is_productive(self) -> bool:
        """PRODUCTIVE = the exchange reconcile PROVABLY succeeded (the marker is
        gone — only ``_block_restart_until_reconciled`` clears it, and only on a
        successful round-trip) AND the process is alive AND every registered
        loop is advancing.

        Defining it this way is what makes B3 and the reconcile-proof the SAME
        measurement: ``work`` cannot start accruing until the book is proven."""
        if self._marker.is_set():
            return False
        if self._heartbeat.is_wedged(self._wedge_timeout_s):
            return False
        return self._progress.wedged_detail() is None

    def _heartbeat_stamp(self) -> str | None:
        """The heartbeat file's RAW contents, or None if absent/unreadable.

        Compared against the value captured just BEFORE the child was spawned, so
        the relighter can tell "THIS child has beaten" from "the dead bot's last
        beat is still on disk". Same idiom as ``_await_supervisor_heartbeat``,
        which likewise waits for a beat written AFTER launch rather than merely
        for the file to exist."""
        try:
            return (self._data_dir / "heartbeat.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def _watch(
        self, proc: Any, poll_s: float, prior_beat: str | None = None
    ) -> tuple[int | None, float | None, float | None]:
        """Poll the child to exit, timing its BOOT COST and its PRODUCTIVE WORK.

        Returns ``(exit_code, cost_s, work_s)``. ``cost_s`` is launch -> first
        productive instant; ``work_s`` is that instant -> exit. ``work_s`` is
        None when the run never became productive at all — a crash loop, refused
        by B3.

        The un-productive branch needs no timer of its own: if the child hangs
        after launching its supervisor, the supervisor's derived bound kills it
        and writes KILL (G5, terminal). If it hangs before, we apply the SAME
        operator anchor to its heartbeat below — but only once the child COULD
        have beaten. See ``_boot_wedged`` for why that qualifier is load-bearing."""
        launched_at = self._clock.monotonic_ns()
        productive_at: int | None = None
        latched = False
        # HEARTBEAT LATCH: flips True the first time THIS CHILD is seen to have
        # written a beat — i.e. a stamp DIFFERENT from the one on disk when it
        # was spawned. Until then the child is "still starting" and is never
        # judged, the same latch shape ``ProgressReader`` uses.
        #
        # Comparing stamps rather than freshness is what makes the latch honest
        # on a RELIGHT: the dead bot beat right up to its halt, so its leftover
        # file is typically only ~10 s old at relaunch and reads as NOT wedged.
        # A freshness-only latch would therefore latch on the CORPSE's beat at
        # t=0 and then fire the bound the moment that corpse aged past the
        # anchor — abandoning a healthy child even EARLIER than the from-launch
        # timer did. See ``_boot_wedged``.
        beat_seen = False
        while True:
            code = proc.poll()
            if code is not None:
                break
            if productive_at is None and self.is_productive():
                productive_at = self._clock.monotonic_ns()
                latched = True
                log.info(
                    "relight_child_productive",
                    cost_s=round((productive_at - launched_at) / 1e9, 2),
                )
            elif productive_at is None and not latched:
                stamp = self._heartbeat_stamp()
                if stamp is not None and stamp != prior_beat:
                    beat_seen = True          # THIS child has provably beaten
                if beat_seen and self._heartbeat.is_wedged(self._wedge_timeout_s):
                    if self._boot_wedged(launched_at):
                        return (proc.poll(), None, None)
            self._sleep(poll_s)
        exited_at = self._clock.monotonic_ns()
        if productive_at is None:
            return (code, None, None)
        return (
            code,
            (productive_at - launched_at) / 1e9,
            (exited_at - productive_at) / 1e9,
        )

    def _boot_wedged(self, launched_at: int) -> bool:
        """A child that WAS beating has gone quiet past the anchor, before ever
        becoming productive.

        WHY THE LATCH IS LOAD-BEARING (measured 2026-07-27, the whole point of
        this method). As first written this bound ran from LAUNCH, and an absent
        or stale heartbeat is WEDGED by design (fail-closed). But the bot emits
        its first ``heartbeat.beat()`` only after ``_startup_book_risk_snapshot``,
        i.e. after the startup exchange reconcile — and across **all 26 recorded
        boots** (``data/live_2026072*.log``) launch -> first beat takes
        **34.2 s - 46.1 s**. Not one is inside the operator's 30 s anchor.

        So the bound fired on every healthy boot, of BOTH kinds: after a relight
        (leftover stale heartbeat) and on a plain ``START_BOT`` (``start_all.ps1``
        deletes ``heartbeat.txt``, and absent reads as wedged too). Measured
        end-to-end: the relighter abandoned a healthy child at exactly t=30.0 s,
        refused at **G1** citing "no halt receipt ... after child exit" while the
        child had **not exited**, exited 1, and left the bot running **ORPHANED**
        with no parent — strictly worse than having no relighter at all.

        The fix is the latch this class always claimed to use ("an absent
        heartbeat before the first sighting is still starting, never a kill") and
        that ``ProgressReader`` really does use: judge the child only once it has
        been SEEN beating. Before that first sighting there is nothing to protect
        — the bot cannot quote until its preflight is green, and the preflight is
        downstream of the first beat — so waiting is free, and it is what the
        pre-relighter world did anyway.

        After the first beat the bound is redundant belt-and-braces: the child
        launches the supervisor immediately after that beat, and the supervisor's
        own derived bound kills a hung bot and writes KILL (=> G5 => terminal).
        That delegation is the design this module's header already describes.

        No new number: the operator's single ``supervisor.heartbeat_timeout_s``
        anchor is unchanged. Only the precondition for consulting it changed.
        """
        log.error(
            "relight_child_boot_wedged",
            since_launch_s=round((self._clock.monotonic_ns() - launched_at) / 1e9, 1),
            wedge_timeout_s=self._wedge_timeout_s,
            detail="child was beating and then went quiet past the operator wedge "
            "anchor before it ever became productive — leaving it to the human",
        )
        return True

    # -- the loop ------------------------------------------------------------ #

    def run(self, *, poll_s: float = 1.0) -> int:
        attempt = 0
        cost_s: float | None = None
        work_s: float | None = None
        while True:
            attempt += 1
            run_id = uuid.uuid4().hex
            self._clear_receipt()
            child_env = dict(os.environ)
            child_env[RUN_ID_ENV] = run_id
            # Captured BEFORE the spawn so any beat we later see is provably
            # this child's, not the dead bot's leftover (see ``_watch``).
            prior_beat = self._heartbeat_stamp()
            proc = self._spawn(self._child_argv, child_env)
            log.info(
                "relight_child_launched",
                run_id=run_id,
                pid=proc.pid,
                attempt_index=attempt,
                argv_hash=f"{abs(hash(tuple(self._child_argv))):x}",
            )
            exit_code, cost_s, work_s = self._watch(proc, poll_s, prior_beat)
            duty = None
            if cost_s is not None and work_s is not None and (cost_s + work_s) > 0:
                duty = round(work_s / (cost_s + work_s), 3)
            log.warning(
                "relight_child_exited",
                run_id=run_id,
                exit_code=exit_code,
                work_s=None if work_s is None else round(work_s, 2),
                duty_cycle=duty,
            )
            if cost_s is not None:
                self._session.dead_time_s += cost_s
            if work_s is not None:
                self._session.productive_time_s += work_s

            receipt = self._read_receipt()
            decision = self._policy.decide(
                receipt=receipt,
                expected_run_id=run_id,
                expected_pid=proc.pid,
                exit_code=exit_code,
                kill_present=self._kill_present(),
                reconcile_marker_present=self._marker.is_set(),
                reconcile_marker_body=self._marker_body(),
                session=self._session,
                cost_s=cost_s,
                work_s=work_s,
            )
            record = {
                "at": self._clock.now().isoformat(),
                "run_id": run_id,
                "pid": proc.pid,
                "attempt_index": attempt,
                "exit_code": exit_code,
                "cost_s": None if cost_s is None else round(cost_s, 3),
                "work_s": None if work_s is None else round(work_s, 3),
                "duty_cycle": duty,
                "grant": decision.grant,
                "gate": decision.gate,
                "detail": decision.detail,
                "signature": decision.signature,
                "halt_class": decision.halt_class,
                "tickers": list(decision.tickers),
                "kill_present": self._kill_present(),
                "reconcile_marker_present": self._marker.is_set(),
                "session_relights": self._session.relights,
                "receipt": receipt,
            }
            self._ledger(record)
            self._status(record)

            if not decision.grant:
                log.error(
                    "relight_refused",
                    refusal_gate=decision.gate,
                    detail=decision.detail,
                    run_id=run_id,
                    receipt=receipt,
                )
                log.error(
                    "relight_terminal_escalation",
                    refusal_gate=decision.gate,
                    why=decision.detail,
                    human_must_check=(
                        "KILL file, data/needs_reconcile, data/halt_receipt.json, and "
                        "the bot log's last kill_switch_halt line; then START_BOT.bat"
                    ),
                    last_log=str(self._data_dir / "CURRENT_LOG.txt"),
                    session_relights=self._session.relights,
                )
                return 1

            assert decision.signature is not None
            self._session.granted_signatures.add(decision.signature)
            self._session.relights += 1
            log.error(  # deliberately LOUD: an auto-restart is never routine
                "relight_granted",
                run_id=run_id,
                halt_class=decision.halt_class,
                tickers=list(decision.tickers),
                signature=decision.signature,
                cost_s=None if cost_s is None else round(cost_s, 2),
                work_s=None if work_s is None else round(work_s, 2),
                session_relights=self._session.relights,
            )
            total = self._session.dead_time_s + self._session.productive_time_s
            log.info(
                "relight_session_summary",
                relights=self._session.relights,
                dead_time_s=round(self._session.dead_time_s, 1),
                productive_time_s=round(self._session.productive_time_s, 1),
                duty_cycle=(
                    None if total <= 0 else round(self._session.productive_time_s / total, 3)
                ),
                signatures=sorted(self._session.granted_signatures),
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="combomaker-relight")
    parser.add_argument("--env", default="demo")
    parser.add_argument("--mode", default="quote")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--config", default=None)
    # Deliberately NO defaults: both paths come from the BOT'S OWN CONFIG unless
    # explicitly overridden, so the relighter can never watch a different
    # data_dir / KILL file than the bot it supervises.
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--kill-file", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    # The ONE operator anchor this module reads, loaded from the bot's OWN
    # config so the relighter and the supervisor judge wedge by the same number.
    from combomaker.ops.config import Env, load_config

    env = Env(args.env)
    config_path = Path(args.config) if args.config else Path("config") / f"{env.value}.yaml"
    # ONE load, THE BOT'S OWN CONFIG — the same file the child will re-load, for
    # the same reason ``supervisor_launch_cmd`` forwards ``--config``: a
    # supervisor override living only in the armed local YAML must apply to
    # everything that enforces it. Nothing is defaulted independently here.
    config = load_config(config_path, env=env)
    wedge_timeout_s = config.supervisor.heartbeat_timeout_s
    relighter = Relighter(
        data_dir=Path(args.data_dir) if args.data_dir else config.data_dir,
        kill_file=Path(args.kill_file) if args.kill_file else config.kill_file,
        child_argv=build_child_argv(
            env=args.env,
            mode=args.mode,
            confirm_live=bool(args.confirm_live),
            config=args.config,
        ),
        wedge_timeout_s=wedge_timeout_s,
    )
    log.info(
        "relight_supervisor_starting",
        child_argv=relighter._child_argv,
        wedge_timeout_s=wedge_timeout_s,
        data_dir=str(relighter._data_dir),
        kill_file=str(relighter._kill_file),
        relightable=sorted(_RELIGHTABLE_HALT_CLASSES),
    )
    return relighter.run()


if __name__ == "__main__":
    raise SystemExit(main())
