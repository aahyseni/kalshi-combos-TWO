"""REWARD IS NOT ONSET-GATED (2026-07-26).

Measured live: rebates had a MEDIAN of 0.02c against a 1-4c markup ladder
(997,581 skew events; 2,390,157 'diversifying' classifications). The steer
recognised diverse flow on nearly every quote and priced that recognition at
nothing, so the book kept filling one direction (short K-overs $282 of a
~$820 book) and p_book sat at 0.37 with ZERO legs live — a design failure,
not variance.

Root cause: every rebate was cap x deficit x need x ONSET, where onset =
concentrated tail / enforced cap. Onset is the correct gate for the PENALTY
(never tax a small book) and the wrong one for the REWARD (a diversifier is
not worth less because the book is small). These tests pin the asymmetry:
rebates scale on measured deficit x need only; widens keep their onset gate.
"""

from __future__ import annotations

from combomaker.risk.skew import LegAxisProfile, PBookProfile, SkewParams, _leg_axis_side, _pbook_component

PARAMS = SkewParams(enabled=True)
GEN = 3


def _profile(total_cc: float, budget_cc: float, *, p_book: float = 0.4) -> PBookProfile:
    return PBookProfile(
        input_generation=GEN,
        p_book=p_book,
        tail_share_by_game={"G1": 0.9, "G2": 0.1},
        total_tail_cc=total_cc,
        game_budget_cc=budget_cc,
        protected_games=frozenset(),
    )


def _rebate_cc(total_cc: float, budget_cc: float) -> int:
    # A brand-new game (no tail presence) = the purest diversifier.
    cc, _rows = _pbook_component([("G9", 0)], PARAMS, _profile(total_cc, budget_cc), GEN)
    return cc


class TestPBookRebate:
    def test_small_book_still_pays_a_visible_rebate(self) -> None:
        # THE REGRESSION: book tail 1% of the cap. Under the old onset gate
        # this produced ~0.01c; it must now be a real, taker-visible number.
        cc = _rebate_cc(total_cc=2_400.0, budget_cc=240_000.0)
        assert cc < 0, "a diversifier must be rebated"
        assert abs(cc) >= 25, f"rebate {abs(cc)}cc is invisible to a taker"

    def test_rebate_is_book_size_invariant(self) -> None:
        # Same deficit + same need, wildly different book sizes ⇒ same reward.
        small = _rebate_cc(total_cc=2_400.0, budget_cc=240_000.0)
        large = _rebate_cc(total_cc=200_000.0, budget_cc=240_000.0)
        assert small == large

    def test_rebate_scales_with_the_pbook_deficit(self) -> None:
        # need = 1 - p_book: a sick book pays MORE for balance than a healthy one.
        sick, _ = _pbook_component(
            [("G9", 0)], PARAMS, _profile(2_400.0, 240_000.0, p_book=0.2), GEN
        )
        healthy, _ = _pbook_component(
            [("G9", 0)], PARAMS, _profile(2_400.0, 240_000.0, p_book=0.9), GEN
        )
        assert abs(sick) > abs(healthy)

    def test_rebate_respects_the_clamp(self) -> None:
        cc = _rebate_cc(total_cc=2_400.0, budget_cc=240_000.0)
        assert abs(cc) <= PARAMS.peak_tighten_max_cc

    def test_widen_keeps_its_onset_gate(self) -> None:
        # The PENALTY must still be onset-gated: a tiny book concentrating is
        # nearly free; the same shape near the cap pays the full widen.
        tiny, _ = _pbook_component(
            [("G1", 100)], PARAMS, _profile(2_400.0, 240_000.0), GEN
        )
        near_cap, _ = _pbook_component(
            [("G1", 100)], PARAMS, _profile(240_000.0, 240_000.0), GEN
        )
        assert 0 <= tiny < near_cap


class TestLegAxisRebate:
    def _side(self, total_cc: float, budget_cc: float) -> int:
        prof = LegAxisProfile(
            shares_by_family={"KXMLBKS:yes": 0.9, "KXMLBTOTAL:no": 0.1},
            total_family_cc=total_cc,
            shares_by_entity={},
            total_entity_cc=0.0,
            budget_cc=budget_cc,
            p_book=0.4,
        )
        cc, _ = _leg_axis_side(
            ["KXMLBKS:no"],  # the MISSING direction — what we want to attract
            prof.shares_by_family,
            prof.total_family_cc,
            prof.budget_cc,
            1.0 - 0.4,
            PARAMS,
        )
        return cc

    def test_missing_direction_gets_a_visible_rebate(self) -> None:
        cc = self._side(total_cc=2_400.0, budget_cc=240_000.0)
        assert cc < 0 and abs(cc) >= 25

    def test_leg_rebate_is_book_size_invariant(self) -> None:
        assert self._side(2_400.0, 240_000.0) == self._side(200_000.0, 240_000.0)


class TestRebateNeverExceedsTheEdge:
    """A meaningful rebate must never quote us THROUGH fair: that fill is
    negative-EV, and the confirm gate declines negative-EV fills — which
    would manufacture the won-then-reneged auctions the 2026-07-25 audit
    eliminated. The rebate is capped at the margin being charged."""

    def _no_bid(self, skew_cc: int, markup_cc: int) -> int:
        from combomaker.pricing.quote import ConstructedQuote, QuoteParams
        from tests.test_quote import build_quote

        q = build_quote(
            params=QuoteParams(base_width_cc=0, per_leg_width_cc=0,
                               size_width_cc_per_100=0),
            markup_cc=markup_cc,
            inventory_skew_cc=skew_cc,
        )
        assert isinstance(q, ConstructedQuote), q
        return int(q.no_bid_cc)

    def test_huge_rebate_cannot_bid_through_fair(self) -> None:
        # joint p=0.30 ⇒ YES fair 3000cc, NO fair 7000cc. A 3c rebate (the
        # composed clamp) against a 1c mains markup must not cross NO fair.
        assert self._no_bid(skew_cc=300, markup_cc=100) <= 7_000

    def test_rebate_within_the_margin_still_lands(self) -> None:
        # A rebate smaller than the markup must still tighten the quote —
        # the cap bounds the reward, it does not disable it.
        assert self._no_bid(skew_cc=200, markup_cc=400) > self._no_bid(
            skew_cc=0, markup_cc=400
        )
