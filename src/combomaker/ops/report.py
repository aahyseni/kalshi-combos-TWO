"""Daily report: flow, decisions, fills, EV-vs-realized, markouts, portfolio MC.

Grading discipline (mission rule #3): the operation is graded on cumulative
expected edge vs realized P&L over a large sample — never on single outcomes.
Every statistic carries its sample count. Demo P&L validates plumbing, not
edge, and this report says so when env is demo.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from combomaker.core.conventions import Side
from combomaker.ops.persistence import Store
from combomaker.risk.exposure import ExposureBook, MarginalProvider
from combomaker.sim.engine import ComboPosition, LegModel, simulate

JsonDict = dict[str, Any]


async def build_report(
    store: Store,
    *,
    env: str,
    exposure: ExposureBook | None = None,
    marginals: MarginalProvider | None = None,
    mc_samples: int = 100_000,
) -> JsonDict:
    report: JsonDict = {
        "env": env,
        "note": (
            "demo P&L validates plumbing, never edge — not a graduation criterion"
            if env == "demo"
            else "graded on cumulative expected edge vs realized P&L, ±2σ MC bands"
        ),
        "rfqs_seen": await store.count("rfqs"),
        "decisions_by_kind": await store.decision_kind_counts(),
        "skip_reasons": await store.decision_reason_counts(),
        "would_quotes": await store.count("would_quotes"),
        "ev": await store.ev_summary(),
        "markouts": await store.markout_summary(),
    }
    if exposure is not None and marginals is not None:
        report["portfolio_mc"] = _portfolio_mc(exposure, marginals, mc_samples)
    return report


def _portfolio_mc(
    exposure: ExposureBook, marginals: MarginalProvider, n_samples: int
) -> JsonDict:
    """Monte Carlo over open positions (independence corr for the report; the
    pricing-side correlation lives in quotes, this is the standing risk view)."""
    positions = list(exposure.positions.values())
    if not positions:
        return {"positions": 0}
    leg_index: dict[str, int] = {}
    legs: list[LegModel] = []
    unknown = False
    for position in positions:
        for leg in position.legs:
            if leg.market_ticker not in leg_index:
                p = marginals(leg.market_ticker)
                if p is None:
                    unknown = True
                    p = 0.5  # placeholder; flagged below, stats marked unusable
                leg_index[leg.market_ticker] = len(legs)
                legs.append(LegModel(p=p))
    sim_positions = []
    for position in positions:
        # NOTE: sim legs are YES-side; a "no" selected side is a complement —
        # handled by flipping the leg probability into a dedicated leg model
        # is not possible per-position, so map: use the position's selected
        # sides via per-position leg list of (index, side). The v1 sim models
        # the AND of YES legs only; NO legs are approximated by complementary
        # pseudo-legs.
        indices = []
        for leg in position.legs:
            if leg.side == "yes":
                indices.append(leg_index[leg.market_ticker])
            else:
                pseudo = f"~{leg.market_ticker}"
                if pseudo not in leg_index:
                    base = legs[leg_index[leg.market_ticker]]
                    leg_index[pseudo] = len(legs)
                    legs.append(LegModel(p=1.0 - base.p))
                indices.append(leg_index[pseudo])
        sim_positions.append(
            ComboPosition(
                leg_indices=tuple(indices),
                side="yes" if position.our_side is Side.YES else "no",
                contracts=max(1, int(position.contracts) // 100),
                price_cc=int(position.entry_price_cc),
            )
        )
    corr = np.eye(len(legs))
    stats = simulate(legs, corr, sim_positions, n_samples=n_samples, seed=7)
    return {
        "positions": len(positions),
        "unknown_marginals": unknown,
        "ev_cc": round(stats.ev_cc, 1),
        "p_profit": round(stats.p_profit, 4),
        "var_cc": {str(k): round(v, 1) for k, v in stats.var_cc.items()},
        "es_cc": {str(k): round(v, 1) for k, v in stats.es_cc.items()},
    }


def format_report(report: JsonDict) -> str:
    lines = [
        f"combomaker daily report — env={report['env']}",
        f"  {report['note']}",
        f"  RFQs seen: {report['rfqs_seen']}  would-quotes: {report['would_quotes']}",
        f"  decisions: {report['decisions_by_kind']}",
        "  skip reasons:",
    ]
    for reason, count in sorted(report["skip_reasons"].items(), key=lambda kv: -kv[1]):
        lines.append(f"    {reason}: {count}")
    ev = report["ev"]
    lines.append(
        f"  fills: {ev['fills']}  expected edge: {ev['expected_edge_cc'] / 10_000:.2f}$"
        f"  settled: {ev['settled']}  realized: {ev['realized_pnl_cc'] / 10_000:.2f}$"
    )
    lines.append("  markouts (mean drift, cc):")
    for row in report["markouts"]:
        lines.append(
            f"    +{row['horizon_s']:.0f}s  n={row['n']}  fair={row['mean_fair_drift_cc']}"
            f"  raw_mid={row['mean_raw_mid_drift_cc']}"
        )
    if "portfolio_mc" in report:
        lines.append(f"  portfolio MC: {report['portfolio_mc']}")
    return "\n".join(lines)
