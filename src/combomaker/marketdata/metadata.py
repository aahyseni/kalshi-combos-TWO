"""Market/collection metadata cache.

Two access styles with different guarantees:

- ``market(ticker)`` — async, may hit REST, respects a TTL. Used by intake and
  reconciliation paths.
- ``peek(ticker)`` — sync, in-memory only, never touches the network. The ONLY
  style permitted on the hot path (pricing at rfq_created, last look).

An unknown or grid-less market never gets a guessed default: ``MarketMeta.grid``
is None when ``price_ranges`` was absent/malformed and the quoting layer must
treat that as no-quote (quiet-failure defense #2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.marketdata.grid import GridError, PriceGrid
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

JsonDict = dict[str, Any]


class RestLike(Protocol):
    async def get_market(self, ticker: str) -> JsonDict: ...

    async def get_event(self, ticker: str) -> JsonDict: ...

    async def get_multivariate_collections(self, **params: str | int) -> JsonDict: ...


@dataclass(frozen=True, slots=True)
class MarketMeta:
    ticker: str
    status: str
    grid: PriceGrid | None            # None ⇒ unquotable (unknown grid)
    event_ticker: str | None
    close_time: datetime | None       # exchange-reported close, if parseable
    expected_expiration_time: datetime | None
    raw: JsonDict                     # full payload for fields we don't model yet
    fetched_mono_ns: int

    def age_s(self, clock: Clock) -> float:
        return (clock.monotonic_ns() - self.fetched_mono_ns) / 1e9


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class EventMeta:
    event_ticker: str
    # None = the payload didn't say — callers must treat as UNKNOWN, not False.
    mutually_exclusive: bool | None
    raw: JsonDict
    fetched_mono_ns: int


class MetadataCache:
    def __init__(self, rest: RestLike, clock: Clock, *, ttl_s: float = 300.0) -> None:
        self._rest = rest
        self._clock = clock
        self._ttl_s = ttl_s
        self._markets: dict[str, MarketMeta] = {}
        self._events: dict[str, EventMeta] = {}

    def peek(self, ticker: str) -> MarketMeta | None:
        """In-memory lookup only — hot-path safe, no network ever."""
        return self._markets.get(ticker)

    def put_combo_grid(self, ticker: str, grid: PriceGrid) -> None:
        """Inject a combo market's grid WITHOUT a network fetch (throughput fix,
        2026-07-14). Combo market tickers are UNIQUE per RFQ, so fetching each
        combo's grid is a per-RFQ REST read that blows the read-rate budget (live
        429 storm). But every combo in a collection shares one grid structure, so
        the quote app fetches the grid ONCE per collection and injects it here for
        the collection's other combos. Only the grid is read downstream
        (construct_quote); the combo's own event/close metadata is never used (the
        pregame gate keys on LEG metadata, and the metadata-change breaker samples
        the book's LEG markets, not the combo)."""
        self._markets[ticker] = MarketMeta(
            ticker=ticker,
            status="active",
            grid=grid,
            event_ticker=None,
            close_time=None,
            expected_expiration_time=None,
            raw={},
            fetched_mono_ns=self._clock.monotonic_ns(),
        )

    async def market(self, ticker: str, *, max_age_s: float | None = None) -> MarketMeta:
        cached = self._markets.get(ticker)
        budget = self._ttl_s if max_age_s is None else max_age_s
        if cached is not None and cached.age_s(self._clock) <= budget:
            return cached
        return await self.refresh(ticker)

    async def refresh(self, ticker: str) -> MarketMeta:
        payload = await self._rest.get_market(ticker)
        market = payload.get("market", payload)  # endpoint wraps in {"market": {...}}
        grid: PriceGrid | None
        try:
            grid = PriceGrid.from_market_payload(market)
        except GridError as exc:
            log.warning("market_grid_unusable", ticker=ticker, error=str(exc))
            grid = None
        meta = MarketMeta(
            ticker=str(market.get("ticker", ticker)),
            status=str(market.get("status", "")),
            grid=grid,
            event_ticker=market.get("event_ticker"),
            close_time=_parse_time(market.get("close_time")),
            expected_expiration_time=_parse_time(market.get("expected_expiration_time")),
            raw=market,
            fetched_mono_ns=self._clock.monotonic_ns(),
        )
        self._markets[meta.ticker] = meta
        if meta.ticker != ticker:  # be forgiving about alias lookups
            self._markets[ticker] = meta
        return meta

    # --- events ---

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        """EventInfoProvider implementation. Peek-only (hot-path safe):
        uncached or flag-less events return None ⇒ UNKNOWN upstream."""
        cached = self._events.get(event_ticker)
        return None if cached is None else cached.mutually_exclusive

    def peek_event(self, event_ticker: str) -> EventMeta | None:
        return self._events.get(event_ticker)

    async def event(self, event_ticker: str, *, max_age_s: float | None = None) -> EventMeta:
        cached = self._events.get(event_ticker)
        budget = self._ttl_s if max_age_s is None else max_age_s
        if cached is not None:
            age_s = (self._clock.monotonic_ns() - cached.fetched_mono_ns) / 1e9
            if age_s <= budget:
                return cached
        payload = await self._rest.get_event(event_ticker)
        event = payload.get("event", payload)
        flag = event.get("mutually_exclusive")
        meta = EventMeta(
            event_ticker=str(event.get("event_ticker", event_ticker)),
            mutually_exclusive=bool(flag) if isinstance(flag, bool) else None,
            raw=event,
            fetched_mono_ns=self._clock.monotonic_ns(),
        )
        self._events[event_ticker] = meta
        return meta
