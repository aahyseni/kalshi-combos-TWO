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

from combomaker.core.conventions import Conventions
from combomaker.ops.config import PricingConfig
from combomaker.ops.logging import get_logger
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import NoQuote
from combomaker.pricing.relationships import Relationship
from combomaker.rfq.models import Rfq

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
