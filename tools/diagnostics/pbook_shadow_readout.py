"""P(book)-steer SHADOW READ-OUT (2026-07-25) — the derive-before-arm read.

Parses the live jsonl log and prints the arming evidence:
  1. pbook_cc distribution (the would-be steer per quote) + reason histogram
     + factor stats per reason — the magnitudes the operator eyeballs.
  2. delta_p_book at the candidate gate (every verdict + admitted fills).
  3. p_book trace (snapshot publishes) + top tail games over time.
  4. Every confirm-phase decline (reason + rfq).
  5. Arm-gate proxy: overweight-game rows whose classification came from the
     mass-acceptance contribution sign (the committed-vs-mass-acc
     disagreement surface) — count + sign mix for the eyeball.

Read-only; all flow is PREGAME by construction (the pregame gate never
quotes started legs). Usage:
  .venv/Scripts/python.exe tools/diagnostics/pbook_shadow_readout.py \
      data/live_20260725_pbook_shadow2.log [--since 2026-07-25T15:12]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--since", default="")
    a = ap.parse_args()

    pbook_cc: list[int] = []
    reasons: Counter[str] = Counter()
    factors: dict[str, list[float]] = defaultdict(list)
    adders: dict[str, list[int]] = defaultdict(list)
    gate_deltas: list[float] = []
    confirm_deltas: list[float] = []
    p_book_trace: list[tuple[str, float, int]] = []
    declines: list[dict] = []
    overweight_sign: Counter[str] = Counter()
    n_skew = 0

    with open(a.log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"event"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = str(d.get("ts", ""))
            if a.since and ts and ts < a.since:
                continue
            ev = d.get("event")
            if ev == "inventory_skew_shadow":
                n_skew += 1
                pbook_cc.append(int(d.get("pbook_cc", 0)))
                for row in d.get("pbook_per_game") or []:
                    game, adder, factor, reason = row
                    reasons[reason] += 1
                    factors[reason].append(float(factor))
                    adders[reason].append(int(adder))
                    if reason in ("pbook_concentrating", "pbook_offsetting"):
                        overweight_sign[reason] += 1
            elif ev == "candidate_gate_ev":
                delta = d.get("delta_p_book")
                if delta is not None:
                    gate_deltas.append(float(delta))
            elif ev == "candidate_gate_confirm":
                delta = d.get("delta_p_book")
                if delta is not None:
                    confirm_deltas.append(float(delta))
            elif ev == "book_risk_snapshot":
                p_book_trace.append(
                    (ts[11:19], float(d.get("p_book", 0.0)),
                     int(d.get("n_positions", 0)))
                )
            elif d.get("phase") == "decline":
                declines.append(
                    {"ts": ts[11:19], "reason": d.get("reason"),
                     "rfq": str(d.get("rfq_id"))[:13],
                     "ev_cc": d.get("candidate_ev_cc")}
                )

    print(f"=== P(BOOK) SHADOW READ-OUT ({a.log}) ===")
    print(f"skew events: {n_skew}")
    nz = [v for v in pbook_cc if v != 0]
    print(f"\n1) pbook_cc (would-be steer, cc): nonzero {len(nz)}/{len(pbook_cc)}")
    if nz:
        widen = [v for v in nz if v > 0]
        rebate = [v for v in nz if v < 0]
        print(f"   widen  n={len(widen)}  mean {statistics.mean(widen):.0f}cc  "
              f"p50 {_pct([float(v) for v in widen], 0.5):.0f}  "
              f"max {max(widen)}cc" if widen else "   widen  n=0")
        print(f"   rebate n={len(rebate)}  mean {statistics.mean(rebate):.0f}cc  "
              f"p50 {_pct([float(v) for v in rebate], 0.5):.0f}  "
              f"min {min(rebate)}cc" if rebate else "   rebate n=0")
    print("\n   reasons:")
    for reason, n in reasons.most_common():
        fs = factors[reason]
        ads = adders[reason]
        print(f"     {reason:<28} n={n:<6} factor p50 {_pct(fs, 0.5):.3f} "
              f"p90 {_pct(fs, 0.9):.3f}  adder p50 {_pct([float(x) for x in ads], 0.5):.0f}cc")
    print(f"\n2) delta_p_book: gate verdicts n={len(gate_deltas)}  "
          f"p50 {_pct(gate_deltas, 0.5):+.4f}  "
          f"p10 {_pct(gate_deltas, 0.1):+.4f}  p90 {_pct(gate_deltas, 0.9):+.4f}")
    print(f"   ADMITTED fills n={len(confirm_deltas)}: "
          f"{[round(x, 4) for x in confirm_deltas[-10:]]}")
    print(f"\n3) p_book trace (last 10 of {len(p_book_trace)}):")
    for ts, pb, n in p_book_trace[-10:]:
        print(f"     {ts}  p_book {pb:.4f}  n_pos {n}")
    print(f"\n4) confirm-phase declines: {len(declines)}")
    for dd in declines:
        print(f"     {dd['ts']}  {dd['reason']}  rfq {dd['rfq']}  ev {dd['ev_cc']}")
    print(f"\n5) arm-gate surface (overweight-game rows classified from the "
          f"mass-acceptance sign): {dict(overweight_sign)}")


if __name__ == "__main__":
    main()
