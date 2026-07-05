"""Tests for combomaker.pricing.fees — exact integer fee math and fail-safe attribution.

Expected values are computed independently from first principles with Fraction
arithmetic: fee = coef × multiplier × contracts × P × (1−P) dollars, ceiled to
the nearest centi-cent (the exchange rounds trade fees UP to $0.0001).
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import DOC_ASSUMED, Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType, FeeUnknownError

TAKER_COEF = Fraction(7, 100)
MAKER_COEF = Fraction(7, 400)

ONE_CONTRACT = CentiContracts(100)


def make_conventions(maker_is_taker_on_fill: bool | None) -> Conventions:
    """Build a Conventions instance directly (no fixture machinery)."""
    return Conventions(
        verified=False,
        source="test",
        maker_side_on_yes_accept=Side.YES,
        maker_side_on_no_accept=Side.NO,
        maker_pays_own_bid=True,
        maker_is_taker_on_fill=maker_is_taker_on_fill,
        combo_no_pays_complement=None,
    )


def make_model(maker_is_taker_on_fill: bool | None) -> FeeModel:
    schedule = FeeSchedule.from_strings("0.07", "0.0175")
    return FeeModel(schedule, make_conventions(maker_is_taker_on_fill))


def exact_fee_cc(
    coef: Fraction, qty: int, price_cc: int, multiplier: Fraction = Fraction(1)
) -> Fraction:
    """First-principles exact fee in centi-cents (pre-ceiling)."""
    contracts = Fraction(qty, 100)
    p = Fraction(price_cc, CC_PER_DOLLAR)
    return coef * multiplier * contracts * p * (1 - p) * CC_PER_DOLLAR


class TestScheduleParsing:
    def test_decimal_strings_become_exact_fractions(self) -> None:
        schedule = FeeSchedule.from_strings("0.07", "0.0175")
        assert schedule.taker_coef == TAKER_COEF
        assert schedule.maker_coef == MAKER_COEF


class TestWorkedExamples:
    def test_taker_one_contract_at_50_cents_is_175_cc(self) -> None:
        # 0.07 × 1 × 0.5 × 0.5 = $0.0175 exactly = 175 cc, no rounding needed.
        model = make_model(True)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(5_000), qty=ONE_CONTRACT, fee_type=FeeType.QUADRATIC
        )
        assert fee == 175
        assert exact_fee_cc(TAKER_COEF, 100, 5_000) == 175  # integral: ceil is a no-op

    def test_taker_one_contract_at_56_cents_ceils_to_173_cc(self) -> None:
        # 0.07 × 1 × 0.56 × 0.44 × 10^4 = 172.48 cc → ceil = 173 cc.
        model = make_model(True)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(5_600), qty=ONE_CONTRACT, fee_type=FeeType.QUADRATIC
        )
        exact = exact_fee_cc(TAKER_COEF, 100, 5_600)
        assert exact == Fraction(17248, 100)  # 172.48, fractional
        assert fee == math.ceil(exact) == 173


class TestCeilingBehavior:
    def test_fractional_exact_fee_rounds_up(self) -> None:
        # price 12.34¢: exact fee = 7·1234·8766/10^6 = 75.720708 cc → 76 cc.
        model = make_model(True)
        exact = exact_fee_cc(TAKER_COEF, 100, 1_234)
        assert exact.denominator != 1  # genuinely fractional case
        fee = model.trade_fee_cc(
            price_cc=CentiCents(1_234), qty=ONE_CONTRACT, fee_type=FeeType.QUADRATIC
        )
        assert fee == math.ceil(exact) == 76
        assert fee > exact  # ceiling, never truncation


class TestFeeAttribution:
    def test_verified_maker_uses_maker_coef_on_maker_fee_series(self) -> None:
        model = make_model(False)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(5_000),
            qty=ONE_CONTRACT,
            fee_type=FeeType.QUADRATIC_WITH_MAKER_FEES,
        )
        # 0.0175 × 1 × 0.25 × 10^4 = 43.75 cc → ceil = 44 cc.
        assert fee == math.ceil(exact_fee_cc(MAKER_COEF, 100, 5_000)) == 44

    def test_verified_maker_pays_zero_on_plain_quadratic_series(self) -> None:
        model = make_model(False)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(5_000), qty=ONE_CONTRACT, fee_type=FeeType.QUADRATIC
        )
        assert fee == 0

    def test_verified_taker_uses_taker_coef_on_both_series(self) -> None:
        model = make_model(True)
        for fee_type in (FeeType.QUADRATIC, FeeType.QUADRATIC_WITH_MAKER_FEES):
            fee = model.trade_fee_cc(
                price_cc=CentiCents(5_000), qty=ONE_CONTRACT, fee_type=fee_type
            )
            assert fee == 175

    def test_doc_assumed_unknown_attribution_prices_as_taker(self) -> None:
        # FAIL-SAFE: maker_is_taker_on_fill=None must use the TAKER coefficient
        # even on maker-fee series — conservative (widens quotes), never the
        # convenient cheap assumption.
        assert DOC_ASSUMED.maker_is_taker_on_fill is None
        schedule = FeeSchedule.from_strings("0.07", "0.0175")
        model = FeeModel(schedule, DOC_ASSUMED)
        taker_model = make_model(True)
        for price in (1_234, 5_000, 5_600, 9_900):
            fee = model.trade_fee_cc(
                price_cc=CentiCents(price),
                qty=ONE_CONTRACT,
                fee_type=FeeType.QUADRATIC_WITH_MAKER_FEES,
            )
            expected = taker_model.trade_fee_cc(
                price_cc=CentiCents(price),
                qty=ONE_CONTRACT,
                fee_type=FeeType.QUADRATIC_WITH_MAKER_FEES,
            )
            assert fee == expected


class TestFeeUnknown:
    @pytest.mark.parametrize("fee_type", [FeeType.FLAT, FeeType.UNKNOWN])
    @pytest.mark.parametrize("attribution", [True, False, None])
    def test_flat_and_unknown_never_produce_a_number(
        self, fee_type: FeeType, attribution: bool | None
    ) -> None:
        model = make_model(attribution)
        with pytest.raises(FeeUnknownError):
            model.trade_fee_cc(price_cc=CentiCents(5_000), qty=ONE_CONTRACT, fee_type=fee_type)

    def test_parse_garbage_is_unknown(self) -> None:
        assert FeeType.parse("garbage") is FeeType.UNKNOWN
        assert FeeType.parse(None) is FeeType.UNKNOWN
        assert FeeType.parse("") is FeeType.UNKNOWN

    def test_parse_known_values(self) -> None:
        assert FeeType.parse("quadratic") is FeeType.QUADRATIC
        assert FeeType.parse("quadratic_with_maker_fees") is FeeType.QUADRATIC_WITH_MAKER_FEES
        assert FeeType.parse("flat") is FeeType.FLAT

    def test_fee_unknown_error_is_a_value_error(self) -> None:
        assert issubclass(FeeUnknownError, ValueError)


class TestMultiplier:
    def test_three_halves_scales_exactly_before_ceiling(self) -> None:
        # At 56¢ the exact fee is 172.48 cc; ×3/2 = 258.72 → ceil 259.
        # A ceil-then-scale implementation would give ceil(173 × 3/2) = 260.
        model = make_model(True)
        mult = Fraction("3/2")
        fee = model.trade_fee_cc(
            price_cc=CentiCents(5_600),
            qty=ONE_CONTRACT,
            fee_type=FeeType.QUADRATIC,
            multiplier=mult,
        )
        exact = exact_fee_cc(TAKER_COEF, 100, 5_600, mult)
        assert exact == Fraction(25872, 100)
        assert fee == math.ceil(exact) == 259

    def test_integral_scaled_fee_needs_no_rounding(self) -> None:
        # 175 cc × 2 = 350 cc exactly.
        model = make_model(True)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(5_000),
            qty=ONE_CONTRACT,
            fee_type=FeeType.QUADRATIC,
            multiplier=Fraction(2),
        )
        assert fee == 350


class TestSymmetry:
    @pytest.mark.parametrize("price", [1, 777, 1_234, 3_000, 4_999, 5_000])
    def test_fee_symmetric_around_50_cents(self, price: int) -> None:
        model = make_model(True)
        low = model.trade_fee_cc(
            price_cc=CentiCents(price), qty=CentiContracts(3_700), fee_type=FeeType.QUADRATIC
        )
        high = model.trade_fee_cc(
            price_cc=CentiCents(CC_PER_DOLLAR - price),
            qty=CentiContracts(3_700),
            fee_type=FeeType.QUADRATIC,
        )
        assert low == high


class TestValidation:
    @pytest.mark.parametrize("price", [-1, 10_001, 20_000])
    def test_price_out_of_range_raises(self, price: int) -> None:
        model = make_model(True)
        with pytest.raises(ValueError, match="price out of range"):
            model.trade_fee_cc(
                price_cc=CentiCents(price), qty=ONE_CONTRACT, fee_type=FeeType.QUADRATIC
            )

    def test_negative_quantity_raises(self) -> None:
        model = make_model(True)
        with pytest.raises(ValueError, match="negative quantity"):
            model.trade_fee_cc(
                price_cc=CentiCents(5_000), qty=CentiContracts(-1), fee_type=FeeType.QUADRATIC
            )


class TestBoundaries:
    @pytest.mark.parametrize("price", [0, 10_000])
    def test_zero_fee_at_price_extremes(self, price: int) -> None:
        model = make_model(True)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(price), qty=CentiContracts(1_000_000), fee_type=FeeType.QUADRATIC
        )
        assert fee == 0

    def test_zero_quantity_is_zero_fee(self) -> None:
        model = make_model(True)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(5_000), qty=CentiContracts(0), fee_type=FeeType.QUADRATIC
        )
        assert fee == 0


class TestPerContract:
    def test_per_contract_matches_trade_fee_for_one_contract(self) -> None:
        model = make_model(True)
        for price in (1_234, 5_000, 5_600):
            per = model.fee_per_contract_cc(price_cc=CentiCents(price), fee_type=FeeType.QUADRATIC)
            trade = model.trade_fee_cc(
                price_cc=CentiCents(price), qty=ONE_CONTRACT, fee_type=FeeType.QUADRATIC
            )
            assert per == trade


class TestProperties:
    @settings(derandomize=True, max_examples=300)
    @given(price=st.integers(0, CC_PER_DOLLAR), qty=st.integers(1, 1_000_000))
    def test_ceil_within_one_centicent_of_exact(self, price: int, qty: int) -> None:
        model = make_model(True)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(price), qty=CentiContracts(qty), fee_type=FeeType.QUADRATIC
        )
        exact = exact_fee_cc(TAKER_COEF, qty, price)
        assert fee >= exact  # never undercharges (rounds up)
        assert fee - exact < 1  # ceil overshoot strictly under one centi-cent

    @settings(derandomize=True, max_examples=200)
    @given(
        price=st.integers(0, CC_PER_DOLLAR),
        qty_a=st.integers(1, 1_000_000),
        qty_b=st.integers(1, 1_000_000),
    )
    def test_monotone_nondecreasing_in_quantity(self, price: int, qty_a: int, qty_b: int) -> None:
        lo, hi = sorted((qty_a, qty_b))
        model = make_model(True)
        fee_lo = model.trade_fee_cc(
            price_cc=CentiCents(price), qty=CentiContracts(lo), fee_type=FeeType.QUADRATIC
        )
        fee_hi = model.trade_fee_cc(
            price_cc=CentiCents(price), qty=CentiContracts(hi), fee_type=FeeType.QUADRATIC
        )
        assert fee_lo <= fee_hi

    @settings(derandomize=True, max_examples=200)
    @given(price=st.integers(0, CC_PER_DOLLAR), qty=st.integers(1, 1_000_000))
    def test_symmetry_property(self, price: int, qty: int) -> None:
        model = make_model(True)
        fee = model.trade_fee_cc(
            price_cc=CentiCents(price), qty=CentiContracts(qty), fee_type=FeeType.QUADRATIC
        )
        mirrored = model.trade_fee_cc(
            price_cc=CentiCents(CC_PER_DOLLAR - price),
            qty=CentiContracts(qty),
            fee_type=FeeType.QUADRATIC,
        )
        assert fee == mirrored
