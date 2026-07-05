import pytest

from combomaker.pricing.stub import independence_would_quote
from tests.test_feed import FakeWs, snapshot_env
from tests.test_filters import Harness, combo_rfq


async def priced_harness() -> Harness:
    h = Harness()
    await h.with_books(["M1", "M2"])
    return h


async def test_independence_product_with_no_side_leg() -> None:
    h = await priced_harness()
    rfq = combo_rfq()  # M1 yes, M2 no
    would = independence_would_quote(rfq, h.feed, width_cc=600)
    assert would is not None
    # both fixture books identical; microprice for each leg:
    micro = h.feed.book("M1").top().microprice()
    assert micro is not None
    expected = micro * (1.0 - micro)
    assert would.fair_prob == pytest.approx(expected)
    assert would.leg_probs == pytest.approx((micro, 1.0 - micro))
    assert would.width_cc == 600
    assert would.bid_below_cc == would.fair_cc - 300
    assert would.bid_above_complement_cc == 10_000 - (would.fair_cc + 300)


async def test_unpriceable_when_book_missing() -> None:
    h = Harness()
    await h.with_books(["M1"])  # M2 unwatched
    assert independence_would_quote(combo_rfq(), h.feed, width_cc=600) is None


async def test_unpriceable_when_book_invalid() -> None:
    h = await priced_harness()
    h.feed.book("M2").invalidate("test")
    assert independence_would_quote(combo_rfq(), h.feed, width_cc=600) is None


async def test_unpriceable_when_side_unknown() -> None:
    h = await priced_harness()
    rfq = combo_rfq(
        mve_selected_legs=[
            {"market_ticker": "M1", "side": "??"},
            {"market_ticker": "M2", "side": "no"},
        ]
    )
    assert independence_would_quote(rfq, h.feed, width_cc=600) is None


async def test_bids_clamped_to_price_space() -> None:
    ws = FakeWs()
    h = Harness()
    h.ws = ws
    # build a book with extreme mids so fair ± width clips at the boundaries
    h.feed.watch(["M1", "M2"])
    await h.feed._ws.ack_subscription(0, 5)  # type: ignore[attr-defined]
    await h.feed._ws.deliver(snapshot_env(5, 1, "M1"))  # type: ignore[attr-defined]
    await h.feed._ws.deliver(snapshot_env(5, 2, "M2"))  # type: ignore[attr-defined]
    would = independence_would_quote(combo_rfq(), h.feed, width_cc=30_000)
    assert would is not None
    assert 0 <= would.bid_below_cc <= 10_000
    assert 0 <= would.bid_above_complement_cc <= 10_000
