"""Off-loop joint pricing via a ProcessPoolExecutor (2026-07-14 throughput fix,
Phase 1 — the wedge guarantee).

WHY: a single cold combo can cost multiple seconds of GIL-bound CPU in the copula
MVN CDF / Dixon-Coles invert. Run inline on the event loop (as price() is today)
that stalls the heartbeat and the WS pongs, and the supervisor emergency-kills the
process (04:20 UTC 2026-07-14: 15.4s wedge → kill). Threads cannot help (GIL), so
the ONLY structural fix is to run the pure joint computation in a separate PROCESS
and bound it with a deadline, so the loop never blocks on CPU.

WHAT: the parent event loop does the cheap prefix (classify + beliefs) and the memo
check; only a genuine MISS is shipped to a worker. The worker runs the engine's OWN
``compute_joint`` (no reimplementation — hard rule 8) on a per-process engine built
once from the shipped config, and returns the JointEstimate/NoQuote. Identical to
the inline result (same pure code, seeded MVN CDF ⇒ deterministic across processes)
— proven by tools/pool_parity_check.py. A miss that exceeds the deadline is
abandoned (the loop moves on and the RFQ is dropped — combos are one-shot and
re-RFQ'd); the worker finishes and frees itself.

Only PRIMITIVE, picklable values cross the boundary: the Rfq, the per-leg beliefs,
the sides, and the (frozen) Relationship — never a feed/metadata/engine object.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from combomaker.core.conventions import Conventions
from combomaker.ops.config import PricingConfig
from combomaker.ops.logging import get_logger
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import NoQuote
from combomaker.pricing.relationships import Relationship
from combomaker.rfq.models import Rfq
from combomaker.sim.book_model import BookModel
from combomaker.sim.book_risk import BookRiskSnapshot, compute_book_risk
from combomaker.sim.structural_book import StructuralConfigView

log = get_logger(__name__)

# Per-worker-process singleton engine, built once in the pool initializer. The
# joint path never touches feed/metadata (verified), so the stubs below are never
# called — they exist only to satisfy construction and to LOUDLY fail if the joint
# path ever grows a feed/metadata dependency (which would silently mis-price).
_WORKER_ENGINE: PricingEngine | None = None


class _StubFeed:
    def book(self, ticker: str) -> object:  # pragma: no cover - must never run
        raise RuntimeError("worker engine feed must not be used on the joint path")


class _StubMetadata:
    def peek(self, ticker: str) -> object:  # pragma: no cover - must never run
        raise RuntimeError("worker engine metadata must not be used on the joint path")


def _pool_init(config: PricingConfig, conventions: Conventions) -> None:
    """Runs once per worker process (spawn-safe). Builds the per-process engine
    from the SAME shipped config so its structural pricer / SGP params / longshot
    floor are byte-for-byte the loop's. Worker memo disabled (maxsize=0): the loop
    owns the authoritative cache and only ever ships misses here."""
    global _WORKER_ENGINE
    _WORKER_ENGINE = PricingEngine(
        _StubFeed(),  # type: ignore[arg-type]
        _StubMetadata(),  # type: ignore[arg-type]
        conventions,
        config,
        joint_memo_maxsize=0,
    )


def _worker_joint(
    rfq: Rfq,
    beliefs: list[LegBelief],
    sides: list[str],
    relationship: Relationship,
) -> JointEstimate | NoQuote:
    """The function the pool runs. Reuses the engine's public compute_joint."""
    assert _WORKER_ENGINE is not None, "pool worker used before _pool_init ran"
    return _WORKER_ENGINE.compute_joint(rfq, beliefs, sides, relationship)


class JointPool:
    """Manages the ProcessPoolExecutor and exposes a deadline-bounded async
    ``run_joint`` that plugs straight into PricingEngine.price_offloaded."""

    def __init__(
        self,
        config: PricingConfig,
        conventions: Conventions,
        *,
        workers: int = 2,
        deadline_s: float = 0.8,
    ) -> None:
        self._config = config
        self._conventions = conventions
        self._workers = max(1, workers)
        self._deadline_s = deadline_s
        self._executor: ProcessPoolExecutor | None = None
        self.timeouts = 0
        self.errors = 0
        self.calls = 0

    def start(self) -> None:
        self._executor = ProcessPoolExecutor(
            max_workers=self._workers,
            initializer=_pool_init,
            initargs=(self._config, self._conventions),
        )
        log.info("joint_pool_started", workers=self._workers, deadline_s=self._deadline_s)

    async def warmup(self) -> None:
        """Force every worker to spawn + import scipy/numpy + build its engine
        BEFORE live traffic, so the first real off-loop price doesn't eat a
        cold-import tail. Submits N trivial probes concurrently."""
        if self._executor is None:
            return
        loop = asyncio.get_running_loop()
        probes = [
            loop.run_in_executor(self._executor, _warm_probe) for _ in range(self._workers)
        ]
        try:
            await asyncio.wait_for(asyncio.gather(*probes), timeout=30.0)
            log.info("joint_pool_warm", workers=self._workers)
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            log.warning("joint_pool_warmup_failed", error=repr(exc))

    async def run_joint(
        self,
        rfq: Rfq,
        beliefs: list[LegBelief],
        sides: list[str],
        relationship: Relationship,
    ) -> JointEstimate | NoQuote:
        """Run the joint step in a worker, bounded by the deadline. On timeout the
        loop stops waiting (the worker keeps running until done, then frees itself)
        and the miss propagates as a TimeoutError for the caller to drop. This is
        the guarantee that CPU can never wedge the loop."""
        if self._executor is None:
            raise RuntimeError("JointPool.run_joint before start()")
        self.calls += 1
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(
            self._executor, _worker_joint, rfq, beliefs, sides, relationship
        )
        try:
            return await asyncio.wait_for(fut, timeout=self._deadline_s)
        except TimeoutError:
            self.timeouts += 1
            raise
        except Exception:
            self.errors += 1
            raise

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
            log.info(
                "joint_pool_stopped",
                calls=self.calls,
                timeouts=self.timeouts,
                errors=self.errors,
            )


def _warm_probe() -> bool:  # pragma: no cover - trivial worker probe
    return _WORKER_ENGINE is not None


# --------------------------------------------------------------------------- #
# P2-2: full-book MC off the event loop, generation-safe.                     #
# --------------------------------------------------------------------------- #
#
# WHY: recompute_book_risk() runs a full portfolio Monte Carlo (tens of thousands
# of correlated samples over the whole book — GIL-bound numpy). Run inline on the
# maintenance tick (as it was), under the RFQ firehose a large book's MC blocks
# the event loop for long enough that the supervisor heartbeat goes stale and the
# process is emergency-killed (the same starvation class as the inline joint
# wedge). Threads cannot help (GIL); the structural fix is to run the pure MC in a
# separate PROCESS so the loop keeps beating the heartbeat while it computes.
#
# WHAT: the parent event loop does the cheap on-loop prefix — read the POSITION
# generation (P0-2) + build the IMMUTABLE BookModel — and ships only that frozen
# model + the scalar params to a worker. The worker runs the engine's OWN
# compute_book_risk (no reimplementation — hard rule 8) and returns the
# BookRiskSnapshot, stamped with the input_generation the parent captured. The
# parent PUBLISHES the snapshot only while the book's live position generation
# still equals that stamp; a snapshot computed against a portfolio that a
# fill/settlement/reconciliation/reservation has since mutated is DISCARDED (never
# gates a stale book). Determinism: compute_book_risk takes an explicit seed, so a
# seeded run is byte-identical across processes — the off-loop snapshot equals the
# inline one on the same immutable model.
#
# Only PICKLABLE, immutable values cross the boundary: the frozen BookModel (leg
# tuple + numpy corr matrices + primitive dicts), the StructuralConfigView, and
# scalar ints/floats — never a live ExposureBook, marginal provider, or engine.


@dataclass(frozen=True, slots=True)
class BookRiskInputs:
    """The IMMUTABLE inputs one off-loop book-risk MC run reads.

    ``model`` is the frozen ``BookModel`` (built on-loop from a generation-stamped
    read of the positions). ``input_generation`` is the ``ExposureBook``
    position generation captured at that read; the returned snapshot carries it so
    the publisher can discard a result whose generation has since been superseded
    (P0-2). Everything here is picklable and frozen, so shipping it to a worker
    process cannot race a concurrent book mutation."""

    model: BookModel
    n_samples: int
    seed: int
    band: str
    bankroll_cc: int | None
    structural_cfg: StructuralConfigView | None
    current_equity_cc: int | None
    ruin_floor_frac: float
    input_generation: int


def _worker_book_risk(inputs: BookRiskInputs) -> BookRiskSnapshot:
    """The function the pool runs. Reuses the engine's OWN compute_book_risk on the
    immutable BookModel — identical to the inline result (same pure code, same
    seed). Stamps ``input_generation`` so a stale result is discarded on publish."""
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


class BookRiskPool:
    """Runs the full-book MC in a worker process so it never blocks the event loop.

    Mirrors ``JointPool``: a small ``ProcessPoolExecutor`` and a single async
    ``run`` that ``await``s the worker (yielding control to the loop, which keeps
    beating the supervisor heartbeat while the MC computes). NO deadline: unlike a
    one-shot RFQ price, the book-risk snapshot is a maintenance artifact — if a run
    is slow it simply publishes late and the previous snapshot ages out (the
    freshness guard then fails the CVaR cap CLOSED, never open). One worker is
    enough (the recompute is throttled to well inside the freshness window)."""

    def __init__(self, *, workers: int = 1) -> None:
        self._workers = max(1, workers)
        self._executor: ProcessPoolExecutor | None = None
        self.calls = 0
        self.errors = 0

    def start(self) -> None:
        self._executor = ProcessPoolExecutor(max_workers=self._workers)
        log.info("book_risk_pool_started", workers=self._workers)

    async def run(self, inputs: BookRiskInputs) -> BookRiskSnapshot:
        """Run the full-book MC in a worker and return its snapshot. Awaiting the
        future yields the loop so the heartbeat keeps beating during the MC."""
        if self._executor is None:
            raise RuntimeError("BookRiskPool.run before start()")
        self.calls += 1
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._executor, _worker_book_risk, inputs
            )
        except Exception:
            self.errors += 1
            raise

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
            log.info("book_risk_pool_stopped", calls=self.calls, errors=self.errors)
