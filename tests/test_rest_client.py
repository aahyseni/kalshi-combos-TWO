"""REST client tests against a local aiohttp fake server (no real network)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import rsa

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiApiError, KalshiRestClient, RateLimitedError

RECORDED: list[dict[str, Any]] = []


def make_app() -> web.Application:
    async def handler(request: web.Request) -> web.Response:
        body = await request.text()
        RECORDED.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
                "body": json.loads(body) if body else None,
            }
        )
        route = (request.method, request.path)
        if route == ("GET", "/trade-api/v2/exchange/status"):
            return web.json_response({"exchange_active": True, "trading_active": True})
        if route == ("GET", "/trade-api/v2/portfolio/balance"):
            if "KALSHI-ACCESS-SIGNATURE" not in request.headers:
                return web.json_response({"code": "missing_auth"}, status=401)
            return web.json_response({"balance": 500000})
        if route == ("POST", "/trade-api/v2/communications/quotes"):
            return web.json_response({"id": "q-123"}, status=201)
        if route == ("PUT", "/trade-api/v2/communications/quotes/q-123/confirm"):
            return web.Response(status=204)
        if route == ("GET", "/trade-api/v2/ratelimited"):
            return web.json_response({"error": "too many requests"}, status=429)
        if route == ("GET", "/trade-api/v2/broken"):
            return web.json_response(
                {"code": "internal", "message": "boom", "details": "d"}, status=500
            )
        return web.json_response({"code": "not_found", "message": "nope"}, status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


@pytest.fixture()
async def client() -> AsyncIterator[KalshiRestClient]:
    RECORDED.clear()
    server = TestServer(make_app())
    await server.start_server()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = RequestSigner(
        Credentials(api_key_id="kid", private_key=key),
        FakeClock(start=datetime(2026, 7, 5, tzinfo=UTC)),
    )
    base = str(server.make_url("/trade-api/v2"))
    async with KalshiRestClient(base, signer) as c:
        yield c
    await server.close()


async def test_unauthenticated_endpoint_sends_no_auth_headers(client: KalshiRestClient) -> None:
    status = await client.get_exchange_status()
    assert status["exchange_active"] is True
    assert "KALSHI-ACCESS-KEY" not in RECORDED[-1]["headers"]


async def test_authenticated_endpoint_sends_all_three_headers(client: KalshiRestClient) -> None:
    balance = await client.get_balance()
    assert balance["balance"] == 500000
    headers = RECORDED[-1]["headers"]
    assert headers["KALSHI-ACCESS-KEY"] == "kid"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    assert headers["KALSHI-ACCESS-SIGNATURE"]


async def test_create_quote_wire_format(client: KalshiRestClient) -> None:
    await client.create_quote(
        "rfq-1", yes_bid_cc=CentiCents(5_600), no_bid_cc=CentiCents(4_000), rest_remainder=False
    )
    body = RECORDED[-1]["body"]
    assert body == {
        "rfq_id": "rfq-1",
        "yes_bid": "0.5600",
        "no_bid": "0.4000",
        "rest_remainder": False,
    }


async def test_create_quote_rejects_double_decline(client: KalshiRestClient) -> None:
    with pytest.raises(ValueError):
        await client.create_quote("rfq-1", yes_bid_cc=CentiCents(0), no_bid_cc=CentiCents(0))


async def test_confirm_quote_returns_empty_on_204(client: KalshiRestClient) -> None:
    assert await client.confirm_quote("q-123") == {}


async def test_rate_limit_maps_to_typed_error(client: KalshiRestClient) -> None:
    with pytest.raises(RateLimitedError):
        await client._request("GET", "/ratelimited")


async def test_error_payload_mapped(client: KalshiRestClient) -> None:
    with pytest.raises(KalshiApiError) as excinfo:
        await client._request("GET", "/broken")
    assert excinfo.value.status == 500
    assert excinfo.value.code == "internal"
    assert excinfo.value.details == "d"


async def test_query_params_on_wire(client: KalshiRestClient) -> None:
    with pytest.raises(KalshiApiError):
        await client.get_rfqs(market_ticker="T", limit=5)
    assert RECORDED[-1]["query"] == {"market_ticker": "T", "limit": "5"}
