# 2026-09-05 — BUILD: tape writer OFF the event loop (a thread with its own connection)

Branch `build/tape-writer-thread` — build commit `{{COMMIT}}` (worktree `C:/Users/aahys/kct-writer`; bot LIVE on main
`08f342e` throughout — store read-only, logs grep/tail only, every heavy job at LOW priority, no process touched).
**Blast radius: the persistence TAPE path only** (`ops/persistence.py` writer + its tests + one bench tool). Pricing,
risk, quote construction, the synchronous ledger paths (fills / position_ledger / markouts / settlement / ev_ledger),
every tape READER (acceptance-tape seed, held_positions tape lookup, retained-floor sweep, tape retention, vitals
snapshot) and every log event name/field (`store_writer_stats`, `store_writer_batch_failed`,
`store_writer_checkpoint_failed`, `store_writer_checkpoint_ok`) are unchanged.

## WRONG / FIXED / OPEN

| | Item | Status |
|---|---|---|
| WRONG | The tape writer was an **asyncio TASK** that issued ONE `await self._db.execute(row)` PER ROW on the shared aiosqlite connection — every await resolves through `call_soon_threadsafe` = one event-loop iteration, so a 1,000-row batch cost **~1,000 loop hops**, each queued behind every other ready callback. Tonight's loop ran `event_loop_lag` **p50 67-134 ms / p99 0.4-1.0 s** (15 s windows), so a batch needed on the order of 100 s and the writer could not drain ~3,000 tape rows/s (`ws_inbound_rate` ≈ 3,000 frames/s Saturday night over N=3 shards). `store_writer_stats`: queue_depth **43k → 200k in 10 min on the FRESH 316 MB store**, then `dropped_writes_delta` **126,492** — the collapse was never store size. | Measured; mechanism named. |
| WRONG | The same writer **competed with quoting for the loop** the whole time the queue was non-empty (always): 1,000 hops per batch are 1,000 turns the dispatcher / `json.loads` / intake filters did not get. | Measured in the bench below (loop hops per 1,000 rows: 1,001 → 0.4). |
| FIXED | **`Store.start_writer()` now starts a daemon THREAD** (`_writer_thread_main`) with its OWN stdlib `sqlite3` connection (`_open_writer_connection`: the same writer-path pragmas the main connection carries — `busy_timeout=5000`, `synchronous=NORMAL`, `wal_autocheckpoint=0`; WAL is a file property already set by `Store.open`), fed by a thread-safe bounded `queue.Queue`. Each batch (≤ `WRITER_BATCH_ROWS`) is grouped by SQL text (every tape table has one INSERT text; per-table enqueue order kept) and written with `executemany` inside **ONE transaction**; a failing row rolls back its WHOLE batch (loud `store_writer_batch_failed`, `n` = batch size, the exception attached as `exc_info`) and the next batch proceeds. | Built + tested. |
| FIXED | **Hot path `_write` is O(1) and NEVER yields** in async mode: one `queue.Queue.put_nowait`, drop on `queue.Full` → `_dropped_writes += 1` (identical accounting). Property-tested: 10,000 awaited tape writes with a live spinner task recording **zero** loop turns. Sync mode (no writer; tests) unchanged: immediate execute + commit. | Tested. |
| FIXED | **Checkpoint semantics preserved on the thread:** `_wal_checkpoint` is now synchronous stdlib on the dedicated checkpoint connection (`_ckpt_db`, opened in `Store.open` with `check_same_thread=False`, one user at a time — the writer thread while it runs, otherwise the direct caller). TRUNCATE → busy VERDICT (row[0]) or raise → count `checkpoint_failures` → PASSIVE fallback (`checkpoint_passive_fallbacks`) → retry after `_CHECKPOINT_RETRY_WRITES` (500) instead of the full 5,000. The 2026-08-19 self-lock rule holds: no checkpoint pragma ever travels the main connection (test unchanged in substance), and the writer's own connection has no read cursors to self-lock on. | Tested against a REAL pinned reader from the thread (busy → PASSIVE → retry → ok once the cursor closes) and via the existing lock-simulating proxies. |
| FIXED | **Logging stays on the loop:** every emit from the thread (`store_writer_stats`, `store_writer_batch_failed`, `store_writer_checkpoint_ok/failed`, the new `store_writer_thread_died`) is posted via `loop.call_soon_threadsafe` (`_post_to_loop`); `_emit_writer_stats` therefore reads the hot path's drop counter on the hot path's thread. A closed loop (a leaked store at test teardown) or no loop (a direct checkpoint call) runs the emit inline — never lost. Tests assert every emit ran on the loop thread. | Tested. |
| FIXED | **Graceful close:** `close()` drains the queue with the EXISTING 2.0 s bound (`WRITER_CLOSE_DRAIN_S`; a bounded `queue.join` on the queue's own condition, off-loop), sets the stop flag, posts the `_WriterStop` sentinel (a thread blocked in `get()` wakes on it; a thread mid-batch with a FULL queue reads the flag after its commit — no idle-poll timeout exists), joins the thread bounded by `STORE_OP_TIMEOUT_S` (the store's own statement of a legitimate block), then closes the checkpoint connection BEFORE the main connection (the close-ordering rule — the writer thread closed ITS connection on exit, before the join returned). Durability at shutdown tested: 3,000 rows queued, `close()`, fresh `Store.open` reads 3,000. | Tested. |
| FIXED | **Bounds are NAMED, not new** (north star): `WRITER_QUEUE_MAXSIZE = 200000` (the asyncio.Queue maxsize since 2026-07-14), `WRITER_BATCH_ROWS = 1000` (the batch the loop always committed), `WRITER_CLOSE_DRAIN_S = 2.0` (close()'s drain bound). Pinned by `test_writer_bounds_are_the_existing_numbers`. Join bound reuses `STORE_OP_TIMEOUT_S`. | No new hand-set number. |
| FIXED | New public `Store.flush_writer(wait_s) -> bool` (bounded wait until everything enqueued so far is committed AND any due checkpoint attempted) — what the tests and the bench use instead of reaching into the queue. `writer_queue_depth()` unchanged (`ops/tape_retention.py` gates on it from its worker thread — `queue.Queue.qsize` is thread-safe). | — |
| FIXED | A legacy direct construction (`Store(db, clock)` — only `tools/diagnostics/restart_gate2_quote_validation.py`, read-only) has no path: `start_writer()` now raises a clear `RuntimeError` (never a silent no-op) and `_wal_checkpoint()` takes the counted, logged failure path with no passive fallback. | Tested. |
| OPEN | **Live verification at the next relight** (this build is NOT armed; the bot runs main `08f342e`): `store_writer_stats.queue_depth` should sit near 0 with `dropped_writes_delta` 0 through a Saturday-night window (tonight: 200k / 126,492); `event_loop_lag` p50 should fall (the writer's ~1,000 hops per batch are gone — how much of the 67-134 ms was the writer vs the dispatcher is the next measurement). Store growth resumes to the full tape (expect the 20 GB/day the 8/20 run showed) — the rotation tool + retention own that. | Relight checklist below. |
| OPEN | Two connections now write the same WAL (main: ledger; thread: tape). A ledger commit can wait on a tape batch commit (or vice versa) up to `busy_timeout` 5 s — a 1,000-row `executemany` holds the write lock for ~10 ms, so the wait is ms-scale. A TRUNCATE checkpoint pending on a pinned reader blocks new writers for up to its busy_timeout — **unchanged** from the dedicated-checkpoint-connection design of 2026-08-19. No main-connection path does read-then-write inside one explicit transaction (no `BEGIN` on the shared connection; the only explicit transaction in the tree is tape retention's `BEGIN IMMEDIATE` on its own connection), so `SQLITE_BUSY_SNAPSHOT` cannot arise. Watch `store_writer_batch_failed` (0 expected) and ledger-write latency at relight. | Watch. |
| OPEN | The thread takes GIL slices while binding parameters (sqlite3 releases the GIL inside `sqlite3_step` / fsync). At ~3,000 rows/s this is ms per second; the bench's saturated-loop run (100 ms burners) drained 50,000 rows in 0.48 s with the burner still getting its turns. Not measured live yet. | Relight. |
| OPEN | `ruff format` drift on `ops/persistence.py` and `tests/test_persistence.py` PRE-EXISTS on main (both files "would reformat" at `08f342e`); not reformatted here (noise). mypy strict: clean on `persistence.py`. | Pre-existing debt. |

## Measured (cite → verify)

| Quantity | Value | Source |
|---|---|---|
| Event-loop lag tonight (main `08f342e`, N=3 shards) | p50 67-134 ms, p99 0.4-1.0 s per 15 s window | `event_loop_lag`, `D:/kalshi-combos-TWO-data/live_20260905_2037.log` (task brief) |
| Inbound communications frames | ~3,000 frames/s Saturday night over N=3 sharded sockets | `ws_inbound_rate` |
| Stale-market drops (main-loop binding) | 1-4k `rfq_created` per ~30 s window at N=3, market lane depth 3-5k | `ws_stale_market_frames` |
| Tape writer collapse on the FRESH store | queue_depth 43k → 200k in 10 min; then `dropped_writes_delta` 126,492 (316 MB store, rotated 20:37 ET) | `store_writer_stats` |
| **Bench — idle loop, 50,000 rows** | legacy task-writer **13,091 rows/s**, **50,050 loop hops = 1,001 per 1,000 rows**; thread writer **138,597 rows/s**, **20 loop hops = 0.4 per 1,000 rows** (the 10 checkpoint-cadence emits × {checkpoint_ok, stats}) — **10.6× the drain rate, 2,500× fewer loop iterations** | `tools/diagnostics/bench_tape_writer.py --rows 50000 --budget-s 20 --burn-ms 100` (LOW priority, temp WAL store; JSON in the session scratchpad) |
| **Bench — saturated loop (100 ms blocking callbacks = the measured p50 shape), 50,000 rows** | legacy task-writer **0 rows committed in 20.33 s** (101 hops in 20 s: one per 100 ms burn — the FIRST 1,000-row batch needs ~1,001 hops ≈ 100 s; the live queue's 200k pin is exactly this); thread writer **50,000 rows in 0.48 s = 103,997 rows/s**, drained, 20 hops, burner kept its turns (4 burns) | same run |
| Loop iterations consumed by the writer per 1,000 rows | before **~1,001** (1 execute await per row + 1 commit); after **0.4** (only the cadence emits; 0 per row) | bench `hops/1k` column |
| Tests | `tests/test_persistence.py` + `tests/test_tape_retention.py`: **39 passed** (25 → 39: +14 new, every prior assertion kept or re-expressed against the thread — none weakened) | LOW-priority pytest, this worktree |
| Full suite (once, after build, LOW priority) | **4,238 passed / 0 failed / 3 deselected in 372 s** (one LOW-priority run after the build; = the tree's prior count + the 14 new tests) | `full_suite_1.log` in the session scratchpad |
| Vitals fast gate (rule 9 — persistence touched) | **8/8 GREEN (GATE PASS, 105.8 s)** from the 2-table snapshot (`VITALS_DATA_DIR`, never the live data dir) | `VITALS_DATA_DIR=<snapshot>` `python -m tools.vitals.gate` |
| ruff / mypy | `ruff check` clean on `persistence.py`, `test_persistence.py`, `bench_tape_writer.py`; `mypy --strict` clean on `persistence.py` | — |

## The mechanism

```
BEFORE (asyncio task on the ONE loop)                 AFTER (a thread; the loop never sees a row)

 hot path ──put_nowait──▶ asyncio.Queue(200k)          hot path ──put_nowait──▶ queue.Queue(200k)   O(1), no await
                              │                                                     │
                     _writer_loop (TASK)                                  store-writer THREAD (daemon)
                     batch ≤ 1000:                                        batch ≤ 1000:
                       for row: await db.execute ──┐  1 loop hop            group by SQL text
                       await db.commit ────────────┤  per row               executemany × tables
                              │                    │  (~1,001 / batch)      commit  ── ONE transaction
                     shared aiosqlite conn ◀───────┘                          │ own sqlite3 conn (same pragmas)
                     (also: fills, ledger, reads)                             │
                              │                                        every 5,000 rows: TRUNCATE ckpt on the
                     loop at p50 lag 100 ms ⇒                          dedicated ckpt conn (busy → PASSIVE → retry 500)
                     ~100 s per batch ⇒ queue pins 200k ⇒ drops          log emits ──call_soon_threadsafe──▶ loop
                                                                        close(): drain ≤ 2 s → stop flag + sentinel
                                                                                 → join ≤ 5 s → ckpt conn → main conn
```

What did NOT move: `record_fill`, `record_position_open/close`, markouts, settlement, `ev_ledger` — every "stay
synchronous & durable" path still awaits its own commit on the main aiosqlite connection. Readers on that connection
see the thread's committed batches (WAL cross-connection visibility — tested: 2,500 rows, order preserved).

## Tests added / adapted (`tests/test_persistence.py`)

Adapted (same assertions, thread shape): `_flood` → `flush_writer`; `_CheckpointLockedDB` / `_PragmaSpyDB` /
`_CloseOrderProxy` wrap the stdlib checkpoint connection (their `execute` was already a plain call);
`test_checkpoint_uses_dedicated_connection_never_main` calls the now-synchronous `_wal_checkpoint`;
`test_dropped_writes_stats_event_delta_and_levels` uses `queue.Queue(maxsize=2)`;
`test_writer_loop_emits_stats_on_checkpoint_cadence` additionally asserts every emit ran on the loop thread.

New: `test_writer_bounds_are_the_existing_numbers`; `test_thread_writer_drains_rows_readable_on_main_connection`
(real daemon thread, idempotent start, 2,500 rows, order, join on close);
`test_batch_is_atomic_failing_row_fails_its_batch_loudly_later_batches_continue` (drives the real thread body over a
pre-filled queue: 3 good + bad + 3 good → 0 rows, `n=7`, `exc_info` attached, next batch commits, no thread death);
`test_batches_group_by_sql_text_and_keep_per_table_order`; `test_live_queue_bound_drops_on_overflow_and_conserves_rows`
(live thread pinned behind a third connection's `BEGIN IMMEDIATE`, queue over-filled, committed == enqueued − dropped);
`test_stats_warning_carries_the_drops_on_the_cadence`; `test_thread_checkpoint_busy_verdict_passive_fallback_on_pinned_reader`
(real pinned reader, busy verdict → PASSIVE → retry cadence → ok, all emits on the loop);
`test_close_flushes_pending_rows_and_joins_thread`; `test_close_with_idle_writer_and_empty_queue_is_prompt`;
`test_sync_mode_unchanged_without_start_writer`; `test_hot_path_write_never_yields_to_the_loop`;
`test_post_to_loop_routes_to_the_loop_and_never_loses_an_emit`;
`test_legacy_direct_construction_cannot_start_writer_and_checkpoint_is_loud`.

## Relight checklist (when the operator merges + relights)

1. `store_writer_stats`: `queue_depth` near 0 and `dropped_writes_delta` 0 across a peak window (tonight 200k / 126,492).
2. `store_writer_batch_failed` = 0; `store_writer_checkpoint_failed` at the pre-build rate or lower (busy verdicts on
   pinned readers are expected and handled); `store_writer_thread_died` never.
3. `event_loop_lag` p50 vs tonight's 67-134 ms — the writer's share of the lag is the number this build buys back.
4. Ledger paths unchanged: fills booked to the cent, `fill_verified` on tape, no new `database is locked` on the main
   connection.
5. Store growth returns (full tape captured) — retention / rotation own the size.

## NEXT STEPS

- **Operator:** review + merge `build/tape-writer-thread`; relight on operator word (session-DETACHED, WMI Create — the
  standing rule). Not armed here; the live bot is untouched.
- **Next lever (out of scope here):** the main loop itself — dispatcher + `json.loads` + intake filters — is the binding
  constraint at ~3,000 frames/s (`ws_stale_market_frames` 1-4k per 30 s at N=3). The reader-side raw-string pre-filter
  from the fan-out report's OPEN row is the candidate; measure `event_loop_lag` after THIS build lands first so the
  writer's share is separated from the dispatcher's.
- **Watch after relight:** items 1-5 above; if `store_writer_batch_failed` ever fires, the `exc_info` renders the
  sqlite error on the loop — a `database is locked` there means a ledger transaction held the write lock past 5 s
  (would be new; none expected).
- **Owed elsewhere (unchanged by this build):** ledger stale-row P1, store rotation cadence, governor cadence.
