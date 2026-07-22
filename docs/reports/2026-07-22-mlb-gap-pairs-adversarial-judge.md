# MLB gap-pairs tranche — ADVERSARIAL JUDGE verdict

**Date:** 2026-07-22 · **Judge posture:** default-REFUTE, run-code proof required.
**Scope:** the just-wired GAP-PAIRS tranche (16 flat-gaps + 7 labeled-prior
tightens, OUTS/RBI/SB) in `src/combomaker/ops/config.py`
(`pair_rho_by_sport["mlb"]` + `pair_rho_uncertainty`, now **327 keys**), staged in
`docs/calibration/staged_mlb_gap_pairs.md`, reported in
`docs/reports/2026-07-22-mlb-gap-pairs-measured.md`.
**Method used:** re-derived spot values from the Retrosheet caches
(`data/history/newprops_cache/*.pkl.gz`) by recomputing joint frequencies and
inverting the SHIPPED copula (`combomaker.pricing.copula.gaussian_copula_joint_prob`)
myself — I did **not** read the staged doc and nod. Config integrity + routing +
firing proven by loading the shipped `CorrelationConfig` and running
`combomaker.pricing.sgp.build_sgp_correlation` on constructed real-shaped tickers.
Isolation proven by a **real `git diff 5b14e01`** (working tree, tranche
uncommitted). READ-ONLY on src/config/tests — I ran code and wrote only this report.

## Attack table

| # | Attack | Verdict | Proof / numbers |
|---|--------|---------|-----------------|
| 1 | Re-derive 4 gap spots independently; confirm sign+magnitude; outs facing uniformly deeper than ks siblings | **CONFIRMED** | Recomputed from caches (1,024,568 rbi_ok batter rows, 98,680 outs_ok starter rows): `outs\|hit:opp` **−0.2088** (wired −0.21, Δ0.001), `outs\|hrr:opp` **−0.3186** (wired −0.32, Δ0.001), `outs\|outs` **+0.1611** (wired +0.16, Δ0.001), `ks\|rbi:opp:r1` **−0.1569** (wired −0.16, Δ0.003). All signs match. outs deeper than ks: hit −0.209 vs −0.125, hrr −0.319 vs −0.175 (both **True**). |
| 2 | Collapse decision: `_pair_rung_suffix` chains → single-rung keys dead; un-runged `outs\|rbi:opp`/`rbi\|rbi:same` FIRE; bands span; other both-runged pairs not mis-wired | **CONFIRMED** | `_pair_rung_suffix(OUTS r15, RBI r2)` → `':r15:r2'` (chains), `(RBI r2, RBI r1)` → `':r1:r2'` (chains). Priced combos: `player_outs\|player_rbi:opp`=**−0.300** fires; `player_rbi\|player_rbi:same`=**+0.070** fires (source notes read from `build_sgp_correlation`). Single-rung `outs\|rbi`/`rbi\|rbi` keys **absent** from config (no dead config); no chained keys either (so `:opp:r15:r2` correctly falls through to un-runged). Bands: `outs\|rbi:opp` 0.06 spans −0.287..−0.337 ✓; `rbi\|rbi:same` 0.04 spans 0.06..0.08 ✓. Other both-runged: `rbi\|spread` correctly wired 24 CHAINED `:rN:rN` keys, fires `:same:r2:r2`=+0.38; `sb\|spread` single-rung `:r2`=+0.13 fires; `ks\|rbi` single-rung `:opp:r1/r2`=−0.16 fires. |
| 3 | `:opp` measured, not negated (asymmetric); outs×batter `:opp`/`:same` both measured | **CONFIRMED** | `rbi\|spread`: same:r1:r2=+0.33 vs opp:r1:r2=**−0.30** (≠ −same); same:r3:r5=+0.42 vs opp:r3:r5=**−0.30** (≠ −0.42). `sb\|spread`: same:r2=+0.13 vs opp:r2=**−0.17**; same:r5=+0.06 vs opp:r5=**−0.18** (genuinely asymmetric). outs×batter `:opp` (facing) and `:same` (teammate) both present, `\|same\|«\|opp\|` for all six (e.g. hrr −0.32/+0.02, tb −0.23/+0.02) — teammate ≈0 measured directly, not a negation of facing. |
| 4 | ZERO-GAPS completeness: no reachable outs/rbi/sb pair resolves to flat +0.6 | **CONFIRMED** | Enumerated 3×12 = 36 pairs across all reachable MLB leg types; **every** pair has ≥1 config entry. Rigor check: **every** new-family pair also has a PLAIN base key (0 missing), so team-routing parse-fail lands on a wired value, never flat. 30 distinct new-family base keys present. |
| 5 | Config integrity: 327↔327 bands, 0 orphans/missing, sort, sign-span, band ≥ era | **CONFIRMED** | 327 mlb `pair_rho` keys ↔ 327 `mlb:` band keys, **0 orphans, 0 missing**. **0** unsorted base keys (all via `legtypes.pair_key`). All plain fallbacks sign-span their oriented pair. Band ≥ era spot-checks all pass (`outs\|outs` 0.05 ≥ era 0.042; `outs\|rbi:opp` 0.06 ≥ 0.042; `hrr\|outs:opp` 0.04 ≥ 0.027; `ks\|sb:opp` 0.04 ≥ 0.020). |
| 6 | Isolation: only new-family keys changed vs HEAD `5b14e01` | **CONFIRMED** | Real `git diff 5b14e01 -- config.py`: 240 changed quoted-key lines, **0** without outs/rbi/sb; **0** removed lines modifying a pre-existing key value; 99 added `mlb:` band lines, **0** off-family. Gap-tranche block (lines 1391–1511) = 95 pair_rho keys, all touch outs/rbi/sb. Pre-existing keys unchanged (spot: `player_ks\|total`=−0.25, `moneyline\|player_hrr:same`=0.37, `player_hrr\|player_hrr:same`=0.17). Soccer table (110 keys) untouched. |

**Suite:** `2613 passed, 3 deselected` (full `tests/`, 159s) at working tree.

## The deliberate wiring deviation — VERIFIED SOUND

`outs×rbi` and `rbi×rbi` were wired **un-runged** even though the staged wire list
enumerated single-rung keys. Run-code proof this is correct, not a defect:
`_pair_rung_suffix` on two rung-keyed legs **chains** `:r{a}:r{b}` (proven:
`:r15:r2`, `:r1:r2`), so a single-rung key (`…:opp:r1`) would be **dead config** —
the runtime looks up the *chained* key, misses, and falls to the un-runged
`:opp`/`:same`. The wirer collapsed to the un-runged entry whose band spans the
measured ladder (both verified above). The contrast case confirms the wirer
understood the rule: `rbi×spread` (also both-rung-keyed) was wired as 24 **chained**
`:rN:rN` keys and fires per-rung. **No dead single-rung keys exist for any
both-rung-keyed gap-pair family.**

## Minor findings (NON-blocking; not gap-pairs defects)

1. **Band tightening during wiring (SAFE):** `mlb:player_outs|player_outs` band is
   **0.05** in config (comment: "covers era shift −0.042") vs **0.04** in the staged
   doc §Bands. Config is the *safer* value (0.05 ≥ 0.042). The `rbi|rbi:opp:r3` staged
   band 0.07 became `rbi|rbi:opp` band 0.04 on collapse — defensible: the collapsed
   `:opp` value is 0.00 and the measured opp ladder is uniformly ~0 (r1..r3:
   −0.010/−0.008/−0.004, era shifts <0.008), so 0.04 spans it. Both are wiring-time
   improvements, not drift.

2. **Pre-existing dead-config in the EARLIER (2026-07-21) new-props tranche —
   OUT OF SCOPE, flagged for the owner:** `player_outs|spread:opp:r2..r5` /
   `:same:r2..r5` (config lines ~1352–1359) were wired **single-rung** on the spread
   margin, but OUTS is *also* rung-keyed, so `_pair_rung_suffix` chains `:r15:r5` —
   the same trap the gap-pairs tranche correctly avoided. Those per-rung outs×spread
   keys are **DEAD**; the pair always resolves to the un-runged `:opp`=−0.52
   (band 0.11) / `:same`=+0.35 (band 0.09). **This is NOT a ZERO-GAPS violation** (no
   flat pricing) and **NOT mispricing** (the un-runged point is central to the
   −0.49..−0.55 / +0.32..+0.38 ladders and the wide band spans them). It only means
   the outs×spread *margin refinement* is silently unused. Fix is trivial when
   convenient (re-wire as `:opp:r{outs}:r{spread}` chains) but does not gate arming.

## NEXT STEPS

- **Runs next (owner: orchestrator):** none required to arm on the gap-pairs
  tranche — it is SHIP-grade. Optionally re-wire the earlier-tranche
  `player_outs|spread` per-rung keys as chained `:rN:rN` (finding 2) to activate the
  margin ladder; low value, non-blocking.
- **Owner (operator):** confirm the `:same`/`:opp` facing resolver behavior on live
  MLB tickers matches the constructed-ticker proofs here (DET-starter × CHC-batter →
  `:opp`; DET-starter × DET-batter → `:same`). The routing was exercised on
  real-shaped tickers from
  `docs/reports/2026-07-22-mlb-newprop-series-kalshi-verification.md`.
- **Decision owed:** none blocking. No family graded NO-QUOTE; no flat-gap remains
  for OUTS/RBI/SB.

## VERDICT

**SHIP.** All six attacks CONFIRMED with run-code / independently-recomputed proof;
the deliberate un-runged collapse is verified sound; config integrity is exact
(327↔327, 0 orphans/missing/unsorted); ZERO reachable OUTS/RBI/SB pair prices the
flat +0.6 default; isolation is clean against HEAD `5b14e01`; suite 2613/0. The one
dead-config issue found belongs to the *earlier* new-props tranche and causes
neither a flat gap nor mispricing.
