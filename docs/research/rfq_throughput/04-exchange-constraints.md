# Lens 4 — Exchange constraints + competitor timing (2026-07-16)

**Question:** what does the exchange actually allow (docs), how fast do winning
makers get paid (tape), what do they cover that we don't (breadth), and which
API levers are we leaving on the table?

**Data:** docs.kalshi.com (fetched today); shadow tape
`data/combomaker-prod.sqlite3` (READ-ONLY `mode=ro`; 25.07M rfqs / 1.53M
combo_trades / 23.15M rfq_deletions, recorded 2026-07-06 → 07-16, **recorder gap
Jul 13–15**: 0 RFQs recorded those days); live DB
`data/combomaker-prod-live-wc.sqlite3` (run since 2026-07-13T20:14Z); run logs
`data/live_logs/`. Analysis scripts:
`docs/research/rfq_throughput/scripts/lens4_*.py`.

---

## 0. The funnel today (one 4h run, primary numbers)

Final metrics snapshot of the current run (`quote_app_stopped`,
`live_20260716_waiver_tiers_corners45.log`, ts 18:13:23Z, run ≈ 14:13→18:13Z):

```
firehose                pipeline                    quotes              money
+------------------+    +---------------------+    +---------------+   +-----------+
| ws.msg.rfq_created|-->| rfq.created_combo   |--->| quote.sent    |-->| accepted 7|
|      600,222      |    |      91,824  (15.3%)|    |  8,008 (1.3%) |   | executed 3|
|  (~41.7 RFQ/s avg)|    | dropped_series_fast |    | closed_before |   +-----------+
+------------------+    |  _path  508,398     |    |  _post  4,128 |
                         | rfq.skipped  93,187 |    | (34% of POSTs)|
                         +---------------------+    +---------------+
```

| stage | count | share | source |
|---|---|---|---|
| RFQCreated received | 600,222 | 100% | `ws.msg.rfq_created`, final metrics snapshot, `live_20260716_waiver_tiers_corners45.log` |
| pre-dropped (series fastpath) | 508,398 | 84.7% | `rfq.dropped_series_fastpath`, same snapshot |
| entered pipeline | 91,824 | 15.3% | `rfq.created_combo`, same snapshot |
| quotes sent | 8,008 | 1.33% of firehose | `quote.sent`, same snapshot |
| POST lost race (409 rfq_closed) | 4,128 | 34% of 12,136 POST attempts | `quote.rfq_closed_before_post`, same snapshot; emitted at `src/combomaker/rfq/lifecycle.py:1780` |
| accepted / executed | 7 / 3 | 0.09% of sent | `quote_event.quote_accepted` / `quote_executed`, same snapshot |

Live-DB day totals (decisions, `file:...prod-live-wc.sqlite3?mode=ro`): Jul 14
19,466 / Jul 15 14,757 / Jul 16 20,555 `quote_sent`; over the whole run
`quote_sent` 55,145 vs `no_quote` 3,548,405 decisions (1.5%), against 2,475,481
RFQs recorded by the live bot (2.2%) → **we price ~1.5–2.2% of the flow we
see** (handoff §4C "~1-2%" re-derived). Fills: 16 / 4 / 9 per day (live
`fills`).

The prior "89,863 rfq_closed_before_post in one run" (handoff §4C) is from the
7/15 firehose run; today's 4h run shows the same phenomenon at 4,128, and the
live DB logged **205,513 `skip_rfq_closed` decisions on Jul 16 alone** (decisions
`kind='no_quote'`, reasons `["skip_rfq_closed"]`).

---

## 1. (a) What the docs actually allow — verified today

### Rate limits (we are ADVANCED)

| tier | read tok/s | write tok/s | how obtained | source |
|---|---|---|---|---|
| Basic | 200 | 100 | default | docs.kalshi.com/getting_started/rate_limits.md |
| **Advanced** | **300** | **300** | self-serve upgrade | same |
| Expert | 600 | 600 | earned: 0.075% volume share (keep 0.05%) | same |
| Premier | 1,000 | 1,000 | earned: 0.125% / 0.10% | same |
| Paragon / Prime / Prestige | 2,000 / 4,000 / 6,000 | 2,000 / 4,000 / 8,000 | earned (Prime 0.50% / 0.40%) | same |

- Write bucket = "Order placement, amends, cancels, order groups, **the RFQ
  quote flow**, and block trade proposal accepts." REST and FIX drain the same
  buckets; WS inbound is **not** token-metered (rate_limits.md; confirms the
  2026-07-14 WS docs review).
- **CreateQuote costs 2 tokens, not the default 10** ("2 tokens per request",
  docs.kalshi.com/api-reference/communications/create-quote.md). DeleteQuote is
  also 2 tokens (delete-rfq-quote.md). Default for unlisted endpoints is 10
  (list-non-default-endpoint-costs.md).
- **⇒ Advanced write budget supports ~150 quote POSTs/s** (300/2). We averaged
  8,008 quotes / 4h ≈ **0.56/s — using ~0.4% of our quote-write headroom.**
  The exchange rate limit is nowhere near binding; our own pipeline is.

### RFQ/quote mechanics (combo = High Volatility Market rules)

| rule | value | source |
|---|---|---|
| Combo (HVM) confirmation window | **3 s** ("Confirmation window: 3 s") | docs.kalshi.com/getting_started/rfqs.md |
| Combo (HVM) execution timer after confirm | **1 s** | same |
| Standard-market windows (non-combo) | 30 s confirm / 15 s execute | same |
| Quote lifetime on exchange | **no TTL** — "A new quote on the same RFQ replaces the maker's previous quote"; quotes die only by DeleteQuote, replacement, or RFQ close | rfqs.md + delete-rfq-quote.md ("deleting a quote … means it can no longer be accepted") |
| Quote privacy | "Each quote is private between the requester and the individual maker; makers cannot see each other's quotes" | rfqs.md |
| Quote price rule | reject if `yes_bid + no_bid > $1` | rfqs.md |
| `rest_remainder` (on CreateQuote) | "Whether to rest the remainder of the quote after execution" | create-quote.md |
| `post_only` (on CreateQuote) | resting order cancelled rather than crossed if it would take | create-quote.md |
| `subaccount` on quotes/RFQs | 0 primary, 1–63 subaccounts | create-quote.md / create-rfq.md |
| Open-RFQ cap (taker side) | "Maximum 100 open RFQs at a time per user" | create-rfq.md |
| Open-QUOTE cap (maker side) | **none documented** — our 60 is self-imposed (`config/prod-live-wc.local.yaml:273 max_open_quotes: 60`; caused 38,162 `skip_max_open_quotes` on Jul 16) | create-quote.md (absence) + live DB decisions |
| Combo market creation | `CreateMarketInMultivariateEventCollection` "must be hit at least once before trading"; **"Users are limited to 5000 creations per week"** | api-reference/multivariate/create-market-in-multivariate-event-collection.md |
| Batch orders (order book, not RFQ) | billed per item ("10 tokens per order in the batch"); batch size scales with tier write budget | api-reference/orders/batch-create-orders-v2.md |
| WS sharding | `shard_factor` 1–100 + `shard_key`; content-agnostic fanout; errors 25 (buffer overflow) / 26 (market limit) / 27 (command rate) | websockets/websocket-connection.md (asyncapi) |
| WS content filter | none — "Market specification ignored"; RFQCreated/RFQDeleted always sent; Quote* events only for RFQs/quotes you're party to | websockets/communications.md |
| Maker fee plans | **no maker-program / maker-fee pages exist in the docs**; tiers above Advanced are earned by volume share, not fees | docs.kalshi.com/llms.txt sweep + rate_limits.md |

**Correction to our own assumption:** our quote TTL is **20s**, hardcoded
`QUOTE_TTL_S = 20.0` at `src/combomaker/ops/quote_app.py:128` (overrides the
`quote_ttl_s: float = 30.0` default at `src/combomaker/rfq/lifecycle.py:139`;
no YAML override exists). The "23s TTL" figure circulating in prompts/reports
matches neither — use 20s.

---

## 2. (b) Competitor timing off the tape

### How long an RFQ lives (create → RFQDeleted)

10k-sample join `rfq_deletions.raw_json.$.deleted_ts` −
`rfqs.raw_json.$.created_ts` (both **exchange** clocks), recent deletions,
indexed on `rfqs.rfq_id` (`scripts/lens4_tape_basics.py` §C):

| percentile | lifetime | | ≤ threshold | share |
|---|---|---|---|---|
| p10 | 2.4 s | | ≤ 5 s | 16.9% |
| p25 | 8.7 s | | ≤ 10 s | 27.1% |
| **p50** | **19.2 s** | | ≤ 20–23 s | ~55–57% |
| p75 | 33.6 s | | ≤ 30 s | 63.5% |
| p90 | 205 s | | ≤ 60 s | 82.4% |
| p99 | 6,091 s | | ≤ 600 s | 94.1% |

The median RFQ is **gone in ~19s**; a sixth of them in under 5s. (13.7% of
sampled deletions had no recorded RFQCreated row — the recorder itself drops
under the firehose; distribution is over the joined 8,626.)

### How fast the WINNING maker gets a print (RFQ create → trade)

Join `combo_trades.created_time` to the **latest** RFQ on the same bespoke
`market_ticker` with `created_ts ≤ trade time` (both exchange clocks;
`scripts/lens4_rfq_to_trade_latency.py`). A print requires: maker quote up →
taker accepts → maker confirms (≤3s window) → execution timer (1s).

**Window Jul 15–16 (lull + today, n=7,252 matched trades):**

| create→trade | value | | traded within | share of matched prints |
|---|---|---|---|---|
| p10 | 0.12 s | | ≤ 1 s | **36.5%** |
| p25 | 0.48 s | | ≤ 2 s | **48.0%** |
| **p50** | **2.26 s** | | ≤ 5 s | **72.9%** |
| p75 | 5.37 s | | ≤ 10 s | 84.7% |
| p90 | 372 s | | ≤ 23 s | 86.9% |
| | | | ≤ 30 s | 87.0% |

**Window Jul 10–12 (WC firehose, n=494,296 matched of 500,669 trades — 98.7%
matched, no recorder gap):**

| create→trade | value | | traded within | share of matched prints |
|---|---|---|---|---|
| p10 | 4.0 s | | ≤ 1 s | 3.6% |
| p25 | 11.2 s | | ≤ 2 s | 5.8% |
| **p50** | **29.6 s** | | ≤ 5 s | 12.5% |
| p75 | 101 s | | ≤ 10 s | 22.8% |
| p90 | 549 s | | ≤ 23 s | **42.7%** |
| p99 | 10,821 s | | ≤ 30 s | **50.4%** |
| | | | ≤ 300 s | 85.8% |

Matched-print rate: 295,107 matched RFQ instances / 6,623,445 window RFQs =
**4.46%** at peak (vs 0.34% in the lull) — even at peak, ≥95% of RFQ instances
never print.

Reading — **two regimes**:

- **Lull/pregame regime (Jul 15–16):** a third of prints complete the ENTIRE
  loop (quote + accept + confirm + execute) inside 1 second of RFQ creation —
  only possible with instant auto-quoting and near-instant taker acceptance;
  the maker side must be fully automated to confirm inside the 3s HVM window.
  This is the race we currently lose by ~1s.
- **Firehose regime (Jul 10–12, in-play WC volume, 245k prints/day):** the
  median print lands **~30s after the latest RFQ** on that ticker, and RFQ
  instances are DENSER here (232 per traded ticker), which biases this delta
  DOWN — so the true gap to the *originating* request is even larger. At
  scale, most money prints against liquidity that persists ≥30s–minutes:
  rested quotes (`rest_remainder`), re-accepted quotes (no exchange TTL), and
  persistent combo books (e.g.
  `KXMVECROSSCATEGORY-S20266FBC5F12369-122F9781F87` printed at 13:31Z and
  17:29Z the same day, shadow `combo_trades` id 1532395–96). Our 20s
  self-delete TTL removes us from precisely this regime: **57.3% of peak-day
  prints happen later than 23s after the latest RFQ create.**

### Us against that clock

| leg of our loop | value | source |
|---|---|---|
| exchange create → our recorder sees it (WS lag) | p50 **3.09 s**, p90 5.2 s, p99 6.4 s (50k recent RFQs) | `scripts/lens4_tape_basics.py` §B (caveat: recorder host clock vs exchange; 0 negative deltas) |
| exchange create → our quote POSTED (live bot, n=55,145) | p50 **1.64 s**; ≤1s 15.6%; ≤2s 67.5%; ≤3s 84.3% | live DB `decisions.at` (quote_sent) − live `rfqs.raw_json.$.created_ts` |
| our confirm round-trip when accepted | rtt p50 ~100 ms (n=3), decision ~10–138 ms | `confirm.rtt_ms` / `confirm.decision_ms`, final metrics snapshot |
| our POSTs that die on a closed RFQ | 34% (4,128 of 12,136) | `quote.rfq_closed_before_post`, same snapshot |

```
 (pregame/lull regime, Jul 15-16)
   t=0        1s         2s        3s         5s          10s         20s
   RFQ ---|----------|----------|---------|-----------|-----------|----->
 winners: 36.5% already PRINTED
                     48% printed
                                          72.9% printed
      us: 15.6% quoted
                     67.5% quoted (median 1.64s)
                                84.3% quoted            ...our TTL deletes at 20s
```

We are ~1–1.5s behind the winning cohort at the median, and completely absent
from the ≤1s cohort that takes ~36% of the prints. Our quote itself is fine
once up (confirm rtt ~100ms) — the loss is intake + pricing + gate latency
before POST, plus not quoting the flow at all (next section).

---

## 3. (c) Breadth: what trades vs what we quote

### Market-wide combo prints per day (shadow `combo_trades`, single pass)

| day | trades | distinct tickers | taker notional $ |
|---|---|---|---|
| 2026-07-08 | 97,809 | 9,063 | 4.88M |
| 2026-07-09 | 179,818 | 11,486 | 8.55M |
| 2026-07-10 | 233,070 | 13,766 | 12.80M |
| 2026-07-11 | 267,599 | 11,041 | 12.80M |
| 2026-07-12 | 94,390 | 3,126 | 5.49M |
| 2026-07-13/14 | 70 / 150 | 27 / 34 | tiny (recorder poll only; recorder RFQ intake was down) |
| 2026-07-15 | 1,008 | 280 | 44k |
| 2026-07-16 (to 18:12Z) | 17,417 | 1,418 | 918k |

Collection split (all days): `KXMVESPORTSMULTIGAMEEXTENDED` 767,111 trades
(84.5%) vs `KXMVECROSSCATEGORY` 140,801 (15.5%).

RFQ flow for scale: **3.0–3.9M RFQs/day** recorded Jul 6–12 (id-range bisect of
`rfqs`; Jul 12 = 3,946,453 ≈ 45.7/s sustained average). The "47.7 RFQ/s"
firehose (report `2026-07-16-heartbeat-config-fix...md`) is the **all-day
baseline**, not a kickoff burst; the current run's snapshot independently gives
600,222/4h ≈ 41.7/s.

### The overlap hole (Jul 16, `scripts/lens4_overlap_jul16.py`)

| set | size |
|---|---|
| distinct tickers TRADED market-wide | 1,418 |
| distinct tickers WE QUOTED | 6,502 |
| **overlap** | **168 (11.8% of traded)** |
| market trades on tickers we quoted | 2,486 of 17,417 (14.3%) |
| trades printing ≤30s after one of our quote posts | 205 |
| our fills | 9 |

**We quote 4.6× more tickers than the market trades, and still miss 88% of the
traded set.** We are quoting the wrong combos: 85.7% of Jul 16 prints (14,931
of 17,417) were on tickers we never quoted that day.

What the missed traded flow is made of (one RFQ per missed ticker, leg-series
mix; `scripts/lens4_missed_flow_mix.py`):

| leg mix (top) | missed traded tickers | why we skip it |
|---|---|---|
| tennis: KXATPMATCH / KXWTAMATCH / challengers, pure + mixed | ~450 | leg-series allowlist `["KXWC","KXMLB"]` (`filters.allowed_leg_series_prefixes`) |
| KXMENWORLDCUP (champion futures) × KXWCGOAL/ADVANCE/TOTAL/BTTS | ~160 | **`KXMENWORLDCUP` fails the `KXWC` prefix test** — World Cup flow our own gate throws away |
| KXNBASUMMERGAME (+WNBA mixes) | ~70 | allowlist |
| KXUFCFIGHT | 30 | allowlist |
| crypto 15-min (KXBTC15M/KXETH15M/…) 2–5-leg | ~25 | allowlist (and operator sports-only policy) |
| KXMLBGAME pure / ×KXMLBTOTAL | 40 | allowed series but skipped — in-play gate (`skip_inplay_leg` 1.65M on Jul 16), caps, or lost races |
| n_legs of missed traded combos | 2 legs 262, 3–6 legs 664, **7+ legs 324 (26%)** | our `skip_too_many_legs` + risk caps bite on long parlays |

Taker structure: **12,197 distinct RFQ creators in the last 200k RFQs; top-10
= 15.4%** (`creator_id` count) — the firehose is broad retail app flow, not a
few bots. Popular tickers get re-RFQ'd relentlessly: 529,538 RFQ instances
landed on the 1,476 traded tickers of Jul 15–16 (~360 RFQs per traded ticker);
232 per traded ticker in the Jul 10–12 peak. Matched-print rates: **0.34% of
RFQ instances (lull) / 4.46% (peak)** — the combo-RFQ no-trade base rate is
**95.5–99.7%**, far above the ~87% all-RFQ figure from the old repo's data
(memory `project_kalshi_combos_fill_dynamics`); RFQs here are overwhelmingly
the app's *pricing* mechanism, not orders.

---

## 4. (d) API levers we are not using

| # | lever | what it buys | primary source | status |
|---|---|---|---|---|
| 1 | **2-token quotes**: 150 POSTs/s on Advanced | rate limit is a non-issue; we run at ~0.5/s. Quote MORE, reprice MORE — budget allows ~270k quote-writes/half-hour | create-quote.md ("2 tokens per request") + rate_limits.md | unused headroom ~99.6% |
| 2 | **`rest_remainder: true` on CreateQuote** | partial fill rests the remainder as book liquidity — we keep earning after the RFQ dies (the p90+ print tail shows rested liquidity DOES get hit for hours/days) | create-quote.md | we never set it (`exchange/rest.py:215` defaults false) |
| 3 | **Pre-create hot combo markets + rest GTC orders** | `CreateMarketInMultivariateEventCollection` allows **5,000 creations/week**; once a combo market exists its book trades like any market (`batch-create-orders-v2`, GTC + `expiration_time`) → we become the RESTING maker on the ~1.4k tickers/day that actually trade, instead of racing 20s RFQ windows | create-market-in-mvec.md + batch-create-orders-v2.md | unused; needs settlement/fee verification on combo books |
| 4 | **WS `shard_factor`/`shard_key` (1–100)** | N connections each get ~1/N of the firehose server-side → parallel intake escapes the single-loop wall; doc-prescribed remedy for our logged error 25 | websocket-connection.md errors 19–27 | validated? NO (semantics inferred; demo test still owed per 2026-07-14 report) |
| 5 | **`post_only` on quotes** | prevents accidental taking when quoting stale RFQs | create-quote.md | unused |
| 6 | **Subaccounts 0–63** | isolate books/strategies (e.g. RFQ-race book vs resting book) with per-subaccount positions | create-quote.md / subaccounts.md | unused |
| 7 | **Expert tier (600/600)** | earned at 0.075% of exchange volume share — plausibly reachable in combo season at $12.8M/day notional; doubles write budget | rate_limits.md | passive; not needed until #1 is exhausted |
| 8 | **FIX intake** | lossless RFQ broadcast, lower jitter; same token buckets | fix/rfq-messages.md (2026-07-14 review) | deferred (right call — doesn't fix pricing CPU) |
| 9 | **QuoteExecuted `client_order_id`** | exact fill↔quote correlation on the self-scoped quote events | websockets/communications.md | partially used |

**No bulk-quote endpoint exists** for RFQs (llms.txt sweep: batch endpoints are
order-book only). **No longer-TTL knob exists** because exchange quotes have no
TTL at all — persistence is the default; OUR 20s self-delete is the only clock.

---

## 5. What this lens says about the operator goal

1. **The exchange is not the constraint.** Advanced write budget supports
   ~150 quote POSTs/s; we use ~0.5/s. No doc'd cap on open maker quotes. WS
   inbound is free. The binding constraints are self-built: 600ms GIL-bound
   pricing, 2s pool deadline, 20s TTL, 60 open-quote cap, and the allowlist.
2. **Speed and presence split the prize by regime.** In the pregame/lull
   regime ~48% of prints complete within 2s of RFQ create (we POST at 1.64s
   median — close but behind) and ~37% inside 1s (we take ~0% of these). On
   peak volume days — Jul 8–12 carried ~96% of all recorded taker notional —
   only ~6% of prints are ≤2s; **half print ≥30s after the latest RFQ**, i.e.
   against liquidity
   that persisted. That slice doesn't need speed, it needs **presence**
   (no-TTL / rested quotes, persistent combo books — levers #2/#3), which our
   20s self-delete TTL currently forfeits.
3. **Breadth is a bigger hole than speed.** 85.7% of today's prints were on
   tickers we never quoted; roughly half of the missed set is blocked by one
   YAML list (tennis, Summer League, UFC, `KXMENWORLDCUP` mixes — the last one
   is World Cup flow the `KXWC` prefix accidentally excludes). Widening the
   allowlist is an OPERATOR DECISION (sports-only policy, pricing-model
   coverage), but the `KXMENWORLDCUP` exclusion looks like an oversight, not a
   policy.
4. **The RFQ stream is a pricing API, not an order stream.** 95.5–99.7% of
   combo RFQs never print; 12k+ distinct creators; ~360 RFQs per traded ticker.
   Design consequence: prices should be CACHED per leg-set and re-served, not
   re-priced per RFQ (the memo table already does this at 73% hit rate —
   `pricing_stats` `memo_hit_rate: 0.7314`, `live_20260716_fixed.log`), and
   the ~1.4k/day tickers that DO trade deserve resting liquidity, not a 20s
   quote race repeated 360×.

## Caveats

- Trade↔RFQ matching is by bespoke market ticker + latest-preceding RFQ; it
  cannot see WHICH maker's quote traded (quote events are private). "Winning
  maker speed" = RFQ-create→print, an upper bound on the winner's quote time.
- The recorder was down Jul 13–15 (0 RFQs recorded) — the Jul 15–16 window's
  60% unmatched-trade rate is mostly that gap; matched-delta distribution is
  conditioned on RFQs we did record. The Jul 10–12 firehose window has no such
  gap (98.7% of trades matched).
- Recorder `seen_at` lag (3.1s median) is measured on the shadow recorder,
  not the live bot; the live bot's own create→POST (1.64s median) already
  includes its intake lag.
- 13.7% of sampled deletions have no recorded RFQCreated → recorder drops
  under load; lifetime distribution conditions on joined rows.
- `docs.kalshi.com` fetch answers are excerpts read today (2026-07-16); the
  2-token CreateQuote cost and 5,000/week market-creation cap are the two
  numbers everything above leans on — re-verify via authenticated
  `GET /account/endpoint_costs` before building on them (10-token default is
  documented as "currently 10", i.e. mutable).

## NEXT STEPS

- **Owner: us (build, P1).** Presence levers first — they own the peak-day
  regime (57% of peak prints land >23s after the latest RFQ): evaluate
  `rest_remainder: true` on quotes (plumbing exists, defaults false at
  `exchange/rest.py:215`), longer/adaptive quote TTL (exchange imposes none;
  `QUOTE_TTL_S=20.0` at `quote_app.py:128` is ours), and pre-created combo
  markets (5,000/week) with resting GTC orders on the top-N traded tickers.
  All three need risk-engine sign-off (resting liquidity widens the
  mass-acceptance worst case).
- **Owner: us (build, P1, already on the books).** Shard/ProcessPool intake
  (2026-07-14 report NEXT STEPS) — the only path into the ≤1s cohort; demo
  validation of shard semantics still owed.
- **Owner: operator (decisions).** (1) Fix the `KXMENWORLDCUP` allowlist hole
  (one YAML prefix — it is World Cup flow, inside current policy); (2) decide
  whether tennis / Summer League / UFC enter the allowlist (needs pricing
  coverage first — separate lens); (3) confirm 20s TTL vs the 63.5%-of-RFQs-
  dead-by-30s reality — with no exchange TTL, longer-lived quotes on pregame
  combos are free presence if reprice discipline holds.
- **Owner: us (verify).** Authenticated `GET /account/endpoint_costs` +
  `GET /account/limits` once, to pin CreateQuote=2 tokens and the Advanced
  300/300 numbers to our actual account.
