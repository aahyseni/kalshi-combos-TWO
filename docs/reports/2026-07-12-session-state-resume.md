# SESSION STATE / RESUME — 2026-07-12 (supersedes 2026-07-11-session-state-resume.md)

**If you are a fresh Claude/operator session: read this file, then
`docs/reports/README.md` newest-first, then `docs/research/RISK_BUILD_PLAN.md`
(the canonical 6-phase risk plan), then `CLAUDE.md`'s ⚡ CURRENT STATE block.
The operator memory (`project_kct_resume_state`) mirrors this.**

## Repo state

- `main` @ `6b5c76f` (pushed; check `git log --oneline -5`), tree clean,
  **suite 1355 passed / 0 failed** (`uv run pytest -q`).
- Engine UNCHANGED from 2026-07-11 (MLB props + WC containment complete,
  pregame-only gate + leg-series allowlist MLB/WC ACTIVE, sell-only book
  un-gated). The current thread is the **RISK ENGINE build**, not pricing.

## Where the RISK ENGINE build is (the active thread)

Canonical plan: `docs/research/RISK_BUILD_PLAN.md` (6 phases). Ordering:
correct the numbers → cap on them → make caps race-safe → tail monitor +
challenger → quoting policy → external watchdog → go live at $2,000.

- **PHASE 0 — Foundation: DONE** (merged, earlier). B1 two money axes
  (true max_loss vs gross_settlement_notional), B2 game-key aggregation,
  BalanceTracker spine. Report: `2026-07-12-risk-foundation-b1-b2-balance.md`.
- **PHASE 1 — Correct the money: DONE, MERGED `6b5c76f`, pushed.**
  Equity-aware bankroll denominator `min(SOD, cash + haircut·PV)` (haircut
  default 0.5, UTC day-boundary — both FLAGGED for operator), fee booking in
  the ledger ($0 for our combo maker fill, proven via real `pricing/fees.py`),
  scalar settlement in the LEDGER (NO pays contracts×(1−V); pricing fair
  untouched — reactive stance), rename `payout_obligation` →
  `gross_settlement_notional`. **Adversarial judge PASS + 2 latent defects
  filed and FIXED before merge** (float-floor reconciliation gap; poison-pill
  idempotency). Report: `2026-07-12-risk-phase1-four-correctness-fixes.md`.
- **PHASE 2 — caps + slate level: NEXT (not started).** Wire the R2 cap
  hierarchy at $2,000 values: game-loss 8%/$160 (on `worst_case_loss_by_game`),
  per-combo 1%/$20 max-LOSS, directional 10%, absolute 3× notional backstop,
  daily-loss 6%, drawdown 10%, hard-trip 12% + the NEW slate/time-window
  PRE-TRADE cap (games settling in one 2–3h window). Fail-closed + starvation
  watchdog. **Ships in SHADOW mode** (logs every would-be breach, zero quote
  impact) before enforce. `risk_bankroll_cc` gets wired into the %-caps here
  (BalanceTracker is spine-only today, not yet consumed by `limits.py`).
- **PHASES 3–6:** single-writer reservation → portfolio MC + challenger overlay
  → skew/widen-vs-decline/pregame precision → external watchdog + go-live gates.

## RUNNING processes (verify before assuming!)

- **Prod observe recorder** (`python -m combomaker.ops.cli run --env prod
  --mode observe`): verify via the recorder log's `observe_metrics` —
  `combo_trades.stored` must keep RISING during game hours. If flat, restart
  (ONLY ONE instance ever). Recording since 2026-07-09 for WC settlement
  backtest. **Check this FIRST in any new session.**

## Operator decisions owed (Phase 2 needs these; plan specifies defaults)

- Confirm `portfolio_haircut = 0.5` and the UTC day-boundary rule (or wire
  `set_start_of_day_equity` to the desk's session/deposit events).
- Confirm the cap %s (plan has them at the $2,000 values above) and the
  hard-trip 12% + fill-velocity rate.
- Exchange-authoritative bankroll poll cadence.
These are FLAGGED, not blocking — Phase 2 builds in shadow mode with the plan's
values and logs would-be breaches for operator review before enforcement.

## Parallel data-accumulation track (does NOT block the phases)

- MARKUP decision + NORMAL/FAT room-predictor: shadow the predictor, pool
  multi-week game-clustered settlements, decide the number from pooled data.
  NEVER refit on a P&L window. The caps assume a profitable markup but don't
  set it.
- Pricing backlog (from 2026-07-11 resume, operator to prioritize): #14 demo
  fill e2e, #15 weekly calibration cadence, #16 MLB blind test, window-guard
  reclaim, UCL/other-competition unblock checklist.

## Key doctrine (operator rules, survive everything)

- Never refit on a P&L window; alarms pre-registered, multi-week, game-clustered.
- No unexplained residuals: every "N of M" names the M−N.
- Fact-check operator recollections against primary sources before acting.
- Use existing analyzers; don't write parallel ad-hoc scripts. Data passes
  between agents BY FILE PATH; fixtures must match prod conventions.
- Money is int centi-cents outside the simulator; binary floats for money
  BANNED (floats OK in probability space). Prototype-in-test → port →
  parity-check to the cent (rule 8). Live modules stay PRISTINE.
- Hard boundary: never touch `kalshi-combos` without `-TWO`; prod DB read-only
  `mode=ro`; secrets env-only; push after every commit.

## NEXT STEPS

- Owner (next session): begin Phase 2 (caps + slate, shadow mode) per
  RISK_BUILD_PLAN — or the operator's chosen thread.
- Owner (operator): confirm the Phase-2 knobs above (all have plan defaults);
  prioritize the parallel pricing backlog if desired.
- Standing: recorder health check FIRST in any new session.
