"""Pillar 2 of the correlation-adaptive risk system (CLAUDE.md North Star): the
SENSOR. Estimates the calibrated inputs the cap family consumes from realized
per-game P&L — never from a P&L *window* for a level refit; these are second
moments (vol / correlation), the structural inputs that size risk.

Outputs (`VolCorrEstimate`):
  sigma1          per-$-of-premium WITHIN-game P&L std (lower = more diversified)
  cross_game_rho  realized correlation of per-game P&L ACROSS games on a night
                  (cross-game parlays are themselves a source of this — a
                  parlay-heavy book measures high cross-rho -> low G_eff)
  g_eff           effective INDEPENDENT games = n/(1+(n-1)*max(0,cross_rho))
  within_game_rho optional: correlation of combos INSIDE one game (what leg
                  diversification lowers); None unless per-combo P&L supplied
  stable          enough nights/observations to trust -> feeds cap-family
                  `provisional = not stable` (unstable => bootstrap clamp)

Attribution: a combo's realized P&L is split across the games it touches in
proportion to its per-game premium-at-risk, so the per-game series sums back to
the book total (a same-game combo lands on one game; a cross-game parlay couples
its games, which is where cross-rho comes from).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GamePnl:
    """One game's realized outcome on one night."""
    game_key: str
    realized_pnl: float       # dollars (signed)
    premium_at_risk: float    # dollars deployed on this game (> 0)


@dataclass(frozen=True)
class NightPnl:
    """All games' realized P&L for one settled night."""
    games: list[GamePnl] = field(default_factory=list)


@dataclass(frozen=True)
class VolCorrEstimate:
    sigma1: float | None
    cross_game_rho: float | None
    g_eff: float | None
    within_game_rho: float | None
    n_nights: int
    n_game_obs: int
    n_multi_game_nights: int
    median_games_per_night: float
    stable: bool


# Minimum evidence before the estimate is trusted (else the cap family stays on
# the provisional bootstrap). Second-moment estimates need real breadth.
MIN_NIGHTS = 10
MIN_GAME_OBS = 30
MIN_MULTI_GAME_NIGHTS = 5


def estimate_vol_corr(
    nights: list[NightPnl],
    *,
    min_nights: int = MIN_NIGHTS,
    min_game_obs: int = MIN_GAME_OBS,
    min_multi_game_nights: int = MIN_MULTI_GAME_NIGHTS,
    within_game_rho: float | None = None,
) -> VolCorrEstimate:
    """Estimate sigma1 / cross_game_rho / G_eff from realized per-game P&L.

    sigma1 = std of per-$-premium per-game returns r = pnl / premium.
    cross_game_rho = pooled same-night pairwise correlation of those returns.
    G_eff = median_n / (1 + (median_n - 1) * max(0, cross_rho))."""
    returns: list[float] = []                 # r_{game,night} across everything
    per_night_returns: list[list[float]] = []  # grouped by night (for cross-rho)
    for night in nights:
        rs = [g.realized_pnl / g.premium_at_risk
              for g in night.games if g.premium_at_risk > 0]
        if rs:
            per_night_returns.append(rs)
            returns.extend(rs)

    n_nights = len(per_night_returns)
    n_game_obs = len(returns)
    games_per_night = [len(rs) for rs in per_night_returns]
    n_multi = sum(1 for k in games_per_night if k >= 2)
    median_n = statistics.median(games_per_night) if games_per_night else 0.0

    stable = (
        n_nights >= min_nights
        and n_game_obs >= min_game_obs
        and n_multi >= min_multi_game_nights
    )
    if n_game_obs < 2:
        return VolCorrEstimate(None, None, None, within_game_rho, n_nights,
                               n_game_obs, n_multi, median_n, False)

    sigma1 = statistics.stdev(returns)
    grand_mean = statistics.fmean(returns)

    # Pooled same-night pairwise covariance -> cross-game rho (normalized by
    # sigma1^2). Each night contributes its within-night centered products.
    num = 0.0
    den = 0
    for rs in per_night_returns:
        if len(rs) < 2:
            continue
        centered = [r - grand_mean for r in rs]
        for i in range(len(centered)):
            for j in range(i + 1, len(centered)):
                num += centered[i] * centered[j]
                den += 1
    cross_rho: float | None
    if den == 0 or sigma1 <= 0:
        cross_rho = None
        median_for_geff = max(1.0, median_n)
        g_eff: float | None = median_for_geff
    else:
        cov = num / den
        cross_rho = max(-1.0, min(1.0, cov / (sigma1 * sigma1)))
        median_for_geff = max(1.0, median_n)
        denom = 1.0 + (median_for_geff - 1.0) * max(0.0, cross_rho)
        g_eff = min(median_for_geff, max(1.0, median_for_geff / denom))

    # Not trusted yet -> hand the cap family None so it bootstraps (provisional).
    if not stable:
        return VolCorrEstimate(None, cross_rho, None, within_game_rho, n_nights,
                               n_game_obs, n_multi, median_n, False)

    return VolCorrEstimate(sigma1, cross_rho, g_eff, within_game_rho, n_nights,
                           n_game_obs, n_multi, median_n, True)
