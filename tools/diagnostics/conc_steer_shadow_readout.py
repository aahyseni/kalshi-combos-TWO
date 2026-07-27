"""LEVER #5 CONCENTRATION STEER — SHADOW READ-OUT (2026-07-27, review B3).

THE ARMING DECISION, ON ONE PAGE. The steer ships SHADOW: it is computed and
logged on every quote and cannot touch price until ``pricing.skew.conc_armed:
true``. This parses the live jsonl and prints exactly what the operator asked to
see before flipping that flag:

  1. WHAT IT WOULD HAVE DONE. ``conc_shadow_applied_cc`` is the pricer-frame
     number the ARMED composition would have produced on the same inputs (the
     classifier's own armed branch, not a re-derivation), against
     ``applied_cc`` — the number that actually shipped. Distribution + the delta.
  2. BUDGET NEUTRALITY. Markups are FROZEN. The mean delta must not be negative
     (negative = the average quote WIDENS = a markup change nobody authorised).
  3. THE ORDERING THE OPERATOR ASKED FOR: diversifiers STRICTLY TIGHTER than
     concentrators, bucketed by the measured ``conc_intensity`` (>0 =
     concentrating, <0 = diversifying), with the gap in cc and as a fraction of
     the live margin.
  4. NO BOOK-SIZE WIDENING. The same read sliced by BOOK SIZE (the AND-bound
     effective loss-event count, ``conc_n_events_pre``). The rule is that flow
     which does not concentrate us must not get wider as the book grows: the
     diversifier bucket's median must not fall across the size slices.
  5. WHAT IT COSTS AT THE MARGIN: the implied fill-rate effect at the measured
     CMH-stratified elasticity, per bucket.
  6. HEALTH: reason codes, which derived bound is binding the half-range, the
     sub-tick / annihilation share, and the live centre trace.

Read-only. Usage:
  .venv/Scripts/python.exe tools/diagnostics/conc_steer_shadow_readout.py \
      data/live_20260727.log [--since 2026-07-27T15:12] [--elasticity 0.22]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter


def _q(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(p * len(s)))]


def _stats(xs: list[float]) -> str:
    if not xs:
        return "        (none)"
    return (
        f"n={len(xs):<7d} mean {statistics.mean(xs):+8.1f}  "
        f"med {statistics.median(xs):+8.1f}  "
        f"p10 {_q(xs, 0.10):+8.1f}  p90 {_q(xs, 0.90):+8.1f}  "
        f"[{min(xs):+.0f} .. {max(xs):+.0f}]"
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--since", default="")
    ap.add_argument(
        "--elasticity", type=float, default=0.22,
        help="measured CMH-stratified fill-rate elasticity, fills lost per cent",
    )
    a = ap.parse_args()

    rows: list[dict] = []
    armed_seen = 0
    with open(a.log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "inventory_skew_shadow" not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("event") != "inventory_skew_shadow":
                continue
            ts = str(d.get("ts", ""))
            if a.since and ts and ts < a.since:
                continue
            if d.get("conc_reason") is None:
                continue        # steer not computed on this quote
            if d.get("conc_armed"):
                armed_seen += 1
            rows.append(d)

    if not rows:
        print("no inventory_skew_shadow rows with a computed steer in that window")
        return

    shipped = [float(r.get("applied_cc") or 0) for r in rows]
    would = [
        float(r["conc_shadow_applied_cc"])
        if r.get("conc_shadow_applied_cc") is not None
        else float(r.get("applied_cc") or 0)
        for r in rows
    ]
    delta = [w - s for w, s in zip(would, shipped, strict=True)]
    intensity = [
        float(r["conc_intensity"]) if r.get("conc_intensity") is not None else 0.0
        for r in rows
    ]

    print("=" * 78)
    print(f"LEVER #5 SHADOW READ-OUT — {len(rows):,} quotes with a computed steer")
    if armed_seen:
        print(f"  !! {armed_seen:,} of these were ALREADY ARMED (conc_armed=true) — "
              f"for those rows 'would' IS what shipped")
    print("=" * 78)

    print()
    print("1. WHAT IT WOULD HAVE DONE  (pricer frame: POSITIVE = TIGHTER quote)")
    print(f"   shipped applied_cc : {_stats(shipped)}")
    print(f"   ARMED  applied_cc  : {_stats(would)}")
    print(f"   delta (armed-ship) : {_stats(delta)}")
    med_abs_ship = statistics.median([abs(x) for x in shipped])
    med_abs_would = statistics.median([abs(x) for x in would])
    print(f"   median |applied|   : {med_abs_ship:.1f} -> {med_abs_would:.1f} cc"
          f"   ({med_abs_would / med_abs_ship:.2f}x)" if med_abs_ship else "")

    print()
    print("2. BUDGET NEUTRALITY  (markups are FROZEN: the mean must not widen)")
    mean_delta = statistics.mean(delta)
    verdict = "OK — reallocates" if mean_delta >= 0 else "!! WIDENS THE AVERAGE"
    print(f"   mean delta {mean_delta:+.2f} cc   {verdict}")
    centres = [float(r["steer_centre_mean"]) for r in rows
               if r.get("steer_centre_mean") is not None]
    sds = [float(r["steer_centre_sd"]) for r in rows
           if r.get("steer_centre_sd") is not None]
    ns = [int(r["steer_centre_n"]) for r in rows if r.get("steer_centre_n")]
    if centres:
        print(f"   live centre: mean {centres[-1]:+.5f}  sd "
              f"{(sds[-1] if sds else 0.0):.5f}  n {(ns[-1] if ns else 0):,}"
              f"   (centre mean range over the window "
              f"{min(centres):+.5f} .. {max(centres):+.5f})")

    print()
    print("3. THE ORDERING  (diversifiers STRICTLY tighter than concentrators)")
    div = [w for w, i in zip(would, intensity, strict=True) if i < 0]
    con = [w for w, i in zip(would, intensity, strict=True) if i > 0]
    neu = [w for w, i in zip(would, intensity, strict=True) if i == 0]
    print(f"   DIVERSIFYING (intensity<0): {_stats(div)}")
    print(f"   CONCENTRATING(intensity>0): {_stats(con)}")
    print(f"   exactly neutral           : n={len(neu)}")
    if div and con:
        gap = statistics.median(div) - statistics.median(con)
        margins = [float(r["conc_half_cc"]) for r in rows
                   if r.get("conc_half_cc") is not None]
        half = statistics.median(margins) if margins else 0.0
        print(f"   median gap {gap:+.1f} cc"
              + (f"   ({gap / (2 * half):.0%} of the full 2x half-range "
                 f"{2 * half:.0f}cc)" if half else ""))
        print("   " + ("OK — diversifiers tighter" if gap > 0
                       else "!! INVERTED — do not arm"))

    print()
    print("4. NO BOOK-SIZE WIDENING  (sliced by AND-bound effective loss events)")
    sized = [
        (float(r["conc_n_events_pre"]), w, i)
        for r, w, i in zip(rows, would, intensity, strict=True)
        if r.get("conc_n_events_pre") is not None
    ]
    if sized:
        sized.sort(key=lambda t: t[0])
        k = max(1, len(sized) // 5)
        print(f"   {'events':>14} {'n':>7} {'DIV med':>9} {'CON med':>9} "
              f"{'all med':>9}")
        for s in range(0, len(sized), k):
            chunk = sized[s:s + k]
            if not chunk:
                continue
            d = [w for _e, w, i in chunk if i < 0]
            c = [w for _e, w, i in chunk if i > 0]
            print(f"   {chunk[0][0]:6.2f}-{chunk[-1][0]:6.2f} {len(chunk):7d} "
                  f"{(statistics.median(d) if d else float('nan')):9.1f} "
                  f"{(statistics.median(c) if c else float('nan')):9.1f} "
                  f"{statistics.median([w for _e, w, _i in chunk]):9.1f}")
        print("   THE RULE: the DIV column must not FALL as events rise "
              "(more book must never widen non-concentrating flow).")

    print()
    print(f"5. IMPLIED FILL-RATE EFFECT @ elasticity {a.elasticity:.2f}/cent")
    for label, xs in (("all", would), ("diversifying", div),
                      ("concentrating", con)):
        if xs:
            m = statistics.mean(xs)
            print(f"   {label:<14} mean {m:+8.2f} cc => "
                  f"{a.elasticity * m / 100:+7.2%} expected fills")

    print()
    print("6. HEALTH")
    print("   reason codes : " + ", ".join(
        f"{k}={v}" for k, v in Counter(
            str(r.get("conc_reason")) for r in rows).most_common()))
    print("   binding bound: " + ", ".join(
        f"{k}={v}" for k, v in Counter(
            str(r.get("conc_scale_binding")) for r in rows).most_common()))
    halves = [float(r["conc_half_cc"]) for r in rows
              if r.get("conc_half_cc") is not None]
    if halves:
        print(f"   half-range   : med {statistics.median(halves):.0f} cc  "
              f"[{min(halves):.0f} .. {max(halves):.0f}]")
    zero = sum(1 for w, s in zip(would, shipped, strict=True) if w == s)
    print(f"   arming would change NOTHING on {zero / len(rows):.1%} of quotes")
    cov = [r for r in rows if r.get("conc_value_cc_per_contract") is not None]
    print(f"   Cov(candidate, book) available on {len(cov) / len(rows):.1%} "
          f"of quotes (absent => the zero-SE Herfindahl reading alone)")
    print("=" * 78)


if __name__ == "__main__":
    main()
