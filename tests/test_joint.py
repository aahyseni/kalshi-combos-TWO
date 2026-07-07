"""Tests for combomaker.pricing.joint — joint probability with priced uncertainty."""

import math

import pytest

from combomaker.pricing.joint import (
    CorrelationParams,
    JointEstimate,
    price_containment,
    price_joint,
)
from combomaker.pricing.legs import LegBelief

INDEPENDENT = CorrelationParams(same_event_rho=0.0, cross_event_rho=0.0, rho_uncertainty=0.0)


def belief(p: float, uncertainty: float = 0.0) -> LegBelief:
    return LegBelief(p=p, uncertainty=uncertainty, source="test")


class TestIndependence:
    def test_no_groups_zero_cross_rho_is_product_of_selected_marginals(self) -> None:
        beliefs = [belief(0.3), belief(0.6), belief(0.85)]
        est = price_joint(beliefs, ["yes", "no", "yes"], [], INDEPENDENT)
        assert isinstance(est, JointEstimate)
        expected = 0.3 * (1.0 - 0.6) * 0.85  # NO leg contributes 1 - p
        assert abs(est.p - expected) < 1e-9
        assert est.uncertainty == 0.0
        assert est.notes == ()

    def test_all_no_sides_use_complements(self) -> None:
        est = price_joint([belief(0.2), belief(0.7)], ["no", "no"], [], INDEPENDENT)
        assert abs(est.p - 0.8 * 0.3) < 1e-9


class TestSignConjugation:
    def test_yes_no_pair_flips_rho_sign(self) -> None:
        """One YES + one NO leg in a rho=0.6 group prices at the FLIPPED rho (-0.6).

        With p1 = p2 = 0.5 both latent thresholds are 0, so the joint is the
        Gaussian orthant probability P(Z1 <= 0, Z2 <= 0) under rho = -0.6:
        1/4 + arcsin(-0.6) / (2*pi)  ==  1/4 - arcsin(0.6) / (2*pi).
        """
        params = CorrelationParams(same_event_rho=0.6, cross_event_rho=0.0, rho_uncertainty=0.0)
        est = price_joint([belief(0.5), belief(0.5)], ["yes", "no"], [[0, 1]], params)
        expected = 0.25 + math.asin(-0.6) / (2.0 * math.pi)
        assert est.p == pytest.approx(expected, abs=1e-6)
        assert est.p < 0.25  # anti-correlated after the flip: below independence


class TestUncertainty:
    def test_rho_uncertainty_widens_and_adds_sensitivity_note(self) -> None:
        beliefs = [belief(0.5), belief(0.5)]
        groups = [[0, 1]]
        certain = CorrelationParams(same_event_rho=0.3, cross_event_rho=0.0, rho_uncertainty=0.0)
        fuzzy = CorrelationParams(same_event_rho=0.3, cross_event_rho=0.0, rho_uncertainty=0.2)
        est_certain = price_joint(beliefs, ["yes", "yes"], groups, certain)
        est_fuzzy = price_joint(beliefs, ["yes", "yes"], groups, fuzzy)
        assert est_certain.uncertainty == 0.0  # zero leg unc, zero rho unc
        assert est_fuzzy.uncertainty > est_certain.uncertainty
        assert any(note.startswith("rho sensitivity") for note in est_fuzzy.notes)
        assert est_certain.notes == ()

    def test_leg_uncertainty_propagates_and_grows(self) -> None:
        lo = price_joint([belief(0.4, 0.01), belief(0.5, 0.01)], ["yes", "yes"], [], INDEPENDENT)
        hi = price_joint([belief(0.4, 0.05), belief(0.5, 0.01)], ["yes", "yes"], [], INDEPENDENT)
        assert hi.uncertainty > lo.uncertainty
        # conservative linear sum: P * sum(u_i / m_i)
        assert lo.uncertainty == pytest.approx(0.2 * (0.01 / 0.4 + 0.01 / 0.5))

    def test_near_zero_marginal_gradient_clamped_not_exploding(self) -> None:
        est = price_joint([belief(0.001, 0.02), belief(0.5)], ["yes", "yes"], [], INDEPENDENT)
        assert math.isfinite(est.uncertainty)
        # divisor clamps at 0.01, so 0.0005 * (0.02 / 0.01) = 0.001, not 0.01
        assert est.uncertainty == pytest.approx(0.0005 * (0.02 / 0.01))
        marginals = [0.001, 0.5]
        frechet_lo = max(0.0, sum(marginals) - (len(marginals) - 1))
        frechet_hi = min(marginals)
        assert est.frechet_lo == pytest.approx(frechet_lo)
        assert est.frechet_hi == pytest.approx(frechet_hi)
        assert frechet_lo <= est.p <= frechet_hi  # estimate is Frechet-clamped


class TestContainment:
    def test_joint_is_exactly_the_subset_marginal(self) -> None:
        # 1H-BTTS (subset) ⟹ FT-BTTS (superset): joint == P(1H-BTTS), NOT the
        # independence product 0.30*0.55.
        est = price_containment([belief(0.30, 0.02), belief(0.55, 0.04)], ["yes", "yes"], (0, 1))
        assert est.p == pytest.approx(0.30)
        assert est.p != pytest.approx(0.30 * 0.55)
        assert est.uncertainty == pytest.approx(0.02)  # tracks the subset leg
        assert any("containment" in n for n in est.notes)

    def test_inconsistent_market_clamps_to_frechet_upper(self) -> None:
        # If the book misprices the subset above the superset, the joint can't
        # exceed the smaller marginal (Fréchet upper bound).
        est = price_containment([belief(0.60), belief(0.55)], ["yes", "yes"], (0, 1))
        assert est.p == pytest.approx(0.55)

    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError):
            price_containment([belief(0.5)], ["yes", "yes"], (0, 1))


class TestValidation:
    def test_mismatched_lengths_raise(self) -> None:
        with pytest.raises(ValueError):
            price_joint([belief(0.5)], ["yes", "no"], [], INDEPENDENT)

    def test_empty_input_raises(self) -> None:
        with pytest.raises(ValueError):
            price_joint([], [], [], INDEPENDENT)
