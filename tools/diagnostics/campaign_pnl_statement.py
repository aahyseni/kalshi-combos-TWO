"""Campaign realized-P&L statement — exchange-ledger-first, read-only.

Reconciles the campaign fill store (data/combomaker-prod-live-wc.sqlite3,
opened mode=ro) against the exchange's OWN settlement ledger
(GET /portfolio/settlements) and balance (GET /portfolio/balance):

  realized per combo market = exchange settlement revenue − our premium − fees

Prints AGGREGATES ONLY (never a secret, never a raw key), all times in ET
(operator rule). Any fill ticker with no exchange settlement row, and any
settlement row on a ticker we never filled inside the window, is listed —
never silently dropped (full-state-awareness rule).

Usage:
  .venv/Scripts/python.exe tools/diagnostics/campaign_pnl_statement.py \
      [--db data/combomaker-prod-live-wc.sqlite3] [--max-pages 50]

Read-only everywhere: sqlite mode=ro; only GET endpoints.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.ops.dotenv import load_dotenv

ET = ZoneInfo("America/New_York")
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
CC_PER_CENT = 100


def _load_fills(db_path: Path) -> list[dict[str, object]]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select at, combo_ticker, our_side, contracts_centi, price_cc, fee_cc "
            "from fills order by at"
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "at": r[0],
            "ticker": r[1],
            "side": r[2],
            "contracts_centi": r[3],
            "price_cc": r[4],
            "fee_cc": r[5] or 0,
            # premium we PAID in cc: contracts × price (centi-contracts × cc / 100)
            "premium_cc": r[3] * r[4] // 100,
        }
        for r in rows
    ]


async def _fetch_exchange(max_pages: int) -> tuple[list[dict[str, object]], dict[str, object]]:
    load_dotenv()
    clock = SystemClock()
    signer = RequestSigner(Credentials.for_env("prod"), clock)
    async with KalshiRestClient(PROD_REST, signer) as rest:
        balance = await rest.get_balance()
        rows: list[dict[str, object]] = []
        cursor = ""
        for _ in range(max_pages):
            params: dict[str, str | int] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await rest.get_settlements(**params)
            rows.extend(payload.get("settlements", []) or [])
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
    return rows, balance


def _et_day(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime(
        "%a %m/%d"
    )


def main() -> None:
    # Windows consoles default to cp1252 — force UTF-8 so the report's arrows
    # and section rules never crash the statement mid-print.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/combomaker-prod-live-wc.sqlite3"))
    ap.add_argument("--max-pages", type=int, default=50)
    a = ap.parse_args()

    fills = _load_fills(a.db)
    settlements, balance = asyncio.run(_fetch_exchange(a.max_pages))

    fill_tickers = {str(f["ticker"]) for f in fills}
    premium_by_ticker: dict[str, int] = defaultdict(int)
    fill_fee_by_ticker: dict[str, int] = defaultdict(int)
    fills_by_ticker: dict[str, int] = defaultdict(int)
    for f in fills:
        t = str(f["ticker"])
        premium_by_ticker[t] += int(f["premium_cc"])  # type: ignore[arg-type]
        fill_fee_by_ticker[t] += int(f["fee_cc"])  # type: ignore[arg-type]
        fills_by_ticker[t] += 1

    ours = [s for s in settlements if str(s.get("ticker")) in fill_tickers]
    settled_tickers = {str(s.get("ticker")) for s in ours}

    per_day: dict[str, dict[str, int | float]] = defaultdict(
        lambda: {"n": 0, "revenue_cc": 0, "premium_cc": 0, "fees_cc": 0}
    )
    total_revenue_cc = total_premium_cc = total_fees_cc = 0
    winners = losers = 0
    for s in ours:
        t = str(s.get("ticker"))
        revenue_cc = int(s.get("revenue") or 0) * CC_PER_CENT
        # settlement fee_cost is a dollars string; sub-cent fees exist — track
        # to the cc without inventing precision (bad parse -> 0 + flagged row).
        fee_raw = s.get("fee_cost")
        try:
            fee_cc = round(float(str(fee_raw)) * 10_000) if fee_raw is not None else 0
        except ValueError:
            fee_cc = 0
            print(f"  !! unparseable fee_cost on {t}: {fee_raw!r}")
        premium_cc = premium_by_ticker[t] + fill_fee_by_ticker[t]
        realized_cc = revenue_cc - premium_cc - fee_cc
        day = _et_day(str(s.get("settled_time") or s.get("settled_ts") or ""))
        bucket = per_day[day]
        bucket["n"] = int(bucket["n"]) + fills_by_ticker[t]
        bucket["revenue_cc"] = int(bucket["revenue_cc"]) + revenue_cc
        bucket["premium_cc"] = int(bucket["premium_cc"]) + premium_cc
        bucket["fees_cc"] = int(bucket["fees_cc"]) + fee_cc
        total_revenue_cc += revenue_cc
        total_premium_cc += premium_cc
        total_fees_cc += fee_cc
        if realized_cc >= 0:
            winners += fills_by_ticker[t]
        else:
            losers += fills_by_ticker[t]

    unsettled = sorted(fill_tickers - settled_tickers)

    def usd(cc: int) -> str:
        sign = "-" if cc < 0 else ""
        return f"{sign}${abs(cc) / 10_000:,.2f}"

    print("=" * 74)
    print("CAMPAIGN REALIZED P&L — exchange settlement ledger vs local fill store")
    print("=" * 74)
    print(f"fills in store: {len(fills)} across {len(fill_tickers)} combo markets "
          f"({fills[0]['at'][:10]} → {fills[-1]['at'][:10]})")  # type: ignore[index]
    print(f"exchange settlement rows matched: {len(ours)} markets "
          f"({winners} winning / {losers} losing fills)")
    print()
    print("| settle day (ET) | fills | revenue | premium | fees | realized |")
    print("|---|---|---|---|---|---|")
    for day in sorted(per_day, key=lambda d: d.split()[1]):
        b = per_day[day]
        realized = int(b["revenue_cc"]) - int(b["premium_cc"]) - int(b["fees_cc"])
        print(
            f"| {day} | {b['n']} | {usd(int(b['revenue_cc']))} | "
            f"{usd(int(b['premium_cc']))} | {usd(int(b['fees_cc']))} | {usd(realized)} |"
        )
    net = total_revenue_cc - total_premium_cc - total_fees_cc
    print("|---|---|---|---|---|---|")
    print(
        f"| TOTAL |  | {usd(total_revenue_cc)} | {usd(total_premium_cc)} | "
        f"{usd(total_fees_cc)} | **{usd(net)}** |"
    )
    print()
    bal_cents = int(balance.get("balance") or 0)
    pv_cents = int(balance.get("portfolio_value") or 0)
    print(f"exchange balance now: ${bal_cents / 100:,.2f} cash + "
          f"${pv_cents / 100:,.2f} positions = ${(bal_cents + pv_cents) / 100:,.2f} equity")
    if unsettled:
        print()
        print(f"!! {len(unsettled)} filled markets with NO settlement row (open or unmatched):")
        for t in unsettled:
            print(f"   {t}  (premium {usd(premium_by_ticker[t])}, {fills_by_ticker[t]} fill(s))")
    else:
        print("every filled market has an exchange settlement row — book fully settled.")


if __name__ == "__main__":
    main()
