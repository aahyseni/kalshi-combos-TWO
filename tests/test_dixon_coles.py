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
    MatchFormat,
    ModelParams,
    PlayerScores,
    StructuralError,
    Team,
    TeamWin,
    TotalOver,
    _dc_grid,
    invert,
    joint_probability,
    marginal_probability,
)

ET = 1.0 / 3.0


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
