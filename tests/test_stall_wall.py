"""DERIVED MAINTENANCE STALL WALL + BOUNDED STORE AWAITS (2026-09-05 build,
item 6).

The receipts said ``supervisor kill: loop stalled: maintenance age=61.1s >
60.5s`` — 28 of them since 2026-08-17. What the log tails say (each receipt's
boot, tail-checked 2026-09-05): in 15 the supervisor's wedge verdict comes
FIRST and the bot's ``kill_switch_halt reason=halt_kill_file`` follows 1-4 s
later — the process was alive enough to read the supervisor's KILL file, so
the event loop was fine and the MAINTENANCE LOOP ALONE had gone 61 s without
a mark: a sub-step held in an un-bounded await (8/17 x3, 8/20 x5, 8/26 x6,
8/27 x1). In the other 13 (both of 2026-09-05's included) another halt came
first and the SHUTDOWN wedged after ``joint_pool_stopped``; the supervisor
stamped a stall receipt on a process that was already exiting. And nothing
ever RECORDED the pass distribution the 60 s was meant to clear:
``loop_progress.json`` is one overwritten snapshot.

So the mechanism repair is (a) MEASURE the loop's completed inter-mark gaps,
(b) DERIVE the wall from them with the hang watchdog's own rule (margin x the
upper quantile at the KILL z), floored at today's bound so it can only loosen
by measurement, (c) BOUND every direct store await on the maintenance path by
the same distribution, and (d) MARK PROGRESS between sub-steps. These tests
pin each, and — the one that must never be lost — that a TRULY hung loop is
still killed.

Blast radius: supervisor bound + maintenance loop. Pricing untouched.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.clock import FakeClock, SystemClock
from combomaker.ops.persistence import STORE_OP_TIMEOUT_S, Store
from combomaker.ops.quote_app import LOOP_MAINTENANCE, LOOP_QUOTE, MAINTENANCE_TICK_INTERVAL_S
from combomaker.ops.supervisor import SafetySupervisor, SupervisorConfig
from combomaker.risk.heartbeat import Heartbeat
from combomaker.risk.limits import RiskLimits
from combomaker.risk.progress import ProgressLedger, ProgressReader, progress_path
from combomaker.risk.stall_wall import (
    STALL_WALL_MARGIN,
    WALL_QUANTILE_Z,
    GapHistogram,
    GapTape,
    derive_stall_wall,
    gap_tape_path,
    normal_upper_tail_p,
    refresh_stall_wall,
)
from tests.test_filters import Harness
from tests.test_lifecycle import Rig, accepted_msg
from tests.test_liveness_progress import StepClock
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, seed_event

REPO = Path(__file__).resolve().parents[1]
FLOOR_LIVE_S = 60.0 + MAINTENANCE_TICK_INTERVAL_S  # today's live bound: 60.5 s
TICK = MAINTENANCE_TICK_INTERVAL_S


# =========================================================================
# 1. The histogram + the derivation
# =========================================================================


def test_histogram_buckets_at_the_loop_cadence_and_quantiles_to_the_tick() -> None:
    h = GapHistogram.empty(TICK)
    for g in (0.1, 0.4, 0.6, 1.1, 2.4):
        h.observe(g)
    assert h.n == 5 and h.max_s == 2.4
    assert h.counts == {0: 2, 1: 1, 2: 1, 4: 1}
    assert h.quantile(0.5) == pytest.approx(1.0)  # 3rd of 5 sits in bucket 1 → edge 1.0
    assert h.quantile(1.0) == pytest.approx(2.4)  # capped at the max, not the bucket edge
    h.observe(-1.0)  # a clock step backwards is not a gap
    h.observe(float("nan"))
    h.observe(float("inf"))
    assert h.n == 5


def test_histogram_merge_and_json_roundtrip() -> None:
    a = GapHistogram.empty(TICK)
    b = GapHistogram.empty(TICK)
    for g in (0.2, 0.7):
        a.observe(g)
    for g in (3.3, 0.1):
        b.observe(g)
    a.merge(b)
    assert (a.n, a.max_s) == (4, 3.3)
    back = GapHistogram.from_json(json.loads(json.dumps(a.to_json())))
    assert back is not None
    assert back.counts == a.counts and back.n == a.n and back.max_s == a.max_s
    with pytest.raises(ValueError):
        a.merge(GapHistogram.empty(1.0))


@pytest.mark.parametrize(
    "payload",
    [None, "x", {"bucket_s": 0.5, "counts": [1, 2]}, {"bucket_s": 0.5, "counts": {"-1": 3}},
     {"bucket_s": 0, "counts": {}}, {"bucket_s": 0.5, "counts": {"1": 2}, "n": 5}],
)
def test_corrupt_histograms_are_dropped_not_raised(payload: Any) -> None:
    assert GapHistogram.from_json(payload) is None


def test_quantile_z_is_the_policy_kill_anchor_and_margin_is_the_watchdogs() -> None:
    assert WALL_QUANTILE_Z == 5.0
    assert normal_upper_tail_p(5.0) == pytest.approx(1 - 2.867e-7, abs=1e-8)
    # the hang watchdog's ``_MARGIN`` IS the rule — read from the tool itself
    spec = importlib.util.spec_from_file_location(
        "hang_watchdog_for_parity", REPO / "tools" / "ops" / "hang_watchdog.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # the tool's dataclasses resolve annotations via sys.modules
    try:
        spec.loader.exec_module(mod)
        assert mod._MARGIN == STALL_WALL_MARGIN  # noqa: SLF001
    finally:
        sys.modules.pop(spec.name, None)


def test_no_tape_derives_the_floor() -> None:
    d = derive_stall_wall(None, floor_s=FLOOR_LIVE_S)
    assert d.wall_s == FLOOR_LIVE_S and d.source == "floor" and d.n_gaps == 0
    assert d.sub_step_bound_s == pytest.approx(FLOOR_LIVE_S / STALL_WALL_MARGIN)
    empty = GapHistogram.empty(TICK)
    assert derive_stall_wall(empty, floor_s=FLOOR_LIVE_S).wall_s == FLOOR_LIVE_S


def test_todays_measured_distribution_keeps_the_floor() -> None:
    """The 2026-09-05 12:13 boot, ``loop_progress.json`` sampled read-only at
    4 Hz for 30 min (17:29-17:59Z, 7,059 samples of the maintenance loop's
    last-mark age): < 0.5 s 6,068 | 0.5-1 s 863 | 1-2 s 100 | 2-3 s 23 |
    3-5 s 5 | >= 5 s 0; max 3.544 s (17:55:00Z). The same shape here, one
    representative gap per bucket. The wall stays at the floor — the
    measurement does not loosen it, and it cannot tighten it."""
    h = GapHistogram.empty(TICK)
    for _ in range(6068):
        h.observe(0.3)
    for _ in range(863):
        h.observe(0.7)
    for _ in range(100):
        h.observe(1.5)
    for _ in range(23):
        h.observe(2.5)
    for _ in range(4):
        h.observe(3.1)
    h.observe(3.544)
    assert h.n == 7059
    d = derive_stall_wall(h, floor_s=FLOOR_LIVE_S, boots_pooled=1)
    assert d.max_gap_s == 3.544
    assert d.quantile_s == pytest.approx(3.544)  # Φ(5) of n=7,059 is the max
    assert d.wall_s == FLOOR_LIVE_S and d.source == "floor"
    assert d.sub_step_bound_s == pytest.approx(FLOOR_LIVE_S / STALL_WALL_MARGIN)
    # the hand number sits ~17x above the longest gap the loop completed
    assert FLOOR_LIVE_S / d.max_gap_s > 17.0


def test_a_measured_slow_pass_loosens_the_wall_by_the_margin_only() -> None:
    """If the loop ever COMPLETES a 45 s gap (the 8/18 claim was 29-31 s
    passes), the wall becomes margin x that — never a hand number, never
    below the floor, and the store bound is the measured quantile itself."""
    h = GapHistogram.empty(TICK)
    for _ in range(1000):
        h.observe(0.5)
    h.observe(45.0)
    d = derive_stall_wall(h, floor_s=FLOOR_LIVE_S)
    assert d.quantile_s == pytest.approx(45.0)
    assert d.wall_s == pytest.approx(STALL_WALL_MARGIN * 45.0) and d.source == "measured"
    assert d.sub_step_bound_s == pytest.approx(45.0)
    assert d.wall_s > FLOOR_LIVE_S


def test_the_wall_never_drops_below_the_floor_for_any_sample() -> None:
    for max_gap in (0.01, 0.5, 5.0, 29.0, 30.24, 30.26, 100.0):
        h = GapHistogram.empty(TICK)
        h.observe(max_gap)
        d = derive_stall_wall(h, floor_s=FLOOR_LIVE_S)
        assert d.wall_s >= FLOOR_LIVE_S
        assert d.wall_s == pytest.approx(max(FLOOR_LIVE_S, STALL_WALL_MARGIN * max_gap))


def test_derivation_rejects_a_margin_inside_the_distribution() -> None:
    with pytest.raises(ValueError):
        derive_stall_wall(None, floor_s=FLOOR_LIVE_S, margin=0.9)
    with pytest.raises(ValueError):
        derive_stall_wall(None, floor_s=0.0)
    with pytest.raises(ValueError):
        derive_stall_wall(None, floor_s=math.inf)


# =========================================================================
# 2. The tape: per-boot rows, retention = the existing log retention
# =========================================================================


def test_tape_folds_prunes_pools_and_survives_a_roundtrip(tmp_path: Path) -> None:
    path = gap_tape_path(tmp_path)
    tape = GapTape(path)
    tape.load()  # absent file ⇒ empty, no raise
    old = GapHistogram.empty(TICK)
    old.observe(40.0)
    new = GapHistogram.empty(TICK)
    new.observe(0.5)
    tape.fold("boot-old", old, started_at_ts=1_000.0)
    tape.fold("boot-new", new, started_at_ts=2_000.0)
    tape.save()

    again = GapTape(path)
    again.load()
    assert set(again.boots) == {"boot-old", "boot-new"}
    assert again.pooled(TICK).max_s == 40.0
    # retention horizon between the two boots: the old row goes
    assert again.prune(retain_since_ts=1_500.0) == 1
    assert set(again.boots) == {"boot-new"}
    assert again.pooled(TICK).max_s == 0.5
    # no horizon ⇒ nothing pruned; a row without a stamp is never pruned
    assert again.prune(retain_since_ts=None) == 0
    again.boots["stampless"] = {"started_at_ts": None, "hist": new.copy()}
    assert again.prune(retain_since_ts=9_999.0) == 1  # boot-new (2000) goes, stampless stays
    assert set(again.boots) == {"stampless"}


def test_tape_fold_replaces_this_boots_row_it_never_double_counts(tmp_path: Path) -> None:
    tape = GapTape(gap_tape_path(tmp_path))
    h = GapHistogram.empty(TICK)
    h.observe(0.5)
    tape.fold("b", h, started_at_ts=1.0)
    h.observe(0.5)
    tape.fold("b", h, started_at_ts=1.0)  # cumulative histogram, folded twice
    assert tape.pooled(TICK).n == 2


def test_corrupt_tape_degrades_to_no_tape(tmp_path: Path) -> None:
    path = gap_tape_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    tape = GapTape(path)
    tape.load()
    assert tape.boots == {}
    path.write_text(json.dumps({"boots": {"x": {"hist": {"bucket_s": "bad"}}}}), "utf-8")
    tape.load()
    assert tape.boots == {}


def test_refresh_pools_this_boot_with_the_retained_tape(tmp_path: Path) -> None:
    """Boot 1 records a 33 s completed gap; boot 2 starts with no gaps of its
    own and still inherits boot 1's evidence → wall = margin x 33 s. Then
    boot 1's log rotates away → the wall returns to the floor."""
    log1 = tmp_path / "live_20260905_0001.log"
    log1.write_text("x", encoding="utf-8")
    b1 = GapHistogram.empty(TICK)
    b1.observe(33.0)
    d1 = refresh_stall_wall(
        tape_path=gap_tape_path(tmp_path), data_dir=tmp_path, boot_key="b1",
        boot_started_at_ts=time.time(), this_boot=b1, bucket_s=TICK, floor_s=FLOOR_LIVE_S,
    )
    assert d1.wall_s == pytest.approx(STALL_WALL_MARGIN * 33.0) and d1.boots_pooled == 1
    d2 = refresh_stall_wall(
        tape_path=gap_tape_path(tmp_path), data_dir=tmp_path, boot_key="b2",
        boot_started_at_ts=time.time(), this_boot=GapHistogram.empty(TICK),
        bucket_s=TICK, floor_s=FLOOR_LIVE_S,
    )
    assert d2.wall_s == pytest.approx(STALL_WALL_MARGIN * 33.0) and d2.boots_pooled == 1
    # the operator's log retention moves past boot 1 (a newer log is the oldest left)
    log1.unlink()
    log2 = tmp_path / "live_20260905_9999.log"
    log2.write_text("x", encoding="utf-8")
    future = time.time() + 3600.0
    import os

    os.utime(log2, (future, future))
    d3 = refresh_stall_wall(
        tape_path=gap_tape_path(tmp_path), data_dir=tmp_path, boot_key="b3",
        boot_started_at_ts=time.time(), this_boot=None, bucket_s=TICK, floor_s=FLOOR_LIVE_S,
    )
    assert d3.wall_s == FLOOR_LIVE_S and d3.source == "floor" and d3.boots_pooled == 0


# =========================================================================
# 3. The ledger measures ONLY the loop asked to, and publishes the derived bound
# =========================================================================


def test_ledger_measures_completed_gaps_for_the_maintenance_loop_only(tmp_path: Path) -> None:
    clock = StepClock()
    ledger = ProgressLedger(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    ledger.register(LOOP_MAINTENANCE, interval_s=TICK, wedge_timeout_s=60.0, measure_gaps=True)
    ledger.register(LOOP_QUOTE, interval_s=0.0, wedge_timeout_s=60.0)
    for gap in (0.5, 0.5, 7.0):
        clock.advance(gap)
        ledger.mark(LOOP_MAINTENANCE)
        ledger.mark(LOOP_QUOTE)
    hist = ledger.gap_histogram(LOOP_MAINTENANCE)
    assert hist is not None and hist.n == 3 and hist.max_s == pytest.approx(7.0)
    assert ledger.gap_histogram(LOOP_QUOTE) is None  # the quote path pays nothing
    with pytest.raises(ValueError):
        ledger.register("x", interval_s=0.0, wedge_timeout_s=1.0, measure_gaps=True)


def test_set_stall_after_reaches_the_supervisor_through_the_file(tmp_path: Path) -> None:
    clock = StepClock()
    ledger = ProgressLedger(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    ledger.register(LOOP_MAINTENANCE, interval_s=TICK, wedge_timeout_s=60.0, measure_gaps=True)
    assert ledger.stall_after_s(LOOP_MAINTENANCE) == pytest.approx(FLOOR_LIVE_S)
    reader = ProgressReader(clock, progress_path(tmp_path))  # type: ignore[arg-type]
    ledger.set_stall_after(LOOP_MAINTENANCE, 90.0)
    ledger.mark(LOOP_MAINTENANCE)
    clock.advance(70.0)  # past the old 60.5, inside the derived 90
    ledger.publish()
    assert reader.wedged_detail() is None
    clock.advance(21.0)  # past 90
    ledger.publish()
    detail = reader.wedged_detail()
    assert detail is not None and LOOP_MAINTENANCE in detail and "90.0s" in detail
    with pytest.raises(KeyError):
        ledger.set_stall_after("nope", 1.0)
    with pytest.raises(ValueError):
        ledger.set_stall_after(LOOP_MAINTENANCE, 0.0)


# =========================================================================
# 4. Bounded store awaits on the maintenance path
# =========================================================================


async def _rig(tmp_path: Path, db: str = "t.sqlite3") -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / db, h.clock)
    return Rig(h, store)


class _HangingStore:
    """The real store with ONE named read that never returns — a saturated
    aiosqlite connection thread from the caller's side."""

    def __init__(self, inner: Store, op: str) -> None:
        self._inner = inner
        self._op = op
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        if name == self._op:

            async def _hang(*_a: Any, **_k: Any) -> Any:
                self.calls += 1
                await asyncio.Event().wait()

            return _hang
        return getattr(self._inner, name)


BOUNDED_OPS = {
    "mark_fill_verified", "has_fill", "fill_ref_for_order_id", "record_fill",
    "void_phantom_fill", "has_fill_for_order_id", "open_ledger_identities",
    "fill_order_ids", "fill_null_order_id_keys",
}


def test_every_maintenance_path_store_await_is_bounded() -> None:
    """The deep dive's twelve sites, as a regression guard: none of these ops
    may be awaited directly again. (Decision-record writes on the quote path
    are queued writes with their own semantics and are not in this set.)"""
    src = (REPO / "src" / "combomaker" / "rfq" / "lifecycle.py").read_text(encoding="utf-8")
    direct = re.findall(r"await self\._store\.(\w+)\(", src)
    leaked = sorted(set(direct) & BOUNDED_OPS)
    assert leaked == [], f"unbounded maintenance-path store awaits: {leaked}"
    wrapped = re.findall(r'_bounded_store\(\s*"(\w+)"', src)
    assert set(wrapped) == BOUNDED_OPS
    assert len(wrapped) == 12


async def test_a_hanging_store_op_times_out_cleanly_and_the_fill_is_retryable(
    tmp_path: Path,
) -> None:
    """``has_fill`` hangs forever under the fill-record path. Pre-fix the await
    was unbounded. Now: the derived bound expires, a WARNING + metric land,
    the executed message stays held for the recovery replay, and the caller
    returns — no wedge, no lost fill, no second row."""
    rig = await _rig(tmp_path)
    hanging = _HangingStore(rig.lifecycle._store, "has_fill")  # noqa: SLF001
    rig.lifecycle._store = hanging  # type: ignore[assignment]  # noqa: SLF001
    rig.lifecycle._clock = SystemClock()  # noqa: SLF001 (real wall time for wait_for)
    rig.lifecycle._sub_step_bound_s_cb = lambda: 0.2  # noqa: SLF001 (derived bound seam)
    await rig.lifecycle.handle_rfq(combo(CROSS_EVENT_LEGS, id="rfq_0"))
    quote_id = rig.sender.created[-1]["id"]
    await rig.lifecycle.on_quote_accepted(accepted_msg(quote_id, "yes"))
    assert f"fill:{quote_id}" in rig.exposure.positions  # booked at confirm
    t0 = time.monotonic()
    await rig.lifecycle.on_quote_executed({"quote_id": quote_id, "order_id": "o1"})
    elapsed = time.monotonic() - t0
    assert hanging.calls == 1
    assert elapsed < 2.0, f"the bounded await took {elapsed:.2f}s"
    assert rig.metrics.counter("store.await_timeout") == 1
    assert rig.metrics.counter("store.await_timeout.has_fill") == 1
    assert rig.metrics.counter("fill_ledger.write_failed") == 1
    state = rig.lifecycle._executed_states[quote_id]  # noqa: SLF001
    assert state.fill_recorded is False
    assert state.executed_msg is not None  # held for the recovery replay
    assert state.fill_write_inflight is False


async def test_store_bound_falls_back_to_the_store_primitive(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    assert rig.lifecycle._store_bound_s() == STORE_OP_TIMEOUT_S  # noqa: SLF001
    rig.lifecycle._sub_step_bound_s_cb = lambda: None  # noqa: SLF001
    assert rig.lifecycle._store_bound_s() == STORE_OP_TIMEOUT_S  # noqa: SLF001
    rig.lifecycle._sub_step_bound_s_cb = lambda: 30.25  # noqa: SLF001
    assert rig.lifecycle._store_bound_s() == 30.25  # noqa: SLF001

    def _broken() -> float:
        raise RuntimeError("provider broke")

    rig.lifecycle._sub_step_bound_s_cb = _broken  # noqa: SLF001
    assert rig.lifecycle._store_bound_s() == STORE_OP_TIMEOUT_S  # noqa: SLF001


# =========================================================================
# 5. Progress advances BETWEEN sub-steps
# =========================================================================


async def test_progress_marks_between_sub_steps_not_only_at_the_top(tmp_path: Path) -> None:
    """A 10 s sub-step (the fill sweep) is followed by a mark BEFORE the
    limit check and the reprice run — the supervisor sees the loop advance
    inside the pass. The step is timed by name."""
    rig = await _rig(tmp_path)
    clock: FakeClock = rig.h.clock
    beats: list[int] = []
    rig.lifecycle._beat_cb = lambda: beats.append(clock.monotonic_ns())  # noqa: SLF001
    rig.lifecycle._sub_step_bound_s_cb = lambda: 30.25  # noqa: SLF001 (live floor / margin)

    async def slow_sweep() -> None:
        clock.advance(10.0)

    rig.lifecycle._sweep_unrecorded_fills = slow_sweep  # type: ignore[method-assign]
    start = clock.monotonic_ns()
    await rig.lifecycle.maintenance_tick()
    # beats: prelude, unrecorded_fills, limits, withdraw_pending, reprice
    assert len(beats) >= 5
    after_slow = [b for b in beats if b - start >= int(10.0 * 1e9)]
    assert len(after_slow) >= 4  # every lap after the slow step marked progress
    assert rig.metrics.histogram_max_ms("maintenance.step_ms.unrecorded_fills") == pytest.approx(
        10_000.0
    )
    assert rig.metrics.histogram_max_ms("maintenance.tick_ms") == pytest.approx(10_000.0)
    assert rig.metrics.counter("maintenance.tick_slow") == 0  # 10 s is inside the 30.25 s bound


async def test_a_pass_over_the_measured_bound_logs_its_step_breakdown(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    clock: FakeClock = rig.h.clock
    rig.lifecycle._sub_step_bound_s_cb = lambda: 1.0  # noqa: SLF001

    async def slow_withdraw() -> None:
        clock.advance(3.0)

    rig.lifecycle._resolve_withdraw_pending = slow_withdraw  # type: ignore[method-assign]
    await rig.lifecycle.maintenance_tick()
    assert rig.metrics.counter("maintenance.tick_slow") == 1
    assert rig.metrics.histogram_max_ms("maintenance.step_ms.withdraw_pending") == pytest.approx(
        3_000.0
    )


async def test_a_halting_pass_still_records_its_timing(tmp_path: Path) -> None:
    """The tick body returns early on a halt; the wrapper's finally still
    lands the tick timing (and the pre-halt laps marked progress)."""
    rig = await _rig(tmp_path)
    beats: list[int] = []
    rig.lifecycle._beat_cb = lambda: beats.append(1)  # noqa: SLF001
    rig.lifecycle._limits._limits = RiskLimits(max_daily_loss_dollars=1.0)  # noqa: SLF001
    rig.lifecycle.record_realized_pnl(-20_000)
    await rig.lifecycle.maintenance_tick()
    assert rig.killswitch.halted
    assert rig.metrics.quantile_ms("maintenance.tick_ms", 0.5) is not None
    assert len(beats) >= 2  # prelude + unrecorded_fills marked before the halt


# =========================================================================
# 6. The one property that must never be lost: a TRULY hung loop still dies
# =========================================================================


async def test_a_truly_hung_maintenance_loop_is_still_killed_at_the_derived_wall(
    tmp_path: Path,
) -> None:
    """The REAL ``_maintenance_loop`` over a tick that never returns (an
    unbounded await — the shape the store bounds cannot cover), against a REAL
    supervisor reading the REAL ledger with the DERIVED wall (no tape ⇒ the
    floor). Wedge tolerance squeezed to 0.5 s so the 3 s run is six
    tolerances: the supervisor MUST fire, write KILL, and name the loop."""
    from tests.test_metadata_change_scope import _armed_app

    rig = await _rig(tmp_path, "hung.sqlite3")
    app = _armed_app(tmp_path)
    clock = SystemClock()
    app._clock = clock  # noqa: SLF001
    app._heartbeat = Heartbeat(clock, tmp_path / "heartbeat.txt")  # noqa: SLF001
    app._progress = ProgressLedger(clock, progress_path(tmp_path))  # noqa: SLF001
    wedge_timeout_s = 0.5
    app._config.supervisor.heartbeat_timeout_s = wedge_timeout_s  # noqa: SLF001
    app._config.supervisor.poll_interval_s = 0.1  # noqa: SLF001
    app._config.data_dir = tmp_path  # noqa: SLF001
    app._progress.register(  # noqa: SLF001
        LOOP_MAINTENANCE,
        interval_s=MAINTENANCE_TICK_INTERVAL_S,
        wedge_timeout_s=wedge_timeout_s,
        measure_gaps=True,
    )
    await app._refresh_stall_wall(reason="boot")  # noqa: SLF001
    derived = app._stall_wall  # noqa: SLF001
    assert derived is not None and derived.source == "floor"
    assert derived.wall_s == pytest.approx(wedge_timeout_s + MAINTENANCE_TICK_INTERVAL_S)
    assert app._progress.stall_after_s(LOOP_MAINTENANCE) == pytest.approx(  # noqa: SLF001
        derived.wall_s
    )
    assert gap_tape_path(tmp_path).exists()

    entered = 0

    async def hung_tick() -> None:
        nonlocal entered
        entered += 1
        await asyncio.Event().wait()  # never returns — a true hang

    rig.lifecycle.maintenance_tick = hung_tick  # type: ignore[method-assign]

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
    liveness = asyncio.create_task(app._liveness_loop())  # noqa: SLF001
    maintenance = asyncio.create_task(app._maintenance_loop(rig.lifecycle))  # noqa: SLF001
    verdicts: list[Any] = []
    deadline = time.monotonic() + 3.0
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            verdicts.append(await supervisor.check_once())
            if any(v is not None for v in verdicts):
                break
    finally:
        maintenance.cancel()
        liveness.cancel()
        for t in (maintenance, liveness):
            try:
                await t
            except asyncio.CancelledError:
                pass
    assert entered == 1
    kills = [v for v in verdicts if v is not None]
    assert kills, "a truly hung maintenance loop was NOT killed"
    assert (tmp_path / "KILL").exists()
    assert LOOP_MAINTENANCE in (tmp_path / "KILL").read_text(encoding="utf-8")
    assert cancelled == ["q1"]


async def test_a_healthy_loop_with_a_bounded_store_is_not_killed(tmp_path: Path) -> None:
    """Control for the test above, through the same derived wall: the store
    hangs (bounded by the derived sub-step bound), the loop keeps advancing,
    the supervisor never fires."""
    from tests.test_metadata_change_scope import _armed_app

    rig = await _rig(tmp_path, "healthy.sqlite3")
    hanging = _HangingStore(rig.lifecycle._store, "open_ledger_identities")  # noqa: SLF001
    rig.lifecycle._store = hanging  # type: ignore[assignment]  # noqa: SLF001
    rig.lifecycle._clock = SystemClock()  # noqa: SLF001
    app = _armed_app(tmp_path)
    clock = SystemClock()
    app._clock = clock  # noqa: SLF001
    app._heartbeat = Heartbeat(clock, tmp_path / "heartbeat.txt")  # noqa: SLF001
    app._progress = ProgressLedger(clock, progress_path(tmp_path))  # noqa: SLF001
    wedge_timeout_s = 1.0
    app._config.supervisor.heartbeat_timeout_s = wedge_timeout_s  # noqa: SLF001
    app._config.supervisor.poll_interval_s = 0.1  # noqa: SLF001
    app._config.data_dir = tmp_path  # noqa: SLF001
    app._progress.register(  # noqa: SLF001
        LOOP_MAINTENANCE,
        interval_s=MAINTENANCE_TICK_INTERVAL_S,
        wedge_timeout_s=wedge_timeout_s,
        measure_gaps=True,
    )
    await app._refresh_stall_wall(reason="boot")  # noqa: SLF001
    rig.lifecycle._sub_step_bound_s_cb = app._sub_step_bound_s  # noqa: SLF001
    assert rig.lifecycle._store_bound_s() == pytest.approx(  # noqa: SLF001
        (wedge_timeout_s + MAINTENANCE_TICK_INTERVAL_S) / STALL_WALL_MARGIN
    )
    supervisor = SafetySupervisor(
        SupervisorConfig(
            heartbeat_path=tmp_path / "heartbeat.txt",
            kill_file=tmp_path / "KILL",
            reconcile_marker_path=tmp_path / "needs_reconcile",
            heartbeat_timeout_s=wedge_timeout_s,
            progress_path_=progress_path(tmp_path),
        ),
        clock,
        exchange=None,
    )
    ticks = 0
    real_tick = rig.lifecycle.maintenance_tick

    async def counted_tick() -> None:
        nonlocal ticks
        await real_tick()
        ticks += 1

    rig.lifecycle.maintenance_tick = counted_tick  # type: ignore[method-assign]
    liveness = asyncio.create_task(app._liveness_loop())  # noqa: SLF001
    maintenance = asyncio.create_task(app._maintenance_loop(rig.lifecycle))  # noqa: SLF001
    verdicts: list[Any] = []
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
    assert ticks >= 4
    assert all(v is None for v in verdicts), f"false kill: {[v for v in verdicts if v]}"
    assert not (tmp_path / "KILL").exists()
    hist = app._progress.gap_histogram(LOOP_MAINTENANCE)  # noqa: SLF001
    assert hist is not None and hist.n >= ticks  # the passes were measured


async def test_a_store_timeout_during_adoption_is_a_failed_round_never_an_ok_read(
    tmp_path: Path,
) -> None:
    """Cancel-verification: the /portfolio/fills READ succeeds and holds the
    real execution, but the ledger lookup inside adoption
    (``has_fill_for_order_id``) hangs. Pre-fix: unbounded — the tick wedges.
    With the bound alone: a ``TimeoutError`` escaping the step would abort the
    rest of the tick, and if the read had been counted OK a final round could
    VOID a real fill. Now: one failed round (retried on cadence), the position
    stays counted, and the next round — store back — adopts the fill."""
    from tests.test_fill_cancel_verification import (
        COMBO_TICKER,
        VERIFY_DELAY_S,
        FakeFillsGetter,
        _cancel_reported,
        _verify_rig,
        taker_fill,
    )
    from tests.test_fill_recovery import FakeQuoteGetter

    getter = FakeQuoteGetter()
    fills = FakeFillsGetter()
    rig = await _verify_rig(tmp_path, getter=getter, fills=fills, attempts=1)
    fills.script(COMBO_TICKER, {"fills": [taker_fill()]})
    quote_id = await _cancel_reported(rig, getter)
    real_store = rig.lifecycle._store  # noqa: SLF001
    hanging = _HangingStore(real_store, "has_fill_for_order_id")
    rig.lifecycle._store = hanging  # type: ignore[assignment]  # noqa: SLF001
    rig.lifecycle._sub_step_bound_s_cb = lambda: 0.2  # noqa: SLF001

    await rig.lifecycle.maintenance_tick()  # round 1: read OK, ledger lookup times out
    await rig.lifecycle.drain_diagnostic_sweeps()
    assert hanging.calls == 1
    assert rig.metrics.counter("store.await_timeout.has_fill_for_order_id") == 1
    assert rig.metrics.counter("fill_recovery.verify_errors") == 1
    assert rig.metrics.counter("fill_recovery.verify_round_failed") == 1
    assert rig.metrics.counter("fill_recovery.cancelled") == 0  # never voided on it
    assert rig.metrics.counter("fill_recovery.late_execution") == 0
    assert f"fill:{quote_id}" in rig.exposure.positions  # fail-safe: kept
    assert rig.metrics.counter("maintenance.tick_ms") == 0  # (a histogram, not a counter)
    assert rig.metrics.quantile_ms("maintenance.tick_ms", 0.5) is not None  # tick completed

    rig.lifecycle._store = real_store  # noqa: SLF001 — the store answers again
    rig.h.clock.advance(VERIFY_DELAY_S + 0.5)
    await rig.lifecycle.maintenance_tick()  # round 2: adopts the real execution
    await rig.lifecycle.drain_diagnostic_sweeps()
    assert rig.metrics.counter("fill_recovery.late_execution") == 1
    assert await rig.store.count("fills") == 1
    assert f"fill:{quote_id}" in rig.exposure.positions
