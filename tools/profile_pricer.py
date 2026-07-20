"""PROFILE the live PricingEngine.price hot path (2026-07-14, throughput fix).

ADDITIVE (CLAUDE.md rule 8): imports and calls the LIVE engine + shipped config;
never edits a live module. Answers, with REAL numbers, the questions that decide
the throughput fix:

  1. What does one engine.price() cost, per combo, at steady state (p50/p90/max)?
  2. PATH MIX: what fraction of real tape combos hit the structural (invert)
     path vs copula vs containment/band vs decline?
  3. WHERE does the time go? (cProfile cumulative — invert / joint_probability /
     price_joint_matrices / classify_legs / Decimal).
  4. CACHE UPSIDE: price the SAME combo twice — is the 2nd call identical output
     (pure) and would a memo skip the whole cost? (proves the memoization plan).

Feeds the engine synthetic two-sided books whose microprice is the 4dp tape
marginal — the SAME construction the rule-8c parity harness uses, so the cost
measured here is the cost the live engine pays.

Usage:  uv run python tools/profile_pricer.py [n_per_bucket]
"""
from __future__ import annotations

import cProfile
import io
import pickle
import pstats
import random
import statistics
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools" / "backtests"))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

from combomaker.core.clock import FakeClock  # noqa: E402
from combomaker.core.conventions import DOC_ASSUMED  # noqa: E402
from combomaker.marketdata.feed import OrderbookFeed  # noqa: E402
from combomaker.marketdata.grid import PriceGrid  # noqa: E402
from combomaker.marketdata.metadata import EventMeta, MarketMeta, MetadataCache  # noqa: E402
from combomaker.ops.config import PricingConfig  # noqa: E402
from combomaker.pricing.engine import PricingEngine  # noqa: E402
from combomaker.pricing.grouping import game_key  # noqa: E402
from combomaker.pricing.quote import ConstructedQuote, NoQuote  # noqa: E402
from combomaker.rfq.models import Rfq  # noqa: E402


def _is_same_game(leg_tickers) -> bool:  # noqa: ANN001
    keys = set()
    for t in leg_tickers:
        ev = "-".join(t.split("-")[:2])
        keys.add(game_key(ev) or ev)
    return len(keys) == 1

from parity_rule8c import CACHES, _quantized_yes_cc  # noqa: E402

COMBO = "KXMVE-PROF"


class _Ws:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.subscriptions: list = []

    def on_message(self, msg_type, handler):  # noqa: ANN001, ANN201
        self.handlers.setdefault(msg_type, []).append(handler)

    def on_disconnect(self, handler):  # noqa: ANN001, ANN201
        pass

    def add_subscription(self, channels, *, on_subscribed=None, **kw):  # noqa: ANN001, ANN003, ANN201
        self.subscriptions.append(on_subscribed)

    async def ack(self, index: int, sid: int) -> None:
        cb = self.subscriptions[index]
        assert cb is not None
        await cb(sid)

    async def deliver(self, env) -> None:  # noqa: ANN001
        for h in self.handlers.get(str(env.get("type")), []):
            await h(env)


async def _build_engine():  # noqa: ANN202
    """ONE engine/feed/metadata reused across combos (live reuses one engine)."""
    ws = _Ws()
    clock = FakeClock()
    feed = OrderbookFeed(ws, clock)
    metadata = MetadataCache(None, clock)  # type: ignore[arg-type]
    metadata._markets[COMBO] = MarketMeta(  # noqa: SLF001
        ticker=COMBO,
        status="active",
        grid=PriceGrid.from_market_payload(
            {"ticker": COMBO, "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}]}
        ),
        event_ticker="E",
        close_time=clock.now() + timedelta(seconds=21_600),
        expected_expiration_time=None,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )
    engine = PricingEngine(feed, metadata, DOC_ASSUMED, PricingConfig())
    return ws, clock, feed, metadata, engine


_SEQ = [0]


async def _prime_books(ws, feed, metadata, leg_tickers, yes_cc, sid):  # noqa: ANN001, ANN202
    feed.watch(list(leg_tickers))
    await ws.ack(len(ws.subscriptions) - 1, sid)  # each watch = its own subscription/sid
    for t, y_cc in zip(leg_tickers, yes_cc, strict=False):
        _SEQ[0] += 1
        yes_bid = (y_cc - 100) / 10_000
        no_bid = (10_000 - y_cc - 100) / 10_000
        await ws.deliver(
            {
                "type": "orderbook_snapshot",
                "sid": sid,
                "seq": _SEQ[0],
                "msg": {
                    "market_ticker": t,
                    "yes_dollars_fp": [[f"{yes_bid:.4f}", "50.00"]],
                    "no_dollars_fp": [[f"{no_bid:.4f}", "50.00"]],
                },
            }
        )
    for t in leg_tickers:
        ev = "-".join(t.split("-")[:2])
        if ev not in metadata._events:  # noqa: SLF001
            metadata._events[ev] = EventMeta(  # noqa: SLF001
                event_ticker=ev, mutually_exclusive=False, raw={}, fetched_mono_ns=0
            )


def _mk_rfq(leg_tickers, sides):  # noqa: ANN001, ANN202
    return Rfq.from_ws(
        {
            "id": "rfq_prof",
            "market_ticker": COMBO,
            "created_ts": "2026-07-14T10:00:00Z",
            "contracts_fp": "10.00",
            "mve_collection_ticker": "KXMVEPROF",
            "mve_selected_legs": [
                {"market_ticker": t, "side": s, "event_ticker": "-".join(t.split("-")[:2])}
                for t, s in zip(leg_tickers, sides, strict=False)
            ],
        }
    )


def _load_sample(n_per_bucket: int):  # noqa: ANN202
    rng = random.Random(20260714)
    sample = []  # (bucket, legs, sides, yes_cc, n_legs)
    for name, base in CACHES.items():
        recs = pickle.load(open(base / "inputs.pkl", "rb"))
        usable = [
            (mt, rec)
            for mt, rec in recs.items()
            if rec.get("snaps") and len(rec["snaps"][-1][1]) == len(rec["legs"])
        ]
        pick = usable if len(usable) <= n_per_bucket else rng.sample(usable, n_per_bucket)
        for _mt, rec in pick:
            legs, sides = list(rec["legs"]), list(rec["sides"])
            marg_sel = list(rec["snaps"][-1][1])
            yes_cc = _quantized_yes_cc(marg_sel, sides)
            sample.append((name, legs, sides, yes_cc, len(legs)))
        del recs
    rng.shuffle(sample)
    return sample


async def _amain(n_per_bucket: int) -> None:
    print(f"loading combos ({n_per_bucket}/bucket)...", flush=True)
    sample = _load_sample(n_per_bucket)
    print(f"loaded {len(sample)} combos; building engine...", flush=True)
    ws, clock, feed, metadata, engine = await _build_engine()

    # Pre-prime every combo's books first so profiling times ONLY price().
    primed = []
    for i, (bucket, legs, sides, yes_cc, nlegs) in enumerate(sample):
        await _prime_books(ws, feed, metadata, legs, yes_cc, sid=1000 + i)
        primed.append((bucket, legs, sides, nlegs, _mk_rfq(legs, sides)))

    disp: Counter = Counter()
    per_combo_ms: list[float] = []
    by_nlegs: dict[int, list[float]] = {}
    sg_ms: dict[bool, list[float]] = {True: [], False: []}

    def price_all() -> None:
        for bucket, legs, sides, nlegs, rfq in primed:
            t0 = time.perf_counter()
            r = engine.price(rfq, time_to_close_s=100_000)
            dt = (time.perf_counter() - t0) * 1000
            per_combo_ms.append(dt)
            by_nlegs.setdefault(nlegs, []).append(dt)
            sg_ms[_is_same_game(legs)].append(dt)
            if isinstance(r, ConstructedQuote):
                disp["quote_farmed" if r.farmed else "quote"] += 1
            elif isinstance(r, NoQuote):
                disp[f"noquote:{r.reason.name}"] += 1

    # 1) cProfile the whole batch (where does time go?)
    pr = cProfile.Profile()
    pr.enable()
    price_all()
    pr.disable()

    n = len(per_combo_ms)
    per_combo_ms.sort()
    print("\n==== PER-COMBO price() WALL TIME ====")
    print(f"  n={n}  mean={statistics.mean(per_combo_ms):.2f}ms  "
          f"p50={per_combo_ms[n//2]:.2f}ms  p90={per_combo_ms[int(n*0.9)]:.2f}ms  "
          f"p99={per_combo_ms[int(n*0.99)]:.2f}ms  max={per_combo_ms[-1]:.2f}ms")
    print(f"  total price() wall = {sum(per_combo_ms):.0f}ms for {n} combos "
          f"→ throughput ≈ {n/ (sum(per_combo_ms)/1000):.0f} combos/s single-thread")

    print("\n==== BY LEG COUNT (median ms) ====")
    for k in sorted(by_nlegs):
        v = sorted(by_nlegs[k])
        print(f"  {k:2d} legs: n={len(v):5d}  median={v[len(v)//2]:.2f}ms  "
              f"max={v[-1]:.2f}ms")

    print("\n==== SAME-GAME vs MULTI-GAME (cost concentration) ====")
    for sg, label in ((True, "same-game "), (False, "multi-game")):
        v = sorted(sg_ms[sg])
        if not v:
            continue
        share = 100 * sum(v) / sum(per_combo_ms)
        print(f"  {label}: n={len(v):5d}  total={sum(v):8.0f}ms ({share:4.1f}% of all cost)  "
              f"median={v[len(v)//2]:7.2f}ms  p99={v[int(len(v)*0.99)]:8.2f}ms  max={v[-1]:8.2f}ms")

    print("\n==== DISPOSITION MIX ====")
    for name, c in disp.most_common():
        print(f"  {c:5d}  {name}")

    print("\n==== cPROFILE TOP 25 BY CUMULATIVE TIME ====")
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(25)
    # Trim to function lines for readability.
    for line in s.getvalue().splitlines():
        if "combomaker" in line or "invert" in line or "joint_probab" in line \
           or "ncalls" in line or "function calls" in line or "seconds" in line:
            print(line)

    # 2) CACHE-UPSIDE proof: re-price a structural combo 30x; confirm identical
    #    output and measure warm cost (a memo would collapse this to a dict get).
    struct = next(
        (p for p in primed if p[0] in ("wc", "mixed") and p[3] >= 2), None
    )
    if struct is not None:
        _b, legs, sides, _nl, rfq = struct
        r0 = engine.price(rfq, time_to_close_s=100_000)
        fair0 = int(r0.fair_cc) if isinstance(r0, ConstructedQuote) else None
        reps = 30
        t0 = time.perf_counter()
        identical = True
        for _ in range(reps):
            r = engine.price(rfq, time_to_close_s=100_000)
            f = int(r.fair_cc) if isinstance(r, ConstructedQuote) else None
            identical &= (f == fair0)
        warm_ms = (time.perf_counter() - t0) / reps * 1000
        print("\n==== CACHE-UPSIDE (same combo re-priced) ====")
        print(f"  legs={len(legs)}  fair_cc={fair0}  identical_over_{reps}={identical}  "
              f"per-call={warm_ms:.2f}ms  → a memo hit would replace this with a dict.get()")


if __name__ == "__main__":
    import asyncio

    npb = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    asyncio.run(_amain(npb))
