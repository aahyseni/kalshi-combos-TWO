"""Lens 4 basics off the shadow tape (READ-ONLY).

A) combo_trades: trades/day, distinct combo tickers/day, breakdown by collection prefix.
B) RFQ intake lag sample: rfqs.seen_at - raw_json.created_ts on recent rows.
C) RFQ lifetime sample: rfq_deletions.deleted_ts - rfqs.created_ts (indexed join on rfq_id).

DB: data/combomaker-prod.sqlite3 opened mode=ro; short indexed / single-pass queries only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

DB = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod.sqlite3?mode=ro"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def bisect_id(cur: sqlite3.Cursor, table: str, target_iso: str) -> int:
    """Smallest id whose seen_at >= target (id insert order == seen_at order)."""
    cur.execute(f"SELECT min(id), max(id) FROM {table}")
    lo, hi = cur.fetchone()
    while lo < hi:
        mid = (lo + hi) // 2
        cur.execute(
            f"SELECT seen_at FROM {table} WHERE id >= ? ORDER BY id LIMIT 1", (mid,)
        )
        row = cur.fetchone()
        if row is None or row[0] >= target_iso:
            hi = mid
        else:
            lo = mid + 1
    return lo


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    xs2 = sorted(xs)
    i = min(len(xs2) - 1, max(0, int(round(q * (len(xs2) - 1)))))
    return xs2[i]


def main() -> None:
    con = connect()
    cur = con.cursor()

    # ---------- A) full single pass over combo_trades (1.53M rows) ----------
    print("== A) combo_trades single pass ==", flush=True)
    per_day_trades: Counter[str] = Counter()
    per_day_tickers: dict[str, set[str]] = defaultdict(set)
    per_day_notional_cc: Counter[str] = Counter()
    coll_counter: Counter[str] = Counter()
    n = 0
    cur.execute(
        "SELECT ticker, substr(created_time,1,10), count_centi, yes_price_cc, taker_side"
        " FROM combo_trades"
    )
    for ticker, day, count_centi, yes_cc, taker_side in cur:
        n += 1
        per_day_trades[day] += 1
        per_day_tickers[day].add(ticker)
        prefix = ticker.split("-", 1)[0]
        coll_counter[prefix] += 1
        # taker notional in cc: price(cc per contract) * contracts(centi)/100
        if count_centi is not None and yes_cc is not None:
            price_cc = yes_cc if taker_side == "yes" else 10000 - yes_cc
            per_day_notional_cc[day] += price_cc * count_centi // 100
    print(f"total trade rows: {n}")
    for day in sorted(per_day_trades):
        print(
            f"{day}  trades={per_day_trades[day]:>8}  distinct_tickers="
            f"{len(per_day_tickers[day]):>7}  taker_notional_$="
            f"{per_day_notional_cc[day] / 10000:>12,.0f}"
        )
    print("\ncollection prefix breakdown (all days):")
    for k, v in coll_counter.most_common(20):
        print(f"  {k:45s} {v:>9}")

    # ---------- B) intake lag on recent rfqs ----------
    print("\n== B) rfq intake lag (seen_at - created_ts), recent 50k rows ==", flush=True)
    cur.execute("SELECT max(id) FROM rfqs")
    max_id = cur.fetchone()[0]
    lags: list[float] = []
    cur.execute(
        "SELECT seen_at, json_extract(raw_json,'$.created_ts') FROM rfqs"
        " WHERE id > ?",
        (max_id - 50_000,),
    )
    for seen_at, created in cur:
        if created is None:
            continue
        lags.append((ts(seen_at) - ts(created)).total_seconds())
    lags_pos = [x for x in lags if x >= 0]
    print(f"n={len(lags)}  (negative-clock-skew rows: {len(lags) - len(lags_pos)})")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        print(f"  p{int(q * 100):02d} = {pct(lags, q):8.3f}s")

    # ---------- C) rfq lifetime: deletions -> rfqs join, 10k sample ----------
    print("\n== C) rfq lifetime (deleted_ts - created_ts), 10k recent sample ==", flush=True)
    cur.execute("SELECT max(id) FROM rfq_deletions")
    del_max = cur.fetchone()[0]
    # take every 5th of the last 50k deletions -> 10k joins
    cur.execute(
        "SELECT rfq_id, json_extract(raw_json,'$.deleted_ts') FROM rfq_deletions"
        " WHERE id > ? AND (id % 5)=0",
        (del_max - 50_000,),
    )
    dels = [(r, d) for r, d in cur.fetchall() if d is not None]
    print(f"sampled deletions: {len(dels)}", flush=True)
    cur2 = con.cursor()
    lifetimes: list[float] = []
    missing = 0
    for rfq_id, deleted in dels:
        cur2.execute(
            "SELECT json_extract(raw_json,'$.created_ts') FROM rfqs WHERE rfq_id=?"
            " LIMIT 1",
            (rfq_id,),
        )
        row = cur2.fetchone()
        if row is None or row[0] is None:
            missing += 1
            continue
        lifetimes.append((ts(deleted) - ts(row[0])).total_seconds())
    print(f"joined: {len(lifetimes)}  missing rfq row: {missing}")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        print(f"  p{int(q * 100):02d} = {pct(lifetimes, q):10.2f}s")
    for t in (5, 10, 23, 30, 60, 120, 300, 600):
        frac = sum(1 for x in lifetimes if x <= t) / max(1, len(lifetimes))
        print(f"  lifetime <= {t:>4}s : {frac * 100:5.1f}%")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
