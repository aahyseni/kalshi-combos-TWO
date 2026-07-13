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

- HEARTBEAT: the bot writes ``heartbeat.txt`` every tick; the supervisor reads
  its age against the supervisor's own clock. Age > ``heartbeat_timeout_s`` (or
  an unreadable heartbeat — fail-closed) ⇒ WEDGED.
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
from combomaker.risk.heartbeat import HeartbeatReader, ReconcileMarker

log = get_logger(__name__)

# Env var names for the supervisor's DEDICATED credential (distinct from the
# bot's KALSHI_API_KEY_ID / KALSHI_PROD_API_KEY_ID — a separate key so a
# throttled/compromised bot credential cannot disable the kill path).
ENV_SUPERVISOR_API_KEY_ID = "KALSHI_SUPERVISOR_API_KEY_ID"
ENV_SUPERVISOR_PRIVATE_KEY_PATH = "KALSHI_SUPERVISOR_PRIVATE_KEY_PATH"
ENV_SUPERVISOR_PRIVATE_KEY_PEM = "KALSHI_SUPERVISOR_PRIVATE_KEY_PEM"


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
class WriteBudget:
    """A reserved token bucket for the supervisor's OWN writes.

    Refills ``capacity`` tokens per ``refill_s`` window. ``try_spend`` consumes a
    token if available. The point: the supervisor's writes are throttled to a
    RESERVED budget so, even when the shared/bot budget is exhausted under a 429
    storm, the supervisor always has tokens to cancel-all — it never draws from
    (or exhausts) the bot's pool. Deterministic under a fake clock.

    Frozen: the mutable state (tokens, last-refill) lives in a tiny inner box so
    the public handle stays hashable/immutable while the bucket refills.
    """

    clock: Clock
    capacity: int
    refill_s: float
    _state: _BudgetState

    @classmethod
    def create(cls, clock: Clock, *, capacity: int, refill_s: float) -> WriteBudget:
        if capacity < 1:
            raise ValueError("write budget capacity must be >= 1")
        if refill_s <= 0:
            raise ValueError("write budget refill_s must be > 0")
        return cls(
            clock=clock,
            capacity=capacity,
            refill_s=refill_s,
            _state=_BudgetState(tokens=capacity, last_refill=clock.now().timestamp()),
        )

    def _refill(self) -> None:
        now = self.clock.now().timestamp()
        elapsed = now - self._state.last_refill
        if elapsed >= self.refill_s:
            # Full refill each window boundary (a reserved emergency budget is
            # bursty, not rate-smoothed: it must be FULL when a kill fires).
            self._state.tokens = self.capacity
            self._state.last_refill = now

    def try_spend(self) -> bool:
        """Consume one token; True if one was available. Refills first."""
        self._refill()
        if self._state.tokens > 0:
            self._state.tokens -= 1
            return True
        return False

    @property
    def tokens(self) -> int:
        self._refill()
        return self._state.tokens


@dataclass(slots=True)
class _BudgetState:
    tokens: int
    last_refill: float


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
    ) -> None:
        self.heartbeat_path = heartbeat_path
        self.kill_file = kill_file
        self.reconcile_marker_path = reconcile_marker_path
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.poll_interval_s = poll_interval_s
        self.write_budget_capacity = write_budget_capacity
        self.write_budget_refill_s = write_budget_refill_s


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
        self._marker = ReconcileMarker(config.reconcile_marker_path)
        self._budget = WriteBudget.create(
            clock,
            capacity=config.write_budget_capacity,
            refill_s=config.write_budget_refill_s,
        )
        self._stop = asyncio.Event()
        self._killed = False

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
        Fail-closed (an unreadable heartbeat is wedged)."""
        return self._reader.is_wedged(self._config.heartbeat_timeout_s)

    async def check_once(self) -> KillResult | None:
        """One watchdog cycle: if the heartbeat is wedged and we haven't already
        killed, emergency-cancel + KILL. Returns the ``KillResult`` on a kill,
        else ``None``. Idempotent: once killed, further checks are no-ops (the
        KILL file + marker persist; re-cancelling adds nothing)."""
        if self._killed:
            return None
        if self.heartbeat_wedged():
            age = self._reader.read_age_s()
            detail = (
                f"heartbeat wedged (age={age:.1f}s > {self._config.heartbeat_timeout_s:.1f}s)"
                if age is not None
                else "heartbeat missing/unreadable"
            )
            log.error("supervisor_heartbeat_wedged", detail=detail)
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
            timeout_s=self._config.heartbeat_timeout_s,
            has_credential=self.has_kill_credential,
        )
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


class KalshiSupervisorExchange:
    """Production ``SupervisorExchange`` over ``KalshiRestClient`` built with the
    supervisor's OWN credential. Thin: list our open quotes, cancel one."""

    def __init__(self, rest: object) -> None:
        # ``rest`` is a KalshiRestClient; typed as object to keep this module
        # importable without pulling the aiohttp client into unit tests.
        self._rest = rest

    async def list_open_quote_ids(self) -> list[str]:
        payload = await self._rest.get_quotes(user_filter="self", status="open")  # type: ignore[attr-defined]
        quotes = payload.get("quotes", []) or []
        ids: list[str] = []
        for quote in quotes:
            quote_id = str(quote.get("id") or quote.get("quote_id") or "")
            if quote_id:
                ids.append(quote_id)
        return ids

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
        exchange = KalshiSupervisorExchange(rest)
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
