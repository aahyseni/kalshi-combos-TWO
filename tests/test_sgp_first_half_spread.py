"""Regression: 1H-spread × full-time pairs (first_half_spread|{spread,moneyline,total}).

``KXWC1HSPREAD`` (first-half spread = 1H goal margin) used to classify UNKNOWN, so
every 1H-spread combo fell to the flat same_event_rho +0.6 fallback — a wrong-signed
default for a pair that flips sign on team orientation. The spread NAMES a team
(TEAM+line-digits suffix), so spread|spread and spread|moneyline resolve to
``:same`` (both legs name one team, +ρ) vs ``:opp`` (different teams,
near-mutually-exclusive −ρ); spread|total is orientation-free (+ρ: a 1H lead needs
1H goals). Priors measured on 8,981 club matches
(tools/calibrate_soccer_1h_spread.py; results_soccer.md §2).
"""

from __future__ import annotations

import pytest

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg


def soccer_params() -> SgpParams:
    """SgpParams from the SHIPPED config, exactly like PricingEngine wiring."""
    c = CorrelationConfig()
    return SgpParams(
        pair_rho=dict(c.pair_rho),
        default_rho=c.same_event_rho,
        cross_event_rho=c.cross_event_rho,
        typed_uncertainty=c.typed_rho_uncertainty,
        untyped_uncertainty=c.untyped_rho_uncertainty,
        pair_uncertainty=dict(c.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in c.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in c.oriented_curve.items()},
        oriented_curve_uncertainty=dict(c.oriented_curve_uncertainty),
    )


G = "26JUL09FRAMAR"


def _leg(market: str, side: str = "yes") -> RfqLeg:
    game = market.split("-")[1]
    return RfqLeg(market, f"EV-{game}", side, None)


FHS_FRA = f"KXWC1HSPREAD-{G}-FRA2"   # France leads at half by over 1.5 (margin>=2)
FT_SPR_FRA = f"KXWCSPREAD-{G}-FRA2"  # France FT spread, same team
FT_SPR_MAR = f"KXWCSPREAD-{G}-MAR2"  # Morocco FT spread, opposite team
FT_ML_FRA = f"KXWCGAME-{G}-FRA"      # France FT winner, same team
FT_ML_MAR = f"KXWCGAME-{G}-MAR"      # Morocco FT winner, opposite team
FT_TOTAL = f"KXWCTOTAL-{G}-3"        # FT over 2.5


# --- spread × spread: sign flips on same vs opposite team --------------------


def test_spread_same_team_positive() -> None:
    out = build_sgp_correlation((_leg(FHS_FRA), _leg(FT_SPR_FRA)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(0.78)
    assert out.corr_low[0, 1] == pytest.approx(0.66)   # 0.78 - 0.12
    assert out.corr_high[0, 1] == pytest.approx(0.90)  # 0.78 + 0.12
    assert out.typed_pairs == 1 and out.untyped_pairs == 0
    assert any(":same" in n for n in out.notes)


def test_spread_opposite_team_negative() -> None:
    out = build_sgp_correlation((_leg(FHS_FRA), _leg(FT_SPR_MAR)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(-0.65)  # was flat +0.6 (wrong sign)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0
    assert any(":opp" in n for n in out.notes)


# --- spread × moneyline: same/opposite team ----------------------------------


def test_spread_moneyline_same_team_positive() -> None:
    out = build_sgp_correlation((_leg(FHS_FRA), _leg(FT_ML_FRA)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(0.74)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0
    assert any(":same" in n for n in out.notes)


def test_spread_moneyline_opposite_team_negative() -> None:
    out = build_sgp_correlation((_leg(FHS_FRA), _leg(FT_ML_MAR)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(-0.66)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0
    assert any(":opp" in n for n in out.notes)


def test_spread_moneyline_order_independent() -> None:
    # moneyline first, 1H-spread second: orientation keyed off the spread suffix.
    out = build_sgp_correlation((_leg(FT_ML_MAR), _leg(FHS_FRA)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(-0.66)


# --- spread × total: orientation-free, positive ------------------------------


def test_spread_total_positive_orientation_free() -> None:
    out = build_sgp_correlation((_leg(FHS_FRA), _leg(FT_TOTAL)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(0.52)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0


# --- fail-closed: unparseable suffix falls back to flat, never a guessed sign -


def test_unparseable_spread_suffix_falls_back_not_a_guess() -> None:
    # Empty suffix cannot be resolved to a team -> flat default, never a guessed
    # orientation (quiet-failure defense #2: widen, don't invent).
    garbage = f"KXWC1HSPREAD-{G}-"
    out = build_sgp_correlation((_leg(garbage), _leg(FT_SPR_FRA)), [(0, 1)], soccer_params())
    assert out.untyped_pairs == 1 and out.typed_pairs == 0
    assert out.corr[0, 1] == pytest.approx(soccer_params().default_rho)


def test_draw_winner_in_spread_pair_resolves_tie() -> None:
    # M2 zero-gaps wire (2026-07-12): 1H-spread × FT-DRAW is now MEASURED
    # (-0.44, pooled both teams) — the flat +0.6 fallback this shape used to
    # hit had the WRONG SIGN (a 2-goal 1H lead makes a FT draw unlikely).
    draw_ml = f"KXWCGAME-{G}-TIE"
    out = build_sgp_correlation((_leg(FHS_FRA), _leg(draw_ml)), [(0, 1)], soccer_params())
    assert out.typed_pairs == 1 and out.untyped_pairs == 0
    assert out.corr[0, 1] == pytest.approx(-0.44)


# --- config carries the calibrated entries -----------------------------------


def test_config_carries_first_half_spread_entries() -> None:
    soccer = CorrelationConfig().pair_rho_by_sport["soccer"]
    assert soccer["first_half_spread|spread:same"] == 0.78
    assert soccer["first_half_spread|spread:opp"] == -0.65
    assert soccer["first_half_spread|moneyline:same"] == 0.74
    assert soccer["first_half_spread|moneyline:opp"] == -0.66
    assert soccer["first_half_spread|total"] == 0.52


def test_config_carries_first_half_spread_bands() -> None:
    bands = CorrelationConfig().pair_rho_uncertainty
    assert bands["soccer:first_half_spread|spread:same"] == 0.12
    assert bands["soccer:first_half_spread|spread:opp"] == 0.15
    assert bands["soccer:first_half_spread|moneyline:same"] == 0.12
    assert bands["soccer:first_half_spread|moneyline:opp"] == 0.15
    assert bands["soccer:first_half_spread|total"] == 0.15
