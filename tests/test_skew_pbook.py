"""P(book) steering component, Phase B1 (operator directive 2026-07-25:
"Pbook should be steering our betting, more variance = higher Pbook").

The component is SHADOW-FIRST: ``pbook_armed`` defaults False, so ``skew_cc``
(and therefore pricing) is byte-identical whether or not a profile is wired —
pinned below — while ``pbook_cc`` is computed + logged for the
derive-before-arm measurement. Semantics pinned:

- same-way add on an OVERWEIGHT game (the DET one-way shape) ⇒ WIDEN;
- offsetting an overweight game (the missing other side)     ⇒ REBATE;
- ANY flow on an underweight/empty game (new-game variance)  ⇒ REBATE;
- absent/stale profile, empty tail, disabled                 ⇒ EXACTLY 0;
- magnitude scales with the P(book) deficit (a healthy 0.9 book barely
  steers; the 7/23 one-way 0.40 book steers hard).
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.concentration_steer import SteerCenter, build_loss_event_book
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.skew import (
    ConcentrationProfile,
    PBookProfile,
    SkewLimits,
    SkewParams,
    compute_inventory_skew,
    ticket_bucket,
)

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

LIMITS = SkewLimits(
    max_event_delta_contracts=500.0,
    max_event_worst_case_loss_dollars=100_000.0,
    max_event_gross_notional_dollars=500_000.0,
)

PARAMS = SkewParams(enabled=True)  # pbook_enabled=True, pbook_armed=False
GEN = 7


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


def leg(market: str, event: str) -> LegRef:
    return LegRef(market_ticker=market, event_ticker=event, side="yes")


def no_position(
    pid: str, legs: tuple[LegRef, ...], *, contracts: int = 10_000
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker="COMBO",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(5_000),
        legs=legs,
    )


def _one_way_book() -> tuple[ExposureBook, dict[str, float]]:
    """A book concentrated one-way on game G1 (the DET shape)."""
    book = ExposureBook(CONVENTIONS)
    marginals = {"A": 0.5, "B": 0.5, "C": 0.5}
    book.add_position(
        no_position("held", (leg("A", "KX-G1"),), contracts=60_000)
    )
    return book, marginals


def _profile(
    *,
    p_book: float = 0.40,
    shares: dict[str, float] | None = None,
    gen: int = GEN,
    total_tail_cc: float = 1.0e9,  # sized ~= game_budget_cc so the
    # caps-derived onset is ~1 in the semantics tests (onset itself is pinned
    # separately in TestCapsDerivedOnset)
    game_budget_cc: float = 1.0e9,
    protected: frozenset[str] = frozenset(),
) -> PBookProfile:
    return PBookProfile(
        input_generation=gen,
        p_book=p_book,
        tail_share_by_game=(
            {"G1": 0.90, "G2": 0.10} if shares is None else shares
        ),
        total_tail_cc=total_tail_cc,
        game_budget_cc=game_budget_cc,
        protected_games=protected,
    )


def _warm_centre(sd: float = 0.12) -> SteerCenter:
    """A centre warmed on real-shaped flow, as the live one always is."""
    c = SteerCenter(half_life=128.0)
    rng = random.Random(19)
    for _ in range(600):
        c.observe(rng.gauss(0.0, sd))
    return c


def _conc(book: ExposureBook, *, centre: SteerCenter | None = None,
          wall_cc: float = 1.0e9) -> ConcentrationProfile:
    """LEVER #5 profile from the committed book, ticket-atomic (2026-07-27).

    The DIVERSIFICATION rebate that used to be priced by the 1/n_keys COUNT
    deficit lives here now, on the AND-BOUND dollar-Herfindahl."""
    events = [
        (ticket_bucket(p.legs), float(p.max_loss_cc))
        for p in book.positions.values()
    ]
    return ConcentrationProfile(
        loss_events=build_loss_event_book(events),
        game_dollars_cc={},
        game_wall_cc=wall_cc,
        family_dollars_cc={},
        family_wall_cc=wall_cc,
        entity_dollars_cc={},
        entity_wall_cc=wall_cc,
        fill_elasticity_per_cent=0.22,
        centre=centre if centre is not None else _warm_centre(),
    )


def _skew(candidate, book, marginals, profile, *, params=PARAMS, gen=GEN,
          conc_profile=None, margin_cc=200, tick_cc=10):
    # LEVER #5 ARMING (2026-07-27 review B3): the steer ships SHADOW, so a test
    # that wires a ``conc_profile`` AND asserts on the composed ``applied_cc``
    # has to arm it explicitly. Byte-identity of the UNARMED composition is
    # pinned in tests/test_conc_arming.py, not here.
    if conc_profile is not None and not params.conc_armed:
        params = dataclasses.replace(params, conc_armed=True)
    snap = book.snapshot(provider(marginals), mass_acceptance=False)
    return compute_inventory_skew(
        candidate,
        snap,
        provider(marginals),
        CONVENTIONS,
        LIMITS,
        params,
        pbook_profile=profile,
        pbook_book_generation=gen,
        conc_profile=conc_profile,
        margin_cc=margin_cc,
        tick_cc=tick_cc,
        observe_centre=False,
    )


class TestShadowInvariant:
    def test_skew_cc_byte_identical_while_unarmed(self) -> None:
        """The load-bearing shadow property: with pbook_armed=False the
        composed skew_cc is IDENTICAL with and without a profile — pricing
        cannot move until the operator arms the steer."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        with_profile = _skew(cand, book, marginals, _profile())
        without = _skew(cand, book, marginals, None)
        assert with_profile.skew_cc == without.skew_cc
        assert with_profile.applied_cc == without.applied_cc
        assert with_profile.pbook_cc != 0  # ...but the signal IS measured
        assert without.pbook_cc == 0

    def test_armed_adds_the_component(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        armed = SkewParams(enabled=True, pbook_armed=True)
        shadow = _skew(cand, book, marginals, _profile())
        live = _skew(cand, book, marginals, _profile(), params=armed)
        assert live.skew_cc == shadow.skew_cc + shadow.pbook_cc


class TestSteeringSemantics:
    def test_same_way_on_overweight_game_widens(self) -> None:
        """The DET shape: adding same-way risk to the game that already
        dominates the tail ⇒ positive (widen) component."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(cand, book, marginals, _profile())
        assert skew.pbook_cc > 0
        assert any(r[3] == "pbook_concentrating" for r in skew.pbook_per_game)

    def test_offsetting_overweight_game_rebates(self) -> None:
        """The missing other side of the concentrated game earns a rebate."""
        book, marginals = _one_way_book()
        # Opposite side of the SAME game: a NO on the complementary leg has
        # the opposite delta sign on G1.
        cand = no_position("cand", (LegRef(market_ticker="A", event_ticker="KX-G1", side="no"),))
        skew = _skew(cand, book, marginals, _profile())
        assert skew.pbook_cc < 0
        assert any(r[3] == "pbook_offsetting" for r in skew.pbook_per_game)

    def test_new_game_variance_rebates(self) -> None:
        """A bet on a game the book does not touch at all — the purest
        variance-adder (what the 7/23 slate was missing) — earns a rebate
        even though the directional component is neutral there."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("C", "KX-G3"),))
        skew = _skew(cand, book, marginals, _profile(),
                     conc_profile=_conc(book))
        assert skew.concentration_cc == 0 and skew.offset_cc == 0  # dir-neutral
        # SIZE-INVARIANCE REPAIR (2026-07-27): a game the book's tail does not
        # touch reads share 0 against a positive Herfindahl, i.e. alignment
        # −H — a strict REBATE, and one that cannot decay as we diversify
        # (there is no key COUNT and no wall anywhere in it).
        assert skew.pbook_cc < 0
        assert any(
            r[3] == "pbook_diversifying" for r in skew.pbook_per_game
        )
        assert skew.conc is not None
        assert skew.conc.hhi.score > 0.0      # it DOES lower concentration
        assert skew.conc.applied_cc > 0       # ...and it is quoted tighter
        assert skew.applied_cc > 0

    def test_magnitude_is_independent_of_p_book(self) -> None:
        """SUPERSEDES ``test_magnitude_scales_with_p_book_deficit``
        (operator directive 2026-07-27).

        ``need = 1 − p_book`` was a BOOK-LEVEL, CANDIDATE-BLIND multiplier on
        every row of this axis, and p_book drifts DOWN as the book grows
        (measured 0.5954 → 0.5653 in one session, ×1.0744 on every widen and
        every rebate). That is a book-size term wearing a risk reason code —
        category (B) — so it is gone. The same candidate against the same
        SHAPE now prices identically whatever p_book says."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        needy = _skew(cand, book, marginals, _profile(p_book=0.40))
        healthy = _skew(cand, book, marginals, _profile(p_book=0.90))
        assert needy.pbook_cc == healthy.pbook_cc > 0

    def test_widen_reads_share_against_the_herfindahl_not_the_game_count(
        self,
    ) -> None:
        """SUPERSEDES the wall-onset test (operator directive 2026-07-27).

        The question is neither "is this game an average FRACTION of the book"
        (a COUNT, which decayed as we diversified) nor "how much of the game's
        WALL is spent" (a book-size term). It is "does this game carry MORE
        than the book's dollar-weighted average share" — ``share − Σ share²``,
        a pure ratio. A uniform two-game split is exactly average ⇒ NEUTRAL;
        a 0.9/0.1 split is above average ⇒ WIDEN — and neither reading moves
        when the tail dollars are scaled by 100x."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        uniform = _skew(
            cand, book, marginals,
            _profile(shares={"G1": 0.5, "G2": 0.5}, total_tail_cc=1.0e9),
        )
        assert uniform.pbook_cc == 0

        skewed_big = _skew(
            cand, book, marginals,
            _profile(shares={"G1": 0.9, "G2": 0.1}, total_tail_cc=1.0e9),
        )
        skewed_small = _skew(
            cand, book, marginals,
            _profile(shares={"G1": 0.9, "G2": 0.1}, total_tail_cc=1.0e7),
        )
        assert skewed_big.pbook_cc > 0
        assert skewed_big.pbook_cc == skewed_small.pbook_cc


class TestScaleInvariance:
    """THE OPERATOR'S RULE (2026-07-27): "we shouldn't be widening all of our
    bets just because we hit a $ amount of positions, we only widen on bets
    that concentrate current directions". The P(book) axis reads SHAPE, so
    scaling the whole book changes nothing it says."""

    def test_identical_candidate_1x_vs_3x_book_gets_the_same_component(
        self,
    ) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        base = _skew(
            cand, book, marginals,
            _profile(total_tail_cc=1.0e8, game_budget_cc=1.0e9),
        )
        triple = _skew(
            cand, book, marginals,
            _profile(total_tail_cc=3.0e8, game_budget_cc=1.0e9),
        )
        assert base.pbook_cc == triple.pbook_cc

    def test_no_discontinuity_as_the_book_sweeps(self) -> None:
        """Sweeping the book's tail dollars across three orders of magnitude
        produces EXACTLY one value — no cliff, no step, nothing to cross."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        seen = {
            _skew(
                cand, book, marginals,
                _profile(total_tail_cc=1.0e6 * (1.35**i)),
            ).pbook_cc
            for i in range(40)
        }
        assert len(seen) == 1

    def test_concentrator_widens_and_diversifier_is_strictly_tighter(
        self,
    ) -> None:
        book, marginals = _one_way_book()
        prof = _profile(shares={"G1": 0.9, "G2": 0.1})
        conc = _skew(
            no_position("c", (leg("A", "KX-G1"),)), book, marginals, prof
        )
        div = _skew(
            no_position("d", (leg("C", "KX-G3"),)), book, marginals, prof
        )
        assert conc.pbook_cc > 0 > div.pbook_cc

    def test_rebate_is_book_size_invariant(self) -> None:
        """SUPERSEDES ``test_rebate_reads_the_book_level_hole`` (2026-07-26).

        The original design scaled the diversification REWARD by the book's
        proximity to its cap (``onset_book``), so a deep hole paid more than
        a shallow one. Live measurement killed that intent: at real book
        sizes onset is a few percent, so rebates landed at a MEDIAN OF 0.02c
        against a 1-4c markup ladder — invisible to any taker — while the
        book filled one direction and p_book sat at 0.37 with zero legs live.

        The corrected doctrine: the REWARD is size-invariant (a diversifier
        is not worth less because the book is small) and scales on
        COMPOSITION instead — ``deficit`` (how skewed the mix is) x ``need``
        (1 - p_book). The "deeper hole pays more" intuition survives in the
        honest place: a concentrating book has a LOWER p_book, so ``need``
        rises and the rebate rises with it. Onset stays on the WIDEN branch
        (never tax a small book), pinned by test_widen_onset_is_convex.

        LEVER #5 (2026-07-27): the doctrine is unchanged, the MECHANISM moved.
        The reward is now the AND-BOUND dollar-Herfindahl marginal, which is
        a RATIO of effective loss-event counts and therefore book-size
        invariant by construction — not by a rule we have to remember."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("C", "KX-G3"),))
        centre = _warm_centre()
        shallow = _skew(cand, book, marginals, _profile(total_tail_cc=2.5e8),
                        conc_profile=_conc(book, centre=centre))
        deep = _skew(cand, book, marginals, _profile(total_tail_cc=1.0e9),
                     conc_profile=_conc(book, centre=centre))
        assert shallow.conc is not None and deep.conc is not None
        assert deep.conc.applied_cc == shallow.conc.applied_cc > 0
        # And it is a number a taker can actually see: the measured OLD
        # median |applied_cc| was 20cc against a 200cc margin.
        assert deep.conc.applied_cc >= 25


class TestReviewRegressions:
    """2026-07-25 adversarial-review regressions (15 confirmed findings)."""

    def test_single_game_book_still_rebates_new_game_flow(self) -> None:
        """Review HIGH (n=1 discontinuity): the PUREST one-way shape — tail
        on exactly ONE game — must reward a new-game diversifier, matching
        the epsilon-split book's rebate (the old code paid 0).

        LEVER #5 (2026-07-27): the n==1 discontinuity cannot recur, because
        there is no ``n`` anywhere any more — the rebate is priced by the
        AND-bound dollar-Herfindahl, which never divides by a key count."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("C", "KX-G3"),))
        centre = _warm_centre()
        single = _skew(
            cand, book, marginals, _profile(shares={"G1": 1.0}),
            conc_profile=_conc(book, centre=centre),
        )
        epsilon = _skew(
            cand, book, marginals, _profile(shares={"G1": 0.999, "G2": 0.001}),
            conc_profile=_conc(book, centre=centre),
        )
        assert single.conc is not None and epsilon.conc is not None
        assert single.conc.applied_cc > 0
        assert single.applied_cc > 0
        # Continuous: the single-game rebate == the epsilon-split rebate
        # EXACTLY (the profile's share split is not an input to it at all).
        assert single.conc.applied_cc == epsilon.conc.applied_cc

    def test_hedged_protected_game_earns_no_rebate(self) -> None:
        """Review LOW (hedge erosion): a game whose tail attribution is
        zero/negative (it PROTECTS the book) is not 'underweight' — flow
        there gets no diversification rebate."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("C", "KX-G3"),))
        skew = _skew(
            cand, book, marginals,
            _profile(protected=frozenset({"G3"})),
        )
        assert skew.pbook_cc == 0
        assert any(r[3] == "hedged_protected" for r in skew.pbook_per_game)

    def test_armed_composed_skew_obeys_documented_bound(self) -> None:
        """Review MEDIUM (stale clamp doc), RE-RULED 2026-07-27: arming pbook
        must not expand the documented overall clamp — but that clamp is now
        SYMMETRIC on the concentration pair, because every concentration axis
        carries ONE weight in both directions. An asymmetric truncation here
        would silently re-impose the 4:1 widen bias the axes just removed."""
        book, marginals = _one_way_book()
        armed = SkewParams(enabled=True, pbook_armed=True)
        lo = -(armed.skew_max_tighten_cc + armed.peak_widen_max_cc)
        hi = armed.skew_max_widen_cc + armed.peak_widen_max_cc
        for cand, profile in (
            (  # maximal widen stack: same-way into a full one-way hole
                no_position("c1", (leg("A", "KX-G1"),), contracts=60_000),
                _profile(p_book=0.0, shares={"G1": 1.0}),
            ),
            (  # maximal rebate stack: new-game flow against the same hole
                no_position("c2", (leg("C", "KX-G3"),), contracts=60_000),
                _profile(p_book=0.0, shares={"G1": 1.0}),
            ),
        ):
            skew = _skew(cand, book, marginals, profile, params=armed)
            assert lo <= skew.skew_cc <= hi

    def test_delta_neutral_on_overweight_still_adds_tail_mass(self) -> None:
        """Review LOW, RE-RULED 2026-07-27: a delta-neutral contribution on an
        OVERWEIGHT game is not neutral — the fill still puts premium at risk on
        the game that already dominates the tail, so it concentrates. The
        directional sign only decides whether the alignment is kept or flipped;
        it never zeroes the reading."""
        from combomaker.risk.skew import _pbook_component

        pbook_cc, rows, score = _pbook_component(
            [("G1", 0)],
            PARAMS,
            _profile(shares={"G1": 0.9, "G2": 0.1}),
            GEN,
        )
        assert pbook_cc > 0 and score is not None and score > 0.0
        assert rows[0][3] == "pbook_concentrating"

    def test_profile_builder_normalizes_by_positive_mass(self) -> None:
        """Review HIGH (shares don't sum to 1): negative attribution entries
        are excluded from numerator AND denominator; they publish as
        PROTECTED games instead."""
        from types import SimpleNamespace

        from combomaker.rfq.lifecycle import _pbook_profile_from_snapshot

        snap = SimpleNamespace(
            input_generation=GEN,
            p_profit=0.55,
            per_game_tail_cc=(
                SimpleNamespace(key="G1", loss_cc=600.0),
                SimpleNamespace(key="G2", loss_cc=400.0),
                SimpleNamespace(key="G3", loss_cc=-250.0),  # hedged
            ),
        )
        profile = _pbook_profile_from_snapshot(snap, game_budget_cc=5_000.0)
        assert profile.tail_share_by_game == {"G1": 0.6, "G2": 0.4}
        assert sum(profile.tail_share_by_game.values()) == 1.0
        assert profile.total_tail_cc == 1_000.0
        assert profile.protected_games == frozenset({"G3"})
        assert profile.game_budget_cc == 5_000.0


class TestFailSafe:
    def test_absent_profile_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        assert _skew(cand, book, marginals, None).pbook_cc == 0

    def test_stale_generation_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(cand, book, marginals, _profile(gen=GEN - 1))
        assert skew.pbook_cc == 0
        assert any(r[3] == "stale_profile" for r in skew.pbook_per_game)

    def test_disabled_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        off = SkewParams(enabled=True, pbook_enabled=False)
        assert _skew(cand, book, marginals, _profile(), params=off).pbook_cc == 0

    def test_empty_tail_is_zero(self) -> None:
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(cand, book, marginals, _profile(shares={}))
        assert skew.pbook_cc == 0

    def test_component_bounded_by_documented_caps(self) -> None:
        """The clamp: never beyond the documented peak hard caps."""
        book, marginals = _one_way_book()
        cand = no_position("cand", (leg("A", "KX-G1"),))
        skew = _skew(
            cand, book, marginals,
            _profile(p_book=0.0, shares={"G1": 1.0}),
        )
        assert -PARAMS.peak_tighten_max_cc <= skew.pbook_cc <= PARAMS.peak_widen_max_cc
