"""P(book) VISIBILITY — Phase A of the P(book)-steering build (operator
directive 2026-07-25: "We need to be using Pbook, it should be steering our
betting, more variance = higher Pbook").

Phase A is SHADOW: ``p_profit`` rides every candidate-gate tail axis and the
verdict carries ``candidate_delta_p_book`` (post − pre on common random
numbers) — logged everywhere, gated on by NOTHING. These tests pin the
measurement itself, including the 7/23 slate lesson in miniature: a
DIVERSIFYING candidate on an independent game RAISES P(book) while a
SAME-WAY CONCENTRATOR leaves it flat — the signal the Phase-B steer derives
from.
"""

from __future__ import annotations

from combomaker.core.conventions import Side
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.sim.book_risk import evaluate_candidate_book_risk


def _pos(
    position_id: str,
    legs: tuple[LegRef, ...],
    *,
    contracts: int = 100,
    price_cc: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=position_id,
        combo_ticker=f"COMBO-{position_id}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=price_cc,  # type: ignore[arg-type]
        legs=legs,
        risk_modeled=True,
    )


def _leg(ticker: str, event: str) -> LegRef:
    return LegRef(market_ticker=ticker, event_ticker=event, side="yes")


class TestPBookMeasurement:
    def test_pre_p_book_matches_the_book_win_probability(self) -> None:
        # One committed NO on a parlay that HITS 10% of the time: the book
        # profits on every miss ⇒ P(book) ≈ 0.90.
        committed = [_pos("c1", (_leg("A", "KXMLBGAME-G1"),))]
        cand = _pos("cand", (_leg("B", "KXMLBGAME-G2"),), price_cc=1_000)
        r = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.10, n_samples=40_000, seed=11
        )
        assert abs(r.pre.p_profit - 0.90) < 0.01

    def test_delta_p_book_is_post_minus_pre(self) -> None:
        committed = [_pos("c1", (_leg("A", "KXMLBGAME-G1"),))]
        cand = _pos("cand", (_leg("B", "KXMLBGAME-G2"),), price_cc=1_000)
        r = evaluate_candidate_book_risk(
            committed, cand, marginals=lambda t: 0.5, n_samples=20_000, seed=11
        )
        assert r.candidate_delta_p_book == r.post.p_profit - r.pre.p_profit

    def test_diversifier_raises_p_book_concentrator_does_not(self) -> None:
        """The 7/23 slate in miniature (operator: 'more variance = higher
        Pbook'). Committed: a coin-flip one-way NO (P(book) ≈ 0.40 — the
        Detroit shape). A DIVERSIFYING candidate (independent game, high-EV,
        2× size) lifts P(book) toward 0.90; a SAME-WAY CONCENTRATOR (same
        leg, same side) leaves P(book) flat while stacking the same outcome.
        The steering signal must SEE that difference."""
        one_way_leg = (_leg("A", "KXMLBGAME-G1"),)
        committed = [_pos("c1", one_way_leg, contracts=100, price_cc=5_000)]
        marginals = {"A": 0.60, "B": 0.10}  # A hits 60% (we lose); B hits 10%

        diversifier = _pos(
            "cand-div",
            (_leg("B", "KXMLBGAME-G2"),),
            contracts=200,  # 2x size so its win outweighs an A loss
            price_cc=5_000,
        )
        concentrator = _pos(
            "cand-conc", one_way_leg, contracts=200, price_cc=5_000
        )

        r_div = evaluate_candidate_book_risk(
            committed,
            diversifier,
            marginals=lambda t: marginals[t],
            n_samples=40_000,
            seed=13,
        )
        r_conc = evaluate_candidate_book_risk(
            committed,
            concentrator,
            marginals=lambda t: marginals[t],
            n_samples=40_000,
            seed=13,
        )

        assert abs(r_div.pre.p_profit - 0.40) < 0.01  # the one-way book
        # The diversifier ADDS variance: book profits whenever B wins
        # (0.90) regardless of A ⇒ P(book) ≈ 0.90, delta ≈ +0.50.
        assert r_div.candidate_delta_p_book > 0.40
        assert abs(r_div.post.p_profit - 0.90) < 0.01
        # The same-way concentrator changes NOTHING about when the book
        # profits — it only deepens the one-way outcome.
        assert abs(r_conc.candidate_delta_p_book) < 0.01
        # The signal separates them decisively.
        assert r_div.candidate_delta_p_book > r_conc.candidate_delta_p_book + 0.35
