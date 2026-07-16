"""LENS 2 follow-ups (READ-ONLY):
1. staleness-family skip rows per day (is the old WS-reconnect wall still real?)
2. created_ts -> seen_at (wire + queue dwell) distribution for today
3. rfq_closed vs actual RFQ deletion time (is rfq_closed the quote window, not death?)
4. unique-RFQ outcome funnel for today (precedence: quoted > closed > deadline > risk > filter)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

LIVE = "file:data/combomaker-prod-live-wc.sqlite3?mode=ro"
SHADOW = "file:data/combomaker-prod.sqlite3?mode=ro"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def dist(vals, label):
    if not vals:
        print(f"  {label}: EMPTY")
        return
    v = sorted(vals)
    n = len(v)
    q = lambda p: v[min(n - 1, int(p * n))]
    print(f"  {label}: n={n} p10={q(.10):.2f} p25={q(.25):.2f} p50={q(.50):.2f} "
          f"p75={q(.75):.2f} p90={q(.90):.2f} p99={q(.99):.2f} max={v[-1]:.1f} (s)")


con = sqlite3.connect(LIVE, uri=True, timeout=5)
con.execute("PRAGMA busy_timeout=5000")

print("=" * 80)
print("1. staleness/subscription skip rows per day")
print("=" * 80)
for reason in ("skip_leg_stale", "skip_ws_unhealthy", "skip_leg_unknown"):
    print(f"\n{reason}:")
    for row in con.execute(
        "SELECT substr(at,1,10) d, COUNT(*) FROM decisions "
        "WHERE kind='no_quote' AND reasons_json LIKE ? GROUP BY d",
        (f"%{reason}%",),
    ):
        print(f"  {row[0]}: {row[1]}")

print("\n" + "=" * 80)
print("2. wire+queue component: created_ts -> seen_at, today's rfqs (sample)")
print("=" * 80)
max_id = con.execute("SELECT MAX(id) FROM rfqs").fetchone()[0]
rows = con.execute(
    "SELECT seen_at, raw_json FROM rfqs WHERE id > ?", (max_id - 150_000,)
).fetchall()
gaps = []
for seen_at, raw in rows:
    try:
        created = json.loads(raw).get("created_ts")
        if created:
            gaps.append((parse_iso(seen_at) - parse_iso(created)).total_seconds())
    except Exception:
        pass
dist(gaps, "created_ts -> seen_at (WS + dispatch + queue dwell)")
under = sum(1 for g in gaps if g < 0.75) / len(gaps) * 100
print(f"  fraction under 0.75s: {under:.1f}%")

print("\n" + "=" * 80)
print("4. unique-RFQ outcome funnel TODAY (precedence: quoted>closed>deadline>risk>filter)")
print("=" * 80)
RISK = {"skip_max_open_quotes", "skip_game_loss_cap", "skip_mass_acceptance_breach",
        "skip_directional_cap", "skip_per_combo_loss_cap", "skip_slate_cap",
        "skip_portfolio_cvar", "skip_portfolio_det_max", "skip_portfolio_ruin",
        "skip_utilization_backstop", "skip_widen_avoided", "skip_bankroll_unavailable"}
LATE = {"skip_rfq_closed": "closed_at_post", "skip_price_deadline": "pool_deadline",
        "skip_quote_timed_out": "quote_timed_out", "skip_pricing_failed": "pricing_failed"}
rank = {"quoted": 0, "closed_at_post": 1, "pool_deadline": 2, "pricing_failed": 3,
        "risk_cap": 4, "filter": 5}
outcome: dict[str, str] = {}
for kind, rid, reasons in con.execute(
    "SELECT kind, rfq_id, reasons_json FROM decisions "
    "WHERE at >= '2026-07-16' AND kind IN ('quote_sent','no_quote')"
):
    if not rid:
        continue
    if kind == "quote_sent":
        o = "quoted"
    else:
        try:
            r0 = json.loads(reasons)
        except Exception:
            continue
        first = r0[0] if r0 else "?"
        if first in LATE:
            o = LATE[first]
        elif first in RISK:
            o = "risk_cap"
        else:
            o = "filter"
    prev = outcome.get(rid)
    if prev is None or rank.get(o, 9) < rank.get(prev, 9):
        outcome[rid] = o
total = len(outcome)
from collections import Counter
cnt = Counter(outcome.values())
print(f"  unique RFQs with any decision today: {total}")
for k, v in cnt.most_common():
    print(f"    {k:16s} {v:>8d}  ({v/total*100:.1f}%)")

# ---- 3. rfq_closed vs deletion time ----
print("\n" + "=" * 80)
print("3. rfq_closed RFQs: when were they actually DELETED? (shadow-DB cross-check)")
print("=" * 80)
closed_ids = {rid for rid, o in outcome.items() if o == "closed_at_post"}
# created_ts for those ids from the live tape
info = {}
ids = list(closed_ids)
for i in range(0, len(ids), 400):
    chunk = ids[i:i + 400]
    ph = ",".join("?" * len(chunk))
    for rid, raw in con.execute(
        f"SELECT rfq_id, raw_json FROM rfqs WHERE rfq_id IN ({ph})", chunk
    ):
        try:
            info[rid] = json.loads(raw).get("created_ts")
        except Exception:
            pass
con.close()

sh = sqlite3.connect(SHADOW, uri=True, timeout=20)
sh.execute("PRAGMA busy_timeout=20000")
max_id = sh.execute("SELECT MAX(id) FROM rfq_deletions").fetchone()[0]
# recent ~3h of deletions; intersect with today's closed set
dels = sh.execute(
    "SELECT rfq_id, seen_at FROM rfq_deletions WHERE id > ?", (max_id - 400_000,)
).fetchall()
sh.close()
lifetimes = []
for rid, del_at in dels:
    created = info.get(rid)
    if created:
        lifetimes.append((parse_iso(del_at) - parse_iso(created)).total_seconds())
print(f"  matched {len(lifetimes)} of {len(closed_ids)} closed-at-post RFQs "
      f"in recent {len(dels)} deletions")
dist(lifetimes, "created_ts -> ACTUAL deletion, for RFQs that were rfq_closed to us")
