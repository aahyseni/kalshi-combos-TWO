"""Orderbook feed: WS subscriptions → per-market mirrors, with gap discipline.

Sequence numbers are per-subscription (per ``sid``) and control acks
(``ok``/``unsubscribed``) consume seq slots too (docs/api-notes/orderbooks.md),
so the continuity checker counts every sid-carrying message we can see for our
sids. On ANY gap or corruption:

1. every book under that sid is invalidated FIRST,
2. ``on_invalidate`` callbacks fire (quote cancel-all subscribes here),
3. only then do we ask for fresh snapshots (``update_subscription`` /
   ``get_snapshot`` — the documented resync primitive).

Never quote off state you can't prove is current: consumers gate on
``book.valid`` plus ``feed_healthy``.

``use_yes_price`` is pinned explicitly to False in subscribe params — docs warn
the server default will flip in a future release, which would silently change
delta price semantics.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.core.money import MoneyParseError, cc_from_dollars_str
from combomaker.core.quantity import QuantityParseError, qty_from_fp_str
from combomaker.marketdata.orderbook import Level, OrderbookMirror
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics

log = get_logger(__name__)

JsonDict = dict[str, Any]

InvalidateHandler = Callable[[str], Awaitable[None]]


class WsLike(Protocol):
    """The slice of WsManager the feed needs (kept narrow for tests)."""

    def on_message(
        self, msg_type: str, handler: Callable[[JsonDict], Awaitable[None]]
    ) -> None: ...

    def on_disconnect(self, handler: Callable[[], Awaitable[None]]) -> None: ...

    def add_subscription(
        self,
        channels: list[str],
        *,
        on_subscribed: Callable[[int], Awaitable[None]] | None = None,
        **params_extra: Any,
    ) -> None: ...

    async def send_command(self, cmd: str, params: dict[str, Any]) -> int: ...

    @property
    def healthy(self) -> bool: ...

    @property
    def last_rx_age_s(self) -> float | None: ...


class OrderbookFeed:
    def __init__(self, ws: WsLike, clock: Clock, metrics: Metrics | None = None) -> None:
        self._ws = ws
        self._clock = clock
        self._metrics = metrics or Metrics()
        self._books: dict[str, OrderbookMirror] = {}
        self._sid_last_seq: dict[int, int] = {}
        self._sid_tickers: dict[int, tuple[str, ...]] = {}
        self._on_invalidate: list[InvalidateHandler] = []

        ws.on_message("orderbook_snapshot", self._handle_snapshot)
        ws.on_message("orderbook_delta", self._handle_delta)
        ws.on_message("ok", self._handle_control)
        ws.on_message("unsubscribed", self._handle_control)
        ws.on_disconnect(self._handle_disconnect)

    # --- public API ---

    def on_invalidate(self, handler: InvalidateHandler) -> None:
        """Called with a reason on every gap/disconnect, AFTER books invalidate
        and BEFORE resync — quote cancel-all belongs here."""
        self._on_invalidate.append(handler)

    def watch(self, tickers: Sequence[str]) -> None:
        """Mirror these markets (one WS subscription per watch call)."""
        ticker_tuple = tuple(tickers)
        for ticker in ticker_tuple:
            self._books.setdefault(ticker, OrderbookMirror(ticker, self._clock))

        async def on_subscribed(sid: int) -> None:
            # Re-acks after reconnect assign a NEW sid: drop any old sid that
            # pointed at this ticker set and start a fresh seq stream.
            for old_sid, sid_tickers in list(self._sid_tickers.items()):
                if sid_tickers == ticker_tuple and old_sid != sid:
                    self._sid_tickers.pop(old_sid, None)
                    self._sid_last_seq.pop(old_sid, None)
            self._sid_tickers[sid] = ticker_tuple
            self._sid_last_seq.pop(sid, None)
            log.info("book_feed_subscribed", sid=sid, tickers=list(ticker_tuple))

        self._ws.add_subscription(
            ["orderbook_delta"],
            on_subscribed=on_subscribed,
            market_tickers=list(ticker_tuple),
            use_yes_price=False,  # pinned: server default flips in a future release
        )

    def book(self, ticker: str) -> OrderbookMirror:
        return self._books[ticker]

    def tickers(self) -> tuple[str, ...]:
        return tuple(self._books)

    @property
    def feed_healthy(self) -> bool:
        return self._ws.healthy

    @property
    def rx_age_s(self) -> float | None:
        """Seconds since server traffic — the freshness proof for last look."""
        return self._ws.last_rx_age_s

    def all_valid(self, tickers: Sequence[str]) -> bool:
        return self.feed_healthy and all(
            t in self._books and self._books[t].valid for t in tickers
        )

    # --- message handling ---

    async def _handle_snapshot(self, envelope: JsonDict) -> None:
        sid = int(envelope.get("sid", -1))
        if sid not in self._sid_tickers:
            return
        if not self._seq_ok(sid, envelope):
            await self._gap(sid, "seq_gap_snapshot")
            # a snapshot IS fresh state — fall through and apply it
        msg = envelope.get("msg", {})
        ticker = str(msg.get("market_ticker", ""))
        book = self._books.get(ticker)
        if book is None:
            return
        try:
            yes = _parse_levels(msg.get("yes_dollars_fp") or [])
            no = _parse_levels(msg.get("no_dollars_fp") or [])
        except (MoneyParseError, QuantityParseError, ValueError) as exc:
            log.warning("book_snapshot_unparseable", ticker=ticker, error=str(exc))
            book.invalidate("unparseable_snapshot")
            return
        book.apply_snapshot(yes, no)
        self._metrics.inc("book.snapshot")

    async def _handle_delta(self, envelope: JsonDict) -> None:
        sid = int(envelope.get("sid", -1))
        if sid not in self._sid_tickers:
            return
        if not self._seq_ok(sid, envelope):
            await self._gap(sid, "seq_gap_delta")
            return  # this delta may follow missed ones; wait for the snapshot
        msg = envelope.get("msg", {})
        ticker = str(msg.get("market_ticker", ""))
        book = self._books.get(ticker)
        if book is None:
            return
        side = str(msg.get("side", ""))
        if side not in ("yes", "no"):
            log.warning("book_delta_bad_side", ticker=ticker, side=side)
            await self._gap(sid, "bad_delta_side")
            return
        try:
            price = cc_from_dollars_str(str(msg["price_dollars"]))
            delta = qty_from_fp_str(str(msg["delta_fp"]))
        except (KeyError, MoneyParseError, QuantityParseError) as exc:
            log.warning("book_delta_unparseable", ticker=ticker, error=str(exc))
            await self._gap(sid, "unparseable_delta")
            return
        ts_ms = msg.get("ts_ms")
        if not book.apply_delta(side, price, delta, int(ts_ms) if ts_ms is not None else None):
            await self._gap(sid, "negative_count")
            return
        self._metrics.inc("book.delta")

    async def _handle_control(self, envelope: JsonDict) -> None:
        # ok/unsubscribed acks consume seq slots on their subscription's stream.
        sid = int(envelope.get("sid", -1))
        if sid not in self._sid_tickers or "seq" not in envelope:
            return
        if not self._seq_ok(sid, envelope):
            await self._gap(sid, "seq_gap_control")

    async def _handle_disconnect(self) -> None:
        for book in self._books.values():
            book.invalidate("ws_disconnect")
        self._sid_last_seq.clear()
        self._sid_tickers.clear()
        await self._fire_invalidate("ws_disconnect")

    # --- gap machinery ---

    def _seq_ok(self, sid: int, envelope: JsonDict) -> bool:
        seq = envelope.get("seq")
        if seq is None:
            return True
        seq = int(seq)
        last = self._sid_last_seq.get(sid)
        self._sid_last_seq[sid] = seq
        if last is None:
            return True  # baseline (fresh subscription or post-gap re-adoption)
        return seq == last + 1

    async def _gap(self, sid: int, reason: str) -> None:
        self._metrics.inc(f"book.gap.{reason}")
        tickers = self._sid_tickers.get(sid, ())
        log.warning("book_feed_gap", sid=sid, reason=reason, tickers=list(tickers))
        for ticker in tickers:
            if ticker in self._books:
                self._books[ticker].invalidate(reason)
        # Unknown seq semantics around control frames: after a gap, adopt the
        # next observed seq as the new baseline instead of alarming forever.
        self._sid_last_seq.pop(sid, None)
        await self._fire_invalidate(reason)
        try:
            await self._ws.send_command(
                "update_subscription",
                {"sids": [sid], "action": "get_snapshot", "market_tickers": list(tickers)},
            )
        except Exception as exc:
            # Reconnect (which re-snapshots everything) is the fallback path.
            log.warning("book_resync_request_failed", sid=sid, error=repr(exc))

    async def _fire_invalidate(self, reason: str) -> None:
        for handler in self._on_invalidate:
            try:
                await handler(reason)
            except Exception:
                log.exception("book_invalidate_handler_failed", reason=reason)


def _parse_levels(raw: list[list[str]]) -> list[Level]:
    return [(cc_from_dollars_str(str(price)), qty_from_fp_str(str(count))) for price, count in raw]
