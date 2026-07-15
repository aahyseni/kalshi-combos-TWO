"""Quote lifecycle: the hot path where makers die or eat.

rfq_created → filter → price (in-memory only) → risk gate → CreateQuote →
… → quote_accepted → LAST LOOK → ConfirmQuote or deliberately lapse.

Rules encoded here:
- Every open quote carries its full pricing snapshot (fair, leg mids) and a
  TTL; it is repriced (replacement quote) when fair moves, deleted when TTL
  expires, its RFQ dies, a book invalidates, or the kill switch fires.
- The last-look decision uses only warm in-memory state; the confirm
  round-trip is the only network call, and its latency is metered
  (``confirm.decision_ms`` local think time, ``confirm.rtt_ms`` round trip).
- Declining = deliberately NOT confirming (no decline endpoint exists
  post-accept). Declined confirms get markouts too — dodged bullet or spurned
  profit, the data decides.
- Every decision is persisted with a reason code and inputs.

Freshness semantics: a quiet book on a live seq-continuous feed IS current —
the staleness input to last look is feed-traffic age (server pings every 10s),
gated by per-book validity. Book invalidation cancels quotes wholesale before
any resync (feed ordering guarantees that).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_CENT, CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts, qty_from_fp_str
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import KalshiApiError
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.pricing_pool import (
    BookRiskInputs,
    BookRiskPool,
    CandidateBookRiskInputs,
    JointPool,
    _worker_candidate_book_risk,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeType, FeeUnknownError
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.models import Rfq
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, OpenQuoteRisk
from combomaker.risk.fill_velocity import FillVelocityTracker
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.lastlook import (
    LastLookInputs,
    LastLookPolicy,
    decide_confirm,
)
from combomaker.risk.limits import (
    Breach,
    DailyPnl,
    HaltInputs,
    LimitChecker,
    PortfolioRisk,
    StartTimeProvider,
    StarvationWatchdog,
    threshold_cc,
)
from combomaker.risk.markouts import MarkoutSubject, MarkoutTracker
from combomaker.risk.reservation import RiskReservationService
from combomaker.risk.skew import (
    GameSkewCache,
    SkewLimits,
    SkewParams,
    WidenPolicyParams,
    compute_inventory_skew,
    decide_widen_or_decline,
)
from combomaker.sim.book_model import (
    BookModel,
    WithinGameRhoProvider,
    build_book_model,
)
from combomaker.sim.book_risk import (
    BookRiskSnapshot,
    compute_book_risk,
    modeled_cost_basis_cc,
)
from combomaker.sim.structural_book import StructuralConfigView

log = get_logger(__name__)

JsonDict = dict[str, Any]


class QuoteSender(Protocol):
    """REST slice the lifecycle needs; PaperSender fakes it for paper mode."""

    async def create_quote(
        self,
        rfq_id: str,
        *,
        yes_bid_cc: CentiCents,
        no_bid_cc: CentiCents,
        rest_remainder: bool = False,
    ) -> JsonDict: ...

    async def delete_quote(self, quote_id: str) -> JsonDict: ...

    async def confirm_quote(self, quote_id: str) -> JsonDict: ...


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    quote_ttl_s: float = 30.0
    reprice_threshold_cc: int = 100
    exchange_active: bool = True  # updated by the exchange-status poller
    # Portfolio-CVaR book-risk MC (armed off the slow loop; never inside check()).
    # ``book_risk_mc_samples`` is smaller than the report's 100k because this runs
    # on the maintenance cadence and only feeds the operative-ES cap; it is still
    # a full portfolio MC. ``book_risk_stale_after_s`` is the freshness window: a
    # non-empty book whose latest snapshot is older than this (or was never built)
    # fails the CVaR cap CLOSED (UNKNOWN tail is never safe). ``book_risk_seed``
    # keeps the MC deterministic/auditable.
    book_risk_mc_samples: int = 20_000
    book_risk_stale_after_s: float = 30.0
    book_risk_seed: int = 7
    # A2 ruin floor: equity below this fraction of bankroll is "ruin". Operator
    # directive 2026-07-15: −30% ⇒ 0.70. Feeds compute_book_risk's p_ruin, which
    # the P(ruin) cap gates against ``portfolio_ruin_prob_budget``.
    ruin_floor_frac: float = 0.70
    # P1-2: z-score for the one-sided Wilson UPPER confidence bound the ruin cap
    # gates on (fail-closed against MC sampling error near the budget). 0.0 (the
    # default) ⇒ the bound == the p̂ point estimate — behaviour unchanged; set e.g.
    # 1.645 for a one-sided 95% level to decline a fill whose ruin p̂ only just
    # clears the budget by luck of the draw.
    ruin_prob_ci_z: float = 0.0
    # P0-1 candidate-aware portfolio-risk gate at CONFIRM. When True (default), a
    # confirm the existing analytic/gross/burst gates already ADMIT runs an ADDITIONAL
    # candidate-aware ~20k-sample portfolio MC (off the loop via the BookRiskPool):
    # confirm ONLY when the candidate's marginal EV is positive AND the POST-book
    # joint-tail / ruin / deterministic / gross budgets pass. STRICTLY ADDITIVE — it
    # can only DECLINE a fill the other gates admit, never admit one they decline. An
    # UNKNOWN merged marginal, an over-budget POST book, or ANY error in the off-loop
    # eval declines (fail-closed). False ⇒ the gate is skipped entirely (prior
    # behaviour preserved) and is the kill switch for this gate.
    candidate_gate_enabled: bool = True
    # P0-1 candidate MC sample count (smaller than the maintenance full-book MC's
    # 20k default is fine — a confirm is one-shot and the window is 3s). Kept
    # explicit + deterministic (seeded) for auditability.
    candidate_gate_mc_samples: int = 20_000


@dataclass(frozen=True, slots=True)
class _StaleBookRisk:
    """A fail-closed ``PortfolioRisk`` sentinel: a NON-empty book whose book-risk
    snapshot is stale/absent must still make BOTH the CVaR cap and the
    deterministic max-loss cap BREACH (an unmeasured joint tail / deterministic
    maximum is never safe). ``usable`` False ⇒ both caps fail closed regardless of
    the values below; the tail fields are 0.0 and never read on the unusable
    path."""

    usable: bool = False
    governing_model_es_99_cc: float = 0.0
    deterministic_max_loss_cc: float = 0.0
    p_ruin: float = 0.0
    p_ruin_upper: float = 0.0  # P1-2 (never read on the unusable path)


@dataclass
class OpenQuoteState:
    quote_id: str
    rfq: Rfq
    constructed: ConstructedQuote
    leg_mids_cc: dict[str, int]
    created_mono_ns: int
    accepted: bool = False
    # Conservative full-RFQ size the risk system uses for this quote.
    risk_qty: CentiContracts = CentiContracts(0)
    # (side accepted, our bid on that side, accepted quantity) once confirmed
    pending_fill: tuple[Side, CentiCents, CentiContracts] | None = None


class QuoteLifecycle:
    def __init__(
        self,
        *,
        clock: Clock,
        sender: QuoteSender,
        engine: PricingEngine,
        rfq_filter: RfqFilter,
        limits: LimitChecker,
        exposure: ExposureBook,
        feed: OrderbookFeed,
        metadata: MetadataCache,
        inplay: InPlayDetector,
        killswitch: KillSwitch,
        conventions: Conventions,
        store: Store,
        metrics: Metrics,
        lastlook_policy: LastLookPolicy,
        config: LifecycleConfig,
        balance_tracker: BalanceTracker | None = None,
        start_time_provider: StartTimeProvider | None = None,
        starvation_watchdog: StarvationWatchdog | None = None,
        within_game_rho: WithinGameRhoProvider | None = None,
        structural_cfg: StructuralConfigView | None = None,
        reservation: RiskReservationService | None = None,
        skew_params: SkewParams | None = None,
        skew_limits: SkewLimits | None = None,
        skew_cache: GameSkewCache | None = None,
        widen_params: WidenPolicyParams | None = None,
        fee_model: FeeModel | None = None,
        fee_type: FeeType = FeeType.QUADRATIC,
        fee_multiplier: Fraction = Fraction(1),
        joint_pool: JointPool | None = None,
        book_risk_pool: BookRiskPool | None = None,
    ) -> None:
        self._clock = clock
        self._sender = sender
        self._engine = engine
        # Off-loop pricing (Phase 1): when set, the async hot path runs the
        # expensive joint step in a worker process with a deadline so a cold combo
        # can never wedge the loop. None ⇒ inline pricing (backtests, paper, tests).
        self._joint_pool = joint_pool
        # P2-2: when set, the full-book portfolio MC runs in a WORKER PROCESS off
        # the event loop (on the immutable BookModel, generation-safe), so a large
        # book's MC can never block the maintenance loop long enough to starve the
        # supervisor heartbeat. None ⇒ the MC runs INLINE on the maintenance tick
        # (backtests, paper, tests, and any embedding without the pool) — same
        # numbers, just on-loop.
        self._book_risk_pool = book_risk_pool
        self._filter = rfq_filter
        self._limits = limits
        self._exposure = exposure
        self._feed = feed
        self._metadata = metadata
        self._inplay = inplay
        self._killswitch = killswitch
        self._conventions = conventions
        self._store = store
        self._metrics = metrics
        self._policy = lastlook_policy
        self._config = config
        # R2 Phase 2 (SHADOW): live bankroll denominator for the %-of-bankroll
        # caps (fail-closed → None when stale), the per-leg game-start source for
        # the slate cap, and the starvation watchdog that warns if the new caps
        # silently decline everything. All optional — omitted, the checker's R2
        # layer simply fails closed (SKIP_BANKROLL_UNAVAILABLE, shadow) and the
        # enforced caps behave exactly as before.
        self._balance = balance_tracker
        self._start_time_provider = start_time_provider
        self._watchdog = starvation_watchdog
        # Phase 4: the PRICER's real within-game rho for the portfolio-CVaR book
        # risk MC. Threaded into build_book_model so the MC's joint tail uses the
        # SHIPPED per-pair correlations (not the flat DEFAULT_FLAT_BAND). Omitted ⇒
        # the MC falls back to the flat band (the pre-wire behaviour); the cap
        # still arms, just off a coarser correlation view.
        self._within_game_rho = within_game_rho
        # A1: the Dixon-Coles constants for the STRUCTURAL portfolio-risk MC. When
        # set, recompute_book_risk samples same-game legs from the joint scoreline
        # (every hedge/exclusion exact, no rho) instead of the Gaussian copula; the
        # copula path still carries corners/cards/other-sport legs. None ⇒ the
        # pre-A1 copula-only MC (byte-identical).
        self._structural_cfg = structural_cfg
        # Latest full-MC book-risk snapshot + the monotonic time it was built.
        # Armed by recompute_book_risk() off the slow loop; READ (never recomputed)
        # inside check() via _book_risk_for_check(), which keeps check() cheap. A
        # non-empty book with a stale/absent snapshot fails the CVaR cap CLOSED.
        self._book_risk: BookRiskSnapshot | None = None
        self._book_risk_mono_ns: int | None = None
        # Throttle the MC recompute to comfortably inside the freshness window
        # (half of it) so the snapshot stays fresh without running a full MC every
        # 0.5s maintenance tick. None ⇒ never refreshed yet.
        self._book_risk_refresh_mono_ns: int | None = None
        # P2-2: the in-flight OFF-LOOP recompute task, if any. The maintenance tick
        # LAUNCHES the off-loop MC as a background task and returns IMMEDIATELY (it
        # never awaits the MC), so the maintenance loop keeps beating the heartbeat
        # on its 0.5s cadence while the MC runs in a worker process. A single-flight
        # guard: a new recompute is not launched while the previous one is still
        # running (its result publishes when it finishes).
        self._book_risk_task: asyncio.Task[None] | None = None
        # Fill-velocity governor: a rolling committed-notional + count window over
        # our OWN acceptances, built from the SAME RiskLimits the caps use. A
        # burst over the soft frac / max fills DECLINEs further confirms +
        # cancels-all; a hard-frac burst HALTs. The COUNT limit binds even on a
        # stale bankroll (fail-closed).
        self._fill_velocity = FillVelocityTracker(
            clock, window_s=limits.limits.fill_velocity_window_s
        )
        # R3 Phase 3: single-writer risk-reservation service. When present, the
        # confirm path RESERVES headroom (atomic + versioned) BEFORE the confirm
        # round-trip and commits/releases/marks-unconfirmed based on the outcome —
        # closing the check→confirm→book gap where two concurrent accepts could
        # each pass the same check against stale headroom. Optional: when omitted
        # the confirm path behaves exactly as before (the reservation is race-free
        # today under one asyncio loop; the service makes it safe for fan-out).
        self._reservation = reservation
        # Phase 5 (R3 Part A): inventory-aware skew, DARK by default. When
        # skew_params/skew_limits are wired, handle_rfq COMPUTES + LOGS the honest
        # skew every quote but passes 0 into the pricer while skew_params.enabled
        # is False (a zero-P&L shadow). Omitted ⇒ no skew computed at all (the
        # pricer's inventory_skew_cc stays 0, behaviour identical to Phase 4).
        self._skew_params = skew_params
        self._skew_limits = skew_limits
        self._skew_cache = skew_cache
        # Widen-vs-DECLINE policy (R3 Part R2), SHADOW by default. Needs the same
        # snapshot + candidate the skew builds, so it is computed alongside it.
        self._widen_params = widen_params
        # Fee model for the REAL fill fee booked at execution (defense #3): our
        # combo maker quadratic fills compute $0 (pricing/fees.py + ground truth),
        # correct for any nonzero-fee series. None ⇒ book fee UNKNOWN (None) — the
        # pre-Phase-6 behaviour — rather than a guessed 0.
        self._fee_model = fee_model
        self._fee_type = fee_type
        self._fee_multiplier = fee_multiplier
        self._markouts = MarkoutTracker(store.record_markout)
        self._open: dict[str, OpenQuoteState] = {}       # quote_id → state
        self._by_rfq: dict[str, str] = {}                # rfq_id → quote_id
        self._executed_states: dict[str, OpenQuoteState] = {}
        self._realized_pnl_cc = 0
        self._confirm_failures = 0
        self.daily_pnl = DailyPnl()
        self.exchange_active = config.exchange_active

    # ------------------------------------------------------------------ R2 seam

    def partition_breaches(self, breaches: list[Breach]) -> list[Breach]:
        """Public alias for the shadow-split used to build the reservation
        service's ``breach_splitter`` — so the shadow rule lives in ONE place
        (this lifecycle) and the reservation layer reuses it verbatim."""
        return self._partition_breaches(breaches)

    def attach_reservation(self, reservation: RiskReservationService) -> None:
        """Wire the reservation service in AFTER construction (the service needs
        this lifecycle's shadow splitter, and this lifecycle needs the service —
        break the cycle by attaching post-construction). Set once."""
        self._reservation = reservation

    def _risk_bankroll_cc(self) -> int | None:
        """The live risk-capital denominator in cc for the %-of-bankroll caps,
        or None when unavailable/stale (fail-closed — the checker then emits
        SKIP_BANKROLL_UNAVAILABLE when a source is configured). Uses the
        NON-raising accessor so a stale poll never throws on the hot path."""
        if self._balance is None:
            return None
        got = self._balance.risk_bankroll_cc_or_none()
        return None if got is None else int(got)

    def _bankroll_source_configured(self) -> bool:
        """Whether a bankroll SOURCE (balance tracker) is wired at all.

        Fail-closed-without-bricking: when a source IS configured but its reading
        is stale (``_risk_bankroll_cc`` → None), the checker fails the %-caps
        CLOSED (a no-quote, the dark-poll runaway defense). When NO source is
        wired (demo/paper without a balance tracker), the R2 %-cap layer is simply
        INACTIVE — a fresh start still quotes off the enforced hard-dollar caps
        rather than bricking. The prod/paper app ALWAYS wires the tracker
        (quote_app.py), so this is False only in minimal embeddings / unit rigs."""
        return self._balance is not None

    def _halt_inputs(self) -> HaltInputs:
        """Give-back inputs (intraday peak + current equity) for the drawdown /
        hard-trip halts, from the BalanceTracker via its NON-raising accessors so
        a stale poll never throws on the hot path. When either reading is
        unavailable both come back None and the checker simply skips those two
        halts (no invented peak — a missing input is never a convenient default).
        Empty when there is no tracker at all."""
        if self._balance is None:
            return HaltInputs()
        return HaltInputs(
            peak_equity_cc=self._balance.peak_equity_cc_or_none(),
            current_equity_cc=self._balance.exchange_equity_cc_or_none(),
        )

    # ------------------------------------------------ portfolio-CVaR book risk

    def _build_book_risk_inputs(self) -> BookRiskInputs:
        """Build the IMMUTABLE inputs for one full-book MC run, on the loop.

        This is the ONLY on-loop work of a recompute: capture the position
        generation (P0-2), read the positions, and build the frozen ``BookModel``.
        It is cheap relative to the MC itself, so it never starves the loop; the
        expensive ``compute_book_risk`` runs on the returned frozen model, either
        inline (``recompute_book_risk``) or in a worker process
        (``recompute_book_risk_offloop``).

        P0-2: the generation is captured BEFORE reading the positions and stamped
        into the returned inputs, so the snapshot the MC produces is tagged with
        the generation of the exact portfolio it prices. If a
        fill/settlement/rehydration/reconciliation/reservation bumps the position
        generation while the (off-loop) MC runs, ``_publish_book_risk`` /
        ``_book_risk_for_check`` discard the result immediately (its
        ``input_generation`` is stale) rather than trusting a still-time-fresh
        snapshot of a superseded portfolio. (Bare quote mutations do NOT bump the
        position generation — the MC prices positions only — so quote churn never
        spuriously invalidates a still-consistent snapshot.)"""
        gen = self._exposure.position_generation
        positions = list(self._exposure.positions.values())
        model = build_book_model(
            positions,
            marginals=self._marginals,
            within_game_rho=self._within_game_rho,
        )
        return BookRiskInputs(
            model=model,
            n_samples=self._config.book_risk_mc_samples,
            seed=self._config.book_risk_seed,
            band="high",
            bankroll_cc=self._risk_bankroll_cc(),
            structural_cfg=self._structural_cfg,
            # P1-3 (no double count of position value): the ruin check adds the
            # sampled ``book_pnl`` (measured ENTRY-to-terminal, ``payout −
            # price_cc`` per contract) onto this scalar. We therefore feed the
            # COST basis — available_cash + Σ price_cc·contracts of the modeled
            # book — NOT exchange equity (cash + portfolio_value). The entry
            # premium then cancels exactly against ``book_pnl`` and the sum equals
            # cash + Σ payout = true terminal equity, independent of the intraday
            # mark. Feeding exchange equity would leave the unrealized mark-to-
            # market (portfolio_value − Σ price_cc·c) ADDED on top of an entry-
            # based P&L, double-counting the already-marked position value. Cash
            # stale/absent ⇒ None ⇒ the ruin cap simply does not evaluate
            # (fail-closed; a missing cash reading is never an invented equity).
            current_equity_cc=self._ruin_equity_basis_cc(model),
            ruin_floor_frac=self._config.ruin_floor_frac,
            ruin_prob_ci_z=self._config.ruin_prob_ci_z,
            input_generation=gen,
        )

    def _ruin_equity_basis_cc(self, model: BookModel) -> int | None:
        """COST-basis equity for the P(ruin) check (P1-3): live available cash
        plus the modeled book's entry premium (``modeled_cost_basis_cc``). This
        is the one basis on which ``equity + book_pnl`` reconciles to the true
        terminal equity (cash + Σ payout) with NO double count of the already-
        marked position value — the derivation is in ``modeled_cost_basis_cc``.
        Returns None (ruin cap does not evaluate) when there is no balance tracker
        or the cash reading is stale/absent — a missing cash figure is never
        replaced with a convenient equity default."""
        if self._balance is None:
            return None
        cash_cc = self._balance.available_cash_cc_or_none()
        if cash_cc is None:
            return None
        return int(cash_cc) + int(round(modeled_cost_basis_cc(model)))

    def _publish_book_risk(self, snap: BookRiskSnapshot) -> None:
        """Publish a fresh MC snapshot — but ONLY if it still describes the CURRENT
        portfolio (P2-2 generation-safety).

        A snapshot computed off the event loop can finish AFTER a fill/settlement/
        reconciliation/reservation mutated the book. Its ``input_generation`` is the
        position generation it was built against; if the live position generation
        has moved on, the snapshot prices a SUPERSEDED portfolio and is DISCARDED
        (never published — the previous snapshot then ages out and the freshness
        guard fails the cap CLOSED, the safe direction). Only a still-current
        snapshot is stored. Cheap: one generation read + one clock read.

        NOTE ``_book_risk_for_check`` re-checks the generation at READ time too, so
        even a snapshot that was current at publish is re-invalidated the instant a
        later fill supersedes it — this publish-time gate simply avoids storing a
        DOA snapshot in the first place."""
        if snap.input_generation != self._exposure.position_generation:
            log.info(
                "book_risk_snapshot_discarded_stale",
                snapshot_generation=snap.input_generation,
                live_generation=self._exposure.position_generation,
            )
            return
        self._book_risk = snap
        self._book_risk_mono_ns = self._clock.monotonic_ns()
        if snap.usable:
            log.info(
                "book_risk_snapshot",
                n_positions=snap.n_positions,
                structural=self._structural_cfg is not None,
                governing_model_es_99_cc=int(snap.governing_model_es_99_cc),
                es_99_cc=int(snap.es_99_cc),
                challenger_es_99_cc=int(snap.challenger_es_99_cc),
                deterministic_max_loss_cc=int(snap.deterministic_max_loss_cc),
                p_ruin=round(snap.p_ruin, 4),
            )

    def recompute_book_risk(self) -> None:
        """Arm the portfolio-CVaR cap: build a fresh full-MC ``BookRiskSnapshot``
        over the REAL book and store it (with the monotonic time it was built).

        INLINE variant — the MC runs on the calling thread. Used by backtests,
        paper mode, unit tests, and any embedding without a ``book_risk_pool``. The
        live async loop prefers ``recompute_book_risk_offloop`` so the MC never
        blocks the loop; this variant is the byte-identical on-loop fallback (same
        seed, same immutable model ⇒ same snapshot).

        The book model threads the PRICER's real ``within_game_rho`` (so the joint
        tail carries the shipped per-pair correlations, not the flat default band)
        and the live ``bankroll_cc`` (so the ruin thresholds populate). An empty
        book stores an empty (unusable) snapshot; a missing marginal makes the
        model UNKNOWN and the snapshot unusable (fail-closed downstream).

        Never raises on the loop: any failure leaves the LAST snapshot to age out
        (the freshness guard in ``_book_risk_for_check`` then fails the cap closed
        for a non-empty book) rather than crashing the maintenance tick."""
        try:
            inputs = self._build_book_risk_inputs()
            snap = compute_book_risk(
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
            # Inline path builds the model and runs the MC without yielding, so the
            # generation cannot move between build and store; the publish gate is a
            # harmless no-op here (and keeps the store logic in one place).
            self._publish_book_risk(snap)
        except Exception:
            log.exception("book_risk_recompute_failed")

    async def recompute_book_risk_offloop(self) -> None:
        """OFF-LOOP variant (P2-2): run the full-book MC in a worker PROCESS on the
        immutable ``BookModel`` so it never blocks the event loop / heartbeat.

        The cheap prefix (capture generation + build the frozen model) runs on the
        loop; the expensive ``compute_book_risk`` is shipped to ``book_risk_pool``
        and ``await``ed — yielding control so the maintenance loop keeps beating the
        supervisor heartbeat while the MC computes. The returned snapshot is stamped
        with the generation it was built against and passed through
        ``_publish_book_risk``, which DISCARDS it if a fill/settlement/reservation
        superseded the book since (generation-safe).

        Falls back to the inline path when no pool is wired. Never raises on the
        loop (any failure ages out the last snapshot ⇒ fail-closed)."""
        if self._book_risk_pool is None:
            self.recompute_book_risk()
            return
        try:
            inputs = self._build_book_risk_inputs()
            snap = await self._book_risk_pool.run(inputs)
            self._publish_book_risk(snap)
        except Exception:
            log.exception("book_risk_recompute_offloop_failed")

    def _book_risk_for_check(self) -> PortfolioRisk | None:
        """The book-risk snapshot to feed ``check()``'s portfolio-CVaR cap.

        Rules (fail-closed; UNKNOWN joint tail is never safe):
          - EMPTY book (no committed positions) ⇒ None: the CVaR cap is simply not
            evaluated (nothing to cap; an empty book must still quote).
          - NON-EMPTY book with NO snapshot yet, or a snapshot whose
            ``input_generation`` no longer matches the live POSITION generation (a
            fill/settlement/rehydration/reconciliation/reservation mutated the
            portfolio since the MC read it — P0-2), or a snapshot older than
            ``book_risk_stale_after_s`` ⇒ a ``_StaleBookRisk`` sentinel
            (``usable=False``) so the cap FAILS CLOSED — the book carries a joint
            tail we have not measured against the CURRENT portfolio.
          - Otherwise ⇒ the latest snapshot (which itself fails closed when its
            ``usable`` is False, e.g. an UNKNOWN marginal made the model no-go).

        P0-2: the GENERATION match is the primary consistency proof; TIME AGE is a
        secondary guard (it still catches a book that mutated in a way the counter
        somehow missed, and a wall-clock-stale snapshot on a quiet book). A snapshot
        can be time-fresh yet generation-stale (fills changed the portfolio within
        the freshness window) — the generation check invalidates it immediately.
        Cheap: reads stored state + one clock read; never runs MC."""
        if not self._exposure.positions:
            return None
        snap = self._book_risk
        stamp = self._book_risk_mono_ns
        if snap is None or stamp is None:
            return _StaleBookRisk()  # non-empty book, never measured ⇒ fail closed
        if snap.input_generation != self._exposure.position_generation:
            # The PORTFOLIO was mutated (fill / settlement / rehydration /
            # reconciliation / reservation) since this snapshot's MC read the
            # positions. Even if it is still time-fresh, it no longer describes the
            # current portfolio, so the positions-only book-risk MC is inconsistent.
            return _StaleBookRisk()  # position generation superseded ⇒ fail closed
        age_s = (self._clock.monotonic_ns() - stamp) / 1e9
        if age_s > self._config.book_risk_stale_after_s:
            return _StaleBookRisk()  # snapshot too old ⇒ fail closed (secondary)
        return snap

    # ------------------------------------------------------------- risk audit

    def _risk_audit_fields(
        self,
        *,
        candidate_ev_cc: int | None,
        binding_cap: str,
        fallback_reason: str,
    ) -> dict[str, Any]:
        """P2-4: assemble the per-quote/confirm risk-audit record from WARM state
        only (no I/O, no MC, hot-path safe) — every field the audit spec enumerates,
        in one place, so a single ``risk_audit`` log line explains every decision:

          - book/snapshot generation + age: which portfolio the tail was measured
            against, whether it still matches the live generation, and how old it is;
          - candidate EV: this quote/fill's expected edge in cc (None ⇒ UNKNOWN, e.g.
            an unverified NO-complement — never coerced to a convenient 0);
          - ES / P(ruin) / deterministic loss: the committed-book tail the caps gate
            on (the governing model ES, the ruin p̂ and its Wilson upper bound, the
            deterministic all-hit maximum) — the numbers ``check()`` actually reads;
          - gross + direction: the mass-acceptance gross premium-at-risk and the
            mutex-aware worst per-game directional bound (P0-9), the two size axes;
          - reservations: outstanding pre-confirm headroom reservations;
          - model split / residual: production vs correlation-inflated challenger vs
            full-copula bridge ES, whether the bridge fired, and the governing −
            production residual (how much the challenger/bridge widened the tail);
          - fallback reason: WHY the tail is unusable when it is (stale generation,
            aged-out snapshot, never-measured book, UNKNOWN marginal) — the
            fail-closed path made visible, "" when the snapshot is usable;
          - binding cap: the cap/decline reason that bound this decision ("" when the
            quote/confirm went through clean).

        Reads the SAME ``_book_risk_for_check`` view the caps consume (so the audit
        matches the gate to the number) plus one cheap exposure snapshot. All money
        stays int cc; probabilities stay float (probability space)."""
        risk = self._book_risk_for_check()
        # Snapshot generation vs the live position generation (P0-2 consistency).
        live_generation = self._exposure.position_generation
        snap = self._book_risk
        snap_generation = snap.input_generation if snap is not None else None
        snap_age_s: float | None = None
        if self._book_risk_mono_ns is not None:
            snap_age_s = round(
                (self._clock.monotonic_ns() - self._book_risk_mono_ns) / 1e9, 3
            )
        # Tail axes come from the SAME view the caps read: usable ⇒ the live snapshot
        # (which _book_risk_for_check returns only when generation-matched + fresh);
        # unusable ⇒ the caps fail closed and there is no trustworthy tail number, so
        # the audit reports None (never a stale/convenient value).
        risk_usable = risk is not None and risk.usable
        es_99_cc: int | None = None
        det_loss_cc: int | None = None
        p_ruin: float | None = None
        p_ruin_upper: float | None = None
        if risk is not None and risk.usable:
            es_99_cc = int(risk.governing_model_es_99_cc)
            det_loss_cc = int(risk.deterministic_max_loss_cc)
            p_ruin = round(risk.p_ruin, 4)
            p_ruin_upper = round(getattr(risk, "p_ruin_upper", risk.p_ruin), 4)
        # Model split + residual read off the raw snapshot (present iff usable here).
        production_es_cc: int | None = None
        challenger_es_cc: int | None = None
        bridge_es_cc: int | None = None
        bridge_active = False
        es_residual_cc: int | None = None
        if risk_usable and snap is not None and snap.usable:
            production_es_cc = int(snap.production_es_99_cc)
            challenger_es_cc = int(snap.challenger_es_99_cc)
            bridge_es_cc = int(snap.bridge_es_99_cc)
            bridge_active = bool(snap.bridge_active)
            # Residual = how much the challenger/bridge widened the governing tail
            # over the production copula (0 ⇒ production is the governing model).
            es_residual_cc = int(
                snap.governing_model_es_99_cc - snap.production_es_99_cc
            )
        # Gross + mutex-aware directional bound from one cheap exposure snapshot
        # (the same mass-acceptance aggregation the directional/gross caps bind on).
        exposure = self._exposure.snapshot(self._marginals, mass_acceptance=True)
        gross_cc = int(exposure.gross_notional_cc)
        direction_cc = (
            max((abs(v) for v in exposure.directional_by_game_cc.values()), default=0)
        )
        reservations = (
            self._reservation.outstanding_count if self._reservation is not None else 0
        )
        # If the caller did not name a fallback but the tail is unusable on a
        # non-empty book, record the fail-closed reason so the audit never shows a
        # silently-missing tail without saying why.
        if not fallback_reason and self._exposure.positions and not risk_usable:
            if snap is None or self._book_risk_mono_ns is None:
                fallback_reason = "book_risk_never_measured"
            elif snap_generation != live_generation:
                fallback_reason = "book_risk_generation_stale"
            elif snap_age_s is not None and snap_age_s > self._config.book_risk_stale_after_s:
                fallback_reason = "book_risk_aged_out"
            else:
                fallback_reason = "book_risk_unusable"
        return {
            "snapshot_generation": snap_generation,
            "live_generation": live_generation,
            "snapshot_age_s": snap_age_s,
            "candidate_ev_cc": candidate_ev_cc,
            "es_99_cc": es_99_cc,
            "p_ruin": p_ruin,
            "p_ruin_upper": p_ruin_upper,
            "deterministic_max_loss_cc": det_loss_cc,
            "gross_cc": gross_cc,
            "direction_cc": direction_cc,
            "reservations": reservations,
            "production_es_99_cc": production_es_cc,
            "challenger_es_99_cc": challenger_es_cc,
            "bridge_es_99_cc": bridge_es_cc,
            "bridge_active": bridge_active,
            "es_residual_cc": es_residual_cc,
            "fallback_reason": fallback_reason,
            "binding_cap": binding_cap,
        }

    def _candidate_edge_cc(
        self, fair_cc: int, bid_cc: int, qty: CentiContracts, our_side: Side
    ) -> int | None:
        """Expected edge (candidate EV) of taking ``our_side`` at ``bid_cc`` on a
        combo whose YES fair is ``fair_cc``, for ``qty`` centi-contracts, in int cc.

        YES side: (fair − bid)·contracts. NO side: settles on the COMPLEMENT, so the
        side-fair is $1 − fair — but ONLY when ``combo_no_pays_complement`` is
        verified True; unverified ⇒ None (the NO payout is UNKNOWN and is never an
        assumed complement, defense #5 / hard rule 6). Mirrors the fill ledger's
        ``expected_edge_cc`` so the audited EV equals the recorded EV to the cent."""
        contracts = int(qty)
        if our_side is Side.YES:
            return (int(fair_cc) - int(bid_cc)) * contracts // 100
        if self._conventions.combo_no_pays_complement:
            side_fair = CC_PER_DOLLAR - int(fair_cc)
            return (side_fair - int(bid_cc)) * contracts // 100
        return None

    def _quote_candidate_ev_cc(
        self, result: ConstructedQuote, qty: CentiContracts
    ) -> int | None:
        """Candidate EV for a QUOTE (before any accept): the edge of the
        BETTER-priced quoted side — the side whose cheaper bid buys the most
        contracts on a target-cost accept and is the likelier take. Skips a declined
        (0-bid) side; None when neither side is priced (nothing to quote) or the NO
        side's payout is UNKNOWN (unverified complement — never assumed)."""
        yes_bid = int(result.yes_bid_cc)
        no_bid = int(result.no_bid_cc)
        fair = int(result.fair_cc)
        candidates: list[int] = []
        if yes_bid > 0:
            ev = self._candidate_edge_cc(fair, yes_bid, qty, Side.YES)
            if ev is not None:
                candidates.append(ev)
        if no_bid > 0:
            ev = self._candidate_edge_cc(
                fair, no_bid, qty, self._conventions.maker_position_side(Side.NO)
            )
            if ev is not None:
                candidates.append(ev)
        return max(candidates) if candidates else None

    def _log_quote_risk_audit(
        self,
        rfq: Rfq,
        result: ConstructedQuote,
        qty: CentiContracts,
        *,
        binding_cap: str = "",
    ) -> None:
        """P2-4: emit the consolidated ``risk_audit`` line for a quote decision
        (sent when ``binding_cap`` is "", risk-declined otherwise)."""
        log.info(
            "risk_audit",
            phase="quote",
            rfq_id=rfq.rfq_id,
            reason=binding_cap or str(ReasonCode.QUOTE_SENT),
            **self._risk_audit_fields(
                candidate_ev_cc=self._quote_candidate_ev_cc(result, qty),
                binding_cap=binding_cap,
                fallback_reason="",
            ),
        )

    # --------------------------------------------------------- fill velocity

    def _record_fill_velocity(self, bid: CentiCents, qty: CentiContracts) -> None:
        """Record one ACCEPTED fill in the velocity window at the instant its
        ``pending_fill`` is set. Committed notional = premium at risk =
        contracts x bid (the LOSS axis, ``contracts·price//100``), matching the
        capital a confirmed fill actually puts at risk."""
        committed_cc = int(qty) * int(bid) // 100
        self._fill_velocity.record(committed_cc)

    def _fill_velocity_verdict(self) -> tuple[str, str]:
        """Evaluate the trailing-window velocity against the configured limits.

        Returns ``(verdict, detail)`` where verdict is:
          - "halt"    committed notional over the HARD frac of bankroll ⇒ the
                      caller HALTs (HALT_FILL_VELOCITY);
          - "decline" committed notional over the SOFT frac OR the fill COUNT over
                      max_fills ⇒ the caller DECLINEs further confirms +
                      cancels-all resting quotes;
          - "ok"      within limits.
        Fail-closed on a STALE bankroll (hard rule 6): the %-of-bankroll notional
        thresholds cannot be computed, so they are SKIPPED (never defaulted to
        fine), but the bankroll-free COUNT limit STILL BINDS — a runaway
        acceptance rate is capped even in the dark. HALT dominates DECLINE.

        SHADOW-consistent with the R2 caps: when ``caps_shadow_mode`` is True the
        whole R2 risk layer is log-only, so the governor still records + LOGS a
        would-be breach but returns "ok" (never declines/halts). Only when the
        caps are ENFORCED (the wire-live default) does it bite."""
        limits = self._limits.limits
        state = self._fill_velocity.state()
        bankroll = self._risk_bankroll_cc()
        verdict = "ok"
        detail = ""
        if bankroll is not None and bankroll > 0:
            hard_thr = threshold_cc(limits.fill_velocity_hard_frac, bankroll)
            soft_thr = threshold_cc(limits.fill_velocity_soft_frac, bankroll)
            if state.committed_cc > hard_thr:
                verdict, detail = (
                    "halt",
                    f"committed {state.committed_cc}cc > "
                    f"{limits.fill_velocity_hard_frac} bankroll = {hard_thr}cc "
                    f"in {limits.fill_velocity_window_s}s (count={state.count})",
                )
            elif state.committed_cc > soft_thr:
                verdict, detail = (
                    "decline",
                    f"committed {state.committed_cc}cc > "
                    f"{limits.fill_velocity_soft_frac} bankroll = {soft_thr}cc "
                    f"in {limits.fill_velocity_window_s}s (count={state.count})",
                )
        # COUNT limit — bankroll-free, so it binds even when the bankroll is stale.
        if verdict == "ok" and state.count > limits.fill_velocity_max_fills:
            verdict, detail = (
                "decline",
                f"fill count {state.count} > max {limits.fill_velocity_max_fills} "
                f"in {limits.fill_velocity_window_s}s",
            )
        if verdict != "ok" and limits.caps_shadow_mode:
            # SHADOW: log the would-be fill-velocity action but do NOT enforce it,
            # matching the R2 shadow guarantee (the whole risk layer is log-only).
            log.info(
                "fill_velocity_shadow",
                would_be=verdict,
                detail=detail,
                committed_cc=state.committed_cc,
                count=state.count,
            )
            return ("ok", "")
        return (verdict, detail)

    def _partition_breaches(self, breaches: list[Breach]) -> list[Breach]:
        """Split R2 SHADOW breaches (log-only) from enforced breaches.

        SHADOW GUARANTEE: shadow breaches are LOGGED (structured — reason code,
        the cap, the bankroll, the detail) but are DROPPED from the returned list,
        so they can never remove a quote, block a confirm, or trigger a halt. Only
        enforced (shadow=False) breaches are returned to the caller. This is the
        one place shadow is enforced-away, so every check() call site is
        shadow-safe by construction. (The starvation watchdog is driven separately
        in ``handle_rfq``, on the ISSUE decision, so it observes shadow would-be
        declines even though those quotes still go out.)
        """
        enforced: list[Breach] = []
        for breach in breaches:
            if breach.shadow:
                log.info(
                    "risk_cap_shadow_breach",
                    reason=str(breach.reason),
                    detail=breach.detail,
                    bankroll_cc=self._risk_bankroll_cc(),
                )
            else:
                enforced.append(breach)
        return enforced

    async def _candidate_gate_verdict(
        self, quote_id: str, state: OpenQuoteState
    ) -> tuple[bool, str]:
        """P0-1 candidate-aware portfolio-risk gate for ONE contemplated fill.

        Returns ``(True, "")`` to PROCEED to the existing fill-velocity / reservation
        / confirm flow, or ``(False, detail)`` to DECLINE with
        ``DECLINE_CANDIDATE_RISK``. STRICTLY ADDITIVE — reachable only after the
        existing gates ADMIT the fill, and it can only DECLINE, never admit.

        Builds the candidate ``OpenPosition`` via the SHARED ``_fill_position``
        builder (the exact position a confirmed fill produces — no reinvented
        sign/side/max-loss) and evaluates it against the committed positions + all
        outstanding reservations on COMMON sampled states. Runs OFF the loop via
        ``BookRiskPool.run_candidate`` when a pool is wired (the CPU-bound MC never
        blocks the heartbeat); falls back to an INLINE eval otherwise (paper /
        backtests / tests — fast there). FAIL-CLOSED: an UNKNOWN merged marginal, an
        over-budget POST book, OR ANY exception in the off-loop eval declines (an
        unmeasured or errored joint tail is never safe — never confirm on it)."""
        try:
            inputs = self._build_candidate_gate_inputs(quote_id, state)
            if self._book_risk_pool is not None:
                result = await self._book_risk_pool.run_candidate(inputs)
            else:
                # Inline fallback (no pool): reuse the pool's OWN worker fn on the
                # loop so the result is byte-identical to the off-loop path.
                result = _worker_candidate_book_risk(inputs)
        except Exception as exc:  # noqa: BLE001 — any error declines (fail-closed)
            log.error(
                "candidate_gate_errored", quote_id=quote_id, error=repr(exc)
            )
            return False, f"candidate gate errored: {exc!r}"
        if result.unknown:
            return False, f"candidate gate UNKNOWN: {result.decline_reason}"
        if not result.confirm:
            return False, (
                f"candidate gate declined: {result.decline_reason} "
                f"(cand_ev_cc={result.candidate_ev_cc:.1f}, "
                f"post_es_cc={result.post.governing_model_es_99_cc:.0f}, "
                f"post_det_cc={result.post.deterministic_max_loss_cc:.0f}, "
                f"post_p_ruin={result.post.p_ruin:.4f})"
            )
        log.info(
            "candidate_gate_confirm",
            quote_id=quote_id,
            candidate_ev_cc=round(result.candidate_ev_cc, 1),
            post_governing_es_cc=int(result.post.governing_model_es_99_cc),
            post_deterministic_max_cc=int(result.post.deterministic_max_loss_cc),
            post_p_ruin=round(result.post.p_ruin, 4),
            n_pre=result.n_pre_positions,
        )
        return True, ""

    def _build_candidate_gate_inputs(
        self, quote_id: str, state: OpenQuoteState
    ) -> CandidateBookRiskInputs:
        """Build the IMMUTABLE, picklable inputs for one off-loop candidate MC.

        On-loop work only: build the candidate position (shared builder), read the
        committed positions + outstanding reservations, resolve every candidate-
        universe leg marginal and within-game pair rho into plain dicts (the live
        feed / SgpParams providers do not pickle), and snapshot the RiskLimits
        budgets. A leg whose marginal is missing is OMITTED from the dict, so the
        worker's provider returns None for it ⇒ the merged model is UNKNOWN ⇒ the
        gate declines (fail-closed — a missing marginal is never a usable p=0.5)."""
        candidate = self._fill_position(quote_id, state)
        committed = tuple(self._exposure.positions.values())
        reservations = (
            tuple(self._reservation.outstanding_positions())
            if self._reservation is not None
            else ()
        )
        # Universe of distinct leg tickers across the merged book.
        tickers: set[str] = set()
        for pos in (*committed, *reservations, candidate):
            for leg in pos.legs:
                tickers.add(leg.market_ticker)
        # Resolve marginals ON-LOOP; a missing marginal is OMITTED (⇒ None in the
        # worker ⇒ UNKNOWN model ⇒ decline). Never fabricate a p=0.5 (defense #2).
        marginals: dict[str, float] = {}
        for ticker in tickers:
            p = self._marginals(ticker)
            if p is not None:
                marginals[ticker] = float(p)
        # Resolve within-game pair rho ON-LOOP for every distinct unordered pair
        # (build_book_model queries only same-game pairs; resolving all is a
        # harmless superset). Only the pairs with a band are stored; a pair the
        # provider maps to None is omitted (the worker provider then returns None
        # for it, exactly as the live provider would).
        rho_pairs: dict[frozenset[str], tuple[float, float, float]] = {}
        if self._within_game_rho is not None:
            ordered = sorted(tickers)
            for i in range(len(ordered)):
                for j in range(i + 1, len(ordered)):
                    band = self._within_game_rho(ordered[i], ordered[j])
                    if band is not None:
                        rho_pairs[frozenset((ordered[i], ordered[j]))] = band
        limits = self._limits.limits
        bankroll_cc = self._risk_bankroll_cc()
        # COST-basis equity for P(ruin) — the same basis recompute_book_risk uses,
        # but computed on the MERGED (committed + reservations + candidate) model so
        # the ruin floor is measured against the post-fill portfolio's cost basis.
        merged_model = build_book_model(
            [*committed, *reservations, candidate],
            marginals=self._marginals,
            within_game_rho=self._within_game_rho,
        )
        current_equity_cc = self._ruin_equity_basis_cc(merged_model)
        return CandidateBookRiskInputs(
            committed=committed,
            candidate=candidate,
            reservations=reservations,
            marginals=marginals,
            within_game_rho_pairs=rho_pairs,
            structural_cfg=self._structural_cfg,
            n_samples=self._config.candidate_gate_mc_samples,
            seed=self._config.book_risk_seed,
            band="high",
            bankroll_cc=bankroll_cc,
            current_equity_cc=current_equity_cc,
            ruin_floor_frac=self._config.ruin_floor_frac,
            ruin_prob_ci_z=self._config.ruin_prob_ci_z,
            # The SAME %-of-bankroll / probability budgets the analytic caps use
            # (RiskLimits). None-safe: a None fraction simply is not gated here (the
            # LimitChecker still enforces the full set — this only ADDS the joint tail
            # credit/charge, never loosens a cap).
            portfolio_cvar_frac=float(limits.portfolio_cvar_frac),
            portfolio_det_max_frac=float(limits.portfolio_det_max_frac),
            portfolio_ruin_prob_budget=float(limits.portfolio_ruin_prob_budget),
            absolute_notional_multiple=limits.absolute_notional_multiple,
            # Negative-EV hedges are DISABLED here (no explicit enabled hedge-cost
            # budget) — the spec's SAFETY DEFAULT (P0-1: do not accept negative-EV
            # hedges without an explicit enabled budget).
            hedge_cost_budget_cc=0,
            allow_negative_ev_hedge=False,
        )

    def _reserve_headroom(
        self, reservation_id: str, quote_id: str, state: OpenQuoteState
    ) -> bool:
        """Reserve risk headroom for a contemplated fill BEFORE the confirm
        round-trip (R3 Phase 3). Returns True to proceed with the confirm, False
        to decline.

        No reservation service ⇒ always True (behaviour unchanged from Phase 2 —
        the check already ran at last look; the race only matters under fan-out).
        With a service, the reservation re-checks the caps against
        committed + all outstanding reservations + this fill, atomically, and
        consumes the headroom on grant. Denied ⇒ False (an ENFORCED cap breach —
        impossible while caps_shadow_mode is True, so SHADOW-mode behaviour is
        unchanged; real once the operator flips caps to enforce). The reservation
        SHARES the lifecycle's shadow split, so a shadow breach never denies.

        NOTE (conservative, intended): this quote's OWN open-quote record is still
        in the exposure book here (it is dropped only at the end of
        ``on_quote_accepted``), so the reservation snapshot counts this fill's
        economic exposure twice — once as the still-open quote's mass-acceptance
        hypothetical, once as the candidate fill. That over-counts (never
        under-counts) the headroom for THIS reservation — the same fail-conservative
        double-count the last-look check already makes — so a reservation can only
        be denied more readily, never granted against a real breach. It is
        transient: after commit + ``_drop_quote`` the book holds the position once
        and the open quote is gone, so the steady-state total is exact."""
        if self._reservation is None:
            return True
        candidate = self._fill_position(quote_id, state)
        result = self._reservation.try_reserve(
            reservation_id,
            candidate,
            marginals=self._marginals,
            daily_pnl=self.daily_pnl,
            risk_bankroll_cc=self._risk_bankroll_cc(),
            bankroll_source_configured=self._bankroll_source_configured(),
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
            book_risk=self._book_risk_for_check(),
        )
        return result.granted

    def _note_watchdog(self, *, risk_declined: bool) -> None:
        """Feed the starvation watchdog one quote decision. ``risk_declined`` is
        True when the quote WOULD be declined for a risk reason — either an
        ENFORCED breach really blocked it, OR (in shadow mode) an R2 breach
        would have. Consecutive would-be declines with zero clean issues fire the
        WARNING (a mis-set cap or stuck/zero bankroll silently declining
        everything). A clean issue (no risk breach of any kind) resets it."""
        if self._watchdog is None:
            return
        if risk_declined:
            if self._watchdog.record_risk_decline():
                log.warning(
                    "risk_starvation_watchdog",
                    consecutive_declines=self._watchdog.consecutive_declines,
                    detail="consecutive risk-driven declines — a cap may be "
                    "mis-set or the bankroll stuck/zero",
                )
        else:
            self._watchdog.record_quote_issued()

    # ------------------------------------------------------------------ intake

    async def handle_rfq(self, rfq: Rfq) -> None:
        reasons = self._filter.evaluate(rfq)
        if reasons:
            await self._record_skip(rfq, reasons, self._pregame_flow_context(rfq, reasons))
            return
        result = await self._price_async(rfq)
        if isinstance(result, NoQuote):
            await self._record_skip(rfq, [result.reason], {"detail": result.detail})
            return

        # Risk-side size: a quote implicitly covers the RFQ's FULL size (no
        # size field on the wire). Target-cost RFQs convert at the accepted
        # side's price — the cheapest quoted side buys the most contracts, so
        # that ceil is the conservative bound the limits must see.
        risk_qty = self._risk_qty(rfq, result)
        if risk_qty is None:
            await self._record_skip(
                rfq, [ReasonCode.SKIP_CLASSIFIER_UNKNOWN], {"detail": "unresolvable risk size"}
            )
            return

        # Phase 5 (R3 Part A + R2): compute + LOG the inventory skew AND the
        # widen-vs-decline verdict from the book + this candidate. Dark ship:
        # applied_skew_cc is 0 and widen_declines False while both policies are
        # disabled, so the re-price is a bit-identical no-op — a zero-P&L shadow.
        applied_skew_cc, widen_declines = self._quoting_policy(rfq, result, risk_qty)
        if widen_declines:
            # ENABLED widen policy: decline near a cap on concentrating flow
            # rather than post a wide quote (SHADOW mode never reaches here).
            await self._record_skip(rfq, [ReasonCode.SKIP_WIDEN_AVOIDED], {})
            return
        if applied_skew_cc != 0:
            reskewed = await self._price_async(rfq, inventory_skew_cc=applied_skew_cc)
            if isinstance(reskewed, NoQuote):
                await self._record_skip(
                    rfq, [reskewed.reason], {"detail": reskewed.detail}
                )
                return
            result = reskewed
            new_qty = self._risk_qty(rfq, result)
            if new_qty is None:
                await self._record_skip(
                    rfq,
                    [ReasonCode.SKIP_CLASSIFIER_UNKNOWN],
                    {"detail": "unresolvable risk size after skew"},
                )
                return
            risk_qty = new_qty

        quote_risk = self._quote_risk(rfq, result, quote_id="pending", qty=risk_qty)
        raw_breaches = self._limits.check(
            self._exposure,
            self._marginals,
            self.daily_pnl,
            candidate_positions=quote_risk.hypothetical_positions(self._conventions),
            adding_quote=True,
            risk_bankroll_cc=self._risk_bankroll_cc(),
            bankroll_source_configured=self._bankroll_source_configured(),
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
            book_risk=self._book_risk_for_check(),
        )
        # Watchdog sees the ISSUE decision: any breach (enforced OR shadow) is a
        # would-be decline; only a fully clean check is a real issue (reset). This
        # lets a mis-set cap surface in SHADOW mode even though the quote goes out.
        self._note_watchdog(risk_declined=bool(raw_breaches))
        breaches = self._partition_breaches(raw_breaches)
        if breaches:
            await self._record_skip(
                rfq, [b.reason for b in breaches], {"details": [b.detail for b in breaches]}
            )
            # P2-4: audit the risk-declined quote — the binding cap is the FIRST
            # enforced breach (checks are severity-ordered), so the operator sees
            # exactly which cap blocked the quote alongside the full tail context.
            self._log_quote_risk_audit(
                rfq, result, risk_qty, binding_cap=str(breaches[0].reason)
            )
            return

        try:
            response = await self._sender.create_quote(
                rfq.rfq_id,
                yes_bid_cc=result.yes_bid_cc,
                no_bid_cc=result.no_bid_cc,
            )
        except KalshiApiError as exc:
            # rfq_closed / 409: the RFQ's ~1s window closed before our POST landed
            # — a NORMAL taker-race loss (we were not first), NOT a failure. Count
            # it (the win-the-taker signal) and decline quietly; any other API
            # error is real and propagates to the worker's error path.
            if exc.code == "rfq_closed" or exc.status == 409:
                self._metrics.inc("quote.rfq_closed_before_post")
                await self._record_skip(
                    rfq,
                    [ReasonCode.SKIP_RFQ_CLOSED],
                    {"detail": "rfq window closed before our quote POST landed"},
                )
                return
            raise
        quote_id = str(response.get("id") or response.get("quote_id") or "")
        if not quote_id:
            log.warning("quote_created_without_id", rfq_id=rfq.rfq_id, response=response)
            return
        state = OpenQuoteState(
            quote_id=quote_id,
            rfq=rfq,
            constructed=result,
            leg_mids_cc=self._current_leg_mids(rfq),
            created_mono_ns=self._clock.monotonic_ns(),
            risk_qty=risk_qty,
        )
        # Replacement semantics: a new quote on the same RFQ replaces ours.
        old_quote_id = self._by_rfq.get(rfq.rfq_id)
        if old_quote_id:
            self._drop_quote(old_quote_id)
        self._open[quote_id] = state
        self._by_rfq[rfq.rfq_id] = quote_id
        self._exposure.upsert_quote(
            self._quote_risk(rfq, result, quote_id=quote_id, qty=risk_qty)
        )
        self._metrics.inc("quote.sent")
        await self._store.record_decision(
            "quote_sent",
            rfq.rfq_id,
            [str(ReasonCode.QUOTE_SENT)],
            {
                "quote_id": quote_id,
                "yes_bid_cc": int(result.yes_bid_cc),
                "no_bid_cc": int(result.no_bid_cc),
                "fair_cc": int(result.fair_cc),
                "width_cc": result.width_components_cc,
                "leg_mids_cc": state.leg_mids_cc,
            },
        )
        # P2-4: one consolidated risk-audit line per quote. The candidate EV is the
        # BETTER-priced quoted side's edge (the side more likely to be taken; the
        # cheaper bid buys the most contracts on a target-cost accept). No binding
        # cap / fallback — this quote cleared every gate to be sent.
        self._log_quote_risk_audit(rfq, result, risk_qty)

    # ------------------------------------------------------- accept → confirm

    async def on_quote_accepted(self, msg: JsonDict) -> None:
        t0 = self._clock.monotonic_ns()
        quote_id = str(msg.get("quote_id", ""))
        state = self._open.get(quote_id)
        if state is None:
            log.warning("accept_for_unknown_quote", quote_id=quote_id)
            return
        state.accepted = True
        # Fill-path visibility (2026-07-14 fill-killer diagnosis): accepts are
        # rare (~tens/day), so log the size-bearing fields of every accept. This
        # confirms the live wire shape and surfaces any field-name drift from the
        # log alone — the accepted size lives in no_contracts_fp/yes_contracts_fp.
        log.info(
            "quote_accepted",
            quote_id=quote_id,
            msg_keys=sorted(msg.keys()),
            msg=msg,
        )
        accepted_raw = str(msg.get("accepted_side", ""))
        if accepted_raw not in ("yes", "no"):
            # Can't know which side we'd be filling — lapse, never guess.
            await self._record_confirm_decision(
                state, confirm=False, reason=ReasonCode.DECLINE_FAIR_MOVED_JOINT,
                detail=f"accepted_side unreadable: {accepted_raw!r}", decision_ms=0.0,
            )
            self._drop_quote(quote_id)
            return
        accepted_side = Side(accepted_raw)
        bid = (
            state.constructed.yes_bid_cc
            if accepted_side is Side.YES
            else state.constructed.no_bid_cc
        )
        if int(bid) <= 0:
            # The accepted side was DECLINED (0 bid): a normal single-sided
            # quote, or the YES side of a farmed impossible combo. We never
            # priced this side, so we never confirm a fill on it — for a farm
            # this is the hard guard that we can NEVER end up long the worthless
            # YES. Deliberate lapse.
            await self._record_confirm_decision(
                state, confirm=False, reason=ReasonCode.DECLINE_SIDE_NOT_QUOTED,
                detail=f"accept on declined side {accepted_side} (bid=0)", decision_ms=0.0,
            )
            self._metrics.inc(f"confirm.declined.{ReasonCode.DECLINE_SIDE_NOT_QUOTED}")
            self._drop_quote(quote_id)
            return
        qty = self._accepted_qty(state, accepted_side, msg)
        if qty is None:
            # Unknown accepted size (defense #2): never confirm a fill we
            # cannot size — deliberate lapse. Record EVERY size field we know
            # about so wire-field drift is diagnosable from the ledger alone
            # (this exact read was the 2026-07-14 fill-killer).
            size_fields = {
                k: msg.get(k)
                for k in (
                    "contracts_accepted_fp",
                    "no_contracts_offered_fp",
                    "yes_contracts_offered_fp",
                    "rfq_target_cost_dollars",
                )
            }
            await self._record_confirm_decision(
                state, confirm=False, reason=ReasonCode.DECLINE_SIZE_UNKNOWN,
                detail=f"no readable accepted size; fields={size_fields}",
                decision_ms=(self._clock.monotonic_ns() - t0) / 1e6,
            )
            self._metrics.inc(f"confirm.declined.{ReasonCode.DECLINE_SIZE_UNKNOWN}")
            self._drop_quote(quote_id)
            return
        our_side = self._conventions.maker_position_side(accepted_side)
        if our_side is Side.NO and self._conventions.combo_no_pays_complement is None:
            # NO-side settlement semantics unverified (Phase 2.5): refusing is
            # the only honest option until ground truth fills the fixture.
            await self._record_confirm_decision(
                state, confirm=False, reason=ReasonCode.DECLINE_CONVENTION_UNKNOWN,
                detail="combo_no_pays_complement unverified",
                decision_ms=(self._clock.monotonic_ns() - t0) / 1e6,
            )
            self._metrics.inc(f"confirm.declined.{ReasonCode.DECLINE_CONVENTION_UNKNOWN}")
            self._drop_quote(quote_id)
            return

        inputs = self._last_look_inputs(state, accepted_side, bid, qty)
        decision = decide_confirm(inputs, self._policy)
        decision_ms = (self._clock.monotonic_ns() - t0) / 1e6
        self._metrics.observe_ms("confirm.decision_ms", decision_ms)

        if decision.confirm:
            # Park state BEFORE the network call: if the confirm times out
            # client-side it may still have landed server-side, and the
            # eventual quote_executed must find this state and book the fill.
            state.pending_fill = (accepted_side, bid, qty)
            self._executed_states[quote_id] = state
            # P0-1 CANDIDATE-AWARE PORTFOLIO-RISK GATE (last look). The existing
            # analytic/gross/burst gates have ADMITTED this fill; now run an
            # ADDITIONAL candidate-aware ~20k-sample portfolio MC over the merged
            # PRE (committed + outstanding reservations) + candidate book and confirm
            # ONLY when the candidate's marginal EV is positive AND the POST-book
            # joint-tail / ruin / deterministic / gross budgets pass. STRICTLY
            # ADDITIVE: reachable only inside `if decision.confirm`, so it can only
            # flip an ADMIT to a DECLINE, never a decline to an admit. Runs OFF the
            # loop (BookRiskPool.run_candidate) so the CPU-bound MC never blocks the
            # heartbeat; confirms are rare and the confirm window is 3s, so awaiting
            # it here is fine. UNKNOWN merged marginal / over-budget POST book / ANY
            # off-loop error ⇒ DECLINE_CANDIDATE_RISK (fail-closed — an unmeasured or
            # errored joint tail is never safe). Disabled by config ⇒ skipped (kill
            # switch + prior behaviour).
            if self._config.candidate_gate_enabled:
                gate_ok, gate_detail = await self._candidate_gate_verdict(
                    quote_id, state
                )
                if not gate_ok:
                    self._metrics.inc(
                        f"confirm.declined.{ReasonCode.DECLINE_CANDIDATE_RISK}"
                    )
                    self._track_markout(f"declined:{quote_id}", state)
                    await self._record_confirm_decision(
                        state, confirm=False,
                        reason=ReasonCode.DECLINE_CANDIDATE_RISK,
                        detail=gate_detail, decision_ms=decision_ms,
                    )
                    self._executed_states.pop(quote_id, None)
                    state.pending_fill = None
                    self._drop_quote(quote_id)
                    return
            # FILL-VELOCITY GOVERNOR (wire-live): record this acceptance's
            # committed notional in the rolling window (the point pending_fill is
            # set), then evaluate the rate. A burst over the SOFT frac / max fills
            # DECLINEs this confirm + cancels-all resting quotes; over the HARD
            # frac HALTs. The COUNT limit binds even on a stale bankroll. Evaluated
            # BEFORE the reservation/round-trip so a runaway rate never confirms.
            self._record_fill_velocity(bid, qty)
            fv_verdict, fv_detail = self._fill_velocity_verdict()
            if fv_verdict != "ok":
                if fv_verdict == "halt":
                    await self._killswitch.halt(
                        ReasonCode.HALT_FILL_VELOCITY, fv_detail
                    )
                    # halt callbacks (cancel-all) already ran; still record the
                    # declined confirm + back out this fill below.
                self._metrics.inc(
                    f"confirm.declined.{ReasonCode.DECLINE_FILL_VELOCITY}"
                )
                self._track_markout(f"declined:{quote_id}", state)
                await self._record_confirm_decision(
                    state, confirm=False, reason=ReasonCode.DECLINE_FILL_VELOCITY,
                    detail=fv_detail, decision_ms=decision_ms,
                )
                self._executed_states.pop(quote_id, None)
                state.pending_fill = None
                # DECLINE further confirms + cancel-all resting quotes (a soft
                # decline; a hard halt already cancelled-all via its callbacks, but
                # cancel_all is idempotent so this is safe either way).
                await self.cancel_all(ReasonCode.DECLINE_FILL_VELOCITY)
                self._drop_quote(quote_id)
                return
            # R3 Phase 3: RESERVE headroom BEFORE the confirm round-trip (atomic +
            # versioned). If the reservation is ENFORCED-denied — impossible in
            # Phase-2 SHADOW mode, real once caps are flipped — we do NOT confirm;
            # we decline instead (the last book of headroom went to another RFQ).
            reservation_id = f"fill:{quote_id}"
            reserved = self._reserve_headroom(reservation_id, quote_id, state)
            if not reserved:
                self._metrics.inc(
                    f"confirm.declined.{ReasonCode.DECLINE_RISK_LIMIT}"
                )
                self._track_markout(f"declined:{quote_id}", state)
                await self._record_confirm_decision(
                    state, confirm=False, reason=ReasonCode.DECLINE_RISK_LIMIT,
                    detail="risk reservation denied at confirm (no headroom)",
                    decision_ms=decision_ms,
                )
                self._executed_states.pop(quote_id, None)
                state.pending_fill = None
                self._drop_quote(quote_id)
                return
            rtt0 = self._clock.monotonic_ns()
            try:
                await self._sender.confirm_quote(quote_id)
                self._metrics.observe_ms(
                    "confirm.rtt_ms", (self._clock.monotonic_ns() - rtt0) / 1e6
                )
                self._metrics.inc("confirm.sent")
                # Once confirmed neither party can withdraw: the position is
                # REAL now — book it immediately, not at quote_executed
                # (execution is ~1s later and the channel has no replay).
                # COMMIT the reservation (which books the position) — or, with no
                # reservation service, book directly. Both are idempotent on id.
                if self._reservation is not None:
                    self._reservation.commit(reservation_id)
                else:
                    self._book_position(quote_id, state)
            except Exception as exc:
                self._metrics.inc("confirm.failed")
                self._confirm_failures += 1
                log.error("confirm_failed", quote_id=quote_id, error=repr(exc))
                # Confirm TIMED OUT: unknown-committed. ASSUME COMMITTED — keep the
                # reserved headroom held (mark_unconfirmed) so a possibly-real
                # position keeps counting against the caps until reconciled against
                # the exchange. Never release on a lost ack.
                if self._reservation is not None:
                    self._reservation.mark_unconfirmed(reservation_id)
                if self._confirm_failures >= 3:
                    await self._killswitch.halt(
                        ReasonCode.HALT_CONFIRM_TIMEOUTS,
                        f"{self._confirm_failures} consecutive confirm failures",
                    )
        else:
            self._metrics.inc(f"confirm.declined.{decision.reason}")
            self._track_markout(f"declined:{quote_id}", state)
        await self._record_confirm_decision(
            state,
            confirm=decision.confirm,
            reason=decision.reason,
            detail=decision.detail,
            decision_ms=decision_ms,
        )
        # Accepted quotes are no longer open either way.
        self._drop_quote(quote_id)

    def _fill_position(self, quote_id: str, state: OpenQuoteState) -> OpenPosition:
        """The exact ``OpenPosition`` a confirmed fill of this quote produces.

        The SINGLE builder shared by ``_book_position`` and the reservation
        service, so the headroom RESERVED before confirm equals the position
        BOOKED after confirm to the cent (position_id, side, contracts, price and
        legs are byte-identical — no drift between reserve and commit)."""
        assert state.pending_fill is not None
        accepted_side, bid, qty = state.pending_fill
        return OpenPosition(
            position_id=f"fill:{quote_id}",
            combo_ticker=state.rfq.market_ticker,
            collection=state.rfq.mve_collection_ticker,
            our_side=self._conventions.maker_position_side(accepted_side),
            contracts=qty,
            entry_price_cc=bid,
            legs=self._leg_refs(state.rfq),
            farmed=state.constructed.farmed,
        )

    def _book_position(self, quote_id: str, state: OpenQuoteState) -> None:
        """Idempotent: adds the confirmed fill's position to the exposure book.

        When a reservation service is wired, the booking flows through
        ``reservation.commit`` (the reservation IS this same position, same id),
        so this is a no-op for an already-committed id. Kept as the fallback
        booking path when no reservation service is present, and as the
        idempotency backstop for the ``on_quote_executed`` replay."""
        position = self._fill_position(quote_id, state)
        if position.position_id in self._exposure.positions:
            return
        self._exposure.add_position(position)

    async def on_quote_executed(self, msg: JsonDict) -> None:
        quote_id = str(msg.get("quote_id", ""))
        # Full-message capture (2026-07-14): the LIVE combo quote_accepted carries
        # NO contract-count field, so the accepted size may only be knowable here.
        log.info(
            "quote_executed_msg",
            quote_id=quote_id,
            msg_keys=sorted(msg.keys()),
            msg=msg,
        )
        state = self._executed_states.get(quote_id) or self._open.get(quote_id)
        if state is None:
            log.warning("execution_for_unknown_quote", quote_id=quote_id)
            return
        if state.pending_fill is None:
            log.warning("execution_without_pending_fill", quote_id=quote_id)
            return
        accepted_side, bid, qty = state.pending_fill
        # Book the fill. With a reservation service, execution CONFIRMS the fill
        # landed — commit the reservation (converts a still-outstanding
        # reservation, e.g. one whose confirm timed out and was marked
        # unconfirmed, into a committed position exactly once; a no-op if the
        # confirm already committed it). Without a service, book directly. Both
        # are idempotent on the position id, so a replayed execution is safe.
        if self._reservation is not None:
            reservation_id = f"fill:{quote_id}"
            if not self._reservation.commit(reservation_id):
                # Not outstanding (already committed at confirm, or a replay) —
                # ensure the position exists in the book anyway (idempotent).
                self._book_position(quote_id, state)
        else:
            self._book_position(quote_id, state)  # no-op if booked at confirm
        our_side = self._conventions.maker_position_side(accepted_side)
        expected_edge_cc: int | None
        if our_side is Side.YES:
            expected_edge_cc = (int(state.constructed.fair_cc) - int(bid)) * int(qty) // 100
        elif self._conventions.combo_no_pays_complement:
            side_fair = CC_PER_DOLLAR - int(state.constructed.fair_cc)
            expected_edge_cc = (side_fair - int(bid)) * int(qty) // 100
        else:
            # NO payout semantics unverified — an honest ledger records
            # UNKNOWN, never an assumed complement (defense #5).
            expected_edge_cc = None
        # Real fill fee from the fee model (defense #3): $0 for our combo maker
        # quadratic fills, correct for any nonzero-fee series. None only when no
        # fee model is wired (pre-Phase-6 behaviour) or the fee is UNKNOWN.
        fill_fee_cc = self._fill_fee_cc(bid, qty)
        await self._store.record_fill(
            f"fill:{quote_id}",
            order_id=str(msg.get("order_id")) if msg.get("order_id") else None,
            combo_ticker=state.rfq.market_ticker,
            our_side=str(our_side),
            contracts_centi=int(qty),
            price_cc=int(bid),
            fee_cc=fill_fee_cc,
            expected_edge_cc=expected_edge_cc,
            raw=msg,
        )
        # The trade fee is a real cash cost AT FILL — it must enter the realized
        # ledger the ENFORCED daily-loss cap reads, not only the settlement fee
        # (else, on a nonzero-fee series, realized P&L understates costs by the
        # trade fee and the cap sees a rosier figure than reality). $0 today for
        # our quadratic maker fills, so no behaviour change now; correct for any
        # nonzero-fee series. A None (no fee model / UNKNOWN) fee is NOT booked as
        # a convenient 0 (defense #2) — the live balance poll remains the backstop
        # that captures the actual cash movement.
        if fill_fee_cc is not None and fill_fee_cc != 0:
            self.record_realized_pnl(-int(fill_fee_cc))
        self._metrics.inc("fill.count")
        self._track_markout(f"fill:{quote_id}", state)

    def _fill_fee_cc(self, bid: CentiCents, qty: CentiContracts) -> int | None:
        """The fee our fill is charged, in cc, from the real fee model
        (pricing/fees.py — never reimplemented). $0 for our combo maker quadratic
        maker fill; correct for a nonzero-fee series. None when no fee model is
        wired OR the fee is genuinely UNKNOWN (flat/unknown fee_type) — an honest
        ledger records UNKNOWN, never a guessed 0 (defense #2)."""
        if self._fee_model is None:
            return None
        try:
            return int(
                self._fee_model.trade_fee_cc(
                    price_cc=bid,
                    qty=qty,
                    fee_type=self._fee_type,
                    multiplier=self._fee_multiplier,
                )
            )
        except FeeUnknownError:
            return None

    # ------------------------------------------------------------ maintenance

    async def on_rfq_deleted(self, rfq_id: str, _msg: JsonDict) -> None:
        quote_id = self._by_rfq.get(rfq_id)
        if quote_id is None:
            return
        state = self._open.get(quote_id)
        if state is not None and not state.accepted:
            await self._delete_quote(quote_id, ReasonCode.DELETE_RFQ_GONE)

    def record_realized_pnl(self, delta_cc: int) -> None:
        """Settlement/fee reconciliation feeds realized P&L here (Phase 6)."""
        self._realized_pnl_cc += delta_cc

    async def reconcile_combo_settlement(
        self,
        combo_ticker: str,
        *,
        settled_yes: bool,
        settled_value: float | None = None,
        expected_revenue_cc: int | None = None,
    ) -> None:
        """Settlement reconciliation for a settled combo market (defense #3).

        Two guards, both HALTing ``HALT_RECONCILIATION_MISMATCH`` (never a log):

        1. **Farmed settle-YES tripwire.** A combo we farmed is short-YES / long
           the certain-NO side: it can ONLY settle NO. If it EVER settles YES, our
           impossibility classification (or the settlement window we assumed) was
           wrong on a position — the exact misclassification loss path farming is
           gated against.

        2. **FULL to-the-cent reconcile (Phase 6, code audit 2026-07-13).** When
           the settlement handler supplies ``settled_value`` (V) and the exchange's
           booked ``expected_revenue_cc``, reconcile EVERY settled position on this
           ticker: our predicted gross settlement credit (Σ contracts·payout —
           LONG NO pays $1−V, LONG YES pays V) must equal the exchange ledger's
           revenue TO THE CENT. Any mismatch means our model of the settlement
           (sign / value / convention) is wrong → HALT. Omitting those args keeps
           the farmed-only tripwire (the pre-Phase-6 callers read unchanged).
        """
        on_ticker = [
            pos
            for pos in self._exposure.positions.values()
            if pos.combo_ticker == combo_ticker
        ]
        farmed = [pos for pos in on_ticker if pos.farmed]
        if farmed and settled_yes:
            await self._killswitch.halt(
                ReasonCode.HALT_RECONCILIATION_MISMATCH,
                f"farmed impossible combo {combo_ticker} settled YES on "
                f"{len(farmed)} position(s) — classification/settlement-window failure",
            )
            return
        if expected_revenue_cc is None or settled_value is None:
            return  # farmed-only tripwire path (no ledger figures supplied)
        if not on_ticker:
            return  # nothing we hold on this ticker to reconcile
        predicted_credit_cc = sum(
            self._predicted_settlement_credit_cc(pos, settled_value) for pos in on_ticker
        )
        # Reconcile to the exchange's CENT grid, not to the raw centi-cent. The
        # exchange books `revenue` as an INTEGER number of cents (always a
        # multiple of CC_PER_CENT). Our predicted credit carries sub-cent
        # precision ONLY when a position holds a fractional number of contracts
        # (a target-cost RFQ, e.g. 0.90 ct) and the combo settles SCALAR
        # (V∈(0,1)): `contracts·(1−V)` is then not a whole cent (0.90·$0.57 =
        # 51.3¢), which the integer-cent revenue (51¢ or 52¢) can NEVER equal.
        # A strict `!=` there would spuriously HALT a legitimate settlement.
        # Binary V∈{0,1} and whole-contract scalars stay whole-cent, so this is
        # still EXACT for them (residual 0). A genuine model error (wrong
        # sign/value/convention) shifts the credit by ≥ a full cent, so the
        # strict `< CC_PER_CENT` guard keeps defense #3 intact — only the sub-
        # cent fractional-contract residual is absorbed, and the tolerance is
        # robust to whether the exchange rounds or floors the half-cent (both
        # land < 1¢ away). Residual = 1¢ or more ⇒ still a mismatch ⇒ HALT.
        residual_cc = abs(predicted_credit_cc - expected_revenue_cc)
        if residual_cc >= CC_PER_CENT:
            await self._killswitch.halt(
                ReasonCode.HALT_RECONCILIATION_MISMATCH,
                f"combo {combo_ticker}: predicted settlement credit "
                f"{predicted_credit_cc}cc != exchange revenue {expected_revenue_cc}cc "
                f"(residual {residual_cc}cc ≥ 1¢, V={settled_value}) — "
                f"settlement model mismatch",
            )

    def _predicted_settlement_credit_cc(
        self, position: OpenPosition, settled_value: float
    ) -> int:
        """Our PREDICTED gross settlement credit for one position, in cc — the
        payout the side we hold receives (contracts · payout_per_contract),
        matching the ledger booking and the exchange ``revenue``. LONG NO pays
        $1 − V; LONG YES pays V (same convention frame as balance.apply_settlement,
        DNP "rounded down" via round-to-grid)."""
        contracts = int(position.contracts)
        v_cc = round(settled_value * CC_PER_DOLLAR)
        if position.our_side is Side.NO:
            payout_per_ct = CC_PER_DOLLAR - v_cc
        else:
            payout_per_ct = v_cc
        return contracts * payout_per_ct // 100

    def _refresh_daily_pnl(self) -> None:
        """Mark open positions at current leg mids so the daily-loss limit
        actually binds. Any unmarkable position keeps the previous mark
        (limits also see UNKNOWN marginals as a breach on their own)."""
        unrealized = 0
        for position in self._exposure.positions.values():
            fair = 1.0
            failed = False
            for leg in position.legs:
                p = self._marginals(leg.market_ticker)
                if p is None:
                    failed = True
                    break
                fair *= p if leg.side == "yes" else 1.0 - p
            if failed:
                return  # keep last daily_pnl rather than mark with holes
            if position.our_side is Side.YES:
                payout_prob = fair
            elif self._conventions.combo_no_pays_complement:
                payout_prob = 1.0 - fair
            else:
                return  # unverified NO payout: don't fabricate a mark
            value = int(payout_prob * CC_PER_DOLLAR) * int(position.contracts) // 100
            unrealized += value - position.max_loss_cc
        self.daily_pnl = DailyPnl(realized_cc=self._realized_pnl_cc, unrealized_cc=unrealized)

    def _maybe_recompute_book_risk(self) -> None:
        """Refresh the portfolio-CVaR snapshot off the maintenance tick, throttled
        to half the freshness window so it stays fresh without running a full MC
        every 0.5s.

        With a ``book_risk_pool`` (live async loop): the MC runs in a WORKER PROCESS
        and this LAUNCHES it as a background task, returning IMMEDIATELY — the
        maintenance tick NEVER awaits the MC, so the maintenance loop keeps beating
        the supervisor heartbeat on its 0.5s cadence no matter how long the MC
        takes. A single-flight guard skips launching a new run while the previous
        one is still in flight. Without a pool (paper/backtests/tests): the MC runs
        INLINE (it is fast there) and this stays synchronous.

        The throttle timestamp is set only when a run is actually
        launched/performed, so a skipped tick (still in flight, or inside the
        throttle window) does not slide the window forward."""
        now = self._clock.monotonic_ns()
        interval_ns = int(self._config.book_risk_stale_after_s / 2 * 1e9)
        last = self._book_risk_refresh_mono_ns
        if last is not None and now - last < interval_ns:
            return
        if self._book_risk_pool is None:
            # No worker pool ⇒ the MC is cheap enough to run inline (paper/tests).
            self._book_risk_refresh_mono_ns = now
            self.recompute_book_risk()
            return
        # Single-flight: never stack a second off-loop MC on top of a running one.
        if self._book_risk_task is not None and not self._book_risk_task.done():
            return
        self._book_risk_refresh_mono_ns = now
        # Fire-and-forget: the task publishes its (generation-checked) result when
        # the worker finishes; the maintenance tick returns now and keeps beating.
        self._book_risk_task = asyncio.ensure_future(self.recompute_book_risk_offloop())

    async def maintenance_tick(self) -> None:
        """TTL expiry + reprice + P&L mark + daily-loss halt. Every few 100ms."""
        self._refresh_daily_pnl()
        # Arm/refresh the portfolio-CVaR book-risk snapshot (throttled, off the hot
        # path) BEFORE the check below reads it, so the maintenance-driven halt
        # escalation sees a current joint-tail figure. With a book_risk_pool this
        # LAUNCHES the MC in a worker and returns immediately (never blocks the tick
        # / heartbeat); without one it runs inline (fast in paper/tests).
        self._maybe_recompute_book_risk()
        if not self._killswitch.halted:
            breaches = self._partition_breaches(
                self._limits.check(
                    self._exposure,
                    self._marginals,
                    self.daily_pnl,
                    risk_bankroll_cc=self._risk_bankroll_cc(),
                    bankroll_source_configured=self._bankroll_source_configured(),
                    start_time_provider=self._start_time_provider,
                    halt_inputs=self._halt_inputs(),
                    book_risk=self._book_risk_for_check(),
                )
            )
            for breach in breaches:
                # Any ENFORCED halt-class breach escalates to the killswitch
                # (cancel-all + stop). Shadow breaches were already dropped by
                # _partition_breaches, so a halt reaching here is real. The
                # give-back halts (drawdown / hard-trip) escalate here too — not
                # only the daily-loss halt — so flipping caps to enforce actually
                # arms them (a peak-equity latch now feeds their inputs).
                if breach.reason in (
                    ReasonCode.HALT_DAILY_LOSS,
                    ReasonCode.HALT_DRAWDOWN,
                    ReasonCode.HALT_HARD_TRIP,
                ):
                    await self._killswitch.halt(breach.reason, breach.detail)
                    return  # halt callbacks (cancel-all) already ran
            # FILL-VELOCITY governor, re-evaluated off the maintenance tick so a
            # burst that just landed is caught even between confirms: over the HARD
            # frac HALTs; over the SOFT frac / max fills cancels-all resting quotes
            # (the same DECLINE action, applied to the standing book). The window
            # decays on its own, so this self-clears once the burst ages out.
            fv_verdict, fv_detail = self._fill_velocity_verdict()
            if fv_verdict == "halt":
                await self._killswitch.halt(
                    ReasonCode.HALT_FILL_VELOCITY, fv_detail
                )
                return  # halt callbacks (cancel-all) already ran
            if fv_verdict == "decline" and self._open:
                log.warning("fill_velocity_cancel_all", detail=fv_detail)
                await self.cancel_all(ReasonCode.DECLINE_FILL_VELOCITY)
        now = self._clock.monotonic_ns()
        for quote_id, state in list(self._open.items()):
            if state.accepted:
                continue
            age_s = (now - state.created_mono_ns) / 1e9
            if age_s > self._config.quote_ttl_s:
                await self._delete_quote(quote_id, ReasonCode.DELETE_TTL_EXPIRED)
                continue
            result = await self._price_async(state.rfq)
            if isinstance(result, NoQuote):
                await self._delete_quote(quote_id, ReasonCode.DELETE_LEG_STALE)
                continue
            if abs(int(result.fair_cc) - int(state.constructed.fair_cc)) > (
                self._config.reprice_threshold_cc
            ):
                self._metrics.inc("quote.reprice")
                await self.handle_rfq(state.rfq)  # replacement quote
                if self._by_rfq.get(state.rfq.rfq_id) == quote_id:
                    # Replacement was refused (filter/risk) — a stale quote
                    # must never stay on the wire.
                    await self._delete_quote(quote_id, ReasonCode.DELETE_LEG_MOVED)

    async def cancel_all(self, reason: ReasonCode | str) -> None:
        """Best-effort delete of every open quote. Idempotent, race-safe."""
        open_ids = [qid for qid, s in self._open.items() if not s.accepted]
        if not open_ids:
            return
        log.warning("cancel_all", reason=str(reason), count=len(open_ids))
        results = await asyncio.gather(
            *(self._sender.delete_quote(qid) for qid in open_ids), return_exceptions=True
        )
        for quote_id, result in zip(open_ids, results, strict=True):
            if isinstance(result, Exception):
                log.warning("cancel_all_delete_failed", quote_id=quote_id, error=repr(result))
            self._drop_quote(quote_id)
        self._metrics.inc("quote.cancel_all")

    @property
    def open_quote_count(self) -> int:
        return len(self._open)

    def has_open_quote(self, rfq_id: str) -> bool:
        return rfq_id in self._by_rfq

    def marginal_of(self, market_ticker: str) -> float | None:
        """Public read accessor for a leg's current P(YES) — the SAME provider
        (feed microprice) the pricer and exposure book use. ``None`` when the
        book is missing/invalid (fail-closed: an unreadable leg is UNKNOWN, never
        a guessed value). Exposed so the risk-breaker sampler (quote_app's
        _sample_breaker_inputs) can feed the marginal-jump breaker the exact
        marginals we priced on, without reaching into a private name."""
        return self._marginals(market_ticker)

    # ---------------------------------------------------------------- helpers

    def pricing_stats(self) -> dict[str, float | int]:
        """Live throughput observability (2026-07-14): the joint-memo hit rate and
        off-loop pool counters. The hit rate is the signal that decides whether the
        pre-warm pump (Phase 4) is even needed — a high same-game hit rate means the
        exact memo already covers the hot flow. Logged every status tick."""
        hits, misses, size = self._engine.joint_cache_stats
        total = hits + misses
        stats: dict[str, float | int] = {
            "memo_hits": hits,
            "memo_misses": misses,
            "memo_size": size,
            "memo_hit_rate": round(hits / total, 4) if total else 0.0,
        }
        if self._joint_pool is not None:
            stats["pool_calls"] = self._joint_pool.calls
            stats["pool_timeouts"] = self._joint_pool.timeouts
            stats["pool_errors"] = self._joint_pool.errors
        return stats

    def _price(
        self, rfq: Rfq, *, inventory_skew_cc: int = 0
    ) -> ConstructedQuote | NoQuote:
        time_to_close = self._min_time_to_close_s(rfq)
        return self._engine.price(
            rfq,
            time_to_close_s=time_to_close if time_to_close is not None else -1.0,
            in_play=self._inplay.any_anomalous(list(rfq.leg_tickers)),
            inventory_skew_cc=inventory_skew_cc,
        )

    async def _price_async(
        self, rfq: Rfq, *, inventory_skew_cc: int = 0
    ) -> ConstructedQuote | NoQuote:
        """Async pricing for the hot RFQ path. With a joint pool configured the
        expensive joint step runs off-loop with a deadline (warm memo hits stay
        inline); without one it is exactly ``_price``. Identical $ output to
        ``_price`` — the pool runs the same pure joint code (pool_parity_check).
        A deadline breach or worker error is a fail-closed decline (no wedge)."""
        if self._joint_pool is None:
            return self._price(rfq, inventory_skew_cc=inventory_skew_cc)
        time_to_close = self._min_time_to_close_s(rfq)
        try:
            return await self._engine.price_offloaded(
                rfq,
                time_to_close_s=time_to_close if time_to_close is not None else -1.0,
                in_play=self._inplay.any_anomalous(list(rfq.leg_tickers)),
                inventory_skew_cc=inventory_skew_cc,
                run_joint=self._joint_pool.run_joint,
            )
        except TimeoutError:
            self._metrics.inc("price.pool_deadline_drop")
            return NoQuote(
                ReasonCode.SKIP_PRICE_DEADLINE, "joint pricing exceeded the off-loop deadline"
            )
        except Exception:
            log.exception("price_pool_error", rfq_id=rfq.rfq_id)
            self._metrics.inc("price.pool_error")
            return NoQuote(ReasonCode.SKIP_PRICING_FAILED, "off-loop pricing error")

    def _quoting_policy(
        self, rfq: Rfq, constructed: ConstructedQuote, risk_qty: CentiContracts
    ) -> tuple[int, bool]:
        """Compute + LOG the inventory skew AND the widen-vs-decline verdict for
        this quote (R3 Part A + Part R2). Returns ``(applied_skew_cc,
        widen_declines)``:

        - ``applied_skew_cc`` — 0 while the skew is dark (skew_params.enabled
          False) or unwired, the honest skew once enabled (fed to the pricer).
        - ``widen_declines`` — True only when the widen policy is ENABLED and
          fires (near a cap on concentrating flow). SHADOW-mode fires log-only.

        Both share ONE snapshot + candidate (the NO position a fill creates —
        exactly what the limit check builds). Never raises on the hot path: a
        hole (unknown marginals ⇒ empty per-game map) yields skew 0 / no decline.
        Returns (0, False) immediately when nothing is wired."""
        if self._skew_params is None or self._skew_limits is None:
            return 0, False
        candidate = OpenPosition(
            position_id=f"skew:{rfq.rfq_id}",
            combo_ticker=rfq.market_ticker,
            collection=rfq.mve_collection_ticker,
            # A sell-only fill leaves us long NO; the honest candidate is the NO
            # position at the quoted no_bid. maker_position_side maps the accepted
            # side ⇒ our side; a NO accept is the seller side we ever hold.
            our_side=self._conventions.maker_position_side(Side.NO),
            contracts=risk_qty,
            entry_price_cc=constructed.no_bid_cc,
            legs=self._leg_refs(rfq),
        )
        snap = self._exposure.snapshot(self._marginals, mass_acceptance=True)
        skew = compute_inventory_skew(
            candidate,
            snap,
            self._marginals,
            self._conventions,
            self._skew_limits,
            self._skew_params,
            cache=self._skew_cache,
        )
        log.info(
            "inventory_skew_shadow",
            rfq_id=rfq.rfq_id,
            skew_cc=skew.skew_cc,                        # honest classifier sign
            applied_cc=skew.applied_cc,                  # 0 while dark
            shadow_applied_cc=skew.shadow_applied_cc,    # pricer-frame, dark-independent
            concentration_cc=skew.concentration_cc,
            offset_cc=skew.offset_cc,
            enabled=skew.enabled,
            per_game=list(skew.per_game),
        )
        widen_declines = False
        if self._widen_params is not None:
            widen = decide_widen_or_decline(
                skew, snap, candidate, self._skew_limits, self._widen_params
            )
            if widen.would_decline:
                log.info(
                    "widen_vs_decline_shadow",
                    rfq_id=rfq.rfq_id,
                    would_decline=widen.would_decline,
                    applied=widen.applied,
                    max_util=round(widen.max_util, 4),
                    reason=widen.reason,
                )
            widen_declines = widen.applied
        return skew.applied_cc, widen_declines

    def _min_time_to_close_s(self, rfq: Rfq) -> float | None:
        times: list[float] = []
        now = self._clock.now()
        for leg in rfq.legs:
            meta = self._metadata.peek(leg.market_ticker)
            close = meta.close_time if meta else None
            if close is None:
                return None
            times.append((close - now).total_seconds())
        return min(times) if times else None

    def _marginals(self, market_ticker: str) -> float | None:
        try:
            book = self._feed.book(market_ticker)
        except KeyError:
            return None
        if not book.valid:
            return None
        return book.top().microprice()

    def _current_leg_mids(self, rfq: Rfq) -> dict[str, int]:
        mids: dict[str, int] = {}
        for ticker in rfq.leg_tickers:
            p = self._marginals(ticker)
            if p is not None:
                mids[ticker] = int(p * CC_PER_DOLLAR)
        return mids

    def _leg_refs(self, rfq: Rfq) -> tuple[LegRef, ...]:
        return tuple(
            LegRef(leg.market_ticker, leg.event_ticker, leg.side) for leg in rfq.legs
        )

    def _risk_qty(self, rfq: Rfq, constructed: ConstructedQuote) -> CentiContracts | None:
        """Full-RFQ size for the risk system. Target-cost RFQs convert at the
        CHEAPEST quoted side (most contracts) — the conservative ceiling.
        None = unresolvable = no-quote (never a placeholder)."""
        if rfq.contracts is not None:
            return rfq.contracts
        if rfq.target_cost_cc is not None:
            bids = [
                int(bid)
                for bid in (constructed.yes_bid_cc, constructed.no_bid_cc)
                if bid > 0
            ]
            if not bids:
                return None
            denom = max(100, min(bids))
            return CentiContracts(-(-int(rfq.target_cost_cc) * 100 // denom))
        return None

    def _quote_risk(
        self, rfq: Rfq, constructed: ConstructedQuote, *, quote_id: str, qty: CentiContracts
    ) -> OpenQuoteRisk:
        return OpenQuoteRisk(
            quote_id=quote_id,
            rfq_id=rfq.rfq_id,
            combo_ticker=rfq.market_ticker,
            collection=rfq.mve_collection_ticker,
            yes_bid_cc=constructed.yes_bid_cc,
            no_bid_cc=constructed.no_bid_cc,
            contracts=qty,
            legs=self._leg_refs(rfq),
        )

    def _accepted_qty(
        self, state: OpenQuoteState, accepted_side: Side, msg: JsonDict
    ) -> CentiContracts | None:
        """Accepted size; None = unknowable = deliberate lapse (defense #2).

        Kalshi's ``quote_accepted`` communications-WS message conveys size via
        (docs.kalshi.com/websockets/communications, verified against the live
        tape 2026-07-14):
          - ``contracts_accepted_fp`` — the accepted count, populated for a
            CONTRACTS-mode RFQ (taker specified a contract count).
          - ``no_contracts_offered_fp`` / ``yes_contracts_offered_fp`` — the
            contracts WE offered per side. On a TARGET-COST RFQ (taker specified
            DOLLARS, 95% of live flow) ``contracts_accepted_fp`` is null, so the
            accepted size is the contracts we offered on the ACCEPTED side — the
            taker accepted our firm quote for the size we offered, which our
            sizing computed to cover ``rfq_target_cost_dollars``.
        We read the accepted count first, then fall back to the accepted side's
        offered count, then to the RFQ's own contracts (contracts-mode wire
        default). Missing all three ⇒ None ⇒ lapse (defense #2).

        2026-07-14 fill-killer: the old code read ``contracts_accepted_fp`` ONLY,
        which is null on every target-cost accept, so 95% of WON auctions lapsed
        DECLINE_SIZE_UNKNOWN at confirm. (The demo ground-truth's contracts_fp /
        no_contracts_fp were a quote-TERMINAL record, not the accept message —
        they do not appear on the live quote_accepted wire.) A present-but-
        unparseable field still lapses (never guess); "0.00" ⇒ try next.
        """
        side_offered = (
            "no_contracts_offered_fp" if accepted_side is Side.NO
            else "yes_contracts_offered_fp"
        )
        for key in ("contracts_accepted_fp", side_offered):
            raw = msg.get(key)
            if raw is None:
                continue
            try:
                qty = qty_from_fp_str(str(raw))
            except ValueError:
                # Present-but-unparseable size = corrupt message: lapse, never
                # guess (defense #2). Do not fall through to another field.
                log.warning("accept_size_unparseable", field=key, raw=str(raw))
                return None
            if qty > 0:
                return qty
            # qty == 0 ⇒ "not this side"; try the next candidate.
        return state.rfq.contracts

    def _last_look_inputs(
        self,
        state: OpenQuoteState,
        accepted_side: Side,
        bid: CentiCents,
        qty: CentiContracts,
    ) -> LastLookInputs:
        result = self._price(state.rfq)
        current_fair = int(result.fair_cc) if isinstance(result, ConstructedQuote) else None

        max_move: int | None
        if not state.leg_mids_cc:
            max_move = None
        else:
            moves: list[int] = []
            max_move = 0
            for ticker, mid_at_quote in state.leg_mids_cc.items():
                p = self._marginals(ticker)
                if p is None:
                    max_move = None
                    break
                moves.append(abs(int(p * CC_PER_DOLLAR) - mid_at_quote))
            if max_move is not None:
                max_move = max(moves)

        books_valid = all(self._book_valid(t) for t in state.rfq.leg_tickers)
        max_leg_age = self._feed.rx_age_s if books_valid else None

        candidate = OpenPosition(
            position_id=f"lastlook:{state.quote_id}",
            combo_ticker=state.rfq.market_ticker,
            collection=state.rfq.mve_collection_ticker,
            our_side=self._conventions.maker_position_side(accepted_side),
            contracts=qty,
            entry_price_cc=bid,
            legs=self._leg_refs(state.rfq),
        )
        breaches = self._partition_breaches(
            self._limits.check(
                self._exposure,
                self._marginals,
                self.daily_pnl,
                candidate_positions=[candidate],
                risk_bankroll_cc=self._risk_bankroll_cc(),
                bankroll_source_configured=self._bankroll_source_configured(),
                start_time_provider=self._start_time_provider,
                halt_inputs=self._halt_inputs(),
                book_risk=self._book_risk_for_check(),
            )
        )
        # Straddle safety (Phase 3): re-run the schedule-based pregame gate —
        # a leg can go in-play between quote and accept. Peek-only, hot-path safe.
        # Phase 5 (R3 §B2): the CONFIRM side uses the stricter M_c margin, so a
        # leg near kickoff declines at last look even if the quote side (M_q) let
        # it through — the confirm buffer stays hard while quoting recovers flow.
        pregame = self._filter.pregame_confirm_status(state.rfq)
        return LastLookInputs(
            quote_time_fair_cc=int(state.constructed.fair_cc),
            current_fair_cc=current_fair,
            max_leg_move_cc=max_move,
            max_leg_age_s=max_leg_age,
            ws_healthy=self._feed.feed_healthy,
            seq_ok=books_valid,
            any_leg_in_play=self._inplay.any_anomalous(list(state.rfq.leg_tickers)),
            any_leg_started=pregame.any_started,
            leg_start_unknown=pregame.any_unknown,
            velocity_anomaly=self._inplay.any_anomalous([state.rfq.market_ticker]),
            exchange_active=self.exchange_active,
            killswitch_halted=self._killswitch.halted,
            risk_breaches=tuple(b.detail for b in breaches),
        )

    def _book_valid(self, ticker: str) -> bool:
        try:
            return self._feed.book(ticker).valid
        except KeyError:
            return False

    def _track_markout(self, fill_ref: str, state: OpenQuoteState) -> None:
        def provider() -> tuple[int | None, int | None]:
            result = self._price(state.rfq)
            fair = int(result.fair_cc) if isinstance(result, ConstructedQuote) else None
            mids = self._current_leg_mids(state.rfq)
            raw_mid: int | None = None
            if len(mids) == len(state.rfq.legs):
                product = 1.0
                for leg in state.rfq.legs:
                    p = mids[leg.market_ticker] / CC_PER_DOLLAR
                    product *= p if leg.side == "yes" else 1.0 - p
                raw_mid = int(product * CC_PER_DOLLAR)
            return fair, raw_mid

        raw_mid_now: int | None
        fair_now, raw_mid_now = provider()
        self._markouts.track(
            MarkoutSubject(
                fill_ref=fill_ref,
                fair_at_event_cc=fair_now,
                raw_mid_at_event_cc=raw_mid_now,
            ),
            provider,
        )

    async def _delete_quote(self, quote_id: str, reason: ReasonCode) -> None:
        try:
            await self._sender.delete_quote(quote_id)
        except Exception as exc:
            log.warning("delete_quote_failed", quote_id=quote_id, error=repr(exc))
        state = self._open.get(quote_id)
        self._drop_quote(quote_id)
        self._metrics.inc(f"quote.deleted.{reason}")
        if state is not None:
            await self._store.record_decision(
                "quote_deleted", state.rfq.rfq_id, [str(reason)], {"quote_id": quote_id}
            )

    def _drop_quote(self, quote_id: str) -> None:
        state = self._open.pop(quote_id, None)
        self._exposure.remove_quote(quote_id)
        if state is not None and self._by_rfq.get(state.rfq.rfq_id) == quote_id:
            del self._by_rfq[state.rfq.rfq_id]

    def _pregame_flow_context(self, rfq: Rfq, reasons: list[ReasonCode]) -> JsonDict:
        """Attach ``time_to_start_s`` to a pregame decline for the flow-loss
        measurement (R3 §B3): the distribution of near-kickoff declines bucketed
        by minutes-to-start is the flow we forgo. Pure counting on the decision
        log, zero P&L. Empty for non-pregame declines (no cost to attach)."""
        pregame_reasons = {
            ReasonCode.SKIP_INPLAY_LEG,
            ReasonCode.SKIP_START_TIME_UNKNOWN,
        }
        if not (set(reasons) & pregame_reasons):
            return {}
        ttl = self._filter.min_time_to_start_s(rfq)
        # None ⇒ start UNKNOWN (itself the decline reason); record as such.
        return {"time_to_start_s": None if ttl is None else round(ttl, 1)}

    async def _record_skip(
        self, rfq: Rfq, reasons: list[ReasonCode], context: JsonDict
    ) -> None:
        self._metrics.inc("rfq.skipped")
        await self._store.record_decision(
            "no_quote", rfq.rfq_id, [str(r) for r in reasons], context
        )

    async def _record_confirm_decision(
        self,
        state: OpenQuoteState,
        *,
        confirm: bool,
        reason: ReasonCode,
        detail: str,
        decision_ms: float,
    ) -> None:
        context: JsonDict = {
            "quote_id": state.quote_id,
            "detail": detail,
            "decision_ms": round(decision_ms, 3),
            "quote_time_fair_cc": int(state.constructed.fair_cc),
        }
        # Flow-loss measurement (R3 §B3): log time_to_start on pregame declines
        # at CONFIRM too (the M_c straddle re-check), matching the quote-time log.
        if reason in (
            ReasonCode.DECLINE_INPLAY_LEG,
            ReasonCode.DECLINE_START_TIME_UNKNOWN,
        ):
            ttl = self._filter.min_time_to_start_s(state.rfq)
            context["time_to_start_s"] = None if ttl is None else round(ttl, 1)
        await self._store.record_decision(
            "confirm" if confirm else "decline",
            state.rfq.rfq_id,
            [str(reason)],
            context,
        )
        # P2-4: one consolidated risk-audit line per confirm/decline. The candidate
        # EV comes from the pending fill (side/bid/qty) when we got far enough to set
        # it — an early lapse (e.g. side-not-quoted) has no sized candidate, so EV is
        # None. The binding cap is the decline reason on a decline ("" on a confirm).
        candidate_ev_cc: int | None = None
        if state.pending_fill is not None:
            accepted_side, bid, qty = state.pending_fill
            candidate_ev_cc = self._candidate_edge_cc(
                int(state.constructed.fair_cc),
                int(bid),
                qty,
                self._conventions.maker_position_side(accepted_side),
            )
        log.info(
            "risk_audit",
            phase="confirm" if confirm else "decline",
            rfq_id=state.rfq.rfq_id,
            quote_id=state.quote_id,
            reason=str(reason),
            **self._risk_audit_fields(
                candidate_ev_cc=candidate_ev_cc,
                binding_cap="" if confirm else str(reason),
                fallback_reason="",
            ),
        )
