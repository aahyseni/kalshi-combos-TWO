"""Phase 6 circuit breakers: each detector fires AT its threshold (not just
under), each fails closed on an uncomputable input, and the coordinator trips
the kill switch."""

from __future__ import annotations

from combomaker.core.clock import FakeClock
from combomaker.core.reasons import ReasonCode
from combomaker.risk.breakers import (
    BreakerInputs,
    BreakerThresholds,
    CircuitBreakers,
    RateLimitWindow,
    detect_data_stale,
    detect_latency_spike,
    detect_marginal_jump,
    detect_metadata_change,
    detect_rate_limit_burst,
    detect_unmapped_game,
)
from combomaker.risk.killswitch import KillSwitch

# --------------------------------------------------------------------------- #
# Pure detectors — threshold + fail-closed contract.
# --------------------------------------------------------------------------- #


def test_data_stale_fires_over_not_at() -> None:
    assert detect_data_stale(5.0, seq_gap=False, max_rx_age_s=5.0).tripped is False
    v = detect_data_stale(5.01, seq_gap=False, max_rx_age_s=5.0)
    assert v.tripped and v.reason is ReasonCode.HALT_DATA_STALE


def test_data_stale_none_rx_age_fails_closed() -> None:
    v = detect_data_stale(None, seq_gap=False, max_rx_age_s=5.0)
    assert v.tripped and v.reason is ReasonCode.HALT_DATA_STALE


def test_data_stale_seq_gap_trips_regardless_of_age() -> None:
    v = detect_data_stale(0.0, seq_gap=True, max_rx_age_s=5.0)
    assert v.tripped and v.reason is ReasonCode.HALT_DATA_STALE


def test_data_stale_cold_feed_does_not_trip() -> None:
    # Feed not warm yet (no first frame): pre-connect None rx-age + seq_gap must
    # NOT self-halt the bot during warmup. The cold-start exemption clears.
    assert (
        detect_data_stale(
            None, seq_gap=True, max_rx_age_s=5.0, feed_warm=False
        ).tripped
        is False
    )
    assert (
        detect_data_stale(
            None, seq_gap=False, max_rx_age_s=5.0, feed_warm=False
        ).tripped
        is False
    )


def test_data_stale_after_warmup_still_fails_closed() -> None:
    # Once warm, a disconnect (None rx-age) or a seq gap still trips (the latch
    # only exempts the warmup window, never a post-warmup failure).
    v = detect_data_stale(None, seq_gap=False, max_rx_age_s=5.0, feed_warm=True)
    assert v.tripped and v.reason is ReasonCode.HALT_DATA_STALE
    v2 = detect_data_stale(0.0, seq_gap=True, max_rx_age_s=5.0, feed_warm=True)
    assert v2.tripped and v2.reason is ReasonCode.HALT_DATA_STALE


def test_latency_spike_fires_over_not_at() -> None:
    assert detect_latency_spike(2_000.0, max_latency_ms=2_000.0).tripped is False
    v = detect_latency_spike(2_000.1, max_latency_ms=2_000.0)
    assert v.tripped and v.reason is ReasonCode.HALT_LATENCY_SPIKE


def test_latency_none_sample_clears() -> None:
    # No round-trip measured yet (startup) ⇒ nothing to judge, clears. A spike
    # requires an actual over-threshold measurement.
    assert detect_latency_spike(None, max_latency_ms=2_000.0).tripped is False


def test_rate_limit_burst_fires_at_count() -> None:
    assert detect_rate_limit_burst(9, max_in_window=10).tripped is False
    v = detect_rate_limit_burst(10, max_in_window=10)  # AT the count IS a burst
    assert v.tripped and v.reason is ReasonCode.HALT_RATE_LIMIT_BURST
    assert detect_rate_limit_burst(11, max_in_window=10).tripped is True


def test_marginal_jump_fires_over_not_at() -> None:
    assert detect_marginal_jump(0.50, 0.75, ticker="X", max_jump=0.25).tripped is False
    v = detect_marginal_jump(0.50, 0.7501, ticker="X", max_jump=0.25)
    assert v.tripped and v.reason is ReasonCode.HALT_MARGINAL_JUMP


def test_marginal_jump_no_baseline_clears() -> None:
    # First-ever reading has no prior to compare — not a jump.
    assert detect_marginal_jump(None, 0.9, ticker="X", max_jump=0.25).tripped is False


def test_marginal_jump_lost_current_fails_closed() -> None:
    # We had a baseline and now can't read it — the leg we priced vanished.
    v = detect_marginal_jump(0.5, None, ticker="X", max_jump=0.25)
    assert v.tripped and v.reason is ReasonCode.HALT_MARGINAL_JUMP


def test_unmapped_game_none_fails_closed() -> None:
    v = detect_unmapped_game(None, ticker="X")
    assert v.tripped and v.reason is ReasonCode.HALT_UNMAPPED_GAME
    assert detect_unmapped_game("", ticker="X").tripped is True
    assert detect_unmapped_game("26JUL05MEXENG", ticker="X").tripped is False


def test_metadata_change_tripwire_and_markets() -> None:
    assert detect_metadata_change(None, ()).tripped is False
    v = detect_metadata_change(("S12", "impossible pair"), ())
    assert v.tripped and v.reason is ReasonCode.HALT_METADATA_CHANGE
    v2 = detect_metadata_change(None, ("KXWCGAME-X",))
    assert v2.tripped and v2.reason is ReasonCode.HALT_METADATA_CHANGE


# --------------------------------------------------------------------------- #
# Rolling 429 window.
# --------------------------------------------------------------------------- #


def test_rate_limit_window_prunes_old_events() -> None:
    clock = FakeClock()
    window = RateLimitWindow(clock=clock, window_s=10.0)
    window.record()
    window.record()
    assert window.count() == 2
    clock.advance(11.0)  # both fall out of the window
    assert window.count() == 0
    window.record()
    assert window.count() == 1


# --------------------------------------------------------------------------- #
# Coordinator wiring — actually trips the kill switch.
# --------------------------------------------------------------------------- #


def _breakers() -> tuple[CircuitBreakers, KillSwitch, FakeClock]:
    clock = FakeClock()
    ks = KillSwitch(clock)
    return CircuitBreakers(ks, BreakerThresholds(), clock), ks, clock


async def test_transient_data_stale_holds_within_grace_then_halts() -> None:
    breakers, ks, clock = _breakers()
    # A warm-feed stale (rx_age None) is TRANSIENT — it must NOT hard-kill on the
    # first tick (the 2026-07-13 live false-kill on a WS reconnect). It holds for
    # the grace window; only a SUSTAINED stale escalates to the hard halt.
    v = await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None))
    assert v.tripped and v.reason is ReasonCode.HALT_DATA_STALE
    assert not ks.halted  # held, not killed
    clock.advance(20.0)  # still within the 30s grace
    await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None))
    assert not ks.halted
    clock.advance(15.0)  # 35s total > 30s grace ⇒ sustained ⇒ hard halt
    await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None))
    assert ks.halted
    assert ks.halt_event is not None
    assert ks.halt_event.reason is ReasonCode.HALT_DATA_STALE


async def test_clear_inputs_do_not_halt() -> None:
    breakers, ks, _clock = _breakers()
    v = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=1.0, seq_gap=False, latency_ms=50.0, rate_limit_count=0)
    )
    assert v.tripped is False
    assert not ks.halted


async def test_coordinator_tracks_marginal_baseline_across_ticks() -> None:
    breakers, ks, _clock = _breakers()
    # Tick 1 establishes the baseline (no trip).
    v1 = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=1.0, marginals={"LEG": 0.50})
    )
    assert v1.tripped is False
    # Tick 2 jumps past the threshold ⇒ trip verdict — but marginal-jump is
    # TRANSIENT (a single move re-baselines), so it HOLDS, not hard-kill.
    v2 = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=1.0, marginals={"LEG": 0.90})
    )
    assert v2.tripped and v2.reason is ReasonCode.HALT_MARGINAL_JUMP
    assert not ks.halted
    # It re-baselined to 0.90; a stable next tick clears ⇒ timer resets, no halt.
    v3 = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=1.0, marginals={"LEG": 0.90})
    )
    assert not v3.tripped and not ks.halted


async def test_coordinator_unmapped_game_trips() -> None:
    breakers, ks, _clock = _breakers()
    v = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=1.0, game_keys={"LEG": None})
    )
    assert v.tripped and v.reason is ReasonCode.HALT_UNMAPPED_GAME
    assert ks.halted


async def test_detector_exception_fails_closed_to_breaker_error() -> None:
    breakers, ks, _clock = _breakers()

    # A mapping whose iteration raises simulates an uncomputable input; the
    # coordinator must convert the raise into a HALT_BREAKER_ERROR trip, never
    # a silent pass.
    class Exploding(dict[str, float | None]):
        def items(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    v = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=1.0, marginals=Exploding())
    )
    assert v.tripped and v.reason is ReasonCode.HALT_BREAKER_ERROR
    assert ks.halted


async def test_cold_feed_inputs_do_not_halt() -> None:
    # The live cold-start signature: rx_age None + seq_gap True while feed_warm
    # is still False. The coordinator must NOT halt (regression for the
    # cold-start self-halt that bricked the process before its first quote).
    breakers, ks, _clock = _breakers()
    v = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=None, seq_gap=True, feed_warm=False)
    )
    assert v.tripped is False
    assert not ks.halted


async def test_warm_feed_stale_trips_and_halts_when_sustained() -> None:
    # After warmup (feed_warm True), a stale/None feed + seq gap TRIPS the verdict
    # (the exemption is warmup-only). It is transient, so it holds, then hard-halts
    # once sustained past grace.
    breakers, ks, clock = _breakers()
    v = await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None, seq_gap=True))
    assert v.tripped and v.reason is ReasonCode.HALT_DATA_STALE
    assert not ks.halted  # transient: held
    clock.advance(35.0)  # sustained past the 30s grace
    await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None, seq_gap=True))
    assert ks.halted


async def test_data_stale_precedence_over_later_breakers() -> None:
    # A stale feed trips first even if a later input would also trip — the
    # first-trip contract keeps the halt reason deterministic.
    breakers, ks, _clock = _breakers()
    v = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=None, game_keys={"LEG": None})
    )
    assert v.reason is ReasonCode.HALT_DATA_STALE


async def test_transient_recovers_after_sustained_clear_never_halts() -> None:
    # The WS-reconnect fix: a stale feed that stays fresh for RECOVERY_CLEARS ticks
    # is forgiven, and a much-later stale starts a FRESH timer (not the old blip's).
    breakers, ks, clock = _breakers()
    await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None))  # stale — held
    assert not ks.halted
    clock.advance(10.0)
    await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=1.0))  # clear 1 (held)
    clock.advance(1.0)
    v = await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=1.0))  # clear 2 ⇒ reset
    assert not v.tripped and not ks.halted
    clock.advance(100.0)  # a much later, separate stale
    await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None))
    assert not ks.halted  # fresh timer — ~0s elapsed, the earlier blip was forgiven


async def test_flapping_transient_still_escalates() -> None:
    # bad/clear/bad/clear around the threshold must NOT evade escalation: a SINGLE
    # clear does not reset the timer (RECOVERY_CLEARS=2), so a flapper accumulates.
    breakers, ks, clock = _breakers()
    await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None))  # bad, since=t0
    for _ in range(4):
        clock.advance(15.0)
        await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=1.0))   # clear (streak 1)
        clock.advance(1.0)
        await breakers.evaluate_and_halt(BreakerInputs(rx_age_s=None))  # bad again
    assert ks.halted  # timer accumulated past the 30s grace despite the flapping


async def test_metadata_change_hard_halts_immediately_no_grace() -> None:
    # A rule/metadata change is structural, not transient — hard-halt at once.
    breakers, ks, _clock = _breakers()
    v = await breakers.evaluate_and_halt(
        BreakerInputs(rx_age_s=1.0, tripwire_hit=("S18", "impossible mix"))
    )
    assert v.reason is ReasonCode.HALT_METADATA_CHANGE
    assert ks.halted  # no grace for structural reasons
