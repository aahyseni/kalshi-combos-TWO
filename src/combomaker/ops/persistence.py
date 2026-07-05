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

import json
from pathlib import Path
from typing import Any, Self

import aiosqlite

from combomaker.core.clock import Clock
from combomaker.rfq.models import Rfq

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
"""


class Store:
    def __init__(self, db: aiosqlite.Connection, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    @classmethod
    async def open(cls, path: Path, clock: Clock) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        await db.executescript(_DDL)
        await db.commit()
        return cls(db, clock)

    async def close(self) -> None:
        await self._db.close()

    def _now(self) -> str:
        return self._clock.now().isoformat()

    async def record_rfq(self, rfq: Rfq, *, source: str) -> None:
        await self._db.execute(
            "INSERT INTO rfqs (rfq_id, seen_at, source, market_ticker, collection_ticker,"
            " contracts_centi, target_cost_cc, n_legs, legs_json, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rfq.rfq_id,
                self._now(),
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
        await self._db.commit()

    async def record_rfq_deleted(self, rfq_id: str, raw: JsonDict) -> None:
        await self._db.execute(
            "INSERT INTO rfq_deletions (rfq_id, seen_at, raw_json) VALUES (?, ?, ?)",
            (rfq_id, self._now(), json.dumps(raw)),
        )
        await self._db.commit()

    async def record_decision(
        self, kind: str, rfq_id: str | None, reasons: list[str], context: JsonDict
    ) -> None:
        await self._db.execute(
            "INSERT INTO decisions (at, kind, rfq_id, reasons_json, context_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (self._now(), kind, rfq_id, json.dumps(reasons), json.dumps(context)),
        )
        await self._db.commit()

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

    # --- simple readers for reports/tests ---

    async def count(self, table: str) -> int:
        if table not in {"rfqs", "rfq_deletions", "decisions", "would_quotes"}:
            raise ValueError(f"unknown table {table!r}")
        async with self._db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:  # noqa: S608
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def decision_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self._db.execute("SELECT reasons_json FROM decisions") as cursor:
            async for row in cursor:
                for reason in json.loads(row[0]):
                    counts[reason] = counts.get(reason, 0) + 1
        return counts
