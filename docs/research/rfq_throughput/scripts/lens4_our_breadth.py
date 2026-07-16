"""Lens 4: our quoting breadth from the LIVE DB (READ-ONLY).

decisions kind='quote_sent' per day + distinct market tickers quoted, vs RFQs
seen. Ticker comes from joining live rfqs on rfq_id (indexed?). Check indexes
first and fall back to reading decisions.context_json if it carries the ticker.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict

DB = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod-live-wc.sqlite3?mode=ro"


def main() -> None:
    con = sqlite3.connect(DB, uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    cur.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")
    print("indexes:", [r[0] for r in cur.fetchall()])

    # sample a quote_sent decision to see context shape
    cur.execute(
        "SELECT at, rfq_id, reasons_json, context_json FROM decisions"
        " WHERE kind='quote_sent' ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    print("sample quote_sent:", row[0], row[1])
    print("context:", (row[3] or "")[:600])

    # quotes sent per day + distinct rfq targets
    cur.execute(
        "SELECT substr(at,1,10), count(*), count(DISTINCT rfq_id) FROM decisions"
        " WHERE kind='quote_sent' GROUP BY 1 ORDER BY 1"
    )
    print("\nquote_sent per day (n, distinct rfq_id):")
    rows = cur.fetchall()
    for r in rows:
        print(" ", r)

    # distinct market tickers we quoted, per day, via live rfqs join
    cur2 = con.cursor()
    per_day_tickers: dict[str, set] = defaultdict(set)
    per_day_games: dict[str, set] = defaultdict(set)
    cur.execute(
        "SELECT substr(at,1,10), rfq_id FROM decisions WHERE kind='quote_sent'"
    )
    misses = 0
    for day, rfq_id in cur.fetchall():
        cur2.execute(
            "SELECT market_ticker FROM rfqs WHERE rfq_id=? LIMIT 1", (rfq_id,)
        )
        r = cur2.fetchone()
        if r is None:
            misses += 1
            continue
        per_day_tickers[day].add(r[0])
    print("\ndistinct combo tickers QUOTED per day (live bot):")
    for day in sorted(per_day_tickers):
        print(f"  {day}  {len(per_day_tickers[day])}")
    print("rfq_id join misses:", misses)

    # no_quote reason breakdown for the latest day (why we skip flow)
    cur.execute(
        "SELECT reasons_json, count(*) FROM decisions"
        " WHERE kind='no_quote' AND at >= '2026-07-16'"
        " GROUP BY reasons_json ORDER BY 2 DESC LIMIT 15"
    )
    print("\nno_quote reasons today:")
    for r in cur.fetchall():
        print(f"  {r[1]:>8}  {r[0][:120]}")

    # fills per day
    cur.execute("SELECT substr(at,1,10) d, count(*) FROM fills GROUP BY 1 ORDER BY 1")
    print("\nfills per day:", cur.fetchall())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
