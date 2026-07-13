"""Bounded, retrying enumeration of the authenticated account's OPEN RFQ quotes.

`GET /communications/quotes?user_filter=self` WITHOUT a `min_ts`/`max_ts` window
makes Kalshi scan the account's ENTIRE quote history — expensive enough to trip
the `midland-exchange` backend circuit-breaker (the error literally reads
`service in fail-fast`), which then returns 500/504 for a cooldown. Kalshi added
`min_ts`/`max_ts` on 2026-06-18 to bound this; the docs
(https://docs.kalshi.com/api-reference/communications/get-quotes) call them
*optional*, but on any account with quote history they are **effectively
required** — VERIFIED 2026-07-13: the unbounded query 500/504s reproducibly while
the identical query with a 7-day window returns instantly. Recorded in NOTES.md
(hard rule 4: docs beat guesses — and here empirical behaviour beats a doc that
says "optional").

BOTH the startup leftover-cancel (`ops/quote_app`) and the supervisor's emergency
cancel-all (`ops/supervisor`) enumerate open quotes the same way, so this helper
is the single place that does it — a bug here is a KILL-PATH bug.

Window: open quotes are short-lived (quote TTL ~30s), so a 7-day lookback
(Kalshi's own retention window for quote data) captures every queryable open
quote; nothing resting is older. The window filters on the quote's LAST-UPDATED
time. A small forward buffer absorbs clock skew and just-updated quotes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from combomaker.exchange.rest import KalshiApiError
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

WINDOW_LOOKBACK_S = 7 * 86_400   # 7 days = Kalshi's quote-data retention window
WINDOW_FORWARD_S = 300           # clock-skew / just-updated buffer
QUOTES_LIMIT = 500               # /communications/quotes documented max
_MAX_PAGES = 1000                # belt-and-braces vs a non-terminating cursor


class QuoteLister(Protocol):
    async def get_quotes(self, **params: Any) -> dict[str, Any]: ...


async def list_open_quotes(
    rest: QuoteLister,
    now_ts: int,
    *,
    retries: int = 4,
    backoff_s: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[dict[str, Any]]:
    """Every OPEN quote the authenticated account holds, cursor-paginated to
    exhaustion, with a bounded ``min_ts``/``max_ts`` window (so the query never
    triggers the full-history scan that trips the exchange circuit-breaker) and
    retry-with-backoff on a 5xx (rides through a transient fail-fast cooldown).

    A 4xx is a real client error and is NEVER retried. If a page still fails with
    a 5xx after all retries, the last error is raised — callers fail closed (the
    startup reconcile refuses to quote; the supervisor still writes KILL).
    """
    min_ts = now_ts - WINDOW_LOOKBACK_S
    max_ts = now_ts + WINDOW_FORWARD_S
    out: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(_MAX_PAGES):
        params: dict[str, Any] = {
            "user_filter": "self",
            "status": "open",
            "min_ts": min_ts,
            "max_ts": max_ts,
            "limit": QUOTES_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor
        payload = await _get_page_with_retry(rest, params, retries, backoff_s, sleep)
        out.extend(payload.get("quotes", []) or [])
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    return out


def open_quote_ids(quotes: list[dict[str, Any]]) -> list[str]:
    """Extract quote ids from raw quote objects (the exact id fields both call
    sites already read: ``id`` then ``quote_id``)."""
    ids: list[str] = []
    for quote in quotes:
        quote_id = str(quote.get("id") or quote.get("quote_id") or "")
        if quote_id:
            ids.append(quote_id)
    return ids


async def _get_page_with_retry(
    rest: QuoteLister,
    params: dict[str, Any],
    retries: int,
    backoff_s: float,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    last: KalshiApiError | None = None
    for attempt in range(max(1, retries)):
        try:
            return await rest.get_quotes(**params)
        except KalshiApiError as exc:
            if exc.status < 500:
                raise  # a 4xx is a real client error — never retry it
            last = exc
            if attempt < retries - 1:
                log.warning(
                    "get_quotes_5xx_retry",
                    status=exc.status,
                    code=exc.code,
                    attempt=attempt + 1,
                    of=retries,
                )
                await sleep(backoff_s * (2**attempt))
    assert last is not None  # loop ran >=1 time and every attempt raised a 5xx
    raise last
