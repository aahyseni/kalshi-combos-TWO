"""OOS gate: margin/total bivariate normal vs v1 copula vs independence (NFL).

NFL is the only margin/total sport with closing lines AND moneylines in our
local history (nflverse), so it gates the MODEL; NBA/WNBA share the geometry
with per-sport calibrated shapes but stay disabled until an odds source (or
our own prod-shadow settlements) can gate them.

Procedure:
  1. TRAIN (seasons 2015-2023): calibrate sigma_margin / sigma_total / rho
     from closing-line residuals — train seasons only.
  2. TEST (2024-2025, the two most recent completed seasons): per game,
     mu_M = sigma_margin * Phi^-1(devigged home ML prob), mu_T = total_line.
     Joint log-loss per game for three dependence models on
       - pair hw x over       (marginals: devigged ML, 0.5 at the line)
       - pair hw x cover      (cover marginal = structural-implied, SAME for
                               all models — marginal parity)
       - triple hw x cover x over  (6 cells; the SGP maker's actual object)
     v1 copula uses the SHIPPED nfl table: ml|total 0.00, ml|spread 0.88,
     spread|total 0.03.

Gate: structural must beat the v1 copula on ALL metrics to put "nfl" into
margin_total.enabled_sports.

Run:  uv run python tools/validate_margin_total_oos.py
"""

from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.stats import norm

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import Team
from combomaker.pricing.margin_total import (
    GameTotalOver,
    SportShape,
    SpreadCover,
    TeamWins,
    region_probability,
)

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"
TRAIN_SEASONS = range(2015, 2024)
TEST_SEASONS = range(2024, 2026)

RHO_ML_OVER = 0.00     # shipped nfl ml|total
RHO_ML_SPREAD = 0.88   # shipped nfl ml|spread
RHO_SPREAD_OVER = 0.03  # shipped nfl spread|total


def american_prob(ml: float) -> float:
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


@dataclass(frozen=True, slots=True)
class Game:
    p_home: float
    spread: float       # closing home spread_line (home favored if > 0)
    total_line: float
    margin: float
    total: float
    season: int


def load_games() -> list[Game]:
    games: list[Game] = []
    with open(HISTORY / "nfl_games.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                season = int(row["season"])
                hs, as_ = float(row["home_score"]), float(row["away_score"])
                spread = float(row["spread_line"])
                total_line = float(row["total_line"])
                hml = float(row["home_moneyline"])
                aml = float(row["away_moneyline"])
            except (KeyError, ValueError, TypeError):
                continue
            ph, pa = american_prob(hml), american_prob(aml)
            games.append(
                Game(
                    p_home=ph / (ph + pa),
                    spread=spread,
                    total_line=total_line,
                    margin=hs - as_,
                    total=hs + as_,
                    season=season,
                )
            )
    return games


def calibrate(train: list[Game]) -> SportShape:
    dm = np.array([g.margin - g.spread for g in train])
    dt = np.array([g.total - g.total_line for g in train])
    return SportShape(
        sigma_margin=float(np.std(dm)),
        sigma_total=float(np.std(dt)),
        rho=float(np.corrcoef(dm, dt)[0, 1]),
    )


def cell_ll2(pa: float, pb: float, ab: float, a: bool, b: bool) -> float:
    cells = {
        (True, True): ab,
        (True, False): pa - ab,
        (False, True): pb - ab,
        (False, False): 1.0 - pa - pb + ab,
    }
    return math.log(max(cells[(a, b)], 1e-12))


def copula_pair(pa: float, pb: float, rho: float) -> float:
    return gaussian_copula_joint_prob([pa, pb], np.array([[1.0, rho], [rho, 1.0]]))


def copula_cell3(
    marginals: tuple[float, float, float],
    corr: np.ndarray,
    signs: tuple[bool, bool, bool],
) -> float:
    m = [p if s else 1.0 - p for p, s in zip(marginals, signs, strict=True)]
    flip = np.array([1.0 if s else -1.0 for s in signs])
    return gaussian_copula_joint_prob(m, corr * np.outer(flip, flip))


def evaluate(test: list[Game], shape: SportShape) -> dict[str, dict[str, float]]:
    corr3 = np.array(
        [
            [1.0, RHO_ML_SPREAD, RHO_ML_OVER],
            [RHO_ML_SPREAD, 1.0, RHO_SPREAD_OVER],
            [RHO_ML_OVER, RHO_SPREAD_OVER, 1.0],
        ]
    )
    sums = {
        "pair hw x over": {"independence": 0.0, "v1 copula": 0.0, "structural": 0.0},
        "pair hw x cover": {"independence": 0.0, "v1 copula": 0.0, "structural": 0.0},
        "triple hw x cover x over": {
            "independence": 0.0, "v1 copula": 0.0, "structural": 0.0
        },
    }
    n = skipped = 0
    for g in test:
        if g.margin == g.spread or g.total == g.total_line:
            skipped += 1  # push on either line: settlement semantics differ
            continue
        n += 1
        hw, cover, over = g.margin > 0, g.margin > g.spread, g.total > g.total_line
        mu_m = shape.sigma_margin * float(norm.ppf(g.p_home))
        mu_t = g.total_line
        p_hw = g.p_home
        p_over = 0.5
        # cover marginal: structural-implied, shared by all models (parity)
        p_cover = region_probability(
            mu_m, mu_t, shape, [(SpreadCover(Team.A, g.spread), True)]
        )
        p_cover = min(0.999, max(0.001, p_cover))

        # pair hw x over
        ab_s = region_probability(
            mu_m, mu_t, shape,
            [(TeamWins(Team.A), True), (GameTotalOver(g.total_line), True)],
        )
        sums["pair hw x over"]["independence"] += cell_ll2(
            p_hw, p_over, p_hw * p_over, hw, over
        )
        sums["pair hw x over"]["v1 copula"] += cell_ll2(
            p_hw, p_over, copula_pair(p_hw, p_over, RHO_ML_OVER), hw, over
        )
        sums["pair hw x over"]["structural"] += cell_ll2(
            p_hw, p_over, min(min(p_hw, p_over), ab_s), hw, over
        )

        # pair hw x cover
        ac_s = region_probability(
            mu_m, mu_t, shape,
            [(TeamWins(Team.A), True), (SpreadCover(Team.A, g.spread), True)],
        )
        sums["pair hw x cover"]["independence"] += cell_ll2(
            p_hw, p_cover, p_hw * p_cover, hw, cover
        )
        sums["pair hw x cover"]["v1 copula"] += cell_ll2(
            p_hw, p_cover, copula_pair(p_hw, p_cover, RHO_ML_SPREAD), hw, cover
        )
        sums["pair hw x cover"]["structural"] += cell_ll2(
            p_hw, p_cover, min(min(p_hw, p_cover), ac_s), hw, cover
        )

        # triple (6 valid cells; independence/copula may leak onto the
        # impossible ones — that's their deserved loss)
        observed = (hw, cover, over)
        marg = (p_hw, p_cover, p_over)
        p_ind = math.prod(p if s else 1 - p for p, s in zip(marg, observed, strict=True))
        sums["triple hw x cover x over"]["independence"] += math.log(max(p_ind, 1e-12))
        sums["triple hw x cover x over"]["v1 copula"] += math.log(
            max(copula_cell3(marg, corr3, observed), 1e-12)
        )
        p_struct = region_probability(
            mu_m, mu_t, shape,
            [
                (TeamWins(Team.A), observed[0]),
                (SpreadCover(Team.A, g.spread), observed[1]),
                (GameTotalOver(g.total_line), observed[2]),
            ],
        )
        sums["triple hw x cover x over"]["structural"] += math.log(max(p_struct, 1e-12))

    print(f"  test n={n} ({skipped} pushes excluded)")
    return {
        metric: {model: -ll / n for model, ll in models.items()}
        for metric, models in sums.items()
    }


def main() -> None:
    games = load_games()
    train = [g for g in games if g.season in TRAIN_SEASONS]
    test = [g for g in games if g.season in TEST_SEASONS]
    print(f"games: train {len(train)} (2015-2023) / test {len(test)} (2024-2025)")
    shape = calibrate(train)
    print(
        f"train shape: sigma_M={shape.sigma_margin:.2f} "
        f"sigma_T={shape.sigma_total:.2f} rho={shape.rho:+.3f}"
    )

    print("\nOOS joint log-loss per game (LOWER is better):")
    results = evaluate(test, shape)
    gate_pass = True
    for metric, models in results.items():
        line = "  ".join(f"{m}={v:.5f}" for m, v in models.items())
        beats = models["structural"] < models["v1 copula"]
        gate_pass = gate_pass and beats
        print(f"  {metric:26s}: {line}   "
              f"{'structural BEATS v1' if beats else 'structural does NOT beat v1'}")

    print(
        "\nGATE: "
        + (
            'PASS — put "nfl" into margin_total.enabled_sports with this '
            "evidence in NOTES.md"
            if gate_pass
            else "FAIL — nfl stays disabled (directive point 4)"
        )
    )
    sys.exit(0 if gate_pass else 1)


if __name__ == "__main__":
    main()
