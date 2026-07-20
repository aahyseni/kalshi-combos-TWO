"""Zero-remainder RFQ flow ledger over the last 48h — live DB, READ-ONLY.

Universe = rfqs table of data/combomaker-prod-live-wc.sqlite3 (worker-processed,
in-allowlist RFQs; the pre-parse fastpath drop + queue drops are metrics-only
and are ledgered separately from log snapshots).

Terminal bucket per rfq_id (exactly one):
  - quoted            : any quote_sent decision. Sub-outcome: confirmed /
                        declined@confirm / deleted:<reason> / open-or-lapsed.
  - skip:<reason>     : else, the FIRST reason of the LAST no_quote decision
                        (filter order / severity order => binding reason).
  - no_decision       : rfq row exists but zero decision rows (enumerated).

Zero remainder enforced: sum(buckets) == COUNT(rfqs in window).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DB = "data/combomaker-prod-live-wc.sqlite3"
HOURS = 48.0


def connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)


def min_id_since(cur: sqlite3.Cursor, table: str, ts_col: str, cutoff: str) -> int:
    """Binary search the first id with ts >= cutoff (id is append-order)."""
    lo = cur.execute(f"SELECT MIN(id) FROM {table}").fetchone()[0]
    hi = cur.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
    if lo is None:
        return 0
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


def main() -> None:
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=HOURS)).isoformat()
    con = connect()
    cur = con.cursor()

    rfq_lo = min_id_since(cur, "rfqs", "seen_at", cutoff)
    dec_lo = min_id_since(cur, "decisions", "at", cutoff)
    rfq_hi = cur.execute("SELECT MAX(id) FROM rfqs").fetchone()[0]
    dec_hi = cur.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
    print(f"window: seen_at >= {cutoff}  (now={now.isoformat()})", flush=True)
    print(f"rfqs id range   [{rfq_lo}, {rfq_hi}]  n={rfq_hi - rfq_lo + 1}", flush=True)
    print(f"decisions range [{dec_lo}, {dec_hi}]  n={dec_hi - dec_lo + 1}", flush=True)

    # ---- pass 1: decisions -> per-rfq terminal state (compact) ----
    # state[rfq_id] = [saw_quote_sent, last_noquote_primary, outcome, last_delete_reason,
    #                  n_noquote_decisions]
    state: dict[str, list] = {}
    ever_reasons: Counter[str] = Counter()  # reason -> n RFQs that ever saw it (dedup below)
    seen_reason_pairs: set = set()
    kinds = Counter()
    CH = 200_000
    lo = dec_lo
    while lo <= dec_hi:
        rows = cur.execute(
            "SELECT id, kind, rfq_id, reasons_json FROM decisions"
            " WHERE id >= ? AND id < ? ORDER BY id",
            (lo, min(lo + CH, dec_hi + 1)),
        ).fetchall()
        for _id, kind, rfq_id, reasons_json in rows:
            kinds[kind] += 1
            if rfq_id is None:
                continue
            st = state.get(rfq_id)
            if st is None:
                st = state[rfq_id] = [False, None, None, None, 0]
            if kind == "no_quote":
                try:
                    reasons = json.loads(reasons_json)
                except Exception:
                    reasons = ["unparseable_reasons"]
                st[1] = reasons[0] if reasons else "empty_reasons"
                st[4] += 1
                for r in set(reasons):
                    key = (rfq_id, r)
                    if key not in seen_reason_pairs:
                        seen_reason_pairs.add(key)
                        ever_reasons[r] += 1
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
        print(f"  decisions pass at id {lo} ({len(state)} rfqs)", file=sys.stderr, flush=True)

    # ---- pass 2: rfqs in window -> buckets ----
    buckets: Counter[str] = Counter()
    quoted_sub: Counter[str] = Counter()
    no_decision_ids: list[str] = []
    series_by_bucket: dict[str, Counter] = defaultdict(Counter)
    nlegs_by_bucket: dict[str, Counter] = defaultdict(Counter)
    total = 0
    lo = rfq_lo
    while lo <= rfq_hi:
        rows = cur.execute(
            "SELECT id, rfq_id, n_legs, legs_json FROM rfqs"
            " WHERE id >= ? AND id < ? ORDER BY id",
            (lo, min(lo + CH, rfq_hi + 1)),
        ).fetchall()
        for _id, rfq_id, n_legs, legs_json in rows:
            total += 1
            st = state.get(rfq_id)
            if st is None:
                bucket = "no_decision"
                if len(no_decision_ids) < 50:
                    no_decision_ids.append(rfq_id)
            elif st[0]:
                bucket = "quoted"
                if st[2]:
                    quoted_sub[st[2]] += 1
                elif st[3]:
                    quoted_sub[f"deleted:{st[3]}"] += 1
                else:
                    quoted_sub["open_or_lapsed_no_delete_row"] += 1
            elif st[1]:
                bucket = f"skip:{st[1]}"
            else:
                bucket = "decision_rows_but_unclassified"
            buckets[bucket] += 1
            # series mix: distinct series prefixes across legs (ticker before first '-')
            try:
                prefixes = sorted({
                    leg["market_ticker"].split("-", 1)[0]
                    for leg in json.loads(legs_json)
                })
                series_by_bucket[bucket]["|".join(prefixes)[:60]] += 1
            except Exception:
                series_by_bucket[bucket]["parse_error"] += 1
            nlegs_by_bucket[bucket][n_legs] += 1
        lo += CH
        print(f"  rfqs pass at id {lo} (total {total})", file=sys.stderr, flush=True)

    print("\n==== TERMINAL BUCKETS (one per RFQ) ====")
    print(f"total_rfqs_in_window\t{total}\t100.00%")
    ssum = 0
    for b, n in buckets.most_common():
        ssum += n
        print(f"{b}\t{n}\t{100.0 * n / total:.3f}%")
    print(f"SUM_CHECK\t{ssum}\t(== total: {ssum == total})")

    print("\n==== QUOTED SUB-OUTCOMES ====")
    for b, n in quoted_sub.most_common():
        print(f"{b}\t{n}")

    print("\n==== decision kinds in window (row counts, not RFQs) ====")
    for k, n in kinds.most_common():
        print(f"{k}\t{n}")

    print("\n==== reasons EVER seen per RFQ (co-occurrence; sums > total) ====")
    for r, n in ever_reasons.most_common():
        print(f"{r}\t{n}")

    print("\n==== top series-mix per bucket (top 12 buckets, top 5 mixes) ====")
    for b, n in buckets.most_common(12):
        mixes = "; ".join(f"{m}={c}" for m, c in series_by_bucket[b].most_common(5))
        print(f"{b}: {mixes}")

    print("\n==== n_legs per bucket (top 12 buckets) ====")
    for b, _n in buckets.most_common(12):
        legs = "; ".join(f"{k}:{c}" for k, c in sorted(nlegs_by_bucket[b].items())[:10])
        print(f"{b}: {legs}")

    if no_decision_ids:
        print("\n==== sample no_decision rfq_ids ====")
        for r in no_decision_ids[:10]:
            print(r)


if __name__ == "__main__":
    main()
