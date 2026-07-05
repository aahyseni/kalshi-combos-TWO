"""Leg probability estimation (top-down).

Primary source: Kalshi's own leg orderbooks (microprice, uncertainty widened
by spread and thinness). External providers plug in as ``OddsSource``
implementations under ``pricing/sources/`` — that is the ONLY place devig may
run (CLAUDE.md decision #8) — and are blended with configurable weights. A
future bottom-up fundamental model is just another OddsSource with a weight.

Disagreement discipline: if sources disagree beyond a threshold the blend
returns None — the caller widens or no-quotes, never averages away a conflict
it can't explain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from combomaker.core.money import CC_PER_DOLLAR
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.feed import OrderbookFeed


@dataclass(frozen=True, slots=True)
class LegBelief:
    """A marginal probability with honest uncertainty (both in prob space)."""

    p: float
    uncertainty: float
    source: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.p <= 1.0:
            raise ValueError(f"p out of range: {self.p}")
        if self.uncertainty < 0.0:
            raise ValueError(f"negative uncertainty: {self.uncertainty}")


class OddsSource(Protocol):
    """Pluggable marginal-probability provider (always for the YES side)."""

    @property
    def name(self) -> str: ...

    def marginal(self, market_ticker: str) -> LegBelief | None: ...


class KalshiBookSource:
    """Microprice from the live leg book; uncertainty from spread + depth."""

    def __init__(
        self,
        feed: OrderbookFeed,
        *,
        thin_depth_contracts: float = 10.0,
        thin_penalty: float = 0.02,
    ) -> None:
        self._feed = feed
        self._thin_depth_centi = int(thin_depth_contracts * 100)
        self._thin_penalty = thin_penalty

    @property
    def name(self) -> str:
        return "kalshi_book"

    def marginal(self, market_ticker: str) -> LegBelief | None:
        try:
            book = self._feed.book(market_ticker)
        except KeyError:
            return None
        if not book.valid:
            return None
        top = book.top()
        micro = top.microprice()
        if micro is None or top.spread_cc is None or top.spread_cc < 0:
            # Crossed derived book (yes bid above $1 − no bid): either a
            # transient mirror state or something is deeply wrong. Either way
            # it is not a price — decline, don't crash the hot path.
            return None
        half_spread_prob = top.spread_cc / 2 / CC_PER_DOLLAR
        thin = (top.yes_bid_qty or CentiContracts(0)) < self._thin_depth_centi or (
            top.no_bid_qty or CentiContracts(0)
        ) < self._thin_depth_centi
        uncertainty = half_spread_prob + (self._thin_penalty if thin else 0.0)
        return LegBelief(p=micro, uncertainty=uncertainty, source=self.name)


def blend_beliefs(
    weighted: list[tuple[LegBelief, float]], *, max_disagreement: float
) -> LegBelief | None:
    """Weighted blend; None when sources disagree beyond ``max_disagreement``.

    Blended uncertainty = weighted mean uncertainty + the spread between
    sources — disagreement below the veto threshold still costs width.
    """
    if not weighted:
        return None
    total_weight = sum(w for _, w in weighted)
    if total_weight <= 0:
        return None
    ps = [b.p for b, _ in weighted]
    spread = max(ps) - min(ps)
    if len(weighted) > 1 and spread > max_disagreement:
        return None
    p = sum(b.p * w for b, w in weighted) / total_weight
    base_unc = sum(b.uncertainty * w for b, w in weighted) / total_weight
    return LegBelief(
        p=p,
        uncertainty=base_unc + spread,
        source="+".join(sorted({b.source for b, _ in weighted})),
    )
