"""BENCHMARK: one status tick enforcing an end-of-game lifecycle wave, across
the three candidate shapes, with the heartbeat watched throughout and the
exchange's WRITE-TOKEN BUCKET metered.

Arms
----
BEFORE    the shipped-at-incident behaviour: sequential DELETEs, every 404
          counted as a failure, no per-call bound, and the liveness heartbeat
          beaten by the SAME loop that does the work.
FANOUT-8  the first draft of the fix (rejected by the 2026-07-26 adversarial
          gate): concurrent under a hand-set fan-out of 8, no token pacing.
          Measured here to show WHY a concurrency literal cannot bound a rate.
AFTER     the live code path (``QuoteLifecycle.cancel_quotes_touching`` via
          ``QuoteApp._enforce_market_quarantine``), paced by the shared
          WRITE-TOKEN BUDGET, with the dedicated ``_liveness_loop`` running.

Latency
-------
The first version of this bench used 0.42 s per delete, taken from the incident
tape's average spacing between two ``delete_quote_failed`` log lines. That is
the SEQUENTIAL INTER-LOG SPACING of the old code, not a per-request latency —
using it as a latency understates the concurrent request rate by ~8x and hides
the 429 storm completely. This version drives REAL per-request latencies:

    FAST   uniform 5-20 ms   (a healthy exchange)
    SLOW   40 ms fixed       (the loaded end-of-game exchange)

Budget arithmetic (docs/api-notes/openapi-comms.md + SupervisorConfig defaults)
------------------------------------------------------------------------------
    DeleteQuote            = 2 tokens
    graded ceiling         = the tier OBSERVED read-only on prod 2026-07-26
                             (advanced: bucket 600, refill 300 tok/s) — offline,
                             so the value is pinned rather than asked for
    write_budget_capacity  = 200 tokens
    write_budget_refill_s  = 10.0 s  ⇒ 20 tokens/s sustained
    bucket guarantee: tokens in ANY window T <= capacity + rate*T
                      ⇒ worst 1 s = 200 + 20 = 220 tokens, vs the observed
                        bucket's 600 + 300 = 900  ✓  (on the fail-safe Basic
                        floor the bot CLAMPS the burst to 100 first)

Testing isolation: this script IMPORTS the live modules and never edits them;
the BEFORE and FANOUT-8 arms are local re-implementations of the old/rejected
loops, not patches of anything shipped.

    .venv/Scripts/python.exe tools/bench_quarantine_enforcement.py
"""

from __future__ import annotations

import asyncio
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from combomaker.exchange.rest import (  # noqa: E402
    DELETE_QUOTE_TOKEN_COST,
    LOWEST_TIER_LIMITS,
    ApiTierLimits,
    KalshiApiError,
)

# The tier this bench grades against. The hard-coded ``WRITE_TOKENS_PER_S = 300``
# it used to import is gone (2026-07-26): the live bot READS its tier from
# GET /account/limits at boot and fails safe to ``LOWEST_TIER_LIMITS``. An
# offline bench cannot ask, so it pins the tier OBSERVED read-only on the live
# prod account 2026-07-26 (tools/diagnostics/account_write_bucket.py):
#   usage_tier=advanced, write {bucket_capacity: 600, refill_rate: 300}
# and grades against what that bucket admits in one second (capacity + refill).
# On the fail-safe Basic floor the bot CLAMPS the configured budget
# (ApiTierLimits.clamp_write_budget: 200-token burst -> 100), so the graded
# property holds there too, by construction rather than by measurement.
OBSERVED_PROD_TIER = ApiTierLimits(
    usage_tier="advanced",
    read_refill_per_s=300,
    read_capacity=600,
    write_refill_per_s=300,
    write_capacity=600,
    observed=True,
)
WRITE_TOKENS_PER_S = OBSERVED_PROD_TIER.write_refill_per_s
from combomaker.ops.config import SupervisorConfig  # noqa: E402
from combomaker.ops.quote_app import (  # noqa: E402
    LOOP_MAINTENANCE,
    MAINTENANCE_TICK_INTERVAL_S,
    STATUS_TICK_INTERVAL_S,
)
from combomaker.risk.heartbeat import Heartbeat, HeartbeatReader  # noqa: E402

# The rejected draft's hand-set concurrency, kept ONLY so this bench can price
# what it would have emitted. Nothing under src/ carries it any more.
REJECTED_FANOUT = 8
QUOTES_PER_MARKET = 3


class _NotFound(KalshiApiError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "not found")


class _Latency:
    """A per-request latency model + a meter for what the exchange's bucket
    would have seen (one send timestamp per DELETE, priced at 2 tokens)."""

    def __init__(self, name: str, lo_s: float, hi_s: float) -> None:
        self.name = name
        self.lo_s = lo_s
        self.hi_s = hi_s
        self.sends: list[float] = []
        self._rng = random.Random(20260726)

    def reset(self) -> None:
        self.sends = []

    def draw(self) -> float:
        if self.hi_s <= self.lo_s:
            return self.lo_s
        return self._rng.uniform(self.lo_s, self.hi_s)

    async def delete_404(self, _quote_id: str) -> dict[str, Any]:
        self.sends.append(time.monotonic())
        await asyncio.sleep(self.draw())
        raise _NotFound()

    # ---- metering -------------------------------------------------------
    def peak_per_window(self, window_s: float) -> int:
        """Most DELETEs sent inside any ``window_s``-wide sliding window."""
        peak = 0
        for start in self.sends:
            n = sum(1 for t in self.sends if start <= t < start + window_s)
            peak = max(peak, n)
        return peak

    def report(self, wall_s: float) -> str:
        if not self.sends:
            return "no requests"
        peak_req = self.peak_per_window(1.0)
        peak_tok = peak_req * DELETE_QUOTE_TOKEN_COST
        avg_req = len(self.sends) / max(wall_s, 1e-9)
        ceiling = OBSERVED_PROD_TIER.write_capacity + WRITE_TOKENS_PER_S
        breach = "  <== OVER TIER" if peak_tok > ceiling else "  ok"
        return (
            f"peak {peak_req:4d} req/s = {peak_tok:5d} tok/s "
            f"(avg {avg_req:6.1f} req/s = "
            f"{avg_req * DELETE_QUOTE_TOKEN_COST:6.1f} tok/s){breach}"
        )


class _HeartbeatWatch:
    """Samples the heartbeat file's age at 10ms while an arm runs."""

    def __init__(self, reader: HeartbeatReader) -> None:
        self._reader = reader
        self._stop = asyncio.Event()
        self.ages: list[float] = []

    async def run(self) -> None:
        while not self._stop.is_set():
            age = self._reader.read_age_s()
            self.ages.append(float("inf") if age is None else age)
            await asyncio.sleep(0.01)

    def stop(self) -> None:
        self._stop.set()


async def _before_arm(n_quotes: int, lat: _Latency, heartbeat: Heartbeat) -> float:
    """The old shape: one loop does the work AND owns the beat, deletes walk
    sequentially, every 404 is an error. Beat lands only between ticks."""
    lat.reset()
    heartbeat.beat()  # the one beat this tick gets, at the top
    started = time.monotonic()
    for _ in range(n_quotes):
        try:
            await lat.delete_404("q")
        except Exception:  # noqa: BLE001 — the old path logged and moved on
            pass
    return time.monotonic() - started


async def _fanout_arm(n_quotes: int, lat: _Latency) -> float:
    """The REJECTED draft: concurrent under a hand-set fan-out, unpaced."""
    lat.reset()
    sem = asyncio.Semaphore(REJECTED_FANOUT)

    async def one() -> None:
        async with sem:
            try:
                await lat.delete_404("q")
            except Exception:  # noqa: BLE001
                pass

    started = time.monotonic()
    await asyncio.gather(*(one() for _ in range(n_quotes)))
    return time.monotonic() - started


async def _after_arm(
    n_markets: int, per_market: int, tmp: Path, lat: _Latency
) -> tuple[float, int, int]:
    """The live path: QuoteApp._enforce_market_quarantine over the real
    QuoteLifecycle.cancel_quotes_touching, paced by the real WriteBudget."""
    from test_liveness_progress import _rig_with_quotes  # noqa: PLC0415
    from test_metadata_change_scope import _armed_app  # noqa: PLC0415

    lat.reset()
    rig, tickers, quote_ids = await _rig_with_quotes(
        tmp,
        markets=n_markets,
        quotes_per_market=per_market,
        delete=lat.delete_404,
        db=f"bench{n_markets}x{per_market}{lat.name}.sqlite3",
        real_clock=True,
    )
    app = _armed_app(tmp)
    for ticker in tickers:
        app._market_quarantine.quarantine(ticker, "lifecycle change")  # noqa: SLF001
    started = time.monotonic()
    await app._enforce_market_quarantine(rig.lifecycle)  # noqa: SLF001
    elapsed = time.monotonic() - started
    return elapsed, len(quote_ids), len(app._market_quarantine.unenforced())  # noqa: SLF001


async def _run_case(
    n_markets: int, lat: _Latency, per_market: int = QUOTES_PER_MARKET
) -> None:
    n_quotes = n_markets * per_market
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        from combomaker.core.clock import SystemClock  # noqa: PLC0415

        print(
            f"\n=== {n_markets} markets x {per_market} quotes = {n_quotes} "
            f"withdrawals (all HTTP 404), latency {lat.name} ==="
        )

        # ---- BEFORE -----------------------------------------------------
        clock = SystemClock()
        hb_path = tmp / "heartbeat_before.txt"
        heartbeat = Heartbeat(clock, hb_path)
        heartbeat.beat()
        watch = _HeartbeatWatch(HeartbeatReader(clock, hb_path))
        watcher = asyncio.create_task(watch.run())
        before_s = await _before_arm(n_quotes, lat, heartbeat)
        watch.stop()
        await watcher
        before_worst = max(watch.ages) if watch.ages else float("nan")
        before_rate = lat.report(before_s)

        # ---- FANOUT-8 (rejected draft) ----------------------------------
        fanout_s = await _fanout_arm(n_quotes, lat)
        fanout_rate = lat.report(fanout_s)

        # ---- AFTER ------------------------------------------------------
        from test_metadata_change_scope import _armed_app  # noqa: PLC0415

        probe = _armed_app(tmp)
        probe._progress.register(  # noqa: SLF001
            LOOP_MAINTENANCE,
            interval_s=MAINTENANCE_TICK_INTERVAL_S,
            wedge_timeout_s=probe._config.supervisor.heartbeat_timeout_s,  # noqa: SLF001
        )
        liveness = asyncio.create_task(probe._liveness_loop())  # noqa: SLF001
        await asyncio.sleep(probe._config.supervisor.poll_interval_s)  # noqa: SLF001
        watch2 = _HeartbeatWatch(
            HeartbeatReader(probe._clock, probe._heartbeat.path)  # noqa: SLF001
        )
        watcher2 = asyncio.create_task(watch2.run())
        after_s, quotes, unenforced = await _after_arm(n_markets, per_market, tmp, lat)
        watch2.stop()
        await watcher2
        liveness.cancel()
        after_worst = max(watch2.ages) if watch2.ages else float("nan")
        after_rate = lat.report(after_s)
        timeout_s = probe._config.supervisor.heartbeat_timeout_s  # noqa: SLF001

        def _verdict(worst: float) -> str:
            return "WEDGE-KILLED" if worst > timeout_s else "survived"

        print(
            f"  BEFORE    wall {before_s * 1000:8.1f} ms   hb {before_worst:6.2f}s "
            f"{_verdict(before_worst):13s} {before_rate}"
        )
        print(
            f"  FANOUT-8  wall {fanout_s * 1000:8.1f} ms   hb    n/a "
            f"{'-':13s} {fanout_rate}"
        )
        print(
            f"  AFTER     wall {after_s * 1000:8.1f} ms   hb {after_worst:6.2f}s "
            f"{_verdict(after_worst):13s} {after_rate}"
        )
        print(
            f"            quotes {quotes}   unenforced quarantines {unenforced}"
            f"   pass budget {STATUS_TICK_INTERVAL_S}s   wedge tolerance {timeout_s}s"
            f"   hb median {statistics.median(watch2.ages):.3f}s"
            f"   hb headroom {timeout_s - after_worst:.1f}s"
        )


async def main() -> None:
    live = SupervisorConfig()
    # What the bot would ACTUALLY run on the graded tier (2026-07-26): the
    # configured knob CLAMPED to that tier's bucket. On the observed prod tier
    # (advanced, 600 cap / 300 tok/s) nothing clamps; on the fail-safe Basic
    # floor the 200-token burst is clamped to 100.
    cap, refill_s = OBSERVED_PROD_TIER.clamp_write_budget(
        live.write_budget_capacity, live.write_budget_refill_s
    )
    floor_cap, floor_refill_s = LOWEST_TIER_LIMITS.clamp_write_budget(
        live.write_budget_capacity, live.write_budget_refill_s
    )
    rate = cap / refill_s
    print(
        f"write budget {live.write_budget_capacity} tokens / "
        f"{live.write_budget_refill_s}s -> clamped to {cap} / {refill_s:.1f}s = "
        f"{rate:.0f} tok/s sustained; worst 1s = {cap} + {rate:.0f} = "
        f"{cap + rate:.0f} <= observed-tier bucket+refill "
        f"{OBSERVED_PROD_TIER.write_capacity + WRITE_TOKENS_PER_S}; "
        f"DeleteQuote = {DELETE_QUOTE_TOKEN_COST} tokens"
    )
    print(
        f"fail-safe floor ({LOWEST_TIER_LIMITS.usage_tier}) would clamp the same "
        f"knob to {floor_cap} / {floor_refill_s:.1f}s = "
        f"{floor_cap / floor_refill_s:.0f} tok/s, worst 1s "
        f"{floor_cap + floor_cap / floor_refill_s:.0f} <= "
        f"{LOWEST_TIER_LIMITS.write_capacity + LOWEST_TIER_LIMITS.write_refill_per_s}"
    )
    fast = _Latency("5-20ms", 0.005, 0.020)
    slow = _Latency("40ms", 0.040, 0.040)
    for lat in (fast, slow):
        # 12 = the incident's own wave. 40x3 = 120 quotes. 100x3 = 300 = 1.5x
        # the live max_open_quotes cap (a stress bound past anything the book
        # can hold). 100x2 = 200 = EXACTLY the live cap, the biggest real wave.
        await _run_case(12, lat)
        await _run_case(40, lat)
        await _run_case(100, lat)
        await _run_case(100, lat, per_market=2)


if __name__ == "__main__":
    asyncio.run(main())
