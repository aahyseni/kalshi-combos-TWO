"""DO-6 basket width adder — detection + width invariants (2026-07-10).

Symptom on file (docs/reports/ 2026-07-10 MLB reports): +25-35c/$1 overbid on
8-16-leg all-NO single-prop-family baskets. Fix: a quote-WIDTH adder
(``QuoteParams.basket_width_extra_cc``, int centi-cents, default 250, 0
disables) applied when the engine detects the shape — >= 8 legs AND every leg
NO-side AND all legs one single MLB prop family (player_hr / player_hit /
player_tb / player_hrr / player_ks). The adder lands AFTER all normal width
components (including the archetype multiplier) and BEFORE the
maker-favorable snap; it may only WIDEN, never tighten.

Covers:
- detection: the 7-vs-8 leg boundary, mixed-family exempt, any-YES-leg
  exempt, non-prop/UNKNOWN families exempt, all 5 prop families qualify
- width: exact 'basket' component, bids only ever move DOWN (widen),
  tunable=0 (and negative) disables, survives the multiplier collapse
- maker-favorable rounding invariant preserved with the adder on (property)
- engine plumbing end to end: an 8-leg all-NO KXMLBHR basket through
  PricingEngine.price widens by exactly the tunable vs a 0-tunable engine
"""

from fractions import Fraction
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts, qty_from_contracts
from combomaker.marketdata.grid import PriceGrid
from combomaker.ops.config import PricingConfig, QuoteConfig
from combomaker.pricing.engine import PricingEngine, is_single_family_no_basket
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import ConstructedQuote, NoQuote, QuoteParams, construct_quote
from combomaker.rfq.models import Rfq, RfqLeg
from tests.test_filters import Harness
from tests.test_pricing_engine import combo, seed_event

CC = CentiCents
Q = CentiContracts

_G = "26JUL10COLSF"
# One series prefix per DO-6 prop family (legtypes.classify_leg types these
# from the ticker alone).
_FAMILY_SERIES = ("KXMLBHR", "KXMLBHIT", "KXMLBTB", "KXMLBHRR", "KXMLBKS")


def _leg(market_ticker: str, side: str = "no") -> RfqLeg:
    return RfqLeg(market_ticker, f"KXMLB-{_G}", side, None)


def _family_legs(series: str, n: int, side: str = "no") -> list[RfqLeg]:
    return [_leg(f"{series}-{_G}-COLB{i}-1", side) for i in range(n)]


def _sides(legs: list[RfqLeg]) -> list[str]:
    return [leg.side for leg in legs]


# --- 1. Detection ----------------------------------------------------------------


class TestDetection:
    def test_eight_all_no_single_family_qualifies_for_every_prop_family(self) -> None:
        for series in _FAMILY_SERIES:
            legs = _family_legs(series, 8)
            assert is_single_family_no_basket(legs, _sides(legs)), series

    def test_seven_vs_eight_leg_boundary(self) -> None:
        seven = _family_legs("KXMLBHR", 7)
        assert not is_single_family_no_basket(seven, _sides(seven))
        eight = _family_legs("KXMLBHR", 8)
        assert is_single_family_no_basket(eight, _sides(eight))

    def test_sixteen_legs_still_qualifies(self) -> None:
        legs = _family_legs("KXMLBKS", 16)
        assert is_single_family_no_basket(legs, _sides(legs))

    def test_mixed_family_exempt(self) -> None:
        # 7 HR + 1 HIT, all NO: two families -> not a single-family basket.
        legs = _family_legs("KXMLBHR", 7) + _family_legs("KXMLBHIT", 1)
        assert len(legs) == 8
        assert not is_single_family_no_basket(legs, _sides(legs))

    def test_any_yes_leg_exempt(self) -> None:
        for yes_at in (0, 4, 7):
            legs = _family_legs("KXMLBHR", 8)
            legs[yes_at] = _leg(legs[yes_at].market_ticker, side="yes")
            assert not is_single_family_no_basket(legs, _sides(legs)), yes_at
        all_yes = _family_legs("KXMLBHR", 8, side="yes")
        assert not is_single_family_no_basket(all_yes, _sides(all_yes))

    def test_non_prop_and_unknown_families_exempt(self) -> None:
        # 8 all-NO moneyline legs: single family but NOT a prop family.
        ml = [_leg(f"KXMLBGAME-{_G}-T{i}") for i in range(8)]
        assert not is_single_family_no_basket(ml, _sides(ml))
        # 8 all-NO UNKNOWN-typed legs: never fires on unclassifiable structure.
        unk = [_leg(f"KXSOMESERIES-{_G}-X{i}") for i in range(8)]
        assert not is_single_family_no_basket(unk, _sides(unk))


# --- 2. Width adder in construct_quote --------------------------------------------

SCHEDULE = FeeSchedule.from_strings("0.07", "0.0175")
TAKER_FEES = FeeModel(SCHEDULE, DOC_ASSUMED)


def cents_grid() -> PriceGrid:
    return PriceGrid.from_market_payload(
        {"ticker": "T", "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}]}
    )


def make_joint(p: float, uncertainty: float = 0.0) -> JointEstimate:
    return JointEstimate(p=p, uncertainty=uncertainty, frechet_lo=0.0, frechet_hi=1.0, notes=())


def build_quote(**overrides: Any) -> ConstructedQuote | NoQuote:
    kwargs: dict[str, Any] = {
        "joint": make_joint(0.30, 0.005),
        "n_legs": 8,
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


class TestBasketWidthAdder:
    def test_adds_exact_component_and_only_widens(self) -> None:
        base = build_quote()
        basket = build_quote(basket_extra_applies=True)
        assert isinstance(base, ConstructedQuote) and isinstance(basket, ConstructedQuote)
        assert "basket" not in base.width_components_cc
        assert basket.width_components_cc["basket"] == QuoteParams().basket_width_extra_cc
        assert basket.total_width_cc == base.total_width_cc + 250
        # Widen means both bids move DOWN (never up), fair untouched.
        assert basket.fair_cc == base.fair_cc
        assert basket.yes_bid_cc <= base.yes_bid_cc
        assert basket.no_bid_cc <= base.no_bid_cc
        # Exact arithmetic lock: half goes 550 -> 675, both bids drop a grid step.
        assert (base.yes_bid_cc, base.no_bid_cc) == (2_300, 6_200)
        assert (basket.yes_bid_cc, basket.no_bid_cc) == (2_100, 6_100)

    def test_tunable_zero_disables(self) -> None:
        off = build_quote(params=QuoteParams(basket_width_extra_cc=0))
        zeroed = build_quote(
            basket_extra_applies=True, params=QuoteParams(basket_width_extra_cc=0)
        )
        assert isinstance(off, ConstructedQuote) and isinstance(zeroed, ConstructedQuote)
        assert "basket" not in zeroed.width_components_cc
        assert zeroed == off  # flag with a 0 tunable is a byte-for-byte no-op

    def test_negative_tunable_never_tightens(self) -> None:
        # A misconfigured negative adder must NOT narrow the quote — the > 0
        # guard drops it entirely (widen-only invariant).
        base = build_quote()
        neg = build_quote(
            basket_extra_applies=True, params=QuoteParams(basket_width_extra_cc=-500)
        )
        assert isinstance(base, ConstructedQuote) and isinstance(neg, ConstructedQuote)
        assert "basket" not in neg.width_components_cc
        assert neg == base

    def test_adder_survives_multiplier_collapse_unscaled(self) -> None:
        # The archetype multiplier collapses width to {'scaled'}; the basket
        # adder lands AFTER it (never scaled away by a favorites tightening).
        tight = build_quote(width_multiplier=0.5)
        tight_basket = build_quote(width_multiplier=0.5, basket_extra_applies=True)
        assert isinstance(tight, ConstructedQuote) and isinstance(tight_basket, ConstructedQuote)
        assert set(tight_basket.width_components_cc) == {"scaled", "basket"}
        assert tight_basket.width_components_cc["scaled"] == tight.width_components_cc["scaled"]
        assert tight_basket.total_width_cc == tight.total_width_cc + 250

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(
        p=st.floats(min_value=0.05, max_value=0.95),
        extra=st.integers(min_value=0, max_value=2_000),
        n_legs=st.integers(min_value=8, max_value=16),
        unc=st.floats(min_value=0.0, max_value=0.05),
        contracts=st.integers(min_value=1, max_value=500),
    )
    def test_maker_favorable_rounding_invariant_preserved(
        self, p: float, extra: int, n_legs: int, unc: float, contracts: int
    ) -> None:
        """Defense #4 with the adder on: every non-zero bid is on-grid and
        NEVER rounded up past its raw (which now includes the basket width)."""
        grid = cents_grid()
        q = build_quote(
            joint=make_joint(p, unc),
            n_legs=n_legs,
            qty=qty_from_contracts(contracts),
            grid=grid,
            basket_extra_applies=True,
            params=QuoteParams(basket_width_extra_cc=extra),
        )
        if isinstance(q, NoQuote):
            return
        if extra > 0:
            assert q.width_components_cc["basket"] == extra
        half = q.total_width_cc // 2

        def fee(price_cc: int) -> int:
            return int(
                TAKER_FEES.fee_per_contract_cc(
                    price_cc=CC(price_cc), fee_type=FeeType.QUADRATIC, multiplier=Fraction(1)
                )
            )

        yes_raw = int(q.fair_cc) - half - fee(int(q.fair_cc))
        no_raw = (CC_PER_DOLLAR - int(q.fair_cc)) - half - fee(CC_PER_DOLLAR - int(q.fair_cc))
        if q.yes_bid_cc != 0:
            assert grid.is_on_grid(q.yes_bid_cc)
            assert q.yes_bid_cc <= yes_raw  # never rounded UP
        if q.no_bid_cc != 0:
            assert grid.is_on_grid(q.no_bid_cc)
            assert q.no_bid_cc <= no_raw
        assert q.yes_bid_cc + q.no_bid_cc <= CC_PER_DOLLAR - QuoteParams().min_capture_cc


# --- 3. Engine plumbing end to end -------------------------------------------------

BASKET_EVENT = f"KXMLB-{_G}"
BASKET_TICKERS = [f"KXMLBHR-{_G}-COLB{i}-1" for i in range(8)]


async def basket_engine(config: PricingConfig | None = None) -> PricingEngine:
    h = Harness()
    await h.with_books(BASKET_TICKERS)
    h.with_meta("KXMVE-C1")  # combo market metadata incl. 1-cent grid
    seed_event(h, BASKET_EVENT, exclusive=False)
    return PricingEngine(h.feed, h.metadata, DOC_ASSUMED, config or PricingConfig())


def basket_combo(sides: list[str] | None = None) -> Rfq:
    chosen = sides or ["no"] * len(BASKET_TICKERS)
    return combo(
        [
            {"market_ticker": t, "side": s, "event_ticker": BASKET_EVENT}
            for t, s in zip(BASKET_TICKERS, chosen, strict=True)
        ]
    )


async def test_engine_applies_basket_width_to_all_no_prop_basket() -> None:
    rfq = basket_combo()
    on = (await basket_engine()).price(rfq, time_to_close_s=100_000.0)
    off = (await basket_engine(PricingConfig(quote=QuoteConfig(basket_width_extra_cc=0)))).price(
        rfq, time_to_close_s=100_000.0
    )
    assert isinstance(on, ConstructedQuote), on
    assert isinstance(off, ConstructedQuote), off
    assert on.width_components_cc["basket"] == QuoteConfig().basket_width_extra_cc == 250
    assert "basket" not in off.width_components_cc
    assert on.total_width_cc == off.total_width_cc + 250
    assert on.fair_cc == off.fair_cc


async def test_engine_exempts_yes_leg_basket() -> None:
    rfq = basket_combo(["yes"] + ["no"] * 7)
    quote = (await basket_engine()).price(rfq, time_to_close_s=100_000.0)
    assert isinstance(quote, ConstructedQuote), quote
    assert "basket" not in quote.width_components_cc
