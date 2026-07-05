"""Joint probability of a combo from leg beliefs + relationship structure.

Model: latent Gaussian per leg (YES threshold at Φ⁻¹(p)). A leg selected on
its NO side is the complement event — handled exactly by flipping the sign of
that leg's latent variable: marginal becomes 1−p and the correlation matrix is
conjugated by diag(±1) (still a valid correlation matrix).

Uncertainty is priced, not hoped away:
- leg uncertainty propagates via the (independence-approximate, conservative
  linear-sum) product gradient ∂P/∂mᵢ ≈ P/mᵢ;
- correlation uncertainty is measured directly: the joint is re-evaluated at
  ρ ± ρ_uncertainty for the same-event blocks and the spread becomes width.

Everything clamps to the Fréchet–Hoeffding bounds of the selected-side
marginals.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from combomaker.pricing.copula import (
    build_block_corr,
    clamp_to_frechet,
    frechet_bounds,
    gaussian_copula_joint_prob,
)
from combomaker.pricing.legs import LegBelief

_MIN_MARGINAL_FOR_GRADIENT = 0.01


@dataclass(frozen=True, slots=True)
class CorrelationParams:
    same_event_rho: float
    cross_event_rho: float
    rho_uncertainty: float


@dataclass(frozen=True, slots=True)
class JointEstimate:
    p: float
    uncertainty: float           # probability-space width input (legs + corr)
    frechet_lo: float
    frechet_hi: float
    notes: tuple[str, ...]


def _signed_corr(
    n: int,
    groups: Sequence[Sequence[int]],
    sides: Sequence[str],
    *,
    rho: float,
    cross_rho: float,
) -> NDArray[np.float64]:
    base = build_block_corr(n, [(list(g), rho) for g in groups], default_rho=cross_rho)
    signs = np.array([1.0 if side == "yes" else -1.0 for side in sides])
    return np.asarray(base * np.outer(signs, signs), dtype=np.float64)


def price_joint(
    beliefs: Sequence[LegBelief],
    sides: Sequence[str],
    groups: Sequence[Sequence[int]],
    params: CorrelationParams,
) -> JointEstimate:
    """P(all legs settle on their selected side) with priced uncertainty.

    ``beliefs`` are YES-side marginals in leg order; ``sides`` the selected
    sides ("yes"/"no", already validated upstream); ``groups`` the same-event
    index blocks from the relationship classifier.
    """
    if len(beliefs) != len(sides) or not beliefs:
        raise ValueError("beliefs and sides must be same nonempty length")
    n = len(beliefs)
    marginals = [b.p if s == "yes" else 1.0 - b.p for b, s in zip(beliefs, sides, strict=True)]
    notes: list[str] = []

    corr = _signed_corr(
        n, groups, sides, rho=params.same_event_rho, cross_rho=params.cross_event_rho
    )
    p = gaussian_copula_joint_prob(marginals, corr)

    # Leg-uncertainty propagation (conservative linear sum).
    leg_unc = p * sum(
        b.uncertainty / max(m, _MIN_MARGINAL_FOR_GRADIENT)
        for b, m in zip(beliefs, marginals, strict=True)
    )

    # Correlation sensitivity, only when correlated blocks exist.
    corr_unc = 0.0
    if groups and params.rho_uncertainty > 0:
        rho_lo = max(-0.99, params.same_event_rho - params.rho_uncertainty)
        rho_hi = min(0.99, params.same_event_rho + params.rho_uncertainty)
        p_lo = gaussian_copula_joint_prob(
            marginals, _signed_corr(n, groups, sides, rho=rho_lo, cross_rho=params.cross_event_rho)
        )
        p_hi = gaussian_copula_joint_prob(
            marginals, _signed_corr(n, groups, sides, rho=rho_hi, cross_rho=params.cross_event_rho)
        )
        corr_unc = max(abs(p_hi - p), abs(p - p_lo), abs(p_hi - p_lo) / 2)
        notes.append(f"rho sensitivity: p({rho_lo:.2f})={p_lo:.4f} p({rho_hi:.2f})={p_hi:.4f}")

    lo, hi = frechet_bounds(marginals)
    return JointEstimate(
        p=clamp_to_frechet(p, marginals),
        uncertainty=leg_unc + corr_unc,
        frechet_lo=lo,
        frechet_hi=hi,
        notes=tuple(notes),
    )
