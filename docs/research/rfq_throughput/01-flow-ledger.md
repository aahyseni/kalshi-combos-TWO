# 01 — Zero-remainder RFQ flow ledger (2026-07-16)

**Question:** where does every incoming RFQ die? Decompose 100% of flow into
exactly one terminal bucket each — no "other" — then rank which buckets are
addressable throughput vs structurally dead.

**Method:** read-only queries against the live DB
`data/combomaker-prod-live-wc.sqlite3` (`decisions`, `rfqs` tables) via
`scripts/flow_ledger_48h.py` and `scripts/flow_ledger_firehose_window.py`;
firehose-level counters from `quote_app_stopped` metric snapshots in
`data/live_logs/*.log`. Every RFQ is assigned its **last** `no_quote`
decision's **first** reason (checks are severity-ordered, so the first reason
is the binding cap — `src/combomaker/rfq/lifecycle.py:1760-1764`), or
`quoted` if a `quote_sent` decision exists. SUM_CHECK == total in both
windows (zero remainder, enforced by the script).

---

## 1. The pipeline and where flow is measured

```
                     Kalshi communications WS (whole-exchange RFQ stream)
                                        |
                    ws.msg.rfq_created           <- LOG metric (L0, firehose)
                                        |
        +------------------------------+--------------------------------+
        | PRE-PARSE fastpath gate: any leg not on allowed prefixes      |
        | (config/prod-live-wc.local.yaml:130 -> [KXWC] ONLY right now) |
        | intake.py:103-111 -> rfq.dropped_series_fastpath (LOG only)   |
        +------------------------------+--------------------------------+
                                        |  in-allowlist, parsed = rfq.created
                                        v
        +------------------------------+--------------------------------+
        | RFQ work queue (32-deep, drop-OLDEST on overflow,             |
        | 1.5s max dwell) quote_app.py:659-713                          |
        |  -> rfq.evicted_oldest_for_fresh / rfq.skipped_stale_in_queue |
        |     / rfq.work_dropped_backpressure  (LOG only, NOT in DB)    |
        +------------------------------+--------------------------------+
                                        |  worker-processed -> rfqs TABLE row
                                        v
        filter.evaluate -> price (8-proc pool, 2s deadline) -> risk caps
                        -> POST CreateQuote -> quote lifecycle
                                        |
                          decisions TABLE (terminal bucket)   <- L1/L2 ledger
```

Consequence for measurement: the `rfqs` table is NOT the firehose. It is the
worker-processed, in-allowlist subset. Fastpath drops and queue drops exist
only as log metrics. Both levels are ledgered below.

## 2. L0 — firehose level (log metric snapshots, game-day 2026-07-16)

Three complete runs on Jul 16 have final `quote_app_stopped` snapshots
(source log : window UTC):

| counter | checkpointB 14:25:29-14:40:05 (876s) | fixed 14:40:40-14:58:35 (1075s) | waiver_tiers 17:30:23-18:13:23 (2580s) |
|---|---|---|---|
| ws.msg.rfq_created (firehose) | 135,028 (154.1/s) | 172,249 (160.2/s) | 600,222 (232.6/s) |
| rfq.dropped_series_fastpath | 113,126 (83.8%) | 142,870 (82.9%) | 508,398 (84.7%) |
| rfq.created (in-allowlist, parsed) | 21,902 (16.2%) | 29,379 (17.1%) | 91,824 (15.3%) |
| rfq.evicted_oldest_for_fresh | 1,126 | 895 | **25,579 (27.9% of parsed!)** |
| rfq.skipped_stale_in_queue | 13 | 17 | 74 |
| quote.sent | 0 | 738 | 8,008 |
| quote.rfq_closed_before_post | 0 | **27,364** | 4,128 |
| price.pool_deadline_drop | 650 | 725 | 2,897 |

Sources: `data/live_logs/live_20260716_checkpointB.log`,
`live_20260716_fixed.log`, `live_20260716_waiver_tiers_corners45.log` — each
run's single `quote_app_stopped` line. Rates = counter / (stop ts − start
`quote_app_starting` ts).

Reading:

- **~83-85% of the raw firehose is out-of-allowlist by config** — and the live
  allowlist is currently `[KXWC]` alone (`config/prod-live-wc.local.yaml:130`),
  so ALL MLB flow is also being fastpath-dropped right now.
- The prior "47.7 RFQ/s firehose" number is *stale low*: all three Jul-16
  windows ran 154-233 msg/s sustained.
- In the waiver_tiers window our OWN 32-deep queue evicted **27.9%** of parsed
  in-allowlist RFQs before a worker ever saw them (25,579 of 91,824) — these
  never reach the DB and are invisible to reason-code reporting.
- Share of firehose quoted: 738/172,249 = **0.43%** (fixed run),
  8,008/600,222 = **1.33%** (waiver run). This re-derives the handoff's "we
  price ~1-2% of incoming flow" (docs/reports/2026-07-15-HANDOFF-for-llm-review.md §4C).

## 3. L1/L2 — 48h zero-remainder ledger (worker-processed universe)

Window: `seen_at >= 2026-07-14T18:17:15Z` (48h to run time), n = **2,242,999**
RFQs, decisions id range [512,678, 3,658,166]. Script:
`scripts/flow_ledger_48h.py` (output reproduced verbatim; SUM_CHECK true).

| terminal bucket | n | % | class |
|---|---|---|---|
| skip_inplay_leg | 652,189 | 29.08% | DEAD by policy (pregame-only gate) |
| skip_rfq_closed | 485,210 | 21.63% | **ADDRESSABLE (lost POST race)** |
| skip_max_open_quotes | 290,994 | 12.97% | **ADDRESSABLE (self-imposed cap 60)** |
| skip_game_loss_cap | 148,465 | 6.62% | risk budget (bankroll-bound) |
| skip_leg_book_thin | 93,175 | 4.15% | data quality (partially addressable) |
| skip_size_above_max | 82,899 | 3.70% | DEAD (oversized for book) |
| no_decision | 77,110 | 3.44% | **ADDRESSABLE (409 quote_timed_out crash-loss, see §5)** |
| skip_price_deadline | 56,862 | 2.54% | **ADDRESSABLE (pool 2s deadline)** |
| skip_classifier_unknown | 51,795 | 2.31% | modeling coverage |
| skip_per_combo_loss_cap | 48,228 | 2.15% | risk budget |
| **quoted** | **46,915** | **2.09%** | — |
| skip_directional_cap | 42,243 | 1.88% | risk budget |
| skip_quote_timed_out | 40,855 | 1.82% | **ADDRESSABLE (exchange 409, legacy code's name for the POST race)** |
| skip_portfolio_cvar | 38,971 | 1.74% | risk budget |
| skip_mass_acceptance_breach | 38,074 | 1.70% | risk budget |
| skip_leg_unknown | 25,865 | 1.15% | data quality |
| skip_size_below_min | 13,516 | 0.60% | DEAD (too small to bother) |
| skip_leg_spread_too_wide | 7,309 | 0.33% | data quality |
| skip_too_many_legs | 1,136 | 0.05% | DEAD by config |
| skip_halted | 391 | 0.02% | ops |
| skip_post_paced | 370 | 0.02% | legacy pacing gate (branch code) |
| skip_slate_cap | 242 | 0.01% | risk budget |
| skip_leg_stale | 131 | 0.01% | data quality |
| skip_no_combo_grid | 47 | — | data quality |
| skip_logically_impossible | 4 | — | DEAD |
| skip_pricing_failed | 3 | — | error |
| SUM | 2,242,999 | 100.00% | zero remainder ✓ |

Notes:

- `skip_quote_timed_out` / `skip_post_paced` are NOT in current
  `src/combomaker/core/reasons.py`; they exist in branch commit `16e34f7`
  ("Kalshi returned the undocumented quote_timed_out business rejection" /
  "non-blocking smooth-output gate between POST slots") — overnight
  `risk-audit-overnight`-era runs wrote them into the shared DB. Both belong
  to the POST-race family.
- Reason co-occurrence (ever-seen per RFQ, from same script output):
  `skip_slate_cap` was seen on 455,644 RFQs but is terminal-primary for only
  242 — the slate cap almost never binds first; game_loss/mass-acceptance
  fire ahead of it. Never read raw reason tallies as flow shares.
- **Quoted sub-outcomes (48h):** deleted:rfq_gone 29,336 (63.2% of deletions),
  deleted:ttl_expired 16,049 (34.6%), deleted:leg_stale 1,000,
  declined_at_confirm 245, open/lapsed 214, leg_moved 44, **confirmed 27**.
  The prior "55% gone / 35% ttl" split re-derives as **63/35** on this window.
- Series mix: every top bucket is dominated by `KXWCADVANCE|KXWCGOAL`
  pairings (e.g. 148,298 of skip_inplay_leg; 89,383 of skip_rfq_closed) —
  World Cup semifinal/final flow. Zero KXMLB anywhere: MLB is not in the
  live allowlist.

## 4. Firehose-window ledger (current code, 17:30:23-18:13:24Z Jul 16)

Same method, n = **66,138** worker-processed (script
`scripts/flow_ledger_firehose_window.py`; SUM_CHECK true). This is the
honest picture of TODAY's binding constraints (no in-play games in window —
WC finals flow is all pregame):

| terminal bucket | n | % |
|---|---|---|
| **skip_max_open_quotes** | **27,677** | **41.85%** |
| skip_game_loss_cap | 8,432 | 12.75% |
| **quoted** | **7,999** | **12.09%** |
| skip_mass_acceptance_breach | 7,234 | 10.94% |
| skip_size_above_max | 5,779 | 8.74% |
| skip_rfq_closed | 3,112 | 4.71% |
| skip_price_deadline | 2,798 | 4.23% |
| skip_portfolio_cvar | 1,197 | 1.81% |
| skip_classifier_unknown | 959 | 1.45% |
| skip_per_combo_loss_cap | 549 | 0.83% |
| skip_size_below_min | 368 | 0.56% |
| (six buckets < 0.02% each: book_thin 10, leg_stale 9, no_decision 7, too_many_legs 6, no_combo_grid 1, leg_unknown 1) | 34 | 0.05% |

Plus the queue level above the DB: 25,579 evictions in this window ≈ **27.9%
of parsed flow dropped before pricing** (log snapshot, §2).

Quoted sub-outcomes in-window: rfq_gone 5,105 / ttl_expired 2,755 /
leg_stale 67 / leg_moved 34 / open 31 / declined 4 / **confirmed 3** (fills).

**The regime changed between the 48h average and now**: the POST-race loss
(21.6% avg) collapsed to 4.7% under today's code, and the binding constraint
moved to our own `max_open_quotes: 60` cap
(`config/prod-live-wc.local.yaml:273`, enforced at
`src/combomaker/risk/limits.py:416-420`) plus the risk-cap family (~26%
combined) on a $5.8k bankroll (breach details in decisions read
"3/10 bankroll = 5836577cc" = $583.66 per-game loss ceiling).

## 5. no_decision forensics (77,110 = 3.44% of 48h — fully explained)

Per-hour histogram (same script): >99% of the bucket sits in three hours —
Jul 15 23h (21,762 = 41.0% of that hour), Jul 16 01h (16,306 = 68.2%),
Jul 16 02h (38,777 = 67.9%). All other hours ≤ 0.08%.

Mechanism, from `data/live_logs/live_20260715_214523.log` (run window
01:45:30-02:34:49Z): **55,074 `rfq_worker_failed` events**, every traceback
ending `KalshiApiError: HTTP 409 {'code': 'quote_timed_out', 'service':
'midland'}` raised from `create_quote` — the overnight branch build did not
map Kalshi's undocumented `quote_timed_out` 409 to a skip decision, so the
worker recorded the rfq row, priced it, POSTed, took the 409, and crashed out
of `handle_rfq` before any decision row. These are POST-race losses in
disguise: **the RFQ reached the POST stage having passed every gate.**
(Current HEAD handles any 409 at `lifecycle.py:1779-1786` → `skip_rfq_closed`;
the firehose window has only 7 no_decision rows.)

## 6. Addressable vs structurally dead (the ranking)

**Addressable flow, 48h window** (RFQs we could have priced/quoted):

| rank | bucket family | n (48h) | % | evidence & lever |
|---|---|---|---|---|
| 1 | POST-race family: skip_rfq_closed + skip_quote_timed_out + no_decision-409s | **603,175** | **26.9%** | all fully priced + risk-passed, POST landed late or 409'd. Lever = latency: WS delivery lag alone is p50 **0.774s**, p90 1.44s, min 0.527s (created_ts→seen_at over 1,500 sampled raw payloads, `scripts/` §7 — could be lag or clock skew, needs NTP check); current-code seen→sent is p50 0.130s / 2.0% >2s (n=8,008, `scripts/latency_seen_to_sent.py`), so the pipe is no longer the fat tail — SEEING the RFQ late is |
| 2 | skip_max_open_quotes | 290,994 | 13.0% | "60 open quotes at cap 60" (decision context; cap at `config/prod-live-wc.local.yaml:273`). 41.9% of the current-code window — the #1 live constraint TODAY |
| 3 | risk-cap family (game_loss + per_combo + directional + cvar + mass_acceptance + slate) | 316,223 | 14.1% | binds against $5.8k bankroll ("3/10 bankroll = 5836577cc"); 26.3% of the current-code window. Addressable via capital or cap policy, NOT via code |
| 4 | pipeline latency drops: skip_price_deadline + queue evictions | 56,862 (+25,579/43min at L1) | 2.5%+ | pool deadline 2.0s (`quote_app.py:119`), 382 pool timeouts in first 6-min of caps100 run, 9,087/119,743 = 7.6% of pool calls over 2h17 (pricing_stats deltas, `live_20260716_caps100_dir40_corners3.log`) |
| 5 | data-quality gates: leg_book_thin + leg_unknown + leg_spread_too_wide + leg_stale | 126,480 | 5.6% | thin/absent Kalshi leg books; partially addressable via external odds (SGO) marginals |
| 6 | skip_classifier_unknown | 51,795 | 2.3% | modeling coverage (containment/collapse plans shrank this before; residual is genuine UNKNOWN relationships) |

**Structurally dead** (not throughput):

| bucket | n / share | why dead |
|---|---|---|
| out-of-allowlist firehose | ~84.7% of L0 stream | sports-only + `[KXWC]`-only config; note KXMLB currently excluded — re-adding it is ONE yaml line if MLB quoting is wanted (`config/prod-live-wc.local.yaml:130`) |
| skip_inplay_leg | 652,189 (29.1% of 48h) | pregame-only operator gate (all sports). Dead **by directive**, not by nature — it was ~0% in today's window (no live games) and will swell again during games |
| skip_size_above_max / below_min | 96,415 (4.3%) | RFQ size out of our book's range (e.g. "candidate 6178.00 contracts > 2000.0") |
| too_many_legs / unmodeled / impossible / halted / no_grid | <0.1% combined | config caps and genuine impossibles |

## 7. Raw examples (top addressable buckets, from firehose window)

**skip_rfq_closed** (lost POST race):
- `fea7d547-95ea` seen 17:32:16.694Z, 2-leg `yes:KXWCGAME-26JUL19ESPARG-ARG +
  yes:KXWCTOTAL-26JUL19ESPARG-3`, target_cost 100000cc — sole decision
  17:32:17.862Z (`+1.17s`) "rfq window closed before our quote POST landed".
- `ef124e83-8cd4` seen 17:32:29.232Z, 3-leg totals combo — passed to POST on
  retry, closed at 17:32:30.601Z (`+1.37s`); first attempt 6ms after sight had
  hit game_loss_cap, the retry found headroom but the window was gone.

**skip_max_open_quotes** (cap 60):
- `2b00446f-4fde` seen 17:31:45.576Z, 3-leg FRA advance + 2 England scorers —
  three attempts over 2.0s, final: "60 open quotes at cap 60".
- `1addc6ea-405f` seen 17:31:45.632Z, 2-leg FRA advance + Dembélé scorer,
  target_cost 850000cc ($85) — same: cap 60 at third attempt (+1.94s).

**skip_price_deadline** (pool 2s budget):
- `597d118b-ce36` seen 17:30:59.591Z, 3-leg ESP/ARG (1H total + BTTS + ESP
  win) — no_quote at +2.60s "joint pricing exceeded the off-loop deadline".
- `73beaa76-3b5e` seen 17:31:01.240Z, 3-leg no-side combo — same at +2.00s.

**skip_game_loss_cap** (bankroll):
- `8b68b6ff-bbe0` seen 17:31:30.244Z, 7-leg cross-category $190 target-cost
  parlay — "game 26JUL19ESPARG loss 5996169cc > 3/10 bankroll = 5836577cc"
  within 5ms of sight.
- `d6301fa6-af49` seen 17:31:36.690Z, 2-leg BTTS+corners — game cap on both
  ESP/ARG and FRA/ENG.

## 8. Prior claims — verified or corrected

| prior claim | verdict | primary source |
|---|---|---|
| 89,863 rfq_closed_before_post in one run | **VERIFIED exactly** | `live_wc10.log` final `quote_app_stopped` counter `"quote.rfq_closed_before_post": 89863` |
| deletions 55% rfq_gone / 35% ttl | **CORRECTED to 63.2% / 34.6%** (48h, per-RFQ last-delete) | flow_ledger_48h.py quoted sub-outcomes (29,336 / 16,049 of 46,429) |
| quote latency p50 0.26s, 9% tail >2s | **IMPROVED: p50 0.130s, 2.0% >2s** (current code, n=8,008) | `scripts/latency_seen_to_sent.py` on waiver window; prior figure from docs/reports/2026-07-14-market-vs-our-pricing-main-combos.md:138 |
| 368 pool timeouts / 6 min under firehose | **VERIFIED order: 382 in first 6:01**; 7.6% of pool calls over 2h17 | pricing_stats cumulative counters in `live_20260716_caps100_dir40_corners3.log` (14:59:30 calls=0/timeouts=0 → 15:05:31 4682/382; 17:16:59 119743/9087) |
| firehose ~47.7 RFQ/s | **STALE LOW — 154-233 msg/s sustained Jul 16** | three `quote_app_stopped` snapshots ÷ run duration (§2) |
| we price ~1-2% of incoming flow | **VERIFIED at L0: 0.43-1.33%** of raw stream; 2.09% of worker-processed 48h; 12.1% of processed under current code | §2 + §3 + §4 |
| ~87% of RFQs never trade for anyone | NOT re-derived here (needs the shadow DB / trades join; prior number from old-repo research memory) | flagged for a follow-up lens |

## 9. What the ledger says (operator summary)

1. **Under today's code the #1 blocker is our own `max_open_quotes: 60`**
   (41.9% of processed flow in the game-day window), followed by the
   bankroll-bound risk caps (26.3%). The POST-race problem that dominated the
   48h average (26.9% incl. its disguises) has already been beaten down to
   ~4.7% by the Jul-16 fixes — remaining race losses are dominated by the
   **0.77s WS delivery lag**, not the pipe.
2. **A quarter of parsed in-allowlist flow died in our 32-deep queue**
   (25,579 evictions in 43 min) before any pricing — invisible to reason
   codes. Queue capacity/worker count is the cheapest big win after the cap.
3. 84.7% of the raw firehose is out-of-allowlist by config — and the live
   allowlist is `[KXWC]` only: **MLB is currently OFF**. One yaml line
   (`allowed_leg_series_prefixes`) is a doubling-plus of eligible flow if
   desired.
4. Everything sums: 2,242,999 48h RFQs and 66,138 window RFQs land in exactly
   one bucket each (SUM_CHECK true in both script outputs); the once-opaque
   no_decision bucket is fully attributed to the un-merged branch's unhandled
   `quote_timed_out` 409.

---

## NEXT STEPS

- **Owner: next lens (research).** Quantify fill-rate and EV impact of raising
  `max_open_quotes` 60 → 80+ (config history at
  `prod-live-wc.llm-b-bak.local.yaml:219` shows 80 was already prepared) and
  of a deeper/wider RFQ queue — both are pure-throughput levers with
  mass-acceptance-cap protection already in place.
- **Owner: research.** Root-cause the 0.774s created→seen lag (Kalshi-side
  fanout vs our WS consumer backlog vs clock skew — check NTP offset first;
  min lag 0.527s even in quiet minutes suggests systematic).
- **Owner: operator decision.** (a) Re-add `KXMLB` to
  `allowed_leg_series_prefixes` when ready to quote MLB again; (b) whether the
  risk-cap family share (~26% of current flow) is a capital question or a cap
  question — per standing rule, never tuned on a P&L window.
- **Owner: research.** Re-derive the "87% never trade" base rate from the
  shadow DB with a proper RFQ→trade join (flagged NOT re-derived in §8).
