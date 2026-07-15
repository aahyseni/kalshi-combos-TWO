"""Hermetic guard for the unit-test suite.

Lesson learned live (2026-07-05): a CLI test asserting "quote mode refuses to
start" went LIVE against demo the moment the conventions fixture was promoted
and a whitelist landed in demo.yaml — the gates it relied on opened, and
main()'s .env loading handed it real credentials. Unit tests must never be one
config change away from the network: strip every credential var and disable
.env loading for everything outside tests/integration.
"""

from __future__ import annotations

import gc
import weakref

import aiosqlite
import pytest

_SENSITIVE = (
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "KALSHI_PRIVATE_KEY_PEM",
    "KALSHI_REQUESTER_API_KEY_ID",
    "KALSHI_REQUESTER_PRIVATE_KEY_PATH",
    "KALSHI_REQUESTER_PRIVATE_KEY_PEM",
    "KALSHI_SUPERVISOR_API_KEY_ID",
    "KALSHI_SUPERVISOR_PRIVATE_KEY_PATH",
    "KALSHI_SUPERVISOR_PRIVATE_KEY_PEM",
    "SPORTSGAMEODDS_API_KEY",
)


@pytest.fixture(autouse=True)
def hermetic_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    if "tests/integration" in str(request.node.fspath).replace("\\", "/"):
        yield
        return
    for name in _SENSITIVE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COMBOMAKER_NO_DOTENV", "1")
    yield


# --------------------------------------------------------------------------- #
# Deterministic aiosqlite teardown (removes ~27 benign teardown warnings).     #
# --------------------------------------------------------------------------- #
#
# ROOT CAUSE (not the process pools — those already join cleanly in
# JointPool/BookRiskPool.shutdown()). aiosqlite runs each Connection's sqlite
# work on a dedicated background thread. Closing a Connection (Store.close →
# _db.close) drains that thread IN-LOOP and sets _connection=None, so a properly
# closed Store emits nothing. But several async tests open a Store and never
# close it (the Store is a local that just falls out of scope). With
# asyncio_mode="auto" + a function-scoped event loop, pytest-asyncio CLOSES the
# test's loop at test end; the leaked Connection is then finalized by the garbage
# collector LATER — during some *unrelated* later test or at interpreter exit.
# aiosqlite.Connection.__del__ → stop() enqueues a "close" onto the worker
# thread, and the worker calls `future.get_loop().call_soon_threadsafe(...)` on
# the loop that CREATED the future — which is already closed → the thread raises
# RuntimeError('Event loop is closed'), surfaced by pytest as a
# PytestUnhandledThreadExceptionWarning. The thread numbers span the whole run
# precisely because the finalization is GC-timed, not test-attributed.
#
# FIX AT THE SOURCE (two coupled decisions, both needed):
#
# 1) WHEN we stop the worker — an ASYNC autouse fixture. Fixture finalizers run
#    LIFO, and pytest-asyncio closes the function loop in its own finalizer. A
#    *sync* autouse fixture set up before the loop therefore tears down AFTER the
#    loop is already gone; we measured exactly this (get_event_loop() → "no
#    current event loop" at teardown), and by then the worker's in-flight
#    call_soon_threadsafe on the closing loop has already raised. An async
#    fixture's teardown body runs while the loop is STILL OPEN, so we pre-empt the
#    race and stop the worker before the close. (This is why a purely sync version
#    only removed ~14 of 26 — the GC-timed leftovers — and left ~12 that fire
#    during the loop close of their own test.)
#
# 2) HOW we stop it — a LOOP-FREE thread stop (_hard_stop_connection), NOT
#    aiosqlite's own `await conn.close()`. close() queues a future on the current
#    loop and awaits it; an async fixture that does that on EVERY test deadlocked
#    the ProcessPool-spawning tests (test_intake / lifecycle / book-risk). Instead
#    we enqueue a raw-sqlite3 close + the stop sentinel with future=None (the
#    worker runs the fn and breaks WITHOUT ever touching an event loop), then join
#    the thread. Once _connection is None and the thread has exited, the later
#    GC-time __del__ is a no-op and can never call_soon_threadsafe() on a closed
#    loop. A test that closed its own Store leaves nothing here (_connection is
#    already None) — well-behaved tests are untouched, and no test is weakened.
def _hard_stop_connection(conn: aiosqlite.Connection) -> None:
    raw = getattr(conn, "_connection", None)
    if raw is None:
        return

    def _close_and_stop():  # type: ignore[no-untyped-def]
        try:
            raw.close()
        except Exception:  # pragma: no cover - best-effort
            pass
        conn._connection = None  # type: ignore[attr-defined]
        return aiosqlite.core._STOP_RUNNING_SENTINEL  # type: ignore[attr-defined]

    conn._running = False  # type: ignore[attr-defined]
    # future=None ⇒ the worker runs the fn and breaks on the sentinel WITHOUT
    # ever calling future.get_loop().call_soon_threadsafe — no loop is touched.
    conn._tx.put_nowait((None, _close_and_stop))  # type: ignore[attr-defined]
    thread = getattr(conn, "_thread", None)
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)


@pytest.fixture(autouse=True)
async def _close_leaked_aiosqlite_connections():  # type: ignore[no-untyped-def]
    # ASYNC (not sync) on purpose: pytest-asyncio's function-scoped event loop is
    # torn down in ITS finalizer, and fixture finalizers run LIFO. A *sync*
    # autouse fixture set up before the loop tears down AFTER the loop is already
    # closed — too late: the aiosqlite worker's in-flight call_soon_threadsafe has
    # nowhere to land and the 'Event loop is closed' exception has already fired.
    # An async fixture's teardown body runs while the loop is STILL OPEN, so
    # stopping the worker here happens before the close, pre-empting the race.
    # We stop the worker via _hard_stop_connection (a loop-free thread stop), NOT
    # aiosqlite's own await close() — so this fixture never re-enters the async
    # sqlite path and cannot deadlock the ProcessPool-spawning tests.
    live: weakref.WeakSet[aiosqlite.Connection] = weakref.WeakSet()
    real_connect = aiosqlite.connect

    def _tracking_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        conn = real_connect(*args, **kwargs)
        live.add(conn)
        return conn

    aiosqlite.connect = _tracking_connect  # type: ignore[assignment]
    try:
        yield
    finally:
        aiosqlite.connect = real_connect  # type: ignore[assignment]
        leaked = [c for c in live if getattr(c, "_connection", None) is not None]
        for conn in leaked:
            try:
                _hard_stop_connection(conn)
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        if leaked:
            leaked.clear()
            gc.collect()
