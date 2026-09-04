"""MEASURED retained-edge floor per combo CELL (2026-09-04 build A item 2 —
the repair for the rebate-ate-the-margin cells: rfi×rfi cross-game NO pairs
sold at 0.02-0.25c retained, "nobody homers" HR baskets at 0.09c).

The inventory rebate used to be capped by a HAND FRACTION of the margin
(``margin // 2``, itself the 8/16 tightening of "the whole margin"). The
constitution wants the cap MEASURED. This module estimates, per cell, how
much retained margin the cell's own settled record says a fill must keep:

    floor_cc(cell) = fee_cc(bid, measured schedule)         ← added in
                   + AS_upper_cc(cell)                        construct_quote

    AS_upper_cc(cell) = max(0, z·SE(cell) − shortfall(cell))

with shortfall = (realized − modeled) per contract on the cell's SETTLED
positions (the store's grade: ``position_ledger.realized_pnl_cc`` vs the
fills' ``expected_edge_cc``, both net of the exchange fee — like for like),
contract-weighted, pooled over the whole settled history (never a P&L
window), SE GAME-CLUSTERED (a combo's games are one loss event; the
cluster is its game set), shrunk EMPIRICAL-BAYES toward the sport pool:

    τ²_sport = max(0, Var_cells(x̄_c) − mean_c(SE_c²))     (method of moments)
    w_c      = τ² / (τ² + SE_c²)                            (weight on the cell)
    post     = w_c·x̄_c + (1 − w_c)·μ_sport,  post_var = 1/(1/SE_c² + 1/τ²)

A cell whose own data carry less than half the posterior weight (SE_c² >
τ² — the DERIVED n_min: the count at which a cell's clustered SE first
falls below the between-cell dispersion) is THIN and takes the sport
pool's UPPER bound instead (fail-closed: a new shape can never be sold at
fair). ``z`` is the policy z ladder's daily anchor (risk/cap_family.py
K_DAILY = 3), not a new number — and it is applied as its TAIL PROBABILITY
through Student-t at G − 1 degrees of freedom, G the number of clusters
the SE was estimated from (``tail_quantile``): with 1,400 clusters the
3σ tail needs 3.0 SEs, with 12 it needs 3.85, with 3 it needs 19.2. The
SE of a 3-cluster pool is not a known quantity, and treating it as one
let the cross-sport pool (3 settled rows, +27.9c/ct) publish a floor of 0
that every absent cross-sport cell then inherited (2026-09-04 review fix
pass, surfaced by the counterfactual's cap-loosened flag). Nothing
publishes until the settled record spans the pre-registered pooled-read
minimum (``MIN_POOL_DAYS``).

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
from statistics import NormalDist

from scipy.stats import t as student_t

from combomaker.pricing.grouping import game_key
from combomaker.pricing.retained_cell import CellKey, cell_key
from combomaker.risk.cap_family import K_DAILY
from combomaker.risk.exposure import LegRef

# The pre-registered pooled-read horizon (feedback_no_refit_on_pnl: alarms
# and reads are multi-week, never a P&L window; the deep-dive spec fixed
# the floor's pool at >= 14 days). A cadence anchor, not a pricing number:
# below it NOTHING is published and the quote path keeps the fee-only floor.
MIN_POOL_DAYS = 14.0
Z_FLOOR = K_DAILY


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
    # The SE multiplier actually applied (Student-t at the cell's own
    # clusters − 1 df for the policy tail probability; None on a pool floor).
    quantile: float | None = None


@dataclass(frozen=True, slots=True)
class FloorEstimate:
    published: bool
    reason: str
    span_days: float
    z: float
    table: dict[CellKey, int] = field(default_factory=dict)
    cells: tuple[CellEstimate, ...] = ()
    pools: dict[str, PoolStats] = field(default_factory=dict)
    tau2_by_sport: dict[str, float] = field(default_factory=dict)
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


def tail_quantile(z: float, n_clusters: int) -> float | None:
    """The SE multiplier that reaches the policy anchor's TAIL PROBABILITY
    Φ(−z) when the SE was estimated from ``n_clusters`` clusters: the
    Student-t quantile at n_clusters − 1 degrees of freedom. → z as the
    clusters grow; None (nothing is measured) below two clusters."""
    if n_clusters < 2:
        return None
    p = float(NormalDist().cdf(z))
    return float(student_t.ppf(p, n_clusters - 1))


def _upper_floor_cc(
    mean_cc: float, se_cc: float | None, z: float, n_clusters: int
) -> tuple[int, float] | None:
    """(max(0, q·SE − mean), q): the tail-probability upper bound of the
    adverse selection, q the Student-t quantile for the clusters."""
    q = tail_quantile(z, n_clusters)
    if se_cc is None or q is None:
        return None
    return max(0, math.ceil(q * se_cc - mean_cc)), q


def estimate_retained_floor(
    rows: Iterable[GradeRow], *, z: float = Z_FLOOR, min_pool_days: float = MIN_POOL_DAYS
) -> FloorEstimate:
    rows = [r for r in rows if r.contracts_centi > 0]
    if not rows:
        return FloorEstimate(False, "no_settled_rows", 0.0, z)
    stamps = [dt for dt in (_parse_iso(r.settled_at) for r in rows) if dt is not None]
    if not stamps:
        return FloorEstimate(False, "unparseable_settle_times", 0.0, z)
    span_days = (max(stamps) - min(stamps)).total_seconds() / 86_400.0
    if span_days < min_pool_days:
        return FloorEstimate(False, "pool_span_below_minimum", span_days, z)
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
    pool_floor: dict[str, int] = {}
    for sport, pool in pools.items():
        upper = _upper_floor_cc(pool.mean_cc, pool.se_cc, z, pool.n_clusters)
        # A pool with a single cluster has no measured dispersion at all:
        # fail closed on it with the largest floor its own data allow — the
        # whole |mean| plus nothing more cannot be justified, so we take the
        # sport's absolute mean shortfall magnitude as the bound.
        pool_floor[sport] = upper[0] if upper is not None else max(0, math.ceil(-pool.mean_cc))
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
                # The quantile follows the cell's OWN clusters (the pool's
                # information enters through the shrunk mean/SE, not the df).
                own = _upper_floor_cc(post_mean, post_se, z, st.n_clusters)
                if own is None:
                    est = CellEstimate(
                        cell, st, post_mean, post_se, w, False, pool_floor[sport], "pool"
                    )
                else:
                    est = CellEstimate(
                        cell, st, post_mean, post_se, w, False, own[0], "cell", own[1]
                    )
        cells.append(est)
        table[cell] = est.floor_cc
    return FloorEstimate(
        published=True,
        reason="ok",
        span_days=span_days,
        z=z,
        table=table,
        cells=tuple(sorted(cells, key=lambda c: c.cell)),
        pools=pools,
        tau2_by_sport=tau2,
        pool_floor_cc=pool_floor,
    )


def summarize(estimate: FloorEstimate) -> Mapping[str, object]:
    """Log-line view."""
    floors = sorted(c.floor_cc for c in estimate.cells)
    return {
        "published": estimate.published,
        "reason": estimate.reason,
        "span_days": round(estimate.span_days, 1),
        "z": estimate.z,
        "pool_quantile_by_sport": {
            s: round(q, 2)
            for s, p in sorted(estimate.pools.items())
            if (q := tail_quantile(estimate.z, p.n_clusters)) is not None
        },
        "n_cells": len(estimate.cells),
        "n_thin": sum(1 for c in estimate.cells if c.thin),
        "floor_cc_min": floors[0] if floors else None,
        "floor_cc_median": floors[len(floors) // 2] if floors else None,
        "floor_cc_max": floors[-1] if floors else None,
        "pool_floor_cc": dict(sorted(estimate.pool_floor_cc.items())),
        "tau_cc_by_sport": {
            s: round(math.sqrt(t), 2) for s, t in sorted(estimate.tau2_by_sport.items())
        },
    }
