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
    taker_side: str             # which side the taker bought
    legs: list[RfqLeg]
    sides: list[str]
    probs: list[float]          # per-leg P(SELECTED side), exactly as recorded
                                # in would_quotes.leg_probs_json — stub.py stores
                                # `p_yes if side=="yes" else 1-p_yes` (its
                                # docstring: "per-leg P(selected side)")
    n_legs: int
    our_fair: float             # recorded stub (independence) fair_cc/1e4 —
                                # legacy; competitiveness now uses the engine fair
    our_half_width: float       # shadow would-quote width/2
    sport: str                  # single sport label or "mixed"

    @property
    def yes_marginals(self) -> list[float]:
        """YES-side marginals, the convention every pricer wants (LegBelief.p,
        structural inversion targets, sgp orientation). The tape stores the
        SELECTED-side prob, so flip NO legs back to YES here — the single
        conversion point between tape convention and pricer convention."""
        return [
            p if side == "yes" else 1.0 - p
            for p, side in zip(self.probs, self.sides, strict=True)
        ]


def game_key(ticker: str) -> str | None:
    parts = ticker.split("-")
    if len(parts) < 2 or not _GAME_CODE.match(parts[1]):
        return None
    return parts[1]


def sport_label(legs: list[RfqLeg]) -> str:
    from combomaker.pricing.legtypes import classify_sport

    sports = {str(classify_sport(leg.market_ticker)) for leg in legs}
    return sports.pop() if len(sports) == 1 else "mixed"


def load_samples(db_path: str) -> list[Sample]:
    # Read-only + timeout: the tape is being written live by the recorder, so a
    # bare connect can hit "database is locked". mode=ro (never immutable=1,
    # which corrupts reads on a live file); timeout waits out the writer.
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
    rows = db.execute(
        """
        SELECT ct.trade_id, ct.yes_price_cc, ct.taker_side, r.legs_json,
               wq.leg_probs_json, wq.fair_cc, wq.width_cc,
               MAX(wq.at)
        FROM combo_trades ct
        JOIN rfqs r ON r.market_ticker = ct.ticker
        JOIN would_quotes wq ON wq.rfq_id = r.rfq_id AND wq.at <= ct.created_time
        GROUP BY ct.trade_id
        """
    ).fetchall()
    samples: list[Sample] = []
    for _tid, price_cc, taker_side, legs_json, probs_json, fair_cc, width_cc, _at in rows:
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
                taker_side=str(taker_side),
                legs=legs,
                sides=[leg.side for leg in legs],
                probs=[float(p) for p in probs],
                n_legs=len(legs),
                our_fair=(fair_cc or 0) / 10_000.0,
                our_half_width=(width_cc or 0) / 2.0 / 10_000.0,
                sport=sport_label(legs),
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
    yes = sample.yes_marginals
    corr = build_sgp_correlation(
        sample.legs, event_groups(sample.legs), params, marginals=yes
    )
    beliefs = [LegBelief(p=p, uncertainty=0.005, source="db") for p in yes]
    est = price_joint_matrices(
        beliefs, sample.sides, corr.corr, corr.corr_low, corr.corr_high
    )
    return est.p


def price_hybrid_structural(
    sample: Sample, pricer: StructuralPricer, params: SgpParams
) -> tuple[float, bool]:
    """(joint, used_structural): structural per game, independent across
    games; per-game copula fallback when the group can't be priced."""
    yes = sample.yes_marginals
    groups = {g: list(g) for g in event_groups(sample.legs)}
    grouped = {i for g in groups for i in g}
    joint = 1.0
    used = False
    for g in groups.values():
        legs = [sample.legs[i] for i in g]
        beliefs = [LegBelief(yes[i], 0.005, "db") for i in g]
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
    for i in range(len(sample.probs)):
        if i not in grouped:
            joint *= sample.probs[i]  # ungrouped leg contributes P(selected side)
    return joint, used


def independence(sample: Sample) -> float:
    # sample.probs is already P(selected side); the independence joint is just
    # their product (this reproduces the recorded stub fair_cc — see stub.py).
    # Re-flipping NO legs here (`1-p`) would double-negate them — the H2 bug.
    joint = 1.0
    for p in sample.probs:
        joint *= p
    return joint


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/combomaker-prod.sqlite3")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="price only a random N-sample of the matched trades (demo speed); "
        "default = all. Full-tape logic is unchanged.",
    )
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for --limit")
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
    if args.limit is not None and args.limit < len(samples):
        import random

        samples = random.Random(args.seed).sample(samples, args.limit)
        print(f"--limit {args.limit}: random sample of {len(samples)} (seed={args.seed})")

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
    for sport in sorted({r[0].sport for r in rows}):
        report(f"sport: {sport}", [r for r in rows if r[0].sport == sport])

    # --- our shadow quote vs the actual winner ------------------------------
    # "Our fair" here is the ENGINE fair this tool computes (structural when it
    # applied, else the v1 copula) — NOT the recorded stub `fair_cc`, which is
    # only the independence product of the selected-side marginals (stub.py) and
    # so ignores every correlation the shipped engine prices. Taker-facing ask
    # on the side the taker took: YES ask = fair + width/2, NO ask =
    # (1 - fair) + width/2 (maker fee $0, caps/skew ignored; the half-width is
    # still the recorded observe-mode shadow width). We'd have WON the auction
    # when our ask beats the executed price; "undercut" = how far below the
    # clearing price our quote sat. (Edge-if-won measured against our own fair is
    # definitionally the half-width once the ask is built from that fair, so it
    # is not reported; the winner's-curse caveat is that we win selectively when
    # our fair sits low.)
    print("\n=== our shadow quote (engine fair) vs actual winning quote ===")

    def competitiveness(subset: list) -> None:
        if not subset:
            return
        won, margins = 0, []
        for r in subset:
            s: Sample = r[0]
            engine_fair = r[3] if r[4] else r[2]  # structural if applied, else copula
            if s.taker_side == "yes":
                our_ask = engine_fair + s.our_half_width
                winner = s.trade_price
            else:
                our_ask = (1.0 - engine_fair) + s.our_half_width
                winner = 1.0 - s.trade_price
            if our_ask < winner - 1e-9:
                won += 1
                margins.append(winner - our_ask)
        n = len(subset)
        print(
            f"  would-have-won {won}/{n} ({won/n*100:.0f}%)"
            + (f"  undercut(mean)={statistics.mean(margins)*100:.2f}c" if margins else "")
        )

    for sport in sorted({r[0].sport for r in rows}):
        subset = [r for r in rows if r[0].sport == sport]
        print(f"sport: {sport} (n={len(subset)})")
        competitiveness(subset)
    print("ALL")
    competitiveness(rows)


if __name__ == "__main__":
    main()
