"""Lens 4: OUR reaction time = exchange RFQ created_ts -> our quote_sent at.

READ-ONLY on the LIVE DB. decisions(kind='quote_sent').at minus
rfqs.raw_json.$.created_ts joined on rfq_id (indexed).

Run of record 2026-07-16 (n=55,145): p50 1.64s p90 3.40s p95 10.46s;
<=1s 15.6%, <=2s 67.5%, <=3s 84.3%, <=5s 93.2%.

Also: taker concentration on the shadow tape (creator_id over last 200k rfqs):
12,197 distinct creators, top-10 = 15.4%.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

LIVE = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod-live-wc.sqlite3?mode=ro"


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main() -> None:
    lv = sqlite3.connect(LIVE, uri=True, timeout=30)
    lv.execute("PRAGMA busy_timeout=30000")
    cur = lv.execute(
        "SELECT d.at, json_extract(r.raw_json,'$.created_ts')"
        " FROM decisions d JOIN rfqs r ON r.rfq_id = d.rfq_id"
        " WHERE d.kind='quote_sent'"
    )
    deltas = sorted(
        ts(at) - ts(created) for at, created in cur.fetchall() if created is not None
    )
    n = len(deltas)
    print("n =", n)
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        print(f"  p{int(q * 100):02d} = {deltas[min(n - 1, int(round(q * (n - 1))))]:8.2f}s")
    for t in (1, 2, 3, 5, 10, 23, 30):
        frac = sum(1 for x in deltas if x <= t) / n
        print(f"  quote posted within {t:>3}s of RFQ create: {frac * 100:5.1f}%")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
