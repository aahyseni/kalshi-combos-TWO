"""Club-soccer wiring probe (2026-08-13) â€” validate-caps-can-quote, offline.

Proves the armed config + new code produce REAL quotes on REAL club-soccer
RFQs before the restart (the 2026-07-23 rule: a wiring that passes every
safety test but quotes nothing is useless). READ-ONLY: open RFQs come from
GET /communications/rfqs, leg mids from GET /markets/{ticker}; the engine
runs on the replay harness (FakeWs books seeded at live mids) â€” no order,
quote, or RFQ is ever created.

Also doubles as the MLB no-drift differential: --mlb prices a fixed set of
synthetic MLB combos at FIXED mids; run it once on main and once on the
wiring branch â€” outputs must be byte-identical.

Run (worktree root, PYTHONPATH=<worktree>/src):
  python -m tools.backtests.club_soccer_quote_probe          # club probe
  python -m tools.backtests.club_soccer_quote_probe --mlb    # differential
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.core.conventions import DOC_ASSUMED
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.metadata import EventMeta, MarketMeta, MetadataCache
from combomaker.ops.config import load_config
from combomaker.ops.dotenv import load_dotenv
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.models import Rfq
from tests.test_feed import FakeWs, snapshot_env  # replay-harness seam (rule 8b)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
CLUB_SERIES = (
    "KXLALIGAGAME-", "KXLALIGATOTAL-", "KXLALIGASPREAD-", "KXLALIGABTTS-",
    "KXMLSGAME-", "KXMLSTOTAL-", "KXMLSSPREAD-", "KXMLSBTTS-",
    "KXUECLGAME-", "KXUECLTOTAL-", "KXUECLSPREAD-", "KXUECLBTTS-",
    "KXUECLADVANCE-",
)

# Fixed differential rows (--mlb): synthetic combos on real MLB series shapes,
# FIXED mids so two code trees must print byte-identical results.
MLB_DIFF_ROWS = [
    {
        "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-SDIFF1-AAA",
        "legs": [
            ("KXMLBGAME-26AUG141910BOSNYY-NYY", "yes"),
            ("KXMLBTOTAL-26AUG141910BOSNYY-9", "yes"),
        ],
        "mids": {
            "KXMLBGAME-26AUG141910BOSNYY-NYY": 0.55,
            "KXMLBTOTAL-26AUG141910BOSNYY-9": 0.48,
        },
    },
    {
        "ticker": "KXMVECROSSCATEGORY-SDIFF2-BBB",
        "legs": [
            ("KXMLBKS-26AUG141910BOSNYY-NYYGCOLE59-6", "yes"),
            ("KXMLBGAME-26AUG141905SEATEX-SEA", "yes"),
            ("KXMLBHR-26AUG141905SEATEX-SEAJRODRIGUEZ44-1", "yes"),
        ],
        "mids": {
            "KXMLBKS-26AUG141910BOSNYY-NYYGCOLE59-6": 0.42,
            "KXMLBGAME-26AUG141905SEATEX-SEA": 0.51,
            "KXMLBHR-26AUG141905SEATEX-SEAJRODRIGUEZ44-1": 0.18,
        },
    },
]


def _event_of(market_ticker: str) -> str:
    parts = market_ticker.split("-")
    return "-".join(parts[:-1]) if len(parts) >= 3 else market_ticker


async def _seed(
    mids: dict[str, float], combo_ticker: str
) -> tuple[OrderbookFeed, MetadataCache, FakeClock]:
    from datetime import UTC, datetime

    clock = FakeClock(start=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    ws = FakeWs()
    feed = OrderbookFeed(ws, clock)
    metadata = MetadataCache(None, clock)  # type: ignore[arg-type]
    feed.watch(list(mids))
    await ws.ack_subscription(0, 5)
    for i, (ticker, mid) in enumerate(mids.items()):
        mid_cc = int(round(mid * 10_000))
        yes_bid = max(mid_cc - 50, 100)
        no_bid = max(10_000 - mid_cc - 50, 100)
        env = snapshot_env(5, i + 1, ticker)
        env["msg"]["yes_dollars_fp"] = [[f"{yes_bid / 10_000:.4f}", "500.00"]]
        env["msg"]["no_dollars_fp"] = [[f"{no_bid / 10_000:.4f}", "500.00"]]
        await ws.deliver(env)
    for ticker in mids:
        event = _event_of(ticker)
        metadata._events[event] = EventMeta(  # noqa: SLF001 (harness seam)
            event_ticker=event,
            mutually_exclusive=False,
            raw={},
            fetched_mono_ns=clock.monotonic_ns(),
        )
    metadata._markets[combo_ticker] = MarketMeta(  # noqa: SLF001 (harness seam)
        ticker=combo_ticker,
        status="active",
        grid=PriceGrid.from_market_payload(
            {
                "ticker": combo_ticker,
                "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
            }
        ),
        event_ticker="E",
        close_time=clock.now() + timedelta(hours=6),
        expected_expiration_time=None,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )
    return feed, metadata, clock


def _rfq(
    combo_ticker: str,
    legs: list[tuple[str, str]],
    contracts: str,
    target_cost: str | None = None,
) -> Rfq:
    msg: dict[str, object] = {
        "id": "rfq_probe",
        "market_ticker": combo_ticker,
        "created_ts": "2026-08-13T12:00:00Z",
        "mve_collection_ticker": combo_ticker.split("-", 1)[0],
        "mve_selected_legs": [
            {"market_ticker": mt, "side": side, "event_ticker": _event_of(mt)}
            for mt, side in legs
        ],
    }
    # Mirror the wire: contracts-mode RFQs carry contracts_fp, target-cost
    # RFQs carry target_cost_dollars (contracts absent/zero).
    if target_cost is not None and float(contracts or 0) == 0:
        msg["target_cost_dollars"] = target_cost
    else:
        msg["contracts_fp"] = contracts
    return Rfq.from_ws(msg)


def _fmt(result: ConstructedQuote | NoQuote) -> str:
    if isinstance(result, NoQuote):
        return f"NOQUOTE {result.reason.value} ({result.detail})"
    width = dict(sorted(result.width_components_cc.items()))
    return (
        f"QUOTE no_bid={result.no_bid_cc}cc yes_bid={result.yes_bid_cc}cc "
        f"fair={result.fair_cc}cc farmed={result.farmed} width={width}"
    )


async def probe_club() -> int:
    load_dotenv(REPO_ROOT / ".env")
    cfg = load_config(REPO_ROOT / "config" / "prod-live-wc.local.yaml")
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    async with KalshiRestClient(PROD_REST, signer) as rest:
        rows, cursor = [], ""
        for _ in range(40):
            params: dict[str, str | int] = {"limit": 200, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            payload = await rest.get_rfqs(**params)
            batch = payload.get("rfqs") or []
            rows.extend(batch)
            cursor = str(payload.get("cursor") or "")
            if not cursor or not batch:
                break
            await asyncio.sleep(0.15)
        club = [
            r
            for r in rows
            if r.get("mve_selected_legs")
            and all(
                str(leg.get("market_ticker", "")).startswith(CLUB_SERIES)
                for leg in r["mve_selected_legs"]
            )
        ]
        print(f"open RFQs: {len(rows)}; PURE-club-leg RFQs: {len(club)}")
        # Diverse sample: prefer multi-family and ADVANCE-carrying shapes.
        club.sort(
            key=lambda r: (
                any(
                    "ADVANCE" in str(leg.get("market_ticker", ""))
                    for leg in r["mve_selected_legs"]
                ),
                len({
                    str(leg.get("market_ticker", "")).split("-", 1)[0]
                    for leg in r["mve_selected_legs"]
                }),
            ),
            reverse=True,
        )
        sample = club[:14]
        verdicts: Counter[str] = Counter()
        quoted = 0
        for r in sample:
            legs = [
                (str(leg["market_ticker"]), str(leg.get("side", "yes")))
                for leg in r["mve_selected_legs"]
            ]
            mids: dict[str, float] = {}
            usable = True
            for mt, _ in legs:
                m = (await rest.get_market(mt)).get("market") or {}
                yes_bid = m.get("yes_bid_dollars") or m.get("yes_bid")
                yes_ask = m.get("yes_ask_dollars") or m.get("yes_ask")
                try:
                    b = float(yes_bid)
                    a = float(yes_ask)
                    if b > 1.5 or a > 1.5:  # cents payload, not dollars
                        b, a = b / 100.0, a / 100.0
                except (TypeError, ValueError):
                    usable = False
                    break
                if not (0.0 < b < 1.0 and 0.0 < a <= 1.0 and b <= a):
                    usable = False
                    break
                mids[mt] = (b + a) / 2.0
                await asyncio.sleep(0.12)
            if not usable:
                verdicts["no_live_leg_book"] += 1
                continue
            feed, metadata, _ = await _seed(mids, str(r["market_ticker"]))
            engine = PricingEngine(feed, metadata, DOC_ASSUMED, cfg.pricing)
            contracts = str(r.get("contracts_fp") or "0")
            target_cost = r.get("target_cost_dollars")
            result = engine.price(
                _rfq(
                    str(r["market_ticker"]),
                    legs,
                    contracts,
                    target_cost=str(target_cost) if target_cost else None,
                ),
                time_to_close_s=3 * 3600.0,
                in_play=False,
            )
            tag = (
                "QUOTED"
                if isinstance(result, ConstructedQuote)
                else result.reason.value
            )
            verdicts[tag] += 1
            if isinstance(result, ConstructedQuote):
                quoted += 1
            fams = ",".join(
                sorted({mt.split("-", 1)[0].removeprefix("KX") for mt, _ in legs})
            )
            print(f"\n[{fams}] {len(legs)}-leg {contracts}ct")
            for mt, side in legs:
                print(f"    {side:3s} {mt}  mid={mids[mt]:.3f}")
            print(f"  -> {_fmt(result)}")
        print(f"\nverdicts: {dict(verdicts)}")
        print(f"QUOTED {quoted}/{len(sample)} sampled pure-club RFQs")
        return 0 if quoted > 0 else 1


async def probe_mlb_differential() -> int:
    cfg = load_config(REPO_ROOT / "config" / "prod-live-wc.local.yaml")
    for row in MLB_DIFF_ROWS:
        feed, metadata, _ = await _seed(row["mids"], row["ticker"])
        engine = PricingEngine(feed, metadata, DOC_ASSUMED, cfg.pricing)
        result = engine.price(
            _rfq(row["ticker"], row["legs"], "100.00"),
            time_to_close_s=3 * 3600.0,
            in_play=False,
        )
        print(f"{row['ticker']}: {_fmt(result)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mlb", action="store_true", help="fixed-mid MLB differential")
    args = ap.parse_args()
    if args.mlb:
        return asyncio.run(probe_mlb_differential())
    return asyncio.run(probe_club())


if __name__ == "__main__":
    sys.exit(main())
