# Risk PHASE 5 — Quoting policy: inventory skew (DARK) + widen-vs-DECLINE + pregame precision ladder

**Date:** 2026-07-13
**Branch:** `risk-phase5` (based on `main@324a74f`)
**Suite:** 1501 passed / 0 failed / 3 deselected (integration) — was 1462 at base (**+39**).
**mypy:** `uv run mypy src` clean w.r.t. Phase 5 (only the 2 PRE-EXISTING
`pricing/ising_amm.py` `no-any-return` errors remain — verified present at base
`324a74f`; no file I touched adds an error).
**ruff:** `uv run ruff check` clean on every touched file.

Everything ships **SHADOW / DARK** — **zero live behaviour change**. The 1462
pre-existing tests pass verbatim, and a dark-ship regression test asserts the
emitted quote is **bit-identical** whether the skew is wired-but-disabled or not
wired at all.

---

## What shipped (three seams, all fed but dark)

```
                 RFQ ──► filter (pregame M_q) ──► price ──► [SKEW] ──► [WIDEN?] ──► risk gate ──► CreateQuote
                          │                                    │          │
                          │                          shade no_bid    decline near-cap
                          │                          (dark ⇒ 0)      concentrating (shadow ⇒ log)
                          ▼                                                                        │
                 log time_to_start on pregame decline (flow-loss datum)                           ▼
                                                                       … accept ──► LAST LOOK (pregame M_c ≥ M_q) ──► confirm
```

### (A) Inventory-aware skew — NEW pure module `risk/skew.py` (R3 Part A)

`compute_inventory_skew(candidate, snapshot, marginals, conventions, limits,
params, *, cache) -> InventorySkew` — a sibling of `lastlook.decide_confirm`: no
I/O, no clock, driven off the per-GAME aggregates the lifecycle already computed
for its limit check.

**THE SIGN (load-bearing, R3 §A0/§A6).** For our SELL-ONLY NO book the only live
lever is `no_bid`, and the pricer applies `no_raw = ($1−fair) − half − fee_no +
skew`:

| flow | skew | effect on `no_bid` | why |
|------|------|--------------------|-----|
| **CONCENTRATING** (candidate ADDS to a game's net direction) | `>= 0` (widen) | more expensive combo | **sell LESS** of what we're already loaded on |
| **OFFSETTING** (candidate OPPOSES the net) | `<= 0` (tighten) | cheaper NO | **win MORE** of the flow that flattens us |
| **empty-book game** | exactly `0` | — | nothing to concentrate into or offset |

```
skew_cc =  Σ_game w_conc · d_e · f(util)          # concentration ≥ 0, convex f(u)=u**γ
         − Σ_game w_off  · min(d_e,|net|) · util   # offset rebate ≥ 0, bounded
  clamped to [−skew_max_tighten_cc, +skew_max_widen_cc]     (defaults 150 / 600)
```

- `util` = the tightest of {delta, worst-case-loss, gross-notional} per-game
  utilisation vs the SAME enforced per-event caps the `LimitChecker` uses — so
  the convex ramp means the last combos before a limit pay the most widen.
- Candidate per-game delta from `analytic_leg_deltas` (hot path). An **OPTIONAL**
  slow-path `GameSkewCache` (populated off the hot path from the Phase-4
  `sim/book_risk` per-game ΔES) may override the per-game **direction**; absent, it
  falls back to the analytic sign. **Correlations/ΔES are never invented here.**
- The offset rebate is the dangerous side — it is **doubly contained**: capped at
  `skew_max_tighten_cc` here AND by the free-money clamp in `construct_quote`
  (`quote.py:202`) which fires AFTER skew and re-checks the capture invariant.

**DARK SHIP.** `SkewConfig.enabled=False` default ⇒ the honest skew is
**computed + logged** (`inventory_skew_shadow`) but `InventorySkew.applied_cc`
returns **0** — a zero-P&L shadow classifier. `lifecycle.handle_rfq` computes the
skew from `self._exposure.snapshot`, logs it, and re-prices with `applied_cc`
(a bit-identical no-op while dark).

**The three mandatory property tests (`tests/test_skew.py`, R3 §A6):**

1. **SIGN SAFETY** (load-bearing) — `TestSignSafety`: an offsetting candidate ⇒
   `skew ≤ 0`, a concentrating one ⇒ `skew ≥ 0`, an empty-book game ⇒ **exactly
   0**; caps never exceeded (`±600 / ±150`); a property sweep proves the honest
   skew always lands in `[−tighten, +widen]`.
2. **SELL-ONLY invariant survives** — extended the `test_quote.py` sell-only fuzz
   (`test_yes_bid_is_always_zero`) with an explicit comment that its `±6000` skew
   sweep is a **superset** of `compute_inventory_skew`'s live range
   `[−skew_max_tighten_cc, +skew_max_widen_cc]`, so `yes_bid == 0` is proven to
   survive ANY skew the risk engine can emit (incl. a large negative one).
3. **NO-ARB survives** — new `TestSkewNoArb` in `test_quote.py`: a deep negative
   (tightening) skew never lets `no_bid` exceed the free-money cap − margin; the
   clamp fires and the capture check still passes (property + a worked case).

### (B/R2) Widen-vs-DECLINE policy — `risk/skew.decide_widen_or_decline` (R3 Part R2)

Pure per-GAME verdict: **DECLINE** (rather than post a wide quote) when SOME game
the candidate touches is BOTH (a) **concentrating** — the skew's per-game
contribution for it is `> 0` — AND (b) **near its cap** (`util ≥ util_threshold`,
default 0.75). Widening a thin quote into a near-cap game only attracts hitters
(our own P&L-sweep finding).

- **Per-game, not aggregate**: a candidate that concentrates a near-cap game
  declines even if it OFFSETS a different un-stressed game — the near-cap game is
  the risk. A game the candidate only offsets is **never** a decline trigger
  (that flow balances the book).
- **SHADOW by default** (`WidenConfig.enabled=False`): `would_decline` is logged
  (`widen_vs_decline_shadow`) with zero live impact; the quote still goes out.
  When enabled, `handle_rfq` records `SKIP_WIDEN_AVOIDED` (NEW reason code) instead
  of quoting. Consistent with the Phase-2 `Breach.shadow` / `_partition_breaches`
  split — one flag flips it live.

### (B) Pregame precision ladder — `rfq/schedule.py` + `rfq/pregame.py` (R3 Part B)

**(a) `ScheduleCache` SEAM** (sibling of `MetadataCache`): an explicit
`event_ticker → scheduled UTC start` table, peek-only, hot-path safe. **INACTIVE
by default** (empty ⇒ a miss on every leg). Fail-closed rules: NO fuzzy matching
(exact `event_ticker` key), a miss falls through to the estimate ⇒ UNKNOWN ⇒
decline, and a **naive datetime is rejected at insert** (no clock = would
misgate). The feed that POPULATES it + its hard-rule-5 API verification is
**DEFERRED**; this class is the interface it will fill.

**Precision LADDER** (`pregame.leg_start` now returns `LegStart{start, precise}`):
`embedded-ET (precise) → schedule feed (precise) → estimate (NOT precise) →
UNKNOWN`. The margins below apply ONLY to a **precise** start; the estimate
already bakes in its 4.5h buffer, so stacking a margin on it would double-count.

**(b) M_q / M_c split** (config `FiltersConfig`, per-prefix overrides):
- **quote-cutoff margin `M_q`** — stop quoting `M_q` before start (the flow knob;
  applied by `PregameGate.status`, used at the quote-time filter).
- **confirm-cutoff margin `M_c ≥ M_q`** — the safety knob, applied at LAST LOOK by
  `PregameGate.confirm_status` (`decline if now ≥ start − M_c`), wired via
  `_last_look_inputs` using the new `RfqFilter.pregame_confirm_status` — a
  **pure-function change, no new I/O**. A pydantic validator enforces `M_c ≥ M_q`
  (confirm never looser than quote).
- **CONSERVATIVE DEFAULTS**: both margins default `0.0`, keeping the estimate at
  4.5h (MLB 4.0). The embedded-ET path keeps today's exact behaviour and the
  schedule tier is inactive — **no live tightening without a verified feed**.

**(c) Flow-loss logging**: `time_to_start_s` is now recorded on EVERY pregame
decline — at the quote-time gate (`_pregame_flow_context` on `SKIP_INPLAY_LEG` /
`SKIP_START_TIME_UNKNOWN`) AND at confirm (`_record_confirm_decision` on
`DECLINE_INPLAY_LEG` / `DECLINE_START_TIME_UNKNOWN`). This is the pure-counting
input for the deferred flow-loss-vs-buffer study — zero P&L, runnable on the
decision log.

---

## What ships DARK vs what's DEFERRED

| Piece | State |
|---|---|
| Inventory skew computed + logged every quote | **SHIPPED, DARK** (`applied_cc=0`) |
| Widen-vs-decline would-be decision logged | **SHIPPED, SHADOW** (quote still goes out) |
| `ScheduleCache` seam + precision ladder + M_q/M_c split | **SHIPPED, SEAM** (empty cache, 0 margins ⇒ today's behaviour) |
| `time_to_start` on pregame declines | **SHIPPED, LIVE** (pure logging, zero P&L) |
| Config wired end-to-end (`quote_app`), demo+prod YAML load clean | **SHIPPED** (all dark) |
| **Enabling the skew** (needs shadow-markout validation: does it reduce portfolio CVaR without an adverse markout?) | **DEFERRED** — never refit on a P&L window |
| **The schedule-feed data source** + its hard-rule-5 API verification + the `event_ticker→fixture` table population | **DEFERRED** |
| **Tightening M_q/M_c** off a measured pooled game-clustered markout study (`tools/`, not live) | **DEFERRED** |
| **Payout-axis caps A4** (`max_combo_payout`, committed-payout kill-switch) | **DEFERRED** — largely covered by the Phase-2 utilization backstop; noted, not duplicated |
| ΔES cache population from `sim/book_risk` (the `GameSkewCache` is wired to read it; nothing populates it yet) | **DEFERRED** |

---

## Files changed

**New (src):** `risk/skew.py` (skew + widen policy, pure), `rfq/schedule.py`
(`ScheduleCache` seam).
**New (tests):** `test_skew.py` (16), `test_pregame_precision.py` (16),
`test_quoting_policy.py` (5 lifecycle integration).
**Edited (src):** `pricing/quote.py` was NOT touched — the `inventory_skew_cc`
seam was already plumbed; the skew now feeds it from the lifecycle.
`rfq/pregame.py` (ladder + margins), `rfq/filters.py` (schedule inject +
confirm-status + time-to-start), `rfq/lifecycle.py` (`_quoting_policy` skew+widen,
M_c confirm gate, flow-loss context), `ops/config.py` (`SkewConfig`, `WidenConfig`,
M_q/M_c fields + validators), `ops/quote_app.py` (wire params, all dark),
`core/reasons.py` (`SKIP_WIDEN_AVOIDED`).
**Edited (tests):** `test_quote.py` (fuzz comment + `TestSkewNoArb`).

---

## NEXT STEPS

- **Owner: operator** — decide whether to merge this DARK branch to `main` (no
  behaviour change; it is the seam + shadow logging for the next study).
- **Owner: next session (`tools/`, never live)** — run the shadow-markout study:
  (1) does the logged `inventory_skew_shadow` correlate with portfolio-CVaR
  reduction? (2) does buffer-admitted near-kickoff flow show an adverse
  short-horizon markout? Both pooled, multi-week, game-clustered — **never refit
  on a P&L window**. Only a PASS flips `skew.enabled` / tightens `M_q`/`M_c`.
- **Owner: next session** — build the schedule-feed data source for ONE sport that
  lacks an embedded token (soccer/WC costs the most flow), with the same
  hard-rule-5 API cross-check the embedded-ET path got, then populate the
  `ScheduleCache` + the explicit `event_ticker→fixture` table.
- **Owner: operator** — decide on the payout-axis caps A4 (deferred; overlaps the
  Phase-2 utilization backstop).
