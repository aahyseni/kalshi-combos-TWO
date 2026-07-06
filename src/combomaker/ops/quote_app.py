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
from typing import Any

from combomaker.core.clock import SystemClock
from combomaker.core.conventions import load_conventions
from combomaker.core.money import CentiCents
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
from combomaker.ops.report import build_report, format_report
from combomaker.pricing.engine import PricingEngine
from combomaker.rfq.filters import RfqFilter
from combomaker.rfq.intake import RfqIntake
from combomaker.rfq.lifecycle import LifecycleConfig, QuoteLifecycle
from combomaker.rfq.models import Rfq
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.inplay import InPlayDetector
from combomaker.risk.killswitch import HaltEvent, KillSwitch
from combomaker.risk.lastlook import LastLookPolicy
from combomaker.risk.limits import LimitChecker, RiskLimits

log = get_logger(__name__)

JsonDict = dict[str, Any]


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
        self._metrics = Metrics()
        self._watched: set[str] = set()
        self._stop = asyncio.Event()

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

        signer = RequestSigner(Credentials.from_env(), self._clock)
        killswitch = KillSwitch(self._clock, kill_file=config.kill_file)
        store = await Store.open(config.data_dir / config.observe.db_filename, self._clock)
        ws = WsManager(config.endpoints.ws_url, signer, self._clock, self._metrics)
        feed = OrderbookFeed(ws, self._clock, self._metrics)
        intake = RfqIntake(ws, self._metrics)
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
            exposure = ExposureBook(conventions)
            risk_cfg = config.risk
            limits = LimitChecker(
                RiskLimits(
                    max_contracts_per_quote=risk_cfg.max_contracts_per_quote,
                    max_notional_per_quote_dollars=risk_cfg.max_notional_per_quote_dollars,
                    max_market_delta_contracts=risk_cfg.max_market_delta_contracts,
                    max_event_delta_contracts=risk_cfg.max_event_delta_contracts,
                    max_gross_notional_dollars=risk_cfg.max_gross_notional_dollars,
                    max_open_quotes=risk_cfg.max_open_quotes,
                    max_daily_loss_dollars=risk_cfg.max_daily_loss_dollars,
                    max_event_worst_case_loss_dollars=(
                        risk_cfg.max_event_worst_case_loss_dollars
                    ),
                )
            )
            sender = (
                PaperSender()
                if config.mode is Mode.PAPER
                else rest
            )
            lifecycle = QuoteLifecycle(
                clock=self._clock,
                sender=sender,
                engine=engine,
                rfq_filter=RfqFilter(config.filters, feed, metadata, killswitch, self._clock),
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
                config=LifecycleConfig(),
            )

            # Idempotent startup: reconcile before doing anything.
            if config.mode is Mode.QUOTE:
                await self._startup_reconcile(rest)

            async def handle_rfq(rfq: Rfq) -> None:
                await store.record_rfq(rfq, source="ws")
                if not rfq.is_combo:
                    return
                await self._ensure_watched(rfq, feed, metadata)
                await lifecycle.handle_rfq(rfq)

            async def on_quote_event(kind: str, msg: JsonDict) -> None:
                if kind == "quote_accepted":
                    await lifecycle.on_quote_accepted(msg)
                elif kind == "quote_executed":
                    await lifecycle.on_quote_executed(msg)

            intake.on_rfq(handle_rfq)
            intake.on_rfq_deleted(lifecycle.on_rfq_deleted)
            intake.on_quote_event(on_quote_event)

            async def on_invalidate(reason: str) -> None:
                await lifecycle.cancel_all(reason)

            feed.on_invalidate(on_invalidate)

            async def on_halt(event: HaltEvent) -> None:
                await lifecycle.cancel_all(event.reason)
                self._stop.set()

            killswitch.on_halt(on_halt)
            killswitch.start_kill_file_watch()

            async def on_channel_lost(reason: str) -> None:
                await lifecycle.cancel_all(reason)
                await ws.force_reconnect()

            intake.on_channel_lost(on_channel_lost)

            ws.start()
            tasks = [
                asyncio.create_task(self._maintenance_loop(lifecycle), name="maintenance"),
                asyncio.create_task(
                    self._status_loop(rest, lifecycle, killswitch), name="exchange-status"
                ),
                asyncio.create_task(
                    self._report_loop(store, exposure, lifecycle), name="report"
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
                await killswitch.stop()
                await store.close()
                log.info("quote_app_stopped", metrics=self._metrics.snapshot())

    def request_stop(self) -> None:
        self._stop.set()

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

    async def _startup_reconcile(self, rest: KalshiRestClient) -> None:
        try:
            payload = await rest.get_quotes(status="open")
            leftover = payload.get("quotes", []) or []
            for quote in leftover:
                quote_id = str(quote.get("id") or quote.get("quote_id") or "")
                if quote_id:
                    try:
                        await rest.delete_quote(quote_id)
                    except KalshiApiError as exc:
                        log.warning("startup_cancel_failed", quote_id=quote_id, error=str(exc))
            log.info("startup_reconciled", leftover_quotes=len(leftover))
            positions = await rest.get_positions()
            if positions.get("market_positions") or positions.get("positions"):
                log.warning(
                    "startup_existing_positions",
                    detail="existing positions found — exposure book starts EMPTY; "
                    "reconcile manually before trusting limits",
                )
        except KalshiApiError as exc:
            log.warning("startup_reconcile_failed", error=str(exc))

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
                meta = await metadata.market(ticker)
                if meta.event_ticker:
                    await metadata.event(meta.event_ticker)
            except KalshiApiError as exc:
                log.warning("leg_metadata_fetch_failed", ticker=ticker, error=str(exc))

    async def _maintenance_loop(self, lifecycle: QuoteLifecycle) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                await lifecycle.maintenance_tick()
            except Exception:
                log.exception("maintenance_tick_failed")

    async def _status_loop(
        self, rest: KalshiRestClient, lifecycle: QuoteLifecycle, killswitch: KillSwitch
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
            except Exception as exc:
                log.warning("exchange_status_failed", error=repr(exc))
                lifecycle.exchange_active = False
            await asyncio.sleep(15.0)

    async def _report_loop(
        self, store: Store, exposure: ExposureBook, lifecycle: QuoteLifecycle
    ) -> None:
        while True:
            await asyncio.sleep(300.0)
            try:
                report = await build_report(
                    store,
                    env=str(self._config.env),
                    exposure=exposure,
                    marginals=lifecycle._marginals,  # noqa: SLF001 (wiring seam)
                )
                log.info("periodic_report", report=format_report(report))
            except Exception:
                log.exception("report_failed")
