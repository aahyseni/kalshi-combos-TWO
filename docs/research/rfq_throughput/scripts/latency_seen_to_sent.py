"""Seen->sent latency (quoted) and seen->rfq_closed lateness, firehose window.

READ-ONLY. Window = waiver_tiers run 2026-07-16T17:30:23Z..18:13:24Z.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

DB = "data/combomaker-prod-live-wc.sqlite3"
W_START = "2026-07-16T17:30:23"
W_END = "2026-07-16T18:13:24"


def p(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))]


def ts(s):
    return datetime.fromisoformat(s)


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    cur = con.cursor()
    lo = cur.execute(
        "SELECT MIN(id) FROM decisions WHERE at >= ?", (W_START,)
    ).fetchone()[0]
    hi = cur.execute(
        "SELECT MAX(id) FROM decisions WHERE at < ?", (W_END,)
    ).fetchone()[0]

    sent, closed = [], []
    cur2 = con.cursor()  # separate cursor: inner lookups must not clobber the scan
    for kind, rfq_id, reasons_json, at in cur.execute(
        "SELECT kind, rfq_id, reasons_json, at FROM decisions"
        " WHERE id >= ? AND id <= ? AND kind IN ('quote_sent','no_quote')",
        (lo, hi),
    ):
        if kind == "no_quote" and '"skip_rfq_closed"' not in reasons_json:
            continue
        row = cur2.execute(
            "SELECT seen_at FROM rfqs WHERE rfq_id = ? LIMIT 1", (rfq_id,)
        ).fetchone()
        if not row:
            continue
        dt = (ts(at) - ts(row[0])).total_seconds()
        (sent if kind == "quote_sent" else closed).append(dt)

    for name, v in (("seen->quote_sent", sent), ("seen->rfq_closed_decision", closed)):
        if not v:
            print(name, "n=0")
            continue
        print(
            f"{name}: n={len(v)} p50={p(v,0.5):.3f}s p90={p(v,0.9):.3f}s"
            f" p95={p(v,0.95):.3f}s p99={p(v,0.99):.3f}s max={max(v):.2f}s"
            f" frac>2s={sum(1 for x in v if x > 2)/len(v):.3f}"
        )


if __name__ == "__main__":
    main()
