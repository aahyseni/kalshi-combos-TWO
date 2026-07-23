"""Correlation-adaptive cap derivation (risk/cap_family.py)."""
from __future__ import annotations

import math
from fractions import Fraction

import pytest

from combomaker.risk.cap_family import (
    K_DAILY,
    K_DD,
    K_TRIP,
    KILL_ANCHOR,
    CapFractions,
    derive_cap_fractions,
    g_eff_from_cross_rho,
)


def _sigma_day_over_bank(c: CapFractions) -> float:
    # reconstruct sigma_day/bank from the deployed f_slate + measurement
    assert c.sigma1 is not None and c.g_eff is not None
    return c.sigma1 * c.slate_loss_frac / math.sqrt(c.g_eff)


# --- unit: the KILL anchor holds when the solve is in force -----------------------

@pytest.mark.parametrize("sigma1,ge", [(0.20, 12.0), (0.30, 9.0), (0.45, 6.0), (0.10, 3.0)])
def test_solved_f_slate_puts_kill_at_anchor(sigma1: float, ge: float) -> None:
    # provisional=False so the raw solve stands (no clamp)
    c = derive_cap_fractions(expected_games=10, sigma1=sigma1, g_eff=ge, provisional=False)
    assert abs(K_TRIP * _sigma_day_over_bank(c) - KILL_ANCHOR) < 1e-9
    assert abs(c.hard_trip_frac - KILL_ANCHOR) < 1e-12


def test_z_anchors_are_fixed_regardless_of_measurement() -> None:
    for kw in ({}, dict(sigma1=0.3, g_eff=9.0, provisional=False), dict(sigma1=0.9, g_eff=2.0)):
        c = derive_cap_fractions(expected_games=12, **kw)  # type: ignore[arg-type]
        assert abs(c.hard_trip_frac - 0.12) < 1e-12
        assert abs(c.drawdown_frac - 0.096) < 1e-12       # 4/5 * 0.12
        assert abs(c.daily_loss_frac - 0.072) < 1e-12     # 3/5 * 0.12
        assert abs(c.sigma_day_over_bank - 0.024) < 1e-12
        assert c.per_combo_loss_frac == 0.01
        assert c.ruin_floor_frac == 0.30


def test_game_is_slate_over_expected_games() -> None:
    c = derive_cap_fractions(expected_games=12)
    assert abs(c.game_loss_frac - c.slate_loss_frac / 12) < 1e-15


def test_halts_are_k_multiples_of_sigma_day() -> None:
    c = derive_cap_fractions(expected_games=8)
    assert abs(c.daily_loss_frac - K_DAILY * c.sigma_day_over_bank) < 1e-12
    assert abs(c.drawdown_frac - K_DD * c.sigma_day_over_bank) < 1e-12


# --- bootstrap + provisional clamp ------------------------------------------------

def test_bootstrap_when_unmeasured() -> None:
    c = derive_cap_fractions(expected_games=12)   # sigma1/g_eff None
    assert not c.measured and c.provisional
    assert c.slate_loss_frac == 0.15
    assert abs(c.game_loss_frac - 0.15 / 12) < 1e-15

def test_provisional_clamps_a_large_solved_slate() -> None:
    # low vol + high diversification wants f_slate >> 0.15, but provisional clamps
    c = derive_cap_fractions(expected_games=12, sigma1=0.10, g_eff=11.0, provisional=True)
    solved = KILL_ANCHOR * math.sqrt(11.0) / (K_TRIP * 0.10)
    assert solved > 0.15
    assert c.slate_loss_frac == 0.15
    # lifting provisional lets the formula earn the bigger cap
    c2 = derive_cap_fractions(expected_games=12, sigma1=0.10, g_eff=11.0, provisional=False)
    assert abs(c2.slate_loss_frac - solved) < 1e-12


# --- property: no measured worsening ever RAISES a cap ----------------------------

def test_raising_sigma1_never_increases_slate_or_game() -> None:
    prev = None
    for s in (0.15, 0.20, 0.30, 0.45, 0.60):
        c = derive_cap_fractions(expected_games=10, sigma1=s, g_eff=8.0, provisional=False)
        if prev is not None:
            assert c.slate_loss_frac <= prev + 1e-12
            assert c.game_loss_frac <= prev / 10 + 1e-12
        prev = c.slate_loss_frac

def test_raising_cross_rho_never_increases_slate() -> None:
    prev = None
    for rho in (0.00, 0.02, 0.05, 0.10, 0.20):
        ge = g_eff_from_cross_rho(12, rho)
        c = derive_cap_fractions(expected_games=12, sigma1=0.30, g_eff=ge,
                                 cross_game_rho=rho, provisional=False)
        if prev is not None:
            assert c.slate_loss_frac <= prev + 1e-12
        prev = c.slate_loss_frac

def test_f_slate_never_exceeds_the_solved_value() -> None:
    # clamp/ratchet only ever REDUCE below the raw solve
    for prov in (True, False):
        c = derive_cap_fractions(expected_games=12, sigma1=0.30, g_eff=9.0, provisional=prov)
        solved = KILL_ANCHOR * math.sqrt(9.0) / (K_TRIP * 0.30)
        assert c.slate_loss_frac <= solved + 1e-12


# --- property: ratchet dominance (cross-rho >= gate blocks any increase) ----------

def test_ratchet_blocks_increase_when_cross_rho_at_or_above_gate() -> None:
    # solved wants to increase over last night, but cross-rho >= 0.05 -> HELD
    ge = g_eff_from_cross_rho(12, 0.02)  # low rho -> big solved
    c = derive_cap_fractions(expected_games=12, sigma1=0.20, g_eff=ge,
                             cross_game_rho=0.09, f_slate_prev=0.15, provisional=False)
    assert c.ratchet_held
    assert c.slate_loss_frac == 0.15

def test_ratchet_allows_increase_only_when_rho_below_gate() -> None:
    ge = g_eff_from_cross_rho(12, 0.02)
    c = derive_cap_fractions(expected_games=12, sigma1=0.20, g_eff=ge,
                             cross_game_rho=0.02, f_slate_prev=0.15, provisional=False)
    assert not c.ratchet_held
    assert c.slate_loss_frac > 0.15  # earned the increase with proven low rho

def test_ratchet_never_blocks_a_decrease() -> None:
    # a shrink (solved < prev) always flows through, gate or not
    ge = g_eff_from_cross_rho(12, 0.20)  # high rho -> small solved
    c = derive_cap_fractions(expected_games=12, sigma1=0.50, g_eff=ge,
                             cross_game_rho=0.20, f_slate_prev=0.15, provisional=False)
    assert not c.ratchet_held
    assert c.slate_loss_frac < 0.15


# --- G_eff estimator + MC headroom + Fraction boundary ----------------------------

def test_g_eff_from_cross_rho_endpoints() -> None:
    assert g_eff_from_cross_rho(12, 0.0) == 12.0            # independent -> n
    assert g_eff_from_cross_rho(12, 1.0) == 1.0            # fully correlated -> 1
    assert 1.0 < g_eff_from_cross_rho(12, 0.10) < 12.0
    # monotone decreasing in rho
    assert g_eff_from_cross_rho(12, 0.05) > g_eff_from_cross_rho(12, 0.15)

def test_mc_headroom_caps() -> None:
    c = derive_cap_fractions(expected_games=10, mc_directional=0.20, mc_det_max=0.15, mc_cvar=0.12)
    assert abs(c.directional_frac - 1.3 * 0.20) < 1e-12
    assert abs(c.portfolio_det_max_frac - 1.3 * 0.15) < 1e-12
    assert abs(c.portfolio_cvar_frac - 1.3 * 0.12) < 1e-12

def test_as_fractions_are_exact_fractions() -> None:
    c = derive_cap_fractions(expected_games=12, sigma1=0.30, g_eff=9.0, provisional=False,
                             mc_det_max=0.15)
    fr = c.as_fractions()
    assert all(isinstance(v, Fraction) for v in fr.values())
    assert fr["hard_trip_frac"] == Fraction(12, 100)
    # unsupplied MC caps are omitted (caller keeps prior/absolute)
    assert "directional_frac" not in fr


# --- sanity replay: diversified vs correlated night -------------------------------

def test_replay_diversified_night_spreads_across_games() -> None:
    # 12-game diversified night: low within-game vol, low cross-rho, measured
    ge = g_eff_from_cross_rho(12, 0.02)
    c = derive_cap_fractions(expected_games=12, sigma1=0.30, g_eff=ge,
                             cross_game_rho=0.02, provisional=False)
    # each game gets ~1/12 of the slate; none is the whole book
    assert abs(c.game_loss_frac - c.slate_loss_frac / 12) < 1e-15
    assert c.game_loss_frac < c.slate_loss_frac / 6  # can't let 2-3 games be the book

def test_replay_correlated_night_ratchets_down() -> None:
    ge_lo = g_eff_from_cross_rho(12, 0.02)
    calm = derive_cap_fractions(expected_games=12, sigma1=0.30, g_eff=ge_lo,
                                cross_game_rho=0.02, provisional=False)
    ge_hi = g_eff_from_cross_rho(12, 0.15)
    lumpy = derive_cap_fractions(expected_games=12, sigma1=0.30, g_eff=ge_hi,
                                 cross_game_rho=0.15, f_slate_prev=calm.slate_loss_frac,
                                 provisional=False)
    # the lumpy night deploys strictly less, before any fills
    assert lumpy.slate_loss_frac < calm.slate_loss_frac
    assert lumpy.game_loss_frac < calm.game_loss_frac
