"""LENS 2 — latency anatomy from the DBs (STRICTLY READ-ONLY: mode=ro).

Live DB  (data/combomaker-prod-live-wc.sqlite3): decisions/rfqs tables ->
  * quote_sent latency: rfq created_ts (exchange) -> decision at; seen_at -> at
  * skip_rfq_closed latency + per-day counts (verify the 89,863 claim)
  * per-reason latency buckets for a recent no_quote sample
  * quote_deleted reason split per day (verify 55%/35%)
  * today's unique-RFQ funnel

Shadow DB (data/combomaker-prod.sqlite3, 77GB, recorder writing): recent
rfq_deletions sample -> RFQ lifetime (created_ts -> deletion seen_at),
split allowlist (KXWC/KXMLB legs) vs rest. Indexed lookups only, chunked.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

LIVE = "file:data/combomaker-prod-live-wc.sqlite3?mode=ro"
SHADOW = "file:data/combomaker-prod.sqlite3?mode=ro"


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def dist(vals: list[float], label: str) -> None:
    if not vals:
        print(f"  {label}: EMPTY")
        return
    v = sorted(vals)
    n = len(v)
    q = lambda p: v[min(n - 1, int(p * n))]
    over2 = sum(1 for x in v if x > 2.0) / n * 100
    over5 = sum(1 for x in v if x > 5.0) / n * 100
    print(f"  {label}: n={n} p10={q(.10):.2f} p25={q(.25):.2f} p50={q(.50):.2f} "
          f"p75={q(.75):.2f} p90={q(.90):.2f} p99={q(.99):.2f} max={v[-1]:.1f} "
          f"| >2s={over2:.1f}% >5s={over5:.1f}%  (seconds)")


def chunked_rfq_lookup(con: sqlite3.Connection, rfq_ids: list[str],
                       want_legs: bool = False) -> dict[str, tuple]:
    """rfq_id -> (seen_at, created_ts[, legs_json]) via idx_rfqs_rfq_id, chunked."""
    out: dict[str, tuple] = {}
    cols = "rfq_id, seen_at, raw_json" + (", legs_json" if want_legs else "")
    for i in range(0, len(rfq_ids), 400):
        chunk = rfq_ids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for row in con.execute(
            f"SELECT {cols} FROM rfqs WHERE rfq_id IN ({ph})", chunk
        ):
            rid, seen_at, raw = row[0], row[1], row[2]
            if rid in out:
                continue  # first sighting wins
            try:
                created = json.loads(raw).get("created_ts")
            except Exception:
                created = None
            out[rid] = (seen_at, created, row[3]) if want_legs else (seen_at, created)
    return out


def live_db() -> None:
    con = sqlite3.connect(LIVE, uri=True, timeout=5)
    con.execute("PRAGMA busy_timeout=5000")
    print("=" * 80)
    print("LIVE DB — decisions by kind / day")
    print("=" * 80)
    for row in con.execute(
        "SELECT substr(at,1,10) d, kind, COUNT(*) FROM decisions "
        "WHERE kind IN ('quote_sent','quote_deleted','confirm','decline') "
        "GROUP BY d, kind ORDER BY d"
    ):
        print(f"  {row[0]}  {row[1]:14s} {row[2]:>8d}")

    # ---- quote_deleted reason split per day (verify 55/35) ----
    print("\nquote_deleted reasons per day:")
    per_day: dict[str, dict[str, int]] = {}
    for at, reasons in con.execute(
        "SELECT at, reasons_json FROM decisions WHERE kind='quote_deleted'"
    ):
        d = at[:10]
        r = json.loads(reasons)[0] if json.loads(reasons) else "?"
        per_day.setdefault(d, {}).setdefault(r, 0)
        per_day[d][r] += 1
    for d in sorted(per_day):
        tot = sum(per_day[d].values())
        parts = ", ".join(f"{k}={v} ({v/tot*100:.0f}%)"
                          for k, v in sorted(per_day[d].items(), key=lambda kv: -kv[1]))
        print(f"  {d} total={tot}: {parts}")

    # ---- skip_rfq_closed per day (verify 89,863 'one run') ----
    print("\nskip_rfq_closed no_quote decisions per day (decision rows, incl retries):")
    closed_today: list[tuple[str, str]] = []
    for row in con.execute(
        "SELECT substr(at,1,10) d, COUNT(*), COUNT(DISTINCT rfq_id) FROM decisions "
        "WHERE kind='no_quote' AND reasons_json LIKE '%skip_rfq_closed%' GROUP BY d"
    ):
        print(f"  {row[0]}: rows={row[1]}  unique_rfqs={row[2]}")

    # ---- quote_sent latency ----
    print("\nquote_sent latency (all-time; joins rfqs tape):")
    sent = con.execute(
        "SELECT at, rfq_id FROM decisions WHERE kind='quote_sent'"
    ).fetchall()
    ids = list({r[1] for r in sent if r[1]})
    look = chunked_rfq_lookup(con, ids)
    lat_created, lat_seen = [], []
    lat_created_today, lat_seen_today = [], []
    for at, rid in sent:
        info = look.get(rid)
        if not info:
            continue
        seen_at, created = info
        t_at = parse_iso(at)
        if created:
            dcr = (t_at - parse_iso(created)).total_seconds()
            lat_created.append(dcr)
            if at[:10] == "2026-07-16":
                lat_created_today.append(dcr)
        if seen_at:
            dsn = (t_at - parse_iso(seen_at)).total_seconds()
            lat_seen.append(dsn)
            if at[:10] == "2026-07-16":
                lat_seen_today.append(dsn)
    dist(lat_created, "exchange created_ts -> quote_sent (FULL path)")
    dist(lat_seen, "our worker pickup (seen_at) -> quote_sent (price+risk+POST)")
    dist(lat_created_today, "TODAY only: created_ts -> quote_sent")
    dist(lat_seen_today, "TODAY only: seen_at -> quote_sent")

    # ---- skip_rfq_closed latency (TODAY, deduped first-decision per rfq) ----
    print("\nskip_rfq_closed (TODAY): how late was the losing POST?")
    rows = con.execute(
        "SELECT at, rfq_id FROM decisions WHERE kind='no_quote' "
        "AND at >= '2026-07-16' AND reasons_json LIKE '%skip_rfq_closed%'"
    ).fetchall()
    first: dict[str, str] = {}
    for at, rid in rows:
        if rid and (rid not in first or at < first[rid]):
            first[rid] = at
    ids = list(first.keys())
    print(f"  decision rows={len(rows)} unique rfqs={len(ids)} "
          f"(retry inflation x{len(rows)/max(1,len(ids)):.2f})")
    look = chunked_rfq_lookup(con, ids)
    lc, ls = [], []
    for rid, at in first.items():
        info = look.get(rid)
        if not info:
            continue
        seen_at, created = info
        t_at = parse_iso(at)
        if created:
            lc.append((t_at - parse_iso(created)).total_seconds())
        if seen_at:
            ls.append((t_at - parse_iso(seen_at)).total_seconds())
    dist(lc, "created_ts -> first rfq_closed decision")
    dist(ls, "seen_at    -> first rfq_closed decision")

    # ---- per-reason latency buckets, recent no_quote sample ----
    print("\nper-reason latency (recent 120k no_quote decisions, first reason):")
    max_id = con.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    sample = con.execute(
        "SELECT at, rfq_id, reasons_json FROM decisions "
        "WHERE id > ? AND kind='no_quote'", (max_id - 120_000,)
    ).fetchall()
    by_reason: dict[str, list[tuple[str, str]]] = {}
    for at, rid, reasons in sample:
        r = json.loads(reasons)
        key = r[0] if r else "?"
        by_reason.setdefault(key, []).append((at, rid))
    all_ids = list({rid for lst in by_reason.values() for _, rid in lst if rid})
    look = chunked_rfq_lookup(con, all_ids)
    for reason, lst in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        lc, ls = [], []
        for at, rid in lst:
            info = look.get(rid)
            if not info:
                continue
            seen_at, created = info
            t_at = parse_iso(at)
            if created:
                lc.append((t_at - parse_iso(created)).total_seconds())
            if seen_at:
                ls.append((t_at - parse_iso(seen_at)).total_seconds())
        print(f"\n {reason} (n={len(lst)}):")
        dist(lc, "created->decision")
        dist(ls, "seen  ->decision")

    # ---- today's funnel ----
    print("\nTODAY funnel (allowlist tape, unique rfq_ids):")
    seen_today = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT rfq_id) FROM rfqs WHERE seen_at >= '2026-07-16'"
    ).fetchone()
    print(f"  rfqs recorded rows={seen_today[0]} unique={seen_today[1]}")
    for kind in ("quote_sent", "no_quote"):
        r = con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT rfq_id) FROM decisions "
            "WHERE kind=? AND at >= '2026-07-16'", (kind,)
        ).fetchone()
        print(f"  {kind}: rows={r[0]} unique={r[1]}")
    con.close()


def shadow_db() -> None:
    print("\n" + "=" * 80)
    print("SHADOW DB — RFQ lifetime (created_ts -> deletion), recent sample")
    print("=" * 80)
    con = sqlite3.connect(SHADOW, uri=True, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    max_id = con.execute("SELECT MAX(id) FROM rfq_deletions").fetchone()[0]
    dels = con.execute(
        "SELECT rfq_id, seen_at FROM rfq_deletions WHERE id > ?",
        (max_id - 30_000,)
    ).fetchall()
    print(f"  sample: {len(dels)} recent deletions "
          f"({dels[0][1][:19]} .. {dels[-1][1][:19]})")
    ids = list({r[0] for r in dels})
    look = chunked_rfq_lookup(con, ids, want_legs=True)
    life_allow, life_rest = [], []
    for rid, del_seen in dels:
        info = look.get(rid)
        if not info:
            continue
        seen_at, created, legs = info
        if not created:
            continue
        life = (parse_iso(del_seen) - parse_iso(created)).total_seconds()
        allow = False
        try:
            allow = all(
                leg.get("market_ticker", "").startswith(("KXWC", "KXMLB"))
                for leg in json.loads(legs)
            )
        except Exception:
            pass
        (life_allow if allow else life_rest).append(life)
    dist(life_allow, "RFQ lifetime, ALL legs KXWC/KXMLB (our allowlist)")
    dist(life_rest, "RFQ lifetime, everything else")
    con.close()


if __name__ == "__main__":
    live_db()
    shadow_db()
