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

CREATE TABLE IF NOT EXISTS ev_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    fill_ref TEXT NOT NULL,
    expected_edge_cc INTEGER NOT NULL,
    realized_pnl_cc INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ev_ref ON ev_ledger (fill_ref);
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
    ) -> None:
        await self._db.execute(
            "INSERT INTO fills (at, fill_ref, order_id, combo_ticker, our_side,"
            " contracts_centi, price_cc, fee_cc, expected_edge_cc, raw_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        if expected_edge_cc is not None:
            await self._db.execute(
                "INSERT INTO ev_ledger (at, fill_ref, expected_edge_cc, realized_pnl_cc)"
                " VALUES (?, ?, ?, NULL)",
                (self._now(), fill_ref, expected_edge_cc),
            )
        await self._db.commit()

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
        }:
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
