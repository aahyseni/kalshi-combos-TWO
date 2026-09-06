import asyncio
import contextlib
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

import combomaker.ops.persistence as persistence
from combomaker.core.clock import FakeClock
from combomaker.ops.persistence import Store
from combomaker.rfq.models import Rfq

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
