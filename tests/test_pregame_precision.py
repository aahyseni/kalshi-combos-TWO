"""Phase 5 (R3 Part B) — pregame precision ladder.

Covers the ScheduleCache seam (explicit table, fail-closed misses, tz-aware
insert guard), the schedule tier's position in the start-time ladder (between
embedded-ET and the estimate), and the M_q/M_c margin split (quote vs the
stricter confirm cutoff) — all SEAM + CONSERVATIVE DEFAULTS: the defaults keep
today's behaviour, the schedule feed is inactive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from combomaker.core.clock import FakeClock
from combomaker.marketdata.metadata import MarketMeta, MetadataCache
from combomaker.ops.config import FiltersConfig
from combomaker.rfq.models import RfqLeg
from combomaker.rfq.pregame import PregameGate
from combomaker.rfq.schedule import ScheduleCache

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def _clock() -> FakeClock:
    return FakeClock(start=NOW)


def _metadata(clock: FakeClock) -> MetadataCache:
    return MetadataCache(None, clock)  # type: ignore[arg-type]


def _seed_meta(
    meta: MetadataCache,
    clock: FakeClock,
    ticker: str,
    *,
    event_ticker: str = "KXWCGAME-26JUL13FRAMAR",
    close_in_s: float | None = 6 * 3600.0,
) -> None:
    close = NOW + timedelta(seconds=close_in_s) if close_in_s is not None else None
    meta._markets[ticker] = MarketMeta(  # noqa: SLF001 (test seam)
        ticker=ticker,
        status="active",
        grid=None,
        event_ticker=event_ticker,
        close_time=close,
        expected_expiration_time=None,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )


def _leg(ticker: str) -> RfqLeg:
    return RfqLeg(
        market_ticker=ticker,
        event_ticker=None,
        side="yes",
        yes_settlement_value_cc=None,
    )


# ---------------------------------------------------------------------------
# ScheduleCache in isolation.
# ---------------------------------------------------------------------------


class TestScheduleCache:
    def test_empty_is_a_miss(self) -> None:
        cache = ScheduleCache()
        assert cache.peek_start("KXWCGAME-26JUL13FRAMAR") is None
        assert cache.size == 0

    def test_explicit_hit(self) -> None:
        start = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)
        cache = ScheduleCache({"KXWCGAME-26JUL13FRAMAR": start})
        assert cache.peek_start("KXWCGAME-26JUL13FRAMAR") == start
        # No fuzzy matching — a different event never matches.
        assert cache.peek_start("KXWCGAME-26JUL13ARGGER") is None

    def test_none_event_is_a_miss(self) -> None:
        cache = ScheduleCache({"E": datetime(2026, 1, 1, tzinfo=UTC)})
        assert cache.peek_start(None) is None

    def test_naive_datetime_rejected_at_insert(self) -> None:
        with pytest.raises(ValueError, match="tz-aware"):
            ScheduleCache({"E": datetime(2026, 7, 13, 15, 0)})  # naive
        cache = ScheduleCache()
        with pytest.raises(ValueError, match="tz-aware"):
            cache.upsert("E", datetime(2026, 7, 13, 15, 0))

    def test_upsert_normalises_to_utc(self) -> None:
        from zoneinfo import ZoneInfo

        eastern = datetime(2026, 7, 13, 11, 0, tzinfo=ZoneInfo("America/New_York"))
        cache = ScheduleCache()
        cache.upsert("E", eastern)
        got = cache.peek_start("E")
        assert got is not None
        assert got.tzinfo is UTC
        assert got == datetime(2026, 7, 13, 15, 0, tzinfo=UTC)  # 11:00 EDT = 15:00Z


# ---------------------------------------------------------------------------
# Ladder precedence: embedded-ET > schedule feed > estimate.
# ---------------------------------------------------------------------------


class TestLadderPrecedence:
    def test_schedule_tier_used_when_no_embedded_token(self) -> None:
        clock = _clock()
        meta = _metadata(clock)
        # A WC leg has NO embedded start token; seed metadata + a schedule entry.
        _seed_meta(meta, clock, "KXWCGAME-26JUL13FRAMAR-FRA")
        sched_start = NOW + timedelta(hours=3)
        cache = ScheduleCache({"KXWCGAME-26JUL13FRAMAR": sched_start})
        gate = PregameGate(FiltersConfig(), meta, clock, cache)
        resolved = gate.leg_start("KXWCGAME-26JUL13FRAMAR-FRA")
        assert resolved is not None
        assert resolved.precise is True
        assert resolved.start == sched_start

    def test_estimate_used_on_schedule_miss(self) -> None:
        clock = _clock()
        meta = _metadata(clock)
        _seed_meta(meta, clock, "KXWCGAME-26JUL13FRAMAR-FRA")
        gate = PregameGate(FiltersConfig(), meta, clock, ScheduleCache())
        resolved = gate.leg_start("KXWCGAME-26JUL13FRAMAR-FRA")
        assert resolved is not None
        assert resolved.precise is False  # estimate ⇒ margins do NOT apply
        # close 6h out − 4.5h offset = NOW + 1.5h.
        assert resolved.start == NOW + timedelta(hours=1.5)

    def test_embedded_beats_schedule(self) -> None:
        clock = _clock()
        meta = _metadata(clock)
        ticker = "KXMLBGAME-26JUL131915BOSNYM-BOS"
        _seed_meta(meta, clock, ticker, event_ticker="KXMLBGAME-26JUL131915BOSNYM")
        # Schedule entry that DISAGREES — embedded ET must still win.
        cache = ScheduleCache(
            {"KXMLBGAME-26JUL131915BOSNYM": NOW + timedelta(hours=99)}
        )
        gate = PregameGate(FiltersConfig(), meta, clock, cache)
        resolved = gate.leg_start(ticker)
        assert resolved is not None
        assert resolved.precise is True
        # 19:15 ET on Jul 13 = 23:15Z.
        assert resolved.start == datetime(2026, 7, 13, 23, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# M_q vs M_c margins on a PRECISE start.
# ---------------------------------------------------------------------------


class TestMargins:
    def _gate_with_precise_start(
        self, config: FiltersConfig, minutes_to_start: float
    ) -> tuple[PregameGate, RfqLeg]:
        clock = _clock()
        meta = _metadata(clock)
        _seed_meta(meta, clock, "KXWCGAME-26JUL13FRAMAR-FRA")
        start = NOW + timedelta(minutes=minutes_to_start)
        cache = ScheduleCache({"KXWCGAME-26JUL13FRAMAR": start})
        gate = PregameGate(config, meta, clock, cache)
        return gate, _leg("KXWCGAME-26JUL13FRAMAR-FRA")

    def test_default_zero_margins_keep_todays_behaviour(self) -> None:
        # 30 min to a precise start; default M_q=M_c=0 ⇒ still pregame both ends.
        gate, leg = self._gate_with_precise_start(FiltersConfig(), minutes_to_start=30)
        assert not gate.status([leg]).any_started
        assert not gate.confirm_status([leg]).any_started

    def test_quote_margin_declines_inside_M_q(self) -> None:
        # M_q = 10 min: a start 5 min out is inside the quote cutoff ⇒ started.
        cfg = FiltersConfig(
            pregame_quote_margin_s=600.0, pregame_confirm_margin_s=600.0
        )
        gate, leg = self._gate_with_precise_start(cfg, minutes_to_start=5)
        assert gate.status([leg]).any_started

    def test_confirm_margin_stricter_than_quote(self) -> None:
        # M_q = 2 min, M_c = 10 min: a start 5 min out is OUTSIDE M_q (quote ok)
        # but INSIDE M_c (confirm declines) — the recover-flow / strict-confirm
        # resolution.
        cfg = FiltersConfig(
            pregame_quote_margin_s=120.0, pregame_confirm_margin_s=600.0
        )
        gate, leg = self._gate_with_precise_start(cfg, minutes_to_start=5)
        assert not gate.status([leg]).any_started        # quote side ok
        assert gate.confirm_status([leg]).any_started     # confirm side strict

    def test_margins_do_not_apply_to_estimate(self) -> None:
        # An ESTIMATE start (schedule miss) already bakes in its buffer; the
        # M_q/M_c margins must NOT stack on top (would double-count). A big M_c
        # against an estimate 1.5h out stays pregame.
        clock = _clock()
        meta = _metadata(clock)
        _seed_meta(meta, clock, "KXWCGAME-26JUL13FRAMAR-FRA")
        cfg = FiltersConfig(
            pregame_quote_margin_s=600.0, pregame_confirm_margin_s=3_000.0
        )
        gate = PregameGate(cfg, meta, clock, ScheduleCache())
        leg = _leg("KXWCGAME-26JUL13FRAMAR-FRA")
        assert not gate.status([leg]).any_started
        assert not gate.confirm_status([leg]).any_started

    def test_per_prefix_margin_override(self) -> None:
        cfg = FiltersConfig(
            pregame_quote_margin_s=0.0,
            pregame_confirm_margin_s=0.0,
            pregame_quote_margin_s_by_prefix={"KXWC": 600.0},
            pregame_confirm_margin_s_by_prefix={"KXWC": 600.0},
        )
        gate, leg = self._gate_with_precise_start(cfg, minutes_to_start=5)
        assert gate.status([leg]).any_started  # prefix M_q = 10 min ⇒ declined


# ---------------------------------------------------------------------------
# Config validation: M_c >= M_q.
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_confirm_margin_below_quote_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be >="):
            FiltersConfig(
                pregame_quote_margin_s=600.0, pregame_confirm_margin_s=120.0
            )

    def test_equal_margins_allowed(self) -> None:
        cfg = FiltersConfig(
            pregame_quote_margin_s=300.0, pregame_confirm_margin_s=300.0
        )
        assert cfg.pregame_confirm_margin_s == 300.0


# ---------------------------------------------------------------------------
# leg_start_time (StartTimeProvider) returns the RAW start, no margin.
# ---------------------------------------------------------------------------


def test_leg_start_time_returns_raw_start_no_margin() -> None:
    clock = _clock()
    meta = _metadata(clock)
    _seed_meta(meta, clock, "KXWCGAME-26JUL13FRAMAR-FRA")
    start = NOW + timedelta(hours=2)
    cache = ScheduleCache({"KXWCGAME-26JUL13FRAMAR": start})
    cfg = FiltersConfig(pregame_confirm_margin_s=600.0, pregame_quote_margin_s=600.0)
    gate = PregameGate(cfg, meta, clock, cache)
    # The StartTimeProvider wants the true start, NOT a margin-adjusted cutoff.
    assert gate.leg_start_time("KXWCGAME-26JUL13FRAMAR-FRA") == start
