"""Pairwise max-entropy (Ising) joint pricer for parlay-style combo contracts.

STANDALONE / ADDITIVE prototype. Implements the ParlayMarket joint form
(Rana, Nadkarni, Moshrefi, Viswanath; arXiv:2603.22596) as an ALTERNATIVE
joint/correlation layer to the Gaussian copula in ``pricing/copula.py``.
Nothing in this module is imported by existing code, so the shipped test
suite is unaffected.

Model (their Eq. 11) over binary leg outcomes ``x = (x_1..x_M)``, ``x_i in {0,1}``::

    P_phi(x) = (1 / Z(phi)) * exp( sum_i theta_i x_i + sum_{i<j} W_ij x_i x_j )

``theta_i`` (bias) set the per-leg marginals; ``W_ij`` (interaction weights)
set the pairwise correlations. This is the maximum-entropy distribution
consistent with specified single- and pair-marginals; it has O(M^2) sufficient
statistics and prices every base market AND all ``2^M - 1`` combinations
coherently from one shared parameter vector ``phi = (theta, W)``.

For realistic combo sizes (``M = 2..6``) we enumerate all ``2^M`` outcomes
exactly, so the partition function ``Z``, every marginal, pair-marginal and
arbitrary sub-combination are exact (no belief-propagation / MCMC needed).

Calibration (their Eqs. 12-13). The composite pseudo-likelihood is a sum of
binary cross-entropies over the observed single- and pair-marginals::

    L(phi) = sum_i  lam_i  CE(p_i*,  p_i^phi)
           + sum_{i<j} lam_ij CE(p_ij*, p_ij^phi)
    CE(p, q) = -p log q - (1 - p) log(1 - q)

with the online SGD step ``phi_{t+1} = phi_t - eta * grad_phi CE(p_m*, p_m^phi)``.
Because the log-partition function is the exp-family cumulant generator, the
gradient of each CE term w.r.t. *its own* natural parameter collapses to the
moment residual::

    d CE(p_i*,  p_i^phi)  / d theta_i = p_i^phi  - p_i*
    d CE(p_ij*, p_ij^phi) / d W_ij    = p_ij^phi - p_ij*

so one SGD step provably nudges the parameter toward the empirical moment.
Batch fitting uses the full max-entropy gradient (data moment - model moment).

Everything here lives in probability space (floats in [0, 1]); money never
enters. This mirrors the discipline in ``pricing/copula.py``.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "IsingAMM",
    "fit_ising",
    "pairwise_frechet_bounds",
]

# Numeric floors so CE / logs never see 0 or 1 exactly.
_EPS = 1e-12
# Default batch-fit controls.
_FIT_MAX_ITERS = 20_000
_FIT_TOL = 1e-12
_FIT_LR = 0.5


def pairwise_frechet_bounds(p_i: float, p_j: float) -> tuple[float, float]:
    """Achievable range of ``P(x_i=1, x_j=1)`` given marginals ``p_i, p_j``.

    ``[max(0, p_i + p_j - 1), min(p_i, p_j)]`` — the 2-leg Fréchet bounds.
    """
    return max(0.0, p_i + p_j - 1.0), min(p_i, p_j)


class IsingAMM:
    """Pairwise max-entropy joint over ``M`` binary legs, priced by exact enumeration.

    Parameters
    ----------
    M:
        Number of binary legs (``2 <= M <= ~20`` for exact enumeration; the
        combo use-case is ``M = 2..6``).
    theta:
        Length-``M`` bias vector. Defaults to zeros.
    W:
        ``M x M`` symmetric interaction matrix, zero diagonal. Only the
        strict upper triangle carries information (``W_ij``); defaults to zeros
        (independent legs, whose joint is the exact product of marginals).
    """

    def __init__(
        self,
        M: int,
        theta: Sequence[float] | None = None,
        W: NDArray[np.float64] | None = None,
    ) -> None:
        if M < 1:
            raise ValueError(f"need at least one leg, got M={M}")
        if M > 22:
            raise ValueError(f"exact enumeration is for small M; got M={M}")
        self.M = int(M)
        self.theta = (
            np.zeros(M, dtype=np.float64)
            if theta is None
            else np.asarray(theta, dtype=np.float64).copy()
        )
        if self.theta.shape != (M,):
            raise ValueError(f"theta must have shape ({M},), got {self.theta.shape}")
        if W is None:
            self.W = np.zeros((M, M), dtype=np.float64)
        else:
            self.W = np.asarray(W, dtype=np.float64).copy()
            if self.W.shape != (M, M):
                raise ValueError(f"W must be {M}x{M}, got {self.W.shape}")
            # Symmetrize and zero the diagonal (self-interaction is folded into theta).
            self.W = (self.W + self.W.T) / 2.0
            np.fill_diagonal(self.W, 0.0)
        # Enumerate the 2^M outcome table once; it is reused for every query.
        self._X = np.array(
            list(itertools.product((0, 1), repeat=M)), dtype=np.float64
        )  # shape (2^M, M)

    # -- core distribution -------------------------------------------------

    def probs(self) -> NDArray[np.float64]:
        """The full normalized distribution over all ``2^M`` outcomes."""
        # log-weight of each outcome: theta.x + sum_{i<j} W_ij x_i x_j
        linear = self._X @ self.theta
        # 0.5 x^T W x == sum_{i<j} W_ij x_i x_j  (W symmetric, zero diagonal)
        pair = 0.5 * np.einsum("si,ij,sj->s", self._X, self.W, self._X)
        logw = linear + pair
        logw -= logw.max()  # stabilize
        w = np.exp(logw)
        return w / w.sum()

    def log_partition(self) -> float:
        """log Z(phi) — the cumulant generator (kept for completeness)."""
        linear = self._X @ self.theta
        pair = 0.5 * np.einsum("si,ij,sj->s", self._X, self.W, self._X)
        logw = linear + pair
        m = float(logw.max())
        return m + float(np.log(np.exp(logw - m).sum()))

    # -- moments / prices --------------------------------------------------

    def marginal(self, i: int) -> float:
        """``P(x_i = 1)`` — the base-market price of leg ``i``."""
        p = self.probs()
        return float(p @ self._X[:, i])

    def marginals(self) -> NDArray[np.float64]:
        """All single-leg marginals ``(p_1..p_M)``."""
        return self.probs() @ self._X

    def pairwise(self, i: int, j: int) -> float:
        """``P(x_i = 1, x_j = 1)`` — the 2-leg joint price."""
        p = self.probs()
        return float(p @ (self._X[:, i] * self._X[:, j]))

    def pairwise_matrix(self) -> NDArray[np.float64]:
        """``M x M`` matrix of pair-marginals ``P(x_i=1, x_j=1)`` (diagonal = marginals)."""
        p = self.probs()
        return np.einsum("s,si,sj->ij", p, self._X, self._X)

    def subset_joint(self, subset: Sequence[int]) -> float:
        """``P(all legs in ``subset`` settle YES)`` — the parlay price for any subset.

        Marginalizes over the legs not in ``subset``. Empty subset -> 1.0.
        """
        idx = list(subset)
        if not idx:
            return 1.0
        for k in idx:
            if not 0 <= k < self.M:
                raise ValueError(f"leg index {k} out of range for M={self.M}")
        p = self.probs()
        mask = np.prod(self._X[:, idx], axis=1)  # 1 iff every leg in subset is YES
        return float(p @ mask)

    def joint_all_yes(self) -> float:
        """``P(all M legs settle YES)`` — the full-parlay price."""
        return self.subset_joint(range(self.M))

    # -- calibration: single online SGD step (their Eq. 13) ---------------

    def sgd_step(
        self,
        eta: float,
        *,
        target_marginals: Sequence[float] | None = None,
        target_pairs: dict[tuple[int, int], float] | None = None,
        lam_marginal: float = 1.0,
        lam_pair: float = 1.0,
    ) -> dict[str, float]:
        """One composite-CE SGD step (Eq. 13) from observed single/pair moments.

        Updates ``theta`` and ``W`` in place by
        ``phi <- phi - eta * lam * grad_phi CE(p*, p^phi)``, using the exact
        exp-family identity that each CE term's gradient w.r.t. its own natural
        parameter is the moment residual ``p^phi - p*``. Returns the residuals
        applied (post-step recompute is left to the caller).

        Pass whichever moments the observed trade revealed:
        ``target_marginals`` (per-leg) and/or ``target_pairs`` mapping
        ``(i, j) -> p_ij*``.
        """
        info: dict[str, float] = {}
        if target_marginals is not None:
            model_m = self.marginals()
            for i, p_star in enumerate(target_marginals):
                if p_star is None:
                    continue
                resid = float(model_m[i]) - float(p_star)  # dCE/dtheta_i
                self.theta[i] -= eta * lam_marginal * resid
                info[f"resid_theta_{i}"] = resid
        if target_pairs is not None:
            for (i, j), p_star in target_pairs.items():
                a, b = (i, j) if i < j else (j, i)
                resid = self.pairwise(a, b) - float(p_star)  # dCE/dW_ij
                step = eta * lam_pair * resid
                self.W[a, b] -= step
                self.W[b, a] -= step
                info[f"resid_W_{a}{b}"] = resid
        return info

    # -- calibration: batch max-entropy fit (moment matching) -------------

    def fit(
        self,
        target_marginals: Sequence[float],
        target_pairs: dict[tuple[int, int], float],
        *,
        lr: float = _FIT_LR,
        max_iters: int = _FIT_MAX_ITERS,
        tol: float = _FIT_TOL,
    ) -> dict[str, float]:
        """Batch-fit ``theta, W`` so the model reproduces the target moments.

        Gradient ascent on the max-entropy log-likelihood, whose gradient is
        ``data_moment - model_moment`` for every sufficient statistic — i.e.
        repeatedly running the Eq. 13 SGD step to convergence. Deterministic,
        no randomness. Returns fit diagnostics (iterations, final max residual).
        """
        tm = [float(p) for p in target_marginals]
        if len(tm) != self.M:
            raise ValueError(f"need {self.M} target marginals, got {len(tm)}")
        for i, p in enumerate(tm):
            if not 0.0 < p < 1.0:
                raise ValueError(f"target marginal {i} must be strictly in (0,1), got {p}")
        norm_pairs: dict[tuple[int, int], float] = {}
        for (i, j), p_ij in target_pairs.items():
            a, b = (i, j) if i < j else (j, i)
            lo, hi = pairwise_frechet_bounds(tm[a], tm[b])
            if not lo - 1e-9 <= p_ij <= hi + 1e-9:
                raise ValueError(
                    f"target P(x{a}, x{b})={p_ij} outside Frechet bounds [{lo:.4f},{hi:.4f}]"
                )
            norm_pairs[(a, b)] = min(max(p_ij, lo + _EPS), hi - _EPS)

        max_resid = math.inf
        it = 0
        for _it in range(1, max_iters + 1):
            model_m = self.marginals()
            model_pair = self.pairwise_matrix()
            max_resid = 0.0
            # marginals -> theta
            for i in range(self.M):
                r = tm[i] - float(model_m[i])
                self.theta[i] += lr * r
                max_resid = max(max_resid, abs(r))
            # pair-marginals -> W
            for (a, b), p_star in norm_pairs.items():
                r = p_star - float(model_pair[a, b])
                self.W[a, b] += lr * r
                self.W[b, a] += lr * r
                max_resid = max(max_resid, abs(r))
            if max_resid < tol:
                break
        return {"iters": float(it), "max_residual": float(max_resid)}


def fit_ising(
    target_marginals: Sequence[float],
    target_pairs: dict[tuple[int, int], float],
    **kwargs: float,
) -> IsingAMM:
    """Convenience: build and batch-fit an :class:`IsingAMM` in one call."""
    model = IsingAMM(len(target_marginals))
    model.fit(target_marginals, target_pairs, **kwargs)  # type: ignore[arg-type]
    return model
