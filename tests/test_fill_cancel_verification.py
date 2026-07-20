"""CANCEL-REPORT VERIFY-BEFORE-DISCARD + writer-miss hardening + reconcile net
(2026-07-18 — two live incidents the same day).

Incident A (quote 903935fc, 16:24Z) and incident B (quote 7d79f32b, 18:30Z):
both CONFIRMED quotes came back ``cancelled`` (``cancellation_reason:
"execution failed"``) from REST GET quote while the exchange EXECUTED the fill
anyway as a taker-style REGULAR order — visible only on ``/portfolio/fills``
(nonzero ``fee_cost``, ``is_taker: true``), with NO ``quote_executed`` WS
message. The old ``_recover_cancelled_fill`` trusted the cancel report,
removed the REAL position from the risk book (undercount — the dangerous
direction) and never wrote a fills row. These tests cover the hardening:

- cancel report ⇒ position KEPT while /portfolio/fills is polled (bounded
  attempts, injectable clock); a matching execution keeps the position and
  writes the row via the NORMAL on_quote_executed writer, with the
  exchange-reported taker fee booked (``fill_recovery_late_execution``);
- truly cancelled (fills tape readable, no match) ⇒ phantom removed exactly
  as before, after the verification evidence;
- ADOPTION GUARDS (2026-07-18 adversarial review — structural match alone can
  hit a HISTORICAL same-ticker/side/exact-count fill and double-count): the
  /portfolio/fills query is time-scoped by ``min_ts`` (confirm wall-time minus
  slack); a fill whose order_id already exists in the local ledger is NEVER
  adopted; an in-memory claim set stops two concurrently-verifying quotes
  adopting ONE exchange fill — each guard pinned by its own test;
- every verification read errored ⇒ position KEPT (fail-safe: risk we cannot
  disprove stays counted) and the whole ROUND retried on the same cadence,
  bounded (3 rounds), THEN the loud ``fill_recovery_verify_unresolved`` ERROR
  — a transient 429 storm must not pin capital until restart;
- WS ``quote_executed`` arriving DURING verification ⇒ a single row (loop-top
  ``fill_recorded`` ordering);
- committed-but-writer-miss (requirement 2): a failed fills-ledger write is a
  loud ``fill_ledger_write_failed`` ERROR and the recovery sweep retries it
  through the same writer;
- verification disabled / no fills getter ⇒ the prior immediate discard;
- periodic position-reconcile net (requirement 3): an exchange open position
  the in-memory book does not model is alarmed (``position_reconcile_
  unmodeled``) and NEVER auto-inserted;
- config: RiskConfig defaults/validation + build_lifecycle_config
  pass-through for the new knobs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import RiskConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.quote_app import (
    build_lifecycle_config,
    position_reconcile_unmodeled_once,
)
from combomaker.rfq.lifecycle import LifecycleConfig
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
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

VERIFY_DELAY_S = 90.0
COMBO_TICKER = "KXMVE-C1"  # tests.test_pricing_engine.combo default

# Incident-A-shaped /portfolio/fills row (index-scan §portfolio fills): the
# taker-style execution behind the "cancelled" quote status — outcome_side +
# count_fp match our pending fill; is_taker true; nonzero fee_cost; the price
# does NOT match our bid (incident A: 0.7660 vs our 0.7670) and must not be
# required to.
INCIDENT_FEE_COST = "0.510840"  # dollars → 5108.4 cc → booked 5109 (round UP)
INCIDENT_FEE_CC = 5109


def taker_fill(
    *,
    ticker: str = COMBO_TICKER,
    outcome_side: str = "yes",
    count_fp: str = "10.00",
    order_id: str = "ord-late-1",
    fee_cost: str = INCIDENT_FEE_COST,
) -> JsonDict:
    return {
        "fill_id": "f-1",
        "order_id": order_id,
        "ticker": ticker,
        "outcome_side": outcome_side,
        "book_side": "bid",
        "count_fp": count_fp,
        "yes_price_dollars": "0.7660",
        "is_taker": True,
        "fee_cost": fee_cost,
        "created_time": "2026-07-18T16:24:28Z",
    }


class FakeFillsGetter:
    """Scripted GET /portfolio/fills: one payload (or Exception) per ticker.
    Records every call (ticker + params) so tests assert the poll cadence and
    the subaccount pin."""

    def __init__(self) -> None:
        self.responses: dict[str, JsonDict | Exception] = {}
        self.calls: list[dict[str, str | int]] = []

    def script(self, ticker: str, response: JsonDict | Exception) -> None:
        self.responses[ticker] = response

    async def get_fills(self, **params: str | int) -> JsonDict:
        self.calls.append(dict(params))
        response = self.responses.get(str(params.get("ticker", "")))
        if response is None:
            raise AssertionError(f"unscripted get_fills({params!r})")
        if isinstance(response, Exception):
            raise response
        return response


async def _verify_rig(
    tmp_path: Path,
    *,
    getter: FakeQuoteGetter,
    fills: FakeFillsGetter | None,
    attempts: int = 3,
) -> RecoveryRig:
    return await _make_rig(
        tmp_path,
        getter=getter,
        fills_getter=fills,
        config=LifecycleConfig(
            book_risk_mc_samples=200,
            fill_record_recovery_after_s=RECOVERY_AFTER_S,
            fill_cancel_verify_attempts=attempts,
            fill_cancel_verify_delay_s=VERIFY_DELAY_S,
        ),
    )


async def _cancel_reported(rig: RecoveryRig, getter: FakeQuoteGetter) -> str:
    """Quote → accept → confirm → REST reports CANCELLED (incident shape).
    After this the position is booked and verification has started."""
    quote_id = await _confirmed_quote(rig)
    getter.script_status(
        quote_id, "cancelled", cancellation_reason="execution failed"
    )
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()  # GET quote → cancelled → verifying
    assert rig.metrics.counter("fill_recovery.cancel_verify_started") == 1
    return quote_id


# --------------------------------------------------------------------------- #
# Incident A: cancel report, then the execution appears LATE on /portfolio/    #
# fills ⇒ position kept the whole time; row via the NORMAL writer.             #
# --------------------------------------------------------------------------- #


async def test_cancel_report_then_late_execution_keeps_position_and_records(
    tmp_path: Path,
) -> None:
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": []})  # nothing on the tape yet
    quote_id = await _cancel_reported(rig, getter)

    # The cancel report did NOT remove the position (the old dangerous step).
    assert f"fill:{quote_id}" in rig.exposure.positions

    # Verify attempt 1 (due immediately on the next tick): tape empty — the
    # position STAYS while attempts remain.
    await rig.lifecycle.maintenance_tick()
    assert len(fills.calls) == 1
    assert f"fill:{quote_id}" in rig.exposure.positions
    assert await rig.store.count("fills") == 0

    # Not due yet: no extra poll before the configured delay elapses.
    await rig.lifecycle.maintenance_tick()
    assert len(fills.calls) == 1

    # The exchange executes LATE (incident A: minutes after the cancel
    # report), as a taker-style regular order at a price off our bid.
    fills.script(COMBO_TICKER, {"fills": [taker_fill()]})
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()  # attempt 2 → match → replay

    assert rig.metrics.counter("fill_recovery.late_execution") == 1
    assert f"fill:{quote_id}" in rig.exposure.positions  # kept, never removed
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill.count") == 1
    (row,) = await _fill_rows(rig.store)
    assert row[0] == f"fill:{quote_id}"
    assert row[1] == "ord-late-1"  # exchange order id
    assert row[2] == COMBO_TICKER
    assert row[6] == INCIDENT_FEE_CC  # exchange-reported taker fee, not model $0
    assert "recovered_via_fills_poll" in str(row[8])  # provenance in raw_json
    # The real taker fee entered the realized ledger the daily-loss cap reads.
    assert rig.lifecycle._realized_pnl_cc == -INCIDENT_FEE_CC  # noqa: SLF001

    # Terminal: nothing further polled or written.
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert len(fills.calls) == 2
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill_recovery.cancelled") == 0  # never discarded


async def test_cancel_report_with_fill_already_on_tape_recovers_first_poll(
    tmp_path: Path,
) -> None:
    """Incident B shape: the execution was ALREADY on /portfolio/fills when
    the cancel report arrived — the very first verification poll finds it."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="ord-b-1")]})
    quote_id = await _cancel_reported(rig, getter)

    await rig.lifecycle.maintenance_tick()  # attempt 1 → immediate match
    assert rig.metrics.counter("fill_recovery.late_execution") == 1
    assert f"fill:{quote_id}" in rig.exposure.positions
    assert await rig.store.count("fills") == 1
    (row,) = await _fill_rows(rig.store)
    assert row[1] == "ord-b-1"
    # Subaccount pin rode the query when configured (None here ⇒ no pin key).
    assert fills.calls[0]["ticker"] == COMBO_TICKER


async def test_wrong_side_or_count_never_matches(tmp_path: Path) -> None:
    """A fill on the same ticker but the WRONG side or count is NOT our
    execution — verification must not adopt it (fail-closed matcher)."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills, attempts=1)
    fills.script(
        COMBO_TICKER,
        {
            "fills": [
                taker_fill(outcome_side="no"),  # wrong side
                taker_fill(count_fp="99.00"),  # wrong count
                taker_fill(ticker="KXMVE-OTHER"),  # wrong ticker
            ]
        },
    )
    quote_id = await _cancel_reported(rig, getter)
    await rig.lifecycle.maintenance_tick()  # attempt 1 (final) → no match
    assert await rig.store.count("fills") == 0
    assert f"fill:{quote_id}" not in rig.exposure.positions  # verified absent
    assert rig.metrics.counter("fill_recovery.cancelled") == 1


# --------------------------------------------------------------------------- #
# ADOPTION GUARDS (2026-07-18 review) — each pinned separately.                #
# --------------------------------------------------------------------------- #


async def test_query_time_scoped_by_min_ts(tmp_path: Path) -> None:
    """GUARD 1 (min_ts): the /portfolio/fills query carries min_ts = the
    quote's confirm WALL-time minus the 60s skew slack, so the server-side
    match window is the verification window — never the ticker's historical
    tape."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": []})
    quote_id = await _cancel_reported(rig, getter)
    state = rig.lifecycle._executed_states[quote_id]  # noqa: SLF001
    assert state.fill_confirmed_wall_ts is not None
    await rig.lifecycle.maintenance_tick()  # verification attempt 1
    assert fills.calls[0]["ticker"] == COMBO_TICKER
    assert fills.calls[0]["min_ts"] == state.fill_confirmed_wall_ts - 60
    assert fills.calls[0]["limit"] == 100


async def test_historical_fill_in_ledger_never_adopted(tmp_path: Path) -> None:
    """GUARD 2 (order_id vs local ledger): the live tape holds same-ticker/
    side/EXACT-count fills hours apart (rows 59/61) — a structurally-matching
    exchange fill whose order_id is already in the local fills ledger belongs
    to an EARLIER quote and must NOT be adopted for this one (that would keep
    a phantom AND double-book the fee). Pinned independently of min_ts: the
    fake returns the historical row regardless of the query window."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills, attempts=1)

    # Quote 1 fills NORMALLY via WS with order ord-hist (the earlier fill).
    q1 = await _confirmed_quote(rig, rfq_id="rfq_1")
    await rig.lifecycle.on_quote_executed({"quote_id": q1, "order_id": "ord-hist"})
    assert await rig.store.count("fills") == 1

    # Quote 2, SAME combo + SAME size, genuinely cancels — but the fills tape
    # (as returned) still shows Q1's historical fill, a perfect structural match.
    q2 = await _confirmed_quote(rig, rfq_id="rfq_2")
    getter.script_status(q2, "cancelled", cancellation_reason="execution failed")
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="ord-hist")]})
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()  # cancel discovered → verifying
    await rig.lifecycle.maintenance_tick()  # attempt 1 (final): guard refuses

    assert (
        rig.metrics.counter("fill_recovery.verify_match_rejected.already_in_ledger")
        == 1
    )
    assert rig.metrics.counter("fill_recovery.late_execution") == 0
    assert await rig.store.count("fills") == 1  # Q1's row only — NO second row
    assert f"fill:{q2}" not in rig.exposure.positions  # phantom discarded
    assert f"fill:{q1}" in rig.exposure.positions  # the real fill untouched
    assert rig.metrics.counter("fill.count") == 1  # fee/metrics never re-booked


async def test_two_concurrent_verifications_one_exchange_fill(
    tmp_path: Path,
) -> None:
    """GUARD 3 (claim set): two quotes (same combo, same size) both verifying
    against ONE exchange fill — exactly one adopts it. Pinned independently of
    the ledger guard: the first adopter's ledger write FAILS (flaky store), so
    when the second quote verifies there is NO fills row yet — only the
    in-memory claim can (and must) block the double adoption."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills, attempts=1)
    q1 = await _confirmed_quote(rig, rfq_id="rfq_1")
    q2 = await _confirmed_quote(rig, rfq_id="rfq_2")
    for quote_id in (q1, q2):
        getter.script_status(
            quote_id, "cancelled", cancellation_reason="execution failed"
        )
    fills.script(COMBO_TICKER, {"fills": [taker_fill(order_id="ord-shared")]})

    original = rig.store.record_fill
    boom = {"armed": True}

    async def flaky(*args: Any, **kwargs: Any) -> bool:
        if boom["armed"]:
            boom["armed"] = False
            raise RuntimeError("database table is locked")
        return await original(*args, **kwargs)

    rig.store.record_fill = flaky  # type: ignore[method-assign]

    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()  # both cancel reports discovered

    # One tick, both verifications: q1 adopts (claims ord-shared) but its
    # write fails — NO ledger row exists when q2 verifies; only the CLAIM
    # blocks q2 from adopting the same exchange fill.
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("fill_recovery.late_execution") == 1
    assert (
        rig.metrics.counter("fill_recovery.verify_match_rejected.already_claimed")
        == 1
    )
    assert rig.metrics.counter("fill_ledger.write_failed") == 1
    assert await rig.store.count("fills") == 0
    assert f"fill:{q1}" in rig.exposure.positions  # the adopter: kept
    assert f"fill:{q2}" not in rig.exposure.positions  # the phantom: discarded

    # Next tick: q1's replay retries and lands EXACTLY ONE row.
    await rig.lifecycle.maintenance_tick()
    assert await rig.store.count("fills") == 1
    (row,) = await _fill_rows(rig.store)
    assert row[0] == f"fill:{q1}"
    assert row[1] == "ord-shared"
    assert rig.metrics.counter("fill.count") == 1


async def test_ws_execution_during_verification_single_row(tmp_path: Path) -> None:
    """The WS quote_executed arriving DURING verification wins cleanly: the
    row is written once by the WS path, the sweep's loop-top ``fill_recorded``
    check stops all further verification polling, and the position stays."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": [taker_fill()]})
    quote_id = await _cancel_reported(rig, getter)

    # WS message lands mid-verification (before any fills poll ran).
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    assert await rig.store.count("fills") == 1

    for _ in range(3):
        rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
        await rig.lifecycle.maintenance_tick()
    assert fills.calls == []  # verification never polled after the WS row
    assert await rig.store.count("fills") == 1  # single row
    assert rig.metrics.counter("fill.count") == 1
    assert f"fill:{quote_id}" in rig.exposure.positions


# --------------------------------------------------------------------------- #
# Truly cancelled ⇒ phantom removed only AFTER the verification evidence.      #
# --------------------------------------------------------------------------- #


async def test_truly_cancelled_removes_phantom_after_verification(
    tmp_path: Path,
) -> None:
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": []})
    quote_id = await _cancel_reported(rig, getter)

    # Attempts 1 and 2: position stays while the budget remains.
    await rig.lifecycle.maintenance_tick()
    assert f"fill:{quote_id}" in rig.exposure.positions
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert f"fill:{quote_id}" in rig.exposure.positions
    assert len(fills.calls) == 2

    # Attempt 3 (final): genuinely absent ⇒ the phantom is removed exactly as
    # the old path did, no fills row ever written.
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert len(fills.calls) == 3
    assert f"fill:{quote_id}" not in rig.exposure.positions
    assert await rig.store.count("fills") == 0
    assert rig.metrics.counter("fill_recovery.cancelled") == 1
    assert rig.lifecycle._executed_states == {}  # noqa: SLF001 — un-parked

    # Terminal: nothing polls again.
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert len(fills.calls) == 3


async def test_all_reads_errored_retries_rounds_then_keeps_position(
    tmp_path: Path,
) -> None:
    """Absence was never PROVEN (every /portfolio/fills read failed): the
    position must stay counted against the caps — undercounting is the
    dangerous direction. A fully-errored ROUND is retried on the same cadence
    (2026-07-18 review: a transient 429 storm must not pin capital until
    restart) — 3 rounds x 3 attempts = 9 polls — and only THEN the loud ERROR
    give-up (position still kept)."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, RuntimeError("rest down"))
    quote_id = await _cancel_reported(rig, getter)

    for _ in range(9):  # 3 rounds x 3 attempts, all errors
        await rig.lifecycle.maintenance_tick()
        rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()  # nothing left — state popped

    assert rig.metrics.counter("fill_recovery.verify_errors") == 9
    assert rig.metrics.counter("fill_recovery.verify_round_failed") == 2
    assert rig.metrics.counter("fill_recovery.verify_unresolved") == 1
    assert rig.metrics.counter("fill_recovery.cancelled") == 0  # NOT discarded
    assert f"fill:{quote_id}" in rig.exposure.positions  # fail-safe: kept
    assert await rig.store.count("fills") == 0
    assert rig.lifecycle._executed_states == {}  # noqa: SLF001 — sweep done
    assert len(fills.calls) == 9  # bounded — no further polling


async def test_transient_error_round_then_recovery_next_round(
    tmp_path: Path,
) -> None:
    """A 429-storm round proves nothing — the RETRY round must still be able
    to find the real execution and adopt it (round retry is not just a louder
    give-up)."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills, attempts=1)
    fills.script(COMBO_TICKER, RuntimeError("429 storm"))
    quote_id = await _cancel_reported(rig, getter)

    await rig.lifecycle.maintenance_tick()  # round 1, attempt 1: error → retry round
    assert rig.metrics.counter("fill_recovery.verify_round_failed") == 1
    assert f"fill:{quote_id}" in rig.exposure.positions

    fills.script(COMBO_TICKER, {"fills": [taker_fill()]})  # exchange back up
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()  # round 2, attempt 1: match
    assert rig.metrics.counter("fill_recovery.late_execution") == 1
    assert await rig.store.count("fills") == 1
    assert f"fill:{quote_id}" in rig.exposure.positions


# --------------------------------------------------------------------------- #
# Requirement 2: committed-but-writer-miss is loud and retried.                #
# --------------------------------------------------------------------------- #


async def test_committed_writer_miss_is_loud_and_retried(tmp_path: Path) -> None:
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

    # WS execution arrives; the ledger write FAILS — loud ERROR, no row, the
    # in-memory book still holds the fill (never silently divergent again).
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    assert rig.metrics.counter("fill_ledger.write_failed") == 1
    assert await rig.store.count("fills") == 0
    assert rig.metrics.counter("fill.count") == 0
    assert f"fill:{quote_id}" in rig.exposure.positions

    # The recovery sweep retries through the SAME writer path and lands it.
    getter.script_status(quote_id, "executed", creator_order_id="o1")
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    assert await rig.store.count("fills") == 1
    assert rig.metrics.counter("fill.count") == 1
    assert rig.metrics.counter("fill_ledger.write_failed") == 1  # once, not two


async def test_verified_fill_write_failure_retries_next_tick(tmp_path: Path) -> None:
    """Verification PROVED the fill real but the first replay write failed:
    the position must never be discarded and the replay retries until the row
    lands (bounded by the sweep's attempt budget)."""
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills)
    fills.script(COMBO_TICKER, {"fills": [taker_fill()]})

    original = rig.store.record_fill
    boom = {"armed": True}

    async def flaky(*args: Any, **kwargs: Any) -> bool:
        if boom["armed"]:
            boom["armed"] = False
            raise RuntimeError("database table is locked")
        return await original(*args, **kwargs)

    rig.store.record_fill = flaky  # type: ignore[method-assign]

    quote_id = await _cancel_reported(rig, getter)
    await rig.lifecycle.maintenance_tick()  # attempt 1 → match → write FAILS
    assert rig.metrics.counter("fill_recovery.late_execution") == 1
    assert rig.metrics.counter("fill_ledger.write_failed") == 1
    assert await rig.store.count("fills") == 0
    assert f"fill:{quote_id}" in rig.exposure.positions  # still kept

    await rig.lifecycle.maintenance_tick()  # replay retry (no new REST poll)
    assert len(fills.calls) == 1
    assert await rig.store.count("fills") == 1
    assert f"fill:{quote_id}" in rig.exposure.positions
    assert rig.metrics.counter("fill_recovery.cancelled") == 0


# --------------------------------------------------------------------------- #
# Disabled / no getter ⇒ the prior immediate discard.                          #
# --------------------------------------------------------------------------- #


async def test_verification_disabled_falls_back_to_immediate_discard(
    tmp_path: Path,
) -> None:
    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills, attempts=0)
    quote_id = await _confirmed_quote(rig)
    getter.script_status(
        quote_id, "cancelled", cancellation_reason="execution failed"
    )
    rig.h.clock.advance(RECOVERY_AFTER_S + 0.5)
    await rig.lifecycle.maintenance_tick()
    # attempts=0 ⇒ verification explicitly disabled: prior behaviour.
    assert f"fill:{quote_id}" not in rig.exposure.positions
    assert rig.metrics.counter("fill_recovery.cancelled") == 1
    assert fills.calls == []  # never polled


# --------------------------------------------------------------------------- #
# Requirement 3: periodic position-reconcile net (alarm-only).                 #
# --------------------------------------------------------------------------- #


class FakePositionsRest:
    def __init__(self, payload: JsonDict) -> None:
        self.payload = payload
        self.params: dict[str, str | int] | None = None

    async def get_positions(self, **params: str | int) -> JsonDict:
        self.params = dict(params)
        return self.payload


def _position(ticker: str) -> OpenPosition:
    return OpenPosition(
        position_id=f"fill:{ticker}",
        combo_ticker=ticker,
        collection=None,
        our_side=Side.YES,
        contracts=CentiContracts(1000),
        entry_price_cc=CentiCents(4600),
        legs=(LegRef(market_ticker="M1", event_ticker="E1", side="yes"),),
    )


async def test_position_reconcile_flags_unknown_exchange_position(
    tmp_path: Path,
) -> None:
    store = await Store.open(tmp_path / "reconcile.sqlite3", FakeClock())
    try:
        exposure = ExposureBook(TEST_CONVENTIONS)
        exposure.add_position(_position("KXMVE-KNOWN"))
        # A local fills row for one unmodeled ticker (the incident class: our
        # own fill fell out of the book) — annotated, still alarmed.
        await store.record_fill(
            "fill:q-fellout",
            order_id=None,
            combo_ticker="KXMVE-FELLOUT",
            our_side="no",
            contracts_centi=2158,
            price_cc=5540,
            fee_cc=None,
            expected_edge_cc=None,
            raw={},
        )
        rest = FakePositionsRest(
            {
                "market_positions": [
                    {"ticker": "KXMVE-KNOWN", "position_fp": "10.00"},
                    {"ticker": "KXMVE-FELLOUT", "position_fp": "-21.58"},
                    {"ticker": "KXMVE-EXTERNAL", "position_fp": "-5.00"},
                    {"ticker": "KXMVE-FLAT", "position_fp": "0.00"},  # settled
                ]
            }
        )
        metrics = Metrics()
        unmodeled = await position_reconcile_unmodeled_once(
            rest, exposure, store, metrics, subaccount=0
        )
        assert unmodeled == ["KXMVE-EXTERNAL", "KXMVE-FELLOUT"]
        assert rest.params is not None
        assert rest.params["subaccount"] == 0  # query-layer pin (P0-5)
        assert metrics.counter("position_reconcile.unmodeled") == 1
        # Neither row ADOPTS here: FELLOUT has a local fills row (the recovery
        # sweep owns full re-modeling) and EXTERNAL carries NO readable
        # market_exposure figure — an at-risk amount is never guessed, so it
        # stays alarm-only (adoption with a real figure: TestReserveAdoption).
        assert {p.combo_ticker for p in exposure.positions.values()} == {
            "KXMVE-KNOWN"
        }

        # Fully modeled book ⇒ no alarm.
        exposure.add_position(_position("KXMVE-FELLOUT"))
        exposure.add_position(_position("KXMVE-EXTERNAL"))
        metrics2 = Metrics()
        assert (
            await position_reconcile_unmodeled_once(
                rest, exposure, store, metrics2, subaccount=0
            )
            == []
        )
        assert metrics2.counter("position_reconcile.unmodeled") == 0
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# Reserve adoption (2026-07-21): a no-local-context exchange position adopts   #
# as a conservatively-reserved holding from exchange figures only.             #
# --------------------------------------------------------------------------- #


class TestReserveAdoption:
    async def test_no_context_position_adopts_as_reserve(
        self, tmp_path: Path
    ) -> None:
        # Past-run history: -3.33 NO contracts, exchange says $2.00 at risk.
        # Adopted exactly: side/count from the signed position, entry rounded
        # UP so the booked max loss is never below the exchange's figure.
        store = await Store.open(tmp_path / "adopt.sqlite3", FakeClock())
        try:
            exposure = ExposureBook(TEST_CONVENTIONS)
            rest = FakePositionsRest(
                {
                    "market_positions": [
                        {
                            "ticker": "KXMVE-PAST",
                            "position_fp": "-3.33",
                            "market_exposure_dollars": "2.00",
                        }
                    ]
                }
            )
            unmodeled = await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            assert unmodeled == ["KXMVE-PAST"]
            reserve = exposure.positions["reserve:KXMVE-PAST"]
            assert reserve.our_side is Side.NO
            assert int(reserve.contracts) == 333
            assert reserve.risk_modeled is False
            # Fail-safe LARGER: ceil(20_000cc × 100 / 333) = 6_007cc/ct ⇒
            # max_loss 20_003cc ≥ the exchange's 20_000cc, never below.
            assert reserve.max_loss_cc >= 20_000
            assert reserve.max_loss_cc == 333 * 6_007 // 100
            # Identity self-leg: its own singleton cluster, never a guessed
            # leg — and side ALWAYS "yes" (the combo settles YES iff its own
            # market does; direction lives solely in our_side — writing the
            # position side here double-complemented NO reserves and inverted
            # the receivable shield; 2026-07-21 review CRITICAL finding 2).
            assert reserve.legs == (
                LegRef(market_ticker="KXMVE-PAST", event_ticker="KXMVE-PAST", side="yes"),
            )

            # Idempotent: the next pass sees the ticker modeled — no re-adopt,
            # no duplicate, no alarm.
            metrics2 = Metrics()
            assert (
                await position_reconcile_unmodeled_once(
                    rest, exposure, store, metrics2, subaccount=0
                )
                == []
            )
            assert metrics2.counter("position_reconcile.unmodeled") == 0
        finally:
            await store.close()

    async def test_int_cents_exposure_fallback(self, tmp_path: Path) -> None:
        # Older payload shape: market_exposure int CENTS (no dollars string).
        store = await Store.open(tmp_path / "adopt2.sqlite3", FakeClock())
        try:
            exposure = ExposureBook(TEST_CONVENTIONS)
            rest = FakePositionsRest(
                {
                    "market_positions": [
                        {
                            "ticker": "KXMVE-CENTS",
                            "position_fp": "-5.00",
                            "market_exposure": 411,
                        }
                    ]
                }
            )
            await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            reserve = exposure.positions["reserve:KXMVE-CENTS"]
            assert reserve.max_loss_cc >= 41_100
        finally:
            await store.close()

    async def test_flat_reserve_releases(self, tmp_path: Path) -> None:
        # The exchange reporting the reserved market flat (settled / manually
        # exited on the app) releases the reserve — held risk never overcounts
        # forever. A still-open reserve is untouched.
        store = await Store.open(tmp_path / "adopt3.sqlite3", FakeClock())
        try:
            exposure = ExposureBook(TEST_CONVENTIONS)
            rest = FakePositionsRest(
                {
                    "market_positions": [
                        {
                            "ticker": "KXMVE-PAST",
                            "position_fp": "-5.00",
                            "market_exposure_dollars": "4.11",
                        }
                    ]
                }
            )
            await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            assert "reserve:KXMVE-PAST" in exposure.positions
            rest.payload = {
                "market_positions": [
                    {"ticker": "KXMVE-PAST", "position_fp": "0.00"}
                ]
            }
            await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            assert "reserve:KXMVE-PAST" not in exposure.positions
        finally:
            await store.close()

    async def test_pagination_reaches_page_two(self, tmp_path: Path) -> None:
        # Review F3: a single unpaginated GET truncates past one page and the
        # truncated tail must still adopt (never read as absent/flat).
        store = await Store.open(tmp_path / "pages.sqlite3", FakeClock())
        try:
            exposure = ExposureBook(TEST_CONVENTIONS)

            class _PagedRest:
                def __init__(self) -> None:
                    self.calls: list[dict[str, str | int]] = []

                async def get_positions(self, **params: str | int) -> JsonDict:
                    self.calls.append(dict(params))
                    if not params.get("cursor"):
                        return {
                            "market_positions": [
                                {
                                    "ticker": "KXMVE-P1",
                                    "position_fp": "-1.00",
                                    "market_exposure_dollars": "1.00",
                                }
                            ],
                            "cursor": "page2",
                        }
                    return {
                        "market_positions": [
                            {
                                "ticker": "KXMVE-P2",
                                "position_fp": "-2.00",
                                "market_exposure_dollars": "2.00",
                            }
                        ],
                        "cursor": "",
                    }

            rest = _PagedRest()
            await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            assert "reserve:KXMVE-P1" in exposure.positions
            assert "reserve:KXMVE-P2" in exposure.positions  # page-2 adopted
        finally:
            await store.close()

    async def test_absence_without_flat_confirmation_holds_reserve(
        self, tmp_path: Path
    ) -> None:
        # Review F3: absence from the open listing (lagging/thin payload) must
        # NEVER release reserved risk — only a targeted read parsing to an
        # explicit zero row does.
        store = await Store.open(tmp_path / "hold.sqlite3", FakeClock())
        try:
            exposure = ExposureBook(TEST_CONVENTIONS)
            rest = FakePositionsRest(
                {
                    "market_positions": [
                        {
                            "ticker": "KXMVE-PAST",
                            "position_fp": "-5.00",
                            "market_exposure_dollars": "4.11",
                        }
                    ]
                }
            )
            await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            assert "reserve:KXMVE-PAST" in exposure.positions
            # The listing (and the targeted read) now returns NOTHING for the
            # ticker — no zero row, no proof of flat: the reserve HOLDS.
            rest.payload = {"market_positions": []}
            await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            assert "reserve:KXMVE-PAST" in exposure.positions
        finally:
            await store.close()

    async def test_quantity_divergence_alarms(self, tmp_path: Path) -> None:
        # Review F5: presence alone is not reconciliation — a known ticker
        # whose exchange count disagrees with the book must alarm (the
        # $31-ARG undercounting class).
        store = await Store.open(tmp_path / "qty.sqlite3", FakeClock())
        try:
            exposure = ExposureBook(TEST_CONVENTIONS)
            exposure.add_position(_position("KXMVE-KNOWN"))  # book: 10.00 YES
            rest = FakePositionsRest(
                {
                    "market_positions": [
                        {"ticker": "KXMVE-KNOWN", "position_fp": "12.00"}
                    ]
                }
            )
            metrics = Metrics()
            await position_reconcile_unmodeled_once(
                rest, exposure, store, metrics, subaccount=0
            )
            assert metrics.counter("position_reconcile.quantity_divergence") == 1
        finally:
            await store.close()

    async def test_reserve_counts_in_deterministic_caps(
        self, tmp_path: Path
    ) -> None:
        # The point of adoption: the reserve's premium is REAL in the exposure
        # snapshot (P0-4 doctrine) — whole-account risk never vanishes.
        store = await Store.open(tmp_path / "adopt4.sqlite3", FakeClock())
        try:
            exposure = ExposureBook(TEST_CONVENTIONS)
            rest = FakePositionsRest(
                {
                    "market_positions": [
                        {
                            "ticker": "KXMVE-PAST",
                            "position_fp": "-5.00",
                            "market_exposure_dollars": "4.11",
                        }
                    ]
                }
            )
            await position_reconcile_unmodeled_once(
                rest, exposure, store, Metrics(), subaccount=0
            )
            snapshot = exposure.snapshot(lambda _t: None, mass_acceptance=False)
            assert snapshot.gross_notional_cc >= 41_100  # Σ max_loss (premium)
            # Its own singleton game cluster carries the loss too.
            assert max(
                snapshot.worst_case_loss_by_game_cc.values(), default=0
            ) >= 41_100
        finally:
            await store.close()


# --------------------------------------------------------------------------- #
# Config: defaults, validation, pass-through.                                  #
# --------------------------------------------------------------------------- #


def test_verify_config_defaults_and_passthrough() -> None:
    rc = RiskConfig()
    assert rc.fill_cancel_verify_attempts == 3
    assert rc.fill_cancel_verify_delay_s == 90.0
    assert rc.position_reconcile_interval_s == 300.0
    assert LifecycleConfig().fill_cancel_verify_attempts == 3
    assert LifecycleConfig().fill_cancel_verify_delay_s == 90.0
    cfg = build_lifecycle_config(
        RiskConfig(fill_cancel_verify_attempts=5, fill_cancel_verify_delay_s=30.0)
    )
    assert cfg.fill_cancel_verify_attempts == 5
    assert cfg.fill_cancel_verify_delay_s == 30.0
    # 0 = verification explicitly disabled (immediate discard) — allowed.
    assert RiskConfig(fill_cancel_verify_attempts=0).fill_cancel_verify_attempts == 0


def test_verify_config_validation() -> None:
    with pytest.raises(ValidationError):
        RiskConfig(fill_cancel_verify_attempts=-1)
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            RiskConfig(fill_cancel_verify_delay_s=bad)
        with pytest.raises(ValidationError):
            RiskConfig(position_reconcile_interval_s=bad)
