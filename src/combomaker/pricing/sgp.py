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
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from combomaker.pricing.copula import is_psd, nearest_psd
from combomaker.pricing.legtypes import LegType, classify_leg, pair_key
from combomaker.rfq.models import RfqLeg


@dataclass(frozen=True, slots=True)
class SgpParams:
    pair_rho: dict[str, float]        # "btts|total" -> signed YES-YES rho
    default_rho: float                # untyped same-event pairs (legacy flat prior)
    cross_event_rho: float
    typed_uncertainty: float          # rho band half-width for typed pairs
    untyped_uncertainty: float        # wider band when we didn't understand the pair


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


def build_sgp_correlation(
    legs: Sequence[RfqLeg],
    same_event_groups: Sequence[Sequence[int]],
    params: SgpParams,
) -> SgpCorrelation:
    """Pairwise YES–YES correlation matrices for the whole combo.

    Cross-event pairs get ``cross_event_rho``; same-event pairs get the typed
    prior (or the flat default when either leg types UNKNOWN). Each matrix in
    the (low, point, high) triplet is independently repaired to PSD.
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
            if types[i] is LegType.UNKNOWN or types[j] is LegType.UNKNOWN:
                rho, band = params.default_rho, params.untyped_uncertainty
                untyped += 1
                notes.append(f"untyped pair {key}: flat prior {rho}")
            elif key in params.pair_rho:
                rho, band = params.pair_rho[key], params.typed_uncertainty
                typed += 1
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
