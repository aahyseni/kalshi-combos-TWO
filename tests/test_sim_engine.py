"""Deterministic, seeded tests for the Monte Carlo combo simulator."""

from __future__ import annotations

import math

import numpy as np
import pytest
from numpy.typing import NDArray

from combomaker.sim.engine import (
    ComboPosition,
    LegModel,
    leg_deltas,
    marginal_impact,
    sample_leg_values,
    simulate,
)

N = 200_000
DOLLAR_CC = 10_000.0


def eye(n: int) -> NDArray[np.float64]:
    return np.eye(n, dtype=np.float64)


def corr2(rho: float) -> NDArray[np.float64]:
    return np.array([[1.0, rho], [rho, 1.0]], dtype=np.float64)


def binary_hit_sigma(p_hit: float, n: int) -> float:
    """MC standard error of the mean payout (cc) for a binary $1 payoff."""
    return DOLLAR_CC * math.sqrt(p_hit * (1.0 - p_hit)) / math.sqrt(n)


class TestIndependentBinary:
    LEGS = (LegModel(p=0.7), LegModel(p=0.5), LegModel(p=0.4))
    PRICE = 1_000
    P_HIT = 0.7 * 0.5 * 0.4

    def test_yes_side_ev(self) -> None:
        pos = ComboPosition(leg_indices=(0, 1, 2), side="yes", contracts=1, price_cc=self.PRICE)
        stats = simulate(self.LEGS, eye(3), [pos], n_samples=N, seed=42)
        expected = DOLLAR_CC * self.P_HIT - self.PRICE
        sigma = binary_hit_sigma(self.P_HIT, N)
        assert abs(stats.ev_cc - expected) < 3.0 * sigma
        assert stats.pnl_samples.shape == (N,)
        assert stats.pnl_samples.dtype == np.float64

    def test_no_side_mirrors_yes(self) -> None:
        pos_yes = ComboPosition(
            leg_indices=(0, 1, 2), side="yes", contracts=1, price_cc=self.PRICE
        )
        pos_no = ComboPosition(
            leg_indices=(0, 1, 2), side="no", contracts=1, price_cc=10_000 - self.PRICE
        )
        s_yes = simulate(self.LEGS, eye(3), [pos_yes], n_samples=N, seed=42)
        s_no = simulate(self.LEGS, eye(3), [pos_no], n_samples=N, seed=42)
        assert np.array_equal(s_no.pnl_samples, -s_yes.pnl_samples)
        assert s_no.ev_cc == -s_yes.ev_cc


class TestCopulaCorrelation:
    def test_symmetric_pair_rho_07_closed_form(self) -> None:
        # P(Z1 >= 0, Z2 >= 0) with corr rho: 1/4 + arcsin(rho) / (2 pi).
        legs = (LegModel(p=0.5), LegModel(p=0.5))
        pos = ComboPosition(leg_indices=(0, 1), side="yes", contracts=1, price_cc=0)
        stats = simulate(legs, corr2(0.7), [pos], n_samples=N, seed=7)
        p_hit = 0.25 + math.asin(0.7) / (2.0 * math.pi)
        sigma = binary_hit_sigma(p_hit, N)
        assert abs(stats.ev_cc - DOLLAR_CC * p_hit) < 3.0 * sigma

    def test_rho_one_comonotone(self) -> None:
        legs = (LegModel(p=0.6), LegModel(p=0.3))
        pos = ComboPosition(leg_indices=(0, 1), side="yes", contracts=1, price_cc=0)
        stats = simulate(legs, corr2(0.999999), [pos], n_samples=N, seed=11)
        hit_rate = stats.ev_cc / DOLLAR_CC
        assert abs(hit_rate - min(0.6, 0.3)) < 0.005

    def test_rho_minus_one_countermonotone(self) -> None:
        # p1 + p2 > 1 so the Frechet lower bound P = p1 + p2 - 1 is positive.
        legs = (LegModel(p=0.7), LegModel(p=0.6))
        pos = ComboPosition(leg_indices=(0, 1), side="yes", contracts=1, price_cc=0)
        stats = simulate(legs, corr2(-0.999999), [pos], n_samples=N, seed=13)
        hit_rate = stats.ev_cc / DOLLAR_CC
        assert abs(hit_rate - (0.7 + 0.6 - 1.0)) < 0.005


class TestScalarSettlement:
    def test_constant_settlement_pays_exactly(self) -> None:
        legs = (LegModel(p=0.5, settlement=((0.4, 1.0),)),)
        pos = ComboPosition(leg_indices=(0,), side="yes", contracts=1, price_cc=0)
        stats = simulate(legs, eye(1), [pos], n_samples=10_000, seed=5)
        assert np.all(stats.pnl_samples == 0.4 * DOLLAR_CC)

    def test_binary_settlement_matches_none_exactly(self) -> None:
        p = 0.35
        legs_none = (LegModel(p=p),)
        legs_expl = (LegModel(p=p, settlement=((0.0, 1.0 - p), (1.0, p))),)
        pos = ComboPosition(leg_indices=(0,), side="yes", contracts=2, price_cc=3_000, fee_cc=7)
        s_none = simulate(legs_none, eye(1), [pos], n_samples=50_000, seed=17)
        s_expl = simulate(legs_expl, eye(1), [pos], n_samples=50_000, seed=17)
        assert np.array_equal(s_none.pnl_samples, s_expl.pnl_samples)

    def test_sample_leg_values_shape(self) -> None:
        legs = (LegModel(p=0.5), LegModel(p=0.2, settlement=((0.0, 0.5), (0.5, 0.3), (1.0, 0.2))))
        rng = np.random.default_rng(0)
        values = sample_leg_values(legs, eye(2), 1_000, rng)
        assert values.shape == (1_000, 2)
        assert values.dtype == np.float64
        assert np.all((values >= 0.0) & (values <= 1.0))


class TestDeterminism:
    LEGS = (LegModel(p=0.5), LegModel(p=0.4))
    POS = (ComboPosition(leg_indices=(0, 1), side="yes", contracts=3, price_cc=1_500),)

    def test_same_seed_identical(self) -> None:
        a = simulate(self.LEGS, corr2(0.3), self.POS, n_samples=20_000, seed=99)
        b = simulate(self.LEGS, corr2(0.3), self.POS, n_samples=20_000, seed=99)
        assert np.array_equal(a.pnl_samples, b.pnl_samples)

    def test_different_seed_differs(self) -> None:
        a = simulate(self.LEGS, corr2(0.3), self.POS, n_samples=20_000, seed=99)
        b = simulate(self.LEGS, corr2(0.3), self.POS, n_samples=20_000, seed=100)
        assert not np.array_equal(a.pnl_samples, b.pnl_samples)


class TestMarginalImpact:
    def test_empty_book_with_equals_simulate_alone(self) -> None:
        legs = (LegModel(p=0.55), LegModel(p=0.45))
        candidate = ComboPosition(
            leg_indices=(0, 1), side="yes", contracts=2, price_cc=2_000, fee_cc=50
        )
        without, with_ = marginal_impact(legs, corr2(0.2), [], candidate, n_samples=N, seed=21)
        alone = simulate(legs, corr2(0.2), [candidate], n_samples=N, seed=21)
        assert np.all(without.pnl_samples == 0.0)
        assert np.array_equal(with_.pnl_samples, alone.pnl_samples)
        assert with_.ev_cc == alone.ev_cc
        assert with_.std_cc == alone.std_cc
        assert with_.var_cc == alone.var_cc

    def test_common_random_numbers_deterministic_candidate(self) -> None:
        # Candidate settles to 0.4 in every scenario -> with-minus-without P&L
        # difference must be the same constant in every sample (zero variance).
        legs = (LegModel(p=0.5), LegModel(p=0.5, settlement=((0.4, 1.0),)))
        book = (ComboPosition(leg_indices=(0,), side="yes", contracts=1, price_cc=5_000),)
        candidate = ComboPosition(
            leg_indices=(1,), side="yes", contracts=2, price_cc=3_000, fee_cc=100
        )
        without, with_ = marginal_impact(legs, eye(2), book, candidate, n_samples=50_000, seed=3)
        diff = with_.pnl_samples - without.pnl_samples
        assert float(np.var(diff)) == 0.0
        assert diff[0] == (0.4 * DOLLAR_CC - 3_000) * 2 - 100


class TestLegDeltas:
    def test_two_leg_combo_deltas(self) -> None:
        legs = (LegModel(p=0.5), LegModel(p=0.6), LegModel(p=0.5))
        pos = ComboPosition(leg_indices=(0, 1), side="yes", contracts=1, price_cc=0)
        n = 100_000
        deltas = leg_deltas(legs, eye(3), pos, n_samples=n, seed=31)
        # Forcing leg 0 to 1 vs 0 changes payout by 10_000 * value(leg 1).
        sigma0 = DOLLAR_CC * math.sqrt(0.6 * 0.4) / math.sqrt(n)
        sigma1 = DOLLAR_CC * math.sqrt(0.5 * 0.5) / math.sqrt(n)
        assert abs(deltas[0] - DOLLAR_CC * 0.6) < 3.0 * sigma0
        assert abs(deltas[1] - DOLLAR_CC * 0.5) < 3.0 * sigma1
        assert deltas[2] == 0.0

    def test_no_side_delta_is_negative(self) -> None:
        legs = (LegModel(p=0.5), LegModel(p=0.6))
        pos = ComboPosition(leg_indices=(0, 1), side="no", contracts=1, price_cc=4_000)
        deltas = leg_deltas(legs, eye(2), pos, n_samples=50_000, seed=33)
        assert deltas[0] < 0.0
        assert deltas[1] < 0.0


class TestRiskStats:
    def test_var_es_symmetric_two_sided_book(self) -> None:
        legs = (LegModel(p=0.5), LegModel(p=0.5))
        book = (
            ComboPosition(leg_indices=(0,), side="yes", contracts=1, price_cc=5_000),
            ComboPosition(leg_indices=(1,), side="no", contracts=1, price_cc=5_000),
        )
        stats = simulate(legs, eye(2), book, n_samples=N, seed=8, loss_thresholds_cc=(5_000,))
        for level in (0.95, 0.99):
            assert stats.var_cc[level] > 0.0
            assert stats.es_cc[level] > 0.0
            assert stats.es_cc[level] >= stats.var_cc[level] - 1e-9
        # P&L is -10_000 / 0 / +10_000 with probs 1/4, 1/2, 1/4.
        assert abs(stats.p_loss_worse_than[5_000.0] - 0.25) < 0.01
        assert abs(stats.p_profit - 0.25) < 0.01
        assert abs(stats.ev_cc) < 3.0 * stats.std_cc / math.sqrt(N)

    def test_fee_subtracted_exactly_once(self) -> None:
        legs = (LegModel(p=0.5),)
        base = ComboPosition(leg_indices=(0,), side="yes", contracts=3, price_cc=4_000)
        with_fee = ComboPosition(
            leg_indices=(0,), side="yes", contracts=3, price_cc=4_000, fee_cc=500
        )
        s0 = simulate(legs, eye(1), [base], n_samples=20_000, seed=44)
        s1 = simulate(legs, eye(1), [with_fee], n_samples=20_000, seed=44)
        # Fee is per position, not per contract: shift is exactly 500 cc.
        assert np.array_equal(s1.pnl_samples, s0.pnl_samples - 500.0)
        assert s1.ev_cc == pytest.approx(s0.ev_cc - 500.0, abs=1e-9)


class TestValidation:
    def test_settlement_probs_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError):
            LegModel(p=0.5, settlement=((0.0, 0.5), (1.0, 0.4)))

    def test_settlement_value_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            LegModel(p=0.5, settlement=((1.5, 1.0),))

    def test_p_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            LegModel(p=1.2)

    def test_contracts_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ComboPosition(leg_indices=(0,), side="yes", contracts=0, price_cc=5_000)

    def test_price_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            ComboPosition(leg_indices=(0,), side="yes", contracts=1, price_cc=10_001)

    def test_corr_shape_mismatch(self) -> None:
        rng = np.random.default_rng(0)
        with pytest.raises(ValueError):
            sample_leg_values((LegModel(p=0.5),), eye(2), 100, rng)
