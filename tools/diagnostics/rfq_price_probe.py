"""Price-discovery probe (2026-07-16): re-RFQ our two open ESP-ARG-legged fills
and read competing maker quotes. READ-ONLY intent: never accepts; deletes its
own RFQs after reading. Mirrors the 2026-07-14 probe technique."""

import asyncio
import json

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.ops.dotenv import load_dotenv

KALSHI = "https://external-api.kalshi.com/trade-api/v2"

# (combo_ticker, description, our fill NO price in cc, our fill ct)
TARGETS = [
    ("KXMVECROSSCATEGORY-S2026DEADDA0B72A-3F55FA29427",
     "yes:FRAENG-BTTS + yes:ESPARG-BTTS  (our fill: NO @ 64.00c x 39.87ct, 15:48Z)", 6400),
    ("KXMVESPORTSMULTIGAMEEXTENDED-S2026CFD2CA10A13-04EA5F03582",
     "yes:ESPARG corners 9+ + Messi 1+ + ARG 4+ tcorners  (our fill: NO @ 83.30c x 9.16ct, 15:47Z)", 8330),
]

POLL_S = 2.0
POLLS = 11  # ~22s inside the ~30s RFQ TTL


async def probe(rest: KalshiRestClient, ticker: str, desc: str, our_cc: int) -> None:
    print(f"\n{'=' * 100}\nPROBE: {desc}\n  ticker {ticker}")
    try:
        r = await rest.create_rfq(
            ticker, target_cost_dollars="25.00",
            rest_remainder=False, replace_existing=True,
        )
    except Exception as e:
        print(f"  create_rfq FAILED: {e}")
        return
    rfq = r.get("rfq", r)
    rfq_id = str(rfq.get("id") or rfq.get("rfq_id") or r.get("id"))
    print(f"  rfq_id {rfq_id}")

    seen: dict[str, dict] = {}
    for _ in range(POLLS):
        await asyncio.sleep(POLL_S)
        try:
            q = await rest.get_quotes(rfq_id=rfq_id, rfq_user_filter="self")
        except Exception as e:
            print(f"  get_quotes err: {e}")
            continue
        for quote in q.get("quotes", []):
            qid = str(quote.get("id") or quote.get("quote_id"))
            seen[qid] = quote

    if not seen:
        print("  NO maker quotes arrived in the window")
    for qid, quote in seen.items():
        print(f"  QUOTE {qid}:")
        print("    " + json.dumps(quote, default=str))

    try:
        await rest.delete_rfq(rfq_id)
        print("  probe rfq deleted (never accepted anything)")
    except Exception as e:
        print(f"  delete_rfq: {e} (TTL will expire it)")


async def main() -> None:
    load_dotenv()
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    async with KalshiRestClient(KALSHI, signer) as rest:  # type: ignore[arg-type]
        for ticker, desc, our_cc in TARGETS:
            await probe(rest, ticker, desc, our_cc)


asyncio.run(main())
