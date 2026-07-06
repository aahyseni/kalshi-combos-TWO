"""MLB NegBin runs model: tie-free grid, structural relationships, inversion."""

from __future__ import annotations

import pytest

from combomaker.pricing.dixon_coles import StructuralError, Team
from combomaker.pricing.margin_total import GameTotalOver, SpreadCover, TeamWins
from combomaker.pricing.mlb_runs import (
    MlbShape,
    invert_runs,
    joint_probability,
    marginal_probability,
)

SHAPE = MlbShape(dispersion_k=3.62)


class TestRunsGrid:
    def test_no_ties_someone_wins(self) -> None:
        a = marginal_probability(4.4, 4.4, SHAPE, TeamWins(Team.A))
        b = marginal_probability(4.4, 4.4, SHAPE, TeamWins(Team.B))
        assert a + b == pytest.approx(1.0, abs=1e-9)
        assert a == pytest.approx(0.5, abs=1e-9)  # symmetric means

    def test_run_line_comonotone_with_moneyline(self) -> None:
        # Winning by over 1.5 implies winning: joint == run-line marginal.
        joint = joint_probability(
            4.8, 4.0, SHAPE, [(TeamWins(Team.A), True), (SpreadCover(Team.A, 1.5), True)]
        )
        cover = marginal_probability(4.8, 4.0, SHAPE, SpreadCover(Team.A, 1.5))
        assert joint == pytest.approx(cover, abs=1e-12)

    def test_total_complement(self) -> None:
        over = marginal_probability(4.4, 4.6, SHAPE, GameTotalOver(8.5))
        under = joint_probability(4.4, 4.6, SHAPE, [(GameTotalOver(8.5), False)])
        assert over + under == pytest.approx(1.0, abs=1e-12)

    def test_win_over_dependence_flips_with_orientation(self) -> None:
        """Mirror symmetry makes win ⊥ over EXACTLY at equal means; with a
        favorite the lift is positive, with a dog negative — the same
        orientation asymmetry the soccer model surfaced, for free."""

        def lift(mu_a: float, mu_b: float) -> float:
            joint = joint_probability(
                mu_a, mu_b, SHAPE, [(TeamWins(Team.A), True), (GameTotalOver(8.5), True)]
            )
            pa = marginal_probability(mu_a, mu_b, SHAPE, TeamWins(Team.A))
            pb = marginal_probability(mu_a, mu_b, SHAPE, GameTotalOver(8.5))
            return joint - pa * pb

        assert lift(4.4, 4.4) == pytest.approx(0.0, abs=1e-12)
        assert lift(4.8, 4.0) > 0.005   # favorite win x over: positive
        assert lift(4.0, 4.8) < -0.005  # dog win x over: negative

    def test_dispersion_moves_totals(self) -> None:
        wide = marginal_probability(4.4, 4.4, MlbShape(dispersion_k=2.5), GameTotalOver(13.5))
        tight = marginal_probability(4.4, 4.4, MlbShape(dispersion_k=6.0), GameTotalOver(13.5))
        assert wide > tight  # fatter tail with more overdispersion


class TestInversion:
    def test_roundtrip_ml_and_total(self) -> None:
        legs = [(TeamWins(Team.A), 0.55), (GameTotalOver(8.5), 0.48)]
        inv = invert_runs(legs, SHAPE)
        for spec, target in legs:
            got = marginal_probability(inv.mu_a, inv.mu_b, SHAPE, spec)
            assert got == pytest.approx(target, abs=1e-3)

    def test_needs_both_flavors(self) -> None:
        with pytest.raises(StructuralError, match="both"):
            invert_runs([(TeamWins(Team.A), 0.55), (SpreadCover(Team.A, 1.5), 0.40)], SHAPE)
        with pytest.raises(StructuralError, match="both"):
            invert_runs([(GameTotalOver(8.5), 0.48), (GameTotalOver(9.5), 0.35)], SHAPE)

    def test_out_of_range_marginal_refuses(self) -> None:
        with pytest.raises(StructuralError, match="out of invertible range"):
            invert_runs([(TeamWins(Team.A), 0.9999), (GameTotalOver(8.5), 0.5)], SHAPE)

    def test_overidentified_reports_residual(self) -> None:
        # ML 0.55 with a run line at 0.55 is inconsistent (covering -1.5 is
        # strictly harder than winning): misfit measured, priced into width.
        legs = [
            (TeamWins(Team.A), 0.55),
            (SpreadCover(Team.A, 1.5), 0.53),
            (GameTotalOver(8.5), 0.48),
        ]
        inv = invert_runs(legs, SHAPE)
        assert inv.residual > 0.01
