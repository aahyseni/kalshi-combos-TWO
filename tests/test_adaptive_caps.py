"""Nightly refresh brain (risk/adaptive_caps.py) — sensor -> formula end to end."""
from __future__ import annotations

import math
import random

from combomaker.risk.adaptive_caps import compute_nightly_caps
from combomaker.risk.pnl_measurement import GamePnl, NightPnl

PREM = 100.0


def _synth(n_nights: int, gpn: int, sigma1: float, cross_rho: float,
           seed: int = 3) -> list[NightPnl]:
    rng = random.Random(seed)
    a = sigma1 * math.sqrt(max(0.0, cross_rho))
    b = sigma1 * math.sqrt(max(0.0, 1.0 - cross_rho))
    out = []
    for _ in range(n_nights):
        f = rng.gauss(0, 1)
        out.append(NightPnl([GamePnl(f"G{g}", (a * f + b * rng.gauss(0, 1)) * PREM, PREM)
                             for g in range(gpn)]))
    return out


def test_no_history_bootstraps() -> None:
    caps, est = compute_nightly_caps(pnl_history=[], expected_games=12)
    assert not est.stable and caps.provisional
    assert caps.slate_loss_frac == 0.15
    assert abs(caps.game_loss_frac - 0.15 / 12) < 1e-15


def test_diversified_history_earns_a_bigger_slate() -> None:
    # stable, low within-game vol + low cross-rho -> formula wants > 0.15
    hist = _synth(50, 12, sigma1=0.18, cross_rho=0.02)
    caps, est = compute_nightly_caps(pnl_history=hist, expected_games=12, f_slate_prev=0.15)
    assert est.stable and not caps.provisional
    assert caps.slate_loss_frac > 0.15                 # earned with evidence
    assert not caps.ratchet_held


def test_correlated_history_ratchets_below_prior() -> None:
    # a hot correlated regime measured -> cross-rho above the gate; caps can't rise
    hist = _synth(50, 12, sigma1=0.30, cross_rho=0.30)
    caps, est = compute_nightly_caps(pnl_history=hist, expected_games=12, f_slate_prev=0.15)
    assert est.stable
    # either the solved value is already below prior (shrunk by low G_eff), or the
    # gate holds it — never an increase on a correlated night
    assert caps.slate_loss_frac <= 0.15 + 1e-12


def test_force_provisional_overrides_a_stable_read() -> None:
    # allowlist just expanded to an unvalidated family -> clamp regardless
    hist = _synth(50, 12, sigma1=0.18, cross_rho=0.02)
    caps, est = compute_nightly_caps(pnl_history=hist, expected_games=12,
                                     f_slate_prev=0.15, force_provisional=True)
    assert est.stable                                   # the data IS stable
    assert caps.provisional and caps.slate_loss_frac == 0.15  # but we clamp anyway


def test_mc_book_caps_flow_through() -> None:
    caps, _ = compute_nightly_caps(pnl_history=[], expected_games=10,
                                   mc_det_max=0.12, mc_cvar=0.10, mc_directional=0.15)
    assert caps.portfolio_det_max_frac is not None
    assert abs(caps.portfolio_det_max_frac - 1.3 * 0.12) < 1e-12
