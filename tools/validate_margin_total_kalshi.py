"""Current-era WNBA/NBA check on Kalshi's OWN prices (settled markets).

Same (margin, total) bivariate-normal model that ships in
pricing/margin_total.py, evaluated on pre-game mids from Kalshi's settled
KX{WNBA,NBA}{GAME,TOTAL,SPREAD} markets (data/history/kalshi_{sport}_*.csv
via fetch_kalshi_history.py) — the exact venue and era we quote. Metrics:
team-win x main-total pair on every game, plus team-win x spread-cover
pair and the triple where a main spread line was captured.

Faithful to production BY CONSTRUCTION: teams are resolved with the shipped
adapter's ``_parse_match``/``_team_of`` (Team.A = game-code blob prefix =
away), and the shape is built with ``shape_in_leg_frame`` — the SAME leg
frame the production pricer uses — so the reported structural score is the
one the live pricer would earn, not a re-implementation that can silently
drift (the earlier version pinned Team.A to whichever moneyline market the
fetcher listed first, a coin flip that made the razor-thin win-over metric a
frame artifact).

Out-of-sample: shapes are the SHIPPED per-sport calibrations (score data
only, no Kalshi prices; means inverted per game exactly as production does).
v1 copula uses the SHIPPED config exactly: ml|total 0.01 for both sports;
ml|spread and spread|total have no calibrated entry and fall back to the flat
same-event prior 0.6.

Context: WNBA is ENABLED on operator request (NFL-gated geometry) — this
is its first native-venue evidence; NBA is gated OFF, and the settled
listing only reaches back ~2 months, so NBA rows are June playoff games
(small n, playoff-only sample — directional evidence, not a gate).

Run:  uv run python tools/validate_margin_total_kalshi.py --sport wnba
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import StructuralError
from combomaker.pricing.margin_total import (
    GameTotalOver,
    SportShape,
    SpreadCover,
    TeamWins,
    invert_means,
    region_probability,
    shape_in_leg_frame,
)
from combomaker.pricing.structural import _parse_match, _team_of

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"
# CALIBRATION-frame (M = home - away) shapes, exactly as ops/config.py stores
# them; converted to the production leg frame via shape_in_leg_frame below.
SHAPES = {
    "wnba": SportShape(sigma_margin=12.04, sigma_total=16.55, rho=-0.019),
    "nba": SportShape(sigma_margin=13.71, sigma_total=18.42, rho=0.000),
}
RHO_ML_OVER = 0.01       # shipped v1 ml|total (both sports)
RHO_FLAT = 0.6           # shipped same_event_rho: what v1 uses for uncal. pairs

MODELS = ("independence", "v1 copula", "structural")


def cell_ll2(pa: float, pb: float, ab: float, a: bool, b: bool) -> float:
    ab = min(min(pa, pb), max(ab, max(0.0, pa + pb - 1.0)))
    cells = {
        (True, True): ab,
        (True, False): pa - ab,
        (False, True): pb - ab,
        (False, False): 1.0 - pa - pb + ab,
    }
    return math.log(max(cells[(a, b)], 1e-12))


def copula_pair(pa: float, pb: float, rho: float) -> float:
    return gaussian_copula_joint_prob([pa, pb], np.array([[1.0, rho], [rho, 1.0]]))


def copula_cell3(marg: tuple[float, float, float], corr: np.ndarray,
                 signs: tuple[bool, bool, bool]) -> float:
    m = [p if s else 1.0 - p for p, s in zip(marg, signs, strict=True)]
    flip = np.array([1.0 if s else -1.0 for s in signs])
    return gaussian_copula_joint_prob(m, corr * np.outer(flip, flip))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=list(SHAPES), required=True)
    args = parser.parse_args()
    # Leg frame (Team.A = blob prefix = away): the exact shape production prices
    # with. Any calibration-frame rho is negated here, in one place.
    cal = SHAPES[args.sport]
    shape = shape_in_leg_frame(cal.sigma_margin, cal.sigma_total, cal.rho)

    spreads_path = HISTORY / f"kalshi_{args.sport}_spreads.csv"
    spreads: dict[str, dict[str, str]] = {}
    if spreads_path.exists():
        with open(spreads_path, encoding="utf-8", newline="") as f:
            spreads = {row["game_code"]: row for row in csv.DictReader(f)}

    corr3 = np.array(
        [
            [1.0, RHO_FLAT, RHO_ML_OVER],
            [RHO_FLAT, 1.0, RHO_FLAT],
            [RHO_ML_OVER, RHO_FLAT, 1.0],
        ]
    )
    sums = {
        "pair team-win x over": dict.fromkeys(MODELS, 0.0),
        "pair team-win x cover": dict.fromkeys(MODELS, 0.0),
        "triple win x cover x over": dict.fromkeys(MODELS, 0.0),
    }
    counts = dict.fromkeys(sums, 0)
    skipped = 0
    with open(
        HISTORY / f"kalshi_{args.sport}_history.csv", encoding="utf-8", newline=""
    ) as f:
        for row in csv.DictReader(f):
            p_team = float(row["p_team_close"])
            p_over = float(row["p_over_close"])
            won = row["team_won"] == "1"
            over = row["went_over"] == "1"
            line = float(row["total_line"])
            if not (0.02 < p_team < 0.98 and 0.02 < p_over < 0.98):
                skipped += 1
                continue
            # Resolve the moneyline team into the production leg frame; the
            # game-code blob prefix is Team.A (away), suffix is Team.B (home).
            match = _parse_match(row["game_code"])
            ml_team = _team_of(row["team"], match) if match is not None else None
            if ml_team is None:
                skipped += 1
                continue
            try:
                inv = invert_means(
                    [(TeamWins(ml_team), p_team), (GameTotalOver(line), p_over)],
                    shape,
                )
            except StructuralError:
                skipped += 1
                continue

            def joint(*legs: tuple, _inv=inv) -> float:  # type: ignore[type-arg, no-untyped-def]
                return region_probability(_inv.mu_m, _inv.mu_t, shape, list(legs))

            counts["pair team-win x over"] += 1
            sums["pair team-win x over"]["independence"] += cell_ll2(
                p_team, p_over, p_team * p_over, won, over
            )
            sums["pair team-win x over"]["v1 copula"] += cell_ll2(
                p_team, p_over, copula_pair(p_team, p_over, RHO_ML_OVER), won, over
            )
            sums["pair team-win x over"]["structural"] += cell_ll2(
                p_team, p_over,
                joint((TeamWins(ml_team), True), (GameTotalOver(line), True)),
                won, over,
            )

            sp = spreads.get(row["game_code"])
            if sp is None:
                continue
            sp_team = _team_of(sp["spread_team"], match)
            p_cover = float(sp["p_spread_close"])
            if sp_team is None or not (0.02 < p_cover < 0.98):
                continue
            covered = sp["covered"] == "1"
            cover_spec = SpreadCover(sp_team, float(sp["spread_line"]))

            counts["pair team-win x cover"] += 1
            sums["pair team-win x cover"]["independence"] += cell_ll2(
                p_team, p_cover, p_team * p_cover, won, covered
            )
            sums["pair team-win x cover"]["v1 copula"] += cell_ll2(
                p_team, p_cover, copula_pair(p_team, p_cover, RHO_FLAT), won, covered
            )
            sums["pair team-win x cover"]["structural"] += cell_ll2(
                p_team, p_cover,
                joint((TeamWins(ml_team), True), (cover_spec, True)), won, covered,
            )

            counts["triple win x cover x over"] += 1
            observed = (won, covered, over)
            marg = (p_team, p_cover, p_over)
            p_ind = math.prod(
                p if s else 1 - p for p, s in zip(marg, observed, strict=True)
            )
            sums["triple win x cover x over"]["independence"] += math.log(
                max(p_ind, 1e-12)
            )
            sums["triple win x cover x over"]["v1 copula"] += math.log(
                max(copula_cell3(marg, corr3, observed), 1e-12)
            )
            p_struct = joint(
                (TeamWins(ml_team), observed[0]),
                (cover_spec, observed[1]),
                (GameTotalOver(line), observed[2]),
            )
            sums["triple win x cover x over"]["structural"] += math.log(
                max(p_struct, 1e-12)
            )

    print(f"Kalshi-native {args.sport.upper()} games ({skipped} skipped)")
    # team-win x over has NO discriminating power in these sports (corr(win,over)
    # ~0; empirically confirmed |z|<2 for NFL/NBA/WNBA/MLB) — print it as a
    # DIAGNOSTIC but never let its coin-flip result gate the verdict (Decision A,
    # 2026-07-06). The decisive metrics are spread-cover and the triple, which
    # carry the real dependence signal; missing THOSE fails closed.
    gate_pass = True
    complete = True
    for metric, models in sums.items():
        n = counts[metric]
        diagnostic = metric == "pair team-win x over"
        if n == 0:
            if diagnostic:
                print(f"  {metric:26s}: no data (diagnostic)")
            else:
                print(f"  {metric:26s}: NO DATA — gate INCOMPLETE (fail-closed)")
                complete = False
            continue
        scores = {m: -ll / n for m, ll in models.items()}
        beats = scores["structural"] < scores["v1 copula"]
        line_s = "  ".join(f"{m}={v:.5f}" for m, v in scores.items())
        if diagnostic:
            tag = "diagnostic — no discriminating power, not gated"
        else:
            gate_pass = gate_pass and beats
            tag = "structural BEATS v1" if beats else "structural does NOT beat v1"
        print(f"  {metric:26s} (n={n}): {line_s}   {tag}")
    verdict = gate_pass and complete
    head = ("structural BEATS" if verdict
            else "structural gate INCOMPLETE for" if not complete
            else "structural does NOT beat")
    print(f"{head} v1 on Kalshi-era {args.sport.upper()} data (gated on cover+triple)")
    sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
