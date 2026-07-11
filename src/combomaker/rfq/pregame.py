"""Pregame-only quote gate (Phase 3; operator directive 2026-07-10).

We do not quote a combo if ANY leg's game has already started — ALL sports.
The gate is schedule-based (game start time), complementing the two existing
in-play defenses: the market-motion detector (``risk/inplay.py``, price/rate
anomalies) and the close-time proximity gate (``filters._timing_reasons``,
``min_time_to_close_s``). It ships ACTIVE; ``FiltersConfig.allow_inplay_legs``
re-enables in-play quoting later without code changes.

Start-time source chain per leg (fail-closed):

(a) VERIFIED embedded game-code start. KXMLB* tickers embed the scheduled
    start as ``<YY><MMM><DD><HHMM>`` in the game code, e.g.
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

(b) Estimate: earliest of the leg's ``close_time`` / ``expected_expiration_
    time`` minus a config offset (``pregame_start_offset_hours``, per-series
    overrides in ``pregame_start_offset_hours_by_prefix``). Same shape the
    backtests validated (expiry-minus-offset), but the LIVE-GATE default is
    4.5h, deliberately larger than the harnesses' soccer 2.5h: measured on
    real World Cup markets 2026-07-10, expected_expiration lands 2.95-3.95h
    after kickoff depending on series (game-end rounded up + settlement
    buffer), so 2.5h would admit up to ~1.5h of in-play — the estimate must
    sit on the maybe-started-means-decline side.

(c) No usable source ⇒ UNKNOWN ⇒ decline, with its own reason code — never
    a convenient default (quiet-failure defense #2).

Boundary: ``now >= start`` is STARTED (a quote at first pitch is in-play).
Peek-only metadata access — hot-path safe, no network ever.
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

    any_started: bool  # a leg's game has started (now >= start)
    any_unknown: bool  # a leg had no usable start-time source


class PregameGate:
    """Schedule-based pregame gate over a combo's legs.

    Used at quote time (RfqFilter.evaluate) and re-checked at last look
    (a leg can go in-play between quote and accept — straddle safety).
    """

    def __init__(self, config: FiltersConfig, metadata: MetadataCache, clock: Clock) -> None:
        self._config = config
        self._metadata = metadata
        self._clock = clock

    def status(self, legs: Sequence[RfqLeg]) -> ComboStartStatus:
        if self._config.allow_inplay_legs:
            # Operator re-enabled in-play quoting: the schedule gate stands
            # down entirely (motion detector + close-time gate stay active).
            return ComboStartStatus(any_started=False, any_unknown=False)
        now = self._clock.now().astimezone(UTC)
        started = False
        unknown = False
        for leg in legs:
            start = self.leg_start_time(leg.market_ticker)
            if start is None:
                unknown = True
            elif now >= start:
                started = True
        return ComboStartStatus(any_started=started, any_unknown=unknown)

    def leg_start_time(self, ticker: str) -> datetime | None:
        """Best available start time for a leg's game; None = UNKNOWN."""
        start = embedded_start_time(ticker)
        if start is not None:
            return start
        meta = self._metadata.peek(ticker)
        if meta is None:
            return None
        anchors = [
            t.astimezone(UTC)
            for t in (meta.close_time, meta.expected_expiration_time)
            if t is not None and t.tzinfo is not None  # naive time = no clock
        ]
        if not anchors:
            return None
        # Earliest anchor is the conservative pick: an earlier estimated start
        # can only decline sooner, never admit an in-play leg.
        return min(anchors) - timedelta(hours=self._offset_hours(ticker))

    def _offset_hours(self, ticker: str) -> float:
        t = ticker.upper()
        for prefix, hours in self._config.pregame_start_offset_hours_by_prefix.items():
            if t.startswith(prefix.upper()):
                return hours
        return self._config.pregame_start_offset_hours
