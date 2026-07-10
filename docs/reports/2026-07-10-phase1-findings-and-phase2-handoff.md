# Phase 1 findings + Phase 2 HANDOFF (spend-limit interruption — resume here)

**Date:** 2026-07-10 ~19:30 UTC · **State:** Phase 1 COMPLETE + committed
(`0a0ab2f`,`402210e`); **Phase 2 NOT DONE** — its 3 agents died instantly on the
operator's monthly spend limit; B2's only edit (a comment block) was reverted;
tree = clean verified state, suite 1095/0, mlb table 68 entries.

## ★ THE WIRE LIST FOR PHASE 2 IS PERSISTED IN-REPO: `docs/calibration/phase2_wire_list.txt`
(judge conventions at top + 88 judge-confirmed entries + 10 prior-judged routed
splits + 3 NOT-WIRED flags). Job-tmp copies are ephemeral — this file is canonical.

## Phase 1 findings (all judge-verified, Retrosheet 2005-25, copula parity ≤1e-4)

1. **Orientation is ASYMMETRIC — never negate** :same to get :opp on spread
   pairs (shared run environment attenuates :opp; gap 0.04-0.13, grows with line).
   Both sides measured directly. hrr₃|spread: +0.384(:same) vs −0.330(:opp) @r1.5.
2. **tb×ks facing ladder is U-SHAPED** (−0.125/−0.122/−0.103/−0.127, r4 dip CI
   excludes 0; HR⇒TB≥4 containment dilutes r4). **Interpolation/extrapolation of
   rungs is BANNED** — exact rung keys only, fall through the chain otherwise.
3. hr|spread:same is ladder-FLAT (single entry +0.241); all other spread×prop
   cells rung-keyed. ml|tb oriented = ±0.25 (rung-flat; exact negation verified).
4. rfi×props measured: hit +0.065, hr +0.091, tb +0.085, hrr +0.122 (old labeled
   priors 3-of-4 off; hrr had been CLIPPED by its own cap).
5. Deep rungs: hr2+×total +0.306; hit×ks facing −0.126/−0.149/−0.160 (r1/2/3);
   hrr×total rungs (STARTERS frame) 0.379/0.407/0.437/0.468.
6. NOT WIRED (fail-closed): hr3+×total (precision-starved), tb×ks slopes,
   unmeasured teammate :same orientations.

## Other Phase-1 tracks (COMMITTED)

- **W12 per-print mode** live in BOTH harnesses (+ migrate-printed-times).
  Validated: MLB within-2¢ 94→98%, mean|err| 0.66→0.47¢.
- **D1**: dnp doc amended (MLB rain-scalar ~1-2%/game-days falsifies
  "strictly binary"; prop DNP stricter: START+1PA/1-pitch; operator
  re-affirmation of reactive stance flagged). `tools/mvec_eligibility_scan.py`
  built+run clean (families stable).
- **D2**: MLB absent from demo collections → farm probe untestable (90
  candidate pairs saved in ph1 scratch); no DH on board; **KS-cell autopsy:
  makers charge a 2-4pp risk premium on illiquid Ks legs and settlement sides
  with US** — not our bug.
- **D3**: mixed-flow audit (52.69% of MLB combos carry foreign legs) —
  **KXWNBAPTS is the #1 foreign gap** (17,146 same-game pairs at flat +0.6,
  distinct players, exact MLB-props-shaped fix); PGA make-cut parlays truth≈0
  priced +0.6; KXETH15M period-flag trap (latent); NBA/NFL/NHL props arrive at
  season start.

## RESUME PLAN (operator-approved order, phases auto-continue)

- **PHASE 2 (re-run when spend resets):** B1 = :rN line-key lookup mechanism in
  sgp.py (rung from ticker: props trailing -N; spread TEAM+digits, ticker int =
  rung key; chain ':same:r1:r2' in pair_key leg order; fallback exact-rung →
  un-runged-oriented → plain; NO interpolation) + DO-6 basket width (+2.5c
  quote-width adder for ≥8-leg all-NO single-prop-family combos,
  config-tunable) ∥ B2 = wire `docs/calibration/phase2_wire_list.txt` verbatim
  into config.py (+KEEP/RAISE directives; respect the 3 NOT-WIRED flags) →
  B3 verify (suite, rung spot checks, MLB differential both modes, regressions).
- **PHASE 3:** pregame-only quote gate — operator directive: NEVER quote combos
  with any in-play leg (all sports), config flag to enable later; MLB tickers
  embed start time, soccer needs expiry−offset estimate; unknown → decline.
- **PHASE 4:** capstone re-backtest (per-print + full new config).
- Then: 14 demo fill e2e (sell-only UN-GATED 2026-07-10 — combo paid $1.00
  exactly, convention promoted) → 15 weekly sweep/calibration cadence →
  16 MLB blind test → E decisions (markup: pooled multi-week, props-first
  shape; per-sport kill switch; prod gates).

## NEXT STEPS
- Owner (operator): spend limit reset/raise, then say "continue phase 2".
- Standing: recorder through Jul 11 (healthy at last check); WC backtest after
  Jul 11 settlements; weekly P&L sweep + calibration ledger.
