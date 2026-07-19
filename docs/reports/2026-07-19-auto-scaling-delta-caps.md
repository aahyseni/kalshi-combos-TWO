# 2026-07-19 — Auto-scaling delta caps (fractions of live bankroll)

**Operator directive (~12:10 AM ET):** "Did we manually move delta? I don't like
manually moving stuff like this, it should be automatic." The delta caps were
the last absolute numbers in the risk stack — hand-bumped 300/500 → 900/1500 →
1500/2500 within one week while every loss budget scaled itself as a fraction
of cash.

## What shipped (`fedb268`, suite 2486/0)

- `RiskLimits.max_market_delta_frac` / `max_event_delta_frac` (string-Fraction,
  default None): cap_contracts = `threshold_cc(frac, risk_bankroll_cc)/10_000`
  — the SAME bankroll basis (`min(start-of-day equity, cash + haircut·portfolio)`)
  and integer-exact arithmetic as the loss budgets, recomputed per check.
- Precedence: frac set ⇒ frac wins; absolute knobs remain ONLY as the
  no-bankroll-reading backstop (never looser — `SKIP_BANKROLL_UNAVAILABLE`
  already blocks quoting when the basis is dark). Frac unset ⇒ byte-identical
  old behavior (pinned). Startup `delta_cap_mode` log names the active mode.
- Breach shape, reason codes, waiver coverage unchanged. 25 new tests.

## Armed (12:17 AM ET restart, `live_20260719_autoscale.log`)

`max_market_delta_frac: "0.80"` / `max_event_delta_frac: "1.30"` — reproduces
the hand-set 1500/2500 at tonight's ~$1,910 basis, then breathes with the
bankroll untouched. Startup log confirmed `delta_cap_mode: frac/frac
0.80/1.30`; preflight green; 67 quotes in the first minute.

## Accepted residuals

1. `SkewLimits.max_event_delta_contracts` (skew headroom denominator) still
   reads the absolute knob — keep the absolute yaml values roughly current or
   accept skew-ramp drift; follow-up if skew precision matters.
2. Validator bound (0, 10] (event arming value is 1.30, so (0,1] was wrong;
   10× bankroll catches the "80"-for-"0.80" typo class).

## NEXT STEPS

- **Morning (me):** overnight fills/declines briefing; investigate the
  "game 26JUL19ESPARG not certifiable" waiver reason (2 occurrences) before
  the 2:45 PM ET final; make the offline MC tool settled-aware.
- **Sunday post-final (operator+me):** budget-family review (game .50 / slate
  .65 / det .36 / cvar .35 / delta fracs .80/1.30) with both games' data →
  merge to main (llm-b ancestry check) → MLB+WNBA switch.
