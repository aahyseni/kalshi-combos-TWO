import asyncio, os
from pathlib import Path
from combomaker.core.clock import FakeClock
from combomaker.ops.persistence import Store

DB = Path(os.environ["CLAUDE_JOB_DIR"]) / "tmp" / "livecopy.sqlite3"


async def main():
    store = await Store.open(DB, FakeClock())
    rows_c = await store._db.execute_fetchall("SELECT DISTINCT combo_ticker FROM fills")
    tickers = [c[0] for c in rows_c]
    rows = await store.held_positions(tickers)
    # delta the cap sees per leg market: contracts * PROD(other-leg marginals).
    # 0.5 is a neutral proxy; real marginals only shrink it further.
    leg_delta = {}
    for h in rows:
        legs = h["legs"]
        ctr = h["contracts_centi"] / 100.0
        prod = 0.5 ** (len(legs) - 1)
        for L in legs:
            mt = L["market_ticker"]
            leg_delta[mt] = leg_delta.get(mt, 0.0) + ctr * prod
    total = sum(h["contracts_centi"] for h in rows) / 100
    print(f"held positions (fixed): {len(rows)}  total real contracts: {total:.0f}")
    print("--- per-leg-market aggregate delta (proxy marginals=0.5), cap=300 ---")
    for mt, d in sorted(leg_delta.items(), key=lambda x: -abs(x[1]))[:12]:
        flag = "OVER CAP" if abs(d) > 300 else "ok"
        print(f"  {d:>10.1f}  {flag:9} {mt}")
    await store.close()


asyncio.run(main())
