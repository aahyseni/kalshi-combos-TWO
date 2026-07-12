# SESSION STATE / RESUME — 2026-07-11 (supersedes 2026-07-09-session-state-resume.md)

**If you are a fresh Claude/operator session: read this file, then
`docs/reports/README.md` newest-first, then `CLAUDE.md`'s ⚡ CURRENT STATE
block. The operator memory (`project_kct_resume_state`) mirrors this.**

## Repo state

- `main` @ `3005793`+ (pushed; check `git log --oneline -5`), tree clean,
  **suite 1245 passed / 0 failed** (`uv run pytest -q`).
- Engine: MLB props complete (165-entry table + rung keys + 149-cell
  conditionals), containment campaign merged (collapse plans, exact windows,
  impossibility rules, tripwire, isolation guard), pregame-only gate ACTIVE,
  **leg-series allowlist ACTIVE (MLB + WC only — `filters.
  allowed_leg_series_prefixes`; unblock = one YAML prefix)**, sell-only book
  UN-GATED (real $1.00 demo settlement).
- Both module overviews + zero-bias judge verdict published
  (`2026-07-11-{soccer,baseball}-module-overview.md`, judge findings all
  closed or filed; F1→series gate, F2/F3 corrected, F4-F7 addenda).

## RUNNING processes (verify before assuming!)

- **Prod observe recorder** (`python -m combomaker.ops.cli run --env prod
  --mode observe`, PID pair ~43992/25972, alive at last check 2026-07-11):
  verify via the recorder log's `observe_metrics` — `combo_trades.stored`
  must keep RISING. If flat during game hours, restart the recorder (ONLY ONE
  instance ever). It has been recording since 2026-07-09 for the post-Jul-11
  WC settlement backtest.

## What's NEXT (operator-approved order)

1. **#14 demo fill e2e** — sell-only fills are un-gated; run a real demo
   fill round-trip with reconciliation (predicted vs ledger to the cent).
2. **#15 weekly sweep/calibration cadence** — settlement ledger, bucket-split,
   game-clustered CIs; P&L sweeps are thermometers (NEVER refit on a window).
3. **#16 MLB blind test** (soccer-style final exam).
4. **E decisions (operator):** markup from POOLED MULTI-WEEK evidence
   (props-first shape suggested; prop YES-hit at 1¢ ≈ 19% — F3);
   prod whitelist/limits/confirm-live (safety.prod_limits_configured still
   false → blocks Phase 7).
5. **WC settlement backtest** once Jul-11 games settle (recorder has the tape;
   `tools/backtests/wc_backtest.py`).

## Queued engine items (operator to prioritize; do NOT start unprompted)

- Reclaim the ~3,350 window-guard declines via a measured window-aware ρ.
- hr1⟹hrr1 / hit-k⟹hrr1 exact cells; S41-ny window; soccer pair priors
  (corners|advance, pgoal|total, btts|advance — the w2 lever).
- Sync the stale job-tmp mixed driver dispatch before any mixed re-run.
- UCL rules-text capture before unblocking KXUCL; unblock checklist for any
  new competition = classification audit → regime flags → priors review.
- WNBA/UFC/golf shapes parked in `docs/calibration/containment_probe/taxonomy.json`.

## Key doctrine (operator rules, survive everything)

- Never refit on a P&L window; alarms pre-registered, multi-week,
  game-clustered. Never negate :same→:opp. Never interpolate rungs.
- No unexplained residuals: every "N of M" names the M−N.
- Fact-check operator recollections against primary sources before acting.
- Use existing analyzers; don't write parallel ad-hoc analysis scripts.
- Data passes between agents BY FILE PATH. Fixtures must match prod
  conventions (the poisoned-`ev()` lesson).
- farmable=True ONLY airtight one-record tautologies.
- Hard boundary: never touch `kalshi-combos` without `-TWO`; prod DB
  read-only `mode=ro`; secrets env-only; push after every commit.

## NEXT STEPS

- Owner (next session): #14 demo fill e2e, then the weekly cadence.
- Owner (operator): E decisions when pooled data exists; say "continue" and
  the chain above resumes.
- Standing: recorder health check FIRST in any new session.
