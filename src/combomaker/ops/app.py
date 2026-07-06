"""Observe-mode application: wire everything, log the flow, send nothing.

For every combo RFQ seen: persist it, lazily start mirroring its leg books,
run the filters, and record either the skip reasons or the independence
would-quote. A REST reconciliation loop backfills RFQs the seq-less
communications channel may have dropped.
"""

from __future__ import annotations

import asyncio
from typing import Any

from combomaker.core.clock import SystemClock
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.auth import Credentials, RequestSigner
from combomaker.exchange.rest import KalshiApiError, KalshiRestClient
from combomaker.exchange.ws import WsManager
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.config import AppConfig, Mode
from combomaker.ops.logging import configure_logging, get_logger
from combomaker.ops.metrics import Metrics
from combomaker.ops.persistence import Store
from combomaker.pricing.stub import independence_would_quote
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.intake import RfqIntake
from combomaker.rfq.models import Rfq, RfqParseError
from combomaker.risk.killswitch import HaltEvent, KillSwitch

log = get_logger(__name__)

JsonDict = dict[str, Any]


class ObserveApp:
    def __init__(self, config: AppConfig) -> None:
        if config.mode is not Mode.OBSERVE:
            raise ValueError("ObserveApp only runs in observe mode")
        config.assert_safe_to_run()
        self._config = config
        self._clock = SystemClock()
        self._metrics = Metrics()
        self._watched: set[str] = set()
        self._stop = asyncio.Event()

    async def run(self) -> None:
        config = self._config
        configure_logging(
            json_output=config.logging.json_output, level=config.logging.level
        )
        log.info("observe_starting", env=str(config.env))

        signer = RequestSigner(Credentials.for_env(str(config.env)), self._clock)
        killswitch = KillSwitch(self._clock, kill_file=config.kill_file)
        store = await Store.open(
            config.data_dir / config.observe.db_name_for(config.env), self._clock
        )
        ws = WsManager(config.endpoints.ws_url, signer, self._clock, self._metrics)
        feed = OrderbookFeed(ws, self._clock, self._metrics)
        intake = RfqIntake(ws, self._metrics)

        async with KalshiRestClient(config.endpoints.rest_base_url, signer) as rest:
            metadata = MetadataCache(rest, self._clock)
            rfq_filter = RfqFilter(
                config.filters, feed, metadata, killswitch, self._clock
            )

            async def handle_rfq(rfq: Rfq) -> None:
                await store.record_rfq(rfq, source="ws")
                log.info(
                    "rfq_seen",
                    rfq_id=rfq.rfq_id,
                    market=rfq.market_ticker,
                    collection=rfq.mve_collection_ticker,
                    legs=[f"{leg.side}:{leg.market_ticker}" for leg in rfq.legs],
                    contracts=rfq.contracts,
                    target_cost_cc=rfq.target_cost_cc,
                )
                if rfq.is_combo:
                    await self._ensure_watched(rfq, feed, metadata)
                reasons = rfq_filter.evaluate(rfq)
                if reasons:
                    self._metrics.inc("rfq.skipped")
                    await store.record_decision(
                        "no_quote",
                        rfq.rfq_id,
                        [str(r) for r in reasons],
                        {"collection": rfq.mve_collection_ticker},
                    )
                    log.info("rfq_skipped", rfq_id=rfq.rfq_id, reasons=[str(r) for r in reasons])
                    return
                would = independence_would_quote(
                    rfq, feed, width_cc=config.observe.would_quote_width_cc
                )
                if would is None:
                    await store.record_decision(
                        "no_quote",
                        rfq.rfq_id,
                        [str(ReasonCode.SKIP_PRICING_FAILED)],
                        {},
                    )
                    return
                self._metrics.inc("rfq.would_quote")
                await store.record_would_quote(
                    rfq.rfq_id,
                    fair_prob=would.fair_prob,
                    fair_cc=int(would.fair_cc),
                    width_cc=would.width_cc,
                    leg_probs=would.leg_probs,
                    context={"collection": rfq.mve_collection_ticker},
                )
                # Shadow markouts (defense #5 / Phase 6): how does the raw mid
                # product drift AFTER we would have quoted? Sustained adverse
                # drift = this flow would have picked us off.
                self._track_would_markout(rfq, store, feed, int(would.fair_cc))
                log.info(
                    "rfq_would_quote",
                    rfq_id=rfq.rfq_id,
                    fair_prob=round(would.fair_prob, 4),
                    fair_cc=int(would.fair_cc),
                    width_cc=would.width_cc,
                )

            async def handle_rfq_deleted(rfq_id: str, raw: JsonDict) -> None:
                await store.record_rfq_deleted(rfq_id, raw)

            async def handle_channel_lost(reason: str) -> None:
                log.error("communications_lost_forcing_reconnect", reason=reason)
                await ws.force_reconnect()

            intake.on_rfq(handle_rfq)
            intake.on_rfq_deleted(handle_rfq_deleted)
            intake.on_channel_lost(handle_channel_lost)

            killswitch.start_kill_file_watch()

            async def on_halt(event: HaltEvent) -> None:
                self._stop.set()

            killswitch.on_halt(on_halt)

            ws.start()
            poll_task = asyncio.create_task(
                self._reconcile_loop(rest, intake), name="rfq-reconcile"
            )
            report_task = asyncio.create_task(self._report_loop(), name="metrics-report")
            try:
                await self._stop.wait()
            finally:
                poll_task.cancel()
                report_task.cancel()
                await ws.stop()
                await killswitch.stop()
                await store.close()
                log.info("observe_stopped", metrics=self._metrics.snapshot())

    def request_stop(self) -> None:
        self._stop.set()

    def _track_would_markout(
        self, rfq: Rfq, store: Store, feed: OrderbookFeed, fair_cc: int
    ) -> None:
        from combomaker.risk.markouts import MarkoutSubject, MarkoutTracker

        if not hasattr(self, "_markouts"):
            self._markouts = MarkoutTracker(store.record_markout)

        def provider() -> tuple[int | None, int | None]:
            would = independence_would_quote(
                rfq, feed, width_cc=self._config.observe.would_quote_width_cc
            )
            now = int(would.fair_cc) if would else None
            return now, now  # observe-mode fair IS the raw mid product

        self._markouts.track(
            MarkoutSubject(
                fill_ref=f"would:{rfq.rfq_id}",
                fair_at_event_cc=fair_cc,
                raw_mid_at_event_cc=fair_cc,
            ),
            provider,
        )

    async def _ensure_watched(
        self, rfq: Rfq, feed: OrderbookFeed, metadata: MetadataCache
    ) -> None:
        new = [t for t in rfq.leg_tickers if t not in self._watched]
        if not new:
            return
        self._watched.update(new)
        feed.watch(new)
        for ticker in new:
            try:
                await metadata.market(ticker)
            except KalshiApiError as exc:
                log.warning("leg_metadata_fetch_failed", ticker=ticker, error=str(exc))

    async def _reconcile_loop(self, rest: KalshiRestClient, intake: RfqIntake) -> None:
        """The communications channel has no seq — poll GetRFQs for completeness."""
        while True:
            await asyncio.sleep(self._config.observe.rfq_poll_s)
            try:
                payload = await rest.get_rfqs(status="open")
            except KalshiApiError as exc:
                log.warning("rfq_reconcile_failed", error=str(exc))
                continue
            except Exception as exc:
                log.warning("rfq_reconcile_error", error=repr(exc))
                continue
            for raw in payload.get("rfqs", []) or []:
                rfq_id = str(raw.get("id", ""))
                if not rfq_id or rfq_id in intake.open_rfqs:
                    continue
                try:
                    rfq = Rfq.from_ws(raw)
                except RfqParseError:
                    continue
                self._metrics.inc("rfq.ws_missed")
                log.warning("rfq_missed_by_ws", rfq_id=rfq_id)
                await intake.inject_rfq(rfq, source="rest_poll")

    async def _report_loop(self, interval_s: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            log.info("observe_metrics", **{"snapshot": self._metrics.snapshot()})
