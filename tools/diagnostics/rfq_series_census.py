"""RFQ series-demand census — which sports do takers actually request?

TWO MODES, read-only either way:

- default (REST, the DEMAND DETECTOR): authed ``GET /communications/rfqs
  ?status=open`` paged to exhaustion — the exchange's OWN open combo-RFQ set,
  exchange-wide. This is the ONLY honest demand measure we have.
- ``--store``: sampled census of the local ``rfqs`` table (mode=ro). CAVEAT
  (2026-08-12 RETRACTION): the intake firehose gate (rfq/intake.py:186,
  ``rfq.dropped_series_fastpath``) drops every RFQ carrying a non-allowlisted
  leg BEFORE the recorder — the store therefore measures the WITHIN-allowlist
  mix only and can NEVER see outside demand. The first version of this tool
  claimed otherwise and concluded "zero latent demand"; the REST probe showed
  ~25%+ of the open universe on non-allowlisted soccer/tennis/WNBA legs. See
  docs/reports/2026-08-12-soccer-league-validation-and-rfq-demand-retraction.md.

Cadence: weekly + at every league opener. A non-allowlisted prefix with real
flow = start docs/sport_onboarding_playbook.md for that sport.

Usage (repo root):
  .venv/Scripts/python.exe tools/diagnostics/rfq_series_census.py
  .venv/Scripts/python.exe tools/diagnostics/rfq_series_census.py --store
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

# Keep in sync with filters.allowed_leg_series_prefixes in the live config —
# read here only to LABEL the census (this tool never touches the filter).
DEFAULT_ALLOWED = (
    "KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD", "KXMLBKS", "KXMLBHIT", "KXMLBHR",
    "KXMLBHRR", "KXMLBRFI", "KXMLBTB", "KXMLBSB", "KXMLBOUTS", "KXMLBRBI",
    "KXLOLGAME", "KXCS2GAME", "KXCSGOGAME",
)

PROD_REST = "https://external-api.kalshi.com/trade-api/v2"


def _print_census(series: Counter, outside_sig: Counter, n: int, what: str) -> None:
    print(f"\n{what}: {n:,} RFQs")
    print("leg series prefixes:")
    for p, c in series.most_common():
        mark = "" if p in DEFAULT_ALLOWED else "   <-- NOT ALLOWLISTED"
        print(f"  {p:34s} {c:8,d}{mark}")
    if outside_sig:
        print("\nNON-ALLOWLISTED signatures (top 30):")
        for sig, c in outside_sig.most_common(30):
            print(f"  {sig:64s} {c:8,d}")
        print("\n>>> demand outside the allowlist: consider the onboarding "
              "playbook for the prefixes above.")
    else:
        print("\nno non-allowlisted legs seen in this view.")


async def rest_mode() -> int:
    sys.path.insert(0, str(Path("src").resolve()))
    from combomaker.core.clock import SystemClock
    from combomaker.exchange.auth import Credentials, RequestSigner
    from combomaker.exchange.rest import KalshiRestClient
    from combomaker.ops.dotenv import load_dotenv

    load_dotenv(Path(".env"))
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    async with KalshiRestClient(PROD_REST, signer) as rest:
        rows, cursor = [], ""
        for _ in range(200):
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
        series: Counter = Counter()
        outside_sig: Counter = Counter()
        no_legs = 0
        for r in rows:
            legs = r.get("mve_selected_legs") or []
            if not legs:
                no_legs += 1  # residual bucket: named, never silent
                continue
            prefixes = {
                (leg.get("event_ticker") or leg.get("market_ticker") or "")
                .split("-", 1)[0]
                for leg in legs
            }
            prefixes.discard("")
            for p in prefixes:
                series[p] += 1
            outside = sorted(p for p in prefixes if p not in DEFAULT_ALLOWED)
            if outside:
                outside_sig[",".join(outside)] += 1
        _print_census(series, outside_sig, len(rows),
                      "EXCHANGE open combo-RFQ set (REST, exchange-wide)")
        if no_legs:
            print(f"\nresidual: {no_legs} open RFQs with NO legs field "
                  "(single-market RFQs?) — inspect before dismissing.")
    return 0


def store_mode(db: str, rows: int, sample: int) -> int:
    if not Path(db).is_file():
        print(f"no such db: {db}", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        (max_id,) = con.execute("SELECT MAX(id) FROM rfqs").fetchone()
        lo = max(0, max_id - rows)
        series: Counter = Counter()
        n = residual = 0
        for (legs_json,) in con.execute(
            "SELECT legs_json FROM rfqs WHERE id > ? AND (id % ?) = 0",
            (lo, sample),
        ):
            n += 1
            try:
                legs = json.loads(legs_json)
            except (TypeError, json.JSONDecodeError):
                residual += 1
                continue
            prefixes = {
                (leg.get("ticker") or leg.get("market_ticker") or "")
                .split("-", 1)[0]
                for leg in legs
            }
            prefixes.discard("")
            if not prefixes:
                residual += 1
                continue
            for p in prefixes:
                series[p] += 1
        _print_census(series, Counter(), n,
                      f"LOCAL store sample (1/{sample} of newest {rows:,} rows)")
        print(f"\nresidual rows (unparseable/no-prefix): {residual}")
        print("\nREMINDER: the store sees only allowlisted RFQs (intake "
              "firehose gate) — this is the within-allowlist MIX, not demand.")
        return 0
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", action="store_true",
                    help="census the local store instead (within-allowlist mix only)")
    ap.add_argument("--db", default="data/combomaker-prod-live-wc.sqlite3")
    ap.add_argument("--rows", type=int, default=8_000_000)
    ap.add_argument("--sample", type=int, default=40)
    args = ap.parse_args()
    if args.store:
        return store_mode(args.db, args.rows, args.sample)
    return asyncio.run(rest_mode())


if __name__ == "__main__":
    raise SystemExit(main())
