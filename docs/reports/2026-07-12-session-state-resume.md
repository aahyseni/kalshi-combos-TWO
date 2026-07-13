# SESSION STATE / RESUME — 2026-07-12 (updated 2026-07-13; supersedes 2026-07-11)

**If you are a fresh Claude/operator session: read this file, then
`docs/reports/README.md` newest-first, then `docs/research/RISK_BUILD_PLAN.md`
(the canonical 6-phase risk plan), then `CLAUDE.md`'s ⚡ CURRENT STATE block.
The operator memory (`project_kct_resume_state`) mirrors this.**

## Repo state

- `main` @ `632cc1f` (pushed; check `git log --oneline -5`), tree clean,
  **suite 1406 passed / 0 failed** (`uv run pytest -q`).
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
- **PHASE 2 — caps + slate level: DONE, MERGED `632cc1f`, pushed (2× judge
  PASS).** Additive SHADOW %-of-bankroll layer alongside the UNCHANGED enforced
  hard-dollar caps (`Breach.shadow`, split in `lifecycle._partition_breaches` —
  shadow = LOG-ONLY, dropped before any block/decline/halt). $2,000 START values,
  integer-exact `thr = frac.num·bankroll // frac.den`: game 8% / per-combo 1%
  (LOSS axis) / directional 10% / **slate 8%** (Σ game-loss over one US/Eastern
  calendar-day bucket, source `PregameGate.leg_start_time`; UNKNOWN start ⇒
  pooled capped bucket) / daily 6% / drawdown 10% / hard-trip 12% / utilization
  3× (NOTIONAL axis — two axes never summed). Fail-closed (no/≤0 bankroll ⇒
  `SKIP_BANKROLL_UNAVAILABLE`) + StarvationWatchdog. Intraday **peak-equity
  latch** in BalanceTracker ARMS the give-back halts (all 9 caps observable in
  shadow); `maintenance_tick` escalates enforced drawdown/hard-trip to killswitch.
  Config validation (frac ∈ (0,1], non-finite guarded). **Still SHADOW** — the
  operator flips `risk.caps_shadow_mode: false` per cap-set sign-off AFTER
  reviewing real shadow-log behaviour. Report:
  `2026-07-13-risk-phase2-caps-and-slate.md`.
  - DEFERRED to Phase 3+: fill-velocity enforcement (needs the reservation
    service's committed-fill stream); hard-trip KILL-file latch (both give-back
    halts already stop quoting). Enforce-time notes (give-back denominator =
    haircut risk-bankroll so halts bite sooner; enforced give-back stops the book
    from 3 directions) in the report.
- **PHASE 3 — concurrency & state safety: NEXT.** Single-writer risk-reservation
  service — reserve capacity BEFORE sending confirm (atomic + versioned), so two
  RFQs can't both claim the same headroom; confirm-TIMEOUT = assume-committed +
  reconcile against the exchange. Race-free today only because we run one asyncio
  loop; this makes it safe for any future fan-out. (See RISK_BUILD_PLAN Phase 3.)
- **PHASES 4–6:** portfolio MC + challenger overlay → skew/widen-vs-decline/
  pregame precision → external watchdog + go-live gates.

## RUNNING processes (verify before assuming!)

- **Prod observe recorder** (`python -m combomaker.ops.cli run --env prod
  --mode observe`): verify via the recorder log's `observe_metrics` —
  `combo_trades.stored` must keep RISING during game hours. If flat, restart
  (ONLY ONE instance ever). Recording since 2026-07-09 for WC settlement
  backtest. **Check this FIRST in any new session.**

## Operator decisions owed (built on plan defaults; confirm before ENFORCE)

Phase 2 shipped on the researched defaults (operator said "build on the
defaults"), all in SHADOW. Before flipping `risk.caps_shadow_mode: false`:

- Review real shadow-log behaviour, then confirm/adjust the cap %s (game 8 /
  combo 1 / directional 10 / slate 8 / daily 6 / drawdown 10 / hard-trip 12 /
  util 3×), `portfolio_haircut = 0.5`, the UTC day-boundary, the ET-day slate
  bucket (vs a rolling 2–3h window), and `starvation_threshold`.
- Enforce-time semantics to accept (both SAFE/conservative, in the report):
  give-back halts measure raw equity vs `frac × haircut-risk-bankroll` (bite
  sooner than "% of equity"); an enforced give-back stops the book from 3
  directions (block quote + decline confirm + kill).
- The caps ASSUME a profitable markup (precondition); markup still pooled-multi-
  week, decided separately.

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

- Owner (next session): begin Phase 3 (single-writer risk-reservation service)
  per RISK_BUILD_PLAN — or the operator's chosen thread.
- Owner (operator): confirm the Phase-2 enforce-time knobs above after shadow
  logs accumulate; prioritize the parallel pricing backlog if desired.
- Standing: recorder health check FIRST in any new session.
