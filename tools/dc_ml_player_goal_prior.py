"""Rank-4 check: what moneyline x player_goal correlation does the SHIPPED
Dixon-Coles scoreline model INDUCE, and how does it compare to the empirically
calibrated soccer scorer x team-win number (tools/calibrate_soccer_scorers.py)?

The DC path does not consume a scalar rho: player goals | n team goals ~
Binomial(n, q) (multinomial thinning), and the win indicator (n_A > n_B) is read
off the SAME scoreline, so a positive ml x player_goal correlation EMERGES from
the structure. This script reads that induced correlation off the shipped model
(imports pricing.dixon_coles; edits nothing) across representative matches and
star-scorer shares, then inverts it to the SAME copula rho the v1 fallback uses.

Run: C:/.../.venv/Scripts/python.exe tools/dc_ml_player_goal_prior.py
"""

from __future__ import annotations

import numpy as np

from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.pricing.dixon_coles import (
    MatchFormat,
    ModelParams,
    PlayerScores,
    Team,
    TeamWin,
    joint_probability,
    marginal_probability,
)


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


def induced_rho(lam_a: float, lam_b: float, share: float) -> tuple[float, float, float, float]:
    params = ModelParams(
        lam_a=lam_a, lam_b=lam_b, dc_rho=-0.05, et_factor=0.0,
        match_format=MatchFormat.GROUP,
    )
    win = TeamWin(team=Team.A, include_et=False)
    ps = PlayerScores(team=Team.A, min_goals=1, include_et=False)
    p_win = marginal_probability(params, win)
    p_pl = marginal_probability(params, ps, share=share)
    p_joint = joint_probability(params, [(win, True), (ps, True)], {1: share})
    return p_win, p_pl, p_joint, implied_rho(p_win, p_pl, p_joint)


def main() -> None:
    print("DC-INDUCED moneyline x player_goal correlation (shipped model, dc_rho=-0.05)\n")
    print(f"{'lam_a':>6}{'lam_b':>6}{'share':>7}{'P(win)':>8}{'P(scr)':>8}{'P(both)':>9}{'rho':>8}")
    scenarios = [
        # (lam_a, lam_b, share)   home-fav / even / dog, star share tuned to P~0.35-0.55
        (1.7, 1.0, 0.35),
        (1.7, 1.0, 0.45),
        (1.5, 1.2, 0.40),
        (1.3, 1.3, 0.40),   # even game
        (1.1, 1.6, 0.45),   # team A is the underdog
        (2.0, 0.9, 0.40),   # strong home favorite
    ]
    rhos = []
    for la, lb, sh in scenarios:
        p_win, p_pl, p_joint, rho = induced_rho(la, lb, sh)
        rhos.append(rho)
        print(f"{la:>6.2f}{lb:>6.2f}{sh:>7.2f}{p_win:>8.3f}{p_pl:>8.3f}{p_joint:>9.3f}{rho:>8.3f}")
    print(f"\nDC-induced rho range: [{min(rhos):+.3f}, {max(rhos):+.3f}]  mean {np.mean(rhos):+.3f}")
    print("Shipped copula-fallback prior soccer:moneyline|player_goal = 0.50")
    print("Empirical (Understat, STAR frame) conditional-MLE = +0.49  99%CI [0.44, 0.54]")


if __name__ == "__main__":
    main()
