import asyncio
from pathlib import Path

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.reasons import ReasonCode
from combomaker.risk.killswitch import HaltEvent, KillSwitch


async def test_halt_is_idempotent() -> None:
    ks = KillSwitch(FakeClock())
    fired: list[HaltEvent] = []

    async def on_halt(event: HaltEvent) -> None:
        fired.append(event)

    ks.on_halt(on_halt)
    assert await ks.halt(ReasonCode.HALT_DAILY_LOSS, "x") is True
    assert await ks.halt(ReasonCode.HALT_MANUAL, "y") is False
    assert ks.halted
    assert len(fired) == 1
    assert fired[0].reason == ReasonCode.HALT_DAILY_LOSS


async def test_reentrant_halt_from_callback_does_not_double_fire() -> None:
    ks = KillSwitch(FakeClock())
    fired: list[str] = []

    async def reenter(event: HaltEvent) -> None:
        fired.append("first")
        await ks.halt(ReasonCode.HALT_MANUAL, "reenter")

    ks.on_halt(reenter)
    await ks.halt(ReasonCode.HALT_ERROR_RATE)
    assert fired == ["first"]


async def test_failing_callback_does_not_block_others() -> None:
    ks = KillSwitch(FakeClock())
    fired: list[str] = []

    async def bad(event: HaltEvent) -> None:
        raise RuntimeError("boom")

    async def good(event: HaltEvent) -> None:
        fired.append("good")

    ks.on_halt(bad)
    ks.on_halt(good)
    await ks.halt(ReasonCode.HALT_WS_UNHEALTHY)
    assert fired == ["good"]


async def test_concurrent_halts_fire_once() -> None:
    ks = KillSwitch(FakeClock())
    fired: list[HaltEvent] = []

    async def on_halt(event: HaltEvent) -> None:
        await asyncio.sleep(0.01)
        fired.append(event)

    ks.on_halt(on_halt)
    results = await asyncio.gather(
        ks.halt(ReasonCode.HALT_DAILY_LOSS),
        ks.halt(ReasonCode.HALT_ERROR_RATE),
        ks.halt(ReasonCode.HALT_MANUAL),
    )
    assert sum(results) == 1
    assert len(fired) == 1


async def test_clear_requires_actor_and_resets() -> None:
    ks = KillSwitch(FakeClock())
    await ks.halt(ReasonCode.HALT_MANUAL)
    ks.clear(actor="human-cli")
    assert not ks.halted


async def test_kill_file_watch(tmp_path: Path) -> None:
    kill_file = tmp_path / "KILL"
    ks = KillSwitch(FakeClock(), kill_file=kill_file)
    ks.start_kill_file_watch(interval_s=0.01)
    await asyncio.sleep(0.05)
    assert not ks.halted
    kill_file.write_text("stop")
    await asyncio.sleep(0.1)
    assert ks.halted
    assert ks.halt_event is not None
    assert ks.halt_event.reason == ReasonCode.HALT_KILL_FILE
    await ks.stop()


async def test_watch_stop_is_clean(tmp_path: Path) -> None:
    ks = KillSwitch(FakeClock(), kill_file=tmp_path / "KILL")
    ks.start_kill_file_watch(interval_s=10)
    await ks.stop()
    assert not ks.halted


def test_halt_event_is_frozen() -> None:
    event = HaltEvent(reason=ReasonCode.HALT_MANUAL, detail="", at_iso="t")
    with pytest.raises(AttributeError):
        event.detail = "x"  # type: ignore[misc]
