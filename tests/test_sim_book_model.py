"""Tests for the risk book-model bridge + the engine NO-side leg-flip fix
(RISK_BUILD_PLAN Phase 4). The mandatory parity gate (M1 §1) lives here: a
single-combo book run through build_book_model + the MC must reproduce the
copula's analytic joint to MC tolerance, proving the risk sim and the pricer
share a joint.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.pricing.copula import gaussian_copula_joint_prob
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import (
    DEFAULT_FLAT_BAND,
    build_book_model,
)
from combomaker.sim.engine import ComboPosition, LegModel, simulate

N = 200_000
DOLLAR_CC = 10_000.0


def _pos(
    position_id: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.NO,
    contracts: int = 100,
    price_cc: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=f"COMBO-{position_id}",
        collection=None,
        our_side=our_side,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
    )


def _leg(ticker: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side=side)


# ---------------------------------------------------------------------------
# Engine NO-side leg-flip fix (the ~2-line _position_pnl port)
# ---------------------------------------------------------------------------
class TestLegSidesFlip:
    def test_all_yes_leg_sides_byte_identical_to_none(self) -> None:
        # Backward compatibility: leg_sides=('yes','yes',...) must reproduce the
        # default (None) exactly, sample for sample.
        legs = (LegModel(p=0.7), LegModel(p=0.5), LegModel(p=0.4))
        corr = np.eye(3)
        base = ComboPosition(leg_indices=(0, 1, 2), side="yes", contracts=2, price_cc=1_000)
        flagged = ComboPosition(
            leg_indices=(0, 1, 2),
            side="yes",
            contracts=2,
            price_cc=1_000,
            leg_sides=("yes", "yes", "yes"),
        )
        s0 = simulate(legs, corr, [base], n_samples=50_000, seed=5)
        s1 = simulate(legs, corr, [flagged], n_samples=50_000, seed=5)
        assert np.array_equal(s0.pnl_samples, s1.pnl_samples)

    def test_no_leg_flips_value(self) -> None:
        # A single NO-selected leg: payout = (1 - v). For a binary p=0.3 leg,
        # E[payout] = 1 - 0.3 = 0.7 → a YES-contract position pays 0.7*$1.
        legs = (LegModel(p=0.3),)
        pos = ComboPosition(
            leg_indices=(0,), side="yes", contracts=1, price_cc=0, leg_sides=("no",)
        )
        stats = simulate(legs, np.eye(1), [pos], n_samples=N, seed=9)
        expected = DOLLAR_CC * 0.7
        sigma = DOLLAR_CC * math.sqrt(0.7 * 0.3) / math.sqrt(N)
        assert abs(stats.ev_cc - expected) < 3.0 * sigma

    def test_no_leg_equals_manual_complement_leg(self) -> None:
        # A NO-selected leg via leg_sides must equal an INDEPENDENT complement
        # leg only when the corr is identity (no correlation to preserve). This
        # pins the algebra: 1 - v of a p leg == a (1-p) leg under independence.
        legs_flip = (LegModel(p=0.6),)
        legs_comp = (LegModel(p=0.4),)  # complement marginal
        pos_flip = ComboPosition(
            leg_indices=(0,), side="yes", contracts=1, price_cc=0, leg_sides=("no",)
        )
        pos_comp = ComboPosition(leg_indices=(0,), side="yes", contracts=1, price_cc=0)
        s_flip = simulate(legs_flip, np.eye(1), [pos_flip], n_samples=N, seed=3)
        s_comp = simulate(legs_comp, np.eye(1), [pos_comp], n_samples=N, seed=3)
        # Same seed, same uniforms: 1 - 1[u <= 0.6] == 1[u <= 0.4] fails per-sample
        # (the flip inverts the threshold direction), so compare the MEANS.
        assert abs(s_flip.ev_cc - s_comp.ev_cc) < 3.0 * (
            DOLLAR_CC * math.sqrt(0.4 * 0.6) / math.sqrt(N)
        )

    def test_no_no_pair_preserves_correlation(self) -> None:
        # THE M1 FIX: two NO-selected legs in the SAME correlated game must show
        # the copula's correlated NO-NO joint, NOT the product of independent
        # complements. At rho -> +1 (comonotone) the two YES legs move together,
        # so (1-v0)(1-v1) with p0=p1=0.5 hits its comonotone value: P(both NO) =
        # min(1-p0, 1-p1) = 0.5, NOT the independent 0.25.
        legs = (LegModel(p=0.5), LegModel(p=0.5))
        corr = np.array([[1.0, 0.999999], [0.999999, 1.0]])
        pos = ComboPosition(
            leg_indices=(0, 1), side="yes", contracts=1, price_cc=0, leg_sides=("no", "no")
        )
        stats = simulate(legs, corr, [pos], n_samples=N, seed=7)
        hit_rate = stats.ev_cc / DOLLAR_CC  # E[(1-v0)(1-v1)]
        assert abs(hit_rate - 0.5) < 0.01  # comonotone, NOT 0.25 independent

    def test_leg_sides_length_validated(self) -> None:
        with pytest.raises(ValueError, match="leg_sides length"):
            ComboPosition(
                leg_indices=(0, 1), side="yes", contracts=1, price_cc=0, leg_sides=("no",)
            )

    def test_leg_sides_value_validated(self) -> None:
        with pytest.raises(ValueError, match="leg side must be"):
            ComboPosition(
                leg_indices=(0,), side="yes", contracts=1, price_cc=0,
                leg_sides=("maybe",),  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# build_book_model — construction + the mandatory pricer-parity gate
# ---------------------------------------------------------------------------
class TestBuildBookModel:
    def test_empty_book(self) -> None:
        m = build_book_model([], marginals=lambda t: 0.5)
        assert m.legs == ()
        assert m.positions == ()
        assert m.corr_point.shape == (0, 0)
        assert not m.unknown

    def test_missing_marginal_flags_unknown(self) -> None:
        legs = (_leg("A", "KXWCGAME-26X"),)
        pos = _pos("p1", legs)
        m = build_book_model([pos], marginals=lambda t: None)
        assert m.unknown  # fail-closed: any missing marginal ⇒ no-go

    def test_leg_universe_dedups_tickers(self) -> None:
        # Two positions sharing a leg ticker collapse to one latent index.
        p1 = _pos("p1", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")))
        p2 = _pos("p2", (_leg("A", "KXWCGAME-G1"), _leg("C", "KXWCGAME-G1")))
        m = build_book_model([p1, p2], marginals=lambda t: 0.5)
        assert len(m.legs) == 3  # A, B, C — A shared
        assert m.leg_index["A"] < len(m.legs)

    def test_cross_game_block_diagonal(self) -> None:
        # Legs in different games must sit at cross_event_rho (0) off-block; legs
        # in the same game carry the within-game rho.
        p = _pos(
            "p1",
            (
                _leg("A", "KXWCGAME-G1"),
                _leg("B", "KXWCGAME-G1"),
                _leg("C", "KXWCGAME-G2"),
            ),
        )
        m = build_book_model(
            [p],
            marginals=lambda t: 0.5,
            within_game_rho=lambda a, b: (0.2, 0.5, 0.8),
        )
        ia, ib, ic = m.leg_index["A"], m.leg_index["B"], m.leg_index["C"]
        # A,B same game G1 → high-band rho 0.8; A,C cross-game → 0.
        assert m.corr_high[ia, ib] == pytest.approx(0.8)
        assert m.corr_high[ia, ic] == pytest.approx(0.0)
        assert m.corr_high[ib, ic] == pytest.approx(0.0)
        # low band uses the min rho 0.2.
        assert m.corr_low[ia, ib] == pytest.approx(0.2)

    def test_flat_band_default_when_no_prior(self) -> None:
        p = _pos("p1", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")))
        m = build_book_model([p], marginals=lambda t: 0.5, within_game_rho=lambda a, b: None)
        ia, ib = m.leg_index["A"], m.leg_index["B"]
        lo, pt, hi = DEFAULT_FLAT_BAND
        assert m.corr_low[ia, ib] == pytest.approx(lo)
        assert m.corr_high[ia, ib] == pytest.approx(hi)

    def test_ungamed_leg_never_merges(self) -> None:
        # A leg with no event_ticker keys on itself and never correlates.
        p = _pos("p1", (LegRef("A", None, "yes"), LegRef("B", None, "yes")))
        m = build_book_model(
            [p], marginals=lambda t: 0.5, within_game_rho=lambda a, b: (0.2, 0.5, 0.8)
        )
        ia, ib = m.leg_index["A"], m.leg_index["B"]
        assert m.corr_high[ia, ib] == pytest.approx(0.0)  # independent, fail-closed

    def test_no_position_uses_leg_sides(self) -> None:
        # A NO-side position's legs are marked in leg_sides so the MC flips them.
        p = _pos("p1", (_leg("A", "KXWCGAME-G1", "no"),), our_side=Side.NO)
        m = build_book_model([p], marginals=lambda t: 0.5)
        combo = m.positions[0]
        assert combo.side == "no"
        assert combo.leg_sides == ("no",)

    # --- THE PARITY GATE (M1 §1, mandatory) --------------------------------
    def test_parity_single_combo_reproduces_copula_joint(self) -> None:
        # A one-position YES book of a same-game 2-leg combo, run through
        # build_book_model + simulate, must reproduce the copula's analytic joint
        # P(both YES) as the MC hit rate to MC tolerance. This proves the risk sim
        # and the pricer share a joint (hard rule 8 parity check).
        p_a, p_b, rho = 0.6, 0.45, 0.5
        legs = (_leg("A", "KXWCGAME-G1", "yes"), _leg("B", "KXWCGAME-G1", "yes"))
        pos = _pos("p1", legs, our_side=Side.YES, contracts=100, price_cc=0)

        marg = {"A": p_a, "B": p_b}
        m = build_book_model(
            [pos],
            marginals=lambda t: marg[t],
            within_game_rho=lambda a, b: (rho, rho, rho),  # point==the analytic rho
        )
        # analytic joint at the SAME rho the model's point band carries.
        analytic = gaussian_copula_joint_prob(
            [p_a, p_b], np.array([[1.0, rho], [rho, 1.0]])
        )
        stats = simulate(
            m.legs, m.corr_for_band("point"), list(m.positions), n_samples=N, seed=101
        )
        # A YES combo at price 0 pays $1 * P(both hit); EV/DOLLAR = MC hit rate.
        mc_hit = stats.ev_cc / DOLLAR_CC
        sigma = math.sqrt(analytic * (1 - analytic)) / math.sqrt(N)
        assert abs(mc_hit - analytic) < 4.0 * sigma

    def test_parity_no_combo_reproduces_complement_joint(self) -> None:
        # The sell-only case: a NO-side combo of a same-game 2-leg pair. The MC
        # NO payout should be 1 - P(both YES) (the combo settles NO unless BOTH
        # legs hit YES), matching 1 - copula joint.
        p_a, p_b, rho = 0.5, 0.5, 0.6
        legs = (_leg("A", "KXWCGAME-G1", "yes"), _leg("B", "KXWCGAME-G1", "yes"))
        pos = _pos("p1", legs, our_side=Side.NO, contracts=100, price_cc=0)
        marg = {"A": p_a, "B": p_b}
        m = build_book_model(
            [pos], marginals=lambda t: marg[t], within_game_rho=lambda a, b: (rho, rho, rho)
        )
        analytic_yes = gaussian_copula_joint_prob(
            [p_a, p_b], np.array([[1.0, rho], [rho, 1.0]])
        )
        stats = simulate(
            m.legs, m.corr_for_band("point"), list(m.positions), n_samples=N, seed=202
        )
        # NO position at price 0 pays (1 - payout)*$1; EV/DOLLAR = 1 - joint.
        mc_no = stats.ev_cc / DOLLAR_CC
        expected = 1.0 - analytic_yes
        sigma = math.sqrt(analytic_yes * (1 - analytic_yes)) / math.sqrt(N)
        assert abs(mc_no - expected) < 4.0 * sigma
