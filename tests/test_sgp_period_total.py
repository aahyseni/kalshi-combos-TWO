"""Regression: 1H-winner x 1H-total (first_half_moneyline|first_half_total).

Root-cause of the SUICOL World-Cup pick-off (ours 2.35c, independence 4.53c,
real maker 15.5c): this within-first-half pair had NO typed prior and fell to
the flat same_event_rho +0.6. For a TIE leg that is the WRONG SIGN — "1H under
0.5 goals" == "0-0" is a SUBSET of "1H tie", so yes(1H tie) x yes(1H under) are
strongly POSITIVELY correlated and their joint must sit ABOVE independence, not
below. The pair flips sign HARD on team-vs-tie (a 1H lead REQUIRES a goal, so
team x over is strong +rho; a tie contains 0-0, so tie x over is strong -rho),
resolved to :team / :tie in sgp.py. Magnitudes measured on 8,981 club matches
(tools/_measure_1h_pairs.py): both hit the +/-0.95 clamp.
"""

from __future__ import annotations

import numpy as np
import pytest

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.joint import price_joint_matrices
from combomaker.pricing.legs import LegBelief
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


def _leg(market: str, side: str = "yes") -> RfqLeg:
    game = market.split("-")[1]
    return RfqLeg(market, f"EV-{game}", side, None)


FH_TIE = "KXWC1H-26JUL07SUICOL-TIE"
FH_TEAM = "KXWC1H-26JUL07SUICOL-COL"
FH_TOTAL = "KXWC1HTOTAL-26JUL07SUICOL-1"  # 1H over 0.5


# --- resolver: sign flips on team vs tie -------------------------------------


def test_tie_leg_resolves_negative_prior() -> None:
    out = build_sgp_correlation((_leg(FH_TIE), _leg(FH_TOTAL)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(-0.95)  # was flat +0.6 (wrong sign)
    assert out.corr_low[0, 1] == pytest.approx(-0.95)  # clamp(-0.95-0.10)
    assert out.corr_high[0, 1] == pytest.approx(-0.85)  # -0.95+0.10
    assert out.typed_pairs == 1 and out.untyped_pairs == 0
    assert any(":tie" in n for n in out.notes)


def test_team_leg_resolves_positive_prior() -> None:
    out = build_sgp_correlation((_leg(FH_TEAM), _leg(FH_TOTAL)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(0.95)
    assert out.corr_low[0, 1] == pytest.approx(0.85)
    assert out.corr_high[0, 1] == pytest.approx(0.95)  # clamp(0.95+0.10)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0
    assert any(":team" in n for n in out.notes)


def test_order_independent() -> None:
    # total first, 1H-moneyline second: orientation keyed to the moneyline leg.
    out = build_sgp_correlation((_leg(FH_TOTAL), _leg(FH_TIE)), [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(-0.95)


def test_unparseable_winner_suffix_falls_back_to_flat_not_a_guess() -> None:
    # Empty suffix cannot be resolved to team or tie -> flat default, never a
    # guessed sign (quiet-failure defense #2: widen, don't invent).
    empty = "KXWC1H-26JUL07SUICOL-"
    out = build_sgp_correlation((_leg(empty), _leg(FH_TOTAL)), [(0, 1)], soccer_params())
    assert out.untyped_pairs == 1 and out.typed_pairs == 0
    assert out.corr[0, 1] == pytest.approx(soccer_params().default_rho)


# --- property: the fixed pair no longer prices below independence ------------


def _price(fixture: list[tuple[str, str, float]]) -> tuple[float, float]:
    """Returns (copula fair, independence product of selected-side probs)."""
    legs = [_leg(t, s) for t, s, _ in fixture]
    sides = [s for _, s, _ in fixture]
    yes_marg = [p if s == "yes" else 1.0 - p for _, s, p in fixture]  # YES marginal
    beliefs = [LegBelief(p, 0.005, "fx") for p in yes_marg]
    by_game: dict[str, list[int]] = {}
    for i, leg in enumerate(legs):
        by_game.setdefault(leg.market_ticker.split("-")[1], []).append(i)
    groups = [tuple(v) for v in by_game.values() if len(v) > 1]
    sgp = build_sgp_correlation(legs, groups, soccer_params(), marginals=yes_marg)
    est = price_joint_matrices(beliefs, sides, sgp.corr, sgp.corr_low, sgp.corr_high)
    return est.p, float(np.prod([p for _, _, p in fixture]))


def test_tie_under_subjoint_above_independence() -> None:
    # yes(1H tie) x no(1H over 0.5)=yes(1H under): under is a SUBSET of tie, so
    # the joint must EXCEED the independence product (it was ~0.5x before).
    fair, indep = _price([(FH_TIE, "yes", 0.47), (FH_TOTAL, "no", 0.37)])
    assert fair >= indep
    assert fair == pytest.approx(0.3553, abs=0.01)  # ~ Frechet upper min(.47,.37)


COMBO_A = [
    ("KXWC1H-26JUL07SUICOL-TIE", "yes", 0.47),
    ("KXWC1HTOTAL-26JUL07SUICOL-1", "no", 0.37),
    ("KXWCADVANCE-26JUL07SUICOL-COL", "yes", 0.61),
    ("KXWCGAME-26JUL07ARGEGY-ARG", "yes", 0.71),
    ("KXWCGOAL-26JUL07ARGEGY-ARGLMESSI10-1", "yes", 0.60),
]


def test_full_combo_a_no_longer_below_independence() -> None:
    # The SUICOL pick-off: was 2.35c vs independence 4.53c; must now sit at or
    # above independence (property, not a magic number).
    fair, indep = _price(COMBO_A)
    assert fair >= indep


def test_config_carries_period_total_orientation_entries() -> None:
    soccer = CorrelationConfig().pair_rho_by_sport["soccer"]
    assert soccer["first_half_moneyline|first_half_total:team"] == 0.95
    assert soccer["first_half_moneyline|first_half_total:tie"] == -0.95
