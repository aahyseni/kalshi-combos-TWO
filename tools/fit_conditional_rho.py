"""Conditional copula-rho fitting (SGP dependence-fitting directive, pt. 2+4).

Pooled-frequency fitting confounds within-game dependence with between-game
team-strength heterogeneity that live marginals already price — double
counting. Here every historical game contributes its OWN closing-line implied
marginals (p_a, p_b); a single copula rho is fit by MLE over the four joint
outcome cells; and the fit must beat independence on held-out seasons
(log-loss) or it does not ship.

Speed: a vectorized bivariate-normal CDF via Owen's T (scipy.special.owens_t),
self-checked against the exact pricer copula before use.

Run: uv run python tools/fit_conditional_rho.py
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.special import ndtr, ndtri, owens_t

from combomaker.pricing.copula import gaussian_copula_joint_prob

HISTORY = Path(__file__).resolve().parents[1] / "data" / "history"
Arr = NDArray[np.float64]


# ---------------------------------------------------------------- fast BVN CDF

def bvn_cdf(h: Arr, k: Arr, rho: float) -> Arr:
    """P(Z1<=h, Z2<=k) for standard bivariate normal, vectorized (Owen 1956)."""
    h = np.clip(h, -8.0, 8.0)
    k = np.clip(k, -8.0, 8.0)
    if abs(rho) < 1e-12:
        result: Arr = ndtr(h) * ndtr(k)
        return result
    denom = math.sqrt(1.0 - rho * rho)
    eps = 1e-12
    safe_h = np.where(np.abs(h) < eps, eps, h)
    safe_k = np.where(np.abs(k) < eps, eps, k)
    a_h = (k - rho * safe_h) / (safe_h * denom)
    a_k = (h - rho * safe_k) / (safe_k * denom)
    both_zero = (np.abs(h) < eps) & (np.abs(k) < eps)
    hk = h * k
    delta = np.where((hk > 0) | ((np.abs(hk) < eps * eps) & (h + k >= 0)), 0.0, 0.5)
    value = 0.5 * (ndtr(h) + ndtr(k)) - owens_t(h, a_h) - owens_t(k, a_k) - delta
    # exact special case h=k=0: 1/4 + arcsin(rho)/(2 pi)
    value = np.where(both_zero, 0.25 + math.asin(rho) / (2 * math.pi), value)
    return np.clip(value, 0.0, 1.0)


def self_check() -> None:
    """bvn_cdf must agree with the exact pricer copula before we trust it."""
    rng = np.random.default_rng(7)
    worst = 0.0
    for _ in range(200):
        p_a, p_b = rng.uniform(0.05, 0.95, 2)
        rho = rng.uniform(-0.9, 0.9)
        ours = float(
            bvn_cdf(np.array([ndtri(p_a)]), np.array([ndtri(p_b)]), rho)[0]
        )
        exact = gaussian_copula_joint_prob([p_a, p_b], np.array([[1, rho], [rho, 1]]))
        worst = max(worst, abs(ours - exact))
    assert worst < 5e-7, f"bvn_cdf self-check failed: max err {worst}"
    print(f"bvn_cdf self-check vs pricer copula: max abs err {worst:.2e}  OK")


# ------------------------------------------------------------------ MLE + OOS

def cell_loglik(p_a: Arr, p_b: Arr, a: Arr, b: Arr, rho: float) -> float:
    """Mean log-likelihood of observed (a, b) cells given per-game marginals."""
    p11 = bvn_cdf(ndtri(p_a), ndtri(p_b), rho)
    lower = np.maximum(0.0, p_a + p_b - 1.0)
    upper = np.minimum(p_a, p_b)
    p11 = np.clip(p11, lower + 1e-9, upper - 1e-9)
    cell = np.where(
        a & b, p11,
        np.where(a, p_a - p11, np.where(b, p_b - p11, 1.0 - p_a - p_b + p11)),
    )
    return float(np.mean(np.log(np.clip(cell, 1e-12, 1.0))))


def fit_rho(p_a: Arr, p_b: Arr, a: Arr, b: Arr) -> tuple[float, float]:
    """Grid+refine MLE; returns (rho_hat, approx SE from curvature)."""
    grid = np.arange(-0.95, 0.951, 0.01)
    scores = [cell_loglik(p_a, p_b, a, b, float(r)) for r in grid]
    best = int(np.argmax(scores))
    rho_hat = float(grid[best])
    # curvature-based SE: second difference of TOTAL loglik
    n = len(p_a)
    if 0 < best < len(grid) - 1:
        d2 = (scores[best - 1] - 2 * scores[best] + scores[best + 1]) * n / (0.01**2)
        se = math.sqrt(-1.0 / d2) if d2 < 0 else float("nan")
    else:
        se = float("nan")
    return rho_hat, se


def devig_two_way(odds_a: float, odds_b: float) -> tuple[float, float]:
    ia, ib = 1.0 / odds_a, 1.0 / odds_b
    return ia / (ia + ib), ib / (ia + ib)


def devig_three_way(odds_h: float, odds_d: float, odds_a: float) -> tuple[float, float, float]:
    ih, id_, ia = 1.0 / odds_h, 1.0 / odds_d, 1.0 / odds_a
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


# --------------------------------------------------------------------- soccer

def load_soccer_conditional() -> list[dict[str, float | bool | int]]:
    """Club matches WITH closing odds: devigged 1X2 home prob + O/U 2.5 prob."""
    rows: list[dict[str, float | bool | int]] = []
    for path in sorted(HISTORY.glob("*-2*.csv")):
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    home_goals = int(row["FTHG"])
                    away_goals = int(row["FTAG"])
                    odds_h = float(row.get("B365H") or row.get("AvgH") or 0)
                    odds_d = float(row.get("B365D") or row.get("AvgD") or 0)
                    odds_a = float(row.get("B365A") or row.get("AvgA") or 0)
                    odds_over = float(row.get("B365>2.5") or row.get("Avg>2.5") or 0)
                    odds_under = float(row.get("B365<2.5") or row.get("Avg<2.5") or 0)
                    season = int("20" + path.stem.split("-")[1][:2])
                except (KeyError, ValueError, TypeError, IndexError):
                    continue
                if min(odds_h, odds_d, odds_a, odds_over, odds_under) <= 1.0:
                    continue
                p_home, _, _ = devig_three_way(odds_h, odds_d, odds_a)
                p_over, _ = devig_two_way(odds_over, odds_under)
                rows.append(
                    {
                        "p_a": p_home,
                        "p_b": p_over,
                        "a": home_goals > away_goals,
                        "b": home_goals + away_goals >= 3,
                        "btts": home_goals >= 1 and away_goals >= 1,
                        "season": season,
                    }
                )
    return rows


# ------------------------------------------------------------------------ NFL

def american_implied(odds: float) -> float:
    return (-odds / (-odds + 100.0)) if odds < 0 else (100.0 / (odds + 100.0))


def load_nfl_conditional() -> list[dict[str, float | bool | int]]:
    rows: list[dict[str, float | bool | int]] = []
    with open(HISTORY / "nfl_games.csv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                home = int(row["home_score"])
                away = int(row["away_score"])
                ml_home = float(row["home_moneyline"])
                ml_away = float(row["away_moneyline"])
                line = float(row["total_line"])
                season = int(row["season"])
            except (KeyError, ValueError, TypeError):
                continue
            total = home + away
            if total == line:
                continue  # push
            ih, ia = american_implied(ml_home), american_implied(ml_away)
            rows.append(
                {
                    "p_a": ih / (ih + ia),
                    "p_b": 0.5,  # the closing line IS the even-money point
                    "a": home > away,
                    "b": total > line,
                    "season": season,
                }
            )
    return rows


# ---------------------------------------------------------------------- runs

def arrays(rows: list[dict], a_key: str = "a", b_key: str = "b") -> tuple[Arr, Arr, Arr, Arr]:
    p_a = np.array([r["p_a"] for r in rows], dtype=np.float64)
    p_b = np.array([r["p_b"] for r in rows], dtype=np.float64)
    a = np.array([r[a_key] for r in rows], dtype=bool)
    b = np.array([r[b_key] for r in rows], dtype=bool)
    return p_a, p_b, a, b


def run(title: str, rows: list[dict], train_before: int) -> None:
    train = [r for r in rows if int(r["season"]) < train_before]
    test = [r for r in rows if int(r["season"]) >= train_before]
    p_a, p_b, a, b = arrays(train)
    rho, se = fit_rho(p_a, p_b, a, b)
    print(f"\n{title}")
    print(f"  train n={len(train)} (seasons <{train_before}): conditional-MLE rho = "
          f"{rho:+.3f} (SE {se:.3f})")
    ta, tb, taa, tbb = arrays(test)
    ll_fit = cell_loglik(ta, tb, taa, tbb, rho)
    ll_ind = cell_loglik(ta, tb, taa, tbb, 0.0)
    verdict = "BEATS independence" if ll_fit > ll_ind else "does NOT beat independence"
    print(f"  OOS n={len(test)} (seasons >={train_before}): logloss fit={-ll_fit:.5f} "
          f"vs indep={-ll_ind:.5f}  -> {verdict}")
    fa, fb, faa, fbb = arrays(rows)
    rho_all, se_all = fit_rho(fa, fb, faa, fbb)
    print(f"  full-sample conditional rho = {rho_all:+.3f} (SE {se_all:.3f})")


def main() -> None:
    self_check()
    soccer = load_soccer_conditional()
    run("SOCCER CLUB  home-win x over2.5  (conditional on closing odds)", soccer, 2024)
    nfl = load_nfl_conditional()
    run("NFL  home-win x over-line  (conditional on closing ML; line = 0.5)", nfl, 2019)


if __name__ == "__main__":
    main()
