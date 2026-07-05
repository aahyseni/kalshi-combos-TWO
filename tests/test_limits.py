"""Tests for combomaker.risk.limits — every limit independently produces its
breach, check() returns ALL breaches (never just the first), the
mass-acceptance worst case is enforced with no candidate at all (the
stop-issuing-quotes condition), and the daily-loss boundary is inclusive.
"""

from __future__ import annotations

from collections.abc import Callable

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, OpenQuoteRisk
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits

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

MARGINALS = {"A": 0.5, "B": 0.5}


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


MARG = provider(MARGINALS)

LEG_A = (LegRef("A", "EV1", "yes"),)
LEG_B = (LegRef("B", "EV1", "yes"),)


def make_position(
    pid: str,
    legs: tuple[LegRef, ...] = LEG_A,
    *,
    our_side: Side = Side.YES,
    contracts: int = 100,
    entry_price: int = 5_000,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=our_side,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=legs,
    )


def make_quote(
    qid: str,
    legs: tuple[LegRef, ...] = LEG_A,
    *,
    yes_bid: int = 2_000,
    no_bid: int = 2_000,
    contracts: int = 1_000,
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


def empty_book() -> ExposureBook:
    return ExposureBook(CONVENTIONS)


def reasons(breaches: list) -> list[ReasonCode]:
    return [b.reason for b in breaches]


class TestEachLimitIndependently:
    def test_per_quote_contracts(self) -> None:
        # 150 contracts > 100 cap; price tiny so nothing else trips.
        candidate = make_position("cand", contracts=15_000, entry_price=100)
        breaches = LimitChecker(RiskLimits()).check(
            empty_book(), MARG, DailyPnl(), candidate_positions=[candidate]
        )
        assert len(breaches) == 1
        assert breaches[0].reason is ReasonCode.SKIP_SIZE_ABOVE_MAX
        assert "contracts" in breaches[0].detail

    def test_per_quote_notional(self) -> None:
        # 100 contracts at $0.90 = $90 notional > $50 cap; contracts at cap pass.
        limits = RiskLimits(max_notional_per_quote_dollars=50.0)
        candidate = make_position("cand", contracts=10_000, entry_price=9_000)
        breaches = LimitChecker(limits).check(
            empty_book(), MARG, DailyPnl(), candidate_positions=[candidate]
        )
        assert len(breaches) == 1
        assert breaches[0].reason is ReasonCode.SKIP_SIZE_ABOVE_MAX
        assert "notional" in breaches[0].detail

    def test_max_open_quotes_at_cap_when_adding(self) -> None:
        limits = RiskLimits(max_open_quotes=2)
        book = empty_book()
        book.upsert_quote(make_quote("q1"))
        book.upsert_quote(make_quote("q2"))
        breaches = LimitChecker(limits).check(book, MARG, DailyPnl(), adding_quote=True)
        assert reasons(breaches) == [ReasonCode.SKIP_MAX_OPEN_QUOTES]
        # Not adding a quote: sitting at the cap is fine.
        assert LimitChecker(limits).check(book, MARG, DailyPnl(), adding_quote=False) == []

    def test_max_open_quotes_below_cap_passes(self) -> None:
        limits = RiskLimits(max_open_quotes=2)
        book = empty_book()
        book.upsert_quote(make_quote("q1"))
        assert LimitChecker(limits).check(book, MARG, DailyPnl(), adding_quote=True) == []

    def test_market_delta(self) -> None:
        # 400 contracts on one leg -> market delta 400 > 300; $4 notional.
        book = empty_book()
        book.add_position(make_position("p1", contracts=40_000, entry_price=100))
        breaches = LimitChecker(RiskLimits()).check(book, MARG, DailyPnl())
        assert len(breaches) == 1
        assert breaches[0].reason is ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH
        assert "market A" in breaches[0].detail

    def test_market_delta_negative_direction_also_breaches(self) -> None:
        book = empty_book()
        book.add_position(
            make_position("p1", our_side=Side.NO, contracts=40_000, entry_price=100)
        )
        breaches = LimitChecker(RiskLimits()).check(book, MARG, DailyPnl())
        assert reasons(breaches) == [ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH]
        assert "market A" in breaches[0].detail

    def test_event_delta(self) -> None:
        # Two markets in one event: 280 + 280 = 560 > 500, each market <= 300.
        book = empty_book()
        book.add_position(make_position("p1", LEG_A, contracts=28_000, entry_price=100))
        book.add_position(make_position("p2", LEG_B, contracts=28_000, entry_price=100))
        breaches = LimitChecker(RiskLimits()).check(book, MARG, DailyPnl())
        assert len(breaches) == 1
        assert breaches[0].reason is ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH
        assert "event EV1 delta" in breaches[0].detail

    def test_gross_notional(self) -> None:
        # 100 contracts at $0.50 = $50 gross > $10 cap; deltas fine.
        limits = RiskLimits(max_gross_notional_dollars=10.0)
        book = empty_book()
        book.add_position(make_position("p1", contracts=10_000, entry_price=5_000))
        breaches = LimitChecker(limits).check(book, MARG, DailyPnl())
        assert len(breaches) == 1
        assert breaches[0].reason is ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH
        assert "gross notional" in breaches[0].detail

    def test_event_worst_case_loss(self) -> None:
        limits = RiskLimits(max_event_worst_case_loss_dollars=10.0)
        book = empty_book()
        book.add_position(make_position("p1", contracts=10_000, entry_price=5_000))
        breaches = LimitChecker(limits).check(book, MARG, DailyPnl())
        assert len(breaches) == 1
        assert breaches[0].reason is ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH
        assert "worst-case loss" in breaches[0].detail

    def test_unknown_marginals(self) -> None:
        book = empty_book()
        book.add_position(
            make_position("p1", (LegRef("ZZZ", "EV9", "yes"),), contracts=1_000, entry_price=100)
        )
        breaches = LimitChecker(RiskLimits()).check(book, MARG, DailyPnl())
        assert reasons(breaches) == [ReasonCode.SKIP_CLASSIFIER_UNKNOWN]


class TestDailyLoss:
    def test_exactly_at_limit_breaches(self) -> None:
        # -$500.00 with the default $500 limit: source uses >= (inclusive).
        pnl = DailyPnl(realized_cc=-5_000_000)
        breaches = LimitChecker(RiskLimits()).check(empty_book(), MARG, pnl)
        assert reasons(breaches) == [ReasonCode.HALT_DAILY_LOSS]

    def test_just_under_limit_passes(self) -> None:
        pnl = DailyPnl(realized_cc=-4_999_999)  # -$499.9999
        assert LimitChecker(RiskLimits()).check(empty_book(), MARG, pnl) == []

    def test_realized_plus_unrealized_combine(self) -> None:
        pnl = DailyPnl(realized_cc=-2_000_000, unrealized_cc=-3_000_000)
        breaches = LimitChecker(RiskLimits()).check(empty_book(), MARG, pnl)
        assert reasons(breaches) == [ReasonCode.HALT_DAILY_LOSS]

    def test_unrealized_gains_offset_realized_losses(self) -> None:
        pnl = DailyPnl(realized_cc=-6_000_000, unrealized_cc=2_500_000)  # -$350 net
        assert LimitChecker(RiskLimits()).check(empty_book(), MARG, pnl) == []


class TestCleanBook:
    def test_small_candidate_no_breaches(self) -> None:
        book = empty_book()
        book.add_position(make_position("p1", contracts=1_000, entry_price=5_000))  # $5
        book.upsert_quote(make_quote("q1", contracts=1_000))
        candidate = make_position("cand", LEG_B, contracts=1_000, entry_price=4_000)  # $4
        breaches = LimitChecker(RiskLimits()).check(
            book,
            MARG,
            DailyPnl(realized_cc=-1_000_000),  # -$100, well under $500
            candidate_positions=[candidate],
            adding_quote=True,
        )
        assert breaches == []


class TestAllBreachesReported:
    def test_pathological_case_reports_every_reason(self) -> None:
        limits = RiskLimits(
            max_contracts_per_quote=10.0,
            max_notional_per_quote_dollars=1.0,
            max_market_delta_contracts=1.0,
            max_event_delta_contracts=1.0,
            max_gross_notional_dollars=1.0,
            max_open_quotes=0,
            max_daily_loss_dollars=10.0,
            max_event_worst_case_loss_dollars=0.5,
        )
        book = empty_book()
        # Book position: delta 5 > 1 (market AND event), $25 > $1 gross,
        # event worst-case $25 > $0.50.
        book.add_position(make_position("p1", contracts=500, entry_price=5_000))
        # Candidate: 50 contracts > 10, $25 > $1 notional, unknown marginal leg.
        candidate = make_position(
            "cand", (LegRef("MISSING", "EV1", "yes"),), contracts=5_000, entry_price=5_000
        )
        breaches = LimitChecker(limits).check(
            book,
            MARG,
            DailyPnl(realized_cc=-200_000),  # -$20 >= $10 limit
            candidate_positions=[candidate],
            adding_quote=True,  # 0 open quotes but cap is 0
        )
        seen = set(reasons(breaches))
        assert seen == {
            ReasonCode.SKIP_SIZE_ABOVE_MAX,
            ReasonCode.SKIP_MAX_OPEN_QUOTES,
            ReasonCode.SKIP_CLASSIFIER_UNKNOWN,
            ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
            ReasonCode.HALT_DAILY_LOSS,
        }
        assert len(seen) >= 3  # spec floor: several distinct reasons at once
        # Both per-quote size breaches AND several mass breaches are present.
        assert reasons(breaches).count(ReasonCode.SKIP_SIZE_ABOVE_MAX) == 2
        assert reasons(breaches).count(ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH) >= 3


class TestMassAcceptanceEnforcement:
    def test_worst_case_breach_with_no_candidate_stops_quoting(self) -> None:
        # One open quote: 100 contracts, both sides at $0.90 -> worst-case fill
        # costs $90. Current book exposure is ZERO — nothing has filled.
        limits = RiskLimits(max_gross_notional_dollars=50.0)
        book = empty_book()
        book.upsert_quote(make_quote("q1", yes_bid=9_000, no_bid=9_000, contracts=10_000))

        current = book.snapshot(MARG, mass_acceptance=False)
        assert current.gross_notional_cc == 0  # not a current-exposure breach

        breaches = LimitChecker(limits).check(book, MARG, DailyPnl())
        assert reasons(breaches) == [ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH]
        assert "gross notional" in breaches[0].detail

    def test_same_book_passes_under_default_gross_limit(self) -> None:
        book = empty_book()
        book.upsert_quote(make_quote("q1", yes_bid=9_000, no_bid=9_000, contracts=10_000))
        assert LimitChecker(RiskLimits()).check(book, MARG, DailyPnl()) == []
