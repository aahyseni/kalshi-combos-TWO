"""V8/V9 — SUPERVISION COVERAGE and PROCESS HYGIENE. The two caught PRE-LIVE.

  R7 SUPERVISOR DEMOTION (caught pre-live 2026-07-27). Removing the liveness axis
     left the STARTUP WINDOW uncovered, because ``ProgressReader`` deliberately
     does not latch on an empty ledger ("not published yet"). That window SPANS
     THE STARTUP RECONCILE — precisely when leftover quotes are known to be
     resting. Every per-axis assertion was correct; the hole was in the UNION.
     GENERALISABLE RULE: removing an axis is a change to a UNION. Assert the
     union's coverage, never the survivor's behaviour.

  R6 AUTO-RELIGHTER (caught pre-live 2026-07-27). Two defects: a boot-wedge
     branch returned without ``terminate()``, abandoning a LIVE child; and it
     reused ``supervisor.heartbeat_timeout_s`` as a BOOT budget when the MEASURED
     launch->first-beat time is 33.6-48.3 s across every recorded boot — not one
     is inside the 30 s anchor, so it would have fired on EVERY launch. Every
     fake-child test passed while production was broken.

The bound here is not a number, it is a LATCH: consult the anchor only after the
child has been SEEN beating. The gate replays the measured boot distribution and
requires ZERO false abandonments.
"""

from __future__ import annotations

import ast
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from combomaker.ops import relight as relight_mod
from tools.vitals.result import FAST, Probe, Result, Vital

V8 = Vital(
    key="V8",
    incident="R7 supervisor demotion left the startup window uncovered "
    "(caught pre-live 2026-07-27)",
    name="SUPERVISION COVERAGE — the UNION of kill axes has no None-hole",
    degraded="the exact startup window: empty (unlatched) loop_progress.json, "
    "stale/absent heartbeat, leftover quotes RESTING on the exchange",
    money="a dead bot's quotes keep resting and keep filling with nobody home; "
    "no KILL, no needs_reconcile, unbounded adverse fills",
    tier=FAST,
)

V9 = Vital(
    key="V9",
    incident="R6 auto-relighter orphan + boot budget (caught pre-live 2026-07-27)",
    name="PROCESS HYGIENE — no orphaned child, no false boot-wedge",
    degraded="every recorded boot's measured launch->first-beat delay, for BOTH "
    "launch shapes (START_BOT deletes the heartbeat; a relight inherits a warm "
    "or stale corpse)",
    money="the relighter abandons a HEALTHY child mid-boot and exits, leaving a "
    "LIVE trading process with no parent and no supervisor — strictly worse "
    "than having no relighter at all",
    tier=FAST,
)


# --------------------------------------------------------------------------- #
# V8 — sweep the union across the whole lifecycle timeline.
# --------------------------------------------------------------------------- #


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def advance(self, s: float) -> None:
        self.t += s

    def monotonic_ns(self) -> int:
        return int(self.t * 1e9)

    def now(self) -> Any:
        from datetime import UTC, datetime

        return datetime(2026, 7, 27, 12, 0, tzinfo=UTC) + timedelta(seconds=self.t)


def check_supervision_coverage(probe: Probe, d: Any) -> Result:
    from combomaker.ops.supervisor import SafetySupervisor, SupervisorConfig
    from combomaker.risk.heartbeat import Heartbeat
    from combomaker.risk.progress import ProgressLedger, progress_path

    # The window's EXTENT is measured, not typed: the longest recorded
    # launch->first-beat, which is exactly how long the bot can sit with an
    # empty ledger and no beat while its startup reconcile runs.
    window_s = max(d.boot_delays_s)
    holes: list[str] = []
    samples = 0

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        clock = _Clock()
        cfg = SupervisorConfig(
            heartbeat_path=tmp / "heartbeat.txt",
            kill_file=tmp / "KILL",
            reconcile_marker_path=tmp / "needs_reconcile",
            heartbeat_timeout_s=d.heartbeat_timeout_s,
            poll_interval_s=1.0,
            write_budget_capacity=d.write_budget_capacity,
            write_budget_refill_s=d.write_budget_refill_s,
            progress_path_=progress_path(tmp),
        )
        sup = SafetySupervisor(cfg, clock, exchange=None)  # type: ignore[arg-type]

        # PHASE 1 — startup. quote_app publishes exactly an EMPTY ledger at
        # preflight (quote_app.py:2125,:3554) before any register(), and
        # start_all.ps1 DELETES heartbeat.txt. The bot is DEAD from t=0; every
        # sample in this window must return a kill reason.
        ledger = ProgressLedger(clock, progress_path(tmp))  # type: ignore[arg-type]
        ledger.publish()  # empty: the un-latchable snapshot
        step = max(1.0, window_s / 20.0)
        t = 0.0
        while t <= window_s + step:
            clock.advance(step)
            ledger.publish()  # the empty ledger keeps being refreshed
            verdict = sup.wedged_detail()
            samples += 1
            if verdict is None and clock.t > d.heartbeat_timeout_s:
                holes.append(f"startup t={clock.t:.0f}s (empty ledger, no beat)")
            t += step
        probe.note(
            f"startup window swept to {window_s:.0f}s (longest recorded boot) in "
            f"{step:.0f}s steps — empty unlatched ledger + absent heartbeat"
        )

        # PHASE 2 — a HEALTHY established bot must NOT be killed (no false
        # positive, which is the whole reason the liveness axis was demoted).
        beat = Heartbeat(clock, cfg.heartbeat_path)
        beat.beat()
        ledger.register("quote", interval_s=0.5, wedge_timeout_s=d.heartbeat_timeout_s)
        ledger.mark("quote")
        ledger.publish()
        healthy = sup.wedged_detail()
        samples += 1
        false_kill = healthy is not None
        probe.note(f"healthy established bot -> {healthy!r}")

        # PHASE 3 — established, then the loop stops advancing. Must kill, and
        # must NAME the loop.
        clock.advance(d.heartbeat_timeout_s * 2)
        ledger.publish()  # refreshed file, stale loop
        stalled = sup.wedged_detail()
        samples += 1
        if stalled is None:
            holes.append("post-registration stalled loop read HEALTHY")
        probe.note(f"stalled loop after establishment -> {stalled!r}")

    ok = not holes and not false_kill
    return probe.grade(
        ok,
        measured=f"{len(holes)} None-holes over {samples} sampled instants; "
        f"false-kill on a healthy bot = {false_kill}",
        bound=f"zero None-holes anywhere the bot is dead, across [0, "
        f"{window_s:.0f}s] (longest measured boot) and after establishment; "
        f"zero kills on a healthy bot",
        detail="; ".join(holes) if holes else ("healthy bot was killed" if false_kill else ""),
    )


# --------------------------------------------------------------------------- #
# V9 — process hygiene: a structural invariant plus the measured replay.
# --------------------------------------------------------------------------- #


def _orphan_invariant() -> list[str]:
    """STRUCTURAL, in the style of ``test_sender_delete_quote_has_exactly_one_call
    _site``: inside ``Relighter.run``, once ``self._spawn(...)`` has produced a
    live child, no statement may ``return`` unless the child is provably not
    running. An unmetered orphan must be UNWRITABLE, not merely discouraged."""
    src = Path(relight_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    run = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "run"
        ),
        None,
    )
    if run is None:
        return ["Relighter.run not found — the invariant cannot be checked"]
    spawn_line = min(
        (
            n.lineno
            for n in ast.walk(run)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_spawn"
        ),
        default=None,
    )
    if spawn_line is None:
        return ["no self._spawn call inside Relighter.run"]
    # Every return AFTER the spawn must be reached only once the child has
    # exited. The one legal shape is a return that follows _watch() (which polls
    # to exit) — anything else abandons a live process.
    watch_lines = [
        n.lineno
        for n in ast.walk(run)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("_watch", "terminate", "kill")
    ]
    bad: list[str] = []
    for node in ast.walk(run):
        if isinstance(node, ast.Return) and node.lineno > spawn_line:
            if not any(w < node.lineno for w in watch_lines):
                bad.append(
                    f"relight.py:{node.lineno} returns after the spawn with no "
                    f"_watch/terminate before it — a LIVE child is abandoned"
                )
    return bad


def check_process_hygiene(probe: Probe, d: Any) -> Result:
    from tests.test_auto_relight import (
        HALT_RECEIPT_FILENAME,
        Relighter,
        _StepClock,
        _synthetic_receipt,
    )

    structural = _orphan_invariant()
    probe.note(
        f"orphan invariant over relight.py: "
        f"{'clean' if not structural else '; '.join(structural)}"
    )

    # THE MEASURED REPLAY. Every recorded boot's launch->first-beat delay, for
    # both launch shapes. Zero false abandonments is the bound.
    abandoned: list[str] = []
    shapes = (("start_bot_deletes_heartbeat", None), ("relight_warm_corpse", 10.0),
              ("relight_stale_corpse", 45.0))
    delays = sorted(d.boot_delays_s)
    for label, corpse_age in shapes:
        for delay in delays:
            with tempfile.TemporaryDirectory() as td:
                data = Path(td) / "data"
                data.mkdir()
                clock = _StepClock()
                (data / "needs_reconcile").write_text("halt_metadata_change", "utf-8")
                if corpse_age is not None:
                    (data / "heartbeat.txt").write_text(
                        (clock.now() - timedelta(seconds=corpse_age)).isoformat(),
                        encoding="utf-8",
                    )
                reconcile_s = max(0.0, delay - 1.2)

                def _child(_argv: Any, _env: Any, *, _d=data, _c=clock,
                           _first=delay, _rec=reconcile_s) -> Any:
                    class _Child:
                        pid = 4242

                        def poll(self) -> int | None:
                            _c.t += 1.0
                            if _c.t >= _rec:
                                (_d / "needs_reconcile").unlink(missing_ok=True)
                            if _c.t >= _first:
                                (_d / "heartbeat.txt").write_text(
                                    _c.now().isoformat(), encoding="utf-8"
                                )
                                (_d / "loop_progress.json").write_text(
                                    json.dumps(
                                        {
                                            "written_at": _c.now().isoformat(),
                                            "loops": {
                                                "maintenance": {
                                                    "age_s": 0.1,
                                                    "stall_after_s": 30.5,
                                                }
                                            },
                                        }
                                    ),
                                    encoding="utf-8",
                                )
                            if _c.t >= _first + 120.0:
                                (_d / HALT_RECEIPT_FILENAME).write_text(
                                    json.dumps(
                                        _synthetic_receipt(run_id="R", pid=4242, ppid=4242)
                                    ),
                                    encoding="utf-8",
                                )
                                return 0
                            return None

                    return _Child()

                relighter = Relighter(
                    data_dir=data,
                    kill_file=Path(td) / "KILL",
                    child_argv=["python"],
                    wedge_timeout_s=d.heartbeat_timeout_s,  # the operator anchor
                    clock=clock,  # type: ignore[arg-type]
                    spawn=_child,
                    sleep=lambda s: None,
                )
                code, cost_s, work_s = relighter._watch(  # noqa: SLF001
                    relighter._spawn([], {}), 0.0, relighter._heartbeat_stamp()  # noqa: SLF001
                )
                if code != 0 or cost_s is None or work_s is None:
                    abandoned.append(f"{label}@{delay:.1f}s (exit={code}, cost={cost_s})")

    probe.note(
        f"replayed {len(delays)} recorded boots x {len(shapes)} launch shapes = "
        f"{len(delays) * len(shapes)} runs; launch->first beat "
        f"{min(delays):.1f}s-{max(delays):.1f}s vs the "
        f"{d.heartbeat_timeout_s:.0f}s operator anchor "
        f"({sum(1 for x in delays if x > d.heartbeat_timeout_s)} boots are OUTSIDE it)"
    )
    ok = not structural and not abandoned
    return probe.grade(
        ok,
        measured=f"{len(abandoned)} healthy boots abandoned; "
        f"{len(structural)} orphaning return paths",
        bound=f"zero abandonments over the measured boot distribution "
        f"({len(delays)} boots x {len(shapes)} shapes); zero returns past the "
        f"spawn without a _watch/terminate",
        detail="; ".join(structural + abandoned[:4]),
    )


CHECKS = [(V8, check_supervision_coverage), (V9, check_process_hygiene)]
