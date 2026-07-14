"""Live pricing viewer — tails the live quote store and prints, in real time, every
RFQ we see and exactly how WE priced it (our sell-YES offer) or why we declined.

Read-ONLY (mode=ro); safe to run in a separate terminal tab alongside the live bot.
It shows OUR bids — the source of truth for "what are we actually offering" — which
is NOT the same as Kalshi's combo-market display (that shows the whole market).

Run (from repo root, in another terminal):
    .venv/Scripts/python tools/live_viewer.py
Options:
    --db  <path>   (default: data/combomaker-prod-live-wc.sqlite3)
    --all          also print declines (default: quotes + a 1-line decline tally)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "combomaker-prod-live-wc.sqlite3",
)
GREEN, DIM, YEL, CYAN, RST = "\033[32m", "\033[2m", "\033[33m", "\033[36m", "\033[0m"


def short_leg(t: str) -> str:
    """KXWCADVANCE-26JUL14FRAESP-FRA -> FRAESP:ADV/FRA ; player/total abbreviated."""
    p = t.split("-")
    if len(p) < 3:
        return t
    series = p[0].replace("KX", "")
    game = p[1][-6:] if len(p[1]) >= 6 else p[1]
    sel = "-".join(p[2:])
    kind = ("ADV" if "ADVANCE" in series else "TOT" if "TOTAL" in series
            else "GOAL" if "GOAL" in series else "BTTS" if "BTTS" in series
            else "CORN" if "CORNER" in series else "SPRD" if "SPREAD" in series
            else series[:4])
    return f"{game}:{kind}/{sel}"[:26]


def legs_summary(legs_json: str, n: int) -> str:
    try:
        legs = json.loads(legs_json)
        tk = [l.get("market_ticker", "?") if isinstance(l, dict) else str(l) for l in legs]
    except Exception:
        return f"{n} legs"
    s = " + ".join(short_leg(t) for t in tk[:3])
    if len(tk) > 3:
        s += f" +{len(tk) - 3}more"
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    print(f"{CYAN}live pricing viewer — {args.db}{RST}")
    print(f"{DIM}watching our decisions; Ctrl-C to quit. Our sell-YES offer = 100 - no_bid.{RST}\n")
    last_id = None
    declines = Counter()
    last_tally = time.time()

    while True:
        try:
            con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=2.0)
            try:
                if last_id is None:  # start from the tail, not full history
                    row = con.execute("select max(id) from decisions").fetchone()
                    last_id = (row[0] or 0)
                q = ("select d.id, d.at, d.kind, d.reasons_json, d.context_json, "
                     "r.legs_json, r.n_legs, r.contracts_centi, r.target_cost_cc "
                     "from decisions d left join rfqs r on d.rfq_id=r.rfq_id "
                     "where d.id > ? order by d.id")
                for did, at, kind, rj, cj, lj, n, cc, tc in con.execute(q, (last_id,)):
                    last_id = did
                    tstr = str(at)[11:19]
                    if kind == "quote_sent":
                        c = json.loads(cj) if cj else {}
                        yes = (10000 - c.get("no_bid_cc", 10000)) / 100.0
                        fair = c.get("fair_cc", 0) / 100.0
                        size = (f"{cc/100:.0f}ct" if cc else
                                f"${(tc or 0)/10000:.0f}" if tc else "?")
                        print(f"{GREEN}{tstr} QUOTE  Yes {yes:4.1f}¢{RST} "
                              f"{DIM}(fair {fair:.1f}¢, {n}legs, req {size}){RST}  "
                              f"{legs_summary(lj or '[]', n or 0)}")
                    elif kind == "no_quote":
                        reasons = json.loads(rj) if rj else []
                        r0 = (reasons[0] if reasons else "?")
                        declines[r0] += 1
                        if args.all:
                            print(f"{DIM}{tstr} decline {r0:22} "
                                  f"{legs_summary(lj or '[]', n or 0)}{RST}")
            finally:
                con.close()
            # periodic decline tally so the feed shows what's being filtered
            if time.time() - last_tally > 15 and declines:
                top = ", ".join(f"{k}={v}" for k, v in declines.most_common(4))
                print(f"{YEL}  … declines(15s): {top}{RST}")
                declines.clear()
                last_tally = time.time()
        except Exception as exc:  # noqa: BLE001 — viewer must never die
            print(f"{DIM}(viewer skip: {type(exc).__name__}){RST}")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
