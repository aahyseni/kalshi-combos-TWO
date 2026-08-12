"""RFQ series-demand census — which sports do takers actually request?

Read-only (store opened ``mode=ro``). Samples the recent ``rfqs`` window and
counts arriving RFQs by leg-series prefix, split allowlisted vs not. The
``rfqs`` table is written for EVERY ws ``rfq_created`` BEFORE any filter or
pricing (``quote_app.handle_rfq_record_after`` records first), so a prefix
absent here is absent from taker demand, not filtered by us.

This is the NEW-SPORT DEMAND DETECTOR (2026-08-12 operator direction: wire
more sports for diversification): run it weekly — any non-allowlisted prefix
with real flow is the trigger to start the sport-onboarding playbook
(docs/sport_onboarding_playbook.md) for that sport. It is also the inverse of
the F5 lesson: F5's pickoff flow was visible on this axis before we knew to
look.

Usage:
  .venv/Scripts/python.exe tools/diagnostics/rfq_series_census.py \
      [--db data/combomaker-prod-live-wc.sqlite3] [--rows 8000000] [--sample 40]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Keep in sync with filters.allowed_leg_series_prefixes in the live config —
# read here only to LABEL the census (this tool never touches the filter).
DEFAULT_ALLOWED = (
    "KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD", "KXMLBKS", "KXMLBHIT", "KXMLBHR",
    "KXMLBHRR", "KXMLBRFI", "KXMLBTB", "KXMLBSB", "KXMLBOUTS", "KXMLBRBI",
    "KXLOLGAME", "KXCS2GAME", "KXCSGOGAME",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/combomaker-prod-live-wc.sqlite3")
    ap.add_argument("--rows", type=int, default=8_000_000,
                    help="how many newest rfqs rows the window covers")
    ap.add_argument("--sample", type=int, default=40,
                    help="sample 1 in N rows (id modulus)")
    args = ap.parse_args()

    if not Path(args.db).is_file():
        print(f"no such db: {args.db}", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        (max_id,) = con.execute("SELECT MAX(id) FROM rfqs").fetchone()
        lo = max(0, max_id - args.rows)
        t0 = con.execute(
            "SELECT seen_at FROM rfqs WHERE id > ? ORDER BY id LIMIT 1", (lo,)
        ).fetchone()
        t1 = con.execute(
            "SELECT seen_at FROM rfqs WHERE id = ?", (max_id,)
        ).fetchone()
        print(
            f"window ids ({lo}, {max_id}] seen_at {t0[0] if t0 else '?'} .. "
            f"{t1[0] if t1 else '?'}, sampling 1/{args.sample}"
        )

        series: Counter[str] = Counter()
        outside_sig: Counter[str] = Counter()
        n = bad = 0
        for (legs_json,) in con.execute(
            "SELECT legs_json FROM rfqs WHERE id > ? AND (id % ?) = 0",
            (lo, args.sample),
        ):
            n += 1
            try:
                legs = json.loads(legs_json)
            except (TypeError, json.JSONDecodeError):
                bad += 1
                continue
            prefixes = set()
            for leg in legs:
                tick = leg.get("ticker") or leg.get("market_ticker") or ""
                if tick:
                    prefixes.add(tick.split("-", 1)[0])
            for p in prefixes:
                series[p] += 1
            outside = sorted(p for p in prefixes if p not in DEFAULT_ALLOWED)
            if outside:
                outside_sig[",".join(outside)] += 1

        print(f"sampled {n:,} RFQs (~{n * args.sample:,} in window), "
              f"{bad} unparseable (listed, never dropped silently)")
        print("\nleg series prefixes (sampled counts):")
        for p, c in series.most_common():
            mark = "" if p in DEFAULT_ALLOWED else "   <-- NOT ALLOWLISTED"
            print(f"  {p:34s} {c:8,d}{mark}")
        if outside_sig:
            print("\nNON-ALLOWLISTED demand signatures (RFQs containing them):")
            for sig, c in outside_sig.most_common(30):
                print(f"  {sig:60s} {c:8,d}")
            print("\n>>> new-sport demand detected: start the onboarding "
                  "playbook for the prefixes above.")
        else:
            print("\nZERO non-allowlisted RFQs in the window — current taker "
                  "demand is fully inside the allowlist.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
