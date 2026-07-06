"""Current-era MLB gate on Kalshi's OWN prices (2025-2026 settled markets).

Complements the SBR-archive gate (test=2021): same NegBin runs model, but
marginals are pre-game mids from Kalshi's settled KXMLBGAME/KXMLBTOTAL
markets (data/history/kalshi_mlb_history.csv via fetch_kalshi_mlb_history.py)
— the exact venue and era we quote. Events available: team-win x main-total
pair (run lines not captured yet; shadow settlements will add them).

Entirely out-of-sample: k is Retrosheet-fitted (scores only, no prices) and
the model has never seen these games' prices.

Run:  uv run python tools/validate_mlb_runs_kalshi.py
"""

from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import StructuralError, Team
from combomaker.pricing.margin_total import GameTotalOver, TeamWins
from combomaker.pricing.mlb_runs import MlbShape, invert_runs, joint_probability

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "history" / "kalshi_mlb_history.csv"
K = 3.54                 # Retrosheet 2021-2025 (fitted on scores, not prices)
RHO_ML_OVER = -0.05      # shipped v1 mlb ml|total


def cell_ll2(pa: float, pb: float, ab: float, a: bool, b: bool) -> float:
    ab = min(min(pa, pb), max(ab, max(0.0, pa + pb - 1.0)))
    cells = {
        (True, True): ab,
        (True, False): pa - ab,
        (False, True): pb - ab,
        (False, False): 1.0 - pa - pb + ab,
    }
    return math.log(max(cells[(a, b)], 1e-12))


def main() -> None:
    shape = MlbShape(dispersion_k=K)
    sums = dict.fromkeys(("independence", "v1 copula", "structural"), 0.0)
    n = skipped = 0
    with open(CSV_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            p_team = float(row["p_team_close"])
            p_over = float(row["p_over_close"])
            won = row["team_won"] == "1"
            over = row["went_over"] == "1"
            line = float(row["total_line"])
            if not (0.02 < p_team < 0.98 and 0.02 < p_over < 0.98):
                skipped += 1
                continue
            try:
                inv = invert_runs(
                    [(TeamWins(Team.A), p_team), (GameTotalOver(line), p_over)], shape
                )
            except StructuralError:
                skipped += 1
                continue
            n += 1
            sums["independence"] += cell_ll2(p_team, p_over, p_team * p_over, won, over)
            sums["v1 copula"] += cell_ll2(
                p_team, p_over,
                gaussian_copula_joint_prob(
                    [p_team, p_over],
                    np.array([[1.0, RHO_ML_OVER], [RHO_ML_OVER, 1.0]]),
                ),
                won, over,
            )
            sums["structural"] += cell_ll2(
                p_team, p_over,
                joint_probability(
                    inv.mu_a, inv.mu_b, shape,
                    [(TeamWins(Team.A), True), (GameTotalOver(line), True)],
                ),
                won, over,
            )

    print(f"Kalshi-native games scored: {n} ({skipped} skipped)")
    scores = {m: -ll / n for m, ll in sums.items()}
    for m, v in scores.items():
        print(f"  {m:13s} pair team-win x over logloss/game = {v:.5f}")
    beats = scores["structural"] < scores["v1 copula"]
    print("structural " + ("BEATS" if beats else "does NOT beat") + " v1 on Kalshi-era data")
    sys.exit(0 if beats else 1)


if __name__ == "__main__":
    main()
