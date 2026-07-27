# 2026-07-26 — The REAL 20:12Z stall: an unbounded store read on the maintenance loop; plus read-budget pacing, observed-tier derivation, and B3

**Status:** ROOT CAUSE CONFIRMED FROM THE TAPE, FIXED, TESTED, BENCHMARKED.
Suite **2969 passed / 0 failed / 3 deselected** (132 s). mypy strict clean on
every changed module; ruff clean (3 pre-existing baseline errors unchanged).

**Blast radius:** the maintenance loop's *alarm-only* sweeps, the single-quote
withdrawal path, metadata/settled READ pacing, and rate-limit tier derivation.
**No pricing, no quote construction, no risk cap, no sizing, no correlation.**
Hot-path cost measured unchanged (below). `sim/book_model.py` and
`sim/book_risk.py` untouched. `KILL` left in place — the operator relights.

---

## 1. The prior diagnosis was wrong, and the tape says exactly what was

The 16:12 local (20:12 UTC) supervisor kill was blamed on quarantine
enforcement. Disproved by `data/live_20260726_1606.log`:

| evidence | value |
|---|---|
| `market_quarantine_enforced` | `quotes_pulled: 0`, **5 ms** |
| `cancel_quotes_touching_delete_failed` | **0 occurrences in the whole run** |
| status loop (owns enforcement) | ticked on schedule *through* the wedge — `pricing_stats` at 20:12:25.652, 20:12:40.716, 20:12:55.961 |

The loop that went silent is **MAINTENANCE**, and the arithmetic names the
await to the millisecond.

### The proof chain

```
20:07:24.4326  position_ledger_divergence  <- the run's ONLY completed check
                                              AND the sweep's cadence stamp
      + 300.000 s   (ledger_divergence_sweep_interval_s = 300.0, config.py:2832)
= 20:12:24.4326  the next check comes due
20:12:24.389   last heartbeat beat  (20:12:54.489 verdict "age=30.1s" - 30.1)
20:12:24.456   peak_profile_snapshot   <- step 3 of that same tick, ON the loop
   ...         step 8 = _sweep_ledger_divergence
                 -> await self._store.open_ledger_identities()   NO TIMEOUT
   ---         NOTHING from the maintenance loop is ever logged again
20:12:54.489   supervisor_heartbeat_wedged age=30.1s > 30.0s
20:12:55.991   supervisor_emergency_kill  (27 quotes cancelled, KILL written)
20:13:29.897   quote_app_stopped   metrics: "ledger_divergence.checks": 1
```

**`ledger_divergence.checks == 1` is the smoking gun.** The counter is
incremented *after* the store read returns; the cadence stamp is written
*before* it. So the second sweep **started** (the stamp was exactly 300.000 s
old) and **never finished**, from 20:12:24.4 until process exit at 20:13:29.9 —
**≥ 65 seconds blocked on one await**, of which the first 30.1 s is precisely
the wedge verdict.

### Elimination of the other candidates in that tick

The tick's steps between the last on-loop log line and the silence:

| step | verdict |
|---|---|
| `_refresh_settlement_receivables` | pure in-memory, no I/O |
| `_sweep_unrecorded_fills` | already bounded: ≤3 REST polls/tick × 2.5 s `wait_for`; and all 3 fills of the run were `fill_recorded` |
| `_sweep_fills_ledger_diff` | **never ran**: cadence 900 s, run was 410 s, and `fills_ledger_sweep.ran` is absent from the final counters |
| `_sweep_ledger_divergence` | due to the millisecond; started; never completed |

### Why the store did not answer

`Store` runs **one aiosqlite connection**, i.e. one background thread that
serializes *every* statement from every task. The background tape writer commits
batches of up to 1,000 statements on that same thread and periodically runs
`PRAGMA wal_checkpoint(TRUNCATE)` / `(PASSIVE)`. The run logged **46
`store_writer_checkpoint_failed`**, the last at 20:12:23.126 reporting
`busy (wal_frames=57765, checkpointed=0)` — a ~230 MB WAL being passively folded
on the same thread the divergence SELECT was queued behind.

`PRAGMA busy_timeout=5000` does **not** help here: it bounds SQLite's own *lock*
waits, not the time a statement spends *queued* on the connection thread. Only an
asyncio-level bound can.

### Compounding condition, confirmed and quantified

| measure | value |
|---|---|
| `metadata_fetch_failed`, all HTTP 429 | **5,726** in 332.6 s |
| sustained FAILED reads | **17.2 /s** |
| **peak** failed reads in one second | **183** (20:07:27Z) |
| distinct tickers re-asked | 1,390 |
| `settled_fetch_failed` HTTP 429 | 123 |
| `exchange_status_rate_limited` | 5 |

A metadata GET costs the **default 10 tokens** — verified live, not assumed:
`GET /account/endpoint_costs` returns `default_cost: 10` and only 13 overrides,
none of which is `/markets/{ticker}` or `/events/{ticker}`
(docs/api-notes/openapi-comms.md **line 276-277**). So the peak second attempted
**1,830 read tokens/s against this account's 300 tokens/s** — 6.1× over
sustained, and that counts only the reads that FAILED.

The mechanism was self-sustaining: `_ensure_watched` retries a leg's metadata on
**every RFQ that names it** while the cache stays empty (the deliberate
`peek`-None rule). 244,337 RFQs in 410 s × a 429 that never populates the cache =
a storm that cannot damp itself.

---

## 2. The fixes

### 2.1 An alarm-only diagnostic never runs inline on a safety loop

`maintenance_tick` owns TTL expiry, the **enforced limit/halt check**, the fill
recovery sweep and the reprice sweep. Two pure diagnostics were awaited in front
of all of it. They are now **launched, never awaited** (the existing
`_maybe_resolve_settled_marginals` pattern), single-flight, each under its own
wall bound:

```
_LEDGER_DIVERGENCE_SWEEP_TIMEOUT_S = STORE_OP_TIMEOUT_S                    = 5.0 s
_FILLS_LEDGER_SWEEP_TIMEOUT_S      = 2*STORE_OP_TIMEOUT_S
                                   + _FILLS_SWEEP_MAX_PAGES
                                     * _MAINTENANCE_POLL_TIMEOUT_S         = 17.5 s
```

**Every bound is derived from a primitive that already existed**, and is the
arithmetic of what that sweep actually does:

- `STORE_OP_TIMEOUT_S` **is** the store's own `PRAGMA busy_timeout` (5000 ms),
  now a named constant in `ops/persistence.py` and used by the PRAGMA itself, so
  the two can never drift;
- `_MAINTENANCE_POLL_TIMEOUT_S` (2.5 s) is the **existing** per-REST-poll bound
  the maintenance tick already used in three places as a bare literal — now named
  once and referenced;
- `_FILLS_SWEEP_MAX_PAGES` (3) is the sweep's existing page bound.

Outcomes: timeout ⇒ `{name}.timeout` + a loud `{name}_sweep_timeout` warning,
skipped, retried on the next cadence. Still running when the next launch comes
due ⇒ `{name}.skipped_in_flight`, never stacked. Error ⇒ `{name}.errors` + a
warning. **Nothing can raise into the tick**, and no caller waits.

New `QuoteLifecycle.drain_diagnostic_sweeps()` is awaited on shutdown, so a
sweep holding a cursor no longer dies into a "Connection closed" when the store
closes under it.

### 2.2 B3 — the forgotten-but-still-resting quote

`_delete_quote` called `_drop_quote(quote_id)` **unconditionally after its except
block**. A 429 / 5xx / timeout on a TTL / leg-stale / leg-moved / RFQ-gone /
risk-eviction delete therefore made our mirror **forget a quote that was very
possibly still resting and still fillable** — on a path that ran **1,137 times**
in the incident (438 `delete_ttl_expired` + 1,104 `delete_rfq_gone` recorded
deletions, on a run whose write path was 429ing). A 429 is not a failed delete at
all: the request never reached the book.

Now the same rule the batch path (`_withdraw_batch`) already uses:

| exchange said | verdict | mirror |
|---|---|---|
| ack (204) | provably off the wire | **dropped** |
| 404 `not_found` (`_already_gone`) | provably off the wire | **dropped** |
| 429 / 5xx / timeout / transport | **UNKNOWN** | **kept**, `withdraw_pending_reason` set, `quote.delete_unresolved` counted, retried |

Keeping it is only safe with a retry driver, so one was added: the **reprice
sweep re-asks every `withdraw_pending` quote on every maintenance tick (0.5 s)
for its ORIGINAL reason** — including the event-driven reasons (`DELETE_RFQ_GONE`,
`DELETE_RISK_EVICTED_ON_FILL`) whose trigger never fires twice. A
`withdraw_pending` quote is never repriced (we are removing it, not improving it)
and is skipped by `_pick_eviction_victim` (or one 429ing quote would consume the
whole bounded eviction pass).

### 2.3 Read-budget pacing — the same treatment writes already got

`MetadataCache` now spends from a **READ token bucket** before every network GET
(`ops/write_budget.TokenBudget` — the existing `WriteBudget` primitive under a
name that does not lie about which bucket it guards). Placed in the cache, not at
the call sites, because the fetch is reached from three places (RFQ hot path,
startup leg-arming, warm-cache revalidation) and a bucket some of them respect is
not a bucket.

Two disciplines, deliberately different:

- **HOT PATH (RFQ workers) — REFUSE, never wait.** Out of tokens raises
  `ReadBudgetExhausted`; the leg stays uncached and the next RFQ naming it retries
  once the bucket refills. Identical retry semantics to today, minus the 429 and
  minus the round trip. Waiting here would convert a read shortage into quote
  latency.
- **SLOW PATHS — WAIT, never refuse.** The startup rehydrated-leg arming pass and
  the **settled-marginal resolver** (the tape's second-biggest read source, 123 of
  the 429s) pay the bucket's own `seconds_until` wait, bounded at one full bucket
  refill, then fall back to their existing backoff. The resolver reaches REST
  directly, so it goes through `_PacedMarketSource`, which **waits AND spends** —
  one bucket, or the pacing is fiction.

`ReadBudgetExhausted` subclasses `KalshiApiError` (so every existing "this read
failed, skip and retry" handler already degrades correctly) but **deliberately not
`RateLimitedError`**, and carries `status = 0`: nothing touched the exchange, so
it must never feed the 429-burst breaker (`HALT_RATE_LIMIT_BURST`). Counting our
own back-pressure as the exchange refusing us would halt the bot on its own fix.

`read_budget=None` (paper, backtests, tests) ⇒ byte-for-byte the prior behaviour.

### 2.4 Tier derivation — the load-bearing constant is gone

`exchange/rest.py` hard-coded `WRITE_TOKENS_PER_S = 300` commented *"we are on
Advanced"*, while the only recorded in-repo observation
(`tests/fixtures/ground_truth/scenario_account_facts.jsonl`, DEMO, 2026-07-06)
said `usage_tier: basic` with a 100-token write bucket — i.e. the paced wave was
either fine or a 2.2× breach and **nothing in the process could tell which**.

**Asked, read-only, on LIVE PROD (2026-07-26, `GET /account/limits`):**

```json
{"usage_tier": "advanced",
 "read":  {"bucket_capacity": 600, "refill_rate": 300},
 "write": {"bucket_capacity": 600, "refill_rate": 300},
 "grants": [{"exchange_instance": "event_contract", "level": "advanced",
             "source": "manual"}]}
```

No `expires_ts` ⇒ permanent. This corroborates the documented Advanced row
(`docs/api-notes/openapi-comms.md` **line 269**: `| Advanced | 300 | 300 | 2 s |`
⇒ capacity = 300 × 2 = 600) exactly. `GET /account/limits` as the tier-inspection
endpoint is documented at **line 279**; the tier table is **lines 266-274**.

So: the constant is **deleted**. `observe_api_tier(rest)` runs at boot, both
buckets are built from the observed `BucketLimit`s verbatim, and any failure —
network error, 401, non-object payload, missing/zero fields — falls back to
`LOWEST_TIER_LIMITS` (**Basic: 200 read / 100 write**, openapi-comms.md line 268).
**Fail-safe to the LOWEST tier, never the highest**: guessing high turns an
unreadable response into a self-inflicted 429 storm, which is the incident.

The operator's write knob is then **clamped to the observed bucket on BOTH axes**
(`ApiTierLimits.clamp_write_budget`), which surfaced a real latent bug: the
shipped knob is 200 tokens burst / 20 tok/s sustained. The **rate** fits every
tier; the **200-token burst is a 2× breach of the Basic bucket (100)**. Clamping
only the rate — the obvious implementation — would have let the fail-safe path
429 its own withdrawal wave. Clamping preserves the sustained rate and costs only
burst depth (200/10 s → 100/5 s = still 20 tok/s). On the observed prod tier
nothing clamps, and `write_budget_within_tier` is logged at boot.

---

## 3. Measurements (all executed)

### 3.1 Read pacing — reads/s and tokens/s before vs after

`tests/test_maintenance_stall_and_read_budget.py::test_metadata_reads_stay_inside_the_read_budget_under_a_refresh_wave`
hammers a 1,390-ticker universe (the incident's distinct-ticker count) for a
2.00 s wall window, both arms identical apart from the budget:

```
READ PACING  (tier=advanced, read 300 tok/s cap 600, metadata GET = 10 tok)
  UNPACED : 308176 reads in 2.00s = 154087.8 reads/s = 1540877.6 tok/s | peak-1s 1553920 tok/s
  PACED   :    120 reads in 2.00s =      60.0 reads/s =     600.0 tok/s | peak-1s     900 tok/s
                                        (805,522 refused locally, 0 emitted 429s)
  CEILING : 300 tok/s sustained; bucket guarantee in any 1s = capacity + rate = 900 tok
```

The paced arm sits **exactly on** what the exchange's own bucket admits in any
one second (600 burst + 300 refill) and converges to the 300 tok/s = **30
metadata reads/s** sustained ceiling as the window lengthens; the 600 tok/s
figure over 2.00 s is the initial full bucket amortised. The control **must**
breach for the bench to mean anything, and it does, by 1,700×.

Against the live tape for scale: the incident sustained **17.2 FAILED reads/s
(172 tok/s of pure waste)** and peaked at **183 reads/s = 1,830 tok/s**, i.e.
**6.1× the sustained ceiling** before counting the reads that succeeded.

### 3.2 Hot-path cost — no throughput regression

| measurement | before | after |
|---|---|---|
| `MetadataCache.peek()` (the actual hot path; 400k calls) | **42.2 ns** | **41.1 ns** |
| `TokenBudget.try_spend(10)` (500k calls) | — | **598 ns**, paid ONLY on a metadata cache MISS |

The budget is never consulted on a cache hit — `_ensure_watched` short-circuits
on `peek()` before reaching it — so the RFQ hot path is unchanged. On a MISS the
budget either admits (adding 0.6 µs in front of a millisecond-scale network GET)
or refuses, which **skips** a round trip that would have 429'd. Both outcomes are
≤ the prior cost.

### 3.3 Withdrawal bench still green under the new tier grading

`tools/bench_quarantine_enforcement.py`, now grading against the OBSERVED prod
bucket (and printing what the fail-safe floor would clamp to):

```
write budget 200 tokens / 10.0s -> clamped to 200 / 10.0s = 20 tok/s sustained;
  worst 1s = 200 + 20 = 220 <= observed-tier bucket+refill 900
fail-safe floor (basic) would clamp the same knob to 100 / 5.0s = 20 tok/s,
  worst 1s 120 <= 200
```

| case (40 ms deletes) | BEFORE wall | AFTER wall | AFTER worst hb age | token verdict |
|---|---|---|---|---|
| 36 withdrawals | 1,654.7 ms | **53.0 ms** | 0.08 s | ok |
| 120 withdrawals | 5,541.2 ms | **2,070.5 ms** | 1.00 s | ok |
| 200 withdrawals (= live cap) | 9,226.5 ms | **10,081.8 ms** | 1.00 s | ok |
| 300 withdrawals (stress) | 13,842.8 ms | 15,011.9 ms (budget wall, 100 deferred fail-closed) | 1.00 s | ok |

Heartbeat median 0.51 s, headroom 14.0 s, at every wave size. (The 200/300 rows
are *slower* than BEFORE by design: the token budget deliberately paces a wave
the old code emitted as fast as the socket allowed — which is what 429s.)

---

## 4. Tests (all executed)

New file `tests/test_maintenance_stall_and_read_budget.py` — **23 passed**.

| # | requirement | test |
|---|---|---|
| 1 | a hanging store call does not stall the loop or trip the supervisor | `test_a_hanging_store_read_never_stalls_the_maintenance_loop` — the REAL `QuoteApp._maintenance_loop` + REAL `_liveness_loop` + REAL `SafetySupervisor` reading REAL files, wedge tolerance squeezed to **1.0 s** and run for **3.0 s** (3× the tolerance): ≥4 ticks completed, **every** `check_once()` returned None, no KILL written, nothing cancelled, `ledger_divergence.skipped_in_flight ≥ 1` |
| 1b | …and it was *this* await | `test_the_prefix_shape_would_have_stalled_the_tick` — the CONTROL: awaiting the sweep inline on the same store raises `TimeoutError` at 0.3 s, while the shipped `maintenance_tick` returns in < 0.3 s having launched it |
| 1c | logs, skips, RETRIES | `test_a_timed_out_sweep_is_logged_and_retried` — `ledger_divergence.timeout == 1`, `checks == 0`; store recovers ⇒ next cadence `checks == 1`, timeout still 1 |
| 1d | bounds derived, not invented | `test_sweep_bounds_are_derived_not_invented` — equals the `STORE_OP_TIMEOUT_S` / page arithmetic exactly, and each bound is < its own sweep cadence so timeouts retry instead of stacking |
| 2 | 429 / 5xx / timeout keeps the quote | `test_unresolved_delete_keeps_the_quote_in_the_mirror[429,500,timeout]` — still in `_open` **and** in `exposure.open_quotes` (risk still counts it), `withdraw_pending_reason` set, `quote.deleted.*` NOT incremented |
| 2b | 404 / ack removes it | `test_proved_withdrawal_removes_the_quote[ack,404]` |
| 2c | and it is actually retried | `test_an_unresolved_delete_is_retried_by_the_next_maintenance_tick` — an `on_rfq_deleted` (a trigger that never fires twice) 429s, is retried by the sweep, 429s again, then withdraws for the ORIGINAL reason when the exchange recovers |
| 2d | never re-evicted while pending | `test_a_withdraw_pending_quote_is_never_re_evicted` |
| 3 | reads stay in budget under a refresh wave | `test_metadata_reads_stay_inside_the_read_budget_under_a_refresh_wave` (numbers in §3.1) |
| 3b | a local refusal is not an exchange 429 | `test_a_local_refusal_is_not_an_exchange_rate_limit` — is a `KalshiApiError`, is **not** a `RateLimitedError`, `status == 0` |
| 3c | unpaced callers unchanged | `test_no_budget_wired_is_unchanged_behaviour` |
| 4 | tier is read, not hard-coded | `test_tier_is_read_from_the_account_not_hard_coded` — the live prod payload through the real parser |
| 4b | fails safe to the LOWEST | `test_an_unreadable_tier_fails_safe_to_the_lowest` — 5 unreadable shapes (network error, 401, non-dict, `{}`, zeroed buckets) |
| 4c | floor matches the doc | `test_the_lowest_tier_matches_the_documented_floor` |
| 4d | budget clamped on BOTH axes | `test_the_write_budget_is_clamped_to_the_observed_bucket`, `test_a_rate_over_the_tier_is_clamped_to_the_tier` |
| 5 | existing liveness / quarantine / supervisor tests green | `test_liveness_progress.py` (18) + `test_metadata_change_scope.py` (40) + `test_supervisor.py` + `test_fill_cancel_verification.py` (41) = **142 passed**; full suite **2969 passed / 0 failed** |

`tests/test_liveness_progress.py::test_live_write_budget_clears_a_full_book_without_breaching`
was rewritten to grade the CLAMPED budget against both the observed prod tier and
the fail-safe floor instead of importing the deleted constant.
`tests/test_fill_cancel_verification.py` gained
`await …drain_diagnostic_sweeps()` after each `maintenance_tick()` (56 sites) —
**no assertion changed**; the sweeps are simply off-loop now.

---

## 5. Files changed

| file | change |
|---|---|
| `src/combomaker/exchange/rest.py` | `ReadBudgetExhausted`; `DEFAULT_ENDPOINT_TOKEN_COST = 10` (live-verified); `ApiTierLimits` + `clamp_write_budget`; `LOWEST_TIER_LIMITS`; `observe_api_tier`; **`WRITE_TOKENS_PER_S` deleted** |
| `src/combomaker/ops/persistence.py` | `BUSY_TIMEOUT_MS` / `STORE_OP_TIMEOUT_S` named and used by the PRAGMA itself |
| `src/combomaker/ops/write_budget.py` | `TokenBudget` alias + why the read side shares the primitive |
| `src/combomaker/marketdata/metadata.py` | read-budget gate on `refresh()` and `event()`; `read_budget_refusals` |
| `src/combomaker/ops/quote_app.py` | `observe_api_tier` at boot; shared read `TokenBudget`; `_tier_clamped_write_budget`; `_sleep_for_read_budget` / `_reserve_read_token`; `_PacedMarketSource` for the settled resolver; `ReadBudgetExhausted` handling in `_ensure_watched` + `_arm_rehydrated_legs`; `RFQ_WORKERS` hoisted to module scope; sweep drain on shutdown |
| `src/combomaker/rfq/lifecycle.py` | OFF-LOOP ALARM-ONLY SWEEPS block + `_launch_diagnostic_sweeps` / `_launch_diagnostic_sweep` / `drain_diagnostic_sweeps`; derived sweep bounds; `_MAINTENANCE_POLL_TIMEOUT_S` / `_FILLS_SWEEP_MAX_PAGES` named (3 bare `2.5`s replaced); B3 `_delete_quote`; `withdraw_pending_reason` / `withdraw_attempts`; reprice-sweep retry driver; eviction-victim guard |
| `tools/bench_quarantine_enforcement.py`, `tools/diagnostics/relight_gate_withdrawal_probe.py` | grade against the observed/pinned tier instead of the deleted constant |
| `tests/test_maintenance_stall_and_read_budget.py` | **NEW** — 23 tests |
| `tests/test_liveness_progress.py`, `tests/test_fill_cancel_verification.py` | tier-grading rewrite; sweep drains |

---

## 6. What this does NOT fix (named, not hidden)

1. **The store is still one aiosqlite connection.** Reads and the tape writer
   share one thread; the WAL still grew to 57,765 frames with `checkpointed=0`.
   The stall is now *bounded and loud* rather than fatal, but a saturated store
   still means the divergence invariant silently stops reporting. The real repair
   is a **second, read-only connection** for diagnostics (or a WAL that actually
   checkpoints). Owed, not done here.
2. **`_withdraw_batch`'s unresolved quotes** stay in the mirror without a
   `withdraw_pending_reason` — they are retried by their *caller's* tick
   (the status loop for quarantine, shutdown for cancel-all) rather than by the
   reprice sweep. Consistent outcome, different driver. Worth unifying.
3. **The read bucket is sized at 100 % of the account's read bucket.** Every
   high-frequency reader now spends from it (metadata + settled resolver), but
   the low-frequency polls (balance 10 s, settlement 30 s, transfer 60 s,
   exchange status 15 s, `_count_slate_games`) do not. They are ~0.2 reads/s
   combined, so the headroom is real, but if a new read loop is added it must
   join the bucket rather than sit beside it.

---

## NEXT STEPS

1. **Operator — relight decision.** `KILL` is still present by design; nothing
   here removes it. `tools/ops/start_all.ps1` prompts before removing it.
2. **Watch on the first live run** (in this order of importance):
   - `ledger_divergence.timeout` / `ledger_divergence.skipped_in_flight` — either
     being non-zero means the store is still saturated and item 6.1 is now the
     top of the queue;
   - `metadata_fetch_failed` **HTTP 429 should go to ~zero**, replaced by the
     `metadata.read_budget_deferred` counter; `settled_fetch_failed` 429s too;
   - `api_tier_observed` at boot must read `usage_tier=advanced` with 300/600 on
     both buckets — anything else (especially `api_tier_unreadable`) means the
     bot is running on the Basic fail-safe and paced 3× tighter, which is safe
     but is a signal, not a shrug;
   - `write_budget_within_tier` (good) vs `write_budget_clamped_to_tier` (the
     knob no longer fits the account);
   - `quote.delete_unresolved` — non-zero means B3 saved a live quote from being
     forgotten; each one should clear within a tick or two.
3. **Owed engineering (ranked):** (a) a second read-only store connection for
   diagnostics; (b) unify `_withdraw_batch`'s unresolved quotes onto the same
   `withdraw_pending` retry driver; (c) route the remaining low-frequency read
   loops through the shared read bucket.
4. **Unchanged and still open for the operator:** resume posture, `kill_anchor` %,
   C1/C3/C5.
