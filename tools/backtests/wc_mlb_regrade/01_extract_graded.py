"""B stage 1 — extract the graded universe from the read-only prod shadow DB.

Graded universe = combos that BOTH (a) actually traded (combo_trades → clearing
price) AND (b) we would-quoted (would_quotes → our fair, via rfqs rfq_id↔ticker).
Per combo (market_ticker): median clearing, median our-fair, room = clearing-fair,
n_legs, sport, leg tickers/sides (for settlement + family features).

Writes: <tmp>/graded_universe.csv  (one row per traded+would-quoted combo)
Progress + ETA to stdout so the long rfqs scan is observable.

READ-ONLY on the prod DB (mode=ro). Never writes to it.
"""
import csv
import json
import os
import sqlite3
import statistics
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")  # Windows cp1252 stdout chokes on → etc.

TMP = r"C:\Users\aahys\.claude\jobs\24844262\tmp"
PROD = r"C:\Users\aahys\kalshi-combos-TWO\data\combomaker-prod.sqlite3"
OUT = os.path.join(TMP, "graded_universe.csv")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
    c.execute("PRAGMA mmap_size=30000000000")  # 30GB mmap — avoid copy on read
    c.execute("PRAGMA cache_size=-1048576")     # 1GB page cache
    c.execute("PRAGMA temp_store=MEMORY")
    return c


def main() -> None:
    t0 = time.monotonic()
    c = connect_ro()

    # ---- Phase 1: clearing price per traded combo ticker ----
    log("phase1: aggregating combo_trades by ticker ...")
    clearing: dict[str, list[int]] = {}
    taker: dict[str, list[str]] = {}
    n = 0
    for tk, ypx, cnt, side in c.execute(
        "SELECT ticker, yes_price_cc, count_centi, taker_side FROM combo_trades"
    ):
        if tk is None or ypx is None:
            continue
        clearing.setdefault(tk, []).append(int(ypx))
        taker.setdefault(tk, []).append(side or "")
        n += 1
        if n % 500_000 == 0:
            log(f"  ...{n:,} trades scanned, {len(clearing):,} distinct combos")
    clr = {
        tk: (int(statistics.median(v)), len(v)) for tk, v in clearing.items()
    }
    traded = set(clr)
    log(f"phase1 done: {n:,} trades → {len(traded):,} distinct traded combos "
        f"({time.monotonic()-t0:.0f}s)")

    # ---- Phase 2: rfqs scan → rfq_id→(ticker,n_legs,legs_json) for traded combos ----
    log("phase2: scanning rfqs (the long pole) for traded combos ...")
    rfq_meta: dict[str, tuple] = {}   # rfq_id -> (ticker, n_legs, legs_json)
    scanned = 0
    t2 = time.monotonic()
    cur = c.execute("SELECT rfq_id, market_ticker, n_legs, legs_json FROM rfqs")
    while True:
        rows = cur.fetchmany(50_000)
        if not rows:
            break
        for rfq_id, mtk, nl, legs in rows:
            if mtk in traded:
                rfq_meta[rfq_id] = (mtk, nl, legs)
        scanned += len(rows)
        if scanned % 1_000_000 == 0:
            el = time.monotonic() - t2
            rate = scanned / el
            log(f"  rfqs {scanned:,} scanned  {rate:,.0f}/s  "
                f"matched={len(rfq_meta):,}  elapsed={el:.0f}s")
    log(f"phase2 done: {scanned:,} rfqs scanned → {len(rfq_meta):,} rfqs on traded "
        f"combos ({time.monotonic()-t2:.0f}s)")

    # ---- Phase 3: would_quotes scan → our fair for those rfq_ids ----
    log("phase3: scanning would_quotes for our fair ...")
    want = set(rfq_meta)
    fair_by_combo: dict[str, list[int]] = {}
    fairprob_by_combo: dict[str, list[float]] = {}
    scanned = 0
    t3 = time.monotonic()
    cur = c.execute("SELECT rfq_id, fair_cc, fair_prob FROM would_quotes")
    while True:
        rows = cur.fetchmany(50_000)
        if not rows:
            break
        for rfq_id, fcc, fpb in rows:
            if rfq_id in want and fcc is not None:
                mtk = rfq_meta[rfq_id][0]
                fair_by_combo.setdefault(mtk, []).append(int(fcc))
                fairprob_by_combo.setdefault(mtk, []).append(float(fpb))
        scanned += len(rows)
        if scanned % 2_000_000 == 0:
            log(f"  would_quotes {scanned:,} scanned  combos_with_fair="
                f"{len(fair_by_combo):,}  ({time.monotonic()-t3:.0f}s)")
    log(f"phase3 done: {scanned:,} would_quotes scanned → {len(fair_by_combo):,} "
        f"combos have our fair ({time.monotonic()-t3:.0f}s)")

    # ---- Phase 4: join → one row per traded+would-quoted combo ----
    log("phase4: joining → graded_universe.csv ...")
    # pick representative legs per combo (first rfq we saw for it)
    legs_by_combo: dict[str, tuple] = {}
    for _rfq_id, (mtk, nl, legs) in rfq_meta.items():
        if mtk not in legs_by_combo:
            legs_by_combo[mtk] = (nl, legs)
    rows_out = 0
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "combo_ticker", "sport", "n_legs", "our_fair_cc", "our_fair_prob",
            "clearing_cc", "room_cc", "n_trades", "leg_tickers", "leg_sides",
        ])
        for mtk, fairs in fair_by_combo.items():
            if mtk not in clr:
                continue
            clearing_cc, n_trades = clr[mtk]
            our_fair_cc = int(statistics.median(fairs))
            our_fair_prob = float(statistics.median(fairprob_by_combo[mtk]))
            nl, legs = legs_by_combo.get(mtk, (None, None))
            try:
                legobjs = json.loads(legs) if legs else []
            except Exception:
                legobjs = []
            leg_tickers = [str(l.get("market_ticker", "")) for l in legobjs]
            leg_sides = [str(l.get("side", "")) for l in legobjs]
            sport = (
                "soccer" if any(t.startswith("KXWC") for t in leg_tickers)
                else "mlb" if any(t.startswith("KXMLB") for t in leg_tickers)
                else "other"
            )
            w.writerow([
                mtk, sport, nl, our_fair_cc, round(our_fair_prob, 6),
                clearing_cc, clearing_cc - our_fair_cc, n_trades,
                "|".join(leg_tickers), "|".join(leg_sides),
            ])
            rows_out += 1
    log(f"phase4 done: wrote {rows_out:,} graded combos → {OUT}")
    log(f"TOTAL {time.monotonic()-t0:.0f}s")


if __name__ == "__main__":
    main()
