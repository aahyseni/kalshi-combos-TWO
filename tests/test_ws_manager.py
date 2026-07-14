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
