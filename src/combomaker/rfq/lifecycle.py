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
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any, Protocol

from combomaker.core.clock import Clock
from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import (
    CC_PER_CENT,
    CC_PER_DOLLAR,
    CentiCents,
    MoneyParseError,
    fee_cc_from_dollars_str,
)
from combomaker.core.quantity import CentiContracts, qty_from_fp_str
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.quote_query import (
    QuoteLister,
    list_open_quotes,
    open_quote_ids,
)
from combomaker.exchange.rest import (
    DEFAULT_ENDPOINT_TOKEN_COST,
    DEFAULT_REQUEST_TIMEOUT_S,
    DELETE_QUOTE_TOKEN_COST,
    HTTP_TOO_MANY_REQUESTS,
    KalshiApiError,
    already_gone,
)
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.marketdata.settled import SettledMarginalResolver
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import STORE_OP_TIMEOUT_S, Store
from combomaker.ops.pricing_pool import (
    BookRiskInputs,
    BookRiskPool,
    CandidateBookRiskInputs,
    JointPool,
    StateWorstCaseInputs,
    _worker_candidate_book_risk,
    _worker_state_worst_case,
)
from combomaker.ops.write_budget import TokenBudget, WriteBudget
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeType, FeeUnknownError
from combomaker.pricing.grouping import game_key
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.models import Rfq
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.cap_family import det_max_backstop_frac
from combomaker.risk.concentration_steer import (
    CrnBookCache,
    LossEventBook,
    SteerCenter,
    build_loss_event_book,
)
from combomaker.risk.deploy_scale import FAILSAFE as DEPLOY_SCALE_FAILSAFE
from combomaker.risk.deploy_scale import (
    DeployScaleResult,
    scale_grid,
    solve_deployment_scale,
)
from combomaker.risk.exposure import (
    ExposureBook,
    ExposureSnapshot,
    LegRef,
    OpenPosition,
    OpenQuoteRisk,
    SettledFactProvider,
    concentration_live_legs,
    stable_ledger_key,
)
from combomaker.risk.fill_velocity import FillVelocityTracker
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.killswitch import KillSwitch
from combomaker.risk.lastlook import (
    LastLookInputs,
    LastLookPolicy,
    decide_confirm,
)
from combomaker.risk.limits import (
    _SLATE_TZ,
    Breach,
    ConcentrationCertificate,
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
    ConcentrationProfile,
    GameSkewCache,
    LegAxisProfile,
    PBookProfile,
    SkewLimits,
    SkewParams,
    WidenPolicyParams,
    compute_inventory_skew,
    decide_widen_or_decline,
    ticket_bucket,
)
from combomaker.sim.book_model import (
    BookModel,
    WithinGameRhoProvider,
    build_book_model,
    select_modeled_positions,
    within_game_pair_tickers,
)
from combomaker.sim.book_risk import (
    BookRiskSnapshot,
    CandidateBookRisk,
    compute_book_risk,
    modeled_cost_basis_cc,
)
from combomaker.sim.peak_profile import PeakProfile, build_peak_profile
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


def _pbook_profile_from_snapshot(
    snap: Any, *, game_budget_cc: float
) -> PBookProfile:
    """The P(book) steer profile from an ACCEPTED book-risk snapshot — pure
    (extracted for direct testing; 2026-07-25 review).

    Shares normalize by the POSITIVE tail mass only (negative attribution
    entries — hedged/protective games — are excluded from BOTH numerator and
    denominator so shares sum to 1; a perfectly uniform book must never read
    as concentrated). Zero/negative-attribution games are published as
    PROTECTED: flow there gets no underweight rebate (it can erode the
    hedge). ``share × total_tail_cc`` reproduces each game's own tail loss
    exactly, so the component's onset ratio is that loss vs the enforced
    budget."""
    positive_tail = sum(
        tc.loss_cc for tc in snap.per_game_tail_cc if tc.loss_cc > 0.0
    )
    shares = (
        {
            tc.key: tc.loss_cc / positive_tail
            for tc in snap.per_game_tail_cc
            if tc.loss_cc > 0.0
        }
        if positive_tail > 0.0
        else {}
    )
    return PBookProfile(
        input_generation=snap.input_generation,
        p_book=snap.p_profit,
        tail_share_by_game=shares,
        total_tail_cc=positive_tail,
        # The tail-share Herfindahl: a BOOK property, so it is computed here
        # (once per publish, off the hot path) and read O(1) on the quote path.
        tail_hhi=math.fsum(s * s for s in shares.values()),
        game_budget_cc=game_budget_cc,
        protected_games=frozenset(
            tc.key for tc in snap.per_game_tail_cc if tc.loss_cc <= 0.0
        ),
    )


# CONFIRM-PATH LAST-LOOK MC WAIVER (handoff Problem A): the ONLY reservation-
# denial breach reasons the waiver may lift — the two deliberately comonotone-
# OVERSTATED analytic per-game bounds whose true state-consistent loss the exact
# scoreline enumeration can certify. ANY other enforced breach in the denial ⇒
# decline exactly as today (the waiver never touches gross / per-combo / daily /
# CVaR / ruin / notional / slate caps or any halt).
# SLATE-AXIS WAIVER game-selection bound (2026-07-25): the max games a
# slate-ONLY denial certifies in one waiver run — a COMPUTE bound under the
# enumeration deadline (each game adds scoreline-enumeration work; the
# deadline fail-closes regardless), never a risk number: every certificate is
# still validated against the unchanged game budget and the slate threshold
# is never raised. RAISED 6→16 same day: the first live attempt (11:58a)
# certified the top-6 but the UNCERTIFIED tail games' analytic terms alone
# kept the sum over threshold on a 13-game slate — while the enumeration ran
# in ~190ms, far under the 1.0s deadline. 16 covers a full MLB slate; the
# deadline remains the guard on pathological nights.
_SLATE_WAIVER_MAX_GAMES = 16

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


class FillsGetter(Protocol):
    """GET slice for CANCEL-REPORT VERIFICATION (2026-07-18 incidents A+B): the
    REST ``GET /portfolio/fills`` read the sweep polls before it will believe a
    CANCELLED quote status. PROVEN twice live 2026-07-18 (quotes 903935fc and
    7d79f32b): the exchange reported a CONFIRMED quote as ``cancelled``
    (``cancellation_reason: "execution failed"``) while the fill EXECUTED
    anyway as a taker-style REGULAR order (nonzero ``fee_cost``,
    ``is_taker: true``) visible only on /portfolio/fills — no
    ``quote_executed`` WS message ever fires for that variant. Optional and
    separate from ``QuoteSender``/``QuoteGetter`` so paper mode and existing
    fakes stay untouched; no getter wired ⇒ the prior immediate-discard
    behaviour (backtests/minimal rigs only — live always wires it)."""

    async def get_fills(self, **params: str | int) -> JsonDict: ...


# FILL-RECORD RECOVERY SWEEP bounds (2026-07-16 P1). Rate-bound the REST polls
# per maintenance tick (the tick beats every 0.5s; recovery is not latency-
# critical) and bound the per-quote attempts: after the budget is exhausted the
# sweep gives up LOUDLY (fill_recovery.exhausted + an error log) and leaves the
# state for the next-restart exchange reconcile (the P0-4/P0-5 backstop that
# found the 2026-07-16 proven case) rather than polling forever.
_FILL_RECOVERY_MAX_POLLS_PER_TICK = 3
_FILL_RECOVERY_MAX_ATTEMPTS = 10
# Per-REST-poll wall bound on the maintenance tick. Tighter than the REST
# client's own 10s total (``DEFAULT_REQUEST_TIMEOUT_S``) because these polls run
# SERIALLY inside one tick: 3 × 10s turned a black-holed connection into a ~30s
# tick (2026-07-16 review). Named 2026-07-26 so the off-loop sweep bounds below
# can be DERIVED from it instead of repeating the literal.
_MAINTENANCE_POLL_TIMEOUT_S = 2.5
# Pages the fills-ledger diff walks per sweep.
_FILLS_SWEEP_MAX_PAGES = 3

# ── OFF-LOOP ALARM-ONLY SWEEPS (2026-07-26, the 20:12:24Z maintenance stall) ──
# The maintenance loop owns TTL expiry, the enforced limit/halt check and the
# reprice sweep — and, until the 2026-07-26 liveness split, the supervisor
# heartbeat itself. Two ALARM-ONLY diagnostics were awaited INLINE on that loop:
# the position-ledger divergence invariant (one SELECT) and the fills-ledger
# diff. On 2026-07-26 the divergence SELECT came due at 20:12:24.43Z — exactly
# 300.000 s after the run's only completed check at 20:07:24.43Z — on a store
# whose single aiosqlite connection thread was saturated by the tape writer
# (46 ``store_writer_checkpoint_failed``, WAL 57,765 frames). It never returned:
# ``ledger_divergence.checks`` finished the run at 1. The maintenance loop went
# silent from that instant and the supervisor killed a healthy, quoting bot 30.1 s
# later.
#
# The rule that follows: an ALARM-ONLY diagnostic never runs inline on a loop
# that owns safety work. These are launched as SINGLE-FLIGHT background tasks
# (the ``_maybe_resolve_settled_marginals`` pattern), each under its own wall
# bound, and a slow store degrades to a logged, retried skip. The tick returns
# in the time its own work takes, whatever the store is doing.
#
# Bounds are DERIVED from what each sweep does, out of primitives that already
# exist — never a fresh literal:
#   * divergence  = one store read            ⇒ STORE_OP_TIMEOUT_S
#   * fills diff  = 2 store reads + N pages   ⇒ 2·STORE_OP_TIMEOUT_S
#                                               + N·_MAINTENANCE_POLL_TIMEOUT_S
_LEDGER_DIVERGENCE_SWEEP_TIMEOUT_S = STORE_OP_TIMEOUT_S
_FILLS_LEDGER_SWEEP_TIMEOUT_S = (
    2 * STORE_OP_TIMEOUT_S + _FILLS_SWEEP_MAX_PAGES * _MAINTENANCE_POLL_TIMEOUT_S
)

# ── BOUNDED QUOTE WITHDRAWAL (2026-07-26, the 20:12:54Z false kill) ──────────
# A lifecycle wave at the END OF EVERY GAME quarantines many markets in the same
# tick (11 across TORBOS+CHCPIT at 20:12:25Z) and the withdrawal underneath is a
# burst of DELETEs — 63 of them over 27s in the incident, one at a time, most
# answered HTTP 404 because the exchange had already dropped the quotes with
# their finished markets. Three bounds, so the burst is a normal event and not
# an outage:
#
#   1. CONCURRENT, but paced by the WRITE-TOKEN BUDGET (``ops/write_budget.py``,
#      the same bucket + the same config source the supervisor's own emergency
#      cancel-all is paced by). NOT by a concurrency literal: the first cut of
#      this fix used a fan-out of 8, and a fan-out is not a rate — 8 in flight
#      at the exchange's measured 5-40 ms DELETE latency is 200-1,600 req/s =
#      400-3,200 WRITE TOKENS/s (DeleteQuote costs 2 —
#      ``exchange.rest.DELETE_QUOTE_TOKEN_COST``) against a 300 tokens/s
#      Advanced-tier ceiling. A 200-quote wave (the live ``max_open_quotes``)
#      would therefore 429 its own tail, and every 429 here lands on the three
#      worst places at once: the burst breaker (HALT_RATE_LIMIT_BURST at 10 per
#      10s), the quarantine's unenforced escalation (HALT_METADATA_CHANGE), and
#      — before this fix — ``_drop_quote``, which would have made the mirror
#      FORGET quotes that were still resting and could still fill. Pacing in
#      tokens is latency-independent by construction; a fan-out is not.
#   2. A PER-CALL wall bound, reusing the REST client's own request timeout, so
#      one hung socket cannot pin the pass forever. (The real client already
#      enforces it; the bound here also covers the ``QuoteSender`` PROTOCOL,
#      through which no aiohttp timeout is visible.)
#   3. An optional WHOLE-PASS budget from the caller, sized to the caller's own
#      tick. Work that does not fit is DEFERRED, never abandoned: its markets
#      stay unenforced and are retried next tick (and escalate to the halt if
#      they are still unenforced then — the fail-closed contract is unchanged).
#      Because a deferral costs a halt on the NEXT tick, a pass that is merely
#      out of tokens WAITS for the bucket to refill (the wait itself is bounded
#      by the caller's budget) instead of deferring on the spot.
#
# ── ONE WRITE PATH, ONE PROVER, AND THE PROVER IS A READ (2026-07-26) ────────
# The bounds above make a withdrawal wave *safe*; they do not make an UNKNOWN
# withdrawal *terminate*. The first cut left two holes the adversarial gate
# proved with PoCs:
#
#   * a SECOND, unmetered DELETE path (``_delete_quote`` owned its own
#     ``self._sender.delete_quote`` call), so the busiest way to withdraw a
#     quote — TTL / leg-stale / leg-moved / RFQ-gone / eviction, 1,137 calls in
#     the incident — spent no tokens at all; and
#   * the reprice sweep re-DRIVING every pending withdrawal every 0.5 s tick,
#     which is O(pending) WRITES per tick on the exact bucket that is already
#     429ing. At the live ``max_open_quotes`` = 200 that is 400 write tokens
#     per tick against a 300 tok/s ceiling: the storm sustains its own 429s and
#     never establishes truth, so the pending set never drains and quoting
#     bricks at capacity.
#
# Both are now structural, not cadence-tuned:
#
#   A. ``self._sender.delete_quote`` is referenced EXACTLY ONCE in this module —
#      inside ``_withdraw_batch._one``, immediately after the token gate. An
#      unmetered write is unwritable, not merely discouraged. Every caller goes
#      through ``_withdraw_and_reconcile``, which is also the ONLY caller of
#      ``_drop_quote`` for a withdrawal, so no path can drop an id the exchange
#      never proved gone. (Both invariants are grep-checked by
#      tests/test_architecture.py.)
#   B. The UNKNOWN is resolved by a READ, not a retried write:
#      ``_resolve_withdraw_pending`` asks ``GET /communications/quotes?
#      user_filter=self&status=open`` ONCE per tick — 10 READ tokens, O(1) in
#      the pending count, on the bucket the write storm is not touching (Kalshi
#      meters reads on a separate bucket; observed advanced 600 cap / 300
#      refill on EACH). Absent from the open set ⇒ PROVEN off the wire ⇒
#      dropped. Present ⇒ PROVEN still resting ⇒ re-deleted through the metered
#      write path. A failed read resolves NOTHING.
#
# The resolver's whole pass reuses the sweep's existing wall budget rather than
# introducing a number: the READ is bounded by the tick's existing per-REST-poll
# bound and whatever is left of the pass budget is what the metered re-DELETE
# drain may spend. Work that does not fit stays pending and is retried next tick.
_CANCEL_TIMEOUT_S = DEFAULT_REQUEST_TIMEOUT_S


def _default_write_budget(clock: Clock) -> WriteBudget:
    """The withdrawal bucket for a lifecycle nobody injected one into.

    Sized from the SAME config object the live bot's supervisor budget is sized
    from (``ops.config.SupervisorConfig`` defaults: 200 tokens / 10.0 s), so
    there is exactly one place an operator changes the write budget and no
    second literal to drift. Imported inside the function to keep the pydantic
    config module off this hot module's import graph."""
    from combomaker.ops.config import SupervisorConfig  # noqa: PLC0415

    defaults = SupervisorConfig()
    return WriteBudget.create(
        clock,
        capacity=defaults.write_budget_capacity,
        refill_s=defaults.write_budget_refill_s,
    )


def _already_gone(exc: BaseException) -> bool:
    """True when a DELETE failed because the exchange has no such quote.

    A 404 on a withdrawal is SUCCESS for our purposes: the thing we wanted off
    the wire is off the wire, and it cannot fill. Treating it as an error is
    what turned a routine end-of-game wave into 63 warnings and an unenforceable
    quarantine (which escalates to a WHOLE-BOT HALT) for markets that carried no
    exposure at all.

    Delegates to ``exchange.rest.already_gone`` — ONE definition of the narrow
    404-only rule, shared with the startup reconcile's leftover cancel, so the
    two withdrawal paths cannot drift apart. NARROW by construction: a 429
    (``RateLimitedError``), a 5xx, a timeout, or a transport error is UNRESOLVED
    — the quote may still be resting and may still fill, so it stays in our
    mirror, counts as not-provably-withdrawn, and is retried (see
    ``_withdraw_batch``)."""
    return already_gone(exc)


def _withdraw_failure_kind(exc: BaseException | None) -> str:
    """Name the FAILURE MODE behind one not-provably-withdrawn quote.

    Pure observability, and the ONLY new datum the halt receipt carries that is
    not already computed somewhere (see ``QuoteApp._write_halt_receipt``). It is
    what makes a repeat halt distinguishable from a NEW one: the relighter's
    novelty bound keys on the set of distinct kinds observed at THIS failure
    site, so "the exchange 429'd us again" and "the exchange timed out this
    time" are different root causes and "429 again" is not.

    ``None`` means the quote was never asked about at all (the pass ran out of
    wall budget) — a genuinely different mode from any exchange answer. An
    exception carrying an HTTP ``status`` is named by that status (so 429 / 500
    / 503 separate); anything else — a timeout, a transport error, our own
    read-budget refusal (status 0) — is named by its exception class. Derived
    from the exception, never chosen by a caller."""
    if exc is None:
        return "budget_deferred"
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status:
        return str(status)
    return type(exc).__name__


def _rate_limited(exc: BaseException) -> bool:
    """True when a DELETE was refused by the exchange's TOKEN BUCKET (429).

    Split out from the other unresolved outcomes for observability only — the
    handling is identical (the quote is UNKNOWN, never dropped) — because a 429
    on THIS path means our pacing is wrong, which is a different repair from a
    5xx or a hung socket."""
    return getattr(exc, "status", None) == HTTP_TOO_MANY_REQUESTS

# CANCEL-REPORT VERIFICATION bounds (2026-07-18 review). A verification ROUND
# whose EVERY /portfolio/fills read failed proves nothing about absence — the
# whole round is retried on the same cadence up to this many rounds (so a
# transient 429 storm cannot pin a possibly-phantom position's budget until
# restart), THEN the loud unresolved give-up. And the /portfolio/fills query is
# time-scoped to the verification window: min_ts = the quote's confirm
# WALL-time minus this slack (absorbs local-vs-exchange clock skew without
# re-admitting the ticker's historical tape — the live ledger holds
# same-ticker/side/exact-count fills hours apart, so an unscoped match can hit
# a HISTORICAL fill and double-count it).
_CANCEL_VERIFY_MAX_ROUNDS = 3
_CANCEL_VERIFY_MIN_TS_SLACK_S = 60


def _parse_epoch_s(raw: object) -> int | None:
    """Fill-row timestamp → epoch seconds; None on absent/unreadable
    (fills-ledger sweep watermark input — a row whose timestamp cannot be
    read still gets its ledger check; the sweep then refuses to advance its
    watermark past it). Accepts ISO-8601 (Z or offset; a NAIVE parse is
    treated as UTC — .timestamp() on a naive datetime would read it in LOCAL
    time and shift the watermark hours ahead) and the legacy integer-epoch
    ``ts`` form."""
    if raw is None or raw == "":
        return None
    text = str(raw)
    if text.lstrip("-").isdigit():
        try:
            return int(text)
        except ValueError:  # pragma: no cover — isdigit guarded
            return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())

# REPRICE-SWEEP WEDGE DEFENSES (2026-07-16 — the 18:13Z heartbeat kill; see
# maintenance_tick). Consecutive pool-deadline results that trip the frozen-pool
# circuit breaker, and the sweep's total wall budget per tick.
_REPRICE_POOL_TRIP = 2
_REPRICE_SWEEP_BUDGET_S = 2.5
# WITHDRAW-RESOLUTION pass budget (2026-07-26). The resolver runs at the top of
# the same maintenance tick, immediately ahead of the reprice sweep, and gets the
# SAME whole-pass wall bound the sweep already has — an EXISTING primitive, not a
# new number. Inside it the list READ is bounded by the tick's existing
# per-REST-poll bound (_MAINTENANCE_POLL_TIMEOUT_S) and the remainder is the
# metered re-DELETE drain's budget.
_WITHDRAW_RESOLVE_BUDGET_S = _REPRICE_SWEEP_BUDGET_S

# F1 PRE-PRICING GATE cache age bound (seconds). The generation/bankroll keys
# do the real invalidation; this bound only covers slow drift (metadata ME
# answers) and matches the 0.5s maintenance-tick granularity the book is
# re-checked at anyway.
_PRE_GATE_CACHE_TTL_S = 0.5

# IN-PLAY SHADOW eligibility (2026-07-25): the reason set an RFQ may carry to
# count as skipped SOLELY for being in-play. SKIP_INPLAY_LEG is the schedule
# gate (required); SKIP_IN_PLAY is the close-time proximity proxy that fires
# ALONGSIDE it once a game is deep enough in-play (MLB close = start+3h, the
# 1h min_time_to_close bar trips ~2h in). Any reason outside this set means
# the RFQ would have been declined even with in-play quoting armed, so pricing
# it would poison the adverse-selection measurement.
_INPLAY_ONLY_SKIPS = frozenset(
    {ReasonCode.SKIP_INPLAY_LEG, ReasonCode.SKIP_IN_PLAY}
)

# IN-PLAY SHADOW recorded-RFQ dedupe cap (a MEMORY bound, not a behaviour
# knob): quote_app's pending-retry loop re-runs handle_rfq up to 5x per RFQ
# within ~2s, and each re-skip would re-shadow an idle-pool RFQ into duplicate
# rows. One row per rfq_id: recorded ids are remembered insertion-ordered and
# evicted FIFO past this cap (the standing 4096 cache-cap precedent — an
# eviction can only ever cost a duplicate row, never a wrong number).
_INPLAY_SHADOW_DEDUPE_CAP = 4096

# === EXCHANGE CONFIRM WINDOW (protocol fact, not a tunable) ==================
# Combo RFQs are HVM: the exchange gives the maker 3.0s to confirm an accepted
# quote and 1s to execute (docs/api-notes/communications-ws.md:261; the same 3.0
# the RiskConfig budget validators already encode). This is a property of the
# venue, in the same class as a tick size — it is READ here, never tuned. Every
# confirm-path budget in this module is DERIVED from it minus MEASURED costs.
EXCHANGE_CONFIRM_WINDOW_S: float = 3.0
EXCHANGE_CONFIRM_WINDOW_NS: int = int(EXCHANGE_CONFIRM_WINDOW_S * 1e9)

# NO COST PREDICTOR (2026-07-27, B2). This module used to carry a
# ``_MeasuredRate`` ms/unit estimator and skip the candidate gate's build/MC
# when the PREDICTED cost did not fit the confirm window. It is gone. Two
# defects made it unfixable-in-place rather than merely mistuned:
#
#   * it was fed the WHOLE build time as if every millisecond scaled with the
#     PAIR count, while the build's dominant term on a small book is FIXED
#     (``_gate_pricing_edge`` — a full engine call, 160-450 ms measured), so a
#     small book yielded ~1000x the true marginal rate; and
#   * ``predict_ms`` took max() over the retained samples, and samples only
#     arrived when a build actually RAN. One pessimistic sample skipped the
#     build; a skipped build produced no new sample; the max never decayed. The
#     joint-tail / P(ruin) / delta-P(book) gate went DARK PERMANENTLY, with only
#     a counter to show for it.
#
# The replacement is not a better predictor, it is no predictor: the input build
# is INTERRUPTIBLE against the derived deadline and the MC is BOUNDED by the
# remaining window (the same deadline_s the last-look waiver's off-loop
# enumeration already uses). Both stages are therefore attempted on EVERY
# accept, both are bounded by the clock rather than by history, and there is no
# state a bad sample can poison — the MC cannot be turned off by anything except
# the window itself running out.


class _GateBudgetExceeded(Exception):
    """Raised out of the candidate-gate INPUT BUILD when it crosses its deadline.

    The build (not the MC) was the live killer: 23 of 24 lost auctions in one
    session never reached a single Monte Carlo sample — they were still
    resolving O(T^2) rho pairs when the budget ran out. A synchronous loop that
    cannot be abandoned is a loop that spends the whole confirm window, so the
    build is now interruptible and its abandonment is a TIMEOUT (deterministic
    fallback), never a risk decline."""

    def __init__(self, stage: str, elapsed_ms: float) -> None:
        super().__init__(f"candidate gate build exceeded its budget in {stage}")
        self.stage = stage
        self.elapsed_ms = elapsed_ms


@dataclass(frozen=True, slots=True)
class LifecycleConfig:
    quote_ttl_s: float = 30.0
    reprice_threshold_cc: int = 100
    # LEVER #5 (2026-07-27) — the MEASURED, CMH-stratified fill-rate
    # elasticity: the fraction of fills lost per CENT of extra width. This is
    # a MEASUREMENT, not a knob: the concentration steer's validity horizon is
    # ``1/(2e)`` cents (the half-range at which the linear elasticity's own
    # extrapolation has burned half the flow), so a re-measurement moves the
    # steer automatically and nothing has to be hand-tuned. 0 ⇒ that bound
    # simply abstains and the LIVE MARGIN binds instead.
    fill_elasticity_per_cent: float = 0.22
    # LEVER #5 CRN cache: rows drawn on the PRICING joint for the
    # Cov(candidate, book) price. A COMPUTE bound (the standing 4096 precedent
    # in peak_profile / the waiver), never a risk number — the RESOLVABILITY
    # decision is made from the drawn sample's own measured SNR against the
    # ratified z anchor below, and an unresolvable draw simply abstains.
    crn_cache_samples: int = 4096
    crn_min_snr_z: float = 3.0
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
    # FIX 5 (2026-07-28): arm the BOOK-GROWTH DECAY of a generation-stale
    # snapshot instead of discarding it (see ``_book_risk_for_check``). False
    # (default) = SHADOW: the decayed verdict is computed and logged, the
    # discard still happens, behaviour byte-identical. The TIME-staleness guard
    # (``book_risk_stale_after_s``) is NOT decayed and keeps failing closed.
    book_risk_stale_decay: bool = False
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
    # --- DEPLOYMENT SCALE (risk/deploy_scale.py; operator LEVER #1) ----------
    # Default OFF ⇒ the scale is never solved, never consumed, and every
    # ``check`` call passes 1.0 (byte-identical to before this existed). When
    # armed, the scale is SOLVED off the maintenance tick — same off-loop
    # discipline as the book-risk MC, never on the quote path — as the largest
    # uniform book scaling that STILL clears every enforced cap including the
    # ratified ``portfolio_kill_tail_prob``. It then breathes only the
    # DEPLOY-side budgets (per-combo / entity / game / slate / directional);
    # the envelope it was solved against is untouchable by construction.
    deploy_scale_enabled: bool = False
    # Search ceiling for the bisection. NOT a target and NOT a cap: if the
    # envelope is still clean at this bound the solve reports it and stops
    # looking (a bound, so one pathological snapshot cannot ask for 100x).
    deploy_scale_s_max: float = 3.0
    # Probe ladder resolution: at most this many full-book MCs per solve, and
    # the scale is quantized to (s_max - 1) / points. Bounded by construction —
    # the operator can read the MC budget straight off the number.
    deploy_scale_grid_points: int = 16
    # Solve cadence. Deliberately MUCH slower than the book-risk refresh: the
    # solve shares the SAME single-worker pool the gating snapshot uses, and a
    # starved gating snapshot fails the CVaR cap CLOSED (declines everything).
    # At 16 probes x deploy_scale_mc_samples this is a ~1% duty cycle on that
    # worker. The generation guard, not this interval, is what keeps the number
    # honest between solves (a fill invalidates it instantly).
    deploy_scale_refresh_s: float = 300.0
    # MC budget PER bisection probe. Smaller than the gating snapshot's because
    # this solve never gates anything — it only decides how much of the already-
    # enforced envelope the deploy budgets may use, and every candidate still
    # faces the full-fidelity gating snapshot afterwards.
    deploy_scale_mc_samples: int = 20_000
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
    # === CONFIRM-WINDOW REBUILD (2026-07-27) ==================================
    # THE DEFECT THIS REPLACES: ``candidate_gate_deadline_s`` is a TYPED constant
    # and its expiry resolved to DECLINE. Live, that discarded 70 already-WON
    # auctions in two days (254.0 contracts) — 23 of 24 losses in one session
    # never ran a single MC sample: the O(T^2) INPUT BUILD alone blew the budget,
    # and the loss was recorded as ``decline_candidate_risk``, a stopwatch
    # wearing a risk reason code. Worse, it was self-stalling: each fill grew the
    # book, which grew the build, which timed out the next fill.
    #
    # THE REPLACEMENT, per the operator's "no manual number updates; the bot
    # should know how to react smoothly":
    #   * the budget is DERIVED per accept from the exchange's own confirm window
    #     minus the time already burnt since the accept minus a MEASURED reserve
    #     (confirm RTT high quantile + its observed dispersion + the measured cost
    #     of the deterministic fallback itself) — never a typed constant;
    #   * the gate predicts build+MC cost from MEASURED per-unit rates and simply
    #     DOES NOT START work that cannot fit, so growth degrades continuously
    #     instead of falling off a cliff;
    #   * expiry no longer means decline: it means fall back to the DETERMINISTIC
    #     caps, re-checked against the live book.
    # False restores the pre-2026-07-27 typed budget (rollback switch only).
    candidate_gate_derived_deadline: bool = True
    # A candidate-gate TIMEOUT resolves to the DETERMINISTIC fallback (the held
    # reservation's enforced caps — det-max / per-combo / entity / game / slate /
    # gross — re-checked against the CURRENT book, arithmetic, microseconds)
    # instead of an automatic decline. We have already WON an auction that passed
    # those caps at reservation time and was priced +EV; throwing it away because
    # a Monte Carlo was slow is the worst available default. The MC gate becomes a
    # best-effort REFINEMENT: it can still DECLINE when it finishes, and it can
    # never be the reason we miss the window. False restores the old
    # decline-on-timeout (rollback switch only).
    candidate_gate_timeout_fallback: bool = True
    # Granularity (in ticker PAIRS) at which the O(T^2) rho resolution inside the
    # gate's input build checks its deadline. Pure LOOP-CHECK granularity, not a
    # risk number: smaller = tighter deadline honouring, larger = fewer clock
    # reads. At the measured ~0.126 ms/pair, 256 pairs is ~32 ms of overshoot.
    candidate_gate_build_check_pairs: int = 256
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
    # SLATE-AXIS WAIVER (2026-07-25). When True, a slate-ONLY reservation
    # denial (every breach SKIP_SLATE_CAP — the 7/25 evidence: 7 accepted +EV
    # fills bounced while real committed risk was ~$7, the resting burst-floor
    # projection alone spanning the slate budget) certifies the largest
    # analytic contributors and retries: the checker substitutes each
    # certificate into the slate roll-up (2026-07-17 machinery) and re-checks
    # the UNCHANGED slate threshold honestly. Default OFF = the
    # pre-2026-07-25 decline, byte-identical. Requires
    # ``lastlook_mc_waiver_enabled``.
    lastlook_waiver_slate_axis: bool = False
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
    # CANCEL-REPORT VERIFY-BEFORE-DISCARD (2026-07-18, two live incidents).
    # When the recovery sweep's REST GET quote reports a CONFIRMED quote as
    # CANCELLED, the position is NOT discarded until ``/portfolio/fills`` has
    # been checked for a matching execution: the exchange executed BOTH
    # 2026-07-18 "cancelled" quotes as taker-style regular orders (fills
    # visible only on /portfolio/fills, one landing minutes after the cancel
    # report). ``fill_cancel_verify_attempts`` bounded polls, spaced
    # ``fill_cancel_verify_delay_s`` apart on the injectable clock (defaults: 3
    # polls over ~3 minutes — covers the observed late execution). A match ⇒
    # the position is KEPT and the fills row is written through the NORMAL
    # on_quote_executed writer (fill_recovery_late_execution). Genuinely absent
    # after the polls ⇒ the phantom is removed exactly as before, with the
    # verification evidence logged. attempts <= 0 ⇒ verification disabled (the
    # pre-2026-07-18 immediate discard; not recommended live).
    fill_cancel_verify_attempts: int = 3
    fill_cancel_verify_delay_s: float = 90.0
    # FILLS-LEDGER SWEEP (2026-07-24 incident C). Periodic account-wide diff of
    # /portfolio/fills against the local fills ledger (order_id keyed) — the
    # generic backstop under ANY writer-path miss (incident C: a partial
    # execution behind a cancel report was structurally rejected and its fill
    # landed nowhere until the settlement reconciliation found it a day later).
    # ALARM-ONLY: the fills ledger keeps its ONE writer (on_quote_executed) —
    # a miss is a loud ERROR + metric, never an auto-written row; rows owned by
    # an in-flight verification/recovery are excluded. The first maintenance
    # tick only stamps the cadence; the first fetch runs one interval later
    # (keeps scripted per-ticker test fakes off the account-wide query).
    # Non-positive/NaN interval ⇒ sweep disabled (belt to the config validator).
    fills_ledger_sweep_interval_s: float = 900.0
    fills_ledger_sweep_lookback_s: float = 3600.0
    # POSITION-LEDGER DIVERGENCE INVARIANT (2026-07-26). Cadence of the
    # maintenance-tick "open exposure positions vs open position_ledger rows"
    # count. Alarm-only observability (one small indexed SELECT per interval,
    # never a risk input, never on the pricing path) so a ledger that stops
    # matching the book is SELF-REPORTING. Non-positive ⇒ disabled.
    ledger_divergence_sweep_interval_s: float = 300.0
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
    # B2 DERIVED HEDGE BUDGET (operator directive 2026-07-25: pay up to win
    # offsetting flow when lopsided, no manual number). When True the
    # certified-hedge budget becomes max(static budget, PRE−POST certified
    # governing-tail reduction) — pay up to $1 of EV per $1 of tail actually
    # removed, measured on common random numbers. Effective only alongside
    # ``allow_negative_ev_hedge``; default OFF (byte-identical prior
    # behaviour). Armed together with the P(book) steer after the shadow
    # slate validates.
    hedge_budget_tail_derived: bool = False
    # TAIL-PROBABILITY BOOK GATE (operator anchor ratified 2026-07-25: "more
    # bets = more variance = more money"). When armed the candidate gate's
    # joint-tail budget binds P(post-book loss ≥ the cvar threshold) ≤
    # ``kill_tail_prob`` (worst credible model, Wilson-upper at the ruin CI z)
    # instead of the ES99 average — diversification directly buys capacity;
    # a one-way book stays hard-blocked. Default OFF (byte-identical ES form).
    tail_prob_gate: bool = False
    kill_tail_prob: float = 0.02
    # KILL-ANCHORED BOOK GATE (2026-07-29): DELIBERATELY NOT a LifecycleConfig
    # field. The candidate gate reads the arming flag straight off the SAME
    # ``RiskLimits`` the quote-time cap enforces (``limits.kill_anchored_book_
    # gate``, see ``_candidate_gate_inputs``), so cap and gate can never
    # disagree about the anchor — a second config copy here was a divergence
    # trap (dead field, removed 2026-07-31 audit). A gate looser than the cap
    # reneges on won auctions; a gate stricter than the cap declines flow the
    # cap admitted.
    # AWARD SIZING (2026-07-25 big-fill audit, renege root cause #1): size
    # target-cost candidates at the exchange's actual award (target / taker
    # price, fee-free upper bound) instead of our own cheapest bid — see
    # ``_risk_qty``. Default OFF (byte-identical legacy sizing).
    risk_qty_award_sizing: bool = False
    # WAIVER GAME-SCOPED STABILITY (2026-07-25 peak-flow gap): compare the
    # breached games' position/reservation CONTENT instead of the global
    # generation/version counters, so an unrelated-game fill landing during
    # the enumeration no longer invalidates a certificate it cannot affect.
    # Default OFF (byte-identical global-counter stability).
    waiver_game_scoped_stability: bool = False
    # RELEASE ACCEPTED-QUOTE EXPOSURE (2026-07-25 review HIGH): drop the
    # accepted quote's own resting entry before the confirm-path checks —
    # it is economically dead post-accept (fill replaces it; lapse voids it)
    # and leaving it double-counted this fill's exposure at confirm (~2× the
    # quote-time admission on the summing axes = the renege zone's second
    # half). Default OFF (byte-identical double-count).
    release_accepted_quote_exposure: bool = False
    # GATE EV SOURCE (2026-07-25 big-fill audit, renege root cause #2): when
    # True the candidate gate's ADMISSION EV sign check uses the CALIBRATED
    # PRICING fair's edge (the same number that priced the quote — the
    # backtested moat) instead of the risk copula's band-high EV, which
    # scores heavily-correlated same-game combos structurally negative and
    # reneged 20 won auctions tonight (pricing said +EV, gate said −EV).
    # The TAIL budgets still gate on the conservative risk models — this
    # only changes which fair judges the candidate's own edge. Certified
    # negative-edge hedges still route through the B2 budget. Default OFF.
    gate_ev_from_pricing_fair: bool = False
    # P(BOOK) NON-DECREASE (operator doctrine 2026-07-25: "anything we take
    # in should push it up, or neutral" — 23 of 74 same-day admits LOWERED
    # p_book, worst −0.226). Gate declines ΔP(book) < −3×SE fills unless
    # they certifiably reduce the governing tail. Default OFF.
    require_p_book_non_decreasing: bool = False
    # EV-BASED SLOT EVICTION (2026-07-25 big-fill audit): at the open-quote
    # slot cap, evict the weakest-EV resting quote for a strictly-higher-EV
    # candidate instead of blind-declining by arrival order. Default OFF.
    open_quote_ev_eviction: bool = False
    # FIX 4 VALUE-RANKED DET-MAX ALLOCATION (2026-07-28): at the det-max budget
    # wall, evict the weakest EV-per-consumed-det-max resting quote for a
    # materially denser candidate instead of blind-declining by arrival order.
    # Same mechanism as the slot axis above (one eviction path, not two).
    # Default OFF = SHADOW: the would-be eviction is logged, nothing deleted.
    det_budget_value_ranking: str = "shadow"
    # PEAK-CONCENTRATION pricing steer (operator directive 2026-07-18 evening).
    # K cached worst scorelines per game for the committed-book peak profile
    # (sim/peak_profile.build_peak_profile) — rebuilt OFF the hot path on the
    # maintenance tick, ONLY when the position generation changed (fills/
    # settlements are rare; the build is a committed-only enumeration, ms-scale
    # vs the waiver's trimmed 29.5ms which includes resting quotes). The profile
    # is a PRICING input to the skew seam: stale/absent => the peak component is
    # a hard ZERO adder (neutral), never a decline. Wired from
    # ``pricing.skew.peak_topk_states``.
    peak_topk_states: int = 5
    # MULTI-CLUSTER peak steer (operator directive 2026-07-19): up to
    # ``peak_n_clusters`` DISTINCT loss levels cached per game — the top
    # plateau plus lower level sets at >= ``peak_cluster_min_frac`` (decimal
    # string, exact Fraction, (0, 1]) of the top loss, all under the shared
    # 4096-state cache cap (drop-lowest-first on overflow). 1 = the
    # single-plateau CLUSTER semantics exactly (cluster-view rollback knob;
    # the 2026-07-19 magnitude recalibration applies at every n).
    # Wired from ``pricing.skew.peak_n_clusters`` / ``peak_cluster_min_frac``.
    peak_n_clusters: int = 3
    peak_cluster_min_frac: str = "0.30"


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
    # FIX 3: a stale/absent snapshot takes NO hedge credit (fail closed).
    det_max_hedge_credit_cc: float = 0.0
    p_ruin: float = 0.0
    p_ruin_upper: float = 0.0  # P1-2 (never read on the unusable path)


@dataclass(frozen=True, slots=True)
class _DecayedBookRisk:
    """FIX 5 (2026-07-28). A generation-stale book-risk snapshot CHARGED for the
    book growth it has not seen, instead of thrown away.

    THE DOCTRINE THIS COPIES is already written down in ``risk/deploy_scale.py``:
    "a reservation bumps this counter on every accept, so a hard cliff here made
    the whole feature inert". The book-risk snapshot had exactly that cliff —
    ``_book_risk_for_check`` discarded on ANY generation mismatch — and it
    produced exactly that outcome: measured 2026-07-28, ALL 407 of the session's
    ``skip_portfolio_cvar`` declines were ``book_risk_generation_stale`` and
    nothing else, 289 of them inside one minute against 62 quotes, with the
    live-vs-snapshot generation gap equal to 1 in every single instance. One
    reservation was darkening the whole tail axis.

    THE CHARGE, and why it is sound. ``added_premium_cc`` is the EXACT summed
    ``max_loss_cc`` of the positions present in the live book that were NOT in
    the book the MC priced — computed from the position-ID sets, not from a
    scalar difference, so a simultaneous add-and-remove cannot net out and
    undercharge. Every loss axis is then shifted UP by that amount:

        loss(live book) <= loss(snapshot book) + Σ premium(added positions)

    which holds for ANY joint outcome, because a position's loss can never
    exceed the premium paid for it (``OpenPosition.max_loss_cc``, verified
    ground truth) and REMOVED positions can only ever reduce the loss. The
    charge is comonotone — it assumes every new position loses in full, at the
    same time as the old book's worst case — so it is strictly MORE conservative
    than a fresh measurement of the same live book, never less. Note this is a
    tighter-than-ratio argument: ``deploy_scale`` decays a PERMISSION by the
    premium RATIO, whereas a risk measure admits the exact ADDITIVE bound, and
    additive is the more conservative of the two here (the old ES is at most the
    old premium, so ``es·Δ/premium <= Δ``).

    ABSTAIN BAND. The decay is a BETWEEN-SNAPSHOTS BRIDGE, not a substitute for
    measuring. When the added premium exceeds the premium the snapshot was built
    on — the book has more than DOUBLED since it was measured — the measurement
    no longer describes the book in any useful sense and the caller abstains
    back to the fail-closed sentinel. That bound is the measured book itself,
    not a tolerance number. The TIME-staleness guard is untouched and still
    fails closed on its own.

    WHAT IS NOT DECAYED. Every CREDIT is dropped to zero rather than carried:
    a credit is a loosening, and a loosening certified against a book that no
    longer exists is exactly what must not survive staleness. ``p_ruin`` is
    re-derived from the shifted loss-quantile envelope and then floored at the
    snapshot's own value, so it can only rise. A snapshot with NO envelope
    cannot have its ruin axis charged at all, so the caller refuses to decay it.

    Built ONCE per (snapshot generation, live generation) and cached — the
    quantile shift is O(1001) and must never run per quote."""

    usable: bool
    governing_model_es_99_cc: float
    deterministic_max_loss_cc: float
    mutex_aware_det_max_cc: float | None
    p_ruin: float
    p_ruin_upper: float
    loss_quantiles_cc: tuple[float, ...]
    n_samples: int
    # A stale snapshot's certified offsets are NOT carried (see above).
    det_max_hedge_credit_cc: float = 0.0
    # Provenance for the readout — never a control input.
    added_premium_cc: int = 0
    unsampled_premium_cc: int = 0


def _decay_book_risk(
    snap: BookRiskSnapshot,
    added_premium_cc: int,
    unsampled_premium_cc: int | None = None,
) -> _DecayedBookRisk | None:
    """FIX 5. Charge ``snap`` for the book growth it never saw, returning the
    more-conservative view — or None when the snapshot cannot be charged soundly
    (the caller then keeps today's fail-closed discard).

    TWO CHARGES, because the two axes are exposed to different things:

      * ``added_premium_cc`` — premium of live positions the snapshot did not
        HOLD at all. Charged to the DETERMINISTIC axis, where it is exact:
        det-max is literally a sum of premiums, so adding the new positions'
        premium reproduces the new det-max to the cent.
      * ``unsampled_premium_cc`` — premium of live positions the snapshot did not
        SAMPLE, which is a SUPERSET: it also includes positions the snapshot held
        only as a flat RESERVE (an unpriceable leg) and which have since become
        modeled. Those contribute nothing to the snapshot's sampled model ES, so
        when their leg book comes online the ES rises with NO change in total
        premium — measured live 2026-07-28 08:46:45 → 08:47:00, governing ES
        +$16.44 against det-max flat at $777.66. Charging the ES/tail axes only
        for absent positions would have walked straight past that, so those axes
        take the larger charge. Defaults to ``added_premium_cc`` when the caller
        cannot distinguish the two (never smaller — fail toward the larger).

    Returns None when: the snapshot is unusable (an UNKNOWN book is not made
    knowable by charging it more), or it carries no loss-quantile envelope (the
    ruin axis then has no sound charge available, and shipping an uncharged ruin
    probability alongside charged loss axes would be a silent free pass on the
    one axis that was not decayed).

    Pure; cost is O(len(loss_quantiles)) — call once per generation pair."""
    if not snap.usable:
        return None
    quantiles = snap.loss_quantiles_cc
    if not quantiles:
        return None
    delta = float(max(0, added_premium_cc))
    tail_delta = delta if unsampled_premium_cc is None else float(
        max(0, unsampled_premium_cc)
    )
    tail_delta = max(tail_delta, delta)  # never charge the tail axes less
    # Every LOSS quantile rises by at most the unsampled premium (comonotone
    # worst case for every position the model did not price); shifting the whole
    # envelope keeps it sorted, so the tail-probability cap reads a strictly more
    # adverse distribution than it did before.
    shifted = tuple(q + tail_delta for q in quantiles)
    # P(ruin) is exactly P(book loss > the ruin loss threshold), so with the
    # threshold carried on the snapshot the charged probability is a direct
    # re-read of the SHIFTED envelope — no guessing where the threshold sits.
    # (Inferring it from p_ruin alone is not viable: a book with p_ruin = 0 only
    # proves the threshold is somewhere ABOVE the largest sampled loss, and
    # assuming it sits exactly there invented a 34.7% ruin probability on a book
    # nowhere near ruin, which would have made the whole decay unusable.)
    # Floored at the snapshot's own values so the charge can only ever RAISE ruin.
    denom = max(1, len(quantiles) - 1)
    threshold = snap.ruin_loss_threshold_cc
    if threshold is None:
        # No equity/bankroll reading ⇒ the ruin cap does not evaluate on this
        # snapshot and there is nothing to shift; carry its value through.
        decayed_ruin = snap.p_ruin
    else:
        decayed_ruin = sum(1 for q in shifted if q > threshold) / denom
    p_ruin = min(1.0, max(snap.p_ruin, decayed_ruin))
    p_ruin_upper = min(
        1.0, max(getattr(snap, "p_ruin_upper", snap.p_ruin), decayed_ruin)
    )
    mutex = snap.mutex_aware_det_max_cc
    return _DecayedBookRisk(
        usable=True,
        # SAMPLED tail axes take the (larger) unsampled charge; the DETERMINISTIC
        # axes take the exact absent-position charge — a position the snapshot
        # already held as a reserve is already inside its det-max.
        governing_model_es_99_cc=snap.governing_model_es_99_cc + tail_delta,
        deterministic_max_loss_cc=snap.deterministic_max_loss_cc + delta,
        mutex_aware_det_max_cc=None if mutex is None else mutex + delta,
        p_ruin=p_ruin,
        p_ruin_upper=p_ruin_upper,
        loss_quantiles_cc=shifted,
        n_samples=snap.n_samples,
        added_premium_cc=int(added_premium_cc),
        unsampled_premium_cc=int(tail_delta),
    )


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
    # WALL-clock epoch seconds of the same confirm-success moment (2026-07-18
    # review): the ``min_ts`` anchor that scopes the cancel-verification
    # /portfolio/fills query to THIS quote's execution window instead of the
    # ticker's whole recent tape (which holds identical-count historical fills).
    fill_confirmed_wall_ts: int | None = None
    # True once this quote's fills-ledger row exists (recorded, or confirmed
    # present on a replay) — the sweep's terminal success state.
    fill_recorded: bool = False
    # REST polls spent recovering this quote (bounded; exhausted ⇒ loud metric).
    fill_recovery_attempts: int = 0
    # UNRESOLVED WITHDRAWAL (B3, 2026-07-26). Set when a DELETE neither acked
    # nor 404'd (429 / 5xx / timeout), or was deferred by a wall budget before
    # it was even asked: the quote may still be RESTING and FILLABLE, so it
    # stays in the mirror. The reason it was being withdrawn for is kept so the
    # withdraw-pending RESOLVER can re-drive the SAME withdrawal — including for
    # the event-driven reasons (RFQ gone, risk eviction) whose trigger never
    # fires twice. This is the UNKNOWN marker risk, reprice and eviction read.
    # None ⇒ not being withdrawn.
    withdraw_pending_reason: ReasonCode | str | None = None
    # Monotonic ns at which the most recent withdrawal REQUEST for this quote
    # finished without proving anything (2026-07-26, replacing the write-only
    # ``withdraw_attempts`` counter). This is the HAPPENS-BEFORE key of the read
    # resolver: only a quote whose ask STRICTLY PRE-DATES the open-quote list
    # request may be resolved by its absence from that list. A quote deleted
    # after the list was issued could legitimately still appear in it (or have
    # been dropped between the two), so it is never judged by that read — the
    # read-after-write hazard is killed by construction, not by a delay.
    withdraw_asked_mono_ns: int = 0
    # CANCEL-REPORT VERIFICATION (2026-07-18 incidents A+B). Set (monotonic ns)
    # when REST reported this CONFIRMED quote as CANCELLED — the point
    # verify-before-discard starts. While set, the position stays booked
    # (fail-safe: risk keeps counting) and the sweep polls /portfolio/fills on
    # the configured cadence instead of trusting the cancel report.
    cancel_verify_started_mono_ns: int | None = None
    # /portfolio/fills polls spent (bounded by fill_cancel_verify_attempts).
    cancel_verify_attempts: int = 0
    # Polls that READ successfully (a no-match only counts as evidence of
    # absence when we actually read the fills tape; all-errors ⇒ unresolved).
    cancel_verify_ok_reads: int = 0
    # Fully-errored verification ROUNDS completed (2026-07-18 review): each
    # all-errors round is retried on the same cadence up to
    # _CANCEL_VERIFY_MAX_ROUNDS before the loud unresolved give-up, so a
    # transient 429 storm cannot pin a phantom's budget until restart.
    cancel_verify_rounds: int = 0
    # The matched exchange fill once verification FINDS the execution — the
    # terminal "this fill is REAL" state; the ledger row is then written via
    # the normal on_quote_executed path (retried boundedly if the write fails).
    cancel_verified_fill: JsonDict | None = None
    # EXACT verification key (2026-07-24 incident-C review): the quote
    # payload's ``creator_order_id`` (docs: Quote.creator_order_id ==
    # Fill.order_id after execution) captured off the CANCELLED-status
    # payload. When present, verification queries + matches by THIS id only —
    # structural guessing is the fallback, never the primary.
    cancel_expected_order_id: str | None = None
    # Set when a structurally-plausible PARTIAL group was REFUSED because the
    # evidence was ambiguous (no expected order id + another in-flight quote
    # on the same ticker). Ambiguous evidence must never conclude "genuinely
    # absent" — resolution keeps the position instead of discarding.
    cancel_verify_ambiguous: bool = False
    # The cancel report's cancellation_reason, kept for the resolution log.
    cancel_reported_reason: str | None = None


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
        fills_getter: FillsGetter | None = None,
        fills_subaccount: int | None = None,
        beat: Callable[[], None] | None = None,
        rfq_alive: Callable[[str], bool] | None = None,
        settled_marginals: SettledMarginalResolver | None = None,
        withdraw_budget: WriteBudget | None = None,
        quote_lister: QuoteLister | None = None,
        read_budget: TokenBudget | None = None,
    ) -> None:
        self._clock = clock
        self._sender = sender
        # WRITE-TOKEN BUDGET for the quote-withdrawal burst (see the BOUNDED
        # QUOTE WITHDRAWAL block). Injected by quote_app from the operator's
        # SINGLE budget knob (``supervisor.write_budget_capacity`` /
        # ``write_budget_refill_s``); when nothing is injected (backtests, paper,
        # tests) we build the SAME bucket from the SAME config object's
        # defaults, so there is no second literal anywhere and the withdrawal
        # path is paced even if a caller forgets to wire it (fail-closed).
        self._withdraw_budget = withdraw_budget or _default_write_budget(clock)
        # FIFO ADMISSION to that bucket (2026-07-27, the 07-27 maintenance
        # stall). One lock for the whole lifecycle — the bucket is one shared
        # resource, so the queue for it has to be one queue. See
        # ``_spend_withdraw_tokens`` for why a bare ``try_spend`` loop starves.
        self._withdraw_gate = asyncio.Lock()
        # The LAST withdrawal batch's failure modes, by kind. Written by
        # ``_withdraw_batch`` (the one place an outcome is classified), read by
        # quote_app's quarantine enforcement to attribute an UNENFORCED
        # quarantine. Observability only — nothing reads it to make a
        # withdrawal decision, so it can never change what we cancel.
        self._last_withdraw_failure_kinds: Counter[str] = Counter()
        # WITHDRAW-PENDING RESOLVER (2026-07-26). ``quote_lister`` is the
        # account-wide open-quote enumerator (the SAME
        # ``exchange/quote_query.list_open_quotes`` helper the startup reconcile
        # and the supervisor's kill path use, min_ts/max_ts windowed so it never
        # trips the exchange circuit-breaker); ``read_budget`` is quote_app's ONE
        # read bucket, so the resolver's GET charges the same place every other
        # read charges. Unwired (paper / backtests / minimal rigs) ⇒ no read ⇒
        # the resolver degrades to the METERED write drain, which still
        # terminates (a re-DELETE of a gone quote 404s = provably gone) — it just
        # cannot observe an accept/execute transition. Fail-closed either way:
        # nothing is ever dropped without proof.
        self._quote_lister = quote_lister
        self._read_budget = read_budget
        # Monotonic ns of the last SUCCESSFUL open-quote read. Feeds the
        # all-pending-at-capacity terminal predicate: a book that is full, wholly
        # UNKNOWN, and has not been read since the oldest ask cannot be proven at
        # all, and an unprovable book is HALT_NEEDS_RECONCILE (the same doctrine
        # ``cancel_quotes_touching`` and ``_startup_reconcile`` already state).
        self._withdraw_read_ok_mono_ns: int | None = None
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
        # FIX 5 (book-growth decay of a generation-stale snapshot). The position
        # id → max_loss_cc map the LATEST snapshot was built on, captured at
        # publish; the growth charge is the summed premium of live positions
        # absent from it. None ⇒ nothing published yet ⇒ no decay is possible.
        self._book_risk_positions: dict[str, int] | None = None
        # (generation, position ids the model at that generation actually
        # SAMPLED). A position HELD but only RESERVED sits outside the sampled
        # model ES, so the tail axes must be charged for it even though it is
        # already inside the snapshot's det-max — see ``_decay_book_risk``.
        self._book_risk_sampled: tuple[int, set[str]] | None = None
        # The decayed view, memoised on (snapshot generation, live generation) —
        # the quantile shift is O(1001) and must never run per quote. The third
        # slot is None when this generation pair is NOT decayable (abstained).
        self._decayed_book_risk: (
            tuple[int, int, _DecayedBookRisk | None] | None
        ) = None
        # Throttle the MC recompute to comfortably inside the freshness window
        # (half of it) so the snapshot stays fresh without running a full MC every
        # 0.5s maintenance tick. None ⇒ never refreshed yet.
        self._book_risk_refresh_mono_ns: int | None = None
        # BOOT-WARMUP QUOTE GATE (2026-07-31, the 10:11:12Z boot reneges).
        # Quote SENDING is held at startup until the FIRST moment the confirm
        # path's own usability predicate (``_book_risk_for_check``: empty book
        # ⇒ None ⇒ nothing to measure, else a usable generation-matched fresh
        # snapshot) reads open — the SAME predicate, never a duplicate, so the
        # gate can never be looser or stricter than the confirm that would
        # renege the fill. ONE-WAY latch: once open it stays open for the
        # process lifetime, so mid-run staleness behaviour is UNCHANGED (only
        # the boot window is new). ``_warmup_last_warn_mono_ns`` throttles the
        # loud still-holding warning to once per snapshot-freshness window
        # (``book_risk_stale_after_s`` — the system's own staleness horizon,
        # not a new knob).
        self._quote_warmup_open: bool = False
        self._quote_warmup_start_mono_ns: int = clock.monotonic_ns()
        self._warmup_last_warn_mono_ns: int | None = None
        # P2-2: the in-flight OFF-LOOP recompute task, if any. The maintenance tick
        # LAUNCHES the off-loop MC as a background task and returns IMMEDIATELY (it
        # never awaits the MC), so the maintenance loop keeps beating the heartbeat
        # on its 0.5s cadence while the MC runs in a worker process. A single-flight
        # guard: a new recompute is not launched while the previous one is still
        # running (its result publishes when it finishes).
        self._book_risk_task: asyncio.Task[None] | None = None
        # DEPLOYMENT SCALE (risk/deploy_scale.py) — the SOLVED multiple of the
        # current book the live envelope still permits. Refreshed on the SAME
        # off-loop cadence as the book-risk snapshot (never on the quote path:
        # the hot path only READS this float). 1.0 until a solve succeeds, and
        # 1.0 forever while ``deploy_scale_enabled`` is off ⇒ byte-identical.
        self._deploy_scale: DeployScaleResult = DEPLOY_SCALE_FAILSAFE
        self._deploy_scale_refresh_mono_ns: int | None = None
        self._deploy_scale_task: asyncio.Task[None] | None = None
        # Committed premium-at-risk, cached on the position generation — the
        # denominator of the deployment scale's book-growth decay. Recomputed
        # only when the position set changes, never per quote.
        self._committed_premium_cache: tuple[int, int] | None = None
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
        # PEAK-CONCENTRATION steer (2026-07-18): the cached committed-book
        # peak-state profile (sim/peak_profile.py), rebuilt off the hot path on
        # position-generation change (_maybe_recompute_peak_profile) and read
        # at quote time by _quoting_policy as a PRICING input to the skew. A
        # stale/absent profile is a hard ZERO adder (neutral) — never a
        # decline. ``_peak_profile_failed_generation`` rate-limits rebuild
        # retries after a build error to once per generation (no exception loop
        # on the 0.5s maintenance cadence).
        self._peak_profile: PeakProfile | None = None
        self._peak_profile_failed_generation: int | None = None
        # P(BOOK) STEER profile, Phase B1 (2026-07-25): the cached P(book) +
        # per-game tail-share profile, published from every ACCEPTED book-risk
        # snapshot (same generation-stamp discipline as the snapshot itself).
        # Absent/stale ⇒ the skew's pbook component is a hard ZERO (neutral).
        self._pbook_profile: PBookProfile | None = None
        # LEVER #5 (2026-07-27) — the ECONOMICALLY-REAL concentration steer.
        # ``_loss_event_book`` is the committed book as AND-BOUND LOSS EVENTS
        # in premium dollars, rebuilt ONLY when the position generation moves
        # (a fill or a settlement), so the quote path pays the O(1) Herfindahl
        # marginal (1.47us) and never the O(n_positions) rebuild.
        # ``_steer_centre`` is the live measured mean+dispersion of the steer
        # score: the BUDGET-NEUTRALITY mechanism (markups are FIXED, so the
        # steer reallocates and must never widen the average quote) and the
        # standardiser that makes the swing economically real.
        # Key = (position_generation, settled-facts generation) — the second
        # element is -1 while skew settled-fact resolution is OFF (byte-
        # identical: a constant key element), and the resolver's monotone fact
        # count when ON (2026-08-01: facts land at boot WITHOUT a position-
        # generation move; a gen-only key would pin the unresolved book).
        self._loss_event_book: LossEventBook = build_loss_event_book(())
        self._loss_event_generation: tuple[int, int] = (-1, -1)
        self._steer_centre = SteerCenter()
        # LEG-AXIS PROFILE CACHE (2026-07-27 throughput). The (family x side) /
        # (entity x side) shares read ONLY ``ExposureBook.positions`` — the
        # COMMITTED book (``exposure.snapshot`` fills
        # ``committed_loss_by_*_cc`` from ``self.positions`` alone, resting
        # candidates go to the separate enforced map) — so the whole profile is
        # a pure function of ``position_generation`` and rebuilding its two
        # share dicts on EVERY quote was waste. Keyed exactly like the
        # loss-event book and the peak/P(book) caches.
        self._leg_axis_profile: LegAxisProfile | None = None
        self._leg_axis_profile_key: tuple[int, float | None, int] = (-1, None, -1)
        # The already-paid-for CRN sample (PRICING joint) behind
        # ``Cov(candidate payoff, pre-existing book P&L)`` — the measured,
        # EV-orthogonal price of concentration (SE 0.0161 c/contract against a
        # 1.1333 c/contract spread => SNR 70.4). Generation-stamped: stale =>
        # the steer falls back to the ZERO-standard-error Herfindahl reading.
        self._crn_cache: CrnBookCache | None = None
        # The frozen BookModel the CRN cache samples from (stamped with the
        # generation it was read at, so a superseded model can never publish).
        self._last_book_inputs: Any | None = None
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
        # CANCEL-REPORT VERIFICATION (2026-07-18): the /portfolio/fills GET
        # slice verify-before-discard polls before believing a CANCELLED status
        # on a confirmed quote (both 2026-07-18 incidents executed as
        # taker-style regular orders while the quote status said cancelled).
        # None (paper/backtests/minimal rigs) ⇒ the prior immediate discard.
        # ``fills_subaccount`` pins the read to our ONE subaccount at the query
        # layer (P0-5 doctrine), matching every other portfolio read.
        self._fills_getter = fills_getter
        self._fills_subaccount = fills_subaccount
        # CLAIM SET (2026-07-18 review): exchange order_ids adopted by an
        # IN-FLIGHT verification whose ledger row has not landed yet. Two
        # concurrently-verifying quotes structurally matching the SAME exchange
        # fill must not both adopt it — the first claims the order_id here; the
        # claim is released once the fills row exists (the ledger's own
        # order_id guard takes over from there).
        self._claimed_exchange_order_ids: set[str] = set()
        # FILLS-LEDGER SWEEP cadence/window state (2026-07-24 incident C):
        # monotonic stamp of the last sweep (None = first tick stamps, no
        # fetch) and the advancing min_ts watermark — held back to the OLDEST
        # unresolved miss so a persistent miss re-alarms every interval.
        self._fills_sweep_last_mono_ns: int | None = None
        self._fills_sweep_min_ts: int | None = None
        # POSITION-LEDGER DIVERGENCE cadence (2026-07-26): monotonic stamp of
        # the last open-positions-vs-open-ledger-rows count. None = never run;
        # the FIRST maintenance tick runs it (a boot-time read, right after
        # rehydration, is the most valuable one) and then it throttles.
        self._ledger_divergence_last_mono_ns: int | None = None
        # OFF-LOOP DIAGNOSTIC SWEEPS (2026-07-26): the single-flight task handle
        # per alarm-only sweep. A sweep still running when its next launch comes
        # due is SKIPPED (never stacked) and the skip is counted, so a store that
        # is permanently too slow is loud instead of silently piling up tasks.
        self._diag_tasks: dict[str, asyncio.Task[None]] = {}
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
        # IN-PLAY SHADOW (2026-07-25, measurement only): the RFQ work queue's
        # depth probe (quote_app attaches ``rfq_work.qsize`` post-construction,
        # the attach_reservation pattern — the queue is created after this
        # lifecycle). The shadow prices an in-play-skipped RFQ ONLY when this
        # reads 0 (no queued live work to delay); None (tests/backtests/paper
        # rigs without the pool) reads as idle — the shadow is measurement-only
        # and cannot affect quoting there. ``_inplay_shadow_inflight`` is the
        # single-flight guard: at most ONE shadow pricing ever runs at a time.
        self._rfq_backlog_depth: Callable[[], int] | None = None
        self._inplay_shadow_inflight = False
        # rfq_ids already RECORDED by the shadow (insertion-ordered, FIFO-
        # evicted at _INPLAY_SHADOW_DEDUPE_CAP): the pending-retry loop re-runs
        # handle_rfq on skipped RFQs, and one RFQ must yield ONE row. Marked
        # only on a successful record, so a warming-book NoQuote may retry.
        self._inplay_shadow_done: dict[str, None] = {}
        # SETTLED-LEG MARGINAL RESOLUTION (2026-07-18 live outage: FRAENG
        # settled while cross-game combos with FRAENG legs stayed open — the
        # settled legs' feed books were gone, so build_book_model went UNKNOWN
        # and the CVaR cap failed closed on EVERY quote for hours). When wired
        # (quote_app, risk.settled_marginal_resolution), ``_marginals`` falls
        # back — feed first, settled-cache second, else UNKNOWN — to the
        # exchange-GRADED settlement fact (GET /markets/{ticker} result:
        # yes ⇒ 1.0 / no ⇒ 0.0, accepted only under status determined/
        # finalized), permanently cached and fetched OFF the hot path by
        # ``_maybe_resolve_settled_marginals`` on the maintenance tick. None
        # (tests/backtests/knob off) ⇒ the prior fail-closed behaviour.
        self._settled = settled_marginals
        # Single-flight fetch task (the book-risk-task pattern in miniature).
        self._settled_task: asyncio.Task[int] | None = None
        # Committed-position leg tickers, cached per position generation, so
        # the hot-path fallback only ever REGISTERS resolution candidates for
        # legs we actually HOLD (an RFQ leg with no book stays a plain
        # no-quote — settled resolution exists to repair the BOOK model, never
        # to admit quoting on dead markets).
        self._committed_leg_cache: tuple[int, frozenset[str]] | None = None
        # F1 PRE-PRICING GATE cache: (exposure generation, bankroll_cc, built
        # mono_ns, breaches). Every allowlisted cap input is either static per
        # book mutation (loss/notional folds, quote count — invalidated by the
        # GENERATION key) or the bankroll itself (its own key); the ≤0.5s age
        # bound is belt-and-suspenders for metadata drift (ME answers), matching
        # the maintenance-tick granularity. A falsely-CACHED verdict can only
        # DECLINE (never admit), and retry_pending re-checks within 1s anyway.
        self._pre_gate_cache: tuple[int, int | None, int, list[Breach]] | None = None
        # FIX 4 ANTI-THRASH LEDGER (2026-07-28): combo_ticker -> the EV/det
        # density of the candidate that displaced a quote on it. Insertion-
        # ordered dict used as a bounded FIFO (``_EVICTION_LEDGER_MAX``). See
        # ``_thrash_blocked`` for the invariant it enforces.
        self._evicted_density: dict[str, float] = {}
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
        # ET calendar date the realized accumulator belongs to (day-rollover
        # reset — see _roll_realized_day). None until the first mark.
        self._realized_day: str | None = None
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

    def attach_rfq_backlog_probe(self, probe: Callable[[], int]) -> None:
        """Wire the RFQ work queue's depth probe in AFTER construction (the
        queue is created inside quote_app.run, after this lifecycle exists —
        the attach_reservation pattern). The in-play shadow's throughput gate:
        it prices only when this reads 0 (the pool is provably idle)."""
        self._rfq_backlog_depth = probe

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
            # P&L-SPACE pair (2026-07-21 review): peak/current of equity minus
            # detected external transfers, so a deposit is never give-back
            # headroom and a withdrawal is never a drawdown. The raw
            # high-water accessors remain for reports only.
            peak_equity_cc=self._balance.peak_pnl_cc_or_none(),
            current_equity_cc=self._balance.pnl_equity_cc_or_none(),
            # Settlement-cascade shield (2026-07-19 false-positive kill): the
            # give-back halts subtract KNOWN-outcome in-flight settlement
            # credits from the measured give-back (floored at 0 in the checker).
            pending_settlement_credit_cc=self._balance.pending_receivables_cc(),
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
            # FIX 2: the EXCHANGE'S OWN determinations, so the deterministic
            # max-loss axis can retire a combo whose outcome is already decided.
            # Deliberately NOT ``self._marginals`` — that walks the live feed
            # first and would let a market pinned at 0/100 masquerade as settled.
            settled_facts=self._settled_fact,
        )
        inputs = BookRiskInputs(
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
            # FIX 2 arming (shadow default). The settled credit is MEASURED
            # either way; this decides only whether it is SUBTRACTED from the
            # enforced det-max axes.
            det_max_settlement_aware=self._det_max_settlement_aware(),
            input_generation=gen,
            # P(NIGHT) (2026-07-25 operator KPI): realized-so-far (settlements
            # + fees; process-scoped until the day-anchored ledger lands).
            realized_pnl_cc=self._realized_pnl_cc,
        )
        # LEVER #5: keep the frozen model + its generation so the CRN cache can
        # be drawn from EXACTLY the book the MC priced, on the same slow loop
        # (never the quote path). Superseded generations can never publish.
        self._last_book_inputs = inputs
        # FIX 5: record WHICH positions this model actually SAMPLED, at BUILD
        # time (not publish — the priceability of a leg can move in between, and
        # the charge basis must describe the book the MC really priced).
        #
        # WHY A SECOND SET, and not just "was it in the book". A position that is
        # RESERVED (an unpriceable leg) sits OUTSIDE the sampled model ES and
        # inside the flat deterministic reserve. When its leg book comes online
        # it becomes SAMPLED, and the model ES rises even though total premium —
        # and therefore det-max — has not moved by a cent. Measured live on
        # 2026-07-28 08:46:45 → 08:47:00: governing ES +$16.44 with det-max
        # unchanged at $777.66. Charging the ES axis only for positions that were
        # ABSENT would have missed that entirely, so the ES axis is charged for
        # every live position the snapshot did not SAMPLE, while the det-max axis
        # is charged only for positions it did not HOLD (a reserved position's
        # premium is already inside its reserve — charging it twice would be
        # sound but so tight the bridge would never help).
        self._book_risk_sampled = (
            gen,
            {
                p.position_id
                for p in select_modeled_positions(
                    positions, lambda t: self._marginals(t) is not None
                )[0]
            },
        )
        return inputs

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
        # FIX 5: remember WHICH positions (and at what premium) this snapshot
        # priced. On a later generation mismatch the growth charge is computed
        # from the position-ID SETS, not from a scalar total, so a simultaneous
        # add-and-remove between generations cannot net out and undercharge.
        # Cheap: one dict of ~book-size ints, rebuilt only when a snapshot
        # publishes (the slow loop), never on the quote path.
        self._book_risk_positions = {
            pid: p.max_loss_cc for pid, p in self._exposure.positions.items()
        }
        self._decayed_book_risk = None
        # P(BOOK) STEER profile (2026-07-25): publish the measured P(book) +
        # per-game tail shares alongside the snapshot — the skew's pbook
        # component reads this cached profile at quote time (generation-
        # checked there; a stale profile is a hard ZERO adder). Only a USABLE
        # snapshot publishes (an UNKNOWN book must not steer anything). The
        # ENFORCED per-game budget is computed HERE (the one owner with the
        # live bankroll): min(hard game $cap, game_loss_frac × bank, KILL
        # frac × bank) — the tightest enforced loss bound a one-way game can
        # burn (2026-07-25 review: the static SkewLimits dollar made the
        # steer inert at 7/23 scale; this adapts with the bankroll).
        if snap.usable:
            self._pbook_profile = _pbook_profile_from_snapshot(
                snap, game_budget_cc=self._enforced_game_budget_cc()
            )
            self._log_leg_axis_exposure()
            self._publish_crn_cache(snap.input_generation)
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
                # KILL-ANCHOR SHADOW READ-OUT (2026-07-29) — the number the
                # operator ARMS from, and the one that has been invisible: the
                # book's P(loss ≥ the ratified 12% KILL line) and that line in
                # cc. Emitted on EVERY snapshot regardless of the arming flag,
                # so a shadow slate is readable straight off the tape without a
                # replay. Computed HERE, on the maintenance tick that publishes
                # the snapshot — never on the quote path (fix isolation +
                # throughput: ``check`` is untouched by this read-out).
                **self._kill_anchor_readout(snap),
                # P(BOOK) VISIBILITY (2026-07-25 operator directive: P(book)
                # must steer — Phase A publishes the signal every refresh):
                # P(book P&L > 0), the book EV, and the top tail-concentration
                # games (which game dominates the downside = the anti-variance
                # concentration P(book) steering will price against).
                p_book=round(snap.p_profit, 4),
                # AXIS SPLIT (2026-07-26): p_book/p_night/ev ride the exact
                # per-pair PRICING joint (``location_joint`` at
                # ``location_band``); the ES/ruin/loss-quantile gates ride the
                # game-collapsed TAIL-DEPENDENCE STRESS joint (``tail_joint`` at
                # the adverse ``band``). The band alone does NOT identify a joint
                # — the two differ AT THE SAME BAND — so both the band and the
                # joint NAME are stamped and a log line is unambiguous.
                band=snap.band,
                tail_joint=snap.tail_joint,
                location_band=snap.location_band,
                location_joint=snap.location_joint,
                # P(NIGHT) (2026-07-25 operator KPI): realized + open book —
                # does not reset as winners settle out; the headline number.
                p_night=round(snap.p_night, 4),
                realized_pnl_cc=self._realized_pnl_cc,
                ev_cc=int(snap.ev_cc),
                top_tail_games=[
                    (tc.key, int(tc.loss_cc))
                    for tc in sorted(
                        snap.per_game_tail_cc, key=lambda tc: -tc.loss_cc
                    )[:3]
                ],
            )

    def _kill_anchor_readout(self, snap: object) -> dict[str, object]:
        """SHADOW READ-OUT for the KILL-anchored book gate (2026-07-29).

        Returns ``{kill_line_cc, p_kill_night}`` — the ratified 12%-of-bankroll
        KILL line and the book's P(loss ≥ that line), read off the SAME
        loss-quantile envelope ``risk/limits`` §(8a) gates on (same conservative
        round-UP point count, so the logged number IS the number the armed gate
        would test). Empty dict when the bankroll or the envelope is
        unavailable — an absent read-out is never invented.

        ``det_max_backstop_cc`` is the RATIFIED (2026-07-31) armed det-max
        wall — ``cap_family.det_max_backstop_frac() x bankroll`` — published on
        every snapshot so the operator can see, before arming, exactly where
        the demoted wall would sit next to the book's premium.

        Blast radius: telemetry only. It runs on the maintenance tick that
        publishes a snapshot, never on the quote path, and any failure is
        swallowed so a logging bug cannot reach pricing or a risk decision.
        """
        try:
            bankroll_cc = self._risk_bankroll_cc()
            if bankroll_cc is None or bankroll_cc <= 0:
                return {}
            limits = self._limits.limits
            kill_line_cc = int(threshold_cc(limits.hard_trip_frac, bankroll_cc))
            out: dict[str, object] = {
                "kill_line_cc": kill_line_cc,
                # The RATIFIED armed det-max position (demotion, 2026-07-31).
                "det_max_backstop_cc": int(
                    threshold_cc(det_max_backstop_frac(), bankroll_cc)
                ),
            }
            quantiles = getattr(snap, "loss_quantiles_cc", ()) or ()
            if quantiles:
                n_grid = len(quantiles)
                k_ge = sum(1 for q in quantiles if q >= kill_line_cc)
                out["p_kill_night"] = round(k_ge / max(1, n_grid - 1), 5)
            return out
        except Exception:  # pragma: no cover - telemetry only
            return {}

    def _enforced_game_budget_cc(self) -> float:
        """The tightest ENFORCED per-game loss budget in cc: min(hard game
        $cap, game_loss_frac × bankroll, hard_trip/KILL frac × bankroll).
        The ONE denominator every steering onset divides by (P(book) axis and
        leg-direction axis) — computed here, the owner with the live bankroll,
        so it adapts with equity and the derived-cap swap (2026-07-25 review:
        a static dollar made the steer inert at 7/23 scale)."""
        limits = self._limits.limits
        budget_candidates = [
            limits.max_event_worst_case_loss_dollars * 10_000.0
        ]
        bankroll_cc = self._risk_bankroll_cc()
        if bankroll_cc is not None and bankroll_cc > 0:
            budget_candidates.append(
                float(threshold_cc(limits.game_loss_frac, bankroll_cc))
            )
            budget_candidates.append(
                float(threshold_cc(limits.hard_trip_frac, bankroll_cc))
            )
        return min(budget_candidates)

    def _leg_axis_profile_from(self, snap: ExposureSnapshot) -> LegAxisProfile:
        """Build the leg-direction steering profile from THIS quote's exposure
        snapshot (already computed for the limits — no extra aggregation).
        Shares normalize the committed (family × side) / (entity × side) loss
        attributions; ``p_book`` rides the cached MC profile ONLY when it is
        generation-fresh (stale ⇒ None ⇒ the component is neutral: UNKNOWN
        never widens).

        THE BUDGETS ARE TELEMETRY NOW (operator directive 2026-07-27). They no
        longer enter the component's magnitude at all: a wall in the
        denominator with the BOOK'S OWN dollars in the numerator is exactly the
        book-size term the operator ruled out, and it is why the
        ``family_wall_cc = game_wall_cc`` mis-assignment mattered. The axis
        reads SHARE against its own dollar-Herfindahl instead — both pure
        ratios — and the enforced walls stay where they belong: the REFUSAL
        layer in ``risk/limits``. They are still published here so the operator
        readout can show how loaded each axis is."""
        p_book: float | None = None
        profile = self._pbook_profile
        gen = self._exposure.position_generation
        if profile is not None and profile.input_generation == gen:
            p_book = profile.p_book
        # CACHED ON THE POSITION GENERATION (2026-07-27 throughput): the shares
        # below read the COMMITTED book only, so they cannot move between two
        # quotes at the same generation. ``p_book`` is part of the key so a
        # refreshed MC read still lands immediately. The facts-generation
        # element (2026-08-01) is a CONSTANT -1 while skew settled-fact
        # resolution is off; when on, a graded fact landing at a static
        # position generation must rebuild the resolved shares (the snapshot
        # this profile is built FROM already resolved them).
        key = (gen, p_book, self._skew_facts_generation())
        cached = self._leg_axis_profile
        if cached is not None and self._leg_axis_profile_key == key:
            return cached
        fam = snap.committed_loss_by_family_cc
        ent = snap.committed_loss_by_entity_cc
        total_fam = float(sum(fam.values()))
        total_ent = float(sum(ent.values()))
        fam_shares = (
            {k: v / total_fam for k, v in fam.items()} if total_fam > 0 else {}
        )
        ent_shares = (
            {k: v / total_ent for k, v in ent.items()} if total_ent > 0 else {}
        )
        built = LegAxisProfile(
            shares_by_family=fam_shares,
            total_family_cc=total_fam,
            shares_by_entity=ent_shares,
            total_entity_cc=total_ent,
            # Each axis's dollar-Herfindahl, computed HERE with the shares (once
            # per position generation) rather than re-summed over 79 entity keys
            # on the quote path.
            hhi_family=math.fsum(s * s for s in fam_shares.values()),
            hhi_entity=math.fsum(s * s for s in ent_shares.values()),
            family_budget_cc=self._enforced_game_budget_cc(),
            entity_budget_cc=self._enforced_entity_budget_cc(),
            p_book=p_book,
        )
        self._leg_axis_profile = built
        self._leg_axis_profile_key = key
        return built

    def _log_entity_admission(
        self, cert: ConcentrationCertificate, ceiling_cc: int, would_admit: bool
    ) -> None:
        """TIERED-ENTITY SHADOW READ-OUT: one structured line per entity-axis
        certificate, so admissions and refusals are countable on the tape in
        DOLLARS and TIERS (the operator's measure) instead of inferred from a
        decline tally. ``would_admit`` is the decision the ARMED cap would take —
        the number the operator arms from while the cap still refuses. The row
        carries the breaching key's own PRIOR vs ADDED dollars and its tier on
        both sides, so an ACCUMULATION refusal and a SIZE admission are
        distinguishable on the tape without re-deriving anything. Fires only when
        a key has actually breached, so it is bounded by the breach rate, not the
        RFQ rate. Fix-isolation: a logging failure can never reach the pricing
        path or change a risk decision — it is swallowed here."""
        try:
            log.info(
                "entity_tier_admission",
                key=cert.key,
                would_admit=bool(would_admit),
                certified=bool(cert.certified),
                verdict=cert.verdict,
                key_loss_cc=cert.key_loss_cc,
                prior_cc=cert.prior_cc,
                add_cc=cert.add_cc,
                prior_tier=cert.prior_tier,
                post_tier=cert.post_tier,
                diversifying_cc=cert.diversifying_cc,
                concentrating_cc=cert.concentrating_cc,
                candidate_total_cc=cert.candidate_total_cc,
                widen_pct=round(cert.widen_weight_pct, 2),
                cool_keys=cert.n_cool_keys,
                loaded_keys=cert.n_loaded_keys,
                ceiling_cc=int(ceiling_cc),
            )
        except Exception:  # pragma: no cover - telemetry must never propagate
            pass

    def _log_slate_partition(
        self, slate: str, naive_cc: int, partitioned_cc: int, threshold_cc: int
    ) -> None:
        """FIX 2 SHADOW READ-OUT: for every slate the NAIVE Σ-per-game roll-up
        would refuse, the once-counted ENUMERATED JOINT WORST CASE beside it and
        whether that corrected number still breaches. Fires only on a would-be
        refusal. Telemetry only; swallowed on failure."""
        try:
            log.info(
                "slate_partition_shadow",
                slate=slate,
                naive_cc=int(naive_cc),
                partitioned_cc=int(partitioned_cc),
                threshold_cc=int(threshold_cc),
                would_admit=bool(partitioned_cc <= threshold_cc),
            )
        except Exception:  # pragma: no cover - telemetry must never propagate
            pass

    def _enforced_entity_budget_cc(self) -> float:
        """The ENFORCED accumulated-entity wall in cc — ``threshold_cc(
        entity_loss_frac, bankroll)``, the exact number the entity cap refuses
        on. Unarmed axis or unusable bankroll ⇒ the per-game budget (an unarmed
        axis has no wall of its own; falling back to the tightest enforced game
        bound is what both leg-axis steers did before the split)."""
        limits = self._limits.limits
        bankroll_cc = self._risk_bankroll_cc()
        if (
            limits.entity_loss_frac is not None
            and bankroll_cc is not None
            and bankroll_cc > 0
        ):
            return float(threshold_cc(limits.entity_loss_frac, bankroll_cc))
        return self._enforced_game_budget_cc()

    def _publish_crn_cache(self, generation: int) -> None:
        """LEVER #5 — publish the CRN sample behind ``Cov(candidate, book)``.

        WHY THIS IS THE SIGNAL. ``delta_p_book`` must NOT reach price: R² =
        0.921 against candidate EV (92% redundant with what the pricer already
        has), 42.2% of candidates unresolvable at 3σ at n=20k, and 41.7% of
        EV-residual signs flip across caches. ``Cov(candidate payoff,
        pre-existing book P&L)`` is a MEAN OF A PRODUCT — every draw
        contributes — and it is EV-ORTHOGONAL BY CONSTRUCTION. Measured: SE
        $0.378 = 0.0161 c/contract against an EV-controlled spread of 1.1333
        c/contract ⇒ SNR 70.4, matched-pair t = 8.4.

        THE JOINT IS DELIBERATE. This is a LOCATION-axis object (it prices a
        quote), so it samples ``corr_location_point`` — the PRICING joint the
        fills were actually quoted on — never the tail-dependence stress joint
        every ENFORCED gate rides (2026-07-26 axis split). A pricing steer off
        the collapsed stress joint would mark every fill adverse by
        construction.

        BLAST RADIUS. Slow loop only: this rides the off-hot-path book-risk
        publish and runs at most once per position generation. The quote path
        reads the cached arrays and never samples. Any failure logs and leaves
        the cache None — the steer then runs on the ZERO-standard-error
        Herfindahl reading alone, which is a complete steer by itself.

        DERIVE BEFORE USE. The cache publishes only if the drawn sample's own
        measured SNR clears the ratified z = 3 anchor; below that the estimate
        is not resolvable and the covariance term abstains rather than
        contributing noise to a price."""
        inputs = self._last_book_inputs
        if inputs is None or inputs.input_generation != generation:
            return
        bankroll_cc = self._risk_bankroll_cc()
        if bankroll_cc is None or bankroll_cc <= 0:
            self._crn_cache = None
            return
        try:
            import numpy as np

            from combomaker.sim.engine import book_pnl, sample_leg_values

            model = inputs.model
            if model.unknown or not model.legs or not model.positions:
                self._crn_cache = None
                return
            n = int(self._config.crn_cache_samples)
            rng = np.random.default_rng(inputs.seed ^ 0x5E31)
            values = sample_leg_values(
                model.legs, model.corr_location_point, n, rng
            )
            pnl = book_pnl(values, model.positions)
            # The value signal's own measured dispersion over the book's OWN
            # tickets — the steer's derived half-range candidate, and the
            # denominator of the resolvability check. Nothing hand-set.
            cache = CrnBookCache(
                input_generation=generation,
                col_by_ticker=dict(model.leg_index),
                leg_values=values,
                book_pnl=pnl,
                bankroll_cc=float(bankroll_cc),
            )
            vals: list[float] = []
            for pos in self._exposure.positions.values():
                v = cache.value_cc_per_contract(pos.legs)
                if v is not None:
                    vals.append(v)
            if len(vals) < 2:
                self._crn_cache = None
                return
            sd = float(np.std(vals, ddof=1))
            # SE of a covariance mean over n draws, in the same cc/contract
            # units — the resolvability denominator.
            se = float(np.std(pnl, ddof=1)) * 0.5 * CC_PER_DOLLAR / (
                float(bankroll_cc) * math.sqrt(max(1, pnl.size))
            )
            snr = sd / se if se > 0 else 0.0
            if snr < self._config.crn_min_snr_z:
                log.info(
                    "crn_cache_unresolvable",
                    generation=generation,
                    snr=round(snr, 2),
                    required_z=self._config.crn_min_snr_z,
                )
                self._crn_cache = None
                return
            self._crn_cache = CrnBookCache(
                input_generation=generation,
                col_by_ticker=cache.col_by_ticker,
                leg_values=values,
                book_pnl=pnl,
                bankroll_cc=float(bankroll_cc),
                value_sd_cc=sd,
            )
            log.info(
                "crn_cache_published",
                generation=generation,
                n_samples=int(pnl.size),
                n_legs=len(model.legs),
                value_sd_cc=round(sd, 3),
                snr=round(snr, 2),
            )
        except Exception:
            log.exception("crn_cache_publish_failed")
            self._crn_cache = None

    def _concentration_profile(self, snap: ExposureSnapshot) -> ConcentrationProfile:
        """LEVER #5 (2026-07-27) — the steer's inputs, all measured state.

        THE LOSS-EVENT BOOK is rebuilt only when the position generation moves
        (a fill / settlement / rehydrate), never per quote: the AND-bound
        dollar-Herfindahl marginal is O(1) against the cached running sums,
        which is the whole reason it costs 1.47us against the 7.16us heuristic
        it replaces. Correctness is exact, not approximate — the generation
        counter is the same one every other cache in this class keys on.

        THE WALLS are each axis's OWN ENFORCED threshold, taken from the LIVE
        limit checker: the per-game loss budget the P(book) axis already
        divides by, and the accumulated-entity wall (``entity_loss_frac`` x
        bankroll) the entity cap refuses on. The family axis has no separate
        enforced wall, so it reads the game budget — the tightest bound a
        one-direction stack can burn. NOTHING here is a count.
        """
        gen = self._exposure.position_generation
        key = (gen, self._skew_facts_generation())
        if key != self._loss_event_generation:
            facts = self._skew_settled_facts()
            if facts is None:
                entries = [
                    (ticket_bucket(p.legs), float(p.max_loss_cc))
                    for p in self._exposure.positions.values()
                ]
            else:
                # Settled-leg fact resolution (2026-08-01): the SAME rule the
                # skew snapshot applies — determined positions are realized
                # P&L (no loss event), partially-settled ones bucket by their
                # LIVE legs only, unresolvable legs count in full.
                entries = []
                for p in self._exposure.positions.values():
                    live = concentration_live_legs(p.legs, facts)
                    if live is None:
                        continue
                    entries.append((ticket_bucket(live), float(p.max_loss_cc)))
            self._loss_event_book = build_loss_event_book(entries)
            self._loss_event_generation = key
        game_wall_cc = self._enforced_game_budget_cc()
        # ONE owner for the entity denominator (2026-07-27): the same helper the
        # leg-axis profile uses, so the two steers can never drift apart.
        entity_wall_cc = self._enforced_entity_budget_cc()
        return ConcentrationProfile(
            loss_events=self._loss_event_book,
            game_dollars_cc={
                k: float(v) for k, v in snap.worst_case_loss_by_game_cc.items()
            },
            game_wall_cc=game_wall_cc,
            family_dollars_cc={
                k: float(v) for k, v in snap.committed_loss_by_family_cc.items()
            },
            family_wall_cc=game_wall_cc,
            entity_dollars_cc={
                k: float(v) for k, v in snap.loss_by_entity_cc.items()
            },
            entity_wall_cc=entity_wall_cc,
            fill_elasticity_per_cent=self._config.fill_elasticity_per_cent,
            centre=self._steer_centre,
            crn=(
                self._crn_cache
                if self._crn_cache is not None
                and self._crn_cache.input_generation == gen
                else None
            ),
        )

    def _log_leg_axis_exposure(self) -> None:
        """LEG-DIRECTION AXIS visibility (2026-07-25 operator directive): one
        ``leg_axis_exposure`` line per accepted book-risk publish — the
        committed book's premium-at-risk by (family × side) and (entity ×
        side), top-8 each, so "short K-overs everywhere / one arm carrying
        $127" is measured continuously instead of hand-decomposed. Slow-loop
        only (rides the off-hot-path publish); any failure logs and returns —
        fix-isolation: this can never touch the pricing path."""
        try:
            snap = self._exposure.snapshot(self._marginals, mass_acceptance=False)
            fam = snap.committed_loss_by_family_cc
            ent = snap.committed_loss_by_entity_cc
            if not fam:
                return
            log.info(
                "leg_axis_exposure",
                n_family_keys=len(fam),
                n_entity_keys=len(ent),
                budget_cc=int(self._enforced_game_budget_cc()),
                top_families=sorted(
                    fam.items(), key=lambda kv: -kv[1]
                )[:8],
                top_entities=sorted(
                    ent.items(), key=lambda kv: -kv[1]
                )[:8],
            )
        except Exception:
            log.exception("leg_axis_exposure_failed")

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
                realized_pnl_cc=inputs.realized_pnl_cc,
                # FIX 2 arming — MUST be forwarded here too, or the INLINE path
                # (paper mode, backtests, any embedding without a worker pool)
                # silently ignores the flag while the off-loop path honours it.
                det_max_settlement_aware=inputs.det_max_settlement_aware,
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

    # ------------------------------------------------------ deployment scale

    def deploy_scale_for_check(self) -> float:
        """The scale the caps' DEPLOY-side budgets breathe at, for one check.

        HOT-PATH COST: an attribute read, a comparison and (only when the book
        changed since the solve) one cached sum over committed positions. The
        SOLVE never runs here — it runs on the maintenance tick, off-loop.

        BOOK-GROWTH DECAY (2026-07-27, found by the live ship gate). The first
        cut invalidated the scale on any position-generation change. That is
        correct in spirit and useless in practice: a RESERVATION bumps the
        position generation, so on live flow the scale collapsed to 1.0 within
        one accept and never re-armed between solves — the feature measured
        headroom it could never spend.

        The honest replacement charges every dollar of book growth against the
        measured headroom instead of throwing the measurement away. The solve
        said "this book could be S times bigger". If committed premium-at-risk
        has since grown by g = live / solved, the room that is left is S / g:

            effective = clamp(S x solved_premium / live_premium, 1.0, S)

        Monotone and fail-safe SMALLER by construction — the scale can only
        ever walk DOWN as fills land, reaching exactly 1.0 when the book has
        grown into the whole solved envelope, with no cliff and no dead zone.
        It is a MECHANISM (measured book growth), not a tolerance number.

        Every remaining uncertainty still returns 1.0 outright:
          - feature disarmed (byte-identical to before this existed);
          - the solve never succeeded / raised (``FAILSAFE``);
          - the solved or the live premium is unreadable / non-positive.

        This decay is a BETWEEN-SOLVES bridge, never the safety argument: the
        portfolio ENVELOPE (det-max / tail-prob / ruin / CVaR) is enforced at
        100% and UNSCALED against the fresh gating snapshot on every candidate,
        so a scale that is briefly generous can still only loosen SHAPE caps
        inside a wall that has not moved.
        """
        if not self._config.deploy_scale_enabled:
            return 1.0
        res = self._deploy_scale
        if not res.solved or res.scale <= 1.0:
            return 1.0
        solved_premium = res.solved_premium_cc
        if solved_premium <= 0:
            return 1.0
        if res.book_generation == self._exposure.position_generation:
            return res.scale
        live_premium = self._committed_premium_cc()
        if live_premium <= 0:
            return 1.0
        if live_premium <= solved_premium:
            return res.scale                    # book did not grow — full room
        decayed = res.scale * solved_premium / live_premium
        return max(1.0, min(res.scale, decayed))

    def _committed_premium_cc(self) -> int:
        """Total committed premium-at-risk, CACHED on the position generation.

        One O(positions) sum per position change (a fill / settlement /
        reservation), not per quote — the hot path re-reads the cache."""
        gen = self._exposure.position_generation
        cached = self._committed_premium_cache
        if cached is not None and cached[0] == gen:
            return cached[1]
        total = sum(p.max_loss_cc for p in self._exposure.positions.values())
        self._committed_premium_cache = (gen, total)
        return total

    async def solve_deploy_scale_offloop(self) -> None:
        """OFF-LOOP solve: every MC probe runs in the ``book_risk_pool`` worker.

        The cheap parts (build the scaled ``BookModel``, run ``check``) stay on
        the loop — the same on-loop work one book-risk refresh and one
        quote-time check already do — and the expensive per-probe MC is
        ``await``ed in the worker process, yielding control so the maintenance
        loop keeps beating the supervisor heartbeat throughout. Falls back to
        the inline solve when no pool is wired (paper/backtests/tests).

        The ladder is walked DESCENDING and STOPS at the first feasible scale,
        so the common case costs far fewer than ``deploy_scale_grid_points``
        MCs. Never raises on the loop."""
        if not self._config.deploy_scale_enabled:
            return
        if self._book_risk_pool is None:
            self.solve_deploy_scale()
            return
        pool = self._book_risk_pool
        t0 = self._clock.monotonic_ns()
        try:
            gen = self._exposure.position_generation
            positions = list(self._exposure.positions.values())
            bankroll = self._risk_bankroll_cc()
            if not positions or bankroll is None or bankroll <= 0:
                self._deploy_scale = DEPLOY_SCALE_FAILSAFE
                return
            graded: dict[float, tuple[bool, tuple[str, ...]]] = {}

            async def grade(s: float) -> bool:
                inputs = self._deploy_scale_mc_inputs(s, positions, bankroll, gen)
                if inputs is None:
                    graded[s] = (False, ("empty_scaled_book",))
                    return False
                snap = await pool.run(inputs)
                verdict = self._deploy_scale_check(s, positions, bankroll, snap)
                graded[s] = verdict
                return verdict[0]

            await grade(1.0)
            for s in scale_grid(
                self._config.deploy_scale_s_max,
                self._config.deploy_scale_grid_points,
            ):
                if self._exposure.position_generation != gen:
                    # The book moved under the solve — the answer would describe
                    # a portfolio we no longer hold. Abandon; the next tick
                    # re-solves. (``deploy_scale_for_check`` would reject it on
                    # the generation stamp anyway; stopping saves the rest of
                    # the MC budget.)
                    log.info("deploy_scale_solve_abandoned_stale", generation=gen)
                    return
                if await grade(s):
                    break                      # monotone => this is the answer
            result = solve_deployment_scale(
                graded,
                s_max=self._config.deploy_scale_s_max,
                points=self._config.deploy_scale_grid_points,
                solve_ms=(self._clock.monotonic_ns() - t0) / 1e6,
            )
            self._deploy_scale = replace(
                result,
                book_generation=gen,
                solved_premium_cc=sum(p.max_loss_cc for p in positions),
            )
            self._log_deploy_scale(result, gen, len(positions), offloop=True)
        except Exception:
            log.exception("deploy_scale_solve_offloop_failed")
            self._deploy_scale = DEPLOY_SCALE_FAILSAFE

    def solve_deploy_scale(self) -> None:
        """INLINE solve (paper / backtests / tests / no pool wired).

        Byte-identical POLICY to the off-loop path — they share the probe
        ladder, the per-probe grading and ``solve_deployment_scale`` — the MC
        simply runs on the calling thread. Never raises."""
        if not self._config.deploy_scale_enabled:
            return
        t0 = self._clock.monotonic_ns()
        try:
            gen = self._exposure.position_generation
            positions = list(self._exposure.positions.values())
            bankroll = self._risk_bankroll_cc()
            if not positions or bankroll is None or bankroll <= 0:
                self._deploy_scale = DEPLOY_SCALE_FAILSAFE
                return
            graded: dict[float, tuple[bool, tuple[str, ...]]] = {}

            def grade(s: float) -> bool:
                graded[s] = self._deploy_scale_feasible(s, positions, bankroll, gen)
                return graded[s][0]

            grade(1.0)
            for s in scale_grid(
                self._config.deploy_scale_s_max,
                self._config.deploy_scale_grid_points,
            ):
                if grade(s):
                    break
            result = solve_deployment_scale(
                graded,
                s_max=self._config.deploy_scale_s_max,
                points=self._config.deploy_scale_grid_points,
                solve_ms=(self._clock.monotonic_ns() - t0) / 1e6,
            )
            self._deploy_scale = replace(
                result,
                book_generation=gen,
                solved_premium_cc=sum(p.max_loss_cc for p in positions),
            )
            self._log_deploy_scale(result, gen, len(positions), offloop=False)
        except Exception:
            log.exception("deploy_scale_solve_failed")
            self._deploy_scale = DEPLOY_SCALE_FAILSAFE

    def _log_deploy_scale(
        self, result: DeployScaleResult, gen: int, n_positions: int, *, offloop: bool
    ) -> None:
        log.info(
            "deploy_scale_solved",
            scale=round(result.scale, 4),
            solved=result.solved,
            binding=list(result.binding),
            reason=result.reason,
            evaluations=result.evaluations,
            solve_ms=round(result.solve_ms, 1),
            generation=gen,
            n_positions=n_positions,
            offloop=offloop,
        )

    def _scaled_positions(
        self, s: float, positions: list[OpenPosition]
    ) -> list[OpenPosition]:
        """The book UNIFORMLY scaled by ``s``.

        Contracts are integer centi-contracts, so the scaling rounds; a position
        that rounds to zero is dropped. That can only make the scaled book
        SMALLER (a sub-centi-contract amount), i.e. the solve marginally more
        permissive at the 1e-2-contract level — while a scaled book that comes
        out entirely EMPTY is graded INFEASIBLE, never a free pass."""
        out: list[OpenPosition] = []
        for p in positions:
            c = int(round(int(p.contracts) * s))
            if c > 0:
                out.append(replace(p, contracts=CentiContracts(c)))
        return out

    def _deploy_scale_mc_inputs(
        self, s: float, positions: list[OpenPosition], bankroll_cc: int, gen: int
    ) -> BookRiskInputs | None:
        """The immutable inputs for ONE probe's MC (picklable => pool-safe)."""
        scaled = self._scaled_positions(s, positions)
        if not scaled:
            return None
        model = build_book_model(
            scaled,
            marginals=self._marginals,
            within_game_rho=self._within_game_rho,
            # FIX 2: the deploy-scale solve must measure the SAME det-max the
            # caps enforce, or it would solve against a book carrying forward
            # loss the exchange has already retired and under-report headroom.
            settled_facts=self._settled_fact,
        )
        return BookRiskInputs(
            model=model,
            n_samples=self._config.deploy_scale_mc_samples,
            seed=self._config.book_risk_seed,
            band="high",
            bankroll_cc=bankroll_cc,
            structural_cfg=self._structural_cfg,
            current_equity_cc=self._ruin_equity_basis_cc(model),
            ruin_floor_frac=self._config.ruin_floor_frac,
            ruin_prob_ci_z=self._config.ruin_prob_ci_z,
            det_max_settlement_aware=self._det_max_settlement_aware(),
            input_generation=gen,
            realized_pnl_cc=self._realized_pnl_cc,
        )

    def _deploy_scale_check(
        self,
        s: float,
        positions: list[OpenPosition],
        bankroll_cc: int,
        snap: BookRiskSnapshot,
    ) -> tuple[bool, tuple[str, ...]]:
        """Is the book scaled by ``s`` clean under EVERY enforced cap?

        The caps are evaluated at their UNSCALED thresholds — s is bounded by
        the deploy-side budgets as well as by the portfolio envelope, which is
        the strictest reading of the operator's "bounded above by every
        already-enforced cap". An UNUSABLE snapshot is INFEASIBLE (fail-closed:
        an unmeasured tail is never headroom)."""
        if not snap.usable:
            return False, ("book_risk_unusable",)
        scaled = self._scaled_positions(s, positions)
        if not scaled:
            return False, ("empty_scaled_book",)
        probe = ExposureBook(self._conventions, is_me_event=self._exposure.is_me_event)
        for p in scaled:
            probe.add_position(p)
        breaches = self._limits.check(
            probe,
            self._marginals,
            self.daily_pnl,
            risk_bankroll_cc=bankroll_cc,
            bankroll_source_configured=self._bankroll_source_configured(),
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
            book_risk=snap,
        )
        enforced = self.partition_breaches(list(breaches))
        return (not enforced), tuple(sorted({str(b.reason) for b in enforced}))

    def _deploy_scale_feasible(
        self,
        s: float,
        positions: list[OpenPosition],
        bankroll_cc: int,
        gen: int,
    ) -> tuple[bool, tuple[str, ...]]:
        """One INLINE probe: live MC on the scaled book, then the live checker."""
        inputs = self._deploy_scale_mc_inputs(s, positions, bankroll_cc, gen)
        if inputs is None:
            return False, ("empty_scaled_book",)
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
            realized_pnl_cc=inputs.realized_pnl_cc,
            # FIX 2: the deploy-scale probe must measure the SAME det-max the
            # caps enforce (see ``_deploy_scale_mc_inputs``).
            det_max_settlement_aware=inputs.det_max_settlement_aware,
        )
        return self._deploy_scale_check(s, positions, bankroll_cc, snap)


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
        Cheap: reads stored state + one clock read; never runs MC.

        FIX 5 (2026-07-28) — the GENERATION branch DECAYS instead of discarding.
        A hard cliff on the generation counter is a mistake this repo has already
        made once and already written the lesson for (``risk/deploy_scale.py``:
        "a reservation bumps this counter on every accept, so a hard cliff here
        made the whole feature inert"). It had the same effect here: every one of
        the session's 407 ``skip_portfolio_cvar`` declines on 2026-07-28 was this
        branch and nothing else, 289 of them in the 10:24Z minute alone against
        62 quotes, at a generation gap of exactly 1 — a single reservation.
        Armed, the snapshot is instead CHARGED for the premium added since it was
        measured (``_decay_book_risk``) and used; the result is strictly more
        adverse than the measurement, so a book that genuinely breaches still
        breaches. Unarmed (the default) the decayed verdict is computed for the
        log and the discard happens exactly as before. The TIME guard below is
        NOT decayed: an old measurement of an UNCHANGED book is a different
        failure (nothing to charge for) and keeps failing closed."""
        if not self._exposure.positions:
            return None
        snap = self._book_risk
        stamp = self._book_risk_mono_ns
        if snap is None or stamp is None:
            return _StaleBookRisk()  # non-empty book, never measured ⇒ fail closed
        if snap.input_generation != self._exposure.position_generation:
            # The PORTFOLIO was mutated (fill / settlement / rehydration /
            # reconciliation / reservation) since this snapshot's MC read the
            # positions. Charge the growth and keep the measurement rather than
            # throwing away the only tail number we have (FIX 5).
            decayed = self._book_risk_decayed()
            if decayed is not None and self._config.book_risk_stale_decay:
                # The TIME guard still applies to a decayed snapshot: growth is
                # charged, but an ancient measurement is still refused.
                if (
                    self._clock.monotonic_ns() - stamp
                ) / 1e9 > self._config.book_risk_stale_after_s:
                    return _StaleBookRisk()
                return decayed
            return _StaleBookRisk()  # position generation superseded ⇒ fail closed
        age_s = (self._clock.monotonic_ns() - stamp) / 1e9
        if age_s > self._config.book_risk_stale_after_s:
            return _StaleBookRisk()  # snapshot too old ⇒ fail closed (secondary)
        return snap

    def quote_warmup_open(self) -> bool:
        """BOOT-WARMUP QUOTE GATE (2026-07-31). True once quote SENDING is open.

        Holds quote sending at startup until the FIRST evaluation on which the
        confirm gate's book-risk usability predicate could pass — REUSING
        ``_book_risk_for_check`` (None ⇒ empty book, nothing to measure — an
        empty book must still quote; else the snapshot's own ``usable``). A
        quote sent before that moment is a quote whose acceptance the confirm
        path is GUARANTEED to renege (fail-closed on the unmeasured tail), so
        sending it only burns exchange goodwill (the 2026-07-31 10:11:12Z
        boot: two won auctions reneged "book-risk snapshot unusable").

        ONE-WAY LATCH: opens once, never re-holds — mid-run staleness keeps
        today's behaviour exactly (per-check fail-closed via the portfolio
        caps). Emits ONE loud line with the measured warmup duration when
        quoting opens; while holding, emits a loud warning throttled to once
        per snapshot-freshness window (a NEVER-usable book stays silent
        forever, loudly). Cheap: latched-open path is one bool read; the
        holding path is ``_book_risk_for_check`` (state + clock reads)."""
        if self._quote_warmup_open:
            return True
        risk = self._book_risk_for_check()
        now_ns = self._clock.monotonic_ns()
        # ENFORCEMENT PARITY — the gate may hold ONLY where the confirm path
        # would actually renege, never wider (no-double-risk-layers; a gate
        # stricter than confirm is a pure throughput loss):
        #   * SHADOW caps: the fail-closed portfolio breaches carry
        #     ``shadow=caps_shadow_mode`` (risk/limits.py) and the confirm
        #     path DROPS shadow breaches (``_partition_breaches``) — a confirm
        #     cannot renege, so the gate stands down. Read live off the
        #     checker, so an operator cap-mode change needs no second switch.
        #   * NO BANKROLL LAYER: with no bankroll source configured and no
        #     reading, the whole %-cap layer (incl. the book-risk fail-closed)
        #     is INACTIVE (limits.py "do-not-brick path" — demo/paper rigs) —
        #     same two inputs check() receives, so the gate mirrors it exactly.
        caps_shadow = self._limits.limits.caps_shadow_mode
        bankroll_layer_active = (
            self._risk_bankroll_cc() is not None
            or self._bankroll_source_configured()
        )
        if risk is None or risk.usable or caps_shadow or not bankroll_layer_active:
            self._quote_warmup_open = True
            warmup_s = (now_ns - self._quote_warmup_start_mono_ns) / 1e9
            if risk is None or risk.usable:
                detail = (
                    "first USABLE book-risk verdict — quote sending opens "
                    "(boot-warmup gate released; it will not re-hold this "
                    "process)"
                )
            elif caps_shadow:
                detail = (
                    "portfolio caps in SHADOW mode — a confirm cannot renege, "
                    "so the boot-warmup gate stands down (opens un-held)"
                )
            else:
                detail = (
                    "no bankroll layer configured — the %-cap fail-closed is "
                    "inactive (limits.py do-not-brick path), so the "
                    "boot-warmup gate stands down (opens un-held)"
                )
            log.info(
                "quote_warmup_open",
                warmup_s=round(warmup_s, 3),
                book_positions=len(self._exposure.positions),
                caps_shadow_mode=caps_shadow,
                bankroll_layer_active=bankroll_layer_active,
                detail=detail,
            )
            return True
        elapsed_s = (now_ns - self._quote_warmup_start_mono_ns) / 1e9
        last = self._warmup_last_warn_mono_ns
        if (
            last is None
            or (now_ns - last) / 1e9 >= self._config.book_risk_stale_after_s
        ):
            self._warmup_last_warn_mono_ns = now_ns
            log.warning(
                "quote_warmup_holding",
                elapsed_s=round(elapsed_s, 1),
                book_positions=len(self._exposure.positions),
                detail="boot warmup: non-empty book with NO usable book-risk "
                "snapshot yet — quote sending held (fail-closed; a confirm "
                "could only renege). Feeds/metadata/settlement continue; "
                "quoting opens automatically on the first usable snapshot",
            )
        return False

    def _book_risk_decayed(self) -> _DecayedBookRisk | None:
        """FIX 5. The book-growth-charged view of the current (generation-stale)
        snapshot, or None when this book cannot be charged soundly.

        MEMOISED on (snapshot generation, live generation): the growth charge is
        O(live positions) and the quantile shift O(1001), so both run once per
        book change rather than once per quote — the hot path pays a tuple
        compare. This mirrors ``_committed_premium_cc``'s generation cache.

        Returns None (⇒ the caller keeps the fail-closed discard) when:
          - no snapshot / no captured position map;
          - the snapshot is unusable or carries no loss-quantile envelope
            (``_decay_book_risk``'s own refusals); or
          - the ABSTAIN BAND trips: the premium ADDED since the measurement
            exceeds the premium the measurement was built on, i.e. the book has
            more than doubled and the snapshot no longer describes it. That
            bound is the measured book itself, not a tolerance knob."""
        snap = self._book_risk
        priced = self._book_risk_positions
        if snap is None or priced is None:
            return None
        snap_gen = snap.input_generation
        live_gen = self._exposure.position_generation
        cached = self._decayed_book_risk
        if cached is not None and cached[0] == snap_gen and cached[1] == live_gen:
            return cached[2]
        # EXACT growth charges from the position-ID sets: only positions the
        # snapshot never saw (resp. never SAMPLED) are charged, and REMOVED
        # positions are ignored (dropping a position can only lower the loss). A
        # scalar premium difference would let a simultaneous add+remove net out
        # and undercharge, which is exactly what must not happen here.
        sampled_stamp = self._book_risk_sampled
        sampled = (
            sampled_stamp[1]
            if sampled_stamp is not None and sampled_stamp[0] == snap_gen
            else None
        )
        added = 0
        unsampled = 0
        for pid, p in self._exposure.positions.items():
            if pid not in priced:
                added += p.max_loss_cc
                unsampled += p.max_loss_cc
            elif sampled is not None and pid not in sampled:
                # HELD by the snapshot but only as a flat RESERVE — inside its
                # det-max already, outside its sampled model ES. Charge the tail
                # axes for it (its leg book may have come online since).
                unsampled += p.max_loss_cc
        result: _DecayedBookRisk | None
        measured = sum(priced.values())
        if measured > 0 and added > measured:
            result = None  # abstain: the book more than doubled since measuring
        else:
            result = _decay_book_risk(snap, added, unsampled)
        self._decayed_book_risk = (snap_gen, live_gen, result)
        return result

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
        # FIX 5 SHADOW OBSERVABILITY (2026-07-28). On the generation-stale
        # branch — the one that produced 100% of the session's portfolio-CVaR
        # declines — report what the book-growth decay MEASURED, whether or not
        # it is armed. Unarmed this is the whole shadow readout: how much premium
        # the snapshot had not seen, and the charged tail the cap WOULD have read
        # instead of failing closed. Cached per generation pair, so this costs a
        # tuple compare on the hot path.
        decay_added_cc: int | None = None
        decay_es_99_cc: int | None = None
        decay_det_max_cc: int | None = None
        if fallback_reason == "book_risk_generation_stale":
            decayed = self._book_risk_decayed()
            if decayed is not None:
                decay_added_cc = decayed.added_premium_cc
                decay_es_99_cc = int(decayed.governing_model_es_99_cc)
                decay_det_max_cc = int(decayed.deterministic_max_loss_cc)
        return {
            "stale_decay_added_premium_cc": decay_added_cc,
            "stale_decay_es_99_cc": decay_es_99_cc,
            "stale_decay_det_max_cc": decay_det_max_cc,
            "stale_decay_armed": bool(self._config.book_risk_stale_decay),
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

    def _gate_pricing_edge(self, state: OpenQuoteState) -> float | None:
        """``_pricing_edge_cc`` gated on the arming flag, with the FALLBACK
        made VISIBLE (2026-07-25 review: an armed gate silently reverting to
        MC EV was indistinguishable from a pricing-edge verdict)."""
        if not self._config.gate_ev_from_pricing_fair:
            return None
        edge = self._pricing_edge_cc(state)
        if edge is None:
            self._metrics.inc("candidate_gate.pricing_edge_fallback")
            log.info(
                "candidate_gate_pricing_edge_fallback",
                quote_id=state.quote_id,
                detail="fresh re-price unavailable — MC EV judges admission",
            )
        return edge

    def _pricing_edge_cc(self, state: OpenQuoteState) -> float | None:
        """The CALIBRATED pricing fair's edge for the pending fill, float cc,
        computed against a FRESH engine fair at CONFIRM time (2026-07-25
        renege root cause #2: the candidate gate's band-high risk copula
        scores heavily-correlated same-game combos structurally negative-EV
        even when the calibrated pricing fair — the model that priced the
        quote — says +EV; 20 won auctions reneged tonight). The gate's
        admission-EV source can be switched to THIS number
        (``gate_ev_from_pricing_fair``); tail budgets keep the risk models.

        FRESH, never the frozen quote-time fair: the MC EV's one real virtue
        at admission is catching STALE-QUOTE PICKOFFS (the fair moved between
        quote and accept — a −$4.96 catch live tonight). Re-pricing the RFQ
        off the LIVE books preserves that: a moved fair shows up in the fresh
        edge exactly as it does in the MC, while a pure model disagreement on
        same-game structure (fair unchanged) admits. Any failure to re-price
        (stale legs, no-quote, error) returns None ⇒ the gate keeps its MC EV
        — fail-safe, never a loosened admission."""
        if state.pending_fill is None:
            return None
        accepted_side, bid, qty = state.pending_fill
        try:
            fresh = self._price(state.rfq)
        except Exception:
            return None
        if not isinstance(fresh, ConstructedQuote):
            return None  # engine no-quotes the RFQ NOW ⇒ no calibrated fair
        edge = self._candidate_edge_cc(
            int(fresh.fair_cc),
            int(bid),
            qty,
            self._conventions.maker_position_side(accepted_side),
        )
        return None if edge is None else float(edge)

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
        self, inputs: CandidateBookRiskInputs, *, deadline_s: float
    ) -> CandidateBookRisk:
        """Run ONE candidate-MC eval, off the loop via ``BookRiskPool.run_candidate``
        when a pool is wired (the CPU-bound MC never blocks the heartbeat), else
        INLINE via the pool's OWN worker fn (paper / backtests / tests — fast there,
        and byte-identical to the off-loop path).

        BOUNDED by ``deadline_s`` — the confirm window that is actually LEFT,
        measured off the clock by the caller. This is what replaced the deleted
        cost predictor (B2): the MC is always STARTED while any window remains,
        and a run that will not land in time raises ``TimeoutError`` (the worker
        finishes and frees itself, exactly as ``run_state_worst_case`` documents)
        which the caller resolves as a LATENCY event through the deterministic
        fallback — never as a risk verdict. Any other pool/worker error raises and
        the caller turns it into a fail-closed decline.

        The INLINE path (no pool) is synchronous and cannot be interrupted, so it
        is not wrapped: it is the paper/backtest/test path, where the MC is fast
        and there is no exchange window to miss."""
        if self._book_risk_pool is not None:
            return await self._book_risk_pool.run_candidate(
                inputs, deadline_s=deadline_s
            )
        return _worker_candidate_book_risk(inputs)

    # ---------------------------------------- derived confirm-window budgeting

    def _confirm_window_reserve_ns(self) -> int:
        """The time that MUST still be on the clock when the candidate gate gives
        up, so the confirm can actually land. Fully MEASURED — no typed number.

        Three MEASURED terms:
          * ``confirm.rtt_ms`` p99 — the observed round trip of the confirm call
            we still have to make;
          * its observed DISPERSION (p99 − p50) — the safety margin taken FROM
            the latency distribution itself, so a link that becomes erratic
            widens its own margin without anyone touching a config;
          * ``candidate_gate.fallback_ms`` p99 — the measured cost of the
            deterministic fallback that runs when the budget expires (it is
            microseconds of arithmetic, but it is measured rather than assumed).

        Neither term needs a prior CONFIRM to be measured, so the very first
        accept of a process already budgets honestly:
          * confirms are rare (tens/day), so until ``confirm.rtt_ms`` has samples
            the round trip is read from ``quote.create_rtt_ms`` — the same REST
            verb to the same venue over the same link, sampled thousands of times
            an hour. A measured proxy, never a typed guess;
          * the deterministic check is timed on EVERY accept by the reservation
            itself (``confirm.deterministic_check_ms``), which runs the identical
            ``LimitChecker.check`` machinery moments before the gate.

        UNMEASURED ⇒ WORST CASE. With no samples at all the reserve is the WHOLE
        exchange window, driving the gate budget to zero: that accept resolves on
        the deterministic caps. Under the new semantics a zero budget costs a
        refinement, never an auction — and it self-warms immediately."""
        rtt_hi = self._first_measured_quantile_ms(
            ("confirm.rtt_ms", "quote.create_rtt_ms"), 0.99
        )
        rtt_mid = self._first_measured_quantile_ms(
            ("confirm.rtt_ms", "quote.create_rtt_ms"), 0.50
        )
        fallback_hi = self._first_measured_quantile_ms(
            ("confirm.deterministic_check_ms", "confirm.decision_ms"), 0.99
        )
        if rtt_hi is None or rtt_mid is None or fallback_hi is None:
            return EXCHANGE_CONFIRM_WINDOW_NS
        dispersion_ms = max(0.0, rtt_hi - rtt_mid)
        reserve_ms = rtt_hi + dispersion_ms + fallback_hi
        return min(EXCHANGE_CONFIRM_WINDOW_NS, int(reserve_ms * 1e6))

    def _first_measured_quantile_ms(
        self, series: Sequence[str], q: float
    ) -> float | None:
        """The ``q``-quantile of the FIRST series in ``series`` that has samples,
        or None if none do.

        The order is a MEASURED-PROXY CHAIN, most-direct first: the real thing,
        then the closest thing we actually measure often enough to be warm. It
        exists so "unmeasured ⇒ worst case" never fires for a quantity we can in
        fact observe — e.g. the confirm round trip is the direct measurement but
        confirms are rare, while the quote POST is the same REST verb to the same
        venue and is sampled thousands of times an hour."""
        for name in series:
            value = self._metrics.quantile_ms(name, q)
            if value is not None:
                return value
        return None

    def _candidate_gate_budget_ns(self, accept_ns: int | None) -> int:
        """The wall budget this accept's candidate gate may consume, DERIVED:

            exchange confirm window
              − time already burnt since the accept landed (last look, fill
                velocity, the reservation, and the last-look MC waiver when it
                ran — all of it accounted automatically, because the anchor is
                the accept itself)
              − the MEASURED reserve the confirm round trip needs (above)

        Clamped at zero. With ``candidate_gate_derived_deadline`` off, or with no
        accept anchor (paper / tests calling the gate directly), the pre-
        2026-07-27 typed ``candidate_gate_deadline_s`` applies unchanged."""
        if not self._config.candidate_gate_derived_deadline or accept_ns is None:
            return int(self._config.candidate_gate_deadline_s * 1e9)
        elapsed_ns = self._clock.monotonic_ns() - accept_ns
        return max(
            0, EXCHANGE_CONFIRM_WINDOW_NS - elapsed_ns - self._confirm_window_reserve_ns()
        )

    async def _candidate_gate_fallback(
        self,
        quote_id: str,
        state: OpenQuoteState,
        *,
        reservation_id: str | None,
        cause: str,
        detail: str,
    ) -> tuple[bool, str, ReasonCode | None]:
        """THE TIMEOUT IS NOT A DECLINE (2026-07-27, operator directive).

        Reached when the MC refinement could not be completed inside the DERIVED
        budget (the build or the MC would not fit, the build was abandoned
        mid-flight, or the book moved under every retry). We have ALREADY WON an
        auction that (a) the analytic/gross/burst gates admitted, (b) a real
        reservation granted headroom for, and (c) was priced +EV. Discarding it
        because a computation was slow is the worst available default — it cost
        70 won auctions in two days.

        What we do instead is decide from state we ALREADY HAVE, deterministically:
        re-run the ENFORCED caps (det-max, per-combo, entity/game, slate, gross,
        halt inputs) over the SAME entity set the reservation used, against the
        CURRENT live book. This is arithmetic — no Monte Carlo, microseconds —
        and it preserves the reason last look exists: the book can move between
        the reservation and now, and a fill that no longer fits the caps STILL
        DECLINES. Only the MC REFINEMENT is skipped.

        Returns ``(True, "", None)`` to confirm, or ``(False, detail,
        DECLINE_CANDIDATE_GATE_TIMEOUT)`` — a reason code that is deliberately
        DISTINCT from ``DECLINE_CANDIDATE_RISK`` so the decline report can tell a
        latency-degraded refusal from a risk model refusal."""
        if not self._config.candidate_gate_timeout_fallback:
            # Rollback switch: the pre-2026-07-27 behaviour, byte-identical.
            return False, detail, ReasonCode.DECLINE_CANDIDATE_RISK
        t0 = self._clock.monotonic_ns()
        try:
            breaches = self._deterministic_confirm_breaches(
                quote_id, state, reservation_id=reservation_id
            )
        except Exception as exc:  # noqa: BLE001 — a broken fallback fails closed
            log.error(
                "candidate_gate_fallback_errored",
                quote_id=quote_id,
                cause=cause,
                error=repr(exc),
            )
            return (
                False,
                f"{detail}; deterministic fallback errored: {exc!r}",
                ReasonCode.DECLINE_CANDIDATE_GATE_TIMEOUT,
            )
        fallback_ms = (self._clock.monotonic_ns() - t0) / 1e6
        # Feeds _confirm_window_reserve_ns: the budget reserves the measured cost
        # of this very check, so the fallback can never be the thing that runs out
        # of window. SHARED series with the reservation (identical machinery), so
        # the estimate is warm from the first accept of a process.
        self._metrics.observe_ms("confirm.deterministic_check_ms", fallback_ms)
        self._metrics.observe_ms("candidate_gate.fallback_ms", fallback_ms)
        if breaches:
            self._metrics.inc("candidate_gate.timeout_fallback_decline")
            reasons = ",".join(sorted({str(b.reason) for b in breaches}))
            log.warning(
                "candidate_gate_timeout_fallback_decline",
                quote_id=quote_id,
                cause=cause,
                fallback_ms=round(fallback_ms, 3),
                breaches=reasons,
                detail="MC refinement did not fit the derived confirm budget AND "
                "the deterministic caps refuse this fill against the live book",
            )
            return (
                False,
                (
                    f"{detail}; deterministic fallback DECLINED "
                    f"(enforced breaches: {reasons})"
                ),
                ReasonCode.DECLINE_CANDIDATE_GATE_TIMEOUT,
            )
        self._metrics.inc("candidate_gate.timeout_fallback_confirm")
        log.warning(
            "candidate_gate_timeout_fallback_confirm",
            quote_id=quote_id,
            cause=cause,
            fallback_ms=round(fallback_ms, 3),
            n_positions=len(self._exposure.positions),
            detail="MC refinement did not fit the derived confirm budget; the "
            "enforced deterministic caps re-checked against the live book ADMIT "
            "this already-won auction — confirming (latency is not a risk verdict)",
        )
        return True, "", None

    def _deterministic_confirm_breaches(
        self,
        quote_id: str,
        state: OpenQuoteState,
        *,
        reservation_id: str | None,
    ) -> list[Breach]:
        """The CHEAP DETERMINISTIC re-check of the ENFORCED caps at confirm — the
        mandatory safety floor under the timeout fallback. Returns the enforced
        breaches ([] = admissible).

        With a reservation service the candidate is already HELD, so
        ``RiskReservationService.revalidate`` re-runs ``LimitChecker.check`` over
        exactly the entity set ``try_reserve`` used (outstanding reservations,
        which include this candidate) — no double count, no state mutation.

        Without one (paper / backtests / tests) there is nothing held, so the
        candidate is passed explicitly to the SAME checker with the SAME
        arguments the reservation path uses. Either way this is the identical
        machinery the enforced caps always run through — never a reimplementation
        of a cap (hard rule 8)."""
        if self._reservation is not None and reservation_id is not None:
            return self._reservation.revalidate(
                reservation_id,
                marginals=self._marginals,
                daily_pnl=self.daily_pnl,
                risk_bankroll_cc=self._risk_bankroll_cc(),
                bankroll_source_configured=self._bankroll_source_configured(),
                start_time_provider=self._start_time_provider,
                halt_inputs=self._halt_inputs(),
                book_risk=self._book_risk_for_check(),
                apply_resting_haircut=self._config.resting_haircut_at_confirm,
                deploy_scale=self.deploy_scale_for_check(),
            )
        candidate = self._fill_position(quote_id, state)
        raw = self._limits.check(
            self._exposure,
            self._marginals,
            self.daily_pnl,
            candidate_positions=[candidate],
            risk_bankroll_cc=self._risk_bankroll_cc(),
            bankroll_source_configured=self._bankroll_source_configured(),
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
            book_risk=self._book_risk_for_check(),
            apply_resting_haircut=self._config.resting_haircut_at_confirm,
            deploy_scale=self.deploy_scale_for_check(),
        )
        return self._partition_breaches(raw)

    async def _candidate_gate_verdict(
        self,
        quote_id: str,
        state: OpenQuoteState,
        *,
        reservation_id: str | None,
        accept_ns: int | None = None,
    ) -> tuple[bool, str, ReasonCode | None]:
        """P0-1/P0-2 candidate-aware portfolio-risk gate for ONE contemplated fill,
        ATOMIC with the reservation book.

        Returns ``(True, "", None)`` to PROCEED to the confirm round-trip (the
        provisional reservation, if any, stays held for the caller to commit), or
        ``(False, detail, reason)`` to DECLINE — where ``reason`` is
        ``DECLINE_CANDIDATE_RISK`` when the RISK MODEL refused and
        ``DECLINE_CANDIDATE_GATE_TIMEOUT`` when the MC could not be completed in
        the derived window AND the deterministic fallback also refused. STRICTLY
        ADDITIVE — reachable only after the existing gates ADMIT the fill, and it
        can only DECLINE, never admit.

        2026-07-27 CONFIRM-WINDOW REBUILD. The gate is now a best-effort
        REFINEMENT on a DERIVED budget (``_candidate_gate_budget_ns``): before
        starting any piece of work it predicts that work's cost from MEASURED
        per-unit rates (ms/pair for the O(T^2) input build, ms/position for the
        MC) and simply does not start what cannot fit; the build is itself
        interruptible; and every way of running out of time resolves through
        ``_candidate_gate_fallback`` — the deterministic enforced caps re-checked
        against the live book — instead of an automatic decline.

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

        The retry loop is BOUNDED by BOTH the DERIVED confirm budget and
        ``candidate_gate_max_retries``. Running out of either is a LATENCY event,
        not a risk verdict, and resolves through the deterministic fallback.
        Still FAIL-CLOSED where it must be: an UNKNOWN merged marginal, an
        over-budget POST book, or ANY exception in the eval DECLINE outright with
        ``DECLINE_CANDIDATE_RISK`` — an unmeasured or errored joint tail is never
        safe, and an exception is a correctness failure, not a slow clock. The
        CALLER releases the provisional reservation on any decline.

        With no reservation service (paper / backtests / tests) ``reservation_id`` is
        None: there is no provisional reservation and the single-loop confirm cannot
        race, so the version check is inert (the stamps default to -1 and the live
        reservation version is -1 too) and the gate runs exactly one MC attempt — the
        prior behaviour, preserved."""
        start_ns = self._clock.monotonic_ns()
        budget_ns = self._candidate_gate_budget_ns(accept_ns)
        # Absolute wall the interruptible build honours (same clock as start_ns).
        hard_deadline_ns = start_ns + budget_ns
        for attempt in range(self._config.candidate_gate_max_retries + 1):
            # ---- BUDGET EXHAUSTED? (measured, never predicted) ----------------
            # The ONLY pre-work check: is there any window left at all? There is
            # no cost PREDICTION here by design — see the block comment on
            # _run_candidate_mc's deadline. A predictor that is only ever
            # validated by doing the work can lock itself out of the work, and
            # this one provably did: one poisoned ms/pair sample skipped the
            # build, a skipped build produced no new sample, and the joint-tail /
            # P(ruin) / delta-P(book) gate went dark permanently.
            elapsed_ns = self._clock.monotonic_ns() - start_ns
            remaining_ns = budget_ns - elapsed_ns
            if remaining_ns <= 0:
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
                    budget_ms=round(budget_ns / 1e6, 1),
                    remaining_ms=round(remaining_ns / 1e6, 1),
                    detail="no confirm budget remains — resolving on the "
                    "deterministic caps",
                )
                return await self._candidate_gate_fallback(
                    quote_id,
                    state,
                    reservation_id=reservation_id,
                    cause="budget_exhausted",
                    detail=(
                        "candidate gate: MC refinement did not fit the derived "
                        "confirm budget"
                    ),
                )
            # ---- INTERRUPTIBLE build -----------------------------------------
            build0_ns = self._clock.monotonic_ns()
            try:
                inputs = self._build_candidate_gate_inputs(
                    quote_id,
                    state,
                    exclude_reservation_id=reservation_id,
                    deadline_ns=hard_deadline_ns,
                )
            except _GateBudgetExceeded as exc:
                # The build itself ran out of window. It abandoned its own work at
                # the deadline (``exc.stage`` carries how far it got, e.g.
                # ``rho_pairs[440/880]``) — nothing is learned or remembered, so
                # the NEXT accept starts the build again from a clean clock.
                self._metrics.inc("candidate_gate.deadline_exceeded")
                self._metrics.inc("candidate_gate.window_expired_before_confirm")
                self._metrics.inc("candidate_gate.build_aborted")
                self._metrics.observe_ms(
                    "candidate_gate.runtime_ms",
                    (self._clock.monotonic_ns() - start_ns) / 1e6,
                )
                self._metrics.observe_ms("candidate_gate.remaining_window_ms", 0.0)
                log.warning(
                    "candidate_gate_deadline",
                    quote_id=quote_id,
                    attempt=attempt,
                    elapsed_ms=round(exc.elapsed_ms, 1),
                    budget_ms=round(budget_ns / 1e6, 1),
                    stage=exc.stage,
                    detail="candidate-gate input build abandoned at its deadline "
                    "— resolving on the deterministic caps",
                )
                return await self._candidate_gate_fallback(
                    quote_id,
                    state,
                    reservation_id=reservation_id,
                    cause=f"build_aborted:{exc.stage}",
                    detail=(
                        "candidate gate: input build exceeded the derived confirm "
                        f"budget in {exc.stage}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — any build error declines
                log.error(
                    "candidate_gate_errored", quote_id=quote_id, error=repr(exc)
                )
                return (
                    False,
                    f"candidate gate errored: {exc!r}",
                    ReasonCode.DECLINE_CANDIDATE_RISK,
                )
            build_ms = (self._clock.monotonic_ns() - build0_ns) / 1e6
            self._metrics.observe_ms("candidate_gate.build_ms", build_ms)
            # ---- The MC, BOUNDED by what is actually left of the window -------
            # No prediction: the remaining budget is MEASURED off the clock and
            # handed to the MC as its deadline, exactly as the last-look waiver
            # already bounds its off-loop enumeration
            # (BookRiskPool.run_state_worst_case). The MC therefore ALWAYS RUNS
            # while any window remains, and a run that cannot finish inside the
            # window resolves as a TIMEOUT through the deterministic fallback —
            # the same money outcome a "predicted over budget" skip aimed for,
            # reached by measurement instead of prophecy, and with no path-
            # dependent state that could make the next accept skip too.
            elapsed_ns = self._clock.monotonic_ns() - start_ns
            remaining_ns = budget_ns - elapsed_ns
            if remaining_ns <= 0:
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
                    budget_ms=round(budget_ns / 1e6, 1),
                    remaining_ms=round(remaining_ns / 1e6, 1),
                    detail="the input build consumed the confirm budget — no "
                    "window remains for the MC",
                )
                return await self._candidate_gate_fallback(
                    quote_id,
                    state,
                    reservation_id=reservation_id,
                    cause="mc_no_budget",
                    detail=(
                        "candidate gate: MC refinement did not fit the derived "
                        "confirm budget"
                    ),
                )
            mc0_ns = self._clock.monotonic_ns()
            try:
                result = await self._run_candidate_mc(
                    inputs, deadline_s=remaining_ns / 1e9
                )
            except TimeoutError:
                # LATENCY, NOT A RISK VERDICT: the MC did not land inside the
                # window. The worker finishes and frees itself; we resolve on the
                # deterministic caps re-checked against the live book.
                self._metrics.inc("candidate_gate.deadline_exceeded")
                self._metrics.inc("candidate_gate.window_expired_before_confirm")
                self._metrics.inc("candidate_gate.mc_timeout")
                mc_ms = (self._clock.monotonic_ns() - mc0_ns) / 1e6
                self._metrics.observe_ms(
                    "candidate_gate.runtime_ms",
                    (self._clock.monotonic_ns() - start_ns) / 1e6,
                )
                self._metrics.observe_ms("candidate_gate.remaining_window_ms", 0.0)
                log.warning(
                    "candidate_gate_deadline",
                    quote_id=quote_id,
                    attempt=attempt,
                    mc_ms=round(mc_ms, 1),
                    budget_ms=round(budget_ns / 1e6, 1),
                    detail="the candidate MC exceeded the remaining confirm "
                    "budget — resolving on the deterministic caps",
                )
                return await self._candidate_gate_fallback(
                    quote_id,
                    state,
                    reservation_id=reservation_id,
                    cause="mc_timeout",
                    detail=(
                        "candidate gate: MC refinement did not fit the derived "
                        "confirm budget"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — any error declines (fail-closed)
                log.error(
                    "candidate_gate_errored", quote_id=quote_id, error=repr(exc)
                )
                return (
                    False,
                    f"candidate gate errored: {exc!r}",
                    ReasonCode.DECLINE_CANDIDATE_RISK,
                )
            mc_ns = self._clock.monotonic_ns() - mc0_ns
            # LIVE CANDIDATE-GATE LATENCY: one observation per MC attempt feeds the
            # candidate-gate p50/p90/p99 runtime histogram; the MC worker queue dwell
            # (submit→worker-start, decomposed by the pool from total await − in-worker
            # compute) is recorded when a pool ran it (inline runs have no queue).
            self._metrics.observe_ms("candidate_gate.mc_ms", mc_ns / 1e6)
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
            self._record_gate_completion_latency(start_ns, budget_ns)
            # P1 EV VISIBILITY: log the production candidate EV DISTINCTLY from the
            # challenger / bridge / split candidate EVs (and the worst-credible EV), so
            # a candidate that is +EV under production yet −EV under a challenger is
            # visible in the logs even when it is ADMITTED (the admission policy stays
            # production-model-EV based).
            self._log_candidate_gate_ev(quote_id, attempt, result)
            if result.unknown:
                return (
                    False,
                    f"candidate gate UNKNOWN: {result.decline_reason}",
                    ReasonCode.DECLINE_CANDIDATE_RISK,
                )
            if not result.confirm:
                return (
                    False,
                    f"candidate gate declined: {result.decline_reason} "
                    f"(admission_ev_cc={result.admission_ev_cc:.1f} "
                    f"[{result.admission_ev_source}], "
                    f"cand_ev_cc={result.candidate_ev_cc:.1f}, "
                    f"worst_challenger_ev_cc="
                    f"{result.worst_credible_candidate_ev_cc:.1f}, "
                    f"post_es_cc={result.post.governing_model_es_99_cc:.0f}, "
                    f"post_det_cc={result.post.deterministic_max_loss_cc:.0f}, "
                    f"post_mutex_det_cc="
                    f"{_fmt_opt_cc(result.post.mutex_aware_det_max_cc)}, "
                    f"post_p_ruin={result.post.p_ruin:.4f})",
                    ReasonCode.DECLINE_CANDIDATE_RISK,
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
                # ΔP(book) VISIBILITY (2026-07-25): does THIS fill add
                # variance/diversity (delta > 0) or concentrate one-way
                # (delta < 0)? Shadow — logged on every admitted fill.
                post_p_book=round(result.post.p_profit, 4),
                delta_p_book=round(result.candidate_delta_p_book, 4),
                n_pre=result.n_pre_positions,
            )
            return True, "", None
        # Retry budget exhausted without a stable verdict: the book kept moving
        # under every attempt. This is a STABILITY/LATENCY outcome, not a risk
        # verdict — the MC never got to price the live book — so it resolves the
        # same way a timeout does: the ENFORCED deterministic caps, re-checked
        # against the book as it stands RIGHT NOW (which is precisely the book
        # whose churn defeated the MC). Still declines when those caps refuse.
        self._metrics.inc("candidate_gate.retries_exhausted")
        self._record_gate_completion_latency(start_ns, budget_ns)
        log.warning(
            "candidate_gate_unstable",
            quote_id=quote_id,
            retries=self._config.candidate_gate_max_retries,
            detail="book moved under every candidate-MC attempt — resolving on "
            "the deterministic caps against the current book",
        )
        return await self._candidate_gate_fallback(
            quote_id,
            state,
            reservation_id=reservation_id,
            cause="retries_exhausted",
            detail="candidate gate unstable: reservation/book moved every retry",
        )

    def _record_gate_completion_latency(
        self, start_ns: int, budget_ns: int | None = None
    ) -> None:
        """LIVE CANDIDATE-GATE LATENCY: at a terminal gate outcome record the total
        gate runtime (all attempts) and the remaining confirm-window time — the
        budget left when the verdict landed. A negative remainder (the gate
        overran the budget) is clamped to 0 (no time left). ``budget_ns`` is the
        DERIVED budget this gate actually ran under; None falls back to the static
        config value (callers that predate the derived budget)."""
        elapsed_ns = self._clock.monotonic_ns() - start_ns
        if budget_ns is None:
            budget_ns = int(self._config.candidate_gate_deadline_s * 1e9)
        self._metrics.observe_ms("candidate_gate.runtime_ms", elapsed_ns / 1e6)
        self._metrics.observe_ms(
            "candidate_gate.remaining_window_ms",
            max(0.0, (budget_ns - elapsed_ns) / 1e6),
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
            # ΔP(book) VISIBILITY (2026-07-25 operator directive): logged on
            # EVERY gate verdict (admit or decline) so the shadow record shows
            # which flow raises P(book) (variance/diversifiers/offsetting) and
            # which lowers it (one-way concentration) — the measured input the
            # Phase-B steering derives from.
            pre_p_book=round(result.pre.p_profit, 4),
            post_p_book=round(result.post.p_profit, 4),
            delta_p_book=round(result.candidate_delta_p_book, 4),
            # GATE EV SOURCE audit (2026-07-25 review): the EV that judged
            # admission and which fair produced it — verifiable per fill.
            admission_ev_cc=round(result.admission_ev_cc, 2),
            admission_ev_source=result.admission_ev_source,
        )

    def _gate_build_deadline_check(self, deadline_ns: int | None, stage: str) -> None:
        """Abandon the candidate-gate input build if the absolute wall has passed.
        Called between the build's heavy stages so no single stage can spend the
        whole confirm window."""
        if deadline_ns is None:
            return
        now_ns = self._clock.monotonic_ns()
        if now_ns > deadline_ns:
            raise _GateBudgetExceeded(stage, (now_ns - deadline_ns) / 1e6)

    def _build_candidate_gate_inputs(
        self,
        quote_id: str,
        state: OpenQuoteState,
        *,
        exclude_reservation_id: str | None = None,
        deadline_ns: int | None = None,
    ) -> CandidateBookRiskInputs:
        """Build the IMMUTABLE, picklable inputs for one off-loop candidate MC.

        INTERRUPTIBLE (2026-07-27). ``deadline_ns`` is an ABSOLUTE monotonic wall.
        The O(T^2) rho resolution and the two other heavy stages (the fresh
        re-price, the committed-book model) check it and raise
        ``_GateBudgetExceeded`` rather than run past the exchange's confirm
        window. This was the live killer: the build alone is what blew the budget
        on 23 of 24 lost auctions, and a synchronous loop that cannot be
        abandoned is a loop that spends the WHOLE window before anyone can react.
        Partial work is still MEASURED (a ms/pair rate is recorded for the pairs
        that completed), so the very next accept predicts the cost correctly and
        declines to start. None = no deadline (the pre-2026-07-27 behaviour, used
        by every non-confirm caller and by the rollback path).

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
        # The merged book, in the EXACT order the worker's
        # ``evaluate_candidate_book_risk`` assembles it (committed, reservations,
        # then the candidate). Both the leg universe and the same-game grouping
        # are first-occurrence ordered, so this order is load-bearing.
        merged: tuple[OpenPosition, ...] = (*committed, *reservations, candidate)
        # Universe of distinct leg tickers across the merged book.
        tickers: set[str] = set()
        for pos in merged:
            for leg in pos.legs:
                tickers.add(leg.market_ticker)
        # Resolve marginals ON-LOOP; a missing marginal is OMITTED (⇒ None in the
        # worker ⇒ UNKNOWN model ⇒ decline). Never fabricate a p=0.5 (defense #2).
        marginals: dict[str, float] = {}
        for ticker in tickers:
            p = self._marginals(ticker)
            if p is not None:
                marginals[ticker] = float(p)
        # FIX 2: resolve the EXCHANGE'S OWN determinations ON-LOOP too (the
        # worker has no resolver, and the live one is not picklable). Only exact
        # 0.0/1.0 facts are shipped; a ticker the exchange has not determined is
        # OMITTED, so the worker reads it as UNKNOWN and charges its position in
        # full — the same fail-closed contract the marginals dict already has.
        # Populated only when ARMED, so the shadow build ships an empty dict and
        # the gate is byte-identical.
        settled_facts: dict[str, float] = {}
        if self._det_max_settlement_aware():
            for ticker in tickers:
                fact = self._settled_fact(ticker)
                if fact == 0.0 or fact == 1.0:
                    settled_facts[ticker] = float(fact)
        # Resolve within-game pair rho ON-LOOP for EXACTLY the pairs
        # ``build_book_model`` will ask for — no more, no fewer.
        #
        #   NO MORE: the pre-2026-07-27 build resolved every unordered ticker
        #   pair on the theory that a superset is harmless. It is not. On the
        #   live 100-position book that is 22,155 pairs against 879 same-game
        #   ones — 96.03% of the work computed and thrown away, and it is what
        #   burnt the 3.0 s exchange confirm window (measured COLD: 4,507 ms vs
        #   197 ms; at 3x the book, 35,505 ms vs 521 ms).
        #
        #   NO FEWER: a same-game pair MISSING from this dict makes the worker's
        #   ``_DictWithinGameRho`` return None, ``build_book_model`` substitute
        #   ``flat_band``, and the joint tail come out at LESS correlation than
        #   the pricer measured — a silent UNDERSTATEMENT of risk. So the pair
        #   set is not re-derived here: ``within_game_pair_tickers`` is the SAME
        #   grouping code ``build_book_model`` itself runs (shared primitives in
        #   sim/book_model.py), fed the SAME ordered positions and the SAME
        #   priced-predicate the worker's ``_DictMarginals`` implements
        #   (present in ``marginals`` ⇔ priceable). Equality is by construction.
        #
        # Only pairs WITH a band are stored; a pair the provider maps to None is
        # omitted (the worker provider then returns None for it, exactly as the
        # live provider would).
        rho_pairs: dict[frozenset[str], tuple[float, float, float]] = {}
        if self._within_game_rho is not None:
            same_game_pairs = within_game_pair_tickers(
                merged, lambda ticker: ticker in marginals
            )
            total_pairs = len(same_game_pairs)
            check_every = max(1, int(self._config.candidate_gate_build_check_pairs))
            rho0_ns = self._clock.monotonic_ns()
            done = 0
            for ticker_a, ticker_b in same_game_pairs:
                band = self._within_game_rho(ticker_a, ticker_b)
                if band is not None:
                    rho_pairs[frozenset((ticker_a, ticker_b))] = band
                done += 1
                if deadline_ns is not None and done % check_every == 0:
                    now_ns = self._clock.monotonic_ns()
                    if now_ns > deadline_ns:
                        raise _GateBudgetExceeded(
                            f"rho_pairs[{done}/{total_pairs}]",
                            (now_ns - rho0_ns) / 1e6,
                        )
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
        self._gate_build_deadline_check(deadline_ns, "build_book_model")
        committed_only_model = build_book_model(
            list(committed),
            marginals=self._marginals,
            within_game_rho=self._within_game_rho,
        )
        current_equity_cc = self._ruin_equity_basis_cc(committed_only_model)
        # The fresh re-price is a full engine call (measured 160-450 ms live) —
        # the last heavy stage, and the last place worth abandoning before the
        # window is gone.
        self._gate_build_deadline_check(deadline_ns, "pricing_edge")
        pricing_edge_cc = self._gate_pricing_edge(state)
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
            # B2 (2026-07-25): derived budget = certified tail reduction —
            # pay $1 of EV per $1 of risk removed; static budget stays a
            # manual floor. Default OFF.
            hedge_budget_tail_derived=self._config.hedge_budget_tail_derived,
            # TAIL-PROBABILITY BOOK GATE (2026-07-25 operator anchor): bind
            # P(KILL-distance night) instead of the ES99 average. Default OFF.
            tail_prob_gate=self._config.tail_prob_gate,
            kill_tail_prob=self._config.kill_tail_prob,
            # KILL-ANCHORED BOOK GATE (2026-07-29; demotion RATIFIED
            # 2026-07-31): read off the SAME ``RiskLimits`` the quote-time cap
            # enforces (not a second config copy), so cap and gate can never
            # disagree about the anchor OR about which det-max wall is in
            # force (armed + governing ⇒ both sites demote to
            # ``cap_family.det_max_backstop_frac()``; see
            # ``risk/limits.RiskLimits.kill_anchored_book_gate``). The KILL
            # line rides along as a float.
            kill_anchored_book_gate=bool(limits.kill_anchored_book_gate),
            hard_trip_frac=float(limits.hard_trip_frac),
            # GATE EV SOURCE (2026-07-25 renege root cause #2): the CALIBRATED
            # pricing fair's edge for THIS fill — the same fair that priced
            # the quote. None when no sized pending fill (the gate then keeps
            # its MC EV, fail-safe). Armed via gate_ev_from_pricing_fair.
            gate_ev_from_pricing_fair=self._config.gate_ev_from_pricing_fair,
            # Fresh re-price only when armed (one engine call per confirm
            # attempt); OFF ⇒ None ⇒ the gate keeps its MC EV, byte-identical.
            pricing_edge_cc=pricing_edge_cc,
            # P(BOOK) NON-DECREASE doctrine gate (2026-07-25). Default OFF.
            require_p_book_non_decreasing=(
                self._config.require_p_book_non_decreasing
            ),
            # P1 EV VISIBILITY: the OPTIONAL worst-challenger-EV tolerance. −inf by
            # default (no behaviour change — the gate stays production-model-EV only);
            # a finite operator value ALSO declines a +production-EV candidate whose
            # worst credible challenger EV falls below it (strictly additive).
            worst_challenger_ev_tolerance=self._config.worst_challenger_ev_tolerance_cc,
            # MUTEX-AWARE DET-MAX rollback switch: the SAME RiskLimits knob the
            # quote-time cap honors, threaded to the worker gate so knob=False
            # restores comonotone gating at BOTH sites (verify finding 2026-07-18).
            det_max_mutex_aware=bool(limits.portfolio_det_max_mutex_aware),
            # FIX 3 HEDGE ACCOUNTING arming (2026-07-28): the SAME RiskLimits
            # knob the quote-time det-max cap honors, so both sites arm together
            # and shadow together (a looser gate than cap is the renege zone).
            det_max_hedge_credit=bool(limits.det_max_hedge_credit),
            # FIX 2 SETTLED-LEG DET-MAX arming (2026-07-28) + the determinations
            # themselves, so the confirm-time gate retires the same already-
            # decided combos the quote-time cap does. Arming together is the
            # point: a gate STRICTER than the cap is the renege zone.
            settled_facts=settled_facts,
            det_max_settlement_aware=self._det_max_settlement_aware(),
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
        # MEASURE the deterministic cap check (2026-07-27). This is the SAME
        # LimitChecker.check the candidate gate's timeout fallback runs, so timing
        # it here — on every accept, before the gate — keeps the derived confirm
        # budget's fallback reserve warm from the first accept of a process
        # instead of waiting for a timeout to happen first.
        check0_ns = self._clock.monotonic_ns()
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
            waived_games=waived_games,
            # Confirm-time resting haircut (operator 2026-07-17): committed +
            # reservations + candidate stay at 100%; only the resting fold
            # weights. Default False = today; armed in the local YAML.
            apply_resting_haircut=self._config.resting_haircut_at_confirm,
            # DEPLOYMENT SCALE: the AUTHORITATIVE confirm-path check breathes at
            # the SAME solved scale quote-time admission used — size-layer
            # coherence (quote-time never looser than confirm) is exactly what
            # keeps this out of the renege zone. 1.0 while disarmed.
            deploy_scale=self.deploy_scale_for_check(),
        )
        self._metrics.observe_ms(
            "confirm.deterministic_check_ms",
            (self._clock.monotonic_ns() - check0_ns) / 1e6,
        )
        return result

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
        # SLATE breaches are certificate-RESOLVABLE (2026-07-17): the slate cap
        # sums the per-game analytic losses, and the retry's certificate-aware
        # roll-up substitutes each certified game's exact worst case — so a
        # denial carrying slate breaches ALONGSIDE per-game waivable breaches
        # still arms the waiver; the retry then re-checks the slate HONESTLY on
        # the substituted sum (fail-closed if it still breaches).
        #
        # SLATE-AXIS EXTENSION (2026-07-25, operator-armed via
        # ``lastlook_waiver_slate_axis``; the 7/25 live evidence: 7 accepted
        # +EV fills bounced on slate-ONLY denials while REAL committed risk was
        # ~$7 — the burst-floor top-K resting projection alone spans the slate
        # budget): a slate-ONLY denial now certifies the LARGEST analytic
        # contributors to the slate sum (top ``_SLATE_WAIVER_MAX_GAMES`` by
        # comonotone worst-case — a compute bound under the enumeration
        # deadline, not a risk number; the deadline itself fail-closes). The
        # SAME machinery runs: exact per-game state-consistent certificates,
        # each re-validated against the UNCHANGED game budget, substituted into
        # the slate roll-up by the retry's checker — the slate threshold is
        # never raised; only the comonotone overstatement is disproved. Flag
        # OFF (default) ⇒ the pre-2026-07-25 decline, byte-identical.
        core = [b for b in breaches if b.reason is not ReasonCode.SKIP_SLATE_CAP]
        slate_only = bool(breaches) and not core
        if slate_only and self._config.lastlook_waiver_slate_axis:
            snap = self._exposure.snapshot(self._marginals, mass_acceptance=True)
            ranked = sorted(
                snap.worst_case_loss_by_game_cc.items(), key=lambda kv: -kv[1]
            )
            games = sorted(
                g for g, _loss in ranked[:_SLATE_WAIVER_MAX_GAMES]
            )
            if not games:
                return False, "slate breach with no games to certify"
        elif not core or any(
            b.reason not in WAIVABLE_RESERVATION_BREACHES for b in core
        ):
            # A non-waivable cap (gross/per-combo/daily/CVaR/ruin/notional/
            # halt…) is part of the denial — or a slate-ONLY denial with the
            # slate axis un-armed: never waived, decline as today.
            return False, "non-waivable breach in denial"
        elif any(b.game is None for b in core):
            # A per-game breach without its game key cannot be certified.
            return False, "waivable breach missing its game key (fail-closed)"
        else:
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
                # whose bound still held). The stamps stay EXACT — global
                # position generation + reservation version by default, or
                # (``waiver_game_scoped_stability``, 2026-07-25) the BREACHED
                # GAMES' position/reservation content, so a same-game fill or
                # reservation still invalidates while an unrelated-game one
                # no longer does (the retry re-checks live cross-game state
                # anyway). Quote churn is then judged by grant-time
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
    ) -> tuple[object, object, tuple[str, ...]]:
        """Stability key for a waiver enumeration (2026-07-18): the POSITION
        generation + reservation version + the ids of the resting quotes
        touching the BREACHED games — or, with ``waiver_game_scoped_stability``
        (2026-07-25), the breached games' position/reservation CONTENT in
        place of the global counters (same-game changes still invalidate;
        unrelated-game fills no longer do — see the scoped branch below). A
        quote landing/expiring on an UNRELATED
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
        if self._config.waiver_game_scoped_stability:
            # GAME-SCOPED STABILITY (2026-07-25 — the peak-flow waiver gap):
            # the GLOBAL position generation / reservation version bump on
            # EVERY concurrent fill anywhere, so at tonight's fill rate the
            # waiver conflicted on every attempt ("book moved during every
            # enumeration", $1.99-EV wins reneged twice). A game's EXACT
            # certificate depends only on SAME-GAME entities; cross-game
            # moves are re-checked anyway by the reservation retry against
            # the LIVE book (certificates replace only the breached games'
            # terms). So compare the breached games' POSITION/RESERVATION
            # CONTENT instead of the global counters: any same-game change
            # (id or size) still invalidates — an unrelated-game fill no
            # longer does.
            def _touches(pos: OpenPosition) -> bool:
                return any(
                    leg.event_ticker and game_key(leg.event_ticker) in gset
                    for leg in pos.legs
                )

            scoped_positions = tuple(sorted(
                (p.position_id, int(p.contracts), int(p.entry_price_cc))
                for p in self._exposure.positions.values()
                if _touches(p)
            ))
            scoped_reservations = tuple(sorted(
                (p.position_id, int(p.contracts), int(p.entry_price_cc))
                for p in (
                    self._reservation.outstanding_positions()
                    if self._reservation is not None
                    else ()
                )
                if _touches(p)
            ))
            return (scoped_positions, scoped_reservations, qids)
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

    # FIX 4 anti-thrash ledger cap (combo_ticker -> density that displaced it).
    _EVICTION_LEDGER_MAX = 512

    @staticmethod
    def _quote_det_consumed_cc(quote: OpenQuoteRisk) -> int:
        """The det-max budget a RESTING quote consumes: the worse quotable
        side's premium at risk, ``contracts x bid // 100`` — the EXACT
        arithmetic ``sim.book_risk._det_and_gross`` charges the hypothetical
        fill (both quotable sides fold in as hypotheticals and the det axis
        takes the worse). Correlation-INDEPENDENT by construction, which is
        what makes ranking on it MODEL-FREE (FIX 4, 2026-07-28)."""
        return max(
            int(quote.contracts) * int(bid) // 100
            for bid in (int(quote.yes_bid_cc), int(quote.no_bid_cc), 0)
        )

    @staticmethod
    def _value_density(ev_cc: int, det_cc: int) -> float | None:
        """EV per unit of consumed det-max. None when the denominator is not
        positive — a zero-consumption quote has no density, is never ranked,
        and is never chosen as the loser (UNKNOWN is never a convenient
        loser, quiet-failure defense 2)."""
        if det_cc <= 0:
            return None
        return float(ev_cc) / float(det_cc)

    def _note_eviction(self, combo_ticker: str, winner_density: float) -> None:
        """Record that ``combo_ticker`` was evicted by a candidate of density
        ``winner_density`` (bounded FIFO ledger)."""
        ledger = self._evicted_density
        prior = ledger.pop(combo_ticker, None)
        ledger[combo_ticker] = (
            winner_density if prior is None else max(prior, winner_density)
        )
        while len(ledger) > self._EVICTION_LEDGER_MAX:
            ledger.pop(next(iter(ledger)))

    def _thrash_blocked(self, combo_ticker: str, density: float) -> bool:
        """ANTI-THRASH INVARIANT (FIX 4): a combo evicted at density ``d`` may
        not itself evict anything until its OWN density strictly exceeds ``d``.

        A evicting B requires density(A) > density(B); B could only evict its
        way back in with density(B) > density(A), which this refuses. So an
        evicted quote can never immediately re-enter by displacing its evictor.
        It CAN still be admitted normally when the budget genuinely frees up —
        that is correct reallocation, not thrash — and a genuine reprice to a
        higher density clears the block."""
        prior = self._evicted_density.get(combo_ticker)
        return prior is not None and density <= prior

    async def _try_slot_eviction(
        self,
        rfq: Rfq,
        result: ConstructedQuote,
        risk_qty: CentiContracts,
        quote_risk: OpenQuoteRisk,
        raw_breaches: list[Breach],
    ) -> list[Breach]:
        """VALUE-RANKED ALLOCATION OF A FIXED BUDGET — ONE eviction mechanism,
        two axes (2026-07-25 slot axis; 2026-07-28 FIX 4 det-max axis).

        Fires ONLY when EVERY ENFORCED breach is the SINGLE axis being
        reallocated (any other enforced wall stands untouched — this must never
        loosen a risk cap), and after any eviction the SAME
        ``LimitChecker.check`` re-runs so a surviving breach declines exactly as
        before. It decides WHICH candidates get a fixed budget, NEVER whether
        the budget exists.

          * SLOT axis (``open_quote_ev_eviction``, SKIP_MAX_OPEN_QUOTES) —
            ranks on raw quote-time EV. Every resting quote consumes exactly
            one slot, so EV and EV-per-slot rank identically. UNCHANGED from
            2026-07-25.
          * DET-MAX axis (``det_budget_value_ranking``, SKIP_PORTFOLIO_DET_MAX)
            — ranks on DENSITY = EV / consumed det-max, because quotes consume
            the det budget in wildly different amounts. Measured on the
            2026-07-27 won-auction set: the 17 auctions we DECLINED were 24%
            DENSER than the 21 we confirmed (0.0491 vs 0.0395 EV per unit of
            det). Arrival order was actively selecting the worse half of the
            flow. det-max is correlation-INDEPENDENT, so the reordering is
            MODEL-FREE — a deterministic knapsack, not a model call.

        Both axes compare STORED quote-time EVs (all produced by the same
        ``_quote_candidate_ev_cc`` at issue/reprice, apples to apples); a quote
        with UNKNOWN stored EV is never chosen as the loser. Strict ``>`` on the
        ranking key, one attempt per RFQ, and the anti-thrash ledger bound
        churn. Mode ``"shadow"`` (the det axis default) LOGS the reallocation it
        would have made and deletes nothing — the decision path is byte-
        identical to today."""
        enforced = [b for b in raw_breaches if not b.shadow]
        if not enforced:
            return raw_breaches
        reasons = {b.reason for b in enforced}
        if reasons == {ReasonCode.SKIP_MAX_OPEN_QUOTES}:
            if not self._config.open_quote_ev_eviction:
                return raw_breaches
            return await self._try_axis_eviction(
                rfq, result, risk_qty, quote_risk, raw_breaches,
                axis="slot", mode="on",
            )
        if reasons == {ReasonCode.SKIP_PORTFOLIO_DET_MAX}:
            mode = str(self._config.det_budget_value_ranking)
            if mode == "off":
                return raw_breaches
            return await self._try_axis_eviction(
                rfq, result, risk_qty, quote_risk, raw_breaches,
                axis="det_max", mode=mode,
            )
        return raw_breaches

    async def _try_axis_eviction(
        self,
        rfq: Rfq,
        result: ConstructedQuote,
        risk_qty: CentiContracts,
        quote_risk: OpenQuoteRisk,
        raw_breaches: list[Breach],
        *,
        axis: str,
        mode: str,
    ) -> list[Breach]:
        """One axis of the value-ranked reallocation above. ``axis`` selects the
        ranking key ("slot" = raw EV, "det_max" = EV per consumed det-max);
        ``mode`` is "on" (evict) or "shadow" (log the would-be eviction and
        return the breaches untouched)."""
        cand_ev = self._quote_candidate_ev_cc(result, risk_qty)
        if cand_ev is None:
            return raw_breaches
        cand_det = 0
        if axis == "det_max":
            cand_det = self._quote_det_consumed_cc(quote_risk)
            cand_key_opt = self._value_density(cand_ev, cand_det)
            if cand_key_opt is None:
                return raw_breaches
            cand_key = cand_key_opt
            if self._thrash_blocked(quote_risk.combo_ticker, cand_key):
                self._metrics.inc("quote.eviction_thrash_blocked")
                return raw_breaches
        else:
            cand_key = float(cand_ev)
        loser: OpenQuoteRisk | None = None
        loser_key: float | None = None
        for q in self._exposure.open_quotes.values():
            if q.expected_edge_cc is None:
                continue
            if axis == "det_max":
                key_opt = self._value_density(
                    q.expected_edge_cc, self._quote_det_consumed_cc(q)
                )
                if key_opt is None:
                    continue
                key = key_opt
            else:
                key = float(q.expected_edge_cc)
            if loser_key is None or key < loser_key:
                loser, loser_key = q, key
        if loser is None or loser_key is None or loser.expected_edge_cc is None:
            return raw_breaches
        if cand_key <= loser_key:
            return raw_breaches
        if mode != "on":
            # SHADOW: report the reallocation the operator arms from and change
            # NOTHING. Bounded by the breach rate, not the RFQ rate.
            self._metrics.inc("quote.eviction_shadow." + axis)
            log.info(
                "open_quote_eviction_shadow",
                axis=axis,
                would_evict_quote_id=loser.quote_id,
                evicted_ev_cc=loser.expected_edge_cc,
                evicted_key=loser_key,
                candidate_rfq_id=rfq.rfq_id,
                candidate_ev_cc=cand_ev,
                candidate_det_cc=cand_det,
                candidate_key=cand_key,
            )
            return raw_breaches
        self._metrics.inc("quote.slot_evictions")
        self._metrics.inc("quote.evictions." + axis)
        log.info(
            "open_quote_evicted",
            axis=axis,
            evicted_quote_id=loser.quote_id,
            evicted_ev_cc=loser.expected_edge_cc,
            evicted_key=loser_key,
            candidate_rfq_id=rfq.rfq_id,
            candidate_ev_cc=cand_ev,
            candidate_key=cand_key,
        )
        if axis == "det_max":
            self._note_eviction(loser.combo_ticker, cand_key)
        await self._delete_quote(loser.quote_id, ReasonCode.DELETE_EVICTED_LOWER_EV)
        return self._limits.check(
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
            apply_resting_haircut=True,
            # DEPLOYMENT SCALE: quote-time and confirm-time breathe at the SAME
            # solved scale (never diverge — a looser quote-time cap than confirm
            # is the renege zone). 1.0 while disarmed ⇒ byte-identical.
            deploy_scale=self.deploy_scale_for_check(),
        )

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
            # DEPLOYMENT SCALE: quote-time and confirm-time breathe at the SAME
            # solved scale (never diverge — a looser quote-time cap than confirm
            # is the renege zone). 1.0 while disarmed ⇒ byte-identical.
            deploy_scale=self.deploy_scale_for_check(),
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
        # BOOT-WARMUP QUOTE GATE (2026-07-31): hold quote SENDING until the
        # first usable book-risk verdict (the confirm gate's own predicate,
        # reused). BEFORE filter/pricing — no work is spent on a quote that
        # could only be reneged. Leg watching/metadata warm-up happens in
        # quote_app._ensure_watched BEFORE this method, so books keep warming
        # during the hold; the skip is durably recorded per RFQ (hedge-watch).
        if not self.quote_warmup_open():
            self._metrics.inc("quote.warmup_held")
            await self._record_skip(
                rfq,
                [ReasonCode.SKIP_WARMUP_BOOK_RISK],
                {
                    "detail": "boot warmup: no usable book-risk snapshot yet — "
                    "quote sending held (fail-closed)"
                },
            )
            return
        reasons = self._filter.evaluate(rfq)
        if reasons:
            await self._record_skip(rfq, reasons, self._pregame_flow_context(rfq, reasons))
            # IN-PLAY SHADOW (2026-07-25): AFTER the skip is durably recorded —
            # measurement only, exception-proof, can never touch the decision.
            await self._maybe_shadow_inplay(rfq, reasons)
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
            # DEPLOYMENT SCALE: quote-time and confirm-time breathe at the SAME
            # solved scale (never diverge — a looser quote-time cap than confirm
            # is the renege zone). 1.0 while disarmed ⇒ byte-identical.
            deploy_scale=self.deploy_scale_for_check(),
            # FIX 1 / FIX 2 SHADOW READ-OUTS (2026-07-27). One line per
            # entity-axis ticket-admission verdict (admitted AND refused) and one
            # per would-be slate refusal, carrying the naive sum next to the
            # once-counted joint worst case — the numbers the operator arms from.
            # Observational only: ``check`` never branches on either, both fire
            # ONLY on a would-be refusal (bounded by the breach rate, not the RFQ
            # rate), and a logging failure is swallowed inside.
            entity_admission_observer=self._log_entity_admission,
            slate_partition_observer=self._log_slate_partition,
        )
        # EV-BASED SLOT EVICTION (2026-07-25 big-fill audit: the slot cap was
        # arrival-order-blind — $3.2M/day of flow died while low-EV leftovers
        # held slots): when the ONLY enforced blocker is SKIP_MAX_OPEN_QUOTES
        # and this candidate's quote-time EV beats the weakest resting
        # quote's stored EV, evict the loser and re-check once.
        # FIX 4 (2026-07-28): the SAME mechanism now also reallocates the
        # DET-MAX budget by value. The dispatcher below is a no-op unless the
        # enforced breach set is exactly one reallocatable axis whose mode is
        # not "off"; the det axis defaults to "shadow" (log, never delete).
        if raw_breaches and (
            self._config.open_quote_ev_eviction
            or str(self._config.det_budget_value_ranking) != "off"
        ):
            raw_breaches = await self._try_slot_eviction(
                rfq, result, risk_qty, quote_risk, raw_breaches
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
        # SAME-LINK LATENCY WARM-UP (2026-07-27). The confirm-window budget is
        # derived from the MEASURED confirm round trip — but confirms are rare
        # (tens/day) and the FIRST one of a process has no history, so an
        # unmeasured link would force the gate to skip its MC refinement exactly
        # when the book is least understood. The quote POST is the same REST verb
        # to the same venue over the same link and happens thousands of times an
        # hour, so it is the honest MEASURED proxy until real confirm samples
        # exist. Two clock reads + one histogram insert; benchmarked on the quote
        # path (see the 2026-07-27 confirm-window report).
        create_rtt0_ns = self._clock.monotonic_ns()
        try:
            response = await self._sender.create_quote(
                rfq.rfq_id,
                yes_bid_cc=result.yes_bid_cc,
                no_bid_cc=result.no_bid_cc,
            )
            self._metrics.observe_ms(
                "quote.create_rtt_ms",
                (self._clock.monotonic_ns() - create_rtt0_ns) / 1e6,
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
        """ACCEPT → last look → confirm/lapse, plus the TERMINATION GUARANTEE the
        withdraw-resolver's mid-confirm deferral rests on (2026-07-26 gate, B1).

        ``_resolve_withdraw_pending`` refuses to resolve an ACCEPTED quote, so
        something must guarantee that an accepted quote always leaves ``_open``.
        Every branch of ``_on_quote_accepted`` already ends in ``_drop_quote``
        ("Accepted quotes are no longer open either way"); the one path that did
        not was an EXCEPTION escaping mid-confirm, which quote_app's
        ``quote_event_worker`` logs and moves past — leaving the state
        accepted + withdraw-pending in the mirror forever, i.e. the deferral
        turned into a strand. Dropping here is the SAME thing the normal branches
        do and is safe for the same reason: once accepted, the resting quote is
        economically dead in every outcome (confirm ⇒ the fill position replaces
        it; lapse/decline ⇒ the exchange voids it — there is no post-accept
        withdrawal). The position/reservation state is NOT touched: it lives in
        ``_executed_states`` and the reservation book, which the confirm-timeout
        reconcile and the executed-replay own. The exception is re-raised so the
        worker's own logging and any halt accounting are unchanged.

        ``BaseException`` (task cancellation at shutdown) is deliberately NOT
        caught: that path is the process stopping, where the startup reconcile
        rebuilds the book from the exchange."""
        quote_id = str(msg.get("quote_id", ""))
        try:
            await self._on_quote_accepted(msg)
        except Exception as exc:
            self._metrics.inc("confirm.errored")
            log.error(
                "quote_accepted_errored",
                quote_id=quote_id,
                error=repr(exc),
                detail="accept handling raised — the accepted quote is dropped "
                "from the mirror (as every normal branch does) so a deferred "
                "withdraw-pending state can never strand",
            )
            self._drop_quote(quote_id)
            raise

    async def _on_quote_accepted(self, msg: JsonDict) -> None:
        t0 = self._clock.monotonic_ns()
        # HONEST DEADLINE ANCHOR (2026-07-31 double confirm-expiry halt). The
        # exchange's confirm window (EXCHANGE_CONFIRM_WINDOW_NS, a protocol
        # fact) opens at the taker's accept — BEFORE this frame crossed the
        # network, the WS dispatch queue, and the quote-event lane. Anchoring
        # the derived confirm budget at handler start silently granted the gate
        # time the exchange had already spent: on all 12 'expired' confirms
        # ever taped, the in-handler time was <= 1.14s of the 3.0s window —
        # >= 1.86s died upstream of this line. The WS read loop stamps accept
        # frames the instant they leave the socket (ws.py priority lane) and
        # the intake passes the stamp through; anchoring there makes every
        # in-process delay deduct from the budget automatically, so a future
        # delivery regression degrades the gate toward its deterministic
        # fallback (fail-safe) instead of confirming into a dead window. The
        # residual unmeasurable is network+venue emit time only. Guarded to
        # [0, t0]: a clockless/foreign stamp can only ever SHRINK the budget's
        # view of the window, never grow it.
        recv_ns = msg.get("_ws_recv_mono_ns")
        if isinstance(recv_ns, int) and 0 < recv_ns <= t0:
            self._metrics.observe_ms(
                "confirm.accept_dispatch_delay_ms", (t0 - recv_ns) / 1e6
            )
            t0 = recv_ns
        quote_id = str(msg.get("quote_id", ""))
        state = self._open.get(quote_id)
        if state is None:
            # FAST-LANE CREATE RACE (2026-07-31): with accepts jumping the
            # dispatch backlog, an accept can now beat our OWN create POST's
            # response parse (the taker's auto-accept vs our REST round trip),
            # in which case ``_open`` has no entry YET. Wait exactly the
            # measured p99 of the same REST verb once — the only in-flight
            # state that can legitimately hide an accepted quote is that POST,
            # and its own latency distribution bounds how long until ``_open``
            # is populated. No samples yet ⇒ no wait (pre-fast-lane behavior).
            wait_ms = self._first_measured_quantile_ms(("quote.create_rtt_ms",), 0.99)
            if wait_ms is not None and wait_ms > 0:
                await asyncio.sleep(wait_ms / 1e3)
                state = self._open.get(quote_id)
                if state is not None:
                    self._metrics.inc("confirm.accept_beat_create_ack")
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

        # RELEASE THE ACCEPTED QUOTE'S OWN EXPOSURE (2026-07-25 review HIGH —
        # the renege zone's second half): once ACCEPTED, this quote's resting
        # entry is ECONOMICALLY DEAD in every outcome (confirm ⇒ the fill
        # position replaces it; lapse/decline ⇒ the exchange voids it — no
        # post-accept decline mechanism exists, NOTES.md). Leaving it in the
        # book made every confirm-path check count this fill's exposure TWICE
        # (once as the resting mass-acceptance hypothetical, once as the true
        # candidate) — confirm demanded ~2× the headroom quote-time admitted,
        # so won auctions on the summing axes (game/slate/directional) still
        # reneged even with award sizing. Removing it here restores the
        # no-double-risk-layers invariant; ``_drop_quote`` later is idempotent.
        if self._config.release_accepted_quote_exposure:
            self._exposure.remove_quote(quote_id)

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
                gate_ok, gate_detail, gate_reason = await self._candidate_gate_verdict(
                    quote_id, state,
                    reservation_id=(
                        reservation_id if self._reservation is not None else None
                    ),
                    # The DERIVED confirm budget is anchored on the ACCEPT, not on
                    # the gate's own start: everything the accept path already spent
                    # (last look, fill velocity, the reservation, and the last-look
                    # MC waiver when it ran) is then automatically deducted from the
                    # exchange's window — no static split between the two budgets to
                    # keep in sync by hand.
                    accept_ns=t0,
                )
                if not gate_ok:
                    # Release the provisional reservation: this fill is declined, so
                    # its held headroom must be freed immediately (fail-closed — never
                    # confirm, never leave headroom consumed for a non-fill).
                    if self._reservation is not None:
                        self._reservation.release(reservation_id)
                    # LATENCY vs RISK are DIFFERENT EVENTS (2026-07-27). The gate
                    # names which one this was; before the split, 100% of live
                    # confirm.declined.decline_candidate_risk was a stopwatch, which
                    # is exactly why no decline analysis ever surfaced it.
                    decline_reason = gate_reason or ReasonCode.DECLINE_CANDIDATE_RISK
                    self._metrics.inc(f"confirm.declined.{decline_reason}")
                    self._track_markout(f"declined:{quote_id}", state)
                    await self._record_confirm_decision(
                        state, confirm=False,
                        reason=decline_reason,
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
                # DURABLE POSITION LEDGER (2026-07-26): the open side of the
                # ledger the settlement handler later marks SETTLED. Without
                # this row the settled-write is a silent no-op (it UPDATEs
                # WHERE status='open'), which is exactly why position_ledger
                # was empty, p_night could not roll across restarts, and
                # settlement calibration was unanswerable from local data.
                # Fire-and-forget on the store's own queue; never blocks or
                # breaks the confirm path.
                self._ledger_record_open(quote_id, state)
                # FILL-RECORD RECOVERY (2026-07-16 P1): the confirm SUCCEEDED —
                # a quote_executed message is now EXPECTED. Stamp the clock so
                # the maintenance sweep can poll REST if it never arrives (the
                # WS channel has no replay). Only this success path stamps: a
                # failed/timed-out confirm is the unknown-committed path the
                # reservation-reconcile loop owns.
                state.fill_confirmed_mono_ns = self._clock.monotonic_ns()
                # WALL-time twin of the stamp above (2026-07-18 review): the
                # min_ts anchor for the cancel-verification /portfolio/fills
                # query — scopes matching to THIS execution window.
                state.fill_confirmed_wall_ts = int(self._clock.now().timestamp())
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

    def _ledger_record_open(self, quote_id: str, state: OpenQuoteState) -> None:
        """Write the OPEN row of the durable position ledger for a confirmed
        fill (2026-07-26). Best-effort by construction: any failure logs and
        the bot proceeds exactly as before (the in-memory book is unchanged
        and remains the risk source of truth)."""
        try:
            position = self._fill_position(quote_id, state)
            coro = self._store.record_position_open(
                position,
                subaccount=(
                    str(self._fills_subaccount)
                    if self._fills_subaccount is not None
                    else ""
                ),
            )
            asyncio.get_running_loop().create_task(coro)
        except Exception:
            log.exception("position_ledger_open_failed", quote_id=quote_id)

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
                    deploy_scale=self.deploy_scale_for_check(),
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
            if state.withdraw_pending_reason is not None:
                # Already being withdrawn, outcome UNKNOWN (B3). Re-picking it
                # would spend the whole eviction budget re-deleting one quote
                # instead of releasing a different game's exposure.
                continue
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
        # WS/poll COUNT CROSS-CHECK (2026-07-24 review): live combo executed
        # messages usually carry NO count field — but when one IS present and
        # reads SMALLER than pending, the exchange count is the truth (the
        # rule the cancel-verification path enforces). Runs AFTER the booking
        # block so the resize's remove+rebook always acts on a booked
        # position (reserved == booked stays exact through the commit).
        # Absent/unreadable keeps the larger booked size — bit-identical
        # prior behaviour on every live message observed so far.
        if "exchange_fill" not in msg:
            ws_count_raw = msg.get("contracts_accepted_fp") or msg.get("count_fp")
            if ws_count_raw is not None:
                self._resize_pending_to_exchange_count(
                    quote_id, state, {"count_fp": ws_count_raw}
                )
        fill_ref = f"fill:{quote_id}"
        # LEDGER-WRITE HARDENING (2026-07-18 requirement 2): the in-memory
        # commit above SUCCEEDED (position booked / evictions scheduled), so a
        # failed or crashed ledger write below must never pass silently — that
        # is exactly the "book counted it, persistence did not" divergence. Any
        # exception here is a LOUD ERROR + a retry: ``fill_recorded`` stays
        # False and ``fill_confirmed_mono_ns`` is stamped, so the maintenance
        # recovery sweep re-polls and replays this same path (bounded attempts,
        # loud exhaustion); the /portfolio/fills verification net applies if
        # the quote status comes back cancelled.
        try:
            await self._record_executed_fill(quote_id, state, msg, fill_ref)
            # CLAIM RELEASE (2026-07-24 review): once the row exists (or the
            # writer terminally refused it) the ledger's own order_id guard
            # owns dedupe — release any transient claim held for this order
            # (executed-status recovery claims; a no-op for plain WS fills).
            if state.fill_recorded and msg.get("order_id"):
                self._claimed_exchange_order_ids.discard(str(msg["order_id"]))
        except Exception:
            self._metrics.inc("fill_ledger.write_failed")
            if state.fill_confirmed_mono_ns is None:
                state.fill_confirmed_mono_ns = self._clock.monotonic_ns()
            if state.fill_confirmed_wall_ts is None:
                state.fill_confirmed_wall_ts = int(self._clock.now().timestamp())
            self._executed_states.setdefault(quote_id, state)
            log.exception(
                "fill_ledger_write_failed",
                quote_id=quote_id,
                fill_ref=fill_ref,
                detail="in-memory book holds this committed fill but the "
                "fills-ledger write failed — row NOT written yet; the recovery "
                "sweep will retry (bounded, loud on exhaustion)",
            )

    async def _record_executed_fill(
        self, quote_id: str, state: OpenQuoteState, msg: JsonDict, fill_ref: str
    ) -> None:
        """The fills-ledger tail of ``on_quote_executed`` — the ONE writer of
        fills rows (2026-07-16 P1). Split out so the caller can make any
        failure here a loud ERROR with a retry (2026-07-18 requirement 2)
        without touching the booking logic above it."""
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
        # ORDER-ID UNIQUENESS (2026-07-24 review): one exchange order must
        # never produce two ledger rows — fill_ref dedupes per QUOTE, not per
        # exchange order, so a cross-quote misattribution upstream (another
        # quote adopted this order's fill) would otherwise double-write it.
        # Terminal + LOUD; never a second row.
        order_id_raw = msg.get("order_id")
        if order_id_raw and await self._store.has_fill_for_order_id(
            str(order_id_raw)
        ):
            state.fill_recorded = True
            self._metrics.inc("fill_ledger.order_id_conflict")
            log.error(
                "fill_order_id_already_in_ledger",
                quote_id=quote_id,
                fill_ref=fill_ref,
                order_id=str(order_id_raw),
                detail="a fills row for this exchange order already exists "
                "under ANOTHER fill_ref — refusing a second row (one "
                "exchange order = one ledger row); this signals a cross-"
                "quote misattribution upstream: reconcile by hand",
            )
            return
        assert state.pending_fill is not None  # caller verified
        accepted_side, bid, qty = state.pending_fill
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
        # EXCHANGE-REPORTED FEE OVERRIDE (2026-07-18 verify-before-discard): a
        # late/taker-style execution recovered off /portfolio/fills carries the
        # REAL charged fee (both incidents: is_taker=true, nonzero fee_cost) —
        # the model's maker-quadratic $0 would understate a cash cost we
        # actually paid. The recovery replay passes it as ``exchange_fee_cc``
        # (int cc, parsed fail-closed by _exchange_fill_fee_cc); it beats the
        # model figure. Absent on every normal WS/poll message ⇒ bit-identical
        # prior behaviour.
        exchange_fee = msg.get("exchange_fee_cc")
        exchange_fee_reported = False
        if isinstance(exchange_fee, int) and not isinstance(exchange_fee, bool):
            exchange_fee_reported = True
            fill_fee_cc = int(exchange_fee)
        # EAT-THE-FEE accounting in the EV ledger: on a maker-fee-active series
        # the predicted fee is a known cash cost of this fill, so the recorded
        # expected edge is net of it (grading expected vs realized stays
        # apples-to-apples). Gated on the prefix list so an EMPTY list is
        # bit-identical to prior behaviour on every ledger row. An
        # EXCHANGE-REPORTED fee (late-execution recovery) is a real cash cost
        # regardless of the maker-fee list, so it nets the edge the same way.
        if (
            expected_edge_cc is not None
            and fill_fee_cc is not None
            and (
                exchange_fee_reported
                or self._maker_fee_active(
                    state.rfq.market_ticker, state.rfq.mve_collection_ticker
                )
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
            if state.cancel_verify_started_mono_ns is not None:
                # CANCEL-REPORT VERIFICATION in progress (2026-07-18): the
                # quote status said CANCELLED but the position stays booked
                # until /portfolio/fills proves the fill absent (or finds it —
                # both 2026-07-18 "cancelled" quotes were REAL taker-style
                # executions). Own cadence + bounds; never re-polls GET quote.
                polls += await self._cancel_verification_step(quote_id, state, now)
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
                    self._quote_getter.get_quote(quote_id),
                    timeout=_MAINTENANCE_POLL_TIMEOUT_S,
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
                    # CLAIM (2026-07-24 review): while this recovery is in
                    # flight, a concurrently-verifying quote on the same
                    # ticker must not adopt this order's fill (the cross-
                    # quote-steal hole); released once the row lands.
                    self._claimed_exchange_order_ids.add(str(order_id))
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
        no WS event exists for it, doc: rfq-flow.md).

        VERIFY-BEFORE-DISCARD (2026-07-18, two live incidents the same day):
        the cancel report is NOT trusted on its own. Quotes 903935fc (16:24Z)
        and 7d79f32b (18:30Z) both came back ``cancelled``
        (``cancellation_reason: "execution failed"``) from REST GET quote while
        the exchange EXECUTED the fill anyway as a taker-style REGULAR order —
        visible only on ``/portfolio/fills`` (nonzero fee, ``is_taker: true``),
        with no ``quote_executed`` WS message ever emitted. Discarding on the
        report alone removed a REAL position from the risk book (undercount —
        the dangerous direction). So: with a fills getter wired, the position
        STAYS BOOKED and verification polls /portfolio/fills on the sweep's
        next ticks (bounded attempts, injectable clock). Only a verified
        absence discards the phantom. No fills getter (paper/backtests/minimal
        rigs) or verification disabled ⇒ the prior immediate discard."""
        cancellation_reason = (
            quote.get("cancellation_reason") if isinstance(quote, dict) else None
        )
        if (
            self._fills_getter is None
            or self._config.fill_cancel_verify_attempts <= 0
        ):
            self._discard_phantom_position(
                quote_id,
                state,
                cancellation_reason=cancellation_reason,
                detail="confirmed quote came back CANCELLED from REST — fill "
                "never executed; phantom position removed, no fills row written"
                " (no fills getter wired — /portfolio/fills verification "
                "unavailable)",
                verify_attempts=0,
                verify_ok_reads=0,
            )
            return
        state.cancel_verify_started_mono_ns = self._clock.monotonic_ns()
        state.cancel_reported_reason = (
            None if cancellation_reason is None else str(cancellation_reason)
        )
        # EXACT KEY (2026-07-24 incident-C review): the quote payload exposes
        # ``creator_order_id`` after ANY execution (doc-verified: it equals
        # Fill.order_id and /portfolio/fills filters by it). Capturing it here
        # turns verification from structural guessing into an exact join —
        # a partially-executed "cancelled" quote's prints carry this id.
        expected = quote.get("creator_order_id") if isinstance(quote, dict) else None
        state.cancel_expected_order_id = str(expected) if expected else None
        self._metrics.inc("fill_recovery.cancel_verify_started")
        log.warning(
            "fill_recovery_cancel_report_verifying",
            quote_id=quote_id,
            cancellation_reason=cancellation_reason,
            expected_order_id=state.cancel_expected_order_id,
            attempts=self._config.fill_cancel_verify_attempts,
            delay_s=self._config.fill_cancel_verify_delay_s,
            detail="confirmed quote came back CANCELLED from REST — NOT "
            "discarding yet; the position stays in the risk book while "
            "/portfolio/fills is checked for a late/taker-style execution "
            "(both 2026-07-18 cancel reports were REAL executed fills; the "
            "2026-07-23 one was a PARTIAL execution)",
        )
        self._drop_quote(quote_id)

    def _discard_phantom_position(
        self,
        quote_id: str,
        state: OpenQuoteState,
        *,
        cancellation_reason: Any,
        detail: str,
        verify_attempts: int,
        verify_ok_reads: int,
    ) -> None:
        """Remove a verified-phantom position: release any straggler
        reservation (idempotent — a committed one is no longer outstanding),
        drop the phantom position from the exposure book (the settlement seam's
        own removal — bumps the position generation so stale snapshots
        invalidate), and un-park the state exactly like the decline paths do.
        The ONLY writer of ``fill_recovery_quote_cancelled`` — every discard
        carries its verification evidence. DEFENSIVE GUARD (2026-07-24
        review): a fill that was RECORDED (WS raced the verification) is real
        by definition — refuse the discard no matter what the caller
        concluded."""
        if state.fill_recorded:
            self._release_fill_claim(state)
            self._executed_states.pop(quote_id, None)
            log.warning(
                "fill_recovery_discard_refused_fill_recorded",
                quote_id=quote_id,
                detail="discard requested for a quote whose fills row exists "
                "— refused (a recorded fill is a real position)",
            )
            return
        self._metrics.inc("fill_recovery.cancelled")
        log.warning(
            "fill_recovery_quote_cancelled",
            quote_id=quote_id,
            cancellation_reason=cancellation_reason,
            verify_attempts=verify_attempts,
            verify_ok_reads=verify_ok_reads,
            detail=detail,
        )
        if self._reservation is not None:
            self._reservation.release(f"fill:{quote_id}")
        self._exposure.remove_position(f"fill:{quote_id}")
        # A phantom position may have accrued a settlement RECEIVABLE while it
        # sat in the book through game end (the fact sweep notes any held
        # position with graded legs) — cash that will never arrive, because
        # there was no fill. Cancel it with the position (2026-07-21 review
        # F6), else it shields the give-back halts for the full TTL.
        if self._balance is not None:
            self._balance.cancel_receivable(f"fill:{quote_id}")
        self._executed_states.pop(quote_id, None)
        state.pending_fill = None
        self._drop_quote(quote_id)

    # ------------------------------------ cancel-report /portfolio/fills verify

    async def _cancel_verification_step(
        self, quote_id: str, state: OpenQuoteState, now: int
    ) -> int:
        """One maintenance-tick step of verify-before-discard for a quote whose
        REST status said CANCELLED. Returns the number of REST polls spent (0
        or 1) so the sweep's per-tick budget covers these too.

        States: (a) execution already VERIFIED but the ledger write failed ⇒
        retry the normal-writer replay (no REST poll; bounded by the sweep's
        attempt budget, loud on exhaustion); (b) next /portfolio/fills poll due
        (attempt n is due at start + n·delay on the injectable clock) ⇒ poll
        and match; (c) not due yet ⇒ nothing. Resolution is in
        ``_resolve_cancel_verification``: verified-absent discards the phantom,
        all-reads-errored KEEPS the position (fail-safe — never uncount risk we
        could not disprove) and reports loudly."""
        assert state.cancel_verify_started_mono_ns is not None
        if state.cancel_verified_fill is not None:
            # (a) fill PROVEN real; only the ledger row is missing. Replay the
            # normal writer path until it lands (bounded + loud). The order_id
            # claim is held until the row exists (then the ledger guard owns it).
            if state.fill_recovery_attempts >= _FILL_RECOVERY_MAX_ATTEMPTS:
                return 0  # exhausted — already reported loudly
            state.fill_recovery_attempts += 1
            await self._replay_verified_fill(quote_id, state)
            if state.fill_recorded:
                self._release_fill_claim(state)
            else:
                self._note_fill_recovery_exhausted(quote_id, state)
            return 0
        delay_ns = self._cancel_verify_delay_ns()
        max_attempts = self._config.fill_cancel_verify_attempts
        due_ns = (
            state.cancel_verify_started_mono_ns
            + state.cancel_verify_attempts * delay_ns
        )
        if now < due_ns:
            return 0
        if state.cancel_verify_attempts >= max_attempts:
            # Budget spent without a match (only reachable if resolution was
            # interrupted): resolve now.
            self._resolve_cancel_verification(quote_id, state, now)
            return 0
        state.cancel_verify_attempts += 1
        final = state.cancel_verify_attempts >= max_attempts
        self._beat()  # a REST poll is progress, not a wedge
        self._metrics.inc("fill_recovery.verify_polls")
        # TIME-SCOPE the query (2026-07-18 review): min_ts = confirm wall-time
        # minus a small skew slack, so the match window is THIS quote's
        # verification window — never the ticker's historical tape (which holds
        # identical-count fills hours apart; adopting one double-counts).
        min_ts: int | None = None
        if state.fill_confirmed_wall_ts is not None:
            min_ts = max(0, state.fill_confirmed_wall_ts - _CANCEL_VERIFY_MIN_TS_SLACK_S)
        try:
            payload = await asyncio.wait_for(
                self._get_portfolio_fills(
                    state.rfq.market_ticker,
                    min_ts=min_ts,
                    order_id=state.cancel_expected_order_id,
                ),
                timeout=_MAINTENANCE_POLL_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — any poll error retries on cadence
            self._metrics.inc("fill_recovery.verify_errors")
            log.warning(
                "fill_recovery_verify_poll_failed",
                quote_id=quote_id,
                attempt=state.cancel_verify_attempts,
                error=repr(exc),
            )
            if final:
                self._resolve_cancel_verification(quote_id, state, now)
            return 1
        state.cancel_verify_ok_reads += 1
        match = await self._adopt_exchange_fill(quote_id, state, payload)
        if match is not None:
            state.cancel_verified_fill = dict(match)
            self._metrics.inc("fill_recovery.late_execution")
            log.warning(
                "fill_recovery_late_execution",
                quote_id=quote_id,
                combo_ticker=state.rfq.market_ticker,
                order_id=match.get("order_id"),
                aggregate=match.get("_aggregate"),
                is_taker=match.get("is_taker"),
                created_time=match.get("created_time"),
                attempts=state.cancel_verify_attempts,
                detail="quote status said CANCELLED but /portfolio/fills shows "
                "the execution (late/taker-style/partial) — position KEPT in "
                "the risk book; fills row now written via the normal "
                "on_quote_executed writer",
            )
            await self._replay_verified_fill(quote_id, state)
            if state.fill_recorded:
                self._release_fill_claim(state)
            return 1
        if final:
            self._resolve_cancel_verification(quote_id, state, now)
        return 1

    def _cancel_verify_delay_ns(self) -> int:
        """The verification poll spacing in monotonic ns. NaN/negative delay ⇒
        0 (attempts run back-to-back per tick — still bounded by the attempt
        budget; never a silent verification stall)."""
        delay_s = self._config.fill_cancel_verify_delay_s
        return int(delay_s * 1e9) if delay_s > 0.0 else 0

    def _resolve_cancel_verification(
        self, quote_id: str, state: OpenQuoteState, now: int
    ) -> None:
        """Verdict once a verification ROUND's poll budget is spent without a
        match. At least one successful read (any round) ⇒ the fill is GENUINELY
        absent: discard the phantom exactly as before (with the evidence).
        Every read errored ⇒ absence was never proven: KEEP the position and
        RETRY a whole round on the same cadence (2026-07-18 review — a
        transient 429 storm must not pin a phantom's budget until restart),
        bounded by ``_CANCEL_VERIFY_MAX_ROUNDS``; only then the loud ERROR
        give-up (position still kept — fail-safe; the next-restart exchange
        reconcile owns it from there).

        2026-07-24 review guards: a fill RECORDED mid-verification (the late
        WS message landing inside the final attempt's awaits) is terminal
        success — never a discard; and an AMBIGUOUS round (a plausible
        partial group refused because another in-flight quote shares the
        ticker and no exact order id exists) is not proof of absence — the
        position is KEPT with the loud unresolved give-up instead."""
        if state.fill_recorded:
            # The late WS quote_executed (or the replay) landed the row while
            # this resolution was pending: the fill is REAL — un-park, never
            # discard (the discard would remove a real, ledger-recorded
            # position and cancel its receivable).
            self._release_fill_claim(state)
            self._executed_states.pop(quote_id, None)
            log.info(
                "fill_recovery_verify_resolved_by_recorded_fill",
                quote_id=quote_id,
            )
            return
        if state.cancel_verify_ok_reads > 0 and state.cancel_verify_ambiguous:
            self._release_fill_claim(state)  # defensive — no adoption happened
            self._metrics.inc("fill_recovery.verify_ambiguous_kept")
            log.error(
                "fill_recovery_verify_ambiguous_kept",
                quote_id=quote_id,
                combo_ticker=state.rfq.market_ticker,
                verify_attempts=state.cancel_verify_attempts,
                verify_ok_reads=state.cancel_verify_ok_reads,
                detail="verification saw a structurally-plausible PARTIAL "
                "fill it could not attribute (no creator_order_id and "
                "another in-flight quote on the same ticker) — ambiguous "
                "evidence never discards: position KEPT (fail-safe, "
                "undercounting is the dangerous direction); the next-restart "
                "exchange reconcile owns it",
            )
            self._executed_states.pop(quote_id, None)
            return
        if state.cancel_verify_ok_reads > 0:
            self._release_fill_claim(state)  # defensive — no adoption happened
            self._discard_phantom_position(
                quote_id,
                state,
                cancellation_reason=state.cancel_reported_reason,
                detail="confirmed quote came back CANCELLED from REST and "
                "/portfolio/fills shows NO matching execution after bounded "
                "verification — phantom position removed, no fills row written",
                verify_attempts=state.cancel_verify_attempts,
                verify_ok_reads=state.cancel_verify_ok_reads,
            )
            return
        state.cancel_verify_rounds += 1
        if state.cancel_verify_rounds < _CANCEL_VERIFY_MAX_ROUNDS:
            # Reschedule a fresh round one delay from now (same cadence).
            state.cancel_verify_attempts = 0
            state.cancel_verify_started_mono_ns = now + self._cancel_verify_delay_ns()
            self._metrics.inc("fill_recovery.verify_round_failed")
            log.warning(
                "fill_recovery_verify_round_failed",
                quote_id=quote_id,
                combo_ticker=state.rfq.market_ticker,
                round=state.cancel_verify_rounds,
                max_rounds=_CANCEL_VERIFY_MAX_ROUNDS,
                detail="every /portfolio/fills read in this verification round "
                "failed — position stays booked; retrying a full round",
            )
            return
        self._release_fill_claim(state)  # defensive — no adoption happened
        self._metrics.inc("fill_recovery.verify_unresolved")
        log.error(
            "fill_recovery_verify_unresolved",
            quote_id=quote_id,
            combo_ticker=state.rfq.market_ticker,
            verify_rounds=state.cancel_verify_rounds,
            detail="cancel report could NOT be verified against "
            "/portfolio/fills (every read failed across all retry rounds) — "
            "position KEPT in the risk book (fail-safe), no fills row written; "
            "the next-restart exchange reconcile is the backstop",
        )
        self._executed_states.pop(quote_id, None)

    async def _get_portfolio_fills(
        self, ticker: str, *, min_ts: int | None = None, order_id: str | None = None
    ) -> JsonDict:
        """GET /portfolio/fills for one combo ticker (read-only), pinned to our
        subaccount when configured (P0-5 query-layer pin), time-scoped by
        ``min_ts`` (epoch seconds — index-scan §portfolio fills) when given,
        and — 2026-07-24 incident-C review — filtered by ``order_id`` when the
        quote's ``creator_order_id`` is known (the documented exact join:
        Quote.creator_order_id == Fill.order_id)."""
        assert self._fills_getter is not None
        params: dict[str, str | int] = {"ticker": ticker, "limit": 100}
        if min_ts is not None:
            params["min_ts"] = min_ts
        if order_id is not None:
            params["order_id"] = order_id
        if self._fills_subaccount is not None:
            params["subaccount"] = self._fills_subaccount
        return await self._fills_getter.get_fills(**params)

    async def _adopt_exchange_fill(
        self, quote_id: str, state: OpenQuoteState, payload: JsonDict
    ) -> JsonDict | None:
        """The first admissible exchange fill GROUP — all prints of ONE order
        (one Kalshi order can cross the public book at several levels, so one
        execution is one ``order_id`` across possibly-many /portfolio/fills
        rows; 2026-07-24 incident-C review) — that passes the ADOPTION GUARDS
        (2026-07-18 review), with its order_id CLAIMED. Returns an AGGREGATE
        fill dict (total count + summed fee + the raw prints as evidence) or
        None. Guards, each independently load-bearing against double-count:

        - ``order_id`` present (the dedupe key; the documented Fill schema
          always carries it — a row without one can never be deduped, so it is
          never adopted: fail-closed);
        - not already CLAIMED by another in-flight verification (two
          concurrently-verifying quotes must not both adopt ONE exchange fill);
        - not already IN THE LOCAL LEDGER (a historical fill of the same
          ticker/side/exact count — proven on today's live tape — belongs to
          an earlier quote; adopting it would keep a phantom AND double-book
          the fee).

        Rejected candidates are logged + counted per reason so a guard firing
        live is visible evidence, not silence."""
        groups, ambiguous = self._match_exchange_fill_groups(state, payload)
        if ambiguous:
            # Ambiguous partial evidence (see _match_exchange_fill_groups):
            # resolution must NOT conclude "genuinely absent" this round.
            state.cancel_verify_ambiguous = True
        for group in groups:
            order_id = group["order_id"]
            if not order_id:
                reason = "order_id_missing"
            elif order_id in self._claimed_exchange_order_ids:
                reason = "already_claimed"
            elif await self._store.has_fill_for_order_id(order_id):
                reason = "already_in_ledger"
            else:
                self._claimed_exchange_order_ids.add(order_id)
                return self._aggregate_fill(group)
            self._metrics.inc("fill_recovery.verify_match_rejected")
            self._metrics.inc(f"fill_recovery.verify_match_rejected.{reason}")
            log.warning(
                "fill_recovery_verify_match_rejected",
                quote_id=quote_id,
                combo_ticker=state.rfq.market_ticker,
                order_id=order_id,
                n_prints=len(group["rows"]),
                total_cc=group["total_cc"],
                reason=reason,
                detail="structurally-matching exchange fill group NOT adopted "
                "— adoption guard refused it (double-count protection)",
            )
        return None

    def _aggregate_fill(self, group: JsonDict) -> JsonDict:
        """One adoptable fill dict from a group of prints sharing one
        ``order_id``: the first print's fields ride through (created_time,
        is_taker, prices — evidence), with the group TOTAL count, the summed
        exchange fee (None if ANY print's fee is unreadable — an honest
        UNKNOWN, never a partial sum booked as complete), and every raw print
        attached. Downstream readers (``_resize_pending_to_exchange_count``,
        ``_exchange_fill_fee_cc``) prefer the ``_aggregate`` block."""
        rows: list[JsonDict] = group["rows"]
        fee_cc_total: int | None = 0
        for row in rows:
            fee = self._exchange_fill_fee_cc(row)
            if fee is None or fee_cc_total is None:
                fee_cc_total = None
                break
            fee_cc_total = fee_cc_total + fee
        return {
            **dict(rows[0]),
            "order_id": group["order_id"],
            "prints": [dict(r) for r in rows],
            "_aggregate": {
                "count_cc": int(group["total_cc"]),
                "fee_cc": fee_cc_total,
                "n_prints": len(rows),
            },
        }

    def _release_fill_claim(self, state: OpenQuoteState) -> None:
        """Release this state's claimed exchange order_id (once the ledger row
        exists the ledger's own order_id guard takes over; on discard/terminal
        paths the release is defensive — no adoption reaches them)."""
        fill = state.cancel_verified_fill
        if not fill:
            return
        order_id = fill.get("order_id")
        if order_id:
            self._claimed_exchange_order_ids.discard(str(order_id))

    def _match_exchange_fill_groups(
        self, state: OpenQuoteState, payload: JsonDict
    ) -> tuple[list[JsonDict], bool]:
        """(admissible groups, ambiguous_partial_refused) for this quote's
        pending fill. A GROUP is every structurally-matching print sharing one
        ``order_id`` (one order can cross the public book at several levels),
        with its TOTAL count — adoption guards are applied per group by
        ``_adopt_exchange_fill``.

        INCIDENT C (2026-07-23, quote 7824bf04): a target-cost RFQ's
        "execution failed" cancel can mean PARTIALLY EXECUTED — the exchange
        filled 38.15 of the 57.44 we offered, and the old EXACT-count rule
        silently rejected the real fill on all three verification reads, so
        the "phantom" removal deleted a REAL $28.69 position.

        Matching rules (2026-07-24 adversarial review hardening):
        - EXACT KEY FIRST: when ``state.cancel_expected_order_id`` (the quote
          payload's ``creator_order_id`` — doc-verified == Fill.order_id) is
          known, ONLY that order's group is admissible; every other same-
          ticker row is a logged skip. Structural matching is the FALLBACK
          for payloads that omit the id, never the primary.
        - Structural screen per print: same combo ticker (queried, but
          re-checked) + same OUR side (``outcome_side``, legacy ``side``
          fallback) + readable positive count. Price is deliberately NOT
          matched (incident A executed at 0.7660 against our 0.7670 bid).
        - Group admissible when 0 < total <= pending; a total LARGER than
          pending is never ours (Kalshi cannot fill beyond the offered size).
        - AMBIGUITY GUARD (review finding: the <=-count rule let quote B
          adopt quote A's unledgered same-ticker fill, shrinking a REAL
          position on foreign evidence): with NO expected order id, a
          PARTIAL group (total < pending) is REFUSED while another
          not-yet-recorded quote state exists on the same ticker — and the
          refusal poisons "genuinely absent" (the caller keeps the position
          rather than discarding on ambiguous evidence). Exact-total groups
          keep the pre-incident-C behaviour (ledger/claim guards own them).
        - Ordering: exact-total groups first, then largest total.
        - Every same-ticker row/group the matcher refuses is LOGGED
          (incident C's rejections were invisible; a real fill must never be
          silently skipped again). Unreadable fields skip fail-closed — a
          fill is never matched from a guess."""
        if state.pending_fill is None:
            return [], False
        accepted_side, _bid, qty = state.pending_fill
        our_side = self._conventions.maker_position_side(accepted_side)
        rows = payload.get("fills") or []
        if not isinstance(rows, list):
            return [], False
        expected = state.cancel_expected_order_id
        by_order: dict[str, list[tuple[int, JsonDict]]] = {}
        keyless: list[tuple[int, JsonDict]] = []
        skips: list[JsonDict] = []

        def skip(row: JsonDict, side_raw: str, count_raw: object, why: str) -> None:
            skips.append(
                {
                    "order_id": row.get("order_id"),
                    "outcome_side": side_raw,
                    "count_fp": count_raw,
                    "created_time": row.get("created_time"),
                    "reason": why,
                }
            )

        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or row.get("market_ticker") or "")
            if ticker != state.rfq.market_ticker:
                continue
            side_raw = str(row.get("outcome_side") or row.get("side") or "").lower()
            count_raw = row.get("count_fp") or row.get("count")
            count: int | None
            try:
                count = (
                    int(qty_from_fp_str(str(count_raw)))
                    if count_raw is not None
                    else None
                )
            except ValueError:
                count = None
            if side_raw != our_side.value or count is None or count <= 0:
                skip(row, side_raw, count_raw, "side_or_count_unreadable")
                continue
            order_id_raw = row.get("order_id")
            order_id = str(order_id_raw) if order_id_raw else None
            if expected is not None and order_id != expected:
                skip(row, side_raw, count_raw, "order_id_mismatch")
                continue
            if order_id is None:
                keyless.append((count, row))
            else:
                by_order.setdefault(order_id, []).append((count, row))

        others_in_flight = any(
            s is not state and not s.fill_recorded
            and s.rfq.market_ticker == state.rfq.market_ticker
            for s in self._executed_states.values()
        )
        groups: list[JsonDict] = []
        ambiguous = False
        candidates: list[tuple[str | None, list[tuple[int, JsonDict]]]] = [
            *by_order.items(),
            *[(None, [pair]) for pair in keyless],
        ]
        for order_id, pairs in candidates:
            total = sum(c for c, _r in pairs)
            if total > int(qty):
                skip(pairs[0][1], our_side.value, total, "group_exceeds_pending")
                continue
            if (
                expected is None
                and total < int(qty)
                and others_in_flight
            ):
                ambiguous = True
                skip(pairs[0][1], our_side.value, total, "ambiguous_partial_refused")
                continue
            groups.append(
                {
                    "order_id": order_id,
                    "rows": [r for _c, r in pairs],
                    "total_cc": total,
                }
            )
        if skips:
            self._metrics.inc("fill_recovery.verify_structural_skips")
            log.warning(
                "fill_recovery_verify_structural_skips",
                combo_ticker=state.rfq.market_ticker,
                pending_side=our_side.value,
                pending_count_cc=int(qty),
                expected_order_id=expected,
                n_skipped=len(skips),
                skipped=skips[:5],
            )
        groups.sort(key=lambda g: (g["total_cc"] != int(qty), -g["total_cc"]))
        return groups, ambiguous

    def _resize_pending_to_exchange_count(
        self, quote_id: str, state: OpenQuoteState, fill: JsonDict
    ) -> None:
        """INCIDENT C (partial execution): the adopted exchange fill's count is
        the TRUTH of what executed — when it is smaller than the pending
        (offered/accepted) quantity, shrink the pending fill AND the booked
        position to it BEFORE the replay writes the ledger row, so the ledger
        row, the EV edge, and the risk book all carry the exchange's count
        (the old path booked the offered 57.44 while the exchange held 38.15
        — the position-reconcile count-divergence alarm class). Idempotent
        (equal counts no-op, so the bounded replay retries re-enter safely);
        an unreadable count keeps the LARGER booked size (fail-safe: risk is
        never shrunk on a guess); growth is impossible (the matcher only
        admits count <= pending). The remove+rebook pair goes through the
        exposure book's own removal (position-generation bump — stale MC
        snapshots invalidate) and the single ``_fill_position`` builder."""
        if state.pending_fill is None:
            return
        exchange_qty: int | None = None
        agg = fill.get("_aggregate")
        if isinstance(agg, dict):
            agg_count = agg.get("count_cc")
            if isinstance(agg_count, int) and not isinstance(agg_count, bool):
                exchange_qty = agg_count
        if exchange_qty is None:
            count_raw = fill.get("count_fp") or fill.get("count")
            if count_raw is None:
                return
            try:
                exchange_qty = int(qty_from_fp_str(str(count_raw)))
            except ValueError:
                return
        accepted_side, bid, qty = state.pending_fill
        if exchange_qty <= 0 or exchange_qty >= int(qty):
            return
        self._metrics.inc("fill_recovery.partial_execution_resize")
        log.warning(
            "fill_recovery_partial_execution_resize",
            quote_id=quote_id,
            combo_ticker=state.rfq.market_ticker,
            pending_count_cc=int(qty),
            exchange_count_cc=exchange_qty,
            detail="cancel-verified exchange fill executed PARTIALLY — "
            "pending fill and booked position resized to the exchange count "
            "(ledger row, EV edge and risk book now carry the executed size)",
        )
        state.pending_fill = (accepted_side, bid, CentiContracts(exchange_qty))
        position_id = f"fill:{quote_id}"
        if position_id in self._exposure.positions:
            self._exposure.remove_position(position_id)
            self._book_position(quote_id, state)

    async def _replay_verified_fill(self, quote_id: str, state: OpenQuoteState) -> None:
        """Book the VERIFIED late execution through the NORMAL writer path —
        the same ``on_quote_executed`` every ordinary fill takes (never a
        hand-built ledger row). The position was never removed (verification
        keeps it booked) — though a PARTIAL execution first shrinks it to the
        exchange count (incident C) — so the booking side is an idempotent
        no-op; the ledger tail writes the row, books the exchange-reported
        fee into realized P&L, and tracks markouts exactly once (fill_ref
        guard)."""
        fill = state.cancel_verified_fill
        assert fill is not None
        self._resize_pending_to_exchange_count(quote_id, state, fill)
        msg: JsonDict = {
            "quote_id": quote_id,
            "recovered_via_fills_poll": True,
            "exchange_fill": dict(fill),
        }
        order_id = fill.get("order_id")
        if order_id:
            msg["order_id"] = str(order_id)
        fee_cc = self._exchange_fill_fee_cc(fill)
        if fee_cc is not None:
            msg["exchange_fee_cc"] = fee_cc
        await self.on_quote_executed(msg)

    @staticmethod
    def _exchange_fill_fee_cc(fill: JsonDict) -> int | None:
        """The exchange fill's ``fee_cost`` (fixed-point dollars) in cc,
        rounded UP to a whole cc (fee_cc_from_dollars_str — never understate a
        cost we paid). An AGGREGATE fill (multi-print adoption, 2026-07-24)
        carries its per-print-summed fee in ``_aggregate.fee_cc`` — preferred;
        None there means some print's fee was unreadable (honest UNKNOWN,
        never a partial sum). None otherwise when absent/unreadable — the
        ledger then records the fee model's figure rather than a guessed
        number (defense #2)."""
        agg = fill.get("_aggregate")
        if isinstance(agg, dict):
            fee = agg.get("fee_cc")
            if isinstance(fee, int) and not isinstance(fee, bool):
                return fee
            return None
        raw = fill.get("fee_cost")
        if raw is None:
            return None
        try:
            return int(fee_cc_from_dollars_str(str(raw)))
        except MoneyParseError:
            return None

    # ---------------------------------- off-loop alarm-only diagnostic sweeps

    def _launch_diagnostic_sweeps(self) -> None:
        """Launch every ALARM-ONLY sweep as a single-flight background task.

        SYNC and non-blocking by construction — the maintenance tick calls this
        and moves straight on to the enforced limit check. Each sweep keeps its
        OWN cadence guard internally (so this is cheap on the ticks where
        nothing is due) and runs under its own derived wall bound.

        A sweep that is still running when the next launch comes due is skipped
        with a counter, never stacked: piling up tasks against a saturated store
        is how a diagnostic becomes an outage. Nothing here can raise into the
        tick; the wrapper swallows every outcome into a log + metric.

        FIX ISOLATION: no pricing, risk, or quoting state is read or written
        here, and no caller awaits the result."""
        self._launch_diagnostic_sweep(
            "ledger_divergence",
            self._sweep_ledger_divergence,
            _LEDGER_DIVERGENCE_SWEEP_TIMEOUT_S,
        )
        self._launch_diagnostic_sweep(
            "fills_ledger_sweep",
            self._sweep_fills_ledger_diff,
            _FILLS_LEDGER_SWEEP_TIMEOUT_S,
        )

    def _launch_diagnostic_sweep(
        self,
        name: str,
        sweep: Callable[[], Awaitable[None]],
        timeout_s: float,
    ) -> None:
        running = self._diag_tasks.get(name)
        if running is not None and not running.done():
            # Still going from a previous launch. The sweep's own cadence guard
            # already stamped, so this is not a missed interval — it is a store
            # (or exchange) slower than the sweep's whole wall bound, which is
            # exactly what the 2026-07-26 incident looked like from inside.
            self._metrics.inc(f"{name}.skipped_in_flight")
            return

        async def _run() -> None:
            try:
                await asyncio.wait_for(sweep(), timeout_s)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # THE incident, now bounded: a store read that does not return.
                # Loud, counted, retried on the next cadence — never a stall.
                self._metrics.inc(f"{name}.timeout")
                log.warning(
                    f"{name}_sweep_timeout",
                    timeout_s=timeout_s,
                    detail="alarm-only sweep exceeded its wall bound (a "
                    "saturated store or exchange) — skipped and retried on "
                    "the next interval; the maintenance loop was never held",
                )
            except Exception as exc:  # noqa: BLE001 — alarm-only, never fatal
                self._metrics.inc(f"{name}.errors")
                log.warning(f"{name}_sweep_failed", error=repr(exc))

        self._diag_tasks[name] = asyncio.ensure_future(_run())

    async def drain_diagnostic_sweeps(self) -> None:
        """Wait for every in-flight alarm-only sweep to finish (or time out).

        SHUTDOWN uses this: a sweep still holding a store cursor when the store
        closes logs a spurious ``Connection closed`` error and, worse, an
        orphaned task means the process exits with the divergence check half
        done and no record of it. Each sweep is already wall-bounded, so this
        can never hold shutdown open longer than the slowest bound.

        Never raises: the wrapper inside ``_launch_diagnostic_sweep`` has
        already swallowed every outcome into a log + metric."""
        tasks = [t for t in self._diag_tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -------------------------------- position-ledger divergence invariant

    async def _sweep_ledger_divergence(self) -> None:
        """POSITION-LEDGER DIVERGENCE INVARIANT (2026-07-26).

        Count OPEN exposure positions vs OPEN ``position_ledger`` rows and log
        the divergence. This exists because the "settled rows can never land"
        defect was SILENT: ``record_settled`` keyed on the volatile in-memory
        ``position_id``, the rehydrator re-mints ids on every restart, so no
        settled write could match an open row written before the restart — and
        nothing ever said so. The ledger had drifted to 6 rows / $27.35 of a
        $731.04 book (3.74%) before a human noticed.

        Matching is by the DURABLE key ``(leg_set_hash, combo_ticker,
        our_side)`` — the same identity the settled write now resolves on — so
        this measures exactly the property that must hold: every open position
        we carry has an open ledger row its settlement can land on.

        ALARM-ONLY and off the pricing path: one small indexed SELECT per
        ``ledger_divergence_sweep_interval_s``, never a risk input, never a
        writer (the boot upsert in ``_rehydrate_exposure_book`` owns backfill).
        Non-positive interval ⇒ disabled. A leg-less/synthetic reserved
        position (an exchange holding adopted with no local record) has no
        durable identity and is counted separately as ``no_identity`` rather
        than being scored as a false divergence."""
        interval_s = self._config.ledger_divergence_sweep_interval_s
        if not (interval_s > 0.0):
            return
        now = self._clock.monotonic_ns()
        last = self._ledger_divergence_last_mono_ns
        if last is not None and now - last < int(interval_s * 1e9):
            return
        self._ledger_divergence_last_mono_ns = now
        open_rows = await self._store.open_ledger_identities()
        # Multiset: two identical open positions on one combo need TWO rows.
        available: Counter[tuple[str, str, str]] = Counter(open_rows)
        positions = list(self._exposure.positions.values())
        no_identity = 0
        missing: list[str] = []
        for pos in positions:
            key = stable_ledger_key(pos)
            if key is None:
                no_identity += 1
                continue
            ident = (key, pos.combo_ticker, pos.our_side.value)
            if available[ident] > 0:
                available[ident] -= 1
            else:
                missing.append(pos.position_id)
        self._metrics.inc("ledger_divergence.checks")
        if missing:
            self._metrics.inc("ledger_divergence.missing_rows", by=len(missing))
        if no_identity:
            self._metrics.inc("ledger_divergence.no_identity", by=no_identity)
        orphan_rows = sum(available.values())
        if orphan_rows:
            self._metrics.inc("ledger_divergence.orphan_rows", by=orphan_rows)
        fields = {
            "open_positions": len(positions),
            "open_ledger_rows": len(open_rows),
            "positions_without_row": len(missing),
            "rows_without_position": orphan_rows,
            "positions_without_identity": no_identity,
        }
        if missing or orphan_rows or no_identity:
            log.warning(
                "position_ledger_divergence",
                **fields,
                position_ids=sorted(missing)[:20],
                detail="open exposure positions and open position_ledger rows "
                "disagree — a position with no open row cannot record its "
                "settlement (p_night's realized anchor + settlement "
                "calibration under-count); alarm-only, no risk effect",
            )
        else:
            log.info("position_ledger_divergence_clean", **fields)

    # ------------------------------------ fills-ledger diff sweep (incident C)

    async def _sweep_fills_ledger_diff(self) -> None:
        """FILLS-LEDGER SWEEP (2026-07-24 incident C, backstop 3). Periodic
        account-wide diff of /portfolio/fills against the local fills ledger,
        keyed by ``order_id`` — the generic net under EVERY writer-path miss
        (incident C's partial execution was invisible to the per-quote
        verification for its whole bounded budget and to the ledger forever).

        ALARM-ONLY by design: the fills ledger has ONE writer
        (``on_quote_executed``) and this sweep never becomes a second one — a
        miss is a loud ERROR + metric; adoption happens only through the
        per-quote verification machinery (rows claimed by an in-flight
        verification, or on a ticker a still-recovering quote owns, are
        skipped). The min_ts watermark never advances past the OLDEST
        unresolved miss, so a persistent miss re-alarms every interval until
        resolved (the restart reconcile owns anything older than the
        lookback).

        Guards: no fills getter (paper/backtests) or a non-positive interval
        ⇒ disabled. The first tick only stamps the cadence — the first fetch
        runs one interval later. Bounded: ≤3 pages per sweep; any fetch error
        logs + retries next interval (slow-loop isolation — never raises into
        the maintenance tick, never touches the pricing path)."""
        if self._fills_getter is None:
            return
        interval_s = self._config.fills_ledger_sweep_interval_s
        if not (interval_s > 0.0):
            return
        now = self._clock.monotonic_ns()
        if self._fills_sweep_last_mono_ns is None:
            self._fills_sweep_last_mono_ns = now
            return
        if now - self._fills_sweep_last_mono_ns < int(interval_s * 1e9):
            return
        self._fills_sweep_last_mono_ns = now
        try:
            await self._fills_ledger_diff_once()
        except Exception as exc:  # noqa: BLE001 — slow loop (2026-07-24 review):
            # NOTHING here may raise into the maintenance tick — a store error
            # mid-diff must not abort the enforced limit check / TTL / reprice
            # behind this call. Log + retry next interval.
            self._metrics.inc("fills_ledger_sweep.errors")
            log.warning("fills_ledger_sweep_failed", error=repr(exc))

    async def _fills_ledger_diff_once(self) -> None:
        """One fills-ledger diff pass (called only under the cadence guard +
        try/except of ``_sweep_fills_ledger_diff``). Review-hardened:
        2.5s-per-page timeout (house REST bound), limit 1000, batched ledger
        reads (two SELECTs, never per-row point reads), truncation clamp,
        naive/legacy timestamp parsing, non-exhausted in-flight exclusion
        only, and an age-out release so an unresolvable miss cannot pin the
        watermark forever."""
        assert self._fills_getter is not None
        min_ts = self._fills_sweep_min_ts
        now_wall = int(self._clock.now().timestamp())
        lookback_s = int(self._config.fills_ledger_sweep_lookback_s)
        if min_ts is None:
            min_ts = now_wall - lookback_s
        # In-flight exclusion ONLY for states still inside their recovery/
        # verification budget (2026-07-24 review: an EXHAUSTED state parked
        # forever must not suppress sweep alarms on its ticker — its missing
        # fill is exactly what the sweep exists to keep loud).
        in_flight_tickers = {
            s.rfq.market_ticker
            for s in self._executed_states.values()
            if not s.fill_recorded
            and s.fill_recovery_attempts < _FILL_RECOVERY_MAX_ATTEMPTS
        }
        ledger_ids = await self._store.fill_order_ids()
        null_keys = await self._store.fill_null_order_id_keys()
        rows: list[JsonDict] = []
        cursor = ""
        truncated = False
        for _ in range(_FILLS_SWEEP_MAX_PAGES):
            params: dict[str, str | int] = {"limit": 1000, "min_ts": min_ts}
            if self._fills_subaccount is not None:
                params["subaccount"] = self._fills_subaccount
            if cursor:
                params["cursor"] = cursor
            self._beat()
            payload = await asyncio.wait_for(
                self._fills_getter.get_fills(**params),
                timeout=_MAINTENANCE_POLL_TIMEOUT_S,
            )
            page = payload.get("fills") or []
            if isinstance(page, list):
                rows.extend(r for r in page if isinstance(r, dict))
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        else:
            truncated = bool(cursor)
        self._metrics.inc("fills_ledger_sweep.ran")
        max_seen_ts: int | None = None
        oldest_scanned_ts: int | None = None
        missing_ts: list[int] = []
        missing_unparseable_ts = False
        missing = 0
        for row in rows:
            created_ts = _parse_epoch_s(row.get("created_time")) or _parse_epoch_s(
                row.get("ts")
            )
            if created_ts is not None:
                max_seen_ts = (
                    created_ts if max_seen_ts is None else max(max_seen_ts, created_ts)
                )
                oldest_scanned_ts = (
                    created_ts
                    if oldest_scanned_ts is None
                    else min(oldest_scanned_ts, created_ts)
                )
            order_id_raw = row.get("order_id")
            if not order_id_raw:
                # Undedupable (the documented Fill schema always carries an
                # order_id) — alarmed, never guessed into the ledger; holds
                # the watermark like any other unattributable row.
                self._metrics.inc("fills_ledger_sweep.no_order_id")
                log.error("fills_ledger_sweep_row_without_order_id", row=row)
                missing_unparseable_ts = missing_unparseable_ts or created_ts is None
                continue
            order_id = str(order_id_raw)
            if order_id in ledger_ids:
                continue
            if order_id in self._claimed_exchange_order_ids:
                # An in-flight adoption/recovery owns it — VISIBLE (2026-07-24
                # review: a replay-exhausted claim must not be silent), and it
                # self-resolves when the replay lands the row.
                self._metrics.inc("fills_ledger_sweep.claimed_unwritten")
                log.warning(
                    "fills_ledger_sweep_claimed_unwritten",
                    order_id=order_id,
                    detail="tape fill claimed by an in-flight adoption whose "
                    "ledger row has not landed yet — watching, not alarming",
                )
                continue
            ticker = str(row.get("ticker") or row.get("market_ticker") or "")
            count_cc: int | None
            try:
                raw_count = row.get("count_fp") or row.get("count")
                count_cc = (
                    int(qty_from_fp_str(str(raw_count)))
                    if raw_count is not None
                    else None
                )
            except ValueError:
                count_cc = None
            if count_cc is not None and (ticker, count_cc) in null_keys:
                # A ledger row EXISTS for this ticker+count but carries no
                # order_id (poll-recovered without creator_order_id) — a
                # visible skip, never a permanent false alarm.
                self._metrics.inc("fills_ledger_sweep.matched_null_order_id")
                continue
            if ticker in in_flight_tickers:
                continue  # per-quote recovery/verification still owns it
            missing += 1
            if created_ts is not None:
                missing_ts.append(created_ts)
            else:
                missing_unparseable_ts = True
            self._metrics.inc("fills_ledger_sweep.missing")
            log.error(
                "fills_ledger_missing_exchange_fill",
                order_id=order_id,
                ticker=ticker,
                count_fp=row.get("count_fp"),
                outcome_side=row.get("outcome_side") or row.get("side"),
                created_time=row.get("created_time"),
                is_taker=row.get("is_taker"),
                detail="exchange reports a fill our fills ledger does not "
                "hold and no in-flight recovery owns — a writer-path miss "
                "(incident-C class); NOT auto-written (one-writer rule): the "
                "operator/restart reconcile owns adoption; re-alarms every "
                "sweep until resolved",
            )
        if truncated:
            # >3 pages in the window: unscanned rows exist — LOUD, and the
            # watermark must not advance past the oldest row actually seen.
            self._metrics.inc("fills_ledger_sweep.truncated")
            log.error(
                "fills_ledger_sweep_truncated",
                scanned=len(rows),
                detail="fills window exceeded the page budget — older rows "
                "unscanned this sweep; watermark clamped to the oldest "
                "scanned row so they are re-fetched next interval",
            )
        if missing_unparseable_ts:
            # A miss whose timestamp cannot be read cannot be held-back by
            # timestamp — refuse to advance at all (it would silently drop
            # the row from every later window).
            log.error(
                "fills_ledger_sweep_watermark_held",
                detail="a missing/unattributable row had no parseable "
                "timestamp — watermark NOT advanced this sweep",
            )
        elif max_seen_ts is not None:
            next_ts = max_seen_ts - 300
            if truncated and oldest_scanned_ts is not None:
                next_ts = min(next_ts, oldest_scanned_ts - 1)
            # Hold the watermark at the oldest UNRESOLVED miss so it re-alarms
            # every sweep — but AGE OUT misses older than the lookback (an
            # unresolvable miss must not pin the window forever; it gets a
            # distinct terminal alarm and belongs to the restart reconcile).
            aged = [t for t in missing_ts if t < now_wall - lookback_s]
            held = [t for t in missing_ts if t >= now_wall - lookback_s]
            if aged:
                self._metrics.inc("fills_ledger_sweep.miss_aged_out")
                log.error(
                    "fills_ledger_sweep_miss_aged_out",
                    n=len(aged),
                    oldest=min(aged),
                    detail="unresolved ledger misses older than the lookback "
                    "released from the watermark hold — final sweep alarm; "
                    "the operator/restart reconcile owns them now",
                )
            if held:
                next_ts = min(next_ts, min(held) - 1)
            self._fills_sweep_min_ts = max(min_ts, next_ts)
        if missing:
            log.error(
                "fills_ledger_sweep_summary", missing=missing, scanned=len(rows)
            )

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
            # BOUNDED (2026-07-27). This runs INLINE on the intake worker that
            # carries our RFQ flow — an unbounded wait for write tokens here
            # stalls the socket's consumer, and an end-of-game wave fires this
            # handler for the whole book at once. Deferral is safe and is not
            # abandonment: the quote stays withdraw-pending in the mirror and
            # the 0.5s resolver re-drives it (the trigger never repeating is
            # exactly why the resolver exists). Stated explicitly rather than
            # inherited so the bound is visible at the risk-bearing site.
            await self._delete_quote(
                quote_id,
                ReasonCode.DELETE_RFQ_GONE,
                budget_s=_WITHDRAW_RESOLVE_BUDGET_S,
            )

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

    def _roll_realized_day(self) -> None:
        """DAY ROLLOVER for the realized accumulator (2026-07-25, found while
        answering "will it just keep running tomorrow?").

        ``_realized_pnl_cc`` is the REALIZED half of ``DailyPnl``, which the
        DAILY-LOSS halt and the give-back/KILL ladder bind on. It was only
        ever zeroed in the constructor, so a process that lives across
        midnight carried the PRIOR day's realized P&L into the new day's
        halt baseline — a profitable day silently LOOSENS the next day's
        daily-loss halt by exactly that profit (and a losing day tightens
        it). The slate cap already rolls on the US/Eastern calendar date
        (``slate_key_for_start``); this rolls the realized counter on the
        SAME boundary so both agree. Idempotent: the date is captured on
        first call, and a change zeroes the counter exactly once.
        Unrealized needs no reset — it is recomputed from live marks."""
        today = self._clock.now().astimezone(_SLATE_TZ).date().isoformat()
        if self._realized_day is None:
            self._realized_day = today
            return
        if self._realized_day != today:
            log.info(
                "realized_pnl_day_rolled",
                prior_day=self._realized_day,
                prior_realized_cc=self._realized_pnl_cc,
                new_day=today,
                detail="daily-loss/give-back baseline reset on the ET day "
                "boundary (the slate cap's own boundary)",
            )
            self._realized_pnl_cc = 0
            self._realized_day = today

    def _refresh_daily_pnl(self) -> None:
        """Mark open positions at current leg mids so the daily-loss limit
        actually binds. Any unmarkable position keeps the previous mark
        (limits also see UNKNOWN marginals as a breach on their own)."""
        self._roll_realized_day()
        unrealized = 0
        for position in self._exposure.positions.values():
            if not position.risk_modeled:
                # CONSERVATIVELY-RESERVED holding (P0-4 / adoption 2026-07-21):
                # its legs have no readable marginals BY CONSTRUCTION, so it
                # can never be marked — SKIP it (its premium is already fully
                # at risk in the deterministic caps). Treating it as a
                # temporarily-unmarkable position would freeze the whole
                # book's unrealized mark for the reserve's lifetime and
                # silently disarm the daily-loss cap's unrealized half
                # (2026-07-21 review, CRITICAL finding 1).
                continue
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

    def _maybe_solve_deploy_scale(self) -> None:
        """Refresh the SOLVED deployment scale off the maintenance tick.

        OFF THE QUOTE HOT PATH, always. The solve is ``O(iterations)`` full-book
        MCs; it runs here on the same throttle the book-risk snapshot uses and
        NEVER inside ``check``. On the live async loop it is launched as a
        BACKGROUND TASK (single-flight) so the maintenance tick returns
        immediately and keeps beating the supervisor heartbeat, exactly like
        ``_maybe_recompute_book_risk``; without an event loop (paper/tests) it
        runs inline.

        Disarmed ⇒ returns immediately, never even reads the clock, so the
        throttle state and the tick's timing are byte-identical to before this
        existed."""
        if not self._config.deploy_scale_enabled:
            return
        now = self._clock.monotonic_ns()
        interval_ns = int(self._config.deploy_scale_refresh_s * 1e9)
        last = self._deploy_scale_refresh_mono_ns
        if last is not None and now - last < interval_ns:
            return
        if self._deploy_scale_task is not None and not self._deploy_scale_task.done():
            return
        self._deploy_scale_refresh_mono_ns = now
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.solve_deploy_scale()
            return
        self._deploy_scale_task = asyncio.ensure_future(
            self.solve_deploy_scale_offloop()
        )

    def _maybe_recompute_peak_profile(self) -> None:
        """Refresh the PEAK-CONCENTRATION profile (sim/peak_profile.py) off the
        maintenance tick — ONLY when the committed position set changed
        (position generation), mirroring the book-risk snapshot's
        read-generation-stamp-publish discipline in miniature.

        The build enumerates the COMMITTED book only (no resting quotes, no
        reservations) so it is ms-scale and runs inline on the tick; position
        generation moves only on fills/settlements, so this is a rare event,
        not a per-tick cost — and NEVER a per-quote cost (the hot path only
        READS the cached profile). A build error logs once, marks the
        generation failed (retry only after the NEXT position change), and
        leaves the profile ABSENT — the skew's neutral zero-adder branch
        (pricing fail-safe, never a decline)."""
        if self._skew_params is None or not self._skew_params.peak_enabled:
            return
        if self._structural_cfg is None:
            return  # no structural machinery wired -> no profile -> neutral
        gen = self._exposure.position_generation
        if self._peak_profile is not None and self._peak_profile.input_generation == gen:
            return  # current — nothing changed
        if self._peak_profile_failed_generation == gen:
            return  # already failed on this exact book — wait for the next change
        try:
            positions = list(self._exposure.positions.values())
            tickers: set[str] = set()
            for position in positions:
                tickers.update(leg.market_ticker for leg in position.legs)
            marginals: dict[str, float] = {}
            for ticker in sorted(tickers):
                p = self._marginals(ticker)
                if p is not None:
                    marginals[ticker] = float(p)
            profile = build_peak_profile(
                positions,
                marginals,
                None,
                self._structural_cfg,
                k=self._config.peak_topk_states,
                # MULTI-CLUSTER steer (2026-07-19): Fraction("0.30") is exact
                # (3/10) — the YAML decimal-string convention, validated in
                # SkewConfig; a bad string raises here and rides the fail-safe
                # neutral branch below.
                n_clusters=self._config.peak_n_clusters,
                cluster_min_frac=Fraction(self._config.peak_cluster_min_frac),
                input_generation=gen,
            )
        except Exception:
            log.exception("peak_profile_recompute_failed", generation=gen)
            self._peak_profile_failed_generation = gen
            self._peak_profile = None  # fail-safe: neutral, never stale-wrong
            return
        self._peak_profile = profile
        self._peak_profile_failed_generation = None
        log.info(
            "peak_profile_snapshot",
            generation=gen,
            n_positions=len(positions),
            games={
                game: gp.top_loss_cc for game, gp in sorted(profile.by_game.items())
            },
            k=self._config.peak_topk_states,
            n_clusters=self._config.peak_n_clusters,
            # Per-game cached lower-cluster loss levels — the live observable
            # for the 2026-07-19 multi-cluster steer (empty at n_clusters=1).
            clusters={
                game: [c.loss_cc for c in gp.lower_clusters]
                for game, gp in sorted(profile.by_game.items())
                if gp.lower_clusters
            },
        )

    def _peak_profile_for_quote(self) -> PeakProfile | None:
        """The cached peak profile IF it still describes the current committed
        book (generation match — the same read-time re-check
        ``_book_risk_for_check`` performs); else None => the skew's peak
        component is neutral (zero adder) until the next off-hot-path rebuild.
        Cheap: one generation read."""
        profile = self._peak_profile
        if profile is None:
            return None
        if profile.input_generation != self._exposure.position_generation:
            return None
        return profile

    def _register_settled_candidates(self) -> None:
        """BATCH-register EVERY committed leg whose feed book is dark and whose
        graded fact is unresolved — never gated on being reached by a serial
        marginal walk (relight2 stall, 2026-07-19: registration rode the
        provider walks, only the first walk-order legs ever entered the fetch
        queue, and graded FRAENG facts sat unfetched on the exchange for 25
        minutes while the walk sat stuck behind blockers). Runs on EVERY
        maintenance tick — this covers startup (first tick), every
        position-generation change, and continuously self-heals any discovery
        gap. Cost: O(distinct committed legs) dict/set work, no network
        (``note_missing`` is a no-op for pending/resolved/unresolvable
        tickers)."""
        if self._settled is None:
            return
        for ticker in self._committed_leg_tickers():
            if self._settled.resolved(ticker) is not None:
                continue
            # THE SAME feed-readability predicate the provider uses
            # (relight3 fix): register iff the feed cannot PRICE the leg —
            # which includes a settled market's lingering VALID-but-EMPTY
            # husk book, not just an absent/invalid one. Single source of
            # truth (``_feed_marginal``) so registrar and provider can never
            # diverge again.
            if self._feed_marginal(ticker) is not None:
                continue  # the feed serves a readable price — nothing to resolve
            self._settled.note_missing(ticker)

    def _refresh_settlement_receivables(self) -> None:
        """Note a settlement RECEIVABLE for every held position whose outcome is
        KNOWN from exchange-graded leg facts (the settled-marginal resolver's
        permanent cache) — the give-back cascade shield (2026-07-19 false-
        positive kill: settled value leaves ``portfolio_value`` before the cash
        lands in ``balance``, so the equity trough reads as a give-back).

        Facts ONLY — never a live/feed marginal: a receivable shields a halt,
        so doubt must produce NO receivable (fail-closed toward halting). A leg
        without a graded fact ⇒ no receivable for that position this tick (the
        sweep self-heals next tick once the fact lands). A position whose
        predicted credit is zero (a loser) never notes one, so a genuine loss
        cascade is never shielded. Runs every maintenance tick —
        O(positions × legs) dict lookups, no I/O."""
        if self._balance is None or self._settled is None:
            return
        for position in self._exposure.positions.values():
            v_combo = 1.0
            unresolved = False
            for leg in position.legs:
                fact = self._settled.resolved(leg.market_ticker)
                if fact is None:
                    unresolved = True
                    break
                v_combo *= fact if leg.side == "yes" else 1.0 - fact
            if unresolved:
                continue
            credit_cc = self._predicted_settlement_credit_cc(position, v_combo)
            if credit_cc > 0:
                self._balance.note_receivable(position.position_id, credit_cc)

    def _maybe_resolve_settled_marginals(self) -> None:
        """Launch one bounded settled-marginal fetch pass (single-flight,
        fire-and-forget — the book-risk-task pattern) when a committed leg's
        book has left the feed and its graded result is still unfetched/due.
        The maintenance tick never awaits the REST reads; results land in the
        resolver's permanent cache and the NEXT book-risk recompute consumes
        them via ``_marginals``. No resolver wired ⇒ no-op (prior behaviour).

        Registration is the BATCH walk below (every tick, all committed legs
        at once — the relight2 fix); the ``_marginals`` fallback's per-query
        noting remains as belt-and-braces only."""
        if self._settled is None:
            return
        self._register_settled_candidates()
        if not self._settled.has_due_pending:
            return
        if self._settled_task is not None and not self._settled_task.done():
            return
        self._settled_task = asyncio.ensure_future(self._settled.resolve_pending())

    async def maintenance_tick(self) -> None:
        """TTL expiry + reprice + P&L mark + daily-loss halt. Every few 100ms."""
        self._refresh_daily_pnl()
        # Arm/refresh the portfolio-CVaR book-risk snapshot (throttled, off the hot
        # path) BEFORE the check below reads it, so the maintenance-driven halt
        # escalation sees a current joint-tail figure. With a book_risk_pool this
        # LAUNCHES the MC in a worker and returns immediately (never blocks the tick
        # / heartbeat); without one it runs inline (fast in paper/tests).
        self._maybe_recompute_book_risk()
        # BOOT-WARMUP QUOTE GATE: evaluate/latch off the tick too, so quoting
        # opens the moment the first usable snapshot publishes even with zero
        # RFQ flow — and the loud still-holding warning fires on a quiet boot
        # (self-throttled to once per freshness window). One bool read once
        # open; never re-holds.
        self.quote_warmup_open()
        # DEPLOYMENT SCALE (operator LEVER #1): re-SOLVE how much of the already-
        # enforced envelope is still unused, on its own (slower) throttle and in
        # the SAME worker pool — never on the quote path, which only reads the
        # resulting float. Disarmed by default ⇒ an immediate return, so the
        # tick's timing is unchanged until the operator arms it.
        self._maybe_solve_deploy_scale()
        # PEAK-CONCENTRATION profile (2026-07-18): rebuild the cached committed-
        # book peak scorelines when a fill/settlement changed the position set —
        # a pure PRICING input to the skew seam (stale/absent = neutral adder).
        self._maybe_recompute_peak_profile()
        # SETTLED-LEG MARGINAL RESOLUTION (2026-07-18): fetch graded results
        # for committed legs whose books left the feed — bounded, off-loop,
        # single-flight — so the book-risk model regains a marginal for every
        # risk-modeled leg and the CVaR cap stops failing closed on a book
        # that merely holds settled-leg cross-game combos. Runs AFTER the
        # recompute paths above, which are what REGISTER the missing committed
        # legs (via ``_marginals``), so the first pass already sees them all.
        self._maybe_resolve_settled_marginals()
        # SETTLEMENT RECEIVABLES (2026-07-19): once a position's outcome is
        # KNOWN from graded facts, its predicted credit shields the give-back
        # halts from the settlement-cascade equity trough. After the resolver
        # pass above so a fact fetched this tick notes its receivable this tick.
        self._refresh_settlement_receivables()
        # FILL-RECORD RECOVERY SWEEP (2026-07-16 P1): repair a confirmed fill
        # whose quote_executed WS message was lost, BEFORE the limit check so a
        # recovered position counts against the caps this same tick. Runs even
        # when halted — recording exchange truth is reconciliation, not quoting.
        await self._sweep_unrecorded_fills()
        # ALARM-ONLY SWEEPS — LAUNCHED, NEVER AWAITED (2026-07-26, the
        # 20:12:24Z stall). The fills-ledger diff (incident C's account-wide
        # backstop) and the position-ledger divergence invariant are pure
        # diagnostics: they hold no safety decision and nothing below reads
        # their result this tick. Awaiting them put an UNBOUNDED store read in
        # front of TTL expiry, the enforced limit/halt check and the reprice
        # sweep — and, before the liveness split, in front of the heartbeat.
        # They now run as single-flight background tasks under their own wall
        # bounds; a saturated store degrades to a logged, retried skip. See the
        # OFF-LOOP ALARM-ONLY SWEEPS block at the top of this module.
        self._launch_diagnostic_sweeps()
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
        #
        # WITHDRAW-PENDING RESOLUTION runs FIRST (2026-07-26): a quote whose
        # withdrawal is UNKNOWN is not a reprice candidate — it is a quote we are
        # trying to remove — and resolving it by READ before the sweep both frees
        # capacity this same tick and keeps the sweep out of the write-driver
        # business it used to be in. Returns instantly when nothing is pending
        # (the steady state), so the tick's cost is unchanged.
        await self._resolve_withdraw_pending()
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

        def _sweep_remaining_s() -> float:
            """What is LEFT of this sweep's wall budget, for the withdrawals it
            drives (2026-07-27). The top-of-iteration check bounds how many
            quotes the sweep touches; this bounds how long ONE of them may wait
            on the write-token bucket, which is the bound that was missing when
            the 07-27 tick blew its progress bound inside a single starved
            delete. Derived from the sweep's own budget — no second number."""
            return (budget_ns - (self._clock.monotonic_ns() - sweep_start_ns)) / 1e9

        prev_handled: str | None = None
        for quote_id, state in items:
            self._beat()
            # WALL BUDGET FIRST (2026-07-26): every branch below can now do
            # TOKEN-PACED work — the TTL delete goes through the same metered
            # write path as everything else — so the budget has to bound the
            # loop BEFORE the branch, not only before the reprice. Checked here
            # it also matches its own comment: the current quote was not
            # handled, so the rotation resumes AT it next tick.
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
            if state.accepted:
                prev_handled = quote_id
                continue
            if state.withdraw_pending_reason is not None:
                # UNKNOWN withdrawal (B3). NOT re-driven here: the sweep is not a
                # write driver any more. ``_resolve_withdraw_pending`` (top of
                # this tick) owns it — one READ proves gone-or-resting for the
                # WHOLE pending set, and only the proven-resting ids pay a
                # metered write. Re-asking per quote per tick is what made the
                # 429 storm self-sustaining. Never repriced while pending: it is
                # a quote we are trying to remove, not one we want to improve.
                prev_handled = quote_id
                continue
            age_s = (now - state.created_mono_ns) / 1e9
            if age_s > self._config.quote_ttl_s:
                await self._delete_quote(
                    quote_id,
                    ReasonCode.DELETE_TTL_EXPIRED,
                    budget_s=_sweep_remaining_s(),
                )
                # Deleted — prev_handled must only ever name a SURVIVING quote:
                # a marker pointing at a removed id fails next tick's index
                # lookup and silently discards the rotation (verify follow-up
                # 2026-07-16). Deleted quotes aren't in next tick's items, so
                # resuming after the last survivor skips nothing. An UNRESOLVED
                # TTL delete leaves the quote in ``_open`` (pending) — it is a
                # survivor and a valid marker, so mark it as one.
                if quote_id in self._open:
                    prev_handled = quote_id
                continue
            result = await self._price_async(state.rfq)
            if isinstance(result, NoQuote):
                await self._delete_quote(
                    quote_id,
                    ReasonCode.DELETE_LEG_STALE,
                    budget_s=_sweep_remaining_s(),
                )
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
                    await self._delete_quote(
                        quote_id,
                        ReasonCode.DELETE_LEG_MOVED,
                        budget_s=_sweep_remaining_s(),
                    )
            if quote_id in self._open:
                prev_handled = quote_id

    async def _spend_withdraw_tokens(
        self,
        budget: WriteBudget,
        cost: int,
        remaining_s: Callable[[], float | None],
    ) -> bool:
        """FIFO admission to the write-token bucket. True ⇒ ``cost`` tokens were
        spent and the caller may send its DELETE; False ⇒ deferred (never asked).

        WHY A GATE AND NOT A BARE ``try_spend`` LOOP (2026-07-27, the 07-27
        maintenance stall). A token bucket is not a queue: whoever calls
        ``try_spend`` at the instant tokens exist wins them. A waiter that slept
        on ``seconds_until`` is therefore beatable — forever — by any task that
        arrives later and spends on entry, and a starved waiter inside the
        reprice sweep holds the whole maintenance loop (the sweep checks its wall
        budget only at the TOP of each iteration), which is how a tick ran past
        its 30.5 s progress bound and got the bot emergency-killed while it was
        healthy. ``asyncio.Lock`` wakes waiters in ARRIVAL order, so admission is
        FIFO: a later arrival queues BEHIND an existing waiter and cannot take
        the tokens it is sleeping for.

        The lock is held ACROSS the refill sleep on purpose — that is the
        fairness — but only until tokens are spent; the DELETE itself is issued
        outside it, so batch concurrency is unchanged. Throughput is unchanged
        too: the holder sleeps exactly until IT can pay, and nothing else could
        have paid earlier out of the same shared bucket.

        Both waits are bounded by the caller's ``remaining_s`` when it has one:
        the LOCK wait as well as the refill wait, because a bounded caller queued
        behind the terminal unbounded ``cancel_all`` would otherwise inherit its
        unbounded wait through the lock — the same stall by another door."""
        left = remaining_s()
        if left is not None and left <= 0.0:
            return False
        try:
            if left is None:
                await self._withdraw_gate.acquire()
            else:
                await asyncio.wait_for(self._withdraw_gate.acquire(), left)
        except TimeoutError:  # asyncio.TimeoutError is this since 3.11
            self._metrics.inc("withdraw.token_gate_deferred")
            return False
        try:
            while not budget.try_spend(cost):
                wait_s = budget.seconds_until(cost)
                if wait_s == float("inf"):
                    # capacity < one call's cost: no wait can ever satisfy it.
                    return False
                left = remaining_s()
                if left is not None:
                    if left <= 0.0:
                        self._metrics.inc("withdraw.token_gate_deferred")
                        return False
                    wait_s = min(wait_s, left)
                await asyncio.sleep(max(wait_s, 0.0))
            return True
        finally:
            self._withdraw_gate.release()

    async def _withdraw_batch(
        self, quote_ids: Sequence[str], *, budget_s: float | None = None
    ) -> tuple[list[str], list[str], list[str]]:
        """Withdraw a BATCH of resting quotes from the exchange, concurrently,
        PACED BY THE WRITE-TOKEN BUDGET, under a per-call wall bound and an
        optional whole-pass budget. Returns ``(gone, unresolved, deferred)``.

        - ``gone``: PROVABLY off the wire — the exchange acknowledged the
          delete, or answered 404 (it has no such quote — ``_already_gone``).
          Only these leave our mirror.
        - ``unresolved``: we asked and do NOT know the outcome (429 / 5xx /
          timeout / transport). The quote may still be RESTING and may still
          FILL, so it is UNKNOWN, not gone: it stays in the mirror, counts as
          not-provably-withdrawn, and is retried on the next pass. A 429 in
          particular is not a failed delete at all — the request never reached
          the book — and treating it as a confirmed failure is how a rate-limit
          storm would make the mirror forget live quotes.
        - ``deferred``: never asked, because the pass ran out of WALL budget.
          Still resting and still ours; the caller retries next tick.

        Concurrency is whatever the token bucket pays for and no more: at most
        ``capacity / DELETE_QUOTE_TOKEN_COST`` deletes can be in flight before
        the bucket is empty, and from then on the pass admits calls at the
        bucket's refill rate. Never raises. See the BOUNDED QUOTE WITHDRAWAL
        block for why each bound exists."""
        if not quote_ids:
            return ([], [], [])
        budget = self._withdraw_budget
        cost = DELETE_QUOTE_TOKEN_COST
        deadline_ns: int | None = None
        if budget_s is not None:
            deadline_ns = self._clock.monotonic_ns() + int(budget_s * 1e9)

        def _remaining_s() -> float | None:
            if deadline_ns is None:
                return None
            return (deadline_ns - self._clock.monotonic_ns()) / 1e9

        async def _one(quote_id: str) -> tuple[str, str, BaseException | None]:
            # ── token gate ──────────────────────────────────────────────────
            # Spend BEFORE the request, so what we emit is what the exchange's
            # bucket sees. Out of tokens ⇒ WAIT for the bucket's own refill
            # (never a hand-picked poll interval), because deferring costs a
            # halt on the next tick while waiting costs only wall time the
            # caller already budgeted for. FAIR (FIFO) since 2026-07-27: see
            # ``_spend_withdraw_tokens``.
            if not await self._spend_withdraw_tokens(budget, cost, _remaining_s):
                return (quote_id, "deferred", None)
            # RE-CHECK AFTER THE TOKEN GATE (2026-07-26, gate PoC R2/R3). The
            # gate above can sleep for SECONDS waiting on bucket refill, and
            # accepts land on a different task meanwhile. Every other withdrawal
            # chooser refuses to touch a mid-confirm quote; without this the
            # drain fires a fresh DELETE at a quote we have already reserved
            # risk for and are about to confirm. Checked as late as possible —
            # once the request is in flight the window is closed and benign.
            live = self._open.get(quote_id)
            if live is not None and live.accepted:
                self._metrics.inc("withdraw_resolve.accepted_deferred")
                return (quote_id, "deferred", None)
            timeout_s = _CANCEL_TIMEOUT_S
            remaining_s = _remaining_s()
            if remaining_s is not None:
                if remaining_s <= 0.0:
                    return (quote_id, "deferred", None)
                # The budget is a HARD wall, not just an admission gate: a call
                # admitted with 0.2s left may not run for the full per-call
                # timeout, or a hung exchange would still hold the caller for
                # one timeout (measured: 2 waves = 20s against a 0.2s budget).
                # Whichever bound is tighter wins.
                timeout_s = min(timeout_s, remaining_s)
            try:
                await asyncio.wait_for(self._sender.delete_quote(quote_id), timeout_s)
                return (quote_id, "gone", None)
            except Exception as exc:
                if _already_gone(exc):
                    return (quote_id, "gone", exc)
                return (quote_id, "unresolved", exc)

        results = await asyncio.gather(*(_one(qid) for qid in quote_ids))
        gone: list[str] = []
        unresolved: list[str] = []
        deferred: list[str] = []
        already_gone = 0
        rate_limited = 0
        # Reset per batch: this names THIS pass's failure modes, never a union
        # over history (a stale union would make every later halt look like a
        # repeat of the first).
        kinds: Counter[str] = Counter()
        for quote_id, outcome, exc in results:
            if outcome == "gone":
                gone.append(quote_id)
                if exc is not None:
                    already_gone += 1
            elif outcome == "unresolved":
                unresolved.append(quote_id)
                kinds[_withdraw_failure_kind(exc)] += 1
                if exc is not None and _rate_limited(exc):
                    rate_limited += 1
                log.warning(
                    "delete_quote_unresolved", quote_id=quote_id, error=repr(exc)
                )
            else:
                deferred.append(quote_id)
                kinds[_withdraw_failure_kind(None)] += 1
        self._last_withdraw_failure_kinds = kinds
        if already_gone:
            # Routine at end-of-game; counted, not alarmed.
            self._metrics.inc("quote.delete_already_gone", already_gone)
            log.info("delete_quote_already_gone", count=already_gone)
        if unresolved:
            self._metrics.inc("quote.delete_unresolved", len(unresolved))
        if rate_limited:
            # Our pacing is wrong if this is ever non-zero — the whole point of
            # the token budget is that the withdrawal wave never 429s.
            self._metrics.inc("quote.delete_rate_limited", rate_limited)
            log.error(
                "quote_withdrawal_rate_limited",
                count=rate_limited,
                detail="429 on a token-paced withdrawal — quotes are UNKNOWN "
                "(kept in the mirror) and retried; pacing needs review",
            )
        if deferred:
            self._metrics.inc("quote.delete_deferred", len(deferred))
            log.warning(
                "quote_withdrawal_budget_deferred",
                deferred=len(deferred),
                detail="withdrawal pass exceeded its wall budget — remaining "
                "quotes stay resting and are retried next tick",
            )
        return (gone, unresolved, deferred)

    async def _withdraw_and_reconcile(
        self,
        quote_ids: Sequence[str],
        reason: ReasonCode | str,
        *,
        budget_s: float | None = None,
    ) -> tuple[int, int]:
        """THE withdrawal path. Every caller — single quote, scoped market
        quarantine, whole-book cancel-all, the resolver's re-delete — comes
        through here, so the drop rule is written ONCE. Returns
        ``(gone, unresolved_or_deferred)``.

        The rule, verbatim from ``_withdraw_batch``'s contract:

        * ``gone`` — the exchange ACKED the delete or answered 404
          (``_already_gone``: it has no such quote). PROVABLY off the wire,
          cannot fill ⇒ dropped from the mirror and from exposure.
        * everything else — 429 / 5xx / timeout / transport, or never asked
          because the pass ran out of wall budget — is UNKNOWN. The quote may
          still be RESTING and may still FILL, so it STAYS in the mirror (risk
          keeps counting it, it keeps consuming ``max_open_quotes``) and is
          marked withdraw-pending for the read resolver.

        ``_drop_quote`` is called for withdrawals ONLY from here, and only from
        the ``gone`` list, so no caller's docstring premise can make the mirror
        forget a quote the exchange never proved gone (defect 1: ``cancel_all``
        used to drop unresolved ids on a "terminal path, no next tick" premise
        that is false for 5 of its 7 callers)."""
        if not quote_ids:
            return (0, 0)
        gone, unresolved, deferred = await self._withdraw_batch(
            quote_ids, budget_s=budget_s
        )
        for quote_id in gone:
            self._drop_quote(quote_id)
            self._metrics.inc(f"quote.deleted.{reason}")
        # Stamp AFTER the batch returns, never before: the happens-before guard
        # is only sound if the recorded ask time is >= the instant the request
        # actually went out. A later stamp can only make the resolver MORE
        # conservative (it declines to judge the quote on this round's read).
        asked_ns = self._clock.monotonic_ns()
        for quote_id in (*unresolved, *deferred):
            state = self._open.get(quote_id)
            if state is None:
                continue
            state.withdraw_pending_reason = reason
            state.withdraw_asked_mono_ns = asked_ns
        return (len(gone), len(unresolved) + len(deferred))

    async def _resolve_withdraw_pending(self) -> None:
        """Resolve every UNKNOWN withdrawal with ONE READ, not a retried write.

        ``GET /communications/quotes?user_filter=self&status=open`` is the same
        account-wide enumerator the startup reconcile and the supervisor kill
        path already use (``exchange/quote_query.list_open_quotes`` — cursor
        paginated, min_ts/max_ts windowed so it never triggers the full-history
        scan that trips the exchange circuit-breaker). It costs 10 READ tokens —
        the default endpoint cost; GetQuotes is not one of the 13 live-verified
        overrides — and Kalshi meters reads on a bucket SEPARATE from writes, so
        this is O(1) in the pending count on the bucket a write storm is not
        touching. At ``max_open_quotes`` = 200 that is 10 read tokens per tick
        against the deleted retry driver's 400 WRITE tokens per tick.

        Three outcomes, no fourth:
          * ABSENT from the open set (and asked strictly before the read) ⇒
            PROVEN off the wire ⇒ dropped, counted as a completed deletion.
          * PRESENT ⇒ PROVEN still resting ⇒ re-deleted through the metered
            write path, grouped by the reason it was being withdrawn for.
          * the read FAILED, or the quote's ask does not pre-date the read ⇒
            NOTHING is resolved. UNKNOWN never silently becomes "gone".

        No lister wired (paper / backtests / minimal rigs) ⇒ the pending set is
        drained by the metered write alone. That still terminates — a re-DELETE
        of a gone quote answers 404, which is proof — it simply cannot observe an
        accept/execute transition, and it is bounded by the write bucket rather
        than by the read.

        AN ACCEPTED QUOTE IS NEVER RESOLVED HERE (2026-07-26 gate, B1). This was
        the ONLY withdrawal chooser without the guard the other three carry
        (``cancel_all``, ``cancel_quotes_touching``, ``_pick_eviction_victim`` —
        "not mid-confirm, never yank"), and it is the one chooser that both REAPS
        and RE-DELETEs. One step reaches it: a TTL / RFQ-gone DELETE 429s ⇒ the
        quote is UNKNOWN and STILL RESTING ⇒ the taker ACCEPTS it ⇒
        ``on_quote_accepted`` sets ``accepted`` and then awaits (store write +
        reservation + confirm REST) across several 0.5 s ticks. Inside that window
        the unguarded resolver did two unrecoverable things: absent from the open
        list (an ACCEPTED quote is no longer OPEN) it booked a quote that FILLED as
        a proven withdrawal, and present it fired a fresh DELETE at a quote whose
        risk we have already reserved and are about to confirm.

        The guard DEFERS, it does not strand: the accepted quote keeps its pending
        reason and keeps counting for risk, and ``on_quote_accepted`` drops it from
        the mirror in EVERY branch — confirm, decline, lapse, unreadable side,
        unknown size, and (via the wrapper's ``_drop_quote``) an escaping
        exception. So the deferral terminates in exactly one confirm window, with
        or without the network."""
        pending: list[tuple[str, OpenQuoteState]] = []
        accepted_deferred = 0
        for qid, state in self._open.items():
            if state.withdraw_pending_reason is None:
                continue
            if state.accepted:
                # MID-CONFIRM: not ours to reap and not ours to re-delete. The
                # accept path owns this quote and drops it when it finishes.
                accepted_deferred += 1
                continue
            pending.append((qid, state))
        if accepted_deferred:
            self._metrics.inc(
                "withdraw_resolve.accepted_deferred", accepted_deferred
            )
        if not pending:
            return  # the steady state — zero cost on the maintenance tick
        deadline_ns = self._clock.monotonic_ns() + int(
            _WITHDRAW_RESOLVE_BUDGET_S * 1e9
        )
        read = await self._read_open_quote_ids(len(pending))
        still_resting: dict[ReasonCode | str, list[str]] = {}
        reaped: list[str] = []
        if read is not None:
            issued, open_ids = read
            for quote_id, state in pending:
                reason = state.withdraw_pending_reason
                if reason is None:  # pragma: no cover — pending by construction
                    continue
                if state.withdraw_asked_mono_ns >= issued:
                    # The ask does not strictly pre-date the read; this read
                    # cannot speak about it. Next tick's read can.
                    self._metrics.inc("withdraw_resolve.not_yet_readable")
                    continue
                # RE-CHECK AFTER THE AWAIT (2026-07-26, gate PoC R1/R2). The
                # `state.accepted` test that built `pending` is a SNAPSHOT taken
                # BEFORE `_read_open_quote_ids`, and accepts arrive on a
                # DIFFERENT task (quote_app.quote_event_worker ->
                # on_quote_accepted). An accept landing inside that await is
                # invisible to the snapshot — and because an ACCEPTED quote is
                # not OPEN, "absent from the open list" is the EXPECTED answer
                # for it, so reaping would be the DEFAULT outcome, not a rare
                # interleaving. Measured without this re-check: proven_gone=2 on
                # a quote that then logged confirm_ok and booked a fill. Always
                # read the LIVE state here, never the snapshot.
                live = self._open.get(quote_id)
                if live is None or live.accepted:
                    # Already dropped by the accept path, or now mid-confirm:
                    # not ours to reap and not ours to re-delete.
                    self._metrics.inc("withdraw_resolve.accepted_deferred")
                    continue
                if quote_id in open_ids:
                    still_resting.setdefault(reason, []).append(quote_id)
                else:
                    self._drop_quote(quote_id)
                    self._metrics.inc(f"quote.deleted.{reason}")
                    self._metrics.inc("withdraw_resolve.proven_gone")
                    reaped.append(quote_id)
            if reaped:
                # ONE aggregated line, not one per quote: an end-of-game wave
                # reaps the whole book at once and this runs on the maintenance
                # loop.
                log.info(
                    "withdraw_resolved_gone",
                    count=len(reaped),
                    quote_ids=reaped[:20],
                    detail="absent from the exchange's open-quote list — PROVEN "
                    "off the wire; the mirror can forget them",
                )
        else:
            # No read (unwired or failed): nothing is PROVEN, so nothing is
            # dropped. The metered write is the only drain available.
            for quote_id, state in pending:
                reason = state.withdraw_pending_reason
                if reason is None:  # pragma: no cover
                    continue
                still_resting.setdefault(reason, []).append(quote_id)
        for reason, ids in still_resting.items():
            remaining_s = (deadline_ns - self._clock.monotonic_ns()) / 1e9
            if remaining_s <= 0.0:
                self._metrics.inc("withdraw_resolve.drain_budget_deferred")
                break
            gone, _failed = await self._withdraw_and_reconcile(
                ids, reason, budget_s=remaining_s
            )
            if gone:
                self._metrics.inc("withdraw_resolve.drained", gone)
        await self._halt_if_book_is_wholly_unprovable()

    async def _read_open_quote_ids(
        self, pending_count: int
    ) -> tuple[int, set[str]] | None:
        """The prover: ``(issued_mono_ns, our account's OPEN quote ids)``, or
        ``None`` when we could not establish them (no lister wired, no read
        tokens, or the read failed / timed out). ``None`` resolves NOTHING — it
        never means "empty". ``issued`` is the instant the request went OUT,
        which is the happens-before key: only a withdrawal asked strictly before
        it may be judged by this answer."""
        if self._quote_lister is None:
            return None
        budget = self._read_budget
        if budget is not None and not budget.try_spend(DEFAULT_ENDPOINT_TOKEN_COST):
            # Refused, never queued: the 0.5s maintenance tick IS the retry.
            self._metrics.inc("withdraw_resolve.read_budget_deferred")
            return None
        issued = self._clock.monotonic_ns()
        try:
            quotes = await asyncio.wait_for(
                # retries=1: the helper's default 4x0.5s backoff (3.5s) exceeds
                # this tick's own poll bound, and the maintenance tick is the
                # retry loop.
                list_open_quotes(
                    self._quote_lister,
                    int(self._clock.now().timestamp()),
                    retries=1,
                ),
                _MAINTENANCE_POLL_TIMEOUT_S,
            )
        except Exception as exc:
            self._metrics.inc("withdraw_resolve.read_failed")
            log.warning(
                "withdraw_resolve_read_failed",
                pending=pending_count,
                error=repr(exc),
                detail="open-quote read failed — NOTHING resolved; the pending "
                "quotes stay UNKNOWN (risk keeps counting them) and the next "
                "maintenance tick re-reads",
            )
            return None
        self._withdraw_read_ok_mono_ns = issued
        self._metrics.inc("withdraw_resolve.reads")
        return (issued, set(open_quote_ids(quotes)))

    async def _halt_if_book_is_wholly_unprovable(self) -> None:
        """THE BOUNDED EXIT. Pending quotes keep counting against
        ``max_open_quotes`` — a quote that may be resting is real risk AND real
        capacity, and excluding it would uncap the resting worst-case loss the
        mass-acceptance fold carries. So the terminal state is a predicate over
        EXISTING state and an EXISTING limit, with no new number:

            the book is AT capacity, AND every open quote is withdraw-pending,
            AND no successful open-quote read has landed since the oldest of
            those asks ⇒ the book cannot be proven at all ⇒
            HALT_NEEDS_RECONCILE.

        That is the doctrine ``cancel_quotes_touching`` already states ("a scoped
        response we could not carry out is not a scoped response") and exactly
        what ``_startup_reconcile`` does when it returns False: the restart
        rebuilds the book from the exchange over this same endpoint. Every branch
        of the withdrawal now terminates — resolve-by-read, else metered write
        drain, else halt → restart reconcile — and none of them converts UNKNOWN
        into "gone"."""
        if self._killswitch.halted:
            return
        states = list(self._open.values())
        if not states:
            return
        if len(states) < self._limits.limits.max_open_quotes:
            return
        if any(s.withdraw_pending_reason is None for s in states):
            return
        oldest_ask_ns = min(s.withdraw_asked_mono_ns for s in states)
        read_ok = self._withdraw_read_ok_mono_ns
        if read_ok is not None and read_ok >= oldest_ask_ns:
            return
        self._metrics.inc("withdraw_resolve.unprovable_halt")
        await self._killswitch.halt(
            ReasonCode.HALT_NEEDS_RECONCILE,
            f"{len(states)} open quotes at cap "
            f"{self._limits.limits.max_open_quotes}, every one withdraw-PENDING "
            "and unread since its withdrawal was asked — the book cannot be "
            "proven; the restart exchange-reconcile owns it",
        )

    async def cancel_all(
        self,
        reason: ReasonCode | str,
        *,
        budget_s: float | None = _WITHDRAW_RESOLVE_BUDGET_S,
    ) -> None:
        """Best-effort delete of every open quote. Idempotent, race-safe.

        Token-paced + per-call timeout (2026-07-26): a whole-book cancel on a
        big book must not out-run the exchange's write budget, and one hung
        DELETE must not hold the halt path open forever.

        WHOLE-PASS WALL BUDGET (2026-07-26 gate, B2 — THROUGHPUT NEVER
        REGRESSES). Token pacing alone made this call BLOCK its caller for the
        bucket's refill time: at the live bucket (200 tok / 20 tok/s) and the
        live ``max_open_quotes`` = 200 the cliff sits at 100 quotes (the burst
        the full bucket pays for) and the measured hold is 10.0 s full-bucket /
        20.0 s empty-bucket. Five of the seven callers are NOT terminal and run
        INLINE on paths that must not stall: the feed's invalidate (fired from
        ``_handle_disconnect`` AND from a gap BEFORE the resync is sent),
        ``on_channel_lost`` (which force-reconnects the socket carrying our RFQ
        flow immediately after), the exchange-status halt, and both
        DECLINE_FILL_VELOCITY sites. Holding those for tens of seconds is a
        pricing/quoting throughput regression — the exact thing the standing rule
        forbids — and the pre-token-pacing implementation was ~1 RTT.

        The bound is the EXISTING withdrawal-pass budget
        (``_WITHDRAW_RESOLVE_BUDGET_S``, itself ``_REPRICE_SWEEP_BUDGET_S``), not
        a new number: it is the whole-pass wall bound the read-resolver's own
        pass already gets on every maintenance tick, and the resolver is exactly
        who inherits this pass's leftovers. Nothing is abandoned — quotes the
        budget defers are never asked, so they stay in the mirror marked
        withdraw-pending (risk keeps counting them) and the 0.5 s resolver
        re-drives them, proving each one gone by READ or re-DELETEing it. This is
        the same "deferred, never abandoned" contract ``cancel_quotes_touching``
        carries.

        ``budget_s=None`` restores the unbounded pass and is passed by the TWO
        genuinely terminal callers only (``on_halt`` → ``_stop.set()`` and
        shutdown), where waiting for the bucket is right: there is no next tick
        to inherit the leftovers, so the pass must attempt every quote, and a
        halting bot has nothing left to hold up.

        SAME DROP RULE AS EVERY OTHER WITHDRAWAL (2026-07-26 gate, defect 1).
        This used to drop the mirror for EVERY id, unresolved ones included, on
        the premise "terminal path, there is no next tick". That premise is FALSE
        for 5 of the 7 callers — ``on_invalidate`` (feed resync), the
        force-reconnecting ``on_channel_lost``, ``HALT_EXCHANGE_STATUS``, and
        both ``DECLINE_FILL_VELOCITY`` sites all keep the process running — so a
        429 storm on any of them made the book forget quotes that were still
        resting and could still fill. The premise is deleted rather than
        defended: only PROVEN-gone ids leave the mirror, here as everywhere. For
        the 2 genuinely terminal callers (``on_halt`` → ``_stop.set()``, and
        shutdown) keeping the mirror costs nothing — the process is stopping and
        ``_startup_reconcile`` rebuilds truth from the exchange. One rule, no
        flag, no branch."""
        open_ids = [qid for qid, s in self._open.items() if not s.accepted]
        if not open_ids:
            return
        log.warning("cancel_all", reason=str(reason), count=len(open_ids))
        _gone, failures = await self._withdraw_and_reconcile(
            open_ids, reason, budget_s=budget_s
        )
        if failures:
            log.warning(
                "cancel_all_unresolved",
                reason=str(reason),
                unresolved=failures,
                detail="withdrawal outcome UNKNOWN — those quotes stay in the "
                "mirror (risk keeps counting them) and are resolved by the "
                "open-quote read; a restart reconciles them off the exchange",
            )
        self._metrics.inc("quote.cancel_all")

    async def cancel_quotes_touching(
        self,
        tickers: AbstractSet[str],
        reason: ReasonCode | str,
        *,
        budget_s: float | None = None,
    ) -> tuple[int, int]:
        """SCOPED withdrawal: delete every DELETABLE resting quote that carries
        a leg in ``tickers``. Returns ``(deleted, failures)``.

        The market-scoped counterpart of ``cancel_all`` (2026-07-26 metadata
        breaker rebuild): when one market's LIFECYCLE state moves (exchange
        trading pause, unpause — which auto-cancels resting orders exchange-side
        anyway — or a close_time rewrite that invalidates the time-to-close we
        priced on), the proportionate response is to pull OUR quotes off THAT
        market, not to kill the whole book.

        ACCEPTED quotes are skipped exactly as ``cancel_all`` skips them: an
        accepted quote is mid-confirm and is not ours to delete. They are not
        counted as failures — nothing was left undone that we could do.

        BURST-BOUNDED (2026-07-26): concurrent, paced by the WRITE-TOKEN BUDGET,
        with a per-call wall bound and — when the caller passes ``budget_s`` — a
        whole-pass budget sized to the caller's own tick. An end-of-game
        lifecycle wave quarantining a dozen markets at once is NORMAL and must
        neither occupy the calling loop for tens of seconds nor breach the
        account's write-token bucket (a 429 storm here would trip the
        rate-limit-burst halt AND leave the quarantine unenforced).

        ``failures`` counts quotes we could not provably get off the wire:
        deletes whose outcome is UNKNOWN (429/5xx/timeout/transport) PLUS any
        deferred by the budget. The caller (quote_app's quarantine enforcement)
        treats a non-zero count as an UNENFORCED quarantine and escalates it to
        the whole-bot halt on the next status tick — fail-closed: a scoped
        response we could not carry out is not a scoped response.

        A 404 is NOT a failure: the exchange has already dropped that quote, so
        it is provably off the wire and can never fill (``_already_gone``).
        ``deleted`` is the count of quotes provably gone."""
        if not tickers:
            return (0, 0)
        target_ids = [
            qid
            for qid, state in self._open.items()
            if not state.accepted
            and any(leg.market_ticker in tickers for leg in state.rfq.legs)
        ]
        if not target_ids:
            return (0, 0)
        log.warning(
            "cancel_quotes_touching",
            reason=str(reason),
            count=len(target_ids),
            markets=sorted(tickers),
        )
        # Drop the mirror ONLY for quotes PROVABLY off the wire (acked, or 404 =
        # the exchange has no such quote). An UNRESOLVED delete — 429, 5xx,
        # timeout, transport — is not a confirmed anything: that quote may still
        # be resting and may still FILL, so it stays in the mirror, keeps
        # counting as not-provably-withdrawn, and is resolved by the read.
        # DEFERRED quotes were never asked about at all — same rule.
        return await self._withdraw_and_reconcile(
            target_ids, reason, budget_s=budget_s
        )

    @property
    def last_withdraw_failure_kinds(self) -> Counter[str]:
        """Failure modes of the LAST withdrawal batch, by kind (a COPY — the
        caller can never mutate the live counter). Empty when the last batch
        withdrew everything provably, or when no batch has run.

        Read by ``QuoteApp._enforce_market_quarantine`` to attribute an
        UNENFORCED quarantine in the halt receipt. Deliberately a separate
        read-only property rather than a widened ``cancel_quotes_touching``
        return: seven call sites destructure that ``(deleted, failures)``
        tuple, and an observability field must not reshape a withdrawal API."""
        return Counter(self._last_withdraw_failure_kinds)

    @property
    def open_quote_count(self) -> int:
        return len(self._open)

    def has_open_quote(self, rfq_id: str) -> bool:
        return rfq_id in self._by_rfq

    def marginal_of(self, market_ticker: str) -> float | None:
        """Public read accessor for a leg's current P(YES) — the SAME provider
        (feed microprice, then the settled-fact cache) the pricer and exposure
        book use. ``None`` when the book is missing/invalid and no graded fact
        is cached (fail-closed: an unreadable leg is UNKNOWN, never a guessed
        value). Exposed so the risk-breaker sampler (quote_app's
        _sample_breaker_inputs) can feed the marginal-jump breaker the exact
        marginals we priced on, without reaching into a private name."""
        return self._marginals(market_ticker)

    def settled_watch_exempt(self, market_ticker: str) -> bool:
        """True iff the marginal-jump breaker must NOT watch this leg's
        readability/jump: the settled-marginal resolver holds its
        exchange-graded fact, or the exchange told it the market is no longer
        live (closed/determined/disputed/amended/finalized — including
        closed-but-UNGRADED). For such a ticker "readable → unreadable" is the
        NORMAL, PERMANENT close transition (books cease to exist at close) and
        "0.97 → 1.000" is a grading, not a feed move — neither is the
        dead-feed/mis-mark signature the breaker exists to catch (live halt
        2026-07-18 02:17Z: halt_marginal_jump on a settled FRAENG leg killed
        the bot 90s after preflight). False when no resolver is wired or
        nothing is exchange-confirmed about the ticker — the breaker keeps its
        full fail-closed watch (a genuinely dead feed on a LIVE market still
        halts). The QUOTE path is unaffected either way: an ungraded closed
        leg still prices as UNKNOWN (no-quote)."""
        if self._settled is None:
            return False
        return self._settled.market_no_longer_live(market_ticker)

    def inplay_watch_exempt(self, market_ticker: str) -> bool:
        """True iff the marginal-jump breaker must NOT watch this leg because
        its game is IN-PLAY: the game has STARTED per the SAME start-time ladder
        the pregame gate stops quoting on (2026-07-19: 45 halt_marginal_jump
        trips through the final, every one an in-play ESPARG book going dark
        mid-game — normal in-play behaviour, not the dead-feed signature). The
        exemption begins exactly when quoting on the game ends, so a leg we can
        still QUOTE keeps the full fail-closed watch; committed in-play legs
        have nothing actionable behind a halt (resting quotes die via
        cancel-on-invalidate, confirms via last-look freshness, and the
        whole-feed staleness breaker stays fully armed). UNKNOWN start ⇒ False
        (keep watching); ``allow_inplay_legs`` ⇒ False (never blind a leg the
        operator re-enabled quoting on)."""
        return self._filter.leg_inplay_watch_exempt(market_ticker)

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
        self, rfq: Rfq, *, inventory_skew_cc: int = 0, force_in_play: bool = False
    ) -> ConstructedQuote | NoQuote:
        time_to_close = self._min_time_to_close_s(rfq)
        return self._engine.price(
            rfq,
            time_to_close_s=time_to_close if time_to_close is not None else -1.0,
            # force_in_play: the in-play SHADOW path (a leg is KNOWN started —
            # the schedule gate said so) prices with the engine's in-play
            # treatment regardless of the motion detector's read.
            in_play=force_in_play or self._inplay.any_anomalous(list(rfq.leg_tickers)),
            inventory_skew_cc=inventory_skew_cc,
        )

    async def _price_async(
        self, rfq: Rfq, *, inventory_skew_cc: int = 0, force_in_play: bool = False
    ) -> ConstructedQuote | NoQuote:
        """Async pricing for the hot RFQ path. With a joint pool configured the
        expensive joint step runs off-loop with a deadline (warm memo hits stay
        inline); without one it is exactly ``_price``. Identical $ output to
        ``_price`` — the pool runs the same pure joint code (pool_parity_check).
        A deadline breach or worker error is a fail-closed decline (no wedge)."""
        if self._joint_pool is None:
            return self._price(
                rfq, inventory_skew_cc=inventory_skew_cc, force_in_play=force_in_play
            )
        time_to_close = self._min_time_to_close_s(rfq)
        try:
            return await self._engine.price_offloaded(
                rfq,
                time_to_close_s=time_to_close if time_to_close is not None else -1.0,
                in_play=force_in_play
                or self._inplay.any_anomalous(list(rfq.leg_tickers)),
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
        # SETTLED-LEG FACT RESOLUTION (2026-08-01, flag-gated, PRICE-ONLY).
        # With ``skew.settled_fact_resolution`` armed, THIS snapshot — the one
        # the skew composition, the widen-shadow verdict and the leg-axis /
        # conc profiles read — fact-resolves exchange-determined legs out of
        # every concentration aggregate (a settled leg is realized P&L, not
        # concentration; the 7/29 boot fed 7 finished games' positions in as
        # if live and they never un-concentrated). The LIMIT-CHECK snapshots
        # (pre-gate, quote-time, confirm) never receive the provider: caps and
        # risk walls keep seeing the whole committed book. None while dark =
        # byte-identical to today.
        snap = self._exposure.snapshot(
            self._marginals,
            mass_acceptance=True,
            settled_facts=self._skew_settled_facts(),
        )
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
            # PEAK-CONCENTRATION steer (2026-07-18): the cached committed-book
            # peak profile + the live position generation. Absent/stale =>
            # the component is a hard ZERO adder inside the classifier
            # (neutral pricing — never a decline, never a throughput cost:
            # the hot path only reads <= K cached state rows per game).
            peak_profile=self._peak_profile_for_quote(),
            peak_book_generation=self._exposure.position_generation,
            # P(BOOK) STEER, Phase B1 (2026-07-25): the cached P(book)/tail-
            # share profile. SHADOW — pbook_armed defaults False, so skew_cc
            # (and pricing) stay byte-identical while pbook_cc is measured
            # on real flow (the derive-before-arm rule).
            pbook_profile=self._pbook_profile,
            pbook_book_generation=self._exposure.position_generation,
            # LEG-DIRECTION AXIS (2026-07-25): built from THIS quote-time
            # snapshot (no staleness window); p_book only when the cached MC
            # profile is generation-fresh (None ⇒ the component is neutral).
            leg_axis_profile=self._leg_axis_profile_from(snap),
            # LEVER #5 (operator directive 2026-07-27): the AND-BOUND
            # dollar-Herfindahl marginal (zero SE) priced by
            # Cov(candidate payoff, book P&L) off the already-paid-for CRN
            # cache, on a SYMMETRIC half-range derived from measured state,
            # centred so the average quote cannot widen (markups are FIXED),
            # and quantized onto the combo's OWN grid step so it can never be
            # annihilated by ``snap_bid_down``.
            # SHADOW-FIRST (2026-07-27 review B3): ``conc_armed`` defaults
            # False, so the steer is computed + logged here and CANNOT reach
            # price until the operator arms it off the shadow read-out.
            # ``conc_enabled: false`` is the zero-cost rollback — the profile
            # itself (three dict copies + the loss-event cache read) is not
            # even built.
            conc_profile=(
                self._concentration_profile(snap)
                if self._skew_params.conc_enabled
                else None
            ),
            margin_cc=constructed.total_width_cc,
            tick_cc=self._combo_tick_cc(rfq, constructed),
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
            peak_cc=skew.peak_cc,                        # composed peak component
            pbook_cc=skew.pbook_cc,                      # P(book) steer (shadow)
            # Full pbook decomposition at INFO (2026-07-25 review: the shadow
            # record must carry everything the ARMING decision reads —
            # factors + reasons per game, not just the composed number).
            pbook_per_game=[
                (game, adder, round(factor, 4), reason)
                for game, adder, factor, reason in skew.pbook_per_game
            ],
            # LEG-DIRECTION AXIS (2026-07-25, shadow): the family/entity
            # concentration components + full row decomposition — everything
            # the arming decision reads.
            family_cc=skew.family_cc,
            entity_cc=skew.entity_cc,
            leg_axis_rows=[
                (key, adder, round(factor, 4), reason)
                for key, adder, factor, reason in skew.leg_axis_rows
            ],
            # LEVER #5 (2026-07-27): everything the ARMING/AUDIT decision
            # reads — the AND-bound effective loss-event count before and
            # after, the zero-SE Herfindahl marginal, the measured
            # Cov(candidate, book) price in cc/contract, the per-axis wall
            # loads (DOLLARS vs each axis's own enforced wall — never a
            # count), the DERIVED symmetric half-range and WHICH measured
            # bound is binding it.
            conc_cc=(skew.conc.skew_cc if skew.conc else 0),
            conc_applied_cc=(skew.conc.applied_cc if skew.conc else 0),
            conc_n_events_pre=(
                round(skew.conc.hhi.n_pre, 4) if skew.conc else None
            ),
            conc_n_events_post=(
                round(skew.conc.hhi.n_post, 4) if skew.conc else None
            ),
            conc_hhi_marginal=(
                round(skew.conc.hhi.relative, 6) if skew.conc else None
            ),
            # THE SCALE-FREE READING (2026-07-27) — the number that actually
            # prices. ``conc_hhi_marginal`` above carries the 2p/T book-size
            # factor and is kept only for continuity of the readout;
            # ``conc_intensity`` is bucket dollar SHARE minus the book's
            # dollar-Herfindahl, which is invariant when the whole book scales.
            conc_intensity=(
                round(skew.conc.hhi.intensity, 6) if skew.conc else None
            ),
            conc_value_cc_per_contract=(
                None
                if skew.conc is None or skew.conc.value_cc_per_contract is None
                else round(skew.conc.value_cc_per_contract, 3)
            ),
            conc_score_raw=(round(skew.conc.score_raw, 4) if skew.conc else None),
            conc_score_centred=(
                round(skew.conc.score_centred, 4) if skew.conc else None
            ),
            conc_wall_loads={
                k: round(v, 4)
                for k, v in (skew.conc.wall_load_by_axis if skew.conc else {}).items()
            },
            conc_half_cc=(
                skew.conc.scale.half_cc
                if skew.conc and skew.conc.scale
                else None
            ),
            conc_scale_binding=(
                skew.conc.scale.binding if skew.conc and skew.conc.scale else None
            ),
            conc_reason=(skew.conc.reason if skew.conc else None),
            # THE ARMING SEAM (2026-07-27 review B3). ``conc_armed`` says
            # whether the steer touched THIS quote's price;
            # ``conc_shadow_applied_cc`` is the pricer-frame number the armed
            # composition WOULD have produced on exactly these inputs (None
            # once armed — then ``applied_cc`` is that number). Together they
            # are the whole arming read-out: pair them per bucket and the
            # operator sees the counterfactual against what actually shipped.
            conc_armed=self._skew_params.conc_armed,
            conc_shadow_applied_cc=skew.shadow_armed_applied_cc,
            # Budget neutrality (markups are FIXED): the live measured centre
            # of the score distribution. A mean far from 0 means the steer is
            # drifting into a markup change and the centring is not keeping up.
            steer_centre_mean=round(self._steer_centre.mean, 5),
            steer_centre_sd=round(self._steer_centre.sd, 5),
            steer_centre_n=self._steer_centre.n,
        )
        if skew.pbook_per_game:
            log.debug(
                "pbook_steer_detail",
                rfq_id=rfq.rfq_id,
                pbook_cc=skew.pbook_cc,
                per_game=[
                    {
                        "game": game,
                        "adder_cc": adder_cc,
                        "factor": round(factor, 4),
                        "reason": reason,
                    }
                    for game, adder_cc, factor, reason in skew.pbook_per_game
                ],
            )
        if skew.peak_per_game:
            # DEBUG-level explainability (operator directive: every peak adder
            # decision explainable — peak_overlap + adder_cc per game — without
            # flooding info-level; the info event above carries only peak_cc).
            log.debug(
                "peak_concentration_detail",
                rfq_id=rfq.rfq_id,
                peak_cc=skew.peak_cc,
                peak_widen_cc=skew.peak_widen_cc,
                peak_tighten_cc=skew.peak_tighten_cc,
                per_game=[
                    {
                        "game": game,
                        "adder_cc": adder_cc,
                        "peak_overlap": overlap,
                        "reason": reason,
                    }
                    for game, adder_cc, overlap, reason in skew.peak_per_game
                ],
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

    def _combo_tick_cc(self, rfq: Rfq, constructed: ConstructedQuote) -> int:
        """The combo's OWN grid step at the quoted bid (LEVER #5).

        The steer is quantized onto this so ``snap_bid_down`` reproduces it
        exactly instead of erasing it — 32.25% of live steer events fell below
        one tick and were silently annihilated. Unknown grid ⇒ 0, which makes
        the steer a hard 0: an unknown lattice is never a guessed default."""
        meta = self._metadata.peek(rfq.market_ticker)
        grid = meta.grid if meta is not None else None
        if grid is None:
            return 0
        step = grid.step_at(constructed.no_bid_cc)
        return 0 if step is None else int(step)

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

    def _feed_marginal(self, market_ticker: str) -> float | None:
        """The FEED-path marginal: the book's microprice, or None when the
        feed cannot price the leg — no book object, an invalid book, OR a
        valid book with no priceable two-sided top (``microprice()`` is None
        on an empty/one-sided book).

        THE single feed-readability predicate (relight3 root cause,
        2026-07-19): settled/closed markets can retain VALID-but-EMPTY mirrors
        in the feed, so "has a valid book object" and "the provider can read a
        price" are DIFFERENT tests. The registrar used the former while the
        provider effectively applied the latter — 9 exchange-finalized FRAENG
        legs were never registered (their husk books looked feed-owned) and
        the snapshot stayed unusable. Both ``_marginals`` and
        ``_register_settled_candidates`` now consume THIS helper, so the two
        can never diverge again: the feed serves a leg iff this returns a
        price; the settled machinery owns it iff this returns None."""
        try:
            book = self._feed.book(market_ticker)
        except KeyError:
            return None
        if not book.valid:
            return None
        return book.top().microprice()

    def _marginals(self, market_ticker: str) -> float | None:
        """The lifecycle's ONE marginal provider: the feed-READABLE microprice
        first (``_feed_marginal`` — the shared predicate); for a leg the feed
        cannot price (book gone, invalid, or a valid-but-EMPTY husk on a
        settled market), the permanently-cached exchange-GRADED settlement
        fact (0.0/1.0) second; else None (UNKNOWN, fail-closed — unchanged).
        The settled fallback is a pure in-memory cache read (hot-path safe);
        fetching happens on the maintenance tick.

        Feeding the graded 0/1 into the book/risk model makes every number
        CONDITIONAL on the settled facts — the correct book risk: a combo
        whose settled leg LOST samples a deterministically-dead parlay (zero
        further loss), one whose settled leg WON carries the full conditional
        exposure of its remaining legs."""
        p = self._feed_marginal(market_ticker)
        if p is not None:
            return p
        if self._settled is None:
            return None
        fact = self._settled.resolved(market_ticker)
        if fact is not None:
            return fact
        # Not resolved yet: register COMMITTED legs as fetch candidates (an
        # RFQ-only leg with no book stays a plain no-quote — never fetched).
        if market_ticker in self._committed_leg_tickers():
            self._settled.note_missing(market_ticker)
        return None

    def _settled_fact(self, market_ticker: str) -> float | None:
        """FIX 2 (2026-07-28). The EXCHANGE'S OWN graded outcome for this leg
        (exactly 0.0 or 1.0), or None when it has not determined one.

        THIS IS NOT ``_marginals``, and the difference is the whole point. The
        marginal provider walks the live FEED FIRST and only falls back to the
        graded fact, so it can legitimately return 0.0 or 1.0 for a market that
        is trading, pinned, and entirely unsettled. Resolving deterministic
        max-loss off that number would be INFERRING a settlement we never read —
        exactly what the det-max axis must never do. This reads the graded cache
        and nothing else.

        Pure in-memory (hot-path safe); the fetching happens on the maintenance
        tick. No resolver ⇒ None for every leg ⇒ nothing is ever resolved and
        det-max charges the whole book in full (fail-closed)."""
        if self._settled is None:
            return None
        return self._settled.resolved(market_ticker)

    def _det_max_settlement_aware(self) -> bool:
        """FIX 2 arming, read from the LIVE ``RiskLimits`` (which the nightly
        adaptive-cap swap may replace) so the book-risk MC, the candidate gate
        and the quote-time cap can never disagree about whether the settled
        credit is enforced. False ⇒ SHADOW: still measured, never subtracted."""
        return bool(self._limits.limits.det_max_settlement_aware)

    def _skew_settled_facts(self) -> SettledFactProvider | None:
        """The graded-fact provider for the SKEW'S concentration snapshot
        (settled-leg fact resolution, 2026-08-01), or None while the flag is
        off — the byte-identical default. Reuses ``_settled_fact`` (the det-max
        FIX 2 provider: graded cache ONLY, never the feed), so boot and
        intraday resolve through ONE path. PRICE-ONLY: this provider must never
        be passed to a limit-check / confirm-path snapshot — the caps keep
        seeing the whole committed book."""
        if self._skew_params is None or not self._skew_params.settled_fact_resolution:
            return None
        return self._settled_fact

    def _skew_facts_generation(self) -> int:
        """The cache-key element for skew settled-fact resolution: -1 while the
        flag is off (a CONSTANT — pre-existing cache behaviour is untouched),
        else the resolver's monotone fact count. Facts land at boot without a
        position-generation move, so any cache over resolved shares must key on
        this too (``_leg_axis_profile_from`` / ``_concentration_profile``)."""
        if self._skew_params is None or not self._skew_params.settled_fact_resolution:
            return -1
        if self._settled is None:
            return 0
        return self._settled.facts_generation

    def _committed_leg_tickers(self) -> frozenset[str]:
        """Distinct leg tickers of the COMMITTED positions, cached per position
        generation (fills/settlements are rare; the hot path only pays a tuple
        compare + set lookup)."""
        gen = self._exposure.position_generation
        cached = self._committed_leg_cache
        if cached is not None and cached[0] == gen:
            return cached[1]
        tickers = frozenset(
            leg.market_ticker
            for position in self._exposure.positions.values()
            for leg in position.legs
        )
        self._committed_leg_cache = (gen, tickers)
        return tickers

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
        """Full-RFQ size for the risk system. None = unresolvable = no-quote.

        TARGET-COST conversion — TWO FORMS (2026-07-25 big-fill audit):

        LEGACY (default): convert at the CHEAPEST quoted side's OWN bid. On a
        sell-only book this is the NO bid (~80¢ on longshots) — but the
        exchange awards contracts at the TAKER'S price for the side they buy
        (~1 − our bid), so the quote-time candidate was 3.6–4.7× SMALLER than
        the fill that arrived at confirm. Quote-time caps passed a phantom-
        small candidate, we WON the auction, and the confirm reservation
        (true size) correctly declined: tonight 49 auctions won → only 15
        filled, $355 premium won-then-reneged. The old "conservative ceiling"
        claim was true only for favorites.

        AWARD SIZING (``risk_qty_award_sizing``, arm-gated): contracts =
        target / (taker's price on the accepted side) = target / ($1 − our
        bid), worst (largest) across the sides we actually quote. Fees are
        deliberately EXCLUDED from the denominator: the taker pays price+fee
        per contract, so the fee only SHRINKS the awarded count — excluding
        it keeps this a strict UPPER bound on the exchange's award (~5% over
        on tonight's tape: $50 @ no_bid 0.8210 → 279.3 here vs 264.13
        awarded). A bid above 99¢ is unresolvable (the bound would invert)
        ⇒ None ⇒ no-quote. Size-layer coherence — quote-time admission never
        looser than confirm enforcement, closing the renege zone — holds when
        this is armed TOGETHER with ``release_accepted_quote_exposure``
        (2026-07-25 review: without the release, the accepted quote's own
        resting entry still double-counted the fill at confirm)."""
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
            if self._config.risk_qty_award_sizing:
                # Taker price for accepting side s = $1 − our bid on s; the
                # worst case (most contracts) is the side with the HIGHEST
                # bid. A bid above 99¢ would make the 1¢ floor an UNDER-
                # estimate of the award (the ceiling inverts — 2026-07-25
                # review): unresolvable ⇒ no-quote, never an understated
                # candidate (hard rule 6).
                denom = CC_PER_DOLLAR - max(bids)
                if denom < 100:
                    return None
            else:
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
            # Quote-time expected edge for EV-based slot eviction (2026-07-25):
            # refreshed on every reprice (a reprice re-builds this record).
            expected_edge_cc=self._quote_candidate_ev_cc(constructed, qty),
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
                deploy_scale=self.deploy_scale_for_check(),
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

    async def _delete_quote(
        self,
        quote_id: str,
        reason: ReasonCode,
        *,
        budget_s: float | None = _WITHDRAW_RESOLVE_BUDGET_S,
    ) -> None:
        """Withdraw ONE resting quote — a one-element call into THE withdrawal
        path. Only a PROVED withdrawal drops it.

        BOUNDED BY DEFAULT (2026-07-27 — the 07-27 emergency kill). This used to
        pass no ``budget_s`` at all, so it inherited ``_withdraw_and_reconcile``'s
        ``None`` = UNBOUNDED wait for write tokens. Every caller here is a
        NON-terminal, inline one — the reprice sweep's TTL / leg-stale /
        leg-moved deletions, the RFQ-gone handler, both risk evictions — and each
        runs on a loop that the supervisor judges for PROGRESS. A starved bucket
        turned any one of them into an unbounded await inside a loop whose own
        wall budget is only checked at the top of an iteration, which is how a
        maintenance tick ran 30.9 s against a 30.5 s bound and got a healthy bot
        killed. The default is now the EXISTING withdrawal-pass bound
        (``_WITHDRAW_RESOLVE_BUDGET_S`` = ``_REPRICE_SWEEP_BUDGET_S``), and the
        reprice sweep tightens it further to the budget it has actually got left.

        RESIDUAL RISK, ACCEPTED AND BOUNDED: a budget-deferred delete leaves the
        quote RESTING for up to one more 0.5 s maintenance tick, where it can
        fill. That is the exposure the batch path (``cancel_all``,
        ``cancel_quotes_touching``, the resolver's drain) already accepts, and it
        is contained the same way: last look re-judges the quote at confirm, the
        quote keeps its ``withdraw_pending_reason`` so risk keeps counting it in
        the mirror, and ``_resolve_withdraw_pending`` re-drives it on the very
        next tick. Weigh that against the alternative it replaces — a stalled
        maintenance loop, which withdraws NOTHING and kills the bot.

        ``budget_s=None`` (unbounded) is still available and is correct for a
        genuinely terminal caller; today only ``cancel_all``'s two terminal
        callers (``on_halt`` → ``_stop.set()``, and shutdown) use it, and they
        call ``cancel_all``, not this.

        B3 + the 2026-07-26 gate. This used to own a SECOND, UNMETERED
        ``self._sender.delete_quote`` call plus a private copy of the
        already-gone / unresolved handling. That copy was the busiest way to
        withdraw a quote (1,137 calls in the incident: 438 TTL + 1,104 RFQ-gone,
        on a run whose write path was 429ing) and it spent no write tokens at
        all, so the exchange's bucket never saw what we were emitting. It is
        gone: ``_withdraw_and_reconcile`` → ``_withdraw_batch._one`` is the only
        code in this module that may call the sender's delete, and it spends
        ``DELETE_QUOTE_TOKEN_COST`` BEFORE the request.

        The rule is therefore the batch rule, unconditionally:
          * ACK or 404 (``_already_gone``) ⇒ provably off the wire ⇒ dropped;
          * anything else ⇒ UNKNOWN ⇒ stays in the mirror (still counted by
            risk, still able to fill), marked withdraw-pending, and resolved by
            the next maintenance tick's open-quote READ — including for the
            event-driven reasons (RFQ-gone, eviction) whose triggers never
            repeat.

        The decision record stays HERE, on the single-quote path only, exactly
        as before: batch withdrawals (cancel-all, quarantine, the resolver's
        drain) have never written one, and adding N store writes to the halt path
        is the maintenance-stall class we just removed."""
        state = self._open.get(quote_id)
        gone, _failures = await self._withdraw_and_reconcile(
            [quote_id], reason, budget_s=budget_s
        )
        if gone and state is not None:
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

    async def _maybe_shadow_inplay(
        self, rfq: Rfq, reasons: list[ReasonCode]
    ) -> None:
        """IN-PLAY SHADOW INSTRUMENTATION (2026-07-25; measurement ONLY — no
        quote is ever sent, the skip already happened and is already recorded).

        Fires only when EVERY skip reason is in-play (``skip_inplay_leg``
        present, optionally accompanied by the close-time proxy
        ``skip_in_play`` — for MLB close = start+3h, so a game deep enough
        in-play trips both). Any OTHER reason (thin book, size, stale feed,
        halt, UNKNOWNs) means the RFQ would have been declined even with
        in-play quoting armed — pricing it would poison the measurement.

        THROUGHPUT ISOLATION (hard requirement — shadow work must never delay
        live pregame pricing). Bounded by the pool's own MEASURED state, not a
        hand-tuned sample rate (north star: a number a human must move is a
        bug):
        - queue-idle gate: prices only when the RFQ work queue depth reads 0
          (``attach_rfq_backlog_probe`` ← quote_app's ``rfq_work.qsize``). By
          construction there is zero queued live work to delay; an evening
          in-play burst — exactly when live pregame flow is also heaviest —
          suppresses the shadow entirely instead of competing with it.
        - single-flight: at most ONE shadow pricing in flight process-wide, so
          shadow occupancy is bounded at 1 of the 8 RFQ workers and 1 of the 8
          joint-pool slots even if a burst of in-play skips lands on many
          workers at once.
        - per-pricing bound: the shadow prices via ``_price_async``, so in live
          quote mode the joint runs off-loop under the SAME pool deadline as
          live pricing (a cold in-play joint can never wedge the event loop).
        Skipped samples are counted (``inplay_shadow.skipped_*``) and every
        in-play skip is on the decisions tape regardless, so measurement
        coverage (rows / eligible skips) stays computable — sampling density
        adapts to live load instead of a static 1-in-N.

        FAILURE ISOLATION: the entire body is exception-proof — any error logs
        ``inplay_shadow_errored`` and returns; the skip decision (already
        recorded by the caller) is untouched."""
        try:
            if not self._filter.inplay_shadow_enabled:
                return  # default OFF: byte-identical behaviour
            rs = set(reasons)
            if ReasonCode.SKIP_INPLAY_LEG not in rs or not rs <= _INPLAY_ONLY_SKIPS:
                return
            if rfq.rfq_id in self._inplay_shadow_done:
                return  # pending-retry re-skip: one recorded row per RFQ
            if self._inplay_shadow_inflight:
                self._metrics.inc("inplay_shadow.skipped_busy")
                return
            if self._rfq_backlog_depth is not None and self._rfq_backlog_depth() > 0:
                self._metrics.inc("inplay_shadow.skipped_backlog")
                return
            self._inplay_shadow_inflight = True
            try:
                await self._shadow_price_inplay(rfq, reasons)
            finally:
                self._inplay_shadow_inflight = False
        except Exception:
            self._metrics.inc("inplay_shadow.errored")
            log.exception("inplay_shadow_errored", rfq_id=rfq.rfq_id)

    async def _shadow_price_inplay(
        self, rfq: Rfq, reasons: list[ReasonCode]
    ) -> None:
        """Price the in-play-skipped RFQ with the LIVE engine (in_play=True)
        and record the would-be quote to ``would_quotes_inplay``. Per-leg
        time-to-start comes from the SAME pregame ladder that produced the
        skip (negative = seconds INTO the game — the depth axis of the
        adverse-selection study); a leg with UNKNOWN start records null."""
        result = await self._price_async(rfq, force_in_play=True)
        if isinstance(result, NoQuote):
            # The engine itself declined (books moved, deadline, …): count it —
            # it is the "we could not even price this in-play" bucket.
            self._metrics.inc("inplay_shadow.noquote")
            return
        now = self._clock.now().astimezone(UTC)
        leg_tts: dict[str, float | None] = {}
        for leg in rfq.legs:
            start = self._filter.leg_start_time(leg.market_ticker)
            leg_tts[leg.market_ticker] = (
                None
                if start is None
                else round((start.astimezone(UTC) - now).total_seconds(), 1)
            )
        await self._store.record_would_quote_inplay(
            rfq.rfq_id,
            market_ticker=rfq.market_ticker,
            fair_cc=int(result.fair_cc),
            yes_bid_cc=int(result.yes_bid_cc),
            no_bid_cc=int(result.no_bid_cc),
            target_cost_cc=(
                None if rfq.target_cost_cc is None else int(rfq.target_cost_cc)
            ),
            contracts_centi=None if rfq.contracts is None else int(rfq.contracts),
            leg_time_to_start_s=leg_tts,
            context={
                "collection": rfq.mve_collection_ticker,
                "width_cc": int(result.total_width_cc),
                "skip_reasons": [str(r) for r in reasons],
            },
        )
        self._inplay_shadow_done[rfq.rfq_id] = None
        while len(self._inplay_shadow_done) > _INPLAY_SHADOW_DEDUPE_CAP:
            del self._inplay_shadow_done[next(iter(self._inplay_shadow_done))]
        self._metrics.inc("inplay_shadow.recorded")

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
            # 2026-07-25 operator directive: a decline's audit line must NAME the
            # bound that fired (the gate's detail string carries the specific
            # budget + post-book numbers) — DECLINE_CANDIDATE_RISK alone is opaque.
            detail=detail,
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
