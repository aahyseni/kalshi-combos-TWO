"""Lens 4: leg-series mix + n_legs of traded-but-never-quoted tickers, Jul 16.

READ-ONLY. traded set from shadow combo_trades; quoted set from live decisions
(kind='quote_sent') joined to live rfqs; per missed ticker one shadow RFQ row
(idx_rfqs_market_ticker) parsed for mve_selected_legs series prefixes.

Run of record 2026-07-16T18:3xZ output:
  traded: 1418  quoted: 6502  missed: 1250 ; missed trades 14931/17417
  top mixes: KXATPMATCH 127; ATP|WTA(+challengers) mixes ~300;
  KXMENWORLDCUP x KXWC* ~160; KXNBASUMMERGAME 52(+mixes); KXUFCFIGHT 30;
  KXMLBGAME 25 (+15 xKXMLBTOTAL); crypto 15m 12+10.
  n_legs: 2:262 3:220 4:210 5:131 6:103 7+:324
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter

SHADOW = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod.sqlite3?mode=ro"
LIVE = "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod-live-wc.sqlite3?mode=ro"


def main() -> None:
    sh = sqlite3.connect(SHADOW, uri=True, timeout=30)
    sh.execute("PRAGMA busy_timeout=30000")
    lv = sqlite3.connect(LIVE, uri=True, timeout=30)
    lv.execute("PRAGMA busy_timeout=30000")

    cur = sh.execute(
        "SELECT DISTINCT ticker FROM combo_trades"
        " WHERE created_time >= '2026-07-16' AND created_time < '2026-07-17'"
    )
    traded = {r[0] for r in cur.fetchall()}
    cur = lv.execute(
        "SELECT DISTINCT r.market_ticker FROM decisions d"
        " JOIN rfqs r ON r.rfq_id=d.rfq_id"
        " WHERE d.kind='quote_sent' AND d.at >= '2026-07-16'"
    )
    quoted = {r[0] for r in cur.fetchall()}
    missed = sorted(traded - quoted)
    print("traded:", len(traded), "quoted:", len(quoted), "missed:", len(missed))

    trade_counts = Counter()
    cur = sh.execute(
        "SELECT ticker, count(*) FROM combo_trades"
        " WHERE created_time >= '2026-07-16' AND created_time < '2026-07-17'"
        " GROUP BY ticker"
    )
    for tk, c in cur.fetchall():
        trade_counts[tk] = c

    series_mix: Counter[str] = Counter()
    nlegs: Counter[int] = Counter()
    no_rfq = 0
    missed_trades = 0
    cur2 = sh.cursor()
    for tk in missed:
        missed_trades += trade_counts[tk]
        cur2.execute("SELECT raw_json FROM rfqs WHERE market_ticker=? LIMIT 1", (tk,))
        row = cur2.fetchone()
        if row is None:
            no_rfq += 1
            continue
        legs = json.loads(row[0]).get("mve_selected_legs") or []
        nlegs[len(legs)] += 1
        prefixes = sorted(
            {(l.get("market_ticker") or "").split("-", 1)[0] for l in legs}
        )
        series_mix["|".join(prefixes)] += 1

    print("missed tickers with no recorded RFQ:", no_rfq)
    print("market trades on missed tickers:", missed_trades, "of", sum(trade_counts.values()))
    print("n_legs:", dict(sorted(nlegs.items())))
    for k, v in series_mix.most_common(25):
        print(f"  {v:>5}  {k}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
