# Settled-leg marginal resolution — the FRAENG dark-bot outage fix (2026-07-18)

## The outage (live, 2026-07-18 ~7:19 PM ET onward, `data/live_logs/live_20260718_final_steer.log`)

FRAENG finished; its leg markets settled/closed and their order books left the
feed. The book still held 9 committed positions, several CROSS-GAME combos with
settled FRAENG legs (e.g. `yes:FRAENG-FRA + no:FRAENG-5 + no:ESPARG-5` — open
until the ESPARG legs resolve Sunday). The book-risk model requires a marginal
for EVERY risk-modeled leg; the settled legs' marginals were missing from the
feed ⇒ `BookModel.unknown` ⇒ every `BookRiskSnapshot` unusable ⇒ the
portfolio-CVaR cap failed CLOSED on EVERY quote (`fallback_reason:
book_risk_unusable`; **366k RFQ audits, 0 quotes in 2h20m**). Same marginals
poison the candidate gate and the peak-profile builder. Without a fix the bot
quotes NOTHING until the final settles.

## The fix

A settled/deterministic leg is not UNKNOWN — its probability is an
exchange-graded FACT: 1.0 if the market settled YES, 0.0 if NO.

```
                     _marginals(ticker)  (rfq/lifecycle.py — the ONE provider)
                              |
              +---------------+----------------+
              | feed book valid?               |
              |   yes -> microprice (as today) |
              |   no  -> settled cache?        |
              |            hit  -> 0.0 / 1.0   |  <- graded FACT, permanent
              |            miss -> None (UNKNOWN, fail-closed as today)
              |                    + note_missing(ticker) iff COMMITTED leg
              +--------------------------------+
                              ^
   maintenance tick: _maybe_resolve_settled_marginals()  (single-flight,
   bounded, off the hot path)  ->  SettledMarginalResolver.resolve_pending()
     GET /markets/{ticker} (public)  ->  result yes/no under status
     determined|finalized  ->  cached PERMANENTLY (a settlement never changes)
```

| item | desc | status |
|------|------|--------|
| `marketdata/settled.py` (NEW) | `SettledMarginalResolver`: permanent cache + bounded off-loop fetcher; `resolved()` sync hot-path read; graded = `result∈{yes,no}` AND `status∈{determined,finalized}`; cross-checks `settlement_value_dollars` when present; scalar/inconsistent ⇒ permanently unresolvable (fail-closed); closed/disputed/amended/error ⇒ retry on backoff; live statuses dropped (feed owns them) | SHIPPED |
| `rfq/lifecycle.py` | `_marginals` fallback (feed → settled cache → UNKNOWN); committed-leg-only registration (generation-cached set); `_maybe_resolve_settled_marginals` on the maintenance tick (book-risk-task pattern) | SHIPPED |
| `sim/structural_book.py` | degenerate (0/1) marginals NEVER enter Dixon-Coles inversion targets — a settled leg rides the copula as a CONSTANT column (the exact conditional treatment) | SHIPPED |
| `sim/book_risk.py` | P1.9 structural challenger `_shock_marginals` passes a 0/1 fact through UNSHOCKED (a feed error cannot apply to a graded result) | SHIPPED |
| `ops/config.py` | `risk.settled_marginal_resolution: bool = True` + `risk.settled_resolution_retry_s: float = 30.0` (positive-finite validator) | SHIPPED |
| `ops/quote_app.py` | `build_settled_resolver` (pure knob→wiring builder, `build_lifecycle_config` precedent) + threaded into the lifecycle in both paper and quote modes | SHIPPED |
| NOT touched | caps semantics, waiver, det-max, markup/tiers, filters, skew/peak steer (consumes marginals — benefits automatically), fill-recovery region | pinned by full-suite green |

## Why the conditional risk is now CORRECT

Feeding the exact 0/1 into `build_book_model` makes every MC number conditional
on the settled facts, with no new risk code:

- **Settled leg LOST** (its selected side lost): the sampled leg column is a
  constant that zeroes the payout product ⇒ the parlay is deterministically
  dead ⇒ as its seller we keep the premium in every scenario — **zero further
  loss** (test pins `es_99 == 0`, `p_profit == 1`).
- **Settled leg WON**: the column is a constant 1 ⇒ the combo payout is the
  product over the REMAINING legs — **full conditional exposure** (test pins
  `es_99 == the full premium` on a one-live-leg combo).
- UNRESOLVED-but-closed (game over, Kalshi not yet graded): stays UNKNOWN —
  outcomes are NEVER inferred from scores/feeds; only exchange-graded results
  count. If Kalshi settles the COMBO first (the early-NO path observed
  2026-07-10), the settlement poller removes the position and the leg simply
  stops being queried — both grading orders are graceful.

## API facts used (hard rule 4 — verified against the LIVE spec, 2026-07-18)

Fetched `https://docs.kalshi.com/openapi.yaml` this session; matches
`docs/api-notes/index-scan.md` §5/§10 exactly (NO discrepancy):

- `GET /markets/{ticker}` → `market.result`: enum `yes | no | scalar | ''`
  (empty until determined).
- `market.status`: enum `initialized|inactive|active|closed|determined|
  disputed|amended|finalized`; `settlement_timer_seconds` = "time after
  determination that the market settles" ⇒ `determined` = outcome graded
  (mathematically locked), `finalized` = settled.
- `market.settlement_value_dollars` (nullable): "settlement value of the
  YES/LONG side … Only filled after determination" — used as a consistency
  cross-check only.

Assumption-audit rows appended to `NOTES.md` (SR1–SR4).

## Verification

| check | result |
|-------|--------|
| new `tests/test_settled_marginals.py` | **17/17** (resolver cache/retry/fail-closed × 8, structural exclusion × 2, lifecycle wiring × 6 incl. the 9-position outage-shape e2e through `handle_rfq`/`_book_risk_for_check`, knob/validator × 1) |
| book-risk / lifecycle / structural / config / quote-app suites | 397/397 |
| FULL suite | **2446 passed** (baseline 2429 + 17 new), 3 deselected (integration, unchanged) |
| ruff + mypy --strict on all 6 touched files | clean |

## Residual risks

- A `determined` result is cached permanently; Kalshi's dispute window could in
  principle flip it before `finalized` (rare). Backstop: the settlement
  poller's to-the-cent reconcile still HALTs on any real settlement mismatch
  (defense #3), and the cached marginal only feeds RISK, never a settlement
  booking.
- `disputed`/`amended` statuses deliberately stay UNKNOWN (retried); a market
  parked there keeps its combos' snapshots unusable — fail-closed by design.
- Live-market fetches (a committed leg whose book merely flickered) cost one
  public GET then drop from pending — bounded (5/pass, 512 pending cap).

---

## ADDENDUM (2026-07-18 relight, ~02:17Z / 10:17 PM ET 07-17): marginal-jump breaker missed — HARD HALT 90s after preflight — FIXED

The relight on `a57afc3` worked (`settled_marginal_resolved` fired, snapshot
healed) but the **marginal-jump circuit breaker** was a missed consumer
(`data/live_logs/live_20260718_settled_relight.log`):
`halt_marginal_jump` — `"KXWCTOTAL-26JUL18FRAENG-4: marginal became unreadable
(had 1.000)"` → 30s transient grace → `circuit_breaker_tripped_sustained` →
kill switch → clean shutdown (supervisor rc=1).

Mechanism: the breaker sampler DOES read the patched provider
(`lifecycle.marginal_of`), so the residual gap was the window where the book
is gone but the exchange has NOT graded yet (`status=closed`, `result=""` ⇒
marginal legitimately None) — the baseline 1.000 came from the market's last
live echo, and "readable → unreadable" sustained 30s is the breaker's
dead-feed trip. For a settled/closing market that transition is NORMAL AND
PERMANENT.

Fix (smallest sound change — exempt exchange-confirmed non-live tickers from
the jump/readability watch):

| item | desc | status |
|------|------|--------|
| `marketdata/settled.py` | resolver now tracks `NON_LIVE_STATUSES` knowledge (`closed/determined/disputed/amended/finalized` seen on a successful fetch, incl. closed-but-UNGRADED and scalar) + public `market_no_longer_live()`; a graded fact counts too | SHIPPED |
| `rfq/lifecycle.py` | public `settled_watch_exempt(ticker)` — True iff the resolver holds the fact OR the exchange confirmed non-live; False with no resolver / nothing confirmed (full fail-closed watch retained) | SHIPPED |
| `risk/breakers.py` | `BreakerInputs.settled_tickers` (default empty = pre-fix contract); `_evaluate_marginal_jumps` SKIPS exempt tickers and PURGES their baseline — held fact ≠ jump, live 0.97 → graded 1.000 ≠ jump, unreadable-while-closed ≠ dead feed | SHIPPED |
| `ops/quote_app.py` | `_book_leg_signals`/`_sample_breaker_inputs` build + thread the exempt set from `settled_watch_exempt` | SHIPPED |

Quote path unaffected: an ungraded closed leg still prices UNKNOWN (no-quote);
only the BREAKER stops reading the close transition as a feed failure. A
genuinely dead feed on a LIVE market still trips + sustains + halts
(regression-pinned).

Consumer audit (every reader of the marginal provider / raw feed books vs
settled tickers — so no third layer surfaces):

| call site | verdict |
|---|---|
| breaker sampler `_book_leg_signals` → jump watch | **PATCHED** (exempt set + skip/purge) |
| `_build_book_risk_inputs` → `build_book_model` | patched (the original fix — conditional model) |
| `_build_candidate_gate_inputs` marginals dict (candidate MC) | already-safe (inherits provider; facts flow) |
| `_build_state_worst_case_inputs` (waiver enumeration) | already-safe (facts flow; degenerate excluded from inversion; per-state settlement marginal-free) |
| `_maybe_recompute_peak_profile` marginals dict | already-safe (facts flow; absent ⇒ neutral zero-adder, never a decline) |
| `limits.check` / `reservation.try_reserve` / `exposure.snapshot` (skew) | already-safe (provider inherited; settled 0/1 = exact conditional folds) |
| `_refresh_daily_pnl` mark | already-safe + benefits (settled legs mark at fact) |
| `_current_leg_mids` (RFQ legs) | N-A (exchange mints no RFQs on settled legs) |
| `_last_look_inputs` max-move (open-quote legs) | already-safe (a settled move ⇒ last-look declines — fail-safe) |
| `ops/report.py build_report` | already-safe (observability only, no halt path) |
| breaker `game_keys` → unmapped-game | N-A (settled legs keep event tickers; key resolves) |
| `_book_tripwire` taxonomy | N-A (shape-only, settlement-independent) |
| `_metadata_changes` fingerprint (HALT_METADATA_CHANGE, structural/no-grace) | already-safe TODAY: peek-only, and metadata is fetched only for legs NEW to the watch set — nothing refreshes a held leg's meta, so the active→closed flip never lands in the fingerprint. **Watch item**: if a future sweep refreshes held-leg metadata, exempt the normal close progression first |
| `pricing/engine`, `pricing/legs`, `rfq/filters`, `_book_valid` raw `feed.book` reads | N-A / fail-safe (RFQ + open-quote legs only; missing book ⇒ NoQuote/decline/cancel, never a halt) |

Verification: 8 new tests (breaker public-path: fact-held, the exact 02:17Z
unreadable-while-closed sequence past grace, live→graded 0.40 delta no-trip,
same delta WITHOUT exemption still trips, dead-feed LIVE regression halt;
wiring: resolver liveness knowledge, lifecycle exemption incl. no-resolver
False, real sampler surfaces the set). Suites: settled file **25/25**,
breakers/quote_app/lifecycle/book-risk **173/173**, FULL suite
**2454 passed** (2446 + 8), 3 deselected. ruff + mypy --strict clean.

---

## ADDENDUM 2 (relight2, 02:40–02:57Z): registration stall — batch registrar + never-fetched priority + pending observability

Relight2 evidence (`live_20260719_relight2.log`): only THREE
`settled_marginal_resolved` (BTTS, TOTAL-3, TOTAL-4, all 02:40:19–20) in 25
minutes; **42,377 `book_risk_unusable` audits to log end**; zero
`settled_fetch_failed`. Direct exchange probe (this session): **every FRAENG
leg market was `finalized` HOURS earlier** (1H legs 21:55Z, TOTAL-1..6
21:24–22:34Z, ADVANCE/SPREAD 23:04–23:05Z, Mbappé GOAL 23:16Z) — the facts sat
graded on the exchange while the bot stayed dark. Champion legs
(`KXMENWORLDCUP-26-AR/-ES`) are genuinely `active` until Sunday.

**Root cause (proven by offline repro + tape):** the resolver's fetch loop is
CORRECT — replaying the exact live book shape with FULL registration resolves
every graded leg in ≤3 passes (budget 5). The stall was REGISTRATION: it rode
the serial marginal-provider walks, and in the live process only the first
~5 walk-order tickers ever entered the fetch queue (the exact trio+2 pattern
in the tape: 3 graded resolved, 2 active legs silently dropped by the
live-status branch). Every later leg — including 9+ already-graded FRAENG
facts — waited on a serial walk that sat stuck behind UNGRADED/active
blockers (`_refresh_daily_pnl` returns at the FIRST unmarkable leg; the
live-pop cycle was silent, so the operator was blind to it).

| item | desc | status |
|------|------|--------|
| `rfq/lifecycle.py _register_settled_candidates` | BATCH registrar on EVERY maintenance tick: every committed leg with a dark feed book and no fact registers at once — startup, every position-generation change, continuous self-heal; never gated on any serial walk (provider-walk noting stays as belt-and-braces) | SHIPPED |
| `marketdata/settled.py resolve_pending` | per-pass PRIORITY: never-fetched tickers beat backoff retries for the budget (stable sort — insertion order within tiers), so a freshly-registered graded fact lands within ~2 passes no matter how many ungraded tickers cycle; attempts counter dropped with the pending entry | SHIPPED |
| `marketdata/settled.py` observability | INFO `settled_resolution_pending {n_pending, n_never_fetched, sample}` once per pass over a non-empty pending set — the line that would have answered relight2 instantly | SHIPPED |

Verification: offline repro (live book shape, 16 tickers, graded+active mix)
resolves ALL graded in 3 passes; 4 new tests — batch registrar registers all
10 dark legs in one call; never-fetched priority (5 new beat 6 due retries,
fetch order pinned); the full relight2 shape e2e through `maintenance_tick`
(9 positions, ungraded-first leg order → all 9 graded facts within TWO
passes, blocker stays pending, snapshot unusable while it blocks
[fail-closed pinned], flips usable + `es_99==0`/`p_profit==1` the pass after
the exchange grades it); the pending log line emitted with counts + sample.
Suites: settled file **29/29**, related **183/183**, FULL **2458 passed**
(2454 + 4), 3 deselected. ruff + mypy --strict clean.

---

## ADDENDUM 3 (relight3, `live_20260719_batchfacts.log`): valid-but-EMPTY husk books — shared feed-readability predicate

Relight3 confirmed the batch registrar + priority + pending line work (pending
burned to 1, ESPARG live-drops silent) but 9 exchange-finalized FRAENG legs
(`1HSPREAD-FRA2`, `1HTOTAL-2/3`, `ADVANCE-FRA`, `GOAL-MBAPP`, `SPREAD-FRA2/3`,
`TOTAL-1/5`) STILL never registered → snapshot still unusable.

**Root cause confirmed in code** (`marketdata/orderbook.py:61-77`):
`TopOfBook.microprice()` returns **None whenever either book side is empty**.
A settled market can retain a VALID-but-EMPTY (or one-sided) husk mirror in
the feed. The registrar tested "has a valid book object" (`book.valid`) and
skipped those legs as feed-owned, while the provider's read
(`book.top().microprice()`) returned None — an early return that ALSO
bypassed the settled-cache fallback. Predicate mismatch ⇒ unreadable-but-
book-present legs never registered ⇒ permanent UNKNOWN.

| item | desc | status |
|------|------|--------|
| `rfq/lifecycle.py _feed_marginal` (new) | THE single feed-readability predicate: book missing OR invalid OR unpriceable top (empty/one-sided ⇒ microprice None) ⇒ None. `_marginals` serves the feed iff it returns a price; the registrar registers iff it returns None — single source of truth, can never diverge again | SHIPPED |
| `rfq/lifecycle.py _marginals` | now falls through to the settled-fact cache even when a husk book lingers (the old early return served the husk's None and shadowed a cached fact) | SHIPPED |
| `rfq/lifecycle.py _register_settled_candidates` | consumes `_feed_marginal` instead of `book.valid` | SHIPPED |

Verification: 3 new tests — (1) the exact relight3 shape: 9 committed legs
with PRESENT-but-unpriceable books (valid-empty husks + invalid mirrors),
exchange-finalized ⇒ registered on the next tick, resolved within TWO passes,
snapshot usable; (2) the shared-predicate property over every feed state
(no-book / invalid / valid-empty / valid-one-sided / full two-sided):
registrar-registers ⟺ provider-feed-read-is-None, with only the priceable
book feed-owned; (3) a cached fact serves THROUGH a lingering husk book.
Suites: settled file **32/32**, related **198/198**, FULL **2461 passed**
(2458 + 3), 3 deselected. ruff + mypy --strict clean.

## NEXT STEPS

- **Operator**: relight. Watch order: `settled_resolution_pending` (the full
  pending set is now visible immediately — expect the remaining FRAENG legs +
  the active ESPARG/champion legs in the sample) → a burst of
  `settled_marginal_resolved` within ~2 passes (seconds) →
  `book_risk_snapshot` usable → quotes; and still NO `halt_marginal_jump`
  holds on settled legs (Addendum 1). Live/champion legs keep cycling
  silently on the 30s backoff until their books subscribe — expected, and now
  visible in the pending line's counts.
- **Operator decision owed**: none — no new knobs; everything rides
  `risk.settled_marginal_resolution` (False ⇒ no resolver ⇒ pre-fix
  behaviour everywhere).
- **Next session**: after the Sunday final settles, confirm the settlement
  poller's combo reconcile agreed to the cent with the resolver's cached leg
  facts (both grading orders exercised live); close the `_metadata_changes`
  watch item if a held-leg metadata sweep is ever added.
