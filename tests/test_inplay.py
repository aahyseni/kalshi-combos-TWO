"""Tests for combomaker.risk.inplay — market-based in-play/velocity detection.

All timing runs on FakeClock (deterministic). Defaults under test:
velocity_window_s=5.0, velocity_threshold_cc=300 (strict >),
update_count_threshold=25 (strict >), cooldown_s=30.0 (anomalous while
now < until, i.e. exactly-at-expiry is clean).
"""

from __future__ import annotations

from combomaker.core.clock import FakeClock
from combomaker.core.money import CentiCents
from combomaker.risk.inplay import InPlayDetector, InPlayPolicy


def make_detector(
    policy: InPlayPolicy | None = None,
) -> tuple[FakeClock, InPlayDetector]:
    clock = FakeClock()
    return clock, InPlayDetector(clock, policy)


def test_calm_mids_within_window_no_anomaly() -> None:
    clock, det = make_detector()
    for mid in (5_000, 5_100, 5_200):  # range 200 <= 300 threshold
        det.note_mid("T", CentiCents(mid))
        clock.advance(1.0)
    assert det.velocity_anomaly("T") is False


def test_range_exactly_at_threshold_no_anomaly() -> None:
    clock, det = make_detector()
    det.note_mid("T", CentiCents(5_000))
    clock.advance(0.5)
    det.note_mid("T", CentiCents(5_300))  # range == 300, strict > required
    assert det.velocity_anomaly("T") is False


def test_velocity_spike_triggers_and_cooldown_holds_then_expires() -> None:
    clock, det = make_detector()
    det.note_mid("T", CentiCents(5_000))
    clock.advance(1.0)
    det.note_mid("T", CentiCents(5_301))  # range 301 > 300 ⇒ anomalous
    assert det.velocity_anomaly("T") is True

    clock.advance(29.9)  # just under the 30s cooldown
    assert det.velocity_anomaly("T") is True

    clock.advance(0.2)  # past cooldown expiry
    assert det.velocity_anomaly("T") is False

    # Calm mid after expiry: the spike aged out of the 5s window long ago,
    # so this must not re-trigger.
    det.note_mid("T", CentiCents(5_301))
    assert det.velocity_anomaly("T") is False


def test_exactly_at_cooldown_expiry_is_clean() -> None:
    clock, det = make_detector()
    det.note_mid("T", CentiCents(5_000))
    det.note_mid("T", CentiCents(5_400))  # same instant, range 400 ⇒ anomalous
    assert det.velocity_anomaly("T") is True
    clock.advance(30.0)  # now == anomalous_until_ns; source uses strict <
    assert det.velocity_anomaly("T") is False


def test_update_count_over_threshold_triggers_on_tiny_moves() -> None:
    policy = InPlayPolicy(update_count_threshold=5)
    clock, det = make_detector(policy)
    for _ in range(5):  # count == threshold: no anomaly yet (strict >)
        det.note_mid("T", CentiCents(5_000))
        clock.advance(0.1)
    assert det.velocity_anomaly("T") is False
    det.note_mid("T", CentiCents(5_000))  # 6th update within window ⇒ anomalous
    assert det.velocity_anomaly("T") is True


def test_big_move_aged_out_of_window_does_not_trigger() -> None:
    clock, det = make_detector()
    det.note_mid("T", CentiCents(5_000))
    clock.advance(6.0)  # > velocity_window_s: first mid is trimmed
    det.note_mid("T", CentiCents(5_400))  # alone in window ⇒ no range to compute
    assert det.velocity_anomaly("T") is False


def test_mid_exactly_at_window_edge_still_counts() -> None:
    clock, det = make_detector()
    det.note_mid("T", CentiCents(5_000))
    clock.advance(5.0)  # first mid sits exactly at the horizon; trim is strict <
    det.note_mid("T", CentiCents(5_400))
    assert det.velocity_anomaly("T") is True


def test_tickers_are_independent() -> None:
    _clock, det = make_detector()
    det.note_mid("HOT", CentiCents(5_000))
    det.note_mid("HOT", CentiCents(6_000))  # anomalous
    det.note_mid("COLD", CentiCents(5_000))
    det.note_mid("COLD", CentiCents(5_010))
    assert det.velocity_anomaly("HOT") is True
    assert det.velocity_anomaly("COLD") is False


def test_any_anomalous_and_unknown_ticker() -> None:
    _clock, det = make_detector()
    det.note_mid("HOT", CentiCents(5_000))
    det.note_mid("HOT", CentiCents(6_000))  # anomalous
    assert det.any_anomalous(["HOT", "COLD"]) is True
    assert det.any_anomalous(["COLD"]) is False
    assert det.any_anomalous(["NEVER_SEEN"]) is False
    assert det.any_anomalous([]) is False
    assert det.velocity_anomaly("NEVER_SEEN") is False
