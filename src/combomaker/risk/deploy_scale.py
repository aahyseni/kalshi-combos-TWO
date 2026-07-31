"""DEPLOYMENT SCALE — solve, never configure (North Star layer 1).

The operator's LEVER #1: the book is permitted to be larger than it is. Rather
than hand-set a multiplier ("2.19x" — measured once, on one night's book, and
stale the moment a fill lands), SOLVE the largest deployment scale ``s`` for
which the CURRENT book, scaled by ``s``, still clears EVERY enforced cap —
including the ratified tail-probability anchor
``P(book loss >= portfolio_cvar_frac x bankroll) <= portfolio_kill_tail_prob``.

    s* = max { s in GRID, s >= 1 : feasible(s) }

``feasible(s)`` is supplied by the caller and is literally the live
``compute_book_risk`` on the s-scaled book followed by the live
``LimitChecker.check`` against it (hard rule 8 — nothing is reimplemented here).
It is MONOTONE in ``s``: every loss / tail / notional axis is non-decreasing
under a uniform book scaling. The scan therefore walks the grid DOWNWARD and
stops at the first feasible point, so the answer is always a scale the real
checker actually graded clean — never an interpolation.

WHAT ``s`` IS BOUNDED BY (all of them, always):
  * the tail-probability book gate at the ratified ``portfolio_kill_tail_prob``;
  * the portfolio deterministic-max / CVaR / P(ruin) / utilization budgets;
  * every deploy-side cap (per-combo, entity, game, slate, directional) at its
    UNSCALED threshold — the strictest reading of the operator's
    "``s`` must be bounded above by every already-enforced cap".
So ``s`` never raises the envelope; it measures the unused room INSIDE it.

FAIL-SAFE, ALWAYS SMALLER (non-negotiable):
  * a solve that raises, or has no usable envelope             -> 1.0;
  * a CURRENT book (s = 1) that is already infeasible          -> 1.0;
  * a probe whose MC never arrived                             -> infeasible;
  * the result is clamped to [1.0, s_max] and NEVER below 1.0 (below 1.0 the
    enforced caps are already refusing on their own — that is their job; adding
    a second, unratified wall underneath them is not this build's mandate).

CONSUMPTION: :func:`scale_deploy_budgets` (defined in ``risk.limits``, re-exported
here) returns a copy of ``RiskLimits`` with ONLY the DEPLOY-SIDE budgets
multiplied by ``s`` — per-combo, entity, game, slate, directional. The ENVELOPE
(portfolio CVaR / det-max / ruin budget / tail-prob anchor), the HALTS (daily /
drawdown / hard-trip KILL / ruin floor) and every ABSOLUTE backstop come back
UNTOUCHED, so the walls that bounded the solve can never be moved by its own
answer. Thresholds stay exact ``Fraction``s (floats are never live thresholds).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from combomaker.ops.logging import get_logger
from combomaker.risk.limits import (
    DEPLOY_BUDGET_FIELDS,
    as_exact,
    scale_deploy_budgets,
)

log = get_logger(__name__)

__all__ = [
    "DEPLOY_BUDGET_FIELDS",
    "ENVELOPE_INVARIANT_FIELDS",
    "FAILSAFE",
    "DeployScaleResult",
    "as_exact",
    "scale_deploy_budgets",
    "scale_grid",
    "solve_deployment_scale",
]

# Fields that MUST be identical before and after a scale — pinned by test.
ENVELOPE_INVARIANT_FIELDS: tuple[str, ...] = (
    "portfolio_cvar_frac",
    "portfolio_det_max_frac",
    "portfolio_kill_tail_prob",
    "portfolio_ruin_prob_budget",
    # KILL-ANCHORED BOOK GATE (2026-07-29). The arming flag is ENVELOPE, not
    # deploy budget: a solved deployment scale must never be able to silently
    # disarm (or arm) the re-anchor it was solved against.
    "kill_anchored_book_gate",
    "daily_loss_frac",
    "drawdown_frac",
    "hard_trip_frac",
    "max_contracts_per_quote",
    "max_notional_per_quote_dollars",
    "absolute_notional_multiple",
)


def scale_grid(s_max: float, points: int) -> tuple[float, ...]:
    """The DESCENDING probe ladder, largest first.

    Deterministic and bounded: the caller knows every ``s`` that will ever be
    asked about before it runs a single MC, which is what lets the off-loop
    solver interleave ``await``s and lets the MC budget be stated exactly
    (at most ``points`` full-book MCs per solve). Excludes 1.0 — that probe is
    the solver's own precondition and is always run.
    """
    if s_max <= 1.0 or points < 1:
        return ()
    step = (s_max - 1.0) / points
    return tuple(round(1.0 + step * k, 6) for k in range(points, 0, -1))


@dataclass(frozen=True, slots=True)
class DeployScaleResult:
    """One solve. ``scale`` is what the caps consume; everything else is
    provenance for the operator readout (never a control input)."""

    scale: float
    solved: bool                    # False ⇒ the fail-safe 1.0 is in force
    binding: tuple[str, ...]        # reason codes that stopped s from growing
    reason: str                     # why we are at this scale, in words
    evaluations: int
    solve_ms: float = 0.0
    # The ExposureBook POSITION GENERATION this scale was solved against. A
    # match means the book is EXACTLY the one measured, so the full scale
    # applies; a mismatch routes the consumer through the book-growth decay
    # below (a reservation bumps this counter on every accept, so a hard cliff
    # here made the whole feature inert — see deploy_scale_for_check).
    # -1 = unstamped, which never equals a real generation (>= 0).
    book_generation: int = -1
    # Committed premium-at-risk (cc) of the book this scale was solved against.
    # The consumer divides by the LIVE premium to decay the scale as the book
    # grows between solves (see ``QuoteLifecycle.deploy_scale_for_check``), so
    # every dollar of new book is charged against the measured headroom instead
    # of the measurement being discarded wholesale. 0 = unstamped ⇒ the consumer
    # falls back to 1.0 (fail-safe: an un-anchored ratio is not evidence).
    solved_premium_cc: int = 0

    @property
    def exact(self) -> Fraction:
        return as_exact(self.scale)


FAILSAFE = DeployScaleResult(
    scale=1.0,
    solved=False,
    binding=(),
    reason="fail-safe: no usable envelope (never larger than today)",
    evaluations=0,
)


def solve_deployment_scale(
    graded: Mapping[float, tuple[bool, tuple[str, ...]]],
    *,
    s_max: float,
    points: int,
    solve_ms: float = 0.0,
) -> DeployScaleResult:
    """Pick the scale from an ALREADY-GRADED probe ladder.

    ``graded`` maps each probed ``s`` (which MUST include 1.0 and a subset of
    :func:`scale_grid`) to ``(ok, binding_reason_codes)`` as returned by the
    live checker. Splitting "grade" from "pick" keeps ONE copy of the policy
    while letting the caller grade synchronously (inline) or with ``await``s
    between probes (off-loop) — no duplicated decision logic between the two.

    Never raises: anything unexpected collapses to :data:`FAILSAFE`.
    """
    try:
        base = graded.get(1.0)
        if base is None:
            return FAILSAFE
        ok_1, binding_1 = base
        if not ok_1:
            # The book we ALREADY hold does not clear the envelope. The enforced
            # caps are refusing new flow on their own; do not also shrink the
            # deploy budgets. Stay at today's size.
            return DeployScaleResult(
                scale=1.0,
                solved=True,
                binding=binding_1,
                reason="current book infeasible — enforced caps already refusing",
                evaluations=len(graded),
                solve_ms=solve_ms,
            )
        binding: tuple[str, ...] = ()
        for s in scale_grid(s_max, points):          # descending
            entry = graded.get(s)
            if entry is None:
                continue
            ok, why = entry
            if ok:
                return DeployScaleResult(
                    scale=max(1.0, min(s, s_max)),
                    solved=True,
                    binding=binding,
                    reason="solved against the live envelope",
                    evaluations=len(graded),
                    solve_ms=solve_ms,
                )
            if not binding:
                binding = why                        # the FIRST wall walking down
        return DeployScaleResult(
            scale=1.0,
            solved=True,
            binding=binding,
            reason="no scale above 1.0 clears the envelope",
            evaluations=len(graded),
            solve_ms=solve_ms,
        )
    except Exception:
        log.exception("deploy_scale_pick_failed")
        return FAILSAFE
