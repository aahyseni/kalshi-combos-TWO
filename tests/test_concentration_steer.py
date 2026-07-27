"""LEVER #5 — the concentration steer made ECONOMICALLY REAL (operator
directive 2026-07-27).

What is pinned here, one class per requirement:

* **AND-BINDING** — a 3-leg ticket across 3 DIFFERENT matches is ONE loss
  event (effective events 1.00), and splitting it into 3 tickets reads 2.99.
  The measured live pair, reproduced exactly.
* **DOLLARS, NEVER COUNTS** — the deficit is dollars against each axis's OWN
  ENFORCED WALL. Adding a brand-new key does NOT decay an existing key's
  reading (the measured 42%-in-one-session decay of the 1/n_keys basis).
* **SYMMETRY** — one half-range: the widen ceiling and the rebate ceiling are
  the same number. No 4:1.
* **SCALE FROM MEASURED STATE** — the half-range is the min of the measured
  value dispersion, the measured fill-elasticity validity horizon, and the
  LIVE margin. Never a constant.
* **SUB-TICK ANNIHILATION** — the steer is always a whole number of grid
  ticks, so ``snap_bid_down`` reproduces it instead of erasing it (32.25% of
  live events were annihilated).
* **THE OPERATOR INVARIANT, AS A PROPERTY** — for two otherwise-identical
  candidates, the one that LOWERS dollar-weighted book concentration gets a
  STRICTLY tighter quote, asserted at the CLASSIFIER, PRE-SNAP and POST-SNAP,
  with a ``NoQuote`` ranked as infinite width.
* **BUDGET NEUTRALITY** — markups are FIXED (operator, binding), so the mean
  applied_cc must not go negative: the steer reallocates, it never widens.
* **delta_p_book NEVER REACHES PRICE** — it is not an input anywhere.
"""

from __future__ import annotations

import math
import random
from fractions import Fraction

import numpy as np
import pytest

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.grid import GridRange, PriceGrid
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.quote import construct_quote
from combomaker.risk.concentration_steer import (
    CC_PER_CENT,
    ConcentrationSteer,
    CrnBookCache,
    HhiMarginal,
    SteerCenter,
    SteerInputs,
    SteerScale,
    assert_diversifier_tighter,
    build_loss_event_book,
    compute_concentration_steer,
    elasticity_half_cc,
    event_bucket,
    quote_rank_cc,
    tick_ladder,
    wall_load,
)
from combomaker.risk.exposure import LegRef, OpenPosition
from combomaker.risk.skew import ticket_bucket

# The MEASURED, CMH-stratified fill-rate elasticity: 22% of fills lost per cent.
FILL_ELASTICITY = 0.22
TICK_CC = 10  # the live combo grid step the 32.25% annihilation was measured on

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def leg(market: str, event: str, side: str = "yes") -> LegRef:
    return LegRef(market_ticker=market, event_ticker=event, side=side)


def ticket(pid: str, legs: tuple[LegRef, ...], *, contracts: int = 10_000,
           price_cc: int = 5_000) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"C-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(price_cc),
        legs=legs,
    )


# Real live ticker shapes (source-of-truth rule: never invent ticker grammar).
G1_A = "KXMLBGAME-26JUL251610SDMIA-SD"
G2_A = "KXMLBGAME-26JUL251905PITATL-PIT"
G3_A = "KXMLBGAME-26JUL251805NYYPHI-PHI"
KS_CEASE_5 = "KXMLBKS-26JUL251610SDMIA-SDDCEASE44-5"
KS_CEASE_6 = "KXMLBKS-26JUL251610SDMIA-SDDCEASE44-6"
KS_SKENES_5 = "KXMLBKS-26JUL251905PITATL-PITPSKENES30-5"
EV1 = "KXMLBGAME-26JUL251610SDMIA"
EV2 = "KXMLBGAME-26JUL251905PITATL"
EV3 = "KXMLBGAME-26JUL251805NYYPHI"


class TestAndBinding:
    """A ticket is ONE loss event, not one per leg (the measured 1.00 / 2.99)."""

    def test_three_match_ticket_is_one_loss_event(self) -> None:
        whole = ticket(
            "t", (leg(G1_A, EV1), leg(G2_A, EV2), leg(G3_A, EV3))
        )
        book = build_loss_event_book(
            [(ticket_bucket(whole.legs), float(whole.max_loss_cc))]
        )
        assert book.effective_events == pytest.approx(1.00, abs=0.01)

    def test_split_into_three_tickets_reads_about_three(self) -> None:
        # The SAME dollars, held as three separate tickets, with the same
        # slight size inequality the live book had.
        parts = [
            ticket("a", (leg(G1_A, EV1),), contracts=10_000),
            ticket("b", (leg(G2_A, EV2),), contracts=9_800),
            ticket("c", (leg(G3_A, EV3),), contracts=10_200),
        ]
        book = build_loss_event_book(
            [(ticket_bucket(p.legs), float(p.max_loss_cc)) for p in parts]
        )
        assert book.effective_events == pytest.approx(2.99, abs=0.02)

    def test_leg_wise_scoring_would_have_read_three_rebates(self) -> None:
        """The BUG this replaces: the same 3-match ticket, scored leg-wise,
        looks like three separate diversifying events."""
        whole = ticket(
            "t", (leg(G1_A, EV1), leg(G2_A, EV2), leg(G3_A, EV3))
        )
        leg_wise = build_loss_event_book(
            [((k,), float(whole.max_loss_cc) / 3.0)
             for k in event_bucket(whole.legs, lambda x: x.event_ticker or "")]
        )
        assert leg_wise.effective_events == pytest.approx(3.0, abs=0.01)
        ticket_wise = build_loss_event_book(
            [(ticket_bucket(whole.legs), float(whole.max_loss_cc))]
        )
        assert ticket_wise.effective_events < leg_wise.effective_events

    def test_same_bucket_tickets_merge_into_one_event(self) -> None:
        a = ticket("a", (leg(KS_CEASE_5, EV1),))
        b = ticket("b", (leg(KS_CEASE_6, EV1),))  # another rung, same arm
        assert ticket_bucket(a.legs) == ticket_bucket(b.legs)
        book = build_loss_event_book(
            [(ticket_bucket(a.legs), float(a.max_loss_cc)),
             (ticket_bucket(b.legs), float(b.max_loss_cc))]
        )
        assert book.effective_events == pytest.approx(1.0)


class TestDollarsNeverCounts:
    """The 1/n_keys COUNT basis is gone: nothing decays because we succeeded."""

    def test_wall_load_is_dollars_over_the_axis_own_wall(self) -> None:
        dollars = {"KXMLBKS:yes": 4.0e5}
        assert wall_load(dollars, ["KXMLBKS:yes"], 1.0e6) == pytest.approx(0.4)
        # Half the wall, half the load — a pure dollar reading.
        assert wall_load(dollars, ["KXMLBKS:yes"], 8.0e5) == pytest.approx(0.5)

    def test_adding_a_new_key_does_not_decay_an_existing_reading(self) -> None:
        """The measured failure of the count basis: the absent-key rebate fell
        42% (0.0250 -> 0.0145) in ONE session BECAUSE we diversified. A wall
        does not move when a key appears, so the reading is unchanged."""
        before = wall_load({"A": 5.0e5}, ["A"], 1.0e6)
        after = wall_load({"A": 5.0e5, "B": 1.0, "C": 1.0, "D": 1.0}, ["A"], 1.0e6)
        assert before == after == pytest.approx(0.5)

    def test_unknown_wall_never_widens(self) -> None:
        assert wall_load({"A": 9.9e9}, ["A"], 0.0) == 0.0

    def test_zero_standard_error(self) -> None:
        """The Herfindahl marginal has NO sampling in it: the same inputs give
        bit-identical answers, forever."""
        book = build_loss_event_book([(("G1",), 1.0e5), (("G2",), 2.0e5)])
        vals = {book.marginal(("G3",), 5.0e4).relative for _ in range(50)}
        assert len(vals) == 1


class TestScaleFromMeasuredState:
    def test_elasticity_horizon_is_derived_from_the_measurement(self) -> None:
        # e = 0.22 fills lost per cent => the linear model's own half-life is
        # 1/(2e) = 2.27 cents = 227cc. Nothing hand-set.
        assert elasticity_half_cc(FILL_ELASTICITY) == int(
            CC_PER_CENT / (2.0 * FILL_ELASTICITY)
        )
        assert elasticity_half_cc(FILL_ELASTICITY) == 227
        assert elasticity_half_cc(0.0) == 0  # no measurement, no steer

    def test_symmetric_no_four_to_one(self) -> None:
        """ONE half-range: the widen ceiling IS the rebate ceiling."""
        s = SteerScale.derive(
            value_sd_cc=57.0, fill_elasticity_per_cent=FILL_ELASTICITY,
            margin_cc=200,
        )
        assert s.half_cc == 57 and s.binding == "value"
        assert tick_ladder(1.0, s.half_cc, TICK_CC) == -tick_ladder(
            -1.0, s.half_cc, TICK_CC
        )

    def test_live_margin_binds_and_never_widens_the_quote(self) -> None:
        """Markups are FIXED: the steer may only reallocate inside the width
        that already exists."""
        s = SteerScale.derive(
            value_sd_cc=500.0, fill_elasticity_per_cent=FILL_ELASTICITY,
            margin_cc=40,
        )
        assert s.half_cc == 40 and s.binding == "margin"

    def test_clamps_are_not_a_constant(self) -> None:
        thin = SteerScale.derive(
            value_sd_cc=57.0, fill_elasticity_per_cent=FILL_ELASTICITY,
            margin_cc=30,
        )
        fat = SteerScale.derive(
            value_sd_cc=57.0, fill_elasticity_per_cent=FILL_ELASTICITY,
            margin_cc=400,
        )
        assert thin.half_cc != fat.half_cc


class TestSubTickAnnihilation:
    def test_every_steer_is_a_whole_number_of_ticks(self) -> None:
        rng = random.Random(7)
        for _ in range(2000):
            score = rng.uniform(-1.0, 1.0)
            out = tick_ladder(score, 57, TICK_CC)
            assert out % TICK_CC == 0

    def test_a_tiny_score_still_gets_one_whole_tick(self) -> None:
        """32.25% of the old steer fell below one tick and was ERASED by
        snap_bid_down. The minimum rung makes that impossible."""
        assert tick_ladder(1e-9, 57, TICK_CC) == TICK_CC
        assert tick_ladder(-1e-9, 57, TICK_CC) == -TICK_CC

    def test_the_tick_steer_survives_snap_bid_down_exactly(self) -> None:
        grid = PriceGrid(
            ranges=(GridRange(start_cc=0, end_cc=10_000, step_cc=TICK_CC),)
        )
        for raw in (3_333, 4_017, 5_000, 6_789):
            base = grid.snap_bid_down(CentiCents(raw))
            steer = tick_ladder(0.9, 57, TICK_CC)
            moved = grid.snap_bid_down(CentiCents(raw + steer))
            assert base is not None and moved is not None
            assert int(moved) - int(base) == steer  # NOT annihilated

    def test_half_range_thinner_than_one_tick_says_so(self) -> None:
        assert tick_ladder(1.0, 7, TICK_CC) == 0


class TestCovariancePrice:
    """The price is Cov(candidate payoff, pre-existing book P&L) off the CRN
    cache we already pay for — NOT delta_p_book."""

    def _cache(self, corr_sign: float, n: int = 4096) -> CrnBookCache:
        rng = np.random.default_rng(11)
        u = rng.random(n)
        hit = (u < 0.4).astype(np.float64)
        # book P&L moves WITH the leg when corr_sign > 0.
        noise = rng.normal(0.0, 50_000.0, n)
        book = corr_sign * 200_000.0 * hit + noise
        return CrnBookCache(
            input_generation=1,
            col_by_ticker={KS_CEASE_5: 0},
            leg_values=hit.reshape(-1, 1),
            book_pnl=book,
            bankroll_cc=21_800_000.0,
            value_sd_cc=57.0,
        )

    def test_a_ticket_that_pays_when_the_book_wins_rebates(self) -> None:
        v = self._cache(+1.0).value_cc_per_contract([leg(KS_CEASE_5, EV1)])
        assert v is not None and v > 0.0

    def test_a_ticket_that_doubles_down_widens(self) -> None:
        v = self._cache(-1.0).value_cc_per_contract([leg(KS_CEASE_5, EV1)])
        assert v is not None and v < 0.0

    def test_magnitude_lands_in_the_measured_range(self) -> None:
        """Measured live: an EV-controlled spread of 1.1333 c/contract. The
        derived log-utility term must land on that order, not 100x off."""
        v = self._cache(+1.0).value_cc_per_contract([leg(KS_CEASE_5, EV1)])
        assert v is not None
        assert 1.0 <= abs(v) <= 400.0  # 0.01c .. 4c per contract

    def test_unknown_leg_is_neutral_never_a_guess(self) -> None:
        assert self._cache(+1.0).value_cc_per_contract(
            [leg("KXMLBKS-UNSEEN-XX-1", EV1)]
        ) is None

    def test_it_is_a_mean_of_a_product_so_every_draw_contributes(self) -> None:
        """Contrast with delta_p_book, which is an indicator difference: 42.2%
        of candidates were unresolvable at 3 sigma at n=20k."""
        big = self._cache(+1.0, n=20_000).value_cc_per_contract(
            [leg(KS_CEASE_5, EV1)]
        )
        small = self._cache(+1.0, n=2_000).value_cc_per_contract(
            [leg(KS_CEASE_5, EV1)]
        )
        assert big is not None and small is not None
        assert math.copysign(1, big) == math.copysign(1, small)

    def test_delta_p_book_is_not_an_input_anywhere(self) -> None:
        import inspect

        from combomaker.risk import concentration_steer as mod

        src = inspect.getsource(mod)
        # It appears ONLY in the module docstring, explaining why it is banned.
        body = src.split('"""', 2)[2]
        assert "delta_p_book" not in body
        assert "p_book" not in body


def _warm_centre(sd: float = 0.12) -> SteerCenter:
    """A centre warmed on real-shaped flow, as the live one always is.

    A COLD centre has no measured dispersion, so it cannot standardise — the
    steer then runs on the raw deviation (fail-safe: never divide by a number
    we have not seen). Live, the centre is warm within a minute of quoting."""
    c = SteerCenter(half_life=128.0)
    rng = random.Random(19)
    for _ in range(600):
        c.observe(rng.gauss(0.0, sd))
    return c


def _steer(
    *,
    bucket: tuple[str, ...],
    book,
    premium_cc: float = 500_000.0,
    margin_cc: int = 200,
    crn: CrnBookCache | None = None,
    centre: SteerCenter | None = None,
    walls: dict | None = None,
) -> ConcentrationSteer:
    return compute_concentration_steer(
        SteerInputs(
            loss_events=book,
            candidate_bucket=bucket,
            candidate_premium_cc=premium_cc,
            walls_by_axis=walls or {},
            margin_cc=margin_cc,
            tick_cc=TICK_CC,
            fill_elasticity_per_cent=FILL_ELASTICITY,
            crn=crn,
        ),
        centre if centre is not None else SteerCenter(),
        observe=False,
    )


class TestOperatorInvariantProperty:
    """THE requirement: for two otherwise-identical candidates, the one that
    LOWERS dollar-weighted book concentration gets a STRICTLY tighter quote —
    at the classifier, pre-snap, and post-snap."""

    def _book(self):
        return build_loss_event_book(
            [(("KXMLBGAME:SDMIA", "KXMLBGAME:yes"), 3.0e6),
             (("KXMLBGAME:PITATL", "KXMLBGAME:yes"), 1.0e6)]
        )

    def test_classifier_stage(self) -> None:
        book = self._book()
        div = _steer(bucket=("KXMLBGAME:NEW", "KXMLBHR:yes"), book=book)
        conc = _steer(bucket=("KXMLBGAME:SDMIA", "KXMLBGAME:yes"), book=book)
        assert div.hhi.relative > 0.0 > conc.hhi.relative
        assert_diversifier_tighter(div, conc)

    def test_all_three_stages_including_the_grid_snap(self) -> None:
        """The invariant travels the WHOLE path: classifier -> pre-snap ->
        the real ``construct_quote`` output after ``snap_bid_down``."""
        book = self._book()
        div = _steer(bucket=("KXMLBGAME:NEW", "KXMLBHR:yes"), book=book)
        conc = _steer(bucket=("KXMLBGAME:SDMIA", "KXMLBGAME:yes"), book=book)
        grid = PriceGrid.from_market_payload(
            {"ticker": "T",
             "price_ranges": [{"start": "0.001", "end": "0.999",
                               "step": "0.001"}]}
        )
        schedule = FeeSchedule.from_strings("0.07", "0.0175")
        fees = FeeModel(schedule, CONVENTIONS)
        quotes = []
        raws = []
        for st in (div, conc):
            q = construct_quote(
                joint=JointEstimate(
                    p=0.30, uncertainty=0.0, frechet_lo=0.0, frechet_hi=1.0,
                    notes=(),
                ),
                n_legs=3,
                qty=CentiContracts(10_000),
                grid=grid,
                fee_model=fees,
                fee_type=FeeType.QUADRATIC,
                fee_multiplier=Fraction(1),
                time_to_close_s=48 * 3600.0,
                in_play=False,
                yes_cap_cc=CentiCents(9_900),
                no_cap_cc=CentiCents(10_000),
                inventory_skew_cc=st.applied_cc,
            )
            quotes.append(q)
            raws.append(int(CC_PER_DOLLAR * 0.70) + st.applied_cc)
        assert_diversifier_tighter(
            div, conc,
            pre_snap=(raws[0], raws[1]),
            post_snap=(quotes[0], quotes[1]),
        )
        # POST-SNAP the difference is EXACTLY the steer: not annihilated.
        assert int(quotes[0].no_bid_cc) - int(quotes[1].no_bid_cc) == (
            div.applied_cc - conc.applied_cc
        )

    def test_a_refusal_can_never_land_on_the_diversifier(self) -> None:
        class _NoQuote:
            pass

        assert quote_rank_cc(_NoQuote()) == -math.inf
        div = _steer(bucket=("NEW",), book=self._book())
        conc = _steer(bucket=("KXMLBGAME:SDMIA", "KXMLBGAME:yes"),
                      book=self._book())
        with pytest.raises(AssertionError, match="REFUSAL landed"):
            assert_diversifier_tighter(
                div, conc, post_snap=(_NoQuote(), _NoQuote())
            )

    def test_property_holds_over_randomised_books(self) -> None:
        """300 randomised books x a warmed live centre: the diversifier is
        never quoted wider, and never refused, than the candidate that piles
        onto the book's HEAVIEST loss event."""
        rng = random.Random(3)
        checked = 0
        for _ in range(300):
            n = rng.randint(1, 6)
            sizes = [rng.uniform(1e4, 5e6) for _ in range(n)]
            book = build_loss_event_book(
                [((f"G{i}",), s) for i, s in enumerate(sizes)]
            )
            heaviest = (f"G{sizes.index(max(sizes))}",)
            fresh = (f"NEW{rng.random()}",)
            prem = rng.uniform(1e4, 1e6)
            centre = _warm_centre()
            div = _steer(bucket=fresh, book=book, premium_cc=prem,
                         centre=centre)
            conc = _steer(bucket=heaviest, book=book, premium_cc=prem,
                          centre=centre)
            if div.hhi.relative <= conc.hhi.relative:
                continue
            assert_diversifier_tighter(div, conc)
            checked += 1
        assert checked > 200

    def test_the_swing_is_bigger_than_one_tier_step(self) -> None:
        """The measured failure: the FULL concentrating->diversifying swing was
        0.36-0.39c, SMALLER than one markup tier step, against a 200cc median
        margin (5.6% of markup). Standardising on the signal's OWN measured
        dispersion makes +-1 sigma span the whole derived half-range."""
        book = self._book()
        crn = CrnBookCache(
            input_generation=1, col_by_ticker={}, leg_values=None,
            book_pnl=None, bankroll_cc=21_800_000.0, value_sd_cc=57.0,
        )
        centre = _warm_centre()
        div = _steer(bucket=("NEW",), book=book, crn=crn, centre=centre)
        conc = _steer(bucket=("KXMLBGAME:SDMIA", "KXMLBGAME:yes"), book=book,
                      crn=crn, centre=centre)
        swing_cc = div.applied_cc - conc.applied_cc
        # The measured OLD full swing was 39cc (0.39c) and the old median
        # |applied_cc| was 20cc against a 200cc margin. The derived span here
        # is 2 x half_cc = 114cc; the realised swing must use a real part of
        # it and must beat the old FULL swing outright.
        assert swing_cc > 39
        assert swing_cc >= div.scale.half_cc  # >= half the derived span
        assert div.applied_cc > 20  # beats the old median |applied_cc|


class TestBudgetNeutrality:
    """Markups are FIXED (operator, binding). The steer REALLOCATES."""

    def test_centring_pins_the_mean_at_zero(self) -> None:
        centre = SteerCenter(half_life=64.0)
        rng = random.Random(5)
        # A one-sided night: nothing but concentrators.
        scores = [-abs(rng.gauss(0.4, 0.1)) for _ in range(2000)]
        for s in scores:
            centre.observe(s)
        assert abs(centre.mean - sum(scores[-500:]) / 500) < 0.1
        # After centring, the applied steer is no longer uniformly negative.
        centred = [centre.centre(s) for s in scores[-500:]]
        assert abs(sum(centred) / len(centred)) < 0.05

    def test_centring_preserves_the_strict_ordering(self) -> None:
        centre = SteerCenter(half_life=32.0)
        for _ in range(200):
            centre.observe(-0.3)
        a, b = 0.5, 0.1
        assert (centre.centre(a) > centre.centre(b)) == (a > b)

    def test_the_steer_never_exceeds_the_live_margin(self) -> None:
        book = build_loss_event_book([(("G1",), 5.0e6)])
        for margin in (0, 10, 50, 200, 1_000):
            st = _steer(bucket=("G1",), book=book, margin_cc=margin)
            assert abs(st.skew_cc) <= max(margin, 0)


class TestFailSafe:
    def test_empty_book_is_exactly_neutral(self) -> None:
        st = _steer(bucket=("G1",), book=build_loss_event_book([]))
        assert st.skew_cc == 0 and st.hhi.relative == 0.0

    def test_no_crn_falls_back_to_the_zero_se_reading(self) -> None:
        book = build_loss_event_book([(("G1",), 5.0e6)])
        st = _steer(bucket=("NEW",), book=book, crn=None)
        assert st.value_cc_per_contract is None
        assert st.skew_cc < 0  # still rebates, on the zero-SE Herfindahl alone

    def test_no_tick_is_neutral_not_a_guess(self) -> None:
        book = build_loss_event_book([(("G1",), 5.0e6)])
        out = compute_concentration_steer(
            SteerInputs(
                loss_events=book, candidate_bucket=("NEW",),
                candidate_premium_cc=1e5, walls_by_axis={},
                margin_cc=200, tick_cc=0,
                fill_elasticity_per_cent=FILL_ELASTICITY,
            ),
            SteerCenter(), observe=False,
        )
        assert out.skew_cc == 0

    def test_hhi_score_is_strictly_monotone(self) -> None:
        """The steer prices off ``intensity`` (scale-free) since 2026-07-27;
        ``score`` is its negation, so it is strictly DECREASING in intensity —
        a more concentrating candidate always ranks lower."""
        prev = 2.0
        for i in (-0.9, -0.5, -0.1, 0.0, 0.1, 0.5, 0.9, 1.0):
            s = HhiMarginal(
                n_pre=1.0, n_post=1.0, relative=0.0, intensity=i
            ).score
            assert s < prev
            prev = s
            assert -1.0 <= s <= 1.0
