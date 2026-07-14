"""WsManager write-dead-socket recovery (2026-07-13 live bug).

A half-dead WS — read side alive (server pings + book deltas keep arriving so
receive_timeout never fires), write side dead ("Cannot write to closing
transport") — silently failed EVERY new leg-book subscription (80
live_subscribe_failed / only 4 books subscribed live), so combos on the
unsubscribed legs all declined skip_leg_stale. A failed write must force ONE
reconnect to rebuild full duplex.
"""
from __future__ import annotations

import aiohttp
import pytest

from combomaker.core.clock import SystemClock
from combomaker.exchange.ws import WsManager


class _FakeWs:
    def __init__(self, *, raise_on_send: bool = False) -> None:
        self.closed = False
        self._raise = raise_on_send
        self.close_calls = 0
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        if self._raise:
            raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")
        self.sent.append(data)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _manager() -> WsManager:
    # signer/clock are stored but unused by send_command/force_reconnect.
    return WsManager("wss://example/ws", object(), SystemClock(), name="test")  # type: ignore[arg-type]


async def test_send_command_write_failure_forces_one_reconnect() -> None:
    m = _manager()
    fake = _FakeWs(raise_on_send=True)
    m._ws = fake  # type: ignore[assignment]
    with pytest.raises(aiohttp.ClientError):
        await m.send_command("subscribe", {"channels": ["orderbook_delta"]})
    assert fake.close_calls == 1  # write-dead socket → forced reconnect
    assert m._force_reconnecting is True


async def test_send_command_success_does_not_reconnect() -> None:
    m = _manager()
    fake = _FakeWs(raise_on_send=False)
    m._ws = fake  # type: ignore[assignment]
    cmd_id = await m.send_command("subscribe", {"channels": ["orderbook_delta"]})
    assert cmd_id == 1
    assert fake.close_calls == 0
    assert m._force_reconnecting is False
    assert len(fake.sent) == 1


async def test_force_reconnect_reentrancy_guard() -> None:
    m = _manager()
    fake = _FakeWs()
    m._ws = fake  # type: ignore[assignment]
    await m.force_reconnect()
    await m.force_reconnect()  # burst of failed writes must not re-close
    assert fake.close_calls == 1
    assert m._force_reconnecting is True


async def test_send_command_on_closed_socket_raises_runtime_not_reconnect() -> None:
    m = _manager()
    fake = _FakeWs()
    fake.closed = True
    m._ws = fake  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await m.send_command("subscribe", {"channels": ["x"]})
    assert fake.close_calls == 0  # already closed — nothing to force


# --------------------------------------------------------------------------- #
# Pong-starvation fix: the read loop must NEVER await handlers inline — it
# reads + enqueues; a dispatcher task consumes in order. (2026-07-14: inline
# handler awaits stalled receive() during RFQ bursts, Kalshi's 10s pings went
# un-ponged, and the server closed us every ~90-150s.)
# --------------------------------------------------------------------------- #

import asyncio
import json


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

    def __aiter__(self) -> "_IterWs":
        return self

    async def __anext__(self) -> _Frame:
        if not self._frames or self.closed:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


async def test_read_loop_never_blocks_on_slow_handler() -> None:
    m = _manager()
    seen: list[int] = []
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(msg: dict) -> None:
        seen.append(msg["n"])
        if msg["n"] == 1:
            first_started.set()
            await release.wait()  # simulate pricing + REST round trip

    m.on_message("t", slow_handler)
    m._dispatch_task = asyncio.create_task(m._dispatch_loop())
    try:
        frames = [_Frame({"type": "t", "n": i}) for i in (1, 2, 3)]
        # The read loop must drain ALL frames even while handler #1 is stuck —
        # under the old inline design this would deadlock (read blocked on n=1).
        await asyncio.wait_for(m._read_loop(_IterWs(frames)), timeout=1.0)  # type: ignore[arg-type]
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        assert m._msg_queue.qsize() == 2  # n=2, n=3 buffered, not blocking reads
        release.set()
        await asyncio.wait_for(m._msg_queue.join(), timeout=1.0)
        assert seen == [1, 2, 3]  # FIFO order preserved (seq continuity)
    finally:
        release.set()
        m._dispatch_task.cancel()
        try:
            await m._dispatch_task
        except asyncio.CancelledError:
            pass


async def test_read_loop_queue_overflow_fails_closed() -> None:
    m = _manager()
    m._msg_queue = asyncio.Queue(maxsize=2)  # no dispatcher draining
    ws = _IterWs([_Frame({"type": "t", "n": i}) for i in (1, 2, 3)])
    await m._read_loop(ws)  # type: ignore[arg-type]
    assert ws.close_calls == 1  # overflow ⇒ close ⇒ reconnect path (fail-closed)
    assert m._msg_queue.qsize() == 2  # the two that fit; the third triggered close
