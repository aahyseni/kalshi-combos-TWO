# Combo-pricing throughput — Phases 0–2 SHIPPED + validated, live-run findings, and the reframed next steps — 2026-07-14

Trigger: the 04:20 UTC supervisor kill (event loop wedged 15.4s on a multi-second
price()); operator directive to make combo pricing "A LOT faster", price every
combo "< 1s always", and be first to the taker.

## TL;DR

- **Shipped + validated (all parity-to-the-cent / suite-green): the joint memo,
  the off-loop ProcessPool+deadline, and the dimension-adaptive MVN tolerance.**
- **The wedge is SOLVED.** Live prod run 16:22 UTC under the full firehose: 0
  supervisor kills, 0 overflows, 0 halts, one stable connection. The off-loop
  deadline (Phase 1) is doing exactly its job.
- **Two live surprises that reframe the remaining plan:**
  1. the exact-belief memo hits only **~6%** live (books tick faster than RFQs
     repeat) → **Phase 4 pre-warm is the WRONG fix**; the real hit-rate lever is
     quantizing the marginal in the memo key (re-grade-gated).
  2. we now price fast enough to *attempt* quotes, but **lose the taker race** (0
     quotes land; RFQs close before our POST) → the next problem is the
     RFQ→wire **latency pipeline** (queue freshness + deadline + throughput), not
     raw pricing speed.
- **Phase 3 (per-game structural fit cache) was SKIPPED** — verified redundant with
  the shipped joint memo (fits are combo-specific, not game-invariant), operator-
  confirmed.

## UPDATE 17:xx UTC — classifier-unknown resolved (disambiguation + band×neighbour derivation) + RFQ-lifecycle research

- **`skip_classifier_unknown` was mostly MISLABELLED.** ~169/240 were "no combo
  grid" (missing metadata on multi-game exotics) — never a classifier failure.
  Split into distinct reasons (`SKIP_NO_COMBO_GRID` / `SKIP_SIZE_UNRESOLVABLE` /
  `SKIP_MALFORMED_COMBO`), so the code now means ONLY a genuine relationship-UNKNOWN.
- **The real ~70 (WC same-game containment-window × neighbour, e.g. FRA-ESP) now
  PRICE via a derivation.** Insight: the copula super-leg can't correlate a
  non-monotone window with a same-game neighbour (the old fail-close at
  `relationships.py:1040`), but the **structural Dixon-Coles model prices
  `P(window ∧ neighbour)` DIRECTLY** (a scoreline region). So the classifier now
  routes band+same-game-neighbour combos to structural (new `NESTED_BAND
  band_with_neighbour` signal) and declines only if structural can't represent them
  (corners / MLB / multi-game). Validated: FRA win × spread window + total prices
  `p=0.125`, exact + arb-safe. **Only affects currently-declining combos — no
  existing quote moves a cent.** Suite **1735/0**, rule-8c parity **227/227** (both
  backtest harnesses kept in sync per rule 8c).
- **RFQ-lifecycle research (how long to keep a bid out):** Kalshi RFQs have **no
  fixed exchange TTL** — a quote rests, swipeable at its posted price, until the RFQ
  closes or we pull it (no server-side book-move auto-void → stale-book exposure is
  entirely our `quote_ttl_s`). Live tape: **median combo RFQ lives ~11s, p90 ~24s,
  only 3.3% past 30s.** Our `quote_ttl_s` is the **30s default and UNWIRED**
  (`quote_app.py:405` builds a bare `LifecycleConfig()`), and 44% of our
  quote-deletes are that 30s TTL firing FIRST — i.e. we hold quotes ~3× the median
  RFQ life on moved books. **Recommendation: lower to ~20s (RFQ p90)** — catches
  ~97% of realistic swipes, cuts stale exposure, frees capacity to price more.
  Caveat: 0 fills in the recording, so re-validate the exact value once we have
  fills. (Confirm/execute ~1–3s HVM window is a backstop, not a substitute.)

---

## UPDATE 16:52 UTC — P1/P3 + 1¢ markup shipped; "price failed" resolved; the real limiter is the CAPS

Second live re-run (`live_ph3.log`, task `bkysqe19i`) with the fixes below. Key results:

- **"Why am I seeing price failed?" — RESOLVED.** It was **100%** the 0.8s off-loop
  deadline drops mislabeled `SKIP_PRICING_FAILED`. New-run decline reasons show
  **`skip_pricing_failed` = 0**; the deadline drops now read `skip_price_deadline`
  (a deliberate latency drop of a combo too slow to win its ~1s window — NOT a
  pricer failure). **We never fail to price anything.**
- **We ARE competitive.** Prior run landed **1,640 quotes** (`quote.sent`) with **1
  taker acceptance**; this run **~1.1 quotes/s** (207 in 3 min). (My earlier "0
  quotes" was early-snapshot bias before the pool warmed.)
- **`rfq_worker_failed` tracebacks: 231 → 0** (rfq_closed is now a counted decline).
- **Pool timeout rate ~18% → ~5%** (freshness skips stale RFQs before they burn a
  pool slot).
- **THE REAL QUOTE-VOLUME LIMITER IS THE RISK CAPS, not pricing.** New-run declines:
  `skip_slate_cap` 1367, `skip_game_loss_cap` 1366, `skip_max_open_quotes` 968,
  `skip_per_combo_loss_cap` 178, `skip_directional_cap` 119, `skip_mass_acceptance_breach`
  97 — ~4,000 CAP declines vs 207 quotes. The caps decline ~95% of priceable combos.
  Raising quote volume is now a **risk-appetite decision on the $2k bankroll**, not
  an engineering one.

### P1/P3 fixes shipped this pass (all suite-green 1734/0)
- **P3a**: new `SKIP_PRICE_DEADLINE` reason for the off-loop deadline drop (distinct
  from `SKIP_PRICING_FAILED`, which now only ever means a genuine error — of which
  there are none live).
- **P3b**: `rfq_closed`/409 caught as a counted decline (`quote.rfq_closed_before_post`
  + `SKIP_RFQ_CLOSED`), no traceback. Regression: `test_rfq_closed_is_graceful_not_a_failure`.
- **P1**: win-the-taker FRESHNESS — shallow queue (maxsize 8), **drop-OLDEST** on
  overflow (was drop-newest), skip an RFQ whose queue dwell already exceeds
  `RFQ_MAX_QUEUE_DWELL_S` (0.4s) before pricing, and stop retrying a pending RFQ once
  older than `RFQ_RETRY_WINDOW_S` (2s). (`ops/quote_app.py`.)
- **Markup 2¢ → 1¢** (operator, `config/prod-live-wc.local.yaml`): more competitive on
  the taker race. NOTE: 1¢ is BELOW the re-grade's ~2.2¢ robustly-+EV floor — a
  competitiveness bet, watch markouts.
- **Phase 2 finding (memo hit rate ~6–8%)** confirmed unchanged: the exact-belief key
  rarely repeats live (books tick). A marginal-quantized key would lift it but changes
  the $ (bucketed price) → needs the zero-cent re-grade + operator sign-off. Deferred.

### NEXT decision owed by operator
- **Caps** are the throttle. Loosening `slate` / `game_loss` / `max_open_quotes`
  raises quote volume (and risk) on the $2k bankroll — your call on appetite. This,
  not pricing, is what stands between ~1 quote/s and more.
- Marginal-quantized memo (P2) — enable behind a re-grade if you accept a sub-cent
  bucketed price for a higher hit rate.

---

## What shipped (3 changes, all validated)

| # | Change | File(s) | Parity gate |
|---|---|---|---|
| **0** | **Exact joint memo** — LRU on ordered (ticker,side,event)+exact (p,unc) beliefs+frozen Relationship → cached JointEstimate. Warm repeats become O(1). | `pricing/engine.py` | memo-off==miss==hit 0/120; suite 1729/0; rule-8c 227/227 |
| **1** | **Off-loop ProcessPool + 0.8s deadline** — cold (memo-MISS) joint runs in a worker PROCESS with a hard deadline so it can never wedge the loop; warm hits stay inline. Refactored price() into prefix/joint/suffix; added async `price_offloaded`, `JointPool`, and lifecycle `_price_async` at the 3 async hot sites (sync `_price` kept for last-look/markout). | `pricing/engine.py`, `ops/pricing_pool.py`, `rfq/lifecycle.py`, `ops/quote_app.py` | pool==inline 0/75 (tape); real-pool bit-identical + deadline-drop tests; suite 1733/0 |
| **2** | **Dimension-adaptive MVN tolerance** — small-n `abseps` 1e-10→1e-7 (error 0.001¢, 100× inside a cent); n>4 unchanged (scipy default). | `pricing/copula.py` | re-grade 690 tape combos = **0 quote-cent changes** vs 1e-10; suite 1733/0; rule-8c 227/227 |

Tools added (additive, rule-8): `tools/profile_pricer.py`, `tools/memo_parity_check.py`,
`tools/pool_parity_check.py`, `tools/tolerance_regrade.py`. Regression: `tests/test_pricing_pool.py`.

## Measured cold-path speedup (tools/profile_pricer.py, 450 real combos)

| | baseline | after Phase 2 |
|---|---|---|
| p50 | 2ms | 2.6ms |
| **p90** | **1,855ms** | **166ms** (11×) |
| mean | 457ms | 145ms |
| p99 / max | 4,513 / 7,374ms | ~5,289 / 8,160ms (unchanged — high-dim tail) |

The p99/max tail (5–8s) is high-effective-dim (n>4) / structural-heavy same-game
combos, deliberately untouched by the tolerance change and **capped operationally
by Phase 1's 0.8s deadline** (dropped, then re-priced on the re-RFQ).

## LIVE PROD RUN — 2026-07-14 16:22 UTC (the Phase-4 measurement)

Config `config/prod-live-wc.local.yaml`; log `…/tmp/live_ph012.log`; task `bqoz1rqqe`.
Startup clean: `joint_pool_started`(2w,0.8s) → `joint_pool_warm` → `book_reconciled`
→ `prod_preflight_green` → `communications_subscribed`. (First launch refused on a
leftover repo-root `KILL` file from the 04:20 kill — removed, relaunched.)

| Signal (first ~3 min) | Value | Reading |
|---|---|---|
| supervisor kills / overflows / halts | **0 / 0 / 0** | **WEDGE SOLVED** — stable under the full firehose |
| connections | 1 | stable |
| pool calls / timeouts / errors | ~1,500 / ~410 / 0 | ~30% of cold combos exceed 0.8s → deadline-dropped cleanly |
| **memo hit rate** | **~6%** (stable) | 🔴 exact-belief keys rarely repeat — books tick fast |
| quote_created / accepted / executed | **0 / 0 / 0** | 🔴 taker race lost — RFQs `rfq_closed` before our POST |
| rfq_closed (409) | many | we price + POST, but the ~1s RFQ window has closed |

### Finding A — Phase 4 (pre-warm) is the wrong fix
The joint memo keys on EXACT beliefs. Live, the same hot leg-set re-RFQs with
DIFFERENT marginals each time (books tick), so it's a new key → ~6% hit. Pre-warming
those keys would be equally stale the moment the book ticks. **The real hit-rate
lever is quantizing the marginal in the key** (round p to ~½¢): the same leg-set
within a price bucket becomes a hit. That is a sub-cent pricing change → gate it
with the SAME zero-cent-drift re-grade the tolerance change used (tools/tolerance_regrade
pattern), and it likely lifts the hit rate from ~6% to the majority of the hot flow.

### Finding B — the real problem is now taker-race LATENCY, not pricing speed
We're finally fast enough to attempt quotes (the old bot wedged before pricing), but
0 land. RFQ→wire is too slow for the ~1s HVM window. Contributing causes:
- **Deep FIFO intake queue (maxsize 40)** holds STALE RFQs; put_nowait drops the
  NEWEST on overflow. So we spend pricing budget on RFQs that are already closing
  and drop fresh ones — backwards for latency. Want a shallow / drop-OLDEST / freshest-
  first intake, and/or skip an RFQ whose age already exceeds the window before pricing.
- **0.8s deadline eats most of the ~1s window** — a combo finishing at ~0.7s then
  needs a POST RTT and is closed. The deadline may need to be much lower (price only
  what we can also POST in time; drop the rest sooner).
- **Throughput (2 asyncio × 2 pool ≈ price a few/s) ≪ 170 RFQ/s arrival** — most are
  shed. More pool workers (cores permitting) + the marginal-quantized memo (Finding A)
  both raise how many we can answer in time.

### Finding C — log hygiene
`rfq_closed` (409) propagates as a loud `rfq_worker_failed` traceback. A closed RFQ
is a NORMAL race outcome, not a failure — it should be caught and counted
(metric `rfq.closed_before_quote`), not stack-traced. Small fix.

## Phase 3 — SKIPPED (redundant), operator-confirmed
`structural._price` inverts the Dixon-Coles model to match THE SPECIFIC COMBO's legs
(`constraints = [(spec, b.p) …]`), so the fit is combo-specific, not game-invariant.
A per-game fit cache keyed on exact constraints hits at the same granularity as the
joint memo → no new hits. The "830× re-RFQ" repeats it was meant to help are already
caught by Phase 0. A coarser game-level key would change prices (violates the cent
rule). Only a genuine structural win would be ANALYTIC uncertainty (replacing the
2n+ re-inversions) — output-changing, risks NARROWING the band (the one money-losing
direction), needs a dedicated width re-grade. Not worth it; the deadline caps the tail.

## NEXT STEPS (reframed by the live data)

- **Owner: us — P1 (win the taker).** Rework the intake for FRESHNESS: shallow/drop-
  oldest queue + skip RFQs already past the window before pricing + tune the deadline
  DOWN. Measure quote_created > 0. This — not more pricing speed — is what lands quotes.
- **Owner: us — P2 (lift the memo hit rate).** Marginal-quantized memo key (round p to
  ~½¢), gated by the zero-cent-drift re-grade. Turns the ~6% hit into the majority of
  the hot flow (and makes pre-positioning meaningful). Supersedes Phase 4 pre-warm.
- **Owner: us — P3 (hygiene).** Catch `rfq_closed`/409 as a counted decline, not a
  traceback. Consider more pool workers if the host has cores.
- **Decision owed by operator:** (a) keep the bot running as the measurement source
  vs stop it (stable, $0 exposure, 0 fills, but winning 0 quotes); (b) proceed with
  the taker-race + quantized-memo work above; (c) Advanced API upgrade still deferred
  until we fill.
