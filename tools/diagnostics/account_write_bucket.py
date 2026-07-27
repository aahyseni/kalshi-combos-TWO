"""READ-ONLY: what write-token bucket does THIS PROD ACCOUNT actually have?

The 2026-07-26 withdrawal re-pacing hard-codes ``WRITE_TOKENS_PER_S = 300``
("we are on Advanced"). The only recorded observation in the repo
(``tests/fixtures/ground_truth/scenario_account_facts.jsonl``, 2026-07-06) says
``usage_tier: basic``, ``write {bucket_capacity: 100, refill_rate: 100}``.
This asks the exchange. Two GETs, no writes, no orders.

Usage:  .venv/Scripts/python.exe tools/diagnostics/account_write_bucket.py
"""

from __future__ import annotations

import asyncio
import json

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.ops.dotenv import load_dotenv

PROD_REST = "https://external-api.kalshi.com/trade-api/v2"


async def run() -> None:
    load_dotenv()
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    async with KalshiRestClient(PROD_REST, signer) as rest:
        limits = await rest.get_api_limits()
        print("GET /account/limits ->", json.dumps(limits, sort_keys=True))
        costs = await rest.get_endpoint_costs()
        default = costs.get("default_cost")
        rows = [
            r
            for r in (costs.get("endpoint_costs") or [])
            if "quote" in str(r.get("path", "")).lower()
        ]
        print("default_cost:", default)
        for r in rows:
            print("  ", r)


if __name__ == "__main__":
    asyncio.run(run())
