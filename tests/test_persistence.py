import asyncio
import contextlib
import json
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

import combomaker.ops.persistence as persistence
from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.persistence import Store
from combomaker.ops.report import build_report
from combomaker.pricing.fit_challenge import FitChallenge, FitVerdict
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import LegRef, OpenPosition

RFQ = Rfq.from_ws(
    {
        "id": "rfq_1",
        "market_ticker": "KXMVE-C1",
        "created_ts": "2026-07-05T10:00:00Z",
        "target_cost_dollars": "50.00",
        "mve_collection_ticker": "KXMVESPORTS",
        "mve_selected_legs": [
            {"market_ticker": "M1", "side": "yes", "event_ticker": "E1"},
        ],
    }
)


async def test_roundtrip(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        await store.record_rfq(RFQ, source="ws")
        await store.record_rfq_deleted("rfq_1", {"id": "rfq_1"})
        await store.record_decision(
            "no_quote", "rfq_1", ["skip_leg_stale", "skip_in_play"], {"k": "v"}
        )
        await store.record_would_quote(
            "rfq_1",
            fair_prob=0.31,
            fair_cc=3_100,
            width_cc=600,
            leg_probs=(0.62, 0.5),
            context={},
        )
        assert await store.count("rfqs") == 1
        assert await store.count("rfq_deletions") == 1
        assert await store.count("decisions") == 1
        assert await store.count("would_quotes") == 1
        reasons = await store.decision_reason_counts()
        assert reasons == {"skip_leg_stale": 1, "skip_in_play": 1}
    finally:
        await store.close()


async def test_record_rfq_seen_at_override_and_default(tmp_path: Path) -> None:
    """rfqs.seen_at semantics (risk audit fix 2026-07-16): the fast-lane
    passes the wall-clock captured at worker PICKUP so the column keeps its
    pre-fast-lane meaning even though the row lands after pricing; the default
    (no override) still stamps call time for every other caller."""
    clock = FakeClock()
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        pickup = clock.now()
        clock.advance(2.0)  # pricing-pool dwell between pickup and the write
        await store.record_rfq(RFQ, source="ws", seen_at=pickup)
        await store.record_rfq(RFQ, source="ws")  # default: call-time stamp
        async with store._db.execute(  # noqa: SLF001
            "SELECT seen_at FROM rfqs ORDER BY id"
        ) as cursor:
            rows = [row[0] async for row in cursor]
        assert rows[0] == pickup.isoformat()          # override: pickup time
        assert rows[1] == clock.now().isoformat()     # default: write time
        assert rows[0] != rows[1]
    finally:
        await store.close()


async def test_open_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite3"
    store1 = await Store.open(path, FakeClock())
    await store1.record_rfq(RFQ, source="ws")
    await store1.close()
    store2 = await Store.open(path, FakeClock())  # DDL re-runs harmlessly
    try:
        assert await store2.count("rfqs") == 1
    finally:
        await store2.close()


# --------------------------------------------------------------------------- #
# WAL CHECKPOINT RESILIENCE (2026-07-18) — the writer loop / _wal_checkpoint.  #
# Live failure: 'database table is locked' on EVERY manual TRUNCATE of a run   #
# (a long-lived read cursor starves the lock); the old shared try/except       #
# logged it as a batch failure and waited another 5000 writes while the WAL    #
# grew 78→194MB. The fix: own failure path + PASSIVE fallback + ~500-write     #
# retry cadence. Simulated with a delegating connection proxy that locks the   #
# TRUNCATE (and optionally PASSIVE) pragma.                                    #
# Since 2026-08-19 checkpoints run on a DEDICATED second connection (the       #
# self-lock fix — on the shared connection its own read cursors made BOTH     #
# pragmas raise), so the proxy wraps ``store._ckpt_db``, not ``store._db``.    #
# Since 2026-09-05 the writer is a THREAD with its own stdlib sqlite3          #
# connection and ``_ckpt_db`` is a stdlib connection too (the thread has no   #
# loop to await on) — the proxies below wrap either shape: their ``execute``  #
# is a plain call returning whatever the wrapped connection returns.          #
# --------------------------------------------------------------------------- #


class _CheckpointLockedDB:
    """Delegating proxy over the DEDICATED checkpoint connection (stdlib
    sqlite3 since 2026-09-05): raises 'database table is locked' on
    ``wal_checkpoint(TRUNCATE)`` (and on PASSIVE too when ``passive_locked``)
    while ``locked`` is True; counts every checkpoint attempt. All other
    traffic passes through to the real connection — the batch INSERT/commit
    path (the writer thread's OWN connection) is untouched, exactly the live
    failure shape."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.locked = True
        self.passive_locked = False
        self.truncate_attempts = 0
        self.passive_attempts = 0

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        # A plain call: returns the wrapped connection's cursor untouched; a
        # locked pragma raises at call time, which the writer's try sees.
        if "wal_checkpoint(TRUNCATE)" in sql:
            self.truncate_attempts += 1
            if self.locked:
                raise sqlite3.OperationalError("database table is locked")
        elif "wal_checkpoint(PASSIVE)" in sql:
            self.passive_attempts += 1
            if self.passive_locked:
                raise sqlite3.OperationalError("database table is locked")
        return self._db.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


async def _flood(store: Store, n: int) -> None:
    """Enqueue n tape writes and wait for the writer THREAD to drain them
    (task_done runs AFTER the checkpoint attempt of the batch, so the flush
    implies any due checkpoint has been attempted). The trailing ``sleep(0)``
    lets the log emits the thread posted to the loop (``_post_to_loop``) run
    before the caller inspects the log spy — they were posted BEFORE the
    task_done that released the flush, so FIFO already orders them first;
    the yield just makes the test independent of that detail."""
    for i in range(n):
        await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
    assert store._write_q is not None  # noqa: SLF001
    assert await store.flush_writer(10.0)
    await asyncio.sleep(0)


async def test_checkpoint_failure_has_own_path_and_fast_retry(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    assert store._ckpt_db is not None  # noqa: SLF001
    proxy = _CheckpointLockedDB(store._ckpt_db)  # noqa: SLF001
    store._ckpt_db = proxy  # type: ignore[assignment]  # noqa: SLF001
    # Tight cadences so the test stays fast (class attrs, instance-overridable).
    store._CHECKPOINT_EVERY_WRITES = 50
    store._CHECKPOINT_RETRY_WRITES = 10
    store.start_writer()
    try:
        # (1) First cadence: the locked TRUNCATE fails ONCE, PASSIVE fallback
        # runs, the failure is counted — and the batch data still committed
        # (the checkpoint no longer shares the batch's fate).
        await _flood(store, 50)
        assert proxy.truncate_attempts == 1
        assert proxy.passive_attempts == 1
        assert store.checkpoint_failures == 1
        assert store.checkpoint_passive_fallbacks == 1
        assert await store.count("decisions") == 50

        # (2) Retry after ~RETRY writes (10), NOT the full cadence (50).
        await _flood(store, 10)
        assert proxy.truncate_attempts == 2
        assert store.checkpoint_failures == 2

        # (3) Lock released: the next retry succeeds and the cadence resets to
        # the full EVERY (50): 10 more writes fire attempt #3 (success)…
        proxy.locked = False
        await _flood(store, 10)
        assert proxy.truncate_attempts == 3
        assert store.checkpoint_failures == 2  # no new failure
        # …and another 10 writes do NOT fire attempt #4 (cadence is 50 again).
        await _flood(store, 10)
        assert proxy.truncate_attempts == 3
        await _flood(store, 40)  # completes the 50-write cadence
        assert proxy.truncate_attempts == 4
    finally:
        await store.close()


async def test_checkpoint_passive_also_locked_survives_and_keeps_tape(
    tmp_path: Path,
) -> None:
    """Adversarial edge: BOTH pragmas locked — the cycle gives up loudly
    (failure counted, no passive fallback recorded), the writer loop survives,
    every batch still commits, and the fast retry cadence still arms."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    assert store._ckpt_db is not None  # noqa: SLF001
    proxy = _CheckpointLockedDB(store._ckpt_db)  # noqa: SLF001
    proxy.passive_locked = True
    store._ckpt_db = proxy  # type: ignore[assignment]  # noqa: SLF001
    store._CHECKPOINT_EVERY_WRITES = 50
    store._CHECKPOINT_RETRY_WRITES = 10
    store.start_writer()
    try:
        await _flood(store, 50)
        assert proxy.truncate_attempts == 1
        assert proxy.passive_attempts == 1
        assert store.checkpoint_failures == 1
        assert store.checkpoint_passive_fallbacks == 0  # fallback failed too
        await _flood(store, 10)  # fast retry still armed
        assert proxy.truncate_attempts == 2
        assert store.checkpoint_failures == 2
        assert await store.count("decisions") == 60  # tape fully durable
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# CHECKPOINT SELF-LOCK FIX (2026-08-19) — dedicated checkpoint connection,     #
# dropped-writes observability, close ordering. Live failure: the ONE shared   #
# connection served writer batches, checkpoints, AND maintenance/settlement    #
# reads; a read cursor open across an await made BOTH pragmas raise            #
# SQLITE_LOCKED — 3,120 consecutive checkpoint failures, a 6GB WAL, and        #
# ~75-80% of a day's tape silently dropped with no reader of the counter.      #
# --------------------------------------------------------------------------- #


class _PragmaSpyDB:
    """Delegating aiosqlite proxy that RECORDS every wal_checkpoint pragma it
    sees (and passes ALL traffic through untouched)."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.checkpoint_sqls: list[str] = []

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if "wal_checkpoint" in sql:
            self.checkpoint_sqls.append(sql)
        return self._db.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class _CloseOrderProxy:
    """Delegating proxy recording WHEN its close() ran (returns whatever the
    real close returns — the main aiosqlite connection's close coroutine, or
    None for the stdlib checkpoint connection — so ``await conn.close()`` and
    ``conn.close()`` both still work)."""

    def __init__(self, db: Any, name: str, order: list[str]) -> None:
        self._db = db
        self._name = name
        self._order = order

    def close(self) -> Any:
        self._order.append(self._name)
        return self._db.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class _LogSpy:
    """Stands in for the module logger: records (level, event, kwargs) and
    the ident of the thread each emit ran on (the writer thread must post
    its emits back to the loop thread — ``_post_to_loop``)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.threads: list[int] = []

    def _record(self, level: str, event: str, kw: dict[str, Any]) -> None:
        self.events.append((level, event, kw))
        self.threads.append(threading.get_ident())

    def info(self, event: str, **kw: Any) -> None:
        self._record("info", event, kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._record("warning", event, kw)

    def error(self, event: str, **kw: Any) -> None:
        self._record("error", event, kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._record("exception", event, kw)

    def of(self, event: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [e for e in self.events if e[1] == event]


async def test_checkpoint_uses_dedicated_connection_never_main(
    tmp_path: Path,
) -> None:
    """A checkpoint attempt while the MAIN connection holds an open read
    cursor neither raises nor touches the main connection: every
    wal_checkpoint pragma travels the DEDICATED connection, where the live
    reader is a busy=1 VERDICT (counted failure), not SQLITE_LOCKED. Once the
    reader closes, the same transport TRUNCATEs cleanly."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        assert store._ckpt_db is not None  # noqa: SLF001
        # Bound the TRUNCATE's reader-wait so the busy verdict is fast (stdlib
        # connection — a plain call).
        store._ckpt_db.execute("PRAGMA busy_timeout=50")  # noqa: SLF001
        main_spy = _PragmaSpyDB(store._db)  # noqa: SLF001
        ckpt_spy = _PragmaSpyDB(store._ckpt_db)  # noqa: SLF001
        store._db = main_spy  # type: ignore[assignment]  # noqa: SLF001
        store._ckpt_db = ckpt_spy  # type: ignore[assignment]  # noqa: SLF001
        # Synchronous writes (no writer task): WAL frames + rows to iterate.
        for i in range(10):
            await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
        # The self-lock shape: a stepped, UNFINISHED cursor on the main
        # connection pins the WAL read-mark across the checkpoint attempt.
        cursor = await store._db.execute(  # noqa: SLF001
            "SELECT reasons_json FROM decisions"
        )
        assert await cursor.fetchone() is not None  # mid-iteration
        assert store._wal_checkpoint() is False  # noqa: SLF001 — busy, NOT a raise
        assert store.checkpoint_failures == 1
        assert store.checkpoint_passive_fallbacks == 1  # PASSIVE still folded
        assert main_spy.checkpoint_sqls == []  # main saw NO checkpoint pragma
        assert len(ckpt_spy.checkpoint_sqls) >= 1
        await cursor.close()
        assert store._wal_checkpoint() is True  # noqa: SLF001 — reader gone
        assert main_spy.checkpoint_sqls == []
    finally:
        await store.close()


async def test_dropped_writes_stats_event_delta_and_levels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """store_writer_stats carries cumulative dropped writes, the delta since
    the last emit, and queue depth — WARNING when tape dropped since the last
    emit, INFO when not. Drop-on-overflow semantics themselves unchanged."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        # Async tape mode with a TINY queue (the writer's own thread-safe
        # queue type) and no writer draining it: 2 of 5 writes fit, 3 drop.
        store._write_q = queue.Queue(maxsize=2)  # noqa: SLF001
        for i in range(5):
            await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
        assert store._dropped_writes == 3  # noqa: SLF001
        store._emit_writer_stats()  # noqa: SLF001
        store._emit_writer_stats()  # noqa: SLF001 — no NEW drops since
        stats = spy.of("store_writer_stats")
        assert [e[0] for e in stats] == ["warning", "info"]
        assert stats[0][2]["dropped_writes_total"] == 3
        assert stats[0][2]["dropped_writes_delta"] == 3
        assert stats[0][2]["queue_depth"] == 2
        assert stats[1][2]["dropped_writes_total"] == 3
        assert stats[1][2]["dropped_writes_delta"] == 0
    finally:
        store._write_q = None  # noqa: SLF001 — writer never ran; nothing to drain
        await store.close()


async def test_writer_loop_emits_stats_on_checkpoint_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer loop emits store_writer_stats on the checkpoint cadence (a
    healthy run: INFO, zero dropped) and the successful checkpoint logs its
    result frames."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    store._CHECKPOINT_EVERY_WRITES = 10
    store.start_writer()
    try:
        await _flood(store, 10)
        stats = spy.of("store_writer_stats")
        assert len(stats) == 1
        assert stats[0][0] == "info"
        assert stats[0][2]["dropped_writes_total"] == 0
        assert stats[0][2]["dropped_writes_delta"] == 0
        assert len(spy.of("store_writer_checkpoint_ok")) == 1
        assert spy.of("store_writer_checkpoint_failed") == []
        # LOGGING STAYS ON THE LOOP (2026-09-05): the writer thread posts its
        # emits back via _post_to_loop — every event ran on THIS thread.
        assert spy.threads and set(spy.threads) == {threading.get_ident()}
    finally:
        await store.close()


async def test_close_closes_checkpoint_connection_before_main(
    tmp_path: Path,
) -> None:
    """Store.close closes the DEDICATED checkpoint connection FIRST, so the
    main connection is the LAST one open and its close-time WAL fold isn't
    blocked by a second live connection."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    order: list[str] = []
    assert store._ckpt_db is not None  # noqa: SLF001
    store._db = _CloseOrderProxy(  # type: ignore[assignment]  # noqa: SLF001
        store._db, "main", order  # noqa: SLF001
    )
    store._ckpt_db = _CloseOrderProxy(  # type: ignore[assignment]  # noqa: SLF001
        store._ckpt_db, "ckpt", order  # noqa: SLF001
    )
    await store.close()
    assert order == ["ckpt", "main"]


# --------------------------------------------------------------------------- #
# TAPE WRITER OFF THE EVENT LOOP (2026-09-05) — a THREAD with its own sqlite3   #
# connection, executemany batches in ONE transaction, O(1) never-yielding hot  #
# path. Live defect: the asyncio-task writer paid one loop hop PER ROW (1,000  #
# per batch) on a loop at p50 lag 67-134 ms / p99 0.4-1.0 s — the queue       #
# pinned at 200k within 10 min of a FRESH 316 MB store and dropped 126,492     #
# rows in the next 10 min (store_writer_stats), competing with quoting for the #
# loop the whole time. Blast radius: the persistence TAPE path only.           #
# --------------------------------------------------------------------------- #


_DECISION_SQL = (
    "INSERT INTO decisions (at, kind, rfq_id, reasons_json, context_json)"
    " VALUES (?, ?, ?, ?, ?)"
)
_DELETION_SQL = "INSERT INTO rfq_deletions (rfq_id, seen_at, raw_json) VALUES (?, ?, ?)"
_BAD_ROW: tuple[str, tuple[Any, ...]] = ("INSERT INTO no_such_table (x) VALUES (?)", (1,))


def _row(rfq_id: str) -> tuple[str, tuple[Any, ...]]:
    return (_DECISION_SQL, ("2026-09-05T00:00:00", "no_quote", rfq_id, "[]", "{}"))


def _run_thread_main_on(store: Store, items: list[Any]) -> None:
    """Drive the REAL thread body synchronously over a PRE-FILLED queue — the
    one deterministic way to control batch membership (a live thread grabs
    whatever is queued the instant it wakes). The stop sentinel ends it; the
    body closes the connection it was handed on exit."""
    q: queue.Queue[Any] = queue.Queue()
    for item in items:
        q.put_nowait(item)
    q.put_nowait(persistence._WRITER_STOP)  # noqa: SLF001
    assert store._path is not None  # noqa: SLF001
    wdb = Store._open_writer_connection(store._path)  # noqa: SLF001
    store._writer_thread_main(wdb, q)  # noqa: SLF001
    assert q.unfinished_tasks == 0  # every row AND the sentinel task_done'd
    with pytest.raises(sqlite3.ProgrammingError):  # closed its connection on exit
        wdb.execute("SELECT 1")


def test_writer_bounds_are_the_existing_numbers() -> None:
    """The bounds are NAMED, not new (north star: no new hand-set numbers):
    200,000 = the asyncio.Queue maxsize the writer carried since 2026-07-14;
    1,000 = the per-transaction batch it always committed; 2.0 s = close()'s
    drain bound since the writer existed."""
    assert persistence.WRITER_QUEUE_MAXSIZE == 200000
    assert persistence.WRITER_BATCH_ROWS == 1000
    assert persistence.WRITER_CLOSE_DRAIN_S == 2.0


async def test_thread_writer_drains_rows_readable_on_main_connection(
    tmp_path: Path,
) -> None:
    """The writer is a real daemon THREAD (not a task) with its own connection;
    rows it commits are visible to the MAIN aiosqlite connection's readers (WAL
    visibility across connections) in enqueue order. 2,500 rows = two full
    1,000-row batches + a partial one."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        store.start_writer()
        thread = store._writer_thread  # noqa: SLF001
        assert isinstance(thread, threading.Thread)
        assert thread.is_alive() and thread.daemon
        assert thread is not threading.current_thread()
        assert isinstance(store._write_q, queue.Queue)  # noqa: SLF001
        assert store._write_q.maxsize == persistence.WRITER_QUEUE_MAXSIZE  # noqa: SLF001
        assert store.start_writer() is None  # idempotent: no second thread
        assert store._writer_thread is thread  # noqa: SLF001
        await _flood(store, 2500)
        assert await store.count("decisions") == 2500
        async with store._db.execute(  # noqa: SLF001
            "SELECT rfq_id FROM decisions ORDER BY id"
        ) as cursor:
            ids = [row[0] async for row in cursor]
        assert ids == [f"r{i}" for i in range(2500)]
    finally:
        await store.close()
    assert not thread.is_alive()
    assert store._writer_thread is None  # noqa: SLF001


async def test_batch_is_atomic_failing_row_fails_its_batch_loudly_later_batches_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One batch = ONE transaction: a single bad row rolls back every row of
    its batch (3 good + bad + 3 good → 0 rows), the failure is the loud
    ``store_writer_batch_failed`` (n = the batch size, the exception attached
    as exc_info so the traceback renders on the loop), and the next batch
    commits normally. The thread never dies of a batch failure."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        _run_thread_main_on(
            store,
            [_row("a"), _row("b"), _row("c"), _BAD_ROW, _row("d"), _row("e"), _row("f")],
        )
        assert await store.count("decisions") == 0  # nothing of the failed batch
        failed = spy.of("store_writer_batch_failed")
        assert len(failed) == 1
        assert failed[0][0] == "exception"
        assert failed[0][2]["n"] == 7
        assert isinstance(failed[0][2]["exc_info"], sqlite3.OperationalError)
        _run_thread_main_on(store, [_row("g"), _row("h")])  # a later batch
        assert await store.count("decisions") == 2
        assert len(spy.of("store_writer_batch_failed")) == 1
        assert spy.of("store_writer_thread_died") == []
    finally:
        await store.close()


async def test_batches_group_by_sql_text_and_keep_per_table_order(
    tmp_path: Path,
) -> None:
    """Rows of two tape tables interleaved in one batch land in both tables
    (one executemany per SQL text, one commit), each table in enqueue order."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        items: list[tuple[str, tuple[Any, ...]]] = []
        for i in range(10):
            items.append(_row(f"d{i}"))
            items.append((_DELETION_SQL, (f"x{i}", "2026-09-05T00:00:00", "{}")))
        _run_thread_main_on(store, items)
        assert await store.count("decisions") == 10
        assert await store.count("rfq_deletions") == 10
        async with store._db.execute(  # noqa: SLF001
            "SELECT rfq_id FROM decisions ORDER BY id"
        ) as cursor:
            assert [r[0] async for r in cursor] == [f"d{i}" for i in range(10)]
        async with store._db.execute(  # noqa: SLF001
            "SELECT rfq_id FROM rfq_deletions ORDER BY id"
        ) as cursor:
            assert [r[0] async for r in cursor] == [f"x{i}" for i in range(10)]
    finally:
        await store.close()


async def test_live_queue_bound_drops_on_overflow_and_conserves_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue bound + drop accounting against the LIVE thread: the writer's
    first commit is pinned behind a third connection's write lock (its
    busy_timeout wait) while the hot path over-fills the queue past the bound
    — never blocking, every overflow counted — then the lock lifts and the
    thread drains. Conservation: rows committed == rows enqueued − rows
    dropped, and the first store_writer_stats emit is the WARNING carrying
    exactly those drops. The bound is shrunk for the test's speed (the real
    bound is pinned by test_writer_bounds_are_the_existing_numbers)."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    monkeypatch.setattr(persistence, "WRITER_QUEUE_MAXSIZE", 50)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        store.start_writer()
        assert store._write_q is not None  # noqa: SLF001
        assert store._write_q.maxsize == 50  # noqa: SLF001
        # Enough rows that at least one MUST drop: the bound plus the most
        # the thread can hold in flight (one batch) plus one.
        total = 50 + persistence.WRITER_BATCH_ROWS + 1
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")  # hold the WAL write lock
            for i in range(total):
                await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
            dropped = store._dropped_writes  # noqa: SLF001
            assert dropped >= 1
            assert store.writer_queue_depth() <= 50
            blocker.execute("ROLLBACK")
        finally:
            blocker.close()
        assert await store.flush_writer(10.0)
        await asyncio.sleep(0)
        assert await store.count("decisions") == total - dropped
        assert store._dropped_writes == dropped  # noqa: SLF001 — none after the lift
        assert spy.of("store_writer_batch_failed") == []  # the lock wait was absorbed
    finally:
        await store.close()


async def test_stats_warning_carries_the_drops_on_the_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drops that happen before a cadence emit surface in THAT emit as a
    WARNING with the exact delta (the 2026-08-19 visibility rule, now with
    the emit posted from the thread to the loop)."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    monkeypatch.setattr(persistence, "WRITER_QUEUE_MAXSIZE", 5)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    # Cadence 1: the very first committed batch (the one pinned behind the
    # lock while the drops happened) fires the checkpoint + stats emit.
    store._CHECKPOINT_EVERY_WRITES = 1
    try:
        store.start_writer()
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            for i in range(5 + persistence.WRITER_BATCH_ROWS + 1):
                await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
            dropped = store._dropped_writes  # noqa: SLF001
            assert dropped >= 1
            blocker.execute("ROLLBACK")
        finally:
            blocker.close()
        assert await store.flush_writer(10.0)
        await asyncio.sleep(0)
        stats = spy.of("store_writer_stats")
        assert stats
        assert stats[0][0] == "warning"
        assert stats[0][2]["dropped_writes_total"] == dropped
        assert stats[0][2]["dropped_writes_delta"] == dropped
        assert set(spy.threads) == {threading.get_ident()}  # emitted on the loop
    finally:
        await store.close()


async def test_thread_checkpoint_busy_verdict_passive_fallback_on_pinned_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL checkpoint path from the writer thread against a REAL pinned
    reader (a stepped, unfinished cursor on the main connection): TRUNCATE
    returns the busy verdict (no raise), PASSIVE still folds, the failure is
    counted + logged with the fast retry cadence, the retry fires after
    _CHECKPOINT_RETRY_WRITES, and once the reader closes the TRUNCATE
    succeeds — all while every batch stays durable."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        assert store._ckpt_db is not None  # noqa: SLF001
        store._ckpt_db.execute("PRAGMA busy_timeout=50")  # noqa: SLF001 — fast verdict
        store._CHECKPOINT_EVERY_WRITES = 10
        store._CHECKPOINT_RETRY_WRITES = 5
        store.start_writer()
        await _flood(store, 5)  # WAL frames + rows to pin; cadence not yet due
        assert store.checkpoint_failures == 0
        cursor = await store._db.execute(  # noqa: SLF001
            "SELECT reasons_json FROM decisions"
        )
        assert await cursor.fetchone() is not None  # mid-iteration: read-mark pinned
        await _flood(store, 5)  # cadence 10 → TRUNCATE busy → PASSIVE fallback
        assert store.checkpoint_failures == 1
        assert store.checkpoint_passive_fallbacks == 1
        failed = spy.of("store_writer_checkpoint_failed")
        assert len(failed) == 1
        assert failed[0][2]["error"].startswith("busy")
        assert failed[0][2]["passive_fallback_ok"] is True
        assert failed[0][2]["retry_after_writes"] == 5
        await _flood(store, 5)  # fast retry after 5 → still pinned → failure #2
        assert store.checkpoint_failures == 2
        await cursor.close()
        await _flood(store, 5)  # retry → reader gone → TRUNCATE completes
        assert store.checkpoint_failures == 2
        assert len(spy.of("store_writer_checkpoint_ok")) == 1
        await _flood(store, 5)  # cadence back to the full 10: no attempt yet
        assert len(spy.of("store_writer_checkpoint_ok")) == 1
        await _flood(store, 5)
        assert len(spy.of("store_writer_checkpoint_ok")) == 2
        assert await store.count("decisions") == 30  # tape durable throughout
        assert set(spy.threads) == {threading.get_ident()}  # every emit on the loop
    finally:
        await store.close()


async def test_close_flushes_pending_rows_and_joins_thread(tmp_path: Path) -> None:
    """close() with 3,000 rows still queued (no explicit flush): the bounded
    drain commits them, the thread exits (its connection closed by itself),
    and a fresh Store on the same file reads all 3,000 — durability at
    shutdown."""
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    store.start_writer()
    thread = store._writer_thread  # noqa: SLF001
    wdb = store._writer_db  # noqa: SLF001
    assert thread is not None and wdb is not None
    for i in range(3000):
        await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
    await store.close()
    assert not thread.is_alive()
    assert store._writer_thread is None  # noqa: SLF001
    with pytest.raises(sqlite3.ProgrammingError):  # the thread closed its connection
        wdb.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):  # ckpt connection closed by close()
        assert store._ckpt_db is not None  # noqa: SLF001
        store._ckpt_db.execute("SELECT 1")  # noqa: SLF001
    reopened = await Store.open(path, FakeClock())
    try:
        assert await reopened.count("decisions") == 3000
    finally:
        await reopened.close()


async def test_close_with_idle_writer_and_empty_queue_is_prompt(tmp_path: Path) -> None:
    """An idle thread blocked in queue.get() wakes on the stop sentinel — no
    poll timeout, no join bound consumed."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    store.start_writer()
    thread = store._writer_thread  # noqa: SLF001
    assert thread is not None
    t0 = asyncio.get_running_loop().time()
    await store.close()
    assert asyncio.get_running_loop().time() - t0 < persistence.STORE_OP_TIMEOUT_S
    assert not thread.is_alive()


async def test_sync_mode_unchanged_without_start_writer(tmp_path: Path) -> None:
    """No start_writer → no queue, no thread; _write commits immediately and
    read-after-write holds with no flush; flush_writer is a no-op True."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        assert store._write_q is None  # noqa: SLF001
        assert store._writer_thread is None  # noqa: SLF001
        await store.record_decision("no_quote", "r0", ["skip_test"], {})
        assert await store.count("decisions") == 1
        assert await store.flush_writer(0.0) is True
        assert store.writer_queue_depth() == 0
        assert not any(t.name == "store-writer" for t in threading.enumerate())
    finally:
        await store.close()


async def test_hot_path_write_never_yields_to_the_loop(tmp_path: Path) -> None:
    """PROPERTY: 10,000 awaited tape writes complete without the event loop
    running ANY other callback — a spinner task counting loop turns records
    zero turns across the whole burst. (The old put_nowait never yielded
    either; what this build changed is that DRAINING those rows now costs the
    loop zero iterations too — measured in tools/diagnostics/bench_tape_writer.py.)"""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        store.start_writer()
        turns = 0

        async def spin() -> None:
            nonlocal turns
            while True:
                turns += 1
                await asyncio.sleep(0)

        spinner = asyncio.create_task(spin())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        before = turns
        assert before > 0  # the spinner is live
        for i in range(10_000):
            await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
        assert turns == before  # not ONE loop turn during 10k tape writes
        spinner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await spinner
        assert await store.flush_writer(10.0)
        assert await store.count("decisions") == 10_000
    finally:
        await store.close()


class _FakeLoop:
    def __init__(self) -> None:
        self.closed = False
        self.posted: list[Any] = []

    def is_closed(self) -> bool:
        return self.closed

    def call_soon_threadsafe(self, fn: Any, *args: Any) -> None:
        if self.closed:
            raise RuntimeError("Event loop is closed")
        self.posted.append(fn)


async def test_post_to_loop_routes_to_the_loop_and_never_loses_an_emit(
    tmp_path: Path,
) -> None:
    """Emits from the thread go through loop.call_soon_threadsafe (run later
    ON the loop); with the loop closed (a leaked store at test teardown) or
    no loop recorded (a direct checkpoint call) they run inline instead of
    being lost."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        ran: list[str] = []
        fake = _FakeLoop()
        store._writer_loop_ref = fake  # type: ignore[assignment]  # noqa: SLF001
        store._post_to_loop(lambda: ran.append("a"))  # noqa: SLF001
        assert ran == [] and len(fake.posted) == 1
        fake.posted[0]()
        assert ran == ["a"]
        fake.closed = True
        store._post_to_loop(lambda: ran.append("b"))  # noqa: SLF001
        assert ran == ["a", "b"]
        store._writer_loop_ref = None  # noqa: SLF001
        store._post_to_loop(lambda: ran.append("c"))  # noqa: SLF001
        assert ran == ["a", "b", "c"]
    finally:
        await store.close()


async def test_legacy_direct_construction_cannot_start_writer_and_checkpoint_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one direct construction in the tree (tools/diagnostics/
    restart_gate2_quote_validation.py: ``Store(db, clock)``, read-only) has no
    path and no checkpoint connection: start_writer refuses with a clear
    error (never a silent no-op) and a checkpoint attempt takes the counted,
    logged failure path with no passive fallback — never a crash."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    db = await aiosqlite.connect(tmp_path / "t.sqlite3")
    store = Store(db, FakeClock())
    try:
        with pytest.raises(RuntimeError, match="Store.open"):
            store.start_writer()
        assert store._writer_thread is None  # noqa: SLF001
        assert store._write_q is None  # noqa: SLF001
        assert store._wal_checkpoint() is False  # noqa: SLF001
        assert store.checkpoint_failures == 1
        assert store.checkpoint_passive_fallbacks == 0
        failed = spy.of("store_writer_checkpoint_failed")
        assert len(failed) == 1
        assert failed[0][2]["passive_fallback_ok"] is False
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# THE SHARED CONNECTION UNDER A FOREIGN COMMITTER (2026-09-05 review fixes).   #
# With the tape writer committing on ITS OWN connection, the shared aiosqlite  #
# connection has a foreign committer on its WAL: any read statement left       #
# ACTIVE across a loop hop while another coroutine writes → SQLITE_BUSY_       #
# SNAPSHOT ('database is locked', instantly) + a WEDGED connection (Python's   #
# implicit BEGIN keeps the stale snapshot for the rest of the run). Review     #
# probe on the real Store: 40/40 record_fill raised during the report pager;  #
# main-connection count(decisions) 300,000 vs 305,000 truth. Fixes: ONE        #
# connection lock around every statement lifecycle (_fetchall / _fetchone /   #
# _ledger_txn), rollback + one retry on a lock-class failure, the report's     #
# unbounded pager moved to its own read-only connection, and the checkpoint    #
# connection that never waits.                                                 #
# --------------------------------------------------------------------------- #


def _position(ticker: str = "KXMVE-T1") -> OpenPosition:
    return OpenPosition(
        position_id=f"fill:{ticker}",
        combo_ticker=ticker,
        collection="KXMVESPORTS",
        our_side=Side.NO,
        contracts=CentiContracts(500),
        entry_price_cc=CentiCents(6200),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
    )


async def _fill(store: Store, k: str, *, ticker: str = "KXMVE-T1") -> bool:
    return await store.record_fill(
        f"fill:{k}",
        order_id=f"o{k}",
        combo_ticker=ticker,
        our_side="no",
        contracts_centi=100,
        price_cc=5000,
        fee_cc=1,
        expected_edge_cc=10,
        raw={},
    )


def _truth(path: Path, sql: str) -> int:
    """A FRESH read-only connection's answer — the file's truth, immune to any
    stale snapshot the shared connection might be pinned on."""
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return int(con.execute(sql).fetchone()[0])
    finally:
        con.close()


def _foreign_commit(path: Path) -> None:
    """One committed row from ANOTHER connection — the tape thread's role:
    advances the WAL past whatever snapshot the shared connection holds."""
    other = sqlite3.connect(path)
    try:
        other.execute(
            "INSERT INTO decisions (at, kind, rfq_id, reasons_json, context_json)"
            " VALUES ('t', 'no_quote', 'foreign', '[]', '{}')"
        )
        other.commit()
    finally:
        other.close()


class _LockWitnessDB:
    """Delegating aiosqlite proxy over the SHARED connection recording, for
    every execute / commit / rollback, whether the store's connection lock
    was HELD at call time (all other traffic passes through untouched)."""

    def __init__(self, db: Any, lock: asyncio.Lock) -> None:
        self._db = db
        self._lock = lock
        self.calls: list[tuple[str, bool]] = []

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((sql, self._lock.locked()))
        return self._db.execute(sql, *args, **kwargs)

    def commit(self) -> Any:
        self.calls.append(("COMMIT", self._lock.locked()))
        return self._db.commit()

    def rollback(self) -> Any:
        self.calls.append(("ROLLBACK", self._lock.locked()))
        return self._db.rollback()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


async def test_ledger_writes_survive_shared_connection_reads_under_the_tape_thread(
    tmp_path: Path,
) -> None:
    """THE REVIEW PROBE as a regression test (must-fix #1 (c)). The writer
    thread commits tape on ITS connection (a foreign committer on the shared
    connection's WAL) while, on the loop, a tape firehose, a reader task
    issuing bounded shared-connection reads (count / has_fill /
    open_ledger_tickers — each a multi-hop statement lifecycle), the report's
    ``decision_reason_counts`` and 10 ``record_fill`` calls all interleave.
    The firehose keeps the loop CPU-BOUND (a ``json.loads`` burn per turn, the
    intake's own work) at roughly the live tape rate — the shape that also
    caught the writer thread's GIL starvation (an ``executemany`` batch held
    the write lock for seconds; ``record_fill`` failed SQLITE_BUSY). On the
    unfixed branch: every fill raised 'database is locked' and the connection
    stayed wedged. Now: every fill books, the connection is never left inside
    a transaction, the lock alone prevents the snapshot collision (no rollback
    was even needed), and main-connection reads equal a fresh connection's
    truth."""
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        store.start_writer()
        await _flood(store, 2000)  # rows for the report scan to count
        stop = False
        reads = 0
        frame = '{"type": "rfq_created", "msg": {"id": "r1", "legs": [{"t": "M1", "s": "yes"}]}}'

        async def firehose() -> None:
            i = 0
            while not stop:
                for _ in range(10):
                    await store.record_decision("no_quote", f"f{i}", ["skip_test", "a"], {})
                    i += 1
                for _ in range(400):  # ~2 ms of the intake's own CPU work per turn
                    json.loads(frame)
                await asyncio.sleep(0)

        async def reader() -> None:
            nonlocal reads
            while not stop:
                await store.count("fills")  # a LEDGER table: stays on the shared connection
                await store.open_ledger_tickers()
                reads += 2
                await asyncio.sleep(0)

        tasks = [asyncio.create_task(firehose()), asyncio.create_task(reader())]
        scan = asyncio.create_task(store.decision_reason_counts())
        booked = 0
        for k in range(10):
            await asyncio.sleep(0.005)
            booked += int(await _fill(store, str(k)))
        stop = True
        await asyncio.gather(*tasks)
        counts = await scan
        assert booked == 10
        assert reads > 0
        assert counts["skip_test"] >= 2000
        assert store._db.in_transaction is False  # noqa: SLF001 — never wedged
        assert store.ledger_txn_rollbacks == 0  # the LOCK prevented every collision
        assert store.ledger_txn_retries == 0
        assert await store.flush_writer(10.0)
        assert await store.count("fills") == 10 == _truth(path, "SELECT COUNT(*) FROM fills")
        # Main-connection reads are FRESH (a wedged connection would be pinned
        # on the snapshot the first failure took).
        assert await store.count("decisions") == _truth(path, "SELECT COUNT(*) FROM decisions")
    finally:
        await store.close()


async def test_every_shared_connection_statement_runs_under_the_connection_lock(
    tmp_path: Path,
) -> None:
    """STRUCTURAL: every execute / commit / rollback this class issues on the
    shared connection after open happens with the connection lock HELD —
    reads (``_fetchall`` / ``_fetchone``) and transactions (``_ledger_txn``)
    alike — across the whole public surface. ``decision_reason_counts`` issues
    NOTHING on the shared connection (its own read-only connection)."""
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        witness = _LockWitnessDB(store._db, store._conn_lock)  # noqa: SLF001
        store._db = witness  # type: ignore[assignment]  # noqa: SLF001
        # Tape (sync mode) + shadow + fits-adjacent writes.
        await store.record_rfq(RFQ, source="ws")
        await store.record_rfq_deleted("rfq_1", {})
        await store.record_decision("no_quote", "rfq_1", ["skip_test"], {})
        await store.record_would_quote(
            "rfq_1", fair_prob=0.5, fair_cc=5000, width_cc=100, leg_probs=(0.5,), context={}
        )
        await store.record_would_quote_inplay(
            "rfq_1",
            market_ticker="KXMVE-C1",
            fair_cc=5000,
            yes_bid_cc=4900,
            no_bid_cc=4900,
            target_cost_cc=None,
            contracts_centi=100,
            leg_time_to_start_s={"M1": -10.0},
            context={},
        )
        await store.record_structural_fit(
            rfq_id="rfq_1",
            model="mutex",
            n_legs=2,
            tickers=("M1", "M2"),
            challenge=FitChallenge(
                verdict=FitVerdict.ACCEPT,
                residual=0.001,
                exactly_identified=True,
                reject_bar=0.05,
                challenge_bar=0.01,
            ),
            family="ml_parlay",
            route="structural",
        )
        # Ledger.
        await store.record_position_open(_position(), subaccount="0")
        assert await store.ensure_open_position_row(_position(), subaccount="0") is False
        assert await store.ensure_open_position_row(_position("KXMVE-T2"), subaccount="0")
        assert await _fill(store, "a") is True
        assert await _fill(store, "b") is True
        assert await store.has_fill("fill:a") is True
        assert await store.has_fill_for_order_id("oa") is True
        assert await store.fill_ref_for_order_id("oa") == ("fill:a", "booked")
        assert await store.fill_status("fill:a") == "booked"
        assert await store.has_fill_for_ticker("KXMVE-T1") is True
        assert await store.mark_fill_verified("fill:a", exchange_fill_id="x") is True
        assert (await store.void_phantom_fill("fill:b", reason="r"))["fills"] == 1
        assert await store.fills_verification_watermark() is not None
        assert await store.booked_unverified_fills(after_id=0) == []
        assert await store.fill_order_ids() == {"oa"}
        assert await store.fill_null_order_id_keys() == set()
        await store.record_markout(
            "fill:a",
            horizon_s=60.0,
            fair_at_fill_cc=5000,
            fair_now_cc=5100,
            raw_mid_at_fill_cc=None,
            raw_mid_now_cc=None,
        )
        assert (await store.markout_summary())[0]["n"] == 1
        assert await store.record_combo_trades("KXMVE-T1", [{"trade_id": "t1"}]) == 1
        await store.settle_ev_entry("fill:a", 5)
        assert (await store.ev_summary())["fills"] == 1
        settled = await store.record_position_settled(
            "fill:KXMVE-T1", settled_value=0.0, realized_pnl_cc=100, settlement_fee_cc=1
        )
        assert settled == "fill:KXMVE-T1"
        assert await store.open_ledger_rows_for_ticker("KXMVE-T2")
        assert await store.open_ledger_tickers() == {"KXMVE-T2"}
        assert len(await store.open_ledger_identities()) == 1
        assert (await store.ledger_position("fill:KXMVE-T1"))["status"] == "settled"
        assert "KXMVE-T2" in await store.open_ledger_quantity_by_ticker()
        assert await store.day_realized_pnl_cc("2000", "2100") == 100 - 1
        assert await store.settled_grade_rows()
        assert await store.held_positions(["KXMVE-T1"])
        assert await store.count("fills") == 2
        assert await store.count("structural_fits") == 1
        n_calls = len(witness.calls)
        # Every TAPE read runs on its own read-only connection (review #2,
        # should-fix 1): not ONE statement on the shared connection.
        assert await store.decision_kind_counts() == {"no_quote": 1}
        assert await store.decision_reason_counts() == {"skip_test": 1}
        assert await store.count("decisions") == 1
        assert await store.count("rfqs") == 1
        assert (await store.report_tape_counts())["would_quotes"] == 1
        assert len(witness.calls) == n_calls

        assert len(witness.calls) >= 50
        unlocked = [sql for sql, held in witness.calls if not held]
        assert unlocked == []
        touched = " ".join(sql for sql, _ in witness.calls)
        for table in (
            "rfqs",
            "rfq_deletions",
            "decisions",
            "would_quotes",
            "would_quotes_inplay",
            "position_ledger",
            "fills",
            "ev_ledger",
            "markouts",
            "combo_trades",
            "store_meta",
            "structural_fits",
        ):
            assert table in touched, table
        assert ("COMMIT", True) in witness.calls
        assert store._db.in_transaction is False  # noqa: SLF001
    finally:
        await store.close()


async def test_ledger_txn_never_leaves_the_connection_wedged_by_a_leaked_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The residue the lock cannot see (must-fix #1 (b)): a read cursor left
    ACTIVE on the shared connection OUTSIDE any lock (what a cancelled
    ``_bounded_store`` read leaves behind), then a foreign commit. The next
    ledger write hits SQLITE_BUSY_SNAPSHOT — instantly, busy_timeout not
    consulted. Unfixed, Python's implicit BEGIN kept that stale snapshot for
    the REST OF THE RUN: every later write failed, every later read was
    stale (the review probe). Now ``_ledger_txn`` rolls back (counted +
    logged with the sqlite error name) and retries once; while the leaked
    cursor still lives the retry fails the same way — SQLite keeps the
    connection's read snapshot pinned for an active statement — and the
    caller sees the exception, but the connection is left OUTSIDE any
    transaction, so the moment the cursor closes the very next write books
    with no intervention and reads are fresh. The wedge is bounded by the
    leaked statement's lifetime, never the run's."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        for i in range(5):
            await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
        leaked = await store._db.execute(  # noqa: SLF001 — outside the lock, on purpose
            "SELECT reasons_json FROM decisions"
        )
        assert await leaked.fetchone() is not None  # mid-iteration: snapshot pinned
        _foreign_commit(path)
        t0 = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await _fill(store, "a")
        elapsed = time.perf_counter() - t0
        assert elapsed < persistence.STORE_OP_TIMEOUT_S / 10  # instant: no busy wait
        assert store._db.in_transaction is False  # noqa: SLF001 — NOT left in the stale transaction
        assert store.ledger_txn_rollbacks == 2  # both attempts rolled back
        assert store.ledger_txn_retries == 1
        rolled = spy.of("store_ledger_txn_rolled_back")
        assert len(rolled) == 2
        assert all(e[0] == "warning" for e in rolled)
        assert all(e[2]["lock_error"] is True for e in rolled)
        assert all(e[2]["sqlite_errorname"] == "SQLITE_BUSY_SNAPSHOT" for e in rolled)
        retried = spy.of("store_ledger_txn_retry_after_rollback")
        assert len(retried) == 1
        assert retried[0][2]["sqlite_errorname"] == "SQLITE_BUSY_SNAPSHOT"
        # The leaked statement dies (its coroutine's frame would): the wedge ends with it.
        await leaked.close()
        assert await _fill(store, "a") is True  # the very next write books — no intervention
        assert store.ledger_txn_rollbacks == 2  # no further rollback was needed
        assert store._db.in_transaction is False  # noqa: SLF001
        assert await store.count("fills") == 1 == _truth(path, "SELECT COUNT(*) FROM fills")
        assert await store.count("decisions") == 6  # FRESH: sees the foreign row
        assert spy.of("store_ledger_txn_left_open") == []
    finally:
        await store.close()


class _StmtSpyConnection:
    """Delegating proxy over the writer's stdlib connection: records every
    execute / executemany (text, parameter count) and lets the test pin the
    bound-variable limit the batch writer derives its chunking from."""

    def __init__(self, con: sqlite3.Connection, *, variable_limit: int) -> None:
        self._con = con
        self._variable_limit = variable_limit
        self.executes: list[tuple[str, int]] = []
        self.executemanys: list[tuple[str, int]] = []

    def getlimit(self, category: int) -> int:
        assert category == sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
        return self._variable_limit

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.executes.append((sql, len(params)))
        return self._con.execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> Any:
        rows = list(rows)
        self.executemanys.append((sql, len(rows)))
        return self._con.executemany(sql, rows)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


async def test_commit_batch_writes_multi_row_statements_within_the_engine_variable_limit(
    tmp_path: Path,
) -> None:
    """GIL-starvation fix (review fix pass): a batch group is written as
    multi-row ``INSERT … VALUES (…), (…)`` statements — ONE sqlite3_step per
    chunk instead of one per row — chunked by the engine's own bound-variable
    limit (here pinned to 12 → 2 rows of 5 parameters per statement: 5 rows →
    3 statements of 2, 2, 1), rows land in enqueue order, the real limit fits
    a whole 1,000-row batch in one statement, and a SQL text without a
    ``VALUES (?, …)`` tail still goes through ``executemany``."""
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        wdb = Store._open_writer_connection(path)  # noqa: SLF001
        try:
            spy = _StmtSpyConnection(wdb, variable_limit=12)
            Store._commit_batch(spy, [_row(f"r{i}") for i in range(5)])  # type: ignore[arg-type]  # noqa: SLF001
            assert [n for _, n in spy.executes] == [10, 10, 5]  # 2 + 2 + 1 rows × 5 params
            assert all(sql.count("(?, ?, ?, ?, ?)") == n // 5 for sql, n in spy.executes)
            assert spy.executemanys == []
            assert await store.count("decisions") == 5
            async with store._db.execute("SELECT rfq_id FROM decisions ORDER BY id") as cur:  # noqa: SLF001
                assert [r[0] async for r in cur] == [f"r{i}" for i in range(5)]
            # The engine's real limit: a whole batch of the widest tape table is ONE statement.
            real_limit = wdb.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
            widest = max(
                sql.rpartition(" VALUES ")[2].count("?")
                for sql in (_DECISION_SQL, _DELETION_SQL)
            )
            assert real_limit // 13 >= persistence.WRITER_BATCH_ROWS  # structural_fits: 13 columns
            assert real_limit // widest >= persistence.WRITER_BATCH_ROWS
            spy2 = _StmtSpyConnection(wdb, variable_limit=real_limit)
            Store._commit_batch(  # type: ignore[arg-type]  # noqa: SLF001
                spy2, [_row(f"s{i}") for i in range(persistence.WRITER_BATCH_ROWS)]
            )
            assert [n for _, n in spy2.executes] == [5 * persistence.WRITER_BATCH_ROWS]
            assert await store.count("decisions") == 5 + persistence.WRITER_BATCH_ROWS
            # No VALUES tail → executemany fallback (the group still commits).
            spy3 = _StmtSpyConnection(wdb, variable_limit=real_limit)
            odd = "INSERT INTO rfq_deletions (rfq_id, seen_at, raw_json) SELECT ?, ?, ?"
            Store._commit_batch(spy3, [(odd, ("x", "t", "{}")), (odd, ("y", "t", "{}"))])  # type: ignore[arg-type]  # noqa: SLF001
            assert spy3.executes == []
            assert spy3.executemanys == [(odd, 2)]
            assert await store.count("rfq_deletions") == 2
        finally:
            wdb.close()
    finally:
        await store.close()


async def test_ledger_txn_rolls_back_any_failed_body_and_commits_a_left_open_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that raises a NON-lock error inside its transaction is rolled
    back (nothing of it persists, the connection leaves the transaction) and
    the exception is re-raised unchanged — no retry (counted 0). A body that
    RETURNS without committing is a bug: committed, logged at ERROR
    (``store_ledger_txn_left_open``), never a wedge."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    store = await Store.open(tmp_path / "t.sqlite3", FakeClock())
    try:
        ins = "INSERT INTO rfq_deletions (rfq_id, seen_at, raw_json) VALUES (?, 't', '{}')"

        async def bad() -> None:
            await store._db.execute(ins, ("d",))  # noqa: SLF001
            assert store._db.in_transaction is True  # noqa: SLF001
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await store._ledger_txn(bad)  # noqa: SLF001
        assert store._db.in_transaction is False  # noqa: SLF001
        assert await store.count("rfq_deletions") == 0
        assert store.ledger_txn_rollbacks == 1
        assert store.ledger_txn_retries == 0
        rolled = spy.of("store_ledger_txn_rolled_back")
        assert len(rolled) == 1
        assert rolled[0][2]["lock_error"] is False
        assert spy.of("store_ledger_txn_retry_after_rollback") == []

        async def forgot() -> str:
            await store._db.execute(ins, ("e",))  # noqa: SLF001
            return "done"

        assert await store._ledger_txn(forgot) == "done"  # noqa: SLF001
        assert store._db.in_transaction is False  # noqa: SLF001
        assert await store.count("rfq_deletions") == 1
        left = spy.of("store_ledger_txn_left_open")
        assert len(left) == 1
        assert left[0][0] == "error"
        assert store.ledger_txn_rollbacks == 1  # a left-open body is committed, not rolled back
    finally:
        await store.close()


def _start_thread_body(
    store: Store, path: Path, rows: list[Any], *, busy_timeout_ms: int
) -> tuple[threading.Thread, queue.Queue[Any]]:
    """Run the REAL thread body on a real thread over a pre-filled queue, with
    the writer connection's OWN busy wait squeezed (the SAME mechanism, a
    smaller number so each attempt costs milliseconds)."""
    wdb = Store._open_writer_connection(path)  # noqa: SLF001
    wdb.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    q: queue.Queue[Any] = queue.Queue()
    for row in rows:
        q.put_nowait(row)
    thread = threading.Thread(
        target=store._writer_thread_main,  # noqa: SLF001
        args=(wdb, q),
        daemon=True,
    )
    thread.start()
    return thread, q


async def _wait_until(pred: Any, bound_s: float) -> bool:
    deadline = time.monotonic() + bound_s
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return bool(pred())


async def test_tape_batch_hitting_a_lock_is_retried_with_its_rows_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch whose commit fails with a LOCK-class error (SQLITE_BUSY after
    the connection's busy wait — a ledger transaction or a retention DELETE
    holding the write lock past the bound) is RETRIED with its rows still in
    memory, attempt after attempt, until it lands: nothing dropped, no
    ``store_writer_batch_failed``, one ``store_writer_batch_locked_retrying``
    WARNING per attempt carrying n / attempt / the sqlite error name. (Before:
    the 1,000 rows were discarded as a batch failure.)"""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    rows = [_row(f"r{i}") for i in range(7)]
    blocker = sqlite3.connect(path, isolation_level=None)
    thread: threading.Thread | None = None
    try:
        blocker.execute("BEGIN IMMEDIATE")  # hold the WAL write lock
        thread, q = _start_thread_body(store, path, rows, busy_timeout_ms=50)
        assert await _wait_until(
            lambda: store.batch_lock_retries >= 2, persistence.STORE_OP_TIMEOUT_S
        )
        assert spy.of("store_writer_batch_failed") == []
        assert await store.count("decisions") == 0
        blocker.execute("ROLLBACK")  # the lock lifts
        q.put_nowait(persistence._WRITER_STOP)  # noqa: SLF001
        await asyncio.to_thread(thread.join, persistence.STORE_OP_TIMEOUT_S)
        assert not thread.is_alive()
        assert await store.count("decisions") == 7  # every row of the batch landed
        locked = spy.of("store_writer_batch_locked_retrying")
        assert len(locked) >= 2
        assert all(e[0] == "warning" for e in locked)
        assert all(e[2]["n"] == 7 for e in locked)
        assert all(e[2]["sqlite_errorname"] == "SQLITE_BUSY" for e in locked)
        assert [e[2]["attempt"] for e in locked] == list(range(1, len(locked) + 1))
        assert store.batch_lock_retries == len(locked)
        assert spy.of("store_writer_batch_failed") == []
        assert spy.of("store_writer_thread_died") == []
    finally:
        with contextlib.suppress(sqlite3.Error):
            blocker.execute("ROLLBACK")
        blocker.close()
        if thread is not None and thread.is_alive():
            store._writer_stop.set()  # noqa: SLF001
            await asyncio.to_thread(thread.join, persistence.STORE_OP_TIMEOUT_S)
        await store.close()


async def test_tape_batch_lock_retry_gives_up_loudly_on_the_stop_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry has no number of its own: it ends when the batch lands or
    when ``close()`` sets the stop flag — then the batch is dropped LOUDLY
    (``store_writer_batch_failed`` with the lock retries it survived) and the
    thread exits, closing its connection."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    rows = [_row(f"r{i}") for i in range(3)]
    blocker = sqlite3.connect(path, isolation_level=None)
    thread: threading.Thread | None = None
    try:
        blocker.execute("BEGIN IMMEDIATE")
        thread, _q = _start_thread_body(store, path, rows, busy_timeout_ms=50)
        assert await _wait_until(
            lambda: store.batch_lock_retries >= 1, persistence.STORE_OP_TIMEOUT_S
        )
        store._writer_stop.set()  # noqa: SLF001 — what close() does
        await asyncio.to_thread(thread.join, persistence.STORE_OP_TIMEOUT_S)
        assert not thread.is_alive()
        failed = spy.of("store_writer_batch_failed")
        assert len(failed) == 1
        assert failed[0][2]["n"] == 3
        assert failed[0][2]["lock_retries"] >= 1
        assert isinstance(failed[0][2]["exc_info"], sqlite3.OperationalError)
        assert spy.of("store_writer_thread_died") == []
        blocker.execute("ROLLBACK")
        assert _truth(path, "SELECT COUNT(*) FROM decisions") == 0
    finally:
        with contextlib.suppress(sqlite3.Error):
            blocker.execute("ROLLBACK")
        blocker.close()
        if thread is not None and thread.is_alive():
            store._writer_stop.set()  # noqa: SLF001
            await asyncio.to_thread(thread.join, persistence.STORE_OP_TIMEOUT_S)
        await store.close()


async def test_checkpoint_connection_never_waits_and_folds_passive_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedicated checkpoint connection carries busy_timeout 0 — under a
    pinned reader the whole attempt returns its busy VERDICT at once (the
    old connection sat in the busy handler for the full STORE_OP_TIMEOUT_S
    per attempt, the thread's only stall), and PASSIVE runs BEFORE the
    TRUNCATE so the frames no reader pins are folded regardless of the
    verdict. Failure accounting unchanged: counted, ``passive_fallback_ok``
    True, fast retry; once the reader closes the TRUNCATE completes."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        assert store._ckpt_db is not None  # noqa: SLF001
        assert store._ckpt_db.execute("PRAGMA busy_timeout").fetchone()[0] == 0  # noqa: SLF001
        ckpt_spy = _PragmaSpyDB(store._ckpt_db)  # noqa: SLF001
        store._ckpt_db = ckpt_spy  # type: ignore[assignment]  # noqa: SLF001
        for i in range(10):
            await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
        cursor = await store._db.execute(  # noqa: SLF001 — the pinned reader
            "SELECT reasons_json FROM decisions"
        )
        assert await cursor.fetchone() is not None  # mid-iteration: read-mark pinned
        for _ in range(10):
            _foreign_commit(path)  # frames PAST the reader's mark: unfoldable while it lives
        t0 = time.perf_counter()
        assert store._wal_checkpoint() is False  # noqa: SLF001
        elapsed = time.perf_counter() - t0
        assert elapsed < persistence.STORE_OP_TIMEOUT_S / 10  # no busy wait at all
        assert ckpt_spy.checkpoint_sqls == [
            "PRAGMA wal_checkpoint(PASSIVE)",
            "PRAGMA wal_checkpoint(TRUNCATE)",
        ]
        assert store.checkpoint_failures == 1
        assert store.checkpoint_passive_fallbacks == 1
        failed = spy.of("store_writer_checkpoint_failed")
        assert len(failed) == 1
        assert failed[0][2]["passive_fallback_ok"] is True
        m = re.fullmatch(r"busy \(wal_frames=(\d+), checkpointed=(\d+)\)", failed[0][2]["error"])
        assert m is not None
        wal_frames, checkpointed = int(m.group(1)), int(m.group(2))
        assert 0 < checkpointed < wal_frames  # PASSIVE folded up to the reader's mark, no further
        await cursor.close()
        assert store._wal_checkpoint() is True  # noqa: SLF001 — reader gone: WAL reset
        assert ckpt_spy.checkpoint_sqls[-2:] == [
            "PRAGMA wal_checkpoint(PASSIVE)",
            "PRAGMA wal_checkpoint(TRUNCATE)",
        ]
        assert store.checkpoint_failures == 1
    finally:
        await store.close()


async def test_close_leaves_the_checkpoint_connection_to_a_still_running_writer_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the writer thread outlives close()'s bounded join (here: its one
    batch is stuck in the busy wait behind a third connection's write lock),
    close() must NOT close the checkpoint connection the thread owns — it
    logs ``store_checkpoint_connection_left_to_writer_thread`` alongside the
    join-timeout warning and leaves it to the daemon. Once the lock lifts the
    thread commits its batch, reads the stop flag and exits on its own."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    real_bound = persistence.STORE_OP_TIMEOUT_S
    # Squeeze close()'s two waits (module globals read at call time) so the
    # test costs milliseconds; the mechanism is unchanged.
    monkeypatch.setattr(persistence, "WRITER_CLOSE_DRAIN_S", 0.05)
    monkeypatch.setattr(persistence, "STORE_OP_TIMEOUT_S", 0.05)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    store.start_writer()
    thread = store._writer_thread  # noqa: SLF001
    ckpt = store._ckpt_db  # noqa: SLF001
    assert thread is not None and ckpt is not None
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        await store.record_decision("no_quote", "r0", ["skip_test"], {})
        # The thread took the row and is inside its busy wait on the commit.
        assert await _wait_until(lambda: store.writer_queue_depth() == 0, real_bound)
        await store.close()
        assert thread.is_alive()
        assert len(spy.of("store_writer_thread_join_timeout")) == 1
        assert len(spy.of("store_checkpoint_connection_left_to_writer_thread")) == 1
        ckpt.execute("SELECT 1")  # still open — never closed under the thread
    finally:
        with contextlib.suppress(sqlite3.Error):
            blocker.execute("ROLLBACK")
        blocker.close()
    await asyncio.to_thread(thread.join, real_bound)
    assert not thread.is_alive()
    ckpt.close()
    assert _truth(path, "SELECT COUNT(*) FROM decisions") == 1  # landed once the lock lifted
    assert spy.of("store_writer_batch_failed") == []


class _SqlSpyDB:
    """Delegating aiosqlite proxy recording EVERY statement text it sees."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.sqls: list[str] = []

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.sqls.append(sql)
        return self._db.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


async def test_decision_reason_counts_runs_off_the_shared_connection_inside_sqlite(
    tmp_path: Path,
) -> None:
    """Must-fix #2: the ONE unbounded read issues NO statement on the shared
    connection (its own ``mode=ro`` connection in a worker thread) and counts
    inside SQLite — same numbers as the retired per-row ``json.loads`` loop,
    including a reason repeated inside one row counting twice. It sees rows
    the tape THREAD committed. A legacy direct construction (no path) runs
    the same aggregate on the shared connection."""
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        spy = _SqlSpyDB(store._db)  # noqa: SLF001
        store._db = spy  # type: ignore[assignment]  # noqa: SLF001
        seeded = (["a", "b"], ["a"], [], ["b", "b"], ["c"])
        for reasons in seeded:
            await store.record_decision("no_quote", "r", reasons, {})
        expected: dict[str, int] = {}
        for reasons in seeded:  # the retired loop's arithmetic
            for reason in reasons:
                expected[reason] = expected.get(reason, 0) + 1
        n_before = len(spy.sqls)
        counts = await store.decision_reason_counts()
        assert counts == expected == {"a": 2, "b": 3, "c": 1}
        assert len(spy.sqls) == n_before  # not ONE statement on the shared connection
        db = await aiosqlite.connect(path)
        legacy = Store(db, FakeClock())
        try:
            assert await legacy.decision_reason_counts() == counts
        finally:
            await legacy.close()
        store.start_writer()
        await _flood(store, 3)  # committed by the THREAD, on its connection
        assert (await store.decision_reason_counts())["skip_test"] == 3
    finally:
        await store.close()


async def test_tape_thread_yields_the_write_lock_to_a_ledger_transaction_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ledger first on the WAL write lock (review fix pass, measured: with a
    tape backlog the thread re-took the lock the instant it committed and a
    ``record_fill`` waited 178-1,483 ms in SQLite's unfair busy backoff).
    While a ledger transaction is in flight (``_ledger_txn`` clears
    ``_ledger_txn_idle``) the thread holds its next batch — counted in
    ``batch_yields_to_ledger`` — and proceeds the moment the ledger is done.
    The hold is bounded by STORE_OP_TIMEOUT_S: a flag left clear (a stuck
    caller) never stalls the tape. ``_ledger_txn`` itself restores the flag
    on success and failure; on cancellation the RESIDUE task it hands the
    connection lock to restores it once the late statement has landed
    (review #2 fix pass)."""
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    thread: threading.Thread | None = None
    try:
        # (1) In flight → the batch waits; released → it lands at once.
        store._ledger_txn_idle.clear()  # noqa: SLF001 — what _ledger_txn does around its body
        thread, q = _start_thread_body(
            store, path, [_row(f"r{i}") for i in range(4)], busy_timeout_ms=50
        )
        assert await _wait_until(
            lambda: store.batch_yields_to_ledger == 1, persistence.STORE_OP_TIMEOUT_S
        )
        await asyncio.sleep(0.05)
        assert _truth(path, "SELECT COUNT(*) FROM decisions") == 0  # held back
        store._ledger_txn_idle.set()  # noqa: SLF001
        assert await _wait_until(
            lambda: _truth(path, "SELECT COUNT(*) FROM decisions") == 4,
            persistence.STORE_OP_TIMEOUT_S,
        )
        q.put_nowait(persistence._WRITER_STOP)  # noqa: SLF001
        await asyncio.to_thread(thread.join, persistence.STORE_OP_TIMEOUT_S)
        assert not thread.is_alive()
        thread = None
        # (2) A flag left clear is BOUNDED: the batch proceeds after STORE_OP_TIMEOUT_S.
        monkeypatch.setattr(persistence, "STORE_OP_TIMEOUT_S", 0.05)
        store._ledger_txn_idle.clear()  # noqa: SLF001
        thread, q = _start_thread_body(store, path, [_row("late")], busy_timeout_ms=50)
        assert await _wait_until(lambda: _truth(path, "SELECT COUNT(*) FROM decisions") == 5, 5.0)
        assert store.batch_yields_to_ledger == 2
        q.put_nowait(persistence._WRITER_STOP)  # noqa: SLF001
        await asyncio.to_thread(thread.join, 5.0)
        assert not thread.is_alive()
        thread = None
        store._ledger_txn_idle.set()  # noqa: SLF001
        # (3) _ledger_txn clears the flag for its body and restores it every way out.
        seen: list[bool] = []

        async def body() -> None:
            seen.append(store._ledger_txn_idle.is_set())  # noqa: SLF001

        await store._ledger_txn(body)  # noqa: SLF001
        assert seen == [False]
        assert store._ledger_txn_idle.is_set()  # noqa: SLF001

        async def failing() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await store._ledger_txn(failing)  # noqa: SLF001
        assert store._ledger_txn_idle.is_set()  # noqa: SLF001

        async def hanging() -> None:
            await asyncio.sleep(60)

        task = asyncio.create_task(store._ledger_txn(hanging))  # noqa: SLF001
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not store._ledger_txn_idle.is_set()  # noqa: SLF001 — in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Restored by the residue task the cancelled body handed the lock to.
        assert await _wait_until(lambda: store._ledger_txn_idle.is_set(), 5.0)  # noqa: SLF001
        assert store.ledger_txn_cancellations == 1
        assert not store._conn_lock.locked()  # noqa: SLF001
    finally:
        if thread is not None and thread.is_alive():
            store._ledger_txn_idle.set()  # noqa: SLF001
            store._writer_stop.set()  # noqa: SLF001
            await asyncio.to_thread(thread.join, 5.0)
        await store.close()


# --------------------------------------------------------------------------- #
# REVIEW #2 FIX PASS (2026-09-05): CANCELLATION RESIDUE ON THE SHARED           #
# CONNECTION. A body cancelled by ``asyncio.wait_for`` while its statement is  #
# queued / busy-waiting on the aiosqlite worker LANDS LATE. Probe E (the       #
# reviewer's, on the real Store) proved the shape: the late INSERT succeeds,   #
# the connection is left INSIDE an open write transaction holding the WAL      #
# write lock, the tape thread cannot commit, and the next ``_ledger_txn``      #
# joined + committed the PARTIAL body. Probes B and C are the read and the     #
# writer-less write shapes.                                                    #
# --------------------------------------------------------------------------- #


def _hold_write_lock(path: Path, hold_s: float) -> threading.Thread:
    """A THIRD connection takes the WAL write lock (``BEGIN IMMEDIATE``) for
    ``hold_s`` seconds, then commits — the foreign write-lock hold a tape
    batch commit or a retention prune batch is (returns once the lock is
    held)."""
    held = threading.Event()

    def body() -> None:
        con = sqlite3.connect(path, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            held.set()
            time.sleep(hold_s)
            con.execute("COMMIT")
        finally:
            con.close()

    thread = threading.Thread(target=body, name="foreign-write-lock", daemon=True)
    thread.start()
    assert held.wait(persistence.STORE_OP_TIMEOUT_S)
    return thread


async def _settle_after_cancel(store: Store, holder: threading.Thread) -> None:
    """Let the foreign hold lift, the late statement land and any residue task
    finish: the connection lock free again, then one SQLite busy-handler
    step (<= 100 ms) for the late statement's own landing."""
    await asyncio.to_thread(holder.join, persistence.STORE_OP_TIMEOUT_S)
    assert not holder.is_alive()
    assert await _wait_until(
        lambda: not store._conn_lock.locked(),  # noqa: SLF001
        persistence.STORE_OP_TIMEOUT_S,
    )
    await asyncio.sleep(0.2)


async def test_probe_e_cancelled_ledger_body_landing_late_is_rolled_back_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROBE E (review #2 must-fix #1) as a regression test — FAILS on
    ``b24c0db``. Real Store + ``start_writer()`` + a tape firehose; a
    ``record_fill`` body is cancelled by ``wait_for`` (bound 0.4 s) while its
    INSERT busy-waits behind a foreign write-lock hold (0.7 s). The INSERT
    then LANDS. Unfixed: the shared connection stays inside an open write
    transaction (WAL write lock held), the tape thread cannot commit, and the
    next ledger transaction joins + commits the partial body (fills 1,
    ev_ledger 0). Fixed: the cancelled ``_ledger_txn`` hands the connection
    lock to a residue task that runs FIFO behind the late statement and
    rolls it back; nothing partial is ever committed; the tape lands within
    its cadence; the normal replay re-books the fill exactly once."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        store.start_writer()
        stop = False
        enqueued = 0

        async def firehose() -> None:
            nonlocal enqueued
            while not stop:
                for _ in range(10):
                    await store.record_decision("no_quote", f"f{enqueued}", ["skip_test"], {})
                    enqueued += 1
                await asyncio.sleep(0.001)

        fh = asyncio.create_task(firehose())
        holder = _hold_write_lock(path, 0.7)
        t0 = time.perf_counter()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_fill(store, "late"), 0.4)
        assert time.perf_counter() - t0 < 0.7  # the bound returned BEFORE the hold lifted
        await _settle_after_cancel(store, holder)
        # (1) never left inside a transaction once the residue is cleared
        assert store._db.in_transaction is False  # noqa: SLF001
        # (2) the NEXT unrelated ledger transaction must not commit a partial body
        await store.record_position_open(_position(), subaccount="0")
        stop = True
        await fh
        # (3) the tape thread commits within its cadence (the write lock was never pinned)
        assert await store.flush_writer(persistence.STORE_OP_TIMEOUT_S)
        assert store._dropped_writes == 0  # noqa: SLF001
        assert _truth(path, "SELECT COUNT(*) FROM decisions") == enqueued
        # (4) a fresh read-only connection sees exactly the committed fills: NONE
        assert _truth(path, "SELECT COUNT(*) FROM fills") == 0
        assert _truth(path, "SELECT COUNT(*) FROM ev_ledger") == 0
        assert _truth(path, "SELECT COUNT(*) FROM position_ledger") == 1
        assert spy.of("store_ledger_txn_inherited_open_transaction") == []
        assert spy.of("store_ledger_txn_left_open") == []
        cancelled = spy.of("store_ledger_txn_cancelled")
        assert len(cancelled) == 1
        assert cancelled[0][0] == "warning"
        assert cancelled[0][2]["late_statement_landed"] is True
        assert cancelled[0][2]["rolled_back"] is True
        assert store.ledger_txn_cancellations == 1
        assert store.ledger_txn_cancel_rollbacks == 1
        # (5) THE REPLAY PATH: the normal record_fill re-books — inserted True, ONE row
        # (lifecycle: has_fill False -> record_fill -> inserted -> no late-landing adoption)
        assert await store.has_fill("fill:late") is False
        assert await _fill(store, "late") is True
        assert _truth(path, "SELECT COUNT(*) FROM fills") == 1
        assert _truth(path, "SELECT COUNT(*) FROM ev_ledger") == 1
        assert await store.count("fills") == 1
        assert store._db.in_transaction is False  # noqa: SLF001
    finally:
        await store.close()


async def test_probe_c_cancelled_write_landing_late_is_rolled_back_not_half_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PROBE C: no writer thread (sync mode). A ``record_fill`` cancelled by a
    real ``wait_for`` while its INSERT busy-waits behind a foreign write-lock
    hold lands late -> rolled back (fills 0 / ev_ledger 0, connection outside
    any transaction), never half-committed by the next transaction; the
    replay books it once. A cancellation whose body had already COMMITTED
    (the bound expired during the commit hop) is a clean residue: nothing to
    roll back, the committed row stands for the replay to adopt."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        holder = _hold_write_lock(path, 0.5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_fill(store, "c"), 0.2)
        await _settle_after_cancel(store, holder)
        assert store._db.in_transaction is False  # noqa: SLF001
        assert await store.record_combo_trades("KXMVE-T1", [{"trade_id": "t1"}]) == 1
        assert _truth(path, "SELECT COUNT(*) FROM fills") == 0
        assert _truth(path, "SELECT COUNT(*) FROM ev_ledger") == 0
        assert _truth(path, "SELECT COUNT(*) FROM combo_trades") == 1
        assert spy.of("store_ledger_txn_inherited_open_transaction") == []
        assert store.ledger_txn_cancellations == 1
        assert store.ledger_txn_cancel_rollbacks == 1
        assert await _fill(store, "c") is True
        assert _truth(path, "SELECT COUNT(*) FROM fills") == 1
        assert _truth(path, "SELECT COUNT(*) FROM ev_ledger") == 1
        # A body cancelled AFTER its commit hop was queued: the commit lands,
        # the residue task finds nothing to roll back (clean), the row stands.
        real_commit = store._db.commit  # noqa: SLF001

        async def slow_commit() -> None:
            await real_commit()
            await asyncio.sleep(0.2)

        monkeypatch.setattr(store._db, "commit", slow_commit)  # noqa: SLF001
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_fill(store, "d"), 0.1)
        monkeypatch.setattr(store._db, "commit", real_commit)  # noqa: SLF001
        assert await _wait_until(
            lambda: store.ledger_txn_cancellations == 2
            and not store._conn_lock.locked(),  # noqa: SLF001
            persistence.STORE_OP_TIMEOUT_S,
        )
        assert store.ledger_txn_cancel_rollbacks == 1  # nothing to roll back this time
        assert _truth(path, "SELECT COUNT(*) FROM fills") == 2
        assert await store.has_fill("fill:d") is True  # the replay ADOPTS a committed row
        assert await _fill(store, "d") is False
        cancelled = spy.of("store_ledger_txn_cancelled")
        assert [e[2]["late_statement_landed"] for e in cancelled] == [True, False]
        assert [e[0] for e in cancelled] == ["warning", "info"]
    finally:
        await store.close()


async def test_probe_b_cancelled_read_mid_statement_then_record_fill_is_booked(
    tmp_path: Path,
) -> None:
    """PROBE B: a READ cancelled by a real ``wait_for`` while its statement is
    still stepping on the aiosqlite worker (a table-reading scan), a foreign
    commit under it (the tape thread's role — the read snapshot is now
    stale), then ``record_fill``. Booked, exactly once; the connection ends
    outside any transaction; the lock is free. (The reviewer measured this
    landing 50/50 through the rollback+retry path; the lock hand-off now
    waits out the residue statement, so the write never collides with it.)"""
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        for i in range(120):
            await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
        slow_scan = (  # per-row string work: a COUNT(*) alone ran the cross join in 15 ms
            "SELECT SUM(LENGTH(a.reasons_json || b.kind || c.rfq_id))"
            " FROM decisions a, decisions b, decisions c"
        )
        t0 = time.perf_counter()
        n_scan = (await store._fetchone(slow_scan))[0]  # noqa: SLF001
        scan_s = time.perf_counter() - t0
        assert n_scan > 0
        assert scan_s > 0.05, scan_s  # slow enough to be cancelled mid-statement
        for k in range(3):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(store._fetchone(slow_scan), 0.01)  # noqa: SLF001
            _foreign_commit(path)
            assert await _fill(store, f"b{k}") is True
            assert store._db.in_transaction is False  # noqa: SLF001
        assert await _wait_until(
            lambda: not store._conn_lock.locked(),  # noqa: SLF001
            persistence.STORE_OP_TIMEOUT_S,
        )
        assert store.ledger_txn_cancellations == 3
        assert store.ledger_txn_cancel_rollbacks == 0  # reads open no transaction
        assert store.ledger_txn_rollbacks == 0  # no snapshot collision: the residue was waited out
        assert _truth(path, "SELECT COUNT(*) FROM fills") == 3
        assert await store.count("decisions") == 123
    finally:
        await store.close()


async def test_ledger_txn_rolls_back_an_inherited_open_transaction_never_joins_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #2: a transaction already open on ENTRY (foreign residue — a
    DML statement issued outside the class and never committed) is ROLLED
    BACK and logged at ERROR, never joined: the first fix pass joined it and
    committed a partial body along with our own. Our body still lands."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        await store._db.execute(  # noqa: SLF001 — outside the class, no commit: the residue
            "INSERT INTO rfq_deletions (rfq_id, seen_at, raw_json) VALUES ('x', 't', '{}')"
        )
        assert store._db.in_transaction is True  # noqa: SLF001
        assert await _fill(store, "a") is True
        assert store._db.in_transaction is False  # noqa: SLF001
        assert _truth(path, "SELECT COUNT(*) FROM rfq_deletions") == 0  # never committed
        assert _truth(path, "SELECT COUNT(*) FROM fills") == 1
        inherited = spy.of("store_ledger_txn_inherited_open_transaction")
        assert len(inherited) == 1
        assert inherited[0][0] == "error"
        assert store.ledger_txn_rollbacks == 1
        assert store.ledger_txn_retries == 0
    finally:
        await store.close()


async def test_dead_writer_thread_is_restarted_then_abandoned_without_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #2 should-fix 2. (1) The writer thread dies (a bug raised past
    the batch handler): the in-flight batch is lost — counted, its queue
    slots released — and the LOOP-SIDE supervisor restarts the thread on a
    fresh connection within its cadence (STORE_OP_TIMEOUT_S); rows queued
    while it was dead land. (2) It dies again after landing a batch: another
    restart (progress since the last one). (3) It dies again WITHOUT landing
    a batch: ABANDONED — the queue is purged into the drop counter,
    ``writer_queue_depth`` reads 0 (tape_retention's idle gate stays
    truthful), ``_write`` drops directly, and ``store_writer_stats`` is
    emitted from the loop with the drops even though no thread lives to post
    it. ``close()`` returns promptly."""
    spy = _LogSpy()
    monkeypatch.setattr(persistence, "log", spy)
    monkeypatch.setattr(persistence, "STORE_OP_TIMEOUT_S", 0.05)
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        real = store._commit_batch_retrying  # noqa: SLF001
        deaths_left = {"n": 1}

        def flaky(wdb: sqlite3.Connection, batch: list[Any]) -> bool:
            if deaths_left["n"] > 0:
                deaths_left["n"] -= 1
                raise RuntimeError("writer bug")
            return bool(real(wdb, batch))

        monkeypatch.setattr(store, "_commit_batch_retrying", flaky)
        store.start_writer()
        # (1) first death → restart
        await _flood(store, 5)
        assert await _wait_until(lambda: store.writer_thread_deaths == 1, 2.0)
        await _flood(store, 3)  # queued while dead
        assert await _wait_until(lambda: store.writer_thread_restarts == 1, 2.0)
        assert await store.flush_writer(2.0)
        thread = store._writer_thread  # noqa: SLF001
        assert thread is not None and thread.is_alive()
        lost = store._writer_rows_lost  # noqa: SLF001
        assert 1 <= lost <= 5
        assert _truth(path, "SELECT COUNT(*) FROM decisions") + lost == 8
        died = spy.of("store_writer_thread_died")
        assert len(died) == 1 and died[0][2]["rows_lost"] == lost
        assert len(spy.of("store_writer_thread_restarted")) == 1
        assert any(e[2]["writer_thread_alive"] is False for e in spy.of("store_writer_stats"))
        assert not store.writer_abandoned
        # (2) dies again after progress → restarted again
        deaths_left["n"] = 1
        await _flood(store, 2)
        assert await _wait_until(lambda: store.writer_thread_restarts == 2, 2.0)
        assert not store.writer_abandoned
        # (3) dies again with NO landed batch since that restart → abandoned
        deaths_left["n"] = 10**6
        await _flood(store, 4)
        assert await _wait_until(lambda: store.writer_thread_deaths == 3, 2.0)
        assert await _wait_until(lambda: store.writer_abandoned, 2.0)
        assert store.writer_thread_restarts == 2  # no third restart
        assert store.writer_queue_depth() == 0  # purged: truthful for the idle gate
        abandoned = spy.of("store_writer_abandoned")
        assert len(abandoned) == 1 and abandoned[0][0] == "error"
        # _write drops directly now, and the loop-side cadence REPORTS the drops
        before = store._dropped_writes  # noqa: SLF001
        n_stats = len(spy.of("store_writer_stats"))
        await _flood(store, 7)
        assert store._dropped_writes == before + 7  # noqa: SLF001
        assert store.writer_queue_depth() == 0
        assert await _wait_until(lambda: len(spy.of("store_writer_stats")) > n_stats, 2.0)
        last = spy.of("store_writer_stats")[-1]
        assert last[0] == "warning"
        assert last[2]["dropped_writes_delta"] >= 7
        assert last[2]["writer_abandoned"] is True
        assert last[2]["writer_thread_alive"] is False
        assert last[2]["writer_thread_restarts"] == 2
        t0 = time.perf_counter()
    finally:
        await store.close()
    assert time.perf_counter() - t0 < 2.0  # nothing to drain, nothing to join


async def test_report_tape_reads_run_on_one_read_only_connection_in_one_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #2 should-fix 1: the report's FOUR tape reads issue no statement
    on the shared connection and cost ONE worker-thread hop on ONE read-only
    connection; the individual methods agree and are off-connection too;
    ``build_report`` carries the same keys in place and touches the shared
    connection only for the ledger summaries. Legacy direct construction
    (no path) runs them on the shared connection."""
    path = tmp_path / "t.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        await store.record_rfq(RFQ, source="ws")
        await store.record_decision("no_quote", "rfq_1", ["skip_test", "a"], {})
        await store.record_decision("quote", "rfq_2", [], {})
        await store.record_would_quote(
            "rfq_1", fair_prob=0.5, fair_cc=5000, width_cc=100, leg_probs=(0.5,), context={}
        )
        spy = _SqlSpyDB(store._db)  # noqa: SLF001
        store._db = spy  # type: ignore[assignment]  # noqa: SLF001
        hops: list[str] = []
        real_to_thread = asyncio.to_thread

        async def counting_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
            hops.append(getattr(fn, "__name__", repr(fn)))
            return await real_to_thread(fn, *args, **kwargs)

        monkeypatch.setattr(persistence.asyncio, "to_thread", counting_to_thread)
        expected = {
            "rfqs_seen": 1,
            "decisions_by_kind": {"no_quote": 1, "quote": 1},
            "skip_reasons": {"skip_test": 1, "a": 1},
            "would_quotes": 1,
        }
        n = len(spy.sqls)
        assert await store.report_tape_counts() == expected
        assert hops == ["_report_tape_counts_ro"]  # ONE hop
        assert len(spy.sqls) == n  # not ONE statement on the shared connection
        assert await store.count("rfqs") == 1
        assert await store.count("decisions") == 2
        assert await store.count("would_quotes") == 1
        assert await store.decision_kind_counts() == expected["decisions_by_kind"]
        assert await store.decision_reason_counts() == expected["skip_reasons"]
        assert len(spy.sqls) == n
        assert len(hops) == 6
        report = await build_report(store, env="demo")
        assert {k: report[k] for k in expected} == expected
        assert list(report)[:6] == ["env", "note", *expected]  # key order kept
        shared = spy.sqls[n:]
        assert shared  # the ledger summaries (ev_ledger / markouts) still read here
        tape = ("rfqs", "decisions", "would_quotes")
        assert [s for s in shared if any(t in s for t in tape)] == []
        db = await aiosqlite.connect(path)
        legacy = Store(db, FakeClock())
        try:
            assert await legacy.report_tape_counts() == expected
        finally:
            await legacy.close()
    finally:
        await store.close()
