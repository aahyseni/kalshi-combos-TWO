"""FIX 4 REPLAY — value-ranked vs arrival-order allocation of the SAME det-max
budget, measured on the LIVE tape (2026-07-28).

Rule 8 (testing isolation): read-only. The store is opened READ-ONLY and the
live log is streamed; nothing is imported that mutates state.

METHOD. For every quote the bot actually ISSUED today we recover the two numbers
the allocation turns on:

  * EV        — ``candidate_ev_cc`` from the ``risk_audit`` line the quote path
                emits for that RFQ (the SAME ``_quote_candidate_ev_cc`` value
                the eviction ranks on).
  * det spend — ``contracts_centi x our bid // 100`` from the RFQ tape's size
                and the ``quote_sent`` decision's bids: the EXACT arithmetic
                ``QuoteLifecycle._quote_det_consumed_cc`` uses.

Then it answers: with the SAME total det spend, what does keeping the DENSEST
quotes bank versus taking them in arrival order? det-max is correlation-
INDEPENDENT, so this reordering is a deterministic knapsack — MODEL-FREE.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOG = REPO / "data" / "live_20260728_0445.log"
DB = REPO / "data" / "combomaker-prod-live-wc.sqlite3"


def ev_by_rfq() -> dict[str, tuple[int, str]]:
    """rfq_id -> (candidate_ev_cc, reason) from the risk_audit stream."""
    out: dict[str, tuple[int, str]] = {}
    with LOG.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"event": "risk_audit"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            rfq = d.get("rfq_id")
            ev = d.get("candidate_ev_cc")
            if rfq and isinstance(ev, int):
                out[rfq] = (ev, str(d.get("reason", "")))
    return out


def sent_quotes() -> dict[str, tuple[int, int]]:
    """rfq_id -> (contracts_centi, worse-side bid_cc) for quotes we ISSUED."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    sizes = {
        r[0]: int(r[1] or 0)
        for r in con.execute(
            "select rfq_id, contracts_centi from rfqs where contracts_centi is not null"
        )
    }
    out: dict[str, tuple[int, int]] = {}
    for rfq, ctx in con.execute(
        "select rfq_id, context_json from decisions where kind='quote_sent'"
    ):
        if rfq not in sizes or not ctx:
            continue
        try:
            d = json.loads(ctx)
        except Exception:
            continue
        bid = max(int(d.get("yes_bid_cc") or 0), int(d.get("no_bid_cc") or 0))
        if bid > 0 and sizes[rfq] > 0:
            out[rfq] = (sizes[rfq], bid)
    con.close()
    return out


def main() -> None:
    evs = ev_by_rfq()
    sent = sent_quotes()
    rows: list[tuple[float, int, int]] = []  # (density, ev_cc, det_cc)
    for rfq, (contracts, bid) in sent.items():
        hit = evs.get(rfq)
        if hit is None:
            continue
        ev, _reason = hit
        det = contracts * bid // 100
        if det <= 0 or ev <= 0:
            continue
        rows.append((ev / det, ev, det))
    if not rows:
        print("no joinable (EV, det) pairs on this tape")
        return
    total_ev = sum(r[1] for r in rows)
    total_det = sum(r[2] for r in rows)
    print("=" * 78)
    print("FIX 4 REPLAY — value-ranked vs arrival-order allocation")
    print("=" * 78)
    print(f"joinable issued quotes         {len(rows)}")
    print(f"total EV                       ${total_ev / 10_000:,.2f}")
    print(f"total det spend                ${total_det / 10_000:,.2f}")
    print(f"BOOK-WIDE density (EV/det)     {total_ev / total_det:.4f}")
    print()
    dense = sorted(rows, key=lambda r: -r[0])
    print(f"{'keep top':>9}{'quotes':>8}{'det spend':>14}{'EV banked':>13}"
          f"{'density':>10}{'lift':>8}")
    base = total_ev / total_det
    for frac in (0.10, 0.25, 0.42, 0.50, 0.75, 1.00):
        k = max(1, int(round(len(dense) * frac)))
        kept = dense[:k]
        ev_k = sum(r[1] for r in kept)
        det_k = sum(r[2] for r in kept)
        print(f"{frac:>8.0%}{k:>8}${det_k / 10_000:>12,.2f}"
              f"${ev_k / 10_000:>11,.2f}{ev_k / det_k:>10.4f}"
              f"{ev_k / det_k / base:>7.2f}x")
    print()
    # Equal-BUDGET comparison: spend the SAME det as the arrival-order half.
    half_det = total_det // 2
    arrival = rows  # store order == arrival order
    def spend(seq: list[tuple[float, int, int]]) -> tuple[int, int, int]:
        ev = det = n = 0
        for _d, e, dt in seq:
            if det + dt > half_det:
                continue
            ev, det, n = ev + e, det + dt, n + 1
        return ev, det, n
    a_ev, a_det, a_n = spend(arrival)
    d_ev, d_det, d_n = spend(dense)
    print(f"SAME BUDGET ${half_det / 10_000:,.2f} of det-max:")
    print(f"  arrival order   {a_n:>4} quotes  ${a_det / 10_000:>10,.2f} det  "
          f"${a_ev / 10_000:>9,.2f} EV  density {a_ev / max(1, a_det):.4f}")
    print(f"  value-ranked    {d_n:>4} quotes  ${d_det / 10_000:>10,.2f} det  "
          f"${d_ev / 10_000:>9,.2f} EV  density {d_ev / max(1, d_det):.4f}")
    if a_ev:
        print(f"  LIFT            {d_ev / a_ev:.2f}x EV on the same budget")


if __name__ == "__main__":
    main()
