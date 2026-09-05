# 2026-09-05 — DESIGN + TOOLS: quiesced STORE ROTATION (item 7) + the dark TAPE-RETENTION prune — `tools/ops/rotate_store.py`, `tools/ops/prune_tape.py`, `ops/tape_retention.py`

**Status: BUILT + GATED, DRY-RUN EXECUTED READ-ONLY against the live store (twice), `--apply` NOT run.**
Branch `build/store-rotation-tool` (worktree `C:/Users/aahys/kct-rotation`). Bot LIVE on `main`
throughout; nothing under the data dir was written (every read `mode=ro`, every heavy step at LOW
priority). **Blast radius of this commit:** two tools (`tools/ops/rotate_store.py`,
`tools/ops/prune_tape.py`), one new module (`src/combomaker/ops/tape_retention.py`), two test files,
and three ADDITIVE live-module edits that are inert until a flag is set: `ops/config.py`
(`observe.tape_retention_enabled: bool = False`), `ops/persistence.py` (`Store.writer_queue_depth()`,
a read accessor), `ops/quote_app.py` (a guarded `if self._tape_retention is not None:` step on the
~60 s slow cadence + construction in `run()` under the flag + close at shutdown). No pricing, risk,
rfq or sim module touched (rule 8); default config = byte-identical behaviour.

## WRONG / FIXED / OPEN (scannable)

| | Item | State |
|---|---|---|
| FINDING | The live store is **213.68 GB** (52,168,228 × 4 KB pages, WAL mode): `rfqs` 66.4M ids + `decisions` 134.3M ids + `would_quotes_inplay` 3.25M rows = the recorder TAPE; every table the bot actually needs to run is **≤ 22,636 rows** (fills 4,343 / position_ledger 4,110 / ev_ledger 4,338 / markouts 22,636 / structural_fits 763 / store_meta 2 / daily_ruin_anchors 2) | measured by a read-only probe (399 s at LOW priority — itself a symptom) |
| FINDING | **Writer collapse per boot, measured from `store_writer_stats`:** 00:52 boot 1,862,536 dropped rows, queue 198–200k; 09:05 boot 31,354 dropped in 36 min; 09:42 boot 1,314,521 dropped in 2 h (queue 200,000 at its 15:40:52Z halt); 11:40 boot 0 dropped / queue 30,260 at 15:47Z (6 min in); **12:13 boot: last `store_writer_stats` at 16:19:02Z (queue 6,326) and last `decisions` row `at` = 16:18:16Z — no tape row landed for the following ~60 min** (0 `store_writer_batch_failed`, 0 lock errors: the writer starved, not crashed). The 12:13 boot's acceptance seed scanned only **7,426 quote_sent rows for 24 h** (`acceptance_tape_seed_result`, 12.5 s) vs the 8/13 live table of 238k — the seed is reading a tape that is 97% holes | evidence for the design; the fix IS the rotation |
| FINDING | **HARD-LINK HAZARD:** the store inode has **3 names** — `D:\kalshi-combos-TWO-data\combomaker-prod-live-wc.sqlite3` (live), `D:\kct-vdata\combomaker-prod-live-wc.sqlite3` (the 8/1 frozen-tape snapshot dir, with its own `-wal` of 23,484,032 B dated Aug 1 + `-shm` Aug 6) and `D:\kalshi-combos-TWO-data\vitals_snapshot\combomaker-prod-live-wc.sqlite3` (dir created 9/4 20:36, **with its own `-wal` of 201,912 B written 08:22–09:04 ET TODAY and a 294,912 B `-shm`**). SQLite forbids two WALs on one file: frames committed through another name are invisible to the live connection, and any open through that name REPLAYS that WAL's pages onto the current file. The tool enumerates these (`fsutil hardlink list`), parses each stray WAL header, and **refuses `--apply` while a foreign WAL holds frames** | OPEN — operator: after STOP_BOT, inspect and move aside `vitals_snapshot\*-wal/-shm` and `kct-vdata\*-wal/-shm`, then delete the two extra links (`tools/vitals/snapshot.py` makes a 3.9 MB COPY; a hard link was never a snapshot) |
| BUILT | `tools/ops/rotate_store.py` — `--dry-run` (default, read-only plan + boot-reader audit + measured throughput + the retention alternative's plan + refusals-now), `--apply` (refuse-first / checkpoint / rename / build at a temp name **through the real `Store.open`** / column-wise copy / verify / swap / manifest **with the exact START_BOT sequence**; rollback on any failure), `--verify --manifest` (post-relight read-only check) | 17 tests on a synthetic store built by the REAL `Store` (`tests/test_rotate_store.py`) |
| BUILT (DARK) | **The ALTERNATIVE: `src/combomaker/ops/tape_retention.py`** — a nightly, bounded prune of `rfqs`/`decisions`/`would_quotes*` rows older than a **DERIVED** window (`SEED_WINDOW_S` + the pass cadence + the tape's MEASURED time disorder — no number of its own), never a protected leg-provenance row, only against an idle tape writer, off-loop on a second connection; wired into `quote_app`'s slow loop behind `observe.tape_retention_enabled` (**default False**); CLI face `tools/ops/prune_tape.py` (dry-run / refuse-if-alive apply) | 13 tests (`tests/test_tape_retention.py`): derivation pins, bisection plan, pass deletes only older rows and splits around protected ids, every bound, boot-reader parity (seed + `held_positions`) before/after, scheduler (due / single-flight / writer-idle / re-arm), dark-flag source pin, CLI refusals |
| GATES | Suite **4,075/0** (baseline 4,045 + 30 new) at LOW priority; vitals fast **8/8 GREEN** from a rebuilt read-only snapshot (`tools.vitals.snapshot` 16:26 ET); `ruff check` clean on every touched file; `mypy --strict` clean on the four touched `src` modules (the full `mypy` run reports 6 PRE-EXISTING errors in `pricing/ising_amm.py` + `pricing/engine.py`, files this branch never touches — same on `main`) | at the commit |
| OPEN | `--apply` execution: operator-gated (STOP_BOT → move the two foreign WALs aside → apply → START_BOT), ~3–5 min of downtime by the measured estimate (the copy itself ≈ seconds; STOP/START/boot dominate); best window = a settled, quote-quiet hour | decision owed |
| OPEN | Arming the prune: `observe.tape_retention_enabled: true` AFTER one rotation (on the 213 GB store the first batch would outlast the writer's lock tolerance and the pass would stop by design — measured below) | decision owed, after the rotation's first window |

## 1. Why a rotation and not a bigger queue / faster disk

The single writer thread commits 1,000-row batches into `rfqs` (3 secondary indexes: `rfq_id`,
`collection_ticker`, `market_ticker` — random-key inserts) and `decisions` (`kind` index). At 213 GB
those B-trees are far outside any page cache, so each index insert is a random 4 KB read on the same
disk the bot's log (3.37 GB for the 00:52 boot alone) and the exchange feed's metadata cache are
hammering. The queue is bounded at 200,000 (`Store.start_writer`) and drops on overflow by design
(hot path never blocks) — the drops are the *correct* behaviour of a writer that cannot keep up; the
store size is what makes it unable to keep up. The 8/19 collapse (150 GB, writer 2h35m behind, 75–80%
of tape dropped) was the same mechanism one size earlier. The WS `communications_channel_lost:
Subscription buffer overflow` drops (59 in 55 min on the 12:13 boot) share the box's I/O and the event
loop's scheduling with the aiosqlite thread; the retained-floor sweep's 5 s `wait_for` timeouts
(`retained_floor_sweep_timeout` 17:14:49Z) are the same saturation seen from the read side.

A rotation gives the writer a store whose indexes fit in RAM (the carried tape is < 0.1% of the
archive). Nothing else on the box changes. The prune (section 6) then keeps it there.

## 2. The rotation, step by step (what `--apply` does)

```
 operator                    rotate_store.py --apply                         disk
 --------                    ----------------------                          ----
 STOP_BOT.bat  ───────►  (1) REFUSE if alive: heartbeat.txt / supervisor_heartbeat.txt /
                             loop_progress.json (repair_phantom_fills' readers + window)
                             + ours_predicate.ps1 process probe
                         (2) plan() read-only; REFUSE if any other hard link has a WAL with frames
                         (3) PRAGMA wal_checkpoint(TRUNCATE); REFUSE unless busy=0, all frames
                             folded, -wal == 0 bytes                                 store + wal
                         (4) os.rename(store -> store.archive-YYYYMMDD) (+ -wal/-shm)  ── atomic; fails
                                                                                         if anyone holds it
                         (5) build store.rotating-<stamp>:                            temp file
                               Store.open(temp) + close  = the LIVE DDL, every idempotent
                                 ADD COLUMN migration, every index, WAL mode (its own thread
                                 + event loop; never a hand-written or copied schema)
                               + archive DDL for tables the bot's DDL does not know
                                 (daily_ruin_anchors, daily_realized_events — vitals-owned)
                               column parity: archive cols ⊆ fresh cols, else REFUSE
                               ATTACH archive ro; BEGIN;
                               INSERT INTO t (cols) SELECT cols  — every LIVE table whole
                                 (store_meta REPLACES the fresh open's 0/now watermark);
                               decisions WHERE kind IN (quote_sent,confirm,decline) AND id>=first_id;
                               rfqs WHERE id>=first_id; rfqs IN (one id per leg set per
                               fills ticker the ledger cannot resolve);
                               sqlite_sequence := archive's; COMMIT;
                               quick_check; COUNT(*) == rowcount per table (+ archive count)
                             any exception ⇒ delete temp, rename archive back      ── rollback
                         (6) os.replace(temp -> live name); manifest ->
                             data/backups/<stamp>-rotate_store_manifest.json  (+ next_steps)
 START_BOT.bat ───────►  bot opens the fresh store (Store.open: idempotent DDL/migrations)
 rotate_store.py --verify --manifest <json>   (read-only post-check)
```

Design choices worth stating:

* **Rename, never copy, the 213 GB.** The archive is the old file under a new name (same inode);
  the rotation copies ~120k rows (≈ 21 MB of row text; 126 MB by the whole-store average) — measured
  below. A rename is atomic on NTFS and fails with a sharing violation while any process has the file
  open — a second, mechanical liveness guard under the evidence-based one.
* **Schema = the live `Store.open`**, exactly as the next boot creates it (task requirement; the
  first cut copied the archive's `sqlite_master` DDL, which would have frozen the archive's column
  ORDER into the fresh store). Rows are copied BY COLUMN NAME: a fresh-only column (an archive that
  predates a migration) takes its DDL default — tested by dropping `fills.exchange_fill_id` from a
  synthetic archive; an archive column the live code no longer creates is a REFUSAL + rollback (data
  is never silently dropped) — tested. The manifest names `archive_only_tables`,
  `archive_only_indexes` and `columns_defaulted`.
* **`store_meta` is carried whole and REPLACES** the watermark the fresh open stamps, so the fills
  verification WATERMARK stays `(4186, 2026-09-04T22:44:44Z)`. Without it
  `_ensure_fills_verification_columns` would leave watermark = 0/now in the fresh store, silently
  re-classifying every booked-but-unverified row as "legacy" (the re-arm would never verify them) —
  this is the single most important row in the copy (tested: `fills_verification_watermark()`
  identical before/after through the real `Store`).
* **`sqlite_sequence` is carried for every AUTOINCREMENT table**, carried or not: fresh ids
  continue above the archive's `sqlite_sequence` at rotation time (`fills` → 4345+, `decisions` → one past the archive's last id). No id ever names two
  rows across the boundary; `fills.id > watermark` keeps its meaning; analysis tools that key on
  `rowid` ranges (tonight's `>= 134238260`) stay unambiguous across archive + fresh store.
* **The tape carry is defined by the readers, not by a retention number.** The acceptance seed reads
  `SEED_WINDOW_S` (24 h — the anchor's own horizon, a measurement partition) of `quote_sent` /
  `confirm` / `decline` decisions and joins `rfqs` by `rfq_id`; the tool imports `SEED_WINDOW_S` and
  pins the kind literals to the seed's source with a test. `held_positions` falls back to the tape
  for a fills ticker with no ledger row → one real `rfqs` row per distinct leg set for exactly
  those tickers (an ambiguous ticker stays ambiguous: fail-closed parity, tested). **The same
  protected set is what the prune never deletes** — one function, `tape_retention.protected_rfq_ids`.
* **Verify before swap**: `quick_check`, and every copied table's `COUNT(*)` must equal the
  `INSERT…SELECT` rowcount (LIVE tables also against the archive's count). A failed verify never
  reaches the live name.
* **The exact next step is printed and written**: `--apply` ends with `START_BOT.bat` (repo root),
  the first-window checks, the `--verify` command with its manifest path, and where the history now
  lives (the archive) — also in the manifest as `next_steps`.
* **Nothing hand-set**: the liveness window is the supervisor's `heartbeat_timeout_s` (live YAML,
  60 s), the busy wait is the store's `BUSY_TIMEOUT_MS`, the carry windows are the readers' own
  constants, the archive suffix is the date.

## 3. Boot-time reader audit (what reads store history, and whether the rotation carries it)

Grep of every `Store.` reader outside `persistence.py` plus the two direct `sqlite3` users;
printed by `--dry-run` (`BOOT_READERS`).

| Reader (module → method) | Tables | Carried? |
|---|---|---|
| `Store.open` — DDL, ADD COLUMNs, unique indexes, `_ensure_fills_verification_columns` | store_meta, fills | **yes** — store_meta whole ⇒ watermark preserved |
| `quote_app` startup rehydrate → `held_positions` | fills, position_ledger, rfqs (fallback) | yes — ledgers whole + one rfqs row per leg set for unresolved fills tickers |
| `quote_app` position reconcile → `has_fill_for_ticker`, `held_positions`, `ledger_quantity_reconcile_once` (`fills_verification_watermark`, `open_ledger_quantity_by_ticker`) | fills, position_ledger, store_meta | yes |
| `quote_app` day-anchored realized seed → `day_realized_pnl_cc` | position_ledger.reconciled_at, fills.at/fee_cc/status | yes |
| `ops/acceptance_seed.seed_counts_from_store` (2nd ro connection, off-thread) | decisions (quote_sent/confirm/decline), rfqs by rfq_id | yes — seed window, bisected on the PK exactly as the seed does |
| `lifecycle` fill-verification re-arm → `fills_verification_watermark`, `booked_unverified_fills` | store_meta, fills | yes |
| `lifecycle` retained-floor sweep → `settled_grade_rows` | position_ledger (settled), fills | yes |
| `lifecycle` ledger-divergence sweep → `open_ledger_identities` | position_ledger (open) | yes |
| `lifecycle` fills-ledger sweep → `fill_order_ids`, `fill_null_order_id_keys` | fills | yes |
| `risk/settlement` orphan reconcile → `open_ledger_tickers`, `open_ledger_rows_for_ticker`, `record_position_settled`, `settle_ev_entry` | position_ledger, ev_ledger | yes |
| `tools/ops/hang_watchdog.store_sig` (last `decisions.at` of the newest `*.sqlite3`) | decisions | window only; the fresh store is the newest `*.sqlite3`, the archive name does not match the glob |
| `tools/vitals/derive.risk_bankroll_cc` / `live_open_positions` (`combomaker*.sqlite3` glob) | daily_ruin_anchors, position_ledger | yes — `daily_ruin_anchors` is written by NO live module (2 rows, latest 2026-07-16 = $2,050.41, the stale anchor the gate already reads); carried from the archive's DDL so the gate keeps an anchor |
| `ops/report.build_report` (CLI `report`) | rfqs, decisions, would_quotes, ev_ledger, markouts, fills | ledgers yes; tape counts restart — `report --db <archive>` for history |
| data_dir FILES: `fee_schedule_observed.json`, `metadata_cache.json`, `watchdog_tape.json`, `fill_prober_watermark.txt`, heartbeat/progress files | — | untouched (the rotation renames one file) |
| `would_quotes_inplay` (in-play shadow, measurement only) | would_quotes_inplay | **no** — 3.25M rows, no boot reader; the archive keeps the study |
| `fee_schedule` | — | **there is no such table**: the fee observer persists to `data/fee_schedule_observed.json` (179,699 B, rewritten 12:29 ET) — unaffected |
| `daily_realized_events` | 0 rows, no reader in `src/` | carried (empty) |

Parity proof in `tests/test_rotate_store.py::test_fresh_store_opens_with_the_real_store_and_boot_readers_agree`:
before/after rotation through the real `Store.open`, `fills_verification_watermark`, `held_positions`
(ledger-first, tape-fallback, conflicting-set rejection), `settled_grade_rows`, `fill_order_ids`,
`open_ledger_identities`, `day_realized_pnl_cc`, `booked_unverified_fills` return identical values,
and the next `record_fill` takes the next id above the archive's.

## 4. Risk list — what breaks if a small table is NOT carried

| Not carried | Consequence |
|---|---|
| `position_ledger` | The settlement poller cannot close open rows from exchange truth (orphan reconcile finds nothing); `day_realized_pnl_cc` seeds 0 → p_night degrades to p_book; the retained-edge floor has no settled grade rows → fee-only floor everywhere; the ledger-divergence and quantity alarms fire on every position; rehydration loses its durable leg provenance (only the tape fallback remains). **Catastrophic for risk truth.** |
| `fills` | `held_positions` returns nothing → every exchange position is "unmodeled" and RESERVED from exchange figures (never zero, but no legs → no clustering/mutex); `fill_order_ids` empty → the fills-ledger sweep alarms every historical exchange fill as a MISS; `has_fill_for_ticker` false everywhere; the re-arm has no claims; the day seed loses fee reversal. |
| `store_meta` | Watermark re-stamped at rotation → every unverified booked row becomes "legacy" and is never re-armed for verification; the ledger-quantity alarm's post-fix scope shifts. Silent. |
| `ev_ledger` | `settle_ev_entry` UPDATEs hit no row → realized EV grading stops for pre-rotation fills; `ev_summary` restarts. |
| `markouts` | Only the CLI report's `markout_summary` loses history (recorder keeps writing). |
| `structural_fits` | Telemetry history lost (the 9/4 build's 763 ACCEPT/CHALLENGE rows); no reader at boot. |
| `daily_ruin_anchors` | `tools.vitals.gate` "refuses to invent a bankroll" if no `combomaker*.sqlite3` under data/ has an anchor — the demo/old stores there might; a stale anchor either way (vitals owner's item). |
| seed-window `decisions`/`rfqs` | The 8/1 brick: acceptance tape empty at boot → CP-lower P(accept) = 0 in every bucket → only dES99 ≤ 0 diversifiers admitted until the live tape refills (~hours). |
| leg-provenance `rfqs` rows | A held ticker with fills but no ledger row (pre-P1.10 legacy) rehydrates from EXCHANGE figures instead of its legs (deterministic max loss preserved, clustering blind). The dry-run counts exactly how many such tickers exist today (below). |
| `sqlite_sequence` | ids restart at 1 in the fresh store: `fills.id > watermark(4186)` would be FALSE for every new fill until id 4187 → the verification re-arm blind to the first 4,186 post-rotation fills; archive/fresh rowid collisions for every analysis tool. |

## 5. Dry-run against the live store (READ-ONLY, LOW priority, bot up)

Command (worktree, `PYTHONPATH=src`, `start /LOW /B /WAIT`):
`python tools/ops/rotate_store.py --dry-run --out <scratch>/plan_live2.json` — the FINAL tool, run
16:27–<<DRYRUN2_END>> ET with the suite and the vitals gate running beside it (the first run, 13:37–13:52 ET
on the earlier cut, took 942 s; its numbers are quoted where they differ).

```
<<DRYRUN2>>
```

Reading the dry-run:

* **The writer is crawling, quantified.** Read-only probe at ~17:15Z: `MAX(decisions.id)` =
  134,303,838, last `at` = 16:18:16Z. First dry-run at ~17:52Z: 134,304,442, last `at` = 16:18:24Z
  (**+604 rows in ~37 min ≈ 0.27 rows/s, enqueue stamps spanning 8 s** — draining a queue enqueued
  at 16:18Z, an hour and a half behind, three orders of magnitude below intake). Second dry-run
  (this section, ~20:30Z): <<WRITER_PROGRESS>>. The 12:13 boot's `store_writer_stats` went silent
  because the emit rides the write count (5,000 writes per emit) — a starved writer cannot report
  starvation.
* **The 24 h seed window** holds <<SEED_ROWS>> against ~400k real sends/day — the tape is ~98%
  holes, exactly the `acceptance_tape_seed_result` the 12:13 boot logged (7,426 rows scanned). The
  seed the fresh store boots on is therefore thin either way; what the rotation buys is that the
  NEXT 24 h are recorded.
* **Leg provenance:** <<PROVENANCE>> — those unresolvable tickers are reserved from exchange
  figures today already and will be after (no change).
* **Carry ≈ <<CARRY>>.** Measured on this saturated box: <<MEASURED>> → **copy estimate ≈
  seconds (read × 3)**; the index builds on ~120k rows are sub-second. The rotation's own wall time
  is seconds; the downtime is STOP_BOT + START_BOT + the bot's boot (~2–3 min per the 8/26 and 9/4
  relights). **≈ 3–5 min end to end.** Not carried: ~203.8M tape rows (the archive).
* **The retention alternative on THIS store (read-only plan):** <<RETENTION>>. The measured
  disorder is the term that makes the window derived rather than set: `rfqs.seen_at` is stamped at
  worker pickup and recorded after dispatch, so consecutive ids can carry time stamps a few
  seconds out of order — the bisection's boundary error, measured on the newest 25k rows of each
  table and added to the window.
* **Refusals that fire now** (all correct): the three liveness files beaten < 1 s ago, our
  processes by the launch-site predicate (bot, watchdog, prober, monitors), the live WAL's frames
  (expected; `--apply` checkpoints it), and the **two foreign WALs**: `D:\kct-vdata\…-wal` 5,700
  frames / checkpoint_seq 8 (Aug 1) and `…\vitals_snapshot\…-wal` 49 frames / checkpoint_seq 1
  (written today 08:22–09:04 ET). Whoever opened the store through `vitals_snapshot\` today wrote
  49 frames the live bot has never seen; they are not in the main file (its WAL checkpoint_seq is
  far past 1, so replaying them later would be wrong either way). Operator action before `--apply`:
  move both `-wal`/`-shm` pairs aside (keep them for forensics), delete the two extra hard links.
* `would_quotes` and `rfq_deletions` are empty on the live store (the recorders are not wired).

## 6. The ALTERNATIVE — BUILT DARK: cap the recorder tables by a DERIVED retention window + nightly prune

**What it is.** `src/combomaker/ops/tape_retention.py` + `tools/ops/prune_tape.py` +
`observe.tape_retention_enabled` (default `false`).

```
 retention_s = max(reader windows)   SEED_WINDOW_S (86,400) — the longest boot-time tape reader
             + PRUNE_CADENCE_S       86,400 — one pass per NIGHT (the anchors are per-night
                                     quantities; the seed window is the night); a pass may land
                                     anywhere in its period, so the row a reader needs at the END
                                     of the period was window+cadence old when the pass at its
                                     START ran
             + measured disorder     the largest BACKWARD step of the time column over the newest
                                     25k rows of each tape table (rfqs.seen_at is a pickup stamp
                                     recorded after dispatch) — the bisection's boundary error
                                     = 172,800 s + <<DISORDER>> on the live store today

 protected  = one real rfqs row per distinct (market_ticker, legs_json) for every fills ticker
              without a position_ledger row — Store.held_positions' tape fallback; the SAME
              function the rotation carries (134 rows today)

 pass       = plan read-only on a SECOND stdlib connection (never the aiosqlite writer thread)
              for each tape table: start at the first UNPROTECTED id, delete [lo, lo+25k) at a
              time (acceptance_seed._CHUNK_IDS — the seed's own chunk), each batch its own
              BEGIN IMMEDIATE / COMMIT, split around protected ids; stop when
                * should_continue() is false  (the app passes "writer queue empty"),
                * a batch took > STORE_OP_TIMEOUT_S (5 s — the writer's own lock tolerance;
                  a longer hold could fail the writer's commit ⇒ "store too slow to prune live"),
                * PRUNE_CADENCE_S / STORE_OP_TIMEOUT_S = 17,280 batches (a pass never outlasts
                  its own period, so passes never overlap)
 scheduler  = TapeRetentionStep: DUE when no pass has COMPLETED within the cadence (an
              incomplete pass re-arms next minute — bounded leftover, keeps trying against an
              idle writer); SINGLE-FLIGHT; launched only while Store.writer_queue_depth() == 0;
              asyncio.to_thread; errors log + retry; called from quote_app._maintenance_loop
              every 120 ticks (~60 s, the metadata-flush / capacity-probe cadence) ONLY when
              self._tape_retention is not None (constructed under the flag in run()).
```

**What it does NOT do.** `DELETE` returns pages to the freelist: the FILE does not shrink — that is
the rotation's job, once. It stops the store growing past ~2 days of tape (≈ 7–8 GB at today's
flow), so the B-trees the writer inserts into stay small forever and no further rotation is needed.
Never `VACUUM` the live store (a rotation-sized outage with none of the rotation's safety).

**Why it is the SECOND step, never the first (and why the mechanism enforces that):** on the
213 GB store a 25k-id `DELETE` touches ~25k random leaf pages across four indexes on a saturated
disk — far over the 5 s batch bound — so the first batch ends the pass with
`stopped_reason = "batch took … > STORE_OP_TIMEOUT_S"` and the scheduler retries next minute (each
retry one bounded batch, only while the writer is idle, which on the collapsed store it never is).
Arming the flag today would therefore do (almost) nothing, by construction; after the rotation the
same pass deletes a night's tape in ~150 batches of milliseconds. No knob changes between the two:
the store's own timeout is the judge.

**Tests (`tests/test_tape_retention.py`, 13):** the window pins (`= SEED_WINDOW_S + PRUNE_CADENCE_S
+ disorder`, negative disorder cannot shrink it; batch = `_CHUNK_IDS`; cap = cadence / timeout); the
disorder measurement on a crafted out-of-order tape (9 s against the running max; 0 for monotone;
empty table); the plan's bisection on a 3-day hourly synthetic tape (48 h window ⇒ exactly the
oldest 24 h below the bound; empty tables never error; protected ids all inside the prune range);
a full pass deletes exactly the older rows, keeps every protected row, preserves ids, leaves the
ledgers untouched, and **the acceptance seed and `held_positions` (through the real `Store`) answer
identically before and after**; a second pass finds nothing; the bounds (predicate false ⇒ 0
batches; `max_batches=2` ⇒ 2 and resumable; a batch over the time bound ⇒ stop with the reason;
small batches ⇒ completes, starting past the protected prefix); `_delete_range` splits one batch
around protected ids; the scheduler (writer busy ⇒ no launch; launched ⇒ in_flight ⇒ not_due until
the cadence; an incomplete pass re-arms; a raising pass logs and re-arms; close cancels);
`Store.writer_queue_depth()`; the dark default + a source pin that `QuoteApp.run` constructs the
step only under `config.observe.tape_retention_enabled` and `_maintenance_loop` only ever calls
`maybe_launch()` behind `is not None` and never awaits it; the CLI dry-run is read-only and `--apply`
refuses a live heartbeat; `--apply` prunes to completion when the bot is down.

**A third option considered and rejected:** moving `rfqs`/`decisions` to a second database file
(ATTACH) so the ledgers live in a small file — it changes `Store` (rule 8 port + parity, a live
pricing-path module's dependency), and the tape writer would still bloat the second file: same
problem, one file over.

## 7. What was NOT done / limits

* `--apply` was not run (operator-gated by the task); `prune_tape.py --apply` was not run either
  (it refuses while the bot is alive, and the live store is the wrong place to start it).
* The tool does not stop/start the bot or the watchdog; STOP_BOT/START_BOT remain the operator's
  buttons (the watchdog must die first — STOP_BOT does that; a watchdog alive during the rotation
  would relight into a renamed store).
* `would_quotes_inplay` is not carried (no boot reader); the in-play study continues in the fresh
  store from zero, the archive holds the 3.25M rows. The prune treats it as tape (pruned).
* The prune is DARK and untested live; its first arming should be watched for one night
  (`tape_retention_pass` log line: `complete`, `batches`, `rows_deleted`, `slowest_batch_s`,
  `stopped_reason`).
* Analysis tools with `DEFAULT_STORE = D:/…/combomaker-prod-live-wc.sqlite3` hard-coded (38 files
  under `tools/`) will read the FRESH store after rotation and must be pointed at the archive with
  their `--store`/`--db` flags for history.

## NEXT STEPS

* **Operator (decision owed):** approve the rotation window. Sequence: `STOP_BOT.bat` → move
  aside the two foreign `-wal/-shm` pairs (`D:\kalshi-combos-TWO-data\vitals_snapshot\`,
  `D:\kct-vdata\`) and delete those two extra hard links → `PYTHONPATH=src python
  tools/ops/rotate_store.py --apply` (main after merge) → follow the printed NEXT STEPS
  (`START_BOT.bat` → first-window verify: sends/min in the 300–460 band, `store_writer_stats` queue
  near 0 / dropped 0, `acceptance_tape_seed_result` rows_scanned in the hundreds of thousands,
  `retained_floor_sweep_timeout` gone → `rotate_store.py --verify --manifest
  data/backups/<stamp>-rotate_store_manifest.json`).
* **Operator (after one clean rotation window):** arm the prune with ONE yaml line under `observe:`
  — `tape_retention_enabled: true` — at the next restart; watch the first `tape_retention_pass`.
* **Builder (after merge):** a `store_writer_stats` emit on a TIME cadence as well as the write
  cadence (the 12:13 boot's writer went silent for an hour with no emit because emits ride the
  write count — a starved writer cannot report that it is starved).
* **Vitals owner:** `daily_ruin_anchors` is written by nothing since 7/16; the gate's bankroll is
  stale ($2,050.41 vs ~$7.3k equity) — either wire the anchor writer or read `balance.py`'s
  anchor.
* **Orchestrator:** merge `build/store-rotation-tool`; the report index row is in
  `docs/reports/README.md`; yaml for main: `observe:` / `  tape_retention_enabled: false`
  (documenting the dark default — optional, the code default is already false).
