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


def load_nfl() -> list[dict[str, bool | None]]:
    """nflverse games.csv: scores + Vegas closing lines + overtime flag.
    Over/under measured RELATIVE TO THE MARKET LINE (removes era drift);
    pushes are excluded (None)."""
    games: list[dict[str, bool | None]] = []
    with open(HISTORY / "nfl_games.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                home = int(row["home_score"])
                away = int(row["away_score"])
                total = home + away
                margin = home - away
            except (KeyError, ValueError):
                continue
            over: bool | None = None
            home_cover: bool | None = None
            try:
                line = float(row["total_line"])
                over = None if total == line else total > line
            except (KeyError, ValueError, TypeError):
                pass
            try:
                spread = float(row["spread_line"])  # positive = home favored
                home_cover = None if margin == spread else margin > spread
            except (KeyError, ValueError, TypeError):
                pass
            games.append(
                {
                    "home_win": margin > 0,
                    "away_win": margin < 0,
                    "over": over,
                    "home_cover": home_cover,
                    "overtime": row.get("overtime") in ("1", "True", "TRUE"),
                }
            )
    return games


def load_nba() -> list[dict[str, bool | None]]:
    """538 nbaallelo.csv (1946-2015, team-game rows). Seasons >= 2000 only;
    over = total points above that season's median (self-normalizing)."""
    rows: list[tuple[int, int, int, bool]] = []  # (season, total, margin>0 home?, home_win)
    with open(HISTORY / "nba_elo.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("_iscopy") != "0" or row.get("game_location") != "H":
                continue
            try:
                season = int(row["year_id"])
                pts = int(row["pts"])
                opp = int(row["opp_pts"])
            except (KeyError, ValueError):
                continue
            if season < 2000:
                continue
            rows.append((season, pts + opp, pts - opp, row.get("game_result") == "W"))
    medians: dict[int, float] = {}
    for season in {r[0] for r in rows}:
        totals = sorted(t for s, t, _, _ in rows if s == season)
        medians[season] = totals[len(totals) // 2]
    return [
        {
            "home_win": home_win,
            "away_win": (not home_win),
            "over": None if total == medians[season] else total > medians[season],
        }
        for season, total, _, home_win in rows
    ]


SOCCER_PAIRS = [
    ("ml|total  (home win x over2.5)", "home_win", "over25"),
    ("ml|total  (away win x over2.5)", "away_win", "over25"),
    ("btts|total (btts x over2.5)", "btts", "over25"),
    ("btts|ml   (btts x home win)", "btts", "home_win"),
    ("btts|ml   (btts x away win)", "btts", "away_win"),
    ("total|total (over2.5 x over3.5)", "over25", "over35"),
    ("corners|total (corners>=10 x over2.5)", "corners95", "over25"),
    ("btts|corners", "btts", "corners95"),
    ("ml|ml SAME GAME (home win x away win)", "home_win", "away_win"),
]

NFL_PAIRS = [
    ("ml|total (home win x over LINE)", "home_win", "over"),
    ("ml|total (away win x over LINE)", "away_win", "over"),
    ("spread|total (home cover x over)", "home_cover", "over"),
    ("ml|spread (home win x home cover)", "home_win", "home_cover"),
    ("extras|total (overtime x over)", "overtime", "over"),
    ("ml|ml SAME GAME", "home_win", "away_win"),
]

NBA_PAIRS = [
    ("ml|total (home win x over median)", "home_win", "over"),
    ("ml|total (away win x over median)", "away_win", "over"),
    ("ml|ml SAME GAME", "home_win", "away_win"),
]


def report(title: str, matches: list[dict[str, bool | None]], pairs: list) -> None:
    print(f"\n=== {title}: {len(matches)} games ===")
    print(f"{'pair':44} {'n':>6} {'P(A)':>7} {'P(B)':>7} {'P(AB)':>8} {'rho':>8}")
    for label, a, b in pairs:
        n, p_a, p_b, p_ab, rho = measure(matches, a, b)
        print(f"{label:44} {n:>6} {p_a:>7.3f} {p_b:>7.3f} {p_ab:>8.3f} {rho:>8.3f}")


def main() -> None:
    report("SOCCER (top-5 EU, 20/21-24/25)", load_matches(), SOCCER_PAIRS)
    report("NFL (nflverse, vs Vegas lines)", load_nfl(), NFL_PAIRS)
    report("NBA (538, seasons 2000-2015)", load_nba(), NBA_PAIRS)


if __name__ == "__main__":
    main()
