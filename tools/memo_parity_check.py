"""PARITY + HIT check for the joint-layer memo (2026-07-14 throughput fix).

Rule-8 gate: proves the memo is TRANSPARENT — memo-on output == memo-off output
== the memo-on HIT-path output, to the centi-cent, on real tape combos — and that
a hit is orders of magnitude faster than a miss.

Two engines share ONE primed feed/metadata:
  engine_off : joint_memo_maxsize=0   (cache disabled — the reference)
  engine_on  : joint_memo_maxsize>0   (cache live)
Each combo priced: off (baseline) | on #1 (miss) | on #2 (hit). All three fairs
(or NoQuote reasons) MUST be identical.

Usage:  uv run python tools/memo_parity_check.py [n_per_bucket]
"""
from __future__ import annotations

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
from combomaker.pricing.engine import PricingEngine  # noqa: E402
from combomaker.pricing.quote import ConstructedQuote, NoQuote  # noqa: E402

import profile_pricer as pp  # noqa: E402


def _fair(engine, rfq):  # noqa: ANN001, ANN202
    r = engine.price(rfq, time_to_close_s=100_000)
    if isinstance(r, ConstructedQuote):
        return ("quote_farmed" if r.farmed else "quote", int(r.fair_cc),
                int(r.yes_bid_cc), int(r.no_bid_cc))
    assert isinstance(r, NoQuote)
    return ("noquote", r.reason.name, None, None)


async def _amain(n_per_bucket: int) -> None:
    print(f"loading combos ({n_per_bucket}/bucket)...", flush=True)
    sample = pp._load_sample(n_per_bucket)
    ws, clock, feed, metadata, engine_on = await pp._build_engine()  # memo default ON
    engine_off = PricingEngine(feed, metadata, DOC_ASSUMED, PricingConfig(),
                               joint_memo_maxsize=0)  # memo OFF (reference)
    print(f"priced {len(sample)} combos on two engines sharing one feed...", flush=True)

    mismatches = 0
    miss_ms: list[float] = []
    hit_ms: list[float] = []
    n = 0
    for i, (bucket, legs, sides, yes_cc, nlegs) in enumerate(sample):
        await pp._prime_books(ws, feed, metadata, legs, yes_cc, sid=5000 + i)
        rfq = pp._mk_rfq(legs, sides)
        off = _fair(engine_off, rfq)                      # reference (no cache)
        t0 = time.perf_counter(); on1 = _fair(engine_on, rfq); miss_ms.append((time.perf_counter()-t0)*1000)  # miss
        t0 = time.perf_counter(); on2 = _fair(engine_on, rfq); hit_ms.append((time.perf_counter()-t0)*1000)   # hit
        n += 1
        if not (off == on1 == on2):
            mismatches += 1
            if mismatches <= 10:
                print(f"  MISMATCH {'-'.join(legs[:2])}... off={off} on_miss={on1} on_hit={on2}")

    hits, misses, size = engine_on.joint_cache_stats
    miss_ms.sort(); hit_ms.sort()
    print("\n==== MEMO PARITY ====")
    print(f"  combos compared: {n}")
    print(f"  mismatches (off vs on-miss vs on-hit): {mismatches}   "
          f"{'✅ EXACT PARITY' if mismatches == 0 else '❌ PARITY BROKEN'}")
    print(f"  engine_on cache: hits={hits} misses={misses} size={size} "
          f"(expect hits≈{n}, one hit per combo's 2nd price)")
    print("\n==== HIT vs MISS LATENCY (memo_on) ====")
    print(f"  miss: p50={miss_ms[len(miss_ms)//2]:.3f}ms  p90={miss_ms[int(len(miss_ms)*0.9)]:.2f}ms  max={miss_ms[-1]:.1f}ms")
    print(f"  hit : p50={hit_ms[len(hit_ms)//2]:.4f}ms  p90={hit_ms[int(len(hit_ms)*0.9)]:.4f}ms  max={hit_ms[-1]:.3f}ms")
    if hit_ms[len(hit_ms)//2] > 0:
        print(f"  median speedup on hit: {miss_ms[len(miss_ms)//2]/max(hit_ms[len(hit_ms)//2],1e-6):.0f}x")


if __name__ == "__main__":
    import asyncio
    npb = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    asyncio.run(_amain(npb))
