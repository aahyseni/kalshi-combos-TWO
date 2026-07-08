"""Dixon-Coles scoreline model: grid math, inversion, joint pricing.

The two REGRESSION anchors are the worked examples that were validated
against an independent 2M-path Monte Carlo (and, for SPA/POR, against the
live Kalshi combo market, which priced the parlay at exactly the structural
fair): ENG/NOR joint 0.2282 and SPA/POR joint 0.1088 at dc_rho=0.
"""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.pricing.dixon_coles import (
    Advance,
    Btts,
    Draw,
    GoalSpread,
    HalfBtts,
    HalfDraw,
    HalfGoalSpread,
    HalfResult,
    HalfTotalOver,
    MatchFormat,
    ModelParams,
    PlayerScores,
    StructuralError,
    Team,
    TeamWin,
    TotalOver,
    _dc_grid,
    _states,
    invert,
    joint_probability,
    marginal_probability,
)

ET = 1.0 / 3.0


def _brute_4d(
    lam_a: float, lam_b: float, dc_rho: float, h: float, max_goals: int = 12
) -> tuple[np.ndarray, ...]:
    """Independent brute-force 4-D Poisson-split enumeration (GROUP, no ET) —
    the ground truth the factored half enumeration must reproduce (design §8).
    The 90' aggregate is capped at ``max_goals`` to share the model's support
    exactly (halves run to 20 so the convolution is complete up to the cap), so
    the only thing under test is the factoring, not grid truncation."""
    from scipy.stats import poisson

    k = 21
    a1 = poisson.pmf(np.arange(k), lam_a * h)
    a2 = poisson.pmf(np.arange(k), lam_a * (1 - h))
    b1 = poisson.pmf(np.arange(k), lam_b * h)
    b2 = poisson.pmf(np.arange(k), lam_b * (1 - h))
    w = (
        a1[:, None, None, None]
        * a2[None, :, None, None]
        * b1[None, None, :, None]
        * b2[None, None, None, :]
    )
    i1, i2, j1, j2 = np.meshgrid(*(np.arange(k),) * 4, indexing="ij")
    a90, b90 = i1 + i2, j1 + j2
    tau = np.ones_like(w)
    tau[(a90 == 0) & (b90 == 0)] = 1 - lam_a * lam_b * dc_rho
    tau[(a90 == 0) & (b90 == 1)] = 1 + lam_a * dc_rho
    tau[(a90 == 1) & (b90 == 0)] = 1 + lam_b * dc_rho
    tau[(a90 == 1) & (b90 == 1)] = 1 - dc_rho
    w = np.clip(w * tau, 0.0, None)
    w[(a90 > max_goals) | (b90 > max_goals)] = 0.0
    w /= w.sum()
    return w, a90, b90, i1, j1


def knockout_invert(legs, **kw):  # type: ignore[no-untyped-def]
    defaults = dict(dc_rho=0.0, et_factor=ET, match_format=MatchFormat.KNOCKOUT)
    defaults.update(kw)
    return invert(legs, **defaults)  # type: ignore[arg-type]


class TestGrid:
    def test_rho_zero_is_plain_poisson_product(self) -> None:
        grid = _dc_grid(1.5, 1.1, 0.0, 12)
        from scipy.stats import poisson

        expect = np.outer(poisson.pmf(np.arange(13), 1.5), poisson.pmf(np.arange(13), 1.1))
        assert np.allclose(grid, expect / expect.sum())

    def test_grid_normalizes(self) -> None:
        assert abs(_dc_grid(2.2, 0.7, -0.12, 12).sum() - 1.0) < 1e-12

    def test_negative_rho_boosts_low_draws_cuts_low_wins(self) -> None:
        base = _dc_grid(1.4, 1.2, 0.0, 12)
        adj = _dc_grid(1.4, 1.2, -0.10, 12)
        assert adj[0, 0] > base[0, 0]
        assert adj[1, 1] > base[1, 1]
        assert adj[1, 0] < base[1, 0]
        assert adj[0, 1] < base[0, 1]


class TestInversion:
    def test_two_constraints_reproduce_marginals_exactly(self) -> None:
        legs = [(TeamWin(Team.A), 0.65), (Btts(), 0.55)]
        model = knockout_invert(legs)
        for spec, target in legs:
            assert abs(marginal_probability(model.params, spec) - target) < 1e-4

    def test_player_share_reproduces_player_marginal(self) -> None:
        legs = [(TeamWin(Team.A), 0.65), (Btts(), 0.55), (PlayerScores(Team.A), 0.50)]
        model = knockout_invert(legs)
        assert abs(marginal_probability(model.params, legs[2][0], model.shares[2]) - 0.50) < 1e-6

    def test_eng_nor_regression_matches_monte_carlo(self) -> None:
        """England-win 0.65 / Kane 0.50 / BTTS 0.55 -> joint 0.2282 (MC 2M)."""
        legs = [(TeamWin(Team.A), 0.65), (PlayerScores(Team.A), 0.50), (Btts(), 0.55)]
        model = knockout_invert(legs)
        assert model.params.lam_a == pytest.approx(1.862, abs=0.02)
        assert model.params.lam_b == pytest.approx(1.028, abs=0.02)
        assert model.shares[1] == pytest.approx(0.342, abs=0.01)
        joint = joint_probability(model.params, [(s, True) for s, _ in legs], model.shares)
        assert joint == pytest.approx(0.2282, abs=0.002)

    def test_spa_por_regression_matches_live_market(self) -> None:
        """POR-win 0.24 / BTTS 0.60 / Ronaldo 0.38 -> joint 0.1088; the live
        Kalshi market paid $46-48 on $5 == priced this exact fair."""
        legs = [(TeamWin(Team.A), 0.24), (Btts(), 0.60), (PlayerScores(Team.A), 0.38)]
        model = knockout_invert(legs)
        joint = joint_probability(model.params, [(s, True) for s, _ in legs], model.shares)
        assert joint == pytest.approx(0.1088, abs=0.002)
        # Double independence: the whole reason this pricer exists.
        assert joint > 1.8 * (0.24 * 0.60 * 0.38)

    def test_one_team_constraint_is_unidentified(self) -> None:
        with pytest.raises(StructuralError, match="cannot identify"):
            knockout_invert([(TeamWin(Team.A), 0.65), (PlayerScores(Team.A), 0.50)])

    def test_goal_spread_margin_one_equals_win_90(self) -> None:
        # A 1-goal margin IS a win, so GoalSpread(min_margin=1) == 90' TeamWin.
        params = ModelParams(
            lam_a=1.6, lam_b=1.1, dc_rho=0.0, et_factor=ET, match_format=MatchFormat.GROUP
        )
        win90 = marginal_probability(params, TeamWin(Team.A, include_et=False))
        margin1 = marginal_probability(params, GoalSpread(Team.A, min_margin=1))
        assert margin1 == pytest.approx(win90, abs=1e-12)

    def test_goal_spread_monotone_in_margin(self) -> None:
        params = ModelParams(
            lam_a=1.6, lam_b=1.1, dc_rho=0.0, et_factor=ET, match_format=MatchFormat.GROUP
        )
        p1 = marginal_probability(params, GoalSpread(Team.A, 1))
        p2 = marginal_probability(params, GoalSpread(Team.A, 2))
        p3 = marginal_probability(params, GoalSpread(Team.A, 3))
        assert p1 > p2 > p3 > 0.0

    def test_goal_spread_orients_a_scorer(self) -> None:
        # A spread NAMES a team -> resolves orientation, so a scorer combo prices
        # (does NOT raise the orientation guard). Marginals consistent w/ (1.6,1.1).
        model = invert(
            [
                (GoalSpread(Team.A, 2), 0.2552),
                (TotalOver(3), 0.5064),
                (PlayerScores(Team.A), 0.40),
            ],
            dc_rho=0.0,
            et_factor=ET,
            match_format=MatchFormat.GROUP,
        )
        assert model.shares  # priced, did not raise the orientation StructuralError

    def test_scorer_with_symmetric_constraints_only_is_unoriented(self) -> None:
        # BTTS + Over pin {lam_a, lam_b} only as an unordered pair (both are
        # symmetric under team swap); a single-team scorer then attaches to an
        # arbitrary mirror. Decline -> copula (audit #2).
        with pytest.raises(StructuralError, match="orientation is unidentified"):
            knockout_invert(
                [(Btts(), 0.55), (TotalOver(3), 0.50), (PlayerScores(Team.A), 0.35)]
            )

    def test_draw_does_not_orient_a_scorer(self) -> None:
        # Draw is a moneyline-family selection but is SYMMETRIC — it must not
        # count as an orientation resolver.
        with pytest.raises(StructuralError, match="orientation is unidentified"):
            knockout_invert(
                [(Btts(), 0.55), (Draw(), 0.25), (PlayerScores(Team.A), 0.35)]
            )

    def test_scorer_with_orienting_leg_still_prices(self) -> None:
        # A TeamWin (or Advance) leg NAMES a team and pins orientation, so a
        # scorer combo stays structural — the guard must not over-catch.
        # (Targets are consistent with lam=(1.6, 1.1) so the exact solve holds.)
        m1 = knockout_invert(
            [(TeamWin(Team.A), 0.5674), (Btts(), 0.541), (PlayerScores(Team.A), 0.40)]
        )
        assert m1.shares  # priced, did not raise
        m2 = knockout_invert(
            [(Advance(Team.A), 0.6284), (Btts(), 0.541), (PlayerScores(Team.A), 0.40)]
        )
        assert m2.shares

    def test_scorers_on_both_teams_without_orienting_leg_is_unoriented(self) -> None:
        # Two scorers on OPPOSITE teams do NOT rescue orientation. The selected
        # joint is orientation-invariant only for all-YES / symmetric selections
        # (a coincidence); a mixed-side / asymmetric selection diverges ~11c
        # (adversarial audit: the identical combo priced 9.6c vs 20.2c under the
        # two team-code orderings). So we decline the whole no-orienting-leg
        # scorer class rather than lean on that selection-dependent cancellation.
        with pytest.raises(StructuralError, match="orientation is unidentified"):
            knockout_invert(
                [
                    (Btts(), 0.541),
                    (TotalOver(3), 0.5808),
                    (PlayerScores(Team.A), 0.35),
                    (PlayerScores(Team.B), 0.30),
                ]
            )

    def test_contradictory_exact_system_refuses(self) -> None:
        # A 95% favorite in a game where both teams score 90% of the time is
        # not representable by any Poisson scoreline.
        with pytest.raises(StructuralError):
            knockout_invert([(TeamWin(Team.A), 0.95), (Btts(), 0.90)])

    def test_infeasible_player_marginal_refuses(self) -> None:
        # Player scores more often than his team plausibly could.
        with pytest.raises(StructuralError, match="infeasible"):
            knockout_invert(
                [(TeamWin(Team.A), 0.30), (Btts(), 0.40), (PlayerScores(Team.A), 0.90)]
            )

    def test_overidentified_system_reports_residual(self) -> None:
        # Third constraint deliberately inconsistent with the first two.
        legs = [(TeamWin(Team.A), 0.65), (Btts(), 0.55), (TotalOver(3), 0.80)]
        model = knockout_invert(legs)
        assert model.residual > 0.005

    def test_marginal_out_of_range_refuses(self) -> None:
        with pytest.raises(StructuralError, match="out of invertible range"):
            knockout_invert([(TeamWin(Team.A), 0.9999), (Btts(), 0.55)])


class TestJoint:
    def make(self) -> tuple[ModelParams, dict[int, float]]:
        model = knockout_invert(
            [(TeamWin(Team.A), 0.65), (Btts(), 0.55), (PlayerScores(Team.A), 0.50)]
        )
        return model.params, model.shares

    def test_no_side_is_exact_complement(self) -> None:
        params, shares = self.make()
        legs_yes = [(TeamWin(Team.A), True), (Btts(), True)]
        legs_no = [(TeamWin(Team.A), True), (Btts(), False)]
        p_a = marginal_probability(params, TeamWin(Team.A))
        both = joint_probability(params, legs_yes, {})
        a_not_b = joint_probability(params, legs_no, {})
        assert abs((both + a_not_b) - p_a) < 1e-9

    def test_two_moneylines_same_game_impossible(self) -> None:
        params, _ = self.make()
        p = joint_probability(params, [(TeamWin(Team.A), True), (TeamWin(Team.B), True)], {})
        assert p == 0.0

    def test_draw_and_win90_disjoint(self) -> None:
        params, _ = self.make()
        p = joint_probability(
            params, [(Draw(), True), (TeamWin(Team.A, include_et=False), True)], {}
        )
        assert p == 0.0

    def test_win_incl_et_exceeds_win90(self) -> None:
        params, _ = self.make()
        incl = marginal_probability(params, TeamWin(Team.A, include_et=True))
        only90 = marginal_probability(params, TeamWin(Team.A, include_et=False))
        assert incl > only90

    def test_group_format_has_no_et(self) -> None:
        params = ModelParams(
            lam_a=1.5, lam_b=1.1, dc_rho=0.0, et_factor=ET, match_format=MatchFormat.GROUP
        )
        incl = marginal_probability(params, TeamWin(Team.A, include_et=True))
        only90 = marginal_probability(params, TeamWin(Team.A, include_et=False))
        assert incl == pytest.approx(only90, abs=1e-12)

    def test_advance_partitions_the_match(self) -> None:
        """Exactly one team advances: P(adv A) + P(adv B) = 1, and advancing
        strictly exceeds winning inside 90+ET (pens paths are extra)."""
        params, _ = self.make()
        adv_a = marginal_probability(params, Advance(Team.A))
        adv_b = marginal_probability(params, Advance(Team.B))
        assert adv_a + adv_b == pytest.approx(1.0, abs=1e-9)
        assert adv_a > marginal_probability(params, TeamWin(Team.A, include_et=True))

    def test_advance_pens_probability_moves_the_marginal(self) -> None:
        from dataclasses import replace

        params, _ = self.make()
        hi = marginal_probability(replace(params, pens_win_a=0.6), Advance(Team.A))
        lo = marginal_probability(replace(params, pens_win_a=0.4), Advance(Team.A))
        assert hi > lo

    def test_advance_in_group_format_refuses(self) -> None:
        params = ModelParams(
            lam_a=1.5, lam_b=1.1, dc_rho=0.0, et_factor=ET, match_format=MatchFormat.GROUP
        )
        with pytest.raises(StructuralError, match="non-knockout"):
            marginal_probability(params, Advance(Team.A))

    def test_two_players_same_team_inclusion_exclusion(self) -> None:
        params, _ = self.make()
        shares = {0: 0.30, 1: 0.25}
        legs = [(PlayerScores(Team.A), True), (PlayerScores(Team.A), True)]
        both = joint_probability(params, legs, shares)
        p0 = marginal_probability(params, PlayerScores(Team.A), 0.30)
        p1 = marginal_probability(params, PlayerScores(Team.A), 0.25)
        # Teammates compete for the same goals given n, but n varies: the
        # joint must stay inside Frechet and above the product-with-blanks
        # sanity floor of zero.
        assert 0.0 < both < min(p0, p1)
        # inclusion-exclusion identity: P(A&B) = P(A)+P(B)-P(A or B), where
        # P(A or B) = 1 - P(both blank) = 1 - E[(1-q0-q1)^n].
        neither = joint_probability(
            params,
            [(PlayerScores(Team.A), False), (PlayerScores(Team.A), False)],
            shares,
        )
        assert abs((p0 + p1 - (1 - neither)) - both) < 1e-9


@settings(max_examples=25, deadline=None)
@given(
    p_win=st.floats(0.15, 0.80),
    p_btts=st.floats(0.30, 0.75),
    p_player=st.floats(0.15, 0.60),
)
def test_property_inversion_roundtrip_and_frechet(
    p_win: float, p_btts: float, p_player: float
) -> None:
    legs = [(TeamWin(Team.A), p_win), (Btts(), p_btts), (PlayerScores(Team.A), p_player)]
    try:
        model = knockout_invert(legs)
    except StructuralError:
        return  # honest refusal is always acceptable
    for i, (spec, target) in enumerate(legs):
        got = marginal_probability(model.params, spec, model.shares.get(i))
        assert abs(got - target) < 5e-3
    joint = joint_probability(model.params, [(s, True) for s, _ in legs], model.shares)
    lo = max(0.0, p_win + p_btts + p_player - 2)
    hi = min(p_win, p_btts, p_player)
    assert lo - 1e-6 <= joint <= hi + 5e-3


# ------------------------------------------------------------------ half-time DC


def _both(la: float, lb: float, rho: float, fmt: MatchFormat) -> tuple[ModelParams, ModelParams]:
    ft = ModelParams(lam_a=la, lam_b=lb, dc_rho=rho, et_factor=ET, match_format=fmt)
    from dataclasses import replace

    return ft, replace(ft, with_halves=True)


class TestHalfTimePreservesFullGame:
    """The hard fail-closed invariant: turning on the half machinery must not
    move any full-game price (design 'FT preserved to ~1e-9')."""

    def test_ft_leg_joints_identical_2d_vs_4d(self) -> None:
        for fmt in (MatchFormat.GROUP, MatchFormat.KNOCKOUT):
            ft, half = _both(1.7, 1.05, -0.05, fmt)
            for legs in (
                [(TeamWin(Team.A, include_et=False), True)],
                [(Btts(include_et=False), True)],
                [(TotalOver(3, include_et=False), True)],
                [(Draw(), True)],
                [
                    (TeamWin(Team.A, include_et=False), True),
                    (TotalOver(3, include_et=False), True),
                    (Btts(include_et=False), True),
                ],
            ):
                p2 = joint_probability(ft, legs, {})
                p4 = joint_probability(half, legs, {})
                assert abs(p2 - p4) < 1e-9, (fmt, legs, p2, p4)

    def test_default_params_are_ft_only(self) -> None:
        # The lazy gate: a params built the old way must NOT carry the half grid.
        assert ModelParams(1.5, 1.1, 0.0, ET, MatchFormat.GROUP).with_halves is False

    def test_collapsed_half_grid_equals_dc_grid(self) -> None:
        # Sum a state's 1H splits -> its FT weight, so the FT marginal read off
        # the half enumeration equals _dc_grid to float precision.
        _ft, half = _both(2.0, 0.9, -0.1, MatchFormat.GROUP)
        states = _states(half)
        g = half.max_goals
        ft_grid = np.zeros((g + 1, g + 1))
        np.add.at(ft_grid, (states.a90, states.b90), states.w)
        ref = _dc_grid(2.0, 0.9, -0.1, g)
        assert np.max(np.abs(ft_grid - ref)) < 1e-12


class TestHalfTimeExactness:
    """The half joint is the true joint to all orders (design §5): it equals an
    independent brute-force 4-D Poisson-split enumeration."""

    @settings(max_examples=20, deadline=None)
    @given(
        la=st.floats(0.6, 2.8),
        lb=st.floats(0.6, 2.8),
        rho=st.floats(-0.15, 0.0),
    )
    def test_matches_brute_force_4d(self, la: float, lb: float, rho: float) -> None:
        h = 0.45
        w, a90, b90, i1, j1 = _brute_4d(la, lb, rho, h)
        p = ModelParams(
            lam_a=la, lam_b=lb, dc_rho=rho, et_factor=ET,
            match_format=MatchFormat.GROUP, half_share=h,
        )
        cases = {
            "1H home lead": (HalfResult(Team.A), (i1 > j1)),
            "1H draw": (HalfDraw(), (i1 == j1)),
            "1H over1.5": (HalfTotalOver(2), (i1 + j1 >= 2)),
            "1H btts": (HalfBtts(), (i1 >= 1) & (j1 >= 1)),
            "1H spread A>=2": (HalfGoalSpread(Team.A, 2), (i1 - j1 >= 2)),
        }
        # Same support (aggregate capped at max_goals), so the factored joint
        # equals the direct 4-D joint to float precision.
        for spec, mask in cases.values():
            assert abs(marginal_probability(p, spec) - float(w[mask].sum())) < 1e-9
        # a mixed 1H x FT joint
        joint = joint_probability(
            p,
            [(HalfResult(Team.A), True), (TeamWin(Team.A, include_et=False), True)],
            {},
        )
        assert abs(joint - float(w[(i1 > j1) & (a90 > b90)].sum())) < 1e-9

    def test_first_half_over_contains_full_time_over(self) -> None:
        # 1H goals <= FT goals, so 1H-over-N ⊆ FT-over-N: the joint equals the
        # 1H marginal EXACTLY (structural containment falls out, no rho).
        p = ModelParams(
            lam_a=1.6, lam_b=1.2, dc_rho=-0.05, et_factor=ET,
            match_format=MatchFormat.GROUP,
        )
        for n in (1, 2, 3):
            p1 = marginal_probability(p, HalfTotalOver(n))
            both = joint_probability(
                p, [(HalfTotalOver(n), True), (TotalOver(n, include_et=False), True)], {}
            )
            assert both == pytest.approx(p1, abs=1e-12)

    def test_reproduces_empirical_lead_persistence(self) -> None:
        # design §8 sanity anchor: at a representative league game the model's
        # P(FT home-win | 1H home-lead) lands near the empirical 0.767-0.80.
        p = ModelParams(
            lam_a=1.5, lam_b=1.15, dc_rho=-0.05, et_factor=ET,
            match_format=MatchFormat.GROUP,
        )
        lead = marginal_probability(p, HalfResult(Team.A))
        lead_and_win = joint_probability(
            p, [(HalfResult(Team.A), True), (TeamWin(Team.A, include_et=False), True)], {}
        )
        assert lead_and_win / lead == pytest.approx(0.795, abs=0.02)


class TestHalfTimeInversionAndGuards:
    def test_half_total_leg_identifies_lambdas(self) -> None:
        # An FT win + a 1H total exactly identify (lam_a, lam_b) with h fixed.
        model = invert(
            [(TeamWin(Team.A, include_et=False), 0.55), (HalfTotalOver(1), 0.70)],
            dc_rho=0.0, et_factor=ET, match_format=MatchFormat.KNOCKOUT,
        )
        assert abs(marginal_probability(model.params, HalfTotalOver(1)) - 0.70) < 5e-3
        assert model.residual < 5e-3

    def test_half_result_orients_a_scorer(self) -> None:
        # A 1H team-lead NAMES a team, so it resolves orientation for a scorer
        # combo (no orientation decline).
        model = invert(
            [
                (HalfResult(Team.A), 0.34),
                (TotalOver(3, include_et=False), 0.52),
                (PlayerScores(Team.A), 0.40),
            ],
            dc_rho=0.0, et_factor=ET, match_format=MatchFormat.GROUP,
        )
        assert model.shares  # priced, did not raise the orientation guard

    def test_symmetric_half_leg_does_not_orient_scorer(self) -> None:
        # HalfTotalOver is team-symmetric -> with only symmetric constraints the
        # scorer orientation is unidentified (decline to copula).
        with pytest.raises(StructuralError, match="orientation is unidentified"):
            invert(
                [
                    (HalfTotalOver(1), 0.70),
                    (TotalOver(3, include_et=False), 0.52),
                    (PlayerScores(Team.A), 0.35),
                ],
                dc_rho=0.0, et_factor=ET, match_format=MatchFormat.GROUP,
            )

    def test_half_indicator_fails_closed_on_ft_only_states(self) -> None:
        # Directly evaluating a 1H indicator on the FT-only (sentinel) states
        # must raise, never read a wrong zero.
        from combomaker.pricing.dixon_coles import _half_indicator

        ft = ModelParams(1.5, 1.1, 0.0, ET, MatchFormat.GROUP)
        with pytest.raises(StructuralError, match="half-time enumeration"):
            _half_indicator(_states(ft), HalfResult(Team.A))
