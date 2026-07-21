# STAGED — MLB new prop families: OUTS / RBI / SB (NOT YET WIRED)

**Date:** 2026-07-21 · **Status: MEASUREMENT + STAGED VALUES ONLY — nothing in
`src/`, `config/`, or `tests/` was touched. Wiring is a separate reviewed step
(rule 8).** · Tool: `tools/calibrate_mlb_new_props.py` (NEW, additive; imports
the shipped copula via `tools/calibrate_mlb_player_props.py::implied_rho` and
the LIVE `combomaker.pricing.legtypes.pair_key` — never reimplemented) ·
Results artifact: `data/history/newprops_results.json`, per-year parse cache
`data/history/newprops_cache/*.pkl.gz` · Repro:
`.venv/Scripts/python.exe tools/calibrate_mlb_new_props.py report 2005 2025`.

Method mirrors `docs/calibration/results_baseball.md` and the 2026-07-09
measurement tranche EXACTLY: Rank 1 raw joint frequencies over Retrosheet;
Rank 2 bisection-inversion of the SHIPPED
`combomaker.pricing.copula.gaussian_copula_joint_prob` to a drop-in `pair_rho`;
99% CI = binomial SE on P(A∩B) through the monotone solver (`_Z99 = 2.576`);
OOS = era split **train 2005–2019 vs holdout 2020–2025** (the existing MLB
split); cluster-floor CIs (n = distinct team-games) on team-context batter
pairs; `:same`/`:opp` per shipped `sgp.py` semantics; same-player cross-family
= conditional cells (`conditionals_mlb.py` format), NEVER a rho.

## Data provenance + parse validation (the evidence)

Corpus: the 21 local Retrosheet event zips `data/history/20{05..25}eve.zip`
joined to official team game logs `gl2005..gl2025.txt` (49,492 games seen,
**49,490 joined**, 2 ties dropped, 0 unjoined). The prior tranche's parsed
artifacts (`mlb_parsed_*.csv.gz`, `parsed2.npz`) no longer exist in `data/` —
fresh parse, extended for: per-play out counting with full base-state tracking
(lineup-slot pinch-runner substitution, 2020+ `radj` ghost runners), bevent-
style RBI crediting, and SB attribution to the runner identity on the stolen
base. 1,033,950 batter-game rows / 98,894 starter-game rows (prior tranche:
1,033,852 — 0.01% apart, consistent).

**Reconciliation vs official game logs (per-game exact-match):**

| check | rate |
|---|---|
| team runs == official score | 49,448/49,490 = **99.92%** |
| team RBI == official GL RBI | 49,051/49,490 = **99.11%** (residual ±1, league bias ≈ +0.1%) |
| team SB == official GL SB | 49,489/49,490 = **100.00%** |
| team HR == official GL HR (parser + GL-index sanity) | 49,490/49,490 = **100.00%** |
| game outs == GL length-in-outs | 49,383/49,490 = **99.78%** |
| defensive outs == GL putouts (per team) | 49,383/49,490 = **99.78%** |
| half-innings with exactly 3 outs (non-final) | 784,916/785,010 = **99.99%** (94 bad) |
| SB events unattributable to a runner | **0** |

Per-stat measurements use only the games whose team totals reconcile for that
stat (`rbi_ok` 99.09% of batter rows, `sb_ok` 100.0%, `outs_ok` 99.78% of
starter rows) — selection effects at ≥99% retention are negligible. The
exact-containment sweeps run on the FULL corpus including unreconciled games.

**Spot checks vs published box scores (printed by the tool):**

- Scherzer `WAS201605110` (2016-05-11 20-K game): parsed **ks=20, outs=27** ✓ (9 IP CG).
- Rendon `WAS201704300` (2017-04-30): parsed **rbi=10, hr=3, hit=6** ✓.
- Elly De La Cruz `MIL202307080` (2023-07-08): parsed **SB=3** ✓ (2nd, 3rd and
  home — the steal-of-home game; located by the 3-SB scan, which also surfaced
  the real Giménez/Stott/Abrams 3-SB and Lane Thomas 4-SB July-2023 games).
- Season aggregates: Acuña 2023 **SB 73 / HR 41 / RBI 106** ✓✓✓, Judge 2022
  **HR 62 / RBI 131** ✓✓ — all exact.

**RBI-vs-runs reconciliation:** parsed team RBI matches the official GL to the
game in 99.11% of games; the RBI-suppression rules (no RBI on GDP,
baserunning-event runs, error-caused runs — Retrosheet signals the scorer's
error judgment via `(UR)`/`(TUR)` on error-aided advances, `(NR)` explicit)
were derived against the GL ground truth in both eras, not assumed.

## Population frame

Batter rows use the **all-PA batter-game frame** (any player with ≥1 completed
PA — the existing tranche's ml|prop anchor convention). Kalshi settles binary
only for **starters** (lineup) with ≥1 PA; the starters-frame variant was
measured to quantify the gap and it is priced into the bands:
`rbi|total:r1` +0.314 (all-PA) vs **+0.345 (starters)** — gap +0.031;
`ml|rbi:r1` +0.329 vs +0.329 — gap ≈0.001; `ml|sb` +0.151 vs +0.141 — gap
0.010. **1,273 SB events (≈2%) belong to 0-PA pinch-runners** — outside both
frames; Kalshi's starter-only settlement excludes them too, so the frame is
conservative-correct. Starter-pitcher rows use the fielding-position-1 starter,
self-season-median lines with ≥5 starts (the shipped KS convention).

---

## 1. OUTS family (starter-game unit; A = outs > own season-median unless noted)

| pair | n | P(A) | P(B) | P(A∩B) | rho | 99% CI | OOS 05-19→20-25 | verdict |
|---|---|---|---|---|---|---|---|---|
| **outs × KS over, SAME pitcher** | 57,047 | 0.477 | 0.471 | 0.319 | **+0.560** | [+0.533,+0.586] | +0.553→+0.578 (d +0.025, in-band) | **WIDEN-ONLY** (rung non-flat, below) |
| outs × KS over, OPPOSING starters | 54,484 | 0.479 | 0.488 | 0.241 | **+0.045** | [+0.015,+0.075] | +0.051→+0.028 | SHIP-grade (≈ ks\|ks +0.04) |
| **outs × GAME total over** | 66,215 | 0.480 | 0.502 | 0.151 | **−0.536** | [−0.555,−0.517] · cluster [−0.558,−0.514] | −0.564→**−0.452** (d +0.113) **FLAG** | **WIDEN-ONLY**, recent-era anchor −0.45 |
| **outs × own team WINS (:same)** | 72,627 | 0.479 | 0.495 | 0.309 | **+0.439** | [+0.414,+0.464] | +0.450→+0.406 (d −0.043) FLAG | SHIP w/ resolver (band covers drift) |
| outs × own ML (:opp), direct | 72,627 | — | — | — | **−0.439** | — | — | exact negation VERIFIED by direct measurement |
| outs × own team by 2+ (:same:r2) | 72,627 | 0.479 | 0.355 | 0.228 | +0.382 | [+0.357,+0.407] | d −0.038 | SHIP-grade |
| :same:r3 / :r4 / :r5 | 72,627 | — | — | — | +0.355 / +0.332 / +0.315 | ±0.026..0.032 | d −0.022/−0.009/−0.005 | SHIP-grade |
| outs × opp team by 2+ (:opp:r2) | 72,627 | 0.479 | 0.366 | 0.099 | −0.493 | [−0.510,−0.475] | d +0.063 FLAG | WIDEN (era-drift bands) |
| :opp:r3 / :r4 / :r5 | 72,627 | — | — | — | −0.519 / −0.542 / −0.552 | ±0.016..0.017 | d +0.076/+0.084/+0.087 FLAG | WIDEN (bands 0.08–0.10, ks\|spread:opp precedent) |
| outs × RFI | 72,627 | 0.479 | 0.515 | 0.209 | **−0.238** | [−0.262,−0.214] | d +0.013 | SHIP-grade (era-stable; ks\|rfi is −0.10) |

**Ladder structure — the OUTS convention question, ANSWERED and it is NOT
KS's answer.** KS was ladder-FLAT (one entry serves every rung). OUTS is
ladder-DRIFTING in both frames:

| frame | shallow → deep | values |
|---|---|---|
| absolute rungs × KS over | r12 → r15 → r18 → r21 | +0.610 / +0.532 / +0.435 / +0.365 (CIs disjoint) |
| self-relative × KS over | med−3 → med → med+3 → med+6 | +0.631 / +0.559 / +0.414 / +0.359 |
| absolute rungs × total | r12 → r21 | −0.428 / −0.458 / −0.454 / −0.444 (≈flat, range 0.03) |
| self-relative × total | med−3 → med → med+3 | −0.520 / −0.537 / −0.439 |

The Gaussian copula's strike-stability holds for outs×total (≈flat) but
**fails for same-pitcher outs×KS**: shallow outs lines are blow-up avoidance
(K-coupled, ρ≈+0.6), deep lines are pitch-count/efficiency survival
(K-decoupled, ρ≈+0.36). Conditional lift color: P(outs≥18 | K over) = 0.694 vs
0.549 base. → outs legs need **per-rung keys for the ks pair** (rung =
Kalshi ticker line integer of the OUTS leg, `:rN` = N+ outs); single un-runged
entries are only safe with the wide bands staged below. The era drift on
outs×total/±spread (weaker post-2020) is the openers/early-hook era — same
phenomenon the tranche saw on ks×total (−0.287→−0.228), handled the same way:
recent-era-leaning point + band.

## 2. RBI family (batter-game unit, all-PA frame, rbi-reconciled rows)

**Exact containment — verified on the FULL corpus (including unreconciled
games), pooled AND per-era, per the 77-exact-cell convention:**

| implication | n (A holds) | violations |
|---|---|---|
| **HR≥1 ⇒ RBI≥1** | **101,201** | **0 — EXACT** (a HR always credits the batter an RBI) |
| HR≥2 ⇒ RBI≥2 | 6,195 | 0 — EXACT |
| HR≥3 ⇒ RBI≥3 | 243 | 0 — EXACT |
| RBI≥1/2/3 ⇒ HRR≥1/2/3 | 279,981 / 97,680 / 32,459 | 0 / 0 / 0 — EXACT (arithmetic: HRR = H+R+RBI ≥ RBI) |
| SB≥1 ⇒ HIT≥1 | 51,323 | **8,826 violations (17.2%) — NOT a containment**, as scoring rules require (walk/HBP/E then steal) |

| pair | n | P(A) | P(B) | P(A∩B) | rho | 99% CI (naive · cluster-floor) | OOS | verdict |
|---|---|---|---|---|---|---|---|---|
| **RBI≥1 × game total over (:r1)** | 932,511 | 0.270 | 0.504 | 0.178 | **+0.314** | ±0.008 · [+0.291,+0.338] | +0.312→+0.322 (d +0.011) | SHIP-grade (era-stable) |
| RBI≥2 × total (:r2) | 932,511 | 0.095 | 0.504 | 0.073 | **+0.378** | ±0.011 · [+0.345,+0.413] | d +0.009 | SHIP-grade |
| RBI≥3 × total (:r3) | 932,511 | 0.032 | 0.504 | 0.027 | **+0.421** | ±0.020 · [+0.362,+0.487] | d +0.014 | SHIP-grade |
| **RBI≥1 × own team WINS (:same:r1)** | 1,024,568 | 0.270 | 0.490 | 0.177 | **+0.329** | ±0.007 · [+0.306,+0.352] | d +0.015 | SHIP w/ resolver |
| RBI≥2 × ML (:same:r2) | 1,024,568 | 0.094 | 0.490 | 0.070 | **+0.363** | ±0.010 · [+0.330,+0.396] | d +0.013 | SHIP w/ resolver |
| RBI≥3 × ML (:same:r3) | 1,024,568 | 0.031 | 0.490 | 0.026 | **+0.382** | ±0.017 · [+0.328,+0.440] | d +0.001 | SHIP w/ resolver |
| ML :opp (r1), direct | — | — | — | — | **−0.329** | — | — | exact negation VERIFIED |
| RBI≥1 × teammate HR≥1 (:same) | 9,846,764 pairs | 0.267 | 0.096 | 0.0269 | **+0.022** | cluster-floor [−0.001,+0.045] | +0.020→+0.028 | SHIP-grade small (matches [B]/[D] siblings) |
| RBI≥1 × opposing HR≥1 (:opp) | 10,772,314 pairs | 0.273 | 0.099 | 0.0267 | **−0.003** | cluster-floor [−0.036,+0.029] | d −0.005 | SHIP-grade ≈0 |

Rungs are **monotone** (+0.31→+0.42 total, +0.33→+0.38 ML) like HIT/HR — the
danger direction is a single 1+ entry understating deep rungs → per-rung
entries staged. Win-prob color: P(win | RBI≥1) = 0.653.

**Same-player conditional cells** (`conditionals_mlb.py` format,
`(famA, rungA, famB, rungB) → (P(B|A), n, marker)`, rbi/sb-reconciled rows,
era-stable ≤0.04 drift on every cell) — full staged block in §4. Highlights:
(hr,1,rbi,1) = **1.0 EXACT** n=100,369; (hr,1,rbi,2) = 0.556; (hr,2,rbi,2) =
1.0 EXACT; (rbi,1,hr,1) = 0.362; (rbi,1,hit,1) = 0.919 (NOT exact — 8.1% of
RBI games are hitless: sac flies, bases-loaded walks); (rbi,2,hit,1) = 0.994;
(rbi,k,hrr,k) = 1.0 EXACT.

## 3. SB family (batter-game unit, all-PA frame, sb-reconciled = 100.0%)

| pair | n | P(A) | P(B) | P(A∩B) | rho | 99% CI (naive · cluster) | OOS | verdict |
|---|---|---|---|---|---|---|---|---|
| SB≥1 × HIT≥1 (SAME player) | 1,033,932 | 0.0496 | 0.569 | 0.0411 | **+0.350** | [+0.334,+0.366] | +0.361→+0.322 (d −0.038) FLAG | same-player ⇒ **conditional cells**, not a rho (§4) |
| SB≥1 × game total over | 940,970 | 0.0496 | 0.506 | 0.0260 | **+0.022** | ±0.010 · [−0.010,+0.054] | d +0.012 | **WIDEN-ONLY ≈0** (cluster CI spans 0) |
| SB≥1 × own team WINS (:same) | 1,033,932 | 0.0496 | 0.490 | 0.0305 | **+0.151** | ±0.011 · [+0.116,+0.186] | +0.145→+0.166 (d +0.021) | **WIDEN-ONLY** (prior DIVERGED, see §5) |
| SB≥1 × teammate SB≥1 (:same) | 9,939,344 pairs | 0.0486 | 0.0486 | 0.0035 | **+0.096** | cluster-floor [+0.057,+0.132] | +0.088→+0.113 | WIDEN-ONLY |

Base rate P(SB≥1) = 4.96% of all-PA batter-games (5.6% starters frame) — as
predicted, low; the giant n keeps naive CIs tight but per-game clustering and
era wobble keep every SB verdict WIDEN-ONLY. Conditional color:
P(win | SB≥1) = **0.614**; (hit,1,sb,1) = 0.0723 vs 0.0496 base (+46% lift).

---

## 4. STAGED entries — **NOT YET WIRED** (promotion = separate reviewed step, rule 8)

Every base key below was generated by **executing the live
`legtypes.pair_key`** (tool prints them; note the sort traps — the directive's
labels `player_outs|player_ks`, `player_rbi|player_hr`, `player_sb|player_hit`
sort to `player_ks|player_outs`, `player_hr|player_rbi`,
`player_hit|player_sb`). Staged LegType string values follow the sibling
convention: `player_outs`, `player_rbi`, `player_sb` (families KXMLBOUTS /
KXMLBRBI / KXMLBSB, `staged_mlb_props.md` naming). Rung grammar: `:rN` = Kalshi
ticker line integer, N+ (`floor_strike` N−0.5); when both legs are rung-keyed
the suffixes chain in pair_key leg order. **RBI joins the rung-keyed families
(1+/2+/3+); OUTS needs rung keys for the ks pair (ladder NOT flat — new
finding); SB is 1+-only.**

```python
# ============ pair_rho_by_sport["mlb"] — STAGED, NOT WIRED ============
# ---- OUTS (KXMLBOUTS). Self-median-line frame values; outs x ks ladder
# ---- is NOT flat -> per-rung keys for the ks pair, rung = OUTS leg line.
"player_ks|player_outs:same": 0.56,        # SAME PITCHER (headline). WIDEN-ONLY
                                           # rung-dependent: r12 +0.61 / r15 +0.53
                                           # / r18 +0.44 / r21 +0.36 (see per-rung)
"player_ks|player_outs:same:r12": 0.61,
"player_ks|player_outs:same:r15": 0.53,
"player_ks|player_outs:same:r18": 0.44,
"player_ks|player_outs:same:r21": 0.36,    # NO interpolation/extrapolation, ever
"player_ks|player_outs:opp": 0.045,        # opposing starters (= ks|ks scale)
"player_ks|player_outs": 0.30,             # plain parse-failure fallback: spans
                                           # both orientations (+0.045..+0.61)
"player_outs|total": -0.50,                # orientation-free. Pooled −0.536,
                                           # holdout −0.452, recent(2021-25) −0.455:
                                           # era-drift FLAG, recent-leaning point
"moneyline|player_outs:same": 0.43,        # own team wins; negation VERIFIED
"moneyline|player_outs:opp": -0.43,
"moneyline|player_outs": 0.00,             # plain fail-closed, sign-spanning band
"player_outs|spread:same:r2": 0.38,
"player_outs|spread:same:r3": 0.36,
"player_outs|spread:same:r4": 0.33,
"player_outs|spread:same:r5": 0.32,
"player_outs|spread:opp:r2": -0.49,
"player_outs|spread:opp:r3": -0.52,
"player_outs|spread:opp:r4": -0.54,        # era-drift-widened bands (ks precedent)
"player_outs|spread:opp:r5": -0.55,
"player_outs|spread:same": 0.35,           # un-runged oriented fallbacks
"player_outs|spread:opp": -0.52,
"player_outs|spread": 0.00,                # plain fail-closed
"player_outs|rfi": -0.24,                  # orientation-free, era-stable

# ---- RBI (KXMLBRBI). Rung-keyed 1+/2+/3+, rung-monotone like HIT/HR.
"player_rbi|total:r1": 0.31,
"player_rbi|total:r2": 0.38,
"player_rbi|total:r3": 0.42,
"player_rbi|total": 0.31,                  # plain un-runged fallback over the ladder
"moneyline|player_rbi:same:r1": 0.33,
"moneyline|player_rbi:same:r2": 0.36,
"moneyline|player_rbi:same:r3": 0.38,
"moneyline|player_rbi:opp:r1": -0.33,      # exact negation, verified directly
"moneyline|player_rbi:opp:r2": -0.36,
"moneyline|player_rbi:opp:r3": -0.38,
"moneyline|player_rbi:same": 0.33,         # un-runged oriented fallbacks
"moneyline|player_rbi:opp": -0.33,
"moneyline|player_rbi": 0.00,              # plain fail-closed, spans ±0.38
"player_hr|player_rbi:same": 0.02,         # TEAMMATE (distinct players)
"player_hr|player_rbi:opp": 0.00,          # opponent ≈0 (matches [B]/[D] siblings)
"player_hr|player_rbi": 0.01,              # unrouted fallback
# SAME-PLAYER hr x rbi is CONTAINMENT/conditional (cells below) — must route
# BEFORE any of these rho keys, exactly like hit/hr/tb/hrr do today.

# ---- SB (KXMLBSB). 1+-only; ALL WIDEN-ONLY.
"player_sb|total": 0.02,                   # ≈0 as predicted (cluster CI spans 0)
"moneyline|player_sb:same": 0.15,          # DIVERGED from ≈0 prior — see §5
"moneyline|player_sb:opp": -0.15,
"moneyline|player_sb": 0.00,               # plain fail-closed
"player_sb|player_sb:same": 0.10,          # teammate (running-team common factor)
"player_sb|player_sb": 0.05,               # unrouted; :opp UNMEASURED — band spans

# ---- LABELED PRIORS (unmeasured but reachable once classified; bounded by
# ---- measured neighbors; MEASURE-BEFORE-TIGHTEN — else they'd price +0.60):
"player_hit|player_sb:same": 0.05,         # distinct-player teammate (≤ sb|sb 0.10)
"player_hit|player_sb:opp": 0.00,
"player_hit|player_sb": 0.03,
"player_rbi|player_sb": 0.03,              # distinct-player; same-player via cells
"player_ks|player_rbi:opp": -0.12,         # FACING, bounded by hit|ks −0.126 /
"player_ks|player_rbi:same": 0.01,         #   hrr|ks −0.19
"player_ks|player_rbi": 0.00,
"player_hit|player_rbi:same": 0.06,        # distinct-player, [D]-sibling bounds
"player_hit|player_rbi:opp": 0.00,
"player_hit|player_rbi": 0.03,
"player_rbi|player_tb:same": 0.08,
"player_rbi|player_tb:opp": 0.00,
"player_rbi|player_tb": 0.04,
"player_hrr|player_rbi:same": 0.10,        # distinct-player (hrr|hrr teammate 0.17 cap)
"player_hrr|player_rbi:opp": 0.00,
"player_hrr|player_rbi": 0.05,

# ============ pair_rho_uncertainty — STAGED bands ("mlb:"+key) ============
"mlb:player_ks|player_outs:same": 0.15,    # covers the self-relative ladder ±3
"mlb:player_ks|player_outs:same:r12": 0.10,  # CI99 hw 0.09 at extreme marginal
"mlb:player_ks|player_outs:same:r15": 0.06,
"mlb:player_ks|player_outs:same:r18": 0.05,
"mlb:player_ks|player_outs:same:r21": 0.05,
"mlb:player_ks|player_outs:opp": 0.05,
"mlb:player_ks|player_outs": 0.35,         # spans +0.045..+0.61 around 0.30
"mlb:player_outs|total": 0.12,             # covers pooled −0.536 & recent −0.45
"mlb:moneyline|player_outs:same": 0.08,    # covers era +0.450→+0.406
"mlb:moneyline|player_outs:opp": 0.08,
"mlb:moneyline|player_outs": 0.50,         # sign-spanning ±0.44
"mlb:player_outs|spread:same:r2": 0.06,
"mlb:player_outs|spread:same:r3": 0.06,
"mlb:player_outs|spread:same:r4": 0.06,
"mlb:player_outs|spread:same:r5": 0.06,
"mlb:player_outs|spread:opp:r2": 0.08,
"mlb:player_outs|spread:opp:r3": 0.09,     # era d +0.076
"mlb:player_outs|spread:opp:r4": 0.10,     # era d +0.084
"mlb:player_outs|spread:opp:r5": 0.10,     # era d +0.087
"mlb:player_outs|spread:same": 0.09,       # spans r2..r5
"mlb:player_outs|spread:opp": 0.11,
"mlb:player_outs|spread": 0.60,            # plain must span ±0.55
"mlb:player_outs|rfi": 0.06,
"mlb:player_rbi|total:r1": 0.07,           # cluster hw 0.023 + starters-frame gap 0.031
"mlb:player_rbi|total:r2": 0.07,
"mlb:player_rbi|total:r3": 0.09,           # cluster hw 0.063
"mlb:player_rbi|total": 0.13,              # plain spans r1..r3 + frame
"mlb:moneyline|player_rbi:same:r1": 0.06,
"mlb:moneyline|player_rbi:same:r2": 0.06,
"mlb:moneyline|player_rbi:same:r3": 0.07,
"mlb:moneyline|player_rbi:opp:r1": 0.06,
"mlb:moneyline|player_rbi:opp:r2": 0.06,
"mlb:moneyline|player_rbi:opp:r3": 0.07,
"mlb:moneyline|player_rbi:same": 0.09,
"mlb:moneyline|player_rbi:opp": 0.09,
"mlb:moneyline|player_rbi": 0.40,          # sign-spanning ±0.38
"mlb:player_hr|player_rbi:same": 0.05,
"mlb:player_hr|player_rbi:opp": 0.04,
"mlb:player_hr|player_rbi": 0.05,
"mlb:player_sb|total": 0.06,
"mlb:moneyline|player_sb:same": 0.07,      # cluster hw 0.035 + era d 0.021
"mlb:moneyline|player_sb:opp": 0.07,
"mlb:moneyline|player_sb": 0.25,
"mlb:player_sb|player_sb:same": 0.06,
"mlb:player_sb|player_sb": 0.11,           # spans teammate 0.10 & unmeasured :opp 0
"mlb:player_hit|player_sb:same": 0.08,     # labeled priors: wide
"mlb:player_hit|player_sb:opp": 0.05,
"mlb:player_hit|player_sb": 0.08,
"mlb:player_rbi|player_sb": 0.08,
"mlb:player_ks|player_rbi:opp": 0.08,
"mlb:player_ks|player_rbi:same": 0.05,
"mlb:player_ks|player_rbi": 0.20,
"mlb:player_hit|player_rbi:same": 0.08,
"mlb:player_hit|player_rbi:opp": 0.05,
"mlb:player_hit|player_rbi": 0.08,
"mlb:player_rbi|player_tb:same": 0.08,
"mlb:player_rbi|player_tb:opp": 0.05,
"mlb:player_rbi|player_tb": 0.08,
"mlb:player_hrr|player_rbi:same": 0.08,
"mlb:player_hrr|player_rbi:opp": 0.05,
"mlb:player_hrr|player_rbi": 0.10,
```

```python
# ======== conditionals_mlb.SAME_PLAYER_CONDITIONALS — STAGED cells ========
# (famA, rungA, famB, rungB) -> (P(B|A), n, marker); rbi/sb-reconciled rows,
# 2005-25; 'exact' verified ==1.0 pooled AND on the era split (and the hr=>rbi
# implications verified 0 violations on the FULL corpus incl. unreconciled:
# n=101,201 / 6,195 / 243).
('hr', 1, 'rbi', 1): (1.0, 100369, 'exact'),
('hr', 1, 'rbi', 2): (0.555630, 100369, 'measured'),
('hr', 1, 'rbi', 3): (0.243960, 100369, 'measured'),
('hr', 2, 'rbi', 1): (1.0, 6144, 'exact'),
('hr', 2, 'rbi', 2): (1.0, 6144, 'exact'),
('hr', 2, 'rbi', 3): (0.721029, 6144, 'measured'),
('rbi', 1, 'hr', 1): (0.362310, 277025, 'measured'),
('rbi', 1, 'hr', 2): (0.022179, 277025, 'measured'),
('rbi', 2, 'hr', 1): (0.577470, 96573, 'measured'),
('rbi', 2, 'hr', 2): (0.063620, 96573, 'measured'),
('rbi', 3, 'hr', 1): (0.763089, 32088, 'measured'),
('rbi', 3, 'hr', 2): (0.138058, 32088, 'measured'),
('rbi', 1, 'hit', 1): (0.918621, 277025, 'measured'),
('rbi', 1, 'hit', 2): (0.448959, 277025, 'measured'),
('rbi', 1, 'hit', 3): (0.123350, 277025, 'measured'),
('rbi', 2, 'hit', 1): (0.993601, 96573, 'measured'),
('rbi', 2, 'hit', 2): (0.609539, 96573, 'measured'),
('rbi', 2, 'hit', 3): (0.203670, 96573, 'measured'),
('rbi', 3, 'hit', 1): (0.999844, 32088, 'measured'),
('rbi', 3, 'hit', 2): (0.735103, 32088, 'measured'),
('rbi', 3, 'hit', 3): (0.296933, 32088, 'measured'),
('hit', 1, 'rbi', 1): (0.436912, 582454, 'measured'),
('hit', 1, 'rbi', 2): (0.164743, 582454, 'measured'),
('hit', 1, 'rbi', 3): (0.055082, 582454, 'measured'),
('hit', 2, 'rbi', 1): (0.591289, 210342, 'measured'),
('hit', 2, 'rbi', 2): (0.279854, 210342, 'measured'),
('hit', 2, 'rbi', 3): (0.112141, 210342, 'measured'),
('hit', 3, 'rbi', 1): (0.714262, 47841, 'measured'),
('hit', 3, 'rbi', 2): (0.411133, 47841, 'measured'),
('hit', 3, 'rbi', 3): (0.199160, 47841, 'measured'),
('rbi', 1, 'tb', 2): (0.720545, 277025, 'measured'),
('rbi', 1, 'tb', 3): (0.536461, 277025, 'measured'),
('rbi', 1, 'tb', 4): (0.432499, 277025, 'measured'),
('rbi', 2, 'tb', 2): (0.908391, 96573, 'measured'),
('rbi', 2, 'tb', 3): (0.769884, 96573, 'measured'),
('rbi', 2, 'tb', 4): (0.668665, 96573, 'measured'),
('rbi', 3, 'tb', 2): (0.987004, 32088, 'measured'),
('rbi', 3, 'tb', 3): (0.923772, 32088, 'measured'),
('rbi', 3, 'tb', 4): (0.853933, 32088, 'measured'),
('rbi', 1, 'hrr', 1): (1.0, 277025, 'exact'),   # HRR = H+R+RBI >= RBI, arithmetic
('rbi', 1, 'hrr', 2): (0.932724, 277025, 'measured'),
('rbi', 1, 'hrr', 3): (0.761971, 277025, 'measured'),
('rbi', 1, 'hrr', 4): (0.509127, 277025, 'measured'),
('rbi', 1, 'hrr', 5): (0.308669, 277025, 'measured'),
('rbi', 2, 'hrr', 2): (1.0, 96573, 'exact'),
('rbi', 2, 'hrr', 3): (0.994936, 96573, 'measured'),
('rbi', 2, 'hrr', 4): (0.896358, 96573, 'measured'),
('rbi', 2, 'hrr', 5): (0.652294, 96573, 'measured'),
('rbi', 3, 'hrr', 2): (1.0, 32088, 'exact'),
('rbi', 3, 'hrr', 3): (1.0, 32088, 'exact'),
('rbi', 3, 'hrr', 4): (0.999875, 32088, 'measured'),
('rbi', 3, 'hrr', 5): (0.968057, 32088, 'measured'),
('hrr', 2, 'rbi', 1): (0.596136, 433438, 'measured'),
('hrr', 2, 'rbi', 2): (0.222807, 433438, 'measured'),
('hrr', 2, 'rbi', 3): (0.074031, 433438, 'measured'),
('hrr', 3, 'rbi', 1): (0.771056, 273761, 'measured'),
('hrr', 3, 'rbi', 2): (0.350978, 273761, 'measured'),
('hrr', 3, 'rbi', 3): (0.117212, 273761, 'measured'),
('hrr', 4, 'rbi', 1): (0.871953, 161753, 'measured'),
('hrr', 4, 'rbi', 2): (0.535162, 161753, 'measured'),
('hrr', 4, 'rbi', 3): (0.198352, 161753, 'measured'),
('hrr', 5, 'rbi', 1): (0.939887, 90978, 'measured'),
('hrr', 5, 'rbi', 2): (0.692409, 90978, 'measured'),
('hrr', 5, 'rbi', 3): (0.341434, 90978, 'measured'),
('sb', 1, 'hit', 1): (0.828043, 51321, 'measured'),  # NOT exact: 17.2% hitless SB games
('sb', 1, 'hit', 2): (0.417665, 51321, 'measured'),
('sb', 1, 'hit', 3): (0.121744, 51321, 'measured'),
('hit', 1, 'sb', 1): (0.072269, 588028, 'measured'),
('hit', 2, 'sb', 1): (0.100861, 212521, 'measured'),
('hit', 3, 'sb', 1): (0.129147, 48379, 'measured'),
```

**Wiring seams the reviewed step must handle (documented, NOT done here):**

1. **Classification**: LegTypes `PLAYER_OUTS/PLAYER_RBI/PLAYER_SB` +
   MLB-anchored keywords (`MLBOUTS`/`MLBRBI`/`MLBSB`) — these were deliberately
   NOT staged in 2026-07-09 ("RBI/SB/OUTS stay UNKNOWN") because they weren't
   combo-eligible then; a fresh universe collision scan is required (the bare
   "SB"/"RBI" substring traps), and classification + table entries must ship
   TOGETHER as always.
2. **Same-pitcher ks×outs routing**: `sgp._mlb_prop_pair_prior` refuses
   identical player segments (`seg_a == seg_b → None`) and falls back to the
   PLAIN key — for pitcher-stat pairs same-team IS same-player and the pair is
   a genuine copula rho (NOT a containment), so the resolver must learn to
   route identical-segment *pitcher* pairs to `:same` (else the plain 0.30
   fallback prices the dominant traded shape ~0.26 too low).
3. **RBI/SB same-player cells**: extend `SAME_PLAYER_CONDITIONALS` + the
   relationships.py/sgp family maps with fams `'rbi'`/`'sb'`; until then
   same-player RBI×HR/HIT/TB/HRR pairs decline UNKNOWN (fail-closed by
   construction — safe, just no-quote). The (hr,k,rbi,k) EXACT cells drive
   containment/impossible verdicts exactly like HR⇒HIT/TB/HRR today.
4. **OUTS rung grammar**: add `player_outs` (and `player_rbi`) to the
   rung-keyed families; NO rung interpolation/extrapolation, ever (tb×ks
   precedent).
5. Rule-8b gate: tape-replay backtest of the staged table vs the
   flat-UNKNOWN baseline before promote + parity check.

## 5. Sign-check vs the pre-registered priors

| prior (stated before measurement) | measured | grade |
|---|---|---|
| OUTS strong + with same-pitcher KS | +0.56 (self-med), +0.36..+0.61 by rung | **MATCHED** |
| OUTS + with own-team ML | +0.44 (> KS's +0.24 — outs embed "kept the lead, stayed in") | **MATCHED** |
| OUTS − with game TOTAL | −0.54 (pooled; −0.45 recent) — 2x stronger than ks\|total −0.25 | **MATCHED** |
| OUTS ladder flat like KS (9..21) | **NOT flat vs KS** (+0.61→+0.36, CIs disjoint; ≈flat vs total) | **DIVERGED — investigated**: strike-stability fails because shallow outs lines price blow-up avoidance (K-coupled) while deep lines price pitch-count survival (K-decoupled); per-rung keys staged; the same split shows up within-pitcher (self-relative ladder), so it is not a pooling artifact |
| OUTS − with opposing batter props | not in the decision set — unmeasured, stays fail-closed | n/a (queued) |
| RBI strong + same-player HR, HR1+⇒RBI1+ == 1.0 | (hr,1,rbi,1)=1.0, **0 violations / 101,201 full-corpus** (and 2+/3+ exact) | **MATCHED — EXACT** |
| RBI + same-player HIT/TB/HRR | cells 0.92/0.72/0.93 at r1 (RBI⇒HRR arithmetic-exact) | **MATCHED** (note: rbi⇒hit is NOT exact — 8.1% hitless-RBI) |
| RBI + with total | +0.31/+0.38/+0.42 rung-monotone, era-stable | **MATCHED** |
| RBI mild + with own ML | **+0.33..+0.38 — NOT mild** (above HR's +0.23, just under hrr's +0.37) | sign MATCHED, magnitude above prior: RBI *is* run creation, it loads on winning almost as hard as HRR |
| RBI teammate/opposing frames like HR's | teammate +0.022 / opposing −0.003 (hr\|hr is +0.04/+0.02) | **MATCHED** |
| SB weak everywhere | total +0.02≈0; teammate +0.10 | **MATCHED** |
| SB slight + with same-player HIT, NOT a containment | rho +0.35, (hit,1,sb,1) lift +46%; containment REFUTED (17.2% hitless) | sign MATCHED; magnitude above "slight" — low-base-rate copula geometry inflates the rho metric; the conditional cell is the safe representation |
| SB ≈0 with ML | **+0.15 (P(win\|SB)=0.614) — DIVERGED, investigated**: reaching base is a prerequisite (on-base ⊂ scoring chances ⊂ winning), steals are attempted selectively in winnable/close states, and running teams are better teams — channels the live SB marginal does NOT fully absorb; shipped WIDEN-ONLY with band 0.07 | DIVERGED (+) |
| SB wide CIs → WIDEN-ONLY | cluster CIs 3–4x naive; all SB verdicts WIDEN-ONLY | **MATCHED** |

Cross-checks vs siblings: outs×ks-opposing (+0.045) ≈ shipped ks|ks (+0.04);
outs|rfi (−0.24) vs ks|rfi (−0.10) — deeper-start channel stronger, same sign;
rbi|total (+0.31) sits between hit|total (+0.25) and hrr|total (+0.40) exactly
as a run-credit stat should; rbi ML-pairs between hit (±0.23) and hrr (±0.37).
Ordering coherent, no frame/label anomalies.

## 6. OOS-drift flags (holdout outside pooled CI99)

Flagged and handled via bands / recent-era anchoring (never refit on P&L —
these are era-structure observations): `player_outs|total` d +0.113 (openers /
early-hook era — the ks|total precedent, recent anchor −0.45 staged);
`player_outs|spread:opp:r2..r5` d +0.06..+0.09 (bands 0.08–0.10);
outs-rung×total ladder d +0.12..+0.15 (same phenomenon, absolute-rung frame);
`moneyline|player_outs` d −0.043 (band 0.08); `sb|hit` d −0.038 (cells carry
era spread); RBI pairs d ≤ +0.015 (flags fire only because CIs are ±0.007 —
inside every staged band). Clustering caveat: batter×team-context rows share
team-games; cluster-floor CIs reported and bands sized off them.

## NEXT STEPS

- **Runs next (owner: next engineering session):** (1) rule-8b tape-replay
  backtest of this staged table (extend `tools/backtest_mlb_pairs.py` to the
  three families) against the flat-UNKNOWN baseline; (2) wiring step per §4
  seams — LegTypes+keywords (collision scan) + pair table + bands + conditional
  cells + the ks×outs same-player routing + OUTS/RBI rung grammar, port +
  parity-check, classification and table TOGETHER; (3) measure the queued
  leftovers: outs × opposing-batter props, distinct-player hit/tb/hrr×rbi and
  hit×sb (labeled priors staged wide), sb|sb:opp.
- **Owner (operator):** confirm the OUTS convention decision — per-rung keys
  for ks×outs (staged) vs single wide-band entry; confirm the KXMLBOUTS ticker
  line grammar (rung = outs integer, N+ assumed) against the live series
  rules before wiring (`feedback_kalshi_docs_first`); sign off on the
  ml|sb +0.15 divergence note.
- **Decision owed:** none blocking measurement; NO family graded NO-QUOTE —
  parse reconciliation cleared the bar everywhere (RBI 99.1%, SB 100%, outs
  99.8%, HR 100%).
