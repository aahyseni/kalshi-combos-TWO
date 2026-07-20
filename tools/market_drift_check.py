"""Drift bound: our quotes run Jul13-15 but the tape ends Jul12, so the two
never overlap in time. This measures how fast each liquid main's clearing price
moved over the tape week (Jul6-12), and compares our game-day ask to the LAST
tape day (Jul11-12, closest to our quotes) rather than the whole-week average.

taker_side='yes' only (the ask side we, as NO-seller, provide)."""
from __future__ import annotations
import sqlite3, json, os
from collections import defaultdict

LIVE = "data/combomaker-prod-live-wc.sqlite3"
SHAD = "data/combomaker-prod.sqlite3"
TS = os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp", "ticksig.json")


def sig_str(legs):
    return "||".join(sorted(f"{l['market_ticker']},{l.get('side','yes')}" for l in legs))


def leg_name(mt, side):
    p = mt.split("-"); s = p[0].replace("KXWC", ""); tail = p[-1]
    if s == "ADVANCE": nm = f"{tail} adv"
    elif s == "GOAL":
        pl = p[-2][3:].rstrip("0123456789") if len(p) > 2 else "?"; nm = f"{pl.title()} {tail}+g"
    elif s == "BTTS": nm = "BTTS"
    elif s == "TOTAL": nm = f"{tail}+tot"
    elif s == "SPREAD": nm = f"sprd {tail}"
    else: nm = f"{s}:{tail}"
    return ("NO " if side == "no" else "") + nm


def combo_name(sstr):
    parts = []
    for tok in sstr.split("||"):
        mt, side = tok.rsplit(",", 1)
        parts.append(leg_name(mt, side))
    return " + ".join(parts)


# our quotes by sig_str
lcon = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
legs_by_rfq = {}
for rid, lj in lcon.execute("select rfq_id, legs_json from rfqs"):
    if not lj or "KXWC" not in lj: continue
    legs = json.loads(lj)
    if not all(l.get("market_ticker", "").startswith("KXWC") for l in legs): continue
    legs_by_rfq[rid] = sig_str(legs)
our = defaultdict(lambda: {"n": 0, "ask": 0.0, "fair": 0.0})
for rid, cj in lcon.execute("select rfq_id, context_json from decisions where kind='quote_sent'"):
    ss = legs_by_rfq.get(rid)
    if ss is None: continue
    c = json.loads(cj); f, nb = c.get("fair_cc"), c.get("no_bid_cc")
    if f is None or nb is None: continue
    d = our[ss]; d["n"] += 1; d["ask"] += 100 - nb / 100.0; d["fair"] += f / 100.0
lcon.close()
for d in our.values():
    d["ask"] /= d["n"]; d["fair"] /= d["n"]

# ticksig -> reverse map for target tickers only
ticksig = json.load(open(TS))
targets = {ss for ss, d in our.items() if d["n"] >= 20}
tick_target = {tk: ss for tk, ss in ticksig.items() if ss in targets}

# scan combo_trades once, bucket by sig+day (taker yes only)
scon = sqlite3.connect(f"file:{SHAD}?mode=ro", uri=True)
bysig = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))  # sig -> day -> [pxvol, vol]
tot = defaultdict(lambda: [0.0, 0.0, 0])                       # sig -> [pxvol, vol, ntr]
for tk, ct, ypx, cnt, tside in scon.execute(
        "select ticker, created_time, yes_price_cc, count_centi, taker_side from combo_trades"):
    ss = tick_target.get(tk)
    if ss is None or tside != "yes" or ypx is None: continue
    day = (ct or "")[:10]
    w = (cnt or 0) / 100.0; px = ypx / 100.0
    bysig[ss][day][0] += px * w; bysig[ss][day][1] += w
    tot[ss][0] += px * w; tot[ss][1] += w; tot[ss][2] += 1
scon.close()


def vwap(pxvol, vol): return pxvol / vol if vol > 0 else float("nan")


rows = []
for ss in targets:
    if ss not in tot or tot[ss][1] <= 0: continue
    days = sorted(bysig[ss])
    first_v = vwap(*bysig[ss][days[0]])
    last_v = vwap(*bysig[ss][days[-1]])
    # last-2-tape-day vwap (closest to our quote time)
    last2 = days[-2:]
    l2pv = sum(bysig[ss][d][0] for d in last2); l2v = sum(bysig[ss][d][1] for d in last2)
    rows.append({
        "name": combo_name(ss), "our_n": our[ss]["n"],
        "our_ask": round(our[ss]["ask"], 1), "our_fair": round(our[ss]["fair"], 1),
        "wk_vwap": round(vwap(tot[ss][0], tot[ss][1]), 1),
        "last2_vwap": round(vwap(l2pv, l2v), 1),
        "drift": round(last_v - first_v, 1), "ntr": tot[ss][2],
        "vol": round(tot[ss][1]),
    })

rows = [r for r in rows if r["ntr"] >= 30]         # liquid enough for drift
rows.sort(key=lambda r: -r["vol"])
print(f"{'combo':<40}{'ourAsk':>7}{'wkVWAP':>7}{'last2':>7}{'drift':>7}"
      f"{'ask-last2':>10}{'ntr':>6}{'vol':>9}")
print("-" * 93)
for r in rows[:40]:
    print(f"{r['name'][:39]:<40}{r['our_ask']:>7.1f}{r['wk_vwap']:>7.1f}{r['last2_vwap']:>7.1f}"
          f"{r['drift']:>+7.1f}{r['our_ask']-r['last2_vwap']:>+10.1f}{r['ntr']:>6}{r['vol']:>9.0f}")

import statistics as st
gaps = [r["our_ask"] - r["last2_vwap"] for r in rows]
print("-" * 93)
print(f"liquid mains: {len(rows)}   median(ask-last2)={st.median(gaps):+.2f}c   "
      f"mean={st.mean(gaps):+.2f}c   #we-cheaper={sum(g<0 for g in gaps)}  #we-higher={sum(g>0 for g in gaps)}")
json.dump(rows, open(os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp", "drift.json"), "w"), indent=2)
