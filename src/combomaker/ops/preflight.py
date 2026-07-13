"""Prod go-live preflight (RISK_BUILD_PLAN Phase 6).

The RUNTIME half of the go-live gates. ``AppConfig.assert_safe_to_run`` checks
the STATIC gates (``--confirm-live``, ``prod_limits_configured``, the non-empty
leg-series whitelist) at construction. This module checks the LIVE conditions
that only exist once the app is running and must ALL be green before the FIRST
quote reaches the exchange:

  1. prod limits configured        (static, re-asserted here for a single verdict)
  2. leg-series whitelist non-empty (ditto)
  3. supervisor heartbeat established — the bot has beaten at least once AND the
     external supervisor's kill path is reachable (its own credential present),
     so the external kill can actually fire.
  4. external kill reachable        — the supervisor credential is configured.
  5. book reconciled                — the startup exchange-first reconcile ran and
     the ``needs_reconcile`` marker is clear.

Fail-closed: a gate whose input is UNKNOWN is RED, never "probably fine". The bot
REFUSES to quote on prod if ANY gate is red. Demo is unaffected — the preflight
is a no-op on demo (there is no real money to protect and no supervisor
requirement), so demo/paper flows are untouched.

Pure + deterministic: ``evaluate_preflight`` takes plain booleans and returns the
verdict, so it is testable without any app wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PreflightConditions:
    """The live inputs the preflight grades. Every field defaults to the
    FAIL-CLOSED value (``False`` = not-yet-green), so an unset condition is red."""

    limits_configured: bool = False
    whitelist_non_empty: bool = False
    supervisor_heartbeat_established: bool = False
    external_kill_reachable: bool = False
    book_reconciled: bool = False


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """The verdict. ``green`` iff no gate is red. ``red_gates`` names each failing
    gate for a single loud log line."""

    green: bool
    red_gates: tuple[str, ...] = field(default_factory=tuple)


def evaluate_preflight(
    conditions: PreflightConditions,
    *,
    require_supervisor: bool,
) -> PreflightResult:
    """Grade every gate. ``require_supervisor`` gates whether the two supervisor
    conditions are load-bearing (an operator can disable them, but they default
    on). Returns the set of RED gates; empty ⇒ green."""
    red: list[str] = []
    if not conditions.limits_configured:
        red.append("limits_configured")
    if not conditions.whitelist_non_empty:
        red.append("whitelist_non_empty")
    if require_supervisor:
        if not conditions.supervisor_heartbeat_established:
            red.append("supervisor_heartbeat_established")
        if not conditions.external_kill_reachable:
            red.append("external_kill_reachable")
    if not conditions.book_reconciled:
        red.append("book_reconciled")
    return PreflightResult(green=not red, red_gates=tuple(red))


class PreflightError(RuntimeError):
    """Raised when a prod preflight is red — the bot refuses to quote."""
