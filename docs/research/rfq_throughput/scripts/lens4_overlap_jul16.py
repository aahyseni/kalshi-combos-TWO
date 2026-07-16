"""Lens 4: Jul 16 overlap — tickers the market traded vs tickers we quoted (READ-ONLY).

Also: of trades on tickers we quoted, how many printed while we plausibly had a
quote up? (approx: trade within 30s of SOME rfq instance we quoted — we only
know rfq_id we quoted, so join on rfq_id -> market_ticker -> trades that day).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

SHADOW = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod.sqlite3?mode=ro"
LIVE = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod-live-wc.sqlite3?mode=ro"


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main() -> None:
    sh = sqlite3.connect(SHADOW, uri=True, timeout=30)
    sh.execute("PRAGMA busy_timeout=30000")
    lv = sqlite3.connect(LIVE, uri=True, timeout=30)
    lv.execute("PRAGMA busy_timeout=30000")

    # market trades on Jul 16 (exchange time)
    cur = sh.execute(
        "SELECT ticker, created_time FROM combo_trades"
        " WHERE created_time >= '2026-07-16' AND created_time < '2026-07-17'"
    )
    trades: list[tuple[str, float]] = [(t, ts(c)) for t, c in cur.fetchall()]
    traded_tickers = {t for t, _ in trades}
    print(f"market trades Jul16: {len(trades)}  distinct tickers: {len(traded_tickers)}")

    # our quoted (rfq_id, ticker, at) on Jul 16
    cur = lv.execute(
        "SELECT d.at, d.rfq_id, r.market_ticker FROM decisions d"
        " JOIN rfqs r ON r.rfq_id = d.rfq_id"
        " WHERE d.kind='quote_sent' AND d.at >= '2026-07-16'"
    )
    quoted = [(ts(a), rid, tk) for a, rid, tk in cur.fetchall()]
    quoted_tickers = {tk for _, _, tk in quoted}
    print(f"our quote_sent Jul16: {len(quoted)}  distinct tickers: {len(quoted_tickers)}")

    inter = traded_tickers & quoted_tickers
    print(f"overlap tickers (traded AND we quoted same day): {len(inter)}")
    trades_on_quoted = [(t, c) for t, c in trades if t in quoted_tickers]
    print(f"market trades on tickers we quoted: {len(trades_on_quoted)}")

    # tighter: trades within [0, 30s] after one of OUR quote_sent times on that ticker
    from collections import defaultdict

    our_times: dict[str, list[float]] = defaultdict(list)
    for at, _, tk in quoted:
        our_times[tk].append(at)
    for v in our_times.values():
        v.sort()
    from bisect import bisect_left

    near = 0
    for tk, t_time in trades_on_quoted:
        times = our_times[tk]
        j = bisect_left(times, t_time)
        # our quote posted within 30s BEFORE the trade
        if j > 0 and t_time - times[j - 1] <= 30.0:
            near += 1
    print(
        f"trades printing within 30s AFTER one of our quote posts (same ticker): {near}"
    )
    print("(our fills that day: see live fills table = 9)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
