"""Metrics: the recent-window latency sampler the Phase-6 latency-spike breaker
uses. Regression for the all-time-max latch (one historical slow confirm must
not permanently trip HALT_LATENCY_SPIKE)."""

from __future__ import annotations

from combomaker.core.clock import FakeClock
from combomaker.ops.metrics import Metrics


def test_histogram_max_is_all_time_and_never_decays() -> None:
    m = Metrics()
    m.observe_ms("confirm.rtt_ms", 5_000.0)
    m.observe_ms("confirm.rtt_ms", 10.0)
    # The all-time max keeps the historical spike forever — this is exactly why
    # the breaker must NOT sample it.
    assert m.histogram_max_ms("confirm.rtt_ms") == 5_000.0


def test_recent_max_ms_none_when_never_observed() -> None:
    m = Metrics(FakeClock())
    assert m.recent_max_ms("confirm.rtt_ms", 60.0) is None


def test_recent_max_ms_none_without_clock() -> None:
    # No clock ⇒ no recent-window bookkeeping ⇒ fail-closed None (nothing recent
    # to judge). The spike breaker clears on None, so an unmetered build never
    # false-trips.
    m = Metrics()
    m.observe_ms("confirm.rtt_ms", 9_999.0)
    assert m.recent_max_ms("confirm.rtt_ms", 60.0) is None


def test_recent_max_ms_windows_out_old_spikes() -> None:
    clock = FakeClock()
    m = Metrics(clock)
    m.observe_ms("confirm.rtt_ms", 5_000.0)  # a spike
    assert m.recent_max_ms("confirm.rtt_ms", 60.0) == 5_000.0
    # After the window passes with only fast confirms, the spike ages out and the
    # recent max reflects the CURRENT (fast) latency — the breaker self-clears.
    clock.advance(61.0)
    m.observe_ms("confirm.rtt_ms", 10.0)
    assert m.recent_max_ms("confirm.rtt_ms", 60.0) == 10.0


def test_recent_max_ms_keeps_spike_inside_window() -> None:
    clock = FakeClock()
    m = Metrics(clock)
    m.observe_ms("confirm.rtt_ms", 5_000.0)
    clock.advance(30.0)  # still inside a 60s window
    m.observe_ms("confirm.rtt_ms", 10.0)
    # A CURRENT spike must still show — the recent max is the worst in-window.
    assert m.recent_max_ms("confirm.rtt_ms", 60.0) == 5_000.0
