"""Margin-free normalization of probability vectors.

This is deliberately NOT devig. Kalshi binaries carry no bookmaker margin
(yes + no = $1 by construction; fees are handled separately in the fee module),
so Kalshi-sourced leg probabilities must never pass through a margin-removal
model. The one Kalshi-side use of this math is **cross-family normalization**:
when a leg's probability is derived from a set of mutually exclusive Kalshi
markets (e.g. the outcomes of one event) whose mids don't sum to 100% because
of microstructure noise, ``normalize_exclusive_family`` renormalizes them onto
the simplex.

``pricing/devig.py`` builds its external-odds margin removal on the same
solvers; that module may only be imported from external ``OddsSource``
adapters (enforced by ``tests/test_architecture.py``). See CLAUDE.md
decision #8.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum

from scipy.optimize import brentq


class NormalizeMethod(StrEnum):
    """How to project a positive vector onto the probability simplex."""

    PROPORTIONAL = "proportional"
    POWER = "power"


def validate_probability_vector(values: Sequence[float], *, min_outcomes: int = 2) -> list[float]:
    """Validate a vector of probabilities: each in (0, 1), at least ``min_outcomes``."""
    probs = [float(p) for p in values]
    if len(probs) < min_outcomes:
        raise ValueError(f"need at least {min_outcomes} outcomes, got {len(probs)}")
    for p in probs:
        if not (math.isfinite(p) and 0.0 < p < 1.0):
            raise ValueError(f"probability must be in (0, 1), got {p!r}")
    return probs


def normalize_proportional(raw: Sequence[float]) -> list[float]:
    """Proportionally rescale ``raw`` (all positive) to sum to 1."""
    total = math.fsum(raw)
    if not (math.isfinite(total) and total > 0.0):
        raise ValueError(f"cannot normalize vector with sum {total!r}")
    return [p / total for p in raw]


def normalize_power(
    probs: Sequence[float], *, tol: float = 1e-10, max_iter: int = 200
) -> list[float]:
    """Find ``k > 0`` with ``sum(p_i ** k) == 1`` and return ``p_i ** k``.

    ``sum(p**k)`` is strictly decreasing in ``k`` for ``p in (0, 1)`` and runs
    from ``n`` (k -> 0) down to 0 (k -> inf), so a unique root always exists —
    for over-, exactly-, and under-dispersed vectors alike. The result is
    proportionally renormalized so it sums to 1 at float precision rather than
    merely within the solver tolerance.
    """
    checked = validate_probability_vector(probs)

    def excess(k: float) -> float:
        return math.fsum(p**k for p in checked) - 1.0

    lo = hi = 1.0
    for _ in range(max_iter):
        if excess(lo) > 0.0:
            break
        lo /= 2.0
    else:
        raise RuntimeError("power normalization failed to bracket the exponent from below")
    for _ in range(max_iter):
        if excess(hi) < 0.0:
            break
        hi *= 2.0
    else:
        raise RuntimeError("power normalization failed to bracket the exponent from above")

    k = float(brentq(excess, lo, hi, xtol=tol, maxiter=max_iter))
    return normalize_proportional([p**k for p in checked])


def normalize_exclusive_family(
    mids: Sequence[float],
    method: NormalizeMethod = NormalizeMethod.PROPORTIONAL,
    *,
    tol: float = 1e-10,
    max_iter: int = 200,
) -> list[float]:
    """Renormalize mids of a mutually exclusive Kalshi market family to sum to 1.

    Input is the per-market mid-implied probabilities of markets that are
    jointly exhaustive and mutually exclusive; the deviation of their sum from
    1 is microstructure noise, not margin. Default is proportional — there is
    no favorite–longshot story to correct for in Kalshi mids.
    """
    checked = validate_probability_vector(mids)
    if method == NormalizeMethod.PROPORTIONAL:
        return normalize_proportional(checked)
    if method == NormalizeMethod.POWER:
        return normalize_power(checked, tol=tol, max_iter=max_iter)
    raise ValueError(f"unknown normalization method: {method!r}")
