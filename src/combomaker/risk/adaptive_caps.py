"""Correlation-adaptive caps — the nightly refresh brain (CLAUDE.md North Star,
pillar composition). Runs the SENSOR (`pnl_measurement`) over realized per-game
P&L, feeds σ₁ / G_eff / cross-ρ into the cap-family FORMULA (`cap_family`), and
returns the ``CapFractions`` the live ``RiskLimits`` reads — plus the estimate
for telemetry. The sensor is the eyes, the formula is the constitution-bound
derivation, this is where they meet.

Injection into the live ``RiskLimits`` (shadow-mode first) is the wiring layer
that consumes this; it is deliberately kept OUT of here so the whole brain stays
a pure, testable function of (history, bankroll, tonight's slate, projected MC).
"""
from __future__ import annotations

from combomaker.risk.cap_family import CapFractions, derive_cap_fractions
from combomaker.risk.pnl_measurement import (
    NightPnl,
    VolCorrEstimate,
    estimate_vol_corr,
)


def compute_nightly_caps(
    *,
    pnl_history: list[NightPnl],
    expected_games: int,
    f_slate_prev: float | None = None,
    mc_directional: float | None = None,
    mc_det_max: float | None = None,
    mc_cvar: float | None = None,
    force_provisional: bool = False,
) -> tuple[CapFractions, VolCorrEstimate]:
    """Tonight's derived caps + the measurement behind them.

    ``force_provisional`` clamps to the bootstrap regardless of the estimate —
    used when the allowlist just expanded to a family whose correlation is not
    yet trusted (a sport/prop family must never out-run its measured ρ). The
    ratchet still lives in ``derive_cap_fractions`` (an increase over
    ``f_slate_prev`` is blocked unless measured cross-ρ is below the gate)."""
    est = estimate_vol_corr(pnl_history)
    provisional = force_provisional or not est.stable
    caps = derive_cap_fractions(
        expected_games=expected_games,
        sigma1=est.sigma1,
        g_eff=est.g_eff,
        cross_game_rho=est.cross_game_rho,
        within_game_rho=est.within_game_rho,
        f_slate_prev=f_slate_prev,
        provisional=provisional,
        mc_directional=mc_directional,
        mc_det_max=mc_det_max,
        mc_cvar=mc_cvar,
    )
    return caps, est
