"""Bivariate-normal (margin, total) structural model for NFL/NBA/WNBA SGPs.

The game state is X = (M, T): M = team_a points - team_b points, T = combined
points, modeled jointly normal. Sport-level shape parameters (sigma_margin,
sigma_total, rho) are calibrated offline from recent seasons
(tools/calibrate_margin_total.py); the per-game means (mu_M, mu_T) are
INVERTED from the live leg prices, so who-is-better always comes from the
market, never from history.

Every supported leg is a halfplane in (M, T):
    team win        M > 0            (ties are measure-zero in the model;
                                      the discreteness band covers reality)
    spread cover    M > line
    game total      T > threshold
    team total      (T +/- M)/2 > threshold

The joint of any leg set is therefore an exact region probability — computed
by 1D Gauss-Legendre integration over M of the conditional normal T-interval,
which handles ANY mix of margin, total, and diagonal (team-total) constraints
without copula approximation. The structure prices for free what the v1
copula hand-encodes: ML x spread is comonotone (implied rho ~0.88+), ML x
total is near-independent (measured rho(M,T) residual ~0.0-0.03), team totals
correlate with both coherently.

Same honesty contract as the soccer model: identification failures,
unrepresentable legs, and infeasible marginals raise StructuralError and the
caller falls back to the v1 copula.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.polynomial.legendre import leggauss
from numpy.typing import NDArray
from scipy.optimize import least_squares
from scipy.stats import norm

from combomaker.pricing.dixon_coles import StructuralError, Team

_GL_NODES = 96
_MU_M_BOUND = 60.0
_MU_T_BOUNDS = (20.0, 400.0)


@dataclass(frozen=True, slots=True)
class SportShape:
    """Calibrated per-sport shape of (M, T). Means are per-game, inverted."""

    sigma_margin: float
    sigma_total: float
    rho: float


# --- leg specs (YES-side events, team frame explicit) ---------------------------


@dataclass(frozen=True, slots=True)
class TeamWins:
    team: Team


@dataclass(frozen=True, slots=True)
class SpreadCover:
    """YES = the team's margin exceeds ``line`` (line < 0 = getting points)."""

    team: Team
    line: float


@dataclass(frozen=True, slots=True)
class GameTotalOver:
    threshold: float  # YES = T > threshold (adapter applies any continuity fix)


@dataclass(frozen=True, slots=True)
class TeamTotalOver:
    team: Team
    threshold: float


MTLegSpec = TeamWins | SpreadCover | GameTotalOver | TeamTotalOver


def _halfplane(spec: MTLegSpec) -> tuple[float, float, float]:
    """(a, b, c): the YES event is a*M + b*T > c."""
    if isinstance(spec, TeamWins):
        return (1.0, 0.0, 0.0) if spec.team is Team.A else (-1.0, 0.0, 0.0)
    if isinstance(spec, SpreadCover):
        sign = 1.0 if spec.team is Team.A else -1.0
        return (sign, 0.0, spec.line)
    if isinstance(spec, GameTotalOver):
        return (0.0, 1.0, spec.threshold)
    sign = 1.0 if spec.team is Team.A else -1.0
    return (sign * 0.5, 0.5, spec.threshold)


# --- region probability ---------------------------------------------------------


def region_probability(
    mu_m: float,
    mu_t: float,
    shape: SportShape,
    legs: list[tuple[MTLegSpec, bool]],
) -> float:
    """P(every leg settles on its selected side) — exact up to quadrature.

    Margin-only constraints reduce to an interval on M; everything else is,
    conditional on M = m, an interval on T (the conditional is normal). The
    result is a single 1D integral, robust for any leg mix.
    """
    m_lo, m_hi = -math.inf, math.inf
    # conditional-T constraints as (b_sign, slope, intercept, keep_above)
    t_constraints: list[tuple[float, float, bool]] = []
    for spec, yes in legs:
        a, b, c = _halfplane(spec)
        if b == 0.0:
            bound = c / a  # dividing by a<0 flips the inequality
            if (a > 0) == yes:
                m_lo = max(m_lo, bound)
            else:
                m_hi = min(m_hi, bound)
            continue
        # a*M + b*T > c (b > 0 in all our specs) => T > (c - a*M)/b for YES
        t_constraints.append((a / b, c / b, yes))
    if m_lo >= m_hi:
        return 0.0

    sm, st, rho = shape.sigma_margin, shape.sigma_total, shape.rho
    lo = max(m_lo, mu_m - 8.5 * sm)
    hi = min(m_hi, mu_m + 8.5 * sm)
    if lo >= hi:
        return 0.0
    nodes, weights = leggauss(_GL_NODES)
    m = 0.5 * (hi - lo) * nodes + 0.5 * (hi + lo)
    w = 0.5 * (hi - lo) * weights
    dens = norm.pdf(m, loc=mu_m, scale=sm)

    cond_mu = mu_t + rho * st / sm * (m - mu_m)
    cond_sd = st * math.sqrt(max(1e-12, 1.0 - rho * rho))
    t_lo = np.full_like(m, -np.inf)
    t_hi = np.full_like(m, np.inf)
    for slope, intercept, yes in t_constraints:
        t_bound = intercept - slope * m
        if yes:
            t_lo = np.maximum(t_lo, t_bound)
        else:
            t_hi = np.minimum(t_hi, t_bound)
    upper = norm.cdf((t_hi - cond_mu) / cond_sd)
    lower = norm.cdf((t_lo - cond_mu) / cond_sd)
    prob = float(np.sum(w * dens * np.clip(upper - lower, 0.0, 1.0)))
    return min(1.0, max(0.0, prob))


def marginal_probability(
    mu_m: float, mu_t: float, shape: SportShape, spec: MTLegSpec
) -> float:
    return region_probability(mu_m, mu_t, shape, [(spec, True)])


# --- inversion ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvertedMeans:
    mu_m: float
    mu_t: float
    residual: float
    notes: tuple[str, ...]


def invert_means(
    legs: list[tuple[MTLegSpec, float]],  # (spec, YES-side market marginal)
    shape: SportShape,
    warm_start: tuple[float, float] | None = None,
) -> InvertedMeans:
    """Solve (mu_M, mu_T) from the leg marginals. StructuralError when the
    leg directions cannot identify the needed means (fail-safe: an unpinned
    mean silently defaulting would misprice every leg that touches it)."""
    for spec, p in legs:
        if not 0.001 <= p <= 0.999:
            raise StructuralError(f"marginal {p} out of invertible range for {spec}")

    dirs = np.array([_halfplane(spec)[:2] for spec, _ in legs])
    needs_m = bool(np.any(dirs[:, 0] != 0.0))
    needs_t = bool(np.any(dirs[:, 1] != 0.0))
    if needs_m and needs_t and np.linalg.matrix_rank(dirs) < 2:
        raise StructuralError("leg directions cannot identify both means")

    if warm_start is not None:
        start = warm_start
    else:
        t_scale = [
            _halfplane(s)[2] / _halfplane(s)[1]
            for s, _ in legs
            if _halfplane(s)[1] != 0.0
        ]
        start = (0.0, float(np.median(t_scale)) if t_scale else 0.0)

    free = [needs_m, needs_t]

    def unpack(x: NDArray[np.float64]) -> tuple[float, float]:
        vals = [start[0], start[1]]
        j = 0
        for i in range(2):
            if free[i]:
                vals[i] = float(x[j])
                j += 1
        return vals[0], vals[1]

    def residuals(x: NDArray[np.float64]) -> NDArray[np.float64]:
        mu_m, mu_t = unpack(x)
        return np.array(
            [marginal_probability(mu_m, mu_t, shape, spec) - p for spec, p in legs]
        )

    x0 = np.array([start[i] for i in range(2) if free[i]], dtype=np.float64)
    lo = np.array(
        [(-_MU_M_BOUND if i == 0 else _MU_T_BOUNDS[0]) for i in range(2) if free[i]]
    )
    hi = np.array(
        [(_MU_M_BOUND if i == 0 else _MU_T_BOUNDS[1]) for i in range(2) if free[i]]
    )
    fit = least_squares(
        residuals,
        x0=np.clip(x0, lo, hi),
        bounds=(lo, hi),
        xtol=1e-12,
        ftol=1e-12,
        gtol=1e-12,
    )
    mu_m, mu_t = unpack(np.asarray(fit.x))
    resid = np.abs(residuals(np.asarray(fit.x)))
    residual = float(resid.max())
    n_free = int(sum(free))
    if residual > 0.05:
        # Over-identified misfit is priced into width, but a residual this
        # large means the legs contradict each other outright (e.g. an ML
        # and a spread implying opposite favorites) — refuse, don't widen.
        raise StructuralError(f"legs mutually inconsistent: residual {residual:.3f}")
    if len(legs) <= n_free and residual > 0.005:
        raise StructuralError(f"inversion residual {residual:.4f} on exact system")
    return InvertedMeans(
        mu_m=mu_m,
        mu_t=mu_t,
        residual=residual,
        notes=(f"mt inversion: mu_M={mu_m:+.2f} mu_T={mu_t:.1f} residual={residual:.4f}",),
    )
