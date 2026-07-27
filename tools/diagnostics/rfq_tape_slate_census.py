"""READ-ONLY census of the live RFQ tape by GAME SLOT.

Answers "are there any RFQs at all on the remaining pregame games, and how
deep in the tape do they start?" — the prerequisite for driving the reservation
path on tonight's late shapes.

GETs only (/rfqs paging). Nothing written, no orders.

    .venv/Scripts/python.exe tools/diagnostics/rfq_tape_slate_census.py --pages 25
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from combomaker.core.clock import SystemClock  # noqa: E402
from combomaker.exchange.auth import Credentials, RequestSigner  # noqa: E402
from combomaker.exchange.rest import KalshiRestClient  # noqa: E402
from combomaker.ops.dotenv import load_dotenv  # noqa: E402
from combomaker.rfq.models import Rfq, RfqParseError  # noqa: E402

PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
SLOT = re.compile(r"-(\d{2}[A-Z]{3}\d{2})(\d{4})([A-Z]+)")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=25)
    ap.add_argument("--min-start", type=int, default=2100)
    args = ap.parse_args()

    load_dotenv()
    signer = RequestSigner(Credentials.for_env("prod"), SystemClock())
    rfqs: dict[str, Rfq] = {}
    first_seen_page: dict[str, int] = {}
    async with KalshiRestClient(PROD_REST, signer) as rest:
        cursor = ""
        for page in range(args.pages):
            params: dict[str, object] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await rest.get_rfqs(**params)  # type: ignore[arg-type]
            rows = payload.get("rfqs") or []
            for row in rows:
                try:
                    r = Rfq.from_ws(row)
                except RfqParseError:
                    continue
                if not r.is_combo:
                    continue
                rfqs[r.rfq_id] = r
                for t in r.leg_tickers:
                    m = SLOT.search(t)
                    if m:
                        key = f"{m.group(2)}{m.group(3)}"
                        first_seen_page.setdefault(key, page)
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                print(f"  tape exhausted after page {page} ({len(rfqs)} combo RFQs)")
                break

    print(f"combo RFQs harvested: {len(rfqs)}")
    slots: Counter[str] = Counter()
    all_legs_late = 0
    for r in rfqs.values():
        ks = set()
        ok = True
        for t in r.leg_tickers:
            m = SLOT.search(t)
            if not m:
                ok = False
                continue
            ks.add(f"{m.group(2)}{m.group(3)}")
        for k in ks:
            slots[k] += 1
        if ok and ks and all(int(k[:4]) >= args.min_start for k in ks):
            all_legs_late += 1

    print(f"\nRFQs whose EVERY leg starts >= {args.min_start}: {all_legs_late}")
    print("\n  slot          RFQs touching   first seen on page")
    for k, n in sorted(slots.items(), key=lambda kv: kv[0]):
        late = "  <-- LATE" if int(k[:4]) >= args.min_start else ""
        print(f"  {k:<14}{n:>10}{first_seen_page.get(k, -1):>19}{late}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
