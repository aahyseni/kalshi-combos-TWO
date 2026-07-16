# 02 — LENS 2: Where the time goes (RFQ latency anatomy)

**Date:** 2026-07-16 (analysis run ~18:25 UTC).
**Scope:** logs `data/live_logs/live_20260716_{fixed,caps100_dir40_corners3,waiver_tiers_corners45}.log`,
live DB `data/combomaker-prod-live-wc.sqlite3` (read-only), shadow DB
`data/combomaker-prod.sqlite3` (read-only), source at HEAD `6d0f933`.
**Scripts (re-runnable):** `docs/research/rfq_throughput/scripts/{log_latency_anatomy,db_latency_anatomy,db_funnel_followup}.py`
— every number below is reproduced by one of these three read-only scripts unless a
file:line or report is cited instead.

> ⚠ **OPERATOR ALERT (found in passing):** the newest live run
> (`live_20260716_waiver_tiers_corners45.log`) was **emergency-killed at
> 2026-07-16T18:13:22Z** (`supervisor_heartbeat_wedged` 18:13:20 →
> `supervisor_emergency_kill` → `halt_kill_file`). As of 18:25 UTC no newer live
> log exists — **the quote bot is DOWN**. Mechanism in §4 below (it is a
> latency-anatomy finding, not a random crash).

---

## 0. Executive summary

The pipeline itself is fast when it works — **priced+risk+POST is p50 0.13 s** —
and the event loop does NOT stall under the firehose. The flow dies in four
places, in this order of magnitude (today's unique-RFQ funnel, §2):

| # | Where flow dies | Share of today's 426,901 decided allowlist RFQs | Nature |
|---|---|---|---|
| 1 | **POST race lost** (`rfq_closed` on CreateQuote) | **44.4%** (34.9% `skip_rfq_closed` + 9.5% same mode logged as `skip_quote_timed_out` by this morning's code) | Exchange quote-window closes ~0.67 s (median) after RFQ creation; our end-to-end floor is ~0.6–0.7 s |
| 2 | **Own risk caps** (max_open_quotes, game_loss, mass_acceptance, …) | **32.8%** | Self-inflicted; these RFQs were already PRICED (CPU spent) then declined |
| 3 | **Filters** (size, series, classifier, …) | 13.6% | Cheap, by design |
| 4 | **Pool deadline** (2.0 s joint timeout) | 4.4% | 5–8 s structural tail + timeout cascades (§3) |
| — | **Quoted** | **4.8%** (20,546) | |

Plus a bucket **invisible to the DB**: RFQs **evicted from the intake queue before
any decision** — 27.9% of the last run's intake (25,579 of 91,824
`rfq.created`; §2.4).

The single biggest physical constraint: **the exchange decides the auction in the
first ~0.5–1 s, and our wire→worker-pickup latency alone is p50 0.78 s** (floor
0.52 s). The old WS-reconnect staleness wall is **dead** (§5). The heartbeat kills
are **not** loop starvation any more — they are the *maintenance sweep serially
awaiting a frozen pricing pool* (§4).

---

## 1. The pipeline and where each millisecond goes

```
      Kalshi exchange                                 our process (asyncio loop)
 ┌───────────────────────┐      ┌─────────────────────────────────────────────────────────┐
 │ RFQ created_ts        │      │                                                         │
 │   │  publish + WS     │      │  intake fastpath gate (intake.py:103-113)               │
 │   ▼                   │  ws  │  drops non-KXWC/KXMLB: 84.7% of wire                    │
 │ communications feed ──┼──────┼─► on_rfq_enqueue (put_nowait, drop-oldest, 32-deep)     │
 │                       │      │        │   queue dwell (evict >1.5s: quote_app.py:664)  │
 │ quote window closes   │      │        ▼                                                │
 │ ~0.67s median (§2.3)  │      │  rfq_worker ×8 (quote_app.py:650)                       │
 │                       │      │        │  record_rfq → seen_at stamped HERE             │
 │                       │      │        ▼  (persistence.py:295)                          │
 │                       │      │  lifecycle.handle_rfq (lifecycle.py:1689)               │
 │                       │      │    filter.evaluate ───────── ~0-10ms                    │
 │                       │      │    _price_async:                                        │
 │                       │      │      memo hit (68-73%) ───── ~2.6ms inline              │
 │                       │      │      memo miss → JointPool ─ 8 procs, 2.0s deadline     │
 │                       │      │        (quote_app.py:112,119; pricing_pool.py:206)      │
 │                       │      │    risk limits.check ─────── ~ms (analytic)             │
 │  409 rfq_closed ◄─────┼──────┼──  create_quote POST ─────── ~50-100ms RTT              │
 │      (88% of posts!)  │      │        │                                                │
 │                       │      │        ▼                                                │
 │ RFQ deleted (median   │      │  quote_sent (4.8% of decided RFQs)                      │
 │ minutes-hours later)  │      │                                                         │
 └───────────────────────┘      │  maintenance loop (0.5s): beat heartbeat, then          │
                                │    RE-PRICE EVERY open quote via the SAME pool          │
                                │    (lifecycle.py:2431) + TTL deletes via REST           │
                                └─────────────────────────────────────────────────────────┘
```

### Stage timings (today, 2026-07-16, live-DB decisions ⋈ rfqs tape)

| Stage | Metric | p10 | p50 | p90 | p99 | Source |
|---|---|---|---|---|---|---|
| exchange→worker pickup (WS + dispatch + queue dwell) | `created_ts → seen_at`, n=150,000 recent | 0.52 s | **0.78 s** | 1.40 s | 1.69 s | `db_funnel_followup.py` §2 |
| pickup→quote posted (filter+price+risk+POST) | `seen_at → quote_sent at`, n=20,555 today | 0.05 s | **0.13 s** | 1.49 s | 2.20 s | `db_latency_anatomy.py` |
| end-to-end on wins | `created_ts → quote_sent`, today | 0.57 s | **1.25 s** | 2.60 s | 3.78 s | same |
| pickup→losing POST | `seen_at →` first `skip_rfq_closed`, n=149,104 today | 0.04 s | **0.05 s** | 0.44 s | 1.66 s | same |
| exchange window close (implied) | `created_ts →` first `skip_rfq_closed` | 0.52 s | **0.67 s** | 1.62 s | 2.72 s | same |
| one cold `engine.price()` (memo hits are faster still) | offline profile, 450 real tape combos | — | 2.6 ms | 166 ms | ~5.3 s (max 8.2 s) | report `2026-07-14-pricing-throughput-memo` |
| REST RTT proxy | `confirm.rtt_ms` mean 78.6 ms | — | ~0.08 s | — | — | `quote_app_stopped` metrics dump, waiver log 18:13:23Z |

Readings:

- **The wire+dispatch+queue segment (0.78 s median) is ~6× the pipeline segment
  (0.13 s median).** Its floor is 0.52 s (p10) — present even for instant filter
  skips (`skip_size_below_min` created→decision p10 0.55 s) — so ~0.5 s is
  exchange-publish + network + WS dispatch, and the p50→p90 spread (0.78→1.40 s)
  is queue dwell under bursts (dwell is capped at 1.5 s by the eviction rule,
  `quote_app.py:664`, and the p99 of 1.69 s ≈ 0.5 floor + 1.5 cap confirms it).
  Caveat: `created_ts` is Kalshi's clock vs our local clock — absolute skew
  unverified, but the *same* skew is inside every row of both the win and loss
  distributions, so the comparisons stand.
- **The race is decided before we arrive.** Half of the RFQs that 409'd us were
  already closed 0.67 s after creation, while our best case (floor 0.52 s +
  memo-hit pipeline 0.05–0.13 s) lands at ~0.6–0.7 s. We only win the *slow*
  auctions: our successful posts landed at median 1.25 s — the wins are the RFQs
  nobody closed quickly (selection effect, not skill at speed).
- The 2026-07-15 handoff's "quote latency p50 0.26 s, 9% tail over 2 s" is
  **verified** as the all-time `seen_at → quote_sent` distribution: re-derived
  p50 0.25 s, 7.1% > 2 s (n=55,145). Today's tail is better (3.0% > 2 s).

### `rfq_closed` is the QUOTE WINDOW, not the RFQ dying

Cross-check (shadow DB, `db_funnel_followup.py` §3): for 4,319 of today's
closed-at-POST RFQs matched in the recent deletion tape, actual deletion came
**p10 11.8 s, median 6,752 s (≈112 min), p75–p99 ≈ 10,810 s (≈3 h, a server-side
expiry ridge)** after creation. The RFQ object lives for minutes-to-hours; only
its quote-acceptance window closes sub-second. So RFQ *lifetime* (allowlist
median 17.0 s to deletion; §2.2) is irrelevant to the race — **the only clock
that matters is created_ts + ~0.7 s.**
(Exact semantics of `rfq_closed`/409 — first-quote-wins vs requester-accept —
should be confirmed against docs.kalshi.com before building on this; the
empirical timing above is solid either way.)

---

## 2. (a) RFQ lifetime vs our time-to-quote — the funnel

### 2.1 Today's unique-RFQ outcome funnel (live DB, precedence quoted > closed > deadline > risk > filter)

426,901 unique allowlist RFQs received a decision today (482,008 tape rows;
`no_quote` rows 613,017 → retry inflation ×1.5 from the pending-retry loop,
`quote_app.py:665`):

| Outcome | Unique RFQs | Share |
|---|---|---|
| lost POST race (`skip_rfq_closed`) | 148,783 | 34.9% |
| lost POST race, morning-code label (`skip_quote_timed_out` — same detail string "window elapsed before our POST landed"; reason no longer exists at HEAD) | 40,654 | 9.5% |
| own risk caps (priced first, then declined) | 139,884 | 32.8% |
| filters | 58,208 | 13.6% |
| quoted | 20,546 | **4.8%** |
| pool deadline (`skip_price_deadline`) | 18,823 | 4.4% |

Of the raw **wire** (pre-fastpath) flow the quoted share is ~**1.3%** (waiver run
metrics dump: `quote.sent` 8,008 / `ws.msg.rfq_created` 600,222) — this
re-derives the handoff's "~1–2% of incoming flow" and locates it: **84.7% of the
wire is non-sports fastpath-dropped by design** (`rfq.dropped_series_fastpath`
508,398), and of the in-scope remainder we actually **price ~72%**
(quoted+closed+risk-capped all pass pricing) — the loss is the race and our own
caps, **not** pricing coverage.

### 2.2 RFQ lifetime (shadow DB, 30k most-recent deletions 18:11–18:13Z)

| Population | p10 | p25 | p50 | p75 | p90 | p99 |
|---|---|---|---|---|---|---|
| all legs KXWC/KXMLB (n=6,285) | 3.8 s | 8.0 s | **17.0 s** | 29.9 s | 69 s | 4,782 s |
| everything else (n=20,202) | 5.9 s | 12.8 s | 24.1 s | 37.8 s | 251 s | 5,550 s |

Median in-scope RFQ lives 17 s **as an object** — long enough for any pipeline —
but per §1 the quote window inside it closes ~0.67 s in. The wire also carries
`rfq_deleted` at ~224/s (waiver run: 579,221 deletes vs 600,222 creates in 43
min) — requester churn, most RFQs never trade.

### 2.3 Wire rates (verify "47.7 RFQ/s firehose")

| Run (all 2026-07-16) | Wall | `ws.msg.rfq_created` | wire rate | post-gate `rfq.created` | priced-decisions rate (log lines/s) |
|---|---|---|---|---|---|
| fixed (14:40–14:58Z) | 17.9 min | 172,249 | **160/s** | 29,379 (27.3/s) | p50 39/s, peak 86/s |
| caps100 (14:58–17:17Z) | 138.5 min | (no dump — emergency kill) | ~27.5/s post-gate | — | p50 37/s, peak 117/s |
| waiver (17:30–18:13Z) | 43.0 min | 600,222 | **233/s** | 91,824 (35.6/s) | p50 38/s, peak 109/s |

The "47.7 priced-RFQs/s" from `2026-07-16-heartbeat-config-fix...md` is the same
order as our measured per-second `risk_audit`/`inventory_skew_shadow` rates
(p50 37–39/s active, peak 117/s). The handoff's "~170–1500 RFQ/s near kickoff"
is consistent at the wire level (160–233/s average on a NO-kickoff day; kickoff
peaks were not observable today).

### 2.4 The invisible bucket: queue evictions

`rfq.evicted_oldest_for_fresh` (drop-oldest on the 32-deep queue,
`quote_app.py:707`): **25,579 of 91,824 (27.9%) of the waiver run's post-gate
intake was evicted before pricing** (vs 895 = 3.0% in the fixed run). These RFQs
never reach `record_rfq`, so they are absent from every DB funnel — DB-based
flow accounting undercounts intake loss. Eviction spikes when the pool is slow
(workers await the pool → queue backs up), i.e. it is downstream of §3, not an
independent cause.

---

## 3. (b) Pool-timeout anatomy under firehose

**Which pool:** only `JointPool` — 8 worker processes, 2.0 s deadline
(`quote_app.py:112,119`; enforced `pricing_pool.py:206`). The book-risk /
candidate-MC pool (`BookRiskPool`) has NO deadline and is confirm-time only
(3 candidate MCs all day, ~491 ms each in-worker, dwell ≤13 ms — metrics dump
18:13:23Z). It is not a throughput factor.

**What the work is:** `engine.compute_joint` on a memo miss — per-combo
Dixon-Coles structural inversion / copula MVN CDF (`pricing_pool.py:100-108`).
Cold-path cost (450 real tape combos, `tools/profile_pricer.py`, 2026-07-14
memo): p50 2.6 ms, p90 166 ms, **p99/max 5.3–8.2 s** (high-effective-dim n>4 /
structural same-game tail). The fit is combo-specific by design — a per-game fit
cache was evaluated and rejected as no-hit (memo already keys at that
granularity; memo hit rate 68–73% live).

**Timeout counts (verify "368 per 6 min"):** run totals — fixed 723/13,364 calls
(5.4%), caps100 9,087/119,743 (7.6%), waiver 2,866/39,904 (7.2%). Worst ~6-min
windows: **315 (fixed), 755 (caps100, 16:45Z), 694 (waiver, 17:38Z)** — the old
"368/6 min" figure is verified in magnitude and is now routinely *exceeded*.

**The cascade mechanism (the key dynamic):** a timed-out future is abandoned by
the loop but **the worker process keeps computing it to completion**
(`pricing_pool.py:16-18` documents this). A burst of 5–8 s tail combos therefore
freezes all 8 workers on already-abandoned work; every queued call then waits
> 2 s and times out too. Direct evidence — collapse ticks where **every** call
timed out and **zero** joints completed (memo `misses` counts only completed
joints):

```
18:13:01→18:13:16Z (last tick before the kill):
  pool_calls +68, pool_timeouts +68, memo_misses +0, memo_hits +21
earlier: 16:50:44Z tick: 59 calls / 59 timeouts / 0 misses
```

**Who feeds the pool:** not just fresh RFQs. The 0.5 s maintenance sweep
re-prices **every open unaccepted quote** through the same `_price_async`
(`lifecycle.py:2431`) — with 30–60 open quotes that is a standing ~60–120
price-calls/s of demand that competes with fresh flow for the same 8 workers
(mostly memo hits while books are quiet; on book deltas they mass-miss into the
pool exactly when fresh flow also misses).

---

## 4. (c) Event-loop stalls and the heartbeat kills

**The loop itself is healthy under firehose.** Status-loop ticks (15 s cadence,
`quote_app.py:1553`): p99 15.4–15.8 s, max 15.6 s in the waiver run. Global
log-timestamp gaps > 2 s during healthy operation: ≤3.7 s and rare (7 in the
43-min waiver run). The off-loop offload (P2-2/Phase-1) did its job — the
2026-07-15-style inline-CPU wedge did not recur today.

Both of today's kills have *specific, different* mechanisms:

**Kill 1 — caps100 17:17:30Z: teardown join, cosmetic but kill-generating.**
Local DNS outage 17:16:11Z (`gaierror(11001)` on external-api.kalshi.com — our
machine, not Kalshi) → 13 failed WS reconnects → `halt_data_stale` sustained 42 s
→ `kill_switch_halt` 17:16:59Z → `JointPool.shutdown` runs
`executor.shutdown(wait=True)` **on the event loop** (`pricing_pool.py:227`) →
30.6 s log silence (`joint_pool_stopped` 17:16:59 → next line 17:17:30) →
heartbeat aged past 30 s → `supervisor_heartbeat_wedged` + emergency kill +
needs-reconcile marker — all *during an already-orderly halt*.

**Kill 2 — waiver 18:13:20Z: maintenance sweep serially awaiting a frozen pool.**
The maintenance loop beats the heartbeat once per iteration *before* the tick
(`quote_app.py:1444-1455` — deliberately, so a slow tick reads as wedged). The
tick re-prices each open quote sequentially (`lifecycle.py:2426-2444`); with the
pool frozen (§3: 68/68 timeouts) each await burns the full 2.0 s deadline, and
there were **31 open quotes** at the kill (`cancel_all count=31`, 18:13:22Z) →
worst-case ~62 s in ONE tick, plus 254 `delete_quote_failed` REST 404 round
trips in the final 3 min. The heartbeat aged > 30 s while the rest of the loop
kept logging normally (pricing_stats 18:13:16, risk_audit 18:13:17) — i.e. this
was a *starved maintenance task*, not a starved loop. The supervisor then
cancelled our quotes and wrote KILL (`supervisor_emergency_kill`,
`kill_written: true`).

**Batch/store effects:** `store_writer_batch_failed` ("database table is locked",
the WAL-checkpoint contention of handoff §4D) fired 4–10×/run — it stalls only
the tape writer task (`persistence.py:258-290`), never quotes; timestamps are
stamped at enqueue so DB latency numbers are unaffected.

---

## 5. (d) Subscription / leg-staleness losses — the old wall is DEAD

Per-day `no_quote` rows (live DB):

| reason | 07-13 | 07-14 | 07-15 | 07-16 |
|---|---|---|---|---|
| `skip_leg_stale` | 27,514 | 37,971 | **84** | **102** |
| `skip_ws_unhealthy` | 1,184 | 20,631 | 0 | 0 |
| `skip_leg_unknown` | 16 | 197,154 | 72 | 82 |

The ~90–150 s WS-reconnect staleness wall (fixed by the 2026-07-13
receive-timeout fix, report `2026-07-13-ws-churn-root-cause...`) is **no longer a
throughput factor**: ~0.02% of today's decisions. Today's only WS incident was
the 17:16Z local-DNS outage (§4), which correctly halted rather than quoting
stale. The cumulative `skip_leg_stale: 237,975` in the periodic reports is
all-time tape history (mostly 07-13/14), not current behavior.

---

## 6. Verification of the prior claims (never trust, re-derive)

| Claim (source) | Re-derivation | Verdict |
|---|---|---|
| 89,863 `rfq_closed_before_post` in one run (handoff §4C) | Metric dump lost in that run's emergency kill; per-day DB rows: 07-14 139,723 / 07-15 267,755 / 07-16 205,513 (unique RFQs 112,839 / 225,487 / 149,104) | **Plausible** (07-15 had 3 runs summing 267,755); order confirmed, exact number unrecoverable |
| deletions 55% `delete_rfq_gone` / 35% `delete_ttl_expired` | 07-14: 55%/34%; 07-15: 65%/34%; 07-16: 64%/35% (quote_deleted reasons by day) | **Verified** (was the 07-14 mix; rfq_gone share has grown) |
| quote latency p50 0.26 s, 9% > 2 s (POOL_DEADLINE_S=2.0) | all-time `seen_at→quote_sent`: p50 0.25 s, 7.1% > 2 s (n=55,145); today p50 0.13 s, 3.0% > 2 s | **Verified** (basis = pickup→post; improving) |
| 368 pool timeouts / 6 min under firehose | worst 6-min windows today: 315 / 755 / 694 | **Verified & exceeded** |
| ~87% of RFQs never trade for anyone | not re-derived in this pass (needs combo_trades ⋈ rfqs matching) | **Unverified here** — consistent with 224 deletes/s churn |
| POOL_WORKERS=8 / POOL_DEADLINE_S=2.0 / RFQ_WORKERS=8 | `quote_app.py:112,119,650` at HEAD `6d0f933` | **Verified** |

---

## 7. Leverage map (sized by today's funnel)

| Lever | Attacks | Ceiling if perfect | Notes |
|---|---|---|---|
| Cut wire→pickup 0.78 s (dedicated RFQ socket priority, dispatch fast-lane, skip record_rfq before pricing) | the 44.4% POST-race bucket | win rate on posts is 12.1% today at HEAD code (20,546 of 169,329 attempts; ~9.8% including the morning-code runs); every 100 ms saved moves us left on a race decided at ~0.67 s median | biggest single bucket; floor 0.52 s is partly exchange-side |
| Risk-cap posture (max_open_quotes was #1 reason in the recent sample, 38%) | the 32.8% risk-cap bucket | these RFQs are already priced — pure policy | interacts with Problem A/F (caps overstated) |
| Pool: cancel abandoned work / shed cold n>4 combos early / deadline-aware admission | 4.4% deadline bucket + the eviction bucket (27.9% of waiver intake) + kill 2 | stops timeout cascades that freeze ALL pricing | worker-side cooperative cancellation, or a cheap dim-classifier pre-gate |
| Maintenance sweep: batch/parallelize re-prices, budget per tick, never serial-await a frozen pool | heartbeat kill 2; frees ~60–120 calls/s of pool demand | uptime + pool headroom | also fixes the 31×2 s = 62 s tick |
| Teardown: `shutdown(wait=False)` or off-thread join | kill 1 (cosmetic kill + needs-reconcile on every halt) | clean halts | one line in `pricing_pool.py:227,657` |

---

## NEXT STEPS

- **OPERATOR, now:** bot is DOWN since 18:13:22Z (§ alert box). Relaunch when
  ready; expect kill-2 recurrence while the maintenance sweep serially awaits
  the pool under saturation.
- **Next measurement (owner: research):** confirm `rfq_closed`/409 semantics
  against docs.kalshi.com (first-quote-wins vs requester-accept window) — the
  0.67 s empirical window is solid, its *rule* is not.
- **Design decisions owed (operator):** which leverage rows in §7 to green-light;
  the wire→pickup attack and the maintenance-sweep budget are the two with no
  pricing-model risk.
- **Feeds into:** `01-*` / `03-*` companion lenses of this research channel and
  the eventual throughput plan doc.
