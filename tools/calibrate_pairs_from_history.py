"""Calibrate SGP pair correlations from historical match results.

For each event pair (A, B) we observe P(A), P(B), P(A∩B) across thousands of
matches, then solve for the Gaussian-copula rho that reproduces P(A∩B) using
the SAME copula the pricer runs — so the output is directly a `pair_rho`
config value, not a statistic needing translation.

Data: football-data.co.uk CSVs in data/history/ (FTHG/FTAG goals, FTR result,
HC/AC corners). Run: uv run python tools/calibrate_pairs_from_history.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
    """The rho making copula(p_a, p_b; rho) == p_ab (monotone ⇒ bisection)."""

    def joint(rho: float) -> float:
        corr = np.array([[1.0, rho], [rho, 1.0]])
        return gaussian_copula_joint_prob([p_a, p_b], corr)

    lo, hi = -0.99, 0.99
    if p_ab <= joint(lo):
        return lo
    if p_ab >= joint(hi):
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if joint(mid) < p_ab:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def load_matches() -> list[dict[str, bool | None]]:
    matches: list[dict[str, bool | None]] = []
    for path in sorted(HISTORY.glob("*.csv")):
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    home_goals = int(row["FTHG"])
                    away_goals = int(row["FTAG"])
                    result = row["FTR"].strip()
                except (KeyError, ValueError, AttributeError):
                    continue
                total = home_goals + away_goals
                corners: bool | None = None
                try:
                    corners = int(row["HC"]) + int(row["AC"]) >= 10
                except (KeyError, ValueError, TypeError):
                    corners = None
                matches.append(
                    {
                        "home_win": result == "H",
                        "away_win": result == "A",
                        "over25": total >= 3,
                        "over35": total >= 4,
                        "btts": home_goals >= 1 and away_goals >= 1,
                        "corners95": corners,
                    }
                )
    return matches


def measure(
    matches: list[dict[str, bool | None]], a: str, b: str
) -> tuple[int, float, float, float, float]:
    rows = [m for m in matches if m[a] is not None and m[b] is not None]
    n = len(rows)
    p_a = sum(1 for m in rows if m[a]) / n
    p_b = sum(1 for m in rows if m[b]) / n
    p_ab = sum(1 for m in rows if m[a] and m[b]) / n
    return n, p_a, p_b, p_ab, implied_rho(p_a, p_b, p_ab)


PAIRS = [
    ("ml|total  (home win × over2.5)", "home_win", "over25"),
    ("ml|total  (away win × over2.5)", "away_win", "over25"),
    ("btts|total (btts × over2.5)", "btts", "over25"),
    ("btts|ml   (btts × home win)", "btts", "home_win"),
    ("btts|ml   (btts × away win)", "btts", "away_win"),
    ("total|total (over2.5 × over3.5)", "over25", "over35"),
    ("corners|total (corners>=10 x over2.5)", "corners95", "over25"),
    ("btts|corners", "btts", "corners95"),
    ("ml|ml SAME GAME (home win × away win)", "home_win", "away_win"),
]


def main() -> None:
    matches = load_matches()
    print(f"matches loaded: {len(matches)}\n")
    print(f"{'pair':44} {'n':>6} {'P(A)':>7} {'P(B)':>7} {'P(AB)':>8} {'rho':>8}")
    for label, a, b in PAIRS:
        n, p_a, p_b, p_ab, rho = measure(matches, a, b)
        print(f"{label:44} {n:>6} {p_a:>7.3f} {p_b:>7.3f} {p_ab:>8.3f} {rho:>8.3f}")


if __name__ == "__main__":
    main()
