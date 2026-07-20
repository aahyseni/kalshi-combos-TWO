"""F2 mid-pipeline RFQ liveness checks (throughput synthesis 2026-07-16).

An RFQ deleted while it sits in our queue / pricing pool / risk checks must
stop consuming pipeline work at the next check point — dequeue ("pre_price"),
after the joint returns ("post_price"), and immediately before the POST
("pre_post") — each with its own metric and the shared
``skip_rfq_deleted_midflight`` reason. Strictly additive: with no liveness view
(or a broken probe) behaviour is byte-identical to today, and a LIVE RFQ's
quote is cent-identical with and without the view.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from combomaker.ops.persistence import Store
from combomaker.rfq.intake import RfqIntake
from tests.test_feed import FakeWs
from tests.test_filters import Harness
from tests.test_lifecycle import Rig, rfq
from tests.test_pricing_engine import seed_event

LIVENESS_REASON = "skip_rfq_deleted_midflight"


async def make_rig(tmp_path: Path, rfq_alive: Any, name: str = "t.sqlite3") -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / name, h.clock)
    return Rig(h, store, rfq_alive=rfq_alive)


def probe_seq(*answers: bool) -> Any:
    """A liveness probe that returns the given answers in order (then False)."""
    seq = list(answers)

    def probe(_rfq_id: str) -> bool:
        return seq.pop(0) if seq else False

    return probe


def count_pricing(rig: Rig) -> dict[str, int]:
    """Instrument the rig's ``_price_async`` so tests can pin how many times
    the expensive pricing step actually ran."""
    calls = {"n": 0}
    orig = rig.lifecycle._price_async  # noqa: SLF001

    async def counting(rfq_: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return await orig(rfq_, **kw)

    rig.lifecycle._price_async = counting  # type: ignore[method-assign]  # noqa: SLF001
    return calls


async def test_no_liveness_view_behaviour_unchanged(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, rfq_alive=None)
    await rig.lifecycle.handle_rfq(rfq())
    assert len(rig.sender.created) == 1
    assert rig.metrics.counter("rfq.liveness_skip.pre_price") == 0


async def test_dead_on_dequeue_skips_before_any_pricing(tmp_path: Path) -> None:
    rig = await make_rig(tmp_path, rfq_alive=lambda _rfq_id: False)
    calls = count_pricing(rig)
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []          # never reached the POST
    assert calls["n"] == 0                   # never reached pricing either
    assert rig.metrics.counter("rfq.liveness_skip.pre_price") == 1
    store = rig.lifecycle._store  # noqa: SLF001
    assert (await store.decision_reason_counts()).get(LIVENESS_REASON) == 1


async def test_deleted_during_pricing_skips_post_price(tmp_path: Path) -> None:
    # Alive at dequeue, gone once the joint returns — the pool-dwell race.
    rig = await make_rig(tmp_path, rfq_alive=probe_seq(True, False))
    calls = count_pricing(rig)
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []
    assert calls["n"] == 1                   # pricing DID run; POST did not
    assert rig.metrics.counter("rfq.liveness_skip.post_price") == 1
    assert rig.metrics.counter("rfq.liveness_skip.pre_price") == 0


async def test_deleted_just_before_post_skips_pre_post(tmp_path: Path) -> None:
    # Survives dequeue + pricing, dies between the risk check and the POST.
    rig = await make_rig(tmp_path, rfq_alive=probe_seq(True, True, False))
    await rig.lifecycle.handle_rfq(rfq())
    assert rig.sender.created == []
    assert rig.metrics.counter("rfq.liveness_skip.pre_post") == 1
    assert rig.lifecycle.open_quote_count == 0


async def test_adversarial_probe_error_proceeds_exactly_as_today(
    tmp_path: Path,
) -> None:
    # ADVERSARIAL EDGE: a broken probe must never become a quote blackout —
    # unknown liveness proceeds (money-safe: a genuinely dead RFQ still cannot
    # fill; the POST-time rfq_closed handling is the backstop).
    def broken(_rfq_id: str) -> bool:
        raise RuntimeError("liveness registry exploded")

    rig = await make_rig(tmp_path, rfq_alive=broken)
    await rig.lifecycle.handle_rfq(rfq())
    assert len(rig.sender.created) == 1      # quoted exactly as with no view
    store = rig.lifecycle._store  # noqa: SLF001
    assert LIVENESS_REASON not in await store.decision_reason_counts()


async def test_ws_disconnect_mid_pipeline_does_not_skip_live_rfq(
    tmp_path: Path,
) -> None:
    """Risk audit fix 2026-07-16: a comms-WS drop between enqueue and dequeue
    CLEARS the intake registry, but that is UNKNOWN, not positive deletion —
    wired via ``intake.rfq_alive`` (the exact quote_app wiring), the lifecycle
    must still price + POST the RFQ (the REST POST needs no WS), never
    mislabel it ``skip_rfq_deleted_midflight``."""
    ws = FakeWs()
    intake = RfqIntake(ws)
    r = rfq()
    intake.open_rfqs[r.rfq_id] = r          # rfq_created landed pre-drop
    rig = await make_rig(tmp_path, rfq_alive=intake.rfq_alive)
    await ws.drop_connection()              # blip BEFORE the worker dequeues
    await rig.lifecycle.handle_rfq(r)
    assert len(rig.sender.created) == 1     # quoted, not liveness-skipped
    assert rig.metrics.counter("rfq.liveness_skip.pre_price") == 0
    store = rig.lifecycle._store  # noqa: SLF001
    assert LIVENESS_REASON not in await store.decision_reason_counts()


async def test_positively_deleted_rfq_still_skips_after_disconnect(
    tmp_path: Path,
) -> None:
    # The F2 gate keeps its teeth through a reconnect: a POSITIVE rfq_deleted
    # (here: arriving after the drop) still liveness-skips at dequeue.
    ws = FakeWs()
    intake = RfqIntake(ws)
    r = rfq()
    intake.open_rfqs[r.rfq_id] = r
    rig = await make_rig(tmp_path, rfq_alive=intake.rfq_alive)
    await ws.drop_connection()
    await ws.deliver(
        {"type": "rfq_deleted", "sid": 1, "msg": {"id": r.rfq_id, "deleted_ts": "t"}}
    )
    await rig.lifecycle.handle_rfq(r)
    assert rig.sender.created == []
    assert rig.metrics.counter("rfq.liveness_skip.pre_price") == 1


async def test_live_rfq_quote_cent_identical_with_and_without_view(
    tmp_path: Path,
) -> None:
    # Additivity to the cent: a LIVE RFQ prices identically whether or not the
    # liveness view is wired (the gate touches no pricing/risk math).
    rig_none = await make_rig(tmp_path, rfq_alive=None, name="a.sqlite3")
    rig_live = await make_rig(tmp_path, rfq_alive=lambda _r: True, name="b.sqlite3")
    await rig_none.lifecycle.handle_rfq(rfq())
    await rig_live.lifecycle.handle_rfq(rfq())
    assert len(rig_none.sender.created) == len(rig_live.sender.created) == 1
    assert rig_none.sender.created[0]["yes"] == rig_live.sender.created[0]["yes"]
    assert rig_none.sender.created[0]["no"] == rig_live.sender.created[0]["no"]
