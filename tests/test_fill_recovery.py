"""FILL-RECORD RECOVERY SWEEP (2026-07-16 P1 — real-money bug).

``on_quote_executed`` is the only writer of fills-ledger rows and fires only on
the exchange's ``quote_executed`` WS message, which has no replay: a missed
message left a REAL, confirm-committed fill permanently out of the ledger
(proven live 2026-07-16, quote 527b5a3a…). These tests cover the maintenance
sweep that repairs it:

- missed message ⇒ recovered EXACTLY ONCE via REST poll, through the SAME
  ``on_quote_executed`` path, with row values identical to the WS path;
- WS+poll replay (either order) ⇒ exactly one fills row (``fill_replay_skipped``),
  fill.count booked once;
- store-level INSERT-if-absent: a double ``record_fill`` on one fill_ref writes
  one fills row + one ev_ledger row (restart-safe idempotency);
- REST says ``cancelled`` ⇒ NO fills row, phantom position removed, state
  un-parked (the proper lapse);
- poll error / unreadable status ⇒ retried next tick (never assumed executed),
  bounded attempts then a LOUD exhausted metric;
- rate bound: at most 3 REST polls per maintenance tick;
- fail-closed edges: no quote_getter wired, non-positive/NaN delay, and a
  confirm that never succeeded are all never swept;
- config: RiskConfig.fill_record_recovery_after_s default/validation and the
  build_lifecycle_config pass-through.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from combomaker.ops.config import FiltersConfig, PricingConfig, RiskConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import build_lifecycle_config
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender, accepted_msg
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

JsonDict = dict[str, Any]

RECOVERY_AFTER_S = 10.0


class FakeQuoteGetter:
    """Scripted REST GET /communications/quotes/{id}: a payload per quote_id, or
    an Exception to raise. Records every poll so tests can assert the rate
    bound and the retry cadence."""

    def __init__(self) -> None:
        self.responses: dict[str, JsonDict | Exception] = {}
        self.calls: list[str] = []

    def script(self, quote_id: str, response: JsonDict | Exception) -> None:
        self.responses[quote_id] = response

    def script_status(self, quote_id: str, status: str, **fields: Any) -> None:
        self.script(
            quote_id, {"quote": {"id": quote_id, "status": status, **fields}}
        )

    async def get_quote(self, quote_id: str) -> JsonDict:
        self.calls.append(quote_id)
        response = self.responses.get(quote_id)
        if response is None:
            raise AssertionError(f"unscripted get_quote({quote_id!r})")
        if isinstance(response, Exception):
            raise response
        return response


class RecoveryRig:
    def __init__(
        self,
        h: Harness,
        store: Store,
        *,
        getter: FakeQuoteGetter | None,
        config: LifecycleConfig | None = None,
    ) -> None:
        self.h = h
        self.store = store
        self.sender = FakeSender()
        self.getter = getter
        self.exposure = ExposureBook(TEST_CONVENTIONS)
        self.metrics = Metrics()
        engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
        self.lifecycle = QuoteLifecycle(
            clock=h.clock,
            sender=self.sender,
            engine=engine,
            rfq_filter=RfqFilter(
                FiltersConfig(min_time_to_close_s=0.0).model_copy(
                    update={"allowed_leg_series_prefixes": None}
                ),
                h.feed, h.metadata, h.killswitch, h.clock,
            ),
            limits=LimitChecker(RiskLimits()),
            exposure=self.exposure,
            feed=h.feed,
            metadata=h.metadata,
            inplay=InPlayDetector(h.clock),
            killswitch=h.killswitch,
            conventions=TEST_CONVENTIONS,
            store=store,
            metrics=self.metrics,
            lastlook_policy=LastLookPolicy(),
            # Small MC so the inline book-risk refresh on maintenance ticks
            # stays cheap; recovery delay = the module default under test.
            config=config
            or LifecycleConfig(
                book_risk_mc_samples=200,
                fill_record_recovery_after_s=RECOVERY_AFTER_S,
            ),
            quote_getter=getter,
        )


async def _make_rig(
    tmp_path: Path,
    *,
    getter: FakeQuoteGetter | None,
    db: str = "recovery.sqlite3",
    config: LifecycleConfig | None = None,
) -> RecoveryRig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    return RecoveryRig(h, store, getter=getter, config=config)


def rfq(rfq_id: str = "rfq_1") -> Rfq:
    return combo(CROSS_EVENT_LEGS, id=rfq_id)


async def _confirmed_quote(rig: RecoveryRig, rfq_id: str = "rfq_1") -> str:
    """Quote → accept → CONFIRMED (no quote_executed delivered). Returns the
    quote id."""
    before = len(rig.sender.created)
    await rig.lifecycle.handle_rfq(rfq(rfq_id))
    assert len(rig.sender.created) == before + 1
    quote_id = str(rig.sender.created[-1]["id"])
    await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
    assert quote_id in rig.sender.confirmed
    return quote_id


async def _fill_rows(store: Store) -> list[tuple[Any, ...]]:
    async with store._db.execute(  # noqa: SLF001 - white-box ledger read
        "SELECT fill_ref, order_id, combo_ticker, our_side, contracts_centi,"
        " price_cc, fee_cc, expected_edge_cc, raw_json FROM fills ORDER BY id"
    ) as cursor:
        return [tuple(row) async for row in cursor]


# --------------------------------------------------------------------------- #
# Missed WS message ⇒ recovered exactly once, identical row values.            #
# --------------------------------------------------------------------------- #


async def test_missed_ws_message_recovered_exactly_once(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script_status(quote_id, "executed", creator_order_id="ord-rec-1")

    # Too early: within the recovery delay the WS message may still arrive.
    rig.h.clock.advance(1.0)
    await rig.lifecycle.maintenance_tick()
    assert getter.calls == []
    assert await rig.store.count("fills") == 0

    # Past the delay: exactly one poll, fill recovered via the SAME path.
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert getter.calls == [quote_id]
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill_recovery.swept") == 1
    assert rig.metrics.counter("fill_recovery.recovered") == 1
    assert rig.metrics.counter("fill.count") == 1

    # Terminal: later ticks never poll or record again.
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert getter.calls == [quote_id]
    assert await rig.store.count("fills") == 1

    (row,) = await _fill_rows(rig.store)
    assert row[0] == f"fill:{quote_id}"
    assert row[1] == "ord-rec-1"  # order_id from the quote payload
    assert "recovered_via_poll" in str(row[8])  # provenance in raw_json


async def test_recovered_row_values_identical_to_ws_path(tmp_path: Path) -> None:
    """The recovered fill runs the SAME on_quote_executed path, so every value
    column (ticker/side/size/price/fee/edge) matches a WS-delivered fill of the
    identical quote to the cent — only provenance (raw_json/order_id) differs."""
    getter = FakeQuoteGetter()
    ws_rig = await _make_rig(tmp_path, getter=None, db="ws.sqlite3")
    ws_quote = await _confirmed_quote(ws_rig)
    await ws_rig.lifecycle.on_quote_executed({"quote_id": ws_quote, "order_id": "o1"})
    (ws_row,) = await _fill_rows(ws_rig.store)

    poll_rig = await _make_rig(tmp_path, getter=getter, db="poll.sqlite3")
    poll_quote = await _confirmed_quote(poll_rig)
    getter.script_status(poll_quote, "executed", creator_order_id="o1")
    poll_rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await poll_rig.lifecycle.maintenance_tick()
    (poll_row,) = await _fill_rows(poll_rig.store)

    # Same books, same RFQ, same accept ⇒ identical fill_ref + value columns:
    # fill_ref, order_id, combo_ticker, our_side, contracts_centi, price_cc,
    # fee_cc, expected_edge_cc (everything except the raw provenance json).
    assert poll_row[:8] == ws_row[:8]


# --------------------------------------------------------------------------- #
# WS+poll replay (either order) ⇒ one row.                                      #
# --------------------------------------------------------------------------- #


async def test_ws_then_poll_replay_single_row(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script_status(quote_id, "executed")
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    assert await rig.store.count("fills") == 1
    # The sweep never polls a recorded fill (terminal state).
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert getter.calls == []
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill.count") == 1


async def test_poll_then_ws_replay_single_row(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script_status(quote_id, "executed")
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert await rig.store.count("fills") == 1
    # The late WS message replays through on_quote_executed: skipped, one row,
    # fill.count/markouts booked once.
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    assert await rig.store.count("fills") == 1
    assert await rig.store.count("ev_ledger") == 1
    assert rig.metrics.counter("fill.count") == 1


async def test_store_record_fill_is_insert_if_absent(tmp_path: Path) -> None:
    """Store-level guard (restart-safe): the SECOND record_fill on one fill_ref
    writes nothing — one fills row, one ev_ledger row — even with no lifecycle
    state at all (the WS+poll race collapsed to its essence)."""
    from combomaker.core.clock import FakeClock

    store = await Store.open(tmp_path / "s.sqlite3", FakeClock())
    try:
        kwargs: dict[str, Any] = dict(
            order_id="o1", combo_ticker="KXMVE-C1", our_side="yes",
            contracts_centi=1_000, price_cc=4_600, fee_cc=0,
            expected_edge_cc=123, raw={"src": "ws"},
        )
        assert await store.record_fill("fill:q1", **kwargs) is True
        assert await store.has_fill("fill:q1") is True
        kwargs["raw"] = {"src": "poll"}
        assert await store.record_fill("fill:q1", **kwargs) is False
        assert await store.count("fills") == 1
        assert await store.count("ev_ledger") == 1
        assert await store.has_fill("fill:other") is False
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# Cancelled ⇒ no row + the proper lapse.                                        #
# --------------------------------------------------------------------------- #


async def test_cancelled_quote_writes_no_row_and_lapses(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script_status(quote_id, "cancelled", cancellation_reason="rfq_voided")
    # The position was booked at confirm (irrevocable-fill assumption)…
    assert f"fill:{quote_id}" in rig.exposure.positions

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()

    # …but the exchange says the fill never executed: no ledger row, phantom
    # position removed, state un-parked, nothing left to sweep.
    assert await rig.store.count("fills") == 0
    assert f"fill:{quote_id}" not in rig.exposure.positions
    assert rig.lifecycle._executed_states == {}  # noqa: SLF001
    assert rig.metrics.counter("fill_recovery.cancelled") == 1
    assert rig.metrics.counter("fill_recovery.recovered") == 0

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert getter.calls == [quote_id]  # terminal — never polled again


# --------------------------------------------------------------------------- #
# Errors retry next tick; unreadable status is never assumed executed.         #
# --------------------------------------------------------------------------- #


async def test_poll_error_retries_next_tick_then_recovers(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script(quote_id, RuntimeError("rest boom"))

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("fill_recovery.errors") == 1
    assert await rig.store.count("fills") == 0

    await rig.lifecycle.maintenance_tick()  # next tick retries
    assert getter.calls == [quote_id, quote_id]
    assert rig.metrics.counter("fill_recovery.errors") == 2

    getter.script_status(quote_id, "executed")  # exchange back up
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("fill_recovery.recovered") == 1
    assert await rig.store.count("fills") == 1


async def test_unreadable_status_never_fabricates_a_fill(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script(quote_id, {"quote": {"id": quote_id}})  # NO status field

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert await rig.store.count("fills") == 0  # fail-closed: never assumed
    assert rig.metrics.counter("fill_recovery.errors") == 1
    assert rig.metrics.counter("fill_recovery.recovered") == 0


async def test_still_pending_status_counts_and_waits(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script_status(quote_id, "confirmed")  # execution timer still running

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("fill_recovery.still_pending") == 1
    assert await rig.store.count("fills") == 0

    getter.script_status(quote_id, "executed")  # …then it executes
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("fill_recovery.recovered") == 1
    assert await rig.store.count("fills") == 1


async def test_exhausted_after_bounded_attempts(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script(quote_id, RuntimeError("permanently down"))

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    for _ in range(12):  # more ticks than the 10-attempt budget
        await rig.lifecycle.maintenance_tick()
    assert len(getter.calls) == 10  # bounded — gave up after the budget
    assert rig.metrics.counter("fill_recovery.exhausted") == 1  # said so LOUDLY
    assert await rig.store.count("fills") == 0


# --------------------------------------------------------------------------- #
# Rate bound: at most 3 polls per maintenance tick.                            #
# --------------------------------------------------------------------------- #


async def test_sweep_rate_bounded_per_tick(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_ids = [
        await _confirmed_quote(rig, rfq_id=f"rfq_{i}") for i in range(4)
    ]
    for quote_id in quote_ids:
        getter.script_status(quote_id, "executed")

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert len(getter.calls) == 3  # capped this tick
    await rig.lifecycle.maintenance_tick()
    assert len(getter.calls) == 4  # the straggler lands next tick
    assert await rig.store.count("fills") == 4
    assert rig.metrics.counter("fill_recovery.recovered") == 4


# --------------------------------------------------------------------------- #
# Fail-closed edges: no getter / bad config / confirm never succeeded.         #
# --------------------------------------------------------------------------- #


async def test_no_quote_getter_never_sweeps(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, getter=None)
    await _confirmed_quote(rig)
    rig.h.clock.advance(RECOVERY_AFTER_S + 5.0)
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("fill_recovery.swept") == 0
    assert await rig.store.count("fills") == 0  # unchanged prior behaviour


@pytest.mark.parametrize("bad_after_s", [0.0, -1.0, float("nan")])
async def test_nonsense_recovery_delay_disables_sweep(
    tmp_path: Path, bad_after_s: float
) -> None:
    # Belt-and-braces below the RiskConfig validator: a nonsense delay wired
    # straight into LifecycleConfig must disable the sweep, never poll-storm.
    getter = FakeQuoteGetter()
    rig = await _make_rig(
        tmp_path,
        getter=getter,
        config=LifecycleConfig(
            book_risk_mc_samples=200, fill_record_recovery_after_s=bad_after_s
        ),
    )
    quote_id = await _confirmed_quote(rig)
    getter.script_status(quote_id, "executed")
    rig.h.clock.advance(60.0)
    await rig.lifecycle.maintenance_tick()
    assert getter.calls == []
    assert await rig.store.count("fills") == 0


async def test_unconfirmed_fill_is_never_swept(tmp_path: Path) -> None:
    """A confirm that FAILED client-side (unknown-committed) is the
    reservation-reconcile loop's territory — the sweep only repairs fills whose
    confirm SUCCEEDED (a quote_executed message was genuinely expected)."""
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    await rig.lifecycle.handle_rfq(rfq())
    quote_id = str(rig.sender.created[-1]["id"])
    rig.sender.fail_confirm = True
    await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
    assert rig.lifecycle._executed_states  # noqa: SLF001 — state parked
    rig.h.clock.advance(RECOVERY_AFTER_S + 5.0)
    await rig.lifecycle.maintenance_tick()
    assert getter.calls == []  # never polled: fill_confirmed_mono_ns is None


# --------------------------------------------------------------------------- #
# Config: RiskConfig field + pass-through.                                      #
# --------------------------------------------------------------------------- #


def test_risk_config_recovery_delay_default_and_validation() -> None:
    assert RiskConfig().fill_record_recovery_after_s == 10.0
    assert LifecycleConfig().fill_record_recovery_after_s == 10.0
    assert RiskConfig(fill_record_recovery_after_s=30.0).fill_record_recovery_after_s == 30.0
    for bad in (0.0, -5.0, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            RiskConfig(fill_record_recovery_after_s=bad)


def test_recovery_delay_passes_through_to_lifecycle_config() -> None:
    cfg = build_lifecycle_config(RiskConfig(fill_record_recovery_after_s=17.5))
    assert cfg.fill_record_recovery_after_s == 17.5
    assert not math.isnan(build_lifecycle_config(RiskConfig()).fill_record_recovery_after_s)
