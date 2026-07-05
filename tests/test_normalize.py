import math

import pytest

from combomaker.pricing.normalize import (
    NormalizeMethod,
    normalize_exclusive_family,
    normalize_power,
    normalize_proportional,
    validate_probability_vector,
)


class TestExclusiveFamily:
    def test_overdispersed_mids_proportional(self) -> None:
        # Two mutually exclusive Kalshi markets whose mids sum to 1.05
        out = normalize_exclusive_family([0.55, 0.50])
        assert out == pytest.approx([0.55 / 1.05, 0.50 / 1.05])
        assert math.fsum(out) == pytest.approx(1.0)

    def test_underdispersed_mids(self) -> None:
        # Sum 0.90 — thin books can under-round; renormalizes upward
        out = normalize_exclusive_family([0.30, 0.30, 0.30])
        assert out == pytest.approx([1 / 3, 1 / 3, 1 / 3])

    def test_already_coherent_family_unchanged(self) -> None:
        out = normalize_exclusive_family([0.25, 0.75])
        assert out == pytest.approx([0.25, 0.75])

    def test_power_method_sums_to_one_and_orders_preserved(self) -> None:
        mids = [0.62, 0.30, 0.15]
        out = normalize_exclusive_family(mids, NormalizeMethod.POWER)
        assert math.fsum(out) == pytest.approx(1.0, abs=1e-12)
        assert sorted(out, reverse=True) == out  # order preserved

    def test_validation(self) -> None:
        with pytest.raises(ValueError):
            normalize_exclusive_family([0.5])
        with pytest.raises(ValueError):
            normalize_exclusive_family([0.5, 1.0])
        with pytest.raises(ValueError):
            normalize_exclusive_family([0.5, 0.0])


class TestSolvers:
    def test_proportional_rejects_degenerate_sum(self) -> None:
        with pytest.raises(ValueError):
            normalize_proportional([0.0, 0.0])

    def test_power_matches_proportional_on_symmetric_vector(self) -> None:
        prop = normalize_proportional([0.52, 0.52])
        power = normalize_power([0.52, 0.52])
        assert power == pytest.approx(prop)

    def test_validate_returns_floats(self) -> None:
        out = validate_probability_vector([0.5, 0.5])
        assert all(isinstance(p, float) for p in out)
