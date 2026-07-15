"""Vectorized Monte Carlo engine over the maker's whole book of combo positions.

Joint leg outcomes are sampled with a Gaussian copula: latent normals correlated
by ``corr`` are mapped to uniforms U = Phi(Z), and each leg's settlement value is
read off the inverse CDF of its (possibly scalar) settlement distribution, so
rank correlation between legs carries over regardless of marginal shape.

Legs settle to values in [0, 1] — binary {0, 1} by default. A combo YES contract
pays (product of leg settlement values, capped at 1.0) * $1; NO pays $1 minus
that. Money at the interface is integer centi-cents (1 cc = $0.0001, $1 =
10_000 cc); statistics come back as float cc. Everything is deterministic under
``seed`` via ``np.random.default_rng``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.special import ndtr

from combomaker.core.money import CC_PER_DOLLAR

VAR_LEVELS: tuple[float, ...] = (0.95, 0.99)

_SETTLEMENT_PROB_TOL = 1e-9
_CHOLESKY_JITTER = 1e-12


@dataclass(frozen=True, slots=True)
class LegModel:
    """Marginal model for one leg.

    ``p`` is P(YES); it sets the copula threshold for the default binary payoff
    (``settlement is None`` means the distribution [(0.0, 1-p), (1.0, p)]).

    ``settlement``, when given, REPLACES the binary payoff with a discrete
    distribution of settlement values: ``((value, prob), ...)`` with values in
    [0, 1] and probs summing to 1. The leg's value is then drawn via the inverse
    CDF of the same copula uniform (values ascending: higher uniform -> higher
    value), so the shared uniform preserves rank correlation with other legs;
    ``p`` plays no further role. A binary distribution [(0, 1-p), (1, p)]
    reproduces ``settlement=None`` exactly.
    """

    p: float
    settlement: tuple[tuple[float, float], ...] | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.p <= 1.0:
            raise ValueError(f"p out of [0, 1]: {self.p}")
        if self.settlement is None:
            return
        if not self.settlement:
            raise ValueError("settlement distribution must be non-empty")
        for value, prob in self.settlement:
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"settlement value out of [0, 1]: {value}")
            if prob < 0.0:
                raise ValueError(f"negative settlement probability: {prob}")
        total = math.fsum(prob for _, prob in self.settlement)
        if abs(total - 1.0) > _SETTLEMENT_PROB_TOL:
            raise ValueError(f"settlement probabilities sum to {total!r}, expected 1")
        # Normalize to ascending value order so the inverse CDF maps higher
        # copula uniforms to higher settlement values.
        object.__setattr__(self, "settlement", tuple(sorted(self.settlement)))


@dataclass(frozen=True, slots=True)
class ComboPosition:
    """One combo position; ``leg_indices`` index into the leg universe.

    ``price_cc`` is the entry price paid per contract (0..10_000 cc);
    ``fee_cc`` is the TOTAL fee for the position, subtracted from P&L once.

    ``contracts`` is the (possibly FRACTIONAL) contract quantity — Kalshi allows
    fractional fills, so the live book carries centi-contracts (1.00 contract =
    100 centi-contracts) which ``book_model`` converts EXACTLY to fractional
    contracts by dividing by 100 (P0-6). There is no one-contract floor: a 0.40
    contract position is scored as 0.40, not rounded up to 1. Per-scenario P&L is
    ``per_contract_cc · contracts`` in float cc (probability/float space is fine
    for money at the simulator interface, hard rule 5).

    ``leg_sides`` (optional) selects, PER LEG, whether the combo needs that leg's
    YES value or its NO value (``1 − value``) inside the payout product. The
    default (``None``) means every leg contributes its YES value — the historical
    behaviour, byte-for-byte. This is the copula's latent-sign-flip expressed on
    the sampled value instead of the latent Z (``1 − 1[Z≤t] = 1[−Z ≤ −t]``);
    algebraically identical for a binary leg and correct for a graded settlement
    leg too. It lets a NO-selected leg keep its within-game correlation with the
    rest of its game (the M1 fix) instead of being modeled as an independent
    complement pseudo-leg. NOTE ``side`` is the whole POSITION's side (YES/NO
    contract we hold); ``leg_sides`` is the per-leg selection INSIDE the combo —
    the two are orthogonal.
    """

    leg_indices: tuple[int, ...]
    side: Literal["yes", "no"]
    contracts: float
    price_cc: int
    fee_cc: int = 0
    leg_sides: tuple[Literal["yes", "no"], ...] | None = None

    def __post_init__(self) -> None:
        if not self.leg_indices:
            raise ValueError("combo position needs at least one leg")
        if any(i < 0 for i in self.leg_indices):
            raise ValueError(f"negative leg index in {self.leg_indices}")
        if self.side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no': {self.side!r}")
        if self.contracts <= 0:
            raise ValueError(f"contracts must be > 0: {self.contracts}")
        if not 0 <= self.price_cc <= CC_PER_DOLLAR:
            raise ValueError(f"price_cc out of [0, {CC_PER_DOLLAR}]: {self.price_cc}")
        if self.fee_cc < 0:
            raise ValueError(f"fee_cc must be >= 0: {self.fee_cc}")
        if self.leg_sides is not None:
            if len(self.leg_sides) != len(self.leg_indices):
                raise ValueError(
                    f"leg_sides length {len(self.leg_sides)} != leg_indices length "
                    f"{len(self.leg_indices)}"
                )
            for s in self.leg_sides:
                if s not in ("yes", "no"):
                    raise ValueError(f"leg side must be 'yes' or 'no': {s!r}")


@dataclass(frozen=True, slots=True, eq=False)
class PortfolioStats:
    """Monte Carlo P&L statistics for a book; all money is float cc.

    ``var_cc``/``es_cc`` map confidence level -> positive loss magnitude
    (VaR_q = max(0, -quantile(pnl, 1-q)); ES is the mean loss at or beyond that
    quantile, falling back to VaR on an empty tail). ``p_loss_worse_than`` maps
    a loss threshold in cc -> P(pnl < -threshold). ``pnl_samples`` is the raw
    per-scenario total book P&L in cc.
    """

    ev_cc: float
    std_cc: float
    p_profit: float
    var_cc: dict[float, float]
    es_cc: dict[float, float]
    p_loss_worse_than: dict[float, float]
    pnl_samples: NDArray[np.float64]


def _cholesky_with_jitter(corr: NDArray[np.float64]) -> NDArray[np.float64]:
    """Cholesky factor of ``corr``; on failure add 1e-12 * I and retry once."""
    try:
        return np.linalg.cholesky(corr)
    except np.linalg.LinAlgError:
        jitter = _CHOLESKY_JITTER * np.eye(corr.shape[0], dtype=np.float64)
        return np.linalg.cholesky(corr + jitter)


def _leg_table(leg: LegModel) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Ascending settlement values and cumulative probs for inverse-CDF lookup.

    The last cumulative bin is set to +inf so float round-off in the total (and
    a copula uniform of exactly 1.0) still lands on the highest value.
    """
    if leg.settlement is None:
        values = np.array([0.0, 1.0], dtype=np.float64)
        probs = np.array([1.0 - leg.p, leg.p], dtype=np.float64)
    else:
        values = np.array([v for v, _ in leg.settlement], dtype=np.float64)
        probs = np.array([q for _, q in leg.settlement], dtype=np.float64)
    cum = np.cumsum(probs)
    cum[-1] = np.inf
    return values, cum


def sample_leg_values(
    legs: Sequence[LegModel],
    corr: NDArray[np.float64],
    n: int,
    rng: np.random.Generator,
) -> NDArray[np.float64]:
    """Sample an ``(n, len(legs))`` float64 matrix of leg settlement values in [0, 1]."""
    n_legs = len(legs)
    if n <= 0:
        raise ValueError(f"n must be > 0: {n}")
    corr_arr = np.asarray(corr, dtype=np.float64)
    if corr_arr.shape != (n_legs, n_legs):
        raise ValueError(f"corr shape {corr_arr.shape} != ({n_legs}, {n_legs})")
    chol = _cholesky_with_jitter(corr_arr)
    z = rng.standard_normal((n, n_legs)) @ chol.T
    u = np.asarray(ndtr(z), dtype=np.float64)
    out = np.empty((n, n_legs), dtype=np.float64)
    for j, leg in enumerate(legs):
        values, cum = _leg_table(leg)
        out[:, j] = values[np.searchsorted(cum, u[:, j], side="right")]
    return out


def _position_pnl(
    values: NDArray[np.float64], position: ComboPosition
) -> NDArray[np.float64]:
    """Per-scenario total P&L of one position in float cc; fee subtracted once."""
    cols = values[:, list(position.leg_indices)]
    if position.leg_sides is not None:
        # Per-leg selected-side: a NO-selected leg contributes (1 − value) to the
        # payout product, keeping its correlation with the rest of its game (the
        # sampled `cols` already carry the copula dependence). YES-selected legs
        # are untouched, so an all-"yes" leg_sides reproduces the default exactly.
        flip = np.array(
            [s == "no" for s in position.leg_sides], dtype=bool
        )
        if flip.any():
            cols = np.where(flip[np.newaxis, :], 1.0 - cols, cols)
    payout_cc = np.minimum(np.prod(cols, axis=1), 1.0) * float(CC_PER_DOLLAR)
    if position.side == "yes":
        per_contract = payout_cc - float(position.price_cc)
    else:
        per_contract = (float(CC_PER_DOLLAR) - payout_cc) - float(position.price_cc)
    result: NDArray[np.float64] = per_contract * position.contracts - position.fee_cc
    return result


def _book_pnl(
    values: NDArray[np.float64], positions: Sequence[ComboPosition]
) -> NDArray[np.float64]:
    """Per-scenario total P&L of the whole book in float cc (zeros for an empty book)."""
    pnl = np.zeros(values.shape[0], dtype=np.float64)
    for position in positions:
        pnl += _position_pnl(values, position)
    return pnl


# Public aliases so the book-risk / tail-attribution layer (sim/book_risk.py) can
# reuse the EXACT per-position and whole-book P&L math on the SAME sampled value
# matrix — no reimplementation of the payout/fee/side arithmetic (hard rule 8).
def position_pnl(
    values: NDArray[np.float64], position: ComboPosition
) -> NDArray[np.float64]:
    """Per-scenario P&L of one position (float cc); public alias of the engine's
    payout math for tail attribution (restrict ``values`` to the tail rows to get
    that position's contribution to the tail loss)."""
    return _position_pnl(values, position)


def book_pnl(
    values: NDArray[np.float64], positions: Sequence[ComboPosition]
) -> NDArray[np.float64]:
    """Per-scenario P&L of the whole book (float cc); public alias."""
    return _book_pnl(values, positions)


def _stats_from_pnl(
    pnl: NDArray[np.float64], loss_thresholds_cc: Sequence[int]
) -> PortfolioStats:
    """Summarize a per-scenario P&L vector into PortfolioStats."""
    ev = float(pnl.mean())
    std = float(pnl.std(ddof=1)) if pnl.size > 1 else 0.0
    p_profit = float(np.mean(pnl > 0.0))
    var_cc: dict[float, float] = {}
    es_cc: dict[float, float] = {}
    for level in VAR_LEVELS:
        cut = float(np.quantile(pnl, 1.0 - level))
        var = max(0.0, -cut)
        tail = pnl[pnl <= cut]
        es = max(0.0, -float(tail.mean())) if tail.size > 0 else var
        var_cc[level] = var
        es_cc[level] = es
    p_loss_worse_than = {
        float(t): float(np.mean(pnl < -float(t))) for t in loss_thresholds_cc
    }
    return PortfolioStats(
        ev_cc=ev,
        std_cc=std,
        p_profit=p_profit,
        var_cc=var_cc,
        es_cc=es_cc,
        p_loss_worse_than=p_loss_worse_than,
        pnl_samples=pnl,
    )


def simulate(
    legs: Sequence[LegModel],
    corr: NDArray[np.float64],
    positions: Sequence[ComboPosition],
    *,
    n_samples: int = 100_000,
    seed: int = 0,
    loss_thresholds_cc: Sequence[int] = (),
) -> PortfolioStats:
    """Monte Carlo P&L distribution of the whole book; deterministic under ``seed``."""
    rng = np.random.default_rng(seed)
    values = sample_leg_values(legs, corr, n_samples, rng)
    return _stats_from_pnl(_book_pnl(values, positions), loss_thresholds_cc)


def marginal_impact(
    legs: Sequence[LegModel],
    corr: NDArray[np.float64],
    positions: Sequence[ComboPosition],
    candidate: ComboPosition,
    *,
    n_samples: int = 100_000,
    seed: int = 0,
) -> tuple[PortfolioStats, PortfolioStats]:
    """(book without candidate, book with candidate) on common random numbers.

    Both stats are computed from the same sampled leg values, so the with-minus-
    without difference is a low-variance estimate of the candidate's impact.
    """
    rng = np.random.default_rng(seed)
    values = sample_leg_values(legs, corr, n_samples, rng)
    pnl_without = _book_pnl(values, positions)
    pnl_with = pnl_without + _position_pnl(values, candidate)
    return _stats_from_pnl(pnl_without, ()), _stats_from_pnl(pnl_with, ())


def leg_deltas(
    legs: Sequence[LegModel],
    corr: NDArray[np.float64],
    position: ComboPosition,
    *,
    n_samples: int = 100_000,
    seed: int = 0,
) -> NDArray[np.float64]:
    """Position delta to each leg in the universe, in cc per 1.0 of leg value.

    delta_i = E[pnl | leg i value forced to 1] - E[pnl | forced to 0], estimated
    on the same sampled scenarios (conditional resampling). Legs the position
    does not reference get exactly 0 by construction.
    """
    rng = np.random.default_rng(seed)
    values = sample_leg_values(legs, corr, n_samples, rng)
    deltas = np.zeros(len(legs), dtype=np.float64)
    for i in range(len(legs)):
        forced = values.copy()
        forced[:, i] = 1.0
        pnl_hi = _position_pnl(forced, position)
        forced[:, i] = 0.0
        pnl_lo = _position_pnl(forced, position)
        deltas[i] = float(np.mean(pnl_hi - pnl_lo))
    return deltas
