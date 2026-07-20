"""Full-account standing — the exchange's OWN all-time ledger, read-only.

Answers "what is our TRUE standing" from nothing but exchange history — no
local store required, so it covers every era: pre-bot manual combos, old bot
runs, the WC campaign, everything (operator rule 2026-07-21: the bot must know
its standing, positions, and balance even for what happened before it went
live).

The account identity it reconciles TO THE CENT:

    applied deposits − applied withdrawals + Σ settlement realized
        − (anything not in settlement rows, e.g. order-time trading fees)
        ≡ current balance + open-position cost

Per settlement row the exchange gives BOTH sides' cost basis
(``yes_total_cost_dollars`` / ``no_total_cost_dollars``), the gross
``revenue`` (cents) and the settlement ``fee_cost`` (dollars string):

    realized = revenue − yes_cost − no_cost − fee

Prints aggregates only; all times ET. Any unparseable row is listed, never
silently dropped.

Usage:
  .venv/Scripts/python.exe tools/diagnostics/account_standing.py [--era-split 2026-07-14]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from combomaker.core.clock import SystemClock
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiRestClient
from combomaker.ops.dotenv import load_dotenv

ET = ZoneInfo("America/New_York")
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"


async def _page(rest: KalshiRestClient, method: str, key: str, max_pages: int = 100) -> list[dict]:
    rows: list[dict] = []
    cursor = ""
    for _ in range(max_pages):
        params: dict[str, str | int] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = await getattr(rest, method)(**params)
        rows.extend(payload.get(key) or [])
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    return rows


async def _fetch() -> dict[str, object]:
    load_dotenv()
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    async with KalshiRestClient(PROD_REST, signer) as rest:
        return {
            "balance": await rest.get_balance(),
            "settlements": await _page(rest, "get_settlements", "settlements"),
            "deposits": await _page(rest, "get_deposits", "deposits"),
            "withdrawals": await _page(rest, "get_withdrawals", "withdrawals"),
            "positions": await rest.get_positions(),
        }


def _dollars_cents(raw: object) -> int:
    """Exact ``*_dollars`` fixed-point string → int cents (never float)."""
    if raw is None:
        return 0
    return int((Decimal(str(raw)) * 100).to_integral_value())


def _sett_time(s: dict) -> datetime | None:
    raw = s.get("settled_time")
    if not raw:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser()
    ap.add_argument("--era-split", default="2026-07-14",
                    help="ET date splitting 'before the current store' vs after")
    a = ap.parse_args()

    data = asyncio.run(_fetch())
    settlements: list[dict] = data["settlements"]  # type: ignore[assignment]
    deposits: list[dict] = data["deposits"]  # type: ignore[assignment]
    withdrawals: list[dict] = data["withdrawals"]  # type: ignore[assignment]
    balance: dict = data["balance"]  # type: ignore[assignment]
    positions: dict = data["positions"]  # type: ignore[assignment]

    split_day = a.era_split

    by_era = defaultdict(lambda: [0, 0, 0, 0])  # n, revenue_c, cost_c, fee_c
    bad_rows: list[str] = []
    for s in settlements:
        t = _sett_time(s)
        if t is None:
            era = "unknown-date"  # never silently bucketed (review F9)
        else:
            day = t.astimezone(ET).strftime("%Y-%m-%d")
            era = "pre-store" if day < split_day else "store-era"
        try:
            revenue_c = int(s.get("revenue") or 0)
            cost_c = _dollars_cents(s.get("yes_total_cost_dollars")) + _dollars_cents(
                s.get("no_total_cost_dollars")
            )
            fee_c = _dollars_cents(s.get("fee_cost"))
        except (ValueError, ArithmeticError) as exc:
            bad_rows.append(f"{s.get('ticker')}: {exc!r}")
            continue
        b = by_era[era]
        b[0] += 1
        b[1] += revenue_c
        b[2] += cost_c
        b[3] += fee_c

    dep_applied_c = sum(
        int(d.get("amount_cents") or 0)
        for d in deposits
        if str(d.get("status")) == "applied"
    )
    dep_fees_c = sum(
        int(d.get("fee_cents") or 0)
        for d in deposits
        if str(d.get("status")) == "applied"
    )
    wd_applied_c = sum(
        int(w.get("amount_cents") or 0)
        for w in withdrawals
        if str(w.get("status")) in ("applied", "complete", "completed")
    )

    bal_c = int(balance.get("balance") or 0)
    pv_c = int(balance.get("portfolio_value") or 0)
    open_positions = [
        p
        for p in (positions.get("market_positions") or [])
        if int(p.get("position") or 0) != 0
    ]

    def usd(c: int) -> str:
        sign = "-" if c < 0 else ""
        return f"{sign}${abs(c) / 100:,.2f}"

    print("=" * 74)
    print("FULL-ACCOUNT STANDING — exchange ledger only (all eras)")
    print("=" * 74)
    print(f"deposits: {len(deposits)} rows, applied {usd(dep_applied_c)}"
          + (f" (fees {usd(dep_fees_c)})" if dep_fees_c else ""))
    print(f"withdrawals: {len(withdrawals)} rows, applied {usd(wd_applied_c)}")
    print()
    print(f"| era (split {split_day} ET) | settlements | revenue | cost basis | fees | realized |")
    print("|---|---|---|---|---|---|")
    tot = [0, 0, 0, 0]
    eras = ["pre-store", "store-era"]
    if "unknown-date" in by_era:
        eras.append("unknown-date")  # never silently bucketed (review F9)
    for era in eras:
        n, rev, cost, fee = by_era[era]
        realized = rev - cost - fee
        print(f"| {era} | {n} | {usd(rev)} | {usd(cost)} | {usd(fee)} | {usd(realized)} |")
        for i, v in enumerate((n, rev, cost, fee)):
            tot[i] += v
    all_realized_c = tot[1] - tot[2] - tot[3]
    print(
        f"| **ALL-TIME** | {tot[0]} | {usd(tot[1])} | {usd(tot[2])} | "
        f"{usd(tot[3])} | **{usd(all_realized_c)}** |"
    )
    print()
    print(
        f"balance now: {usd(bal_c)} cash + {usd(pv_c)} positions "
        f"({len(open_positions)} open) = {usd(bal_c + pv_c)} equity"
    )
    print()
    identity_c = dep_applied_c - wd_applied_c + all_realized_c
    residual_c = (bal_c + pv_c) - identity_c
    print("account identity: deposits − withdrawals + all-time realized "
          f"= {usd(identity_c)}")
    print(f"vs current equity {usd(bal_c + pv_c)} → residual {usd(residual_c)}")
    if residual_c == 0:
        print("RECONCILED TO THE CENT.")
    else:
        print("residual = costs not in settlement rows (order-time trading fees, "
              "open-position cost basis if any, in-flight transfers) — list open "
              "positions above and pull /portfolio/fills fees to attribute.")
    if bad_rows:
        print()
        print(f"!! {len(bad_rows)} unparseable settlement rows:")
        for r in bad_rows:
            print(f"   {r}")


if __name__ == "__main__":
    main()
