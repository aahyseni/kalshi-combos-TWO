"""RE-GRADE for the dimension-adaptive MVN-tolerance change (Phase 2).

The MVN CDF small-n path (effective dim <= 4) is pinned at abseps=1e-10 — 6 orders
of magnitude tighter than a cent (1e-4 in prob) needs, and the tail of price()
cost. This tool measures, on REAL tape combos, whether loosening that tolerance
changes ANY quoted cent (the operator's hard rule: speed only, $ unchanged) and
how much speed it buys — so we can pick the LOOSEST tolerance with ZERO cent drift.

Method (rule 8: prototype in the tool, monkeypatching the LIVE copula._mvn_cdf —
never editing it here): price every combo with the shipped tight baseline, then
re-price with each candidate tolerance and diff the quote cents (yes_bid, no_bid,
fair). Engines run memo-OFF so every price recomputes.

Usage:  uv run python tools/tolerance_regrade.py [n_per_bucket]
"""
from __future__ import annotations

import statistics
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

import numpy as np  # noqa: E402
from scipy.stats import multivariate_normal  # noqa: E402

import combomaker.pricing.copula as copula  # noqa: E402
from combomaker.core.conventions import DOC_ASSUMED  # noqa: E402
from combomaker.ops.config import PricingConfig  # noqa: E402
from combomaker.pricing.engine import PricingEngine  # noqa: E402
from combomaker.pricing.quote import ConstructedQuote, NoQuote  # noqa: E402

SEED = copula._MVN_SEED  # noqa: SLF001
MAXDIM = copula._TIGHT_ABSEPS_MAX_DIM  # noqa: SLF001 - the effective-dim gate (4)


def make_mvn(abseps_small):  # noqa: ANN001, ANN201
    """Build an _mvn_cdf variant. abseps_small=None ⇒ scipy DEFAULT for small n
    too (the loosest). A float ⇒ that abseps on the small-n path. n>MAXDIM always
    uses the scipy default (unchanged from today)."""

    def _mvn(z, corr):  # noqa: ANN001, ANN202
        k = z.shape[0]
        if abseps_small is not None and k <= MAXDIM:
            raw = multivariate_normal.cdf(
                z, mean=np.zeros(k), cov=corr, allow_singular=True,
                abseps=abseps_small, releps=0.0, rng=np.random.default_rng(SEED),
            )
        else:
            raw = multivariate_normal.cdf(
                z, mean=np.zeros(k), cov=corr, allow_singular=True,
                rng=np.random.default_rng(SEED),
            )
        return float(raw)

    return _mvn


def _quote(engine, rfq):  # noqa: ANN001, ANN202
    r = engine.price(rfq, time_to_close_s=100_000)
    if isinstance(r, ConstructedQuote):
        return (int(r.yes_bid_cc), int(r.no_bid_cc), int(r.fair_cc))
    assert isinstance(r, NoQuote)
    return ("nq", r.reason.name)


async def _amain(n_per_bucket: int) -> None:
    import profile_pricer as pp

    print(f"loading combos ({n_per_bucket}/bucket)...", flush=True)
    sample = pp._load_sample(n_per_bucket)
    ws, clock, feed, metadata, _ = await pp._build_engine()
    engine = PricingEngine(feed, metadata, DOC_ASSUMED, PricingConfig(), joint_memo_maxsize=0)

    primed = []
    for i, (bucket, legs, sides, yes_cc, nlegs) in enumerate(sample):
        await pp._prime_books(ws, feed, metadata, legs, yes_cc, sid=8000 + i)
        primed.append((nlegs, pp._mk_rfq(legs, sides)))

    orig = copula._mvn_cdf  # noqa: SLF001 - restore after

    # Baseline = the shipped tight (1e-10 on small n).
    copula._mvn_cdf = make_mvn(copula._TIGHT_ABSEPS)  # noqa: SLF001
    base = [_quote(engine, rfq) for _nl, rfq in primed]

    candidates = {
        "default(loose)": None,
        "abseps=1e-6": 1e-6,
        "abseps=1e-7": 1e-7,
        "abseps=1e-8": 1e-8,
    }
    print(f"\nre-graded {len(primed)} combos; baseline = tight abseps={copula._TIGHT_ABSEPS}\n")  # noqa: SLF001
    print(f"{'candidate':16s} {'cent-changed':>13s} {'max|Δfair|c':>12s}  worst examples")
    results = {}
    for name, abs_small in candidates.items():
        copula._mvn_cdf = make_mvn(abs_small)  # noqa: SLF001
        changed = 0
        max_dfair = 0
        worst = []
        for (nl, rfq), b in zip(primed, base, strict=True):
            q = _quote(engine, rfq)
            if q != b:
                changed += 1
                if len(b) == 3 and len(q) == 3:
                    d = abs(q[2] - b[2])
                    max_dfair = max(max_dfair, d)
                    worst.append((d, nl, b, q))
        worst.sort(reverse=True)
        results[name] = changed
        ex = "; ".join(f"n{nl} Δ{d}c {b[2]}→{q[2]}" for d, nl, b, q in worst[:2])
        print(f"{name:16s} {changed:>13d} {max_dfair:>12d}  {ex}")

    copula._mvn_cdf = orig  # noqa: SLF001

    # Speed microbench on the small-n path (tight vs loosest) — the win.
    small = [rfq for nl, rfq in primed if nl <= MAXDIM][:60]
    if small:
        copula._mvn_cdf = make_mvn(copula._TIGHT_ABSEPS)  # noqa: SLF001
        t0 = time.perf_counter()
        for rfq in small:
            engine.price(rfq, time_to_close_s=100_000)
        tight_ms = (time.perf_counter() - t0) / len(small) * 1000
        copula._mvn_cdf = make_mvn(None)  # noqa: SLF001
        t0 = time.perf_counter()
        for rfq in small:
            engine.price(rfq, time_to_close_s=100_000)
        loose_ms = (time.perf_counter() - t0) / len(small) * 1000
        copula._mvn_cdf = orig  # noqa: SLF001
        print(f"\nsmall-n price() mean: tight={tight_ms:.1f}ms  loose={loose_ms:.2f}ms  "
              f"speedup={tight_ms/max(loose_ms,1e-6):.0f}x  (n={len(small)})")

    print("\nVERDICT: ship the LOOSEST candidate whose cent-changed == 0.")


if __name__ == "__main__":
    import asyncio
    npb = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    asyncio.run(_amain(npb))
