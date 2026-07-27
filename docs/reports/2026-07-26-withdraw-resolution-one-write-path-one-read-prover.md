# 2026-07-26 — ONE WRITE PATH, ONE PROVER, AND THE PROVER IS A READ

**Scope.** The adversarial gate on the first B3 cut (`_delete_quote` keeps an
unresolved quote in the mirror) proved three defects with PoCs. This report is
the fix for all three, the PoCs re-run against it, and the throughput evidence.

**Blast radius.** `rfq/lifecycle.py` (withdrawal paths + the maintenance tick's
sweep loop), `ops/quote_app.py` (one wiring block + one decorator method).
**Nothing on `handle_rfq`, `_price_async`, the pricing pool, or the confirm
path.** Suite **2994 passed / 0 failed** (was 2969/0; +25 new tests).

---

## What was broken

| # | Defect (proved by the gate's PoCs) | Mechanism |
|---|---|---|
| 1 | **`cancel_all` dropped UNRESOLVED ids** | Its docstring premise was "terminal path, there is no next tick". **False for 5 of its 7 call sites** — `on_invalidate` (feed resync), `on_channel_lost` (which force-**reconnects**), `HALT_EXCHANGE_STATUS`, and both `DECLINE_FILL_VELOCITY` sites all keep the bot running. Under a 429 storm the mirror forgot quotes that were still resting and could still fill. |
| 2 | **Self-amplifying write storm** | (a) `_delete_quote` owned a **SECOND, UNMETERED** `self._sender.delete_quote` call — the busiest way to withdraw a quote (**1,137 calls** in the 2026-07-26 incident: 438 TTL + 1,104 RFQ-gone), spending **zero** write tokens. (b) The reprice sweep **re-drove every pending withdrawal every 0.5 s tick** — `O(pending)` writes/tick on the bucket that was already 429ing. At the live `max_open_quotes` = 200 that is **400 write tok/tick = 800 tok/s against a 300 tok/s ceiling**. |
| 3 | **Quoting bricks** | The storm sustained its own 429s and never established truth, so the pending set never drained, the book pinned at `max_open_quotes`, and every new RFQ was refused for capacity. |

---

## What shipped — three changes, two of which are deletions

```
                    ┌────────────────────────── THE ONE WRITE PATH ────────────┐
 _delete_quote ─┐   │                                                          │
 cancel_all ────┼──►│ _withdraw_and_reconcile(ids, reason, budget_s)           │
 cancel_quotes_ ┤   │   └─► _withdraw_batch  ─── token gate ──► _one:          │
   touching     │   │          (DELETE_QUOTE_TOKEN_COST spent BEFORE the call) │
 resolver drain ┘   │          self._sender.delete_quote(...)   ← ONLY SITE    │
                    │   gone ⇒ _drop_quote                                     │
                    │   else ⇒ stays in mirror, marked withdraw-pending        │
                    └──────────────────────────────────────────────────────────┘
                                            │  UNKNOWN
                                            ▼
 maintenance_tick ──► _resolve_withdraw_pending()      ← THE PROVER, A READ
        │              GET /communications/quotes?user_filter=self&status=open
        │              10 READ tokens, ONE call, O(1) in the pending count,
        │              on the bucket the write storm is NOT touching
        │                 absent  ⇒ PROVEN gone     ⇒ _drop_quote
        │                 present ⇒ PROVEN resting  ⇒ metered re-DELETE
        │                 read failed / ask post-dates the read ⇒ NOTHING
        ▼
 reprice sweep ──► pending quotes are SKIPPED (no longer a write driver)
```

**1 — the second, unmetered DELETE path is deleted.** `_delete_quote` is now a
one-element call into `_withdraw_and_reconcile` (plus its existing single-quote
decision record, deliberately kept off the batch paths so a whole-book cancel
does not add N store writes to the halt path — the maintenance-stall class we
just removed).

**2 — the UNKNOWN is resolved by a READ.** `_resolve_withdraw_pending()` runs
once at the top of `maintenance_tick`, **before** the reprice sweep. It returns
instantly when nothing is pending (the steady state). It reuses
`exchange/quote_query.list_open_quotes` — the SAME enumerator the startup
reconcile and the supervisor kill path use, with the mandatory `min_ts`/`max_ts`
window that stops the full-history scan from tripping the exchange circuit
breaker — at `retries=1` (the tick is the retry loop) under the existing
`_MAINTENANCE_POLL_TIMEOUT_S`.

**Happens-before, by construction not by delay.** `withdraw_attempts` (written,
logged, never read) is **deleted** and replaced by `withdraw_asked_mono_ns`.
Only a quote whose ask **strictly pre-dates** the list request may be judged by
that request's answer, so a quote deleted after the read went out can never be
read as absent. The stamp is taken **after** the batch returns, so it is always
≥ the instant the request actually went out — a later stamp can only make the
resolver more conservative.

**3 — the retry driver and the `cancel_all` special case are deleted.** The
sweep's `withdraw_pending_reason` branch now just `continue`s. `cancel_all` makes
the same `_withdraw_and_reconcile` call `cancel_quotes_touching` already made.
One rule, no flag, no branch.

**4 — the bounded exit.** Pending quotes **stay** in `_open` and
`exposure.open_quotes`, so they keep counting in `max_open_quotes` and in every
mass-acceptance fold (correct: a quote that may be resting is real risk *and*
real capacity). The terminal state is a predicate over **existing** state and an
**existing** limit, with no new number:

> `len(_open) >= limits.max_open_quotes` **and** every open quote is
> withdraw-pending **and** no successful open-quote read has landed since the
> oldest of those asks ⇒ `HALT_NEEDS_RECONCILE`.

**No new tuned numbers.** The resolver's whole-pass wall bound is
`_WITHDRAW_RESOLVE_BUDGET_S = _REPRICE_SWEEP_BUDGET_S` (an existing primitive);
the read's bound is the tick's existing `_MAINTENANCE_POLL_TIMEOUT_S`; the read
cost is `DEFAULT_ENDPOINT_TOKEN_COST`; the write cost is
`DELETE_QUOTE_TOKEN_COST`; the capacity bound is `risk.max_open_quotes`.

**Wiring.** Two optional ctor args, `quote_lister` + `read_budget`, passed from
`quote_app` (the same wrapped sender whose 429s feed the burst breaker — it grew
a `get_quotes` pass-through — and the ONE read bucket every other read spends
from). Unwired (paper / backtests / minimal rigs) ⇒ the resolver falls back to
the **metered write drain**, which still terminates (a re-DELETE of a gone quote
404s = proof). Fail-closed either way: nothing is ever dropped without proof.

**One sweep-loop change for fix isolation.** The reprice sweep's wall-budget
check moved to the **top** of the loop body. It had to: every branch below it —
the TTL delete included — now goes through the token-paced write path and can
therefore WAIT for the bucket, so the budget has to bound the loop *before* the
branch. Checked there it also finally matches its own comment ("the current quote
was NOT handled — resume AT it next tick").

---

## The invariants (source-checked, not asserted in prose)

`tests/test_withdraw_resolution.py` parses `lifecycle.py` with `ast`:

| Invariant | Test |
|---|---|
| `self._sender.delete_quote` appears **exactly once**, inside `_withdraw_batch`, and a `try_spend` precedes it in that function | `test_sender_delete_quote_has_exactly_one_call_site` |
| `_delete_quote`, `cancel_all`, `cancel_quotes_touching`, `_withdraw_batch` contain **zero** `_drop_quote` calls; the only droppers of a withdrawn quote are `_withdraw_and_reconcile` (the `gone` list) and `_resolve_withdraw_pending` (absent from the exchange's list) | `test_no_withdrawal_entry_point_can_drop_an_unproven_quote` |
| The sweep's pending branch contains **no `await` at all** | `test_the_reprice_sweep_is_no_longer_a_write_driver` |
| Exactly **7** `cancel_all` call sites tree-wide, all on the one implementation | `test_cancel_all_call_sites_are_all_the_one_implementation` |

---

## REQUIRED TEST 1 — `cancel_all` under a persistent 429, from EVERY caller

Parametrized over all 7 call sites with **the exact argument each one passes**.
Under a persistent 429: every quote stays in `_open` **and** in
`exposure.open_quotes`, is marked withdraw-pending with the caller's reason, and
`quote.deleted.<reason>` stays 0. Under an ack or a 404 the same quote IS
dropped; a mixed book drops exactly the proven half.

| Call site | Argument | 429 ⇒ kept? | ack/404 ⇒ dropped? |
|---|---|---|---|
| `quote_app:on_invalidate` (feed resync) | `reason: str` | ✅ | ✅ |
| `quote_app:on_halt` → `_stop.set()` | `event.reason` | ✅ | ✅ |
| `quote_app:on_channel_lost` (force reconnect) | `reason: str` | ✅ | ✅ |
| `quote_app:shutdown` | `HALT_MANUAL` | ✅ | ✅ |
| `quote_app:HALT_EXCHANGE_STATUS` | `HALT_EXCHANGE_STATUS` | ✅ | ✅ |
| `lifecycle:confirm DECLINE_FILL_VELOCITY` | `DECLINE_FILL_VELOCITY` | ✅ | ✅ |
| `lifecycle:tick DECLINE_FILL_VELOCITY` | `DECLINE_FILL_VELOCITY` | ✅ | ✅ |

The tick's fill-velocity site is additionally driven **end to end** through
`maintenance_tick` (`test_tick_fill_velocity_cancel_all_is_driven_end_to_end`).

---

## REQUIRED TEST 2 — persistent write-side 429: BOUNDED and METERED

Every DELETE 429s forever and the exchange's open-quote list says **every**
pending quote is still resting — the worst case, where every one justifies a
re-DELETE. Write bucket = the live one (`_tier_clamped_write_budget`: operator
knob 200 tok / 10 s, clamped by the observed advanced tier 600 cap / 300 refill)
⇒ **capacity 200 tok, 20 tok/s = 10 DELETEs/s sustained**.

```
  DELETE STORM  (12 pending, every DELETE 429, all PROVEN resting)
    write bucket : capacity 200 tok, 20.0 tok/s
    emitted      : 144 DELETEs = 288 write tokens over 6.00s
                 = 24.0 req/s = 48.0 tok/s
    bucket bound : capacity + rate*T = 200 + 20.0*6.00 = 320 tok  (288 <= 320)
    peak any 1s  : 48 tok  <= capacity + rate = 220 tok  << ceiling 300 tok/s
    list reads   : 12 over 12 ticks = 1.00/tick x 10 read tok
                 = 10 read tok/tick   (O(1) in pending, SEPARATE bucket)
    DELETED driver: re-drove ALL 12 pending every 0.5s tick, unmetered
                 = 24 write tok/tick = 48 tok/s vs the 300 tok/s ceiling

  DELETE STORM  (200 pending = the live max_open_quotes)
    emitted      : 348 DELETEs = 696 write tokens over 36.00s
                 = 9.7 req/s = 19.3 tok/s
    bucket bound : capacity + rate*T = 200 + 20.0*36.00 = 920 tok  (696 <= 920)
    peak any 1s  : 28 tok  <= 220 tok  << ceiling 300 tok/s
    list reads   : 12 over 12 ticks = 10 read tok/tick
    DELETED driver: re-drove ALL 200 pending every 0.5s tick, unmetered
                 = 400 write tok/tick = 800 tok/s vs the 300 tok/s ceiling
                   (2,400 DELETEs over these same 12 ticks)
```

**The arithmetic.** At the live book the deleted retry driver emitted
`200 × 2 = 400` write tokens per 0.5 s tick = **800 tok/s**, i.e. **2.7× the
account's 300 tok/s write ceiling**, unmetered — that is the 429 feedback loop.
The shipped path emitted **348** DELETEs in the same 12 ticks (vs 2,400), at
**19.3 tok/s**, never exceeding the bucket's own guarantee
`capacity + rate·T` in any window and never exceeding **28 tokens in any single
second** against a 300 tok/s ceiling. Emitted rate is bounded **by the bucket, by
construction** — the token gate spends before the request and WAITS for refill —
not by a cadence anyone tuned. Reads: **exactly 1 per tick = 10 read tokens**,
flat in the pending count, on the read bucket (observed 600 cap / 300 refill),
which the write storm never touches.

Asserted, not just printed: tokens were actually spent (`budget.tokens <
budget.capacity`), `tokens <= capacity + rate·elapsed`, `peak_1s <= capacity +
rate`, `tok/s <= 300`, `req/s <= 600`, `reads <= ticks`, and **nothing was
forgotten** (`set(_open) == the full resting set`).

---

## REQUIRED TEST 3 — QUOTING CANNOT BRICK (the standing ship rule)

Book FULL at the **live `max_open_quotes` = 200**, every DELETE 429s forever,
every one of the 200 quotes an UNKNOWN withdrawal.

```
  BRICK TEST  (max_open_quotes=200, every DELETE 429)
    at capacity   : new RFQ refused (CORRECT — 200 quotes of real risk)
    one list READ : 10 read tokens resolved all 200 pending; 0 successful DELETEs
    QUOTED AFTER  : yes 22.00c   no 72.00c   risk_qty 10.00 contracts
```

**This is the dominant live case**, not a contrived one: **1,104 of the
incident's 1,137** withdrawals were `DELETE_RFQ_GONE`, whose quotes the exchange
had already dropped with their RFQs. One read proves them gone, the mirror
empties, and quoting resumes **on the same tick with zero successful DELETEs
anywhere in the story**. `rig.sender.deleted == []` is asserted.

**The other branch also terminates.** If the 200 really ARE still resting and the
write path really cannot remove them, refusing to quote is *correct* (the
exposure is real), so the state escalates rather than sitting bricked:
`HALT_NEEDS_RECONCILE` fires, the mirror is still intact (the halt is an
escalation, not amnesia), and the restart reconcile rebuilds the book from the
exchange over this same endpoint
(`test_a_full_book_of_PROVEN_RESTING_quotes_takes_the_bounded_exit`). Below
capacity, or with one non-pending quote, it does not fire
(`test_a_partial_book_never_halts`).

---

## REQUIRED TEST 4 — gone is reaped, resting is never forgotten

One book, one read: quote A absent from the exchange's open-quote list ⇒ dropped
from `_open` **and** exposure, counted as `withdraw_resolve.proven_gone` +
`quote.deleted.<reason>`; quote B present ⇒ kept, still counted by risk, still
withdraw-pending, and **re-asked** (so the eviction/withdrawal intent is never
lost). `asked == [b]` — only the PROVEN-resting quote pays a write.

Plus the three ways the resolver must refuse to conclude:

- `test_a_failed_read_resolves_nothing` — a 429/5xx/timeout on the read leaves
  every pending quote exactly where it was, even though the (unreached) list
  would have said "all gone". UNKNOWN never silently becomes gone.
- `test_happens_before_guard_rejects_a_delete_that_postdates_the_read` — B's
  DELETE lands from inside the list call; the list comes back empty; A (asked
  before the request went out) is reaped and **B is not**.
- `test_the_read_is_refused_rather_than_queued_when_out_of_read_tokens` — an
  empty read bucket is a SKIP (`withdraw_resolve.read_budget_deferred`), never a
  queued wait on the maintenance loop. The 0.5 s tick is the retry.
- `test_the_read_is_windowed_and_scoped_like_the_kill_path` — the request
  carries `user_filter=self`, `status=open`, `min_ts < max_ts`, `limit=500`.

---

## REQUIRED TEST 5 — the pending set cannot grow unbounded

**The bound is `risk.max_open_quotes`, and it comes from the fact that a
withdraw-pending quote is never removed from `_open`/`exposure.open_quotes`.**
It therefore keeps consuming the same capacity cap that bounds the resting book,
and the pending set is a **subset** of the open book. There is no counter, TTL,
or attempt limit of its own — and there must not be one, because "force-drop
after N ticks" is "UNKNOWN silently becomes gone" with a timer (hard rule 6 /
quiet-failure defense #2).

`test_the_pending_set_is_bounded_by_max_open_quotes` drives 8 rounds of
"try to add more quotes + re-run the resolver" against a fully-pending book with
every DELETE 429ing, and asserts `len(_open) <= cap` and
`pending ⊆ open ⊆ the original resting set` every round.

Excluding pending quotes from the cap was explicitly rejected: it would let a
write outage grow an unbounded book of possibly-resting quotes while we keep
quoting on top of it — an uncapped mass-acceptance worst case. The brick is fixed
by making the set **drain**, not by making it **free**.

---

## REQUIRED TEST 6 — existing tests

| File | Result |
|---|---|
| `test_liveness_progress.py` | green |
| `test_metadata_change_scope.py` | green |
| `test_supervisor.py` | green |
| `test_maintenance_stall_and_read_budget.py` | green (2 assertions updated — see below) |
| `test_fill_cancel_verification.py` | green |
| `test_lifecycle.py` | green (1 test restructured — see below) |

Three assertions in existing tests were updated because the design **deletes**
what they asserted; the intent of each test is unchanged:

1. `withdraw_attempts == 1` → `withdraw_asked_mono_ns > 0` (the field is
   replaced, per the design's "field churn is net zero").
2. `withdraw_attempts == 2` → `withdraw_pending_reason is DELETE_RFQ_GONE`
   (same test still proves the RFQ-gone withdrawal is re-driven every tick and
   completes when the exchange recovers; it now exercises the **unwired-lister
   fallback** — the metered write drain — which is exactly the path the design
   specifies for paper/backtest/minimal rigs).
3. `test_reprice_sweep_budget_marker_skips_deleted_quotes` — the sweep's budget
   check moved to the top of the loop body, so the budget is now blown by the
   **DELETE** (the newly realistic case, since deletes are token-paced) rather
   than by the pricing call before it. Same assertion, same intent: the resume
   marker names the surviving q1, never the removed q2.

**Full suite: 2994 passed, 3 deselected (integration), 0 failed.**

---

## THROUGHPUT — fix isolation, before/after

`handle_rfq` is byte-unchanged. The only hot-path site that could move is the EV
slot eviction at `max_open_quotes`, which calls `_delete_quote` and is now
token-metered. Measured against a **legacy `_delete_quote`** (the pre-change
unmetered `wait_for(delete)` + unconditional drop) monkeypatched into the same
rig:

400 RFQs per measurement, `max_open_quotes` = 200, live write bucket, two
independent runs:

| Path | BEFORE (legacy path in the same rig) | AFTER | Δ |
|---|---|---|---|
| A — `handle_rfq`, free capacity (no eviction) | — | **8,062 / 8,024 quotes/min** | untouched code |
| B — `handle_rfq` AT cap, evicting every RFQ | 7,680 / 7,950 quotes/min | **7,774 / 7,914 quotes/min** | **+1.2% / −0.5%** |
| C — `maintenance_tick`, 200 open, 0 pending | 16.20 ms/tick (resolver call removed) | **15.81 ms/tick** | **−0.4 ms** |

The metered eviction delete is **not slower** — the two runs straddle zero, i.e.
run-to-run noise. It should not be: the bucket is uncontended in steady state
(the live budget admits 10 DELETEs/s sustained against a steady-state TTL churn
of 200 quotes / 30 s ≈ 6.7/s). The resolver's steady-state cost is
indistinguishable from removing the call entirely — it early-returns on an empty
pending set.

---

## Behaviours prevented BY CONSTRUCTION

| Defect | Prevented by |
|---|---|
| 1 — `cancel_all` drops unresolved | `_drop_quote` is unreachable from every withdrawal entry point; the only two droppers hold PROOF (ack/404, or absent from the exchange's own list). Correctness no longer depends on any caller's docstring being true. AST-checked. |
| 2 — write storm | (a) One `delete_quote` call site, tokens spent before it: an unmetered write is *unwritable*. (b) The per-tick retry is no longer proportional to the pending set — it is **one 10-read-token GET, O(1) in pending, on the bucket the storm is not touching** (10 read tok/tick vs the PoC's 400 write tok/tick at N=200). (c) Justified re-DELETEs pass a token gate that **waits** for refill, so emitted write rate is bounded by the bucket by construction; the 429 → `HALT_RATE_LIMIT_BURST` loop is severed at its source. |
| 3 — quoting bricks | Every pending quote reaches a definite answer on the next successful maintenance tick, **independent of the write bucket's health**. The set drains under exactly the condition (a readable account) that also proves the drain is safe. If it genuinely cannot drain, capacity refusal is *correct* and the state escalates to `HALT_NEEDS_RECONCILE` → restart reconcile → exchange-proven book. Bounded path back to quoting on every branch. |

---

## NEW OBSERVABILITY

| Metric / log | Meaning |
|---|---|
| `withdraw_resolve.reads` | successful open-quote reads (1 per tick with anything pending) |
| `withdraw_resolve.proven_gone` + `withdraw_resolved_gone` (one aggregated line) | quotes reaped by the read |
| `withdraw_resolve.drained` | quotes removed by the metered re-DELETE |
| `withdraw_resolve.read_failed` / `withdraw_resolve_read_failed` | the read did not answer ⇒ **nothing resolved** |
| `withdraw_resolve.read_budget_deferred` | out of read tokens ⇒ skipped, retried next tick |
| `withdraw_resolve.not_yet_readable` | ask post-dates the read ⇒ deliberately unjudged |
| `withdraw_resolve.drain_budget_deferred` | the pass ran out of wall budget |
| `withdraw_resolve.unprovable_halt` + `cancel_all_unresolved` | the bounded exit fired / a cancel-all left UNKNOWNs |

---

## NOT DONE (deliberately out of scope, flagged for the operator)

**The EV slot-eviction chooser (`lifecycle.py`, `_maybe_evict_lower_ev_slot`)
does not skip withdraw-pending quotes**, unlike `_pick_eviction_victim` which
does. At capacity with a pending lowest-EV quote, every RFQ re-picks it, pays a
metered DELETE that 429s, and declines. This is **bounded and safe** (the write
is metered; declining at capacity is correct; the resolver still frees the slot
on the next readable tick) but it is per-RFQ hot-path work with no chance of
success. The design explicitly did not include it; adding the same one-line guard
is the obvious follow-up. Filed, not shipped.

---

## NEXT STEPS

1. **Owner: operator — DECISION OWED.** Confirm `HALT_NEEDS_RECONCILE` (vs a
   dedicated reason code) for the all-pending-at-capacity terminal state. The
   code ships with `HALT_NEEDS_RECONCILE` per the design.
2. **Owner: operator — DECISION OWED.** Confirm this lands **before** tomorrow's
   relight, or is deferred with the bot staying down. Nothing here changes
   pricing or sizing; it changes what happens to a quote whose withdrawal the
   exchange never answered.
3. **Owner: implementer.** The slot-eviction pending-skip above (one line,
   mirrors `_pick_eviction_victim:3746`), if the operator wants it before
   relight.
4. **Owner: implementer.** First live run with anything pending: watch
   `withdraw_resolve.reads` == ticks-with-pending, `withdraw_resolve.proven_gone`
   dominating `withdraw_resolve.drained` (the RFQ-gone case), and
   `quote.delete_rate_limited` returning to 0.
5. **Standing:** KILL file remains in place; bot stays DOWN. This work does not
   relight anything.

---
---

# ADDENDUM (same day) — SECOND adversarial gate: B1 (live money) + B2 (throughput)

**Scope.** A second adversarial gate on the rebuild above PoC-proved **two**
blocking findings. Both are fixed here; nothing else was touched.

**Blast radius.** `rfq/lifecycle.py` — `_resolve_withdraw_pending` (one guard),
`on_quote_accepted` (a thin wrapper around the unchanged body, now
`_on_quote_accepted`), `cancel_all` (one keyword argument). `ops/quote_app.py` —
the two terminal `cancel_all` call sites pass the opt-out. **Nothing on
`handle_rfq`, `_price_async`, the pricing pool, the reservation/gate maths, or
the confirm decision itself.** Suite **3008 passed / 3 deselected / 0 failed**
(was 2994/0; +14 new tests).

---

## B1 — LIVE MONEY: the resolver was the only chooser that would REAP and RE-DELETE a MID-CONFIRM quote

**What was broken.** `_resolve_withdraw_pending` built its pending set as
`if state.withdraw_pending_reason is not None` — with **no `state.accepted`
guard**. The other three choosers all refuse an accepted quote (`cancel_all`,
`cancel_quotes_touching`, and `_pick_eviction_victim`'s "not mid-confirm — never
yank"). This one both **reaps** (drops from mirror + exposure) and **re-DELETEs**.

**Reachable in one step, out of paths that already exist:**

```
 TTL / RFQ-gone DELETE ── 429 ──► quote is UNKNOWN and STILL RESTING
                                        │
                          the taker ACCEPTS it (it was never off the wire)
                                        │
              on_quote_accepted: accepted = True, then AWAITS
              (store write + reservation + candidate MC + confirm REST)
                                        │
                     ...spanning several 0.5 s maintenance ticks...
                                        │
              ┌─────────────────── the resolver's ONE read ───────────────┐
              │  quote ABSENT from open list  ⇒  "PROVEN gone"  ⇒  REAPED │  ← PoC A
              │  quote PRESENT in open list   ⇒  fresh DELETE issued      │  ← PoC A2
              └───────────────────────────────────────────────────────────┘
```

An ACCEPTED quote is **not open**, so PoC A is the *expected* answer from a
healthy exchange: the resolver books **a quote that filled** as a proven
withdrawal.

**PoCs re-run (`tests/test_withdraw_resolution.py`, TEST 6). Both drive the real
path — a real 429 from a real `_withdraw_and_reconcile`, then a real accept
parked mid-confirm on a held-open confirm REST call:**

| PoC | Guard REMOVED (re-run today) | Guard SHIPPED |
|---|---|---|
| A — accepted quote absent from the open list | `still in _open? False`, `withdraw_resolve.proven_gone = 2` — **a quote that FILLED booked as a proven withdrawal** | `still in _open? True`, `proven_gone = 1` (only the genuinely-gone neighbour), `withdraw_resolve.accepted_deferred = 2` |
| A2 — accepted quote still listed open | `DELETEs issued against a MID-CONFIRM quote: ['q1','q1']` | `DELETEs issued against a MID-CONFIRM quote: []` — while the proven-resting neighbour is still driven (`['q2','q2']`) |

**The inverse hazard — a deferral must not become a strand.** Every branch of the
accept path already ends in `_drop_quote` ("Accepted quotes are no longer open
either way") — confirm, decline, lapse, unreadable side, unknown size. The **one**
escape was an exception between `accepted = True` and the drop: quote_app's
`quote_event_worker` logs it and moves on, so the state would sit accepted +
withdraw-pending **forever** and the guarded resolver would never touch it again.
`on_quote_accepted` is now a thin wrapper that drops the quote on an escaping
`Exception` and re-raises. `BaseException`/cancellation is deliberately NOT caught
— that path is the process stopping, where the startup reconcile rebuilds truth
from the exchange.

**Termination proved by execution, not by argument:**

| Branch | Result |
|---|---|
| confirm COMPLETES (held open across 3 ticks, then released) | deferred while mid-confirm, then `open after confirm : 0`, `pending after next tick : 0`, `positions booked : 1` |
| LAPSE (`accepted_side` unreadable — the deliberate no-confirm) | `open after accept : 0`, `accepted_deferred = 0` on the next tick |
| EXCEPTION mid-confirm (raised from the reservation call, which sits OUTSIDE the confirm try/except) | `open after accept : 0`, `confirm.errored = 1`, exception still propagates to the worker |
| that same branch with the wrapper's drop REMOVED | **`assert 'q1' not in _open` FAILS — the strand reproduces exactly** |

Dropping an accepted quote from the mirror is safe for the reason the accept path
already relies on: once accepted the resting entry is economically dead in every
outcome (confirm ⇒ the fill position replaces it; lapse/decline ⇒ the exchange
voids it; there is no post-accept withdrawal). Position/reservation state is
untouched — it lives in `_executed_states` and the reservation book, owned by the
confirm-timeout reconcile and the `quote_executed` replay.

---

## B2 — THROUGHPUT REGRESSION: `cancel_all` held NON-TERMINAL callers for tens of seconds

**What was broken.** `cancel_all` passed **no `budget_s`**, so
`_withdraw_batch._one` looped `while not budget.try_spend(2)` with
`_remaining_s()` returning `None` — an unbounded wait on the write bucket.
Pre-rebuild (`git HEAD` `lifecycle.py:5582`) this was one `asyncio.gather` of N
deletes ≈ 1 RTT. Five of the seven callers are **not** terminal and run
**inline** on paths that must not stall: `marketdata/feed.py:267
_fire_invalidate` (from `_handle_disconnect` **and** from a seq `_gap` *before*
the resync is even sent), `rfq/intake.py:185 on_channel_lost` (immediately before
`ws.force_reconnect()` on the socket carrying our RFQ flow), the exchange-status
halt, and both `DECLINE_FILL_VELOCITY` sites.

**MEASURED (TEST 7; live bucket 200 tok / 20 tok/s, every DELETE ACKs — so the
hold is PURE PACING. `budget_s=None` reproduces the pre-fix path exactly):**

| n | bucket | `budget_s=None` (pre-fix) | shipped default bound (2.5 s) |
|---|---|---|---|
| 50 | full | 0.0 ms — 50/50 deleted | 0.0 ms — 50/50 |
| 50 | empty | 5,000.0 ms — 50/50 | 2,500.0 ms — 24/50, **26 left pending** |
| 100 | full | 0.0 ms — 100/100 | 0.0 ms — 100/100 |
| 100 | empty | 10,000.0 ms — 100/100 | 2,500.0 ms — 24/100, 76 pending |
| **200** (`risk.max_open_quotes`) | full | **10,000.0 ms** — 200/200 | **2,500.0 ms** — 124/200, 76 pending |
| **200** | empty | **20,000.0 ms** — 200/200 | **2,500.0 ms** — 24/200, 176 pending |

The cliff sits exactly at 100 quotes (the burst a full 200-token bucket buys at
2 tokens per DELETE), reproducing the gate's numbers to the millisecond.

**The fix, with no new literal.** `cancel_all(reason, *, budget_s =
_WITHDRAW_RESOLVE_BUDGET_S)` — the **existing** whole-pass wall bound the
read-resolver's own pass gets every maintenance tick (`= _REPRICE_SWEEP_BUDGET_S
= 2.5 s`; the test asserts that equality, so a future edit cannot slip a number
in). The resolver is exactly who inherits the leftovers, so its budget is the
right unit. Nothing is abandoned: a budget-deferred quote is **never asked**, so
it stays in the mirror marked withdraw-pending (risk keeps counting it, it keeps
consuming `max_open_quotes`) and the 0.5 s resolver proves it gone by READ or
re-DELETEs it — the same "deferred, never abandoned" contract
`cancel_quotes_touching` carries. No unmetered `gather` is reintroduced: the
single `delete_quote` call site behind the token gate is untouched (still
AST-checked).

**The two genuinely terminal callers keep today's behaviour**, passing
`budget_s=None` explicitly — `quote_app.on_halt` (which then `_stop.set()`s) and
the shutdown `finally`. There the wait is *right*: the loops are already
cancelled, so no next tick can inherit a deferred withdrawal, and the pass must
attempt every quote rather than leave live ones resting; the startup reconcile
rebuilds truth from the exchange for anything the exchange never answered.
AST-checked: exactly **2** of the 7 sites opt out (both in `quote_app.py`),
exactly **5** take the bound.

**The WS paths, driven through the REAL objects** (`OrderbookFeed._handle_disconnect`
→ `_fire_invalidate` → the exact `on_invalidate` closure quote_app registers;
`RfqIntake._handle_error(code 25)` → `on_channel_lost` → cancel_all →
force_reconnect), at a 200-quote book with the write bucket **EMPTY** — the worst
case:

| Path | held | then |
|---|---|---|
| feed `_handle_disconnect` → `_fire_invalidate` → `cancel_all` | **2,500.0 ms** (bound 2,500 ms; was 20,000 ms) | resync proceeds |
| intake `_handle_error` → `on_channel_lost` → `cancel_all` | **2,500.0 ms** (was 20,000 ms) | `force_reconnect` fired = True |

---

## Nothing unconfirmed is dropped, on any path

Asserted per-quote in every new test and unchanged from the first cut: after a
bounded pass **every** quote not provably deleted is still in `_open`, still in
`exposure.open_quotes`, and carries `withdraw_pending_reason` (checked across all
6 × 2 wall-time cells and both WS paths), and `deleted + still-open == n` exactly.
The accepted quotes in PoCs A/A2 are likewise kept. The only quotes that leave
the mirror are those holding PROOF (ack / 404 / absent from the exchange's own
open list) or an ACCEPTED quote leaving through the accept path that owns it.

## New tests (14) and new observability

`tests/test_withdraw_resolution.py` — **TEST 6**:
`test_poc_a_an_accepted_quote_is_never_reaped`,
`test_poc_a2_no_delete_is_ever_issued_against_a_mid_confirm_quote`,
`test_the_deferred_accepted_quote_resolves_when_the_confirm_completes`,
`test_the_deferral_terminates_on_every_accept_branch[lapse|confirm_raises]`,
`test_every_withdrawal_chooser_refuses_an_accepted_quote` (AST: all four choosers
guard on `state.accepted`). **TEST 7**:
`test_cancel_all_wall_time_bounded_for_non_terminal_callers` (6 cells),
`test_the_ws_feed_paths_are_not_stalled`,
`test_only_the_terminal_callers_opt_out_of_the_wall_budget`.

| Metric / log | Meaning |
|---|---|
| `withdraw_resolve.accepted_deferred` | pending quotes skipped this tick for being MID-CONFIRM. Expected 0 almost always; a sustained non-zero means a confirm is wedged. |
| `confirm.errored` + `quote_accepted_errored` | an exception escaped the accept path; the quote was dropped from the mirror so no pending state can strand |

---

## ADDENDUM THROUGHPUT — before/after, same process, same rig

The B2 fix can only *reduce* blocking, but both edits were A/B'd against the
pre-change code path in one process anyway (the "before" arm is the real
pre-change function, not a re-implementation):

| Surface | BEFORE (pre-change path) | AFTER | Δ |
|---|---|---|---|
| PRICING — `handle_rfq` → quotes/min (300 RFQs) | byte-unchanged code | **10,632 quotes/min** | — |
| MAINTENANCE TICK — 200 open, **0 pending** (the steady state, the only place the rewritten pending scan runs) | 15.875 ms/tick (the pre-change one-comprehension scan) | **15.601 ms/tick** | **−274 µs** |
| ACCEPT — `on_quote_accepted` (BEFORE = the raw `_on_quote_accepted`, i.e. the pre-change function) | 19.717 ms | **19.880 ms** | +163 µs (+0.8%, run-to-run noise on a store-write-dominated path; accepts are tens/day) |

`cancel_all` itself is strictly faster on every non-terminal caller (the table
above: 10–20 s → 2.5 s at the live book) and byte-identical on the two terminal
ones.

---

## ADDENDUM NEXT STEPS

1. **Owner: operator — DECISION OWED.** Same relight decision as above; this
   addendum changes nothing about pricing or sizing. B1 is live-money (a filled
   quote could be booked as a withdrawal and a mid-confirm quote could be
   DELETEd) and B2 restores the pre-rebuild non-terminal latency, so both belong
   in the build that relights.
2. **Owner: implementer.** First live run: watch `withdraw_resolve.accepted_deferred`
   (should be ~0, and never sustained), `confirm.errored` (should be 0), and
   `quote.delete_deferred` on a feed-resync/channel-lost event — a non-zero
   deferral there is now EXPECTED and is drained by `withdraw_resolve.*` on the
   following ticks.
3. **Owner: implementer (unchanged, still filed).** The EV slot-eviction chooser
   `_maybe_evict_lower_ev_slot` still does not skip withdraw-pending quotes.
   Out of scope for this gate.
4. **Standing:** KILL file remains in place; bot stays DOWN.
