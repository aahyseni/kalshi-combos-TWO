"""TAPE RETENTION — the recorder tables bounded by what the boot-time readers
need (2026-09-05 design item 7, the ALTERNATIVE to a rotation). DARK: the
in-process step runs only behind ``observe.tape_retention_enabled`` (default
False = byte-identical: no connection, no read, no thread).

THE PROBLEM. ``rfqs`` (66.4M rows) and ``decisions`` (134.3M rows) are the
recorder TAPE, written through ``Store._write``'s drop-on-overflow queue and
never deleted. At 213 GB every index insert is a random 4 KB read into
B-trees that fit no cache, the single writer falls behind the firehose and
DROPS the tape (1.86M rows on the 9/5 00:52 boot). No live reader needs that
history: the boot-time acceptance seed reads the last ``SEED_WINDOW_S`` of
``decisions``/``rfqs``; ``Store.held_positions`` needs one ``rfqs`` row per
leg set for a fills ticker the ``position_ledger`` cannot resolve. Everything
else that reads old tape is an analysis tool, pointed at an archive.

THE RETENTION WINDOW IS DERIVED, NOT SET (North Star):

    retention_s = max(reader windows)      # SEED_WINDOW_S — what a reader needs
                + PRUNE_CADENCE_S          # a nightly pass may land anywhere in
                                           #   its period; the row a reader needs
                                           #   at the END of the period was
                                           #   ``window + cadence`` old when the
                                           #   pass at its START ran
                + measured disorder        # the tape's time column is enqueue-
                                           #   ordered with the PK only up to the
                                           #   worker's pickup->record latency
                                           #   (``rfqs.seen_at`` is stamped at
                                           #   pickup, recorded after dispatch);
                                           #   the bisection's boundary error is
                                           #   the largest backward step MEASURED
                                           #   on the newest rows, so it is added

The only rows below the cutoff that survive are the PROTECTED leg-provenance
rows: one real ``rfqs`` row per distinct ``(market_ticker, legs_json)`` for
every fills ticker without a ``position_ledger`` row — exactly the rows
``Store.held_positions`` falls back to (an ambiguous ticker keeps every
distinct shape, so it stays ambiguous: fail-closed parity).

THE PASS is bounded by primitives that already exist, never a fresh literal:
  * batch = ``acceptance_seed._CHUNK_IDS`` ids (the seed's own chunk, so a
    batch's lock hold is the size the store already tolerates from a reader);
  * a batch is INTERRUPTED at ``STORE_OP_TIMEOUT_S`` (the writer's own lock
    tolerance — a longer hold could fail the writer's synchronous confirm-path
    commit with 'database is locked': a confirmed fill without a ledger row)
    and rolled back, and the pass STOPS: SQLite's progress handler consults
    the clock while the DELETE runs, so the write lock is never held past the
    bound — a post-hoc check alone would notice only after the damage. This
    store is too slow to prune live, and the mechanism says so instead of
    pressing on;
  * ``MAX_BATCHES_PER_PASS = PRUNE_CADENCE_S / STORE_OP_TIMEOUT_S``: a pass
    never outlasts its own period even if every batch waited its full lock
    timeout, so passes never overlap;
  * ``should_continue()`` between batches — the app passes "the tape writer's
    queue is empty", so the prune only ever runs against an idle writer (the
    2026-08-19 lesson: piling work onto a saturated store is how a diagnostic
    becomes an outage).

Each batch is its own transaction on a SECOND stdlib connection (never the
shared aiosqlite writer thread — the 2026-07-26 stall), run in a worker thread
by the app. The store's own writer keeps owning checkpoints; the prune never
checkpoints in-process — its connection sets ``wal_autocheckpoint=0``, because
a default connection would PASSIVE-checkpoint after any commit that grows the
WAL past 1000 pages (every 25k-row batch does), inside the measured batch time
and against the writer's own checkpoint cadence. ``DELETE`` returns pages to the freelist — the file
does not shrink (that is ``tools/ops/rotate_store.py``'s job, once) but stops
growing past ~2 days of tape, and the B-trees the writer inserts into stay
small forever.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from structlog import get_logger

from combomaker.core.clock import Clock
from combomaker.ops.acceptance_seed import _CHUNK_IDS, SEED_WINDOW_S
from combomaker.ops.persistence import BUSY_TIMEOUT_MS, STORE_OP_TIMEOUT_S

log = get_logger(__name__)

JsonDict = dict[str, Any]

#: The recorder TAPE: unbounded per-RFQ growth, written through the
#: drop-on-overflow queue (``Store._write``: rfqs, rfq_deletions, decisions)
#: or the observe-only measurement recorders (would_quotes*). Everything
#: else in the store is a LEDGER (bounded by fills/positions) or telemetry
#: the operator reads whole (``structural_fits`` rides ``_write`` too, but is
#: one row per structural fit — never pruned, carried whole by a rotation).
TAPE_TABLES: tuple[str, ...] = (
    "rfqs",
    "rfq_deletions",
    "decisions",
    "would_quotes",
    "would_quotes_inplay",
)
#: Time column per tape table — enqueue-ordered with the PK up to the measured
#: disorder; bisected on the PK, never scanned (ops/acceptance_seed.py READ
#: DISCIPLINE: the tape has no index on its time column).
TAPE_TIME_COLUMN: dict[str, str] = {
    "rfqs": "seen_at",
    "rfq_deletions": "seen_at",
    "decisions": "at",
    "would_quotes": "at",
    "would_quotes_inplay": "at",
}
#: Every boot-time reader of tape HISTORY by a time window, with its window.
#: The retention derives from the LONGEST. (``Store.held_positions`` reads the
#: tape by TICKER, not by time — those rows are PROTECTED, not windowed.)
READER_WINDOWS_S: dict[str, int] = {
    "ops/acceptance_seed.seed_counts_from_store": int(SEED_WINDOW_S),
}
#: One pass per NIGHT: the ratified anchors are per-night quantities and the
#: seed window is the night (acceptance_seed.py) — the cadence is that same
#: partition, not a schedule of its own.
PRUNE_CADENCE_S: int = int(SEED_WINDOW_S)
#: Batch = the seed's own id chunk (a lock hold the store already tolerates).
PRUNE_BATCH_IDS: int = int(_CHUNK_IDS)
#: A pass never outlasts its own period even if every batch waited the
#: writer's full lock tolerance ⇒ passes never overlap.
MAX_BATCHES_PER_PASS: int = int(PRUNE_CADENCE_S / STORE_OP_TIMEOUT_S)
#: The newest rows the disorder is MEASURED on: one seed chunk per table.
DISORDER_SAMPLE_IDS: int = PRUNE_BATCH_IDS


def reader_window_s() -> int:
    """The longest boot-time tape-reader window (what a reader needs)."""
    return max(READER_WINDOWS_S.values())


def retention_window_s(*, disorder_s: float) -> float:
    """``max reader window + prune cadence + measured disorder`` (module doc)."""
    return float(reader_window_s()) + float(PRUNE_CADENCE_S) + max(0.0, float(disorder_s))


# --------------------------------------------------------------- bisection


def bisect_first_id(
    con: sqlite3.Connection, table: str, column: str, cutoff_iso: str
) -> int | None:
    """First PK id whose ``column`` >= cutoff — bisection on the PK, never a
    scan. The same algorithm as ``acceptance_seed._bisect_first_id``
    generalised to the time column (parity-tested against it on
    ``decisions``), with one difference: past the LAST row it returns None
    (the seed tolerates an off-by-one there; a prune/copy must not treat a
    row older than the window as inside it). None also for an empty table."""
    row = con.execute(f'SELECT MIN(id), MAX(id) FROM "{table}"').fetchone()  # noqa: S608
    if not row or row[0] is None:
        return None
    lo, hi = int(row[0]), int(row[1])

    def at_of(i: int) -> str | None:
        r = con.execute(
            f'SELECT "{column}" FROM "{table}" WHERE id >= ? ORDER BY id LIMIT 1',  # noqa: S608
            (i,),
        ).fetchone()
        return None if r is None else str(r[0])

    first = at_of(lo)
    if first is not None and first >= cutoff_iso:
        return lo
    while lo < hi:
        mid = (lo + hi) // 2
        a = at_of(mid)
        if a is None or a >= cutoff_iso:
            hi = mid
        else:
            lo = mid + 1
    last = at_of(lo)
    if last is None or last < cutoff_iso:
        return None
    return lo


def _parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def measure_disorder_s(
    con: sqlite3.Connection, table: str, column: str, *, sample_ids: int = DISORDER_SAMPLE_IDS
) -> JsonDict:
    """The largest BACKWARD step of the time column across the newest
    ``sample_ids`` ids (0.0 when monotone) — the bisection's boundary error,
    measured on this tape right now."""
    row = con.execute(f'SELECT MAX(id) FROM "{table}"').fetchone()  # noqa: S608
    if not row or row[0] is None:
        return {"table": table, "rows": 0, "disorder_s": 0.0, "unparseable": 0}
    lo = int(row[0]) - int(sample_ids) + 1
    worst = 0.0
    prev: datetime | None = None
    n = unparseable = 0
    for (value,) in con.execute(
        f'SELECT "{column}" FROM "{table}" WHERE id >= ? ORDER BY id',  # noqa: S608
        (lo,),
    ):
        n += 1
        cur = _parse_iso(str(value))
        if cur is None:
            unparseable += 1
            continue
        if prev is not None and cur < prev:
            worst = max(worst, (prev - cur).total_seconds())
        prev = cur if prev is None else max(prev, cur)
    return {"table": table, "rows": n, "disorder_s": worst, "unparseable": unparseable}


# --------------------------------------------------------------- protected


@dataclass(frozen=True)
class ProtectedRows:
    """The ``rfqs`` rows a prune must never delete and a rotation must carry:
    ``Store.held_positions``' tape fallback — one real row per distinct
    ``(market_ticker, legs_json)`` for every fills ticker with no
    ``position_ledger`` row. ``conflicting`` tickers hold >1 shape (kept
    ambiguous: fail-closed parity); ``unresolvable`` have no tape row at all
    (reserved from exchange figures today already)."""

    tickers: list[str] = field(default_factory=list)
    rfq_ids: list[int] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    unresolvable: list[str] = field(default_factory=list)

    def as_dict(self) -> JsonDict:
        return asdict(self)


def protected_rfq_ids(con: sqlite3.Connection) -> ProtectedRows:
    """PREDICATE PARITY with ``Store.held_positions``: the ledger resolves a
    ticker only through rows whose ``legs_json`` is non-empty
    (``Store._ledger_legsets`` skips ``not legs_json`` and then consults the
    tape for that ticker), so a fills ticker whose ledger rows ALL carry an
    empty ``legs_json`` still needs its tape rows — protected here too. (The
    DDL says ``legs_json TEXT NOT NULL``, so only ``''`` can occur.) Per
    ``(market_ticker, legs_json)`` the MIN(id) row is kept with ITS
    ``collection_ticker``; ``held_positions`` reads ``MAX(collection_ticker)``
    over the group — identical because a market_ticker belongs to exactly one
    collection."""
    tables = {
        str(r[0])
        for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not {"fills", "position_ledger", "rfqs"} <= tables:
        return ProtectedRows()
    tickers = [
        str(r[0])
        for r in con.execute(
            "SELECT DISTINCT combo_ticker FROM fills WHERE combo_ticker NOT IN"
            " (SELECT combo_ticker FROM position_ledger"
            "  WHERE legs_json IS NOT NULL AND legs_json != '')"
            " ORDER BY combo_ticker"
        )
    ]
    ids: list[int] = []
    conflicting: list[str] = []
    unresolvable: list[str] = []
    for t in tickers:
        rows = con.execute(
            "SELECT MIN(id) FROM rfqs WHERE market_ticker = ? GROUP BY legs_json", (t,)
        ).fetchall()
        if not rows:
            unresolvable.append(t)
            continue
        if len(rows) > 1:
            conflicting.append(t)
        ids.extend(int(r[0]) for r in rows)
    return ProtectedRows(
        tickers=tickers, rfq_ids=sorted(ids), conflicting=conflicting, unresolvable=unresolvable
    )


# -------------------------------------------------------------------- plan


def plan_prune(con: sqlite3.Connection, *, now: datetime) -> JsonDict:
    """READ-ONLY: the derived window and, per tape table, the id below which
    rows are older than it (``prune_below_id``; None = nothing to prune),
    with an id-range row estimate (COUNT on 134M rows is a scan)."""
    present = {
        str(r[0])
        for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    disorder: dict[str, JsonDict] = {}
    for t in TAPE_TABLES:
        if t in present:
            disorder[t] = measure_disorder_s(con, t, TAPE_TIME_COLUMN[t])
    worst = max((d["disorder_s"] for d in disorder.values()), default=0.0)
    retention = retention_window_s(disorder_s=worst)
    cutoff = datetime.fromtimestamp(now.timestamp() - retention, tz=UTC).isoformat()
    protected = protected_rfq_ids(con)
    tables: dict[str, JsonDict] = {}
    for t in TAPE_TABLES:
        if t not in present:
            continue
        bounds = con.execute(f'SELECT MIN(id), MAX(id) FROM "{t}"').fetchone()  # noqa: S608
        if not bounds or bounds[0] is None:
            tables[t] = {"min_id": None, "max_id": None, "prune_below_id": None, "rows_estimate": 0}
            continue
        min_id, max_id = int(bounds[0]), int(bounds[1])
        first_keep = bisect_first_id(con, t, TAPE_TIME_COLUMN[t], cutoff)
        # No row inside the window ⇒ every row is older ⇒ prune the whole table.
        below = (max_id + 1) if first_keep is None else first_keep
        # A pass starts at the first UNPROTECTED id: the protected leg-
        # provenance rows are the oldest rows of ``rfqs`` and stay forever,
        # so MIN(id) alone would re-walk an empty prefix every night.
        start = _first_unprotected_id(con, t, protected.rfq_ids if t == "rfqs" else [])
        if start is None:
            start = min_id
        tables[t] = {
            "min_id": min_id,
            "max_id": max_id,
            "start_id": start,
            "first_keep_id": first_keep,
            "prune_below_id": below if below > start else None,
            "rows_estimate": max(0, below - start),
        }
    return {
        "now": now.isoformat(),
        "reader_windows_s": dict(READER_WINDOWS_S),
        "reader_window_s": reader_window_s(),
        "prune_cadence_s": PRUNE_CADENCE_S,
        "disorder": disorder,
        "disorder_s": worst,
        "retention_window_s": retention,
        "cutoff_iso": cutoff,
        "batch_ids": PRUNE_BATCH_IDS,
        "max_batches_per_pass": MAX_BATCHES_PER_PASS,
        "batch_time_bound_s": STORE_OP_TIMEOUT_S,
        "tables": tables,
        "protected": protected.as_dict(),
    }


def _first_unprotected_id(
    con: sqlite3.Connection, table: str, protected: list[int]
) -> int | None:
    """MIN(id) over the rows that are not protected — the PK walk stops at the
    first qualifying row (a bounded IN list: 134 ids on the live store)."""
    if not protected:
        row = con.execute(f'SELECT MIN(id) FROM "{table}"').fetchone()  # noqa: S608
    else:
        marks = ",".join("?" * len(protected))
        row = con.execute(
            f'SELECT MIN(id) FROM "{table}" WHERE id NOT IN ({marks})',  # noqa: S608
            protected,
        ).fetchone()
    return None if not row or row[0] is None else int(row[0])


# -------------------------------------------------------------------- pass


@dataclass
class TablePrune:
    table: str
    prune_below_id: int | None
    batches: int = 0
    rows_deleted: int = 0
    complete: bool = True


@dataclass
class PruneResult:
    """One bounded pass. ``complete`` = every table reached its bound;
    ``stopped_reason`` names the bound that ended an incomplete pass."""

    started_at: str
    retention_window_s: float
    cutoff_iso: str
    tables: dict[str, TablePrune] = field(default_factory=dict)
    batches: int = 0
    rows_deleted: int = 0
    protected_rfq_ids: int = 0
    elapsed_s: float = 0.0
    complete: bool = True
    stopped_reason: str | None = None
    slowest_batch_s: float = 0.0

    def as_log_fields(self) -> JsonDict:
        d = asdict(self)
        d["tables"] = {t: asdict(v) for t, v in self.tables.items()}
        return d


def connect_rw(db_path: Path) -> sqlite3.Connection:
    """The prune's OWN connection (never the shared aiosqlite one): the
    store's busy wait, autocommit off (each batch is an explicit BEGIN
    IMMEDIATE / COMMIT), no schema writes, and NO auto-checkpoint — the
    store's writer owns checkpoints (a default connection would PASSIVE-
    checkpoint after every batch that grows the WAL past 1000 pages, inside
    the measured batch time and against the writer's cadence)."""
    con = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=rw",
        uri=True,
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
    )
    con.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}")
    con.execute("PRAGMA wal_autocheckpoint=0")
    return con


#: How often (in SQLite VM instructions) a running batch consults the clock for
#: its time bound — a polling granularity, not a bound (the bound is
#: ``STORE_OP_TIMEOUT_S``); a 25k-row DELETE crosses it hundreds of times.
_DEADLINE_POLL_OPS = 10_000


def _arm_batch_deadline(con: sqlite3.Connection, deadline_mono: float) -> None:
    """Interrupt whatever statement runs on ``con`` once the monotonic clock
    passes ``deadline_mono``: SQLite's progress handler aborts the statement
    with ``OperationalError('interrupted')`` — the time bound holds WHILE the
    batch runs, so the write lock is released at the bound, never after."""
    con.set_progress_handler(
        lambda: 1 if time.monotonic() > deadline_mono else 0, _DEADLINE_POLL_OPS
    )


def _disarm_batch_deadline(con: sqlite3.Connection) -> None:
    con.set_progress_handler(None, 0)


def _is_interrupt(exc: sqlite3.OperationalError) -> bool:
    return "interrupt" in str(exc).lower()


def run_prune_pass(
    db_path: Path,
    *,
    now: datetime,
    should_continue: Callable[[], bool] | None = None,
    max_batches: int = MAX_BATCHES_PER_PASS,
    batch_ids: int = PRUNE_BATCH_IDS,
    batch_time_bound_s: float = STORE_OP_TIMEOUT_S,
) -> PruneResult:
    """ONE bounded pass (module doc): plan read-only, then delete tape rows
    below each table's bound in id-range batches, each its own transaction,
    skipping the protected ``rfqs`` rows. Stops early (``complete=False``)
    when ``should_continue()`` says no, a batch was INTERRUPTED at the
    writer's lock tolerance (rolled back — ``batches`` counts the attempt,
    ``rows_deleted`` does not), or the per-pass batch cap is reached. Never
    raises for an empty/absent table; other SQLite errors propagate to the
    caller (the app step logs them and retries next cadence)."""
    t0 = time.monotonic()
    con = connect_rw(db_path)
    try:
        p = plan_prune(con, now=now)
        protected = [int(x) for x in p["protected"]["rfq_ids"]]
        res = PruneResult(
            started_at=now.isoformat(),
            retention_window_s=float(p["retention_window_s"]),
            cutoff_iso=str(p["cutoff_iso"]),
            protected_rfq_ids=len(protected),
        )
        for t, facts in p["tables"].items():
            below = facts.get("prune_below_id")
            tp = TablePrune(table=t, prune_below_id=below)
            res.tables[t] = tp
            if below is None:
                continue
            lo = int(facts["start_id"])
            tp.complete = False
            while lo < below:
                if res.batches >= max_batches:
                    res.stopped_reason = f"max_batches_per_pass {max_batches} reached"
                    break
                if should_continue is not None and not should_continue():
                    res.stopped_reason = "should_continue() false (writer busy)"
                    break
                hi = min(lo + int(batch_ids), below)
                tb = time.monotonic()
                con.execute("BEGIN IMMEDIATE")
                # The lock is held from here: the deadline counts from tb (the
                # wait for the lock is part of the batch) and the DELETE is
                # interrupted the moment the clock passes it.
                _arm_batch_deadline(con, tb + float(batch_time_bound_s))
                interrupted = False
                deleted = 0
                try:
                    deleted = _delete_range(con, t, lo, hi, protected if t == "rfqs" else [])
                    con.execute("COMMIT")
                except sqlite3.OperationalError as exc:
                    _disarm_batch_deadline(con)
                    # An interrupted DELETE inside an explicit transaction makes
                    # SQLite roll back the ENTIRE transaction itself (documented
                    # sqlite3_interrupt semantics): no partial batch can survive,
                    # and there may be nothing left to roll back here.
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                    if not _is_interrupt(exc):
                        raise
                    interrupted = True
                    deleted = 0
                except BaseException:
                    _disarm_batch_deadline(con)
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                    raise
                finally:
                    _disarm_batch_deadline(con)
                took = time.monotonic() - tb
                res.slowest_batch_s = max(res.slowest_batch_s, took)
                res.batches += 1
                tp.batches += 1
                tp.rows_deleted += deleted
                res.rows_deleted += deleted
                if interrupted:
                    res.stopped_reason = (
                        f"batch interrupted at STORE_OP_TIMEOUT_S {batch_time_bound_s}s"
                        f" (the writer's lock tolerance) after {took:.2f}s and rolled back"
                        " — store too slow to prune live"
                    )
                    break
                lo = hi
                if took > batch_time_bound_s:
                    res.stopped_reason = (
                        f"batch took {took:.2f}s > STORE_OP_TIMEOUT_S {batch_time_bound_s}s"
                        " (the writer's lock tolerance) — store too slow to prune live"
                    )
                    break
            else:
                tp.complete = True
            if not tp.complete:
                res.complete = False
                break
        res.elapsed_s = round(time.monotonic() - t0, 3)
        return res
    finally:
        con.close()


def _delete_range(
    con: sqlite3.Connection, table: str, lo: int, hi: int, protected: list[int]
) -> int:
    """``DELETE ... WHERE id >= lo AND id < hi`` with the range SPLIT around
    every protected id inside it (a handful of ids on the live store — 134
    across the whole tape), so a protected row is never named by a DELETE."""
    inside = sorted(x for x in protected if lo <= x < hi)
    deleted = 0
    prev = lo
    for pid in [*inside, hi]:
        if pid > prev:
            cur = con.execute(
                f'DELETE FROM "{table}" WHERE id >= ? AND id < ?',  # noqa: S608
                (prev, pid),
            )
            deleted += int(cur.rowcount)
        prev = pid + 1
    return deleted


# ------------------------------------------------------------------- step


class TapeRetentionStep:
    """The app-side scheduler for the nightly pass (constructed only when
    ``observe.tape_retention_enabled``; the maintenance loop calls
    ``maybe_launch`` on its slow cadence).

    * DUE when no pass has COMPLETED within ``cadence_s`` (a pass that stopped
      early — writer busy, batch too slow, cap — re-arms for the next call:
      the leftover is bounded by one cadence of tape and the mechanism keeps
      trying against an idle writer instead of waiting a day).
    * SINGLE-FLIGHT: a pass still running is never stacked.
    * WRITER-IDLE: launched only while the tape writer's queue is empty, and
      ``should_continue`` re-checks that between batches from the worker
      thread (``qsize`` is a plain length read).
    * OFF-LOOP: ``asyncio.to_thread`` — the event loop never blocks on it;
      errors log and retry next cadence (fix isolation: nothing here is read
      by pricing, risk or quoting)."""

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Clock,
        writer_queue_depth: Callable[[], int],
        cadence_s: float = PRUNE_CADENCE_S,
    ) -> None:
        self._db_path = db_path
        self._clock = clock
        self._writer_queue_depth = writer_queue_depth
        self._cadence_s = float(cadence_s)
        self._task: asyncio.Task[None] | None = None
        self._last_complete_mono_ns: int | None = None
        # Set by close(): the worker thread cannot be cancelled, so the pass
        # reads this between batches and stops (one batch, <= its time bound).
        self._closing = False
        self.passes = 0
        self.last_result: PruneResult | None = None

    def due(self) -> bool:
        if self._last_complete_mono_ns is None:
            return True
        age_s = (self._clock.monotonic_ns() - self._last_complete_mono_ns) / 1e9
        return age_s >= self._cadence_s

    def maybe_launch(self) -> str:
        """Returns why nothing launched, or ``"launched"``."""
        if self._task is not None and not self._task.done():
            return "in_flight"
        if not self.due():
            return "not_due"
        if self._writer_queue_depth() > 0:
            return "writer_busy"
        self._task = asyncio.create_task(self._run(), name="tape-retention")
        return "launched"

    async def _run(self) -> None:
        try:
            res = await asyncio.to_thread(
                run_prune_pass,
                self._db_path,
                now=self._clock.now().astimezone(UTC),
                should_continue=self.should_continue,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tape_retention_pass_failed")
            return
        self.passes += 1
        self.last_result = res
        if res.complete:
            self._last_complete_mono_ns = self._clock.monotonic_ns()
        emit = log.info if res.complete else log.warning
        emit("tape_retention_pass", **res.as_log_fields())

    def should_continue(self) -> bool:
        """Between batches (worker thread): the writer is idle and nobody is
        shutting down."""
        return not self._closing and self._writer_queue_depth() == 0

    async def close(self) -> None:
        self._closing = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown
                pass
