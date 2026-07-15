import sqlite3, json, collections

con = sqlite3.connect("file:data/combomaker-prod-live-wc.sqlite3?mode=ro", uri=True)

def fam(mt):
    # KXWCADVANCE-... -> ADVANCE ; KXWCTCORNERS -> CORNERS ; KXWCGOAL -> GOAL ; etc.
    p = mt.split("-")[0]
    for f in ("ADVANCE","TCORNERS","CORNERS","GOAL","BTTS","1H","FH","SPREAD","TOTAL","GAME","WCGA","1X2","DC"):
        if f in p:
            return "CORNERS" if "CORNERS" in p else ("1H" if ("1H" in p or "FH" in p) else f)
    return p.replace("KX","")

def fams_of(legs_json):
    try:
        return set(fam(L["market_ticker"]) for L in json.loads(legs_json or "[]"))
    except Exception:
        return set()

# ---- fills (exact) ----
print("=== FILLS since relaunch (exact) ===")
for at, ct, side, ctr, px, fee in con.execute(
    "SELECT at,combo_ticker,our_side,contracts_centi,price_cc,fee_cc FROM fills WHERE at>='2026-07-15T16:08'"):
    yes = (10000 - px) / 100.0
    mult = (100.0 / yes) if yes > 0 else 0
    r = con.execute("SELECT legs_json FROM rfqs WHERE market_ticker=? LIMIT 1", (ct,)).fetchone()
    fams = fams_of(r[0]) if r else set()
    legs = [L["market_ticker"].split("-", 2)[-1] for L in json.loads(r[0])] if r else []
    print(f"  {at[11:19]} {side} {ctr/100:.2f}ct  NO {px/100:.1f}c => YES {yes:.1f}c (~{mult:.0f}x)  fams={sorted(fams)}")
    print(f"     legs: {legs}")

# ---- family distribution: FLOW vs QUOTE vs DECLINE ----
SINCE = "2026-07-15T16:19"
rows = list(con.execute(
    "SELECT d.kind, d.reasons_json, r.legs_json FROM decisions d "
    "LEFT JOIN rfqs r ON d.rfq_id=r.rfq_id WHERE d.at>=?", (SINCE,)))
flow = collections.Counter(); quoted = collections.Counter(); declined = collections.Counter()
decl_reason = collections.defaultdict(collections.Counter)
for kind, rj, lj in rows:
    fs = fams_of(lj)
    if not fs:
        continue
    for f in fs:
        flow[f] += 1
        if kind == "quote_sent":
            quoted[f] += 1
        elif kind == "no_quote":
            declined[f] += 1
            rs = [x for x in json.loads(rj or "[]") if x.startswith("skip_")]
            if rs:
                decl_reason[f][rs[0]] += 1
print(f"\n=== market-family distribution since {SINCE} (combos CONTAINING each family) ===")
print(f"  {'family':10} {'in-flow':>8} {'QUOTED':>7} {'declined':>9}  top-decline-reason")
for f in sorted(flow, key=lambda x: -flow[x]):
    tr = decl_reason[f].most_common(1)
    trs = f"{tr[0][0]} ({tr[0][1]})" if tr else ""
    print(f"  {f:10} {flow[f]:>8} {quoted[f]:>7} {declined[f]:>9}  {trs}")

# ---- corners combos we quoted: our YES price distribution ----
print("\n=== CORNERS combos we QUOTED — our YES-ask distribution (are we too cheap?) ===")
cyes = []
for kind, rj, cj in con.execute(
    "SELECT d.kind, d.reasons_json, d.context_json FROM decisions d "
    "LEFT JOIN rfqs r ON d.rfq_id=r.rfq_id WHERE d.kind='quote_sent' AND d.at>=? ", (SINCE,)):
    pass
q = list(con.execute(
    "SELECT d.context_json, r.legs_json FROM decisions d LEFT JOIN rfqs r ON d.rfq_id=r.rfq_id "
    "WHERE d.kind='quote_sent' AND d.at>=?", (SINCE,)))
for cj, lj in q:
    if "CORNERS" not in fams_of(lj):
        continue
    c = json.loads(cj or "{}")
    nb = c.get("no_bid_cc")
    if nb:
        cyes.append((10000 - nb) / 100.0)
if cyes:
    cyes.sort()
    import statistics
    print(f"  n={len(cyes)}  YES-ask median={statistics.median(cyes):.1f}c  min={cyes[0]:.1f}c  max={cyes[-1]:.1f}c")
    print(f"  (longshot bucket <10c: {sum(1 for y in cyes if y<10)} quotes)")
con.close()
