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
import itertools
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any, Protocol

from combomaker.core.clock import Clock, SystemClock
from combomaker.core.conventions import Side, load_conventions
from combomaker.core.money import CentiCents, MoneyParseError, cc_from_dollars_str
from combomaker.core.quantity import CentiContracts, qty_from_fp_str
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.quote_query import list_open_quotes, open_quote_ids
from combomaker.exchange.rest import KalshiApiError, KalshiRestClient, RateLimitedError
from combomaker.exchange.ws import WsManager
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.grid import PriceGrid
from combomaker.marketdata.metadata import MarketMeta, MetadataCache
from combomaker.marketdata.settled import MarketSource, SettledMarginalResolver
from combomaker.ops.config import AppConfig, Env, Mode, RiskConfig
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
from combomaker.ops.report import build_report, format_report
from combomaker.ops.supervisor import (
    ENV_SUPERVISOR_API_KEY_ID,
    ENV_SUPERVISOR_PRIVATE_KEY_PATH,
    ENV_SUPERVISOR_PRIVATE_KEY_PEM,
    supervisor_credential_configured,
    supervisor_heartbeat_path,
    supervisor_heartbeat_reachable,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeSchedule, FeeType
from combomaker.pricing.grouping import game_key
from combomaker.pricing.tripwire import taxonomy_impossible
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.intake import RfqIntake
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.rfq.models import Rfq, RfqLeg
from combomaker.rfq.schedule import ScheduleCache
from combomaker.risk.balance import BalanceTracker, StaleBalanceError
from combomaker.risk.breakers import BreakerInputs, CircuitBreakers, RateLimitWindow
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition
from combomaker.risk.heartbeat import Heartbeat, ReconcileMarker
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.killswitch import HaltEvent, KillSwitch
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, StarvationWatchdog
from combomaker.risk.reservation import (
    RiskReservationService,
    open_combo_positions_from_positions,
    open_combo_tickers_from_positions,
    reservation_ids_backed_by_exchange,
)
from combomaker.risk.settlement import SettlementHandler, SettlementPoller
from combomaker.risk.skew import SkewLimits, SkewParams, WidenPolicyParams
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
        worst_challenger_ev_tolerance_cc=risk_cfg.worst_challenger_ev_tolerance_cc,
        lastlook_mc_waiver_enabled=risk_cfg.lastlook_mc_waiver_enabled,
        lastlook_mc_waiver_deadline_s=risk_cfg.lastlook_mc_waiver_deadline_s,
        # WAIVER ENTITY-SET TRIM (2026-07-18): K largest resting quotes per
        # breached game inside the waiver enumeration; dropped tail rides as a
        # constant conservative adder. 0 (default) = full-set enumeration.
        lastlook_waiver_topk_resting=risk_cfg.lastlook_waiver_topk_resting,
        # CERTIFIED-HEDGE EV BUDGET (2026-07-18): the candidate gate's verified
        # negative-EV hedge exception. Default disabled / 0 = today.
        allow_negative_ev_hedge=risk_cfg.allow_negative_ev_hedge,
        hedge_cost_budget_cc=risk_cfg.hedge_cost_budget_cc,
        # FILL-RECORD RECOVERY SWEEP (2026-07-16 P1): poll REST for a confirmed
        # fill whose quote_executed WS message never arrived.
        fill_record_recovery_after_s=risk_cfg.fill_record_recovery_after_s,
        # CANCEL-REPORT VERIFY-BEFORE-DISCARD (2026-07-18 incidents): bounded
        # /portfolio/fills polls before a CANCELLED-status confirmed quote's
        # position may be discarded (both incidents were REAL taker-style
        # executions behind a "cancelled" quote status).
        fill_cancel_verify_attempts=risk_cfg.fill_cancel_verify_attempts,
        fill_cancel_verify_delay_s=risk_cfg.fill_cancel_verify_delay_s,
        # F1 MONOTONE PRE-PRICING GATE (2026-07-16 throughput batch-1): decline
        # on already-breached candidate-monotone caps BEFORE pricing. Default
        # OFF (today's behaviour); the operator arms it in the local YAML.
        pre_pricing_gate_enabled=risk_cfg.pre_pricing_gate_enabled,
        # CONFIRM-TIME resting haircut (2026-07-17): the reservation check
        # weights ONLY the resting fold; the serial commit chain stays 100%.
        resting_haircut_at_confirm=risk_cfg.resting_haircut_at_confirm,
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


def build_settled_resolver(
    risk_cfg: RiskConfig, source: MarketSource, clock: Clock
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
        source, clock, retry_after_s=risk_cfg.settled_resolution_retry_s
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

    async def get_fills(self, **params: str | int) -> JsonDict:
        """GET /portfolio/fills slice for the cancel-report verification
        (2026-07-18 verify-before-discard) — same pass-through + 429 tap, so
        verification polls feed the burst breaker too."""
        try:
            return await self._inner.get_fills(**params)  # type: ignore[attr-defined,no-any-return]
        except RateLimitedError:
            self._window.record()
            raise


class PositionsGetter(Protocol):
    """GET /portfolio/positions slice the periodic position-reconcile net
    reads (2026-07-18 requirement 3). A protocol so tests fake it without a
    real REST client; the live loop passes the KalshiRestClient."""

    async def get_positions(self, **params: str | int) -> JsonDict: ...


def _exchange_exposure_cc_by_ticker(positions_payload: dict[str, Any]) -> dict[str, int]:
    """Per-ticker cost basis of the remaining open position, in cc, from the
    positions payload's own ``market_exposure_dollars`` (doc-verified
    2026-07-21: "Cost of the aggregate market position in dollars" — the money
    at risk on the open position). Prefers the exact fixed-point dollars
    string; falls back to int-cents ``market_exposure``; a ticker with neither
    (or an unparseable value) is simply absent — the caller must then leave
    that position alarm-only rather than invent an at-risk figure (rule 6)."""
    rows = positions_payload.get("market_positions") or positions_payload.get("positions") or []
    out: dict[str, int] = {}
    for row in rows:
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        if not ticker:
            continue
        dollars = row.get("market_exposure_dollars")
        if dollars is not None:
            try:
                out[ticker] = int(cc_from_dollars_str(str(dollars)))
                continue
            except MoneyParseError:
                pass
        cents = row.get("market_exposure")
        if isinstance(cents, int) and not isinstance(cents, bool):
            out[ticker] = cents * 100
    return out


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
    # count_filter=position keeps the listing to genuinely open rows.
    rows: list[dict[str, Any]] = []
    cursor = ""
    for _ in range(25):
        params: dict[str, str | int] = {
            "subaccount": subaccount,
            "limit": 200,
            "count_filter": "position",
        }
        if cursor:
            params["cursor"] = cursor
        payload = await rest.get_positions(**params)
        rows.extend(payload.get("market_positions") or payload.get("positions") or [])
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    merged: dict[str, Any] = {"market_positions": rows}
    exch_by_ticker = open_combo_positions_from_positions(merged)
    exposure_cc_by_ticker = _exchange_exposure_cc_by_ticker(merged)

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

    known = {pos.combo_ticker for pos in exposure.positions.values()}
    unmodeled = sorted(t for t in exch_by_ticker if t not in known)
    if not unmodeled:
        return []
    local_fill_tickers = [
        t for t in unmodeled if await store.has_fill_for_ticker(t)
    ]
    recovery_owned = set(local_fill_tickers)

    adopted: list[str] = []
    alarm_only: list[str] = []
    for ticker in unmodeled:
        if ticker in recovery_owned:
            continue  # class 1: the fill-recovery sweep re-models it exactly
        exch = exch_by_ticker[ticker]
        exposure_cc = exposure_cc_by_ticker.get(ticker)
        if exposure_cc is None or exposure_cc <= 0 or exch.contracts_centi <= 0:
            alarm_only.append(ticker)  # no provable at-risk figure — never guess
            continue
        # Entry price per contract, rounded UP: booked max_loss_cc
        # (= contracts × entry // 100) is then ≥ the exchange's exposure.
        entry_cc = -(-exposure_cc * 100 // exch.contracts_centi)  # ceil div
        reserved = OpenPosition(
            position_id=f"reserve:{ticker}",
            combo_ticker=ticker,
            collection=None,
            our_side=exch.side,
            contracts=CentiContracts(exch.contracts_centi),
            entry_price_cc=CentiCents(entry_cc),
            # Self-leg side is ALWAYS "yes": a combo settles YES iff its own
            # market settles YES — the leg encodes the combo's YES definition,
            # and direction lives SOLELY in our_side. Writing our position
            # side here double-complements every NO reserve downstream
            # (receivable sweep, daily mark): losers would shield the
            # give-back halts with full notional and winners with nothing —
            # the exact inversion of the shield's contract (2026-07-21
            # review, CRITICAL finding 2).
            legs=(LegRef(ticker, ticker, "yes"),),
            risk_modeled=False,
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
            detail="exchange position with NO local context adopted as a "
            "conservatively-reserved holding (risk_modeled=False) — counted "
            "in every deterministic/gross cap from exchange figures only",
        )

    metrics.inc("position_reconcile.unmodeled")
    log.warning(
        "position_reconcile_unmodeled",
        tickers=unmodeled,
        local_fill_tickers=local_fill_tickers,
        adopted_as_reserve=adopted,
        alarm_only=alarm_only,
        detail="exchange reports open positions the in-memory risk book did "
        "not model — no-context positions are adopted as reserved holdings "
        "(exchange figures only); tickers with a local fills row are left to "
        "the fill-recovery sweep (full re-model, 2026-07-18 incident class); "
        "alarm-only rows had no readable exposure figure",
    )
    return unmodeled


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
        self._stop = asyncio.Event()
        # Phase 6 out-of-process safety plumbing. The heartbeat file is what the
        # external supervisor reads; the reconcile marker enforces
        # block-restart-until-reconciled (both live under data_dir so the
        # standalone supervisor process finds them at the same paths).
        self._heartbeat = Heartbeat(self._clock, config.data_dir / "heartbeat.txt")
        self._reconcile_marker = ReconcileMarker(config.data_dir / "needs_reconcile")
        # 429-burst window for the rate-limit circuit breaker (recorded from the
        # REST error paths in the polling loops).
        self._rate_limit_window = RateLimitWindow(
            clock=self._clock, window_s=config.breakers.rate_limit_window_s
        )
        # Set once the startup reconcile succeeds and the marker is clear — the
        # book-reconciled preflight gate reads this.
        self._book_reconciled = config.mode is not Mode.QUOTE
        # Metadata-change breaker baseline: the last sampled settlement-relevant
        # fingerprint per market ticker (close_time / status / event / expiry).
        # The breaker sampler compares the current metadata cache against this and
        # trips HALT_METADATA_CHANGE if a market the risk path touches changed
        # settlement-relevant metadata tick-over-tick. First sighting seeds the
        # baseline (no trip); it is off the hot path (status loop, 15s cadence).
        self._metadata_fingerprints: dict[str, str] = {}
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
        )
        inplay = InPlayDetector(self._clock)

        external = self._build_external_odds()
        async with KalshiRestClient(config.endpoints.rest_base_url, signer) as rest:
            metadata = MetadataCache(rest, self._clock)
            engine = PricingEngine(
                feed,
                metadata,
                conventions,
                config.pricing,
                extra_sources=(
                    [(external[0], config.pricing.external_odds.weight)] if external else []
                ),
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
            limits = LimitChecker(risk_cfg.to_risk_limits())
            # Live bankroll denominator for the %-caps (fail-closed on stale) +
            # the starvation watchdog. The tracker is polled in _balance_loop.
            balance_tracker = BalanceTracker(
                conventions, self._clock, stale_after_s=BALANCE_STALE_AFTER_S
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
                config.filters, feed, metadata, killswitch, self._clock, schedule
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
            # the rate-limit-burst breaker (not just the polls). Paper never 429s.
            sender: PaperSender | RateLimitRecordingSender
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
                sender = RateLimitRecordingSender(rest, self._rate_limit_window)
                quote_getter = sender
            # Real fee model for the fill fee the ledger books at execution
            # (defense #3): $0 for our combo maker quadratic fills, correct for a
            # nonzero-fee series. Built from the SAME config the engine uses.
            fee_cfg = config.pricing.fee
            fee_model = FeeModel(
                FeeSchedule.from_strings(fee_cfg.taker_coef, fee_cfg.maker_coef),
                conventions,
            )
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
            settled_marginals = build_settled_resolver(
                risk_cfg, rest, self._clock
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
                # MAKER-FEE LIST (2026-07-16, eat-the-fee doctrine): prefixes on
                # which OUR maker fills pay the maker fee — accounted in the
                # ledger/edge/waiver, never added to the quoted price.
                maker_fee_active_prefixes=tuple(fee_cfg.maker_fee_active_prefixes),
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
                # WEDGE FIX (2026-07-16, the 18:13Z kill): the lifecycle beats
                # this heartbeat per iteration inside its long maintenance
                # sub-loops (reprice sweep / recovery polls) — progress is not a
                # wedge; a true event-loop wedge still cannot beat.
                beat=self._heartbeat.beat,
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
                await self._startup_book_risk_snapshot(lifecycle)
                # LAUNCH THE EXTERNAL SUPERVISOR (separate OS process) BEFORE the
                # preflight so its own-heartbeat is beating when external_kill_
                # reachable is graded. The bot beats its heartbeat first so the
                # supervisor has a file to watch from t=0.
                self._heartbeat.beat()
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
            RFQ_WORKERS = 8
            # WIN-THE-TAKER FRESHNESS (2026-07-14 P1). A combo RFQ has a ~1s window;
            # an RFQ that sat in our queue too long can only rfq_closed AFTER wasting
            # pool budget on it — starving the fresh RFQs we could still win. Now the
            # queue is SHALLOW and holds (rfq, recv_mono_ns): on overflow we evict the
            # OLDEST and keep the freshest (was: dropped the newest — backwards), and
            # a worker SKIPS any RFQ whose queue dwell already exceeds the budget
            # before spending a pool slot. Off-loop pricing means CPU never wedges the
            # loop regardless, so the levers here are purely about answering FRESH.
            RFQ_QUEUE_MAX = 32           # buffer RFQ bursts (was 8 → dropped bursts)
            # Price RFQs up to 1.5s old. Combo RFQs live ~11s median, so the old 0.4s
            # SKIPPED still-winnable fresh RFQs during bursts — a stop driver. 1.5s
            # is still well inside the window and, with 8 pool workers, the queue
            # drains fast enough that few RFQs ever dwell this long.
            RFQ_MAX_QUEUE_DWELL_S = 1.5
            RFQ_RETRY_WINDOW_S = 2.0     # stop retrying a pending RFQ once it's this old
            rfq_work: asyncio.Queue[tuple[Rfq, int]] = asyncio.Queue(maxsize=RFQ_QUEUE_MAX)

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
                    try:
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
            quote_event_q: asyncio.Queue[tuple[str, JsonDict]] = asyncio.Queue()

            async def on_quote_event(kind: str, msg: JsonDict) -> None:
                quote_event_q.put_nowait((kind, msg))

            async def quote_event_worker() -> None:
                while True:
                    kind, msg = await quote_event_q.get()
                    try:
                        if kind == "quote_accepted":
                            await lifecycle.on_quote_accepted(msg)
                        elif kind == "quote_executed":
                            await lifecycle.on_quote_executed(msg)
                    except Exception:
                        log.exception("quote_event_worker_failed", kind=kind)
                    finally:
                        quote_event_q.task_done()

            intake.on_rfq(on_rfq_enqueue)
            intake.on_rfq_deleted(lifecycle.on_rfq_deleted)
            intake.on_rfq_deleted(on_rfq_deleted_cleanup)
            intake.on_quote_event(on_quote_event)

            async def on_invalidate(reason: str) -> None:
                await lifecycle.cancel_all(reason)

            feed.on_invalidate(on_invalidate)

            async def on_halt(event: HaltEvent) -> None:
                # RESTART SAFETY (Phase 6, code audit 2026-07-13 §3): on an
                # in-process HARD-class halt, DROP the needs_reconcile marker so a
                # bare restart is BLOCKED (HALT_NEEDS_RECONCILE) until the book is
                # reconciled against the exchange. Soft/manual halts do NOT need it.
                self.mark_reconcile_on_hard_halt(event)
                await lifecycle.cancel_all(event.reason)
                self._stop.set()

            killswitch.on_halt(on_halt)
            killswitch.start_kill_file_watch()

            async def on_channel_lost(reason: str) -> None:
                await lifecycle.cancel_all(reason)
                await ws.force_reconnect()

            intake.on_channel_lost(on_channel_lost)

            ws.start()
            book_ws.start()  # dedicated order-book socket (see construction note)
            tasks = [
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
                for task in tasks:
                    task.cancel()
                # Crash-path discipline: best-effort cancel-all before exit.
                try:
                    await lifecycle.cancel_all(ReasonCode.HALT_MANUAL)
                except Exception:
                    log.exception("shutdown_cancel_all_failed")
                await ws.stop()
                await book_ws.stop()
                if joint_pool is not None:
                    joint_pool.shutdown()
                if book_risk_pool is not None:
                    book_risk_pool.shutdown()
                await killswitch.stop()
                # Tear down the external supervisor subprocess (best-effort).
                try:
                    await self._stop_supervisor()
                except Exception:
                    log.exception("supervisor_stop_failed")
                await store.close()
                log.info("quote_app_stopped", metrics=self._metrics.snapshot())

    def request_stop(self) -> None:
        self._stop.set()

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
        existing positions. Returns True iff the reconcile round-trip SUCCEEDED
        (the exchange was reachable). A failure returns False so the caller can
        keep the ``needs_reconcile`` block in place — a bot that couldn't reach
        the exchange has NOT proven its book and must not resume quoting."""
        try:
            # Enumerate leftover resting quotes via the SHARED bounded+retrying
            # helper (cursor-paginated, min_ts/max_ts windowed so it never trips
            # the exchange circuit-breaker with a full-history scan, 5xx-retried).
            # Same helper the supervisor's kill-path uses — see exchange/quote_query.
            leftover = await list_open_quotes(
                rest, int(self._clock.now().timestamp())
            )
            for quote_id in open_quote_ids(leftover):
                try:
                    await rest.delete_quote(quote_id)
                except KalshiApiError as exc:
                    log.warning("startup_cancel_failed", quote_id=quote_id, error=str(exc))
            log.info("startup_reconciled", leftover_quotes=len(leftover))
            # P0-5: pin the positions read to our one subaccount (query-layer pin).
            positions = await rest.get_positions(subaccount=self._config.safety.subaccount)
            if positions.get("market_positions") or positions.get("positions"):
                log.info(
                    "startup_existing_positions",
                    detail="existing positions found — the exposure book is rehydrated "
                    "from them next (_rehydrate_exposure_book, #33)",
                )
            return True
        except KalshiApiError as exc:
            log.warning("startup_reconcile_failed", error=str(exc))
            return False

    async def _rehydrate_exposure_book(
        self,
        rest: KalshiRestClient,
        store: Store,
        exposure: ExposureBook,
        allowed_series: list[str] | None = None,
        subaccount: int | None = None,
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
            payload = (
                await rest.get_positions(subaccount=subaccount)
                if subaccount is not None
                else await rest.get_positions()
            )
        except KalshiApiError as exc:
            log.warning("rehydrate_positions_failed", error=str(exc))
            return
        # EXCHANGE = authoritative side + quantity (P0-5); settled/zero excluded here.
        exch_by_ticker = open_combo_positions_from_positions(payload)
        if not exch_by_ticker:
            return
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
        for ticker, exch in exch_by_ticker.items():
            h = held.get(ticker)
            if h is None:
                continue  # no local fill/rfq record → surfaced as unmodeled below
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
            exposure.add_position(
                OpenPosition(
                    position_id=f"{prefix}:{ticker}",
                    combo_ticker=ticker,
                    collection=h["collection"],
                    our_side=side,
                    contracts=CentiContracts(contracts),
                    entry_price_cc=CentiCents(entry_price_cc),
                    legs=legs,
                    risk_modeled=not is_reserved,
                )
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
        unmodeled = sorted(set(exch_by_ticker) - modeled - reserved)
        log.info(
            "exposure_rehydrated",
            positions=len(modeled),
            reserved=len(reserved),
            games=sorted(games),
            unmodeled_open=len(unmodeled),
            reconcile_mismatches=len(mismatched),
        )
        if unmodeled:
            log.warning(
                "rehydrate_unmodeled_positions",
                reason=str(ReasonCode.SKIP_RECONCILE_QUANTITY_MISMATCH),
                detail="open exchange positions with no local fill/rfq record (a "
                "manual/external trade) — NOT in the risk book; reconcile manually "
                "before trusting the caps",
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

        Fail-closed: if the reconcile round-trip FAILS (exchange unreachable), the
        marker STAYS set and ``_book_reconciled`` STAYS false — the preflight then
        refuses to quote. A revived bot that can't reach the exchange never
        resumes blind. Idempotent: no marker ⇒ a normal startup reconcile."""
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
                detail="exchange unreachable — book NOT reconciled; the bot will "
                "refuse to quote (needs_reconcile stays in force)",
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
        # watch from t=0 (rather than a gap until the first maintenance tick).
        self._heartbeat.beat()
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
        for ticker in rfq.leg_tickers:
            if metadata.peek(ticker) is not None:
                continue
            try:
                meta = await metadata.market(ticker)
                if meta.event_ticker:
                    await metadata.event(meta.event_ticker)
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
                except KalshiApiError as exc:
                    log.warning("metadata_fetch_failed", ticker=combo, error=str(exc))

    async def _maintenance_loop(self, lifecycle: QuoteLifecycle) -> None:
        while True:
            await asyncio.sleep(0.5)
            # Beat the heartbeat FIRST, every tick — the external supervisor
            # presumes the bot wedged if this file goes stale. A slow/failed
            # maintenance_tick still leaves the last beat aging, which is exactly
            # the wedged signal the supervisor watches for (fail-closed).
            self._heartbeat.beat()
            try:
                await lifecycle.maintenance_tick()
            except Exception:
                log.exception("maintenance_tick_failed")

    async def _balance_loop(
        self, rest: KalshiRestClient, tracker: BalanceTracker
    ) -> None:
        """Poll the exchange balance so the R2 %-of-bankroll caps have a fresh
        risk-bankroll denominator. A failed/stale poll leaves the last good
        reading to age out ⇒ the caps fail closed (they never quote off a guessed
        bankroll). Shadow in Phase 2, so a dark poll has zero quote impact today —
        but the poll keeps the shadow numbers honest on the tape."""
        while True:
            try:
                await tracker.refresh(rest)
            except RateLimitedError as exc:
                self._rate_limit_window.record()  # feed the 429-burst breaker
                log.warning("balance_poll_rate_limited", error=str(exc))
            except StaleBalanceError as exc:
                log.warning("balance_poll_stale", error=str(exc))
            except Exception as exc:
                log.warning("balance_poll_failed", error=repr(exc))
            await asyncio.sleep(BALANCE_POLL_INTERVAL_S)

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
        for ticker in tickers:
            if metadata.peek(ticker) is not None:
                continue
            try:
                meta = await metadata.market(ticker)
                if meta.event_ticker:
                    await metadata.event(meta.event_ticker)
            except KalshiApiError as exc:
                log.warning(
                    "rehydrated_leg_metadata_fetch_failed",
                    ticker=ticker,
                    error=str(exc),
                )
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
                await breakers.evaluate_and_halt(
                    self._sample_breaker_inputs(feed, lifecycle, exposure, metadata)
                )
            except Exception:
                log.exception("breaker_evaluation_failed")
            # Throughput observability: joint-memo hit rate + off-loop pool
            # counters (the Phase-3/4 decision signal). Off the hot path.
            try:
                log.info("pricing_stats", **lifecycle.pricing_stats())
            except Exception:
                log.exception("pricing_stats_log_failed")
            await asyncio.sleep(15.0)

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
            tripwire_hit=self._book_tripwire(self._book_leg_refs(exposure)),
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
        """Diff each in-book market's settlement-relevant metadata against the
        last sampled fingerprint. A market whose fingerprint changed
        tick-over-tick (close_time / status / event / expected expiry moved under
        us) is returned so ``detect_metadata_change`` trips — our settlement model
        of that market is stale. First sighting SEEDS the baseline (no trip): a
        newly-quoted market is not a change. Peek-only (no network, hot-path
        safe); a market with no cached metadata yet is skipped (nothing to
        fingerprint — the staleness/no-quote gates cover an unpriceable market)."""
        changed: list[str] = []
        for leg in legs:
            meta = metadata.peek(leg.market_ticker)
            if meta is None:
                continue
            fingerprint = self._settlement_fingerprint(meta)
            prior = self._metadata_fingerprints.get(leg.market_ticker)
            if prior is not None and prior != fingerprint:
                changed.append(leg.market_ticker)
            self._metadata_fingerprints[leg.market_ticker] = fingerprint
        return tuple(changed)

    @staticmethod
    def _settlement_fingerprint(meta: MarketMeta) -> str:
        """A stable string of the settlement-relevant metadata fields. Any change
        here means our model of when/how the market settles moved: close_time,
        exchange status (e.g. active→settled/closed), the parent event, and the
        expected expiration time. NOT the grid or the price — those move every
        tick and are not settlement-relevant."""
        return "|".join(
            (
                meta.status,
                meta.event_ticker or "",
                meta.close_time.isoformat() if meta.close_time else "",
                meta.expected_expiration_time.isoformat()
                if meta.expected_expiration_time
                else "",
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
