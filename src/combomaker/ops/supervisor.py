"""External (out-of-process) safety supervisor (RISK_BUILD_PLAN Phase 6).

The last line of defense. This is a SEPARATE process from the bot: it watches the
bot's heartbeat file and, if the heartbeat goes stale (the bot is presumed
wedged — crash, deadlock, GIL stall, network partition), it uses its OWN REST
credential to EMERGENCY CANCEL-ALL every resting quote on the exchange AND writes
the KILL file, so a revived bot halts immediately. Because it runs in a separate
process (deployment: a separate host + a distinct credential), the kill path does
NOT depend on the bot's own host being healthy — an in-process KILL file can't
survive the host that hosts it deadlocking.

Design pillars (spec §1):

- HEARTBEAT ("ALIVE"): the bot's DEDICATED liveness task writes ``heartbeat.txt``
  on a fixed cadence and does nothing else; the supervisor reads its age against
  the supervisor's own clock. **LOG-ONLY since 2026-07-27** — a stale heartbeat
  emits ``supervisor_heartbeat_stale`` and decides NOTHING. It went 0-for-8 as a
  kill trigger (every one of the 8 emergency kills had ``exchange_reachable=true``
  on a live bot; the 07-27 kill fired at age=30.9s vs a 30.5s bound, one tick
  over, on a bot that withdrew all 67 quotes 4.2s later). The reader is kept for
  forensics and for the bot's own preflight.
- LOOP PROGRESS ("WORKING") — **the sole kill axis**: every loop that matters
  publishes a last-progress age to ``loop_progress.json`` (``risk/progress.py``);
  the supervisor escalates when any loop exceeds its own DERIVED stall bound.
  It strictly DOMINATES the liveness axis — anything that truly stops the event
  loop also stops every registered loop advancing — and it additionally NAMES the
  stalled loop instead of asserting a corpse. See ``risk/progress.py`` for the
  2026-07-26T20:12:54Z false-kill this replaced (a healthy, quoting bot whose
  maintenance loop sat in a 30s non-beating await while the event loop never
  stalled).
- EMERGENCY CANCEL-ALL: on wedged (or an explicit trigger), cancel every resting
  quote via the supervisor's OWN REST client, THEN write KILL + drop the
  ``needs_reconcile`` marker. FAIL-CLOSED: if the exchange is unreachable, we
  STILL write KILL + the marker + alarm — a supervisor that can't cancel must at
  least stop the bot from resuming.
- RESERVED API WRITE BUDGET: the supervisor throttles its OWN writes to a reserved
  budget (a token bucket) so it can always act even under a 429 storm on the
  shared bot budget — it never spends the bot's tokens and never exhausts the
  shared pool. The budget is sized so the emergency cancels always fit.
- CREDENTIAL ROTATE: the supervisor loads a DISTINCT credential (env-only) so a
  compromised / rate-limited BOT credential can't disable the kill path. The
  default is fail-closed: absent a dedicated credential the supervisor REFUSES to
  claim it has a working kill path (it still writes KILL, which is credential-free).
- BLOCK-RESTART-UNTIL-RECONCILED: writing the KILL file + the ``needs_reconcile``
  marker is what enforces it on the bot side (the bot's startup checks the marker
  and refuses to quote until it reconciles). The supervisor's job is only to DROP
  the marker as part of a kill; the bot owns clearing it.

Secrets: the supervisor credential comes ONLY from env (hard rule 3) and is never
logged. Determinism/testability: the exchange is behind a small ``SupervisorExchange``
protocol, so tests inject a fake (no real network); the clock is injectable.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from combomaker.core.clock import Clock
from combomaker.ops.logging import get_logger

# THE one write-token budget (ops/write_budget.py). It used to be defined here;
# it moved when the lifecycle's end-of-game withdrawal wave had to pace on the
# SAME bucket concept, so there is one token budget in the codebase and one
# config source for it. Re-exported by this import — importing it from here
# still works.
from combomaker.ops.write_budget import WriteBudget
from combomaker.risk.heartbeat import Heartbeat, HeartbeatReader, ReconcileMarker
from combomaker.risk.progress import ProgressReader, progress_path

log = get_logger(__name__)

# Env var names for the supervisor's DEDICATED credential (distinct from the
# bot's KALSHI_API_KEY_ID / KALSHI_PROD_API_KEY_ID — a separate key so a
# throttled/compromised bot credential cannot disable the kill path).
ENV_SUPERVISOR_API_KEY_ID = "KALSHI_SUPERVISOR_API_KEY_ID"
ENV_SUPERVISOR_PRIVATE_KEY_PATH = "KALSHI_SUPERVISOR_PRIVATE_KEY_PATH"
ENV_SUPERVISOR_PRIVATE_KEY_PEM = "KALSHI_SUPERVISOR_PRIVATE_KEY_PEM"

# The supervisor writes its OWN heartbeat here (under the shared data_dir) so the
# bot's preflight can verify a RUNNING, RECENTLY-BEATING watcher — not merely a
# configured credential. Filename is stable so both processes agree without
# passing paths between them.
SUPERVISOR_HEARTBEAT_FILENAME = "supervisor_heartbeat.txt"


def supervisor_heartbeat_path(data_dir: Path) -> Path:
    """The path the supervisor beats and the bot's preflight reads."""
    return data_dir / SUPERVISOR_HEARTBEAT_FILENAME


class SupervisorExchange(Protocol):
    """The exchange operations the supervisor needs, behind a protocol so tests
    inject a fake. In production this is a thin adapter over ``KalshiRestClient``
    built with the supervisor's OWN credential."""

    async def list_open_quote_ids(self) -> list[str]:
        """Every resting quote id owned by us. Raises on an unreachable exchange."""
        ...

    async def cancel_quote(self, quote_id: str) -> None:
        """Cancel one resting quote. Raises on failure."""
        ...


@dataclass(frozen=True, slots=True)
class KillResult:
    """Outcome of an emergency kill. ``kill_written`` is the load-bearing
    invariant — it is True on EVERY path that completes (reachable or not),
    because writing KILL is credential-free and always attempted."""

    cancelled: int
    failed: int
    exchange_reachable: bool
    kill_written: bool
    marker_written: bool
    budget_exhausted: bool = False


class SupervisorConfig:
    """Plain config holder (not pydantic — the supervisor is a tiny standalone
    process). Paths + thresholds + the reserved budget size."""

    def __init__(
        self,
        *,
        heartbeat_path: Path,
        kill_file: Path,
        reconcile_marker_path: Path,
        heartbeat_timeout_s: float = 15.0,
        poll_interval_s: float = 1.0,
        write_budget_capacity: int = 200,
        write_budget_refill_s: float = 10.0,
        own_heartbeat_path: Path | None = None,
        progress_path_: Path | None = None,
    ) -> None:
        self.heartbeat_path = heartbeat_path
        self.kill_file = kill_file
        self.reconcile_marker_path = reconcile_marker_path
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.poll_interval_s = poll_interval_s
        self.write_budget_capacity = write_budget_capacity
        self.write_budget_refill_s = write_budget_refill_s
        # Where the supervisor writes its OWN heartbeat (the bot's preflight reads
        # it to prove a RUNNING watcher). Defaults next to the bot's heartbeat so
        # a caller that only knows the data_dir gets the right path for free.
        self.own_heartbeat_path = (
            own_heartbeat_path
            if own_heartbeat_path is not None
            else heartbeat_path.parent / SUPERVISOR_HEARTBEAT_FILENAME
        )
        # The bot's per-loop PROGRESS ledger ("working"), read alongside the
        # heartbeat ("alive"). Defaults next to the heartbeat so a caller that
        # only knows the data_dir gets the right path for free.
        self.progress_path = (
            progress_path_
            if progress_path_ is not None
            else progress_path(heartbeat_path.parent)
        )


class SafetySupervisor:
    """Watches the heartbeat; kills the bot externally when it goes wedged.

    ``exchange`` may be ``None`` — the supervisor still runs and still writes KILL
    on a wedge (the credential-free half of the kill path), but it reports that it
    has no working cancel path (fail-closed: no dedicated credential ⇒ can't
    claim to cancel). In production ``exchange`` is built from the supervisor's
    OWN credential (``credential_configured``).
    """

    def __init__(
        self,
        config: SupervisorConfig,
        clock: Clock,
        *,
        exchange: SupervisorExchange | None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._exchange = exchange
        self._reader = HeartbeatReader(clock, config.heartbeat_path)
        # The second, INDEPENDENT axis: per-loop progress. The heartbeat alone
        # can only say the event loop schedules; this says the loops that
        # matter are still advancing. Latching (see ProgressReader) so a bot
        # that has never published one is not killed on sight, while a bot that
        # HAS published one can never make the ledger vanish to hide a stall.
        self._progress = ProgressReader(clock, config.progress_path)
        self._marker = ReconcileMarker(config.reconcile_marker_path)
        # The supervisor's OWN heartbeat: it beats this every poll cycle so the
        # bot's preflight can verify a RUNNING, RECENTLY-BEATING watcher (not just
        # a configured credential). Independent of the bot's heartbeat — the
        # kill path must not depend on the bot being healthy.
        self._own_heartbeat = Heartbeat(clock, config.own_heartbeat_path)
        self._budget = WriteBudget.create(
            clock,
            capacity=config.write_budget_capacity,
            refill_s=config.write_budget_refill_s,
        )
        self._stop = asyncio.Event()
        self._killed = False
        # Edge-trigger latch for the DEMOTED liveness axis (2026-07-27): the
        # heartbeat no longer kills, so it must not spam either — one line per
        # stale episode, reset when it comes back.
        self._heartbeat_stale_logged = False

    @property
    def has_kill_credential(self) -> bool:
        """True iff a working exchange (own credential) is attached. Fail-closed
        default is False — no dedicated credential means no cancel path."""
        return self._exchange is not None

    def _write_kill_file(self, reason: str) -> bool:
        """Write the KILL file + drop the reconcile marker. Credential-free — this
        is the half of the kill path that ALWAYS runs, even when the exchange is
        unreachable. Returns True if KILL landed on disk."""
        kill_written = False
        try:
            self._config.kill_file.parent.mkdir(parents=True, exist_ok=True)
            self._config.kill_file.write_text(
                f"supervisor kill: {reason}\n", encoding="utf-8"
            )
            kill_written = True
        except OSError as exc:  # pragma: no cover - disk failure path
            log.error("supervisor_kill_file_write_failed", error=repr(exc))
        return kill_written

    async def emergency_cancel_all(self, reason: str) -> KillResult:
        """Cancel every resting quote via the supervisor's OWN credential, then
        write KILL + the reconcile marker. FAIL-CLOSED: on ANY exchange error we
        still write KILL + the marker + alarm (a supervisor that can't cancel must
        at least stop the bot resuming). Idempotent-safe to call repeatedly."""
        cancelled = 0
        failed = 0
        exchange_reachable = False
        budget_exhausted = False

        if self._exchange is None:
            log.error(
                "supervisor_no_cancel_credential",
                reason=reason,
                detail="no dedicated supervisor credential — KILL only, no cancel path",
            )
        else:
            try:
                quote_ids = await self._exchange.list_open_quote_ids()
                exchange_reachable = True
                for quote_id in quote_ids:
                    if not self._budget.try_spend():
                        # Reserved budget exhausted mid-cancel: alarm loudly and
                        # stop spending (the remaining quotes are left, but KILL
                        # still lands so the bot can't add more). This should not
                        # happen with a correctly-sized reserved budget.
                        budget_exhausted = True
                        log.error(
                            "supervisor_write_budget_exhausted",
                            reason=reason,
                            cancelled=cancelled,
                            remaining=len(quote_ids) - cancelled - failed,
                        )
                        break
                    try:
                        await self._exchange.cancel_quote(quote_id)
                        cancelled += 1
                    except Exception as exc:
                        failed += 1
                        log.warning(
                            "supervisor_cancel_failed", quote_id=quote_id, error=repr(exc)
                        )
            except Exception as exc:
                # Exchange unreachable / listing failed — fail closed.
                log.error(
                    "supervisor_exchange_unreachable",
                    reason=reason,
                    error=repr(exc),
                    detail="cannot reach exchange — writing KILL anyway (fail-closed)",
                )

        kill_written = self._write_kill_file(reason)
        self._marker.set(f"supervisor kill: {reason}")
        marker_written = self._marker.is_set()
        self._killed = True
        log.error(
            "supervisor_emergency_kill",
            reason=reason,
            cancelled=cancelled,
            failed=failed,
            exchange_reachable=exchange_reachable,
            kill_written=kill_written,
            marker_written=marker_written,
            budget_exhausted=budget_exhausted,
        )
        return KillResult(
            cancelled=cancelled,
            failed=failed,
            exchange_reachable=exchange_reachable,
            kill_written=kill_written,
            marker_written=marker_written,
            budget_exhausted=budget_exhausted,
        )

    def heartbeat_wedged(self) -> bool:
        """True if the bot's heartbeat is missing / stale beyond the timeout.
        Fail-closed (an unreadable heartbeat is wedged).

        LOG-ONLY since 2026-07-27 — see ``wedged_detail``. Kept (with the
        ``HeartbeatReader`` behind it) because it is still read by the bot's own
        preflight and by the forensic line ``supervisor_heartbeat_stale``; it
        simply no longer decides anything."""
        return self._reader.is_wedged(self._config.heartbeat_timeout_s)

    def _log_heartbeat_staleness(self) -> None:
        """Emit the demoted liveness signal EXACTLY ONCE per stale episode.

        Edge-triggered on purpose: after a real death the heartbeat is stale
        forever, and a per-poll line at ``poll_interval_s`` would bury the log
        that the next incident has to be read out of."""
        stale = self.heartbeat_wedged()
        if not stale:
            self._heartbeat_stale_logged = False
            return
        if self._heartbeat_stale_logged:
            return
        self._heartbeat_stale_logged = True
        age = self._reader.read_age_s()
        log.warning(
            "supervisor_heartbeat_stale",
            age_s=age,
            timeout_s=self._config.heartbeat_timeout_s,
            detail=(
                "heartbeat missing/unreadable"
                if age is None
                else f"heartbeat age={age:.1f}s > "
                f"{self._config.heartbeat_timeout_s:.1f}s"
            ),
            consequence="log-only — the PROGRESS axis owns the kill decision",
        )

    def wedged_detail(self) -> str | None:
        """The kill reason, or ``None`` if the bot is WORKING.

        ONE axis decides: PROGRESS — ``loop_progress.json``. A quote/maintenance
        loop that stops advancing past its own derived bound is a wedge, and the
        reason NAMES the loop.

        WHY THE LIVENESS AXIS NO LONGER DECIDES (2026-07-27, proportionality
        review). The heartbeat axis has an 0-for-8 true-positive record: all 8
        supervisor emergency kills fired with ``exchange_reachable=true`` on a
        bot that was alive, and the 07-27 kill fired at age=30.9s against a 30.5s
        bound — ONE TICK over — while 4.2s later that same "wedged" bot withdrew
        all 67 of its quotes in 218ms. The progress axis strictly DOMINATES it:
        anything that genuinely stops the event loop also stops every registered
        loop from advancing, and progress additionally NAMES the stalled loop.
        A dead process therefore still gets cancelled and marked — one poll cycle
        later, via a signal that cannot mistake a slow await for a corpse.

        NOT WEAKENED: the supervisor still emergency-cancels every resting quote
        and still writes KILL + ``needs_reconcile`` on any progress wedge, and
        the liveness signal survives as ``supervisor_heartbeat_stale``.

        THE ONE WINDOW WHERE PROGRESS DOES *NOT* DOMINATE (2026-07-27 gate B1,
        proven by probe, not argued). ``ProgressReader`` deliberately does not
        latch on an EMPTY ledger, and the bot publishes exactly an empty ledger
        at preflight (quote_app.py:2125, :3554) while its loops only register
        later (:2355, :2367). Between ``_launch_supervisor`` and registration the
        progress axis is blind BY CONSTRUCTION — and that window SPANS THE
        STARTUP RECONCILE, worst case ~40s, which is precisely when leftover
        quotes are KNOWN to be resting on the exchange, and it is LONGEST against
        a degraded exchange. Measured with the axis removed: empty ledger +
        600s-stale heartbeat + a leftover resting quote gave wedged_detail None,
        zero quotes cancelled, no KILL, no needs_reconcile. Pre-change that path
        emergency-cancelled.

        So the liveness axis is kept for EXACTLY that window: it decides only
        while the progress ledger has never established. Once any loop has
        registered, progress is the sole authority and a stale heartbeat is
        forensic-only — which is what the 0-for-8 record was actually about."""
        progress_verdict = self._progress.wedged_detail()
        if progress_verdict is not None:
            return progress_verdict
        if not self._progress.seen and self.heartbeat_wedged():
            # Startup window only: no loop has ever reported progress, so the
            # dominating axis cannot speak. Fall back to liveness rather than
            # leave the reconcile window uncovered.
            return "liveness stale before any loop registered (startup window)"
        return None

    def beat_own_heartbeat(self) -> None:
        """Record the supervisor's OWN liveness. Beaten every poll cycle (and
        after a kill — the supervisor stays up as the latch, and a live latch
        must keep proving it's alive). The bot's preflight reads this to confirm
        a RUNNING watcher before it risks a cent."""
        self._own_heartbeat.beat()

    async def check_once(self) -> KillResult | None:
        """One watchdog cycle: beat our own heartbeat, log the demoted liveness
        signal, then — if a registered LOOP has stalled past its own derived
        bound and we haven't already killed — emergency-cancel + KILL. Returns
        the ``KillResult`` on a kill, else ``None``. Idempotent: once killed,
        further checks are no-ops (the KILL file + marker persist; re-cancelling
        adds nothing) EXCEPT we keep beating our own heartbeat."""
        self.beat_own_heartbeat()
        # Demoted liveness axis: forensic only, never a consequence.
        self._log_heartbeat_staleness()
        if self._killed:
            return None
        detail = self.wedged_detail()
        if detail is not None:
            log.error("supervisor_loop_wedged", detail=detail)
            return await self.emergency_cancel_all(detail)
        return None

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Poll the heartbeat until stopped or a kill fires. After a kill the loop
        keeps running (idempotent no-ops) so a supervisor process stays up as the
        latch, but does no further work."""
        log.info(
            "supervisor_starting",
            heartbeat_path=str(self._config.heartbeat_path),
            own_heartbeat_path=str(self._config.own_heartbeat_path),
            progress_path=str(self._config.progress_path),
            timeout_s=self._config.heartbeat_timeout_s,
            has_credential=self.has_kill_credential,
        )
        # Beat immediately so the bot's preflight sees a fresh watcher from t=0,
        # not a gap until the first poll cycle.
        self.beat_own_heartbeat()
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception:  # a supervisor must never crash silently
                log.exception("supervisor_check_raised")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._config.poll_interval_s
                )
            except TimeoutError:
                pass


def supervisor_credential_configured() -> bool:
    """True iff the dedicated supervisor credential env vars are present (id +
    either a PEM path or PEM body). Never reads the key material here — only
    checks presence, so nothing secret is logged. Fail-closed: absent ⇒ False."""
    key_id = os.environ.get(ENV_SUPERVISOR_API_KEY_ID, "").strip()
    if not key_id:
        return False
    has_pem = bool(os.environ.get(ENV_SUPERVISOR_PRIVATE_KEY_PEM, "").strip())
    has_path = bool(os.environ.get(ENV_SUPERVISOR_PRIVATE_KEY_PATH, "").strip())
    return has_pem or has_path


def supervisor_heartbeat_reachable(
    data_dir: Path, clock: Clock, *, max_age_s: float
) -> bool:
    """True iff the external kill path is ACTUALLY operative right now: a
    supervisor process is RUNNING and RECENTLY BEATING its own heartbeat AND the
    dedicated cancel credential is present.

    This is the preflight's ``external_kill_reachable`` gate. It is deliberately
    STRONGER than ``supervisor_credential_configured`` (mere env presence): a
    credential in the environment with NO watcher process running is a dead kill
    path — exactly the shadow/dead-process gap the audit flagged. We require a
    live, recently-beating watcher so the external cancel can genuinely fire.

    Fail-closed on every UNKNOWN: a missing/unreadable/stale supervisor heartbeat
    (``read_age_s`` None or age > ``max_age_s``) ⇒ False, and an absent credential
    ⇒ False (a beating watcher with no cancel credential is KILL-only, which is
    not a reachable CANCEL path). Wall-clock based (two processes share only wall
    time); a future-skewed beat reads as stale (heartbeat reader's contract)."""
    if not supervisor_credential_configured():
        return False
    reader = HeartbeatReader(clock, supervisor_heartbeat_path(data_dir))
    return not reader.is_wedged(max_age_s)


class KalshiSupervisorExchange:
    """Production ``SupervisorExchange`` over ``KalshiRestClient`` built with the
    supervisor's OWN credential. Thin: list our open quotes, cancel one."""

    def __init__(self, rest: object, clock: Clock) -> None:
        # ``rest`` is a KalshiRestClient; typed as object to keep this module
        # importable without pulling the aiohttp client into unit tests.
        self._rest = rest
        self._clock = clock

    async def list_open_quote_ids(self) -> list[str]:
        # SHARED bounded+retrying enumeration: cursor-paginated, min_ts/max_ts
        # windowed so the emergency cancel-all NEVER trips the exchange
        # circuit-breaker with a full-history scan (which 500/504s — verified
        # 2026-07-13, NOTES.md), and 5xx-retried so a transient fail-fast
        # cooldown doesn't abort the kill. Lazy import keeps this module
        # importable without the aiohttp client. See exchange/quote_query.
        from combomaker.exchange.quote_query import list_open_quotes, open_quote_ids

        quotes = await list_open_quotes(
            self._rest,  # type: ignore[arg-type]
            int(self._clock.now().timestamp()),
        )
        return open_quote_ids(quotes)

    async def cancel_quote(self, quote_id: str) -> None:
        await self._rest.delete_quote(quote_id)  # type: ignore[attr-defined]


async def _run_supervisor_cli(env: str, config_path: Path | None) -> int:
    """Wire the real supervisor: load config, build the OWN-credential REST
    client if the dedicated credential is present, run the watchdog. Returns a
    process exit code."""
    from combomaker.core.clock import SystemClock
    from combomaker.exchange.auth import Credentials, RequestSigner
    from combomaker.exchange.rest import KalshiRestClient
    from combomaker.ops.config import Env, load_config
    from combomaker.ops.logging import configure_logging

    configure_logging(json_output=True, level="INFO")
    resolved_env = Env(env)
    cfg_path = config_path or (Path("config") / f"{resolved_env.value}.yaml")
    app_config = load_config(cfg_path, env=resolved_env)
    sup_config = SupervisorConfig(
        heartbeat_path=app_config.data_dir / "heartbeat.txt",
        kill_file=app_config.kill_file,
        reconcile_marker_path=app_config.data_dir / "needs_reconcile",
        heartbeat_timeout_s=app_config.supervisor.heartbeat_timeout_s,
        poll_interval_s=app_config.supervisor.poll_interval_s,
        write_budget_capacity=app_config.supervisor.write_budget_capacity,
        write_budget_refill_s=app_config.supervisor.write_budget_refill_s,
        own_heartbeat_path=supervisor_heartbeat_path(app_config.data_dir),
        progress_path_=progress_path(app_config.data_dir),
    )
    clock = SystemClock()

    if not supervisor_credential_configured():
        log.error(
            "supervisor_no_dedicated_credential",
            detail=(
                f"set {ENV_SUPERVISOR_API_KEY_ID} + "
                f"{ENV_SUPERVISOR_PRIVATE_KEY_PATH}/{ENV_SUPERVISOR_PRIVATE_KEY_PEM} — "
                "running KILL-only (no cancel path)"
            ),
        )
        supervisor = SafetySupervisor(sup_config, clock, exchange=None)
        await supervisor.run()
        return 0

    creds = Credentials.from_env_names(
        ENV_SUPERVISOR_API_KEY_ID,
        ENV_SUPERVISOR_PRIVATE_KEY_PATH,
        ENV_SUPERVISOR_PRIVATE_KEY_PEM,
    )
    signer = RequestSigner(creds, clock)
    async with KalshiRestClient(app_config.endpoints.rest_base_url, signer) as rest:
        exchange = KalshiSupervisorExchange(rest, clock)
        supervisor = SafetySupervisor(sup_config, clock, exchange=exchange)
        await supervisor.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    """``python -m combomaker.ops.supervisor --env {demo,prod}``."""
    import argparse

    from combomaker.exchange.auth import CredentialsError
    from combomaker.ops.dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(prog="combomaker-supervisor")
    parser.add_argument("--env", choices=["demo", "prod"], default="demo")
    parser.add_argument("--config", type=Path, default=None, help="YAML config path")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_supervisor_cli(args.env, args.config))
    except CredentialsError as exc:
        log.error("supervisor_credential_error", error=str(exc))
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
