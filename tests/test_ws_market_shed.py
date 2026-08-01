"""MARKET-DATA SHED under dispatch-queue pressure (2026-08-01 pregame-surge
deafness) — unit proofs.

The 2026-08-01 pregame surge (12:08-13:06 ET) exceeded the comms dispatcher's
real-parse drain for 62+ minutes; the fail-closed overflow=>reconnect policy
(410e8fb, sized for transient runaways) then cycled 14 times, discarding
276,644 queued frames — including every queued rfq_created — and the bot took
ZERO new-RFQ intake for most of its only filling window. The repair is a
policy split by frame class (WsManager.mark_sheddable):

  1. MARKET-DATA frames (rfq_created/rfq_deleted) shed OLDEST-first on a full
     queue and the socket stays connected. NEVER a disconnect.
  2. ORDER-INTEGRITY frames (the priority lane) are NEVER shed, and the
     priority lane's own overflow still fails closed (disconnect).
  3. Control frames (subscribed acks, errors) are NEVER shed — displaced to
     the carry lane and dispatched ahead of the queue.
  4. A full queue with nothing sheddable is the original genuine-runaway
     signal => same fail-closed disconnect as before.
  5. Unmarked managers (the book socket) are byte-identical to the old
     fail-closed behavior — orderbook deltas are seq-dependent and must
     never be shed.

Plus the intake WIRE-AGE pre-parse gate: rfq_created frames older (at the
wire-receive stamp) than the quote-freshness horizon are dropped before the
~1ms Rfq.from_ws parse, so a saturated queue drains its dead frames at
subtraction cost. Fail-safe: no stamp / no horizon => byte-identical.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.exchange.ws import WsManager
from combomaker.ops.metrics import Metrics
from combomaker.rfq.intake import RfqIntake

JsonDict = dict[str, Any]


class _Frame:
    def __init__(self, payload: dict) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(payload)


class _IterWs:
    """Async-iterable fake socket yielding pre-canned frames."""

    def __init__(self, frames: list[_Frame]) -> None:
        self._frames = list(frames)
        self.closed = False
        self.close_calls = 0

    def __aiter__(self) -> _IterWs:
        return self

    async def __anext__(self) -> _Frame:
        if not self._frames or self.closed:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _manager(maxsize: int | None = None) -> tuple[WsManager, Metrics]:
    metrics = Metrics()
    m = WsManager("wss://example/ws", object(), SystemClock(), metrics, name="test")  # type: ignore[arg-type]
    if maxsize is not None:
        m._msg_queue = asyncio.Queue(maxsize=maxsize)
    return m, metrics


async def _run_dispatcher(m: WsManager, body) -> None:
    m._dispatch_task = asyncio.create_task(m._dispatch_loop())
    try:
        await body()
    finally:
        m._dispatch_task.cancel()
        try:
            await m._dispatch_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------- #
# 1. Market overflow sheds oldest, stays connected
# --------------------------------------------------------------------------- #


async def test_market_overflow_sheds_oldest_never_disconnects() -> None:
    m, metrics = _manager(maxsize=3)
    m.mark_sheddable("rfq_created")
    ws = _IterWs([_Frame({"type": "rfq_created", "n": i}) for i in range(10)])
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 0  # NEVER disconnect for market-data overflow
    # Oldest shed, newest kept (drop-oldest), FIFO preserved on survivors.
    kept = [m._msg_queue.get_nowait()["n"] for _ in range(m._msg_queue.qsize())]
    assert kept == [7, 8, 9]
    assert metrics.counter("test.shed_market_frames") == 7
    assert metrics.counter("test.shed.rfq_created") == 7
    assert metrics.counter("test.dispatch_queue_overflow") == 0


async def test_shed_survivors_still_dispatch_in_order() -> None:
    m, _ = _manager(maxsize=2)
    m.mark_sheddable("rfq_created")
    seen: list[int] = []

    async def rec(msg: JsonDict) -> None:
        seen.append(msg["n"])

    m.on_message("rfq_created", rec)
    await m._read_loop(  # type: ignore[arg-type]
        _IterWs([_Frame({"type": "rfq_created", "n": i}) for i in range(5)])
    )

    async def body() -> None:
        await asyncio.wait_for(m._msg_queue.join(), timeout=1.0)

    await _run_dispatcher(m, body)
    assert seen == [3, 4]


# --------------------------------------------------------------------------- #
# 2. Order-integrity frames: never shed; priority-lane overflow still closes
# --------------------------------------------------------------------------- #


async def test_order_class_frames_are_never_shed() -> None:
    """An accept arriving mid-saturation rides the priority lane untouched
    while market frames shed around it."""
    m, metrics = _manager(maxsize=2)
    m.mark_priority("quote_accepted")
    m.mark_sheddable("rfq_created")
    frames = [_Frame({"type": "rfq_created", "n": i}) for i in range(4)]
    frames.insert(2, _Frame({"type": "quote_accepted", "q": "a1"}))
    frames.extend(_Frame({"type": "rfq_created", "n": i}) for i in range(4, 6))
    ws = _IterWs(frames)
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 0
    assert m._priority_queue.qsize() == 1  # the accept, intact
    assert metrics.counter("test.shed.quote_accepted") == 0
    assert metrics.counter("test.priority_frame") == 1


async def test_priority_lane_overflow_still_fails_closed() -> None:
    m, _ = _manager()
    m.mark_priority("quote_accepted")
    m.mark_sheddable("rfq_created")
    m._priority_queue = asyncio.Queue(maxsize=1)  # no dispatcher draining
    ws = _IterWs([_Frame({"type": "quote_accepted", "n": i}) for i in (1, 2)])
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 1  # order-integrity overflow => fail-closed


async def test_mark_priority_and_sheddable_are_mutually_exclusive() -> None:
    m, _ = _manager()
    m.mark_priority("quote_accepted")
    with pytest.raises(ValueError):
        m.mark_sheddable("quote_accepted")
    m2, _ = _manager()
    m2.mark_sheddable("rfq_created")
    with pytest.raises(ValueError):
        m2.mark_priority("rfq_created")


async def test_full_queue_no_longer_disconnects_on_accept_wake() -> None:
    """2026-08-01 regression shape: an ACCEPT arriving while the normal queue
    is full used to disconnect (the wake sentinel's put_nowait raised
    QueueFull inside the same try) — dropping the just-won accept with it.
    Now the sentinel is skipped (dispatcher provably busy) and the accept
    stays queued in its lane."""
    m, _ = _manager(maxsize=1)
    m.mark_priority("quote_accepted")
    m.mark_sheddable("rfq_created")
    ws = _IterWs(
        [
            _Frame({"type": "rfq_created", "n": 1}),  # fills the queue
            _Frame({"type": "quote_accepted", "q": "a1"}),
        ]
    )
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 0
    assert m._priority_queue.qsize() == 1


# --------------------------------------------------------------------------- #
# 3. Control frames: displaced to carry, dispatched ahead, never lost
# --------------------------------------------------------------------------- #


async def test_control_frames_survive_shedding_via_carry() -> None:
    m, metrics = _manager(maxsize=3)
    m.mark_sheddable("rfq_created")
    seen: list[str] = []

    async def rec(msg: JsonDict) -> None:
        seen.append(f"{msg['type']}:{msg.get('n', msg.get('id', ''))}")

    m.on_message("rfq_created", rec)
    m.on_message("subscribed", rec)
    # subscribed lands FIRST (queue head), then market frames force sheds:
    # the ack must be displaced to carry, never dropped.
    frames = [_Frame({"type": "subscribed", "id": 7})]
    frames += [_Frame({"type": "rfq_created", "n": i}) for i in range(6)]
    ws = _IterWs(frames)
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 0
    assert metrics.counter("test.shed.subscribed") == 0

    async def body() -> None:
        await asyncio.wait_for(m._msg_queue.join(), timeout=1.0)

    await _run_dispatcher(m, body)
    assert "subscribed:7" in seen  # the ack survived the shed storm
    # And it was dispatched BEFORE the surviving (newer) market frames.
    assert seen.index("subscribed:7") == 0


async def test_nothing_sheddable_still_fails_closed() -> None:
    """A full queue of control frames (nothing sheddable) is the original
    genuine-runaway signal — disconnect, exactly as before."""
    m, _ = _manager(maxsize=2)
    m.mark_sheddable("rfq_created")
    # Simulate control runaway with a tiny carry bound (instance shadows class).
    m._QUEUE_MAX = 2  # type: ignore[misc]
    ws = _IterWs([_Frame({"type": "subscribed", "id": i}) for i in range(6)])
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 1


async def test_unmarked_manager_is_byte_identical_fail_closed() -> None:
    """The book socket path: no mark_sheddable => overflow disconnects,
    exactly the pre-2026-08-01 semantics (seq-dependent deltas must never
    be shed)."""
    m, metrics = _manager(maxsize=2)
    ws = _IterWs([_Frame({"type": "orderbook_delta", "n": i}) for i in range(3)])
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 1
    assert metrics.counter("test.dispatch_queue_overflow") == 1
    assert metrics.counter("test.shed_market_frames") == 0


async def test_discard_queued_clears_carry_too() -> None:
    m, _ = _manager(maxsize=2)
    m.mark_sheddable("rfq_created")
    frames = [_Frame({"type": "subscribed", "id": 1})]
    frames += [_Frame({"type": "rfq_created", "n": i}) for i in range(4)]
    await m._read_loop(_IterWs(frames))  # type: ignore[arg-type]
    assert len(m._carry) == 1
    m._discard_queued()
    assert not m._carry
    assert m._msg_queue.qsize() == 0


# --------------------------------------------------------------------------- #
# 4. Wire-receive stamp on every frame
# --------------------------------------------------------------------------- #


async def test_every_frame_carries_the_wire_stamp() -> None:
    m, _ = _manager()
    await m._read_loop(  # type: ignore[arg-type]
        _IterWs([_Frame({"type": "rfq_created", "n": 1})])
    )
    frame = m._msg_queue.get_nowait()
    assert isinstance(frame.get("_recv_mono_ns"), int)


# --------------------------------------------------------------------------- #
# 5. Intake wire-age pre-parse gate
# --------------------------------------------------------------------------- #


class _StubWs:
    def add_subscription(self, *a: Any, **k: Any) -> None: ...
    def on_message(self, *a: Any, **k: Any) -> None: ...
    def on_disconnect(self, *a: Any, **k: Any) -> None: ...


def _rfq_envelope(recv_ns: int | None = None) -> JsonDict:
    env: JsonDict = {
        "type": "rfq_created",
        "msg": {
            "id": "r1",
            "market_ticker": "KXMVE-C1",
            "created_ts": "2026-08-01T16:00:00Z",
            "contracts_fp": "100.00",
            "mve_collection_ticker": "KXMVESPORTS",
            "mve_selected_legs": [{"market_ticker": "KXMLB-X", "side": "yes"}],
        },
    }
    if recv_ns is not None:
        env["_recv_mono_ns"] = recv_ns
    return env


async def test_intake_drops_stale_wire_age_preparse() -> None:
    clock = FakeClock()
    metrics = Metrics()
    intake = RfqIntake(_StubWs(), metrics, stale_horizon_s=1.5, clock=clock)
    seen: list[str] = []

    async def on_rfq(rfq) -> None:  # noqa: ANN001
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    stamp = clock.monotonic_ns()
    clock.advance(2.0)  # frame aged 2.0s > 1.5s horizon in the backlog
    await intake._handle_rfq_created(_rfq_envelope(stamp))
    assert seen == []
    assert metrics.counter("rfq.dropped_stale_preparse") == 1


async def test_intake_fresh_frame_processes_normally() -> None:
    clock = FakeClock()
    metrics = Metrics()
    intake = RfqIntake(_StubWs(), metrics, stale_horizon_s=1.5, clock=clock)
    seen: list[str] = []

    async def on_rfq(rfq) -> None:  # noqa: ANN001
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    stamp = clock.monotonic_ns()
    clock.advance(0.2)  # well inside the horizon
    await intake._handle_rfq_created(_rfq_envelope(stamp))
    assert seen == ["r1"]
    assert metrics.counter("rfq.dropped_stale_preparse") == 0


async def test_intake_missing_stamp_is_fail_safe() -> None:
    """No stamp (observe mode / replays / foreign envelope) => process
    normally, byte-identical to the pre-gate behavior."""
    clock = FakeClock()
    intake = RfqIntake(_StubWs(), Metrics(), stale_horizon_s=1.5, clock=clock)
    seen: list[str] = []

    async def on_rfq(rfq) -> None:  # noqa: ANN001
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    clock.advance(1000.0)
    await intake._handle_rfq_created(_rfq_envelope(None))
    assert seen == ["r1"]


async def test_intake_no_horizon_is_bit_identical() -> None:
    """Horizon unset (observe mode wiring) => stamp ignored entirely."""
    intake = RfqIntake(_StubWs(), Metrics())
    seen: list[str] = []

    async def on_rfq(rfq) -> None:  # noqa: ANN001
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    await intake._handle_rfq_created(_rfq_envelope(1))  # ancient stamp
    assert seen == ["r1"]


async def test_intake_rfq_deleted_never_age_dropped() -> None:
    """Deletions maintain mirror consistency and are cheap — the age gate
    must not apply."""
    clock = FakeClock()
    metrics = Metrics()
    intake = RfqIntake(_StubWs(), metrics, stale_horizon_s=1.5, clock=clock)
    deleted: list[str] = []

    async def on_del(rfq_id: str, msg: JsonDict) -> None:
        deleted.append(rfq_id)

    intake.on_rfq_deleted(on_del)
    stamp = clock.monotonic_ns()
    clock.advance(60.0)
    await intake._handle_rfq_deleted(
        {"type": "rfq_deleted", "msg": {"id": "r9"}, "_recv_mono_ns": stamp}
    )
    assert deleted == ["r9"]
