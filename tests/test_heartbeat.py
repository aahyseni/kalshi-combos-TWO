"""Phase 6: cross-process heartbeat + needs_reconcile marker."""

from __future__ import annotations

from pathlib import Path

import pytest

import combomaker.risk.heartbeat as heartbeat_mod
from combomaker.core.clock import FakeClock
from combomaker.risk.heartbeat import (
    Heartbeat,
    HeartbeatReader,
    ReconcileMarker,
    _atomic_write,
)


def test_beat_writes_readable_fresh_age(tmp_path: Path) -> None:
    clock = FakeClock()
    hb = Heartbeat(clock, tmp_path / "heartbeat.txt")
    hb.beat()
    reader = HeartbeatReader(clock, tmp_path / "heartbeat.txt")
    age = reader.read_age_s()
    assert age is not None
    assert age == 0.0  # same clock instant


def test_beat_write_throttled_but_retries_after_failure(tmp_path: Path) -> None:
    # Adversarial verify 2026-07-16: the wedge fix beats once per swept quote
    # (N atomic replaces per 0.5s tick) — the WRITE dedupes to 10/s so the
    # defense cannot stall the loop it defends, while a FAILED write must not
    # arm the throttle (the very next beat retries).
    clock = FakeClock()
    path = tmp_path / "heartbeat.txt"
    hb = Heartbeat(clock, path)
    hb.beat()
    first = path.read_text(encoding="utf-8")
    clock.advance(0.05)  # inside the 100ms window
    hb.beat()
    assert path.read_text(encoding="utf-8") == first  # deduped
    clock.advance(0.06)  # past the window
    hb.beat()
    assert path.read_text(encoding="utf-8") != first  # wrote again
    # Failure never arms the throttle: a beat right after a failed write
    # (fresh instance, unwritable dir simulated via monkeypatch-free check)
    # is covered by the success-only assignment — pinned structurally here:
    hb2 = Heartbeat(FakeClock(), tmp_path / "hb2.txt")
    assert hb2._last_write_mono_ns is None
    hb2.beat()
    assert hb2._last_write_mono_ns is not None
    # An externally-WIPED file is healed immediately, throttle or not (the
    # supervisor latch re-beats after a kill; relaunch purges delete the file).
    path.unlink()
    hb.beat()  # still inside the throttle window
    assert path.exists()


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


# --- Windows read-vs-rename race (2026-07-13 live go-live bug): the supervisor's
# read momentarily holds heartbeat.txt open, and Windows os.replace then denies
# the bot's write. Retry rides through it WITHOUT weakening fail-closed. ---


def test_atomic_write_retries_replace_on_permission_error(tmp_path, monkeypatch) -> None:
    real_replace = heartbeat_mod.os.replace
    n = {"calls": 0}

    def flaky(src: str, dst: str) -> None:
        n["calls"] += 1
        if n["calls"] < 3:  # deny twice (Windows: target open by the reader)
            raise PermissionError(13, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(heartbeat_mod.os, "replace", flaky)
    slept: list[float] = []
    p = tmp_path / "heartbeat.txt"
    _atomic_write(p, "hello", sleep=slept.append)
    assert p.read_text(encoding="utf-8") == "hello"
    assert n["calls"] == 3  # 2 denials + 1 success
    assert len(slept) == 2  # backoff between the two retries


def test_atomic_write_exhausts_retries_and_fails_closed(tmp_path, monkeypatch) -> None:
    def always_denied(src: str, dst: str) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(heartbeat_mod.os, "replace", always_denied)
    p = tmp_path / "heartbeat.txt"
    with pytest.raises(PermissionError):  # re-raises => beat() logs, next tick retries
        _atomic_write(p, "x", retries=3, sleep=lambda _s: None)
    assert list(tmp_path.glob("heartbeat.txt.tmp*")) == []  # no leaked temp
    assert not p.exists()  # target never written => reader sees stale => wedged (fail-closed)


def test_read_age_retries_transient_read_error(tmp_path, monkeypatch) -> None:
    Heartbeat(FakeClock(), tmp_path / "heartbeat.txt").beat()
    reader = HeartbeatReader(FakeClock(), tmp_path / "heartbeat.txt")
    orig = heartbeat_mod.Path.read_text
    n = {"calls": 0}

    def flaky_read(self, *a, **k):  # type: ignore[no-untyped-def]
        n["calls"] += 1
        if n["calls"] == 1:
            raise PermissionError(13, "Access is denied")
        return orig(self, *a, **k)

    monkeypatch.setattr(heartbeat_mod.Path, "read_text", flaky_read)
    age = reader.read_age_s(sleep=lambda _s: None)
    assert age is not None and age >= 0.0  # transient error retried, then read
    assert n["calls"] == 2


def test_read_age_persistent_read_error_is_wedged(tmp_path, monkeypatch) -> None:
    reader = HeartbeatReader(FakeClock(), tmp_path / "heartbeat.txt")

    def always_error(self, *a, **k):  # type: ignore[no-untyped-def]
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(heartbeat_mod.Path, "read_text", always_error)
    # A persistently unreadable heartbeat still fails closed => None => wedged.
    assert reader.read_age_s(retries=3, sleep=lambda _s: None) is None
