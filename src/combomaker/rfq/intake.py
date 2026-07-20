"""RFQ intake from the communications WS channel.

The channel has NO seq numbers (docs/api-notes/communications-ws.md) — gap
detection is impossible on-stream, so completeness comes from periodic REST
reconciliation (the observe app owns that loop). Error codes 10/17/25 on the
channel are terminal ("must resubscribe"); 25 additionally means messages were
LOST because we read too slowly — both are surfaced via ``on_channel_lost`` so
the app can force a reconnect and count the incident.

Intake never crashes on a malformed message: parse failures are logged with
the raw payload, counted, and skipped — one weird RFQ must not stop the flow.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from combomaker.marketdata.feed import WsLike
from combomaker.ops.logging import get_logger
from combomaker.ops.metrics import Metrics
from combomaker.rfq.models import Rfq, RfqParseError

log = get_logger(__name__)

JsonDict = dict[str, Any]

_TERMINAL_WS_ERROR_CODES = {10, 17, 25}

RfqHandler = Callable[[Rfq], Awaitable[None]]
RfqDeletedHandler = Callable[[str, JsonDict], Awaitable[None]]
QuoteEventHandler = Callable[[str, JsonDict], Awaitable[None]]
ChannelLostHandler = Callable[[str], Awaitable[None]]


class RfqIntake:
    def __init__(
        self,
        ws: WsLike,
        metrics: Metrics | None = None,
        *,
        series_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        """``series_prefixes``: PRE-PARSE firehose gate (quote mode only). The
        communications channel is the WHOLE exchange's RFQ stream — measured
        ~600 msgs/s sustained (crypto RFQ bots) 2026-07-14 — and full
        ``Rfq.from_ws`` parsing (Decimal money per leg) caps consumption at
        ~80-100/s, which made us a genuine slow consumer (server closed the
        socket every ~90-150s; then the bounded dispatch queue overflowed every
        ~35s). An RFQ whose raw legs aren't ALL on these prefixes is dropped on
        a cheap string check BEFORE any parsing (metric only) — those would
        decline SKIP_SERIES_NOT_ALLOWED anyway. None (observe mode / default)
        keeps the record-everything behavior."""
        self._ws = ws
        self._metrics = metrics or Metrics()
        self._series_prefixes = series_prefixes
        self._on_rfq: list[RfqHandler] = []
        self._on_rfq_deleted: list[RfqDeletedHandler] = []
        self._on_quote_event: list[QuoteEventHandler] = []
        self._on_channel_lost: list[ChannelLostHandler] = []
        self.open_rfqs: dict[str, Rfq] = {}
        # F2 liveness fix (risk audit 2026-07-16): a comms-WS drop CLEARS
        # open_rfqs (no replay on reconnect), but absence-after-clear is
        # UNKNOWN, not positive deletion — the lifecycle's liveness probe must
        # keep treating those ids as alive, or every RFQ queued just before a
        # blip gets mislabeled skip_rfq_deleted_midflight (the REST POST does
        # not need the WS and could still win). Two GENERATIONS bound memory:
        # ids cleared by a disconnect stay "stale-alive" until the second
        # subsequent disconnect, far past the pipeline's ~2s queue/retry
        # budget for them. A positive rfq_deleted removes an id immediately.
        self._stale_open: tuple[set[str], set[str]] = (set(), set())

        ws.add_subscription(["communications"], on_subscribed=self._on_subscribed)
        ws.on_message("rfq_created", self._handle_rfq_created)
        ws.on_message("rfq_deleted", self._handle_rfq_deleted)
        for quote_type in ("quote_created", "quote_accepted", "quote_executed"):
            ws.on_message(quote_type, self._make_quote_handler(quote_type))
        ws.on_message("error", self._handle_error)
        ws.on_disconnect(self._handle_disconnect)

    # --- registration ---

    def on_rfq(self, handler: RfqHandler) -> None:
        self._on_rfq.append(handler)

    def on_rfq_deleted(self, handler: RfqDeletedHandler) -> None:
        self._on_rfq_deleted.append(handler)

    def on_quote_event(self, handler: QuoteEventHandler) -> None:
        """Fires with (message_type, msg) for quote_created/accepted/executed."""
        self._on_quote_event.append(handler)

    def on_channel_lost(self, handler: ChannelLostHandler) -> None:
        self._on_channel_lost.append(handler)

    # --- liveness view (F2 mid-pipeline probe) ---

    def rfq_alive(self, rfq_id: str) -> bool:
        """Liveness answer for the lifecycle's F2 probe (wired by quote_app).

        True means alive OR unknown; False ONLY when the registry positively
        saw the RFQ die (``rfq_deleted``) — or never saw it at all (the probe
        is only ever asked about RFQs this intake fanned out). Ids cleared by
        a disconnect are UNKNOWN, not deleted, so they answer True until aged
        out of the stale generations (see ``_handle_disconnect``)."""
        if rfq_id in self.open_rfqs:
            return True
        return any(rfq_id in gen for gen in self._stale_open)

    # --- injection point for REST reconciliation ---

    async def inject_rfq(self, rfq: Rfq, *, source: str) -> None:
        """Feed an RFQ discovered outside the WS stream (reconciliation)."""
        if rfq.rfq_id in self.open_rfqs:
            return
        self._metrics.inc(f"rfq.injected.{source}")
        self.open_rfqs[rfq.rfq_id] = rfq
        await self._fan_out_rfq(rfq)

    # --- handlers ---

    async def _on_subscribed(self, sid: int) -> None:
        log.info("communications_subscribed", sid=sid)

    async def _handle_rfq_created(self, envelope: JsonDict) -> None:
        msg = envelope.get("msg", {})
        if self._series_prefixes is not None:
            # Firehose gate: raw string check BEFORE Rfq.from_ws (see __init__).
            legs = msg.get("mve_selected_legs") or []
            if not legs or any(
                not str(leg.get("market_ticker", "")).startswith(self._series_prefixes)
                for leg in legs
                if isinstance(leg, dict)
            ):
                self._metrics.inc("rfq.dropped_series_fastpath")
                return
        try:
            rfq = Rfq.from_ws(msg)
        except RfqParseError as exc:
            self._metrics.inc("rfq.parse_error")
            log.warning("rfq_unparseable", error=str(exc), raw=msg)
            return
        self._metrics.inc("rfq.created")
        if rfq.is_combo:
            self._metrics.inc("rfq.created_combo")
        self.open_rfqs[rfq.rfq_id] = rfq
        await self._fan_out_rfq(rfq)

    async def _handle_rfq_deleted(self, envelope: JsonDict) -> None:
        msg = envelope.get("msg", {})
        rfq_id = str(msg.get("id", ""))
        self._metrics.inc("rfq.deleted")
        self.open_rfqs.pop(rfq_id, None)
        # Positive deletion beats stale-UNKNOWN: a delete that arrives after a
        # disconnect cleared the registry must flip the liveness answer too.
        for gen in self._stale_open:
            gen.discard(rfq_id)
        for handler in self._on_rfq_deleted:
            try:
                await handler(rfq_id, msg)
            except Exception:
                log.exception("rfq_deleted_handler_failed", rfq_id=rfq_id)

    def _make_quote_handler(
        self, quote_type: str
    ) -> Callable[[JsonDict], Awaitable[None]]:
        async def handle(envelope: JsonDict) -> None:
            msg = envelope.get("msg", {})
            self._metrics.inc(f"quote_event.{quote_type}")
            for handler in self._on_quote_event:
                try:
                    await handler(quote_type, msg)
                except Exception:
                    log.exception("quote_event_handler_failed", quote_type=quote_type)

        return handle

    async def _handle_error(self, envelope: JsonDict) -> None:
        msg = envelope.get("msg", {})
        code = int(msg.get("code", 0))
        if code in _TERMINAL_WS_ERROR_CODES:
            reason = f"ws_terminal_error_{code}"
            self._metrics.inc(f"rfq.channel_lost.{code}")
            log.error("communications_channel_lost", code=code, detail=msg.get("msg"))
            for handler in self._on_channel_lost:
                try:
                    await handler(reason)
                except Exception:
                    log.exception("channel_lost_handler_failed")

    async def _handle_disconnect(self) -> None:
        # Open-RFQ registry is only as fresh as the stream; a reconnect gets
        # no replay, so REST reconciliation must rebuild it. Clearing is NOT
        # positive deletion: park the cleared ids in the stale generations so
        # ``rfq_alive`` keeps answering True (UNKNOWN ⇒ proceed-as-today) for
        # RFQs already in the pipeline; rotate out the oldest generation.
        self._metrics.inc("rfq.registry_reset")
        self._stale_open = (set(self.open_rfqs), self._stale_open[0])
        self.open_rfqs.clear()

    async def _fan_out_rfq(self, rfq: Rfq) -> None:
        for handler in self._on_rfq:
            try:
                await handler(rfq)
            except Exception:
                log.exception("rfq_handler_failed", rfq_id=rfq.rfq_id)
