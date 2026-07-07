"""Tests for pricing/sgp.py: typed same-game-parlay correlation matrices."""

from __future__ import annotations

import numpy as np
import pytest

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.copula import is_psd
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

# Real production series prefixes so classify_leg types the legs as intended.
BTTS_TICKER = "KXWCBTTS-26JUL05MEXENG-BTTS"
TOTAL_TICKER = "KXWCTOTAL-26JUL05MEXENG-3"
GOAL_TICKER = "KXWCGOAL-26JUL05MEXENG-ENGHKANE9-1"
CORNERS_TICKER = "KXWCCORNERS-26JUL05MEXENG-10"
ADVANCE_TICKER = "KXWCADVANCE-26JUL05MEXENG-POR"
ML_NYY_TICKER = "KXMLBGAME-26JUL081840NYYTB-NYY"
ML_TB_TICKER = "KXMLBGAME-26JUL081840NYYTB-TB"
ML_WNBA_TICKER = "KXWNBAGAME-26JUL06NYLLVA-NYL"
WEIRD_TICKER = "KXHIGHNY-26JUL06-B90"  # classifies UNKNOWN
GOAL2_TICKER = "KXWCGOAL-26JUL05MEXENG-ENGHSMITH8-1"  # a 2nd scorer, same game
ML_SOCCER_TICKER = "KXWCGAME-26JUL05MEXENG-MEX"


def leg(market: str, event: str | None = None) -> RfqLeg:
    return RfqLeg(
        market_ticker=market, event_ticker=event, side="yes", yes_settlement_value_cc=None
    )


def params(
    pair_rho: dict[str, float] | None = None,
    *,
    default_rho: float = 0.3,
    cross_event_rho: float = 0.0,
    typed_uncertainty: float = 0.15,
    untyped_uncertainty: float = 0.25,
) -> SgpParams:
    return SgpParams(
        pair_rho=pair_rho if pair_rho is not None else {},
        default_rho=default_rho,
        cross_event_rho=cross_event_rho,
        typed_uncertainty=typed_uncertainty,
        untyped_uncertainty=untyped_uncertainty,
    )


def soccer_params() -> SgpParams:
    """SgpParams built from the SHIPPED soccer config, so the 1H×FT tests
    exercise the real calibrated numbers (mirrors PricingEngine wiring)."""
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


# 1H × full-time soccer tickers (same MEX/ENG game).
FH_ML_MEX = "KXWC1H-26JUL05MEXENG-MEX"  # SOURCE OF TRUTH: real 1H-winner series
FT_ML_MEX = "KXWCGAME-26JUL05MEXENG-MEX"
FT_ML_ENG = "KXWCGAME-26JUL05MEXENG-ENG"
FH_TOTAL = "KXWC1HTOTAL-26JUL05MEXENG-1"
FT_TOTAL = "KXWCTOTAL-26JUL05MEXENG-3"


# --- period × full-time (1H×FT) pairs ---------------------------------------------


def test_first_half_winner_same_team_gets_positive_prior() -> None:
    # [1H-winner-MEX, FT-winner-MEX] -> :same = +0.71, band 0.08. NOT independence.
    out = build_sgp_correlation(
        (leg(FH_ML_MEX, "EV"), leg(FT_ML_MEX, "EV")), [(0, 1)], soccer_params()
    )
    assert out.corr[0, 1] == pytest.approx(0.71)
    assert out.corr_low[0, 1] == pytest.approx(0.63)
    assert out.corr_high[0, 1] == pytest.approx(0.79)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0


def test_first_half_winner_opposite_team_gets_negative_prior() -> None:
    # [1H-winner-MEX, FT-winner-ENG] -> :opp = -0.67. Sign flips vs :same.
    out = build_sgp_correlation(
        (leg(FH_ML_MEX, "EV"), leg(FT_ML_ENG, "EV")), [(0, 1)], soccer_params()
    )
    assert out.corr[0, 1] == pytest.approx(-0.67)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0


def test_first_half_total_gets_positive_prior() -> None:
    out = build_sgp_correlation(
        (leg(FH_TOTAL, "EV"), leg(FT_TOTAL, "EV")), [(0, 1)], soccer_params()
    )
    assert out.corr[0, 1] == pytest.approx(0.73)
    assert out.corr_low[0, 1] == pytest.approx(0.61)
    assert out.corr_high[0, 1] == pytest.approx(0.85)
    assert out.typed_pairs == 1 and out.untyped_pairs == 0


def test_first_half_winner_with_draw_leg_falls_back_to_flat() -> None:
    # A draw-involving 1H winner pair is UNMEASURED -> untyped flat prior, not a
    # guessed number.
    out = build_sgp_correlation(
        (leg("KXWC1H-26JUL05MEXENG-TIE", "EV"), leg(FT_ML_MEX, "EV")),
        [(0, 1)],
        soccer_params(),
    )
    assert out.untyped_pairs == 1 and out.typed_pairs == 0
    assert out.corr[0, 1] == pytest.approx(soccer_params().default_rho)


# --- cross-event pairs --------------------------------------------------------


def test_cross_event_two_legs_rho_zero_gives_identity_matrices() -> None:
    legs = (leg(ML_NYY_TICKER, "E1"), leg(ML_WNBA_TICKER, "E2"))
    out = build_sgp_correlation(legs, [], params(cross_event_rho=0.0))
    ident = np.eye(2)
    np.testing.assert_array_equal(out.corr, ident)
    np.testing.assert_array_equal(out.corr_low, ident)
    np.testing.assert_array_equal(out.corr_high, ident)
    assert out.typed_pairs == 0
    assert out.untyped_pairs == 0
    assert out.notes == ()


def test_cross_event_rho_fills_off_diagonal_with_no_uncertainty_band() -> None:
    legs = (leg(ML_NYY_TICKER, "E1"), leg(ML_WNBA_TICKER, "E2"))
    out = build_sgp_correlation(legs, [], params(cross_event_rho=0.1))
    expected = np.array([[1.0, 0.1], [0.1, 1.0]])
    np.testing.assert_array_equal(out.corr, expected)
    # Cross-event pairs carry no band: low == point == high.
    np.testing.assert_array_equal(out.corr_low, expected)
    np.testing.assert_array_equal(out.corr_high, expected)
    assert out.typed_pairs == 0
    assert out.untyped_pairs == 0


def test_two_singleton_groups_are_still_cross_event() -> None:
    # Legs in *different* groups get the cross-event rho, not a same-event prior.
    legs = (leg(BTTS_TICKER, "E1"), leg(TOTAL_TICKER, "E2"))
    out = build_sgp_correlation(
        legs, [(0,), (1,)], params({"btts|total": 0.6}, cross_event_rho=0.05)
    )
    assert out.corr[0, 1] == pytest.approx(0.05)
    assert out.typed_pairs == 0
    assert out.untyped_pairs == 0


# --- typed same-event pairs -----------------------------------------------------


def test_same_event_btts_total_uses_typed_prior_and_band() -> None:
    legs = (leg(BTTS_TICKER, "EV"), leg(TOTAL_TICKER, "EV"))
    out = build_sgp_correlation(
        legs, [(0, 1)], params({"btts|total": 0.6}, typed_uncertainty=0.15)
    )
    assert out.corr[0, 1] == pytest.approx(0.6)
    assert out.corr[1, 0] == pytest.approx(0.6)
    assert out.corr_low[0, 1] == pytest.approx(0.45)
    assert out.corr_high[0, 1] == pytest.approx(0.75)
    assert out.typed_pairs == 1
    assert out.untyped_pairs == 0
    assert out.notes == ()


def test_moneyline_moneyline_negative_prior_clamps_low_at_minus_095() -> None:
    legs = (leg(ML_NYY_TICKER, "EV"), leg(ML_TB_TICKER, "EV"))
    out = build_sgp_correlation(
        legs, [(0, 1)], params({"moneyline|moneyline": -0.85}, typed_uncertainty=0.15)
    )
    assert out.corr[0, 1] == pytest.approx(-0.85)
    # -0.85 - 0.15 = -1.0 -> clamped to -0.95; [[1,-0.95],[-0.95,1]] is PSD so
    # the clamp survives to the returned matrix untouched.
    assert out.corr_low[0, 1] == pytest.approx(-0.95)
    assert out.corr_high[0, 1] == pytest.approx(-0.70)
    assert out.typed_pairs == 1


def test_positive_prior_band_clamps_high_at_plus_095() -> None:
    legs = (leg(BTTS_TICKER, "EV"), leg(TOTAL_TICKER, "EV"))
    out = build_sgp_correlation(
        legs, [(0, 1)], params({"btts|total": 0.9}, typed_uncertainty=0.15)
    )
    assert out.corr[0, 1] == pytest.approx(0.9)
    assert out.corr_low[0, 1] == pytest.approx(0.75)
    assert out.corr_high[0, 1] == pytest.approx(0.95)


# --- untyped fallbacks ----------------------------------------------------------


def test_unknown_leg_forces_flat_prior_even_when_a_prior_key_exists() -> None:
    # UNKNOWN typing must always widen (defense #2): even with a "total|unknown"
    # entry in the table, an UNKNOWN leg falls back to default_rho + wide band.
    legs = (leg(WEIRD_TICKER, "EV"), leg(TOTAL_TICKER, "EV"))
    out = build_sgp_correlation(
        legs,
        [(0, 1)],
        params({"total|unknown": 0.9}, default_rho=0.2, untyped_uncertainty=0.3),
    )
    assert out.corr[0, 1] == pytest.approx(0.2)
    # Fail-safe widening: fall-through band = |default_rho| + untyped_uncertainty
    # = 0.2 + 0.3 = 0.5, so the low bound spans zero (old floor was +0.1 = 0.2 -
    # 0.3, a confident positive that could not admit an anti-correlated pair).
    assert out.corr_low[0, 1] == pytest.approx(-0.3)
    assert out.corr_low[0, 1] <= 0.0
    assert out.corr_high[0, 1] == pytest.approx(0.7)
    assert out.typed_pairs == 0
    assert out.untyped_pairs == 1
    assert len(out.notes) == 1
    assert "untyped pair total|unknown" in out.notes[0]
    assert "flat prior 0.2" in out.notes[0]


def test_typed_pair_without_prior_entry_uses_default_rho_and_note() -> None:
    legs = (leg(CORNERS_TICKER, "EV"), leg(ADVANCE_TICKER, "EV"))
    out = build_sgp_correlation(
        legs,
        [(0, 1)],
        params({"btts|total": 0.6}, default_rho=0.25, untyped_uncertainty=0.3),
    )
    assert out.corr[0, 1] == pytest.approx(0.25)
    # Fail-safe widening: band = |default_rho| + untyped = 0.25 + 0.3 = 0.55, so
    # the low bound reaches -0.30 (old floor was -0.05); high = clamp(0.8) = 0.8.
    assert out.corr_low[0, 1] == pytest.approx(-0.30)
    assert out.corr_low[0, 1] <= 0.0
    assert out.corr_high[0, 1] == pytest.approx(0.80)
    assert out.typed_pairs == 0
    assert out.untyped_pairs == 1
    assert len(out.notes) == 1
    assert "no prior for pair advance|corners" in out.notes[0]


# --- fail-safe: default_rho fall-through band spans zero (fix #1) ----------------
#
# Same-game pairs that fall to the flat default_rho are only a prior-MEAN
# positive. An unmodeled pair (e.g. MLB pitcher-strikeouts x game-total ~ -0.2)
# could be uncorrelated OR anti-correlated, so its low matrix MUST reach <= 0 --
# a band that can't span zero is a confident positive that invites adverse
# selection. This is a pure WIDENING: the point estimate stays default_rho and
# calibrated/typed pairs keep their own (narrow) band untouched.


def test_unknown_leg_fallthrough_low_spans_zero_point_is_default_rho() -> None:
    # (1) UNKNOWN-leg branch: point == default_rho (0.6, still the right prior
    # for the many typical POSITIVE same-game pairs) but the band is widened so
    # corr_low reaches into the negative regime (<= 0, and here <= -0.2).
    legs = (leg(WEIRD_TICKER, "EV"), leg(TOTAL_TICKER, "EV"))
    out = build_sgp_correlation(
        legs, [(0, 1)], params(default_rho=0.6, untyped_uncertainty=0.3)
    )
    assert out.corr[0, 1] == pytest.approx(0.6)  # point unchanged: flat prior
    assert out.corr_low[0, 1] <= 0.0  # low spans zero (fail-safe)
    assert out.corr_low[0, 1] <= -0.2  # ...and reaches the anti-correlated regime
    # band = |0.6| + 0.3 = 0.9 -> clamp(0.6 - 0.9) = -0.3; high = clamp(1.5) = 0.95.
    assert out.corr_low[0, 1] == pytest.approx(-0.3)
    assert out.corr_high[0, 1] == pytest.approx(0.95)
    assert out.untyped_pairs == 1 and out.typed_pairs == 0


def test_typed_no_prior_fallthrough_low_spans_zero() -> None:
    # (2) TYPED pair with NO config entry (advance|corners is absent from every
    # table here) also falls through and must widen the same way.
    legs = (leg(ADVANCE_TICKER, "EV"), leg(CORNERS_TICKER, "EV"))
    out = build_sgp_correlation(
        legs, [(0, 1)], params({"btts|total": 0.6}, default_rho=0.6, untyped_uncertainty=0.3)
    )
    assert out.corr[0, 1] == pytest.approx(0.6)  # point stays default_rho
    assert out.corr_low[0, 1] <= 0.0  # low spans zero
    assert out.corr_low[0, 1] <= -0.2
    assert out.corr_low[0, 1] == pytest.approx(-0.3)
    assert out.untyped_pairs == 1 and out.typed_pairs == 0


def test_calibrated_pair_band_is_unchanged_and_tight() -> None:
    # (3) A CALIBRATED pair keeps its own tight band -- the widening touches ONLY
    # the default_rho fall-through. Soccer btts|total = 0.70 with band 0.12
    # (soccer:btts|total): corr_low stays STRICTLY POSITIVE.
    legs = (leg(BTTS_TICKER, "EV"), leg(TOTAL_TICKER, "EV"))
    out = build_sgp_correlation(legs, [(0, 1)], soccer_params())
    assert out.corr[0, 1] == pytest.approx(0.70)
    assert out.corr_low[0, 1] == pytest.approx(0.58)  # 0.70 - 0.12, tight
    assert out.corr_high[0, 1] == pytest.approx(0.82)  # 0.70 + 0.12
    assert out.corr_low[0, 1] > 0.0  # calibrated pair does NOT widen to zero
    assert out.typed_pairs == 1 and out.untyped_pairs == 0


def test_property_fallthrough_widens_calibrated_does_not() -> None:
    # Property: EVERY default_rho fall-through pair has corr_low <= 0; NO
    # calibrated pair widens (its corr_low stays strictly positive). Assembled in
    # one 3-leg combo: an UNKNOWN leg + a calibrated soccer btts|total pair.
    legs = (leg(WEIRD_TICKER, "EV"), leg(BTTS_TICKER, "EV"), leg(TOTAL_TICKER, "EV"))
    out = build_sgp_correlation(legs, [(0, 1, 2)], soccer_params())
    # Fall-through pairs (either pair touching the UNKNOWN leg 0): low <= 0.
    for j in (1, 2):
        assert out.corr[0, j] == pytest.approx(soccer_params().default_rho)
        assert out.corr_low[0, j] <= 0.0
    # Calibrated btts|total pair (legs 1,2): unchanged tight positive band.
    assert out.corr[1, 2] == pytest.approx(0.70)
    assert out.corr_low[1, 2] > 0.0
    assert out.untyped_pairs == 2 and out.typed_pairs == 1


# --- PSD repair ------------------------------------------------------------------


def test_inconsistent_three_leg_rhos_are_repaired_to_psd() -> None:
    legs = (leg(BTTS_TICKER, "EV"), leg(TOTAL_TICKER, "EV"), leg(GOAL_TICKER, "EV"))
    rhos = {"btts|total": 0.9, "btts|player_goal": 0.9, "player_goal|total": -0.9}
    # Sanity: the raw assembled point matrix is genuinely non-PSD, so this test
    # actually exercises the nearest_psd repair path.
    raw = np.array([[1.0, 0.9, 0.9], [0.9, 1.0, -0.9], [0.9, -0.9, 1.0]])
    assert not is_psd(raw)

    out = build_sgp_correlation(legs, [(0, 1, 2)], params(rhos, typed_uncertainty=0.05))
    assert out.typed_pairs == 3
    assert out.untyped_pairs == 0
    for m in (out.corr, out.corr_low, out.corr_high):
        assert is_psd(m)
        np.testing.assert_allclose(np.diag(m), np.ones(3), rtol=0.0, atol=1e-12)
        np.testing.assert_allclose(m, m.T, rtol=0.0, atol=1e-12)


# --- mixed combos ----------------------------------------------------------------


def test_mixed_same_event_pair_plus_cross_event_leg() -> None:
    legs = (leg(BTTS_TICKER, "EV1"), leg(TOTAL_TICKER, "EV1"), leg(ML_NYY_TICKER, "EV2"))
    out = build_sgp_correlation(
        legs,
        [(0, 1)],
        params({"btts|total": 0.6}, cross_event_rho=0.1, typed_uncertainty=0.15),
    )
    # Same-event pair gets its typed prior with the band...
    assert out.corr[0, 1] == pytest.approx(0.6)
    assert out.corr_low[0, 1] == pytest.approx(0.45)
    assert out.corr_high[0, 1] == pytest.approx(0.75)
    # ...cross-event pairs get cross_event_rho with no band.
    for i, j in ((0, 2), (1, 2)):
        assert out.corr[i, j] == pytest.approx(0.1)
        assert out.corr[j, i] == pytest.approx(0.1)
        assert out.corr_low[i, j] == pytest.approx(0.1)
        assert out.corr_high[i, j] == pytest.approx(0.1)
    assert out.typed_pairs == 1
    assert out.untyped_pairs == 0
    assert out.notes == ()
    for m in (out.corr, out.corr_low, out.corr_high):
        assert is_psd(m)


class TestScorerAndCornerPairs:
    """The corners+scorer wiring (2026-07-07): every pair below previously fell
    to the flat same-event default (0.6) — now it resolves a typed soccer ρ."""

    def _rho(self, t1: str, t2: str) -> tuple[float, int]:
        sgp = build_sgp_correlation([leg(t1), leg(t2)], [(0, 1)], soccer_params())
        return float(sgp.corr[0, 1]), sgp.typed_pairs

    def test_player_goal_player_goal_resolves(self) -> None:
        # teammates ~0 (Poisson-splitting) / opposing +0.05 -> single +0.03,
        # NOT the flat 0.6 that over-stated two-scorer joints by ~60%.
        rho, typed = self._rho(GOAL_TICKER, GOAL2_TICKER)
        assert typed == 1 and rho == pytest.approx(0.03)

    def test_player_goal_total_resolves(self) -> None:
        rho, typed = self._rho(GOAL_TICKER, TOTAL_TICKER)
        assert typed == 1 and rho == pytest.approx(0.46)

    def test_btts_player_goal_resolves(self) -> None:
        # RE-MEASURED 2026-07-07 (Understat 3,652, orientation-balanced top-xG
        # scorer): implied +0.549 -> ship 0.55 (was 0.45). Exceeds player_goal|
        # total (0.46), as a scorer guaranteeing one BTTS leg must.
        rho, typed = self._rho(BTTS_TICKER, GOAL_TICKER)
        assert typed == 1 and rho == pytest.approx(0.55)
        assert rho > 0.46  # must exceed player_goal|total

    def test_corners_moneyline_no_longer_flat_default(self) -> None:
        # The blind 0.6 same-event default is dead: corners|moneyline is a typed
        # 0.00 (TOTAL corners KXWCCORNERS, indep of result).
        rho, typed = self._rho(CORNERS_TICKER, ML_SOCCER_TICKER)
        assert typed == 1 and rho == pytest.approx(0.00)

    def test_team_corners_are_sub_typed_from_total(self) -> None:
        # SOURCE OF TRUTH (RFQ tape 2026-07-07): team corners = KXWCTCORNERS
        # (distinct series) vs total corners = KXWCCORNERS. They must NOT share a
        # type — team corners carry the measured -0.15 chasing-team signal vs the
        # result, not the total-corner 0.00.
        tc = "KXWCTCORNERS-26JUL07SUICOL-COL5"
        rho, typed = self._rho(tc, "KXWCGAME-26JUL07SUICOL-COL")
        assert typed == 1 and rho == pytest.approx(-0.15)  # team corners x that team wins
        # Opposite teams (COL vs SUI) resolve the :opp orientation. RE-MEASURED
        # 2026-07-07 (HC x AC, lines 4-7): implied -0.287 -> ship -0.28 (was the
        # ~0.07-too-shallow -0.21). Team parsed by stripping the line digits.
        rho2, typed2 = self._rho(tc, "KXWCTCORNERS-26JUL07SUICOL-SUI5")
        assert typed2 == 1 and rho2 == pytest.approx(-0.28)  # opposite teams, zero-sum
        # and total corners still resolve their own (unchanged) 0.00.
        rho3, typed3 = self._rho(CORNERS_TICKER, ML_SOCCER_TICKER)
        assert typed3 == 1 and rho3 == pytest.approx(0.00)

    def test_same_team_corners_strong_positive(self) -> None:
        # SAME team's nested lines (COL4 & COL8, POR4/POR8 analog) resolve :same,
        # a strong comonotone-approx positive — team parsed by stripping the line
        # digits so COL4 and COL8 read as ONE team (COL), not two.
        rho, typed = self._rho(
            "KXWCTCORNERS-26JUL07SUICOL-COL4", "KXWCTCORNERS-26JUL07SUICOL-COL8"
        )
        assert typed == 1 and rho == pytest.approx(0.90)


# --- FIX 1: btts|moneyline win-prob orientation CURVE ---------------------------

BTTS_SOCCER = "KXWCBTTS-26JUL05MEXENG-BTTS"
ML_SOCCER = "KXWCGAME-26JUL05MEXENG-MEX"


def _btts_ml_curve(ml_marginal: float) -> np.ndarray:
    """corr matrix for [ML(soccer), BTTS] of one game at a given ML YES-marginal,
    using the SHIPPED soccer config (curve forwarded via soccer_params())."""
    out = build_sgp_correlation(
        (leg(ML_SOCCER, "EV"), leg(BTTS_SOCCER, "EV")),
        [(0, 1)],
        soccer_params(),
        marginals=[ml_marginal, 0.55],
    )
    return out.corr


def test_btts_moneyline_curve_heavy_longshot_prices_near_zero() -> None:
    # THE headline fix: a heavy-longshot ML leg (win-prob 0.20) prices ~0, NOT the
    # -0.19/-0.20 the old 2-anchor fav/dog blend over-negated.
    rho = _btts_ml_curve(0.20)[0, 1]
    assert rho == pytest.approx(-0.05, abs=1e-9)
    assert rho > -0.10  # unmistakably ~0, not the old -0.20


def test_btts_moneyline_curve_pickem() -> None:
    assert _btts_ml_curve(0.50)[0, 1] == pytest.approx(-0.28, abs=1e-9)


def test_btts_moneyline_curve_favorite() -> None:
    assert _btts_ml_curve(0.65)[0, 1] == pytest.approx(-0.34, abs=1e-9)


def test_btts_moneyline_curve_monotone_deepens_with_favoritism() -> None:
    rhos = [_btts_ml_curve(p)[0, 1] for p in (0.20, 0.35, 0.50, 0.65, 0.85)]
    assert all(a > b for a, b in zip(rhos, rhos[1:], strict=False))  # strictly deepening


def test_btts_moneyline_curve_flat_clamp_outside_knot_range() -> None:
    # Below the lowest knot (0.20) and above the highest (0.85) the curve clamps
    # flat — never extrapolated past the measured range.
    assert _btts_ml_curve(0.05)[0, 1] == pytest.approx(-0.05, abs=1e-9)
    assert _btts_ml_curve(0.98)[0, 1] == pytest.approx(-0.36, abs=1e-9)


def test_btts_moneyline_curve_band_from_config() -> None:
    out = build_sgp_correlation(
        (leg(ML_SOCCER, "EV"), leg(BTTS_SOCCER, "EV")),
        [(0, 1)],
        soccer_params(),
        marginals=[0.50, 0.55],
    )
    # band 0.13 around the -0.28 pick-em knot -> low -0.41, high -0.15.
    assert out.corr_low[0, 1] == pytest.approx(-0.41, abs=1e-9)
    assert out.corr_high[0, 1] == pytest.approx(-0.15, abs=1e-9)


def test_btts_moneyline_without_marginals_falls_to_plain() -> None:
    # No marginals -> the curve cannot orient -> plain -0.19 pooled value.
    out = build_sgp_correlation(
        (leg(ML_SOCCER, "EV"), leg(BTTS_SOCCER, "EV")), [(0, 1)], soccer_params()
    )
    assert out.corr[0, 1] == pytest.approx(-0.19, abs=1e-9)


def test_btts_moneyline_curve_wins_over_fav_dog() -> None:
    # The shipped config carries BOTH the curve AND the legacy :fav/:dog scalars.
    # The curve must WIN: at a clear favorite the fav scalar would give -0.19, but
    # the curve gives -0.36; at a heavy longshot dog's ~0.0 would apply but the
    # curve gives -0.05. Either way the resolved value is the CURVE's.
    p = soccer_params()
    assert p.pair_rho_by_sport["soccer"]["btts|moneyline:fav"] == -0.19  # present...
    assert _btts_ml_curve(0.85)[0, 1] == pytest.approx(-0.36, abs=1e-9)   # ...but curve wins


def test_shipped_config_carries_btts_moneyline_curve() -> None:
    c = CorrelationConfig()
    knots = c.oriented_curve["soccer:btts|moneyline"]
    assert knots[0] == (0.20, -0.05) and knots[-1] == (0.85, -0.36)
    assert c.oriented_curve_uncertainty["soccer:btts|moneyline"] == pytest.approx(0.13)


def test_corners_team_unparseable_suffix_uses_plain_not_orientation() -> None:
    # A suffix that isn't <team><digits> cannot orient -> the PLAIN entry, never a
    # guessed :opp/:same. Distinct plain value proves the fallback path.
    p = SgpParams(
        pair_rho={},
        default_rho=0.3,
        cross_event_rho=0.0,
        typed_uncertainty=0.15,
        untyped_uncertainty=0.25,
        pair_rho_by_sport={
            "soccer": {
                "corners_team|corners_team": -0.10,       # plain (distinct)
                "corners_team|corners_team:opp": -0.28,
                "corners_team|corners_team:same": 0.90,
            }
        },
    )
    out = build_sgp_correlation(
        (leg("KXWCTCORNERS-26JUL05MEXENG-", "EV"),  # empty suffix -> unparseable
         leg("KXWCTCORNERS-26JUL05MEXENG-ENG5", "EV")),
        [(0, 1)],
        p,
    )
    assert out.corr[0, 1] == pytest.approx(-0.10, abs=1e-9)  # plain, not :opp -0.28
