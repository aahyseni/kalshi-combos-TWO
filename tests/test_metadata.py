from typing import Any

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.marketdata.metadata import MetadataCache

JsonDict = dict[str, Any]

MARKET_PAYLOAD: JsonDict = {
    "market": {
        "ticker": "KXMVE-TEST",
        "status": "active",
        "event_ticker": "KXMVE-EV",
        "close_time": "2026-07-10T00:00:00Z",
        "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
    }
}


class FakeRest:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payload: JsonDict = MARKET_PAYLOAD

    async def get_market(self, ticker: str) -> JsonDict:
        self.calls.append(ticker)
        return self.payload

    async def get_event(self, ticker: str) -> JsonDict:
        self.calls.append(f"event:{ticker}")
        return {"event": {"event_ticker": ticker, "mutually_exclusive": True}}

    async def get_multivariate_collections(self, **params: str | int) -> JsonDict:
        return {"multivariate_contracts": []}


async def test_fetch_parses_grid_and_times() -> None:
    rest = FakeRest()
    cache = MetadataCache(rest, FakeClock())
    meta = await cache.market("KXMVE-TEST")
    assert meta.status == "active"
    assert meta.grid is not None
    assert meta.grid.is_on_grid(CentiCents(5_600))
    assert meta.close_time is not None and meta.close_time.year == 2026


async def test_ttl_caching() -> None:
    rest = FakeRest()
    clock = FakeClock()
    cache = MetadataCache(rest, clock, ttl_s=300)
    await cache.market("KXMVE-TEST")
    await cache.market("KXMVE-TEST")
    assert rest.calls == ["KXMVE-TEST"]  # served from cache
    clock.advance(301)
    await cache.market("KXMVE-TEST")
    assert rest.calls == ["KXMVE-TEST", "KXMVE-TEST"]


async def test_peek_never_fetches() -> None:
    rest = FakeRest()
    cache = MetadataCache(rest, FakeClock())
    assert cache.peek("KXMVE-TEST") is None
    await cache.market("KXMVE-TEST")
    assert cache.peek("KXMVE-TEST") is not None
    assert rest.calls == ["KXMVE-TEST"]


async def test_missing_grid_yields_none_not_default() -> None:
    rest = FakeRest()
    rest.payload = {"market": {"ticker": "X", "status": "active"}}
    cache = MetadataCache(rest, FakeClock())
    meta = await cache.market("X")
    assert meta.grid is None  # quoting layer must treat as no-quote


async def test_event_fetch_and_peek_discipline() -> None:
    rest = FakeRest()
    cache = MetadataCache(rest, FakeClock())
    # peek before fetch: UNKNOWN, no network
    assert cache.event_mutually_exclusive("EV1") is None
    assert rest.calls == []
    meta = await cache.event("EV1")
    assert meta.mutually_exclusive is True
    assert cache.event_mutually_exclusive("EV1") is True
    # cached: second call doesn't refetch
    await cache.event("EV1")
    assert rest.calls == ["event:EV1"]


async def test_event_without_flag_is_unknown_not_false() -> None:
    class NoFlagRest(FakeRest):
        async def get_event(self, ticker: str) -> JsonDict:
            return {"event": {"event_ticker": ticker}}

    cache = MetadataCache(NoFlagRest(), FakeClock())
    meta = await cache.event("EV2")
    assert meta.mutually_exclusive is None
    assert cache.event_mutually_exclusive("EV2") is None
