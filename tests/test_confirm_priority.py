"""CONFIRM PRIORITY INVERSION fix (2026-07-31 double halt) — unit proofs.

Both 2026-07-31 halts were HALT_CONFIRM_TIMEOUTS: the accept event waited out
most of the exchange's 3.0s confirm window UPSTREAM of the accept handler
(measured: in-handler time <= 1.14s on all 12 expired confirms ever taped),
behind the comms dispatcher's FIFO backlog and the reprice storm's loop
contention. Three mechanisms, each proven here:

  1. WsManager priority lane — quote_accepted/quote_executed frames jump the
     dispatch backlog (at most ONE normal dispatch of wait).
  2. AcceptPriorityGate — from the moment an accept is seen until its confirm
     handling ends, new quote work parks; bound derived from the exchange
     window (never hand-set).
  3. RfqIntake hold — rfq_created frames (the dominant per-frame parse cost)
     are dropped pre-parse while a confirm is in flight.

The mid-storm replay proof lives at the bottom: a dispatcher drowning in
normal frames still delivers the accept in a small fraction of the window.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.exchange.ws import WsManager
from combomaker.ops.metrics import Metrics
from combomaker.ops.quote_app import AcceptPriorityGate
from combomaker.rfq.intake import RfqIntake

JsonDict = dict[str, Any]


# --------------------------------------------------------------------------- #
# WsManager priority lane
# --------------------------------------------------------------------------- #


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


def _manager() -> WsManager:
    return WsManager("wss://example/ws", object(), SystemClock(), name="test")  # type: ignore[arg-type]


async def _settled(m: WsManager, within_s: float = 1.0) -> None:
    """Lane analogue of ``Queue.join``: the dispatcher clears ``wake_pending``
    only after a drain observed every lane empty, i.e. the last handler
    returned (2026-09-05 lanes port)."""

    async def _wait() -> None:
        while any(m.lane_depths().values()) or m._lanes.wake_pending:  # noqa: ASYNC110
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait(), timeout=within_s)


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


async def test_priority_frame_dispatches_before_queued_backlog() -> None:
    m = _manager()
    m.mark_priority("quote_accepted")
    order: list[str] = []

    async def rec(msg: JsonDict) -> None:
        order.append(f"{msg['type']}:{msg.get('n', '')}")

    m.on_message("rfq_created", rec)
    m.on_message("quote_accepted", rec)

    frames = [_Frame({"type": "rfq_created", "n": i}) for i in (1, 2, 3)]
    frames.append(_Frame({"type": "quote_accepted", "n": 9}))
    await m._read_loop(_IterWs(frames))  # type: ignore[arg-type]

    async def body() -> None:
        await _settled(m)

    await _run_dispatcher(m, body)
    # The accept arrived LAST but dispatches FIRST (drained before any normal
    # frame); normal FIFO is otherwise preserved.
    assert order == ["quote_accepted:9", "rfq_created:1", "rfq_created:2", "rfq_created:3"]


async def test_priority_fifo_within_lane_and_wake_on_idle() -> None:
    m = _manager()
    m.mark_priority("quote_accepted", "quote_executed")
    order: list[str] = []
    got_two = asyncio.Event()

    async def rec(msg: JsonDict) -> None:
        order.append(f"{msg['type']}:{msg['n']}")
        if len(order) == 2:
            got_two.set()

    m.on_message("quote_accepted", rec)
    m.on_message("quote_executed", rec)

    async def body() -> None:
        # Dispatcher is IDLE (empty queues) — the wake sentinel must wake it.
        await m._read_loop(  # type: ignore[arg-type]
            _IterWs(
                [
                    _Frame({"type": "quote_accepted", "n": 1}),
                    _Frame({"type": "quote_executed", "n": 2}),
                ]
            )
        )
        await asyncio.wait_for(got_two.wait(), timeout=1.0)

    await _run_dispatcher(m, body)
    # accept→executed order preserved within the priority lane (FIFO).
    assert order == ["quote_accepted:1", "quote_executed:2"]


async def test_unmarked_types_keep_plain_fifo() -> None:
    m = _manager()  # mark_priority never called
    order: list[int] = []

    async def rec(msg: JsonDict) -> None:
        order.append(msg["n"])

    m.on_message("t", rec)
    await m._read_loop(  # type: ignore[arg-type]
        _IterWs([_Frame({"type": "t", "n": i}) for i in (1, 2, 3)])
    )

    async def body() -> None:
        await _settled(m)

    await _run_dispatcher(m, body)
    assert order == [1, 2, 3]


async def test_priority_overflow_fails_closed() -> None:
    m = _manager()
    m.mark_priority("quote_accepted")
    m._lanes.capacity = 1  # no dispatcher draining
    ws = _IterWs([_Frame({"type": "quote_accepted", "n": i}) for i in (1, 2)])
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 1  # overflow ⇒ close ⇒ reconnect (fail-closed)


async def test_discard_queued_drains_both_lanes() -> None:
    m = _manager()
    m.mark_priority("quote_accepted")
    await m._read_loop(  # type: ignore[arg-type]
        _IterWs(
            [
                _Frame({"type": "rfq_created", "n": 1}),
                _Frame({"type": "quote_accepted", "n": 2}),
            ]
        )
    )
    # (2026-09-05 lanes port: the wake SENTINEL is gone — wakeups are
    # coalesced cross-thread — so the normal side holds exactly the frame.)
    assert m.lane_depths() == {"priority": 1, "control": 1, "market": 0}
    m._discard_queued()
    assert m.lane_depths() == {"priority": 0, "control": 0, "market": 0}


# --------------------------------------------------------------------------- #
# AcceptPriorityGate
# --------------------------------------------------------------------------- #


def test_gate_holds_from_enqueue_to_done() -> None:
    clock = FakeClock()
    gate = AcceptPriorityGate(clock, 3.0)
    assert not gate.holding()
    gate.accept_enqueued()
    assert gate.holding()
    gate.accept_done()
    assert not gate.holding()


def test_gate_holds_until_last_of_overlapping_accepts() -> None:
    clock = FakeClock()
    gate = AcceptPriorityGate(clock, 3.0)
    gate.accept_enqueued()
    gate.accept_enqueued()
    gate.accept_done()
    assert gate.holding()  # one confirm still in flight
    gate.accept_done()
    assert not gate.holding()


def test_gate_fail_safe_bound_is_the_exchange_window() -> None:
    clock = FakeClock()
    gate = AcceptPriorityGate(clock, 3.0)
    gate.accept_enqueued()
    clock.advance(2.9)
    assert gate.holding()  # still inside the window
    clock.advance(0.2)  # past 3.0s: the exchange has voided the confirm anyway
    assert not gate.holding()


def test_gate_new_accept_reanchors_the_bound() -> None:
    clock = FakeClock()
    gate = AcceptPriorityGate(clock, 3.0)
    gate.accept_enqueued()
    clock.advance(2.5)
    gate.accept_enqueued()  # fresh accept owns a fresh window
    clock.advance(1.0)  # 3.5s after the FIRST accept, 1.0s after the second
    assert gate.holding()


def test_gate_spurious_done_never_underflows() -> None:
    clock = FakeClock()
    gate = AcceptPriorityGate(clock, 3.0)
    gate.accept_done()  # no accept pending — must not corrupt state
    gate.accept_enqueued()
    assert gate.holding()
    gate.accept_done()
    assert not gate.holding()


async def test_gate_wait_clear_wakes_on_done() -> None:
    gate = AcceptPriorityGate(SystemClock(), 3.0)
    gate.accept_enqueued()
    woke = asyncio.Event()

    async def waiter() -> None:
        await gate.wait_clear()
        woke.set()

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert not woke.is_set()
    gate.accept_done()
    await asyncio.wait_for(woke.wait(), timeout=1.0)
    await t


async def test_gate_wait_clear_is_noop_when_clear() -> None:
    gate = AcceptPriorityGate(SystemClock(), 3.0)
    await asyncio.wait_for(gate.wait_clear(), timeout=0.1)  # returns immediately


# --------------------------------------------------------------------------- #
# RfqIntake hold probe
# --------------------------------------------------------------------------- #


class _StubWs:
    def add_subscription(self, channels, **kw) -> None:  # noqa: ANN001
        pass

    def on_message(self, msg_type, handler) -> None:  # noqa: ANN001
        pass

    def on_disconnect(self, handler) -> None:  # noqa: ANN001
        pass


def _rfq_msg() -> JsonDict:
    return {
        "msg": {
            "id": "r1",
            "market_ticker": "KXMVE-C1",
            "created_ts": "2026-07-31T00:00:00Z",
            "contracts_fp": "100.00",
            "mve_collection_ticker": "KXMVESPORTS",
            "mve_selected_legs": [{"market_ticker": "KXMLB-X", "side": "yes"}],
        }
    }


async def test_intake_drops_rfq_created_while_holding() -> None:
    holding = True
    metrics = Metrics()
    intake = RfqIntake(_StubWs(), metrics, hold_probe=lambda: holding)
    seen: list[str] = []

    async def on_rfq(rfq) -> None:  # noqa: ANN001
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    await intake._handle_rfq_created(_rfq_msg())
    assert seen == []  # dropped pre-parse
    assert metrics.counter("rfq.dropped_accept_priority") == 1
    assert "r1" not in intake.open_rfqs

    holding = False
    await intake._handle_rfq_created(_rfq_msg())
    assert seen == ["r1"]  # normal flow restored the instant the hold clears


async def test_intake_rfq_deleted_still_processes_while_holding() -> None:
    metrics = Metrics()
    intake = RfqIntake(_StubWs(), metrics, hold_probe=lambda: True)
    deleted: list[str] = []

    async def on_del(rfq_id: str, msg: JsonDict) -> None:
        deleted.append(rfq_id)

    intake.on_rfq_deleted(on_del)
    intake.open_rfqs["dead"] = object()  # type: ignore[assignment]
    await intake._handle_rfq_deleted({"msg": {"id": "dead"}})
    assert deleted == ["dead"]
    assert "dead" not in intake.open_rfqs  # mirror consistency kept during hold


async def test_intake_without_probe_is_bit_identical() -> None:
    intake = RfqIntake(_StubWs(), Metrics())  # no probe — observe-mode shape
    seen: list[str] = []

    async def on_rfq(rfq) -> None:  # noqa: ANN001
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    await intake._handle_rfq_created(_rfq_msg())
    assert seen == ["r1"]


# --------------------------------------------------------------------------- #
# MID-STORM REPLAY: an accept lands while the dispatcher is drowning in the
# firehose. The proof is RELATIVE (same machine, same storm, only the lane
# toggled) so it cannot flake on host speed: with the lane the accept waits
# for at most one normal dispatch; without it, the whole backlog.
# --------------------------------------------------------------------------- #


async def _storm_accept_latency(*, priority: bool) -> tuple[float, float]:
    """(accept latency s, storm total s) with a 2,000-frame backlog ahead."""
    m = _manager()
    if priority:
        m.mark_priority("quote_accepted")
    accept_seen = asyncio.Event()
    done = asyncio.Event()
    handled = 0

    async def firehose_handler(msg: JsonDict) -> None:
        nonlocal handled
        handled += 1
        await asyncio.sleep(0)  # each frame costs at least one loop pass
        if handled == 2000:
            done.set()

    async def accept_handler(msg: JsonDict) -> None:
        accept_seen.set()

    m.on_message("rfq_created", firehose_handler)
    m.on_message("quote_accepted", accept_handler)

    frames = [_Frame({"type": "rfq_created", "n": i}) for i in range(2000)]
    frames.append(_Frame({"type": "quote_accepted", "n": 0}))
    await m._read_loop(_IterWs(frames))  # type: ignore[arg-type]

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    accept_at = storm_at = 0.0

    async def body() -> None:
        nonlocal accept_at, storm_at
        await asyncio.wait_for(accept_seen.wait(), timeout=30.0)
        accept_at = loop.time() - t0
        await asyncio.wait_for(done.wait(), timeout=30.0)
        storm_at = loop.time() - t0

    await _run_dispatcher(m, body)
    return accept_at, storm_at


async def test_mid_storm_accept_jumps_the_backlog() -> None:
    with_lane, storm_s = await _storm_accept_latency(priority=True)
    without_lane, _ = await _storm_accept_latency(priority=False)
    # Without the lane the accept waits out the WHOLE storm (it was queued
    # last); with it, it must land in a small fraction of that.
    assert without_lane >= storm_s * 0.5  # sanity: the storm really was ahead
    assert with_lane < without_lane / 10
    # And in absolute protocol terms: the lane's wait is far inside the 3.0s
    # exchange window even with 2,000 frames queued ahead.
    assert with_lane < 0.5
