# Adversarial Semantics Judge — MLB new props (OUTS / RBI / SB) measurement re-derivation

**Date:** 2026-07-22 · **Judge stance:** REFUTE-by-default; independently
re-derive from the raw Retrosheet population before trusting any staged/wired
value. · **Scope (blast radius):** READ-ONLY audit of the joint/correlation
layer for the three newly-wired MLB prop families. No `src/`, config, or test
file modified. This report touches only combo-pricing correlation inputs; it
does not affect monitoring, P&L, settlement, or pricing throughput.

**What "independent" means here:** I did NOT read values off
`staged_mlb_new_props.md` and nod, and I did NOT trust the tool's own
`newprops_results.json`. I wrote a from-scratch measurement script
(`scratchpad/judge_measure.py`) that:
- reads ONLY the parsed population caches (`data/history/newprops_cache/*.pkl.gz`),
- recomputes every P(A), P(B), P(A∩B) with its own counting, and
- inverts the Gaussian copula with an **independent tetrachoric solver built on
  `scipy.stats.multivariate_normal`** (BVN survival + bisection) — NOT the
  tool's `implied_rho`. Solver self-test: recovered a synthetic ρ=0.5 to 1e-5.

If the tool's counting or its copula inversion carried a bug, my orthogonal
path would disagree. It does not. I additionally re-parsed raw event files for
the safety-critical containment (orthogonal to the tool's full parser).

Population loaded independently: batter_games=1,033,950 · starter_games=98,894 ·
game_rows=49,490 (matches the staged provenance).

---

## Attack results

| # | Attack | Wired | My independent number | Verdict |
|---|--------|-------|------------------------|---------|
| 1a | outs×KS same-pitcher (self-med) | +0.56 | **+0.5596** (n=57,047, P(A)=0.4766 P(B)=0.4709 P(AB)=0.3186) | **CONFIRMED** |
| 1a | …ladder r12 / r15 / r18 / r21 | 0.61/0.53/0.44/0.36 | **+0.6095 / +0.5323 / +0.4350 / +0.3648** | **CONFIRMED** (non-flat, monotone) |
| 1b | outs×game-total (pooled) | −0.50 pt (recent-anchored) | **−0.5364** pooled, **−0.4517** holdout, **−0.4543** recent 2021-25 | **CONFIRMED** |
| 1c | rbi×total ladder r1/r2/r3 | 0.31/0.38/0.42 | **+0.3144 / +0.3783 / +0.4213** | **CONFIRMED** (rung-monotone) |
| 1d | HR⇒RBI exact containment | ==1.0, 0 viol | **0 violations** on full corpus: HR≥1 n=101,201·0 / HR≥2 n=6,195·0 / HR≥3 n=243·0. Raw-event recheck: 4,164/4,164 HR plays credit the batter ≥1 RBI, 0 NR flags | **CONFIRMED — EXACT** |
| 2 | ml×sb :same +0.15 divergence real vs artifact | +0.15 | **+0.1508**; P(win\|SB≥1)=**0.614** vs base 0.490 (**+12.4pp**, copula-free lift); SB win-lift (+0.124) is **2× HIT's** (+0.061) → extra channel real, not on-base geometry | **CONFIRMED** (real +div, not artifact/label bug) |
| 3 | :opp faithfulness (measured/exact-complement, not hand-negated) | see below | ml:opp = exact 2-way complement, measured direct (−same to 4dp). **spread:opp genuinely asymmetric** (opp≠−same by 0.11→0.24, gap grows with line) — wired to the DIRECT measurement, not the negation | **CONFIRMED** |
| 4 | Band adequacy (era shift ≤ band) | — | outs\|total d=0.085<0.12 ✓; rbi\|total:r1 d=0.011<0.07 ✓; ml\|sb:same d=0.021<0.07 ✓; outs\|spread:opp:r5 d=0.087<0.10 ✓ | **CONFIRMED** |

Every conditional cell wired into `conditionals_mlb.py` was independently
recomputed and matched to 6 decimals (spot set: (rbi,1,hit,1)=0.918621,
(rbi,1,hr,1)=0.362310, (rbi,2,hit,1)=0.993601, (rbi,1,tb,2)=0.720545,
(hr,1,rbi,2)=0.555630, (sb,1,hit,1)=0.828043, (hit,1,sb,1)=0.072269,
(rbi,1,hrr,1)=1.0, (hr,2,rbi,2)=1.0).

---

## Detail per attack

### ATTACK 1 — spot cells re-derived from the corpus
All four spot cells reproduce to ≤0.0001 on my independent solver. The
sign AND magnitude hold on every one.

- **1a outs×KS (+0.56):** non-flat ladder confirmed and strongly monotone
  (+0.61 → +0.36, a 0.25 spread). CI99 disjointness: r15/r18 and r18/r21 are
  cleanly disjoint. The r12/r15 adjacent pair *overlaps* — but only because
  r12's P(A)=0.90 pushes the marginal to an extreme and inflates its SE
  (CI99 [+0.526,+0.708]); the point estimates and the overall trend
  (r12 vs r18, r12 vs r21) are unambiguous. **Minor doc nit:** the staged doc's
  blanket "CIs disjoint" over-states the r12/r15 adjacent pair. **No wiring
  risk** — each rung is wired VERBATIM at its measured point with its own band
  (0.10/0.06/0.05/0.05) and NO interpolation, so the wide r12 CI is priced.
- **1b outs×total (−0.50):** pooled −0.536, recent-era −0.45; the −0.50
  recent-leaning anchor with band 0.12 covers both. Era drift is the
  openers/early-hook era, correctly characterized.
- **1c rbi×total (+0.31→+0.42):** rung-monotone, era-stable (|d|=0.011).
- **1d HR⇒RBI (==1.0):** verified two independent ways. (i) 0 violations on the
  full 101,201/6,195/243-game sweep. (ii) Orthogonal raw-event re-parse: every
  one of 4,164 sampled 2023 HR plays credits the batter ≥1 RBI with no NR flag
  — the arithmetic reason (a HR always scores its own batter) is structural.
  Airtight; the (hr,k,rbi,k) EXACT conditional cells are safe to drive
  containment verdicts.

### ATTACK 2 — ml×sb +0.15 divergence: REAL, not an artifact/label bug
- Copula-free lift metric P(win|SB≥1)=0.614 vs base 0.490 = **+12.4pp**
  (ratio 1.25) — this is a raw conditional frequency, immune to any
  low-base-rate copula-geometry inflation. A geometry artifact would show
  ratio≈1.0; it does not.
- SB's win-lift (+0.124) is **double** HIT's (+0.061). If SB only lifted
  win-prob through "reached base," it would track the on-base baseline (HIT).
  It exceeds it 2×, confirming the doc's channels (selective attempts in
  winnable/close states + running teams are better teams) that the live SB
  marginal cannot absorb.
- **Label/frame integrity verified:** 0 rows where `(team_runs>opp_runs) != won`
  (no home/away frame inversion); P(win|RBI≥1)=0.653 reproduces the doc exactly;
  P(win|HR≥1)=0.649 is coherent. The +0.15 is a genuine positive divergence,
  correctly shipped WIDEN-ONLY (band 0.07, covering era +0.145→+0.166).

### ATTACK 3 — :opp faithfulness
- **Moneyline :opp (outs/rbi/sb):** the game is win-XOR-lose in this population
  (ties dropped), so "opp wins" is the exact logical complement of "own wins."
  The tool measures `not won` **directly**, and it equals −ρ(:same) to 4dp by
  copula antisymmetry. This is the legitimate exact-2-way-complement case the
  playbook §C permits ("both measured directly"), NOT a hand-negation.
- **Spread :opp (outs×spread:opp:rN):** the decisive check. `:same` (margin≥n)
  and `:opp` (margin≤−n) are **genuinely asymmetric** — they diverge by
  0.11 (r2) → 0.24 (r5), and the gap **grows with the line** (the exact trap
  B2.6 signature). The config wires the DIRECT measurements
  (−0.49/−0.52/−0.54/−0.55), NOT the negation of :same (which would be
  −0.38/−0.36/−0.33/−0.32). The negation trap was avoided.
- `player_sb|player_sb:opp` is honestly staged UNMEASURED with a sign-spanning
  band (0.11) — no fake value.

### ATTACK 4 — band adequacy
Every spot-checked band ≥ max(0.04, era |shift|): outs|total (d 0.085 < 0.12),
rbi|total:r1 (0.011 < 0.07), ml|sb:same (0.021 < 0.07), outs|spread:opp:r5
(0.087 < 0.10). No band found too tight for its measured era drift.

---

## Wiring fidelity (VERBATIM check)
`src/combomaker/ops/config.py` `pair_rho_by_sport["mlb"]` and
`pair_rho_uncertainty`, and `conditionals_mlb.SAME_PLAYER_CONDITIONALS`, match
the staged doc's numbers exactly. No transcription drift found.

## Defects found
- **None blocking.** One documentation-precision nit (ATTACK 1a): the staged
  doc claims the whole outs×KS ladder has "CIs disjoint"; the r12/r15 adjacent
  pair actually overlaps due to r12's extreme marginal. This does not affect
  any wired value (each rung wired at its own measured point + band, no
  interpolation) and does not change the non-flat conclusion. Recommend a
  one-line doc correction; not a wiring fix.

## NEXT STEPS
- **Runs next (owner: next engineering session):** proceed to the rule-8b
  tape-replay backtest of the staged/wired table vs the flat-UNKNOWN baseline
  (Stage 7 gate) — the measurement layer is judge-cleared; nothing here blocks
  it. Verify the ks×outs same-player routing (§4 seam #2) and OUTS/RBI rung
  grammar are exercised by the backtest, since those are engine-routing risks
  this measurement judge does not cover.
- **Owner (operator):** optional one-line doc fix to the "CIs disjoint" phrasing
  on the outs×KS ladder (cosmetic). Sign-off on the ml×sb +0.15 divergence note
  stands — independently reconfirmed as a real signal.
- **Decision owed:** none from this audit. No family graded NO-QUOTE; no
  sign/frame/magnitude error found.

---

## VERDICT: **SHIP** — all four attacks CONFIRMED; no sign/frame/magnitude defect; only a cosmetic doc-precision nit on ladder-CI wording.
