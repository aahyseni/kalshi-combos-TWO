"""Main-loop relief from the READER-SIDE RAW PRE-FILTER, measured on the REAL
transport (rule 8: drive the live module, never reimplement it).

    PYTHONPATH=src .venv/Scripts/python.exe tools/bench_ws_prefilter.py [--reps 3] [--frames 20000]

Builds a wire-shaped corpus from the real-frame fixture
(``tests/fixtures/ws_rfq_frames_corpus_20260905.jsonl``) at the MEASURED mix
(2026-09-05 live: 49.5 % ``rfq_deleted``, 50.5 % ``rfq_created`` of which
~1.5 % carry an allowlisted series), pre-serialized so the reader sees the
text the exchange would send, and pushes it through ``WsManager`` (reader
thread → lanes → dispatcher) + ``RfqIntake`` with the live allowlist — once
with the pre-filter OFF (today's path) and once ON. Reports, per 1,000 frames:
main-thread CPU (the dispatcher + intake = the event loop's thread), reader
thread busy time, and wall time. Also a micro-benchmark of ``judge`` vs
``json.loads`` on the same texts.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import math
import random
import statistics
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from combomaker.core.clock import SystemClock  # noqa: E402
from combomaker.exchange.ws import WsManager  # noqa: E402
from combomaker.exchange.ws_prefilter import RawSeriesPrefilter  # noqa: E402
from combomaker.ops.metrics import Metrics  # noqa: E402
from combomaker.rfq.intake import RfqIntake  # noqa: E402
from combomaker.rfq.models import Rfq  # noqa: E402
from tests.test_ws_prefilter import FOREIGN_SERIES, LIVE_ALLOWLIST, _foreign_variant  # noqa: E402

CORPUS = REPO / "tests" / "fixtures" / "ws_rfq_frames_corpus_20260905.jsonl"
DELETED_SHARE = 0.495  # measured wire mix (report)
ALLOWLISTED_SHARE_OF_CREATED = 0.015  # measured (report)


class _TextFrame:
    __slots__ = ("type", "data")

    def __init__(self, raw: str) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = raw


class _TextSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames: collections.deque[_TextFrame] = collections.deque(
            _TextFrame(r) for r in frames
        )
        self._lock = threading.Lock()
        self.closed = False
        self.sent: list[str] = []

    def __aiter__(self) -> _TextSocket:
        return self

    async def __anext__(self) -> _TextFrame:
        while True:
            if self.closed:
                raise StopAsyncIteration
            with self._lock:
                frame = self._frames.popleft() if self._frames else None
            if frame is not None:
                return frame
            await asyncio.sleep(0.001)

    async def close(self) -> None:
        self.closed = True

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


class _Ctx:
    def __init__(self, sock: _TextSocket) -> None:
        self._sock = sock

    async def __aenter__(self) -> _TextSocket:
        return self._sock

    async def __aexit__(self, *exc: object) -> bool:
        self._sock.closed = True
        return False


class _Signer:
    def headers(self, method: str, path: str) -> dict[str, str]:
        return {}


def build_corpus(n_frames: int, seed: int = 20260905) -> tuple[list[str], dict[str, int]]:
    rng = random.Random(seed)
    lines = CORPUS.read_text(encoding="utf-8").splitlines()
    recs = [json.loads(line) for line in lines if line.strip()]
    current = [r["msg"] for r in recs if r["era"] == "2026-09-05"]
    now = datetime.now(UTC).isoformat()
    frames: list[str] = []
    counts = {"rfq_deleted": 0, "created_allowlisted": 0, "created_foreign": 0}
    for i in range(n_frames):
        u = rng.random()
        if u < DELETED_SHARE:
            msg = {
                "id": f"del-{i}",
                "creator_id": "c" * 64,
                "market_ticker": f"{rng.choice(FOREIGN_SERIES)}-26SEP05-X",
                "event_ticker": f"{rng.choice(FOREIGN_SERIES)}-26SEP05",
                "target_cost_dollars": "10.0000",
                "deleted_ts": now,
            }
            frames.append(json.dumps({"type": "rfq_deleted", "sid": 15, "msg": msg}))
            counts["rfq_deleted"] += 1
            continue
        src = dict(rng.choice(current))
        src["id"] = f"rfq-{i}"
        src["created_ts"] = now
        if rng.random() < ALLOWLISTED_SHARE_OF_CREATED:
            counts["created_allowlisted"] += 1
        else:
            src = _foreign_variant(src, rng, legs_to_swap=None)
            counts["created_foreign"] += 1
        frames.append(json.dumps({"type": "rfq_created", "sid": 15, "msg": src}))
    return frames, counts


async def run_once(frames: list[str], *, prefilter: bool) -> dict[str, float]:
    metrics = Metrics()
    sock = _TextSocket(frames)
    sockets = [sock]

    def connect(session: Any, url: str, headers: dict[str, str]) -> _Ctx:
        if not sockets:
            raise ConnectionError("no more sockets")
        return _Ctx(sockets.pop(0))

    m = WsManager(
        "wss://example/ws",
        _Signer(),  # type: ignore[arg-type]
        SystemClock(),
        metrics,
        name="b",
        backoff_initial_s=0.01,
        backoff_max_s=0.02,
        connect=connect,
    )
    m.mark_priority("quote_accepted", "quote_executed")
    m.mark_sheddable("rfq_created", stale_after_s=600.0)
    m.mark_sheddable("rfq_deleted")
    if prefilter:
        m.set_raw_prefilter(RawSeriesPrefilter(LIVE_ALLOWLIST))
    intake = RfqIntake(m, metrics, series_prefixes=LIVE_ALLOWLIST)
    seen: list[str] = []

    async def on_rfq(rfq: Rfq) -> None:
        seen.append(rfq.rfq_id)

    intake.on_rfq(on_rfq)
    n = len(frames)
    # NOTE: ``time.thread_time`` is tick-sampled on Windows (~15.6 ms quanta
    # charged to whichever thread is running at the tick) — useless for a
    # thread that runs in µs bursts; the whole-process CPU is the honest
    # figure here, and ``dispatch_cost`` below measures the main-loop path
    # per frame class with perf_counter.
    proc0 = time.process_time()
    wall0 = time.perf_counter()
    m.start()
    while (  # noqa: ASYNC110 — bounded by the finite frame list
        m._frames_read < n or any(m.lane_depths().values()) or m._lanes.wake_pending
    ):
        await asyncio.sleep(0.002)
    wall1 = time.perf_counter()
    proc1 = time.process_time()
    reader_busy_ns = m._busy_ns
    await m.stop()
    return {
        "frames": n,
        "process_cpu_ms_per_1k": (proc1 - proc0) * 1e3 / n * 1e3,
        "reader_busy_ms_per_1k": reader_busy_ns / 1e6 / n * 1e3,
        "wall_ms_per_1k": (wall1 - wall0) * 1e3 / n * 1e3,
        "reached_intake": len(seen),
        "msg_rfq_created_dispatched": metrics.counter("b.msg.rfq_created"),
        "msg_rfq_deleted_dispatched": metrics.counter("b.msg.rfq_deleted"),
        "prefiltered": metrics.counter("b.prefiltered"),
        "intake_fastpath_drops": metrics.counter("rfq.dropped_series_fastpath"),
    }


async def dispatch_cost(frames: list[str]) -> dict[str, float]:
    """MAIN-LOOP cost per frame class, perf_counter, min of 10 reps: the
    dispatcher (``_dispatch`` → handlers → the intake's own allowlist drop)
    and the lane round trip (push + pop + stale check). The pre-filter
    removes exactly this work for every frame it drops."""
    from combomaker.exchange.ws import Lane

    metrics = Metrics()
    m = WsManager(
        "wss://x",
        _Signer(),  # type: ignore[arg-type]
        SystemClock(),
        metrics,
        name="d",
        connect=None,  # type: ignore[arg-type]  # never started
    )
    m.mark_sheddable("rfq_created", stale_after_s=600.0)
    m.mark_sheddable("rfq_deleted")
    intake = RfqIntake(m, metrics, series_prefixes=LIVE_ALLOWLIST)

    async def on_rfq(rfq: Rfq) -> None:
        pass

    intake.on_rfq(on_rfq)
    now_ns = time.monotonic_ns()
    created = [json.loads(f) for f in frames if '"rfq_created"' in f[:40]][:2000]
    deleted = [json.loads(f) for f in frames if '"rfq_deleted"' in f[:40]][:2000]
    for msg in (*created, *deleted):
        msg["_recv_mono_ns"] = now_ns
    out: dict[str, float] = {}
    for label, msgs in (("dispatch_us_foreign_created", created), ("dispatch_us_deleted", deleted)):
        best = math.inf
        for _ in range(10):
            t0 = time.perf_counter()
            for msg in msgs:
                await m._dispatch(msg)
            best = min(best, (time.perf_counter() - t0) / len(msgs) * 1e6)
        out[label] = best
    best = math.inf
    for _ in range(10):
        t0 = time.perf_counter()
        for msg in created:
            m._lanes.push(msg, Lane.MARKET)
        for _msg in created:
            popped = m._lanes.pop(Lane.MARKET)
            m._market_frame_stale(popped or {})
        m._lanes.settle_idle()
        best = min(best, (time.perf_counter() - t0) / len(created) * 1e6)
    out["lane_push_pop_stale_us"] = best
    return out


def micro(frames: list[str], prefilter: RawSeriesPrefilter) -> dict[str, float]:
    created = [f for f in frames if '"rfq_created"' in f[:40]]
    deleted = [f for f in frames if '"rfq_deleted"' in f[:40]]
    out: dict[str, float] = {}
    for label, sample in (("rfq_created", created), ("rfq_deleted", deleted)):
        sample = sample[:5000]
        t0 = time.perf_counter()
        for raw in sample:
            prefilter.judge(raw)
        t1 = time.perf_counter()
        for raw in sample:
            json.loads(raw)
        t2 = time.perf_counter()
        out[f"{label}_judge_us"] = (t1 - t0) / len(sample) * 1e6
        out[f"{label}_json_loads_us"] = (t2 - t1) / len(sample) * 1e6
        out[f"{label}_bytes_p50"] = statistics.median(len(r) for r in sample)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--frames", type=int, default=20000)
    args = ap.parse_args()
    frames, counts = build_corpus(args.frames)
    print(json.dumps({"corpus": counts, "frames": len(frames)}))
    print(json.dumps({"micro": micro(frames, RawSeriesPrefilter(LIVE_ALLOWLIST))}))
    costs = asyncio.run(dispatch_cost(frames))
    print(json.dumps({"main_loop": {k: round(v, 3) for k, v in costs.items()}}))
    results: dict[str, list[dict[str, float]]] = {"off": [], "on": []}
    for _ in range(args.reps):
        for mode in ("off", "on"):
            r = asyncio.run(run_once(frames, prefilter=(mode == "on")))
            results[mode].append(r)
            rounded = {k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()}
            print(json.dumps({"mode": mode, **rounded}))
    keys = ("process_cpu_ms_per_1k", "reader_busy_ms_per_1k", "wall_ms_per_1k")
    summary: dict[str, Any] = {}
    for mode, rs in results.items():
        summary[mode] = {k: round(statistics.median(r[k] for r in rs), 3) for k in keys}
    off, on = summary["off"], summary["on"]
    summary["relief"] = {k: round(1.0 - on[k] / off[k], 4) if off[k] else None for k in keys}
    print(json.dumps({"summary_median": summary}, indent=2))


if __name__ == "__main__":
    main()
