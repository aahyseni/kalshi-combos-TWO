# MLB backtest gate: PASS · DO-1 + DO-7 wired · differential clean

**Date:** 2026-07-10 ~05:20 UTC · **Commits:** `2eaebc8` (gate tool + DO-1 + DO-7),
`fe396cc` (artifact) · **Artifact:** the gate report with by-composition +
by-leg-count breakdowns → `docs/reports/2026-07-10-mlb-backtest-gate.html`
(deployed: claude.ai/code/artifact/70c9a61c-…) · Steps 0–1 of the approved order.

## Step 0 — the gate (rule-8 validation of the promoted table): **PASS**

`tools/backtests/mlb_backtest.py` (wc_backtest-mirrored, dual-config, zero-bias,
strictly-pregame): 13.08M rfq rows → 691,896 MLB-strict combos → 182,097
priceable → **6,647 pregame-cleared, priced under BOTH configs**.

| bucket | n | promoted | legacy flat-0.6 |
|---|---|---|---|
| prop-carrying | 1,533 | **1.25¢ · 62% w2** | 2.22¢ · 48% |
| props-only | 812 | **0.75¢ · 75% w2 · bias −1.03** | 1.19¢ · 62% · +1.04 |
| prop-leg→copula declines | 600 | **3.39¢** | 8.46¢ |
| game-lines-only | 5,114 | 1.00¢ (no regression) | 1.02¢ |
| cross-game parlays | 5,226 | identical both configs (consistency proof) | — |

Settlement bonus (5,507 resolved): realized YES 18.0% · promoted fair 18.4¢ ·
legacy 19.1¢. **Markup lens (operator point):** clearing = maker fair + markup,
so a healthy model shows bias ≈ −markup — props-only at **−1.03¢ is the healthy
signature**; game-lines +3.08¢ (and ML×2 at 18.3¢ median) means our fair sits
above fair+markup → winner's-curse territory, markup/quoting-policy work, not
correlation work.

## Step 1 — DO-1 + DO-7: wired, verified, differential exact

- 11 untabled cells → typed (spread×props, ml|tb, rfi×props); mlb table now
  **43 entries / 43 bands, zero orphans**. ml|spread deliberately untabled
  (containment phase). DO-7 fixture pins event_mutually_exclusive (6 props
  false, GAME true) + behavioral tests.
- **Differential: ZERO violations** across 115,401 same-game pairs / 182,097
  combos — exactly the 11 cells moved (8,433 hits; +843 ml|spread unchanged =
  9,276, exact reconciliation with baseline), legacy fairs bit-identical,
  unaffected promoted fairs bit-identical. Post-DO-1: mixed bucket 2.16→1.98¢
  (48→50% w2); flat-fallback combos 156→15 (ml|spread only).
- Note: the wire agent lost its report to an API connection drop AFTER
  completing its work; orchestrator re-ran all gates (1019 passed / 0 failed,
  ruff+mypy clean) before committing. Final verify agent: ALL 6 CHECKS PASS.

## New follow-ups surfaced by the gate

1. **mlb_runs NegBin grid: 5.53¢ median error, +4.21¢ bias** (n=232, config-
   independent) — 5× the copula's game-line error; needs its own calibration
   pass (new item, not previously known).
2. **ML×2 parlays: 18.3¢ median gap** — winner's-curse tail concentrated;
   markup-decision-relevant (echoes WC).
3. Combo-trades poller sees ~2.5k of ~84k traded MLB combos (45.9% of
   "untraded" combos actually traded) — tape-sourcing vindicated; poller is
   monitoring-only.
4. Neutralized spread×prop priors slightly overshoot downward vs tape
   (bias −1.98 on the affected cleared subset, n=143) — within bands, and the
   healthy side under the markup lens; DO-8 measurement will replace them.

## NEXT STEPS

- **Now (approved order steps 2–3):** wire BANDS (atomic: relationships +
  engine + BOTH backtest dispatch mirrors) and ROUTING (fix the broken staged
  invariant test first; parity vs proto_resolver.py) — staged code extracted to
  job-tmp design/.
- Then DO-2 same-player containment (regression fix), DO-3 ml|spread.
- **Owner (operator):** artifact regen offer standing (same-game/cross-game
  split per composition + markup-adjusted bias view).
