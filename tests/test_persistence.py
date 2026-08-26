import asyncio
import sqlite3
from pathlib import Path
from typing import Any

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
# WAL CHECKPOINT RESILIENCE (2026-07-18) — _writer_loop / _wal_checkpoint.     #
# Live failure: 'database table is locked' on EVERY manual TRUNCATE of a run   #
# (a long-lived read cursor starves the lock); the old shared try/except       #
# logged it as a batch failure and waited another 5000 writes while the WAL    #
# grew 78→194MB. The fix: own failure path + PASSIVE fallback + ~500-write     #
# retry cadence. Simulated with a delegating connection proxy that locks the   #
# TRUNCATE (and optionally PASSIVE) pragma.                                    #
# Since 2026-08-19 checkpoints run on a DEDICATED second connection (the       #
# self-lock fix — on the shared connection its own read cursors made BOTH     #
# pragmas raise), so the proxy wraps ``store._ckpt_db``, not ``store._db``.    #
# --------------------------------------------------------------------------- #


class _CheckpointLockedDB:
    """Delegating aiosqlite proxy over the DEDICATED checkpoint connection:
    raises 'database table is locked' on ``wal_checkpoint(TRUNCATE)`` (and on
    PASSIVE too when ``passive_locked``) while ``locked`` is True; counts
    every checkpoint attempt. All other traffic passes through to the real
    connection — the batch INSERT/commit path is untouched, exactly the live
    failure shape."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self.locked = True
        self.passive_locked = False
        self.truncate_attempts = 0
        self.passive_attempts = 0

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        # NOT async: returns aiosqlite's awaitable/async-with cursor object
        # untouched so every caller shape (await / async with) still works; a
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
    """Enqueue n tape writes and wait for the writer to drain them (task_done
    runs AFTER the checkpoint attempt of the batch, so join() implies any due
    checkpoint has been attempted)."""
    for i in range(n):
        await store.record_decision("no_quote", f"r{i}", ["skip_test"], {})
    assert store._write_q is not None  # noqa: SLF001
    await asyncio.wait_for(store._write_q.join(), timeout=10.0)  # noqa: SLF001


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
    """Delegating proxy recording WHEN its close() ran (returns the real
    close coroutine so ``await conn.close()`` still works)."""

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
    """Stands in for the module logger: records (level, event, kwargs)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append(("warning", event, kw))

    def exception(self, event: str, **kw: Any) -> None:
        self.events.append(("exception", event, kw))

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
        # Bound the TRUNCATE's reader-wait so the busy verdict is fast.
        await store._ckpt_db.execute("PRAGMA busy_timeout=50")  # noqa: SLF001
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
        assert await store._wal_checkpoint() is False  # noqa: SLF001 — busy, NOT a raise
        assert store.checkpoint_failures == 1
        assert store.checkpoint_passive_fallbacks == 1  # PASSIVE still folded
        assert main_spy.checkpoint_sqls == []  # main saw NO checkpoint pragma
        assert len(ckpt_spy.checkpoint_sqls) >= 1
        await cursor.close()
        assert await store._wal_checkpoint() is True  # noqa: SLF001 — reader gone
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
        # Async tape mode with a TINY queue and no writer draining it:
        # 2 of 5 writes fit, 3 drop.
        store._write_q = asyncio.Queue(maxsize=2)  # noqa: SLF001
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
