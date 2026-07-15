import pytest

from combomaker.core.money import (
    CC_PER_DOLLAR,
    CentiCents,
    MoneyParseError,
    cc_from_cents,
    cc_from_dollars_str,
    cc_from_prob,
    cc_to_dollars_str,
    prob_from_cc,
    round_to_tick,
)


class TestParsing:
    @pytest.mark.parametrize(
        ("wire", "cc"),
        [
            ("0", 0),
            ("1", 10_000),
            ("0.56", 5_600),
            ("0.5600", 5_600),
            ("0.0001", 1),
            ("12.3456", 123_456),
            ("-0.05", -500),
        ],
    )
    def test_exact_parse(self, wire: str, cc: int) -> None:
        assert cc_from_dollars_str(wire) == cc

    def test_sub_centicent_rejected(self) -> None:
        with pytest.raises(MoneyParseError):
            cc_from_dollars_str("0.00005")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(MoneyParseError):
            cc_from_dollars_str("abc")

    def test_float_string_precision_never_used(self) -> None:
        # The classic 0.1+0.2 trap must be impossible: parsing goes through Decimal.
        assert cc_from_dollars_str("0.3") == 3_000

    def test_roundtrip(self) -> None:
        for cc in (0, 1, 99, 5_600, 10_000, 123_456):
            assert cc_from_dollars_str(cc_to_dollars_str(CentiCents(cc))) == cc

    def test_format_fewer_places_raises_when_inexact(self) -> None:
        with pytest.raises(MoneyParseError):
            cc_to_dollars_str(CentiCents(1), places=2)

    def test_format_fewer_places_ok_when_exact(self) -> None:
        assert cc_to_dollars_str(CentiCents(5_600), places=2) == "0.56"


class TestConversions:
    def test_cents(self) -> None:
        assert cc_from_cents(56) == 5_600

    def test_prob_roundtrip(self) -> None:
        assert prob_from_cc(CentiCents(5_600)) == pytest.approx(0.56)
        assert cc_from_prob(0.56) == 5_600

    def test_prob_bounds(self) -> None:
        with pytest.raises(ValueError):
            cc_from_prob(1.5)
        with pytest.raises(ValueError):
            cc_from_prob(-0.1)

    def test_prob_rounding_directions(self) -> None:
        # 0.56789 * 10_000 = 5678.9
        assert cc_from_prob(0.56789, "down") == 5_678
        assert cc_from_prob(0.56789, "up") == 5_679
        assert cc_from_prob(0.56789, "nearest") == 5_679


class TestTickRounding:
    def test_on_grid_unchanged(self) -> None:
        tick = CentiCents(100)  # one cent
        assert round_to_tick(CentiCents(5_600), tick, "up") == 5_600
        assert round_to_tick(CentiCents(5_600), tick, "down") == 5_600

    def test_directions(self) -> None:
        tick = CentiCents(100)
        assert round_to_tick(CentiCents(5_650), tick, "down") == 5_600
        assert round_to_tick(CentiCents(5_650), tick, "up") == 5_700
        assert round_to_tick(CentiCents(5_650), tick, "nearest") == 5_700
        assert round_to_tick(CentiCents(5_649), tick, "nearest") == 5_600

    def test_bad_tick(self) -> None:
        with pytest.raises(ValueError):
            round_to_tick(CentiCents(5_600), CentiCents(0), "down")

    def test_full_dollar_is_whole_number_of_common_ticks(self) -> None:
        assert CC_PER_DOLLAR % 100 == 0
        assert CC_PER_DOLLAR % 25 == 0


class TestFeeCcFromDollarsStr:
    """Fees can be SUB-centi-cent on the Kalshi wire (observed live: a combo
    settlement ``fee_cost='0.000080'`` = 0.8 cc). Prices/revenue stay exact; a fee
    is booked at cc granularity, rounded UP so we never understate a cost we paid."""

    def test_sub_cc_fee_rounds_up(self) -> None:
        from combomaker.core.money import fee_cc_from_dollars_str

        assert int(fee_cc_from_dollars_str("0.000080")) == 1   # 0.8 cc → 1
        assert int(fee_cc_from_dollars_str("0.00011")) == 2    # 1.1 cc → 2
        assert int(fee_cc_from_dollars_str("0.0001")) == 1     # exactly 1 cc
        assert int(fee_cc_from_dollars_str("0")) == 0
        assert int(fee_cc_from_dollars_str("0.5600")) == 5600  # whole cc unchanged

    def test_negative_or_garbage_raises(self) -> None:
        import pytest

        from combomaker.core.money import MoneyParseError, fee_cc_from_dollars_str

        with pytest.raises(MoneyParseError):
            fee_cc_from_dollars_str("-0.01")
        with pytest.raises(MoneyParseError):
            fee_cc_from_dollars_str("abc")
