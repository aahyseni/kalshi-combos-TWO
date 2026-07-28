"""SETTLEMENT PRIORITY + LEDGER RECONCILIATION (2026-07-27).

Two defects, measured on the live run ``data/live_20260727_1810.log`` and the
live store (read-only):

1. **PRIORITY INVERSION on the shared READ bucket.** One token bucket served a
   CONTINUOUS high-volume consumer (metadata refresh) and a LOW-VOLUME
   correctness-critical one (settled-leg resolution). The high-volume consumer
   wins every race for the last token, so what fails is the read that decides
   whether we know a leg settled (27 ``settled_fetch_failed``, every one a 429).
   FIX: the bucket carries a CRITICAL RESERVE — routine spenders yield at a
   floor the critical tier may draw. The reserve is DERIVED: the resolver's own
   bounded per-pass claim (``FETCH_BUDGET_PER_PASS``) × the live-verified
   per-endpoint token cost (``DEFAULT_ENDPOINT_TOKEN_COST``, from
   ``GET /account/endpoint_costs``). No typed constant.

2. **LEDGER ORPHANS.** ``open_ledger_rows=118`` vs ``open_positions=63``. The
   divergence was CONSTANT within a run and stepped ONLY at restarts: a combo
   that settled while the process was down is absent from ``get_positions`` at
   the next boot, so the exposure book never holds it, so the settlement
   handler dropped its row as "not ours" and the open ledger row lived forever
   (56 rows, $775.85 of cost basis). FIX: close those rows from EXCHANGE TRUTH,
   ledger-only, fail-closed on any ambiguity — never delete, never guess.

Every test drives the LIVE modules (rule 8). No credentials, no live process.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.exchange.rest import (
    DEFAULT_ENDPOINT_TOKEN_COST,
    KalshiApiError,
    observe_api_tier,
)
from combomaker.marketdata.metadata import MetadataCache
from combomaker.marketdata.settled import (
    FETCH_BUDGET_PER_PASS,
    SettledMarginalResolver,
)
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import QuoteApp, _PacedMarketSource, _StoreOrphanLedger
from combomaker.ops.write_budget import TokenBudget
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.settlement import SettlementHandler
from tests.test_maintenance_stall_and_read_budget import (
    OBSERVED_PROD_LIMITS,
    _CountingRest,
    _StubTierReader,
)
from tests.test_settlement import VERIFIED, FakeLifecycle, _settlement_row

CC = CentiCents
Q = CentiContracts

# The exchange settlement stamp the orphan close attributes realized P&L to.
SETTLED_TIME = "2026-07-27T02:30:00Z"


async def _observed_tier() -> Any:
    return await observe_api_tier(_StubTierReader(OBSERVED_PROD_LIMITS))


def _critical_reserve() -> int:
    """The reserve exactly as ``QuoteApp.run`` derives it — one settled
    resolution pass, at the live-verified per-call cost."""
    return FETCH_BUDGET_PER_PASS * DEFAULT_ENDPOINT_TOKEN_COST


def _app_with_budget(budget: TokenBudget, clock: Any) -> QuoteApp:
    """A QuoteApp shell whose REAL ``_reserve_read_token`` is under test.

    Built without ``__init__`` (a full AppConfig is irrelevant here) so the
    test drives the shipped method rather than a re-implementation of it."""
    app = cast(QuoteApp, QuoteApp.__new__(QuoteApp))
    app._read_budget = budget  # noqa: SLF001 — driving the live method
    app._clock = clock  # noqa: SLF001
    app._metrics = Metrics(clock)  # noqa: SLF001
    return app


class _StormRest:
    """A market source that never fails — so the ONLY thing that can stop a
    settlement resolution in these tests is the read budget."""

    def __init__(self, status: str = "finalized", result: str = "no") -> None:
        self.calls = 0
        self._status = status
        self._result = result

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.calls += 1
        return {"market": {"ticker": ticker, "status": self._status, "result": self._result}}


# =============================================================================
# 1. THE PRIORITY ITSELF
# =============================================================================


async def test_reserve_is_derived_from_measured_state_not_typed() -> None:
    """The reserve is the resolver's own pass budget × the endpoint's OBSERVED
    token cost, against the OBSERVED tier bucket — three measured inputs, no
    fourth number."""
    tier = await _observed_tier()
    reserve = _critical_reserve()
    assert reserve == FETCH_BUDGET_PER_PASS * DEFAULT_ENDPOINT_TOKEN_COST == 50
    # It costs the routine tier a small, stated slice of the observed bucket.
    assert reserve < tier.read_capacity
    assert reserve / tier.read_capacity < 0.1
    budget = TokenBudget.create(
        SystemClock(),
        capacity=tier.read_capacity,
        refill_s=tier.read_refill_s,
        reserve=reserve,
    )
    # ...and it is exactly ONE critical pass, not a token more.
    assert budget.reserve == FETCH_BUDGET_PER_PASS * DEFAULT_ENDPOINT_TOKEN_COST


def test_routine_yields_at_the_floor_while_critical_drains_it() -> None:
    """Drain with ROUTINE spends: refusals must begin while the reserve is
    still intact, and the CRITICAL tier must then get its whole pass."""
    clock = FakeClock()
    budget = TokenBudget.create(clock, capacity=600, refill_s=2.0, reserve=50)
    routine_spends = 0
    while budget.try_spend(DEFAULT_ENDPOINT_TOKEN_COST):
        routine_spends += 1
    assert routine_spends == (600 - 50) // DEFAULT_ENDPOINT_TOKEN_COST == 55
    assert budget.tokens == 50  # the floor, untouched by the routine tier
    for _ in range(FETCH_BUDGET_PER_PASS):
        assert budget.try_spend(DEFAULT_ENDPOINT_TOKEN_COST, critical=True)
    assert budget.tokens == 0
    # And a routine waiter's own sleep is measured against ITS floor.
    assert budget.seconds_until(DEFAULT_ENDPOINT_TOKEN_COST) > budget.seconds_until(
        DEFAULT_ENDPOINT_TOKEN_COST, critical=True
    )


def test_reserve_can_never_swallow_the_bucket() -> None:
    """The mirror defect (routine starved forever) is refused at construction."""
    clock = FakeClock()
    with pytest.raises(ValueError, match="reserve"):
        TokenBudget.create(clock, capacity=50, refill_s=2.0, reserve=50)
    with pytest.raises(ValueError, match="reserve"):
        TokenBudget.create(clock, capacity=600, refill_s=2.0, reserve=-1)
    # reserve=0 is the historical single-tier behaviour every write caller keeps.
    plain = TokenBudget.create(clock, capacity=10, refill_s=1.0)
    assert plain.reserve == 0
    assert plain.try_spend(10) and not plain.try_spend(1, critical=True)


# =============================================================================
# 2. UNDER A 429 STORM: settlement completes, metadata yields
# =============================================================================


async def test_under_a_read_storm_settlement_resolves_while_metadata_yields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """THE LIVE FAILURE, replayed on both arms.

    A metadata refresh wave hammers the shared bucket as hard as the loop will
    go (the 2026-07-26 storm's shape) while the settled resolver runs its
    bounded passes through the SHIPPED ``_PacedMarketSource`` →
    ``QuoteApp._reserve_read_token``.

    CONTROL (reserve=0, today's build): the resolver is refused by our own
    bucket — ``settled_read_budget_deferred``.
    SHIPPED (reserve = one pass): every pending resolution lands, zero
    deferrals, and metadata provably yielded (its refusals are non-zero).
    """
    clock = SystemClock()
    tier = await _observed_tier()
    tickers = [f"KXMLBGAME-26JUL27-{i:04d}" for i in range(64)]

    window_s = 3.0  # > the bounded wait (one full refill of the observed bucket)

    async def run_arm(reserve: int) -> tuple[int, int, int, int]:
        budget = TokenBudget.create(
            clock,
            capacity=tier.read_capacity,
            refill_s=tier.read_refill_s,
            reserve=reserve,
        )
        meta_rest = _CountingRest(clock)
        cache = MetadataCache(
            meta_rest,
            clock,
            ttl_s=0.0,  # always stale ⇒ every touch is a fetch attempt
            read_budget=budget,
            read_token_cost=DEFAULT_ENDPOINT_TOKEN_COST,
        )
        app = _app_with_budget(budget, clock)
        source = _PacedMarketSource(_StormRest(), app._reserve_read_token)  # noqa: SLF001
        resolver = SettledMarginalResolver(
            source, clock, retry_after_s=0.05, metrics=Metrics(clock)
        )
        for t in tickers:
            resolver.note_missing(t)
        stop = time.monotonic() + window_s

        async def storm() -> None:
            """The metadata refresh wave — CONCURRENT with the maintenance
            tick, exactly as the RFQ workers are in production."""
            i = 0
            while time.monotonic() < stop:
                try:
                    await cache.market(f"KXMLBTOTAL-{i:05d}")
                except KalshiApiError:
                    pass
                i += 1
                if i % 64 == 0:
                    await asyncio.sleep(0)

        hammer = asyncio.create_task(storm())
        resolved = 0
        while resolver.pending_count and time.monotonic() < stop:
            resolved += await resolver.resolve_pending()
            await asyncio.sleep(0)
        await hammer
        return (
            resolved,
            resolver.pending_count,
            resolver.read_budget_deferrals,
            cache.read_budget_refusals,
        )

    ctl_resolved, ctl_pending, ctl_deferrals, ctl_meta_refusals = await run_arm(0)
    shp_resolved, shp_pending, shp_deferrals, shp_meta_refusals = await run_arm(
        _critical_reserve()
    )

    with capsys.disabled():
        print(
            f"\n  SETTLEMENT UNDER A READ STORM "
            f"(tier={tier.usage_tier}, {tier.read_refill_per_s} tok/s, "
            f"cap {tier.read_capacity}, GET={DEFAULT_ENDPOINT_TOKEN_COST} tok, "
            f"{window_s}s window, {len(tickers)} pending)\n"
            f"    CONTROL (reserve=0)  : {ctl_resolved:3d} resolved, "
            f"{ctl_pending:3d} still pending, {ctl_deferrals:4d} settlement "
            f"deferrals, {ctl_meta_refusals} metadata refusals\n"
            f"    SHIPPED (reserve={_critical_reserve()}) : {shp_resolved:3d} resolved, "
            f"{shp_pending:3d} still pending, {shp_deferrals:4d} settlement "
            f"deferrals, {shp_meta_refusals} metadata refusals"
        )

    # The control MUST starve, or this proves nothing. Graded on the BACKLOG
    # (a robust margin: measured 9/64 resolved vs 64/64), not on the deferral
    # count, which is the sharper but timing-sensitive signature of the same
    # thing and is reported above.
    assert ctl_pending > 0, "the storm did not even starve the old build"
    assert shp_resolved > ctl_resolved
    # The shipped build: settlement resolution completes, never deferred...
    assert shp_deferrals == 0
    assert shp_pending == 0
    assert shp_resolved == len(tickers)
    # ...and metadata is what YIELDED (its refusals are local back-pressure,
    # never an emitted request, so nothing was 429'd).
    assert shp_meta_refusals > 0


async def test_sixty_four_pending_resolutions_clear_in_a_bounded_number_of_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live run's backlog size (64 ``settled_resolution_pending`` lines),
    cleared under the storm within the resolver's OWN bound: ceil(n / pass
    budget) passes. Nothing here is a chosen number — the bound is the pass
    budget the reserve is sized from."""
    clock = SystemClock()
    tier = await _observed_tier()
    budget = TokenBudget.create(
        clock,
        capacity=tier.read_capacity,
        refill_s=tier.read_refill_s,
        reserve=_critical_reserve(),
    )
    cache = MetadataCache(
        _CountingRest(clock),
        clock,
        ttl_s=0.0,
        read_budget=budget,
        read_token_cost=DEFAULT_ENDPOINT_TOKEN_COST,
    )
    app = _app_with_budget(budget, clock)
    rest = _StormRest()
    resolver = SettledMarginalResolver(
        _PacedMarketSource(rest, app._reserve_read_token),  # noqa: SLF001
        clock,
        retry_after_s=30.0,
        metrics=Metrics(clock),
    )
    for i in range(64):
        resolver.note_missing(f"KXMLBGAME-26JUL27-{i:04d}")

    bound = -(-64 // FETCH_BUDGET_PER_PASS)  # ceil
    passes = 0
    resolved = 0
    while resolver.pending_count and passes < bound:
        for _ in range(64):  # keep the storm on the bucket the whole time
            try:
                await cache.market(f"KXMLBTOTAL-{passes}-{_}")
            except KalshiApiError:
                pass
        resolved += await resolver.resolve_pending()
        passes += 1

    with capsys.disabled():
        print(
            f"\n  BACKLOG CLEARANCE : 64 pending -> {resolved} resolved in "
            f"{passes} passes (bound = ceil(64/{FETCH_BUDGET_PER_PASS}) = "
            f"{bound}); read-budget deferrals={resolver.read_budget_deferrals}"
        )
    assert resolved == 64
    assert resolver.pending_count == 0
    assert passes <= bound
    assert resolver.read_budget_deferrals == 0


async def test_a_starved_resolution_is_loud_and_counted() -> None:
    """Observability requirement: a resolution DUE for longer than one retry
    cycle raises a counter and a WARNING — the operator must never again be
    blind to "we are not learning what settled"."""
    clock = FakeClock()
    # A bucket that can NEVER pay for one GET (capacity < the endpoint's cost)
    # ⇒ ``seconds_until`` is inf ⇒ the shipped reservation refuses immediately
    # instead of waiting. Deterministic, and it drives the real code path.
    starving = TokenBudget.create(
        clock, capacity=DEFAULT_ENDPOINT_TOKEN_COST - 1, refill_s=1.0, reserve=0
    )
    app = _app_with_budget(starving, clock)
    resolver = SettledMarginalResolver(
        _PacedMarketSource(_StormRest(), app._reserve_read_token),  # noqa: SLF001
        clock,
        retry_after_s=30.0,
        metrics=Metrics(clock),
    )
    resolver.note_missing("KXMLBGAME-26JUL27-0001")
    assert await resolver.resolve_pending() == 0
    assert resolver.read_budget_deferrals == 1  # counted, and NOT an exchange failure
    assert resolver.pending_count == 1  # retried on the backoff, never dropped

    import structlog

    # Past ONE retry cycle with the entry still due ⇒ the loud line fires.
    clock.advance(31.0 + 31.0)
    with structlog.testing.capture_logs() as cap:
        await resolver.resolve_pending()
    starved = [e for e in cap if e["event"] == "settled_resolution_starved"]
    assert len(starved) == 1
    assert starved[0]["log_level"] == "warning"
    assert starved[0]["n_overdue"] == 1
    assert resolver.starved_passes >= 1


async def test_backoff_alone_is_not_reported_as_starvation() -> None:
    """A ticker deferred to its backoff/live floor is waiting BY DESIGN — the
    live run's one permanently-pending live-combo entry must not spam the
    alarm (that would train the operator to ignore it)."""
    clock = FakeClock()
    budget = TokenBudget.create(clock, capacity=600, refill_s=2.0, reserve=50)
    app = _app_with_budget(budget, clock)
    resolver = SettledMarginalResolver(
        _PacedMarketSource(_StormRest(status="active", result=""), app._reserve_read_token),  # noqa: SLF001
        clock,
        retry_after_s=30.0,
        metrics=Metrics(clock),
    )
    import structlog

    resolver.note_missing("KXMVESPORTS-LIVE-1")
    await resolver.resolve_pending()  # fetched → LIVE → dropped + floor armed
    resolver.note_missing("KXMVESPORTS-LIVE-1")  # lifecycle re-notes it
    clock.advance(5.0)
    with structlog.testing.capture_logs() as cap:
        await resolver.resolve_pending()
    assert not [e for e in cap if e["event"] == "settled_resolution_starved"]
    assert resolver.starved_passes == 0


# =============================================================================
# 3. LEDGER RECONCILIATION
# =============================================================================


def _position(
    position_id: str = "fill:q1",
    *,
    ticker: str = "KXMVE-C1",
    contracts: int = 100,
    entry_price: int = 5_000,
) -> OpenPosition:
    """LONG NO 1.00 ct @ $0.50 — the 2026-07-10 demo ground truth."""
    return OpenPosition(
        position_id=position_id,
        combo_ticker=ticker,
        collection=None,
        our_side=Side.NO,
        contracts=Q(contracts),
        entry_price_cc=CC(entry_price),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
    )


async def _ledger_rig(
    tmp_path: Path,
) -> tuple[Store, ExposureBook, SettlementHandler]:
    clock = FakeClock()
    store = await Store.open(tmp_path / "ledger.sqlite3", clock)
    exposure = ExposureBook(VERIFIED)
    killswitch = KillSwitch(clock)
    handler = SettlementHandler(
        exposure=exposure,
        balance_tracker=BalanceTracker(VERIFIED, clock, stale_after_s=1e9),
        lifecycle=FakeLifecycle(exposure, killswitch),
        killswitch=killswitch,
        orphan_ledger=_StoreOrphanLedger(store),
    )
    return store, exposure, handler


async def _row(store: Store, position_id: str) -> dict[str, Any]:
    got = await store.ledger_position(position_id)
    assert got is not None
    return got


async def test_a_settled_combo_closes_the_ledger_row_left_by_a_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE DEFECT, end to end: the combo settled while the process was down, so
    the exposure book does not hold it. The row must still close, with the
    realized P&L the live path would have booked and the EXCHANGE's own
    settled_time as the attribution stamp."""
    store, exposure, handler = await _ledger_rig(tmp_path)
    try:
        await store.record_position_open(_position("fill:q1"), subaccount="")
        assert not exposure.positions  # the restart lost it — the whole defect
        before = await store.open_ledger_identities()
        assert len(before) == 1

        rows = [
            _settlement_row(market_result="no", revenue=100) | {"settled_time": SETTLED_TIME}
        ]
        assert await handler.handle_settlements(rows) == []  # no live position booked

        after = await store.open_ledger_identities()
        settled = await _row(store, "fill:q1")
        with capsys.disabled():
            print(
                f"\n  ORPHAN CLOSE : open rows {len(before)} -> {len(after)}; "
                f"row status={settled['status']} realized="
                f"{settled['realized_pnl_cc']}cc reconciled_at="
                f"{settled['reconciled_at']}"
            )
        # The divergence counter the sweep reports returns to ZERO.
        assert after == []
        assert settled["status"] == "settled"
        # LONG NO 1.00ct @ $0.50 settling NO pays $1.00 ⇒ realized +$0.50.
        assert settled["realized_pnl_cc"] == 5_000
        assert settled["settled_value"] == 0.0
        assert handler.orphan_rows_closed == 1
        # Attributed to the night it SETTLED, not to the moment we noticed.
        assert str(settled["reconciled_at"]).startswith("2026-07-27T02:30:00")
        # ...so p_night's day-anchored seed picks it up in the right window.
        assert (
            await store.day_realized_pnl_cc(
                "2026-07-27T00:00:00+00:00", "2026-07-28T00:00:00+00:00"
            )
            == 5_000
        )
    finally:
        await store.close()


async def test_the_orphan_close_is_idempotent_across_repolls(tmp_path: Path) -> None:
    """The poller re-pages the same settlement every 30 s all night: realized
    P&L must be booked exactly once."""
    store, _, handler = await _ledger_rig(tmp_path)
    try:
        await store.record_position_open(_position("fill:q1"), subaccount="")
        rows = [
            _settlement_row(market_result="no", revenue=100) | {"settled_time": SETTLED_TIME}
        ]
        for _ in range(5):
            await handler.handle_settlements(rows)
        assert handler.orphan_rows_closed == 1
        assert await store.open_ledger_identities() == []
        assert (
            await store.day_realized_pnl_cc(
                "2026-07-27T00:00:00+00:00", "2026-07-28T00:00:00+00:00"
            )
            == 5_000
        )
    finally:
        await store.close()


async def test_an_ambiguous_row_is_never_closed_or_deleted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FAIL-CLOSED. Rows whose gross payout does not reconstruct the exchange's
    own revenue to the cent may not be this settlement's counterpart — they
    stay OPEN, loudly. A row that may represent real exposure is never deleted
    and never closed on a guess."""
    import structlog

    store, _, handler = await _ledger_rig(tmp_path)
    try:
        # TWO open rows on the ticker; the exchange revenue covers only one.
        await store.record_position_open(_position("fill:q1"), subaccount="")
        await store.record_position_open(
            _position("fill:q2", contracts=250), subaccount=""
        )
        rows = [
            _settlement_row(market_result="no", revenue=100) | {"settled_time": SETTLED_TIME}
        ]
        with structlog.testing.capture_logs() as cap:
            await handler.handle_settlements(rows)
        alarm = [e for e in cap if e["event"] == "settlement_orphan_row_ambiguous"]
        with capsys.disabled():
            print(
                f"\n  AMBIGUITY : predicted {alarm[0]['predicted_gross_cc']}cc vs "
                f"exchange revenue {alarm[0]['exchange_revenue_cc']}cc -> "
                f"{len(await store.open_ledger_identities())} rows LEFT OPEN"
            )
        assert len(alarm) == 1
        assert alarm[0]["log_level"] == "warning"
        assert handler.orphan_rows_closed == 0
        assert handler.orphan_rows_ambiguous == 2
        # BOTH rows survive, still open, still carrying their cost basis.
        assert len(await store.open_ledger_identities()) == 2
        assert (await _row(store, "fill:q1"))["status"] == "open"
        assert (await _row(store, "fill:q2"))["status"] == "open"
    finally:
        await store.close()


async def test_an_unattributable_settlement_leaves_the_row_open(
    tmp_path: Path,
) -> None:
    """No readable ``settled_time`` ⇒ the realized P&L could only be booked to
    the WRONG day. Refuse: leave the row open rather than corrupt the anchor."""
    import structlog

    store, _, handler = await _ledger_rig(tmp_path)
    try:
        await store.record_position_open(_position("fill:q1"), subaccount="")
        rows = [_settlement_row(market_result="no", revenue=100)]  # no settled_time
        with structlog.testing.capture_logs() as cap:
            await handler.handle_settlements(rows)
        assert [
            e for e in cap if e["event"] == "settlement_orphan_row_unattributable"
        ]
        assert len(await store.open_ledger_identities()) == 1
        assert (await _row(store, "fill:q1"))["status"] == "open"
        assert handler.orphan_rows_closed == 0
    finally:
        await store.close()


async def test_a_held_position_still_takes_the_live_path(tmp_path: Path) -> None:
    """BLAST RADIUS: when the exposure book DOES hold the position, nothing
    changes — the live reconcile/HALT path books it and the orphan path never
    fires (no double booking of realized P&L)."""
    store, exposure, handler = await _ledger_rig(tmp_path)
    try:
        pos = _position("fill:q1")
        exposure.add_position(pos)
        await store.record_position_open(pos, subaccount="")
        rows = [
            _settlement_row(market_result="no", revenue=100) | {"settled_time": SETTLED_TIME}
        ]
        results = await handler.handle_settlements(rows)
        assert len(results) == 1 and results[0].realized_cc == 5_000
        assert handler.orphan_rows_closed == 0  # the orphan path never ran
    finally:
        await store.close()


async def test_the_divergence_counter_returns_to_zero_after_the_orphan_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """END TO END through the LIVE sweep: the alarm that fired 4x all day with
    ``rows_without_position`` stuck at 55 must go CLEAN once the settlement
    lands — that is the whole point of making the divergence self-correcting
    instead of alarm-only."""
    import structlog

    from tests.test_ledger_durable_identity import _lifecycle

    clock = FakeClock(datetime(2026, 7, 27, 12, 0, tzinfo=UTC))
    store = await Store.open(tmp_path / "t.sqlite3", clock)
    try:
        lc = await _lifecycle(tmp_path, store, clock)
        # The restart shape: the ledger row exists, the exposure book does not
        # hold the position (it settled while the process was down).
        await store.record_position_open(_position("fill:q1"), subaccount="")
        with structlog.testing.capture_logs() as before:
            await lc._sweep_ledger_divergence()  # noqa: SLF001
        div = [e for e in before if e.get("event") == "position_ledger_divergence"]
        assert len(div) == 1 and div[0]["rows_without_position"] == 1

        killswitch = KillSwitch(clock)
        handler = SettlementHandler(
            exposure=lc._exposure,  # noqa: SLF001
            balance_tracker=BalanceTracker(VERIFIED, clock, stale_after_s=1e9),
            lifecycle=FakeLifecycle(lc._exposure, killswitch),  # noqa: SLF001
            killswitch=killswitch,
            orphan_ledger=_StoreOrphanLedger(store),
        )
        await handler.handle_settlements(
            [
                _settlement_row(market_result="no", revenue=100)
                | {"settled_time": SETTLED_TIME}
            ]
        )

        lc._ledger_divergence_last_mono_ns = None  # noqa: SLF001 — un-throttle
        with structlog.testing.capture_logs() as after:
            await lc._sweep_ledger_divergence()  # noqa: SLF001
        clean = [
            e for e in after if e.get("event") == "position_ledger_divergence_clean"
        ]
        with capsys.disabled():
            print(
                f"\n  DIVERGENCE SWEEP : rows_without_position "
                f"{div[0]['rows_without_position']} -> "
                f"{clean[0]['rows_without_position']} "
                f"(open rows {div[0]['open_ledger_rows']} -> "
                f"{clean[0]['open_ledger_rows']})"
            )
        assert len(clean) == 1
        assert clean[0]["rows_without_position"] == 0
        assert clean[0]["open_ledger_rows"] == 0
        assert not [
            e for e in after if e.get("event") == "position_ledger_divergence"
        ]
    finally:
        await store.close()


async def test_settled_leg_marginals_and_ledger_rows_are_independent_paths(
    tmp_path: Path,
) -> None:
    """No resolver wired / no orphan ledger wired ⇒ byte-identical prior
    behaviour (the row survives, alarm-only). The fix is opt-in at the wiring,
    so a test/backtest rig cannot accidentally mutate a ledger."""
    clock = FakeClock()
    store = await Store.open(tmp_path / "l.sqlite3", clock)
    try:
        exposure = ExposureBook(VERIFIED)
        killswitch = KillSwitch(clock)
        handler = SettlementHandler(
            exposure=exposure,
            balance_tracker=BalanceTracker(VERIFIED, clock, stale_after_s=1e9),
            lifecycle=FakeLifecycle(exposure, killswitch),
            killswitch=killswitch,
        )
        await store.record_position_open(_position("fill:q1"), subaccount="")
        await handler.handle_settlements(
            [_settlement_row(market_result="no", revenue=100) | {"settled_time": SETTLED_TIME}]
        )
        assert len(await store.open_ledger_identities()) == 1
    finally:
        await store.close()


# =============================================================================
# 4. THE RECONCILIATION MUST SCALE TO THE POLLER'S REAL BATCH
# =============================================================================


class _CountingOrphanLedger(_StoreOrphanLedger):
    """The shipped adapter, instrumented: counts the PER-TICKER reads (the
    needle) against the PER-BATCH scans (the haystack pre-filter)."""

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self.ticker_reads = 0
        self.batch_scans = 0

    async def open_ledger_tickers(self) -> set[str]:
        self.batch_scans += 1
        return await super().open_ledger_tickers()

    async def open_ledger_rows_for_ticker(
        self, combo_ticker: str
    ) -> list[dict[str, Any]]:
        self.ticker_reads += 1
        return await super().open_ledger_rows_for_ticker(combo_ticker)


async def _counting_rig(
    tmp_path: Path,
) -> tuple[Store, SettlementHandler, _CountingOrphanLedger]:
    clock = FakeClock()
    store = await Store.open(tmp_path / "scale.sqlite3", clock)
    exposure = ExposureBook(VERIFIED)
    killswitch = KillSwitch(clock)
    ledger = _CountingOrphanLedger(store)
    handler = SettlementHandler(
        exposure=exposure,
        balance_tracker=BalanceTracker(VERIFIED, clock, stale_after_s=1e9),
        lifecycle=FakeLifecycle(exposure, killswitch),
        killswitch=killswitch,
        orphan_ledger=ledger,
    )
    return store, handler, ledger


async def test_the_orphan_search_costs_one_scan_per_batch_not_one_read_per_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE POLLER PAGES THE WHOLE SETTLEMENT HISTORY EVERY 30 s (up to
    ``max_pages * page_limit`` = 10,000 rows). Only the handful of tickers that
    still carry an OPEN ledger row can possibly be orphans, so the search must
    cost ONE indexed scan per batch — not one DB round-trip per historical
    settlement. Without the pre-filter the 30 s loop would issue thousands of
    reads on the first pass of every process, on the same connection the
    settlement writes use."""
    store, handler, ledger = await _counting_rig(tmp_path)
    try:
        await store.record_position_open(
            _position("fill:q1", ticker="KXMVE-ORPHAN"), subaccount=""
        )
        # 2,000 historical settlements for combos we never held + the orphan.
        history = [
            _settlement_row(ticker=f"KXMVE-OLD-{i}", market_result="no", revenue=100)
            | {"settled_time": SETTLED_TIME}
            for i in range(2_000)
        ]
        batch = history + [
            _settlement_row(ticker="KXMVE-ORPHAN", market_result="no", revenue=100)
            | {"settled_time": SETTLED_TIME}
        ]
        await handler.handle_settlements(batch)
        with capsys.disabled():
            print(
                f"\n  ORPHAN SEARCH COST : {len(batch)} settlement rows -> "
                f"{ledger.batch_scans} batch scan(s), {ledger.ticker_reads} "
                f"per-ticker read(s); orphans closed={handler.orphan_rows_closed}"
            )
        # The needle is still found...
        assert handler.orphan_rows_closed == 1
        assert await store.open_ledger_identities() == []
        # ...and the haystack cost ONE scan, not 2,001 reads.
        assert ledger.batch_scans == 1
        assert ledger.ticker_reads == 1
    finally:
        await store.close()


async def test_a_row_opened_after_a_batch_is_still_reconciled_next_poll(
    tmp_path: Path,
) -> None:
    """The pre-filter must SUPPRESS work, never CORRECTNESS. A ticker with no
    open row when one batch started, but with one by the next batch, is still
    reconciled — the set is re-read at the top of every batch, so nothing is
    memoised for the life of the process."""
    store, handler, ledger = await _counting_rig(tmp_path)
    try:
        batch = [
            _settlement_row(ticker="KXMVE-LATE", market_result="no", revenue=100)
            | {"settled_time": SETTLED_TIME}
        ]
        await handler.handle_settlements(batch)  # nothing open yet
        assert handler.orphan_rows_closed == 0
        assert ledger.ticker_reads == 0  # correctly skipped

        # A restart rehydrates a row for that ticker AFTER the first batch.
        await store.record_position_open(
            _position("rehydrate:KXMVE-LATE", ticker="KXMVE-LATE"), subaccount=""
        )
        await handler.handle_settlements(batch)  # the SAME re-paged rows
        assert handler.orphan_rows_closed == 1
        assert await store.open_ledger_identities() == []
    finally:
        await store.close()


async def test_a_failed_batch_scan_falls_back_to_the_per_ticker_read(
    tmp_path: Path,
) -> None:
    """FAIL-OPEN TOWARD DOING THE WORK. If the batch scan errors, the
    pre-filter must become a no-op and every row take the per-ticker read —
    slower, never blind."""
    store, handler, ledger = await _counting_rig(tmp_path)
    try:
        await store.record_position_open(
            _position("fill:q1", ticker="KXMVE-ORPHAN"), subaccount=""
        )

        async def _boom() -> set[str]:
            raise RuntimeError("scan failed")

        ledger.open_ledger_tickers = _boom  # type: ignore[method-assign]
        await handler.handle_settlements(
            [
                _settlement_row(ticker="KXMVE-OTHER", market_result="no", revenue=100)
                | {"settled_time": SETTLED_TIME},
                _settlement_row(ticker="KXMVE-ORPHAN", market_result="no", revenue=100)
                | {"settled_time": SETTLED_TIME},
            ]
        )
        assert ledger.ticker_reads == 2  # both rows took the read
        assert handler.orphan_rows_closed == 1  # and the orphan still closed
    finally:
        await store.close()


async def test_the_starvation_alarm_is_throttled_to_one_per_retry_cycle() -> None:
    """A pass runs every few hundred ms; an unthrottled WARNING would emit
    thousands of identical lines an hour and TRAIN THE OPERATOR TO IGNORE IT.
    The throttle window is the resolver's OWN backoff — no new number — and it
    re-arms the moment the starvation clears, so a fresh onset is still news."""
    import structlog

    clock = FakeClock()
    starving = TokenBudget.create(
        clock, capacity=DEFAULT_ENDPOINT_TOKEN_COST - 1, refill_s=1.0, reserve=0
    )
    app = _app_with_budget(starving, clock)
    resolver = SettledMarginalResolver(
        _PacedMarketSource(_StormRest(), app._reserve_read_token),  # noqa: SLF001
        clock,
        retry_after_s=30.0,
        metrics=Metrics(clock),
    )
    resolver.note_missing("KXMLBGAME-26JUL27-0001")
    await resolver.resolve_pending()
    clock.advance(62.0)  # comfortably past one retry cycle ⇒ overdue

    with structlog.testing.capture_logs() as cap:
        for _ in range(50):  # 50 passes inside ONE retry cycle
            await resolver.resolve_pending()
            clock.advance(0.5)
    fired = [e for e in cap if e["event"] == "settled_resolution_starved"]
    assert len(fired) == 1, f"alarm spammed {len(fired)}x in one cycle"

    # Past the window, still starving ⇒ it speaks again (never goes silent).
    clock.advance(31.0)
    with structlog.testing.capture_logs() as cap2:
        await resolver.resolve_pending()
    assert len([e for e in cap2 if e["event"] == "settled_resolution_starved"]) == 1


# =============================================================================
# 5. THE PRICE OF THE PRIORITY — what the ROUTINE tier actually gives up
# =============================================================================


def test_the_reserve_costs_burst_depth_only_never_sustained_read_rate() -> None:
    """GATE (b): prioritising settlement must not starve METADATA, because
    metadata staleness feeds the no-quote path and the breaker.

    A token bucket's SUSTAINED throughput is its refill rate; a floor only
    removes BURST DEPTH. So the exact, whole price of the reserve is: the
    routine tier loses ``reserve/cost`` calls of burst and waits
    ``reserve/refill_per_s`` longer at the bottom of the bucket. Its steady-state
    reads-per-second is unchanged, to the call."""
    cost = DEFAULT_ENDPOINT_TOKEN_COST
    reserve = _critical_reserve()

    def burst(reserve_tokens: int) -> int:
        b = TokenBudget.create(
            FakeClock(), capacity=600, refill_s=2.0, reserve=reserve_tokens
        )
        n = 0
        while b.try_spend(cost):
            n += 1
        return n

    def sustained(reserve_tokens: int, seconds: float) -> int:
        clock = FakeClock()
        b = TokenBudget.create(
            clock, capacity=600, refill_s=2.0, reserve=reserve_tokens
        )
        n = 0
        for _ in range(int(seconds * 1000)):  # drain-as-fast-as-possible, 1ms steps
            while b.try_spend(cost):
                n += 1
            clock.advance(0.001)
        return n

    b0, b1 = burst(0), burst(reserve)
    refill_per_s = 600 / 2.0
    extra_wait_s = reserve / refill_per_s
    short, long = 10.0, 30.0
    s0, s1 = sustained(0, short), sustained(reserve, short)
    l0, l1 = sustained(0, long), sustained(reserve, long)

    print(
        f"\n  ROUTINE TIER COST OF THE RESERVE (cap 600, {refill_per_s:.0f} tok/s, "
        f"GET={cost} tok, reserve={reserve})"
        f"\n    burst depth        : {b0} calls -> {b1} calls "
        f"(-{b0 - b1}, = reserve/cost)"
        f"\n    over {short:.0f}s          : {s0} calls -> {s1} calls "
        f"({(s1 / s0 - 1) * 100:+.2f}%)"
        f"\n    over {long:.0f}s          : {l0} calls -> {l1} calls "
        f"({(l1 / l0 - 1) * 100:+.2f}%)"
        f"\n    deficit is CONSTANT: {s0 - s1} == {l0 - l1} == burst loss "
        f"(a level shift, not a rate change)"
        f"\n    extra wait at the bucket floor : {extra_wait_s * 1000:.0f} ms"
    )
    # Burst depth drops by EXACTLY the reserve, in calls — nothing more.
    assert b0 - b1 == reserve // cost
    # THE POINT: the deficit is the one-time burst loss and does NOT grow with
    # the window. The routine tier keeps the bucket's full sustained rate, so
    # metadata refresh (and the no-quote/breaker paths it feeds) is unaffected
    # in steady state.
    assert s0 - s1 == b0 - b1
    assert l0 - l1 == s0 - s1
    # A bucket at rest still serves a routine call immediately — the reserve is
    # a floor, not a tax.
    assert TokenBudget.create(
        FakeClock(), capacity=600, refill_s=2.0, reserve=reserve
    ).seconds_until(cost) == pytest.approx(0.0)
