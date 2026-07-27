"""NESTED SAME-LADDER RUNGS: the exact comonotone coupling (2026-07-27).

Two rungs of ONE entity's ONE counting variable are COMONOTONE by arithmetic
(K>=5 implies K>=4), so their coupling is DERIVED from the two marginals — never
a table lookup. Before this resolver a same-pitcher K-ladder pair fell through to
the plain ``player_ks|player_ks`` entry, i.e. the value MEASURED FOR TWO OPPOSING
STARTERS.

What these tests pin:
  1. the derived value reproduces the EXACT nested joint (P(both) = P(higher
     rung)) through the shipped copula, for every YES/NO side mix;
  2. the closed-form implied PEARSON correlation the operator specified,
     sqrt(p_sub(1-p_sup)/(p_sup(1-p_sub))), is what the exact coupling implies —
     and is NOT what goes in the latent matrix (a regression guard: writing it
     there leaves a measurable joint error);
  3. the STRUCTURAL detector — same series + same entity + rung ordering — so a
     different pitcher, a different stat family, a different game, a
     doubleheader sibling, or an unparseable shape all keep their existing
     treatment;
  4. the degenerate marginals (p in {0,1}, p_a == p_b, crossed rungs).

ISOLATION: every test injects a SYNTHETIC pair table with POISON values on the
keys the old path used, so a regression that re-routes a nested pair to a table
entry shows up as the poison value rather than as a silent near-match.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from combomaker.core.money import CentiCents
from combomaker.pricing.copula import gaussian_copula_joint_prob, is_psd
from combomaker.pricing.legtypes import LegType
from combomaker.pricing.sgp import (
    SgpParams,
    build_sgp_correlation,
    nested_ladder_rho,
    nested_pearson_rho,
)
from combomaker.rfq.models import RfqLeg

_G = "26JUL271840PHIMIA"          # real game-code shape (PHI @ MIA, live 2026-07-27)
_G2 = "26JUL271435SEATEX"
_EV = f"KXMLBKS-{_G}"

KS4 = f"KXMLBKS-{_G}-PHIZWHEELER45-4"
KS5 = f"KXMLBKS-{_G}-PHIZWHEELER45-5"
KS7 = f"KXMLBKS-{_G}-PHIZWHEELER45-7"
KS4_OTHER_PITCHER = f"KXMLBKS-{_G}-MIATPHILLIPS30-4"
KS4_OTHER_GAME = f"KXMLBKS-{_G2}-PHIZWHEELER45-4"
KS_NO_LINE = f"KXMLBKS-{_G}-PHIZWHEELER45"
OUTS18 = f"KXMLBOUTS-{_G}-PHIZWHEELER45-18"
HIT1 = f"KXMLBHIT-{_G}-PHIJREALMUTO10-1"
HIT2 = f"KXMLBHIT-{_G}-PHIJREALMUTO10-2"
HR1 = f"KXMLBHR-{_G}-PHIJREALMUTO10-1"
# Doubleheader siblings: the RAW game segment differs, so they are two DIFFERENT
# games and must never merge into one ladder.
KS4_DH1 = f"KXMLBKS-{_G}G1-PHIZWHEELER45-4"
KS5_DH2 = f"KXMLBKS-{_G}G2-PHIZWHEELER45-5"

# POISON: every value the old (pre-fix) resolution chain could have produced.
_POISON = {
    "player_ks|player_ks": 0.777,
    "player_ks|player_ks:same": 0.888,
    "player_hit|player_hit": 0.777,
    "player_hit|player_hit:same": 0.888,
}


def _params(**over: object) -> SgpParams:
    base = dict(
        pair_rho=dict(_POISON),
        default_rho=0.04,
        cross_event_rho=0.0,
        typed_uncertainty=0.10,
        untyped_uncertainty=0.30,
    )
    base.update(over)
    return SgpParams(**base)  # type: ignore[arg-type]


def _leg(ticker: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(
        market_ticker=ticker,
        event_ticker=_EV,
        side=side,
        yes_settlement_value_cc=CentiCents(0),
    )


def _corr(ticker_a: str, ticker_b: str, p_a: float, p_b: float, **over: object):
    return build_sgp_correlation(
        [_leg(ticker_a), _leg(ticker_b)], [[0, 1]], _params(**over),
        marginals=[p_a, p_b],
    )


class TestExactJoint:
    """The shipped copula must reproduce the nested arithmetic, not approach it."""

    @pytest.mark.parametrize(
        ("p_a", "p_b"),
        [(0.9018, 0.8405), (0.8865, 0.8002), (0.8926, 0.0440), (0.5, 0.4999)],
    )
    def test_joint_equals_the_higher_rungs_marginal(self, p_a: float, p_b: float) -> None:
        sgp = _corr(KS4, KS5, p_a, p_b)
        joint = gaussian_copula_joint_prob([p_a, p_b], sgp.corr)
        # P(K>=4 and K>=5) == P(K>=5) — the subset IS the higher rung. The only
        # residual is the 1e-6 inset that keeps the matrix strictly inside
        # (-1, 1); MEASURED worst case 1.8e-4 of probability (0.018c, 1/50th of a
        # tick) and it appears ONLY when the two rungs are quoted essentially
        # equal. The inset MATCHES sim/book_model._clamp_open_unit exactly, so
        # the pricing matrix and the risk LOCATION matrix cannot diverge.
        assert joint == pytest.approx(min(p_a, p_b), abs=2e-4)

    def test_band_joint_is_the_exact_difference_of_marginals(self) -> None:
        """YES(low rung) x NO(high rung) must price P(low) - P(high) exactly —
        the arithmetic the copula could not express at the old +0.04 (it priced
        that 6.1c window at 14.2c, an 8.1c overstatement on a shape we SELL)."""
        p_low, p_high = 0.9018, 0.8405
        sgp = _corr(KS4, KS5, p_low, p_high)
        rho = float(sgp.corr[0, 1])
        joint_both = gaussian_copula_joint_prob([p_low, p_high], sgp.corr)
        band = p_low - joint_both
        assert band == pytest.approx(p_low - p_high, abs=5e-5)
        assert rho > 0.99

    def test_low_point_high_matrices_agree(self) -> None:
        """Band ZERO: an arithmetic identity has no prior uncertainty to widen."""
        sgp = _corr(KS4, KS5, 0.9018, 0.8405)
        assert float(sgp.corr_low[0, 1]) == float(sgp.corr[0, 1])
        assert float(sgp.corr_high[0, 1]) == float(sgp.corr[0, 1])

    def test_the_pair_counts_as_TYPED_not_a_flat_fallback(self) -> None:
        sgp = _corr(KS4, KS5, 0.9018, 0.8405)
        assert (sgp.typed_pairs, sgp.untyped_pairs) == (1, 0)
        assert any("nested ladder" in n for n in sgp.notes)

    def test_note_records_the_implied_pearson(self) -> None:
        sgp = _corr(KS4, KS5, 0.9018, 0.8405)
        note = next(n for n in sgp.notes if "nested ladder" in n)
        assert "PHIZWHEELER45 r4xr5" in note
        assert "+0.758" in note  # sqrt(.8405*.0982/(.9018*.1595))

    def test_never_takes_the_poison_table_value(self) -> None:
        sgp = _corr(KS4, KS5, 0.9018, 0.8405)
        assert float(sgp.corr[0, 1]) not in (0.777, 0.888)
        assert float(sgp.corr[0, 1]) > 0.99

    def test_marginal_less_caller_is_unchanged(self) -> None:
        """No marginals (the book-risk provider's default call) => the pair keeps
        its old table treatment. The nested value is DERIVED FROM MARGINALS, so
        it cannot exist without them — and must not be invented."""
        sgp = build_sgp_correlation(
            [_leg(KS4), _leg(KS5)], [[0, 1]], _params(), marginals=None
        )
        # 0.888 = the ':same' pitcher-pair entry the old chain resolved to.
        assert float(sgp.corr[0, 1]) == pytest.approx(0.888)


class TestPearsonClosedForm:
    """The operator's closed form is what the exact coupling IMPLIES — and is
    NOT the parameter the latent matrix takes."""

    @pytest.mark.parametrize(
        ("p_a", "p_b"), [(0.9018, 0.8405), (0.8865, 0.8002), (0.5934, 0.0440)]
    )
    def test_matches_the_sampled_pearson_of_the_shipped_coupling(
        self, p_a: float, p_b: float
    ) -> None:
        sgp = _corr(KS4, KS5, p_a, p_b)
        joint = gaussian_copula_joint_prob([p_a, p_b], sgp.corr)
        cov = joint - p_a * p_b
        pearson = cov / math.sqrt(p_a * (1 - p_a) * p_b * (1 - p_b))
        assert pearson == pytest.approx(nested_pearson_rho(p_a, p_b), abs=1e-3)

    def test_closed_form_is_the_documented_formula(self) -> None:
        p_sup, p_sub = 0.9018, 0.8405
        expected = math.sqrt(p_sub * (1 - p_sup) / (p_sup * (1 - p_sub)))
        assert nested_pearson_rho(p_sup, p_sub) == pytest.approx(expected)
        # order-independent
        assert nested_pearson_rho(p_sub, p_sup) == pytest.approx(expected)

    def test_pearson_in_the_latent_slot_would_NOT_be_exact(self) -> None:
        """Regression guard for the tempting "simplification": these matrices are
        GAUSSIAN-COPULA parameters, so the Pearson number is measurably wrong
        there. If this ever stops failing, the copula convention changed and the
        nested resolver must be revisited."""
        p_a, p_b = 0.9018, 0.8405
        pearson = nested_pearson_rho(p_a, p_b)
        assert pearson is not None
        corr = np.array([[1.0, pearson], [pearson, 1.0]])
        joint = gaussian_copula_joint_prob([p_a, p_b], corr)
        assert joint < min(p_a, p_b) - 0.02  # ~3.2c short of exact


class TestDegenerateMarginals:
    @pytest.mark.parametrize("p", [0.0, 1.0])
    def test_degenerate_marginal_falls_back(self, p: float) -> None:
        """At p in {0,1} the indicator has zero variance, so the correlation is
        undefined (0/0). Fail-closed: keep the existing treatment."""
        assert nested_pearson_rho(p, 0.5) is None
        assert nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_KS, KS4, KS5, p, 0.5
        ) is None
        sgp = _corr(KS4, KS5, p, 0.5)
        assert float(sgp.corr[0, 1]) == pytest.approx(0.888)  # the old ':same' entry

    def test_equal_marginals_are_perfectly_correlated(self) -> None:
        assert nested_pearson_rho(0.61, 0.61) == pytest.approx(1.0)
        resolved = nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_KS, KS4, KS5, 0.61, 0.61
        )
        assert resolved is not None
        assert resolved[1] == pytest.approx(1.0)

    def test_crossed_rungs_degrade_to_comonotone_for_the_marginals(self) -> None:
        """A CROSSED book (the higher rung quoted ABOVE the lower one) is a
        marginal pair no nesting can produce. The resolver sorts by marginal, so
        it yields the comonotone coupling for the marginals AS GIVEN rather than
        a >1 nonsense correlation."""
        p_high_rung, p_low_rung = 0.90, 0.70  # KS5 quoted above KS4: impossible
        resolved = nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_KS, KS4, KS5, p_low_rung, p_high_rung
        )
        assert resolved is not None
        assert 0.0 < resolved[1] <= 1.0
        sgp = _corr(KS4, KS5, p_low_rung, p_high_rung)
        joint = gaussian_copula_joint_prob([p_low_rung, p_high_rung], sgp.corr)
        assert joint == pytest.approx(min(p_low_rung, p_high_rung), abs=5e-5)


class TestStructuralDetection:
    """Same SERIES + same ENTITY + a rung ordering — never a ticker heuristic."""

    def test_different_pitchers_keep_the_measured_prior(self) -> None:
        # Two opposing starters route ':opp' (unwired here) -> the plain entry.
        sgp = _corr(KS4, KS4_OTHER_PITCHER, 0.9018, 0.8405)
        assert float(sgp.corr[0, 1]) == pytest.approx(0.777)

    def test_different_games_are_not_one_ladder(self) -> None:
        assert nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_KS, KS4, KS4_OTHER_GAME, 0.90, 0.84
        ) is None

    def test_doubleheader_siblings_are_not_one_ladder(self) -> None:
        assert nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_KS, KS4_DH1, KS5_DH2, 0.90, 0.84
        ) is None

    def test_different_stat_on_the_same_player_keeps_its_copula_rho(self) -> None:
        """ks x outs on ONE pitcher is a genuine measured copula rho (a start can
        be high-K/low-outs), NOT a containment — it must be untouched."""
        assert nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_OUTS, KS4, OUTS18, 0.8865, 0.6019
        ) is None
        table = {"player_ks|player_outs": -0.21}
        sgp = _corr(KS4, OUTS18, 0.8865, 0.6019, pair_rho=table)
        assert float(sgp.corr[0, 1]) == pytest.approx(-0.21)

    def test_cross_family_same_player_batter_pair_is_untouched(self) -> None:
        """HIT x HR of one batter is containment/conditional-shaped and is owned
        by relationships.py + the conditional seam — never by this resolver."""
        assert nested_ladder_rho(
            LegType.PLAYER_HIT, LegType.PLAYER_HR, HIT1, HR1, 0.55, 0.12
        ) is None

    def test_same_rung_twice_is_not_a_nested_pair(self) -> None:
        assert nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_KS, KS4, KS4, 0.90, 0.90
        ) is None

    def test_unparseable_shape_falls_back(self) -> None:
        assert nested_ladder_rho(
            LegType.PLAYER_KS, LegType.PLAYER_KS, KS4, KS_NO_LINE, 0.90, 0.84
        ) is None

    def test_a_withheld_family_is_not_treated_as_nested(self) -> None:
        """SPREAD rungs nest too, but they are TEAM-suffix-shaped and already
        carry wired ':rN' entries — deliberately out of the registry until
        probed. Guards against someone widening the registry by accident."""
        spread_a = f"KXMLBSPREAD-{_G}-PHI1"
        spread_b = f"KXMLBSPREAD-{_G}-PHI2"
        assert nested_ladder_rho(
            LegType.SPREAD, LegType.SPREAD, spread_a, spread_b, 0.60, 0.35
        ) is None

    def test_batter_hit_ladder_is_nested(self) -> None:
        """The registry is family-generic, not ks-specific."""
        sgp = _corr(HIT1, HIT2, 0.62, 0.24)
        joint = gaussian_copula_joint_prob([0.62, 0.24], sgp.corr)
        assert joint == pytest.approx(0.24, abs=5e-5)


class TestMatrixHealth:
    def test_a_four_rung_ladder_stays_psd(self) -> None:
        """A whole ladder is rank-deficient BY CONSTRUCTION (every rung is a
        function of one count) — it must still survive assembly + PSD repair."""
        rungs = [
            f"KXMLBKS-{_G}-PHIZWHEELER45-{n}" for n in (4, 5, 6, 7)
        ]
        ps = [0.9018, 0.8405, 0.7013, 0.5739]
        sgp = build_sgp_correlation(
            [_leg(t) for t in rungs], [[0, 1, 2, 3]], _params(), marginals=ps
        )
        for m in (sgp.corr, sgp.corr_low, sgp.corr_high):
            assert is_psd(m)
        # every off-diagonal is comonotone, and the whole-ladder joint is the
        # top rung's marginal
        off = sgp.corr[~np.eye(4, dtype=bool)]
        assert off.min() > 0.99
        assert gaussian_copula_joint_prob(ps, sgp.corr) == pytest.approx(
            min(ps), abs=2e-3
        )

    def test_a_ladder_mixed_with_an_ordinary_pair_stays_psd(self) -> None:
        """The rungs sit at ~1 while their pairs with a third leg sit at the
        table value — a shape that can breach PSD. The existing repair must
        absorb it."""
        ml = f"KXMLBGAME-{_G}-PHI"
        table = dict(_POISON)
        table["moneyline|player_ks"] = 0.24
        sgp = build_sgp_correlation(
            [_leg(KS4), _leg(KS5), _leg(KS7), _leg(ml)],
            [[0, 1, 2, 3]],
            _params(pair_rho=table),
            marginals=[0.9018, 0.8405, 0.5739, 0.55],
        )
        for m in (sgp.corr, sgp.corr_low, sgp.corr_high):
            assert is_psd(m)


class TestRiskProviderWiring:
    """The BOOK-RISK side (sim/within_game_rho) must resolve the SAME pairs to the
    SAME value as the quote path — that is the whole point of the lever: our
    largest concentration was invisible to the joint the risk MC samples."""

    @staticmethod
    def _provider(marginals: dict[str, float] | None = None):
        from combomaker.sim.within_game_rho import sgp_within_game_rho_provider

        if marginals is None:
            return sgp_within_game_rho_provider(_params())
        return sgp_within_game_rho_provider(_params(), lambda t: marginals.get(t))

    def test_unbound_provider_is_byte_identical_to_the_old_behaviour(self) -> None:
        """No marginal source (offline tools, tests) => the pre-fix band."""
        band = self._provider()(KS4, KS5)
        assert band is not None
        assert band[1] == pytest.approx(0.888)

    def test_bound_provider_returns_the_exact_comonotone_band(self) -> None:
        band = self._provider({KS4: 0.9018, KS5: 0.8405})(KS4, KS5)
        assert band is not None
        low, point, high = band
        assert point > 0.99
        assert low == point == high  # exact: no band to widen

    def test_bind_marginals_after_construction(self) -> None:
        """The live wiring builds the provider BEFORE the lifecycle that owns the
        marginal source, so late binding must work on the SAME object."""
        prov = self._provider()
        assert prov(KS4, KS5)[1] == pytest.approx(0.888)  # type: ignore[index]
        prov.bind_marginals({KS4: 0.9018, KS5: 0.8405}.get)
        assert prov(KS4, KS5)[1] > 0.99  # type: ignore[index]

    def test_missing_marginal_falls_back_not_forward(self) -> None:
        band = self._provider({KS4: 0.9018})(KS4, KS5)  # KS5 unpriced
        assert band is not None
        assert band[1] == pytest.approx(0.888)

    def test_non_nested_pairs_are_untouched_by_the_binding(self) -> None:
        marg = {KS4: 0.9018, KS4_OTHER_PITCHER: 0.8405, OUTS18: 0.60}
        unbound, bound = self._provider(), self._provider(marg)
        for a, b in ((KS4, KS4_OTHER_PITCHER), (KS4, OUTS18)):
            assert unbound(a, b) == bound(a, b)

    def test_self_pair_still_returns_none(self) -> None:
        assert self._provider({KS4: 0.9018})(KS4, KS4) is None

    def test_risk_provider_agrees_with_the_quote_path(self) -> None:
        p_a, p_b = 0.9018, 0.8405
        risk = self._provider({KS4: p_a, KS5: p_b})(KS4, KS5)
        quote = _corr(KS4, KS5, p_a, p_b)
        assert risk is not None
        assert risk[1] == pytest.approx(float(quote.corr[0, 1]))
