"""exchange.quote_query.list_open_quotes — bounded (min_ts/max_ts window) +
5xx-retrying enumeration of the account's open quotes. The window keeps the query
off the full-history scan that trips Kalshi's midland-exchange circuit-breaker
(500/504); the retry rides through a transient fail-fast cooldown. Used by BOTH
the startup reconcile and the supervisor's emergency cancel-all (kill path)."""

from __future__ import annotations

from typing import Any

import pytest

from combomaker.exchange.quote_query import (
    QUOTES_LIMIT,
    WINDOW_FORWARD_S,
    WINDOW_LOOKBACK_S,
    list_open_quotes,
    open_quote_ids,
)
from combomaker.exchange.rest import KalshiApiError

NOW = 1_800_000_000


class FakeRest:
    """Records the params of each get_quotes call; optionally raises a queued
    exception before returning the next page payload."""

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        raise_seq: list[KalshiApiError | None] | None = None,
    ) -> None:
        self._pages = list(pages or [{"quotes": [], "cursor": ""}])
        self._raise_seq = list(raise_seq or [])
        self.calls: list[dict[str, Any]] = []

    async def get_quotes(self, **params: Any) -> dict[str, Any]:
        self.calls.append(params)
        if self._raise_seq:
            exc = self._raise_seq.pop(0)
            if exc is not None:
                raise exc
        return self._pages.pop(0) if self._pages else {"quotes": [], "cursor": ""}


async def _noop_sleep(_: float) -> None:
    pass


async def test_query_is_bounded_and_filtered() -> None:
    rest = FakeRest(pages=[{"quotes": [{"id": "q1"}], "cursor": ""}])
    out = await list_open_quotes(rest, NOW, sleep=_noop_sleep)
    p = rest.calls[0]
    assert p["user_filter"] == "self"
    assert p["status"] == "open"
    assert p["limit"] == QUOTES_LIMIT
    # THE FIX: a bounded time window (unbounded = full-history scan = 500/504).
    assert p["min_ts"] == NOW - WINDOW_LOOKBACK_S
    assert p["max_ts"] == NOW + WINDOW_FORWARD_S
    assert out == [{"id": "q1"}]


async def test_paginates_to_exhaustion_carrying_the_window() -> None:
    rest = FakeRest(
        pages=[
            {"quotes": [{"id": "q0"}, {"id": "q1"}], "cursor": "c1"},
            {"quotes": [{"id": "q2"}], "cursor": ""},
        ]
    )
    out = await list_open_quotes(rest, NOW, sleep=_noop_sleep)
    assert [q["id"] for q in out] == ["q0", "q1", "q2"]
    assert len(rest.calls) == 2 and rest.calls[1]["cursor"] == "c1"
    assert all(c["min_ts"] == NOW - WINDOW_LOOKBACK_S for c in rest.calls)


async def test_retries_on_5xx_then_succeeds() -> None:
    err = KalshiApiError(504, "fail-fast", "service in fail-fast")
    rest = FakeRest(
        pages=[{"quotes": [{"id": "q1"}], "cursor": ""}], raise_seq=[err, err, None]
    )
    out = await list_open_quotes(rest, NOW, retries=4, sleep=_noop_sleep)
    assert out == [{"id": "q1"}]
    assert len(rest.calls) == 3  # 2 x 5xx retried, then the success


async def test_does_not_retry_a_4xx() -> None:
    err = KalshiApiError(403, "forbidden", "must fill a user filter")
    rest = FakeRest(raise_seq=[err])
    with pytest.raises(KalshiApiError) as ei:
        await list_open_quotes(rest, NOW, retries=4, sleep=_noop_sleep)
    assert ei.value.status == 403
    assert len(rest.calls) == 1  # a client error is never retried


async def test_raises_after_exhausting_5xx_retries() -> None:
    err = KalshiApiError(500, "internal_server_error", "midland")
    rest = FakeRest(raise_seq=[err, err, err])
    with pytest.raises(KalshiApiError) as ei:
        await list_open_quotes(rest, NOW, retries=3, sleep=_noop_sleep)
    assert ei.value.status == 500
    assert len(rest.calls) == 3  # every attempt used, then re-raised (fail closed)


def test_open_quote_ids_reads_id_then_quote_id() -> None:
    quotes = [{"id": "a"}, {"quote_id": "b"}, {"other": 1}, {"id": "", "quote_id": "c"}]
    assert open_quote_ids(quotes) == ["a", "b", "c"]
