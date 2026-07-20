"""Firehose-window ledger + no_decision time clustering + raw examples.

READ-ONLY on data/combomaker-prod-live-wc.sqlite3.

1. Mini-ledger for the waiver_tiers run window 2026-07-16T17:30:23Z ->
   18:13:23Z (game-day firehose: 600,222 ws rfq_created msgs, 232.6/s,
   per the run's quote_app_stopped metric snapshot).
2. Per-hour histogram of the 48h no_decision bucket (kill-cluster test).
3. 2 raw examples per top addressable bucket (rfq + decision timeline).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DB = "data/combomaker-prod-live-wc.sqlite3"
W_START = "2026-07-16T17:30:23"
W_END = "2026-07-16T18:13:24"

EXAMPLE_BUCKETS = {
    "skip:skip_rfq_closed",
    "skip:skip_max_open_quotes",
    "skip:skip_price_deadline",
    "skip:skip_game_loss_cap",
}


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)


def min_id_since(cur, table, ts_col, cutoff):
    lo = cur.execute(f"SELECT MIN(id) FROM {table}").fetchone()[0]
    hi = cur.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
    while lo < hi:
        mid = (lo + hi) // 2
        row = cur.execute(
            f"SELECT {ts_col} FROM {table} WHERE id >= ? ORDER BY id LIMIT 1", (mid,)
        ).fetchone()
        if row is None or row[0] >= cutoff:
            hi = mid
        else:
            lo = mid + 1
    return lo


def build_state(cur, dec_lo, dec_hi):
    state = {}
    CH = 200_000
    lo = dec_lo
    while lo <= dec_hi:
        for _id, kind, rfq_id, reasons_json in cur.execute(
            "SELECT id, kind, rfq_id, reasons_json FROM decisions"
            " WHERE id >= ? AND id < ? ORDER BY id",
            (lo, min(lo + CH, dec_hi + 1)),
        ):
            if rfq_id is None:
                continue
            st = state.get(rfq_id)
            if st is None:
                st = state[rfq_id] = [False, None, None, None]
            if kind == "no_quote":
                try:
                    st[1] = json.loads(reasons_json)[0]
                except Exception:
                    st[1] = "unparseable"
            elif kind == "quote_sent":
                st[0] = True
            elif kind == "confirm":
                st[2] = "confirmed"
            elif kind == "decline":
                if st[2] != "confirmed":
                    st[2] = "declined_at_confirm"
            elif kind == "quote_deleted":
                try:
                    st[3] = json.loads(reasons_json)[0]
                except Exception:
                    st[3] = "unparseable"
        lo += CH
    return state


def main() -> None:
    con = connect()
    cur = con.cursor()

    # ---- 1. firehose-window mini-ledger ----
    rfq_lo = min_id_since(cur, "rfqs", "seen_at", W_START)
    rfq_hi = min_id_since(cur, "rfqs", "seen_at", W_END) - 1
    dec_lo = min_id_since(cur, "decisions", "at", W_START)
    dec_hi = cur.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    print(f"firehose window {W_START}Z..{W_END}Z rfq ids [{rfq_lo},{rfq_hi}]")
    state = build_state(cur, dec_lo, dec_hi)

    buckets = Counter()
    quoted_sub = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    total = 0
    for _id, rfq_id in cur.execute(
        "SELECT id, rfq_id FROM rfqs WHERE id >= ? AND id <= ? ORDER BY id",
        (rfq_lo, rfq_hi),
    ).fetchall():
        total += 1
        st = state.get(rfq_id)
        if st is None:
            b = "no_decision"
        elif st[0]:
            b = "quoted"
            quoted_sub[st[2] or (f"deleted:{st[3]}" if st[3] else "open_or_lapsed")] += 1
        elif st[1]:
            b = f"skip:{st[1]}"
        else:
            b = "unclassified"
        buckets[b] += 1
        if b in EXAMPLE_BUCKETS and len(examples[b]) < 2:
            examples[b].append(rfq_id)

    print(f"\n==== FIREHOSE WINDOW BUCKETS (total {total}) ====")
    ssum = 0
    for b, n in buckets.most_common():
        ssum += n
        print(f"{b}\t{n}\t{100.0 * n / total:.3f}%")
    print(f"SUM_CHECK\t{ssum}\t(== total: {ssum == total})")
    print("\n---- quoted sub-outcomes ----")
    for b, n in quoted_sub.most_common():
        print(f"{b}\t{n}")

    # ---- 2. no_decision per-hour histogram over 48h ----
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=48)).isoformat()
    rfq48_lo = min_id_since(cur, "rfqs", "seen_at", cutoff)
    dec48_lo = min_id_since(cur, "decisions", "at", cutoff)
    max_rfq = cur.execute("SELECT MAX(id) FROM rfqs").fetchone()[0]
    dec_ids = set()
    CH = 400_000
    lo = dec48_lo
    while lo <= dec_hi:
        for (rid,) in cur.execute(
            "SELECT DISTINCT rfq_id FROM decisions WHERE id >= ? AND id < ?",
            (lo, min(lo + CH, dec_hi + 1)),
        ):
            if rid:
                dec_ids.add(rid)
        lo += CH
    hist = Counter()
    per_hour_total = Counter()
    lo = rfq48_lo
    while lo <= max_rfq:
        for rfq_id, seen_at in cur.execute(
            "SELECT rfq_id, seen_at FROM rfqs WHERE id >= ? AND id < ?",
            (lo, min(lo + CH, max_rfq + 1)),
        ):
            hour = seen_at[:13]
            per_hour_total[hour] += 1
            if rfq_id not in dec_ids:
                hist[hour] += 1
        lo += CH
    print("\n==== no_decision per hour (48h) — hour, no_decision, total, % ====")
    for h in sorted(per_hour_total):
        n, t = hist.get(h, 0), per_hour_total[h]
        print(f"{h}\t{n}\t{t}\t{100.0 * n / t:.2f}%")

    # ---- 3. raw examples ----
    print("\n==== RAW EXAMPLES (top addressable buckets) ====")
    for b, ids in examples.items():
        for rid in ids:
            row = cur.execute(
                "SELECT seen_at, market_ticker, collection_ticker, n_legs,"
                " contracts_centi, target_cost_cc, legs_json FROM rfqs WHERE rfq_id=?",
                (rid,),
            ).fetchone()
            legs = [
                f"{l.get('side')}:{l['market_ticker']}" for l in json.loads(row[6])
            ]
            print(f"\n--- {b} | rfq_id={rid}")
            print(
                f"    seen_at={row[0]} market={row[1]} n_legs={row[3]}"
                f" contracts_centi={row[4]} target_cost_cc={row[5]}"
            )
            print(f"    legs={legs}")
            for at, kind, reasons, ctx in cur.execute(
                "SELECT at, kind, reasons_json, substr(context_json,1,220)"
                " FROM decisions WHERE rfq_id=? ORDER BY id",
                (rid,),
            ):
                print(f"    {at} {kind} {reasons} ctx={ctx}")


if __name__ == "__main__":
    main()
