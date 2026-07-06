"""Bivariate-normal (margin, total) model: region math, inversion, and the
structural relationships the v1 copula hand-encodes (ML x spread comonotone,
ML x total near-independent, team totals coherent with both)."""

from __future__ import annotations

import pytest
from scipy.stats import norm

from combomaker.pricing.dixon_coles import StructuralError, Team
from combomaker.pricing.margin_total import (
    GameTotalOver,
    SportShape,
    SpreadCover,
    TeamTotalOver,
    TeamWins,
    invert_means,
    marginal_probability,
    region_probability,
)

NFL = SportShape(sigma_margin=12.66, sigma_total=13.06, rho=0.026)
NBA = SportShape(sigma_margin=13.71, sigma_total=18.42, rho=0.0)


class TestRegionMath:
    def test_moneyline_marginal_is_normal_cdf(self) -> None:
        got = marginal_probability(3.0, 45.0, NFL, TeamWins(Team.A))
        assert got == pytest.approx(norm.cdf(3.0 / NFL.sigma_margin), abs=1e-6)

    def test_total_marginal_is_normal_cdf(self) -> None:
        got = marginal_probability(0.0, 47.0, NFL, GameTotalOver(44.5))
        assert got == pytest.approx(norm.cdf((47.0 - 44.5) / NFL.sigma_total), abs=1e-6)

    def test_team_moneylines_partition(self) -> None:
        a = marginal_probability(2.5, 45.0, NFL, TeamWins(Team.A))
        b = marginal_probability(2.5, 45.0, NFL, TeamWins(Team.B))
        assert a + b == pytest.approx(1.0, abs=1e-6)

    def test_ml_and_spread_are_comonotone(self) -> None:
        """Win AND cover -3.5 == cover alone (M > 3.5 implies M > 0): the
        structure prices exactly what the copula approximates with rho 0.88."""
        legs = [(TeamWins(Team.A), True), (SpreadCover(Team.A, 3.5), True)]
        joint = region_probability(2.0, 45.0, NFL, legs)
        cover = marginal_probability(2.0, 45.0, NFL, SpreadCover(Team.A, 3.5))
        assert joint == pytest.approx(cover, abs=1e-6)

    def test_opposite_moneylines_impossible(self) -> None:
        legs = [(TeamWins(Team.A), True), (TeamWins(Team.B), True)]
        assert region_probability(1.0, 45.0, NFL, legs) == 0.0

    def test_ml_x_total_near_independence_at_zero_rho(self) -> None:
        legs = [(TeamWins(Team.A), True), (GameTotalOver(220.5), True)]
        joint = region_probability(4.0, 224.0, NBA, legs)
        pa = marginal_probability(4.0, 224.0, NBA, TeamWins(Team.A))
        pb = marginal_probability(4.0, 224.0, NBA, GameTotalOver(220.5))
        assert joint == pytest.approx(pa * pb, abs=1e-6)  # rho=0: exact product

    def test_no_side_is_exact_complement(self) -> None:
        both = region_probability(
            2.0, 46.0, NFL, [(TeamWins(Team.A), True), (GameTotalOver(44.5), True)]
        )
        a_not_b = region_probability(
            2.0, 46.0, NFL, [(TeamWins(Team.A), True), (GameTotalOver(44.5), False)]
        )
        pa = marginal_probability(2.0, 46.0, NFL, TeamWins(Team.A))
        assert both + a_not_b == pytest.approx(pa, abs=1e-6)

    def test_team_total_couples_margin_and_total(self) -> None:
        """Home team total rises with BOTH the margin mean and total mean."""
        base = marginal_probability(0.0, 45.0, NFL, TeamTotalOver(Team.A, 24.5))
        more_margin = marginal_probability(6.0, 45.0, NFL, TeamTotalOver(Team.A, 24.5))
        more_total = marginal_probability(0.0, 51.0, NFL, TeamTotalOver(Team.A, 24.5))
        assert more_margin > base and more_total > base

    def test_win_and_team_total_positively_associated(self) -> None:
        """Winning implies scoring: P(win & team over) > P(win)P(team over)."""
        legs = [(TeamWins(Team.A), True), (TeamTotalOver(Team.A, 24.5), True)]
        joint = region_probability(0.0, 45.0, NFL, legs)
        pa = marginal_probability(0.0, 45.0, NFL, TeamWins(Team.A))
        pb = marginal_probability(0.0, 45.0, NFL, TeamTotalOver(Team.A, 24.5))
        assert joint > pa * pb + 0.01


class TestInversion:
    def test_roundtrip_ml_and_total(self) -> None:
        legs = [(TeamWins(Team.A), 0.62), (GameTotalOver(224.5), 0.55)]
        inv = invert_means(legs, NBA)
        for spec, target in legs:
            got = marginal_probability(inv.mu_m, inv.mu_t, NBA, spec)
            assert got == pytest.approx(target, abs=1e-4)

    def test_margin_only_combo_is_comonotone_after_inversion(self) -> None:
        # Consistent marginals: ML 0.62 -> mu_M = 3.868; the -3.5 cover prob
        # implied by that mean is norm.cdf((3.868-3.5)/12.66) = 0.5116.
        mu = NFL.sigma_margin * norm.ppf(0.62)
        p_cover = float(norm.cdf((mu - 3.5) / NFL.sigma_margin))
        legs = [(TeamWins(Team.A), 0.62), (SpreadCover(Team.A, 3.5), p_cover)]
        inv = invert_means(legs, NFL)
        assert inv.residual < 1e-4
        joint = region_probability(
            inv.mu_m, inv.mu_t, NFL, [(spec, True) for spec, _ in legs]
        )
        # comonotone: win-and-cover == the tighter (spread) marginal
        assert joint == pytest.approx(p_cover, abs=1e-3)

    def test_underidentified_directions_refuse(self) -> None:
        # A lone team-total leg needs both means but pins only one direction.
        with pytest.raises(StructuralError, match="identify"):
            invert_means([(TeamTotalOver(Team.A, 24.5), 0.5)], NFL)

    def test_contradictory_legs_refuse(self) -> None:
        # ML says A is a huge favorite; the +20 spread priced at 0.05 says
        # the market expects A to LOSE by 20 -> no mu_M satisfies both.
        legs = [(TeamWins(Team.A), 0.95), (SpreadCover(Team.A, -20.0), 0.05)]
        with pytest.raises(StructuralError, match="inconsistent"):
            invert_means(legs, NFL)

    def test_out_of_range_marginal_refuses(self) -> None:
        with pytest.raises(StructuralError, match="out of invertible range"):
            invert_means([(TeamWins(Team.A), 0.9999), (GameTotalOver(44.5), 0.5)], NFL)

    def test_overidentified_reports_residual(self) -> None:
        # ML at 0.60 and a -3.5 spread at 0.55 are mildly inconsistent
        # (covering -3.5 must be LESS likely than winning): the misfit is
        # measured and reported, not refused (it prices into width).
        legs = [
            (TeamWins(Team.A), 0.60),
            (SpreadCover(Team.A, 3.5), 0.55),
            (GameTotalOver(44.5), 0.50),
        ]
        inv = invert_means(legs, NFL)
        assert 0.015 < inv.residual < 0.05
