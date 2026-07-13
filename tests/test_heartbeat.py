"""Phase 6: cross-process heartbeat + needs_reconcile marker."""

from __future__ import annotations

from pathlib import Path

from combomaker.core.clock import FakeClock
from combomaker.risk.heartbeat import Heartbeat, HeartbeatReader, ReconcileMarker


def test_beat_writes_readable_fresh_age(tmp_path: Path) -> None:
    clock = FakeClock()
    hb = Heartbeat(clock, tmp_path / "heartbeat.txt")
    hb.beat()
    reader = HeartbeatReader(clock, tmp_path / "heartbeat.txt")
    age = reader.read_age_s()
    assert age is not None
    assert age == 0.0  # same clock instant


def test_age_grows_with_reader_clock(tmp_path: Path) -> None:
    write_clock = FakeClock()
    hb = Heartbeat(write_clock, tmp_path / "heartbeat.txt")
    hb.beat()
    # Reader clock advances relative to the written wall timestamp.
    read_clock = FakeClock()
    read_clock.advance(12.0)
    reader = HeartbeatReader(read_clock, tmp_path / "heartbeat.txt")
    age = reader.read_age_s()
    assert age is not None
    assert abs(age - 12.0) < 1e-6


def test_missing_heartbeat_is_wedged(tmp_path: Path) -> None:
    reader = HeartbeatReader(FakeClock(), tmp_path / "nope.txt")
    assert reader.read_age_s() is None  # fail-closed: cannot establish liveness
    assert reader.is_wedged(timeout_s=15.0) is True


def test_corrupt_heartbeat_is_wedged(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.txt"
    path.write_text("not-a-timestamp", encoding="utf-8")
    reader = HeartbeatReader(FakeClock(), path)
    assert reader.read_age_s() is None
    assert reader.is_wedged(timeout_s=15.0) is True


def test_naive_timestamp_rejected(tmp_path: Path) -> None:
    # A tz-naive timestamp is ambiguous across processes ⇒ fail-closed.
    path = tmp_path / "heartbeat.txt"
    path.write_text("2026-01-01T00:00:00", encoding="utf-8")
    reader = HeartbeatReader(FakeClock(), path)
    assert reader.read_age_s() is None


def test_future_heartbeat_rejected(tmp_path: Path) -> None:
    write_clock = FakeClock()
    write_clock.advance(10_000.0)  # a beat far in the future vs the reader
    Heartbeat(write_clock, tmp_path / "heartbeat.txt").beat()
    reader = HeartbeatReader(FakeClock(), tmp_path / "heartbeat.txt")
    assert reader.read_age_s() is None  # implausible future ⇒ stale (tamper guard)


def test_wedged_at_threshold_boundary(tmp_path: Path) -> None:
    write_clock = FakeClock()
    Heartbeat(write_clock, tmp_path / "heartbeat.txt").beat()
    read_clock = FakeClock()
    reader = HeartbeatReader(read_clock, tmp_path / "heartbeat.txt")
    read_clock.advance(15.0)
    assert reader.is_wedged(timeout_s=15.0) is False  # exactly at, not over
    read_clock.advance(0.01)
    assert reader.is_wedged(timeout_s=15.0) is True   # strictly over


def test_reconcile_marker_set_clear_roundtrip(tmp_path: Path) -> None:
    marker = ReconcileMarker(tmp_path / "needs_reconcile")
    assert marker.is_set() is False
    marker.set("hard trip")
    assert marker.is_set() is True
    marker.clear()
    assert marker.is_set() is False
    marker.clear()  # idempotent


def test_reconcile_marker_survives_new_instance(tmp_path: Path) -> None:
    # The marker is on disk, so a *restarted* bot (new instance) still sees it.
    ReconcileMarker(tmp_path / "needs_reconcile").set("supervisor kill")
    fresh = ReconcileMarker(tmp_path / "needs_reconcile")
    assert fresh.is_set() is True


def test_beat_overwrites_previous(tmp_path: Path) -> None:
    clock = FakeClock()
    hb = Heartbeat(clock, tmp_path / "heartbeat.txt")
    hb.beat()
    clock.advance(5.0)
    hb.beat()
    reader = HeartbeatReader(clock, tmp_path / "heartbeat.txt")
    assert reader.read_age_s() == 0.0  # reads the LATEST beat, not the first
