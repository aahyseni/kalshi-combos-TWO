"""RELIGHT SOAK — 20 minutes of COMPRESSED live-loop activity under the two
stressors that actually produced the 20:12:24Z maintenance stall:

  (1) STORE CONTENTION   — one aiosqlite connection thread saturated by the tape
                           writer (the incident's 46 ``store_writer_checkpoint_
                           failed`` / ``wal_frames=57765, checkpointed=0``), with
                           the divergence sweep's read queued behind it.
  (2) READ 429 PRESSURE  — metadata GETs refused by a 429-ing exchange at the
                           incident's PEAK second (183 reads/s = 1,830 tok/s
                           against a 300 tok/s account), driven continuously.

Compression: the live slate produces an end-of-game quarantine wave once per
game and a divergence sweep every 300 s. Here a full wave runs EVERY status tick
(15 s) and the divergence sweep is re-armed EVERY tick (0.5 s), so 20 minutes of
wall time contains ~80 enforcement waves and ~2,400 sweep launches — orders of
magnitude more of the exact events that stalled the loop than a real 20 minutes
would hold.

Judged against the LIVE tolerances, not the demo ones:
    supervisor.heartbeat_timeout_s = 30.0   (config/prod-live-wc.local.yaml)
    supervisor.poll_interval_s     =  1.0   (SupervisorConfig default)
    maintenance stall bound = 30.0 + MAINTENANCE_TICK_INTERVAL_S (0.5) = 30.5 s
    quote       stall bound = 30.0 + POOL_DEADLINE_S + RFQ_MAX_QUEUE_DWELL_S = 33.5 s

Hard rule 8: every loop under test is the LIVE one —
``QuoteApp._liveness_loop``, ``QuoteApp._maintenance_loop``,
``QuoteLifecycle.maintenance_tick``, ``QuoteLifecycle.cancel_quotes_touching``,
``MetadataCache.refresh``, ``SafetySupervisor.check_once`` — all reading and
writing REAL heartbeat / progress / KILL files in a scratch dir. Nothing live is
edited; nothing is POSTed; the KILL under test is the scratch one, never the
repo's.

    .venv/Scripts/python.exe tools/diagnostics/relight_soak_loops.py --minutes 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

from combomaker.core.clock import SystemClock  # noqa: E402
from combomaker.core.reasons import ReasonCode  # noqa: E402
from combomaker.exchange.rest import (  # noqa: E402
    DEFAULT_ENDPOINT_TOKEN_COST,
    DELETE_QUOTE_TOKEN_COST,
    RateLimitedError,
    observe_api_tier,
)
from combomaker.marketdata.metadata import MetadataCache  # noqa: E402
from combomaker.ops.config import SupervisorConfig as SupKnobs  # noqa: E402
from combomaker.ops.persistence import STORE_OP_TIMEOUT_S, Store  # noqa: E402
from combomaker.ops.quote_app import (  # noqa: E402
    LOOP_MAINTENANCE,
    LOOP_QUOTE,
    LOOP_STATUS,
    MAINTENANCE_TICK_INTERVAL_S,
    POOL_DEADLINE_S,
    RFQ_WORKERS,
    STATUS_TICK_INTERVAL_S,
)
from combomaker.ops.supervisor import SafetySupervisor, SupervisorConfig  # noqa: E402
from combomaker.ops.write_budget import TokenBudget, WriteBudget  # noqa: E402
from combomaker.rfq.lifecycle import OpenQuoteState  # noqa: E402
from combomaker.risk.heartbeat import Heartbeat, HeartbeatReader  # noqa: E402
from combomaker.risk.progress import (  # noqa: E402
    ProgressLedger,
    ProgressReader,
    progress_path,
)

# Live wedge tolerance (config/prod-live-wc.local.yaml supervisor block).
LIVE_WEDGE_TIMEOUT_S = 30.0
RFQ_MAX_QUEUE_DWELL_S = 1.5  # quote_app local, same value the app registers

# The tier read read-only off LIVE PROD 2026-07-26 (GET /account/limits).
OBSERVED_PROD_LIMITS = {
    "usage_tier": "advanced",
    "read": {"bucket_capacity": 600, "refill_rate": 300},
    "write": {"bucket_capacity": 600, "refill_rate": 300},
}

# The incident's measured pressure, from data/live_20260726_1606.log.
INCIDENT_PEAK_READS_PER_S = 183
INCIDENT_RFQ_PER_S = 244_337 / 410.0
INCIDENT_DISTINCT_TICKERS = 1_390


class _Rest429:
    """Every metadata GET is refused 429 — the incident's exchange."""

    def __init__(self) -> None:
        self.attempts = 0

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.attempts += 1
        await asyncio.sleep(0)
        raise RateLimitedError(429, "rate_limited", "slow down")

    async def get_event(self, event_ticker: str) -> dict[str, Any]:
        self.attempts += 1
        await asyncio.sleep(0)
        raise RateLimitedError(429, "rate_limited", "slow down")

    async def get_api_limits(self) -> dict[str, Any]:
        """The tier probe the LIVE ``observe_api_tier`` actually calls. Without
        this the probe raises, falls back to ``LOWEST_TIER_LIMITS`` (basic,
        200 tok/s) and the soak silently grades itself against a SMALLER budget
        than the account it relights into — which understates read pressure
        (fewer tokens => fewer GETs attempted => fewer 429s => less loop churn).
        Returns the tier read read-only off LIVE PROD 2026-07-26."""
        return dict(OBSERVED_PROD_LIMITS)

    async def get(self, path: str, **_: object) -> dict[str, Any]:
        return dict(OBSERVED_PROD_LIMITS)


class _ContendedStore:
    """The real Store, with the divergence read subjected to the same queueing
    the saturated connection thread imposed: it waits behind whatever the tape
    writer is doing, and periodically never answers at all (the WAL checkpoint
    the incident logged as ``busy (wal_frames=57765, checkpointed=0)``)."""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.calls = 0
        self.hangs = 0

    async def open_ledger_identities(self) -> list[tuple[str, str, str]]:
        self.calls += 1
        # Every 3rd read is the pathological one: it NEVER returns.
        if self.calls % 3 == 0:
            self.hangs += 1
            await asyncio.Event().wait()
        # The rest are merely slow — queued behind the writer's batch.
        await asyncio.sleep(1.5)
        return await self._inner.open_ledger_identities()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def build(tmp: Path) -> Any:
    from tests.test_filters import Harness
    from tests.test_lifecycle import Rig
    from tests.test_metadata_change_scope import _armed_app
    from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp / "soak.sqlite3", h.clock)
    store.start_writer()  # THE CONTENTION: batch writer + WAL checkpoints
    rig = Rig(h, store)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    template = rig.lifecycle._open[next(iter(rig.lifecycle._open))].constructed
    await rig.lifecycle.cancel_all(ReasonCode.HALT_MANUAL)
    rig.lifecycle._clock = SystemClock()
    rig.lifecycle._store = _ContendedStore(store)  # type: ignore[assignment]
    knobs = SupKnobs()
    rig.lifecycle._withdraw_budget = WriteBudget.create(
        SystemClock(),
        capacity=knobs.write_budget_capacity,
        refill_s=knobs.write_budget_refill_s,
    )
    # COMPRESSION: the divergence sweep comes due every 0.5 s tick instead of
    # every 300 s, so 20 min holds ~2,400 launches of the await that stalled.
    try:
        object.__setattr__(
            rig.lifecycle._config, "ledger_divergence_sweep_interval_s", 0.001
        )
    except Exception:
        rig.lifecycle._config.ledger_divergence_sweep_interval_s = 0.001
    app = _armed_app(tmp)
    clock = SystemClock()
    app._clock = clock
    app._heartbeat = Heartbeat(clock, tmp / "heartbeat.txt")
    app._progress = ProgressLedger(clock, progress_path(tmp))
    app._config.supervisor.heartbeat_timeout_s = LIVE_WEDGE_TIMEOUT_S
    app._config.supervisor.poll_interval_s = SupKnobs().poll_interval_s
    return app, rig, store, template, clock


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20.0)
    args = ap.parse_args()

    import tempfile

    tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    tmp = Path(tmpdir.name)
    app, rig, store, template, clock = await build(tmp)

    wedge = LIVE_WEDGE_TIMEOUT_S
    maint_bound = wedge + MAINTENANCE_TICK_INTERVAL_S
    quote_bound = wedge + POOL_DEADLINE_S + RFQ_MAX_QUEUE_DWELL_S

    rfq_work: asyncio.Queue[int] = asyncio.Queue()
    app._progress.register(
        LOOP_MAINTENANCE, interval_s=MAINTENANCE_TICK_INTERVAL_S, wedge_timeout_s=wedge
    )
    app._progress.register(
        LOOP_QUOTE,
        interval_s=POOL_DEADLINE_S + RFQ_MAX_QUEUE_DWELL_S,
        wedge_timeout_s=wedge,
        idle=rfq_work.empty,
    )

    cancelled: list[str] = []

    class _Exchange:
        async def list_open_quote_ids(self) -> list[str]:
            return ["q-live"]

        async def cancel_quote(self, quote_id: str) -> None:
            cancelled.append(quote_id)

    supervisor = SafetySupervisor(
        SupervisorConfig(
            heartbeat_path=tmp / "heartbeat.txt",
            kill_file=tmp / "KILL",
            reconcile_marker_path=tmp / "needs_reconcile",
            heartbeat_timeout_s=wedge,
            poll_interval_s=SupKnobs().poll_interval_s,
            progress_path_=progress_path(tmp),
        ),
        clock,
        exchange=_Exchange(),
    )
    hb_reader = HeartbeatReader(clock, tmp / "heartbeat.txt")
    prog_reader = ProgressReader(clock, progress_path(tmp))

    # ---- READ 429 PRESSURE ------------------------------------------------
    rest = _Rest429()
    tier = await observe_api_tier(rest)  # type: ignore[arg-type]
    read_budget = TokenBudget.create(
        clock, capacity=tier.read_capacity, refill_s=tier.read_capacity / tier.read_refill_per_s
    )
    metadata = MetadataCache(
        rest,  # type: ignore[arg-type]
        clock,
        read_budget=read_budget,
        read_token_cost=DEFAULT_ENDPOINT_TOKEN_COST,
    )

    stop = asyncio.Event()
    stats: dict[str, int] = {
        "rfqs": 0,
        "meta_429": 0,
        "meta_deferred": 0,
        "waves": 0,
        "wave_deleted": 0,
        "wave_failures": 0,
        "store_writes": 0,
        "maint_ticks": 0,
        "delete_calls": 0,
    }

    async def quote_workers(worker: int) -> None:
        """The RFQ worker shape: dequeue, mark progress, touch metadata (which
        is where the 429 pressure lands), repeat."""
        while not stop.is_set():
            try:
                n = await asyncio.wait_for(rfq_work.get(), 0.5)
            except TimeoutError:
                continue
            app._progress.mark(LOOP_QUOTE)
            stats["rfqs"] += 1
            ticker = f"KXMLBTOTAL-SOAK-{n % INCIDENT_DISTINCT_TICKERS}"
            if metadata.peek(ticker) is None:
                try:
                    await metadata.refresh(ticker)
                except RateLimitedError:
                    stats["meta_429"] += 1
                except Exception as exc:  # ReadBudgetExhausted -> local refusal
                    if type(exc).__name__ == "ReadBudgetExhausted":
                        stats["meta_deferred"] += 1
                    else:
                        raise
            rfq_work.task_done()

    async def rfq_feeder() -> None:
        """Drive the incident's RFQ arrival rate (596/s), compressed."""
        n = 0
        while not stop.is_set():
            for _ in range(60):
                n += 1
                rfq_work.put_nowait(n)
            await asyncio.sleep(0.1)

    async def tape_writer_pressure() -> None:
        """Saturate the single aiosqlite connection thread exactly as the live
        tape does: continuous appends through the real batching writer."""
        n = 0
        while not stop.is_set():
            for _ in range(200):
                n += 1
                await store.record_decision(
                    kind="skip",
                    rfq_id=f"soak-{n}",
                    reasons=["SOAK"],
                    context={"n": n, "pad": "x" * 512},
                )
                stats["store_writes"] += 1
            await asyncio.sleep(0.01)

    async def status_loop() -> None:
        """The real 15 s status tick: an end-of-game quarantine wave enforced
        through the LIVE ``cancel_quotes_touching``, every tick."""
        wave = 0
        while not stop.is_set():
            await asyncio.sleep(STATUS_TICK_INTERVAL_S)
            if stop.is_set():
                break
            app._progress.mark(LOOP_STATUS)
            wave += 1
            ticker = f"KXMLBTOTAL-WAVE-{wave}"
            for i in range(120):  # = live max_open_quotes
                qid = f"w{wave}-q{i}"
                from tests.test_pricing_engine import combo

                rig.lifecycle._open[qid] = OpenQuoteState(
                    quote_id=qid,
                    rfq=combo(
                        [{"market_ticker": ticker, "side": "yes", "event_ticker": "E1"}],
                        id=f"wr-{wave}-{i}",
                    ),
                    constructed=template,
                    leg_mids_cc={ticker: 5_000},
                    created_mono_ns=clock.monotonic_ns(),
                )

            async def deleter(quote_id: str) -> dict[str, Any]:
                stats["delete_calls"] += 1  # write tokens actually put on the wire
                await asyncio.sleep(0.040)  # the loaded end-of-game exchange
                return {}

            rig.lifecycle._sender.delete_quote = deleter  # type: ignore[assignment]
            d, f = await rig.lifecycle.cancel_quotes_touching(
                {ticker}, ReasonCode.DELETE_MARKET_QUARANTINED,
                budget_s=STATUS_TICK_INTERVAL_S,
            )
            stats["waves"] += 1
            stats["wave_deleted"] += d
            stats["wave_failures"] += f

    real_tick = rig.lifecycle.maintenance_tick

    async def counted_tick() -> None:
        await real_tick()
        stats["maint_ticks"] += 1

    rig.lifecycle.maintenance_tick = counted_tick  # type: ignore[method-assign]

    # ---- observation ------------------------------------------------------
    max_hb_age = 0.0
    max_loop_age: dict[str, float] = {}
    verdicts: list[str] = []
    samples = 0

    tasks = [
        asyncio.create_task(app._liveness_loop(), name="liveness"),
        asyncio.create_task(app._maintenance_loop(rig.lifecycle), name="maintenance"),
        asyncio.create_task(status_loop(), name="status"),
        asyncio.create_task(rfq_feeder(), name="feeder"),
        asyncio.create_task(tape_writer_pressure(), name="tape"),
        *[asyncio.create_task(quote_workers(i), name=f"w{i}") for i in range(RFQ_WORKERS)],
    ]

    t0 = time.monotonic()
    deadline = t0 + args.minutes * 60.0
    next_print = t0 + 60.0
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.10)
            samples += 1
            age = hb_reader.read_age_s()
            if age is not None:
                max_hb_age = max(max_hb_age, age)
            payload = json.loads(progress_path(tmp).read_text())
            file_age = prog_reader.file_age_s(payload) or 0.0
            for name, entry in (payload.get("loops") or {}).items():
                a = float(entry.get("age_s", 0.0)) + file_age
                max_loop_age[name] = max(max_loop_age.get(name, 0.0), a)
            v = await supervisor.check_once()
            if v:
                verdicts.append(f"t+{time.monotonic()-t0:.1f}s {v}")
                break
            if time.monotonic() >= next_print:
                next_print += 60.0
                el = time.monotonic() - t0
                print(
                    f"  t+{el/60:5.1f}m  hb_max={max_hb_age:5.2f}s  "
                    f"maint_max={max_loop_age.get(LOOP_MAINTENANCE,0):5.2f}s  "
                    f"quote_max={max_loop_age.get(LOOP_QUOTE,0):5.2f}s  "
                    f"ticks={stats['maint_ticks']} rfqs={stats['rfqs']} "
                    f"429={stats['meta_429']} deferred={stats['meta_deferred']} "
                    f"waves={stats['waves']} writes={stats['store_writes']}",
                    flush=True,
                )
    finally:
        # BOUNDED TEARDOWN (2026-07-26). This harness DELIBERATELY injects a
        # store whose divergence reads never return, so a task parked on one
        # does not necessarily unwind on ``cancel()`` and a bare ``await t`` /
        # ``drain_diagnostic_sweeps()`` blocks FOREVER — which is exactly what
        # happened on the first 20-minute run: every measurement below had been
        # collected, and the process hung in teardown before it could print a
        # single one of them. The soak's whole job is to REPORT, so teardown is
        # best-effort and never allowed to eat the verdict. Bound is
        # STORE_OP_TIMEOUT_S, the store's own existing statement of "how long an
        # operation here may legitimately block" — no new number.
        stop.set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(asyncio.shield(t), STORE_OP_TIMEOUT_S)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        try:
            await asyncio.wait_for(
                rig.lifecycle.drain_diagnostic_sweeps(), STORE_OP_TIMEOUT_S
            )
        except (asyncio.TimeoutError, Exception):
            pass

    elapsed = time.monotonic() - t0
    counters = rig.metrics.snapshot()["counters"]
    hang_store = rig.lifecycle._store

    print("\n" + "=" * 78)
    print(f"SOAK  {elapsed/60:.2f} min  |  {samples} samples @100ms")
    print("=" * 78)
    print(f"tier observed        : {tier.usage_tier} read {tier.read_refill_per_s}/s "
          f"cap {tier.read_capacity} (observed={tier.observed})")
    print(f"maintenance ticks    : {stats['maint_ticks']}  "
          f"({stats['maint_ticks']/elapsed:.2f}/s, cadence {MAINTENANCE_TICK_INTERVAL_S}s)")
    print(f"RFQs through workers : {stats['rfqs']}  ({stats['rfqs']/elapsed:.0f}/s; "
          f"incident was {INCIDENT_RFQ_PER_S:.0f}/s)")
    print(f"metadata 429s emitted: {stats['meta_429']}  "
          f"({stats['meta_429']/elapsed:.2f}/s; incident sustained 17.2/s)")
    print(f"metadata deferred    : {stats['meta_deferred']} refused LOCALLY (never sent)")
    print(f"exchange GET attempts: {rest.attempts} "
          f"({rest.attempts/elapsed*DEFAULT_ENDPOINT_TOKEN_COST:.0f} tok/s vs "
          f"{tier.read_refill_per_s} tok/s ceiling)")
    print(f"store writes         : {stats['store_writes']} "
          f"({stats['store_writes']/elapsed:.0f}/s through the shared connection)")
    print(f"divergence reads     : {hang_store.calls} attempted, {hang_store.hangs} never returned")
    print(f"quarantine waves     : {stats['waves']} x 120 quotes = "
          f"{stats['wave_deleted']} withdrawn, {stats['wave_failures']} failures")
    _wkn = SupKnobs()
    _write_ceiling = _wkn.write_budget_capacity / _wkn.write_budget_refill_s
    print(f"withdrawal WRITE     : {stats['delete_calls']} DELETEs = "
          f"{stats['delete_calls']/elapsed*DELETE_QUOTE_TOKEN_COST:.1f} tok/s vs "
          f"{_write_ceiling:.0f} tok/s local budget "
          f"({_wkn.write_budget_capacity} tok / {_wkn.write_budget_refill_s}s) "
          f"and {tier.write_refill_per_s} tok/s exchange ceiling")
    print("-" * 78)
    print(f"MAX HEARTBEAT AGE        : {max_hb_age:6.3f}s   tolerance {wedge:.1f}s   "
          f"headroom {wedge-max_hb_age:6.3f}s  ({max_hb_age/wedge*100:.1f}% of tolerance)")
    for name, bound in ((LOOP_MAINTENANCE, maint_bound), (LOOP_QUOTE, quote_bound)):
        got = max_loop_age.get(name, 0.0)
        print(f"MAX {name:12s} PROGRESS: {got:6.3f}s   bound     {bound:.1f}s   "
              f"headroom {bound-got:6.3f}s  ({got/bound*100:.1f}% of bound)")
    st = max_loop_age.get(LOOP_STATUS)
    if st is not None:
        print(f"MAX status       PROGRESS: {st:6.3f}s   (observability only — not a kill signal)")
    print(f"SUPERVISOR VERDICT       : {verdicts or 'NONE - never fired'}")
    print(f"KILL file written        : {(tmp / 'KILL').exists()}   cancelled={cancelled}")
    print("-" * 78)
    for k in sorted(counters):
        if k.startswith(("ledger_divergence", "fills_ledger_sweep", "quote.delete",
                         "metadata.read_budget")):
            print(f"  {k:48s} {counters[k]}")

    ok = (
        not verdicts
        and not (tmp / "KILL").exists()
        and max_hb_age < wedge
        and max_loop_age.get(LOOP_MAINTENANCE, 0.0) < maint_bound
        and max_loop_age.get(LOOP_QUOTE, 0.0) < quote_bound
        and stats["maint_ticks"] > 0
    )
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}", flush=True)
    # HARD EXIT (2026-07-26, second occurrence). The bounded teardown above is
    # best-effort by design, but "best effort" still left the interpreter alive
    # after every measurement had printed: the injected never-returning store
    # read parks a non-daemon aiosqlite thread that neither ``cancel()`` nor
    # loop shutdown can join, so the process hung PAST its 20-minute deadline
    # with CPU flat — and any pipe on stdout (``| grep``, ``| tee``) held the
    # whole report in its buffer until the operator killed it by hand. The
    # verdict is the only product of this harness, so once it is written and
    # flushed the process exits immediately rather than waiting on threads that
    # exist only to be pathological. Nothing here touches a measurement.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
