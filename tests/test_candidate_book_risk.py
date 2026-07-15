"""P0-1: candidate- and reservation-aware portfolio risk (A2 last-look gate).

The mandatory tests for ``evaluate_candidate_book_risk``:
  * a CONCENTRATING candidate crosses the ruin / ES budget and DECLINES;
  * a BALANCING candidate LOWERS the post-risk tail and can PASS;
  * a negative-EV HEDGE DECLINES absent explicit authorization (and passes only
    with an enabled budget within cost);
  * a NEW-GAME candidate and OUTSTANDING RESERVATIONS are included in the
    pre/post evaluation on common sampled states.
Plus the fail-closed UNKNOWN-marginal path and common-random-number determinism.
"""

from __future__ import annotations

import numpy as np

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_risk import evaluate_candidate_book_risk


def _pos(
    position_id: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.NO,
    contracts: int = 100,
    price_cc: int = 5_000,
    risk_modeled: bool = True,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=f"COMBO-{position_id}",
        collection=None,
        our_side=our_side,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
        risk_modeled=risk_modeled,
    )


def _leg(ticker: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side=side)


# A NO-seller sells the parlay: the worst case (the parlay HITS) forfeits the
# premium. A high hit probability (marginals near 1) makes the loss frequent, so
# the sampled ES / P(ruin) are material — good for exercising the budgets.


class TestCommonRandomNumbers:
    def test_same_seed_identical_verdict(self) -> None:
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        cand = _pos("cand", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO)
        a = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.6, n_samples=20_000, seed=5
        )
        b = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.6, n_samples=20_000, seed=5
        )
        assert a.candidate_ev_cc == b.candidate_ev_cc
        assert a.post.governing_model_es_99_cc == b.post.governing_model_es_99_cc
        assert a.post.p_ruin == b.post.p_ruin
        assert a.confirm == b.confirm

    def test_pre_and_post_share_states(self) -> None:
        # POST P&L is PRE P&L plus the candidate's on the SAME scenarios, so with a
        # positive-EV candidate the post EV strictly exceeds the pre EV (the
        # marginal EV is the candidate's, not sampling noise).
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        # A cheaply-priced NO on a rarely-hitting parlay is +EV to sell.
        cand = _pos(
            "cand", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO, price_cc=1_000
        )
        r = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.10, n_samples=40_000, seed=7
        )
        assert r.candidate_ev_cc > 0.0
        assert r.post.ev_cc > r.pre.ev_cc


class TestConcentratingCandidateDeclines:
    # A NO-seller is +EV only when the sell price is BELOW the fair miss-value. On a
    # parlay that HITS with prob 0.9 (misses 0.1), the NO's fair value is ~$0.10, so
    # a NO sold at $0.09 (900cc) is marginally +EV — clearing the EV gate — yet its
    # tail loss (the premium, forfeited when the parlay HITS 90% of the time) is
    # large. This lets the CONCENTRATING candidate be admitted by the EV gate and
    # then DECLINED by the joint-tail / ruin budget, which is the point of P0-1.

    def test_concentrating_candidate_crosses_ruin_budget(self) -> None:
        # A book whose committed loss ALONE stays above the ruin floor, but the
        # extra loss a +EV-but-CONCENTRATING same-game candidate adds crosses it.
        # The candidate concentrates on the SAME game (same leg) as the committed
        # NO, so both forfeit their premium together in the 90% of scenarios the
        # parlay HITS — the joint loss (10_800cc) breaches the floor gap while the
        # committed loss alone (1_800cc) does not. A committed-only snapshot would
        # never see this; the candidate-aware POST book does.
        bankroll = 200_000
        leg = (_leg("A", "KXWCGAME-G1"),)
        committed = [
            _pos("c1", leg, our_side=Side.NO, contracts=200, price_cc=900)
        ]
        cand = _pos("cand", leg, our_side=Side.NO, contracts=1_000, price_cc=900)
        r = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: 0.90,  # parlay hits 90% → the NO forfeits its premium
            n_samples=40_000,
            seed=3,
            bankroll_cc=bankroll,
            # Floor = 140_000cc; equity 145_000cc ⇒ gap 5_000cc. Committed loss
            # 1_800cc stays above; committed+candidate 10_800cc breaches.
            current_equity_cc=145_000,
            ruin_floor_frac=0.70,
            portfolio_ruin_prob_budget=0.05,
        )
        assert r.candidate_ev_cc > 0.0  # cleared the EV gate
        assert not r.confirm
        assert r.decline_reason == "post_ruin_prob_over_budget"
        assert r.pre.p_ruin < 0.05  # committed alone is inside the ruin budget
        assert r.post.p_ruin > r.pre.p_ruin  # the candidate ADDED ruin risk

    def test_concentrating_candidate_crosses_es_budget(self) -> None:
        # A tiny CVaR budget the post joint tail cannot fit under → decline on the
        # governing-model-ES axis (the candidate is +EV, so the EV gate passes).
        bankroll = 100_000
        committed = [
            _pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO, price_cc=900)
        ]
        cand = _pos(
            "cand",
            (_leg("B", "KXWCGAME-G2"),),
            our_side=Side.NO,
            contracts=300,
            price_cc=900,
        )
        r = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: 0.9,
            n_samples=40_000,
            seed=8,
            bankroll_cc=bankroll,
            portfolio_cvar_frac=0.01,  # 1% = 1_000cc, far below the post tail
        )
        assert r.candidate_ev_cc > 0.0
        assert not r.confirm
        assert r.decline_reason == "post_governing_model_es_over_budget"


class TestBalancingCandidatePasses:
    def test_balancing_candidate_lowers_post_ruin_and_can_pass(self) -> None:
        # A committed NO-parlay on game G1 loses when G1's parlay HITS. A candidate
        # that is LONG YES on the SAME game (our_side YES) PAYS OFF exactly when the
        # committed NO loses — a hedge that lowers the joint tail. On common random
        # numbers the post P(ruin) is <= the pre P(ruin), and with a +EV hedge it
        # can pass. We give the candidate a genuinely +EV price so it is not a
        # negative-EV hedge (which would be declined by design).
        bankroll = 200_000
        legs = (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1"))
        committed = [
            _pos(
                "c1",
                legs,
                our_side=Side.NO,
                contracts=300,
                price_cc=3_000,
            )
        ]
        # LONG YES on the same parlay, bought cheap → +EV given the parlay hits
        # often (marginals 0.9 each ⇒ joint ~0.81), and it pays when the NO loses.
        cand = _pos(
            "cand",
            legs,
            our_side=Side.YES,
            contracts=300,
            price_cc=1_000,
        )
        r = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: 0.9,
            within_game_rho=lambda a, b: (0.3, 0.5, 0.7),
            n_samples=60_000,
            seed=4,
            band="high",
            bankroll_cc=bankroll,
            current_equity_cc=int(0.80 * bankroll),
            ruin_floor_frac=0.70,
            portfolio_ruin_prob_budget=0.05,
            portfolio_cvar_frac=0.60,
            portfolio_det_max_frac=0.90,
        )
        # The hedge lowers (never raises) the post ruin probability vs pre.
        assert r.post.p_ruin <= r.pre.p_ruin
        assert r.candidate_ev_cc > 0.0
        assert r.confirm

    def test_balancing_candidate_gets_mc_credit_in_es(self) -> None:
        # The same hedge lowers the governing model ES relative to a CONCENTRATING
        # candidate of the same size/price on a NEW independent game — the credit a
        # committed-only snapshot could never award the balancing fill.
        legs = (_leg("A", "KXWCGAME-G1"), _leg("B", "KXWCGAME-G1"))
        committed = [_pos("c1", legs, our_side=Side.NO, contracts=300, price_cc=3_000)]
        hedge = _pos("hedge", legs, our_side=Side.YES, contracts=300, price_cc=1_000)
        concentrate = _pos(
            "conc",
            (_leg("C", "KXWCGAME-G2"), _leg("D", "KXWCGAME-G2")),
            our_side=Side.NO,
            contracts=300,
            price_cc=3_000,
        )
        rho = lambda a, b: (0.3, 0.5, 0.7)  # noqa: E731
        r_hedge = evaluate_candidate_book_risk(
            committed, hedge, marginals=lambda t: 0.9,
            within_game_rho=rho, n_samples=60_000, seed=9,
        )
        r_conc = evaluate_candidate_book_risk(
            committed, concentrate, marginals=lambda t: 0.9,
            within_game_rho=rho, n_samples=60_000, seed=9,
        )
        # The hedge's POST governing ES is strictly below the concentrator's.
        assert (
            r_hedge.post.governing_model_es_99_cc
            < r_conc.post.governing_model_es_99_cc
        )


class TestNegativeEvHedge:
    def _hedge_inputs(self) -> tuple[list[OpenPosition], OpenPosition]:
        legs = (_leg("A", "KXWCGAME-G1"),)
        committed = [_pos("c1", legs, our_side=Side.NO, contracts=200, price_cc=2_000)]
        # LONG YES bought EXPENSIVELY on a rarely-hitting parlay → negative EV, but
        # it hedges the committed NO's tail.
        hedge = _pos("hedge", legs, our_side=Side.YES, contracts=200, price_cc=9_500)
        return committed, hedge

    def test_negative_ev_hedge_declines_without_authorization(self) -> None:
        committed, hedge = self._hedge_inputs()
        r = evaluate_candidate_book_risk(
            committed, hedge, marginals=lambda t: 0.05, n_samples=40_000, seed=2
        )
        assert r.candidate_ev_cc < 0.0
        assert not r.confirm
        assert r.decline_reason == "negative_ev_no_hedge_budget"

    def test_negative_ev_hedge_declines_over_budget(self) -> None:
        committed, hedge = self._hedge_inputs()
        r = evaluate_candidate_book_risk(
            committed,
            hedge,
            marginals=lambda t: 0.05,
            n_samples=40_000,
            seed=2,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=1,  # far below the actual EV cost
        )
        assert not r.confirm
        assert r.decline_reason == "negative_ev_exceeds_hedge_budget"

    def test_negative_ev_hedge_passes_within_budget(self) -> None:
        committed, hedge = self._hedge_inputs()
        # First measure the actual EV cost, then authorize a budget above it.
        probe = evaluate_candidate_book_risk(
            committed, hedge, marginals=lambda t: 0.05, n_samples=40_000, seed=2
        )
        cost = int(-probe.candidate_ev_cc) + 1
        r = evaluate_candidate_book_risk(
            committed,
            hedge,
            marginals=lambda t: 0.05,
            n_samples=40_000,
            seed=2,
            allow_negative_ev_hedge=True,
            hedge_cost_budget_cc=cost,
        )
        assert r.candidate_ev_cc < 0.0
        assert r.confirm  # authorized hedge within budget, no post budgets tripped


class TestNewGameCandidate:
    def test_new_game_candidate_included(self) -> None:
        # The committed book touches only G1; the candidate introduces a NEW game
        # G2. The merged model must include G2's leg (so n_post > n_pre) and the
        # candidate must add positive EV (a +EV sell on an independent new game).
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        cand = _pos(
            "cand", (_leg("Z", "KXWCGAME-G2"),), our_side=Side.NO, price_cc=1_000
        )
        r = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.10, n_samples=40_000, seed=6
        )
        assert r.n_pre_positions == 1
        assert r.n_post_positions == 2
        assert r.candidate_ev_cc > 0.0
        # The new game raised the deterministic all-hit maximum by the candidate's
        # premium (1 ct @ $0.10 = 1_000cc).
        assert r.post.deterministic_max_loss_cc > r.pre.deterministic_max_loss_cc


class TestReservationsIncluded:
    def test_outstanding_reservations_in_pre_book(self) -> None:
        # An outstanding reservation must count in the PRE book: two reservations on
        # game G1 plus the committed NO make the PRE tail already large, so the same
        # candidate is charged against a larger pre book (its post ruin is >= a run
        # with no reservations).
        bankroll = 200_000
        committed = [
            _pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO, contracts=200,
                 price_cc=9_000)
        ]
        reservations = [
            _pos("r1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO, contracts=200,
                 price_cc=9_000),
            _pos("r2", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO, contracts=200,
                 price_cc=9_000),
        ]
        cand = _pos("cand", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO,
                    contracts=200, price_cc=9_000)
        with_res = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.95,
            reservations=reservations, n_samples=40_000, seed=1,
            bankroll_cc=bankroll, current_equity_cc=int(0.80 * bankroll),
        )
        without_res = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.95,
            n_samples=40_000, seed=1,
            bankroll_cc=bankroll, current_equity_cc=int(0.80 * bankroll),
        )
        # Reservations counted in PRE ⇒ n_pre includes them, and the deterministic
        # maximum and ruin are strictly larger than without.
        assert with_res.n_pre_positions == 3  # committed + 2 reservations
        assert without_res.n_pre_positions == 1
        assert (
            with_res.post.deterministic_max_loss_cc
            > without_res.post.deterministic_max_loss_cc
        )
        assert with_res.post.p_ruin >= without_res.post.p_ruin

    def test_simultaneous_accepts_in_pre_book(self) -> None:
        # A simultaneously-executable accept is folded into the PRE book exactly
        # like a reservation (both are held-but-not-committed exposure).
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        accept = _pos("acc", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO)
        cand = _pos("cand", (_leg("C", "KXWCGAME-G3"),), our_side=Side.NO)
        r = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.5,
            simultaneous_accepts=[accept], n_samples=20_000, seed=1,
        )
        assert r.n_pre_positions == 2  # committed + 1 simultaneous accept
        assert r.n_post_positions == 3


class TestFailClosed:
    def test_unknown_marginal_declines(self) -> None:
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        cand = _pos("cand", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO)
        # B has no marginal ⇒ merged model UNKNOWN ⇒ fail-closed decline.
        r = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: 0.5 if t == "A" else None,
            n_samples=10_000,
            seed=1,
        )
        assert r.unknown
        assert not r.usable
        assert not r.confirm
        assert r.decline_reason == "unknown_marginal"

    def test_reserved_candidate_not_sampled_but_counts(self) -> None:
        # A reserved (unmodeled) candidate is never sampled — its missing marginal
        # cannot force UNKNOWN — but its premium still enters the deterministic
        # maximum and gross.
        committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
        cand = _pos(
            "cand",
            (_leg("HELD", "KXOTHER-G9"),),
            our_side=Side.NO,
            contracts=200,
            price_cc=6_000,
            risk_modeled=False,
        )
        r = evaluate_candidate_book_risk(
            committed,
            cand,
            marginals=lambda t: 0.5 if t == "A" else None,  # HELD marginal absent
            n_samples=20_000,
            seed=1,
        )
        assert not r.unknown  # reserved candidate never poisons the model
        # Its premium (2 ct @ $0.60 = 12_000cc) is added to the deterministic max.
        assert r.post.deterministic_max_loss_cc > r.pre.deterministic_max_loss_cc


def test_module_smoke_positive_ev_confirms() -> None:
    # A clean +EV candidate with generous budgets confirms.
    committed = [_pos("c1", (_leg("A", "KXWCGAME-G1"),), our_side=Side.NO)]
    cand = _pos("cand", (_leg("B", "KXWCGAME-G2"),), our_side=Side.NO, price_cc=1_000)
    r = evaluate_candidate_book_risk(
        committed,
        cand,
        marginals=lambda t: 0.10,
        n_samples=40_000,
        seed=1,
        bankroll_cc=200_000,
        portfolio_cvar_frac=0.50,
        portfolio_det_max_frac=0.90,
        portfolio_ruin_prob_budget=0.50,
        absolute_notional_multiple=5,
    )
    assert r.confirm
    assert r.decline_reason == ""
    assert isinstance(r.candidate_ev_cc, float)
    assert not np.isnan(r.candidate_ev_cc)
