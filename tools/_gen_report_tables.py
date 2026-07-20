"""Generate the markdown tables for the market-vs-our-pricing report.
taker_side='yes'-only market VWAP (the ask side we provide as NO-seller)."""
import sqlite3, json, os, statistics as st
from collections import defaultdict

LIVE = "data/combomaker-prod-live-wc.sqlite3"
SHAD = "data/combomaker-prod.sqlite3"
TS = os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp", "ticksig.json")
OUT = os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp", "report_tables.md")


def sig_str(legs):
    return "||".join(sorted(f"{l['market_ticker']},{l.get('side','yes')}" for l in legs))


def leg_name(mt, side):
    p = mt.split("-"); s = p[0].replace("KXWC", ""); tail = p[-1]
    if s == "ADVANCE": nm = f"{tail} adv"
    elif s == "GOAL":
        pl = p[-2][3:].rstrip("0123456789") if len(p) > 2 else "?"; nm = f"{pl.title()} {tail}+"
    elif s == "BTTS": nm = "BTTS"
    elif s == "TOTAL": nm = f"{tail}+ gls"
    elif s == "SPREAD": nm = f"sprd {tail}"
    elif s == "CORNERS": nm = f"corners {tail}+"
    elif s == "TCORNERS": nm = f"tmcorners {tail}+"
    elif s == "GAME": nm = f"win {tail}"
    elif s == "FIRSTGOAL": nm = f"1stgoal {tail[3:].rstrip('0123456789').title()}"
    elif s == "1HTOTAL": nm = f"1H {tail}+ gls"
    else: nm = f"{s}:{tail}"
    return ("NO " if side == "no" else "") + nm


def game_of(sstr):
    tok = sstr.split("||")[0]; mt = tok.rsplit(",", 1)[0]; p = mt.split("-")
    return p[1] if len(p) > 1 else "?"


def combo_name(sstr):
    return " + ".join(leg_name(*tok.rsplit(",", 1)) for tok in sstr.split("||"))


# our quotes
lcon = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
legs_by_rfq = {}
for rid, lj in lcon.execute("select rfq_id, legs_json from rfqs"):
    if not lj or "KXWC" not in lj: continue
    legs = json.loads(lj)
    if not all(l.get("market_ticker", "").startswith("KXWC") for l in legs): continue
    legs_by_rfq[rid] = sig_str(legs)
our = defaultdict(lambda: {"n": 0, "ask": 0.0, "fair": 0.0, "nlegs": 0})
for rid, cj in lcon.execute("select rfq_id, context_json from decisions where kind='quote_sent'"):
    ss = legs_by_rfq.get(rid)
    if ss is None: continue
    c = json.loads(cj); f, nb = c.get("fair_cc"), c.get("no_bid_cc")
    if f is None or nb is None: continue
    d = our[ss]; d["n"] += 1; d["ask"] += 100 - nb / 100.0; d["fair"] += f / 100.0
    d["nlegs"] = ss.count("||") + 1
lcon.close()
for d in our.values():
    d["ask"] /= d["n"]; d["fair"] /= d["n"]

# market (yes-only) for our sigs
ticksig = json.load(open(TS))
mine = {ss for ss, d in our.items() if d["n"] >= 20}
tick_target = {tk: ss for tk, ss in ticksig.items() if ss in mine}
scon = sqlite3.connect(f"file:{SHAD}?mode=ro", uri=True)
mkt = defaultdict(lambda: [0.0, 0.0, 0])   # pxvol, vol, ntr
for tk, ypx, cnt, tside in scon.execute(
        "select ticker, yes_price_cc, count_centi, taker_side from combo_trades"):
    ss = tick_target.get(tk)
    if ss is None or tside != "yes" or ypx is None: continue
    w = (cnt or 0) / 100.0
    mkt[ss][0] += ypx / 100.0 * w; mkt[ss][1] += w; mkt[ss][2] += 1
scon.close()

drift = {r["name"]: r for r in json.load(open(os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp", "drift.json")))}

rows = []
for ss in mine:
    m = mkt.get(ss)
    if not m or m[1] <= 0: continue
    nm = combo_name(ss)
    rows.append({
        "name": nm, "game": game_of(ss), "nlegs": our[ss]["nlegs"], "our_n": our[ss]["n"],
        "fair": round(our[ss]["fair"], 1), "ask": round(our[ss]["ask"], 1),
        "vwap": round(m[0] / m[1], 1), "ntr": m[2], "vol": round(m[1]),
        "gap": round(our[ss]["ask"] - m[0] / m[1], 1),
    })

liquid = sorted([r for r in rows if r["ntr"] >= 30], key=lambda r: -r["vol"])
thin = sorted([r for r in rows if r["ntr"] < 30], key=lambda r: -r["our_n"])


def tbl(rs):
    out = ["| combo | game | legs | our_n | our fair | our ask | mkt clear | gap | trades | volume |",
           "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for r in rs:
        g = f"{r['gap']:+.1f}"
        out.append(f"| {r['name']} | {r['game']} | {r['nlegs']} | {r['our_n']} | "
                   f"{r['fair']:.1f}¢ | {r['ask']:.1f}¢ | {r['vwap']:.1f}¢ | {g}¢ | {r['ntr']} | {r['vol']:,} |")
    return "\n".join(out)


gaps_liq = [r["gap"] for r in liquid]
lines = []
lines.append(f"LIQUID mains (tape ≥30 yes-trades) — {len(liquid)} combos; "
             f"median gap {st.median(gaps_liq):+.2f}¢, mean {st.mean(gaps_liq):+.2f}¢; "
             f"we're cheaper on {sum(g<0 for g in gaps_liq)}, higher on {sum(g>0 for g in gaps_liq)}\n")
lines.append(tbl(liquid))
lines.append(f"\n\nTHIN-tape repeated combos (tape <30 trades — LOW confidence; incl. ENG-ARG game which barely traded before the recorder stopped Jul 12) — {len(thin)} combos\n")
lines.append(tbl(thin))
open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("\n".join(lines))
print(f"\n\n[wrote {OUT}]")
