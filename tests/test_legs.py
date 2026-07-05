"""Tests for pricing/legs.py: KalshiBookSource, blend_beliefs, LegBelief."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from combomaker.core.clock import FakeClock
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.pricing.legs import KalshiBookSource, LegBelief, blend_beliefs
from tests.test_feed import FakeWs, snapshot_env

SID = 5

Levels = list[list[str]]


async def feed_with_books(books: dict[str, tuple[Levels, Levels]]) -> OrderbookFeed:
    """Real OrderbookFeed fed via FakeWs snapshots (yes levels, no levels)."""
    ws = FakeWs()
    feed = OrderbookFeed(ws, FakeClock())
    feed.watch(list(books))
    await ws.ack_subscription(0, SID)
    for i, (ticker, (yes, no)) in enumerate(books.items()):
        env = snapshot_env(SID, i + 1, ticker)
        env["msg"]["yes_dollars_fp"] = yes
        env["msg"]["no_dollars_fp"] = no
        await ws.deliver(env)
    return feed


# --- KalshiBookSource.marginal ---


async def test_marginal_returns_microprice_belief() -> None:
    # yes bid $0.40 x 30, no bid $0.50 x 10 => yes ask $0.50, spread $0.10.
    feed = await feed_with_books({"A": ([["0.4000", "30.00"]], [["0.5000", "10.00"]])})
    belief = KalshiBookSource(feed).marginal("A")
    assert belief is not None
    # Microprice weights toward the thin side: (0.40*10 + 0.50*30) / 40 = 0.475.
    assert belief.p == pytest.approx(0.475)
    # Half-spread in prob space; both sides at/above 10 contracts => no thin penalty.
    assert belief.uncertainty == pytest.approx(0.05)
    assert belief.source == "kalshi_book"


async def test_marginal_unknown_ticker_returns_none() -> None:
    feed = await feed_with_books({"A": ([["0.4000", "30.00"]], [["0.5000", "10.00"]])})
    assert KalshiBookSource(feed).marginal("NEVER-WATCHED") is None


async def test_marginal_invalidated_book_returns_none() -> None:
    feed = await feed_with_books({"A": ([["0.4000", "30.00"]], [["0.5000", "10.00"]])})
    feed.book("A").invalidate("test")
    assert KalshiBookSource(feed).marginal("A") is None


async def test_marginal_never_snapshotted_book_returns_none() -> None:
    ws = FakeWs()
    feed = OrderbookFeed(ws, FakeClock())
    feed.watch(["A"])
    await ws.ack_subscription(0, SID)  # subscribed but no snapshot yet
    assert KalshiBookSource(feed).marginal("A") is None


@pytest.mark.parametrize(
    ("yes", "no"),
    [
        ([], [["0.5000", "10.00"]]),  # no YES bids
        ([["0.4000", "30.00"]], []),  # no NO bids (no derived ask)
        ([], []),  # both empty
    ],
    ids=["empty-yes", "empty-no", "empty-both"],
)
async def test_marginal_empty_side_returns_none(yes: Levels, no: Levels) -> None:
    feed = await feed_with_books({"A": (yes, no)})
    assert feed.book("A").valid  # book itself is fine; the side is just empty
    assert KalshiBookSource(feed).marginal("A") is None


async def test_uncertainty_widens_with_spread() -> None:
    feed = await feed_with_books(
        {
            "TIGHT": ([["0.4800", "50.00"]], [["0.5000", "50.00"]]),  # spread $0.02
            "WIDE": ([["0.3000", "50.00"]], [["0.5000", "50.00"]]),  # spread $0.20
        }
    )
    source = KalshiBookSource(feed)
    tight = source.marginal("TIGHT")
    wide = source.marginal("WIDE")
    assert tight is not None and wide is not None
    assert wide.uncertainty > tight.uncertainty
    assert tight.uncertainty == pytest.approx(0.01)  # half of $0.02, deep both sides
    assert wide.uncertainty == pytest.approx(0.10)  # half of $0.20, deep both sides


async def test_thinness_adds_thin_penalty_exactly() -> None:
    # Identical prices; DEEP sits exactly AT the 10-contract default threshold
    # (strictly-less-than => not thin), THIN dips one centi-contract below.
    feed = await feed_with_books(
        {
            "DEEP": ([["0.4000", "30.00"]], [["0.5000", "10.00"]]),
            "THIN": ([["0.4000", "30.00"]], [["0.5000", "9.99"]]),
        }
    )
    source = KalshiBookSource(feed)
    deep = source.marginal("DEEP")
    thin = source.marginal("THIN")
    assert deep is not None and thin is not None
    assert deep.uncertainty == pytest.approx(0.05)
    assert thin.uncertainty == deep.uncertainty + 0.02  # default thin_penalty, exactly


async def test_thin_penalty_custom_params_one_thin_side_suffices() -> None:
    # yes side 49 < 50 threshold, no side 60 >= 50: thin if EITHER side is thin.
    feed = await feed_with_books({"A": ([["0.4000", "49.00"]], [["0.5000", "60.00"]])})
    source = KalshiBookSource(feed, thin_depth_contracts=50.0, thin_penalty=0.07)
    belief = source.marginal("A")
    assert belief is not None
    assert belief.uncertainty == pytest.approx(0.05 + 0.07)


# --- blend_beliefs ---


def test_blend_empty_list_returns_none() -> None:
    assert blend_beliefs([], max_disagreement=1.0) is None


def test_blend_single_source_passthrough() -> None:
    belief = LegBelief(p=0.4, uncertainty=0.03, source="book")
    blended = blend_beliefs([(belief, 2.5)], max_disagreement=0.01)
    assert blended is not None
    assert blended.p == pytest.approx(0.4)  # weight cancels
    assert blended.uncertainty == pytest.approx(0.03)  # own uncertainty + zero spread
    assert blended.source == "book"


def test_blend_two_agreeing_sources_weighted_mean_plus_spread() -> None:
    b1 = LegBelief(p=0.40, uncertainty=0.02, source="a")
    b2 = LegBelief(p=0.44, uncertainty=0.04, source="b")
    blended = blend_beliefs([(b1, 1.0), (b2, 3.0)], max_disagreement=0.05)
    assert blended is not None
    assert blended.p == pytest.approx((0.40 * 1.0 + 0.44 * 3.0) / 4.0)  # 0.43
    # Weighted mean uncertainty (0.035) plus the 0.04 spread between sources.
    assert blended.uncertainty == pytest.approx(0.035 + 0.04)
    assert blended.source == "a+b"


def test_blend_disagreement_beyond_threshold_is_none_never_averaged() -> None:
    b1 = LegBelief(p=0.30, uncertainty=0.02, source="a")
    b2 = LegBelief(p=0.40, uncertainty=0.02, source="b")
    assert blend_beliefs([(b1, 1.0), (b2, 1.0)], max_disagreement=0.05) is None


def test_blend_disagreement_exactly_at_threshold_still_blends() -> None:
    # 0.25 and 0.5 are exact binary floats: spread == max_disagreement, not beyond.
    b1 = LegBelief(p=0.25, uncertainty=0.01, source="a")
    b2 = LegBelief(p=0.50, uncertainty=0.01, source="b")
    blended = blend_beliefs([(b1, 1.0), (b2, 1.0)], max_disagreement=0.25)
    assert blended is not None
    assert blended.p == pytest.approx(0.375)
    assert blended.uncertainty == pytest.approx(0.01 + 0.25)  # sub-veto spread costs width


def test_blend_zero_total_weight_returns_none() -> None:
    belief = LegBelief(p=0.4, uncertainty=0.03, source="a")
    assert blend_beliefs([(belief, 0.0)], max_disagreement=1.0) is None


def test_blend_negative_total_weight_returns_none() -> None:
    b1 = LegBelief(p=0.40, uncertainty=0.02, source="a")
    b2 = LegBelief(p=0.41, uncertainty=0.02, source="b")
    assert blend_beliefs([(b1, 1.0), (b2, -2.0)], max_disagreement=1.0) is None


def test_blend_deduplicates_source_names() -> None:
    b1 = LegBelief(p=0.40, uncertainty=0.02, source="a")
    b2 = LegBelief(p=0.42, uncertainty=0.02, source="a")
    blended = blend_beliefs([(b1, 1.0), (b2, 1.0)], max_disagreement=0.1)
    assert blended is not None
    assert blended.source == "a"


@settings(derandomize=True, max_examples=200)
@given(
    data=st.lists(
        st.tuples(
            st.floats(min_value=0.01, max_value=0.99),
            st.floats(min_value=0.0, max_value=0.2),
            st.floats(min_value=0.01, max_value=5.0),
        ),
        min_size=1,
        max_size=4,
    ),
    max_disagreement=st.floats(min_value=0.0, max_value=1.0),
)
def test_blend_property_veto_and_bounds(
    data: list[tuple[float, float, float]], max_disagreement: float
) -> None:
    weighted = [
        (LegBelief(p=p, uncertainty=u, source=f"s{i}"), w) for i, (p, u, w) in enumerate(data)
    ]
    ps = [p for p, _, _ in data]
    spread = max(ps) - min(ps)
    blended = blend_beliefs(weighted, max_disagreement=max_disagreement)
    if len(weighted) > 1 and spread > max_disagreement:
        assert blended is None  # disagreement is vetoed, never averaged away
    else:
        assert blended is not None
        assert min(ps) - 1e-9 <= blended.p <= max(ps) + 1e-9
        assert blended.uncertainty >= spread - 1e-12  # sub-veto disagreement costs width


# --- LegBelief validation ---


@pytest.mark.parametrize("p", [-0.001, 1.001, -5.0, 2.0])
def test_legbelief_p_out_of_range_raises(p: float) -> None:
    with pytest.raises(ValueError, match="p out of range"):
        LegBelief(p=p, uncertainty=0.01, source="x")


def test_legbelief_negative_uncertainty_raises() -> None:
    with pytest.raises(ValueError, match="negative uncertainty"):
        LegBelief(p=0.5, uncertainty=-1e-9, source="x")


def test_legbelief_boundary_values_accepted() -> None:
    assert LegBelief(p=0.0, uncertainty=0.0, source="x").p == 0.0
    assert LegBelief(p=1.0, uncertainty=0.0, source="x").p == 1.0
