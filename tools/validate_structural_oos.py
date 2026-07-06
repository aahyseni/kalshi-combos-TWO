"""OOS gate: Dixon-Coles structural pricer vs v1 copula vs independence.

Directive point 4: a dependence model that does not beat the incumbent out of
sample is noise and must not ship. The incumbent here is the SHIPPED v1
copula (soccer table: ml|total +0.28, btts|total +0.70, btts|ml oriented
fav -0.19 / dog 0.00) — a higher bar than independence.

Procedure (club soccer, football-data.co.uk CSVs already under data/history):
  1. Per game, devig closing 1X2 (home/draw) + O/U 2.5 into market marginals.
  2. TRAIN (seasons < 2024): invert the production DC model per game from
     (p_home, p_draw, p_over) — the same invert() the pricer runs — and fit
     dc_rho by scoreline log-likelihood over a grid.
  3. TEST (2023/24 + 24/25 seasons, never touched by the fit): score joint
     log-loss per game for three dependence models on
       - PAIR home-win x over2.5   (both marginals from market odds)
       - PAIR home-win x btts      (btts marginal = DC-implied, SAME for all
                                    models — marginal parity isolates the
                                    dependence structure)
       - TRIPLE hw x over x btts   (8-cell; what a 3-leg SGP maker quotes)
     Structural pair joints are clamped to the Frechet bounds of the shared
     marginals so no model gains from marginal disagreement; the triple uses
     each model's own coherent cells (noted caveat: structural marginals
     carry its inversion misfit).

Gate: structural must beat the v1 copula on ALL THREE metrics to flip
`structural.enabled`. Results recorded in NOTES.md either way.

Run:  uv run python tools/validate_structural_oos.py
"""

from __future__ import annotations

import csv
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import (
    Btts,
    Draw,
    MatchFormat,
    ModelParams,
    StructuralError,
    Team,
    TeamWin,
    TotalOver,
    invert,
    joint_probability,
)

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"
TRAIN_BEFORE = 2024
MAX_GOALS = 10
DC_RHO_GRID = (0.00, -0.05, -0.10, -0.15)

# The SHIPPED v1 soccer pair table (ops/config.py) — the incumbent to beat.
RHO_ML_OVER = 0.28
RHO_BTTS_OVER = 0.70
RHO_BTTS_ML_FAV = -0.19
RHO_BTTS_ML_DOG = 0.00


def oriented_btts_ml(p_home: float) -> float:
    w = min(1.0, max(0.0, (p_home - 0.45) / 0.10))
    return RHO_BTTS_ML_DOG + w * (RHO_BTTS_ML_FAV - RHO_BTTS_ML_DOG)


# ------------------------------------------------------------------ data


@dataclass(frozen=True, slots=True)
class Game:
    p_home: float
    p_draw: float
    p_over: float
    home_goals: int
    away_goals: int
    season: int

    @property
    def hw(self) -> bool:
        return self.home_goals > self.away_goals

    @property
    def over(self) -> bool:
        return self.home_goals + self.away_goals >= 3

    @property
    def btts(self) -> bool:
        return self.home_goals >= 1 and self.away_goals >= 1


def load_games() -> list[Game]:
    games: list[Game] = []
    for path in sorted(HISTORY.glob("*-2*.csv")):
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    hg, ag = int(row["FTHG"]), int(row["FTAG"])
                    oh = float(row.get("B365H") or row.get("AvgH") or 0)
                    od = float(row.get("B365D") or row.get("AvgD") or 0)
                    oa = float(row.get("B365A") or row.get("AvgA") or 0)
                    oo = float(row.get("B365>2.5") or row.get("Avg>2.5") or 0)
                    ou = float(row.get("B365<2.5") or row.get("Avg<2.5") or 0)
                    season = int("20" + path.stem.split("-")[1][:2])
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
                if min(oh, od, oa, oo, ou) <= 1.0:
                    continue
                ih, id_, ia = 1 / oh, 1 / od, 1 / oa
                s3 = ih + id_ + ia
                io, iu = 1 / oo, 1 / ou
                games.append(
                    Game(
                        p_home=ih / s3,
                        p_draw=id_ / s3,
                        p_over=io / (io + iu),
                        home_goals=hg,
                        away_goals=ag,
                        season=season,
                    )
                )
    return games


# ------------------------------------------------------------------ model


def invert_game(g: Game, dc_rho: float) -> ModelParams | None:
    legs = [
        (TeamWin(Team.A, include_et=False), g.p_home),
        (Draw(), g.p_draw),
        (TotalOver(3, include_et=False), g.p_over),
    ]
    try:
        model = invert(
            legs,
            dc_rho=dc_rho,
            et_factor=1.0 / 3.0,
            match_format=MatchFormat.GROUP,
            max_goals=MAX_GOALS,
        )
    except StructuralError:
        return None
    return model.params


def scoreline_loglik(params: ModelParams, g: Game) -> float:
    from combomaker.pricing.dixon_coles import _dc_grid  # tool-only seam

    grid = _dc_grid(params.lam_a, params.lam_b, params.dc_rho, params.max_goals)
    h = min(g.home_goals, params.max_goals)
    a = min(g.away_goals, params.max_goals)
    return math.log(max(grid[h, a], 1e-12))


def fit_dc_rho(train: list[Game]) -> float:
    best_rho, best_ll = 0.0, -math.inf
    for rho in DC_RHO_GRID:
        ll, n = 0.0, 0
        t0 = time.perf_counter()
        for g in train:
            params = invert_game(g, rho)
            if params is None:
                continue
            ll += scoreline_loglik(params, g)
            n += 1
        print(
            f"  dc_rho={rho:+.2f}: train scoreline loglik/game = {ll / n:.5f} "
            f"(n={n}, {time.perf_counter() - t0:.0f}s)"
        )
        if ll > best_ll:
            best_rho, best_ll = rho, ll
    return best_rho


# ------------------------------------------------------------------ scoring


def clamp_frechet2(p_joint: float, pa: float, pb: float) -> float:
    lo = max(0.0, pa + pb - 1.0)
    hi = min(pa, pb)
    return min(hi, max(lo, p_joint))


def cell_ll2(pa: float, pb: float, ab: float, a: bool, b: bool) -> float:
    """Log-lik of the observed 2-event cell given marginals + joint."""
    cells = {
        (True, True): ab,
        (True, False): pa - ab,
        (False, True): pb - ab,
        (False, False): 1.0 - pa - pb + ab,
    }
    return math.log(max(cells[(a, b)], 1e-12))


def copula_pair(pa: float, pb: float, rho: float) -> float:
    corr = np.array([[1.0, rho], [rho, 1.0]])
    return gaussian_copula_joint_prob([pa, pb], corr)


def copula_cell3(
    marginals: tuple[float, float, float],
    corr: np.ndarray,
    signs: tuple[bool, bool, bool],
) -> float:
    """P(cell) via the v1 copula: NO sides flip marginal + conjugate corr."""
    m = [p if s else 1.0 - p for p, s in zip(marginals, signs, strict=True)]
    flip = np.array([1.0 if s else -1.0 for s in signs])
    return gaussian_copula_joint_prob(m, corr * np.outer(flip, flip))


def structural_cell3(
    params: ModelParams, signs: tuple[bool, bool, bool]
) -> float:
    legs = [
        (TeamWin(Team.A, include_et=False), signs[0]),
        (TotalOver(3, include_et=False), signs[1]),
        (Btts(include_et=False), signs[2]),
    ]
    return joint_probability(params, legs, {})


def evaluate(test: list[Game], dc_rho: float) -> dict[str, dict[str, float]]:
    sums = {
        "pair hw x over": {"independence": 0.0, "v1 copula": 0.0, "structural": 0.0},
        "pair hw x btts": {"independence": 0.0, "v1 copula": 0.0, "structural": 0.0},
        "triple hw x over x btts": {
            "independence": 0.0, "v1 copula": 0.0, "structural": 0.0
        },
    }
    n = 0
    skipped = 0
    for g in test:
        params = invert_game(g, dc_rho)
        if params is None:
            skipped += 1
            continue
        n += 1
        pa, pb = g.p_home, g.p_over
        p_btts = joint_probability(
            params, [(Btts(include_et=False), True)], {}
        )  # shared btts marginal: marginal parity across all three models

        # pair hw x over
        ab_s = clamp_frechet2(
            joint_probability(
                params,
                [(TeamWin(Team.A, include_et=False), True), (TotalOver(3, include_et=False), True)],
                {},
            ),
            pa,
            pb,
        )
        sums["pair hw x over"]["independence"] += cell_ll2(pa, pb, pa * pb, g.hw, g.over)
        sums["pair hw x over"]["v1 copula"] += cell_ll2(
            pa, pb, copula_pair(pa, pb, RHO_ML_OVER), g.hw, g.over
        )
        sums["pair hw x over"]["structural"] += cell_ll2(pa, pb, ab_s, g.hw, g.over)

        # pair hw x btts (marginal parity: p_btts from the DC inversion)
        ac_s = clamp_frechet2(
            joint_probability(
                params,
                [(TeamWin(Team.A, include_et=False), True), (Btts(include_et=False), True)],
                {},
            ),
            pa,
            p_btts,
        )
        rho_ml_btts = oriented_btts_ml(pa)
        sums["pair hw x btts"]["independence"] += cell_ll2(
            pa, p_btts, pa * p_btts, g.hw, g.btts
        )
        sums["pair hw x btts"]["v1 copula"] += cell_ll2(
            pa, p_btts, copula_pair(pa, p_btts, rho_ml_btts), g.hw, g.btts
        )
        sums["pair hw x btts"]["structural"] += cell_ll2(pa, p_btts, ac_s, g.hw, g.btts)

        # triple (8-cell). v1 corr: shipped soccer table with oriented btts|ml.
        corr = np.array(
            [
                [1.0, RHO_ML_OVER, rho_ml_btts],
                [RHO_ML_OVER, 1.0, RHO_BTTS_OVER],
                [rho_ml_btts, RHO_BTTS_OVER, 1.0],
            ]
        )
        observed = (g.hw, g.over, g.btts)
        marg = (pa, pb, p_btts)
        p_ind = math.prod(p if s else 1 - p for p, s in zip(marg, observed, strict=True))
        sums["triple hw x over x btts"]["independence"] += math.log(max(p_ind, 1e-12))
        sums["triple hw x over x btts"]["v1 copula"] += math.log(
            max(copula_cell3(marg, corr, observed), 1e-12)
        )
        sums["triple hw x over x btts"]["structural"] += math.log(
            max(structural_cell3(params, observed), 1e-12)
        )

    if skipped:
        print(f"  ({skipped} test games skipped: inversion refused)")
    return {
        metric: {model: -ll / n for model, ll in models.items()}
        for metric, models in sums.items()
    }


def main() -> None:
    games = load_games()
    train = [g for g in games if g.season < TRAIN_BEFORE]
    test = [g for g in games if g.season >= TRAIN_BEFORE]
    print(f"games: {len(games)} (train {len(train)} / test {len(test)}; "
          f"split at season {TRAIN_BEFORE})")

    print("\nfitting dc_rho on TRAIN scorelines (per-game production inversion):")
    dc_rho = fit_dc_rho(train)
    print(f"  -> fitted dc_rho = {dc_rho:+.2f} (frozen for test)")

    print("\nOOS joint log-loss per game (LOWER is better), held-out seasons:")
    results = evaluate(test, dc_rho)
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
            "PASS — structural beats the v1 copula on all metrics; "
            "flip structural.enabled=True with this evidence in NOTES.md"
            if gate_pass
            else "FAIL — structural must stay disabled (directive point 4)"
        )
    )
    sys.exit(0 if gate_pass else 1)


if __name__ == "__main__":
    main()
