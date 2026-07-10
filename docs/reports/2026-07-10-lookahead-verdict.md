# Look-ahead verdict: ALL SIX pre-registered predictions TRUE — the artifact was the cause, WC survived, no pickoff risk

**Date:** 2026-07-10 ~13:30 UTC · **Method:** pre-registration committed BEFORE
results (`316c5cb`), fresh full gathers (13.53M rfq rows, 1.99M in-scope combos,
82k tape fetches), three buckets × two snapshot policies, independent verifier
recomputing from raw pickles (zero discrepancies >0.05¢) · **Pre-reg:**
`2026-07-10-lookahead-rerun-preregistration.md` · **Artifact (v2, corrected):**
`2026-07-10-mlb-backtest-gate.html`.

## The scored predictions

| # | prediction | verdict | the deciding numbers |
|---|---|---|---|
| P1 | MLB fixed pure-ML honest (±1¢) | **TRUE** | all-ML n=3,207: med 0.28¢, bias −0.31¢, **99% within-2¢**; every leg count −0.19..−0.49¢ |
| P2 | lookahead reproduces the bias on fresh data | **TRUE** | ML×2 bias **+5.93¢**, med 16.93¢ under the old policy — the phantom, reproduced on demand |
| P3 | WC survived; delta smaller than MLB | **TRUE** | WC fixed 1.55¢/−1.54 vs published 1.60¢/−1.82 at **19× the sample** — the backbone reproduces; WC contamination exists but DEFLATES fairs (opposite sign, −1.17¢ median on affected rows) |
| P4 | mixed bucket sane | **TRUE** | 0.74¢/−0.95; cross-sport independence EXACT (fair==product on all 407 2-leg cross-sport; 0.88¢ vs clearings) |
| P5 | HR+ML / HIT+ML cells de-scare | **TRUE** | HR+ML n=42: **−0.59¢/0.38¢** · HIT+ML n=89: **−1.07¢/0.97¢** — the earlier −5.98/−14.25 were artifact; all cells inside the markup band, NO pickoff escalation |
| P6 | our fair not systematically high on ML | **TRUE** | fixed bias slightly negative everywhere |

## What the honest book looks like (fresh MLB fixed run, n=6,128)

ALL: **0.34¢ median, −0.39¢ bias, 94% within-2¢**. Gate PASS under BOTH
policies (prop-carrying 0.70¢ vs 1.56¢ legacy; game-lines 0.30 vs 0.30) — the
promoted table's win was never artifact-dependent. Every bucket's bias is
negative (−0.2..−1.1¢): the maker-markup signature, i.e. our fair sits just
under the winning quote, exactly where a calibrated parlay-seller should sit.

## Two mechanism notes worth keeping

1. **The leakage signature, quantified:** under lookahead, resolved combos'
   fairs "predict" settlement better (18.0¢ fair vs 17.9% realized) than the
   honest fixed fairs (15.4¢ vs 18.4%) — because post-cutoff marginals CONTAIN
   in-play information. Better-looking calibration was itself the contamination.
2. **WC's contamination has the opposite sign** (deflates fairs): different
   flow conditioning — worth remembering that look-ahead artifacts are not
   always optimistic.

## Surviving real findings (not artifacts)

- Sub-2¢ longshot parlays settle ~0.3% but clear ~8¢ — takers overpay for
  lottery tickets; the profitable sell-side flow.
- WC corners-carrying compositions remain the worst class (2.24¢ vs 1.19¢
  non-corners) — the nested-band + corners work is aimed at exactly this.
- WC-ADVANCE+MLB-KS cross-sport cell: −2.0¢ bias (n=38) — watch item.
- WC 2-leg over-priced rate 26.5% (n=1,847) — tail watch on short combos.
- Fixed MLB residual bias ≈ −0.3..−0.9¢ = markup; settlement-side edge question
  (does the whole market underprice favorites?) still needs 2–4 weeks of games.

## Fixes landed

Both harnesses gather-filter snapshots to pregame (`bfcd954`); the gate
artifact re-issued as v2 with a correction banner; the 18.3¢ callout RETRACTED.

## NEXT STEPS
- Weekly re-run cadence as the recorder accumulates (settlement-side questions
  need n).
- Remaining queue unchanged: DO-5 rung keys · DO-6 basket width · DO-8
  measurements · mlb_runs grid calibration · WC corners follow-through.
- Process lesson already codified: agents pass data by file path; backtest
  snapshot policy is now structural (gather-level), not a convention.
