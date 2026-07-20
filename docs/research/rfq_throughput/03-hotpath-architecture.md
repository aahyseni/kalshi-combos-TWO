# 03 — Hot-path architecture review (Lens 3): where RFQ throughput actually goes

**Date:** 2026-07-16 (evening). **Repo state:** `6d0f933` working tree.
**Scope:** code-level map of the live quote pipeline (`src/combomaker`), every
serialization point, every per-RFQ recomputation, decline ordering, batching/
dedupe opportunities, and concurrency limits — each with file:line evidence, a
fresh live measurement, expected throughput effect, and a staleness/fail-closed
(hard rule 6) risk analysis. **RESEARCH ONLY — no live module was touched.**

All DB numbers re-derived read-only from `data/combomaker-prod-live-wc.sqlite3`
(`file:...?mode=ro`, uri=True, timeout=5) via
[`scripts/hotpath_tape_stats.py`](scripts/hotpath_tape_stats.py); log numbers
from `data/live_logs/live_20260716_*.log`. **Measurement window** unless noted:
the current run `live_20260716_waiver_tiers_corners45.log`,
2026-07-16T17:30:58Z → 18:13:22Z (42.4 min).

---

## 1. The pipeline as it actually runs (quote mode)

```
                        KALSHI EXCHANGE
        ┌────────────────────┬──────────────────────────┐
        │ comms WS (firehose)│ book WS (dedicated)      │  REST (300/300 adv. tier)
        ▼                    ▼                          ▼
┌───────────────┐   ┌───────────────┐        ┌────────────────────────┐
│ ws.py read    │   │ ws.py read    │        │ create/delete/confirm   │
│ loop: enqueue │   │ loop: enqueue │        │ + metadata + polls      │
│ ONLY (:292)   │   │ ONLY          │        └────────────────────────┘
└──────┬────────┘   └──────┬────────┘
       ▼ 20k queue (:59)   ▼ 20k queue
┌───────────────┐   ┌───────────────┐
│ S1 dispatch   │   │ S2 dispatch   │  ← ONE task per socket (ws.py:333)
│ task (FIFO)   │   │ task (FIFO)   │
└──────┬────────┘   └──────┬────────┘
       │ rfq_created              │ orderbook snapshot/delta
       ▼                          ▼
┌────────────────────┐   ┌──────────────────┐
│ intake pre-parse   │   │ OrderbookFeed    │
│ prefix gate        │   │ mirrors (feed.py)│
│ (intake.py:101-111)│   └──────────────────┘
│ drop ~83% cheap    │
└──────┬─────────────┘
       │ Rfq.from_ws (Decimal parse, allowed combos only)
       ▼
┌────────────────────────────┐  drop-oldest on overflow
│ S3 rfq_work queue, 32 deep │  (quote_app.py:659,:696-713)
└──────┬─────────────────────┘
       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ S4 8 async rfq_workers (quote_app.py:650,:677-694) — ONE event loop │
│  handle_rfq wrapper (quote_app.py:668-675):                         │
│   1 store.record_rfq  (json.dumps raw on caller path, persist:319)  │
│   2 _ensure_watched   (REST metadata on FIRST leg sighting :1417)   │
│   3 lifecycle.handle_rfq (lifecycle.py:1689):                       │
│      a filter.evaluate         cheap, in-memory (filters.py:55)     │
│      b _price_async            ← THE EXPENSIVE STEP                 │
│         prefix: classify_legs + beliefs   (engine.py:284-311)       │
│         joint:  memo hit inline  ────────────────┐                  │
│                 memo miss → S5 JointPool         │                  │
│         suffix: grid/caps/markup/construct (engine.py:313-369)      │
│      c _risk_qty + _quoting_policy (book snapshot #1, lcy:2567)     │
│      d limits.check (book snapshots #2+#3, limits.py:415,:424)      │
│      e create_quote POST (REST RTT, awaited inline, lcy:1769)       │
└─────────────────────────────────────────────────────────────────────┘
        │ memo miss                              ▲ every 0.5s
        ▼                                        │
┌──────────────────────┐            ┌────────────────────────────────┐
│ S5 JointPool: 8 PROC │            │ S8 maintenance loop            │
│ workers, 2.0s        │            │ (quote_app.py:1444): heartbeat │
│ deadline             │            │ + lifecycle.maintenance_tick   │
│ (quote_app:112,119;  │            │   → REPRICES EVERY open quote  │
│ pricing_pool:187-212)│            │     EVERY tick (lcy:2424-2443) │
└──────────────────────┘            └────────────────────────────────┘
┌──────────────────────┐            ┌────────────────────────────────┐
│ S6 quote_event_worker│            │ S7 BookRiskPool: 1 PROC worker │
│ single FIFO task     │            │ shared by maintenance full-book│
│ (quote_app:747-763)  │            │ MC + confirm candidate MC +    │
│ accepts/confirms     │            │ last-look waiver (qapp:488)    │
└──────────────────────┘            └────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│ S9 Store writer: single task, 200k queue, batch commit,          │
│ manual checkpoint per ~5000 writes (persistence.py:220-291)      │
└──────────────────────────────────────────────────────────────────┘
```

### Serialization points

| # | point | file:line | what serializes | measured pressure |
|---|-------|-----------|-----------------|-------------------|
| S1 | comms WS single dispatch task | `exchange/ws.py:333-345` | ALL rfq_created/deleted + quote events, in FIFO | 172,249 rfq_created + 160,827 rfq_deleted in 1,075 s (fixed run final metrics, log ts 14:58:35Z) ≈ **310 msg/s sustained** |
| S2 | book WS dispatch task | own `WsManager` (`quote_app.py:352-355`) | book deltas only (the 2026-07-14 split) | 11,823 deltas/18 min in fixed run — light |
| S3 | `rfq_work` queue, 32 deep, drop-oldest | `quote_app.py:659,:696-713` | burst absorption | `rfq.evicted_oldest_for_fresh` 895, `skipped_stale_in_queue` 17 (fixed run) |
| S4 | 8 async workers on ONE loop thread (GIL) | `quote_app.py:650` | all prefix/suffix/filter/snapshot/memo CPU + POST awaits | see §2, §3 |
| S5 | JointPool 8 processes, 2.0 s deadline | `quote_app.py:112,119`; `ops/pricing_pool.py:187-212` | cold joints only | 38,472 calls / 2,745 timeouts (7.1%) this run (`pricing_stats` 18:11:45Z) |
| S6 | single quote-event worker (deliberate: per-quote ordering) | `quote_app.py:739-763` | confirms/executes | rare (7 accepts in fixed run) — fine |
| S7 | BookRiskPool **1** worker process | `quote_app.py:488`; `ops/pricing_pool.py:535` | full-book MC (every ~15 s) *and* confirm candidate MC *and* waiver enumeration | dwell measured (`last_candidate_dwell_ms`, pricing_pool.py:547) but 3s-confirm can queue behind a 20k-sample book MC |
| S8 | maintenance loop 0.5 s | `quote_app.py:1444-1455`; `rfq/lifecycle.py:2373-2443` | heartbeat + candidate-free limits check + **reprice scan of every open quote** | up to 60 open quotes (config `max_open_quotes: 60`, `config/prod-live-wc.local.yaml:273`) ⇒ up to ~120 price calls/s |
| S9 | store writer task | `ops/persistence.py:220-291` | tape writes, batched | bounded queue 200k, drop-only-on-overflow — healthy |

---

## 2. Fresh measurements (the denominator for every claim below)

Window 17:30:58→18:13:22Z (42.4 min), current run, via
`scripts/hotpath_tape_stats.py`:

| metric | value | source |
|--------|-------|--------|
| combo RFQs recorded (post-prefix-gate) | **66,139** (26.0/s avg) | `rfqs` table window count |
| unique (collection, legs, sides) signatures | **7,016 → 9.4× duplication**; top combo re-RFQ'd **1,271×** | same query |
| quotes sent | **8,008** | `decisions kind='quote_sent'` |
| no_quote rows | **93,187** (> RFQ count: `retry_pending` re-attempts + reprice re-quotes re-record) | `decisions kind='no_quote'` |
| — died cheap at filter (pre-pricing) | 9,090 (**9.8%**) | stage categorization (script §2) |
| — **fully priced, then risk-cap declined** | **75,569 (81.1%)** | same |
| — died in pricing (deadline/classifier/…) | 4,400 (4.7%) | same |
| — priced + risk-cleared + **POSTed to a dead RFQ** (`skip_rfq_closed` only) | 4,128 (4.4%) | same |
| joint memo | hit rate **0.7176** (90,834 hits / 35,743 misses), size 33,623 | `pricing_stats` log 18:11:45Z |
| joint pool | 38,472 calls, **2,745 timeouts (7.1%)**, 0 errors | same |
| seen→quote_sent latency | **p50 0.130 s, p90 1.475 s, p99 2.046 s, 2.0% > 2 s** (n=8,008) | script §4 |
| quote deletions | delete_rfq_gone 5,105 (**64%**) / delete_ttl_expired 2,755 (**35%**) | `decisions kind='quote_deleted'` |
| total joint lookups (hits+misses) | 126,577 / ~41.5 min ≈ **50.8 price calls/s** vs 26 RFQ/s inflow — the ~2× surplus is the maintenance reprice scan + retries | `pricing_stats` + rfqs window |

Earlier-today runs (context for load ceiling):

| run | evidence | source |
|-----|----------|--------|
| `live_20260716_fixed.log` (14:40:40→14:58:35Z, 1,075 s) | `ws.msg.rfq_created` 172,249 (**160/s** sustained firehose), `rfq.dropped_series_fastpath` 142,870 (83%), `rfq.created` 29,379, `quote.sent` **738**, `quote.rfq_closed_before_post` **27,364** ⇒ **97.4% of 28,102 POSTs wasted** | final `quote_app_stopped` metrics snapshot, log ts 14:58:35Z |
| `live_20260716_caps100_dir40_corners3.log` (ended 13:17, no clean stop) | memo LRU **saturated at 65,536**, hit rate 0.6818, pool 119,743 calls / 9,087 timeouts (7.6%) | last `pricing_stats`, log ts 17:16:59Z |

### Prior-claim verification (never trust without re-deriving)

| prior claim | verdict | fresh number |
|-------------|---------|--------------|
| 89,863 `rfq_closed_before_post` in one run (handoff §4C) | metric + handling verified; **line refs drifted**: emit is now `lifecycle.py:1780` (handling block :1774-1786), not ~1419-1426 | my runs: 27,364 (fixed run) and 4,128 rows (current window) — magnitude regime-dependent (caps now decline most flow before POST) |
| deletions 55% rfq_gone / 35% ttl | **confirmed shape** | 64% / 35% current window; 62% / 36% fixed run |
| quote latency p50 0.26 s, 9% > 2 s | **superseded** (that was an earlier run) | p50 0.130 s, 2.0% > 2 s now; `POOL_DEADLINE_S=2.0` (`quote_app.py:119`) explains the p99 pile-up at 2.046 s |
| 368 pool timeouts / 6 min | same order | current: ~22/30 s ≈ 44/min steady; caps100 run cumulative 9,087 |
| 47.7 RFQ/s firehose | **not re-derivable as a log metric** (the "47.7" hits in the logs are incidental audit-line values) | my sustained figure: 160 rfq_created msg/s + ~150 rfq_deleted msg/s over 18 min (fixed run) |
| 12,456 re-RFQs of one combo | shape confirmed at window scale | 9.4× dup factor; top signature 1,271 repeats in 42 min |
| ~87% of RFQs never trade for anyone | not re-derived here (Lens 1/2 scope) | — |

⚠ Tape caveat: today's tape 14:00→17:16Z contains reason codes
(`skip_quote_timed_out` 4,662, `skip_post_paced` 370) that **do not exist at
HEAD** (`core/reasons.py` has no such members) — the quarantined experimental
build (report `2026-07-16-quarantine-external-llm-and-checkpoint-restore.md`)
wrote them. All headline numbers above are restricted to the current-run window
(17:30Z+) which is HEAD code.

---

## 3. Findings — ordered by expected throughput effect

### F1. We price everything, then throw 81% of it away on book-level risk caps — the cheap rejects run LAST  ⚡ biggest lever

**Evidence.** `handle_rfq` order (`rfq/lifecycle.py:1689-1766`): filter →
`_price_async` (the expensive joint) → `_risk_qty` → `_quoting_policy` →
`limits.check` → POST. In the current window **75,569 of 93,187 no-quotes
(81.1%) were fully priced first and then declined** by `limits.check` breaches
(top rows-with-reason: `skip_game_loss_cap` 56,538, `skip_max_open_quotes`
49,168, `skip_mass_acceptance_breach` 23,627, `skip_per_combo_loss_cap` 16,946,
`skip_slate_cap` 9,317). Most of these caps don't need the price:

- `skip_max_open_quotes` (`risk/limits.py:415-422`) is a **pure count check**
  (`open_quotes + 1 > max`), candidate-independent given `adding_quote=True`.
- The game/slate/mass/notional caps breach on aggregates that **include games
  the candidate doesn't even touch** (`limits.py:465-473` and `:597-599`
  iterate ALL games in the snapshot) — so once ONE game is over its cap, every
  candidate breaches, price or no price.
- The exposure fold is documented **monotone** (E2 mass-acceptance dominance,
  `risk/exposure.py:888-896`; handoff §4A: the analytic bound is deliberately
  comonotone-overstated and never decreases when a position is added), so
  *"already breached without the candidate"* ⇒ *"breached with any candidate"*
  is a sound pre-pricing decline.

**Opportunity.** A candidate-free pre-check before `_price_async` — the exact
call already exists on the maintenance tick (`lifecycle.py:2383-2393`,
`limits.check(...)` with no `candidate_positions`) — cached per
(`exposure.generation`, short time bound) and consulted first. Everything that
would breach on a candidate-independent/monotone cap declines before the
prefix/joint/pool/POST.

**Expected effect.** −~81% of pricing work in the current caps regime
(75,569 avoided prices / 42 min ≈ 30/s of loop+pool work freed); pool
utilization drops → the 7.1% deadline-timeout tail (`skip_price_deadline`
2,798) shrinks → fresh quotable RFQs price faster → fewer `skip_rfq_closed`.

**Risk / staleness.** Fail-closed is *strengthened* (it only ever declines
earlier). Three correctness constraints: (a) only **enforced** breaches may
pre-decline — shadow breaches are dropped by `partition_breaches`
(`lifecycle.py:1755`, wired at `quote_app.py:563-568`); (b) only **monotone**
caps qualify (max_open_quotes, already-over game/slate/mass/notional/backstop;
NOT the candidate-EV/CVaR credit paths); (c) the confirm-path waiver
(`LifecycleConfig.lastlook_mc_waiver_enabled`, `lifecycle.py:204-217`) is
confirm-only, so quote-time pre-declines can't bypass it. Watchdog semantics
(`_note_watchdog`, `lifecycle.py:1667`) must still see the would-be decline.

### F2. Mid-pipeline RFQ liveness is never checked — we spend pool budget and REST writes on RFQs we already KNOW are dead

**Evidence.** `intake.open_rfqs` tracks liveness (populated at
`rfq/intake.py:122`, popped on `rfq_deleted` at `:129`), but the lifecycle
never consults it: between dequeue and POST (queue dwell ≤1.5 s + pool ≤2 s +
risk checks), a delete that already arrived is ignored, and
`lifecycle.on_rfq_deleted` (`lifecycle.py:2213-2219`) only cancels
already-posted quotes. Result: fixed run **27,364 rfq_closed POSTs vs 738
accepted posts (97.4% of write budget wasted)**; current window 4,128 (34% of
12,136 POST attempts). Each wasted POST also holds one of the 8 async workers
for a full REST RTT and burns the ADVANCED-tier write budget (300/s).

**Opportunity.** Check `rfq_id in intake.open_rfqs` (or a liveness callback) at
three points: on dequeue (before the prefix), after the pool joint returns, and
immediately before `create_quote`. Plus: `on_rfq_deleted_cleanup`
(`quote_app.py:736`) already pops `pending`, but nothing purges the same rfq_id
from the **`rfq_work` queue**.

**Expected effect.** In firehose regimes (fixed run) this reclaims ~25 wasted
POSTs/s of REST budget and worker time (8 workers × ~150-300 ms RTT each ⇒ the
worker pool's POST ceiling is only ~30-50 POSTs/s — see F7); in the current
regime it kills ~4k wasted round-trips/42 min.

**Risk / staleness.** None to correctness — a deleted RFQ can never fill;
declining it earlier is strictly fail-closed. The one-sided race (delete
arrives during our POST) still lands as `rfq_closed` and is handled
(`lifecycle.py:1774-1786`).

### F3. The maintenance loop re-prices EVERY open quote EVERY 0.5 s — half of all pricing calls are the reprice scan

**Evidence.** `maintenance_tick` (`rfq/lifecycle.py:2424-2443`): for every
open, unaccepted quote it calls `_price_async(state.rfq)` **each tick** (0.5 s
cadence, `quote_app.py:1446`) to compare fair vs `reprice_threshold_cc` (1¢,
`config` via `LifecycleConfig:140`). With `max_open_quotes: 60`
(`config/prod-live-wc.local.yaml:273`) that is up to ~120 price calls/s —
observed total joint lookups run at **50.8/s vs 26 RFQ/s inflow** (≈2×). Warm
memo hits are cheap but every belief tick turns the whole open book into pool
misses at once (burst coupling: book delta → N quotes reprice cold
simultaneously).

**Opportunity.** Event-driven reprice: only reprice quotes whose leg books
actually changed since the quote's `leg_mids_cc` snapshot
(`OpenQuoteState.leg_mids_cc`, `lifecycle.py:241`; `_marginals` at `:2616-2623`
is a cheap peek) — a mid-comparison is ~µs vs a full `price()`; or lengthen the
scan to 1-2 s.

**Expected effect.** Cuts ~half of all pricing calls (and the correlated
cold-burst pool spikes) in quiet books; frees the memo/pool for fresh RFQs.

**Risk / staleness.** This is the one lever with a real staleness trade: a
resting quote on a moved book gets pulled later. Bounded by (a) the unchanged
20 s TTL (`quote_app.py:128`), (b) **last-look at confirm** re-checks leg/joint
moves + freshness before any fill (`decide_confirm`, `lifecycle.py:1915-1916`,
policy `risk/lastlook.py`), which is the actual fail-closed backstop (hard rule
6 survives — a stale accepted quote is declined at confirm, never filled
blind); (c) the mid-comparison trigger is *exactly* the same signal the reprice
compares today. The residual exposure is adverse selection on the resting
quote between book-move and pull — the same exposure the current 0.5 s scan
has, extended by the detection delta only if the trigger misses.

### F4. Joint memo misses are belief-float churn, not new combos — the key is exact to the last bit

**Evidence.** Memo key = exact `(p, uncertainty)` floats per leg
(`pricing/engine.py:423-432`); beliefs are top-of-book **microprices**
(`lifecycle.py:2616-2623` → `KalshiBookSource`), which move on any size change
at top of book even when price levels don't. Result: 9.4× RFQ duplication but
only 71.8% memo hit rate; in the caps100 run the LRU **saturated at 65,536**
with hit 0.68 (`pricing_stats` 17:16:59Z) — the same combos cycling through
belief-perturbed keys. Every miss is a process-pool round trip (serialize Rfq +
beliefs + Relationship, `pricing_pool.py:187-208`) of a multi-hundred-ms joint.

**Opportunity (operator decision — pricing change).** Quantize the *beliefs
themselves* (e.g. to 1e-4 ≈ 1 cc) before the joint so key == inputs and a hit
is still exact for what was computed; the ≤0.5 cc/leg input perturbation is
bounded and can be covered by the existing uncertainty term. Alternative
without pricing change: key on top-of-book *price levels* per leg instead of
microprice floats — coarser, still exact-invalidating on a real price move.

**Expected effect.** Hit rate 72% → 90%+ (the duplication factor supports it);
pool call volume down ~3-4×; `skip_price_deadline` and pool-dwell tail shrink
proportionally.

**Risk / staleness.** A real pricing change (hard rule 8: prototype in
`tools/`, parity-check to the cent on the same inputs, backtest before port).
Never do silently: the memo's current EXACTNESS guarantee
(`engine.py:65-78`) is a stated invariant. Fail-closed unaffected (a missing
book still returns None → no-quote).

### F5. Three full book decompositions per RFQ — one of them just to count quotes

**Evidence.** Per admitted RFQ the loop builds the full exposure snapshot
(iterate every position + every open quote × legs × marginals,
`risk/exposure.py:881-975`) **three times**: `_quoting_policy`
(`lifecycle.py:2567`), then `limits.check` twice —
`book.snapshot(...).open_quote_count` at `risk/limits.py:415` (an entire
decomposition consumed for a **len()**) and the mass-acceptance snapshot at
`:424-426`. At ~30 handle_rfq/s with 60 open quotes × 2-12 legs this is
hundreds of thousands of marginal lookups + dict folds per second, all on the
single loop thread.

**Opportunity.** (a) Replace the count-snapshot with a direct count; (b) cache
the candidate-free decomposition keyed on (`exposure.generation`
(`exposure.py:786-813`), marginal epoch/time-bound ≤ maintenance cadence) and
fold the candidate incrementally — the P0-2 generation counters already exist
for exactly this invalidation discipline.

**Expected effect.** O(book × legs) → O(candidate legs) per RFQ; frees loop CPU
(the same thread that must beat the 0.5 s heartbeat under load — the 2026-07-15
15 s supervisor-kill class).

**Risk / staleness.** The snapshot reads live marginals; a cached decomposition
is stale within its epoch. Bounded: caps compare against slow-moving aggregates
and the maintenance tick re-checks the book every 0.5 s
(`lifecycle.py:2382-2393`); an epoch ≤0.5 s is no staler than today's
maintenance-halt granularity. UNKNOWN-marginal fail-closed semantics
(`exposure.py:927-937`) must key the cache too (a leg going unreadable
invalidates).

### F6. 9.4× re-RFQ duplication is absorbed per-layer, never at the front door

**Evidence.** 66,139 RFQs / 7,016 unique signatures in 42 min; top signature
re-RFQ'd 1,271×. Each duplicate pays: `record_rfq` with full `raw_json`
serialization on the caller path (`persistence.py:295-321`, `json.dumps` at
`:319`), `_ensure_watched` scan, filter, prefix (uncached `classify_legs`,
F8), memo lookup, suffix construction, 3 snapshots, limits check, POST.
`has_open_quote` keys on `rfq_id` (`lifecycle.py:2464`), never on the combo
signature, so a re-RFQ of a combo we're already resting on is a full re-quote.

**Opportunity.** (a) Signature-level dedupe in the queue: when a new RFQ
arrives whose signature matches a QUEUED one, supersede the older *only if the
older is already deleted* (combine with F2's liveness check — replace-spam
takers delete the old RFQ first, which is why 64% of our quote deletions are
`delete_rfq_gone`); (b) a short-lived ConstructedQuote memo keyed on
(signature, belief epoch, grid) capturing suffix work too — the joint memo
already carries the expensive part, so this is a smaller increment.

**Expected effect.** Queue dwell down during spam bursts; wasted end-of-pipe
work down proportional to the dup factor on dead predecessors.

**Risk / staleness.** Dropping a LIVE older duplicate trades a small win-chance
for freshness — operator call; dropping a DELETED one is free (fail-closed).
The suffix memo must invalidate on `free_money_caps` inputs (leg book depth,
`engine.py:338-339`), not just microprice — depth changes without mid changes.

### F7. The 8 async workers await the create-quote POST inline — REST RTT caps quoting throughput

**Evidence.** `handle_rfq` awaits `self._sender.create_quote(...)` on the
worker (`lifecycle.py:1768-1773`); with `RFQ_WORKERS = 8` (`quote_app.py:650`)
and a ~150-300 ms POST RTT, the theoretical posting ceiling is ~27-53 POSTs/s
— the fixed run *measured* 26.1 POST attempts/s while 97.4% of them were
`rfq_closed`, i.e. the workers were saturated posting to dead RFQs. Workers are
also held by pool dwell (≤2 s) and first-sighting metadata fetches
(`quote_app.py:1417-1423`).

**Opportunity.** Raise `RFQ_WORKERS` (async tasks are near-free; the CPU-bound
part is already off-loop) and/or split the POST into a small sender stage so
pricing workers never block on the wire. Pace against the 300/s write budget
(the `RateLimitRecordingSender` already counts 429s, `quote_app.py:210-260`).

**Expected effect.** Removes the posting ceiling during bursts; with F1+F2 the
same 8 workers may suffice — measure after those land.

**Risk / staleness.** More in-flight quotes concurrently: the reservation
service is single-writer and built for concurrent RFQs
(`quote_app.py:558-568`), and per-quote event ordering is preserved by the
single quote-event worker (S6). Rate-limit breaker must stay wired (it is:
create/delete/confirm all record 429s).

### F8. Uncached pure classifiers run per price call — `classify_legs`, `classify_leg`, `classify_sport`

**Evidence.** `_price_prefix` runs `classify_legs(rfq.legs, metadata)` on every
price call (`engine.py:293`) — regex ticker parsing + per-event grouping over
up to 12 legs (`relationships.py:442+`), ~50.8 calls/s currently, with **zero
caching**; `classify_leg` / `classify_sport` (`pricing/legtypes.py:148,:214`)
are pure regex functions with no `lru_cache` (contrast: `dixon_coles.py:263,282`
and `mlb_runs.py:43` do cache). `is_single_family_no_basket` re-classifies
every leg again in the suffix (`engine.py:92-103`).

**Opportunity.** `lru_cache` on `classify_leg`/`classify_sport` (ticker-pure);
a bounded relationship cache keyed on (leg tickers+sides+event tickers,
metadata fingerprint) — the settlement-fingerprint machinery already exists
(`quote_app.py:1698-1737`) for invalidation.

**Expected effect.** Shaves loop-thread ms/RFQ (largest on 8-16-leg baskets);
small individually, meaningful because it's on the same GIL thread as the
heartbeat.

**Risk / staleness.** Ticker-pure caches are risk-free. The relationship cache
must invalidate on event-metadata change (mutual-exclusion detection reads
metadata) — fingerprint-keyed or metadata-generation-keyed; UNKNOWN results are
cacheable only with the same key discipline (an UNKNOWN from a *missing*
metadata peek must NOT be cached, or it outlives the fetch that would fix it).

### F9. The structural fit re-inverts ~2N+7 times per cold combo with no cross-call warm start

**Evidence.** `structural.py:_price` (`:348-485`): base `invert` (`:401`), then
per-leg band re-inversions (2 × N legs, `:406-419`), dc_rho band (2), ET band
(2, knockout), half-share band (2, if 1H legs), pens band (2, advance legs) —
each a full least-squares solve. The warm start exists only WITHIN one call
(`warm`, `:377,:402`); the same game's next combo (or the same combo at the
next belief tick) starts cold. Same pattern in `_price_margin_total`
(`:574-606`) and `_price_mlb` (`:682-706`). The pool worker's engine is
per-process with memo disabled (`pricing_pool.py:81-97`), so nothing is shared
across the 8 workers either. This is the per-game-per-tick fit-inversion
recomputation the review brief asked about — the joint memo dedupes exact
repeats, but every belief tick is a full cold re-fit.

**Opportunity.** Worker-local per-game warm-start cache: keep the last
converged `(lam_a, lam_b)` (soccer) / `(mu_m, mu_t)` (MT) / `(mu_a, mu_b)`
(MLB) keyed on the game code (`_parse_match`, `:153-160`) and feed it as
`warm_start`. A converged solve is a fixed point of the constraints — the warm
start changes iteration count, not the solution (within solver tolerance).

**Expected effect.** Cuts inversion iterations across the dominant same-game
re-quote flow → lowers the cold-price tail → fewer of the 7.1% pool deadline
timeouts. (The handoff §4C "cache the per-game structural fit" durable fix,
made concrete.)

**Risk / staleness.** None to freshness (the fit still targets the CURRENT
beliefs; only the solver's starting point is cached). Must pass the pool/memo
parity harness to the cent (`tools/pool_parity_check.py` pattern, hard rule 8);
a per-game cache in 8 separate processes is 8 caches — fine, it's only a hint.

### F10. One BookRiskPool worker serves three masters inside the 3 s confirm window

**Evidence.** `BookRiskPool(workers=1, ...)` (`quote_app.py:488`;
`ops/pricing_pool.py:535`). The maintenance full-book MC (20k samples, every
~15 s via `_maybe_recompute_book_risk`, `lifecycle.py:2339-2371`), the
confirm-path candidate MC (`run_candidate`, `pricing_pool.py:592-622`), and the
last-look waiver enumeration (`run_state_worst_case`, `:624-648`) share the one
process. A confirm that lands while the book MC is mid-flight queues for the
worker; dwell is *measured* (`last_candidate_dwell_ms`, `:547`) and the
candidate gate fails closed on its deadline (`LifecycleConfig
candidate_gate_deadline_s = 2.0`, `lifecycle.py:191`) — i.e. the failure mode
is a **declined winnable fill**, not a safety hole.

**Opportunity.** `workers=2` (one for maintenance, one effectively reserved for
confirm-path), or defer a pending maintenance MC when a confirm is in flight.

**Expected effect.** Removes a rare-but-expensive loss (a won auction declined
for queueing, at ~tens of accepts/day each worth the full edge).

**Risk / staleness.** None — more process memory only; generation-stamp
discipline (P0-2) is unchanged.

### F11 (observations, smaller / already-handled)

| item | evidence | note |
|------|----------|------|
| intake pre-parse gate works | 83% of firehose dropped on a string check before `Rfq.from_ws` (`intake.py:101-111`; fixed-run metrics) | keep — this is the front-door shed |
| store writer is off-hot-path & bounded | `persistence.py:220-291` (batch commit, manual checkpoint per 5k, drop-on-overflow) | `json.dumps(rfq.raw)` still runs on the caller path (`:319`) at 26/s — micro |
| `retry_pending` re-prices declined RFQs | `quote_app.py:715-734`: ANY un-quoted RFQ enters `pending` (`:675`) and is retried ≤5×/2 s — under binding caps this is pure re-pricing of certain declines | F1's pre-check also fixes this branch |
| dispatch queue sizing | 20k bound with discard-on-reconnect (`ws.py:59,:272-279`) — the boot-snapshot overflow loop is fixed | — |
| memo LRU size | 65,536 saturated in the caps100 run (`engine.py:78`) | with F4 the same LRU covers far more flow; without F4, bump costs only memory |

---

## 4. Concurrency limits summary

| resource | limit | file:line | binding today? |
|----------|-------|-----------|----------------|
| event loop thread | 1 (GIL) | — | YES: prefix/suffix/filter/3×snapshot/memo + all dispatch at 50.8 price-calls/s |
| RFQ workers | 8 async tasks | `quote_app.py:650` | YES under bursts (POST RTT + pool dwell hold them; F2/F7) |
| joint pool | 8 procs, 2.0 s deadline | `quote_app.py:112,119` | 7.1% timeout tail; would un-bind with F1+F3+F4 load shed |
| book-risk pool | 1 proc | `quote_app.py:488` | rare confirm-path queueing (F10) |
| REST writes | 300/s (ADVANCED tier) | `RateLimitRecordingSender` `quote_app.py:210` | wasted, not exhausted: 97.4% of POSTs dead in firehose regime (F2) |
| rfq_work queue | 32, drop-oldest | `quote_app.py:659` | 895 evictions in the 18-min fixed run |
| dispatch queues | 20,000/socket | `ws.py:59` | healthy since the enqueue-only read loop |

## 5. Recommended sequencing (throughput per unit of risk)

1. **F2 liveness checks** (zero pricing/risk semantics change, pure waste removal)
2. **F1 monotone pre-pricing risk gate** (same decline outcomes, earlier; biggest single lever at −81% of priced work under binding caps)
3. **F5 snapshot count fix + candidate-free decomposition cache** (loop CPU)
4. **F3 event-driven reprice** (staleness bounded by last-look; watch markouts)
5. **F9 structural warm-start cache** (parity-gated)
6. **F10 book-risk pool workers=2**, **F7 more workers/sender stage**, **F8 classifier caches**
7. **F4 belief quantization** last — it is a *pricing* change (operator decision, backtest + parity per hard rule 8)

Every item above must keep: UNKNOWN ⇒ no-quote (hard rule 6), enforced-vs-shadow
breach partition, last-look confirm gates, and the reason-code tape (the
research denominator).

---

## NEXT STEPS

- **Owner: next research session** — instrument the split of the 50.8/s price
  calls (new-RFQ vs maintenance-reprice vs retry) with a counter tag; today it
  is inferable only by subtraction (F3's sizing depends on it).
- **Owner: implementation session (NOT this research pass)** — prototype F2+F1
  in `tools/` against the tape (hard rule 8: test-script first, then port +
  parity), targeting the same-decline-different-stage invariant: identical
  reason codes, earlier exit.
- **Owner: operator decision** — F4 belief quantization (pricing change: pick a
  quantum, backtest med/edge deltas, cover with width) and F6 live-duplicate
  dedupe policy (drop-live-older vs quote-all).
- **Owner: operator** — confirm whether the `skip_quote_timed_out` /
  `skip_post_paced` reason codes from the quarantined build should be excluded
  from all longitudinal tape analyses (they pollute 14:00-17:16Z today).
- Cross-reference: Lens 1/2 docs in this folder for the flow-mix and
  fill-quality denominators; handoff `docs/reports/2026-07-15-HANDOFF-for-llm-review.md`
  §4C line refs are stale (rfq_closed handling now `lifecycle.py:1774-1786`).
