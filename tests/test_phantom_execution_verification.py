"""PHANTOM EXECUTIONS — execution verification of the fills ledger
(2026-09-04 build, item D; report docs/reports/2026-09-04-build-poll-
recovery-double-book.md).

THE DEFECT: the exchange emitted ``quote_executed`` (WS) AND ``status:
executed`` (REST) for orders that never produced a ``/portfolio/fills`` row
and never became positions — 28 since 2026-07-27, 12 on 2026-08-26; six of
them (AC104B1B2E5: 14.00+3.00+3.24+3.00 on top of one real 43.47;
1F4E0958F23: 48.93+24.46 on top of 29.10) halted the 22:41 ET settlement
reconcile. Each was ONE row per quote (0 duplicate order_ids / fill_refs in
the live store) — not a double-book: the ledger simply believed the claim.

THE FIX these pin:
- every booked row is a CLAIM: the sweep proves it on /portfolio/fills by the
  message's exact exchange ``order_id`` (status booked → verified) and VOIDS
  it (→ phantom, across fills/position_ledger/ev_ledger, fee reversed,
  position/reservation/receivable removed) when the bounded verification
  reads the tape and never finds the order;
- a REST ``executed`` status with no WS message books NOTHING from the
  status — only a positive tape match keyed on ``creator_order_id`` writes
  the row (exactly once; a late WS replay is skipped);
- the 8/26 replay: a WS message whose ledger write is SLOW is neither
  "never arrived" (no REST poll) nor booked twice; the poll-synthesized
  replay and an exchange replay both skip;
- same-quote order_id races are replay skips, not "another fill_ref"
  conflicts; a genuine cross-quote conflict still never books a second row;
- store: idempotent verification-column migration on a legacy DB with
  duplicate order_ids (opens, logs, never crashes); partial UNIQUE index on
  fills(order_id) on a fresh DB; ``void_phantom_fill`` state machine;
  phantom rows leave ``fill_order_ids`` (a later tape print re-alarms);
- per-ticker ``ledger_quantity_mismatch`` vs /portfolio/positions
  (alarm-only), wired into the 5-minute position reconcile.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import (
    ledger_quantity_reconcile_once,
    position_reconcile_unmodeled_once,
)
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.reservation import ExchangePosition
from tests.test_fill_cancel_verification import (
    COMBO_TICKER,
    VERIFY_DELAY_S,
    FakeFillsGetter,
    FakePositionsRest,
    _verify_rig,
    taker_fill,
)
from tests.test_fill_recovery import (
    RECOVERY_AFTER_S,
    FakeQuoteGetter,
    RecoveryRig,
    _confirmed_quote,
    _fill_rows,
    _make_rig,
)
from tests.test_lifecycle import TEST_CONVENTIONS

JsonDict = dict[str, Any]

# The 2026-08-26 20:36Z phantom, verbatim shape (quote 7d391d94, order
# 01a03fc9-fc08…, client_order_id quote:<hash>:<quote_id>).
PHANTOM_ORDER_ID = "01a03fc9-fc08-78af-bcf3-a101bf951f6d"


def ws_executed_msg(quote_id: str, order_id: str = PHANTOM_ORDER_ID) -> JsonDict:
    return {
        "quote_id": quote_id,
        "rfq_id": "6b15ff92-4b25-4b8a-abda-230caf29e1cd",
        "order_id": order_id,
        "client_order_id": f"quote:00be467a:{quote_id}",
        "market_ticker": COMBO_TICKER,
        "executed_ts": "2026-08-26T20:36:37.726757Z",
        "_ws_recv_mono_ns": 1260231716689900,
    }


async def _drain() -> None:
    """Let fire-and-forget ledger writes (position_ledger open row) land."""
    pending = [
        t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
    ]
    if pending:
        await asyncio.wait(pending, timeout=2.0)


async def _tick(rig: RecoveryRig) -> None:
    await rig.lifecycle.maintenance_tick()
    await rig.lifecycle.drain_diagnostic_sweeps()


async def _fill_row(store: Store, fill_ref: str) -> dict[str, Any]:
    async with store._db.execute(  # noqa: SLF001 — white-box ledger read
        "SELECT status, verified_at, exchange_fill_id, order_id FROM fills WHERE fill_ref = ?",
        (fill_ref,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return {
        "status": row[0], "verified_at": row[1], "exchange_fill_id": row[2], "order_id": row[3]
    }


async def _ledger_row(store: Store, position_id: str) -> tuple[str, int] | None:
    async with store._db.execute(  # noqa: SLF001
        "SELECT status, contracts_centi FROM position_ledger WHERE position_id = ?",
        (position_id,),
    ) as cur:
        row = await cur.fetchone()
    return None if row is None else (str(row[0]), int(row[1]))


async def _ev_row(store: Store, fill_ref: str) -> tuple[int | None, int | None] | None:
    async with store._db.execute(  # noqa: SLF001
        "SELECT expected_edge_cc, realized_pnl_cc FROM ev_ledger WHERE fill_ref = ?",
        (fill_ref,),
    ) as cur:
        row = await cur.fetchone()
    return None if row is None else (row[0], row[1])


# --------------------------------------------------------------------------- #
# 1. A WS execution the tape never shows is VOIDED after bounded verification. #
# --------------------------------------------------------------------------- #


async def test_phantom_ws_execution_is_voided_after_bounded_verification(
    tmp_path: Path,
) -> None:
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    # The tape holds the ticker's REAL fill (a different order) and nothing
    # for the phantom's order — the exact 8/26 AC104B1B2E5 shape.
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="01a03ee0-real")]})
    # Force a nonzero trade fee so the reversal is exercised to the cent.
    rig.lifecycle._fill_fee_cc = lambda *a, **k: 123  # type: ignore[method-assign]  # noqa: SLF001
    quote_id = await _confirmed_quote(rig)
    await _drain()
    fill_ref = f"fill:{quote_id}"

    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id))
    assert [e["event"] for e in cap if e["event"] == "fill_verification_started"]
    assert await rig.store.count("fills") == 1
    assert (await _fill_row(rig.store, fill_ref))["status"] == "booked"
    assert rig.lifecycle._realized_pnl_cc == -123  # noqa: SLF001 — fee booked at write
    assert (await _ledger_row(rig.store, fill_ref)) == ("open", 1000)

    # Attempt 1 (immediately due), 2 and 3 (final): the order never appears.
    await _tick(rig)
    assert len(fills.calls) == 1
    assert fills.calls[0]["order_id"] == PHANTOM_ORDER_ID
    assert fill_ref in rig.exposure.positions  # kept while attempts remain
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await _tick(rig)
    assert len(fills.calls) == 2
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    assert len(fills.calls) == 3
    voided = [e for e in cap if e["event"] == "fill_phantom_execution_voided"]
    assert len(voided) == 1
    assert voided[0]["order_id"] == PHANTOM_ORDER_ID
    assert voided[0]["contracts_centi"] == 1000
    assert voided[0]["fee_reversed_cc"] == 123
    assert voided[0]["touched"] == {"fills": 1, "position_ledger": 1, "ev_ledger": 1}

    # Durable ledgers: fills phantom (row kept as audit trail), position
    # ledger phantom (never 'open', never 'settled'), EV row zeroed.
    row = await _fill_row(rig.store, fill_ref)
    assert row["status"] == "phantom"
    assert row["exchange_fill_id"] == "phantom:absent_from_portfolio_fills"
    assert row["verified_at"] is not None
    assert await rig.store.count("fills") == 1
    assert (await _ledger_row(rig.store, fill_ref)) == ("phantom", 1000)
    assert (await _ev_row(rig.store, fill_ref)) == (0, 0)
    # In-memory: position + reservation gone, fee reversed to the cent,
    # state un-parked, and the phantom's order is NOT "in the ledger" for
    # the fills-ledger sweep (a later tape print must re-alarm as a miss).
    assert fill_ref not in rig.exposure.positions
    assert rig.lifecycle._realized_pnl_cc == 0  # noqa: SLF001
    assert quote_id not in rig.lifecycle._executed_states  # noqa: SLF001
    assert rig.metrics.counter("fill_verify.phantom_voided") == 1
    assert rig.metrics.counter("fill_verify.verified") == 0
    assert PHANTOM_ORDER_ID not in await rig.store.fill_order_ids()
    # A voided row never comes back through the same order id: a replayed
    # executed message is refused by the ledger's order_id guard.
    await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id))
    assert await rig.store.count("fills") == 1
    # Terminal: nothing polls again.
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await _tick(rig)
    assert len(fills.calls) == 3


# --------------------------------------------------------------------------- #
# 2. A REAL WS execution is verified on the first read; then never polled.     #
# --------------------------------------------------------------------------- #


async def test_real_ws_execution_verified_on_first_read(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(
        COMBO_TICKER,
        {"fills": [taker_fill(order_id="other"), taker_fill(order_id=PHANTOM_ORDER_ID)]},
    )
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id))
    assert (await _fill_row(rig.store, fill_ref))["status"] == "booked"

    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    assert len(fills.calls) == 1
    assert fills.calls[0]["ticker"] == COMBO_TICKER
    assert fills.calls[0]["order_id"] == PHANTOM_ORDER_ID
    ok = [e for e in cap if e["event"] == "fill_verified_on_tape"]
    assert len(ok) == 1 and ok[0]["tape_cc"] == 1000 and ok[0]["booked_cc"] == 1000
    row = await _fill_row(rig.store, fill_ref)
    assert row["status"] == "verified"
    assert row["exchange_fill_id"] == "f-1"
    assert fill_ref in rig.exposure.positions
    assert rig.metrics.counter("fill_verify.verified") == 1
    assert rig.metrics.counter("fill_verify.count_mismatch") == 0

    for _ in range(3):
        rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
        await _tick(rig)
    assert len(fills.calls) == 1  # terminal
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill_verify.phantom_voided") == 0


async def test_verification_count_mismatch_is_alarm_only(tmp_path: Path) -> None:
    """A tape count that differs from the booked size (a partial) is
    alarmed — never a silent resize of an already-ledgered row."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="o1", count_fp="6.00")]})
    quote_id = await _confirmed_quote(rig)
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    mism = [e for e in cap if e["event"] == "fill_verified_count_mismatch"]
    assert len(mism) == 1 and mism[0]["tape_cc"] == 600 and mism[0]["booked_cc"] == 1000
    assert (await _fill_row(rig.store, f"fill:{quote_id}"))["status"] == "verified"
    assert rig.metrics.counter("fill_verify.count_mismatch") == 1


async def test_all_reads_errored_keeps_row_and_position(tmp_path: Path) -> None:
    """A 429 storm across every read of every round must never void a real
    fill: the row stays 'booked', the position stays, and the give-up is a
    loud ERROR (fill-safe: risk we could not disprove stays counted)."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills, attempts=1)
    fills.script(COMBO_TICKER, RuntimeError("429"))
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    with structlog.testing.capture_logs() as cap:
        for _ in range(4):
            await _tick(rig)
            rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    assert [e for e in cap if e["event"] == "fill_verify_round_failed"]
    assert len([e for e in cap if e["event"] == "fill_verify_unresolved"]) == 1
    assert (await _fill_row(rig.store, fill_ref))["status"] == "booked"
    assert fill_ref in rig.exposure.positions
    assert rig.metrics.counter("fill_verify.phantom_voided") == 0
    n_calls = len(fills.calls)
    await _tick(rig)  # terminal: no further polls
    assert len(fills.calls) == n_calls


# --------------------------------------------------------------------------- #
# 3. Genuine missed WS message: REST executed + tape fill ⇒ recovered ONCE.    #
# --------------------------------------------------------------------------- #


async def test_missed_ws_message_recovered_exactly_once_via_tape(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    getter.script_status(quote_id, "executed", creator_order_id="ord-rec-1")
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="ord-rec-1")]})

    # Past the recovery delay: the REST poll says executed — NOTHING is
    # booked from the status; verification is armed.
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await _tick(rig)
    assert getter.calls == [quote_id]
    assert await rig.store.count("fills") == 0
    assert rig.metrics.counter("fill_recovery.recovered") == 0
    assert rig.metrics.counter("fill_recovery.executed_status_verifying") == 1
    assert fill_ref in rig.exposure.positions  # booked at confirm, kept meanwhile

    # Next tick: the exact-key tape read finds the order ⇒ row written ONCE
    # through the normal writer, tape-proven ⇒ verified at write.
    await _tick(rig)
    assert len(fills.calls) == 1
    assert fills.calls[0]["order_id"] == "ord-rec-1"
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill_recovery.executed_status_verified") == 1
    assert rig.metrics.counter("fill.count") == 1
    (row,) = await _fill_rows(rig.store)
    assert row[0] == fill_ref and row[1] == "ord-rec-1"
    assert "recovered_via_fills_poll" in str(row[8])
    assert (await _fill_row(rig.store, fill_ref))["status"] == "verified"
    assert fill_ref in rig.exposure.positions

    # The late WS message (an exchange replay) is skipped: still one row.
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "ord-rec-1"})
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill.count") == 1
    assert rig.metrics.counter("fill_ledger.order_id_conflict") == 0
    # Terminal: no further REST polls of either kind.
    rig.h.clock.advance(RECOVERY_AFTER_S + VERIFY_DELAY_S)
    await _tick(rig)
    assert getter.calls == [quote_id]
    assert len(fills.calls) == 1


async def test_executed_status_without_tape_fill_books_nothing(tmp_path: Path) -> None:
    """REST says executed, the tape never shows the order (the 8/26 class
    caught BEFORE any row): no fills row, the confirm-booked position is
    removed after the bounded verification, loud phantom alarm."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    getter.script_status(quote_id, "executed", creator_order_id=PHANTOM_ORDER_ID)
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="01a03ee0-real")]})
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await _tick(rig)  # REST executed → verifying (claims the exact key)
    assert PHANTOM_ORDER_ID in rig.lifecycle._claimed_exchange_order_ids  # noqa: SLF001
    for i in range(3):
        with structlog.testing.capture_logs() as cap:
            await _tick(rig)
        assert len(fills.calls) == i + 1
        if i < 2:
            assert fill_ref in rig.exposure.positions
            rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    ph = [e for e in cap if e["event"] == "fill_recovery_executed_status_phantom"]
    assert len(ph) == 1 and ph[0]["expected_order_id"] == PHANTOM_ORDER_ID
    assert await rig.store.count("fills") == 0
    assert fill_ref not in rig.exposure.positions
    assert rig.metrics.counter("fill_recovery.executed_status_phantom") == 1
    assert rig.metrics.counter("fill_recovery.cancelled") == 0
    assert rig.metrics.counter("fill_recovery.recovered") == 0
    assert PHANTOM_ORDER_ID not in rig.lifecycle._claimed_exchange_order_ids  # noqa: SLF001
    assert rig.lifecycle._executed_states == {}  # noqa: SLF001


async def test_no_fills_getter_keeps_prior_direct_booking(tmp_path: Path) -> None:
    """Paper/minimal rigs with no /portfolio/fills getter keep the
    pre-2026-09-04 direct booking from the REST status (explicitly logged
    UNVERIFIED) — live always wires the getter."""
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    getter.script_status(quote_id, "executed", creator_order_id="o1")
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    rec = [e for e in cap if e["event"] == "fill_record_recovered_via_poll"]
    assert len(rec) == 1 and "UNVERIFIED" in rec[0]["detail"]
    assert await rig.store.count("fills") == 1
    assert (await _fill_row(rig.store, f"fill:{quote_id}"))["status"] == "booked"


# --------------------------------------------------------------------------- #
# 4. The 8/26 replay: slow write ⇒ no false "never arrived", ONE row.          #
# --------------------------------------------------------------------------- #


async def test_8_26_replay_slow_ws_write_never_polls_and_books_once(tmp_path: Path) -> None:
    """The live shape: quote_executed (WS) at T; the store write stalls; at
    T+10 s the sweep used to REST-poll and log "WS message never arrived"
    (75× on 8/26) and race the WS handler on the same row. Now: no REST
    poll while the write is in flight (a once-per-quote stall warning), the
    row lands once, the poll-synthesized replay and an exchange replay skip."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id=PHANTOM_ORDER_ID)]})
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    getter.script_status(quote_id, "executed", creator_order_id=PHANTOM_ORDER_ID)

    original = rig.store.record_fill
    gate = asyncio.Event()

    async def slow(*args: Any, **kwargs: Any) -> bool:
        await gate.wait()  # the saturated store
        return await original(*args, **kwargs)

    rig.store.record_fill = slow  # type: ignore[method-assign]

    task = asyncio.ensure_future(rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id)))
    await asyncio.sleep(0)  # the handler reaches the store write and parks
    state = rig.lifecycle._executed_states[quote_id]  # noqa: SLF001
    assert state.fill_write_inflight is True
    assert state.executed_msg is not None

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
        await _tick(rig)
    assert getter.calls == []  # NEVER a REST poll for a message we hold
    assert rig.metrics.counter("fill_recovery.swept") == 0
    assert len([e for e in cap if e["event"] == "fill_ledger_write_stalled"]) == 1  # once
    assert rig.metrics.counter("fill_ledger.write_stalled") == 1

    gate.set()
    await task
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill.count") == 1
    # The two racers the 8/26 log shows AFTER the row: the poll-synthesized
    # replay (recovered_via_poll) and an exchange WS replay — both skip.
    await rig.lifecycle.on_quote_executed(
        {"quote_id": quote_id, "order_id": PHANTOM_ORDER_ID, "recovered_via_poll": True}
    )
    await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id))
    assert await rig.store.count("fills") == 1
    assert await rig.store.count("ev_ledger") == 1
    assert rig.metrics.counter("fill.count") == 1
    assert rig.metrics.counter("fill_ledger.order_id_conflict") == 0
    # ... and the (real) fill verifies on the next tick.
    await _tick(rig)
    assert (await _fill_row(rig.store, fill_ref))["status"] == "verified"


async def test_failed_write_replays_held_message_without_rest_poll(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    original = rig.store.record_fill
    boom = {"armed": True}

    async def flaky(*args: Any, **kwargs: Any) -> bool:
        if boom["armed"]:
            boom["armed"] = False
            raise RuntimeError("database table is locked")
        return await original(*args, **kwargs)

    rig.store.record_fill = flaky  # type: ignore[method-assign]
    await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id))
    assert rig.metrics.counter("fill_ledger.write_failed") == 1
    assert await rig.store.count("fills") == 0
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    assert getter.calls == []  # replayed from the held message, no GET quote
    assert [e for e in cap if e["event"] == "fill_record_replayed_from_held_message"]
    assert rig.metrics.counter("fill_recovery.held_message_replayed") == 1
    assert await rig.store.count("fills") == 1
    (row,) = await _fill_rows(rig.store)
    assert row[1] == PHANTOM_ORDER_ID
    assert "client_order_id" in str(row[8])  # the ORIGINAL WS message landed


# --------------------------------------------------------------------------- #
# 5. Order-id guard: same quote = replay skip; another quote = conflict.       #
# --------------------------------------------------------------------------- #


async def test_same_quote_order_id_race_is_replay_not_conflict(tmp_path: Path) -> None:
    """Both racers passed ``has_fill`` before either wrote (the 38 8/26
    'fill_order_id_already_in_ledger' lines): the loser finds ITS OWN
    fill_ref under the order id ⇒ an info replay skip, zero conflicts."""
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id))
    assert await rig.store.count("fills") == 1
    state = rig.lifecycle._executed_states[quote_id]  # noqa: SLF001
    # Re-create the loser's view: its fill_recorded flag is still False and
    # its has_fill read raced to False.
    state.fill_recorded = False

    async def has_fill_false(_ref: str) -> bool:
        return False

    rig.store.has_fill = has_fill_false  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_executed(
            {"quote_id": quote_id, "order_id": PHANTOM_ORDER_ID, "recovered_via_poll": True}
        )
    skipped = [e for e in cap if e["event"] == "fill_replay_skipped"]
    assert len(skipped) == 1 and skipped[0]["via"] == "order_id"
    assert not [e for e in cap if e["event"] == "fill_order_id_already_in_ledger"]
    assert rig.metrics.counter("fill_ledger.order_id_conflict") == 0
    assert await rig.store.count("fills") == 1
    assert state.fill_recorded is True
    assert (await _fill_row(rig.store, fill_ref))["order_id"] == PHANTOM_ORDER_ID


async def test_cross_quote_order_id_conflict_never_books_second_row(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _make_rig(tmp_path, getter=getter)
    q1 = await _confirmed_quote(rig, rfq_id="rfq_1")
    await rig.lifecycle.on_quote_executed(ws_executed_msg(q1))
    q2 = await _confirmed_quote(rig, rfq_id="rfq_2")
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_executed(ws_executed_msg(q2))
    conflict = [e for e in cap if e["event"] == "fill_order_id_already_in_ledger"]
    assert len(conflict) == 1
    assert conflict[0]["existing_fill_ref"] == f"fill:{q1}"
    assert conflict[0]["existing_status"] == "booked"
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill_ledger.order_id_conflict") == 1
    # The store-level guard is the belt under the pre-check: a direct write
    # of the same order under a third fill_ref is refused too.
    assert (
        await rig.store.record_fill(
            "fill:q3", order_id=PHANTOM_ORDER_ID, combo_ticker=COMBO_TICKER,
            our_side="yes", contracts_centi=100, price_cc=5000, fee_cc=0,
            expected_edge_cc=1, raw={},
        )
        is False
    )
    assert await rig.store.count("fills") == 1


# --------------------------------------------------------------------------- #
# 6. Store: migration on legacy duplicates, unique index, void state machine.  #
# --------------------------------------------------------------------------- #


def _legacy_db(path: Path) -> None:
    """A pre-2026-09-04 store: no verification columns, no order_id index,
    and two rows sharing one order_id (the class the migration must survive)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, fill_ref TEXT NOT NULL,
            order_id TEXT, combo_ticker TEXT NOT NULL, our_side TEXT NOT NULL,
            contracts_centi INTEGER NOT NULL, price_cc INTEGER NOT NULL, fee_cc INTEGER,
            expected_edge_cc INTEGER, raw_json TEXT NOT NULL
        );
        CREATE INDEX idx_fills_ref ON fills (fill_ref);
        INSERT INTO fills VALUES
            (1,'2026-08-26T20:36:57','fill:a','dup','T','no',1400,6820,0,1,'{}');
        INSERT INTO fills VALUES
            (2,'2026-08-26T20:37:05','fill:b','dup','T','no',1400,6820,0,1,'{}');
        INSERT INTO fills VALUES (3,'2026-08-26T20:38:00','fill:c',NULL,'T','no',300,6820,0,1,'{}');
        INSERT INTO fills VALUES (4,'2026-08-26T20:39:00','fill:d',NULL,'T','no',300,6820,0,1,'{}');
        """
    )
    conn.commit()
    conn.close()


async def test_migration_on_legacy_duplicates_opens_and_logs(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    with structlog.testing.capture_logs() as cap:
        store = await Store.open(path, FakeClock())
    try:
        added = [e for e in cap if e["event"] == "fills_verification_columns_added"]
        assert len(added) == 1
        assert added[0]["columns"] == ["status", "verified_at", "exchange_fill_id"]
        idx = [e for e in cap if e["event"] == "fills_order_id_unique_index_unavailable"]
        assert len(idx) == 1
        assert idx[0]["n_duplicate_order_ids"] == 1
        assert idx[0]["duplicates"] == [("dup", 2)]
        # Legacy rows read 'booked'; the store is fully usable.
        assert await store.fill_status("fill:a") == "booked"
        assert await store.fill_status("fill:b") == "booked"
        assert await store.fill_ref_for_order_id("dup") == ("fill:a", "booked")
        assert await store.count("fills") == 4
        # New writes: a fresh order books; the duplicated order is refused by
        # the writer's guard even though the index could not be built.
        ok = await store.record_fill(
            "fill:new", order_id="fresh", combo_ticker="T", our_side="no",
            contracts_centi=100, price_cc=5000, fee_cc=0, expected_edge_cc=1, raw={},
        )
        assert ok is True
        refused = await store.record_fill(
            "fill:new2", order_id="dup", combo_ticker="T", our_side="no",
            contracts_centi=100, price_cc=5000, fee_cc=0, expected_edge_cc=1, raw={},
        )
        assert refused is False
        # Re-open is idempotent (columns present ⇒ nothing added, no crash).
        await store.close()
        with structlog.testing.capture_logs() as cap2:
            store = await Store.open(path, FakeClock())
        assert not [e for e in cap2 if e["event"] == "fills_verification_columns_added"]
        assert await store.count("fills") == 5
    finally:
        await store.close()


async def test_fresh_store_has_partial_unique_index_on_order_id(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    store = await Store.open(path, FakeClock())
    try:
        async with store._db.execute("PRAGMA index_list(fills)") as cur:  # noqa: SLF001
            names = {str(r[1]) for r in await cur.fetchall()}
        assert "idx_fills_order_id_unique" in names
        async with store._db.execute("PRAGMA table_info(fills)") as cur:  # noqa: SLF001
            cols = {str(r[1]) for r in await cur.fetchall()}
        assert {"status", "verified_at", "exchange_fill_id"} <= cols
        # A raw second row on one order_id is refused by SQLite itself; NULL
        # order_ids (legacy poll-recovered rows) stay exempt.
        ins = (
            "INSERT INTO fills (at, fill_ref, order_id, combo_ticker, our_side, contracts_centi,"
            " price_cc, raw_json) VALUES ('t', ?, ?, 'T', 'no', 1, 1, '{}')"
        )
        await store._db.execute(ins, ("fill:x", "o1"))  # noqa: SLF001
        try:
            await store._db.execute(ins, ("fill:y", "o1"))  # noqa: SLF001
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("duplicate order_id must be refused by the index")
        await store._db.execute(ins, ("fill:n1", None))  # noqa: SLF001
        await store._db.execute(ins, ("fill:n2", None))  # noqa: SLF001
        await store._db.commit()  # noqa: SLF001
    finally:
        await store.close()


async def test_void_phantom_fill_state_machine(tmp_path: Path) -> None:
    clock = FakeClock()
    store = await Store.open(tmp_path / "void.sqlite3", clock)
    try:
        pos = OpenPosition(
            position_id="fill:q1", combo_ticker="T", collection=None, our_side=Side.NO,
            contracts=CentiContracts(1400),
            entry_price_cc=CentiCents(6820),
            legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
        )
        await store.record_position_open(pos, subaccount="0")
        assert await store.record_fill(
            "fill:q1", order_id="o1", combo_ticker="T", our_side="no", contracts_centi=1400,
            price_cc=6820, fee_cc=7, expected_edge_cc=1330, raw={},
        )
        assert await store.fill_status("fill:q1") == "booked"
        assert "o1" in await store.fill_order_ids()
        day = clock.now().isoformat()
        assert await store.day_realized_pnl_cc("2000-01-01", "2100-01-01") == -7
        touched = await store.void_phantom_fill("fill:q1", reason="absent_from_portfolio_fills")
        assert touched == {"fills": 1, "position_ledger": 1, "ev_ledger": 1}
        assert await store.fill_status("fill:q1") == "phantom"
        assert (await _ledger_row(store, "fill:q1")) == ("phantom", 1400)
        assert (await _ev_row(store, "fill:q1")) == (0, 0)
        assert "o1" not in await store.fill_order_ids()  # a later tape print re-alarms
        assert await store.day_realized_pnl_cc("2000-01-01", "2100-01-01") == 0  # fee excluded
        assert day  # (the day anchor is irrelevant to the exclusion)
        # Terminal: a second void touches nothing; a phantom is never
        # resurrected by mark_fill_verified; open identities exclude it.
        assert await store.void_phantom_fill("fill:q1", reason="again") == {
            "fills": 0, "position_ledger": 0, "ev_ledger": 0
        }
        assert await store.mark_fill_verified("fill:q1", exchange_fill_id="f") is False
        assert await store.open_ledger_identities() == []
        # A booked row verifies exactly once.
        assert await store.record_fill(
            "fill:q2", order_id="o2", combo_ticker="T", our_side="no", contracts_centi=100,
            price_cc=5000, fee_cc=0, expected_edge_cc=1, raw={},
        )
        assert await store.mark_fill_verified("fill:q2", exchange_fill_id="f2") is True
        assert await store.mark_fill_verified("fill:q2", exchange_fill_id="f2") is False
        assert await store.fill_status("fill:q2") == "verified"
        assert await store.void_phantom_fill("fill:q2", reason="x") == {
            "fills": 0, "position_ledger": 0, "ev_ledger": 0
        }
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# 7. Per-ticker LEDGER quantity vs /portfolio/positions — alarm-only.          #
# --------------------------------------------------------------------------- #


def _ledger_pos(position_id: str, ticker: str, side: Side, centi: int) -> OpenPosition:
    return OpenPosition(
        position_id=position_id, combo_ticker=ticker, collection=None, our_side=side,
        contracts=CentiContracts(centi), entry_price_cc=CentiCents(6820),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E2", "no")),
    )


async def test_ledger_quantity_mismatch_alarm_only(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "q.sqlite3", FakeClock())
    try:
        # AC104B1B2E5 shape: one real row + one phantom row on one ticker.
        for pid, ticker, side, centi in (
            ("fill:real", "T-AC", Side.NO, 4347),
            ("fill:ph", "T-AC", Side.NO, 1400),
            ("fill:s", "T-SIDE", Side.YES, 100),
            ("fill:lo", "T-LEDGER-ONLY", Side.NO, 300),
            ("fill:ok", "T-OK", Side.NO, 500),
        ):
            await store.record_position_open(_ledger_pos(pid, ticker, side, centi), subaccount="0")
        exch = {
            "T-AC": ExchangePosition(side=Side.NO, contracts_centi=4347),
            "T-SIDE": ExchangePosition(side=Side.NO, contracts_centi=100),
            "T-OK": ExchangePosition(side=Side.NO, contracts_centi=500),
            "T-EXCH-ONLY": ExchangePosition(side=Side.NO, contracts_centi=42),
        }
        metrics = Metrics()
        with structlog.testing.capture_logs() as cap:
            out = await ledger_quantity_reconcile_once(store, exch, metrics)
        ev = [e for e in cap if e["event"] == "ledger_quantity_mismatch"]
        assert len(ev) == 1
        assert ev[0]["by_kind"] == {"quantity": 1, "side": 1, "ledger_only": 1, "exchange_only": 1}
        by_ticker = {m["ticker"]: m for m in out}
        assert by_ticker["T-AC"]["kind"] == "quantity"
        assert by_ticker["T-AC"]["ledger_contracts_centi"] == 5747  # 4347 + the phantom 1400
        assert by_ticker["T-AC"]["exchange_contracts_centi"] == 4347
        assert by_ticker["T-AC"]["ledger_rows"] == 2
        assert by_ticker["T-SIDE"]["kind"] == "side"
        assert by_ticker["T-LEDGER-ONLY"]["kind"] == "ledger_only"
        assert by_ticker["T-EXCH-ONLY"]["kind"] == "exchange_only"
        assert "T-OK" not in by_ticker
        assert metrics.counter("ledger_quantity.mismatch") == 4
        assert metrics.counter("ledger_quantity.mismatch.quantity") == 1
        # ALARM-ONLY: no ledger row changed.
        assert (await _ledger_row(store, "fill:ph")) == ("open", 1400)
        # Voiding the phantom (what the verification path does) makes it clean
        # for that ticker.
        await store.record_fill(
            "fill:ph", order_id="o-ph", combo_ticker="T-AC", our_side="no", contracts_centi=1400,
            price_cc=6820, fee_cc=0, expected_edge_cc=1, raw={},
        )
        await store.void_phantom_fill("fill:ph", reason="absent_from_portfolio_fills")
        out2 = await ledger_quantity_reconcile_once(store, exch, Metrics())
        assert "T-AC" not in {m["ticker"] for m in out2}
        # Fully clean ⇒ the clean event, nothing counted as a mismatch.
        clean_store = await Store.open(tmp_path / "clean.sqlite3", FakeClock())
        try:
            await clean_store.record_position_open(
                _ledger_pos("fill:ok", "T-OK", Side.NO, 500), subaccount="0"
            )
            m2 = Metrics()
            with structlog.testing.capture_logs() as cap2:
                assert await ledger_quantity_reconcile_once(
                    clean_store, {"T-OK": ExchangePosition(side=Side.NO, contracts_centi=500)}, m2
                ) == []
            assert [e["event"] for e in cap2] == ["ledger_quantity_mismatch_clean"]
            assert m2.counter("ledger_quantity.mismatch") == 0
        finally:
            await clean_store.close()
    finally:
        await store.close()


async def test_ledger_quantity_check_is_wired_into_position_reconcile(tmp_path: Path) -> None:
    """The 5-minute position reconcile (the loop that already holds the
    exchange payload) runs the ledger quantity check on every pass — same
    payload, no second GET."""
    store = await Store.open(tmp_path / "wired.sqlite3", FakeClock())
    try:
        exposure = ExposureBook(TEST_CONVENTIONS)
        pos = _ledger_pos("fill:T-AC", "T-AC", Side.NO, 4347)
        exposure.add_position(pos)
        await store.record_position_open(pos, subaccount="0")
        await store.record_position_open(
            _ledger_pos("fill:ph", "T-AC", Side.NO, 1400), subaccount="0"
        )
        rest = FakePositionsRest(
            {"market_positions": [{"ticker": "T-AC", "position_fp": "-43.47"}]}
        )
        metrics = Metrics()
        with structlog.testing.capture_logs() as cap:
            await position_reconcile_unmodeled_once(rest, exposure, store, metrics, subaccount=0)
        ev = [e for e in cap if e["event"] == "ledger_quantity_mismatch"]
        assert len(ev) == 1 and ev[0]["by_kind"] == {"quantity": 1}
        assert ev[0]["mismatches"][0]["ledger_contracts_centi"] == 5747
        assert metrics.counter("ledger_quantity.checks") == 1
        assert metrics.counter("ledger_quantity.mismatch") == 1
        # The in-memory book matched the exchange exactly: the OLD alarm is
        # silent while the DURABLE-ledger alarm fires — the 8/26 gap closed.
        assert metrics.counter("position_reconcile.quantity_divergence") == 0
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# 8. Repair tool: classification on the 8/26 shape + apply == Store.void.      #
# --------------------------------------------------------------------------- #


def _exists(path: str) -> bool:
    return Path(path).exists()


def _load_repair_tool() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools" / "ops" / "repair_phantom_fills.py"
    spec = importlib.util.spec_from_file_location("repair_phantom_fills", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tape_fill(order_id: str, ticker: str, count_fp: str) -> JsonDict:
    return {
        "fill_id": f"f-{order_id}", "order_id": order_id, "market_ticker": ticker,
        "ticker": ticker, "outcome_side": "no", "side": "no", "count_fp": count_fp,
        "no_price_dollars": "0.6820", "is_taker": False, "created_time": "2026-08-26T16:21:00Z",
    }


def test_repair_tool_classifies_the_8_26_shape() -> None:
    tool = _load_repair_tool()
    ac = "KX-AC104B1B2E5"
    rows = [
        {"fill_ref": "fill:real", "order_id": "01a03ee0-real", "combo_ticker": ac,
         "our_side": "no", "contracts_centi": 4347, "price_cc": 6750},
        {"fill_ref": "fill:7d391d94", "order_id": "01a03fc9-fc08", "combo_ticker": ac,
         "our_side": "no", "contracts_centi": 1400, "price_cc": 6820},
        {"fill_ref": "fill:568fb2d8-reconciled", "order_id": "b2c301fc-eecd",
         "combo_ticker": "KX-39F452DE826", "our_side": "no", "contracts_centi": 1635,
         "price_cc": 7660},
        {"fill_ref": "fill:never", "order_id": "deadbeef-0000", "combo_ticker": "KX-NEVER",
         "our_side": "no", "contracts_centi": 300, "price_cc": 5000},
        {"fill_ref": "fill:unres", "order_id": "cafe0000-0000", "combo_ticker": "KX-UNRES",
         "our_side": "no", "contracts_centi": 300, "price_cc": 5000},
        {"fill_ref": "fill:nokey", "order_id": None, "combo_ticker": ac, "our_side": "no",
         "contracts_centi": 1, "price_cc": 1},
    ]
    exchange = {
        "fills": [
            _tape_fill("01a03ee0-real", ac, "43.47"),
            _tape_fill("b2c301fc-eecd-4820-94b2-34909928e851", "KX-39F452DE826", "16.35"),
            _tape_fill("01a04023-tapeonly", "KX-TAPEONLY", "46.18"),
            _tape_fill("feed0000-0000", "KX-UNRES", "5.00"),  # tape holds SOME fill here
        ],
        "settlements": [
            {"ticker": ac, "market_result": "no", "no_count_fp": "43.47", "yes_count_fp": "0.00"},
            {"ticker": "KX-UNRES", "market_result": "no", "no_count_fp": "8.00",
             "yes_count_fp": "0.00"},  # 8.00 ≠ matched 5.00 ⇒ NOT corroborated
        ],
    }
    out = tool.classify(rows, exchange)
    assert out["by_class"] == {
        "legacy_prefix_match": 1, "matched": 1, "null_order_id": 1, "phantom": 2,
        "unresolved": 1,
    }
    phantom = {p["fill_ref"]: p for p in out["phantom"]}
    assert phantom["fill:7d391d94"]["_reason"] == "settlement_equals_matched_rows"
    assert phantom["fill:7d391d94"]["_settlement_cc"] == 4347
    assert phantom["fill:never"]["_reason"] == "no_settlement_no_tape_fill"
    assert out["unresolved"][0]["fill_ref"] == "fill:unres"
    assert out["legacy_prefix_match"][0]["_tape_order_id"] == "b2c301fc-eecd-4820-94b2-34909928e851"
    # Tape fills with no store row are listed (never written): the tape-only
    # order AND the un-corroborating KX-UNRES print.
    assert [t["order_id"] for t in out["tape_only"]] == ["01a04023-tapeonly", "feed0000-0000"]
    assert out["tape_only"][0]["contracts_centi"] == 4618


async def test_repair_tool_apply_matches_store_void_to_the_row(tmp_path: Path) -> None:
    """PARITY: the offline --apply must leave a store in EXACTLY the state the
    live ``Store.void_phantom_fill`` leaves it in (same three-ledger writes),
    and must back the file up first."""
    tool = _load_repair_tool()
    clock = FakeClock()

    async def seed(path: Path) -> Store:
        store = await Store.open(path, clock)
        pos = _ledger_pos("fill:ph", "T", Side.NO, 1400)
        await store.record_position_open(pos, subaccount="0")
        assert await store.record_fill(
            "fill:ph", order_id="o-ph", combo_ticker="T", our_side="no", contracts_centi=1400,
            price_cc=6820, fee_cc=0, expected_edge_cc=1330, raw={},
        )
        assert await store.record_fill(
            "fill:real", order_id="o-real", combo_ticker="T", our_side="no",
            contracts_centi=4347, price_cc=6750, fee_cc=0, expected_edge_cc=10, raw={},
        )
        return store

    live = await seed(tmp_path / "live.sqlite3")
    await live.void_phantom_fill("fill:ph", reason="absent_from_portfolio_fills")
    await live.close()

    offline = await seed(tmp_path / "offline.sqlite3")
    await offline.close()
    outcome = tool.apply_void(
        str(tmp_path / "offline.sqlite3"),
        [{"fill_ref": "fill:ph", "_reason": "settlement_equals_matched_rows"}],
        migrate=False,
        backup_dir=tmp_path / "backups",
    )
    assert outcome["touched"] == {"fills": 1, "position_ledger": 1, "ev_ledger": 1}
    assert _exists(outcome["backup"])

    def snapshot(path: Path) -> list[tuple[Any, ...]]:
        conn = sqlite3.connect(path)
        try:
            fills = conn.execute(
                "SELECT fill_ref, status, exchange_fill_id IS NOT NULL, verified_at IS NOT NULL"
                " FROM fills ORDER BY fill_ref"
            ).fetchall()
            ledger = conn.execute(
                "SELECT position_id, status, contracts_centi FROM position_ledger"
                " ORDER BY position_id"
            ).fetchall()
            ev = conn.execute(
                "SELECT fill_ref, expected_edge_cc, realized_pnl_cc FROM ev_ledger"
                " ORDER BY fill_ref"
            ).fetchall()
            return fills + ledger + ev
        finally:
            conn.close()

    assert snapshot(tmp_path / "live.sqlite3") == snapshot(tmp_path / "offline.sqlite3")
    # The backup is the PRE-void state.
    conn = sqlite3.connect(outcome["backup"])
    try:
        assert conn.execute(
            "SELECT status FROM fills WHERE fill_ref='fill:ph'"
        ).fetchone()[0] == "booked"
    finally:
        conn.close()
    # Idempotent: a second apply touches nothing.
    again = tool.apply_void(
        str(tmp_path / "offline.sqlite3"),
        [{"fill_ref": "fill:ph", "_reason": "x"}],
        migrate=False,
        backup_dir=tmp_path / "backups2",
    )
    assert again["touched"] == {"fills": 0, "position_ledger": 0, "ev_ledger": 0}


# --------------------------------------------------------------------------- #
# 9. Review fixes (2026-09-04 adversarial review of item D).                  #
# --------------------------------------------------------------------------- #


async def test_migration_watermark_is_max_id_once(tmp_path: Path) -> None:
    """The verification watermark is MAX(fills.id) at migration (0 on a fresh
    store), stamped once and never moved — the boundary between legacy rows
    (repair-tool territory) and rows the verifying code wrote."""
    fresh = await Store.open(tmp_path / "fresh.sqlite3", FakeClock())
    try:
        wm = await fresh.fills_verification_watermark()
        assert wm is not None and wm[0] == 0
        await fresh.record_fill(
            "fill:n", order_id="o-n", combo_ticker="T", our_side="no", contracts_centi=1,
            price_cc=1, fee_cc=0, expected_edge_cc=1, raw={},
        )
        wm_after = await fresh.fills_verification_watermark()
        assert wm_after is not None and wm_after[0] == 0
    finally:
        await fresh.close()
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    store = await Store.open(path, FakeClock())
    try:
        wm = await store.fills_verification_watermark()
        assert wm is not None and wm[0] == 4 and wm[1]
        await store.record_fill(
            "fill:new", order_id="fresh", combo_ticker="T", our_side="no", contracts_centi=1,
            price_cc=1, fee_cc=0, expected_edge_cc=1, raw={},
        )
        await store.close()
        store = await Store.open(path, FakeClock())  # re-open: unchanged
        assert await store.fills_verification_watermark() == wm
    finally:
        await store.close()


async def test_booked_unverified_fills_scopes_to_post_watermark_keyed_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    store = await Store.open(path, FakeClock())
    try:
        wm = await store.fills_verification_watermark()
        assert wm is not None
        # Legacy rows (ids 1-4, all 'booked') are NEVER re-armable.
        assert await store.booked_unverified_fills(after_id=wm[0]) == []
        for ref, oid in (("fill:k1", "o-k1"), ("fill:k2", "o-k2"), ("fill:nokey", None)):
            assert await store.record_fill(
                ref, order_id=oid, combo_ticker="T", our_side="no", contracts_centi=100,
                price_cc=5000, fee_cc=7, expected_edge_cc=1, raw={},
            )
        rows = await store.booked_unverified_fills(after_id=wm[0])
        assert [r["fill_ref"] for r in rows] == ["fill:k1", "fill:k2"]  # NULL key excluded
        assert rows[0]["order_id"] == "o-k1" and rows[0]["fee_cc"] == 7
        assert rows[0]["contracts_centi"] == 100 and rows[0]["at"]
        # Terminal states leave the set.
        assert await store.mark_fill_verified("fill:k1", exchange_fill_id="f1")
        await store.void_phantom_fill("fill:k2", reason="absent_from_portfolio_fills")
        assert await store.booked_unverified_fills(after_id=wm[0]) == []
    finally:
        await store.close()


async def test_ev_summary_excludes_voided_phantom_rows(tmp_path: Path) -> None:
    store = await Store.open(tmp_path / "ev.sqlite3", FakeClock())
    try:
        for ref, edge in (("fill:real", 500), ("fill:ph", 1330)):
            assert await store.record_fill(
                ref, order_id=f"o-{ref}", combo_ticker="T", our_side="no", contracts_centi=100,
                price_cc=5000, fee_cc=0, expected_edge_cc=edge, raw={},
            )
        before = await store.ev_summary()
        assert before["fills"] == 2 and before["expected_edge_cc"] == 1830
        await store.void_phantom_fill("fill:ph", reason="absent_from_portfolio_fills")
        after = await store.ev_summary()
        # Not "n=2 at 0/0" — the phantom is not graded at all.
        assert after["fills"] == 1 and after["expected_edge_cc"] == 500
    finally:
        await store.close()


async def test_post_insert_tail_failure_still_arms_verification(tmp_path: Path) -> None:
    """Review: if record_realized_pnl/_track_markout raised AFTER the INSERT,
    the caller saw inserted=False and the row stayed 'booked' unverified
    forever (the sweep skips fill_recorded states). The tail is now loud and
    the insert result stands: verification arms and proves the row."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="o1")]})
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("markout tracker exploded")

    rig.lifecycle._track_markout = boom  # type: ignore[method-assign]  # noqa: SLF001
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id, order_id="o1"))
    events = [e["event"] for e in cap]
    assert "fill_ledger_post_insert_failed" in events
    assert "fill_ledger_write_failed" not in events  # never reported as a failed write
    assert "fill_verification_started" in events
    assert rig.metrics.counter("fill_ledger.post_insert_failed") == 1
    assert rig.metrics.counter("fill_verify.started") == 1
    assert await rig.store.count("fills") == 1
    await _tick(rig)
    assert (await _fill_row(rig.store, fill_ref))["status"] == "verified"
    assert len(fills.calls) == 1


async def test_late_verification_is_a_preregistered_alarm(tmp_path: Path) -> None:
    """Review evidence gap: the 0.486 s figure is two EXCHANGE stamps, not the
    REST visibility lag of /portfolio/fills. A real fill that verifies only on
    a LATER read is therefore a loud, counted signal to revisit the budget
    (a wrong void is an undercount) — registered before relight."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": []})  # not visible yet on read 1
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    await rig.lifecycle.on_quote_executed(ws_executed_msg(quote_id, order_id="o1"))
    await _tick(rig)
    assert len(fills.calls) == 1
    assert (await _fill_row(rig.store, fill_ref))["status"] == "booked"
    assert fill_ref in rig.exposure.positions
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="o1")]})  # visible on read 2
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    assert (await _fill_row(rig.store, fill_ref))["status"] == "verified"
    late = [e for e in cap if e["event"] == "fill_verified_late"]
    assert len(late) == 1 and late[0]["attempts"] == 2 and late[0]["order_id"] == "o1"
    assert rig.metrics.counter("fill_verify.verified_late") == 1
    assert rig.metrics.counter("fill_verify.phantom_voided") == 0


async def test_executed_status_without_creator_order_id_is_unmatched_not_adopted(
    tmp_path: Path,
) -> None:
    """Review: in executed_status with no creator_order_id the structural
    fallback could adopt an exact-total same-ticker fill of ANOTHER in-flight
    quote whose row has not landed. The brief required a POSITIVE match:
    unkeyed => fill_recovery_unmatched, nothing booked, nothing polled, the
    confirm-booked position KEPT (fail-safe), terminal."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    quote_id = await _confirmed_quote(rig)
    fill_ref = f"fill:{quote_id}"
    getter.script_status(quote_id, "executed")  # no creator_order_id
    # A structurally-perfect same-ticker print sits on the tape (someone
    # else's, or ours — unknowable without the key).
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="ord-someone")]})
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    un = [e for e in cap if e["event"] == "fill_recovery_unmatched"]
    assert len(un) == 1 and un[0]["quote_id"] == quote_id
    assert rig.metrics.counter("fill_recovery.unmatched") == 1
    assert rig.metrics.counter("fill_recovery.executed_status_verifying") == 0
    assert rig.metrics.counter("fill_recovery.recovered") == 0
    assert fills.calls == []  # no structural read at all
    assert await rig.store.count("fills") == 0
    assert fill_ref in rig.exposure.positions  # KEPT — undercount is the dangerous way
    assert quote_id not in rig.lifecycle._executed_states  # noqa: SLF001 — terminal
    for _ in range(3):
        rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
        await _tick(rig)
    assert getter.calls == [quote_id] and fills.calls == []
    assert fill_ref in rig.exposure.positions


async def test_restart_rearms_unproven_booked_claims(tmp_path: Path) -> None:
    """Review: verification state lived only in memory — a crash inside the
    window left rows 'booked' forever. PROCESS 1 books two WS executions and
    dies before proving either; PROCESS 2 (same store) re-arms exactly those
    rows on the verify cadence: the real one verifies on read 1, the phantom
    is voided after the bounded budget — durable ledgers identical to the
    in-process void; no GET quote polls; terminal."""
    getter1 = FakeQuoteGetter()
    fills1 = FakeFillsGetter()
    rig1 = await _verify_rig(tmp_path, getter=getter1, fills=fills1)
    rig1.lifecycle._fill_fee_cc = lambda *a, **k: 123  # type: ignore[method-assign]  # noqa: SLF001
    q_real = await _confirmed_quote(rig1, rfq_id="rfq_real")
    await _drain()
    await rig1.lifecycle.on_quote_executed(ws_executed_msg(q_real, order_id="o-real"))
    q_ph = await _confirmed_quote(rig1, rfq_id="rfq_ph")
    await _drain()
    await rig1.lifecycle.on_quote_executed(ws_executed_msg(q_ph, order_id=PHANTOM_ORDER_ID))
    await _drain()
    assert await rig1.store.count("fills") == 2
    assert (await _fill_row(rig1.store, f"fill:{q_real}"))["status"] == "booked"
    assert (await _ledger_row(rig1.store, f"fill:{q_ph}")) == ("open", 1000)
    assert fills1.calls == []  # the process dies before any verification read
    await rig1.store.close()

    getter2 = FakeQuoteGetter()
    fills2 = FakeFillsGetter()
    rig2 = await _verify_rig(tmp_path, getter=getter2, fills=fills2)
    fills2.script(COMBO_TICKER, {"fills": [taker_fill(order_id="o-real")]})
    with structlog.testing.capture_logs() as cap:
        await _tick(rig2)
    armed = [e for e in cap if e["event"] == "fill_verify_rearmed"]
    assert len(armed) == 1 and armed[0]["n"] == 2 and armed[0]["watermark_id"] == 0
    assert rig2.metrics.counter("fill_verify.rearmed") == 2
    # Tick 1: both claims read (inside the 3-poll budget); the real one is
    # proven, the phantom's attempt 1 finds nothing.
    assert sorted(str(c["order_id"]) for c in fills2.calls) == sorted(
        ["o-real", PHANTOM_ORDER_ID]
    )
    assert (await _fill_row(rig2.store, f"fill:{q_real}"))["status"] == "verified"
    assert (await _fill_row(rig2.store, f"fill:{q_ph}"))["status"] == "booked"
    assert rig2.metrics.counter("fill_verify.rearm_verified") == 1
    rig2.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await _tick(rig2)
    assert len(fills2.calls) == 3
    rig2.h.clock.advance(VERIFY_DELAY_S + 0.5)
    with structlog.testing.capture_logs() as cap:
        await _tick(rig2)
    assert len(fills2.calls) == 4
    voided = [e for e in cap if e["event"] == "fill_phantom_execution_voided"]
    assert len(voided) == 1 and voided[0]["rearmed"] is True
    assert voided[0]["order_id"] == PHANTOM_ORDER_ID and voided[0]["contracts_centi"] == 1000
    assert voided[0]["touched"] == {"fills": 1, "position_ledger": 1, "ev_ledger": 1}
    # Same-day row => the boot seed carried its fee => reversed in-process.
    assert voided[0]["fee_reversed_cc"] == 123
    assert rig2.lifecycle._realized_pnl_cc == 123  # noqa: SLF001 (the seed would hold -123)
    row = await _fill_row(rig2.store, f"fill:{q_ph}")
    assert row["status"] == "phantom"
    assert row["exchange_fill_id"] == "phantom:absent_from_portfolio_fills"
    assert (await _ledger_row(rig2.store, f"fill:{q_ph}")) == ("phantom", 1000)
    assert (await _ev_row(rig2.store, f"fill:{q_ph}")) == (0, 0)
    assert PHANTOM_ORDER_ID not in await rig2.store.fill_order_ids()
    assert rig2.metrics.counter("fill_verify.rearm_voided") == 1
    assert rig2.metrics.counter("fill_verify.phantom_voided") == 1
    assert getter2.calls == []  # never a GET quote for a re-armed claim
    # Terminal: nothing polls again; a third boot finds nothing to re-arm.
    rig2.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await _tick(rig2)
    assert len(fills2.calls) == 4
    await rig2.store.close()
    rig3 = await _verify_rig(tmp_path, getter=FakeQuoteGetter(), fills=FakeFillsGetter())
    with structlog.testing.capture_logs() as cap:
        await _tick(rig3)
    assert [e for e in cap if e["event"] == "fill_verify_rearmed"][0]["n"] == 0
    await rig3.store.close()


async def test_restart_rearm_never_touches_legacy_rows(tmp_path: Path) -> None:
    """Rows at/below the watermark (the 2026-07-18 truncated-id rows among
    them) are never re-armed: an exact order_id read would wrongly void a
    real fill. Their proof is the settlement-corroborated repair tool."""
    _legacy_db(tmp_path / "recovery.sqlite3")
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)  # migrates: watermark 4
    with structlog.testing.capture_logs() as cap:
        await _tick(rig)
    armed = [e for e in cap if e["event"] == "fill_verify_rearmed"]
    assert len(armed) == 1 and armed[0]["n"] == 0 and armed[0]["watermark_id"] == 4
    assert fills.calls == []
    assert await rig.store.fill_status("fill:a") == "booked"
    assert rig.metrics.counter("fill_verify.rearm_polls") == 0


async def test_rearm_fee_reversal_only_for_current_realized_day(tmp_path: Path) -> None:
    getter = FakeQuoteGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=FakeFillsGetter())
    today = rig.h.clock.now().isoformat()
    assert rig.lifecycle._booked_in_current_realized_day(today)  # noqa: SLF001
    assert not rig.lifecycle._booked_in_current_realized_day(  # noqa: SLF001
        "2020-01-01T12:00:00+00:00"
    )
    assert not rig.lifecycle._booked_in_current_realized_day("garbage")  # noqa: SLF001


async def test_ledger_quantity_legacy_rows_counted_not_alarmed(tmp_path: Path) -> None:
    """Review: 434 legacy open rows would fire the alarm every 5 min and bury
    a NEW phantom. Rows opened before the verification migration are counted
    as legacy in the same log line; only post-fix tickers are alarmed/metered."""
    store = await Store.open(tmp_path / "scope.sqlite3", FakeClock())
    try:
        for pid, ticker in (("fill:legacy", "T-OLD"), ("fill:new", "T-NEW"), ("fill:mix", "T-MIX")):
            await store.record_position_open(_ledger_pos(pid, ticker, Side.NO, 300), subaccount="0")
        await store.record_position_open(
            _ledger_pos("fill:mix-old", "T-MIX", Side.NO, 200), subaccount="0"
        )
        await store._db.execute(  # noqa: SLF001 — backdate to before the migration stamp
            "UPDATE position_ledger SET opened_at='2025-12-31T00:00:00+00:00'"
            " WHERE position_id IN ('fill:legacy', 'fill:mix-old')"
        )
        await store._db.commit()  # noqa: SLF001
        metrics = Metrics()
        with structlog.testing.capture_logs() as cap:
            out = await ledger_quantity_reconcile_once(store, {}, metrics)
        # T-NEW (post-fix) and T-MIX (a post-fix row exists => alarmed in full,
        # legacy row included in its sum); T-OLD is legacy only.
        assert {m["ticker"] for m in out} == {"T-NEW", "T-MIX"}
        mix = next(m for m in out if m["ticker"] == "T-MIX")
        assert mix["ledger_contracts_centi"] == 500 and mix["ledger_rows_post_fix"] == 1
        ev = [e for e in cap if e["event"] == "ledger_quantity_mismatch"]
        assert len(ev) == 1
        assert ev[0]["n"] == 2 and ev[0]["legacy_n"] == 1
        assert ev[0]["legacy_by_kind"] == {"ledger_only": 1}
        assert ev[0]["legacy_tickers"] == ["T-OLD"]
        assert ev[0]["post_fix_since"]
        assert metrics.counter("ledger_quantity.mismatch") == 2
        assert metrics.counter("ledger_quantity.legacy") == 1
        # Only legacy rows => clean event, legacy still counted.
        await store._db.execute(  # noqa: SLF001
            "UPDATE position_ledger SET opened_at='2025-12-31T00:00:00+00:00'"
        )
        await store._db.commit()  # noqa: SLF001
        m2 = Metrics()
        with structlog.testing.capture_logs() as cap2:
            assert await ledger_quantity_reconcile_once(store, {}, m2) == []
        assert cap2[0]["event"] == "ledger_quantity_mismatch_clean" and cap2[0]["legacy_n"] == 3
        assert m2.counter("ledger_quantity.mismatch") == 0
        assert m2.counter("ledger_quantity.legacy") == 3
    finally:
        await store.close()
