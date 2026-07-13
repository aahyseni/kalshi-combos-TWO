"""Tests for combomaker.risk.exposure — analytic leg deltas, hypothetical
fills from open quotes, book snapshots, and the mass-acceptance dominance
bound (the worst-case book must dominate ANY realizable acceptance pattern).

Direction semantics are injected through Conventions instances built here;
an inverted instance proves hypothetical_positions hardcodes nothing.

Honest guarantee encoded by the dominance property (read from the source's
sign-aligned aggregation): for every market ticker t present in a realized
book, |mass delta_by_market[t]| >= |realized delta_by_market[t]|, because the
mass snapshot walks each quote's worst-side magnitude AWAY from zero
(current >= 0 adds, current < 0 subtracts — the running total never crosses
zero), so |mass| = |positions| + sum of magnitudes, while any realized fill
contributes at most its magnitude in either direction (triangle inequality).
The same argument covers delta_by_event.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    OpenQuoteRisk,
    analytic_leg_deltas,
    mark_to_market,
)

CC = CentiCents
Q = CentiContracts

CONVENTIONS = Conventions(
    verified=True,
    source="test",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)

# Deliberately inverted direction semantics: accepting YES leaves us long NO.
INVERTED = Conventions(
    verified=True,
    source="test-inverted",
    maker_side_on_yes_accept=Side.NO,
    maker_side_on_no_accept=Side.YES,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=False,
    combo_no_pays_complement=True,
)


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


def make_position(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.YES,
    contracts: int = 100,
    entry_price: int = 5_000,
    combo: str = "COMBO-X",
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=combo,
        collection=None,
        our_side=our_side,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


def make_quote(
    qid: str,
    legs: tuple[LegRef, ...],
    *,
    yes_bid: int,
    no_bid: int,
    contracts: int = 100,
) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=qid,
        rfq_id=f"rfq-{qid}",
        combo_ticker=f"COMBO-{qid}",
        collection=None,
        yes_bid_cc=CC(yes_bid),
        no_bid_cc=CC(no_bid),
        contracts=Q(contracts),
        legs=legs,
    )


TWO_YES_LEGS = (LegRef("AAA", "EV1", "yes"), LegRef("BBB", "EV1", "yes"))
TWO_LEG_MARGINALS = {"AAA": 0.5, "BBB": 0.6}


class TestAnalyticLegDeltas:
    def test_two_leg_yes_side_yes_position(self) -> None:
        pos = make_position("p1", TWO_YES_LEGS, our_side=Side.YES, contracts=100)
        deltas = analytic_leg_deltas(pos, provider(TWO_LEG_MARGINALS))
        assert deltas == pytest.approx({"AAA": 0.6, "BBB": 0.5})

    def test_our_side_no_flips_both_signs(self) -> None:
        pos = make_position("p1", TWO_YES_LEGS, our_side=Side.NO, contracts=100)
        deltas = analytic_leg_deltas(pos, provider(TWO_LEG_MARGINALS))
        assert deltas == pytest.approx({"AAA": -0.6, "BBB": -0.5})

    def test_entry_price_is_irrelevant_to_deltas(self) -> None:
        cheap = make_position("p1", TWO_YES_LEGS, entry_price=1)
        rich = make_position("p2", TWO_YES_LEGS, entry_price=9_999)
        marg = provider(TWO_LEG_MARGINALS)
        assert analytic_leg_deltas(cheap, marg) == analytic_leg_deltas(rich, marg)

    def test_three_leg_mixed_sides_hand_computed(self) -> None:
        # A yes p=0.5 (selected 0.5), B no p=0.3 (selected 0.7), C yes p=0.2.
        # 2.00 contracts, our_side YES:
        #   A: +2.0 * 0.7 * 0.2 = +0.28
        #   B: -2.0 * 0.5 * 0.2 = -0.20   (no-side leg flips its own sign AND
        #                                  contributes 1-p to the others)
        #   C: +2.0 * 0.5 * 0.7 = +0.70
        legs = (
            LegRef("AAA", "EV1", "yes"),
            LegRef("BBB", "EV1", "no"),
            LegRef("CCC", "EV2", "yes"),
        )
        marg = provider({"AAA": 0.5, "BBB": 0.3, "CCC": 0.2})
        pos = make_position("p1", legs, our_side=Side.YES, contracts=200)
        deltas = analytic_leg_deltas(pos, marg)
        assert deltas == pytest.approx({"AAA": 0.28, "BBB": -0.20, "CCC": 0.70})

        flipped = analytic_leg_deltas(
            make_position("p2", legs, our_side=Side.NO, contracts=200), marg
        )
        assert flipped is not None and deltas is not None
        assert flipped == {t: -d for t, d in deltas.items()}

    def test_missing_marginal_returns_none_not_zeros(self) -> None:
        pos = make_position("p1", TWO_YES_LEGS)
        result = analytic_leg_deltas(pos, provider({"AAA": 0.5}))  # BBB missing
        assert result is None  # never an empty dict, never zeros


class TestOpenPositionMaxLoss:
    def test_one_contract_at_56_cents(self) -> None:
        pos = make_position("p1", TWO_YES_LEGS, contracts=100, entry_price=5_600)
        assert pos.max_loss_cc == 5_600

    def test_two_and_a_half_contracts_at_40_cents(self) -> None:
        pos = make_position("p1", TWO_YES_LEGS, contracts=250, entry_price=4_000)
        assert pos.max_loss_cc == 10_000


class TestB1SideAwareAxesGroundTruth:
    """B1 anchored to the 2026-07-10 demo settlement: LONG NO 1.00 ct paid
    $0.50 -> max_loss $0.50 (true loss if it HITS), gross_settlement_notional
    $1.00 (capital-utilization axis). Two axes, never summed."""

    def test_demo_no_position_max_loss_is_the_premium(self) -> None:
        pos = make_position(
            "demo", TWO_YES_LEGS, our_side=Side.NO, contracts=100, entry_price=5_000
        )
        assert pos.max_loss_cc == 5_000            # $0.50 — what we PAID / can lose

    def test_demo_no_position_gross_settlement_notional_is_one_dollar(self) -> None:
        pos = make_position(
            "demo", TWO_YES_LEGS, our_side=Side.NO, contracts=100, entry_price=5_000
        )
        assert pos.gross_settlement_notional_cc == 10_000  # $1.00 — notional axis

    def test_two_axes_are_independent_never_summed(self) -> None:
        pos = make_position(
            "demo", TWO_YES_LEGS, our_side=Side.NO, contracts=100, entry_price=5_000
        )
        # The loss axis depends on price paid; the notional axis does NOT.
        assert pos.max_loss_cc != pos.gross_settlement_notional_cc
        cheaper = make_position(
            "c", TWO_YES_LEGS, our_side=Side.NO, contracts=100, entry_price=1_000
        )
        assert cheaper.max_loss_cc == 1_000                       # loss axis moved
        assert cheaper.gross_settlement_notional_cc == 10_000     # notional fixed $1/ct

    def test_gross_settlement_notional_is_price_independent(self) -> None:
        for price in (1, 2_500, 5_000, 9_999):
            pos = make_position(
                "p", TWO_YES_LEGS, our_side=Side.NO, contracts=250, entry_price=price
            )
            assert pos.gross_settlement_notional_cc == 25_000  # 2.50 ct x $1, always


class TestHypotheticalPositions:
    def test_both_sides_quoted_maps_via_conventions(self) -> None:
        quote = make_quote("q1", TWO_YES_LEGS, yes_bid=4_500, no_bid=4_700, contracts=150)
        hypos = quote.hypothetical_positions(CONVENTIONS)
        assert len(hypos) == 2
        by_entry = {int(h.entry_price_cc): h for h in hypos}
        yes_fill = by_entry[4_500]
        no_fill = by_entry[4_700]
        assert yes_fill.our_side is Side.YES  # yes accept -> long YES at yes_bid
        assert no_fill.our_side is Side.NO  # no accept -> long NO at no_bid
        for h in hypos:
            assert h.contracts == Q(150)
            assert h.legs == TWO_YES_LEGS
            assert h.combo_ticker == "COMBO-q1"

    def test_declined_yes_side_produces_no_position(self) -> None:
        quote = make_quote("q1", TWO_YES_LEGS, yes_bid=0, no_bid=4_700)
        hypos = quote.hypothetical_positions(CONVENTIONS)
        assert len(hypos) == 1
        assert hypos[0].our_side is Side.NO
        assert hypos[0].entry_price_cc == CC(4_700)

    def test_declined_no_side_produces_no_position(self) -> None:
        quote = make_quote("q1", TWO_YES_LEGS, yes_bid=4_500, no_bid=0)
        hypos = quote.hypothetical_positions(CONVENTIONS)
        assert len(hypos) == 1
        assert hypos[0].our_side is Side.YES
        assert hypos[0].entry_price_cc == CC(4_500)

    def test_both_sides_declined_produces_nothing(self) -> None:
        quote = make_quote("q1", TWO_YES_LEGS, yes_bid=0, no_bid=0)
        assert quote.hypothetical_positions(CONVENTIONS) == []

    def test_inverted_conventions_flip_sides_no_hardcoding(self) -> None:
        quote = make_quote("q1", TWO_YES_LEGS, yes_bid=4_500, no_bid=4_700)
        hypos = quote.hypothetical_positions(INVERTED)
        by_entry = {int(h.entry_price_cc): h for h in hypos}
        assert by_entry[4_500].our_side is Side.NO  # yes accept -> long NO
        assert by_entry[4_700].our_side is Side.YES  # no accept -> long YES


SNAP_MARGINALS = {"AAA": 0.5, "BBB": 0.6, "CCC": 0.25}


def two_position_book() -> ExposureBook:
    book = ExposureBook(CONVENTIONS)
    # pos1: long YES 1.00 @ $0.56, legs AAA/BBB both in EV1.
    book.add_position(
        make_position("p1", TWO_YES_LEGS, our_side=Side.YES, contracts=100, entry_price=5_600)
    )
    # pos2: long NO 2.00 @ $0.30, legs BBB (EV1) and CCC (EV2).
    book.add_position(
        make_position(
            "p2",
            (LegRef("BBB", "EV1", "yes"), LegRef("CCC", "EV2", "yes")),
            our_side=Side.NO,
            contracts=200,
            entry_price=3_000,
        )
    )
    return book


class TestSnapshotWithoutMassAcceptance:
    def test_aggregates_across_positions(self) -> None:
        # pos1 deltas: AAA +0.6, BBB +0.5.  pos2 deltas: BBB -0.5, CCC -1.2.
        snap = two_position_book().snapshot(provider(SNAP_MARGINALS), mass_acceptance=False)
        assert snap.delta_by_market == pytest.approx({"AAA": 0.6, "BBB": 0.0, "CCC": -1.2})
        assert snap.delta_by_event == pytest.approx({"EV1": 0.6, "EV2": -1.2})
        assert snap.gross_notional_cc == 5_600 + 6_000
        assert snap.worst_case_loss_by_event_cc == {"EV1": 11_600, "EV2": 6_000}
        assert snap.open_quote_count == 0
        assert snap.unknown_marginals is False

    def test_open_quotes_ignored_except_count(self) -> None:
        book = two_position_book()
        book.upsert_quote(
            make_quote("q1", TWO_YES_LEGS, yes_bid=4_000, no_bid=4_000, contracts=100)
        )
        snap = book.snapshot(provider(SNAP_MARGINALS), mass_acceptance=False)
        assert snap.gross_notional_cc == 11_600  # quote contributes nothing
        assert snap.delta_by_market == pytest.approx({"AAA": 0.6, "BBB": 0.0, "CCC": -1.2})
        assert snap.open_quote_count == 1

    def test_unknown_marginal_sets_flag_but_keeps_gross(self) -> None:
        marg = provider({"AAA": 0.5, "BBB": 0.6})  # CCC unavailable
        snap = two_position_book().snapshot(marg, mass_acceptance=False)
        assert snap.unknown_marginals is True
        # pos2's deltas are dropped (not zeroed into the aggregate)...
        assert snap.delta_by_market == pytest.approx({"AAA": 0.6, "BBB": 0.5})
        # ...but its notional risk still counts — missing data never shrinks gross.
        assert snap.gross_notional_cc == 11_600
        assert snap.worst_case_loss_by_event_cc == {"EV1": 11_600, "EV2": 6_000}


# --- B2: aggregation keys on the GAME, not the raw event ---------------------

GAME = "26JUL05MEXENG"
# Two market FAMILIES of ONE match: distinct event_tickers, ONE game code.
GAME_LEG = LegRef("KXWCGAME-26JUL05MEXENG-MEX", f"KXWCGAME-{GAME}", "yes")
TOTAL_LEG = LegRef("KXWCTOTAL-26JUL05MEXENG-3", f"KXWCTOTAL-{GAME}", "yes")
OTHER_GAME_LEG = LegRef("KXWCGAME-26JUL06ARGBRA-ARG", "KXWCGAME-26JUL06ARGBRA", "yes")


class TestB2GameClustering:
    """The B2 proof: a GAME leg and a TOTAL leg of the SAME match (different
    event_tickers, same game code) MUST land in ONE game bucket — pre-B2 they
    split across two event buckets."""

    def _book(self) -> ExposureBook:
        book = ExposureBook(CONVENTIONS)
        # One combo per family, both NO (sell-only), both on the same game.
        book.add_position(
            make_position(
                "game_combo", (GAME_LEG,), our_side=Side.NO, contracts=100, entry_price=5_000
            )
        )
        book.add_position(
            make_position(
                "total_combo", (TOTAL_LEG,), our_side=Side.NO, contracts=100, entry_price=4_000
            )
        )
        return book

    def test_two_families_land_in_one_game_bucket(self) -> None:
        marg = provider(
            {GAME_LEG.market_ticker: 0.5, TOTAL_LEG.market_ticker: 0.6}
        )
        snap = self._book().snapshot(marg, mass_acceptance=False)
        # ONE key, the game code — not two event keys.
        assert set(snap.worst_case_loss_by_game_cc) == {GAME}
        # Loss axis: both premiums sum into the single game cluster.
        assert snap.worst_case_loss_by_game_cc[GAME] == 5_000 + 4_000
        # Notional axis: both $1/ct notionals sum into the same cluster.
        assert snap.gross_settlement_notional_by_game_cc[GAME] == 10_000 + 10_000
        # Delta axis also game-keyed to one bucket.
        assert set(snap.delta_by_game) == {GAME}

    def test_distinct_games_stay_separate(self) -> None:
        book = self._book()
        book.add_position(
            make_position(
                "other", (OTHER_GAME_LEG,), our_side=Side.NO, contracts=100, entry_price=2_000
            )
        )
        marg = provider(
            {
                GAME_LEG.market_ticker: 0.5,
                TOTAL_LEG.market_ticker: 0.6,
                OTHER_GAME_LEG.market_ticker: 0.4,
            }
        )
        snap = book.snapshot(marg, mass_acceptance=False)
        assert set(snap.worst_case_loss_by_game_cc) == {GAME, "26JUL06ARGBRA"}
        assert snap.worst_case_loss_by_game_cc["26JUL06ARGBRA"] == 2_000

    def test_back_compat_alias_returns_game_keyed_data(self) -> None:
        marg = provider(
            {GAME_LEG.market_ticker: 0.5, TOTAL_LEG.market_ticker: 0.6}
        )
        snap = self._book().snapshot(marg, mass_acceptance=False)
        # The old field name now yields the game-keyed data (no event split).
        assert snap.worst_case_loss_by_event_cc == snap.worst_case_loss_by_game_cc
        assert snap.delta_by_event == snap.delta_by_game

    def test_ungamed_event_never_merges(self) -> None:
        # A leg whose event carries no hyphen keys on the whole string, so it
        # can never be pulled into a real game's cluster (fail-closed).
        book = ExposureBook(CONVENTIONS)
        book.add_position(
            make_position(
                "u", (LegRef("MKT", "SYNTHETIC", "yes"),),
                our_side=Side.NO, contracts=100, entry_price=1_000,
            )
        )
        snap = book.snapshot(provider({"MKT": 0.5}), mass_acceptance=False)
        assert set(snap.worst_case_loss_by_game_cc) == {"SYNTHETIC"}


ONE_LEG = (LegRef("AAA", "EV1", "yes"),)


class TestSnapshotMassAcceptance:
    def test_worst_side_aligned_with_positive_position_delta(self) -> None:
        book = ExposureBook(CONVENTIONS)
        book.add_position(
            make_position("p1", ONE_LEG, our_side=Side.YES, contracts=50, entry_price=2_000)
        )
        book.upsert_quote(make_quote("q1", ONE_LEG, yes_bid=4_000, no_bid=3_000, contracts=100))
        snap = book.snapshot(provider({"AAA": 0.5}), mass_acceptance=True)
        # Position delta +0.5; quote fills at +/-1.0 -> aligned bound +1.5.
        assert snap.delta_by_market == pytest.approx({"AAA": 1.5})
        assert snap.delta_by_event == pytest.approx({"EV1": 1.5})
        # Gross takes the WORSE (yes @ $0.40) side: 1000 + 4000.
        assert snap.gross_notional_cc == 5_000
        assert snap.worst_case_loss_by_event_cc == {"EV1": 5_000}
        assert snap.unknown_marginals is False

    def test_worst_side_aligned_with_negative_position_delta(self) -> None:
        book = ExposureBook(CONVENTIONS)
        book.add_position(
            make_position("p1", ONE_LEG, our_side=Side.NO, contracts=50, entry_price=2_000)
        )
        book.upsert_quote(make_quote("q1", ONE_LEG, yes_bid=4_000, no_bid=3_000, contracts=100))
        snap = book.snapshot(provider({"AAA": 0.5}), mass_acceptance=True)
        assert snap.delta_by_market == pytest.approx({"AAA": -1.5})
        assert snap.delta_by_event == pytest.approx({"EV1": -1.5})

    def test_unknown_marginal_in_quote_leg_sets_flag(self) -> None:
        book = ExposureBook(CONVENTIONS)
        book.upsert_quote(make_quote("q1", ONE_LEG, yes_bid=4_000, no_bid=3_000, contracts=100))
        snap = book.snapshot(provider({}), mass_acceptance=True)
        assert snap.unknown_marginals is True
        assert snap.gross_notional_cc == 4_000  # notional bound survives missing data


# --- mass-acceptance dominance property ------------------------------------

TICKERS = ("M0", "M1", "M2", "M3", "M4")
UNIVERSE_MARGINALS = {"M0": 0.2, "M1": 0.35, "M2": 0.5, "M3": 0.65, "M4": 0.8}
EVENT_OF = {"M0": "EV-A", "M1": "EV-A", "M2": "EV-B", "M3": "EV-B", "M4": "EV-C"}
UNIVERSE_PROVIDER = provider(UNIVERSE_MARGINALS)


@st.composite
def book_cases(
    draw: st.DrawFn,
) -> tuple[list[OpenPosition], list[OpenQuoteRisk], list[int]]:
    def draw_legs() -> tuple[LegRef, ...]:
        tickers = draw(
            st.lists(st.sampled_from(TICKERS), min_size=1, max_size=3, unique=True)
        )
        return tuple(
            LegRef(t, EVENT_OF[t], draw(st.sampled_from(("yes", "no")))) for t in tickers
        )

    positions = [
        OpenPosition(
            position_id=f"pos{i}",
            combo_ticker=f"COMBO-P{i}",
            collection=None,
            our_side=draw(st.sampled_from((Side.YES, Side.NO))),
            contracts=Q(draw(st.sampled_from((50, 100, 250, 300)))),
            entry_price_cc=CC(draw(st.sampled_from((1_000, 4_000, 7_500)))),
            legs=draw_legs(),
        )
        for i in range(draw(st.integers(min_value=0, max_value=3)))
    ]
    quotes = [
        OpenQuoteRisk(
            quote_id=f"q{i}",
            rfq_id=f"rfq{i}",
            combo_ticker=f"COMBO-Q{i}",
            collection=None,
            yes_bid_cc=CC(draw(st.sampled_from((0, 2_000, 5_000, 8_000)))),
            no_bid_cc=CC(draw(st.sampled_from((0, 2_000, 5_000, 8_000)))),
            contracts=Q(draw(st.sampled_from((50, 100, 200)))),
            legs=draw_legs(),
        )
        for i in range(draw(st.integers(min_value=0, max_value=4)))
    ]
    # One acceptance choice per quote: an index into hypos + "no fill".
    choices = [draw(st.integers(min_value=0, max_value=2)) for _ in quotes]
    return positions, quotes, choices


class TestMassAcceptanceDominance:
    @given(case=book_cases())
    @settings(derandomize=True, max_examples=200, deadline=None)
    def test_mass_snapshot_dominates_every_realized_acceptance(
        self, case: tuple[list[OpenPosition], list[OpenQuoteRisk], list[int]]
    ) -> None:
        positions, quotes, choices = case

        book = ExposureBook(CONVENTIONS)
        for pos in positions:
            book.add_position(pos)
        for quote in quotes:
            book.upsert_quote(quote)
        mass = book.snapshot(UNIVERSE_PROVIDER, mass_acceptance=True)
        assert mass.unknown_marginals is False

        # Realize one arbitrary acceptance pattern: each open quote fills on
        # the chosen side (or not at all) at its quoted price.
        realized_book = ExposureBook(CONVENTIONS)
        for pos in positions:
            realized_book.add_position(pos)
        for quote, choice in zip(quotes, choices, strict=True):
            hypos = quote.hypothetical_positions(CONVENTIONS)
            idx = choice % (len(hypos) + 1)
            if idx < len(hypos):
                realized_book.add_position(hypos[idx])
        realized = realized_book.snapshot(UNIVERSE_PROVIDER, mass_acceptance=False)
        assert realized.unknown_marginals is False

        assert realized.gross_notional_cc <= mass.gross_notional_cc

        for event, loss_cc in realized.worst_case_loss_by_event_cc.items():
            assert event in mass.worst_case_loss_by_event_cc
            assert loss_cc <= mass.worst_case_loss_by_event_cc[event]

        # Honest per-market guarantee (see module docstring): sign-aligned
        # magnitudes give |mass delta| >= |any realized delta|, market by market.
        for ticker, delta in realized.delta_by_market.items():
            assert ticker in mass.delta_by_market
            assert abs(delta) <= abs(mass.delta_by_market[ticker]) + 1e-9

        # Same mechanism per event.
        for event, delta in realized.delta_by_event.items():
            assert event in mass.delta_by_event
            assert abs(delta) <= abs(mass.delta_by_event[event]) + 1e-9


class TestMarkToMarket:
    def test_two_positions_exact_values(self) -> None:
        # Fairs chosen as exact binary fractions so cc conversion is exact.
        pos_yes = make_position(
            "p1", TWO_YES_LEGS, our_side=Side.YES, contracts=100, entry_price=5_600, combo="CY"
        )
        pos_no = make_position(
            "p2", TWO_YES_LEGS, our_side=Side.NO, contracts=250, entry_price=4_000, combo="CN"
        )
        fairs = {"CY": 0.75, "CN": 0.25}
        result = mark_to_market([pos_yes, pos_no], lambda p: fairs.get(p.combo_ticker))
        assert result is not None
        # YES: payout prob 0.75 -> 7500cc * 1.00 = 7500.
        # NO:  payout prob 1 - 0.25 = 0.75 -> 7500cc * 2.50 = 18750.
        assert result.value_cc == 7_500 + 18_750
        assert result.cost_cc == 5_600 + 10_000
        assert result.unrealized_cc == 26_250 - 15_600

    def test_unmarkable_position_returns_none(self) -> None:
        pos_a = make_position("p1", TWO_YES_LEGS, combo="KNOWN")
        pos_b = make_position("p2", TWO_YES_LEGS, combo="UNKNOWN")
        fairs = {"KNOWN": 0.5}
        assert mark_to_market([pos_a, pos_b], lambda p: fairs.get(p.combo_ticker)) is None
