# MLB measurement tranche — every pair measured, both blockers resolved, basket overbid quantified

**Date:** 2026-07-09 ~23:59 UTC · **Status:** DONE — 8 measurement agents + xhigh
judge, **33 verdicts, 0 refuted measurements** · **Final table:**
`docs/calibration/staged_mlb_props.md` (judge-amended, rule-8 gated) · Follows
`2026-07-09-mlb-classification-and-rho-verification.md` (phase 1).

## The money finding — the all-NO HR basket overbid

The signature tape shape (8–9-leg all-NO HR baskets) priced with today's flat
+0.6 fallback vs reality (Retrosheet, 49,486 games, leave-one-out marginals):

| basket shape | empirical P(all-no) | flat-0.6 copula | **overbid** | measured-ρ copula |
|---|---|---|---|---|
| 9-leg star | 0.257 | 0.529 | **+27¢/$1** | 0.240 (−1.7¢) |
| 16-leg (top-8 both lineups) | 0.158 | 0.506 | **+35¢/$1** | **0.1579 (−0.0003)** |
| 8-leg top-4 | 0.356 | 0.604 | **+25¢/$1** | +1.4¢ |

Flat +0.6 would have us systematically overbidding these baskets by a quarter to
a third of a dollar; the measured ρs reproduce the 16-leg joint essentially
exactly. Remedy: measured pair ρs + 2–3¢ symmetric width on ≥8-leg same-family
baskets (ρ inflation rejected — it would misprice 2–3-leg combos).

## Headline measurements (all CONFIRMED; drop-in parity ≤0.0014 through the shipped copula)

| pair | ρ (rec) | evidence |
|---|---|---|
| player_hr\|GAME-total | **+0.24**/0.10 | the phase-1 critical gap, now measured (+0.233, era-stable, n=941k) — below team-frame +0.367 exactly as dilution requires |
| player_hrr\|total | **+0.40**/0.08 | starters population; line-monotone 0.33/0.40/0.44 |
| rfi\|total | **+0.37**/0.10 | strongest + most era-stable pair measured |
| hit/tb\|total | +0.25/+0.27 | rung-monotone (per-rung entries available) |
| ks\|ks · hr\|hr · hit\|hit · tb\|tb · hrr\|hrr | +0.04 · t+0.04/o+0.02 · t+0.07/o0 · t+0.06/o0 · t+0.17/o0 | ALL an order of magnitude below flat +0.6; hit/tb/hrr need teammate/opponent ticker-prefix routing (cheap, no ML resolver) |
| hit\|ks facing / teammate | −0.126 / +0.013 | confirms the facing-channel structure — a same-vs-opposing-team resolver captures the whole signal |
| ml\|hit · ml\|hrr oriented | ±0.226 · ±0.367 | resolver-gated; neutralized bands verified/amended (ml\|hit 0.25→0.26) |
| hr\|ks facing | −0.076 | was MISSING from staged table — 11,628 pairs/10h currently sign-wrong at +0.6; added 0.00/0.12 |
| spread\|total | +0.13/0.10 | fallback only; total≡margin (mod 2) parity coupling explains fixed-line oscillation |
| ml\|spread | **containment** | exact, 0 violations in 98,980 team-games; route structurally, fallbacks ±0.95 |
| same-day cross-game | +0.007 | independence STANDS (under the 0.02 threshold); doubleheader-spanning → UNKNOWN/wide |

## Strike ladders — the operator's K-line question is RESOLVED

- **Pitcher K pairs are ladder-FLAT** (ρ −0.229..−0.252 across lines 3.5–8.5
  while P(A) swings 0.65→0.08; slope CI contains 0). The Gaussian copula's
  strike-stability prediction holds → **the self-median line convention was
  fine, posted-line re-measurement is unnecessary, one entry serves all KS
  rungs.** No decision owed anymore.
- **Batter rungs DRIFT** (HIT +0.054/rung, HR +0.04–0.08 per rung, CIs exclude
  0) → per-rung entries; danger direction: a single 1+ ρ *understates*
  deep-rung joints = sells correlated combos too cheap.

## Both phase-1 blockers resolved

1. **event_mutually_exclusive — NOT a blocker.** Live probe of 24 events: all 6
   prop families report `false` (real boolean); KXMLBGAME reports `true`
   (correctly IMPOSSIBLE's YES+YES moneyline pairs). Baskets are reachable today
   — merely uncalibrated (UNKNOWN → default ρ). Un-gate action: nothing
   structural; ship LegTypes + table together + a ground-truth fixture pinning
   the flags (a silent flip to true would no-quote every basket).
2. **Same-player rungs — zero flow.** 0 of 223,096 same-game same-family prop
   pairs share a player (rungs are live — 45–71% of players have 2+ — takers
   just never bundle them; joint = tighter leg). Cross-family same-player = 6.5%
   (5,591/10h: HIT×HR 2,223, HR×TB 1,051…) — all deterministic containments
   (HR⇒HIT, HR⇒TB≥4, HR⇒HRR≥3 verified EXACT 101,186/101,186) → structural
   branch, never a ρ.

## Validation quality

Fresh parsers cross-validated against official gamelogs: team runs/hits/HR/SO
**exact in 49,486/49,486 games**; HRR's new RBI scorer 96–99% per-game exact
(league bias ≤0.46%). hit|total reproduced **identical to 4 decimals across two
independent parses** in different agents. Recorder health re-verified during the
tape scan (combo_trades.stored +210/min).

## Remaining gaps (small, bounded)

- Cross-family distinct-player batter pairs (hit|hr, hit|hrr, hit|tb, hr|tb,
  hrr|tb — ~31k same-game pairs/10h): labeled priors staged (bounded by measured
  same-family values); measurable from the existing parsed npz in hours.
- teammate hrr|ks and player_tb|player_ks: labeled priors staged, measure with the above.
- HR2+×game-total and teammate hrr|ks are inferred-not-measured but inside shipped bands.

## NEXT STEPS

- **Next engineering session (the promotion path, in order):**
  1. `tools/backtest_mlb_pairs.py` — replay recorded MLB RFQs, staged config vs
     live flat-0.6, log-loss/markout gate (rule 8). THE gate for everything.
  2. Ship legtypes keywords + [A]/[B] table entries + teammate/opponent
     ticker-prefix routing + same-player containment branch + ml|spread MLB
     containment family + the event-flag fixture — port + parity-check.
  3. ML-orientation resolver (prop-ticker team prefix vs ML suffix) → flip [C]
     to signed values.
  4. Measure the [D] cross-family pairs (hours, existing npz).
- **Owner (operator):** K-line decision CLOSED (resolved empirically). Review
  the basket-width policy (+2–3¢ on ≥8-leg all-NO baskets) and the final table.
- **Standing:** monthly MVE-collection eligibility re-scan (F5 blockers must
  ship before any F5 eligibility flip; TEAMTOTAL eligibility would un-strand
  +0.367/−0.380).
