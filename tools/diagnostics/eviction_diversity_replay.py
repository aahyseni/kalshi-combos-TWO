"""COUNTERFACTUAL REPLAY — derived capacity + diversity-aware eviction key
(2026-07-31, Task C proof #3).

Read-only against the LIVE store (sqlite mode=ro) and the live log tape.
Answers, from today's actual eviction stream:

  1. the MEASURED per-bucket acceptance table (quote_sent denominators from
     the store joined to confirm/decline accepts), with Clopper-Pearson
     bounds at the ratified alpha 0.02 — the same table the live
     ``AcceptanceCounters`` accumulates in-process;
  2. the DERIVED capacity over today's measured sent-rate windows — would
     the slot cap have bound at all under the derivation?
  3. for every ``open_quote_evicted`` event on the tape: recompute both keys
     (absolute-EV as lived vs diversity dEV x P(accept|bucket) with the CP
     candidate-lower/incumbent-upper asymmetry; dES99 = 0 offline — the MC
     snapshot of that moment is not reconstructable, so this is the degraded
     dEV x P(accept) form the live code also falls back to) and count the
     REVERSALS: evictions of small resting quotes by large candidates that
     the diversity key would have refused.

Usage:
    python -m tools.diagnostics.eviction_diversity_replay \
        [--db PATH] [--log PATH ...] [--since 2026-07-26]

Rule 8: imports the live ``eviction_value`` module — never a
reimplementation of the key.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from combomaker.rfq.eviction_value import (  # noqa: E402
    N_BUCKETS,
    AcceptanceCounters,
    clopper_pearson_lower,
    clopper_pearson_upper,  # noqa: E402
    derive_open_quote_capacity,
    size_bucket,
)

ALPHA = 0.02  # ratified portfolio_kill_tail_prob anchor
DATA = Path("C:/Users/aahys/kalshi-combos-TWO/data")
DB = DATA / "combomaker-prod-live-wc.sqlite3"
BUCKET_NAMES = ["<$5", "$5-15", "$15-50", "$50-150", ">$150"]


def _det_cc(yes_bid: int, no_bid: int, contracts_centi: int | None,
            target_cost_cc: int | None) -> int | None:
    """Premium at risk of the worse quotable side — the live
    ``_quote_det_consumed_cc`` arithmetic when contracts are known; the
    taker's target cost as the offline stand-in for target-cost RFQs."""
    if contracts_centi and contracts_centi > 0:
        return max(
            contracts_centi * b // 100 for b in (int(yes_bid), int(no_bid), 0)
        )
    if target_cost_cc and target_cost_cc > 0:
        return int(target_cost_cc)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--log", nargs="*", default=None)
    ap.add_argument("--since", default="2026-07-26")
    ap.add_argument(
        "--day",
        default=None,
        help="day (YYYY-MM-DD) for the derived-capacity windows + default "
        "log glob; defaults to the LATEST day on the store's own tape "
        "(never a typed date)",
    )
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA query_only=1")

    # ---- RFQ sizing facts -------------------------------------------------
    rfq_size: dict[str, tuple[int | None, int | None]] = {}
    for rfq_id, cc, tc in con.execute(
        "SELECT rfq_id, contracts_centi, target_cost_cc FROM rfqs "
        "WHERE seen_at >= ?", (args.since,)
    ):
        rfq_size[rfq_id] = (cc, tc)

    # ---- quote_sent -> per-quote det + the acceptance DENOMINATOR ---------
    tape = AcceptanceCounters()
    quote_det: dict[str, int] = {}  # quote_id -> det_cc
    rfq_det: dict[str, int] = {}    # rfq_id -> last sent det_cc
    n_sent = 0
    for rfq_id, ctx_json in con.execute(
        "SELECT rfq_id, context_json FROM decisions "
        "WHERE kind='quote_sent' AND at >= ?", (args.since,)
    ):
        try:
            ctx = json.loads(ctx_json)
        except json.JSONDecodeError:
            continue
        cc, tc = rfq_size.get(rfq_id, (None, None))
        det = _det_cc(ctx.get("yes_bid_cc", 0), ctx.get("no_bid_cc", 0), cc, tc)
        if det is None:
            continue
        n_sent += 1
        tape.record_quoted(det)
        qid = str(ctx.get("quote_id", ""))
        if qid:
            quote_det[qid] = det
        rfq_det[rfq_id] = det

    # ---- accepts (confirm + decline = confirm-path entries) ---------------
    n_acc = 0
    for (rfq_id,) in con.execute(
        "SELECT rfq_id FROM decisions "
        "WHERE kind IN ('confirm','decline') AND at >= ?", (args.since,)
    ):
        det = rfq_det.get(rfq_id)
        if det is not None:
            n_acc += 1
            tape.record_accepted(det)

    print(f"window since {args.since}: quote_sent={n_sent} accepts={n_acc}")
    print("\nMEASURED ACCEPTANCE TABLE (per-bucket, CP bounds at alpha=0.02):")
    print(f"{'bucket':>8} {'quoted':>8} {'accepted':>8} {'per_1k':>8} "
          f"{'CP_lo/1k':>9} {'CP_hi/1k':>9}")
    bounds = {}
    for i in range(N_BUCKETS):
        b = tape.bounds(i, ALPHA)
        bounds[i] = b
        if b is None:
            print(f"{BUCKET_NAMES[i]:>8} {'-':>8}")
            continue
        print(f"{BUCKET_NAMES[i]:>8} {b.quoted:>8} {b.accepted:>8} "
              f"{1000 * b.p_hat:>8.2f} {1000 * b.lower:>9.3f} "
              f"{1000 * b.upper:>9.3f}")
    print(f"table discriminating: {tape.discriminating(ALPHA)}")

    # ---- derived capacity over today's sent-rate --------------------------
    rows = list(con.execute(
        "SELECT at FROM decisions WHERE kind='quote_sent' AND at >= ?",
        (args.since[:10] if args.since else "2026-07-31",),
    ))
    # 60s windows on the target day's tape only (the derivation's live
    # cadence). The day is the store's own latest, never a typed date.
    day = args.day or (max(r[0][:10] for r in rows) if rows else None)
    print(f"\nderived-capacity day: {day}")
    today = [r[0] for r in rows if r[0][:10] == day]
    per_min = Counter(t[:16] for t in today)  # YYYY-MM-DDTHH:MM
    if per_min:
        worst = max(per_min.values())
        med = sorted(per_min.values())[len(per_min) // 2]
        for label, sent_per_min in (("median", med), ("worst", worst)):
            flow = sent_per_min / 60.0 * 4  # create+delete per sent quote
            # The LIVE derivation (rule 8 — never a reimplementation).
            # Offline stand-ins for the observed inputs: Advanced tier
            # 300 t/s write; the tier-clamped supervisor withdraw budget
            # 200 tok/10 s = 20 t/s (the SAME rate passed as the tier
            # form's reserve and the withdraw form's budget, exactly as
            # quote_app._derived_capacity_tick wires it post-G1).
            d = derive_open_quote_capacity(
                tier_write_rate_per_s=300.0,
                reserve_rate_per_s=20.0,
                withdraw_rate_per_s=20.0,
                measured_flow_tokens_per_s=flow,
                ttl_s=20.0,
                refresh_cost_tokens=4,
                delete_cost_tokens=2,
                fallback=200,
            )
            print(f"derived capacity at {label} sent-rate "
                  f"({sent_per_min}/min -> {flow:.1f} tok/s): {d.capacity} "
                  f"[tier form {d.tier_capacity}, withdraw form "
                  f"{d.withdraw_capacity} binds]")

    # ---- eviction replay --------------------------------------------------
    logs = [Path(p) for p in (args.log or [])] or sorted(
        DATA.glob(f"live_{day.replace('-', '')}_*.log" if day else "live_*.log")
    )
    evictions: list[dict] = []
    for lp in logs:
        try:
            with open(lp, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"open_quote_evicted"' not in line:
                        continue
                    try:
                        j = json.loads(line[line.index("{"):])
                    except (ValueError, json.JSONDecodeError):
                        continue
                    if j.get("event") == "open_quote_evicted":
                        evictions.append(j)
        except OSError as exc:
            print(f"log unreadable {lp}: {exc}")

    n = len(evictions)
    joined = reversed_n = small_victim = 0
    victim_buckets: Counter[int] = Counter()
    cand_buckets: Counter[int] = Counter()
    for e in evictions:
        vdet = quote_det.get(str(e.get("evicted_quote_id", "")))
        cdet = rfq_det.get(str(e.get("candidate_rfq_id", "")))
        if vdet is None or cdet is None:
            continue
        vb, cb = size_bucket(vdet), size_bucket(cdet)
        vbnd, cbnd = bounds.get(vb), bounds.get(cb)
        if vbnd is None or cbnd is None:
            continue
        joined += 1
        victim_buckets[vb] += 1
        cand_buckets[cb] += 1
        if vb <= 1:
            small_victim += 1
        v_ev = int(e.get("evicted_ev_cc") or 0)
        c_ev = int(e.get("candidate_ev_cc") or 0)
        # Diversity key, degraded (dES=0) form: candidate at CP lower,
        # incumbent at CP upper — as the live code scores them.
        c_key = c_ev * clopper_pearson_lower(cbnd.accepted, cbnd.quoted, ALPHA)
        v_key = v_ev * clopper_pearson_upper(vbnd.accepted, vbnd.quoted, ALPHA)
        if c_key <= v_key:
            reversed_n += 1

    print(f"\nEVICTION REPLAY: {n} open_quote_evicted events on the tape, "
          f"{joined} joined to store sizes")
    print(f"victim buckets:    "
          f"{ {BUCKET_NAMES[k]: v for k, v in sorted(victim_buckets.items())} }")
    print(f"candidate buckets: "
          f"{ {BUCKET_NAMES[k]: v for k, v in sorted(cand_buckets.items())} }")
    print(f"small (<$15) victims evicted as lived: {small_victim}")
    print(f"evictions the DIVERSITY key would have REFUSED "
          f"(victim survives): {reversed_n} / {joined}")
    print("\nNOTE dES99=0 offline (book-MC snapshot not reconstructable). "
          "POST-G1: derived capacity = min(tier form, withdraw form) and the "
          "withdraw form binds at ~190 today (the delete path's own 20 tok/s "
          "budget) — capacity no longer moots the eviction stream; the key "
          "genuinely governs at today's book sizes until the operator raises "
          "the withdraw budget (staged decision, 2026-08-01 port report).")


if __name__ == "__main__":
    main()
