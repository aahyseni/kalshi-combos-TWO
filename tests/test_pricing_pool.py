"""Phase-1 off-loop pricing: the wedge guarantee + exact off-loop parity.

Covers the contract that makes a cold combo unable to wedge the event loop:
  - _price_async with NO pool == sync _price (inline fallback, unchanged $).
  - _price_async through a pool that returns the joint == inline quote (same $).
  - a pool that blows the deadline (TimeoutError) DROPS the combo (fail-closed
    NoQuote, counted) instead of hanging — the loop keeps breathing.
  - a REAL ProcessPool computes the SAME JointEstimate as inline (the pure joint
    code, run in another process, is bit-identical).
"""
from __future__ import annotations

from pathlib import Path

from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import KalshiApiError
from combomaker.ops.config import PricingConfig
from combomaker.ops.persistence import Store
from combomaker.ops.pricing_pool import JointPool
from combomaker.pricing.engine import PricingEngine, _JointInputs
from combomaker.pricing.quote import ConstructedQuote
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, Rig, rfq
from tests.test_pricing_engine import seed_event


async def _make_rig(tmp_path: Path, name: str, joint_pool: object = None) -> Rig:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / f"{name}.sqlite3", h.clock)
    rig = Rig(h, store, joint_pool=joint_pool)
    rig.store = store  # type: ignore[attr-defined] - closed by the test to avoid a ResourceWarning
    return rig


class _InlineStubPool:
    """A JointPool stand-in whose run_joint computes the joint INLINE via the
    engine's own public entry — proves the price_offloaded plumbing is exact
    without spawning processes."""

    def __init__(self, engine: PricingEngine) -> None:
        self._engine = engine
        self.calls = 0

    async def run_joint(self, rfq_, beliefs, sides, relationship):  # noqa: ANN001, ANN201
        self.calls += 1
        return self._engine.compute_joint(rfq_, beliefs, sides, relationship)


class _TimeoutStubPool:
    """A JointPool stand-in that always blows the deadline."""

    async def run_joint(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise TimeoutError("simulated off-loop deadline breach")


async def test_price_async_inline_fallback_is_identical(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, "fallback")
    sync = rig.lifecycle._price(rfq())  # noqa: SLF001
    asyncq = await rig.lifecycle._price_async(rfq())  # noqa: SLF001
    assert isinstance(sync, ConstructedQuote) and isinstance(asyncq, ConstructedQuote)
    assert (int(asyncq.yes_bid_cc), int(asyncq.no_bid_cc), int(asyncq.fair_cc)) == (
        int(sync.yes_bid_cc), int(sync.no_bid_cc), int(sync.fair_cc)
    )
    await rig.store.close()  # type: ignore[attr-defined]


async def test_offloaded_quote_matches_inline_to_the_cent(tmp_path: Path) -> None:
    inline = await _make_rig(tmp_path, "inline")
    await inline.lifecycle.handle_rfq(rfq())
    assert len(inline.sender.created) == 1

    off = await _make_rig(tmp_path, "off")
    off.lifecycle._joint_pool = _InlineStubPool(off.lifecycle._engine)  # noqa: SLF001
    await off.lifecycle.handle_rfq(rfq())
    assert len(off.sender.created) == 1
    assert off.lifecycle._joint_pool.calls >= 1  # noqa: SLF001 - the pool path ran
    # Same $ on the wire, to the cent.
    assert (off.sender.created[0]["yes"], off.sender.created[0]["no"]) == (
        inline.sender.created[0]["yes"], inline.sender.created[0]["no"]
    )
    await inline.store.close()  # type: ignore[attr-defined]
    await off.store.close()  # type: ignore[attr-defined]


async def test_deadline_breach_drops_without_wedging(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, "deadline")
    rig.lifecycle._joint_pool = _TimeoutStubPool()  # noqa: SLF001
    await rig.lifecycle.handle_rfq(rfq())
    # Fail-closed: no quote reaches the sender, and the drop is counted.
    assert rig.sender.created == []
    assert rig.metrics.counter("price.pool_deadline_drop") == 1
    # And the decline is the pricing-failed reason (not a crash).
    priced = await rig.lifecycle._price_async(rfq())  # noqa: SLF001
    from combomaker.pricing.quote import NoQuote

    assert isinstance(priced, NoQuote)
    assert priced.reason is ReasonCode.SKIP_PRICE_DEADLINE  # deadline drop, NOT a failure
    await rig.store.close()  # type: ignore[attr-defined]


async def test_real_process_pool_joint_matches_inline(tmp_path: Path) -> None:
    rig = await _make_rig(tmp_path, "realpool")
    engine = rig.lifecycle._engine  # noqa: SLF001
    r = rfq()
    pre = engine._price_prefix(r)  # noqa: SLF001
    assert isinstance(pre, _JointInputs)
    inline_joint = engine.compute_joint(r, pre.beliefs, pre.sides, pre.relationship)

    pool = JointPool(PricingConfig(), TEST_CONVENTIONS, workers=1, deadline_s=30.0)
    pool.start()
    try:
        await pool.warmup()
        offloop_joint = await pool.run_joint(r, pre.beliefs, pre.sides, pre.relationship)
    finally:
        pool.shutdown()
    # JointEstimate is a frozen dataclass; the seeded MVN CDF is deterministic, so
    # the worker-process result is bit-identical to the inline one.
    assert offloop_joint == inline_joint
    assert pool.timeouts == 0 and pool.errors == 0
    await rig.store.close()  # type: ignore[attr-defined]


async def test_rfq_closed_is_graceful_not_a_failure(tmp_path: Path) -> None:
    """A 409 rfq_closed (we lost the taker race) must be a counted decline, never
    a propagating exception / traceback (P3b, 2026-07-14)."""
    rig = await _make_rig(tmp_path, "rfqclosed")

    async def _raise_closed(rfq_id, *, yes_bid_cc, no_bid_cc, rest_remainder=False):  # noqa: ANN001, ANN202
        raise KalshiApiError(409, "rfq_closed", "rfq closed", None)

    rig.sender.create_quote = _raise_closed  # type: ignore[assignment,method-assign]
    await rig.lifecycle.handle_rfq(rfq())  # must NOT raise
    assert rig.sender.created == []  # no quote landed
    assert rig.metrics.counter("quote.rfq_closed_before_post") == 1
    await rig.store.close()  # type: ignore[attr-defined]
