"""PARITY check for the off-loop ProcessPool joint path (Phase 1).

Proves engine.price() (inline) == engine.price_offloaded(run_joint=pool) to the
centi-cent on real tape combos — i.e. running the joint in a worker PROCESS does
not move the $ we quote, only where the CPU runs. Uses a generous deadline so
nothing is dropped (drop-on-deadline is the wedge guarantee, tested operationally,
not a pricing change).

Two engines share ONE primed feed (separate memos so the offloaded engine always
MISSES and therefore actually exercises the pool worker):
  engine_ref  : price()            inline reference
  engine_pool : price_offloaded()  miss -> ProcessPool worker

Usage:  uv run python tools/pool_parity_check.py [n_per_bucket]
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools" / "backtests"))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

from combomaker.core.conventions import DOC_ASSUMED  # noqa: E402
from combomaker.ops.config import PricingConfig  # noqa: E402
from combomaker.ops.pricing_pool import JointPool  # noqa: E402
from combomaker.pricing.engine import PricingEngine  # noqa: E402
from combomaker.pricing.quote import ConstructedQuote, NoQuote  # noqa: E402


def _fair(r):  # noqa: ANN001, ANN202
    if isinstance(r, ConstructedQuote):
        return ("quote_farmed" if r.farmed else "quote", int(r.fair_cc),
                int(r.yes_bid_cc), int(r.no_bid_cc))
    assert isinstance(r, NoQuote)
    return ("noquote", r.reason.name, None, None)


async def _amain(n_per_bucket: int) -> None:
    import profile_pricer as pp

    print(f"loading combos ({n_per_bucket}/bucket)...", flush=True)
    sample = pp._load_sample(n_per_bucket)
    ws, clock, feed, metadata, engine_ref = await pp._build_engine()  # inline reference
    engine_pool = PricingEngine(feed, metadata, DOC_ASSUMED, PricingConfig())  # off-loop path

    pool = JointPool(PricingConfig(), DOC_ASSUMED, workers=2, deadline_s=15.0)  # generous: no drops
    pool.start()
    await pool.warmup()
    print(f"pool warm; comparing {len(sample)} combos inline vs off-loop...", flush=True)

    mism = 0
    n = 0
    off_ms: list[float] = []
    for i, (bucket, legs, sides, yes_cc, nlegs) in enumerate(sample):
        await pp._prime_books(ws, feed, metadata, legs, yes_cc, sid=7000 + i)
        rfq = pp._mk_rfq(legs, sides)
        ref = _fair(engine_ref.price(rfq, time_to_close_s=100_000))
        t0 = time.perf_counter()
        pooled = _fair(await engine_pool.price_offloaded(
            rfq, time_to_close_s=100_000, run_joint=pool.run_joint))
        off_ms.append((time.perf_counter() - t0) * 1000)
        n += 1
        if ref != pooled:
            mism += 1
            if mism <= 10:
                print(f"  MISMATCH {'-'.join(legs[:2])}...  inline={ref}  offloop={pooled}")

    pool.shutdown()
    off_ms.sort()
    print("\n==== OFF-LOOP POOL PARITY ====")
    print(f"  combos compared: {n}")
    print(f"  mismatches (inline vs off-loop quote cents): {mism}   "
          f"{'✅ EXACT PARITY' if mism == 0 else '❌ PARITY BROKEN'}")
    print(f"  pool: calls={pool.calls} timeouts={pool.timeouts} errors={pool.errors}")
    print(f"  off-loop round-trip incl IPC: p50={off_ms[n//2]:.2f}ms  "
          f"p90={off_ms[int(n*0.9)]:.1f}ms  max={off_ms[-1]:.1f}ms")


if __name__ == "__main__":
    npb = int(sys.argv[1]) if len(sys.argv) > 1 else 25
    asyncio.run(_amain(npb))
