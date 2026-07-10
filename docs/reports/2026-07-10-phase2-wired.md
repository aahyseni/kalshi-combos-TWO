# Phase 2 WIRED — rung keys + 101 measured entries + all 3 NOT-WIRED holes closed + DO-6

**Date:** 2026-07-10 (evening; re-run after the spend-limit interruption)
**Verdict:** **PASS** — suite 1133/0 (baseline 1095 + 38 new), mlb table 68 → **165
entries / 165 bands, zero orphans**, prop-carrying book improves in BOTH backtest
modes, pure-ML **bit-identical**, non-MLB config untouched.
**Executed by:** 5-agent workflow (B1 ∥ B2 ∥ B4 → B5 → B3) + independent
operator-side re-verification (suite re-run + shipped-config spot checks by hand).

## What was wired

### B1 — :rN rung-key lookup mechanism (`pricing/sgp.py`)

- Rung extraction: props = all-digit last ticker segment (4-segment shape,
  `KXMLBHIT-…-COLWCASTRO3-2`); spread = TEAM+digits suffix (digits REQUIRED —
  distinct from the digits-optional corners-team parse). **Type-gated:**
  ks/total/moneyline/rfi return no rung unconditionally.
- Chaining in pair_key leg order (`player_hit|spread:same:r1:r2`); equal-type
  pairs tiebreak ascending so keys are leg-order independent.
- Fallback chain: **exact rung key → un-runged oriented → plain → fail-closed
  default**; the `mlb:` band always resolves at the SAME level as the value.
  Unparseable rung collapses the whole suffix (never a partial chain, never a
  guess). **No interpolation/extrapolation anywhere.**
- **Surprise found + fixed:** sgp.py had NO spread×prop orientation resolver —
  every `player_*|spread:same/:opp` wire-list entry would have been unreachable.
  Added `_mlb_spread_prop_prior` (blob-anchored; :same = prop player's team IS
  the spread YES team). Pre-wire behavior provably unchanged (158 sgp-family
  tests green before the new keys landed).
- Soccer stays un-runged by construction (plain-level rung attempt is
  MLB-gated; `…-FRA2` line ints never become keys).

### B2 — the wire list, verbatim (`ops/config.py`)

- All **84** entries from `docs/calibration/phase2_wire_list.txt` wired and
  script-verified VERBATIM against the loaded config: 64 measured A1–A4 cells
  (60 new + 4 in-place rfi replacements of old labeled priors), 10 judge
  un-runged oriented fallbacks, 10 routed [D] splits.
- 6 bands RAISED per judge directives (plain spread families now span their
  measured oriented extremes, e.g. `mlb:player_hrr|spread` 0.20→0.42); 0 values
  changed on KEEP lines; 0 KEEP mismatches.
- All 20 distinct base keys verified by EXECUTING `legtypes.pair_key`.
- 3 tests updated where the new keys made routing CORRECTLY beat old fallbacks
  (e.g. facing hit×ks now takes exact `:opp:r2` −0.149 over un-runged −0.13).

### B4 — the 3 NOT-WIRED holes: **all closed by measurement** (unrelaxed judge standard)

7/7 Phase-1 regression anchors reproduced to ±0.0005 before any new number was
trusted; 25/25 judge windows PASS. Full precision in the addendum
(`docs/calibration/phase2_wire_list_addendum.txt`) + `b4_measurements.json`.

| hole (wire-list line) | verdict | value |
|---|---|---|
| `player_hr|total:r3` (89) | **PASS** | +0.357 band 0.07 — full-precision remeasure killed the stored-precision failure; 154/227 positives, CI95 hw 0.069/0.054, era shift −0.003; ladder stays monotone 0.238 → 0.306 → 0.357 |
| tb×ks extra rungs (90) | **PASS** | TB rung universe from 3.0M tape rows = {r2..r7}; **r6 = −0.127, r7 = −0.128** (12.7k/6.0k positives, hw ≤0.013) measured DIRECTLY — extrapolation ban honored; r1 doesn't exist on Kalshi; >r7 fail-closed |
| teammate `:same` (91) | **PASS** | own-team-starter channel is ~flat: hit|ks:same r1–r3 +0.013..−0.002, hr|ks:same +0.010, hrr|ks:same r2–r5 +0.017..+0.005, ks|tb:same r2–r5 +0.010..+0.006 (+ un-runged aggregates); **confirms** the line-85 "teammate ~0" blend |

17 addendum entries wired by B5 (148 → 165). Two Kalshi-real teammate rungs
outside measurement scope (hit r4, hrr r1) fall back to the un-runged `:same`
aggregates — fail-closed, not interpolated.

### B5 — DO-6 basket width (`pricing/quote.py`, `engine.py`, `ops/config.py`)

`QuoteConfig.basket_width_extra_cc = 250` (int centi-cents, tunable): +2.5¢
width component for combos with ≥8 legs, ALL legs NO-side, single prop family.
Applied after normal width, before maker-favorable rounding; widen-only
(negative tunable can't tighten). Motivated by the measured +25–35¢/$1 basket
overbid. 13 dedicated tests incl. a 200-example rounding-invariant property.

## Verification (B3 + operator re-check)

- **Suite:** 1133/0 (twice — B3's run and mine). mypy strict: only 2
  pre-existing `ising_amm.py` notes; ruff clean on all changed files.
- **Spot checks through the live resolver:** hit-r2×spread-r3:same → +0.285;
  ks×tb-r4 → −0.103; tb-r9 → un-runged −0.12 fallback; ml|tb ±0.25;
  hr|spread:same flat +0.241 at every spread rung; no rung leak from
  ks/total/ml/rfi; band level == value level in every case.
- **MLB backtest differential** (price step on cached gather, both modes,
  row-identical joins; baseline artifacts untouched):

| mode | bucket | n | median\|err\| | bias | within-2¢ |
|---|---|---|---|---|---|
| snapshot | overall | 6,128 | 0.337 → 0.334 | −0.390 → −0.360 | 93.75 → 94.47% |
| snapshot | prop-carrying | 1,330 | 0.702 → **0.646** | −0.881 → −0.746 | 81.58 → **84.89%** |
| snapshot | pure-ML | 3,207 | 0.285 (0 fairs changed) | identical | 98.82% |
| per-print | overall | 37,934 | 0.344 → 0.344 | −0.312 → −0.308 | 98.21 → 98.30% |
| per-print | prop-carrying | 2,929 | 0.583 → **0.578** | −0.684 → −0.637 | 90.95 → **92.18%** |
| per-print | pure-ML | 31,995 | 0.327 (0 fairs changed) | identical | 99.57% |

  Prop-carrying mean|err|: 1.207 → 1.091¢ (snapshot), 0.883 → 0.837¢
  (per-print). No bucket regresses at all.
- **Blast radius:** full config-dump diff = 211 paths, ALL mlb table/bands +
  the one basket tunable; soccer/nfl/nba/wnba/global bit-identical.

## NEXT STEPS

- **Phase 3 (auto-starting now):** pregame-only quote gate — never quote a
  combo with any in-play leg, ALL sports; config flag to enable later;
  unknown start time → decline (fail-closed).
- **Phase 4 after that:** capstone re-backtest (per-print + the full new
  config) → then #14 demo fill e2e (sell-only un-gated), #15 weekly cadence,
  #16 MLB blind test.
- Operator: no decisions owed this phase. Markup/kill-switch/prod remain
  E-track, pooled multi-week.
- Standing: recorder through Jul 11; WC backtest after Jul 11 settlements.
