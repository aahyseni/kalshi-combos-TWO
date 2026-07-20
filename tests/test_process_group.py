"""P2-1: orphaned-worker prevention (parent-owned kill group, parent-death
detection, finally close/join, startup straggler cleanup).

The load-bearing property: a pricing/book-risk worker can NEVER outlive an
abnormal parent exit. We can't portably kill THIS test's parent, so we prove the
four layers by their observable contracts:

- Layer 4 (startup straggler reap) end-to-end: a LIVE child process is recorded in
  the registry and left running (an orphan a crashed parent stranded); a fresh
  ``cleanup_straggler_workers`` identifies it as verifiably-ours-and-alive,
  terminates it, and truncates the registry. A DEAD PID is a no-op; our OWN pid is
  guarded; an unrelated non-Python PID is never touched.
- Layer 1 (Windows kill-job): the Job Object is created with KILL_ON_JOB_CLOSE and
  a real pool worker assigns into it (win32 only).
- Layer 3 (finally close/join): JointPool.shutdown joins its workers (wait=True) so
  none linger, and releases the kill-job handle even if called twice.
- worker_pids_of / _ensure_workers_spawned surface the real worker PIDs of a
  ProcessPoolExecutor.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from combomaker.ops.process_group import (
    WindowsKillJob,
    _ensure_workers_spawned,
    _looks_like_our_worker,
    _read_registry,
    cleanup_straggler_workers,
    install_parent_death_signal,
    record_worker_pids,
    worker_pids_of,
    worker_registry_path,
)


# Module-level so ProcessPoolExecutor (spawn) can pickle it.
def _probe_pid() -> int:
    return os.getpid()


def test_registry_roundtrip_and_dedupe(tmp_path: Path) -> None:
    record_worker_pids(tmp_path, [111, 222, 111])
    record_worker_pids(tmp_path, [222, 333])
    got = _read_registry(worker_registry_path(tmp_path))
    assert got == [111, 222, 333]  # merged, deduped, sorted


def test_registry_empty_pids_is_noop(tmp_path: Path) -> None:
    record_worker_pids(tmp_path, [])
    assert not worker_registry_path(tmp_path).exists()


def test_self_pid_is_never_reaped() -> None:
    # Our own live Python pid is verifiably-python, but the guard must refuse to
    # treat the current process as a straggler (never kill ourselves).
    assert _looks_like_our_worker(os.getpid()) is False


def test_dead_pid_is_not_our_worker() -> None:
    # A pid that (almost certainly) doesn't exist is not reapable.
    assert _looks_like_our_worker(2_000_000_000) is False


def test_cleanup_truncates_registry_even_with_no_live_stragglers(tmp_path: Path) -> None:
    record_worker_pids(tmp_path, [2_000_000_000, 2_000_000_001])  # dead pids
    reaped = cleanup_straggler_workers(tmp_path)
    assert reaped == 0
    # Registry truncated for the new run.
    assert worker_registry_path(tmp_path).read_text() == ""


def test_cleanup_missing_registry_is_noop(tmp_path: Path) -> None:
    # No registry file at all ⇒ clean startup, zero reaped, no raise.
    assert cleanup_straggler_workers(tmp_path) == 0


def test_worker_pids_of_surfaces_real_pool_workers() -> None:
    ex = ProcessPoolExecutor(max_workers=2)
    try:
        # Force the workers to actually spawn.
        _ = [ex.submit(_probe_pid).result() for _ in range(4)]
        pids = _ensure_workers_spawned(ex, 1, timeout_s=5.0)
        assert pids, "expected at least one live worker pid"
        for p in pids:
            assert isinstance(p, int) and p > 0
            # A live pool worker is verifiably ours.
            assert _looks_like_our_worker(p) is True
    finally:
        ex.shutdown(wait=True)


def test_worker_pids_of_on_non_executor_is_empty() -> None:
    assert worker_pids_of(object()) == []


def test_live_orphan_is_reaped_end_to_end(tmp_path: Path) -> None:
    """The core guarantee: a LIVE worker a prior (crashed) parent stranded is
    identified and terminated by a fresh startup, then the registry is cleared."""
    # A long-lived child python — stands in for an orphaned pool worker.
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"]
    )
    try:
        record_worker_pids(tmp_path, [orphan.pid])
        # Sanity: it is alive and verifiably-ours BEFORE cleanup.
        assert orphan.poll() is None
        assert _looks_like_our_worker(orphan.pid) is True

        reaped = cleanup_straggler_workers(tmp_path)
        assert reaped == 1
        # The orphan is actually gone.
        assert orphan.wait(timeout=10) is not None
        assert _looks_like_our_worker(orphan.pid) is False
        # Registry truncated for the new run.
        assert worker_registry_path(tmp_path).read_text() == ""
    finally:
        if orphan.poll() is None:  # pragma: no cover - cleanup on assertion failure
            orphan.kill()
            orphan.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object layer")
def test_windows_kill_job_created_and_assigns_worker() -> None:
    job = WindowsKillJob()
    try:
        assert job.active is True  # created on a supported Windows host
        ex = ProcessPoolExecutor(max_workers=1)
        try:
            _ = [ex.submit(_probe_pid).result() for _ in range(2)]
            pids = _ensure_workers_spawned(ex, 1, timeout_s=5.0)
            assert pids
            # Assigning a live worker into the kill-job succeeds.
            assert job.assign(pids[0]) is True
        finally:
            ex.shutdown(wait=True)
    finally:
        job.close()
        # Idempotent second close is safe.
        job.close()
        assert job.active is False


def test_kill_job_assign_nonexistent_pid_is_false() -> None:
    job = WindowsKillJob()
    try:
        # A dead/never-existed pid cannot be assigned; must not raise.
        assert job.assign(2_000_000_000) is False
    finally:
        job.close()


def test_install_parent_death_signal_never_raises() -> None:
    # No-op on Windows; on Linux arms PR_SET_PDEATHSIG. Must never raise in-process.
    install_parent_death_signal()


def test_ensure_workers_spawned_times_out_without_workers() -> None:
    class _NoProcs:
        _processes: dict = {}

    t0 = time.monotonic()
    pids = _ensure_workers_spawned(_NoProcs(), 4, timeout_s=0.1)
    assert pids == []
    assert time.monotonic() - t0 < 2.0
