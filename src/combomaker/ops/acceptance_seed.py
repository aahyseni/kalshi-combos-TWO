"""Boot-time acceptance-tape seed from the store's measured history.

THE DEFECT THIS FIXES (2026-08-01 stand-down): the in-process acceptance tape
(``rfq/eviction_value.AcceptanceCounters``) is per-boot and starts EMPTY, so on
arming day the marginal KILL/ruin gates' CP-lower P(accept|size-bucket) read
0.0 for every bucket and only dES99<=0 diversifiers admitted (~0.6% — the 8/1
brick). The fix: at boot, reconstruct (size-bucket, quoted, accepted) from the
store's OWN decisions/rfqs tape — measured, never invented (North Star) — and
additively seed the tape.

WINDOW: ``SEED_WINDOW_S`` = 24h. The ratified anchors are per-NIGHT quantities
(P(KILL-night), ``portfolio_kill_tail_prob`` 0.02), so the seed window is the
anchor's own horizon — a measurement partition like ``SIZE_BUCKET_EDGES_CC``,
not a risk knob. It also keeps every bucket's counts ~30x inside the
Clopper-Pearson exact regime (the G3 (1-p)^n underflow boundary — the
``clopper_pearson_lower`` docstring's multi-day caveat) and inside ONE
acceptance regime (the 7/31 flow study measured 3-10x acceptance shifts across
posture changes).

READ DISCIPLINE (the live store is a 150GB WAL DB with a single writer):
  * SYNCHRONOUS stdlib sqlite3 on a SECOND ``mode=ro`` connection — never the
    shared aiosqlite connection (its single thread also serves the tape
    writer; the 2026-07-26 65s stall). Callers run this via
    ``asyncio.to_thread`` (measured ~77s cold).
  * NEVER ``WHERE at >= ...`` on decisions (no index on ``at``, 100M+ rows):
    the id window is found by BISECTION on the PK (``at`` is enqueue-ordered
    with id; off-by-a-few at the boundary is harmless).
  * CHUNKED cursors (~25k ids) so a long-lived read cursor never pins the
    WAL TRUNCATE checkpoint (the 2026-07-18 starvation incident).

FAIL-SAFE: any exception, missing table, unreadable path ⇒ ``None`` ⇒ the
tape stays empty ⇒ exactly today's behavior. Unjoinable rows (missing rfqs
row, malformed context, unmatched accepts) are COUNTED and SKIPPED — never
guessed into a bucket.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from combomaker.rfq.eviction_value import (
    N_BUCKETS,
    det_consumed_cc,
    risk_qty_from_terms,
    size_bucket,
)

SEED_WINDOW_S = 86_400
_CHUNK_IDS = 25_000
_IN_BATCH = 500

__all__ = ["SEED_WINDOW_S", "SeedResult", "seed_counts_from_store"]


@dataclass(frozen=True)
class SeedResult:
    """The operator-facing seed record — logged verbatim at boot."""

    quoted: list[int] = field(default_factory=lambda: [0] * N_BUCKETS)
    accepted: list[int] = field(default_factory=lambda: [0] * N_BUCKETS)
    rows_scanned: int = 0
    unjoinable: int = 0
    accepts_unmatched: int = 0
    elapsed_s: float = 0.0


def _bisect_first_id(
    con: sqlite3.Connection, table: str, cutoff_iso: str
) -> int | None:
    row = con.execute(f"SELECT MIN(id), MAX(id) FROM {table}").fetchone()
    if not row or row[0] is None:
        return None
    lo, hi = int(row[0]), int(row[1])

    def at_of(i: int) -> str | None:
        r = con.execute(
            f"SELECT at FROM {table} WHERE id >= ? ORDER BY id LIMIT 1", (i,)
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
    return lo


def seed_counts_from_store(
    db_path: Path, *, now_utc: datetime, award_sizing: bool
) -> SeedResult | None:
    """Reconstruct the last ``SEED_WINDOW_S`` of (quoted, accepted) per size
    bucket from the decisions + rfqs tables. Returns None on ANY failure."""
    t0 = time.monotonic()
    try:
        cutoff = datetime.fromtimestamp(
            now_utc.timestamp() - SEED_WINDOW_S, tz=UTC
        ).isoformat()
        uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            first_id = _bisect_first_id(con, "decisions", cutoff)
            if first_id is None:
                return SeedResult(elapsed_s=time.monotonic() - t0)
            (max_id,) = con.execute("SELECT MAX(id) FROM decisions").fetchone()
            quoted = [0] * N_BUCKETS
            accepted = [0] * N_BUCKETS
            rows_scanned = unjoinable = accepts_unmatched = 0
            quote_bucket: dict[str, int] = {}

            # Pass 1 — denominators: quote_sent rows, chunked, joined to rfqs
            # for the sizing terms; det from the SAME live arithmetic.
            chunk_lo = first_id
            while chunk_lo <= max_id:
                chunk_hi = min(chunk_lo + _CHUNK_IDS - 1, max_id)
                rows = con.execute(
                    "SELECT rfq_id, context_json FROM decisions"
                    " WHERE kind = 'quote_sent' AND id BETWEEN ? AND ?",
                    (chunk_lo, chunk_hi),
                ).fetchall()
                chunk_lo = chunk_hi + 1
                if not rows:
                    continue
                rows_scanned += len(rows)
                terms: dict[str, tuple[int | None, int | None]] = {}
                rfq_ids = sorted({str(r[0]) for r in rows if r[0]})
                for i in range(0, len(rfq_ids), _IN_BATCH):
                    batch = rfq_ids[i : i + _IN_BATCH]
                    marks = ",".join("?" * len(batch))
                    for rid, cc, tc in con.execute(
                        f"SELECT rfq_id, contracts_centi, target_cost_cc"
                        f" FROM rfqs WHERE rfq_id IN ({marks})",
                        batch,
                    ):
                        # rfq_id may be duplicated (repriced RFQs re-record):
                        # first row wins — identical sizing terms either way.
                        terms.setdefault(
                            str(rid),
                            (
                                None if cc is None else int(cc),
                                None if tc is None else int(tc),
                            ),
                        )
                for rfq_id, ctx_json in rows:
                    try:
                        ctx = json.loads(ctx_json) if ctx_json else {}
                        yes_bid = int(ctx["yes_bid_cc"])
                        no_bid = int(ctx["no_bid_cc"])
                        quote_id = str(ctx.get("quote_id") or "")
                        contracts, target = terms[str(rfq_id)]
                    except Exception:  # noqa: BLE001 — count + skip, never guess
                        unjoinable += 1
                        continue
                    qty = risk_qty_from_terms(
                        contracts, target, yes_bid, no_bid,
                        award_sizing=award_sizing,
                    )
                    if qty is None:
                        unjoinable += 1
                        continue
                    bucket = size_bucket(det_consumed_cc(qty, yes_bid, no_bid))
                    quoted[bucket] += 1
                    if quote_id:
                        quote_bucket[quote_id] = bucket

            # Pass 2 — numerators: BOTH confirm and decline decision rows (the
            # live tape counts every accept, confirmed OR last-look-declined).
            for (ctx_json,) in con.execute(
                "SELECT context_json FROM decisions"
                " WHERE kind IN ('confirm', 'decline') AND id >= ?",
                (first_id,),
            ):
                try:
                    ctx = json.loads(ctx_json) if ctx_json else {}
                    quote_id = str(ctx.get("quote_id") or "")
                except Exception:  # noqa: BLE001
                    accepts_unmatched += 1
                    continue
                accept_bucket = quote_bucket.get(quote_id)
                if accept_bucket is None:
                    accepts_unmatched += 1  # quote sent before the window edge
                    continue
                accepted[accept_bucket] += 1

            return SeedResult(
                quoted=quoted,
                accepted=accepted,
                rows_scanned=rows_scanned,
                unjoinable=unjoinable,
                accepts_unmatched=accepts_unmatched,
                elapsed_s=time.monotonic() - t0,
            )
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — fail-safe: None = empty tape = today
        return None
