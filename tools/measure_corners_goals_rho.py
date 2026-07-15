"""Measure the TRUE corners<->goals correlation at the lines we trade.

MEASUREMENT ONLY (operator directive 2026-07-15: "measure first, change nothing
live"). This script reads the local football-data.co.uk CLUB CSVs and computes,
for each traded Kalshi line pair, the empirical 2x2 joint and the TETRACHORIC
(Gaussian-copula) correlation -- the apples-to-apples number vs config.py's
Gaussian-copula ``pair_rho``.

Why tetrachoric == the config's number
--------------------------------------
combomaker prices a 2-leg combo with ``pricing/copula.gaussian_copula_joint_prob``:
each leg's YES prob p_i becomes a latent threshold z_i = Phi^-1(p_i), and
P(A and B) = BVN_CDF(z_A, z_B; rho). The tetrachoric correlation of a 2x2 table
is *defined* as the rho of that same bivariate normal that reproduces the
observed P(A and B) given the two marginals. So the rho this script solves for is
exactly on the same scale as ``config.pair_rho`` -- promoting a measured value
(if warranted) is a like-for-like swap.

KEEP IN SYNC: the tetrachoric solver here inverts the SAME BVN CDF that
``pricing/copula.gaussian_copula_joint_prob`` evaluates in the 2-leg case. This
script re-derives the 2-leg BVN CDF via ``scipy.stats.multivariate_normal`` (with
an Owen's-T cross-check) instead of importing the live function, because the live
one is tuned for the forward direction (fixed-seed QMC) and we want a clean,
deterministic inverse. A parity assertion at import time confirms our forward BVN
matches the live copula to < 2e-4 on a grid, so the inverse is on the live scale.

Data
----
- data/history/{D1,E0,F1,I1,SP1}-{2021..2425}.csv : football-data.co.uk CLUB data
  (HC/AC home/away corners, FTHG/FTAG full-time goals). Our ONLY real corners set.
- data/history/*eve.zip : Retrosheet MLB baseball event files -- NOT soccer, no
  corners. Confirmed by inspection; excluded.
- data/history/intl_results.csv : martj42 internationals, GOALS ONLY, no corners
  -> cannot measure corners. Stated as a limitation, not used here.

Run:  python tools/measure_corners_goals_rho.py
"""

from __future__ import annotations

import sys

# Windows consoles default to cp1252; force UTF-8 so any stray glyph won't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:
    pass

import csv
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import brentq
from scipy.stats import multivariate_normal, norm
from scipy.stats import norm as _norm
from scipy.special import owens_t

# ---------------------------------------------------------------------------
# Paths / traded lines
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]
HIST = REPO / "data" / "history"

# football-data division codes -> readable league names
LEAGUES = {
    "E0": "England (E0)",
    "D1": "Germany (D1)",
    "I1": "Italy (I1)",
    "SP1": "Spain (SP1)",
    "F1": "France (F1)",
}
SEASONS = ["2021", "2122", "2223", "2324", "2425"]

# Kalshi lines we trade.
#   TOTAL corners  (KXWCTCORNERS): over 7/8/9/10  ==  total_corners >= {7,8,9,10}
#   TOTAL goals    (KXWCTGOALS)  : over 1.5/2.5/3.5 == total_goals  >= {2,3,4}
#   BTTS: both teams score.
CORNER_THRESHOLDS = [7, 8, 9, 10]  # total_corners >= t
GOAL_THRESHOLDS = [2, 3, 4]  # total_goals >= g  (over 1.5 / 2.5 / 3.5)

N_BOOT = 2000
SEED = 20260715


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


@dataclass
class Game:
    league: str
    total_corners: int
    total_goals: int
    btts: bool


def load_games() -> list[Game]:
    games: list[Game] = []
    for code in LEAGUES:
        for season in SEASONS:
            path = HIST / f"{code}-{season}.csv"
            if not path.exists():
                continue
            with path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    hc, ac = row.get("HC"), row.get("AC")
                    hg, ag = row.get("FTHG"), row.get("FTAG")
                    if not (hc and ac and hg and ag):
                        continue
                    try:
                        hc_i, ac_i = int(hc), int(ac)
                        hg_i, ag_i = int(hg), int(ag)
                    except ValueError:
                        continue
                    games.append(
                        Game(
                            league=code,
                            total_corners=hc_i + ac_i,
                            total_goals=hg_i + ag_i,
                            btts=(hg_i >= 1 and ag_i >= 1),
                        )
                    )
    return games


# ---------------------------------------------------------------------------
# Bivariate-normal CDF + tetrachoric solver
# ---------------------------------------------------------------------------

_MVN_SEED = 20260705  # the live copula's QMC seed (used only in the parity check)


def bvn_cdf(za: float, zb: float, rho: float) -> float:
    """P(Z_A <= za, Z_B <= zb) for standard bivariate normal with corr rho.

    Exact, deterministic, fast: the standard Owen's-T decomposition of the
    bivariate-normal CDF (used instead of scipy's QMC ``multivariate_normal.cdf``
    so the bootstrap's ~64k BVN inversions run in seconds, not hours). This is a
    pure math identity for the SAME BVN the live Gaussian copula integrates -- the
    import-time parity check asserts it matches ``gaussian_copula_joint_prob`` to
    < 2e-4, so the tetrachoric rho we solve is on the live config's exact scale.

    L(h,k;rho) = P(X>h, Y>k). BVN_CDF(h,k;rho) = 1 - Phi(-h) - Phi(-k) + L(-h,-k;rho)
    and L is computed via Owen's T (Owen 1956 / Young-Minder).
    """
    if rho <= -0.999999:
        return max(0.0, float(_norm.cdf(za) + _norm.cdf(zb) - 1.0))
    if rho >= 0.999999:
        return float(min(_norm.cdf(za), _norm.cdf(zb)))

    def _L(h: float, k: float, r: float) -> float:
        """Upper-orthant P(X>h, Y>k) via Owen's T (handles r sign & h,k signs)."""
        if abs(r) < 1e-12:
            return float((1.0 - _norm.cdf(h)) * (1.0 - _norm.cdf(k)))
        denom = math.sqrt(1.0 - r * r)
        # a-terms for Owen's T
        ah = (k / h - r) / denom if h != 0 else None
        ak = (h / k - r) / denom if k != 0 else None
        if h != 0 and k != 0:
            t_h = float(owens_t(h, ah))
            t_k = float(owens_t(k, ak))
            delta = 0.0 if (h * k > 0 or (h * k == 0 and (h + k) >= 0)) else 0.5
            bvn_upper = (
                0.5 * (1.0 - _norm.cdf(h))
                + 0.5 * (1.0 - _norm.cdf(k))
                - t_h
                - t_k
                - delta
            )
            return float(bvn_upper)
        # h==0 or k==0 edge cases via the CDF-form directly
        cov = np.array([[1.0, r], [r, 1.0]], dtype=np.float64)
        c = float(
            multivariate_normal.cdf(
                np.array([-h, -k]), mean=np.zeros(2), cov=cov, allow_singular=True
            )
        )
        return c

    # BVN_CDF(za,zb;rho) = L(-za,-zb;rho)  (survival symmetry of the BVN)
    val = _L(-za, -zb, rho)
    return float(min(1.0, max(0.0, val)))


def tetrachoric_rho(p_a: float, p_b: float, p_ab: float) -> float | None:
    """rho of the BVN reproducing P(A), P(B), P(A and B) via the upper-tail joint.

    A = {indicator_A = 1} has P(A) = p_a, so the latent crosses z_a = Phi^-1(1-p_a)
    from ABOVE: P(A) = P(Z > z_a). P(A and B) = P(Z_A > z_a, Z_B > z_b). Under the
    BVN survival symmetry P(Z_A > z_a, Z_B > z_b; rho) = BVN_CDF(-z_a, -z_b; rho),
    so we solve BVN_CDF(Phi^-1(p_a), Phi^-1(p_b); rho) = p_ab for rho, using the
    lower thresholds Phi^-1(p_a) = -z_a directly. Returns None if a marginal is
    degenerate (0/1) -- no finite latent threshold.
    """
    if not (0.0 < p_a < 1.0) or not (0.0 < p_b < 1.0):
        return None
    la = norm.ppf(p_a)  # = -z_a ; P(Z_A > z_a) = P(Z_A < la) after sign flip
    lb = norm.ppf(p_b)
    lo, hi = 1e-6, 1.0 - 1e-6  # Frechet: p_ab in [max(0,pa+pb-1), min(pa,pb)]

    def f(rho: float) -> float:
        return bvn_cdf(la, lb, rho) - p_ab

    f_lo, f_hi = f(-1 + lo), f(1 - lo)
    # p_ab outside the achievable BVN range -> clamp to the Frechet-implied bound.
    if f_lo > 0:  # even rho=-1 overshoots -> p_ab below lower Frechet bound
        return -1.0
    if f_hi < 0:  # even rho=+1 undershoots -> p_ab above upper Frechet bound
        return 1.0
    try:
        return float(brentq(f, -1 + lo, 1 - lo, xtol=1e-6, rtol=1e-8))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Per-pair stats
# ---------------------------------------------------------------------------


@dataclass
class PairResult:
    corner_thr: int
    goal_label: str
    n: int
    p_a: float  # P(corners >= thr)
    p_b: float  # P(goal event)
    p_ab: float  # P(both)
    rho_tet: float | None
    rho_tet_lo: float | None
    rho_tet_hi: float | None
    phi: float  # Pearson of the two indicators
    pearson_counts: float  # Pearson(total_corners, total_goals)  [const across goal_label]


def indicator_arrays(
    games: list[Game], corner_thr: int, goal_label: str
) -> tuple[np.ndarray, np.ndarray]:
    a = np.array([1.0 if g.total_corners >= corner_thr else 0.0 for g in games])
    if goal_label == "btts":
        b = np.array([1.0 if g.btts else 0.0 for g in games])
    else:
        thr = int(goal_label.split(">=")[1])
        b = np.array([1.0 if g.total_goals >= thr else 0.0 for g in games])
    return a, b


def phi_coef(a: np.ndarray, b: np.ndarray) -> float:
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def measure_pair(
    games: list[Game], corner_thr: int, goal_label: str, rng: np.random.Generator
) -> PairResult:
    a, b = indicator_arrays(games, corner_thr, goal_label)
    n = len(a)
    p_a = float(a.mean())
    p_b = float(b.mean())
    p_ab = float((a * b).mean())
    rho = tetrachoric_rho(p_a, p_b, p_ab)

    # Bootstrap CI on the tetrachoric rho (resample games with replacement).
    boots: list[float] = []
    idx_all = np.arange(n)
    for _ in range(N_BOOT):
        idx = rng.choice(idx_all, size=n, replace=True)
        aa, bb = a[idx], b[idx]
        pa, pb, pab = float(aa.mean()), float(bb.mean()), float((aa * bb).mean())
        r = tetrachoric_rho(pa, pb, pab)
        if r is not None:
            boots.append(r)
    if boots:
        lo = float(np.percentile(boots, 2.5))
        hi = float(np.percentile(boots, 97.5))
    else:
        lo = hi = None

    tc = np.array([g.total_corners for g in games], dtype=float)
    tg = np.array([g.total_goals for g in games], dtype=float)
    pearson_counts = float(np.corrcoef(tc, tg)[0, 1])

    return PairResult(
        corner_thr=corner_thr,
        goal_label=goal_label,
        n=n,
        p_a=p_a,
        p_b=p_b,
        p_ab=p_ab,
        rho_tet=rho,
        rho_tet_lo=lo,
        rho_tet_hi=hi,
        phi=phi_coef(a, b),
        pearson_counts=pearson_counts,
    )


# ---------------------------------------------------------------------------
# Import-time parity check: our forward BVN == live copula (2-leg case)
# ---------------------------------------------------------------------------


def _parity_check() -> float:
    """Assert our bvn_cdf matches the live gaussian_copula_joint_prob to < 2e-4."""
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from combomaker.pricing.copula import gaussian_copula_joint_prob  # noqa: WPS433

    worst = 0.0
    for pa in (0.3, 0.5, 0.7):
        for pb in (0.3, 0.5, 0.7):
            for rho in (-0.4, -0.1, 0.0, 0.2, 0.5):
                live = gaussian_copula_joint_prob(
                    [pa, pb], np.array([[1.0, rho], [rho, 1.0]])
                )
                mine = bvn_cdf(norm.ppf(pa), norm.ppf(pb), rho)
                worst = max(worst, abs(live - mine))
    return worst


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def fmt(x: float | None, w: int = 6, d: int = 3) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return " " * (w - 3) + "n/a"
    return f"{x:>{w}.{d}f}"


def print_table(title: str, results: list[PairResult]) -> None:
    print(f"\n=== {title} ===")
    header = (
        f"{'corners>=':>10} {'goals':>8} {'n':>6} "
        f"{'P(A)':>6} {'P(B)':>6} {'P(A&B)':>7} "
        f"{'rho_tet':>8} {'CI95':>16} {'phi':>7} {'r_counts':>9}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        ci = (
            f"[{fmt(r.rho_tet_lo, 5, 2)},{fmt(r.rho_tet_hi, 5, 2)}]"
            if r.rho_tet_lo is not None
            else " " * 16
        )
        print(
            f"{r.corner_thr:>10} {r.goal_label:>8} {r.n:>6} "
            f"{fmt(r.p_a)} {fmt(r.p_b)} {fmt(r.p_ab, 7)} "
            f"{fmt(r.rho_tet, 8)} {ci:>16} {fmt(r.phi, 7)} {fmt(r.pearson_counts, 9)}"
        )


def main() -> None:
    worst = _parity_check()
    print(f"[parity] forward BVN vs live gaussian_copula_joint_prob: worst |delta| = {worst:.2e}")
    assert worst < 2e-4, f"forward BVN drifted from live copula ({worst:.2e})"

    games = load_games()
    rng = np.random.default_rng(SEED)

    print(f"\nLoaded {len(games)} club matches across {len(LEAGUES)} leagues, seasons {SEASONS}.")
    per_league_n = {code: sum(1 for g in games if g.league == code) for code in LEAGUES}
    for code, name in LEAGUES.items():
        print(f"  {name:>16}: n={per_league_n[code]}")

    tc = [g.total_corners for g in games]
    tg = [g.total_goals for g in games]
    print(
        f"\nPooled means: total_corners={statistics.mean(tc):.2f}, "
        f"total_goals={statistics.mean(tg):.2f}, "
        f"raw Pearson(total_corners,total_goals)={np.corrcoef(tc,tg)[0,1]:+.4f}"
    )

    goal_labels = [f"goals>={g}" for g in GOAL_THRESHOLDS] + ["btts"]

    # Pooled across all leagues.
    pooled: list[PairResult] = []
    for ct in CORNER_THRESHOLDS:
        for gl in goal_labels:
            pooled.append(measure_pair(games, ct, gl, rng))
    print_table("POOLED (all 5 leagues)", pooled)

    # Per-league stability (tetrachoric rho only, no bootstrap for brevity).
    print("\n=== PER-LEAGUE rho_tetrachoric (stability / dispersion check) ===")
    print("     (corners>=9 x goals>=3 is the marquee traded pair)")
    hdr = f"{'pair':>22} " + " ".join(f"{name.split()[0]:>10}" for name in LEAGUES.values())
    print(hdr)
    print("-" * len(hdr))
    for ct in CORNER_THRESHOLDS:
        for gl in goal_labels:
            cells = []
            for code in LEAGUES:
                sub = [g for g in games if g.league == code]
                a, b = indicator_arrays(sub, ct, gl)
                r = tetrachoric_rho(float(a.mean()), float(b.mean()), float((a * b).mean()))
                cells.append(fmt(r, 10, 2))
            label = f"c>={ct} x {gl}"
            print(f"{label:>22} " + " ".join(cells))

    # Dispersion summary across leagues for each pair.
    print("\n=== CROSS-LEAGUE DISPERSION (min / mean / max of per-league rho_tet) ===")
    for ct in CORNER_THRESHOLDS:
        for gl in goal_labels:
            vals = []
            for code in LEAGUES:
                sub = [g for g in games if g.league == code]
                a, b = indicator_arrays(sub, ct, gl)
                r = tetrachoric_rho(float(a.mean()), float(b.mean()), float((a * b).mean()))
                if r is not None:
                    vals.append(r)
            if vals:
                print(
                    f"  c>={ct} x {gl:>8}: "
                    f"min={min(vals):+.3f} mean={statistics.mean(vals):+.3f} "
                    f"max={max(vals):+.3f} (spread {max(vals)-min(vals):.3f})"
                )

    # Headline: the shipped config values these would replace.
    shipped = {
        "goals>=2": 0.00,
        "goals>=3": 0.00,
        "goals>=4": 0.00,
        "btts": 0.00,
    }
    print("\n=== HEADLINE vs shipped config (soccer:corners|total = 0.00, btts|corners = 0.00) ===")
    for r in pooled:
        ship = shipped.get(r.goal_label, 0.00)
        flag = ""
        if r.rho_tet_lo is not None and r.rho_tet_hi is not None:
            if r.rho_tet_lo <= 0.0 <= r.rho_tet_hi:
                flag = "CI straddles 0  -> shipped 0.00 DEFENSIBLE"
            elif r.rho_tet_lo > 0.0:
                flag = "CI > 0          -> positive, consider promote"
            else:
                flag = "CI < 0          -> negative"
        print(
            f"  corners>={r.corner_thr} x {r.goal_label:>8}: "
            f"rho_tet={fmt(r.rho_tet,7)}  shipped={ship:+.2f}  {flag}"
        )


if __name__ == "__main__":
    main()
