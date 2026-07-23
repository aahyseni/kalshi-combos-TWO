"""Sensor for the correlation-adaptive caps (risk/pnl_measurement.py)."""
from __future__ import annotations

import math
import random

from combomaker.risk.pnl_measurement import (
    GamePnl,
    NightPnl,
    VolCorrEstimate,
    estimate_vol_corr,
)

PREM = 100.0


def _synth(n_nights: int, games_per_night: int, sigma1: float, cross_rho: float,
           seed: int = 7) -> list[NightPnl]:
    """r_{g,night} = a*F_night + b*e  ->  Var=a^2+b^2=sigma1^2,
    within-night Cov = a^2  ->  cross_rho = a^2/sigma1^2."""
    rng = random.Random(seed)
    a = sigma1 * math.sqrt(max(0.0, cross_rho))
    b = sigma1 * math.sqrt(max(0.0, 1.0 - cross_rho))
    nights = []
    for _ in range(n_nights):
        f = rng.gauss(0.0, 1.0)
        games = [GamePnl(f"G{g}", (a * f + b * rng.gauss(0.0, 1.0)) * PREM, PREM)
                 for g in range(games_per_night)]
        nights.append(NightPnl(games))
    return nights


def test_recovers_sigma1() -> None:
    est = estimate_vol_corr(_synth(40, 10, sigma1=0.30, cross_rho=0.0))
    assert est.stable
    assert est.sigma1 is not None and abs(est.sigma1 - 0.30) < 0.04


def test_independent_games_give_cross_rho_near_zero_and_g_eff_near_n() -> None:
    est = estimate_vol_corr(_synth(60, 10, sigma1=0.30, cross_rho=0.0))
    assert est.stable
    assert est.cross_game_rho is not None and abs(est.cross_game_rho) < 0.06
    assert est.g_eff is not None and est.g_eff > 8.0   # near 10


def test_correlated_night_gives_positive_cross_rho_and_collapsed_g_eff() -> None:
    est = estimate_vol_corr(_synth(60, 10, sigma1=0.30, cross_rho=0.30))
    assert est.stable
    assert est.cross_game_rho is not None and est.cross_game_rho > 0.18
    # G_eff collapses well below the 10 games (parlay-style coupling)
    assert est.g_eff is not None and est.g_eff < 4.5


def test_higher_cross_rho_monotonically_collapses_g_eff() -> None:
    lo = estimate_vol_corr(_synth(60, 10, 0.30, 0.05, seed=1))
    hi = estimate_vol_corr(_synth(60, 10, 0.30, 0.30, seed=1))
    assert lo.g_eff is not None and hi.g_eff is not None
    assert hi.g_eff < lo.g_eff


def test_unstable_when_too_few_nights_bootstraps() -> None:
    est = estimate_vol_corr(_synth(3, 10, 0.30, 0.05))   # < MIN_NIGHTS
    assert not est.stable
    assert est.sigma1 is None and est.g_eff is None       # -> cap family bootstraps
    # but the raw cross-rho signal is still surfaced for the pre-fill ratchet
    assert est.cross_game_rho is not None


def test_g_eff_within_bounds() -> None:
    for rho in (0.0, 0.05, 0.15, 0.5, 0.9):
        est = estimate_vol_corr(_synth(40, 8, 0.30, rho))
        if est.g_eff is not None:
            assert 1.0 <= est.g_eff <= est.median_games_per_night + 1e-9


def test_empty_history_is_all_none() -> None:
    est = estimate_vol_corr([])
    assert est == VolCorrEstimate(None, None, None, None, 0, 0, 0, 0.0, False)


def test_premium_weighted_returns_ignore_zero_premium_games() -> None:
    nights = [NightPnl([GamePnl("A", 5.0, 100.0), GamePnl("B", 0.0, 0.0)])]
    est = estimate_vol_corr(nights)          # B (0 premium) dropped, only A left
    assert est.n_game_obs == 1
    assert not est.stable                    # 1 obs -> unstable
