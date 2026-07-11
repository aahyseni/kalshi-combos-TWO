# Phase 4 CAPSTONE — fresh 3-bucket re-backtest (full new config, snapshot + per-print)

**Date:** 2026-07-11 · **Config:** shipped 165-entry MLB table + rung keys +
spread×prop resolver + DO-6 (repo `648dc61`/`2163642`) · **Window:** 2026-07-06 →
2026-07-11 ~00:52Z (tape complete ~01:35-02:04Z) · **Artifact:** MLB gate page
re-issued **v3 CAPSTONE** (same URL, `docs/reports/2026-07-10-mlb-backtest-gate.html`).
**Status:** MLB ✅ · mixed ✅ · WC per-combo + defect-check ✅ · **WC per-print ✅
(completed 2026-07-11 12:02 — Phase 4 CLOSED, all buckets, both modes).**

## WC per-print (final section — first-ever WC per-print run)

- **PARITY PASS:** 2,656 stock-path keys re-priced through the unmodified
  single-process harness vs the sliced-parallel pass — **0 mismatches, 0
  extra keys** (the 10.5h sliced pass ≡ the stock harness exactly).
- **Numbers** (n=656,555 print-rows, fair recomputed just-before-EACH print):
  median |err| **1.57¢** · mean 1.98¢ · bias −1.72¢ · within-2¢ **62.1%** ·
  within-5¢ 95.1% — vs per-combo 1.61¢/58.1%. Same shape as the honest WC
  baseline; the fat-markup w2 caveat applies (clearing = fair + winning
  markup; settlement remains the unbendable ruler).
- **Zero-residual print accounting:** 723,167 trades = 656,555 priced +
  65,633 no-prior-snapshot + 397 missing-marginals + 582 unpriceable. (The
  earlier 656,555-vs-631,834 "gap" was trades-vs-unique-(ticker,timestamp)
  keys — 25,303 trades share a timestamp; identity closes exactly.)
- Artifacts: `ph4\wc\wc_fixed_printed\{fairs_perprint.pkl (64.7MB, merged),
  wc_backtest_perprint.json}`; parity in `ph4\parity_pp\`.

## Headline per bucket (all promoted = shipped config, strictly-pregame, zero-bias split)

| bucket | mode | n | med\|err\| | bias | w2 | vs comparator |
|---|---|---|---|---|---|---|
| MLB | snapshot | 6,969 | **0.34¢** | −0.43¢ | 93.3% | legacy flat-0.6: 0.35¢ / 88.2% |
| MLB prop-carrying | snapshot | 1,305 | **0.66¢** | −0.85¢ | 83.6% | legacy **1.37¢ / 57.6%** → GATE PASS |
| MLB | per-print | 53,022 | 0.38¢ | −0.34¢ | **98.6%** | legacy 97.3% |
| mixed (cross-sport) | snapshot | 3,844 | **0.70¢** | −0.87¢ | 87.5% | old-window baseline 0.74¢ / 85.2% |
| mixed | per-print | 8,555 | 0.84¢ | −0.86¢ | 92.3% | (no prior per-print baseline) |
| WC soccer | per-combo | 21,443 | 1.60¢ | −1.60¢ | 58.1% | honest baseline 1.55¢ / 59.6% at n=18,819 — **backbone reproduces at +14% sample** |

Fresh-data volume vs the old caches: +2.53M RFQ rows (+19%), +18.3h window,
MLB per-print rows +40% (37,934→53,022). Full lineage + tail decomposition on
the v3 artifact.

## The four load-bearing verifications

1. **Blast radius exact (MLB-only config change):** WC re-priced all 19,016 old
   printed inputs under the current repo — **bit-identical fairs, max delta 0**.
   Mixed same-snapshot overlap: fair changed on exactly **6 of 742** combos, all
   carrying MLB prop legs (TB/KS/HRR/HIT/HR — the rung-key targets), and their
   error improved (2.06→0.97¢ med, w2 50→83%). Zero changes on WC-only and
   no-same-game combos.
2. **Cross-sport independence EXACT through the live path:** all 2,605
   pure-cross combos price at the product of leg fairs to <1e-6; live
   `build_sgp_correlation` spot-checks show cross-sport ρ +0.0000 with same-event
   pairs consulting the measured priors (notes exposed per pair).
3. **Look-ahead signature replicated on purpose (A/B control):** mixed
   fixed-vs-lookahead reproduces the forensics — lookahead med 3.31¢ vs fixed
   0.70¢ on differing rows; settled-YES fairs drift +8.73¢ vs settled-NO −2.05¢
   (baseline +10.47/−2.10). The pregame-filtered policy stays vindicated.
4. **Settlement calibration (thermometers, no refit):** mixed bucket —
   settled-YES **15.28% vs mean fair 15.25¢** (n=2,892 resolved; clearing 16.18¢
   = ~0.9¢ maker markup above fair) — essentially perfectly calibrated. MLB
   window ran favorite-hot again (22.2% YES vs 17.5¢ fair, n=3,128) → pooled
   multi-week ledger item. WC settled-YES 8.9% (longshot-heavy flow, n=14,359).

## Found + fixed during the capstone

- **Harness bug (repo-fixed `2163642`):** `mlb_backtest.py` gather KeyError on
  never-snapshotted combos (outcomes covers only priceable; inputs spans all
  ticker_legs). Latent since the look-ahead fix; first truly-fresh gather
  exercised it. Fix validated end-to-end via a verbatim job-tmp replica BEFORE
  porting (rule 8 flow); `wc_backtest.py` verified immune.
- **Audit-draw churn caveat (methodology):** the fetch policy's seeded 20k audit
  sample re-draws over a grown untraded pool, so fresh runs are NOT supersets of
  old runs (MLB shared rows 2,210/6,128; mixed 782). Aggregates comparable;
  per-ticker longitudinal comparisons only on shared/candidate subsets.
- Config parity on shared tickers vs ph2 (identical config): 2,073/2,210
  bit-identical; the 137 diffs are all data-side (more pre-cutoff snapshots
  attached by the longer scan).
- 37 combos WARN clearings-incomplete (retry-exhausted 429s) — negligible vs
  23,293 fetched, recorded.
- Prior mixed attempt died one-by-one-fetching an 85k clearings universe; the
  re-run reused the one-pass gather's caches with **zero** API fetches
  (`ph4\mixed\PROVENANCE.txt`).
- **Trust link (b) CLOSED — PARITY-EXACT** (operator-approved re-run):
  232-combo stratified sample (6 config-effect, 6 UNKNOWN, 70 prop-carrying,
  80 pure-cross, 40 wc2+mlb1, 30 same-event) re-priced through the unmodified
  stock `_build_pricer` path: **232/232 bit-identical, max delta 0.0¢**
  (`ph4\mixed\parity_tonight.json`).
- **The 6 UNKNOWN no-quotes root-caused** (`ph4\mixed\unknown_diagnosis.json`):
  NOT unrecognized families — all 6 are a **logical containment pair inside a
  larger combo** (soccer ML + same-match total-over-0.5: win ⟹ ≥1 goal, inside
  4-6 leg combos). The classifier's exact reason: "containment pair inside a
  larger combo: not modeled" → UNKNOWN → decline (defense #2 working —
  independence would OVERSTATE fair). Fix designed, mirrors the nested-band
  collapse: A⟹B ⇒ drop the implied leg, P(A∧B)=P(A), price the reduced combo.
  **SIZED in pure-WC** (`ph4\wc\containment_frequency.json`): 227 printed
  combos / 712 prints ≈ **91 declined prints/day** — 1.03% of printed combos,
  0.098% of prints; the ONLY material UNKNOWN species in WC flow. Dominant
  shape is **1H-BTTS ⟹ FT-BTTS** (127 combos), then ml⟹over-0.5 (70), 1H⟹FT
  same-line totals (29); spread evenly across 3-6 legs (general N-leg fix
  required). Pure 2-leg containment already prices via `price_containment`
  (13 combos / 312 prints — untouched). 194 resolved decliners = free OOS
  validation set. **Fix IN PROGRESS in an isolated worktree** (main tree
  untouched while the per-print pass imports from it); merges after the WC
  per-print pass completes.
- **Residual audit (operator rule: no unexplained residuals — every "N of M"
  must name the M−N):** exact set arithmetic over the fix
  (`ph4\containment_residuals.json`, symmetric diffs asserted in code). The
  implementer's "225 plans + 5 guards = 230" CONFLATED two guards: the gate
  hits are **227 = 225 PLAN + 2 GUARDED, identical set to the old 227
  (symmetric difference = ∅)**; the other 3 are PRE-EXISTING nested-band
  companion declines (corners windows), byte-identical notes old vs new, never
  in the gate. The 2 GUARDED, each explained: (1) FRA-win + FT-over0.5-YES +
  1H-over0.5-NO — one leg claimed by both a containment drop and a band window
  ("goal, but not before half-time"); guard CORRECT, solvable EXACTLY by a
  containment-drop precedence rule (combo ≡ FRA-win ∧ no-1H-goal — no
  measurement needed). (2) 5-leg ESPBEL — post-collapse band ("3rd goal after
  half-time") with same-game companions; guard CORRECT (would smuggle an
  unmeasured band-vs-companion ρ); solvable EXACTLY by difference-of-parlays
  (width summed, never differenced). **Full tape accounting, remainder = 0:**
  245 containment-adjacent combos = 225 PLAN + 2 GUARDED + 3 band-guard
  UNKNOWN (unchanged) + 13 pure-2-leg (already priced, set unchanged) + 2
  IMPOSSIBLE (farmable=True re-verified). Mixed: 6/6 → PLAN, remainder 0.
  Operator decision open: engine-surface the two exact-algebra TODOs (worth 2
  combos of observed flow) or keep fail-closed.
- **Historical audit (operator challenge: "soccer was complete — regression?"):
  NOT a regression.** The decline branch was born 2026-07-07 in `6325dbb` — the
  SAME commit that wired the 1H×FT soccer correlations (the 1H legs created the
  shape; the in-larger-combo case was deliberately fail-closed that day). The
  old honest baseline carried it at the identical rate: its 499 unpriced
  printed combos = 302 snapshot-empty + **197 containment declines + 0 other**
  (197/19,318 = 1.02% vs fresh 227/21,968 = 1.03%). No published soccer stat
  ever included these (declines produce no error rows) and no money was quoted
  on them. **Process lesson codified:** decline-reason histograms must be
  printed by every backtest analyze step (MLB's DO-9 counters do; the WC
  analyzer didn't) — silence must be enumerable. WC analyzer gets the
  histogram after the per-print pass completes (no mid-pass edits).
- **Within-2¢ decomposed** (`ph4\mixed\w2c_decomposition.json`): 86% of misses
  are one-sided (fair BELOW clearing = markup + winner's curse in the prints);
  the fixed 2¢ band is relatively harsher on expensive combos (≤5¢: 99.1% w2 →
  35¢+: 68.8%); recentering on the median bias alone recovers a third of the
  shortfall (87.5→91.1%); single- vs multi-print identical (not print noise).
  The genuine drag is soccer same-event pairs (ADVANCE×CORNERS, ADVANCE×PGOAL
  ×TOTAL) — **the identified lever is measuring soccer pair priors
  (corners|advance, pgoal|total, btts|advance), not chasing clearing prints.**
  Against settlements (the ruler that can't bend) the bucket is centered to
  0.03¢.

## Watch list (measurement-grade, pre-registered — never refit on P&L)

- Illiquid prop-only parlays (HRR/HIT/KS): the only cells ≥1.5¢ (max 2.01¢,
  n=26); bias −1.4..−2.6¢ = fat-markup-shaped; weekly settlement ledger decides
  (makers-side settlements 3-4 independent weeks → measurement investigation).
- `mlb_runs` structural grid: config-independent 1.10¢ slice — own calibration
  pass queued.
- Mixed weakest cell wc2+mlb1 (n=385, 1.11¢) is WC-pair-heavy — a soccer
  same-event correlation item, not MLB config.

## NEXT STEPS

- Me: WC per-print pass completes (overnight) → merge parts → harness analyze →
  stock-vs-parallel parity gate → append the WC per-print section here.
- Then the runway: **#14 demo fill e2e** (sell-only un-gated) → **#15 weekly
  sweep/calibration cadence** (game-clustered, bucket-split) → **#16 MLB blind
  test** → **E decisions** (markup from pooled multi-week; per-sport kill
  switch; prod gates).
- Operator: no decisions owed. Artifact v3 live at the existing URL.
