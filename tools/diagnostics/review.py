import sqlite3, json, collections, os, re

DB = "data/combomaker-prod-live-wc.sqlite3"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

def adv_side(legs_json):
    """ARG / ENG / None from a combo's ADVANCE leg (the directional dimension)."""
    try:
        legs = json.loads(legs_json or "[]")
    except Exception:
        return None
    for L in legs:
        mt = L.get("market_ticker", "")
        if "ADVANCE" in mt:
            return mt.split("-")[-1]
    return None

SINCE = "2026-07-15T16:19"
rows = list(con.execute(
    "SELECT d.kind, d.reasons_json, r.legs_json "
    "FROM decisions d LEFT JOIN rfqs r ON d.rfq_id=r.rfq_id "
    f"WHERE d.at>='{SINCE}'"))
print(f"=== decisions since {SINCE}: {len(rows)} ===")
print("kinds:", dict(collections.Counter(r[0] for r in rows)))

# sole-reason declines
sole = collections.Counter(); multi = 0
for kind, rj, lj in rows:
    if kind != "no_quote":
        continue
    rs = [x for x in json.loads(rj or "[]") if x.startswith("skip_")]
    if len(rs) == 1:
        sole[rs[0]] += 1
    elif len(rs) > 1:
        multi += 1
print("\n=== SOLE-reason declines (the ONLY blocker = true cost) ===")
for r, c in sole.most_common(12):
    print(f"  {c:>6}  {r}")
print(f"  (multi-reason declines: {multi})")

# ADVANCE-side of what we QUOTE vs what we DECLINE for max_open_quotes
q_side = collections.Counter(); moq_side = collections.Counter(); dir_side = collections.Counter()
for kind, rj, lj in rows:
    s = adv_side(lj)
    if s is None:
        continue
    if kind == "quote_sent":
        q_side[s] += 1
    if kind == "no_quote":
        rs = json.loads(rj or "[]")
        if "skip_max_open_quotes" in rs:
            moq_side[s] += 1
        if "skip_directional_cap" in rs:
            dir_side[s] += 1
print("\n=== ADVANCE-side balance (ARG=our book side, ENG=the HEDGE side) ===")
print("  QUOTES sent by adv side       :", dict(q_side))
print("  skip_max_open_quotes by side  :", dict(moq_side), " <- hedge flow turned away on CAPACITY")
print("  skip_directional_cap by side  :", dict(dir_side))

# fills since relaunch
fills = list(con.execute(
    "SELECT at, combo_ticker, our_side, contracts_centi, price_cc "
    "FROM fills WHERE at>='2026-07-15T16:08'"))
print(f"\n=== FILLS since relaunch (16:08): {len(fills)} ===")
for f in fills:
    print("  ", f)

# our current book: the rehydrated positions (source of concentration)
print("\n=== OUR BOOK (rehydrated ENGARG positions driving concentration) ===")
held = list(con.execute(
    "SELECT combo_ticker, our_side, SUM(contracts_centi) "
    "FROM fills GROUP BY combo_ticker, our_side ORDER BY 3 DESC LIMIT 12"))
for h in held:
    ct, side, ctr = h
    print(f"  {side} {ctr/100:>6.0f}ct  {ct[:46]}")
con.close()

# latest book_risk_snapshot from the live log
print("\n=== latest book_risk_snapshot (from live_wc10.log) ===")
last = None
try:
    with open("live_wc10.log", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if '"book_risk_snapshot"' in line:
                last = line
except Exception as e:
    print("  (log read err)", e)
if last:
    d = json.loads(last)
    for k in ("n_positions", "es_99_cc", "governing_model_es_99_cc",
              "deterministic_max_loss_cc", "p_ruin", "structural"):
        print(f"  {k}: {d.get(k)}")
