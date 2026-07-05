import pytest

from combomaker.core.money import CentiCents
from combomaker.marketdata.grid import GridError, PriceGrid


def cents_grid() -> PriceGrid:
    """Plain 1-cent grid from $0.01 to $0.99."""
    return PriceGrid.from_market_payload(
        {"ticker": "T", "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}]}
    )


def tapered_grid() -> PriceGrid:
    """Deci-cent tails, cent middle (tapered structure)."""
    return PriceGrid.from_market_payload(
        {
            "ticker": "T",
            "price_ranges": [
                {"start": "0.001", "end": "0.10", "step": "0.001"},
                {"start": "0.10", "end": "0.90", "step": "0.01"},
                {"start": "0.90", "end": "0.999", "step": "0.001"},
            ],
        }
    )


class TestParsing:
    def test_missing_price_ranges_is_no_quote_not_default(self) -> None:
        with pytest.raises(GridError):
            PriceGrid.from_market_payload({"ticker": "T"})

    def test_malformed_entry(self) -> None:
        with pytest.raises(GridError):
            PriceGrid.from_market_payload(
                {"ticker": "T", "price_ranges": [{"start": "0.01", "end": "0.99"}]}
            )

    def test_zero_step_rejected(self) -> None:
        with pytest.raises(GridError):
            PriceGrid.from_market_payload(
                {"ticker": "T", "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0"}]}
            )


class TestOnGrid:
    def test_cent_grid(self) -> None:
        g = cents_grid()
        assert g.is_on_grid(CentiCents(5_600))
        assert not g.is_on_grid(CentiCents(5_650))  # half-cent off a cent grid
        assert not g.is_on_grid(CentiCents(0))      # below range

    def test_tapered(self) -> None:
        g = tapered_grid()
        assert g.is_on_grid(CentiCents(50))      # $0.005 on the deci-cent tail
        assert not g.is_on_grid(CentiCents(55))  # $0.0055 is half a deci-cent
        assert g.is_on_grid(CentiCents(5_600))   # $0.56 in the cent middle
        assert not g.is_on_grid(CentiCents(5_650))  # deci-cent price in cent zone
        assert g.is_on_grid(CentiCents(9_990))   # $0.999 top of upper tail


class TestMakerFavorableRounding:
    def test_bid_rounds_down_never_nearest(self) -> None:
        g = cents_grid()
        # $0.5699 must round DOWN to $0.56 even though $0.57 is nearer.
        assert g.snap_bid_down(CentiCents(5_699)) == 5_600

    def test_on_grid_unchanged(self) -> None:
        g = cents_grid()
        assert g.snap_bid_down(CentiCents(5_600)) == 5_600

    def test_below_grid_is_unquotable(self) -> None:
        g = cents_grid()
        assert g.snap_bid_down(CentiCents(50)) is None  # below $0.01

    def test_above_grid_clamps_to_top(self) -> None:
        g = cents_grid()
        assert g.snap_bid_down(CentiCents(9_999)) == 9_900

    def test_tapered_boundary(self) -> None:
        g = tapered_grid()
        # $0.1005 sits in the cent zone: down to $0.10
        assert g.snap_bid_down(CentiCents(1_005)) == 1_000
        # $0.0999 is off the deci-cent lattice: down to $0.099
        assert g.snap_bid_down(CentiCents(999)) == 990

    def test_snap_up_for_ask_walks(self) -> None:
        g = cents_grid()
        assert g.snap_up(CentiCents(5_601)) == 5_700
        assert g.snap_up(CentiCents(5_600)) == 5_600
        assert g.snap_up(CentiCents(9_950)) is None  # above top of grid
