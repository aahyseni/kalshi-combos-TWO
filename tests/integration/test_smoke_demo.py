"""Phase 0 smoke test against the DEMO environment. Needs real demo credentials.

Run explicitly:  uv run pytest -m integration tests/integration/test_smoke_demo.py
Skips (not fails) when credentials are absent so CI stays green without them.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import ENV_API_KEY_ID, Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.exchange.ws import WsManager
from combomaker.ops.config import Env, load_config

pytestmark = pytest.mark.integration

needs_creds = pytest.mark.skipif(
    not os.environ.get(ENV_API_KEY_ID),
    reason="demo credentials not configured (KALSHI_API_KEY_ID unset)",
)

CONFIG = Path(__file__).resolve().parents[2] / "config" / "demo.yaml"


async def test_exchange_status_unauthenticated() -> None:
    config = load_config(CONFIG, env=Env.DEMO)
    async with KalshiRestClient(config.endpoints.rest_base_url, signer=None) as client:
        status = await client.get_exchange_status()
    assert "exchange_active" in status


@needs_creds
async def test_authenticated_rest_call() -> None:
    config = load_config(CONFIG, env=Env.DEMO)
    signer = RequestSigner(Credentials.from_env(), SystemClock())
    async with KalshiRestClient(config.endpoints.rest_base_url, signer) as client:
        balance = await client.get_balance()
        assert "balance" in balance
        comms = await client.get_communications_id()
        assert comms.get("communications_id")


@needs_creds
async def test_ws_connect_and_subscribe_communications() -> None:
    config = load_config(CONFIG, env=Env.DEMO)
    signer = RequestSigner(Credentials.from_env(), SystemClock())
    clock = SystemClock()
    ws = WsManager(config.endpoints.ws_url, signer, clock, name="smoke")
    acked = asyncio.Event()

    async def on_subscribed(message: dict[str, object]) -> None:
        acked.set()

    ws.on_message("subscribed", on_subscribed)
    ws.add_subscription(["communications"])
    ws.start()
    try:
        await asyncio.wait_for(acked.wait(), timeout=15)
        assert ws.healthy
    finally:
        await ws.stop()
