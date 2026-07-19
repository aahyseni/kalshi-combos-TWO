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

## NEXT STEPS

- **Operator**: restart the bot to pick up the fix (edits are inert until
  restart); watch for `settled_marginal_resolved` lines for the FRAENG legs,
  then `book_risk_snapshot` usable=true and quotes flowing while ESPARG legs
  stay open (Sunday's exact scenario).
- **Operator decision owed**: none — knob defaults ON
  (`risk.settled_marginal_resolution`); set `false` in the local YAML to
  restore pre-fix behaviour.
- **Next session**: after the Sunday final settles, confirm the settlement
  poller's combo reconcile agreed to the cent with the resolver's cached leg
  facts (both grading orders exercised live).
