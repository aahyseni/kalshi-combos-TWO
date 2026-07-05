"""Tests for combomaker.pricing.devig — deterministic, no network."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import pytest

from combomaker.pricing.devig import (
    DevigMethod,
    devig,
    devig_multiplicative,
    devig_power,
    devig_shin,
    implied_from_decimal_odds,
)

DevigFn = Callable[[Sequence[float]], list[float]]

ALL_METHOD_FNS: list[DevigFn] = [devig_multiplicative, devig_power, devig_shin]
METHOD_IDS = ["multiplicative", "power", "shin"]

# Hardcoded overround books (order intentionally strictly decreasing so the
# order-preservation check below is meaningful).
BOOKS: list[list[float]] = [
    [0.62, 0.47],  # sum 1.09
    [0.55, 0.33, 0.18],  # sum 1.06
    [0.28, 0.22, 0.17, 0.13, 0.10, 0.08, 0.05, 0.03],  # sum 1.06
    [0.99, 0.50, 0.30],  # heavy vig, sum 1.79
]


# ---------------------------------------------------------------------------
# implied_from_decimal_odds
# ---------------------------------------------------------------------------


def test_implied_from_decimal_odds_basic() -> None:
    assert implied_from_decimal_odds([2.0, 4.0]) == [0.5, 0.25]


@pytest.mark.parametrize("bad", [1.0, 0.5, -3.0, 0.0, float("inf"), float("nan")])
def test_implied_from_decimal_odds_rejects(bad: float) -> None:
    with pytest.raises(ValueError):
        implied_from_decimal_odds([2.0, bad])


# ---------------------------------------------------------------------------
# Core properties shared by all three methods
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", ALL_METHOD_FNS, ids=METHOD_IDS)
def test_symmetric_two_way_book_is_exactly_half(fn: DevigFn) -> None:
    implied = implied_from_decimal_odds([1.91, 1.91])
    assert fn(implied) == [0.5, 0.5]


@pytest.mark.parametrize("fn", ALL_METHOD_FNS, ids=METHOD_IDS)
@pytest.mark.parametrize("book", BOOKS, ids=lambda b: f"n{len(b)}")
def test_sums_to_one_preserves_order_and_length(fn: DevigFn, book: list[float]) -> None:
    out = fn(book)
    assert len(out) == len(book)
    assert abs(math.fsum(out) - 1.0) < 1e-9
    assert all(0.0 < p < 1.0 for p in out)
    # Input books are strictly decreasing; fair probs must keep that order.
    assert all(out[i] > out[i + 1] for i in range(len(out) - 1))
    # Positional correspondence: reversing the input reverses the output.
    rev = fn(list(reversed(book)))
    assert rev == pytest.approx(list(reversed(out)), abs=1e-12)


@pytest.mark.parametrize("fn", ALL_METHOD_FNS, ids=METHOD_IDS)
@pytest.mark.parametrize(
    "fair_book",
    [[0.5, 0.5], [0.5, 0.3, 0.2], [0.25, 0.25, 0.25, 0.25]],
    ids=["even2", "mixed3", "even4"],
)
def test_fair_book_passes_through(fn: DevigFn, fair_book: list[float]) -> None:
    assert fn(fair_book) == pytest.approx(fair_book, abs=1e-9)


@pytest.mark.parametrize("fn", ALL_METHOD_FNS, ids=METHOD_IDS)
def test_underround_book_is_normalized(fn: DevigFn) -> None:
    # Arb/underround book (sum 0.9): every method must still return sum == 1.
    out = fn([0.40, 0.50])
    assert abs(math.fsum(out) - 1.0) < 1e-9
    assert out[1] > out[0]


def test_underround_shin_falls_back_to_multiplicative() -> None:
    book = [0.40, 0.50]
    assert devig_shin(book) == pytest.approx(devig_multiplicative(book), abs=1e-15)
    assert devig_multiplicative(book) == pytest.approx([4.0 / 9.0, 5.0 / 9.0], abs=1e-15)


# ---------------------------------------------------------------------------
# Method-defining invariants (implementation-independent)
# ---------------------------------------------------------------------------


def test_multiplicative_keeps_ratios() -> None:
    book = [0.55, 0.33, 0.18]
    out = devig_multiplicative(book)
    ratios = [o / p for o, p in zip(out, book, strict=True)]
    assert ratios == pytest.approx([ratios[0]] * len(ratios), abs=1e-12)


def test_power_output_is_common_exponent_of_input() -> None:
    book = [0.55, 0.33, 0.18]
    out = devig_power(book)
    ks = [math.log(o) / math.log(p) for o, p in zip(out, book, strict=True)]
    assert ks == pytest.approx([ks[0]] * len(ks), abs=1e-6)
    assert ks[0] > 1.0  # overround book -> k > 1


# ---------------------------------------------------------------------------
# Favorite–longshot shading
# ---------------------------------------------------------------------------


def test_power_and_shin_shade_the_longshot_vs_multiplicative() -> None:
    book = [0.80, 0.25]  # sum 1.05, heavy favorite vs longshot
    mult = devig_multiplicative(book)
    for fn in (devig_power, devig_shin):
        out = fn(book)
        assert out[0] > mult[0]  # favorite gets MORE probability
        assert out[1] < mult[1]  # longshot gets LESS probability
        assert abs(math.fsum(out) - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Shin: known worked example on a two-way book
# ---------------------------------------------------------------------------


def test_shin_known_worked_example_two_way() -> None:
    # Binary Shin has a closed form for the insider fraction z. With
    # B = pi1 + pi2 and d = pi1 - pi2, requiring the two Shin fair
    # probabilities p_i = (sqrt(z^2 + 4(1-z) pi_i^2 / B) - z) / (2(1-z))
    # to sum to 1 forces sqrt-term_1 = 1 + (1-z) d, and squaring gives
    #   z = (4 pi1^2 / B - (1 + d)^2) / ((1 + d) (1 - d)).
    pi1, pi2 = 0.60, 0.50
    b = pi1 + pi2
    d = pi1 - pi2
    z = (4.0 * pi1**2 / b - (1.0 + d) ** 2) / ((1.0 + d) * (1.0 - d))
    assert 0.0 < z < 1.0
    assert z == pytest.approx(0.0990909090909091 / 0.99, abs=1e-12)  # hand arithmetic

    def shin_fair(pi: float) -> float:
        return (math.sqrt(z**2 + 4.0 * (1.0 - z) * pi**2 / b) - z) / (2.0 * (1.0 - z))

    expected = [shin_fair(pi1), shin_fair(pi2)]
    # Sanity check of the hand-derived z: the textbook formula sums to 1.
    assert math.fsum(expected) == pytest.approx(1.0, abs=1e-12)
    # This book works out to clean values: favorite 0.55, longshot 0.45.
    assert expected == pytest.approx([0.55, 0.45], abs=1e-12)

    assert devig_shin([pi1, pi2]) == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", ALL_METHOD_FNS, ids=METHOD_IDS)
@pytest.mark.parametrize(
    "bad_book",
    [
        [],
        [0.5],
        [0.0, 0.5],
        [1.0, 0.5],
        [-0.1, 0.5],
        [1.2, 0.3],
        [float("nan"), 0.5],
        [float("inf"), 0.5],
    ],
    ids=["empty", "single", "zero", "one", "negative", "above-one", "nan", "inf"],
)
def test_invalid_books_raise(fn: DevigFn, bad_book: list[float]) -> None:
    with pytest.raises(ValueError):
        fn(bad_book)


# ---------------------------------------------------------------------------
# devig() dispatcher
# ---------------------------------------------------------------------------


def test_devig_dispatch_matches_direct_calls() -> None:
    book = [0.55, 0.33, 0.18]
    assert devig(book) == devig_power(book)  # POWER is the default
    assert devig(book, DevigMethod.POWER) == devig_power(book)
    assert devig(book, DevigMethod.SHIN) == devig_shin(book)
    assert devig(book, DevigMethod.MULTIPLICATIVE) == devig_multiplicative(book)


def test_devig_passes_tuning_kwargs() -> None:
    book = [0.62, 0.47]
    out = devig(book, DevigMethod.SHIN, tol=1e-12, max_iter=100)
    assert out == pytest.approx(devig_shin(book, tol=1e-12, max_iter=100), abs=1e-15)
    # Multiplicative ignores tuning kwargs instead of choking on them.
    assert devig(book, DevigMethod.MULTIPLICATIVE, tol=1e-12) == devig_multiplicative(book)


def test_devig_rejects_unknown_kwargs() -> None:
    with pytest.raises(TypeError):
        devig([0.62, 0.47], DevigMethod.POWER, bogus=1.0)
