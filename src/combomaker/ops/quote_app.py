"""Paper/quote-mode application: the full hot path, wired.

paper: everything runs — filters, pricing, risk, lifecycle — but the sender is
a dry-run fake, so nothing reaches the exchange. Hypothetical quotes are
persisted for Phase 6 scoring. Conventions may be unverified.

quote: real sender. HARD GATES at startup: conventions must be ground-truth
verified (Phase 2.5) and the prod guard applies. On start, leftover quotes are
cancelled and positions reconciled from REST before anything else; on any exit
path, best-effort cancel-all.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from combomaker.core.clock import Clock, SystemClock
from combomaker.core.conventions import Side, load_conventions
from combomaker.core.money import (
    ONE_DOLLAR,
    CentiCents,
    MoneyParseError,
    exposure_cc_from_dollars_str,
)
from combomaker.core.quantity import CentiContracts, qty_from_fp_str
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.quote_query import (
    BACKOFF_S as RECONCILE_BACKOFF_S,
)
from combomaker.exchange.quote_query import (
    RETRIES as RECONCILE_RETRIES,
)
from combomaker.exchange.quote_query import (
    list_open_quotes,
    open_quote_ids,
    open_quote_tickers,
)
from combomaker.exchange.rest import (
    CREATE_QUOTE_TOKEN_COST,
    DEFAULT_ENDPOINT_TOKEN_COST,
    DEFAULT_REQUEST_TIMEOUT_S,
    DELETE_QUOTE_TOKEN_COST,
    INSUFFICIENT_BALANCE_CODE,
    LOWEST_TIER_LIMITS,
    ApiTierLimits,
    CashGatedError,
    KalshiApiError,
    KalshiRestClient,
    RateLimitedError,
    ReadBudgetExhausted,
    already_gone,
    observe_api_tier,
)
from combomaker.exchange.ws import WsManager
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.metadata import (
    MarketMeta,
    MetadataCache,
    write_persist_payload,
)
from combomaker.marketdata.settled import (
    FETCH_BUDGET_PER_PASS,
    MarketSource,
    SettledMarginalResolver,
)
from combomaker.ops.config import AppConfig, Env, Mode, RiskConfig
from combomaker.ops.fee_schedule import fee_schedule_path, load_observed_fee_schedule
from combomaker.ops.logging import configure_logging, get_logger
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.ops.preflight import (
    PreflightConditions,
    PreflightError,
    evaluate_preflight,
)
from combomaker.ops.pricing_pool import BookRiskPool, JointPool
from combomaker.ops.process_group import cleanup_straggler_workers
from combomaker.ops.relight import (
    HALT_RECEIPT_FILENAME,
    HALT_RECEIPT_SCHEMA_VERSION,
    RUN_ID_ENV,
)
from combomaker.ops.report import build_report, format_report
from combomaker.ops.supervisor import (
    ENV_SUPERVISOR_API_KEY_ID,
    ENV_SUPERVISOR_PRIVATE_KEY_PATH,
    ENV_SUPERVISOR_PRIVATE_KEY_PEM,
    supervisor_credential_configured,
    supervisor_heartbeat_path,
    supervisor_heartbeat_reachable,
)
from combomaker.ops.write_budget import TokenBudget, WriteBudget
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeType
from combomaker.pricing.grouping import game_key
from combomaker.pricing.tripwire import taxonomy_impossible
from combomaker.rfq.eviction_value import derive_open_quote_capacity
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.intake import RfqIntake
from combomaker.rfq.lifecycle import (
    EXCHANGE_CONFIRM_WINDOW_S,
    LifecycleConfig,
    QuoteLifecycle,
)
from combomaker.rfq.models import Rfq, RfqLeg
from combomaker.rfq.schedule import ScheduleCache
from combomaker.risk.balance import BalanceTracker, StaleBalanceError
from combomaker.risk.breakers import BreakerInputs, CircuitBreakers, RateLimitWindow
from combomaker.risk.confirm_expired_rate import (
    expired_tape_path,
    refresh_expired_baseline,
)
from combomaker.risk.derived_cap_engine import DerivedCapEngine
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.heartbeat import Heartbeat, ReconcileMarker, _atomic_write
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.killswitch import HaltEvent, KillSwitch
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, StarvationWatchdog
from combomaker.risk.progress import ProgressLedger, progress_path
from combomaker.risk.quarantine import MarketQuarantine
from combomaker.risk.reservation import (
    ExchangePosition,
    RiskReservationService,
    open_combo_positions_from_positions,
    open_combo_tickers_from_positions,
    reservation_ids_backed_by_exchange,
)
from combomaker.risk.settlement import SettlementHandler, SettlementPoller
from combomaker.risk.skew import SkewLimits, SkewParams, WidenPolicyParams
from combomaker.risk.stall_wall import (
    StallWallDerivation,
    gap_tape_path,
    oldest_live_log_mtime,
    refresh_stall_wall,
)
from combomaker.sim.book_model import WithinGameRhoProvider
from combomaker.sim.structural_book import StructuralConfigView
from combomaker.sim.within_game_rho import sgp_within_game_rho_provider

log = get_logger(__name__)

JsonDict = dict[str, Any]

# The balance poll cadence must keep the risk bankroll fresh for the %-of-bankroll
# caps: staleness beyond this ⇒ risk_bankroll_cc_or_none() returns None and the
# caps fail closed (SKIP_BANKROLL_UNAVAILABLE). Poll interval is well inside it.
BALANCE_STALE_AFTER_S = 30.0
BALANCE_POLL_INTERVAL_S = 10.0
# Correlation-adaptive cap refresh cadence (North Star). The derived deploy/halt
# caps only change when the measured vol/rho or the projected-book MC change, so a
# slow loop is enough — it re-derives at startup (first tick) then every interval.
# Nightly slate-boundary detection is a fast-follow; a 30-min tick is safe because
# the bootstrap caps are constant within a night and the measured regime updates as
# the P&L sensor accumulates whole nights.
ADAPTIVE_CAPS_REFRESH_S = 1800.0
# Settlement poll cadence: combos settle at game end, so a slow poll is fine — the
# handler is idempotent per position, so a re-poll never double-books. Kept modest
# so realized P&L lands promptly for the enforced daily-loss cap.
SETTLEMENT_POLL_INTERVAL_S = 30.0
# External-transfer watch (2026-07-21): deposits/withdrawals are rare human
# events, but a LAGGING withdrawal transiently reads as a give-back in P&L
# space until detected (the K-ledger corrects it at detection) — 60s bounds
# that window well under the operator's reaction time while costing two GETs
# a minute. The startup delay lets the first balance poll land so the
# account_standing line reports real figures instead of None.
TRANSFER_WATCH_INTERVAL_S = 60.0
TRANSFER_WATCH_STARTUP_DELAY_S = 15.0

# Doc-verified 2026-07-21 (get-deposits.md / get-withdrawals.md): BOTH enums
# are pending|applied|failed|returned; "applied" is the money-moved status
# (deposit: "funds are reflected in balance"; withdrawal: "funds have been
# deducted from balance"); finalized_ts (unix ms, nullable) stamps the
# terminal transition. Never guess a Kalshi enum.
_TRANSFER_APPLIED = "applied"
_TRANSFER_RETURNED = "returned"


def new_external_transfer_deltas(
    statuses: dict[str, str],
    deposits: list[dict[str, Any]],
    withdrawals: list[dict[str, Any]],
    *,
    baseline_before_ms: int | None = None,
) -> list[tuple[str, str, int, int]]:
    """``(kind, ref, delta_cc, finalized_wall_ms)`` for every transfer whose
    STATUS TRANSITION moved money since the last pass. ``statuses`` (mutated)
    tracks each transfer's last-seen status so:

    - a transition INTO ``applied`` applies its delta exactly once (deposit:
      +net(amount − fee); withdrawal: −(amount + fee) — the balance moves by
      those, int cents on the wire ×100 → cc);
    - a later ``applied`` → ``returned`` regression (ACH clawback / bounced
      withdrawal) applies the REVERSING delta (review F5 — a one-way seen-set
      would leave the anchors permanently shifted by a clawed-back deposit);
    - pending/failed rows only record status, so pending→applied IS picked up.

    ``baseline_before_ms`` (first pass only): a transition whose
    ``finalized_ts`` is at/before this instant is already inside the balance
    the anchors formed on — status is recorded, NO delta (review F6: "terminal
    at first pass" is the wrong criterion; ordering vs the anchor instant is
    the right one). A row missing both id or a readable amount is skipped
    loudly (never guess money); a missing ``finalized_ts`` falls back to
    ``created_ts`` and then to 0 (treated as ancient ⇒ baselined / peak-safe
    direction)."""
    out: list[tuple[str, str, int, int]] = []
    for kind, rows in (("deposit", deposits), ("withdrawal", withdrawals)):
        prefix = "dep" if kind == "deposit" else "wd"
        for row in rows:
            row_id = row.get("id")
            if not row_id:
                log.warning("transfer_row_missing_id", kind=kind)
                continue
            key = f"{prefix}:{row_id}"
            status = str(row.get("status"))
            prev = statuses.get(key)
            statuses[key] = status
            amount = row.get("amount_cents")
            if not isinstance(amount, int) or isinstance(amount, bool):
                if status == _TRANSFER_APPLIED and prev != _TRANSFER_APPLIED:
                    log.warning(
                        "transfer_row_unreadable_amount", kind=kind, ref=key
                    )
                    statuses.pop(key, None)  # a later readable row still applies
                continue
            fee = row.get("fee_cents")
            fee_c = fee if isinstance(fee, int) and not isinstance(fee, bool) else 0
            # Balance delta of the money-moved event, signed.
            moved_cc = (
                (amount - fee_c) * 100 if kind == "deposit" else -(amount + fee_c) * 100
            )
            finalized = row.get("finalized_ts") or row.get("created_ts")
            finalized_ms = (
                finalized
                if isinstance(finalized, int) and not isinstance(finalized, bool)
                else 0
            )
            if status == _TRANSFER_APPLIED and prev != _TRANSFER_APPLIED:
                if (
                    baseline_before_ms is not None
                    and finalized_ms <= baseline_before_ms
                ):
                    continue  # already inside the anchored readings — baseline
                out.append((kind, key, moved_cc, finalized_ms))
            elif status == _TRANSFER_RETURNED and prev == _TRANSFER_APPLIED:
                # Clawback: reverse the applied delta (money moved back).
                out.append((f"{kind}-returned", key, -moved_cc, finalized_ms))
    return out


async def _page_portfolio(
    method: Callable[..., Awaitable[dict[str, Any]]], key: str, max_pages: int = 25
) -> list[dict[str, Any]]:
    """Page a cursor-paginated portfolio GET to exhaustion (bounded)."""
    rows: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(max_pages):
        params: dict[str, str | int] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = await method(**params)
        rows.extend(payload.get(key) or [])
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    return rows


def _int_or_none(value: CentiCents | int | None) -> int | None:
    return None if value is None else int(value)  # CentiCents → plain int for logs
# Reservation-vs-exchange reconcile cadence: resolves a confirm-timeout
# mark_unconfirmed reservation against the exchange's real open positions before it
# leaks headroom until restart. Only touches the network when a reservation is
# outstanding.
RESERVATION_RECONCILE_INTERVAL_S = 15.0

# ── LIVENESS vs PROGRESS (2026-07-26 false-kill rebuild) ────────────────────
# The two loops the external supervisor judges, and their OWN cadences. The
# stall bound for each is DERIVED (``supervisor.heartbeat_timeout_s`` + the
# cadence below) inside ProgressLedger.register — nothing here is a threshold,
# only the cadence each loop already runs at.
MAINTENANCE_TICK_INTERVAL_S = 0.5
STATUS_TICK_INTERVAL_S = 15.0
LOOP_MAINTENANCE = "maintenance"
LOOP_QUOTE = "quote"
LOOP_STATUS = "status"

# Concurrent RFQ workers (see the block at the async-worker launch in run() for
# the measured reasoning). Module-level because the read-budget wait bound below
# is derived from it.
RFQ_WORKERS = 8
# Quote-freshness horizon: price RFQs up to this old (see the measured
# derivation at the rfq_work queue in run() — combo RFQs live ~11s median, the
# old 0.4s skipped winnable fresh RFQs during bursts). Module-level since
# 2026-08-01: the SAME horizon now also feeds the intake's WIRE-AGE pre-parse
# gate (frames older than this in the WS dispatch backlog are dead on arrival
# — see RfqIntake's stale_horizon_s derivation), so the two freshness gates
# cannot drift apart.
RFQ_MAX_QUEUE_DWELL_S = 1.5
# How many times a WAITING (slow-path) metadata read re-waits for the read
# bucket before giving the leg up for this pass. Not a duration — the wait
# itself is the bucket's own ``seconds_until`` — just a bound on losing the
# token race to a concurrent reader, so it is exactly the number of concurrent
# readers that could take the token from us.
_READ_BUDGET_WAIT_ATTEMPTS = RFQ_WORKERS

# Off-loop joint pricing (Phase 1 — the wedge guarantee). Cold combo pricing runs
# in worker PROCESSES (escaping the GIL) with a hard per-call deadline so a
# multi-second cold combo can never stall the event loop / heartbeat / WS pongs
# (the 04:20 UTC 2026-07-14 supervisor kill). Warm memo hits stay inline. 8
# workers (2026-07-14 throughput fix): the prod host has 16 cores and the joint
# MVN is the bottleneck under RFQ bursts — at 2 workers, skip_price_deadline +
# skip_rfq_closed caused minute-long quote STOPS (verified: zero-quote minutes
# lined up with rfq_closed spikes of 400-560/min). 8 pricing processes = 4x cold
# throughput, in SEPARATE processes so they add ZERO event-loop pressure; the
# deadline is set safely above the post-fix cold-combo cost so only a
# pathological tail is dropped.
POOL_WORKERS = 8
# 0.8→2.0 (2026-07-14): after the WAL persistence fix killed the rfq_closed stall,
# skip_price_deadline became the top skip (~150/min) — the slow-combo tail hit the
# 0.8s cutoff and we threw away winnable quotes. rfq_closed is now ~0 and combo
# RFQs live ~11s, so there is ample latency headroom; 2.0s lets that tail finish
# pricing and post (a worker AWAITS the pool, so the longer deadline adds no
# event-loop pressure).
POOL_DEADLINE_S = 2.0

# STARTUP FIRST SNAPSHOT deadline (2026-07-16 warmup fix): the bounded wall
# budget for the ONE synchronous book-risk snapshot computed after rehydration
# and before quoting opens. Generous vs the MC's normal runtime (worker spawn +
# numpy import on a cold pool is the tail); on timeout startup proceeds exactly
# as today (warmup declines until the maintenance loop publishes the first
# snapshot — never block startup on risk observability).
STARTUP_BOOK_RISK_DEADLINE_S = 5.0

# Quote resting TTL (2026-07-14, RFQ-lifecycle research). Kalshi RFQs have no fixed
# exchange TTL — our quote rests, swipeable at its posted price, until the RFQ
# closes or we pull it, with NO server-side book-move auto-void. Live tape: median
# combo RFQ lives ~11s, p90 ~24s, only 3.3% past 30s. The old 30s default left
# quotes resting ~3x the median RFQ life on moved books (stale-book exposure) for
# almost no late-swipe upside. 20s ≈ the RFQ p90: catches ~97% of realistic swipes,
# cuts stale exposure, frees capacity to price more. Re-validate once we have fills.
QUOTE_TTL_S = 20.0


def build_lifecycle_config(
    risk_cfg: RiskConfig,
    *,
    peak_topk_states: int = 5,
    peak_n_clusters: int = 3,
    peak_cluster_min_frac: str = "0.30",
) -> LifecycleConfig:
    """The ONE place YAML risk knobs become the live ``LifecycleConfig`` —
    extracted pure (the ``supervisor_launch_cmd`` precedent) so tests can prove
    every operator knob actually REACHES the lifecycle (a YAML field that stops
    here is a dead knob; the 2026-07-15 heartbeat_timeout_s lesson).

    - P0-1: candidate-aware portfolio-risk gate at confirm (ENFORCED by
      default; YAML ``risk.candidate_gate_enabled: false`` is the kill switch).
      The gate reads the SAME %-of-bankroll / ruin budgets from RiskLimits the
      analytic caps use — it only ADDS the joint-tail credit/charge, never
      loosens a cap.
    - P0-2 (game-day wiring 2026-07-16): ``candidate_gate_deadline_s`` — the
      gate's wall budget of the 3s confirm window, now YAML-settable so the
      operator can rebalance it against the waiver (their joint fit is
      validated by RiskConfig).
    - P1 EV VISIBILITY: the OPTIONAL worst-challenger-EV tolerance. −inf by
      default (the gate stays production-model-EV only, no behaviour change); a
      finite operator value ALSO declines a +production-EV candidate whose
      worst credible challenger EV falls below it (strictly additive).
    - LAST-LOOK MC WAIVER (handoff Problem A — CONFIRM-PATH ONLY): committed
      default OFF; the operator arms it in the local YAML.
    """
    return LifecycleConfig(
        quote_ttl_s=QUOTE_TTL_S,
        candidate_gate_enabled=risk_cfg.candidate_gate_enabled,
        candidate_gate_deadline_s=risk_cfg.candidate_gate_deadline_s,
        # CONFIRM-WINDOW REBUILD (2026-07-27). Both default ON — they ARE the
        # fix for the 70 won auctions discarded on our own stopwatch — and are
        # exposed here only so the operator has a real rollback lever without an
        # edit. ``candidate_gate_deadline_s`` above is now only the fallback
        # budget used when the derived deadline is switched OFF.
        candidate_gate_derived_deadline=risk_cfg.candidate_gate_derived_deadline,
        candidate_gate_timeout_fallback=risk_cfg.candidate_gate_timeout_fallback,
        worst_challenger_ev_tolerance_cc=risk_cfg.worst_challenger_ev_tolerance_cc,
        lastlook_mc_waiver_enabled=risk_cfg.lastlook_mc_waiver_enabled,
        lastlook_mc_waiver_deadline_s=risk_cfg.lastlook_mc_waiver_deadline_s,
        # SLATE-AXIS WAIVER (2026-07-25): slate-ONLY denials certify the top
        # analytic contributors — armed in the local YAML with operator go.
        lastlook_waiver_slate_axis=risk_cfg.lastlook_waiver_slate_axis,
        # WAIVER ENTITY-SET TRIM (2026-07-18): K largest resting quotes per
        # breached game inside the waiver enumeration; dropped tail rides as a
        # constant conservative adder. 0 (default) = full-set enumeration.
        lastlook_waiver_topk_resting=risk_cfg.lastlook_waiver_topk_resting,
        # CERTIFIED-HEDGE EV BUDGET (2026-07-18): the candidate gate's verified
        # negative-EV hedge exception. Default disabled / 0 = today.
        allow_negative_ev_hedge=risk_cfg.allow_negative_ev_hedge,
        hedge_cost_budget_cc=risk_cfg.hedge_cost_budget_cc,
        # B2 (2026-07-25): derived certified-hedge budget = tail reduction.
        hedge_budget_tail_derived=risk_cfg.hedge_budget_tail_derived,
        # TAIL-PROBABILITY BOOK GATE (2026-07-25 operator anchor): the
        # candidate gate binds P(KILL-distance night) when armed; the SAME
        # flag drives the quote-time cap via RiskLimits (one YAML key).
        tail_prob_gate=risk_cfg.portfolio_tail_prob_gate,
        kill_tail_prob=float(
            Fraction(Decimal(risk_cfg.portfolio_kill_tail_prob))
        ),
        # RENEGE FIXES (2026-07-25 big-fill audit): award-true candidate
        # sizing + pricing-fair admission EV + game-scoped waiver stability.
        # All default OFF.
        risk_qty_award_sizing=risk_cfg.risk_qty_award_sizing,
        gate_ev_from_pricing_fair=risk_cfg.gate_ev_from_pricing_fair,
        waiver_game_scoped_stability=risk_cfg.waiver_game_scoped_stability,
        release_accepted_quote_exposure=risk_cfg.release_accepted_quote_exposure,
        require_p_book_non_decreasing=risk_cfg.require_p_book_non_decreasing,
        open_quote_ev_eviction=risk_cfg.open_quote_ev_eviction,
        # FIX 4 (2026-07-28): value-ranked allocation of the fixed det-max
        # budget, riding the SAME eviction mechanism. Default shadow.
        det_budget_value_ranking=risk_cfg.det_budget_value_ranking,
        # DIVERSITY-AWARE EVICTION KEY (2026-07-31 operator ruling): the slot
        # axis ranks on dEV x P(accept|size, measured) - dES99. Default off.
        eviction_diversity_key=risk_cfg.eviction_diversity_key,
        # DEPLOYMENT SCALE (operator LEVER #1): solved off the maintenance tick
        # from the live envelope; consumed by the deploy-side budgets only.
        # Default OFF ⇒ never solved, never consumed (byte-identical).
        deploy_scale_enabled=risk_cfg.deploy_scale_enabled,
        deploy_scale_s_max=risk_cfg.deploy_scale_s_max,
        deploy_scale_grid_points=risk_cfg.deploy_scale_grid_points,
        deploy_scale_refresh_s=risk_cfg.deploy_scale_refresh_s,
        deploy_scale_mc_samples=risk_cfg.deploy_scale_mc_samples,
        # FILL-RECORD RECOVERY SWEEP (2026-07-16 P1): poll REST for a confirmed
        # fill whose quote_executed WS message never arrived.
        fill_record_recovery_after_s=risk_cfg.fill_record_recovery_after_s,
        # CANCEL-REPORT VERIFY-BEFORE-DISCARD (2026-07-18 incidents): bounded
        # /portfolio/fills polls before a CANCELLED-status confirmed quote's
        # position may be discarded (both incidents were REAL taker-style
        # executions behind a "cancelled" quote status).
        fill_cancel_verify_attempts=risk_cfg.fill_cancel_verify_attempts,
        fill_cancel_verify_delay_s=risk_cfg.fill_cancel_verify_delay_s,
        # FILLS-LEDGER SWEEP (2026-07-24 incident C): account-wide
        # /portfolio/fills vs local-ledger diff cadence + first-fetch lookback
        # (alarm-only backstop under every writer-path miss).
        fills_ledger_sweep_interval_s=risk_cfg.fills_ledger_sweep_interval_s,
        fills_ledger_sweep_lookback_s=risk_cfg.fills_ledger_sweep_lookback_s,
        # POSITION-LEDGER DIVERGENCE INVARIANT (2026-07-26): cadence of the
        # open-positions vs open-ledger-rows count (alarm-only observability).
        ledger_divergence_sweep_interval_s=(
            risk_cfg.ledger_divergence_sweep_interval_s
        ),
        # F1 MONOTONE PRE-PRICING GATE (2026-07-16 throughput batch-1): decline
        # on already-breached candidate-monotone caps BEFORE pricing. Default
        # OFF (today's behaviour); the operator arms it in the local YAML.
        pre_pricing_gate_enabled=risk_cfg.pre_pricing_gate_enabled,
        # CONFIRM-TIME resting haircut (2026-07-17): the reservation check
        # weights ONLY the resting fold; the serial commit chain stays 100%.
        resting_haircut_at_confirm=risk_cfg.resting_haircut_at_confirm,
        # FIX 5 (2026-07-28): arm the book-growth DECAY of a generation-stale
        # book-risk snapshot rather than discarding it. Default SHADOW.
        book_risk_stale_decay=risk_cfg.book_risk_stale_decay,
        # PEAK-CONCENTRATION steer (2026-07-18): K cached worst scorelines per
        # game for the off-hot-path committed-book peak profile (a PRICING
        # input to the skew seam — sim/peak_profile.py). Sourced from
        # ``pricing.skew.peak_topk_states`` (a keyword here because this
        # builder's positional contract is RiskConfig-only).
        peak_topk_states=peak_topk_states,
        # MULTI-CLUSTER steer (2026-07-19): distinct loss clusters cached per
        # game + the qualifying threshold as a fraction of the top loss.
        # Sourced from ``pricing.skew.peak_n_clusters`` /
        # ``peak_cluster_min_frac``; 1 = the single-plateau behaviour.
        peak_n_clusters=peak_n_clusters,
        peak_cluster_min_frac=peak_cluster_min_frac,
    )


class _PacedMarketSource:
    """A ``MarketSource`` that WAITS for read tokens before each GET.

    The settled-marginal resolver is the second-largest read source in the bot
    (123 of the 2026-07-26 incident's 429s were ``settled_fetch_failed`` HTTP
    429). It runs off-loop, bounded to a few fetches per pass, and its result
    must eventually land — so it pays the bucket's own refill wait instead of
    being refused. Sharing ONE bucket with the metadata cache is the point: two
    independent budgets against one exchange bucket is not a budget."""

    def __init__(
        self, inner: MarketSource, reserve: Callable[[], Awaitable[None]]
    ) -> None:
        self._inner = inner
        self._reserve = reserve

    async def get_market(self, ticker: str) -> dict[str, Any]:
        await self._reserve()  # waits AND spends
        return await self._inner.get_market(ticker)


def build_settled_resolver(
    risk_cfg: RiskConfig,
    source: MarketSource,
    clock: Clock,
    metrics: Metrics | None = None,
) -> SettledMarginalResolver | None:
    """SETTLED-LEG MARGINAL RESOLUTION wiring (2026-07-18 live outage) — the
    ONE place the YAML knob decides whether a resolver exists, extracted pure
    (the ``build_lifecycle_config`` precedent) so a test can prove the knob
    actually reaches the lifecycle. ``risk.settled_marginal_resolution: false``
    ⇒ None ⇒ the lifecycle behaves exactly as before the fix (a settled leg's
    missing marginal leaves the book-risk snapshot unusable, fail-closed)."""
    if not risk_cfg.settled_marginal_resolution:
        return None
    return SettledMarginalResolver(
        source,
        clock,
        retry_after_s=risk_cfg.settled_resolution_retry_s,
        metrics=metrics,
    )


async def handle_rfq_record_after(
    rfq: Rfq,
    *,
    handle: Callable[[Rfq], Awaitable[None]],
    record: Callable[[Rfq], Awaitable[None]],
) -> None:
    """RECORD-AFTER-PRICE FAST-LANE (throughput synthesis 2026-07-16, B6).

    Run the pricing path FIRST, then ALWAYS record the RFQ tape row — the
    ``record_rfq`` write (a ``json.dumps(rfq.raw)`` serialize + writer-queue
    put) used to sit BEFORE pricing on the wire→POST critical path, where the
    exchange's ~0.67s quote window makes every pre-POST millisecond count. The
    tape is observability, not a quoting input, so it moves AFTER
    pricing/dispatch.

    Exactly-once guarantee: the ``finally`` records every RFQ that entered the
    pipeline — priced, skipped, non-combo, or RAISED (the exception still
    propagates to the worker's error path afterwards). Extracted module-level
    (the ``build_lifecycle_config`` testability precedent) so the invariant is
    pinned by unit tests rather than living only inside ``run()``'s closure.
    """
    try:
        await handle(rfq)
    finally:
        await record(rfq)


class AcceptPriorityGate:
    """CONFIRM PREEMPTS QUOTING (2026-07-31, the double confirm-expiry halt).

    Both 2026-07-31 kill-switch halts were HALT_CONFIRM_TIMEOUTS: won auctions
    whose confirm hit the exchange's 3.0s window ('expired'). Measured over
    EVERY confirm ever taped (1,038 accepts, 12 failures — all 'expired'): the
    accept-handler's own time was <= 1.14s on every failure, so >= 1.86s of the
    window died in queue/loop contention with the reprice storm (~500 comms
    frames/s, 8 pricing workers). The WS priority lane (WsManager.mark_priority)
    removes the dispatch-backlog wait; THIS gate removes the loop-contention
    wait: while an accepted quote's confirm is in flight, no NEW quote work
    starts (intake drops rfq_created pre-parse; rfq workers + the retry loop
    park), so the confirm chain gets the loop to itself.

    NOTHING HAND-SET — both bounds derive from the venue + the code:
      * hold bound = EXCHANGE_CONFIRM_WINDOW_S (a protocol fact, rfq/lifecycle):
        past that window the exchange has already voided the confirm, so a
        longer hold protects nothing and only costs quoting. Each new accept
        re-anchors the bound (it owns a fresh window). This is a FAIL-SAFE
        bound only — the normal release is accept_done() in the worker's
        ``finally``, which runs on every path (on_quote_accepted catches its
        own exceptions and re-raises through the worker's logger).
      * release = pending-count reaching zero (pure bookkeeping, no number).

    Named cost (operator priority 2026-07-31: a banked win beats any number of
    reprices): quoting pauses for the confirm-handling time, measured median
    0.53s / max 1.14s per accept, at tens of accepts per day — worst case
    ~30-60s of paused quoting per day.
    """

    def __init__(self, clock: Clock, max_hold_s: float) -> None:
        self._clock = clock
        self._max_hold_s = max_hold_s
        self._pending = 0
        self._deadline_mono_ns = 0
        self._clear = asyncio.Event()
        self._clear.set()

    def accept_enqueued(self) -> None:
        """An accept entered the pipeline — quoting yields NOW."""
        self._pending += 1
        self._deadline_mono_ns = self._clock.monotonic_ns() + int(
            self._max_hold_s * 1e9
        )
        self._clear.clear()

    def accept_done(self) -> None:
        """The accept's confirm handling finished (any outcome)."""
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._clear.set()

    def holding(self) -> bool:
        """True while quote work must yield to an in-flight confirm."""
        if self._clear.is_set():
            return False
        if self._clock.monotonic_ns() >= self._deadline_mono_ns:
            return False  # fail-safe: past the exchange window, holding is moot
        return True

    async def wait_clear(self) -> None:
        """Park until the confirm resolves (or its exchange window lapses)."""
        if not self.holding():
            return
        remaining_s = (self._deadline_mono_ns - self._clock.monotonic_ns()) / 1e9
        try:
            await asyncio.wait_for(self._clear.wait(), timeout=max(0.0, remaining_s))
        except TimeoutError:
            return  # the fail-safe bound above — resume quoting


def supervisor_launch_cmd(config: AppConfig) -> list[str]:
    """Argv for the safety-supervisor subprocess.

    Must forward the bot's OWN config file (``--config``): the supervisor
    re-loads config in its own process, and before this it always fell back to
    the base per-env YAML — so any supervisor override living only in a local
    launch config (e.g. ``supervisor.heartbeat_timeout_s: 30`` in the armed
    ``*.local.yaml``) applied to the bot but silently NOT to the watchdog that
    enforces it (the 2026-07-15 15s heartbeat kills, handoff Problem B)."""
    cmd = [
        sys.executable,
        "-m",
        "combomaker.ops.supervisor",
        "--env",
        str(config.env),
    ]
    if config.source_path is not None:
        cmd += ["--config", str(config.source_path)]
    return cmd


# HARD-class halts: an in-process trip on any of these means our local book /
# money model is provably wrong or under stress, so a restart MUST reconcile
# against the exchange before quoting again — we drop the needs_reconcile marker
# (block-restart-until-reconciled). Give-back KILLs (drawdown / hard-trip),
# fill-velocity, the reconcile mismatch, and EVERY circuit breaker (fail-closed
# detectors — a book that tripped one is a book to re-prove). SOFT/manual halts
# (HALT_MANUAL, HALT_KILL_FILE, HALT_SUPERVISOR, HALT_EXCHANGE_STATUS,
# HALT_DAILY_LOSS soft-cap, WS/clock/error-rate/confirm-timeout) are a deliberate
# or transient stop and do NOT force a reconcile on the next start.
_HARD_HALT_REASONS: frozenset[ReasonCode] = frozenset(
    {
        ReasonCode.HALT_HARD_TRIP,
        ReasonCode.HALT_RECONCILIATION_MISMATCH,
        ReasonCode.HALT_FILL_VELOCITY,
        ReasonCode.HALT_DRAWDOWN,
        # Circuit breakers (risk/breakers.py): fail-closed known-failure signatures.
        ReasonCode.HALT_DATA_STALE,
        ReasonCode.HALT_LATENCY_SPIKE,
        ReasonCode.HALT_RATE_LIMIT_BURST,
        ReasonCode.HALT_MARGINAL_JUMP,
        ReasonCode.HALT_METADATA_CHANGE,
        ReasonCode.HALT_UNMAPPED_GAME,
        ReasonCode.HALT_BREAKER_ERROR,
    }
)


# --------------------------------------------------------------------------- #
# Metadata-change breaker: the field partition (2026-07-26 rebuild).
# Everything here is derived from measured exchange behaviour — see
# QuoteApp._settlement_fingerprint for the per-field evidence.
# --------------------------------------------------------------------------- #

# Raw-payload keys that are terms in the PAYOFF FUNCTION. A move in any of them
# is a settlement change: whole-bot halt + needs_reconcile.
_SETTLEMENT_PAYOUT_FIELDS: tuple[str, ...] = (
    "rules_primary",
    "rules_secondary",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "custom_strike",
    "expiration_time",
    "latest_expiration_time",
    "market_type",
    "notional_value_dollars",
)

# Sentinel for a key that is ABSENT from the payload, distinct from any value it
# could hold (including None/"" ), so a rule or strike VANISHING is a change.
_ABSENT = "\x00absent"


def _canonical(value: object) -> str:
    """Order-stable, type-stable rendering of a raw payload value. ``sort_keys``
    means a dict (``custom_strike``) whose key order changes is NOT a change,
    while any value change is. The type tag keeps 5.5 (float) distinct from
    "5.5" (str) — a silent type flip on a strike is a real change."""
    if value is _ABSENT:
        return "\x00absent"
    try:
        return f"{type(value).__name__}:{json.dumps(value, sort_keys=True)}"
    except (TypeError, ValueError):  # pragma: no cover - non-JSON payload value
        return f"{type(value).__name__}:{value!r}"


# Kalshi market statuses we MODEL as pure trading-state lifecycle. REST enum is
# doc-verified (docs/api-notes/index-scan.md:106,149): initialized | inactive |
# active | closed | determined | disputed | amended | finalized. `deactivated` /
# `activated` are the explicit pause/unpause EVENTS that move active<->inactive;
# active/inactive -> closed happens implicitly at close_time.
_LIFECYCLE_STATUSES: frozenset[str] = frozenset(
    {
        "initialized",
        "inactive",
        "active",
        "closed",
        "determined",
        "finalized",
        # "settled" is a WEBSOCKET event name; REST never returns it (the
        # pre-rebuild terminal tuple carried it as dead code). Kept modelled on
        # purpose: if a WS-sourced status string ever reaches this baseline it
        # means "settled", which is lifecycle — it must not read as an
        # unmodelled value and hard-halt.
        "settled",
    }
)

# Statuses whose ARRIVAL is settlement-relevant: the graded result is being
# contested (`disputed`) or changed (`amended`). Never exempted, at any horizon.
_SETTLEMENT_STATUSES: frozenset[str] = frozenset({"disputed", "amended"})

# The only status in which the exchange accepts orders on a market. Used for the
# STRUCTURAL quarantine release (no timers): a paused market that never comes
# back never leaves quarantine.
_TRADABLE_STATUSES: frozenset[str] = frozenset({"active"})


def _is_tradable_status(status: str) -> bool:
    return status in _TRADABLE_STATUSES


def _status_change_class(prior: str, current: str) -> str:
    """Classify a status TRANSITION, not the field.

    ``"none"`` — unchanged.
    ``"lifecycle"`` — both ends are modelled trading-state values (pause,
    unpause, close, determine, finalize). Scopes to a quarantine.
    ``"settlement"`` — the new status is ``disputed``/``amended`` (the grade is
    contested or changed), or is a string the REST enum does not contain
    (fail-closed: a status we do not model is one we cannot call benign).
    """
    if prior == current:
        return "none"
    if current in _SETTLEMENT_STATUSES:
        return "settlement"
    if current in _LIFECYCLE_STATUSES:
        return "lifecycle"
    return "settlement"


@dataclass(frozen=True, slots=True)
class ShutdownStage:
    """One post-cancel teardown stage, named so a hang can name itself.

    ``run`` is a zero-arg callable returning either an awaitable or ``None``
    (the two process-pool shutdowns are synchronous by construction).
    ``best_effort`` mirrors the pre-existing per-stage exception policy exactly:
    the diagnostic-sweep drain and the supervisor teardown log-and-continue;
    every other stage propagates, as it always did.
    """

    name: str
    run: Callable[[], Awaitable[None] | None]
    best_effort: bool = False


@dataclass(frozen=True, slots=True)
class _MetaBaseline:
    """Last sampled metadata state for one market — the metadata breaker's
    per-ticker baseline. Two fingerprints (payoff vs trading-state), the raw
    status and result the transition rules need, and the end-of-life horizon
    kept for observability on the lifecycle lane."""

    settlement_fp: str
    lifecycle_fp: str
    status: str
    result: str
    horizon: datetime | None


class PaperSender:
    """Dry-run QuoteSender: fabricates ids, logs, sends nothing."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    async def create_quote(
        self,
        rfq_id: str,
        *,
        yes_bid_cc: CentiCents,
        no_bid_cc: CentiCents,
        rest_remainder: bool = False,
    ) -> JsonDict:
        quote_id = f"paper-{next(self._ids)}"
        log.info(
            "paper_quote",
            rfq_id=rfq_id,
            quote_id=quote_id,
            yes_bid_cc=int(yes_bid_cc),
            no_bid_cc=int(no_bid_cc),
        )
        return {"id": quote_id}

    async def delete_quote(self, quote_id: str) -> JsonDict:
        return {}

    async def confirm_quote(self, quote_id: str) -> JsonDict:
        raise RuntimeError("paper quotes cannot be accepted — confirm is unreachable")


class RateLimitRecordingSender:
    """A thin ``QuoteSender`` decorator that records a 429 into the rate-limit
    burst window on EVERY write endpoint the lifecycle drives — create, delete,
    confirm — then re-raises unchanged.

    Why: the 429-burst circuit breaker only saw the balance / exchange-status /
    settlement / reservation POLL 429s (recorded straight in those loops). A
    real rate-limit storm shows up FIRST on the write path (create/confirm), so
    counting only the polls under-counts the burst and the breaker fires late.
    Wrapping the sender (rather than the REST client or the lifecycle) keeps
    both of those modules PRISTINE (hard rule 8): the lifecycle's control flow
    is untouched — the 429 still propagates exactly as before (create/confirm
    already treat it as a failure), we only tap it on the way past. Paper mode
    is never wrapped (a PaperSender never 429s)."""

    def __init__(self, inner: object, rate_limit_window: RateLimitWindow) -> None:
        self._inner = inner
        self._window = rate_limit_window

    async def create_quote(
        self,
        rfq_id: str,
        *,
        yes_bid_cc: CentiCents,
        no_bid_cc: CentiCents,
        rest_remainder: bool = False,
    ) -> JsonDict:
        try:
            return await self._inner.create_quote(  # type: ignore[attr-defined,no-any-return]
                rfq_id,
                yes_bid_cc=yes_bid_cc,
                no_bid_cc=no_bid_cc,
                rest_remainder=rest_remainder,
            )
        except RateLimitedError:
            self._window.record()
            raise

    async def delete_quote(self, quote_id: str) -> JsonDict:
        try:
            return await self._inner.delete_quote(quote_id)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise

    async def confirm_quote(self, quote_id: str) -> JsonDict:
        try:
            return await self._inner.confirm_quote(quote_id)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise

    async def get_quote(self, quote_id: str) -> JsonDict:
        """GET slice for the fill-record recovery sweep (2026-07-16 P1) — same
        pass-through + 429 tap as the write endpoints, so a rate-limit storm on
        the recovery polls feeds the burst breaker too."""
        try:
            return await self._inner.get_quote(quote_id)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise

    async def get_quotes(self, **params: Any) -> JsonDict:
        """``QuoteLister`` slice for the WITHDRAW-PENDING RESOLVER (2026-07-26):
        the account-wide open-quote list that PROVES whether an UNKNOWN
        withdrawal is off the wire. Same pass-through + 429 tap, so a rate-limit
        storm on the prover feeds the burst breaker like every other endpoint."""
        try:
            return await self._inner.get_quotes(**params)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise

    async def get_fills(self, **params: str | int) -> JsonDict:
        """GET /portfolio/fills slice for the cancel-report verification
        (2026-07-18 verify-before-discard) — same pass-through + 429 tap, so
        verification polls feed the burst breaker too."""
        try:
            return await self._inner.get_fills(**params)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise

    async def get_series(self, ticker: str) -> JsonDict:
        """``SeriesGetter`` slice for the fee observer's series fee_type fetch
        (2026-09-04) — same pass-through + 429 tap."""
        try:
            return await self._inner.get_series(ticker)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise

    async def get_multivariate_collection(self, ticker: str) -> JsonDict:
        """``SeriesGetter`` slice: the collection payload the observer reads a
        combo collection's series ticker from (2026-09-04)."""
        try:
            return await self._inner.get_multivariate_collection(ticker)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise


class CashGateSender:
    """A thin ``QuoteSender`` decorator (2026-08-15, operator lever 4 — the
    dead-time/cash-storm fix): after the exchange refuses a create with
    ``insufficient_balance`` (HTTP 400), refuse further creates LOCALLY
    (``CashGatedError``, no HTTP) until the balance tracker produces a NEW
    poll reading — at which point exactly ONE exchange probe is allowed, and
    a successful create clears the gate entirely.

    Why: cash exhaustion was discovered by erroring — 225k insufficient_
    balance 400s/day at 7.2/s peak (2026-08-13), 181k on 2026-08-15 — each
    burning write budget and log volume to learn a fact the balance poll
    already knows. Probe cadence = the poll cadence (``last_poll_ns_or_
    none``): fully derived, no hand-set timer, and settlements freeing cash
    are discovered on the very next reading. DELETE and CONFIRM are NEVER
    gated (withdrawals and accepted-quote confirms are risk-reducing /
    contractual — gating a confirm would be a renege). Wraps the
    429-recording sender (both decorators stay pristine, hard rule 8);
    ``CashGatedError`` is status-0/local so it never feeds the 429-burst
    breaker."""

    def __init__(self, inner: RateLimitRecordingSender, balance: Any) -> None:
        self._inner = inner
        self._balance = balance
        # Poll stamp at the moment of the last exchange insufficient_balance
        # refusal; None = gate open.
        self._gated_at_poll_ns: int | None = None
        self._gated_creates = 0  # local refusals since gating (telemetry)
        # DELETES free collateral (2026-08-16 PM incident): the poll-only
        # probe cadence (~6/min) throttled creates to a trickle while our
        # own resting quotes' TTL deletions were releasing collateral
        # continuously — 404,314 suppressed creates / 3,348 sends / $1,082
        # deployed on $4.7k equity in 4.5h. Each successful delete_quote is
        # a measured collateral-release event and now also licenses a probe
        # (cadence ≈ the delete churn, ~50/min at normal flow — still ~100x
        # fewer 400s than the pre-gate storm at 7.2/s).
        self._deletes_since_gate = 0

    async def create_quote(
        self,
        rfq_id: str,
        *,
        yes_bid_cc: CentiCents,
        no_bid_cc: CentiCents,
        rest_remainder: bool = False,
    ) -> JsonDict:
        if self._gated_at_poll_ns is not None:
            poll_ns = self._balance.last_poll_ns_or_none()
            fresh_poll = poll_ns is None or poll_ns != self._gated_at_poll_ns
            if not fresh_poll and self._deletes_since_gate == 0:
                self._gated_creates += 1
                raise CashGatedError()
            # A new reading OR a collateral-freeing delete since the last
            # refusal: allow ONE probe (consume the signal); on failure the
            # gate re-stamps below.
            self._deletes_since_gate = 0
        try:
            result = await self._inner.create_quote(
                rfq_id,
                yes_bid_cc=yes_bid_cc,
                no_bid_cc=no_bid_cc,
                rest_remainder=rest_remainder,
            )
        except KalshiApiError as e:
            if e.code == INSUFFICIENT_BALANCE_CODE:
                was_gated = self._gated_at_poll_ns is not None
                self._gated_at_poll_ns = self._balance.last_poll_ns_or_none()
                self._deletes_since_gate = 0
                if not was_gated:
                    log.warning(
                        "cash_gate_armed",
                        rfq_id=rfq_id,
                        suppressed_since_gate=self._gated_creates,
                    )
            raise
        if self._gated_at_poll_ns is not None:
            log.info(
                "cash_gate_cleared",
                rfq_id=rfq_id,
                suppressed_creates=self._gated_creates,
            )
            self._gated_at_poll_ns = None
            self._gated_creates = 0
        return result

    async def delete_quote(self, quote_id: str) -> JsonDict:
        result = await self._inner.delete_quote(quote_id)
        if self._gated_at_poll_ns is not None:
            # Collateral released — license a probe (see __init__ note).
            self._deletes_since_gate += 1
        return result

    async def confirm_quote(self, quote_id: str) -> JsonDict:
        return await self._inner.confirm_quote(quote_id)

    async def get_quote(self, quote_id: str) -> JsonDict:
        return await self._inner.get_quote(quote_id)

    async def get_quotes(self, **params: Any) -> JsonDict:
        return await self._inner.get_quotes(**params)

    async def get_fills(self, **params: str | int) -> JsonDict:
        return await self._inner.get_fills(**params)

    async def get_series(self, ticker: str) -> JsonDict:
        return await self._inner.get_series(ticker)

    async def get_multivariate_collection(self, ticker: str) -> JsonDict:
        return await self._inner.get_multivariate_collection(ticker)


class WholeBookBalanceSource:
    """BalanceSource that merges per-shard exchange readings into ONE
    whole-book payload (operator constitutional ruling 2026-08-17: shards
    are parts of a single book/balance — one total capital, one risk book;
    Kalshi's per-shard wallets are plumbing, never risk entities).

    The exchange's ``balance`` field is already the cross-shard TOTAL, but
    ``portfolio_value`` is scoped to one shard per call (live-verified:
    idx0 pv $1,264.23 vs idx1 pv $4.06 on the same account). Without this
    merge, every SHARD1 fill made the risk denominator SHRINK (cash left
    the total; the position's value never entered it) — the exact
    "deployed != lost" failure the deployed-aware denominator exists to
    prevent, resurrected by sharding. This source sums PV across every
    shard enumerated in ``balance_breakdown`` (dynamic — a new shard is
    picked up on its first appearance) and returns the standard payload
    shape, so ``BalanceTracker`` parses it unchanged. One extra GET per
    additional shard per poll (~6/min at today's 2 shards — read-budget
    noise). Any per-shard fetch failure raises, so a partial reading can
    never masquerade as whole-book (the tracker's stale fail-closed then
    governs)."""

    def __init__(self, rest: KalshiRestClient) -> None:
        self._rest = rest

    async def get_balance(self) -> JsonDict:
        base = await self._rest.get_balance()
        breakdown = base.get("balance_breakdown") or []
        extra_indices = sorted(
            {
                int(row.get("exchange_index", 0))
                for row in breakdown
                if int(row.get("exchange_index", 0)) != 0
            }
        )
        total_pv = int(base.get("portfolio_value", 0))
        for idx in extra_indices:
            shard = await self._rest.get_balance(exchange_index=idx)
            total_pv += int(shard.get("portfolio_value", 0))
        merged = dict(base)
        merged["portfolio_value"] = total_pv
        return merged


class PositionsGetter(Protocol):
    """GET /portfolio/positions slice the periodic position-reconcile net
    reads (2026-07-18 requirement 3). A protocol so tests fake it without a
    real REST client; the live loop passes the KalshiRestClient."""

    async def get_positions(self, **params: str | int) -> JsonDict: ...


def _exchange_exposure_cc_by_ticker(
    positions_payload: dict[str, Any],
) -> tuple[dict[str, int], dict[str, str]]:
    """Per-ticker cost basis of the remaining open position, in cc, from the
    positions payload's own ``market_exposure_dollars`` ("Cost of the aggregate
    market position in dollars" — the money at risk on the open position).

    Returns ``(exposure_cc_by_ticker, unreadable_by_ticker)``: the second maps a
    ticker whose at-risk figure could NOT be read to the raw wire value, so the
    caller can adopt it fail-CLOSED and ALARM instead of silently booking zero.

    PRECISION (2026-07-26 live fail-open defect — this is the whole fix).
    ``market_exposure_dollars`` is a ``FixedPointDollars`` string with "up to 6
    decimal places of precision" (get-positions.md). Measured on the live
    account the same day: 46/46 open rows carry SIX decimals and 21/46 are not a
    whole number of centi-cents ("2.688790" = 26.8879 cc). The exact parser
    ``cc_from_dollars_str`` RAISES on every one of those, and the documented
    int-cents fallback ``market_exposure`` is absent from 46/46 real rows — so
    the fallback was dead code and this function returned NOTHING for 21 of 46
    positions. ``reserve_from_exchange_figures`` then returned None for each and
    the fail-CLOSED reserve — the entire safety net for positions no durable
    source can model — never fired. Measured: 2 open positions worth $9.0106
    contributed ZERO to deterministic max loss at boot AND after reconcile.
    An EXPOSURE is a loss figure feeding fail-closed caps, so it now parses
    through ``exposure_cc_from_dollars_str``, which rounds UP: the booked
    exposure is never BELOW the exchange's truth.

    The int-cents ``market_exposure`` fallback is KEPT (older payload eras and
    the recorded fixtures still carry it) but is no longer load-bearing."""
    rows = positions_payload.get("market_positions") or positions_payload.get("positions") or []
    out: dict[str, int] = {}
    unreadable: dict[str, str] = {}
    for row in rows:
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        dollars = row.get("market_exposure_dollars")
        if dollars is not None:
            try:
                # Fail-CLOSED rounding: ceil to the next whole cc so a
                # sub-cc wire figure is booked at or ABOVE truth, never below.
                out[ticker] = int(exposure_cc_from_dollars_str(str(dollars)))
                continue
            except MoneyParseError:
                unreadable[ticker] = str(dollars)
                continue
        cents = row.get("market_exposure")
        if isinstance(cents, int) and not isinstance(cents, bool):
            out[ticker] = cents * 100
        else:
            unreadable[ticker] = "" if cents is None else repr(cents)
    return out, unreadable


def _bump(metrics: Metrics | None, name: str) -> None:
    """Counter bump that tolerates an absent Metrics sink. The boot rehydrator
    takes ``metrics`` as an explicit optional argument (the live run always
    passes ``self._metrics``) so the alarm never depends on instance state the
    unit tests, which call it as an unbound method, do not construct."""
    if metrics is not None:
        metrics.inc(name)


def reserve_from_exchange_figures(
    ticker: str, side: Side, contracts_centi: int, exposure_cc: int | None
) -> OpenPosition | None:
    """Build the CONSERVATIVELY-RESERVED holding for an exchange position whose
    legs no durable source can resolve (P0-4, ``risk_modeled=False``). ONE code
    path, shared by the startup rehydrator and the runtime reconcile net, so the
    two can never drift.

    Everything comes from exchange truth: side/count from the signed position,
    premium at risk from the exchange's own ``market_exposure``. The entry price
    is rounded UP so the booked ``max_loss_cc`` (= contracts × entry // 100) is
    never BELOW the exchange's figure (fail-safe LARGER). The reserve counts in
    every deterministic / gross / concentration cap and enters the portfolio MC
    as a deterministic reserve — never a leg sampled at a fabricated marginal.

    Identity is a single self-leg (the combo market itself, its own cluster):
    permanently unreadable ⇒ the marginal watch never baselines it (no false
    trip), and its game key is its own singleton (it can't be netted with
    anything anyway). Self-leg side is ALWAYS "yes": a combo settles YES iff its
    own market settles YES — the leg encodes the combo's YES definition, and
    direction lives SOLELY in ``our_side``. Writing our position side here would
    double-complement every NO reserve downstream (receivable sweep, daily mark):
    losers would shield the give-back halts with full notional and winners with
    nothing — the exact inversion of the shield's contract (2026-07-21 review,
    CRITICAL finding 2).

    Returns ``None`` when there is no PROVABLE at-risk figure (missing/unreadable
    /non-positive exposure, or a non-positive count). A caller ADOPTING a position
    must then fall back to ``reserve_at_full_notional`` (fail-CLOSED upper bound)
    and alarm — never to zero."""
    if exposure_cc is None or exposure_cc <= 0 or contracts_centi <= 0:
        return None
    # Entry price per contract, rounded UP: booked max_loss_cc
    # (= contracts × entry // 100) is then ≥ the exchange's exposure.
    entry_cc = -(-exposure_cc * 100 // contracts_centi)  # ceil div
    return _build_reserve(ticker, side, contracts_centi, CentiCents(entry_cc))


def reserve_at_full_notional(
    ticker: str, side: Side, contracts_centi: int
) -> OpenPosition | None:
    """FAIL-CLOSED LAST RESORT (2026-07-26): reserve an exchange position whose
    at-risk figure is UNREADABLE at the maximum possible loss — $1.00 per
    contract.

    This is NOT an invented number (hard rule 6 is about guessing a *plausible*
    figure): a Kalshi binary is bought for at most $1.00 per contract and can
    never lose more than the premium paid, so ``contracts × $1.00`` is a PROVEN
    upper bound on the position's cost basis, derived from nothing but the
    exchange's own signed contract count. It is the only value that is safe in
    the absence of a price, and it is deliberately punitive — an unreadable
    exposure figure should make the caps bind HARDER, not vanish, which is what
    forces the operator to fix the parse rather than quietly run fail-open.

    Callers MUST pair this with an alarm (``exchange_exposure.unreadable``) so
    the degraded state is visible instead of silent.

    Returns ``None`` only for a non-positive contract count (nothing at risk)."""
    if contracts_centi <= 0:
        return None
    return _build_reserve(ticker, side, contracts_centi, ONE_DOLLAR)


def _build_reserve(
    ticker: str, side: Side, contracts_centi: int, entry_price_cc: CentiCents
) -> OpenPosition:
    """The ONE reserve-holding constructor both routes above share, so the
    identity contract documented in ``reserve_from_exchange_figures`` (self-leg
    always "yes", ``risk_modeled=False``, ``reserve:<ticker>`` id) can never
    drift between the priced and the fail-closed path."""
    return OpenPosition(
        position_id=f"reserve:{ticker}",
        combo_ticker=ticker,
        collection=None,
        our_side=side,
        contracts=CentiContracts(contracts_centi),
        entry_price_cc=entry_price_cc,
        legs=(LegRef(ticker, ticker, "yes"),),
        risk_modeled=False,
    )


async def _exchange_position_confirmed_flat(
    rest: PositionsGetter, ticker: str, *, subaccount: int
) -> bool:
    """True iff a TARGETED read returns a row for ``ticker`` whose signed
    position parses to exactly zero — the only provable "flat". A missing or
    unparseable row is NOT flat (fail-safe: never release reserved risk on a
    lagging or unreadable payload); a read error propagates to the caller's
    retry."""
    payload = await rest.get_positions(subaccount=subaccount, ticker=ticker)
    rows = payload.get("market_positions") or payload.get("positions") or []
    for row in rows:
        if str(row.get("ticker") or row.get("market_ticker") or "") != ticker:
            continue
        raw = row.get("position_fp")
        if raw is None:
            return False
        try:
            return int(qty_from_fp_str(str(raw))) == 0
        except ValueError:
            return False
    return False


async def get_positions_paged(
    rest: PositionsGetter, *, subaccount: int | None
) -> dict[str, Any]:
    """The OPEN-positions listing, PAGED TO EXHAUSTION, merged into one payload.

    2026-07-21 review F3: a single unpaginated GET truncates past the endpoint's
    default page size (~100 rows — MLB volume crosses that within days) and a
    truncated read must NEVER be able to read as "flat"/absent. That fix landed
    on the 5-minute reconcile path only; the BOOT rehydrate kept a bare
    ``get_positions(...)`` (2026-07-27 diagnosis), so a restart past ~100 open
    combos would boot an exposure book missing the tail and under-reserve real
    risk for a whole reconcile interval. One helper now, both callers.

    ``count_filter=position`` keeps the listing to genuinely open rows;
    ``subaccount=None`` uses the exchange default (P0-5 pins it when given)."""
    rows: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(25):
        params: dict[str, str | int] = {"limit": 200, "count_filter": "position"}
        if subaccount is not None:
            params["subaccount"] = subaccount
        if cursor:
            params["cursor"] = cursor
        payload = await rest.get_positions(**params)
        rows.extend(payload.get("market_positions") or payload.get("positions") or [])
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    return {"market_positions": rows}


async def position_reconcile_unmodeled_once(
    rest: PositionsGetter,
    exposure: ExposureBook,
    store: Store,
    metrics: Metrics,
    *,
    subaccount: int,
    balance: BalanceTracker | None = None,
) -> list[str]:
    """RUNTIME POSITION-RECONCILE NET (2026-07-18; ADOPTION 2026-07-21).

    Compare the exchange's open positions (read-only GET, pinned to our
    subaccount — P0-5) against the in-memory exposure book. Divergences split
    into three classes:

    1. **Our own fill fell out of the book** (a local fills row exists): the
       fill-recovery sweep owns full re-modeling from the stored RFQ context —
       here it stays ALARM-ONLY so two writers never race one position.
    2. **No local context** (an older store's era, a manual app trade, any
       past-run history — operator directive 2026-07-21: the bot must know its
       standing even for what happened before it went live): ADOPTED as a
       CONSERVATIVELY-RESERVED holding (P0-4, ``risk_modeled=False``) built
       ONLY from exchange truth — side/count from the signed position, premium
       at risk from the exchange's own ``market_exposure``; the entry price is
       rounded UP so the booked ``max_loss_cc`` is never below the exchange's
       figure (fail-safe LARGER). The reserve counts in every deterministic /
       gross / concentration cap and enters the portfolio MC as a
       deterministic reserve — never a leg sampled at a fabricated marginal.
       Identity is a single self-leg (the combo market itself, its own
       cluster): permanently unreadable ⇒ the marginal watch never baselines
       it (no false trip), and its game key is its own singleton (it can't be
       netted with anything anyway). Nothing is ever modeled from a GUESS —
       a row whose exposure figure is unreadable stays alarm-only.
    3. **A reserve whose exchange position went flat** (settled or manually
       exited on the app): REMOVED — the exchange ledger says the risk is
       gone, holding it would overcount forever.

    Returns the unmodeled tickers seen this pass (for tests/callers)."""
    # Page the OPEN-positions listing to exhaustion (2026-07-21 review F3: a
    # single unpaginated GET truncates past ~100 rows — MLB volume crosses
    # that within days — and truncation must never read as "flat").
    merged: dict[str, Any] = await get_positions_paged(rest, subaccount=subaccount)
    exch_by_ticker = open_combo_positions_from_positions(merged)
    exposure_cc_by_ticker, unreadable_exposure = _exchange_exposure_cc_by_ticker(merged)
    if unreadable_exposure:
        # OBSERVABLE, never silent (2026-07-26): a figure we cannot read is the
        # precondition for the fail-open defect this fix closed. Alarm on every
        # pass it persists — the adoption loop below still books these fail-CLOSED.
        metrics.inc("exchange_exposure.unreadable")
        log.error(
            "exchange_exposure_unreadable",
            tickers=sorted(unreadable_exposure),
            raw_values=[unreadable_exposure[t] for t in sorted(unreadable_exposure)],
            detail="the exchange's own at-risk figure could not be parsed for "
            "these open positions — any position ADOPTED below is booked at the "
            "$1.00/contract fail-closed upper bound, never at zero; investigate "
            "the wire format immediately (this is the fail-open defect class)",
        )

    # (3) release reserves the exchange no longer lists open — but ONLY on a
    # TARGETED read whose row parses to an explicit zero (review F3: absence
    # from a listing — a lagging/partial payload, an unparseable row — must
    # never release real reserved risk; only a provable flat does).
    stale_reserves = [
        pos
        for pos in exposure.positions.values()
        if pos.position_id.startswith("reserve:")
        and pos.combo_ticker not in exch_by_ticker
    ]
    for pos in stale_reserves:
        flat = await _exchange_position_confirmed_flat(
            rest, pos.combo_ticker, subaccount=subaccount
        )
        if not flat:
            log.warning(
                "position_reconcile_reserve_missing",
                ticker=pos.combo_ticker,
                detail="reserved position absent from the open listing but NOT "
                "confirmed flat by a targeted read — reserve HELD (fail-safe)",
            )
            continue
        exposure.remove_position(pos.position_id)
        if balance is not None:
            # A receivable noted for this reserve is void with it (review F6).
            balance.cancel_receivable(pos.position_id)
        log.info(
            "position_reconcile_reserve_released",
            ticker=pos.combo_ticker,
            reserved_max_loss_cc=pos.max_loss_cc,
            detail="targeted read confirms the reserved position flat (settled "
            "or externally exited) — reserve released",
        )

    # RESIDUAL-RESERVE REBUILD (2026-07-26, B1). A reserve is a placeholder for
    # the part of an exchange holding that NO leg-aware record covers. The
    # exchange reports ONE aggregate row per ticker, so once modeled positions
    # appear on a reserved ticker there are exactly two wrong answers and one
    # right one:
    #
    #   * keep both      ⇒ DOUBLE-COUNTS the modeled contracts in every
    #                      deterministic / gross / concentration cap;
    #   * drop the whole ⇒ FAILS OPEN. A ``fill:<quote_id>`` position carries
    #     reserve         ONLY its own fill, never the aggregate; repeat fills
    #                      on one combo ticker are NORMAL (the live store shows
    #                      31 / 21 / 19 / 15 on single tickers), so a 200-centi
    #                      fill would erase a 700-centi exchange holding — a 71%
    #                      understatement with only the divergence line alarming;
    #   * QUANTITY-AWARE ⇒ carry ``max(0, exchange − modeled)``, REBUILT from
    #                      exchange truth on every pass. The book then EQUALS the
    #                      exchange rather than choosing which way to be wrong.
    #
    # Premium for the residual comes from the exchange payload's OWN cost basis
    # (``market_exposure``) MINUS the premium the modeled records already book
    # (F1 below) — never an assumed price, never a per-contract average (hard
    # rule 6). With no readable exposure figure, or no positive remainder, the
    # existing reserve is HELD unchanged (fail-safe LARGER) and alarms. Modeled
    # ≥ exchange clamps the reserve to zero (removed); any EXCESS is left to the
    # quantity-divergence net below, which still alarms. And the reserve is only
    # ever SHRUNK by a payload that accounts for the whole book on this ticker
    # (F2 below) — never by one that lags a just-confirmed fill.
    for reserve in [
        p for p in exposure.positions.values() if p.position_id.startswith("reserve:")
    ]:
        ticker = reserve.combo_ticker
        exch = exch_by_ticker.get(ticker)
        if exch is None:
            continue  # absence is owned by the stale-reserve pass above (HELD)
        modeled = [
            p
            for p in exposure.positions.values()
            if p.combo_ticker == ticker
            and p.our_side is exch.side
            and not p.position_id.startswith("reserve:")
        ]
        modeled_centi = sum(int(p.contracts) for p in modeled)
        if modeled_centi <= 0:
            continue  # nothing leg-aware covers this ticker — the reserve stands
        residual_centi = max(0, exch.contracts_centi - modeled_centi)
        # LAGGING-PAYLOAD GUARD (2026-07-26, F2). The positions payload is
        # fetched at the TOP of this function (up to 25 sequential REST pages);
        # ``modeled`` is read from the LIVE book here, many round trips later. A
        # fill confirmed INSIDE that window is in the book but NOT yet in the
        # exchange row, so ``exchange − modeled`` would subtract the brand-new
        # fill FROM THE RESERVE — measured: a true 700-centi / 43,400cc holding
        # collapsing to 500 centi / 31,000cc (a 28.6% undercount) while the
        # divergence net stayed SILENT, because the shrunken book then matched
        # the stale row exactly. Self-healing only at the next reconcile
        # (``risk.position_reconcile_interval_s``) is not a defense.
        #
        # So the reserve is only ever SHRUNK by a payload that accounts for the
        # WHOLE book on this ticker (exchange ≥ reserve + modeled). Otherwise
        # the reserve is HELD at full size (fail-safe LARGER) and the condition
        # is made OBSERVABLE: the held book necessarily exceeds the exchange row,
        # so the quantity-divergence net below alarms, plus a dedicated counter
        # here. A payload that has caught up shrinks it on the very next pass.
        if 0 < residual_centi < int(reserve.contracts):
            metrics.inc("position_reconcile.reserve_hold_lagging_payload")
            log.warning(
                "position_reconcile_reserve_hold_lagging_payload",
                ticker=ticker,
                exchange_contracts_centi=exch.contracts_centi,
                modeled_contracts_centi=modeled_centi,
                reserved_contracts_centi=int(reserve.contracts),
                would_be_residual_contracts_centi=residual_centi,
                detail="the exchange row does not account for the whole book on "
                "this ticker (exchange < reserve + modeled) — a fill confirmed "
                "after this pass's positions fetch reads exactly like this, and "
                "shrinking would subtract that fill FROM the reserve — reserve "
                "HELD at full size (fail-safe LARGER); the quantity-divergence "
                "net alarms until the payload catches up",
            )
            continue
        if residual_centi == 0:
            exposure.remove_position(reserve.position_id)
            if balance is not None:
                # A receivable noted for this reserve is void with it (review F6).
                balance.cancel_receivable(reserve.position_id)
            log.info(
                "position_reconcile_reserve_superseded",
                ticker=ticker,
                reserved_max_loss_cc=reserve.max_loss_cc,
                exchange_contracts_centi=exch.contracts_centi,
                modeled_contracts_centi=modeled_centi,
                detail="leg-aware modeled positions now cover the WHOLE exchange "
                "holding on this ticker — the unknown-legs reserve is superseded "
                "(holding both would double-count identical contracts); any "
                "modeled EXCESS is graded by the quantity-divergence net",
            )
            continue
        exposure_cc = exposure_cc_by_ticker.get(ticker)
        rebuilt = None
        if exposure_cc is not None and exposure_cc > 0:
            # RESIDUAL COST BASIS = exchange aggregate MINUS what the leg-aware
            # records already book (2026-07-26, F1). Pro-rating the aggregate by
            # CONTRACTS assumed every contract on the ticker was bought at the
            # same price; on a price-skewed ticker that is simply false and it
            # fails OPEN. Measured: a reserve bought at 90¢ (500 centi /
            # 45,000cc) plus a modeled fill at 20¢ (200 centi / 4,000cc) —
            # exchange 700 centi / 49,000cc — booked 39,000cc, a 20.4%
            # UNDERSTATEMENT flowing straight into deterministic_max_loss_cc,
            # gross, per-game and every cap that scales off det-max (the reverse
            # skew OVERSTATED by 49.7%).
            #
            # Subtracting the modeled positions' OWN booked premium needs no
            # price assumption at all and makes booked premium identically EQUAL
            # to the exchange figure (reserve + modeled = exchange), not merely
            # approximately. A non-positive remainder (modeled premium already
            # covers the exchange figure — e.g. a lagging cost basis) yields no
            # PROVABLE at-risk amount, so ``reserve_from_exchange_figures``
            # returns None and the reserve is HELD unchanged below (fail-safe
            # LARGER; an at-risk amount is never invented — hard rule 6).
            residual_exposure_cc = max(
                0, exposure_cc - sum(p.max_loss_cc for p in modeled)
            )
            rebuilt = reserve_from_exchange_figures(
                ticker, exch.side, residual_centi, residual_exposure_cc
            )
        if rebuilt is None:
            log.warning(
                "position_reconcile_reserve_residual_unpriced",
                ticker=ticker,
                exchange_contracts_centi=exch.contracts_centi,
                modeled_contracts_centi=modeled_centi,
                residual_contracts_centi=residual_centi,
                detail="modeled positions cover only part of this ticker but the "
                "exchange payload carries no readable at-risk figure — the "
                "existing reserve is HELD unchanged (fail-safe LARGER; an "
                "at-risk amount is never invented)",
            )
            continue
        if (
            int(rebuilt.contracts) == int(reserve.contracts)
            and int(rebuilt.entry_price_cc) == int(reserve.entry_price_cc)
            and rebuilt.our_side is reserve.our_side
        ):
            continue  # already exact — no churn, no generation bump
        # Same position_id ⇒ replaces the reserve in place.
        exposure.add_position(rebuilt)
        if balance is not None:
            # The old size's cash claim is stale; the settlement-receivable
            # refresh re-notes this position at its new size on the next tick.
            balance.cancel_receivable(reserve.position_id)
        log.info(
            "position_reconcile_reserve_resized",
            ticker=ticker,
            exchange_contracts_centi=exch.contracts_centi,
            modeled_contracts_centi=modeled_centi,
            residual_contracts_centi=residual_centi,
            previous_reserved_max_loss_cc=reserve.max_loss_cc,
            reserved_max_loss_cc=rebuilt.max_loss_cc,
            detail="leg-aware modeled positions cover part of this ticker — the "
            "unknown-legs reserve is rebuilt from exchange truth to carry ONLY "
            "the residual contracts, so the book equals the exchange aggregate "
            "(no double count, no dropped premium)",
        )

    # QUANTITY divergence net (review F5): presence alone is not
    # reconciliation — a known ticker whose exchange count/side disagrees with
    # the book total is undercounting (the $31-ARG class) and must alarm.
    book_by_ticker: dict[str, int] = {}
    book_side_by_ticker: dict[str, Side] = {}
    for pos in exposure.positions.values():
        book_by_ticker[pos.combo_ticker] = (
            book_by_ticker.get(pos.combo_ticker, 0) + int(pos.contracts)
        )
        book_side_by_ticker[pos.combo_ticker] = pos.our_side
    for ticker, exch in exch_by_ticker.items():
        if ticker not in book_by_ticker:
            continue
        if (
            exch.contracts_centi != book_by_ticker[ticker]
            or exch.side is not book_side_by_ticker[ticker]
        ):
            metrics.inc("position_reconcile.quantity_divergence")
            log.warning(
                "position_reconcile_quantity_divergence",
                ticker=ticker,
                exchange_contracts_centi=exch.contracts_centi,
                exchange_side=str(exch.side),
                book_contracts_centi=book_by_ticker[ticker],
                book_side=str(book_side_by_ticker[ticker]),
                detail="exchange count/side disagrees with the modeled book — "
                "caps may be undercounting until reconciled (alarm-only; the "
                "startup rehydrate reconciles quantities fail-safe LARGER)",
            )

    # DURABLE-LEDGER QUANTITY reconcile (2026-09-04 build, item D): the
    # in-memory net above proved itself on 8/26 (it alarmed
    # ``position_reconcile_quantity_divergence`` on AC104B1B2E5 at
    # 20:37:08Z — 31 s after the first phantom execution — and every 5 min
    # after), but nothing checks the DURABLE ``position_ledger`` the
    # settlement reconcile grades against. Same exchange payload, one grouped
    # SELECT, alarm-only.
    await ledger_quantity_reconcile_once(store, exch_by_ticker, metrics)

    known = {pos.combo_ticker for pos in exposure.positions.values()}
    unmodeled = sorted(t for t in exch_by_ticker if t not in known)
    if not unmodeled:
        return []
    local_fill_tickers = [
        t for t in unmodeled if await store.has_fill_for_ticker(t)
    ]
    # BOOK COMPLETENESS (2026-07-26): "a local fills row exists" is NOT enough to
    # hand a ticker to the fill-recovery sweep. That sweep re-models from THIS
    # run's in-memory quote state, and both it and the startup rehydrator need a
    # resolvable LEG SET. A combo whose legs no durable source can answer (ledger
    # ∪ tape) is re-modelable by NOBODY — the old ``recovery_owned`` skip made it
    # count in NEITHER path, so its premium silently left deterministic max loss
    # (measured live: 7 combos / $40.03, a 5.35% understatement of every cap that
    # scales off det-max). Ownership now requires PROVEN leg resolvability.
    resolvable = {
        h["combo_ticker"] for h in await store.held_positions(local_fill_tickers)
    }
    recovery_owned = set(local_fill_tickers) & resolvable
    unresolvable_legs = sorted(set(local_fill_tickers) - resolvable)

    adopted: list[str] = []
    alarm_only: list[str] = []
    notional_floored: list[str] = []
    for ticker in unmodeled:
        if ticker in recovery_owned:
            continue  # class 1: the fill-recovery sweep re-models it exactly
        exch = exch_by_ticker[ticker]
        exposure_cc = exposure_cc_by_ticker.get(ticker)
        reserved = reserve_from_exchange_figures(
            ticker, exch.side, exch.contracts_centi, exposure_cc
        )
        if reserved is None:
            # FAIL-CLOSED, NEVER SILENT (2026-07-26). The old code left this
            # position out of the book entirely ("alarm-only") — a real exchange
            # holding contributing ZERO to every cap, which is exactly how the
            # 6-decimal parse failure turned into a fail-OPEN book. An
            # unreadable at-risk figure now books the PROVEN upper bound
            # ($1.00/contract) instead of zero; only a non-positive contract
            # count (nothing at risk) can still leave a ticker unbooked.
            reserved = reserve_at_full_notional(ticker, exch.side, exch.contracts_centi)
            if reserved is None:
                alarm_only.append(ticker)
                continue
            notional_floored.append(ticker)
            metrics.inc("exchange_exposure.reserved_at_full_notional")
            log.error(
                "position_reconcile_reserved_at_full_notional",
                ticker=ticker,
                contracts_centi=exch.contracts_centi,
                raw_exposure=unreadable_exposure.get(ticker, ""),
                reserved_max_loss_cc=reserved.max_loss_cc,
                detail="the exchange's at-risk figure for this open position is "
                "unreadable — booked at the $1.00/contract MAXIMUM POSSIBLE loss "
                "so the caps bind harder, never at zero (fail-CLOSED). Fix the "
                "exposure parse; this reserve deliberately overstates risk",
            )
        exposure.add_position(reserved)
        adopted.append(ticker)
        log.warning(
            "position_reconcile_reserved_adopted",
            ticker=ticker,
            side="yes" if exch.side is Side.YES else "no",
            contracts_centi=exch.contracts_centi,
            exchange_exposure_cc=exposure_cc,
            reserved_max_loss_cc=reserved.max_loss_cc,
            unresolvable_legs=ticker in unresolvable_legs,
            full_notional_fallback=ticker in notional_floored,
            detail="exchange position with no MODELABLE local context adopted "
            "as a conservatively-reserved holding (risk_modeled=False) — "
            "counted in every deterministic/gross cap from exchange figures "
            "only; unknown legs never mean zero exposure",
        )

    metrics.inc("position_reconcile.unmodeled")
    if unresolvable_legs:
        metrics.inc("position_reconcile.unresolvable_legs")
    log.warning(
        "position_reconcile_unmodeled",
        tickers=unmodeled,
        local_fill_tickers=local_fill_tickers,
        unresolvable_leg_tickers=unresolvable_legs,
        adopted_as_reserve=adopted,
        full_notional_fallback=notional_floored,
        alarm_only=alarm_only,
        detail="exchange reports open positions the in-memory risk book did "
        "not model — positions no durable source can re-model are adopted as "
        "reserved holdings (exchange figures only); tickers with a local fills "
        "row AND a resolvable leg set are left to the fill-recovery sweep "
        "(full re-model, 2026-07-18 incident class); rows with an unreadable "
        "exposure figure are booked at the $1.00/contract fail-closed bound "
        "(full_notional_fallback) — only a non-positive contract count is "
        "alarm-only now",
    )
    return unmodeled


async def ledger_quantity_reconcile_once(
    store: Store,
    exch_by_ticker: dict[str, ExchangePosition],
    metrics: Metrics,
) -> list[dict[str, Any]]:
    """PER-TICKER LEDGER QUANTITY vs EXCHANGE (2026-09-04 build, item D;
    ALARM-ONLY). Compare every combo ticker's OPEN ``position_ledger`` rows
    (side + Σ contracts_centi) against the exchange's authoritative
    ``/portfolio/positions`` row for that ticker (the payload the caller has
    already fetched — no second GET). Kinds:

      * ``quantity`` — same side, different count (the 8/26 shape: ledger
        66.71 vs exchange 43.47 on AC104B1B2E5 — 23.24 of phantom rows);
      * ``side``     — the ledger's open rows sit on the other side;
      * ``ledger_only``   — open rows on a ticker the exchange does not hold
        (a phantom execution, or a settled position whose settled write
        never landed — the 9/1 stale-row item);
      * ``exchange_only`` — an exchange holding with no open ledger row (a
        writer-path miss; the fills-ledger sweep owns the tape side).

    Never a writer, never a risk input: a mismatch is a loud WARNING + a
    counted metric; corrections belong to the execution verification path
    (``fill_phantom_execution_voided``), the settlement seam, or
    ``tools/ops/repair_phantom_fills.py``. Bounded output (20 rows) — a
    legacy store with hundreds of stale open rows must not flood the log.

    LEGACY SCOPING (2026-09-04 review fixes): the live store carries 434
    open rows that predate execution verification (settled positions whose
    settled write never landed — the 9/1 stale-row item — plus the 28
    corroborated phantoms awaiting the repair tool). Alarming all of them
    every 5 min would bury a NEW phantom in noise. A mismatch whose ticker
    has NO open row opened at/after the verification migration stamp
    (``Store.fills_verification_watermark``) is LEGACY: counted and named in
    the same log line (``legacy_n`` / ``legacy_by_kind`` / a bounded ticker
    list), never in the alarmed list or the mismatch metric. A ticker with
    any post-fix open row is alarmed in full, legacy rows included."""
    try:
        watermark = await store.fills_verification_watermark()
        since = watermark[1] if watermark is not None else None
        ledger = await store.open_ledger_quantity_by_ticker(post_fix_since=since)
    except Exception as exc:  # noqa: BLE001 — alarm-only; never into the loop
        metrics.inc("ledger_quantity.read_failed")
        log.warning("ledger_quantity_reconcile_read_failed", error=repr(exc))
        return []
    mismatches: list[dict[str, Any]] = []
    legacy: list[dict[str, Any]] = []
    for ticker in sorted(ledger):
        side, total_cc, n_rows, n_post_fix = ledger[ticker]
        exch = exch_by_ticker.get(ticker)
        if exch is None:
            kind = "ledger_only"
            exch_side: str | None = None
            exch_cc = 0
        else:
            exch_side = exch.side.value
            exch_cc = exch.contracts_centi
            if exch.side.value != side:
                kind = "side"
            elif exch.contracts_centi != total_cc:
                kind = "quantity"
            else:
                continue
        row = {
            "ticker": ticker,
            "kind": kind,
            "ledger_side": side,
            "ledger_contracts_centi": total_cc,
            "ledger_rows": n_rows,
            "ledger_rows_post_fix": n_post_fix,
            "exchange_side": exch_side,
            "exchange_contracts_centi": exch_cc,
        }
        (mismatches if n_post_fix > 0 else legacy).append(row)
    for ticker in sorted(exch_by_ticker):
        if ticker in ledger:
            continue
        exch = exch_by_ticker[ticker]
        mismatches.append(
            {
                "ticker": ticker,
                "kind": "exchange_only",
                "ledger_side": None,
                "ledger_contracts_centi": 0,
                "ledger_rows": 0,
                "ledger_rows_post_fix": 0,
                "exchange_side": exch.side.value,
                "exchange_contracts_centi": exch.contracts_centi,
            }
        )
    metrics.inc("ledger_quantity.checks")
    legacy_by_kind: dict[str, int] = {}
    for row in legacy:
        legacy_by_kind[str(row["kind"])] = legacy_by_kind.get(str(row["kind"]), 0) + 1
    if legacy:
        metrics.inc("ledger_quantity.legacy", by=len(legacy))
    if not mismatches:
        log.info(
            "ledger_quantity_mismatch_clean",
            tickers=len(ledger),
            exchange_tickers=len(exch_by_ticker),
            legacy_n=len(legacy),
            legacy_by_kind=legacy_by_kind,
            post_fix_since=since,
        )
        return []
    by_kind: dict[str, int] = {}
    for row in mismatches:
        by_kind[str(row["kind"])] = by_kind.get(str(row["kind"]), 0) + 1
    metrics.inc("ledger_quantity.mismatch", by=len(mismatches))
    for kind, n in by_kind.items():
        metrics.inc(f"ledger_quantity.mismatch.{kind}", by=n)
    log.warning(
        "ledger_quantity_mismatch",
        n=len(mismatches),
        by_kind=by_kind,
        ledger_tickers=len(ledger),
        exchange_tickers=len(exch_by_ticker),
        mismatches=mismatches[:20],
        legacy_n=len(legacy),
        legacy_by_kind=legacy_by_kind,
        legacy_tickers=[str(r["ticker"]) for r in legacy[:20]],
        post_fix_since=since,
        detail="per-ticker OPEN position_ledger quantity disagrees with "
        "/portfolio/positions — alarm-only, no risk effect; a phantom "
        "execution shows here as ledger_only/quantity (the 8/26 class), a "
        "settled row that never closed as ledger_only; rows that predate "
        "execution verification are counted as legacy_* (not alarmed); "
        "corrections belong to the execution verification path, the "
        "settlement seam, or tools/ops/repair_phantom_fills.py",
    )
    return mismatches


class _StoreSettlementLedger:
    """Adapter: the settlement handler's tiny ledger surface onto the Store's
    durable ``position_ledger`` (2026-07-26). Fire-and-forget on the store's
    own writer queue so a settlement never blocks on disk; the handler already
    wraps calls in try/except so a failure here degrades to today's
    in-memory-only behaviour instead of breaking the money path."""

    def __init__(self, store: Store) -> None:
        self._store = store
        # STRONG REFERENCES to in-flight writes. asyncio holds only a WEAK ref
        # to a bare create_task, so a settled write could be garbage-collected
        # mid-flight — the exact silent-drop species this fix exists to kill.
        self._tasks: set[asyncio.Task[None]] = set()

    def record_settled(
        self,
        *,
        position_id: str,
        settled_value: float,
        realized_pnl_cc: int,
        settlement_fee_cc: int,
        leg_set_hash: str | None = None,
        combo_ticker: str | None = None,
        our_side: str | None = None,
        contracts_centi: int | None = None,
    ) -> None:
        import asyncio

        task = asyncio.get_running_loop().create_task(
            self._settle(
                position_id=position_id,
                settled_value=settled_value,
                realized_pnl_cc=realized_pnl_cc,
                settlement_fee_cc=settlement_fee_cc,
                leg_set_hash=leg_set_hash,
                combo_ticker=combo_ticker,
                our_side=our_side,
                contracts_centi=contracts_centi,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _settle(
        self,
        *,
        position_id: str,
        settled_value: float,
        realized_pnl_cc: int,
        settlement_fee_cc: int,
        leg_set_hash: str | None,
        combo_ticker: str | None,
        our_side: str | None,
        contracts_centi: int | None,
    ) -> None:
        """Write + REPORT. ``record_position_settled`` returns the row it
        actually settled; None means the durable keyspace had no open row for
        this position (pre-ledger fill, or a boot upsert that never ran) — a
        silent drop before 2026-07-26, now a loud warning so the ledger's
        coverage gap can never go unobserved again."""
        landed = await self._store.record_position_settled(
            position_id,
            settled_value=settled_value,
            realized_pnl_cc=realized_pnl_cc,
            settlement_fee_cc=settlement_fee_cc,
            leg_set_hash=leg_set_hash,
            combo_ticker=combo_ticker,
            our_side=our_side,
            contracts_centi=contracts_centi,
        )
        if landed is None:
            log.warning(
                "position_ledger_settled_unmatched",
                position_id=position_id,
                combo_ticker=combo_ticker,
                realized_pnl_cc=realized_pnl_cc,
                detail="no OPEN position_ledger row matched this settlement by "
                "position_id OR durable (leg_set_hash, combo_ticker, our_side) "
                "key — realized P&L is booked in memory but NOT durable; the "
                "day-anchored p_night seed and settlement calibration will "
                "under-count it",
            )
        elif landed != position_id:
            log.info(
                "position_ledger_settled_by_stable_key",
                position_id=position_id,
                ledger_position_id=landed,
                combo_ticker=combo_ticker,
                detail="settled row matched across an id re-mint (restart) via "
                "the durable leg-set identity",
            )


class _StoreOrphanLedger:
    """Adapter: the settlement handler's ORPHAN-ROW reconciliation onto the
    Store's ``position_ledger`` (2026-07-27).

    AWAITED, not fire-and-forget (unlike ``_StoreSettlementLedger``): the
    handler must see whether the row actually closed before it counts it, and
    this path runs in the 30 s settlement loop where a disk round-trip costs
    nothing. It never writes anything but the settled transition of a row that
    is currently ``open``, so a re-polled settlement is a no-op."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def open_ledger_tickers(self) -> set[str]:
        return await self._store.open_ledger_tickers()

    async def open_ledger_rows_for_ticker(
        self, combo_ticker: str
    ) -> list[dict[str, Any]]:
        return await self._store.open_ledger_rows_for_ticker(combo_ticker)

    async def close_ledger_row_settled(
        self,
        position_id: str,
        *,
        settled_value: float,
        realized_pnl_cc: int,
        settlement_fee_cc: int,
        reconciled_at: str,
    ) -> str | None:
        return await self._store.record_position_settled(
            position_id,
            settled_value=settled_value,
            realized_pnl_cc=realized_pnl_cc,
            settlement_fee_cc=settlement_fee_cc,
            reconciled_at=reconciled_at,
        )


class QuoteApp:
    def __init__(self, config: AppConfig) -> None:
        if config.mode not in (Mode.PAPER, Mode.QUOTE):
            raise ValueError("QuoteApp runs in paper or quote mode")
        config.assert_safe_to_run()
        self._conventions = load_conventions()
        if config.mode is Mode.QUOTE:
            self._conventions.require_verified()  # Phase 2.5 gate — hard
            if not config.filters.collection_whitelist:
                raise RuntimeError("quote mode requires a non-empty collection whitelist")
        self._config = config
        self._clock = SystemClock()
        # Clock-backed metrics so the latency-spike breaker can sample a
        # recent-window max (not the all-time histogram max, which one historical
        # slow confirm would latch forever).
        self._metrics = Metrics(self._clock)
        self._watched: set[str] = set()
        # Per-COLLECTION combo-grid cache (throughput, 2026-07-14). Combo market
        # tickers are UNIQUE per RFQ, so fetching each combo's grid was a per-RFQ
        # REST read that blew the read-rate budget (live 429 storm). Every combo in
        # a collection shares one grid, so we fetch it ONCE per collection and reuse
        # it (metadata.put_combo_grid) for every other combo of that collection.
        self._collection_grid: dict[str, PriceGrid] = {}
        # PERSISTENT METADATA CACHE (2026-07-25): set at boot in run();
        # the maintenance loop flushes ~every 60s when dirty (off-loop).
        self._metadata_cache: MetadataCache | None = None
        self._metadata_cache_path = str(config.data_dir / "metadata_cache.json")
        self._metadata_persist_ticks = 0
        # DERIVED OPEN-QUOTE CAPACITY (2026-07-31): window sampler for the
        # measured new-quote token rate — (mono_ns, quote.sent counter) at
        # the last derivation — plus the tick counter that paces derivations
        # to the same ~60s cadence as the metadata flush. The LimitChecker
        # handle is set in run() so the "on" mode can swap max_open_quotes;
        # None until then (shadow logging works without it).
        self._capacity_probe_ticks = 0
        self._capacity_probe_prev: tuple[int, int] | None = None
        self._limit_checker: LimitChecker | None = None
        self._stop = asyncio.Event()
        # OBSERVED rate-limit tier + the READ bucket derived from it (2026-07-26).
        # Both start at the fail-safe FLOOR so any code path that reads them
        # before ``run()`` asks the exchange paces itself as the smallest tier.
        self._api_tier: ApiTierLimits = LOWEST_TIER_LIMITS
        self._read_budget: TokenBudget | None = None
        # Phase 6 out-of-process safety plumbing. The heartbeat file is what the
        # external supervisor reads; the reconcile marker enforces
        # block-restart-until-reconciled (both live under data_dir so the
        # standalone supervisor process finds them at the same paths).
        self._heartbeat = Heartbeat(self._clock, config.data_dir / "heartbeat.txt")
        # LIVENESS ("alive") vs PROGRESS ("working") — split 2026-07-26 after the
        # 20:12:54Z false kill. The heartbeat above is now beaten ONLY by the
        # dedicated ``_liveness_loop`` (its whole job is to prove the event loop
        # schedules); every loop that matters publishes a last-progress age
        # here, and the supervisor escalates on EITHER signal. Splitting them is
        # what stops a legitimately slow maintenance pass from reading as a
        # wedge without blinding the supervisor to a real one — see
        # risk/progress.py.
        self._progress = ProgressLedger(self._clock, progress_path(config.data_dir))
        # DERIVED MAINTENANCE STALL WALL (2026-09-05, item 6): the maintenance
        # loop's kill bound is measured from its own completed inter-mark gaps
        # (this boot + the retained boots' tape) and can only LOOSEN from the
        # register-time bound by measurement. Derived at registration and
        # refreshed on the metadata-flush cadence in ``_maintenance_loop``;
        # ``_stall_wall`` is the latest derivation (None until registered).
        # The boot key names this boot's row in the tape; the run id from the
        # relighter is preferred so the tape row and the receipts agree.
        self._stall_wall: StallWallDerivation | None = None
        self._stall_wall_ticks = 0
        self._boot_started_at_ts = self._clock.now().timestamp()
        self._boot_key = (
            os.environ.get(RUN_ID_ENV, "") or self._clock.now().isoformat()
        )
        # The wall APPLIED to the ledger (review fix 2026-09-05, must-fix #1):
        # ``supervisor.stall_wall_derived`` "shadow" keeps the floor and only
        # LOGS the derived wall; "on" applies it. None until the first
        # derivation.
        self._stall_wall_applied_s: float | None = None
        # EXPIRED-ACCEPT RATE baseline (review should-fix #5): pooled
        # (expired, confirmed, boots) over the retained PRIOR boots, refreshed
        # with the stall wall; None = no prior boot => the lifecycle's alarm
        # stays unjudged.
        self._expired_baseline: tuple[int, int, int] | None = None
        self._reconcile_marker = ReconcileMarker(config.data_dir / "needs_reconcile")
        # 429-burst window for the rate-limit circuit breaker (recorded from the
        # REST error paths in the polling loops).
        self._rate_limit_window = RateLimitWindow(
            clock=self._clock, window_s=config.breakers.rate_limit_window_s
        )
        # Set once the startup reconcile succeeds and the marker is clear — the
        # book-reconciled preflight gate reads this.
        self._book_reconciled = config.mode is not Mode.QUOTE
        # Metadata-change breaker baseline: per market ticker, the last sampled
        # SETTLEMENT fingerprint (the payoff function) and LIFECYCLE fingerprint
        # (the exchange's trading-state bookkeeping), plus the status/result the
        # transition rules read and the end-of-life horizon. First sighting
        # seeds the baseline (no trip); off the hot path (status loop, 15s).
        self._metadata_fingerprints: dict[str, _MetaBaseline] = {}
        # SCOPED response for a LIFECYCLE-only metadata move (2026-07-26): the
        # market is held out of quoting and its resting quotes pulled, instead
        # of killing the whole book for one market's pause/close-time rewrite.
        # Bounded by the markets this process has actually held. See
        # risk/quarantine.py for the fail-closed properties.
        self._market_quarantine = MarketQuarantine()
        # ── HALT RECEIPT evidence (auto-relight, 2026-07-27) ─────────────────
        # Purely a RECORD of decisions the metadata lanes already made. Nothing
        # here is read to make a halt/quarantine choice, so it cannot change
        # what the breaker does; it exists so an out-of-process reader can
        # RE-DERIVE the halt's class from its own evidence instead of trusting
        # a label (see ``_write_halt_receipt`` and ops/relight.py gate G7).
        #
        # ``_lifecycle_evidence``: captured AT THE MOMENT a market is
        # quarantined, and held, because the promotion to a whole-bot halt
        # happens on a LATER tick by which time the fingerprints no longer
        # differ — the evidence of the move would otherwise be gone.
        self._lifecycle_evidence: dict[str, dict[str, Any]] = {}
        # ``_quarantine_failure_kinds``: which withdrawal failure modes left a
        # quarantine unenforced, per market, from the enforcement pass itself.
        self._quarantine_failure_kinds: dict[str, dict[str, int]] = {}
        # ``_halt_evidence`` / ``_halt_tripwire``: the CURRENT status-tick
        # diagnosis, rebuilt every pass, serialized by ``on_halt``.
        self._halt_evidence: dict[str, dict[str, Any]] = {}
        self._halt_tripwire: tuple[str, str] | None = None
        self._halt_receipt_path = config.data_dir / HALT_RECEIPT_FILENAME
        # The external SafetySupervisor subprocess (launched on startup in quote
        # mode). A SEPARATE OS process so its kill path survives the bot's own
        # host deadlocking; None until launched / when launch is skipped.
        self._supervisor_proc: asyncio.subprocess.Process | None = None

    async def run(self) -> None:
        config = self._config
        configure_logging(
            json_output=config.logging.json_output, level=config.logging.level
        )
        conventions = self._conventions
        log.info(
            "quote_app_starting",
            env=str(config.env),
            mode=str(config.mode),
            conventions=conventions.source,
        )

        # KILL FILE SURVIVES RESTART (CLAUDE.md hard rule): consult it
        # SYNCHRONOUSLY before any quoting can begin. A supervisor kill (or a
        # human) leaves KILL on disk; the async watcher below only notices it on
        # its ~1s poll, which a revived bot could beat to the first quote. This
        # up-front check closes that race — a revived bot with KILL present
        # refuses to start, full stop.
        self._refuse_if_kill_file_present()
        signer = RequestSigner(Credentials.for_env(str(config.env)), self._clock)
        killswitch = KillSwitch(self._clock, kill_file=config.kill_file)
        store = await Store.open(
            config.data_dir / config.observe.db_name_for(config.env), self._clock
        )
        # Move tape writes (rfqs/decisions) OFF the hot path: an inline WAL
        # checkpoint on the ~2GB DB was freezing the whole event loop 34s+ during
        # RFQ bursts (2026-07-14 pipeline audit). Fills stay synchronous & durable.
        store.start_writer()
        ws = WsManager(config.endpoints.ws_url, signer, self._clock, self._metrics)
        # CONFIRM PRIORITY (2026-07-31 double halt): accept/execute frames jump
        # the comms dispatch backlog, and while a confirm is in flight all NEW
        # quote work yields (gate derivation in AcceptPriorityGate). The book
        # socket is deliberately NOT priority-marked or held — last look needs
        # its freshness the most exactly while a confirm is in flight.
        ws.mark_priority("quote_accepted", "quote_executed")
        # MARKET-DATA SHED CLASS (2026-08-01 pregame-surge deafness — full
        # derivation at WsManager._sheddable_types). The rfq_created firehose
        # + deletions are individually recoverable (one missed auction; the
        # next arrives in seconds), so a saturated dispatch queue drops the
        # OLDEST of them and STAYS CONNECTED instead of the overflow⇒
        # reconnect cycle that made the bot deaf to every new auction for
        # most of the 2026-08-01 pregame window (14 disconnect cycles,
        # 276,644 frames discarded, zero new-RFQ intake at the exact hours
        # this pregame-only bot fills in). Order-integrity frames (the
        # priority lane above) keep their fail-closed guarantees untouched.
        # The book socket below is deliberately NOT marked: orderbook deltas
        # are seq-dependent — shedding one corrupts the mirror silently;
        # reconnect+resnapshot is the only sound recovery there.
        ws.mark_sheddable("rfq_created", "rfq_deleted")
        accept_gate = AcceptPriorityGate(self._clock, EXCHANGE_CONFIRM_WINDOW_S)
        # DEDICATED order-book socket (2026-07-14 fix). The communications firehose
        # (~650 msg/s exchange-wide RFQ stream on `ws`) and the orderbook_delta feed
        # MUST NOT share a connection: the firehose saturates the dispatcher and
        # STARVES book snapshots + subscribe-acks, so leg mirrors stay empty and
        # every combo reads skip_leg_book_thin in bursts (main markets with millions
        # of contracts of depth looked "thin"). PROVEN: a dedicated book socket pulls
        # the deep books instantly (ADVANCE-ENG mid 54.5¢, 2.27B ct valid) while the
        # shared socket subscribed exactly ONE book all run. Own socket, shared
        # signer/clock/metrics.
        book_ws = WsManager(
            config.endpoints.ws_url, signer, self._clock, self._metrics
        )
        feed = OrderbookFeed(book_ws, self._clock, self._metrics)
        # Quote mode: gate the exchange-wide RFQ firehose PRE-PARSE on the series
        # allowlist (intake docstring has the measured numbers). Observe mode
        # (app.py) passes no prefixes and keeps recording everything.
        allowed = config.filters.allowed_leg_series_prefixes
        intake = RfqIntake(
            ws,
            self._metrics,
            series_prefixes=tuple(allowed) if allowed is not None else None,
            # Confirm-priority intake hold — see AcceptPriorityGate + the
            # intake docstring. Self-bounding (exchange confirm window).
            hold_probe=accept_gate.holding,
            # Wire-age pre-parse gate (2026-08-01): the SAME freshness horizon
            # the worker-side dwell gate enforces, applied at the earliest
            # observable instant (the WS read loop's receive stamp) so a
            # surge-deep dispatch backlog drains its dead frames at
            # subtraction cost instead of parse cost. Derivation in the
            # intake docstring.
            stale_horizon_s=RFQ_MAX_QUEUE_DWELL_S,
            clock=self._clock,
        )
        inplay = InPlayDetector(self._clock)

        external = self._build_external_odds()
        async with KalshiRestClient(config.endpoints.rest_base_url, signer) as rest:
            # ── OBSERVED RATE-LIMIT TIER (2026-07-26) ────────────────────────
            # Both token buckets come from the exchange, not from a constant.
            # ``WRITE_TOKENS_PER_S = 300`` used to be hard-coded with the
            # comment "we are on Advanced" while the only recorded in-repo
            # observation said BASIC (100 write tok/s) — i.e. the paced wave
            # was either correct or a 2.2x breach and nothing in the process
            # could tell which. Now it asks; an unreadable answer falls back to
            # the LOWEST documented tier, never the highest.
            tier = await observe_api_tier(rest)
            self._api_tier = tier
            # READ bucket: one shared budget for every metadata GET. Sized
            # VERBATIM from the observed bucket — capacity is the exchange's own
            # burst allowance and the refill window is that capacity at the
            # exchange's own refill rate, so there is no number here a human
            # picked.
            #
            # CRITICAL RESERVE (2026-07-27). One bucket shared by the
            # CONTINUOUS metadata refresh and the LOW-VOLUME settled-leg
            # resolver is a PRIORITY INVERSION: metadata wins every race for
            # the last token, so what starves is the correctness-critical read
            # (live run: 27 settled_fetch_failed, every one a 429). The reserve
            # is exactly ONE settled-resolution pass — the resolver's own
            # bounded per-pass claim × the live-verified per-endpoint token
            # cost (GET /account/endpoint_costs default_cost=10). Both factors
            # are measured/observed state, and the reserve moves automatically
            # if either does. It costs the metadata tier 50 of 600 tokens
            # (8.3%) and it is what guarantees a settlement resolution can
            # always be served while metadata yields.
            critical_read_reserve = FETCH_BUDGET_PER_PASS * DEFAULT_ENDPOINT_TOKEN_COST
            read_budget = TokenBudget.create(
                self._clock,
                capacity=tier.read_capacity,
                refill_s=tier.read_refill_s,
                reserve=critical_read_reserve,
            )
            log.info(
                "read_budget_armed",
                capacity=tier.read_capacity,
                refill_s=tier.read_refill_s,
                sustained_tokens_per_s=tier.read_refill_per_s,
                per_call_token_cost=DEFAULT_ENDPOINT_TOKEN_COST,
                critical_reserve_tokens=critical_read_reserve,
                critical_reserve_calls=FETCH_BUDGET_PER_PASS,
                routine_capacity=tier.read_capacity - critical_read_reserve,
                detail="settlement resolution spends CRITICAL (may draw the "
                "reserve); routine metadata refresh yields at the floor",
            )
            self._read_budget = read_budget
            metadata = MetadataCache(rest, self._clock, read_budget=read_budget)
            # PERSISTENT METADATA CACHE (2026-07-25): warm boot from the last
            # run's still-live markets — kills the boot 429 burst (2,319
            # failed fetches in 36s at the 7/25 v6 boot) that left low-flow
            # families (HR/HIT/TB/RBI) unpriceable until sparse RFQ flow
            # re-healed them. peek() serves instantly; the async path still
            # revalidates each loaded entry on first touch (TTL-expired
            # stamps). Corrupt/missing file ⇒ cold boot, never an error.
            self._metadata_cache = metadata
            metadata.load_persisted(self._metadata_cache_path)
            # THE ONE MEASURED FEE SCHEDULE (2026-09-04 fee-seam repair):
            # loaded from data_dir before the engine exists and shared by
            # the pricer, the lifecycle ledger/waiver and the observer sweep
            # that refits it in place from charged exchange fills.
            fee_schedule = load_observed_fee_schedule(config.pricing.fee, config.data_dir)
            engine = PricingEngine(
                feed,
                metadata,
                conventions,
                config.pricing,
                extra_sources=(
                    [(external[0], config.pricing.external_odds.weight)] if external else []
                ),
                fee_schedule=fee_schedule,
            )
            if config.pricing.leg_pricing_aliases:
                # Loud install record (review 2026-07-16): a mistyped alias
                # that never matches is otherwise invisible — this line plus
                # the classification-mix metrics are the operator's check that
                # the mapping actually fires on live flow.
                log.info(
                    "pricing_aliases_active",
                    aliases=dict(config.pricing.leg_pricing_aliases),
                )
            # Stage B: the per-game loss cap is mutual-exclusion-aware; it asks the
            # metadata cache whether an event's market family is mutually exclusive
            # (advance(ARG) ⊥ advance(ENG)) so opposite-side same-event positions
            # net instead of comonotone-summing. event_mutually_exclusive reads the
            # cache synchronously (None when unfetched ⇒ that dimension is skipped).
            exposure = ExposureBook(
                conventions, is_me_event=metadata.event_mutually_exclusive
            )
            risk_cfg = config.risk
            # RiskLimits now carries the R2 %-of-bankroll cap layer (Phase 2,
            # SHADOW by default); to_risk_limits() parses the decimal-string
            # percentages into exact Fractions (no binary-float money).
            base_limits = risk_cfg.to_risk_limits()
            limits = LimitChecker(base_limits)
            # DERIVED OPEN-QUOTE CAPACITY (2026-07-31): the maintenance
            # loop's capacity derivation needs the live checker to swap
            # ``max_open_quotes`` in "on" mode (``set_limits``, the
            # derived_cap_engine seam). Shadow only logs.
            self._limit_checker = limits
            # Correlation-adaptive cap engine (North Star). off -> None (the static
            # fracs above enforce, byte-identical to prior behaviour); shadow/enforce
            # -> the _adaptive_caps_loop derives the deploy+halt caps nightly and, in
            # enforce, swaps them onto `limits` via set_limits. Invalid mode fails
            # CLOSED at startup rather than silently enforcing an unintended layer.
            if risk_cfg.adaptive_caps_mode not in ("off", "shadow", "enforce"):
                raise ValueError(
                    "risk.adaptive_caps_mode must be off/shadow/enforce, got "
                    f"{risk_cfg.adaptive_caps_mode!r}"
                )
            cap_engine = (
                DerivedCapEngine(
                    base_limits, kill_anchor=risk_cfg.adaptive_caps_kill_anchor
                )
                if risk_cfg.adaptive_caps_mode in ("shadow", "enforce")
                else None
            )
            # GAME series to count tonight's slate from (the per-game cap divisor):
            # the "<prefix>GAME" series of the allowlist (KXMLB -> KXMLBGAME).
            # Distinct game_keys across their OPEN markets = the live slate size —
            # the fully-adaptive source for expected_games (config value is only a
            # bootstrap fallback if the count is unavailable). No hand-set slate size.
            game_series = tuple(p + "GAME" for p in (allowed or ()))
            # Derive the caps ONCE now — before the RFQ workers can quote — so the
            # FIRST fill is bound by the DERIVED caps, never the looser static config
            # caps that sit underneath only as a fallback. The _adaptive_caps_loop
            # then re-derives nightly. shadow just logs; enforce swaps immediately.
            if cap_engine is not None:
                _eg = await self._count_slate_games(rest, game_series)
                self._refresh_adaptive_caps_once(
                    limits,
                    cap_engine,
                    risk_cfg.adaptive_caps_mode,
                    _eg or risk_cfg.adaptive_caps_expected_games,
                )
            # Live bankroll denominator for the %-caps (fail-closed on stale) +
            # the starvation watchdog. The tracker is polled in _balance_loop.
            balance_tracker = BalanceTracker(
                conventions,
                self._clock,
                stale_after_s=BALANCE_STALE_AFTER_S,
                # 2026-07-28: previously left at balance.py's module default of
                # 0.5, so every dollar deployed shrank the ceiling it was being
                # measured against ($100 deployed -> denominator -$50 ->
                # ceiling -$18). Now an explicit operator anchor; see
                # RiskConfig.portfolio_haircut for the measurement.
                portfolio_haircut=risk_cfg.portfolio_haircut,
            )
            watchdog = StarvationWatchdog(threshold=risk_cfg.starvation_threshold)
            # Pregame precision tier a2: an operator-set explicit schedule table
            # (config-validated tz-aware ISO strings -> ScheduleCache). Empty
            # default = tier inactive, identical to the always-empty cache the
            # gate constructed before this plumbing existed.
            schedule = ScheduleCache(
                {
                    event_ticker: datetime.fromisoformat(raw)
                    for event_ticker, raw in config.filters.pregame_scheduled_starts.items()
                }
            )
            if config.filters.pregame_scheduled_starts:
                # Loud install record (adversarial verify 2026-07-16): an entry
                # that never matches a live event ticker is otherwise invisible
                # — this line is the operator's check that the table is armed
                # (the canonical-key config validator guards the match itself).
                log.info(
                    "pregame_scheduled_starts_active",
                    entries=dict(config.filters.pregame_scheduled_starts),
                )
            rfq_filter = RfqFilter(
                config.filters,
                feed,
                metadata,
                killswitch,
                self._clock,
                schedule,
                # ARMS the scoped metadata-change lane: the filter is the
                # consumer that refuses NEW quotes on a quarantined market.
                # Without this the breaker hard-halts on lifecycle moves as it
                # did before (MarketQuarantine.armed).
                self._market_quarantine,
            )
            # Phase 5 (R3): inventory skew + widen-vs-decline policies, both DARK
            # by default (SkewConfig.enabled / WidenConfig.enabled False ⇒ computed
            # + logged, passed as 0 / non-blocking). The skew's headroom
            # denominators are the SAME enforced per-event caps the LimitChecker
            # uses (the % of headroom left drives the convex ramp); notional uses
            # the book-wide gross cap as a loose per-game denominator.
            skew_cfg = config.pricing.skew
            widen_cfg = config.pricing.widen
            skew_params = SkewParams(
                w_conc=skew_cfg.w_conc,
                w_off=skew_cfg.w_off,
                gamma=skew_cfg.gamma,
                skew_max_widen_cc=skew_cfg.skew_max_widen_cc,
                skew_max_tighten_cc=skew_cfg.skew_max_tighten_cc,
                enabled=skew_cfg.enabled,
                # PEAK-CONCENTRATION steer (2026-07-18): additive component on
                # the same armed seam; its clamps compose with the directional
                # caps above (overall bound documented on SkewParams).
                peak_enabled=skew_cfg.peak_enabled,
                peak_widen_max_cc=skew_cfg.peak_widen_max_cc,
                peak_tighten_max_cc=skew_cfg.peak_tighten_max_cc,
                # P(BOOK) STEER (2026-07-25): shadow-computed by default;
                # ``pricing.skew.pbook_armed: true`` in the armed YAML is the
                # ONE flip that adds it into pricing (after the shadow slate
                # validates + adversarial review).
                pbook_enabled=skew_cfg.pbook_enabled,
                pbook_armed=skew_cfg.pbook_armed,
                # LEG-DIRECTION AXIS steer (2026-07-25): shadow-computed by
                # default; ``pricing.skew.leg_axis_armed: true`` is the ONE
                # flip that adds it into pricing (after its shadow slate).
                leg_axis_enabled=skew_cfg.leg_axis_enabled,
                leg_axis_armed=skew_cfg.leg_axis_armed,
                # LEVER #5 CONCENTRATION STEER (2026-07-27): shadow-computed by
                # default; ``pricing.skew.conc_armed: true`` is the ONE flip
                # that lets it price (after the shadow read-out).
                # ``conc_enabled: false`` stops it being computed at all.
                conc_enabled=skew_cfg.conc_enabled,
                conc_armed=skew_cfg.conc_armed,
                # SETTLED-LEG FACT RESOLUTION (2026-08-01):
                # ``pricing.skew.settled_fact_resolution: true`` is the ONE
                # flip that lets the skew's snapshot fact-resolve exchange-
                # determined legs out of its concentration input (price-only;
                # caps unaffected). Default False = today's behaviour.
                settled_fact_resolution=skew_cfg.settled_fact_resolution,
            )
            skew_limits = SkewLimits(
                max_event_delta_contracts=risk_cfg.max_event_delta_contracts,
                max_event_worst_case_loss_dollars=(
                    risk_cfg.max_event_worst_case_loss_dollars
                ),
                max_event_gross_notional_dollars=risk_cfg.max_gross_notional_dollars,
            )
            widen_params = WidenPolicyParams(
                enabled=widen_cfg.enabled, util_threshold=widen_cfg.util_threshold
            )
            # Quote mode: wrap the REST sender so create/delete/confirm 429s feed
            # the rate-limit-burst breaker (not just the polls), then wrap THAT
            # in the cash gate (2026-08-15 lever 4): one insufficient_balance
            # probe per fresh balance reading instead of a 181k/day 400-storm.
            # Paper never 429s and never runs out of cash.
            sender: PaperSender | CashGateSender | RateLimitRecordingSender
            # FILL-RECORD RECOVERY (2026-07-16 P1): the GET-capable handle the
            # lifecycle's recovery sweep polls — the SAME wrapped REST sender the
            # write path uses (its get_quote taps 429s into the burst breaker
            # too). Paper mode wires none: paper quotes never confirm, so there
            # is nothing to recover (the sweep stays off, fail-closed).
            quote_getter: RateLimitRecordingSender | None
            if config.mode is Mode.PAPER:
                sender = PaperSender()
                quote_getter = None
            else:
                rate_tapped = RateLimitRecordingSender(
                    rest, self._rate_limit_window
                )
                # cash_gate_enabled False (2026-08-16 shard discovery — see
                # the config field): the sender is NOT wrapped, restoring
                # the pre-gate create behaviour byte-identically.
                sender = (
                    CashGateSender(rate_tapped, balance_tracker)
                    if config.risk.cash_gate_enabled
                    else rate_tapped
                )
                quote_getter = rate_tapped
            # Real fee model for the fill fee the ledger books at execution
            # (defense #3) — the SAME live schedule object the engine prices
            # on, so ledger, waiver and quote can never disagree on the fee.
            fee_cfg = config.pricing.fee
            fee_model = FeeModel(fee_schedule, conventions)
            fee_type = FeeType.parse(fee_cfg.default_fee_type)
            fee_multiplier = Fraction(Decimal(fee_cfg.default_multiplier))
            # The PRICER's real within-game rho, built ONCE from the engine's
            # shipped SgpParams via the pricer's own build_sgp_correlation. Shared
            # by the lifecycle's portfolio-CVaR MC AND the observability report MC
            # so BOTH use the same per-pair correlations we quote on.
            within_game_rho = sgp_within_game_rho_provider(engine.sgp_params)
            # A1: the SAME Dixon-Coles constants the pricer uses, as a decoupled
            # view for the structural portfolio-risk MC (recompute_book_risk samples
            # same-game legs from the joint scoreline instead of the copula).
            _sc = config.pricing.structural
            structural_cfg = StructuralConfigView(
                dc_rho=_sc.dc_rho,
                et_factor=_sc.et_factor,
                pens_win_a=_sc.pens_win_prob,
                half_share=_sc.half_share,
                max_goals=_sc.max_goals,
                knockout_series=tuple(_sc.knockout_series),
                enabled=_sc.enabled,
                corners_et_loading=_sc.corners_et_loading,
            )
            # Off-loop joint pricing (Phase 1). Live quote mode only: cold-combo
            # CPU runs in worker processes with a deadline so it can never wedge
            # the loop. Warm memo hits stay inline. Paper/backtests price inline
            # (deterministic, no process pool). Warmed before any traffic so the
            # first off-loop price doesn't pay a cold-import tail.
            joint_pool: JointPool | None = None
            # P2-2: full-book portfolio MC off the event loop. Live quote mode only:
            # a large book's MC runs in a worker process (generation-safe) so it can
            # never block the maintenance loop long enough to starve the supervisor
            # heartbeat under the RFQ firehose. Paper/backtests run the MC inline.
            book_risk_pool: BookRiskPool | None = None
            if config.mode is Mode.QUOTE:
                # P2-1 layer 4: ONCE, before any pool spawns, reap pool workers a
                # PRIOR crashed run orphaned (identity-verified — never kills a
                # stranger) and truncate the registry. Doing it here (not per-pool)
                # means the second pool's start can't clobber the first pool's
                # freshly-recorded PIDs; each pool then only APPENDS its own.
                cleanup_straggler_workers(config.data_dir)
                joint_pool = JointPool(
                    config.pricing,
                    conventions,
                    workers=POOL_WORKERS,
                    deadline_s=POOL_DEADLINE_S,
                    data_dir=config.data_dir,
                )
                joint_pool.start()
                await joint_pool.warmup()
                # workers=2 (2026-07-16, research F10 + live evidence): ONE
                # worker served three masters — the ~seconds-long maintenance
                # snapshot MC, the candidate-gate MC, and the Problem-A waiver
                # enumeration — inside the 3s confirm window. The waiver's FIRST
                # live shot (quote b0d6696e, 19:50:30Z, a pure game-loss breach
                # it was built to rescue) timed out at 1.0s while the
                # enumeration itself measures 87ms warm: the wall was queue-wait
                # behind an in-flight snapshot. A second worker gives confirm-
                # window calls a free lane; correctness rests on the P0-2
                # generation/version stamps (review-verified), not on worker
                # exclusivity.
                book_risk_pool = BookRiskPool(
                    workers=2,
                    data_dir=config.data_dir,
                    # Workers must hold the pricing aliases or an aliased
                    # champion leg prices structurally on the loop but nets
                    # adversarially in the risk/waiver MC (see BookRiskPool).
                    pricing_aliases=config.pricing.leg_pricing_aliases,
                )
                book_risk_pool.start()
                # Eager warmup (review 2026-07-16): without it worker #2 only
                # spawns on the first CONTENDED submit — i.e. cold-imports 2.66s
                # inside the first waiver/candidate confirm window after every
                # restart — and the per-run register poll stalled the loop 1.0s
                # per call until then.
                await book_risk_pool.warmup()
            # SETTLED-LEG MARGINAL RESOLUTION (2026-07-18 live outage): a
            # committed leg whose market settled (book gone from the feed)
            # resolves to the exchange-GRADED 0/1 fact — fetched off the
            # maintenance tick via public GET /markets/{ticker}, permanently
            # cached — so a cross-game book stays risk-modelable after one of
            # its games settles. Knob: risk.settled_marginal_resolution
            # (False ⇒ None ⇒ the pre-fix fail-closed behaviour).
            # ...through the SAME read bucket the metadata cache spends from
            # (2026-07-26). This is the second-biggest read source on the tape
            # (123 of the incident's 429s) and it is a SLOW, off-loop, bounded
            # pass, so it WAITS for tokens rather than refusing — a graded fact
            # must eventually land. One bucket, or it is not a budget.
            settled_marginals = build_settled_resolver(
                risk_cfg,
                _PacedMarketSource(rest, self._reserve_read_token),
                self._clock,
                self._metrics,
            )
            lifecycle = QuoteLifecycle(
                clock=self._clock,
                sender=sender,
                engine=engine,
                rfq_filter=rfq_filter,
                limits=limits,
                exposure=exposure,
                feed=feed,
                metadata=metadata,
                inplay=inplay,
                killswitch=killswitch,
                conventions=conventions,
                store=store,
                metrics=self._metrics,
                lastlook_policy=LastLookPolicy(
                    leg_move_tolerance_cc=risk_cfg.leg_move_tolerance_cc,
                    joint_move_tolerance_cc=risk_cfg.joint_move_tolerance_cc,
                    max_leg_age_s=risk_cfg.max_leg_age_s,
                ),
                # YAML risk knobs → LifecycleConfig via the ONE pure builder
                # (candidate gate + its deadline, EV tolerance, MC waiver —
                # see build_lifecycle_config for the per-knob rationale).
                config=build_lifecycle_config(
                    risk_cfg,
                    peak_topk_states=skew_cfg.peak_topk_states,
                    peak_n_clusters=skew_cfg.peak_n_clusters,
                    peak_cluster_min_frac=skew_cfg.peak_cluster_min_frac,
                ),
                balance_tracker=balance_tracker,
                # Slate cap's per-leg game-start source — the exact pregame gate
                # the filter already uses (peek-only, hot-path safe, no network).
                start_time_provider=rfq_filter.leg_start_time,
                starvation_watchdog=watchdog,
                # Portfolio-CVaR MC: the PRICER's real within-game rho (built from
                # the engine's shipped SgpParams via the pricer's own
                # build_sgp_correlation) so the book-risk joint tail uses the same
                # per-pair correlations we quote on, not the flat default band.
                within_game_rho=within_game_rho,
                # A1/A2: structural portfolio-risk MC (joint-scoreline sampling +
                # P(ruin)); same Dixon-Coles constants the pricer uses.
                structural_cfg=structural_cfg,
                # Phase 5 quoting policies (DARK by default; see above).
                skew_params=skew_params,
                skew_limits=skew_limits,
                widen_params=widen_params,
                # Real fill fee at execution (defense #3).
                fee_model=fee_model,
                fee_type=fee_type,
                fee_multiplier=fee_multiplier,
                # MAKER-FEE LIST (2026-07-16; an OVERRIDE since 2026-09-04):
                # prefixes that force the maker fee type regardless of
                # observation. The live mechanism is ``fee_schedule``.
                maker_fee_active_prefixes=tuple(fee_cfg.maker_fee_active_prefixes),
                # MEASURED FEE SCHEDULE (2026-09-04): the shared observed
                # schedule + its persistence path; the lifecycle's observer
                # sweep ingests /portfolio/fills (same GET handle the
                # recovery sweep polls), refits, alarms on drift, persists.
                fee_schedule=fee_schedule,
                fee_schedule_path=fee_schedule_path(config.data_dir),
                series_getter=quote_getter,
                joint_pool=joint_pool,
                book_risk_pool=book_risk_pool,
                # FILL-RECORD RECOVERY (2026-07-16 P1): REST GET handle for the
                # maintenance sweep (None in paper mode — nothing to recover).
                quote_getter=quote_getter,
                # CANCEL-REPORT VERIFY-BEFORE-DISCARD (2026-07-18): the
                # /portfolio/fills handle the sweep polls before believing a
                # CANCELLED status on a confirmed quote (same wrapped sender —
                # its 429s feed the burst breaker), pinned to our subaccount at
                # the query layer (P0-5). None in paper mode.
                fills_getter=quote_getter,
                fills_subaccount=config.safety.subaccount,
                # WRITE-TOKEN PACING for the end-of-game withdrawal wave
                # (2026-07-26 adversarial gate B1): the SAME bucket and the SAME
                # operator knob the supervisor's emergency cancel-all is paced
                # by, so the two write bursts cannot be sized apart and there is
                # one number to move. A concurrency literal cannot bound a token
                # RATE — see rfq/lifecycle.py's BOUNDED QUOTE WITHDRAWAL block.
                withdraw_budget=self._tier_clamped_write_budget(),
                # WITHDRAW-PENDING RESOLVER (2026-07-26 gate): the PROVER for an
                # UNKNOWN withdrawal is a READ, not a retried write. Same wrapped
                # sender (its 429s feed the burst breaker) exposing
                # GET /communications/quotes, and the SAME single read bucket
                # every other read charges — 10 read tokens per maintenance tick
                # that has anything pending, O(1) in the pending count, on the
                # bucket a write storm is not touching. None in paper mode ⇒ the
                # resolver falls back to the metered write drain (fail-closed:
                # nothing is ever dropped without proof either way).
                quote_lister=quote_getter,
                read_budget=self._read_budget,
                # WEDGE FIX (2026-07-16, the 18:13Z kill; re-pointed 2026-07-26
                # at the PROGRESS ledger): the lifecycle marks progress per
                # iteration inside its long maintenance sub-loops (reprice
                # sweep / recovery polls) — a loop grinding through a big book
                # is working, not wedged. It no longer writes the liveness
                # heartbeat (the dedicated ``_liveness_loop`` owns that), so a
                # slow sub-loop can never masquerade as a dead process, and a
                # sub-loop that stops iterating still ages this mark and still
                # escalates.
                beat=self._progress.marker(LOOP_MAINTENANCE),
                # BOUNDED STORE AWAITS on the maintenance path (2026-09-05,
                # item 6): each direct store await is wrapped in a wait
                # derived from the same measured gap distribution as the
                # stall wall (``wall / MARGIN``), so a saturated store yields
                # a logged timeout, never a wedge.
                sub_step_bound_s=self._sub_step_bound_s,
                # TAINT (review fix 2026-09-05, must-fix #1): a store await
                # that gives up at its bound taints the ledger's gap in
                # progress, so the wall is never derived from a gap the bound
                # itself produced (the ratchet: a gap in (wall/2, wall]
                # doubles the wall).
                taint_progress_gap=self._progress.tainter(LOOP_MAINTENANCE),
                # The APPLIED wall — what the supervisor kills at — so a whole
                # pass over it is counted (``maintenance.tick_over_wall``)
                # even though progress is marked between sub-steps and the
                # supervisor therefore no longer bounds the pass (should-fix
                # #4).
                stall_wall_s=self._applied_stall_wall_s,
                # EXPIRED-ACCEPT RATE baseline (should-fix #5): the pooled
                # (expired, confirmed, boots) of the retained prior boots for
                # the ``confirm_expired_rate_anomalous`` alarm.
                confirm_expired_baseline=self._confirm_expired_baseline,
                # F2 MID-PIPELINE LIVENESS (throughput synthesis 2026-07-16):
                # the intake's liveness view over its open-RFQ registry
                # (populated on rfq_created, popped on rfq_deleted). The
                # lifecycle re-checks it at dequeue / post-price / pre-POST so
                # RFQs POSITIVELY deleted mid-flight stop consuming pool +
                # POST budget. A comms-WS drop CLEARS the registry with no
                # replay, and RFQs queued just before the drop can still be
                # live and winnable (the REST POST needs no WS) — so absence
                # after a disconnect is UNKNOWN, not deletion, and the view
                # keeps answering alive for disconnect-cleared ids (risk
                # audit fix 2026-07-16; intake._handle_disconnect). NOTE:
                # active in BOTH paper and quote modes (additive skips only);
                # only tests/backtests with no registry wired are inert.
                rfq_alive=intake.rfq_alive,
                # SETTLED-LEG MARGINAL RESOLUTION (2026-07-18): see above.
                settled_marginals=settled_marginals,
            )
            # NESTED-LADDER RESOLUTION for the risk view (2026-07-27). The rho
            # provider is built ABOVE (it is a lifecycle constructor argument),
            # but the ONE marginal source it needs — the lifecycle's feed/settled
            # resolver — only exists now. Binding it here means the SAME provider
            # object held by the lifecycle's portfolio MC, the candidate gate's
            # shipped rho_pairs, and the report MC resolves same-ladder rung pairs
            # (a starter's K-ladder) to their EXACT comonotone coupling instead of
            # the +0.04 MEASURED FOR TWO OPPOSING STARTERS. Nothing else in the
            # provider changes: every non-nested pair keeps the marginal-less band
            # (see sim/within_game_rho.py).
            within_game_rho.bind_marginals(lifecycle._marginals)  # noqa: SLF001 (wiring seam)
            # R3 Phase 3: single-writer risk-reservation service. Wired AFTER the
            # lifecycle (it reuses the lifecycle's shadow splitter, so a %-cap
            # breach in Phase-2 SHADOW mode never denies a reservation — only
            # ENFORCED breaches do). Reserves headroom BEFORE each confirm so two
            # RFQs can't both claim the same headroom under any future fan-out.
            reservation = RiskReservationService(
                exposure=exposure,
                limits=limits,
                breach_splitter=lifecycle.partition_breaches,
            )
            lifecycle.attach_reservation(reservation)

            # SETTLEMENT handler (Phase 6, code audit 2026-07-13 §3): the live
            # wiring that makes the realized-P&L ledger + exchange-first settlement
            # reconciliation ACTIVE. Polled by _settlement_loop; books each settled
            # position we HOLD, feeds realized P&L into the ENFORCED daily-loss cap,
            # and HALTs HALT_RECONCILIATION_MISMATCH on any to-the-cent mismatch.
            settlement_handler = SettlementHandler(
                exposure=exposure,
                balance_tracker=balance_tracker,
                lifecycle=lifecycle,
                killswitch=killswitch,
                # DURABLE SETTLEMENT LEDGER (2026-07-26): position_ledger had
                # no live writer, so p_night's day-anchored seed was a silent
                # no-op and settlement calibration was unanswerable locally.
                ledger=_StoreSettlementLedger(store),
                # LEDGER RECONCILIATION (2026-07-27): close open ledger rows for
                # combos that settled while they were NOT in the exposure book
                # (they settled during a process gap, or boot dropped them as
                # already-finalized). Ledger-only, fail-closed, no cash/risk
                # effect — the repair for the 56-row / $775.85 divergence that
                # was alarm-only all day.
                orphan_ledger=_StoreOrphanLedger(store),
            )
            settlement_poller = SettlementPoller(
                source=rest,
                handler=settlement_handler,
                poll_interval_s=SETTLEMENT_POLL_INTERVAL_S,
            )

            # Phase 6 circuit breakers: fail-closed detectors that trip the kill
            # switch on the known failure signatures. Evaluated in the status
            # loop off the hot path (a trip cancels-all + stops via on_halt).
            breakers = CircuitBreakers(
                killswitch, config.breakers.to_thresholds(), self._clock
            )

            # Idempotent startup: reconcile before doing anything, THEN enforce
            # the Phase 6 go-live gates. Both are quote-mode only (demo/paper are
            # unaffected).
            if config.mode is Mode.QUOTE:
                # BLOCK-RESTART-UNTIL-RECONCILED: a needs_reconcile marker left by
                # a prior hard halt / supervisor kill means a restarted bot must
                # NOT resume quoting until it reconciles its book. The startup
                # reconcile is the exchange-first pass that satisfies it.
                await self._block_restart_until_reconciled(rest, reservation)
                # #33: rehydrate the exposure book from the exchange's open positions
                # (+ our fills for legs/price) so the caps + portfolio MC see what we
                # already hold — a restarted bot must NOT quote on an empty book.
                await self._rehydrate_exposure_book(
                    rest,
                    store,
                    exposure,
                    config.filters.allowed_leg_series_prefixes,
                    subaccount=config.safety.subaccount,
                    metrics=self._metrics,
                )
                # ARM THE REHYDRATED LEGS (2026-07-21 review, HIGH): watch
                # their books and fetch their metadata NOW — a restarted bot
                # otherwise holds committed legs with no cached metadata, so
                # the pregame start ladder resolves UNKNOWN and the in-play
                # watch exemption silently stands down (the mid-slate-relight
                # halt storm, the exact 2026-07-19 signature). Best-effort:
                # a failed fetch retries via _ensure_watched's peek-None rule.
                await self._arm_rehydrated_legs(exposure, feed, metadata)
                # STARTUP FIRST SNAPSHOT (2026-07-16 warmup fix): compute ONE
                # book-risk snapshot SYNCHRONOUSLY — after rehydration, before
                # quote processing — so a restarted bot's first RFQs are gated
                # against a FRESH tail instead of failing closed on the never-
                # measured book for the first ~40s (69 skip_portfolio_cvar
                # warmup declines, report 2026-07-16-heartbeat-config-fix…).
                # Bounded; on timeout/error startup proceeds exactly as today.
                # DAY-ANCHORED REALIZED SEED (2026-07-25 operator KPI: p_night
                # = P(the DAY ends positive) must roll across restarts — the
                # in-process realized accumulator resets at boot). Reconstruct
                # today's realized P&L (ET day, the slate convention) from the
                # durable position ledger + fills fees, and seed the lifecycle
                # accumulator BEFORE the first book-risk snapshot so p_night
                # carries the banked day from the first publish. Failure ⇒
                # p_night degrades to p_book (never blocks boot).
                try:
                    now_et = self._clock.now().astimezone(
                        ZoneInfo("America/New_York")
                    )
                    day_start = now_et.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    start_iso = day_start.astimezone(UTC).isoformat()
                    end_iso = (
                        (day_start + timedelta(days=1))
                        .astimezone(UTC)
                        .isoformat()
                    )
                    seeded_cc = await store.day_realized_pnl_cc(
                        start_iso, end_iso
                    )
                    if seeded_cc:
                        lifecycle.record_realized_pnl(seeded_cc)
                    log.info(
                        "realized_pnl_day_seeded",
                        realized_cc=seeded_cc,
                        day_start_utc=start_iso,
                    )
                except Exception:
                    log.exception("realized_pnl_seed_failed")
                # ACCEPTANCE-TAPE BOOT SEED (2026-08-13, the 8/1 empty-tape
                # defect). OFF-THREAD single-flight: the reconstruction reads
                # ~77s cold from the 150GB store, so boot is never delayed —
                # the tape stays empty (today's fail-safe) until the seed
                # lands, and the ADDITIVE merge is race-free against intraday
                # increments. A second READ-ONLY stdlib connection: never the
                # shared aiosqlite writer thread (the 2026-07-26 stall).
                # Failure ⇒ empty tape ⇒ exactly today (never blocks boot).
                if config.risk.acceptance_seed_from_store:
                    seed_db = config.data_dir / config.observe.db_name_for(
                        config.env
                    )
                    seed_award = config.risk.risk_qty_award_sizing

                    async def _seed_acceptance() -> None:
                        from dataclasses import asdict

                        from combomaker.ops.acceptance_seed import (
                            seed_counts_from_store,
                        )

                        try:
                            res = await asyncio.to_thread(
                                seed_counts_from_store,
                                seed_db,
                                now_utc=self._clock.now().astimezone(UTC),
                                award_sizing=seed_award,
                            )
                            if res is None:
                                log.warning("acceptance_tape_seed_failed")
                                return
                            lifecycle.seed_acceptance_tape(
                                res.quoted, res.accepted
                            )
                            log.info(
                                "acceptance_tape_seed_result", **asdict(res)
                            )
                        except Exception:
                            log.exception("acceptance_tape_seed_failed")

                    self._acceptance_seed_task = asyncio.create_task(
                        _seed_acceptance(), name="acceptance-seed"
                    )
                await self._startup_book_risk_snapshot(lifecycle)
                # LAUNCH THE EXTERNAL SUPERVISOR (separate OS process) BEFORE the
                # preflight so its own-heartbeat is beating when external_kill_
                # reachable is graded. The bot beats its heartbeat first so the
                # supervisor has a file to watch from t=0.
                self._heartbeat.beat()
                # …and publish a FRESH progress ledger for the same reason,
                # which additionally OVERWRITES any leftover from a previous
                # run. A stale ledger on disk would latch the new supervisor's
                # reader and read as an instantly-stalled loop — the same
                # stale-liveness-file trap the launcher already clears for the
                # heartbeat (tools/ops/start_all.ps1). No loops are registered
                # yet, so this is an empty, un-stallable snapshot.
                self._progress.publish()
                await self._launch_supervisor()
                await self._await_supervisor_heartbeat()
                # PROD PREFLIGHT: every go-live condition must be green before the
                # first quote. Refuses to start on any red gate.
                self._run_prod_preflight()

            # RFQs skipped for transient reasons (books warming up on first
            # sighting) get retried until quoted, dead, or out of attempts —
            # a one-shot RFQ must not be starved by lazy subscriptions.
            pending: dict[str, tuple[Rfq, int, int]] = {}  # rfq_id → (rfq, attempts, recv_mono_ns)

            # RFQ WORK POOL (2026-07-14). The intake pre-parse gate (RfqIntake
            # series_prefixes) already drops the ~90% non-allowlist firehose before
            # it reaches here, so handle_rfq runs only for WC/MLB combos — but
            # pricing + metadata fetch + the quote POST are slow (10s-100s ms) and
            # the WS dispatcher is single-threaded, so running handle_rfq INLINE on
            # it blocked the dispatch-queue drain and overflowed it every ~35s
            # (live 2026-07-14). The on_rfq handler now only ENQUEUES (put_nowait,
            # fast); a small pool of workers prices concurrently. The lifecycle +
            # single-writer reservation service were built for concurrent RFQs.
            # 8 workers (2026-07-14 throughput fix). The EARLIER 8-worker wedge
            # (heartbeat 15.7s > 15s → supervisor kill) was with pricing INLINE:
            # CPU-bound joints monopolised the loop. That heavy phase is now
            # OFFLOADED to the POOL_WORKERS process pool, so a worker AWAITS the pool
            # (yields control → the maintenance loop beats the heartbeat) and only
            # the light prefix/suffix (book microprice + quote construction) runs
            # inline. So 8 async workers now FEED the 8 pool processes without
            # starving the loop. At 2 workers + an 8-deep queue the bot STOPPED for
            # whole minutes under RFQ bursts (skip_rfq_closed 400-560/min,
            # skip_price_deadline steady): the queue backed up and RFQs closed / hit
            # the deadline before we posted. WATCH the heartbeat on the first run —
            # if it wedges, the offload assumption is wrong; drop back to 4.
            # (RFQ_WORKERS is module-level — the read-budget wait bound is
            # derived from it, see _READ_BUDGET_WAIT_ATTEMPTS.)
            # WIN-THE-TAKER FRESHNESS (2026-07-14 P1). A combo RFQ has a ~1s window;
            # an RFQ that sat in our queue too long can only rfq_closed AFTER wasting
            # pool budget on it — starving the fresh RFQs we could still win. Now the
            # queue is SHALLOW and holds (rfq, recv_mono_ns): on overflow we evict the
            # OLDEST and keep the freshest (was: dropped the newest — backwards), and
            # a worker SKIPS any RFQ whose queue dwell already exceeds the budget
            # before spending a pool slot. Off-loop pricing means CPU never wedges the
            # loop regardless, so the levers here are purely about answering FRESH.
            RFQ_QUEUE_MAX = 32           # buffer RFQ bursts (was 8 → dropped bursts)
            # Price RFQs up to RFQ_MAX_QUEUE_DWELL_S (1.5s) old. Combo RFQs live
            # ~11s median, so the old 0.4s SKIPPED still-winnable fresh RFQs
            # during bursts — a stop driver. 1.5s is still well inside the
            # window and, with 8 pool workers, the queue drains fast enough
            # that few RFQs ever dwell this long. (Hoisted to module level
            # 2026-08-01 — the intake's wire-age gate shares it.)
            RFQ_RETRY_WINDOW_S = 2.0     # stop retrying a pending RFQ once it's this old
            rfq_work: asyncio.Queue[tuple[Rfq, int]] = asyncio.Queue(maxsize=RFQ_QUEUE_MAX)
            # IN-PLAY SHADOW throughput isolation (2026-07-25): the lifecycle's
            # measurement-only shadow pricer (filters.inplay_shadow_enabled,
            # default OFF) may price an in-play-skipped RFQ ONLY while this
            # queue is idle (qsize == 0 ⇒ zero queued live RFQs to delay); the
            # bound is the pool's own measured state, not a hand-tuned sample
            # rate. Attached post-construction because the queue is created
            # here, after the lifecycle (the attach_reservation pattern).
            lifecycle.attach_rfq_backlog_probe(rfq_work.qsize)

            async def handle_rfq(rfq: Rfq, recv_mono: int) -> None:
                # RECORD-AFTER-PRICE FAST-LANE (2026-07-16 B6): pricing first,
                # tape row after — via the exactly-once helper, so an error
                # path (worker exception) still records the RFQ once.
                # seen_at SEMANTICS (risk audit fix 2026-07-16): capture the
                # pickup wall-clock NOW — before pricing — and pass it through,
                # so the late-landing row still means "worker pickup,
                # pre-pricing" (the pre-fast-lane meaning every latency
                # instrument reads: stamping at write time inflated
                # wire→pickup by the handling duration and drove
                # pickup→quote_sent negative).
                picked_up_at = self._clock.now()

                async def price_path(r: Rfq) -> None:
                    if not r.is_combo:
                        return
                    await self._ensure_watched(r, feed, metadata)
                    await lifecycle.handle_rfq(r)
                    if not lifecycle.has_open_quote(r.rfq_id):
                        pending[r.rfq_id] = (r, 0, recv_mono)

                await handle_rfq_record_after(
                    rfq,
                    handle=price_path,
                    record=lambda r: store.record_rfq(
                        r, source="ws", seen_at=picked_up_at
                    ),
                )

            async def rfq_worker() -> None:
                while True:
                    rfq, recv_mono = await rfq_work.get()
                    # PROGRESS (2026-07-26): the quote path advanced. Marked on
                    # DEQUEUE so a worker that goes into a long price/POST and
                    # never comes back ages the mark. Idleness is handled by the
                    # registered probe (an empty work queue is not a wedge), so
                    # a quiet market never looks stalled and a deadlocked worker
                    # pool with work queued always does.
                    self._progress.mark(LOOP_QUOTE)
                    try:
                        # CONFIRM PREEMPTS QUOTING (2026-07-31): park before
                        # starting new work while a confirm is in flight. The
                        # dwell check BELOW then discards anything that went
                        # stale during the hold — no extra staleness risk.
                        await accept_gate.wait_clear()
                        dwell_s = (self._clock.monotonic_ns() - recv_mono) / 1e9
                        if dwell_s > RFQ_MAX_QUEUE_DWELL_S:
                            # Already too stale to win its window — don't spend a
                            # pool slot on a combo that will just rfq_closed.
                            self._metrics.inc("rfq.skipped_stale_in_queue")
                        else:
                            await handle_rfq(rfq, recv_mono)
                    except Exception:
                        log.exception("rfq_worker_failed", rfq_id=rfq.rfq_id)
                    finally:
                        rfq_work.task_done()
                        # Yield unconditionally between RFQs so a full queue can
                        # never monopolise the loop away from the heartbeat / pongs.
                        await asyncio.sleep(0)

            async def on_rfq_enqueue(rfq: Rfq) -> None:
                # Non-blocking: the WS dispatcher must NOT stall on pricing. Keep the
                # FRESHEST: on a full queue, evict the oldest queued RFQ and enqueue
                # this one (drop-oldest), so workers always price recent RFQs.
                item = (rfq, self._clock.monotonic_ns())
                try:
                    rfq_work.put_nowait(item)
                except asyncio.QueueFull:
                    try:
                        rfq_work.get_nowait()
                        rfq_work.task_done()
                        self._metrics.inc("rfq.evicted_oldest_for_fresh")
                    except asyncio.QueueEmpty:  # pragma: no cover - racy drain
                        pass
                    try:
                        rfq_work.put_nowait(item)
                    except asyncio.QueueFull:  # pragma: no cover - still full
                        self._metrics.inc("rfq.work_dropped_backpressure")

            async def retry_pending() -> None:
                while True:
                    await asyncio.sleep(1.0)
                    if accept_gate.holding():
                        continue  # confirm in flight — retries are quote work
                    for rfq_id, (rfq, attempts, recv_mono) in list(pending.items()):
                        age_s = (self._clock.monotonic_ns() - recv_mono) / 1e9
                        # Drop once quoted, out of attempts, OR past the RFQ window
                        # (retrying a closed RFQ just wastes a pool slot on a certain
                        # rfq_closed — the win-the-taker anti-pattern).
                        if (
                            lifecycle.has_open_quote(rfq_id)
                            or attempts >= 5
                            or age_s > RFQ_RETRY_WINDOW_S
                        ):
                            pending.pop(rfq_id, None)
                            continue
                        try:
                            await lifecycle.handle_rfq(rfq)
                        except Exception:
                            log.exception("pending_retry_failed", rfq_id=rfq_id)
                        pending[rfq_id] = (rfq, attempts + 1, recv_mono)

            async def on_rfq_deleted_cleanup(rfq_id: str, msg: JsonDict) -> None:
                pending.pop(rfq_id, None)

            # Confirm path OFF the dispatch loop (2026-07-14 audit). on_quote_accepted
            # awaits confirm_quote (REST POST) + record_fill (sync DB commit); running
            # that INLINE on the single communications dispatch loop head-of-line-blocks
            # NEW rfq_created intake during a fill burst → the 8 workers drain rfq_work
            # and go idle → a quote block. Enqueue instead; ONE worker drains FIFO
            # (preserves per-quote accept→execute order) so confirms never block the
            # firehose consumer. Unbounded + never-drop: quote events are rare (not the
            # firehose) and losing one = a missed confirm / an unbooked fill.
            quote_event_q: asyncio.Queue[tuple[str, JsonDict, int]] = asyncio.Queue()

            async def on_quote_event(kind: str, msg: JsonDict) -> None:
                # quote_created is a no-op ack downstream (already counted by
                # intake's metric) — keep it out of the confirm lane entirely
                # so it can never sit ahead of an accept (2026-07-31).
                if kind not in ("quote_accepted", "quote_executed"):
                    return
                if kind == "quote_accepted":
                    # Quoting yields to the confirm from the moment the accept
                    # is SEEN, not when the worker picks it up — the pipeline
                    # ahead of the worker is exactly what must go quiet.
                    accept_gate.accept_enqueued()
                quote_event_q.put_nowait((kind, msg, self._clock.monotonic_ns()))

            async def quote_event_worker() -> None:
                while True:
                    kind, msg, enq_ns = await quote_event_q.get()
                    # Lane-wait observability (2026-07-31 measurement gap: the
                    # tape could not split WS-delivery wait from lane wait).
                    self._metrics.observe_ms(
                        f"confirm.lane_wait_ms.{kind}",
                        (self._clock.monotonic_ns() - enq_ns) / 1e6,
                    )
                    try:
                        if kind == "quote_accepted":
                            await lifecycle.on_quote_accepted(msg)
                        elif kind == "quote_executed":
                            await lifecycle.on_quote_executed(msg)
                    except Exception:
                        log.exception("quote_event_worker_failed", kind=kind)
                    finally:
                        if kind == "quote_accepted":
                            # Release on EVERY path (the gate's stated
                            # invariant): confirm_ok, decline, and raise all
                            # come through here.
                            accept_gate.accept_done()
                        quote_event_q.task_done()

            intake.on_rfq(on_rfq_enqueue)
            intake.on_rfq_deleted(lifecycle.on_rfq_deleted)
            intake.on_rfq_deleted(on_rfq_deleted_cleanup)
            intake.on_quote_event(on_quote_event)

            async def on_invalidate(reason: str) -> None:
                await lifecycle.cancel_all(reason)

            feed.on_invalidate(on_invalidate)

            async def on_halt(event: HaltEvent) -> None:
                # HALT RECEIPT (2026-07-27, auto-relight). FIRST statement, so
                # the evidence lands even if cancel-all then hangs or throws.
                # Never blocks the cure: it is fully wrapped, and a failed
                # receipt simply means the relighter refuses (default deny).
                self._write_halt_receipt(event)
                # RESTART SAFETY (Phase 6, code audit 2026-07-13 §3): on an
                # in-process HARD-class halt, DROP the needs_reconcile marker so a
                # bare restart is BLOCKED (HALT_NEEDS_RECONCILE) until the book is
                # reconciled against the exchange. Soft/manual halts do NOT need it.
                self.mark_reconcile_on_hard_halt(event)
                # TERMINAL caller (2026-07-26 gate, B2): `_stop.set()` below ends
                # the process, so there is no next maintenance tick for the
                # withdraw-resolver to inherit leftovers from — this pass must
                # attempt EVERY quote, waiting for the write bucket if need be.
                # `budget_s=None` keeps exactly the pre-bound behaviour; the wall
                # budget exists for the NON-terminal callers, which keep running.
                await lifecycle.cancel_all(event.reason, budget_s=None)
                self._stop.set()

            killswitch.on_halt(on_halt)
            killswitch.start_kill_file_watch()

            async def on_channel_lost(reason: str) -> None:
                await lifecycle.cancel_all(reason)
                await ws.force_reconnect()

            intake.on_channel_lost(on_channel_lost)

            ws.start()
            book_ws.start()  # dedicated order-book socket (see construction note)
            # Register the loops the external supervisor judges. Each declares
            # only its OWN cadence; the stall bound is DERIVED from the single
            # operator wedge-tolerance anchor (supervisor.heartbeat_timeout_s).
            # Nothing here is a hand-set threshold.
            wedge_timeout_s = self._config.supervisor.heartbeat_timeout_s
            self._progress.register(
                LOOP_MAINTENANCE,
                interval_s=MAINTENANCE_TICK_INTERVAL_S,
                wedge_timeout_s=wedge_timeout_s,
                # MEASURED: every completed inter-mark gap feeds the derived
                # stall wall (risk/stall_wall.py). The register-time bound
                # above is the wall's FLOOR — it can only loosen by measurement.
                measure_gaps=True,
            )
            await self._refresh_stall_wall(reason="boot")
            await self._refresh_expired_baseline(reason="boot")
            # NOTE — the 15s status loop is deliberately NOT a kill signal. It
            # already tolerates a 10s exchange GET plus a 15s enforcement budget
            # inside one tick, so any bound loose enough to be safe for it is
            # too loose to mean anything, and a tight one is a NEW false-kill
            # surface on the exact end-of-game path this rebuild exists to stop
            # false-killing. It publishes progress for OBSERVABILITY only (its
            # mark is a no-op while unregistered).
            self._progress.register(
                LOOP_QUOTE,
                # The quote path is event-driven: its "cadence" is the longest a
                # single RFQ may legitimately occupy a worker (the pool deadline
                # plus the dwell budget that bounds how stale an RFQ may be when
                # it starts). Both are existing quote-path numbers, not new ones.
                interval_s=POOL_DEADLINE_S + RFQ_MAX_QUEUE_DWELL_S,
                wedge_timeout_s=wedge_timeout_s,
                # IDLE IS NOT A STALL: with nothing queued, workers block on an
                # empty queue by design. Only a BACKED-UP queue that stops
                # draining is a wedge.
                idle=rfq_work.empty,
            )
            tasks = [
                # DEDICATED LIVENESS first: nothing this task does can be
                # delayed by the bot's work, which is the whole point.
                asyncio.create_task(self._liveness_loop(), name="liveness"),
                asyncio.create_task(retry_pending(), name="rfq-retry"),
                asyncio.create_task(quote_event_worker(), name="quote-event-worker"),
                *[
                    asyncio.create_task(rfq_worker(), name=f"rfq-worker-{i}")
                    for i in range(RFQ_WORKERS)
                ],
                asyncio.create_task(self._maintenance_loop(lifecycle), name="maintenance"),
                asyncio.create_task(
                    self._status_loop(
                        rest, lifecycle, killswitch, breakers, feed, exposure, metadata
                    ),
                    name="exchange-status",
                ),
                asyncio.create_task(
                    self._report_loop(
                        store, exposure, lifecycle, within_game_rho, balance_tracker
                    ),
                    name="report",
                ),
                asyncio.create_task(
                    self._balance_loop(rest, balance_tracker), name="balance-poll"
                ),
                asyncio.create_task(
                    self._adaptive_caps_loop(
                        rest,
                        limits,
                        cap_engine,
                        risk_cfg.adaptive_caps_mode,
                        risk_cfg.adaptive_caps_expected_games,
                        game_series,
                    ),
                    name="adaptive-caps",
                ),
                asyncio.create_task(
                    self._settlement_loop(settlement_poller), name="settlement-poll"
                ),
                asyncio.create_task(
                    self._reservation_reconcile_loop(rest, reservation),
                    name="reservation-reconcile",
                ),
                # RUNTIME POSITION-RECONCILE NET (2026-07-18; adoption
                # 2026-07-21): exchange-vs-book comparison every N minutes
                # (read-only GET) — no-context positions adopt as reserves.
                asyncio.create_task(
                    self._position_reconcile_loop(
                        rest, exposure, store, balance_tracker
                    ),
                    name="position-reconcile",
                ),
                # EXTERNAL-TRANSFER WATCH + startup account-standing line
                # (2026-07-21): deposits/withdrawals auto-adjust the SOD/peak
                # anchors — never a manual re-anchor.
                asyncio.create_task(
                    self._transfer_watch_loop(rest, balance_tracker, exposure),
                    name="transfer-watch",
                ),
            ]
            if external is not None:
                _, poller, sgo_client = external
                await sgo_client.__aenter__()
                tasks.append(asyncio.create_task(poller.run(), name="sgo-poller"))
            try:
                await self._stop.wait()
            finally:
                seed_task = getattr(self, "_acceptance_seed_task", None)
                if seed_task is not None:
                    seed_task.cancel()
                for task in tasks:
                    task.cancel()
                # BOUNDED SHUTDOWN (2026-07-27). Ordered, NAMED stages handed to
                # ``_shutdown``, which runs cancel-all UNBOUNDED first (the book
                # is always fully attempted) and everything below under ONE wall
                # bound derived from the operator wedge-tolerance anchor. See
                # ``_shutdown`` for the incident this replaces.
                stages: list[ShutdownStage] = [
                    ShutdownStage("ws_stop", ws.stop),
                    ShutdownStage("book_ws_stop", book_ws.stop),
                ]
                if joint_pool is not None:
                    stages.append(
                        ShutdownStage("joint_pool_shutdown", joint_pool.shutdown)
                    )
                if book_risk_pool is not None:
                    stages.append(
                        ShutdownStage(
                            "book_risk_pool_shutdown", book_risk_pool.shutdown
                        )
                    )
                stages += [
                    ShutdownStage("killswitch_stop", killswitch.stop),
                    # Let any in-flight alarm-only sweep finish before the store
                    # closes under it (2026-07-26): each is wall-bounded, so this
                    # cannot hold shutdown open, and it stops a half-done
                    # divergence check from vanishing into a "Connection closed".
                    ShutdownStage(
                        "diagnostic_sweep_drain",
                        lifecycle.drain_diagnostic_sweeps,
                        best_effort=True,
                    ),
                    # Tear down the external supervisor subprocess (best-effort).
                    ShutdownStage(
                        "supervisor_stop", self._stop_supervisor, best_effort=True
                    ),
                    ShutdownStage("store_close", store.close),
                ]
                completed = await self._shutdown(
                    # Crash-path discipline: best-effort cancel-all before exit.
                    # TERMINAL (see on_halt): the loops are already cancelled, so
                    # nothing will re-drive a deferred withdrawal — no wall budget.
                    cancel_all=lambda: lifecycle.cancel_all(
                        ReasonCode.HALT_MANUAL, budget_s=None
                    ),
                    stages=stages,
                )
                if completed:
                    log.info("quote_app_stopped", metrics=self._metrics.snapshot())

    def request_stop(self) -> None:
        self._stop.set()

    def _halt_class(self, event: HaltEvent) -> str:
        """Name the halt's CLASS from the evidence this tick recorded.

        The label is a convenience only — ``ops/relight.py`` gate G7 never
        trusts it and re-derives the same verdict from the evidence dict in the
        same file. So a wrong label here cannot make an unrelightable halt
        relightable; it can only be contradicted."""
        if str(event.reason) != str(ReasonCode.HALT_METADATA_CHANGE):
            return str(event.reason)
        if self._halt_tripwire is not None:
            return "taxonomy_tripwire"
        origins = {
            str(e.get("origin")) for e in self._halt_evidence.values()
        }
        if origins == {"quarantine_unenforced"}:
            return "lifecycle_quarantine_unenforced"
        if "settlement" in origins:
            return "settlement_metadata_change"
        if "unarmed" in origins:
            return "lifecycle_quarantine_unarmed"
        return "metadata_change_unclassified"

    def _root_cause_signature(self) -> str:
        """The halt's ROOT CAUSE, as ``origin:<sorted distinct failure kinds>``.

        Every input is measured at its own failure site: the origin by the
        metadata lane, the kinds by the withdrawal path
        (``lifecycle._withdraw_failure_kind``). The relighter's novelty bound
        (B2) keys on this, so what counts as "the same failure again" is decided
        by the exchange's behaviour, not by a threshold anyone can move."""
        origins = sorted({str(e.get("origin")) for e in self._halt_evidence.values()})
        kinds: set[str] = set()
        for entry in self._halt_evidence.values():
            raw = entry.get("withdraw_failure_kinds")
            if isinstance(raw, dict):
                kinds.update(str(k) for k in raw)
        return f"{'+'.join(origins) or 'none'}:{','.join(sorted(kinds)) or 'none'}"

    def _write_halt_receipt(self, event: HaltEvent) -> None:
        """Record WHY this process halted, as EVIDENCE plus a claim the evidence
        itself re-derives, for the out-of-process relighter (``ops/relight.py``).

        Written ATOMICALLY with the same primitive the heartbeat/marker use, and
        carrying the run nonce the relighter passed in by env: a receipt from a
        previous run, a second stack, or a hand-written file cannot match
        (relight gates G2/G3). No secrets, no credentials, no config — only the
        halt's own facts.

        NEVER RAISES. A receipt is diagnostics; ``cancel_all`` is the cure, and
        nothing about the cure may depend on this succeeding. A missing receipt
        just means the relighter refuses, which is the default anyway."""
        try:
            payload = {
                "schema_version": HALT_RECEIPT_SCHEMA_VERSION,
                "run_id": os.environ.get(RUN_ID_ENV, ""),
                # BOTH pids, because on Windows the venv's ``python.exe`` is a
                # LAUNCHER SHIM that re-spawns the real interpreter as a child
                # with an identical command line (the same fact start_all.ps1's
                # root-counting encodes: one launch = two processes). So the pid
                # the relighter spawned is our PARENT, not us. Recording both is
                # what lets gate G3 verify "you are the process I started"
                # without the relighter having to walk a process tree.
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "written_at": self._clock.now().isoformat(),
                "reason": str(event.reason),
                "verdict_detail": event.detail,
                "tripwire_hit": (
                    None if self._halt_tripwire is None else list(self._halt_tripwire)
                ),
                "halt_class": self._halt_class(event),
                "evidence": self._halt_evidence,
                "root_cause_signature": self._root_cause_signature(),
            }
            _atomic_write(self._halt_receipt_path, json.dumps(payload, default=str))
            log.error(
                "halt_receipt_written",
                path=str(self._halt_receipt_path),
                halt_class=payload["halt_class"],
                signature=payload["root_cause_signature"],
                markets=sorted(self._halt_evidence),
            )
        except Exception:
            log.exception("halt_receipt_write_failed", reason=str(event.reason))

    def mark_reconcile_on_hard_halt(self, event: HaltEvent) -> None:
        """RESTART SAFETY (Phase 6): drop the ``needs_reconcile`` marker on an
        in-process HARD-class halt so a bare restart is BLOCKED
        (HALT_NEEDS_RECONCILE) until the book reconciles against the exchange. The
        marker survives the restart on disk (like the KILL file), so an
        auto-restarter can't skip it. Soft/manual halts (a deliberate human stop,
        an exchange-status pause, a soft daily-loss cap) are NOT hard-class — they
        leave the marker alone so a normal restart resumes cleanly."""
        if event.reason not in _HARD_HALT_REASONS:
            return
        self._reconcile_marker.set(str(event.reason))
        self._book_reconciled = False
        log.error(
            "needs_reconcile_marker_dropped",
            reason=str(event.reason),
            detail="in-process hard halt — a restart must reconcile before quoting",
        )

    def _kill_file_present(self) -> bool:
        """True if the KILL file is on disk. Fail-closed: any stat error is
        treated as PRESENT (a filesystem we can't read is one we can't trust to
        say 'no kill')."""
        try:
            return self._config.kill_file.exists()
        except OSError:  # pragma: no cover - exotic FS failure ⇒ fail closed
            return True

    def _refuse_if_kill_file_present(self) -> None:
        """Synchronous KILL-file gate (Phase 6, CLAUDE.md fail-closed). The KILL
        file is written by the external supervisor (or a human) and SURVIVES a
        process restart on disk. If it is present at startup, the bot must refuse
        to run — do NOT rely solely on the async watcher (``start_kill_file_watch``
        polls ~1s and a revived bot could emit the first quote before it fires).
        Raises ``PreflightError`` (fail-closed refusal) so no code path reaches a
        quote. The operator clears a kill by REMOVING the KILL file deliberately."""
        kill_file = self._config.kill_file
        if self._kill_file_present():
            log.error(
                "kill_file_present_at_startup",
                kill_file=str(kill_file),
                detail="KILL file on disk — refusing to start; remove it to clear",
            )
            raise PreflightError(
                f"KILL file present at startup ({kill_file}) — the bot refuses to "
                "run until it is deliberately removed (kill switch survives restart)"
            )

    def _build_external_odds(self) -> tuple[Any, Any, Any] | None:
        """(source, poller, client) when enabled + key present, else None."""
        cfg = self._config.pricing.external_odds
        if not cfg.enabled:
            return None
        import os

        api_key = os.environ.get("SPORTSGAMEODDS_API_KEY", "").strip()
        if not api_key:
            log.warning("external_odds_enabled_but_no_key", var="SPORTSGAMEODDS_API_KEY")
            return None
        from combomaker.pricing.sources.sportsgameodds import (
            MappedLeg,
            SgoClient,
            SgoPoller,
            SportsGameOddsSource,
            StaticMarketMapping,
        )

        entries: dict[str, MappedLeg] = {}
        for ticker, spec in cfg.mapping.items():
            event_id, _, odd_id = spec.partition("|")
            if event_id and odd_id:
                entries[ticker] = MappedLeg(event_id=event_id, odd_id=odd_id)
        source = SportsGameOddsSource(
            StaticMarketMapping(entries), self._clock, max_age_s=cfg.max_age_s
        )
        client = SgoClient(api_key)
        poller = SgoPoller(
            client,
            source,
            leagues=cfg.leagues,
            poll_interval_s=cfg.poll_interval_s,
            max_events_per_league=cfg.max_events_per_league,
            devig_method=cfg.devig_method,
        )
        return source, poller, client

    async def _startup_reconcile(self, rest: KalshiRestClient) -> bool:
        """Exchange-first startup pass: cancel leftover resting quotes + observe
        existing positions. Returns True iff the pass PROVED the book — every
        leftover resting quote is provably off the wire AND the positions read
        answered. Anything less returns False so the caller keeps the
        ``needs_reconcile`` block in place.

        THE FAIL-OPEN SEAM THIS CLOSES (2026-07-27). This pass is the re-proof
        that no quote of ours is still resting on the exchange; the whole
        block-restart-until-reconciled chain, and every consequence scope-down
        that leans on it, is sound ONLY if "reconciled" means proven. It used to
        swallow a per-quote ``KalshiApiError`` with a warning and STILL log
        ``startup_reconciled`` and green the gate — so a 429 or a 503 on one
        DELETE left a live quote resting while the bot resumed quoting on a book
        it believed was empty. That quote can fill. Now: only an ACK or a 404
        (``already_gone`` — the exchange has no such quote) counts as proof;
        429 / 5xx / timeout / transport are UNRESOLVED, never proof.

        FAIL-CLOSED, WITH A BOUNDED RETRY FIRST. Both halves retry a bounded
        number of times (``RECONCILE_RETRIES``) and then STOP: an exchange that
        is simply unreachable at boot leaves the book unproven, the marker in
        force, ``_book_reconciled`` False, and — on prod — the preflight red,
        which raises ``PreflightError`` and exits the process. The bot stays
        DOWN; it never quotes against an unproven book. The retry exists only so
        one transient 429/503 on one DELETE does not need a human, which is the
        same proportionality the rest of this work is about. It cannot spin: the
        attempt count is a constant, each attempt is bounded by the REST client's
        own request timeout, the backoff is finite, and the withdrawal half is
        additionally capped by a wall deadline (see
        ``_withdraw_leftover_quotes``)."""
        try:
            # Enumerate leftover resting quotes via the SHARED bounded+retrying
            # helper (cursor-paginated, min_ts/max_ts windowed so it never trips
            # the exchange circuit-breaker with a full-history scan, 5xx-retried).
            # Same helper the supervisor's kill-path uses — see exchange/quote_query.
            leftover = await list_open_quotes(
                rest, int(self._clock.now().timestamp())
            )
        except Exception as exc:
            # BROAD on purpose: a timeout or a transport error is exactly the
            # "exchange unreachable" case this must fail closed on, and it is not
            # a KalshiApiError. (CancelledError is a BaseException and still
            # propagates, so shutdown is unaffected.)
            log.error(
                "startup_reconcile_failed",
                phase="enumerate",
                error=repr(exc),
                detail="could not enumerate open quotes — the book is UNPROVEN; "
                "quoting stays blocked",
            )
            return False
        quote_ids = open_quote_ids(leftover)
        unproven = await self._withdraw_leftover_quotes(rest, quote_ids)
        if unproven:
            tickers = open_quote_tickers(leftover)
            self._metrics.inc("startup_reconcile.unproven_quotes", len(unproven))
            log.error(
                "startup_reconcile_unproven_quotes",
                unproven=len(unproven),
                of=len(quote_ids),
                tickers=sorted({tickers.get(q) or q for q in unproven}),
                quote_ids=sorted(unproven),
                detail="these quotes could NOT be proven off the wire (only an ACK "
                "or a 404 is proof) — they may still be RESTING and may still FILL. "
                "The book is UNPROVEN: needs_reconcile stays in force and the bot "
                "refuses to quote.",
            )
            return False
        # P0-5: pin the positions read to our one subaccount (query-layer pin).
        try:
            positions = await rest.get_positions(subaccount=self._config.safety.subaccount)
        except Exception as exc:
            log.error(
                "startup_reconcile_failed",
                phase="positions",
                error=repr(exc),
                detail="quotes were withdrawn but the positions read failed — the "
                "exposure book cannot be rehydrated, so the book is UNPROVEN",
            )
            return False
        if positions.get("market_positions") or positions.get("positions"):
            log.info(
                "startup_existing_positions",
                detail="existing positions found — the exposure book is rehydrated "
                "from them next (_rehydrate_exposure_book, #33)",
            )
        log.info(
            "startup_reconciled",
            leftover_quotes=len(leftover),
            withdrawn=len(quote_ids),
        )
        return True

    async def _withdraw_leftover_quotes(
        self, rest: KalshiRestClient, quote_ids: Sequence[str]
    ) -> list[str]:
        """Withdraw the leftover resting quotes a restart found, and return the
        ones that are NOT PROVABLY off the wire.

        PROOF, narrowly (mirrors ``rfq.lifecycle._withdraw_batch``, the other
        withdrawal path, via the one shared ``already_gone``):

        - an ACK  ⇒ gone;
        - a 404   ⇒ gone (the exchange has no such quote — it cannot fill);
        - 429 / 5xx / timeout / transport ⇒ UNRESOLVED. The request may never
          have reached the book; the quote may still be resting. Retried, and if
          still unresolved when the attempts run out it is RETURNED — the caller
          fails the whole reconcile on it.

        TERMINATION (three independent bounds, all constants):
        1. at most ``RECONCILE_RETRIES`` passes — a constant range;
        2. every DELETE is bounded by the REST client's own request timeout;
        3. the whole withdrawal is bounded by a wall deadline of
           ``RECONCILE_RETRIES x request_timeout`` — so N leftover quotes against
           a hung exchange cost that deadline, not N x retries x timeout. Quotes
           past the deadline are simply never asked about, which is
           not-provably-gone, which fails closed.
        Worst case wall cost is therefore ``RECONCILE_RETRIES x timeout`` plus
        the finite backoff, independent of how many quotes were left over.

        Sequential on purpose: this is the boot path, not the quote path, and one
        DELETE in flight at a time is self-pacing against the write token bucket
        (the exact behaviour that shipped, unchanged)."""
        if not quote_ids:
            return []
        # No new number: the per-request wall bound the client already enforces,
        # times the shared attempt anchor. Read defensively so a Protocol-shaped
        # test double without the attribute falls back to the same constant.
        timeout_s = float(getattr(rest, "request_timeout_s", DEFAULT_REQUEST_TIMEOUT_S))
        deadline_ns = self._clock.monotonic_ns() + int(
            RECONCILE_RETRIES * timeout_s * 1e9
        )
        pending = list(quote_ids)
        for attempt in range(RECONCILE_RETRIES):
            still: list[str] = []
            for quote_id in pending:
                if self._clock.monotonic_ns() >= deadline_ns:
                    # Never asked ⇒ never proven. Fails closed, loudly, below.
                    still.append(quote_id)
                    continue
                try:
                    await rest.delete_quote(quote_id)
                except Exception as exc:
                    if already_gone(exc):
                        continue  # 404: provably off the wire
                    still.append(quote_id)
                    log.warning(
                        "startup_cancel_failed",
                        quote_id=quote_id,
                        attempt=attempt + 1,
                        of=RECONCILE_RETRIES,
                        error=repr(exc),
                    )
            pending = still
            if not pending:
                return []
            if attempt < RECONCILE_RETRIES - 1:
                await asyncio.sleep(RECONCILE_BACKOFF_S * (2**attempt))
        return pending

    async def _rehydrate_exposure_book(
        self,
        rest: KalshiRestClient,
        store: Store,
        exposure: ExposureBook,
        allowed_series: list[str] | None = None,
        subaccount: int | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        """#33 (over-book reconciliation gap) + P0-5 (exact exchange-quantity
        reconciliation): after a restart the in-memory exposure book starts EMPTY,
        so the risk caps (game/slate loss, mass-acceptance, the portfolio MC) can't
        see positions we still hold and the book would over-commit on top of live
        exposure. Rehydrate from the exchange's ACTUAL open positions.

        P0-5 — the exchange is AUTHORITATIVE for ticker/side/QUANTITY (position_fp);
        our local fills supply ONLY cost basis (entry price), legs, and provenance.
        We fold each position at the exchange's quantity, not the reconstructed
        local one. On a local/exchange MISMATCH — a size delta, an opposite side, or
        a manual/external holding with no local fill — we do NOT trust the local
        number: we reserve the LARGER exposure (max of exchange and local
        contracts), never a convenient smaller default, and tag it
        ``SKIP_RECONCILE_QUANTITY_MISMATCH`` so the caps bind conservatively and the
        divergence is diagnosable (defense #3). Settled/zero positions are excluded
        at the source (``open_combo_positions_from_positions`` drops position_fp==0).
        ``subaccount`` pins account truth to ONE subaccount AT THE QUERY LAYER —
        ``GET /portfolio/positions`` takes a ``subaccount`` query param (default 0 =
        primary; index-scan §portfolio) and returns ONLY that subaccount's
        positions, so another subaccount's holdings never enter the payload. This is
        the real pin (the ``MarketPosition`` schema carries no per-row subaccount
        field to filter on); we pass it straight to ``get_positions``.

        Best-effort: an unreachable exchange leaves the book empty — the
        conservative-but-blind state the prior code left SILENTLY; this only ever
        ADDS real positions. An exchange position with no local fill/rfq record has
        no legs (so it can't be clustered or its marginals modeled) — it is surfaced
        as an unmodeled reconciliation gap, never modeled from a guess (rule 6).

        ``allowed_series``: rehydrate ONLY positions whose every leg is on a quoted
        (allow-listed) series. A position on a GATED-OFF series (e.g. MLB while the
        allowlist is [KXWC]) has no subscribed leg books, so its marginals are
        unavailable — and a committed position with an unavailable marginal makes the
        exposure snapshot ``unknown_marginals`` on EVERY check, declining EVERY quote
        via SKIP_CLASSIFIER_UNKNOWN (verified live 2026-07-15: 2 rehydrated MLB
        positions blocked all WC quoting). P0-4: such positions are now RESERVED
        (``risk_modeled=False``) rather than skipped — their exact premium loss,
        gross settlement notional, and per-game concentration COUNT in the global
        deterministic/gross caps, but their (unavailable) marginals are never
        queried (no p=0.5) and they are held OUTSIDE the portfolio model ES, so
        their missing data cannot poison quote-eligible candidate decomposition or
        vanish from global capital accounting."""
        try:
            # P0-5: pin the positions read to ONE subaccount at the QUERY LAYER.
            # subaccount=None ⇒ the exchange default (0/primary); an int pins that
            # subaccount and the endpoint returns ONLY its positions.
            # PAGED (2026-07-27): this read was the last unpaginated
            # get_positions in the tree — at boot, past the endpoint's default
            # page size, the exposure book would silently come up missing its
            # tail and under-reserve real risk until the 5-minute reconcile.
            payload = await get_positions_paged(rest, subaccount=subaccount)
        except KalshiApiError as exc:
            log.warning("rehydrate_positions_failed", error=str(exc))
            return
        # EXCHANGE = authoritative side + quantity (P0-5); settled/zero excluded here.
        exch_by_ticker = open_combo_positions_from_positions(payload)
        if not exch_by_ticker:
            return
        # Exchange's own premium-at-risk per ticker — the ONLY input the
        # fail-CLOSED reserve below is built from when legs are unresolvable.
        exposure_cc_by_ticker, unreadable_exposure = _exchange_exposure_cc_by_ticker(payload)
        if unreadable_exposure:
            # OBSERVABLE, never silent (2026-07-26): an unreadable at-risk figure
            # is the precondition for the fail-open book. Positions adopted below
            # fall back to the $1.00/contract bound, never to zero.
            _bump(metrics, "exchange_exposure.unreadable")
            log.error(
                "exchange_exposure_unreadable",
                tickers=sorted(unreadable_exposure),
                raw_values=[unreadable_exposure[t] for t in sorted(unreadable_exposure)],
                detail="the exchange's own at-risk figure could not be parsed "
                "for these open positions at BOOT — any adopted below is booked "
                "at the $1.00/contract fail-closed upper bound, never at zero",
            )
        # DROP-SETTLED-ON-REHYDRATION (2026-07-16, clears the stale $4.46
        # reserve): a position row on a market whose Market.status says the
        # market is DEFINITIVELY SETTLED carries no live risk — folding it back
        # in reserves capital against a corpse until the settlement poller
        # happens to see it. Status vocabulary is the Market.status FIELD enum
        # (initialized|inactive|active|closed|determined|disputed|amended|
        # finalized — docs/api-notes/index-scan.md; the WS lifecycle notes map
        # `settled` → `finalized`, and `settled` is also accepted in case the
        # wire uses the filter-vocabulary spelling). ONLY those two drop:
        # closed/determined-but-unsettled keeps today's behaviour (the payout
        # has not landed — still real risk), and ANY error (unreachable market,
        # unreadable payload) KEEPS the position (fail-safe: risk we cannot
        # disprove stays in the caps).
        for ticker in list(exch_by_ticker):
            try:
                market_payload = await rest.get_market(ticker)
                market = market_payload.get("market", market_payload)
                status = str(market.get("status", "")).lower()
            except Exception as exc:  # noqa: BLE001 — any error keeps the position
                log.warning(
                    "rehydrate_market_status_unavailable",
                    ticker=ticker,
                    error=repr(exc),
                    detail="could not verify settlement status — position kept "
                    "(fail-safe)",
                )
                continue
            if status in ("finalized", "settled"):
                del exch_by_ticker[ticker]
                # WARNING not info (adversarial verify 2026-07-16): dropping
                # the position also skips the settlement poller's realized-P&L
                # booking into the daily-loss ledger AND the to-the-cent
                # settlement reconcile for this position this run — capital is
                # released, but the ledger side effect must be loud until a
                # startup-side reconcile pass exists.
                log.warning(
                    "rehydrate_dropped_settled",
                    ticker=ticker,
                    status=status,
                    detail="market definitively settled — position not "
                    "rehydrated; realized P&L NOT booked to the daily ledger "
                    "and the settlement reconcile is SKIPPED for it this run",
                )
        if not exch_by_ticker:
            return
        held = {h["combo_ticker"]: h for h in await store.held_positions(list(exch_by_ticker))}

        def _quoted(mt: str) -> bool:
            if allowed_series is None:
                return True
            series = mt.split("-", 1)[0]
            return any(series.startswith(p) for p in allowed_series)

        modeled: set[str] = set()
        reserved: set[str] = set()
        games: set[str] = set()
        mismatched: list[str] = []
        # Tickers for which THIS boot wrote the missing OPEN ledger row (the
        # historical-gap backfill — see the block below).
        ledger_backfilled: list[str] = []
        # Held positions whose LEGS no durable source can resolve — reserved
        # from exchange figures rather than dropped (BOOK COMPLETENESS).
        unknown_legs: list[str] = []
        # Reserved at the $1.00/contract fail-closed bound because the exchange's
        # own at-risk figure was unreadable (2026-07-26).
        notional_floored: list[str] = []
        legs_from_ledger: list[str] = []
        for ticker, exch in exch_by_ticker.items():
            h = held.get(ticker)
            if h is None:
                # FAIL-CLOSED BOOK COMPLETENESS (2026-07-26). No durable source
                # (position_ledger, then the rfqs tape) resolves this position's
                # legs — and nothing downstream ever will: the fill-recovery
                # sweep only re-models THIS run's own quotes, and the runtime
                # reconcile net used to hand the ticker to that sweep on the
                # mere presence of a fills row. So this ``continue`` dropped
                # REAL exchange premium out of the risk book entirely —
                # measured live 2026-07-26: 7 combos / $40.03, a 5.35%
                # understatement of deterministic_max_loss_cc and of every cap
                # that scales off it, on a book whose concentration was exactly
                # that same KXMLBKS strikeout family. Fail-OPEN cap integrity.
                # UNKNOWN LEGS MUST NEVER MEAN ZERO EXPOSURE: reserve it from
                # EXCHANGE figures through the same single code path the
                # runtime reconcile uses, so its premium counts in the
                # deterministic/gross caps even though its legs — and hence its
                # entity/family attribution — are unknowable. And a position
                # whose at-risk FIGURE is also unreadable is no longer dropped
                # either (2026-07-26 fail-open fix): it books at the PROVEN
                # $1.00/contract upper bound + alarms, because "we can't read
                # the price" must make the caps bind harder, never vanish.
                unknown = reserve_from_exchange_figures(
                    ticker,
                    exch.side,
                    exch.contracts_centi,
                    exposure_cc_by_ticker.get(ticker),
                )
                if unknown is None:
                    unknown = reserve_at_full_notional(
                        ticker, exch.side, exch.contracts_centi
                    )
                    if unknown is None:
                        continue  # non-positive count — genuinely nothing at risk
                    notional_floored.append(ticker)
                    _bump(metrics, "exchange_exposure.reserved_at_full_notional")
                    log.error(
                        "rehydrate_reserved_at_full_notional",
                        ticker=ticker,
                        contracts_centi=exch.contracts_centi,
                        raw_exposure=unreadable_exposure.get(ticker, ""),
                        reserved_max_loss_cc=unknown.max_loss_cc,
                        detail="unreadable exchange at-risk figure at BOOT — "
                        "booked at the $1.00/contract MAXIMUM POSSIBLE loss so "
                        "the caps bind harder, never at zero (fail-CLOSED)",
                    )
                exposure.add_position(unknown)
                unknown_legs.append(ticker)
                continue
            if h.get("legs_source") == "position_ledger":
                legs_from_ledger.append(ticker)
            legs = tuple(
                LegRef(
                    market_ticker=leg["market_ticker"],
                    event_ticker=leg.get("event_ticker"),
                    side=leg.get("side", "yes"),
                )
                for leg in h["legs"]
            )
            # P0-4: a position on a GATED-OFF series (no subscribed leg books →
            # unavailable marginals) is RESERVED, never dropped. We rehydrate EVERY
            # exchange-held position regardless of quote eligibility so its exact
            # premium loss, gross settlement notional, and per-game concentration
            # stay in the global deterministic/gross caps. ``risk_modeled=False``
            # marks it a conservatively-reserved holding: the exposure snapshot
            # never queries its (unavailable) marginals — so a missing marginal is
            # NEVER scored as an ordinary usable p=0.5 — and the portfolio MC holds
            # it OUTSIDE model ES as a deterministic reserve rather than sampling it
            # (build_book_model). Its missing data therefore cannot poison the
            # decomposition of unrelated (quote-eligible) candidates, and it cannot
            # vanish from global capital accounting.
            is_reserved = not all(_quoted(leg.market_ticker) for leg in legs)
            local_side = Side.NO if h["our_side"] == "no" else Side.YES
            local_ctr = int(h["contracts_centi"])
            entry_price_cc = int(h["entry_price_cc"])  # cost basis from local fills
            # P0-5 reconciliation: the exchange side/quantity are authoritative.
            # Reserve the LARGER exposure on ANY divergence (opposite side or a size
            # delta), never the convenient local number.
            side = exch.side
            contracts = max(local_ctr, exch.contracts_centi)
            reconcile_mismatch = local_side is not exch.side or local_ctr != exch.contracts_centi
            if reconcile_mismatch:
                mismatched.append(ticker)
            if is_reserved:
                prefix = "reserve"
            elif reconcile_mismatch:
                prefix = "reconcile"
            else:
                prefix = "rehydrate"
            position = OpenPosition(
                position_id=f"{prefix}:{ticker}",
                combo_ticker=ticker,
                collection=h["collection"],
                our_side=side,
                contracts=CentiContracts(contracts),
                entry_price_cc=CentiCents(entry_price_cc),
                legs=legs,
                risk_modeled=not is_reserved,
            )
            exposure.add_position(position)
            # DURABLE LEDGER IDENTITY — BOOT KEYSPACE CLOSURE (2026-07-26).
            # We just re-minted this position's id, so any settled write for
            # it would have to find its ledger row by the DURABLE key. Close
            # the keyspace here: if no OPEN row exists for (leg_set_hash,
            # combo_ticker, our_side) — the historical gap, every position
            # filled before the ledger writer existed — write one now under
            # the re-minted id. Stable-key gated, so a position that already
            # has its `fill:<quote_id>` open row is a NO-OP and the open-row
            # count never diverges from the open-position count.
            # Best-effort by construction: the ledger is diagnostics + the
            # p_night anchor, never the risk source of truth, so a store
            # failure logs and startup proceeds exactly as before.
            try:
                if await store.ensure_open_position_row(
                    position,
                    subaccount="" if subaccount is None else str(subaccount),
                ):
                    ledger_backfilled.append(ticker)
            except Exception as exc:  # noqa: BLE001 — never blocks startup
                log.warning(
                    "rehydrate_ledger_open_failed",
                    ticker=ticker,
                    error=repr(exc),
                )
            if is_reserved:
                reserved.add(ticker)
            else:
                modeled.add(ticker)
                games.update(game_key(leg.event_ticker) for leg in legs if leg.event_ticker)
        if reserved:
            log.info(
                "rehydrate_reserved_gated_series",
                count=len(reserved),
                detail="P0-4: positions on non-allow-listed series (no subscribed leg "
                "books → unavailable marginals) RESERVED into the risk book — exact "
                "premium loss / gross / per-game concentration COUNT in the "
                "deterministic + gross caps, held OUTSIDE model ES; never decomposed "
                "against marginals (no p=0.5), so they cannot poison quote-eligible "
                "candidate decomposition",
                tickers=sorted(reserved),
            )
        if mismatched:
            log.warning(
                "rehydrate_reconcile_mismatch",
                reason=str(ReasonCode.SKIP_RECONCILE_QUANTITY_MISMATCH),
                detail="exchange position (authoritative side/quantity) disagreed with "
                "the local fill reconstruction — reserved the LARGER exposure and "
                "tagged the position for manual reconciliation",
                tickers=sorted(mismatched),
            )
        if ledger_backfilled:
            log.info(
                "rehydrate_ledger_backfilled",
                count=len(ledger_backfilled),
                tickers=sorted(ledger_backfilled),
                detail="held positions with NO open position_ledger row (filled "
                "before the ledger writer existed) now have one under the "
                "re-minted id — their settlements can land durably",
            )
        if unknown_legs:
            log.warning(
                "rehydrate_unknown_legs_reserved",
                count=len(unknown_legs),
                tickers=sorted(unknown_legs),
                reserved_max_loss_cc=sum(
                    exposure.positions[f"reserve:{t}"].max_loss_cc
                    for t in unknown_legs
                ),
                full_notional_fallback=sorted(notional_floored),
                detail="held exchange positions whose LEGS no durable source "
                "(position_ledger, then the rfqs tape) could resolve — RESERVED "
                "from exchange figures so their premium counts in the "
                "deterministic/gross caps. Their legs are unknown, so they "
                "cannot be attributed to a per-entity or per-family axis: treat "
                "the entity/family caps as understated by this premium until "
                "the ledger covers them",
            )
        unmodeled = sorted(set(exch_by_ticker) - modeled - reserved - set(unknown_legs))
        log.info(
            "exposure_rehydrated",
            positions=len(modeled),
            reserved=len(reserved),
            games=sorted(games),
            unmodeled_open=len(unmodeled),
            reconcile_mismatches=len(mismatched),
            ledger_backfilled=len(ledger_backfilled),
            legs_from_ledger=len(legs_from_ledger),
            unknown_legs_reserved=len(unknown_legs),
            full_notional_fallback=len(notional_floored),
        )
        if unmodeled:
            log.warning(
                "rehydrate_unmodeled_positions",
                reason=str(ReasonCode.SKIP_RECONCILE_QUANTITY_MISMATCH),
                detail="open exchange positions with NO readable at-risk figure — "
                "NOT in the risk book (a figure is never invented, hard rule 6); "
                "reconcile manually before trusting the caps",
                tickers=unmodeled,
            )

    async def _startup_book_risk_snapshot(
        self,
        lifecycle: QuoteLifecycle,
        *,
        deadline_s: float = STARTUP_BOOK_RISK_DEADLINE_S,
    ) -> None:
        """STARTUP FIRST SNAPSHOT (2026-07-16 warmup fix). Compute ONE book-risk
        snapshot synchronously — called AFTER ``_rehydrate_exposure_book`` and
        BEFORE quote processing begins — so the first RFQs of a restarted bot
        are evaluated against a fresh portfolio-tail snapshot instead of
        failing closed on the never-measured book (69 skip_portfolio_cvar
        warmup declines in the first ~40s, report 2026-07-16-heartbeat-config-
        fix-and-cvar-usable-fix).

        REUSES the exact maintenance-path machinery
        (``recompute_book_risk_offloop`` → BookRiskPool worker when wired,
        inline otherwise — never a duplicate MC path), bounded by
        ``deadline_s``. On timeout or ANY error, startup proceeds exactly as
        today: the warmup declines return until the maintenance loop publishes
        the first snapshot — risk observability never blocks startup, and a
        failed snapshot is never faked (the CVaR cap keeps failing closed on
        the unmeasured book, the safe direction)."""
        try:
            await asyncio.wait_for(
                lifecycle.recompute_book_risk_offloop(), timeout=deadline_s
            )
        except Exception as exc:
            log.warning(
                "startup_book_risk_snapshot_failed",
                error=repr(exc),
                detail="first snapshot did not land inside the startup budget — "
                "proceeding as before (warmup declines until the maintenance "
                "loop publishes one)",
            )
            return
        log.info(
            "startup_book_risk_snapshot",
            detail="fresh book-risk snapshot computed before quote processing — "
            "first RFQs gate against a measured tail (no warmup fail-closed)",
        )

    async def _block_restart_until_reconciled(
        self, rest: KalshiRestClient, reservation: RiskReservationService
    ) -> None:
        """BLOCK-RESTART-UNTIL-RECONCILED (Phase 6). A ``needs_reconcile`` marker
        (dropped by a prior hard halt / supervisor kill and surviving the restart
        on disk) means the bot must reconcile its book against the exchange BEFORE
        it may quote. The exchange-first reconcile is the proof; only on success
        do we clear the marker and set ``_book_reconciled`` (the preflight gate).

        Fail-closed: if the reconcile does not PROVE the book — the exchange was
        unreachable, or a leftover resting quote could not be provably withdrawn
        (only an ACK or a 404 is proof) — the marker STAYS set and
        ``_book_reconciled`` STAYS false, so the preflight refuses to quote. A
        revived bot that can't reach the exchange, or that may still have a live
        quote resting on it, never resumes blind. Idempotent: no marker ⇒ a
        normal startup reconcile."""
        marker_present = self._reconcile_marker.is_set()
        if marker_present:
            log.warning(
                "needs_reconcile_marker_present",
                detail="a prior hard halt/supervisor kill requires an exchange "
                "reconcile before quoting resumes",
            )
        ok = await self._startup_reconcile(rest)
        if not ok:
            log.error(
                "startup_reconcile_incomplete",
                detail="book NOT proven (exchange unreachable, or a leftover quote "
                "could not be provably withdrawn — see the preceding "
                "startup_reconcile_failed / startup_reconcile_unproven_quotes "
                "event); the bot will refuse to quote (needs_reconcile stays in "
                "force)",
            )
            self._book_reconciled = False
            return
        # A KILL file outranks a successful reconcile: while it is on disk the
        # bot is deliberately stopped, so do NOT clear the needs_reconcile marker
        # or mark the book reconciled (that would let a later restart resume once
        # KILL is removed WITHOUT re-reconciling). The operator clears a kill by
        # removing KILL; the marker then clears on the next clean reconcile.
        # Defense-in-depth: run()'s synchronous gate already refuses to start
        # with KILL present, but this keeps the invariant local to the method.
        if self._kill_file_present():
            log.error(
                "reconcile_blocked_by_kill_file",
                detail="KILL file present — marker stays set, book stays unreconciled",
            )
            self._book_reconciled = False
            return
        # Exchange-first reconcile against the exchange's ACTUAL open positions
        # (not an empty set): map GET /portfolio/positions → the reservation ids
        # the exchange confirms open, so any stale/unconfirmed reservation is
        # committed-or-released against the ledger, never left leaking headroom. On
        # a fresh service this is a no-op (nothing outstanding); it becomes load-
        # bearing on the periodic reconcile after a confirm timeout.
        await self._reconcile_reservations(rest, reservation)
        self._reconcile_marker.clear()
        self._book_reconciled = True
        log.info("book_reconciled", detail="startup reconcile complete; quoting unblocked")

    async def _reconcile_reservations(
        self, rest: KalshiRestClient, reservation: RiskReservationService
    ) -> None:
        """Reconcile outstanding risk reservations against the exchange's ACTUAL
        open positions (RISK_BUILD_PLAN Phase 3; code audit 2026-07-13 §3
        "reconcile(real positions)"). Fetches ``GET /portfolio/positions``, maps it
        to ``{combo_ticker: Side}``, and commits the reservations the exchange
        confirms open / releases the ones it does not — so a confirm-timeout
        ``mark_unconfirmed`` reservation is RESOLVED instead of leaking headroom
        until restart.

        Called from the maintenance loop (periodic) AND from the startup pass.
        Best-effort: a failed positions poll leaves reservations outstanding (still
        counting against the caps — the conservative direction), retried next
        tick. No reservations outstanding ⇒ a no-op that skips the network call."""
        if reservation.outstanding_count == 0:
            return
        # P0-5: pin the positions read to our one subaccount (query-layer pin).
        positions = await rest.get_positions(subaccount=self._config.safety.subaccount)
        open_by_ticker = open_combo_tickers_from_positions(positions)
        backed = reservation_ids_backed_by_exchange(
            reservation.outstanding_positions(), open_by_ticker
        )
        outcome = reservation.reconcile(backed)
        if outcome.committed or outcome.released:
            log.info(
                "reservations_reconciled_with_exchange",
                committed=outcome.committed,
                released=outcome.released,
                open_tickers=len(open_by_ticker),
            )

    async def _launch_supervisor(self) -> None:
        """Launch the external SafetySupervisor as a SEPARATE OS subprocess so its
        kill path survives the bot's own host deadlocking (an in-process watcher
        can't). It runs ``python -m combomaker.ops.supervisor --env <env>`` with
        the SAME data_dir (so it finds the bot's heartbeat, KILL, and reconcile
        marker at the shared paths) and beats its OWN heartbeat, which the prod
        preflight then verifies (external_kill_reachable).

        The supervisor loads its OWN env-only KALSHI_SUPERVISOR_* credential; when
        that credential is ABSENT the supervisor runs KILL-only (it still writes
        KILL on a wedge — the credential-free half — but has no cancel path) and
        logs a loud warning. We emit the warning bot-side too so a missing kill
        credential is impossible to miss.

        The subprocess inherits the bot's environment (secrets stay env-only,
        never passed on the command line, never logged). Idempotent-safe: only one
        is launched per run; failure to launch logs and leaves _supervisor_proc
        None (the preflight's external_kill_reachable then fails closed, refusing
        to quote on prod — a missing watcher is never waved through)."""
        if not supervisor_credential_configured():
            log.warning(
                "supervisor_launch_no_credential",
                detail=(
                    f"{ENV_SUPERVISOR_API_KEY_ID} / "
                    f"{ENV_SUPERVISOR_PRIVATE_KEY_PATH}|{ENV_SUPERVISOR_PRIVATE_KEY_PEM} "
                    "absent — supervisor will run KILL-only (no cancel path); the "
                    "prod preflight external_kill_reachable gate will refuse to quote"
                ),
            )
        cmd = supervisor_launch_cmd(self._config)
        try:
            self._supervisor_proc = await asyncio.create_subprocess_exec(*cmd)
        except OSError as exc:
            log.error("supervisor_launch_failed", error=repr(exc))
            self._supervisor_proc = None
            return
        log.info(
            "supervisor_launched",
            pid=self._supervisor_proc.pid,
            env=str(self._config.env),
            has_credential=supervisor_credential_configured(),
        )

    async def _await_supervisor_heartbeat(self) -> None:
        """Give the freshly-launched supervisor subprocess a bounded moment to
        write its FIRST heartbeat before the preflight grades external_kill_
        reachable — otherwise a genuinely-launched watcher would race the gate and
        the bot would (wrongly) refuse to start. Bounded (never blocks forever); if
        the beat never lands, the preflight simply fails closed as it should (a
        watcher that can't even beat once is not a working kill path). Skipped when
        the launch didn't produce a process."""
        if self._supervisor_proc is None:
            return
        path = supervisor_heartbeat_path(self._config.data_dir)
        # Wait for the heartbeat to be (re)written AFTER launch, not merely to
        # exist. A stale file from a PRIOR (now-dead) supervisor must not
        # short-circuit this: it would return on mere existence while the
        # preflight grades external_kill_reachable on FRESHNESS and (correctly)
        # fails red — the stale-file race that blocked a full-tree cold restart
        # 2026-07-14. Baselining the pre-launch mtime makes "a NEW beat landed"
        # the release condition; a pre-existing LIVE supervisor still releases on
        # its very next beat (~0.1s), so the healthy path is unchanged.
        try:
            baseline_mtime_ns = path.stat().st_mtime_ns if path.exists() else -1
        except OSError:  # pragma: no cover - exotic FS failure
            baseline_mtime_ns = -1
        deadline_beats = 50  # ~5s at 0.1s cadence — well inside a 1s poll launch
        for _ in range(deadline_beats):
            if self._supervisor_proc.returncode is not None:
                log.error(
                    "supervisor_exited_before_heartbeat",
                    returncode=self._supervisor_proc.returncode,
                )
                return
            try:
                if path.exists() and path.stat().st_mtime_ns > baseline_mtime_ns:
                    return
            except OSError:  # pragma: no cover - exotic FS failure
                pass
            await asyncio.sleep(0.1)
        log.warning(
            "supervisor_heartbeat_not_established",
            detail="supervisor did not beat within the startup window — preflight "
            "external_kill_reachable will fail closed",
        )

    async def _shutdown(
        self,
        *,
        cancel_all: Callable[[], Awaitable[object]],
        stages: Sequence[ShutdownStage],
        budget_s: float | None = None,
        exit_fn: Callable[[int], None] | None = None,
    ) -> bool:
        """Flatten the book, THEN tear down under ONE wall bound.

        THE INCIDENT (2026-07-27T10:17Z). The last line this process ever wrote
        was ``joint_pool_stopped`` at 10:17:18.39; ``book_risk_pool_stopped`` and
        ``quote_app_stopped`` appear ZERO times in that log and the next boot was
        32.6 minutes later. Money exposure was already gone — 4.2s earlier the
        same bot withdrew all 67 quotes in 218ms — so 32.6 of the 33.4 minutes of
        downtime was pure teardown hang AFTER the book was flat. An unbounded
        shutdown converts a solved problem into an outage.

        TWO PROPERTIES, in this order:

        1. THE BOOK FIRST, UNBOUNDED. ``cancel_all`` runs OUTSIDE the bound on
           purpose: no deadline may ever cut short the attempt to flatten. It
           completes in ~250ms measured, long before the bounded region starts.
        2. EVERYTHING AFTER, BOUNDED. The stages run under a single
           ``asyncio.wait_for`` whose T is ``supervisor.heartbeat_timeout_s`` —
           the EXISTING operator wedge-tolerance anchor, no new number. On expiry
           we log ``shutdown_timed_out`` naming the last COMPLETED stage and
           ``os._exit(1)``. Each stage emits a ``shutdown_step`` line, so the
           next hang names itself instead of vanishing.

        WHY A SECOND, THREADED DEADLINE: ``asyncio.wait_for`` can only interrupt
        an ``await``. A stage that blocks the event loop synchronously would make
        the async bound itself unreachable — exactly the failure mode where a
        bound matters most. A daemon timer on the SAME T (still no new number)
        carries the guarantee in that case; whichever fires first wins, and the
        log line is written exactly once.

        RESIDUAL RISK, ACCEPTED. A hard exit can leave the SQLite WAL
        uncheckpointed and pool workers orphaned. Both are already the steady
        state — one session logged 390 ``store_writer_checkpoint_failed``, SQLite
        recovers a WAL on next open by design, and ``ops/process_group.py`` reaps
        orphaned pool workers (plus the pools' own KILL_ON_JOB_CLOSE handles).
        Money exposure at that point is zero by property (1).

        Returns True if the teardown completed within the bound. On timeout it
        does not return at all in production (``os._exit``); ``exit_fn`` is
        injectable so tests can observe the hard exit without dying.
        """
        # (1) THE BOOK, UNBOUNDED.
        try:
            await cancel_all()
        except Exception:
            log.exception("shutdown_cancel_all_failed")

        # (2) EVERYTHING ELSE, BOUNDED. No new literal: the same anchor the
        # supervisor judges a wedge by is the operator's stated tolerance for a
        # process that has stopped making progress.
        bound_s = (
            budget_s
            if budget_s is not None
            else self._config.supervisor.heartbeat_timeout_s
        )
        hard_exit = exit_fn if exit_fn is not None else os._exit
        fired = threading.Event()
        last_completed = ["cancel_all"]

        def _give_up(source: str) -> None:
            if fired.is_set():  # pragma: no cover - both deadlines racing
                return
            fired.set()
            log.error(
                "shutdown_timed_out",
                last_step=last_completed[0],
                budget_s=bound_s,
                source=source,
            )
            hard_exit(1)

        async def _teardown() -> None:
            for stage in stages:
                try:
                    result = stage.run()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if not stage.best_effort:
                        raise
                    log.exception(f"{stage.name}_failed")
                last_completed[0] = stage.name
                log.info("shutdown_step", step=stage.name)

        watchdog = threading.Timer(bound_s, _give_up, args=("watchdog",))
        watchdog.daemon = True
        watchdog.start()
        try:
            await asyncio.wait_for(_teardown(), timeout=bound_s)
        except TimeoutError:
            _give_up("asyncio")
            return False
        finally:
            # Both deadlines sit on the SAME T (one number, by design), so they
            # can race. The join makes the outcome deterministic: if the watchdog
            # won, we do not return until its hard exit has actually been issued
            # — in production it never returns, because ``os._exit`` ends the
            # process from that thread. If it never fired, ``cancel`` wakes the
            # timer immediately and the join is free.
            watchdog.cancel()
            watchdog.join()
        return not fired.is_set()

    async def _stop_supervisor(self) -> None:
        """Terminate the supervisor subprocess on shutdown. Best-effort: SIGTERM
        (terminate), then a bounded wait, then kill. A supervisor that already
        exited is a no-op."""
        proc = self._supervisor_proc
        if proc is None:
            return
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:  # pragma: no cover - already gone
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:  # pragma: no cover - stubborn child
            proc.kill()
            await proc.wait()
        log.info("supervisor_stopped", returncode=proc.returncode)

    def _run_prod_preflight(self) -> None:
        """PROD GO-LIVE PREFLIGHT (Phase 6). Every live go-live condition must be
        green before the first quote. On demo this is a no-op (no real money);
        on prod any red gate raises ``PreflightError`` and the bot refuses to
        start. Fail-closed: an unestablished condition is red.

        The supervisor gates check that (a) the bot has beaten its heartbeat at
        least once (the file the external supervisor reads exists) and (b) the
        external kill path is reachable — a supervisor process is RUNNING and
        RECENTLY BEATING its own heartbeat AND its dedicated cancel credential is
        present. (b) is deliberately stronger than mere credential presence: a
        credential with no watcher running is a DEAD kill path (the shadow-process
        gap the audit flagged). So the external kill can actually fire before we
        risk a cent."""
        config = self._config
        if config.env is not Env.PROD:
            return
        # The bot writes its first heartbeat here so the supervisor has a file to
        # watch from t=0 (rather than a gap until the first maintenance tick),
        # and refreshes the progress ledger alongside it for the same reason.
        self._heartbeat.beat()
        self._progress.publish()
        heartbeat_established = self._heartbeat.path.exists()
        # external_kill_reachable requires a LIVE, recently-beating supervisor
        # (not just a configured credential) — verified against the supervisor's
        # OWN heartbeat file. Fail-closed: no running watcher ⇒ red.
        kill_reachable = supervisor_heartbeat_reachable(
            config.data_dir,
            self._clock,
            max_age_s=config.supervisor.heartbeat_timeout_s,
        )
        conditions = PreflightConditions(
            limits_configured=config.safety.prod_limits_configured,
            whitelist_non_empty=bool(config.filters.allowed_leg_series_prefixes),
            supervisor_heartbeat_established=heartbeat_established,
            external_kill_reachable=kill_reachable,
            book_reconciled=self._book_reconciled,
        )
        result = evaluate_preflight(
            conditions, require_supervisor=config.safety.prod_require_supervisor
        )
        if not result.green:
            log.error("prod_preflight_red", red_gates=list(result.red_gates))
            raise PreflightError(
                "prod go-live preflight failed — red gates: "
                + ", ".join(result.red_gates)
                + " (the bot refuses to quote until every gate is green)"
            )
        log.info("prod_preflight_green", detail="all go-live gates green")

    async def _ensure_watched(
        self, rfq: Rfq, feed: OrderbookFeed, metadata: MetadataCache
    ) -> None:
        # Only subscribe book feeds (+ fetch metadata) for combos we COULD quote.
        # A combo with any leg outside the series allowlist WILL decline
        # (SKIP_SERIES_NOT_ALLOWED, ~half of all declines), so subscribing its
        # legs' books floods us with irrelevant deltas (WNBA/ATP/UFC/crypto legs
        # in cross-category RFQs) → we fall behind → Kalshi slow-consumer-kills the
        # socket (~90s write-dead loop live 2026-07-13, capping distinct books at
        # ~5 → most quotable combos then decline stale). Skip watching entirely;
        # the decline is still recorded cheaply downstream (series check needs no
        # book). Legs shared with an all-allowed combo get watched when THAT
        # arrives, so no quotable leg is missed.
        allowed = self._config.filters.allowed_leg_series_prefixes
        if allowed is not None and any(
            not t.startswith(tuple(allowed)) for t in rfq.leg_tickers
        ):
            return
        new = [t for t in rfq.leg_tickers if t not in self._watched]
        if new:
            self._watched.update(new)
            feed.watch(new)
        # LEG metadata: legs are SHARED real markets, so a fetch caches and is
        # reused across combos — no per-RFQ storm. Keyed on the CACHE being
        # empty, NOT on first sighting (2026-07-21 review, HIGH): gating on
        # ``new`` made a 429'd fetch permanent — the ticker was already in
        # ``_watched`` so the fetch never retried, and post-restart a
        # committed leg without metadata loses its pregame start resolution
        # (⇒ the in-play watch exemption silently stands down and the halt
        # storm returns). peek-None retries on every RFQ naming the leg.
        # 2026-07-25 review (HIGH): a WARM-LOADED (persisted) entry makes
        # peek() non-None, so the peek-only gate would NEVER revalidate it —
        # a stale later close_time could feed the in-play gate for the
        # market's whole life and blind the metadata-change breaker.
        # needs_revalidation() (cached but TTL-expired) counts as a fetch
        # miss: pricing stays warm off peek(), while one spaced async
        # refresh per stale leg heals status/close/expiry.
        for ticker in rfq.leg_tickers:
            if metadata.peek(ticker) is not None and not metadata.needs_revalidation(
                ticker
            ):
                continue
            try:
                meta = await metadata.market(ticker)
                if meta.event_ticker:
                    await metadata.event(meta.event_ticker)
            except ReadBudgetExhausted:
                # LOCAL refusal, not an exchange error: the read bucket is spent
                # for this instant. The leg stays uncached, so the very next RFQ
                # naming it retries once the bucket refills — the same
                # retry-until-cached behaviour, minus the 429 and minus the
                # round trip. Counted, never logged per-RFQ (a per-refusal log
                # would just be the 5,726-line storm in a different colour).
                self._metrics.inc("metadata.read_budget_deferred")
                break
            except KalshiApiError as exc:
                log.warning("metadata_fetch_failed", ticker=ticker, error=str(exc))
        # COMBO market grid: the combo ticker is UNIQUE per RFQ, so fetching it per
        # combo blew the read-rate budget (429 storm, 2026-07-14). Every combo in a
        # collection shares one grid, so fetch ONCE per collection and inject the
        # cached grid for the rest (no per-combo fetch, no combo-event fetch — the
        # engine only needs the grid). Only the FIRST unseen combo of a collection
        # hits the network.
        combo = rfq.market_ticker
        if metadata.peek(combo) is None:
            collection = rfq.mve_collection_ticker
            cached = self._collection_grid.get(collection) if collection else None
            if cached is not None:
                metadata.put_combo_grid(combo, cached)
            else:
                try:
                    meta = await metadata.market(combo)
                    if meta.grid is not None and collection:
                        self._collection_grid[collection] = meta.grid
                except ReadBudgetExhausted:
                    self._metrics.inc("metadata.read_budget_deferred")
                except KalshiApiError as exc:
                    log.warning("metadata_fetch_failed", ticker=combo, error=str(exc))

    async def _sleep_for_read_budget(self) -> None:
        """Sleep until the READ bucket could pay for one metadata GET.

        WAIT ONLY — it does not spend; the caller's own fetch does that. The
        sleep is the bucket's OWN ``seconds_until``, never a poll interval
        somebody picked. No budget wired (paper/backtests) ⇒ returns at once."""
        budget = self._read_budget
        if budget is None:
            return
        wait_s = budget.seconds_until(DEFAULT_ENDPOINT_TOKEN_COST)
        if wait_s <= 0.0 or wait_s == float("inf"):
            return
        await asyncio.sleep(wait_s)

    async def _reserve_read_token(self) -> None:
        """Wait for AND SPEND one metadata GET's worth of read tokens, at the
        CRITICAL priority tier.

        For callers that reach the exchange directly (not through
        ``MetadataCache``, which spends for itself): they must charge the same
        bucket or the pacing is fiction.

        CRITICAL (2026-07-27): this is the settled-marginal resolver's path —
        low volume, correctness-critical, and the loser of every race against
        the continuous metadata refresh (live: 27 settled_fetch_failed, all
        429). It spends with ``critical=True`` so it may draw the bucket's
        reserve, which the routine metadata tier can never touch. Settlement
        resolution therefore cannot be starved by refresh volume.

        BOUNDED: the bucket is not FIFO, so a hot path continuously draining it
        could in principle starve a waiter forever. The wait is therefore capped
        at ONE FULL REFILL of the bucket (``budget.refill_s`` — the bucket's own
        property, not a picked number); past that we raise the same
        ``ReadBudgetExhausted`` the cache raises, and the caller's existing
        fetch-failed backoff owns the retry. In practice contention is
        self-correcting: pacing lets the metadata cache actually FILL, which is
        what collapses the miss rate that caused the contention."""
        budget = self._read_budget
        if budget is None:
            return
        cost = DEFAULT_ENDPOINT_TOKEN_COST
        deadline = self._clock.monotonic_ns() + int(budget.refill_s * 1e9)
        while not budget.try_spend(cost, critical=True):
            wait_s = budget.seconds_until(cost, critical=True)
            if wait_s == float("inf") or self._clock.monotonic_ns() >= deadline:
                self._metrics.inc("metadata.read_budget_deferred")
                raise ReadBudgetExhausted("GET /markets", cost)
            await asyncio.sleep(max(wait_s, 0.0))

    def _tier_clamped_write_budget(self) -> WriteBudget:
        """The withdrawal write bucket, CLAMPED to the observed tier ceiling.

        The operator knob (``supervisor.write_budget_capacity`` /
        ``write_budget_refill_s``, 200 tokens / 10 s = 20 tok/s) is a
        deliberately conservative sub-allocation of the account's write bucket,
        and it stays the number the operator moves. But it is a number a human
        set, so it can be set ABOVE what the account can actually spend — and a
        budget that admits more than the exchange does is not a budget. Both the
        sustained rate AND the burst capacity are clamped to the OBSERVED bucket
        (``ApiTierLimits.clamp_write_budget``) and any clamp is alarmed. This is
        the "derive from measured state, never a hand-set number" rule applied
        to the ceiling: a tier downgrade under a running bot re-paces the wave
        automatically, and an UNREADABLE tier paces it as the smallest bucket."""
        sup = self._config.supervisor
        tier = self._api_tier
        capacity, refill_s = tier.clamp_write_budget(
            sup.write_budget_capacity, sup.write_budget_refill_s
        )
        if (capacity, refill_s) != (
            sup.write_budget_capacity,
            sup.write_budget_refill_s,
        ):
            log.error(
                "write_budget_clamped_to_tier",
                usage_tier=tier.usage_tier,
                tier_observed=tier.observed,
                configured_capacity=sup.write_budget_capacity,
                configured_tokens_per_s=(
                    sup.write_budget_capacity / sup.write_budget_refill_s
                ),
                tier_capacity=tier.write_capacity,
                tier_tokens_per_s=tier.write_refill_per_s,
                clamped_capacity=capacity,
                clamped_refill_s=refill_s,
                detail="configured write budget exceeds the account's OBSERVED "
                "write bucket — clamped to the tier (a budget that admits more "
                "than the exchange does is not a budget)",
            )
        else:
            log.info(
                "write_budget_within_tier",
                usage_tier=tier.usage_tier,
                tier_observed=tier.observed,
                capacity=capacity,
                tokens_per_s=capacity / refill_s,
                tier_capacity=tier.write_capacity,
                tier_tokens_per_s=tier.write_refill_per_s,
            )
        return WriteBudget.create(
            self._clock, capacity=capacity, refill_s=refill_s
        )

    async def _liveness_loop(self) -> None:
        """DEDICATED LIVENESS (2026-07-26 rebuild). This task's ONLY job is to
        prove the process and its event loop are still scheduling: sleep, beat,
        publish, repeat. It does no exchange I/O, holds no lock, and touches no
        risk state, so it cannot be slowed by anything the bot is doing — which
        is precisely the point. A slow maintenance pass is no longer
        indistinguishable from a dead process.

        It also publishes the PROGRESS ledger, i.e. how long each real loop has
        gone without completing an iteration. That is the signal that keeps this
        from BLINDING the supervisor: if the event loop breathes while the quote
        or maintenance loop is genuinely stuck, the ledger ages and the
        supervisor still kills — now naming the stalled loop.

        Cadence = the supervisor's own poll interval (beating faster than the
        watcher reads buys nothing), capped at a QUARTER of the wedge tolerance
        so three consecutive failed writes still cannot age the file past it.
        Both inputs are existing supervisor config; no new number, and a
        misconfigured poll interval can never starve the beat below the
        tolerance it is judged against.

        NEVER let a write failure end this task (2026-07-26 morning: two live
        runs died when a Windows ``os.replace`` PermissionError escaped the
        beat and ENDED the loop that owned it — after which no beat ever landed
        again and the supervisor killed a healthy bot 30s later). Swallow +
        log + metric: a genuinely stuck disk still ages the file on disk and
        still trips the (correct) wedged kill, so fail-closed is preserved."""
        sup = self._config.supervisor
        interval_s = max(
            min(sup.poll_interval_s, sup.heartbeat_timeout_s / 4.0), 0.05
        )
        while True:
            try:
                self._heartbeat.beat()
            except Exception:
                self._metrics.inc("heartbeat.beat_failed")
                log.exception("heartbeat_beat_failed")
            try:
                self._progress.publish()
            except Exception:
                self._metrics.inc("progress.publish_failed")
                log.exception("progress_publish_failed")
            await asyncio.sleep(interval_s)

    async def _maintenance_loop(self, lifecycle: QuoteLifecycle) -> None:
        while True:
            await asyncio.sleep(MAINTENANCE_TICK_INTERVAL_S)
            # PROGRESS, not liveness (2026-07-26). This loop used to beat the
            # heartbeat itself, which made "the maintenance pass is slow"
            # indistinguishable from "the process is dead" — the exact
            # confusion that emergency-killed a healthy, quoting bot at
            # 20:12:54Z when an end-of-game lifecycle wave put the tick in a
            # ~30s non-beating await. Liveness is now the dedicated
            # ``_liveness_loop``'s job; this loop only reports that it is
            # ADVANCING. A tick that stops advancing past its derived bound
            # (supervisor.heartbeat_timeout_s + this loop's cadence — the same
            # age the old signal died at) still escalates, by name.
            self._progress.mark(LOOP_MAINTENANCE)
            try:
                await lifecycle.maintenance_tick()
            except Exception:
                log.exception("maintenance_tick_failed")
            # PERSISTENT METADATA CACHE flush (2026-07-25): ~every 60s (120
            # ticks × 0.5s) when new metadata arrived. The SNAPSHOT is built
            # ON the loop (cheap dict walks — iterating the live dicts from a
            # thread would race refresh()); only the file write runs off-loop
            # (to_thread) so a slow disk can never stall pricing. Failure
            # logs inside the writer and retries next interval.
            self._metadata_persist_ticks += 1
            if (
                self._metadata_persist_ticks >= 120
                and self._metadata_cache is not None
                and self._metadata_cache.dirty
            ):
                self._metadata_persist_ticks = 0
                try:
                    payload = self._metadata_cache.build_persist_payload()
                    written = await asyncio.to_thread(
                        write_persist_payload, self._metadata_cache_path, payload
                    )
                    if written < 0:
                        # Failed write: the dirty flag was cleared at
                        # snapshot time — re-arm it so the next interval
                        # retries (2026-07-25 review).
                        self._metadata_cache.mark_dirty()
                except Exception:
                    log.exception("metadata_cache_persist_errored")
                    self._metadata_cache.mark_dirty()
            # DERIVED OPEN-QUOTE CAPACITY (2026-07-31): ~every 60s (120
            # ticks x 0.5s — the metadata-flush cadence precedent; the
            # window IS the rate-measurement denominator). "off" = never
            # runs, byte-identical. Errors log and keep current limits
            # (fix isolation: this slow-loop derivation must never reach
            # the pricing path).
            self._capacity_probe_ticks += 1
            if (
                self._capacity_probe_ticks >= 120
                and str(self._config.risk.open_quote_capacity_derived) != "off"
            ):
                self._capacity_probe_ticks = 0
                try:
                    self._derived_capacity_tick()
                except Exception:
                    log.exception("open_quote_capacity_tick_errored")
            # DERIVED STALL WALL refresh (2026-09-05): same ~60s cadence (120
            # ticks x 0.5s — the metadata-flush precedent). Folds this boot's
            # measured gaps into the tape, re-derives, publishes the bound the
            # supervisor reads. Errors log and keep the current bound (never
            # tighter than the floor; never reaches the pricing path).
            self._stall_wall_ticks += 1
            if self._stall_wall_ticks >= 120:
                self._stall_wall_ticks = 0
                await self._refresh_stall_wall(reason="refresh")
                await self._refresh_expired_baseline(reason="refresh")

    async def _refresh_stall_wall(self, *, reason: str) -> None:
        """Derive the maintenance loop's stall wall from the MEASURED completed
        inter-mark gaps (this boot's histogram + the retained boots' tape) and
        publish the APPLIED bound through the progress ledger — the supervisor
        reads it from ``loop_progress.json`` and needs no change.

        Rule (risk/stall_wall.py — the hang watchdog's rule, in the bot):
        ``wall = max(floor, MARGIN * Q_Φ(5)(gaps))`` with the floor being the
        register-time bound (``supervisor.heartbeat_timeout_s`` + the loop's
        cadence, 60.5 s live). MODE (review fix 2026-09-05, must-fix #1):
        ``supervisor.stall_wall_derived`` "shadow" (default) derives and LOGS
        but applies the floor — the loosening branch can only act under
        degradation and whether the wall may loosen at all is an operator
        ruling; "on" applies the derived wall. Gaps tainted by a timed-out
        store await were never recorded (``ProgressLedger.taint``).

        OFF-LOOP I/O (should-fix #1): the live histogram is COPIED here (it is
        mutated by ``mark`` on this loop) and the glob + stat of every
        ``live_*.log``, the tape read and the atomic write run in a worker
        thread — the metadata flush precedent, so the store's disk can never
        stall this loop. Logged as ``stall_wall_derivation`` at boot and on
        every refresh. Any failure logs and keeps the current bound (the
        floor at worst)."""
        floor_s = self._config.supervisor.heartbeat_timeout_s + MAINTENANCE_TICK_INTERVAL_S
        mode = str(self._config.supervisor.stall_wall_derived)
        try:
            live = self._progress.gap_histogram(LOOP_MAINTENANCE)
            hist = None if live is None else live.copy()
            tainted = self._progress.tainted_gaps(LOOP_MAINTENANCE)
            derivation = await asyncio.to_thread(
                refresh_stall_wall,
                tape_path=gap_tape_path(self._config.data_dir),
                data_dir=self._config.data_dir,
                boot_key=self._boot_key,
                boot_started_at_ts=self._boot_started_at_ts,
                this_boot=hist,
                bucket_s=MAINTENANCE_TICK_INTERVAL_S,
                floor_s=floor_s,
            )
            applied_s = derivation.wall_s if mode == "on" else derivation.floor_s
            self._progress.set_stall_after(LOOP_MAINTENANCE, applied_s)
            self._stall_wall = derivation
            self._stall_wall_applied_s = applied_s
            boot_fields: dict[str, object] = {}
            if hist is not None and hist.n > 0:
                # This boot's own pass evidence, so the tape of log lines
                # carries the distribution even if the JSON tape is lost.
                boot_fields = {
                    "boot_n_gaps": hist.n,
                    "boot_p50_s": round(hist.quantile(0.50), 3),
                    "boot_p99_s": round(hist.quantile(0.99), 3),
                    "boot_max_gap_s": round(hist.max_s, 3),
                }
            log.info(
                "stall_wall_derivation",
                reason=reason,
                loop=LOOP_MAINTENANCE,
                mode=mode,
                applied_wall_s=round(applied_s, 3),
                applied_sub_step_bound_s=round(applied_s / derivation.margin, 3),
                boot_tainted_gaps=tainted,
                **derivation.as_log(),
                **boot_fields,
            )
        except Exception:
            log.exception("stall_wall_derivation_failed", reason=reason, floor_s=floor_s)

    async def _refresh_expired_baseline(self, *, reason: str) -> None:
        """EXPIRED-ACCEPT RATE baseline (review should-fix #5,
        risk/confirm_expired_rate.py): fold this boot's
        ``confirm.expired_by_exchange`` / ``confirm.sent`` counters into the
        per-boot tape (same retention as the gap tape: the oldest
        ``live_*.log``), and hold the pooled counts of the OTHER retained
        boots for the lifecycle's ``confirm_expired_rate_anomalous`` alarm.
        File I/O off-loop; failures log and keep the current baseline."""
        try:
            expired = self._metrics.counter("confirm.expired_by_exchange")
            confirmed = self._metrics.counter("confirm.sent")
            retain = await asyncio.to_thread(oldest_live_log_mtime, self._config.data_dir)
            baseline = await asyncio.to_thread(
                refresh_expired_baseline,
                tape_path=expired_tape_path(self._config.data_dir),
                boot_key=self._boot_key,
                boot_started_at_ts=self._boot_started_at_ts,
                boot_expired=expired,
                boot_confirmed=confirmed,
                retain_since_ts=retain,
            )
            changed = baseline != self._expired_baseline
            self._expired_baseline = baseline
            if reason == "boot" or changed:
                log.info(
                    "confirm_expired_baseline",
                    reason=reason,
                    boot_expired=expired,
                    boot_confirmed=confirmed,
                    baseline_expired=None if baseline is None else baseline[0],
                    baseline_confirmed=None if baseline is None else baseline[1],
                    baseline_boots=None if baseline is None else baseline[2],
                )
        except Exception:
            log.exception("confirm_expired_baseline_failed", reason=reason)

    def _confirm_expired_baseline(self) -> tuple[int, int, int] | None:
        return self._expired_baseline

    def _applied_stall_wall_s(self) -> float | None:
        """The wall the supervisor currently kills the maintenance loop at
        (the ledger's bound — floor in shadow mode, derived when "on")."""
        return self._progress.stall_after_s(LOOP_MAINTENANCE)

    def _sub_step_bound_s(self) -> float | None:
        """The maintenance loop's per-sub-step store bound from the latest
        derivation: the APPLIED wall / MARGIN (``floor / MARGIN`` = 30.25 s
        live in shadow mode; the measured healthy upper quantile when the
        operator has ruled ``stall_wall_derived: on``). None before the first
        derivation ⇒ the lifecycle falls back to ``STORE_OP_TIMEOUT_S``."""
        if self._stall_wall is None or self._stall_wall_applied_s is None:
            return None
        return self._stall_wall_applied_s / self._stall_wall.margin

    def _derived_capacity_tick(self) -> None:
        """DERIVED OPEN-QUOTE CAPACITY (2026-07-31) — one ~60s derivation.

        Dissolves the hand-bumped ``max_open_quotes`` (20 -> 60 -> 120 -> 200)
        into the write bucket's own bound on a standing book (the full
        derivation, its exchange-doc verification and the mass-acceptance
        guard argument live in ``rfq/eviction_value.py``):

            capacity = min(
                (OBSERVED tier write rate
                 - kill/withdraw reserve rate
                 - MEASURED new-quote token rate)
                    x QUOTE_TTL_S / (create + delete cost),   # exchange tier
                (withdraw budget rate
                 - MEASURED delete-share of new-quote flow)
                    x QUOTE_TTL_S / delete cost,     # bot's own delete budget
            )

        The second form is the G1 repair (adversarial gate 2026-08-01):
        every delete flows through the SAME tier-clamped withdraw budget
        this method passes to the lifecycle (``_tier_clamped_write_budget``
        -> ``QuoteLifecycle._withdraw_budget`` -> ``_spend_withdraw_tokens``),
        so the standing book is bounded by the delete path too — today at
        ~200, exactly the hand cap the derivation dissolves.

        Modes (``risk.open_quote_capacity_derived``): "off" — never called;
        "shadow" — derive + LOG ``open_quote_capacity`` beside the enforced
        cap, enforcement unchanged; "on" — a USABLE derivation replaces
        ``max_open_quotes`` on the live checker via ``set_limits`` (the
        derived_cap_engine seam; every other field kept as-is, so an
        adaptive-caps swap is preserved). FAIL-CLOSED: the first window after
        boot (no rate sample), an unusable tier, or a derivation that cannot
        admit one quote all keep TODAY'S configured cap. Errors log and keep
        current limits — the adaptation is repaired, never silently widened."""
        mode = str(self._config.risk.open_quote_capacity_derived)
        now_ns = self._clock.monotonic_ns()
        sent = self._metrics.counter("quote.sent")
        prev = self._capacity_probe_prev
        self._capacity_probe_prev = (now_ns, sent)
        flow_tokens_per_s: float | None = None
        if prev is not None:
            prev_ns, prev_sent = prev
            dt_s = (now_ns - prev_ns) / 1e9
            if dt_s > 0:
                # Every sent quote eventually costs its delete too; reprices
                # re-register as sent quotes, so the standing book's own
                # refresh is double-counted here — strictly CONSERVATIVE
                # (capacity understated, never overstated).
                flow_tokens_per_s = (
                    max(0, sent - prev_sent)
                    * (CREATE_QUOTE_TOKEN_COST + DELETE_QUOTE_TOKEN_COST)
                    / dt_s
                )
        sup = self._config.supervisor
        reserve_capacity, reserve_refill_s = self._api_tier.clamp_write_budget(
            sup.write_budget_capacity, sup.write_budget_refill_s
        )
        # G1 (2026-08-01): the "reserve" carved out of the tier form IS the
        # bot's own metered withdraw budget — the ONE conduit every delete
        # spends (``_tier_clamped_write_budget`` sizes the SAME clamped knob
        # into ``QuoteLifecycle._withdraw_budget``). Pass it as BOTH the tier
        # form's reserve and the withdraw form's sustained rate so the
        # derivation is bounded by the bucket that actually pays deletes.
        withdraw_rate_per_s = reserve_capacity / reserve_refill_s
        fallback = int(self._config.risk.max_open_quotes)
        derived = derive_open_quote_capacity(
            tier_write_rate_per_s=float(self._api_tier.write_refill_per_s),
            reserve_rate_per_s=withdraw_rate_per_s,
            withdraw_rate_per_s=withdraw_rate_per_s,
            measured_flow_tokens_per_s=flow_tokens_per_s,
            ttl_s=QUOTE_TTL_S,
            refresh_cost_tokens=CREATE_QUOTE_TOKEN_COST + DELETE_QUOTE_TOKEN_COST,
            delete_cost_tokens=DELETE_QUOTE_TOKEN_COST,
            fallback=fallback,
        )
        checker = self._limit_checker
        enforced_now = (
            int(checker.limits.max_open_quotes) if checker is not None else None
        )
        log.info(
            "open_quote_capacity",
            mode=mode,
            usable=derived.usable,
            reason=derived.reason,
            derived_capacity=derived.capacity,
            enforced_max_open_quotes=enforced_now,
            tier_write_rate_per_s=derived.tier_write_rate_per_s,
            tier_observed=self._api_tier.observed,
            reserve_rate_per_s=derived.reserve_rate_per_s,
            withdraw_rate_per_s=derived.withdraw_rate_per_s,
            measured_flow_tokens_per_s=derived.measured_flow_tokens_per_s,
            ttl_s=derived.ttl_s,
            refresh_cost_tokens=derived.refresh_cost_tokens,
            delete_cost_tokens=derived.delete_cost_tokens,
            tier_capacity=derived.tier_capacity,
            withdraw_capacity=derived.withdraw_capacity,
            fallback=derived.fallback,
        )
        if (
            mode == "on"
            and derived.usable
            and checker is not None
            and enforced_now != derived.capacity
        ):
            checker.set_limits(
                replace(checker.limits, max_open_quotes=derived.capacity)
            )
            log.info(
                "open_quote_capacity_applied",
                previous=enforced_now,
                applied=derived.capacity,
            )

    async def _balance_loop(
        self, rest: KalshiRestClient, tracker: BalanceTracker
    ) -> None:
        """Poll the exchange balance so the R2 %-of-bankroll caps have a fresh
        risk-bankroll denominator. A failed/stale poll leaves the last good
        reading to age out ⇒ the caps fail closed (they never quote off a guessed
        bankroll). Shadow in Phase 2, so a dark poll has zero quote impact today —
        but the poll keeps the shadow numbers honest on the tape."""
        source = WholeBookBalanceSource(rest)
        while True:
            try:
                await tracker.refresh(source)
            except RateLimitedError as exc:
                self._rate_limit_window.record()  # feed the 429-burst breaker
                log.warning("balance_poll_rate_limited", error=str(exc))
            except StaleBalanceError as exc:
                log.warning("balance_poll_stale", error=str(exc))
            except Exception as exc:
                log.warning("balance_poll_failed", error=repr(exc))
            await asyncio.sleep(BALANCE_POLL_INTERVAL_S)

    def _refresh_adaptive_caps_once(
        self,
        limits: LimitChecker,
        cap_engine: DerivedCapEngine,
        mode: str,
        expected_games: int,
    ) -> None:
        """One correlation-adaptive cap derivation → log → (enforce) swap.

        Derives the deploy + halt caps from measured per-game vol / cross-game rho
        and, in ``enforce``, swaps them onto the LimitChecker (``set_limits``); in
        ``shadow`` it only LOGS the derived caps beside the enforced ones so the
        operator can watch it derive against a live slate with zero enforcement
        change. FAIL-SAFE: any error keeps the current limits (the adaptation is
        repaired, never silently widened on a bug).

        Bootstrap regime: no per-game P&L history is wired yet (the DB
        reconstruction that feeds the sensor is the fast-follow) so history is
        empty ⇒ the conservative provisional caps (slate 0.15), and the book caps
        sit at their bootstrap floor until the projected-book MC is hooked in (also
        fast-follow). Both fast-follows only ever let the caps BREATHE WIDER with
        evidence; their absence just holds the safe bootstrap."""
        try:
            new_limits, caps, est = cap_engine.refresh(
                expected_games=expected_games,
                pnl_history=[],
            )
            cur = limits.limits
            # Startup alarm (spec): a mismatched (f_slate, kill_anchor) pair is
            # visible BEFORE capital deploys. Healthy = kill_sigma_multiple >= k_trip
            # (5) and kill_prob_60n ~ 0; a self-destructing config lights this up.
            if caps.kill_prob_60n > 0.10 or caps.kill_sigma_multiple < 4.0:
                log.warning(
                    "adaptive_caps_kill_mismatch",
                    kill_sigma_multiple=caps.kill_sigma_multiple,
                    kill_prob_60n=caps.kill_prob_60n,
                    detail="KILL sits too few sigma above the daily swing — the "
                    "(f_slate, kill_anchor) pair will self-destruct; check config",
                )
            log.info(
                "adaptive_caps_refresh",
                mode=mode,
                provisional=caps.provisional,
                measured=caps.measured,
                stable=est.stable,
                g_eff=est.g_eff,
                kill_sigma_multiple=caps.kill_sigma_multiple,
                kill_prob_60n=caps.kill_prob_60n,
                ratchet_held=caps.ratchet_held,
                slate_frac=str(new_limits.slate_loss_frac),
                game_frac=str(new_limits.game_loss_frac),
                per_combo_frac=str(new_limits.per_combo_loss_frac),
                daily_frac=str(new_limits.daily_loss_frac),
                drawdown_frac=str(new_limits.drawdown_frac),
                hard_trip_frac=str(new_limits.hard_trip_frac),
                directional_frac=str(new_limits.directional_frac),
                det_max_frac=str(new_limits.portfolio_det_max_frac),
                cvar_frac=str(new_limits.portfolio_cvar_frac),
                enforced_slate_before=str(cur.slate_loss_frac),
                expected_games=expected_games,
            )
            if mode == "enforce":
                limits.set_limits(new_limits)
        except Exception:
            log.exception("adaptive_caps_refresh_failed")  # keep current limits

    async def _count_slate_games(
        self, rest: KalshiRestClient, game_series: tuple[str, ...]
    ) -> int | None:
        """Distinct games in tonight's live slate = distinct ``game_key``s across
        the OPEN markets of the allowed GAME series — the fully-adaptive source for
        expected_games (the per-game cap divisor ``game = slate / expected_games``).
        Returns None on any error / empty result so the caller falls back to the
        config bootstrap estimate; NEVER blocks or crashes the refresh (a slow-loop
        read must never reach the pricing path — fix-isolation rule)."""
        games: set[str] = set()
        try:
            for series in game_series:
                cursor = ""
                for _ in range(20):
                    params: dict[str, str | int] = {
                        "series_ticker": series,
                        "status": "open",
                        "limit": 1000,
                    }
                    if cursor:
                        params["cursor"] = cursor
                    payload = await rest.get_markets(**params)
                    for m in payload.get("markets") or []:
                        ev = m.get("event_ticker")
                        if ev:
                            games.add(game_key(ev))
                    cursor = str(payload.get("cursor") or "")
                    if not cursor:
                        break
        except Exception:
            log.warning("adaptive_caps_slate_count_failed", exc_info=True)
            return None
        return len(games) or None

    async def _adaptive_caps_loop(
        self,
        rest: KalshiRestClient,
        limits: LimitChecker,
        cap_engine: DerivedCapEngine | None,
        mode: str,
        expected_games_fallback: int,
        game_series: tuple[str, ...],
    ) -> None:
        """PERIODIC re-derivation of the correlation-adaptive caps (North Star).
        The FIRST derivation runs at startup (in run(), before the RFQ workers can
        quote) so the DERIVED caps — not the looser static config caps that sit
        underneath as a fallback — bind the very first fill. This loop re-derives
        nightly as the P&L sensor accumulates whole nights AND re-counts the live
        slate (expected_games); the caps change slowly, so a coarse tick suffices."""
        if cap_engine is None:
            return  # mode=off — the static config fracs enforce, unchanged
        while True:
            await asyncio.sleep(ADAPTIVE_CAPS_REFRESH_S)
            eg = await self._count_slate_games(rest, game_series)
            self._refresh_adaptive_caps_once(
                limits, cap_engine, mode, eg or expected_games_fallback
            )

    async def _settlement_loop(self, poller: SettlementPoller) -> None:
        """Poll GET /portfolio/settlements and book+reconcile each settled
        position we HOLD (realized P&L → the enforced daily-loss cap; to-the-cent
        mismatch → HALT_RECONCILIATION_MISMATCH). Idempotent per position, so a
        re-poll never double-books. Errors retry next interval; a real mismatch
        HALTs inside the handler (the loop then stops with the app). A fresh
        paper/demo start with no positions is a pure no-op — demo is unaffected."""
        while True:
            try:
                await poller.poll_once()
            except RateLimitedError as exc:
                self._rate_limit_window.record()
                log.warning("settlement_poll_rate_limited", error=str(exc))
            except Exception as exc:
                log.warning("settlement_poll_failed", error=repr(exc))
            await asyncio.sleep(SETTLEMENT_POLL_INTERVAL_S)

    async def _arm_rehydrated_legs(
        self, exposure: ExposureBook, feed: OrderbookFeed, metadata: MetadataCache
    ) -> None:
        """Watch + fetch metadata for every rehydrated position leg at startup
        (2026-07-21 review): committed legs must have their start times
        resolvable BEFORE any RFQ flow arrives, or the in-play watch
        exemption (estimate tier needs metadata anchors) cannot protect them.
        Self-legs of reserved holdings (leg ticker == combo ticker) are
        skipped — they have no start ladder and no book to watch. Failures
        log and retry via ``_ensure_watched``'s peek-None rule."""
        tickers = sorted(
            {
                leg.market_ticker
                for pos in exposure.positions.values()
                for leg in pos.legs
                if leg.market_ticker != pos.combo_ticker
            }
        )
        if not tickers:
            return
        new = [t for t in tickers if t not in self._watched]
        if new:
            self._watched.update(new)
            feed.watch(new)
        # Same warm-cache revalidation rule as _ensure_watched (2026-07-25
        # review HIGH): a persisted, TTL-expired entry counts as a fetch
        # miss — a rehydrated COMMITTED leg's start/close resolution must
        # come off fresh metadata, not last run's.
        for ticker in tickers:
            if metadata.peek(ticker) is not None and not metadata.needs_revalidation(
                ticker
            ):
                continue
            # SLOW PATH ⇒ WAIT for read tokens, never refuse (2026-07-26). This
            # startup pass must eventually succeed — a committed leg without
            # metadata loses its pregame start resolution — and it is not
            # latency-critical, so it pays the bucket's own refill wait instead
            # of storming it. (The RFQ hot path does the opposite: refuse
            # instantly, retry on the next RFQ.) That is also what turns a boot
            # burst into a paced trickle rather than the 2,319-failure storm.
            for _ in range(_READ_BUDGET_WAIT_ATTEMPTS):
                try:
                    meta = await metadata.market(ticker)
                    if meta.event_ticker:
                        await metadata.event(meta.event_ticker)
                except ReadBudgetExhausted:
                    await self._sleep_for_read_budget()
                    continue  # bucket was empty (or lost the race) — re-ask
                except KalshiApiError as exc:
                    log.warning(
                        "rehydrated_leg_metadata_fetch_failed",
                        ticker=ticker,
                        error=str(exc),
                    )
                break
        log.info(
            "rehydrated_legs_armed",
            legs=len(tickers),
            newly_watched=len(new),
        )

    async def _transfer_watch_loop(
        self,
        rest: KalshiRestClient,
        tracker: BalanceTracker,
        exposure: ExposureBook,
    ) -> None:
        """External-transfer watcher + startup account-standing line
        (2026-07-21, operator: the bot must 100% know its standing/balance at
        all times, with NO manual anchor updates).

        First pass (shortly after start, once the first balance poll has
        landed): BASELINE — every already-terminal deposit/withdrawal is
        seeded WITHOUT applying (its cash is already in the balance the
        anchors formed on) and one ``account_standing`` line reports the
        exchange-truth standing: applied deposits/withdrawals, cash, equity,
        modeled positions, pending receivables. Every later pass: a NEWLY
        terminal transfer adjusts the SOD/peak anchors by exactly its delta
        via ``apply_external_transfer`` — a mid-session deposit is not
        profit, a withdrawal is not a give-back. Fetch errors retry next
        interval (anchors untouched — fail-safe: an unobserved transfer means
        halts read conservative, never loose)."""
        statuses: dict[str, str] = {}
        await asyncio.sleep(TRANSFER_WATCH_STARTUP_DELAY_S)
        first = True
        while True:
            try:
                # The baseline needs the anchors to EXIST (the ordering rule
                # compares finalized_ts against the anchor instant) — until the
                # first successful balance poll, defer (review F6: a failed
                # first pass must not widen the mis-baseline window).
                anchor_ms = tracker.anchor_wall_ms_or_none()
                if first and anchor_ms is None:
                    await asyncio.sleep(TRANSFER_WATCH_INTERVAL_S)
                    continue
                deposits = await _page_portfolio(rest.get_deposits, "deposits")
                withdrawals = await _page_portfolio(rest.get_withdrawals, "withdrawals")
                deltas = new_external_transfer_deltas(
                    statuses,
                    deposits,
                    withdrawals,
                    baseline_before_ms=anchor_ms if first else None,
                )
                for kind, ref, delta_cc, finalized_ms in deltas:
                    tracker.apply_external_transfer(
                        delta_cc, kind=kind, ref=ref, finalized_wall_ms=finalized_ms
                    )
                if first:
                    first = False
                    dep_cc = sum(
                        (int(d.get("amount_cents") or 0) - int(d.get("fee_cents") or 0))
                        * 100
                        for d in deposits
                        if str(d.get("status")) == _TRANSFER_APPLIED
                    )
                    wd_cc = sum(
                        (int(w.get("amount_cents") or 0) + int(w.get("fee_cents") or 0))
                        * 100
                        for w in withdrawals
                        if str(w.get("status")) == _TRANSFER_APPLIED
                    )
                    log.info(
                        "account_standing",
                        applied_deposits_cc=dep_cc,
                        applied_withdrawals_cc=wd_cc,
                        available_cash_cc=_int_or_none(
                            tracker.available_cash_cc_or_none()
                        ),
                        exchange_equity_cc=_int_or_none(
                            tracker.exchange_equity_cc_or_none()
                        ),
                        modeled_positions=len(exposure.positions),
                        pending_receivables_cc=tracker.pending_receivables_cc(),
                        detail="startup exchange-truth standing; historical "
                        "transfers baselined (already inside the balance)",
                    )
            except RateLimitedError as exc:
                self._rate_limit_window.record()
                log.warning("transfer_watch_rate_limited", error=str(exc))
            except Exception as exc:
                log.warning("transfer_watch_failed", error=repr(exc))
            await asyncio.sleep(TRANSFER_WATCH_INTERVAL_S)

    async def _position_reconcile_loop(
        self,
        rest: KalshiRestClient,
        exposure: ExposureBook,
        store: Store,
        balance: BalanceTracker | None = None,
    ) -> None:
        """Periodic position-reconcile net (2026-07-18 requirement 3; adoption
        2026-07-21): every ``risk.position_reconcile_interval_s`` (default
        5 min) compare the exchange's open positions against the book —
        no-local-context positions ADOPT as conservatively-reserved holdings
        from exchange figures, recovery-owned ones alarm, flat reserves
        release (see ``position_reconcile_unmodeled_once``). Sleeps FIRST so
        the startup rehydrate/reconcile pass finishes before the first
        comparison; errors retry next interval."""
        interval_s = self._config.risk.position_reconcile_interval_s
        while True:
            await asyncio.sleep(interval_s)
            try:
                await position_reconcile_unmodeled_once(
                    rest,
                    exposure,
                    store,
                    self._metrics,
                    subaccount=self._config.safety.subaccount,
                    balance=balance,
                )
            except RateLimitedError as exc:
                self._rate_limit_window.record()
                log.warning("position_reconcile_rate_limited", error=str(exc))
            except Exception as exc:
                log.warning("position_reconcile_failed", error=repr(exc))

    async def _reservation_reconcile_loop(
        self, rest: KalshiRestClient, reservation: RiskReservationService
    ) -> None:
        """Periodically reconcile outstanding risk reservations against the
        exchange's ACTUAL open positions, so a confirm-timeout mark_unconfirmed
        reservation is committed-or-released instead of leaking headroom until
        restart. Skips the network entirely when nothing is outstanding, so a
        fresh paper/demo start with no reservations is a pure no-op."""
        while True:
            try:
                await self._reconcile_reservations(rest, reservation)
            except RateLimitedError as exc:
                self._rate_limit_window.record()
                log.warning("reservation_reconcile_rate_limited", error=str(exc))
            except Exception as exc:
                log.warning("reservation_reconcile_failed", error=repr(exc))
            await asyncio.sleep(RESERVATION_RECONCILE_INTERVAL_S)

    async def _status_loop(
        self,
        rest: KalshiRestClient,
        lifecycle: QuoteLifecycle,
        killswitch: KillSwitch,
        breakers: CircuitBreakers,
        feed: OrderbookFeed,
        exposure: ExposureBook,
        metadata: MetadataCache,
    ) -> None:
        while True:
            self._progress.mark(LOOP_STATUS)
            try:
                status = await rest.get_exchange_status()
                active = bool(status.get("exchange_active")) and bool(
                    status.get("trading_active", True)
                )
                lifecycle.exchange_active = active
                if not active:
                    await lifecycle.cancel_all(ReasonCode.HALT_EXCHANGE_STATUS)
            except RateLimitedError as exc:
                self._rate_limit_window.record()
                log.warning("exchange_status_rate_limited", error=str(exc))
                lifecycle.exchange_active = False
            except Exception as exc:
                log.warning("exchange_status_failed", error=repr(exc))
                lifecycle.exchange_active = False
            # Phase 6 circuit breakers, evaluated off the hot path. A trip halts
            # the kill switch (cancel-all + stop via on_halt). Fail-closed inside
            # ``evaluate`` — a detector that can't run trips HALT_BREAKER_ERROR.
            try:
                inputs = self._sample_breaker_inputs(feed, lifecycle, exposure, metadata)
                # SCOPED metadata response, carried out BEFORE the breakers are
                # evaluated so a market quarantined by this very sample has its
                # resting quotes pulled milliseconds later — not a tick later.
                # Never raises; an enforcement it could not complete escalates
                # to the whole-bot halt on the next sample.
                await self._enforce_market_quarantine(lifecycle)
                await breakers.evaluate_and_halt(inputs)
            except Exception:
                log.exception("breaker_evaluation_failed")
            # Throughput observability: joint-memo hit rate + off-loop pool
            # counters (the Phase-3/4 decision signal). Off the hot path.
            try:
                log.info("pricing_stats", **lifecycle.pricing_stats())
            except Exception:
                log.exception("pricing_stats_log_failed")
            await asyncio.sleep(STATUS_TICK_INTERVAL_S)

    def _sample_breaker_inputs(
        self,
        feed: OrderbookFeed,
        lifecycle: QuoteLifecycle,
        exposure: ExposureBook,
        metadata: MetadataCache,
    ) -> BreakerInputs:
        """Snapshot the live signals the circuit breakers evaluate off the hot
        path. Each field is a REAL measurement:

        - ``rx_age_s`` / ``feed_warm``: the feed's freshness age plus its warmth
          latch. While the feed is cold (no first frame yet), ``feed_warm=False``
          exempts the data-staleness breaker so a slow initial WS connect can't
          self-halt the bot before it quotes; once warm, a disconnect (rx_age
          None) still fails closed.
        - ``seq_gap``: the feed's ACTUAL in-stream sequence-gap event since the
          last sample (``pop_seq_gap`` — return-and-clear), NOT WS traffic
          silence. A genuine gap means the mirror is provably wrong until
          re-synced.
        - ``latency_ms``: the worst confirm round-trip in a RECENT window (not
          the all-time histogram max — one historical slow confirm must not latch
          the human-only kill switch forever). None ⇒ no recent sample ⇒ the
          spike breaker clears (nothing current to judge).
        - ``rate_limit_count``: the rolling 429-burst window (polls AND writes).
        - ``marginals``: the CURRENT per-leg P(YES) for every leg the risk path
          touches (legs of every open quote + open position), from the SAME
          marginal provider the pricer/exposure use (feed first, settled-fact
          cache second). The coordinator diffs each against its own last-seen
          baseline ⇒ ``detect_marginal_jump`` fires on a real move (and on a
          leg that became unreadable after we priced it) — EXCEPT the
          ``settled_tickers`` set: legs whose market the exchange confirmed no
          longer live are exempt from the jump/readability watch (a settled
          book leaving the feed is normal and permanent — the 2026-07-18
          02:17Z live halt).
        - ``game_keys``: the resolved ``pricing.grouping.game_key`` for each of
          those legs ⇒ ``detect_unmapped_game`` fires on a None/unresolved key
          (a leg that would escape the game/slate cluster caps).
        - ``tripwire_hit`` / ``changed_markets``: the taxonomy tripwire re-run
          over the legs in the book + a settlement-relevant metadata diff of the
          same markets tick-over-tick ⇒ ``detect_metadata_change`` fires if a
          pinned-impossible shape became constructible or a market's
          close_time/status/settlement metadata changed under us.

        Fail-closed by construction: a leg on the risk path whose marginal can't
        be read surfaces as ``None`` (jump breaker trips), and an event_ticker
        we can't resolve surfaces as a ``None`` game key (unmapped breaker trips)
        — UNKNOWN is never a convenient pass. Runs off the hot path (status loop,
        15s cadence), never in the 0.5s maintenance/status hot path.
        """
        marginals, game_keys, book_legs, settled, inplay = self._book_leg_signals(
            exposure, lifecycle
        )
        # The taxonomy tripwire shares HALT_METADATA_CHANGE with the metadata
        # lanes and OUTRANKS them in ``detect_metadata_change``. Record it so the
        # halt receipt can say which of the two fired — reason code alone can
        # never attribute (relight gate G7 refuses any non-null tripwire).
        self._halt_tripwire = self._book_tripwire(self._book_leg_refs(exposure))
        return BreakerInputs(
            rx_age_s=feed.rx_age_s,
            feed_warm=feed.warm,
            seq_gap=feed.pop_seq_gap(),
            latency_ms=self._metrics.recent_max_ms(
                "confirm.rtt_ms", self._config.breakers.latency_spike_window_s
            ),
            rate_limit_count=self._rate_limit_window.count(),
            marginals=marginals,
            game_keys=game_keys,
            settled_tickers=settled,
            inplay_tickers=inplay,
            tripwire_hit=self._halt_tripwire,
            changed_markets=self._metadata_changes(book_legs, metadata),
        )

    def _book_leg_signals(
        self, exposure: ExposureBook, lifecycle: QuoteLifecycle
    ) -> tuple[
        dict[str, float | None],
        dict[str, str | None],
        tuple[RfqLeg, ...],
        frozenset[str],
        frozenset[str],
    ]:
        """Extract, from the legs the risk path actually touches (every open
        quote + every open position), the per-leg marginal map, the per-leg
        game-key map, the deduped legs (as ``RfqLeg`` for the tripwire), and
        the SETTLED watch-exemption set for the marginal-jump breaker.

        The marginal map keys on ``market_ticker`` and reads the SAME provider
        the pricer/exposure use (``lifecycle.marginal_of`` → feed microprice,
        then the settled-fact cache); a leg whose book is missing/invalid and
        holds no graded fact surfaces as ``None`` (fail-closed: the jump
        breaker trips a leg we priced against that we can no longer read).
        The SETTLED set (``lifecycle.settled_watch_exempt``) carries every leg
        whose market the EXCHANGE confirmed no longer live (graded fact
        cached, or last status read closed/determined/…): the jump breaker
        SKIPS those — their book leaving the feed is the normal permanent
        close transition, and a grading (0.97 → 1.000) is not a feed move
        (live halt 2026-07-18 02:17Z). The game-key map resolves
        ``pricing.grouping.game_key`` on each leg's ``event_ticker`` — a leg
        with no event_ticker resolves to ``None`` so the unmapped-game breaker
        trips (a leg that would escape the cluster caps).

        The IN-PLAY set (``lifecycle.inplay_watch_exempt``) carries every leg
        whose game has STARTED per the same start-time ladder the pregame gate
        stops quoting on: an in-play book going dark / gapping on a goal is
        normal in-play behaviour, not the dead-feed signature (2026-07-19: 45
        halt_marginal_jump trips through the WC final). UNKNOWN start or
        operator-re-enabled in-play quoting ⇒ NOT in the set ⇒ full watch."""
        marginals: dict[str, float | None] = {}
        game_keys: dict[str, str | None] = {}
        legs: dict[str, RfqLeg] = {}  # market_ticker → RfqLeg (deduped)
        settled: set[str] = set()
        inplay: set[str] = set()
        marginal_of = lifecycle.marginal_of
        for leg_refs in self._book_leg_refs(exposure):
            for leg in leg_refs:
                ticker = leg.market_ticker
                if ticker not in marginals:
                    marginals[ticker] = marginal_of(ticker)
                    if lifecycle.settled_watch_exempt(ticker):
                        settled.add(ticker)
                    elif lifecycle.inplay_watch_exempt(ticker):
                        inplay.add(ticker)
                    game_keys[ticker] = (
                        game_key(leg.event_ticker) if leg.event_ticker else None
                    )
                    legs[ticker] = RfqLeg(
                        market_ticker=ticker,
                        event_ticker=leg.event_ticker,
                        side=leg.side,
                        # Settlement value is irrelevant to the taxonomy tripwire
                        # (it matches on series/side/line/team, not settlement);
                        # None is the pre-determination value.
                        yes_settlement_value_cc=None,
                    )
        return (
            marginals,
            game_keys,
            tuple(legs.values()),
            frozenset(settled),
            frozenset(inplay),
        )

    @staticmethod
    def _book_leg_refs(exposure: ExposureBook) -> list[tuple[Any, ...]]:
        """The leg tuples of every open position + every open quote — the legs on
        the risk path. Positions first (real exposure), then resting quotes."""
        refs: list[tuple[Any, ...]] = [
            position.legs for position in exposure.positions.values()
        ]
        refs.extend(quote.legs for quote in exposure.open_quotes.values())
        return refs

    @staticmethod
    def _book_tripwire(leg_groups: list[tuple[Any, ...]]) -> tuple[str, str] | None:
        """Re-run the taxonomy-impossible tripwire PER resting quote / position
        (each ``leg_groups`` entry is one combo's legs) — NOT over the union of
        every book leg. The per-RFQ classifier already DECLINES an impossible combo
        at pricing time (relationships.py → RelationshipKind.IMPOSSIBLE), so a
        single resting combo can never be impossible; this is the live
        belt-and-braces for exactly that.

        Scanning the UNION instead pairs legs ACROSS SEPARATE legitimate combos on
        the same game and false-halts the whole book — 2026-07-13 live: two valid
        ENG-ARG quotes ({ARG advance} in one, {ENG win} in another) formed the
        pinned impossible {advance × opponent-win} pair and killed the live book,
        even though Kalshi STILL blocks that combo (the validator did NOT loosen;
        an exchange-blocked shape is declined at pricing, never a book-wide kill).
        Same-game pairs only, matching the classifier."""
        for leg_refs in leg_groups:
            if len(leg_refs) < 2:
                continue
            rfq_legs = [
                RfqLeg(
                    market_ticker=leg.market_ticker,
                    event_ticker=leg.event_ticker,
                    side=leg.side,
                    yes_settlement_value_cc=None,
                )
                for leg in leg_refs
            ]
            game_keys = [
                game_key(leg.event_ticker) if leg.event_ticker else leg.market_ticker
                for leg in rfq_legs
            ]
            hit = taxonomy_impossible(rfq_legs, game_keys)
            if hit is not None:
                return hit
        return None

    def _metadata_changes(
        self, legs: tuple[RfqLeg, ...], metadata: MetadataCache
    ) -> tuple[str, ...]:
        """Diff each in-book market's metadata tick-over-tick and route the
        change to the response that FITS it.

        TWO LANES, chosen by FIELD — never by ticker, series, or elapsed time
        (2026-07-26 rebuild, after the 14:15 ET whole-bot halt on
        ``KXMLBTOTAL-…CLETB-6`` for ``status: active → inactive``):

        - **SETTLEMENT lane ⇒ returned here ⇒ HALT_METADATA_CHANGE + the
          needs_reconcile marker.** The market's PAYOFF FUNCTION moved: the
          rules text, the strike/line, the parent event, the expiration
          deadline, the payoff shape — or the graded ``result`` was RE-graded,
          or the status entered ``disputed``/``amended`` (the grade is being
          contested/changed), or an unrecognised status string appeared
          (fail-closed: an enum value we do not model). Behaviour is unchanged
          from before this rebuild.
        - **LIFECYCLE lane ⇒ scoped market QUARANTINE, no halt.** Only the
          exchange's trading-state bookkeeping moved: ``status`` among the
          modelled lifecycle values, ``close_time`` (which Kalshi REWRITES
          backdated at every single close — measured 2026-07-26 on
          ``KXMLBRFI-…SEATEX``: ``close_time 2026-07-29T18:35:00Z →
          2026-07-26T18:54:21Z`` bundled with the determination write, while
          ``expiration_time`` did not move), or ``can_close_early``. Nothing
          about what the position PAYS changed — the CLETB market that halted
          the bot settled ``result="no"`` on an untouched rule six minutes
          later. The proportionate response is to pull our quotes off THAT
          market and refuse it until the exchange reports it tradable and
          stable again.

        WHY the lifecycle lane is not simply "ignore": a pause may be transient
        or a permanent withdrawal (a VOID — refund, not 0/1), and the two are
        indistinguishable at the instant of the change; an unpause auto-cancels
        every resting order exchange-side (docs/api-notes/index-scan.md:150);
        a close_time rewrite invalidates the time-to-close we priced on. All
        three are cured by the quarantine, none by a whole-book kill.

        FAIL-CLOSED PROPERTIES (each pinned by a test):
        1. The lifecycle lane is reachable ONLY when the settlement fingerprint
           is byte-identical across the two samples. No settlement-relevant
           field can take it.
        2. With no quarantine sink wired, a lifecycle change is routed to the
           HALT lane instead — an unwired scoped response can never degrade to
           a silent pass.
        3. A quarantine we could not ENFORCE (a resting quote we failed to
           pull, see ``_enforce_market_quarantine``) is promoted to the
           whole-bot halt on the next status tick.

        First sighting SEEDS the baseline (no trip, no quarantine): a
        newly-quoted market is not a change. Peek-only (no network, hot-path
        safe). A market with no cached metadata, or one carrying no exchange
        payload at all (the ``put_combo_grid`` stub injects a grid with
        ``raw={}``), is skipped — there is nothing to fingerprint and no
        settlement claim to make."""
        quarantine = self._market_quarantine
        # HALT-RECEIPT evidence is rebuilt from scratch every pass: it must name
        # THIS tick's diagnosis, never a union over history. Pure record-keeping
        # — no branch below reads it.
        self._halt_evidence = {}
        # PROMOTION (property 3): quarantines still unenforced from a PREVIOUS
        # tick — this tick's new ones are added below and are not in this
        # snapshot, so a market always gets its full enforcement pass first.
        changed: list[str] = list(quarantine.unenforced())
        if changed:
            log.error(
                "market_quarantine_unenforced_escalation",
                markets=changed,
                detail="resting quotes not provably withdrawn — escalating to halt",
            )
            for ticker in changed:
                # The lifecycle move itself was recorded when the quarantine was
                # raised, on an EARLIER tick — by now the fingerprints no longer
                # differ, so re-deriving it here would find nothing. Replay the
                # held record and add what the enforcement pass learned.
                held = dict(self._lifecycle_evidence.get(ticker, {}))
                held.update(
                    origin="quarantine_unenforced",
                    quarantine_detail=quarantine.detail(ticker),
                    quarantine_armed=quarantine.armed,
                    withdraw_failure_kinds=dict(
                        self._quarantine_failure_kinds.get(ticker, {})
                    ),
                )
                self._halt_evidence[ticker] = held
        now_wall = self._clock.now()
        # The union of the risk path and the quarantine: a market that LEAVES
        # the book while quarantined must still be re-evaluated, or it could
        # never be released.
        tickers = sorted({leg.market_ticker for leg in legs} | set(quarantine.pending()))
        for ticker in tickers:
            meta = metadata.peek(ticker)
            if meta is None or not meta.raw:
                continue
            settlement_fp = self._settlement_fingerprint(meta)
            lifecycle_fp = self._lifecycle_fingerprint(meta)
            result = str(meta.raw.get("result", "") or "")
            # SETTLEMENT HORIZON = the EARLIEST tz-aware end-of-life stamp
            # (2026-07-25 THIRD halt of this class): props carry a far-future
            # LISTED close alongside a same-day expected_expiration. Retained
            # as OBSERVABILITY on the lifecycle lane (it says "this market's
            # life is ending"); it is no longer an exemption gate, because the
            # settlement fingerprint no longer contains any field that moves
            # at end of life, and a change to what a position PAYS matters
            # MOST after the game is over (this closes the hole where a
            # post-horizon amendment was silently exempt).
            horizons = [
                h
                for h in (meta.close_time, meta.expected_expiration_time)
                if h is not None and h.tzinfo is not None
            ]
            horizon = min(horizons) if horizons else None
            prior = self._metadata_fingerprints.get(ticker)
            if prior is not None:
                settlement_moved = prior.settlement_fp != settlement_fp
                # RE-GRADE (settlement lane): a graded result being REPLACED by
                # a different graded result is the outcome changing under a
                # booked position. The FIRST grading ("" → "yes"/"no") is the
                # normal determination write (measured 2026-07-26 on SEATEX)
                # and is owned by the settlement/fact machinery.
                regraded = bool(prior.result) and bool(result) and prior.result != result
                status_verdict = _status_change_class(prior.status, meta.status)
                # The receipt's evidence for THIS market, from values already
                # computed above. Recorded, never consulted.
                observed = {
                    "settlement_fp_prior": prior.settlement_fp,
                    "settlement_fp_new": settlement_fp,
                    "lifecycle_fp_prior": prior.lifecycle_fp,
                    "lifecycle_fp_new": lifecycle_fp,
                    "status_prior": prior.status,
                    "status_new": meta.status,
                    "status_class": status_verdict,
                    "regraded": regraded,
                    "settlement_moved": settlement_moved,
                }
                if settlement_moved or regraded or status_verdict == "settlement":
                    log.error(
                        "metadata_change_settlement_relevant",
                        ticker=ticker,
                        prior_status=prior.status,
                        new_status=meta.status,
                        fingerprint_moved=settlement_moved,
                        regraded=regraded,
                        status_class=status_verdict,
                    )
                    changed.append(ticker)
                    self._halt_evidence[ticker] = {
                        **observed,
                        "origin": "settlement",
                        "quarantine_armed": quarantine.armed,
                        "withdraw_failure_kinds": {},
                    }
                elif prior.lifecycle_fp != lifecycle_fp:
                    detail = (
                        f"lifecycle change {prior.status}->{meta.status}"
                        f" (settlement fingerprint unchanged)"
                    )
                    if not quarantine.armed:
                        # Property 2: nothing is refusing NEW quotes on this
                        # market, so the scoped lane would be a silent pass.
                        log.error(
                            "market_quarantine_unarmed_failclosed",
                            ticker=ticker,
                            detail=detail,
                        )
                        changed.append(ticker)
                        self._halt_evidence[ticker] = {
                            **observed,
                            "origin": "unarmed",
                            "quarantine_detail": detail,
                            "quarantine_armed": False,
                            "withdraw_failure_kinds": {},
                        }
                    else:
                        log.warning(
                            "metadata_change_lifecycle_scoped",
                            ticker=ticker,
                            prior_status=prior.status,
                            new_status=meta.status,
                            horizon=horizon.isoformat() if horizon else None,
                            horizon_passed=(
                                horizon is not None and horizon <= now_wall
                            ),
                        )
                        quarantine.quarantine(ticker, detail)
                        # HOLD the evidence of the move: if this quarantine
                        # cannot be enforced, the promotion to a whole-bot halt
                        # lands on a LATER tick, by which time these two
                        # fingerprints agree again and the move is unprovable.
                        self._lifecycle_evidence[ticker] = observed
                elif quarantine.is_quarantined(ticker) and _is_tradable_status(
                    meta.status
                ):
                    # STRUCTURAL RELEASE, never a timer: the exchange reports
                    # the market normally tradable again AND its lifecycle
                    # fingerprint stopped moving (a full 15s tick with no
                    # change). A permanently deactivated / voided market never
                    # returns to "active", so it never leaves quarantine.
                    quarantine.release(ticker)
                    # A released market's held evidence is spent: drop it so
                    # these dicts stay bounded by the LIVE quarantine set, the
                    # same bound the quarantine itself carries.
                    self._lifecycle_evidence.pop(ticker, None)
                    self._quarantine_failure_kinds.pop(ticker, None)
            self._metadata_fingerprints[ticker] = _MetaBaseline(
                settlement_fp=settlement_fp,
                lifecycle_fp=lifecycle_fp,
                status=meta.status,
                result=result,
                horizon=horizon,
            )
        # Dedupe (order-preserving): a ticker can be BOTH an unenforced
        # promotion and a fresh settlement change in the same pass — one halt,
        # one name in the detail.
        return tuple(dict.fromkeys(changed))

    async def _enforce_market_quarantine(self, lifecycle: QuoteLifecycle) -> None:
        """Carry out the scoped response: pull every DELETABLE resting quote off
        each newly-quarantined market. Runs on the status loop immediately after
        the sample that raised the quarantine, so the exposure window is the few
        milliseconds between detection and cancel — not a tick.

        BURST-BOUNDED (2026-07-26). A lifecycle wave at the END OF EVERY GAME
        quarantines many markets in one tick — 11 at 20:12:25Z across TORBOS +
        CHCPIT as their markets walked active→inactive→determined→finalized.
        That is NORMAL, not exceptional, and MLB nights end 15 games. So the
        withdrawal underneath runs CONCURRENTLY with a bounded fan-out, per-call
        timeouts, and a wall budget sized to this loop's own tick — never a
        sequential walk that can grow with the slate and never an unbounded one
        that can storm the exchange. See ``cancel_quotes_touching``.

        Only a quarantine whose quotes were provably withdrawn is marked
        enforced. Any delete failure (or any exception here at all) leaves the
        whole batch unenforced, and ``_metadata_changes`` promotes it to the
        whole-bot halt on the next tick — a scoped response we could not carry
        out is not a scoped response. Never raises: the escalation is the
        error path, not an exception."""
        pending = self._market_quarantine.unenforced()
        if not pending:
            return
        try:
            deleted, failures = await lifecycle.cancel_quotes_touching(
                set(pending),
                ReasonCode.DELETE_MARKET_QUARANTINED,
                # The pass may not outlive the loop that owns it: whatever it
                # cannot finish inside one tick stays UNENFORCED and is retried
                # next tick (and escalates to the halt if it is still unenforced
                # then — the existing fail-closed contract, unchanged).
                budget_s=STATUS_TICK_INTERVAL_S,
            )
        except Exception:
            log.exception("market_quarantine_enforcement_failed", markets=list(pending))
            return
        if failures:
            # ATTRIBUTION for the halt receipt (2026-07-27): name the failure
            # MODES that left this quarantine unenforced, from the withdrawal
            # path's own classification. This is what makes "the exchange 429'd
            # us again" distinguishable from "it timed out this time", and it is
            # the only genuinely new datum the receipt carries. Read-only,
            # additive, no control-flow change — the escalation below is byte
            # for byte what it was.
            # DEFENSIVE BY DESIGN, not by accident: this is pure attribution on
            # the ESCALATION path of a fail-closed halt. If it could raise it
            # would abort the enforcement pass — an observability field must
            # never be able to break the cure it describes. Unattributable
            # degrades to "no kinds", which the relighter reads as a distinct
            # (and unrelightable-on-repeat) signature.
            try:
                kinds = dict(lifecycle.last_withdraw_failure_kinds)
            except Exception:  # noqa: BLE001 - see above
                log.warning("withdraw_failure_kinds_unavailable", exc_info=True)
                kinds = {}
            for ticker in pending:
                self._quarantine_failure_kinds[ticker] = kinds
            log.error(
                "market_quarantine_enforcement_incomplete",
                markets=list(pending),
                deleted=deleted,
                failures=failures,
                failure_kinds=kinds,
            )
            return
        for ticker in pending:
            self._market_quarantine.mark_enforced(ticker)
            self._quarantine_failure_kinds.pop(ticker, None)
        log.warning(
            "market_quarantine_enforced",
            markets=list(pending),
            quotes_pulled=deleted,
        )

    @staticmethod
    def _settlement_fingerprint(meta: MarketMeta) -> str:
        """The fields that can actually change WHAT AN OPEN POSITION PAYS. Any
        move here is a hard halt + needs_reconcile.

        INCLUDED — each is a term in the payoff function itself:

        - ``event_ticker``: a different parent event is a different settlement
          source. (Ruled out as the 2026-07-26 cause: identical in the
          18:14:49Z cache snapshot and in the live API read.)
        - ``rules_primary``: the settlement rule verbatim — the definition of
          what pays ("If Cleveland and Tampa Bay collectively score more 11.5
          runs …, then the market resolves to Yes").
        - ``rules_secondary``: carries the POSTPONEMENT policy, which decides
          whether a delayed game still pays 0/1 or "resolves to a fair price"
          (a refund-shaped payoff). Ambiguous fields stay IN, fail-closed.
        - ``strike_type`` / ``floor_strike`` / ``cap_strike`` /
          ``custom_strike``: the LINE and the entity the rule is evaluated
          against. Total 5.5 → 6.5 is a different bet on the same game.
        - ``expiration_time`` / ``latest_expiration_time``: the real settlement
          DEADLINE, and the outer bound of the postponement window that decides
          the pays-0/1 vs resolves-to-fair-price branch above. MEASURED stable
          across a full lifecycle: untouched at 2026-07-29T16:15:00Z through
          the CLETB market's entire life, and untouched through the SEATEX
          determination write that backdated ``close_time`` in the same step.
          These are the settlement stamps the pre-rebuild fingerprint was
          missing entirely.
        - ``market_type`` / ``notional_value_dollars``: the payoff SHAPE and
          per-contract notional (binary $1). A scalar or re-notionalised market
          does not pay what we priced.

        EXCLUDED — each is exchange bookkeeping that cannot move the payoff,
        with the live evidence that says so:

        - ``status``: handled per-TRANSITION by ``_status_change_class``, not as
          a flat field. Lifecycle transitions (initialized/active/inactive/
          closed/determined/finalized) scope to a quarantine; ``disputed`` and
          ``amended`` — the grade being contested or changed — stay hard halts;
          an unrecognised string is a hard halt (fail-closed).
        - ``close_time``: "when trading stopped", not what the position pays.
          Kalshi lists it as start+3 days and REWRITES it backdated at every
          close, bundled with the terminal status write (measured 2026-07-26,
          13 markets in one evening). Watching it made two of the three
          pre-rebuild fingerprint fields pure lifecycle stamps. A postponement
          INSIDE the listed window does not change the payoff; one BEYOND it
          moves ``expiration_time``, which is in the fingerprint.
        - ``expected_expiration_time``: Kalshi's ESTIMATE, drifts as a game runs
          long (the 2026-07-25 fourth-halt fix). In NEITHER lane — it moves on
          nearly every in-play market and would quarantine the whole slate.
        - ``result`` / ``expiration_value``: the settlement OUTCOME, owned by
          the settlement/fact machinery. Its FIRST write is the normal
          determination; a RE-grade is caught explicitly in
          ``_metadata_changes``.
        - grid, prices, volume, open interest, ``updated_time``: tick noise.
        """
        raw = meta.raw
        parts = [f"event={meta.event_ticker or ''}"]
        for field in _SETTLEMENT_PAYOUT_FIELDS:
            # A key that DISAPPEARS is itself a change (distinct sentinel), so
            # a rule/strike vanishing from the payload cannot read as "same".
            value = raw.get(field, _ABSENT)
            parts.append(f"{field}={_canonical(value)}")
        return "|".join(parts)

    @staticmethod
    def _lifecycle_fingerprint(meta: MarketMeta) -> str:
        """The exchange's TRADING-STATE bookkeeping: which of these moving means
        "we may not keep quoting this market as-is", never "the payoff changed".
        A move here scopes to a market quarantine (pull our quotes, refuse the
        market, re-price on release), never a whole-bot halt.

        ``status`` — pause/unpause/close/settle; ``close_time`` — the rewritten
        trading-stop stamp we price time-to-close against; ``can_close_early``
        — whether the market may close before its listed time at all."""
        return "|".join(
            (
                f"status={meta.status}",
                f"close={meta.close_time.isoformat() if meta.close_time else ''}",
                f"can_close_early={_canonical(meta.raw.get('can_close_early', _ABSENT))}",
            )
        )

    async def _report_loop(
        self,
        store: Store,
        exposure: ExposureBook,
        lifecycle: QuoteLifecycle,
        within_game_rho: WithinGameRhoProvider,
        balance_tracker: BalanceTracker,
    ) -> None:
        while True:
            await asyncio.sleep(300.0)
            try:
                report = await build_report(
                    store,
                    env=str(self._config.env),
                    exposure=exposure,
                    marginals=lifecycle._marginals,  # noqa: SLF001 (wiring seam)
                    # The observability MC uses the SAME real per-pair correlations
                    # the quoted book carries (not the flat band) + the live
                    # bankroll so its ruin thresholds populate. Non-raising bankroll
                    # accessor: None when stale ⇒ the report MC skips ruin bands.
                    within_game_rho=within_game_rho,
                    bankroll_cc=balance_tracker.risk_bankroll_cc_or_none(),
                )
                log.info("periodic_report", report=format_report(report))
            except Exception:
                log.exception("report_failed")
