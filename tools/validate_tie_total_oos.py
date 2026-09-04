"""Held-out calibration of the DC EXACT cell for the same-game tie x over-2.5
pair — the population the ONE structural bar (build 2026-09-04 item B) newly
QUOTES: before the build every tie x total pair whose 2-leg inversion left a
residual > 0.005 was DECLINED by the 8/15 pickoff guard (10,528 such declines in
the 8/17-8/27 tape, residual p50 0.009, max 0.044 — all under the 0.05 bar), so
they now price the DC cell with the residual in the width.

Usage:
  PYTHONPATH=src python tools/validate_tie_total_oos.py [--history data/history] [--boot 3000]

Scores, per game, the DC cell P(draw AND over 2.5) from the LIVE inverter on the
market (draw, over) marginals only — the exact system the live pricer solves —
against realized outcomes: log-loss vs independence, both-cell predicted vs
realized (z), and the same split by the OLD route (residual <= 0.005 = was
priced structurally; (0.005, 0.05] = was DECLINED, now priced). No fitted
parameter: dc_rho is the live config value, so train and held-out are both
pure out-of-sample for this cell.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_structural_oos as V  # noqa: E402

from combomaker.ops.config import StructuralConfig  # noqa: E402
from combomaker.pricing.dixon_coles import (  # noqa: E402
    Draw,
    MatchFormat,
    StructuralError,
    TotalOver,
    invert,
    joint_probability,
)
from combomaker.pricing.fit_challenge import REJECT_EXACT, classify_fit  # noqa: E402


def score(games: list[V.Game], label: str, dc_rho: float, resolution: float,
          n_boot: int, rng: np.random.Generator) -> None:
    d, t = Draw(), TotalOver(3, include_et=False)
    rows = []
    n_reject = 0
    for g in games:
        try:
            fit = invert([(d, g.p_draw), (t, g.p_over)], dc_rho=dc_rho, et_factor=1.0 / 3.0,
                         match_format=MatchFormat.GROUP, max_goals=V.MAX_GOALS)
        except StructuralError:
            n_reject += 1
            continue
        cell = V.clamp_frechet2(joint_probability(fit.params, [(d, True), (t, True)], {}),
                                g.p_draw, g.p_over)
        hit = g.home_goals == g.away_goals and g.home_goals + g.away_goals >= 3
        ind = g.p_draw * g.p_over
        rows.append((fit.residual, cell, ind, hit))
    n = len(rows)
    print(f"\n[{label}] games {len(games)}  inverted {n}  rejected(>0.05) {n_reject}")
    res = np.array([r[0] for r in rows])
    print(f"  2-leg residual p50={np.percentile(res, 50):.4f} p90={np.percentile(res, 90):.4f} "
          f"max={res.max():.4f}; verdicts@{resolution}: "
          + str({v: int((np.array([classify_fit(r, exactly_identified=True, resolution=resolution)
                                    .verdict.value for r in res]) == v).sum())
                 for v in ("accept", "challenge", "reject")}))
    for sub, mask in (("ALL", np.ones(n, bool)),
                      (f"old-priced (residual <= {REJECT_EXACT})", res <= REJECT_EXACT),
                      (f"NEWLY PRICED ({REJECT_EXACT} < residual <= 0.05)", res > REJECT_EXACT)):
        m = int(mask.sum())
        if m == 0:
            continue
        cell = np.array([r[1] for r in rows])[mask]
        ind = np.array([r[2] for r in rows])[mask]
        hit = np.array([r[3] for r in rows])[mask].astype(float)
        ll_cell = -(hit * np.log(np.clip(cell, 1e-12, 1))
                    + (1 - hit) * np.log(np.clip(1 - cell, 1e-12, 1)))
        ll_ind = -(hit * np.log(np.clip(ind, 1e-12, 1))
                   + (1 - hit) * np.log(np.clip(1 - ind, 1e-12, 1)))
        z_cell = (hit.sum() - cell.sum()) / math.sqrt((cell * (1 - cell)).sum())
        z_ind = (hit.sum() - ind.sum()) / math.sqrt((ind * (1 - ind)).sum())
        diff = ll_cell - ll_ind
        idx = rng.integers(0, m, size=(n_boot, m))
        bs = diff[idx].mean(1)
        print(f"  {sub:44s} n={m:5d} realized {hit.mean():.4f} | DC cell pred {cell.mean():.4f} "
              f"z={z_cell:+.2f} LL {ll_cell.mean():.5f} | indep pred {ind.mean():.4f} "
              f"z={z_ind:+.2f} LL {ll_ind.mean():.5f} | DC-indep {diff.mean():+.5f} "
              f"CI95 [{np.percentile(bs, 2.5):+.5f},{np.percentile(bs, 97.5):+.5f}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", type=Path, default=V.HISTORY)
    ap.add_argument("--boot", type=int, default=3000)
    ap.add_argument("--resolution", type=float, default=0.01,
                    help="summed leg-book uncertainty (two 1c-spread books = 0.01)")
    args = ap.parse_args()
    V.HISTORY = args.history
    games = V.load_games()
    dc_rho = StructuralConfig().dc_rho
    rng = np.random.default_rng(7)
    print(f"games {len(games)}  dc_rho {dc_rho}  (tie x over-2.5 DC exact cell, live inverter)")
    score([g for g in games if g.season < V.TRAIN_BEFORE], f"seasons < {V.TRAIN_BEFORE}",
          dc_rho, args.resolution, args.boot, rng)
    score([g for g in games if g.season >= V.TRAIN_BEFORE], f"held-out >= {V.TRAIN_BEFORE}",
          dc_rho, args.resolution, args.boot, rng)


if __name__ == "__main__":
    main()
