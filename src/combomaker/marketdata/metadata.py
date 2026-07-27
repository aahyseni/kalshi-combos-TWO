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

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.exchange.rest import (
    DEFAULT_ENDPOINT_TOKEN_COST,
    ReadBudgetExhausted,
)
from combomaker.marketdata.grid import GridError, PriceGrid
from combomaker.ops.logging import get_logger
from combomaker.ops.write_budget import TokenBudget

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


def _aware_or_none(dt: datetime | None) -> datetime | None:
    """A tz-AWARE datetime or None — never a naive one (2026-07-25 review:
    comparing a naive persisted close_time against ``datetime.now(utc)``
    raised TypeError, crash-looping boot). A naive time is malformed input;
    the persistence filters treat it as undated (drop), never as a crash."""
    if dt is None or dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return None
    return dt


def write_persist_payload(path: str, payload: JsonDict) -> int:
    """Atomically write a ``build_persist_payload()`` result to ``path``
    (tmp + replace). Pure file I/O on plain data — safe to run in a worker
    thread (``asyncio.to_thread``) while the loop keeps mutating the cache.
    Returns markets written on success; **-1 on failure** (logged) — the
    caller must ``mark_dirty()`` on -1 so the next interval retries (the
    dirty flag was cleared at snapshot time; 2026-07-25 review)."""
    markets = payload.get("markets", [])
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:
        log.warning("metadata_cache_persist_failed", path=path, error=str(exc))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return -1
    return len(markets) if isinstance(markets, list) else 0


@dataclass(frozen=True, slots=True)
class EventMeta:
    event_ticker: str
    # None = the payload didn't say — callers must treat as UNKNOWN, not False.
    mutually_exclusive: bool | None
    raw: JsonDict
    fetched_mono_ns: int


class MetadataCache:
    """
    READ-BUDGET PACING (2026-07-26). Every network fetch below spends from a
    READ token bucket before it is emitted, exactly as the withdrawal wave
    spends from the write bucket. WHY here and not at the call sites: the
    metadata fetch is reached from the RFQ hot path (``_ensure_watched``), the
    status loop's metadata-change sampler, and the startup leg-arming pass, and
    a bucket that only some of them respect is not a bucket. A refusal RAISES
    ``ReadBudgetExhausted`` (a ``KalshiApiError``) rather than blocking: the
    caller's existing "this read failed, keep the cached value and retry on the
    next RFQ" path is already the correct behaviour, and it costs no wall time
    on the hot path — a local refusal is strictly cheaper than the 429 it
    replaces.

    ``read_budget=None`` (tests, backtests, paper) ⇒ unpaced, exactly as before.
    """

    def __init__(
        self,
        rest: RestLike,
        clock: Clock,
        *,
        ttl_s: float = 300.0,
        read_budget: TokenBudget | None = None,
        read_token_cost: int = DEFAULT_ENDPOINT_TOKEN_COST,
    ) -> None:
        self._rest = rest
        self._clock = clock
        self._ttl_s = ttl_s
        self._read_budget = read_budget
        self._read_token_cost = read_token_cost
        # Reads REFUSED locally (never emitted). The mirror image of a 429 —
        # and the number that should replace the incident's 5,726 of them.
        self.read_budget_refusals = 0
        self._markets: dict[str, MarketMeta] = {}
        self._events: dict[str, EventMeta] = {}
        # PERSISTENCE dirty flag (2026-07-25): set by every network-backed
        # store (refresh/event), cleared by ``persist`` — the maintenance
        # loop's "anything new worth writing?" read.
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    # --- disk persistence (2026-07-25) --------------------------------------
    # WHY: the cache was memory-only, so every restart re-fetched every
    # watched leg's metadata in one boot burst — a 429 storm (2,319 failures
    # in 36s at the 7/25 v6 boot) that left low-flow families (HR/HIT/TB/RBI:
    # 100% of their fetches failed) unpriceable until sparse RFQ flow healed
    # them, structurally biasing fills toward the high-flow K/total series.
    # Persisting markets whose close time is still in the FUTURE (+ the
    # events they reference) makes the NEXT boot warm: ``peek`` (the hot
    # path) serves instantly, while the reload stamps each entry TTL-expired
    # so the ASYNC ``market()`` path still revalidates lazily on first touch
    # (status can change; grid/event facts are immutable per market).
    # Failure anywhere logs and degrades to a cold cache — never blocks boot,
    # never blocks the loop (callers persist off-loop via asyncio.to_thread).

    def load_persisted(self, path: str) -> int:
        """Warm the cache from ``path``. Returns the number of markets kept
        (0 on missing/corrupt file — cold boot, never an error).

        The ENTIRE parse is inside the catch-all (2026-07-25 review: a
        valid-JSON-but-non-object file, or a naive close_time, escaped the
        json-only guard and crash-LOOPED boot until a human deleted the
        file — the literal 'corrupt file ⇒ cold boot' contract violation).
        A partial parse keeps what it validated; anything else is a colder
        boot, never a dead bot."""
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            log.warning("metadata_cache_load_failed", path=path, error=str(exc))
            return 0
        kept = 0
        kept_events = 0
        try:
            if not isinstance(payload, dict):
                raise TypeError(f"payload is {type(payload).__name__}, not dict")
            raw_markets = payload.get("markets")
            raw_events = payload.get("events")
            now_wall = datetime.now(timezone.utc)
            # TTL-expired stamp: peek() serves immediately; the async
            # market() path (which respects the TTL) refreshes on first
            # touch — quote_app's watch gates treat such entries as a
            # fetch-miss via needs_revalidation() (see below).
            stale_mono = self._clock.monotonic_ns() - int(
                (self._ttl_s + 1.0) * 1e9
            )
            for raw in raw_markets if isinstance(raw_markets, list) else []:
                if not isinstance(raw, dict):
                    continue
                meta = self._meta_from_market_payload(raw, stale_mono)
                if meta is None:
                    continue
                horizon = _aware_or_none(
                    meta.close_time
                ) or _aware_or_none(meta.expected_expiration_time)
                if horizon is None or horizon <= now_wall:
                    continue  # expired/undated/naive-dated — never resurrect
                if meta.ticker not in self._markets:
                    self._markets[meta.ticker] = meta
                    kept += 1
            wanted_events = {
                m.event_ticker for m in self._markets.values() if m.event_ticker
            }
            for raw in raw_events if isinstance(raw_events, list) else []:
                if not isinstance(raw, dict):
                    continue
                ticker = str(raw.get("event_ticker", ""))
                # Events live exactly as long as a kept market references
                # them (no retention constant to tune).
                if not ticker or ticker not in wanted_events:
                    continue
                if ticker in self._events:
                    continue
                flag = raw.get("mutually_exclusive")
                self._events[ticker] = EventMeta(
                    event_ticker=ticker,
                    mutually_exclusive=(
                        bool(flag) if isinstance(flag, bool) else None
                    ),
                    raw=raw,
                    fetched_mono_ns=stale_mono,
                )
                kept_events += 1
        except Exception as exc:
            log.warning(
                "metadata_cache_load_failed",
                path=path,
                error=repr(exc),
                markets_kept_before_failure=kept,
            )
            return kept
        log.info(
            "metadata_cache_loaded",
            path=path,
            markets=kept,
            events=kept_events,
        )
        return kept

    def needs_revalidation(self, ticker: str) -> bool:
        """True iff ``ticker`` is cached but TTL-expired — i.e. a warm-boot
        (or long-lived) entry whose status/close_time may be stale.

        2026-07-25 review (HIGH): the quote app's watch gates skip the async
        fetch whenever ``peek()`` is non-None, so a warm-loaded entry was
        NEVER revalidated — a mid-slate reschedule could leave a stale later
        close_time feeding the in-play gate for the market's whole life, and
        the metadata-change breaker (baseline = first peek) was structurally
        blind to cross-run changes. The gates now treat a stale entry as a
        fetch-miss: ``peek()`` keeps pricing warm (the whole point of the
        cache) while ONE spaced async refresh per stale leg heals
        status/close/expiry and re-arms the breaker."""
        cached = self._markets.get(ticker)
        if cached is None:
            return False
        return cached.age_s(self._clock) > self._ttl_s

    def mark_dirty(self) -> None:
        """Re-arm the flush after a FAILED disk write (2026-07-25 review:
        the dirty flag is cleared at snapshot time, so a transient write
        failure on a quiet book would never retry)."""
        self._dirty = True

    def build_persist_payload(self) -> JsonDict:
        """Build the persistable slice of the cache as a plain JSON payload:
        markets with a REAL payload (grid injections carry raw={} and are
        skipped — grids are per-day collections) and a future close time,
        plus the events they reference.

        SYNC + ON-LOOP by design: the caller snapshots here on the event
        loop (cheap dict walks), then hands the payload to
        ``write_persist_payload`` in a worker thread — iterating the LIVE
        dicts from a thread would race ``refresh()`` mutating them (dict
        changed size during iteration). Clears the dirty flag (the payload
        now reflects everything worth writing)."""
        now_wall = datetime.now(timezone.utc)
        markets: list[JsonDict] = []
        seen: set[str] = set()
        wanted_events: set[str] = set()
        for meta in self._markets.values():
            if not meta.raw or meta.ticker in seen:
                continue
            horizon = _aware_or_none(meta.close_time) or _aware_or_none(
                meta.expected_expiration_time
            )
            if horizon is None or horizon <= now_wall:
                continue
            seen.add(meta.ticker)
            markets.append(meta.raw)
            if meta.event_ticker:
                wanted_events.add(meta.event_ticker)
        events = [
            self._events[t].raw
            for t in sorted(wanted_events)
            if t in self._events and self._events[t].raw
        ]
        self._dirty = False
        return {"markets": markets, "events": events}

    def persist(self, path: str) -> int:
        """Snapshot + write in one call — for SYNC callers (tests, tools).
        The live loop must split it: ``build_persist_payload()`` on the loop,
        ``write_persist_payload`` via ``asyncio.to_thread``. A failed write
        re-arms the dirty flag and returns 0."""
        payload = self.build_persist_payload()
        written = write_persist_payload(path, payload)
        if written < 0:
            self._dirty = True
            return 0
        return written

    def _meta_from_market_payload(
        self, market: JsonDict, fetched_mono_ns: int
    ) -> MarketMeta | None:
        """Rebuild a MarketMeta from a persisted raw payload via the SAME
        parse path ``refresh`` uses (one grid parser, no drift)."""
        ticker = market.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            return None
        try:
            grid: PriceGrid | None = PriceGrid.from_market_payload(market)
        except GridError:
            grid = None
        return MarketMeta(
            ticker=ticker,
            status=str(market.get("status", "")),
            grid=grid,
            event_ticker=market.get("event_ticker"),
            close_time=_parse_time(market.get("close_time")),
            expected_expiration_time=_parse_time(
                market.get("expected_expiration_time")
            ),
            raw=market,
            fetched_mono_ns=fetched_mono_ns,
        )

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

    def _spend_read_budget(self, endpoint: str) -> None:
        """Pay for one GET out of the READ bucket, or refuse it locally."""
        budget = self._read_budget
        if budget is None:
            return
        if not budget.try_spend(self._read_token_cost):
            self.read_budget_refusals += 1
            raise ReadBudgetExhausted(endpoint, self._read_token_cost)

    async def refresh(self, ticker: str) -> MarketMeta:
        self._spend_read_budget(f"GET /markets/{ticker}")
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
        self._dirty = True
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
        self._spend_read_budget(f"GET /events/{event_ticker}")
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
        self._dirty = True
        return meta
