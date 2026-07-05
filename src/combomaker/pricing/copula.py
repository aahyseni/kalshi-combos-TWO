"""Gaussian-copula joint probabilities for combo legs.

Exact/analytic pricing path for small numbers of legs; Monte Carlo sampling
lives elsewhere. Model: leg ``i`` settles YES with marginal probability
``p_i``; a latent vector ``Z ~ MVN(0, R)`` with correlation matrix ``R``
drives outcomes via ``leg i is YES  <=>  Z_i <= Phi^{-1}(p_i)``, so
``P(all YES)`` is the MVN CDF evaluated at those thresholds.

Everything in this module lives in probability space (floats in ``[0, 1]``);
money never enters here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray
from scipy.stats import multivariate_normal, norm

__all__ = [
    "build_block_corr",
    "clamp_to_frechet",
    "conditional_joint_prob",
    "frechet_bounds",
    "gaussian_copula_joint_prob",
    "is_psd",
    "nearest_psd",
]

# Fixed seed for the randomized-QMC integrator inside scipy's MVN CDF: the
# same inputs must always price to the same number.
_MVN_SEED = 20260705

# At or below this dimension we request a tight absolute tolerance from the
# integrator (this module is the small-n exact path); above it, scipy's
# defaults apply.
_TIGHT_ABSEPS_MAX_DIM = 4
_TIGHT_ABSEPS = 1e-10

# Eigenvalue floor for nearest_psd's clip: keeps the repaired matrix strictly
# away from numerical indefiniteness so downstream Cholesky/CDF calls succeed.
_EIG_FLOOR = 1e-12

# Default tolerance on the most-negative eigenvalue when deciding PSD-ness.
_PSD_TOL = 1e-10


def _validate_ps(ps: Sequence[float]) -> list[float]:
    """Check marginals: at least one leg, every p in [0, 1] (NaN rejected)."""
    if len(ps) == 0:
        raise ValueError("need at least one leg")
    out = [float(p) for p in ps]
    for i, p in enumerate(out):
        if not 0.0 <= p <= 1.0:  # also False (=> raise) when p is NaN
            raise ValueError(f"marginal probability out of range at index {i}: {p}")
    return out


def _validate_corr(corr: NDArray[np.float64], n: int) -> NDArray[np.float64]:
    """Check a correlation matrix: square n x n, symmetric, unit diagonal, PSD."""
    m = np.asarray(corr, dtype=np.float64)
    if m.ndim != 2 or m.shape[0] != m.shape[1]:
        raise ValueError(f"correlation matrix must be square, got shape {m.shape}")
    if m.shape[0] != n:
        raise ValueError(f"correlation matrix is {m.shape[0]}x{m.shape[0]}, expected {n}x{n}")
    if not np.allclose(m, m.T, rtol=0.0, atol=1e-10):
        raise ValueError("correlation matrix must be symmetric")
    if not np.allclose(np.diag(m), 1.0, rtol=0.0, atol=1e-10):
        raise ValueError("correlation matrix must have unit diagonal")
    if not is_psd(m, tol=_PSD_TOL):
        raise ValueError("correlation matrix must be positive semidefinite")
    return m


def frechet_bounds(ps: Sequence[float]) -> tuple[float, float]:
    """Fréchet–Hoeffding bounds on P(all legs YES) given marginals ``ps``.

    Returns ``(max(0, sum(ps) - (n - 1)), min(ps))``; requires ``n >= 1``.
    """
    vals = _validate_ps(ps)
    n = len(vals)
    lower = max(0.0, math.fsum(vals) - (n - 1))
    upper = min(vals)
    return lower, upper


def clamp_to_frechet(p_joint: float, ps: Sequence[float]) -> float:
    """Clamp a joint probability into the Fréchet–Hoeffding bounds for ``ps``."""
    lower, upper = frechet_bounds(ps)
    return min(max(p_joint, lower), upper)


def is_psd(m: NDArray[np.float64], *, tol: float = _PSD_TOL) -> bool:
    """True iff ``m`` is symmetric with smallest eigenvalue ``>= -tol``."""
    a = np.asarray(m, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        return False
    if a.size == 0:
        return True
    if not np.allclose(a, a.T, rtol=0.0, atol=1e-10):
        return False
    eigenvalues = np.linalg.eigvalsh((a + a.T) / 2.0)
    return bool(eigenvalues.min() >= -tol)


def nearest_psd(m: NDArray[np.float64]) -> NDArray[np.float64]:
    """Higham-style projection of a symmetric matrix to a PSD correlation matrix.

    Single pass: symmetrize, clip eigenvalues at a tiny positive floor,
    reconstruct, then rescale to unit diagonal (a congruence transform, so
    PSD-ness is preserved). Already-PSD unit-diagonal inputs come back
    unchanged up to floating-point noise.
    """
    a = np.asarray(m, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise ValueError(f"matrix must be square, got shape {a.shape}")
    sym = (a + a.T) / 2.0
    eigenvalues, eigenvectors = np.linalg.eigh(sym)
    clipped = np.clip(eigenvalues, _EIG_FLOOR, None)
    rebuilt = (eigenvectors * clipped) @ eigenvectors.T
    scale = np.sqrt(np.diag(rebuilt))
    rebuilt = rebuilt / np.outer(scale, scale)
    rebuilt = (rebuilt + rebuilt.T) / 2.0
    np.fill_diagonal(rebuilt, 1.0)
    return np.asarray(rebuilt, dtype=np.float64)


def _mvn_cdf(z: NDArray[np.float64], corr: NDArray[np.float64]) -> float:
    """MVN(0, corr) CDF at ``z``, deterministic (fixed QMC seed), tight for small n."""
    k = z.shape[0]
    if k <= _TIGHT_ABSEPS_MAX_DIM:
        raw = multivariate_normal.cdf(
            z,
            mean=np.zeros(k),
            cov=corr,
            allow_singular=True,
            abseps=_TIGHT_ABSEPS,
            releps=0.0,
            rng=np.random.default_rng(_MVN_SEED),
        )
    else:
        raw = multivariate_normal.cdf(
            z,
            mean=np.zeros(k),
            cov=corr,
            allow_singular=True,
            rng=np.random.default_rng(_MVN_SEED),
        )
    return float(raw)


def gaussian_copula_joint_prob(ps: Sequence[float], corr: NDArray[np.float64]) -> float:
    """P(all legs YES) under a Gaussian copula with marginals ``ps`` and correlation ``corr``.

    Degeneracies: any ``p_i == 0`` gives 0; legs with ``p_i == 1`` are dropped
    (certain events don't constrain the joint); a single remaining leg is its
    own marginal. The result is always clamped to the Fréchet–Hoeffding bounds,
    since the numerical MVN CDF can breach them by integration noise.
    """
    vals = _validate_ps(ps)
    m = _validate_corr(corr, len(vals))
    if any(p == 0.0 for p in vals):
        return 0.0
    keep = [i for i, p in enumerate(vals) if p < 1.0]
    if not keep:
        return 1.0  # every leg certain
    if len(keep) == 1:
        return clamp_to_frechet(vals[keep[0]], vals)
    sub_ps = [vals[i] for i in keep]
    sub_corr: NDArray[np.float64] = m[np.ix_(keep, keep)]
    if np.array_equal(sub_corr, np.eye(len(keep))):
        # Independent legs: the joint is exactly the product; skip the integrator.
        return clamp_to_frechet(math.prod(sub_ps), vals)
    z = np.asarray(norm.ppf(sub_ps), dtype=np.float64)
    return clamp_to_frechet(_mvn_cdf(z, sub_corr), vals)


def conditional_joint_prob(
    ps: Sequence[float],
    corr: NDArray[np.float64],
    *,
    given: int,
    value: bool,
) -> float:
    """P(all other legs YES | leg ``given`` settles ``value``), as a ratio of joint CDFs.

    ``P(others | YES) = P(all YES) / p_given`` and
    ``P(others | NO) = (P(others YES) - P(all YES)) / (1 - p_given)``.
    Raises ValueError if ``p_given`` is 0 or 1 (conditioning on a degenerate
    leg is a caller bug). With a single leg the conjunction over "others" is
    empty, so the result is 1.
    """
    vals = _validate_ps(ps)
    n = len(vals)
    m = _validate_corr(corr, n)
    if not 0 <= given < n:
        raise ValueError(f"given index {given} out of range for {n} legs")
    p_given = vals[given]
    if p_given in (0.0, 1.0):
        raise ValueError(f"cannot condition on leg {given} with degenerate marginal {p_given}")
    if n == 1:
        return 1.0
    others = [i for i in range(n) if i != given]
    others_ps = [vals[i] for i in others]
    others_corr: NDArray[np.float64] = m[np.ix_(others, others)]
    p_all = gaussian_copula_joint_prob(vals, m)
    p_others = gaussian_copula_joint_prob(others_ps, others_corr)
    if value:
        result = p_all / p_given
    else:
        result = (p_others - p_all) / (1.0 - p_given)
    # Guard against integration noise pushing the ratio just outside [0, 1].
    return min(max(result, 0.0), 1.0)


def build_block_corr(
    n: int,
    blocks: Sequence[tuple[Sequence[int], float]],
    *,
    default_rho: float = 0.0,
) -> NDArray[np.float64]:
    """Build an ``n x n`` correlation matrix from pairwise-constant blocks.

    Starts at ``default_rho`` everywhere off-diagonal, then for each
    ``(indices, rho)`` sets that rho on every ordered pair within the block;
    later blocks override earlier ones on overlapping pairs. Diagonal is 1.
    All rhos must lie strictly inside (-1, 1). If the assembled matrix is not
    PSD, it is repaired via :func:`nearest_psd`.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    _validate_rho(default_rho)
    m = np.full((n, n), default_rho, dtype=np.float64)
    np.fill_diagonal(m, 1.0)
    for indices, rho in blocks:
        _validate_rho(rho)
        idx = [int(i) for i in indices]
        for i in idx:
            if not 0 <= i < n:
                raise ValueError(f"block index {i} out of range for n={n}")
        if len(set(idx)) != len(idx):
            raise ValueError(f"duplicate index within block {idx}")
        for a in idx:
            for b in idx:
                if a != b:
                    m[a, b] = rho
    if not is_psd(m):
        m = nearest_psd(m)
    return m


def _validate_rho(rho: float) -> None:
    """Pairwise correlations must lie strictly inside (-1, 1)."""
    if not -1.0 < rho < 1.0:  # also False (=> raise) when rho is NaN
        raise ValueError(f"rho must be in (-1, 1), got {rho}")
