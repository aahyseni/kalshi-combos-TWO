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
    # Max |model − market| over the identifying leg constraints of a structural
    # inversion (0.0 for copula / containment / exact-identified paths, which
    # carry no over-identification misfit). Surfaced so the fit can be persisted
    # and challenged (P1-4): a residual below the hard REJECT bar but elevated is
    # an *inconsistent-but-priceable* fit — recorded, and widen-flagged, never a
    # silent accept.
    residual: float = 0.0


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


def price_joint_matrices(
    beliefs: Sequence[LegBelief],
    sides: Sequence[str],
    corr: NDArray[np.float64],
    corr_low: NDArray[np.float64],
    corr_high: NDArray[np.float64],
    *,
    extra_notes: Sequence[str] = (),
) -> JointEstimate:
    """Joint from explicit YES–YES correlation matrices (SGP structured path).

    The (low, high) matrices bound the correlation uncertainty; the joint is
    re-priced at both bounds and the spread priced into width, exactly like
    the flat-ρ sensitivity but per-pair.
    """
    if len(beliefs) != len(sides) or not beliefs:
        raise ValueError("beliefs and sides must be same nonempty length")
    marginals = [b.p if s == "yes" else 1.0 - b.p for b, s in zip(beliefs, sides, strict=True)]
    signs = np.array([1.0 if side == "yes" else -1.0 for side in sides])
    flip = np.outer(signs, signs)

    p = gaussian_copula_joint_prob(marginals, np.asarray(corr * flip, dtype=np.float64))
    p_lo = gaussian_copula_joint_prob(marginals, np.asarray(corr_low * flip, dtype=np.float64))
    p_hi = gaussian_copula_joint_prob(marginals, np.asarray(corr_high * flip, dtype=np.float64))
    corr_unc = max(abs(p_hi - p), abs(p - p_lo), abs(p_hi - p_lo) / 2)

    leg_unc = p * sum(
        b.uncertainty / max(m, _MIN_MARGINAL_FOR_GRADIENT)
        for b, m in zip(beliefs, marginals, strict=True)
    )
    lo, hi = frechet_bounds(marginals)
    return JointEstimate(
        p=clamp_to_frechet(p, marginals),
        uncertainty=leg_unc + corr_unc,
        frechet_lo=lo,
        frechet_hi=hi,
        notes=(*extra_notes, f"corr band: p_lo={p_lo:.4f} p_hi={p_hi:.4f}"),
    )


def price_containment(
    beliefs: Sequence[LegBelief],
    sides: Sequence[str],
    containment: tuple[int, int],
    *,
    extra_notes: Sequence[str] = (),
) -> JointEstimate:
    """Joint of a logically-contained pair: YES(subset) ⟹ YES(superset), so
    P(subset ∧ superset) = P(subset), exactly (the Fréchet upper bound). Used
    for 1H-BTTS ⟹ FT-BTTS, which the copula's pairwise ρ cannot express. Only
    the all-YES 2-leg case reaches here (relationships.py returns IMPOSSIBLE for
    subset-yes × superset-no). ``clamp_to_frechet`` still guards the pathological
    case of a market that misprices the subset above the superset."""
    if len(beliefs) != len(sides) or not beliefs:
        raise ValueError("beliefs and sides must be same nonempty length")
    subset, _superset = containment
    marginals = [b.p if s == "yes" else 1.0 - b.p for b, s in zip(beliefs, sides, strict=True)]
    lo, hi = frechet_bounds(marginals)
    # Joint tracks the subset marginal exactly, so its width is the subset leg's.
    return JointEstimate(
        p=clamp_to_frechet(marginals[subset], marginals),
        uncertainty=beliefs[subset].uncertainty,
        frechet_lo=lo,
        frechet_hi=hi,
        notes=(*extra_notes, f"containment: joint = P(leg {subset}) = {marginals[subset]:.4f}"),
    )


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
