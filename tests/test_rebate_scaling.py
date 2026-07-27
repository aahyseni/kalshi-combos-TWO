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

LEVER #5 SUPERSESSION (2026-07-27). The 2026-07-26 fix above raised the
rebate's SCALE but left its BASIS a COUNT (``deficit = (share - 1/n)/(1 -
1/n)``) — and a count decays as we succeed: the absent-key rebate fell 42%
(0.0250 -> 0.0145) in ONE session purely BECAUSE the book diversified. So the
count basis is dead and the DIVERSIFICATION REWARD moved wholesale to
``risk/concentration_steer``: the AND-BOUND dollar-Herfindahl marginal (zero
standard error) priced by ``Cov(candidate payoff, book P&L)``. These two
axes keep ONLY the dollar-denominated half they can honestly speak to — the
WIDEN, measured as dollars against that axis's OWN ENFORCED WALL.

The DOCTRINE is unchanged and is still pinned, on the component that now owns
it: a diversifier gets a taker-visible rebate; it is book-size invariant; it
scales with the P(book) deficit; it never quotes through fair.
"""

from __future__ import annotations

import random

from combomaker.risk.concentration_steer import (
    SteerCenter,
    SteerInputs,
    build_loss_event_book,
    compute_concentration_steer,
)
from combomaker.risk.skew import (
    LegAxisProfile,
    PBookProfile,
    SkewParams,
    _leg_axis_side,
    _pbook_component,
)

TICK_CC = 10
FILL_ELASTICITY = 0.22


def _warm_centre(sd: float = 0.12) -> SteerCenter:
    c = SteerCenter(half_life=128.0)
    rng = random.Random(19)
    for _ in range(600):
        c.observe(rng.gauss(0.0, sd))
    return c


def _hhi_rebate_cc(
    *, book_cc: float, margin_cc: int = 200, centre: SteerCenter | None = None
) -> int:
    """The rebate a brand-new AND-bound loss event now earns, in pricer frame
    (positive = tighter). ``book_cc`` sizes the committed book — the whole
    point is that the answer does not depend on it."""
    out = compute_concentration_steer(
        SteerInputs(
            loss_events=build_loss_event_book(
                [(("G1",), book_cc * 0.9), (("G2",), book_cc * 0.1)]
            ),
            candidate_bucket=("G9",),
            candidate_premium_cc=book_cc * 0.2,
            walls_by_axis={},
            margin_cc=margin_cc,
            tick_cc=TICK_CC,
            fill_elasticity_per_cent=FILL_ELASTICITY,
        ),
        centre if centre is not None else _warm_centre(),
        observe=False,
    )
    return out.applied_cc

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
    cc, _rows, _s = _pbook_component(
        [("G9", 0)], PARAMS, _profile(total_cc, budget_cc), GEN
    )
    return cc


class TestPBookRebate:
    def test_small_book_still_pays_a_visible_rebate(self) -> None:
        # THE REGRESSION: book tail 1% of the cap. Under the old onset gate
        # this produced ~0.01c; it must be a real, taker-visible number. The
        # OWNER of the reward is now the AND-bound dollar-Herfindahl.
        cc = _hhi_rebate_cc(book_cc=2_400.0)
        assert cc > 0, "a diversifier must be rebated"
        assert cc >= 25, f"rebate {cc}cc is invisible to a taker"

    def test_rebate_is_book_size_invariant(self) -> None:
        # Wildly different book sizes, identical composition => same reward.
        # Now TRUE BY CONSTRUCTION: the Herfindahl marginal is a RATIO of
        # effective loss-event counts, so absolute dollars cancel.
        centre = _warm_centre()
        small = _hhi_rebate_cc(book_cc=2_400.0, centre=centre)
        large = _hhi_rebate_cc(book_cc=200_000.0, centre=centre)
        assert small == large

    def test_the_pbook_axis_pays_a_share_derived_rebate(self) -> None:
        """SUPERSEDES the count-derived rebate (2026-07-27). The basis is now
        the game's tail SHARE against the tail Herfindahl — a pure ratio, so
        it cannot decay as we diversify and cannot inflate as the book grows.
        A game with no tail presence reads exactly ``−H``: the rebate for
        diversifying IS how concentrated we currently are."""
        cc, rows, score = _pbook_component(
            [("G9", 0)], PARAMS, _profile(2_400.0, 240_000.0, p_book=0.2), GEN
        )
        assert cc < 0 and score is not None and score < 0.0
        assert [r[3] for r in rows] == ["pbook_diversifying"]

    def test_rebate_respects_the_clamp(self) -> None:
        # The clamp is now the DERIVED symmetric half-range, not an asymmetric
        # constant: it can never exceed the LIVE margin (markups are FIXED).
        for margin in (30, 200, 1_000):
            assert abs(_hhi_rebate_cc(book_cc=2_400.0, margin_cc=margin)) <= margin

    def test_widen_no_longer_has_a_book_size_gate(self) -> None:
        """SUPERSEDES ``test_widen_keeps_its_onset_gate`` (operator directive
        2026-07-27: "we shouldn't be widening all of our bets just because we
        hit a $ amount of positions"). The onset gate WAS the book-size term —
        the book's own tail dollars over a fixed wall — so the identical
        candidate against the identical SHAPE now pays the identical widen at
        any book size, and refusal stays with the caps."""
        tiny, _r, _s = _pbook_component(
            [("G1", 100)], PARAMS, _profile(2_400.0, 240_000.0), GEN
        )
        near_cap, _r2, _s2 = _pbook_component(
            [("G1", 100)], PARAMS, _profile(240_000.0, 240_000.0), GEN
        )
        assert tiny == near_cap > 0


class TestLegAxisRebate:
    def _side(self, total_cc: float, budget_cc: float) -> int:
        prof = LegAxisProfile(
            shares_by_family={"KXMLBKS:yes": 0.9, "KXMLBTOTAL:no": 0.1},
            total_family_cc=total_cc,
            shares_by_entity={},
            total_entity_cc=0.0,
            family_budget_cc=budget_cc,
            entity_budget_cc=budget_cc,
            p_book=0.4,
        )
        cc, _rows, _score = _leg_axis_side(
            ["KXMLBKS:no"],  # the MISSING direction — what we want to attract
            prof.shares_by_family,
            PARAMS,
        )
        return cc

    def test_missing_direction_pays_a_share_derived_rebate(self) -> None:
        """SUPERSEDES the count-derived rebate. The MISSING direction is a
        different family key carrying ZERO share, so it reads ``−H`` against
        the axis's own Herfindahl — a rebate priced in shape, not in a
        1/n_keys count that decays as we succeed."""
        assert self._side(total_cc=2_400.0, budget_cc=240_000.0) < 0
        assert _hhi_rebate_cc(book_cc=2_400.0) >= 25

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
