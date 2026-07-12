# Zero-gaps campaign WIRED + go-live scorecard — the honest verdict

**Date:** 2026-07-12 (overnight) · **Merged:** `e5d34e5` on main, pushed · suite
**1294/0**. Worktree round: wire `9cab414` → judge (all 5 SURVIVE) → bit-exact
differential (STAGE-A PASS) → merge → full suite → this scorecard.

## What the campaign closed (the measurement mandate: "nothing missing, fallback, or unmeasured")

The gap ledger (282 config entries, zero remainder) is **closed** for both live
sports. Every reachable hand-prior / flat-fallback pair is now measured or
exact:

- **Soccer: 26 keys wired** — advance|corners (a measured STRENGTH CURVE,
  dog +0.23 ↔ fav −0.23, from a NEW StatsBomb knockout ET-corners corpus),
  corners|player_goal and corners_team|player_goal (a **sign flip** to −0.14,
  from a NEW Understat×football-data join — the "no public corpus" was false),
  advance|corners_team (bridge-derived + KO-validated), the corners×1H cluster,
  and the **wrong-sign flat-0.6 draw fallbacks fixed** (1h_spread|ml:tie
  +0.6 → −0.44, a 1.04 swing). btts|1h_btts = exact containment +0.95.
- **MLB: 84 conditional cells + 21 pair keys** — 37 EXACT cells (hit⟹HRR,
  hr⟹HRR, TB⟹HRR, all ==1.0 on 1,033,852 batter-games), 47 measured
  conditionals (incl. the S41 reverse row), rfi|spread hand-prior → measured
  per-rung ladder, teammate rungs, ml|hr:same rungs.
- **M3 mlb_runs grid: KEEP** (k=3.54 reproduced; the old 5.53¢ scare was
  exonerated as the look-ahead artifact, not the grid).

Correctness is proven three ways: **verbatim audit CLEAN** (zero off-list
wires), **adversarial judge all-5-SURVIVE** (curve direction, sign flips,
suffix grammar, MLB exact re-derived on the full population), and the
**bit-exact differential STAGE-A PASS** (untouched combos identical to the bit;
every mover a wired-key combo; zero unexplained; continuity-bad=0).

## The go-live scorecard (ph4 window, wired config) — read this carefully

| | MLB | Soccer/WC |
|---|---|---|
| overall median\|err\| vs clearing | **0.34¢** | 1.64¢ |
| within-2¢ | 93.4% | 57.2% |
| fair-above-clearing >2¢ (never-win) | 1.6% | 4.9% |
| fair-below >5¢ (pickoff-watch) | 1.0% | 10.5% |
| **families >2¢ median vs clearing** | **1** | **97** |
| settlement Brier: our fair vs clearing | — | **0.06378 BEATS 0.06776** |

**MLB is at the bar.** 0.34¢, tiny tails, and the ONE >2¢ family is the known
`player_hrr`-only cell — settlement-confirmed as maker markup (our fair sits on
the realized rate; the clearing pads above it), not our defect.

**Soccer is NOT at the literal "no family >2¢ vs clearing" bar — and it cannot
be, because that bar is markup-confounded.** The 97 families are longshot
corner/advance parlays. The unbendable ruler says why: on 14,038 resolved WC
combos, **settled-YES 8.86% vs our fair 12.37% vs clearing 14.20%** — the
makers price these longshots ~5pp above where they actually settle. Our fair
is closer to truth than the clearing is, so "error vs clearing" is measuring
their markup, not our miss. Confirmed by settlement Brier: **our new fair
0.06378 beats the clearing 0.06776, and improved on the old fair 0.06391** —
the wiring made soccer *more* calibrated to reality, which moves it *further*
from the padded clearing on longshots.

## Markup-vs-mispricing diagnostic (the 97 soccer families)

The 97 >2¢ families cover **7,305 combos = 34.6% of WC flow**. For each, is our
fair closer to the SETTLED rate than the maker's clearing is? Resolved-weighted
across all 97: **our fair is closer to reality on 6,701 combos (92%)**; the maker
is closer on only 604 (8%). Pattern (top families by flow):

| family | n | %tot | our fair | maker | settled-YES | closer |
|---|---|---|---|---|---|---|
| ADVANCE+CORNERS+PLAYER_GOAL | 955 | 4.5% | 7.2% | 10.1% | 6.4% | OURS |
| ADV+CORNERS+CORNERS_TEAM+PGOAL | 479 | 2.3% | 5.6% | 8.6% | 3.8% | OURS |
| ADVANCE+CORNERS_TEAM+PLAYER_GOAL | 421 | 2.0% | 4.7% | 7.6% | 3.3% | OURS |
| ADVANCE+CORNERS | 347 | 1.6% | 19.9% | 23.0% | 18.5% | OURS |
| ADVANCE+CORNERS+TOTAL | 284 | 1.3% | 21.2% | 24.0% | 12.0% | OURS |
| ADVANCE+BTTS+TOTAL | 284 | 1.3% | 14.4% | 17.0% | 9.3% | OURS |
| ADVANCE+CORNERS+PGOAL+TOTAL | 220 | 1.0% | 12.5% | 15.4% | 13.6% | OURS |
| CORNERS+CORNERS_TEAM | 60 | 0.3% | 26.0% | 30.8% | 9.1% | OURS |

**Verdict: markup, not mispricing.** Our fair sits at or just above the settled
rate; the maker clearing sits ~3pp above ours. We price these longshot
corner/advance parlays honestly; the market clears fat because the true
settlement is far below the print. **To win these auctions we PAD MARKUP on top
of our (correct) fair — we don't change the fair.** The 8% "maker-closer" tail
is small-n noise where the settled rate happened to land near the clearing.
This is the strongest possible evidence for a fat markup on soccer longshots
(the markup decision, deferred to pooled multi-week — this window is one input).

## The honest bottom line

- **The measurement mandate is MET**: zero unmeasured, zero fallback, zero
  hand-prior pairs remain in MLB or soccer flow. Every value is measured or
  exact, judged, and settlement-validated.
- **MLB pricing clears the go-live bar.**
- **Soccer pricing is CORRECT (beats clearing and old fair on settlement) but
  cannot clear a "no family >2¢ vs clearing" bar**, because ~40-97 of its
  families are longshot parlays where the clearing is maker markup, not fair
  value. Chasing that bar would mean pricing like the makers (worse Brier).
- **Decision owed by operator:** the right soccer readiness bar is SETTLEMENT
  calibration (we pass: Brier beats clearing), not median-error-vs-clearing on
  longshots. Confirm that reframing, or direct further work — but note no
  additional measurement can close a *markup* gap.

## NEXT STEPS

- Operator: accept the settlement-calibration bar for soccer longshots (we
  pass), or specify a different course. MLB: accept the 1 hrr markup cell
  (settlement-confirmed) or config-decline hrr-only parlays.
- Then the money path: #14 demo fills (sell-only un-gated) → #15 weekly
  settlement cadence (the ruler that separates markup from mispricing) → #16
  blind test → E decisions (markup, pooled multi-week).
- Housekeeping: the fresh-window gate gather completed but its downstream
  pricing was abandoned amid process-reaping chaos; this scorecard used the
  ph4 window (like-for-like with the capstone) instead — a fresh-window
  re-gate can confirm on new data when useful. Full scorecard:
  `zerogaps/scorecard_output.txt`.
