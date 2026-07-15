"""A1 — structural portfolio-risk sampler parity + hedge tests.

The sampler must reproduce ``dixon_coles.joint_probability`` for every leg type it
settles (the parity gate that proves the sampled joint equals the priced joint),
settle advance(A)/advance(B) as EXACT complements (the cross-combo hedge), and net
opposite-side combos to a zero joint-loss probability. Deterministic seed → the
Monte-Carlo assertions are reproducible, not flaky.
"""
from __future__ import annotations

import numpy as np
import pytest

from combomaker.pricing.dixon_coles import (
    Advance,
    Btts,
    Draw,
    GoalSpread,
    HalfTotalOver,
    MatchFormat,
    ModelParams,
    PlayerScores,
    Team,
    TeamWin,
    TotalOver,
    joint_probability,
)
from combomaker.sim.structural_book import sample_game_values

PARAMS = ModelParams(
    lam_a=1.35, lam_b=1.05, dc_rho=-0.05, et_factor=0.35,
    match_format=MatchFormat.KNOCKOUT, pens_win_a=0.55,
)
N = 200_000
TOL = 0.006  # ~4.5σ at p≈0.5, n=200k; deterministic seed makes it non-flaky


def _mc_joint(vals: np.ndarray, sides: list[bool]) -> float:
    cols = vals.copy()
    for j, yes in enumerate(sides):
        if not yes:
            cols[:, j] = 1.0 - cols[:, j]
    return float(np.prod(cols, axis=1).mean())


_CASES = [
    ("A_advance", [Advance(Team.A)], {}, [True]),
    ("B_advance", [Advance(Team.B)], {}, [True]),
    ("btts", [Btts()], {}, [True]),
    ("total3", [TotalOver(3)], {}, [True]),
    ("btts_and_total3", [Btts(), TotalOver(3)], {}, [True, True]),
    ("adv_and_scorer", [Advance(Team.A), PlayerScores(Team.A, 1)], {1: 0.30}, [True, True]),
    ("win_and_spread2", [TeamWin(Team.A), GoalSpread(Team.A, 2)], {}, [True, True]),
    ("draw", [Draw()], {}, [True]),
    ("two_scorers_same_team", [PlayerScores(Team.A, 1), PlayerScores(Team.A, 1)],
     {0: 0.28, 1: 0.22}, [True, True]),
    ("btts_no_and_total3", [Btts(), TotalOver(3)], {}, [False, True]),
    ("half_total1", [HalfTotalOver(1)], {}, [True]),
    ("half_total1_and_ft_total3", [HalfTotalOver(1), TotalOver(3)], {}, [True, True]),
]


@pytest.mark.parametrize("name,legs,shares,sides", _CASES, ids=[c[0] for c in _CASES])
def test_sampler_parity_with_joint_probability(name, legs, shares, sides):
    rng = np.random.default_rng(7)
    vals = sample_game_values(PARAMS, legs, shares, N, rng)
    mc = _mc_joint(vals, sides)
    analytic = joint_probability(PARAMS, list(zip(legs, sides, strict=True)), shares)
    assert abs(mc - analytic) < TOL, f"{name}: MC={mc:.5f} analytic={analytic:.5f}"


def test_advance_legs_are_exact_complements():
    """Shared shootout coin ⇒ advance(A) and advance(B) can never both settle YES,
    and exactly one settles on every sample."""
    rng = np.random.default_rng(3)
    vals = sample_game_values(PARAMS, [Advance(Team.A), Advance(Team.B)], {}, N, rng)
    a, b = vals[:, 0], vals[:, 1]
    assert float((a * b).mean()) == 0.0                 # never both advance
    assert np.all((a + b) == 1.0)                       # exactly one advances always


def test_values_are_binary():
    rng = np.random.default_rng(1)
    vals = sample_game_values(
        PARAMS, [Advance(Team.A), Btts(), TotalOver(3), PlayerScores(Team.A, 1)],
        {3: 0.3}, 5_000, rng)
    assert set(np.unique(vals)).issubset({0.0, 1.0})


def test_opposite_side_combos_cannot_both_lose():
    """The portfolio hedge: NO on (advance A + scorer A) and NO on (advance B +
    scorer B). Our NO loses iff the combo YES hits (all legs). A and B cannot both
    advance ⇒ the two combos can NEVER both lose ⇒ one side always pays us."""
    rng = np.random.default_rng(5)
    vals = sample_game_values(
        PARAMS,
        [Advance(Team.A), PlayerScores(Team.A, 1), Advance(Team.B), PlayerScores(Team.B, 1)],
        {1: 0.30, 3: 0.28}, N, rng)
    combo1_hits = vals[:, 0] * vals[:, 1]     # advance A AND scorer A
    combo2_hits = vals[:, 2] * vals[:, 3]     # advance B AND scorer B
    assert float((combo1_hits * combo2_hits).mean()) == 0.0   # never both hit ⇒ NO never both lose


def test_empty_and_bad_n():
    rng = np.random.default_rng(0)
    assert sample_game_values(PARAMS, [], {}, 10, rng).shape == (10, 0)
    with pytest.raises(ValueError):
        sample_game_values(PARAMS, [Btts()], {}, 0, rng)


# ---------------- build_game_plans + sample_structural_values -------------
from combomaker.sim.engine import LegModel  # noqa: E402
from combomaker.sim.structural_book import (  # noqa: E402
    StructuralConfigView,
    build_game_plans,
    sample_structural_values,
)

_ADV_ARG = "KXWCADVANCE-26JUL15ENGARG-ARG"
_ADV_ENG = "KXWCADVANCE-26JUL15ENGARG-ENG"
_CORNERS = "KXWCCORNERS-26JUL15ENGARG-9"
_EV = "KXWCADVANCE-26JUL15ENGARG"
CFG = StructuralConfigView()


def test_build_game_plans_inverts_advance_pair_and_recovers_marginals():
    tickers = [_ADV_ARG, _ADV_ENG]
    plans, copula = build_game_plans(tickers, [_EV, _EV], [0.55, 0.45], CFG)
    assert len(plans) == 1 and copula == []
    assert set(plans[0].global_indices) == {0, 1}
    rng = np.random.default_rng(7)
    vals = sample_structural_values(
        plans, copula, [LegModel(p=0.55), LegModel(p=0.45)], np.eye(2), N, rng)
    assert abs(vals[:, 0].mean() - 0.55) < 0.01     # recovers input marginal
    assert abs(vals[:, 1].mean() - 0.45) < 0.01
    assert float((vals[:, 0] * vals[:, 1]).mean()) == 0.0   # exact advance mutex


def test_corners_leg_falls_back_to_copula():
    tickers = [_ADV_ARG, _ADV_ENG, _CORNERS]
    plans, copula = build_game_plans(tickers, [_EV, _EV, _EV], [0.55, 0.45, 0.40], CFG)
    assert len(plans) == 1
    assert set(plans[0].global_indices) == {0, 1}
    assert copula == [2]                               # corners → copula


def test_single_leg_game_and_ungamed_are_copula():
    # one team-level leg can't identify (lam_a, lam_b) → whole game copula
    plans, copula = build_game_plans([_ADV_ARG], [_EV], [0.55], CFG)
    assert plans == [] and copula == [0]
    # an ungamed leg (no event ticker) → copula
    plans2, copula2 = build_game_plans([_ADV_ARG], [None], [0.55], CFG)
    assert plans2 == [] and copula2 == [0]


def test_disabled_config_is_all_copula():
    tickers = [_ADV_ARG, _ADV_ENG]
    plans, copula = build_game_plans(
        tickers, [_EV, _EV], [0.55, 0.45], StructuralConfigView(enabled=False))
    assert plans == [] and copula == [0, 1]


def test_partition_is_exact():
    tickers = [_ADV_ARG, _ADV_ENG, _CORNERS]
    plans, copula = build_game_plans(tickers, [_EV, _EV, _EV], [0.55, 0.45, 0.40], CFG)
    covered = set(copula)
    for p in plans:
        covered |= set(p.global_indices)
    assert covered == {0, 1, 2}                        # every leg is placed exactly once


def test_mixed_structural_and_copula_sample():
    tickers = [_ADV_ARG, _ADV_ENG, _CORNERS]
    plans, copula = build_game_plans(tickers, [_EV, _EV, _EV], [0.55, 0.45, 0.40], CFG)
    rng = np.random.default_rng(11)
    legs = [LegModel(p=0.55), LegModel(p=0.45), LegModel(p=0.40)]
    vals = sample_structural_values(plans, copula, legs, np.eye(3), N, rng)
    assert vals.shape == (N, 3)
    assert abs(vals[:, 2].mean() - 0.40) < 0.01        # copula corners column ~ its marginal
    assert set(np.unique(vals)).issubset({0.0, 1.0})


def test_compute_book_risk_structural_seam_captures_hedge():
    """Two NO combos on OPPOSITE advance sides. Under structural sampling they can
    never both lose (advance mutex) → a lower MC tail than the copula path, which
    applies a positive within-game rho and lets both sit in the tail together."""
    from combomaker.sim.book_model import BookModel
    from combomaker.sim.book_risk import compute_book_risk
    from combomaker.sim.engine import ComboPosition

    legs = (LegModel(p=0.55), LegModel(p=0.30), LegModel(p=0.45), LegModel(p=0.28))
    positions = (
        ComboPosition((0, 1), "no", 50, 8000, leg_sides=("yes", "yes")),  # NO(ARG adv + ARG scorer)
        ComboPosition((2, 3), "no", 50, 8000, leg_sides=("yes", "yes")),  # NO(ENG adv + ENG scorer)
    )
    corr = np.full((4, 4), 0.40)
    np.fill_diagonal(corr, 1.0)
    adv_ev, goal_ev = "KXWCADVANCE-26JUL15ENGARG", "KXWCGOAL-26JUL15ENGARG"
    leg_index = {
        _ADV_ARG: 0, "KXWCGOAL-26JUL15ENGARG-ARGX-1": 1,
        _ADV_ENG: 2, "KXWCGOAL-26JUL15ENGARG-ENGX-1": 3,
    }
    event_by_index = {0: adv_ev, 1: goal_ev, 2: adv_ev, 3: goal_ev}
    model = BookModel(legs, positions, corr, corr.copy(), corr.copy(),
                      leg_index, event_by_index, False)

    struct = compute_book_risk(model, n_samples=60_000, seed=1, structural_cfg=CFG)
    copula = compute_book_risk(model, n_samples=60_000, seed=1, structural_cfg=None)
    assert struct.usable and copula.usable
    # the hedge shows up in the MC tail: structural es_99 is strictly lower.
    assert struct.es_99_cc < copula.es_99_cc
