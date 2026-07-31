"""TASK C, QUESTION 2 — is FILL-RATE ELASTICITY size-dependent?

Do big tickets need MORE or LESS markup to win? A size-scaled markup only pays
if big tickets are either (a) more toxic (question 1 — measured, answer NO) or
(b) less price-sensitive, so the extra cents are collected rather than driving
the flow away.

DESIGN — CASE/CONTROL, because acceptance is RARE.
  1,464,758 RFQs were quoted; 1,427 reached last look. That is a 0.097%
  acceptance rate, so a plain random sample would contain ~14 accepted RFQs and
  measure nothing. Instead: take EVERY accepted RFQ as a CASE and a systematic
  1-in-N sample of quoted RFQs as CONTROLS, then compare their joint
  (size, margin) distributions. Each RFQ has EXACTLY ONE quote_sent row
  (1,464,833 rows / 1,464,758 distinct), so sampling rows samples RFQs.

MARGIN AXIS. The taker sees our NO bid against the NO fair, so the charge is
    gap_cc = (100c - fair_cc) - no_bid_cc
which is exactly margin + fee_no - inventory_skew + snap loss (the identity
tools/diagnostics/margin_attribution.py decomposes). Nothing is reimplemented:
both numbers are read verbatim off the logged decision context.

READ-ONLY. mode=ro, no writes, no exchange calls.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
from collections import defaultdict

DB_DEFAULT = (
    "file:C:/Users/aahys/kalshi-combos-TWO/data/combomaker-prod-live-wc.sqlite3?mode=ro"
)
CC_PER_DOLLAR = 10_000


def cluster_bootstrap(items, reps=3000, seed=20260731):
    if not items:
        return float("nan"), float("nan"), float("nan"), 0
    groups = defaultdict(list)
    for c, v in items:
        groups[c].append(v)
    keys = list(groups)
    point = statistics.mean(v for _, v in items)
    if len(keys) < 2:
        return point, float("nan"), float("nan"), len(keys)
    rng = random.Random(seed)
    draws = []
    for _ in range(reps):
        pool = []
        for _ in range(len(keys)):
            pool.extend(groups[keys[rng.randrange(len(keys))]])
        if pool:
            draws.append(statistics.mean(pool))
    draws.sort()
    return point, draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws))], len(keys)


def pull(conn, sql, params=()):
    out = []
    for at, rfq_id, ctx in conn.execute(sql, params):
        if not rfq_id:
            continue
        try:
            d = json.loads(ctx)
        except Exception:
            continue
        fair = d.get("fair_cc")
        nob = d.get("no_bid_cc")
        if fair is None or nob is None or nob <= 0:
            continue
        out.append((at[:10], rfq_id, fair, nob, (CC_PER_DOLLAR - fair) - nob))
    return out


def sizes_for(conn, rfq_ids):
    got = {}
    for rid in rfq_ids:
        r = conn.execute(
            "SELECT target_cost_cc, contracts_centi, n_legs FROM rfqs WHERE rfq_id=? LIMIT 1",
            (rid,),
        ).fetchone()
        if r and r[0] is not None:
            got[rid] = (r[0] / CC_PER_DOLLAR, r[2])
    return got


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--stride", type=int, default=80, help="1-in-N control sample")
    ap.add_argument("--reps", type=int, default=3000)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, uri=True)

    accepted_ids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT rfq_id FROM decisions WHERE kind IN ('confirm','decline') "
            "AND rfq_id IS NOT NULL"
        )
    }
    print(f"accepted (reached last look) RFQs: {len(accepted_ids)}")

    cases = pull(
        conn,
        "SELECT at, rfq_id, context_json FROM decisions WHERE kind='quote_sent' "
        f"AND rfq_id IN ({','.join('?'*len(accepted_ids))})",
        tuple(accepted_ids),
    )
    print(f"cases with a logged quote_sent context: {len(cases)}")

    controls = [
        r
        for r in pull(
            conn,
            "SELECT at, rfq_id, context_json FROM decisions WHERE kind='quote_sent' "
            "AND id % ? = 0",
            (args.stride,),
        )
        if r[1] not in accepted_ids
    ]
    print(f"controls (1-in-{args.stride} systematic sample): {len(controls)}")

    sz = sizes_for(conn, [r[1] for r in cases] + [r[1] for r in controls])
    print(f"size (target_cost) resolved for {len(sz)} of "
          f"{len(cases)+len(controls)} RFQs")

    def enrich(rows):
        return [
            (day, rid, fair, nob, gap, sz[rid][0], sz[rid][1])
            for day, rid, fair, nob, gap in rows
            if rid in sz
        ]

    C, K = enrich(cases), enrich(controls)
    print(f"\ncases with size: {len(C)}   controls with size: {len(K)}")
    if not C or not K:
        return

    # ------------------------------------------------------------------ #
    # A. SIZE DISTRIBUTION: accepted vs quoted.  If big tickets were harder
    #    to win, cases would skew SMALL relative to controls.
    # ------------------------------------------------------------------ #
    # FIXED dollar edges, not quantiles: target_cost is heavily QUANTIZED
    # ($10 is 62% of all RFQs), so quantile edges collapse onto each other and
    # erase the top of the range. These bins are the observed lumps.
    edges = [5.0, 10.0, 10.01, 50.0, 250.0]
    print(f"\n{'='*78}\nA. ACCEPTANCE RATE BY REQUESTED SIZE "
          f"(control quartile edges USD: {[round(e,2) for e in edges]})")
    print(f"{'size bucket':<24}{'cases':>8}{'controls':>10}{'quoted_est':>12}"
          f"{'accept%':>10}{'rel.rate':>10}")

    def b_of(x):
        return sum(1 for e in edges if x >= e)

    tot_rate = len(C) / (len(K) * args.stride)
    for b in range(len(edges)+1):
        nc = sum(1 for r in C if b_of(r[5]) == b)
        nk = sum(1 for r in K if b_of(r[5]) == b)
        est = nk * args.stride
        rate = nc / est if est else float("nan")
        lo_e = 0.0 if b == 0 else edges[b - 1]
        hi_e = edges[b] if b < len(edges) else float("inf")
        lbl = f"[{lo_e:>6.2f},{hi_e:>7.2f})" if hi_e != float("inf") else f"[{lo_e:>6.2f},    inf)"
        print(f"{lbl:<24}{nc:>8}{nk:>10}{est:>12}{100*rate:>10.4f}"
              f"{rate/tot_rate:>10.2f}x")

    # ------------------------------------------------------------------ #
    # B. ELASTICITY: within each size bucket, how much CHEAPER (in gap cc)
    #    is a WON auction than a typical quoted one? A bigger case/control
    #    gap deficit = MORE price-sensitive flow at that size.
    # ------------------------------------------------------------------ #
    print(f"\n{'='*78}\nB. PRICE SENSITIVITY BY SIZE — gap = (100c - fair) - no_bid, in CENTS")
    print(f"{'size bucket':<24}{'set':<10}{'n':>7}{'clus':>6}{'mean gap c':>12}"
          f"{'lo95':>9}{'hi95':>9}")
    deficits = {}
    for b in range(len(edges)+1):
        cs = [(r[0], r[4] / 100.0) for r in C if b_of(r[5]) == b]
        ks = [(r[0], r[4] / 100.0) for r in K if b_of(r[5]) == b]
        lo_e = 0.0 if b == 0 else edges[b - 1]
        hi_e = edges[b] if b < len(edges) else float("inf")
        lbl = f"[{lo_e:>6.2f},{hi_e:>7.2f})" if hi_e != float("inf") else f"[{lo_e:>6.2f},    inf)"
        for name, items in (("WON", cs), ("quoted", ks)):
            pt, blo, bhi, ncl = cluster_bootstrap(items, reps=args.reps)
            print(f"{lbl if name=='WON' else '':<24}{name:<10}{len(items):>7}{ncl:>6}"
                  f"{pt:>12.3f}{blo:>9.3f}{bhi:>9.3f}")
        if cs and ks:
            deficits[b] = statistics.mean(v for _, v in cs) - statistics.mean(v for _, v in ks)
            print(f"{'':<24}{'DEFICIT':<10}{'':>7}{'':>6}{deficits[b]:>12.3f}")
    print("\n  deficit trend small->large: "
          + " -> ".join(f"{deficits[b]:+.2f}c" for b in sorted(deficits)))

    # Is the deficit trend REAL?  Bootstrap the difference (largest quoted
    # bucket) - (smallest) over DAY clusters.  A size-scaled markup needs this
    # to be POSITIVE and significant: big flow LESS price-sensitive.
    lo_b, hi_b = min(deficits), max(b for b in deficits if b < len(edges))
    pooled = defaultdict(lambda: {"cW": [], "cQ": [], "hW": [], "hQ": []})
    for r in C:
        b = b_of(r[5])
        if b == lo_b:
            pooled[r[0]]["cW"].append(r[4] / 100.0)
        elif b == hi_b:
            pooled[r[0]]["hW"].append(r[4] / 100.0)
    for r in K:
        b = b_of(r[5])
        if b == lo_b:
            pooled[r[0]]["cQ"].append(r[4] / 100.0)
        elif b == hi_b:
            pooled[r[0]]["hQ"].append(r[4] / 100.0)
    keys = [k for k in pooled if all(pooled[k][x] for x in ("cW", "cQ", "hW", "hQ"))]
    rng = random.Random(31)

    def dd(sample, pooled=pooled):
        acc = {x: [] for x in ("cW", "cQ", "hW", "hQ")}
        for k in sample:
            for x in acc:
                acc[x].extend(pooled[k][x])
        if not all(acc.values()):
            return None
        lo_def = statistics.mean(acc["cW"]) - statistics.mean(acc["cQ"])
        hi_def = statistics.mean(acc["hW"]) - statistics.mean(acc["hQ"])
        return hi_def - lo_def

    pt = dd(keys)
    draws = sorted(x for x in (dd([keys[rng.randrange(len(keys))] for _ in keys])
                               for _ in range(args.reps)) if x is not None)
    if draws and pt is not None:
        blo, bhi = draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws))]
        sig = "SIGNIFICANT" if (blo > 0) == (bhi > 0) else "not significant"
        print(f"  DEFICIT(bucket {hi_b}) - DEFICIT(bucket {lo_b}) = {pt:+.3f}c "
              f"[{blo:+.3f}, {bhi:+.3f}] over {len(keys)} day-clusters -> {sig}")
        print("  (positive = big flow is LESS price-sensitive = the only "
              "elasticity case for a size markup)")

    # ------------------------------------------------------------------ #
    # C. Is the size axis just the price axis again?  Report the NO-bid
    #    level per size bucket for the CONTROLS (what we quote, not what we
    #    win) — the confound that killed question 1's raw gradient.
    # ------------------------------------------------------------------ #
    print(f"\n{'='*78}\nC. SIZE vs NO-BID LEVEL on the QUOTED population (the confound)")
    print(f"{'size bucket':<24}{'n':>8}{'mean no_bid c':>15}{'mean fair c':>13}"
          f"{'mean legs':>11}")
    for b in range(len(edges)+1):
        ks = [r for r in K if b_of(r[5]) == b]
        if not ks:
            continue
        lo_e = 0.0 if b == 0 else edges[b - 1]
        hi_e = edges[b] if b < len(edges) else float("inf")
        lbl = f"[{lo_e:>6.2f},{hi_e:>7.2f})" if hi_e != float("inf") else f"[{lo_e:>6.2f},    inf)"
        legs = [r[6] for r in ks if r[6]]
        print(f"{lbl:<24}{len(ks):>8}{statistics.mean(r[3] for r in ks)/100:>15.2f}"
              f"{statistics.mean(r[2] for r in ks)/100:>13.2f}"
              f"{(statistics.mean(legs) if legs else float('nan')):>11.2f}")


if __name__ == "__main__":
    main()
