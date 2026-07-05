"""OrderbookFeed replay tests with a fake WS (no network)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from combomaker.core.clock import FakeClock
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.orderbook import OrderbookMirror

JsonDict = dict[str, Any]


class FakeWs:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Callable[[JsonDict], Awaitable[None]]]] = {}
        self.disconnect_handlers: list[Callable[[], Awaitable[None]]] = []
        self.subscriptions: list[dict[str, Any]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self._healthy = True

    def on_message(self, msg_type: str, handler: Callable[[JsonDict], Awaitable[None]]) -> None:
        self.handlers.setdefault(msg_type, []).append(handler)

    def on_disconnect(self, handler: Callable[[], Awaitable[None]]) -> None:
        self.disconnect_handlers.append(handler)

    def add_subscription(
        self,
        channels: list[str],
        *,
        on_subscribed: Callable[[int], Awaitable[None]] | None = None,
        **params_extra: Any,
    ) -> None:
        self.subscriptions.append(
            {"channels": channels, "on_subscribed": on_subscribed, **params_extra}
        )

    async def send_command(self, cmd: str, params: dict[str, Any]) -> int:
        self.commands.append((cmd, params))
        return len(self.commands)

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_rx_age_s(self) -> float | None:
        return 0.1 if self._healthy else None

    # test drivers
    async def ack_subscription(self, index: int, sid: int) -> None:
        callback = self.subscriptions[index]["on_subscribed"]
        assert callback is not None
        await callback(sid)

    async def deliver(self, envelope: JsonDict) -> None:
        for handler in self.handlers.get(str(envelope.get("type")), []):
            await handler(envelope)

    async def drop_connection(self) -> None:
        for handler in self.disconnect_handlers:
            await handler()


def snapshot_env(sid: int, seq: int, ticker: str) -> JsonDict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
            "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]],
        },
    }


def delta_env(sid: int, seq: int, ticker: str, price: str, delta: str, side: str) -> JsonDict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": price,
            "delta_fp": delta,
            "side": side,
            "ts_ms": 1_700_000_000_000,
        },
    }


async def make_feed(tickers: list[str], sid: int = 7) -> tuple[OrderbookFeed, FakeWs, list[str]]:
    ws = FakeWs()
    feed = OrderbookFeed(ws, FakeClock())
    invalidations: list[str] = []

    async def on_invalidate(reason: str) -> None:
        invalidations.append(reason)

    feed.on_invalidate(on_invalidate)
    feed.watch(tickers)
    await ws.ack_subscription(0, sid)
    return feed, ws, invalidations


async def test_use_yes_price_pinned_false() -> None:
    _, ws, _ = await make_feed(["A"])
    assert ws.subscriptions[0]["use_yes_price"] is False


async def test_snapshot_then_delta_updates_book() -> None:
    feed, ws, _ = await make_feed(["A"])
    await ws.deliver(snapshot_env(7, 1, "A"))
    book: OrderbookMirror = feed.book("A")
    assert book.valid
    assert book.top().yes_bid_cc == 2_200
    await ws.deliver(delta_env(7, 2, "A", "0.2300", "50.00", "yes"))
    assert book.top().yes_bid_cc == 2_300
    # varying decimal count in delta prices must parse ("0.960" style)
    await ws.deliver(delta_env(7, 3, "A", "0.240", "10.00", "yes"))
    assert book.top().yes_bid_cc == 2_400


async def test_seq_gap_invalidates_and_requests_snapshot() -> None:
    feed, ws, invalidations = await make_feed(["A", "B"])
    await ws.deliver(snapshot_env(7, 1, "A"))
    await ws.deliver(snapshot_env(7, 2, "B"))
    await ws.deliver(delta_env(7, 5, "A", "0.2300", "50.00", "yes"))  # gap: 2 -> 5
    assert not feed.book("A").valid
    assert not feed.book("B").valid  # whole sid invalidated, not just one market
    assert invalidations == ["seq_gap_delta"]
    assert ws.commands[-1][0] == "update_subscription"
    assert ws.commands[-1][1]["action"] == "get_snapshot"
    assert ws.commands[-1][1]["sids"] == [7]
    # recovery: snapshots re-validate and seq baseline re-adopts
    await ws.deliver(snapshot_env(7, 9, "A"))
    await ws.deliver(snapshot_env(7, 10, "B"))
    assert feed.book("A").valid and feed.book("B").valid
    await ws.deliver(delta_env(7, 11, "A", "0.2300", "50.00", "yes"))
    assert feed.book("A").top().yes_bid_cc == 2_300


async def test_control_acks_consume_seq_slots() -> None:
    feed, ws, invalidations = await make_feed(["A"])
    await ws.deliver(snapshot_env(7, 1, "A"))
    await ws.deliver({"type": "ok", "id": 5, "sid": 7, "seq": 2, "msg": {}})
    await ws.deliver(delta_env(7, 3, "A", "0.2300", "50.00", "yes"))
    assert feed.book("A").valid
    assert invalidations == []  # no false gap around the control ack


async def test_negative_count_treated_as_gap() -> None:
    feed, ws, invalidations = await make_feed(["A"])
    await ws.deliver(snapshot_env(7, 1, "A"))
    await ws.deliver(delta_env(7, 2, "A", "0.5600", "-999.00", "no"))
    assert not feed.book("A").valid
    assert invalidations == ["negative_count"]


async def test_disconnect_invalidates_everything() -> None:
    feed, ws, invalidations = await make_feed(["A"])
    await ws.deliver(snapshot_env(7, 1, "A"))
    await ws.drop_connection()
    assert not feed.book("A").valid
    assert invalidations == ["ws_disconnect"]
    # after reconnect a new sid arrives and fresh snapshots restore validity
    await ws.ack_subscription(0, 8)
    await ws.deliver(snapshot_env(8, 1, "A"))
    assert feed.book("A").valid
    assert feed.all_valid(["A"])


async def test_unknown_sid_ignored() -> None:
    feed, ws, invalidations = await make_feed(["A"])
    await ws.deliver(snapshot_env(99, 1, "A"))
    assert not feed.book("A").valid
    assert invalidations == []


async def test_unparseable_delta_is_gap() -> None:
    feed, ws, invalidations = await make_feed(["A"])
    await ws.deliver(snapshot_env(7, 1, "A"))
    await ws.deliver(delta_env(7, 2, "A", "0.00005", "50.00", "yes"))  # sub-cc price
    assert not feed.book("A").valid
    assert invalidations == ["unparseable_delta"]


async def test_all_valid_requires_feed_health() -> None:
    feed, ws, _ = await make_feed(["A"])
    await ws.deliver(snapshot_env(7, 1, "A"))
    assert feed.all_valid(["A"])
    ws._healthy = False
    assert not feed.all_valid(["A"])
