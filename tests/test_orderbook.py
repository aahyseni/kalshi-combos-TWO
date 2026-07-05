import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.marketdata.orderbook import BookInvalidError, OrderbookMirror

CC = CentiCents
Q = CentiContracts


def make_book() -> tuple[OrderbookMirror, FakeClock]:
    clock = FakeClock()
    book = OrderbookMirror("KXTEST-1", clock)
    # yes bids: $0.01 x 200, $0.42 x 13 ; no bids: $0.01 x 100, $0.56 x 17
    book.apply_snapshot(
        yes=[(CC(100), Q(20_000)), (CC(4_200), Q(1_300))],
        no=[(CC(100), Q(10_000)), (CC(5_600), Q(1_700))],
    )
    return book, clock


class TestDerivedPrices:
    def test_top_of_book_and_derived_ask(self) -> None:
        book, _ = make_book()
        top = book.top()
        assert top.yes_bid_cc == 4_200
        assert top.no_bid_cc == 5_600
        assert top.yes_ask_cc == 10_000 - 5_600  # 0.44: YES ask derived from NO bid
        assert top.spread_cc == 200
        assert top.mid_cc == 4_300

    def test_microprice_weights_toward_thin_side(self) -> None:
        book, _ = make_book()
        top = book.top()
        micro = top.microprice()
        assert micro is not None
        # ask side (no bids 1700 centi) heavier than bid side (1300 centi):
        # weight on bid price is ask_qty → micro leans toward the bid... check exact
        expected = (4_200 * 1_700 + 4_400 * 1_300) / 3_000 / 10_000
        assert micro == pytest.approx(expected)

    def test_empty_side_yields_none(self) -> None:
        clock = FakeClock()
        book = OrderbookMirror("T", clock)
        book.apply_snapshot(yes=[(CC(4_200), Q(1_300))], no=[])
        top = book.top()
        assert top.yes_bid_cc == 4_200
        assert top.no_bid_cc is None
        assert top.yes_ask_cc is None
        assert top.mid_cc is None
        assert top.microprice() is None


class TestDeltas:
    def test_add_and_remove_levels(self) -> None:
        book, _ = make_book()
        assert book.apply_delta("yes", CC(4_300), Q(500), ts_ms=1)
        assert book.top().yes_bid_cc == 4_300
        assert book.apply_delta("yes", CC(4_300), Q(-500), ts_ms=2)  # zero → level removed
        assert book.top().yes_bid_cc == 4_200

    def test_negative_count_invalidates(self) -> None:
        book, _ = make_book()
        assert not book.apply_delta("no", CC(5_600), Q(-1_800), ts_ms=3)
        assert not book.valid

    def test_delta_before_snapshot_ignored(self) -> None:
        book = OrderbookMirror("T", FakeClock())
        assert book.apply_delta("yes", CC(4_200), Q(100), ts_ms=1)  # ignored, no error
        assert not book.valid


class TestExecutable:
    def test_walk_multiple_levels(self) -> None:
        book, _ = make_book()
        # Buy 100.00 YES: lifts derived asks $0.44 (17.00 avail) then $0.99
        result = book.executable_buy("yes", Q(10_000))
        assert result is not None
        assert result.worst_price_cc == 9_900
        expected_cost = 1_700 * 4_400 + 8_300 * 9_900
        assert result.cost_micro_dollars == expected_cost

    def test_underfill_returns_none(self) -> None:
        book, _ = make_book()
        assert book.executable_buy("yes", Q(100_000_00)) is None

    def test_buy_no_side(self) -> None:
        book, _ = make_book()
        result = book.executable_buy("no", Q(1_000))
        assert result is not None
        assert result.worst_price_cc == 10_000 - 4_200  # lifts best YES bid


class TestValidityDiscipline:
    def test_reads_on_invalid_book_raise(self) -> None:
        book, _ = make_book()
        book.invalidate("test")
        with pytest.raises(BookInvalidError):
            book.top()
        with pytest.raises(BookInvalidError):
            book.executable_buy("yes", Q(100))

    def test_age_tracks_changes(self) -> None:
        book, clock = make_book()
        clock.advance(2.5)
        age = book.age_since_change_s()
        assert age == pytest.approx(2.5)
        book.apply_delta("yes", CC(4_200), Q(100), ts_ms=9)
        assert book.age_since_change_s() == pytest.approx(0.0)
