"""B stage 2 — settle the graded universe from Kalshi's settled-markets API.

Reads <tmp>/graded_universe.csv (stage 1). For every distinct leg series, pulls
Kalshi's settled markets (result yes/no) and builds a leg->result map. Then per
combo computes parlay settlement with EARLY-NO short-circuit: the parlay settles
NO the moment ANY resolved leg settles against its chosen side (so a combo can be
resolved-NO even while other legs are still pending — matches the real combo
settlement convention). Only if EVERY leg is resolved-and-hit is it resolved-YES.

Writes <tmp>/graded_settled.csv + caches the settled-market map so a re-run is cheap.
PROD read-only market data (GET /markets?status=settled); never trades.
"""
import asyncio
import csv
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, r"C:\Users\aahys\kalshi-combos-TWO\src")
sys.stdout.reconfigure(encoding="utf-8")

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiApiError, KalshiRestClient
from combomaker.ops.dotenv import load_dotenv

TMP = r"C:\Users\aahys\.claude\jobs\24844262\tmp"
IN = os.path.join(TMP, "graded_universe.csv")
OUT = os.path.join(TMP, "graded_settled.csv")
CACHE = os.path.join(TMP, "settlements_cache.json")
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
THROTTLE_S = 0.12


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def series_of(leg_ticker: str) -> str:
    return leg_ticker.split("-", 1)[0]


async def list_settled(rest, series: str) -> list[dict]:
    out: list[dict] = []
    cursor = ""
    while True:
        params = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(5):
            await asyncio.sleep(THROTTLE_S)
            try:
                payload = await rest.get_markets(**params)
                break
            except KalshiApiError as exc:
                if exc.status >= 500 and attempt < 4:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise
        out.extend(payload.get("markets") or [])
        cursor = payload.get("cursor") or ""
        if not cursor:
            return out


def combo_settlement(leg_tickers: list[str], leg_sides: list[str], results: dict) -> str:
    """'yes' | 'no' | 'unresolved'. Early-NO: any resolved leg against its side => NO."""
    any_unresolved = False
    for tk, side in zip(leg_tickers, leg_sides):
        res = results.get(tk)          # 'yes' | 'no' | None
        if res is None:
            any_unresolved = True
            continue
        hit = (res == side)            # leg contributes to the parlay iff it settled its side
        if not hit:
            return "no"                # one miss kills the parlay regardless of the rest
    return "unresolved" if any_unresolved else "yes"


async def main() -> None:
    load_dotenv()
    # ---- read stage-1 universe ----
    combos = []
    series_needed: set[str] = set()
    with open(IN, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            legs = row["leg_tickers"].split("|") if row["leg_tickers"] else []
            sides = row["leg_sides"].split("|") if row["leg_sides"] else []
            row["_legs"], row["_sides"] = legs, sides
            combos.append(row)
            for lt in legs:
                if lt:
                    series_needed.add(series_of(lt))
    log(f"universe: {len(combos):,} combos, {len(series_needed)} distinct leg series")

    # ---- fetch settled markets per series (cached) ----
    results: dict[str, str] = {}
    cached_series: set[str] = set()
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            blob = json.load(f)
        results = blob.get("results", {})
        cached_series = set(blob.get("series_done", []))
        log(f"cache: {len(results):,} leg results, {len(cached_series)} series done")

    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    async with KalshiRestClient(PROD_REST, signer) as rest:
        todo = sorted(series_needed - cached_series)
        for i, series in enumerate(todo, 1):
            try:
                mkts = await list_settled(rest, series)
            except KalshiApiError as exc:
                log(f"  [{i}/{len(todo)}] {series}: ERROR {exc.status} — skipping")
                continue
            got = 0
            for m in mkts:
                r = m.get("result")
                if r in ("yes", "no"):
                    results[m["ticker"]] = r
                    got += 1
            cached_series.add(series)
            log(f"  [{i}/{len(todo)}] {series}: {got:,} settled markets "
                f"(total legs resolved={len(results):,})")
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump({"results": results, "series_done": sorted(cached_series)}, f)

    # ---- grade each combo's settlement ----
    stats = defaultdict(lambda: [0, 0, 0])  # sport -> [yes, no, unresolved]
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "combo_ticker", "sport", "n_legs", "our_fair_cc", "our_fair_prob",
            "clearing_cc", "room_cc", "n_trades", "resolved", "combo_yes",
            "leg_tickers", "leg_sides",
        ])
        for row in combos:
            s = combo_settlement(row["_legs"], row["_sides"], results)
            resolved = s in ("yes", "no")
            combo_yes = 1 if s == "yes" else (0 if s == "no" else "")
            sport = row["sport"]
            idx = 0 if s == "yes" else 1 if s == "no" else 2
            stats[sport][idx] += 1
            w.writerow([
                row["combo_ticker"], sport, row["n_legs"], row["our_fair_cc"],
                row["our_fair_prob"], row["clearing_cc"], row["room_cc"],
                row["n_trades"], int(resolved), combo_yes,
                row["leg_tickers"], row["leg_sides"],
            ])
    log(f"wrote {OUT}")
    for sport, (y, n, u) in sorted(stats.items()):
        tot = y + n + u
        res = y + n
        log(f"  {sport}: {tot:,} combos | resolved {res:,} ({100*res/tot:.0f}%) "
            f"| YES {y:,} NO {n:,} | unresolved {u:,}")


if __name__ == "__main__":
    asyncio.run(main())
