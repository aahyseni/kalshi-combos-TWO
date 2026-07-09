# Correlation Calibration — BASEBALL (MLB player props)

Agent domain: MLB batter home-run and starting-pitcher-strikeout SGP pairs
(ranks 1+2). Calibrates ONLY the joint/correlation layer — marginals come live
from Kalshi leg books at quote time. Method mirrors
`tools/calibrate_pairs_from_history.py`: over N historical (batter|pitcher)-games
measure P(A), P(B), P(A∩B), then invert the SAME Gaussian copula the pricer runs
(`combomaker.pricing.copula.gaussian_copula_joint_prob`) by bisection to a
drop-in `pair_rho`. 99% CI = binomial SE on P(A∩B) pushed through the monotone
solver (`_Z99 = 2.576`).

Script: `tools/calibrate_mlb_player_props.py` (NEW, additive — imports the shipped
copula, touches no `src/`/`config/`). Run with the project venv:
`.venv/Scripts/python.exe tools/calibrate_mlb_player_props.py 2015 … 2025`.

## Data provenance

- **Play-by-play:** Retrosheet EVENT files `https://www.retrosheet.org/events/<year>eve.zip`
  (HTTP 200), seasons **2015–2025** (11), saved raw to `data/history/<year>eve.zip`.
  Parsed directly (no Chadwick/pandas): `play` records → batter HR (`basic
  play == "HR"`) and pitcher K (`basic play` starts with `K`), pitcher tracked
  via `start`/`sub` fielding-position-1 records.
- **Final scores / winner:** joined to the Retrosheet TEAM game logs
  `gl2015..gl2025.txt` (already in `data/history/`) on key = `home+date+gamenum`
  = the event `id`. Join rate **25,192 / 25,193 games (99.996%)**; 1 tie
  (suspended) dropped. Official scores used for team runs and moneyline so no
  runs are re-derived from PBP.
- **Parsed data saved:** `data/history/mlb_parsed_batter_games.csv.gz`
  (521,413 batter-games), `data/history/mlb_parsed_starter_games.csv.gz`
  (50,384 starter-games).

**Parser validation vs public league totals** (independent ground truth):

| year | parsed total HR | official MLB | parsed pitcher K | official MLB |
|------|-----------------|--------------|------------------|--------------|
| 2023 | 5,864 | 5,868 (−0.07%) | 41,843 | 41,843 (exact) |
| 2024 | 5,446 | 5,453 (−0.13%) | 41,197 | ~41,200 |

Strikeout total matches to the unit; HR within 0.1% (a handful of edge codings)
— negligible for correlation estimates.

## Definitions / lines

- **Batter pairs** unit = **batter-game** (any player with ≥1 completed plate
  appearance). A = batter hit ≥1 HR that game. Universe includes pinch hitters
  and (pre-2022 NL) pitchers batting — dilutes P(A) but not the correlation.
- **team-runs OVER** = batter's team runs > that season's median team-runs-per-game
  (self-normalizing, removes era drift; the repo's convention). `team_over_45` =
  fixed team-over-4.5 line, reported as a robustness variant. Ties to the line → excluded.
- **Pitcher pairs** unit = **starter-game** (the fielding-position-1 starter),
  restricted to pitchers with **≥5 starts that season** so the self-referential K
  line is stable. A = starter's Ks > his own season-median K (the actual prop
  shape). **total OVER** = game total runs > season-median game total.
  **opp OVER** = opponent (batted-against) team runs > season-median team-runs.
- CI note: batter rows within a team-game share B (team over / win), so the naive
  99% CI is optimistic. A **cluster-floor CI** (n = distinct team-games, i.e. all
  batters in a game treated as fully redundant) is reported alongside; the truth
  lies between the two.

## Results (2015–2025, 11 seasons)

| pair | n | P(A) | P(B) | P(A∩B) | rho | 99% CI | verdict | source |
|------|---|------|------|--------|-----|--------|---------|--------|
| **1** HR × team-runs OVER (season median) | 454,873 | 0.107 | 0.497 | 0.080 | **+0.367** | [+0.352, +0.382] naive · [+0.320, +0.415] cluster-floor | strong + (matches prior) → **WIDEN-ONLY** | Retrosheet events 2015–25 + game logs |
| 1b HR × team OVER 4.5 (fixed line) | 521,413 | 0.107 | 0.433 | 0.070 | **+0.315** | [+0.303, +0.327] naive | strong + → WIDEN-ONLY | same |
| **2** HR × team WINS (moneyline) | 521,413 | 0.107 | 0.491 | 0.069 | **+0.232** | [+0.220, +0.245] naive · [+0.192, +0.272] cluster-floor | moderate + (matches prior) → **WIDEN-ONLY** | same |
| **3** K OVER × GAME total OVER (median) | 34,021 | 0.487 | 0.503 | 0.203 | **−0.257** | [−0.291, −0.222] | negative (matches prior) → **WIDEN-ONLY** | same |
| **3b** K OVER × OPP team total OVER (median) | 32,563 | 0.484 | 0.494 | 0.177 | **−0.380** | [−0.412, −0.348] | more − than full-game (matches −0.38 prior) → **WIDEN-ONLY** | same |
| 3b′ K OVER × OPP team total **UNDER** | 32,563 | — | — | — | **+0.380** | [+0.348, +0.412] | = −(3b), the pitcher-controlled half → POSITIVE | copula antisymmetry |
| **4** K OVER × pitcher's team WINS | 37,338 | 0.486 | 0.503 | 0.283 | **+0.242** | [+0.205, +0.278] | positive (matches prior) → **WIDEN-ONLY** | same |

**Win-prob lift per HR (pair 2 colour):** P(team wins | batter homered) = **0.650**
vs P(win | no HR) = 0.472 → a homer lifts the team's win prob by **+0.178** (n=521,413).

**Sign check vs priors — all pass, no investigation triggered:**
HR pairs strong/moderate positive ✓; K × total negative ✓; K × opponent-total
MORE negative than full-game total (−0.380 vs −0.257) exactly as predicted, and
matching the independent betfirm season K↔runs-allowed ≈ −0.38 prior ✓; the
opponent-UNDER framing (the half the pitcher actually controls) is +0.380 ✓.

**Copula-antisymmetry note (3b′):** the Gaussian copula rho is exactly
antisymmetric under complementing one leg (corr(Z_A, −Z_B) = −rho, threshold
flips sign), so `K_over × opp_UNDER` rho = −(`K_over × opp_OVER` rho) exactly.
Reported both ways so no false sign flip is read.

## Era stability (lightweight held-out-season check)

| pair | seasons < 2020 | seasons ≥ 2020 | drift |
|------|----------------|----------------|-------|
| HR × team-over | +0.364 (n=257,230) | +0.370 (n=264,183) | +0.006 (flat) |
| K × total-over | −0.287 (n=23,348) | −0.228 (n=24,574) | +0.059 (weaker recently) |

Batter HR×over is essentially constant across eras. K×total has weakened modestly
in the launch-angle/three-true-outcomes era (more HR-driven runs blunt the
K-suppresses-runs channel) — the point estimate is a blend; if anything, quote the
recent value (−0.228 total / expect the opp-total analog near −0.34) as the
forward-looking prior and widen.

## Recommended `pair_rho` priors (for central reconciliation — NOT applied)

Per hard rule 2 (report, don't edit config) and rule 5 (conservative default),
all are tagged **WIDEN-ONLY**: use the point estimate as the prior mean but keep
width wide, because (a) no per-game devigged player-prop closing lines exist here
to run the gold-standard conditional-MLE OOS log-loss gate the PLAN requires for
SHIP, and (b) batter rows are clustered within team-games. Era-stability is the
only genuine OOS check performed and it passes.

| SGP pair (leg family) | proposed prior rho | notes |
|-----------------------|--------------------|-------|
| batter HR × team total OVER | **+0.37** | strongest, most stable; floor CI +0.32 |
| batter HR × team moneyline (win) | **+0.23** | |
| starting-pitcher K OVER × game total OVER | **−0.26** (recent −0.23) | |
| starting-pitcher K OVER × opponent team total OVER | **−0.38** (recent ≈ −0.34) | pitcher-controlled half; strongest K pair |
| starting-pitcher K OVER × team moneyline (win) | **+0.24** | |

Same-team direction matters: "team total" for the HR pair = the batter's OWN
team; "opponent total" for the K pair = the team the pitcher faces. A leg-frame
mixup here silently flips the sign.

## Cross-check vs peers

At write time `docs/calibration/` held `PLAN.md`, this file, and **`results_ising.md`**
(the Rank-3 pairwise-Ising/max-entropy peer). No soccer/basketball player-prop peer
existed yet, so no head-to-head player rho comparison was possible.

**vs `results_ising.md`** (methodological, not a shared pair):
- The Ising agent proves the Ising `W_ij` and the copula `rho` are two coordinates
  on the *same* pair-joint (2-leg exact to 1e-12; 3-leg to ≤1e-6 for rho ≥ −0.4).
  Since I invert that *same* shipped copula to get each pair rho, my five player
  rhos are directly expressible as Ising weights — e.g. my strongest, HR×team-total
  rho ≈ +0.37, and the Ising's soccer btts×over2.5 (rho +0.75 → W +2.40) shows a
  rho of ~+0.37 maps to a positive W roughly ~+1.1, both "both-need-the-same-runs"
  positives, consistent in sign/ordering.
- Both files independently flag the **shared blind spot**: pairwise-only joints
  (copula OR Ising) omit true 3-way+ dependence. Relevant here when a batter-HR or
  pitcher-K leg sits inside a 3+ leg SGP — the pairwise rho is the right 2-way input
  but a same-game trio (e.g. HR × team-total × team-win, all positively linked)
  will carry residual positive 3-way mass neither model prices → reinforces the
  WIDEN-ONLY stance on multi-leg player combos.
- Both agents saw no peer files at their own run time and both defer SHIP to an
  OOS log-loss gate — no method conflict.

Consistency notes for whoever lands next:
- My game-level MLB moneyline↔total sign should agree with the existing
  `tools/calibrate_pairs_from_history.py::load_mlb` game-level pairs (same
  Retrosheet source, team level). This work is orthogonal (player→team), so no
  double-count.
- If a soccer/basketball peer reports scorer×win and scorer×over, the ordering
  should echo mine: player-scoring-event × own-team-win is a solid positive, and
  the player event × own-team-total is the stronger positive — mirrored here by
  HR×win (+0.23) < HR×team-total (+0.37).

## NEXT STEPS

- **Runs next:** peer calibration agents (soccer/basketball/ising) finish their
  `results_*.md`; a reconciliation pass compares signs/magnitudes across domains.
- **Owner (operator):** decide whether to load these five as WIDEN-ONLY priors
  into the pricer's player-prop `pair_rho` table (currently uncalibrated) under
  sign-off — additive, gated wide.
- **To promote WIDEN-ONLY → SHIP:** run the gold-standard conditional-MLE OOS
  log-loss gate once per-game devigged player-prop closing marginals exist
  (Kalshi prod-shadow settlements, Phase 6), the same bar the game-level pairs
  cleared. Until then, keep width proportional to uncertainty.
- **Decision owed by user:** confirm the starter-K line convention (self season
  median vs Kalshi's posted K line) so the calibrated shape matches the traded
  prop; if Kalshi posts fixed K lines, re-run with those lines when a book exists.
