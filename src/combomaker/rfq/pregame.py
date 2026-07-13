"""Pregame-only quote gate (Phase 3; operator directive 2026-07-10).

We do not quote a combo if ANY leg's game has already started — ALL sports.
The gate is schedule-based (game start time), complementing the two existing
in-play defenses: the market-motion detector (``risk/inplay.py``, price/rate
anomalies) and the close-time proximity gate (``filters._timing_reasons``,
``min_time_to_close_s``). It ships ACTIVE; ``FiltersConfig.allow_inplay_legs``
re-enables in-play quoting later without code changes.

Start-time source chain per leg (fail-closed, PRECISION LADDER, R3 §B1):

(a) VERIFIED embedded game-code start (PRECISE). KXMLB* tickers embed the
    scheduled start as ``<YY><MMM><DD><HHMM>`` in the game code, e.g.
    ``KXMLBGAME-26JUL101915BOSNYM-BOS`` = 2026-07-10 19:15. The timezone is
    US/Eastern — VERIFIED 2026-07-10 against the live prod API (public GET
    /markets/{ticker}, 18 markets across KXMLBGAME/HIT/KS/TB/RFI/TOTAL/SPREAD
    and ET/CT/PT venues, day + night games): ``expected_expiration_time``
    equals the token-as-Eastern plus EXACTLY 3.00h on every market, while the
    venue-local reading scatters 0-3h and the UTC reading implies impossible
    game facts (day games at 9-10am local). Evidence:
    docs/reports/2026-07-10-phase3-pregame-gate.md. Only series in
    ``_EMBEDDED_START_SERIES`` may use this path — an unverified family's
    digits are never trusted as a clock.

(a2) EXPLICIT schedule feed (PRECISE; Phase 5 seam). If the ``ScheduleCache``
    holds this leg's ``event_ticker`` (an explicit table, no fuzzy matching),
    use its exact scheduled UTC start. INACTIVE by default (empty table); a
    cache miss falls through to the estimate. This is the flow-recovery tier —
    an exact start lets the precision margins M_q/M_c replace the blunt 4.5h.

(b) Estimate (NOT precise): earliest of the leg's ``close_time`` /
    ``expected_expiration_time`` minus a config offset
    (``pregame_start_offset_hours``, per-series overrides in
    ``pregame_start_offset_hours_by_prefix``). Same shape the backtests
    validated (expiry-minus-offset), but the LIVE-GATE default is 4.5h,
    deliberately larger than the harnesses' soccer 2.5h: measured on real World
    Cup markets 2026-07-10, expected_expiration lands 2.95-3.95h after kickoff
    depending on series (game-end rounded up + settlement buffer), so 2.5h
    would admit up to ~1.5h of in-play — the estimate must sit on the
    maybe-started-means-decline side. The estimate ALREADY bakes in its buffer,
    so the M_q/M_c margins do NOT apply on top of it (they would double-count).

(c) No usable source ⇒ UNKNOWN ⇒ decline, with its own reason code — never
    a convenient default (quiet-failure defense #2).

Precision margins (R3 §B2, Phase 5 SEAM + conservative defaults): on a PRECISE
start (a/a2), a quote-cutoff margin ``M_q`` and a stricter confirm-cutoff margin
``M_c`` (``M_c >= M_q``) split the buffer — quote up to ``start − M_q``, confirm
up to ``start − M_c``. Both default 0s this phase (no live tightening without a
hard-rule-5-verified feed): the embedded-ET path keeps today's behaviour, the
schedule tier is inactive.

Boundary: ``now >= start`` is STARTED (a quote at first pitch is in-play).
Peek-only metadata + schedule access — hot-path safe, no network ever.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from combomaker.core.clock import Clock
from combomaker.marketdata.metadata import MetadataCache
from combomaker.ops.config import FiltersConfig
from combomaker.rfq.models import RfqLeg
from combomaker.rfq.schedule import ScheduleCache

# Series verified to embed the scheduled start (Eastern) in the game code.
# Extend ONLY with fresh API evidence recorded in docs/reports/ — an
# unverified family must fall through to the estimate/UNKNOWN chain.
_EMBEDDED_START_SERIES = ("KXMLB",)

_EASTERN = ZoneInfo("America/New_York")

# Game-code segment: 26JUL101915BOSNYM… — date, 4-digit start, then the
# alphabetic team blob (the lookahead pins the token as a TIME, not a line).
_EMBEDDED_START = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})(?=[A-Z])")

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def embedded_start_time(ticker: str) -> datetime | None:
    """Scheduled start from a VERIFIED embedded game code, or None.

    None means "this path has nothing to say" (unverified series, no time
    token, malformed digits) — callers fall through the chain, they never
    guess.
    """
    t = ticker.upper()
    if not t.startswith(_EMBEDDED_START_SERIES):
        return None
    parts = t.split("-")
    if len(parts) < 2:
        return None
    m = _EMBEDDED_START.match(parts[1])
    if m is None:
        return None
    yy, mon, dd, hh, mm = m.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    if not (int(hh) <= 23 and int(mm) <= 59):
        return None
    try:
        return datetime(2000 + int(yy), month, int(dd), int(hh), int(mm), tzinfo=_EASTERN)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class ComboStartStatus:
    """Aggregated start-time verdict over a combo's legs."""

    any_started: bool  # a leg's game has started (now >= start − margin)
    any_unknown: bool  # a leg had no usable start-time source


@dataclass(frozen=True, slots=True)
class LegStart:
    """A leg's resolved start + whether it came from a PRECISE source.

    ``precise`` True ⇒ the start came from the verified embedded-ET path or the
    explicit schedule feed, so the precision margins M_q/M_c apply. False ⇒ the
    conservative ``expiry − offset`` estimate, which already bakes in its buffer,
    so no additional margin applies (that would double-count)."""

    start: datetime
    precise: bool


class PregameGate:
    """Schedule-based pregame gate over a combo's legs.

    Used at quote time (RfqFilter.evaluate, margin ``M_q``) and re-checked at
    last look (a leg can go in-play between quote and accept — straddle safety —
    with the stricter margin ``M_c``).
    """

    def __init__(
        self,
        config: FiltersConfig,
        metadata: MetadataCache,
        clock: Clock,
        schedule: ScheduleCache | None = None,
    ) -> None:
        self._config = config
        self._metadata = metadata
        self._clock = clock
        # Explicit schedule feed (Phase 5 seam). INACTIVE by default: an empty
        # cache is a miss on every leg ⇒ the estimate path is unchanged.
        self._schedule = schedule or ScheduleCache()

    def status(self, legs: Sequence[RfqLeg]) -> ComboStartStatus:
        """Quote-time gate: a leg is STARTED if now >= start − M_q (precise
        start) or now >= estimate (estimate has its own padding)."""
        return self._status(legs, confirm=False)

    def confirm_status(self, legs: Sequence[RfqLeg]) -> ComboStartStatus:
        """Last-look gate: the STRICTER margin M_c (M_c >= M_q). Declines a
        confirm earlier than the quote-time gate would, keeping a hard safety
        buffer even when the quote side was tightened for flow (R3 §B2)."""
        return self._status(legs, confirm=True)

    def _status(self, legs: Sequence[RfqLeg], *, confirm: bool) -> ComboStartStatus:
        if self._config.allow_inplay_legs:
            # Operator re-enabled in-play quoting: the schedule gate stands
            # down entirely (motion detector + close-time gate stay active).
            return ComboStartStatus(any_started=False, any_unknown=False)
        now = self._clock.now().astimezone(UTC)
        started = False
        unknown = False
        for leg in legs:
            cutoff = self._cutoff_time(leg.market_ticker, confirm=confirm)
            if cutoff is None:
                unknown = True
            elif now >= cutoff:
                started = True
        return ComboStartStatus(any_started=started, any_unknown=unknown)

    def _cutoff_time(self, ticker: str, *, confirm: bool) -> datetime | None:
        """The clock instant at/after which this leg declines: ``start − margin``
        for a precise start, the bare estimate otherwise. None = UNKNOWN."""
        resolved = self.leg_start(ticker)
        if resolved is None:
            return None
        if not resolved.precise:
            return resolved.start  # estimate already includes its buffer
        margin_s = (
            self._confirm_margin_s(ticker) if confirm else self._quote_margin_s(ticker)
        )
        return resolved.start - timedelta(seconds=margin_s)

    def leg_start_time(self, ticker: str) -> datetime | None:
        """Best available RAW start time for a leg's game; None = UNKNOWN.

        Backward-compatible: returns the resolved start with NO margin applied
        (the R2 slate cap's StartTimeProvider consumes this — it wants the true
        start, not a margin-adjusted cutoff)."""
        resolved = self.leg_start(ticker)
        return resolved.start if resolved is not None else None

    def leg_start(self, ticker: str) -> LegStart | None:
        """Resolved start + precision flag via the ladder (embedded ET →
        explicit schedule feed → estimate). None = UNKNOWN."""
        embedded = embedded_start_time(ticker)
        if embedded is not None:
            return LegStart(start=embedded, precise=True)
        meta = self._metadata.peek(ticker)
        # Schedule feed keys on the event_ticker (explicit table). A leg with no
        # metadata still has no event key here, so the schedule can't match — the
        # estimate needs the metadata anchors anyway, so a missing meta = UNKNOWN.
        if meta is None:
            return None
        scheduled = self._schedule.peek_start(meta.event_ticker)
        if scheduled is not None:
            return LegStart(start=scheduled, precise=True)
        anchors = [
            t.astimezone(UTC)
            for t in (meta.close_time, meta.expected_expiration_time)
            if t is not None and t.tzinfo is not None  # naive time = no clock
        ]
        if not anchors:
            return None
        # Earliest anchor is the conservative pick: an earlier estimated start
        # can only decline sooner, never admit an in-play leg.
        estimate = min(anchors) - timedelta(hours=self._offset_hours(ticker))
        return LegStart(start=estimate, precise=False)

    def _offset_hours(self, ticker: str) -> float:
        t = ticker.upper()
        for prefix, hours in self._config.pregame_start_offset_hours_by_prefix.items():
            if t.startswith(prefix.upper()):
                return hours
        return self._config.pregame_start_offset_hours

    def _quote_margin_s(self, ticker: str) -> float:
        return _prefix_lookup(
            ticker,
            self._config.pregame_quote_margin_s_by_prefix,
            self._config.pregame_quote_margin_s,
        )

    def _confirm_margin_s(self, ticker: str) -> float:
        return _prefix_lookup(
            ticker,
            self._config.pregame_confirm_margin_s_by_prefix,
            self._config.pregame_confirm_margin_s,
        )


def _prefix_lookup(ticker: str, table: dict[str, float], default: float) -> float:
    t = ticker.upper()
    for prefix, value in table.items():
        if t.startswith(prefix.upper()):
            return value
    return default
