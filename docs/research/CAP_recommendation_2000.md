# Cap recommendation — $2,000 bankroll (reconciled)

**Date:** 2026-07-12 · Two independent derivations — ruin/Kelly theory
(`CAP_theory_2000.md`) and empirical game-clustered simulation over 17,351
resolved WC+MLB combos / 59 games (`CAP_empirical_2000.md`) — reconciled here.
Leans conservative: a $2k bankroll is an ABSORBING barrier (you can't recover
from 0) and this is ONE window.

## Recommended cap set (start values, for operator sign-off)

| Cap | START | $ @ $2,000 | axis it caps | why this number |
|---|---|---|---|---|
| **%-of-GAME (correlated loss)** | **8%** | **$160** | worst-case LOSS per game (Σ max_loss, game-keyed) | Theory's ruin knee (10% is the ruin cliff); empirical P(ruin)=0 at 10% too but 8% is the safe start. THE headline cap. Upgrade to 10-15% only after pooled multi-week. |
| **Per-COMBO max payout** | **1%** | **$20** | single combo max loss | Empirical: the STRONGEST downside lever — 1% beats 2%/5% on profit/drawdown at every game level. Forces whale RFQs to be sliced. |
| **One-directional / theme** | **10%** | **$200** | net exposure to one leg outcome across games | A theme ≈ same-game-correlated on losing nights → hold near the game cap. Not tape-derivable; measure realized correlation then tighten. |
| **Absolute-$ (utilization backstop)** | **3×** | **$6,000** | gross committed payout, whole book | Loose backstop ABOVE the % caps; binds even when the live bankroll poll is stale. |
| **Daily-loss halt (soft)** | **6%** | **$120** | realized+unrealized, from day start | Between theory 4% (empirical shows 4% nuisance-trips on normal $2k variance) and empirical 8%. Fires on a genuinely bad day, cancel-all + stop for the day. |
| **Peak-drawdown halt** | **10%** | **$200 trough** | give-back from intraday peak | Between theory 8% / empirical 12%; catches profit give-back a from-zero cap misses. |
| **Hard-trip kill** | **12%** | **$240** | KILL file, human-only clear | Outside anything the tape produced (worst in-sample DD 14.5%); safety gap below the ~30%-of-bankroll operational-death barrier. OPERATOR-CONFIRM. |
| Fill-velocity | 5%/2s soft ($100), 10%/2s hard ($200), 8 fills/2s | — | committed payout per rolling window | Mass-acceptance rate limit; operator-set (not tape-derivable). |

## The two caveats BOTH agents insisted on (read these)

1. **Caps limit downside; they cannot make a book profitable — the MARKUP does.**
   At thin markup (WC 1.5¢ / MLB 0.5¢) EVERY cap set loses money in the sim —
   that's adverse selection, and no cap fixes a book priced in the loss zone.
   These cap values ASSUME the profitable two-tier markup (≈ WC +3¢ / MLB +1.5¢,
   above the ~2¢/1¢ break-even). Markup ≥ break-even is a precondition for the
   caps to matter.
2. **At $2,000 with these caps, P(ruin) = 0 across the whole frontier** — the
   binding constraint is DRAWDOWN, not survival. Good news: sized this way, the
   book doesn't blow up; it just bleeds in a bad week (which the halts catch).

## On the "wire committed_payout first" precondition (resolved)

The theory agent flagged that an "8% game cap is silently 60-90%" until the cap
axis is fixed — but that was based on the same premise the B1 build DISPROVED:
`max_loss_cc` was already the true NO cost (not a small premium), verified
against the demo settlement. So the **%-of-game LOSS cap goes on the
game-keyed `worst_case_loss_by_game_cc` that B1+B2 already built — it is ready
to wire correctly.** The `payout_obligation` axis feeds the utilization
backstop. No hidden looseness.

## Honesty / limits

ONE window; WC only 6 games (coarse bootstrap; MLB's 53 carry the diversity);
markup plateau and the theme/velocity caps are assumed, not tape-derived. These
are START values to run in SHADOW/observe and re-derive from pooled multi-week
game-clustered settlement before any prod scaling. Never refit on this window.

## NEXT STEPS

- **Operator:** sign off (or adjust) the START column, especially the two
  OPERATOR-CONFIRM items (hard-trip 12%, fill-velocity rate). Confirm the
  markup precondition (caps assume a profitable markup is set).
- **Then (me/engine):** wire these caps onto the B1/B2 foundation — the
  %-of-game LOSS cap on `worst_case_loss_by_game_cc`, per-combo on the position
  max_loss, utilization on `payout_obligation`, halts on the BalanceTracker's
  realized P&L — each shadow-first, fail-closed, with the starvation watchdog.
- Re-derive the values from pooled multi-week before they gate real capital.
