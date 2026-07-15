"""One-time: persist shadow combo-ticker -> leg-set-signature map + per-trade
series, so time-aligned analysis doesn't need to rescan 19.7M rfqs each time."""
import sqlite3, json, time, os
SHAD = "data/combomaker-prod.sqlite3"
OUT = os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp", "ticksig.json")


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


con = sqlite3.connect(f"file:{SHAD}?mode=ro", uri=True)
con.execute("PRAGMA temp_store=MEMORY")
con.execute("CREATE TEMP TABLE want(ticker TEXT PRIMARY KEY)")
con.execute("INSERT OR IGNORE INTO want SELECT DISTINCT ticker FROM combo_trades")
log("scanning rfqs for ticker->legs...")
t = time.time()
tick_sig = {}
for mt, lj in con.execute(
        "SELECT r.market_ticker, r.legs_json FROM rfqs r JOIN want w ON r.market_ticker=w.ticker"):
    if mt in tick_sig or not lj:
        continue
    try:
        legs = json.loads(lj)
    except Exception:
        continue
    tick_sig[mt] = "||".join(sorted(f"{l['market_ticker']},{l.get('side','yes')}" for l in legs))
log(f"resolved {len(tick_sig):,} tickers ({time.time()-t:.0f}s)")
con.close()
with open(OUT, "w") as fh:
    json.dump(tick_sig, fh)
log(f"wrote {OUT}")
