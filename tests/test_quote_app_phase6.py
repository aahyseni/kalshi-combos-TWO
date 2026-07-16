"""Phase 6 QuoteApp wiring: block-restart-until-reconciled + prod preflight
refusal + heartbeat/breaker sampling. Uses a fake REST + a demo QuoteApp so no
network is touched."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import Side, load_conventions
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.core.reasons import ReasonCode
from combomaker.exchange.rest import KalshiApiError, RateLimitedError
from combomaker.marketdata.metadata import MarketMeta
from combomaker.ops.config import (
    AppConfig,
    EndpointsConfig,
    Env,
    FiltersConfig,
    Mode,
    SafetyConfig,
)
from combomaker.ops.metrics import Metrics
from combomaker.ops.preflight import PreflightError
from combomaker.ops.quote_app import QuoteApp, RateLimitRecordingSender
from combomaker.ops.supervisor import supervisor_heartbeat_path
from combomaker.risk.breakers import RateLimitWindow
from combomaker.risk.exposure import ExposureBook, LegRef, OpenPosition, OpenQuoteRisk
from combomaker.risk.heartbeat import Heartbeat, ReconcileMarker
from combomaker.risk.killswitch import HaltEvent
from combomaker.risk.limits import DailyPnl, LimitChecker, RiskLimits
from combomaker.risk.reservation import RiskReservationService


class FakeRest:
    """Minimal REST double for the startup reconcile. ``fail`` makes the calls
    raise KalshiApiError (exchange unreachable). ``positions`` sets the
    /portfolio/positions payload for the reservation reconcile."""

    def __init__(
        self, *, fail: bool = False, positions: dict[str, Any] | None = None
    ) -> None:
        self._fail = fail
        self._positions = positions or {"market_positions": []}
        self.deleted: list[str] = []

    async def get_quotes(self, **params: Any) -> dict[str, Any]:
        if self._fail:
            raise KalshiApiError(503, "unavailable", "down")
        return {"quotes": []}

    async def delete_quote(self, quote_id: str) -> dict[str, Any]:
        self.deleted.append(quote_id)
        return {}

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        if self._fail:
            raise KalshiApiError(503, "unavailable", "down")
        return self._positions


def _demo_app(tmp_path: Path) -> QuoteApp:
    config = AppConfig(
        env=Env.DEMO,
        mode=Mode.PAPER,  # paper avoids the quote-mode conventions/whitelist gate
        endpoints=EndpointsConfig.for_env(Env.DEMO),
        data_dir=tmp_path,
        kill_file=tmp_path / "KILL",
    )
    return QuoteApp(config)


def _reservation() -> RiskReservationService:
    return RiskReservationService(
        exposure=ExposureBook(load_conventions()),
        limits=LimitChecker(RiskLimits()),
        breach_splitter=lambda breaches: breaches,
    )


async def test_block_restart_clears_marker_on_success(tmp_path: Path) -> None:
    marker = ReconcileMarker(tmp_path / "needs_reconcile")
    marker.set("prior hard trip")  # a prior kill left the marker
    app = _demo_app(tmp_path)
    rest = FakeRest()
    reservation = _reservation()
    await app._block_restart_until_reconciled(rest, reservation)  # type: ignore[arg-type]
    assert app._book_reconciled is True
    assert marker.is_set() is False  # cleared only after a successful reconcile


async def test_block_restart_keeps_marker_when_exchange_unreachable(
    tmp_path: Path,
) -> None:
    marker = ReconcileMarker(tmp_path / "needs_reconcile")
    marker.set("prior hard trip")
    app = _demo_app(tmp_path)
    rest = FakeRest(fail=True)  # exchange down ⇒ reconcile fails
    reservation = _reservation()
    await app._block_restart_until_reconciled(rest, reservation)  # type: ignore[arg-type]
    assert app._book_reconciled is False  # NOT reconciled — refuse to quote
    assert marker.is_set() is True        # marker stays in force (fail-closed)


async def test_startup_reconcile_returns_success_flag(tmp_path: Path) -> None:
    app = _demo_app(tmp_path)
    assert await app._startup_reconcile(FakeRest()) is True  # type: ignore[arg-type]
    assert await app._startup_reconcile(FakeRest(fail=True)) is False  # type: ignore[arg-type]


async def test_ensure_watched_skips_out_of_allowlist_combos(tmp_path: Path) -> None:
    """Only subscribe book feeds for combos we could quote: a combo with any
    out-of-allowlist leg (declined anyway) must NOT trigger book subscriptions —
    else its irrelevant legs (WNBA/ATP/crypto) flood the WS → slow-consumer kill.
    """
    from tests.test_filters import combo_rfq

    class _RecordingFeed:
        def __init__(self) -> None:
            self.watched: list[str] = []

        def watch(self, tickers: Any) -> None:
            self.watched.extend(tickers)

    class _StubMeta:
        def peek(self, ticker: str) -> Any:
            return object()  # non-None ⇒ combo market not re-fetched

        async def market(self, ticker: str) -> Any:
            class _M:
                event_ticker = None

            return _M()

    app = _demo_app(tmp_path)  # default filters allowlist = ["KXWC", "KXMLB"]
    feed = _RecordingFeed()
    meta = _StubMeta()

    mixed = combo_rfq(
        mve_selected_legs=[
            {"market_ticker": "KXWCADVANCE-26JUL14FRAESP-FRA", "side": "yes"},
            {"market_ticker": "KXWNBAGAME-26JUL13LAATL-ATL", "side": "yes"},
        ]
    )
    await app._ensure_watched(mixed, feed, meta)  # type: ignore[arg-type]
    assert feed.watched == []  # out-of-allowlist leg ⇒ skipped entirely

    allowed = combo_rfq(
        mve_selected_legs=[
            {"market_ticker": "KXWCADVANCE-26JUL14FRAESP-FRA", "side": "yes"},
            {"market_ticker": "KXMLBGAME-26JUL13NYYBOS-NYY", "side": "no"},
        ]
    )
    await app._ensure_watched(allowed, feed, meta)  # type: ignore[arg-type]
    assert set(feed.watched) == {
        "KXWCADVANCE-26JUL14FRAESP-FRA",
        "KXMLBGAME-26JUL13NYYBOS-NYY",
    }


def test_prod_preflight_is_noop_on_demo(tmp_path: Path) -> None:
    app = _demo_app(tmp_path)
    app._run_prod_preflight()  # demo ⇒ no-op, no raise


def _prod_app_for_preflight(tmp_path: Path, *, reconciled: bool) -> QuoteApp:
    # Build a demo app (construction is network-free) then swap its config to a
    # prod one so _run_prod_preflight exercises the prod path without the network.
    app = _demo_app(tmp_path)
    prod_config = AppConfig(
        env=Env.PROD,
        mode=Mode.QUOTE,
        endpoints=EndpointsConfig.for_env(Env.PROD),
        safety=SafetyConfig(
            prod_limits_configured=True,
            prod_require_series_whitelist=True,
            prod_require_supervisor=True,
        ),
        filters=FiltersConfig(allowed_leg_series_prefixes=["KXWC"]),
        data_dir=tmp_path,
        kill_file=tmp_path / "KILL",
        confirm_live=True,
    )
    app._config = prod_config
    app._book_reconciled = reconciled
    return app


def test_prod_preflight_refuses_without_supervisor_credential(
    tmp_path: Path,
) -> None:
    # conftest strips the supervisor credential env, so external_kill_reachable is
    # red ⇒ the prod preflight must refuse to quote.
    app = _prod_app_for_preflight(tmp_path, reconciled=True)
    with pytest.raises(PreflightError, match="external_kill_reachable"):
        app._run_prod_preflight()


def test_prod_preflight_refuses_when_book_unreconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Give it a supervisor credential so that gate is green, but leave the book
    # unreconciled ⇒ still refuses (block-restart-until-reconciled).
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    app = _prod_app_for_preflight(tmp_path, reconciled=False)
    with pytest.raises(PreflightError, match="book_reconciled"):
        app._run_prod_preflight()


def test_prod_preflight_refuses_when_supervisor_not_beating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Credential present BUT no supervisor process beating its heartbeat ⇒
    # external_kill_reachable is now RED (stronger than mere credential presence:
    # a credential with no running watcher is a dead kill path).
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    app = _prod_app_for_preflight(tmp_path, reconciled=True)
    with pytest.raises(PreflightError, match="external_kill_reachable"):
        app._run_prod_preflight()


def test_prod_preflight_green_when_supervisor_beating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    app = _prod_app_for_preflight(tmp_path, reconciled=True)
    # A live supervisor has beaten its OWN heartbeat recently.
    Heartbeat(app._clock, supervisor_heartbeat_path(tmp_path)).beat()
    app._run_prod_preflight()  # all gates green ⇒ no raise
    assert (tmp_path / "heartbeat.txt").exists()  # bot's first beat for the supervisor


# --------------------------------------------------------------------------- #
# Synchronous KILL-file gate (finding: KILL must be honored at restart before
# any quoting, not left to the 1s async watcher).
# --------------------------------------------------------------------------- #


def test_kill_file_present_at_startup_refuses(tmp_path: Path) -> None:
    app = _demo_app(tmp_path)
    (tmp_path / "KILL").write_text("supervisor kill", encoding="utf-8")
    with pytest.raises(PreflightError, match="KILL file present"):
        app._refuse_if_kill_file_present()


def test_kill_file_absent_at_startup_allows(tmp_path: Path) -> None:
    app = _demo_app(tmp_path)
    app._refuse_if_kill_file_present()  # no KILL ⇒ no raise


async def test_reconcile_does_not_clear_marker_while_kill_present(
    tmp_path: Path,
) -> None:
    # Supervisor kill wrote BOTH the needs_reconcile marker AND the KILL file. A
    # reconcile against a reachable exchange must NOT clear the marker or mark the
    # book reconciled while KILL is still on disk — the operator clears a kill by
    # removing KILL, not by a successful reconcile.
    marker = ReconcileMarker(tmp_path / "needs_reconcile")
    marker.set("supervisor kill")
    (tmp_path / "KILL").write_text("supervisor kill", encoding="utf-8")
    app = _demo_app(tmp_path)
    await app._block_restart_until_reconciled(FakeRest(), _reservation())  # type: ignore[arg-type]
    assert app._book_reconciled is False   # NOT reconciled — KILL outranks
    assert marker.is_set() is True         # marker stays set while KILL present


# --------------------------------------------------------------------------- #
# Breaker input sampler: real signals, not the mis-wired / all-time-max ones.
# --------------------------------------------------------------------------- #


class FakeFeed:
    """Minimal OrderbookFeed double for _sample_breaker_inputs."""

    def __init__(self, *, rx_age_s: float | None, warm: bool, seq_gap: bool) -> None:
        self.rx_age_s = rx_age_s
        self.warm = warm
        self._seq_gap = seq_gap

    def pop_seq_gap(self) -> bool:
        gap = self._seq_gap
        self._seq_gap = False
        return gap


class FakeLifecycle:
    """Minimal QuoteLifecycle double: only the marginal_of accessor the breaker
    sampler uses. ``marginals`` maps market_ticker → P(YES) (None = unreadable)."""

    def __init__(self, marginals: dict[str, float | None] | None = None) -> None:
        self._m = marginals or {}

    def marginal_of(self, market_ticker: str) -> float | None:
        return self._m.get(market_ticker)


def _empty_book() -> ExposureBook:
    return ExposureBook(load_conventions())


class FakeMetadata:
    """Minimal MetadataCache double: peek returns a pre-seeded MarketMeta or None."""

    def __init__(self, metas: dict[str, Any] | None = None) -> None:
        self._metas = metas or {}

    def peek(self, ticker: str) -> Any:
        return self._metas.get(ticker)


def _sample(
    app: QuoteApp,
    feed: FakeFeed,
    *,
    lifecycle: FakeLifecycle | None = None,
    exposure: ExposureBook | None = None,
    metadata: FakeMetadata | None = None,
) -> Any:
    return app._sample_breaker_inputs(  # type: ignore[arg-type]
        feed,
        lifecycle or FakeLifecycle(),
        exposure or _empty_book(),
        metadata or FakeMetadata(),
    )


def test_sampler_cold_feed_is_not_stale(tmp_path: Path) -> None:
    # Cold feed (rx_age None, not warm): the sampler carries feed_warm=False so
    # the data-staleness breaker is exempt during warmup.
    app = _demo_app(tmp_path)
    feed = FakeFeed(rx_age_s=None, warm=False, seq_gap=True)
    inputs = _sample(app, feed)
    assert inputs.feed_warm is False
    assert inputs.rx_age_s is None
    assert inputs.seq_gap is True  # real gap flag surfaced (but exempt while cold)
    # Empty book ⇒ the three book-derived breakers see their CLEAR defaults.
    assert inputs.marginals == {}
    assert inputs.game_keys == {}
    assert inputs.tripwire_hit is None
    assert inputs.changed_markets == ()


def test_sampler_uses_real_seq_gap_flag(tmp_path: Path) -> None:
    app = _demo_app(tmp_path)
    warm_no_gap = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    assert _sample(app, warm_no_gap).seq_gap is False
    warm_gap = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=True)
    assert _sample(app, warm_gap).seq_gap is True


def test_sampler_latency_is_recent_window_not_all_time(tmp_path: Path) -> None:
    # A historical spike outside the window must not appear in the sampled
    # latency (regression for the all-time-histogram-max latch). Swap in a
    # FakeClock so the window can be advanced deterministically.
    app = _demo_app(tmp_path)
    clock = FakeClock()
    app._clock = clock
    app._metrics = Metrics(clock)
    window_s = app._config.breakers.latency_spike_window_s
    app._metrics.observe_ms("confirm.rtt_ms", 9_999.0)  # a historical spike
    clock.advance(window_s + 1.0)  # age it out of the window
    app._metrics.observe_ms("confirm.rtt_ms", 12.0)     # a recent fast confirm
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    inputs = _sample(app, feed)
    assert inputs.latency_ms == 12.0  # recent, not the 9,999ms all-time max


# --------------------------------------------------------------------------- #
# RESTART SAFETY: an in-process HARD halt drops the needs_reconcile marker so a
# bare restart is blocked; a soft/manual halt does not.
# --------------------------------------------------------------------------- #


def _halt(reason: ReasonCode) -> HaltEvent:
    return HaltEvent(reason=reason, detail="test", at_iso="2026-07-13T00:00:00+00:00")


@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.HALT_HARD_TRIP,
        ReasonCode.HALT_RECONCILIATION_MISMATCH,
        ReasonCode.HALT_FILL_VELOCITY,
        ReasonCode.HALT_DRAWDOWN,
        ReasonCode.HALT_DATA_STALE,
        ReasonCode.HALT_LATENCY_SPIKE,
        ReasonCode.HALT_BREAKER_ERROR,
    ],
)
def test_hard_halt_drops_reconcile_marker(tmp_path: Path, reason: ReasonCode) -> None:
    app = _demo_app(tmp_path)
    marker = ReconcileMarker(tmp_path / "needs_reconcile")
    assert marker.is_set() is False
    app.mark_reconcile_on_hard_halt(_halt(reason))
    assert marker.is_set() is True          # restart is now BLOCKED until reconciled
    assert app._book_reconciled is False


@pytest.mark.parametrize(
    "reason",
    [
        ReasonCode.HALT_MANUAL,
        ReasonCode.HALT_KILL_FILE,
        ReasonCode.HALT_SUPERVISOR,
        ReasonCode.HALT_EXCHANGE_STATUS,
        ReasonCode.HALT_DAILY_LOSS,
        ReasonCode.HALT_WS_UNHEALTHY,
    ],
)
def test_soft_or_manual_halt_leaves_marker_alone(
    tmp_path: Path, reason: ReasonCode
) -> None:
    app = _demo_app(tmp_path)
    marker = ReconcileMarker(tmp_path / "needs_reconcile")
    app.mark_reconcile_on_hard_halt(_halt(reason))
    assert marker.is_set() is False         # a soft/manual halt does not block restart


# --------------------------------------------------------------------------- #
# reconcile-with-real-positions: a confirm-timeout reservation is resolved by the
# exchange's actual open positions, not left leaking headroom.
# --------------------------------------------------------------------------- #


def _outstanding_position(pid: str, ticker: str) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker=ticker,
        collection=None,
        our_side=Side.NO,
        contracts=CentiContracts(10_000),
        entry_price_cc=CentiCents(5_000),
        legs=(LegRef("A", "SER-GAME1", "no"),),
    )


async def test_reconcile_reservations_commits_landed_releases_leaked(
    tmp_path: Path,
) -> None:
    app = _demo_app(tmp_path)
    reservation = _reservation()
    # Two reservations whose confirms timed out (mark_unconfirmed).
    reservation.try_reserve(
        "fill:q1", _outstanding_position("fill:q1", "C1"),
        marginals=lambda _t: 0.5, daily_pnl=DailyPnl(),
    )
    reservation.try_reserve(
        "fill:q2", _outstanding_position("fill:q2", "C2"),
        marginals=lambda _t: 0.5, daily_pnl=DailyPnl(),
    )
    reservation.mark_unconfirmed("fill:q1")
    reservation.mark_unconfirmed("fill:q2")
    # Exchange reports ONLY C1 open (NO). The reconcile commits q1, releases q2.
    rest = FakeRest(positions={"market_positions": [{"ticker": "C1", "position_fp": "-100.00"}]})
    await app._reconcile_reservations(rest, reservation)  # type: ignore[arg-type]
    assert reservation.is_outstanding("fill:q1") is False  # committed (booked)
    assert reservation.is_outstanding("fill:q2") is False  # released (headroom freed)
    assert reservation.outstanding_count == 0


async def test_reconcile_reservations_noop_when_nothing_outstanding(
    tmp_path: Path,
) -> None:
    app = _demo_app(tmp_path)
    reservation = _reservation()
    rest = FakeRest()
    await app._reconcile_reservations(rest, reservation)  # type: ignore[arg-type]
    # No outstanding reservations ⇒ the positions endpoint is never even hit.
    assert reservation.outstanding_count == 0


# --------------------------------------------------------------------------- #
# The 3 formerly-dark breakers now FIRE on crafted real book inputs.
# The sampler builds marginals / game_keys / tripwire / metadata-diff from the
# legs the risk path touches, then the coordinator's detectors trip. These test
# the FULL wire: book → sampler → BreakerInputs → CircuitBreakers verdict.
# --------------------------------------------------------------------------- #


def _quote_with_legs(
    quote_id: str, combo: str, legs: tuple[LegRef, ...]
) -> OpenQuoteRisk:
    return OpenQuoteRisk(
        quote_id=quote_id,
        rfq_id=f"rfq-{quote_id}",
        combo_ticker=combo,
        collection=None,
        yes_bid_cc=CentiCents(4_000),
        no_bid_cc=CentiCents(0),
        contracts=CentiContracts(10_000),
        legs=legs,
    )


def _book_with_quote_legs(legs: tuple[LegRef, ...]) -> ExposureBook:
    book = _empty_book()
    book.upsert_quote(_quote_with_legs("q1", "COMBO", legs))
    return book


def _breakers(app: QuoteApp) -> Any:
    from combomaker.risk.breakers import CircuitBreakers
    from combomaker.risk.killswitch import KillSwitch

    ks = KillSwitch(app._clock, kill_file=app._config.kill_file)
    return CircuitBreakers(ks, app._config.breakers.to_thresholds(), app._clock)


def test_marginal_jump_breaker_fires_on_real_book_move(tmp_path: Path) -> None:
    # A leg resting in the book that moves more than the jump threshold across two
    # samples must trip HALT_MARGINAL_JUMP — the breaker that was formerly dark.
    app = _demo_app(tmp_path)
    breakers = _breakers(app)
    legs = (LegRef("LEG", "KXWCGAME-26JUL05MEXENG", "yes"),)
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    # Tick 1 seeds the baseline (0.50): no jump yet.
    first = _sample(
        app, feed, lifecycle=FakeLifecycle({"LEG": 0.50}), exposure=book,
    )
    assert breakers.evaluate(first).tripped is False
    # Tick 2: the leg jumped 0.50 → 0.90 (> 0.25 default) ⇒ trip.
    second = _sample(
        app, feed, lifecycle=FakeLifecycle({"LEG": 0.90}), exposure=book,
    )
    verdict = breakers.evaluate(second)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_MARGINAL_JUMP


def test_marginal_jump_breaker_fires_when_leg_becomes_unreadable(
    tmp_path: Path,
) -> None:
    # Fail-closed: a leg we priced against whose marginal becomes None (book gone)
    # trips — UNKNOWN is never a convenient pass.
    app = _demo_app(tmp_path)
    breakers = _breakers(app)
    legs = (LegRef("LEG", "KXWCGAME-26JUL05MEXENG", "yes"),)
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    breakers.evaluate(
        _sample(app, feed, lifecycle=FakeLifecycle({"LEG": 0.50}), exposure=book)
    )
    gone = _sample(app, feed, lifecycle=FakeLifecycle({"LEG": None}), exposure=book)
    verdict = breakers.evaluate(gone)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_MARGINAL_JUMP


def test_unmapped_game_breaker_fires_on_none_event_ticker(tmp_path: Path) -> None:
    # A leg with no resolvable event_ticker (→ None game key) would escape the
    # game/slate cluster caps ⇒ trip HALT_UNMAPPED_GAME.
    app = _demo_app(tmp_path)
    breakers = _breakers(app)
    legs = (LegRef("LEG", None, "yes"),)  # no event_ticker ⇒ unresolvable
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    inputs = _sample(
        app, feed, lifecycle=FakeLifecycle({"LEG": 0.50}), exposure=book
    )
    assert inputs.game_keys == {"LEG": None}
    verdict = breakers.evaluate(inputs)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_UNMAPPED_GAME


def _market_meta(
    ticker: str, *, status: str, close_time: datetime | None
) -> MarketMeta:
    return MarketMeta(
        ticker=ticker,
        status=status,
        grid=None,
        event_ticker="KXWCGAME-26JUL05MEXENG",
        close_time=close_time,
        expected_expiration_time=None,
        raw={},
        fetched_mono_ns=0,
    )


def test_metadata_change_breaker_fires_on_settlement_meta_change(
    tmp_path: Path,
) -> None:
    # A market whose settlement-relevant metadata (close_time / status) changes
    # tick-over-tick must trip HALT_METADATA_CHANGE. First sighting seeds the
    # baseline (no trip); the change on the next sample trips.
    app = _demo_app(tmp_path)
    breakers = _breakers(app)
    legs = (LegRef("LEG", "KXWCGAME-26JUL05MEXENG", "yes"),)
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    t0 = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)
    meta_v1 = FakeMetadata({"LEG": _market_meta("LEG", status="active", close_time=t0)})
    first = _sample(
        app, feed, lifecycle=FakeLifecycle({"LEG": 0.50}), exposure=book,
        metadata=meta_v1,
    )
    assert first.changed_markets == ()  # baseline seeded, no change yet
    assert breakers.evaluate(first).tripped is False
    # The close_time moved under us (settlement window changed).
    t1 = datetime(2026, 7, 5, 20, 0, tzinfo=UTC)
    meta_v2 = FakeMetadata({"LEG": _market_meta("LEG", status="active", close_time=t1)})
    second = _sample(
        app, feed, lifecycle=FakeLifecycle({"LEG": 0.50}), exposure=book,
        metadata=meta_v2,
    )
    assert second.changed_markets == ("LEG",)
    verdict = breakers.evaluate(second)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_METADATA_CHANGE


def test_metadata_change_breaker_quiet_on_first_sighting(tmp_path: Path) -> None:
    # A newly-quoted market is NOT a change (it seeds the baseline) — the breaker
    # must not self-trip on every fresh market.
    app = _demo_app(tmp_path)
    breakers = _breakers(app)
    legs = (LegRef("LEG", "KXWCGAME-26JUL05MEXENG", "yes"),)
    book = _book_with_quote_legs(legs)
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    t0 = datetime(2026, 7, 5, 18, 0, tzinfo=UTC)
    meta = FakeMetadata({"LEG": _market_meta("LEG", status="active", close_time=t0)})
    inputs = _sample(
        app, feed, lifecycle=FakeLifecycle({"LEG": 0.50}), exposure=book, metadata=meta
    )
    assert inputs.changed_markets == ()
    assert breakers.evaluate(inputs).tripped is False


# --------------------------------------------------------------------------- #
# The rate-limit-burst window now counts confirm/create/delete 429s (not just
# the balance/status polls). A wrapped sender records a confirm 429 into the
# window on the way past, then re-raises.
# --------------------------------------------------------------------------- #


class _RaisingSender:
    """A QuoteSender whose confirm/create/delete raise a 429."""

    async def create_quote(
        self, rfq_id: str, *, yes_bid_cc: Any, no_bid_cc: Any,
        rest_remainder: bool = False,
    ) -> dict[str, Any]:
        raise RateLimitedError(429, "rate_limited", "slow down")

    async def delete_quote(self, quote_id: str) -> dict[str, Any]:
        raise RateLimitedError(429, "rate_limited", "slow down")

    async def confirm_quote(self, quote_id: str) -> dict[str, Any]:
        raise RateLimitedError(429, "rate_limited", "slow down")


async def test_confirm_429_counts_toward_rate_limit_window() -> None:
    clock = FakeClock()
    window = RateLimitWindow(clock=clock, window_s=10.0)
    sender = RateLimitRecordingSender(_RaisingSender(), window)
    assert window.count() == 0
    # A confirm 429 is recorded (the write path, not just the polls) and re-raised.
    with pytest.raises(RateLimitedError):
        await sender.confirm_quote("q1")
    assert window.count() == 1
    # create + delete 429s count too.
    with pytest.raises(RateLimitedError):
        await sender.create_quote("rfq1", yes_bid_cc=CentiCents(4_000), no_bid_cc=CentiCents(0))
    with pytest.raises(RateLimitedError):
        await sender.delete_quote("q1")
    assert window.count() == 3


async def test_rate_limit_window_burst_trips_breaker_from_confirm_429s(
    tmp_path: Path,
) -> None:
    # End-to-end: enough confirm 429s in the window to reach the breaker's
    # threshold ⇒ the rate-limit-burst breaker trips.
    app = _demo_app(tmp_path)
    breakers = _breakers(app)
    sender = RateLimitRecordingSender(_RaisingSender(), app._rate_limit_window)
    threshold = app._config.breakers.to_thresholds().max_rate_limit_in_window
    for _ in range(threshold):
        with pytest.raises(RateLimitedError):
            await sender.confirm_quote("q1")
    feed = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    inputs = _sample(app, feed)
    assert inputs.rate_limit_count >= threshold
    verdict = breakers.evaluate(inputs)
    assert verdict.tripped is True
    assert verdict.reason is ReasonCode.HALT_RATE_LIMIT_BURST


def test_supervisor_launch_cmd_forwards_config_path(tmp_path: Path) -> None:
    # Problem B (2026-07-15 heartbeat kills): the supervisor subprocess re-loads
    # config itself; if the launcher omits --config, a local-override launch
    # config (e.g. supervisor.heartbeat_timeout_s) applies to the bot but NOT to
    # the watchdog that enforces it. The launch argv must forward the SAME file
    # the bot was started with.
    from combomaker.ops.quote_app import supervisor_launch_cmd

    cfg_file = tmp_path / "prod-live.local.yaml"
    config = AppConfig(
        env=Env.PROD,
        mode=Mode.QUOTE,
        endpoints=EndpointsConfig.for_env(Env.PROD),
        source_path=cfg_file,
    )
    cmd = supervisor_launch_cmd(config)
    assert cmd[1:3] == ["-m", "combomaker.ops.supervisor"]
    assert cmd[cmd.index("--env") + 1] == "prod"
    assert cmd[cmd.index("--config") + 1] == str(cfg_file)


def test_supervisor_launch_cmd_without_source_path(tmp_path: Path) -> None:
    # No explicit config file (base per-env launch) ⇒ no --config flag, the
    # supervisor derives config/{env}.yaml exactly as before.
    from combomaker.ops.quote_app import supervisor_launch_cmd

    config = AppConfig(
        env=Env.DEMO,
        mode=Mode.QUOTE,
        endpoints=EndpointsConfig.for_env(Env.DEMO),
    )
    cmd = supervisor_launch_cmd(config)
    assert "--config" not in cmd
    assert cmd[cmd.index("--env") + 1] == "demo"
