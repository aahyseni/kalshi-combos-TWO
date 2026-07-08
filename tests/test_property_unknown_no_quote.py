"""Property (quiet-failure defense #2): no UNKNOWN classification can reach
CreateQuote — at ANY width. Exhaustive mutation sweep over every way a combo
can be under-understood; each one must yield NoQuote from the pricing engine,
while the unmutated baseline quotes fine (so the sweep isn't vacuous).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.models import Rfq
from tests.test_filters import Harness
from tests.test_pricing_engine import (
    CROSS_EVENT_LEGS,
    combo,
    engine_harness,
    seed_event,
)

Mutation = Callable[[Harness], Awaitable[Rfq]]


async def baseline(h: Harness) -> Rfq:
    return combo(CROSS_EVENT_LEGS)


async def unknown_leg_side(h: Harness) -> Rfq:
    return combo(
        [
            {"market_ticker": "M1", "side": "long", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E2"},
        ]
    )


async def missing_event_ticker(h: Harness) -> Rfq:
    return combo(
        [
            {"market_ticker": "M1", "side": "yes"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E2"},
        ]
    )


async def unseen_event(h: Harness) -> Rfq:
    return combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E_NEW"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E_NEW"},
        ]
    )


async def event_flag_unknown(h: Harness) -> Rfq:
    seed_event(h, "E_FLAGLESS", exclusive=None)
    return combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E_FLAGLESS"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E_FLAGLESS"},
        ]
    )


async def impossible_pair(h: Harness) -> Rfq:
    # Mutual-exclusion IMPOSSIBLE (two YES legs of exclusive E1): NOT farmable
    # (metadata-dependent), so it always no-quotes — stays in this sweep.
    return combo(
        [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "yes", "event_ticker": "E1"},
        ]
    )


# NOTE: same_market_both_sides is a LOGICALLY-CERTAIN (farmable) impossibility,
# so with farm_impossible_combos ON it is deliberately QUOTED, not declined — it
# is not an "under-understood" combo and lives in the farm tests
# (test_pricing_engine.test_farm_*), not this UNKNOWN/no-quote sweep.


async def missing_combo_grid(h: Harness) -> Rfq:
    del h.metadata._markets["KXMVE-C1"]  # noqa: SLF001
    return combo(CROSS_EVENT_LEGS)


async def invalid_leg_book(h: Harness) -> Rfq:
    h.feed.book("M1").invalidate("mutation")
    return combo(CROSS_EVENT_LEGS)


async def unwatched_leg(h: Harness) -> Rfq:
    return combo(
        [
            {"market_ticker": "M_UNSEEN", "side": "yes", "event_ticker": "E1"},
            {"market_ticker": "M2", "side": "no", "event_ticker": "E2"},
        ]
    )


async def no_sizing_mode(h: Harness) -> Rfq:
    return combo(CROSS_EVENT_LEGS, contracts_fp=None)


async def not_a_combo(h: Harness) -> Rfq:
    return combo([], mve_collection_ticker=None)


MUTATIONS: list[Mutation] = [
    unknown_leg_side,
    missing_event_ticker,
    unseen_event,
    event_flag_unknown,
    impossible_pair,
    missing_combo_grid,
    invalid_leg_book,
    unwatched_leg,
    no_sizing_mode,
    not_a_combo,
]


async def test_baseline_actually_quotes() -> None:
    engine, h = await engine_harness()
    rfq = await baseline(h)
    assert isinstance(engine.price(rfq, time_to_close_s=1e6), ConstructedQuote)


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.__name__)
async def test_unknown_never_reaches_create_quote(mutation: Mutation) -> None:
    engine, h = await engine_harness()
    rfq = await mutation(h)
    result = engine.price(rfq, time_to_close_s=1e6)
    assert isinstance(result, NoQuote), (
        f"mutation {mutation.__name__} produced a quote: {result}"
    )


async def test_engine_price_never_raises_on_mutations() -> None:
    """The hot path must decline, not crash."""
    for mutation in MUTATIONS:
        engine, h = await engine_harness()
        rfq = await mutation(h)
        engine.price(rfq, time_to_close_s=1e6)  # no exception is the assertion
