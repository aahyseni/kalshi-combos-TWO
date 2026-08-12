# 2026-08-12 — DEFERRED-UPDATES LEDGER: everything parked until the 8/31 month-of-data review

**Operator ruling (2026-08-12, verbatim):** "i want to run the bot for at least
a full month (until august 31st) and after that i think 1 month of data is good
enough to decide what to do from then on for risk and more stuff. keep all
these updates that are being added or pushed off in a report for the future in
case we need to add it again."

**What this means in practice:** the live engine runs UNTOUCHED on the current
posture (main `cb48a5c` code = `27ccd65` engine + docs; the armed-flag state in
`config/prod-live-wc.local.yaml` as audited in the 8/12 readout) through
**2026-08-31**. Every change that was built, ratified, or identified — including
the two the operator ratified earlier today — is DEFERRED and recorded here
with an exact resume recipe, so nothing has to be re-derived on 9/1. The only
work during the window: **sports onboarding** (operator-directed, pricing-side,
per-sport allowlist-gated — see the companion sports report) and read-only
analysis/diagnostics.

**Nothing in the risk path was edited today.** The scoping for item 2 stopped
before any code change; the working tree is clean at `cb48a5c`.

---

## A. Ratified but DEFERRED (operator said yes, execution parked)

### 1. Utilization-backstop repair — RATIFIED 8/12 ("Ratify: fix + derive")
- **Defect:** the gross-settlement-notional ≤ 3×bankroll wall sums notional
  PER GAME, so a 3–4-game combo counts 3–4× (measured ×3.6 on the live book);
  notional ≠ risk on a prepaid sell-only book; taxes cheap NO ~2.5×/premium-$.
- **Evidence:** 183k util=1.00 events + 123k cap-skips vs 36 accepts (8/5);
  1,273,036 skips on 8/11 alone; binding harder as bankroll grows.
- **Resume recipe:** count notional ONCE per combo (port the slate-cap
  partition repair — `risk/exposure.partitioned_worst_case_cc` pattern — onto
  the utilization axis / `gross_settlement_notional_by_game_cc` consumers);
  derive/ratify the multiple as a layer-2 anchor (its real job: catastrophic
  convention-misread damping — thin, conventions are ledger-verified). Rule-8
  prototype in `tools/` → tape replay → port dark → parity → vitals fast +
  pre-ship → VALIDATE-caps-can-quote against real trade sizes → worktree
  self-containment proof → staged arming line.
- **Expected effect:** frees ~70% of the top decline wall on busy slates.
  NOTE: this raises volume, which makes item 3 (marginal gates) more relevant —
  the broken wall is currently doing accidental risk work. Sequence them
  consciously on 9/1.

### 2. Delete the static $500/day loss stop — RATIFIED 8/12 ("Delete it")
- **Defect:** hand-set `max_daily_loss_dollars: 500.0` contradicts the 8/5
  ruling ("daily pnl should never matter" — the frac form is already disarmed
  at `daily_loss_frac: "1.0"`), and its day-P&L input is restart-scoped: the
  8/11 02:16 ET relight reset the counter after 9 trips and it stayed silent
  the rest of the −$917 day. A halt that a relight silently launders.
- **Exact edit sites (scoped 8/12, no edits made):**
  `src/combomaker/risk/limits.py:182` (field), `:1263–1270` (the check →
  `HALT_DAILY_LOSS` breach), `:269–270` (comment referencing it);
  `src/combomaker/ops/config.py:2642` (knob), `:3480` (plumbing);
  tests: `test_caps_enforced.py:88,111` + `test_risk_shadow_mode.py:198,219,
  242,265` (drop the kwarg), `test_limits.py:253–289`
  (`TestAllBreachesReported` expects `HALT_DAILY_LOSS` — rewrite expectations),
  `test_review_fixes.py:174–183` (`test_daily_loss_halts_via_maintenance` —
  delete or repoint at the frac form). **`ReasonCode.HALT_DAILY_LOSS` STAYS**
  (the R2 frac form at `limits.py:1700` still emits it).
- **Gates:** vitals fast (+ pre-ship — halt-path change), full suite, worktree
  build + self-containment proof on the pushed tree, merge at a restart
  boundary. Companion (optional, reporting-only): wire the day-P&L seed
  (item B.3) so any future daily measure survives restarts.

## B. Built/staged and DARK (awaiting arming decisions — several predate today)

### 3. Marginal KILL + ruin gate arm — operator "NOT YET" (8/12), revisit ≥9/1
- The 2% night-tail budget is currently **telemetry only** (`p_kill_night`
  peaked 0.25–0.50 every big-slate day of the away week; kill-tail decline
  reasons fired 0× in 16.63M candidate rows — full audit:
  `2026-08-12-loss-day-forensics-and-kill-budget-audit.md`). Operator keeps
  the capacity-walls-only posture knowingly; revisit after new-sports
  diversification lands and with the month of data.
- **Resume recipe:** build the acceptance-seed fix (seed the eviction/
  acceptance table from the store's measured history at boot — the
  day-one-empty-tape defect), then flip the FOUR staged lines together at one
  restart (local yaml footer ~1032–1052): `kill_anchored_book_gate: true`,
  `kill_gate_marginal: true`, `ruin_gate_marginal: true`,
  `portfolio_ruin_prob_budget: "0.05"`; revert `portfolio_det_max_frac`
  "0.70" → "0.36". Quiet-machine pre-ship ritual + first-hour watch items per
  the 8/1 readingb/ruin-gate reports.
- **Expected effect:** over-budget books stop ADDING tail (diversifiers/
  reducers still quote; concentrators declined); both P&L tails narrow —
  fewer −$900 nights AND fewer +$1,100 nights.

### 4. P1 Stage-1 per-STRUCTURE + per-game-DIRECTION accumulated bounds
- Designed (7/25 dossier), evidenced **4×** (7/31 −$143.89 whale; 8/1 win-side;
  8/6 top-5 = 96% of net; 8/9 two 15–19¢ KS longshots, $0.55 EV → +$390.78).
  Caps the near-zero-EV longshot/whale seam at the reservation path.
  Validate-can-quote before arming.

### 5. Other dark/staged flags (unchanged state, for completeness)
| item | state | note |
|---|---|---|
| `open_quote_capacity_derived` | staged, commented out | arm order: shadow one slate → on; pairs with withdraw-budget raise (#7) |
| `skew.conc_armed` | false (shadow) | armed 7/27 then reverted (collapsed discrimination 74%) |
| `skew.pbook_armed` | false | disarmed 7/27 (no-book-size-in-price directive) |
| `book_risk_stale_decay` | false | "removed positions only lower loss" defect must be fixed first |
| adaptive_caps_mode | shadow since 7/23 | enforce after MLB σ₁/ρ measured — a natural 9/1 candidate with the month of data |
| peak-skew arming flag | code change owed | peak_cc has no flag; the hot-key small-book widener (acceptance collapse tail) |
| derived resting floor (whole-book) | designed, NOT built | `resting_floor_count: 1` stopgap stands; burst_floor_derived stays WITHDRAWN |

## C. Identified defects/builds queued (no ratification needed, parked with the rest)

| # | item | evidence | shape |
|---|---|---|---|
| 6 | day-P&L seed wiring (`position_ledger` has no live writers; p_night restart-roll is a no-op; settlements-while-down never booked) | 7/25 §5-HIGH; 8/11 halt-laundering | slow-loop, fix-isolation |
| 7 | withdraw-budget raise (200 tok/10s = 6.7% of documented tier; capacity withdraw-bound at ~191 slots) | 8/1 G1 | operator decision + one knob, then derivation self-scales |
| 8 | `insufficient_balance` back-off (pause new creates while cash-exhausted) | 52,490 doomed retries on 8/11; 2,233 in the final 14 min pre-reboot | small, isolated, mechanism not knob |
| 9 | accounting sweep extension: invisible-settlement class (+12 tickers/+$170.07 in the away week; ~20 lifetime), multi-fill pl de-dup (−$132.24 era class), the 2 esports `rehydrate_reconcile_mismatch` tickers (8/12 boot), 8/8 `halt_reconciliation_mismatch` audit ($51 predicted vs $5 exchange) | 8/5 sweep + 8/12 forensics | read/reporting path only |
| 10 | store rotation + stall-wall mechanism (149.6 GB store; `rfqs` = 48.8M rows; 34 supervisor stall-kills/week at the hand-set 30.5s wall) | 8/12 readout | ops + mechanism repair (derive the budget from measured loop latency) |
| 11 | marked-EV drift decomposition (snapshot book EV median NEGATIVE every full day vs +2.5¢/$1 entry EV) | 8/12 tape audit | read-only analysis first |
| 12 | boot persistence after OS reboot | operator DECLINED 8/12 ("leave as is") — recorded, not queued | — |
| 13 | smaller standing items | F5 re-admit after classification+ρ; halt-counter cumulative-vs-consecutive ruling; watchdog latched-halt cancel-all decision; lineup-status feed decision; partial-scalar math (multi-player/mixed combos); 48h rain-rule surface; UCL two-legged-tie handling; vitals per-file manifest + loud missing-config error | various |

## D. The 9/1 review — what the month of data decides (define the dashboard NOW)

Pre-registering the reads so the 9/1 session opens with a dashboard, not a
re-derivation (all exchange-truth, no refit):

1. **Pooled era read at ~2,500–3,000 settlements**: P(excess≤0) series
   (0.061 → 0.200 → 0.242 → 0.095 → ?), P(realized≤0), the 60–80¢ and 2-leg
   cells, prop-leg marginal bias by family (the one live pricing defect).
2. **Loss/win-day census**: daily realized as % of equity-at-open (the 8/12
   method), count of ≥±8% days vs the p_kill_night distribution the engine
   logged — the empirical basis for the marginal-gate / posture decision.
3. **p_kill_night + p_book distributions** per day (tape audit method).
4. **Whale-seam tally** (top-5 share per outlier day) → sizes P1 Stage-1.
5. **Capacity walls**: decline-reason histogram + insufficient_balance counts
   → sizes the backstop repair + withdraw raise.
6. **Diversification effect**: same reads split MLB-only vs new-sport days as
   sports come online — the operator's own hypothesis test.

## NEXT STEPS

- **Standing through 8/31:** engine untouched; bot runs; watchdog relights;
  this ledger is the single place deferred work lives. Weekly (or on demand):
  `tools/diagnostics/rfq_series_census.py` for new-sport demand.
- **Active workstream:** sports onboarding per the companion report
  (`2026-08-12-sports-onboarding-review-and-demand-census.md`) — pricing-side,
  allowlist-gated, playbook-driven.
- **9/1:** open with the §D dashboard; decide items A.1, A.2, B.3, B.4 (+
  adaptive-caps enforce) with a month of evidence.
