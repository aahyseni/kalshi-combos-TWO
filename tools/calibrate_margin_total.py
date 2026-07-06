"""Calibrate the bivariate-normal (margin, total) model for NFL/NBA/WNBA.

Two estimation methods:
  A. LINE-RESIDUAL (NFL only — closing spread_line/total_line in nflverse):
     sigma_margin = std(margin - spread_line), sigma_total = std(total -
     total_line), rho = corr of the residuals. This is the gold standard:
     the market line removes between-game team-strength heterogeneity.
  B. TEAM-FIXED-EFFECTS (NBA/WNBA — no lines in the box data): per season,
     regress margin on (+team A, -team B, home intercept) and total on
     (+team A, +team B, intercept); residual std/corr estimate the same
     quantities. NFL runs BOTH so B is validated against A before we trust
     it for basketball.

Windows are RECENT by design (operator directive: sports change every year):
NFL 2020-2025, NBA 2022-2026 seasons, WNBA 2021-2026 — with an era-split
drift check against the preceding window.

Run:  uv run python tools/calibrate_margin_total.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

HISTORY = Path(__file__).resolve().parent.parent / "data" / "history"


# ------------------------------------------------------------- method A (NFL)


def nfl_line_residuals(seasons: range) -> tuple[np.ndarray, np.ndarray]:
    dm, dt = [], []
    with open(HISTORY / "nfl_games.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                season = int(row["season"])
                hs, as_ = float(row["home_score"]), float(row["away_score"])
                spread = float(row["spread_line"])
                total_line = float(row["total_line"])
            except (KeyError, ValueError, TypeError):
                continue
            if season not in seasons:
                continue
            dm.append((hs - as_) - spread)
            dt.append((hs + as_) - total_line)
    return np.array(dm), np.array(dt)


# ------------------------------------------------- method B (fixed effects)


def _season_games(path: Path) -> list[tuple[str, str, float, float, bool]]:
    """(team, opponent, margin, total, is_home) one row per game, home view."""
    t = pq.read_table(
        path,
        columns=[
            "team_abbreviation",
            "opponent_team_abbreviation",
            "team_score",
            "opponent_team_score",
            "team_home_away",
            "season_type",
        ],
    ).to_pylist()
    games = []
    for r in t:
        if r["team_home_away"] != "home" or r["season_type"] != 2:  # regular season
            continue
        ts, os_ = r["team_score"], r["opponent_team_score"]
        if not ts or not os_:
            continue
        games.append(
            (
                str(r["team_abbreviation"]),
                str(r["opponent_team_abbreviation"]),
                float(ts) - float(os_),
                float(ts) + float(os_),
                True,
            )
        )
    return games


def fe_residuals(paths: list[Path]) -> tuple[np.ndarray, np.ndarray]:
    """Pooled per-season fixed-effects residuals for margin and total."""
    all_dm, all_dt = [], []
    for path in paths:
        games = _season_games(path)
        if len(games) < 50:
            continue
        teams = sorted({g[0] for g in games} | {g[1] for g in games})
        index = {team: i for i, team in enumerate(teams)}
        n, k = len(games), len(teams)
        xm = np.zeros((n, k + 1))
        xt = np.zeros((n, k + 1))
        ym = np.zeros(n)
        yt = np.zeros(n)
        for i, (home, away, margin, total, _) in enumerate(games):
            xm[i, index[home]] = 1.0
            xm[i, index[away]] = -1.0
            xm[i, k] = 1.0  # home-court intercept
            xt[i, index[home]] = 1.0
            xt[i, index[away]] = 1.0
            xt[i, k] = 1.0
            ym[i] = margin
            yt[i] = total
        rm = ym - xm @ np.linalg.lstsq(xm, ym, rcond=None)[0]
        rt = yt - xt @ np.linalg.lstsq(xt, yt, rcond=None)[0]
        # small-sample dof correction so per-season sigma is unbiased-ish
        scale = np.sqrt(n / max(1, n - (k + 1)))
        all_dm.append(rm * scale)
        all_dt.append(rt * scale)
    return np.concatenate(all_dm), np.concatenate(all_dt)


def nfl_fe_residuals(seasons: range) -> tuple[np.ndarray, np.ndarray]:
    """Method B on NFL (validation against method A): build per-season game
    rows from nfl_games.csv and reuse the FE machinery via a temp structure."""
    by_season: dict[int, list[tuple[str, str, float, float, bool]]] = {}
    with open(HISTORY / "nfl_games.csv", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                season = int(row["season"])
                hs, as_ = float(row["home_score"]), float(row["away_score"])
            except (KeyError, ValueError, TypeError):
                continue
            if season not in seasons or row.get("game_type") not in ("REG", None, ""):
                continue
            by_season.setdefault(season, []).append(
                (row["home_team"], row["away_team"], hs - as_, hs + as_, True)
            )
    all_dm, all_dt = [], []
    for games in by_season.values():
        teams = sorted({g[0] for g in games} | {g[1] for g in games})
        index = {team: i for i, team in enumerate(teams)}
        n, k = len(games), len(teams)
        xm = np.zeros((n, k + 1))
        xt = np.zeros((n, k + 1))
        ym = np.zeros(n)
        yt = np.zeros(n)
        for i, (home, away, margin, total, _) in enumerate(games):
            xm[i, index[home]] = 1.0
            xm[i, index[away]] = -1.0
            xm[i, k] = 1.0
            xt[i, index[home]] = 1.0
            xt[i, index[away]] = 1.0
            xt[i, k] = 1.0
            ym[i] = margin
            yt[i] = total
        rm = ym - xm @ np.linalg.lstsq(xm, ym, rcond=None)[0]
        rt = yt - xt @ np.linalg.lstsq(xt, yt, rcond=None)[0]
        scale = np.sqrt(n / max(1, n - (k + 1)))
        all_dm.append(rm * scale)
        all_dt.append(rt * scale)
    return np.concatenate(all_dm), np.concatenate(all_dt)


def report(name: str, dm: np.ndarray, dt: np.ndarray) -> tuple[float, float, float]:
    sm, st = float(np.std(dm)), float(np.std(dt))
    rho = float(np.corrcoef(dm, dt)[0, 1])
    se = 1.0 / np.sqrt(len(dm))
    print(
        f"  {name:34s} n={len(dm):6d}  sigma_M={sm:6.2f}  sigma_T={st:6.2f}  "
        f"rho={rho:+.3f} (SE~{se:.3f})"
    )
    return sm, st, rho


def main() -> None:
    print("NFL (method A: closing-line residuals)")
    a_recent = report("2020-2025 (RECENT -> config)", *nfl_line_residuals(range(2020, 2026)))
    report("2015-2019 (era check)", *nfl_line_residuals(range(2015, 2020)))

    print("NFL (method B: team fixed effects; validates B against A)")
    report("2020-2025", *nfl_fe_residuals(range(2020, 2026)))

    print("NBA (method B)")
    nba_recent = [HISTORY / f"nba_team_box_{y}.parquet" for y in range(2022, 2027)]
    nba_prev = [HISTORY / f"nba_team_box_{y}.parquet" for y in range(2017, 2022)]
    b_recent = report("2022-2026 seasons (RECENT -> config)", *fe_residuals(nba_recent))
    report("2017-2021 (era check)", *fe_residuals(nba_prev))

    print("WNBA (method B)")
    wnba_recent = [HISTORY / f"wnba_team_box_{y}.parquet" for y in range(2021, 2027)]
    wnba_prev = [HISTORY / f"wnba_team_box_{y}.parquet" for y in range(2019, 2021)]
    w_recent = report("2021-2026 seasons (RECENT -> config)", *fe_residuals(wnba_recent))
    report("2019-2020 (era check)", *fe_residuals(wnba_prev))

    print("\nconfig values:")
    for sport, (sm, st, rho) in (
        ("nfl", a_recent),
        ("nba", b_recent),
        ("wnba", w_recent),
    ):
        print(
            f'  "{sport}": {{"sigma_margin": {sm:.2f}, "sigma_total": {st:.2f}, '
            f'"rho": {rho:.3f}}},'
        )


if __name__ == "__main__":
    main()
