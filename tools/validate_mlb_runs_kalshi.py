"""Current-era MLB gate on Kalshi's OWN prices (2025-2026 settled markets).

Complements the SBR-archive gate (test=2021): same NegBin runs model, but
marginals are pre-game mids from Kalshi's settled KXMLBGAME/KXMLBTOTAL/
KXMLBSPREAD markets (data/history/kalshi_mlb_{history,spreads}.csv via
fetch_kalshi_history.py) — the exact venue and era we quote. Metrics:
team-win x main-total pair on every game, plus team-win x spread-cover
pair and the triple where a main spread line was captured.

Entirely out-of-sample: k is Retrosheet-fitted (scores only, no prices) and
the model has never seen these games' prices. v1 copula uses the SHIPPED
config exactly: mlb ml|total -0.05; ml|spread and spread|total have no
calibrated entry and fall back to the flat same-event prior 0.6.

Run:  uv run python tools/validate_mlb_runs_kalshi.py
"""

from __future__ import annotations

import csv
import math
import re
import sys
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import StructuralError, Team
from combomaker.pricing.margin_total import GameTotalOver, SpreadCover, TeamWins
from combomaker.pricing.mlb_runs import MlbShape, invert_runs, joint_probability

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"
K = 3.54                 # Retrosheet 2021-2025 (fitted on scores, not prices)
RHO_ML_OVER = -0.05      # shipped v1 mlb ml|total
RHO_FLAT = 0.6           # shipped same_event_rho: what v1 uses for uncal. pairs

_CODE = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?([A-Z0-9]+)$")
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


def spread_team_side(game_code: str, game_team: str, spread_team: str) -> Team | None:
    """Team frame: Team.A == the game-market team. None = unresolvable."""
    if spread_team == game_team:
        return Team.A
    m = _CODE.match(game_code)
    if m is None:
        return None
    pair = m.group(5)
    if pair in (spread_team + game_team, game_team + spread_team):
        return Team.B
    return None


def load_spreads() -> dict[str, dict[str, str]]:
    path = HISTORY / "kalshi_mlb_spreads.csv"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        return {row["game_code"]: row for row in csv.DictReader(f)}


def main() -> None:
    shape = MlbShape(dispersion_k=K)
    spreads = load_spreads()
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
    with open(HISTORY / "kalshi_mlb_history.csv", encoding="utf-8", newline="") as f:
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

            def joint(*legs: tuple, _inv=inv) -> float:  # type: ignore[type-arg, no-untyped-def]
                return joint_probability(_inv.mu_a, _inv.mu_b, shape, list(legs))

            counts["pair team-win x over"] += 1
            sums["pair team-win x over"]["independence"] += cell_ll2(
                p_team, p_over, p_team * p_over, won, over
            )
            sums["pair team-win x over"]["v1 copula"] += cell_ll2(
                p_team, p_over, copula_pair(p_team, p_over, RHO_ML_OVER), won, over
            )
            sums["pair team-win x over"]["structural"] += cell_ll2(
                p_team, p_over,
                joint((TeamWins(Team.A), True), (GameTotalOver(line), True)),
                won, over,
            )

            sp = spreads.get(row["game_code"])
            if sp is None:
                continue
            side = spread_team_side(row["game_code"], row["team"], sp["spread_team"])
            p_cover = float(sp["p_spread_close"])
            if side is None or not (0.02 < p_cover < 0.98):
                continue
            covered = sp["covered"] == "1"
            cover_spec = SpreadCover(side, float(sp["spread_line"]))

            counts["pair team-win x cover"] += 1
            sums["pair team-win x cover"]["independence"] += cell_ll2(
                p_team, p_cover, p_team * p_cover, won, covered
            )
            sums["pair team-win x cover"]["v1 copula"] += cell_ll2(
                p_team, p_cover, copula_pair(p_team, p_cover, RHO_FLAT), won, covered
            )
            sums["pair team-win x cover"]["structural"] += cell_ll2(
                p_team, p_cover,
                joint((TeamWins(Team.A), True), (cover_spec, True)), won, covered,
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
                (TeamWins(Team.A), observed[0]),
                (cover_spec, observed[1]),
                (GameTotalOver(line), observed[2]),
            )
            sums["triple win x cover x over"]["structural"] += math.log(
                max(p_struct, 1e-12)
            )

    print(f"Kalshi-native MLB games ({skipped} skipped)")
    # team-win x over has NO discriminating power (MLB ml x total ~0; confirmed
    # tie, z=+0.43) — print it as a DIAGNOSTIC, never let its coin flip gate the
    # verdict (Decision A, 2026-07-06). Decisive metrics = run-line cover + triple;
    # missing THOSE fails closed.
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
    print(f"{head} v1 on Kalshi-era MLB data (gated on cover+triple)")
    sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
