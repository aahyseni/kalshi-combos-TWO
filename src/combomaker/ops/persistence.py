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
    raw_json TEXT NOT NULL
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
    tickers_json TEXT NOT NULL
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
"""


class Store:
    # Manual WAL checkpoint cadence (writes between attempts) and the
    # ACCELERATED retry cadence after a failed/busy checkpoint (2026-07-18:
    # 'database table is locked' was observed on EVERY attempt of a live run —
    # a long-lived read cursor starves the TRUNCATE — and the old shared
    # try/except waited another full 5000 writes while the WAL grew 78→194MB
    # in ~40min). Class attributes so tests can tighten them per instance.
    _CHECKPOINT_EVERY_WRITES = 5000
    _CHECKPOINT_RETRY_WRITES = 500

    def __init__(self, db: aiosqlite.Connection, clock: Clock) -> None:
        self._db = db
        self._clock = clock
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
        # WAL-checkpoint health counters (2026-07-18): failed/busy TRUNCATE
        # attempts and PASSIVE fallbacks that ran. Public so an ops surface can
        # report them alongside dropped writes.
        self.checkpoint_failures = 0
        self.checkpoint_passive_fallbacks = 0

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
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=5000")
        # autocheckpoint OFF (2026-07-14): with it ON, a 2000-page checkpoint
        # fired INLINE on every writer commit that crossed the threshold — on the
        # ~2GB DB that ran near-continuously during bursts, so the background
        # writer fell behind and DROPPED ~96% of the tape (18,759 quotes posted,
        # 603 recorded) → the live viewer went blind → PHANTOM blocks. The writer
        # now runs a BOUNDED manual checkpoint every ~5000 writes instead.
        await db.execute("PRAGMA wal_autocheckpoint=0")
        await db.executescript(_DDL)
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
        return cls(db, clock)

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
            for _ in batch:
                q.task_done()

    async def _wal_checkpoint(self) -> bool:
        """ONE bounded manual checkpoint attempt (writer task only). Returns
        True iff the TRUNCATE fully completed (the WAL was reset).

        A raised error ('database table is locked') AND a busy verdict (the
        pragma's first result column — TRUNCATE that could not finish reports
        busy=1 WITHOUT raising, which the old code silently counted as success)
        both take the failure path: count + log ``store_writer_checkpoint_failed``
        (its own event — never confused with a batch failure), then attempt a
        PASSIVE checkpoint before giving up the cycle. PASSIVE copies what it
        can without blocking readers, so the WAL keeps getting folded even
        while the TRUNCATE lock is starved by a long-lived cursor."""
        failure: str
        try:
            cursor = await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            row = await cursor.fetchone()
            if row is None or not row[0]:
                return True
            failure = f"busy (wal_frames={row[1]}, checkpointed={row[2]})"
        except Exception as exc:  # noqa: BLE001 - the pragma's failure IS the signal
            failure = repr(exc)
        self.checkpoint_failures += 1
        passive_ok = False
        try:
            await self._db.execute("PRAGMA wal_checkpoint(PASSIVE)")
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

    async def record_structural_fit(
        self,
        *,
        rfq_id: str | None,
        model: str,
        n_legs: int,
        tickers: tuple[str, ...],
        challenge: FitChallenge,
    ) -> None:
        """Durably record a structural inversion's misfit + its challenge verdict
        (P1-4). Synchronous & committed like other risk-relevant records — this
        is the audit trail for systematic structural misfit against the live
        market, so it must not be droppable tape."""
        await self._db.execute(
            "INSERT INTO structural_fits (at, rfq_id, model, n_legs,"
            " exactly_identified, residual, verdict, reject_bar, challenge_bar,"
            " tickers_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        await self._db.commit()

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

    async def record_position_settled(
        self,
        position_id: str,
        *,
        settled_value: float,
        realized_pnl_cc: int,
        settlement_fee_cc: int,
    ) -> None:
        """P1.10. Mark a ledger position SETTLED with the exchange settlement:
        value V, realized P&L, settlement fee, and the reconciliation TIME (now).
        Only transitions an existing OPEN row — an unknown/already-settled
        position_id is a no-op (idempotent re-poll), matching the settlement
        handler's own per-id dedup. Synchronous & committed (audit trail)."""
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
                self._now(),
                position_id,
            ),
        )
        await self._db.commit()

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
        NOTHING was written)."""
        cursor = await self._db.execute(
            "INSERT INTO fills (at, fill_ref, order_id, combo_ticker, our_side,"
            " contracts_centi, price_cc, fee_cc, expected_edge_cc, raw_json)"
            " SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
            " WHERE NOT EXISTS (SELECT 1 FROM fills WHERE fill_ref = ?)",
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
                fill_ref,
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

    async def held_positions(self, combo_tickers: list[str]) -> list[JsonDict]:
        """Rehydration source for the exposure book on restart (#33). For each combo
        ticker still OPEN on the exchange, aggregate our recorded fills (summed
        contracts + a max-loss-preserving entry price) and attach the combo's legs
        from the rfqs tape (``fills.combo_ticker == rfqs.market_ticker``). Only
        tickers we have BOTH a fill AND an rfq for are returned; an exchange
        position with no local record is surfaced by the caller, never modeled from
        a guess. Entry price is chosen so ``contracts × entry_price // 100`` equals
        the summed per-fill max loss (the loss axis the caps bind on)."""
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
        legs_q = (
            "SELECT market_ticker, legs_json,"
            " MAX(collection_ticker) AS collection_ticker"
            f" FROM rfqs WHERE market_ticker IN ({placeholders})"  # noqa: S608 - ints-only placeholders
            " GROUP BY market_ticker, legs_json"
        )
        # market_ticker -> {legs_json: collection_ticker} across DISTINCT leg-sets.
        legsets: dict[str, dict[str, Any]] = {}
        async with self._db.execute(legs_q, tuple(tickers)) as cursor:
            async for market_ticker, legs_json, collection in cursor:
                if not legs_json:
                    continue
                legsets.setdefault(market_ticker, {})[legs_json] = collection

        out: list[JsonDict] = []
        async with self._db.execute(fills_q, tuple(tickers)) as cursor:
            async for combo_ticker, our_side, ctr, loss_num in cursor:
                if not ctr:
                    continue
                distinct = legsets.get(combo_ticker)
                if not distinct:
                    # No leg definition on the tape ⇒ cannot model ⇒ not rehydrated.
                    continue
                if len(distinct) > 1:
                    # CONFLICTING leg definitions for the same combo ticker: the
                    # originating identity is ambiguous. Fail closed — reject rather
                    # than guess (surfaced by the caller as an unmodeled position).
                    log.warning(
                        "held_positions.conflicting_leg_sets",
                        combo_ticker=combo_ticker,
                        distinct_leg_sets=len(distinct),
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
                    }
                )
        return out

    async def decision_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self._db.execute("SELECT reasons_json FROM decisions") as cursor:
            async for row in cursor:
                for reason in json.loads(row[0]):
                    counts[reason] = counts.get(reason, 0) + 1
        return counts

    async def decision_kind_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self._db.execute(
            "SELECT kind, COUNT(*) FROM decisions GROUP BY kind"
        ) as cursor:
            async for row in cursor:
                counts[str(row[0])] = int(row[1])
        return counts

    async def ev_summary(self) -> dict[str, object]:
        async with self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(expected_edge_cc), 0),"
            " COUNT(realized_pnl_cc), COALESCE(SUM(realized_pnl_cc), 0) FROM ev_ledger"
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
            async for row in cursor:
                out.append(
                    {
                        "horizon_s": float(row[0]),
                        "n": int(row[1]),
                        "mean_fair_drift_cc": None if row[2] is None else float(row[2]),
                        "mean_raw_mid_drift_cc": None if row[3] is None else float(row[3]),
                    }
                )
        return out
