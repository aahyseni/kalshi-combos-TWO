# MLB gap pairs MEASURED — OUTS/RBI/SB reachable pairs off flat/labeled priors

**Date:** 2026-07-22 · **Status: STAGED, NOT WIRED** (rule 8 — measurement only;
live modules pristine; wiring + re-verification owned by the orchestrator).
**Blast radius:** ZERO on live pricing/throughput — a new file under `tools/`
that reads the existing parse caches; no `src/`, `config/`, or `tests/` edit.
**Deliverable wire list:** `docs/calibration/staged_mlb_gap_pairs.md` (the
canonical handoff — survives compaction). **Results artifact:**
`data/history/gap_pairs_results.json`.

## Headline

The OUTS/RBI/SB families shipped through Stage 6+7 sign-off (2026-07-22) but a
Stage-3 gap-ledger enumeration found **23 reachable same-game pair families
STILL on the flat +0.6 default or on GUESSED labeled priors** — including the
one named fail-closed gap the differential judge flagged
(`player_hit|player_outs`, band spanning 0). This tranche MEASURES all 23 from
Retrosheet 2005-2025 to close the zero-gaps ledger. **Every one clears the
min-n gate and the reconciliation bar; zero NO-QUOTE cells; zero sign flips.**

| pair family | orient | measured ρ | band | n | verdict |
|---|---|---|---|---|---|
| outs × hit | :opp (facing) / :same | **-0.21** / +0.03 | 0.04 | 750,529 / 751,151 | SHIP (deeper than ks×hit -0.126) |
| outs × hr | :opp / :same | **-0.17** / +0.01 | 0.04 | 750,529 | SHIP (deeper than ks×hr -0.075) |
| outs × tb | :opp / :same | **-0.23** / +0.02 | 0.04 | 750,529 | SHIP |
| outs × hrr | :opp / :same | **-0.32** / +0.02 | 0.04 | 750,529 | SHIP (deeper than ks×hrr -0.19) |
| outs × rbi | :opp:r1/r2/r3 / :same | **-0.29/-0.32/-0.34** / +0.01 | 0.04 | 750,529 | SHIP (per-rbi-rung; ladder non-flat) |
| outs × sb | :opp / :same | **-0.13** / +0.05 | 0.04 | 757,208 | SHIP |
| outs × outs | (opposing) | **+0.16** | 0.04 | 53,726 | SHIP (like ks|ks +0.04, deeper) |
| sb × hr | :same / :opp | +0.01 / -0.02 | 0.04-0.05 | 9.9M / 10.9M pairs | SHIP WIDEN-ONLY |
| sb × tb | :same / :opp | +0.02 / -0.02 | 0.04 | 9.9M / 10.9M | SHIP WIDEN-ONLY |
| sb × hrr | :same / :opp | +0.04 / -0.03 | 0.04 | 9.9M / 10.9M | SHIP WIDEN-ONLY |
| sb × ks | :opp (facing) / :same | -0.04 / +0.03 | 0.04 | 771,608 | SHIP |
| rbi × spread | :same / :opp (chained) | **+0.33..+0.42** / **-0.24..-0.35** | 0.04 | 1,024,568 | SHIP (like ml×rbi ±0.33) |
| sb × spread | :same / :opp | +0.06..+0.13 / -0.17..-0.18 | 0.04 | 1,033,932 | SHIP (asymmetric — measured both) |
| rbi × rfi | (orient-free) | **+0.10** | 0.04 | 1,024,568 | SHIP (flat; between hit +0.065 / hrr +0.122) |
| sb × rfi | (orient-free) | +0.02 | 0.04 | 1,033,932 | SHIP ≈0 |
| rbi × rbi | :same:rN / :opp:rN | +0.06..+0.08 / ~0 | 0.04-0.07 | 9.8M / 10.8M | SHIP WIDEN-ONLY |
| hit × rbi | :same / :opp | +0.08 / ~0 | 0.04 | 9.8M / 10.8M | TIGHTEN (was 0.06) |
| hrr × rbi | :same / :opp | +0.14 / ~0 | 0.04 | 9.8M / 10.8M | TIGHTEN (was 0.10; < hrr|hrr 0.17) |
| tb × rbi | :same / :opp | +0.08 / ~0 | 0.04 | 9.8M / 10.8M | CONFIRM (was 0.08) |
| hit × sb | :same / :opp | +0.01 / -0.02 | 0.04-0.05 | 9.9M / 10.9M | TIGHTEN DOWN (was 0.05) |
| rbi × sb | :same / :opp | +0.05 / -0.03 | 0.04 | 9.9M / 10.9M | MEASURE (was plain 0.03) |
| ks × rbi | :opp:r1/r2/r3 (facing) / :same | **-0.16/-0.16/-0.17** / +0.01 | 0.04 | 764,802 | TIGHTEN (was flat -0.12; per-rung) |
| sb × sb | :opp | ~0 (-0.003) | 0.06 | 10.9M pairs | SHIP WIDEN-ONLY (teammate +0.10 shipped) |

## Method (mirrors `staged_mlb_new_props.md` EXACTLY)

Reused the 21-year Retrosheet parse caches from the new-props tranche
(`data/history/newprops_cache/*.pkl.gz`) — the same parse that reconciled to
official game logs at RBI 99.1% / SB 100% / OUTS 99.8% / HR 100% (49,490 joined
games; 1,033,950 batter-game / 98,894 starter-game rows). No re-parse; the tool
(`tools/calibrate_mlb_gap_pairs.py`) is additive and imports the SHIPPED copula
via `implied_rho` (`combomaker.pricing.copula.gaussian_copula_joint_prob`) and
the LIVE `legtypes.pair_key`. Rank-1 raw joint frequencies → Rank-2 bisection
copula inversion → CI99 binomial SE through the monotone solver (`_Z99=2.576`) →
era split train 2005-19 / holdout 2020-25 → cluster-floor CI (n=distinct
team-games) on every team-context / combinatorial pair → band = max(0.04, CI99
hw, |era shift|). Min-n gate 50,000.

**pair_key verification (sort traps EXECUTED):** the tool prints all 23 keys.
Confirmed traps: `outs|hit`→`player_hit|player_outs`, `outs|tb`→
`player_outs|player_tb`, `sb|hr`→`player_hr|player_sb`, `sb|tb`→
`player_sb|player_tb`, `ks|rbi`→`player_ks|player_rbi`. The pair_key leg order
determines which rung a chained `:rN:rN` suffix names (rbi first in
`player_rbi|spread`, so `:same:r1:r2` = rbi 1+ × margin ≥2).

## Reconciliation / parse evidence (reused, re-affirmed)

The gap-pairs join sits on the same reconciled rows: batter rows 1,033,950
(rbi_ok 1,024,568 = 99.1%, sb_ok 1,033,932 = 100.0%), starter rows 98,894
(outs_ok 98,680 = 99.8%). The pitcher×batter join (each game's batters ×
opposing/own starter, both outs_ok and stat_ok) yields 1,021,909 facing /
1,021,893 same rows on the rbi frame — a 99.4% join rate against the batter
population, confirming the game-code keying is clean (no orphan batters). The
`:opp` (facing) and `:same` (teammate) frames are built by side arithmetic
(`batter side != starter side` ⇒ facing), never by negation.

## Per-family findings

### OUTS × batter (pitcher × facing/same batter) — the largest flat-gap block

`:opp` (the batter bats AGAINST that starter) is uniformly NEGATIVE and
uniformly DEEPER than the shipped ks×batter facing siblings — exactly as a
full-outs stat should be (outs embed "the starter went deep = suppressed this
lineup"):

| stat | outs×prop :opp (facing) | shipped ks×prop :opp | ratio |
|---|---|---|---|
| hit | **-0.209** | -0.126 | 1.66× |
| hr | **-0.172** | -0.075 | 2.29× |
| tb | **-0.232** | -0.12 (r2) | ~1.9× |
| hrr | **-0.319** | -0.190 | 1.68× |
| rbi | **-0.287** (r1) | -0.157 (measured here, ks×rbi) | 1.83× |
| sb | **-0.130** | -0.036 (measured here, sb×ks) | 3.6× |

`:same` (teammate) is ≈0 across the board (+0.009..+0.052) — the environment
channel only, same as the shipped ks:same ~0. Both measured directly; `:same`
was NOT negated from `:opp`.

**outs × rbi ladder is NON-FLAT on :opp** (-0.287 → -0.315 → -0.337, monotone,
CI99s effectively disjoint at the ±0.007 naive width) → staged **per-rbi-rung**.
On `:same` the ladder is flat ~0 (r1-r3 within 0.011) → single un-runged entry.

### OUTS × OUTS (opposing starters): **+0.161**

Like the shipped `player_ks|player_ks +0.04` but stronger — two opposing
starters both going deep co-occurs in low-scoring pitchers' duels (a shared
game-environment factor the live marginals don't fully absorb). n=53,726
opposing-starter orderings across 45,764 games; cluster CI99 [+0.131,+0.191];
era +0.171→+0.130 (d -0.042, the openers-era attenuation, band covers it).

### SB × batter (distinct-player teammate/opp)

All small and WIDEN-ONLY (SB base rate 4.96%). Teammate slightly positive
(+0.01..+0.04), opponent ~0 (-0.02..-0.03) — the exact hr|hr shape. Ordered-pair
n in the 9.9-10.9M range with cluster n = 98,978 (teammate) / 49,489 (opposing).

### RBI × SPREAD (team-signed, chained rungs): the strongest new oriented block

Sits right on the shipped `ml×rbi ±0.33` anchor and rises with the RBI rung
(RBI *is* run creation — it loads on the margin almost as hard as it loads on the
win). `:same` (the batter's team covers) is ~flat across the spread margin rung
(+0.334→+0.342 at rbi 1+) but rises with rbi rung (r1 +0.33 / r2 +0.38 / r3
+0.40). `:opp` (the opponent covers) declines with margin (-0.295→-0.244) and
deepens with rbi rung. Both sides chained-per-rung; `:opp` is genuinely
asymmetric to `:same` (measured directly, not negated).

### SB × SPREAD (team-signed): genuinely asymmetric

`:same` declines with margin (+0.128 → +0.063 at r2→r5); `:opp` is ~flat and
deeper (-0.171 → -0.179). The asymmetry (|:opp| > |:same|, and the opposite
rung-slope) is exactly the `spread:opp` asymmetry the new-props measurement judge
flagged for OUTS — measured BOTH directly, per B2.6.

### RBI/SB × RFI (orientation-free)

`rbi×rfi` = +0.10, flat across rbi rungs (+0.103/+0.107/+0.115), sitting between
the shipped `hit×rfi +0.065` and `hrr×rfi +0.122` — coherent (RBI is a
run-credit stat). `sb×rfi` = +0.02 ≈0 (a stolen base is independent of a
first-inning run by either team).

### RBI × RBI (distinct-player teammate/opp)

Teammate +0.06..+0.08, opponent ~0 — the hr|hr shape at the RBI base rate. The
deep rungs (r3) carry wider WIDEN-ONLY bands (teammate 0.05, opponent 0.07) off
the small deep-cell cluster CI; both ~0-to-small, SHIP-grade WIDEN-ONLY.

### LABELED-PRIOR TIGHTEN block — all guesses CONFIRMED, zero sign flips

| key | wired guess | measured | move |
|---|---|---|---|
| player_hit\|player_rbi:same | 0.06 | +0.081 | tighten up |
| player_hrr\|player_rbi:same | 0.10 | +0.139 | tighten up (< hrr\|hrr 0.17 cap) |
| player_rbi\|player_tb:same | 0.08 | +0.084 | **confirmed** |
| player_hit\|player_sb:same | 0.05 | +0.014 | tighten DOWN (~0) |
| player_rbi\|player_sb (plain) | 0.03 | +0.05/-0.03 split | now oriented |
| player_ks\|player_rbi:opp | -0.12 flat | -0.157/-0.160/-0.168 | tighten deeper, per-rung |
| player_ks\|player_rbi:same | 0.01 | +0.013 | **confirmed** |
| player_sb\|player_sb:opp | (unmeasured) | -0.003 ~0 | measured ≈0 |

`ks×rbi:opp` measured -0.16..-0.17 lands inside the pre-registered band
(bounded by hit|ks -0.126 / hrr|ks -0.19) — deeper than the -0.12 guess but
sign-correct. `hrr×rbi:same` +0.139 sits just under the `hrr|hrr` teammate cap
of 0.17, as expected.

## Sign-checks (law: any sign differing from a sibling ⇒ investigate)

**Zero sign flips.** Every measured value matches its pre-registered / sibling
sign (full table in `staged_mlb_gap_pairs.md` §Sign-check). The OOS-FLAGs that
fire (marked `[OOS-band-covered]` in the wire list) are all the giant-n artifact:
naive CI99 ±0.008 so a +0.01-0.04 era shift trips the flag, but every band is
sized off `max(0.04, hw, |era shift|)` and covers the shift. No structural
concern.

## Residual / queue accounting (law #4)

- **Directive PAIRS TO MEASURE:** 16 flat-gaps + 7 labeled-prior-tighten = **23
  pair families. All 23 MEASURED** (each expands to its orient/rung cells).
- **NO-QUOTE / UNMEASURED cells:** NONE. Smallest direct-frequency cell
  `outs×outs` n=53,726 (> 50k gate). Combinatorial cells carry ordered-pair n in
  the millions, cluster n ≥ 49,051. No min-n gate hit; no reconciliation problem.
- **Queue closed:** this tranche closes `staged_mlb_new_props.md` NEXT STEPS
  item 3 (the queued distinct-player and opposing-batter gap leftovers).
- **After wiring:** every one of the 23 leaves the flat +0.6 / labeled prior →
  the OUTS/RBI/SB gap ledger is EMPTY.

## Reproduce

```
.venv/Scripts/python.exe tools/calibrate_mlb_gap_pairs.py report 2005 2025
# reads data/history/newprops_cache/*.pkl.gz (reused, unchanged)
# writes data/history/gap_pairs_results.json
```

## NEXT STEPS

- **Runs next (owner: orchestrator wiring session):** wire the staged `pair_rho`
  + `pair_rho_uncertainty` blocks from `staged_mlb_gap_pairs.md` VERBATIM;
  ensure `player_rbi` rung-keyed routing covers the new chained `rbi|spread`
  keys and `player_ks|player_rbi:opp:rN` facing keys; verify every key against
  `legtypes.pair_key`; run the bit-exact differential (untouched combos
  identical, every mover on the enumerated gap list) + the flat-baseline
  backtest; re-run the gap-ledger enumeration to confirm ZERO remaining
  flat/labeled OUTS/RBI/SB cells.
- **Owner (operator):** confirm the `:same`/`:opp` resolver routes pitcher×batter
  (outs/ks × batter) FACING vs same-team by comparing the batter's team prefix
  to the pitcher's; sign off on the `player_outs|player_rbi:opp` per-rbi-rung
  ladder and the chained `rbi|spread` grammar.
- **Decision owed:** none blocking measurement; NO family graded NO-QUOTE.
