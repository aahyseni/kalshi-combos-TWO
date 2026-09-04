"""SQLite persistence behind a thin repository so Postgres is a drop-in later.

Everything the system sees or decides is recorded: every RFQ, every deletion,
every decision with its reason codes and context, every would-quote with the
pricing snapshot that produced it. This doubles as the offline replay /
backtest dataset, and — because closed RFQs vanish from the exchange after
~7 days — our local record is the durable one.

Later phases add tables (quotes, fills, markouts, ev_ledger) via new idempotent
DDL statements here; the schema is append-only by convention.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import aiosqlite

from combomaker.core.clock import Clock
from combomaker.ops.logging import get_logger
from combomaker.rfq.models import Rfq

if TYPE_CHECKING:
    from combomaker.pricing.fit_challenge import FitChallenge
    from combomaker.risk.exposure import OpenPosition

log = get_logger(__name__)

JsonDict = dict[str, Any]

_DDL = """
CREATE TABLE IF NOT EXISTS rfqs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    source TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    collection_ticker TEXT,
    contracts_centi INTEGER,
    target_cost_cc INTEGER,
    n_legs INTEGER NOT NULL,
    legs_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rfqs_rfq_id ON rfqs (rfq_id);
CREATE INDEX IF NOT EXISTS idx_rfqs_collection ON rfqs (collection_ticker);
CREATE INDEX IF NOT EXISTS idx_rfqs_market_ticker ON rfqs (market_ticker);

CREATE TABLE IF NOT EXISTS rfq_deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    rfq_id TEXT,
    reasons_json TEXT NOT NULL,
    context_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_kind ON decisions (kind);

CREATE TABLE IF NOT EXISTS would_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    rfq_id TEXT NOT NULL,
    fair_prob REAL NOT NULL,
    fair_cc INTEGER NOT NULL,
    width_cc INTEGER NOT NULL,
    leg_probs_json TEXT NOT NULL,
    context_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_would_quotes_rfq ON would_quotes (rfq_id);

-- IN-PLAY SHADOW (2026-07-25, measurement only — no quote is ever sent).
-- One row per in-play-skipped RFQ the shadow priced: the would-be quote the
-- pregame gate declined, for the in-play adverse-selection study that gates
-- ever arming in-play quoting. Kept SEPARATE from would_quotes: that table's
-- schema (fair_prob/width/leg_probs, no bids/ticker/sizing/leg timing) is the
-- Phase-2 observe shape and lacks every column this measurement needs.
-- leg_time_to_start_s_json: {leg_ticker: seconds to scheduled start} from the
-- SAME pregame ladder that produced the skip — NEGATIVE = seconds INTO the
-- game (the depth axis of the study); null = that leg's start UNKNOWN.
CREATE TABLE IF NOT EXISTS would_quotes_inplay (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    rfq_id TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    fair_cc INTEGER NOT NULL,
    yes_bid_cc INTEGER NOT NULL,
    no_bid_cc INTEGER NOT NULL,
    target_cost_cc INTEGER,
    contracts_centi INTEGER,
    leg_time_to_start_s_json TEXT NOT NULL,
    context_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_would_quotes_inplay_rfq ON would_quotes_inplay (rfq_id);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    fill_ref TEXT NOT NULL,
    order_id TEXT,
    combo_ticker TEXT NOT NULL,
    our_side TEXT NOT NULL,
    contracts_centi INTEGER NOT NULL,
    price_cc INTEGER NOT NULL,
    fee_cc INTEGER,
    expected_edge_cc INTEGER,
    raw_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',   -- booked | verified | phantom (2026-09-04)
    verified_at TEXT,                         -- /portfolio/fills proof (or void) stamp
    exchange_fill_id TEXT                     -- matching tape fill_id, or phantom:<reason>
);
CREATE INDEX IF NOT EXISTS idx_fills_ref ON fills (fill_ref);

CREATE TABLE IF NOT EXISTS markouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    fill_ref TEXT NOT NULL,
    horizon_s REAL NOT NULL,
    fair_at_fill_cc INTEGER,
    fair_now_cc INTEGER,
    raw_mid_at_fill_cc INTEGER,
    raw_mid_now_cc INTEGER
);
CREATE INDEX IF NOT EXISTS idx_markouts_ref ON markouts (fill_ref);

CREATE TABLE IF NOT EXISTS combo_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    seen_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    created_time TEXT,
    yes_price_cc INTEGER,
    count_centi INTEGER,
    taker_side TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_combo_trades_ticker ON combo_trades (ticker);

CREATE TABLE IF NOT EXISTS ev_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    fill_ref TEXT NOT NULL,
    expected_edge_cc INTEGER NOT NULL,
    realized_pnl_cc INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ev_ref ON ev_ledger (fill_ref);

CREATE TABLE IF NOT EXISTS structural_fits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    rfq_id TEXT,
    model TEXT NOT NULL,
    n_legs INTEGER NOT NULL,
    exactly_identified INTEGER NOT NULL,
    residual REAL NOT NULL,
    verdict TEXT NOT NULL,
    reject_bar REAL NOT NULL,
    challenge_bar REAL NOT NULL,
    tickers_json TEXT NOT NULL,
    family TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_structural_fits_verdict ON structural_fits (verdict);
CREATE INDEX IF NOT EXISTS idx_structural_fits_rfq ON structural_fits (rfq_id);

-- P1.10 DURABLE POSITION LEDGER. One row per position (keyed on the exchange
-- position_id) carrying the fields the audit plan mandates: exchange
-- quantity/side, cost, fees, subaccount, status, settlement value, reconcile
-- time, and the order-independent leg-set hash. This is the SOURCE OF TRUTH for
-- what we hold and how it settled — distinct from the append-only `fills` tape
-- (which can hold many fills per position). Money/quantities are int centi-
-- units; a position is OPEN until a settlement row reconciles it to SETTLED.
CREATE TABLE IF NOT EXISTS position_ledger (
    position_id TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL,
    combo_ticker TEXT NOT NULL,
    collection_ticker TEXT,
    subaccount TEXT NOT NULL,
    our_side TEXT NOT NULL,            -- "yes" | "no" (exchange side we hold)
    contracts_centi INTEGER NOT NULL, -- exchange quantity, centi-contracts
    entry_price_cc INTEGER NOT NULL,  -- cost basis per contract, centi-cents
    cost_cc INTEGER NOT NULL,         -- total premium PAID = max loss, centi-cents
    fees_cc INTEGER NOT NULL,         -- fees paid to date, centi-cents
    leg_set_hash TEXT NOT NULL,       -- durable order-independent combo identity
    legs_json TEXT NOT NULL,
    status TEXT NOT NULL,             -- "open" | "settled"
    settled_value REAL,               -- V in [0,1], NULL until settled
    realized_pnl_cc INTEGER,          -- NULL until settled
    settlement_fee_cc INTEGER,        -- NULL until settled
    reconciled_at TEXT                -- reconciliation time, NULL until settled
);
CREATE INDEX IF NOT EXISTS idx_position_ledger_ticker ON position_ledger (combo_ticker);
CREATE INDEX IF NOT EXISTS idx_position_ledger_status ON position_ledger (status);
CREATE INDEX IF NOT EXISTS idx_position_ledger_leghash ON position_ledger (leg_set_hash);

-- STORE METADATA (2026-09-04 review fixes, item D): tiny key/value table for
-- facts about the store ITSELF that no ledger row carries — today the fills
-- verification WATERMARK: the highest fills.id (and the wall stamp) at the
-- moment the verification columns were added. Rows at or below it predate
-- execution verification (their proof is tools/ops/repair_phantom_fills.py,
-- settlement-corroborated); rows above it were booked by code that verifies
-- every claim, so a restart re-arms exactly those still 'booked'.
CREATE TABLE IF NOT EXISTS store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# SQLite's own lock-wait tolerance for this connection (``PRAGMA busy_timeout``).
# It is the store's EXISTING statement of "how long an operation here may
# legitimately block before something is wrong", so a caller that must bound a
# store await derives its wall bound from this rather than inventing a second
# number. Named 2026-07-26 for exactly that reason: the maintenance tick's
# ledger-divergence SELECT had NO bound at all and sat on a saturated aiosqlite
# connection for >65 s, silencing the loop that owned the liveness heartbeat.
#
# IMPORTANT: busy_timeout only bounds SQLite's own LOCK waits. It does NOT bound
# the time a statement spends QUEUED behind other work on the single aiosqlite
# connection thread (the background tape writer's 1000-statement batches run
# there; its WAL TRUNCATE/PASSIVE checkpoints moved to a DEDICATED second
# connection, 2026-08-19). That queueing is the actual 2026-07-26 stall, and
# only an asyncio-level ``wait_for`` can bound it.
BUSY_TIMEOUT_MS = 5000
STORE_OP_TIMEOUT_S = BUSY_TIMEOUT_MS / 1000.0


class Store:
    # Manual WAL checkpoint cadence (writes between attempts) and the
    # ACCELERATED retry cadence after a failed/busy checkpoint (2026-07-18:
    # 'database table is locked' was observed on EVERY attempt of a live run —
    # a long-lived read cursor starves the TRUNCATE — and the old shared
    # try/except waited another full 5000 writes while the WAL grew 78→194MB
    # in ~40min). Class attributes so tests can tighten them per instance.
    _CHECKPOINT_EVERY_WRITES = 5000
    _CHECKPOINT_RETRY_WRITES = 500

    def __init__(
        self,
        db: aiosqlite.Connection,
        clock: Clock,
        *,
        ckpt_db: aiosqlite.Connection | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        # DEDICATED CHECKPOINT CONNECTION (2026-08-19 self-lock fix). WAL
        # checkpoints run here, NEVER on the shared connection: a read cursor
        # held open across an await on the SAME connection (maintenance tick /
        # settlement poller scans) made EVERY TRUNCATE **and** PASSIVE raise
        # SQLITE_LOCKED 'database table is locked' — 3,120 consecutive
        # checkpoint failures live, a 6GB WAL, writer 2h35m behind. Against a
        # SEPARATE connection the same readers yield a busy=1 VERDICT
        # (non-raising, the already-handled path) and PASSIVE still folds
        # pages up to the read-mark. None only for legacy direct
        # constructions (read-only diagnostics that never start the writer);
        # ``Store.open`` always provides it.
        self._ckpt_db = ckpt_db
        # Optional background writer for NON-critical tape (rfqs, decisions,
        # deletions). OFF by default → writes are SYNCHRONOUS (tests + read-after-
        # write stay correct, no leaked task). The app calls start_writer() so the
        # hot RFQ path ENQUEUES instead of awaiting a commit — otherwise a WAL
        # auto-checkpoint on the ~2GB DB runs INLINE on the awaited commit and
        # freezes the WHOLE event loop (34s+ intake stalls; 2026-07-14 audit).
        # Fills/markouts/settlement stay synchronous & durable. Bounded queue:
        # drop tape on overflow, never block the loop.
        self._write_q: asyncio.Queue[tuple[str, tuple[Any, ...]]] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._dropped_writes = 0
        # Cumulative _dropped_writes as of the last ``store_writer_stats``
        # emit — the delta between emits is the alarm signal (2026-08-19:
        # ~75-80% of a day's tape rows were silently dropped while the
        # counter incremented with NO reader anywhere).
        self._dropped_writes_reported = 0
        # WAL-checkpoint health counters (2026-07-18): failed/busy TRUNCATE
        # attempts and PASSIVE fallbacks that ran. Public so an ops surface can
        # report them alongside dropped writes.
        self.checkpoint_failures = 0
        self.checkpoint_passive_fallbacks = 0
        # Cached fills verification watermark (2026-09-04 review fixes) —
        # read once from store_meta by ``fills_verification_watermark``.
        self._fills_verification_watermark: tuple[int, str] | None = None

    @classmethod
    async def open(cls, path: Path, clock: Clock) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        # WAL + relaxed sync (2026-07-14 throughput fix). The hot RFQ path awaits
        # a commit per RFQ + per decision (~300+/s during big-game bursts) to a
        # ~2GB DB; the default rollback-journal + synchronous=FULL fsyncs on EVERY
        # commit, and those fsyncs periodically STALLED the event loop → the RFQ
        # queue backed up → whole-minute quote blocks. WAL appends without a full
        # rewrite and synchronous=NORMAL fsyncs only at CHECKPOINT (not per commit),
        # so a commit is now ~microseconds and the write path can't stall the loop.
        # busy_timeout absorbs the brief checkpoint lock on the large DB.
        # ORDERING (2026-08-15 boot-race fix, second live occurrence): the
        # busy_timeout must be set BEFORE the WAL pragma. journal_mode=WAL
        # takes a brief exclusive lock; with busy_timeout still at its 0
        # default, a concurrent reader (the watchdog's store probe, a
        # diagnostic mode=ro scan of the ~150GB store) at that instant makes
        # it raise ``database is locked`` and the boot DIES (2026-08-14
        # 09:20 ET false start; 2026-08-15 12:06 ET, 25 min of downtime
        # mid-slate). With the timeout first, the same collision waits.
        await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        # autocheckpoint OFF (2026-07-14): with it ON, a 2000-page checkpoint
        # fired INLINE on every writer commit that crossed the threshold — on the
        # ~2GB DB that ran near-continuously during bursts, so the background
        # writer fell behind and DROPPED ~96% of the tape (18,759 quotes posted,
        # 603 recorded) → the live viewer went blind → PHANTOM blocks. The writer
        # now runs a BOUNDED manual checkpoint every ~5000 writes instead.
        await db.execute("PRAGMA wal_autocheckpoint=0")
        await db.executescript(_DDL)
        await db.commit()
        # structural_fits family / route / reason (build 2026-09-04 item B):
        # idempotent ADD COLUMN for a store created before them (CREATE TABLE
        # IF NOT EXISTS never alters an existing table; the live table held 0
        # rows — the recorder was never wired). SQLite ADD COLUMN is a
        # metadata-only O(1) change; NOT NULL DEFAULT '' keeps old readers valid.
        existing = {
            row[1]
            for row in await db.execute_fetchall("PRAGMA table_info(structural_fits)")
        }
        for column in ("family", "route", "reason"):
            if column not in existing:
                await db.execute(
                    f"ALTER TABLE structural_fits ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
        await db.commit()
        # FILL-LEDGER IDEMPOTENCY BACKSTOP (2026-07-16 P1): a UNIQUE index on
        # fills.fill_ref, so even a code path that bypasses record_fill's own
        # INSERT-if-absent guard can never double-insert a fill. Created OUTSIDE
        # the main DDL script deliberately: a legacy DB that already holds
        # duplicate fill_refs (the pre-fix WS-replay double-insert) must NOT
        # brick startup on the index build — the record_fill guard still
        # protects every NEW write; the loud error tells the operator to de-dup
        # offline. (Same IF NOT EXISTS idempotent-DDL pattern as
        # idx_rfqs_market_ticker; the old non-unique idx_fills_ref stays.)
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_ref_unique"
                " ON fills (fill_ref)"
            )
            await db.commit()
        except Exception:
            log.exception(
                "fills_unique_index_unavailable",
                detail="fills.fill_ref holds pre-existing duplicates — the "
                "UNIQUE index could not be created; record_fill's INSERT-if-"
                "absent still guards new writes; de-dup the table offline",
            )
        # PHANTOM-EXECUTION LEDGER STATE (2026-09-04 build, item D). The
        # exchange emitted ``quote_executed`` (WS AND REST quote status) for 28
        # orders since 2026-07-27 that never produced a /portfolio/fills row
        # (12 on 2026-08-26 alone; two of them HALTED settlement reconcile at
        # 22:41 ET: 66.71 predicted vs 43.47 exchange). A fills row therefore
        # carries a verification STATE: ``booked`` (written off the executed
        # message), ``verified`` (its order_id found on /portfolio/fills —
        # ``exchange_fill_id``/``verified_at`` are the evidence) or ``phantom``
        # (proven absent after the bounded verification; voided). ADD COLUMN
        # is idempotent against ``PRAGMA table_info`` so a legacy store opens
        # unchanged (every existing row reads ``booked``).
        await cls._ensure_fills_verification_columns(
            db, now_iso=clock.now().isoformat()
        )
        # ONE EXCHANGE ORDER = ONE LEDGER ROW (2026-09-04 build, item D): a
        # partial UNIQUE index on fills.order_id (NULL-keyed rows — poll-
        # recovered fills whose quote payload exposed no creator_order_id —
        # are exempt, exactly as the writer's own guard treats them). Same
        # tolerant pattern as idx_fills_ref_unique: a legacy store that
        # already holds duplicate order_ids must NOT brick startup — the
        # duplicates are enumerated in the loud error for the operator and
        # record_fill's INSERT-if-absent (now order_id-aware) guards every
        # new write regardless.
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_order_id_unique"
                " ON fills (order_id) WHERE order_id IS NOT NULL"
            )
            await db.commit()
        except Exception:
            duplicates: list[tuple[str, int]] = []
            try:
                async with db.execute(
                    "SELECT order_id, COUNT(*) FROM fills WHERE order_id IS NOT NULL"
                    " GROUP BY order_id HAVING COUNT(*) > 1 LIMIT 50"
                ) as cursor:
                    duplicates = [
                        (str(row[0]), int(row[1])) async for row in cursor
                    ]
            except Exception:  # noqa: BLE001 — diagnostics only, never fatal
                duplicates = []
            log.exception(
                "fills_order_id_unique_index_unavailable",
                n_duplicate_order_ids=len(duplicates),
                duplicates=duplicates[:20],
                detail="fills.order_id holds pre-existing duplicate rows (one "
                "exchange order booked under several fill_refs) — the partial "
                "UNIQUE index could not be created; record_fill's order_id-aware "
                "INSERT-if-absent still guards new writes; repair the listed "
                "rows offline (tools/ops/repair_phantom_fills.py)",
            )
        # DEDICATED CHECKPOINT CONNECTION (2026-08-19 self-lock fix — see
        # __init__). Only busy_timeout carries over: journal_mode=WAL is a
        # property of the DB FILE (already set above), and this connection
        # runs nothing but wal_checkpoint pragmas, so the writer-path pragmas
        # (synchronous, wal_autocheckpoint) are irrelevant here.
        ckpt_db = await aiosqlite.connect(path)
        await ckpt_db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return cls(db, clock, ckpt_db=ckpt_db)

    #: fills-ledger verification states (2026-09-04 build, item D).
    FILL_STATUS_BOOKED = "booked"
    FILL_STATUS_VERIFIED = "verified"
    FILL_STATUS_PHANTOM = "phantom"
    #: position_ledger status of a VOIDED phantom position (never 'open',
    #: never 'settled' — it was never held on the exchange).
    POSITION_STATUS_PHANTOM = "phantom"

    #: store_meta keys of the fills verification WATERMARK (2026-09-04 review
    #: fixes): the highest fills.id and the wall stamp when the verification
    #: columns were added to THIS store. Written once (INSERT OR IGNORE).
    META_FILLS_VERIFICATION_WATERMARK_ID = "fills_verification_watermark_id"
    META_FILLS_VERIFICATION_MIGRATED_AT = "fills_verification_migrated_at"

    @staticmethod
    async def _ensure_fills_verification_columns(
        db: aiosqlite.Connection, *, now_iso: str
    ) -> None:
        """Idempotent schema migration for the fills verification state
        (2026-09-04 build, item D): ``status`` (booked|verified|phantom),
        ``verified_at`` (ISO stamp of the /portfolio/fills proof) and
        ``exchange_fill_id`` (the matching tape row's fill_id — evidence).
        Reads ``PRAGMA table_info`` first so a store that already carries the
        columns is untouched; a legacy store gains them with every existing
        row reading ``booked``. Never raises into ``open``: a failure logs
        loudly and the store still opens (the ledger writer treats a missing
        column as "verification state unavailable", never as a crash).

        WATERMARK (review fixes): the first time this runs on a store it
        records ``MAX(fills.id)`` (0 on a fresh store) and ``now_iso`` in
        ``store_meta`` — once, never overwritten. Everything the restart
        re-arm and the ledger-quantity alarm scope to "after the fix" derives
        from these two facts; nothing is hand-set."""
        try:
            async with db.execute("PRAGMA table_info(fills)") as cursor:
                present = {str(row[1]) for row in await cursor.fetchall()}
            added: list[str] = []
            if "status" not in present:
                await db.execute(
                    "ALTER TABLE fills ADD COLUMN status TEXT NOT NULL DEFAULT 'booked'"
                )
                added.append("status")
            if "verified_at" not in present:
                await db.execute("ALTER TABLE fills ADD COLUMN verified_at TEXT")
                added.append("verified_at")
            if "exchange_fill_id" not in present:
                await db.execute("ALTER TABLE fills ADD COLUMN exchange_fill_id TEXT")
                added.append("exchange_fill_id")
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_fills_status ON fills (status)"
            )
            await db.execute(
                "INSERT OR IGNORE INTO store_meta (key, value)"
                " SELECT ?, CAST(COALESCE(MAX(id), 0) AS TEXT) FROM fills",
                (Store.META_FILLS_VERIFICATION_WATERMARK_ID,),
            )
            await db.execute(
                "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)",
                (Store.META_FILLS_VERIFICATION_MIGRATED_AT, now_iso),
            )
            await db.commit()
            if added:
                log.info("fills_verification_columns_added", columns=added)
        except Exception:
            log.exception(
                "fills_verification_columns_unavailable",
                detail="could not add the fills verification columns (status/"
                "verified_at/exchange_fill_id) — the store opens without them; "
                "fill verification cannot stamp state until the schema is repaired",
            )

    def start_writer(self) -> None:
        """Enable the off-hot-path background writer (the app calls this; tests
        don't, so their tape writes stay synchronous & immediately readable)."""
        if self._writer_task is not None:
            return
        self._write_q = asyncio.Queue(maxsize=200000)
        self._writer_task = asyncio.create_task(
            self._writer_loop(), name="store-writer"
        )

    async def close(self) -> None:
        if self._writer_task is not None:
            q = self._write_q
            try:  # drain queued tape before shutdown (bounded)
                if q is not None:
                    await asyncio.wait_for(q.join(), timeout=2.0)
            except TimeoutError:
                pass
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        # Checkpoint connection FIRST: were it still open when the main
        # connection closed, the main close's implicit final WAL fold would
        # see a second live connection and skip the truncate. Closed first,
        # the main connection is the LAST one and its close-time checkpoint
        # resets the WAL.
        if self._ckpt_db is not None:
            await self._ckpt_db.close()
        await self._db.close()

    async def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        """A NON-critical tape write. Async mode (writer running) → enqueue,
        NEVER blocks the hot path (drops on overflow). Sync mode (tests) → write
        immediately so read-after-write is correct."""
        q = self._write_q
        if q is None:
            await self._db.execute(sql, params)
            await self._db.commit()
            return
        try:
            q.put_nowait((sql, params))
        except asyncio.QueueFull:
            self._dropped_writes += 1

    async def _writer_loop(self) -> None:
        """Drain the tape queue and commit in BATCHES off the hot path — a WAL
        checkpoint here stalls only THIS task, never the intake/worker loop.

        CHECKPOINT RESILIENCE (2026-07-18): the manual checkpoint has its OWN
        failure path, no longer sharing the batch try/except — the live run's
        repeated 'database table is locked' checkpoint failures were logged
        indistinguishably from batch failures and each waited the full 5000
        writes to retry. A failed/busy TRUNCATE now (a) logs its own
        ``store_writer_checkpoint_failed`` event + counts
        ``checkpoint_failures``, (b) falls back to a PASSIVE checkpoint (folds
        whatever pages it can WITHOUT blocking readers — bounds WAL growth even
        while a long-lived cursor pins the lock), and (c) retries after
        ``_CHECKPOINT_RETRY_WRITES`` (~500) writes instead of the full cadence.
        Batch failures keep the existing loud ``store_writer_batch_failed``
        path, which now ALWAYS means the tape writes themselves failed."""
        assert self._write_q is not None
        q = self._write_q
        writes_since_checkpoint = 0
        checkpoint_after = self._CHECKPOINT_EVERY_WRITES
        while True:
            first = await q.get()
            batch = [first]
            while len(batch) < 1000:
                try:
                    batch.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                for sql, params in batch:
                    await self._db.execute(sql, params)
                await self._db.commit()
            except Exception:
                log.exception("store_writer_batch_failed", n=len(batch))
            else:
                # Bounded manual checkpoint OFF the hot path (autocheckpoint=0):
                # a TRUNCATE every ~5000 writes keeps the WAL small without an
                # inline checkpoint stalling every commit (which starved the
                # writer and dropped 96% of the tape during bursts). It runs on
                # the writer task, never the intake/worker loop, so a brief
                # stall only delays tape, not quotes. Committed data is durable
                # BEFORE the pragma — a checkpoint failure never loses tape.
                writes_since_checkpoint += len(batch)
                if writes_since_checkpoint >= checkpoint_after:
                    writes_since_checkpoint = 0
                    checkpoint_after = (
                        self._CHECKPOINT_EVERY_WRITES
                        if await self._wal_checkpoint()
                        else self._CHECKPOINT_RETRY_WRITES
                    )
                    # Dropped-tape visibility rides the checkpoint cadence
                    # (2026-08-19): _dropped_writes had NO reader anywhere
                    # while ~75-80% of a day's tape silently vanished.
                    self._emit_writer_stats()
            for _ in batch:
                q.task_done()

    async def _wal_checkpoint(self) -> bool:
        """ONE bounded manual checkpoint attempt (writer task only). Returns
        True iff the TRUNCATE fully completed (the WAL was reset).

        RUNS ON THE DEDICATED CHECKPOINT CONNECTION (2026-08-19 self-lock
        fix): on the SHARED connection, any read cursor open across an await
        made BOTH pragmas raise SQLITE_LOCKED, so the checkpoint could never
        run at all. Cross-connection, a live reader is a busy=1 verdict
        (handled below) and PASSIVE still folds pages up to the read-mark.
        A legacy direct construction without a checkpoint connection falls
        back to the shared one (it never starts the writer).

        A raised error ('database table is locked') AND a busy verdict (the
        pragma's first result column — TRUNCATE that could not finish reports
        busy=1 WITHOUT raising, which the old code silently counted as success)
        both take the failure path: count + log ``store_writer_checkpoint_failed``
        (its own event — never confused with a batch failure), then attempt a
        PASSIVE checkpoint before giving up the cycle. PASSIVE copies what it
        can without blocking readers, so the WAL keeps getting folded even
        while the TRUNCATE lock is starved by a long-lived cursor."""
        db = self._ckpt_db if self._ckpt_db is not None else self._db
        failure: str
        try:
            cursor = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or not row[0]:
                log.info(
                    "store_writer_checkpoint_ok",
                    wal_frames=None if row is None else row[1],
                    checkpointed=None if row is None else row[2],
                )
                return True
            failure = f"busy (wal_frames={row[1]}, checkpointed={row[2]})"
        except Exception as exc:  # noqa: BLE001 - the pragma's failure IS the signal
            failure = repr(exc)
        self.checkpoint_failures += 1
        passive_ok = False
        try:
            passive_cursor = await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            await passive_cursor.close()
            passive_ok = True
            self.checkpoint_passive_fallbacks += 1
        except Exception:  # noqa: BLE001 - fallback is best-effort
            passive_ok = False
        log.warning(
            "store_writer_checkpoint_failed",
            error=failure,
            passive_fallback_ok=passive_ok,
            checkpoint_failures=self.checkpoint_failures,
            retry_after_writes=self._CHECKPOINT_RETRY_WRITES,
        )
        return False

    def _emit_writer_stats(self) -> None:
        """Writer observability on the checkpoint cadence (2026-08-19):
        cumulative dropped tape writes, the delta since the last emit, and
        the live queue depth. WARNING whenever tape was dropped since the
        last emit — drop-on-overflow is the DESIGNED hot-path behaviour, but
        it must never again be invisible (~75-80% of a day's tape rows gone
        with nothing reading the counter)."""
        dropped = self._dropped_writes
        delta = dropped - self._dropped_writes_reported
        self._dropped_writes_reported = dropped
        q = self._write_q
        emit = log.warning if delta > 0 else log.info
        emit(
            "store_writer_stats",
            dropped_writes_total=dropped,
            dropped_writes_delta=delta,
            queue_depth=0 if q is None else q.qsize(),
        )

    def _now(self) -> str:
        return self._clock.now().isoformat()

    async def record_rfq(
        self, rfq: Rfq, *, source: str, seen_at: datetime | None = None
    ) -> None:
        """``seen_at``: optional PICKUP wall-time override (risk audit
        2026-07-16). The quote-mode fast-lane records the tape row AFTER
        pricing/dispatch, so it captures the wall-clock at worker pickup and
        passes it here — ``rfqs.seen_at`` keeps its pre-fast-lane meaning
        ("worker pickup, pre-pricing") for every latency instrument that reads
        it (wire→pickup = created_ts→seen_at, pickup→post = seen_at→quote_sent
        at). Default None stamps call time — all other callers unchanged."""
        await self._write(
            "INSERT INTO rfqs (rfq_id, seen_at, source, market_ticker, collection_ticker,"
            " contracts_centi, target_cost_cc, n_legs, legs_json, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rfq.rfq_id,
                seen_at.isoformat() if seen_at is not None else self._now(),
                source,
                rfq.market_ticker,
                rfq.mve_collection_ticker,
                int(rfq.contracts) if rfq.contracts is not None else None,
                int(rfq.target_cost_cc) if rfq.target_cost_cc is not None else None,
                len(rfq.legs),
                json.dumps(
                    [
                        {
                            "market_ticker": leg.market_ticker,
                            "event_ticker": leg.event_ticker,
                            "side": leg.side,
                        }
                        for leg in rfq.legs
                    ]
                ),
                json.dumps(rfq.raw),
            ),
        )

    async def record_rfq_deleted(self, rfq_id: str, raw: JsonDict) -> None:
        await self._write(
            "INSERT INTO rfq_deletions (rfq_id, seen_at, raw_json) VALUES (?, ?, ?)",
            (rfq_id, self._now(), json.dumps(raw)),
        )

    async def record_decision(
        self, kind: str, rfq_id: str | None, reasons: list[str], context: JsonDict
    ) -> None:
        await self._write(
            "INSERT INTO decisions (at, kind, rfq_id, reasons_json, context_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (self._now(), kind, rfq_id, json.dumps(reasons), json.dumps(context)),
        )

    async def record_would_quote(
        self,
        rfq_id: str,
        *,
        fair_prob: float,
        fair_cc: int,
        width_cc: int,
        leg_probs: tuple[float, ...],
        context: JsonDict,
    ) -> None:
        await self._db.execute(
            "INSERT INTO would_quotes (at, rfq_id, fair_prob, fair_cc, width_cc,"
            " leg_probs_json, context_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._now(),
                rfq_id,
                fair_prob,
                fair_cc,
                width_cc,
                json.dumps(list(leg_probs)),
                json.dumps(context),
            ),
        )
        await self._db.commit()

    async def record_would_quote_inplay(
        self,
        rfq_id: str,
        *,
        market_ticker: str,
        fair_cc: int,
        yes_bid_cc: int,
        no_bid_cc: int,
        target_cost_cc: int | None,
        contracts_centi: int | None,
        leg_time_to_start_s: dict[str, float | None],
        context: JsonDict,
    ) -> None:
        """One in-play shadow row (measurement tape, 2026-07-25): the would-be
        quote on an RFQ skipped SOLELY for in-play reasons. Money is int
        centi-cents; ``leg_time_to_start_s`` maps each leg ticker to seconds
        until its scheduled start (NEGATIVE = seconds into the game; None =
        UNKNOWN). Goes through the non-critical ``_write`` tape path — with
        the background writer running it enqueues (drop-on-overflow) and can
        never block or delay the hot pricing path (fix isolation)."""
        await self._write(
            "INSERT INTO would_quotes_inplay (at, rfq_id, market_ticker,"
            " fair_cc, yes_bid_cc, no_bid_cc, target_cost_cc, contracts_centi,"
            " leg_time_to_start_s_json, context_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._now(),
                rfq_id,
                market_ticker,
                fair_cc,
                yes_bid_cc,
                no_bid_cc,
                target_cost_cc,
                contracts_centi,
                json.dumps(leg_time_to_start_s),
                json.dumps(context),
            ),
        )

    async def record_structural_fit(
        self,
        *,
        rfq_id: str | None,
        model: str,
        n_legs: int,
        tickers: tuple[str, ...],
        challenge: FitChallenge,
        family: str = "",
        route: str = "",
        reason: str = "",
    ) -> None:
        """Record a structural inversion's misfit + verdict (P1-4), with the
        combo's leg family and the route taken (structural / hybrid / copula /
        declined) and the inverter's reason on a REJECT — the audit trail for
        systematic structural misfit against the live market.

        Wired 2026-09-04 (item B) from the lifecycle, ONCE per priced RFQ,
        through the non-critical ``_write`` tape path: with the background
        writer running it enqueues (drop-on-overflow) and can never block or
        delay the hot pricing path (fix isolation); in sync mode (tests) it
        writes immediately. Before this it was a committed write with no
        caller (0 rows ever).

        CONTRACT (review note 2026-09-04): these rows are DROPPABLE tape —
        under a queue burst they are lost like any other ``_write`` row
        (``store_writer_stats.dropped_writes`` counts them). The LOSS-FREE
        count of verdicts is the ``structural.fallback.<verdict>[.<family>]``
        metrics counters; any tool computing a fallback SHARE from this table
        must treat it as a sample, not a census."""
        await self._write(
            "INSERT INTO structural_fits (at, rfq_id, model, n_legs,"
            " exactly_identified, residual, verdict, reject_bar, challenge_bar,"
            " tickers_json, family, route, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._now(),
                rfq_id,
                model,
                n_legs,
                1 if challenge.exactly_identified else 0,
                float(challenge.residual),
                challenge.verdict.value,
                float(challenge.reject_bar),
                float(challenge.challenge_bar),
                json.dumps(list(tickers)),
                family,
                route,
                reason,
            ),
        )

    async def record_position_open(
        self,
        position: OpenPosition,
        *,
        subaccount: str,
        fees_cc: int = 0,
    ) -> None:
        """P1.10. Durably record an OPEN position in the ledger: exchange
        quantity/side, cost basis, fees so far, subaccount, status, and the
        order-independent leg-set hash. Keyed on ``position_id`` — an UPSERT so a
        re-recorded open (rehydration / re-poll) is idempotent and never
        duplicates a row NOR clobbers an already-SETTLED status back to open.

        Fail-closed (defense #2): the leg-set hash is derived from the position's
        REAL legs; a leg-less position raises rather than getting a placeholder
        identity. Synchronous & committed like other risk-relevant records — this
        is the source of truth for what we hold, not droppable tape."""
        from combomaker.risk.exposure import leg_set_hash

        lset_hash = leg_set_hash(position.legs)
        legs_json = json.dumps(
            [
                {
                    "market_ticker": leg.market_ticker,
                    "event_ticker": leg.event_ticker,
                    "side": leg.side,
                }
                for leg in position.legs
            ]
        )
        cost_cc = int(position.max_loss_cc)
        # UPSERT: on a replayed open, refresh mutable open-state (fees/legs) but
        # PRESERVE any settlement already recorded — never regress SETTLED→open.
        await self._db.execute(
            "INSERT INTO position_ledger (position_id, opened_at, combo_ticker,"
            " collection_ticker, subaccount, our_side, contracts_centi,"
            " entry_price_cc, cost_cc, fees_cc, leg_set_hash, legs_json, status,"
            " settled_value, realized_pnl_cc, settlement_fee_cc, reconciled_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, NULL, NULL)"
            " ON CONFLICT(position_id) DO UPDATE SET"
            "   fees_cc=excluded.fees_cc,"
            "   legs_json=excluded.legs_json,"
            "   leg_set_hash=excluded.leg_set_hash",
            (
                position.position_id,
                self._now(),
                position.combo_ticker,
                position.collection,
                subaccount,
                position.our_side.value,
                int(position.contracts),
                int(position.entry_price_cc),
                cost_cc,
                int(fees_cc),
                lset_hash,
                legs_json,
            ),
        )
        await self._db.commit()

    async def ensure_open_position_row(
        self,
        position: OpenPosition,
        *,
        subaccount: str,
        fees_cc: int = 0,
    ) -> bool:
        """DURABLE LEDGER IDENTITY — boot keyspace closure (2026-07-26).

        ``record_position_open`` is written by the CONFIRM path only, so every
        position that was filled before the ledger writer existed (or by an
        earlier build) has NO open row — and a settled write can then never
        land on it. On restart ``_rehydrate_exposure_book`` re-mints a NEW
        position id for each exchange-held position; this closes the keyspace
        by writing an OPEN row for any rehydrated position that has none.

        Keyed on the DURABLE identity ``(leg_set_hash, combo_ticker,
        our_side)``, NOT on the volatile position_id: if an open row for the
        same real combo already exists under its original ``fill:<quote_id>``
        id, this is a NO-OP (returns False). Otherwise it inserts the
        re-minted row (returns True). Never duplicates a live position, so the
        open-row count stays equal to the open-position count.

        Fail-closed: a leg-less position has no durable identity —
        ``leg_set_hash`` raises rather than writing a colliding placeholder."""
        from combomaker.risk.exposure import leg_set_hash

        lset_hash = leg_set_hash(position.legs)
        async with self._db.execute(
            "SELECT position_id FROM position_ledger"
            " WHERE status='open' AND leg_set_hash=? AND combo_ticker=?"
            " AND our_side=? LIMIT 1",
            (lset_hash, position.combo_ticker, position.our_side.value),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing is not None:
            return False
        await self.record_position_open(
            position, subaccount=subaccount, fees_cc=fees_cc
        )
        return True

    async def _resolve_open_ledger_row(
        self,
        position_id: str,
        *,
        leg_set_hash: str | None,
        combo_ticker: str | None,
        our_side: str | None,
        contracts_centi: int | None,
    ) -> str | None:
        """The ONE open ledger row a settlement belongs to, by DURABLE identity.

        Match order (never more than one row consumed per settlement, so N
        settlements retire exactly N open rows and realized P&L is never
        double-counted into ``day_realized_pnl_cc``):

          1. the exact ``position_id`` (same process — today's behaviour);
          2. else the stable key ``(leg_set_hash, combo_ticker, our_side)``,
             preferring an exact contract-count match, then the OLDEST row.

        (2) is the restart fix: after ``_rehydrate_exposure_book`` re-mints
        ids, the in-memory ``position_id`` of a held position no longer equals
        the one its open row was written under, so a position_id-only UPDATE
        could never match and EVERY settled write silently vanished."""
        clauses = ["position_id = ?"]
        params: list[object] = [position_id]
        if leg_set_hash and combo_ticker and our_side:
            clauses.append("(leg_set_hash = ? AND combo_ticker = ? AND our_side = ?)")
            params.extend((leg_set_hash, combo_ticker, our_side))
        order = ["(position_id = ?) DESC"]
        order_params: list[object] = [position_id]
        if contracts_centi is not None:
            order.append("(contracts_centi = ?) DESC")
            order_params.append(int(contracts_centi))
        order.extend(("opened_at ASC", "position_id ASC"))
        sql = (
            "SELECT position_id FROM position_ledger WHERE status='open'"
            f" AND ({' OR '.join(clauses)})"
            f" ORDER BY {', '.join(order)} LIMIT 1"
        )
        async with self._db.execute(sql, (*params, *order_params)) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row[0])

    async def record_position_settled(
        self,
        position_id: str,
        *,
        settled_value: float,
        realized_pnl_cc: int,
        settlement_fee_cc: int,
        leg_set_hash: str | None = None,
        combo_ticker: str | None = None,
        our_side: str | None = None,
        contracts_centi: int | None = None,
        reconciled_at: str | None = None,
    ) -> str | None:
        """P1.10. Mark a ledger position SETTLED with the exchange settlement:
        value V, realized P&L, settlement fee, and the reconciliation TIME (now).
        Only transitions an OPEN row — an unknown/already-settled position is a
        no-op (idempotent re-poll), matching the settlement handler's own
        per-id dedup. Synchronous & committed (audit trail).

        DURABLE IDENTITY (2026-07-26 fix): the row is located by
        ``_resolve_open_ledger_row`` — exact ``position_id`` first, else the
        restart-stable key ``(leg_set_hash, combo_ticker, our_side)``. Keying
        on the in-memory position_id ALONE meant that after any restart (which
        re-mints ids as ``rehydrate:<ticker>``) no settled row could ever match
        an open row written pre-restart, so the ledger silently stopped
        recording settlements. Returns the position_id of the row actually
        settled, or None when nothing matched (caller may log the miss).

        ``reconciled_at`` overrides the stamp with the EXCHANGE's own
        ``settled_time`` (2026-07-27). ``day_realized_pnl_cc`` — p_night's
        cross-restart realized anchor — buckets on this column, so a row closed
        today for a combo that settled last night must carry LAST NIGHT's
        stamp or the seed mis-attributes the whole backlog to today. Default
        (None) keeps the live path's behaviour: reconciled now, settled now."""
        target = await self._resolve_open_ledger_row(
            position_id,
            leg_set_hash=leg_set_hash,
            combo_ticker=combo_ticker,
            our_side=our_side,
            contracts_centi=contracts_centi,
        )
        if target is None:
            return None
        await self._db.execute(
            "UPDATE position_ledger SET status='settled', settled_value=?,"
            " realized_pnl_cc=?, settlement_fee_cc=?,"
            " fees_cc=fees_cc + ?, reconciled_at=?"
            " WHERE position_id=? AND status='open'",
            (
                float(settled_value),
                int(realized_pnl_cc),
                int(settlement_fee_cc),
                int(settlement_fee_cc),
                reconciled_at or self._now(),
                target,
            ),
        )
        await self._db.commit()
        return target

    async def open_ledger_rows_for_ticker(self, combo_ticker: str) -> list[JsonDict]:
        """Every OPEN ``position_ledger`` row on one combo ticker, oldest first.
        (``open_ledger_tickers`` below is the batched pre-filter for it.)

        LEDGER RECONCILIATION (2026-07-27). A combo that settled while the
        process was DOWN is absent from ``get_positions`` at the next boot, so
        the exposure book never holds it, so the settlement handler dropped its
        row as "not ours" and the ledger row stayed ``open`` forever (measured:
        56 orphan rows, $775.85 of cost basis, +1..+4 per restart, under-counting
        p_night's realized anchor and the settlement-calibration curve). The
        handler now closes those rows from EXCHANGE TRUTH, and this is the read
        it does it with: everything needed to recompute the position's realized
        P&L under the one payout formula, and nothing else."""
        # MATERIALIZED READ (2026-08-19 checkpoint self-lock fix): fetchall
        # inside the cursor scope, transform outside. A cursor iterated
        # across await points on the shared connection held SQLite's read
        # lock open, which is exactly what made every WAL checkpoint raise
        # SQLITE_LOCKED. Every bounded read in this class follows this shape
        # now (position_ledger and fills are a few thousand rows); the one
        # UNBOUNDED read, ``decision_reason_counts``, pages with fetchmany.
        async with self._db.execute(
            "SELECT position_id, combo_ticker, our_side, contracts_centi,"
            " entry_price_cc, opened_at FROM position_ledger"
            " WHERE status='open' AND combo_ticker=?"
            " ORDER BY opened_at ASC, position_id ASC",
            (combo_ticker,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "position_id": str(r[0]),
                "combo_ticker": str(r[1]),
                "our_side": str(r[2]),
                "contracts_centi": int(r[3]),
                "entry_price_cc": int(r[4]),
                "opened_at": str(r[5]),
            }
            for r in rows
        ]

    async def open_ledger_tickers(self) -> set[str]:
        """Every DISTINCT combo ticker carrying an OPEN ``position_ledger``
        row — ONE indexed read (2026-07-27).

        The orphan reconciliation is a needle-in-a-haystack search: the
        settlement poller re-pages the account's WHOLE settlement history every
        30 s, and only the few hundred tickers in this set can possibly still
        have an open row. Without this the reconciliation would issue one DB
        round-trip per historical settlement, per process."""
        async with self._db.execute(
            "SELECT DISTINCT combo_ticker FROM position_ledger WHERE status='open'"
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(r[0]) for r in rows}

    async def open_ledger_identities(self) -> list[tuple[str, str, str]]:
        """``(leg_set_hash, combo_ticker, our_side)`` of every OPEN ledger row —
        one batched read for the maintenance-tick DIVERGENCE INVARIANT (open
        exposure positions vs open ledger rows). Alarm-only diagnostics; never
        a risk input."""
        async with self._db.execute(
            "SELECT leg_set_hash, combo_ticker, our_side FROM position_ledger"
            " WHERE status='open'"
        ) as cursor:
            rows = await cursor.fetchall()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    async def ledger_position(self, position_id: str) -> JsonDict | None:
        """Read one ledger row by position_id (reports/tests). None if absent."""
        async with self._db.execute(
            "SELECT position_id, opened_at, combo_ticker, collection_ticker,"
            " subaccount, our_side, contracts_centi, entry_price_cc, cost_cc,"
            " fees_cc, leg_set_hash, legs_json, status, settled_value,"
            " realized_pnl_cc, settlement_fee_cc, reconciled_at"
            " FROM position_ledger WHERE position_id = ?",
            (position_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "position_id": row[0],
            "opened_at": row[1],
            "combo_ticker": row[2],
            "collection_ticker": row[3],
            "subaccount": row[4],
            "our_side": row[5],
            "contracts_centi": int(row[6]),
            "entry_price_cc": int(row[7]),
            "cost_cc": int(row[8]),
            "fees_cc": int(row[9]),
            "leg_set_hash": row[10],
            "legs": json.loads(row[11]),
            "status": row[12],
            "settled_value": row[13],
            "realized_pnl_cc": None if row[14] is None else int(row[14]),
            "settlement_fee_cc": None if row[15] is None else int(row[15]),
            "reconciled_at": row[16],
        }

    async def has_fill(self, fill_ref: str) -> bool:
        """True iff a fills row with this ``fill_ref`` already exists — the
        restart-safe idempotency read the lifecycle uses to skip a REPLAYED
        execution (WS replay, or the 2026-07-16 recovery sweep's REST poll
        racing the WS message) before it books fees/metrics twice."""
        async with self._db.execute(
            "SELECT 1 FROM fills WHERE fill_ref = ? LIMIT 1", (fill_ref,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def has_fill_for_order_id(self, order_id: str) -> bool:
        """True iff a fills row already records this exchange ``order_id`` —
        the verify-before-discard ADOPTION GUARD (2026-07-18 review). An
        exchange fill whose order is already in the local ledger belongs to an
        EARLIER quote and must never be adopted for a second one: the live
        tape holds same-ticker/same-side/same-exact-count fills hours apart
        (rows 59/61, both 4071 centi-ct NO on one combo), so a structural
        match alone can hit a HISTORICAL fill and double-count it."""
        async with self._db.execute(
            "SELECT 1 FROM fills WHERE order_id = ? LIMIT 1", (order_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def fill_ref_for_order_id(self, order_id: str) -> tuple[str, str] | None:
        """``(fill_ref, status)`` of the fills row recording this exchange
        ``order_id``, or None (2026-09-04 build, item D). The writer's
        order-id guard uses this instead of the bare boolean so a REPLAY of
        the SAME quote (fill_ref equal — the 2026-08-26 run logged 38 false
        ``fill_order_id_already_in_ledger`` errors, every one a WS+poll race
        on one quote, zero cross-quote misattributions) is told apart from a
        genuine second quote claiming an already-booked order."""
        async with self._db.execute(
            "SELECT fill_ref, status FROM fills WHERE order_id = ? LIMIT 1",
            (order_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1])

    async def fill_status(self, fill_ref: str) -> str | None:
        """Verification state of one fills row (booked|verified|phantom), or
        None when no row exists."""
        async with self._db.execute(
            "SELECT status FROM fills WHERE fill_ref = ? LIMIT 1", (fill_ref,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else str(row[0])

    async def mark_fill_verified(
        self, fill_ref: str, *, exchange_fill_id: str | None
    ) -> bool:
        """Stamp a BOOKED fills row VERIFIED: its exchange order_id was found
        on /portfolio/fills (2026-09-04 build, item D). Evidence columns:
        ``exchange_fill_id`` (the tape row's fill_id) + ``verified_at`` (now).
        Only a ``booked`` row transitions (idempotent re-verification is a
        no-op; a voided phantom is never resurrected here — a tape fill that
        appears AFTER a void is the fills-ledger sweep's alarm to raise).
        Returns True iff a row was stamped."""
        cursor = await self._db.execute(
            "UPDATE fills SET status = ?, verified_at = ?, exchange_fill_id = ?"
            " WHERE fill_ref = ? AND status = ?",
            (
                self.FILL_STATUS_VERIFIED,
                self._now(),
                exchange_fill_id,
                fill_ref,
                self.FILL_STATUS_BOOKED,
            ),
        )
        await self._db.commit()
        return (cursor.rowcount or 0) > 0

    async def void_phantom_fill(self, fill_ref: str, *, reason: str) -> dict[str, int]:
        """VOID a phantom execution across the three ledgers (2026-09-04
        build, item D): the exchange said ``executed`` but /portfolio/fills
        holds NO row for the order after the bounded verification — the fill
        never happened, so nothing about it may keep counting.

          * ``fills``: status → ``phantom`` (the row STAYS as the audit trail
            of what the exchange told us; ``raw_json`` keeps the executed
            message; ``verified_at`` stamps the void; ``exchange_fill_id``
            records the reason);
          * ``position_ledger``: the OPEN row keyed ``position_id == fill_ref``
            → status ``phantom`` (never 'open' — it can no longer be matched
            by a settlement — and never 'settled' — it realized nothing);
          * ``ev_ledger``: expected_edge_cc → 0 and realized_pnl_cc → 0 so the
            EV grading counts it as exactly nothing on both sides.

        Only a ``booked`` fills row is voided (a VERIFIED row is a real fill by
        proof; a phantom row is already voided) — the caller decides on
        evidence, this method enforces the state machine. Synchronous +
        committed like every other risk-relevant write. Returns the rows
        touched per table (tests + the log line)."""
        touched = {"fills": 0, "position_ledger": 0, "ev_ledger": 0}
        cursor = await self._db.execute(
            "UPDATE fills SET status = ?, verified_at = ?, exchange_fill_id = ?"
            " WHERE fill_ref = ? AND status = ?",
            (
                self.FILL_STATUS_PHANTOM,
                self._now(),
                f"phantom:{reason}",
                fill_ref,
                self.FILL_STATUS_BOOKED,
            ),
        )
        touched["fills"] = cursor.rowcount if cursor.rowcount > 0 else 0
        if touched["fills"] == 0:
            await self._db.commit()
            return touched
        cursor = await self._db.execute(
            "UPDATE position_ledger SET status = ?, reconciled_at = ?"
            " WHERE position_id = ? AND status = 'open'",
            (self.POSITION_STATUS_PHANTOM, self._now(), fill_ref),
        )
        touched["position_ledger"] = cursor.rowcount if cursor.rowcount > 0 else 0
        cursor = await self._db.execute(
            "UPDATE ev_ledger SET expected_edge_cc = 0, realized_pnl_cc = 0"
            " WHERE fill_ref = ?",
            (fill_ref,),
        )
        touched["ev_ledger"] = cursor.rowcount if cursor.rowcount > 0 else 0
        await self._db.commit()
        return touched

    async def fills_verification_watermark(self) -> tuple[int, str] | None:
        """``(max_fills_id_at_migration, migrated_at_iso)`` — the once-written
        store_meta facts from ``_ensure_fills_verification_columns`` (2026-09-04
        review fixes), or None when the migration never completed (a store
        opened while the columns could not be added). Cached after the first
        successful read: the value never changes for the life of a store."""
        cached = self._fills_verification_watermark
        if cached is not None:
            return cached
        async with self._db.execute(
            "SELECT key, value FROM store_meta WHERE key IN (?, ?)",
            (
                self.META_FILLS_VERIFICATION_WATERMARK_ID,
                self.META_FILLS_VERIFICATION_MIGRATED_AT,
            ),
        ) as cursor:
            rows = {str(row[0]): str(row[1]) for row in await cursor.fetchall()}
        wm_raw = rows.get(self.META_FILLS_VERIFICATION_WATERMARK_ID)
        at = rows.get(self.META_FILLS_VERIFICATION_MIGRATED_AT)
        if wm_raw is None or at is None:
            return None
        try:
            watermark = (int(wm_raw), at)
        except ValueError:
            return None
        self._fills_verification_watermark = watermark
        return watermark

    async def booked_unverified_fills(self, *, after_id: int) -> list[JsonDict]:
        """Every fills row STILL ``booked`` (never verified nor voided) that
        carries an exchange ``order_id`` and was written AFTER the verification
        watermark (``id > after_id``) — the claims a crashed process left
        unproven (2026-09-04 review fixes: three multi-day outages this month;
        8/26 had 16 boots, so a ~30-180 s in-memory verification window is
        routinely cut short). The restart re-arm verifies exactly these on the
        same cadence. Rows at or below the watermark are legacy (their proof is
        the settlement-corroborated repair tool — an exact order_id lookup
        would wrongly void the three 2026-07-18 truncated-id rows). Ordered
        oldest-first; the caller bounds the work per tick."""
        async with self._db.execute(
            "SELECT id, at, fill_ref, order_id, combo_ticker, our_side,"
            " contracts_centi, fee_cc FROM fills"
            " WHERE status = ? AND order_id IS NOT NULL AND id > ? ORDER BY id",
            (self.FILL_STATUS_BOOKED, int(after_id)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": int(row[0]),
                "at": str(row[1]),
                "fill_ref": str(row[2]),
                "order_id": str(row[3]),
                "combo_ticker": str(row[4]),
                "our_side": str(row[5]),
                "contracts_centi": int(row[6]),
                "fee_cc": None if row[7] is None else int(row[7]),
            }
            for row in rows
        ]

    async def open_ledger_quantity_by_ticker(
        self, *, post_fix_since: str | None = None
    ) -> dict[str, tuple[str, int, int, int]]:
        """``{combo_ticker: (our_side, Σ contracts_centi, n_rows, n_post_fix)}``
        over every OPEN ``position_ledger`` row — one grouped read for the
        per-ticker LEDGER-vs-EXCHANGE quantity reconcile (2026-09-04 build,
        item D; alarm-only). ``n_post_fix`` counts the rows opened at or after
        ``post_fix_since`` (the verification migration stamp — review fixes:
        the live store carries 434 legacy stale open rows that would otherwise
        bury a NEW phantom in the alarm every 5 min); None ⇒ every row counts
        as post-fix. A ticker whose open rows disagree on side is reported
        under the side of its FIRST row with the summed magnitude — the
        divergence alarm fires either way."""
        since = post_fix_since if post_fix_since is not None else ""
        async with self._db.execute(
            "SELECT combo_ticker, our_side, SUM(contracts_centi), COUNT(*),"
            " SUM(CASE WHEN opened_at >= ? THEN 1 ELSE 0 END)"
            " FROM position_ledger WHERE status='open'"
            " GROUP BY combo_ticker, our_side ORDER BY combo_ticker, our_side",
            (since,),
        ) as cursor:
            rows = await cursor.fetchall()
        out: dict[str, tuple[str, int, int, int]] = {}
        for row in rows:
            ticker = str(row[0])
            side, total, n = str(row[1]), int(row[2] or 0), int(row[3] or 0)
            n_post = int(row[4] or 0)
            if ticker in out:
                prev_side, prev_total, prev_n, prev_post = out[ticker]
                out[ticker] = (prev_side, prev_total + total, prev_n + n, prev_post + n_post)
            else:
                out[ticker] = (side, total, n, n_post)
        return out

    async def day_realized_pnl_cc(self, start_iso: str, end_iso: str) -> int:
        """DAY-ANCHORED realized P&L reconstruction (2026-07-25 operator KPI:
        p_night must roll across restarts — the in-process accumulator resets
        at boot). Mirrors exactly what ``record_realized_pnl`` accumulates in
        one process: Σ settlement ``realized_pnl_cc`` reconciled in the window
        plus Σ(−fill fee) for fills in the window. ISO-UTC string bounds
        (lexicographic — the ledger stamps are tz-aware isoformat)."""
        async with self._db.execute(
            "SELECT COALESCE(SUM(realized_pnl_cc), 0) FROM position_ledger"
            " WHERE reconciled_at IS NOT NULL"
            " AND reconciled_at >= ? AND reconciled_at < ?",
            (start_iso, end_iso),
        ) as cursor:
            row = await cursor.fetchone()
            settled = int(row[0]) if row and row[0] is not None else 0
        # A VOIDED phantom's fee never left the account (no fill happened) and
        # the in-process accumulator reversed it at void time — exclude it here
        # too so the restart-seeded figure matches (2026-09-04 build, item D).
        async with self._db.execute(
            "SELECT COALESCE(SUM(fee_cc), 0) FROM fills"
            " WHERE fee_cc IS NOT NULL AND at >= ? AND at < ? AND status != 'phantom'",
            (start_iso, end_iso),
        ) as cursor:
            row = await cursor.fetchone()
            fees = int(row[0]) if row and row[0] is not None else 0
        return settled - fees

    async def fill_order_ids(self) -> set[str]:
        """Every non-NULL exchange ``order_id`` in the fills ledger — one read
        per fills-ledger sweep (2026-07-24 incident-C review: hundreds of
        serial point-reads inside the maintenance tick were a wedge risk; the
        table is small, so one batched SELECT replaces them all)."""
        # A VOIDED phantom row's order_id is deliberately NOT "in the ledger"
        # here (2026-09-04 build, item D): if the exchange later shows a fill
        # for that order, the sweep must alarm it as a MISS (the void was
        # wrong) rather than treat the voided row as its record.
        # ASYMMETRY, on purpose (2026-09-04 review fixes): ``has_fill_for_
        # order_id`` above still COUNTS a phantom row, so the same late print
        # is ``already_in_ledger`` to ``_adopt_exchange_fill`` and can never be
        # re-adopted for another quote — the voided row keeps its order_id
        # claim (one exchange order = one row) while this read keeps the miss
        # LOUD. Automatic un-void is not built; the alarm is the operator's cue
        # to un-void by hand (the row and its raw_json are intact).
        async with self._db.execute(
            "SELECT DISTINCT order_id FROM fills WHERE order_id IS NOT NULL"
            " AND status != 'phantom'"
        ) as cursor:
            rows = await cursor.fetchall()
        return {str(row[0]) for row in rows}

    async def fill_null_order_id_keys(self) -> set[tuple[str, int]]:
        """(combo_ticker, contracts_centi) of every fills row WITHOUT an
        exchange order_id (poll-recovered rows whose quote payload exposed no
        creator_order_id). The fills-ledger sweep matches a tape row against
        these BEFORE alarming, so a legitimately-recorded-but-unkeyed fill is
        a visible skip, not a permanent false alarm pinning the watermark."""
        async with self._db.execute(
            "SELECT combo_ticker, contracts_centi FROM fills "
            "WHERE order_id IS NULL AND status != 'phantom'"
        ) as cursor:
            rows = await cursor.fetchall()
        return {(str(row[0]), int(row[1])) for row in rows}

    async def settled_grade_rows(self) -> list[JsonDict]:
        """The SETTLED grade per combo ticker for the measured retained-edge
        floor (risk/retained_edge_floor.py, 2026-09-04): every settled
        ``position_ledger`` ticker with its contracts, realized P&L (net of
        the exchange settlement fee), the fee columns, one leg set, and the
        fills' recorded expected edge + booked fee summed over the ticker.
        ONE batched read of two small tables (a few thousand rows) on the
        slow loop — never per-row point reads, never the pricing path."""
        ledger_q = (
            "SELECT combo_ticker, SUM(contracts_centi), SUM(realized_pnl_cc),"
            " SUM(COALESCE(settlement_fee_cc, 0)), MIN(opened_at),"
            " MAX(COALESCE(reconciled_at, opened_at)), MAX(legs_json)"
            " FROM position_ledger WHERE status = 'settled'"
            " AND realized_pnl_cc IS NOT NULL GROUP BY combo_ticker"
        )
        fills_q = (
            "SELECT combo_ticker, SUM(contracts_centi), SUM(expected_edge_cc),"
            " SUM(COALESCE(fee_cc, 0)), SUM(expected_edge_cc IS NULL)"
            " FROM fills GROUP BY combo_ticker"
        )
        async with self._db.execute(fills_q) as cursor:
            fill_rows = await cursor.fetchall()
        fills = {str(r[0]): r for r in fill_rows}
        out: list[JsonDict] = []
        async with self._db.execute(ledger_q) as cursor:
            ledger_rows = await cursor.fetchall()
        for ticker, ctr, realized, settle_fee, opened_at, settled_at, legs_json in ledger_rows:
            f = fills.get(str(ticker))
            if f is None or f[2] is None or int(f[4] or 0) > 0 or not int(f[1] or 0):
                continue  # no recorded model edge for this ticker: cannot grade it
            out.append(
                {
                    "combo_ticker": str(ticker),
                    "ledger_contracts_centi": int(ctr),
                    "realized_pnl_cc": int(realized),
                    "settlement_fee_cc": int(settle_fee or 0),
                    "opened_at": str(opened_at),
                    "settled_at": str(settled_at),
                    "legs_json": str(legs_json or "[]"),
                    "fill_contracts_centi": int(f[1]),
                    "expected_edge_cc": int(f[2]),
                    "fill_fee_cc": int(f[3] or 0),
                }
            )
        return out

    async def has_fill_for_ticker(self, combo_ticker: str) -> bool:
        """True iff ANY fills row exists for this combo ticker. Used by the
        periodic position-reconcile net (2026-07-18): an exchange position the
        in-memory book does not model is alarmed either way, but whether a
        local fill record exists distinguishes "our own fill fell out of the
        book" (the 2026-07-18 fill-recovery incidents) from "a manual/external
        trade we never saw"."""
        async with self._db.execute(
            "SELECT 1 FROM fills WHERE combo_ticker = ? LIMIT 1", (combo_ticker,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def record_fill(
        self,
        fill_ref: str,
        *,
        order_id: str | None,
        combo_ticker: str,
        our_side: str,
        contracts_centi: int,
        price_cc: int,
        fee_cc: int | None,
        expected_edge_cc: int | None,
        raw: JsonDict,
    ) -> bool:
        """Record a fill EXACTLY ONCE per ``fill_ref`` (2026-07-16 P1).

        INSERT-if-absent in a single statement (atomic within this
        transaction, restart-safe): a WS+poll race — the exchange's
        quote_executed message and the recovery sweep's REST poll replaying the
        same fill — can never double-insert, even if both callers passed a
        ``has_fill`` pre-check before either wrote. The EV-ledger row rides the
        same guard (inserted only when the fills row was). Returns True iff the
        row was inserted (False ⇒ a fill with this ref already existed and
        NOTHING was written).

        ONE EXCHANGE ORDER = ONE ROW (2026-09-04 build, item D): the same
        single-statement guard also refuses a row whose non-NULL ``order_id``
        is already recorded under ANY fill_ref — the store-level twin of the
        writer's order-id pre-check and of the partial UNIQUE index, so a
        race that slips both pre-checks lands as a silent no-op (False),
        never an IntegrityError into the executed handler."""
        cursor = await self._db.execute(
            "INSERT INTO fills (at, fill_ref, order_id, combo_ticker, our_side,"
            " contracts_centi, price_cc, fee_cc, expected_edge_cc, raw_json, status)"
            " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
            " WHERE NOT EXISTS (SELECT 1 FROM fills WHERE fill_ref = ?)"
            " AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM fills WHERE order_id = ?))",
            (
                self._now(),
                fill_ref,
                order_id,
                combo_ticker,
                our_side,
                contracts_centi,
                price_cc,
                fee_cc,
                expected_edge_cc,
                json.dumps(raw),
                self.FILL_STATUS_BOOKED,
                fill_ref,
                order_id,
                order_id,
            ),
        )
        inserted = (cursor.rowcount or 0) > 0
        if inserted and expected_edge_cc is not None:
            await self._db.execute(
                "INSERT INTO ev_ledger (at, fill_ref, expected_edge_cc, realized_pnl_cc)"
                " VALUES (?, ?, ?, NULL)",
                (self._now(), fill_ref, expected_edge_cc),
            )
        await self._db.commit()
        return inserted

    async def record_markout(
        self,
        fill_ref: str,
        *,
        horizon_s: float,
        fair_at_fill_cc: int | None,
        fair_now_cc: int | None,
        raw_mid_at_fill_cc: int | None,
        raw_mid_now_cc: int | None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO markouts (at, fill_ref, horizon_s, fair_at_fill_cc, fair_now_cc,"
            " raw_mid_at_fill_cc, raw_mid_now_cc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._now(),
                fill_ref,
                horizon_s,
                fair_at_fill_cc,
                fair_now_cc,
                raw_mid_at_fill_cc,
                raw_mid_now_cc,
            ),
        )
        await self._db.commit()

    async def record_combo_trades(self, ticker: str, trades: list[JsonDict]) -> int:
        """Store public combo-market trades (deduped on trade_id). This is the
        implied-markup dataset: executed RFQ prices vs our shadow fairs."""
        stored = 0
        for trade in trades:
            trade_id = str(trade.get("trade_id") or trade.get("fill_id") or "")
            if not trade_id:
                continue
            price_raw = trade.get("yes_price_dollars") or trade.get("yes_price")
            try:
                from combomaker.core.money import cc_from_dollars_str
                from combomaker.core.quantity import qty_from_fp_str

                price_cc = int(cc_from_dollars_str(str(price_raw))) if price_raw else None
                count_raw = trade.get("count_fp") or trade.get("count")
                count_centi = int(qty_from_fp_str(str(count_raw))) if count_raw else None
            except ValueError:
                price_cc = None
                count_centi = None
            cursor = await self._db.execute(
                "INSERT OR IGNORE INTO combo_trades (trade_id, seen_at, ticker,"
                " created_time, yes_price_cc, count_centi, taker_side, raw_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id,
                    self._now(),
                    ticker,
                    trade.get("created_time"),
                    price_cc,
                    count_centi,
                    trade.get("taker_side"),
                    json.dumps(trade),
                ),
            )
            stored += cursor.rowcount if cursor.rowcount > 0 else 0
        await self._db.commit()
        return stored

    async def settle_ev_entry(self, fill_ref: str, realized_pnl_cc: int) -> None:
        await self._db.execute(
            "UPDATE ev_ledger SET realized_pnl_cc = ? WHERE fill_ref = ?",
            (realized_pnl_cc, fill_ref),
        )
        await self._db.commit()

    # --- simple readers for reports/tests ---

    async def count(self, table: str) -> int:
        if table not in {
            "rfqs",
            "rfq_deletions",
            "decisions",
            "would_quotes",
            "would_quotes_inplay",
            "fills",
            "markouts",
            "ev_ledger",
            "structural_fits",
            "position_ledger",
        }:
            raise ValueError(f"unknown table {table!r}")
        async with self._db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:  # noqa: S608
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def _ledger_legsets(
        self, tickers: list[str]
    ) -> dict[str, dict[str, Any]]:
        """DURABLE leg provenance from ``position_ledger`` (BOOK COMPLETENESS,
        2026-07-26). ``combo_ticker -> {legs_json: collection_ticker}`` keyed on
        the ORDER-INDEPENDENT ``leg_set_hash``, so two ledger rows that spell the
        same leg set in a different JSON order are ONE identity (not a false
        conflict). No status filter: a combo ticker's leg definition is immutable
        for the life of the market, so a settled row is still valid provenance for
        a re-opened position on the same ticker.

        Why this exists: ``rfqs`` is an OBSERVABILITY tape — best-effort, and
        measurably lossy (2026-07-26: 8 held combos, $40.24 of premium at risk,
        had ZERO rfqs rows), while ``position_ledger`` is written synchronously
        and committed on every confirmed fill. Risk-book completeness must never
        depend on a tape that is allowed to drop rows."""
        placeholders = ",".join("?" * len(tickers))
        legsets: dict[str, dict[str, Any]] = {}
        # leg_set_hash is the durable identity; legs_json/collection are payload.
        async with self._db.execute(
            "SELECT combo_ticker, leg_set_hash, MAX(legs_json) AS legs_json,"
            " MAX(collection_ticker) AS collection_ticker"
            f" FROM position_ledger WHERE combo_ticker IN ({placeholders})"  # noqa: S608 - placeholders only
            " GROUP BY combo_ticker, leg_set_hash",
            tuple(tickers),
        ) as cursor:
            rows = await cursor.fetchall()
        for combo_ticker, _hash, legs_json, collection in rows:
            if not legs_json:
                continue
            legsets.setdefault(combo_ticker, {})[legs_json] = collection
        return legsets

    async def held_positions(self, combo_tickers: list[str]) -> list[JsonDict]:
        """Rehydration source for the exposure book on restart (#33). For each combo
        ticker still OPEN on the exchange, aggregate our recorded fills (summed
        contracts + a max-loss-preserving entry price) and attach the combo's legs.

        LEG PROVENANCE IS LEDGER-FIRST (BOOK COMPLETENESS, 2026-07-26). Legs come
        from the DURABLE ``position_ledger`` (written+committed by
        ``record_position_open`` on every confirmed fill) and fall back to the
        ``rfqs`` OBSERVABILITY tape only for tickers the ledger cannot resolve. The
        old tape-only lookup was fail-OPEN: a combo whose RFQ row never landed had
        no resolvable legs, so the rehydrator dropped it AND the runtime reconcile
        skipped it (a local fills row exists ⇒ "the recovery sweep owns it", but
        that sweep only ever re-models THIS run's quotes) — the position counted in
        NEITHER path and its premium vanished from ``deterministic_max_loss_cc``.
        Each returned row carries ``legs_source`` ("position_ledger" | "rfqs_tape")
        so the caller can log which durable source answered.

        Only tickers we have BOTH a fill AND a resolvable leg set for are returned;
        an exchange position whose legs no durable source can resolve is surfaced to
        the caller, which reserves it from EXCHANGE figures (never modeled from a
        guess, and never zero). Entry price is chosen so
        ``contracts × entry_price // 100`` equals the summed per-fill max loss (the
        loss axis the caps bind on)."""
        tickers = list(dict.fromkeys(combo_tickers))
        if not tickers:
            return []
        placeholders = ",".join("?" * len(tickers))
        # The rfqs tape holds MANY rows per combo_ticker (one per re-quote — up to
        # tens of thousands). A naive ``fills JOIN rfqs`` fans each fill out by that
        # count BEFORE the SUM, inflating contracts_centi (and every risk cap that
        # scales with it) by the fanout factor. Aggregate fills so the fills side is
        # exactly one row per (combo_ticker, our_side). (entry_price was fanout-safe
        # before — numerator and denominator scaled together — but contracts_centi
        # was not; that de-dup is the earlier fix.)
        fills_q = (
            "SELECT combo_ticker, our_side, SUM(contracts_centi) AS ctr,"
            " SUM(contracts_centi * price_cc) AS loss_num"
            f" FROM fills WHERE combo_ticker IN ({placeholders})"  # noqa: S608 - ints-only placeholders
            " GROUP BY combo_ticker, our_side"
        )
        # P1.11 — EXACT ORIGINATING LEG-SET IDENTITY, not MAX(legs_json) provenance.
        # The old ``MAX(legs_json)`` silently picked the lexicographically-largest
        # leg definition when the tape held MORE THAN ONE distinct leg-set for a
        # market_ticker — a provenance guess that could rehydrate a position with the
        # WRONG legs (poisoning clustering / mutex / marginals) and hide the conflict.
        # Instead pull the DISTINCT leg-sets per ticker and resolve fail-closed:
        # exactly one distinct legs_json ⇒ that is the identity; two or more ⇒ the
        # provenance is ambiguous ⇒ REJECT the ticker (never rehydrated from a guess),
        # exactly as the exchange-reconcile path drops a position it cannot model.
        # (1) DURABLE source first. Whatever the ledger answers is authoritative
        # provenance and the tape is never consulted for that ticker — which also
        # shrinks the (large, index-scanned) rfqs lookup to the leftovers.
        ledger_legsets = await self._ledger_legsets(tickers)
        tape_needed = [t for t in tickers if t not in ledger_legsets]
        # market_ticker -> {legs_json: collection_ticker} across DISTINCT leg-sets.
        tape_legsets: dict[str, dict[str, Any]] = {}
        if tape_needed:
            tape_placeholders = ",".join("?" * len(tape_needed))
            legs_q = (
                "SELECT market_ticker, legs_json,"
                " MAX(collection_ticker) AS collection_ticker"
                f" FROM rfqs WHERE market_ticker IN ({tape_placeholders})"  # noqa: S608 - placeholders only
                " GROUP BY market_ticker, legs_json"
            )
            async with self._db.execute(legs_q, tuple(tape_needed)) as cursor:
                legs_rows = await cursor.fetchall()
            for market_ticker, legs_json, collection in legs_rows:
                if not legs_json:
                    continue
                tape_legsets.setdefault(market_ticker, {})[legs_json] = collection

        out: list[JsonDict] = []
        async with self._db.execute(fills_q, tuple(tickers)) as cursor:
            fills_rows = await cursor.fetchall()
        for combo_ticker, our_side, ctr, loss_num in fills_rows:
            if not ctr:
                continue
            # LEDGER-FIRST: the durable ledger wins over the lossy tape.
            distinct = ledger_legsets.get(combo_ticker)
            source = "position_ledger"
            if not distinct:
                distinct = tape_legsets.get(combo_ticker)
                source = "rfqs_tape"
            if not distinct:
                # No leg definition in ANY durable source ⇒ cannot model ⇒ not
                # rehydrated here. The caller RESERVES it from exchange figures
                # (unknown legs must never mean zero exposure).
                continue
            if len(distinct) > 1:
                # CONFLICTING leg definitions for the same combo ticker: the
                # originating identity is ambiguous. Fail closed — reject rather
                # than guess (the caller then reserves it from exchange figures).
                log.warning(
                    "held_positions.conflicting_leg_sets",
                    combo_ticker=combo_ticker,
                    distinct_leg_sets=len(distinct),
                    source=source,
                )
                continue
            legs_json, collection = next(iter(distinct.items()))
            out.append(
                {
                    "combo_ticker": combo_ticker,
                    "our_side": our_side,
                    "contracts_centi": int(ctr),
                    "entry_price_cc": int(loss_num) // int(ctr),
                    "collection": collection,
                    "legs": json.loads(legs_json),
                    "legs_source": source,
                }
            )
        return out

    async def decision_reason_counts(self) -> dict[str, int]:
        # The ONE unbounded read here (decisions grows without bound on the
        # live DB), so it pages with fetchmany rather than materializing —
        # a fetchall could hold the whole table in memory. The statement
        # stays open between chunks, but each chunk's await is bounded and
        # the checkpoint no longer shares this connection (2026-08-19).
        counts: dict[str, int] = {}
        cursor = await self._db.execute("SELECT reasons_json FROM decisions")
        try:
            while True:
                rows = await cursor.fetchmany(10_000)
                if not rows:
                    break
                for row in rows:
                    for reason in json.loads(row[0]):
                        counts[reason] = counts.get(reason, 0) + 1
        finally:
            await cursor.close()
        return counts

    async def decision_kind_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self._db.execute(
            "SELECT kind, COUNT(*) FROM decisions GROUP BY kind"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            counts[str(row[0])] = int(row[1])
        return counts

    async def ev_summary(self) -> dict[str, object]:
        """Aggregate EV grading over the ev_ledger. A VOIDED phantom's row is
        EXCLUDED (2026-09-04 review fixes): ``void_phantom_fill`` zeroes its
        expected/realized, but a 0/0 row still counted as n+1 in a row-based
        grade — a fill that never happened must not be graded at all."""
        async with self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(e.expected_edge_cc), 0),"
            " COUNT(e.realized_pnl_cc), COALESCE(SUM(e.realized_pnl_cc), 0)"
            " FROM ev_ledger e WHERE NOT EXISTS ("
            "SELECT 1 FROM fills f WHERE f.fill_ref = e.fill_ref AND f.status = 'phantom')"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return {
            "fills": int(row[0]),
            "expected_edge_cc": int(row[1]),
            "settled": int(row[2]),
            "realized_pnl_cc": int(row[3]),
        }

    async def markout_summary(self) -> list[dict[str, object]]:
        """Mean fair/raw-mid drift per horizon WITH sample counts — markout
        stats without an n are noise dressed up as signal."""
        out: list[dict[str, object]] = []
        async with self._db.execute(
            "SELECT horizon_s,"
            " COUNT(*),"
            " AVG(fair_now_cc - fair_at_fill_cc),"
            " AVG(raw_mid_now_cc - raw_mid_at_fill_cc)"
            " FROM markouts"
            " WHERE fair_now_cc IS NOT NULL AND fair_at_fill_cc IS NOT NULL"
            " GROUP BY horizon_s ORDER BY horizon_s"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            out.append(
                {
                    "horizon_s": float(row[0]),
                    "n": int(row[1]),
                    "mean_fair_drift_cc": None if row[2] is None else float(row[2]),
                    "mean_raw_mid_drift_cc": None if row[3] is None else float(row[3]),
                }
            )
        return out
