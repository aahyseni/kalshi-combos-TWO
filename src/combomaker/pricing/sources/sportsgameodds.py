"""SportsGameOdds adapter: budget-aware poller → cache → OddsSource.

Free tier is 2,500 objects/MONTH (docs/api-notes/sportsgameodds.md), so this
is a slow background poller over configured leagues — never a hot-path fetch.
``marginal()`` is sync and reads only the in-memory cache; stale or unmapped
entries yield None and pricing silently falls back to the Kalshi book alone.

Devig happens HERE (the quarantine-sanctioned location): we devig the juiced
two-sided ``bookOdds`` pair with our own configured method, and use the
distance to SGO's opaque ``fairOdds`` as an honesty term in the belief's
uncertainty.

Mapping discipline: a Kalshi market ticker resolves to (eventID, oddID) only
through an EXPLICIT mapping table. Unmapped ⇒ None — never a fuzzy team-name
guess (quiet-failure defense #2). Automated mapping is Phase 6 work.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from combomaker.core.clock import Clock
from combomaker.ops.logging import get_logger
from combomaker.pricing.devig import DevigMethod, devig
from combomaker.pricing.legs import LegBelief

log = get_logger(__name__)

JsonDict = dict[str, Any]

BASE_URL = "https://api.sportsgameodds.com/v2"

_OPPOSITE_SIDE = {"home": "away", "away": "home", "over": "under", "under": "over"}


class SgoParseError(ValueError):
    pass


def implied_from_american(odds_str: str) -> float:
    """American odds string → implied probability (juiced)."""
    text = odds_str.strip().replace("−", "-")  # tolerate unicode minus
    try:
        value = int(text)
    except ValueError as exc:
        raise SgoParseError(f"unparseable american odds: {odds_str!r}") from exc
    if value == 0:
        raise SgoParseError("american odds of 0 are meaningless")
    if value < 0:
        return -value / (-value + 100.0)
    return 100.0 / (value + 100.0)


def opposing_odd_id(odd_id: str) -> str | None:
    """points-home-game-ml-home → points-away-game-ml-away (entity+side flip)."""
    parts = odd_id.split("-")
    if len(parts) != 5:
        return None
    stat, entity, period, bet_type, side = parts
    flipped_side = _OPPOSITE_SIDE.get(side)
    if flipped_side is None:
        return None
    flipped_entity = _OPPOSITE_SIDE.get(entity, entity)  # ou stats keep entity
    return f"{stat}-{flipped_entity}-{period}-{bet_type}-{flipped_side}"


@dataclass(frozen=True, slots=True)
class MappedLeg:
    """Where a Kalshi market's YES side lives in SGO's odds space."""

    event_id: str
    odd_id: str  # the oddID whose success == Kalshi YES


class MarketMapping(Protocol):
    def lookup(self, kalshi_market_ticker: str) -> MappedLeg | None: ...


class StaticMarketMapping:
    """Explicit ticker → (eventID, oddID) table; unmapped is None, never a guess."""

    def __init__(self, entries: dict[str, MappedLeg]) -> None:
        self._entries = dict(entries)

    def lookup(self, kalshi_market_ticker: str) -> MappedLeg | None:
        return self._entries.get(kalshi_market_ticker)


@dataclass(frozen=True, slots=True)
class CachedMarginal:
    p: float
    uncertainty: float
    fetched_mono_ns: int


class SgoClient:
    """Thin REST client. Every call spends monthly object budget — the poller
    is the only caller, and it counts."""

    def __init__(self, api_key: str, *, session: aiohttp.ClientSession | None = None) -> None:
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> SgoClient:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15.0)
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def get_events(
        self, *, league_id: str, odds_available: bool = True, limit: int = 25
    ) -> list[JsonDict]:
        if self._session is None:
            raise RuntimeError("client not started — use 'async with'")
        params = {
            "leagueID": league_id,
            "oddsAvailable": "true" if odds_available else "false",
            "limit": str(limit),
        }
        async with self._session.get(
            f"{BASE_URL}/events", params=params, headers={"x-api-key": self._api_key}
        ) as resp:
            payload: JsonDict = await resp.json()
            if resp.status == 429:
                log.warning("sgo_rate_limited", body=str(payload)[:200])
                return []
            if resp.status >= 400:
                log.warning("sgo_error", status=resp.status, body=str(payload)[:200])
                return []
        data = payload.get("data")
        return list(data) if isinstance(data, list) else []

    async def get_usage(self) -> JsonDict:
        if self._session is None:
            raise RuntimeError("client not started — use 'async with'")
        async with self._session.get(
            f"{BASE_URL}/account/usage", headers={"x-api-key": self._api_key}
        ) as resp:
            result: JsonDict = await resp.json()
            return result


def marginal_from_event_odds(
    odds: JsonDict, odd_id: str, *, devig_method: DevigMethod, base_uncertainty: float
) -> tuple[float, float] | None:
    """(p, uncertainty) for ``odd_id`` from an event's odds map, or None.

    p comes from OUR devig of the juiced two-sided bookOdds pair; the distance
    to SGO's own juice-removed fairOdds is added to the uncertainty — if we
    and they disagree about the fair, that disagreement is real width.
    """
    entry = odds.get(odd_id)
    opposing_id = opposing_odd_id(odd_id)
    opposing = odds.get(opposing_id) if opposing_id else None
    if not isinstance(entry, dict) or not isinstance(opposing, dict):
        return None
    try:
        juiced_this = implied_from_american(str(entry["bookOdds"]))
        juiced_opp = implied_from_american(str(opposing["bookOdds"]))
    except (KeyError, SgoParseError):
        return None
    if not (0.0 < juiced_this < 1.0 and 0.0 < juiced_opp < 1.0):
        return None
    try:
        fair_pair = devig([juiced_this, juiced_opp], devig_method)
    except ValueError:
        return None
    p = fair_pair[0]

    uncertainty = base_uncertainty
    their_fair_raw = entry.get("fairOdds")
    if their_fair_raw is not None:
        try:
            uncertainty += abs(p - implied_from_american(str(their_fair_raw)))
        except SgoParseError:
            uncertainty += 0.02  # their fair unreadable: extra humility
    return p, uncertainty


class SportsGameOddsSource:
    """OddsSource over the poller's cache. Sync, in-memory, hot-path safe."""

    def __init__(
        self,
        mapping: MarketMapping,
        clock: Clock,
        *,
        max_age_s: float = 900.0,
    ) -> None:
        self._mapping = mapping
        self._clock = clock
        self._max_age_s = max_age_s
        self._cache: dict[tuple[str, str], CachedMarginal] = {}

    @property
    def name(self) -> str:
        return "sportsgameodds"

    # --- poller side ---

    def ingest_events(
        self,
        events: list[JsonDict],
        *,
        devig_method: DevigMethod = DevigMethod.POWER,
        base_uncertainty: float = 0.01,
    ) -> int:
        """Parse polled events into cached marginals; returns entries stored."""
        stored = 0
        now = self._clock.monotonic_ns()
        for event in events:
            event_id = str(event.get("eventID", ""))
            status = event.get("status") or {}
            if not event_id or status.get("started") or status.get("ended"):
                continue  # pregame only — in-play external odds are stale by definition
            odds = event.get("odds")
            if not isinstance(odds, dict):
                continue
            for odd_id in list(odds.keys()):
                result = marginal_from_event_odds(
                    odds, odd_id, devig_method=devig_method, base_uncertainty=base_uncertainty
                )
                if result is None:
                    continue
                p, uncertainty = result
                self._cache[(event_id, odd_id)] = CachedMarginal(
                    p=p, uncertainty=uncertainty, fetched_mono_ns=now
                )
                stored += 1
        return stored

    # --- OddsSource side ---

    def marginal(self, market_ticker: str) -> LegBelief | None:
        mapped = self._mapping.lookup(market_ticker)
        if mapped is None:
            return None  # unmapped: Kalshi book prices alone, no guessing
        cached = self._cache.get((mapped.event_id, mapped.odd_id))
        if cached is None:
            return None
        age_s = (self._clock.monotonic_ns() - cached.fetched_mono_ns) / 1e9
        if age_s > self._max_age_s:
            return None  # stale external data is worse than none
        return LegBelief(p=cached.p, uncertainty=cached.uncertainty, source=self.name)


class SgoPoller:
    """Slow background poll over configured leagues, hard-capped per cycle.

    Budget math: free tier is ~2,500 objects/month ≈ 80/day. With
    ``max_events_per_league`` × len(leagues) events per cycle, pick
    ``poll_interval_s`` so cycles/day × events/cycle stays under budget —
    the poller refuses to run faster than ``MIN_INTERVAL_S`` regardless.
    """

    MIN_INTERVAL_S = 600.0

    def __init__(
        self,
        client: SgoClient,
        source: SportsGameOddsSource,
        *,
        leagues: list[str],
        poll_interval_s: float = 3_600.0,
        max_events_per_league: int = 10,
        devig_method: DevigMethod | str = DevigMethod.POWER,
    ) -> None:
        # Accept the config string here so wiring code never imports devig
        # (the quarantine guard allows it only inside pricing/sources/).
        if isinstance(devig_method, str):
            devig_method = DevigMethod(devig_method)
        self._client = client
        self._source = source
        self._leagues = list(leagues)
        self._interval_s = max(poll_interval_s, self.MIN_INTERVAL_S)
        self._max_events = max_events_per_league
        self._devig_method = devig_method
        self.objects_fetched = 0

    async def poll_once(self) -> int:
        stored = 0
        for league in self._leagues:
            try:
                events = await self._client.get_events(
                    league_id=league, limit=self._max_events
                )
            except Exception as exc:
                log.warning("sgo_poll_failed", league=league, error=repr(exc))
                continue
            self.objects_fetched += len(events)
            stored += self._source.ingest_events(events, devig_method=self._devig_method)
        log.info(
            "sgo_polled",
            leagues=self._leagues,
            stored=stored,
            objects_fetched_total=self.objects_fetched,
        )
        return stored

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval_s)
