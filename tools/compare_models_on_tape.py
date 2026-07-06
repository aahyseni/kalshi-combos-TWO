"""v1 copula vs structural vs independence, measured against WINNING quotes.

Every executed combo trade on the tape is a taker lifting the auction-winning
maker's price — the closest thing to ground truth on what the market thinks
the joint is worth. For each trade we take the latest shadow would-quote on
the same combo market (its stored leg marginals are the market state at quote
time) and re-price the combo three ways OFFLINE:

  independence   product of selected-side marginals
  v1 copula      shipped CorrelationConfig pair tables through the pricer
  structural     Dixon-Coles per soccer match / margin-total per game where
                 parseable, INDEPENDENT ACROSS GAMES (cross_event_rho = 0),
                 marginals for unparseable single legs — i.e. the hybrid the
                 engine would run; combos with no structurally-priceable
                 group fall back to the copula fair (reported separately)

Metrics per model: signed and absolute distance of fair from the executed
price, win-rate (closest to the winning quote), and the maker-viability rate
(fair below the clearing price — the side you can sell at profitably).

Run:  uv run python tools/compare_models_on_tape.py [--db data/combomaker-prod.sqlite3]
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

from combomaker.ops.config import CorrelationConfig, MarginTotalConfig, StructuralConfig
from combomaker.pricing.joint import price_joint_matrices
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.pricing.structural import StructuralPricer
from combomaker.rfq.models import RfqLeg

_GAME_CODE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?[A-Z0-9]+$")


@dataclass
class Sample:
    trade_price: float          # executed YES price, prob space
    legs: list[RfqLeg]
    sides: list[str]
    probs: list[float]          # YES-side marginals at quote time
    n_legs: int


def game_key(ticker: str) -> str | None:
    parts = ticker.split("-")
    if len(parts) < 2 or not _GAME_CODE.match(parts[1]):
        return None
    return parts[1]


def load_samples(db_path: str) -> list[Sample]:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = db.execute(
        """
        SELECT ct.trade_id, ct.yes_price_cc, ct.created_time, r.legs_json,
               wq.leg_probs_json,
               MAX(wq.at)
        FROM combo_trades ct
        JOIN rfqs r ON r.market_ticker = ct.ticker
        JOIN would_quotes wq ON wq.rfq_id = r.rfq_id AND wq.at <= ct.created_time
        GROUP BY ct.trade_id
        """
    ).fetchall()
    samples: list[Sample] = []
    for _tid, price_cc, _ts, legs_json, probs_json, _at in rows:
        try:
            legs_raw = json.loads(legs_json)
            probs = json.loads(probs_json)
        except (TypeError, json.JSONDecodeError):
            continue
        if len(legs_raw) != len(probs) or not legs_raw:
            continue
        legs = [
            RfqLeg(
                market_ticker=leg["market_ticker"],
                event_ticker=leg.get("event_ticker"),
                side=leg.get("side", "yes"),
                yes_settlement_value_cc=None,
            )
            for leg in legs_raw
        ]
        if any(leg.side not in ("yes", "no") for leg in legs):
            continue
        samples.append(
            Sample(
                trade_price=price_cc / 10_000.0,
                legs=legs,
                sides=[leg.side for leg in legs],
                probs=[float(p) for p in probs],
                n_legs=len(legs),
            )
        )
    return samples


def event_groups(legs: list[RfqLeg]) -> list[tuple[int, ...]]:
    by_game: dict[str, list[int]] = defaultdict(list)
    for i, leg in enumerate(legs):
        key = game_key(leg.market_ticker)
        if key is not None:
            by_game[key].append(i)
    return [tuple(idx) for idx in by_game.values() if len(idx) > 1]


def price_copula(sample: Sample, params: SgpParams) -> float:
    corr = build_sgp_correlation(
        sample.legs, event_groups(sample.legs), params, marginals=sample.probs
    )
    beliefs = [LegBelief(p=p, uncertainty=0.005, source="db") for p in sample.probs]
    est = price_joint_matrices(
        beliefs, sample.sides, corr.corr, corr.corr_low, corr.corr_high
    )
    return est.p


def price_hybrid_structural(
    sample: Sample, pricer: StructuralPricer, params: SgpParams
) -> tuple[float, bool]:
    """(joint, used_structural): structural per game, independent across
    games; per-game copula fallback when the group can't be priced."""
    groups = {g: list(g) for g in event_groups(sample.legs)}
    grouped = {i for g in groups for i in g}
    joint = 1.0
    used = False
    for g in groups.values():
        legs = [sample.legs[i] for i in g]
        beliefs = [LegBelief(sample.probs[i], 0.005, "db") for i in g]
        sides = [sample.sides[i] for i in g]
        est, _reason = pricer.try_price(legs, beliefs, sides)
        if est is not None:
            joint *= est.p
            used = True
            continue
        corr = build_sgp_correlation(legs, [tuple(range(len(g)))], params,
                                     marginals=[b.p for b in beliefs])
        joint *= price_joint_matrices(
            beliefs, sides, corr.corr, corr.corr_low, corr.corr_high
        ).p
    for i, (p, side) in enumerate(zip(sample.probs, sample.sides, strict=True)):
        if i not in grouped:
            joint *= p if side == "yes" else 1.0 - p
    return joint, used


def independence(sample: Sample) -> float:
    joint = 1.0
    for p, side in zip(sample.probs, sample.sides, strict=True):
        joint *= p if side == "yes" else 1.0 - p
    return joint


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/combomaker-prod.sqlite3")
    args = ap.parse_args()

    cfg = CorrelationConfig()
    params = SgpParams(
        pair_rho=dict(cfg.pair_rho),
        default_rho=cfg.same_event_rho,
        cross_event_rho=cfg.cross_event_rho,
        typed_uncertainty=cfg.typed_rho_uncertainty,
        untyped_uncertainty=cfg.untyped_rho_uncertainty,
        pair_uncertainty=dict(cfg.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in cfg.pair_rho_by_sport.items()},
    )
    pricer = StructuralPricer(
        StructuralConfig(enabled=True),
        MarginTotalConfig(enabled_sports=["nfl", "nba", "wnba"]),
    )

    samples = load_samples(args.db)
    print(f"matched executed trades: {len(samples)}")

    rows = []
    n_structural = 0
    for s in samples:
        try:
            p_ind = independence(s)
            p_cop = price_copula(s, params)
            p_str, used = price_hybrid_structural(s, pricer, params)
        except Exception:
            continue
        n_structural += used
        rows.append((s, p_ind, p_cop, p_str, used))
    print(f"priced: {len(rows)} (structural applied on {n_structural})")

    def report(name: str, subset: list) -> None:
        if not subset:
            return
        print(f"\n{name} (n={len(subset)})")
        for label, idx in (("independence", 1), ("v1 copula", 2), ("structural", 3)):
            errs = [(r[idx] - r[0].trade_price) for r in subset]
            abs_errs = [abs(e) for e in errs]
            below = sum(1 for e in errs if e < 0)
            print(
                f"  {label:13s} mean|err|={statistics.mean(abs_errs)*100:5.2f}c  "
                f"median|err|={statistics.median(abs_errs)*100:5.2f}c  "
                f"bias={statistics.mean(errs)*100:+5.2f}c  "
                f"fair<clearing {below/len(errs)*100:4.0f}%"
            )
        wins = {"independence": 0, "v1 copula": 0, "structural": 0, "tie": 0}
        for r in subset:
            errs = {
                "independence": abs(r[1] - r[0].trade_price),
                "v1 copula": abs(r[2] - r[0].trade_price),
                "structural": abs(r[3] - r[0].trade_price),
            }
            best = min(errs.values())
            leaders = [k for k, v in errs.items() if v - best < 0.0005]  # 0.05c
            wins[leaders[0] if len(leaders) == 1 else "tie"] += 1
        total = len(subset)
        print("  closest-to-winning-quote: "
              + "  ".join(f"{k}={v/total*100:.0f}%" for k, v in wins.items()))

    report("ALL matched trades", rows)
    report("structural actually applied", [r for r in rows if r[4]])
    report("2-3 legs", [r for r in rows if r[0].n_legs <= 3])
    report("4+ legs", [r for r in rows if r[0].n_legs >= 4])


if __name__ == "__main__":
    main()
