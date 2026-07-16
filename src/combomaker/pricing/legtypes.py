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
from collections.abc import Mapping
from enum import StrEnum

# --- PRICING ALIASES (2026-07-16, operator-directed) -------------------------
# Exact-ticker aliases applied ONLY inside the pricing-classification layer:
# classify_leg / classify_sport / is_period_leg / structural parsing / markup
# sport tagging. The exchange-facing identity (order-book subscription,
# marginal source, settlement, metadata, freshness) keeps the REAL ticker.
# Motivating case: KXMENWORLDCUP-26-AR ("Argentina wins the World Cup") is, at
# finals time, settlement-identical to winning the final incl. ET/pens — i.e.
# exactly a KXWCADVANCE leg on the final. Aliasing it to the synthetic ticker
# KXWCADVANCE-26JUL19ESPARG-ARG makes the leg STRUCTURAL: the Dixon-Coles
# engine nets it exactly against every other final leg (Messi, corners, BTTS)
# instead of the flat UNKNOWN prior, and the markup layer sees soccer instead
# of 'other' (which quoted ZERO markup). Config-driven
# (PricingConfig.leg_pricing_aliases, committed default {}), installed by
# PricingEngine.__init__ (per PROCESS: the pricing-pool worker initializer
# builds a worker engine from the same config, and BookRiskPool's initializer
# installs the mapping directly — a registry that exists only in the loop
# process would quietly split pricing from risk); only UNKNOWN-classifying
# tickers may be aliased (validated — an alias can never override a modeled
# family, and the target must BE a modeled family).
#
# A derived EVENT alias registry rides along: Kalshi market = EVENT-SUFFIX, so
# {KXMENWORLDCUP-26-AR: KXWCADVANCE-26JUL19ESPARG-ARG} implies the event alias
# {KXMENWORLDCUP-26: KXWCADVANCE-26JUL19ESPARG}. ``grouping.game_key`` resolves
# it, which is the ONE seam both the copula same-game regroup AND every risk
# aggregation (exposure/limits/skew/book-risk game plans) key on — so the
# champion leg joins the final's game block everywhere at once, with the
# pricer/risk parity property preserved by construction. Mutual-exclusion
# metadata lookups (relationships Pass 1) deliberately keep the REAL
# event_ticker, so exchange ME flags stay authoritative.
_PRICING_ALIASES: dict[str, str] = {}
_PRICING_EVENT_ALIASES: dict[str, str] = {}


def _event_of(market_ticker: str) -> str:
    """EVENT-SUFFIX -> EVENT (Kalshi market-ticker convention)."""
    return market_ticker.rsplit("-", 1)[0]


def validate_pricing_aliases(aliases: Mapping[str, str]) -> None:
    """Raise ValueError unless every alias is safe to install.

    Rules (each guards a real failure mode):
    - keys/values are hyphenated non-empty strings and key != value (an
      un-hyphenated ticker has no EVENT prefix to derive, so grouping would
      silently not follow the market alias);
    - no value is itself a key (resolution is single-step by design — a chain
      in config is an intent error, not something to half-honor);
    - a key must classify UNKNOWN on the RAW tables (an alias may never
      override a modeled family) and a value must NOT (an UNKNOWN target
      would re-launder the leg back into the flat prior, defeating the alias);
    - keys sharing a derived real event must map to ONE synthetic event
      (otherwise game grouping for that event is ill-defined).
    """
    events_seen: dict[str, str] = {}
    for key, value in aliases.items():
        if not key or not value or key == value:
            raise ValueError(f"pricing alias {key!r} -> {value!r}: empty or self-alias")
        if "-" not in key or "-" not in value:
            raise ValueError(
                f"pricing alias {key!r} -> {value!r}: both sides need an "
                "EVENT-SUFFIX shape to derive the event alias"
            )
        if value in aliases:
            raise ValueError(f"pricing alias chain: {key!r} -> {value!r} is also a key")
        if _classify_leg_raw(key) is not LegType.UNKNOWN:
            raise ValueError(
                f"pricing alias key {key!r} classifies {_classify_leg_raw(key)}, "
                "not UNKNOWN — an alias may never override a modeled family"
            )
        if _classify_leg_raw(value) is LegType.UNKNOWN:
            raise ValueError(
                f"pricing alias target {value!r} classifies UNKNOWN — pointless alias"
            )
        real_event, synth_event = _event_of(key), _event_of(value)
        if events_seen.setdefault(real_event, synth_event) != synth_event:
            raise ValueError(
                f"pricing aliases map event {real_event!r} to multiple synthetic "
                f"events ({events_seen[real_event]!r}, {synth_event!r})"
            )


def set_pricing_aliases(aliases: Mapping[str, str]) -> None:
    """Validate + install the exact-ticker pricing aliases (replaces the
    registry). The derived event-alias registry is rebuilt atomically with it."""
    validate_pricing_aliases(aliases)
    _PRICING_ALIASES.clear()
    _PRICING_EVENT_ALIASES.clear()
    _PRICING_ALIASES.update(aliases)
    for key, value in aliases.items():
        _PRICING_EVENT_ALIASES[_event_of(key)] = _event_of(value)


def resolve_pricing_alias(market_ticker: str) -> str:
    """The ticker the PRICING layer should reason about (identity when
    unaliased). Single-step on purpose — no chains."""
    return _PRICING_ALIASES.get(market_ticker, market_ticker)


def resolve_pricing_event_alias(event_ticker: str) -> str:
    """The event the PRICING layer should group on (identity when unaliased).
    Derived from the market aliases at install time; consumed by
    ``grouping.game_key`` so pricing correlation and risk aggregation move
    together — never resolve one without the other."""
    return _PRICING_EVENT_ALIASES.get(event_ticker, event_ticker)


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
    # MLB per-game player props (combo-eligible per KXMVESPORTSMULTIGAMEEXTENDED-R
    # / KXMVECROSSCATEGORY-R, 2026-07-09). Ticker line suffix -N means N+
    # (floor_strike = N-0.5) — NOT the N-0.5 "over" convention of TOTAL/SPREAD.
    PLAYER_HR = "player_hr"    # batter home runs N+ (KXMLBHR; -1 = 'to hit a HR')
    PLAYER_HIT = "player_hit"  # batter hits N+ (KXMLBHIT)
    PLAYER_KS = "player_ks"    # starting-pitcher strikeouts N+ (KXMLBKS)
    PLAYER_TB = "player_tb"    # batter total bases N+ (KXMLBTB)
    # Combined hits+runs+RBIs (KXMLBHRR, MLBHITSRUNSRBIS.pdf). NOT a home-run
    # market — and 'MLBHRR' contains 'MLBHR', so its keyword MUST precede MLBHR.
    PLAYER_HRR = "player_hrr"
    # Run in the FIRST INNING by either team (KXMLBRFI). Dedicated type: it
    # settles on a first-inning window (never a game TOTAL), and the market
    # ticker has NO outcome suffix (KXMLBRFI-<gamecode> is the full ticker).
    RFI = "rfi"
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
    # MLB props block — placement is LOAD-BEARING: the F5TOTAL / SERIESGAMETOTAL /
    # F5SPREAD blockers must precede TOTAL and SPREAD. SOURCE OF TRUTH (full
    # 11,305-series universe scan, job 24844262, 2026-07-09): bare "HR"/"KS"/
    # "HIT"/"TB"/"RFI" collide with 64/67/9/128/10 series (KXANTHROPICRISK,
    # KXLEADERNFLSACKS, KXDANAWHITEFB, KXBILBASKETBALL, KXSINNERFINISH, …) — so
    # every MLB prop keyword is MLB-anchored, and UNKNOWN-mapped blocker entries
    # kill the known superstring traps (same precede-the-superstring pattern as
    # TEAMTOTAL above / TCORNERS below).
    # --- blockers (explicit UNKNOWN = widen, never masquerade) ---
    ("LEADERMLB", LegType.UNKNOWN),        # KXLEADERMLB{HR,HITS,KS,…} season leaders
    ("MLBHRDERBY", LegType.UNKNOWN),       # KXMLBHRDERBY[QUAL] — contains 'MLBHR'
    ("SERIESGAMETOTAL", LegType.UNKNOWN),  # KXMLBSERIESGAMETOTAL = series game COUNT,
                                           # was mis-typing as full-game TOTAL (live bug)
    ("F5TOTAL", LegType.UNKNOWN),          # KX{MLB,WBC}F5TOTAL = first-5-innings total,
                                           # was mis-typing as full-game TOTAL (live bug:
                                           # 'F5' evades _PERIOD_SERIES)
    ("F5SPREAD", LegType.UNKNOWN),         # KX{MLB,WBC}F5SPREAD — was mis-typing as SPREAD
    # --- MLB player props + RFI (universe-verified unique hit sets) ---
    ("MLBHRR", LegType.PLAYER_HRR),        # MUST precede MLBHR (contains it)
    ("MLBHR", LegType.PLAYER_HR),
    ("MLBHIT", LegType.PLAYER_HIT),
    ("MLBKS", LegType.PLAYER_KS),
    ("MLBTB", LegType.PLAYER_TB),
    ("MLBRFI", LegType.RFI),
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
    series = resolve_pricing_alias(market_ticker).split("-", 1)[0].upper()
    return _PERIOD_SERIES.search(series) is not None


def classify_leg(market_ticker: str) -> LegType:
    return _classify_leg_raw(resolve_pricing_alias(market_ticker))


def _classify_leg_raw(market_ticker: str) -> LegType:
    """Classification from the ticker AS GIVEN (no alias resolution) — the
    public ``classify_leg`` resolves first; the alias validator needs the raw
    verdict to enforce the only-UNKNOWN rule without registry recursion."""
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
    market_ticker = resolve_pricing_alias(market_ticker)
    series = market_ticker.split("-", 1)[0].upper()
    for keyword, sport in _SPORT_KEYWORDS:
        if keyword in series:
            return sport
    return Sport.UNKNOWN
