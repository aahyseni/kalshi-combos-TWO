"""Within-game hybrid: structural DC subgroup + copula-attached remainder.

A dense same-game soccer combo can carry a leg the Dixon-Coles scoreline model
cannot represent — in the prod tape this is ONLY corners (total KXWCCORNERS /
team KXWCTCORNERS). Today one such leg makes ``StructuralPricer.try_price``
decline for the WHOLE group, so the engine prices every leg with the v1 copula,
whose pairwise-ρ compounding over-states the advance+BTTS+scorer joint that DC
prices correctly (a real 3,344-contract WC combo: copula 8.76c vs the maker's
cleared 5.60c).

This module keeps the correct DC joint for the representable subgroup and
attaches the non-representable remainder through the SAME copula machinery, so
any real corner correlation is honoured (corners is measured ⊥ goals and ⊥
result, so the attach is near-independent, but it is not hard-wired to 1.0):

    P_hybrid = P_dc(subgroup) · [ P_cop(all legs) / P_cop(subgroup) ]

The bracket is the copula's conditional P(remainder | subgroup): it uses the
full signed per-pair correlation structure between each remainder leg and each
subgroup leg (and among remainder legs), and the within-subgroup correlations
cancel between numerator and denominator, so only the remainder-attachment
survives. Reduces to P_dc(subgroup)·P(remainder) exactly when the remainder is
independent of the subgroup in the copula.

SAFETY (do-no-harm — over-pricing is the safe direction, never under-price):
  * fail-closed to the copula (return None) on ANY doubt — not soccer, no
    non-representable remainder, fewer than two DC legs, the subgroup itself
    declines ``try_price`` (orientation guard / feasibility), or the copula
    subgroup joint is too small to divide;
  * the ratio is clamped to [0, 1] and the result to the Fréchet bounds, so the
    hybrid can never exceed the DC subgroup joint nor breach the marginals;
  * width is the MAX of the (ratio-scaled) structural width plus the remainder
    legs' marginal propagation AND today's full copula width — never tighter
    than the copula path it replaces (maker-favorable width preserved).
"""

from __future__ import annotations

from collections.abc import Sequence

from combomaker.pricing.copula import clamp_to_frechet, frechet_bounds
from combomaker.pricing.joint import JointEstimate, price_joint_matrices
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.legtypes import Sport, classify_sport
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.pricing.structural import StructuralPricer, soccer_representable
from combomaker.rfq.models import RfqLeg

# Gradient floor for leg-marginal uncertainty propagation (matches joint.py).
_MIN_MARGINAL_FOR_GRADIENT = 0.01
# Below this copula subgroup joint the conditional ratio is numerically unsafe
# to form (a tiny, noisy denominator) — fail closed to the copula instead.
_RATIO_DENOM_FLOOR = 0.02


def _copula(
    legs: Sequence[RfqLeg],
    beliefs: Sequence[LegBelief],
    sides: Sequence[str],
    sgp_params: SgpParams,
) -> JointEstimate:
    """The v1 copula joint for one same-game group (all legs in a single block)."""
    corr = build_sgp_correlation(
        legs, [tuple(range(len(legs)))], sgp_params, marginals=[b.p for b in beliefs]
    )
    return price_joint_matrices(
        beliefs, sides, corr.corr, corr.corr_low, corr.corr_high
    )


def price_within_game_hybrid(
    legs: Sequence[RfqLeg],
    beliefs: Sequence[LegBelief],
    sides: Sequence[str],
    structural: StructuralPricer,
    sgp_params: SgpParams,
) -> JointEstimate | None:
    """Structural DC subgroup × copula-conditional remainder, or None (→ copula).

    ``legs`` are ONE same-game group (the engine only reaches here when
    ``structural_applicable`` held and ``try_price`` on the whole group declined).
    Soccer-only: the non-representable taxonomy (corners) is soccer-specific;
    every other sport fails closed to the copula, exactly as today.
    """
    n = len(legs)
    if n < 3:
        # A non-representable remainder needs ≥1 leg and a ≥2-leg DC subgroup.
        return None
    if any(classify_sport(leg.market_ticker) is not Sport.SOCCER for leg in legs):
        return None

    sub = [i for i in range(n) if soccer_representable(legs[i])]
    rem = [i for i in range(n) if i not in sub]
    if not rem or len(sub) < 2:
        # No remainder to split off (would be the pure-structural case), or too
        # few DC-orienting legs to identify a scoreline — fail closed.
        return None

    sub_legs = [legs[i] for i in sub]
    sub_beliefs = [beliefs[i] for i in sub]
    sub_sides = [sides[i] for i in sub]
    sub_est, _reason = structural.try_price(sub_legs, sub_beliefs, sub_sides)
    if sub_est is None:
        # The DC-representable subset still can't be safely priced (orientation
        # guard, infeasible inversion, …) — do not get clever; use the copula.
        return None

    p_all = _copula(legs, beliefs, sides, sgp_params)
    p_sub = _copula(sub_legs, sub_beliefs, sub_sides, sgp_params)
    if p_sub.p < _RATIO_DENOM_FLOOR:
        return None

    # Copula conditional P(remainder | subgroup). Clamped to [0, 1]: adding legs
    # can only reduce a coherent joint, but independent PSD repairs of the two
    # correlation matrices plus MVN integration noise can nudge it a hair over.
    ratio = min(1.0, max(0.0, p_all.p / p_sub.p))

    marginals = [
        b.p if s == "yes" else 1.0 - b.p
        for b, s in zip(beliefs, sides, strict=True)
    ]
    p = clamp_to_frechet(sub_est.p * ratio, marginals)

    # Remainder legs' own marginal uncertainty (the subgroup's is inside
    # sub_est.uncertainty); same conservative linear-sum gradient as joint.py.
    rem_leg_unc = p * sum(
        beliefs[i].uncertainty / max(marginals[i], _MIN_MARGINAL_FOR_GRADIENT)
        for i in rem
    )
    # Maker-favorable: never tighter than either the (ratio-scaled) structural
    # width plus the remainder propagation, or today's full copula width.
    uncertainty = max(ratio * sub_est.uncertainty + rem_leg_unc, p_all.uncertainty)

    lo, hi = frechet_bounds(marginals)
    return JointEstimate(
        p=p,
        uncertainty=uncertainty,
        frechet_lo=lo,
        frechet_hi=hi,
        notes=(
            *sub_est.notes,
            f"within-game hybrid: dc_subgroup={sub_est.p:.4f} legs{tuple(sub)} "
            f"× copula P(remainder{tuple(rem)}|subgroup)={ratio:.4f} "
            f"(p_all_cop={p_all.p:.4f} p_sub_cop={p_sub.p:.4f})",
        ),
    )


__all__ = ["price_within_game_hybrid"]
