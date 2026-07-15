"""Daily report: flow, decisions, fills, EV-vs-realized, markouts, portfolio MC.

Grading discipline (mission rule #3): the operation is graded on cumulative
expected edge vs realized P&L over a large sample — never on single outcomes.
Every statistic carries its sample count. Demo P&L validates plumbing, not
edge, and this report says so when env is demo.
"""

from __future__ import annotations

from typing import Any

from combomaker.ops.persistence import Store
from combomaker.risk.exposure import ExposureBook, MarginalProvider
from combomaker.sim.book_model import WithinGameRhoProvider, build_book_model
from combomaker.sim.book_risk import compute_book_risk

JsonDict = dict[str, Any]


async def build_report(
    store: Store,
    *,
    env: str,
    exposure: ExposureBook | None = None,
    marginals: MarginalProvider | None = None,
    mc_samples: int = 100_000,
    within_game_rho: WithinGameRhoProvider | None = None,
    bankroll_cc: int | None = None,
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
        report["portfolio_mc"] = _portfolio_mc(
            exposure,
            marginals,
            mc_samples,
            within_game_rho=within_game_rho,
            bankroll_cc=bankroll_cc,
        )
    return report


def _portfolio_mc(
    exposure: ExposureBook,
    marginals: MarginalProvider,
    n_samples: int,
    *,
    within_game_rho: WithinGameRhoProvider | None = None,
    bankroll_cc: int | None = None,
) -> JsonDict:
    """Standing portfolio-risk MC over open positions, built the PRICER's way
    (Phase 4 / M1): a block-diagonal game-keyed correlation and per-position NO
    handling, NOT the old independence ``np.eye`` + complement pseudo-legs (F8).

    The correlation view now matches the fair we quoted — the risk sim and the
    pricer share a joint (parity-gated in ``test_sim_book_model``). CVaR is at the
    ``high`` band (correlation uncertainty widens risk), and the operative ES is
    the max-of-three challenger/stress overlay (§5). UNKNOWN marginals ⇒ the
    snapshot is flagged unusable (fail-closed) — never a silent 0.5 placeholder in
    the stats (the old report's live UNKNOWN-is-never-safe violation)."""
    positions = list(exposure.positions.values())
    if not positions:
        return {"positions": 0}
    model = build_book_model(
        positions, marginals=marginals, within_game_rho=within_game_rho
    )
    snap = compute_book_risk(
        model,
        n_samples=n_samples,
        seed=7,
        band="high",
        bankroll_cc=bankroll_cc,
    )
    out: JsonDict = {
        "positions": len(positions),
        "unknown_marginals": snap.unknown,
        "usable": snap.usable,
        "band": snap.band,
        "ev_cc": round(snap.ev_cc, 1),
        "ev_stderr_cc": round(snap.ev_stderr_cc, 2),
        "p_profit": round(snap.p_profit, 4),
        "var_99_cc": round(snap.var_99_cc, 1),
        "es_99_cc": round(snap.es_99_cc, 1),
        "production_es_99_cc": round(snap.production_es_99_cc, 1),
        "challenger_es_99_cc": round(snap.challenger_es_99_cc, 1),
        "governing_model_es_99_cc": round(snap.governing_model_es_99_cc, 1),
        "deterministic_max_loss_cc": round(snap.deterministic_max_loss_cc, 1),
        "per_game_tail_cc": {
            c.key: round(c.loss_cc, 1) for c in snap.per_game_tail_cc[:10]
        },
    }
    if snap.p_loss_worse_than:
        out["p_loss_worse_than"] = {
            str(int(k)): round(v, 4) for k, v in snap.p_loss_worse_than.items()
        }
    return out


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
