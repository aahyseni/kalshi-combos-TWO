"""COMMUNICATIONS FAN-OUT SHARDING (2026-09-05, the fills lever) — unit proofs.

After reader isolation the exchange-side overflow drops went to zero and BOTH
accepts on the 17:59 ET boot still expired (accept_to_confirm_ms 547/666 of a
3,000 ms window): the accept frame was late BEFORE it reached the process.
Pipe lag (rfq created_ts → our socket) p50 6.4 s at ~1,200 frames/s on the ONE
unsharded communications subscription. The repair is Kalshi's documented
fan-out sharding (asyncapi-ws.md §3.2): N connections, one per shard_key, all
feeding the ONE dispatcher; N derived from measurement.

Proofs here, each against a fake SHARDED exchange (frames for key k arrive
only on the socket that subscribed with shard_key k) driving the REAL
``CommsFanout`` → follower ``WsManager`` reader threads → shared lanes →
dispatcher (rule 8: drive the live module, never reimplement it):

  1. Every frame arrives EXACTLY ONCE across N connections; priority frames
     (accepts) dispatch before the market backlog, whichever socket carried
     them; the wire stamps and shard stamps ride the envelope.
  2. N = 1 puts exactly today's bytes on the wire (no shard params).
  3. A terminal error (25) on ONE shard reconnects THAT shard only and is NOT
     forwarded (no cancel_all); every shard lost at once IS forwarded (the
     whole-channel rule); the consumer's on_disconnect fires once per epoch.
  4. Sharding validation errors 19-22 fall back LOUDLY to the unsharded
     single subscription — never to no subscription.
  5. Derivation: rate → N; floor 1; cap 100; bootstrap on an empty tape;
     override and refusal are logged sources; anchors pinned to the z ladder
     and the stall wall's margin.
  6. Pipe-lag histogram, windows, snapshot exclusion, and the
     ``pipe_lag_exceeds_confirm_window`` alarm; the governor applies GROWTH
     live and defers a shrink to the next boot; the tape pools across boots.
  7. A follower's death purges only ITS market frames from the shared lanes.
"""

from __future__ import annotations

import asyncio
import collections
import json
import math
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import structlog.testing

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.exchange.ws import (
    SHARD_FACTOR_MAX,
    SHARD_VALIDATION_ERROR_CODES,
    Lane,
    WsManager,
    _Lanes,
)
from combomaker.exchange.ws_fanout import (
    FANOUT_HEADROOM,
    FANOUT_Z,
    CommsFanout,
    FanoutGovernor,
    FanoutTape,
    LagHistogram,
    RateHistogram,
    ShardMeter,
    derive_shard_factor,
    fanout_tape_path,
    refresh_fanout_tape,
)
from combomaker.ops.metrics import Metrics
from combomaker.risk.confirm_expired_rate import EXPIRED_RATE_ALARM_Z
from combomaker.risk.stall_wall import STALL_WALL_MARGIN

JsonDict = dict[str, Any]
WINDOW_S = 3.0  # the exchange confirm window — a protocol fact the tests pass in


# --------------------------------------------------------------------------- #
# The fake sharded exchange
# --------------------------------------------------------------------------- #


class _Frame:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload)


class _ThreadSocket:
    """Lives on a follower's reader loop; fed from any thread."""

    def __init__(self, exchange: _FakeExchange) -> None:
        self._exchange = exchange
        self._frames: collections.deque[_Frame] = collections.deque()
        self._lock = threading.Lock()
        self.closed = False
        self.close_calls = 0
        self.sent: list[str] = []
        self.key: int | None = None
        self.acked = False

    def feed(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._frames.append(_Frame(payload))

    def __aiter__(self) -> _ThreadSocket:
        return self

    async def __anext__(self) -> _Frame:
        while True:
            if self.closed:
                raise StopAsyncIteration
            with self._lock:
                frame = self._frames.popleft() if self._frames else None
            if frame is not None:
                await asyncio.sleep(0)  # yield once per frame: marshalled sends/closes run
                return frame
            await asyncio.sleep(0.001)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    async def send_str(self, data: str) -> None:
        self.sent.append(data)
        self._exchange.on_command(self, json.loads(data))


class _Ctx:
    def __init__(self, sock: _ThreadSocket) -> None:
        self._sock = sock

    async def __aenter__(self) -> _ThreadSocket:
        return self._sock

    async def __aexit__(self, *exc: object) -> bool:
        self._sock.closed = True
        return False


def _route_key(rfq_id: str, factor: int) -> int:
    return sum(ord(c) for c in rfq_id) % factor


class _FakeExchange:
    """Hands out one socket per connect; validates subscribe params exactly as
    the documented error table does (19-22); routes frames for key k only to
    the socket that subscribed with shard_key k; N = 1 routes to the unsharded
    socket. ``hold_acks`` parks subscribe acks so a test can hold every shard
    in the lost state at once."""

    def __init__(self, *, refuse: dict[int, int] | None = None) -> None:
        self.sockets: list[_ThreadSocket] = []
        self.by_key: dict[int | None, _ThreadSocket] = {}
        self.subscribes: list[dict[str, Any]] = []
        self.connects = 0
        self.factor = 1
        self._sid = 0
        self._refuse = refuse or {}
        self.hold_acks = False
        self.hold_connects = False  # refuse connects (the reader backs off and retries)
        self._held: list[tuple[_ThreadSocket, dict[str, Any]]] = []

    def connector(self, session: Any, url: str, headers: dict[str, str]) -> _Ctx:
        if self.hold_connects:
            raise ConnectionError("held")
        self.connects += 1
        sock = _ThreadSocket(self)
        self.sockets.append(sock)
        return _Ctx(sock)

    def on_command(self, sock: _ThreadSocket, cmd: dict[str, Any]) -> None:
        if cmd.get("cmd") != "subscribe":
            return
        params = cmd["params"]
        self.subscribes.append(dict(params))
        key = params.get("shard_key")
        factor = params.get("shard_factor")
        code = 0
        if factor is not None and factor <= 0:
            code = 19
        elif key is not None and factor is None:
            code = 20
        elif factor is not None and factor > SHARD_FACTOR_MAX:
            code = 22
        elif key is not None and not (0 <= key < factor):
            code = 21
        elif key in self._refuse:
            code = self._refuse[key]
        if code:
            sock.feed({"id": cmd["id"], "type": "error", "msg": {"code": code, "msg": f"e{code}"}})
            return
        if self.hold_acks:
            self._held.append((sock, cmd))
            return
        self._ack(sock, cmd)

    def release_acks(self) -> None:
        held, self._held = self._held, []
        for sock, cmd in held:
            self._ack(sock, cmd)

    def _ack(self, sock: _ThreadSocket, cmd: dict[str, Any]) -> None:
        params = cmd["params"]
        key = params.get("shard_key")
        factor = params.get("shard_factor")
        self._sid += 1
        sock.key = key
        sock.acked = True
        self.factor = int(factor) if factor is not None else 1
        self.by_key[key] = sock
        sock.feed(
            {
                "id": cmd["id"],
                "type": "subscribed",
                "msg": {"channel": "communications", "sid": self._sid},
            }
        )

    def socket_for(self, rfq_id: str) -> _ThreadSocket:
        key = _route_key(rfq_id, self.factor) if self.factor > 1 else None
        return self.by_key[key]

    def route(self, payload: dict[str, Any], rfq_id: str) -> None:
        self.socket_for(rfq_id).feed(payload)

    def live_keys(self) -> set[int | None]:
        return {k for k, s in self.by_key.items() if not s.closed}


class _Signer:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {}


def _fanout(exchange: _FakeExchange, metrics: Metrics | None = None) -> tuple[CommsFanout, Metrics]:
    metrics = metrics or Metrics()
    f = CommsFanout(
        "wss://example/ws",
        _Signer(),  # type: ignore[arg-type]
        SystemClock(),
        metrics,
        name="t",
        backoff_initial_s=0.01,
        backoff_max_s=0.02,
        connect=exchange.connector,
    )
    f.mark_priority("quote_accepted", "quote_executed")
    f.mark_sheddable("rfq_created", stale_after_s=5.0)
    f.mark_sheddable("rfq_deleted")
    return f, metrics


async def _until(pred: Any, within_s: float = 3.0) -> None:
    async def _wait() -> None:
        while not pred():  # noqa: ASYNC110 — bounded by wait_for
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait(), timeout=within_s)


async def _settled(f: CommsFanout, within_s: float = 3.0) -> None:
    await _until(lambda: not any(f.lane_depths().values()) and not f._lanes.wake_pending, within_s)


def _rfq_created(rfq_id: str, *, age_s: float = 0.0) -> dict[str, Any]:
    created = datetime.now(UTC) - timedelta(seconds=age_s)
    return {
        "type": "rfq_created",
        "sid": 1,
        "msg": {"id": rfq_id, "market_ticker": "KXT", "created_ts": created.isoformat()},
    }


def _accept(rfq_id: str, quote_id: str) -> dict[str, Any]:
    return {"type": "quote_accepted", "sid": 1, "msg": {"quote_id": quote_id, "rfq_id": rfq_id}}


async def _start_sharded(exchange: _FakeExchange, n: int) -> tuple[CommsFanout, Metrics, list[str]]:
    f, metrics = _fanout(exchange)
    seen: list[str] = []

    async def rec(msg: JsonDict) -> None:
        seen.append(
            f"{msg['type']}:{msg['msg'].get('id') or msg['msg'].get('quote_id')}:{msg['_shard']}"
        )

    f.on_message("rfq_created", rec)
    f.on_message("quote_accepted", rec)
    f.add_subscription(["communications"])
    await f.apply_shard_factor(n, reason="test")
    f.start()
    await _until(lambda: exchange.live_keys() == (set(range(n)) if n > 1 else {None}))
    await _until(lambda: metrics.counter("t.msg.subscribed") >= n and not f._lost)
    return f, metrics, seen


# --------------------------------------------------------------------------- #
# 1-2. Exactly-once across shards, priority first, wire bytes
# --------------------------------------------------------------------------- #


async def test_every_frame_arrives_exactly_once_across_shards_and_accepts_dispatch_first() -> None:
    exchange = _FakeExchange()
    f, metrics, seen = await _start_sharded(exchange, 3)
    try:
        # Each shard subscribed with ITS key and the common factor.
        params = sorted(
            (p for p in exchange.subscribes if "shard_key" in p), key=lambda p: p["shard_key"]
        )
        assert params == [
            {"channels": ["communications"], "shard_factor": 3, "shard_key": k} for k in range(3)
        ]
        await asyncio.sleep(0.05)  # acks dispatched; lanes idle

        ids = [f"r{i}" for i in range(300)]
        accepts = [("r5", "q5"), ("r150", "q150"), ("r299", "q299")]

        def feeder() -> None:
            time.sleep(0.02)  # the main loop is in its stall by now
            for i, rid in enumerate(ids):
                exchange.route(_rfq_created(rid), rid)
                if i in (5, 150, 299):
                    a = next(a for a in accepts if a[0] == rid)
                    exchange.route(_accept(*a), rid)

        t = threading.Thread(target=feeder)
        t.start()
        time.sleep(0.4)  # noqa: ASYNC251 — THE STALL: nothing on the main loop runs
        t.join()
        await _until(lambda: sum(s._frames_read for s in f._shards) >= 303 + 3)  # + 3 acks
        assert f.lane_depths()["priority"] == 3  # all three accepts queued, none dispatched
        await _settled(f)

        # Exactly once, every frame, whichever socket carried it.
        rfq_seen = [s for s in seen if s.startswith("rfq_created")]
        acc_seen = [s for s in seen if s.startswith("quote_accepted")]
        assert sorted(s.split(":")[1] for s in rfq_seen) == sorted(ids)
        assert len(rfq_seen) == 300
        assert sorted(s.split(":")[1] for s in acc_seen) == ["q150", "q299", "q5"]
        # Every shard carried some of the flow; each frame's stamp names ITS socket.
        shards_used = {int(s.split(":")[2]) for s in seen}
        assert shards_used == {0, 1, 2}
        for s in rfq_seen:
            _, rid, shard = s.split(":")
            assert int(shard) == _route_key(rid, 3)
        # PRIORITY FIRST: the three accepts were dispatched before any market frame.
        assert [s.split(":")[0] for s in seen[:3]] == ["quote_accepted"] * 3
        assert metrics.counter("t.shed_market_frames") == 0
        assert metrics.counter("t.dispatch_queue_overflow") == 0
        assert exchange.connects == 3
        # Per-shard measurement saw the frames; the first window is a snapshot window.
        windows = f.take_windows(time.monotonic_ns())
        assert sum(w.frames for w in windows) >= 306
        assert all(w.snapshot for w in windows)
        assert all(w.lag_rfq.n > 0 for w in windows)
        assert f.connected and f.shard_count == 3
    finally:
        await f.stop()
    assert all(s.closed for s in exchange.sockets)
    assert not [t for t in threading.enumerate() if t.name.startswith("t.s")]


async def test_unsharded_factor_one_puts_exactly_todays_bytes_on_the_wire() -> None:
    exchange = _FakeExchange()
    f, _, seen = await _start_sharded(exchange, 1)
    try:
        assert exchange.subscribes == [{"channels": ["communications"]}]
        assert f.shard_factor == 1 and f.shard_count == 1
        exchange.route(_rfq_created("r1"), "r1")
        await _until(lambda: len(seen) == 1)
        assert seen == ["rfq_created:r1:0"]
    finally:
        await f.stop()


# --------------------------------------------------------------------------- #
# 3. The recovery rule
# --------------------------------------------------------------------------- #


async def test_terminal_error_on_one_shard_reconnects_only_that_shard_and_is_not_forwarded() -> (
    None
):
    exchange = _FakeExchange()
    f, metrics, _ = await _start_sharded(exchange, 3)
    forwarded: list[int] = []
    disconnects: list[int] = []

    async def on_error(msg: JsonDict) -> None:
        # The intake's terminal-code handler → the app's on_channel_lost:
        # cancel_all (recorded here as ``forwarded``) + force_reconnect(all).
        forwarded.append(int(msg["msg"]["code"]))
        await f.force_reconnect()

    async def on_disc() -> None:
        disconnects.append(1)

    f.on_message("error", on_error)
    f.on_disconnect(on_disc)
    try:
        await asyncio.sleep(0.05)
        s0, s1, s2 = (exchange.by_key[k] for k in range(3))
        with structlog.testing.capture_logs() as logs:
            s1.feed(
                {
                    "type": "error",
                    "sid": 2,
                    "msg": {"code": 25, "msg": "Subscription buffer overflow"},
                }
            )
            await _until(lambda: exchange.connects == 4)
            await _until(lambda: exchange.by_key[1] is not s1 and exchange.by_key[1].acked)
            await asyncio.sleep(0.05)
        assert forwarded == []  # NOT forwarded: no cancel_all for one socket
        assert s1.close_calls == 1 and s1.closed
        assert not s0.closed and not s2.closed and s0.close_calls == 0 and s2.close_calls == 0
        assert exchange.subscribes[-1] == {
            "channels": ["communications"],
            "shard_factor": 3,
            "shard_key": 1,
        }
        assert metrics.counter("t.shard_channel_lost.25") == 1
        assert (
            metrics.counter("t.s1.subscription_buffer_overflow") == 1
        )  # code 25 stays CRITICAL, per shard
        lost = [e for e in logs if e["event"] == "ws_shard_channel_lost"]
        assert len(lost) == 1 and lost[0]["shard"] == 1 and lost[0]["log_level"] == "warning"
        assert not [e for e in logs if e["event"] == "ws_fanout_channel_lost_all"]
        crit = [e for e in logs if e["event"] == "ws_subscription_buffer_overflow"]
        assert len(crit) == 1 and crit[0]["name"] == "t.s1"
        # The consumer's on_disconnect fired exactly once for the one loss.
        assert disconnects == [1]
        assert 1 not in f._lost  # re-ack cleared the lost state

        # WHOLE-CHANNEL: every shard lost at once → forwarded → cancel_all path
        # (the app's handler above also force_reconnects every shard). Connects
        # are held so all three disconnects land in ONE loss epoch.
        exchange.hold_acks = True
        exchange.hold_connects = True
        live_before = {k: exchange.by_key[k] for k in range(3)}
        for k in range(3):
            live_before[k].feed(
                {"type": "error", "sid": 9, "msg": {"code": 17, "msg": "Internal error"}}
            )
        await _until(lambda: forwarded == [17])  # forwarded EXACTLY once, on the third loss
        assert metrics.counter("t.shard_channel_lost.17") == 3
        await _until(lambda: all(sock.closed for sock in live_before.values()))
        await asyncio.sleep(0.05)  # every disconnect follow-up has run; no reconnect possible yet
        # Three simultaneous disconnects: on_disconnect coalesced to ONE more fire.
        assert disconnects == [1, 1]
        exchange.hold_connects = False
        await _until(lambda: exchange.connects == 7)
        await _until(lambda: len(exchange._held) == 3)  # every resubscribe parked
        exchange.release_acks()
        await _until(
            lambda: (
                len(exchange.live_keys()) == 3 and all(exchange.by_key[k].acked for k in range(3))
            )
        )
        await _until(lambda: not f._lost)
        assert forwarded == [17]
    finally:
        await f.stop()


async def test_factor_one_terminal_error_is_forwarded_exactly_as_today() -> None:
    exchange = _FakeExchange()
    f, _, _ = await _start_sharded(exchange, 1)
    forwarded: list[int] = []

    async def on_error(msg: JsonDict) -> None:
        forwarded.append(int(msg["msg"]["code"]))
        await f.force_reconnect()  # the app's on_channel_lost, as today

    f.on_message("error", on_error)
    try:
        await asyncio.sleep(0.05)
        exchange.by_key[None].feed({"type": "error", "msg": {"code": 25, "msg": "overflow"}})
        await _until(lambda: forwarded == [25])
        await _until(lambda: exchange.connects == 2)  # the app's reconnect
        await _until(lambda: exchange.by_key[None].acked and exchange.connects == 2)
        assert exchange.subscribes[-1] == {"channels": ["communications"]}
    finally:
        await f.stop()


async def test_ordinary_socket_death_reconnects_that_shard_and_purges_only_its_market_frames() -> (
    None
):
    exchange = _FakeExchange()
    f, metrics, seen = await _start_sharded(exchange, 2)
    release = asyncio.Event()
    first_started = asyncio.Event()

    async def slow(msg: JsonDict) -> None:  # holds the dispatcher on the FIRST market frame
        first_started.set()
        await release.wait()

    f.on_message("rfq_created", slow)
    try:
        await asyncio.sleep(0.05)
        s0, s1 = exchange.by_key[0], exchange.by_key[1]
        ids0 = [rid for rid in (f"a{i}" for i in range(400)) if _route_key(rid, 2) == 0][:20]
        ids1 = [rid for rid in (f"b{i}" for i in range(400)) if _route_key(rid, 2) == 1][:20]
        # One shard-1 frame first (it becomes the in-flight frame the slow
        # handler holds), then 20 of shard 0's and the rest of shard 1's.
        s1.feed(_rfq_created(ids1[0]))
        await first_started.wait()
        for rid in ids0:
            s0.feed(_rfq_created(rid))
        for rid in ids1[1:]:
            s1.feed(_rfq_created(rid))
        await _until(lambda: f.lane_depths()["market"] == 39)
        # Shard 0's socket dies under its reader (EOF): the disconnect
        # follow-up purges ITS 20 frames from the shared lane and nothing else.
        s0.closed = True
        await _until(lambda: metrics.counter("t.s0.queue_discarded") == 20)
        assert f.lane_depths()["market"] == 19
        release.set()
        await _settled(f)
        assert sorted(s.split(":")[1] for s in seen) == sorted(ids1)
        assert not [s for s in seen if s.split(":")[1] in ids0]
        await _until(lambda: exchange.connects == 3 and exchange.by_key[0].acked)
        assert s1.close_calls == 0 and not s1.closed
        assert metrics.counter("t.s1.queue_discarded") == 0
    finally:
        release.set()
        await f.stop()


def test_purge_market_keeps_priority_control_and_other_shards() -> None:
    lanes = _Lanes(100)
    for k in (0, 1, 0, 1, 0):
        lanes.push({"type": "rfq_created", "_shard": k}, Lane.MARKET)
    lanes.push({"type": "quote_accepted", "_shard": 0}, Lane.PRIORITY)
    lanes.push({"type": "subscribed", "_shard": 0}, Lane.CONTROL)
    lanes.push({"type": "rfq_created"}, Lane.MARKET)  # unsharded stamp: kept
    assert lanes.purge_market(0) == 3
    assert lanes.depths() == {"priority": 1, "control": 1, "market": 3}
    assert [m.get("_shard") for m in lanes.market] == [1, 1, None]


# --------------------------------------------------------------------------- #
# 4. Sharding refused → unsharded, loudly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("code", sorted(SHARD_VALIDATION_ERROR_CODES))
async def test_sharding_validation_error_falls_back_to_unsharded_loudly(code: int) -> None:
    exchange = _FakeExchange(refuse={2: code})
    f, metrics = _fanout(exchange)
    seen: list[str] = []

    async def rec(msg: JsonDict) -> None:
        seen.append(str(msg["msg"]["id"]))

    f.on_message("rfq_created", rec)
    f.add_subscription(["communications"])
    await f.apply_shard_factor(3, reason="test")
    with structlog.testing.capture_logs() as logs:
        f.start()
        try:
            await _until(
                lambda: (
                    f.sharding_refused and f.shard_factor == 1 and exchange.live_keys() == {None}
                )
            )
            await _until(lambda: exchange.by_key[None].acked)
            refused = [e for e in logs if e["event"] == "ws_fanout_sharding_refused"]
            assert len(refused) == 1
            assert refused[0]["log_level"] == "warning"
            assert refused[0]["code"] == code and refused[0]["shard"] == 2
            assert metrics.counter("t.sharding_refused") == 1
            # Fallback = today's subscribe, and it still delivers.
            assert exchange.subscribes[-1] == {"channels": ["communications"]}
            exchange.route(_rfq_created("r1"), "r1")
            await _until(lambda: seen == ["r1"])
            # Sticky for the boot: a later apply cannot re-shard.
            assert await f.apply_shard_factor(5, reason="test") == 1
            d = derive_shard_factor(
                None, None, n_windows=0, n_violating=0, boots_pooled=0, refused=True
            )
            assert d.n == 1 and d.source == "fallback_unsharded"
        finally:
            await f.stop()


# --------------------------------------------------------------------------- #
# 5. Derivation
# --------------------------------------------------------------------------- #


def _hist(values: list[float]) -> RateHistogram:
    h = RateHistogram()
    for v in values:
        h.observe(v)
    return h


def test_derive_shard_factor_from_todays_measured_rates() -> None:
    # 2026-09-05 17:59 boot: ~1,200-1,340 fps inbound on ONE connection whose
    # pipe lag violated the window every minute → capacity = its read rate.
    inbound = _hist([1200.0, 1300.0, 1340.0])
    capacity = _hist([1150.0, 1200.0, 1100.0])
    d = derive_shard_factor(inbound, capacity, n_windows=3, n_violating=3, boots_pooled=1)
    assert d.source == "measured"
    assert d.margin == FANOUT_HEADROOM == 2.0 and d.z == FANOUT_Z == 3.0
    # Small sample: Q_hi = the max bucket's upper edge (≥ 1340), Q_lo = the min (1100).
    assert d.inbound_fps >= 1340.0 and d.inbound_fps <= 1340.0 * RateHistogram.RATIO
    assert d.capacity_fps == 1100.0
    assert d.n == math.ceil(d.inbound_fps * 2.0 / 1100.0) == 3
    assert d.n_measured == 3
    log = d.as_log()
    assert log["shard_factor_derived"] == 3 and log["source"] == "measured"
    assert log["cap"] == SHARD_FACTOR_MAX == 100 and log["floor"] == 1


def test_derive_shard_factor_floor_cap_bootstrap_override() -> None:
    quiet = derive_shard_factor(
        _hist([10.0]), _hist([5000.0]), n_windows=1, n_violating=0, boots_pooled=1
    )
    assert quiet.n == 1 and quiet.source == "measured"
    flood = derive_shard_factor(
        _hist([1e6]), _hist([10.0]), n_windows=1, n_violating=1, boots_pooled=1
    )
    assert flood.n == 100 and flood.source == "measured"
    empty = derive_shard_factor(
        RateHistogram(), RateHistogram(), n_windows=0, n_violating=0, boots_pooled=0
    )
    assert empty.n == 1 and empty.source == "bootstrap" and empty.n_measured is None
    none = derive_shard_factor(None, None, n_windows=0, n_violating=0, boots_pooled=0)
    assert none.n == 1 and none.source == "bootstrap"
    over = derive_shard_factor(
        _hist([1200.0]), _hist([1100.0]), n_windows=1, n_violating=1, boots_pooled=1, override=7
    )
    assert over.n == 7 and over.source == "override" and over.override == 7 and over.n_measured == 3
    over_cap = derive_shard_factor(
        None, None, n_windows=0, n_violating=0, boots_pooled=0, override=500
    )
    assert over_cap.n == 100
    with pytest.raises(ValueError):
        derive_shard_factor(None, None, n_windows=0, n_violating=0, boots_pooled=0, margin=0.5)


def test_anchors_are_the_existing_policy_constants() -> None:
    """No new numbers: the headroom IS the stall wall's / hang watchdog's
    margin; the quantile rung IS the z ladder's daily rung (the same rung the
    expired-accept-rate alarm judges at)."""
    assert FANOUT_HEADROOM == STALL_WALL_MARGIN
    assert FANOUT_Z == EXPIRED_RATE_ALARM_Z == 3.0
    assert RateHistogram.RATIO == 1.0 + 1.0 / SHARD_FACTOR_MAX


def test_rate_histogram_quantiles_and_json_roundtrip() -> None:
    h = _hist([100.0, 200.0, 400.0, 800.0])
    assert h.n == 4 and h.max_fps == 800.0 and h.min_fps == 100.0
    assert h.quantile_upper(1.0) == 800.0  # clipped to the max
    assert h.quantile_lower(1e-9) == 100.0  # clipped to the min
    q = h.quantile_upper(0.5)
    assert 200.0 <= q <= 200.0 * RateHistogram.RATIO
    h.observe(0.0)  # not a rate
    h.observe(-1.0)
    h.observe(math.inf)
    assert h.n == 4
    back = RateHistogram.from_json(json.loads(json.dumps(h.to_json())))
    assert back is not None and back.counts == h.counts and back.n == 4
    assert back.max_fps == 800.0 and back.min_fps == 100.0
    assert RateHistogram.from_json({"counts": {"1": -1}}) is None
    assert RateHistogram.from_json("junk") is None
    m = _hist([50.0])
    m.merge(h)
    assert m.n == 5 and m.min_fps == 50.0 and m.max_fps == 800.0
    with pytest.raises(ValueError):
        h.quantile_upper(0.0)


def test_fanout_tape_folds_prunes_and_pools(tmp_path: Path) -> None:
    path = fanout_tape_path(tmp_path)
    pooled = refresh_fanout_tape(
        tape_path=path,
        data_dir=tmp_path,
        boot_key="old",
        boot_started_at_ts=1000.0,
        boot_inbound=_hist([1200.0]),
        boot_capacity=_hist([1100.0]),
        boot_n_windows=1,
        boot_n_snapshot=1,
        boot_n_violating=1,
        boot_shard_factors=[1],
    )
    assert pooled.boots == 1 and pooled.n_windows == 1
    pooled = refresh_fanout_tape(
        tape_path=path,
        data_dir=tmp_path,
        boot_key="new",
        boot_started_at_ts=2000.0,
        boot_inbound=_hist([600.0]),
        boot_capacity=_hist([3000.0]),
        boot_n_windows=1,
        boot_n_snapshot=0,
        boot_n_violating=0,
        boot_shard_factors=[3],
    )
    assert pooled.boots == 2 and pooled.n_windows == 2 and pooled.n_violating == 1
    assert pooled.inbound.max_fps == 1200.0 and pooled.capacity.min_fps == 1100.0
    # Retention = the oldest live_*.log on disk: a log newer than the old boot prunes it.
    (tmp_path / "live_x.log").write_text("x")
    import os

    os.utime(tmp_path / "live_x.log", (1500.0, 1500.0))
    pooled = refresh_fanout_tape(
        tape_path=path,
        data_dir=tmp_path,
        boot_key="new",
        boot_started_at_ts=2000.0,
        boot_inbound=_hist([600.0]),
        boot_capacity=_hist([3000.0]),
        boot_n_windows=1,
        boot_n_snapshot=0,
        boot_n_violating=0,
        boot_shard_factors=[3],
    )
    assert pooled.boots == 1 and pooled.inbound.max_fps == 600.0
    tape = FanoutTape(path)
    tape.load()
    assert set(tape.boots) == {"new"} and tape.boots["new"]["shard_factors"] == [3]
    # Corrupt file → no tape, never a raise.
    path.write_text("{not json")
    tape.load()
    assert tape.boots == {}


# --------------------------------------------------------------------------- #
# 6. Pipe lag, windows, the alarm, the governor
# --------------------------------------------------------------------------- #


def test_shard_meter_measures_pipe_lag_utilisation_and_snapshot_windows() -> None:
    clock = FakeClock(datetime(2026, 9, 5, 22, 0, tzinfo=UTC))
    meter = ShardMeter(0, clock)
    t0 = clock.monotonic_ns()

    def frame(kind: str, age_s: float) -> JsonDict:
        created = (clock.now() - timedelta(seconds=age_s)).isoformat()
        return {"type": kind, "msg": {"id": "x", "created_ts": created}}

    for _ in range(10):
        meter.observe(frame("rfq_created", 6.0), t0, 1_000_000)  # 1 ms handling each
    meter.observe(frame("quote_created", 1.0), t0, 1_000_000)
    meter.observe({"type": "rfq_deleted", "msg": {"id": "x"}}, t0, 1_000_000)  # rate only
    meter.observe({"type": "rfq_created", "msg": {"id": "y", "created_ts": "garbage"}}, t0, 0)
    clock.advance(1.0)
    w = meter.take(clock.monotonic_ns(), shed_lost_total=4)
    assert w.frames == 13 and w.elapsed_s == pytest.approx(1.0)
    assert w.fps == pytest.approx(13.0)
    assert w.utilization == pytest.approx(0.012)  # 12 ms of 1 s
    assert w.lag_rfq.n == 10 and w.lag_rfq.quantile(0.5) == pytest.approx(6000.0, abs=1.0)
    assert w.lag_quote.n == 1 and w.lag_quote.quantile(0.5) == pytest.approx(1000.0, abs=1.0)
    assert w.violating(WINDOW_S * 1e3)
    assert w.capacity_fps(WINDOW_S * 1e3) == pytest.approx(13.0)  # violating: the read rate
    assert w.shed_lost == 4 and not w.snapshot
    s = w.lag_rfq.summary(WINDOW_S * 1e3)
    assert s is not None and s.over_window == 10 and s.share_over_window == 1.0
    # Healthy window: capacity extrapolates by utilisation.
    for _ in range(10):
        meter.observe(frame("rfq_created", 0.2), t0, 2_000_000)
    clock.advance(1.0)
    w2 = meter.take(clock.monotonic_ns(), shed_lost_total=4)
    assert not w2.violating(WINDOW_S * 1e3)
    assert w2.utilization == pytest.approx(0.02)
    assert w2.capacity_fps(WINDOW_S * 1e3) == pytest.approx(10.0 / 0.02)
    assert w2.shed_lost == 0
    # Snapshot: a subscribe ack marks this window AND the next; the third is clean.
    meter.mark_subscribed()
    clock.advance(1.0)
    assert meter.take(clock.monotonic_ns(), 4).snapshot is True
    clock.advance(1.0)
    assert meter.take(clock.monotonic_ns(), 4).snapshot is True
    clock.advance(1.0)
    assert meter.take(clock.monotonic_ns(), 4).snapshot is False
    # No frames → no capacity evidence.
    assert meter.take(clock.monotonic_ns(), 4).capacity_fps(3000.0) is None


def test_lag_histogram_quantiles_and_merge() -> None:
    h = LagHistogram()
    for ms in (100.0, 200.0, 7000.0):
        h.observe(ms)
    h.observe(math.nan)
    assert h.n == 3 and h.quantile(0.5) == 200.0 and h.max_ms == 7000.0
    other = LagHistogram()
    other.observe(50.0)
    h.merge(other)
    assert h.n == 4 and h.quantile(0.01) == 50.0
    s = h.summary(3000.0)
    assert s is not None and s.over_window == 1 and s.n == 4
    assert LagHistogram().summary(3000.0) is None


async def test_governor_alarms_on_pipe_lag_grows_live_then_defers_shrink(tmp_path: Path) -> None:
    exchange = _FakeExchange()
    f, metrics, seen = await _start_sharded(exchange, 1)
    disconnects: list[int] = []

    async def on_disc() -> None:
        disconnects.append(1)

    f.on_disconnect(on_disc)
    gov = FanoutGovernor(
        f,
        SystemClock(),
        metrics,
        tape_path=fanout_tape_path(tmp_path),
        data_dir=tmp_path,
        boot_key="boot-1",
        boot_started_at_ts=1.0,
        confirm_window_s=WINDOW_S,
    )
    try:
        await asyncio.sleep(0.05)

        async def flood(n: int, age_s: float) -> None:
            # Wait against a per-call baseline: ``_frames_read`` is cumulative.
            before = sum(s._frames_read for s in f._shards)
            for i in range(n):
                exchange.route(_rfq_created(f"f{i}", age_s=age_s), f"f{i}")
            await _until(lambda: sum(s._frames_read for s in f._shards) >= before + n)
            await _settled(f)

        # Windows 1-2 (the subscribe ack marks its own window AND the next as
        # SNAPSHOT — the re-dump can straddle the boundary): lagging frames,
        # alarm suppressed, no evidence, N stays 1.
        await flood(60, age_s=6.0)
        with structlog.testing.capture_logs() as logs:
            d1 = await gov.tick(reason="refresh")
        assert d1 is not None and d1.source == "bootstrap" and f.shard_factor == 1
        assert gov.n_snapshot == 1 and gov.n_windows == 0
        assert not [e for e in logs if e["event"] == "pipe_lag_exceeds_confirm_window"]
        rate_lines = [e for e in logs if e["event"] == "ws_inbound_rate"]
        assert len(rate_lines) == 1 and rate_lines[0]["snapshot_window"] is True
        lag_lines = [e for e in logs if e["event"] == "ws_pipe_lag"]
        assert len(lag_lines) == 1 and lag_lines[0]["rfq_created"]["n"] == 60
        assert lag_lines[0]["rfq_created"]["p50_ms"] > 3000.0
        assert lag_lines[0]["window_ms"] == 3000.0
        await flood(60, age_s=6.0)
        with structlog.testing.capture_logs() as logs:
            await gov.tick(reason="refresh")
        assert gov.n_snapshot == 2 and gov.n_windows == 0 and f.shard_factor == 1
        assert not [e for e in logs if e["event"] == "pipe_lag_exceeds_confirm_window"]

        # Window 3: still lagging → ALARM; violating unsharded connection ⇒
        # capacity = its read rate = inbound ⇒ N = ceil(1 × 2) = 2, applied LIVE.
        await flood(60, age_s=6.0)
        with structlog.testing.capture_logs() as logs:
            d2 = await gov.tick(reason="refresh")
        alarms = [e for e in logs if e["event"] == "pipe_lag_exceeds_confirm_window"]
        assert len(alarms) == 1 and alarms[0]["log_level"] == "warning"
        assert alarms[0]["p50_ms"] > 3000.0 and alarms[0]["share_over_window"] == 1.0
        assert metrics.counter("t.pipe_lag.exceeds_confirm_window") == 1
        assert d2 is not None and d2.source == "measured" and d2.n == 2
        assert gov.n_windows == 1 and gov.n_violating == 1
        deriv = [e for e in logs if e["event"] == "ws_fanout_derivation"]
        assert len(deriv) == 1
        assert deriv[0]["shard_factor_current"] == 1 and deriv[0]["shard_factor_applied"] == 2
        assert deriv[0]["deferred_shrink"] is False
        assert [e for e in logs if e["event"] == "ws_fanout_resharding"]
        assert f.shard_factor == 2 and f.shard_count == 2
        await _until(lambda: exchange.live_keys() == {0, 1})
        assert exchange.subscribes[-2:] == [
            {"channels": ["communications"], "shard_factor": 2, "shard_key": k} for k in range(2)
        ] or sorted(exchange.subscribes[-2:], key=lambda p: p["shard_key"]) == [
            {"channels": ["communications"], "shard_factor": 2, "shard_key": k} for k in range(2)
        ]
        # The re-shard fired the consumer's on_disconnect exactly once; the old socket is closed.
        assert disconnects == [1]
        assert exchange.sockets[0].closed
        assert (tmp_path / "ws_fanout_tape.json").exists()
        tape = FanoutTape(fanout_tape_path(tmp_path))
        tape.load()
        # The tick writes the tape BEFORE it derives and applies (tape -> derive
        # -> apply), so this tick's row still names the factor it ran at; the
        # applied factor lands on the next refresh (asserted below).
        assert tape.boots["boot-1"]["shard_factors"] == [1]
        assert tape.boots["boot-1"]["n_windows"] == 1 and tape.boots["boot-1"]["n_violating"] == 1

        # Windows 4-5 (the new shards' snapshot windows), 6: healthy, fast
        # frames on two connections → a SHRINK would be measured, but it is
        # deferred to the next boot.
        await asyncio.sleep(0.05)
        for _ in range(2):
            await flood(20, age_s=0.05)
            await gov.tick(reason="refresh")
        await flood(20, age_s=0.05)
        with structlog.testing.capture_logs() as logs:
            d3 = await gov.tick(reason="refresh")
        assert d3 is not None and d3.n >= 1
        deriv = [e for e in logs if e["event"] == "ws_fanout_derivation"]
        assert deriv[0]["shard_factor_applied"] == 2 and f.shard_factor == 2
        # Pooled evidence still holds the violating unsharded window (capacity
        # ≈ its rate), so the derived N cannot fall below the window's demand.
        assert deriv[0]["deferred_shrink"] == (d3.n < 2)
        assert not [e for e in logs if e["event"] == "pipe_lag_exceeds_confirm_window"]
        tape.load()
        assert tape.boots["boot-1"]["shard_factors"] == [1, 2]
        assert tape.boots["boot-1"]["n_windows"] == 2  # the violating window + one healthy
    finally:
        await f.stop()

    # NEXT BOOT: the tape alone derives N=2 before any socket opens (bootstrap
    # skipped: measured evidence exists), applied at start.
    exchange2 = _FakeExchange()
    f2, metrics2 = _fanout(exchange2)
    f2.add_subscription(["communications"])
    gov2 = FanoutGovernor(
        f2,
        SystemClock(),
        metrics2,
        tape_path=fanout_tape_path(tmp_path),
        data_dir=tmp_path,
        boot_key="boot-2",
        boot_started_at_ts=2.0,
        confirm_window_s=WINDOW_S,
    )
    with structlog.testing.capture_logs() as logs:
        d = await gov2.tick(reason="boot")
    assert d is not None and d.source == "measured" and d.boots_pooled == 2
    assert f2.shard_factor == d.n >= 2 and f2.shard_count == 0  # recorded, not yet built
    deriv = [e for e in logs if e["event"] == "ws_fanout_derivation"]
    assert deriv[0]["reason"] == "boot" and deriv[0]["shard_factor_applied"] == d.n
    f2.start()
    try:
        await _until(lambda: exchange2.live_keys() == set(range(d.n)))
        assert all(p.get("shard_factor") == d.n for p in exchange2.subscribes)
    finally:
        await f2.stop()


async def test_boot_tick_with_empty_tape_is_unsharded_bootstrap(tmp_path: Path) -> None:
    exchange = _FakeExchange()
    f, metrics = _fanout(exchange)
    gov = FanoutGovernor(
        f,
        SystemClock(),
        metrics,
        tape_path=fanout_tape_path(tmp_path),
        data_dir=tmp_path,
        boot_key="b",
        boot_started_at_ts=1.0,
        confirm_window_s=WINDOW_S,
    )
    with structlog.testing.capture_logs() as logs:
        d = await gov.tick(reason="boot")
    assert d is not None and d.n == 1 and d.source == "bootstrap"
    assert f.shard_factor == 1
    line = next(e for e in logs if e["event"] == "ws_fanout_derivation")
    assert line["source"] == "bootstrap" and line["shard_factor_applied"] == 1
    assert line["margin"] == 2.0 and line["z"] == 3.0 and line["cap"] == 100
    # An override is applied and named as such — never a silent number.
    gov_o = FanoutGovernor(
        f,
        SystemClock(),
        metrics,
        tape_path=fanout_tape_path(tmp_path),
        data_dir=tmp_path,
        boot_key="b",
        boot_started_at_ts=1.0,
        confirm_window_s=WINDOW_S,
        override=4,
    )
    with structlog.testing.capture_logs() as logs:
        d = await gov_o.tick(reason="boot")
    assert d is not None and d.n == 4 and d.source == "override"
    assert next(e for e in logs if e["event"] == "ws_fanout_derivation")["override"] == 4
    assert f.shard_factor == 4
    with pytest.raises(ValueError):
        FanoutGovernor(
            f,
            SystemClock(),
            metrics,
            tape_path=fanout_tape_path(tmp_path),
            data_dir=tmp_path,
            boot_key="b",
            boot_started_at_ts=1.0,
            confirm_window_s=0.0,
        )


async def test_governor_never_raises_into_the_caller(tmp_path: Path) -> None:
    exchange = _FakeExchange()
    f, metrics = _fanout(exchange)

    async def broken(**kw: Any) -> Any:
        raise OSError("disk")

    gov = FanoutGovernor(
        f,
        SystemClock(),
        metrics,
        tape_path=fanout_tape_path(tmp_path),
        data_dir=tmp_path,
        boot_key="b",
        boot_started_at_ts=1.0,
        confirm_window_s=WINDOW_S,
        io=broken,
    )
    with structlog.testing.capture_logs() as logs:
        assert await gov.tick(reason="boot") is None
    assert [e for e in logs if e["event"] == "ws_fanout_refresh_failed"]
    assert f.shard_factor == 1


# --------------------------------------------------------------------------- #
# 7. Host-level plumbing details
# --------------------------------------------------------------------------- #


async def test_control_frames_from_a_retired_shard_set_are_dropped() -> None:
    exchange = _FakeExchange()
    f, metrics, _ = await _start_sharded(exchange, 2)
    forwarded: list[int] = []

    async def on_error(msg: JsonDict) -> None:
        forwarded.append(int(msg["msg"]["code"]))

    f.on_message("error", on_error)
    try:
        await asyncio.sleep(0.05)
        stale_gen = f.generation - 1
        # A terminal error stamped by a retired generation must neither
        # reconnect a live shard nor reach the intake.
        await f._dispatch(
            {"type": "error", "_shard": 0, "_gen": stale_gen, "msg": {"code": 25, "msg": "x"}}
        )
        await f._dispatch(
            {"type": "subscribed", "_shard": 0, "_gen": stale_gen, "id": 1, "msg": {"sid": 99}}
        )
        assert forwarded == []
        assert metrics.counter("t.retired_control_frame") == 2
        assert exchange.connects == 2
        # A frame with no stamps at all (a test double, a replay) takes the plain path.
        await f._dispatch({"type": "error", "msg": {"code": 6, "msg": "already"}})
        assert forwarded == [6]
    finally:
        await f.stop()


async def test_single_manager_subscribe_error_hook_fires_and_releases_the_pending_ack() -> None:
    """The ws.py seam the fan-out builds on, on a plain WsManager: an error
    frame echoing a subscribe's command id fires ``on_subscribe_error`` once
    and clears the pending ack; without the hook nothing changes but the
    cleanup."""
    m = WsManager("wss://example/ws", object(), SystemClock(), name="t")  # type: ignore[arg-type]
    got: list[tuple[int, str]] = []

    async def on_err(code: int, text: str) -> None:
        got.append((code, text))

    m.add_subscription(["communications"], on_subscribe_error=on_err, shard_factor=3, shard_key=7)
    m.add_subscription(["orderbook_delta"], market_tickers=["M1"])
    m._pending_sub_acks[1] = m._subscriptions[0]
    m._pending_sub_acks[2] = m._subscriptions[1]
    await m._dispatch(
        {"id": 1, "type": "error", "msg": {"code": 21, "msg": "shard_key must be < shard_factor"}}
    )
    assert got == [(21, "shard_key must be < shard_factor")]
    assert 1 not in m._pending_sub_acks
    await m._dispatch({"id": 2, "type": "error", "msg": {"code": 16, "msg": "Market not found"}})
    assert 2 not in m._pending_sub_acks and got == [(21, "shard_key must be < shard_factor")]
    await m._dispatch({"type": "error", "msg": {"code": 6, "msg": "no id"}})  # id 0 = no id


async def test_health_and_send_command_aggregate_over_shards() -> None:
    exchange = _FakeExchange()
    f, _, _ = await _start_sharded(exchange, 2)
    try:
        assert f.connected and f.healthy
        assert f.last_rx_age_s is not None
        states = f.shard_states()
        assert [s["shard"] for s in states] == [0, 1] and all(s["connected"] for s in states)
        cmd_id = await f.send_command("list_subscriptions", {})
        assert json.loads(exchange.by_key[0].sent[-1])["id"] == cmd_id
        exchange.by_key[1].closed = True
        await _until(lambda: not f.connected)
        assert f.last_rx_age_s is None  # any shard down ⇒ no freshness proof
        await _until(lambda: f.connected)  # it reconnects
    finally:
        await f.stop()
    with pytest.raises(RuntimeError):
        await f.send_command("x", {})
