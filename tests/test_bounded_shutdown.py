"""BOUNDED SHUTDOWN (2026-07-27) — ``QuoteApp._shutdown``.

THE INCIDENT. The 07-27 run's last log line ever was ``joint_pool_stopped`` at
10:17:18.39; ``book_risk_pool_stopped`` and ``quote_app_stopped`` appear ZERO
times in that file and the next boot was 32.6 minutes later. The book was
already flat — 4.2s earlier the same bot withdrew all 67 quotes in 218ms — so
32.6 of the 33.4 minutes of downtime was teardown hang AFTER exposure was gone.

The contract these tests pin:

1. The book is flattened FIRST and UNBOUNDED (no deadline may cut cancel-all
   short), and the bounded region provably does not start until it is done.
2. Everything after runs under ONE wall bound derived from the EXISTING
   ``supervisor.heartbeat_timeout_s`` anchor — no new number.
3. On expiry: ``shutdown_timed_out`` naming the last COMPLETED stage, then a
   hard exit. Both when the hang is an ``await`` AND when it blocks the event
   loop synchronously (where ``asyncio.wait_for`` alone is powerless).
4. Every stage emits ``shutdown_step``, so the next hang names itself.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from combomaker.ops.quote_app import QuoteApp, ShutdownStage

# The bound is the operator's wedge-tolerance anchor in production. Tests inject
# a scaled-down budget through the same parameter so they run in milliseconds;
# nothing here introduces a production number.
BUDGET_S = 0.25


def _app() -> QuoteApp:
    """A bare QuoteApp shell. ``_shutdown`` touches no instance state other than
    (optionally) the config budget, which these tests pass explicitly."""
    return QuoteApp.__new__(QuoteApp)


def _capture_logs(monkeypatch: Any) -> list[tuple[str, dict[str, Any]]]:
    import combomaker.ops.quote_app as qa

    seen: list[tuple[str, dict[str, Any]]] = []
    for level in ("info", "error"):
        real = getattr(qa.log, level)

        def _cap(event: str, _real=real, **kw: Any) -> None:  # type: ignore[no-untyped-def]
            seen.append((event, kw))
            _real(event, **kw)

        monkeypatch.setattr(qa.log, level, _cap)
    return seen


# --------------------------------------------------------------------------- #
# 1. Happy path: every stage runs, in order, each named; no exit.
# --------------------------------------------------------------------------- #


async def test_shutdown_completes_and_names_every_step(monkeypatch: Any) -> None:
    seen = _capture_logs(monkeypatch)
    order: list[str] = []
    exits: list[int] = []

    async def _cancel() -> None:
        order.append("cancel_all")

    async def _async_stage() -> None:
        order.append("ws_stop")

    def _sync_stage() -> None:
        order.append("joint_pool_shutdown")  # pools are synchronous by design

    completed = await _app()._shutdown(  # noqa: SLF001
        cancel_all=_cancel,
        stages=[
            ShutdownStage("ws_stop", _async_stage),
            ShutdownStage("joint_pool_shutdown", _sync_stage),
        ],
        budget_s=BUDGET_S,
        exit_fn=exits.append,
    )

    assert completed is True
    assert exits == []  # no hard exit on the healthy path
    assert order == ["cancel_all", "ws_stop", "joint_pool_shutdown"]
    steps = [kw["step"] for e, kw in seen if e == "shutdown_step"]
    assert steps == ["ws_stop", "joint_pool_shutdown"]
    assert not [e for e, _ in seen if e == "shutdown_timed_out"]


# --------------------------------------------------------------------------- #
# 2. THE BOOK IS PROVABLY FLAT BEFORE THE BOUND BEGINS.
# --------------------------------------------------------------------------- #


async def test_cancel_all_is_outside_the_bound_and_completes_first(
    monkeypatch: Any,
) -> None:
    """cancel-all is deliberately UNBOUNDED: it takes LONGER than the whole
    teardown budget here and still runs to completion, and only then does the
    first bounded stage start. A deadline must never cut the book short."""
    _capture_logs(monkeypatch)
    order: list[str] = []
    exits: list[int] = []
    open_quotes = [f"q{i}" for i in range(67)]  # the incident's 67 resting quotes

    async def _cancel() -> None:
        await asyncio.sleep(BUDGET_S * 3)  # 3x the ENTIRE teardown budget
        open_quotes.clear()
        order.append("cancel_all")

    async def _first_stage() -> None:
        # If the bound covered cancel-all, this would never be reached.
        assert open_quotes == [], "bounded teardown started with a live book"
        order.append("ws_stop")

    started = time.monotonic()
    completed = await _app()._shutdown(  # noqa: SLF001
        cancel_all=_cancel,
        stages=[ShutdownStage("ws_stop", _first_stage)],
        budget_s=BUDGET_S,
        exit_fn=exits.append,
    )
    elapsed = time.monotonic() - started

    assert completed is True
    assert exits == []
    assert order == ["cancel_all", "ws_stop"]
    assert elapsed > BUDGET_S  # cancel-all really did outlast the budget
    assert open_quotes == []   # ...and the book really is flat


async def test_a_raising_cancel_all_still_reaches_the_bounded_teardown(
    monkeypatch: Any,
) -> None:
    """Crash-path discipline: cancel-all is best-effort. A throw is logged and
    teardown still proceeds — an un-cancellable book must not also strand the
    process."""
    seen = _capture_logs(monkeypatch)
    exits: list[int] = []
    ran: list[str] = []

    async def _cancel() -> None:
        raise ConnectionError("exchange unreachable")

    completed = await _app()._shutdown(  # noqa: SLF001
        cancel_all=_cancel,
        stages=[ShutdownStage("store_close", lambda: ran.append("store_close"))],
        budget_s=BUDGET_S,
        exit_fn=exits.append,
    )
    assert completed is True
    assert ran == ["store_close"]
    assert [kw["step"] for e, kw in seen if e == "shutdown_step"] == ["store_close"]


# --------------------------------------------------------------------------- #
# 3. A HANG IS BOUNDED AND NAMES ITSELF — both hang shapes.
# --------------------------------------------------------------------------- #


async def test_hung_await_hard_exits_within_the_bound(monkeypatch: Any) -> None:
    """The 07-27 shape: a teardown stage that never returns. The process must
    hard-exit within the bound, naming the last COMPLETED stage."""
    seen = _capture_logs(monkeypatch)
    exits: list[int] = []

    async def _fine() -> None:
        return None

    async def _hangs() -> None:
        await asyncio.sleep(3600)

    reached_after_hang = []

    started = time.monotonic()
    completed = await _app()._shutdown(  # noqa: SLF001
        cancel_all=_fine,
        stages=[
            ShutdownStage("joint_pool_shutdown", _fine),
            ShutdownStage("book_risk_pool_shutdown", _hangs),
            ShutdownStage("store_close", lambda: reached_after_hang.append(1)),
        ],
        budget_s=BUDGET_S,
        exit_fn=exits.append,
    )
    elapsed = time.monotonic() - started

    assert completed is False
    assert exits == [1]                       # hard exit, non-zero
    assert elapsed < BUDGET_S * 4, f"{elapsed:.3f}s vs bound {BUDGET_S}s"
    assert reached_after_hang == []           # the hung stage was cut off
    timed_out = [kw for e, kw in seen if e == "shutdown_timed_out"]
    assert len(timed_out) == 1
    # It names WHERE it hung: the last stage that completed, exactly the line
    # missing from the 07-27 log after joint_pool_stopped.
    assert timed_out[0]["last_step"] == "joint_pool_shutdown"
    assert timed_out[0]["budget_s"] == BUDGET_S


def test_synchronously_blocked_loop_still_hard_exits(monkeypatch: Any) -> None:
    """``asyncio.wait_for`` can only interrupt an AWAIT. A stage that blocks the
    event loop synchronously (a pool join, a stuck SQLite close) would make the
    async bound itself unreachable — so a daemon watchdog on the SAME budget
    carries the guarantee. Without it this test hangs forever."""
    seen = _capture_logs(monkeypatch)
    exits: list[int] = []

    async def _fine() -> None:
        return None

    def _blocks_the_loop() -> None:
        time.sleep(BUDGET_S * 4)  # NOT awaitable: the event loop is frozen

    async def _drive() -> bool:
        return await _app()._shutdown(  # noqa: SLF001
            cancel_all=_fine,
            stages=[
                ShutdownStage("killswitch_stop", _fine),
                ShutdownStage("store_close", _blocks_the_loop),
            ],
            budget_s=BUDGET_S,
            exit_fn=exits.append,
        )

    started = time.monotonic()
    asyncio.run(_drive())
    elapsed = time.monotonic() - started

    assert exits == [1], "the sync-blocked teardown was never hard-exited"
    timed_out = [kw for e, kw in seen if e == "shutdown_timed_out"]
    assert len(timed_out) == 1
    assert timed_out[0]["last_step"] == "killswitch_stop"
    assert timed_out[0]["source"] == "watchdog"  # the async bound could not fire
    # The watchdog fired ON the budget even though the loop was frozen; the
    # blocking call still has to unwind, hence the generous upper check.
    assert elapsed < BUDGET_S * 8


# --------------------------------------------------------------------------- #
# 4. Per-stage exception policy is unchanged by the rewrite.
# --------------------------------------------------------------------------- #


async def test_best_effort_stages_log_and_continue(monkeypatch: Any) -> None:
    seen = _capture_logs(monkeypatch)
    exits: list[int] = []
    ran: list[str] = []

    async def _fine() -> None:
        return None

    async def _boom() -> None:
        raise RuntimeError("sweep drain failed")

    completed = await _app()._shutdown(  # noqa: SLF001
        cancel_all=_fine,
        stages=[
            ShutdownStage("diagnostic_sweep_drain", _boom, best_effort=True),
            ShutdownStage("store_close", lambda: ran.append("store_close")),
        ],
        budget_s=BUDGET_S,
        exit_fn=exits.append,
    )
    assert completed is True
    assert ran == ["store_close"]  # the drain failure did not strand the store
    assert [kw["step"] for e, kw in seen if e == "shutdown_step"] == [
        "diagnostic_sweep_drain",
        "store_close",
    ]


async def test_non_best_effort_stage_still_propagates(monkeypatch: Any) -> None:
    """Unchanged from before the rewrite: a hard failure in a non-best-effort
    stage propagates rather than being silently swallowed."""
    _capture_logs(monkeypatch)
    exits: list[int] = []

    async def _fine() -> None:
        return None

    async def _boom() -> None:
        raise RuntimeError("ws stop failed")

    try:
        await _app()._shutdown(  # noqa: SLF001
            cancel_all=_fine,
            stages=[ShutdownStage("ws_stop", _boom)],
            budget_s=BUDGET_S,
            exit_fn=exits.append,
        )
    except RuntimeError as exc:
        assert "ws stop failed" in str(exc)
    else:
        raise AssertionError("a non-best-effort stage failure was swallowed")
    assert exits == []  # a raise is not a timeout — no hard exit


# --------------------------------------------------------------------------- #
# 5. THE BOUND IS DERIVED, NOT HAND-SET.
# --------------------------------------------------------------------------- #


async def test_default_budget_is_the_supervisor_wedge_anchor(monkeypatch: Any) -> None:
    """NO NEW NUMBER: with no explicit budget the bound is exactly
    ``config.supervisor.heartbeat_timeout_s`` — the operator's single stated
    wedge tolerance, the same anchor every loop's stall bound derives from."""
    seen = _capture_logs(monkeypatch)
    exits: list[int] = []

    class _Sup:
        heartbeat_timeout_s = BUDGET_S

    class _Cfg:
        supervisor = _Sup()

    app = _app()
    app._config = _Cfg()  # type: ignore[assignment]  # noqa: SLF001

    async def _fine() -> None:
        return None

    async def _hangs() -> None:
        await asyncio.sleep(3600)

    completed = await app._shutdown(  # noqa: SLF001
        cancel_all=_fine,
        stages=[ShutdownStage("ws_stop", _hangs)],
        exit_fn=exits.append,  # budget_s omitted on purpose
    )
    assert completed is False
    timed_out = [kw for e, kw in seen if e == "shutdown_timed_out"]
    assert timed_out[0]["budget_s"] == _Sup.heartbeat_timeout_s
