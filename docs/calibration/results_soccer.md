# Correlation Calibration — SOCCER (first-half, player-scorers, Dixon-Coles rank-4)

Run date: 2026-07-07. Agent domain (PLAN ranks 1/2/4 for soccer): first-half ×
full-game pairs, player anytime-scorer SGP pairs, and the Dixon-Coles player-goal
extension. Calibrates ONLY the joint/correlation layer — marginals come live from
Kalshi leg books at quote time.

Method mirrors the shipped pipeline exactly:
- **Rank 1** — over N historical matches measure P(A), P(B), P(A∩B).
- **Rank 2** — invert the SAME copula the pricer runs
  (`combomaker.pricing.copula.gaussian_copula_joint_prob`) by bisection to a
  drop-in `pair_rho` (`implied_rho()`); 99% CI = binomial SE on P(A∩B) pushed
  through the monotone solver (`_Z99 = 2.576`). Mirror of
  `tools/calibrate_pairs_from_history.py`.
- **Gold conditional-MLE** — every observation carries its OWN implied marginals;
  a single copula rho is fit by MLE over the 4 joint cells and OOS-gated by
  held-out-season log-loss vs independence. Mirror of `tools/fit_conditional_rho.py`
  (same Owen's-T `bvn_cdf`, self-checked to <5e-7 vs the shipped copula).

New tools (additive; import the shipped copula/DC modules, touch no `src/`,
`config/`, or existing tool):
`tools/calibrate_soccer_firsthalf.py`, `tools/calibrate_soccer_scorers.py`,
`tools/fetch_understat.py`, `tools/dc_ml_player_goal_prior.py`. Run with the
project venv (`.venv/Scripts/python.exe`).

```
 DATA FLOW
 ┌──────────────────────────────┐        ┌─────────────────────────────────┐
 │ football-data.co.uk club CSV │        │ Understat JSON API (discovered   │
 │ E0/D1/F1/I1/SP1-*.csv        │        │ from js/league.min.js,match.min) │
 │ HTHG/HTAG/HTR + FTHG/FTAG/FTR│        │ getLeagueData/{lg}/{season}      │
 │ + closing 1X2 + O/U2.5 odds  │        │ getMatchData/{id} -> rosters     │
 │ 8,981 matches (already local)│        │ 3,652 matches (5 lg × 21,22)     │
 └──────────────┬───────────────┘        └────────────────┬────────────────┘
                │ tools/calibrate_soccer_firsthalf.py      │ tools/fetch_understat.py
                ▼                                          ▼ (cached data/history/understat/)
        FIRST-HALF × FULL-GAME                      tools/calibrate_soccer_scorers.py
        (rank 1/2 + era-stability)                  PLAYER SCORERS (rank1/2 + gold MLE)
                                                          │
                                            tools/dc_ml_player_goal_prior.py (rank 4)
```

## Data provenance

- **First-half:** the EXISTING football-data.co.uk club CSVs in `data/history/`
  (E0/D1/F1/I1/SP1, top-5 EU, seasons 20/21–24/25). Carry half-time goals/result
  (HTHG/HTAG/HTR) alongside full-time and closing 1X2 + O/U-2.5 odds. **8,981
  matches, no download needed.**
- **Player scorers:** **Understat** JSON endpoints (the pages no longer inline
  the data — 2026-07-06 the blobs load via `getLeagueData/{league}/{season}` →
  `{dates(with devigged forecast), teams, players}` and `getMatchData/{id}` →
  `{rosters:{h,a}, shots}`; `rosters[side]` = per-player `goals/own_goals/xG/
  time/team_id`). Fetched **5 leagues (EPL, La_liga, Bundesliga, Serie_A,
  Ligue_1) × seasons 2021 & 2022 = 3,652 result-matches**, raw-cached to
  `data/history/understat/` (`league_*.json`, `match_*.json`). StatsBomb open-data
  reachable but not needed — Understat gave full 5-league coverage.
- **Data-quality gate (Understat, EPL-2022 spot-check, 380 matches):**
  roster-goals + opponent own-goals **reconstruct the official scoreline in
  380/380 matches (0 mismatches)**; 44 matches carry own goals (handled — own
  goals excluded from a player "scoring"). Anytime-scorer rate among players with
  minutes = **8.18%**, vs the mean xG-implied `1−exp(−xG)` = **7.73%** → the
  xG-derived per-player scorer marginal is well-calibrated (used for the gold
  conditional fit).

Notes on the gold conditional-MLE marginals: per-game **devigged player-prop
closing lines do not exist offline**, so (per PLAN, "condition on a
devigged/implied scorer probability rather than pooling") the per-obs marginals
are model-implied: scorer prob = `1−exp(−xG_player)`; team-win prob = Understat's
own devigged forecast `{w,d,l}`; over-2.5 prob = `P(N≥3), N~Poisson(team-total
xG)`. These are model-implied not market-devigged — an honest substitute where no
historical prop book exists; treated as the OOS gate, not a SHIP-at-tight-width
license.

---

## 1. FIRST-HALF × FULL-GAME (rank 1/2)   `tools/calibrate_soccer_firsthalf.py`

**Empirical conditional fractions (the numbers the directive asks for):**

| statement | value | n |
|-----------|-------|---|
| P(FT home-win \| HT home-leader) | **76.7%** | 3,021 |
| P(FT away-win \| HT away-leader) | **68.5%** | 2,380 |
| P(FT over2.5 \| 1H over0.5) | **67.0%** | 6,463 |
| P(FT over2.5 \| 1H over1.5) | **87.6%** | 3,244 |

**Copula calibration** (pooled Rank-1/2; OOS = era-stability split at 2023, since
no per-game HT closing line exists to run the conditional-MLE gate):

| pair | n | P(A) | P(B) | P(A∩B) | rho | 99% CI | OOS (era drift) | verdict | source |
|------|---|------|------|--------|-----|--------|-----------------|---------|--------|
| 1H home-lead × FT home-win | 8,981 | 0.336 | 0.427 | 0.258 | **+0.710** | [+0.649,+0.766] | stable (−0.047) | **SHIP** (era-stable) | football-data club CSV |
| 1H away-lead × FT away-win | 8,981 | 0.265 | 0.319 | 0.182 | **+0.698** | [+0.638,+0.753] | stable (−0.004) | **SHIP** (era-stable) | " |
| 1H over0.5 × FT over2.5 | 8,981 | 0.720 | 0.530 | 0.482 | **+0.693** | [+0.610,+0.772] | stable (+0.014) | **SHIP** (era-stable) | " |
| 1H over1.5 × FT over2.5 | 8,981 | 0.361 | 0.530 | 0.316 | **+0.765** | [+0.700,+0.826] | stable (−0.008) | **SHIP** (era-stable) | " |
| 1H over1.5 × FT over3.5 | 8,981 | 0.361 | 0.314 | 0.225 | **+0.722** | [+0.664,+0.775] | stable (−0.007) | **SHIP** (era-stable) | " |

**Sign check vs prior — PASS.** Prior: "1H×FG strong POSITIVE (HT home leader →
~81% FT win)." Measured strong positive rho **+0.69…+0.77**, era-drift < 0.05
across a 2023 split (5,478 vs 3,503 matches). The empirical HT-home-leader → FT-win
is **76.7%** (away-leader 68.5%, lower because a half-time lead is worth less to the
away side that lacks home advantage) — a touch below the folk 81% but same strong
regime; no sign issue. `SHIP` tag caveat: OOS here is era-stability, not the
conditional-MLE log-loss gate (impossible without a per-game HT line), so keep a
modest width until a live 1H book exists.

---

## 2. PLAYER SCORERS (rank 1/2 + gold conditional-MLE)   `tools/calibrate_soccer_scorers.py`

`STAR` = the single top-xG player per team (≤1 obs/side, least clustered, most
decision-relevant — SGP scorer legs are quoted on featured players). `THREATS` =
all players with match xG ≥ 0.20. `ALL` = every player with minutes. Pooled rho +
99% binomial CI is the Rank-1/2 number; **conditional-MLE rho (99% CI = ±2.576·SE)
is the double-counting-safe SHIP number** and the OOS column is its held-out-season
(2021→2022) log-loss verdict.

| pair (frame) | n | P(A) | P(B) | P(A∩B) | pooled rho [99% CI] | **cond-MLE rho [99% CI]** | OOS | verdict | source |
|--------------|---|------|------|--------|----------------------|---------------------------|-----|---------|--------|
| **scorer × team-win** `STAR` | 7,304 | 0.503 | 0.375 | 0.281 | +0.579 [+0.502,+0.652] | **+0.490 [+0.441,+0.539]** | BEATS indep | **SHIP** | Understat 5lg 21–22 |
| scorer × team-win `THREATS` | 14,071 | 0.461 | 0.519 | 0.301 | +0.381 [+0.322,+0.439] | +0.460 [+0.427,+0.493] | BEATS indep | SHIP | " |
| scorer × team-win `ALL` | 111,018 | 0.081 | 0.375 | 0.051 | +0.348 [+0.321,+0.375] | +0.420 [+0.397,+0.443] | BEATS indep | SHIP (clustered) | " |
| **scorer × over2.5** `STAR` | 7,304 | 0.503 | 0.523 | 0.343 | +0.483 [+0.402,+0.560] | **+0.460 [+0.414,+0.506]** | BEATS indep | **SHIP** | " |
| scorer × over2.5 `THREATS` | 14,071 | 0.461 | 0.604 | 0.343 | +0.414 [+0.351,+0.476] | +0.480 [+0.449,+0.511] | BEATS indep | SHIP | " |
| **two TEAMMATES both score** `top-2 xG` | 4,191 | 0.617 | 0.424 | 0.270 | +0.057 [−0.061,+0.176] | **+0.000 [−0.067,+0.067]** | does NOT beat indep | **WIDEN-ONLY / NO-QUOTE** | " |
| two TEAMMATES both score `all threat pairs` | 13,368 | 0.560 | 0.348 | 0.207 | +0.083 [+0.021,+0.145] | +0.020 [−0.019,+0.059] | beats indep by ~1e-4 | WIDEN-ONLY | " |
| **two OPPOSING scorers** `top-1 each` | 2,654 | 0.574 | 0.543 | 0.313 | +0.010 [−0.139,+0.158] | **+0.090 [+0.002,+0.178]** | beats indep by ~1e-3 | **WIDEN-ONLY** | " |
| two OPPOSING scorers `all cross-pairs` | 12,153 | 0.463 | 0.451 | 0.208 | −0.004 [−0.065,+0.056] | +0.030 [−0.011,+0.071] | beats indep by ~2e-4 | WIDEN-ONLY | " |

**Odds-discount framing** (how much a fair scorer price should shorten given the
other leg, P(A|B) vs P(A)):

| pair | P(A) | P(A \| B) | conditional lift |
|------|------|-----------|------------------|
| scorer × team-win `STAR` | 0.503 | 0.751 | **+49.1%** |
| scorer × team-win `THREATS` | 0.461 | 0.580 | +25.9% |
| scorer × over2.5 `STAR` | 0.503 | 0.657 | **+30.4%** |
| scorer × over2.5 `THREATS` | 0.461 | 0.568 | +23.3% |
| two TEAMMATES `top-2` | 0.617 | 0.637 | +3.3% |
| two OPPOSING `top-1` | 0.574 | 0.576 | +0.5% |

**Sign check vs priors:**

- **scorer × win — strong POSITIVE, MATCHES prior** ("blogs ~r0.5–0.6, ~40–55%
  odds discount"). Star conditional **+0.49** (pooled +0.58), odds discount
  **+49%**. Dead in the predicted band. ✓
- **scorer × over2.5 — POSITIVE, MATCHES prior** ("weaker positive, ~15–30%
  discount"). Odds discount **+23–30%** (weaker than the win pair, as predicted).
  Note the copula *rho* (~+0.46–0.48) is ≈ the win-pair rho, not visibly weaker —
  the "weaker" shows up in the odds-discount metric because over-2.5 has a higher
  base rate; a single goal is a large share of soccer's low total, so it moves the
  over about as hard as the win. ✓ (sign & discount magnitude both match).
- **two teammates — near ZERO, prior said mild + (~+0.15…+0.35): DIVERGENCE (not a
  sign flip), and the conditional number is the right one.** Pooled is a weak
  +0.06–0.08; the **conditional-MLE is ~0.00–0.02 and fails / barely-passes the OOS
  gate.** Explanation (investigated): two teammates scoring share a "team had a big
  attacking game" common factor (drives +), competing against a fixed-goal-supply
  substitution effect (drives −). The conditional fit conditions on **each
  player's own xG**, which already encodes how many chances the team generated —
  stripping out exactly the shared team-output factor that live SGP marginals
  ALSO already price. What remains is ≈ 0. The folk "+0.15–0.35" is the pooled
  number that **double-counts** team strength (the very double-count PLAN warns
  about). For our top-down pricer (marginals live), the correct residual is ~0 →
  **WIDEN-ONLY / NO-QUOTE**, do not ship a fat positive.
- **two opposing — ≈ ZERO / very slightly +, MATCHES prior** ("≈0/slightly
  negative"). Pooled ≈ 0 (−0.004…+0.010); conditional a small **+0.03…+0.09**. The
  Dixon-Coles low-score ρ≈−0.13 is a *scoreline-cell* effect (0-0/1-1 excess), not
  the anytime-scorer correlation, which nets to ~0 (open games: both score, + ;
  blowouts: loser shut out, − ). Tiny net positive from shared game-tempo. ✓

**Clustering caveat:** in the `THREATS`/`ALL`/`all-pairs` frames many
observations share a match/team outcome, so those conditional SEs are optimistic
(effective n < row n). The `STAR`/`top-1`/`top-2` frames (≤1 player per team) are
the least clustered and are the values to trust; they are also the ones quoted
in the tables' bold rows.

---

## 3. DIXON-COLES rank-4 — `moneyline | player_goal` prior   `tools/dc_ml_player_goal_prior.py`

**Shipped state:** `config.py` soccer table `moneyline|player_goal = 0.50` (band
±0.12), tagged "structural implication ×2 examples". This scalar lives in the **v1
copula fallback** `pair_rho`, used when the DC scoreline can't be identified (parse
/ identification doubt). The DC structural path itself consumes **no scalar rho**:
player goals | n team goals ~ `Binomial(n, q)` (multinomial thinning), and the win
indicator `n_A > n_B` is read off the **same** scoreline, so a positive
ml×player_goal correlation *emerges* from the structure.

**What the shipped DC model INDUCES** (recomputed off `pricing.dixon_coles`, edited
nothing; 6 representative matches, star share tuned to P(scorer)≈0.35–0.55):

```
 lam_a lam_b  share  P(win)  P(scr)  P(both)     rho
  1.70  1.00   0.35   0.532   0.448    0.328   0.538   (home fav)
  1.70  1.00   0.45   0.532   0.535    0.384   0.587
  1.50  1.20   0.40   0.435   0.451    0.288   0.550
  1.30  1.30   0.40   0.362   0.405    0.232   0.548   (even game)
  1.10  1.60   0.45   0.256   0.390    0.175   0.556   (team A underdog)
  2.00  0.90   0.40   0.625   0.551    0.435   0.566   (strong fav)
  DC-induced rho range [+0.538, +0.587], mean +0.558
```

**Reconciliation:**

| source | ml×player_goal rho |
|--------|--------------------|
| shipped copula-fallback prior | 0.50 |
| shipped DC structural path (induced, recomputed) | **+0.56** (mean; +0.54…+0.59) |
| empirical POOLED star scorer×win | +0.58 [+0.50, +0.65] |
| **empirical conditional-MLE star scorer×win (double-count-safe)** | **+0.49 [+0.44, +0.54]** |

**Proposed value: KEEP `moneyline|player_goal = 0.50`** — now empirically
validated, not a hand prior. It sits on the conditional-MLE point (+0.49, 99% CI
[0.44, 0.54]) which is the correct target for the copula fallback (live marginals
already price team/player strength, so the double-counting-safe conditional — not
the pooled +0.58 — is the right rho). Upgrade its config note from "structural
implication ×2 examples" to "calibrated: Understat 5-league 2021–22, n=7,304
star-scorer obs, conditional-MLE +0.49 (99% CI [0.44,0.54]), OOS-beats-independence;
DC-structural-induced +0.56". Band ±0.12 is adequate (covers [0.44,0.54] and the
star↔threats spread).

**Non-obvious finding to flag:** the DC structural path induces **+0.56**, about
**+0.07 above** the empirical conditional **+0.49**. The pure Poisson-thinning
idealization ties a player's goals to the team goal count more tightly than reality
(real games leak idiosyncratic variance: the star blanks in a win, penalties, own
goals, rotation, red cards). So the structural fair is ~0.07-rho **rich** on
scorer×win SGPs — inside the shipped model-form band, but it means the DC path
should keep the player-leg uncertainty band ≥ ~0.10 and, if anything, the copula
fallback at 0.50 is *closer to the data* than the structural +0.56. (The empirical
+0.49 may itself be mildly attenuated by xG-marginal noise, nudging the truth
toward 0.50–0.52 — which only strengthens "keep 0.50".) *No module edit made,
per hard rule 2 — reported for operator reconciliation.*

---

## 4. Cross-check vs peers

`ls docs/calibration/` at write time: `PLAN.md`, `results_baseball.md`,
`results_ising.md`, and this file.

**vs `results_baseball.md` (shared structure: player-event × team-win / team-total):**
The baseball agent explicitly predicted the soccer ordering — "player-scoring-event
× own-team-win a solid positive, player event × own-team-total the *stronger*
positive — mirrored by HR×win (+0.23) < HR×team-total (+0.37)."
- **Signs all agree:** every soccer scorer×win / scorer×over is a solid positive,
  matching baseball's positive HR pairs. ✓
- **Magnitude ordering differs (noted, explained):** in soccer scorer×win (star
  cond **+0.49**) ≈ scorer×over (star cond **+0.46**) — roughly EQUAL, and in the
  THREATS frame over (+0.48) edges win (+0.46) as baseball predicted. Unlike
  baseball's clear win < total, soccer's win and total pairs are ~tied. Cause:
  baseball totals are ~9 runs so one HR barely swings the win but co-moves with a
  slugfest (over); soccer totals are ~2.7 goals so a single goal is frequently the
  decisive winning margin AND a big share of the total — it loads onto win and over
  about equally. Different base-rate structure, same signs. No frame/label bug.
- Both agents land player pairs at **+0.2…+0.5** and both defer SHIP-at-tight-width
  to an OOS gate; my scorer pairs additionally **pass** a held-out-season log-loss
  gate (Understat forecast marginals), so they carry a firmer SHIP than baseball's
  WIDEN-ONLY (which lacked per-game prop marginals).

**vs `results_ising.md` (shared reference: game-level soccer pairs):** I ran the
shipped `tools/calibrate_pairs_from_history.py`; its soccer block reproduces the
Ising §3 numbers **to the digit** — `btts×over2.5 +0.746`, `home_win×over2.5
+0.276`, `btts×home_win −0.197` (8,982 games). So my environment/copula path is
byte-identical to the game-level calibration the Ising agent used; no drift. My new
pairs (first-half, scorers) are disjoint from the Ising's game-level set, so there
is no shared-pair rho to diff — but the structural ordering is coherent:
`btts×over2.5 (+0.75) > scorer×over2.5 (+0.46) > home_win×over2.5 (+0.28)` — both
teams scoring correlates with the over harder than one player scoring, which in
turn beats a single moneyline. Sensible nesting, no contradictions.

Consistency note for the next lander: my scorer×win conditional **+0.49** and the
DC-induced **+0.56** both agree in sign/magnitude with the shipped
`soccer:moneyline|player_goal = 0.50` — no reconciliation conflict.

---

## 5. Recommended priors (for central reconciliation — NOT applied; hard rule 2)

| SGP pair (soccer) | proposed rho | band | verdict | basis |
|-------------------|--------------|------|---------|-------|
| 1H home-lead × FT home-win | **+0.71** | ±0.08 | SHIP (era-stable) | 8,981 club, pooled + era-split |
| 1H away-lead × FT away-win | **+0.70** | ±0.08 | SHIP | " |
| 1H over0.5 × FT over2.5 | **+0.69** | ±0.10 | SHIP | " |
| 1H over1.5 × FT over2.5 | **+0.77** | ±0.08 | SHIP | " |
| 1H over1.5 × FT over3.5 | **+0.72** | ±0.08 | SHIP | " |
| `moneyline\|player_goal` (scorer × win) | **+0.49** (keep 0.50) | ±0.12 | SHIP | Understat, cond-MLE OOS-pass, DC-induced +0.56 |
| `player_goal\|total` (scorer × over2.5) | **+0.47** | ±0.12 | SHIP | Understat, cond-MLE OOS-pass |
| two teammates both score | **~0.00** | ±0.10 | WIDEN-ONLY / NO-QUOTE | conditional ≈0, OOS fails; pooled +0.06 double-counts team |
| two opposing scorers | **~0.03** | ±0.10 | WIDEN-ONLY | ≈independent, tiny + |

Frame reminder: "team-win" and "over" are the scorer's OWN-team win / the full
game total; a leg-frame mixup silently flips scorer×win. `player_goal|total`
currently hand-prior 0.40 in config — this calibration supports nudging to ~0.47.
Teammates/opposing player-goal×player-goal is NOT in the config table today; keep
it out (or at ~0 with wide band) rather than adding a fat positive.

## NEXT STEPS

- **Runs next:** nothing auto-runs. `tools/fetch_understat.py` (resumable, cached)
  then `tools/calibrate_soccer_firsthalf.py` / `tools/calibrate_soccer_scorers.py`
  / `tools/dc_ml_player_goal_prior.py` reproduce every number on demand.
- **Owner (operator):** decide whether to (a) load the five 1H×FG pairs as new
  SHIP priors when a Kalshi 1H book exists; (b) confirm `soccer:moneyline|player_goal
  = 0.50` stays (validated) and nudge `player_goal|total` 0.40 → ~0.47; (c) accept
  teammates/opposing scorer pairs as WIDEN-ONLY/NO-QUOTE (do NOT ship the folk
  +0.15–0.35 teammate prior — it double-counts team strength the live marginals
  already carry).
- **To promote to full SHIP:** the scorer pairs already pass a held-out-season
  log-loss gate on Understat-forecast marginals; re-run the gate on Kalshi
  prod-shadow devigged prop closing lines (Phase 6) to graduate from
  model-implied to market-devigged marginals. The 1H pairs need a live 1H book to
  replace the era-stability proxy with the conditional-MLE gate.
- **Decision owed by user:** whether the DC structural path's +0.07-rich
  scorer×win induction (structural +0.56 vs empirical +0.49) warrants widening the
  DC player-leg band or a small thinning-model haircut — flagged, not changed
  (hard rule 2).
</content>
