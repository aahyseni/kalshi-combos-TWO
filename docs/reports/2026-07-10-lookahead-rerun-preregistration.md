# PRE-REGISTRATION — look-ahead verification reruns (committed BEFORE results)

**Date:** 2026-07-10 ~09:45 UTC · **Purpose:** the operator demands 100%
certainty that the ML-parlay bias was the snaps[-1] look-ahead artifact — and
protection against confirmation bias ("not biased to get a better number").
This file is committed BEFORE any rerun completes; the verifier scores every
prediction TRUE/FALSE and the results are published regardless of outcome.

## Design

Three FRESH gathers (all recorder data 2026-07-06 → now), each priced under
BOTH snapshot policies from the same inputs:
- **A. WC-only** (every leg KXWC*)
- **B. MLB-only** (every leg KXMLB*)
- **C. WC/MLB mixed** (all legs in KXWC* ∪ KXMLB*, at least one of each —
  tests the cross-sport independence assumption vs clearings)

Per bucket: `fixed` = last PRE-CUTOFF snapshot (the repaired harness) vs
`lookahead` = latest snapshot (the old behavior, reproduced deliberately).
The per-bucket (lookahead − fixed) delta IS the artifact, measured per sport
on identical data. An independent verifier recomputes all headline numbers
directly from the pickles (not trusting the analyze stage) and scores the
predictions.

## Pre-registered predictions

| # | prediction | falsifies the artifact theory if wrong |
|---|---|---|
| P1 | MLB-only **fixed**: pooled cross-game ML×N bias in **[−1.0, +0.5]¢**; median \|err\| ≤ 1.0¢; ≥ 88% within-2¢ on pure-ML buckets | yes — if the +bias persists after the fix on fresh data, the artifact was NOT the (whole) cause |
| P2 | MLB-only **lookahead** on the SAME fresh data: ML×2/×3 mean bias ≥ **+3¢** | yes — if the old policy doesn't reproduce the bias, the original +6.5¢ came from something else |
| P3 | WC-only: fixed-vs-lookahead delta **smaller than MLB's**; WC fixed bias stays negative-to-zero ([−3.5, 0]¢), median \|err\| ≤ 2.5¢ | partially — a WC delta > +3¢ means the published 1.60¢/−1.82 WC numbers were also contaminated and must be re-issued |
| P4 | Mixed bucket fixed: median \|err\| between the two sport-pure numbers; bias mildly negative (markup) | no — exploratory |
| P5 | HR+ML and HIT+ML cells (fresh, fixed, n permitting): bias shrinks into **[−4, 0]¢**. If ≤ −4¢ persists at n ≥ 30 → escalate as GENUINE adverse-selection risk, not artifact | yes for those cells — the earlier −5.98/−12.6 were computed on contaminated fairs |
| P6 | Settlement direction (where resolved): realized-vs-fair gap does NOT show our fair systematically HIGH on any pure-ML bucket (the inflation hypothesis stays rejected) | yes |

## Honesty rules

1. All buckets and all compositions get reported — no cherry-picking rows.
2. The verifier recomputes from raw pickles with independent code.
3. If ANY prediction fails, the failure is the headline of the results report,
   not a footnote.
4. Old published numbers that turn out contaminated get corrected in-place with
   a visible correction note (gate artifact + WC report if P3 fails).

## NEXT STEPS
- Rerun fleet launches immediately after this commit; results report + scored
  predictions to follow.
