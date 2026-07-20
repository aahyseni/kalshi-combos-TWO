"""P2-2 (impl-order step 10): full-book MC OFF the event loop, generation-safe.

The portfolio-CVaR Monte Carlo is a full-book run (tens of thousands of correlated
samples, GIL-bound numpy). Run inline on the maintenance tick it can block the
event loop long enough that the supervisor heartbeat goes stale and the process is
emergency-killed under the RFQ firehose. P2-2 moves it into a WORKER PROCESS on the
IMMUTABLE ``BookModel`` and publishes the result only while it still describes the
CURRENT portfolio (generation-safe, reusing the P0-2 position-generation counter).

Two mandatory properties, both proved here against the real lifecycle wiring with a
controllable fake pool (no process-spawn flakiness — the LIFECYCLE's off-loop launch
+ generation-safe publish is what P2-2 adds; the worker just runs the same pure
``compute_book_risk`` proved elsewhere):

1. OLD-GENERATION MC results are DISCARDED — a snapshot whose ``input_generation``
   was superseded by a fill/settlement while the off-loop MC ran is never published.
2. A full-book MC run does NOT starve a maintenance/heartbeat beat — the event loop
   is not blocked while the off-loop MC computes (the tick launches it and returns).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.ops.config import FiltersConfig, PricingConfig
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.pricing_pool import BookRiskInputs, BookRiskPool
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle, _StaleBookRisk
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.sim.book_risk import BookRiskSnapshot, compute_book_risk
from combomaker.sim.within_game_rho import sgp_within_game_rho_provider
from tests.test_book_risk_wiring import _sgp_params
from tests.test_filters import Harness
from tests.test_lifecycle import TEST_CONVENTIONS, FakeSender
from tests.test_pricing_engine import seed_event
from tests.test_risk_shadow_mode import _FixedBankroll


@pytest.fixture()
async def harness(tmp_path: Path) -> tuple[Harness, Store]:
    h = Harness()
    await h.with_books(["M1", "M2"])
    h.with_meta("M1")
    h.with_meta("M2")
    h.with_meta("KXMVE-C1")
    seed_event(h, "E1", exclusive=True)
    seed_event(h, "E2", exclusive=True)
    store = await Store.open(tmp_path / "t.sqlite3", h.clock)
    return h, store


def _position(pid: str, *, contracts: int = 100, price_cc: int = 5_000) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=f"COMBO-{pid}",
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(contracts),
        entry_price_cc=CentiCents(price_cc),
        legs=(LegRef("M1", "E1", "yes"), LegRef("M2", "E1", "yes")),
    )


class _FakePool:
    """A stand-in for ``BookRiskPool``: ``run`` computes the snapshot in-process via
    the SAME pure ``compute_book_risk`` (so the numbers are real), but under an
    ``asyncio.Event`` gate the test releases when it wants — modelling a slow
    off-loop worker whose completion the test controls. ``await``ing ``run`` yields
    the event loop, exactly like the real ProcessPoolExecutor future does."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.gate.set()  # ungated by default; a test clears it to hold a run open
        self.started = asyncio.Event()  # set the instant a run begins
        self.calls = 0

    async def run(self, inputs: BookRiskInputs) -> BookRiskSnapshot:
        self.calls += 1
        self.started.set()
        await self.gate.wait()  # yields the loop until the test releases the run
        return compute_book_risk(
            inputs.model,
            n_samples=inputs.n_samples,
            seed=inputs.seed,
            band=inputs.band,
            bankroll_cc=inputs.bankroll_cc,
            structural_cfg=inputs.structural_cfg,
            current_equity_cc=inputs.current_equity_cc,
            ruin_floor_frac=inputs.ruin_floor_frac,
            input_generation=inputs.input_generation,
        )


def _build(
    h: Harness,
    store: Store,
    *,
    bankroll_cc: int,
    book_risk_pool: object,
    book_risk_stale_after_s: float = 1_000_000.0,
) -> tuple[QuoteLifecycle, ExposureBook]:
    exposure = ExposureBook(TEST_CONVENTIONS)
    engine = PricingEngine(h.feed, h.metadata, TEST_CONVENTIONS, PricingConfig())
    rfq_filter = RfqFilter(
        FiltersConfig(min_time_to_close_s=0.0).model_copy(
            update={"allowed_leg_series_prefixes": None}
        ),
        h.feed, h.metadata, h.killswitch, h.clock,
    )
    limits = LimitChecker(RiskLimits(caps_shadow_mode=False))
    lifecycle = QuoteLifecycle(
        clock=h.clock,
        sender=FakeSender(),
        engine=engine,
        rfq_filter=rfq_filter,
        limits=limits,
        exposure=exposure,
        feed=h.feed,
        metadata=h.metadata,
        inplay=InPlayDetector(h.clock),
        killswitch=h.killswitch,
        conventions=TEST_CONVENTIONS,
        store=store,
        metrics=Metrics(),
        lastlook_policy=LastLookPolicy(),
        config=LifecycleConfig(book_risk_stale_after_s=book_risk_stale_after_s),
        balance_tracker=_FixedBankroll(bankroll_cc),  # type: ignore[arg-type]
        start_time_provider=rfq_filter.leg_start_time,
        within_game_rho=sgp_within_game_rho_provider(_sgp_params()),  # type: ignore[arg-type]
        book_risk_pool=book_risk_pool,  # type: ignore[arg-type]
    )
    return lifecycle, exposure


# --------------------------------------------------------------------------- #
# Property 1: OLD-GENERATION off-loop MC results are DISCARDED.               #
# --------------------------------------------------------------------------- #


async def test_offloop_stale_generation_result_is_discarded(
    harness: tuple[Harness, Store],
) -> None:
    # An off-loop MC starts against a one-position book. WHILE it runs (gated open),
    # a second fill lands and bumps the position generation. When the MC finishes,
    # its snapshot describes the SUPERSEDED (pre-fill) portfolio, so the publisher
    # DISCARDS it — the lifecycle stores nothing, and the CVaR cap fails closed on
    # the (now larger) book until a fresh MC prices it.
    h, store = harness
    pool = _FakePool()
    pool.gate.clear()  # hold the run open so a fill can slip in mid-MC
    lifecycle, exposure = _build(h, store, bankroll_cc=100_000_000_000, book_risk_pool=pool)
    exposure.add_position(_position("fill-1"))
    gen_at_launch = exposure.position_generation

    task = asyncio.ensure_future(lifecycle.recompute_book_risk_offloop())
    await pool.started.wait()  # the MC has read its immutable inputs and is running

    # A SECOND fill supersedes the portfolio while the off-loop MC is in flight.
    exposure.add_position(_position("fill-2"))
    assert exposure.position_generation != gen_at_launch

    pool.gate.set()  # let the (now-stale) MC finish and try to publish
    await task

    # The stale-generation snapshot was DISCARDED: nothing published.
    assert lifecycle._book_risk is None
    assert lifecycle._book_risk_mono_ns is None
    # And the cap fails closed on the non-empty book (no usable snapshot).
    assert isinstance(lifecycle._book_risk_for_check(), _StaleBookRisk)


async def test_offloop_current_generation_result_is_published(
    harness: tuple[Harness, Store],
) -> None:
    # The mirror: when NO mutation intervenes, the off-loop snapshot IS published,
    # stamped with the generation it priced, and the cap reads the real snapshot.
    h, store = harness
    pool = _FakePool()
    lifecycle, exposure = _build(h, store, bankroll_cc=100_000_000_000, book_risk_pool=pool)
    exposure.add_position(_position("held"))

    await lifecycle.recompute_book_risk_offloop()

    snap = lifecycle._book_risk
    assert snap is not None
    assert snap.input_generation == exposure.position_generation
    assert lifecycle._book_risk_for_check() is snap
    assert pool.calls == 1


async def test_offloop_falls_back_to_inline_without_pool(
    harness: tuple[Harness, Store],
) -> None:
    # No pool wired ⇒ recompute_book_risk_offloop runs the MC inline (paper/tests),
    # producing an identical, published snapshot. The off-loop entrypoint is safe to
    # call in every mode.
    h, store = harness
    lifecycle, exposure = _build(h, store, bankroll_cc=100_000_000_000, book_risk_pool=None)
    exposure.add_position(_position("held"))
    await lifecycle.recompute_book_risk_offloop()
    assert lifecycle._book_risk is not None
    assert lifecycle._book_risk.input_generation == exposure.position_generation


# --------------------------------------------------------------------------- #
# Property 2: a full-book MC run does NOT starve a heartbeat/maintenance beat. #
# --------------------------------------------------------------------------- #


async def test_offloop_mc_does_not_block_the_event_loop(
    harness: tuple[Harness, Store],
) -> None:
    # The maintenance tick LAUNCHES the off-loop MC and returns IMMEDIATELY, so a
    # concurrent heartbeat-beat coroutine keeps ticking on its cadence while the MC
    # is in flight. If the MC blocked the loop (the pre-P2-2 inline behaviour), the
    # beater could not advance until the MC finished.
    h, store = harness
    pool = _FakePool()
    pool.gate.clear()  # keep the MC running until we explicitly release it
    lifecycle, exposure = _build(h, store, bankroll_cc=100_000_000_000, book_risk_pool=pool)
    exposure.add_position(_position("held"))

    beats = 0

    async def heartbeat_beater() -> None:
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0)  # yield: only advances if the loop is responsive

    beater = asyncio.ensure_future(heartbeat_beater())
    try:
        # The maintenance tick returns without awaiting the MC (fire-and-forget).
        lifecycle._maybe_recompute_book_risk()
        await pool.started.wait()  # the MC is now in flight (gated open)

        # The loop is FREE: let the beater run while the MC is blocked in the worker.
        beats_before = beats
        for _ in range(50):
            await asyncio.sleep(0)
        assert beats > beats_before, "heartbeat starved: event loop blocked by MC"

        # The in-flight task is the off-loop recompute, not yet done.
        assert lifecycle._book_risk_task is not None
        assert not lifecycle._book_risk_task.done()

        # Release the MC; its result now publishes (generation still current).
        pool.gate.set()
        await lifecycle._book_risk_task
        assert lifecycle._book_risk is not None
        assert lifecycle._book_risk.input_generation == exposure.position_generation
    finally:
        beater.cancel()


async def test_maybe_recompute_is_single_flight(
    harness: tuple[Harness, Store],
) -> None:
    # While an off-loop MC is in flight, a second maintenance tick must NOT launch a
    # redundant overlapping MC (single-flight guard) — the throttle window is not
    # even consulted; the running task simply keeps the slot.
    h, store = harness
    pool = _FakePool()
    pool.gate.clear()
    lifecycle, exposure = _build(h, store, bankroll_cc=100_000_000_000, book_risk_pool=pool)
    exposure.add_position(_position("held"))

    lifecycle._maybe_recompute_book_risk()
    await pool.started.wait()
    first_task = lifecycle._book_risk_task

    # A second tick while the first run is in flight: no new launch.
    lifecycle._maybe_recompute_book_risk()
    assert lifecycle._book_risk_task is first_task
    assert pool.calls == 1

    pool.gate.set()
    await first_task


async def test_real_book_risk_pool_roundtrips(
    harness: tuple[Harness, Store],
) -> None:
    # End-to-end with the REAL ProcessPoolExecutor-backed BookRiskPool: the off-loop
    # snapshot equals the inline one (same immutable model, same seed) to the cent,
    # and it is published under the current generation. Proves the process boundary
    # is picklable + deterministic (byte-identical seeded MVN across processes).
    h, store = harness
    pool = BookRiskPool(workers=1)
    pool.start()
    try:
        lifecycle, exposure = _build(
            h, store, bankroll_cc=100_000_000_000, book_risk_pool=pool
        )
        exposure.add_position(_position("held"))
        # Inline reference.
        lifecycle.recompute_book_risk()
        inline = lifecycle._book_risk
        assert inline is not None
        # Off-loop, same book.
        await lifecycle.recompute_book_risk_offloop()
        offloop = lifecycle._book_risk
        assert offloop is not None
        assert offloop.input_generation == exposure.position_generation
        assert offloop.governing_model_es_99_cc == inline.governing_model_es_99_cc
        assert offloop.deterministic_max_loss_cc == inline.deterministic_max_loss_cc
        assert offloop.p_ruin == inline.p_ruin
    finally:
        pool.shutdown()
