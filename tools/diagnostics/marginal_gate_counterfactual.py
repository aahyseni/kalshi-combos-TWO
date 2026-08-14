"""Quote-production counterfactual for the marginal KILL/ruin gates.

THE NEW MANDATORY PRE-ARM GATE (2026-08-14, second VALIDATE-CAPS-CAN-QUOTE
incident): before arming (or re-arming) a criterion change on the marginal
gates, replay the store's recorded marginal-form refusals through the LIVE
admission function and prove the armed configuration would actually produce
quotes — safety-side replays (whale refusal) are necessary but NEVER
sufficient. Read-only: opens the store ``mode=ro``, imports the live
criterion (hard rule 8 — never a reimplementation), writes nothing.

What it measures, from ``decisions.context_json`` detail strings recorded by
the OLD criterion (format: ``candidate marginal tail <des99>cc > EV credit
<credit>cc (ev <ev>cc x p_accept_lower <p>)``):

  * how many of the window's marginal-form refusals would ADMIT under the
    corrected ``marginal_tail_admit`` (des99 x ES_TAIL_ALPHA <= ev);
  * the admit rate under the OLD formula on the same rows (sanity: ~0);
  * the des99/ev distribution so a criterion change can be judged against
    the real trade shapes of the day, not theory.

Usage (quiet window, read-only):
    .venv/Scripts/python.exe -m tools.diagnostics.marginal_gate_counterfactual \
        --since-utc 2026-08-14T13:33:00 --until-utc 2026-08-14T21:40:00
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import dataclass

from combomaker.rfq.eviction_value import ES_TAIL_ALPHA, marginal_tail_admit

DB_DEFAULT = "data/combomaker-prod-live-wc.sqlite3"
# OLD-format detail recorded by the pre-fix criterion (what the store holds
# for the incident day); the NEW format ("x alpha") is parsed too so the
# tool keeps working after the fix ships.
OLD_RE = re.compile(
    r"candidate marginal tail (?P<des99>\d+)cc > EV credit "
    r"(?P<credit>\d+)cc \(ev (?P<ev>-?\d+)cc x p_accept_lower "
    r"(?P<p>[0-9.]+)\)"
)
NEW_RE = re.compile(
    r"candidate marginal tail (?P<des99>\d+)cc x alpha [0-9.]+ > "
    r"ev (?P<ev>-?\d+)cc"
)


@dataclass
class Parsed:
    des99_cc: float
    ev_cc: int
    p_accept_lower: float | None


def _connect(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)


def _bisect_first_id(cur: sqlite3.Cursor, since_utc: str) -> int | None:
    row = cur.execute("SELECT MIN(id), MAX(id) FROM decisions").fetchone()
    if not row or row[0] is None:
        return None
    lo, hi = int(row[0]), int(row[1])
    ans: int | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        r = cur.execute(
            "SELECT at FROM decisions WHERE id >= ? ORDER BY id LIMIT 1",
            (mid,),
        ).fetchone()
        if r is None:
            hi = mid - 1
            continue
        if str(r[0]) >= since_utc:
            ans = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return ans


def parse_detail(context_json: str) -> Parsed | None:
    m = OLD_RE.search(context_json)
    if m:
        return Parsed(
            des99_cc=float(m.group("des99")),
            ev_cc=int(m.group("ev")),
            p_accept_lower=float(m.group("p")),
        )
    m = NEW_RE.search(context_json)
    if m:
        return Parsed(
            des99_cc=float(m.group("des99")),
            ev_cc=int(m.group("ev")),
            p_accept_lower=None,
        )
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--since-utc", required=True)
    ap.add_argument("--until-utc", required=True)
    ap.add_argument("--chunk", type=int, default=25_000)
    ap.add_argument(
        "--max-rows", type=int, default=3_000_000,
        help="scan budget (id-ordered rows read before stopping)",
    )
    args = ap.parse_args()

    con = _connect(args.db)
    cur = con.cursor()
    first_id = _bisect_first_id(cur, args.since_utc)
    if first_id is None:
        print("no decisions at/after --since-utc; nothing to replay")
        return

    scanned = 0
    parsed_rows: list[Parsed] = []
    marginal_rows = 0
    next_id = first_id
    while scanned < args.max_rows:
        rows = cur.execute(
            "SELECT id, at, context_json FROM decisions "
            "WHERE id >= ? ORDER BY id LIMIT ?",
            (next_id, args.chunk),
        ).fetchall()
        if not rows:
            break
        for rid, at, ctx in rows:
            scanned += 1
            if str(at) >= args.until_utc:
                rows = []
                break
            if not ctx or "(marginal form)" not in ctx:
                continue
            marginal_rows += 1
            p = parse_detail(ctx)
            if p is not None:
                parsed_rows.append(p)
        if not rows:
            break
        next_id = int(rows[-1][0]) + 1

    if not parsed_rows:
        print(
            f"scanned {scanned:,} decisions from id {first_id:,}: "
            f"{marginal_rows:,} marginal-form refusals, 0 parseable — "
            "nothing to replay (window empty or format drift)"
        )
        return

    old_admits = sum(
        1
        for p in parsed_rows
        if p.p_accept_lower is not None
        and p.des99_cc <= float(p.ev_cc) * p.p_accept_lower
    )
    new_admits = sum(
        1 for p in parsed_rows if marginal_tail_admit(p.des99_cc, p.ev_cc)
    )
    n = len(parsed_rows)
    des = sorted(p.des99_cc for p in parsed_rows)
    evs = sorted(p.ev_cc for p in parsed_rows)

    def q(v: list[float] | list[int], f: float) -> float:
        return float(v[min(len(v) - 1, int(f * len(v)))])

    print(
        f"window {args.since_utc} -> {args.until_utc}  "
        f"(scanned {scanned:,} decisions, {marginal_rows:,} marginal-form "
        f"refusals, {n:,} parsed)"
    )
    print(
        f"  OLD criterion (des99 <= ev x p_accept_lower): "
        f"{old_admits:,}/{n:,} would admit ({100.0 * old_admits / n:.2f}%)"
    )
    print(
        f"  NEW criterion (des99 x {ES_TAIL_ALPHA} <= ev): "
        f"{new_admits:,}/{n:,} would admit ({100.0 * new_admits / n:.2f}%)"
    )
    print(
        f"  des99_cc p10/p50/p90: {q(des, 0.1):,.0f} / {q(des, 0.5):,.0f} / "
        f"{q(des, 0.9):,.0f}   ev_cc p10/p50/p90: {q(evs, 0.1):,.0f} / "
        f"{q(evs, 0.5):,.0f} / {q(evs, 0.9):,.0f}"
    )
    print(
        "  VALIDATE-CAPS-CAN-QUOTE verdict: "
        + (
            "PASS — the armed criterion admits real refused flow"
            if new_admits > 0
            else "FAIL — still a 100%-decline gate; DO NOT ARM"
        )
    )


if __name__ == "__main__":
    main()
