"""Tests for the full book-risk MC + tail attribution + challenger/stress overlay
(RISK_BUILD_PLAN Phase 4). Covers the five key outputs, the additive tail
decomposition, the separated sampled-ES / deterministic-max axes (P0-3), and the
fail-closed UNKNOWN path.
"""

from __future__ import annotations

import numpy as np
import pytest

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_model import build_book_model
from combomaker.sim.book_risk import (
    BookRiskSnapshot,
    _deterministic_all_hit_loss_cc,
    _inflate_corr,
    _same_game_mask,
    compute_book_risk,
)


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


class TestFailClosed:
    def test_unknown_model_no_usable_stats(self) -> None:
        pos = _pos("p1", (_leg("A", "KXWCGAME-G1"),))
        m = build_book_model([pos], marginals=lambda t: None)
        snap = compute_book_risk(m, n_samples=1_000, seed=1)
        assert snap.unknown
        assert not snap.usable
        assert snap.es_99_cc == 0.0
        assert snap.governing_model_es_99_cc == 0.0
        assert snap.deterministic_max_loss_cc == 0.0

    def test_empty_book_no_go(self) -> None:
        m = build_book_model([], marginals=lambda t: 0.5)
        snap = compute_book_risk(m, n_samples=1_000, seed=1)
        assert not snap.usable
        assert snap.n_positions == 0


class TestDeterminism:
    def test_same_seed_identical_governing_model_es(self) -> None:
        pos = _pos("p1", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")))
        m = build_book_model([pos], marginals=lambda t: 0.5)
        a = compute_book_risk(m, n_samples=20_000, seed=42)
        b = compute_book_risk(m, n_samples=20_000, seed=42)
        assert a.governing_model_es_99_cc == b.governing_model_es_99_cc
        assert a.es_99_cc == b.es_99_cc
        assert a.challenger_es_99_cc == b.challenger_es_99_cc
        assert a.deterministic_max_loss_cc == b.deterministic_max_loss_cc


class TestDeterministicStress:
    def test_all_hit_loss_is_premium_sum(self) -> None:
        # The exact all-hit worst case = Σ (premium + fee) over positions.
        p1 = _pos("p1", (_leg("A", "KXWCGAME-G1"),), contracts=100, price_cc=5_000)
        p2 = _pos("p2", (_leg("B", "KXWCGAME-G2"),), contracts=200, price_cc=3_000)
        m = build_book_model([p1, p2], marginals=lambda t: 0.5)
        # contracts floor: 100cc→1 ct, 200cc→2 ct. Premium = 1*5000 + 2*3000 = 11000.
        assert _deterministic_all_hit_loss_cc(m) == pytest.approx(11_000.0)

    def test_deterministic_max_equals_all_hit_loss(self) -> None:
        # P0-3: the deterministic maximum axis is EXACTLY the all-hit premium sum
        # (no reserve here), reported on its OWN field — never folded into the ES.
        pos = _pos("p1", (_leg("A", "KXWCGAME-G1"),), contracts=100, price_cc=5_000)
        m = build_book_model([pos], marginals=lambda t: 0.5)
        snap = compute_book_risk(m, n_samples=20_000, seed=3)
        assert snap.deterministic_max_loss_cc == pytest.approx(
            _deterministic_all_hit_loss_cc(m)
        )

    def test_deterministic_max_upper_bounds_sampled_es(self) -> None:
        # The exact all-hit maximum is an unconditional upper bound the SAMPLED
        # model ES can never exceed — even though it is no longer max'd INTO it.
        pos = _pos("p1", (_leg("A", "KXWCGAME-G1"),), contracts=100, price_cc=5_000)
        m = build_book_model([pos], marginals=lambda t: 0.5)
        snap = compute_book_risk(m, n_samples=20_000, seed=3)
        assert snap.deterministic_max_loss_cc >= snap.governing_model_es_99_cc - 1e-6


class TestChallengerOverlay:
    def test_inflate_corr_pushes_same_game_toward_one(self) -> None:
        # P0-8: legs 0,1 are same-game (masked); leg 2 is cross-game (NOT masked).
        corr = np.array([[1.0, 0.2, 0.3], [0.2, 1.0, 0.3], [0.3, 0.3, 1.0]])
        mask = np.array(
            [
                [False, True, False],
                [True, False, False],
                [False, False, False],
            ]
        )
        out = _inflate_corr(corr, 0.5, mask)
        # masked same-game 0.2 → 0.2 + 0.5*(1-0.2) = 0.6.
        assert out[0, 1] == pytest.approx(0.6)
        assert out[1, 0] == pytest.approx(0.6)
        # cross-game 0.3 entries are UNCHANGED (not inflated to 0.65).
        assert out[0, 2] == pytest.approx(0.3)
        assert out[1, 2] == pytest.approx(0.3)
        assert out[0, 0] == 1.0  # diagonal preserved
        # the input matrix is not mutated in place.
        assert corr[0, 1] == pytest.approx(0.2)

    def test_inflate_corr_none_mask_touches_nothing(self) -> None:
        # No game grouping ⇒ conservative default is to inflate NOTHING (never
        # inflate blindly): off-diagonals returned unchanged.
        corr = np.array([[1.0, 0.2, 0.0], [0.2, 1.0, 0.0], [0.0, 0.0, 1.0]])
        out = _inflate_corr(corr, 0.5, None)
        assert out[0, 1] == pytest.approx(0.2)
        assert out[0, 2] == pytest.approx(0.0)
        assert out[0, 0] == 1.0

    def test_inflate_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            _inflate_corr(np.eye(2), 1.5)

    def test_challenger_es_at_least_production_for_correlated_no_book(self) -> None:
        # For a NO-seller book on a correlated game, inflating the correlation
        # FATTENS the joint-hit tail (more parlays hit together), so the
        # challenger ES should be >= the production-copula ES. This is the
        # anti-monoculture guarantee: a correlation under-estimate is caught.
        legs = (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1"))
        # Two NO positions on the same game, both selling the parlay.
        p1 = _pos("p1", legs, our_side=Side.NO, contracts=100, price_cc=2_000)
        p2 = _pos("p2", legs, our_side=Side.NO, contracts=100, price_cc=2_000)
        m = build_book_model(
            [p1, p2],
            marginals=lambda t: 0.6,
            within_game_rho=lambda a, b: (0.1, 0.3, 0.5),
        )
        snap = compute_book_risk(m, n_samples=80_000, seed=11, band="point")
        # challenger over-correlates → its tail loss is at least the production one
        # (allow a small MC slack).
        assert snap.challenger_es_99_cc >= snap.es_99_cc - 50.0

    def test_same_game_mask_groups_by_game(self) -> None:
        # P0-8 mask: same-game pair True, cross-game pair False, diagonal False.
        p1 = _pos("p1", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")))
        p2 = _pos("p2", (_leg("C", "KXWCGAME-G2"),))
        m = build_book_model([p1, p2], marginals=lambda t: 0.5)
        mask = _same_game_mask(m)
        ia, ib, ic = m.leg_index["A"], m.leg_index["B"], m.leg_index["C"]
        assert mask[ia, ib] and mask[ib, ia]  # A,B same game G1
        assert not mask[ia, ic]  # A,C different games
        assert not mask[ib, ic]  # B,C different games
        assert not mask[ia, ia]  # diagonal is False (restored by _inflate_corr)

    def test_challenger_leaves_cross_game_rho_unchanged(self) -> None:
        # MANDATORY (P0-8): same-game inflation must leave the cross-game rho
        # UNCHANGED. Two games, one two-leg same-game position each, so the built
        # matrix has same-game blocks AND cross-game (≈0) entries.
        p1 = _pos("p1", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")))
        p2 = _pos("p2", (_leg("C", "KXWCGAME-G2"), _leg("D", "KXWCGAME-G2")))
        m = build_book_model(
            [p1, p2],
            marginals=lambda t: 0.5,
            within_game_rho=lambda a, b: (0.1, 0.3, 0.5),
        )
        corr = m.corr_for_band("point")
        mask = _same_game_mask(m)
        challenger = _inflate_corr(corr, 0.5, mask)
        ia, ib = m.leg_index["A"], m.leg_index["B"]
        ic, id_ = m.leg_index["C"], m.leg_index["D"]
        # same-game entries inflated (0.3 → 0.3 + 0.5*0.7 = 0.65).
        assert challenger[ia, ib] == pytest.approx(0.65)
        assert challenger[ic, id_] == pytest.approx(0.65)
        # EVERY cross-game entry is byte-identical to the production matrix.
        for i, j in [(ia, ic), (ia, id_), (ib, ic), (ib, id_)]:
            assert challenger[i, j] == corr[i, j]
            assert challenger[j, i] == corr[j, i]

    def test_universal_inflation_can_reduce_tail_on_hedged_book(self) -> None:
        # MANDATORY (P0-8): prove universal positive correlation is NOT always
        # conservative. Build a CROSS-GAME HEDGED book: long YES on game G1's leg
        # A (loses only when A MISSES) and long NO on game G2's leg B (loses only
        # when B HITS). The ONLY book-loss state is (A miss AND B hit). A is likely
        # to hit and B likely to miss, so under INDEPENDENCE (production) that loss
        # state has ~2% probability — above the 1% tail, so the 0.99 ES sits at the
        # full -20000 loss. Forcing a POSITIVE cross-game rho makes A and B
        # co-move, so A-miss pairs with B-miss (the hedge fires) and the joint
        # loss state drops BELOW 1% → the 0.99 ES SHRINKS. An indiscriminate
        # (universal) inflation therefore UNDERSTATES the tail here; the P0-8
        # same-game-only challenger must not.
        pyes = _pos(
            "g1_yes",
            (_leg("A", "KXWCGAME-G1"),),
            our_side=Side.YES,
            contracts=100,
            price_cc=5_000,
        )
        pno = _pos(
            "g2_no",
            (_leg("B", "KXWCGAME-G2"),),
            our_side=Side.NO,
            contracts=100,
            price_cc=5_000,
        )
        # p(A hit)=0.85, p(B hit)=0.15 ⇒ P(loss)=P(A miss)·P(B hit)=0.15·0.15≈2.25%.
        marg = {"A": 0.85, "B": 0.15}
        m = build_book_model([pyes, pno], marginals=lambda t: marg[t])
        corr = m.corr_for_band("high")  # cross-game rho=0 ⇒ identity (independent)

        from combomaker.sim.book_risk import _book_pnl_from_values, _es_from_pnl
        from combomaker.sim.engine import sample_leg_values

        rng_p = np.random.default_rng(7)
        prod_vals = sample_leg_values(list(m.legs), corr, 200_000, rng_p)
        _, prod_es = _es_from_pnl(
            _book_pnl_from_values(prod_vals, m.positions), 0.99
        )

        # UNIVERSAL inflation (the OLD behaviour: every off-diagonal, incl. the
        # cross-game 0 → 0.5) — the non-conservative construction P0-8 removes.
        n = len(m.legs)
        universal_mask = ~np.eye(n, dtype=np.bool_)
        universal_corr = _inflate_corr(corr, 0.5, universal_mask)
        rng_u = np.random.default_rng(7)
        univ_vals = sample_leg_values(list(m.legs), universal_corr, 200_000, rng_u)
        _, univ_es = _es_from_pnl(
            _book_pnl_from_values(univ_vals, m.positions), 0.99
        )

        # The proof: universal positive correlation REDUCED the tail below the
        # (correct) independent tail — it is NOT conservative on this hedged book.
        # full loss = 5000 premium at risk on each of the two positions.
        assert prod_es == pytest.approx(10_000.0)
        assert univ_es < prod_es - 1_000.0

        # SAME-GAME-ONLY challenger (P0-8): each leg is its own game, so nothing is
        # inflated — the challenger equals production (never understates it).
        same_game_corr = _inflate_corr(corr, 0.5, _same_game_mask(m))
        np.testing.assert_allclose(same_game_corr, corr)


class TestGoverningRuin:
    """P1-1: P(ruin) is computed for the production AND the challenger (AND, when
    active, the bridge) book and the snapshot reports/gates on the WORST — mirroring
    the governing ES. A correlation under-estimate must not understate ruin either."""

    def _correlated_no_book(self):  # type: ignore[no-untyped-def]
        # Two NO parlays on ONE correlated game: inflating the within-game rho
        # breaks more of them together, so more mass lands in the deep-loss tail —
        # the ruin driver for a NO-seller.
        legs = (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1"))
        p1 = _pos("p1", legs, our_side=Side.NO, contracts=100, price_cc=2_000)
        p2 = _pos("p2", legs, our_side=Side.NO, contracts=100, price_cc=2_000)
        return build_book_model(
            [p1, p2],
            marginals=lambda t: 0.6,
            within_game_rho=lambda a, b: (0.1, 0.3, 0.5),
        )

    def test_governing_ruin_is_worst_of_production_and_challenger(self) -> None:
        # With NO correlation inflation (challenger == production, no bridge) the
        # governing ruin is the pure production value. Turning the inflation ON can
        # only RAISE it on this correlated NO book — the governing number must be
        # >= the production-only baseline (never below), i.e. it tracks the worst
        # credible model, not the convenient one.
        m = self._correlated_no_book()
        # Equity/floor chosen so the deep-loss tail straddles the ruin floor: a
        # $10k bankroll at 0.70 floor = $7k floor; equity marked just above it so a
        # multi-parlay break drops us through. Money in centi-cents.
        common = dict(
            n_samples=120_000,
            seed=11,
            band="point",
            bankroll_cc=100_000,      # $10.00 nominal bankroll (test scale)
            current_equity_cc=71_000,  # just above the 70% floor (70_000)
            ruin_floor_frac=0.70,
        )
        prod_only = compute_book_risk(
            self._correlated_no_book(), challenger_inflation=0.0, **common
        )
        governing = compute_book_risk(
            m, challenger_inflation=0.9, **common
        )
        # Sanity: with these settings ruin actually evaluates (non-trivial prob).
        assert prod_only.p_ruin > 0.0
        # The governing (worst-model) ruin is at least the production-only ruin.
        assert governing.p_ruin >= prod_only.p_ruin - 1e-9
        # And the inflation genuinely moved it up (this book's whole point).
        assert governing.p_ruin > prod_only.p_ruin

    def test_governing_ruin_never_below_production_alone(self) -> None:
        # The reported p_ruin can never be LESS than what the production book alone
        # would give — max() is monotone. Re-derive the production-only ruin from
        # the same production substream and assert the snapshot is >= it.
        m = self._correlated_no_book()
        snap = compute_book_risk(
            m,
            n_samples=120_000,
            seed=11,
            band="point",
            bankroll_cc=100_000,
            current_equity_cc=71_000,
            ruin_floor_frac=0.70,
            challenger_inflation=0.9,
        )
        # Reproduce ONLY the production substream (seed→spawn(3)[0]) and its p_ruin.
        from combomaker.sim.book_risk import _book_pnl_from_values, _p_ruin_from_pnl
        from combomaker.sim.engine import sample_leg_values

        seq_prod, _, _ = np.random.SeedSequence(11).spawn(3)
        corr = m.corr_for_band("point")
        prod_vals = sample_leg_values(
            list(m.legs), corr, 120_000, np.random.default_rng(seq_prod)
        )
        prod_ruin = _p_ruin_from_pnl(
            _book_pnl_from_values(prod_vals, m.positions), 71_000, 0.70 * 100_000
        )
        assert snap.p_ruin >= prod_ruin - 1e-9

    def test_no_equity_means_no_ruin_evaluated(self) -> None:
        # Fail-safe unchanged: without equity/bankroll the ruin axis stays 0.0
        # across ALL models (nothing to gate on), never a convenient default.
        m = self._correlated_no_book()
        snap = compute_book_risk(
            m, n_samples=40_000, seed=3, band="point", challenger_inflation=0.9
        )
        assert snap.p_ruin == 0.0


class TestTailAttribution:
    def test_per_game_sum_reconciles_to_cvar(self) -> None:
        # Σ per-game tail contribution == the book CVaR (es_99), an additive
        # decomposition. Two independent games, one NO position each.
        p1 = _pos("g1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO, price_cc=4_000)
        p2 = _pos("g2", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO, price_cc=4_000)
        m = build_book_model([p1, p2], marginals=lambda t: 0.5)
        snap = compute_book_risk(m, n_samples=100_000, seed=7, band="point")
        total = sum(c.loss_cc for c in snap.per_game_tail_cc)
        # Σ contributions ≈ CVaR (both positive loss magnitudes).
        assert total == pytest.approx(snap.es_99_cc, rel=0.02, abs=5.0)
        assert len(snap.per_game_tail_cc) == 2  # two distinct games named

    def test_per_leg_attribution_present(self) -> None:
        p = _pos("p1", (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1")), our_side=Side.NO)
        m = build_book_model([p], marginals=lambda t: 0.5)
        snap = compute_book_risk(m, n_samples=50_000, seed=2, band="point")
        assert len(snap.per_leg_tail_cc) == 2  # both legs attributed


class TestRuinThresholds:
    def test_p_loss_worse_than_at_bankroll_fractions(self) -> None:
        # A NO book that can lose its premium: P(loss > fraction*bankroll) reported.
        pos = _pos(
            "p1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO, contracts=100, price_cc=5_000
        )
        m = build_book_model([pos], marginals=lambda t: 0.5)
        snap = compute_book_risk(
            m, n_samples=100_000, seed=4, band="point", bankroll_cc=100_000
        )
        # thresholds at 10/25/60% of 100_000cc = 10k/25k/60k.
        keys = set(snap.p_loss_worse_than)
        assert 10_000.0 in keys and 25_000.0 in keys and 60_000.0 in keys
        # max loss is 5_000cc premium (1 ct @ $0.50) → never worse than 10k.
        assert snap.p_loss_worse_than[10_000.0] == 0.0

    def test_no_bankroll_no_ruin_thresholds(self) -> None:
        pos = _pos("p1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)
        m = build_book_model([pos], marginals=lambda t: 0.5)
        snap = compute_book_risk(m, n_samples=20_000, seed=1, band="point", bankroll_cc=None)
        assert snap.p_loss_worse_than == {}


class TestSnapshotShape:
    def test_snapshot_carries_provenance(self) -> None:
        pos = _pos("p1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)
        m = build_book_model([pos], marginals=lambda t: 0.5)
        snap = compute_book_risk(m, n_samples=12_345, seed=99, band="high")
        assert isinstance(snap, BookRiskSnapshot)
        assert snap.n_samples == 12_345
        assert snap.seed == 99
        assert snap.band == "high"
        assert snap.n_positions == 1
        assert snap.usable
