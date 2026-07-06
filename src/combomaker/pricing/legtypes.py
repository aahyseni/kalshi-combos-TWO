"""Structural leg typing from Kalshi ticker patterns.

Same-game correlations are STRUCTURED and SIGNED — "BTTS × Over" correlates
very differently than "home ML × away ML". The series prefix of a sports
ticker encodes the market structure (KXWCGOAL…, KXMLBGAME…, KXWCTOTAL…), so
we can type legs without extra metadata. UNKNOWN typing never blocks a quote
by itself — it falls back to the flat same-event prior with WIDER uncertainty
(the honest price of not understanding the structure).
"""

from __future__ import annotations

from enum import StrEnum


class LegType(StrEnum):
    MONEYLINE = "moneyline"        # game/match winner (…GAME, …FIGHT)
    TOTAL = "total"                # over/under team-or-game totals
    BTTS = "btts"                  # both teams to score
    PLAYER_GOAL = "player_goal"    # player scoring props (…GOAL-…PLAYER…)
    CORNERS = "corners"
    ADVANCE = "advance"            # team advances / series outcome
    EXTRAS = "extras"              # extra innings / overtime style props
    SPREAD = "spread"
    UNKNOWN = "unknown"


# Keyword → type, checked in order (GOAL before GAME matters: KXWCGOAL vs
# KXMLBGAME both contain overlapping substrings otherwise).
_KEYWORDS: tuple[tuple[str, LegType], ...] = (
    ("GOAL", LegType.PLAYER_GOAL),
    ("BTTS", LegType.BTTS),
    ("TOTAL", LegType.TOTAL),
    ("CORNERS", LegType.CORNERS),
    ("ADVANCE", LegType.ADVANCE),
    ("EXTRAS", LegType.EXTRAS),
    ("SPREAD", LegType.SPREAD),
    ("FIGHT", LegType.MONEYLINE),
    ("GAME", LegType.MONEYLINE),
)


def classify_leg(market_ticker: str) -> LegType:
    series = market_ticker.split("-", 1)[0].upper()
    for keyword, leg_type in _KEYWORDS:
        if keyword in series:
            return leg_type
    return LegType.UNKNOWN


def pair_key(a: LegType, b: LegType) -> str:
    """Order-independent lookup key: pair_key(TOTAL, BTTS) == 'btts|total'."""
    return "|".join(sorted((str(a), str(b))))
