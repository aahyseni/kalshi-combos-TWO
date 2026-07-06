"""Same-game-parlay correlation structure: typed-pair matrices for the copula.

Replaces the flat same-event ρ with a SIGNED per-pair prior keyed by the two
legs' structural types (YES–YES orientation; the copula sign-flips NO-side
legs downstream). Every prior carries its own uncertainty band; untyped pairs
fall back to the flat prior with a WIDER band. Calibration from co-settlement
data updates the config table — never this code.

The output is three PSD correlation matrices (low / point / high) so the
joint can be re-priced across the band and the spread priced into width.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from combomaker.pricing.copula import is_psd, nearest_psd
from combomaker.pricing.legtypes import LegType, classify_leg, classify_sport, pair_key
from combomaker.rfq.models import RfqLeg


@dataclass(frozen=True, slots=True)
class SgpParams:
    pair_rho: dict[str, float]        # "btts|total" -> signed YES-YES rho
    default_rho: float                # untyped same-event pairs (legacy flat prior)
    cross_event_rho: float
    typed_uncertainty: float          # rho band half-width for typed pairs
    untyped_uncertainty: float        # wider band when we didn't understand the pair
    # Per-pair band overrides (calibrated pairs earn tighter bands).
    pair_uncertainty: dict[str, float] = field(default_factory=dict)
    # Sport-specific pair tables ("nba" -> {"moneyline|total": ...}); the same
    # pair correlates differently per sport. Falls back to pair_rho.
    pair_rho_by_sport: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SgpCorrelation:
    corr: NDArray[np.float64]
    corr_low: NDArray[np.float64]
    corr_high: NDArray[np.float64]
    typed_pairs: int
    untyped_pairs: int
    notes: tuple[str, ...]


def _clamp(rho: float) -> float:
    return max(-0.95, min(0.95, rho))


# Orientation blend zone for moneyline-involving pairs: below DOG_MAX the ML
# leg's YES team is priced a clear underdog, above FAV_MIN a clear favorite;
# in between the two priors are linearly blended so fair value has no cliff
# as a leg mid crosses 50c.
_ORIENT_DOG_MAX = 0.45
_ORIENT_FAV_MIN = 0.55


@dataclass(frozen=True, slots=True)
class _PairPrior:
    rho: float
    band: float
    source: str


def _lookup_pair(key: str, sport: str, params: SgpParams) -> _PairPrior | None:
    sport_table = params.pair_rho_by_sport.get(sport, {})
    if key in sport_table:
        band = params.pair_uncertainty.get(f"{sport}:{key}", params.typed_uncertainty)
        return _PairPrior(sport_table[key], band, f"{sport}:{key}")
    if key in params.pair_rho:
        band = params.pair_uncertainty.get(key, params.typed_uncertainty)
        return _PairPrior(params.pair_rho[key], band, key)
    return None


def _oriented_prior(
    key: str, sport: str, params: SgpParams, ml_marginal: float
) -> _PairPrior | None:
    """Favorite/dog-conditional prior for a pair containing one MONEYLINE leg.

    Some pair correlations flip with which side of the moneyline the YES team
    sits on (btts|moneyline: winners keep clean sheets — but only favorites;
    a dog can only win by scoring). Config expresses this as ``key:fav`` /
    ``key:dog`` entries; orientation comes from the ML leg's YES-side
    marginal, blended across the coin-flip zone.
    """
    fav = _lookup_pair(f"{key}:fav", sport, params)
    dog = _lookup_pair(f"{key}:dog", sport, params)
    if fav is None and dog is None:
        return None
    base = _lookup_pair(key, sport, params)
    fav = fav or base
    dog = dog or base
    if fav is None or dog is None:
        return None  # half-specified orientation: fall back to plain lookup
    w = min(1.0, max(0.0, (ml_marginal - _ORIENT_DOG_MAX) / (_ORIENT_FAV_MIN - _ORIENT_DOG_MAX)))
    return _PairPrior(
        rho=dog.rho + w * (fav.rho - dog.rho),
        band=max(fav.band, dog.band),
        source=f"{fav.source if w >= 0.5 else dog.source} (ml_p={ml_marginal:.2f} w={w:.2f})",
    )


def build_sgp_correlation(
    legs: Sequence[RfqLeg],
    same_event_groups: Sequence[Sequence[int]],
    params: SgpParams,
    marginals: Sequence[float] | None = None,
) -> SgpCorrelation:
    """Pairwise YES–YES correlation matrices for the whole combo.

    Cross-event pairs get ``cross_event_rho``; same-event pairs get the typed
    prior (or the flat default when either leg types UNKNOWN). Each matrix in
    the (low, point, high) triplet is independently repaired to PSD.

    ``marginals`` (YES-side probs, leg order) enables orientation-aware
    priors for moneyline pairs; without them plain entries apply.
    """
    n = len(legs)
    types = [classify_leg(leg.market_ticker) for leg in legs]
    in_group: dict[int, int] = {}
    for group_index, group in enumerate(same_event_groups):
        for leg_index in group:
            in_group[leg_index] = group_index

    point = np.full((n, n), params.cross_event_rho, dtype=np.float64)
    low = point.copy()
    high = point.copy()
    np.fill_diagonal(point, 1.0)
    np.fill_diagonal(low, 1.0)
    np.fill_diagonal(high, 1.0)

    typed = untyped = 0
    notes: list[str] = []
    for i in range(n):
        for j in range(i + 1, n):
            same_event = (
                i in in_group and j in in_group and in_group[i] == in_group[j]
            )
            if not same_event:
                continue
            key = pair_key(types[i], types[j])
            sport = str(classify_sport(legs[i].market_ticker))
            prior: _PairPrior | None = None
            if types[i] is LegType.UNKNOWN or types[j] is LegType.UNKNOWN:
                rho, band = params.default_rho, params.untyped_uncertainty
                untyped += 1
                notes.append(f"untyped pair {key}: flat prior {rho}")
            else:
                one_moneyline = (types[i] is LegType.MONEYLINE) != (
                    types[j] is LegType.MONEYLINE
                )
                if one_moneyline and marginals is not None:
                    ml_index = i if types[i] is LegType.MONEYLINE else j
                    prior = _oriented_prior(key, sport, params, marginals[ml_index])
                prior = prior or _lookup_pair(key, sport, params)
                if prior is not None:
                    rho, band = prior.rho, prior.band
                    typed += 1
                    if prior.source != key:  # plain global hits stay silent
                        notes.append(f"pair {prior.source}={rho:+.3f}")
                else:
                    rho, band = params.default_rho, params.untyped_uncertainty
                    untyped += 1
                    notes.append(f"no prior for pair {key}: flat prior {rho}")
            point[i, j] = point[j, i] = _clamp(rho)
            low[i, j] = low[j, i] = _clamp(rho - band)
            high[i, j] = high[j, i] = _clamp(rho + band)

    def repaired(m: NDArray[np.float64]) -> NDArray[np.float64]:
        return m if is_psd(m) else nearest_psd(m)

    return SgpCorrelation(
        corr=repaired(point),
        corr_low=repaired(low),
        corr_high=repaired(high),
        typed_pairs=typed,
        untyped_pairs=untyped,
        notes=tuple(notes),
    )
