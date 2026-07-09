"""Calibrate SOCCER player-scorer SGP correlations from Understat (ranks 1/2 + gold).

Reads the local Understat cache (data/history/understat/, populated by
tools/fetch_understat.py). Per match we have, from `rosters`, each player's
goals / own_goals / xG / minutes / side, and from the league `dates` feed the
match result, total, and Understat's devigged model forecast {w,d,l}.

Four pairs (per the calibration directive):
  1. anytime-scorer x their team winning
  2. anytime-scorer x game total over 2.5
  3. two TEAMMATES both to score (same side)
  4. two OPPOSING scorers (one per team)

Two estimators for each, mirroring the repo pipeline:
  * Rank 1/2 POOLED: empirical P(A),P(B),P(A∩B) -> implied copula rho (bisection
    through combomaker's gaussian_copula_joint_prob) + 99% binomial CI.
  * GOLD conditional-MLE: every observation carries its OWN implied marginals
    (scorer prob from that player's match xG via 1-exp(-xG); team-win prob from
    the Understat forecast; over-2.5 prob from a team-total-xG Poisson), a single
    copula rho fit by MLE, OOS-gated by held-out season log-loss vs independence.
    This is the double-counting-safe number the directive asks for (condition on
    an implied scorer probability instead of pooling heterogeneous players).

Run: C:/.../.venv/Scripts/python.exe tools/calibrate_soccer_scorers.py
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy.special import ndtr, ndtri, owens_t
from scipy.stats import poisson

from combomaker.pricing.copula import gaussian_copula_joint_prob

CACHE = Path(__file__).resolve().parents[1] / "data" / "history" / "understat"
Arr = NDArray[np.float64]
_Z99 = 2.576
_THREAT_XG = 0.20  # a "genuine goal threat": >=0.20 xG in the match


# ------------------------------------------------------------- rank-2 inversion

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


# ------------------------------------------------------------------ data model

@dataclass
class PlayerObs:
    xg: float
    scored: bool          # proper goal (own goals excluded)
    is_home: bool
    minutes: int


@dataclass
class MatchObs:
    season: int
    total_over25: bool    # FT total >= 3
    home_win: bool
    away_win: bool
    p_home_win: float     # Understat forecast (devigged)
    p_away_win: float
    xg_home_team: float   # sum team xG (for over-2.5 implied marginal)
    xg_away_team: float
    players: list[PlayerObs]


def load_matches() -> list[MatchObs]:
    out: list[MatchObs] = []
    seen: set[str] = set()
    for lpath in sorted(CACHE.glob("league_*.json")):
        ld = json.loads(lpath.read_text(encoding="utf-8"))
        try:
            season = int(lpath.stem.split("_")[-1])
        except ValueError:
            season = 0
        for d in ld["dates"]:
            if not d.get("isResult"):
                continue
            mid = d["id"]
            if mid in seen:
                continue
            mpath = CACHE / f"match_{mid}.json"
            if not mpath.exists():
                continue
            seen.add(mid)
            gh = int(d["goals"]["h"]); ga = int(d["goals"]["a"])
            fc = d.get("forecast") or {}
            try:
                p_hw = float(fc["w"]); p_aw = float(fc["l"])
            except (KeyError, ValueError, TypeError):
                p_hw = p_aw = float("nan")
            md = json.loads(mpath.read_text(encoding="utf-8"))
            players: list[PlayerObs] = []
            xg_h = xg_a = 0.0
            for side in ("h", "a"):
                for pl in md["rosters"][side].values():
                    xg = float(pl.get("xG", 0) or 0)
                    if side == "h":
                        xg_h += xg
                    else:
                        xg_a += xg
                    mins = int(pl.get("time", 0) or 0)
                    if mins <= 0:
                        continue
                    players.append(PlayerObs(
                        xg=xg,
                        scored=int(pl.get("goals", 0) or 0) >= 1,
                        is_home=(side == "h"),
                        minutes=mins,
                    ))
            out.append(MatchObs(
                season=season,
                total_over25=(gh + ga) >= 3,
                home_win=gh > ga,
                away_win=ga > gh,
                p_home_win=p_hw,
                p_away_win=p_aw,
                xg_home_team=xg_h,
                xg_away_team=xg_a,
                players=players,
            ))
    return out


def p_score_from_xg(xg: float) -> float:
    return min(max(1.0 - math.exp(-xg), 1e-4), 0.999)


def p_over25_from_xg(xg_total: float) -> float:
    # P(N>=3), N ~ Poisson(team-total xG). A per-match implied over-2.5 marginal.
    p = 1.0 - float(poisson.cdf(2, xg_total))
    return min(max(p, 1e-4), 0.999)


# ------------------------------------------------------- observation builders

def obs_scorer_win(matches, *, threat_only: bool, star_only: bool):
    """(scored, team-won) with per-obs implied marginals."""
    rows = []
    for m in matches:
        if math.isnan(m.p_home_win):
            continue
        for side, won, pwin in (("h", m.home_win, m.p_home_win), ("a", m.away_win, m.p_away_win)):
            pls = [p for p in m.players if p.is_home == (side == "h")]
            if star_only:
                pls = sorted(pls, key=lambda p: p.xg, reverse=True)[:1]
            elif threat_only:
                pls = [p for p in pls if p.xg >= _THREAT_XG]
            for p in pls:
                rows.append((p_score_from_xg(p.xg), pwin, p.scored, won, m.season))
    return rows


def obs_scorer_over(matches, *, threat_only: bool, star_only: bool):
    rows = []
    for m in matches:
        p_over = p_over25_from_xg(m.xg_home_team + m.xg_away_team)
        pls = m.players
        for side in ("h", "a"):
            side_pls = [p for p in pls if p.is_home == (side == "h")]
            if star_only:
                side_pls = sorted(side_pls, key=lambda p: p.xg, reverse=True)[:1]
            elif threat_only:
                side_pls = [p for p in side_pls if p.xg >= _THREAT_XG]
            for p in side_pls:
                rows.append((p_score_from_xg(p.xg), p_over, p.scored, m.total_over25, m.season))
    return rows


def obs_teammates(matches, *, top2_only: bool):
    """Pairs of same-team players. top2_only: the two highest-xG per side."""
    rows = []
    for m in matches:
        for side in ("h", "a"):
            pls = [p for p in m.players if p.is_home == (side == "h") and p.xg >= _THREAT_XG]
            pls = sorted(pls, key=lambda p: p.xg, reverse=True)
            if top2_only:
                pls = pls[:2]
                if len(pls) == 2:
                    a, b = pls
                    rows.append((p_score_from_xg(a.xg), p_score_from_xg(b.xg),
                                 a.scored, b.scored, m.season))
            else:
                for i in range(len(pls)):
                    for j in range(i + 1, len(pls)):
                        a, b = pls[i], pls[j]
                        rows.append((p_score_from_xg(a.xg), p_score_from_xg(b.xg),
                                     a.scored, b.scored, m.season))
    return rows


def obs_opposing(matches, *, top1_only: bool):
    """One player from each team. top1_only: the top-xG player per side."""
    rows = []
    for m in matches:
        home = sorted([p for p in m.players if p.is_home and p.xg >= _THREAT_XG],
                      key=lambda p: p.xg, reverse=True)
        away = sorted([p for p in m.players if not p.is_home and p.xg >= _THREAT_XG],
                      key=lambda p: p.xg, reverse=True)
        if top1_only:
            if home and away:
                a, b = home[0], away[0]
                rows.append((p_score_from_xg(a.xg), p_score_from_xg(b.xg),
                             a.scored, b.scored, m.season))
        else:
            for a in home:
                for b in away:
                    rows.append((p_score_from_xg(a.xg), p_score_from_xg(b.xg),
                                 a.scored, b.scored, m.season))
    return rows


# ------------------------------------------------------------------- reporting

def pooled(rows) -> tuple[int, float, float, float, float, float, float]:
    """Rank-1/2: empirical rates -> implied copula rho + 99% CI."""
    n = len(rows)
    a = np.array([r[2] for r in rows], dtype=bool)
    b = np.array([r[3] for r in rows], dtype=bool)
    p_a = float(a.mean()); p_b = float(b.mean())
    p_ab = float((a & b).mean())
    rho = implied_rho(p_a, p_b, p_ab)
    se = math.sqrt(max(p_ab * (1 - p_ab), 1e-12) / n)
    lo = implied_rho(p_a, p_b, max(0.0, p_ab - _Z99 * se))
    hi = implied_rho(p_a, p_b, min(1.0, p_ab + _Z99 * se))
    return n, p_a, p_b, p_ab, rho, lo, hi


def conditional(rows, train_before: int):
    """Gold: per-obs implied marginals -> single copula rho MLE + OOS gate."""
    pa = np.array([r[0] for r in rows], dtype=np.float64)
    pb = np.array([r[1] for r in rows], dtype=np.float64)
    a = np.array([r[2] for r in rows], dtype=bool)
    b = np.array([r[3] for r in rows], dtype=bool)
    seas = np.array([r[4] for r in rows], dtype=int)
    tr = seas < train_before
    te = seas >= train_before
    rho_all, se_all = fit_rho(pa, pb, a, b)
    verdict = "n/a (single season)"
    rho_tr = float("nan")
    if tr.sum() >= 100 and te.sum() >= 100:
        rho_tr, _ = fit_rho(pa[tr], pb[tr], a[tr], b[tr])
        ll_fit = cell_loglik(pa[te], pb[te], a[te], b[te], rho_tr)
        ll_ind = cell_loglik(pa[te], pb[te], a[te], b[te], 0.0)
        verdict = (f"BEATS indep (fit {-ll_fit:.5f} < indep {-ll_ind:.5f})"
                   if ll_fit > ll_ind else
                   f"does NOT beat indep (fit {-ll_fit:.5f} vs {-ll_ind:.5f})")
    return rho_all, se_all, rho_tr, verdict, int(tr.sum()), int(te.sum())


def report(name: str, rows, train_before: int) -> None:
    n, p_a, p_b, p_ab, rho, lo, hi = pooled(rows)
    rho_c, se_c, rho_tr, verdict, ntr, nte = conditional(rows, train_before)
    print(f"\n### {name}   (n={n})")
    print(f"  POOLED : P(A)={p_a:.4f} P(B)={p_b:.4f} P(AandB)={p_ab:.4f} "
          f"indep={p_a*p_b:.4f}  implied rho={rho:+.3f}  99%CI[{lo:+.3f},{hi:+.3f}]")
    print(f"  GOLD   : conditional-MLE rho={rho_c:+.3f} (SE {se_c:.3f})  "
          f"train-rho={rho_tr:+.3f}  OOS: {verdict}  (train {ntr}/test {nte})")
    # odds-discount framing: how much a fair scorer price should shorten given win
    if p_a > 0 and p_b > 0:
        p_a_given_b = p_ab / p_b
        disc = (p_a_given_b - p_a) / p_a
        print(f"  P(A|B)={p_a_given_b:.4f} vs P(A)={p_a:.4f} -> "
              f"conditional lift {disc*100:+.1f}% (implied fair-odds shortening)")


def main() -> None:
    matches = load_matches()
    seasons = sorted({m.season for m in matches})
    train_before = seasons[-1] if len(seasons) > 1 else 9999
    print(f"=== SOCCER SCORERS (Understat) : {len(matches)} matches, seasons {seasons} ===")
    print(f"    OOS split: train seasons <{train_before}, test >= {train_before}")

    report("scorer x team-win  [STAR: top-xG player/team]",
           obs_scorer_win(matches, threat_only=False, star_only=True), train_before)
    report("scorer x team-win  [THREATS: xG>=0.20]",
           obs_scorer_win(matches, threat_only=True, star_only=False), train_before)
    report("scorer x team-win  [ALL players, time>0]",
           obs_scorer_win(matches, threat_only=False, star_only=False), train_before)

    report("scorer x over2.5   [STAR]",
           obs_scorer_over(matches, threat_only=False, star_only=True), train_before)
    report("scorer x over2.5   [THREATS: xG>=0.20]",
           obs_scorer_over(matches, threat_only=True, star_only=False), train_before)

    report("two TEAMMATES both score [top-2 xG/side]",
           obs_teammates(matches, top2_only=True), train_before)
    report("two TEAMMATES both score [all threat pairs]",
           obs_teammates(matches, top2_only=False), train_before)

    report("two OPPOSING scorers [top-1 xG each side]",
           obs_opposing(matches, top1_only=True), train_before)
    report("two OPPOSING scorers [all threat cross-pairs]",
           obs_opposing(matches, top1_only=False), train_before)


if __name__ == "__main__":
    main()
