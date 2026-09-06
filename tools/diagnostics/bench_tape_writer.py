"""TAPE WRITER OFF THE EVENT LOOP — before/after bench (2026-09-05 build).

THE DEFECT (measured live 2026-09-05, Saturday night, N=3 sharded sockets,
~3,000 inbound frames/s ``ws_inbound_rate``): the tape writer was an asyncio
TASK issuing one ``await db.execute`` PER ROW on the shared aiosqlite
connection — 1,000 loop hops per batch — on a loop whose ``event_loop_lag``
ran p50 67-134 ms / p99 0.4-1.0 s. Every hop waited its turn behind every
other ready callback, so the writer (a) could not drain the tape
(``store_writer_stats`` queue_depth 43k → 200k in 10 min on the FRESH 316 MB
store, then ``dropped_writes_delta`` 126,492) and (b) competed with quoting
for the loop the whole time the queue was non-empty (always).

THE BENCH: two writers drain the same rows into a temp WAL store, once on an
idle loop (the raw number) and once while a burner task COMPUTES on the loop
in callbacks of the measured length (default 100 ms — the p50 shape; it burns
CPU with ``json.loads``, never sleeps, so the GIL is contended as it is live):

  legacy-task  the pre-2026-09-05 writer, replicated here in exact shape
               (asyncio task, asyncio.Queue at the same 200k bound, batches
               <= WRITER_BATCH_ROWS, ONE await per row on the store's shared
               aiosqlite connection, one commit per batch; the 5,000-write
               checkpoint cadence is omitted — it ran on a separate
               connection in both writers and adds no per-row hops).
  thread       the live ``Store.start_writer()``: a daemon thread with its
               own sqlite3 connection, one multi-row INSERT per SQL text
               (review fix pass: executemany starved on the GIL against a
               computing loop), ONE transaction per batch, O(1)
               never-yielding ``_write``.

Per writer × loop state the bench reports rows committed / elapsed (rows/s)
and the LOOP HOPS the writer consumed per 1,000 rows: for the legacy writer
every aiosqlite await resolves through ``call_soon_threadsafe`` = one loop
iteration spent on tape; for the thread writer every ``_post_to_loop`` post
(the checkpoint-cadence stats/log emits) is counted the same way.

Rule 8: drives the REAL Store; reimplements only the retired writer. Nothing
here touches the live data dir — every store is a temp file.

    .venv/Scripts/python.exe tools/diagnostics/bench_tape_writer.py
        [--rows 50000] [--budget-s 20] [--burn-ms 100] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from combomaker.core.clock import FakeClock  # noqa: E402
from combomaker.ops.persistence import (  # noqa: E402
    WRITER_BATCH_ROWS,
    WRITER_QUEUE_MAXSIZE,
    Store,
)

_ROW_SQL = (
    "INSERT INTO decisions (at, kind, rfq_id, reasons_json, context_json) VALUES (?, ?, ?, ?, ?)"
)


def _row(i: int) -> tuple[str, tuple[Any, ...]]:
    return (_ROW_SQL, ("2026-09-05T00:00:00", "no_quote", f"r{i}", '["skip_bench"]', "{}"))


@dataclasses.dataclass
class Result:
    writer: str
    loop_state: str
    burn_ms: float
    rows_enqueued: int
    rows_committed: int
    elapsed_s: float
    loop_hops: int
    drained_within_budget: bool
    burner_callbacks: int

    @property
    def rows_per_s(self) -> float:
        return self.rows_committed / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def hops_per_1k_rows(self) -> float:
        return 1000.0 * self.loop_hops / self.rows_committed if self.rows_committed else 0.0


class LegacyTaskWriter:
    """The pre-2026-09-05 ``Store._writer_loop`` in exact shape (see module
    docstring). ``hops`` counts every await that resolves through the loop
    (aiosqlite runs each execute/commit on its own thread and completes the
    awaiting future via ``call_soon_threadsafe`` — one loop iteration each)."""

    def __init__(self, store: Store) -> None:
        self._db = store._db  # noqa: SLF001 — the shared connection, as the old writer used
        self.q: asyncio.Queue[tuple[str, tuple[Any, ...]]] = asyncio.Queue(
            maxsize=WRITER_QUEUE_MAXSIZE
        )
        self.hops = 0
        self.committed = 0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="legacy-store-writer")

    async def _loop(self) -> None:
        q = self.q
        while True:
            first = await q.get()
            batch = [first]
            while len(batch) < WRITER_BATCH_ROWS:
                try:
                    batch.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for sql, params in batch:
                await self._db.execute(sql, params)
                self.hops += 1
            await self._db.commit()
            self.hops += 1
            self.committed += len(batch)
            for _ in batch:
                q.task_done()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


_BURN_FRAME = json.dumps(
    {"type": "rfq_created", "msg": {"id": "x" * 40, "legs": [{"t": "M1", "s": "yes"}] * 4}}
)


async def _burner(stop: asyncio.Event, burn_ms: float, stats: dict[str, int]) -> None:
    """The saturated loop: a callback that COMPUTES for ``burn_ms`` (a
    ``json.loads`` loop — the intake's own work) then yields once — every
    other ready callback gets exactly one turn per burn, i.e. a loop lag of
    ~burn_ms per hop (the measured p50 shape). It must BURN CPU, not sleep
    (2026-09-05 review fix pass): a sleeping burner releases the GIL, which
    hid the writer thread's GIL starvation — ``executemany`` re-acquires the
    GIL after every row's step, and against a computing loop each re-acquire
    waits up to the 5 ms switch interval (measured: 362 rows/s, 8.9 s max
    per 1,000-row batch, the WAL write lock held throughout)."""
    while not stop.is_set():
        deadline = time.perf_counter() + burn_ms / 1000.0
        while time.perf_counter() < deadline:
            json.loads(_BURN_FRAME)
        stats["callbacks"] += 1
        await asyncio.sleep(0)


async def run_legacy(rows: int, budget_s: float, burn_ms: float) -> Result:
    with tempfile.TemporaryDirectory() as tmp:
        store = await Store.open(Path(tmp) / "bench.sqlite3", FakeClock())
        try:
            writer = LegacyTaskWriter(store)
            stop = asyncio.Event()
            stats = {"callbacks": 0}
            burn = asyncio.create_task(_burner(stop, burn_ms, stats)) if burn_ms > 0 else None
            writer.start()
            t0 = time.perf_counter()
            for i in range(rows):
                writer.q.put_nowait(_row(i))
            drained = True
            try:
                await asyncio.wait_for(writer.q.join(), timeout=budget_s)
            except TimeoutError:
                drained = False
            elapsed = time.perf_counter() - t0
            stop.set()
            if burn is not None:
                await burn
            await writer.stop()
            return Result(
                "legacy-task",
                "saturated" if burn_ms > 0 else "idle",
                burn_ms,
                rows,
                writer.committed,
                elapsed,
                writer.hops,
                drained,
                stats["callbacks"],
            )
        finally:
            await store.close()


async def run_thread(rows: int, budget_s: float, burn_ms: float) -> Result:
    with tempfile.TemporaryDirectory() as tmp:
        store = await Store.open(Path(tmp) / "bench.sqlite3", FakeClock())
        try:
            hops = {"n": 0}
            real_post = store._post_to_loop  # noqa: SLF001

            def counting_post(fn: Any) -> None:
                hops["n"] += 1
                real_post(fn)

            # Instance attribute shadows the method for the thread's
            # ``self._post_to_loop`` lookups — counts every loop post.
            store._post_to_loop = counting_post  # type: ignore[method-assign]  # noqa: SLF001
            store.start_writer()
            stop = asyncio.Event()
            stats = {"callbacks": 0}
            burn = asyncio.create_task(_burner(stop, burn_ms, stats)) if burn_ms > 0 else None
            t0 = time.perf_counter()
            for i in range(rows):
                await store._write(*_row(i))  # noqa: SLF001 — the tape hot path
            drained = await store.flush_writer(budget_s)
            elapsed = time.perf_counter() - t0
            stop.set()
            if burn is not None:
                await burn
            committed = await store.count("decisions")
            return Result(
                "thread",
                "saturated" if burn_ms > 0 else "idle",
                burn_ms,
                rows,
                committed,
                elapsed,
                hops["n"],
                drained,
                stats["callbacks"],
            )
        finally:
            await store.close()


def _render(results: list[Result]) -> str:
    head = (
        f"{'writer':<12} {'loop':<10} {'rows':>7} {'committed':>9} {'elapsed s':>9}"
        f" {'rows/s':>9} {'loop hops':>9} {'hops/1k':>8} {'drained':>7} {'burns':>6}"
    )
    lines = [head, "-" * len(head)]
    for r in results:
        lines.append(
            f"{r.writer:<12} {r.loop_state:<10} {r.rows_enqueued:>7} {r.rows_committed:>9}"
            f" {r.elapsed_s:>9.2f} {r.rows_per_s:>9.0f} {r.loop_hops:>9} {r.hops_per_1k_rows:>8.1f}"
            f" {str(r.drained_within_budget):>7} {r.burner_callbacks:>6}"
        )
    return "\n".join(lines)


async def main_async(rows: int, budget_s: float, burn_ms: float) -> list[Result]:
    results: list[Result] = []
    results.append(await run_legacy(rows, budget_s, 0.0))
    results.append(await run_thread(rows, budget_s, 0.0))
    results.append(await run_legacy(rows, budget_s, burn_ms))
    results.append(await run_thread(rows, budget_s, burn_ms))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rows", type=int, default=50_000, help="tape rows per run")
    ap.add_argument(
        "--budget-s",
        type=float,
        default=20.0,
        help="wall budget per run; a writer that cannot drain within it reports what it did",
    )
    ap.add_argument(
        "--burn-ms",
        type=float,
        default=100.0,
        help="blocking callback length of the saturated-loop burner (measured p50 lag)",
    )
    ap.add_argument("--json", default="", help="also write the results as JSON here")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass
    results = asyncio.run(main_async(args.rows, args.budget_s, args.burn_ms))
    print(_render(results))
    print()
    legacy_sat = next(
        r for r in results if r.writer == "legacy-task" and r.loop_state == "saturated"
    )
    thread_sat = next(r for r in results if r.writer == "thread" and r.loop_state == "saturated")
    legacy_idle = next(r for r in results if r.writer == "legacy-task" and r.loop_state == "idle")
    thread_idle = next(r for r in results if r.writer == "thread" and r.loop_state == "idle")
    ls, ts = legacy_sat.rows_per_s, thread_sat.rows_per_s
    li, ti = legacy_idle.rows_per_s, thread_idle.rows_per_s
    ratio = f" ({ts / ls:.0f}x)" if ls else ""
    print(
        f"saturated loop ({args.burn_ms:.0f} ms callbacks):"
        f" legacy {ls:.0f} rows/s vs thread {ts:.0f} rows/s{ratio}"
    )
    print(f"idle loop: legacy {li:.0f} rows/s vs thread {ti:.0f} rows/s")
    print(
        f"loop hops per 1,000 rows: legacy {legacy_idle.hops_per_1k_rows:.0f}"
        f" vs thread {thread_idle.hops_per_1k_rows:.2f}"
    )
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        **dataclasses.asdict(r),
                        "rows_per_s": r.rows_per_s,
                        "hops_per_1k": r.hops_per_1k_rows,
                    }
                    for r in results
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
