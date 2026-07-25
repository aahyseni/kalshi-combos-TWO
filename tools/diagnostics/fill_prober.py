"""Standing fill prober (2026-07-25, operator directive: probe every fill).

Watches the live fills ledger; for each NEW fill, re-RFQs the same combo
market (small target cost), reads the competing maker quotes for ~22s, and
reports how RICH our fill was vs the best competing maker on the same side
(ours − best other, in NO-price cents — positive = we filled richer than the
field). READ-ONLY intent: never accepts a quote; deletes its own RFQ after
reading. Watermark persisted so restarts never re-probe.

Usage:
  .venv/Scripts/python.exe tools/diagnostics/fill_prober.py \
      [--db data/combomaker-prod-live-wc.sqlite3] [--interval 30]
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.ops.dotenv import load_dotenv

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
POLL_S = 2.0
POLLS = 11  # ~22s inside the ~30s RFQ TTL
WATERMARK = Path("data/fill_prober_watermark.txt")


def _new_fills(db: Path, after: str) -> list[tuple[str, str, str, int, int]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            "select at, combo_ticker, our_side, price_cc, contracts_centi "
            "from fills where at > ? order by at",
            (after,),
        ).fetchall()
    finally:
        con.close()


async def _probe(
    rest: KalshiRestClient, ticker: str, our_side: str, our_cc: int, ct: int
) -> None:
    print(f"\nPROBE {ticker}  our fill: {our_side.upper()} @ "
          f"{our_cc / 100:.2f}c x {ct / 100:.2f}ct", flush=True)
    try:
        r = await rest.create_rfq(
            ticker, target_cost_dollars="25.00",
            rest_remainder=False, replace_existing=True,
        )
    except Exception as e:  # noqa: BLE001 — probe is best-effort
        print(f"  create_rfq FAILED: {e}", flush=True)
        return
    rfq = r.get("rfq", r)
    rfq_id = str(rfq.get("id") or rfq.get("rfq_id") or r.get("id"))
    seen: dict[str, dict] = {}
    for _ in range(POLLS):
        await asyncio.sleep(POLL_S)
        try:
            q = await rest.get_quotes(rfq_id=rfq_id, rfq_user_filter="self")
        except Exception as e:  # noqa: BLE001
            print(f"  get_quotes err: {e}", flush=True)
            continue
        for quote in q.get("quotes", []):
            qid = str(quote.get("id") or quote.get("quote_id"))
            seen[qid] = quote
    try:
        await rest.delete_rfq(rfq_id)
    except Exception:  # noqa: BLE001 — TTL cleans up anyway
        pass
    if not seen:
        print("  no competing quotes inside the window", flush=True)
        return
    best_no: float | None = None
    for quote in seen.values():
        raw = quote.get("no_bid_dollars") or quote.get("no_bid")
        try:
            no_cents = float(str(raw)) * 100.0
        except (TypeError, ValueError):
            continue
        if no_cents > 0 and (best_no is None or no_cents > best_no):
            best_no = no_cents
    if best_no is None:
        print(f"  {len(seen)} quotes, none with a readable NO bid", flush=True)
        return
    rich = our_cc / 100.0 - best_no
    print(
        f"  RICHNESS {rich:+.2f}c  (ours {our_cc / 100:.2f}c vs best-other NO "
        f"{best_no:.2f}c, {len(seen)} makers)",
        flush=True,
    )


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/combomaker-prod-live-wc.sqlite3")
    ap.add_argument("--interval", type=float, default=30.0)
    a = ap.parse_args()
    load_dotenv()
    watermark = (
        WATERMARK.read_text().strip() if WATERMARK.exists() else "2026-07-25T15:35"
    )
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    async with KalshiRestClient(KALSHI, signer) as rest:
        while True:
            try:
                rows = _new_fills(Path(a.db), watermark)
            except Exception as e:  # noqa: BLE001
                print(f"store read err: {e}", flush=True)
                rows = []
            for at, ticker, side, price_cc, ct in rows:
                await _probe(rest, ticker, side, int(price_cc), int(ct))
                watermark = at
                WATERMARK.write_text(watermark)
            await asyncio.sleep(a.interval)


if __name__ == "__main__":
    asyncio.run(main())
