"""THE OPERATOR'S RULE, as executable properties (directive 2026-07-27).

    "we shouldn't be widening all of our bets just because we hit a $ amount of
     positions, we only widen on bets that concentrate current directions"

Measured defect this pins closed: between 10:20 ET (det_max $514.45) and 13:00
ET (det_max $596.17) the median ``applied_cc`` moved -14.0 -> -26.0 (in the
corrected frame, MORE NEGATIVE = WIDER) on 620,476 live skew events, and 83% of
that drift came from terms whose numerator was the BOOK'S OWN dollars against a
denominator that was either a hand-set constant, a 1/n key count, or an enforced
wall. None of those read the candidate.

The properties below are stated on the CONCENTRATION axes (peak, P(book) tail
share, leg family x side, leg entity x side, and the AND-bound loss-event
Herfindahl), which are the terms the directive governs. Every one of them is now
a pure RATIO - a dollar SHARE against that axis's own dollar-weighted average -
so scaling the whole book leaves the reading numerically identical.

The DIRECTIONAL term (``risk/skew.py`` per-game ``w_conc x d_e x util**gamma``)
is deliberately untouched: it is multiplied by the candidate's OWN per-game delta
and only fires when the candidate ADDS to the game's net direction, i.e. it is
exactly "the candidate's own effect on directional exposure", which the rule
explicitly permits to widen. ``test_non_concentrating_flow_is_never_widened_by_
book_size`` pins the half of the rule that governs it: flow that does NOT
concentrate us never gets wider as the book grows.
"""

from __future__ import annotations

import dataclasses

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.concentration_steer import (
    SteerCenter,
    build_loss_event_book,
    share_alignment,
)
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.skew import (
    ConcentrationProfile,
    InventorySkew,
    LegAxisProfile,
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

# Loose enough that the DIRECTIONAL util ramp is not the thing under test.
LIMITS = SkewLimits(
    max_event_delta_contracts=1e9,
    max_event_worst_case_loss_dollars=1e9,
    max_event_gross_notional_dollars=1e12,
)
GEN = 11
# EVERYTHING ARMED, INCLUDING THE LEVER-#5 STEER. ``conc_armed`` ships FALSE
# (2026-07-27 review B3: shadow first — tests/test_conc_arming.py pins that the
# unarmed composition is byte-identical to the pre-Lever-#5 one). It is armed
# HERE on purpose: these are the properties that must hold on the day the
# operator flips it, so they are proved against the composition that flip
# produces, not against the shadow one.
ARMED = SkewParams(
    enabled=True, pbook_armed=True, leg_axis_armed=True, conc_armed=True
)

MARGINALS = {
    "KXMLBKS-A-5": 0.5,
    "KXMLBKS-B-5": 0.5,
    "KXMLBKS-C-5": 0.5,
    "KXMLBGAME-A": 0.5,
    "KXMLBGAME-B": 0.5,
    "KXMLBGAME-D": 0.5,
}


def provider(mapping: dict[str, float]):
    return lambda ticker: mapping.get(ticker)


def leg(market: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=market, event_ticker=event, side=side)


def position(
    pid: str, legs: tuple[LegRef, ...], *, contracts: int, entry: int = 5_000
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker="COMBO",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(entry),
        legs=legs,
    )


# ---------------------------------------------------------------------------
# A book of a fixed COMPOSITION, scalable by one factor.
# ---------------------------------------------------------------------------

_SHAPE: tuple[tuple[str, tuple[LegRef, ...], int], ...] = (
    ("h1", (leg("KXMLBKS-A-5", "KXMLB-G1"),), 40_000),
    ("h2", (leg("KXMLBKS-B-5", "KXMLB-G2"),), 25_000),
    ("h3", (leg("KXMLBGAME-A", "KXMLB-G1"),), 15_000),
)


def book_at(scale: float) -> tuple[ExposureBook, dict[str, float]]:
    """The SAME composition, every committed ticket scaled by ``scale``."""
    book = ExposureBook(CONVENTIONS)
    for pid, legs, contracts in _SHAPE:
        book.add_position(
            position(pid, legs, contracts=int(round(contracts * scale)))
        )
    return book, dict(MARGINALS)


def profiles_at(book: ExposureBook, snap) -> dict:
    """Every cached steering profile, built from THAT book exactly the way the
    lifecycle builds them (shares off the snapshot, Herfindahl off the shares)."""
    fam = {k: float(v) for k, v in snap.committed_loss_by_family_cc.items()}
    ent = {k: float(v) for k, v in snap.committed_loss_by_entity_cc.items()}
    game = {k: float(v) for k, v in snap.worst_case_loss_by_game_cc.items()}
    total_fam = sum(fam.values())
    total_ent = sum(ent.values())
    total_game = sum(game.values())
    return {
        "pbook_profile": PBookProfile(
            input_generation=GEN,
            p_book=0.56,
            tail_share_by_game=(
                {k: v / total_game for k, v in game.items()}
                if total_game > 0
                else {}
            ),
            total_tail_cc=total_game,
            # The enforced wall does NOT scale with the book - that asymmetry
            # is precisely what used to leak book size into price.
            game_budget_cc=2_811_801.0,
        ),
        "pbook_book_generation": GEN,
        "leg_axis_profile": LegAxisProfile(
            shares_by_family=(
                {k: v / total_fam for k, v in fam.items()} if total_fam else {}
            ),
            total_family_cc=total_fam,
            shares_by_entity=(
                {k: v / total_ent for k, v in ent.items()} if total_ent else {}
            ),
            total_entity_cc=total_ent,
            family_budget_cc=2_811_801.0,
            entity_budget_cc=1_054_425.0,
            p_book=0.56,
        ),
        "conc_profile": ConcentrationProfile(
            loss_events=build_loss_event_book(
                (ticket_bucket(p.legs), float(p.max_loss_cc))
                for p in book.positions.values()
            ),
            game_dollars_cc=game,
            game_wall_cc=2_811_801.0,
            family_dollars_cc=fam,
            family_wall_cc=2_811_801.0,
            entity_dollars_cc=ent,
            entity_wall_cc=1_054_425.0,
            fill_elasticity_per_cent=0.22,
            centre=SteerCenter(mean=0.0, var=0.04, n=600, half_life=256.0),
        ),
    }


def skew_at(
    candidate: OpenPosition,
    scale: float,
    *,
    params: SkewParams = ARMED,
    margin_cc: int = 200,
    tick_cc: int = 10,
) -> InventorySkew:
    book, marginals = book_at(scale)
    snap = book.snapshot(provider(marginals), mass_acceptance=False)
    return compute_inventory_skew(
        candidate,
        snap,
        provider(marginals),
        CONVENTIONS,
        LIMITS,
        params,
        margin_cc=margin_cc,
        tick_cc=tick_cc,
        observe_centre=False,   # a frozen centre, so the comparison is exact
        **profiles_at(book, snap),
    )


def concentration_cc(skew: InventorySkew) -> int:
    """The composed CONCENTRATION steer only (the terms the rule governs)."""
    return skew.conc.skew_cc if skew.conc is not None else 0


# A candidate that lands squarely on the book's biggest family+entity (another
# rung of the SAME pitcher's K-ladder in the SAME game).
CONCENTRATOR = position(
    "conc", (leg("KXMLBKS-A-5", "KXMLB-G1"),), contracts=2_000
)
# A candidate on a family/entity/game the book does not touch at all.
DIVERSIFIER = position(
    "div", (leg("KXMLBGAME-D", "KXMLB-G4"),), contracts=2_000
)
# The OTHER side of the loaded K-ladder: a different family key, same game.
OTHER_SIDE = position(
    "other", (leg("KXMLBKS-A-5", "KXMLB-G1", side="no"),), contracts=2_000
)


class TestTheMeasureItself:
    def test_share_alignment_is_dollar_weighted_mean_zero(self) -> None:
        """The structural reason this cannot be a blanket markup change: the
        DOLLAR-WEIGHTED mean alignment over any book is EXACTLY zero, so the
        measure can only reallocate width between concentrating and
        diversifying flow. Markups are frozen; this respects that by
        construction, not by calibration."""
        for shares in (
            [0.9, 0.1],
            [0.4, 0.35, 0.25],
            [0.25] * 4,
            [0.5, 0.2, 0.2, 0.1],
        ):
            h = sum(s * s for s in shares)
            assert abs(sum(s * (s - h) for s in shares)) < 1e-12

    def test_share_alignment_is_strictly_increasing_and_bounded(self) -> None:
        h = 0.34
        prev = -2.0
        for share in (0.0, 0.05, 0.2, 0.34, 0.5, 0.8, 1.0):
            a = share_alignment(share, h)
            assert a > prev
            prev = a
            assert -1.0 <= a <= 1.0
        assert share_alignment(h, h) == 0.0   # exactly average = neutral
        assert share_alignment(0.0, h) < 0.0  # a brand-new key always rebates
        # UNKNOWN never widens: an axis with no committed dollars reads 0.
        assert share_alignment(0.9, 0.0) == 0.0

    def test_share_alignment_has_no_book_size_input(self) -> None:
        """It takes a share and a Herfindahl. There is no dollar, no wall and
        no key count anywhere in its signature - a book-size term cannot enter
        it even by accident."""
        import inspect

        assert list(inspect.signature(share_alignment).parameters) == [
            "share",
            "hhi",
        ]


class TestOneXVersusThreeX:
    """THE PROPERTY THAT PINS THE RULE: identical candidate, identical book
    COMPOSITION, book scaled 3x => the SAME concentration steer."""

    def test_concentrator_gets_the_same_steer_at_1x_and_3x(self) -> None:
        one = skew_at(CONCENTRATOR, 1.0)
        three = skew_at(CONCENTRATOR, 3.0)
        assert concentration_cc(one) == concentration_cc(three)
        assert one.peak_cc == three.peak_cc
        assert one.pbook_cc == three.pbook_cc
        assert one.family_cc == three.family_cc
        assert one.entity_cc == three.entity_cc

    def test_diversifier_gets_the_same_steer_at_1x_and_3x(self) -> None:
        one = skew_at(DIVERSIFIER, 1.0)
        three = skew_at(DIVERSIFIER, 3.0)
        assert concentration_cc(one) == concentration_cc(three)
        assert one.pbook_cc == three.pbook_cc
        assert one.family_cc == three.family_cc
        assert one.entity_cc == three.entity_cc

    def test_full_applied_cc_is_identical_at_1x_and_3x(self) -> None:
        """End to end, including the directional term: on flow that does not
        concentrate a game's DIRECTION the whole quote-facing number is
        identical at 1x and 3x."""
        assert skew_at(DIVERSIFIER, 1.0).applied_cc == skew_at(
            DIVERSIFIER, 3.0
        ).applied_cc

    def test_every_component_is_identical_across_two_decades_of_book_size(
        self,
    ) -> None:
        for cand in (CONCENTRATOR, DIVERSIFIER, OTHER_SIDE):
            readings = {
                (
                    concentration_cc(skew_at(cand, s)),
                    skew_at(cand, s).pbook_cc,
                    skew_at(cand, s).family_cc,
                    skew_at(cand, s).entity_cc,
                )
                for s in (0.05, 0.2, 1.0, 3.0, 12.0, 50.0)
            }
            assert len(readings) == 1, (cand.position_id, readings)


class TestOrdering:
    """Category (A) intact, and the standing invariant: reducing
    dollar-weighted concentration is quoted STRICTLY tighter."""

    def test_a_concentrating_candidate_is_still_widened(self) -> None:
        s = skew_at(CONCENTRATOR, 1.0)
        assert s.family_cc > 0 and s.entity_cc > 0
        assert concentration_cc(s) > 0          # classifier: widen
        assert s.applied_cc < 0                 # pricer frame: dearer combo

    def test_a_diversifying_candidate_is_strictly_tighter(self) -> None:
        conc = skew_at(CONCENTRATOR, 1.0)
        div = skew_at(DIVERSIFIER, 1.0)
        assert div.conc is not None and conc.conc is not None
        assert div.conc.hhi.score > 0.0 > conc.conc.hhi.score
        assert concentration_cc(div) < concentration_cc(conc)
        assert div.applied_cc > conc.applied_cc     # strictly tighter quote

    def test_the_other_side_of_the_ladder_is_rewarded(self) -> None:
        """"Reward the other side of the K-ladder": ``KXMLBKS-A:no`` is a
        DIFFERENT family key carrying zero share, so it reads -H."""
        other = skew_at(OTHER_SIDE, 1.0)
        conc = skew_at(CONCENTRATOR, 1.0)
        assert other.family_cc < 0 < conc.family_cc
        assert other.applied_cc > conc.applied_cc

    def test_ordering_survives_at_every_book_size(self) -> None:
        for s in (0.05, 1.0, 3.0, 50.0):
            assert skew_at(DIVERSIFIER, s).applied_cc > skew_at(
                CONCENTRATOR, s
            ).applied_cc


class TestSmoothness:
    def test_no_discontinuity_as_the_book_size_sweeps(self) -> None:
        """A dense geometric sweep across ~3.5 orders of magnitude: the
        concentration steer takes exactly ONE value. There is no size at which
        anything steps, because size is not an input."""
        for cand in (CONCENTRATOR, DIVERSIFIER):
            values = {
                concentration_cc(skew_at(cand, 0.02 * (1.25**i)))
                for i in range(40)
            }
            assert len(values) == 1, (cand.position_id, sorted(values))

    def test_response_to_COMPOSITION_is_continuous(self) -> None:
        """What the steer DOES respond to - shape - it responds to smoothly:
        walking one key's share up in 40 steps moves the alignment monotonically
        and by no more than one step's worth at a time."""
        prev = None
        for i in range(41):
            share = i / 40.0
            h = share * share + (1.0 - share) ** 2
            a = share_alignment(share, h)
            if prev is not None:
                assert abs(a - prev) < 0.2
            prev = a


class TestBookSizeNeverWidensNonConcentratingFlow:
    """The other half of the rule, stated on the FULL classifier (directional
    term included): flow that does not concentrate us must never become WIDER
    because the book got bigger."""

    def test_non_concentrating_flow_is_never_widened_by_book_size(self) -> None:
        base = skew_at(DIVERSIFIER, 1.0).applied_cc
        for s in (1.5, 2.0, 3.0, 8.0, 25.0, 100.0):
            # applied_cc: MORE NEGATIVE = WIDER. It must never fall.
            assert skew_at(DIVERSIFIER, s).applied_cc >= base

    def test_p_book_no_longer_scales_anything(self) -> None:
        """``need = 1 - p_book`` was a book-level, candidate-blind multiplier
        on every leg-axis and P(book) row, and p_book drifts DOWN as the book
        grows (0.5954 -> 0.5653 measured in one session, x1.0744 on every row).
        Category (B): removed. The same candidate against the same shape now
        prices identically at any p_book."""
        book, marginals = book_at(1.0)
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        readings = set()
        for p in (0.05, 0.4, 0.5653, 0.5954, 0.95):
            profiles = profiles_at(book, snap)
            profiles["pbook_profile"] = dataclasses.replace(
                profiles["pbook_profile"], p_book=p
            )
            profiles["leg_axis_profile"] = dataclasses.replace(
                profiles["leg_axis_profile"], p_book=p
            )
            sk = compute_inventory_skew(
                CONCENTRATOR,
                snap,
                provider(marginals),
                CONVENTIONS,
                LIMITS,
                ARMED,
                margin_cc=200,
                tick_cc=10,
                observe_centre=False,
                **profiles,
            )
            readings.add(
                (sk.pbook_cc, sk.family_cc, sk.entity_cc, sk.applied_cc)
            )
        assert len(readings) == 1

    def test_a_new_game_appearing_cannot_widen_existing_flow(self) -> None:
        """The measured live shape (TORWSH appearing at 11:44 ADDED widen for
        every candidate touching any other game, because the peak denominator
        was static). Now a new game DILUTES every other game's share, so it can
        only ever steer the others LESS."""
        book, marginals = book_at(1.0)
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        profiles = profiles_at(book, snap)
        before = compute_inventory_skew(
            CONCENTRATOR, snap, provider(marginals), CONVENTIONS, LIMITS,
            ARMED, margin_cc=200, tick_cc=10, observe_centre=False, **profiles,
        )
        # A brand-new game arrives carrying its own tail mass.
        pb = profiles["pbook_profile"]
        total = pb.total_tail_cc
        grown = {k: v * total for k, v in pb.tail_share_by_game.items()}
        grown["KXMLB-G9"] = total * 0.5
        new_total = sum(grown.values())
        profiles["pbook_profile"] = dataclasses.replace(
            pb,
            tail_share_by_game={k: v / new_total for k, v in grown.items()},
            total_tail_cc=new_total,
        )
        after = compute_inventory_skew(
            CONCENTRATOR, snap, provider(marginals), CONVENTIONS, LIMITS,
            ARMED, margin_cc=200, tick_cc=10, observe_centre=False, **profiles,
        )
        assert after.pbook_cc <= before.pbook_cc
        assert after.applied_cc >= before.applied_cc   # never wider


class TestBudgetNeutrality:
    """Markups are FROZEN (operator, binding). The steer must reallocate, never
    add width."""

    def test_the_centred_steer_averages_to_about_zero_over_mixed_flow(
        self,
    ) -> None:
        book, marginals = book_at(1.0)
        snap = book.snapshot(provider(marginals), mass_acceptance=False)
        centre = SteerCenter(half_life=64.0)
        flow = [CONCENTRATOR, DIVERSIFIER, OTHER_SIDE]
        applied: list[int] = []
        for i in range(600):
            profiles = profiles_at(book, snap)
            profiles["conc_profile"] = dataclasses.replace(
                profiles["conc_profile"], centre=centre
            )
            sk = compute_inventory_skew(
                flow[i % len(flow)], snap, provider(marginals), CONVENTIONS,
                LIMITS, ARMED, margin_cc=200, tick_cc=10, observe_centre=True,
                **profiles,
            )
            if sk.conc is not None:
                applied.append(sk.conc.applied_cc)
        tail = applied[-300:]
        mean = sum(tail) / len(tail)
        # applied_cc: POSITIVE raises no_bid => cheaper combo => TIGHTER.
        # Centred on measured flow, the concentration steer's own mean can
        # never sit on the WIDENING side: it moves width BETWEEN candidates,
        # it does not add any. (Exact zero is unattainable on a discrete tick
        # lattice with a discrete flow mix; the binding requirement is the
        # sign, and the tape replay measures the live mean.)
        assert mean >= 0.0, mean
        assert max(tail) > 0 > min(tail)   # ...and it is still discriminating

    def test_the_output_loop_holds_the_mean_at_zero_on_skewed_flow(self) -> None:
        """The mechanism, isolated. The live score distribution is RIGHT-SKEWED
        (measured on the 41,056-event tape replay: mean -0.0422 vs median
        -0.0473), and a hard clamp on the standardised score truncates one tail,
        leaving a systematic WIDEN on frozen markups (measured: -0.141 mean on a
        signal whose mean was supposed to be 0 => 15cc of average width nobody
        asked for). The ``tanh`` bound plus the OUTPUT-space integral term drive
        the emitted mean to ~0 on exactly that shape - with no constant to
        tune, at ANY half-range."""
        import random

        from combomaker.risk.concentration_steer import tick_ladder

        rng = random.Random(7)
        # A right-skewed score stream: many mild concentrators, a few strong
        # diversifiers - the live shape.
        stream = [
            max(-1.0, min(1.0, -0.05 + rng.expovariate(12.0)))
            for _ in range(6_000)
        ]
        for half in (100, 200, 227, 300):
            centre = SteerCenter(half_life=256.0)
            out = []
            for s in stream:
                centre.observe(s)
                c = centre.squash(s)
                centre.observe_output(c)
                out.append(tick_ladder(c, half, 10))
            tail = out[3_000:]
            mean = sum(tail) / len(tail)
            # Well inside one tick, at every half-range: the loop found the
            # zero, it was not tuned to it.
            assert abs(mean) < 10, (half, mean)
            assert min(tail) < 0 < max(tail)

    def test_squash_is_strictly_increasing_so_ordering_survives(self) -> None:
        c = SteerCenter(mean=0.1, var=0.04, n=500)
        c.bias = 0.37
        prev = -2.0
        for s in (-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0):
            v = c.squash(s)
            assert v > prev
            prev = v
            assert -1.0 < v < 1.0
