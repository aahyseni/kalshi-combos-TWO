"""construct_quote fee_mode floor|width (2026-09-04 build A item 5).

Pins, all through the REAL construct_quote with the MEASURED 0.035 combo
maker schedule:

- "width" reproduces the pre-existing arithmetic exactly (the fee is
  subtracted; rebate <= margin // 2);
- "floor" on a mains tier (markup clears the fee) at zero skew is BYTE-
  IDENTICAL to the fee-blind price (today's live output, fee 0);
- "floor" on the razor (markup below the fee) posts EXACTLY fair − fee (the
  razor dissolves into max(razor, measured fee));
- the rebate is capped at margin − fee; with a measured cell floor the cap
  becomes margin − fee − floor and REPLACES the margin // 2 hand fraction;
- the fee-blind bid >= floor bid >= width bid on every input (property);
- an UNKNOWN fee type still cannot quote in either mode;
- a bad mode string is refused.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.quote import ConstructedQuote, NoQuote, QuoteParams, construct_quote
from combomaker.pricing.retained_cell import cell_key
from tests.test_quote import deci_grid, make_joint

VERIFIED_MAKER = Conventions(
    verified=True,
    source="test fixture",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)
MEASURED = FeeModel(
    FeeSchedule(taker_coef=Fraction(7, 100), maker_coef=Fraction(35, 1000)), VERIFIED_MAKER
)
COMBO = FeeType.QUADRATIC_WITH_COMBO_MAKER_FEES
# The live sell-only quote posture: no defensive width components (the live
# yaml zeroes base/per-leg/size), so margin == markup.
PARAMS = QuoteParams(
    base_width_cc=0, per_leg_width_cc=0, size_width_cc_per_100=0,
    time_width_cc=0, sell_parlays_only=True, free_money_margin_cc=100,
)


def quote(
    *, fair: float, markup: int, skew: int = 0, mode: str, fee_type: FeeType = COMBO,
    retained_floor: int | None = None, params: QuoteParams = PARAMS,
) -> ConstructedQuote | NoQuote:
    return construct_quote(
        joint=make_joint(fair),
        n_legs=2,
        qty=CentiContracts(1_000),
        grid=deci_grid(),
        fee_model=MEASURED,
        fee_type=fee_type,
        fee_multiplier=Fraction(1),
        time_to_close_s=10 * 3600.0,
        in_play=False,
        yes_cap_cc=CentiCents(CC_PER_DOLLAR),
        no_cap_cc=CentiCents(CC_PER_DOLLAR),
        inventory_skew_cc=skew,
        markup_cc=markup,
        params=params,
        fee_mode=mode,
        retained_floor_cc=retained_floor,
    )


def fee_cc(price_cc: int) -> int:
    return int(MEASURED.fee_per_contract_cc(price_cc=CentiCents(price_cc), fee_type=COMBO))


def no_bid(q: ConstructedQuote | NoQuote) -> int:
    assert isinstance(q, ConstructedQuote), q
    return int(q.no_bid_cc)


def snap_down(raw: int) -> int:
    return raw // 10 * 10  # the 0.1c combo grid


def today_fee_blind(fair: float, markup: int, skew: int) -> int:
    """The live pre-2026-09-04 arithmetic (fee 0): fair_no − margin + rebate,
    rebate <= margin // 2, snapped DOWN."""
    q = quote(fair=fair, markup=markup, skew=skew, mode="width", fee_type=FeeType.QUADRATIC)
    return no_bid(q)


# ------------------------------------------------------------------- width


@pytest.mark.parametrize("fair", [0.20, 0.30, 0.41, 0.50, 0.65])
@pytest.mark.parametrize("markup", [60, 100, 200, 300])
@pytest.mark.parametrize("skew", [-150, 0, 40, 300])
def test_width_mode_is_the_pre_existing_arithmetic(fair: float, markup: int, skew: int) -> None:
    q = quote(fair=fair, markup=markup, skew=skew, mode="width")
    fair_cc = int(round(fair * CC_PER_DOLLAR))
    no_fair = CC_PER_DOLLAR - fair_cc
    margin = markup
    rebate = min(skew, margin // 2)
    peak = fee_cc(CC_PER_DOLLAR // 2)
    lower = max(0, no_fair - margin - abs(rebate) - peak)
    nearest = min(max(CC_PER_DOLLAR // 2, lower), no_fair)
    fee_no = max(fee_cc(no_fair), fee_cc(nearest))
    assert no_bid(q) == snap_down(no_fair - margin - fee_no + rebate)
    # and the function default is width (every pre-existing caller unchanged)
    assert construct_quote.__kwdefaults__["fee_mode"] == "width"


# ------------------------------------------------------------------- floor


@pytest.mark.parametrize("fair", [0.36, 0.41, 0.50, 0.65, 0.80])
@pytest.mark.parametrize("markup", [100, 200, 300])
def test_floor_mode_mains_at_zero_skew_is_byte_identical_to_today(fair: float, markup: int) -> None:
    """Every tier whose markup clears the fee: the fee-blind live price."""
    q = quote(fair=fair, markup=markup, skew=0, mode="floor")
    assert isinstance(q, ConstructedQuote)
    assert fee_cc(CC_PER_DOLLAR // 2) <= markup  # the premise of the pin
    assert no_bid(q) == today_fee_blind(fair, markup, 0)
    assert q.width_components_cc == quote(
        fair=fair, markup=markup, mode="width", fee_type=FeeType.QUADRATIC
    ).width_components_cc  # type: ignore[union-attr]


def test_floor_mode_razor_posts_exactly_fair_minus_fee() -> None:
    """ML-parlay razor 0.6c on a 30c fair (NO fair 70c): the fee over the
    plausible bid range is 75cc > 60cc, so the margin floors at 75 and the
    bid is fair_no − 75 = 6925 → 6920 on the 0.1c grid. Today posts 6940
    (retaining 0.60c against a 0.74c fee — the measured negative-EV razor)."""
    q = quote(fair=0.30, markup=60, mode="floor")
    no_fair = 7_000
    peak = fee_cc(5_000)
    lower = no_fair - max(60, peak)
    floor_fee = max(fee_cc(no_fair), fee_cc(lower))
    assert floor_fee == 75 and fee_cc(no_fair) == 74
    assert no_bid(q) == snap_down(no_fair - floor_fee) == 6_920
    assert today_fee_blind(0.30, 60, 0) == 6_940
    # On an 80c NO fair the range fee is exactly the razor: margin unchanged,
    # bid == today's — the floor never moves a bid it does not need to.
    q80 = quote(fair=0.20, markup=60, mode="floor")
    assert no_bid(q80) == today_fee_blind(0.20, 60, 0) == 7_940


def test_floor_caps_the_rebate_at_margin_minus_fee() -> None:
    fair, markup = 0.50, 100
    fee_no = fee_cc(5_000)  # 88 at the peak
    assert fee_no == 88
    # A 300cc rebate: today caps at margin // 2 = 50; the floor caps at 12.
    assert no_bid(quote(fair=fair, markup=markup, skew=300, mode="floor")) == snap_down(
        5_000 - 100 + (100 - fee_no)
    )
    assert today_fee_blind(fair, markup, 300) == snap_down(5_000 - 100 + 50)
    # A rebate inside the cap passes untouched (byte-identical to today).
    assert no_bid(quote(fair=fair, markup=markup, skew=10, mode="floor")) == today_fee_blind(
        fair, markup, 10
    )
    # Widening (negative skew) is never touched.
    assert no_bid(quote(fair=fair, markup=markup, skew=-150, mode="floor")) == today_fee_blind(
        fair, markup, -150
    )


def test_measured_cell_floor_replaces_the_half_margin_fraction() -> None:
    fair, markup, skew = 0.41, 300, 400
    no_fair = CC_PER_DOLLAR - 4_100
    # Determine the floor fee the same way construct_quote does.
    peak = fee_cc(5_000)
    lower = max(0, no_fair - max(markup, peak) - skew)
    nearest = min(max(5_000, lower), no_fair)
    floor_fee = max(fee_cc(no_fair), fee_cc(nearest))
    # No cell floor: rebate <= min(margin // 2, margin − fee).
    assert no_bid(quote(fair=fair, markup=markup, skew=skew, mode="floor")) == snap_down(
        no_fair - markup + min(markup // 2, markup - floor_fee)
    )
    # Cell floor 40cc: rebate <= margin − fee − 40 (the half rule is gone).
    assert no_bid(
        quote(fair=fair, markup=markup, skew=skew, mode="floor", retained_floor=40)
    ) == snap_down(no_fair - markup + (markup - floor_fee - 40))
    # A floor above the margin: no rebate at all, margin untouched (widen-side
    # is the caps' job, never the floor's).
    assert no_bid(
        quote(fair=fair, markup=markup, skew=skew, mode="floor", retained_floor=5_000)
    ) == snap_down(no_fair - markup)


def test_unknown_fee_type_cannot_quote_in_either_mode() -> None:
    for mode in ("floor", "width"):
        q = quote(fair=0.4, markup=100, mode=mode, fee_type=FeeType.UNKNOWN)
        assert isinstance(q, NoQuote) and q.reason is ReasonCode.SKIP_CLASSIFIER_UNKNOWN


def test_bad_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="fee_mode"):
        quote(fair=0.4, markup=100, mode="eat")


@settings(derandomize=True, max_examples=300)
@given(
    fair=st.floats(0.05, 0.95),
    markup=st.integers(0, 400),
    skew=st.integers(-600, 600),
)
def test_fee_blind_geq_floor_geq_width(fair: float, markup: int, skew: int) -> None:
    blind = quote(fair=fair, markup=markup, skew=skew, mode="width", fee_type=FeeType.QUADRATIC)
    floor = quote(fair=fair, markup=markup, skew=skew, mode="floor")
    width = quote(fair=fair, markup=markup, skew=skew, mode="width")
    b = no_bid(blind) if isinstance(blind, ConstructedQuote) else 0
    f = no_bid(floor) if isinstance(floor, ConstructedQuote) else 0
    w = no_bid(width) if isinstance(width, ConstructedQuote) else 0
    assert b >= f >= w
    # And the floor never retains less than the fee at its own bid.
    if isinstance(floor, ConstructedQuote):
        retained = (CC_PER_DOLLAR - int(floor.fair_cc)) - f
        assert retained >= fee_cc(f)


# ------------------------------------------------ the engine + the cell key


async def test_engine_passes_mode_and_published_floor_through(tmp_path: Path) -> None:
    """The live engine prices in FeeConfig.mode (default floor) on the shared
    measured schedule; a published cell floor only ever tightens a rebate."""
    from tests.test_fee_net_edge import _charged_on_test_collection
    from tests.test_fee_seam_wiring import _rig
    from tests.test_lifecycle import rfq

    rig = await _rig(tmp_path, fills=None, series=None)
    rig.sched.ingest(_charged_on_test_collection())
    engine = rig.engine
    assert engine.fee_mode == "floor"
    test_rfq = rfq()
    key = cell_key(test_rfq.legs)
    base = engine.price(test_rfq, time_to_close_s=36_000.0, inventory_skew_cc=0)
    rebated = engine.price(test_rfq, time_to_close_s=36_000.0, inventory_skew_cc=300)
    assert isinstance(base, ConstructedQuote) and isinstance(rebated, ConstructedQuote)
    assert int(rebated.no_bid_cc) >= int(base.no_bid_cc)
    engine.publish_retained_floor({key: 100_000})
    floored = engine.price(test_rfq, time_to_close_s=36_000.0, inventory_skew_cc=300)
    assert isinstance(floored, ConstructedQuote)
    assert int(floored.no_bid_cc) == int(base.no_bid_cc)  # no rebate survives the floor
    unfloored = engine.price(test_rfq, time_to_close_s=36_000.0, inventory_skew_cc=0)
    assert isinstance(unfloored, ConstructedQuote)
    assert int(unfloored.no_bid_cc) == int(base.no_bid_cc)  # zero skew: untouched
    engine.publish_retained_floor(None)
    back = engine.price(test_rfq, time_to_close_s=36_000.0, inventory_skew_cc=300)
    assert isinstance(back, ConstructedQuote) and int(back.no_bid_cc) == int(rebated.no_bid_cc)


def test_cell_key_is_a_shape_from_existing_classifiers() -> None:
    from combomaker.risk.exposure import LegRef

    same_game_props = [
        LegRef("KXMLBHIT-26AUG201310SFCLE-CLESKWAN38-1", "KXMLBHIT-26AUG201310SFCLE", "yes"),
        LegRef("KXMLBKS-26AUG201310SFCLE-CLEGWILLIAMS32-5", "KXMLBKS-26AUG201310SFCLE", "yes"),
        LegRef("KXMLBKS-26AUG201310SFCLE-SFLROUPP65-3", "KXMLBKS-26AUG201310SFCLE", "yes"),
    ]
    assert cell_key(same_game_props) == ("mlb", "player_hit|player_ks|player_ks", "all_yes", "same")
    nrfi_pair = [
        LegRef("KXMLBRFI-26AUG201310SFCLE", "KXMLBRFI-26AUG201310SFCLE", "no"),
        LegRef("KXMLBRFI-26AUG201940TEXCWS", "KXMLBRFI-26AUG201940TEXCWS", "no"),
    ]
    assert cell_key(nrfi_pair) == ("mlb", "rfi|rfi", "all_no", "cross")
    partial = [
        LegRef("KXMLBGAME-26AUG262105MINATH-MIN", "KXMLBGAME-26AUG262105MINATH", "yes"),
        LegRef("KXMLBHR-26AUG262105MINATH-MINRLEWIS23-1", "KXMLBHR-26AUG262105MINATH", "no"),
        LegRef("KXMLBGAME-26AUG261940TEXCWS-TEX", "KXMLBGAME-26AUG261940TEXCWS", "yes"),
    ]
    assert cell_key(partial) == ("mlb", "moneyline|moneyline|player_hr", "mixed", "partial")
    # Order-independent (a shape, not a list).
    assert cell_key(list(reversed(partial))) == cell_key(partial)
