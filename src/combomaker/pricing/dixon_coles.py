"""Dixon-Coles scoreline model for soccer same-game parlays (structural v2).

Instead of gluing leg marginals together with pairwise copula priors, invert a
low-dimensional scoreline model FROM the live leg prices themselves:

  goals_A ~ Poisson(lam_a), goals_B ~ Poisson(lam_b) over 90', with the
  Dixon-Coles low-score adjustment tau (excess low draws); in knockout
  formats a drawn 90' is followed by extra time at ``et_factor`` intensity.
  Player scoring is multinomial thinning: each team goal belongs to player p
  with probability q_p, so player goals | n team goals ~ Binomial(n, q_p).

The market's own prices pin the parameters: team-level legs (win/draw/BTTS/
totals) identify (lam_a, lam_b) — two constraints solve exactly, more are fit
least-squares and the residual misfit is PRICED into uncertainty — and each
player leg pins its own share q from that player's market price. The joint of
all selected legs is then read directly off the coherent scoreline x scorer
distribution: every pairwise AND higher-order correlation (star scorer x win,
dog win x BTTS sign flip, three-way interactions) falls out of the structure
instead of a hand-maintained rho table, and Frechet bounds hold by
construction.

Everything here is pure probability math on explicit inputs — no Kalshi wire
types, no conventions, no ticker knowledge (the adapter in
``pricing/structural.py`` owns that). Uncertainty is priced, not hoped away:
marginal bands propagate by re-inversion, model-form risk (ET intensity, DC
rho, settlement windows) by re-pricing under perturbed assumptions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import lru_cache
from itertools import combinations

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import brentq, least_squares
from scipy.stats import binom, poisson

_FloatArray = NDArray[np.float64]

_LAM_MIN, _LAM_MAX = 0.05, 6.0
_MAX_TEAM_SHARE = 0.95  # sum of player shares per team must stay identifiable


class Team(StrEnum):
    A = "a"
    B = "b"


class MatchFormat(StrEnum):
    """GROUP: 90' only. KNOCKOUT: drawn 90' plays extra time (pens excluded —
    a market that says "win in 90+ET" settles NO on a shootout win)."""

    GROUP = "group"
    KNOCKOUT = "knockout"


# --- leg specs (YES-side events; NO sides are complements at evaluation) -------


@dataclass(frozen=True, slots=True)
class TeamWin:
    team: Team
    include_et: bool = True  # False = "win in 90'" style markets


@dataclass(frozen=True, slots=True)
class Advance:
    """YES = the team advances: win in 90', in ET, or on penalties. Kalshi's
    knockout game market settles this way (rules text 2026-07-06). The
    shootout is not part of the scoreline state — states that stay level
    through ET contribute fractionally via ``ModelParams.pens_win_a``."""

    team: Team


@dataclass(frozen=True, slots=True)
class Draw:
    """Match level after regulation (the 3-way TIE market)."""


@dataclass(frozen=True, slots=True)
class Btts:
    include_et: bool = True


@dataclass(frozen=True, slots=True)
class TotalOver:
    """YES = combined goals >= min_total (an over-2.5 market has min_total=3)."""

    min_total: int
    include_et: bool = True


@dataclass(frozen=True, slots=True)
class GoalSpread:
    """YES = ``team`` wins by a goal margin >= min_margin. Kalshi soccer spread
    "wins by over 1.5" is min_margin=2 (integer margin > n-0.5). A 1-goal margin
    IS a win, so min_margin=1 equals a regulation TeamWin at include_et=False.
    Regulation-time market by rule -> include_et defaults False. Distinct name
    from margin_total.SpreadCover (that's the NFL/NBA normal-model spec)."""

    team: Team
    min_margin: int
    include_et: bool = False


@dataclass(frozen=True, slots=True)
class PlayerScores:
    """YES = the player scores >= min_goals. ``share_index`` links the leg to
    its thinning parameter (assigned by the inverter, in leg order)."""

    team: Team
    min_goals: int = 1
    include_et: bool = True


# --- first-half (1H) leg specs -------------------------------------------------
# These read the FIRST-HALF sub-scoreline (goals through 45'), never the full or
# ET scoreline. FT = 1H + 2H is one coherent grid (see ``_states_with_halves``),
# so a mixed 1H+FT selection's joint is a single sum over that grid — every 1H×FT
# correlation falls out of the structure, not a pairwise copula prior. 1H legs
# settle at half-time and are untouched by extra time (ET is a 2H-and-beyond
# increment, design_halftime_dc.md §3.4), so they carry no ``include_et``.


@dataclass(frozen=True, slots=True)
class HalfResult:
    """YES = ``team`` LEADS at half-time (1H goals: a_1h > b_1h for A)."""

    team: Team


@dataclass(frozen=True, slots=True)
class HalfDraw:
    """YES = level at half-time (a_1h == b_1h) — the 1H three-way TIE."""


@dataclass(frozen=True, slots=True)
class HalfTotalOver:
    """YES = combined 1H goals >= min_total (1H over-0.5 == min_total=1)."""

    min_total: int


@dataclass(frozen=True, slots=True)
class HalfBtts:
    """YES = both teams score in the first half (a_1h >= 1 and b_1h >= 1)."""


@dataclass(frozen=True, slots=True)
class HalfGoalSpread:
    """YES = ``team`` leads at half by a 1H goal margin >= min_margin. Kalshi's
    KXWC1HSPREAD ``…-<TEAM>2`` = "leads at half by over 1.5" -> min_margin=2."""

    team: Team
    min_margin: int


LegSpec = (
    TeamWin
    | Advance
    | Draw
    | Btts
    | TotalOver
    | GoalSpread
    | PlayerScores
    | HalfResult
    | HalfDraw
    | HalfTotalOver
    | HalfBtts
    | HalfGoalSpread
)

# 1H team-level constraints. With ``half_share`` (h) a banded CONSTANT (never
# inverted from a single leg — design §6), a 1H marginal is a deterministic
# function of (lam_a, lam_b), so 1H legs are usable identification constraints
# on (lam_a, lam_b) just like their FT siblings.
_HALF_LEVEL = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)

_TEAM_LEVEL = (
    TeamWin,
    Advance,
    Draw,
    Btts,
    TotalOver,
    GoalSpread,
    *_HALF_LEVEL,
)


class StructuralError(ValueError):
    """The scoreline model cannot represent / identify this combo — the
    caller must fall back to the copula path, never guess."""


# --- terminal-state enumeration -------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelParams:
    lam_a: float
    lam_b: float
    dc_rho: float
    et_factor: float
    match_format: MatchFormat
    max_goals: int = 12
    # P(team A wins a shootout | still level after ET). Advance legs only;
    # penalties never contribute goals to any other market (Kalshi rules).
    pens_win_a: float = 0.5
    # First-half goal share h: goals_1H ~ Poisson(lam*h), goals_2H ~
    # Poisson(lam*(1-h)) per team (design_halftime_dc.md §1-3). Banded CONSTANT,
    # never inverted from a single 1H leg (§6). Only read when ``with_halves``.
    half_share: float = 0.45
    # Build the 4-D half-aware enumeration (FT = 1H + 2H). Lazy: set True only
    # when a combo carries a 1H leg (joint_probability auto-upgrades). FT-only
    # combos keep the untouched 2-D fast path — bit-for-bit unchanged (§9).
    with_halves: bool = False


# Sentinel for the FT-only enumeration: no 1H sub-state is populated, so any 1H
# indicator that reads it MUST raise (honest failure, never a silent zero).
_NO_HALF = -1


@dataclass(frozen=True, slots=True)
class _States:
    """Flat weighted enumeration of terminal match states."""

    w: _FloatArray        # state probability
    a90: NDArray[np.int64]
    b90: NDArray[np.int64]
    a_et: NDArray[np.int64]  # zeros for group format / non-draw states
    b_et: NDArray[np.int64]
    # First-half goals per team (design §4.1). Filled only in the 4-D half-aware
    # enumeration; the FT-only path fills them with ``_NO_HALF`` sentinels so a
    # stray 1H indicator fails closed rather than reading a wrong zero.
    a_1h: NDArray[np.int64]
    b_1h: NDArray[np.int64]


def _dc_grid(lam_a: float, lam_b: float, dc_rho: float, max_goals: int) -> _FloatArray:
    """90' scoreline grid with the Dixon-Coles tau adjustment, renormalized."""
    a = poisson.pmf(np.arange(max_goals + 1), lam_a)
    b = poisson.pmf(np.arange(max_goals + 1), lam_b)
    grid = np.outer(a, b)
    if dc_rho != 0.0:
        grid = grid.copy()
        grid[0, 0] *= 1.0 - lam_a * lam_b * dc_rho
        grid[0, 1] *= 1.0 + lam_a * dc_rho
        grid[1, 0] *= 1.0 + lam_b * dc_rho
        grid[1, 1] *= 1.0 - dc_rho
        grid = np.clip(grid, 0.0, None)
    total = float(grid.sum())
    if not total > 0.0:
        raise StructuralError(f"degenerate scoreline grid (lam={lam_a},{lam_b})")
    return np.asarray(grid / total, dtype=np.float64)


@lru_cache(maxsize=8)
def _half_split_table(max_goals: int, half_share: float) -> _FloatArray:
    """``B[m, k]`` = P(1H goals = k | 90' goals = m) = Binomial(k; m, h).

    Poisson SPLITTING (design §1.2): with goals_1H ~ Poisson(lam·h) and
    goals_2H ~ Poisson(lam·(1-h)) independent, then conditional on the 90'
    total m, the 1H count is Binomial(m, h) — independent of lam, and the DC
    tau (a per-cell constant) cancels in the conditional (§3.3). Lower
    triangular (B[m, k] = 0 for k > m)."""
    ks = np.arange(max_goals + 1)
    rows = [np.asarray(binom.pmf(ks, m, half_share), dtype=np.float64)
            for m in range(max_goals + 1)]
    return np.array(rows, dtype=np.float64)


# The hot path re-evaluates the same params many times (every constraint in a
# least-squares iteration, every brentq step of a player-share solve, every
# uncertainty probe): memoize the enumeration. Params are frozen/hashable and
# distinct optimizer iterates simply miss — reuse WITHIN an iterate is the win.
@lru_cache(maxsize=64)
def _states(params: ModelParams) -> _States:
    if params.with_halves:
        return _states_with_halves(params)
    g = params.max_goals
    grid90 = _dc_grid(params.lam_a, params.lam_b, params.dc_rho, g)
    idx = np.arange(g + 1)
    a90, b90 = np.meshgrid(idx, idx, indexing="ij")

    if params.match_format is MatchFormat.GROUP:
        size = (g + 1) ** 2
        sentinel = np.full(size, _NO_HALF, dtype=np.int64)
        return _States(
            w=grid90.ravel(),
            a90=a90.ravel(),
            b90=b90.ravel(),
            a_et=np.zeros(size, dtype=np.int64),
            b_et=np.zeros(size, dtype=np.int64),
            a_1h=sentinel,
            b_1h=sentinel,
        )

    # Knockout: non-draw 90' states terminate; each drawn state fans out over
    # an ET grid (plain Poisson at reduced intensity — DC is a full-match
    # low-score effect and its ET share is inside the model-form band).
    nd_mask = a90 != b90
    et_max = max(4, g // 2)
    et_a = poisson.pmf(np.arange(et_max + 1), params.lam_a * params.et_factor)
    et_b = poisson.pmf(np.arange(et_max + 1), params.lam_b * params.et_factor)
    et_grid = np.outer(et_a, et_b)
    et_grid /= et_grid.sum()
    ea, eb = np.meshgrid(np.arange(et_max + 1), np.arange(et_max + 1), indexing="ij")

    draws = np.arange(g + 1)
    draw_w = np.repeat(grid90[draws, draws], et_grid.size) * np.tile(
        et_grid.ravel(), g + 1
    )
    size = int(nd_mask.sum()) + (g + 1) * et_grid.size
    sentinel = np.full(size, _NO_HALF, dtype=np.int64)
    return _States(
        w=np.concatenate([grid90[nd_mask], draw_w]),
        a90=np.concatenate([a90[nd_mask], np.repeat(draws, et_grid.size)]),
        b90=np.concatenate([b90[nd_mask], np.repeat(draws, et_grid.size)]),
        a_et=np.concatenate(
            [np.zeros(int(nd_mask.sum()), dtype=np.int64), np.tile(ea.ravel(), g + 1)]
        ),
        b_et=np.concatenate(
            [np.zeros(int(nd_mask.sum()), dtype=np.int64), np.tile(eb.ravel(), g + 1)]
        ),
        a_1h=sentinel,
        b_1h=sentinel,
    )


def _states_with_halves(params: ModelParams) -> _States:
    """Half-aware enumeration in FACTORED form: the exact FT enumeration (reused
    verbatim from the 2-D builder, incl. the knockout ET fan-out) EXPANDED over
    each state's 1H split (i1, j1) ~ Binomial(a90, h) x Binomial(b90, h).

    This is the design's 4-D joint written as P(FT cell)·P(1H split | cell) —
    algebraically identical, but it (a) preserves every FT leg to float
    precision (summing a state's 1H splits recovers its FT weight exactly) and
    (b) needs no per-half goal cap (splits run [0..m] fully, so the FT
    convolution is never truncated). ET is a 2H-and-beyond increment (§3.4): it
    rides on the FT state and never touches the 1H counts."""
    ft = _states(replace(params, with_halves=False))
    g = params.max_goals
    table = _half_split_table(g, params.half_share)  # B[m, k] = Binom(k; m, h)

    w_parts: list[_FloatArray] = []
    a90_parts: list[NDArray[np.int64]] = []
    b90_parts: list[NDArray[np.int64]] = []
    aet_parts: list[NDArray[np.int64]] = []
    bet_parts: list[NDArray[np.int64]] = []
    a1_parts: list[NDArray[np.int64]] = []
    b1_parts: list[NDArray[np.int64]] = []
    for s in range(ft.w.size):
        m = int(ft.a90[s])
        n = int(ft.b90[s])
        split = np.outer(table[m, : m + 1], table[n, : n + 1]).ravel()
        count = split.size
        w_parts.append(ft.w[s] * split)
        a90_parts.append(np.full(count, m, dtype=np.int64))
        b90_parts.append(np.full(count, n, dtype=np.int64))
        aet_parts.append(np.full(count, int(ft.a_et[s]), dtype=np.int64))
        bet_parts.append(np.full(count, int(ft.b_et[s]), dtype=np.int64))
        a1_parts.append(np.repeat(np.arange(m + 1, dtype=np.int64), n + 1))
        b1_parts.append(np.tile(np.arange(n + 1, dtype=np.int64), m + 1))
    return _States(
        w=np.concatenate(w_parts),
        a90=np.concatenate(a90_parts),
        b90=np.concatenate(b90_parts),
        a_et=np.concatenate(aet_parts),
        b_et=np.concatenate(bet_parts),
        a_1h=np.concatenate(a1_parts),
        b_1h=np.concatenate(b1_parts),
    )


def _team_goals(states: _States, team: Team, include_et: bool) -> NDArray[np.int64]:
    g90 = states.a90 if team is Team.A else states.b90
    if not include_et:
        return g90
    return g90 + (states.a_et if team is Team.A else states.b_et)


def _team_indicator(
    states: _States,
    spec: TeamWin | Advance | Draw | Btts | TotalOver | GoalSpread,
    params: ModelParams,
) -> _FloatArray:
    if isinstance(spec, (TeamWin, Advance)):
        us, them = (
            (states.a90, states.b90) if spec.team is Team.A else (states.b90, states.a90)
        )
        win90 = us > them
        if isinstance(spec, TeamWin) and not spec.include_et:
            return np.asarray(win90, dtype=np.float64)
        us_et = states.a_et if spec.team is Team.A else states.b_et
        them_et = states.b_et if spec.team is Team.A else states.a_et
        win_et = (us == them) & (us_et > them_et)
        if isinstance(spec, TeamWin):
            return np.asarray(win90 | win_et, dtype=np.float64)
        if params.match_format is not MatchFormat.KNOCKOUT:
            raise StructuralError("advance leg in a non-knockout format")
        pens = params.pens_win_a if spec.team is Team.A else 1.0 - params.pens_win_a
        out = np.asarray(win90 | win_et, dtype=np.float64)
        # Level after ET: the shootout decides, fractionally — pens outcomes
        # are independent of every other market by rule, so a probability
        # factor per state is exact.
        out[(us == them) & (us_et == them_et)] = pens
        return out
    if isinstance(spec, Draw):
        return np.asarray(states.a90 == states.b90, dtype=np.float64)
    if isinstance(spec, Btts):
        a = _team_goals(states, Team.A, spec.include_et)
        b = _team_goals(states, Team.B, spec.include_et)
        return np.asarray((a >= 1) & (b >= 1), dtype=np.float64)
    if isinstance(spec, GoalSpread):
        us = _team_goals(states, spec.team, spec.include_et)
        them = _team_goals(
            states, Team.B if spec.team is Team.A else Team.A, spec.include_et
        )
        return np.asarray((us - them) >= spec.min_margin, dtype=np.float64)
    a = _team_goals(states, Team.A, spec.include_et)
    b = _team_goals(states, Team.B, spec.include_et)
    return np.asarray((a + b) >= spec.min_total, dtype=np.float64)


def _half_indicator(
    states: _States,
    spec: HalfResult | HalfDraw | HalfTotalOver | HalfBtts | HalfGoalSpread,
) -> _FloatArray:
    """YES indicator for a 1H leg off the first-half sub-scoreline. Asserts the
    half state is populated (never the FT-only sentinel) so a stray 1H leg on a
    2-D enumeration fails closed instead of reading a wrong zero."""
    if states.a_1h.size == 0 or states.a_1h[0] == _NO_HALF:
        raise StructuralError("1H leg needs the half-time enumeration")
    a1, b1 = states.a_1h, states.b_1h
    if isinstance(spec, HalfResult):
        us, them = (a1, b1) if spec.team is Team.A else (b1, a1)
        return np.asarray(us > them, dtype=np.float64)
    if isinstance(spec, HalfDraw):
        return np.asarray(a1 == b1, dtype=np.float64)
    if isinstance(spec, HalfBtts):
        return np.asarray((a1 >= 1) & (b1 >= 1), dtype=np.float64)
    if isinstance(spec, HalfGoalSpread):
        us, them = (a1, b1) if spec.team is Team.A else (b1, a1)
        return np.asarray((us - them) >= spec.min_margin, dtype=np.float64)
    return np.asarray((a1 + b1) >= spec.min_total, dtype=np.float64)


def _player_group_factor(
    states: _States,
    players: list[tuple[PlayerScores, float, bool]],  # (spec, share, selected_yes)
    team: Team,
) -> _FloatArray:
    """P(each player leg lands on its selected side | state), one team.

    Multinomial thinning: conditional on n team goals, any set S of players
    is jointly blank with probability (1 - sum(q_S))^n — inclusion-exclusion
    then prices any mix of 1+ YES/NO legs exactly. A lone player leg supports
    arbitrary min_goals via the Binomial tail; several legs on one team
    require min_goals == 1 (the adapter falls back otherwise).
    """
    if len(players) == 1:
        spec, share, yes = players[0]
        n = _team_goals(states, team, spec.include_et)
        if spec.min_goals == 1:  # fast path: P(X>=1 | n) = 1 - (1-q)^n
            p_ge = 1.0 - np.power(1.0 - share, n)
        else:
            p_ge = np.asarray(binom.sf(spec.min_goals - 1, n, share), dtype=np.float64)
        return p_ge if yes else 1.0 - p_ge
    if any(spec.min_goals != 1 for spec, _, _ in players):
        raise StructuralError("multiple player legs on one team require min_goals=1")
    if any(p[0].include_et != players[0][0].include_et for p in players):
        raise StructuralError("mixed player settlement windows on one team")
    n = _team_goals(states, team, players[0][0].include_et)
    total_share = sum(share for _, share, _ in players)
    if total_share > _MAX_TEAM_SHARE:
        raise StructuralError(f"player shares sum to {total_share:.2f} — not identifiable")
    yes_shares = [share for _, share, yes in players if yes]
    no_share = sum(share for _, share, yes in players if not yes)
    # P(all NO-legs blank AND all YES-legs score) via inclusion-exclusion over
    # subsets T of the YES set: sum (-1)^|T| (1 - no_share - sum q_T)^n.
    out = np.zeros(len(n), dtype=np.float64)
    for r in range(len(yes_shares) + 1):
        for subset in combinations(yes_shares, r):
            blank = 1.0 - no_share - sum(subset)
            out += (-1.0) ** r * np.power(blank, n)
    return np.clip(out, 0.0, 1.0)


def joint_probability(
    params: ModelParams,
    legs: list[tuple[LegSpec, bool]],  # (spec, selected side is YES)
    shares: dict[int, float],  # leg index -> thinning share for PlayerScores legs
) -> float:
    """P(every leg settles on its selected side) off the scoreline model.

    Lazy half gating (design §9): a combo carrying any 1H leg builds the 4-D
    half-aware enumeration; an FT-only combo keeps the untouched 2-D fast path.
    The joint of a mixed 1H+FT selection is one sum over the shared grid, so
    every 1H×FT correlation is exact (no pairwise rho)."""
    if any(isinstance(spec, _HALF_LEVEL) for spec, _ in legs) and not params.with_halves:
        params = replace(params, with_halves=True)
    states = _states(params)
    factor = states.w.copy()
    by_team: dict[Team, list[tuple[PlayerScores, float, bool]]] = {}
    for i, (spec, yes) in enumerate(legs):
        if isinstance(spec, PlayerScores):
            by_team.setdefault(spec.team, []).append((spec, shares[i], yes))
            continue
        if isinstance(spec, _HALF_LEVEL):
            ind = _half_indicator(states, spec)
        else:
            ind = _team_indicator(states, spec, params)
        factor *= ind if yes else 1.0 - ind
    for team, players in by_team.items():
        factor *= _player_group_factor(states, players, team)
    return float(factor.sum())


def marginal_probability(
    params: ModelParams, spec: LegSpec, share: float | None = None
) -> float:
    """Model probability of a single YES-side leg."""
    if isinstance(spec, PlayerScores):
        if share is None:
            raise StructuralError("player marginal needs a share")
        return joint_probability(params, [(spec, True)], {0: share})
    return joint_probability(params, [(spec, True)], {})


# --- inversion ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InvertedModel:
    params: ModelParams
    shares: dict[int, float]      # leg index -> player share
    residual: float               # max |model - market| over team constraints
    notes: tuple[str, ...]


def invert(
    legs: list[tuple[LegSpec, float]],  # (spec, YES-side market marginal)
    *,
    dc_rho: float,
    et_factor: float,
    match_format: MatchFormat,
    max_goals: int = 12,
    pens_win_a: float = 0.5,
    half_share: float = 0.45,  # first-half goal share h (banded constant, §6)
    warm_start: tuple[float, float] | None = None,  # (lam_a, lam_b) guess
) -> InvertedModel:
    """Solve (lam_a, lam_b) from the team-level legs, then one thinning share
    per player leg. Raises StructuralError when unidentified or infeasible.

    Two team constraints solve exactly; more are least-squares and the
    residual misfit is reported (the caller prices it into width). Player
    constraints are always exactly identified given the lams.
    """
    team_constraints = [
        (spec, p) for spec, p in legs if isinstance(spec, _TEAM_LEVEL)
    ]
    for spec, p in legs:
        if not 0.001 <= p <= 0.999:
            raise StructuralError(f"marginal {p} out of invertible range for {spec}")
    if len(team_constraints) < 2:
        raise StructuralError(
            f"{len(team_constraints)} team-level legs cannot identify (lam_a, lam_b)"
        )

    # Orientation identifiability (audit #2, 2026-07-07). The team-level solve
    # pins (lam_a, lam_b) only as an UNORDERED pair: Btts / Draw / TotalOver are
    # all symmetric under swapping the two teams, so ONLY a TeamWin/Advance leg
    # NAMES a team and fixes which lam is which. A PlayerScores leg's marginal
    # re-fits at either orientation (its share is free), but its JOINT with the
    # other legs reads the scoreline and differs by orientation — an arbitrary
    # ~5-11c mispricing none of the width channels capture (false confidence).
    # Without an orienting leg the orientation is genuinely unidentified, so
    # decline ANY scorer combo to the copula (which prices the pairs orientation-
    # free). NOTE: scorers on BOTH teams do NOT rescue this — the selected joint
    # is orientation-invariant only for all-YES / symmetric selections (a
    # coincidence); a mixed-side or asymmetric selection diverges ~11c
    # (adversarial audit: 26JUL05 ARSTOT vs TOTARS priced 9.6c vs 20.2c on the
    # identical physical combo). We refuse to lean on that selection-dependent
    # cancellation.
    # A 1H leg that NAMES a team (HalfResult / HalfGoalSpread) fixes orientation
    # exactly like its FT sibling; HalfDraw / HalfTotalOver / HalfBtts are
    # team-symmetric and do NOT orient.
    scorer_present = any(isinstance(spec, PlayerScores) for spec, _ in legs)
    has_orienting = any(
        isinstance(spec, (TeamWin, Advance, GoalSpread, HalfResult, HalfGoalSpread))
        for spec, _ in legs
    )
    if scorer_present and not has_orienting:
        raise StructuralError(
            "player-scorer leg with only symmetric team constraints (no TeamWin/"
            "Advance): team orientation is unidentified — the scorer's rate would "
            "attach to an arbitrary mirror"
        )

    def make_params(x: NDArray[np.float64]) -> ModelParams:
        return ModelParams(
            lam_a=float(np.exp(x[0])),
            lam_b=float(np.exp(x[1])),
            dc_rho=dc_rho,
            et_factor=et_factor,
            match_format=match_format,
            max_goals=max_goals,
            pens_win_a=pens_win_a,
            half_share=half_share,
        )

    def residuals(x: NDArray[np.float64]) -> NDArray[np.float64]:
        p = make_params(x)
        return np.array(
            [marginal_probability(p, spec) - target for spec, target in team_constraints]
        )

    log_bounds = (math.log(_LAM_MIN), math.log(_LAM_MAX))
    start = warm_start or (1.3, 1.3)
    x0 = np.clip(
        np.log(np.array(start, dtype=np.float64)), log_bounds[0], log_bounds[1]
    )
    fit = least_squares(
        residuals,
        x0=x0,
        bounds=(np.full(2, log_bounds[0]), np.full(2, log_bounds[1])),
        xtol=1e-12,
        ftol=1e-12,
        gtol=1e-12,
    )
    params = make_params(np.asarray(fit.x))
    resid = np.abs(residuals(np.asarray(fit.x)))
    residual = float(resid.max())
    exactly_identified = len(team_constraints) == 2
    if exactly_identified and residual > 0.005:
        # Two constraints should solve to ~0; a real residual means the legs
        # contradict any Poisson scoreline (e.g. huge favorite + huge BTTS).
        raise StructuralError(f"inversion residual {residual:.4f} on exact system")

    shares: dict[int, float] = {}
    for i, (spec, target) in enumerate(legs):
        if not isinstance(spec, PlayerScores):
            continue
        ceiling = marginal_probability(params, spec, share=_MAX_TEAM_SHARE)
        if target >= ceiling:
            raise StructuralError(
                f"player marginal {target:.3f} infeasible (team ceiling {ceiling:.3f})"
            )

        def err(q: float, s: PlayerScores = spec, t: float = target) -> float:
            return marginal_probability(params, s, share=q) - t

        shares[i] = float(brentq(err, 1e-4, _MAX_TEAM_SHARE, xtol=1e-10))

    per_team: dict[Team, float] = {}
    for i, (spec, _) in enumerate(legs):
        if isinstance(spec, PlayerScores):
            per_team[spec.team] = per_team.get(spec.team, 0.0) + shares[i]
    for team, total in per_team.items():
        if total > _MAX_TEAM_SHARE:
            raise StructuralError(
                f"team {team} player shares sum to {total:.2f} — inconsistent legs"
            )

    notes = (
        f"dc inversion: lam_a={params.lam_a:.3f} lam_b={params.lam_b:.3f}"
        + (f" residual={residual:.4f}" if not exactly_identified else ""),
    )
    return InvertedModel(params=params, shares=shares, residual=residual, notes=notes)
