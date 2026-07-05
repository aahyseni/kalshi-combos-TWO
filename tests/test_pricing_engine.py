from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.metadata import EventMeta
from combomaker.ops.config import PricingConfig
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.models import Rfq
from tests.test_filters import Harness


def combo(legs: list[dict[str, str]], **overrides: object) -> Rfq:
    msg: dict[str, object] = {
        "id": "rfq_1",
        "market_ticker": "KXMVE-C1",
        "created_ts": "2026-07-05T10:00:00Z",
        "contracts_fp": "10.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": legs,
    }
    msg.update(overrides)
    return Rfq.from_ws(msg)


CROSS_EVENT_LEGS = [
    {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
    {"market_ticker": "M2", "side": "no", "event_ticker": "E2"},
]


def seed_event(h: Harness, event_ticker: str, exclusive: bool | None) -> None:
    h.metadata._events[event_ticker] = EventMeta(  # noqa: SLF001 (test seam)
        event_ticker=event_ticker,
        mutually_exclusive=exclusive,
        raw={},
        fetched_mono_ns=0,
    )


async def engine_harness() -> tuple[PricingEngine, Harness]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("KXMVE-C1")  # combo market metadata incl. 1-cent grid
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    engine = PricingEngine(h.feed, h.metadata, DOC_ASSUMED, PricingConfig())
    return engine, h


async def test_happy_path_produces_two_sided_quote() -> None:
    engine, _ = await engine_harness()
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, ConstructedQuote), result
    assert 0 < result.yes_bid_cc and 0 < result.no_bid_cc
    assert result.yes_bid_cc + result.no_bid_cc <= 10_000 - 100
    assert result.yes_bid_cc % 100 == 0  # on the 1-cent grid
    assert result.width_components_cc["legs"] == 200  # 2 legs x 100


async def test_impossible_combo_refused_not_arbed() -> None:
    engine, h = await engine_harness()
    rfq = combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "yes", "event_ticker": "E1"},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE


async def test_unknown_event_metadata_is_no_quote() -> None:
    engine, h = await engine_harness()
    rfq = combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E_UNSEEN"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E_UNSEEN"},
        ]
    )
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_CLASSIFIER_UNKNOWN


async def test_same_event_mixed_sides_quotes_with_extra_width() -> None:
    engine, _ = await engine_harness()
    cross = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    same_event = engine.price(
        combo(
            [
                {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
                {"market_ticker": "M2", "side": "no", "event_ticker": "E1"},
            ]
        ),
        time_to_close_s=100_000,
    )
    assert isinstance(cross, ConstructedQuote) and isinstance(same_event, ConstructedQuote)
    # correlation uncertainty must cost width relative to the cross-event case
    assert (
        same_event.width_components_cc["uncertainty"] > cross.width_components_cc["uncertainty"]
    )


async def test_missing_combo_grid_is_no_quote() -> None:
    engine, h = await engine_harness()
    del h.metadata._markets["KXMVE-C1"]  # noqa: SLF001
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_CLASSIFIER_UNKNOWN


async def test_invalid_leg_book_is_pricing_failure() -> None:
    engine, h = await engine_harness()
    h.feed.book("M2").invalidate("test")
    result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_PRICING_FAILED


async def test_target_cost_qty_estimate_rounds_up() -> None:
    engine, _ = await engine_harness()
    rfq = combo(CROSS_EVENT_LEGS, contracts_fp=None, target_cost_dollars="100.00")
    qty = engine._resolve_qty(rfq, fair_prob=0.30)  # noqa: SLF001
    assert qty is not None
    # $100 / $0.30 = 333.33... contracts -> rounds UP (more size => more width)
    assert qty == CentiContracts(-(-1_000_000 * 100 // 3_000))


async def test_no_sizing_mode_is_unknown() -> None:
    engine, _ = await engine_harness()
    rfq = combo(CROSS_EVENT_LEGS, contracts_fp=None)
    result = engine.price(rfq, time_to_close_s=100_000)
    assert isinstance(result, NoQuote)
    assert result.reason == ReasonCode.SKIP_CLASSIFIER_UNKNOWN
