"""Quote-time resting-quote haircut (operator design 2026-07-17).

Prototype-first per hard rule 8: the composition was validated in
``tools/proto_resting_haircut.py`` (3,000-case monotonicity fuzz — 0
violations; 1,000-case weight-1 parity vs the live snapshot; 1,000-case burst
floor; 1,000-case post-port parity live == prototype; 3,000-case F1 pre-gate
lemma re-check with the haircut ARMED — 0 violations; 7/16-17 tape replay).
Pinned here:

- COMPOSITION: hand-computed values on the loss/gross axes at weight 0.4 —
  the min(full, max(blend, base+topK)) fold, floor binding and not;
- DEFAULTS: weight 1 / None ⇒ byte-identical aggregates (today's fold);
- MONOTONICITY (spec point 1): adding a resting quote never decreases any
  haircut aggregate (hypothesis, live port);
- BURST FLOOR: haircut snapshot >= the 100% snapshot of only the K largest;
- CONFIRM-TIME REGRESSION (spec point 2): with the weight ARMED in RiskLimits,
  every ``check`` WITHOUT ``apply_resting_haircut`` and every
  ``try_reserve`` is bit-identical to a weight-1 checker — while the ARMED
  quote-time check demonstrably differs (the seam is the flag, and confirm
  sites cannot pick the weight up even by accident);
- E2 REWRITE (spec point 4): the old "quote-time bound dominates any accept
  subset" is void BY DESIGN under the haircut. NEW invariant, property-tested:
  for ANY resting set admitted under the haircut and ANY accept
  subset/order, the SERIAL confirm path (provisional reservations + analytic
  caps at confirm, 100% fold) never commits positions whose realized budget
  consumption exceeds the configured budgets — excess accepts are declined;
- POST-FILL PULL (spec point 3): eviction pass deletes exactly the resting
  quotes whose game shows an enforced quote-time breach, largest first,
  same-game-as-fill first; accepted quotes never yanked; errors fail SAFE;
  disarmed (weight 1) ⇒ the pull never schedules.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import RiskConfig
from combomaker.ops.persistence import Store
from combomaker.rfq.lifecycle import LifecycleConfig, OpenQuoteState
from combomaker.risk.exposure import (
    ExposureBook,
    LegRef,
    OpenPosition,
    OpenQuoteRisk,
)
from combomaker.risk.limits import (
    Breach,
    DailyPnl,
    LimitChecker,
    RiskLimits,
    threshold_cc,
)
from combomaker.risk.reservation import RiskReservationService
from tests.test_filters import Harness
from tests.test_lifecycle import Rig, accepted_msg
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

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

W40 = Fraction(2, 5)


def provider(p: float = 0.5) -> Any:
    return lambda _ticker: p


def leg(game: str, n: int = 1, side: str = "yes") -> LegRef:
    return LegRef(f"KXWCGAME-{game}-T{n}", f"KXWCGAME-{game}", side)


def position(
    pid: str, game: str, *, contracts: int, price: int, side: Side = Side.NO
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=side,
        contracts=Q(contracts),
        entry_price_cc=CC(price),
        legs=(leg(game),),
    )


def quote(
    qid: str, game: str, *, contracts: int, no_bid: int, yes_bid: int = 0, n: int = 1
) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=qid,
        rfq_id=f"rfq-{qid}",
        combo_ticker=f"COMBO-{qid}",
        collection=None,
        yes_bid_cc=CC(yes_bid),
        no_bid_cc=CC(no_bid),
        contracts=Q(contracts),
        legs=(leg(game, n=n),),
    )


# --------------------------------------------------------------- composition


class TestComposition:
    """Hand-computed folds: base 70_000cc committed on G1; resting quotes of
    30_000 and 8_000 worst-case loss on the same game; weight 0.40, floor 1.

      full  = 108_000; blend = ceil(0.4*108_000 + 0.6*70_000) = 85_200
      floor = base + top1 = 100_000  ->  value = min(108_000, 100_000) = 100_000
    """

    def build(self) -> ExposureBook:
        book = ExposureBook(CONVENTIONS)
        book.add_position(position("p1", "G1", contracts=2000, price=3_500))  # 70_000
        book.upsert_quote(quote("q1", "G1", contracts=1000, no_bid=3_000))    # 30_000
        book.upsert_quote(quote("q2", "G1", contracts=800, no_bid=1_000))     # 8_000
        return book

    def test_loss_axis_floor_binding(self) -> None:
        snap = self.build().snapshot(
            provider(), mass_acceptance=True,
            resting_quote_weight=W40, resting_floor_count=1,
        )
        assert snap.worst_case_loss_by_game_cc["G1"] == 100_000
        assert snap.gross_notional_cc == 100_000

    def test_loss_axis_weighted_term_binding(self) -> None:
        # floor 1 with a small top quote: base 70_000, resting 8_000 + 6_000,
        # full 84_000, blend = ceil(0.4*84_000 + 0.6*70_000) = 75_600,
        # floor = 78_000 -> value = 78_000 (floor still the max); with floor
        # count 0 impossible (config >=1) — use two tiny quotes so the WEIGHTED
        # term wins: top1 = 6_000 -> floor 76_000 < blend? blend 75_600 < 76_000
        # -> 76_000. Make top1 smaller: 4_000/2_000: full 76_000, blend =
        # ceil(0.4*76_000+0.6*70_000) = 72_400, floor = 74_000 -> 74_000.
        # For a case where the BLEND binds, the weighted resting mass must
        # exceed the largest single quote: five quotes of 4_000 each ->
        # full 90_000, blend = ceil(0.4*90_000+0.6*70_000) = 78_000,
        # floor(1) = 74_000 -> value 78_000.
        book = ExposureBook(CONVENTIONS)
        book.add_position(position("p1", "G1", contracts=2000, price=3_500))
        for i in range(5):
            book.upsert_quote(quote(f"q{i}", "G1", contracts=400, no_bid=1_000))
        snap = book.snapshot(
            provider(), mass_acceptance=True,
            resting_quote_weight=W40, resting_floor_count=1,
        )
        assert snap.worst_case_loss_by_game_cc["G1"] == 78_000

    def test_weight_one_and_none_identical(self) -> None:
        book = self.build()
        plain = book.snapshot(provider(), mass_acceptance=True)
        w1 = book.snapshot(
            provider(), mass_acceptance=True,
            resting_quote_weight=Fraction(1), resting_floor_count=3,
        )
        assert plain.worst_case_loss_by_game_cc == w1.worst_case_loss_by_game_cc
        assert plain.gross_notional_cc == w1.gross_notional_cc
        assert plain.delta_by_market == w1.delta_by_market
        assert plain.delta_by_game == w1.delta_by_game
        assert plain.directional_by_game_cc == w1.directional_by_game_cc
        assert (
            plain.gross_settlement_notional_by_game_cc
            == w1.gross_settlement_notional_by_game_cc
        )

    def test_floor_count_covering_all_quotes_is_full_fold(self) -> None:
        book = self.build()
        plain = book.snapshot(provider(), mass_acceptance=True)
        hc = book.snapshot(
            provider(), mass_acceptance=True,
            resting_quote_weight=W40, resting_floor_count=10,
        )
        assert (
            hc.worst_case_loss_by_game_cc == plain.worst_case_loss_by_game_cc
        )
        assert hc.gross_notional_cc == plain.gross_notional_cc

    def test_candidates_never_haircut(self) -> None:
        # A candidate (extra_position) counts fully even at weight 0.4: an
        # empty-quotes book with one candidate folds identically armed or not.
        book = ExposureBook(CONVENTIONS)
        cand = position("cand", "G1", contracts=2000, price=3_500)
        plain = book.snapshot(
            provider(), mass_acceptance=True, extra_positions=[cand]
        )
        hc = book.snapshot(
            provider(), mass_acceptance=True, extra_positions=[cand],
            resting_quote_weight=W40, resting_floor_count=1,
        )
        assert hc.worst_case_loss_by_game_cc == plain.worst_case_loss_by_game_cc
        assert hc.gross_notional_cc == plain.gross_notional_cc


# ----------------------------------------------- hypothesis: monotone + floor

TICKERS = ("M0", "M1", "M2", "M3", "M4")
MARGINALS = {"M0": 0.2, "M1": 0.35, "M2": 0.5, "M3": 0.65, "M4": 0.8}
# Two games; G1 carries TWO ME-flagged event families (EVA/EVB) so the
# fail-closed 1->2 ME-event transition is REACHABLE by the property tests
# (2026-07-17 floor finding: the old strategy carried no is_me_event at all,
# so the netting/fail-closed regimes were untested). EVT is non-ME
# (totals-like); EVC makes G2 a single-ME game (the netting regime).
EVENT_OF = {
    "M0": "EVA-G1",
    "M1": "EVA-G1",
    "M2": "EVB-G1",
    "M3": "EVC-G2",
    "M4": "EVT-G2",
}
IS_ME = {"EVA-G1": True, "EVB-G1": True, "EVC-G2": True, "EVT-G2": False}


def _is_me(event: str) -> bool | None:
    return IS_ME.get(event)


def _marg(ticker: str) -> float | None:
    return MARGINALS.get(ticker)


@st.composite
def haircut_cases(
    draw: st.DrawFn,
) -> tuple[list[OpenPosition], list[OpenQuoteRisk], OpenQuoteRisk, Fraction, int]:
    def draw_legs() -> tuple[LegRef, ...]:
        tickers = draw(
            st.lists(st.sampled_from(TICKERS), min_size=1, max_size=3, unique=True)
        )
        return tuple(
            LegRef(t, EVENT_OF[t], draw(st.sampled_from(("yes", "no"))))
            for t in tickers
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

    def draw_quote(qid: str) -> OpenQuoteRisk:
        return OpenQuoteRisk(
            quote_id=qid,
            rfq_id=f"rfq{qid}",
            combo_ticker=f"COMBO-{qid}",
            collection=None,
            yes_bid_cc=CC(draw(st.sampled_from((0, 2_000, 5_000, 8_000)))),
            no_bid_cc=CC(draw(st.sampled_from((0, 2_000, 5_000, 8_000)))),
            contracts=Q(draw(st.sampled_from((50, 100, 200)))),
            legs=draw_legs(),
        )

    quotes = [draw_quote(f"q{i}") for i in range(draw(st.integers(0, 4)))]
    extra = draw_quote("q-new")
    weight = draw(
        st.sampled_from((Fraction(1, 10), Fraction(2, 5), Fraction(3, 4)))
    )
    floor = draw(st.integers(min_value=1, max_value=3))
    return positions, quotes, extra, weight, floor


def build_book(
    positions: list[OpenPosition], quotes: list[OpenQuoteRisk]
) -> ExposureBook:
    # is_me_event wired (2026-07-17): the mutex netting + fail-closed regimes
    # are part of the folds the haircut composes over, so the monotonicity and
    # floor properties must hold ACROSS regime transitions too.
    book = ExposureBook(CONVENTIONS, is_me_event=_is_me)
    for p in positions:
        book.add_position(p)
    for q in quotes:
        book.upsert_quote(q)
    return book


class TestHaircutProperties:
    @given(case=haircut_cases())
    @settings(derandomize=True, max_examples=200, deadline=None)
    def test_adding_a_resting_quote_never_decreases_any_bucket(
        self,
        case: tuple[
            list[OpenPosition], list[OpenQuoteRisk], OpenQuoteRisk, Fraction, int
        ],
    ) -> None:
        positions, quotes, extra, weight, floor = case
        before = build_book(positions, quotes).snapshot(
            _marg, mass_acceptance=True,
            resting_quote_weight=weight, resting_floor_count=floor,
        )
        after = build_book(positions, [*quotes, extra]).snapshot(
            _marg, mass_acceptance=True,
            resting_quote_weight=weight, resting_floor_count=floor,
        )
        assert before.gross_notional_cc <= after.gross_notional_cc
        for game, v in before.worst_case_loss_by_game_cc.items():
            assert v <= after.worst_case_loss_by_game_cc.get(game, 0)
        for game, v in before.gross_settlement_notional_by_game_cc.items():
            assert v <= after.gross_settlement_notional_by_game_cc.get(game, 0)
        for game, v in before.directional_by_game_cc.items():
            assert v <= after.directional_by_game_cc.get(game, 0) + 1
        for ticker, d in before.delta_by_market.items():
            assert abs(d) <= abs(after.delta_by_market.get(ticker, 0.0)) + 1e-9
        for game, d in before.delta_by_game.items():
            assert abs(d) <= abs(after.delta_by_game.get(game, 0.0)) + 1e-9

    @given(case=haircut_cases())
    @settings(derandomize=True, max_examples=200, deadline=None)
    def test_burst_floor_covers_the_k_largest_at_full(
        self,
        case: tuple[
            list[OpenPosition], list[OpenQuoteRisk], OpenQuoteRisk, Fraction, int
        ],
    ) -> None:
        positions, quotes, _extra, weight, floor = case
        hc = build_book(positions, quotes).snapshot(
            _marg, mass_acceptance=True,
            resting_quote_weight=weight, resting_floor_count=floor,
        )
        top = sorted(
            quotes,
            key=lambda q: max(
                (h.max_loss_cc for h in q.hypothetical_positions(CONVENTIONS)),
                default=0,
            ),
            reverse=True,
        )[:floor]
        full_topk = build_book(positions, top).snapshot(_marg, mass_acceptance=True)
        assert hc.gross_notional_cc >= full_topk.gross_notional_cc
        for game, v in full_topk.worst_case_loss_by_game_cc.items():
            assert hc.worst_case_loss_by_game_cc.get(game, 0) >= v

    @given(case=haircut_cases())
    @settings(derandomize=True, max_examples=100, deadline=None)
    def test_weight_one_is_byte_identical_to_default(
        self,
        case: tuple[
            list[OpenPosition], list[OpenQuoteRisk], OpenQuoteRisk, Fraction, int
        ],
    ) -> None:
        positions, quotes, _extra, _weight, floor = case
        book = build_book(positions, quotes)
        plain = book.snapshot(_marg, mass_acceptance=True)
        w1 = book.snapshot(
            _marg, mass_acceptance=True,
            resting_quote_weight=Fraction(1), resting_floor_count=floor,
        )
        assert plain.worst_case_loss_by_game_cc == w1.worst_case_loss_by_game_cc
        assert plain.gross_notional_cc == w1.gross_notional_cc
        assert plain.delta_by_market == w1.delta_by_market
        assert plain.delta_by_game == w1.delta_by_game
        assert plain.directional_by_game_cc == w1.directional_by_game_cc


# -------------------- mutex fail-closed regime floor (2026-07-17 regression)


def me2_is_me(event: str) -> bool | None:
    return True if event.startswith("KXME") else None


def me2_leg(family: str, outcome: str) -> LegRef:
    event = f"{family}-G1"
    return LegRef(f"{event}-{outcome}", event, "yes")


def me2_position(pid: str, outcome: str) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"C-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=Q(2000),
        entry_price_cc=CC(5_000),  # max_loss 100_000cc
        legs=(me2_leg("KXME1", outcome),),
    )


def me2_quote(qid: str, family: str, outcome: str) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=qid,
        rfq_id=f"r-{qid}",
        combo_ticker=f"C-{qid}",
        collection=None,
        yes_bid_cc=CC(0),
        no_bid_cc=CC(3_000),
        contracts=Q(1000),  # worst loss 30_000cc
        legs=(me2_leg(family, outcome),),
    )


class TestMutexRegimeFloorPinned:
    """PINNED REGRESSION (2026-07-17 verify finding): the canonical
    advance-hedge book — committed long-NO on TWO OPPOSING outcomes of ME
    event KXME1 (base NETS to 100_000cc; comonotone 200_000) plus THREE
    30_000cc resting quotes on a SECOND ME event KXME2 in the same game
    bucket. The combined census carries 2 ME events, so the true fold FAILS
    CLOSED to the comonotone 290_000cc — and so does the fold of base + the
    K=3 largest resting quotes. The burst floor's base term must therefore be
    the COMONOTONE base (200_000), not the netted base (100_000): armed value
    = min(full, max(blend 176_000, 200_000 + 90_000)) = 290_000 = full. The
    broken composition produced base_v + topK = 190_000 — 100_000cc BELOW its
    own floor property — and ADMITTED a burst the unarmed check declines."""

    def build(self, n_quotes: int = 3) -> ExposureBook:
        book = ExposureBook(CONVENTIONS, is_me_event=me2_is_me)
        book.add_position(me2_position("p1", "A"))
        book.add_position(me2_position("p2", "B"))
        for i in range(n_quotes):
            book.upsert_quote(me2_quote(f"q{i}", "KXME2", f"O{i}"))
        return book

    def test_base_nets_and_combined_fold_fails_closed(self) -> None:
        base = self.build(0).snapshot(provider(), mass_acceptance=True)
        assert base.worst_case_loss_by_game_cc["G1"] == 100_000  # netted
        full = self.build().snapshot(provider(), mass_acceptance=True)
        assert full.worst_case_loss_by_game_cc["G1"] == 290_000  # comonotone

    def test_armed_floor_holds_at_the_1_to_2_me_transition(self) -> None:
        book = self.build()
        full = book.snapshot(provider(), mass_acceptance=True)
        armed = book.snapshot(
            provider(), mass_acceptance=True,
            resting_quote_weight=W40, resting_floor_count=3,
        )
        # K=3 covers exactly the whole resting set, so the floor must force
        # the FULL fold on both mutex-folded axes (was 190_000 / lower).
        assert armed.worst_case_loss_by_game_cc["G1"] == 290_000
        assert (
            armed.directional_by_game_cc["G1"]
            == full.directional_by_game_cc["G1"]
        )

    def test_floor_count_covering_all_quotes_is_full_fold_two_me(self) -> None:
        book = self.build()
        plain = book.snapshot(provider(), mass_acceptance=True)
        hc = book.snapshot(
            provider(), mass_acceptance=True,
            resting_quote_weight=W40, resting_floor_count=10,
        )
        assert hc.worst_case_loss_by_game_cc == plain.worst_case_loss_by_game_cc
        assert hc.directional_by_game_cc == plain.directional_by_game_cc

    def test_armed_check_declines_the_burst_like_the_unarmed_check(self) -> None:
        limits = RiskLimits(
            game_loss_frac=Fraction(8, 100),
            directional_frac=Fraction(1),  # isolate the game-loss axis
            resting_quote_weight=W40,
            resting_floor_count=3,
        )
        checker = LimitChecker(limits)
        book = self.build()
        bankroll = 3_000_000  # game budget 240_000cc < the 290_000cc full fold
        unarmed = checker.check(
            book, provider(), DailyPnl(),
            risk_bankroll_cc=bankroll, apply_resting_haircut=False,
        )
        armed = checker.check(
            book, provider(), DailyPnl(),
            risk_bankroll_cc=bankroll, apply_resting_haircut=True,
        )
        assert ReasonCode.SKIP_GAME_LOSS_CAP in {b.reason for b in unarmed}
        # THE REGRESSION: the broken floor (190_000) ADMITTED this
        # exactly-floor-count burst; the armed check must decline it too.
        assert ReasonCode.SKIP_GAME_LOSS_CAP in {b.reason for b in armed}

    def test_single_me_event_control_still_nets(self) -> None:
        # Same shape but the quotes ride the SAME ME event as the base
        # (census == 1): the netting credit stays, and the floor is the netted
        # base + topK — the regime the original fuzz covered. Full = branch
        # max(A: 100_000 + 90_000, B: 100_000) = 190_000; armed == full.
        book = ExposureBook(CONVENTIONS, is_me_event=me2_is_me)
        book.add_position(me2_position("p1", "A"))
        book.add_position(me2_position("p2", "B"))
        for i in range(3):
            book.upsert_quote(me2_quote(f"q{i}", "KXME1", "A"))
        full = book.snapshot(provider(), mass_acceptance=True)
        assert full.worst_case_loss_by_game_cc["G1"] == 190_000
        armed = book.snapshot(
            provider(), mass_acceptance=True,
            resting_quote_weight=W40, resting_floor_count=3,
        )
        assert armed.worst_case_loss_by_game_cc["G1"] == 190_000


# ------------------------------- confirm-time bit-identical regression (spec 2)


def breach_key(b: Breach) -> tuple[str, str, bool, str | None]:
    return (str(b.reason), b.detail, b.shadow, b.game)


def limits_with_weight(weight: Fraction) -> RiskLimits:
    return RiskLimits(
        game_loss_frac=Fraction(8, 100),
        resting_quote_weight=weight,
        resting_floor_count=1,
    )


class TestConfirmTimePinnedAtFullFold:
    """The book: 70_000cc committed + 30_000 + 8_000 resting on one game;
    bankroll $100 => game budget 80_000cc. Full fold 108_000 BREACHES; the
    armed haircut fold 100_000 still breaches; drop the 30_000 quote and the
    armed fold (78_000) PASSES while the full fold (78_000... ) — use the
    5-quote blend book where armed=78_000 passes and full=90_000 breaches."""

    BANKROLL = 1_000_000  # cc => game_thr = 80_000

    def blend_book(self) -> ExposureBook:
        book = ExposureBook(CONVENTIONS)
        book.add_position(position("p1", "G1", contracts=2000, price=3_500))
        for i in range(5):
            book.upsert_quote(quote(f"q{i}", "G1", contracts=400, no_bid=1_000))
        return book

    def check(
        self, checker: LimitChecker, book: ExposureBook, *, armed: bool
    ) -> list[Breach]:
        return checker.check(
            book,
            provider(),
            DailyPnl(),
            risk_bankroll_cc=self.BANKROLL,
            apply_resting_haircut=armed,
        )

    def test_armed_quote_time_check_differs_and_unarmed_is_bit_identical(
        self,
    ) -> None:
        book = self.blend_book()
        armed_checker = LimitChecker(limits_with_weight(W40))
        default_checker = LimitChecker(limits_with_weight(Fraction(1)))

        # The haircut has TEETH on this book: armed passes the game cap
        # (78_000 <= 80_000), unarmed breaches (full fold 90_000).
        armed = self.check(armed_checker, book, armed=True)
        assert ReasonCode.SKIP_GAME_LOSS_CAP not in {b.reason for b in armed}
        unarmed = self.check(armed_checker, book, armed=False)
        assert ReasonCode.SKIP_GAME_LOSS_CAP in {b.reason for b in unarmed}

        # CONFIRM-TIME REGRESSION: without the flag, a weight-0.4 checker is
        # bit-identical to a weight-1 checker — the confirm path cannot pick
        # the weight up even by accident.
        baseline = self.check(default_checker, book, armed=False)
        assert [breach_key(b) for b in unarmed] == [breach_key(b) for b in baseline]

    def test_reservation_decisions_bit_identical_armed_vs_not(self) -> None:
        def build_service(weight: Fraction) -> tuple[
            RiskReservationService, ExposureBook
        ]:
            book = self.blend_book()
            service = RiskReservationService(
                exposure=book,
                limits=LimitChecker(limits_with_weight(weight)),
                breach_splitter=lambda bs: [b for b in bs if not b.shadow],
            )
            return service, book

        candidate = position("fill", "G1", contracts=400, price=1_000)  # 4_000cc
        svc_armed, _ = build_service(W40)
        svc_plain, _ = build_service(Fraction(1))
        r_armed = svc_armed.try_reserve(
            "r1", candidate, marginals=provider(), daily_pnl=DailyPnl(),
            risk_bankroll_cc=self.BANKROLL,
        )
        r_plain = svc_plain.try_reserve(
            "r1", candidate, marginals=provider(), daily_pnl=DailyPnl(),
            risk_bankroll_cc=self.BANKROLL,
        )
        assert r_armed.granted == r_plain.granted
        assert [breach_key(b) for b in r_armed.breaches] == [
            breach_key(b) for b in r_plain.breaches
        ]


# ---------------------------------------------------- E2 rewrite (spec point 4)


@st.composite
def e2_cases(
    draw: st.DrawFn,
) -> tuple[list[OpenQuoteRisk], list[tuple[int, int]], Fraction, int]:
    def draw_legs() -> tuple[LegRef, ...]:
        tickers = draw(
            st.lists(st.sampled_from(TICKERS), min_size=1, max_size=3, unique=True)
        )
        return tuple(
            LegRef(t, EVENT_OF[t], draw(st.sampled_from(("yes", "no"))))
            for t in tickers
        )

    n = draw(st.integers(min_value=1, max_value=6))
    quotes = [
        OpenQuoteRisk(
            quote_id=f"q{i}",
            rfq_id=f"rfq{i}",
            combo_ticker=f"COMBO-q{i}",
            collection=None,
            yes_bid_cc=CC(draw(st.sampled_from((0, 2_000, 5_000)))),
            no_bid_cc=CC(draw(st.sampled_from((2_000, 5_000, 8_000)))),
            contracts=Q(draw(st.sampled_from((100, 200, 400)))),
            legs=draw_legs(),
        )
        for i in range(n)
    ]
    # Accept plan: (quote index, side choice) in an arbitrary order/subset.
    order = draw(st.permutations(range(n)))
    k = draw(st.integers(min_value=0, max_value=n))
    accepts = [(i, draw(st.integers(0, 1))) for i in list(order)[:k]]
    weight = draw(st.sampled_from((Fraction(1, 10), Fraction(2, 5))))
    floor = draw(st.integers(min_value=1, max_value=3))
    return quotes, accepts, weight, floor


class TestE2SerialConfirmBudget:
    """THE NEW INVARIANT (replaces the old quote-time dominance): resting
    quotes are admitted under the HAIRCUT quote-time check — deliberately more
    than the old 100% fold would admit — and for ANY accept subset in ANY
    order, the serial confirm path (provisional reservations + analytic caps
    at 100%) never commits positions whose realized consumption exceeds the
    budgets; the excess accepts are DECLINED at confirm."""

    BANKROLL = 1_000_000  # cc
    LIMITS = RiskLimits(
        game_loss_frac=Fraction(8, 100),        # 80_000cc per game
        directional_frac=Fraction(10, 100),     # 100_000cc
        max_gross_notional_dollars=50.0,        # 500_000cc premium
        max_event_worst_case_loss_dollars=20.0,  # 200_000cc hard per game
        absolute_notional_multiple=3,           # 3_000_000cc utilization
        resting_quote_weight=Fraction(2, 5),
        resting_floor_count=1,
    )

    @given(case=e2_cases())
    @settings(derandomize=True, max_examples=300, deadline=None)
    def test_confirm_path_never_exceeds_budgets(
        self,
        case: tuple[list[OpenQuoteRisk], list[tuple[int, int]], Fraction, int],
    ) -> None:
        quotes, accepts, weight, floor = case
        limits = RiskLimits(
            game_loss_frac=self.LIMITS.game_loss_frac,
            directional_frac=self.LIMITS.directional_frac,
            # Wide per-combo cap: this test is about the AGGREGATE budgets
            # (the per-combo cap is candidate-only and orthogonal here).
            per_combo_loss_frac=Fraction(50, 100),
            max_gross_notional_dollars=self.LIMITS.max_gross_notional_dollars,
            max_event_worst_case_loss_dollars=(
                self.LIMITS.max_event_worst_case_loss_dollars
            ),
            absolute_notional_multiple=self.LIMITS.absolute_notional_multiple,
            resting_quote_weight=weight,
            resting_floor_count=floor,
        )
        checker = LimitChecker(limits)
        book = ExposureBook(CONVENTIONS)
        service = RiskReservationService(
            exposure=book,
            limits=checker,
            breach_splitter=lambda bs: [b for b in bs if not b.shadow],
        )
        pnl = DailyPnl()

        # QUOTE TIME: admit each quote under the HAIRCUT check (armed).
        admitted: dict[str, OpenQuoteRisk] = {}
        for q in quotes:
            breaches = checker.check(
                book, _marg, pnl,
                candidate_positions=q.hypothetical_positions(CONVENTIONS),
                adding_quote=True,
                risk_bankroll_cc=self.BANKROLL,
                apply_resting_haircut=True,
            )
            if not [b for b in breaches if not b.shadow]:
                book.upsert_quote(q)
                admitted[q.quote_id] = q

        # CONFIRM TIME: serial accepts (any subset, any order). The quote is
        # still resting during its own reserve (the lifecycle's conservative
        # double count), dropped after — granted or declined.
        for idx, side_choice in accepts:
            q = quotes[idx]
            if q.quote_id not in admitted:
                continue
            hypos = q.hypothetical_positions(CONVENTIONS)
            if not hypos:
                book.remove_quote(q.quote_id)
                continue
            candidate = hypos[side_choice % len(hypos)]
            service.try_reserve(
                f"fill:{q.quote_id}", candidate,
                marginals=_marg, daily_pnl=pnl,
                risk_bankroll_cc=self.BANKROLL,
            )
            # Granted => commit books it; denied => nothing held (the decline).
            service.commit(f"fill:{q.quote_id}")
            book.remove_quote(q.quote_id)

            # THE INVARIANT: realized committed consumption within budgets.
            committed = book.snapshot(_marg, mass_acceptance=False)
            game_thr = threshold_cc(limits.game_loss_frac, self.BANKROLL)
            dir_thr = threshold_cc(limits.directional_frac, self.BANKROLL)
            hard_cc = int(limits.max_event_worst_case_loss_dollars * 10_000)
            assert committed.gross_notional_cc <= int(
                limits.max_gross_notional_dollars * 10_000
            )
            total_notional = sum(
                committed.gross_settlement_notional_by_game_cc.values()
            )
            assert total_notional <= (
                limits.absolute_notional_multiple * self.BANKROLL
            )
            for game, loss in committed.worst_case_loss_by_game_cc.items():
                assert loss <= game_thr, f"game {game} loss {loss} > {game_thr}"
                assert loss <= hard_cc
            for game, d in committed.directional_by_game_cc.items():
                assert d <= dir_thr, f"game {game} directional {d} > {dir_thr}"

    def test_excess_accepts_get_declined_at_confirm_not_vacuous(self) -> None:
        """Deterministic demonstration: the haircut admits MORE resting mass
        than the budgets can absorb; the serial confirm path commits some and
        DECLINES the excess — realized consumption stays within budget."""
        limits = RiskLimits(
            game_loss_frac=Fraction(8, 100),
            per_combo_loss_frac=Fraction(50, 100),  # candidate-only, orthogonal
            directional_frac=Fraction(1),           # isolate the game-loss axis
            resting_quote_weight=Fraction(1, 10),
            resting_floor_count=1,
        )
        checker = LimitChecker(limits)
        book = ExposureBook(CONVENTIONS)
        service = RiskReservationService(
            exposure=book,
            limits=checker,
            breach_splitter=lambda bs: [b for b in bs if not b.shadow],
        )
        bankroll = 1_000_000  # game budget 80_000cc
        pnl = DailyPnl()
        # Each quote is a 30_000cc worst-case loss on G1. At weight 0.1 /
        # floor 1 the quote-time fold admits all four (fold after 4 resting:
        # min(120_000, max(12_000, 30_000)) = 30_000 <= 80_000). The OLD 100%
        # fold would have stopped at two — this violates the OLD E2 dominance
        # by design.
        for i in range(4):
            q = quote(f"q{i}", "G1", contracts=1000, no_bid=3_000)
            breaches = checker.check(
                book, provider(), pnl,
                candidate_positions=q.hypothetical_positions(CONVENTIONS),
                adding_quote=True,
                risk_bankroll_cc=bankroll,
                apply_resting_haircut=True,
            )
            assert not [b for b in breaches if not b.shadow], f"q{i} not admitted"
            book.upsert_quote(q)

        committed_count = 0
        for i in range(4):
            q = book.open_quotes[f"q{i}"]
            candidate = q.hypothetical_positions(CONVENTIONS)[0]
            result = service.try_reserve(
                f"fill:q{i}", candidate,
                marginals=provider(), daily_pnl=pnl,
                risk_bankroll_cc=bankroll,
            )
            if result.granted:
                service.commit(f"fill:q{i}")
                committed_count += 1
            book.remove_quote(f"q{i}")
        committed = book.snapshot(provider(), mass_acceptance=False)
        game_thr = threshold_cc(limits.game_loss_frac, bankroll)
        assert committed.worst_case_loss_by_game_cc["G1"] <= game_thr
        # Some accepted, some declined — the confirm path did the enforcing.
        assert 1 <= committed_count < 4


# ---------------------------------------------------- config wiring (defaults)


def test_config_defaults_and_wiring() -> None:
    cfg = RiskConfig()
    assert cfg.resting_quote_weight == "1.0"
    assert cfg.resting_floor_count == 3
    limits = cfg.to_risk_limits()
    assert limits.resting_quote_weight == Fraction(1)
    assert limits.resting_floor_count == 3
    armed = RiskConfig(resting_quote_weight="0.40").to_risk_limits()
    assert armed.resting_quote_weight == Fraction(2, 5)


def test_config_rejects_bad_weight_and_floor() -> None:
    with pytest.raises(ValueError):
        RiskConfig(resting_quote_weight="0")
    with pytest.raises(ValueError):
        RiskConfig(resting_quote_weight="1.5")
    with pytest.raises(ValueError):
        RiskConfig(resting_quote_weight="nan")
    with pytest.raises(ValueError):
        RiskConfig(resting_floor_count=0)


# ------------------------------------------- post-fill risk pull (spec point 3)


async def make_rig(
    tmp_path: Path, *, name: str = "hc.sqlite3", weight: Fraction = W40,
    floor_count: int = 1, candidate_gate: bool = True,
) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / name, h.clock)
    return Rig(
        h,
        store,
        risk_limits=RiskLimits(
            game_loss_frac=Fraction(8, 100),
            # The pull tests isolate the GAME-LOSS axis: a wide directional
            # budget keeps the committed test position's own directional
            # magnitude (an independence-proxy figure) from co-breaching G1;
            # wide per-combo/slate budgets keep those orthogonal caps out of
            # the confirm-path ordering test the same way (E2-test pattern).
            directional_frac=Fraction(1),
            per_combo_loss_frac=Fraction(50, 100),
            slate_loss_frac=Fraction(1),
            resting_quote_weight=weight,
            resting_floor_count=floor_count,
        ),
        lifecycle_config=LifecycleConfig(
            quote_ttl_s=30.0,
            reprice_threshold_cc=100,
            candidate_gate_enabled=candidate_gate,
        ),
    )


def arm_bankroll(rig: Rig, bankroll_cc: int) -> None:
    rig.lifecycle._risk_bankroll_cc = lambda: bankroll_cc  # type: ignore[method-assign]  # noqa: SLF001
    rig.lifecycle._bankroll_source_configured = lambda: True  # type: ignore[method-assign]  # noqa: SLF001


def seed_resting_quote(
    rig: Rig, qid: str, game: str, *, contracts: int, no_bid: int,
    accepted: bool = False,
) -> None:
    """A resting quote visible to BOTH the exposure book (the fold) and the
    lifecycle's _open map (the eviction victim scan)."""
    q = quote(qid, game, contracts=contracts, no_bid=no_bid)
    rig.exposure.upsert_quote(q)
    state = OpenQuoteState(
        quote_id=qid,
        rfq=combo(CROSS_EVENT_LEGS, id=f"rfq-{qid}"),
        constructed=None,  # type: ignore[arg-type]  # never read by the pull
        leg_mids_cc={},
        created_mono_ns=0,
        accepted=accepted,
    )
    rig.lifecycle._open[qid] = state  # noqa: SLF001


async def test_pull_deletes_largest_breaching_quote_and_stops(
    tmp_path: Path,
) -> None:
    rig = await make_rig(tmp_path)
    arm_bankroll(rig, 1_000_000)  # game budget 80_000cc
    # Committed 70_000cc on G1 + resting 30_000 and 8_000: haircut fold
    # 100_000 > 80_000 breaches; deleting the 30_000 quote clears (78_000).
    rig.exposure.add_position(position("p1", "G1", contracts=2000, price=3_500))
    seed_resting_quote(rig, "qbig", "G1", contracts=1000, no_bid=3_000)
    seed_resting_quote(rig, "qsmall", "G1", contracts=800, no_bid=1_000)
    marg = rig.lifecycle._marginals  # noqa: SLF001
    rig.lifecycle._marginals = provider()  # type: ignore[method-assign]  # noqa: SLF001
    try:
        await rig.lifecycle._risk_evict_after_fill()  # noqa: SLF001
    finally:
        rig.lifecycle._marginals = marg  # type: ignore[method-assign]  # noqa: SLF001
    assert rig.sender.deleted == ["qbig"]           # largest first, then clean
    assert "qsmall" in rig.exposure.open_quotes     # survivor
    assert rig.metrics.counter("risk_evict.on_fill") == 1
    assert rig.metrics.counter(
        f"quote.deleted.{ReasonCode.DELETE_RISK_EVICTED_ON_FILL}"
    ) == 1


async def test_pull_never_yanks_accepted_quotes(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    arm_bankroll(rig, 1_000_000)
    rig.exposure.add_position(position("p1", "G1", contracts=2000, price=3_500))
    seed_resting_quote(rig, "qbig", "G1", contracts=1000, no_bid=3_000, accepted=True)
    rig.lifecycle._marginals = provider()  # type: ignore[method-assign]  # noqa: SLF001
    await rig.lifecycle._risk_evict_after_fill()  # noqa: SLF001
    assert rig.sender.deleted == []          # mid-confirm quote left alone
    assert rig.metrics.counter("risk_evict.on_fill") == 0


async def test_pull_prioritizes_fill_game(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    arm_bankroll(rig, 1_000_000)
    # Both games breach; the fill was on G2 — its quote goes first even though
    # the G1 quote is larger.
    rig.exposure.add_position(position("p1", "G1", contracts=2000, price=3_500))
    rig.exposure.add_position(position("p2", "G2", contracts=2000, price=3_500))
    seed_resting_quote(rig, "qg1", "G1", contracts=1200, no_bid=3_000)  # 36_000
    seed_resting_quote(rig, "qg2", "G2", contracts=1000, no_bid=3_000)  # 30_000
    rig.lifecycle._risk_evict_pending_games.add("G2")  # noqa: SLF001
    rig.lifecycle._marginals = provider()  # type: ignore[method-assign]  # noqa: SLF001
    await rig.lifecycle._risk_evict_after_fill()  # noqa: SLF001
    assert rig.sender.deleted[0] == "qg2"    # same-game-as-fill first
    assert set(rig.sender.deleted) == {"qg1", "qg2"}


async def test_pull_errors_fail_safe(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    arm_bankroll(rig, 1_000_000)
    rig.exposure.add_position(position("p1", "G1", contracts=2000, price=3_500))
    seed_resting_quote(rig, "qbig", "G1", contracts=1000, no_bid=3_000)

    async def boom(_qid: str, _reason: ReasonCode) -> None:
        raise RuntimeError("delete boom")

    rig.lifecycle._delete_quote = boom  # type: ignore[method-assign]  # noqa: SLF001
    rig.lifecycle._marginals = provider()  # type: ignore[method-assign]  # noqa: SLF001
    await rig.lifecycle._risk_evict_after_fill()  # noqa: SLF001  (must not raise)
    assert rig.metrics.counter("risk_evict.pass_error") == 1
    assert "qbig" in rig.exposure.open_quotes  # nothing lost; backstops own it


async def test_pull_disarmed_at_weight_one_never_schedules(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, weight=Fraction(1))
    state = OpenQuoteState(
        quote_id="qx",
        rfq=combo(CROSS_EVENT_LEGS, id="rfq-qx"),
        constructed=None,  # type: ignore[arg-type]
        leg_mids_cc={},
        created_mono_ns=0,
    )
    rig.lifecycle._schedule_risk_evict_on_fill(state)  # noqa: SLF001
    assert rig.lifecycle._risk_evict_task is None  # noqa: SLF001


async def test_pull_armed_schedules_and_completes_clean(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path)
    arm_bankroll(rig, 1_000_000)
    state = OpenQuoteState(
        quote_id="qx",
        rfq=combo(CROSS_EVENT_LEGS, id="rfq-qx"),
        constructed=None,  # type: ignore[arg-type]
        leg_mids_cc={},
        created_mono_ns=0,
    )
    rig.lifecycle._schedule_risk_evict_on_fill(state)  # noqa: SLF001
    task = rig.lifecycle._risk_evict_task  # noqa: SLF001
    assert task is not None
    await task
    assert rig.metrics.counter("risk_evict.pass_error") == 0
    assert rig.sender.deleted == []          # clean book: nothing evicted


async def test_pull_scheduled_only_after_filled_quote_dropped(
    tmp_path: Path,
) -> None:
    """END-TO-END REGRESSION (2026-07-17 finding): the confirm-path pull must
    be scheduled AFTER ``_drop_quote``. Scheduling it between the reservation
    commit and the drop let the pull's first ``limits.check`` (which runs at
    the awaited confirm-decision record — aiosqlite always yields) see the
    fill DOUBLE-counted — booked position AND its own still-resting quote —
    and evict an innocent same-game resting quote on the transient breach.

    The race, driven at its natural seam: an innocent quote lands during the
    confirm ROUND TRIP (after the last-look caps admitted the fill, before
    commit+drop). The game budget sits BETWEEN the transient fold
    (fill position + own quote + innocent) and both real folds (last-look:
    fill + own quote; post-drop: fill + innocent), so the old call site
    deletes the innocent quote and the fixed one deletes nothing."""
    rig = await make_rig(
        tmp_path, name="pull-order.sqlite3", floor_count=3, candidate_gate=False
    )
    arm_bankroll(rig, 10_000_000)  # wide while the quote goes out
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert "q1" in rig.exposure.open_quotes
    hypos = rig.exposure.open_quotes["q1"].hypothetical_positions(CONVENTIONS)
    fill_loss = next(h.max_loss_cc for h in hypos if h.our_side is Side.YES)
    worst_loss = max(h.max_loss_cc for h in hypos)
    assert worst_loss >= fill_loss > 0

    def seed_innocent_quote() -> None:
        # Innocent resting quote on the fill's game (E1), 30_000cc worst loss.
        # Its leg rides the REAL seeded market M1 so the lifecycle's live
        # marginal reader prices the whole book: the transient-window check
        # (the OLD call site's first run) must be a REAL cap verdict, never an
        # unknown-marginals fail-closed no-op.
        rig.exposure.upsert_quote(
            OpenQuoteRisk(
                quote_id="qinn",
                rfq_id="rfq-qinn",
                combo_ticker="COMBO-qinn",
                collection=None,
                yes_bid_cc=CC(0),
                no_bid_cc=CC(3_000),
                contracts=Q(1000),
                legs=(LegRef("M1", "E1", "yes"),),
            )
        )
        rig.lifecycle._open["qinn"] = OpenQuoteState(  # noqa: SLF001
            quote_id="qinn",
            rfq=combo(CROSS_EVENT_LEGS, id="rfq-qinn"),
            constructed=None,  # type: ignore[arg-type]  # never read by the pull
            leg_mids_cc={},
            created_mono_ns=0,
            accepted=False,
        )

    orig_confirm = rig.sender.confirm_quote

    async def confirm_then_concurrent_quote(quote_id: str) -> Any:
        result = await orig_confirm(quote_id)
        # The concurrent admit during the confirm RTT — the book gains the
        # innocent quote after the last-look caps ran, before commit+drop.
        seed_innocent_quote()
        return result

    rig.sender.confirm_quote = confirm_then_concurrent_quote  # type: ignore[method-assign]

    # Budget: last-look fold (fill + own quote) passes; the transient fold
    # (+30_000 innocent) breaches; the post-drop fold (fill + innocent, own
    # quote gone) passes. Floor 3 covers every resting quote, so all three
    # folds are plain comonotone sums — hand-checkable.
    target_thr = fill_loss + worst_loss + 15_000
    bankroll = target_thr * 25 // 2 + 13
    thr = threshold_cc(Fraction(8, 100), bankroll)
    assert fill_loss + worst_loss <= thr < fill_loss + worst_loss + 30_000
    assert fill_loss + 30_000 <= thr  # post-drop book is clean
    arm_bankroll(rig, bankroll)

    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == ["q1"]
    assert "q1" not in rig.exposure.open_quotes   # dropped before the pull ran
    task = rig.lifecycle._risk_evict_task  # noqa: SLF001
    assert task is not None                       # the fill DID schedule a pull
    await task
    # Post-drop book: fill position + innocent quote <= thr — clean. The
    # pre-drop call site saw fill + own quote + innocent > thr and deleted
    # the innocent quote.
    assert rig.sender.deleted == []
    assert "qinn" in rig.exposure.open_quotes
    assert rig.metrics.counter("risk_evict.on_fill") == 0


async def test_declined_confirm_never_schedules_pull(tmp_path: Path) -> None:
    """The post-drop schedule is gated on ``fill_confirmed_mono_ns`` (stamped
    only on confirm-send success): a DECLINED confirm books nothing and must
    not trigger the pull even with the haircut armed."""
    rig = await make_rig(tmp_path, name="pull-decline.sqlite3", floor_count=3)
    arm_bankroll(rig, 10_000_000)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    assert "q1" in rig.exposure.open_quotes
    await rig.killswitch.halt(ReasonCode.HALT_MANUAL)
    await rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert rig.sender.confirmed == []             # deliberate lapse
    # The decline path DID run (not an unknown-quote early return) ...
    assert rig.metrics.counter(
        f"confirm.declined.{ReasonCode.DECLINE_KILL_SWITCH}"
    ) == 1
    # ... and the stamp-gated schedule stayed silent.
    assert rig.lifecycle._risk_evict_task is None  # noqa: SLF001
