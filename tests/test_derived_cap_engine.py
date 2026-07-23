"""Adapter: adaptive-caps brain -> live RiskLimits (risk/derived_cap_engine.py).

FULLY ADAPTIVE: every cap axis comes from measured state + policy anchors ONLY;
no static config fraction governs (operator directive 2026-07-22). Book-cap floors
are the derived slate / drawdown budget, not constants."""
from __future__ import annotations

import dataclasses
import math
import random
from fractions import Fraction

from combomaker.ops.config import RiskConfig
from combomaker.risk.derived_cap_engine import DerivedCapEngine
from combomaker.risk.limits import LimitChecker
from combomaker.risk.pnl_measurement import GamePnl, NightPnl

_CAPS = (
    "per_combo_loss_frac", "game_loss_frac", "slate_loss_frac", "daily_loss_frac",
    "drawdown_frac", "hard_trip_frac", "directional_frac", "portfolio_det_max_frac",
    "portfolio_cvar_frac",
)


def _base():
    return RiskConfig().to_risk_limits()


def _synth(n_nights, gpn, sigma1, cross_rho, seed=3):
    rng = random.Random(seed)
    a = sigma1 * math.sqrt(max(0.0, cross_rho))
    b = sigma1 * math.sqrt(max(0.0, 1.0 - cross_rho))
    out = []
    for _ in range(n_nights):
        f = rng.gauss(0, 1)
        out.append(NightPnl([GamePnl(f"G{g}", (a * f + b * rng.gauss(0, 1)) * 100.0, 100.0)
                             for g in range(gpn)]))
    return out


# --- bootstrap derivation (pure anchors, base-independent) ---

def test_bootstrap_refresh_produces_policy_anchor_caps() -> None:
    lim, caps, est = DerivedCapEngine(_base()).refresh(expected_games=12, pnl_history=[])
    assert not est.stable and caps.provisional
    assert lim.slate_loss_frac == Fraction(15, 100)
    assert lim.game_loss_frac == Fraction(15, 100) / 12
    assert lim.per_combo_loss_frac == Fraction(1, 100)         # 1% policy anchor
    assert lim.daily_loss_frac == Fraction(72, 1000)           # 3sigma anchor (NOT config)
    assert lim.drawdown_frac == Fraction(96, 1000)             # 4sigma anchor
    assert lim.hard_trip_frac == Fraction(12, 100)             # 5sigma = KILL


def test_no_static_config_number_governs_base_independent() -> None:
    # arm the SAME slate against a tight base and a wide base -> identical derived
    # caps. Proves no hand-set config fraction leaks into the enforced result.
    tight = dataclasses.replace(_base(), **{k: Fraction(6, 100) for k in _CAPS})
    wide = dataclasses.replace(_base(), **{k: Fraction(90, 100) for k in _CAPS})
    lt, _, _ = DerivedCapEngine(tight).refresh(expected_games=12, pnl_history=[])
    lw, _, _ = DerivedCapEngine(wide).refresh(expected_games=12, pnl_history=[])
    for k in _CAPS:
        assert getattr(lt, k) == getattr(lw, k)


# --- adaptive book-cap floors (derived budget, not constants) ---

def test_book_floors_are_the_derived_budget_when_no_mc() -> None:
    lim, _, _ = DerivedCapEngine(_base()).refresh(expected_games=10, pnl_history=[])
    assert lim.directional_frac == lim.slate_loss_frac         # directional floor = slate
    assert lim.portfolio_det_max_frac == lim.slate_loss_frac   # det-max floor = slate
    assert lim.portfolio_cvar_frac == lim.drawdown_frac        # CVaR floor = 4s drawdown


def test_book_caps_use_1p3x_mc_when_above_floor() -> None:
    lim, _, _ = DerivedCapEngine(_base()).refresh(
        expected_games=10, pnl_history=[], mc_det_max=0.20, mc_cvar=0.15, mc_directional=0.30)
    assert lim.portfolio_det_max_frac == Fraction(26, 100)     # 1.3 * 0.20 > slate
    assert lim.portfolio_cvar_frac == Fraction(195, 1000)      # 1.3 * 0.15 > drawdown
    assert lim.directional_frac == Fraction(39, 100)           # 1.3 * 0.30 > slate


def test_book_caps_hold_derived_floor_when_mc_below_it() -> None:
    lim, _, _ = DerivedCapEngine(_base()).refresh(
        expected_games=10, pnl_history=[], mc_det_max=0.05)
    assert lim.portfolio_det_max_frac == lim.slate_loss_frac   # 1.3*0.05 < slate floor


# --- deploy caps breathe with earned evidence (adaptive, not clamped) ---

def test_deploy_slate_breathes_when_diversification_is_measured() -> None:
    hist = _synth(50, 12, sigma1=0.18, cross_rho=0.02)
    lim, caps, est = DerivedCapEngine(_base()).refresh(expected_games=12, pnl_history=hist)
    assert est.stable and not caps.provisional
    assert lim.slate_loss_frac > Fraction(15, 100)             # earned, above bootstrap


# --- passthrough + type invariants ---

def test_passthrough_fields_unchanged() -> None:
    base = _base()
    lim, _, _ = DerivedCapEngine(base).refresh(expected_games=12, pnl_history=[])
    assert lim.max_contracts_per_quote == base.max_contracts_per_quote
    assert lim.max_open_quotes == base.max_open_quotes
    assert lim.max_market_delta_contracts == base.max_market_delta_contracts
    assert lim.max_notional_per_quote_dollars == base.max_notional_per_quote_dollars


def test_ratchet_state_persists_across_refreshes() -> None:
    eng = DerivedCapEngine(_base())
    eng.refresh(expected_games=12, pnl_history=[])
    assert eng.f_slate_prev == 0.15                            # bootstrap set it


def test_all_swapped_caps_are_exact_fractions() -> None:
    lim, _, _ = DerivedCapEngine(_base()).refresh(expected_games=12, pnl_history=[])
    for k in _CAPS:
        assert isinstance(getattr(lim, k), Fraction)


def test_limit_checker_set_limits_swaps_atomically() -> None:
    base = _base()
    checker = LimitChecker(base)
    lim, _, _ = DerivedCapEngine(base).refresh(expected_games=12, pnl_history=[])
    checker.set_limits(lim)
    assert checker.limits.slate_loss_frac == Fraction(15, 100)
    assert checker.limits is lim
