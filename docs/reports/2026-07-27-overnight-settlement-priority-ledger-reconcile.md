# Overnight fact-resolution: settlement read priority, ledger reconciliation, balance cadence

**Date:** 2026-07-27 (evening, ET)
**Scope / blast radius:** `marketdata/settled.py`, `risk/settlement.py`,
`risk/balance.py` (pure extraction), `ops/write_budget.py`,
`ops/persistence.py`, `ops/quote_app.py` (wiring only).
**NOT touched:** `risk/limits.py`, `risk/net_effect.py`, `risk/entity_admission.py`,
`risk/exposure.py`, `rfq/lifecycle.py` — the concurrent admission workflow owns those.
**Pricing / markups / caps:** unchanged. No risk threshold moved.

Operator requirement under test (#6 of tonight's definition of done):

> "the bot can run through the night, safely recognize settled and resolved legs,
> along with our current positions and balance as they change through the night."

---

## 1. What the live run actually says

Measured on `data/live_20260727_1839.log` (555 MB, 22:39:58Z → 00:20:45Z, the
process still running) and on `data/combomaker-prod-live-wc.sqlite3` read-only.

| signal | count in the run | note |
|---|---|---|
| `settled_fetch_failed` | **94** | **100 % `RateLimitedError` HTTP 429** — every one an EXCHANGE refusal |
| `settled_read_budget_deferred` | 0 | our LOCAL bucket never refused a settlement read |
| `settled_marginal_resolved` | 5 | |
| `settled_resolution_pending` | 2 607 passes | `n_pending` 135 → 51 over the run |
| `metadata_fetch_failed` | 1 637 | mostly 429, some 404 |
| `reprice_sweep_budget_deferred` | 60 | the LOCAL read bucket *does* hit empty |
| `position_ledger_divergence` | 21 | `open_ledger_rows` 120→140 vs `open_positions` 65→85; `rows_without_position` **56, constant all run** |
| `balance_poll_failed` / `_stale` / `_rate_limited` | **0 / 0 / 0** | |
| `position_reconcile_failed` | 0 | |
| `account_standing` | 1 | at 22:40:13Z — a **boot report line**, not the refresh mechanism |

### Correction to the framing of DEFECT 1

The brief says settlement resolution starves on *our* read budget. It does not,
in this run: the local bucket granted every settlement read (0
`ReadBudgetExhausted`), and all 94 failures came back from Kalshi as 429. The
mechanism is one step removed:

`_reserve_read_token` **waits** for tokens (up to one full refill) instead of
refusing. While it waits, the continuous metadata refresh wins the intervening
tokens, so by the time the settlement GET is issued our local model still has
budget but the **exchange's** bucket is empty → 429. The local bucket and the
exchange bucket are the same shape (capacity 600, 300 tok/s, GET = 10 tok,
live-verified via `GET /account/endpoint_costs` `default_cost=10`), so holding a
local floor of 50 tokens that only the critical tier may draw means we stop
issuing routine reads while the exchange still has ≈5 GETs of headroom, and the
settlement read lands in that headroom.

That is the reserve's real mechanism, and it is **indirect**. It is proven
offline against a local-budget storm; it is **not** proven against exchange-side
429s, because that needs live traffic. See §5.

### Correction to `n_never_fetched`

`n_never_fetched == n_pending` in the log looks like "nothing was ever tried".
It is not: `_ingest` calls `_drop_pending`, which clears the attempt counter, for
every market that comes back **live**. The lifecycle re-registers it next tick,
so a healthy live-market recheck reads as "never fetched". The metric is
misleading, not the resolver.

---

## 2. DEFECT 2 — root cause confirmed by reading the store, not assumed

The brief asked which side of the divergence is wrong. It is **rows never being
closed**, confirmed:

```
position_ledger:  open 140 rows ($1 516.44 cost basis)  |  settled 7 rows
open rows -> 133 DISTINCT combo tickers  vs  85 open exchange positions
tickers with >1 open row: 5  (7 extra rows total — legitimate multi-fill)
prefixes: fill:103  rehydrate:36  reserve:1
```

So the divergence is ~48 tickers whose combos are gone from the exchange but
whose ledger rows are still `open`. It is **constant within a run and steps only
at restarts** — the signature of a combo that settled while the process was
down: it is absent from `get_positions` at the next boot, the exposure book never
holds it, and `_handle_one` therefore dropped its settlement as "not ours".
Duplicate rows are *not* the cause (7 of 140).

The durable-identity fix from earlier does work where it applies —
`_resolve_open_ledger_row` matches on `(leg_set_hash, combo_ticker, our_side)`
and 7 rows are `settled` — but it can only fire for positions the exposure book
still holds.

### The repair

`SettlementHandler._reconcile_orphan_ledger_rows`: when a settlement arrives for
a ticker the exposure book does not hold, close its OPEN ledger rows from
exchange truth.

* **Ledger-only.** Never touches cash, the in-memory realized accumulators, the
  exposure book or any cap. That money moved on the exchange in a previous
  process; re-booking it into today's realized half would corrupt the *enforced*
  daily-loss ladder.
* **Fail-closed on ambiguity.** The rows' Σ gross payout must reconstruct the
  exchange's own `revenue` to the cent (the same identity the live path HALTs
  on), the exchange's `settled_time` must parse, and each row's side/quantity
  must read. Anything else ⇒ the row stays **OPEN** with a loud alarm. There is
  **no `DELETE` on `position_ledger` anywhere in the tree**; the only mutation is
  `UPDATE … WHERE position_id=? AND status='open'`.
* **Alarms, does not HALT.** We hold no position, so there is no live risk to
  stop trading over.
* **Attribution.** `reconciled_at` is the exchange's `settled_time`, not "now",
  so `day_realized_pnl_cc` (p_night's cross-restart seed) buckets a row that
  settled last night into last night.
* **One payout formula.** `settlement_realized_cc` /
  `settlement_payout_per_contract_cc` extracted from
  `BalanceTracker.apply_settlement` — the reconciliation path cannot drift from
  the live path because there is no second copy.

**Scale.** The poller re-pages the account's *entire* settlement history every
30 s (up to 50 × 200 = 10 000 rows). The naive shape issued one DB round-trip per
historical settlement on the first pass of every process. Now one indexed
`SELECT DISTINCT combo_ticker … WHERE status='open'` per **batch** pre-filters
the haystack:

```
ORPHAN SEARCH COST : 2001 settlement rows -> 1 batch scan, 1 per-ticker read; orphans closed=1
```

Fail-open: if the scan errors the pre-filter becomes a no-op and every row takes
the per-ticker read (slower, never blind — tested).

**Effect on the realized seed.** `day_realized_pnl_cc` is read **only at boot**,
to seed `lifecycle.record_realized_pnl`. Today it *under*-counts (rows never
close). After the fix the next boot's seed is more accurate — i.e. the daily-loss
ladder gets tighter and truer, never looser. In-process nothing changes.

---

## 3. DEFECT 3 — verified, and it is NOT a defect

`account_standing` fires once because it is a **boot report line**
(`quote_app.py:4264`), not the refresh mechanism. The refresh paths, read from
the code and confirmed by the log:

| fact | refreshed by | cadence | fail-closed? |
|---|---|---|---|
| cash + portfolio value (⇒ equity ⇒ every %-of-bankroll cap) | `_balance_loop` → `BalanceTracker.refresh` | **10 s** (`BALANCE_POLL_INTERVAL_S`) | yes — stale > **30 s** (`BALANCE_STALE_AFTER_S`) ⇒ `risk_bankroll_cc_or_none()` = None ⇒ `SKIP_BANKROLL_UNAVAILABLE` |
| settled markets / realized P&L / position removal | `_settlement_loop` → `SettlementPoller` | **30 s** | HALT on a mismatch |
| exchange positions vs the book (adopt / alarm / release) | `_position_reconcile_loop` | **300 s** (`position_reconcile_interval_s`) | yes |
| settled-leg 0/1 marginals | `SettledMarginalResolver` via the maintenance tick | per pass, ≤ 5 GETs, 30 s backoff | UNKNOWN ⇒ no-quote |
| derived deploy/halt caps | `_refresh_adaptive_caps_once` | **1 800 s** | keeps current limits on error |

Live: 0 failed/stale/rate-limited balance polls, 0 failed position reconciles.
**No cap goes stale overnight while those loops run.** Do not file a defect here.

### One real overnight behaviour, pre-existing, NOT fixed tonight

`BalanceTracker.refresh` re-anchors `start_of_day_equity`, `peak_equity` and
`peak_pnl` on the **UTC calendar date** — i.e. at **00:00 UTC = 20:00 ET**, in
the middle of a US slate. The drawdown halt and the 12 % hard-trip KILL measure
give-back from `peak_equity`, so after 20:00 ET they measure from the 20:00
equity rather than from the day's real peak: **the give-back halts are looser
after 8 pm ET, and losses booked before 8 pm stop counting toward the daily-loss
halt.** This is flagged for the operator in the code's own docstring
("Day-boundary rule (FLAGGED for operator)") with `set_start_of_day_equity` as
the override.

I did **not** change it. Picking a boundary is a risk-policy decision (operator
territory, and it would be a hand-set number), and changing a halt anchor on a
live unattended bot tonight is exactly the unrequested risk intervention the
standing rules forbid. **It costs money in a tail, not bookkeeping.** Operator
decision owed.

---

## 4. Adversarial gate

| # | requirement | evidence | verdict |
|---|---|---|---|
| a | settlement resolution genuinely cannot starve under sustained read pressure | A/B under a 3 s hot-loop read storm at the observed advanced tier: **CONTROL (reserve=0) 9/64 resolved, 55 still pending, 1 settlement deferral; SHIPPED (reserve=50) 64/64 resolved, 0 pending, 0 deferrals.** Backlog clearance bounded: 64 pending → 64 resolved in **13 passes = ceil(64/5)** | PASS (local budget). Exchange-side 429s: see §5 |
| b | prioritising settlement did not starve METADATA (feeds no-quote + breaker) | A token bucket's sustained rate is its refill rate; a floor removes only burst depth. Measured: burst **60 → 55 calls (−5 = reserve/cost)**; over 10 s **359 → 354**, over 30 s **959 → 954** — the deficit is the **same constant 5**, a level shift not a rate change. Worst extra wait at the bucket floor **167 ms** (= 50 tok / 300 tok·s⁻¹). Routine tier keeps 550/600 burst and 100 % of sustained rate | PASS |
| c | reconciliation can never delete a row representing real exposure | No `DELETE FROM position_ledger` exists in the tree; the only write is `UPDATE … WHERE status='open'`, gated on (i) the EXCHANGE reporting the market settled, (ii) the book not holding it, (iii) Σ gross payout reconstructing `revenue` to the cent. Test: 2 rows predicting 35 000 cc against 10 000 cc of revenue are **LEFT OPEN**; an unreadable `settled_time` leaves the row **OPEN** | PASS |
| d | no risk threshold goes stale overnight | §3 table; live 0 balance-poll failures. **Named exception: the 00:00 UTC peak/SOD re-anchor — pre-existing, not fixed, operator decision** | PASS with a named exception |
| e | no hand-set number | reserve = `FETCH_BUDGET_PER_PASS` (the resolver's own pre-existing per-pass claim) × `DEFAULT_ENDPOINT_TOKEN_COST` (live-verified from `GET /account/endpoint_costs`). Starvation alarm + its throttle window = the resolver's own `retry_after_s`. Test asserts the reserve is derived, not typed | PASS |
| f | no quote-path throughput regression | Only hot-path primitive touched is `WriteBudget.try_spend` (a floor subtraction; the `_floor()` helper was **inlined** after it measured +8.8 %). Interleaved A/B, FakeClock, 1 M calls × 7 reps, min-of-reps: HEAD **374.3 ns**, shipped **420.2 ns** (+12.3 %) — **+46 ns/call**. Operationally: `try_spend` gates REST calls, not pricing; at the metadata ceiling of 30 GET/s that is **1.4 µs per second**. Nothing else changed is on the pricing/quote path | PASS |
| g | `tools/vitals/gate` GREEN | 8/8 GREEN | PASS |
| h | suite green | **3 308 passed, 3 deselected** (`-p no:randomly`), mypy strict clean on all five changed modules, ruff clean on all changed files (the 3 remaining ruff hits are pre-existing in `metadata.py`, untouched) | PASS |

### Starvation is now observable, and the alarm actually fires

The first draft alarmed only on "pending entry overdue by > one retry cycle".
That detector is **blind to the exact failure it was written for**: a failed read
re-arms the backoff, so a ticker whose GETs are all being refused looks
permanently on schedule and the alarm never fires. Added a second signature —
`_unserved_since`: a ticker whose reads have been *failing* for longer than one
retry cycle, cleared by any successful GET. `settled_resolution_starved` now
carries `n_overdue` and `n_unserved` separately, and is **throttled to one line
per retry cycle** (a pass runs every few hundred ms; unthrottled it would emit
thousands of identical WARNINGs an hour and train the operator to ignore it),
re-arming the instant progress resumes.

---

## 5. What still degrades, and what it costs

| # | what | cost |
|---|---|---|
| 1 | **Exchange-side 429s on settlement reads are reduced, not eliminated.** The reserve makes our local bucket yield with ≈5 GETs of exchange headroom left, but local and exchange buckets can desync (our model does not meter `get_balance`/`get_positions`/`get_settlements`/status — ≈1.5 tok/s of 300, small but non-zero) and pacing exactly at the ceiling produces occasional 429s by construction. Residual 429s cost a **30 s retry each**, and are now visible as `settled_resolution_starved` with `n_unserved > 0`. | Bookkeeping + a bounded delay to fact-resolution. Not money, unless a graded leg stays UNKNOWN long enough to no-quote flow we wanted. **Unproven against live traffic** — verify on the tape after the restart. |
| 2 | The principled cure — **draining the local bucket to zero on an observed 429**, so our model resynchronises with the exchange's — is deliberately **not shipped tonight**. It would reduce read throughput materially (1 637 metadata 429s in 100 min) and could push metadata into staleness ⇒ no-quote. Needs a measured live run first. | — |
| 3 | **The 00:00 UTC (20:00 ET) peak/SOD re-anchor** (§3). | **Money, in a tail.** The drawdown and 12 % KILL give-back thresholds measure from the 8 pm ET equity for the rest of the night. |
| 4 | The ~48 orphan rows only close when their settlement rows come back inside the poller's paging window (50 × 200 = 10 000 rows). They are from 26–27 Jul, so they will be on the first pages — but this is unverified against the live endpoint's ordering. | Bookkeeping only. |
| 5 | Orphan rows on a ticker we DO still hold are not swept — `_handle_one` takes the live path and never reaches the orphan branch. Live that is ≤ 7 rows (the multi-fill case). | Bookkeeping only. |
| 6 | **None of this is live.** The running process (PID 16224) is on the old code; `read_budget_armed` = 0 in the log confirms it. Everything here lands on the operator's next restart. | — |
| 7 | `store_writer_checkpoint_failed` × 571 (`database table is locked`, passive fallback also failing) is untouched by this work and is a separate durability question. | Not assessed here. |

---

## 6. Straight answer for an operator leaving it unattended

**Balance and positions: yes.** Cash and portfolio value refresh every 10 s and
fail closed at 30 s; positions reconcile against the exchange every 5 minutes;
settlements poll every 30 s and HALT on a to-the-cent mismatch. Zero failures of
any of these in the observed run. `account_standing` appearing once is a report
line, not a gap.

**Settled and resolved legs: yes, better than tonight, not perfectly.** After the
restart the resolver holds a reserve the metadata flood cannot spend, backlog
clears in a bounded number of passes, and when it *doesn't* clear it says so
loudly instead of being inferable only from a 429 count. Residual exchange 429s
will still delay individual facts by ~30 s each, and that residual is **not
proven** offline.

**The ledger will stop drifting.** Orphan rows close from exchange truth or stay
open loudly; nothing is ever deleted or guessed. The 56-row divergence should
walk to ~0 over the first few settlement polls after the restart.

**Will it survive until morning? Nothing here changes that either way.** This
work is fact-resolution and bookkeeping; the only hot-path delta is +46 ns on a
token-bucket call. The one thing that genuinely changes risk overnight is
**pre-existing and unfixed**: at 20:00 ET the drawdown and KILL anchors
re-baseline to that moment's equity. If the night goes badly before 8 pm, those
halts will be looser than the operator expects afterwards.

---

## NEXT STEPS

1. **Operator** — restart to land this (the live process is on old code). One
   restart, as agreed.
2. **After the restart, verify on the tape** (owner: next session): `read_budget_armed`
   present with `critical_reserve_tokens=50`; `settled_read_budget_deferred` = 0;
   `settled_fetch_failed` 429 rate vs tonight's 94/100 min; `settled_resolution_starved`
   absent or with `n_unserved` explained; `position_ledger_divergence`
   `rows_without_position` walking to 0; `settlement_orphan_row_closed` count vs
   the 48 tickers; `settlement_orphan_row_ambiguous` = 0 (any non-zero is a
   convention question, investigate before trusting the close).
3. **Operator decision owed — the 00:00 UTC give-back re-anchor.** Keep the UTC
   boundary, move to an ET slate boundary, or anchor manually? It is a policy
   anchor, not a knob I should pick.
4. **Deferred build** — resync the local read bucket from observed 429s
   (§5 item 2), only after a measured live run shows the residual 429 rate.
5. **Unrelated, unassessed** — `store_writer_checkpoint_failed` × 571 with the
   passive fallback also failing needs its own look.
