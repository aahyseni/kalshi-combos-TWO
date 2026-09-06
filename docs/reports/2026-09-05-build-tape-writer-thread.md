# 2026-09-05 — BUILD: tape writer OFF the event loop (a thread with its own connection)

Branch `build/tape-writer-thread` — build commit `467ffea`, **review-fix commit `<<FIXSHA>>`** (worktree
`C:/Users/aahys/kct-writer`; bot LIVE on main `08f342e` throughout — store read-only, logs grep/tail only, every heavy
job at LOW priority, no process touched).
**Blast radius: `ops/persistence.py` only** (the tape writer + the shared connection's statement discipline + its
tests + one bench tool). Pricing, risk, quote construction, every tape READER (acceptance-tape seed, held_positions
tape lookup, retained-floor sweep, tape retention, vitals snapshot) and every existing log event name/field are
unchanged. **CORRECTION (review fix pass — the original claim "the synchronous ledger paths … are unchanged" was
FALSE as measured):** moving the tape writer to its own connection put a FOREIGN COMMITTER on the shared aiosqlite
connection's WAL, and that connection's ledger paths were NOT safe under it — see *Review fixes* below. Every ledger
method keeps its signature and its rows, but its statements now run under one connection lock with rollback
discipline (`_fetchall` / `_fetchone` / `_ledger_txn`), the report's decisions scan runs on its own read-only
connection, and the tape writer yields the write lock to a ledger transaction in flight. New log events:
`store_ledger_txn_rolled_back`, `store_ledger_txn_retry_after_rollback`, `store_ledger_txn_left_open`,
`store_ledger_txn_inherited_open_transaction`, `store_writer_batch_locked_retrying`,
`store_checkpoint_connection_left_to_writer_thread`.

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
| ~~OPEN~~ **RETRACTED → FIXED** | ~~Two connections now write the same WAL … No main-connection path does read-then-write inside one explicit transaction … so `SQLITE_BUSY_SNAPSHOT` cannot arise.~~ **FALSE as measured by the review (probe on the real Store, this branch: 40/40 `record_fill` raised `database is locked` while the report pager was open; still raised after the pager closed; `count(decisions)` read 300,000 while the file held 305,000; main: 40/40 ok).** An IMPLICIT read transaction exists whenever any statement is active on the connection (a cursor between `execute()` and its terminal fetch/close — one loop hop, 67-134 ms live), the connection is shared across concurrently running coroutines, and Python's implicit `BEGIN` before a write turns a stale snapshot into a transaction that never ends. Also false in the same row: "a 1,000-row `executemany` holds the write lock for ~10 ms" — under a CPU-bound loop it held it for **2.8 s mean / 8.9 s max** (GIL starvation). Both fixed below. | See *Review fixes*. |
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
| Tests (build) | `tests/test_persistence.py` + `tests/test_tape_retention.py`: **39 passed** (26 → 39: **+13** new — `test_persistence.py` 9 → 22; the build report first said "+14 / 25 → 39", corrected by the review), every prior assertion kept or re-expressed against the thread — none weakened | LOW-priority pytest, this worktree |
| Full suite (once, after build, LOW priority) | **4,238 passed / 0 failed / 3 deselected in 372 s** (main collects 4,225 runnable / 4,228; branch 4,238 / 4,241 = **+13**) | `full_suite_1.log` in the session scratchpad |
| Tests (review fix pass) | the same two files: **50 passed** (39 → 50: **+11** new, none weakened; `test_persistence.py` 22 → 33) | LOW-priority pytest, this worktree |
| Full suite (once, after the review fixes, LOW priority) | **4,249 passed / 0 failed / 3 deselected in 366.24 s** (one LOW-priority run after every fix; = 4,238 + the 11 new tests) | `full_suite_fixpass.log` in the session scratchpad |
| Vitals fast gate (rule 9, after the review fixes) | **8/8 vital signs GREEN (GATE PASS, 108.6 s)** | `VITALS_DATA_DIR=<snapshot>` `python -m tools.vitals.gate` (2-table snapshot; live data dir never touched) |
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
4. Ledger paths under the foreign committer (review fix pass): `record_fill` / `record_position_settled` /
   `mark_fill_verified` succeed under load — fills booked to the cent, `fill_verified` on tape; **any**
   `database is locked` on the main connection is an alarm (`store_ledger_txn_rolled_back` /
   `store_ledger_txn_retry_after_rollback` — expected count 0; a non-zero count means a cancelled read left a cursor
   active and the rollback discipline had to fire); the WEDGE DETECTOR `store_ledger_txn_left_open` /
   `store_ledger_txn_inherited_open_transaction` never fire (`_db.in_transaction` is never true between operations);
   `record_fill` LATENCY — a fill that collides with a tape batch waits in SQLite's busy handler (1/2/5/10/15/20/25 ms
   backoff steps) for at most one batch commit; with the ledger-first yield that is ≤ ~110 ms under a saturated loop
   (bench) and ms-scale on an idle one — measure the `fill_booked`-minus-`quote_executed` gap and alarm past
   `STORE_OP_TIMEOUT_S / 10`; `batch_yields_to_ledger` counts how often the tape thread stood aside.
5. Store growth returns (full tape captured) — retention / rotation own the size.

## Review fixes (same day, commit `<<FIXSHA>>`)

The adversarial review returned **NO_SHIP** with three must-fixes and seven should-fixes. Every must-fix is applied;
every should-fix is applied. The fix pass also found and fixed **one defect the review did not name** (GIL starvation
of the writer thread under a CPU-bound loop — the regression test written for must-fix #1 (c) caught it). Nothing
armed; the live bot ran main `08f342e` throughout.

| | Item | Status |
|---|---|---|
| WRONG (must-fix #1) | **SQLITE_BUSY_SNAPSHOT wedge on the shared connection.** With the tape thread committing on its OWN connection, any ACTIVE read statement on the shared aiosqlite connection (a cursor between `execute()` and its terminal fetch/close — one loop hop) pins a read snapshot; a foreign commit in that window made the next same-connection write fail in **0.000 s** with `database is locked` (busy_timeout not consulted), and Python's implicit `BEGIN` left the connection INSIDE that stale transaction with no `rollback()` anywhere on the main-connection path: every later ledger write failed, every later read was stale, for the rest of the run. Proven by the review on the real Store (40/40 `record_fill` raised during the report pager; 300,000 vs 305,000). | Measured; mechanism named. |
| FIXED | **(a) ONE connection lock** (`Store._conn_lock`, `asyncio.Lock`) around every statement lifecycle on the shared connection, through three helpers that are now the ONLY way the class touches `self._db` after open: `_fetchall(sql, params)` (execute → fetchall → close under the lock), `_fetchone` (single-row reads), `_ledger_txn(body)` (a whole ledger transaction: statements + commit). Every read site (24) and every write site (10: `record_would_quote`, `record_position_open`, `record_position_settled` incl. its resolve read, `mark_fill_verified`, `void_phantom_fill`, `record_fill`, `record_markout`, `record_combo_trades`, `settle_ev_entry`, sync-mode `_write`) routed. Structural test: a witness proxy on `_db` records `lock.locked()` at every execute/commit/rollback across the whole public surface — **0 unlocked statements** (`test_every_shared_connection_statement_runs_under_the_connection_lock`). | Tested. |
| FIXED | **(b) ROLLBACK discipline** in `_ledger_txn`: a body that raises with the connection inside a transaction is rolled back (nothing of it was committed; the caller sees its exception unchanged) and counted (`ledger_txn_rollbacks`, WARNING `store_ledger_txn_rolled_back` with the sqlite error name); a LOCK-class failure (`_is_lock_error`: primary code SQLITE_BUSY — which covers the extended BUSY_SNAPSHOT 517 — or SQLITE_LOCKED) is retried ONCE after the rollback (`ledger_txn_retries`, `store_ledger_txn_retry_after_rollback`); a failed retry is rolled back too. **Measured correction to the review's premise:** rollback does NOT cure the residue while the leaked cursor still lives — SQLite downgrades a rolled-back transaction to a READ transaction while another statement of the connection is active (`btreeEndTransaction`, `nVdbeRead>1`), so the retry fails the same way (my first probe "passed" only because it exhausted the cursor before the write). What the rollback DOES buy: the connection is left OUTSIDE any transaction, so the moment the leaked cursor closes (its coroutine's frame dies) the very next write books with no intervention — **the wedge is bounded by a leaked statement's lifetime, never the run's** (`test_ledger_txn_never_leaves_the_connection_wedged_by_a_leaked_cursor`: first write raises instantly, `in_transaction` False, 2 rollbacks / 1 retry logged as `SQLITE_BUSY_SNAPSHOT`, cursor closed → next write True, reads fresh). Wedge detectors: `store_ledger_txn_left_open` (ERROR; a body returned without commit — committed) and `store_ledger_txn_inherited_open_transaction` (WARNING; something outside the class left a transaction open — joined, never rolled back). Cancellation is deliberately NOT caught (awaiting a rollback inside a cancelled `wait_for` would make the bound wait on the very connection that overran it). | Tested. |
| FIXED | **(c) The probe as a regression test** — `test_ledger_writes_survive_shared_connection_reads_under_the_tape_thread`: real Store + `start_writer()` + a tape firehose that keeps the loop CPU-BOUND at ~the live rate (10 rows + a `json.loads` burn per turn) + a reader task issuing locked shared-connection reads + the report's `decision_reason_counts` + 10 `record_fill` calls interleaved → every fill booked, `in_transaction` False, **`ledger_txn_rollbacks == 0`** (the lock alone prevented every collision), main-connection counts == a fresh connection's truth. **Negative control run: this test FAILS on the reviewed commit `9119ffe`** (`sqlite3.OperationalError: database is locked`) with the identical harness. | Tested both ways. |
| WRONG (must-fix #2) | `decision_reason_counts` — called every 300 s on the LIVE path (`quote_app._report_loop` → `build_report` → `ops/report.py:40`) — paged `SELECT reasons_json FROM decisions` with `fetchmany(10_000)` on the SHARED connection: one ACTIVE statement across ~1,000 chunk awaits at 10M rows/h = MINUTES of pinned snapshot every 5 min — the exact wedge trigger; under fix (a) it would have held the ledger lock for those minutes instead. | Measured. |
| FIXED | Runs on its OWN `mode=ro` stdlib connection inside `asyncio.to_thread` (the `ops/acceptance_seed.py:110` pattern; `BUSY_TIMEOUT_MS` / `STORE_OP_TIMEOUT_S` reused for the reader's lock tolerance), and the counting moved INSIDE SQLite: `SELECT j.value, COUNT(*) FROM decisions d, json_each(d.reasons_json) j GROUP BY j.value` — C code that releases the GIL while stepping, a few dozen result rows however large the table, nothing pages, no per-row `json.loads` on any Python thread. Same arithmetic as the retired loop (a reason repeated inside one row counts twice — asserted). Legacy direct construction (no path) runs the same aggregate on the shared connection under the lock. **Not one statement on the shared connection** (asserted with a SQL spy). | Tested. |
| WRONG (must-fix #3) | The build report's OPEN row 2 and the header blast-radius claim ("the synchronous ledger paths … are unchanged", "`SQLITE_BUSY_SNAPSHOT` cannot arise") were false as measured. | — |
| FIXED | Both rewritten above (header CORRECTION paragraph; OPEN row 2 struck through and retracted with the probe result); relight checklist item 4 now names `record_fill` / `record_position_settled` success under load, the `database is locked` alarm on the main connection, the wedge detectors, and `record_fill` latency with a derived alarm bound. | — |
| WRONG (**NEW — found by the fix pass, not in the review**) | **GIL starvation of the writer thread.** `executemany` steps once PER ROW and CPython re-acquires the GIL after every `sqlite3_step`; against a CPU-bound event loop (the live shape: `json.loads` on ~3,000 frames/s) each re-acquire waits up to the interpreter's 5 ms switch interval. Measured (scratch `probe_gil_starvation.py`, LOW priority, stdlib threads, 20k rows): **executemany under a computing main thread: 362 rows/s, 2.8 s mean / 8.9 s max per 1,000-row batch — the WAL write lock held the whole time**, so a concurrent `record_fill` waited out its 5 s `busy_timeout` and FAILED (plain SQLITE_BUSY); idle main thread: 419k rows/s. That is SLOWER than the asyncio-task writer this build replaced, and it breaks the ledger. The build's bench never saw it because its "saturated loop" burner **slept** (`time.sleep` releases the GIL); the regression test above caught it (first run: `SQLITE_BUSY` after the full 5 s wait, queue pinned at 200,000). | Measured. |
| FIXED | `_commit_batch` writes each SQL-text group as **multi-row `INSERT … VALUES (…), (…), …` statements** — all rows of a chunk bound under ONE GIL hold, ONE `sqlite3_step` — chunked by the ENGINE's own bound-variable limit (`wdb.getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)` = 32,766 on the bundled 3.45.3: ≥ 2,520 rows of the widest tape table, so a 1,000-row batch is one statement per table; no hand-set number). Same probe: **13,399 rows/s, 109 ms max per batch, the burning thread's own throughput unchanged** (idle: 432k rows/s, equal to before). A SQL text without a `VALUES (?, …)` tail falls back to `executemany`. Honest bench (burner now COMPUTES `json.loads` for its 100 ms): **reviewed commit `9119ffe` (executemany thread), same harness, computing burner: 3,000 rows committed in 20.29 s = 148 rows/s, NOT drained (idle: 134,933 rows/s — the idle shape hides it; legacy 12,289 idle / 0 saturated)**; **this commit: thread 50,000 rows in 4.61 s = 10,840 rows/s under the computing loop (burner kept all 45 of its turns), 145,552 rows/s idle; legacy 0 rows in 20 s saturated / 11,827 idle.** Unit test pins the shape (chunking by a pinned limit 12 → 2+2+1 rows; the real limit → one statement per 1,000-row batch; fallback path). | Tested + benched. |
| WRONG (found by the fix pass) | **Ledger write latency under a tape backlog.** SQLite's busy handler has no fairness: with the queue backlogged the writer thread re-took the write lock the instant it committed, and a `record_fill` waiting in the handler's 25 ms backoff saw **178-1,483 ms (mean 457 ms)** per fill in the regression test's backlog shape (locked reads queued behind it: 1.4 s per read triple). | Measured. |
| FIXED | **Ledger first on the write lock:** `_ledger_txn_idle` (`threading.Event`, set while no ledger transaction is in flight) is cleared by `_ledger_txn` for its body (try/finally — restored on success, failure and cancellation) and the tape thread waits on it before each batch, bounded by `STORE_OP_TIMEOUT_S` (the store's own legitimate-block statement) so a stuck flag can never stall the tape; `batch_yields_to_ledger` counts the yields. A fill now waits at most one in-flight batch. | Tested (held back → released → lands; bounded hold; flag restored every way out). |
| FIXED (should-fix 1) | A tape batch whose commit fails with a LOCK-class error (a ledger transaction spanning several loop hops at p99 lag 1 s; a tape-retention DELETE at its own `STORE_OP_TIMEOUT_S` bound — the SAME constant, so the boundary collision is designed-in) is **retried with its rows still in memory** until it lands or the stop flag is set — paced by SQLite's own busy wait, no retry number of its own; `store_writer_batch_locked_retrying` WARNING per attempt (`n`, `attempt`, `sqlite_errorname`), `batch_lock_retries` counter; only a non-lock error or the stop flag drops a batch (`store_writer_batch_failed` now carries `lock_retries`). Before: the 1,000 rows were discarded. | Tested (pinned behind `BEGIN IMMEDIATE` at a squeezed 50 ms busy wait → ≥2 retries → lock lifts → all 7 rows land; stop flag → loud drop, thread exits). |
| FIXED (should-fix 2) | **The checkpoint connection never waits:** `busy_timeout` 0 (the OFF position of the lock wait, not a magnitude) — a TRUNCATE under a pinned reader returns its busy verdict in **0.000 s** (probe: vs 1.155 s at a 1 s timeout, 5 s at the old setting = the thread's ONLY stall, ~100 rows/s in the review probe) — and `_wal_checkpoint` runs **PASSIVE first, then TRUNCATE**: the fold (what bounds WAL growth) never needed a wait; the reviewer's proposed gate "TRUNCATE only when PASSIVE reports wal_frames == checkpointed" was tested and does NOT work — a fully-folded WAL with a reader at HEAD still reports `(1, n, n)` busy (probe K/L) — so the non-blocking connection is the lever. `checkpoint_failures` / `checkpoint_passive_fallbacks` / `passive_fallback_ok` / the `busy (wal_frames=…, checkpointed=…)` message keep their meaning; every existing checkpoint test passes unchanged. | Tested (order PASSIVE→TRUNCATE; verdict < `STORE_OP_TIMEOUT_S / 10`; `0 < checkpointed < wal_frames` under the pinned reader; ok once it closes). |
| FIXED (should-fix 3) | `close()` no longer closes `_ckpt_db` while the writer thread is still alive after the bounded join (it may be inside a wal_checkpoint pragma on that `check_same_thread=False` connection) — logs `store_checkpoint_connection_left_to_writer_thread` and leaves it to the daemon. | Tested (thread stuck behind a write lock; `close()` returns; ckpt connection still open; thread lands its batch and exits once the lock lifts). |
| FIXED (should-fix 4) | The stop-flag exit path's un-`task_done`'d sentinel is documented in `_writer_thread_main` (inert: nobody joins the queue after `close()`). | Doc. |
| FIXED (should-fix 5) | Arithmetic: the build added **13** tests (`test_persistence.py` 9 → 22; 26 → 39 across the two files; 4,225 + 13 = 4,238) — corrected in the Measured table. This pass adds **11** more (22 → 33; 39 → 50). | — |
| FIXED (should-fix 6) | `test_hot_path_write_never_yields_to_the_loop` is kept as a guard but is no longer cited as a build claim (the pre-build `put_nowait` never yielded either; the build's claim is the DRAIN costing zero loop iterations). | Doc. |
| FIXED (should-fix 7) | OPEN row / relight item 4 now name ledger-write LATENCY and its mechanism (busy-handler backoff steps 1/2/5/10/15/20/25 ms; at most one batch with the ledger-first yield) with a derived alarm bound. | Doc. |
| OPEN | The report loop's OTHER reads (`count("rfqs")`, `count("would_quotes")`, `decision_kind_counts`) still run on the shared connection every 300 s; they are fully consumed under the lock (no wedge hazard) but a `COUNT(*)` over a day's tape table holds the ledger lock for its scan (~1-3 s at 30M+ rows) — the same delay the single connection thread already imposed on main, so no regression, but the whole `build_report` belongs on the read-only worker connection. | Next lever on this file. |
| OPEN | Under a pinned reader the checkpoint retry cadence (`_CHECKPOINT_RETRY_WRITES` = 500, pre-existing) now fires ~6×/s at 3,000 rows/s — one `store_writer_checkpoint_failed` WARNING + one `store_writer_stats` INFO per 500 rows for the reader's duration (the report scan: seconds every 5 min; an operator `mode=ro` scan: its length). Pre-existing cadence at 100× the old wall-clock rate; a streak-aware level (first WARNING, repeats INFO) is the candidate. | Watch at relight. |
| OPEN | GIL share of the writer thread live: the bench says ~5 GIL acquisitions per batch (flatten, bind, step, commit) — ms per second at 3 batches/s; `event_loop_lag` after relight is the measurement. `record_fill` latency and `batch_yields_to_ledger` are the two new numbers to read. | Relight. |

### Measured in the fix pass (all LOW priority, temp stores, live data dir never touched)

| Quantity | Value | Source |
|---|---|---|
| BUSY_SNAPSHOT error shape | raised in 0.000 s; `sqlite_errorcode` 517 (extended) → primary 5 = SQLITE_BUSY; `sqlite_errorname` SQLITE_BUSY_SNAPSHOT; `in_transaction` True after | `probe_rollback_semantics.py` (sqlite 3.45.3, Python 3.13.0) |
| Rollback with the leaked cursor still mid-iteration | `in_transaction` False; the cursor keeps fetching; a write succeeds only AFTER the cursor is exhausted/closed (my first reading of this probe was wrong; the regression test exposed it) | same + `test_ledger_txn_never_leaves_the_connection_wedged_by_a_leaked_cursor` |
| Checkpoint verdict under a pinned reader | PASSIVE `(0, 20, 16)` in 6 ms; TRUNCATE `busy_timeout=0` → `(1, 20, 16)` in **0.000 s**; TRUNCATE `busy_timeout=1000` → `(1, 20, 16)` in **1.155 s**; reader at HEAD with every frame folded → TRUNCATE still `(1, 1, 1)` | same |
| Writer thread vs a computing main thread (stdlib threads, 20k rows) | executemany **362 rows/s**, batch mean **2,765 ms** / max **8,886 ms**; multi-row VALUES **13,399 rows/s**, batch mean 75 ms / max **109 ms**; idle main: 419k / 432k rows/s; burner ~360k frames/s in both | `probe_gil_starvation.py` |
| Honest bench, this commit (`--rows 50000 --budget-s 20 --burn-ms 100`, burner COMPUTES) | idle: legacy 11,827 rows/s @ 1,001 hops/1k vs thread **145,552 rows/s @ 0.4 hops/1k**; saturated: legacy **0 rows in 20.25 s** (101 hops) vs thread **50,000 rows in 4.61 s = 10,840 rows/s**, drained, 20 hops, burner 45/45 turns | `tools/diagnostics/bench_tape_writer.py`; JSON `bench_tape_writer_fixpass.json` in the session scratchpad |
| Honest bench, the REVIEWED commit's `persistence.py` (same harness, PYTHONPATH swapped) | **reviewed commit `9119ffe` (executemany thread), same harness, computing burner: 3,000 rows committed in 20.29 s = 148 rows/s, NOT drained (idle: 134,933 rows/s — the idle shape hides it; legacy 12,289 idle / 0 saturated)** | `bench_old.json` / `bench_old.log` in the session scratchpad |
| Ledger fill latency under a tape BACKLOG, before the ledger-first yield | 20 fills: 178 … 1,483 ms, mean 457 ms; locked read triple mean 1.4 s; queue max 54,750 | `time_probe_test.py` (the regression test's shape) |
| Regression test, this commit / reviewed commit | 10/10 fills booked, 0 rollbacks, 4.3 s / `sqlite3.OperationalError: database is locked` (FAILED) | `test_ledger_writes_survive_shared_connection_reads_under_the_tape_thread` |
| Touched tests | `tests/test_persistence.py` + `tests/test_tape_retention.py`: **50 passed** (39 → 50, +11; none weakened) | LOW-priority pytest |
| Full suite (once, after the fixes) | **4,249 passed / 0 failed / 3 deselected in 366.24 s** (one LOW-priority run after every fix; = 4,238 + the 11 new tests) | `full_suite_fixpass.log` |
| Vitals fast gate | **8/8 vital signs GREEN (GATE PASS, 108.6 s)** | `vitals_fixpass.log` |
| ruff / mypy | `ruff check` clean on `persistence.py`, `test_persistence.py`, `bench_tape_writer.py`; `mypy --strict` clean on `persistence.py` | — |

### Where the review was wrong (with evidence)

- *"rollback() and retry the write once — a cancelled `_bounded_store` read that leaves a cursor active must never wedge the ledger"*: the rollback is necessary but the retry cannot succeed while that cursor lives — SQLite keeps the connection's read snapshot for any active statement (`btreeEndTransaction` downgrades to `TRANS_READ` when `nVdbeRead>1`); measured in the leaked-cursor test. The implemented guarantee is the one that holds: never inside a transaction, wedge bounded by the leaked statement's lifetime, next write books once it dies. Retry kept (cheap; cures the case where the statement finished in between).
- *"PASSIVE on the cadence, TRUNCATE only when PASSIVE reports wal_frames == checkpointed"*: measured false as a gate — a fully-folded WAL under a head reader still yields a busy TRUNCATE (`(1, 1, 1)`), so the gate would only skip the attempt; the stall is removed by `busy_timeout=0` on the checkpoint connection, PASSIVE-first kept for the fold.
- The review's evidence bullet *"the saturated-loop burner … is a fair stand-in for the measured p50 lag"* was not: a sleeping burner releases the GIL. The bench now computes.

## NEXT STEPS

- **Operator:** review + merge `build/tape-writer-thread`; relight on operator word (session-DETACHED, WMI Create — the
  standing rule). Not armed here; the live bot is untouched.
- **Next lever (out of scope here):** the main loop itself — dispatcher + `json.loads` + intake filters — is the binding
  constraint at ~3,000 frames/s (`ws_stale_market_frames` 1-4k per 30 s at N=3). The reader-side raw-string pre-filter
  from the fan-out report's OPEN row is the candidate; measure `event_loop_lag` after THIS build lands first so the
  writer's share is separated from the dispatcher's.
- **Watch after relight:** items 1-5 above; `store_writer_batch_failed` should never fire for a lock (those retry
  now: `store_writer_batch_locked_retrying` is the event to read, expected rare); `store_ledger_txn_rolled_back` /
  `_retry_after_rollback` / `_left_open` / `_inherited_open_transaction` expected 0; `record_fill` latency and
  `batch_yields_to_ledger` are the two new numbers.
- **Next lever on this file:** move the rest of `build_report`'s tape reads (`count("rfqs")`, `decision_kind_counts`)
  to the read-only worker connection so no report read ever holds the ledger lock for a tape-table scan.
- **Owed elsewhere (unchanged by this build):** ledger stale-row P1, store rotation cadence, governor cadence.
