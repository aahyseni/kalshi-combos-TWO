"""Structural leg typing from Kalshi ticker patterns.

Same-game correlations are STRUCTURED and SIGNED — "BTTS × Over" correlates
very differently than "home ML × away ML". The series prefix of a sports
ticker encodes the market structure (KXWCGOAL…, KXMLBGAME…, KXWCTOTAL…), so
we can type legs without extra metadata. UNKNOWN typing never blocks a quote
by itself — it falls back to the flat same-event prior with WIDER uncertainty
(the honest price of not understanding the structure).
"""

from __future__ import annotations

import re
from enum import StrEnum


class LegType(StrEnum):
    MONEYLINE = "moneyline"        # game/match winner (…GAME, …FIGHT, …MATCH)
    TOTAL = "total"                # over/under GAME totals (both teams combined)
    # A single TEAM's total (series KX<SPORT>TEAMTOTAL-…-<TEAM>N). "TEAMTOTAL"
    # contains "TOTAL", so without a dedicated keyword (matched first) these
    # mis-type as a game TOTAL and would price/correlate on the game-total grid.
    # CLASSIFY-ONLY: no structural pricing yet — team-total pairs fall through to
    # the copula default (intended), they just must not masquerade as game TOTAL.
    TEAM_TOTAL = "team_total"
    BTTS = "btts"                  # both teams to score
    PLAYER_GOAL = "player_goal"    # player scoring props (…GOAL-…PLAYER…)
    CORNERS = "corners"            # TOTAL corners (series KXWCCORNERS-…-N)
    CORNERS_TEAM = "corners_team"  # a TEAM's corners (series KXWCTCORNERS-…-<TEAM>N)
    ADVANCE = "advance"            # team advances / series outcome
    EXTRAS = "extras"              # extra innings / overtime style props
    SPREAD = "spread"
    # First-half (period) soccer families. A period market settles on a
    # DIFFERENT window (half-time, not full-time), so it must never share a
    # LegType with its full-game sibling — a 1H total reported as a full-game
    # TOTAL is a wrong-settlement-window bug (it would price on the full-game
    # scoreline grid and correlate as full-game total|total). Only the 1st
    # half is modeled today (period × full-time correlations); other periods
    # (2H, quarters) classify UNKNOWN so they never masquerade as full-game.
    FIRST_HALF_MONEYLINE = "first_half_moneyline"
    FIRST_HALF_TOTAL = "first_half_total"
    FIRST_HALF_BTTS = "first_half_btts"
    # First-half spread = 1H goal margin (series KXWC1HSPREAD-…-<TEAM><line>,
    # e.g. …-FRA2 = France leads at half by over 1.5). Measured against FT
    # spread/moneyline/total (results_soccer.md §2). The 1H×1H pairs it forms
    # ARE reachable (real same-game combos in the prod tape — the earlier
    # "blocked by Kalshi" assumption was wrong); they're DEFERRED, not blocked.
    FIRST_HALF_SPREAD = "first_half_spread"
    UNKNOWN = "unknown"


# Keyword → type, checked in order (GOAL before GAME matters: KXWCGOAL vs
# KXMLBGAME both contain overlapping substrings otherwise).
_KEYWORDS: tuple[tuple[str, LegType], ...] = (
    ("GOAL", LegType.PLAYER_GOAL),
    ("BTTS", LegType.BTTS),
    # TEAMTOTAL must precede TOTAL (it contains it). SOURCE OF TRUTH (prod RFQ
    # tape + Kalshi API): a single team's total is KX<SPORT>TEAMTOTAL-…-<TEAM>N
    # (e.g. KXNFLTEAMTOTAL-…-SEA24), distinct from the game TOTAL (…-N). Same
    # precede-the-superstring pattern as TCORNERS-before-CORNERS below.
    ("TEAMTOTAL", LegType.TEAM_TOTAL),
    ("TOTAL", LegType.TOTAL),
    # TCORNERS must precede CORNERS (it contains it). SOURCE OF TRUTH (RFQ tape
    # 2026-07-07): team corners = KXWCTCORNERS, total corners = KXWCCORNERS.
    ("TCORNERS", LegType.CORNERS_TEAM),
    ("CORNERS", LegType.CORNERS),
    ("ADVANCE", LegType.ADVANCE),
    ("EXTRAS", LegType.EXTRAS),
    ("SPREAD", LegType.SPREAD),
    ("FIGHT", LegType.MONEYLINE),
    ("GAME", LegType.MONEYLINE),
    # Tennis match winner: KX{ATP,WTA}[CHALLENGER]MATCH-…-<PLAYER>. "MATCH" is
    # not a substring of any other sport's series family (GOAL/BTTS/TOTAL/
    # CORNERS/ADVANCE/EXTRAS/SPREAD/FIGHT/GAME), so no collision with the
    # entries above; ordering among the MONEYLINE group is immaterial.
    ("MATCH", LegType.MONEYLINE),
)

# Period / derived market families (first/second half, quarters). Matched
# against the SERIES prefix only (KXWC1HTOTAL, KXWC2H, KX…FHTOTAL). These
# settle on a different window than the full game and are structurally
# unmodelable in the full-game inverter.
_PERIOD_SERIES = re.compile(r"(?:1H|2H|H1|H2|FH|SH|[1-4]Q|Q[1-4]|QTR|HALF|PERIOD)")
# First-half specifically: the only period we have measured correlations for.
_FIRST_HALF_SERIES = re.compile(r"(?:1H|H1|FH)")
# The bare 1st-half WINNER series ends in the half token with no family suffix.
# SOURCE OF TRUTH (prod RFQ tape 2026-07-07): the real series is ``KXWC1H``
# (``KXWC1H-<game>-<TEAM|TIE>``) — NOT ``KXWC1HGAME`` (which does not exist).
_FIRST_HALF_WINNER = re.compile(r"(?:1H|H1|FH)$")
# Full-game family → its first-half member. A first-half leg whose base family
# is anything else (player goal, corners, …) is left UNKNOWN: those 1H × FT
# pairs are not measured yet, so they must widen, never guess. SPREAD is now
# mapped (1H-spread × FT priors calibrated, results_soccer.md §2).
_FIRST_HALF_MAP: dict[LegType, LegType] = {
    LegType.MONEYLINE: LegType.FIRST_HALF_MONEYLINE,
    LegType.TOTAL: LegType.FIRST_HALF_TOTAL,
    LegType.BTTS: LegType.FIRST_HALF_BTTS,
    LegType.SPREAD: LegType.FIRST_HALF_SPREAD,
}


def is_period_leg(market_ticker: str) -> bool:
    """True for a period/derived market (first/second half, quarter). Gates the
    structural inverter (no half-time scoreline window) and the same-game
    regroup — matched on the SERIES prefix only."""
    series = market_ticker.split("-", 1)[0].upper()
    return _PERIOD_SERIES.search(series) is not None


def classify_leg(market_ticker: str) -> LegType:
    series = market_ticker.split("-", 1)[0].upper()
    base = LegType.UNKNOWN
    for keyword, leg_type in _KEYWORDS:
        if keyword in series:
            base = leg_type
            break
    if _PERIOD_SERIES.search(series):
        if _FIRST_HALF_SERIES.search(series):
            mapped = _FIRST_HALF_MAP.get(base)
            if mapped is not None:
                return mapped
            # A bare 1st-half winner (series ends in the half token, no family
            # keyword — the real KXWC1H moneyline) has base UNKNOWN. Anything
            # else first-half-but-unmapped (an unknown 1H family) stays UNKNOWN:
            # unmeasured 1H×FT pairs must widen, never guess.
            if base is LegType.UNKNOWN and _FIRST_HALF_WINNER.search(series):
                return LegType.FIRST_HALF_MONEYLINE
            return LegType.UNKNOWN
        return LegType.UNKNOWN  # unmodeled period (2H/quarter): never full-game
    return base


def pair_key(a: LegType, b: LegType) -> str:
    """Order-independent lookup key: pair_key(TOTAL, BTTS) == 'btts|total'."""
    return "|".join(sorted((str(a), str(b))))


class Sport(StrEnum):
    SOCCER = "soccer"
    MLB = "mlb"
    NBA = "nba"
    WNBA = "wnba"
    NFL = "nfl"
    NHL = "nhl"
    UFC = "ufc"
    TENNIS = "tennis"
    UNKNOWN = "unknown"


# Order matters: WNBA before NBA (substring), CFB-style prefixes unmapped stay
# UNKNOWN and inherit the global pair table.
_SPORT_KEYWORDS: tuple[tuple[str, Sport], ...] = (
    ("WNBA", Sport.WNBA),
    ("NBA", Sport.NBA),
    ("MLB", Sport.MLB),
    ("NFL", Sport.NFL),
    ("NHL", Sport.NHL),
    ("UFC", Sport.UFC),
    # Tennis: KX{ATP,WTA}[CHALLENGER]MATCH-…. "ATP"/"WTA" are not substrings of
    # any existing series prefix above (WNBA/NBA/MLB/NFL/NHL/UFC/WC/UCL/MLS/EPL/
    # BRASILEIRO/LALIGA/SERIEA/BUNDESLIGA), and none of those are substrings of
    # the tennis prefixes — so first-match order is safe wherever these sit.
    ("ATP", Sport.TENNIS),
    ("WTA", Sport.TENNIS),
    ("WC", Sport.SOCCER),
    ("UCL", Sport.SOCCER),
    ("MLS", Sport.SOCCER),
    ("EPL", Sport.SOCCER),
    ("BRASILEIRO", Sport.SOCCER),
    ("LALIGA", Sport.SOCCER),
    ("SERIEA", Sport.SOCCER),
    ("BUNDESLIGA", Sport.SOCCER),
)


def classify_sport(market_ticker: str) -> Sport:
    series = market_ticker.split("-", 1)[0].upper()
    for keyword, sport in _SPORT_KEYWORDS:
        if keyword in series:
            return sport
    return Sport.UNKNOWN
