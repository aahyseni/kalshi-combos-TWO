"""HONEST DEADLINE ANCHOR for the accept→confirm path (2026-07-31 double halt).

The exchange's 3.0s confirm window opens at the taker's accept — upstream of
every queue in this process. The old anchor (handler start) silently granted
the derived confirm budget time the exchange had already spent: on all 12
'expired' confirms ever taped, in-handler time was <= 1.14s, i.e. >= 1.86s of
the window died before the handler's first instruction. The WS read loop now
stamps accept frames off the socket (priority lane), the intake passes the
stamp through, and ``_on_quote_accepted`` anchors its budget there.

Also proven here: the FAST-LANE CREATE RACE — with accepts jumping the
dispatch backlog, an accept can beat our own create POST's response parse; the
handler waits exactly the measured p99 of that same REST verb once (never a
typed number) before treating the quote as unknown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from combomaker.ops.persistence import Store
from tests.test_filters import Harness
from tests.test_lifecycle import Rig, accepted_msg, rfq
from tests.test_pricing_engine import seed_event


@pytest.fixture()
async def lifecycle_rig(tmp_path: Path) -> Rig:
    # Same shape as test_lifecycle's ``rig`` fixture (a fixture cannot be
    # imported across modules without shadowing its own name).
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "anchor.sqlite3", h.clock)
    return Rig(h, store)


async def test_accept_anchor_uses_wire_receive_stamp(lifecycle_rig: Rig) -> None:
    await lifecycle_rig.lifecycle.handle_rfq(rfq())
    stamp = lifecycle_rig.h.clock.monotonic_ns()
    lifecycle_rig.h.clock.advance(2.0)  # 2s of simulated dispatch-queue delay
    msg = accepted_msg("q1", "yes")
    msg["_ws_recv_mono_ns"] = stamp
    await lifecycle_rig.lifecycle.on_quote_accepted(msg)
    assert lifecycle_rig.sender.confirmed == ["q1"]
    # The dispatch delay is measured (the tape could never split this out)...
    assert lifecycle_rig.metrics.histogram_max_ms(
        "confirm.accept_dispatch_delay_ms"
    ) == pytest.approx(2000.0)
    # ...and the decision clock is anchored at the WIRE receive, not handler
    # start: everything the queue ate counts against the confirm budget.
    decision_ms = lifecycle_rig.metrics.histogram_max_ms("confirm.decision_ms")
    assert decision_ms is not None and decision_ms >= 2000.0


async def test_accept_anchor_without_stamp_is_handler_start(lifecycle_rig: Rig) -> None:
    # No stamp (REST-reconciled path, tests, replay): byte-identical old
    # behavior — anchor at handler start, no delay series observed.
    await lifecycle_rig.lifecycle.handle_rfq(rfq())
    await lifecycle_rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    assert lifecycle_rig.sender.confirmed == ["q1"]
    assert lifecycle_rig.metrics.histogram_max_ms("confirm.accept_dispatch_delay_ms") is None


async def test_accept_anchor_ignores_foreign_or_future_stamp(lifecycle_rig: Rig) -> None:
    # A stamp from another clock domain (> our monotonic now) must never GROW
    # the budget's view of the window — it is ignored, not clamped.
    await lifecycle_rig.lifecycle.handle_rfq(rfq())
    msg = accepted_msg("q1", "yes")
    msg["_ws_recv_mono_ns"] = lifecycle_rig.h.clock.monotonic_ns() + 10**12
    await lifecycle_rig.lifecycle.on_quote_accepted(msg)
    assert lifecycle_rig.sender.confirmed == ["q1"]
    assert lifecycle_rig.metrics.histogram_max_ms("confirm.accept_dispatch_delay_ms") is None


async def test_fast_lane_accept_beats_create_ack_then_confirms(lifecycle_rig: Rig) -> None:
    # The accept arrives BEFORE our create POST's response has populated
    # ``_open`` (possible only now that accepts jump the dispatch backlog).
    # The handler waits the measured create-RTT p99 once, then finds the quote
    # and confirms — the auction is banked, not silently lapsed.
    lifecycle_rig.metrics.observe_ms("quote.create_rtt_ms", 50.0)
    task = asyncio.create_task(
        lifecycle_rig.lifecycle.on_quote_accepted(accepted_msg("q1", "yes"))
    )
    await asyncio.sleep(0.005)
    assert not task.done()  # inside the measured wait, not yet dropped
    await lifecycle_rig.lifecycle.handle_rfq(rfq())  # create ack lands: _open["q1"]
    await asyncio.wait_for(task, timeout=2.0)
    assert lifecycle_rig.sender.confirmed == ["q1"]
    assert lifecycle_rig.metrics.counter("confirm.accept_beat_create_ack") == 1


async def test_accept_for_truly_unknown_quote_still_lapses(lifecycle_rig: Rig) -> None:
    # Cold create-RTT series (nothing ever created) ⇒ no wait at all — the
    # pre-fast-lane behavior, and never a typed fallback delay.
    await lifecycle_rig.lifecycle.on_quote_accepted(accepted_msg("zzz", "yes"))
    assert lifecycle_rig.sender.confirmed == []
    assert lifecycle_rig.metrics.counter("confirm.accept_beat_create_ack") == 0
