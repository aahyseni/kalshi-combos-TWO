"""Thin async REST client for the Kalshi trade API.

Deliberately thin: methods mirror endpoints one-to-one, take/return wire-level
dicts plus centi-cent integers at the money boundary, and raise typed errors.
Domain parsing lives in the layers that own the data (marketdata, rfq). No
automatic retries — the hot path must decide for itself what a retry means.

Paths are relative to the versioned base URL from config; signing uses the full
path including the ``/trade-api/v2`` prefix with query params stripped
(docs/api-notes/auth-env.md).
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

import aiohttp

from combomaker.core.money import CentiCents, cc_to_dollars_str
from combomaker.exchange.auth import RequestSigner
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

JsonDict = dict[str, Any]


class KalshiApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: str | None = None) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class RateLimitedError(KalshiApiError):
    """HTTP 429 — the caller breached its token bucket."""


class KalshiRestClient:
    def __init__(
        self,
        base_url: str,
        signer: RequestSigner | None,
        *,
        session: aiohttp.ClientSession | None = None,
        request_timeout_s: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_prefix = urlparse(self._base_url).path
        self._signer = signer
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_s)

    async def __aenter__(self) -> Self:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | list[tuple[str, str]] | None = None,
        json_body: JsonDict | None = None,
        auth: bool = True,
    ) -> JsonDict:
        if self._session is None:
            raise RuntimeError("client not started — use 'async with' or __aenter__")
        headers: dict[str, str] = {}
        if auth:
            if self._signer is None:
                raise RuntimeError(f"endpoint {path} requires auth but no signer configured")
            headers = self._signer.headers(method, self._api_prefix + path)

        async with self._session.request(
            method,
            self._base_url + path,
            params=params,
            json=json_body,
            headers=headers,
        ) as resp:
            if resp.status == 204:
                return {}
            try:
                payload: JsonDict = await resp.json()
            except (aiohttp.ContentTypeError, ValueError):
                payload = {"message": (await resp.text())[:500]}
            if resp.status >= 400:
                code = str(payload.get("code", ""))
                message = str(payload.get("message", payload.get("error", "")))
                details = payload.get("details")
                err_cls = RateLimitedError if resp.status == 429 else KalshiApiError
                raise err_cls(resp.status, code, message, details)
            return payload

    # --- exchange / account ---

    async def get_exchange_status(self) -> JsonDict:
        return await self._request("GET", "/exchange/status", auth=False)

    async def get_balance(self) -> JsonDict:
        return await self._request("GET", "/portfolio/balance")

    async def get_api_limits(self) -> JsonDict:
        return await self._request("GET", "/account/limits")

    async def get_endpoint_costs(self) -> JsonDict:
        return await self._request("GET", "/account/endpoint_costs")

    # --- markets / metadata ---

    async def get_market(self, ticker: str) -> JsonDict:
        return await self._request("GET", f"/markets/{ticker}", auth=False)

    async def get_markets(self, **filters: str | int) -> JsonDict:
        return await self._request("GET", "/markets", params=dict(filters), auth=False)

    async def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int = 60,
    ) -> JsonDict:
        """OHLC price history for a market (public; period_interval minutes)."""
        return await self._request(
            "GET",
            f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
            auth=False,
        )

    async def get_orderbook(self, ticker: str, *, depth: int | None = None) -> JsonDict:
        params: dict[str, str | int] = {}
        if depth is not None:
            params["depth"] = depth
        return await self._request("GET", f"/markets/{ticker}/orderbook", params=params)

    async def get_multiple_orderbooks(self, tickers: list[str]) -> JsonDict:
        # Wire contract: query param is named `tickers`, repeated form/explode
        # (?tickers=A&tickers=B), 1-100 items (docs/api-notes/orderbooks.md).
        return await self._request(
            "GET", "/markets/orderbooks", params=[("tickers", t) for t in tickers]
        )

    async def get_trades(self, **params: str | int) -> JsonDict:
        """Public trade tape (GET /markets/trades; filter with ticker/min_ts)."""
        return await self._request("GET", "/markets/trades", params=dict(params))

    async def get_multivariate_collections(self, **params: str | int) -> JsonDict:
        return await self._request(
            "GET", "/multivariate_event_collections", params=dict(params), auth=False
        )

    async def get_multivariate_collection(self, ticker: str) -> JsonDict:
        return await self._request("GET", f"/multivariate_event_collections/{ticker}", auth=False)

    async def get_event(self, ticker: str) -> JsonDict:
        return await self._request("GET", f"/events/{ticker}", auth=False)

    async def get_series(self, ticker: str) -> JsonDict:
        return await self._request("GET", f"/series/{ticker}", auth=False)

    async def get_series_fee_changes(self, **params: str | int) -> JsonDict:
        return await self._request("GET", "/series/fee_changes", params=dict(params), auth=False)

    # --- communications (RFQs / quotes) ---

    async def get_communications_id(self) -> JsonDict:
        return await self._request("GET", "/communications/id")

    async def get_rfqs(self, **params: str | int) -> JsonDict:
        return await self._request("GET", "/communications/rfqs", params=dict(params))

    async def get_rfq(self, rfq_id: str) -> JsonDict:
        return await self._request("GET", f"/communications/rfqs/{rfq_id}")

    async def get_quotes(self, **params: str | int) -> JsonDict:
        return await self._request("GET", "/communications/quotes", params=dict(params))

    async def get_quote(self, quote_id: str) -> JsonDict:
        return await self._request("GET", f"/communications/quotes/{quote_id}")

    async def create_quote(
        self,
        rfq_id: str,
        *,
        yes_bid_cc: CentiCents,
        no_bid_cc: CentiCents,
        rest_remainder: bool = False,
    ) -> JsonDict:
        """Send a quote. A ``0`` bid declines that side; both zero is invalid.

        Wire format is fixed-point dollar strings (up to 6 dp; we emit 4 —
        exactly centi-cent precision). Grid validity is the caller's job.
        """
        if yes_bid_cc == 0 and no_bid_cc == 0:
            raise ValueError("cannot decline both sides of a quote")
        body = {
            "rfq_id": rfq_id,
            "yes_bid": cc_to_dollars_str(yes_bid_cc),
            "no_bid": cc_to_dollars_str(no_bid_cc),
            "rest_remainder": rest_remainder,
        }
        return await self._request("POST", "/communications/quotes", json_body=body)

    async def delete_quote(self, quote_id: str) -> JsonDict:
        return await self._request("DELETE", f"/communications/quotes/{quote_id}")

    # Requester-side endpoints — used ONLY by the Phase 2.5 ground-truth
    # harness and demo integration tests; the maker never creates RFQs.

    async def create_rfq(
        self,
        market_ticker: str,
        *,
        contracts_fp: str | None = None,
        target_cost_dollars: str | None = None,
        rest_remainder: bool = False,
        replace_existing: bool = False,
    ) -> JsonDict:
        body: JsonDict = {
            "market_ticker": market_ticker,
            "rest_remainder": rest_remainder,
            "replace_existing": replace_existing,
        }
        if contracts_fp is not None:
            body["contracts_fp"] = contracts_fp
        if target_cost_dollars is not None:
            body["target_cost_dollars"] = target_cost_dollars
        return await self._request("POST", "/communications/rfqs", json_body=body)

    async def delete_rfq(self, rfq_id: str) -> JsonDict:
        return await self._request("DELETE", f"/communications/rfqs/{rfq_id}")

    async def accept_quote(self, quote_id: str, *, accepted_side: str) -> JsonDict:
        if accepted_side not in ("yes", "no"):
            raise ValueError(f"accepted_side must be yes|no, got {accepted_side!r}")
        return await self._request(
            "PUT",
            f"/communications/quotes/{quote_id}/accept",
            json_body={"accepted_side": accepted_side},
        )

    async def confirm_quote(self, quote_id: str) -> JsonDict:
        """Confirm an accepted quote. 204 on success; starts the execution timer.

        Ground truth (2026-07-05): the docs say no body is required, but a
        bodyless PUT gets 400 invalid_content_type — the server wants
        Content-Type: application/json, so we send an empty JSON object.
        """
        return await self._request(
            "PUT", f"/communications/quotes/{quote_id}/confirm", json_body={}
        )

    # --- portfolio ---

    async def get_fills(self, **params: str | int) -> JsonDict:
        return await self._request("GET", "/portfolio/fills", params=dict(params))

    async def get_positions(self, **params: str | int) -> JsonDict:
        return await self._request("GET", "/portfolio/positions", params=dict(params))

    async def get_settlements(self, **params: str | int) -> JsonDict:
        """Settled markets the account held (GET /portfolio/settlements).

        Response ``{cursor, settlements: Settlement[]}``. Each Settlement carries
        ``ticker``, ``market_result`` (enum ``yes``|``no``|``scalar``),
        ``yes_count_fp``/``no_count_fp``, ``*_total_cost_dollars``, ``revenue``
        (int CENTS), ``fee_cost`` (string dollars), ``settled_time``, and
        ``value`` (int CENTS, nullable — payout per YES contract). Filter with
        ``ticker``/``event_ticker``/``min_ts``/``max_ts``; omit ``subaccount`` for
        all subaccounts (docs/api-notes/index-scan.md §portfolio).
        """
        return await self._request("GET", "/portfolio/settlements", params=dict(params))

    async def get_deposits(self, **params: str | int) -> JsonDict:
        """Deposit history (GET /portfolio/deposits, docs 2026-07-21).

        Response ``{cursor, deposits: Deposit[]}``; each carries ``id``,
        ``status`` (pending|applied|failed|returned — only ``applied`` funds
        are in the balance), ``type``, ``amount_cents``/``fee_cents`` (int
        CENTS), ``created_ts``/``finalized_ts`` (unix ms). Page with
        ``limit`` (≤500) / ``cursor``. Account-standing reconciliation input:
        deposits − withdrawals + realized settlements ≡ balance.
        """
        return await self._request("GET", "/portfolio/deposits", params=dict(params))

    async def get_withdrawals(self, **params: str | int) -> JsonDict:
        """Withdrawal history (GET /portfolio/withdrawals) — mirror of
        ``get_deposits`` (same paging + cents fields), the subtractive side of
        the account-standing identity."""
        return await self._request("GET", "/portfolio/withdrawals", params=dict(params))
