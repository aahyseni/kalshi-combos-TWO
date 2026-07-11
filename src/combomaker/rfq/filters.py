"""Quote/no-quote filters. Pure in-memory checks; every rejection has a reason.

Returns ALL failing reasons, not just the first — skipped RFQs are free data
about the flow, and knowing that an RFQ failed on both size and staleness is
worth more than knowing it failed at all. UNKNOWN classifications (unparseable
sides, missing metadata, unknown close time) are explicit rejections, never
defaults (quiet-failure defense #2).
"""

from __future__ import annotations

from datetime import UTC

from combomaker.core.clock import Clock
from combomaker.core.quantity import CENTI_PER_CONTRACT
from combomaker.core.reasons import ReasonCode
from combomaker.marketdata.feed import OrderbookFeed
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.config import FiltersConfig
from combomaker.rfq.models import Rfq
from combomaker.rfq.pregame import ComboStartStatus, PregameGate
from combomaker.risk.killswitch import KillSwitch

# Two-legged-tie European knockouts (Champions/Europa/Conference League): the
# "advance" market is decided over TWO legs, so a single-match win does not imply
# advancing and the single-match soccer priors mis-apply. Declined as an
# unmodeled regime (config.decline_two_legged_tie) until its own model is built.
_TWO_LEGGED_TIE_PREFIXES = ("KXUCL", "KXUEL", "KXUECL")


def _is_two_legged_tie_leg(ticker: str) -> bool:
    return ticker.upper().startswith(_TWO_LEGGED_TIE_PREFIXES)


class RfqFilter:
    def __init__(
        self,
        config: FiltersConfig,
        feed: OrderbookFeed,
        metadata: MetadataCache,
        killswitch: KillSwitch,
        clock: Clock,
    ) -> None:
        self._config = config
        self._feed = feed
        self._metadata = metadata
        self._killswitch = killswitch
        self._clock = clock
        self._pregame = PregameGate(config, metadata, clock)

    def evaluate(self, rfq: Rfq) -> list[ReasonCode]:
        """Empty list = quotable. Uses only in-memory state (hot-path safe)."""
        reasons: list[ReasonCode] = []
        cfg = self._config

        if self._killswitch.halted:
            reasons.append(ReasonCode.SKIP_HALTED)

        if cfg.combos_only and not rfq.is_combo:
            reasons.append(ReasonCode.SKIP_NOT_WHITELISTED)
        elif cfg.collection_whitelist:
            collection = rfq.mve_collection_ticker or ""
            if not any(collection.startswith(prefix) for prefix in cfg.collection_whitelist):
                reasons.append(ReasonCode.SKIP_NOT_WHITELISTED)

        n_legs = len(rfq.legs)
        if rfq.is_combo and not (cfg.min_legs <= n_legs <= cfg.max_legs):
            reasons.append(ReasonCode.SKIP_TOO_MANY_LEGS)

        if cfg.decline_two_legged_tie and any(
            _is_two_legged_tie_leg(leg.market_ticker) for leg in rfq.legs
        ):
            reasons.append(ReasonCode.SKIP_UNMODELED_REGIME)

        reasons.extend(self._size_reasons(rfq))

        if not rfq.all_leg_sides_known:
            reasons.append(ReasonCode.SKIP_CLASSIFIER_UNKNOWN)

        if not self._feed.feed_healthy:
            reasons.append(ReasonCode.SKIP_WS_UNHEALTHY)

        reasons.extend(self._leg_book_reasons(rfq))
        reasons.extend(self._timing_reasons(rfq))
        reasons.extend(self._pregame_reasons(rfq))
        return reasons

    def pregame_status(self, rfq: Rfq) -> ComboStartStatus:
        """Schedule-based start gate (Phase 3), also re-checked by last look
        at confirm time — a leg can go in-play between quote and accept."""
        return self._pregame.status(rfq.legs)

    def _pregame_reasons(self, rfq: Rfq) -> list[ReasonCode]:
        """Pregame-only gate: any started leg ⇒ skip; any UNKNOWN start ⇒
        skip (fail-closed). Stands down only via config.allow_inplay_legs."""
        status = self.pregame_status(rfq)
        reasons: list[ReasonCode] = []
        if status.any_started:
            reasons.append(ReasonCode.SKIP_INPLAY_LEG)
        if status.any_unknown:
            reasons.append(ReasonCode.SKIP_START_TIME_UNKNOWN)
        return reasons

    def _size_reasons(self, rfq: Rfq) -> list[ReasonCode]:
        cfg = self._config
        if rfq.contracts is not None:
            contracts = rfq.contracts / CENTI_PER_CONTRACT
            if contracts < cfg.min_contracts:
                return [ReasonCode.SKIP_SIZE_BELOW_MIN]
            if contracts > cfg.max_contracts:
                return [ReasonCode.SKIP_SIZE_ABOVE_MAX]
        elif rfq.target_cost_cc is not None:
            dollars = rfq.target_cost_cc / 10_000
            if dollars < cfg.min_target_cost_dollars:
                return [ReasonCode.SKIP_SIZE_BELOW_MIN]
            if dollars > cfg.max_target_cost_dollars:
                return [ReasonCode.SKIP_SIZE_ABOVE_MAX]
        else:
            # No recognizable sizing mode at all: UNKNOWN, not "assume small".
            return [ReasonCode.SKIP_CLASSIFIER_UNKNOWN]
        return []

    def _leg_book_reasons(self, rfq: Rfq) -> list[ReasonCode]:
        cfg = self._config
        reasons: list[ReasonCode] = []
        min_depth_centi = int(cfg.min_leg_depth_contracts * CENTI_PER_CONTRACT)
        for leg in rfq.legs:
            try:
                book = self._feed.book(leg.market_ticker)
            except KeyError:
                reasons.append(ReasonCode.SKIP_LEG_UNKNOWN)
                continue
            if not book.valid:
                reasons.append(ReasonCode.SKIP_LEG_STALE)
                continue
            top = book.top()
            if top.spread_cc is None:
                reasons.append(ReasonCode.SKIP_LEG_BOOK_THIN)
                continue
            if top.spread_cc > cfg.max_leg_spread_cc:
                reasons.append(ReasonCode.SKIP_LEG_SPREAD_TOO_WIDE)
            if (top.yes_bid_qty or 0) < min_depth_centi or (
                top.no_bid_qty or 0
            ) < min_depth_centi:
                reasons.append(ReasonCode.SKIP_LEG_BOOK_THIN)
        return reasons

    def _timing_reasons(self, rfq: Rfq) -> list[ReasonCode]:
        """Pregame gate via each leg's close time (proxy for event start).

        Missing metadata or missing close time is UNKNOWN ⇒ skip. A leg past
        its gate is treated as in-play.
        """
        reasons: list[ReasonCode] = []
        now = self._clock.now().astimezone(UTC)
        for leg in rfq.legs:
            meta = self._metadata.peek(leg.market_ticker)
            if meta is None:
                reasons.append(ReasonCode.SKIP_LEG_UNKNOWN)
                continue
            close = meta.close_time or meta.expected_expiration_time
            if close is None:
                reasons.append(ReasonCode.SKIP_CLASSIFIER_UNKNOWN)
                continue
            if (close.astimezone(UTC) - now).total_seconds() < self._config.min_time_to_close_s:
                reasons.append(ReasonCode.SKIP_IN_PLAY)
        return reasons
