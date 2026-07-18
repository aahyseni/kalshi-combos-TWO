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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
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
    StateWorstCaseInputs,
    _worker_candidate_book_risk,
    _worker_state_worst_case,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeType, FeeUnknownError
from combomaker.pricing.grouping import game_key
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
    monotone_pre_quote_breaches,
    threshold_cc,
)
from combomaker.risk.markouts import MarkoutSubject, MarkoutTracker
from combomaker.risk.reservation import ReserveResult, RiskReservationService
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
    CandidateBookRisk,
    compute_book_risk,
    modeled_cost_basis_cc,
)
from combomaker.sim.state_worst_case import (
    GameWorstCase,
    entity_from_position,
    quote_from_open_quote,
    tail_outside_selection,
    trim_open_quotes_for_games,
)
from combomaker.sim.structural_book import StructuralConfigView

log = get_logger(__name__)


def _fmt_opt_cc(value_cc: float | None) -> str:
    """Format an optional centi-cent figure for decline-detail strings.

    ``None`` (mutex-aware bound unavailable — pre-fix snapshot or fail-closed
    slice) renders as ``"n/a"`` so the operator's decline reports can tell
    "not computed" apart from a real 0."""
    return "n/a" if value_cc is None else f"{value_cc:.0f}"

JsonDict = dict[str, Any]

# CONFIRM-PATH LAST-LOOK MC WAIVER (handoff Problem A): the ONLY reservation-
# denial breach reasons the waiver may lift — the two deliberately comonotone-
# OVERSTATED analytic per-game bounds whose true state-consistent loss the exact
# scoreline enumeration can certify. ANY other enforced breach in the denial ⇒
# decline exactly as today (the waiver never touches gross / per-combo / daily /
# CVaR / ruin / notional / slate caps or any halt).
WAIVABLE_RESERVATION_BREACHES: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.SKIP_GAME_LOSS_CAP,
        ReasonCode.SKIP_DIRECTIONAL_CAP,
        # 2026-07-17: the hard-dollar per-game worst-case cap emits this code
        # WITH its game key (limits.py) — it binds on the SAME game-loss
        # aggregate the waiver certifies, and the certificate is re-validated
        # against the cap's own budget at the enforcement site. The DELTA
        # family emits the same code with game=None, so those denials still
        # fail closed at the "waivable breach missing its game key" check —
        # a delta breach can never be waived.
        ReasonCode.SKIP_MASS_ACCEPTANCE_BREACH,
    }
)

# EVENT-DRIVEN POST-FILL RISK PULL (resting-quote haircut, 2026-07-17): the
# breach reasons the post-fill pull may evict resting quotes on — exactly the
# two per-game caps that carry their game key on the ``Breach`` (the key is
# never parsed out of detail strings) and whose quote-time bound the haircut
# relaxed. Global caps (gross/utilization/slate/delta) are NOT evicted here:
# they carry no game attribution, and the confirm-path exact enforcement +
# TTL/reprice sweeps remain their backstop.
EVICTABLE_ON_FILL_BREACHES: frozenset[ReasonCode] = frozenset(
    {ReasonCode.SKIP_GAME_LOSS_CAP, ReasonCode.SKIP_DIRECTIONAL_CAP}
)


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


class QuoteGetter(Protocol):
    """GET slice for the FILL-RECORD RECOVERY SWEEP (2026-07-16 P1): the REST
    ``GET /communications/quotes/{quote_id}`` read the sweep polls when a
    confirmed fill's ``quote_executed`` WS message never arrived. Kept a
    SEPARATE, optional protocol (not folded into ``QuoteSender``) so paper mode
    and every existing fake sender stay untouched — no getter wired ⇒ no sweep
    (fail-closed: the ledger is never patched from a guess)."""

    async def get_quote(self, quote_id: str) -> JsonDict: ...


# FILL-RECORD RECOVERY SWEEP bounds (2026-07-16 P1). Rate-bound the REST polls
# per maintenance tick (the tick beats every 0.5s; recovery is not latency-
# critical) and bound the per-quote attempts: after the budget is exhausted the
# sweep gives up LOUDLY (fill_recovery.exhausted + an error log) and leaves the
# state for the next-restart exchange reconcile (the P0-4/P0-5 backstop that
# found the 2026-07-16 proven case) rather than polling forever.
_FILL_RECOVERY_MAX_POLLS_PER_TICK = 3
_FILL_RECOVERY_MAX_ATTEMPTS = 10

# REPRICE-SWEEP WEDGE DEFENSES (2026-07-16 — the 18:13Z heartbeat kill; see
# maintenance_tick). Consecutive pool-deadline results that trip the frozen-pool
# circuit breaker, and the sweep's total wall budget per tick.
_REPRICE_POOL_TRIP = 2
_REPRICE_SWEEP_BUDGET_S = 2.5

# F1 PRE-PRICING GATE cache age bound (seconds). The generation/bankroll keys
# do the real invalidation; this bound only covers slow drift (metadata ME
# answers) and matches the 0.5s maintenance-tick granularity the book is
# re-checked at anyway.
_PRE_GATE_CACHE_TTL_S = 0.5


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
    # P0-2 (candidate MC atomic with reservations). The candidate gate reserves a
    # PROVISIONAL reservation under the analytic hard caps BEFORE it runs the MC, so a
    # concurrent accept's own MC sees this candidate's held headroom (no two accepts
    # can each pass against the same old book). It then captures the position
    # generation AND the reservation version, runs the MC, and on return verifies
    # BOTH are unchanged; if a fill/settlement/reconciliation or another accept's
    # reservation moved the book under it, it REBUILDS + RETRIES — bounded by the
    # remaining confirm deadline below. ``candidate_gate_deadline_s`` is the total
    # wall budget the atomic gate (all retries) may consume of the exchange confirm
    # window; when the remaining budget is below one more MC's worth of time the gate
    # FAILS CLOSED (releases the provisional reservation + declines) rather than
    # silently consuming the whole window (audit "do not let risk computation silently
    # consume the confirm window"). ``candidate_gate_max_retries`` bounds the rebuild
    # loop independently of the clock (belt-and-suspenders for a stuck-clock test env
    # / a pathological churn storm). Both are conservative: exceeding either DECLINES.
    candidate_gate_deadline_s: float = 2.0
    candidate_gate_max_retries: int = 3
    # P1 EV VISIBILITY (audit "+EV IS PRODUCTION-MODEL EV, NOT ROBUST EV"). The
    # candidate gate LOGS the production candidate EV AND the challenger / bridge /
    # split candidate EV (and the worst-credible EV) so a candidate that is +EV under
    # production yet −EV under a challenger is visible. The ADMISSION policy stays
    # production-model-EV based; this OPTIONAL tolerance ONLY ADDS a decline: a
    # +production-EV candidate whose WORST credible challenger EV falls below the
    # tolerance is declined too. DEFAULTS to −inf ⇒ no behaviour change (worst >= −inf
    # is always true); the operator sets a finite negative tolerance in cc (e.g.
    # -50.0 ⇒ allow the worst challenger EV down to −0.50 of edge) to opt in. Strictly
    # additive — it can only flip an already-admitted confirm to a decline.
    worst_challenger_ev_tolerance_cc: float = float("-inf")
    # LAST-LOOK MC WAIVER (handoff Problem A — CONFIRM-PATH ONLY; see
    # RiskConfig.lastlook_mc_waiver_enabled for the full rationale). When True, a
    # confirm-time reservation denial whose enforced breaches are ALL game-loss /
    # mutex-directional cap breaches (WAIVABLE_RESERVATION_BREACHES) runs the
    # exact state-consistent per-game worst-case enumeration OFF-LOOP and, ONLY
    # if every breached game is CERTIFIED within the SAME game-loss budget,
    # retries the reservation ONCE with the per-game certificates. Default OFF
    # (byte-identical prior behaviour: the denial declines DECLINE_RISK_LIMIT).
    lastlook_mc_waiver_enabled: bool = False
    # Wall budget (seconds) for the WHOLE waiver evaluation (build + off-loop
    # enumeration + at most one rebuild). Must fit inside the exchange's 3s
    # confirm window ALONGSIDE the candidate gate's own budget; exceeding it
    # DECLINES (fail-closed — never let the waiver silently consume the window).
    lastlook_mc_waiver_deadline_s: float = 1.0
    # FILL-RECORD RECOVERY SWEEP (2026-07-16 P1, real-money bug). How long after
    # a SUCCESSFUL confirm (reservation committed / position booked) the sweep
    # waits for the exchange's quote_executed WS message before polling REST
    # GET quote — the WS channel has NO replay, so a missed message left a REAL
    # fill (quote 527b5a3a…, 117.07ct NO @ 80.60c) permanently out of the fills
    # ledger / P&L / EV / markouts until the next-restart reconcile quarantined
    # it. 10s is far beyond the combo execution timer (1s) + observed WS
    # latency, so a poll only fires when the message is genuinely lost. Wired
    # from RiskConfig.fill_record_recovery_after_s. A non-positive/NaN value
    # disables the sweep (fail-closed: never poll on a nonsense config).
    fill_record_recovery_after_s: float = 10.0
    # F1 MONOTONE PRE-PRICING GATE (throughput synthesis 2026-07-16, lens-3 F1).
    # When True, handle_rfq consults a CANDIDATE-FREE limits check (cached per
    # exposure generation + bankroll, ≤0.5s) BEFORE the expensive joint pricing
    # and pre-declines on the candidate-monotone breach subset
    # (risk/limits.PRE_PRICING_MONOTONE_REASONS) — the SAME decline the full
    # post-pricing check would produce, just before the pricing work is spent
    # (measured: 81% of the game-day window's no-quotes were fully priced then
    # risk-declined; 48.2% carried an allowlisted reason). Identical reason
    # codes, earlier exit; the stage rides the decision context. Prototype-first
    # validated (tools/proto_pre_pricing_gate.py: fuzz + counterexamples + tape
    # replay + port parity). Default False = today's behaviour, byte-identical;
    # the operator arms it in the local YAML (risk.pre_pricing_gate_enabled).
    pre_pricing_gate_enabled: bool = False
    # CONFIRM-TIME resting haircut (operator 2026-07-17, the no-double-counting
    # doctrine extended one layer down): the reservation check weights the
    # RESTING open-quote fold at resting_quote_weight — committed positions,
    # outstanding reservations, and the candidate all stay at 100% (the serial
    # commit chain is untouched). Default False = today's 100% fold; the
    # operator arms it in the local YAML (risk.resting_haircut_at_confirm).
    resting_haircut_at_confirm: bool = False
    # WAIVER ENTITY-SET TRIM (2026-07-18 — burst-floor doctrine inside the
    # enumeration). When > 0, the last-look MC waiver enumerates committed
    # entities + reservations + the candidate (never trimmed) + only the K
    # LARGEST resting quotes per BREACHED game (by comonotone worst-side loss);
    # every dropped resting quote touching a breached game folds into a CONSTANT
    # conservative adder on that game's certificate (state-independent — it can
    # only RAISE the certified worst case, never lower it: fail-closed; see
    # sim/state_worst_case.trim_open_quotes_for_games). 0 (default) = today's
    # full-set enumeration, byte-identical; the operator arms the profiled K in
    # the local YAML (risk.lastlook_waiver_topk_resting).
    lastlook_waiver_topk_resting: int = 0
    # CERTIFIED-HEDGE EV BUDGET (2026-07-18). Wired verbatim into the P0-1
    # candidate gate: a NEGATIVE-EV fill can be admitted ONLY when this is True
    # AND the candidate is CERTIFIED risk-reducing (POST governing model
    # UNCLAMPED expected tail loss <= PRE, on common random numbers —
    # sim/book_risk._candidate_gate; unclamped so the certification is never
    # vacuous on a profit-clamped tail) AND its
    # EV cost fits hedge_cost_budget_cc. Both default to the P0-1 SAFETY
    # DEFAULT (disabled / 0 = today, byte-identical): arming means "pay up to
    # $X of EV only for fills that measurably shrink the book's tail" — never a
    # sniper-tax subsidy on stale quotes.
    allow_negative_ev_hedge: bool = False
    hedge_cost_budget_cc: int = 0


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
    mutex_aware_det_max_cc: float | None = None
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
    # FILL-RECORD RECOVERY (2026-07-16 P1). Set when the confirm round-trip
    # SUCCEEDED (reservation committed / position booked) — the point after
    # which a quote_executed message is EXPECTED; None means the confirm never
    # succeeded client-side (the unknown-committed path belongs to the
    # reservation-reconcile loop, never this sweep).
    fill_confirmed_mono_ns: int | None = None
    # True once this quote's fills-ledger row exists (recorded, or confirmed
    # present on a replay) — the sweep's terminal success state.
    fill_recorded: bool = False
    # REST polls spent recovering this quote (bounded; exhausted ⇒ loud metric).
    fill_recovery_attempts: int = 0


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
        maker_fee_active_prefixes: tuple[str, ...] = (),
        joint_pool: JointPool | None = None,
        book_risk_pool: BookRiskPool | None = None,
        quote_getter: QuoteGetter | None = None,
        beat: Callable[[], None] | None = None,
        rfq_alive: Callable[[str], bool] | None = None,
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
        # MAKER-FEE LIST (2026-07-16, eat-the-fee doctrine — FeeConfig.
        # maker_fee_active_prefixes): series/collection prefixes on which OUR
        # maker fills pay the maker fee. Quoted prices are UNTOUCHED (the fee is
        # never added to width); a matching fill's fee is ACCOUNTED via the real
        # FeeModel (QUADRATIC → QUADRATIC_WITH_MAKER_FEES upgrade in
        # _effective_fee_type) in the fills ledger, realized P&L, expected edge,
        # and the waiver candidate. Empty (default) ⇒ bit-identical behaviour.
        self._maker_fee_active_prefixes = maker_fee_active_prefixes
        # FILL-RECORD RECOVERY SWEEP (2026-07-16 P1): the GET-capable REST slice
        # the sweep polls. None (paper/backtests/minimal rigs) ⇒ no sweep — the
        # ledger is never patched from a guess (fail-closed).
        self._quote_getter = quote_getter
        # HEARTBEAT BEAT (2026-07-16 wedge fix): quote_app's Heartbeat.beat,
        # invoked per iteration inside the long maintenance sub-loops (reprice
        # sweep, recovery polls) so a loop that is genuinely MAKING PROGRESS
        # never reads as a wedge to the external supervisor — while a true
        # event-loop wedge still cannot beat (the fail-closed signal survives).
        # None (tests/backtests) ⇒ no-op.
        self._beat_cb = beat
        # F2 MID-PIPELINE LIVENESS (throughput synthesis 2026-07-16): "is this
        # RFQ still open on the exchange stream?" — wired to the intake's
        # liveness view (``intake.rfq_alive``: open registry + disconnect-
        # cleared ids held as UNKNOWN⇒alive) by quote_app. The hot path
        # re-checks it at three points (dequeue / after the pool joint
        # / immediately before the create-quote POST) so an RFQ POSITIVELY
        # deleted mid-flight stops consuming pricing, snapshots, and REST
        # write budget (fixed run: 97.4% of POSTs went to already-dead RFQs).
        # None (backtests / tests) ⇒ no liveness view ⇒ behaviour identical to
        # today. Wired in BOTH paper and quote modes (additive skips only).
        self._rfq_alive = rfq_alive
        # F1 PRE-PRICING GATE cache: (exposure generation, bankroll_cc, built
        # mono_ns, breaches). Every allowlisted cap input is either static per
        # book mutation (loss/notional folds, quote count — invalidated by the
        # GENERATION key) or the bankroll itself (its own key); the ≤0.5s age
        # bound is belt-and-suspenders for metadata drift (ME answers), matching
        # the maintenance-tick granularity. A falsely-CACHED verdict can only
        # DECLINE (never admit), and retry_pending re-checks within 1s anyway.
        self._pre_gate_cache: tuple[int, int | None, int, list[Breach]] | None = None
        # EVENT-DRIVEN POST-FILL RISK PULL (resting-quote haircut, 2026-07-17):
        # single-flight task + the games of recently committed fills (eviction
        # priority: same-game resting quotes first). Armed only while
        # resting_quote_weight < 1 (the haircut is what opens the gap the pull
        # closes); an idle default build never runs it.
        self._risk_evict_task: asyncio.Task[None] | None = None
        self._risk_evict_pending_games: set[str] = set()
        self._markouts = MarkoutTracker(store.record_markout)
        # LAST-LOOK MC WAIVER observability: the per-confirm waiver audit record
        # ({granted, worst_case_cc, games}), set ONLY when a waiver ATTEMPT ran
        # for the confirm in flight, emitted on that confirm's ``risk_audit``
        # line and reset. None ⇒ no waiver attempted (the default fields).
        self._waiver_audit: dict[str, Any] | None = None
        self._open: dict[str, OpenQuoteState] = {}       # quote_id → state
        # Reprice-sweep rotation marker (review 2026-07-16): when a sweep breaks
        # early (wall budget / pool circuit trip) the next tick RESUMES after
        # the last handled quote instead of restarting from the front of the
        # insertion-ordered dict — without it, front quotes whose fair never
        # moved re-consumed the budget every tick and back-of-book quotes could
        # starve un-repriced for their whole TTL under sustained load.
        self._reprice_resume_after: str | None = None
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
                mutex_aware_det_max_cc=(
                    None
                    if snap.mutex_aware_det_max_cc is None
                    else int(snap.mutex_aware_det_max_cc)
                ),
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

    async def _run_candidate_mc(
        self, inputs: CandidateBookRiskInputs
    ) -> CandidateBookRisk:
        """Run ONE candidate-MC eval, off the loop via ``BookRiskPool.run_candidate``
        when a pool is wired (the CPU-bound MC never blocks the heartbeat), else
        INLINE via the pool's OWN worker fn (paper / backtests / tests — fast there,
        and byte-identical to the off-loop path). Raises on any pool/worker error;
        the caller turns that into a fail-closed decline."""
        if self._book_risk_pool is not None:
            return await self._book_risk_pool.run_candidate(inputs)
        return _worker_candidate_book_risk(inputs)

    async def _candidate_gate_verdict(
        self,
        quote_id: str,
        state: OpenQuoteState,
        *,
        reservation_id: str | None,
    ) -> tuple[bool, str]:
        """P0-1/P0-2 candidate-aware portfolio-risk gate for ONE contemplated fill,
        ATOMIC with the reservation book.

        Returns ``(True, "")`` to PROCEED to the confirm round-trip (the provisional
        reservation, if any, stays held for the caller to commit), or
        ``(False, detail)`` to DECLINE with ``DECLINE_CANDIDATE_RISK``. STRICTLY
        ADDITIVE — reachable only after the existing gates ADMIT the fill, and it can
        only DECLINE, never admit.

        P0-2 (candidate MC atomic with reservations). Before this gate runs the caller
        has ALREADY created a PROVISIONAL reservation for this candidate under the
        analytic hard caps (``reservation_id``), so a concurrent accept's own MC sees
        this candidate's held headroom — two accepts can no longer each pass against
        the same old book. Each MC attempt:

          1. Builds the inputs, STAMPING the ExposureBook position generation AND the
             RiskReservationService version captured at that on-loop read (the
             candidate's own provisional reservation is excluded from the PRE
             reservations so it is not double-counted — it rides as ``candidate``).
          2. Runs the MC (off the loop; the heartbeat keeps beating while it awaits).
          3. On return, re-reads the LIVE generation + version. If EITHER moved — a
             fill/settlement/reconciliation, or another accept's reserve/release ran
             during the await — the verdict priced a book that no longer exists, so it
             is DISCARDED and the inputs are REBUILT + retried.

        The retry loop is BOUNDED by BOTH the remaining confirm deadline
        (``candidate_gate_deadline_s`` wall budget) and ``candidate_gate_max_retries``.
        If a rebuild is needed but too little deadline remains for one more MC, or the
        retry budget is exhausted, the gate FAILS CLOSED (declines) rather than
        silently consuming the whole confirm window (audit LIVE CANDIDATE-GATE
        LATENCY). FAIL-CLOSED throughout: an UNKNOWN merged marginal, an over-budget
        POST book, ANY exception in the eval, or an unstable book that never settles
        within the deadline all DECLINE — an unmeasured, errored, or stale joint tail
        is never safe. The CALLER releases the provisional reservation on any decline.

        With no reservation service (paper / backtests / tests) ``reservation_id`` is
        None: there is no provisional reservation and the single-loop confirm cannot
        race, so the version check is inert (the stamps default to -1 and the live
        reservation version is -1 too) and the gate runs exactly one MC attempt — the
        prior behaviour, preserved."""
        start_ns = self._clock.monotonic_ns()
        deadline_ns = int(self._config.candidate_gate_deadline_s * 1e9)
        last_mc_ns = 0  # duration of the most recent MC attempt (for deadline budget)
        for attempt in range(self._config.candidate_gate_max_retries + 1):
            # Build inputs (stamps the generation + reservation version at this read).
            try:
                inputs = self._build_candidate_gate_inputs(
                    quote_id, state, exclude_reservation_id=reservation_id
                )
            except Exception as exc:  # noqa: BLE001 — any build error declines
                log.error(
                    "candidate_gate_errored", quote_id=quote_id, error=repr(exc)
                )
                return False, f"candidate gate errored: {exc!r}"
            # Deadline guard BEFORE the MC: if less time remains than the previous
            # attempt took, do not start an MC that would overrun the confirm window.
            # (First attempt: last_mc_ns is 0, so this never blocks the first run.)
            elapsed_ns = self._clock.monotonic_ns() - start_ns
            if elapsed_ns + last_mc_ns > deadline_ns:
                # LIVE CANDIDATE-GATE LATENCY: the confirm window expired (too little
                # deadline remains for another MC) BEFORE a stable verdict — an accept
                # LOST because the exchange window ran out. Count both the deadline
                # trip and the window-expired-before-confirm axis the audit enumerates.
                self._metrics.inc("candidate_gate.deadline_exceeded")
                self._metrics.inc("candidate_gate.window_expired_before_confirm")
                self._metrics.observe_ms(
                    "candidate_gate.runtime_ms", elapsed_ns / 1e6
                )
                self._metrics.observe_ms("candidate_gate.remaining_window_ms", 0.0)
                log.warning(
                    "candidate_gate_deadline",
                    quote_id=quote_id,
                    attempt=attempt,
                    elapsed_ms=round(elapsed_ns / 1e6, 1),
                    detail="insufficient confirm deadline remains for another MC",
                )
                return False, (
                    "candidate gate deadline exhausted before a stable verdict"
                )
            mc0_ns = self._clock.monotonic_ns()
            try:
                result = await self._run_candidate_mc(inputs)
            except Exception as exc:  # noqa: BLE001 — any error declines (fail-closed)
                log.error(
                    "candidate_gate_errored", quote_id=quote_id, error=repr(exc)
                )
                return False, f"candidate gate errored: {exc!r}"
            last_mc_ns = self._clock.monotonic_ns() - mc0_ns
            # LIVE CANDIDATE-GATE LATENCY: one observation per MC attempt feeds the
            # candidate-gate p50/p90/p99 runtime histogram; the MC worker queue dwell
            # (submit→worker-start, decomposed by the pool from total await − in-worker
            # compute) is recorded when a pool ran it (inline runs have no queue).
            self._metrics.observe_ms("candidate_gate.mc_ms", last_mc_ns / 1e6)
            if self._book_risk_pool is not None:
                # ``getattr`` so a pool double without the dwell field (test stubs)
                # simply records no queue-dwell sample rather than raising.
                dwell_ms = getattr(
                    self._book_risk_pool, "last_candidate_dwell_ms", None
                )
                if dwell_ms is not None:
                    self._metrics.observe_ms(
                        "candidate_gate.queue_dwell_ms", dwell_ms
                    )
            # P0-2: verify the book did not move under the (possibly off-loop) MC. The
            # reservation version moves on a concurrent accept's reserve/release even
            # when the position generation does not, so BOTH must be unchanged.
            live_gen = self._exposure.position_generation
            live_ver = (
                self._reservation.version if self._reservation is not None else -1
            )
            if (
                live_gen != inputs.input_generation
                or live_ver != inputs.reservation_version
            ):
                # A fill/settlement/reconciliation or a concurrent reservation moved
                # the book: the verdict priced a stale portfolio. Discard + retry.
                self._metrics.inc("candidate_gate.version_conflict_retry")
                log.info(
                    "candidate_gate_version_conflict",
                    quote_id=quote_id,
                    attempt=attempt,
                    snapshot_generation=inputs.input_generation,
                    live_generation=live_gen,
                    snapshot_reservation_version=inputs.reservation_version,
                    live_reservation_version=live_ver,
                )
                continue
            # Stable verdict: the book the MC priced is still the live book. Record
            # the LIVE CANDIDATE-GATE LATENCY completion metrics (total gate runtime +
            # remaining confirm-window time at completion) whatever the verdict.
            self._record_gate_completion_latency(start_ns)
            # P1 EV VISIBILITY: log the production candidate EV DISTINCTLY from the
            # challenger / bridge / split candidate EVs (and the worst-credible EV), so
            # a candidate that is +EV under production yet −EV under a challenger is
            # visible in the logs even when it is ADMITTED (the admission policy stays
            # production-model-EV based).
            self._log_candidate_gate_ev(quote_id, attempt, result)
            if result.unknown:
                return False, f"candidate gate UNKNOWN: {result.decline_reason}"
            if not result.confirm:
                return False, (
                    f"candidate gate declined: {result.decline_reason} "
                    f"(cand_ev_cc={result.candidate_ev_cc:.1f}, "
                    f"worst_challenger_ev_cc="
                    f"{result.worst_credible_candidate_ev_cc:.1f}, "
                    f"post_es_cc={result.post.governing_model_es_99_cc:.0f}, "
                    f"post_det_cc={result.post.deterministic_max_loss_cc:.0f}, "
                    f"post_mutex_det_cc="
                    f"{_fmt_opt_cc(result.post.mutex_aware_det_max_cc)}, "
                    f"post_p_ruin={result.post.p_ruin:.4f})"
                )
            log.info(
                "candidate_gate_confirm",
                quote_id=quote_id,
                attempt=attempt,
                candidate_ev_cc=round(result.candidate_ev_cc, 1),
                post_governing_es_cc=int(result.post.governing_model_es_99_cc),
                post_deterministic_max_cc=int(result.post.deterministic_max_loss_cc),
                post_mutex_det_max_cc=(
                    None
                    if result.post.mutex_aware_det_max_cc is None
                    else int(result.post.mutex_aware_det_max_cc)
                ),
                post_p_ruin=round(result.post.p_ruin, 4),
                n_pre=result.n_pre_positions,
            )
            return True, ""
        # Retry budget exhausted without a stable verdict: the book kept moving under
        # every attempt. FAIL CLOSED (a never-settling book is never safe to confirm).
        self._metrics.inc("candidate_gate.retries_exhausted")
        self._record_gate_completion_latency(start_ns)
        log.warning(
            "candidate_gate_unstable",
            quote_id=quote_id,
            retries=self._config.candidate_gate_max_retries,
            detail="book moved under every candidate-MC attempt — declining",
        )
        return False, "candidate gate unstable: reservation/book moved every retry"

    def _record_gate_completion_latency(self, start_ns: int) -> None:
        """LIVE CANDIDATE-GATE LATENCY: at a terminal gate outcome record the total
        gate runtime (all attempts) and the remaining confirm-window time — the
        deadline budget left when the verdict landed. A negative remainder (the gate
        overran the wall budget) is clamped to 0 (no time left)."""
        elapsed_ns = self._clock.monotonic_ns() - start_ns
        deadline_ns = int(self._config.candidate_gate_deadline_s * 1e9)
        self._metrics.observe_ms("candidate_gate.runtime_ms", elapsed_ns / 1e6)
        self._metrics.observe_ms(
            "candidate_gate.remaining_window_ms",
            max(0.0, (deadline_ns - elapsed_ns) / 1e6),
        )

    def _log_candidate_gate_ev(
        self, quote_id: str, attempt: int, result: CandidateBookRisk
    ) -> None:
        """P1 EV VISIBILITY (audit "+EV IS PRODUCTION-MODEL EV, NOT ROBUST EV").

        Log the PRODUCTION candidate EV — the number the admission policy gates on —
        DISTINCTLY from the correlation-inflated challenger, full-copula bridge, and
        unconditioned-split candidate EVs, plus the worst-credible EV (the min over
        production + every challenger that ran). This makes a candidate that is +EV
        under the production model yet −EV under a challenger visible in the logs even
        when it is ADMITTED. Bridge / split EVs are None when that path did not run
        (never coerced to a convenient 0). Money stays float cc (simulator domain)."""
        log.info(
            "candidate_gate_ev",
            quote_id=quote_id,
            attempt=attempt,
            production_candidate_ev_cc=round(result.candidate_ev_cc, 2),
            challenger_candidate_ev_cc=round(result.challenger_candidate_ev_cc, 2),
            bridge_candidate_ev_cc=(
                round(result.bridge_candidate_ev_cc, 2)
                if result.bridge_candidate_ev_cc is not None
                else None
            ),
            split_candidate_ev_cc=(
                round(result.split_candidate_ev_cc, 2)
                if result.split_candidate_ev_cc is not None
                else None
            ),
            worst_credible_candidate_ev_cc=round(
                result.worst_credible_candidate_ev_cc, 2
            ),
            worst_challenger_ev_tolerance_cc=(
                self._config.worst_challenger_ev_tolerance_cc
            ),
        )

    def _build_candidate_gate_inputs(
        self,
        quote_id: str,
        state: OpenQuoteState,
        *,
        exclude_reservation_id: str | None = None,
    ) -> CandidateBookRiskInputs:
        """Build the IMMUTABLE, picklable inputs for one off-loop candidate MC.

        On-loop work only: build the candidate position (shared builder), read the
        committed positions + outstanding reservations, resolve every candidate-
        universe leg marginal and within-game pair rho into plain dicts (the live
        feed / SgpParams providers do not pickle), and snapshot the RiskLimits
        budgets. A leg whose marginal is missing is OMITTED from the dict, so the
        worker's provider returns None for it ⇒ the merged model is UNKNOWN ⇒ the
        gate declines (fail-closed — a missing marginal is never a usable p=0.5).

        P0-2: ``exclude_reservation_id`` is the candidate's OWN provisional
        reservation id (created before the MC so a concurrent accept sees this
        candidate's held headroom). That reservation's position IS the candidate, so
        it is dropped from the ``reservations`` tuple here — the candidate rides in the
        MC as the dedicated ``candidate`` argument, and folding its provisional
        reservation into ``reservations`` too would DOUBLE-COUNT it (once as PRE, once
        as the candidate). Every OTHER outstanding reservation (concurrent accepts +
        held fills) still rides in ``reservations``. The returned inputs are stamped
        with the ExposureBook position generation AND the reservation version captured
        at this read, so the caller can detect a book move under an off-loop MC."""
        candidate = self._fill_position(quote_id, state)
        committed = tuple(self._exposure.positions.values())
        # P0-2: capture BOTH staleness signals at the read instant. The position
        # generation moves on a fill/settlement/reconciliation/commit; the reservation
        # version moves on EVERY reserve/commit/release/mark_unconfirmed — including a
        # concurrent accept's provisional reserve, which does NOT bump the position
        # generation. Both are needed to detect every kind of book move.
        input_generation = self._exposure.position_generation
        if self._reservation is not None:
            reservation_version = self._reservation.version
            reservations = tuple(
                pos
                for pos in self._reservation.outstanding_positions()
                if exclude_reservation_id is None
                or pos.position_id != exclude_reservation_id
            )
        else:
            reservation_version = -1
            reservations = ()
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
        # P0-1 (candidate P(ruin) equity basis must NOT be overstated). The ruin
        # check adds a sampled POST book_pnl — Σ(payout − price) over committed +
        # reservation + candidate combos — onto this scalar equity basis. The ONLY
        # basis that reconciles that to true post-fill terminal equity is the
        # COMMITTED-ONLY cost basis: available_cash + Σ price·c over committed
        # modeled positions. Reservation and candidate premiums are NOT yet debited
        # from `available_cash`, so adding them here (as the earlier MERGED-model
        # basis did) double-credits the unpaid premium — POST equity became
        #   cash + cand_price + (cand_payout − cand_price) = cash + cand_payout,
        # overstated by exactly the premium and understating P(ruin). Feeding the
        # committed-only basis lets each reservation/candidate combo's sampled
        # (payout − price) carry its own cost, yielding the correct
        #   cash + terminal_value(committed) + Σ_resv(payout − price)
        #     + cand_payout − cand_price.
        committed_only_model = build_book_model(
            list(committed),
            marginals=self._marginals,
            within_game_rho=self._within_game_rho,
        )
        current_equity_cc = self._ruin_equity_basis_cc(committed_only_model)
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
            # CERTIFIED-HEDGE EV BUDGET (2026-07-18): wired from config, BOTH
            # defaulting to the P0-1 SAFETY DEFAULT (disabled / 0 — byte-
            # identical to the old hardcoded values). Arming admits a
            # negative-EV fill ONLY when the gate CERTIFIES it risk-reducing
            # (POST governing UNCLAMPED expected tail loss <= PRE, common
            # random numbers — never vacuous on a profit-clamped tail) AND its
            # EV cost fits the budget — sim/book_risk._candidate_gate.
            hedge_cost_budget_cc=self._config.hedge_cost_budget_cc,
            allow_negative_ev_hedge=self._config.allow_negative_ev_hedge,
            # P1 EV VISIBILITY: the OPTIONAL worst-challenger-EV tolerance. −inf by
            # default (no behaviour change — the gate stays production-model-EV only);
            # a finite operator value ALSO declines a +production-EV candidate whose
            # worst credible challenger EV falls below it (strictly additive).
            worst_challenger_ev_tolerance=self._config.worst_challenger_ev_tolerance_cc,
            # MUTEX-AWARE DET-MAX rollback switch: the SAME RiskLimits knob the
            # quote-time cap honors, threaded to the worker gate so knob=False
            # restores comonotone gating at BOTH sites (verify finding 2026-07-18).
            det_max_mutex_aware=bool(limits.portfolio_det_max_mutex_aware),
            # P0-2 staleness stamps (see _build_candidate_gate_inputs docstring).
            input_generation=input_generation,
            reservation_version=reservation_version,
        )

    def _reserve_headroom(
        self,
        reservation_id: str,
        quote_id: str,
        state: OpenQuoteState,
        *,
        waived_games: Mapping[str, GameWorstCase] | None = None,
    ) -> ReserveResult | None:
        """Reserve risk headroom for a contemplated fill BEFORE the confirm
        round-trip (R3 Phase 3). Returns the ``ReserveResult`` (granted, or
        denied WITH its enforced breaches — the last-look MC waiver needs the
        breach reasons, never just a bool), or None when no reservation service
        is wired (proceed with the confirm — behaviour unchanged from Phase 2:
        the check already ran at last look; the race only matters under fan-out).

        With a service, the reservation re-checks the caps against
        committed + all outstanding reservations + this fill, atomically, and
        consumes the headroom on grant. Denied ⇒ ``granted`` False (an ENFORCED
        cap breach — impossible while caps_shadow_mode is True, so SHADOW-mode
        behaviour is unchanged; real once the operator flips caps to enforce).
        The reservation SHARES the lifecycle's shadow split, so a shadow breach
        never denies.

        ``waived_games`` (CONFIRM-PATH last-look MC waiver): per-game
        state-consistent worst-case certificates, passed ONLY by the waiver's
        single reservation RETRY after a denial whose every enforced breach was
        a game-loss / mutex-directional cap breach. Forwarded verbatim to the
        service (which re-validates each certificate against the live game-loss
        budget at the check site). Every other caller leaves the default None.

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
            return None
        candidate = self._fill_position(quote_id, state)
        return self._reservation.try_reserve(
            reservation_id,
            candidate,
            marginals=self._marginals,
            daily_pnl=self.daily_pnl,
            risk_bankroll_cc=self._risk_bankroll_cc(),
            bankroll_source_configured=self._bankroll_source_configured(),
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
            book_risk=self._book_risk_for_check(),
            waived_games=waived_games,
            # Confirm-time resting haircut (operator 2026-07-17): committed +
            # reservations + candidate stay at 100%; only the resting fold
            # weights. Default False = today; armed in the local YAML.
            apply_resting_haircut=self._config.resting_haircut_at_confirm,
        )

    # ------------------------------------------- last-look MC waiver (Problem A)

    async def _lastlook_mc_waiver(
        self,
        quote_id: str,
        state: OpenQuoteState,
        reservation_id: str,
        breaches: list[Breach],
    ) -> tuple[bool, str]:
        """CONFIRM-PATH LAST-LOOK MC WAIVER (handoff Problem A). Called ONLY when
        the confirm-path reservation was DENIED. Returns ``(True, "")`` when the
        waiver was granted AND the single reservation retry succeeded (the
        headroom is now HELD — the caller proceeds to the candidate gate exactly
        as after a first-try grant), else ``(False, detail)`` and the caller
        declines ``DECLINE_RISK_LIMIT`` exactly as today.

        Semantics (design fixed 2026-07-16 — sim/state_worst_case.py):
          - Runs ONLY when enabled AND EVERY enforced breach in the denial is a
            game-loss / mutex-directional cap breach carrying its game key
            (``WAIVABLE_RESERVATION_BREACHES``). ANY other breach ⇒ (False, …)
            with no waiver attempt — those caps are never waived.
          - Builds picklable inputs ON-LOOP (committed positions + THE CANDIDATE
            as fully-netting entities; outstanding reservations as hedge-credit-
            CLAMPED entities — a released reservation vanishes like an unfilled
            quote, so its credit never certifies; every resting quote as
            adversarial max(0, loss) hypotheticals), stamped with the FULL
            exposure-book generation (position AND quote mutations — the input
            set includes open quotes, so bare quote churn must invalidate it)
            + reservation version (the P0-2 pattern, widened), and runs the
            EXACT scoreline enumeration OFF-LOOP via ``BookRiskPool.
            run_state_worst_case`` bounded by ``lastlook_mc_waiver_deadline_s``.
          - If either stamp moved during the enumeration the verdict priced a
            book that no longer exists: REBUILD ONCE, then fail-closed decline.
            TRIM ARMED (topk > 0, 2026-07-18): quote-churn stability is judged
            against the trim-SELECTED set + tail adder instead of the full
            same-game id set — same-game churn wholly outside the enumerated
            selection and within the adder budget no longer invalidates
            (``_waiver_trim_revalidate``); position/reservation stamps stay
            exact.
          - Every breached game must come back CERTIFIED with worst_case_cc
            within the SAME game-loss budget (threshold_cc(game_loss_frac,
            bankroll) — never a raised one; re-validated AGAIN by the checker at
            the retry). Then the reservation is retried ONCE with the per-game
            certificates; a retry denial (a different cap now binds, or the book
            moved) declines.
          - ANY error / timeout / uncertified game / over-budget certificate /
            unstable book / missing structural config or bankroll ⇒ (False, …):
            the decline path is byte-identical to today's (fail-closed).

        The QUOTE-TIME analytic caps are untouched (E2 mass-acceptance dominance
        needs them MONOTONE; this state-consistent bound is not — the module
        docstring carries the warning)."""
        if not self._config.lastlook_mc_waiver_enabled:
            return False, ""
        if self._reservation is None:  # a denial implies a service; belt+braces
            return False, ""
        # SLATE breaches are certificate-RESOLVABLE, not waivable (2026-07-17):
        # the slate cap sums the per-game analytic losses, and the retry's
        # certificate-aware roll-up substitutes each certified game's exact
        # worst case — so a denial carrying slate breaches ALONGSIDE per-game
        # waivable breaches still arms the waiver; the retry then re-checks
        # the slate HONESTLY on the substituted sum (fail-closed if it still
        # breaches). A slate-ONLY denial has no game to certify ⇒ decline.
        core = [b for b in breaches if b.reason is not ReasonCode.SKIP_SLATE_CAP]
        if not core or any(
            b.reason not in WAIVABLE_RESERVATION_BREACHES for b in core
        ):
            # A non-waivable cap (gross/per-combo/daily/CVaR/ruin/notional/
            # halt…) is part of the denial: never waived, decline as today.
            return False, "non-waivable breach in denial"
        if any(b.game is None for b in core):
            # A per-game breach without its game key cannot be certified.
            return False, "waivable breach missing its game key (fail-closed)"
        games = sorted({b.game for b in core if b.game is not None})
        bankroll_cc = self._risk_bankroll_cc()
        if bankroll_cc is None or bankroll_cc <= 0:
            # The %-caps needed a bankroll to breach, but it may have gone stale
            # between the denial and here — an unknowable budget is never waived.
            return False, "bankroll unavailable for the waiver budget"
        structural_cfg = self._structural_cfg
        if structural_cfg is None:
            return False, "no structural config — state enumeration impossible"
        game_thr_cc = threshold_cc(self._limits.limits.game_loss_frac, bankroll_cc)

        self._metrics.inc("lastlook_waiver.attempted")
        audit: dict[str, Any] = {
            "granted": False,
            "worst_case_cc": None,
            "games": games,
        }
        self._waiver_audit = audit
        start_ns = self._clock.monotonic_ns()
        deadline_ns = int(self._config.lastlook_mc_waiver_deadline_s * 1e9)
        result: dict[str, GameWorstCase] | None = None
        trim_adders: dict[str, int] = {}
        selected_sizes: dict[str, int] = {}
        topk = self._config.lastlook_waiver_topk_resting
        # One build + AT MOST ONE rebuild (version moved during the enumeration),
        # then fail-closed decline — the mandated retry budget.
        for attempt in range(2):
            fp_before = self._waiver_games_fingerprint(games)
            try:
                inputs = self._build_state_worst_case_inputs(
                    quote_id, state, structural_cfg
                )
                # WAIVER ENTITY-SET TRIM (2026-07-18): keep the K largest
                # resting quotes per breached game; the dropped tail rides as a
                # constant conservative adder on each breached game's
                # certificate (applied below, BEFORE the budget check and the
                # reservation retry — the checker re-validates the RAISED
                # bound). Entities (committed + reservations + candidate) are
                # never trimmed. topk == 0 (default) is today's full set.
                if topk > 0:
                    kept, trim_adders = trim_open_quotes_for_games(
                        inputs.open_quotes, games, inputs.events, topk
                    )
                    # The SELECTED set's identity+size — the trimmed stability
                    # key (2026-07-18): what the enumeration actually prices,
                    # revalidated (with the adder) after the off-loop await.
                    selected_sizes = {
                        q.quote_id: q.worst_hit_loss_cc for q in kept
                    }
                    if len(kept) != len(inputs.open_quotes):
                        self._metrics.inc("lastlook_waiver.trimmed")
                        log.info(
                            "lastlook_waiver_trimmed",
                            quote_id=quote_id,
                            kept_quotes=len(kept),
                            dropped_quotes=len(inputs.open_quotes) - len(kept),
                            adders_cc=dict(trim_adders),
                            topk=topk,
                        )
                    inputs = replace(inputs, open_quotes=kept)
            except Exception as exc:  # noqa: BLE001 — any build error declines
                self._metrics.inc("lastlook_waiver.errored")
                log.error(
                    "lastlook_waiver_errored", quote_id=quote_id, error=repr(exc)
                )
                return False, f"waiver build errored: {exc!r}"
            remaining_s = (
                deadline_ns - (self._clock.monotonic_ns() - start_ns)
            ) / 1e9
            if remaining_s <= 0.0:
                self._metrics.inc("lastlook_waiver.timeout")
                log.warning(
                    "lastlook_waiver_deadline",
                    quote_id=quote_id,
                    attempt=attempt,
                    detail="waiver wall budget exhausted before the enumeration",
                )
                return False, "waiver deadline exhausted"
            try:
                candidate_result = await self._run_state_worst_case(
                    inputs, deadline_s=remaining_s
                )
            except TimeoutError:
                self._metrics.inc("lastlook_waiver.timeout")
                log.warning(
                    "lastlook_waiver_deadline",
                    quote_id=quote_id,
                    attempt=attempt,
                    detail="off-loop enumeration exceeded the waiver deadline",
                )
                return False, "waiver enumeration timed out"
            except Exception as exc:  # noqa: BLE001 — any error declines
                self._metrics.inc("lastlook_waiver.errored")
                log.error(
                    "lastlook_waiver_errored", quote_id=quote_id, error=repr(exc)
                )
                return False, f"waiver enumeration errored: {exc!r}"
            # P0-2 (widened for the waiver): the enumeration awaited off-loop —
            # verify the book did not move under it. The stamp is the FULL
            # ExposureBook.generation (not the position generation): the input
            # set includes every resting open quote, and upsert_quote/
            # remove_quote bump ONLY the full generation, so a quote landing
            # (or repricing/expiring) during the await would otherwise be
            # invisible and the stale certificate would skip the per-game caps
            # on a book it never priced (findings 1+3, 2026-07-16).
            # Stability key (2026-07-18): position generation + reservation
            # version + the BREACHED games' resting-quote id set — NOT the
            # full book generation (see _waiver_games_fingerprint).
            fp_after = self._waiver_games_fingerprint(games)
            if topk > 0:
                # TRIMMED STABILITY (2026-07-18 — the "waiver unstable: book
                # moved during every enumeration" churn fix, 51 live declines
                # 2026-07-17 night): the enumeration priced only the trim-
                # SELECTED top-K quotes per breached game plus a CONSTANT tail
                # adder, so the stability key is that certificate's own
                # support — not the full same-game id set (churn among small
                # quotes the trim never priced was invalidating certificates
                # whose bound still held). The stamps (position generation +
                # reservation version) stay EXACT: committed fills and
                # reservation churn are real risk changes, never waived
                # through. Quote churn is then judged by grant-time
                # revalidation (``_waiver_trim_revalidate``): the certificate
                # stays valid iff every still-present SELECTED quote is
                # byte-identical (id + priced size) and the CURRENT outside-
                # selection tail still fits the enumerated adder — then
                # (trimmed worst + adder) still upper-bounds the CURRENT
                # book's worst case. Anything else fails closed exactly as
                # today (retry once, then the unstable decline).
                conflict_why: str | None = None
                if fp_after[:2] != fp_before[:2]:
                    conflict_why = (
                        "positions / reservations moved during the enumeration"
                    )
                else:
                    trim_ok, trim_why = self._waiver_trim_revalidate(
                        games, selected_sizes, trim_adders
                    )
                    if not trim_ok:
                        conflict_why = trim_why
                if conflict_why is not None:
                    self._metrics.inc("lastlook_waiver.version_conflict")
                    log.info(
                        "lastlook_waiver_version_conflict",
                        quote_id=quote_id,
                        attempt=attempt,
                        detail="breached-game resting set / positions / "
                        "reservations moved during the enumeration",
                        why=conflict_why,
                    )
                    continue
                if fp_after != fp_before:
                    # Same-game churn DID happen — but entirely outside the
                    # certificate's support and within its adder budget: the
                    # newly-tolerated case (a spurious unstable-decline before
                    # this fix).
                    log.debug(
                        "lastlook_waiver_tail_churn_tolerated",
                        quote_id=quote_id,
                        attempt=attempt,
                        detail="waiver stable: tail churn within adder",
                    )
            elif fp_after != fp_before:
                self._metrics.inc("lastlook_waiver.version_conflict")
                log.info(
                    "lastlook_waiver_version_conflict",
                    quote_id=quote_id,
                    attempt=attempt,
                    detail="breached-game resting set / positions / "
                    "reservations moved during the enumeration",
                )
                continue
            result = candidate_result
            break
        if result is None:
            return False, "waiver unstable: book moved during every enumeration"

        certs: dict[str, GameWorstCase] = {}
        for game in games:
            cert = result.get(game)
            if cert is None or not cert.certified:
                self._metrics.inc("lastlook_waiver.declined_uncertified")
                log.info(
                    "lastlook_waiver_uncertified",
                    quote_id=quote_id,
                    game=game,
                    reason=None if cert is None else cert.uncertified_reason,
                )
                return False, f"game {game} not certifiable"
            # Dropped-tail adder (trim armed): fold the constant conservative
            # adder INTO the certificate itself, so both the budget check below
            # AND the checker's re-validation at the reservation retry see the
            # RAISED bound (a certificate understating the dropped tail must
            # never reach the enforcement site).
            adder_cc = trim_adders.get(game, 0)
            if adder_cc:
                cert = replace(cert, worst_case_cc=cert.worst_case_cc + adder_cc)
            certs[game] = cert
        worst_cc = max(cert.worst_case_cc for cert in certs.values())
        audit["worst_case_cc"] = worst_cc
        if topk > 0:
            # Trim observability (armed only — the default audit shape is
            # unchanged): the per-game dropped-tail adders inside the bound.
            audit["trim_adders_cc"] = dict(trim_adders)
        if worst_cc > game_thr_cc:
            self._metrics.inc("lastlook_waiver.declined_over_budget")
            log.info(
                "lastlook_waiver_over_budget",
                quote_id=quote_id,
                worst_case_cc=worst_cc,
                budget_cc=game_thr_cc,
                games=games,
            )
            return False, (
                f"state-consistent worst case {worst_cc}cc > game-loss budget "
                f"{game_thr_cc}cc"
            )
        # Certified within the SAME budget: retry the reservation ONCE with the
        # certificates. The checker re-validates each one against the LIVE
        # budget and skips ONLY the game-loss/directional caps for these games;
        # every other cap is re-checked in full — a new breach still denies.
        # Synchronous from the version check to here (no await), so the book
        # cannot have moved since the stamps were verified.
        retry = self._reserve_headroom(
            reservation_id, quote_id, state, waived_games=certs
        )
        if retry is None or not retry.granted:
            self._metrics.inc("lastlook_waiver.retry_denied")
            log.info(
                "lastlook_waiver_retry_denied",
                quote_id=quote_id,
                breaches=[]
                if retry is None
                else [str(b.reason) for b in retry.breaches],
            )
            return False, "reservation retry denied despite certificates"
        self._metrics.inc("lastlook_waiver.granted")
        audit["granted"] = True
        log.info(
            "lastlook_waiver_granted",
            quote_id=quote_id,
            games=games,
            worst_case_cc=worst_cc,
            budget_cc=game_thr_cc,
            n_states={g: certs[g].n_states for g in games},
        )
        return True, ""

    def _build_state_worst_case_inputs(
        self,
        quote_id: str,
        state: OpenQuoteState,
        structural_cfg: StructuralConfigView,
    ) -> StateWorstCaseInputs:
        """Build the IMMUTABLE, picklable inputs for ONE off-loop state-consistent
        worst-case enumeration (the last-look MC waiver), ON-LOOP.

        Entities = committed positions + ALL outstanding reservations + THE
        CANDIDATE. Committed positions and the candidate net FULLY per state;
        outstanding reservations ride with ``earns_credit=False`` (hit-side
        loss sums, miss-side credit CLAMPED away): a reservation is not a real
        holding — an explicit decline/lapse ``release`` vanishes it exactly
        like an unfilled quote, so its hedge credit must never certify a book
        that outlives it (finding 2, 2026-07-16). Unlike the candidate gate
        there is no exclusion of this fill's own reservation: the waiver runs
        only AFTER the reservation was DENIED, so nothing is held for this
        candidate. Open quotes ride as adversarial hypotheticals (max(0, loss)
        per state — the E2 rationale at confirm). NOTE (conservative,
        intended): this quote's OWN open-quote record is still in the book here
        (dropped only at the end of ``on_quote_accepted``), so the enumeration
        counts this fill once as the candidate entity and once as its resting
        quote's clamped hypothetical — the same fail-conservative double-count
        the analytic reservation check makes (see ``_reserve_headroom``); it
        can only OVERSTATE the certified bound, never understate it.

        Marginals resolve ON-LOOP into a plain dict (a missing marginal is
        OMITTED — the enumeration drops that leg from the model INVERSION only;
        per-state settlement is marginal-free). ``events`` is None: every LegRef
        carries its event ticker from the RFQ; a leg without one resolves
        adversarially (never a credit — fail-conservative). Stamped with the
        FULL exposure-book generation (quote mutations included — this input
        set prices open quotes, so bare quote churn must invalidate it; see
        ``StateWorstCaseInputs``) + reservation version at this read (P0-2,
        widened)."""
        candidate = self._fill_position(quote_id, state)
        committed = tuple(self._exposure.positions.values())
        book_generation = self._exposure.generation
        if self._reservation is not None:
            reservation_version = self._reservation.version
            reservations = tuple(self._reservation.outstanding_positions())
        else:
            reservation_version = -1
            reservations = ()
        # MAKER-FEE accounting for THE CANDIDATE (2026-07-16, eat-the-fee — the
        # review-LOW fee_cc=0 hole): on a maker-fee-active series the candidate's
        # predicted fill fee is a real per-state cash cost, so it rides on the
        # candidate's WorstCaseEntity (hit_loss = premium + fee). Gated on the
        # prefix list: empty (the default) ⇒ fee_cc=0, bit-identical waiver
        # inputs. A None fee (no model / UNKNOWN) stays 0 — the pre-fix figure,
        # never an invented cost.
        # TODO(2026-07-16): COMMITTED positions (and outstanding reservations)
        # still ride fee_cc=0 — threading their at-fill fee here needs
        # OpenPosition to carry it (out of scope for this change; conservative
        # direction is unaffected because a real fee only ever ADDS loss).
        candidate_fee_cc = 0
        pending = state.pending_fill
        if pending is not None and self._maker_fee_active(
            state.rfq.market_ticker, state.rfq.mve_collection_ticker
        ):
            predicted = self._fill_fee_cc(
                pending[1],
                pending[2],
                combo_ticker=state.rfq.market_ticker,
                collection=state.rfq.mve_collection_ticker,
            )
            if predicted is not None:
                candidate_fee_cc = int(predicted)
        entities = (
            *(entity_from_position(position) for position in committed),
            *(
                entity_from_position(position, earns_credit=False)
                for position in reservations
            ),
            entity_from_position(candidate, fee_cc=candidate_fee_cc),
        )
        open_quotes = tuple(
            quote_from_open_quote(quote, self._conventions)
            for quote in self._exposure.open_quotes.values()
        )
        tickers: set[str] = set()
        for entity in entities:
            tickers.update(leg.market_ticker for leg in entity.legs)
        for quote in open_quotes:
            for hypothetical in quote.hypotheticals:
                tickers.update(leg.market_ticker for leg in hypothetical.legs)
        marginals: dict[str, float] = {}
        for ticker in sorted(tickers):
            p = self._marginals(ticker)
            if p is not None:
                marginals[ticker] = float(p)
        return StateWorstCaseInputs(
            entities=entities,
            open_quotes=open_quotes,
            marginals=marginals,
            events=None,
            structural_cfg=structural_cfg,
            book_generation=book_generation,
            reservation_version=reservation_version,
        )

    def _waiver_games_fingerprint(
        self, games: list[str]
    ) -> tuple[int, int, tuple[str, ...]]:
        """Stability key for a waiver enumeration (2026-07-18): the POSITION
        generation + reservation version + the ids of the resting quotes
        touching the BREACHED games. A quote landing/expiring on an UNRELATED
        game cannot change the breached games' certified worst case, so it no
        longer invalidates the run — at 400+ quotes/min the old FULL-generation
        stamp made the waiver un-runnable ("book moved during every
        enumeration", observed live on a +$1.76 EV $31 win). Same-game
        quote arrivals and any position/reservation change still invalidate
        (the 2026-07-16 stale-certificate findings stay covered — quotes are
        immutable per id; a reprice replaces the id).

        TRIM ARMED (``lastlook_waiver_topk_resting > 0``, 2026-07-18): only
        the first two components (position generation + reservation version)
        are compared exactly; the quote-id set is judged instead by grant-time
        revalidation against the trim's SELECTED set + tail adder
        (``_waiver_trim_revalidate``) — churn among same-game quotes the
        enumeration never priced no longer invalidates a certificate whose
        bound provably still holds. The id set still feeds the tolerated-churn
        debug log."""
        gset = set(games)
        qids = tuple(sorted(
            qid for qid, q in self._exposure.open_quotes.items()
            if any(
                leg.event_ticker and game_key(leg.event_ticker) in gset
                for leg in q.legs
            )
        ))
        return (
            self._exposure.position_generation,
            self._reservation.version if self._reservation is not None else -1,
            qids,
        )

    def _waiver_trim_revalidate(
        self,
        games: list[str],
        selected_sizes: Mapping[str, int],
        adders: Mapping[str, int],
    ) -> tuple[bool, str]:
        """Grant-time revalidation of a TRIMMED waiver enumeration (2026-07-18
        — runs ON-LOOP after the off-loop await, atomically with the
        reservation retry). Returns ``(still_valid, why)``.

        GRANT CONDITION (the simplest sound sufficient condition; anything
        else fails closed): for every breached game, the CURRENT tail — the
        summed ``worst_hit_loss_cc`` of all current same-game quotes NOT in
        the enumerated selection — must be <= the tail adder the enumeration
        folded into the certificate, AND every still-present SELECTED quote
        must be unchanged (same id, same priced size).

        SOUNDNESS: per state a quote contributes ``max(0, loss) <=
        worst_hit_loss_cc`` (state-independent), so for every state s of a
        breached game:  current_total(s) <= enumerated_trimmed_total(s) +
        current_tail <= enumerated_trimmed_total(s) + adder — i.e. the
        certificate (trimmed worst + adder) still upper-bounds the CURRENT
        book's worst case. A SELECTED quote that VANISHED is conservative
        (its enumerated clamped contribution was >= 0), so it never blocks; a
        selected quote whose content changed under its id makes the
        enumerated per-state terms stale ⇒ fail closed. Entities (positions/
        reservations) are outside this check — the caller compares those
        stamps exactly and never waives through them."""
        current = tuple(
            quote_from_open_quote(quote, self._conventions)
            for quote in self._exposure.open_quotes.values()
        )
        tails, mutated = tail_outside_selection(
            current, games, None, selected_sizes
        )
        if mutated:
            return False, (
                f"selected resting quotes mutated under their ids: "
                f"{list(mutated)}"
            )
        for game in games:
            tail_cc = tails.get(game, 0)
            adder_cc = adders.get(game, 0)
            if tail_cc > adder_cc:
                return False, (
                    f"game {game} current tail {tail_cc}cc exceeds the "
                    f"enumerated adder {adder_cc}cc"
                )
        return True, ""

    async def _run_state_worst_case(
        self, inputs: StateWorstCaseInputs, *, deadline_s: float
    ) -> dict[str, GameWorstCase]:
        """Run one waiver enumeration: in the BookRiskPool worker when wired
        (bounded by ``deadline_s`` — a timeout propagates and the caller declines
        fail-closed), else inline (paper/backtests/tests — deterministic exact
        enumeration, identical result; the caller's pre-run deadline guard still
        bounds a rebuild). Mirrors ``_run_candidate_mc``."""
        if self._book_risk_pool is not None:
            return await self._book_risk_pool.run_state_worst_case(
                inputs, deadline_s=deadline_s
            )
        return _worker_state_worst_case(inputs)

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

    def _rfq_gone(self, rfq: Rfq) -> bool:
        """F2 liveness probe: True iff the intake registry POSITIVELY says the
        RFQ was already deleted. No registry wired (None — paper/backtests/
        tests), or a probe error, ⇒ False: proceed exactly as today. That
        proceed-on-unknown is deliberately NOT a fail-open money hole — a
        deleted RFQ can never fill (the POST 409s ``rfq_closed`` and is
        handled), so this gate is pure waste removal and must never be able to
        turn a probe bug into a quote blackout."""
        if self._rfq_alive is None:
            return False
        try:
            return not self._rfq_alive(rfq.rfq_id)
        except Exception:
            log.exception("rfq_liveness_probe_failed", rfq_id=rfq.rfq_id)
            return False

    async def _skip_dead_rfq(self, rfq: Rfq, stage: str) -> None:
        """Record one F2 mid-flight-delete skip: per-stage metric + the shared
        reason code with the stage in context (same-decline-different-stage
        discipline — the tape's reason vocabulary stays stable while the stage
        attribution rides the context/metrics)."""
        self._metrics.inc(f"rfq.liveness_skip.{stage}")
        await self._record_skip(
            rfq, [ReasonCode.SKIP_RFQ_DELETED_MIDFLIGHT], {"stage": stage}
        )

    def _pre_pricing_breaches(self) -> list[Breach]:
        """F1 MONOTONE PRE-PRICING GATE (throughput synthesis 2026-07-16).

        The candidate-FREE ``limits.check`` (the exact call the maintenance
        tick already makes, plus ``adding_quote=True``) filtered to the
        ENFORCED candidate-monotone subset (``monotone_pre_quote_breaches``):
        every returned breach provably persists under ANY candidate, so a
        pre-decline here is the SAME decline the full post-pricing check would
        have produced — minus the joint pricing, snapshots, and POST. Cached
        per (exposure generation, bankroll, ≤0.5s): all allowlisted cap inputs
        are generation-static or the bankroll itself. Validated
        prototype-first in tools/proto_pre_pricing_gate.py (fuzz 0 violations
        + exclusion counterexamples + tape replay + part-D port parity)."""
        gen = self._exposure.generation
        bankroll = self._risk_bankroll_cc()
        now = self._clock.monotonic_ns()
        cached = self._pre_gate_cache
        if (
            cached is not None
            and cached[0] == gen
            and cached[1] == bankroll
            and now - cached[2] <= int(_PRE_GATE_CACHE_TTL_S * 1e9)
        ):
            self._metrics.inc("pre_gate.cache_hit")
            return cached[3]
        raw = self._limits.check(
            self._exposure,
            self._marginals,
            self.daily_pnl,
            adding_quote=True,
            risk_bankroll_cc=bankroll,
            bankroll_source_configured=self._bankroll_source_configured(),
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
            book_risk=self._book_risk_for_check(),
            # QUOTE-TIME resting haircut: the pre-gate MUST share handle_rfq's
            # haircut semantics — the F1 lemma ("gate fires ⇒ the full
            # quote-time check declines") holds only when both fold resting
            # quotes identically (re-verified armed in
            # tools/proto_resting_haircut.py part D2). No-op at weight 1.
            apply_resting_haircut=True,
        )
        # Shadow-split FIRST (the one shadow-enforcement seam), then the
        # monotone filter (which also drops shadow, belt-and-suspenders).
        breaches = monotone_pre_quote_breaches(self._partition_breaches(raw))
        self._pre_gate_cache = (gen, bankroll, now, breaches)
        self._metrics.inc("pre_gate.check")
        return breaches

    # ------------------------------------------------------------------ intake

    async def handle_rfq(self, rfq: Rfq) -> None:
        # F2 liveness check #1 — on dequeue, before ANY work (the RFQ may have
        # been deleted while queued; nothing purges the rfq_work queue itself).
        if self._rfq_gone(rfq):
            await self._skip_dead_rfq(rfq, "pre_price")
            return
        reasons = self._filter.evaluate(rfq)
        if reasons:
            await self._record_skip(rfq, reasons, self._pregame_flow_context(rfq, reasons))
            return
        # F1 monotone pre-pricing gate (default OFF = today's behaviour). A
        # candidate-monotone cap already breached WITHOUT this RFQ means the
        # full check after pricing MUST decline it too — same reason codes,
        # earlier exit (the stage rides the context), pricing work never spent.
        # The watchdog still sees the would-be decline (constraint: a mis-set
        # cap silently declining everything must surface identically).
        if self._config.pre_pricing_gate_enabled:
            pre = self._pre_pricing_breaches()
            if pre:
                self._metrics.inc("pre_gate.declined")
                self._note_watchdog(risk_declined=True)
                await self._record_skip(
                    rfq,
                    [b.reason for b in pre],
                    {"stage": "pre_pricing", "details": [b.detail for b in pre]},
                )
                return
        result = await self._price_async(rfq)
        if isinstance(result, NoQuote):
            await self._record_skip(rfq, [result.reason], {"detail": result.detail})
            return
        # F2 liveness check #2 — after the joint returned (queue dwell + pool
        # dwell is where most mid-flight deletes land): stop before spending
        # the risk snapshots + POST on a dead RFQ. Deliberately AFTER the
        # NoQuote branch, so pricing-failure reason tallies (the research
        # denominator) are unchanged — only would-be downstream work converts.
        if self._rfq_gone(rfq):
            await self._skip_dead_rfq(rfq, "post_price")
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
            # QUOTE-TIME resting haircut (operator design 2026-07-17): resting
            # quotes fold at resting_quote_weight with the top-K burst floor —
            # the confirm path keeps counting them at 100% and enforces the
            # budgets EXACTLY (reservations + candidate MC + waiver), so the
            # 100% fold here was a double count of that defense. The CANDIDATE
            # (this RFQ's hypothetical fill) is never haircut. No-op at the
            # default weight 1.
            apply_resting_haircut=True,
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

        # F2 liveness check #3 — immediately before the POST. The last cheap
        # exit before a full REST round-trip holds one of the 8 workers and
        # burns write budget on a certain ``rfq_closed``. Placed AFTER the risk
        # check + watchdog so risk-decline tallies/audits are byte-identical.
        if self._rfq_gone(rfq):
            await self._skip_dead_rfq(rfq, "pre_post")
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
        # Fresh confirm ⇒ fresh waiver audit (set only if a waiver attempt runs).
        self._waiver_audit = None
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
            # R3 Phase 3 + P0-2: RESERVE headroom BEFORE the confirm round-trip AND
            # before the candidate MC (atomic + versioned). Creating the PROVISIONAL
            # reservation FIRST — under the analytic hard caps — is the P0-2 fix: a
            # concurrent accept's own candidate MC now sees this candidate's HELD
            # headroom (its reservation is folded into every reserve() check AND its
            # bump moves the reservation VERSION the candidate gate watches), so two
            # accepts can no longer each pass their MC against the same old pre-book.
            # An ENFORCED-denied reservation — impossible in Phase-2 SHADOW mode, real
            # once caps are flipped — declines here (the last headroom went elsewhere).
            reservation_id = f"fill:{quote_id}"
            reserve_result = self._reserve_headroom(reservation_id, quote_id, state)
            if reserve_result is not None and not reserve_result.granted:
                # LAST-LOOK MC WAIVER (handoff Problem A): when enabled AND every
                # enforced breach in this denial is a game-loss/mutex-directional
                # cap breach, evaluate the STATE-CONSISTENT per-game worst case by
                # exact scoreline enumeration OFF-LOOP and retry the reservation
                # ONCE with the per-game certificates (same game-loss budget,
                # never a raised one). Granted ⇒ the headroom is HELD and the
                # confirm proceeds through the candidate gate exactly as after a
                # first-try grant (the gate can still decline and releases the
                # reservation). Disabled / any other breach / any error, timeout,
                # uncertified game, over-budget, or unstable book ⇒ decline
                # exactly as before (fail-closed).
                waived, waiver_detail = await self._lastlook_mc_waiver(
                    quote_id, state, reservation_id, reserve_result.breaches
                )
                if not waived:
                    self._metrics.inc(
                        f"confirm.declined.{ReasonCode.DECLINE_RISK_LIMIT}"
                    )
                    self._track_markout(f"declined:{quote_id}", state)
                    detail = "risk reservation denied at confirm (no headroom)"
                    if waiver_detail:
                        detail = f"{detail}; waiver: {waiver_detail}"
                    await self._record_confirm_decision(
                        state, confirm=False, reason=ReasonCode.DECLINE_RISK_LIMIT,
                        detail=detail,
                        decision_ms=decision_ms,
                    )
                    self._executed_states.pop(quote_id, None)
                    state.pending_fill = None
                    self._drop_quote(quote_id)
                    return
            # P0-1/P0-2 CANDIDATE-AWARE PORTFOLIO-RISK GATE (last look), ATOMIC with
            # the reservation just made. The existing analytic/gross/burst gates AND
            # the provisional reservation have ADMITTED this fill; now run an
            # ADDITIONAL candidate-aware ~20k-sample portfolio MC over the merged PRE
            # (committed + all OTHER outstanding reservations + this candidate's
            # provisional reservation, folded in as the candidate) and confirm ONLY
            # when the candidate's marginal EV is positive AND the POST-book joint-tail
            # / ruin / deterministic / gross budgets pass. The gate captures the book
            # generation + reservation version with its inputs and rebuilds+retries
            # (bounded by the confirm deadline) if either moves under the off-loop MC,
            # so its verdict is atomic with the reservation book. STRICTLY ADDITIVE: it
            # can only flip an ADMIT to a DECLINE, never a decline to an admit. UNKNOWN
            # merged marginal / over-budget POST book / ANY error / an unstable book /
            # insufficient deadline ⇒ DECLINE_CANDIDATE_RISK (fail-closed). On ANY
            # decline the PROVISIONAL reservation is RELEASED (the headroom must not
            # linger for a fill we are not making). Disabled by config ⇒ skipped (kill
            # switch + prior behaviour), and the reservation stays as before.
            if self._config.candidate_gate_enabled:
                gate_ok, gate_detail = await self._candidate_gate_verdict(
                    quote_id, state,
                    reservation_id=(
                        reservation_id if self._reservation is not None else None
                    ),
                )
                if not gate_ok:
                    # Release the provisional reservation: this fill is declined, so
                    # its held headroom must be freed immediately (fail-closed — never
                    # confirm, never leave headroom consumed for a non-fill).
                    if self._reservation is not None:
                        self._reservation.release(reservation_id)
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
                # FILL-RECORD RECOVERY (2026-07-16 P1): the confirm SUCCEEDED —
                # a quote_executed message is now EXPECTED. Stamp the clock so
                # the maintenance sweep can poll REST if it never arrives (the
                # WS channel has no replay). Only this success path stamps: a
                # failed/timed-out confirm is the unknown-committed path the
                # reservation-reconcile loop owns.
                state.fill_confirmed_mono_ns = self._clock.monotonic_ns()
                # POST-FILL RISK PULL: scheduled BELOW, after _drop_quote —
                # see the stamp-gated call at the end of this method. It must
                # NOT be scheduled here: between commit (position booked) and
                # the drop, the fill is DOUBLE-counted (position + its own
                # still-resting quote), and the awaited record below yields to
                # the event loop, so a pull scheduled here runs its first
                # check inside that window and can evict an innocent same-game
                # resting quote on a transient breach (2026-07-17 finding).
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
        # POST-FILL RISK PULL (resting-quote haircut): the fill is COMMITTED —
        # re-evaluate resting quotes against the new book (analytic-only
        # background task; no-op unless the haircut is armed). Scheduled ONLY
        # AFTER the filled quote left the exposure book, so the pull's first
        # check never sees the fill double-counted (position + its own
        # still-resting quote) — a transient breach in that window spuriously
        # evicted an innocent same-game resting quote (2026-07-17 finding).
        # ``fill_confirmed_mono_ns`` is stamped ONLY on confirm-send success,
        # so decline/exception paths never schedule (the confirm-timeout path
        # is owned by reservation-reconcile + the on_quote_executed replay,
        # whose own schedule at that hook is already post-drop-safe).
        if state.fill_confirmed_mono_ns is not None:
            self._schedule_risk_evict_on_fill(state)

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

    # ------------------------- post-fill risk pull (resting-quote haircut) --

    def _resting_haircut_armed(self) -> bool:
        return self._limits.limits.resting_quote_weight < 1

    def _schedule_risk_evict_on_fill(self, state: OpenQuoteState) -> None:
        """EVENT-DRIVEN POST-FILL RISK PULL (haircut design point 3): a fill
        just COMMITTED, consuming real budget — schedule an immediate
        analytic-only re-evaluation of the resting quotes against the new book
        (same tick: the task runs at the next await point; the confirm path's
        latency-sensitive tail is never blocked on REST deletes). Armed only
        while the haircut is armed (weight < 1): at weight 1 the quote-time
        fold still counts every resting quote at 100%, so today's behaviour is
        untouched. Single-flight: a running pass picks pending fill games up
        on its next loop iteration."""
        if not self._resting_haircut_armed():
            return
        self._risk_evict_pending_games |= {
            game_key(leg.event_ticker)
            for leg in self._leg_refs(state.rfq)
            if leg.event_ticker
        }
        if self._risk_evict_task is not None and not self._risk_evict_task.done():
            return
        self._risk_evict_task = asyncio.ensure_future(self._risk_evict_after_fill())

    async def _risk_evict_after_fill(self) -> None:
        """Delete resting quotes whose game now shows an ENFORCED quote-time
        breach (haircut semantics) after a committed fill.

        ANALYTIC-ONLY (``limits.check`` — no pricing pool, no MC snapshot is
        recomputed), bounded (each iteration deletes exactly one quote or
        stops; at most the number of open quotes at entry), beat-friendly (the
        heartbeat is beaten per iteration, and every REST delete awaits).
        Victim choice per iteration: a resting, un-accepted quote touching a
        breached game — quotes on the just-filled game(s) first, then the
        largest worst-case loss (the biggest budget release per delete);
        re-check after each delete so no more quotes are pulled than needed.
        Scope: the two per-game caps that carry their game key
        (``EVICTABLE_ON_FILL_BREACHES``); a breach that persists with no
        matching resting quote is the committed book's own (the confirm-path
        exact caps + maintenance sweeps own it — nothing to evict). ERRORS
        FAIL SAFE: an exception leaves the resting quotes standing — every
        accept still faces the exact confirm-time enforcement, and TTL/reprice
        sweeps remain the backstop."""
        try:
            for _ in range(len(self._open) + 1):
                self._beat()
                fill_games = set(self._risk_evict_pending_games)
                raw = self._limits.check(
                    self._exposure,
                    self._marginals,
                    self.daily_pnl,
                    risk_bankroll_cc=self._risk_bankroll_cc(),
                    bankroll_source_configured=self._bankroll_source_configured(),
                    start_time_provider=self._start_time_provider,
                    halt_inputs=self._halt_inputs(),
                    book_risk=self._book_risk_for_check(),
                    apply_resting_haircut=True,
                )
                breached_games = {
                    b.game
                    for b in self._partition_breaches(raw)
                    if b.game is not None and b.reason in EVICTABLE_ON_FILL_BREACHES
                }
                if not breached_games:
                    break
                victim = self._pick_eviction_victim(breached_games, fill_games)
                if victim is None:
                    break  # committed book's own breach — nothing to evict
                self._metrics.inc("risk_evict.on_fill")
                log.info(
                    "risk_evicted_on_fill",
                    quote_id=victim,
                    breached_games=sorted(breached_games),
                )
                await self._delete_quote(
                    victim, ReasonCode.DELETE_RISK_EVICTED_ON_FILL
                )
        except Exception:
            self._metrics.inc("risk_evict.pass_error")
            log.exception("risk_evict_on_fill_failed")
        finally:
            self._risk_evict_pending_games.clear()

    def _pick_eviction_victim(
        self, breached_games: set[str], fill_games: set[str]
    ) -> str | None:
        """The next resting quote to pull: touches a breached game, is not
        mid-confirm (accepted), same-game-as-the-fill first, then largest
        worst-case loss (per-quote worst-side max_loss — the loss-axis figure
        the caps fold). None ⇒ no resting quote touches any breached game."""
        best_key: tuple[int, int, str] | None = None
        best_id: str | None = None
        for quote_id, quote in self._exposure.open_quotes.items():
            state = self._open.get(quote_id)
            if state is None or state.accepted:
                continue  # unknown to us or mid-confirm — never yank
            qgames = {
                game_key(leg.event_ticker)
                for leg in quote.legs
                if leg.event_ticker
            }
            if not (qgames & breached_games):
                continue
            hypos = quote.hypothetical_positions(self._conventions)
            worst_loss = max((h.max_loss_cc for h in hypos), default=0)
            key = (0 if qgames & fill_games else 1, -worst_loss, quote_id)
            if best_key is None or key < best_key:
                best_key = key
                best_id = quote_id
        return best_id

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
        # POST-FILL RISK PULL (resting-quote haircut): covers the paths where
        # the position lands HERE rather than at confirm (confirm timeout →
        # execution, recovery-sweep replay). A duplicate schedule after a
        # confirm-path pull is a cheap no-op re-check (single-flight, and a
        # clean book breaks the pass on its first iteration).
        self._schedule_risk_evict_on_fill(state)
        fill_ref = f"fill:{quote_id}"
        # LEDGER IDEMPOTENCY (2026-07-16 P1): the recovery sweep polls REST for a
        # fill whose WS message never arrived and replays it through THIS path, so
        # a WS+poll race (or an exchange replay) must never double-write the fills
        # ledger — nor double-book the fee into realized P&L / double-count
        # fill.count / markouts. The position booking above stays (idempotent by
        # id); everything from here down runs at most once per fill_ref.
        if state.fill_recorded or await self._store.has_fill(fill_ref):
            state.fill_recorded = True
            log.info("fill_replay_skipped", quote_id=quote_id, fill_ref=fill_ref)
            return
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
        # quadratic fills, the real maker fee once this combo's series is on
        # Kalshi's maker-fee list (maker_fee_active_prefixes — eat-the-fee
        # doctrine: the price was NOT widened, so the fee must be accounted
        # here). None only when no fee model is wired (pre-Phase-6 behaviour)
        # or the fee is UNKNOWN.
        fill_fee_cc = self._fill_fee_cc(
            bid,
            qty,
            combo_ticker=state.rfq.market_ticker,
            collection=state.rfq.mve_collection_ticker,
        )
        # EAT-THE-FEE accounting in the EV ledger: on a maker-fee-active series
        # the predicted fee is a known cash cost of this fill, so the recorded
        # expected edge is net of it (grading expected vs realized stays
        # apples-to-apples). Gated on the prefix list so an EMPTY list is
        # bit-identical to prior behaviour on every ledger row.
        if (
            expected_edge_cc is not None
            and fill_fee_cc is not None
            and self._maker_fee_active(
                state.rfq.market_ticker, state.rfq.mve_collection_ticker
            )
        ):
            expected_edge_cc -= int(fill_fee_cc)
        inserted = await self._store.record_fill(
            fill_ref,
            order_id=str(msg.get("order_id")) if msg.get("order_id") else None,
            combo_ticker=state.rfq.market_ticker,
            our_side=str(our_side),
            contracts_centi=int(qty),
            price_cc=int(bid),
            fee_cc=fill_fee_cc,
            expected_edge_cc=expected_edge_cc,
            raw=msg,
        )
        state.fill_recorded = True
        if not inserted:
            # Store-level INSERT-if-absent caught a WS+poll race that slipped
            # past the has_fill pre-check (both racers read before either
            # wrote): exactly one row exists; this racer books nothing more.
            log.info("fill_replay_skipped", quote_id=quote_id, fill_ref=fill_ref)
            return
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

    def _maker_fee_active(
        self, combo_ticker: str | None, collection: str | None
    ) -> bool:
        """Whether this combo sits on a series Kalshi charges MAKER fees on
        (FeeConfig.maker_fee_active_prefixes — the operator mirrors Kalshi's
        maker-fee list, monitored via GET /series/fee_changes). Prefix-matched
        against BOTH the combo market ticker and its collection ticker (combo
        tickers embed the collection blob, but matching both keeps either
        spelling honest). Empty list (the default) ⇒ False everywhere —
        bit-identical prior behaviour."""
        if not self._maker_fee_active_prefixes:
            return False
        for prefix in self._maker_fee_active_prefixes:
            if combo_ticker and combo_ticker.startswith(prefix):
                return True
            if collection and collection.startswith(prefix):
                return True
        return False

    def _effective_fee_type(
        self, combo_ticker: str | None, collection: str | None
    ) -> FeeType:
        """The fee type OUR fill on this combo is charged under. A QUADRATIC
        series on the maker-fee list upgrades to QUADRATIC_WITH_MAKER_FEES so
        the real FeeModel (pricing/fees.py — never reimplemented, rule 8) picks
        the verified maker coefficient. Non-quadratic configured types pass
        through untouched (FLAT/UNKNOWN still raise FeeUnknownError inside the
        model — fail-closed, never a guessed coefficient)."""
        if self._fee_type is FeeType.QUADRATIC and self._maker_fee_active(
            combo_ticker, collection
        ):
            return FeeType.QUADRATIC_WITH_MAKER_FEES
        return self._fee_type

    def _fill_fee_cc(
        self,
        bid: CentiCents,
        qty: CentiContracts,
        *,
        combo_ticker: str | None = None,
        collection: str | None = None,
    ) -> int | None:
        """The fee our fill is charged, in cc, from the real fee model
        (pricing/fees.py — never reimplemented). $0 for our combo maker quadratic
        maker fill; the real maker fee when the combo's series is on the
        maker-fee list (``combo_ticker``/``collection`` prefix match — omitted ⇒
        the configured fee type, the pre-2026-07-16 behaviour); correct for a
        nonzero-fee series. None when no fee model is wired OR the fee is
        genuinely UNKNOWN (flat/unknown fee_type) — an honest ledger records
        UNKNOWN, never a guessed 0 (defense #2)."""
        if self._fee_model is None:
            return None
        try:
            return int(
                self._fee_model.trade_fee_cc(
                    price_cc=bid,
                    qty=qty,
                    fee_type=self._effective_fee_type(combo_ticker, collection),
                    multiplier=self._fee_multiplier,
                )
            )
        except FeeUnknownError:
            return None

    def _beat(self) -> None:
        """Beat the external-supervisor heartbeat mid-loop (2026-07-16 wedge
        fix). A beat-write failure is logged, never raised — the file going
        stale IS the fail-closed signal; breaking the maintenance tick over it
        would only make the wedge story worse."""
        if self._beat_cb is None:
            return
        try:
            self._beat_cb()
        except Exception:  # noqa: BLE001 — see docstring
            log.warning("heartbeat_beat_failed_midloop", exc_info=True)

    # ---------------------------------------------- fill-record recovery sweep

    async def _sweep_unrecorded_fills(self) -> None:
        """FILL-RECORD RECOVERY SWEEP (2026-07-16 P1, real-money bug).

        ``on_quote_executed`` is the ONLY writer of fills-ledger rows and fires
        only on the exchange's ``quote_executed`` WS message — which has NO
        replay. A missed message therefore left a REAL fill (reservation
        committed at confirm, position live on the exchange) permanently out of
        the fills ledger: invisible to P&L/EV/markouts/settlement-reconcile
        until the next-restart reconcile quarantined it as a quantity mismatch
        (PROVEN 2026-07-16: quote 527b5a3a…, 117.07ct NO @ 80.60c — confirm
        committed at 15:28:02Z, no quote_executed_msg, fill present on GET
        /portfolio/fills).

        For every state whose confirm SUCCEEDED (``fill_confirmed_mono_ns``
        stamped) but whose fills row was never recorded, once
        ``fill_record_recovery_after_s`` has passed: poll REST GET quote (doc:
        openapi-comms.md — status enum open|accepted|confirmed|executed|
        cancelled) and

          - ``executed``  ⇒ synthesize the executed message ({quote_id,
            order_id from the quote payload's creator_order_id if present,
            recovered_via_poll: true}) and run the SAME ``on_quote_executed``
            path — never a parallel ledger implementation; the store-level
            INSERT-if-absent guard makes a WS+poll race single-row safe;
          - ``cancelled`` ⇒ the fill never happened: the existing lapse/cancel
            cleanup (release any straggler reservation, drop the phantom
            position booked at confirm, clear the parked state);
          - still pending / any error / unreadable status ⇒ leave for the next
            tick (bounded per-quote attempts, then a LOUD exhausted metric —
            the restart reconcile stays the backstop). A fill is NEVER
            synthesized from anything but an explicit ``executed`` status
            (fail-closed).

        Rate-bound to ``_FILL_RECOVERY_MAX_POLLS_PER_TICK`` REST polls per
        maintenance tick. No ``quote_getter`` wired (paper/backtests/minimal
        rigs) or a non-positive/NaN delay ⇒ no sweep at all."""
        if self._quote_getter is None:
            return
        after_s = self._config.fill_record_recovery_after_s
        if not (after_s > 0.0):  # non-positive OR NaN config ⇒ sweep disabled
            return
        after_ns = int(after_s * 1e9)
        now = self._clock.monotonic_ns()
        polls = 0
        for quote_id, state in list(self._executed_states.items()):
            if polls >= _FILL_RECOVERY_MAX_POLLS_PER_TICK:
                break
            if state.fill_recorded or state.pending_fill is None:
                continue
            if state.fill_confirmed_mono_ns is None:
                # Confirm never succeeded client-side (unknown-committed): the
                # reservation-reconcile loop owns that path, never this sweep.
                continue
            if now - state.fill_confirmed_mono_ns < after_ns:
                continue  # the WS message may still arrive — too early to poll
            if state.fill_recovery_attempts >= _FILL_RECOVERY_MAX_ATTEMPTS:
                continue  # exhausted — already reported loudly below
            polls += 1
            state.fill_recovery_attempts += 1
            self._beat()  # a REST poll is progress, not a wedge (2026-07-16)
            self._metrics.inc("fill_recovery.swept")
            try:
                # Per-poll bound (review 2026-07-16): the REST client's own
                # 10s total timeout × 3 serial polls turned a black-holed
                # connection into a ~30s maintenance tick — TTL expiry, reprice
                # and limit-halt checks all waited behind it. A timed-out poll
                # is just a failed attempt (bounded-retry, loud exhaustion).
                payload = await asyncio.wait_for(
                    self._quote_getter.get_quote(quote_id), timeout=2.5
                )
            except Exception as exc:  # noqa: BLE001 — any poll error retries next tick
                self._metrics.inc("fill_recovery.errors")
                log.warning(
                    "fill_recovery_poll_failed",
                    quote_id=quote_id,
                    attempt=state.fill_recovery_attempts,
                    error=repr(exc),
                )
                self._note_fill_recovery_exhausted(quote_id, state)
                continue
            quote = payload.get("quote", payload)
            status = (
                str(quote.get("status", "")).lower()
                if isinstance(quote, dict)
                else ""
            )
            if status == "executed":
                msg: JsonDict = {"quote_id": quote_id, "recovered_via_poll": True}
                order_id = (
                    quote.get("creator_order_id") or quote.get("order_id")
                    if isinstance(quote, dict)
                    else None
                )
                if order_id:
                    msg["order_id"] = str(order_id)
                self._metrics.inc("fill_recovery.recovered")
                log.warning(
                    "fill_record_recovered_via_poll",
                    quote_id=quote_id,
                    order_id=msg.get("order_id"),
                    attempts=state.fill_recovery_attempts,
                    detail="quote_executed WS message never arrived; fill "
                    "recorded from the REST quote status via the SAME "
                    "on_quote_executed path",
                )
                await self.on_quote_executed(msg)
            elif status == "cancelled":
                self._metrics.inc("fill_recovery.cancelled")
                self._recover_cancelled_fill(quote_id, state, quote)
            elif status in ("open", "accepted", "confirmed"):
                # Legitimately not executed yet (a stalled execution timer):
                # keep waiting, bounded like an error so a quote stuck here
                # forever cannot consume the poll budget indefinitely.
                self._metrics.inc("fill_recovery.still_pending")
                self._note_fill_recovery_exhausted(quote_id, state)
            else:
                # Missing/unknown status: NEVER assumed executed (fail-closed) —
                # count as an error and retry next tick.
                self._metrics.inc("fill_recovery.errors")
                log.warning(
                    "fill_recovery_unreadable_status",
                    quote_id=quote_id,
                    status=status,
                    attempt=state.fill_recovery_attempts,
                )
                self._note_fill_recovery_exhausted(quote_id, state)

    def _recover_cancelled_fill(
        self, quote_id: str, state: OpenQuoteState, quote: Any
    ) -> None:
        """The exchange CANCELLED a quote we confirmed (a post-confirm void —
        no WS event exists for it, doc: rfq-flow.md): the fill never executed,
        so the position committed at confirm is PHANTOM. Existing lapse/cancel
        cleanup, applied here: release any straggler reservation (idempotent —
        a committed one is no longer outstanding), drop the phantom position
        from the exposure book (the settlement seam's own removal — bumps the
        position generation so stale snapshots invalidate), and un-park the
        state exactly like the decline paths do."""
        cancellation_reason = (
            quote.get("cancellation_reason") if isinstance(quote, dict) else None
        )
        log.warning(
            "fill_recovery_quote_cancelled",
            quote_id=quote_id,
            cancellation_reason=cancellation_reason,
            detail="confirmed quote came back CANCELLED from REST — fill never "
            "executed; phantom position removed, no fills row written",
        )
        if self._reservation is not None:
            self._reservation.release(f"fill:{quote_id}")
        self._exposure.remove_position(f"fill:{quote_id}")
        self._executed_states.pop(quote_id, None)
        state.pending_fill = None
        self._drop_quote(quote_id)

    def _note_fill_recovery_exhausted(
        self, quote_id: str, state: OpenQuoteState
    ) -> None:
        """When a quote's poll budget is spent without a terminal status, say so
        LOUDLY exactly once: the ledger hole persists until the next-restart
        exchange reconcile (the P0-4/P0-5 backstop) — an operator must know."""
        if state.fill_recovery_attempts != _FILL_RECOVERY_MAX_ATTEMPTS:
            return
        self._metrics.inc("fill_recovery.exhausted")
        log.error(
            "fill_recovery_exhausted",
            quote_id=quote_id,
            attempts=state.fill_recovery_attempts,
            detail="recovery poll budget spent without executed/cancelled — the "
            "fills ledger may still be missing this fill; the next-restart "
            "exchange reconcile is the backstop",
        )

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
        # FILL-RECORD RECOVERY SWEEP (2026-07-16 P1): repair a confirmed fill
        # whose quote_executed WS message was lost, BEFORE the limit check so a
        # recovered position counts against the caps this same tick. Runs even
        # when halted — recording exchange truth is reconciliation, not quoting.
        await self._sweep_unrecorded_fills()
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
        # REPRICE SWEEP — WEDGE-HARDENED (2026-07-16, the 18:13Z heartbeat kill).
        # Under a frozen joint pool (abandoned 5-8s cold-tail futures keep every
        # worker busy) this loop used to serially burn one full pool deadline PER
        # open quote (31 × 2.0s = 62s in the killed run) while the heartbeat is
        # beaten only between ticks — the supervisor read the silence as a wedge
        # and emergency-killed a LIVE bot. Three bounded defenses, none of which
        # change what any individual quote decision would have been:
        #   (1) beat the heartbeat per iteration — the loop IS making progress;
        #       a genuine event-loop wedge still cannot beat (fail-closed);
        #   (2) a consecutive pool-deadline CIRCUIT BREAKER: after
        #       _REPRICE_POOL_TRIP consecutive SKIP_PRICE_DEADLINE results the
        #       pool is presumed frozen and the REST of the sweep defers to the
        #       next tick (0.5s away) — un-repriced quotes stay bounded by
        #       last-look freshness at confirm, and the first tripped quotes
        #       still get today's fail-safe deletion;
        #   (3) a wall budget for the whole sweep (_REPRICE_SWEEP_BUDGET_S) as
        #       belt-and-suspenders against many slow-but-not-timeout awaits.
        now = self._clock.monotonic_ns()
        sweep_start_ns = now
        budget_ns = int(_REPRICE_SWEEP_BUDGET_S * 1e9)
        consecutive_pool_deadline = 0
        # ROTATION (review 2026-07-16): resume after the quote the previous
        # tick's early break last handled, so a budget/trip deferral cycles the
        # whole book across ticks instead of re-walking the same front quotes
        # (whose unmoved fair neither replaces nor deletes them) forever. A
        # vanished marker (filled/expired between ticks) restarts from the
        # front; a completed pass clears it.
        items = list(self._open.items())
        if self._reprice_resume_after is not None and items:
            ids = [qid for qid, _ in items]
            try:
                start = ids.index(self._reprice_resume_after) + 1
            except ValueError:
                start = 0
            items = items[start:] + items[:start]
        self._reprice_resume_after = None
        prev_handled: str | None = None
        for quote_id, state in items:
            self._beat()
            if state.accepted:
                prev_handled = quote_id
                continue
            age_s = (now - state.created_mono_ns) / 1e9
            if age_s > self._config.quote_ttl_s:
                await self._delete_quote(quote_id, ReasonCode.DELETE_TTL_EXPIRED)
                # Deleted — prev_handled must only ever name a SURVIVING quote:
                # a marker pointing at a removed id fails next tick's index
                # lookup and silently discards the rotation (verify follow-up
                # 2026-07-16). Deleted quotes aren't in next tick's items, so
                # resuming after the last survivor skips nothing.
                continue
            if self._clock.monotonic_ns() - sweep_start_ns > budget_ns:
                self._metrics.inc("reprice.sweep_budget_deferred")
                log.warning(
                    "reprice_sweep_budget_deferred",
                    detail="reprice sweep exceeded its wall budget — remaining "
                    "quotes defer to the next tick",
                )
                # Current quote was NOT handled — resume AT it next tick.
                self._reprice_resume_after = prev_handled
                break
            result = await self._price_async(state.rfq)
            if isinstance(result, NoQuote):
                await self._delete_quote(quote_id, ReasonCode.DELETE_LEG_STALE)
                if result.reason is ReasonCode.SKIP_PRICE_DEADLINE:
                    consecutive_pool_deadline += 1
                    if consecutive_pool_deadline >= _REPRICE_POOL_TRIP:
                        self._metrics.inc("reprice.pool_trip")
                        log.warning(
                            "reprice_pool_circuit_tripped",
                            consecutive=consecutive_pool_deadline,
                            detail="consecutive pool deadlines — pool presumed "
                            "frozen; remaining reprices defer to the next tick",
                        )
                        # Current quote WAS handled but fail-safe DELETED — the
                        # marker must name a quote that still exists next tick
                        # (verify follow-up 2026-07-16: a dead marker restarts
                        # from the front and the rotation never survives a trip).
                        self._reprice_resume_after = prev_handled
                        break
                else:
                    consecutive_pool_deadline = 0
                # Fail-safe deleted above — not a surviving marker candidate.
                continue
            consecutive_pool_deadline = 0
            if abs(int(result.fair_cc) - int(state.constructed.fair_cc)) > (
                self._config.reprice_threshold_cc
            ):
                self._metrics.inc("quote.reprice")
                await self.handle_rfq(state.rfq)  # replacement quote
                if self._by_rfq.get(state.rfq.rfq_id) == quote_id:
                    # Replacement was refused (filter/risk) — a stale quote
                    # must never stay on the wire.
                    await self._delete_quote(quote_id, ReasonCode.DELETE_LEG_MOVED)
            if quote_id in self._open:
                prev_handled = quote_id

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
            # SKEW MUTEX FIX (2026-07-18): the snapshot's P0-9 directional
            # entries + the exposure book's OWN ME-metadata answer, so a
            # single-ME hedge (ARG-champ vs a short-ESP book — mis-widened
            # 63/63 on the raw delta sum) classifies OFFSETTING. The
            # COMMITTED-only census (verify fix) fails the mutex path closed
            # to the raw read when the committed book carries a leg on a
            # SECOND explicit-ME event of the game (over-rebate corner);
            # resting quotes never drive that fallback.
            dir_entries_by_game=snap.dir_entries_by_game,
            committed_dir_entries_by_game=snap.committed_dir_entries_by_game,
            is_me_event=self._exposure.is_me_event,
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
            mutex_direction_games=list(skew.mutex_direction_games),
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
                # Confirm-path check ⇒ the confirm-time resting haircut applies
                # here exactly as at the authoritative reservation (2026-07-17;
                # un-weighted, this advisory fold breached slate/directional on
                # the standing resting book and short-circuited BEFORE the
                # deferral could ever hand the denial to the waiver).
                apply_resting_haircut=self._config.resting_haircut_at_confirm,
            )
        )
        # LAST-LOOK MC WAIVER deferral (handoff Problem A). This advisory check
        # runs BEFORE the authoritative reservation, on the SAME book minus the
        # outstanding reservations — so with the waiver armed, a decline whose
        # EVERY enforced breach is a waivable game-loss/mutex-directional cap
        # breach must not short-circuit here (it would mask the waiver: the
        # 2026-07-16 live self-declines fired on THIS path). Defer it to the
        # reservation deny-site, whose atomic check is a strict SUPERSET of this
        # one (same candidate + all outstanding reservations): it re-catches the
        # same breaches, triggers the waiver, and on any waiver failure declines
        # DECLINE_RISK_LIMIT exactly as this path would have. Guarded on a wired
        # reservation service — with no service there is no authoritative
        # re-check downstream, so this path keeps declining as today. Disabled
        # waiver ⇒ byte-identical prior behaviour. ANY non-waivable breach still
        # declines right here.
        if (
            breaches
            and self._config.lastlook_mc_waiver_enabled
            and self._reservation is not None
            and all(
                b.reason in WAIVABLE_RESERVATION_BREACHES
                or b.reason is ReasonCode.SKIP_SLATE_CAP
                for b in breaches
            )
        ):
            self._metrics.inc("lastlook_waiver.deferred_to_reservation")
            log.info(
                "lastlook_waiver_deferred",
                quote_id=state.quote_id,
                breaches=[str(b.reason) for b in breaches],
                detail="all-waivable last-look breaches deferred to the atomic "
                "reservation check + MC waiver",
            )
            breaches = []
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
        # LAST-LOOK MC WAIVER observability: every confirm/decline audit line
        # carries the waiver axes (attempted/granted/worst-case/games — honest
        # defaults when no waiver ran), then the per-confirm record is reset.
        waiver = self._waiver_audit
        log.info(
            "risk_audit",
            phase="confirm" if confirm else "decline",
            rfq_id=state.rfq.rfq_id,
            quote_id=state.quote_id,
            reason=str(reason),
            waiver_attempted=waiver is not None,
            waiver_granted=bool(waiver is not None and waiver.get("granted")),
            waiver_worst_case_cc=(
                None if waiver is None else waiver.get("worst_case_cc")
            ),
            waiver_games=None if waiver is None else waiver.get("games"),
            **self._risk_audit_fields(
                candidate_ev_cc=candidate_ev_cc,
                binding_cap="" if confirm else str(reason),
                fallback_reason="",
            ),
        )
        self._waiver_audit = None
