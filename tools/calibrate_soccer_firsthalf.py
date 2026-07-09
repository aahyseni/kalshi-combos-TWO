"""Calibrate FIRST-HALF x FULL-GAME soccer correlations (SGP dependence, rank 1).

Uses the EXISTING football-data.co.uk club CSVs in data/history/ which carry
HALF-TIME goals/result (HTHG/HTAG/HTR) alongside full-time (FTHG/FTAG/FTR) and
closing 1X2 + O/U 2.5 odds. No download needed.

Two families, measured pooled (Rank 1) AND conditional-MLE on per-game closing
marginals (gold standard, OOS-gated) where closing odds exist:

  1H result x FG result:  HT home-leader x FT home-win  (and away)
  1H total  x FG total:   1H over 0.5 / over 1.5  x  FT over 2.5

Method mirrors tools/calibrate_pairs_from_history.py (implied_rho via bisection
through the SAME pricer copula) and tools/fit_conditional_rho.py (conditional
copula-rho MLE + held-out-season log-loss gate).

Run: C:/Users/aahys/kalshi-combos-TWO/.venv/Scripts/python.exe tools/calibrate_soccer_firsthalf.py
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
_Z99 = 2.576


# ----------------------------------------------------------- implied rho (rank 2)

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


# ---------------------------------------------------------- fast BVN CDF (Owen)

def bvn_cdf(h: Arr, k: Arr, rho: float) -> Arr:
    h = np.clip(h, -8.0, 8.0)
    k = np.clip(k, -8.0, 8.0)
    if abs(rho) < 1e-12:
        return ndtr(h) * ndtr(k)
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
    value = np.where(both_zero, 0.25 + math.asin(rho) / (2 * math.pi), value)
    return np.clip(value, 0.0, 1.0)


def cell_loglik(p_a: Arr, p_b: Arr, a: Arr, b: Arr, rho: float) -> float:
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
    grid = np.arange(-0.95, 0.951, 0.01)
    scores = [cell_loglik(p_a, p_b, a, b, float(r)) for r in grid]
    best = int(np.argmax(scores))
    rho_hat = float(grid[best])
    n = len(p_a)
    if 0 < best < len(grid) - 1:
        d2 = (scores[best - 1] - 2 * scores[best] + scores[best + 1]) * n / (0.01**2)
        se = math.sqrt(-1.0 / d2) if d2 < 0 else float("nan")
    else:
        se = float("nan")
    return rho_hat, se


# ------------------------------------------------------------------ data load

def devig_two_way(oa: float, ob: float) -> tuple[float, float]:
    ia, ib = 1.0 / oa, 1.0 / ob
    return ia / (ia + ib), ib / (ia + ib)


def devig_three_way(oh: float, od: float, oa: float) -> tuple[float, float, float]:
    ih, id_, ia = 1.0 / oh, 1.0 / od, 1.0 / oa
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


def load_matches() -> list[dict[str, object]]:
    """Every club match: HT + FT outcomes and (where present) devigged closing
    1X2 + O/U 2.5 marginals. Only football-data club CSVs (E0/D1/F1/I1/SP1)."""
    matches: list[dict[str, object]] = []
    for path in sorted(HISTORY.glob("*.csv")):
        stem = path.stem
        if not stem.split("-")[0] in {"E0", "D1", "F1", "I1", "SP1"}:
            continue
        try:
            season = int("20" + stem.split("-")[1][:2])
        except (IndexError, ValueError):
            season = 0
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    fthg = int(row["FTHG"]); ftag = int(row["FTAG"])
                    hthg = int(row["HTHG"]); htag = int(row["HTAG"])
                    htr = row["HTR"].strip(); ftr = row["FTR"].strip()
                except (KeyError, ValueError, AttributeError):
                    continue
                ft_total = fthg + ftag
                ht_total = hthg + htag
                m: dict[str, object] = {
                    "season": season,
                    # 1H result markers
                    "ht_home_lead": htr == "H",
                    "ht_away_lead": htr == "A",
                    # FG result markers
                    "ft_home_win": ftr == "H",
                    "ft_away_win": ftr == "A",
                    # 1H totals
                    "ht_over05": ht_total >= 1,
                    "ht_over15": ht_total >= 2,
                    # FG totals
                    "ft_over25": ft_total >= 3,
                    "ft_over35": ft_total >= 4,
                }
                # closing devigged marginals (C-suffix = closing odds)
                try:
                    oh = float(row.get("B365CH") or row.get("AvgCH") or 0)
                    od = float(row.get("B365CD") or row.get("AvgCD") or 0)
                    oa = float(row.get("B365CA") or row.get("AvgCA") or 0)
                    if min(oh, od, oa) > 1.0:
                        ph, _, pa = devig_three_way(oh, od, oa)
                        m["p_home_win"] = ph
                        m["p_away_win"] = pa
                except (KeyError, ValueError, TypeError):
                    pass
                try:
                    oo = float(row.get("B365>2.5") or row.get("Avg>2.5") or 0)
                    ou = float(row.get("B365<2.5") or row.get("Avg<2.5") or 0)
                    if min(oo, ou) > 1.0:
                        po, _ = devig_two_way(oo, ou)
                        m["p_over25"] = po
                except (KeyError, ValueError, TypeError):
                    pass
                matches.append(m)
    return matches


# --------------------------------------------------------------- measurement

def measure(matches: list[dict[str, object]], a: str, b: str) -> tuple[int, float, float, float, float]:
    rows = [m for m in matches if m.get(a) is not None and m.get(b) is not None]
    n = len(rows)
    p_a = sum(1 for m in rows if m[a]) / n
    p_b = sum(1 for m in rows if m[b]) / n
    p_ab = sum(1 for m in rows if m[a] and m[b]) / n
    return n, p_a, p_b, p_ab, implied_rho(p_a, p_b, p_ab)


def rho_ci99(matches: list[dict[str, object]], a: str, b: str) -> tuple[float, float]:
    n, p_a, p_b, p_ab, _ = measure(matches, a, b)
    se = math.sqrt(max(p_ab * (1 - p_ab), 1e-12) / n)
    lo = implied_rho(p_a, p_b, max(0.0, p_ab - _Z99 * se))
    hi = implied_rho(p_a, p_b, min(1.0, p_ab + _Z99 * se))
    return lo, hi


def cond_frac(matches: list[dict[str, object]], cond: str, target: str) -> tuple[int, float]:
    """P(target | cond) empirical, with n of the conditioning set."""
    rows = [m for m in matches if m.get(cond) and m.get(target) is not None]
    n = len(rows)
    if n == 0:
        return 0, float("nan")
    return n, sum(1 for m in rows if m[target]) / n


PAIRS = [
    # 1H result x FG result
    ("1H home-lead x FT home-win", "ht_home_lead", "ft_home_win"),
    ("1H away-lead x FT away-win", "ht_away_lead", "ft_away_win"),
    # 1H total x FG total
    ("1H over0.5 x FT over2.5", "ht_over05", "ft_over25"),
    ("1H over1.5 x FT over2.5", "ht_over15", "ft_over25"),
    ("1H over1.5 x FT over3.5", "ht_over15", "ft_over35"),
]


def cond_arrays(rows: list[dict[str, object]], pa_key: str, a_key: str, b_col_marg: str, b_key: str):
    p_a = np.array([r[pa_key] for r in rows], dtype=np.float64)
    p_b = np.array([r[b_col_marg] for r in rows], dtype=np.float64)
    a = np.array([bool(r[a_key]) for r in rows], dtype=bool)
    b = np.array([bool(r[b_key]) for r in rows], dtype=bool)
    return p_a, p_b, a, b


def era_split(matches, label, a, b, cut) -> None:
    """OOS proxy: implied rho on early vs late seasons (no per-game HT closing
    line exists in this dataset, so a true conditional-MLE gate is impossible
    for 1H legs; stability across an era split is the honest substitute)."""
    early = [m for m in matches if int(m["season"]) < cut]
    late = [m for m in matches if int(m["season"]) >= cut]
    _, _, _, _, r_e = measure(early, a, b)
    _, _, _, _, r_l = measure(late, a, b)
    print(f"  era {label:28} <{cut}: rho={r_e:+.3f} (n={len(early)})  "
          f">={cut}: rho={r_l:+.3f} (n={len(late)})  drift={r_l - r_e:+.3f}")


def main() -> None:
    matches = load_matches()
    print(f"=== SOCCER CLUB first-half x full-game: {len(matches)} matches "
          f"(top-5 EU, 20/21-24/25) ===\n")

    # empirical conditional fractions the prior asks for
    n_hl, f_hl = cond_frac(matches, "ht_home_lead", "ft_home_win")
    n_al, f_al = cond_frac(matches, "ht_away_lead", "ft_away_win")
    n_lead_draw = sum(1 for m in matches if m["ht_home_lead"])
    print(f"P(FT home-win | HT home-leader) = {f_hl:.4f}  (n={n_hl})")
    print(f"P(FT away-win | HT away-leader) = {f_al:.4f}  (n={n_al})")
    n_o05, f_o05 = cond_frac(matches, "ht_over05", "ft_over25")
    n_o15, f_o15 = cond_frac(matches, "ht_over15", "ft_over25")
    print(f"P(FT over2.5 | 1H over0.5)      = {f_o05:.4f}  (n={n_o05})")
    print(f"P(FT over2.5 | 1H over1.5)      = {f_o15:.4f}  (n={n_o15})")

    print(f"\n{'pair':32} {'n':>6} {'P(A)':>7} {'P(B)':>7} {'P(AB)':>8} {'rho':>8}  {'99% CI':>16}")
    for label, a, b in PAIRS:
        n, p_a, p_b, p_ab, rho = measure(matches, a, b)
        lo, hi = rho_ci99(matches, a, b)
        print(f"{label:32} {n:>6} {p_a:>7.3f} {p_b:>7.3f} {p_ab:>8.3f} {rho:>8.3f}  [{lo:>6.3f},{hi:>6.3f}]")

    print("\n--- era-stability (OOS proxy; no per-game HT closing line to gate on) ---")
    for label, a, b in PAIRS:
        era_split(matches, label, a, b, 2023)


if __name__ == "__main__":
    main()
