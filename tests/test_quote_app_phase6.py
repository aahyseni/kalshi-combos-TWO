"""Phase 6 QuoteApp wiring: block-restart-until-reconciled + prod preflight
refusal + heartbeat/breaker sampling. Uses a fake REST + a demo QuoteApp so no
network is touched."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from combomaker.core.clock import FakeClock
from combomaker.core.conventions import load_conventions
from combomaker.exchange.rest import KalshiApiError
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
from combomaker.ops.quote_app import QuoteApp
from combomaker.risk.exposure import ExposureBook
from combomaker.risk.heartbeat import ReconcileMarker
from combomaker.risk.limits import LimitChecker, RiskLimits
from combomaker.risk.reservation import RiskReservationService


class FakeRest:
    """Minimal REST double for the startup reconcile. ``fail`` makes the calls
    raise KalshiApiError (exchange unreachable)."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
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
        return {"market_positions": []}


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


def test_prod_preflight_green_writes_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KALSHI_SUPERVISOR_API_KEY_ID", "sup")
    monkeypatch.setenv("KALSHI_SUPERVISOR_PRIVATE_KEY_PEM", "-----PEM-----")
    app = _prod_app_for_preflight(tmp_path, reconciled=True)
    app._run_prod_preflight()  # all gates green ⇒ no raise
    assert (tmp_path / "heartbeat.txt").exists()  # first beat established for the supervisor


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


def test_sampler_cold_feed_is_not_stale(tmp_path: Path) -> None:
    # Cold feed (rx_age None, not warm): the sampler carries feed_warm=False so
    # the data-staleness breaker is exempt during warmup.
    app = _demo_app(tmp_path)
    feed = FakeFeed(rx_age_s=None, warm=False, seq_gap=True)
    inputs = app._sample_breaker_inputs(feed)  # type: ignore[arg-type]
    assert inputs.feed_warm is False
    assert inputs.rx_age_s is None
    assert inputs.seq_gap is True  # real gap flag surfaced (but exempt while cold)


def test_sampler_uses_real_seq_gap_flag(tmp_path: Path) -> None:
    app = _demo_app(tmp_path)
    warm_no_gap = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=False)
    assert app._sample_breaker_inputs(warm_no_gap).seq_gap is False  # type: ignore[arg-type]
    warm_gap = FakeFeed(rx_age_s=0.1, warm=True, seq_gap=True)
    assert app._sample_breaker_inputs(warm_gap).seq_gap is True  # type: ignore[arg-type]


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
    inputs = app._sample_breaker_inputs(feed)  # type: ignore[arg-type]
    assert inputs.latency_ms == 12.0  # recent, not the 9,999ms all-time max
