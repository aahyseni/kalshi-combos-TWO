"""Tests for the Gaussian-copula joint pricer (exact path).

All randomness inside scipy's MVN CDF is seeded by the module under test, so
every assertion here is deterministic.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from numpy.typing import NDArray

from combomaker.pricing.copula import (
    build_block_corr,
    clamp_to_frechet,
    conditional_joint_prob,
    frechet_bounds,
    gaussian_copula_joint_prob,
    is_psd,
    nearest_psd,
)


def _corr2(rho: float) -> NDArray[np.float64]:
    return np.array([[1.0, rho], [rho, 1.0]], dtype=np.float64)


class TestFrechetBounds:
    def test_single_leg(self) -> None:
        assert frechet_bounds([0.37]) == (0.37, 0.37)

    def test_two_legs_slack_lower(self) -> None:
        lower, upper = frechet_bounds([0.3, 0.4])
        assert lower == 0.0
        assert upper == 0.3

    def test_two_legs_binding_lower(self) -> None:
        lower, upper = frechet_bounds([0.7, 0.6])
        assert lower == pytest.approx(0.3, abs=1e-12)
        assert upper == 0.6

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            frechet_bounds([])

    def test_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            frechet_bounds([0.5, 1.2])


class TestClampToFrechet:
    def test_clamps_above_upper(self) -> None:
        assert clamp_to_frechet(0.9, [0.5, 0.6]) == 0.5

    def test_clamps_below_lower(self) -> None:
        assert clamp_to_frechet(0.0, [0.9, 0.9]) == pytest.approx(0.8, abs=1e-12)

    def test_interior_passthrough(self) -> None:
        assert clamp_to_frechet(0.25, [0.5, 0.6]) == 0.25


class TestIdentityCorr:
    @pytest.mark.parametrize(
        "ps",
        [
            [0.42],
            [0.3, 0.8],
            [0.5, 0.5, 0.5],
            [0.9, 0.8, 0.7, 0.6],
            [0.9, 0.8, 0.7, 0.6, 0.55],
        ],
    )
    def test_product_of_marginals(self, ps: list[float]) -> None:
        corr = np.eye(len(ps))
        joint = gaussian_copula_joint_prob(ps, corr)
        assert joint == pytest.approx(math.prod(ps), abs=1e-9)


class TestBivariateClosedForm:
    @pytest.mark.parametrize("rho", [-0.9, -0.5, 0.0, 0.3, 0.7, 0.95])
    def test_half_half_marginals(self, rho: float) -> None:
        # For p1 = p2 = 1/2: P(both YES) = 1/4 + arcsin(rho) / (2*pi).
        expected = 0.25 + math.asin(rho) / (2.0 * math.pi)
        joint = gaussian_copula_joint_prob([0.5, 0.5], _corr2(rho))
        assert joint == pytest.approx(expected, abs=1e-7)


class TestFrechetLimits:
    def test_rho_near_one_hits_upper_bound(self) -> None:
        joint = gaussian_copula_joint_prob([0.3, 0.6], _corr2(0.9999))
        assert joint == pytest.approx(0.3, abs=1e-3)

    def test_rho_near_minus_one_hits_lower_bound(self) -> None:
        # p1 + p2 > 1, so the lower bound is strictly positive.
        joint = gaussian_copula_joint_prob([0.7, 0.6], _corr2(-0.9999))
        assert joint == pytest.approx(0.3, abs=1e-3)


class TestDegeneracies:
    def test_any_zero_marginal_gives_zero(self) -> None:
        assert gaussian_copula_joint_prob([0.0, 0.5], _corr2(0.5)) == 0.0

    def test_zero_beats_one(self) -> None:
        assert gaussian_copula_joint_prob([0.0, 1.0], _corr2(0.5)) == 0.0

    def test_certain_leg_dropped(self) -> None:
        assert gaussian_copula_joint_prob([1.0, 0.4], _corr2(0.5)) == pytest.approx(
            0.4, abs=1e-12
        )

    def test_all_certain(self) -> None:
        assert gaussian_copula_joint_prob([1.0, 1.0, 1.0], np.eye(3)) == 1.0

    def test_single_leg(self) -> None:
        assert gaussian_copula_joint_prob([0.37], np.array([[1.0]])) == 0.37

    def test_certain_legs_reduce_to_correlated_pair(self) -> None:
        corr = build_block_corr(3, [([0, 1], 0.7)])
        expected = gaussian_copula_joint_prob([0.5, 0.5], _corr2(0.7))
        got = gaussian_copula_joint_prob([0.5, 0.5, 1.0], corr)
        assert got == pytest.approx(expected, abs=1e-12)


class TestCorrValidation:
    def test_non_square_raises(self) -> None:
        with pytest.raises(ValueError):
            gaussian_copula_joint_prob([0.5, 0.5], np.zeros((2, 3)))

    def test_wrong_size_raises(self) -> None:
        with pytest.raises(ValueError):
            gaussian_copula_joint_prob([0.5, 0.5, 0.5], _corr2(0.2))

    def test_asymmetric_raises(self) -> None:
        corr = np.array([[1.0, 0.5], [0.2, 1.0]])
        with pytest.raises(ValueError):
            gaussian_copula_joint_prob([0.5, 0.5], corr)

    def test_non_unit_diagonal_raises(self) -> None:
        corr = np.array([[1.0, 0.2], [0.2, 0.9]])
        with pytest.raises(ValueError):
            gaussian_copula_joint_prob([0.5, 0.5], corr)

    def test_non_psd_raises(self) -> None:
        corr = np.full((3, 3), -0.9)
        np.fill_diagonal(corr, 1.0)
        with pytest.raises(ValueError):
            gaussian_copula_joint_prob([0.5, 0.5, 0.5], corr)


class TestDeterminism:
    def test_same_inputs_same_output(self) -> None:
        ps = [0.55, 0.4, 0.65, 0.3, 0.8]
        corr = build_block_corr(5, [([0, 1, 2], 0.4), ([3, 4], 0.25)], default_rho=0.1)
        first = gaussian_copula_joint_prob(ps, corr)
        second = gaussian_copula_joint_prob(ps, corr)
        assert first == second

    def test_positive_equicorrelation_beats_product(self) -> None:
        ps = [0.5] * 5
        corr = build_block_corr(5, [], default_rho=0.3)
        assert gaussian_copula_joint_prob(ps, corr) > math.prod(ps)


class TestConditionalJointProb:
    @pytest.mark.parametrize(
        ("ps", "corr"),
        [
            ([0.3, 0.7], _corr2(-0.4)),
            ([0.55, 0.4, 0.65], build_block_corr(3, [([0, 1], 0.5)], default_rho=0.2)),
            ([0.5, 0.5, 0.5, 0.5], build_block_corr(4, [([0, 1], 0.6), ([2, 3], 0.3)])),
        ],
    )
    def test_law_of_total_probability(
        self, ps: list[float], corr: NDArray[np.float64]
    ) -> None:
        for given in range(len(ps)):
            others = [i for i in range(len(ps)) if i != given]
            p_others = gaussian_copula_joint_prob(
                [ps[i] for i in others], corr[np.ix_(others, others)]
            )
            p_given = ps[given]
            cond_yes = conditional_joint_prob(ps, corr, given=given, value=True)
            cond_no = conditional_joint_prob(ps, corr, given=given, value=False)
            total = p_given * cond_yes + (1.0 - p_given) * cond_no
            assert total == pytest.approx(p_others, abs=1e-9)

    def test_positive_correlation_raises_conditional(self) -> None:
        ps = [0.5, 0.5, 0.5]
        corr = build_block_corr(3, [], default_rho=0.4)
        p_others = gaussian_copula_joint_prob([0.5, 0.5], corr[np.ix_([1, 2], [1, 2])])
        cond_yes = conditional_joint_prob(ps, corr, given=0, value=True)
        assert cond_yes > p_others

    def test_single_leg_is_vacuous(self) -> None:
        assert conditional_joint_prob([0.4], np.array([[1.0]]), given=0, value=True) == 1.0
        assert conditional_joint_prob([0.4], np.array([[1.0]]), given=0, value=False) == 1.0

    @pytest.mark.parametrize("p_given", [0.0, 1.0])
    def test_degenerate_given_raises(self, p_given: float) -> None:
        with pytest.raises(ValueError):
            conditional_joint_prob([p_given, 0.5], _corr2(0.3), given=0, value=True)

    def test_given_index_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            conditional_joint_prob([0.5, 0.5], _corr2(0.3), given=2, value=True)


class TestBuildBlockCorr:
    def test_two_disjoint_blocks(self) -> None:
        m = build_block_corr(4, [([0, 1], 0.6), ([2, 3], 0.3)])
        expected = np.array(
            [
                [1.0, 0.6, 0.0, 0.0],
                [0.6, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.3],
                [0.0, 0.0, 0.3, 1.0],
            ]
        )
        np.testing.assert_array_equal(m, expected)

    def test_default_rho_fills_off_blocks(self) -> None:
        m = build_block_corr(3, [([0, 1], 0.5)], default_rho=0.1)
        assert m[0, 1] == 0.5
        assert m[0, 2] == 0.1
        assert m[1, 2] == 0.1
        np.testing.assert_array_equal(np.diag(m), np.ones(3))

    def test_later_block_overrides_overlapping_pair(self) -> None:
        m = build_block_corr(3, [([0, 1, 2], 0.5), ([1, 2], -0.2)])
        assert m[0, 1] == 0.5
        assert m[0, 2] == 0.5
        assert m[1, 2] == -0.2
        assert m[2, 1] == -0.2
        assert is_psd(m)

    def test_non_psd_construction_gets_repaired(self) -> None:
        # 3 legs pairwise rho = -0.9: eigenvalue 1 + 2*(-0.9) = -0.8 < 0.
        m = build_block_corr(3, [([0, 1, 2], -0.9)])
        assert is_psd(m)
        np.testing.assert_allclose(np.diag(m), 1.0, atol=1e-12)
        off_diag = m[~np.eye(3, dtype=bool)]
        # Eigenvalue clipping of the equicorrelated matrix lands on rho = -0.5.
        np.testing.assert_allclose(off_diag, -0.5, atol=1e-6)
        assert np.all(off_diag > -0.9)

    def test_rho_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError):
            build_block_corr(2, [([0, 1], 1.0)])
        with pytest.raises(ValueError):
            build_block_corr(2, [], default_rho=-1.0)

    def test_bad_index_raises(self) -> None:
        with pytest.raises(ValueError):
            build_block_corr(2, [([0, 2], 0.5)])

    def test_duplicate_index_raises(self) -> None:
        with pytest.raises(ValueError):
            build_block_corr(3, [([0, 0, 1], 0.5)])

    def test_n_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            build_block_corr(0, [])


class TestNearestPsd:
    def test_already_psd_unchanged(self) -> None:
        m = build_block_corr(4, [([0, 1], 0.6), ([2, 3], 0.3)])
        repaired = nearest_psd(m)
        np.testing.assert_allclose(repaired, m, rtol=0.0, atol=1e-12)

    def test_identity_unchanged(self) -> None:
        repaired = nearest_psd(np.eye(3))
        np.testing.assert_allclose(repaired, np.eye(3), rtol=0.0, atol=1e-12)

    def test_repair_produces_psd_unit_diagonal(self) -> None:
        bad = np.full((3, 3), -0.9)
        np.fill_diagonal(bad, 1.0)
        assert not is_psd(bad)
        repaired = nearest_psd(bad)
        assert is_psd(repaired)
        np.testing.assert_allclose(np.diag(repaired), 1.0, atol=1e-12)

    def test_non_square_raises(self) -> None:
        with pytest.raises(ValueError):
            nearest_psd(np.zeros((2, 3)))


class TestIsPsd:
    def test_identity_true(self) -> None:
        assert is_psd(np.eye(4))

    def test_indefinite_false(self) -> None:
        bad = np.full((3, 3), -0.9)
        np.fill_diagonal(bad, 1.0)
        assert not is_psd(bad)

    def test_asymmetric_false(self) -> None:
        assert not is_psd(np.array([[1.0, 0.5], [0.2, 1.0]]))

    def test_tiny_negative_eigenvalue_within_tol(self) -> None:
        m = np.eye(2) * (1.0 - 5e-11) + np.zeros((2, 2))
        m = m - np.eye(2)  # eigenvalues -5e-11
        assert is_psd(m, tol=1e-10)
        assert not is_psd(m, tol=1e-12)
