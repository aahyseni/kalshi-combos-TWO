"""The 2026-07-26T20:12:24Z MAINTENANCE-LOOP STALL, and the read-budget
saturation that ran underneath it.

What the tape actually says (``data/live_20260726_1606.log``)
-------------------------------------------------------------
A prior diagnosis blamed quarantine enforcement. That is disproved by the log:
``market_quarantine_enforced`` reported ``quotes_pulled: 0`` and finished in
5 ms, and the 15 s status loop that owns enforcement kept ticking on schedule
(``pricing_stats`` at 20:12:25.652, 20:12:40.716, 20:12:55.961). The loop that
went silent is MAINTENANCE, and the arithmetic names the await:

  20:07:24.4326  ``position_ledger_divergence`` — the run's ONLY completed
                 divergence check, and the sweep's cadence stamp.
  ... + 300.000s = 20:12:24.4326 — the next check comes due. The cadence is
                 ``ledger_divergence_sweep_interval_s = 300.0``.
  20:12:24.389   the last heartbeat beat (30.1 s before the 20:12:54.489 wedge
                 verdict) — the top of that very maintenance tick.
  20:12:24.456   ``peak_profile_snapshot`` — step 3 of the tick, ON the loop.
  ---            ``_sweep_ledger_divergence`` (step 8) awaits
                 ``open_ledger_identities()``. NOTHING from the maintenance loop
                 is ever logged again.
  20:12:54.489   ``supervisor_heartbeat_wedged age=30.1s > 30.0s``
  20:13:29.897   ``quote_app_stopped`` — final counters:
                 ``ledger_divergence.checks: 1``.

That last counter is the proof. ``checks`` is incremented AFTER the store read
returns, and the cadence stamp is written BEFORE it. So the second sweep STARTED
(it must have — the stamp is 300.000 s old) and never finished, for the ≥65 s
between coming due and process exit. The store underneath was a single aiosqlite
connection thread saturated by the tape writer: 46 ``store_writer_checkpoint_
failed``, the last reporting ``wal_frames=57765, checkpointed=0``.

Compounding it, a READ-BUDGET storm: 5,726 ``metadata_fetch_failed HTTP 429`` in
332 s (17.2 failed reads/s sustained, PEAK 183 in one second). A metadata GET
costs the default 10 tokens (verified live: ``GET /account/endpoint_costs`` →
``default_cost: 10``, and /markets/{ticker} is not among the 13 overrides), so
that peak attempted 1,830 read tokens/s against this account's observed 300/s.

The fixes these tests pin
-------------------------
 1. ALARM-ONLY sweeps are LAUNCHED, never awaited, each under a derived wall
    bound; a store that never answers becomes a logged, retried skip.
 2. ``_delete_quote`` (B3) drops a quote from the mirror only on a PROVED
    withdrawal (ack or 404). 429 / 5xx / timeout ⇒ it may still be resting and
    fillable, so it stays and is retried.
 3. Metadata reads are paced against the READ token bucket.
 4. Both buckets derive from the OBSERVED tier (``GET /account/limits``), and an
    unreadable tier fails safe to the LOWEST documented one.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.clock import SystemClock
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import (
    DEFAULT_ENDPOINT_TOKEN_COST,
    LOWEST_TIER_LIMITS,
    ApiTierLimits,
    KalshiApiError,
    RateLimitedError,
    ReadBudgetExhausted,
    observe_api_tier,
)
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.config import SupervisorConfig as SupervisorKnobs
from combomaker.ops.persistence import STORE_OP_TIMEOUT_S, Store
from combomaker.ops.quote_app import LOOP_MAINTENANCE, MAINTENANCE_TICK_INTERVAL_S
from combomaker.ops.supervisor import SafetySupervisor, SupervisorConfig
from combomaker.ops.write_budget import TokenBudget
from combomaker.rfq import lifecycle as lifecycle_mod
from combomaker.risk.heartbeat import Heartbeat
from combomaker.risk.progress import ProgressLedger, progress_path
from tests.test_filters import Harness
from tests.test_lifecycle import Rig
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

# The observed LIVE PROD tier, read read-only on 2026-07-26 via
# GET /account/limits (tools/diagnostics/account_write_bucket.py):
#   {"usage_tier": "advanced", "read": {"bucket_capacity": 600,
#    "refill_rate": 300}, "write": {"bucket_capacity": 600, "refill_rate": 300},
#    "grants": [{"exchange_instance": "event_contract", "level": "advanced",
#                "source": "manual"}]}
OBSERVED_PROD_LIMITS = {
    "usage_tier": "advanced",
    "read": {"bucket_capacity": 600, "refill_rate": 300},
    "write": {"bucket_capacity": 600, "refill_rate": 300},
    "grants": [{"exchange_instance": "event_contract", "level": "advanced"}],
}


# ==========================================================================
# REQUIREMENT 1 — a maintenance loop whose STORE CALL HANGS does not stall the
#                 loop and does not trip the supervisor: it logs, skips, retries
# ==========================================================================


class _HangingStore:
    """Wraps a real ``Store`` and makes exactly the incident's read hang.

    Everything else is the real store, so the tick does its real work; only
    ``open_ledger_identities`` — the await the log's arithmetic names — never
    returns, which is what a saturated aiosqlite connection thread looks like
    from the caller's side."""

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.hang = True
        self.calls = 0

    async def open_ledger_identities(self) -> list[tuple[str, str, str]]:
        self.calls += 1
        if self.hang:
            await asyncio.Event().wait()  # never returns — the incident
        return await self._inner.open_ledger_identities()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _rig_with_hanging_store(tmp_path: Path, db: str) -> tuple[Rig, _HangingStore]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    rig = Rig(h, store)
    hanging = _HangingStore(store)
    rig.lifecycle._store = hanging  # type: ignore[assignment]  # noqa: SLF001
    # Production runs on a real clock; the maintenance loop's own sleep is real
    # wall time, so the tick's wall bounds must be measured on the same ruler.
    rig.lifecycle._clock = SystemClock()  # noqa: SLF001 (test seam)
    return rig, hanging


async def test_a_hanging_store_read_never_stalls_the_maintenance_loop(
    tmp_path: Path,
) -> None:
    """THE INCIDENT, reproduced and bounded.

    Runs the REAL ``QuoteApp._maintenance_loop`` over a REAL ``QuoteLifecycle``
    whose ``open_ledger_identities`` never returns — the exact await the log's
    300.000 s cadence arithmetic names — against a REAL ``SafetySupervisor``
    reading REAL heartbeat + progress files.

    Wedge tolerance is squeezed to 1.0 s so the 3 s of wall time below is THREE
    TIMES the tolerance: pre-fix, that is a guaranteed emergency kill.
    """
    from tests.test_metadata_change_scope import _armed_app

    rig, hanging = await _rig_with_hanging_store(tmp_path, "hang.sqlite3")
    app = _armed_app(tmp_path)
    clock = SystemClock()
    app._clock = clock  # noqa: SLF001 (test seam: real wall ages)
    app._heartbeat = Heartbeat(clock, tmp_path / "heartbeat.txt")  # noqa: SLF001
    app._progress = ProgressLedger(clock, progress_path(tmp_path))  # noqa: SLF001
    wedge_timeout_s = 1.0
    app._progress.register(  # noqa: SLF001
        LOOP_MAINTENANCE,
        interval_s=MAINTENANCE_TICK_INTERVAL_S,
        wedge_timeout_s=wedge_timeout_s,
    )
    # Tighten the supervisor's own tolerance to match, so the run below is a
    # real test of the wedge verdict and not just a short run.
    app._config.supervisor.heartbeat_timeout_s = wedge_timeout_s  # noqa: SLF001
    app._config.supervisor.poll_interval_s = 0.1  # noqa: SLF001

    cancelled: list[str] = []

    class _Exchange:
        async def list_open_quote_ids(self) -> list[str]:
            return ["q1"]

        async def cancel_quote(self, quote_id: str) -> None:
            cancelled.append(quote_id)

    supervisor = SafetySupervisor(
        SupervisorConfig(
            heartbeat_path=tmp_path / "heartbeat.txt",
            kill_file=tmp_path / "KILL",
            reconcile_marker_path=tmp_path / "needs_reconcile",
            heartbeat_timeout_s=wedge_timeout_s,
            progress_path_=progress_path(tmp_path),
        ),
        clock,
        exchange=_Exchange(),
    )

    # Count completed ticks by wrapping the REAL tick (nothing about the tick
    # itself changes — the loop under test is production's).
    ticks = 0
    real_tick = rig.lifecycle.maintenance_tick

    async def counted_tick() -> None:
        nonlocal ticks
        await real_tick()
        ticks += 1

    rig.lifecycle.maintenance_tick = counted_tick  # type: ignore[method-assign]

    liveness = asyncio.create_task(app._liveness_loop())  # noqa: SLF001
    maintenance = asyncio.create_task(  # noqa: SLF001
        app._maintenance_loop(rig.lifecycle)
    )
    verdicts: list[str | None] = []
    deadline = time.monotonic() + 3.0
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            verdicts.append(await supervisor.check_once())
    finally:
        maintenance.cancel()
        liveness.cancel()
        for t in (maintenance, liveness):
            try:
                await t
            except asyncio.CancelledError:
                pass

    # 1. The hung read WAS entered — this really is the incident's await.
    assert hanging.calls >= 1

    # 2. The loop kept ADVANCING the whole time. 3.0 s at a 0.5 s cadence is
    #    ~6 ticks; anything above 1 disproves "the tick never returned".
    assert ticks >= 4, f"maintenance advanced only {ticks} ticks in 3.0s"

    # 3. The supervisor NEVER fired, at 3x its own wedge tolerance, and wrote
    #    no KILL and cancelled nothing.
    assert all(v is None for v in verdicts), f"false kill: {[v for v in verdicts if v]}"
    assert not (tmp_path / "KILL").exists()
    assert cancelled == []

    # 4. The sweep was SKIPPED, loudly, not silently: the still-running task is
    #    never stacked, and the counter says so.
    assert rig.metrics.counter("ledger_divergence.skipped_in_flight") >= 1
    # ...and it never completed a check (the store never answered).
    assert rig.metrics.counter("ledger_divergence.checks") == 0


async def test_the_prefix_shape_would_have_stalled_the_tick(tmp_path: Path) -> None:
    """The CONTROL. Awaiting the sweep INLINE — the pre-fix shape — hangs
    forever on the same store, while the shipped ``maintenance_tick`` returns
    promptly. Without this the test above only proves the loop is fast, not that
    this specific await was the stall."""
    rig, hanging = await _rig_with_hanging_store(tmp_path, "control.sqlite3")

    # BEFORE: awaited inline on the tick.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            rig.lifecycle._sweep_ledger_divergence(), 0.3  # noqa: SLF001
        )
    assert hanging.calls == 1

    # AFTER: the same store, the same due sweep, on the shipped tick.
    rig.lifecycle._ledger_divergence_last_mono_ns = None  # noqa: SLF001 (re-arm)
    started = time.monotonic()
    await rig.lifecycle.maintenance_tick()
    elapsed = time.monotonic() - started
    assert elapsed < 0.3, f"maintenance_tick took {elapsed:.3f}s"
    await asyncio.sleep(0)  # let the launched task reach its first await
    assert hanging.calls == 2  # it DID launch the sweep — just did not wait
    rig.lifecycle._diag_tasks["ledger_divergence"].cancel()  # noqa: SLF001


async def test_a_timed_out_sweep_is_logged_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store slower than the sweep's whole wall bound must DEGRADE: a loud
    ``ledger_divergence_sweep_timeout``, a counter, and a clean retry on the
    next cadence once the store recovers. Never a silent stall, never a leak."""
    rig, hanging = await _rig_with_hanging_store(tmp_path, "timeout.sqlite3")
    # The shipped bound is derived (STORE_OP_TIMEOUT_S = the store's own
    # busy_timeout, 5.0 s). Squeezed here so the test costs milliseconds; the
    # shipped value is asserted separately below.
    monkeypatch.setattr(lifecycle_mod, "_LEDGER_DIVERGENCE_SWEEP_TIMEOUT_S", 0.05)

    await rig.lifecycle.maintenance_tick()
    await rig.lifecycle.drain_diagnostic_sweeps()
    assert rig.metrics.counter("ledger_divergence.timeout") == 1
    assert rig.metrics.counter("ledger_divergence.checks") == 0

    # The store recovers; the next due sweep completes normally.
    hanging.hang = False
    rig.lifecycle._ledger_divergence_last_mono_ns = None  # noqa: SLF001 (next cadence)
    await rig.lifecycle.maintenance_tick()
    await rig.lifecycle.drain_diagnostic_sweeps()
    assert rig.metrics.counter("ledger_divergence.checks") == 1
    assert rig.metrics.counter("ledger_divergence.timeout") == 1


def test_sweep_bounds_are_derived_not_invented() -> None:
    """Every off-loop sweep bound comes out of a primitive that already existed:
    the store's own ``busy_timeout`` and the maintenance tick's own per-poll
    bound. No fresh literal, and each bound is the arithmetic of what its sweep
    actually does."""
    assert lifecycle_mod._LEDGER_DIVERGENCE_SWEEP_TIMEOUT_S == STORE_OP_TIMEOUT_S
    assert lifecycle_mod._FILLS_LEDGER_SWEEP_TIMEOUT_S == (
        2 * STORE_OP_TIMEOUT_S
        + lifecycle_mod._FILLS_SWEEP_MAX_PAGES
        * lifecycle_mod._MAINTENANCE_POLL_TIMEOUT_S
    )
    # And both stay well inside the maintenance loop's own stall bound, so a
    # sweep timing out can never itself age the loop past a wedge verdict.
    live = SupervisorKnobs()
    stall_after_s = live.heartbeat_timeout_s + MAINTENANCE_TICK_INTERVAL_S
    assert stall_after_s > lifecycle_mod._LEDGER_DIVERGENCE_SWEEP_TIMEOUT_S
    # The meaningful bound for an OFF-loop sweep is its own cadence: a timing-out
    # sweep must never still be running when the next one comes due, or timeouts
    # stack instead of retrying.
    cfg = lifecycle_mod.LifecycleConfig()
    assert (
        lifecycle_mod._LEDGER_DIVERGENCE_SWEEP_TIMEOUT_S
        < cfg.ledger_divergence_sweep_interval_s
    )
    assert (
        lifecycle_mod._FILLS_LEDGER_SWEEP_TIMEOUT_S
        < cfg.fills_ledger_sweep_interval_s
    )


# ==========================================================================
# REQUIREMENT 2 — B3: a 429/5xx/timeout on ``_delete_quote`` KEEPS the quote in
#                 the mirror and retries it; 404 / ack removes it
# ==========================================================================


class _Gone(KalshiApiError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "not found")


class _ServerError(KalshiApiError):
    def __init__(self) -> None:
        super().__init__(500, "internal", "boom")


async def _one_quote_rig(tmp_path: Path, db: str, delete: Any) -> tuple[Rig, str]:
    """A REAL quote, priced and posted through ``handle_rfq`` — so it is in the
    exposure book as well as the mirror, which is exactly the state B3 is about
    (a quote risk is still counting because it may still fill)."""
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    rig = Rig(h, store)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS))
    quote_id = next(iter(rig.lifecycle._open))  # noqa: SLF001 (test seam)
    rig.sender.delete_quote = delete  # type: ignore[assignment]
    return rig, quote_id


@pytest.mark.parametrize(
    ("name", "exc"),
    [
        ("429", RateLimitedError(429, "too_many_requests", "too many requests")),
        ("500", _ServerError()),
        ("timeout", TimeoutError()),
    ],
)
async def test_unresolved_delete_keeps_the_quote_in_the_mirror(
    tmp_path: Path, name: str, exc: BaseException
) -> None:
    """B3. The exchange never told us the quote is gone, so it may still be
    RESTING and FILLABLE. Forgetting it (the pre-fix unconditional
    ``_drop_quote``) is how a rate-limit storm leaves live quotes off our book —
    on a path that ran 1,137 times in the incident."""

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise exc

    rig, quote_id = await _one_quote_rig(tmp_path, f"b3-{name}.sqlite3", refuses)
    assert quote_id in rig.lifecycle._open  # noqa: SLF001

    await rig.lifecycle._delete_quote(  # noqa: SLF001
        quote_id, ReasonCode.DELETE_TTL_EXPIRED
    )

    # STILL OURS: in the mirror, still counted by risk, marked for retry.
    assert quote_id in rig.lifecycle._open  # noqa: SLF001
    assert quote_id in rig.lifecycle._exposure.open_quotes  # risk still counts it
    state = rig.lifecycle._open[quote_id]  # noqa: SLF001
    assert state.withdraw_pending_reason is ReasonCode.DELETE_TTL_EXPIRED
    # The ask is STAMPED (2026-07-26): the write-only ``withdraw_attempts``
    # counter is replaced by the happens-before key the read resolver needs.
    assert state.withdraw_asked_mono_ns > 0
    assert rig.metrics.counter("quote.delete_unresolved") == 1
    # NOT counted as a completed deletion.
    assert rig.metrics.counter("quote.deleted.delete_ttl_expired") == 0


@pytest.mark.parametrize("outcome", ["ack", "404"])
async def test_proved_withdrawal_removes_the_quote(
    tmp_path: Path, outcome: str
) -> None:
    """The other half: only a PROVED withdrawal drops it. An ack and a 404 are
    both proof the quote is off the wire and cannot fill."""

    async def responds(quote_id: str) -> dict[str, Any]:
        if outcome == "404":
            raise _Gone()
        return {}

    rig, quote_id = await _one_quote_rig(tmp_path, f"b3-ok-{outcome}.sqlite3", responds)
    await rig.lifecycle._delete_quote(  # noqa: SLF001
        quote_id, ReasonCode.DELETE_TTL_EXPIRED
    )
    assert quote_id not in rig.lifecycle._open  # noqa: SLF001
    assert quote_id not in rig.lifecycle._exposure.open_quotes
    assert rig.metrics.counter("quote.deleted.delete_ttl_expired") == 1
    assert rig.metrics.counter("quote.delete_unresolved") == 0
    if outcome == "404":
        assert rig.metrics.counter("quote.delete_already_gone") == 1


async def test_an_unresolved_delete_is_retried_by_the_next_maintenance_tick(
    tmp_path: Path,
) -> None:
    """Keeping the quote is only safe if something re-asks. That driver is the
    maintenance tick's WITHDRAW-PENDING RESOLVER (2026-07-26 — it replaced the
    reprice sweep's per-quote retry branch), every 0.5 s, for EVERY withdrawal
    reason including the event-driven ones (RFQ gone, risk eviction) whose
    trigger never fires twice. This rig wires no ``quote_lister``, so it
    exercises the fallback the design specifies for paper/backtest/minimal rigs:
    the METERED write drain, which still terminates. Here the RFQ-gone path
    429s, then the exchange recovers."""
    attempts: list[str] = []
    fail = True

    async def flaky(quote_id: str) -> dict[str, Any]:
        attempts.append(quote_id)
        if fail:
            raise RateLimitedError(429, "too_many_requests", "too many requests")
        return {}

    rig, quote_id = await _one_quote_rig(tmp_path, "b3-retry.sqlite3", flaky)
    rfq_id = rig.lifecycle._open[quote_id].rfq.rfq_id  # noqa: SLF001
    rig.lifecycle._by_rfq[rfq_id] = quote_id  # noqa: SLF001

    # The RFQ dies on the exchange. This trigger NEVER fires again.
    await rig.lifecycle.on_rfq_deleted(rfq_id, {})
    assert quote_id in rig.lifecycle._open  # noqa: SLF001 — kept, UNKNOWN
    assert len(attempts) == 1

    # Next maintenance tick: the sweep re-asks, still 429 ⇒ still kept.
    await rig.lifecycle.maintenance_tick()
    assert len(attempts) == 2
    assert quote_id in rig.lifecycle._open  # noqa: SLF001
    assert (
        rig.lifecycle._open[quote_id].withdraw_pending_reason  # noqa: SLF001
        is ReasonCode.DELETE_RFQ_GONE
    )

    # Exchange recovers: the next tick withdraws it for the ORIGINAL reason.
    fail = False
    await rig.lifecycle.maintenance_tick()
    assert quote_id not in rig.lifecycle._open  # noqa: SLF001
    assert rig.metrics.counter("quote.deleted.delete_rfq_gone") == 1


async def test_a_withdraw_pending_quote_is_never_re_evicted(tmp_path: Path) -> None:
    """A quote we are already trying to remove must not be picked again as an
    eviction victim: that would spend the whole bounded eviction pass
    re-deleting one quote instead of releasing a different game's exposure."""

    async def refuses(quote_id: str) -> dict[str, Any]:
        raise RateLimitedError(429, "too_many_requests", "too many requests")

    rig, quote_id = await _one_quote_rig(tmp_path, "b3-evict.sqlite3", refuses)
    assert rig.lifecycle._pick_eviction_victim(  # noqa: SLF001
        {"26JUL261335TORBOS"}, set()
    ) in (quote_id, None)
    await rig.lifecycle._delete_quote(  # noqa: SLF001
        quote_id, ReasonCode.DELETE_EVICTED_LOWER_EV
    )
    games = {
        lifecycle_mod.game_key(leg.event_ticker)
        for leg in rig.lifecycle._exposure.open_quotes[quote_id].legs  # noqa: SLF001
        if leg.event_ticker
    }
    assert (
        rig.lifecycle._pick_eviction_victim(games, set()) is None  # noqa: SLF001
    ), "a withdraw-pending quote was re-picked as an eviction victim"


# ==========================================================================
# REQUIREMENT 3 — metadata reads stay inside the READ token budget under a
#                 realistic refresh wave
# ==========================================================================


class _CountingRest:
    """A REST stand-in that counts GETs and stamps each with the wall second it
    landed in, so the test can grade a real per-second token rate."""

    def __init__(self, clock: SystemClock) -> None:
        self._clock = clock
        self.market_calls = 0
        self.event_calls = 0
        self.stamps: list[float] = []

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.market_calls += 1
        self.stamps.append(self._clock.now().timestamp())
        return {
            "market": {
                "ticker": ticker,
                "status": "active",
                "event_ticker": None,
                "close_time": None,
                "price_ranges": [{"start": "0.01", "end": "0.99", "step": "0.01"}],
            }
        }

    async def get_event(self, ticker: str) -> dict[str, Any]:
        self.event_calls += 1
        self.stamps.append(self._clock.now().timestamp())
        return {"event": {"event_ticker": ticker, "mutually_exclusive": True}}

    async def get_multivariate_collections(self, **params: Any) -> dict[str, Any]:
        return {}


def _peak_tokens_per_second(stamps: list[float], cost: int) -> float:
    """Max tokens emitted inside any 1-second window."""
    if not stamps:
        return 0.0
    ordered = sorted(stamps)
    peak = 0
    j = 0
    for i, t in enumerate(ordered):
        while ordered[j] <= t - 1.0:
            j += 1
        peak = max(peak, i - j + 1)
    return peak * cost


async def test_metadata_reads_stay_inside_the_read_budget_under_a_refresh_wave(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE 429 STORM, paced.

    Replays the incident's shape: a wave of RFQs naming a universe of legs whose
    metadata is not cached, retried on every RFQ that names them (the peek-None
    rule that made the storm self-sustaining). Measured on the tape:
    **5,726 FAILED metadata GETs in 332 s = 17.2/s sustained, peaking at 183 in
    one second** — 1,830 read tokens/s against this account's observed 300/s.

    UNPACED is the control arm; PACED is the shipped ``MetadataCache``. The
    paced arm must never exceed the bucket's own guarantee
    (``capacity + rate*T`` in any window T), which is what the exchange's bucket
    admits by construction.
    """
    clock = SystemClock()
    tier = await observe_api_tier(_StubTierReader(OBSERVED_PROD_LIMITS))
    cost = DEFAULT_ENDPOINT_TOKEN_COST
    # The universe of legs the incident kept re-fetching: 1,390 distinct tickers
    # appeared in the 5,726 failures.
    tickers = [f"KXMLBTOTAL-26JUL2613{i:04d}" for i in range(1390)]
    # Both arms run for the SAME wall window, hammered as hard as the event loop
    # will go — the incident's flow (244,337 RFQs in 410 s, every uncached leg
    # re-asked by the very next RFQ naming it) is at least this aggressive.
    window_s = 2.0

    async def hammer(cache: MetadataCache) -> tuple[float, int]:
        refused = 0
        started = time.monotonic()
        i = 0
        while time.monotonic() - started < window_s:
            try:
                await cache.market(tickers[i % len(tickers)])
            except ReadBudgetExhausted:
                refused += 1
            i += 1
            if i % 64 == 0:
                await asyncio.sleep(0)  # let the loop breathe, as production does
        return max(time.monotonic() - started, 1e-9), refused

    # ---- CONTROL: unpaced (the incident's shape) --------------------------
    unpaced_rest = _CountingRest(clock)
    unpaced = MetadataCache(unpaced_rest, clock, ttl_s=0.0)  # always stale
    unpaced_wall, _ = await hammer(unpaced)

    # ---- SHIPPED: paced against the OBSERVED read bucket -------------------
    paced_rest = _CountingRest(clock)
    budget = TokenBudget.create(
        clock, capacity=tier.read_capacity, refill_s=tier.read_refill_s
    )
    paced = MetadataCache(
        paced_rest, clock, ttl_s=0.0, read_budget=budget, read_token_cost=cost
    )
    paced_wall, refused = await hammer(paced)

    unpaced_rate = unpaced_rest.market_calls / unpaced_wall
    paced_rate = paced_rest.market_calls / paced_wall
    unpaced_peak = _peak_tokens_per_second(unpaced_rest.stamps, cost)
    paced_peak = _peak_tokens_per_second(paced_rest.stamps, cost)

    with capsys.disabled():
        print(
            f"\n  READ PACING  (tier={tier.usage_tier}, "
            f"read {tier.read_refill_per_s} tok/s cap {tier.read_capacity}, "
            f"metadata GET = {cost} tok)\n"
            f"    UNPACED : {unpaced_rest.market_calls} reads in "
            f"{unpaced_wall:.2f}s = {unpaced_rate:8.1f} reads/s = "
            f"{unpaced_rate * cost:9.1f} tok/s | peak-1s {unpaced_peak:.0f} tok/s\n"
            f"    PACED   : {paced_rest.market_calls} reads in "
            f"{paced_wall:.2f}s = {paced_rate:8.1f} reads/s = "
            f"{paced_rate * cost:9.1f} tok/s | peak-1s {paced_peak:.0f} tok/s "
            f"({refused} refused locally, 0 emitted 429s)\n"
            f"    CEILING : {tier.read_refill_per_s} tok/s sustained; "
            f"bucket guarantee in any 1s = capacity + rate = "
            f"{tier.read_capacity + tier.read_refill_per_s} tok"
        )

    # The control MUST breach — otherwise this bench proves nothing.
    assert unpaced_peak > tier.read_refill_per_s, (
        "the unpaced control did not even breach the ceiling — the wave is too "
        "small to be a test"
    )
    # The shipped path stays inside what the exchange's own bucket admits.
    bucket_guarantee = tier.read_capacity + tier.read_refill_per_s
    assert paced_peak <= bucket_guarantee, (
        f"paced peak {paced_peak} tok in 1s > bucket guarantee {bucket_guarantee}"
    )
    # And it is a real reduction, not a rounding artefact.
    assert paced_peak < unpaced_peak / 2
    # Refusals are LOCAL: nothing was emitted to be 429'd.
    assert paced.read_budget_refusals == refused > 0


async def test_a_local_refusal_is_not_an_exchange_rate_limit() -> None:
    """A refusal must never feed the 429-burst breaker: nothing touched the
    exchange, so counting it as the exchange refusing us would halt the bot on
    its own back-pressure."""
    clock = SystemClock()
    budget = TokenBudget.create(clock, capacity=1, refill_s=3600.0)
    cache = MetadataCache(
        _CountingRest(clock), clock, ttl_s=0.0, read_budget=budget, read_token_cost=10
    )
    with pytest.raises(ReadBudgetExhausted) as caught:
        await cache.refresh("KXMLBTOTAL-X")
    exc = caught.value
    assert isinstance(exc, KalshiApiError)  # existing handlers still degrade
    assert not isinstance(exc, RateLimitedError)  # ...but never the burst breaker
    assert exc.status == 0  # no HTTP status was ever received
    assert cache.read_budget_refusals == 1


async def test_no_budget_wired_is_unchanged_behaviour() -> None:
    """Paper/backtests/tests inject no budget and must be byte-for-byte the
    prior behaviour — the pacing is a live-only addition."""
    clock = SystemClock()
    rest = _CountingRest(clock)
    cache = MetadataCache(rest, clock, ttl_s=0.0)
    for _ in range(50):
        await cache.refresh("KXMLBTOTAL-X")
    assert rest.market_calls == 50
    assert cache.read_budget_refusals == 0


# ==========================================================================
# REQUIREMENT 4 — the budget DERIVES from the observed tier and FAILS SAFE to
#                 the lowest
# ==========================================================================


class _StubTierReader:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get_api_limits(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


async def test_tier_is_read_from_the_account_not_hard_coded() -> None:
    """The live prod observation of 2026-07-26, replayed through the parser.

    The code used to hard-code ``WRITE_TOKENS_PER_S = 300`` commented "we are on
    Advanced" while the only recorded in-repo observation
    (tests/fixtures/ground_truth/scenario_account_facts.jsonl, DEMO, 2026-07-06)
    said ``usage_tier: basic`` with a 100-token write bucket. The constant is
    gone; the value is read."""
    tier = await observe_api_tier(_StubTierReader(OBSERVED_PROD_LIMITS))
    assert tier.observed is True
    assert tier.usage_tier == "advanced"
    assert (tier.read_refill_per_s, tier.read_capacity) == (300, 600)
    assert (tier.write_refill_per_s, tier.write_capacity) == (300, 600)
    # Both buckets are then expressible as a TokenBudget with no extra numbers.
    assert tier.read_refill_s == pytest.approx(2.0)
    assert tier.write_refill_s == pytest.approx(2.0)
    # ...and the documented Advanced row (openapi-comms.md line 269: 300/300,
    # burst 2 s ⇒ capacity 600) is corroborated by the live account.


@pytest.mark.parametrize(
    "payload",
    [
        RuntimeError("network down"),
        KalshiApiError(401, "unauthorized", "nope"),
        "not-a-dict",
        {},
        {"usage_tier": "advanced", "read": None, "write": {"refill_rate": 0}},
    ],
)
async def test_an_unreadable_tier_fails_safe_to_the_lowest(payload: Any) -> None:
    """FAIL-SAFE, NOT FAIL-FAST. Guessing HIGH turns an unreadable response into
    a self-inflicted 429 storm on the shared account bucket — the very incident
    this derivation exists to prevent. Every unreadable shape lands on Basic."""
    tier = await observe_api_tier(_StubTierReader(payload))
    assert tier.read_refill_per_s <= LOWEST_TIER_LIMITS.read_refill_per_s
    assert tier.write_refill_per_s <= LOWEST_TIER_LIMITS.write_refill_per_s
    assert tier.write_capacity <= LOWEST_TIER_LIMITS.write_capacity
    if not isinstance(payload, dict) or not payload.get("read"):
        assert tier is LOWEST_TIER_LIMITS or tier.observed is True


def test_the_lowest_tier_matches_the_documented_floor() -> None:
    """docs/api-notes/openapi-comms.md line 268: ``| Basic | 200 | 100 | 1 s |``
    ⇒ read 200 tok/s, write 100 tok/s, bucket = rate x 1 s burst. It is also
    exactly what the one recorded in-repo account observation says."""
    assert LOWEST_TIER_LIMITS.usage_tier == "basic"
    assert LOWEST_TIER_LIMITS.read_refill_per_s == 200
    assert LOWEST_TIER_LIMITS.write_refill_per_s == 100
    assert LOWEST_TIER_LIMITS.write_capacity == 100
    assert LOWEST_TIER_LIMITS.observed is False


def test_the_write_budget_is_clamped_to_the_observed_bucket() -> None:
    """Both axes bind. The shipped operator knob (200 tokens / 10 s) fits the
    observed prod bucket unchanged, and is a 2x BURST breach of the fail-safe
    Basic bucket — so an unreadable tier must clamp it, not merely rate-limit
    it. Clamping only the sustained rate would have let the fail-safe path 429
    its own withdrawal wave."""
    live = SupervisorKnobs()
    prod = ApiTierLimits(
        usage_tier="advanced",
        read_refill_per_s=300,
        read_capacity=600,
        write_refill_per_s=300,
        write_capacity=600,
        observed=True,
    )
    assert prod.clamp_write_budget(
        live.write_budget_capacity, live.write_budget_refill_s
    ) == (live.write_budget_capacity, live.write_budget_refill_s)

    capacity, refill_s = LOWEST_TIER_LIMITS.clamp_write_budget(
        live.write_budget_capacity, live.write_budget_refill_s
    )
    assert capacity == LOWEST_TIER_LIMITS.write_capacity == 100
    # Sustained rate PRESERVED (20 tok/s) — clamping costs burst depth, not
    # throughput.
    assert capacity / refill_s == pytest.approx(
        live.write_budget_capacity / live.write_budget_refill_s
    )
    # ...and the clamped bucket now fits inside the Basic bucket.
    assert capacity <= LOWEST_TIER_LIMITS.write_capacity


def test_a_rate_over_the_tier_is_clamped_to_the_tier() -> None:
    """A hand-set knob above what the account can spend is not a budget."""
    capacity, refill_s = LOWEST_TIER_LIMITS.clamp_write_budget(100, 0.1)  # 1000 tok/s
    assert capacity / refill_s == pytest.approx(LOWEST_TIER_LIMITS.write_refill_per_s)
