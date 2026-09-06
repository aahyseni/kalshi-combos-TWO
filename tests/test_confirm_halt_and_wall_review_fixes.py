"""REVIEW FIXES for ``build/confirm-halt-and-derived-wall`` (2026-09-05).

The review of d4de422 found one real defect in B and one misreport in A:

* MUST-FIX #1 — the derived wall could RATCHET: ``ProgressLedger.mark``
  recorded EVERY completed gap, including gaps that "completed" only because
  ``_bounded_store`` gave up at ``wall / MARGIN``; a completed gap in
  (wall/2, wall] then DOUBLED the wall at the next refresh and the bound with
  it, and the tape's retention (the oldest ``live_*.log``, 45 days) kept it
  there. Fixes pinned here: (a) a timed-out store await TAINTS the gap in
  progress and the ledger skips it; (b) N bounded-store timeouts leave the
  boot's histogram and the wall exactly where they were; (c) applying a wall
  above the floor is GATED behind ``supervisor.stall_wall_derived`` ("shadow"
  by default: derive + log, apply the floor; "on" = the operator's ruling).
* SHOULD-FIX #1 — the refresh's file I/O runs OFF the event loop.
* SHOULD-FIX #3 — a ``record_fill`` that times out and then LANDS is adopted
  by the replay (fee / fill.count / markout + verification), never skipped.
* SHOULD-FIX #4 — a whole pass over the APPLIED wall is counted
  (``maintenance.tick_over_wall``): with progress marked between sub-steps
  the supervisor no longer bounds the pass.
* SHOULD-FIX #5 — the derived replacement alarm for the removed halt class:
  this boot's exchange-expired share vs the pooled measured rate of the
  retained boots, as an exact binomial tail at the ladder's daily z
  (``confirm_expired_rate_anomalous``; alarm only — the halt is the operator
  ruling the report lists as owed).
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

import pytest
import structlog

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.ops.config import SupervisorConfig
from combomaker.ops.persistence import STORE_OP_TIMEOUT_S, Store
from combomaker.ops.quote_app import LOOP_MAINTENANCE, MAINTENANCE_TICK_INTERVAL_S, QuoteApp
from combomaker.risk.confirm_expired_rate import (
    EXPIRED_RATE_ALARM_Z,
    ExpiredRateTape,
    binomial_upper_tail,
    expired_tape_path,
    jeffreys_rate,
    judge_expired_rate,
    refresh_expired_baseline,
)
from combomaker.risk.progress import ProgressLedger, progress_path
from combomaker.risk.stall_wall import (
    STALL_WALL_MARGIN,
    GapHistogram,
    GapTape,
    gap_tape_path,
    normal_upper_tail_p,
    refresh_stall_wall,
)
from tests.test_confirm_halt_classifier import ScriptedSender, _accept_n, expired
from tests.test_filters import Harness
from tests.test_lifecycle import Rig, accepted_msg
from tests.test_liveness_progress import StepClock
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event
from tests.test_stall_wall import _HangingStore, _rig

TICK = MAINTENANCE_TICK_INTERVAL_S
WEDGE_S = 60.0
FLOOR_S = WEDGE_S + TICK  # 60.5 s live


# =========================================================================
# 1. MUST-FIX #1(a): a timed-out store await taints the gap in progress
# =========================================================================


def test_a_tainted_gap_is_skipped_and_counted(tmp_path: Path) -> None:
    clock = StepClock()
    ledger = ProgressLedger(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    ledger.register(LOOP_MAINTENANCE, interval_s=TICK, wedge_timeout_s=WEDGE_S, measure_gaps=True)
    ledger.register("quote", interval_s=0.0, wedge_timeout_s=WEDGE_S)
    clock.advance(0.5)
    ledger.mark(LOOP_MAINTENANCE)
    hist = ledger.gap_histogram(LOOP_MAINTENANCE)
    assert hist is not None and hist.n == 1 and hist.max_s == pytest.approx(0.5)
    # The gap that "completes" only because a bounded store await gave up.
    ledger.taint(LOOP_MAINTENANCE)
    clock.advance(45.0)
    ledger.mark(LOOP_MAINTENANCE)
    assert hist.n == 1 and hist.max_s == pytest.approx(0.5)  # NOT recorded
    assert ledger.tainted_gaps(LOOP_MAINTENANCE) == 1
    # The mark still advanced the loop's clock: the supervisor sees progress.
    loops: Any = ledger.snapshot()["loops"]
    assert loops[LOOP_MAINTENANCE]["age_s"] == pytest.approx(0.0)
    # The taint is consumed by that one mark — the next gap is healthy again.
    clock.advance(0.5)
    ledger.mark(LOOP_MAINTENANCE)
    assert hist.n == 2 and hist.max_s == pytest.approx(0.5)
    # Unmeasured / unregistered loops: nothing to protect, no-ops.
    ledger.taint("quote")
    ledger.taint("nope")
    assert ledger.tainted_gaps("quote") == 0 and ledger.tainted_gaps("nope") == 0
    # The bound callback the lifecycle receives.
    ledger.tainter(LOOP_MAINTENANCE)()
    clock.advance(45.0)
    ledger.mark(LOOP_MAINTENANCE)
    assert hist.n == 2 and ledger.tainted_gaps(LOOP_MAINTENANCE) == 2


async def test_n_bounded_store_timeouts_leave_the_histogram_and_the_wall_at_the_floor(
    tmp_path: Path,
) -> None:
    """The reviewer's test (b): a boot with N bounded-store timeouts — each
    followed by the long gap the timeout produced — leaves the boot row's
    ``max_s`` / quantile unchanged and the derived wall at the floor. The
    timeouts are REAL (``_bounded_store`` on a hanging store) and the taint
    reaches the ledger through the lifecycle's callback."""
    rig = await _rig(tmp_path)
    clock = StepClock()
    ledger = ProgressLedger(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    ledger.register(LOOP_MAINTENANCE, interval_s=TICK, wedge_timeout_s=WEDGE_S, measure_gaps=True)
    hanging = _HangingStore(rig.lifecycle._store, "has_fill")  # noqa: SLF001
    rig.lifecycle._store = hanging  # type: ignore[assignment]  # noqa: SLF001
    rig.lifecycle._clock = SystemClock()  # noqa: SLF001 (real wall time for wait_for)
    rig.lifecycle._sub_step_bound_s_cb = lambda: 0.2  # noqa: SLF001
    rig.lifecycle._taint_progress_cb = ledger.tainter(LOOP_MAINTENANCE)  # noqa: SLF001
    clock.advance(0.5)
    ledger.mark(LOOP_MAINTENANCE)
    hist = ledger.gap_histogram(LOOP_MAINTENANCE)
    assert hist is not None
    n_timeouts = 3
    for i in range(n_timeouts):
        await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id=f"rfq_{i}"))
        quote_id = rig.sender.created[-1]["id"]
        await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
        await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": f"o{i}"})
        clock.advance(45.0)  # the gap that only "completed" because the bound expired
        ledger.mark(LOOP_MAINTENANCE)
    assert rig.metrics.counter("store.await_timeout.has_fill") == n_timeouts
    assert ledger.tainted_gaps(LOOP_MAINTENANCE) == n_timeouts
    assert hist.n == 1 and hist.max_s == pytest.approx(0.5)
    assert hist.quantile(normal_upper_tail_p(5.0)) == pytest.approx(0.5)
    derivation = refresh_stall_wall(
        tape_path=gap_tape_path(tmp_path),
        data_dir=tmp_path,
        boot_key="this-boot",
        boot_started_at_ts=1.0,
        this_boot=hist.copy(),
        bucket_s=TICK,
        floor_s=FLOOR_S,
    )
    assert derivation.source == "floor"
    assert derivation.wall_s == pytest.approx(FLOOR_S)
    assert derivation.max_gap_s == pytest.approx(0.5)
    # Without the taint the same three gaps would have DOUBLED the wall.
    untainted = hist.copy()
    for _ in range(n_timeouts):
        untainted.observe(45.0)
    ratchet = refresh_stall_wall(
        tape_path=tmp_path / "counterfactual.json",
        data_dir=tmp_path,
        boot_key="this-boot",
        boot_started_at_ts=1.0,
        this_boot=untainted,
        bucket_s=TICK,
        floor_s=FLOOR_S,
    )
    assert ratchet.source == "measured" and ratchet.wall_s == pytest.approx(90.0)


# =========================================================================
# 2. MUST-FIX #1(c): loosening is an operator ruling — shadow / on
# =========================================================================


def _tape_with_a_45s_completed_gap(tmp_path: Path) -> None:
    hist = GapHistogram.empty(TICK)
    for _ in range(100):
        hist.observe(0.5)
    hist.observe(45.0)
    tape = GapTape(gap_tape_path(tmp_path))
    tape.fold("prior-boot", hist, started_at_ts=1.0)
    tape.save()


def _app_with_a_measured_loop(tmp_path: Path, *, mode: str) -> QuoteApp:
    from tests.test_metadata_change_scope import _armed_app

    app = _armed_app(tmp_path)
    app._config.data_dir = tmp_path  # noqa: SLF001
    app._config.supervisor.heartbeat_timeout_s = WEDGE_S  # noqa: SLF001
    app._config.supervisor.stall_wall_derived = mode  # noqa: SLF001
    app._progress = ProgressLedger(app._clock, progress_path(tmp_path))  # noqa: SLF001
    app._progress.register(  # noqa: SLF001
        LOOP_MAINTENANCE, interval_s=TICK, wedge_timeout_s=WEDGE_S, measure_gaps=True
    )
    return app


async def test_shadow_mode_logs_the_derived_wall_but_applies_the_floor(tmp_path: Path) -> None:
    _tape_with_a_45s_completed_gap(tmp_path)
    app = _app_with_a_measured_loop(tmp_path, mode="shadow")
    with structlog.testing.capture_logs() as cap:
        await app._refresh_stall_wall(reason="boot")  # noqa: SLF001
    derived = app._stall_wall  # noqa: SLF001
    assert derived is not None and derived.source == "measured"
    assert derived.wall_s == pytest.approx(90.0)  # what the tape would derive
    # ...but the supervisor's bound and the store bound stay at the floor.
    assert app._progress.stall_after_s(LOOP_MAINTENANCE) == pytest.approx(FLOOR_S)  # noqa: SLF001
    assert app._sub_step_bound_s() == pytest.approx(FLOOR_S / STALL_WALL_MARGIN)  # noqa: SLF001
    line = [c for c in cap if c.get("event") == "stall_wall_derivation"][0]
    assert line["mode"] == "shadow"
    assert line["wall_s"] == pytest.approx(90.0)
    assert line["applied_wall_s"] == pytest.approx(FLOOR_S)
    assert line["applied_sub_step_bound_s"] == pytest.approx(FLOOR_S / STALL_WALL_MARGIN)
    assert line["boot_tainted_gaps"] == 0


async def test_on_mode_applies_the_derived_wall(tmp_path: Path) -> None:
    _tape_with_a_45s_completed_gap(tmp_path)
    app = _app_with_a_measured_loop(tmp_path, mode="on")
    with structlog.testing.capture_logs() as cap:
        await app._refresh_stall_wall(reason="boot")  # noqa: SLF001
    assert app._progress.stall_after_s(LOOP_MAINTENANCE) == pytest.approx(90.0)  # noqa: SLF001
    assert app._sub_step_bound_s() == pytest.approx(45.0)  # noqa: SLF001
    line = [c for c in cap if c.get("event") == "stall_wall_derivation"][0]
    assert line["mode"] == "on" and line["applied_wall_s"] == pytest.approx(90.0)


async def test_either_mode_at_the_floor_is_the_floor(tmp_path: Path) -> None:
    """No tape (today's measured shape derives the floor anyway): shadow and
    on are byte-identical — the mode only matters when the tape would loosen."""
    for mode in ("shadow", "on"):
        d = tmp_path / mode
        d.mkdir()
        app = _app_with_a_measured_loop(d, mode=mode)
        await app._refresh_stall_wall(reason="boot")  # noqa: SLF001
        assert app._stall_wall is not None and app._stall_wall.source == "floor"  # noqa: SLF001
        assert app._progress.stall_after_s(LOOP_MAINTENANCE) == pytest.approx(FLOOR_S)  # noqa: SLF001
        assert app._sub_step_bound_s() == pytest.approx(FLOOR_S / STALL_WALL_MARGIN)  # noqa: SLF001


def test_default_mode_is_shadow_and_unknown_modes_are_rejected() -> None:
    assert SupervisorConfig().stall_wall_derived == "shadow"
    assert SupervisorConfig(stall_wall_derived="on").stall_wall_derived == "on"
    with pytest.raises(ValueError):
        SupervisorConfig(stall_wall_derived="off")
    with pytest.raises(ValueError):
        SupervisorConfig(stall_wall_derived="ON")


# =========================================================================
# 3. SHOULD-FIX #1: the refresh's file I/O runs off the event loop
# =========================================================================


async def test_the_refresh_runs_its_file_io_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import combomaker.ops.quote_app as qa

    app = _app_with_a_measured_loop(tmp_path, mode="shadow")
    loop_thread = threading.get_ident()
    seen: dict[str, Any] = {}
    real_refresh = qa.refresh_stall_wall

    def spy_refresh(**kw: Any) -> Any:
        seen["refresh_thread"] = threading.get_ident()
        seen["hist_is_a_copy"] = kw["this_boot"] is not app._progress.gap_histogram(  # noqa: SLF001
            LOOP_MAINTENANCE
        )
        return real_refresh(**kw)

    real_mtime = qa.oldest_live_log_mtime

    def spy_mtime(d: Path) -> float | None:
        seen["mtime_thread"] = threading.get_ident()
        return real_mtime(d)

    real_baseline = qa.refresh_expired_baseline

    def spy_baseline(**kw: Any) -> Any:
        seen["baseline_thread"] = threading.get_ident()
        return real_baseline(**kw)

    monkeypatch.setattr(qa, "refresh_stall_wall", spy_refresh)
    monkeypatch.setattr(qa, "oldest_live_log_mtime", spy_mtime)
    monkeypatch.setattr(qa, "refresh_expired_baseline", spy_baseline)
    await app._refresh_stall_wall(reason="boot")  # noqa: SLF001
    await app._refresh_expired_baseline(reason="boot")  # noqa: SLF001
    assert seen["refresh_thread"] != loop_thread
    assert seen["mtime_thread"] != loop_thread
    assert seen["baseline_thread"] != loop_thread
    assert seen["hist_is_a_copy"] is True
    # ...and the result was applied ON the loop after the thread returned.
    assert app._progress.stall_after_s(LOOP_MAINTENANCE) == pytest.approx(FLOOR_S)  # noqa: SLF001
    assert gap_tape_path(tmp_path).exists()
    assert expired_tape_path(tmp_path).exists()


# =========================================================================
# 4. SHOULD-FIX #4: a pass over the APPLIED wall is counted
# =========================================================================


async def test_a_pass_over_the_applied_wall_is_counted_as_over_wall(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    clock: FakeClock = rig.h.clock
    rig.lifecycle._sub_step_bound_s_cb = lambda: 1.0  # noqa: SLF001
    rig.lifecycle._stall_wall_s_cb = lambda: 2.0  # noqa: SLF001
    delay = {"s": 3.0}

    async def slow_withdraw() -> None:
        clock.advance(delay["s"])

    rig.lifecycle._resolve_withdraw_pending = slow_withdraw  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("maintenance.tick_over_wall") == 1
    assert rig.metrics.counter("maintenance.tick_slow") == 1
    line = [c for c in cap if c.get("event") == "maintenance_tick_slow"][0]
    assert line["over_wall"] is True and line["wall_s"] == pytest.approx(2.0)
    # Slow but under the wall: slow, not over_wall.
    delay["s"] = 1.5
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("maintenance.tick_over_wall") == 1
    assert rig.metrics.counter("maintenance.tick_slow") == 2
    # Unwired / broken wall provider: judged against the sub-step bound only.
    delay["s"] = 3.0
    rig.lifecycle._stall_wall_s_cb = None  # noqa: SLF001
    await rig.lifecycle.maintenance_tick()

    def broken() -> float:
        raise RuntimeError("provider broke")

    rig.lifecycle._stall_wall_s_cb = broken  # noqa: SLF001
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("maintenance.tick_over_wall") == 1
    assert rig.metrics.counter("maintenance.tick_slow") == 4


# =========================================================================
# 5. SHOULD-FIX #3: a late-landing record_fill is adopted, never skipped
# =========================================================================


async def test_a_record_fill_that_lands_late_is_adopted_not_skipped(tmp_path: Path) -> None:
    """``record_fill`` outlives its bound (the WS handler returns, the fill is
    held for replay) and then the cancelled INSERT LANDS on the connection
    thread. On ``main`` a slow write completed and continued; the bounded
    version returned early, so the replay used to hit ``has_fill`` → skip and
    the row sat unverified with fee / fill.count / markout never booked. Now
    the replay ADOPTS it exactly once and starts verification."""
    rig = await _rig(tmp_path)
    inner = rig.lifecycle._store  # noqa: SLF001
    hanging = _HangingStore(inner, "record_fill")
    rig.lifecycle._store = hanging  # type: ignore[assignment]  # noqa: SLF001
    rig.lifecycle._clock = SystemClock()  # noqa: SLF001
    rig.lifecycle._sub_step_bound_s_cb = lambda: 0.2  # noqa: SLF001
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_0"))
    quote_id = rig.sender.created[-1]["id"]
    await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
    msg = {"quote_id": quote_id, "order_id": "o1"}
    await rig.lifecycle.on_quote_executed(msg)
    state = rig.lifecycle._executed_states[quote_id]  # noqa: SLF001
    assert rig.metrics.counter("store.await_timeout.record_fill") == 1
    assert rig.metrics.counter("fill_ledger.write_failed") == 1
    assert state.fill_record_timed_out is True
    assert state.fill_recorded is False
    assert rig.metrics.counter("fill.count") == 0
    assert state.executed_msg is not None
    # THE LATE LANDING: the cancelled INSERT lands after the handler returned.
    fill_ref = f"fill:{quote_id}"
    assert state.pending_fill is not None
    accepted_side, bid, qty = state.pending_fill
    our_side = rig.lifecycle._conventions.maker_position_side(accepted_side)  # noqa: SLF001
    assert await inner.record_fill(
        fill_ref,
        order_id="o1",
        combo_ticker=state.rfq.market_ticker,
        our_side=str(our_side),
        contracts_centi=int(qty),
        price_cc=int(bid),
        fee_cc=0,
        expected_edge_cc=None,
        raw=msg,
    )
    rig.lifecycle._store = inner  # noqa: SLF001
    armed: list[str] = []
    real_on_written = rig.lifecycle._on_fill_row_written  # noqa: SLF001

    async def spy(qid: str, st: Any, m: Any, ref: str) -> None:
        armed.append(ref)
        await real_on_written(qid, st, m, ref)

    rig.lifecycle._on_fill_row_written = spy  # type: ignore[method-assign]  # noqa: SLF001
    # The recovery sweep's replay of the HELD message.
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_executed(dict(state.executed_msg))
    assert rig.metrics.counter("fill_ledger.late_landing_adopted") == 1
    assert rig.metrics.counter("fill.count") == 1
    assert state.fill_recorded is True
    assert state.fill_record_timed_out is False
    assert armed == [fill_ref]  # verification armed for the adopted row
    late = [c for c in cap if c.get("event") == "fill_record_landed_late"]
    assert len(late) == 1 and late[0]["via"] == "has_fill"
    # A further replay is an ordinary skip: adopted EXACTLY once.
    await rig.lifecycle.on_quote_executed(dict(state.executed_msg))
    assert rig.metrics.counter("fill_ledger.late_landing_adopted") == 1
    assert rig.metrics.counter("fill.count") == 1
    assert armed == [fill_ref]
    assert await inner.has_fill(fill_ref)


async def test_a_completed_record_fill_clears_the_late_landing_flag(tmp_path: Path) -> None:
    """A timed-out attempt whose INSERT was truly lost: the replay's own
    INSERT succeeds, books the tail once, and the flag is cleared so nothing
    later mistakes a genuine replay for a late landing."""
    rig = await _rig(tmp_path)
    inner = rig.lifecycle._store  # noqa: SLF001
    rig.lifecycle._store = _HangingStore(inner, "record_fill")  # type: ignore[assignment]  # noqa: SLF001
    rig.lifecycle._clock = SystemClock()  # noqa: SLF001
    rig.lifecycle._sub_step_bound_s_cb = lambda: 0.2  # noqa: SLF001
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_0"))
    quote_id = rig.sender.created[-1]["id"]
    await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    state = rig.lifecycle._executed_states[quote_id]  # noqa: SLF001
    assert state.fill_record_timed_out is True
    rig.lifecycle._store = inner  # noqa: SLF001
    assert state.executed_msg is not None
    await rig.lifecycle.on_quote_executed(dict(state.executed_msg))
    assert state.fill_recorded is True and state.fill_record_timed_out is False
    assert rig.metrics.counter("fill_ledger.late_landing_adopted") == 0
    assert rig.metrics.counter("fill.count") == 1
    await rig.lifecycle.on_quote_executed(dict(state.executed_msg))
    assert rig.metrics.counter("fill.count") == 1


# =========================================================================
# 6. SHOULD-FIX #5: the derived expired-rate alarm — the math
# =========================================================================


def test_binomial_upper_tail_matches_the_direct_sum_and_its_edges() -> None:
    for n, k, p in [(5, 2, 0.3), (10, 7, 0.5), (50, 5, 0.03), (117, 3, 0.0299)]:
        direct = sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))
        assert binomial_upper_tail(k, n, p) == pytest.approx(direct, rel=1e-9)
    assert binomial_upper_tail(0, 10, 0.5) == 1.0
    assert binomial_upper_tail(11, 10, 0.5) == 0.0
    assert binomial_upper_tail(3, 10, 0.0) == 0.0
    assert binomial_upper_tail(3, 10, 1.0) == 1.0
    # n in the hundreds: log-space, no overflow, still exact.
    direct = sum(math.comb(800, i) * 0.5**800 for i in range(400, 801))
    assert binomial_upper_tail(400, 800, 0.5) == pytest.approx(direct, rel=1e-9)
    with pytest.raises(ValueError):
        binomial_upper_tail(-1, 5, 0.5)


def test_jeffreys_rate_and_the_ladder_rung() -> None:
    assert jeffreys_rate(0, 116) == pytest.approx(0.5 / 117)
    assert jeffreys_rate(3, 113) == pytest.approx(3.5 / 117)
    assert EXPIRED_RATE_ALARM_Z == 3.0  # the ladder's DAILY rung (3 / 4 / 5)


def test_judge_is_unjudged_without_a_baseline_or_accepts() -> None:
    assert judge_expired_rate(boot_expired=3, boot_confirmed=0, baseline=None) is None
    assert judge_expired_rate(boot_expired=0, boot_confirmed=0, baseline=(3, 113, 1)) is None
    assert judge_expired_rate(boot_expired=1, boot_confirmed=0, baseline=(0, 0, 0)) is None


def test_todays_overnight_baseline_flags_the_afternoon_and_not_itself() -> None:
    overnight = (3, 113, 1)  # 3/116 = 2.6 % on the 00:52 boot
    one = judge_expired_rate(boot_expired=1, boot_confirmed=0, baseline=overnight)
    assert one is not None and not one.anomalous  # one lost auction is no anomaly
    assert one.alarm_p == pytest.approx(1.0 - normal_upper_tail_p(3.0))
    boot_0905 = judge_expired_rate(boot_expired=3, boot_confirmed=0, baseline=overnight)
    assert boot_0905 is not None and boot_0905.anomalous
    assert boot_0905.tail_p < boot_0905.alarm_p
    afternoon = judge_expired_rate(boot_expired=9, boot_confirmed=3, baseline=overnight)
    assert afternoon is not None and afternoon.anomalous
    assert afternoon.as_log()["boot_rate"] == pytest.approx(0.75)
    itself = judge_expired_rate(boot_expired=3, boot_confirmed=113, baseline=overnight)
    assert itself is not None and not itself.anomalous
    # In a world that runs at 70 %, three of three is not an anomaly.
    normal_there = judge_expired_rate(boot_expired=3, boot_confirmed=0, baseline=(70, 30, 4))
    assert normal_there is not None and not normal_there.anomalous
    assert normal_there.as_log()["boot_rate"] == 1.0


def test_expired_tape_folds_prunes_pools_and_excludes_this_boot(tmp_path: Path) -> None:
    path = expired_tape_path(tmp_path)
    tape = ExpiredRateTape(path)
    tape.load()
    tape.fold("old", expired=1, confirmed=99, started_at_ts=10.0)
    tape.fold("prior", expired=3, confirmed=113, started_at_ts=100.0)
    tape.fold("this", expired=9, confirmed=3, started_at_ts=200.0)
    tape.save()
    again = ExpiredRateTape(path)
    again.load()
    assert set(again.boots) == {"old", "prior", "this"}
    assert again.baseline_excluding("this") == (4, 212, 2)
    assert again.prune(retain_since_ts=50.0) == 1 and "old" not in again.boots
    assert again.baseline_excluding("this") == (3, 113, 1)
    assert again.baseline_excluding("prior") == (9, 3, 1)
    solo = ExpiredRateTape(tmp_path / "solo.json")
    solo.fold("this", expired=1, confirmed=1, started_at_ts=1.0)
    assert solo.baseline_excluding("this") is None
    # Corrupt / malformed rows degrade to "no baseline", never raise.
    path.write_text("{not json", encoding="utf-8")
    corrupt = ExpiredRateTape(path)
    corrupt.load()
    assert corrupt.boots == {}
    path.write_text(
        json.dumps(
            {
                "boots": {
                    "x": {"expired": -1, "confirmed": 2},
                    "y": "junk",
                    "z": {"expired": 2, "confirmed": 8, "started_at_ts": 5.0},
                }
            }
        ),
        encoding="utf-8",
    )
    corrupt.load()
    assert set(corrupt.boots) == {"z"}


def test_refresh_expired_baseline_returns_the_other_boots_pooled(tmp_path: Path) -> None:
    path = expired_tape_path(tmp_path)

    def refresh(key: str, ts: float, e: int, c: int, retain: float | None = None) -> Any:
        return refresh_expired_baseline(
            tape_path=path,
            boot_key=key,
            boot_started_at_ts=ts,
            boot_expired=e,
            boot_confirmed=c,
            retain_since_ts=retain,
        )

    assert refresh("b1", 1.0, 3, 113) is None  # first boot: nothing to compare to
    assert refresh("b2", 2.0, 9, 3) == (3, 113, 1)
    # A refresh REPLACES this boot's row (cumulative counters), never adds.
    assert refresh("b2", 2.0, 10, 5) == (3, 113, 1)
    tape = ExpiredRateTape(path)
    tape.load()
    assert tape.boots["b2"]["expired"] == 10 and len(tape.boots) == 2
    assert refresh("b3", 3.0, 0, 0) == (13, 118, 2)
    # The log retention prunes b1.
    assert refresh("b3", 3.0, 0, 0, retain=1.5) == (10, 5, 1)


async def test_quote_app_refreshes_the_baseline_from_its_own_counters(tmp_path: Path) -> None:
    app = _app_with_a_measured_loop(tmp_path, mode="shadow")
    path = expired_tape_path(tmp_path)
    prior = ExpiredRateTape(path)
    prior.fold("prior-boot", expired=3, confirmed=113, started_at_ts=1.0)
    prior.save()
    app._metrics.inc("confirm.sent", 10)  # noqa: SLF001
    app._metrics.inc("confirm.expired_by_exchange", 2)  # noqa: SLF001
    with structlog.testing.capture_logs() as cap:
        await app._refresh_expired_baseline(reason="boot")  # noqa: SLF001
    assert app._expired_baseline == (3, 113, 1)  # noqa: SLF001
    assert app._confirm_expired_baseline() == (3, 113, 1)  # noqa: SLF001
    tape = ExpiredRateTape(path)
    tape.load()
    row = tape.boots[app._boot_key]  # noqa: SLF001
    assert row["expired"] == 2 and row["confirmed"] == 10
    line = [c for c in cap if c.get("event") == "confirm_expired_baseline"][0]
    assert line["baseline_expired"] == 3 and line["baseline_boots"] == 1
    # An unchanged baseline on a refresh is silent.
    with structlog.testing.capture_logs() as cap:
        await app._refresh_expired_baseline(reason="refresh")  # noqa: SLF001
    assert not [c for c in cap if c.get("event") == "confirm_expired_baseline"]


# =========================================================================
# 7. SHOULD-FIX #5: the alarm through the real confirm path
# =========================================================================


async def _classifier_rig(tmp_path: Path) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    r = Rig(h, store)
    sender = ScriptedSender()
    r.sender = sender
    r.lifecycle._sender = sender  # noqa: SLF001 (test seam: scripted outcomes)
    return r


async def test_the_expired_rate_alarm_is_unjudged_without_a_baseline(tmp_path: Path) -> None:
    rig = await _classifier_rig(tmp_path)
    rig.sender.script = [expired() for _ in range(3)]
    await _accept_n(rig, 3)
    assert rig.metrics.counter("confirm.expired_by_exchange") == 3
    assert rig.metrics.counter("confirm.expired_rate_judged") == 0
    assert not rig.killswitch.halted
    rig.lifecycle._confirm_expired_baseline_cb = lambda: None  # noqa: SLF001 (first boot)
    rig.sender.script = [expired()]
    await _accept_n(rig, 1, start=3)
    assert rig.metrics.counter("confirm.expired_rate_judged") == 0


async def test_the_expired_rate_alarm_fires_against_the_pooled_baseline_and_never_halts(
    tmp_path: Path,
) -> None:
    """Boot 0905's shape (three expired in a row) judged against the overnight
    2.6 %: the 3rd expired accept is a 3σ event — WARNING + metric, no halt."""
    rig = await _classifier_rig(tmp_path)
    rig.lifecycle._confirm_expired_baseline_cb = lambda: (3, 113, 1)  # noqa: SLF001
    rig.sender.script = [None, None, expired(), expired(), expired()]
    with structlog.testing.capture_logs() as cap:
        await _accept_n(rig, 5)
    assert rig.metrics.counter("confirm.expired_rate_judged") == 3
    assert rig.metrics.counter("confirm.expired_rate_anomalous") == 1
    lines = [c for c in cap if c.get("event") == "confirm_expired_rate_anomalous"]
    assert len(lines) == 1
    line = lines[0]
    assert line["log_level"] == "warning"
    assert line["boot_expired"] == 3 and line["boot_confirmed"] == 2
    assert line["baseline_boots"] == 1 and line["anomalous"] is True
    assert line["tail_p"] < line["alarm_p"]
    assert not rig.killswitch.halted
    assert rig.metrics.counter("confirm.failed") == 0


async def test_the_expired_rate_alarm_is_quiet_at_the_baseline_rate(tmp_path: Path) -> None:
    rig = await _classifier_rig(tmp_path)
    rig.lifecycle._confirm_expired_baseline_cb = lambda: (70, 30, 4)  # noqa: SLF001
    rig.sender.script = [expired(), expired(), expired()]
    await _accept_n(rig, 3)
    assert rig.metrics.counter("confirm.expired_rate_judged") == 3
    assert rig.metrics.counter("confirm.expired_rate_anomalous") == 0


async def test_a_broken_baseline_provider_never_reaches_the_confirm_path(tmp_path: Path) -> None:
    rig = await _classifier_rig(tmp_path)

    def broken() -> tuple[int, int, int] | None:
        raise RuntimeError("provider broke")

    rig.lifecycle._confirm_expired_baseline_cb = broken  # noqa: SLF001
    rig.sender.script = [expired()]
    await _accept_n(rig, 1)
    assert rig.metrics.counter("confirm.expired_by_exchange") == 1
    assert rig.metrics.counter("confirm.expired_rate_judged") == 0
    assert not rig.killswitch.halted


# =========================================================================
# 6. TAPE-WRITER REVIEW #2 SHOULD-FIX 5: record_fill latency observed + alarmed
# =========================================================================


class _SlowStore:
    """The real store with ``record_fill`` taking ``advance_s`` of FAKE clock
    time (the store's wall latency as the lifecycle's clock sees it), then
    delegating — the row lands."""

    def __init__(self, inner: Store, clock: FakeClock, advance_s: float) -> None:
        self._inner = inner
        self._clock = clock
        self._advance_s = advance_s

    def __getattr__(self, name: str) -> Any:
        if name == "record_fill":

            async def _slow(*a: Any, **k: Any) -> Any:
                self._clock.advance(self._advance_s)
                return await self._inner.record_fill(*a, **k)

            return _slow
        return getattr(self._inner, name)


async def test_record_fill_latency_is_observed_and_alarmed_past_the_store_bound(
    tmp_path: Path,
) -> None:
    """Every ``record_fill`` lands in the ``fill_ledger.record_fill_ms``
    histogram (p50/p95/max in the metrics snapshot — the relight read); one
    that outlives ``STORE_OP_TIMEOUT_S`` (the store's own busy_timeout bound,
    no new number) raises the DERIVED alarm ``fill_ledger_write_slow`` +
    ``fill_ledger.record_fill_slow`` with the row's fate named (landed) — the
    fill itself is booked exactly as before."""
    rig = await _rig(tmp_path)
    clock: FakeClock = rig.h.clock
    inner = rig.lifecycle._store  # noqa: SLF001
    rig.lifecycle._store = _SlowStore(inner, clock, STORE_OP_TIMEOUT_S + 1.0)  # type: ignore[assignment]  # noqa: SLF001
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_0"))
    quote_id = rig.sender.created[-1]["id"]
    await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
    with structlog.testing.capture_logs() as cap:
        await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    assert rig.metrics.counter("fill.count") == 1
    assert rig.metrics.counter("fill_ledger.write_failed") == 0
    assert await inner.has_fill(f"fill:{quote_id}")
    p50 = rig.metrics.quantile_ms("fill_ledger.record_fill_ms", 0.5)
    assert p50 is not None
    hist_max = rig.metrics.histogram_max_ms("fill_ledger.record_fill_ms")
    assert hist_max is not None and hist_max >= (STORE_OP_TIMEOUT_S + 1.0) * 1000.0
    assert rig.metrics.counter("fill_ledger.record_fill_slow") == 1
    slow = [c for c in cap if c.get("event") == "fill_ledger_write_slow"]
    assert len(slow) == 1
    assert slow[0]["bound_ms"] == STORE_OP_TIMEOUT_S * 1000.0
    assert slow[0]["outcome"] == "landed"
    assert slow[0]["elapsed_ms"] > slow[0]["bound_ms"]
    snapshot = rig.metrics.snapshot()
    latencies = snapshot["latencies_ms"]
    assert isinstance(latencies, dict)
    assert latencies["fill_ledger.record_fill_ms"]["count"] == 1
    # A fill inside the bound: observed, never alarmed.
    rig.lifecycle._store = _SlowStore(inner, clock, STORE_OP_TIMEOUT_S / 10)  # type: ignore[assignment]  # noqa: SLF001
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_1"))
    quote_id_2 = rig.sender.created[-1]["id"]
    assert quote_id_2 != quote_id
    await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id_2, "yes"))
    with structlog.testing.capture_logs() as cap2:
        await rig.lifecycle.on_quote_executed({"quote_id": quote_id_2, "order_id": "o2"})
    assert rig.metrics.counter("fill.count") == 2
    assert rig.metrics.counter("fill_ledger.record_fill_slow") == 1
    assert [c for c in cap2 if c.get("event") == "fill_ledger_write_slow"] == []
    assert rig.metrics.snapshot()["latencies_ms"]["fill_ledger.record_fill_ms"]["count"] == 2  # type: ignore[index]
