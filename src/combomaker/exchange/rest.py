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

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self
from urllib.parse import urlparse

import aiohttp

from combomaker.core.money import CentiCents, cc_to_dollars_str
from combomaker.exchange.auth import RequestSigner
from combomaker.ops.logging import get_logger

log = get_logger(__name__)

JsonDict = dict[str, Any]


class TierReader(Protocol):
    """The one call ``observe_api_tier`` needs (so tests/tools need no client)."""

    async def get_api_limits(self) -> JsonDict: ...


class KalshiApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: str | None = None) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class RateLimitedError(KalshiApiError):
    """HTTP 429 — the caller breached its token bucket."""


class ReadBudgetExhausted(KalshiApiError):
    """OUR OWN read-token budget refused this GET before it was emitted.

    The local mirror of a 429, raised *instead of* making the request. It
    subclasses ``KalshiApiError`` so every existing "a metadata read may fail,
    skip it and retry later" handler already degrades correctly, and it is
    deliberately NOT a ``RateLimitedError``: nothing here touched the exchange,
    so it must never feed the 429-burst breaker (``HALT_RATE_LIMIT_BURST``) —
    that breaker measures the exchange refusing us, and a local refusal is the
    breaker's fix, not its input. ``status`` is 0 for the same reason: no HTTP
    status was ever received.
    """

    def __init__(self, endpoint: str, cost: int) -> None:
        super().__init__(
            0,
            "local_read_budget",
            f"local read budget refused {endpoint} (cost {cost} tokens)",
        )
        self.endpoint = endpoint
        self.cost = cost


# The exchange's error code on a create the account cannot collateralize —
# the exact string observed in live 400 bodies (2026-08-14/15 storms: 225k/day
# peak 7.2/s). Named here so the cash-gate sender and any handler match ONE
# constant instead of re-typing the exchange's string.
INSUFFICIENT_BALANCE_CODE = "insufficient_balance"


class CashGatedError(KalshiApiError):
    """OUR OWN cash gate refused this create before it was emitted.

    The local mirror of the exchange's insufficient-balance 400, raised
    *instead of* making the request while the account's last cash reading
    cannot have improved (same balance poll as the last refusal). Subclasses
    ``KalshiApiError`` so every existing "a create may fail, log and move on"
    handler degrades identically; ``status`` 0 and a local code because no
    HTTP request was ever made — it must never feed the 429-burst breaker
    (same doctrine as ``ReadBudgetExhausted``)."""

    def __init__(self) -> None:
        super().__init__(
            0,
            "local_cash_gate",
            "local cash gate refused create (exchange balance unchanged "
            "since last insufficient_balance refusal)",
        )


# The per-request wall bound every REST call already runs under. Named so a
# caller that must bound a call reached through a PROTOCOL (which cannot see the
# aiohttp timeout — e.g. the lifecycle's quote withdrawal) reuses this one number
# instead of inventing a second one that can drift away from it.
DEFAULT_REQUEST_TIMEOUT_S = 10.0

# HTTP 404 on a DELETE: the exchange has no such quote. For a WITHDRAWAL that is
# SUCCESS, not an error — see ``already_gone`` just below.
HTTP_NOT_FOUND = 404


def already_gone(exc: BaseException) -> bool:
    """True when a DELETE failed because the exchange has no such quote.

    THE one definition of "provably off the wire by exception", shared by every
    withdrawal path (the lifecycle's ``_withdraw_batch``, the startup reconcile's
    leftover cancel). It lives here, next to ``HTTP_NOT_FOUND`` and the client
    that raises the error, so a second path cannot grow a looser copy: a
    withdrawal that treats 429/5xx/timeout as "gone" is a fail-OPEN seam, and the
    startup reconcile's whole job is to be the trustworthy re-proof that nothing
    of ours is still resting.

    Duck-typed on ``status`` so any client carrying an HTTP status works, and
    NARROW by construction: ONLY 404 is success. A 429 (``RateLimitedError``), a
    5xx, a timeout, or a transport error is UNRESOLVED — the quote may still be
    resting and may still fill, so it counts as not-provably-withdrawn."""
    return getattr(exc, "status", None) == HTTP_NOT_FOUND

# HTTP 429: the account's write TOKEN BUCKET was empty when the request landed.
# The request never reached the book, so the outcome is UNKNOWN to the caller —
# never a confirmed failure (see ``rfq.lifecycle._rate_limited``).
HTTP_TOO_MANY_REQUESTS = 429

# ── RATE-LIMIT TOKEN COSTS (docs/api-notes/openapi-comms.md, "Rate limits") ──
# Kalshi meters writes in TOKENS, not requests: "token buckets per second,
# default endpoint cost 10 tokens; CreateQuote/DeleteQuote/GetQuote/
# DeleteRFQQuote/GetOrder cost 2; Basic tier = 200 read + 100 write tokens/sec
# (Advanced 300/300 via free upgrade endpoint); 429 on empty bucket;
# authoritative costs from GET /account/endpoint_costs."
# Named here — next to the client that emits the calls — so a caller that must
# PACE a burst prices it in the exchange's own unit instead of counting requests
# and hoping. A concurrency literal cannot do this job: N in flight at latency L
# is 2N/L tokens/s, which moves with the EXCHANGE's speed, not with our code.
DELETE_QUOTE_TOKEN_COST = 2
CREATE_QUOTE_TOKEN_COST = 2
# EVERY endpoint without an explicit override costs the DEFAULT — verified live
# against this prod account 2026-07-26: GET /account/endpoint_costs returns
# ``default_cost: 10`` and only 13 overrides, none of which is /markets/{ticker}
# or /events/{ticker}. So one metadata GET costs 10 READ tokens: at the
# advanced tier's 300 read tokens/s that is a hard ceiling of 30 metadata
# reads/s, and the 2026-07-26 incident attempted a measured PEAK of 183/s.
DEFAULT_ENDPOINT_TOKEN_COST = 10


@dataclass(frozen=True, slots=True)
class ApiTierLimits:
    """The account's OBSERVED rate-limit tier — never a guess.

    ``GET /account/limits`` returns ``{usage_tier, read: BucketLimit, write:
    BucketLimit, grants: [...]}`` (docs/api-notes/openapi-comms.md line 279,
    section 7 "Rate limits"); ``BucketLimit = {refill_rate: int (tokens/sec),
    bucket_capacity: int}`` (same section, line 264). Both buckets are used
    verbatim — the tier TABLE in the docs (line 266-274) is only the fail-safe
    fallback, because a grant can move a tier under us at any time and a
    hard-coded ceiling is exactly the bug this type exists to remove.
    """

    usage_tier: str
    read_refill_per_s: int
    read_capacity: int
    write_refill_per_s: int
    write_capacity: int
    # False ⇒ the tier could not be read and these are the fail-safe floor.
    observed: bool

    @property
    def read_refill_s(self) -> float:
        """Seconds for ``read_capacity`` tokens to accrue — the ``refill_s`` a
        ``TokenBudget`` of that capacity needs to sustain ``read_refill_per_s``."""
        return self.read_capacity / self.read_refill_per_s

    @property
    def write_refill_s(self) -> float:
        return self.write_capacity / self.write_refill_per_s

    def clamp_write_budget(
        self, capacity: int, refill_s: float
    ) -> tuple[int, float]:
        """A CONFIGURED write budget reduced to what this tier can spend.

        Both axes bind, and both were live bugs waiting to happen:

        * SUSTAINED rate — ``capacity / refill_s`` may not exceed the tier's
          ``refill_rate``, or the budget admits a stream the exchange refuses.
        * BURST capacity — the bucket starts FULL and a wave can spend all of it
          inside one exchange second, so ``capacity`` may not exceed the tier's
          ``bucket_capacity``. The shipped operator knob is 200 tokens; that is
          fine against the observed prod bucket (600) and a **2x breach** of the
          fail-safe Basic bucket (100). Clamping only the rate would have let an
          unreadable tier 429 its own withdrawal wave — the exact failure the
          fail-safe exists to prevent.

        The sustained rate is PRESERVED where the tier allows it (the refill
        window is rescaled with the capacity), so clamping costs burst depth,
        never throughput."""
        rate = capacity / refill_s
        new_capacity = min(capacity, self.write_capacity)
        new_rate = min(rate, float(self.write_refill_per_s))
        return new_capacity, new_capacity / new_rate


# FAIL-SAFE FLOOR: the LOWEST tier in the documented table (Basic = 200 read /
# 100 write tokens/s, burst 1 s ⇒ capacity = rate × burst;
# docs/api-notes/openapi-comms.md line 268). Used when /account/limits cannot be
# read, because assuming the HIGHEST tier turns an unreadable response into a
# self-inflicted 429 storm. The only in-repo recorded observation before
# 2026-07-26 (tests/fixtures/ground_truth/scenario_account_facts.jsonl, DEMO,
# 2026-07-06) says exactly this: usage_tier=basic, write 100/100.
LOWEST_TIER_LIMITS = ApiTierLimits(
    usage_tier="basic",
    read_refill_per_s=200,
    read_capacity=200,
    write_refill_per_s=100,
    write_capacity=100,
    observed=False,
)


def _bucket(raw: object, floor_rate: int, floor_capacity: int) -> tuple[int, int]:
    """``(refill_rate, bucket_capacity)`` from a ``BucketLimit`` payload, with
    any missing/absurd field falling back to the LOWEST-tier floor."""
    rate = floor_rate
    capacity = floor_capacity
    if isinstance(raw, dict):
        r = raw.get("refill_rate")
        c = raw.get("bucket_capacity")
        if isinstance(r, int) and not isinstance(r, bool) and r > 0:
            rate = r
        if isinstance(c, int) and not isinstance(c, bool) and c > 0:
            capacity = c
    # A capacity below one refill-second is not a bucket we can pace against.
    return rate, max(capacity, rate)


async def observe_api_tier(rest: TierReader) -> ApiTierLimits:
    """Read this account's ACTUAL token buckets. Never raises.

    FAIL-SAFE BY CONSTRUCTION: any error, any unparseable payload ⇒
    ``LOWEST_TIER_LIMITS``. Pacing derived from an unknown tier must assume the
    smallest bucket, never the largest — the failure mode of guessing high is a
    429 storm on the shared account bucket, which is the incident this exists to
    prevent."""
    try:
        payload = await rest.get_api_limits()
    except Exception as exc:  # noqa: BLE001 — an unreadable tier IS the signal
        log.warning(
            "api_tier_unreadable",
            error=repr(exc),
            detail="GET /account/limits failed — pacing falls back to the "
            "LOWEST documented tier (fail-safe, never the highest)",
            fallback_tier=LOWEST_TIER_LIMITS.usage_tier,
        )
        return LOWEST_TIER_LIMITS
    if not isinstance(payload, dict):
        log.warning("api_tier_unreadable", detail="non-object /account/limits")
        return LOWEST_TIER_LIMITS
    read_rate, read_cap = _bucket(
        payload.get("read"),
        LOWEST_TIER_LIMITS.read_refill_per_s,
        LOWEST_TIER_LIMITS.read_capacity,
    )
    write_rate, write_cap = _bucket(
        payload.get("write"),
        LOWEST_TIER_LIMITS.write_refill_per_s,
        LOWEST_TIER_LIMITS.write_capacity,
    )
    tier = str(payload.get("usage_tier") or LOWEST_TIER_LIMITS.usage_tier)
    limits = ApiTierLimits(
        usage_tier=tier,
        read_refill_per_s=read_rate,
        read_capacity=read_cap,
        write_refill_per_s=write_rate,
        write_capacity=write_cap,
        observed=True,
    )
    log.info(
        "api_tier_observed",
        usage_tier=limits.usage_tier,
        read_refill_per_s=limits.read_refill_per_s,
        read_capacity=limits.read_capacity,
        write_refill_per_s=limits.write_refill_per_s,
        write_capacity=limits.write_capacity,
        metadata_reads_per_s_ceiling=(
            limits.read_refill_per_s / DEFAULT_ENDPOINT_TOKEN_COST
        ),
    )
    return limits


class KalshiRestClient:
    def __init__(
        self,
        base_url: str,
        signer: RequestSigner | None,
        *,
        session: aiohttp.ClientSession | None = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_prefix = urlparse(self._base_url).path
        self._signer = signer
        self._session = session
        self._owns_session = session is None
        self._request_timeout_s = request_timeout_s
        self._timeout = aiohttp.ClientTimeout(total=request_timeout_s)

    @property
    def request_timeout_s(self) -> float:
        """The per-request wall bound this client enforces."""
        return self._request_timeout_s

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
