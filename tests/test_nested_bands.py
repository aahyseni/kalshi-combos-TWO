"""Engine-level nested-band pricing: EXACT P(low) − P(high) arithmetic, never
the copula (2026-07-09 prod mids: the flat-0.6 fallback overpriced live
match-corner bands by +1.8c to +6.6c of fair).

Covers the staged validation set: bare-band exactness to the cent, the
cross-game multi-band product, the farmable impossible direction, and the two
fail-closed mutations (band + same-game companion → UNKNOWN NoQuote; inverted
rung mids → NoQuote) which must decline — at any width — and never raise.
"""

from __future__ import annotations

from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.money import cc_from_prob
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import PricingConfig
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import KalshiBookSource
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.models import Rfq
from tests.test_feed import snapshot_env
from tests.test_filters import Harness
from tests.test_pricing_engine import combo, seed_event

BAND_LOW = "KXWCCORNERS-26JUL10ESPBEL-8"
BAND_HIGH = "KXWCCORNERS-26JUL10ESPBEL-11"
BAND_EV = "KXWCCORNERS-26JUL10ESPBEL"


async def seed_band_books(h: Harness) -> None:
    """low rung ~0.795 (79/80c, equal size => micro=mid), high rung ~0.415."""
    h.feed.watch([BAND_LOW, BAND_HIGH])
    await h.ws.ack_subscription(0, 5)
    env = snapshot_env(5, 1, BAND_LOW)
    env["msg"]["yes_dollars_fp"] = [["0.7900", "50.00"]]
    env["msg"]["no_dollars_fp"] = [["0.2000", "50.00"]]
    await h.ws.deliver(env)
    env = snapshot_env(5, 2, BAND_HIGH)
    env["msg"]["yes_dollars_fp"] = [["0.4100", "50.00"]]
    env["msg"]["no_dollars_fp"] = [["0.5800", "50.00"]]
    await h.ws.deliver(env)


def band_rfq() -> Rfq:
    return combo(
        [
            {"market_ticker": BAND_LOW, "side": "yes", "event_ticker": BAND_EV},
            {"market_ticker": BAND_HIGH, "side": "no", "event_ticker": BAND_EV},
        ]
    )


async def test_nested_band_prices_exact_difference_not_copula() -> None:
    """fair = P(over-8) − P(over-11) EXACTLY (live check 2026-07-09: flat-0.6
    copula overpriced this shape by +2.2c on ESPBEL, +6.6c on a narrow band)."""
    h = Harness()
    await seed_band_books(h)
    h.with_meta("KXMVE-C1")
    seed_event(h, BAND_EV, exclusive=False)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    result = engine.price(band_rfq(), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    src = KalshiBookSource(h.feed)
    p_low, p_high = src.marginal(BAND_LOW), src.marginal(BAND_HIGH)
    assert p_low is not None and p_high is not None
    assert result.fair_cc == cc_from_prob(p_low.p - p_high.p)  # exact arithmetic


async def test_nested_band_inverted_mids_declines() -> None:
    """Identical rung books => P(low) == P(high) => the books contradict the
    ladder ordering: NoQuote, never a clamp-to-0 fair (whose sell-only NO bid
    would quote near $1 on bad data)."""
    h = Harness()
    await h.with_books([BAND_LOW, BAND_HIGH])  # identical books
    h.with_meta("KXMVE-C1")
    seed_event(h, BAND_EV, exclusive=False)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    result = engine.price(band_rfq(), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason is ReasonCode.SKIP_PRICING_FAILED


async def test_two_cross_game_bands_price_as_exact_product() -> None:
    """Buried-band collapse: two bands in two games reduce to two independent
    super-legs (cross_event_rho=0) — fair is the product of the exact bands."""
    low2 = "KXWCCORNERS-26JUL11ARGSUI-7"
    high2 = "KXWCCORNERS-26JUL11ARGSUI-10"
    ev2 = "KXWCCORNERS-26JUL11ARGSUI"
    h = Harness()
    await seed_band_books(h)
    for seq, (t, yes_px, no_px) in enumerate(
        [(low2, "0.6900", "0.3000"), (high2, "0.3900", "0.6000")], start=3
    ):
        h.feed.watch([t])
        env = snapshot_env(5, seq, t)
        env["msg"]["yes_dollars_fp"] = [[yes_px, "50.00"]]
        env["msg"]["no_dollars_fp"] = [[no_px, "50.00"]]
        await h.ws.deliver(env)
    h.with_meta("KXMVE-C1")
    seed_event(h, BAND_EV, exclusive=False)
    seed_event(h, ev2, exclusive=False)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    rfq = combo(
        [
            {"market_ticker": BAND_LOW, "side": "yes", "event_ticker": BAND_EV},
            {"market_ticker": BAND_HIGH, "side": "no", "event_ticker": BAND_EV},
            {"market_ticker": low2, "side": "yes", "event_ticker": ev2},
            {"market_ticker": high2, "side": "no", "event_ticker": ev2},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    src = KalshiBookSource(h.feed)
    m = {t: src.marginal(t) for t in (BAND_LOW, BAND_HIGH, low2, high2)}
    assert all(v is not None for v in m.values())
    lo1, hi1, lo2_, hi2_ = m[BAND_LOW], m[BAND_HIGH], m[low2], m[high2]
    assert lo1 and hi1 and lo2_ and hi2_  # narrow for the type checker
    expected = (lo1.p - hi1.p) * (lo2_.p - hi2_.p)
    assert result.fair_cc == cc_from_prob(expected)


async def test_match_corners_impossible_direction_is_farmed() -> None:
    """over-11 YES + over-8 NO: airtight ladder tautology — farmed exactly like
    the corners_team/same-market families (yes_bid 0, fair 0, farmed flag)."""
    h = Harness()
    await seed_band_books(h)
    h.with_meta("KXMVE-C1")
    seed_event(h, BAND_EV, exclusive=False)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    rfq = combo(
        [
            {"market_ticker": BAND_HIGH, "side": "yes", "event_ticker": BAND_EV},
            {"market_ticker": BAND_LOW, "side": "no", "event_ticker": BAND_EV},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    assert result.farmed is True
    assert result.yes_bid_cc == 0
    assert result.fair_cc == 0


# --- fail-closed mutations (property-sweep companions: NoQuote, never raise) ---


async def test_band_with_same_game_companion_never_quotes() -> None:
    """NESTED_BAND guard: band + a same-game third leg is UNKNOWN -> NoQuote
    (window-event correlation attenuation unmeasured — never a copula guess)."""
    tot, tot_ev = "KXWCTOTAL-26JUL10ESPBEL-3", "KXWCTOTAL-26JUL10ESPBEL"
    h = Harness()
    await h.with_books([BAND_LOW, BAND_HIGH, tot])
    h.with_meta("KXMVE-C1")
    seed_event(h, BAND_EV, exclusive=False)
    seed_event(h, tot_ev, exclusive=False)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    rfq = combo(
        [
            {"market_ticker": BAND_LOW, "side": "yes", "event_ticker": BAND_EV},
            {"market_ticker": BAND_HIGH, "side": "no", "event_ticker": BAND_EV},
            {"market_ticker": tot, "side": "yes", "event_ticker": tot_ev},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)  # must not raise
    assert isinstance(result, NoQuote)
    assert result.reason is ReasonCode.SKIP_CLASSIFIER_UNKNOWN


async def test_band_with_inverted_mids_never_quotes() -> None:
    """Band whose rung books contradict the ladder (identical mids) -> NoQuote,
    never an exception on the hot path."""
    h = Harness()
    await h.with_books([BAND_LOW, BAND_HIGH])  # identical books => P(low)==P(high)
    h.with_meta("KXMVE-C1")
    seed_event(h, BAND_EV, exclusive=False)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    result = engine.price(band_rfq(), time_to_close_s=100_000)  # must not raise
    assert isinstance(result, NoQuote)
    assert result.reason is ReasonCode.SKIP_PRICING_FAILED
