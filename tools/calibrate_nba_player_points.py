"""Calibrate BASKETBALL player-points SGP correlations from hoopR/wehoop player box.

Domain: NBA + WNBA, ranks 1+2 of the method contract in
docs/calibration/PLAN.md. We have NO live prop lines, so — exactly like the
repo's game-total median trick (see load_nba_modern in
tools/calibrate_pairs_from_history.py) — each player gets a self-normalizing
line proxy = his OWN regular-season median points that season. "Over" = he
exceeds that median. Game total "over" = the game total exceeds the season
median total.

For every (season, player, game) we observe:
    A  = player scored over his season median  (the prop leg)
    B1 = game total over the season median total (the total leg)
    B2 = the player's team won                   (the moneyline leg)

Across the whole panel we measure P(A), P(B), P(A n B) and invert the SAME
Gaussian copula the pricer runs (combomaker.pricing.copula) into a copula rho
via bisection — so the output is a drop-in `pair_rho`, not a statistic needing
translation. 99% CI = binomial SE on P(A n B) pushed through the (monotone)
solver, identical to tools/calibrate_pairs_from_history.py.

Data (data/history/, fetched 2026-07-06 from the sportsdataverse/hoopR-data and
sportsdataverse/wehoop-data github repos, main branch, contents API raw media):
    nba_player_box_2010..2023.parquet   (ESPN, rich schema)
    wnba_player_box_2010..2022.parquet  (ESPN; 2016 is the old wide format -> skipped)

Run:
    C:/Users/aahys/kalshi-combos-TWO/.venv/Scripts/python.exe tools/calibrate_nba_player_points.py
"""

from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"

_Z99 = 2.576
MIN_GAMES = 10  # a player needs this many played games for a stable season median
STAR_TIERS = (0.0, 15.0, 20.0, 25.0)  # season points-per-game thresholds

RICH_COLS = [
    "game_id",
    "season",
    "season_type",
    "athlete_id",
    "did_not_play",
    "points",
    "team_id",
    "team_winner",
    "team_score",
    "opponent_team_score",
]


# --------------------------------------------------------------------------- #
# copula inversion (mirrors tools/calibrate_pairs_from_history.py exactly)
# --------------------------------------------------------------------------- #
def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
    """The rho making copula(p_a, p_b; rho) == p_ab (monotone => bisection)."""

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


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n % 2 == 1:
        return float(s[n // 2])
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


# --------------------------------------------------------------------------- #
# load: one observation per (season, player, game) the player actually played
# --------------------------------------------------------------------------- #
def load_player_games(paths: list[Path]) -> list[dict]:
    # pass 1: gather played points per (season, player) and total per (season, game)
    pts_by_player: dict[tuple[int, str], list[int]] = defaultdict(list)
    total_by_game: dict[tuple[int, str], int] = {}
    raw: list[tuple] = []
    for path in paths:
        if not path.exists():
            continue
        tbl = pq.read_table(path, columns=RICH_COLS).to_pylist()
        for r in tbl:
            if r["season_type"] != 2:  # regular season only
                continue
            if r["did_not_play"]:
                continue
            pts = r["points"]
            ts = r["team_score"]
            os_ = r["opponent_team_score"]
            if pts is None or ts is None or os_ is None:
                continue
            season = int(r["season"])
            aid = str(r["athlete_id"])
            gid = str(r["game_id"])
            pts_by_player[(season, aid)].append(int(pts))
            total_by_game[(season, gid)] = int(ts) + int(os_)
            raw.append((season, aid, gid, int(pts), int(ts), int(os_), bool(r["team_winner"])))

    # pass 2: medians
    player_median: dict[tuple[int, str], float] = {}
    player_mean: dict[tuple[int, str], float] = {}
    for key, lst in pts_by_player.items():
        if len(lst) >= MIN_GAMES:
            player_median[key] = median([float(x) for x in lst])
            player_mean[key] = sum(lst) / len(lst)

    season_totals: dict[int, list[int]] = defaultdict(list)
    for (season, _gid), tot in total_by_game.items():
        season_totals[season].append(tot)
    season_median_total = {s: median([float(x) for x in v]) for s, v in season_totals.items()}

    # pass 3: observations
    obs: list[dict] = []
    for season, aid, gid, pts, ts, os_, win in raw:
        key = (season, aid)
        if key not in player_median:
            continue
        pmed = player_median[key]
        total = ts + os_
        smed = season_median_total[season]
        pts_over = None if pts == pmed else pts > pmed
        tot_over = None if total == smed else total > smed
        obs.append(
            {
                "season": season,
                "game": (season, gid),
                "pts_over": pts_over,
                "tot_over": tot_over,
                "win": win,
                "mean_pts": player_mean[key],
                "margin_abs": abs(ts - os_),
            }
        )
    return obs


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
def measure(rows: list[dict], a: str, b: str) -> tuple[int, float, float, float, float]:
    sub = [r for r in rows if r[a] is not None and r[b] is not None]
    n = len(sub)
    if n == 0:
        return 0, float("nan"), float("nan"), float("nan"), float("nan")
    p_a = sum(1 for r in sub if r[a]) / n
    p_b = sum(1 for r in sub if r[b]) / n
    p_ab = sum(1 for r in sub if r[a] and r[b]) / n
    return n, p_a, p_b, p_ab, implied_rho(p_a, p_b, p_ab)


def rho_ci99(rows: list[dict], a: str, b: str, *, n_eff: int | None = None) -> tuple[float, float]:
    """99% CI. If n_eff is given (cluster-floor = distinct games), the binomial
    SE uses it instead of the row count, since players in the same game share the
    total/win leg and the naive row-n CI is optimistic (mirrors results_baseball.md)."""
    n, p_a, p_b, p_ab, _ = measure(rows, a, b)
    if n == 0:
        return float("nan"), float("nan")
    se = math.sqrt(max(p_ab * (1 - p_ab), 1e-12) / (n_eff if n_eff else n))
    lo = implied_rho(p_a, p_b, max(0.0, p_ab - _Z99 * se))
    hi = implied_rho(p_a, p_b, min(1.0, p_ab + _Z99 * se))
    return lo, hi


def line(label: str, rows: list[dict], a: str, b: str, *, cluster: bool = False) -> None:
    n, p_a, p_b, p_ab, rho = measure(rows, a, b)
    lo, hi = rho_ci99(rows, a, b)
    tail = ""
    if cluster:
        ng = len({r["game"] for r in rows if r[a] is not None and r[b] is not None})
        clo, chi = rho_ci99(rows, a, b, n_eff=ng)
        tail = f"  clus[{clo:>6.3f},{chi:>6.3f}] g={ng}"
    print(
        f"{label:46} {n:>7} {p_a:>6.3f} {p_b:>6.3f} {p_ab:>7.3f} {rho:>8.3f}  [{lo:>6.3f},{hi:>6.3f}]{tail}"
    )


def header() -> None:
    print(f"{'subset':46} {'n':>7} {'P(A)':>6} {'P(B)':>6} {'P(AB)':>7} {'rho':>8}  {'99% CI':>16}")


def report_league(name: str, obs: list[dict]) -> None:
    seasons = sorted({r["season"] for r in obs})
    print(f"\n{'=' * 96}")
    print(f"{name}: {len(obs):,} player-games, seasons {seasons[0]}-{seasons[-1]}")
    print("=" * 96)

    # ---- PAIR 1: points-over x total-over, by usage tier ----
    print("\n[PAIR 1] player points OVER (own median)  x  game total OVER (season median)")
    print("  (clus[..] = cluster-floor CI, n=distinct games: players share the total leg)")
    header()
    for thr in STAR_TIERS:
        rows = [r for r in obs if r["mean_pts"] >= thr]
        tag = "ALL players" if thr == 0.0 else f"season PPG >= {thr:.0f}"
        line(tag, rows, "pts_over", "tot_over", cluster=True)

    # ---- PAIR 2: points-over x team win, by usage tier ----
    print("\n[PAIR 2] player points OVER (own median)  x  his TEAM wins (moneyline)")
    header()
    for thr in STAR_TIERS:
        rows = [r for r in obs if r["mean_pts"] >= thr]
        tag = "ALL players" if thr == 0.0 else f"season PPG >= {thr:.0f}"
        line(tag, rows, "pts_over", "win", cluster=True)

    # ---- PAIR 2 blowout / garbage-time nuance (all players) ----
    print("\n[PAIR 2 nuance] points-over x win, split by final margin |team-opp|")
    header()
    buckets = [("close  |margin|<=8", 0, 8), ("mid    9-19", 9, 19), ("blowout >=20", 20, 999)]
    for tag, lo_m, hi_m in buckets:
        rows = [r for r in obs if lo_m <= r["margin_abs"] <= hi_m]
        line(tag, rows, "pts_over", "win")
    # star-only blowout view (the garbage-time bench-sitting story is a star story)
    print("  -- stars only (PPG>=20):")
    for tag, lo_m, hi_m in buckets:
        rows = [r for r in obs if lo_m <= r["margin_abs"] <= hi_m and r["mean_pts"] >= 20.0]
        line("  " + tag, rows, "pts_over", "win")

    # ---- era split ----
    cut = 2017
    print(f"\n[ERA STABILITY] cut at season {cut} (ALL players)")
    header()
    early = [r for r in obs if r["season"] < cut]
    late = [r for r in obs if r["season"] >= cut]
    line(f"points x total  <{cut}", early, "pts_over", "tot_over")
    line(f"points x total  >={cut}", late, "pts_over", "tot_over")
    line(f"points x win    <{cut}", early, "pts_over", "win")
    line(f"points x win    >={cut}", late, "pts_over", "win")


def main() -> None:
    nba_paths = [HISTORY / f"nba_player_box_{y}.parquet" for y in range(2010, 2024)]
    # WNBA 2016 is the old wide-format file (pts as string, no game context) -> skip
    wnba_paths = [
        HISTORY / f"wnba_player_box_{y}.parquet" for y in range(2010, 2023) if y != 2016
    ]

    nba = load_player_games(nba_paths)
    report_league("NBA (hoopR/ESPN player box, reg season)", nba)

    wnba = load_player_games(wnba_paths)
    report_league("WNBA (wehoop/ESPN player box, reg season)", wnba)


if __name__ == "__main__":
    main()
