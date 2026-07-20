"""Market-vs-our pricing backtest for BIG REPEATED World Cup combos.

Pure DB analysis (hard rule 8): imports no live module, only reads the two
sqlite tapes and does arithmetic.

  OUR price   = what we actually quoted  (live DB `decisions` kind=quote_sent):
                  our_fair = context_json.fair_cc         (centi-cents)
                  our_ask  = 100 - no_bid_cc/100          (cents; the YES ask a taker pays us)
  MARKET price = where the combo actually CLEARED on Kalshi
                  (shadow DB `combo_trades.yes_price_cc`, the YES clearing price),
                  joined to its leg-set via shadow `rfqs.market_ticker`.

A combo's identity is its leg-set signature = sorted (leg_market_ticker, side).
The two tapes share the same Kalshi leg tickers, so signatures match exactly.

Usage:  python tools/market_vs_our_pricing.py            # full run + report
"""
from __future__ import annotations
import sqlite3, json, time, sys, os
from collections import defaultdict

LIVE = "data/combomaker-prod-live-wc.sqlite3"
SHAD = "data/combomaker-prod.sqlite3"
CACHE = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "."), "tmp", "mktcmp_cache.json")


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# ---------- leg-set -> human name ----------------------------------------
def leg_name(mt: str, side: str) -> str:
    p = mt.split("-")
    series = p[0].replace("KXWC", "")
    tail = p[-1]
    game = p[1] if len(p) > 1 else "?"
    if series == "ADVANCE":
        nm = f"{tail} adv"
    elif series == "GOAL":
        # KXWCGOAL-<game>-<TEAMPLAYER##>-<N>
        thr = tail
        player = p[-2] if len(p) > 2 else "?"
        # strip 3-char team prefix + trailing shirt number
        core = player[3:]
        core = core.rstrip("0123456789")
        nm = f"{core.title()} {thr}+g"
    elif series == "BTTS":
        nm = "BTTS"
    elif series == "TOTAL":
        nm = f"{tail}+tot"
    elif series == "SPREAD":
        nm = f"sprd {tail}"
    elif series == "GAME":
        nm = f"win {tail}"
    else:
        nm = f"{series}:{tail}"
    if side == "no":
        nm = "NO " + nm
    return nm


def game_of(sig) -> str:
    for mt, _ in sig:
        pp = mt.split("-")
        if len(pp) > 1:
            return pp[1]
    return "?"


def combo_name(sig) -> str:
    return " + ".join(leg_name(mt, s) for mt, s in sig)


# ---------- Phase A: our quotes ------------------------------------------
def our_quotes():
    log("Phase A: our quotes by leg-set (live DB)...")
    con = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    # pull leg-sets for all rfqs we have (WC only), into a map
    legs_by_rfq = {}
    for rid, lj in con.execute("select rfq_id, legs_json from rfqs"):
        if not lj or "KXWC" not in lj:
            continue
        legs = json.loads(lj)
        if not all(l.get("market_ticker", "").startswith("KXWC") for l in legs):
            continue
        legs_by_rfq[rid] = tuple(sorted((l["market_ticker"], l.get("side", "yes")) for l in legs))
    log(f"  {len(legs_by_rfq):,} WC rfqs in live DB")
    our = defaultdict(lambda: {"n": 0, "fair": 0.0, "ask": 0.0, "nlegs": 0})
    seen = 0
    for rid, cj in con.execute("select rfq_id, context_json from decisions where kind='quote_sent'"):
        sig = legs_by_rfq.get(rid)
        if sig is None:
            continue
        c = json.loads(cj)
        f, nb = c.get("fair_cc"), c.get("no_bid_cc")
        if f is None or nb is None:
            continue
        seen += 1
        d = our[sig]
        d["n"] += 1
        d["fair"] += f / 100.0            # cents
        d["ask"] += 100 - nb / 100.0      # cents
        d["nlegs"] = len(sig)
    con.close()
    for sig, d in our.items():
        d["fair"] /= d["n"]
        d["ask"] /= d["n"]
    log(f"  {seen:,} quote_sent rows -> {len(our):,} distinct WC leg-sets we quoted")
    return our


# ---------- Phase B: market clearing tape --------------------------------
def market_clearing():
    if os.path.exists(CACHE):
        log(f"Phase B: loading cached market clearing ({CACHE})")
        with open(CACHE) as fh:
            raw = json.load(fh)
        return {tuple(tuple(x) for x in json.loads(k)): v for k, v in raw.items()}

    log("Phase B: market clearing (shadow DB) — building ticker->legs map...")
    con = sqlite3.connect(f"file:{SHAD}?mode=ro", uri=True)
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("CREATE TEMP TABLE want(ticker TEXT PRIMARY KEY)")
    n = con.execute(
        "INSERT OR IGNORE INTO want SELECT DISTINCT ticker FROM combo_trades"
    ).rowcount
    log(f"  {n:,} distinct traded combo tickers loaded into temp table")

    t = time.time()
    tick_sig = {}
    q = ("SELECT r.market_ticker, r.legs_json FROM rfqs r "
         "JOIN want w ON r.market_ticker = w.ticker")
    for i, (mt, lj) in enumerate(con.execute(q)):
        if mt in tick_sig or not lj:
            continue
        try:
            legs = json.loads(lj)
        except Exception:
            continue
        tick_sig[mt] = tuple(sorted((l["market_ticker"], l.get("side", "yes")) for l in legs))
        if i and i % 20000 == 0:
            log(f"    resolved {len(tick_sig):,} tickers ({time.time()-t:.0f}s)")
    log(f"  resolved {len(tick_sig):,} combo tickers -> legs ({time.time()-t:.0f}s)")

    # aggregate trades by signature (volume-weighted clearing)
    log("  aggregating combo_trades by leg-set...")
    agg = defaultdict(lambda: {"n": 0, "vol": 0.0, "px_vol": 0.0,
                               "pxs": [], "yes_n": 0, "no_n": 0})
    miss = 0
    for tk, ypx, cnt, tside in con.execute(
        "SELECT ticker, yes_price_cc, count_centi, taker_side FROM combo_trades"
    ):
        sig = tick_sig.get(tk)
        if sig is None:
            miss += 1
            continue
        if ypx is None:
            continue
        px = ypx / 100.0                 # cents
        w = (cnt or 0) / 100.0           # contracts
        d = agg[sig]
        d["n"] += 1
        d["vol"] += w
        d["px_vol"] += px * w
        d["pxs"].append(px)
        if tside == "yes":
            d["yes_n"] += 1
        elif tside == "no":
            d["no_n"] += 1
    con.close()
    log(f"  {len(agg):,} distinct traded leg-sets; {miss:,} trades on unresolved tickers")

    out = {}
    for sig, d in agg.items():
        pxs = sorted(d["pxs"])
        vwap = d["px_vol"] / d["vol"] if d["vol"] > 0 else (sum(pxs) / len(pxs))
        out[sig] = {
            "n_trades": d["n"], "vol": round(d["vol"], 1),
            "vwap": round(vwap, 2),
            "med": round(pxs[len(pxs) // 2], 2),
            "lo": round(pxs[0], 2), "hi": round(pxs[-1], 2),
            "taker_yes": d["yes_n"], "taker_no": d["no_n"],
        }
    # cache (json keys must be str)
    with open(CACHE, "w") as fh:
        json.dump({json.dumps([list(x) for x in k]): v for k, v in out.items()}, fh)
    log(f"  cached -> {CACHE}")
    return out


def main():
    our = our_quotes()
    mkt = market_clearing()

    # join
    rows = []
    for sig, od in our.items():
        md = mkt.get(sig)
        rows.append({"sig": sig, "our": od, "mkt": md})

    matched = [r for r in rows if r["mkt"]]
    log(f"\nMATCHED {len(matched)} / {len(rows)} of our quoted leg-sets to the market tape")

    # focus on MAIN combos we quote repeatedly (not longshots): quoted >= 20x
    main = [r for r in matched if r["our"]["n"] >= 20]
    main.sort(key=lambda r: -r["our"]["n"])

    # save full join for the report step
    dump = []
    for r in rows:
        dump.append({
            "name": combo_name(r["sig"]), "game": game_of(r["sig"]),
            "nlegs": r["our"]["nlegs"], "our_n": r["our"]["n"],
            "our_fair": round(r["our"]["fair"], 2), "our_ask": round(r["our"]["ask"], 2),
            "mkt": r["mkt"],
        })
    outp = os.path.join(os.environ.get("CLAUDE_JOB_DIR", "."), "tmp", "mktcmp_join.json")
    with open(outp, "w") as fh:
        json.dump(dump, fh, indent=2)
    log(f"full join -> {outp}")

    # console preview: top repeated matched combos
    print("\n" + "=" * 118)
    print(f"{'combo':<44}{'game':<11}{'n':>4}{'ourFair':>8}{'ourAsk':>8}"
          f"{'mktVWAP':>8}{'mktMed':>7}{'gap':>7}{'trds':>6}{'vol':>9}")
    print("=" * 118)
    for r in main[:60]:
        o, m = r["our"], r["mkt"]
        gap = o["ask"] - m["vwap"]
        print(f"{combo_name(r['sig'])[:43]:<44}{game_of(r['sig'])[:10]:<11}"
              f"{o['n']:>4}{o['fair']:>8.1f}{o['ask']:>8.1f}"
              f"{m['vwap']:>8.1f}{m['med']:>7.1f}{gap:>+7.1f}"
              f"{m['n_trades']:>6}{m['vol']:>9.0f}")


if __name__ == "__main__":
    main()
