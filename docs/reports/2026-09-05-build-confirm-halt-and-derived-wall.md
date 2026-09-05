# 2026-09-05 — BUILD: confirm-halt classifier + derived maintenance stall wall

Branch `build/confirm-halt-and-derived-wall` (worktree `C:/Users/aahys/kct-crash-fix`), built while the
bot ran on `main`; store and data dir read-only throughout (sqlite `mode=ro`, logs by grep/tail only, no
process started or stopped). Two builders: the first left A and B uncommitted; this pass verified every
claim against the tape, finished A(2), re-measured, ran the gates and committed.

Operator ask: *"stuff is settling but we're not filling again … look into the bot crashing and what can be
done to fix it."*

## WRONG / FIXED / OPEN

| # | Claim going in | What the tape says | Status |
|---|---|---|---|
| W1 | "3 confirm halts today (09:04, 09:41, 12:11 ET)" | **EIGHT** `kill_switch_halt reason=halt_confirm_timeouts` on 2026-09-05: 04:50:43Z (boot 2103), 13:04:20Z (0052), 13:41:14Z (0905), 15:40:50Z (0942), 16:11:31Z (1141), 18:45:25Z (1407), 18:59:18Z (1446), 20:00:56Z (1500 = the current `halt_receipt.json`). **25 of 25** `confirm_failed` lines carry `KalshiApiError('HTTP 400 expired: expired')` (24 across the eight halting boots + 1 on the 1213 boot). | FIXED — classifier + consecutive counter (A) |
| W2 | "3 **consecutive** confirm failures" (the halt text and `halt_receipt.json` `verdict_detail`) | `_confirm_failures` was cumulative per boot: nothing reset it on a successful confirm (`lifecycle.py` init at :1856, `+= 1` at :6516, `>= 3` at :6524 on `main`). Boot 0052: 5 ok, **fail, 104 ok, fail, 1 ok, fail → HALT**. Boot 0942: fail, **ok**, fail, fail → HALT. Boot 1141: fail, fail, **ok**, fail → HALT. Boot 1407: 5 accepts, 3 fails → HALT. Only 0905 (3 accepts, 3 expired) and 1446 (3 accepts, 3 expired) were literally consecutive. | FIXED — a successful confirm resets the counter (A) |
| W3 | "an expired confirm is a failure of ours" | Every failure is the exchange saying the TAKER's accept window lapsed before our confirm landed: in-handler time 0.5–0.8 s of the 3.0 s window on every one (boot 1141 quote `bf44eed9`: `quote_accepted 15:54:36.139` → `confirm_failed .729`), and the reservation reconcile then **released** the headroom (`risk_reservation_released` 3/3 on 1407, 2/3 on 1446 — the 3rd was the halting one, 3/3 on 1500; 15/15 on the morning boots) — no position on the exchange. A lost auction, not an unknown-committed position. | FIXED — `confirm_expired_by_exchange`, never counted (A) |
| W4a | "supervisor stall-kills at 00:51 and 12:12 today = maintenance passes crossing 60 s on the 213 GB store" | Both of TODAY's receipts (`KILL_20260905_005252.txt`, `KILL_20260905_121339.txt`: `supervisor kill: loop stalled: maintenance age=61.1s > 60.5s`) are stamped **60 s after a confirm halt's shutdown wedged**: 2103 boot `kill_switch_halt 04:50:43Z → shutdown_step ws_stop 04:50:50Z → joint_pool_stopped 04:50:50Z → [60 s of silence] → supervisor_heartbeat_stale 04:51:50Z → supervisor_loop_wedged 04:51:51Z`; 1141 boot identically `16:11:31Z → 16:11:38Z → 16:12:38Z → 16:12:39Z`. The dedicated liveness heartbeat went stale too, so the whole event loop was blocked during teardown — the wall stamped a corpse. The live loop's measured max inter-mark gap on the 12:13 boot: **3.54 s** (table below). | RETRACTED for today's two (they are O1's shutdown wedge) |
| W4b | The first builder's retraction: "all 27 stall receipts since 8/17 followed a `kill_switch_halt`; no healthy quoting bot has been killed by the wall" | Tail-checked every receipt's preceding log this pass (28 receipts since 8/17, table in B): in **15** the order is `supervisor_loop_wedged` → 1–4 s → `kill_switch_halt reason=halt_kill_file` — the bot halted on the SUPERVISOR's own KILL file. The process answered the file within seconds, so the event loop was alive and the MAINTENANCE LOOP ALONE had not marked progress for 61 s: a stuck await inside the tick, exactly the unbounded-store class F3/F4 bound. 8/17 ×3, 8/20 ×5, 8/26 ×6, 8/27 ×1. The other 13 are corpse stamps after another halt (kill-file/operator ×6, data_stale ×2, hard_trip, reconciliation_mismatch, confirm_timeouts ×3 incl. today's two). | CORRECTED — the task's premise (3) holds for 15/28; B (bounded sub-steps + progress between them) is the mechanism repair for exactly those |
| F1 | Confirm-halt classifier | `classify_confirm_failure`: exactly `HTTP 400` + code `expired` ⇒ `expired_by_exchange` (metric `confirm.expired_by_exchange`, histogram `confirm.expired_by_exchange.accept_to_confirm_ms`, WARNING `confirm_expired_by_exchange` with `accept_to_confirm_ms` / `dispatch_delay_ms` / `confirm_rtt_ms` / `exchange_window_ms`, never counted). Timeouts, connection errors, HTTP 5xx, any other refusal (401/403/429/insufficient_balance — strict side, no such population ever taped) ⇒ ours, counted **consecutively**; success resets; expired neither counts nor resets. `confirm_failed` (ERROR) carries the same timing split plus `kind` and `consecutive`. Unknown-committed posture unchanged for every class (reservation held until the exchange reconcile proves it). Halt text: "N consecutive confirm failures of ours (timeouts / connection errors / HTTP 5xx / refusals; exchange-expired accepts are classified apart and never counted)". | FIXED — `rfq/lifecycle.py`, 26 tests |
| F2 | Derived maintenance stall wall | `risk/stall_wall.py` + `ProgressLedger.measure_gaps`: the maintenance loop records every COMPLETED inter-mark gap (a hang never completes one, so the record is healthy by construction); `wall = max(floor, MARGIN × Q_Φ(5)(gaps))` with MARGIN = the hang watchdog's `_MARGIN` (2.0, pinned by a parity test that reads the tool) and z = the ladder's KILL rung; floor = today's register-time bound (`supervisor.heartbeat_timeout_s` + `MAINTENANCE_TICK_INTERVAL_S` = 60.5 s) — it can only LOOSEN by measurement. Per-boot histograms persist to `data/maintenance_gap_tape.json`, pruned to the operator's existing `live_*.log` retention, pooled at boot and every 120 ticks (the metadata-flush cadence). Logged as `stall_wall_derivation` at boot and on every refresh. The supervisor reads the bound from `loop_progress.json` — **no supervisor change**. | FIXED — 29 tests |
| F3 | Un-bounded store awaits on the maintenance path | The deep dive's 12 direct `await self._store.*` sites (`mark_fill_verified` ×3, `has_fill`, `fill_ref_for_order_id`, `record_fill`, `void_phantom_fill` ×2, `has_fill_for_order_id`, `open_ledger_identities`, `fill_order_ids`, `fill_null_order_id_keys`) run through `_bounded_store(op, coro)` under `sub_step_bound = wall / MARGIN` (30.25 s at the floor; `STORE_OP_TIMEOUT_S` = 5 s before the first derivation). Expiry ⇒ `store.await_timeout.<op>` + WARNING, then the caller's EXISTING failure branch (audited this pass: `_record_executed_fill` is inside `fill_ledger.write_failed`'s try/except; the three stamps and two voids are try/except evidence-only; `_adopt_exchange_fill`'s single caller catches `TimeoutError` as a failed round; the two ledger sweeps run off-loop under their own `wait_for`). The remaining 6 direct awaits are decision/structural-fit record writes on the quote path (queued writes, out of scope). A regression guard asserts none of the 12 ops is awaited directly again. | FIXED |
| F4 | Progress only marked at the top of a tick | `maintenance_tick` runs its body under `_TickLaps`: each sub-step (`prelude`, `unrecorded_fills`, `limits`, `withdraw_pending`, `reprice`) is timed into `maintenance.step_ms.<step>` and MARKS PROGRESS when it completes; the whole pass lands in `maintenance.tick_ms`; a pass over the measured bound logs `maintenance_tick_slow` with the step breakdown. This is the pass-duration tape that never existed (the 8/18 "29–31 s passes" that set the 60 s hand number were never recorded anywhere). | FIXED |
| **O0** | **The stack is DOWN since 16:02 ET** | After the 20:00:56Z confirm halt the 1500 process exited; `hang_watchdog.log 16:02:01 STALL ESCALATION (process_exited): no bot process on 3 consecutive probes` → stragglers stopped → `relighting via the operator start path (-Auto)` → `live_20260905_1602.log` logs `quote_app_starting … adaptive_caps_slate_count_failed (HTTP 429) … joint_pool_started` at 20:02:08Z and **nothing after** (3,888 bytes; `worker_pids.txt` 0 bytes at 16:02:08; no `heartbeat.txt`, no `loop_progress.json`; no quote_app / START_BOT / hang_watchdog process at 16:11 ET). A boot death of the `build/watchdog-network-boot-death` class, not this build's scope; this build did not start or stop anything. | OPEN — operator / orchestrator relight decision (this branch is what to relight WITH) |
| O1 | The shutdown wedge (the real producer of the stall receipts) | On a halt the teardown reaches `ws_stop` → `joint_pool_stopped` → the event loop blocks for the 60 s shutdown budget (the dedicated heartbeat goes stale — `supervisor_heartbeat_stale age=60.0s`), and the supervisor writes KILL + `needs_reconcile` on the corpse. Costs 60 s per halt, a false receipt and an unneeded reconcile. Suspect: the stage AFTER `joint_pool.shutdown` returns. | OPEN — separate build; the receipts now have a documented signature |
| O2 | Supervisor stamps a stall on a declared shutdown | The publisher could stamp `shutdown_since` into `loop_progress.json` at teardown start so the receipt says "shutdown exceeded its budget after halt X". Forensic clarity only. | OPEN — small, pairs with O1 |
| O3 | Expired-accept rate since 09:05 ET: 10/15 accepts on the morning boots, 9/12 on the afternoon boots vs 3/116 overnight (2.6%) | In-handler time is 0.5–0.8 s on every one, so the loss is UPSTREAM of the handler. `communications_channel_lost: Subscription buffer overflow` per boot: 1213 **123**, 1407 40, 1446 12, 1500 64 — the delivery side is dropping our subscription because the loop does not drain the socket. The new WARNING splits `dispatch_delay_ms` (WS stamp → handler) from `confirm_rtt_ms` so the next tape says which. | OPEN — delivery / event-loop load, not this build |
| O4 | Store saturation | `store_writer_stats queue_depth` pinned at 200k, `dropped_writes_total` 1.86M on the 00:52 boot; `retained_floor_sweep_timeout` ×2 per boot. Item 7 (store rotation, `build/store-rotation-tool`) untouched here by instruction. | OPEN — item 7 |

## A. Receipt-level evidence of the confirm halts

All from `D:/kalshi-combos-TWO-data/live_2026090{4_2103,5_*}.log` (grep only), `KILL_20260905_005252.txt`,
`KILL_20260905_121339.txt` and `halt_receipt.json` (`written_at 2026-09-05T20:00:56.843Z, reason
halt_confirm_timeouts, verdict_detail "3 consecutive confirm failures"`).

| Boot (ET) | Accepts | `confirm_failed` (all `HTTP 400 expired`) | Successes between | Halt (Z) | Receipt |
|---|---|---|---|---|---|
| 9/4 21:03 | 28 | 3rd at 04:50:43Z | — | 04:50:43 | → shutdown wedge → `KILL_20260905_005252` "maintenance age=61.1s > 60.5s" |
| 00:52 | 116 | 05:16:40Z, 12:11:20Z, 13:04:20Z | 5 before; **104** between 1st–2nd; **1** between 2nd–3rd | 13:04:20 | — |
| 09:05 | 3 | 13:14:10Z, 13:24:13Z, 13:41:14Z | 0 (genuinely consecutive) | 13:41:14 | — |
| 09:42 | 6 | 15:10:41Z, 15:22:50Z, 15:40:50Z | ok 13:43, ok 14:09, fail, **ok 15:15**, fail, fail | 15:40:50 | — |
| 11:41 | 4 | 15:54:36Z, 15:59:56Z, 16:11:31Z | fail, fail, **ok 16:02**, fail | 16:11:31 | → shutdown wedge → `KILL_20260905_121339` |
| 12:13 | 2 | 16:39:47Z | ok 16:24 | (1 of 3) | boot ended 14:06 ET |
| 14:07 | 5 | 18:32:03Z, 18:40:00Z, 18:45:25Z | 2 ok | 18:45:25 | — |
| 14:46 | 3 | 18:56:13Z, 18:58:38Z, 18:59:18Z | 0 (genuinely consecutive) | 18:59:18 | — |
| 15:00 | 4 | 19:32:28Z, 19:34:29Z, 20:00:56Z | 1 ok | 20:00:56 | `halt_receipt.json`; process exited; 16:02 relight died at boot (O0) |

Per-failure trail (boot 1141, quote `bf44eed9`): `quote_accepted 15:54:36.139` → `risk_reservation_granted .166` →
`candidate_gate_confirm .674` → `confirm_failed .729` (in-handler **0.59 s**) → `risk_reservation_unconfirmed .729` →
`risk_reservation_released 15:54:40.596` (reconcile: no position on the exchange). Same shape on every failure paired.

The 7/31 report deliberately left the rule unchanged ("12/12 are expired, no population to split; any split weakens
the trigger; consecutive-reset is a loosening that needs an operator ruling"). Today's ruling is the operator's ask
plus the evidence that the counter was never consecutive and that an expired accept never leaves an unknown-committed
position; the trigger for OUR failures is unchanged (3 consecutive, halt) and is re-proven for every class
(`test_three_genuinely_consecutive_own_failures_still_halt[timeout|connection|http_5xx|http_4xx_refusal|other]`).

## B. The wall — what was measured, what was derived

### B.0 Every stall receipt since 8/17, classified from the tail of its boot's log

Method: for each `KILL_*.txt` containing "maintenance", the log whose start precedes the receipt; `tail -c 600000`
grepped for `kill_switch_halt` / `joint_pool_stopped` / `shutdown_timed_out` / `supervisor_loop_wedged` (no full
scans). "Supervisor-first" = the wedge verdict precedes the bot's `halt_kill_file` (the bot is reacting to the
supervisor's KILL file). "Halt-first" = another halt precedes the wedge by ≥ 30 s (a shutdown that wedged).

| Class | Receipts | What it says about the wall |
|---|---|---|
| **Supervisor-first** (15): 8/17 `000308 062703 234337`; 8/20 `080542 110544 140040 172221 173244`; 8/26 `122329 142031 152109 180802 181736 214103`; 8/27 `014437` | `supervisor_loop_wedged` → 1.0–4.1 s → `kill_switch_halt halt_kill_file` → `joint_pool_stopped` (8 of 15 then `shutdown_timed_out last_step=book_ws_stop`) | The process was alive (it read the KILL file within seconds); only the maintenance loop had gone 61 s without a mark. A sub-step held in an await — the class F3 bounds and F4 makes visible. Whether those passes would have COMPLETED at 70 s or 700 s was never recorded: a killed pass leaves no completed gap, so the tape can only ever loosen the wall from passes that finished under it. |
| **Halt-first / corpse stamp** (13): 8/17 `233905` (hard_trip); 8/18 `042105`, 8/20 `081629`, 8/26 `083507 084416 085504 101619` (kill-file first = operator/watchdog stop); 8/20 `034724`, 8/27 `020759` (data_stale); 8/26 `220431` (reconciliation_mismatch); 9/4 `201018`, 9/5 `005252 121339` (confirm_timeouts) | `kill_switch_halt <reason>` → `joint_pool_stopped` → 60 s → `supervisor_loop_wedged` (+ `shutdown_timed_out`) | The wall is not the actor; the shutdown wedge (O1) is. |

**There was no pass-duration tape.** `loop_progress.json` is one overwritten snapshot; the supervisor logs only
when it kills; the maintenance tick emitted no timing. So "today's tape" for this build was made read-only, live:
`loop_progress.json` sampled at 4 Hz for 30 min (17:29:39–17:59:17Z) on the 12:13 boot (37 positions, ~240
sends/min, store queue pinned at 200k, 123 WS buffer-overflow drops on that boot). The sampled quantity is the
maintenance loop's last-mark AGE, so the max completed gap is ≥ the max age seen and < it + 0.25 s.

| Inter-mark gap of the maintenance loop (`scratchpad/gap_samples.csv`, recomputed this pass) | Samples (n = 7,059) |
|---|---|
| < 0.5 s | 6,068 |
| 0.5 – 1 s | 863 |
| 1 – 2 s | 100 |
| 2 – 3 s | 23 |
| 3 – 5 s | 5 — max **3.544 s** at 17:55:00Z |
| ≥ 5 s | 0 |

Derivation on that distribution (`tests/test_stall_wall.py::test_todays_measured_distribution_keeps_the_floor`
encodes the same shape, n = 7,059): Q_Φ(5) = the sample max = 3.544 s ⇒ MARGIN × Q = 7.1 s < floor ⇒
**wall = 60.5 s (source `floor`), sub-step bound = 30.25 s**. The 60 s hand number sits **17×** above the longest
gap this loop completed in half an hour of live quoting on the 213 GB store. The measurement does not loosen
today's wall and cannot tighten it. A loop that ever COMPLETES a 45 s gap gets wall = 90 s and a 45 s store bound
(`test_a_measured_slow_pass_loosens_the_wall_by_the_margin_only`) — never a hand number.

Rule provenance: `tools/ops/hang_watchdog.py::derive_threshold` = `max(_MARGIN × max_healthy_gap, floor)` (242 s
live). Same rule, same margin (parity test reads `_MARGIN` from the tool), the quantile expressed at the policy
KILL z (Φ(5) — the sample max below ~3.5 M gaps, the 5σ edge beyond). Floor = the anchor already in the config.
Retention = the oldest `live_*.log` on disk.

The property that must never be lost is pinned with the REAL `_maintenance_loop` + REAL supervisor + REAL ledger
files: a tick that never returns is killed at the derived wall with KILL written and the loop named
(`test_a_truly_hung_maintenance_loop_is_still_killed_at_the_derived_wall`); its control — a store that hangs under
the derived bound — keeps ticking and is never killed (`test_a_healthy_loop_with_a_bounded_store_is_not_killed`);
a store timeout inside cancel-verification is a failed round, never an ok read that could void a real fill
(`test_a_store_timeout_during_adoption_is_a_failed_round_never_an_ok_read`).

## Blast radius

| Surface | Change | Pricing / quoting |
|---|---|---|
| `rfq/lifecycle.py` confirm path | failure classification + counter reset + timing split on the two log lines; the confirm itself, the reservation hold, the record/audit are byte-identical | untouched (the confirm decision is made before this branch) |
| `rfq/lifecycle.py` maintenance path | `_bounded_store` on 12 store awaits; `_TickLaps` timing + progress marks; `sub_step_bound_s` ctor param | untouched (no pricing input read or written) |
| `risk/progress.py` | `measure_gaps` (maintenance only — the quote loop's `mark` stays one integer store), `gap_histogram`, `set_stall_after`, `stall_after_s` | untouched |
| `risk/stall_wall.py` (new) | histogram, tape, derivation | none |
| `ops/quote_app.py` | registers the maintenance loop measured; derives at boot + every 120 ticks; passes the bound callable; new file `data/maintenance_gap_tape.json` | untouched |
| `ops/supervisor.py` | **no change** — reads `stall_after_s` from the ledger as before | — |

Throughput: nothing on the RFQ→quote path changed; `mark()` for the quote loop is unchanged (no histogram). The
fill-record path gains one `asyncio.wait_for` wrapper per store op. Rule 8: no pricing change, no parity check owed.

## Gates

| Gate | Result |
|---|---|
| ruff check (4 src modules + 3 test files) | clean (`ruff format` is not a repo gate: 256 of 330 files on `main` would reformat) |
| mypy strict (`stall_wall.py`, `progress.py`, `quote_app.py`, `lifecycle.py`) | `Success: no issues found in 4 source files` |
| new tests (`test_confirm_halt_classifier.py` 26, `test_stall_wall.py` 29) + touched (`test_withdraw_resolution`, `test_liveness_progress`, `test_supervisor`) | 133 passed before this pass's edits; 26 + 53 re-run green after |
| full suite (low priority, worktree, `pytest tests -q -x`) | **4,100 passed, 3 deselected, 0 failed** in 322 s (baseline 4,045 + 55 new) |
| vitals fast (`VITALS_DATA_DIR` = a fresh read-only 2-table snapshot taken 20:10:48Z via `tools/vitals/snapshot.py`) | **8/8 GREEN** (GATE PASS, 90.7 s) |

No test was weakened: `test_repeated_confirm_failures_halt` (RuntimeError ×3 ⇒ halt) still passes as written;
`test_confirm_failure_is_counted_not_raised` still counts a RuntimeError in `confirm.failed`;
`test_the_reprice_sweep_is_no_longer_a_write_driver` now parses the tick wrapper AND the body (strictly more code).

## NEXT STEPS

1. **Relight decision (operator / orchestrator — O0):** the stack is down; the 16:02 `-Auto` relight died at
   `joint_pool_started`. Whatever relights should carry this branch: the live code halts on the 3rd expired accept
   and every boot today reached it within 4–40 accepts. After relight verify: `stall_wall_derivation` at boot
   (`wall_s 60.5, source floor`), `confirm_expired_by_exchange` WARNINGs instead of halts (watch
   `dispatch_delay_ms` — that is the loop-stall number), `maintenance.tick_ms` in the first `quote_app_stopped`
   dump, `data/maintenance_gap_tape.json` growing one row per boot.
2. **O1 — the shutdown wedge after `joint_pool_stopped`** (owner: next ops build): instrument the stages after
   `ws_stop` with their own `shutdown_step` lines + a thread dump on the 60 s watchdog, then bound the blocking
   call. This is what makes every halt cost a minute and stamps the false receipt.
3. **O3 — expired-accept rate / buffer overflow** (owner: delivery/WS + the `ws-marketdata-shed` branch): read
   `dispatch_delay_ms` on the first WARNINGs and the overflow count per boot; the classifier makes the rate
   observable without halting.
4. **O4 — store rotation** (`build/store-rotation-tool`): the 200k queue and 1.86M dropped writes are the load
   that starves the loop.
5. Decisions owed by the operator: none for A (the ruling is the ask); B ships at the floor (no behavior change
   today) and only ever loosens by measurement.
