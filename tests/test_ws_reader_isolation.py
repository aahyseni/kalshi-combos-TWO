"""READER ISOLATION (2026-09-05, the code-25 day) — unit proofs.

The exchange closed our communications subscription with error 25
("Subscription buffer overflow": the client read too slowly) 0.7/h overnight
rising to ~1/min on a fresh boot, and three confirm halts that day were all
``HTTP 400 expired`` — accepts SEEN late. Root cause: the socket reader was a
coroutine on the main loop, so every synchronous stretch of main-loop work
(a 60 s maintenance pass on the 213 GB store) stopped it reading.

The repair puts the socket on its own thread + loop (``_ReaderThread``) and
hands frames over through ``_Lanes``. Proofs here, each against a REAL thread
and a synthetic main-loop stall (``time.sleep`` on the loop):

  1. The socket is fully drained WHILE the main loop is blocked.
  2. No PRIORITY frame is ever lost; every one is dispatched after the stall.
  3. MARKET frames shed only by capacity (oldest first) and by wire age at
     dequeue; fresh ones after the stall dispatch normally; never a disconnect.
  4. ``force_reconnect`` (the 10/17 path) closes on the socket loop, runs
     on_disconnect BEFORE the reconnect, and re-sends subscriptions on the
     NEW socket only.
  5. Code 25 is logged CRITICAL with reader diagnostics if it ever recurs.
  6. Clean stop: thread joined, no reader loop left behind.
"""

from __future__ import annotations

import asyncio
import collections
import json
import threading
import time
from typing import Any

import aiohttp
import structlog.testing

from combomaker.core.clock import SystemClock
from combomaker.exchange.ws import SUBSCRIPTION_BUFFER_OVERFLOW_CODE, WsManager
from combomaker.ops.metrics import Metrics

JsonDict = dict[str, Any]


class _Frame:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload)


class _ThreadSocket:
    """Socket double that lives on the READER thread's loop. The test feeds
    frames from the main thread (possibly while the main loop is blocked);
    ``pulled_at`` records the monotonic instant the reader took each frame."""

    def __init__(self) -> None:
        self._frames: collections.deque[_Frame] = collections.deque()
        self._lock = threading.Lock()
        self._eof = False
        self.closed = False
        self.close_calls = 0
        self.sent: list[str] = []
        self.pulled_at: list[int] = []

    def feed(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._frames.append(_Frame(payload))

    def eof(self) -> None:
        self._eof = True

    def __aiter__(self) -> _ThreadSocket:
        return self

    async def __anext__(self) -> _Frame:
        while True:
            if self.closed:
                raise StopAsyncIteration
            with self._lock:
                if self._frames:
                    self.pulled_at.append(time.monotonic_ns())
                    return self._frames.popleft()
                if self._eof:
                    raise StopAsyncIteration
            await asyncio.sleep(0.001)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


class _Ctx:
    def __init__(self, sock: _ThreadSocket) -> None:
        self._sock = sock

    async def __aenter__(self) -> _ThreadSocket:
        return self._sock

    async def __aexit__(self, *exc: object) -> bool:
        self._sock.closed = True
        return False


class _Connector:
    """Hands out sockets in order; records every connect and which thread it
    happened on (must be the reader thread, never the main one)."""

    def __init__(self, sockets: list[_ThreadSocket]) -> None:
        self._sockets = list(sockets)
        self.connects = 0
        self.connect_threads: list[str] = []
        self.connected_at: list[int] = []

    def __call__(self, session: Any, url: str, headers: dict[str, str]) -> _Ctx:
        self.connects += 1
        self.connect_threads.append(threading.current_thread().name)
        self.connected_at.append(time.monotonic_ns())
        if not self._sockets:
            raise ConnectionError("no more sockets scripted")
        return _Ctx(self._sockets.pop(0))


class _Signer:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {}


def _manager(
    connector: _Connector, metrics: Metrics | None = None, **kw: Any
) -> tuple[WsManager, Metrics]:
    metrics = metrics or Metrics()
    m = WsManager(
        "wss://example/ws",
        _Signer(),  # type: ignore[arg-type]
        SystemClock(),
        metrics,
        name="t",
        backoff_initial_s=0.01,
        backoff_max_s=0.02,
        connect=connector,
        **kw,
    )
    return m, metrics


async def _until(pred: Any, within_s: float = 2.0) -> None:
    async def _wait() -> None:
        while not pred():  # noqa: ASYNC110 — bounded by wait_for
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait(), timeout=within_s)


async def _settled(m: WsManager, within_s: float = 2.0) -> None:
    await _until(lambda: not any(m.lane_depths().values()) and not m._lanes.wake_pending, within_s)


# --------------------------------------------------------------------------- #
# 1-3. The stall: socket drained meanwhile, priority intact, market shed/aged
# --------------------------------------------------------------------------- #


async def test_reader_drains_socket_and_keeps_every_priority_frame_through_a_main_loop_stall() -> (
    None
):
    sock = _ThreadSocket()
    connector = _Connector([sock])
    m, metrics = _manager(connector)
    m.mark_priority("quote_accepted")
    # stale bound 0.2s: every market frame fed during the 0.5s stall is dead
    # at dequeue; capacity 10: the 30 fed frames shed the oldest 20 on the way.
    m.mark_sheddable("rfq_created", stale_after_s=0.2)
    m._lanes.capacity = 10
    seen: list[str] = []

    async def rec(msg: JsonDict) -> None:
        seen.append(f"{msg['type']}:{msg['n']}")

    m.on_message("rfq_created", rec)
    m.on_message("quote_accepted", rec)
    m.start()
    try:
        await _until(lambda: m.connected)
        assert connector.connect_threads == ["t-reader"]

        # Feed 30 market + 3 priority frames from a helper thread WHILE the
        # main loop is blocked in time.sleep — the reader must take them all.
        def feeder() -> None:
            for i in range(30):
                sock.feed({"type": "rfq_created", "n": i})
                if i in (5, 15, 25):
                    sock.feed({"type": "quote_accepted", "n": i})
                time.sleep(0.004)

        t = threading.Thread(target=feeder)
        stall_start = time.monotonic_ns()
        t.start()
        time.sleep(0.5)  # noqa: ASYNC251 — THE STALL: nothing on the main loop runs
        t.join()
        stall_end = time.monotonic_ns()

        # 1. Every frame was pulled off the socket DURING the stall.
        assert len(sock.pulled_at) == 33
        assert all(stall_start <= at <= stall_end for at in sock.pulled_at)
        assert m._frames_read == 33
        # Lanes hold what survived capacity: all 3 accepts, the newest 10 market.
        depths = m.lane_depths()
        assert depths["priority"] == 3
        assert depths["market"] == 10

        await _settled(m)
        # 2. No priority frame lost; dispatched in lane FIFO.
        assert [s for s in seen if s.startswith("quote_accepted")] == [
            "quote_accepted:5",
            "quote_accepted:15",
            "quote_accepted:25",
        ]
        # 3. Every surviving market frame was older than the 0.2s bound at
        # dequeue (fed >= 0.3s before the loop resumed) — dropped by age,
        # not dispatched; the first 20 were shed by capacity on the reader.
        assert not [s for s in seen if s.startswith("rfq_created")]
        assert metrics.counter("t.shed_market_frames") == 20
        assert metrics.counter("t.shed.rfq_created") == 20
        assert metrics.counter("t.stale_market_frames") == 10
        assert metrics.counter("t.stale.rfq_created") == 10
        assert metrics.counter("t.priority_frame") == 3
        assert metrics.counter("t.connect") == 1
        assert sock.close_calls == 0  # never a disconnect for market pressure
        assert metrics.counter("t.dispatch_queue_overflow") == 0

        # Fresh market frames after the stall dispatch normally.
        for i in range(100, 105):
            sock.feed({"type": "rfq_created", "n": i})
        await _until(lambda: len([s for s in seen if s.startswith("rfq_created")]) == 5)
        assert [s for s in seen if s.startswith("rfq_created")] == [
            f"rfq_created:{i}" for i in range(100, 105)
        ]
    finally:
        await m.stop()


async def test_ageless_market_type_is_never_age_dropped() -> None:
    """``rfq_deleted`` is registered sheddable WITHOUT a stale bound: a late
    delete is still a delete (mirror consistency), only capacity may shed it."""
    sock = _ThreadSocket()
    m, metrics = _manager(_Connector([sock]))
    m.mark_sheddable("rfq_created", stale_after_s=0.05)
    m.mark_sheddable("rfq_deleted")
    seen: list[str] = []

    async def rec(msg: JsonDict) -> None:
        seen.append(f"{msg['type']}:{msg['n']}")

    m.on_message("rfq_created", rec)
    m.on_message("rfq_deleted", rec)
    m.start()
    try:
        await _until(lambda: m.connected)

        def feeder() -> None:
            sock.feed({"type": "rfq_created", "n": 1})
            sock.feed({"type": "rfq_deleted", "n": 2})

        t = threading.Thread(target=feeder)
        t.start()
        time.sleep(0.15)  # noqa: ASYNC251 — stall past the created-frame bound
        t.join()
        assert m._frames_read == 2  # both read during the stall
        await _settled(m)
        assert seen == ["rfq_deleted:2"]
        assert metrics.counter("t.stale.rfq_created") == 1
        assert metrics.counter("t.stale.rfq_deleted") == 0
    finally:
        await m.stop()


# --------------------------------------------------------------------------- #
# 4. Reconnect semantics unchanged: on_disconnect first, then a new socket,
#    subscriptions re-sent on the NEW socket only
# --------------------------------------------------------------------------- #


async def test_force_reconnect_runs_on_disconnect_before_reconnecting_and_resubscribes() -> None:
    sock1, sock2 = _ThreadSocket(), _ThreadSocket()
    connector = _Connector([sock1, sock2])
    m, metrics = _manager(connector)
    events: list[tuple[str, int]] = []

    async def on_disc() -> None:
        events.append(("disconnect_handler", time.monotonic_ns()))

    async def on_conn() -> None:
        events.append(("connect_handler", time.monotonic_ns()))

    m.on_disconnect(on_disc)
    m.on_connect(on_conn)
    m.add_subscription(["communications"])
    m.start()
    try:
        await _until(lambda: len(sock1.sent) == 1)
        assert json.loads(sock1.sent[0])["cmd"] == "subscribe"

        await m.force_reconnect()  # the 10/17 path (intake's on_channel_lost)
        assert sock1.close_calls == 1
        await _until(lambda: len(sock2.sent) == 1)  # resubscribed on the NEW socket
        assert json.loads(sock2.sent[0])["params"]["channels"] == ["communications"]
        assert len(sock1.sent) == 1  # nothing more was ever sent on the dead one
        assert connector.connects == 2
        # on_disconnect COMPLETED before the second connect was attempted.
        disc = next(t for kind, t in events if kind == "disconnect_handler")
        assert disc <= connector.connected_at[1]
        assert [k for k, _ in events] == [
            "connect_handler",
            "disconnect_handler",
            "connect_handler",
        ]
        await _until(lambda: metrics.counter("t.disconnect") == 1)
        assert m._force_reconnecting is False  # guard cleared by the fresh socket
        assert m.connected
    finally:
        await m.stop()


async def test_send_command_is_marshalled_to_the_socket_loop() -> None:
    sock = _ThreadSocket()
    m, _ = _manager(_Connector([sock]))
    m.start()
    try:
        await _until(lambda: m.connected)
        cmd_id = await m.send_command("subscribe", {"channels": ["x"]})
        assert json.loads(sock.sent[-1]) == {
            "id": cmd_id,
            "cmd": "subscribe",
            "params": {"channels": ["x"]},
        }
        # A live add_subscription while connected also lands on the socket.
        m.add_subscription(["orderbook_delta"], market_tickers=["M1"])
        await _until(lambda: len(sock.sent) == 2)
        assert json.loads(sock.sent[-1])["params"] == {
            "channels": ["orderbook_delta"],
            "market_tickers": ["M1"],
        }
    finally:
        await m.stop()


# --------------------------------------------------------------------------- #
# 5. Code 25 recurrence is CRITICAL and carries the reader's state
# --------------------------------------------------------------------------- #


async def test_code_25_is_logged_critical_with_reader_diagnostics() -> None:
    sock = _ThreadSocket()
    m, metrics = _manager(_Connector([sock]))
    m.start()
    try:
        await _until(lambda: m.connected)
        with structlog.testing.capture_logs() as logs:
            await m._dispatch(
                {
                    "type": "error",
                    "msg": {
                        "code": SUBSCRIPTION_BUFFER_OVERFLOW_CODE,
                        "msg": "Subscription buffer overflow",
                    },
                }
            )
        crit = [e for e in logs if e["event"] == "ws_subscription_buffer_overflow"]
        assert len(crit) == 1
        assert crit[0]["log_level"] == "critical"
        assert crit[0]["reader_thread_alive"] is True
        assert "depths" in crit[0] and "frames_read" in crit[0]
        assert metrics.counter("t.subscription_buffer_overflow") == 1
        # Other error codes stay a plain warning.
        with structlog.testing.capture_logs() as logs:
            await m._dispatch({"type": "error", "msg": {"code": 6, "msg": "already subscribed"}})
        assert not [e for e in logs if e["event"] == "ws_subscription_buffer_overflow"]
    finally:
        await m.stop()


# --------------------------------------------------------------------------- #
# 6. Clean stop
# --------------------------------------------------------------------------- #


async def test_stop_joins_the_reader_thread_and_closes_the_socket() -> None:
    sock = _ThreadSocket()
    m, _ = _manager(_Connector([sock]))
    m.start()
    await _until(lambda: m.connected)
    reader = m._reader
    assert reader is not None and reader.is_alive()
    await m.stop()
    assert sock.closed
    assert not reader.is_alive()
    assert reader.loop is None
    assert m._reader is None and m._dispatch_task is None
    assert not m.connected
    assert not [t for t in threading.enumerate() if t.name == "t-reader"]


async def test_start_twice_is_refused() -> None:
    sock = _ThreadSocket()
    m, _ = _manager(_Connector([sock]))
    m.start()
    try:
        try:
            m.start()
        except RuntimeError as exc:
            assert "already started" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("second start() must be refused")
    finally:
        await m.stop()
