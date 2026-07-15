"""A1 — STRUCTURAL portfolio-risk sampling.

Sample ONE game outcome (scoreline + extra-time + first-half split + a shared
shootout coin + a shared per-team goal allocation) from the SAME Dixon-Coles state
enumeration that PRICES the game's legs, then settle EVERY leg against it. The
result is a ``(n, n_legs)`` YES-settlement value matrix that ``sim/engine.book_pnl``
consumes unchanged (the clean seam: everything downstream operates on a value
matrix and never asks how it was produced).

Because every leg of a game reads the ONE sampled state, all same-game hedges and
exclusions are EXACT with no correlation table: advance(A) ⊥ advance(B), BTTS
yes ⊥ no, over/under, goalscorer × total, 1H × FT. This is the model the operator
asked for — "track ALL legs, hedge via any leg" — realized as a joint sample.

Two fractional components are settled with SHARED coins so the correlation the
pricer marginalizes analytically is reproduced (and, for the shootout, made MORE
exact than the analytic — see below):

  * SHOOTOUT: one uniform per game decides advance(A) vs advance(B) on a
    level-after-ET state. advance(A) settles YES iff ``u < pens_win_a``; advance(B)
    iff ``u >= pens_win_a`` — so on any state exactly one advances. (The analytic
    ``joint_probability`` multiplies the two pens factors INDEPENDENTLY, which would
    give a spurious P(both advance) > 0; no valid combo ever holds both advance
    legs, so this never affects pricing, but the shared coin makes the cross-combo
    advance HEDGE exact in the portfolio MC.)
  * PLAYER GOALS: a shared multinomial allocation of the team's sampled goals to its
    scorers (sequential conditional binomials), so same-team scorers correlate the
    way ``_player_group_factor`` models them.

Parity gate (``tests/test_structural_book_mc.py``): the MC-estimated joint
P(all legs YES) equals ``dixon_coles.joint_probability`` to within a few standard
errors for every representable leg type. Legs Dixon-Coles cannot settle from a
scoreline (corners, cards, other sports) are NOT handled here — the caller keeps
them on the copula path (``sim/engine.sample_leg_values``).

Money/probability: pure probability space here (floats OK, hard rule 5); this
module builds only the inputs to the P&L engine and runs no P&L itself.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray

from combomaker.pricing.grouping import game_key

# P1.5: the risk MC reconstructs the pricer's structural model through the PUBLIC
# parse/invert/sample/settle contract (``pricing.structural_api``), never the
# private internals of ``pricing.structural`` / ``pricing.dixon_coles``. The API
# names are the same objects as those internals, so parity is byte-identical
# (``test_structural_api`` pins the identity).
from combomaker.pricing.structural_api import (
    Advance,
    HalfBtts,
    HalfDraw,
    HalfGoalSpread,
    HalfResult,
    HalfTotalOver,
    LegSpec,
    MatchFormat,
    ModelParams,
    PlayerScores,
    States as _States,
    StructuralError,
    Team,
    half_indicator as _half_indicator,
    invert,
    parse_leg as _parse_leg,
    parse_match as _parse_match,
    states as _states,
    team_goals as _team_goals,
    team_indicator as _team_indicator,
)
from combomaker.sim.engine import LegModel, sample_leg_values

_FloatMatrix = NDArray[np.float64]
_HALF_SPECS = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)


@dataclass(frozen=True, slots=True)
class StructuralConfigView:
    """The scalar Dixon-Coles constants the risk MC needs — a decoupled view of
    ``ops.config.StructuralConfig`` (pass its fields; avoids an ops<-sim import)."""

    dc_rho: float = -0.05
    et_factor: float = 0.3333
    pens_win_a: float = 0.5
    half_share: float = 0.45
    max_goals: int = 12
    knockout_series: tuple[str, ...] = ("KXWC",)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class GamePlan:
    """One game the risk MC samples STRUCTURALLY: the inverted model + the specs it
    settles + the GLOBAL leg-universe column each spec fills."""

    params: ModelParams
    specs: tuple[LegSpec, ...]
    shares: dict[int, float]           # LOCAL spec index -> player thinning share
    global_indices: tuple[int, ...]    # value-matrix column for specs[k]


def _match_format(ticker: str, knockout_series: Sequence[str]) -> MatchFormat:
    series = ticker.split("-", 1)[0].upper()
    if any(series.startswith(p.upper()) for p in knockout_series):
        return MatchFormat.KNOCKOUT
    return MatchFormat.GROUP


def _try_build_game(
    idxs: list[int], tickers: Sequence[str], marginals: Sequence[float | None],
    cfg: StructuralConfigView,
) -> GamePlan | None:
    """Invert one game's structural legs, or None ⇒ the whole game is copula.

    Mirrors ``pricing.structural.StructuralPricer._price`` parse+invert (hard rule
    8c: reuses ``_parse_match``/``_parse_leg``/``invert`` verbatim; the sampled
    joint is parity-gated vs ``joint_probability``). A leg that won't parse
    (corners/cards) or has an unknown marginal is left to the copula (dropped from
    the plan). ``invert`` needs >=2 team-level legs + an orienting leg for scorers,
    else it raises and the game falls back to the copula entirely."""
    first = tickers[idxs[0]]
    parts = first.split("-")
    if len(parts) < 2:
        return None
    match = _parse_match(parts[1])
    if match is None:
        return None
    fmt = _match_format(first, cfg.knockout_series)
    specs: list[LegSpec] = []
    gidx: list[int] = []
    targets: list[tuple[LegSpec, float]] = []
    for j in idxs:
        mgl = marginals[j]
        if mgl is None:
            continue
        spec = _parse_leg(tickers[j], match, fmt=fmt)
        if isinstance(spec, str):        # unrepresentable ⇒ copula
            continue
        specs.append(spec)
        gidx.append(j)
        targets.append((spec, float(mgl)))
    if not specs:
        return None
    try:
        model = invert(
            targets, dc_rho=cfg.dc_rho, et_factor=cfg.et_factor, match_format=fmt,
            max_goals=cfg.max_goals, pens_win_a=cfg.pens_win_a, half_share=cfg.half_share,
        )
    except StructuralError:
        return None
    return GamePlan(
        params=model.params, specs=tuple(specs), shares=dict(model.shares),
        global_indices=tuple(gidx),
    )


def build_game_plans(
    tickers: Sequence[str],
    events: Sequence[str | None],
    marginals: Sequence[float | None],
    cfg: StructuralConfigView,
) -> tuple[list[GamePlan], list[int]]:
    """Split the global leg universe into STRUCTURAL game plans + COPULA leg indices.

    Groups legs by game (``grouping.game_key`` of the event ticker), inverts each
    game's structural legs, and leaves everything the structural model can't settle
    (corners/cards, single-leg/unidentified games, ungamed legs) to the copula. The
    returned ``copula`` indices ∪ every plan's ``global_indices`` partition
    ``range(len(tickers))`` exactly."""
    n = len(tickers)
    if not cfg.enabled:
        return [], list(range(n))
    by_game: dict[str, list[int]] = defaultdict(list)
    copula: list[int] = []
    for j, ev in enumerate(events):
        if ev is None:
            copula.append(j)
        else:
            by_game[game_key(ev)].append(j)
    plans: list[GamePlan] = []
    for idxs in by_game.values():
        plan = _try_build_game(idxs, tickers, marginals, cfg)
        if plan is None:
            copula.extend(idxs)
            continue
        plans.append(plan)
        structural = set(plan.global_indices)
        copula.extend(j for j in idxs if j not in structural)
    return plans, copula


def sample_structural_values(
    plans: Sequence[GamePlan],
    copula_indices: Sequence[int],
    legs: Sequence[LegModel],
    corr: _FloatMatrix,
    n: int,
    rng: np.random.Generator,
) -> _FloatMatrix:
    """The full ``(n, len(legs))`` value matrix: structural columns sampled per game
    from the scoreline model, copula columns from the existing Gaussian-copula path
    (block corr restricted to those legs). Cross-game independence holds (each game
    draws from its own rng calls). Byte-compatible with ``sim/engine.book_pnl``."""
    out = np.zeros((n, len(legs)), dtype=np.float64)
    for plan in plans:
        vals = sample_game_values(plan.params, list(plan.specs), plan.shares, n, rng)
        for local, gidx in enumerate(plan.global_indices):
            out[:, gidx] = vals[:, local]
    if copula_indices:
        idx = list(copula_indices)
        sub_legs = [legs[i] for i in idx]
        sub_corr = np.asarray(corr, dtype=np.float64)[np.ix_(idx, idx)]
        sub_vals = sample_leg_values(sub_legs, sub_corr, n, rng)
        for local, gidx in enumerate(idx):
            out[:, gidx] = sub_vals[:, local]
    return out


def _sampled_states(states: _States, idx: NDArray[np.int64]) -> _States:
    """A ``_States`` whose arrays are the sampled state rows (weights all 1 — the
    per-sample states are unweighted draws, the leg indicators are pointwise)."""
    return _States(
        w=np.ones(idx.size, dtype=np.float64),
        a90=states.a90[idx], b90=states.b90[idx],
        a_et=states.a_et[idx], b_et=states.b_et[idx],
        a_1h=states.a_1h[idx], b_1h=states.b_1h[idx],
    )


def _advance_settle(
    states: _States, spec: Advance, params: ModelParams, u_pens: NDArray[np.float64]
) -> _FloatMatrix:
    """0/1 advance settlement with a SHARED shootout coin (same ``u_pens`` for every
    advance leg on the game → advance(A) and advance(B) are exact opposites on a
    level-after-ET state)."""
    if spec.team is Team.A:
        us90, them90, us_et, them_et = states.a90, states.b90, states.a_et, states.b_et
        shoot_win = u_pens < params.pens_win_a
    else:
        us90, them90, us_et, them_et = states.b90, states.a90, states.b_et, states.a_et
        shoot_win = u_pens >= params.pens_win_a
    win = (us90 > them90) | ((us90 == them90) & (us_et > them_et))
    level = (us90 == them90) & (us_et == them_et)
    settled: _FloatMatrix = (win | (level & shoot_win)).astype(np.float64)
    return settled


def sample_game_values(
    params: ModelParams,
    leg_specs: Sequence[LegSpec],
    shares: dict[int, float],
    n: int,
    rng: np.random.Generator,
) -> _FloatMatrix:
    """``(n, len(leg_specs))`` YES-settlement value matrix for ONE game's legs.

    ``leg_specs[j]`` is the Dixon-Coles spec for column j; ``shares[j]`` is the
    thinning share for a ``PlayerScores`` leg (ignored otherwise). Every column is
    settled 0/1 against the same ``n`` sampled game states (+ shared shootout /
    player coins), so columns are jointly correlated exactly as the model implies.
    """
    if n <= 0:
        raise ValueError(f"n must be > 0: {n}")
    need_halves = any(isinstance(s, _HALF_SPECS) for s in leg_specs)
    p = params if (params.with_halves or not need_halves) else replace(params, with_halves=True)
    states = _states(p)
    idx = rng.choice(states.w.size, size=n, p=states.w)
    sampled = _sampled_states(states, idx)
    u_pens = rng.random(n)                     # shared shootout coin (all advance legs)
    out = np.zeros((n, len(leg_specs)), dtype=np.float64)

    player_groups: dict[tuple[Team, bool], list[tuple[int, PlayerScores]]] = defaultdict(list)
    for j, spec in enumerate(leg_specs):
        if isinstance(spec, PlayerScores):
            player_groups[(spec.team, spec.include_et)].append((j, spec))
        elif isinstance(spec, Advance):
            out[:, j] = _advance_settle(sampled, spec, p, u_pens)
        elif isinstance(spec, _HALF_SPECS):
            out[:, j] = _half_indicator(sampled, spec)
        else:
            out[:, j] = _team_indicator(sampled, spec, p)

    for (team, inc_et), members in player_groups.items():
        n_team = _team_goals(sampled, team, inc_et)   # per-sample team goal count
        if len(members) == 1:
            j, spec = members[0]
            scored = rng.binomial(n_team, shares[j])
            out[:, j] = (scored >= spec.min_goals).astype(np.float64)
            continue
        # Shared multinomial: allocate the team's goals to its scorers via sequential
        # conditional binomials (min_goals == 1 enforced by the pricer for a group).
        remaining = n_team.copy()
        remaining_prob = 1.0
        for j, _spec in members:
            q = shares[j]
            pj = np.clip(q / remaining_prob, 0.0, 1.0) if remaining_prob > 0 else 0.0
            scored = rng.binomial(remaining, pj)
            out[:, j] = (scored >= 1).astype(np.float64)
            remaining = remaining - scored
            remaining_prob -= q
    return out
