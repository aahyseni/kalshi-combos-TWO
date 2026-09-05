"""Retained-edge floor = the SHRUNK POINT shortfall — PROTOTYPE and PARITY
CHECK (hard rule 8; 2026-09-04 build "floor-point-estimate").

WHY. The 2026-09-04 build A floor published, per combo cell,
    floor_upper = max(0, t_{G-1}(Φ(-3)) · SE(cell) − shortfall(cell))
— a 3σ UPPER bound on the adverse selection. On the live settled grade
(3,259 tickers, 646 cells, 445 thin) that produced pool floors of 5.9c /
11.6c / 26.7c / 49.5c and populated floors of 15-59c against tier margins
of 1-3c: the rebate cap ``margin − fee − floor`` was <= 0 on essentially
every quote and the constitutional diversity steer (the skew REBATE for
offsetting / diversifying flow — feedback_pbook_diversity_via_pricing) was
muted on the whole wire from the 22:45:44Z relight. The z ladder anchors
TAIL risk (KILL / ruin / caps); a pricing floor is a point estimate of a
cost, and its uncertainty belongs to the quote WIDTH, not to the retained
margin. This prototype re-derives, in plain arithmetic and with no import
from the module under test, the repaired rule:

    floor_point(cell) = max(0, −post_mean(cell))              (populated)
    floor_point(cell) = max(0, −mean(sport pool))             (thin: SE² > τ²)
    unknown sport    → the LARGEST pool point (fail-closed direction)

with post_mean the empirical-Bayes shrunk point: w = τ²/(τ² + SE²) =
n/(n + n0), n0 = σ²/τ² (the same weight build A derived), the per-cell
mean contract-weighted and its SE game-clustered — exactly as before,
minus the z·SE term. A NEGATIVE cell (the record says we lose, e.g.
mlb|rfi|rfi|all_no|cross at −20c/ct) keeps its whole measured shortfall
as the floor: no rebate where the record says we lose. The fee is added
in construct_quote (m_min), so floor_cc(cell) = fee + max(0, −shortfall).

It ALSO reproduces the build-A UPPER rule (today's live table) so the
counterfactual can report bids under {fee-only, upper = live, point}.

PARITY (asserted here and per replayed quote in
tools/diagnostics/fee_floor_counterfactual.py --with-cell-floor):
  1. proto point table == risk/retained_edge_floor.estimate_retained_floor
     on every cell, and the per-sport pool points agree;
  2. proto upper table reproduces the live log line the wire prices on
     (2026-09-04T23:30:04Z: pool floors mlb 570 / soccer 1382 / esports
     1955 / other 4945; 656 cells, 450 thin; floor median 570, max
     366,559) when the settled grade is unchanged since;
  3. properties: 0 <= point <= upper on every cell; a cell with a
     non-negative shrunk shortfall floors at 0 (= the fee alone).

Usage:
    PYTHONPATH=src .venv/Scripts/python.exe -m tools.proto_floor_point \
        --db "file:D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3?mode=ro"
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from statistics import NormalDist
from typing import Any

from scipy.stats import t as student_t

from combomaker.pricing.retained_cell import CellKey, floor_for_cell
from combomaker.risk.retained_edge_floor import GradeRow, estimate_retained_floor

# The live log line the wire prices on (D:/kalshi-combos-TWO-data/
# live_20260904_1929.log, retained_floor_estimate 2026-09-04T23:30:04.934569Z,
# the 19:29 ET relight on main; re-published identically at 00:00:12Z). The
# 22:45:44Z line of the 18:45 ET boot (3,259 rows, 646 cells, 445 thin, pools
# mlb 590 / soccer 1157 / esports 2666 / other 4945) was the grade before the
# boot's stale-row closes landed 76 more settled tickers. The upper prototype
# must reproduce the live line exactly while the grade is unchanged.
LIVE_POOL_UPPER = {"esports": 1955, "mlb": 570, "other": 4945, "soccer": 1382}
LIVE_ROWS = 3335
LIVE_CELLS = 656
LIVE_THIN = 450
LIVE_FLOOR_MEDIAN = 570
LIVE_FLOOR_MAX = 366_559
Z_TAIL = 3.0  # build A's anchor, kept ONLY to reproduce the live (upper) table


# ------------------------------------------------------------- plain stats


def wmean(rows: list[GradeRow]) -> float:
    w = sum(r.contracts_centi for r in rows)
    return math.fsum(r.contracts_centi * r.shortfall_per_contract_cc for r in rows) / w


def clustered_se(rows: list[GradeRow], mean: float) -> tuple[int, float | None]:
    """(clusters, SE): cluster sandwich over the game sets, G/(G−1) factor."""
    w = sum(r.contracts_centi for r in rows)
    by: dict[frozenset[str], float] = defaultdict(float)
    for r in rows:
        by[r.games] += r.contracts_centi * (r.shortfall_per_contract_cc - mean)
    g = len(by)
    if g < 2:
        return g, None
    return g, math.sqrt(math.fsum(v * v for v in by.values()) / (w * w) * g / (g - 1))


def t_quantile(n_clusters: int) -> float | None:
    if n_clusters < 2:
        return None
    return float(student_t.ppf(NormalDist().cdf(Z_TAIL), n_clusters - 1))


def ceil_floor(x: float) -> int:
    return max(0, math.ceil(x))


def pool_mean_cc(rows: list[GradeRow]) -> dict[str, float]:
    by_sport: dict[str, list[GradeRow]] = defaultdict(list)
    for r in rows:
        if r.contracts_centi > 0:
            by_sport[r.cell[0]].append(r)
    return {s: round(wmean(rs), 1) for s, rs in by_sport.items()}


# ------------------------------------------------------------- the tables


def proto_tables(
    rows: list[GradeRow],
) -> tuple[
    dict[CellKey, int], dict[str, int], dict[CellKey, int], dict[str, int],
    dict[CellKey, dict[str, Any]],
]:
    """(point table, pool point, upper table, pool upper, per-cell detail)."""
    by_cell: dict[CellKey, list[GradeRow]] = defaultdict(list)
    by_sport: dict[str, list[GradeRow]] = defaultdict(list)
    for r in rows:
        if r.contracts_centi <= 0:
            continue
        by_cell[r.cell].append(r)
        by_sport[r.cell[0]].append(r)
    pool_mean = {s: wmean(rs) for s, rs in by_sport.items()}
    pool_g_se = {s: clustered_se(rs, pool_mean[s]) for s, rs in by_sport.items()}
    cell_mean = {c: wmean(rs) for c, rs in by_cell.items()}
    cell_g_se = {c: clustered_se(rs, cell_mean[c]) for c, rs in by_cell.items()}
    # τ² per sport: method of moments over the cells whose SE is defined.
    tau2: dict[str, float] = {}
    for s in by_sport:
        defined = [c for c in by_cell if c[0] == s and cell_g_se[c][1] is not None]
        if len(defined) < 2:
            tau2[s] = 0.0
            continue
        mu = pool_mean[s]
        between = math.fsum((cell_mean[c] - mu) ** 2 for c in defined) / (len(defined) - 1)
        within = math.fsum(cell_g_se[c][1] ** 2 for c in defined) / len(defined)  # type: ignore[operator]
        tau2[s] = max(0.0, between - within)
    pool_point = {s: ceil_floor(-m) for s, m in pool_mean.items()}
    pool_upper: dict[str, int] = {}
    for s, (g, se) in pool_g_se.items():
        q = t_quantile(g)
        if se is not None and q is not None:
            pool_upper[s] = ceil_floor(q * se - pool_mean[s])
        else:
            pool_upper[s] = ceil_floor(-pool_mean[s])
    point: dict[CellKey, int] = {}
    upper: dict[CellKey, int] = {}
    detail: dict[CellKey, dict[str, Any]] = {}
    for c, rs in by_cell.items():
        s = c[0]
        g, se = cell_g_se[c]
        mu, t2 = pool_mean[s], tau2[s]
        if se is None:
            w, post_mean, post_se, thin = 0.0, mu, None, True
        else:
            se2 = se * se
            if se2 <= 0.0:
                w, post_mean, post_se = 1.0, cell_mean[c], 0.0
            elif t2 <= 0.0:
                w, post_mean, post_se = 0.0, mu, pool_g_se[s][1]
            else:
                w = t2 / (t2 + se2)
                post_mean = w * cell_mean[c] + (1.0 - w) * mu
                post_se = math.sqrt(1.0 / (1.0 / se2 + 1.0 / t2))
            thin = w < 0.5
        if thin:
            point[c], upper[c] = pool_point[s], pool_upper[s]
        else:
            point[c] = ceil_floor(-post_mean)
            q = t_quantile(g)
            upper[c] = (
                ceil_floor(q * post_se - post_mean)
                if (q is not None and post_se is not None)
                else pool_upper[s]
            )
        detail[c] = {
            "n": len(rs), "G": g, "mean": cell_mean[c], "se": se, "w": w,
            "post_mean": post_mean, "post_se": post_se, "thin": thin,
        }
    return point, pool_point, upper, pool_upper, detail


# ------------------------------------------------------------- the checks


def run(rows: list[GradeRow]) -> int:
    point, pool_point, upper, pool_upper, detail = proto_tables(rows)
    live = estimate_retained_floor(rows)
    n_thin = sum(d["thin"] for d in detail.values())
    print(f"grade rows: {len(rows)}; cells {len(point)}; thin {n_thin}")
    print(f"pool POINT (proto): {dict(sorted(pool_point.items()))}")
    print(f"pool POINT (live) : {dict(sorted(live.pool_floor_cc.items()))}")
    print(f"pool UPPER (proto): {dict(sorted(pool_upper.items()))}"
          "  [build A rule, the wire since 22:45:44Z]")
    print(f"pool mean cc/ct   : {dict(sorted(pool_mean_cc(rows).items()))}")
    assert live.published, live.reason
    # 1. point table == live estimator, cell by cell, and the pools
    mismatch = [c for c in set(point) | set(live.table) if point.get(c) != live.table.get(c)]
    print(f"1. PARITY proto point == live estimate_retained_floor: "
          f"{len(point) - len(mismatch)}/{len(point)} cells; pools "
          f"{'equal' if pool_point == dict(live.pool_floor_cc) else 'DIFFER'}")
    for c in mismatch[:10]:
        print(f"   MISMATCH {'|'.join(c)} proto {point.get(c)} live {live.table.get(c)}")
    assert not mismatch and pool_point == dict(live.pool_floor_cc)
    thin_live = sum(1 for c in live.cells if c.thin)
    assert thin_live == sum(d["thin"] for d in detail.values())
    # 2. the upper table reproduces the live 23:30:04Z log line (if unchanged)
    same_grade = len(rows) == LIVE_ROWS
    floors = sorted(upper.values())
    print(f"2. UPPER reproduces the live log line: rows {len(rows)} vs {LIVE_ROWS}"
          f" ({'same grade' if same_grade else 'grade moved since 23:30Z'}); cells"
          f" {len(point)} vs {LIVE_CELLS}; thin {thin_live} vs {LIVE_THIN}; floor median"
          f" {floors[len(floors) // 2]} vs {LIVE_FLOOR_MEDIAN}; max {floors[-1]} vs"
          f" {LIVE_FLOOR_MAX}; pools"
          f" {'EQUAL' if pool_upper == LIVE_POOL_UPPER else 'differ: ' + str(pool_upper)}")
    if same_grade:
        assert pool_upper == LIVE_POOL_UPPER
        assert len(point) == LIVE_CELLS and thin_live == LIVE_THIN
        assert floors[len(floors) // 2] == LIVE_FLOOR_MEDIAN and floors[-1] == LIVE_FLOOR_MAX
    # 3. properties
    assert all(0 <= point[c] <= upper[c] for c in point), "point must sit inside [0, upper]"
    for c, d in detail.items():
        if not d["thin"] and d["post_mean"] >= 0.0:
            assert point[c] == 0, (c, point[c])
    print("3. PROPERTIES: 0 <= point <= upper on every cell;"
          " non-negative shrunk shortfall -> 0: OK")
    # a few named cells + the unknown-sport rule
    for c in sorted(point, key=lambda k: -point[k])[:12]:
        d = detail[c]
        se = "-" if d["se"] is None else f"{d['se']:.1f}"
        print(f"   {'|'.join(c):<66} n={d['n']:>4} G={d['G']:>4} mean={d['mean']:>8.1f}"
              f" post={d['post_mean']:>8.1f} se={se:>7}"
              f" thin={d['thin']!s:<5} point={point[c]:>5} upper={upper[c]:>6}")
    n_pos = sum(1 for c in point if point[c] > 0 and not detail[c]["thin"])
    n_pop = sum(1 for c in point if not detail[c]["thin"])
    print(f"   populated cells with a positive point floor (measured loss): {n_pos}/{n_pop};"
          f" populated at 0 (fee alone): {n_pop - n_pos}")
    unknown: CellKey = ("nfl", "moneyline|moneyline", "all_yes", "cross")
    assert floor_for_cell(unknown, live.table, live.pool_floor_cc) == max(pool_point.values())
    print(f"   unknown sport -> largest pool point {max(pool_point.values())}: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    from tools.diagnostics.fee_floor_counterfactual import grade_rows_ro

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--db", default="file:D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3?mode=ro"
    )
    ap.add_argument(
        "--as-of", default=None,
        help="ISO time: keep only rows settled BEFORE it (a grade as of a past sweep;"
        " note the boot's stale-row closes carry historical settle stamps)",
    )
    args = ap.parse_args(argv)
    con = sqlite3.connect(args.db, uri=True, timeout=5)
    rows = grade_rows_ro(con)
    if args.as_of:
        rows = [r for r in rows if r.settled_at < args.as_of]
    rc = run(rows)
    print("ALL PARITY CHECKS PASSED")
    return rc


if __name__ == "__main__":
    sys.exit(main())
