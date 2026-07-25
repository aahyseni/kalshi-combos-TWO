"""Plain-English DECLINE read-out (operator ask 2026-07-25: "we're hitting a
lot of declines although it's so hard to read what for").

Reads a live jsonl log and prints one human line per confirm-phase decline:
when, how big, which wall, and what it means. Read-only.

Usage:
  .venv/Scripts/python.exe tools/diagnostics/decline_readout.py data/<log> [--since HH:MM]
"""

from __future__ import annotations

import argparse
import json
import re
import sys

# Wall → plain English (the walls a decline's detail can name).
EXPLAIN = [
    (r"ACCUMULATED", "SAME-STRUCTURE WALL: we already hold too much of this exact combo (anti-hammering, 5% of bankroll)"),
    (r"combo .* loss .* 1/20 bankroll", "PER-COMBO WALL: this single fill's worst loss exceeds 5% of bankroll"),
    (r"slate .* 13/20", "SLATE CEILING: tonight's all-games worst case above 65% of bankroll (analytic belt)"),
    (r"game .* not certifiable", "WAIVER MISS: per-game exact certificate could not be computed in time"),
    (r"book moved during every enumeration", "WAIVER MISS: fills kept landing while the waiver was computing (peak-flow gap, fix queued)"),
    (r"negative_ev_not_risk_reducing", "GATE MODEL SAYS BAD PRICE: at fill size the admission model scores this fill negative-EV and it doesn't reduce our tail (pickoff shield OR pricing/gate model disagreement)"),
    (r"post_kill_tail_prob_over_budget", "KILL-NIGHT ODDS: this fill pushes P(losing a KILL-scale night) above the 2% anchor"),
    (r"post_governing_model_es_over_budget", "ES CEILING (legacy form): post-book tail average above the budget"),
    (r"negative_ev_exceeds_hedge_budget", "HEDGE TOO EXPENSIVE: risk-reducing but costs more EV than the tail it removes"),
    (r"reservation denied", "NO HEADROOM AT CONFIRM: the serial reservation found the true-size fill breaches a wall"),
    (r"leg_stale|stale", "STALE DATA: a leg's book/price went stale between quote and confirm"),
    (r"unstable", "BOOK UNSTABLE: state kept changing during the check (fail-closed)"),
]


def explain(detail: str, reason: str) -> str:
    hits = [msg for pat, msg in EXPLAIN if re.search(pat, detail or reason)]
    return " + ".join(dict.fromkeys(hits)) if hits else f"(unmapped: {reason})"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--since", default="")
    a = ap.parse_args()

    rows = []
    with open(a.log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"phase": "decline"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = str(d.get("ts", ""))[11:19]
            if a.since and ts < a.since:
                continue
            ev = d.get("candidate_ev_cc")
            rows.append(
                (
                    ts,
                    f"${ev / 10000:+.2f}" if isinstance(ev, (int, float)) else "?",
                    str(d.get("rfq_id", ""))[:8],
                    explain(str(d.get("detail", "")), str(d.get("reason", ""))),
                )
            )

    print(f"=== DECLINES ({a.log}) - {len(rows)} total ===")
    print(f"{'time':<9}{'ourEV':<9}{'rfq':<10}why")
    for ts, ev, rfq, why in rows:
        print(f"{ts:<9}{ev:<9}{rfq:<10}{why}")
    counts: dict[str, int] = {}
    for _, _, _, why in rows:
        key = why.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    print("\nby wall:")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>3}x {k}")


if __name__ == "__main__":
    main()
