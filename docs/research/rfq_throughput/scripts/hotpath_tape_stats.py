"""Re-derive the hot-path throughput numbers used by 03-hotpath-architecture.md.

READ-ONLY against the LIVE DB (data/combomaker-prod-live-wc.sqlite3). Every
connection uses mode=ro + timeout=5 per the research hard constraints. Windowed
by binary-search on the autoincrement id (indexed PK) so no full-table scan of
the `at` column is ever issued before the window start is found.

Usage:
  ./.venv/Scripts/python.exe docs/research/rfq_throughput/scripts/hotpath_tape_stats.py \
      [--since 2026-07-16T17:30]

Outputs (all derived 2026-07-16 for the report):
  1. decision-kind counts + per-reason counts in the window
  2. no_quote rows categorized by the pipeline stage that killed them
     (filter-cheap / priced-then-risk-declined / pricing-stage / priced+POSTed-dead)
  3. RFQ duplication factor: unique (collection, sorted (leg, side)) signatures
  4. seen_at -> quote_sent latency percentiles
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parents[4] / "data" / "combomaker-prod-live-wc.sqlite3"

RISK = {
    "skip_game_loss_cap", "skip_mass_acceptance_breach", "skip_max_open_quotes",
    "skip_per_combo_loss_cap", "skip_slate_cap", "skip_directional_cap",
    "skip_portfolio_cvar", "skip_portfolio_det_max", "skip_utilization_backstop",
    "skip_portfolio_ruin", "skip_bankroll_unavailable",
}
FILTER = {
    "skip_size_above_max", "skip_size_below_min", "skip_leg_stale",
    "skip_leg_book_thin", "skip_leg_unknown", "skip_not_whitelisted",
    "skip_series_not_allowed", "skip_too_many_legs", "skip_ws_unhealthy",
    "skip_inplay_leg", "skip_start_time_unknown", "skip_game_too_far",
    "skip_in_play", "skip_halted", "skip_leg_spread_too_wide",
    "skip_unmodeled_regime",
}
PRICE = {
    "skip_price_deadline", "skip_pricing_failed", "skip_classifier_unknown",
    "skip_no_combo_grid", "skip_size_unresolvable", "skip_malformed_combo",
    "skip_logically_impossible", "skip_sources_disagree",
}


def first_id_at_or_after(cur: sqlite3.Cursor, table: str, ts_col: str, target: str) -> int:
    cur.execute(f"SELECT MIN(id), MAX(id) FROM {table}")
    lo, hi = cur.fetchone()

    def at_of(i: int) -> str | None:
        cur.execute(f"SELECT {ts_col} FROM {table} WHERE id>=? ORDER BY id LIMIT 1", (i,))
        r = cur.fetchone()
        return r[0] if r else None

    a, b = lo, hi
    while a < b:
        m = (a + b) // 2
        t = at_of(m)
        if t is None or t < target:
            a = m + 1
        else:
            b = m
    return a


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-16T17:30")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    cur = con.cursor()

    # ---- decisions -------------------------------------------------------
    d0 = first_id_at_or_after(cur, "decisions", "at", args.since)
    kinds: collections.Counter = collections.Counter()
    reasons: collections.Counter = collections.Counter()
    cats: collections.Counter = collections.Counter()
    cur.execute("SELECT kind, reasons_json FROM decisions WHERE id>=?", (d0,))
    for kind, rj in cur:
        kinds[kind] += 1
        rs = set(json.loads(rj))
        for r in rs:
            reasons[(kind, r)] += 1
        if kind != "no_quote":
            continue
        if rs & FILTER:
            cats["filter_cheap"] += 1
        elif rs == {"skip_rfq_closed"}:
            cats["priced_risk_ok_post_dead"] += 1
        elif rs & RISK:
            cats["priced_then_risk_declined"] += 1
        elif rs & PRICE:
            cats["pricing_stage"] += 1
        else:
            cats["other"] += 1
    print("kinds:", dict(kinds))
    print("stage categories:", dict(cats))
    for (k, r), n in reasons.most_common(25):
        print(f"{n:>8}  {k:<13} {r}")

    # ---- rfq duplication + latency --------------------------------------
    r0 = first_id_at_or_after(cur, "rfqs", "seen_at", args.since)
    sig_count: collections.Counter = collections.Counter()
    seen_at: dict[str, str] = {}
    cur.execute(
        "SELECT rfq_id, seen_at, collection_ticker, legs_json FROM rfqs WHERE id>=?",
        (r0,),
    )
    n = 0
    for rfq_id, seen, coll, legs in cur:
        n += 1
        try:
            legs_l = json.loads(legs)
            sig = (coll, tuple(sorted((l.get("market_ticker"), l.get("side")) for l in legs_l)))
        except Exception:
            sig = (coll, legs)
        sig_count[sig] += 1
        seen_at[rfq_id] = seen
    print(f"rfqs={n} unique_signatures={len(sig_count)} dup_factor={n / max(1, len(sig_count)):.1f}x")
    print("top_repeats:", [c for _, c in sig_count.most_common(5)])

    lat: list[float] = []
    cur.execute("SELECT rfq_id, at FROM decisions WHERE id>=? AND kind='quote_sent'", (d0,))
    for rfq_id, at in cur:
        s = seen_at.get(rfq_id)
        if s:
            lat.append((datetime.fromisoformat(at) - datetime.fromisoformat(s)).total_seconds())
    lat.sort()
    if lat:
        def pct(p: float) -> float:
            return lat[min(len(lat) - 1, int(p * len(lat)))]
        over2 = 100 * (len(lat) - bisect.bisect_left(lat, 2.0)) / len(lat)
        print(
            f"quote latency n={len(lat)} p50={pct(.5):.3f}s p90={pct(.9):.3f}s "
            f"p99={pct(.99):.3f}s over2s={over2:.1f}%"
        )
    con.close()


if __name__ == "__main__":
    main()
