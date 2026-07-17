"""Throughput funnel — the operator's Batch-1 KPI (2026-07-17).

"The best measurement is amount of RFQs quoted (declines because of risk and
MC stops don't count); it should be RFQs priced and how many we miss due to
not being fast enough."

Reads the live decisions table (READ-ONLY, hard rule 8) and classifies every
decision into:

  QUOTED      quote_sent
  RISK_STOP   deliberate declines (risk caps / MC / sizing / policy) — the bot
              was FAST ENOUGH, it chose not to quote; not a throughput miss
  SPEED_MISS  the RFQ was in-scope and we lost it to latency:
                skip_rfq_closed             (closed before our POST landed)
                skip_rfq_deleted_midflight  (F2: died while in our pipeline)
                skip_price_deadline         (pricing pool too slow)
  DATA_POLICY market-quality / data fail-closed no-quotes (thin book, wide
              spread, unknown classifier/leg/start) — neither speed nor risk

Precedence per decision row: RISK_STOP > SPEED_MISS > DATA_POLICY (a row
carrying both a risk reason and rfq_closed would have been declined anyway —
not a speed miss).

KPIs per window:
  in_scope        = QUOTED + RISK_STOP + SPEED_MISS
  handled_in_time = (QUOTED + RISK_STOP) / in_scope   <- Batch-1 target: UP
  speed_miss_rate = SPEED_MISS / in_scope             <- Batch-1 target: DOWN
  quoted_per_min

Usage:
  .venv/Scripts/python.exe tools/diagnostics/throughput_funnel.py \
      --window "pre-batch1" 2026-07-17T01:02 2026-07-17T02:19 \
      --window "post-batch1" 2026-07-17T02:21 2026-07-17T09:00

Caveats: overnight flow shifts hour to hour — same-night windows are
indicative, game day is the real read. Queue evictions never reach the DB
(watch the intake metrics in the live log for those).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime

SPEED = {"skip_rfq_closed", "skip_rfq_deleted_midflight", "skip_price_deadline"}
RISK = {
    "skip_game_loss_cap", "skip_max_open_quotes", "skip_per_combo_loss_cap",
    "skip_mass_acceptance_breach", "skip_slate_cap", "skip_directional_cap",
    "skip_portfolio_det_max", "skip_portfolio_cvar", "skip_utilization_backstop",
    "skip_size_above_max", "skip_size_below_min", "skip_logically_impossible",
    "skip_bankroll_unavailable", "decline_candidate_risk", "decline_risk_limit",
}


def classify(reasons: list[str]) -> str:
    rs = set(reasons)
    if rs & RISK:
        return "RISK_STOP"
    if rs & SPEED:
        return "SPEED_MISS"
    return "DATA_POLICY"


def funnel(db: str, label: str, since: str, until: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    buckets: Counter[str] = Counter()
    speed_split: Counter[str] = Counter()
    risk_split: Counter[str] = Counter()
    rows = conn.execute(
        "select kind, reasons_json from decisions where at >= ? and at < ?",
        (since, until),
    )
    for kind, rj in rows:
        if kind == "quote_sent":
            buckets["QUOTED"] += 1
            continue
        if kind != "no_quote":
            continue  # quote_deleted etc. are lifecycle, not intake funnel
        try:
            reasons = json.loads(rj)
        except (TypeError, ValueError):
            buckets["DATA_POLICY"] += 1
            continue
        bucket = classify(reasons)
        buckets[bucket] += 1
        if bucket == "SPEED_MISS":
            for r in set(reasons) & SPEED:
                speed_split[r] += 1
        elif bucket == "RISK_STOP":
            for r in set(reasons) & RISK:
                risk_split[r] += 1
    lo, hi = (
        conn.execute(
            "select min(at), max(at) from decisions where at >= ? and at < ?",
            (since, until),
        ).fetchone()
    )
    minutes = 0.0
    if lo and hi:
        minutes = (
            datetime.fromisoformat(hi) - datetime.fromisoformat(lo)
        ).total_seconds() / 60.0
    return {
        "label": label, "since": since, "until": until, "minutes": minutes,
        "buckets": buckets, "speed_split": speed_split, "risk_split": risk_split,
    }


def report(w: dict) -> None:
    b = w["buckets"]
    in_scope = b["QUOTED"] + b["RISK_STOP"] + b["SPEED_MISS"]
    mins = max(w["minutes"], 1e-9)
    print(f"\n=== {w['label']}  [{w['since']} .. {w['until']}]  ({mins:.1f} min) ===")
    print(
        f"QUOTED {b['QUOTED']:>7}   RISK_STOP {b['RISK_STOP']:>7}   "
        f"SPEED_MISS {b['SPEED_MISS']:>7}   DATA_POLICY {b['DATA_POLICY']:>7}"
    )
    if in_scope:
        print(
            f"KPI  quoted/min {b['QUOTED'] / mins:8.1f}   "
            f"handled-in-time {(b['QUOTED'] + b['RISK_STOP']) / in_scope:7.1%}   "
            f"speed-miss {b['SPEED_MISS'] / in_scope:7.1%}   "
            f"(in-scope {in_scope}, {in_scope / mins:.0f}/min)"
        )
    for name, split in (("speed", w["speed_split"]), ("risk", w["risk_split"])):
        if split:
            top = "  ".join(f"{r}={n}" for r, n in split.most_common(6))
            print(f"  {name}: {top}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/combomaker-prod-live-wc.sqlite3")
    p.add_argument(
        "--window", nargs=3, action="append", required=True,
        metavar=("LABEL", "SINCE", "UNTIL"),
    )
    args = p.parse_args()
    for label, since, until in args.window:
        report(funnel(args.db, label, since, until))


if __name__ == "__main__":
    main()
