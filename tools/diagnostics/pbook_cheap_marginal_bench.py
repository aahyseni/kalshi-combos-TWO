"""THROUGHPUT BENCHMARK for the cheap ΔP(book) marginal.

Scales the snapshot cache over ``n`` (MC scenarios) and reports us/candidate for
both estimator forms, the snapshot build cost (off the quote path), the resident
cache size, and the implied quotes/min headroom. Offline; reuses the fidelity
prototype's cache + estimator.
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from combomaker.ops.pricing_pool import _DictMarginals, _DictWithinGameRho  # noqa: E402
from pbook_cheap_marginal_fidelity import (  # noqa: E402
    build_cache,
    cheap_delta,
    cheap_delta_fast,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", default="data/_pbook_cheap/inputs_all.pkl")
    ap.add_argument("--iters", type=int, default=2000)
    args = ap.parse_args()
    cases = pickle.load(open(args.inputs, "rb"))
    inp0 = cases[0]["inputs"]
    marg: dict[str, float] = {}
    rho: dict = {}
    for c in cases:
        marg.update(c["inputs"].marginals)
        rho.update(c["inputs"].within_game_rho_pairs)
    cands = [c["inputs"].candidate for c in cases]
    pre = [*inp0.committed, *inp0.reservations]

    print(f"{'n':>8} {'legs':>5} {'build_ms':>9} {'cache_MB':>9} "
          f"{'general_us':>11} {'fast_us':>9} {'quotes/min@1core':>18}")
    for n in (5_000, 20_000, 50_000, 100_000):
        t0 = time.perf_counter()
        cache = build_cache(
            pre, _DictMarginals(marg), _DictWithinGameRho(rho), inp0.structural_cfg,
            n=n, seed=inp0.seed, universe_positions=cands,
        )
        build_ms = (time.perf_counter() - t0) * 1e3
        assert cache is not None
        mb = (cache.values.nbytes + cache.pre_pnl.nbytes + cache.pre_sorted.nbytes
              + cache.packed.nbytes + cache.spare.nbytes) / 1e6

        for c in cands[:5]:
            cheap_delta(cache, c, marg, rho)
        t0 = time.perf_counter()
        it = 0
        while it < args.iters:
            for c in cands:
                cheap_delta(cache, c, marg, rho)
                it += 1
                if it >= args.iters:
                    break
        gen_us = (time.perf_counter() - t0) / it * 1e6

        fast_ok = [c for c in cands
                   if all(l.market_ticker in cache.leg_index and l.side == "yes"
                          for l in c.legs)]
        fast_us = float("nan")
        if fast_ok:
            for c in fast_ok[:5]:
                cheap_delta_fast(cache, c)
            t0 = time.perf_counter()
            it = 0
            while it < args.iters:
                for c in fast_ok:
                    cheap_delta_fast(cache, c)
                    it += 1
                    if it >= args.iters:
                        break
            fast_us = (time.perf_counter() - t0) / it * 1e6
        best = min(x for x in (gen_us, fast_us) if x == x)
        print(f"{n:>8} {len(cache.leg_index):>5} {build_ms:>9.0f} {mb:>9.1f} "
              f"{gen_us:>11.1f} {fast_us:>9.1f} {60e6 / best:>18,.0f}")

    # precision floor: SE of Δp_book at each n (binomial-difference SE on the
    # candidate sample, measured empirically by re-drawing the cache)
    print("\ndelta_p_book PRECISION vs cache n (sd across 8 independent caches):")
    for n in (5_000, 20_000, 100_000):
        ests: list[list[float]] = []
        for s in range(8):
            cache = build_cache(
                pre, _DictMarginals(marg), _DictWithinGameRho(rho),
                inp0.structural_cfg, n=n, seed=inp0.seed + 1000 * s + 1,
                universe_positions=cands,
            )
            assert cache is not None
            row = []
            for c in cands:
                g = cheap_delta(cache, c, marg, rho)
                row.append(g[0] if g else float("nan"))
            ests.append(row)
        arr = np.array(ests)
        sds = np.nanstd(arr, axis=0, ddof=1)
        print(f"   n={n:>7}  median sd {np.nanmedian(sds):.5f}  "
              f"p90 sd {np.nanquantile(sds, 0.9):.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
