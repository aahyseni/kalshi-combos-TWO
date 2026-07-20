"""Same-game-parlay correlation structure: typed-pair matrices for the copula.

Replaces the flat same-event ρ with a SIGNED per-pair prior keyed by the two
legs' structural types (YES–YES orientation; the copula sign-flips NO-side
legs downstream). Every prior carries its own uncertainty band; untyped pairs
fall back to the flat prior with a band WIDE ENOUGH TO SPAN ZERO — an unmodeled
same-game pair is only a prior-mean positive and could be uncorrelated or
anti-correlated, so its low matrix must reach the negative regime (fail-safe
against adverse selection). Calibration from co-settlement data updates the
config table — never this code.

The output is three PSD correlation matrices (low / point / high) so the
joint can be re-priced across the band and the spread priced into width.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from combomaker.pricing.conditionals_mlb import (
    BATTER_FAMILIES,
    SAME_PLAYER_RHO_BAND,
    implied_rho,
    strongest_measured_direction,
)
from combomaker.pricing.copula import is_psd, nearest_psd
from combomaker.pricing.legtypes import (
    LegType,
    Sport,
    classify_leg,
    classify_sport,
    pair_key,
    resolve_pricing_alias,
)
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
    # Orientation CURVES: "<sport>:<pair_key>" -> sorted (marginal, rho) knots,
    # piecewise-linear (flat outside the range). When present for a one-moneyline
    # pair with known marginals, the curve WINS over the scalar / fav-dog prior.
    oriented_curve: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    oriented_curve_uncertainty: dict[str, float] = field(default_factory=dict)


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

# A winner leg whose named side is a draw (not a team) — the 1H-winner ×
# FT-winner correlation is measured team-vs-team only, so a draw leg has no
# calibrated orientation and must fall back to the flat prior.
_DRAW_SUFFIXES = ("TIE", "DRAW")


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


def _interp_curve(x: float, knots: Sequence[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation of ``knots`` (sorted by first coord) at
    ``x``, with a FLAT clamp outside the knot range (a marginal below the lowest
    knot keeps the lowest knot's rho; above the highest, the highest's)."""
    pts = sorted(knots)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return pts[-1][1]


def _oriented_curve_prior(
    key: str, sport: str, params: SgpParams, ml_marginal: float
) -> _PairPrior | None:
    """Win-prob CURVE prior for a pair containing one MONEYLINE leg, when the
    residual rho is a monotone function of the ML leg's YES-side marginal rather
    than two fav/dog plateaus (btts|moneyline). Config expresses it as sorted
    ``(marginal, rho)`` knots under ``oriented_curve[<sport>:<key>]``; this wins
    over ``_oriented_prior`` whenever the knots exist and marginals are known."""
    curve_key = f"{sport}:{key}"
    knots = params.oriented_curve.get(curve_key) or params.oriented_curve.get(key)
    if not knots:
        return None
    rho = _interp_curve(ml_marginal, knots)
    band = params.oriented_curve_uncertainty.get(
        curve_key, params.oriented_curve_uncertainty.get(key, params.typed_uncertainty)
    )
    return _PairPrior(rho, band, f"{curve_key} curve (ml_p={ml_marginal:.2f})")


# Team-corner ticker suffix: a team code followed by the (over-)line digits, e.g.
# ``…-POR4`` / ``…-COL5``. The line digits must be STRIPPED before comparing team
# identity — POR4 and POR8 are the SAME team's nested corner lines, which
# ``_winner_team`` (whole-suffix) would read as two different teams.
_CORNERS_TEAM_SUFFIX = re.compile(r"^([A-Za-z]+)\d*$")


def _corners_team_name(ticker: str) -> str | None:
    """The team a team-corners leg names — its ticker suffix with the trailing
    line digits removed (``…-POR8`` -> ``POR``). None when the suffix isn't a
    team-code + optional digits shape (don't guess)."""
    suffix = ticker.rsplit("-", 1)[-1].upper()
    m = _CORNERS_TEAM_SUFFIX.match(suffix)
    if m is None:
        return None
    return m.group(1)


def _corners_team_prior(
    key: str, sport: str, params: SgpParams, ticker_a: str, ticker_b: str
) -> _PairPrior | None:
    """corners_team × corners_team prior, resolved to ``:same`` / ``:opp`` by
    whether the two legs name the same team (nested lines on one team -> strong
    positive comonotone approx) or opposite teams (territory zero-sum, -ρ). The
    same/opposite analogue of ``_winner_period_prior``, but the team is parsed by
    stripping the trailing line digits. Unparseable suffix -> None (caller falls
    back to the plain entry; never invent an orientation)."""
    team_a = _corners_team_name(ticker_a)
    team_b = _corners_team_name(ticker_b)
    if team_a is None or team_b is None:
        return None
    orient = "same" if team_a == team_b else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _spread_team(ticker: str) -> str | None:
    """The team a (1H- or full-game) spread leg names — its ticker suffix with
    the trailing line digits removed (``…-FRA2`` -> ``FRA``). Same TEAM+digits
    shape as team-corners, so it reuses ``_CORNERS_TEAM_SUFFIX`` (whole-suffix
    ``_winner_team`` would read the line digits as part of the team). None when
    the suffix isn't a team-code + optional digits shape (don't guess). Pricing
    aliases resolve first (identity when unaliased)."""
    return _corners_team_name(resolve_pricing_alias(ticker))


def _spread_pair_prior(
    key: str, sport: str, params: SgpParams, ticker_a: str, ticker_b: str
) -> _PairPrior | None:
    """1H-spread × FT-spread prior, resolved to ``:same`` / ``:opp`` by whether
    the two spread legs name the same team (a big 1H lead → a big FT lead, +ρ) or
    opposite teams (a 1H lead for one is a >=4-goal-swing FT lead for the other,
    near-mutually-exclusive, −ρ). Both suffixes are TEAM+line-digits, parsed by
    stripping the digits. Unparseable suffix → None (caller falls back; never
    invent an orientation)."""
    team_a = _spread_team(ticker_a)
    team_b = _spread_team(ticker_b)
    if team_a is None or team_b is None:
        return None
    orient = "same" if team_a == team_b else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _spread_winner_prior(
    key: str, sport: str, params: SgpParams, spread_ticker: str, ml_ticker: str
) -> _PairPrior | None:
    """1H-spread × FT-moneyline prior, resolved to ``:same`` / ``:opp`` by
    whether the spread leg (TEAM+line-digits suffix) and the winner leg (whole
    last segment) name the same team (a 1H lead → that team wins, +ρ) or
    opposite teams (near-mutually-exclusive, −ρ), or to ``:tie`` when the
    winner leg is a DRAW (MEASURED −0.44, M1 2026-07-12 — a 2-goal 1H lead
    makes a FT draw unlikely; the flat +0.6 fallback this case used to hit
    was the WRONG SIGN; pooled over both teams). Either suffix unparseable
    (spread not TEAM+digits, or a non-draw non-team winner) → None so the
    caller falls back — never guess a sign."""
    team_s = _spread_team(spread_ticker)
    if team_s is None:
        return None
    team_m = _winner_team(ml_ticker)
    if team_m is None:
        suffix = ml_ticker.rsplit("-", 1)[-1].upper()
        if suffix not in _DRAW_SUFFIXES:
            return None  # unparseable winner suffix: do not invent an orientation
        return _lookup_pair(f"{key}:tie", sport, params)
    orient = "same" if team_s == team_m else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _winner_team(ticker: str) -> str | None:
    """The team code a (1H- or full-game) moneyline/advance leg names — its
    ticker's last hyphen segment. None for a draw side, which has no measured
    1H×FT orientation. Two same-game winner legs name the SAME team iff these
    strings match (both are drawn from the one game's team-code vocabulary).
    Pricing aliases resolve first: a champion leg's RAW suffix is a 2-letter
    country code (``…-AR``) that startswith-collides with the 3-letter player
    codes (``ARGMESSI``) only by luck — the synthetic advance suffix (``ARG``)
    is the one from the game's actual vocabulary."""
    suffix = resolve_pricing_alias(ticker).rsplit("-", 1)[-1].upper()
    if not suffix or suffix in _DRAW_SUFFIXES:
        return None
    return suffix


def _advance_player_prior(
    key: str, sport: str, params: SgpParams, advance_ticker: str, player_ticker: str
) -> _PairPrior | None:
    """ADVANCE × player-scorer prior, resolved to ``:same`` / ``:opp`` by whether
    the scorer plays for the ADVANCING team (+ρ — his goals push his team through)
    or the OPPONENT (−ρ, mirror). The advance suffix IS the team code (advance is
    never a draw); a player leg is ``SERIES-GAME-<TEAM+name+number>-<goals>``, so
    the scorer is same-team iff his player code starts with the advance team code.
    Unparseable (short player ticker / empty code) → None so the caller falls back
    — never guess the sign of a scorer×advance pair."""
    adv_team = _winner_team(advance_ticker)
    parts = player_ticker.split("-")
    if adv_team is None or len(parts) < 4:
        return None
    player_code = parts[-2].upper()
    if not player_code:
        return None
    orient = "same" if player_code.startswith(adv_team) else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _spread_player_prior(
    key: str, sport: str, params: SgpParams, spread_ticker: str, player_ticker: str
) -> _PairPrior | None:
    """SPREAD × player-scorer prior, resolved to ``:same`` / ``:opp`` by whether
    the scorer plays for the team winning by the margin (+ρ — his goals build the
    margin) or the opponent (−ρ). Same as ``_advance_player_prior`` but the spread
    team is parsed with ``_spread_team`` (TEAM+line-digits suffix). Unparseable
    (spread not TEAM+digits, short/empty player code) → None so the caller falls
    back — never guess the sign."""
    spread_team = _spread_team(spread_ticker)
    parts = player_ticker.split("-")
    if spread_team is None or len(parts) < 4:
        return None
    player_code = parts[-2].upper()
    if not player_code:
        return None
    orient = "same" if player_code.startswith(spread_team) else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _corners_winner_prior(
    key: str, sport: str, params: SgpParams, corners_ticker: str, ml_ticker: str
) -> _PairPrior | None:
    """team-corners × match-winner prior, resolved to ``:same`` / ``:opp`` /
    ``:tie``. A team's corners are anti-correlated with THAT team winning (a
    chasing/pressing team earns corners, −ρ), positively with the OPPONENT
    winning (mirror, +ρ), and ~0 with a draw. The sign is STRENGTH-CONTROLLED:
    the raw pooled corr is a Simpson's-paradox trap (strong teams both win and
    take corners → spuriously positive, the WRONG sign). corners suffix is
    TEAM+line-digits (parsed by ``_corners_team_name``); the winner suffix is a
    whole team code or a draw token. Unparseable corners team, or an
    unrecognizable (non-draw) winner suffix → None so the caller falls back —
    never guess the sign."""
    corners_team = _corners_team_name(corners_ticker)
    if corners_team is None:
        return None
    ml_team = _winner_team(ml_ticker)
    if ml_team is None:
        suffix = ml_ticker.rsplit("-", 1)[-1].upper()
        if suffix not in _DRAW_SUFFIXES:
            return None  # unparseable winner suffix: do not invent an orientation
        return _lookup_pair(f"{key}:tie", sport, params)
    orient = "same" if corners_team == ml_team else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _corners_spread_prior(
    key: str, sport: str, params: SgpParams, corners_ticker: str, spread_ticker: str
) -> _PairPrior | None:
    """team-corners × spread prior, resolved to ``:same`` / ``:opp`` by whether
    the corners team is the one COVERING the margin (chasing/pressing team earns
    corners → −ρ) or the opponent (+ρ). Sibling of ``_corners_winner_prior``;
    both suffixes are TEAM+line-digits (``_corners_team_name`` / ``_spread_team``).
    Sign is STRENGTH-CONTROLLED (the raw pooled corr is a Simpson trap, +0.07
    wrong-signed). Unparseable suffix → None so the caller falls back — never
    guess the sign."""
    corners_team = _corners_team_name(corners_ticker)
    spread_team = _spread_team(spread_ticker)
    if corners_team is None or spread_team is None:
        return None
    orient = "same" if corners_team == spread_team else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _oriented_team_prior(
    key: str,
    sport: str,
    params: SgpParams,
    team_a: str | None,
    team_b: str | None,
    *,
    is_tie: bool,
) -> _PairPrior | None:
    """Emit ``:same`` / ``:opp`` by whether two parsed team codes match, or
    ``:tie`` when a winner leg is a draw. None on unparseable (never guess a
    sign). Shared by the 1H-winner/1H-spread × team-directional-FT resolvers."""
    if is_tie:
        return _lookup_pair(f"{key}:tie", sport, params)
    if team_a is None or team_b is None:
        return None
    return _lookup_pair(f"{key}:{'same' if team_a == team_b else 'opp'}", sport, params)


def _period_winner_player_prior(
    key: str, sport: str, params: SgpParams, fhm_ticker: str, player_ticker: str
) -> _PairPrior | None:
    """1H-winner × scorer: ``:same`` / ``:opp`` by whether the scorer plays for
    the 1H leader (his player code starts with the leader team code), ``:tie``
    when the 1H is a draw. Mirrors ``_advance_player_prior`` with a tie branch."""
    if fhm_ticker.rsplit("-", 1)[-1].upper() in _DRAW_SUFFIXES:
        return _lookup_pair(f"{key}:tie", sport, params)
    fhm_team = _winner_team(fhm_ticker)
    parts = player_ticker.split("-")
    if fhm_team is None or len(parts) < 4:
        return None
    player_code = parts[-2].upper()
    if not player_code:
        return None
    orient = "same" if player_code.startswith(fhm_team) else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _winner_period_prior(
    key: str, sport: str, params: SgpParams, fhm_ticker: str, ml_ticker: str
) -> _PairPrior | None:
    """1H-winner × FT-winner prior. Team-vs-team resolves to ``:same`` /
    ``:opp`` by whether the two winner legs name the same team (+ρ) or opposite
    teams (−ρ) — the same/opposite analogue of ``_oriented_prior``'s fav/dog
    blend, but the choice is HARD (a sign flip), not a marginal-blended
    interpolation. DRAW-involving pairs (MEASURED, M1 2026-07-12 — the flat
    +0.6 fallback they used to hit was the WRONG SIGN for two of the three
    shapes) resolve to ``:tiexwin`` (1H draw × FT team win), ``:teamxtie``
    (1H team lead × FT draw) or ``:tiextie`` (draw × draw); suffix order =
    pair_key leg order, 1H leg first, so the caller MUST pass the 1H ticker
    first. An empty/garbage suffix on either leg → None (caller falls back;
    never invent an orientation)."""
    fh_suffix = fhm_ticker.rsplit("-", 1)[-1].upper()
    ft_suffix = ml_ticker.rsplit("-", 1)[-1].upper()
    if not fh_suffix or not ft_suffix:
        return None
    fh_tie = fh_suffix in _DRAW_SUFFIXES
    ft_tie = ft_suffix in _DRAW_SUFFIXES
    if fh_tie and ft_tie:
        return _lookup_pair(f"{key}:tiextie", sport, params)
    if fh_tie:
        return _lookup_pair(f"{key}:tiexwin", sport, params)
    if ft_tie:
        return _lookup_pair(f"{key}:teamxtie", sport, params)
    team_fh = _winner_team(fhm_ticker)
    team_ft = _winner_team(ml_ticker)
    if team_fh is None or team_ft is None:
        return None
    orient = "same" if team_fh == team_ft else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _period_total_prior(
    key: str, sport: str, params: SgpParams, ml_ticker: str
) -> _PairPrior | None:
    """1H-winner × 1H-total prior (WITHIN the first half), resolved to ``:team``
    or ``:tie`` by whether the 1H-moneyline leg names a team or the draw. The
    sign flips HARD: a 1H lead REQUIRES a goal (lead ⊂ over ⇒ strong +ρ), whereas
    a 1H tie CONTAINS the goalless 0-0 (under ⊂ tie ⇒ tie×over strong −ρ). Same
    same/opposite analogue as ``_winner_period_prior`` but oriented team-vs-tie.
    A team suffix → ``:team``; an explicit TIE/DRAW suffix → ``:tie``; anything
    else (empty/garbage) → None so the caller falls back — never guess a sign."""
    if _winner_team(ml_ticker) is not None:
        orient = "team"
    else:
        suffix = ml_ticker.rsplit("-", 1)[-1].upper()
        if suffix not in _DRAW_SUFFIXES:
            return None  # unparseable winner suffix: do not invent an orientation
        orient = "tie"
    return _lookup_pair(f"{key}:{orient}", sport, params)


# --- MLB team routing ---------------------------------------------------------
# An MLB prop ticker embeds the player's team as the PREFIX of its player
# segment (KXMLBKS-26JUL092145COLSF-SFCWHISENHUNT88-8 -> SF) and the game
# code's tail is the away+home team-code blob, ALSO un-delimited (COLSF =
# COL+SF). Codes are 2 or 3 chars, so a naive both-split prefix match is
# ambiguous ~20% of the time (COLSF + COLRFELTNER18 matches CO and COL).
# Resolution mirrors structural.py:143-180 (keep in sync): anchor a candidate
# fragment at the blob ENDS — prefix ⇒ away, suffix ⇒ home, both-or-neither
# refuses. Safe because no Kalshi MLB team code is a prefix of another and
# every away+home concatenation tiles uniquely (all 30 codes enumerated live
# 2026-07-09, 295 game blobs + all 870 ordered pairs, job 24844262;
# tools/spotcheck_mlb_team_routing.py re-proves this against the live API).
# Doubleheaders append G<digit> to the blob (structural.py:126-135) and are
# stripped identically; both legs must carry the IDENTICAL raw game-code
# segment so a G1×G2 pair can never route. ANY doubt -> None -> the caller
# falls back to the plain (neutralized / unrouted) entry — never invent an
# orientation (the soccer scorer-guard contract).

_MLB_GAME_CODE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z0-9]{4,})$")
_MLB_DOUBLEHEADER = re.compile(r"([A-Z]{4,})G\d")

# The routed MLB player-prop leg types. RFI is absent on purpose (team-
# symmetric, suffixless KXMLBRFI-<gamecode>); KS is included so batter-stat ×
# starter-Ks pairs route to the FACING (:opp) / teammate (:same) keys.
_MLB_PLAYER_PROP_TYPES = frozenset({
    LegType.PLAYER_HR,
    LegType.PLAYER_HIT,
    LegType.PLAYER_KS,
    LegType.PLAYER_TB,
    LegType.PLAYER_HRR,
})

# --- :rN rung keys (Phase 2 wire-list convention line 2) ------------------------
# A rung key pins a pair prior to the Kalshi ticker LINE INTEGER of a
# rung-keyed leg (leg settles YES iff stat/margin > N-0.5; props: N+; spread:
# wins by N+ — an r1.5 run line is ticker 2 = ':r2'). Rung-keyed families:
# the four batter props below + SPREAD. player_ks / total / moneyline / rfi
# legs NEVER carry a rung (self-median / season-median frames) even when
# their tickers end in digits (KXMLBKS-…-8, KXMLBTOTAL-…-9) — the gate is the
# LEG TYPE, never the ticker shape. When BOTH legs are rung-keyed families
# the suffixes CHAIN in pair_key leg order, first leg's rung first
# ('player_hit|spread:same:r1:r2' = hit 1+ × own team wins by 2+); when only
# one is, the suffix is single ('player_ks|player_tb:opp:r4' — the TB leg's).
# Lookup fallback (fail-closed): exact rung key → un-runged oriented key →
# plain key. NO interpolation or extrapolation of rungs, EVER — the tb×ks
# facing ladder is U-shaped (wire-list NOT-WIRED flag), so a missing rung
# falls through the chain, never to a neighbouring rung. Each chain level is
# a single ``_lookup_pair`` call, so the uncertainty band ('mlb:'+key)
# always resolves at the SAME level as the value.

_RUNG_KEYED_PROP_TYPES = frozenset({
    LegType.PLAYER_HIT,
    LegType.PLAYER_HR,
    LegType.PLAYER_TB,
    LegType.PLAYER_HRR,
})
_RUNG_KEYED_TYPES = _RUNG_KEYED_PROP_TYPES | {LegType.SPREAD}

# Spread suffix TEAM+line-digits with the digits REQUIRED (contrast
# _CORNERS_TEAM_SUFFIX, digits optional): a spread leg without a parseable
# line int has no rung — it falls back un-runged, never to a guessed rung.
_SPREAD_RUNG_SUFFIX = re.compile(r"^[A-Za-z]+(\d+)$")


def _leg_rung(leg_type: LegType, ticker: str) -> int | None:
    """The Kalshi line integer of a RUNG-KEYED leg (the ':rN' grammar), or
    None. Batter props: 4-segment ticker with an all-digit last segment
    (KXMLBHIT-<game>-<player>-<line>; the exact access pattern of
    ``_mlb_same_player_conditional_prior``). Spread: TEAM+line-digits suffix,
    the digits are the line int (…-COL2 → 2, same suffix shape
    ``_spread_team`` strips). Never-rung-keyed families return None
    unconditionally — a trailing digit segment on ks/total/… is NOT a rung."""
    if leg_type in _RUNG_KEYED_PROP_TYPES:
        parts = ticker.upper().split("-")
        if len(parts) == 4 and parts[3].isdigit():
            return int(parts[3])
        return None
    if leg_type is LegType.SPREAD:
        m = _SPREAD_RUNG_SUFFIX.match(ticker.rsplit("-", 1)[-1].upper())
        if m is not None:
            return int(m.group(1))
        return None
    return None


def _pair_rung_suffix(ticker_a: str, ticker_b: str) -> str:
    """The chained ':rN' suffix for a pair, in pair_key leg order (first
    leg's rung first; an equal-type pair orders its rungs ascending so the
    key is leg-order-independent). Empty string when no leg is a rung-keyed
    family, OR when ANY rung-keyed leg's rung fails to parse — a partial
    chain would collide with the single-suffix grammar, so the whole pair
    falls back un-runged (never guess a rung)."""
    type_a, type_b = classify_leg(ticker_a), classify_leg(ticker_b)
    if str(type_a) > str(type_b):
        type_a, type_b = type_b, type_a
        ticker_a, ticker_b = ticker_b, ticker_a
    rungs: list[int] = []
    for leg_type, ticker in ((type_a, ticker_a), (type_b, ticker_b)):
        if leg_type in _RUNG_KEYED_TYPES:
            rung = _leg_rung(leg_type, ticker)
            if rung is None:
                return ""
            rungs.append(rung)
    if type_a is type_b and len(rungs) == 2:
        rungs.sort()
    return "".join(f":r{r}" for r in rungs)


def _lookup_pair_runged(
    key: str, sport: str, params: SgpParams, rung_suffix: str
) -> _PairPrior | None:
    """One chain level plus its rung refinement: the exact rung key wins when
    wired, else the un-runged ``key``. Both lookups go through
    ``_lookup_pair``, so the band always resolves at the same chain level as
    the value (never an exact-rung value with an un-runged band, or vice
    versa)."""
    if rung_suffix:
        exact = _lookup_pair(key + rung_suffix, sport, params)
        if exact is not None:
            return exact
    return _lookup_pair(key, sport, params)


def _mlb_team_blob(ticker: str) -> tuple[str, str] | None:
    """(raw game-code segment, away+home team blob) from an MLB ticker's second
    hyphen segment, doubleheader G<digit> stripped. None when the segment isn't
    date+time+alpha-blob shaped (don't guess)."""
    parts = ticker.upper().split("-")
    if len(parts) < 2:
        return None
    m = _MLB_GAME_CODE.match(parts[1])
    if m is None:
        return None
    blob = m.group(1)
    dh = _MLB_DOUBLEHEADER.fullmatch(blob)
    if dh is not None:
        blob = dh.group(1)
    if not blob.isalpha() or len(blob) < 4:
        return None
    return parts[1], blob


def _mlb_side_of(code: str, blob: str) -> str | None:
    """Which side of the away+home blob ``code`` anchors to: ``"away"`` when it
    prefixes the blob, ``"home"`` when it suffixes it. Both-or-neither refuses
    (None) — mirrors structural.py ``_team_of``. Unambiguous on the verified
    vocabulary: no MLB team code is a prefix of another."""
    if len(code) < 2 or len(code) >= len(blob):
        return None
    is_away = blob.startswith(code)
    is_home = blob.endswith(code)
    if is_away == is_home:
        return None
    return "away" if is_away else "home"


def _mlb_player_side(player_seg: str, blob: str) -> str | None:
    """The side the player-segment's team prefix anchors to — longest leading
    fragment (4→2) that anchors exactly one blob end, mirroring structural.py
    ``_player_team``. None when nothing anchors (don't guess)."""
    for length in range(min(4, len(player_seg) - 1), 1, -1):
        side = _mlb_side_of(player_seg[:length], blob)
        if side is not None:
            return side
    return None


def _mlb_prop_pair_prior(
    key: str, sport: str, params: SgpParams, ticker_a: str, ticker_b: str
) -> _PairPrior | None:
    """MLB prop × prop prior, resolved to ``:same`` / ``:opp`` by whether the
    two players' team prefixes anchor to the same side of the game blob.
    FACING CONVENTION: for any batter-stat × player_ks pair, ``:opp`` IS the
    facing case (a batter bats against the OPPOSING starter) and carries the
    measured negative; ``:same`` is the teammate case. Guards: both legs must
    carry the identical raw game-code segment (doubleheader-safe), and an
    IDENTICAL player segment (same player, cross-family) refuses — that pair
    is containment-shaped (HR⇒HIT/TB/HRR), never a copula rho; the
    containment phase owns it. Unparseable anything → None (caller falls back
    to the plain unrouted entry; never invent an orientation)."""
    game_a = _mlb_team_blob(ticker_a)
    game_b = _mlb_team_blob(ticker_b)
    if game_a is None or game_b is None or game_a != game_b:
        return None
    parts_a = ticker_a.upper().split("-")
    parts_b = ticker_b.upper().split("-")
    if len(parts_a) != 4 or len(parts_b) != 4:
        return None
    seg_a, seg_b = parts_a[2], parts_b[2]
    if seg_a == seg_b:
        return None  # same player cross-family: containment, not a rho
    side_a = _mlb_player_side(seg_a, game_a[1])
    side_b = _mlb_player_side(seg_b, game_b[1])
    if side_a is None or side_b is None:
        return None
    orient = "same" if side_a == side_b else "opp"
    return _lookup_pair_runged(
        f"{key}:{orient}", sport, params, _pair_rung_suffix(ticker_a, ticker_b)
    )


def _mlb_same_player_conditional_prior(
    type_a: LegType,
    type_b: LegType,
    ticker_a: str,
    ticker_b: str,
    p_a: float,
    p_b: float,
) -> _PairPrior | None:
    """SAME-PLAYER cross-stat batter pair (DO-2, 2026-07-10): the YES-YES rho
    implied by the MEASURED same-player conditional — joint = P(conditioning
    leg) x P(other | conditioning), conditionals_mlb.SAME_PLAYER_CONDITIONALS
    — solved at the LIVE marginals, so the copula reproduces the
    conditional-table joint exactly (and prices the NO-side sign cases
    consistently: one rho fixes the whole 2x2 table). This resolver lands
    BEFORE the routing resolver's same-player refusal (its None seam,
    reviewer defect #4) so these pairs never price at the distinct-player [D]
    entries — the 2026-07-10 sweep regression. The CLASSIFIER
    (relationships.py MLB same-player family, keep in sync) only lets BARE
    pairs with a strong measured cell reach the copula: exact cells become
    containment/impossible there, unmeasured pairs decline UNKNOWN. Guards
    mirror ``_mlb_prop_pair_prior``; None on any doubt -> the caller proceeds
    to the ordinary routing resolvers (never invent a conditional)."""
    fam_a = BATTER_FAMILIES.get(type_a)
    fam_b = BATTER_FAMILIES.get(type_b)
    if fam_a is None or fam_b is None or fam_a == fam_b:
        # KS never maps (different entity); same-family same-player rungs are
        # a nested ladder, not a conditional cell — out of scope here.
        return None
    game_a = _mlb_team_blob(ticker_a)
    game_b = _mlb_team_blob(ticker_b)
    if game_a is None or game_b is None or game_a != game_b:
        return None
    parts_a = ticker_a.upper().split("-")
    parts_b = ticker_b.upper().split("-")
    if len(parts_a) != 4 or len(parts_b) != 4:
        return None
    if not parts_a[2] or parts_a[2] != parts_b[2]:
        return None  # different players: teammate/opponent routing owns it
    if not (parts_a[3].isdigit() and parts_b[3].isdigit()):
        return None
    pick = strongest_measured_direction(fam_a, int(parts_a[3]), fam_b, int(parts_b[3]))
    if pick is None:
        return None  # exact-only / unmeasured: classifier owns those shapes
    a_conditions, p_cond, n = pick
    p_c, p_o = (p_a, p_b) if a_conditions else (p_b, p_a)
    rho = implied_rho(p_c, p_o, p_cond)
    if rho is None:
        return None
    return _PairPrior(
        rho,
        SAME_PLAYER_RHO_BAND,
        f"mlb same-player {fam_a}{parts_a[3]}|{fam_b}{parts_b[3]} conditional "
        f"(p_cond={p_cond:.3f} n={n} rho={rho:+.3f})",
    )


def _mlb_winner_spread_prior(
    key: str, sport: str, params: SgpParams, ml_ticker: str, spread_ticker: str
) -> _PairPrior | None:
    """MLB moneyline x spread (run line) prior, resolved to ``:same`` /
    ``:opp`` by anchoring BOTH the ML suffix and the spread suffix's team
    (trailing line digits stripped) against the game blob — raw suffix
    inequality alone is NOT proof of opposite teams (reviewer defect #3, the
    ONE anchored parser). The containment / impossible shapes are intercepted
    in relationships.py BEFORE the copula; this routes the reachable copula
    cases (e.g. win-yes x cover-no) to the near-Frechet +-0.95 oriented
    entries (measured exact: 0/98,980 violations). Both legs must carry the
    identical raw game-code segment (doubleheader-safe). Unparseable anything
    -> None (caller falls back to the plain sign-spanning 0.00 fallback;
    never guess a sign)."""
    game_m = _mlb_team_blob(ml_ticker)
    game_s = _mlb_team_blob(spread_ticker)
    if game_m is None or game_s is None or game_m != game_s:
        return None
    ml_side = _mlb_side_of(ml_ticker.upper().rsplit("-", 1)[-1], game_m[1])
    spread_team = _spread_team(spread_ticker)
    if ml_side is None or spread_team is None:
        return None
    spread_side = _mlb_side_of(spread_team, game_s[1])
    if spread_side is None:
        return None
    orient = "same" if ml_side == spread_side else "opp"
    return _lookup_pair_runged(
        f"{key}:{orient}", sport, params, _pair_rung_suffix(ml_ticker, spread_ticker)
    )


def _mlb_winner_prop_prior(
    key: str, sport: str, params: SgpParams, ml_ticker: str, prop_ticker: str
) -> _PairPrior | None:
    """MLB moneyline × player-prop prior, resolved to ``:same`` / ``:opp`` by
    whether the prop player's team prefix and the ML suffix anchor to the same
    side of the game blob (``:same`` = the player's team is the ML YES team,
    e.g. ml|ks +0.24; ``:opp`` the exact sign flip). YES–YES orientation only —
    the copula sign-flips NO legs downstream. Both legs must carry the
    identical raw game-code segment. Unparseable → None (caller falls back to
    the neutralized 0.00 sign-spanning entry; never guess a sign)."""
    game_m = _mlb_team_blob(ml_ticker)
    game_p = _mlb_team_blob(prop_ticker)
    if game_m is None or game_p is None or game_m != game_p:
        return None
    ml_side = _mlb_side_of(ml_ticker.upper().rsplit("-", 1)[-1], game_m[1])
    parts = prop_ticker.upper().split("-")
    if len(parts) != 4:
        return None
    prop_side = _mlb_player_side(parts[2], game_p[1])
    if ml_side is None or prop_side is None:
        return None
    orient = "same" if ml_side == prop_side else "opp"
    return _lookup_pair_runged(
        f"{key}:{orient}", sport, params, _pair_rung_suffix(ml_ticker, prop_ticker)
    )


def _mlb_spread_prop_prior(
    key: str, sport: str, params: SgpParams, spread_ticker: str, prop_ticker: str
) -> _PairPrior | None:
    """MLB run-line spread × player-prop prior, resolved to ``:same`` /
    ``:opp`` by whether the prop player's team prefix and the spread leg's
    team (TEAM+line-digits suffix, digits stripped) anchor to the same side
    of the game blob — ``:same`` = the player's team IS the spread YES team,
    the exact ml|prop axis (spread is team-signed like the moneyline, config
    [C-sibling] note). SPREAD is a rung-keyed family, so the lookup chains
    the line ints per the ':rN' grammar ('player_hit|spread:same:r1:r2',
    'player_ks|spread:opp:r2' — ks never runged). Both legs must carry the
    identical raw game-code segment (doubleheader-safe). Unparseable
    anything → None (caller falls back to the plain sign-spanning entry;
    never guess a sign)."""
    game_s = _mlb_team_blob(spread_ticker)
    game_p = _mlb_team_blob(prop_ticker)
    if game_s is None or game_p is None or game_s != game_p:
        return None
    spread_team = _spread_team(spread_ticker)
    if spread_team is None:
        return None
    spread_side = _mlb_side_of(spread_team, game_s[1])
    parts = prop_ticker.upper().split("-")
    if len(parts) != 4:
        return None
    prop_side = _mlb_player_side(parts[2], game_p[1])
    if spread_side is None or prop_side is None:
        return None
    orient = "same" if spread_side == prop_side else "opp"
    return _lookup_pair_runged(
        f"{key}:{orient}", sport, params, _pair_rung_suffix(spread_ticker, prop_ticker)
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
    # Fail-safe band for pairs that fall through to ``default_rho`` (a leg types
    # UNKNOWN, or a typed pair has no calibrated prior). The flat prior is only a
    # prior-MEAN positive: such a pair could be uncorrelated or truly negative
    # (e.g. MLB pitcher-strikeouts × game-total ≈ −0.2). Widen the band to at
    # least ``|default_rho| + untyped_uncertainty`` so ``corr_low =
    # clamp(default_rho − band)`` reaches ≤ 0 into the negative regime — never a
    # confident positive whose low bound can't span zero. This is a pure
    # WIDENING: the point estimate stays ``default_rho`` and calibrated/typed
    # pairs (which resolve their own tight band) are untouched.
    fallback_band = abs(params.default_rho) + params.untyped_uncertainty
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
                rho, band = params.default_rho, fallback_band
                untyped += 1
                notes.append(f"untyped pair {key}: flat prior {rho}")
            else:
                pair_types = {types[i], types[j]}
                if pair_types == {LegType.FIRST_HALF_MONEYLINE, LegType.MONEYLINE}:
                    # 1H-winner × FT-winner: sign flips on same-vs-opposite
                    # team; draw shapes resolve :tiexwin/:teamxtie/:tiextie
                    # (suffix order = pair_key leg order, 1H leg first).
                    fhm2_i = i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    ml3_i = j if fhm2_i == i else i
                    prior = _winner_period_prior(
                        key,
                        sport,
                        params,
                        legs[fhm2_i].market_ticker,
                        legs[ml3_i].market_ticker,
                    )
                elif pair_types == {LegType.FIRST_HALF_SPREAD, LegType.SPREAD}:
                    # 1H-spread × FT-spread: sign flips on same-vs-opposite team.
                    prior = _spread_pair_prior(
                        key, sport, params, legs[i].market_ticker, legs[j].market_ticker
                    )
                elif pair_types == {LegType.FIRST_HALF_SPREAD, LegType.MONEYLINE}:
                    # 1H-spread × FT-winner: sign flips on same-vs-opposite team.
                    fhs_index = i if types[i] is LegType.FIRST_HALF_SPREAD else j
                    ml_index = j if fhs_index == i else i
                    prior = _spread_winner_prior(
                        key,
                        sport,
                        params,
                        legs[fhs_index].market_ticker,
                        legs[ml_index].market_ticker,
                    )
                elif pair_types == {
                    LegType.FIRST_HALF_MONEYLINE,
                    LegType.FIRST_HALF_TOTAL,
                }:
                    # 1H-winner × 1H-total: sign flips HARD on team-vs-tie
                    # (lead⊂over ⇒ +ρ; under⊂tie ⇒ tie×over −ρ).
                    fhm_index = (
                        i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    )
                    prior = _period_total_prior(
                        key, sport, params, legs[fhm_index].market_ticker
                    )
                elif (
                    types[i] is LegType.CORNERS_TEAM and types[j] is LegType.CORNERS_TEAM
                ):
                    # Team corners × team corners: sign flips on same-vs-opposite
                    # team (nested lines on one team vs opposing-team territory).
                    prior = _corners_team_prior(
                        key, sport, params, legs[i].market_ticker, legs[j].market_ticker
                    )
                elif pair_types == {LegType.ADVANCE, LegType.PLAYER_GOAL}:
                    # advance × scorer: sign flips on whether the scorer plays for
                    # the advancing team (+ρ) or the opponent (−ρ).
                    adv_index = i if types[i] is LegType.ADVANCE else j
                    pl_index = j if adv_index == i else i
                    prior = _advance_player_prior(
                        key,
                        sport,
                        params,
                        legs[adv_index].market_ticker,
                        legs[pl_index].market_ticker,
                    )
                elif pair_types == {LegType.SPREAD, LegType.PLAYER_GOAL}:
                    # spread × scorer: sign flips on whether the scorer plays for
                    # the team winning by the margin (+ρ) or the opponent (−ρ).
                    spr_index = i if types[i] is LegType.SPREAD else j
                    pl_index = j if spr_index == i else i
                    prior = _spread_player_prior(
                        key,
                        sport,
                        params,
                        legs[spr_index].market_ticker,
                        legs[pl_index].market_ticker,
                    )
                elif pair_types == {LegType.CORNERS_TEAM, LegType.MONEYLINE}:
                    # team corners × match winner: −ρ if the corners team IS the
                    # winner (chasing team earns corners), +ρ if the OPPONENT wins,
                    # ~0 on a draw. Strength-controlled (raw pooled is a Simpson
                    # trap with the wrong sign).
                    ct_index = i if types[i] is LegType.CORNERS_TEAM else j
                    ml_index = j if ct_index == i else i
                    prior = _corners_winner_prior(
                        key,
                        sport,
                        params,
                        legs[ct_index].market_ticker,
                        legs[ml_index].market_ticker,
                    )
                elif pair_types == {LegType.CORNERS_TEAM, LegType.SPREAD}:
                    # team corners × spread: −ρ if the corners team COVERS the
                    # margin (chasing team earns corners), +ρ if the OPPONENT
                    # covers. Strength-controlled (raw pooled is the wrong sign).
                    cts_index = i if types[i] is LegType.CORNERS_TEAM else j
                    spr2_index = j if cts_index == i else i
                    prior = _corners_spread_prior(
                        key,
                        sport,
                        params,
                        legs[cts_index].market_ticker,
                        legs[spr2_index].market_ticker,
                    )
                elif pair_types == {
                    LegType.CORNERS_TEAM,
                    LegType.FIRST_HALF_MONEYLINE,
                }:
                    # team corners × 1H-winner (M1 2026-07-12): same shape as
                    # the FT winner pair — the chasing team earns corners
                    # (:same −0.20 / :opp +0.23 / :tie ~0, 1H stronger than
                    # FT). The 1H-winner suffix is the same TEAM-code-or-TIE
                    # shape, so the FT resolver routes it unchanged.
                    ct2_i = i if types[i] is LegType.CORNERS_TEAM else j
                    fh3_i = j if ct2_i == i else i
                    prior = _corners_winner_prior(
                        key,
                        sport,
                        params,
                        legs[ct2_i].market_ticker,
                        legs[fh3_i].market_ticker,
                    )
                elif pair_types == {
                    LegType.CORNERS_TEAM,
                    LegType.FIRST_HALF_SPREAD,
                }:
                    # team corners × 1H-spread (M1 2026-07-12): sibling of the
                    # FT spread pair (:same −0.18 / :opp +0.15); the 1H-spread
                    # suffix is the same TEAM+digits shape.
                    ct3_i = i if types[i] is LegType.CORNERS_TEAM else j
                    fhs2_i = j if ct3_i == i else i
                    prior = _corners_spread_prior(
                        key,
                        sport,
                        params,
                        legs[ct3_i].market_ticker,
                        legs[fhs2_i].market_ticker,
                    )
                elif pair_types == {LegType.ADVANCE, LegType.CORNERS}:
                    # advance × TOTAL corners (M1 2026-07-12): a STRENGTH
                    # CURVE keyed on the ADVANCE leg's marginal (dog +0.23 ↔
                    # fav −0.23 — a drawn-90 forces ET and corners settle
                    # incl ET), the btts|moneyline machinery on a non-ML
                    # marginal. Without marginals the plain 0.00 scalar
                    # (band 0.25 spans the curve) applies via the fallthrough.
                    if marginals is not None:
                        adv2_i = i if types[i] is LegType.ADVANCE else j
                        prior = _oriented_curve_prior(
                            key, sport, params, marginals[adv2_i]
                        )
                elif pair_types == {LegType.ADVANCE, LegType.CORNERS_TEAM}:
                    # advance × TEAM corners (M1 2026-07-12): −ρ when the
                    # corners team IS the advancing team (chasing-team corners
                    # + the ET boost), +ρ for the opponent — derived via the
                    # advance=win90+q·draw90 bridge, KO-validated. Advance
                    # never names a draw, so no tie branch.
                    adv3_i = i if types[i] is LegType.ADVANCE else j
                    ct4_i = j if adv3_i == i else i
                    prior = _oriented_team_prior(
                        key,
                        sport,
                        params,
                        _winner_team(legs[adv3_i].market_ticker),
                        _corners_team_name(legs[ct4_i].market_ticker),
                        is_tie=False,
                    )
                elif pair_types == {LegType.CORNERS_TEAM, LegType.PLAYER_GOAL}:
                    # team corners × scorer (M1 2026-07-12): SIGN FLIP vs the
                    # old +0.05 folk prior — the star scores → his team leads
                    # → its corners are SUPPRESSED (:same −0.14 / :opp +0.11,
                    # strength-controlled). The corners_team suffix is the
                    # same TEAM+digits shape a spread leg carries, so the
                    # spread×scorer resolver routes it unchanged.
                    ct5_i = i if types[i] is LegType.CORNERS_TEAM else j
                    pg2_i = j if ct5_i == i else i
                    prior = _spread_player_prior(
                        key,
                        sport,
                        params,
                        legs[ct5_i].market_ticker,
                        legs[pg2_i].market_ticker,
                    )
                elif pair_types == {LegType.ADVANCE, LegType.FIRST_HALF_MONEYLINE}:
                    # advance × 1H-winner: +ρ if the 1H leader advances, −ρ if the
                    # opponent, ~0 on a 1H draw (direction-only vs symmetric).
                    a_i = i if types[i] is LegType.ADVANCE else j
                    f_i = j if a_i == i else i
                    sfx = legs[f_i].market_ticker.rsplit("-", 1)[-1].upper()
                    prior = _oriented_team_prior(
                        key, sport, params,
                        _winner_team(legs[a_i].market_ticker),
                        _winner_team(legs[f_i].market_ticker),
                        is_tie=sfx in _DRAW_SUFFIXES,
                    )
                elif pair_types == {LegType.FIRST_HALF_MONEYLINE, LegType.TOTAL}:
                    # 1H-winner × FT-total: :team (a lead ⇒ goals) / :tie.
                    f_i = i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    prior = _period_total_prior(key, sport, params, legs[f_i].market_ticker)
                elif pair_types == {LegType.BTTS, LegType.FIRST_HALF_MONEYLINE}:
                    # FT-btts × 1H-winner: :team (POSITIVE — a lead = a goal
                    # already happened) / :tie (1H draw is 1-1 = btts).
                    f_i = i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    prior = _period_total_prior(key, sport, params, legs[f_i].market_ticker)
                elif pair_types == {LegType.FIRST_HALF_BTTS, LegType.FIRST_HALF_MONEYLINE}:
                    # 1H-btts × 1H-winner: :team / :tie.
                    f_i = i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    prior = _period_total_prior(key, sport, params, legs[f_i].market_ticker)
                elif pair_types == {LegType.FIRST_HALF_MONEYLINE, LegType.PLAYER_GOAL}:
                    # 1H-winner × scorer: :same/:opp by scorer's team / :tie.
                    f_i = i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    p_i = j if f_i == i else i
                    prior = _period_winner_player_prior(
                        key, sport, params,
                        legs[f_i].market_ticker, legs[p_i].market_ticker,
                    )
                elif pair_types == {LegType.FIRST_HALF_MONEYLINE, LegType.SPREAD}:
                    # 1H-winner × FT-spread: :same/:opp by team / :tie.
                    f_i = i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    s_i = j if f_i == i else i
                    sfx = legs[f_i].market_ticker.rsplit("-", 1)[-1].upper()
                    prior = _oriented_team_prior(
                        key, sport, params,
                        _winner_team(legs[f_i].market_ticker),
                        _spread_team(legs[s_i].market_ticker),
                        is_tie=sfx in _DRAW_SUFFIXES,
                    )
                elif pair_types == {LegType.FIRST_HALF_MONEYLINE, LegType.FIRST_HALF_SPREAD}:
                    # 1H-winner × 1H-spread: same-team lead ⊃ lead-by-2 (+); opp or
                    # tie EXCLUDES it (−). :same/:opp/:tie.
                    f_i = i if types[i] is LegType.FIRST_HALF_MONEYLINE else j
                    s_i = j if f_i == i else i
                    sfx = legs[f_i].market_ticker.rsplit("-", 1)[-1].upper()
                    prior = _oriented_team_prior(
                        key, sport, params,
                        _winner_team(legs[f_i].market_ticker),
                        _spread_team(legs[s_i].market_ticker),
                        is_tie=sfx in _DRAW_SUFFIXES,
                    )
                elif pair_types == {LegType.ADVANCE, LegType.FIRST_HALF_SPREAD}:
                    # advance × 1H-spread: +ρ if the 1H lead-by-2 is the advancing
                    # team, −ρ if the opponent (advance never draws).
                    a_i = i if types[i] is LegType.ADVANCE else j
                    s_i = j if a_i == i else i
                    prior = _oriented_team_prior(
                        key, sport, params,
                        _winner_team(legs[a_i].market_ticker),
                        _spread_team(legs[s_i].market_ticker),
                        is_tie=False,
                    )
                elif pair_types == {LegType.FIRST_HALF_SPREAD, LegType.PLAYER_GOAL}:
                    # 1H-spread × scorer: :same/:opp by scorer's team (reuse the FT
                    # spread×scorer resolver; 1H-spread suffix is TEAM+digits too).
                    s_i = i if types[i] is LegType.FIRST_HALF_SPREAD else j
                    p_i = j if s_i == i else i
                    prior = _spread_player_prior(
                        key, sport, params,
                        legs[s_i].market_ticker, legs[p_i].market_ticker,
                    )
                elif pair_types == {LegType.ADVANCE, LegType.MONEYLINE}:
                    # FT advance × regulation moneyline. Team cases are logical
                    # containment/impossible (relationships.py intercepts before the
                    # copula); only advance × regulation-DRAW reaches here → :tie ~0.
                    m_i = i if types[i] is LegType.MONEYLINE else j
                    if legs[m_i].market_ticker.rsplit("-", 1)[-1].upper() in _DRAW_SUFFIXES:
                        prior = _lookup_pair(f"{key}:tie", sport, params)
                elif (
                    types[i] in _MLB_PLAYER_PROP_TYPES
                    and types[j] in _MLB_PLAYER_PROP_TYPES
                ):
                    # SAME-PLAYER cross-stat first (the routing resolver's
                    # None seam): measured conditional cells price via the
                    # implied rho; exact/unmeasured same-player shapes are
                    # owned by relationships.py (containment / UNKNOWN).
                    if marginals is not None:
                        prior = _mlb_same_player_conditional_prior(
                            types[i], types[j],
                            legs[i].market_ticker, legs[j].market_ticker,
                            marginals[i], marginals[j],
                        )
                    # MLB prop × prop: teammate vs opponent stacking; batter
                    # prop × the OPPOSING starter's Ks is the FACING case
                    # (:opp carries the negative). Same-player cross-family is
                    # containment-shaped -> None -> plain (containment phase).
                    if prior is None:
                        prior = _mlb_prop_pair_prior(
                            key, sport, params,
                            legs[i].market_ticker, legs[j].market_ticker,
                        )
                elif (
                    pair_types == {LegType.MONEYLINE, LegType.SPREAD}
                    and sport == str(Sport.MLB)
                ):
                    # MLB winner × run line (DO-3): containment-shaped ±0.95
                    # by side. relationships.py intercepts the containment /
                    # impossible shapes before any copula; only cases like
                    # win-yes × cover-no reach here. MLB-gated so NFL (0.88)
                    # and soccer ml|spread behavior is untouched.
                    ml2_i = i if types[i] is LegType.MONEYLINE else j
                    sp2_i = j if ml2_i == i else i
                    prior = _mlb_winner_spread_prior(
                        key, sport, params,
                        legs[ml2_i].market_ticker, legs[sp2_i].market_ticker,
                    )
                elif (
                    LegType.MONEYLINE in pair_types
                    and pair_types & _MLB_PLAYER_PROP_TYPES
                ):
                    # MLB winner × player prop: sign flips on whether the prop
                    # player's team IS the ML YES team. Must intercept BEFORE
                    # the generic one-moneyline fav/dog axis (wrong axis here).
                    ml_i = i if types[i] is LegType.MONEYLINE else j
                    pr_i = j if ml_i == i else i
                    prior = _mlb_winner_prop_prior(
                        key, sport, params,
                        legs[ml_i].market_ticker, legs[pr_i].market_ticker,
                    )
                elif (
                    LegType.SPREAD in pair_types
                    and pair_types & _MLB_PLAYER_PROP_TYPES
                ):
                    # MLB run-line spread × player prop: spread is team-signed
                    # like the moneyline, so the sign flips on whether the
                    # prop player's team IS the spread YES team; the rung
                    # chain keys the line ints (':same:r1:r2'). Prop types
                    # only classify from KXMLB series, so no sport gate is
                    # needed (mirrors the winner × prop branch above).
                    sp3_i = i if types[i] is LegType.SPREAD else j
                    pp_i = j if sp3_i == i else i
                    prior = _mlb_spread_prop_prior(
                        key, sport, params,
                        legs[sp3_i].market_ticker, legs[pp_i].market_ticker,
                    )
                else:
                    one_moneyline = (types[i] is LegType.MONEYLINE) != (
                        types[j] is LegType.MONEYLINE
                    )
                    if one_moneyline and marginals is not None:
                        ml_index = i if types[i] is LegType.MONEYLINE else j
                        # Curve first (monotone win-prob dependence), else the
                        # fav/dog 2-anchor blend, else the plain lookup below.
                        prior = _oriented_curve_prior(
                            key, sport, params, marginals[ml_index]
                        ) or _oriented_prior(key, sport, params, marginals[ml_index])
                if prior is None:
                    # Plain level of the rung chain. An ORIENTATION-FREE
                    # rung-keyed pair (prop × total, prop × rfi) wires its
                    # exact rung key directly onto the plain base
                    # ('player_hr|total:r2'); orientable pairs that resolved
                    # no oriented entry simply miss here and land on the
                    # plain key. MLB-gated: ':rN' is a wire-list / mlb-table
                    # grammar — other sports' spread lines stay un-runged.
                    rung = (
                        _pair_rung_suffix(
                            legs[i].market_ticker, legs[j].market_ticker
                        )
                        if sport == str(Sport.MLB)
                        else ""
                    )
                    prior = _lookup_pair_runged(key, sport, params, rung)
                if prior is not None:
                    rho, band = prior.rho, prior.band
                    typed += 1
                    if prior.source != key:  # plain global hits stay silent
                        notes.append(f"pair {prior.source}={rho:+.3f}")
                else:
                    rho, band = params.default_rho, fallback_band
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
