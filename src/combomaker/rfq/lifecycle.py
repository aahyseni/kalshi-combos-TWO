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
from combomaker.core.money import CC_PER_DOLLAR, CentiCents
from combomaker.core.quantity import CentiContracts, qty_from_fp_str
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.fees import FeeModel, FeeType, FeeUnknownError
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.models import Rfq
from combomaker.risk.balance import BalanceTracker
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, OpenQuoteRisk
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
    StartTimeProvider,
    StarvationWatchdog,
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
        reservation: RiskReservationService | None = None,
        skew_params: SkewParams | None = None,
        skew_limits: SkewLimits | None = None,
        skew_cache: GameSkewCache | None = None,
        widen_params: WidenPolicyParams | None = None,
        fee_model: FeeModel | None = None,
        fee_type: FeeType = FeeType.QUADRATIC,
        fee_multiplier: Fraction = Fraction(1),
    ) -> None:
        self._clock = clock
        self._sender = sender
        self._engine = engine
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
        SKIP_BANKROLL_UNAVAILABLE, shadow in Phase 2). Uses the NON-raising
        accessor so a stale poll never throws on the hot path."""
        if self._balance is None:
            return None
        got = self._balance.risk_bankroll_cc_or_none()
        return None if got is None else int(got)

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
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
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
        result = self._price(rfq)
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
            reskewed = self._price(rfq, inventory_skew_cc=applied_skew_cc)
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
            start_time_provider=self._start_time_provider,
            halt_inputs=self._halt_inputs(),
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
            return

        response = await self._sender.create_quote(
            rfq.rfq_id,
            yes_bid_cc=result.yes_bid_cc,
            no_bid_cc=result.no_bid_cc,
        )
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

    # ------------------------------------------------------- accept → confirm

    async def on_quote_accepted(self, msg: JsonDict) -> None:
        t0 = self._clock.monotonic_ns()
        quote_id = str(msg.get("quote_id", ""))
        state = self._open.get(quote_id)
        if state is None:
            log.warning("accept_for_unknown_quote", quote_id=quote_id)
            return
        state.accepted = True
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
        qty = self._accepted_qty(state, msg)
        if qty is None:
            # Unknown accepted size (defense #2): never confirm a fill we
            # cannot size — deliberate lapse.
            await self._record_confirm_decision(
                state, confirm=False, reason=ReasonCode.DECLINE_SIZE_UNKNOWN,
                detail=f"contracts_accepted_fp unreadable: {msg.get('contracts_accepted_fp')!r}",
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
        await self._store.record_fill(
            f"fill:{quote_id}",
            order_id=str(msg.get("order_id")) if msg.get("order_id") else None,
            combo_ticker=state.rfq.market_ticker,
            our_side=str(our_side),
            contracts_centi=int(qty),
            price_cc=int(bid),
            # Real fill fee from the fee model (defense #3): $0 for our combo maker
            # quadratic fills, correct for any nonzero-fee series. None only when no
            # fee model is wired (pre-Phase-6 behaviour) or the fee is UNKNOWN.
            fee_cc=self._fill_fee_cc(bid, qty),
            expected_edge_cc=expected_edge_cc,
            raw=msg,
        )
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
        if predicted_credit_cc != expected_revenue_cc:
            await self._killswitch.halt(
                ReasonCode.HALT_RECONCILIATION_MISMATCH,
                f"combo {combo_ticker}: predicted settlement credit "
                f"{predicted_credit_cc}cc != exchange revenue {expected_revenue_cc}cc "
                f"(V={settled_value}) — settlement model mismatch",
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

    async def maintenance_tick(self) -> None:
        """TTL expiry + reprice + P&L mark + daily-loss halt. Every few 100ms."""
        self._refresh_daily_pnl()
        if not self._killswitch.halted:
            breaches = self._partition_breaches(
                self._limits.check(
                    self._exposure,
                    self._marginals,
                    self.daily_pnl,
                    risk_bankroll_cc=self._risk_bankroll_cc(),
                    start_time_provider=self._start_time_provider,
                    halt_inputs=self._halt_inputs(),
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
        now = self._clock.monotonic_ns()
        for quote_id, state in list(self._open.items()):
            if state.accepted:
                continue
            age_s = (now - state.created_mono_ns) / 1e9
            if age_s > self._config.quote_ttl_s:
                await self._delete_quote(quote_id, ReasonCode.DELETE_TTL_EXPIRED)
                continue
            result = self._price(state.rfq)
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

    # ---------------------------------------------------------------- helpers

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

    def _accepted_qty(self, state: OpenQuoteState, msg: JsonDict) -> CentiContracts | None:
        """Accepted size; None = unknowable = deliberate lapse (defense #2).

        Missing ``contracts_accepted_fp`` on a contracts-mode RFQ falls back
        to the RFQ's own full size (a quote covers the full size by wire
        contract); on a target-cost RFQ there is nothing safe to assume.
        """
        raw = msg.get("contracts_accepted_fp")
        if raw is not None:
            try:
                return qty_from_fp_str(str(raw))
            except ValueError:
                return None
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
                start_time_provider=self._start_time_provider,
                halt_inputs=self._halt_inputs(),
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
