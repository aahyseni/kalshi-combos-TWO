"""Negative-binomial runs model for MLB same-game parlays.

Baseball scores are discrete, low, and overdispersed (runs per team: mean
~4.4, variance ~9.5) — a bivariate normal is the wrong shape, so MLB gets its
own structural core: FINAL runs per team ~ NegBin(mu, k) independent across
teams, with the tie diagonal removed and the grid renormalized (baseball has
no ties — extra innings resolve them, and calibrating k on final scores means
extras' effect on totals is already inside the distribution).

Per-game means invert from live leg prices exactly like the other structural
models; the dispersion k is the only sport-shape parameter, calibrated from
recent Retrosheet seasons (tools/calibrate_mlb_runs.py). Home/away dispersion
asymmetry (the leading home team skips the bottom 9th) is NOT modeled — the
ticker doesn't reveal which side is home — and is covered by the k band.

Consumes the same leg specs as margin_total (TeamWins / SpreadCover /
GameTotalOver): an MLB spread is a run line ("wins by over n-0.5") and a
total is "over n-0.5", both doc-verified from live market metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import least_squares
from scipy.stats import nbinom

from combomaker.pricing.dixon_coles import StructuralError, Team
from combomaker.pricing.margin_total import GameTotalOver, MTLegSpec, SpreadCover, TeamWins

_MAX_RUNS = 30
_MU_BOUNDS = (0.8, 12.0)


@dataclass(frozen=True, slots=True)
class MlbShape:
    dispersion_k: float  # NegBin k: var = mu + mu^2/k


@lru_cache(maxsize=128)
def _tie_free_grid(mu_a: float, mu_b: float, k: float) -> NDArray[np.float64]:
    """Joint pmf over (runs_a, runs_b), ties removed, renormalized."""
    runs = np.arange(_MAX_RUNS + 1)
    # scipy nbinom: n = k (shape), p = k / (k + mu)
    pa = nbinom.pmf(runs, k, k / (k + mu_a))
    pb = nbinom.pmf(runs, k, k / (k + mu_b))
    grid = np.outer(pa, pb)
    np.fill_diagonal(grid, 0.0)
    total = float(grid.sum())
    if not total > 0.0:
        raise StructuralError(f"degenerate runs grid (mu={mu_a},{mu_b})")
    return np.asarray(grid / total, dtype=np.float64)


def _indicator(spec: MTLegSpec, grid_shape: int) -> NDArray[np.float64]:
    a, b = np.meshgrid(np.arange(grid_shape), np.arange(grid_shape), indexing="ij")
    if isinstance(spec, TeamWins):
        return np.asarray(a > b if spec.team is Team.A else b > a, dtype=np.float64)
    if isinstance(spec, SpreadCover):
        margin = a - b if spec.team is Team.A else b - a
        return np.asarray(margin > spec.line, dtype=np.float64)
    if isinstance(spec, GameTotalOver):
        return np.asarray((a + b) > spec.threshold, dtype=np.float64)
    raise StructuralError(f"leg spec {spec} not representable in the runs model")


def joint_probability(
    mu_a: float, mu_b: float, shape: MlbShape, legs: list[tuple[MTLegSpec, bool]]
) -> float:
    grid = _tie_free_grid(mu_a, mu_b, shape.dispersion_k)
    factor = grid.copy()
    for spec, yes in legs:
        ind = _indicator(spec, _MAX_RUNS + 1)
        factor *= ind if yes else 1.0 - ind
    return float(factor.sum())


def marginal_probability(
    mu_a: float, mu_b: float, shape: MlbShape, spec: MTLegSpec
) -> float:
    return joint_probability(mu_a, mu_b, shape, [(spec, True)])


@dataclass(frozen=True, slots=True)
class InvertedRuns:
    mu_a: float
    mu_b: float
    residual: float
    notes: tuple[str, ...]


def invert_runs(
    legs: list[tuple[MTLegSpec, float]],
    shape: MlbShape,
    warm_start: tuple[float, float] | None = None,
) -> InvertedRuns:
    """Solve (mu_a, mu_b) from leg marginals. Needs a winner-flavored AND a
    totals-flavored constraint to identify both means (a lone ML pins only
    the ratio; a lone total pins only the sum)."""
    for spec, p in legs:
        if not 0.001 <= p <= 0.999:
            raise StructuralError(f"marginal {p} out of invertible range for {spec}")
    has_margin = any(isinstance(s, (TeamWins, SpreadCover)) for s, _ in legs)
    has_total = any(isinstance(s, GameTotalOver) for s, _ in legs)
    if not (has_margin and has_total):
        raise StructuralError(
            "runs inversion needs both a winner-flavored and a total-flavored leg"
        )

    start = warm_start or (4.4, 4.4)

    def residuals(x: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.array(
            [
                joint_probability(float(x[0]), float(x[1]), shape, [(spec, True)]) - p
                for spec, p in legs
            ]
        )

    fit = least_squares(
        residuals,
        x0=np.clip(np.array(start, dtype=np.float64), *_MU_BOUNDS),
        bounds=(np.full(2, _MU_BOUNDS[0]), np.full(2, _MU_BOUNDS[1])),
        xtol=1e-11,
        ftol=1e-11,
        gtol=1e-11,
    )
    mu_a, mu_b = float(fit.x[0]), float(fit.x[1])
    residual = float(np.abs(residuals(np.asarray(fit.x))).max())
    if residual > 0.05:
        raise StructuralError(f"legs mutually inconsistent: residual {residual:.3f}")
    if len(legs) <= 2 and residual > 0.005:
        raise StructuralError(f"inversion residual {residual:.4f} on exact system")
    return InvertedRuns(
        mu_a=mu_a,
        mu_b=mu_b,
        residual=residual,
        notes=(f"mlb inversion: mu_a={mu_a:.2f} mu_b={mu_b:.2f} residual={residual:.4f}",),
    )
