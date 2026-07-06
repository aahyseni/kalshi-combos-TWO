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
from dataclasses import dataclass
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
class PlayerScores:
    """YES = the player scores >= min_goals. ``share_index`` links the leg to
    its thinning parameter (assigned by the inverter, in leg order)."""

    team: Team
    min_goals: int = 1
    include_et: bool = True


LegSpec = TeamWin | Advance | Draw | Btts | TotalOver | PlayerScores

_TEAM_LEVEL = (TeamWin, Advance, Draw, Btts, TotalOver)


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


@dataclass(frozen=True, slots=True)
class _States:
    """Flat weighted enumeration of terminal match states."""

    w: _FloatArray        # state probability
    a90: NDArray[np.int64]
    b90: NDArray[np.int64]
    a_et: NDArray[np.int64]  # zeros for group format / non-draw states
    b_et: NDArray[np.int64]


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


# The hot path re-evaluates the same params many times (every constraint in a
# least-squares iteration, every brentq step of a player-share solve, every
# uncertainty probe): memoize the enumeration. Params are frozen/hashable and
# distinct optimizer iterates simply miss — reuse WITHIN an iterate is the win.
@lru_cache(maxsize=64)
def _states(params: ModelParams) -> _States:
    g = params.max_goals
    grid90 = _dc_grid(params.lam_a, params.lam_b, params.dc_rho, g)
    idx = np.arange(g + 1)
    a90, b90 = np.meshgrid(idx, idx, indexing="ij")

    if params.match_format is MatchFormat.GROUP:
        return _States(
            w=grid90.ravel(),
            a90=a90.ravel(),
            b90=b90.ravel(),
            a_et=np.zeros((g + 1) ** 2, dtype=np.int64),
            b_et=np.zeros((g + 1) ** 2, dtype=np.int64),
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
    )


def _team_goals(states: _States, team: Team, include_et: bool) -> NDArray[np.int64]:
    g90 = states.a90 if team is Team.A else states.b90
    if not include_et:
        return g90
    return g90 + (states.a_et if team is Team.A else states.b_et)


def _team_indicator(
    states: _States, spec: TeamWin | Advance | Draw | Btts | TotalOver, params: ModelParams
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
    a = _team_goals(states, Team.A, spec.include_et)
    b = _team_goals(states, Team.B, spec.include_et)
    return np.asarray((a + b) >= spec.min_total, dtype=np.float64)


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
    """P(every leg settles on its selected side) off the scoreline model."""
    states = _states(params)
    factor = states.w.copy()
    by_team: dict[Team, list[tuple[PlayerScores, float, bool]]] = {}
    for i, (spec, yes) in enumerate(legs):
        if isinstance(spec, PlayerScores):
            by_team.setdefault(spec.team, []).append((spec, shares[i], yes))
            continue
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

    def make_params(x: NDArray[np.float64]) -> ModelParams:
        return ModelParams(
            lam_a=float(np.exp(x[0])),
            lam_b=float(np.exp(x[1])),
            dc_rho=dc_rho,
            et_factor=et_factor,
            match_format=match_format,
            max_goals=max_goals,
            pens_win_a=pens_win_a,
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
