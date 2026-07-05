"""Devig (margin removal) for raw implied probabilities from EXTERNAL odds only.

SCOPING RULE (CLAUDE.md decision #8): this module may be imported only from
external ``OddsSource`` adapters (``combomaker.pricing.sources.*``) — enforced
by ``tests/test_architecture.py``. Kalshi-sourced leg probabilities must never
pass through devig: Kalshi binaries are vig-free by construction (yes + no =
$1) and fees are handled separately in the fee module. For renormalizing a
mutually exclusive family of Kalshi markets whose mids don't sum to 100%, use
``combomaker.pricing.normalize.normalize_exclusive_family`` instead.

A full book of bookmaker implied probabilities sums to more than 1; the excess
is the overround (vig). These helpers strip the margin and return *fair*
probabilities summing to 1, for use by pluggable external odds sources.

Methods
-------
- ``multiplicative`` — proportional normalization ``p_i / sum``. Spreads the
  margin evenly in relative terms; ignores the favorite–longshot bias.
- ``power`` — solves ``sum(p_i ** k) == 1`` for ``k > 0`` and returns
  ``p_i ** k``. On an overround book ``k > 1``, which shades longshots harder
  than favorites.
- ``shin`` — Shin's (1992/1993) insider-trading model: the margin is
  attributed to a fraction ``z`` of insider volume, and with booksum
  ``B = sum(implied)`` the fair probabilities are
  ``p_i = (sqrt(z**2 + 4*(1-z)*pi_i**2 / B) - z) / (2*(1-z))``,
  with ``z`` in ``[0, 1)`` chosen so they sum to 1. Also longshot-shading.

Everything here lives in probability space (plain floats in ``(0, 1)``); no
money enters this module.

Degenerate books: if the input has no overround (``sum(implied) <= 1``, i.e. a
fair or arbitrageable/underround book) every method still returns a normalized
result — ``power`` simply solves for ``k <= 1``, while ``shin`` (whose insider
fraction is only defined for an overround book) falls back to proportional
normalization. The iterative methods renormalize their solved probabilities
proportionally at the end, so outputs sum to 1 to float precision rather than
merely within the solver tolerance.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum

from scipy.optimize import brentq

from combomaker.pricing.normalize import (
    normalize_power,
    normalize_proportional,
    validate_probability_vector,
)


class DevigMethod(StrEnum):
    """Margin-removal model choices."""

    MULTIPLICATIVE = "multiplicative"
    POWER = "power"
    SHIN = "shin"


def implied_from_decimal_odds(odds: Sequence[float]) -> list[float]:
    """Decimal odds -> raw implied probabilities ``1/o``. Every ``o`` must be > 1."""
    out: list[float] = []
    for o in odds:
        f = float(o)
        if not (math.isfinite(f) and f > 1.0):
            raise ValueError(f"decimal odds must be finite and > 1.0, got {o!r}")
        out.append(1.0 / f)
    return out


def devig_multiplicative(implied: Sequence[float]) -> list[float]:
    """Proportional normalization: ``p_i / sum(p)``."""
    return normalize_proportional(validate_probability_vector(implied))


def devig_power(
    implied: Sequence[float], *, tol: float = 1e-10, max_iter: int = 200
) -> list[float]:
    """Power devig: find ``k > 0`` with ``sum(p_i ** k) == 1``; return ``p_i ** k``.

    Same solver as margin-free power normalization — the devig framing (margin
    attributed with favorite–longshot shading) is what's specific here.
    """
    return normalize_power(validate_probability_vector(implied), tol=tol, max_iter=max_iter)


def devig_shin(
    implied: Sequence[float], *, tol: float = 1e-10, max_iter: int = 200
) -> list[float]:
    """Shin devig: insider-trading model, solving for the insider fraction ``z``.

    Uses the rationalized, numerically stable form of the Shin fair price
    ``p_i = 2*pi_i**2 / (B * (z + sqrt(z**2 + 4*(1-z)*pi_i**2 / B)))`` (equal to
    the textbook expression for ``z < 1`` and finite at ``z == 1``). The root of
    ``sum(p_i(z)) == 1`` is bracketed by ``sum(p(0)) = sqrt(B) > 1`` and
    ``sum(p(1)) = sum(pi**2)/B < 1``, so it always exists for an overround book.
    A book with no overround (``B <= 1``) has no insider fraction; it degrades
    to plain proportional normalization.
    """
    probs = validate_probability_vector(implied)
    booksum = math.fsum(probs)
    if booksum <= 1.0:
        return normalize_proportional(probs)

    def fair(z: float) -> list[float]:
        return [
            2.0 * p * p / (booksum * (z + math.sqrt(z * z + 4.0 * (1.0 - z) * p * p / booksum)))
            for p in probs
        ]

    def excess(z: float) -> float:
        return math.fsum(fair(z)) - 1.0

    z = float(brentq(excess, 0.0, 1.0, xtol=tol, maxiter=max_iter))
    return normalize_proportional(fair(z))


_TUNING_KEYS = frozenset({"tol", "max_iter"})


def devig(
    implied: Sequence[float],
    method: DevigMethod = DevigMethod.POWER,
    **kwargs: float,
) -> list[float]:
    """Dispatch to the requested devig method.

    ``kwargs`` may carry solver tuning (``tol``, ``max_iter``) for the iterative
    methods; multiplicative accepts and ignores them. Unknown keys raise
    ``TypeError``.
    """
    unknown = set(kwargs) - _TUNING_KEYS
    if unknown:
        raise TypeError(f"unknown devig kwargs: {sorted(unknown)}")
    if method == DevigMethod.MULTIPLICATIVE:
        return devig_multiplicative(implied)
    tol = float(kwargs.get("tol", 1e-10))
    max_iter = int(kwargs.get("max_iter", 200))
    if method == DevigMethod.POWER:
        return devig_power(implied, tol=tol, max_iter=max_iter)
    if method == DevigMethod.SHIN:
        return devig_shin(implied, tol=tol, max_iter=max_iter)
    raise ValueError(f"unknown devig method: {method!r}")
