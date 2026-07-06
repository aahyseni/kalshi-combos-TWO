"""SportsGameOdds adapter tests: parsing, devig-in-adapter, cache discipline,
mapping UNKNOWN behavior, poller budget, and engine blending."""

from __future__ import annotations

from typing import Any

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import DOC_ASSUMED
from combomaker.core.reasons import ReasonCode
from combomaker.ops.config import PricingConfig
from combomaker.pricing.devig import DevigMethod, devig
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import ConstructedQuote, NoQuote
from combomaker.pricing.sources.sportsgameodds import (
    MappedLeg,
    SgoParseError,
    SgoPoller,
    SportsGameOddsSource,
    StaticMarketMapping,
    implied_from_american,
    marginal_from_event_odds,
    opposing_odd_id,
)
from tests.test_pricing_engine import CROSS_EVENT_LEGS, combo, engine_harness

JsonDict = dict[str, Any]


class TestAmericanOdds:
    @pytest.mark.parametrize(
        ("odds", "implied"),
        [("-110", 110 / 210), ("+150", 100 / 250), ("100", 0.5), ("-100", 0.5)],
    )
    def test_implied(self, odds: str, implied: float) -> None:
        assert implied_from_american(odds) == pytest.approx(implied)

    def test_garbage_and_zero_rejected(self) -> None:
        with pytest.raises(SgoParseError):
            implied_from_american("evens")
        with pytest.raises(SgoParseError):
            implied_from_american("0")


class TestOpposingOddId:
    def test_moneyline_flip(self) -> None:
        assert opposing_odd_id("points-home-game-ml-home") == "points-away-game-ml-away"
        assert opposing_odd_id("points-away-game-ml-away") == "points-home-game-ml-home"

    def test_over_under_flip_keeps_entity(self) -> None:
        assert opposing_odd_id("points-all-game-ou-over") == "points-all-game-ou-under"

    def test_malformed_is_none(self) -> None:
        assert opposing_odd_id("weird") is None


EVENT_ODDS: JsonDict = {
    "points-home-game-ml-home": {
        "oddID": "points-home-game-ml-home",
        "bookOdds": "-125",
        "fairOdds": "-115",
    },
    "points-away-game-ml-away": {
        "oddID": "points-away-game-ml-away",
        "bookOdds": "+105",
        "fairOdds": "+115",
    },
}


class TestMarginalFromEvent:
    def test_devigs_book_odds_ourselves(self) -> None:
        result = marginal_from_event_odds(
            EVENT_ODDS,
            "points-home-game-ml-home",
            devig_method=DevigMethod.POWER,
            base_uncertainty=0.01,
        )
        assert result is not None
        p, uncertainty = result
        juiced = [implied_from_american("-125"), implied_from_american("+105")]
        expected = devig(juiced, DevigMethod.POWER)[0]
        assert p == pytest.approx(expected)
        # uncertainty = base + |ours − their fairOdds implied|
        their_fair = implied_from_american("-115")
        assert uncertainty == pytest.approx(0.01 + abs(p - their_fair))

    def test_missing_opposing_side_is_none(self) -> None:
        odds = {"points-home-game-ml-home": EVENT_ODDS["points-home-game-ml-home"]}
        assert (
            marginal_from_event_odds(
                odds,
                "points-home-game-ml-home",
                devig_method=DevigMethod.POWER,
                base_uncertainty=0.01,
            )
            is None
        )

    def test_unreadable_their_fair_adds_humility_not_crash(self) -> None:
        odds = {
            "points-home-game-ml-home": {"bookOdds": "-125", "fairOdds": "??"},
            "points-away-game-ml-away": {"bookOdds": "+105"},
        }
        result = marginal_from_event_odds(
            odds,
            "points-home-game-ml-home",
            devig_method=DevigMethod.POWER,
            base_uncertainty=0.01,
        )
        assert result is not None
        assert result[1] == pytest.approx(0.03)  # base + 0.02 penalty


def make_event(event_id: str = "ev1", *, started: bool = False) -> JsonDict:
    return {
        "eventID": event_id,
        "leagueID": "NBA",
        "status": {"startsAt": "2026-07-06T00:00:00Z", "started": started, "ended": False},
        "odds": dict(EVENT_ODDS),
    }


class TestSourceCache:
    def make(self) -> tuple[SportsGameOddsSource, FakeClock]:
        clock = FakeClock()
        mapping = StaticMarketMapping(
            {"KXNBA-M1": MappedLeg(event_id="ev1", odd_id="points-home-game-ml-home")}
        )
        return SportsGameOddsSource(mapping, clock, max_age_s=900), clock

    def test_ingest_then_marginal(self) -> None:
        source, _ = self.make()
        stored = source.ingest_events([make_event()])
        assert stored == 2  # both sides cached
        belief = source.marginal("KXNBA-M1")
        assert isinstance(belief, LegBelief)
        assert belief.source == "sportsgameodds"
        assert 0.5 < belief.p < 0.6

    def test_unmapped_ticker_is_none_never_guessed(self) -> None:
        source, _ = self.make()
        source.ingest_events([make_event()])
        assert source.marginal("KXNBA-UNMAPPED") is None

    def test_stale_cache_expires(self) -> None:
        source, clock = self.make()
        source.ingest_events([make_event()])
        clock.advance(901)
        assert source.marginal("KXNBA-M1") is None

    def test_in_play_events_skipped(self) -> None:
        source, _ = self.make()
        assert source.ingest_events([make_event(started=True)]) == 0


class FakeSgoClient:
    def __init__(self, events: list[JsonDict]) -> None:
        self.events = events
        self.calls: list[str] = []

    async def get_events(
        self, *, league_id: str, odds_available: bool = True, limit: int = 25
    ) -> list[JsonDict]:
        self.calls.append(league_id)
        return self.events[:limit]


class TestPoller:
    async def test_poll_once_ingests_and_counts_budget(self) -> None:
        clock = FakeClock()
        source = SportsGameOddsSource(StaticMarketMapping({}), clock)
        client = FakeSgoClient([make_event("ev1"), make_event("ev2")])
        poller = SgoPoller(
            client,  # type: ignore[arg-type]
            source,
            leagues=["NBA", "NFL"],
            max_events_per_league=2,
        )
        stored = await poller.poll_once()
        assert client.calls == ["NBA", "NFL"]
        assert poller.objects_fetched == 4
        assert stored == 8  # 2 events × 2 sides × 2 leagues

    def test_interval_floor_enforced(self) -> None:
        clock = FakeClock()
        source = SportsGameOddsSource(StaticMarketMapping({}), clock)
        poller = SgoPoller(
            FakeSgoClient([]),  # type: ignore[arg-type]
            source,
            leagues=["NBA"],
            poll_interval_s=1.0,  # silly-fast: must be clamped
        )
        assert poller._interval_s >= SgoPoller.MIN_INTERVAL_S  # noqa: SLF001


class FixedSource:
    def __init__(self, p: float, name: str = "fixed") -> None:
        self._p = p
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def marginal(self, market_ticker: str) -> LegBelief | None:
        return LegBelief(p=self._p, uncertainty=0.01, source=self._name)


class TestEngineBlending:
    async def test_agreeing_external_source_blends_in(self) -> None:
        engine, h = await engine_harness()
        base = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=1e6)
        assert isinstance(base, ConstructedQuote)
        # external source agreeing with the book mid (~0.485 for these books)
        blended_engine = PricingEngine(
            h.feed,
            h.metadata,
            DOC_ASSUMED,
            PricingConfig(),
            extra_sources=[(FixedSource(0.49), 0.5)],
        )
        result = blended_engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=1e6)
        assert isinstance(result, ConstructedQuote)

    async def test_disagreeing_source_is_no_quote(self) -> None:
        _, h = await engine_harness()
        engine = PricingEngine(
            h.feed,
            h.metadata,
            DOC_ASSUMED,
            PricingConfig(),
            extra_sources=[(FixedSource(0.95), 0.5)],  # wildly off the book
        )
        result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=1e6)
        assert isinstance(result, NoQuote)
        assert result.reason == ReasonCode.SKIP_SOURCES_DISAGREE

    async def test_source_returning_none_falls_back_to_book_alone(self) -> None:
        class NoneSource:
            @property
            def name(self) -> str:
                return "empty"

            def marginal(self, market_ticker: str) -> LegBelief | None:
                return None

        _, h = await engine_harness()
        engine = PricingEngine(
            h.feed,
            h.metadata,
            DOC_ASSUMED,
            PricingConfig(),
            extra_sources=[(NoneSource(), 0.5)],
        )
        result = engine.price(combo(CROSS_EVENT_LEGS), time_to_close_s=1e6)
        assert isinstance(result, ConstructedQuote)
