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
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from combomaker.core.conventions import Conventions
from combomaker.ops.config import PricingConfig
from combomaker.ops.logging import get_logger
from combomaker.ops.process_group import (
    WindowsKillJob,
    _ensure_workers_spawned,
    install_parent_death_signal,
    record_worker_pids,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.joint import JointEstimate
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import NoQuote
from combomaker.pricing.relationships import Relationship
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import OpenPosition
from combomaker.sim.book_model import BookModel
from combomaker.sim.book_risk import (
    BookRiskSnapshot,
    CandidateBookRisk,
    compute_book_risk,
    evaluate_candidate_book_risk,
)
from combomaker.sim.state_worst_case import (
    GameWorstCase,
    WorstCaseEntity,
    WorstCaseQuote,
    state_worst_case_by_game,
)
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
    # P2-1 layer 2: arm parent-death detection INSIDE this worker (Linux
    # PR_SET_PDEATHSIG; no-op elsewhere) so an abnormal parent exit cannot leave
    # this worker orphaned. Best-effort, never raises.
    install_parent_death_signal()
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
        data_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._conventions = conventions
        self._workers = max(1, workers)
        self._deadline_s = deadline_s
        # P2-1: where the worker-PID registry lives (for startup straggler reap).
        # None ⇒ orphan-prevention registry disabled (tests that don't need it);
        # the Job Object + parent-death signal still apply.
        self._data_dir = data_dir
        self._executor: ProcessPoolExecutor | None = None
        self._kill_job: WindowsKillJob | None = None
        self.timeouts = 0
        self.errors = 0
        self.calls = 0

    def start(self) -> None:
        # P2-1 layer 4 (straggler reap from a prior crashed run) is done ONCE at
        # app startup via ``cleanup_straggler_workers`` BEFORE any pool spawns —
        # not here — so a second pool's start can't truncate the first pool's
        # freshly-recorded PIDs. This pool only APPENDS its own PIDs (after warmup).
        #
        # P2-1 layer 1: parent-owned kill group (Windows Job Object with
        # KILL_ON_JOB_CLOSE). Workers are assigned in after warmup spawns them.
        self._kill_job = WindowsKillJob()
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
        # Workers are now spawned: bind them to the parent-owned kill group and
        # record their PIDs so a future startup can reap them if we die abnormally.
        self._register_workers()

    def _register_workers(self) -> None:
        """Assign spawned workers into the Windows kill-job (layer 1) and append
        their PIDs to the registry (layer 4). Best-effort; safe to call more than
        once (registry dedupes, re-assigning an already-in-job PID is a harmless
        no-op)."""
        if self._executor is None:
            return
        pids = _ensure_workers_spawned(self._executor, self._workers)
        if self._kill_job is not None and self._kill_job.active:
            for pid in pids:
                self._kill_job.assign(pid)
        if self._data_dir is not None and pids:
            record_worker_pids(self._data_dir, pids)

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
        # P2-1 layer 3: finally close/join. Cancel queued futures, then JOIN the
        # workers (wait=True) so a CLEAN stop reaps its own children deterministically
        # — the OS-level layers only ever matter on an ABNORMAL parent exit. The
        # kill-job handle is closed last, in a finally, so it is released even if the
        # join raises (and on an abnormal exit the OS closes it for us, triggering
        # KILL_ON_JOB_CLOSE).
        executor = self._executor
        self._executor = None
        try:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
                # Block until workers actually exit so none linger post-stop.
                executor.shutdown(wait=True)
        finally:
            if self._kill_job is not None:
                self._kill_job.close()
                self._kill_job = None
            if executor is not None:
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
    # P1-2: z-score for the one-sided Wilson upper confidence bound the ruin cap
    # gates on. 0.0 (default) ⇒ the bound == the p̂ point estimate (behaviour
    # unchanged); a positive z (e.g. 1.645 for a one-sided 95% level) makes the
    # ruin gate fail-closed against MC sampling error near the budget.
    ruin_prob_ci_z: float = 0.0


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
        ruin_prob_ci_z=inputs.ruin_prob_ci_z,
        input_generation=inputs.input_generation,
    )


# --------------------------------------------------------------------------- #
# P0-1: candidate- and reservation-aware portfolio risk, OFF the event loop.  #
# --------------------------------------------------------------------------- #
#
# WHY: the P0-1 candidate gate (book_risk.evaluate_candidate_book_risk) runs a
# ~20k-sample portfolio MC over the merged PRE + candidate book at CONFIRM time.
# Confirms are RARE (only when we win an auction) and the confirm window is 3s, so
# awaiting an off-loop MC here is fine — but running the MC INLINE on the event loop
# would block it (GIL-bound numpy), the same starvation class as the inline joint /
# book-risk wedges. So it runs in the SAME worker-process pool as the full-book MC.
#
# WHAT crosses the process boundary is only PICKLABLE, immutable values: the frozen
# OpenPositions (committed / candidate / reservations — primitive fields + LegRef
# tuples), the StructuralConfigView, the scalar budgets, AND two DICT-BACKED provider
# snapshots resolved ON-LOOP. The live MarginalProvider (a feed closure) and the
# WithinGameRhoProvider (a SgpParams closure) are NOT picklable, so the parent reads
# every candidate-universe leg marginal and every within-game pair rho ON THE LOOP
# into plain dicts, and the worker reconstructs pure lookup providers from them. A
# marginal absent from the dict resolves to None in the worker exactly as the live
# provider would return None ⇒ the merged model is UNKNOWN ⇒ confirm forced False
# (fail-closed, hard rule 6 — a missing marginal is NEVER scored as a usable p=0.5).
# The reconstructed providers are pure functions of the shipped dicts, so the
# off-loop verdict is byte-identical to the inline one on the same inputs + seed.


class _DictMarginals:
    """A picklable ``MarginalProvider`` backed by a ticker→marginal dict resolved
    on the loop. A ticker ABSENT from the dict (its live marginal was missing/stale)
    returns None — exactly what the live provider returns for an unpriceable leg —
    so the merged candidate model goes UNKNOWN and the gate declines (fail-closed)."""

    def __init__(self, marginals: dict[str, float]) -> None:
        self._marginals = marginals

    def __call__(self, market_ticker: str) -> float | None:
        return self._marginals.get(market_ticker)


class _DictWithinGameRho:
    """A picklable ``WithinGameRhoProvider`` backed by an unordered-pair→band dict
    resolved on the loop (the pricer's real ``build_sgp_correlation`` band per pair,
    computed on-loop by the live SgpParams provider). A pair absent from the dict
    (e.g. an identical-ticker self-pair, which the live provider maps to None)
    returns None, so ``build_book_model`` leaves that block to its own flat default —
    identical to the live provider's own None handling."""

    def __init__(self, pairs: dict[frozenset[str], tuple[float, float, float]]) -> None:
        self._pairs = pairs

    def __call__(
        self, ticker_a: str, ticker_b: str
    ) -> tuple[float, float, float] | None:
        return self._pairs.get(frozenset((ticker_a, ticker_b)))


@dataclass(frozen=True, slots=True)
class CandidateBookRiskInputs:
    """The IMMUTABLE, picklable inputs for one off-loop CANDIDATE book-risk MC.

    Built on the loop from a generation-stamped read of the committed positions +
    outstanding reservations + the contemplated ``candidate`` fill, with the leg
    marginals and within-game pair rhos resolved into plain dicts (the live feed /
    SgpParams closures do not pickle). Everything here is frozen + picklable, so
    shipping it to a worker cannot race a concurrent book mutation. The scalar
    budgets are the SAME ones the analytic caps use (RiskLimits), passed as floats /
    fractions so the worker gate is byte-identical to an inline call."""

    committed: tuple[OpenPosition, ...]
    candidate: OpenPosition
    reservations: tuple[OpenPosition, ...]
    marginals: dict[str, float]
    within_game_rho_pairs: dict[frozenset[str], tuple[float, float, float]]
    structural_cfg: StructuralConfigView | None
    n_samples: int
    seed: int
    band: str
    bankroll_cc: int | None
    current_equity_cc: int | None
    ruin_floor_frac: float
    ruin_prob_ci_z: float
    portfolio_cvar_frac: float | None
    portfolio_det_max_frac: float | None
    portfolio_ruin_prob_budget: float | None
    absolute_notional_multiple: int | None
    hedge_cost_budget_cc: int
    allow_negative_ev_hedge: bool
    # P1 EV VISIBILITY (audit "+EV IS PRODUCTION-MODEL EV"): the OPTIONAL worst-
    # challenger-EV tolerance. Defaults to −inf ⇒ the gate is production-model-EV
    # only (no behaviour change); the operator sets a finite (negative) tolerance to
    # ALSO decline a candidate whose worst credible challenger EV falls below it.
    worst_challenger_ev_tolerance: float = float("-inf")
    # P0-2 (candidate MC atomic with reservations). The ExposureBook POSITION
    # generation and the RiskReservationService VERSION captured on the loop at the
    # instant these inputs were read. They are NOT consumed by the worker (the MC
    # prices only the positions handed to it); the CALLER stamps them so that, when
    # the off-loop worker returns, it can compare them to the LIVE generation/version
    # and DISCARD+REBUILD a verdict computed against a book a concurrent accept's
    # reservation, or a fill/settlement/reconciliation, has since moved under it. A
    # default of -1 (an impossible real generation/version — both start at 0 and only
    # ever increase) means "not stamped" for the paper/no-reservation path, whose
    # single-loop confirm cannot race and so needs no version check.
    input_generation: int = -1
    reservation_version: int = -1


@dataclass(frozen=True, slots=True)
class StateWorstCaseInputs:
    """Picklable inputs for ONE off-loop state-consistent worst-case enumeration
    (the confirm-path LAST-LOOK MC WAIVER — sim/state_worst_case.py). Built
    ON-LOOP by the lifecycle at the reservation-denial instant: entities =
    committed positions (netting fully) + outstanding reservations (hedge-credit
    clamped, ``earns_credit=False``) + THE CANDIDATE (netting fully),
    ``open_quotes`` = every resting quote's per-side hypotheticals (clamped
    adversarially >= 0 per state by the enumeration — E2 rationale at confirm),
    ``marginals``/``events`` as plain picklable dicts. Stamped with the FULL
    ``ExposureBook.generation`` and the RiskReservationService VERSION captured
    at the read (the P0-2 pattern): the worker never consumes them — the CALLER
    compares them to the live values when the off-loop enumeration returns and
    rebuilds ONCE (then fails closed) if the book moved under it.

    ⚠ ``book_generation`` is deliberately the WHOLE-BOOK generation, NOT the
    position generation ``CandidateBookRiskInputs`` stamps: the candidate gate
    prices POSITIONS ONLY, so bare quote churn cannot stale its verdict — but
    this input set INCLUDES every resting open quote, and ``upsert_quote``/
    ``remove_quote`` bump only the full generation. Stamping the position
    generation here would let a quote land during the awaited enumeration and
    the stale certificate still skip the per-game caps on a book it never
    priced (adversarial-review findings 1+3, 2026-07-16 — by the module's own
    monotonicity property an omitted quote strictly UNDERSTATES the bound)."""

    entities: tuple[WorstCaseEntity, ...]
    open_quotes: tuple[WorstCaseQuote, ...]
    marginals: dict[str, float]
    events: dict[str, str | None] | None
    structural_cfg: StructuralConfigView
    book_generation: int = -1
    reservation_version: int = -1


def _worker_state_worst_case(
    inputs: StateWorstCaseInputs,
) -> dict[str, GameWorstCase]:
    """The function the pool runs for the last-look MC waiver. Pure pass-through
    to the engine's OWN ``state_worst_case_by_game`` (no reimplementation — hard
    rule 8c). Deterministic (exact enumeration, no sampling), so identical to an
    inline call."""
    return state_worst_case_by_game(
        inputs.entities,
        inputs.open_quotes,
        inputs.marginals,
        inputs.events,
        inputs.structural_cfg,
    )


def _timed_worker_candidate_book_risk(
    inputs: CandidateBookRiskInputs,
) -> tuple[CandidateBookRisk, float]:
    """``_worker_candidate_book_risk`` wrapped with an IN-WORKER compute timer.

    Returns ``(verdict, compute_ms)`` where ``compute_ms`` is the wall time the MC
    itself took INSIDE the worker process (``perf_counter`` — a within-process
    duration, safe across the boundary as a scalar). The pool subtracts this from the
    total submit→return wall time to derive the QUEUE DWELL (time the submission
    waited for a free worker) — the audit's "MC worker queue dwell" metric, measured
    without assuming a shared cross-process clock (only durations cross the boundary,
    never absolute timestamps)."""
    import time as _time

    t0 = _time.perf_counter()
    verdict = _worker_candidate_book_risk(inputs)
    return verdict, (_time.perf_counter() - t0) * 1e3


def _worker_candidate_book_risk(
    inputs: CandidateBookRiskInputs,
) -> CandidateBookRisk:
    """The function the pool runs. Reconstructs the dict-backed providers and reuses
    the engine's OWN ``evaluate_candidate_book_risk`` (no reimplementation — hard
    rule 8). Identical to an inline call (same pure code, same seed ⇒ deterministic
    across processes)."""
    return evaluate_candidate_book_risk(
        inputs.committed,
        inputs.candidate,
        marginals=_DictMarginals(inputs.marginals),
        reservations=inputs.reservations,
        within_game_rho=_DictWithinGameRho(inputs.within_game_rho_pairs),
        structural_cfg=inputs.structural_cfg,
        n_samples=inputs.n_samples,
        seed=inputs.seed,
        band=inputs.band,
        bankroll_cc=inputs.bankroll_cc,
        current_equity_cc=inputs.current_equity_cc,
        ruin_floor_frac=inputs.ruin_floor_frac,
        ruin_prob_ci_z=inputs.ruin_prob_ci_z,
        portfolio_cvar_frac=inputs.portfolio_cvar_frac,
        portfolio_det_max_frac=inputs.portfolio_det_max_frac,
        portfolio_ruin_prob_budget=inputs.portfolio_ruin_prob_budget,
        absolute_notional_multiple=inputs.absolute_notional_multiple,
        hedge_cost_budget_cc=inputs.hedge_cost_budget_cc,
        allow_negative_ev_hedge=inputs.allow_negative_ev_hedge,
        worst_challenger_ev_tolerance=inputs.worst_challenger_ev_tolerance,
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

    def __init__(self, *, workers: int = 1, data_dir: Path | None = None) -> None:
        self._workers = max(1, workers)
        self._data_dir = data_dir
        self._executor: ProcessPoolExecutor | None = None
        self._kill_job: WindowsKillJob | None = None
        self.calls = 0
        self.errors = 0
        # P1 LATENCY: the MOST-RECENT candidate MC's in-worker compute time and the
        # queue dwell (total submit→return wall time − in-worker compute = time the
        # submission waited for a free worker). The caller reads these after each
        # ``run_candidate`` to record the audit's queue-dwell / runtime metrics. None
        # until the first candidate MC runs.
        self.last_candidate_compute_ms: float | None = None
        self.last_candidate_dwell_ms: float | None = None

    def start(self) -> None:
        # Straggler reap is done ONCE at app startup (see JointPool.start note).
        self._kill_job = WindowsKillJob()
        # Initializer arms parent-death detection in the worker (layer 2).
        self._executor = ProcessPoolExecutor(
            max_workers=self._workers, initializer=install_parent_death_signal
        )
        log.info("book_risk_pool_started", workers=self._workers)

    def register_workers(self) -> None:
        """Bind spawned workers to the kill-job (layer 1) + record their PIDs
        (layer 4). Unlike JointPool there is no warmup, so the caller invokes this
        after ``start`` (workers spawn lazily on first submit; this polls briefly
        for them). Best-effort."""
        if self._executor is None:
            return
        pids = _ensure_workers_spawned(self._executor, self._workers, timeout_s=1.0)
        if self._kill_job is not None and self._kill_job.active:
            for pid in pids:
                self._kill_job.assign(pid)
        if self._data_dir is not None and pids:
            record_worker_pids(self._data_dir, pids)

    async def run(self, inputs: BookRiskInputs) -> BookRiskSnapshot:
        """Run the full-book MC in a worker and return its snapshot. Awaiting the
        future yields the loop so the heartbeat keeps beating during the MC."""
        if self._executor is None:
            raise RuntimeError("BookRiskPool.run before start()")
        self.calls += 1
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                self._executor, _worker_book_risk, inputs
            )
        except Exception:
            self.errors += 1
            raise
        # The worker has now spawned (lazy on first submit): bind it to the kill
        # group + register its PID. Cheap + idempotent (registry dedupes).
        self.register_workers()
        return result

    async def run_candidate(
        self, inputs: CandidateBookRiskInputs
    ) -> CandidateBookRisk:
        """Run the P0-1 CANDIDATE book-risk MC in a worker and return its verdict.

        Awaiting the future yields the loop so the heartbeat keeps beating during the
        ~20k-sample MC. Confirms are rare and the confirm window is 3s, so awaiting
        off-loop here is fine — what matters is that the CPU-bound MC never runs
        INLINE on the loop. NO deadline (unlike a one-shot RFQ price): the confirm
        path awaits the verdict, and any exception propagates to the caller, which
        DECLINES the confirm (fail-closed — an errored gate never confirms)."""
        if self._executor is None:
            raise RuntimeError("BookRiskPool.run_candidate before start()")
        self.calls += 1
        loop = asyncio.get_running_loop()
        submit_ns = time.monotonic_ns()
        try:
            result, compute_ms = await loop.run_in_executor(
                self._executor, _timed_worker_candidate_book_risk, inputs
            )
        except Exception:
            self.errors += 1
            raise
        # P1 LATENCY: total submit→return wall (parent monotonic) minus the in-worker
        # compute = queue dwell (time the submission waited for a free worker). Clamped
        # at 0 (float noise / a compute_ms marginally above the coarse parent wall).
        total_ms = (time.monotonic_ns() - submit_ns) / 1e6
        self.last_candidate_compute_ms = compute_ms
        self.last_candidate_dwell_ms = max(0.0, total_ms - compute_ms)
        self.register_workers()
        return result

    async def run_state_worst_case(
        self, inputs: StateWorstCaseInputs, *, deadline_s: float
    ) -> dict[str, GameWorstCase]:
        """Run the LAST-LOOK MC WAIVER's state-consistent worst-case enumeration
        in a worker, bounded by ``deadline_s`` (the waiver's REMAINING wall
        budget, ``risk.lastlook_mc_waiver_deadline_s`` at most).

        Unlike the book-risk snapshot (no deadline — a late snapshot just ages
        out) this is a confirm-window decision, so it mirrors ``JointPool``'s
        deadline semantics: on timeout the loop stops waiting (``TimeoutError``
        propagates and the caller DECLINES fail-closed — never confirm on an
        unmeasured waiver) while the worker finishes and frees itself. Awaiting
        yields the loop, so the heartbeat keeps beating during the enumeration."""
        if self._executor is None:
            raise RuntimeError("BookRiskPool.run_state_worst_case before start()")
        self.calls += 1
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._executor, _worker_state_worst_case, inputs)
        try:
            result = await asyncio.wait_for(fut, timeout=deadline_s)
        except Exception:
            self.errors += 1
            raise
        self.register_workers()
        return result

    def shutdown(self) -> None:
        # Layer 3: finally close/join, then release the kill-job handle.
        executor = self._executor
        self._executor = None
        try:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
                executor.shutdown(wait=True)
        finally:
            if self._kill_job is not None:
                self._kill_job.close()
                self._kill_job = None
            if executor is not None:
                log.info("book_risk_pool_stopped", calls=self.calls, errors=self.errors)
