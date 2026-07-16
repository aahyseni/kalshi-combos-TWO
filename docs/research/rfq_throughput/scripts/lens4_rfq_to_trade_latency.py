"""Lens 4: winning-maker speed = RFQ create -> trade print latency (READ-ONLY).

For every combo trade in a window, find the latest RFQ on the same bespoke
market_ticker with created_ts <= trade created_time (both EXCHANGE clocks, no
local skew) and report the delta distribution + CDF at our TTL marks.

Windows: (i) firehose 2026-07-10 .. 07-12 (WC quarters window), (ii) lull+today
2026-07-15 .. now. Per-ticker lookups ride idx_rfqs_market_ticker.

Also: RFQs/day via id-range bisection (insert order == seen_at order), and the
fraction of window RFQs that ever print (distinct matched RFQ instances / RFQs).
"""

from __future__ import annotations

import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime

DB = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod.sqlite3?mode=ro"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def bisect_id(cur: sqlite3.Cursor, table: str, target_iso: str) -> int:
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
    return xs2[min(len(xs2) - 1, max(0, int(round(q * (len(xs2) - 1)))))]


def analyze_window(con: sqlite3.Connection, lo_day: str, hi_day: str) -> None:
    cur = con.cursor()
    print(f"\n==== window created_time in [{lo_day}, {hi_day}) ====", flush=True)
    # full pass over combo_trades filtering on created_time (1.5M rows, one scan)
    cur.execute(
        "SELECT ticker, created_time FROM combo_trades"
        " WHERE created_time >= ? AND created_time < ?",
        (lo_day, hi_day),
    )
    trades: list[tuple[str, float]] = [(t, ts(c)) for t, c in cur.fetchall()]
    tickers = sorted({t for t, _ in trades})
    print(f"trades={len(trades)}  distinct tickers={len(tickers)}", flush=True)

    # per-ticker RFQ creation times (indexed lookups)
    rfq_times: dict[str, list[float]] = {}
    cur2 = con.cursor()
    for i, tk in enumerate(tickers):
        cur2.execute(
            "SELECT json_extract(raw_json,'$.created_ts') FROM rfqs"
            " WHERE market_ticker=?",
            (tk,),
        )
        rfq_times[tk] = sorted(ts(r[0]) for r in cur2.fetchall() if r[0] is not None)
        if (i + 1) % 5000 == 0:
            print(f"  ..{i + 1}/{len(tickers)} tickers", flush=True)

    deltas: list[float] = []
    unmatched = 0
    matched_rfq_instances: set[tuple[str, float]] = set()
    for tk, t_time in trades:
        times = rfq_times.get(tk) or []
        j = bisect_right(times, t_time)
        if j == 0:
            unmatched += 1
            continue
        created = times[j - 1]
        deltas.append(t_time - created)
        matched_rfq_instances.add((tk, created))

    n_rfqs_on_tickers = sum(len(v) for v in rfq_times.values())
    print(
        f"matched trades={len(deltas)}  unmatched (no earlier RFQ)={unmatched}"
        f"  rfq instances on traded tickers={n_rfqs_on_tickers}"
        f"  matched rfq instances={len(matched_rfq_instances)}"
    )
    print("delta = trade.created_time - latest rfq.created_ts (exchange clocks):")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        print(f"  p{int(q * 100):02d} = {pct(deltas, q):10.2f}s")
    for t in (1, 2, 3, 5, 10, 23, 30, 60, 120, 300):
        frac = sum(1 for x in deltas if x <= t) / max(1, len(deltas))
        print(f"  traded within {t:>4}s of RFQ create : {frac * 100:5.1f}%")

    # RFQs in this window overall (id-range) for a trade-through rate
    lo_id = bisect_id(cur, "rfqs", lo_day)
    hi_id = bisect_id(cur, "rfqs", hi_day)
    n_rfqs_window = hi_id - lo_id
    print(
        f"RFQ rows seen in window (id-range approx): {n_rfqs_window:,}"
        f"  -> matched-print rate ~ {len(matched_rfq_instances) / max(1, n_rfqs_window) * 100:.2f}%"
    )


def main() -> None:
    con = connect()
    cur = con.cursor()
    # RFQs per day via bisect (cheap)
    print("== RFQs recorded per day (id-range bisect) ==")
    days = [f"2026-07-{d:02d}" for d in range(6, 17)]
    bounds = [bisect_id(cur, "rfqs", d) for d in days]
    cur.execute("SELECT max(id) FROM rfqs")
    bounds.append(cur.fetchone()[0])
    for d, a, b in zip(days, bounds, bounds[1:]):
        print(f"  {d}: {b - a:>9,}")

    analyze_window(con, "2026-07-15", "2026-07-17")
    analyze_window(con, "2026-07-10", "2026-07-12")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
