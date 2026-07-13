"""Tests for combomaker.pricing.quote — quote construction invariants.

Covers: maker-favorable rounding (defense #4), free-money caps, decline
semantics, width components, inventory skew, fail-safe fee handling, and the
capture invariant yes_bid + no_bid <= $1 - min_capture.
"""

from fractions import Fraction
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import DOC_ASSUMED, Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts, qty_from_contracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.orderbook import OrderbookMirror
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import (
    ConstructedQuote,
    NoQuote,
    QuoteParams,
    construct_farm_quote,
    construct_quote,
    free_money_caps,
)

CC = CentiCents
Q = CentiContracts

SCHEDULE = FeeSchedule.from_strings("0.07", "0.0175")

VERIFIED_MAKER = Conventions(
    verified=True,
    source="test fixture",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

TAKER_FEES = FeeModel(SCHEDULE, DOC_ASSUMED)  # fail-safe: taker coefficient
MAKER_FEES = FeeModel(SCHEDULE, VERIFIED_MAKER)  # quadratic series: zero maker fee


def cents_grid() -> PriceGrid:
    return PriceGrid.from_market_payload(
        {"ticker": "T", "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}]}
    )


def deci_grid() -> PriceGrid:
    return PriceGrid.from_market_payload(
        {"ticker": "T", "price_ranges": [{"start": "0.001", "end": "0.999", "step": "0.001"}]}
    )


def cent_grid_between(start_cents: int, end_cents: int) -> PriceGrid:
    return PriceGrid.from_market_payload(
        {
            "ticker": "T",
            "price_ranges": [
                {
                    "start": f"0.{start_cents:02d}",
                    "end": f"0.{end_cents:02d}",
                    "step": "0.01",
                }
            ],
        }
    )


def make_joint(p: float, uncertainty: float = 0.0) -> JointEstimate:
    return JointEstimate(p=p, uncertainty=uncertainty, frechet_lo=0.0, frechet_hi=1.0, notes=())


def fee_cc(model: FeeModel, price_cc: int) -> int:
    return int(
        model.fee_per_contract_cc(
            price_cc=CC(price_cc), fee_type=FeeType.QUADRATIC, multiplier=Fraction(1)
        )
    )


def build_quote(**overrides: Any) -> ConstructedQuote | NoQuote:
    kwargs: dict[str, Any] = {
        "joint": make_joint(0.30, 0.005),
        "n_legs": 2,
        "qty": Q(10_000),  # 100 contracts
        "grid": cents_grid(),
        "fee_model": TAKER_FEES,
        "fee_type": FeeType.QUADRATIC,
        "fee_multiplier": Fraction(1),
        "time_to_close_s": 48 * 3600.0,
        "in_play": False,
        "yes_cap_cc": CC(9_900),
        "no_cap_cc": CC(10_000),
    }
    kwargs.update(overrides)
    return construct_quote(**kwargs)


def make_leg_book(
    yes: list[tuple[int, int]], no: list[tuple[int, int]], ticker: str = "KXLEG"
) -> OrderbookMirror:
    book = OrderbookMirror(ticker, FakeClock())
    book.apply_snapshot(
        yes=[(CC(p), Q(q)) for p, q in yes],
        no=[(CC(p), Q(q)) for p, q in no],
    )
    return book


class TestHappyPath:
    def test_two_sided_quote_below_fair_net_of_costs(self) -> None:
        q = build_quote()
        assert isinstance(q, ConstructedQuote)
        # p=0.30 -> fair $0.30; width = 200 base + 200 legs + 50 unc + 50 size
        assert q.fair_cc == 3_000
        assert q.total_width_cc == 500
        half = q.total_width_cc // 2
        fee_yes = fee_cc(TAKER_FEES, int(q.fair_cc))
        fee_no = fee_cc(TAKER_FEES, CC_PER_DOLLAR - int(q.fair_cc))
        assert q.yes_bid_cc > 0
        assert q.no_bid_cc > 0
        assert q.yes_bid_cc + q.no_bid_cc <= CC_PER_DOLLAR - QuoteParams().min_capture_cc
        assert q.yes_bid_cc <= q.fair_cc - half - fee_yes
        assert q.no_bid_cc <= (CC_PER_DOLLAR - q.fair_cc) - half - fee_no
        # exact values lock the arithmetic: yes raw 3000-250-147=2603 -> 2600;
        # no side pays the conservative in-range fee (peak-ward): fee(6575)=158,
        # raw 7000-250-158=6592 -> 6500
        assert q.yes_bid_cc == 2_600
        assert q.no_bid_cc == 6_500


class TestMakerFavorableRounding:
    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(
        p=st.floats(min_value=0.05, max_value=0.95),
        base=st.integers(min_value=0, max_value=2_000),
        per_leg=st.integers(min_value=0, max_value=500),
        n_legs=st.integers(min_value=1, max_value=5),
        unc=st.floats(min_value=0.0, max_value=0.05),
        contracts=st.integers(min_value=1, max_value=500),
        start_cents=st.integers(min_value=1, max_value=10),
        end_cents=st.integers(min_value=90, max_value=99),
    )
    def test_nonzero_bids_on_grid_and_never_rounded_up(
        self,
        p: float,
        base: int,
        per_leg: int,
        n_legs: int,
        unc: float,
        contracts: int,
        start_cents: int,
        end_cents: int,
    ) -> None:
        grid = cent_grid_between(start_cents, end_cents)
        q = build_quote(
            joint=make_joint(p, unc),
            n_legs=n_legs,
            qty=qty_from_contracts(contracts),
            grid=grid,
            params=QuoteParams(base_width_cc=base, per_leg_width_cc=per_leg),
        )
        if isinstance(q, NoQuote):
            return
        # Reconstruct the raw exactly as the source does (skew = 0 here).
        half = q.total_width_cc // 2
        yes_raw = int(q.fair_cc) - half - fee_cc(TAKER_FEES, int(q.fair_cc))
        no_raw = (
            (CC_PER_DOLLAR - int(q.fair_cc))
            - half
            - fee_cc(TAKER_FEES, CC_PER_DOLLAR - int(q.fair_cc))
        )
        if q.yes_bid_cc != 0:
            assert grid.is_on_grid(q.yes_bid_cc)
            assert q.yes_bid_cc <= yes_raw  # never rounded UP
        if q.no_bid_cc != 0:
            assert grid.is_on_grid(q.no_bid_cc)
            assert q.no_bid_cc <= no_raw


class TestFreeMoneyCapClamping:
    def test_yes_cap_clamps_and_snaps_down(self) -> None:
        q = build_quote(yes_cap_cc=CC(2_000))  # natural yes raw is 2603
        assert isinstance(q, ConstructedQuote)
        assert q.yes_bid_cc <= 2_000 - QuoteParams().free_money_margin_cc
        assert q.yes_bid_cc == 1_900  # cap - margin, already on the cent grid
        assert q.no_bid_cc == 6_500  # other side untouched

    def test_no_cap_clamps_and_snaps_down(self) -> None:
        q = build_quote(no_cap_cc=CC(5_000))  # natural no raw is 6603
        assert isinstance(q, ConstructedQuote)
        assert q.no_bid_cc <= 5_000 - QuoteParams().free_money_margin_cc
        assert q.no_bid_cc == 4_900
        assert q.yes_bid_cc == 2_600

    def test_missing_caps_mean_no_quote(self) -> None:
        for overrides in (
            {"yes_cap_cc": None, "no_cap_cc": None},
            {"yes_cap_cc": None},
            {"no_cap_cc": None},
        ):
            q = build_quote(**overrides)
            assert isinstance(q, NoQuote)
            assert q.reason is ReasonCode.SKIP_NO_FREE_MONEY_CHECK


class TestFreeMoneyCapsFromBooks:
    """free_money_caps() against real OrderbookMirror walks (1-contract probe)."""

    def books(self) -> tuple[OrderbookMirror, OrderbookMirror]:
        # book1 executable at 1.00: buy yes -> $0.44, buy no -> $0.58
        book1 = make_leg_book(
            yes=[(100, 20_000), (4_200, 1_300)],
            no=[(100, 10_000), (5_600, 1_700)],
            ticker="KXLEG-1",
        )
        # book2 executable at 1.00: buy yes -> $0.35, buy no -> $0.70
        book2 = make_leg_book(yes=[(3_000, 500)], no=[(6_500, 800)], ticker="KXLEG-2")
        return book1, book2

    def test_yes_yes_caps(self) -> None:
        book1, book2 = self.books()
        yes_cap, no_cap = free_money_caps([book1, book2], ["yes", "yes"])
        assert yes_cap == min(4_400, 3_500)
        # complements 5_800 + 7_000 = 12_800, capped at $1
        assert no_cap == CC_PER_DOLLAR

    def test_yes_no_caps(self) -> None:
        book1, book2 = self.books()
        yes_cap, no_cap = free_money_caps([book1, book2], ["yes", "no"])
        assert yes_cap == min(4_400, 7_000)
        assert no_cap == 5_800 + 3_500  # below $1: not clamped

    def test_invalid_book_means_no_caps(self) -> None:
        book1, book2 = self.books()
        book2.invalidate("test")
        assert free_money_caps([book1, book2], ["yes", "yes"]) == (None, None)

    def test_underfilled_walk_means_no_caps(self) -> None:
        # only 0.50 contracts behind the derived yes ask: 1.00 probe underfills
        thin = make_leg_book(yes=[(4_200, 1_300)], no=[(5_600, 50)])
        assert free_money_caps([thin], ["yes"]) == (None, None)


class TestDeclineSemantics:
    def test_extreme_fair_declines_yes_side_only(self) -> None:
        # fair $0.02, half-width 250cc > fair: yes rounds away, no still quotes
        q = build_quote(joint=make_joint(0.02), n_legs=3, qty=Q(100))
        assert isinstance(q, ConstructedQuote)
        assert q.yes_bid_cc == 0
        assert q.no_bid_cc > 0
        assert q.no_bid_cc == 9_500  # 9800 - 250 - 14 = 9536, snapped down

    def test_both_sides_rounded_away_is_no_quote(self) -> None:
        q = build_quote(joint=make_joint(0.5), params=QuoteParams(base_width_cc=20_000))
        assert isinstance(q, NoQuote)
        assert q.reason is ReasonCode.SKIP_PRICING_FAILED
        assert "rounded away" in q.detail


class TestWidthComponents:
    def test_in_play_adds_extra(self) -> None:
        q = build_quote(in_play=True)
        assert isinstance(q, ConstructedQuote)
        assert q.width_components_cc["in_play"] == QuoteParams().in_play_extra_cc
        calm = build_quote(in_play=False)
        assert isinstance(calm, ConstructedQuote)
        assert "in_play" not in calm.width_components_cc

    def test_time_component_only_below_threshold(self) -> None:
        far = build_quote(time_to_close_s=24 * 3600.0)  # above 6h threshold
        assert isinstance(far, ConstructedQuote)
        assert "time" not in far.width_components_cc
        near = build_quote(time_to_close_s=3 * 3600.0)  # halfway to threshold
        assert isinstance(near, ConstructedQuote)
        assert near.width_components_cc["time"] == 100  # 200 * 0.5
        assert near.width_components_cc["time"] > 0

    def test_size_component_scales_per_100_contracts(self) -> None:
        q = build_quote(qty=qty_from_contracts(500))
        assert isinstance(q, ConstructedQuote)
        assert q.width_components_cc["size"] == 50 * 50_000 // 10_000  # 250
        tiny = build_quote(qty=Q(100))  # 1 contract
        assert isinstance(tiny, ConstructedQuote)
        assert tiny.width_components_cc["size"] == 0


class TestInventorySkew:
    def test_positive_skew_lowers_yes_and_raises_no(self) -> None:
        flat = build_quote(joint=make_joint(0.5), grid=deci_grid(), inventory_skew_cc=0)
        skewed = build_quote(joint=make_joint(0.5), grid=deci_grid(), inventory_skew_cc=500)
        assert isinstance(flat, ConstructedQuote)
        assert isinstance(skewed, ConstructedQuote)
        assert skewed.yes_bid_cc < flat.yes_bid_cc
        assert skewed.no_bid_cc > flat.no_bid_cc


class TestFees:
    def test_unknown_fee_type_is_no_quote(self) -> None:
        q = build_quote(fee_type=FeeType.UNKNOWN)
        assert isinstance(q, NoQuote)
        assert q.reason is ReasonCode.SKIP_CLASSIFIER_UNKNOWN
        assert "fee model" in q.detail

    def test_verified_maker_quadratic_fee_is_zero_so_bids_are_higher(self) -> None:
        taker_q = build_quote()
        maker_q = build_quote(fee_model=MAKER_FEES)
        assert isinstance(taker_q, ConstructedQuote)
        assert isinstance(maker_q, ConstructedQuote)
        assert fee_cc(MAKER_FEES, 3_000) == 0
        assert maker_q.yes_bid_cc > taker_q.yes_bid_cc


class TestCaptureInvariant:
    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(
        p=st.floats(min_value=0.02, max_value=0.98),
        base=st.integers(min_value=0, max_value=3_000),
        per_leg=st.integers(min_value=0, max_value=500),
        n_legs=st.integers(min_value=1, max_value=6),
        unc=st.floats(min_value=0.0, max_value=0.05),
        contracts=st.integers(min_value=1, max_value=1_000),
        skew=st.integers(min_value=-500, max_value=500),
        in_play=st.booleans(),
        time_to_close_s=st.floats(min_value=0.0, max_value=1e6),
    )
    def test_every_constructed_quote_respects_capture_and_grid(
        self,
        p: float,
        base: int,
        per_leg: int,
        n_legs: int,
        unc: float,
        contracts: int,
        skew: int,
        in_play: bool,
        time_to_close_s: float,
    ) -> None:
        grid = cents_grid()
        q = build_quote(
            joint=make_joint(p, unc),
            n_legs=n_legs,
            qty=qty_from_contracts(contracts),
            grid=grid,
            time_to_close_s=time_to_close_s,
            in_play=in_play,
            inventory_skew_cc=skew,
            params=QuoteParams(base_width_cc=base, per_leg_width_cc=per_leg),
        )
        if isinstance(q, NoQuote):
            return
        min_capture = QuoteParams().min_capture_cc
        assert q.yes_bid_cc + q.no_bid_cc <= CC_PER_DOLLAR - min_capture
        for bid in (q.yes_bid_cc, q.no_bid_cc):
            assert 0 <= bid <= CC_PER_DOLLAR
            assert bid == 0 or grid.is_on_grid(bid)


class TestFarmQuote:
    """construct_farm_quote invariants — the ONLY structure that makes farming
    a logically-impossible combo safe. The single most important property: we
    can NEVER end up long the worthless YES side (yes_bid is always 0)."""

    def farm(self, **overrides: Any) -> ConstructedQuote | NoQuote:
        kwargs: dict[str, Any] = {
            "farm_ask_cc": CC(950),          # naive YES value ≈ 0.095
            "n_legs": 2,
            "qty": Q(10_000),                # 100 contracts
            "grid": deci_grid(),             # 0.001 step so 0.905 is on-grid
            "no_cap_cc": CC(CC_PER_DOLLAR),  # no binding complement bound
            "size_cap": Q(5_000),            # 50 contracts
        }
        kwargs.update(overrides)
        return construct_farm_quote(**kwargs)

    def test_worked_example_screenshot_combo(self) -> None:
        # {1H-BTTS yes ~0.19, FT-BTTS no ~0.50}: naive value 0.19*0.50 = 0.095,
        # so we offer YES at 0.095 <=> bid NO at 0.905, and never touch YES.
        q = self.farm()
        assert isinstance(q, ConstructedQuote)
        assert q.yes_bid_cc == 0             # never long the worthless YES
        assert q.no_bid_cc == 9_050          # $1 - 0.095, on the 0.001 grid
        assert q.fair_cc == 0                # true fair of an impossible combo
        assert q.farmed is True
        assert q.width_components_cc == {"farm_sell_price": 950}

    @settings(derandomize=True, max_examples=300, deadline=None)
    @given(
        farm_ask_cc=st.integers(min_value=-500, max_value=CC_PER_DOLLAR),
        no_cap=st.integers(min_value=0, max_value=CC_PER_DOLLAR),
        size_cap=st.integers(min_value=-100, max_value=10_000),
        n_legs=st.integers(min_value=2, max_value=6),
        start_cents=st.integers(min_value=1, max_value=10),
        end_cents=st.integers(min_value=90, max_value=99),
    )
    def test_yes_bid_is_always_zero_and_maker_favorable(
        self,
        farm_ask_cc: int,
        no_cap: int,
        size_cap: int,
        n_legs: int,
        start_cents: int,
        end_cents: int,
    ) -> None:
        grid = cent_grid_between(start_cents, end_cents)
        q = construct_farm_quote(
            farm_ask_cc=CC(farm_ask_cc),
            n_legs=n_legs,
            qty=Q(10_000),
            grid=grid,
            no_cap_cc=CC(no_cap),
            size_cap=Q(size_cap),
        )
        if isinstance(q, NoQuote):
            return
        # HARD INVARIANT: never long the YES side of a farmed combo, ever.
        assert q.yes_bid_cc == 0
        assert q.fair_cc == 0
        assert q.farmed is True
        # Maker-favorable: the NO bid is on the grid and never above the raw
        # (rounded DOWN), and never above the free-money cap minus margin.
        assert grid.is_on_grid(q.no_bid_cc)
        no_raw = CC_PER_DOLLAR - farm_ask_cc
        assert int(q.no_bid_cc) <= no_raw
        assert int(q.no_bid_cc) <= no_cap - QuoteParams().free_money_margin_cc
        # The implied sell price of the (worthless) YES is strictly positive —
        # never a degenerate "sell for nothing" quote.
        assert CC_PER_DOLLAR - int(q.no_bid_cc) > 0

    def test_missing_no_cap_is_no_quote(self) -> None:
        q = self.farm(no_cap_cc=None)
        assert isinstance(q, NoQuote)
        assert q.reason is ReasonCode.SKIP_NO_FREE_MONEY_CHECK

    def test_zero_farm_ask_is_no_quote(self) -> None:
        for ask in (0, -10):
            q = self.farm(farm_ask_cc=CC(ask))
            assert isinstance(q, NoQuote)
            assert q.reason is ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE

    def test_zero_size_cap_is_no_quote(self) -> None:
        for cap in (0, -1):
            q = self.farm(size_cap=Q(cap))
            assert isinstance(q, NoQuote)
            assert q.reason is ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE

    def test_no_cap_clamps_the_no_bid(self) -> None:
        # Complement basket only worth $0.60: we must not bid NO above
        # 0.60 - margin, even though 1 - farm_ask is 0.905.
        q = self.farm(no_cap_cc=CC(6_000))
        assert isinstance(q, ConstructedQuote)
        assert q.yes_bid_cc == 0
        assert int(q.no_bid_cc) <= 6_000 - QuoteParams().free_money_margin_cc
        assert q.no_bid_cc == 5_900

    def test_no_bid_rounds_away_is_no_quote(self) -> None:
        # A cap at/below the margin leaves no room to bid the NO side.
        q = self.farm(no_cap_cc=CC(QuoteParams().free_money_margin_cc))
        assert isinstance(q, NoQuote)
        assert q.reason is ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE


class TestSellParlaysOnly:
    """Fade defense: with QuoteParams.sell_parlays_only=True the quote is a pure
    parlay SELLER — yes_bid is ALWAYS 0 (we can never be handed long-YES, the
    -14c/ct adverse side), while the no_bid (sell side) is priced exactly as in
    the two-sided quote."""

    SELL = QuoteParams(sell_parlays_only=True)

    def test_forces_yes_bid_zero_keeps_no_side(self) -> None:
        q = build_quote(params=self.SELL)
        assert isinstance(q, ConstructedQuote)
        assert q.yes_bid_cc == 0            # YES side declined
        assert q.no_bid_cc > 0             # still selling the parlay
        # The implied YES ask ($1 - no_bid) carries the markup: above fair.
        assert CC_PER_DOLLAR - int(q.no_bid_cc) > int(q.fair_cc)

    def test_no_side_identical_to_two_sided(self) -> None:
        """Turning on sell-only must NOT perturb the sell-side price — it only
        drops the YES bid. Same fair, same no_bid; only yes_bid differs."""
        two = build_quote()                                    # default params
        one = build_quote(params=self.SELL)
        assert isinstance(two, ConstructedQuote) and isinstance(one, ConstructedQuote)
        assert two.yes_bid_cc > 0 and one.yes_bid_cc == 0      # the only difference
        assert one.no_bid_cc == two.no_bid_cc
        assert one.fair_cc == two.fair_cc

    def test_negative_skew_cannot_lift_yes_off_zero(self) -> None:
        """A large negative inventory skew RAISES yes_raw and would produce a big
        yes_bid two-sided — sell-only must still pin it to 0 (mutation guard)."""
        two = build_quote(inventory_skew_cc=-3_000)
        one = build_quote(inventory_skew_cc=-3_000, params=self.SELL)
        assert isinstance(two, ConstructedQuote) and isinstance(one, ConstructedQuote)
        assert two.yes_bid_cc > 0                              # skew DID lift it two-sided
        assert one.yes_bid_cc == 0                             # ...but not in sell-only
        assert one.no_bid_cc > 0

    def test_declines_when_no_side_rounds_away(self) -> None:
        """Fair ~ $0.99: the no side rounds to 0; with yes also declined the
        result is a clean NoQuote carrying the sell-only reason."""
        q = build_quote(joint=make_joint(0.99, 0.0), params=self.SELL)
        assert isinstance(q, NoQuote)
        assert q.reason is ReasonCode.SKIP_PRICING_FAILED
        assert "sell-only" in q.detail

    @settings(derandomize=True, max_examples=400, deadline=None)
    @given(
        p=st.integers(min_value=2, max_value=98),        # fair in cents
        # Draw skew across a SUPERSET of compute_inventory_skew's whole live
        # range [−skew_max_tighten_cc, +skew_max_widen_cc] (Phase 5, R3 §A6.2):
        # ±6000 comfortably contains the widest configured caps (±600/150), so
        # the sell-only invariant is proven to survive ANY skew the risk engine
        # can emit — including a large negative (tightening) one.
        skew=st.integers(min_value=-6_000, max_value=6_000),
        qty=st.integers(min_value=100, max_value=50_000),
        in_play=st.booleans(),
        n_legs=st.integers(min_value=2, max_value=8),
    )
    def test_yes_bid_is_always_zero(
        self, p: int, skew: int, qty: int, in_play: bool, n_legs: int
    ) -> None:
        """HARD INVARIANT: across fair, inventory skew (either sign), size,
        in-play, and leg count, sell-only NEVER emits a non-zero yes_bid."""
        q = build_quote(
            joint=make_joint(p / 100.0, 0.01),
            n_legs=n_legs,
            qty=Q(qty),
            in_play=in_play,
            inventory_skew_cc=skew,
            params=self.SELL,
        )
        if isinstance(q, NoQuote):
            return
        assert q.yes_bid_cc == 0
        # And the capture invariant still holds (now just: keep >= min_capture).
        assert int(q.yes_bid_cc) + int(q.no_bid_cc) <= CC_PER_DOLLAR - self.SELL.min_capture_cc


class TestSkewNoArb:
    """R3 §A6.3 — NO-ARB survives ANY skew, stressing the FREE-MONEY-DANGEROUS
    direction.

    The pricer seam is ``no_raw = ($1 − fair) − half − fee_no + inventory_skew_cc``,
    so a POSITIVE ``inventory_skew_cc`` RAISES ``no_bid`` toward the free-money
    cap — this is the direction that can produce an arb if unchecked, and (after
    the classifier→pricer sign fix in risk/skew.py) it is the sign the OFFSETTING
    rebate now enters the pricer with. The free-money clamp in construct_quote
    (quote.py:202) fires AFTER skew and the capture invariant re-checks, so the
    rebate can shrink our edge but can NEVER produce an arb quote. A NEGATIVE
    ``inventory_skew_cc`` LOWERS ``no_bid`` (moves AWAY from the cap) — safe by
    construction; kept as coverage of that side, but the clamp is stressed by the
    positive cases below (regression for the judge's finding #2: the pre-fix test
    fuzzed only the negative side and never exercised the clamp)."""

    @settings(derandomize=True, max_examples=300, deadline=None)
    @given(
        p=st.integers(min_value=5, max_value=95),
        no_cap=st.integers(min_value=2_000, max_value=9_900),
        rebate=st.integers(min_value=0, max_value=6_000),
    )
    def test_positive_skew_never_breaches_free_money_cap(
        self, p: int, no_cap: int, rebate: int
    ) -> None:
        # POSITIVE skew RAISES no_raw toward the cap — the arb-dangerous side.
        q = build_quote(
            joint=make_joint(p / 100.0, 0.0),
            grid=deci_grid(),
            inventory_skew_cc=rebate,
            no_cap_cc=CC(no_cap),
            yes_cap_cc=CC(9_900),
        )
        if isinstance(q, NoQuote):
            return
        # The emitted NO bid never crosses the free-money cap minus its margin,
        # no matter how large the rebate skew (the clamp is the backstop).
        assert int(q.no_bid_cc) <= no_cap - 100  # free_money_margin_cc default
        assert int(q.yes_bid_cc) + int(q.no_bid_cc) <= CC_PER_DOLLAR - 100

    @settings(derandomize=True, max_examples=300, deadline=None)
    @given(
        p=st.integers(min_value=5, max_value=95),
        no_cap=st.integers(min_value=2_000, max_value=9_900),
        tighten=st.integers(min_value=0, max_value=6_000),
    )
    def test_negative_skew_never_breaches_free_money_cap(
        self, p: int, no_cap: int, tighten: int
    ) -> None:
        # NEGATIVE skew LOWERS no_raw (away from the cap) — coverage of the safe
        # side; the invariant must still hold.
        q = build_quote(
            joint=make_joint(p / 100.0, 0.0),
            grid=deci_grid(),
            inventory_skew_cc=-tighten,
            no_cap_cc=CC(no_cap),
            yes_cap_cc=CC(9_900),
        )
        if isinstance(q, NoQuote):
            return
        assert int(q.no_bid_cc) <= no_cap - 100  # free_money_margin_cc default
        assert int(q.yes_bid_cc) + int(q.no_bid_cc) <= CC_PER_DOLLAR - 100

    def test_deep_positive_skew_fires_the_clamp(self) -> None:
        # A large POSITIVE skew WOULD push no_bid to ~$1; it is clamped to the
        # cap. Uses a fair (0.30) whose UNCLAMPED no_raw would exceed the cap, so
        # the clamp provably BINDS: base no_bid ≈ $0.70 − half; +9_000 skew would
        # drive it toward ~$1 but the cap at 7_000 − 100 holds.
        clamped = build_quote(
            joint=make_joint(0.30, 0.0),
            grid=deci_grid(),
            inventory_skew_cc=9_000,           # absurd rebate toward the cap
            no_cap_cc=CC(7_000),
            yes_cap_cc=CC(9_900),
        )
        assert isinstance(clamped, ConstructedQuote)
        assert int(clamped.no_bid_cc) == 7_000 - 100  # clamped to exactly the cap
        # Sanity: WITHOUT the skew the no_bid is strictly below the cap, proving
        # the +9_000 is what drove it into the clamp (not the base price alone).
        base = build_quote(
            joint=make_joint(0.30, 0.0),
            grid=deci_grid(),
            inventory_skew_cc=0,
            no_cap_cc=CC(7_000),
            yes_cap_cc=CC(9_900),
        )
        assert isinstance(base, ConstructedQuote)
        assert int(base.no_bid_cc) < 7_000 - 100

    def test_deep_tightening_skew_stays_below_cap(self) -> None:
        # The opposite side: a deep NEGATIVE skew lowers no_bid, so it sits well
        # below the cap (never breaches). Retained coverage of the safe side.
        clamped = build_quote(
            joint=make_joint(0.30, 0.0),
            grid=deci_grid(),
            inventory_skew_cc=-9_000,          # absurd tighten (away from cap)
            no_cap_cc=CC(7_000),
            yes_cap_cc=CC(9_900),
        )
        assert isinstance(clamped, ConstructedQuote)
        assert int(clamped.no_bid_cc) <= 7_000 - 100


class TestMarkup:
    """Maker markup: margin = max(defensive width, markup_cc). markup==0 is
    BIT-IDENTICAL to pre-markup (parity); a markup below the width floor is a
    no-op; above it, it lowers the seller's no_bid (widens the sale, more edge);
    a markup that exceeds fair declines the side."""

    def test_zero_markup_is_identical(self) -> None:
        # The parity guarantee: an un-set markup does not move any bid.
        assert build_quote(markup_cc=0) == build_quote()

    def test_markup_below_width_floor_is_noop(self) -> None:
        # build_quote's defensive half-width is 250cc; 100cc sits under the floor,
        # so margin stays 250 and the quote is unchanged.
        base = build_quote()
        floored = build_quote(markup_cc=100)
        assert isinstance(base, ConstructedQuote) and isinstance(floored, ConstructedQuote)
        assert int(floored.no_bid_cc) == int(base.no_bid_cc)

    def test_markup_above_floor_lowers_no_bid(self) -> None:
        base = build_quote()
        wide = build_quote(markup_cc=400)  # 4¢ > 2.5¢ floor => margin becomes 400
        assert isinstance(base, ConstructedQuote) and isinstance(wide, ConstructedQuote)
        assert int(wide.no_bid_cc) < int(base.no_bid_cc)

    def test_markup_is_monotonic(self) -> None:
        bids = [int(q.no_bid_cc) for q in (
            build_quote(markup_cc=m) for m in (0, 300, 500, 700)
        ) if isinstance(q, ConstructedQuote)]
        assert len(bids) == 4
        assert bids == sorted(bids, reverse=True)  # more markup => lower no_bid

    def test_markup_exceeding_fair_declines(self) -> None:
        # A markup wider than the fair on both sides rounds every raw <= 0.
        assert isinstance(build_quote(markup_cc=9_000), NoQuote)
