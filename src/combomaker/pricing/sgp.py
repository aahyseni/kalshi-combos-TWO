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
    the suffix isn't a team-code + optional digits shape (don't guess)."""
    return _corners_team_name(ticker)


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
    last segment) name the same team (a 1H lead → that team wins, +ρ) or opposite
    teams (near-mutually-exclusive, −ρ). Either suffix unparseable (spread not
    TEAM+digits, or a draw-side winner) → None so the caller falls back — never
    guess a sign."""
    team_s = _spread_team(spread_ticker)
    team_m = _winner_team(ml_ticker)
    if team_s is None or team_m is None:
        return None
    orient = "same" if team_s == team_m else "opp"
    return _lookup_pair(f"{key}:{orient}", sport, params)


def _winner_team(ticker: str) -> str | None:
    """The team code a (1H- or full-game) moneyline leg names — its ticker's
    last hyphen segment. None for a draw side, which has no measured 1H×FT
    orientation. Two same-game winner legs name the SAME team iff these strings
    match (both are drawn from the one game's team-code vocabulary)."""
    suffix = ticker.rsplit("-", 1)[-1].upper()
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


def _winner_period_prior(
    key: str, sport: str, params: SgpParams, ticker_a: str, ticker_b: str
) -> _PairPrior | None:
    """1H-winner × FT-winner prior, resolved to ``:same`` / ``:opp`` by whether
    the two winner legs name the same team (+ρ) or opposite teams (−ρ). This is
    the same/opposite analogue of ``_oriented_prior``'s fav/dog blend, but the
    choice is HARD (a sign flip), not a marginal-blended interpolation. A
    draw-involving pair is unmeasured → None (caller falls back to the flat
    prior; do not invent a number)."""
    team_a = _winner_team(ticker_a)
    team_b = _winner_team(ticker_b)
    if team_a is None or team_b is None:
        return None
    orient = "same" if team_a == team_b else "opp"
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
                    # 1H-winner × FT-winner: sign flips on same-vs-opposite team.
                    prior = _winner_period_prior(
                        key, sport, params, legs[i].market_ticker, legs[j].market_ticker
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
                prior = prior or _lookup_pair(key, sport, params)
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
