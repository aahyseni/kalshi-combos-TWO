"""Calibrate BASKETBALL first-half / full-game correlations from hoopR PBP.

Domain: NBA period combos (rank 1+2). The team/player box scores carry only
final scores, so half-level data comes from the play-by-play running score.
For each game we read the cumulative (home_score, away_score) and take:

    halftime = the running score at the last play with period_number <= 2
    final    = the running score at the last play of the game (incl. OT)

Then we calibrate two period pairs with the same median self-normalizing trick
and the same copula inversion as the player-points tool:

    1H-total  OVER (season median)  x  FG-total OVER (season median)
    1H-margin (home leads at half)  x  FG-result (home wins)

A correctness gate cross-checks the PBP final total against the hoopR team-box
total for the same game_id; a low match rate means the PBP running score is
untrustworthy and the period combos should be NO-QUOTE.

Data: data/history/nba_pbp_2021..2023.parquet (blobs API from hoopR-data main),
      data/history/nba_team_box_2021..2023.parquet (already local).

Run:
    C:/Users/aahys/kalshi-combos-TWO/.venv/Scripts/python.exe tools/calibrate_nba_first_half.py
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


def implied_rho(p_a: float, p_b: float, p_ab: float) -> float:
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
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def build_games(years: range, league: str = "nba") -> tuple[list[dict], dict]:
    """Return (per-game records, validation stats vs team box) for a league."""
    games: dict[str, dict] = {}
    for y in years:
        path = HISTORY / f"{league}_pbp_{y}.parquet"
        if not path.exists():
            continue
        t = pq.read_table(
            path,
            columns=["game_id", "period_number", "home_score", "away_score", "season", "season_type"],
        )
        gid = t.column("game_id").to_pylist()
        per = t.column("period_number").to_pylist()
        hs = t.column("home_score").to_pylist()
        as_ = t.column("away_score").to_pylist()
        seas = t.column("season").to_pylist()
        stype = t.column("season_type").to_pylist()
        for i in range(len(gid)):
            h, a, p = hs[i], as_[i], per[i]
            if h is None or a is None or p is None:
                continue
            key = str(gid[i])
            g = games.get(key)
            if g is None:
                g = games[key] = {
                    "season": int(seas[i]) if seas[i] is not None else None,
                    "season_type": stype[i],
                    "ht_h": 0, "ht_a": 0, "fg_h": 0, "fg_a": 0,
                }
            h, a = int(h), int(a)
            if h > g["fg_h"]:
                g["fg_h"] = h
            if a > g["fg_a"]:
                g["fg_a"] = a
            if p <= 2:
                if h > g["ht_h"]:
                    g["ht_h"] = h
                if a > g["ht_a"]:
                    g["ht_a"] = a

    # validation vs team box finals
    box_total: dict[str, int] = {}
    for y in years:
        bp = HISTORY / f"{league}_team_box_{y}.parquet"
        if not bp.exists():
            continue
        bt = pq.read_table(bp, columns=["game_id", "team_home_away", "team_score", "opponent_team_score"]).to_pylist()
        for r in bt:
            if r["team_home_away"] == "home" and r["team_score"] is not None:
                box_total[str(r["game_id"])] = int(r["team_score"]) + int(r["opponent_team_score"])

    match = mism = checked = 0
    recs: list[dict] = []
    for g, rec in games.items():
        if rec["season_type"] != 2:  # regular season only
            continue
        fg_total = rec["fg_h"] + rec["fg_a"]
        if g in box_total:
            checked += 1
            if box_total[g] == fg_total:
                match += 1
            else:
                mism += 1
        recs.append(
            {
                "season": rec["season"],
                "h1_total": rec["ht_h"] + rec["ht_a"],
                "fg_total": fg_total,
                "h1_home_lead": rec["ht_h"] > rec["ht_a"],
                "fg_home_win": rec["fg_h"] > rec["fg_a"],
            }
        )
    stats = {"checked": checked, "match": match, "mism": mism}
    return recs, stats


def measure(rows: list[dict], a: str, b: str) -> tuple[int, float, float, float, float]:
    sub = [r for r in rows if r[a] is not None and r[b] is not None]
    n = len(sub)
    p_a = sum(1 for r in sub if r[a]) / n
    p_b = sum(1 for r in sub if r[b]) / n
    p_ab = sum(1 for r in sub if r[a] and r[b]) / n
    return n, p_a, p_b, p_ab, implied_rho(p_a, p_b, p_ab)


def ci99(rows: list[dict], a: str, b: str) -> tuple[float, float]:
    n, p_a, p_b, p_ab, _ = measure(rows, a, b)
    se = math.sqrt(max(p_ab * (1 - p_ab), 1e-12) / n)
    return (
        implied_rho(p_a, p_b, max(0.0, p_ab - _Z99 * se)),
        implied_rho(p_a, p_b, min(1.0, p_ab + _Z99 * se)),
    )


def add_over_indicators(recs: list[dict]) -> None:
    by_season_h1: dict[int, list[int]] = defaultdict(list)
    by_season_fg: dict[int, list[int]] = defaultdict(list)
    for r in recs:
        by_season_h1[r["season"]].append(r["h1_total"])
        by_season_fg[r["season"]].append(r["fg_total"])
    med_h1 = {s: median([float(x) for x in v]) for s, v in by_season_h1.items()}
    med_fg = {s: median([float(x) for x in v]) for s, v in by_season_fg.items()}
    for r in recs:
        r["h1_over"] = None if r["h1_total"] == med_h1[r["season"]] else r["h1_total"] > med_h1[r["season"]]
        r["fg_over"] = None if r["fg_total"] == med_fg[r["season"]] else r["fg_total"] > med_fg[r["season"]]


def line(label: str, rows: list[dict], a: str, b: str) -> None:
    n, p_a, p_b, p_ab, rho = measure(rows, a, b)
    lo, hi = ci99(rows, a, b)
    print(f"{label:40} {n:>6} {p_a:>6.3f} {p_b:>6.3f} {p_ab:>7.3f} {rho:>8.3f}  [{lo:>6.3f},{hi:>6.3f}]")


def report(league: str, years: range, expect: str) -> None:
    recs, stats = build_games(years, league)
    add_over_indicators(recs)
    print(f"\n{'=' * 84}")
    print(
        f"{league.upper()} PBP first-half calibration: {len(recs):,} regular-season games "
        f"({years.start}-{years.stop - 1})"
    )
    rate = stats["match"] / stats["checked"] if stats["checked"] else float("nan")
    print(
        f"VALIDATION vs team-box finals: {stats['match']}/{stats['checked']} match "
        f"({rate:.3%}), {stats['mism']} mismatch"
    )
    mean_h1 = sum(r["h1_total"] for r in recs) / len(recs)
    mean_fg = sum(r["fg_total"] for r in recs) / len(recs)
    print(f"sanity: mean 1H total = {mean_h1:.1f}, mean FG total = {mean_fg:.1f} (expect {expect})")
    print(f"{'pair':40} {'n':>6} {'P(A)':>6} {'P(B)':>6} {'P(AB)':>7} {'rho':>8}  {'99% CI':>16}")
    line("1H-total OVER x FG-total OVER", recs, "h1_over", "fg_over")
    line("1H-margin (home lead) x FG home win", recs, "h1_home_lead", "fg_home_win")
    p_lead = sum(1 for r in recs if r["h1_home_lead"]) / len(recs)
    p_win = sum(1 for r in recs if r["fg_home_win"]) / len(recs)
    p_conv = sum(1 for r in recs if r["h1_home_lead"] and r["fg_home_win"]) / sum(
        1 for r in recs if r["h1_home_lead"]
    )
    print(
        f"  home leads at half {p_lead:.3f}; home wins {p_win:.3f}; "
        f"P(home wins | home leads at half) = {p_conv:.3f}"
    )


def main() -> None:
    report("nba", range(2021, 2024), "~112 / ~225")
    report("wnba", range(2019, 2023), "~82 / ~165")


if __name__ == "__main__":
    main()
