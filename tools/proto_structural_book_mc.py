"""PROTOTYPE (hard rule 8) for A1 — the STRUCTURAL portfolio-risk MC.

Sample ONE game state (scoreline + ET + first-half + a shared shootout coin +
shared player-goal allocation) from the SAME Dixon-Coles model that prices the
legs, then settle EVERY leg against it → a (n, n_legs) value matrix that
sim/engine.book_pnl consumes unchanged. Because all a game's legs read the one
sampled state, every hedge/exclusion is exact with NO rho table: advance(ARG) ⊥
advance(ENG), BTTS yes ⊥ no, over/under, goalscorer × total, etc.

CRITIQUE FIXES baked in:
 - shootout: ONE shared uniform per game decides advance(A) vs advance(B) on a
   level-after-ET state (independent per-leg Bernoullis would re-leak the ME).
 - player goals: a shared multinomial allocation per team (sequential conditional
   binomials) so same-team scorers are correlated the way the pricer models them.

PARITY GATE: the MC-estimated joint P(all legs YES) must equal
dixon_coles.joint_probability(params, legs, shares) to within a few standard
errors — that proves the sampler+settlement reproduce the analytic joint.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

import numpy as np

from combomaker.pricing.dixon_coles import (
    Advance,
    Btts,
    Draw,
    GoalSpread,
    MatchFormat,
    ModelParams,
    PlayerScores,
    Team,
    TeamWin,
    TotalOver,
    _half_indicator,
    _NO_HALF,
    _states,
    _States,
    _team_goals,
    _team_indicator,
    joint_probability,
)
from combomaker.pricing.dixon_coles import (
    HalfBtts,
    HalfDraw,
    HalfGoalSpread,
    HalfResult,
    HalfTotalOver,
)

_HALF = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)


def _sampled_states(states: _States, idx: np.ndarray) -> _States:
    return _States(
        w=np.ones(idx.size),
        a90=states.a90[idx], b90=states.b90[idx],
        a_et=states.a_et[idx], b_et=states.b_et[idx],
        a_1h=states.a_1h[idx], b_1h=states.b_1h[idx],
    )


def _advance_settle(S: _States, spec: Advance, params: ModelParams, u_pens: np.ndarray) -> np.ndarray:
    """0/1 advance settlement with a SHARED shootout coin u_pens (same array for
    every advance leg on the game → advance(A) and advance(B) are exact opposites
    on a level-after-ET state)."""
    if spec.team is Team.A:
        us90, them90, us_et, them_et = S.a90, S.b90, S.a_et, S.b_et
        shoot_win = u_pens < params.pens_win_a
    else:
        us90, them90, us_et, them_et = S.b90, S.a90, S.b_et, S.a_et
        shoot_win = u_pens >= params.pens_win_a
    win = (us90 > them90) | ((us90 == them90) & (us_et > them_et))
    level = (us90 == them90) & (us_et == them_et)
    return (win | (level & shoot_win)).astype(np.float64)


def sample_game_values(
    params: ModelParams,
    leg_specs: list,
    shares: dict[int, float],
    n: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """(n, n_legs) YES-settlement value matrix for one game's legs, sampled from
    the DC state PMF + shared shootout/player coins."""
    need_halves = any(isinstance(s, _HALF) for s in leg_specs)
    p = params if (params.with_halves or not need_halves) else replace(params, with_halves=True)
    states = _states(p)
    idx = rng.choice(states.w.size, size=n, p=states.w)
    S = _sampled_states(states, idx)
    u_pens = rng.random(n)                       # shared shootout coin
    out = np.zeros((n, len(leg_specs)), dtype=np.float64)

    player_groups: dict[tuple, list] = defaultdict(list)
    for j, spec in enumerate(leg_specs):
        if isinstance(spec, PlayerScores):
            player_groups[(spec.team, spec.include_et)].append((j, spec))
            continue
        if isinstance(spec, Advance):
            out[:, j] = _advance_settle(S, spec, p, u_pens)
        elif isinstance(spec, _HALF):
            out[:, j] = _half_indicator(S, spec)
        else:
            out[:, j] = _team_indicator(S, spec, p)

    for (team, inc_et), members in player_groups.items():
        n_team = _team_goals(S, team, inc_et)     # per-sample team goal count
        if len(members) == 1:
            j, spec = members[0]
            scored = rng.binomial(n_team, shares[j])
            out[:, j] = (scored >= spec.min_goals).astype(np.float64)
        else:
            # shared multinomial: sequential conditional binomials over the n_team
            # goals → same-team scorers correlated (min_goals==1 enforced upstream).
            remaining = n_team.copy()
            remaining_prob = 1.0
            for j, spec in members:
                q = shares[j]
                pj = np.clip(q / remaining_prob, 0.0, 1.0)
                cj = rng.binomial(remaining, pj)
                out[:, j] = (cj >= 1).astype(np.float64)
                remaining = remaining - cj
                remaining_prob -= q
    return out


# --------------------------------- parity ---------------------------------
def _mc_joint(vals: np.ndarray, yes_sides: list[bool]) -> float:
    cols = vals.copy()
    for j, yes in enumerate(yes_sides):
        if not yes:
            cols[:, j] = 1.0 - cols[:, j]
    return float(np.prod(cols, axis=1).mean())


def run_parity() -> None:
    print("=== A1 structural-MC parity vs dixon_coles.joint_probability ===")
    params = ModelParams(
        lam_a=1.35, lam_b=1.05, dc_rho=-0.05, et_factor=0.35,
        match_format=MatchFormat.KNOCKOUT, pens_win_a=0.55,
    )
    N = 400_000
    rng = np.random.default_rng(7)

    cases = [
        ("A advance", [Advance(Team.A)], {}, [True]),
        ("B advance", [Advance(Team.B)], {}, [True]),
        ("BTTS", [Btts()], {}, [True]),
        ("total 3+", [TotalOver(3)], {}, [True]),
        ("BTTS & total3", [Btts(), TotalOver(3)], {}, [True, True]),
        ("A adv & scorer1+", [Advance(Team.A), PlayerScores(Team.A, 1)], {1: 0.30}, [True, True]),
        ("A win & spread2", [TeamWin(Team.A), GoalSpread(Team.A, 2)], {}, [True, True]),
        ("draw", [Draw()], {}, [True]),
        ("2 scorers same team", [PlayerScores(Team.A, 1), PlayerScores(Team.A, 1)],
         {0: 0.28, 1: 0.22}, [True, True]),
        ("BTTS-NO & total3", [Btts(), TotalOver(3)], {}, [False, True]),
        ("half total1", [HalfTotalOver(1)], {}, [True]),
        ("half-total1 & FT total3", [HalfTotalOver(1), TotalOver(3)], {}, [True, True]),
    ]
    worst = 0.0
    for name, legs, shares, sides in cases:
        vals = sample_game_values(params, legs, shares, N, rng)
        mc = _mc_joint(vals, sides)
        analytic = joint_probability(params, [(s, y) for s, y in zip(legs, sides)], shares)
        se = max((mc * (1 - mc) / N) ** 0.5, 1e-6)
        z = abs(mc - analytic) / se
        worst = max(worst, z)
        flag = "OK " if z < 4 else "**FAIL**"
        print(f"  [{flag}] {name:<28} MC={mc:.5f}  analytic={analytic:.5f}  z={z:.2f}")
    print(f"\n  worst z = {worst:.2f}  ({'PASS' if worst < 4 else 'FAIL'} at 4-sigma)")

    # Advance MUTEX — the shared shootout coin makes advance(A) & advance(B) EXACTLY
    # mutually exclusive (0). The analytic joint_probability CANNOT express this (it
    # multiplies the two pens factors independently → a spurious P(both)>0). This is
    # A1 being MORE correct than the analytic for the cross-combo advance hedge, and
    # it never matters for pricing (no valid combo holds both advance legs).
    both = sample_game_values(params, [Advance(Team.A), Advance(Team.B)], {}, N, rng)
    p_both = _mc_joint(both, [True, True])
    print(f"\n  advance MUTEX: MC P(A adv & B adv) = {p_both:.6f} (want 0.0 — shared shootout coin)")
    assert p_both == 0.0, "advance legs not exactly mutually exclusive!"


if __name__ == "__main__":
    run_parity()
