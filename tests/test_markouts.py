"""Tests for combomaker.risk.markouts — fire-and-forget markout rows.

These run on the real event loop (asyncio.sleep is baked into _run), so the
horizons are tiny (0.01/0.02s). Nothing asserts wall-clock durations —
determinism comes from ordering and drain(), not timing.
"""

from __future__ import annotations

from typing import Any

from combomaker.risk.markouts import MarkoutSubject, MarkoutTracker


class CapturingSink:
    """Async sink recording every row it is handed (Store.record_markout shape)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def __call__(
        self,
        fill_ref: str,
        *,
        horizon_s: float,
        fair_at_fill_cc: int | None,
        fair_now_cc: int | None,
        raw_mid_at_fill_cc: int | None,
        raw_mid_now_cc: int | None,
    ) -> None:
        self.rows.append(
            {
                "fill_ref": fill_ref,
                "horizon_s": horizon_s,
                "fair_at_fill_cc": fair_at_fill_cc,
                "fair_now_cc": fair_now_cc,
                "raw_mid_at_fill_cc": raw_mid_at_fill_cc,
                "raw_mid_now_cc": raw_mid_now_cc,
            }
        )


def sequence_provider(values: list[tuple[int | None, int | None]]) -> Any:
    """Provider returning the next (fair, mid) snapshot on each call."""
    iterator = iter(values)

    def provider() -> tuple[int | None, int | None]:
        return next(iterator)

    return provider


SUBJECT = MarkoutSubject(fill_ref="fill-1", fair_at_event_cc=111, raw_mid_at_event_cc=222)


async def test_one_row_per_horizon_with_provider_values_in_order() -> None:
    sink = CapturingSink()
    tracker = MarkoutTracker(sink, horizons_s=(0.01, 0.02))
    provider = sequence_provider([(101, 201), (102, 202)])

    tracker.track(SUBJECT, provider)
    await tracker.drain()

    assert len(sink.rows) == 2
    assert [r["horizon_s"] for r in sink.rows] == [0.01, 0.02]
    first, second = sink.rows
    # At-event values pass through unchanged on every row.
    for row in sink.rows:
        assert row["fill_ref"] == "fill-1"
        assert row["fair_at_fill_cc"] == 111
        assert row["raw_mid_at_fill_cc"] == 222
    # Provider's per-call values land on the matching horizon, in call order.
    assert (first["fair_now_cc"], first["raw_mid_now_cc"]) == (101, 201)
    assert (second["fair_now_cc"], second["raw_mid_now_cc"]) == (102, 202)


async def test_horizons_are_sorted_ascending_regardless_of_input_order() -> None:
    sink = CapturingSink()
    tracker = MarkoutTracker(sink, horizons_s=(0.02, 0.01))  # deliberately reversed
    provider = sequence_provider([(1, 1), (2, 2)])

    tracker.track(SUBJECT, provider)
    await tracker.drain()

    assert [r["horizon_s"] for r in sink.rows] == [0.01, 0.02]
    assert [r["fair_now_cc"] for r in sink.rows] == [1, 2]


async def test_provider_raising_still_records_rows_with_nones() -> None:
    sink = CapturingSink()
    tracker = MarkoutTracker(sink, horizons_s=(0.01, 0.02))

    def exploding_provider() -> tuple[int | None, int | None]:
        raise RuntimeError("books gone")

    tracker.track(SUBJECT, exploding_provider)
    await tracker.drain()  # must not raise

    assert len(sink.rows) == 2
    for row in sink.rows:
        assert row["fair_now_cc"] is None
        assert row["raw_mid_now_cc"] is None
        # At-event values are still recorded — the row is not lost.
        assert row["fair_at_fill_cc"] == 111
        assert row["raw_mid_at_fill_cc"] == 222


async def test_sink_raising_does_not_stop_later_horizons() -> None:
    attempted: list[float] = []

    async def exploding_sink(fill_ref: str, **kwargs: Any) -> None:
        attempted.append(kwargs["horizon_s"])
        raise RuntimeError("db down")

    tracker = MarkoutTracker(exploding_sink, horizons_s=(0.01, 0.02))
    provider = sequence_provider([(1, 1), (2, 2)])

    tracker.track(SUBJECT, provider)
    await tracker.drain()  # must not raise

    assert attempted == [0.01, 0.02]


async def test_drain_waits_for_in_flight_markouts() -> None:
    sink = CapturingSink()
    tracker = MarkoutTracker(sink, horizons_s=(0.05,))
    provider = sequence_provider([(1, 1)])

    tracker.track(SUBJECT, provider)
    assert sink.rows == []  # task scheduled, nothing recorded yet
    await tracker.drain()
    assert len(sink.rows) == 1  # drain returned only after the row landed


async def test_drain_with_no_tasks_returns_immediately() -> None:
    tracker = MarkoutTracker(CapturingSink(), horizons_s=(0.01,))
    await tracker.drain()  # no tasks: must not hang or raise


async def test_multiple_subjects_tracked_concurrently() -> None:
    sink = CapturingSink()
    tracker = MarkoutTracker(sink, horizons_s=(0.01, 0.02))

    other = MarkoutSubject(fill_ref="declined:q-9", fair_at_event_cc=None, raw_mid_at_event_cc=None)
    tracker.track(SUBJECT, sequence_provider([(1, 1), (2, 2)]))
    tracker.track(other, sequence_provider([(3, 3), (4, 4)]))
    await tracker.drain()

    assert len(sink.rows) == 4
    by_ref = {r["fill_ref"] for r in sink.rows}
    assert by_ref == {"fill-1", "declined:q-9"}
    declined_rows = [r for r in sink.rows if r["fill_ref"] == "declined:q-9"]
    assert [r["horizon_s"] for r in declined_rows] == [0.01, 0.02]
    assert all(r["fair_at_fill_cc"] is None for r in declined_rows)
    # Done-callbacks cleaned the task set: no leaked task references.
    assert not tracker._tasks
