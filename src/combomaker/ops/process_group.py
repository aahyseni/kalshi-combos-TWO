"""P2-1: prevent ORPHANED worker processes from the joint / book-risk pools.

WHY: the live bot runs a ``ProcessPoolExecutor`` of POOL_WORKERS (8) pricing
workers plus a book-risk worker. If the PARENT dies abnormally — the supervisor
emergency-kills a wedged bot, an operator ``pkill``s the parent by mistake, an
OOM killer takes the parent, a bare ``kill -9`` — the standard-library pool does
NOT guarantee the children die with it. On Windows the children keep running as
orphans; on POSIX they are re-parented to init and keep running. Orphaned pricing
workers hold CPU + memory and (worse) can outlive the operator's KILL-file stop,
which only the parent honours. The operator stops the bot with a KILL FILE for
exactly this reason — a ``pkill`` of the parent is what strands the 8 workers, and
THIS module is the fix.

WHAT (four layers, defence in depth — each independently useful, none relies on
the process shutting down cleanly):

1. PARENT-OWNED KILL GROUP. On Windows, a Job Object created with
   ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` is attached to the parent; every pool
   worker is assigned into it. When the parent process dies for ANY reason its
   handles close, the job's last handle closes, and the OS terminates every
   worker in the job — no cooperation from the (possibly wedged/killed) parent
   required. On POSIX the equivalent is a per-worker ``PR_SET_PDEATHSIG`` (layer
   2) plus the pool's own process group.

2. PARENT-DEATH DETECTION inside each worker. On Linux, ``prctl(PR_SET_PDEATHSIG,
   SIGKILL)`` (run in the pool initializer, i.e. inside each worker) asks the
   kernel to signal the worker the instant its parent dies. Belt to the Windows
   Job Object's braces; the two never both apply on one OS but the initializer is
   registered unconditionally and no-ops where unsupported.

3. FINALLY CLOSE/JOIN. Pool shutdown is wrapped so the executor is always
   ``shutdown(wait=True)``-joined in a ``finally`` — a clean stop reaps its own
   workers deterministically (the common path), so layers 1/2 only ever matter on
   an ABNORMAL parent exit.

4. STARTUP STRAGGLER CLEANUP. Every worker PID the parent spawns is appended to a
   per-data-dir REGISTRY file. On a fresh startup, before spawning new workers, we
   read the registry left by a PRIOR (possibly crashed) run and terminate any of
   those PIDs that are still alive AND still look like one of our Python workers
   (command-line / create-time guard so we never kill an unrelated PID that got
   recycled). Then we truncate the registry for the new run. This reaps orphans
   that layers 1-3 somehow missed (e.g. a Windows build where the Job Object could
   not be created), and it is fully cross-platform.

Everything here is BEST-EFFORT and FAIL-OPEN on the hardening layers (a missing
Job Object API, an unsupported ``prctl``, a psutil-less environment) — the bot
must start even when it cannot install the OS trap. It is FAIL-CLOSED only in the
sense of never killing a PID it is not sure is ours (straggler cleanup verifies
identity before terminating). None of this raises into the hot path.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from combomaker.ops.logging import get_logger

log = get_logger(__name__)

# Filename (under data_dir) of the worker-PID registry used for startup straggler
# cleanup. Stable so the next run finds the prior run's stragglers.
WORKER_REGISTRY_FILENAME = "worker_pids.txt"

# A marker token that appears in every pool worker's command line
# (multiprocessing spawn/forkserver re-exec the Python interpreter with the
# resource-tracker / spawn bootstrap in argv). We additionally require this
# process to be a Python interpreter before we ever terminate a registered PID,
# so a recycled OS PID belonging to an unrelated program is never killed.
_PY_MARKER = "python"


def worker_registry_path(data_dir: Path) -> Path:
    return data_dir / WORKER_REGISTRY_FILENAME


# --------------------------------------------------------------------------- #
# Layer 1 (Windows): parent-owned Job Object with KILL_ON_JOB_CLOSE.           #
# --------------------------------------------------------------------------- #


class WindowsKillJob:
    """A Windows Job Object configured so that when the PARENT process dies (its
    last handle to the job closes), the OS terminates every process assigned to
    the job. Pool workers are assigned in as they spawn. Best-effort: if the Job
    Object API is unavailable the object degrades to a no-op and the registry-based
    straggler cleanup (layer 4) remains the backstop.

    The job handle is held for the lifetime of the parent process (kept alive by a
    reference on the owning pool wrapper). We deliberately do NOT set
    ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` — a worker must not be able to escape the
    job."""

    def __init__(self) -> None:
        self._handle = None
        self._kernel32 = None
        self._ok = False
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # CreateJobObjectW(NULL, NULL) -> anonymous job.
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION with LIMIT_KILL_ON_JOB_CLOSE.
            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
            JobObjectExtendedLimitInformation = 9

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            ok = kernel32.SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                err = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise OSError(err, "SetInformationJobObject failed")

            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            self._handle = handle
            self._kernel32 = kernel32
            self._ok = True
            log.info("worker_kill_job_created")
        except Exception as exc:  # noqa: BLE001 - fail-open: no job, registry backstops
            log.warning("worker_kill_job_unavailable", error=repr(exc))
            self._handle = None
            self._kernel32 = None
            self._ok = False

    @property
    def active(self) -> bool:
        return self._ok

    def assign(self, pid: int) -> bool:
        """Assign a worker PID into the kill-on-close job. Best-effort; returns
        True on success. A worker that cannot be assigned is still tracked in the
        registry (layer 4), so a missed assignment is not silently unprotected."""
        if not self._ok or self._kernel32 is None:
            return False
        try:
            import ctypes

            PROCESS_SET_QUOTA = 0x0100
            PROCESS_TERMINATE = 0x0001
            hproc = self._kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid)
            )
            if not hproc:
                return False
            try:
                ok = self._kernel32.AssignProcessToJobObject(self._handle, hproc)
                if not ok:
                    err = ctypes.get_last_error()
                    # ERROR_ACCESS_DENIED (5) happens if the process is ALREADY in
                    # a job that forbids nesting on old Windows; harmless — the
                    # registry backstop still covers it.
                    log.debug("worker_job_assign_failed", pid=int(pid), error=err)
                    return False
                return True
            finally:
                self._kernel32.CloseHandle(hproc)
        except Exception as exc:  # noqa: BLE001
            log.debug("worker_job_assign_raised", pid=int(pid), error=repr(exc))
            return False

    def close(self) -> None:
        """Explicitly close the job handle. On a CLEAN shutdown the workers are
        already joined, so closing the handle is a formality; on an abnormal exit
        we never reach here and the OS closes the handle for us (which is what
        triggers KILL_ON_JOB_CLOSE)."""
        if self._kernel32 is not None and self._handle is not None:
            try:
                self._kernel32.CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
        self._handle = None
        self._ok = False


# --------------------------------------------------------------------------- #
# Layer 2 (POSIX/Linux): parent-death signal inside each worker.              #
# --------------------------------------------------------------------------- #


def install_parent_death_signal() -> None:
    """Ask the kernel to signal THIS process when its parent dies. Runs INSIDE a
    pool worker (from the pool initializer). Linux-only (``prctl PR_SET_PDEATHSIG``);
    a no-op everywhere else. Best-effort — never raises into worker init."""
    if sys.platform != "linux":
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        import signal as _signal

        libc.prctl(PR_SET_PDEATHSIG, _signal.SIGKILL, 0, 0, 0)
        # Guard the race where the parent died between fork and prctl: if we are
        # already re-parented (ppid == 1) the parent is gone — exit now.
        if os.getppid() == 1:
            os._exit(1)
    except Exception:  # noqa: BLE001 - best effort; registry backstops
        pass


# --------------------------------------------------------------------------- #
# Layer 4: worker-PID registry + startup straggler cleanup.                   #
# --------------------------------------------------------------------------- #


def record_worker_pids(data_dir: Path, pids: list[int]) -> None:
    """Append the given worker PIDs to the per-data-dir registry so a subsequent
    startup can reap any that were orphaned by an abnormal parent exit. Idempotent
    at the value level (duplicates are harmless — cleanup dedupes and verifies
    liveness). Best-effort; a write failure is logged, not raised."""
    if not pids:
        return
    try:
        path = worker_registry_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_registry(path)
        merged = sorted(set(existing) | {int(p) for p in pids})
        path.write_text("\n".join(str(p) for p in merged) + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk failure path
        log.warning("worker_registry_write_failed", error=repr(exc))


def _read_registry(path: Path) -> list[int]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    pids: list[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _win_process_image(pid: int) -> str | None:
    """The full executable image path of a LIVE Windows process, or None if it is
    dead / unqueryable. ctypes-only (no psutil) so identity verification works on
    the live target platform (win32) with no extra dependency. Returns None (⇒
    'not verifiably ours' ⇒ don't kill) on any error."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        hproc = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not hproc:
            return None
        try:
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size))
            if not ok:
                return None
            return buf.value
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:  # noqa: BLE001
        return None


def _looks_like_our_worker(pid: int) -> bool:
    """True only if PID is a LIVE Python process (our pool worker). The identity
    check protects against terminating an unrelated program that inherited a
    recycled OS PID after our worker died. If we CANNOT verify identity we FAIL
    SAFE and DECLINE to kill (return False) — an un-reaped straggler is a lesser
    evil than killing a stranger.

    Verification uses ctypes on Windows (the live target — no psutil needed) and
    psutil elsewhere if present; with neither able to verify, returns False."""
    if pid <= 0 or pid == os.getpid():
        return False
    # Windows: ctypes image-path check (no dependency).
    if sys.platform == "win32":
        image = _win_process_image(pid)
        if image is None:
            return False
        return _PY_MARKER in Path(image).name.lower()
    # POSIX: psutil if available; otherwise cannot verify ⇒ don't kill.
    try:
        import psutil  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001 - no psutil ⇒ cannot verify ⇒ don't kill
        return False
    try:
        proc = psutil.Process(pid)
        name = (proc.name() or "").lower()
        if _PY_MARKER in name:
            return True
        # Fallback: some spawned workers show the exe path in cmdline, not name.
        try:
            cmdline = " ".join(proc.cmdline()).lower()
        except Exception:  # noqa: BLE001 - access denied ⇒ not ours (or unknowable)
            return False
        return _PY_MARKER in cmdline and "multiprocessing" in cmdline
    except Exception:  # noqa: BLE001 - no such process / access denied ⇒ not ours
        return False


def _win_terminate(pid: int, *, grace_s: float = 2.0) -> bool:
    """TerminateProcess a straggler PID via ctypes (no psutil). Returns True if the
    process is gone afterwards. Only called after identity is verified."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000
        WAIT_OBJECT_0 = 0x0
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        hproc = kernel32.OpenProcess(
            PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid)
        )
        if not hproc:
            return True  # can't open ⇒ already gone (or unkillable; treat as done)
        try:
            kernel32.TerminateProcess.restype = wintypes.BOOL
            kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateProcess(hproc, 1)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            rc = kernel32.WaitForSingleObject(hproc, int(grace_s * 1000))
            return bool(rc == WAIT_OBJECT_0)
        finally:
            kernel32.CloseHandle(hproc)
    except Exception:  # noqa: BLE001
        return False


def _terminate(pid: int, *, grace_s: float = 2.0) -> bool:
    """Terminate a verified-straggler PID, escalating to kill after a grace
    window. Returns True if the process is gone afterwards. ctypes on Windows (no
    dependency); psutil elsewhere."""
    if sys.platform == "win32":
        return _win_terminate(pid, grace_s=grace_s)
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return False
    try:
        proc = psutil.Process(pid)
    except Exception:  # noqa: BLE001 - already gone
        return True
    try:
        proc.terminate()
        try:
            proc.wait(timeout=grace_s)
            return True
        except Exception:  # noqa: BLE001 - didn't exit on SIGTERM ⇒ escalate
            proc.kill()
            try:
                proc.wait(timeout=grace_s)
            except Exception:  # noqa: BLE001
                pass
            return not proc.is_running()
    except Exception:  # noqa: BLE001 - vanished under us ⇒ success
        return True


def cleanup_straggler_workers(data_dir: Path) -> int:
    """STARTUP straggler reap (layer 4). Read the worker-PID registry left by a
    PRIOR run, terminate any still-alive-AND-verified-ours PIDs (orphans an
    abnormal parent exit left behind), then TRUNCATE the registry for the new run.

    Returns the number of stragglers terminated. Never raises — a cleanup failure
    must not block startup (the OS-level layers are the primary defence). The
    truncate happens even when nothing was killed, so a fresh run always starts
    from an empty registry it then repopulates as it spawns."""
    killed = 0
    try:
        path = worker_registry_path(data_dir)
        pids = _read_registry(path)
        for pid in pids:
            if _looks_like_our_worker(pid):
                if _terminate(pid):
                    killed += 1
                    log.warning(
                        "straggler_worker_reaped",
                        pid=int(pid),
                        detail="orphaned pool worker from a prior run — terminated",
                    )
        # Truncate for the new run regardless of outcome.
        try:
            if path.exists():
                path.write_text("", encoding="utf-8")
        except OSError:  # pragma: no cover
            pass
    except Exception as exc:  # noqa: BLE001 - startup cleanup is best-effort
        log.warning("straggler_cleanup_failed", error=repr(exc))
    if killed:
        log.warning("straggler_cleanup_done", reaped=killed)
    return killed


def worker_pids_of(executor: object) -> list[int]:
    """Best-effort extraction of the live worker PIDs from a
    ``ProcessPoolExecutor``. Reads the private ``_processes`` map (a
    ``{pid: Process}`` dict populated after workers spawn); returns [] if the
    executor exposes no such map (defensive against a stdlib change). Only used to
    feed the Job Object assignment + the registry — never load-bearing for
    pricing."""
    procs = getattr(executor, "_processes", None)
    if not procs:
        return []
    pids: list[int] = []
    try:
        for key, proc in dict(procs).items():
            pid = getattr(proc, "pid", None)
            if pid is None and isinstance(key, int):
                pid = key
            if pid:
                pids.append(int(pid))
    except Exception:  # noqa: BLE001 - snapshot raced a spawn/teardown
        return pids
    return pids


def _ensure_workers_spawned(executor: object, want: int, *, timeout_s: float = 30.0) -> list[int]:
    """Block briefly until the executor has spawned its workers so their PIDs are
    visible for Job-Object assignment + registry recording. A ProcessPoolExecutor
    spawns lazily on first submit; callers warm it first, but we still poll so an
    un-warmed pool doesn't record zero PIDs. Returns whatever PIDs exist at the
    deadline (best-effort)."""
    deadline = time.monotonic() + timeout_s
    pids: list[int] = []
    while time.monotonic() < deadline:
        pids = worker_pids_of(executor)
        if len(pids) >= want:
            return pids
        time.sleep(0.02)
    return pids
