"""Observe-mode would-quote stub: independence pricing, logging only.

This NEVER sends anything. It exists so observe mode records what we *would*
have quoted, giving Phase 6 a hypothetical-quote dataset to score.

Deliberately convention-light: it prices "P(all selected legs settle on their
selected side)" from leg microprices under independence, and reports a fair ±
half-width in YES-price space. Which wire field that maps to (yes_bid vs
no_bid direction semantics) is Phase 2.5 conventions territory and is NOT
encoded here. Real quote construction (grid snapping, fees, skew, adders)
is Phase 3 and replaces this wholesale.
"""

from __future__ import annotations

from dataclasses import dataclass

from combomaker.core.money import CentiCents, cc_from_prob
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.rfq.models import Rfq


@dataclass(frozen=True, slots=True)
class WouldQuote:
    fair_prob: float                  # P(combo settles YES) under independence
    fair_cc: CentiCents
    bid_below_cc: CentiCents          # fair − half width (YES-price space)
    bid_above_complement_cc: CentiCents  # $1 − (fair + half width): the other side's bid
    width_cc: int
    leg_probs: tuple[float, ...]      # per-leg P(selected side), rfq.legs order


def independence_would_quote(
    rfq: Rfq, feed: OrderbookFeed, *, width_cc: int
) -> WouldQuote | None:
    """None when any leg is unpriceable — missing data is a no-quote, never a guess."""
    leg_probs: list[float] = []
    for leg in rfq.legs:
        if not leg.side_known:
            return None
        try:
            book = feed.book(leg.market_ticker)
        except KeyError:
            return None
        if not book.valid:
            return None
        micro = book.top().microprice()
        if micro is None:
            return None
        p_yes = micro
        leg_probs.append(p_yes if leg.side == "yes" else 1.0 - p_yes)

    fair = 1.0
    for p in leg_probs:
        fair *= p

    half = width_cc // 2
    fair_cc = cc_from_prob(fair)
    below = max(0, fair_cc - half)
    above = min(10_000, fair_cc + half)
    return WouldQuote(
        fair_prob=fair,
        fair_cc=fair_cc,
        bid_below_cc=CentiCents(below),
        bid_above_complement_cc=CentiCents(10_000 - above),
        width_cc=width_cc,
        leg_probs=tuple(leg_probs),
    )
