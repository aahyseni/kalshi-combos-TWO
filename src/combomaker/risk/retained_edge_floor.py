"""MEASURED retained-edge floor per combo CELL (2026-09-04 build A item 2 —
the repair for the rebate-ate-the-margin cells: rfi×rfi cross-game NO pairs
sold at 0.02-0.25c retained, "nobody homers" HR baskets at 0.09c; REPAIRED
the same day by build "floor-point-estimate" — see WHY THE POINT below).

The inventory rebate used to be capped by a HAND FRACTION of the margin
(``margin // 2``, itself the 8/16 tightening of "the whole margin"). The
constitution wants the cap MEASURED. This module estimates, per cell, how
much retained margin the cell's own settled record says a fill must keep:

    floor_cc(cell) = fee_cc(bid, measured schedule)         ← added in
                   + max(0, −shortfall_post(cell))            construct_quote

with shortfall = (realized − modeled) per contract on the cell's SETTLED
positions (the store's grade: ``position_ledger.realized_pnl_cc`` vs the
fills' ``expected_edge_cc``, both net of the exchange fee — like for like),
contract-weighted, pooled over the whole settled history (never a P&L
window), SE GAME-CLUSTERED (a combo's games are one loss event; the
cluster is its game set), and ``shortfall_post`` the EMPIRICAL-BAYES SHRUNK
POINT estimate toward the sport pool:

    τ²_sport = max(0, Var_cells(x̄_c) − mean_c(SE_c²))     (method of moments)
    w_c      = τ² / (τ² + SE_c²)  = n / (n + n0), n0 = σ²/τ²   (weight on the cell)
    post     = w_c·x̄_c + (1 − w_c)·μ_sport,  post_var = 1/(1/SE_c² + 1/τ²)

A cell whose own data carry less than half the posterior weight (SE_c² >
τ² — the DERIVED n_min: the count at which a cell's clustered SE first
falls below the between-cell dispersion) is THIN and takes the sport
pool's POINT (max(0, −μ_sport)); a sport with no pool takes the LARGEST
pool point (``pricing/retained_cell.floor_for_cell`` — the fail-closed
DIRECTION, but a point). A NEGATIVE cell (the record says we lose: e.g.
mlb|rfi|rfi|all_no|cross at −20c/ct) keeps its whole measured shortfall
as the floor — that is the mechanism working: no rebate where the record
says we lose. A cell at or above the model floors at 0: the fee alone.
Nothing publishes until the settled record spans the pre-registered
pooled-read minimum (``MIN_POOL_DAYS``).

WHY THE POINT (2026-09-04 build "floor-point-estimate"). Build A published
``max(0, t_{G−1}(Φ(−3))·SE − shortfall)`` — the policy z ladder's daily
anchor as a 3σ UPPER bound on the adverse selection. Live at 22:45:44Z it
published 646 cells (445 thin) with pool floors mlb 5.9c / soccer 11.6c /
esports 26.7c / cross-sport 49.5c and populated floors of 15-59c: per-
contract settlement noise on a sell-only book is ~40-50c, so at 30-170
settled games the clustered SE is 4-15c and THREE of them dwarf every
1-3c tier margin. The rebate cap ``margin − fee − floor`` was <= 0 on
essentially every quote — the constitutional diversity steer (the skew
REBATE for offsetting / diversifying flow) was muted on the whole wire.
The z ladder anchors TAIL risk (KILL, ruin, the caps); a retained-margin
floor is the point estimate of a COST, and its uncertainty belongs to the
quote WIDTH (which already scales with uncertainty), not to the margin.
The SE is still estimated and reported (``CellEstimate.post_se_cc``) for
that width seam — see the build report for why it is not fed yet.

This is the RETAINED-margin side only: the widen direction and every cap
are untouched; the quote path does one dict lookup (``table``).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from combomaker.pricing.grouping import game_key
from combomaker.pricing.retained_cell import CellKey, cell_key
from combomaker.risk.exposure import LegRef

# The pre-registered pooled-read horizon (feedback_no_refit_on_pnl: alarms
# and reads are multi-week, never a P&L window; the deep-dive spec fixed
# the floor's pool at >= 14 days). A cadence anchor, not a pricing number:
# below it NOTHING is published and the quote path keeps the fee-only floor.
MIN_POOL_DAYS = 14.0


@dataclass(frozen=True, slots=True)
class GradeRow:
    """One settled combo's grade: its cell, size, realized vs modeled P&L
    (both net of the exchange fee, int cc), its game set (the cluster) and
    when it settled."""

    cell: CellKey
    contracts_centi: int
    realized_cc: int
    modeled_cc: int
    games: frozenset[str]
    settled_at: str

    @property
    def shortfall_per_contract_cc(self) -> float:
        return (self.realized_cc - self.modeled_cc) * 100.0 / self.contracts_centi


@dataclass(frozen=True, slots=True)
class PoolStats:
    n_rows: int
    n_clusters: int
    contracts_centi: int
    mean_cc: float
    se_cc: float | None  # None: fewer than 2 clusters


@dataclass(frozen=True, slots=True)
class CellEstimate:
    cell: CellKey
    stats: PoolStats
    post_mean_cc: float
    post_se_cc: float | None
    weight_on_cell: float
    thin: bool
    floor_cc: int
    source: str  # "cell" | "pool"


@dataclass(frozen=True, slots=True)
class FloorEstimate:
    published: bool
    reason: str
    span_days: float
    table: dict[CellKey, int] = field(default_factory=dict)
    cells: tuple[CellEstimate, ...] = ()
    pools: dict[str, PoolStats] = field(default_factory=dict)
    tau2_by_sport: dict[str, float] = field(default_factory=dict)
    # Per sport: max(0, −pool mean) — the POINT a thin or absent cell takes.
    pool_floor_cc: dict[str, int] = field(default_factory=dict)


def grade_row_from_store(row: Mapping[str, object]) -> GradeRow | None:
    """One ``Store.settled_grade_rows`` row → its GradeRow; None when the
    legs cannot be read (skipped, counted by the caller).

    Like for like: the model's edge net of the fee ACTUALLY charged (booked
    fee added back, the settlement echo of the exchange fee taken out),
    scaled to the ledger's contracts when the fills and the ledger disagree
    on size (partial recoveries). CLUSTER: the combo's game set; a row whose
    legs carry no event ticker is its OWN cluster, keyed on the combo
    ticker (2026-09-04 review fix S8 — such rows used to share one empty
    frozenset, collapsing every one of them into a single cluster).

    Pure; the lifecycle sweep and the read-only replay tool both call it
    (rule 8: one conversion, never a copy)."""
    try:
        legs = [
            LegRef(
                market_ticker=str(leg["market_ticker"]),
                event_ticker=leg.get("event_ticker"),
                side=str(leg["side"]),
            )
            for leg in json.loads(str(row["legs_json"]))
        ]
    except (ValueError, KeyError, TypeError):
        return None
    if not legs:
        return None
    ledger_ct = int(row["ledger_contracts_centi"])  # type: ignore[call-overload]
    fill_ct = int(row["fill_contracts_centi"])  # type: ignore[call-overload]
    modeled = int(row["expected_edge_cc"]) + int(row["fill_fee_cc"])  # type: ignore[call-overload]
    if fill_ct != ledger_ct and fill_ct > 0:
        modeled = modeled * ledger_ct // fill_ct
    modeled -= int(row["settlement_fee_cc"])  # type: ignore[call-overload]
    games = frozenset(game_key(leg.event_ticker) for leg in legs if leg.event_ticker)
    if not games:
        games = frozenset({f"combo:{row['combo_ticker']}"})
    return GradeRow(
        cell=cell_key(legs),
        contracts_centi=ledger_ct,
        realized_cc=int(row["realized_pnl_cc"]),  # type: ignore[call-overload]
        modeled_cc=modeled,
        games=games,
        settled_at=str(row["settled_at"]),
    )


def _parse_iso(text: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def pool_stats(rows: Iterable[GradeRow]) -> PoolStats:
    """Contract-weighted mean shortfall per contract and its GAME-CLUSTERED
    standard error (cluster sandwich with the G/(G−1) small-sample factor)."""
    rows = list(rows)
    total_w = sum(r.contracts_centi for r in rows)
    if not rows or total_w <= 0:
        return PoolStats(0, 0, 0, 0.0, None)
    mean = math.fsum(r.contracts_centi * r.shortfall_per_contract_cc for r in rows) / total_w
    by_cluster: dict[frozenset[str], float] = defaultdict(float)
    for r in rows:
        by_cluster[r.games] += r.contracts_centi * (r.shortfall_per_contract_cc - mean)
    g = len(by_cluster)
    if g < 2:
        return PoolStats(len(rows), g, total_w, mean, None)
    var = math.fsum(v * v for v in by_cluster.values()) / (total_w * total_w) * g / (g - 1)
    return PoolStats(len(rows), g, total_w, mean, math.sqrt(var))


def point_floor_cc(mean_cc: float) -> int:
    """max(0, ⌈−mean⌉): the retained margin (cc, EXCLUDING the fee) a
    measured mean shortfall says a fill must keep. A cell at or above the
    model floors at 0 — the fee alone; a losing cell keeps its whole
    measured loss. Never negative (a rebate is never widened by a floor)."""
    return max(0, math.ceil(-mean_cc))


def estimate_retained_floor(
    rows: Iterable[GradeRow], *, min_pool_days: float = MIN_POOL_DAYS
) -> FloorEstimate:
    rows = [r for r in rows if r.contracts_centi > 0]
    if not rows:
        return FloorEstimate(False, "no_settled_rows", 0.0)
    stamps = [dt for dt in (_parse_iso(r.settled_at) for r in rows) if dt is not None]
    if not stamps:
        return FloorEstimate(False, "unparseable_settle_times", 0.0)
    span_days = (max(stamps) - min(stamps)).total_seconds() / 86_400.0
    if span_days < min_pool_days:
        return FloorEstimate(False, "pool_span_below_minimum", span_days)
    by_cell: dict[CellKey, list[GradeRow]] = defaultdict(list)
    by_sport: dict[str, list[GradeRow]] = defaultdict(list)
    for r in rows:
        by_cell[r.cell].append(r)
        by_sport[r.cell[0]].append(r)
    pools = {sport: pool_stats(rs) for sport, rs in by_sport.items()}
    cell_stats = {cell: pool_stats(rs) for cell, rs in by_cell.items()}
    # Between-cell dispersion per sport (method of moments over the cells
    # whose clustered SE is defined).
    tau2: dict[str, float] = {}
    for sport, pool in pools.items():
        defined = [
            st for cell, st in cell_stats.items() if cell[0] == sport and st.se_cc is not None
        ]
        if len(defined) < 2:
            tau2[sport] = 0.0
            continue
        mu = pool.mean_cc
        between = math.fsum((st.mean_cc - mu) ** 2 for st in defined) / (len(defined) - 1)
        within = math.fsum(st.se_cc**2 for st in defined if st.se_cc is not None) / len(defined)
        tau2[sport] = max(0.0, between - within)
    # The sport pool's POINT: what a thin or absent cell of the sport takes.
    pool_floor: dict[str, int] = {
        sport: point_floor_cc(pool.mean_cc) for sport, pool in pools.items()
    }
    cells: list[CellEstimate] = []
    table: dict[CellKey, int] = {}
    for cell, st in cell_stats.items():
        sport = cell[0]
        pool = pools[sport]
        t2 = tau2[sport]
        if st.se_cc is None:
            est = CellEstimate(
                cell, st, pool.mean_cc, pool.se_cc, 0.0, True, pool_floor[sport], "pool"
            )
        else:
            se2 = st.se_cc**2
            post_se: float | None
            if se2 <= 0.0:
                # Every cluster agrees exactly: the cell's own data carry the
                # whole weight (a degenerate but well-defined posterior).
                w = 1.0
                post_mean, post_se = st.mean_cc, 0.0
            elif t2 <= 0.0:
                w = 0.0
                post_mean, post_se = pool.mean_cc, pool.se_cc
            else:
                w = t2 / (t2 + se2)
                post_mean = w * st.mean_cc + (1.0 - w) * pool.mean_cc
                post_se = math.sqrt(1.0 / (1.0 / se2 + 1.0 / t2))
            thin = w < 0.5  # the derived n_min: the cell's SE must beat τ
            if thin:
                est = CellEstimate(cell, st, post_mean, post_se, w, True, pool_floor[sport], "pool")
            else:
                est = CellEstimate(
                    cell, st, post_mean, post_se, w, False, point_floor_cc(post_mean), "cell"
                )
        cells.append(est)
        table[cell] = est.floor_cc
    return FloorEstimate(
        published=True,
        reason="ok",
        span_days=span_days,
        table=table,
        cells=tuple(sorted(cells, key=lambda c: c.cell)),
        pools=pools,
        tau2_by_sport=tau2,
        pool_floor_cc=pool_floor,
    )


def summarize(estimate: FloorEstimate) -> Mapping[str, object]:
    """Log-line view."""
    floors = sorted(c.floor_cc for c in estimate.cells)
    populated = [c for c in estimate.cells if not c.thin]
    return {
        "published": estimate.published,
        "reason": estimate.reason,
        "span_days": round(estimate.span_days, 1),
        "rule": "shrunk_point",
        "n_cells": len(estimate.cells),
        "n_thin": sum(1 for c in estimate.cells if c.thin),
        # Populated cells whose record says we lose (floor > 0: no rebate
        # room beyond margin − fee − loss) vs at/above the model (fee alone).
        "n_populated_losing": sum(1 for c in populated if c.floor_cc > 0),
        "n_populated_at_fee": sum(1 for c in populated if c.floor_cc == 0),
        "floor_cc_min": floors[0] if floors else None,
        "floor_cc_median": floors[len(floors) // 2] if floors else None,
        "floor_cc_max": floors[-1] if floors else None,
        "pool_floor_cc": dict(sorted(estimate.pool_floor_cc.items())),
        "pool_mean_cc_by_sport": {
            s: round(p.mean_cc, 1) for s, p in sorted(estimate.pools.items())
        },
        "pool_se_cc_by_sport": {
            s: (None if p.se_cc is None else round(p.se_cc, 1))
            for s, p in sorted(estimate.pools.items())
        },
        "tau_cc_by_sport": {
            s: round(math.sqrt(t), 2) for s, t in sorted(estimate.tau2_by_sport.items())
        },
    }
