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
| Verification budget reused (existing `fill_cancel_verify_attempts=3 × fill_cancel_verify_delay_s=90 s`) | 3 tape reads over ≥180 s ⇒ ≥ 370× the max observed latency; a real fill absent on the FIRST read has a measured base rate of 0/4,053 |
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

Numbers: none new. The verification cadence reuses `fill_cancel_verify_attempts=3` × `fill_cancel_verify_delay_s=90 s` (2026-07-18 config); the stall alarm reuses `fill_record_recovery_after_s=10 s`; the replay budget reuses `_FILL_RECOVERY_MAX_ATTEMPTS`; rounds reuse `_CANCEL_VERIFY_MAX_ROUNDS`. Mode strings only. `PREFIX_MIN_LEN=13` in the repair tool is the measured length of the three 2026-07-18 truncated ids (two UUID dash-groups), used only to classify those rows.

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

- **8/26 replay:** with the fix, each of the 12 phantom rows booked that day would have been VOIDED after 3 clean tape reads (≈ 3 min after booking on the 90 s cadence — attempt 1 immediately, 2 at +90 s, 3 at +180 s); AC104B1B2E5's ledger would have read 43.47 = exchange at the 22:41 ET settlement, and `halt_reconciliation_mismatch` could not have fired. The 75 false "never arrived" recoveries and 38 mislabelled conflicts become 0 REST polls and 0 errors (held-message path).
- **Real fills:** 4,053/4,053 matched rows have their tape print within 0.486 s of `executed_ts` ⇒ verified on the first read (one extra `/portfolio/fills` GET per fill, ~100–400/day, on the maintenance sweep's existing 3-polls-per-tick budget). No real fill would have been voided (0 rows > 10 s; the budget is ≥ 180 s).
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

`--apply` would mark exactly the 28 phantom rows (fills → phantom; their 28 still-`open` position_ledger rows → phantom; ev rows → 0/0) after a timestamped backup under `data/backups/`.

## Gitignored YAML lines the operator must add

None. No new config field; the verification rides the existing `risk.fill_cancel_verify_attempts` / `risk.fill_cancel_verify_delay_s` / `risk.fill_record_recovery_after_s`.

## OPEN

- Run `tools/ops/repair_phantom_fills.py --apply` (bot DOWN) after reading the dry-run — operator decision; the 28 `open` phantom position_ledger rows otherwise stay in the 9/1 stale-row count.
- 70 tape-only fills (writer-path misses across outages) and 7 partial-count matched rows — the opposite direction; a separate repair (adopt from tape / resize) is owed.
- A voided fill whose order later appears on the tape re-alarms via `fills_ledger_missing_exchange_fill` (phantom rows leave `fill_order_ids`) — alarm-only; automatic un-void is not built.
- The EXCHANGE-side cause (why Kalshi emits `quote_executed` + `status: executed` for orders that never fill — 28 times, all NO side, both series) is unexplained; worth a support ticket with the six order ids above.
- `position_reconcile_quantity_divergence` (in-memory book vs exchange) was RIGHT on 8/26 and ignored — it sits in the log-noise filter (`ctx.sh`). Consider promoting a persistent per-ticker divergence to the vitals gate.

## NEXT STEPS

1. Orchestrator: merge, run `tools.vitals.gate` (fast + pre-ship) — rfq/ and ops/quote_app.py changed.
2. Operator: decide `--apply` for the 28 rows (backup automatic; bot must be down).
3. At relight: expect `fill_verification_started` per fill and `fill_verified_on_tape` on the next tick; `fill_phantom_execution_voided` is the alarm to read; `ledger_quantity_mismatch` will list the legacy stale open rows (434 today) until the 9/1 stale-row item lands — bounded to 20 per log line.
4. Owed separately: tape-only fill adoption (70), partial resize (7), exchange-side ticket.
