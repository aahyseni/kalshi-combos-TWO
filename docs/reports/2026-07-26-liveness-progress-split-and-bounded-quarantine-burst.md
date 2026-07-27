# 2026-07-26 — False supervisor kill at 20:12:54Z: liveness/progress split + bounded quarantine burst

**Status:** FIXED, tested, benchmarked. Suite **2941 passed / 0 failed / 3 deselected**.
**Blast radius:** liveness plumbing + the quote-WITHDRAWAL path only. No pricing, no
quote construction, no risk cap, no sizing. Quote-path throughput unchanged (the
RFQ workers gained one dict-store per dequeue).

---

## 1. What happened (verified against `data/live_20260726_1606.log`)

| ts (UTC) | event |
|---|---|
| 20:12:25.647 | **11×** `market_quarantined` in ONE tick — end-of-game lifecycle wave (`inactive→determined`, `active→inactive`, `active→finalized`) across TORBOS + CHCPIT |
| 20:12:25.652 | `market_quarantine_enforced` — all 11 markets, **`quotes_pulled: 0`**, took **5 ms** |
| 20:12:27.762 → 20:12:54.087 | **63×** `delete_quote_failed`, every one `HTTP 404 not_found`, walked ONE AT A TIME over 26.3 s |
| 20:12:54.489 | `supervisor_heartbeat_wedged age=30.1s > 30.0s` |
| 20:12:55.991 | `supervisor_emergency_kill` — 27 resting quotes cancelled, KILL written |

**The bot was not wedged.** Measured over the whole window:

- largest gap between any two log lines: **0.702 s** (`20:12:39.601 → 20:12:40.303`);
- inside the 30 "wedged" seconds: **559 RFQs priced** (`risk_audit`), 559
  `inventory_skew_shadow`, 245 `widen_vs_decline_shadow`, 930 metadata fetches;
- the 15 s status loop kept ticking on schedule (`pricing_stats` at 20:12:25.652,
  20:12:40.716, 20:12:55.961).

### Refinement to the briefed root cause

The brief attributed the stall to `_enforce_market_quarantine` at
`quote_app.py:3480`. **That call is on the `_status_loop`, not the maintenance
loop, and on the tape it completed in 5 ms having pulled 0 quotes.** The loop that
went silent is the **maintenance loop**: its last markers are
`reprice_sweep_budget_deferred` 20:12:23.880, `peak_profile_snapshot` 20:12:24.456
and `settled_fetch_failed` 20:12:24.697 — then nothing until the kill, ~30 s later,
matching `age=30.1s` exactly. The 404 delete storm ran concurrently on the
maintenance/WS withdrawal paths.

**The mechanism the brief identified is nonetheless exactly right, and is the one
that was repaired:** one loop owned BOTH the liveness file and the work, so "this
pass is slow" and "this process is dead" were literally the same signal — and the
withdrawal burst behind an end-of-game wave was sequential and counted a 404 as an
error. Both are fixed. The fix is strictly broader than the briefed attribution:
a dedicated beater removes the coupling for *every* loop, not just the enforcement
path.

---

## 2. The fix

### Part 1 — decouple liveness from work, WITHOUT blinding the supervisor

Two independent signals, either of which kills:

| signal | file | written by | means |
|---|---|---|---|
| **ALIVE** | `data/heartbeat.txt` | `QuoteApp._liveness_loop` — a dedicated task that only sleeps, beats, publishes | the process and its event loop still schedule |
| **WORKING** | `data/loop_progress.json` | the same task, from in-memory marks made BY each loop | each loop's last-progress age vs its own derived bound |

New module `src/combomaker/risk/progress.py` (`ProgressLedger` bot-side,
`ProgressReader` supervisor-side). `SafetySupervisor.wedged_detail()` checks
liveness first, then progress, and the kill reason NAMES the stalled loop.

**No hand-set numbers.** A loop's bound is derived:

```
stall_after_s = supervisor.heartbeat_timeout_s  +  <that loop's own cadence>
```

`heartbeat_timeout_s` is the operator's single existing wedge-tolerance anchor
(30.0 s live). So the maintenance loop's bound is **30.5 s — the same age the old
single signal died at.** Nothing was loosened to make the incident stop hurting.
Beat cadence = `min(supervisor.poll_interval_s, heartbeat_timeout_s / 4)`, so a
misconfigured poll interval can never starve the beat below its own tolerance.

Registered loops: **maintenance** (cadence 0.5 s → bound 30.5 s) and **quote**
(cadence `POOL_DEADLINE_S + RFQ_MAX_QUEUE_DWELL_S` = 3.5 s → bound 33.5 s). The
15 s status loop publishes progress for observability but is deliberately NOT a
kill signal: it already tolerates a 10 s exchange GET plus a 15 s enforcement
budget in one tick, so any bound loose enough to be safe is meaningless and a
tight one is a NEW false-kill surface on exactly the path this rebuild exists to
stop false-killing.

Fail-closed properties:

- **Idle is not a stall.** The quote loop registers `idle=rfq_work.empty`; a quiet
  slate never reads as wedged, but a queue that stops draining always does.
- **Latch.** Before the bot has ever published, an absent ledger is "starting up".
  After it has, an absent / corrupt / future-skewed ledger is WEDGED — a stalled
  bot cannot hide by deleting the evidence.
- **Stale publisher.** The reader adds the file's own wall age to each loop age, so
  a ledger that stops being rewritten is stale even if every age in it reads 0.
- **Stale-file trap closed.** The bot republishes the ledger before launching the
  supervisor subprocess (and `tools/ops/start_all.ps1` now purges it alongside the
  heartbeats) — a leftover from a previous run would otherwise latch the new
  supervisor and kill instantly at relight.

### Part 2 — bound the enforcement burst

`QuoteLifecycle._withdraw_batch` is the new shared withdrawal primitive behind
`cancel_quotes_touching` and `cancel_all`:

1. **Concurrent, bounded fan-out** — `asyncio.Semaphore(_CANCEL_FANOUT = 8)`,
   matching `RFQ_WORKERS`, the concurrency the exchange path is already sized for.
   Never sequential (grows with the slate), never unbounded (storms the exchange
   and eats the shared write budget the supervisor's own cancel-all needs).
2. **404 = SUCCESS** — `_already_gone()`, duck-typed on `status == 404` and narrow
   by construction: 429 / 5xx / timeout / transport are still FAILURES. A quote the
   exchange has already dropped is provably off the wire and cannot fill.
   `delete_quote_failed` (warning) → `delete_quote_already_gone` (info + counter).
   Applied to the single-quote `_delete_quote` path too — that is where the 63
   incident warnings came from.
3. **Per-call wall bound** — `asyncio.wait_for(..., DEFAULT_REQUEST_TIMEOUT_S)`,
   reusing the REST client's own request timeout (now a named constant + a
   `request_timeout_s` property) rather than inventing a second number.
4. **Whole-pass budget** — `cancel_quotes_touching(..., budget_s=STATUS_TICK_INTERVAL_S)`,
   the calling loop's own cadence. The budget is a HARD wall (it also clamps each
   admitted call's timeout), not just an admission gate — otherwise a hung exchange
   still costs one full timeout per wave (measured: 20 s against a 0.2 s budget).

Deferred quotes were never asked about, so they stay in our mirror, count as
not-provably-withdrawn, and are retried next tick. **The existing escalation is
untouched:** a quarantine still unenforced on the next status tick is still
promoted to `HALT_METADATA_CHANGE`, and
`test_unenforceable_quarantine_escalates_to_halt` is green.

---

## 3. Benchmark — one tick, before vs after, heartbeat watched throughout

`tools/bench_quarantine_enforcement.py`. Per-delete latency **0.42 s** = the
measured mean spacing of the incident's 63 404s. BEFORE = a local
re-implementation of the old shape (sequential, 404-as-error, beat only between
ticks); AFTER = the live path with the real `_liveness_loop` running. Wedge
tolerance in the harness is the demo default 15.0 s (live is 30.0 s).

| case | withdrawals | BEFORE tick | AFTER tick | speedup | BEFORE worst HB age | AFTER worst HB age |
|---|---|---|---|---|---|---|
| **12 markets** (the incident) | 36 | **15 523 ms** → WEDGE-KILLED | **2 144 ms** | 7.2× | 15.51 s | **1.013 s** |
| **40 markets** (= live `max_open_quotes` 120) | 120 | **51 736 ms** → WEDGE-KILLED | **6 462 ms** | 8.0× | 51.72 s | **1.010 s** |
| **100 markets** (stress, past the cap) | 300 | **129 340 ms** → WEDGE-KILLED | **15 008 ms** | 8.6× | 129.33 s | **1.009 s** |

AFTER heartbeat ages across 963 samples in the 100-market case: **median 0.508 s,
max 1.009 s** — i.e. the beat lands every poll interval and **never approaches the
tolerance**, at any wave size. In the 12- and 40-market cases all quarantines were
marked enforced (`unenforced 0`). In the 300-withdrawal stress case — beyond the
bot's own 120 `max_open_quotes` cap — the 15 s budget bound and 100 quarantines
stayed unenforced, i.e. it **deferred fail-closed instead of stalling the loop**.

---

## 4. Tests (all executed)

New: `tests/test_liveness_progress.py` — **13 passed**.

| # | requirement | test |
|---|---|---|
| 1 | the exact incident: 12 markets in one tick, slow 404ing deletes, heartbeat still beats | `test_incident_twelve_market_wave_never_stales_the_heartbeat` — real `_liveness_loop` + real `cancel_quotes_touching`, heartbeat sampled at 10 ms; worst age ≤ 3 intervals, all 12 enforced, elapsed < ⅓ of sequential |
| 2 | a genuinely wedged quote loop is STILL detected | `test_wedged_quote_loop_is_still_detected_with_a_fresh_heartbeat` — beater keeps `heartbeat_wedged() is False` the whole time; supervisor still fires, cancels both quotes, writes KILL + marker. Plus `..._dies_at_the_same_age_as_the_old_signal`, `..._fail_closed_once_established`, `..._stale_publisher_is_wedged`, `test_idle_queue_is_not_a_stall` |
| 3 | 404 = already gone, not an error path | `test_already_gone_is_404_and_only_404`, `test_404_deletes_count_as_enforced_not_as_failures` (12 quotes, `failures == 0`), `test_non_404_delete_errors_still_fail_closed` (429 ⇒ all failures) |
| 4 | bounded concurrency at 100+ markets | `test_enforcement_fanout_is_bounded_at_a_hundred_markets` — 300 withdrawals, peak in-flight ≤ 8 **and** > 1; `test_a_hung_exchange_defers_instead_of_stalling_the_caller` |
| 5 | `tests/test_metadata_change_scope.py` stays green | **40 passed** (3 stub signatures gained `**_: object` for the new `budget_s` kwarg; no assertion changed) |

Full suite: **2941 passed, 3 deselected, 0 failed** (124 s). mypy strict clean on
every changed module; ruff clean on every changed file.

---

## 5. Files changed

| file | change |
|---|---|
| `src/combomaker/risk/progress.py` | **NEW** — `ProgressLedger` / `ProgressReader` / `progress_path` |
| `src/combomaker/ops/quote_app.py` | `_liveness_loop` (dedicated beater); maintenance/quote/status progress marks; loop registration; lifecycle `beat=` re-pointed at the ledger; `budget_s` on enforcement; `MAINTENANCE_TICK_INTERVAL_S` / `STATUS_TICK_INTERVAL_S` / `LOOP_*` constants; startup ledger publish |
| `src/combomaker/ops/supervisor.py` | `progress_path` config, `ProgressReader`, `wedged_detail()` two-axis verdict |
| `src/combomaker/rfq/lifecycle.py` | `_already_gone`, `_CANCEL_FANOUT`, `_CANCEL_TIMEOUT_S`, `_withdraw_batch`; `cancel_all` + `cancel_quotes_touching` rewritten on it; `_delete_quote` 404 + timeout |
| `src/combomaker/exchange/rest.py` | `DEFAULT_REQUEST_TIMEOUT_S`, `HTTP_NOT_FOUND`, `request_timeout_s` property |
| `tools/ops/start_all.ps1` | purge stale `data/loop_progress.json` |
| `tools/bench_quarantine_enforcement.py` | **NEW** — the before/after benchmark above |
| `tests/test_liveness_progress.py` | **NEW** — 13 tests |
| `tests/test_metadata_change_scope.py` | 3 stub signatures accept `**_` |

`heartbeat_timeout_s` was **not** raised. It is still 30.0 s in
`config/prod-live-wc.local.yaml`, whose own comment ("the durable fix is …a
thread-based heartbeat beat that CPU-bound RFQ processing cannot starve") is what
this change finally delivers.

---

## NEXT STEPS

1. **Operator — relight decision.** The `KILL` file is still present by design
   (`supervisor kill: heartbeat wedged (age=30.1s > 30.0s)`). Nothing here deletes
   it. `tools/ops/start_all.ps1` prompts before removing it; the new ledger purge
   runs in the same hygiene block. Tonight's slate starts 18:40 ET.
2. **Watch on the first live run:** `delete_quote_already_gone` should replace the
   `delete_quote_failed` 404 storm at every game end; `quote_withdrawal_budget_deferred`
   should be **absent** (it means a pass hit the 15 s budget — expected only past
   the 120 `max_open_quotes` cap); `supervisor_heartbeat_wedged` reasons now name a
   loop when the progress axis fires.
3. **Owed follow-up (not done here):** the liveness task starts with the other
   tasks, so the startup window between the preflight beat and task launch is still
   covered only by the two explicit startup beats — same as before this change, but
   starting the beater earlier would strictly improve it.
4. **Open for the operator (unchanged from the resume state):** resume posture,
   `kill_anchor` %, C1/C3/C5.
