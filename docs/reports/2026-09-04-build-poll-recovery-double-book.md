# 2026-09-04 — Build: item D "poll-recovery double-book" → PHANTOM EXECUTIONS (fill ledger)

Branch `build/poll-recovery-double-book`. Bot DOWN throughout; the live store was
read with `mode=ro` only; nothing was started. Continues a previous builder's
worktree (its uncommitted `persistence.py` schema/index/void work was kept; its
diagnosis scripts were re-run and their findings are reproduced below).

## WRONG / FIXED / OPEN

| Claim | Verdict | Truth / what shipped |
|---|---|---|
| "Poll recovery DOUBLE-BOOKED the fill ledger: 4 + 2 extra rows on AC104B1B2E5 / 1F4E0958F23" | **WRONG (retracted)** | The six rows are six DISTINCT quotes (own quote_id / rfq_id / exchange order_id / `client_order_id=quote:<hash>:<quote_id>`), each with its own `quote_accepted → candidate_gate_confirm → risk_reservation_committed → quote_executed_msg` chain. The live store holds **0 duplicate order_ids and 0 duplicate fill_refs** (4,185 rows). Nothing was booked twice. |
| "The extra rows came from the poll path" | **WRONG** | 27 of the 28 store-only rows since 7/27 carry `_ws_recv_mono_ns` (WS-delivered `quote_executed`); 0 carry `recovered_via_poll`. The WS message arrived first for every one of the six; the poll fired 8–10 s later and lost the race (`fill_replay_skipped` / `fill_order_id_already_in_ledger`). |
| What actually happened | **DIAGNOSED** | **PHANTOM EXECUTIONS.** The exchange emitted `quote_executed` (WS) AND returned `status: executed` (REST GET quote) for orders that never produced a `/portfolio/fills` row and never became a position: settlement `no_count_fp` = **43.47** (= the one matched fill) and **29.10** (= the three matched fills); revenue 4347¢ / 2910¢. 28 such rows exist since 2026-07-27 (12 on 8/26 alone, 6 on the two halting tickers). The ledger believed the exchange's CLAIM; nothing ever checked it against the tape. |
| "fill_record_recovered_via_poll: quote_executed WS message never arrived" (75×, 8/26 run) | **FALSE ALARMS** | The WS `quote_executed_msg` line precedes each recovery by 8–10 s (e.g. 7d391d94: WS 20:36:38.263Z, recovery 20:36:46.754Z, row `at` 20:36:57, `fill_replay_skipped` 20:37:05). The handler was stalled inside `_record_executed_fill` on the saturated store (row landed 19 s after the message); the sweep polled REST for a message it already held. Fixed: the sweep replays the HELD message and never REST-polls while a write is in flight. |
| "fill_order_id_already_in_ledger … under ANOTHER fill_ref" (38×) | **MIS-LABELLED** | Every one of the 38 was the SAME quote (fill_ref == the row's fill_ref) — the WS handler and the poll replay passing the `has_fill` pre-check before either wrote. Zero cross-quote misattributions. Fixed: the guard now distinguishes a same-quote replay (info skip) from a cross-quote conflict (error, never a second row). |
| Halt `halt_reconciliation_mismatch` 22:41 ET (66.71 predicted vs 43.47 exchange) | **EXPLAINED** | Σ(43.47 real + 14.00 + 3.00 + 3.24 + 3.00 phantom) = 66.71. Correct halt on a wrong ledger. |
| FIX | **SHIPPED** | Every booked fill is now a CLAIM until its exchange `order_id` is found on `/portfolio/fills` (status `booked → verified`); absent after the bounded verification ⇒ `phantom` (fills/position_ledger/ev_ledger voided, in-memory position + reservation + receivable removed, booked fee reversed). A REST `executed` status with no WS message books NOTHING from the status — only a positive tape match keyed on `creator_order_id` books the row (exactly once). Partial UNIQUE index on `fills(order_id)`; order_id-aware INSERT-if-absent. Per-ticker `ledger_quantity_mismatch` alarm vs `/portfolio/positions`. Repair tool `tools/ops/repair_phantom_fills.py`. |
| Legacy 7/18 rows `fill:*-reconciled` (3 rows, order_id truncated to 13 chars) | **NOT PHANTOM** | Manual-reconcile rows whose `order_id` is a PREFIX of the tape's (b2c301fc-eecd… etc.); settlement 400.22 = store Σ. The repair tool classifies them `legacy_prefix_match`, untouched. |
| 73 tape fills with no store row (all maker; 8/26 boots #6–#16 and earlier outages) | **OPEN (unrepaired, listed)** | The opposite direction (writer-path misses across restarts/outages). Not this item; enumerated by the tool's dry-run. |

## The mechanism, with log lines (D:/kalshi-combos-TWO-data/live_20260826_1521.log, grep only)

Quote 7d391d94 (AC104B1B2E5, 14.00 NO @ 0.6820), all times Z:

```
20:36:35.202  quote_accepted            contracts_accepted_fp 14.00, rfq 6b15ff92
20:36:36.026  candidate_gate_confirm
20:36:36.072  risk_reservation_committed  reservation fill:7d391d94
20:36:38.263  quote_executed_msg (WS)   order_id 01a03fc9-fc08-78af-bcf3-a101bf951f6d,
                                        client_order_id quote:00be467a:7d391d94-…, executed_ts 20:36:37.726
20:36:46.754  fill_record_recovered_via_poll  "quote_executed WS message never arrived"  ← FALSE (see 20:36:38)
20:36:46.754  quote_executed_msg (synthesized, recovered_via_poll: true, same order_id)
20:36:57      fills row written (at=20:36:57)          ← 19 s after the WS message: store saturated
20:37:05.430  fill_replay_skipped       (the losing racer)
```

Exchange truth for order 01a03fc9-fc08…: **no `/portfolio/fills` row** (4,228-row all-time pull,
`alltime_exchange.json`); settlement of AC104B1B2E5 on 8/27 02:41Z: `no_count_fp 43.47`
(= order 01a03ee0… alone), `revenue 4347`. Same shape for 23cf57f1, a95d7a46, cbdc3e8e
(AC104B1B2E5) and 10a338ad, 40b6da4a (1F4E0958F23: three real fills 12.20+9.66+7.24 = 29.10 vs
store 102.49); 40b6da4a's second racer hit `fill_order_id_already_in_ledger` (21:15:47) instead
of `fill_replay_skipped` — same quote, same fill_ref.

Measured base rates that size the fix (no hand-set numbers):

| Measurement | Value |
|---|---|
| Real fills: tape `created_time − executed_ts` (4,053 matched rows) | min 0.005 s · p50 0.014 s · p90 0.031 s · p99 0.061 s · **max 0.486 s**; 0 rows > 10 s |
| Verification budget reused (existing `fill_cancel_verify_attempts=3` × `fill_cancel_verify_delay_s`; code default 90 s, **live YAML 15 s** — corrected in the review-fix pass) | 3 tape reads, the last at +2·delay: +30 s live (≥ 61× the max observed exchange-stamp latency), +180 s at the default (≥ 370×); a real fill absent on the FIRST read has a measured base rate of 0/4,053 — but see the REST-visibility caveat under Counterfactual and the pre-registered `fill_verified_late` alarm |
| Phantom `quote_executed` claims since 7/27 | 28 (27 WS-delivered, 1 other); 12 on 8/26 (364.79 contracts); premium claimed Σ contracts×price = $831.35 of positions never held |
| Store duplicate order_ids / fill_refs | 0 / 0 |
| Tape fills carrying `client_order_id` | 0 / 4,228 ⇒ the exact join key is `Fill.order_id == Quote.creator_order_id`; `client_order_id` (WS only) is evidence, not a key |

## What changed (mechanism, not numbers)

```
                    exchange claim                         exchange PROOF
  quote_executed (WS) ──┐                    ┌── GET /portfolio/fills?ticker&order_id=<order_id>
  status: executed (REST)┘                   │        (exact join: Quote.creator_order_id == Fill.order_id)
          │                                  │
          ▼                                  ▼
  on_quote_executed ── record_fill ──► fills.status='booked' ──► sweep: _execution_verification_step
   (held msg, in-flight flag)                 │                    attempt n due at start + n·delay
                                              │                    (existing fill_cancel_verify_* cadence)
                                              ├── print found ──► 'verified' (+exchange_fill_id, verified_at)
                                              └── ≥1 clean read, none after budget ──► VOID
                                                   fills→'phantom' · position_ledger→'phantom' · ev_ledger→0/0
                                                   fee reversed · position+reservation+receivable removed
  REST executed, NO WS msg ──► verify_mode 'executed_status': NOTHING booked from the status;
                               tape match ⇒ _replay_verified_fill (row written once, verified at write);
                               verified absence ⇒ confirm-booked position discarded, no row.
```

| File | Change | Mechanism |
|---|---|---|
| `src/combomaker/ops/persistence.py` (previous builder's schema work, kept + finished) | `fills.status` (`booked`/`verified`/`phantom`), `verified_at`, `exchange_fill_id` — idempotent `ALTER` migration guarded by `PRAGMA table_info`; partial `UNIQUE INDEX idx_fills_order_id_unique ON fills(order_id) WHERE order_id IS NOT NULL`, tolerant of legacy duplicates (enumerated in a loud error, never a crash — same pattern as `idx_fills_ref_unique`); `record_fill` INSERT-if-absent also refuses a non-NULL `order_id` already recorded under ANY fill_ref; new `fill_ref_for_order_id`, `fill_status`, `mark_fill_verified` (booked→verified only), `void_phantom_fill` (booked→phantom across fills/position_ledger/ev_ledger, synchronous + committed), `open_ledger_quantity_by_ticker`; `fill_order_ids`/`fill_null_order_id_keys`/`day_realized_pnl_cc` exclude phantom rows (a later tape print for a voided order re-alarms as a MISS; a voided fee never counts). | One exchange order = one row (index + store guard + writer guard); a row carries its proof state. |
| `src/combomaker/rfq/lifecycle.py` | `OpenQuoteState`: `verify_mode`, `executed_msg`, `fill_write_inflight`, `fill_write_started_mono_ns`, `fill_write_stall_alarmed`, `fill_fee_booked_cc`, `exec_verified`, `exec_voided`. `on_quote_executed` holds the first executed message, marks the write in flight, and after a NEW row calls `_on_fill_row_written` → tape-proven rows (`exchange_fill` on the message) are stamped verified at write; otherwise `_start_execution_verification(order_id)`. New `_execution_verification_step` / `_tape_prints_for_order` / `_mark_execution_verified` / `_resolve_execution_verification` / `_void_phantom_execution` / `_unbook_position`. `_record_executed_fill` returns whether it inserted and uses `fill_ref_for_order_id`: same fill_ref ⇒ `fill_replay_skipped` (info), other fill_ref ⇒ `fill_order_id_already_in_ledger` (error) — neither books. `_sweep_unrecorded_fills`: verify_mode `executed` states step first; a state holding an executed message is NEVER REST-polled — in flight ⇒ leave (once-per-quote `fill_ledger_write_stalled` after the recovery delay), failed ⇒ `fill_record_replayed_from_held_message`; REST `executed` with a fills getter ⇒ verify_mode `executed_status` (exact-key claim at discovery, `fill_recovery_executed_status_verifying`) and the existing `_cancel_verification_step` machinery proves/discards it (`fill_recovery_executed_status_verified` / `fill_recovery_executed_status_phantom`); without a fills getter the prior direct booking stays, logged UNVERIFIED. `_adopt_exchange_fill` lets the exact-key owner pass its own claim; `_release_fill_claim` releases the discovery-time claim. | The exchange's executed message is a claim; `/portfolio/fills` is the proof; a claim the tape never backs is voided — automatically, on the existing verify cadence. |
| `src/combomaker/ops/quote_app.py` | `ledger_quantity_reconcile_once(store, exch_by_ticker, metrics)` — per-ticker OPEN `position_ledger` (side, Σ contracts_centi, rows) vs the exchange's `/portfolio/positions` row: kinds `quantity` / `side` / `ledger_only` / `exchange_only`; WARNING `ledger_quantity_mismatch` (bounded to 20 rows) + metrics `ledger_quantity.mismatch[.kind]`, INFO `ledger_quantity_mismatch_clean`. Called inside `position_reconcile_unmodeled_once` on the payload it already fetched (no second GET), alarm-only, never a writer. | The durable ledger the settlement reconcile grades against now self-reports against exchange truth (the in-memory book already did — `position_reconcile_quantity_divergence` fired at 20:37:08Z on 8/26, 31 s after the first phantom, and every 5 min after; nothing acted on it). |
| `tools/ops/repair_phantom_fills.py` (new) | Read-only join of the local fills ledger vs `/portfolio/fills` + settlements (saved JSON or `--pull`): classes `matched` / `legacy_prefix_match` / `phantom` (settlement-corroborated) / `unresolved` / `null_order_id`, plus tape-only fills and matched-count mismatches. `--apply` = timestamped backup under `data/backups/` (+WAL/SHM sidecars), refuses on a fresh heartbeat, mirrors `Store.void_phantom_fill` (parity-tested to the row). NOT run with `--apply`. | Operator repair path for the 28 historical rows. |
| `tests/test_phantom_execution_verification.py` (new, 18 tests) | See "Tests". | |
| `tests/test_fill_cancel_verification.py` | Two pins deliberately changed with inline citations: `test_ws_execution_during_verification_single_row` ("never polled after the WS row" → "polled ONCE by exact order_id, verified, then never"; the scripted tape now carries the WS order) and `test_claimed_order_blocks_concurrent_verification_steal` (one extra tick: an executed status is verified before its first write). | |

Numbers: none new. The verification cadence reuses `fill_cancel_verify_attempts=3` × `fill_cancel_verify_delay_s` (code default 90 s; **the live `config/prod-live-wc.local.yaml` sets 15.0 s since 2026-07-25** — correction in the review-fix pass: the first draft of this report quoted only the default); the stall alarm reuses `fill_record_recovery_after_s=10 s`; the replay budget reuses `_FILL_RECOVERY_MAX_ATTEMPTS`; rounds reuse `_CANCEL_VERIFY_MAX_ROUNDS`. Mode strings only. `PREFIX_MIN_LEN=13` in the repair tool is the measured length of the three 2026-07-18 truncated ids (two UUID dash-groups), used only to classify those rows.

## Tests

| Suite | Result |
|---|---|
| `tests/test_phantom_execution_verification.py` (NEW, 18) | 18 passed: phantom WS execution voided after 3 clean reads (fills/position_ledger/ev_ledger states, fee reversed to the cent, position+reservation gone, order id leaves `fill_order_ids`, replay refused, terminal); real WS execution verified on the first read + terminal; tape count mismatch alarm-only; all-reads-errored keeps row+position (unresolved, terminal); genuine missed WS → REST executed + tape ⇒ recovered EXACTLY once (verified at write; late WS replay skipped; no further polls); REST executed with no tape fill ⇒ nothing booked, position discarded, claim released; no fills getter ⇒ prior direct booking (UNVERIFIED); **8/26 replay** (slow write: no REST poll, one stall warning, one row, poll-synthesized + WS replays skip, verified next tick); failed write replays the HELD message (no GET quote; the original WS payload lands); same-quote order_id race = replay skip / cross-quote = conflict with no second row + store-level belt; legacy DB with duplicate order_ids opens + logs (`fills_verification_columns_added`, `fills_order_id_unique_index_unavailable` n=1) and stays usable, re-open idempotent; fresh DB has the partial unique index (raw duplicate ⇒ IntegrityError, NULLs exempt); `void_phantom_fill` state machine; `ledger_quantity_mismatch` (4 kinds, alarm-only, clean case) + wiring through `position_reconcile_unmodeled_once`; repair tool classification on the 8/26 shape; repair tool `--apply` == `Store.void_phantom_fill` row-for-row (backup is the pre-void state; idempotent). |
| `tests/test_fill_recovery.py`, `tests/test_fill_cancel_verification.py`, `tests/test_persistence.py`, `tests/test_ledger_durable_identity.py`, `tests/test_book_completeness.py` | 101 passed (2 pins deliberately changed, cited inline) |
| Full suite | **3,796 passed / 0 failed / 3 deselected (integration) in 5:06** on the final code, nothing else running. Three runs total: (1) 3,794/0 before the last two repair-tool tests were added; (2) 3,795 passed / 1 failed while three other pytest processes were loading the machine — `tests/test_confirm_priority.py::test_mid_storm_accept_jumps_the_backlog` on its own wall-clock sanity assertion (`without_lane >= storm_s * 0.5`: 9.05 ms vs 18.9 ms × 0.5), a dispatcher-latency measurement on a path this branch does not touch; the module passes 3/3 in isolation; (3) the clean run above. Main baseline was 3,778; +18 new tests. |
| `ruff check` on every changed/new file | clean (18 pre-existing findings elsewhere in `tests/` are identical on main, untouched) |
| `mypy --strict src/combomaker` | 0 errors in changed modules; 6 pre-existing errors in `pricing/ising_amm.py` / `pricing/engine.py` (identical on main, untouched) |
| `tools.vitals.gate` | NOT run — per the orchestrator's instruction it runs after merge (the rfq/ and ops/quote_app.py paths changed, so it must). |

## Parity

| Check | Result |
|---|---|
| Repair tool `classify()` vs the previous builder's read-only evidence scripts on the live store + all-time JSON | identical: 28 phantom / 3 legacy prefix / 4,154 matched / 0 null; the six named quotes are all in the phantom set |
| Repair tool `apply_void()` vs live `Store.void_phantom_fill()` on identical seeded stores | row-for-row identical across fills / position_ledger / ev_ledger (test) |
| Recovered-row values via the verified path vs the WS path | unchanged from the 2026-07-16 guarantee (the row is still written by `on_quote_executed`; `test_recovered_row_values_identical_to_ws_path` passes) |

## Counterfactual / quote-ability

- **8/26 replay:** with the fix, each of the 12 phantom rows booked that day would have been VOIDED after 3 clean tape reads — attempts at +0 / +delay / +2·delay with the void on the final one, i.e. ≈ 2·`fill_cancel_verify_delay_s` after booking: **~30 s at the live 15 s setting** (~3 min at the 90 s code default); AC104B1B2E5's ledger would have read 43.47 = exchange at the 22:41 ET settlement, and `halt_reconciliation_mismatch` could not have fired. The 75 false "never arrived" recoveries and 38 mislabelled conflicts become 0 REST polls and 0 errors (held-message path).
- **Real fills:** 4,053/4,053 matched rows have their tape print within 0.486 s of `executed_ts` ⇒ expected to verify on the first read (one extra `/portfolio/fills` GET per fill, ~100–400/day, on the maintenance sweep's existing 3-polls-per-tick budget). **Evidence caveat (review):** 0.486 s is `created_time − executed_ts` — two EXCHANGE stamps — not the REST visibility lag of `/portfolio/fills`, which has not been measured; the budget's anchor is the pre-existing 2026-07-18 verify-before-discard cadence (which found all 14 of the 7/25 partial fills on the FIRST poll). Because a wrong void is an UNDERCOUNT, a real fill that verifies only on attempt > 1 is now a pre-registered alarm (`fill_verified_late`, see Review fixes).
- **Quote-ability:** pricing, caps, sizing and the quote/confirm hot path are UNTOUCHED — there is no quote-ability change to prove; `on_quote_executed` gained one dict copy + two flag writes (O(1)); the sweep's added work is O(#states awaiting verification) per tick, bounded by the existing per-tick poll budget. Throughput of `handle_rfq` → `create_quote` is not on any changed path.

## Blast radius

Fill recovery (`_sweep_unrecorded_fills` + `on_quote_executed`'s ledger tail), the fills/position/EV ledgers (new columns + a partial index), one alarm in the 5-minute position reconcile, one ops tool. Pricing, risk caps, sizing, the quote/confirm path and the settlement seam are untouched. The store migration is additive and idempotent; a legacy store with duplicate order_ids opens (test). VOID is the only new writer and only ever moves `booked → phantom` after ≥1 successful tape read with the order absent through the whole budget.

## Dry-run against the live store (READ-ONLY; `--apply` NOT run)

`PYTHONPATH=src python tools/ops/repair_phantom_fills.py --store D:/kalshi-combos-TWO-data/combomaker-prod-live-wc.sqlite3 --exchange-json alltime_exchange.json --dry-run`

| Class | Rows | Detail |
|---|---|---|
| matched | 4,154 | exact `order_id` join |
| legacy_prefix_match | 3 | the 2026-07-18 `fill:*-reconciled` rows (truncated ids) — real, untouched |
| **phantom** | **28** | 9 `settlement_equals_matched_rows` (incl. **all six**: 7d391d94, 23cf57f1, a95d7a46, cbdc3e8e on AC104B1B2E5; 10a338ad, 40b6da4a on 1F4E0958F23; plus 05ec10e1 + d6449bd6 on 9D960791507 and bad310d8 on 1B4108F9230) + 19 `no_settlement_no_tape_fill`; 27 WS-delivered, 1 other; 12 on 8/26; Σ contracts×price = $831.35 never held |
| unresolved / null_order_id | 0 / 0 | |
| tape-only (exchange fills with NO store row) | 70 | = the brief's 73 minus the 3 prefix-matched; 220,965 centi-contracts; 3 taker prints (5AFEF86276D, 8/9), the rest maker fills across 7/23–8/27 (bot-down/restart windows) — **unrepaired, listed** |
| matched rows whose tape count differs | 7 | booked > tape (partials never resized: 4071/3071, 5640/3940, 1850/1465, 966/573, 3910/3519, 10384/10033, 2816/2777) — **unrepaired, listed** |

`--apply` would mark exactly the 28 phantom rows (fills → phantom; their 28 still-`open` position_ledger rows → phantom; ev rows → 0/0) after a timestamped ROW-LEVEL backup under `data/backups/` (review fix — no whole-store copy). **The live store has NO verification columns yet** (verified read-only in the dry-run: `store_has_verification_columns: false`, `apply_requires_migrate: true`), so `--apply` today must be run as `--apply --migrate`: the migration adds the three columns + `idx_fills_status` + the once-only `store_meta` watermark exactly as the bot's own `Store.open` would (parity-tested), so the bot's first open after the repair changes nothing. Re-run in the review-fix pass (read-only, `mode=ro`): identical numbers — 4,185 rows; matched 4,154 / legacy_prefix_match 3 / phantom 28 (all six named quotes present) / unresolved 0 / null_order_id 0; tape-only 70; count mismatches 7.

## Review fixes (2026-09-04 adversarial review: SHIP_WITH_FIXES)

Fix pass on the same branch; commits `bdb6540` (store), `e4df4ad` (lifecycle), `b092e6a` (tool). Bot DOWN throughout; live store read `mode=ro` only.

| # | Finding | Class | Verdict | What changed |
|---|---|---|---|---|
| 1 | `repair_phantom_fills._heartbeat_fresh` looked for `heartbeat.json` / `heartbeat` / `liveness.json` — files the bot never writes — so `--apply` would have run against a LIVE store; bare `HEARTBEAT_FRESH_S=120` | **MUST-FIX** | **CONFIRMED + FIXED** | `bot_liveness_evidence()` reads the bot's REAL signals in the store's data_dir — `heartbeat.txt` (quote_app's `Heartbeat`), `supervisor_heartbeat.txt` (supervisor), `loop_progress.json` (risk/progress) — with the bot's own `HeartbeatReader` / `ProgressReader` parsing against the real clock; fresh (age ≤ window; for progress the bound is max(window, every loop's own `stall_after_s`)) OR present-but-unparseable ⇒ `SystemExit` **before any backup or store connection**. Window = `supervisor.heartbeat_timeout_s` from the launch YAML (`config/*.local.yaml` first, values only; live = 60 s), else the `SupervisorConfig` default (15 s) — `HEARTBEAT_FRESH_S` deleted. Verified (`ls D:/kalshi-combos-TWO-data/`) that the live data_dir holds none of the three real files today (bot down since 8/27; the relaunch purge deletes them) and never held any of the three dead names — the old check could never have refused anything. Test seeds each file next to a tmp store and asserts `SystemExit` with the row still `booked` and no backup dir; a corrupt heartbeat also refuses; stale (600 s old) proceeds. |
| 2 | Backup = `shutil.copy2` of the whole store (213 GB live) for a 28-row repair | should-fix | **FIXED** | Row-level backup: `<stamp>-phantom_rows_backup.json` (every column of every affected fills / position_ledger / ev_ledger row, pre-state) + `<stamp>-restore.sql` (exact reversing UPDATEs), written and flushed before any write. Parity test: apply, then run restore.sql ⇒ the store equals a pristine seed row-for-row. Report + tool now state `--apply` requires `--migrate` today (`apply_requires_migrate: true` in the dry-run JSON); `--migrate` also stamps the `store_meta` watermark exactly like `Store.open` (parity test). |
| 3 | Verification state lived only in `_executed_states`; a crash inside the window left rows `booked` forever | should-fix | **FIXED (mechanism)** | `store_meta` watermark = MAX(fills.id) + stamp at migration, written once. On the first recovery sweep the lifecycle loads `booked_unverified_fills(after_id=watermark)` ONCE (wall-bounded read, retried) and verifies each `OrphanClaim` on the SAME cadence/budget/rounds/verdicts as an in-process claim, inside the same per-tick poll budget. Void = `Store.void_phantom_fill` (identical ledger writes); the book was rehydrated exchange-first so a phantom is not in it; the fee is reversed in-process only when the boot seed counted it (same ET day). Legacy rows (≤ watermark, incl. the three 2026-07-18 truncated-id rows an exact lookup would wrongly void) are never re-armed — the repair tool owns them. Tests: two-process replay (real row verified on read 1, phantom voided after the budget, no GET quote, terminal, third boot re-arms 0); legacy store re-arms 0; fee-day rule. |
| 4 | `executed_status` kept the STRUCTURAL fallback when the payload omits `creator_order_id` — an exact-total same-ticker fill of another in-flight quote was adoptable; the brief required a POSITIVE match | should-fix | **FIXED** | Unkeyed executed status ⇒ `fill_recovery_unmatched` (error + metric), structural adoption REFUSED, nothing booked, nothing polled, the confirm-booked position KEPT (fail-safe — undercount is the dangerous direction), terminal; the next-restart reconcile + fills-ledger sweep own it. Measured 0/75 unkeyed on 8/26, so this path is rare by construction. Test: tape holds a structurally perfect print; no fills read, no row, position kept. |
| 5 | `_record_executed_fill`: a raise in `record_realized_pnl`/`_track_markout` after the INSERT made the caller see `inserted=False`; row stayed `booked`, never verified | should-fix | **FIXED** | The post-insert tail runs in its own try/except: `fill_ledger_post_insert_failed` (loud, counted), the insert result stands, verification arms. Test: `_track_markout` raises ⇒ `fill_verification_started`, no `fill_ledger_write_failed`, verified next tick. |
| 6 | Docstring "~4.5 min"; cadence is +0/+90/+180 s ⇒ ~3 min | should-fix | **FIXED (+ correction)** | Docstring and report now state void ≈ 2·delay after booking: ~3 min at the 90 s default, **~30 s at the live 15 s setting** — the first draft of this report quoted only the default; the live YAML has had `fill_cancel_verify_delay_s: 15.0` since 2026-07-25. |
| 7 | Evidence gap: 0.486 s is `created_time − executed_ts` (two exchange stamps), not the REST visibility lag; a wrong void is an undercount | should-fix | **FIXED (pre-registered alarm)** | `fill_verified_late` WARNING + `fill_verify.verified_late` metric whenever a REAL fill verifies on attempt > 1 (in-process and re-armed paths). Stated in OPEN/NEXT STEPS as the post-relight check that revisits the budget. Test: tape empty on read 1, present on read 2 ⇒ verified + alarm(attempts=2). |
| 8 | `void_phantom_fill` zeroes ev_ledger to 0/0 — still n+1 in row-based grading | should-fix | **FIXED** | `ev_summary` joins out rows whose fills.status = `phantom` (the zeroing stays as belt). Test: n 2→1, edge 1830→500. |
| 9 | `ledger_quantity_mismatch` would fire every 5 min on 434 legacy `ledger_only` rows and bury a NEW phantom | should-fix | **FIXED (scoped, derived)** | `open_ledger_quantity_by_ticker(post_fix_since=<migration stamp>)` reports `n_post_fix` per ticker; a mismatch whose ticker has NO open row opened at/after the stamp is LEGACY — counted as `legacy_n` / `legacy_by_kind` / `legacy_tickers` (bounded) in the same log line and a `ledger_quantity.legacy` metric, never in the alarmed list or `ledger_quantity.mismatch`. A ticker with any post-fix row is alarmed in full. No number: the boundary is the store's own migration stamp. Test: legacy-only ticker counted; mixed ticker alarmed with both rows summed; all-legacy ⇒ clean event with `legacy_n`. |
| 10 | `has_fill_for_order_id` still counts phantom rows while `fill_order_ids` excludes them — undocumented asymmetry | should-fix | **DOCUMENTED** | Comment next to `fill_order_ids`: the voided row keeps its order_id claim (one exchange order = one row; a late print is `already_in_ledger` for adoption) while the sweep keeps the miss loud; no auto un-void; un-void by hand. |

Nothing new is hand-set: the liveness window is the supervisor's anchor, the legacy/post-fix boundary is the store's own migration stamp, the re-arm rides the existing verify cadence and poll budget.

**Tests (fix pass):** `tests/test_phantom_execution_verification.py` 18 → 32 (+14: 10 store/lifecycle + 4 tool; the parity test extended for the row-level backup + restore.sql). Touched-module set (`test_phantom_execution_verification`, `test_fill_cancel_verification`, `test_fill_recovery`, `test_persistence`, `test_ledger_durable_identity`, `test_book_completeness`): **133 passed** (119 before). No existing test weakened or skipped; the only changed assertions are in this branch's own parity test (whole-file backup → row-level backup, cited inline). `ruff check` clean on every changed file; `mypy --strict` clean on `persistence.py`, `lifecycle.py`, `quote_app.py` and now the tool (one pre-existing `str`/`Any | None` reassignment in `classify` fixed). Full suite: **3,810 passed / 0 failed / 3 deselected (integration) in 4:48** on the final fix-pass code (`PYTHONPATH=src .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`, one run, alone on the machine) = the first pass's 3,796 + 14 new. `tools.vitals.gate` NOT run here (the orchestrator runs it after merge; rfq/lifecycle.py and ops/quote_app.py changed again in this pass, so it must).

**Parity (fix pass):** repair tool `--migrate` vs `Store.open` on the same legacy store: identical columns, index and `store_meta` watermark (the Store's subsequent open adds nothing and reads the tool's stamp); `restore.sql` vs a pristine seed: identical rows across the three ledgers; `classify()` on the live store + all-time JSON: identical to the previous pass (28/3/4,154/0/0; 70; 7). Re-armed void vs in-process void: same `Store.void_phantom_fill` call, same touched counts, same fee reversal figure (test).

**Blast radius (fix pass):** store schema (+`store_meta`, idempotent), one reconcile alarm's scoping, the recovery sweep (one added store read per PROCESS, wall-bounded, cached; orphan polls share the existing 3-per-tick budget), `_record_executed_fill`'s post-insert tail (try/except only), the executed-status arming branch (unkeyed refusal), one ops tool. Pricing, caps, sizing, the quote/confirm hot path and the settlement seam are untouched. `handle_rfq` → `create_quote` is not on any changed path; `on_quote_executed`'s hot section is unchanged.

## Gitignored YAML lines the operator must add

None. No new config field; the verification rides the existing `risk.fill_cancel_verify_attempts` / `risk.fill_cancel_verify_delay_s` / `risk.fill_record_recovery_after_s`.

## OPEN

- Run `tools/ops/repair_phantom_fills.py --apply --migrate` (bot DOWN — the tool now refuses on the bot's real liveness files; `--migrate` is REQUIRED today because the live store has no verification columns) after reading the dry-run — operator decision; the 28 `open` phantom position_ledger rows otherwise stay in the 9/1 stale-row count and, being legacy (below the watermark), are never touched by the restart re-arm.
- 70 tape-only fills (writer-path misses across outages) and 7 partial-count matched rows — the opposite direction; a separate repair (adopt from tape / resize) is owed.
- A voided fill whose order later appears on the tape re-alarms via `fills_ledger_missing_exchange_fill` (phantom rows leave `fill_order_ids`) while `has_fill_for_order_id` still counts it (documented asymmetry: one exchange order = one row; no auto un-void) — alarm-only; un-void is by hand (the row and its raw_json are intact).
- The REST visibility lag of `/portfolio/fills` is UNMEASURED (the 0.486 s figure is two exchange stamps). Pre-registered check after relight: any `fill_verified_late` (a real fill verified on attempt > 1) or `fill_verify.verified_late > 0` is the signal to revisit the void budget BEFORE it voids a real fill.
- The EXCHANGE-side cause (why Kalshi emits `quote_executed` + `status: executed` for orders that never fill — 28 times, all NO side, both series) is unexplained; worth a support ticket with the six order ids above.
- `position_reconcile_quantity_divergence` (in-memory book vs exchange) was RIGHT on 8/26 and ignored — it sits in the log-noise filter (`ctx.sh`). Consider promoting a persistent per-ticker divergence to the vitals gate.
- Legacy `ledger_only` rows (434 today) are now COUNTED, not alarmed, by `ledger_quantity_mismatch` — the 9/1 stale-row item still owns closing them.

## NEXT STEPS

1. Orchestrator: merge, run `tools.vitals.gate` (fast + pre-ship) — rfq/lifecycle.py and ops/quote_app.py changed (both passes).
2. Operator: decide `--apply --migrate` for the 28 rows (row-level backup + restore.sql automatic; the tool refuses while `heartbeat.txt` / `supervisor_heartbeat.txt` / `loop_progress.json` are fresh within `supervisor.heartbeat_timeout_s` = 60 s live).
3. At relight, read in this order: `fills_verification_columns_added` + the `store_meta` watermark (first open migrates the live store: MAX(fills.id) = 4,185+ at that moment); `fill_verify_rearmed` (n = 0 on the first relight — everything is legacy); then per fill `fill_verification_started` → `fill_verified_on_tape` on the next tick; **`fill_verified_late` is the pre-registered alarm to act on** (revisit the budget); `fill_phantom_execution_voided` is the alarm to read; `ledger_quantity_mismatch` now shows `legacy_n≈434` in the clean/alarm line without alarming them.
4. After the first crash/restart with post-fix rows: expect `fill_verify_rearmed n>0` and `rearmed=True` verdicts within ~30 s.
5. Owed separately: tape-only fill adoption (70), partial resize (7), exchange-side ticket, the 9/1 stale-row closure.
