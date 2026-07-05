"""Regression tests for the two source fixes from the Phase 3 test sweep."""

from fractions import Fraction

from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.grid import PriceGrid
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import ConstructedQuote, construct_quote
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.rfq.models import RfqLeg


class RaisingProvider:
    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        raise AssertionError("provider must not be consulted for unknown sides")


def test_unknown_side_is_unknown_inside_classifier_too() -> None:
    """Defense in depth: a 'maybe' side must not be counted as NO."""
    legs = (
        RfqLeg("M1", "E1", "maybe", None),
        RfqLeg("M2", "E1", "yes", None),
    )
    result = classify_legs(legs, RaisingProvider())
    assert result.kind is RelationshipKind.UNKNOWN
    assert any("unknown side" in note for note in result.notes)


def _quote(fair_prob: float) -> ConstructedQuote:
    grid = PriceGrid.from_market_payload(
        {"ticker": "T", "price_ranges": [{"start": "0.001", "end": "0.999", "step": "0.001"}]}
    )
    fee_model = FeeModel(FeeSchedule.from_strings("0.07", "0.0175"), DOC_ASSUMED)
    result = construct_quote(
        joint=JointEstimate(
            p=fair_prob, uncertainty=0.0, frechet_lo=0.0, frechet_hi=1.0, notes=()
        ),
        n_legs=2,
        qty=CentiContracts(1_000),
        grid=grid,
        fee_model=fee_model,
        fee_type=FeeType.QUADRATIC,
        fee_multiplier=Fraction(1),
        time_to_close_s=1e9,
        in_play=False,
        yes_cap_cc=CentiCents(9_900),
        no_cap_cc=CentiCents(9_900),
    )
    assert isinstance(result, ConstructedQuote)
    return result


def test_fee_subtraction_covers_fee_at_the_actual_bid() -> None:
    """The fee the exchange will charge at OUR fill price must be fully paid
    for by the subtraction — for fair on both sides of $0.50."""
    fee_model = FeeModel(FeeSchedule.from_strings("0.07", "0.0175"), DOC_ASSUMED)
    for fair_prob in (0.30, 0.50, 0.70, 0.90):
        quote = _quote(fair_prob)
        fair_cc = quote.fair_cc
        half = quote.total_width_cc // 2
        for bid, side_fair in (
            (quote.yes_bid_cc, int(fair_cc)),
            (quote.no_bid_cc, 10_000 - int(fair_cc)),
        ):
            if bid == 0:
                continue
            fee_at_bid = int(
                fee_model.fee_per_contract_cc(price_cc=bid, fee_type=FeeType.QUADRATIC)
            )
            # margin captured at the fill = side fair − bid; it must cover the
            # half width AND the true fee at the bid.
            assert side_fair - int(bid) >= half + fee_at_bid, (
                f"fair_prob={fair_prob}, side_fair={side_fair}, bid={int(bid)}, "
                f"half={half}, fee_at_bid={fee_at_bid}"
            )
