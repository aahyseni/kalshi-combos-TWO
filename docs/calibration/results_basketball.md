# Correlation Calibration — BASKETBALL (NBA + WNBA)

Run date: 2026-07-07. Agent domain (PLAN ranks 1/2 for basketball): player
points-prop SGP pairs and first-half/period × full-game pairs. Calibrates ONLY
the joint/correlation layer — marginals come live from Kalshi leg books at quote
time.

Method mirrors the shipped pipeline exactly (`tools/calibrate_pairs_from_history.py`):
- **Rank 1** — over N historical (player|game) observations measure P(A), P(B),
  P(A∩B).
- **Rank 2** — invert the SAME Gaussian copula the pricer runs
  (`combomaker.pricing.copula.gaussian_copula_joint_prob`) by bisection to a
  drop-in `pair_rho` (`implied_rho()`); 99% CI = binomial SE on P(A∩B) pushed
  through the monotone solver (`_Z99 = 2.576`).
- No live prop line exists offline, so — exactly like the repo's game-total
  median trick (`load_nba_modern`) — each player's line proxy = his OWN
  regular-season **median points** that season ("over" = exceeds it), and the
  total/1H-total line = the season median total. Base rates confirm the proxy is
  a faithful ~50/50 line: P(points-over)≈0.507, P(total-over)≈0.500. Because the
  Gaussian-copula rho is marginal-invariant, a rho calibrated at ~0.50 marginals
  transfers to a real prop line set at any level.

New tools (additive; import the shipped copula, touch no `src/`, `config/`, or
existing tool). Run with the project venv (`.venv/Scripts/python.exe`):
- `tools/calibrate_nba_player_points.py` — pairs 1 & 2, NBA + WNBA, usage tiers,
  blowout split, era split.
- `tools/calibrate_nba_first_half.py` — pair 3 (1H × FG) from PBP, NBA + WNBA,
  with a team-box final-score validation gate.

```
 DATA FLOW
 ┌─────────────────────────────────────┐     ┌──────────────────────────────────┐
 │ hoopR-data (NBA) / wehoop-data(WNBA) │     │ same repos, nba|wnba/pbp/parquet │
 │ .../player_box/parquet/*.parquet     │     │ play_by_play_YYYY.parquet        │
 │ points, team_score, opp_score,       │     │ period_number + running          │
 │ team_winner, did_not_play  (ESPN)    │     │ home_score/away_score            │
 │ NBA 2010-23 + WNBA 2010-22 (excl 16) │     │ NBA 2021-23, WNBA 2019-22        │
 └──────────────────┬──────────────────┘     └─────────────────┬────────────────┘
        tools/calibrate_nba_player_points.py     tools/calibrate_nba_first_half.py
                    ▼                                            ▼
         PAIR 1  points-over × total-over          PAIR 3  1H-total × FG-total
         PAIR 2  points-over × own moneyline               1H-margin × FG-result
         (+ usage tiers, blowout split, era)      (+ team-box final-score gate 99.4%)
```

## Data provenance (fetch notes — non-trivial)

- **Player box:** `sportsdataverse/hoopR-data` (NBA) and `sportsdataverse/wehoop-data`
  (WNBA), `main` branch, path `{nba,wnba}/player_box/parquet/player_box_YYYY.parquet`.
  **These 100+ GB repos have NO GitHub releases and NO tags** (releases API → `[]`,
  tags API → `[]`), and **`raw.githubusercontent.com` returns `404: Not Found`**
  for every blob (a known large-repo raw limitation; the guessed
  `.../releases/download/nba/player_box_2024.parquet` in PLAN 404'd for the same
  reason — there is no release at all). Working route discovered: the **GitHub
  contents API with `Accept: application/vnd.github.raw`**, which streams files
  <1 MB directly. Downloaded NBA `player_box_2010..2023` and WNBA
  `player_box_2010..2022` into `data/history/nba_player_box_*.parquet` /
  `wnba_player_box_*.parquet`. Player box on `main` stops at NBA 2023 / WNBA 2022
  (the tree is not updated to 2024-26 — the local team-box 2024-26 files came from
  a fresher pull); 14 NBA + 12 WNBA seasons is ample n.
- **Schema quirk:** WNBA `player_box_2016` is the OLD wide format (`pts` as a
  *string*, no game context) — **skipped**. Every other NBA/WNBA year uses the
  rich schema (`points:int` + inline `team_score/opponent_team_score/team_winner/
  did_not_play`), so both leagues share one code path.
- **Play-by-play (for pair 3):** same repos, `{nba,wnba}/pbp/parquet/
  play_by_play_YYYY.parquet`. These are >1 MB (NBA ~24 MB, WNBA ~3-4 MB) so the
  contents API caps out; fetched via the **git blobs API by SHA** (base64,
  ≤100 MB). Halftime = running score at the last play with `period_number ≤ 2`;
  final = max running score (incl. OT). **`stats.nba.com` live API confirmed
  IP-blocked (HTTP 000) — not used.**
- **Correctness gate (PBP):** PBP-derived final total vs hoopR team-box final total,
  joined on `game_id`: **NBA 3421/3443 (99.36%)**, **WNBA 744/746 (99.73%)**; mean
  1H/FG totals 113.5/224.9 (NBA) and 81.8/162.3 (WNBA) — both spot-on for the
  league. The PBP running score is trustworthy for half-level combos.
- Regular season only (`season_type == 2`) throughout; a player needs ≥10 played
  games that season for a stable median; DNP rows dropped.

---

## PAIR 1 — player points OVER × game total OVER   (rank 1/2)

Unit = **player-game** (player who played). A = his points > his season median;
B = the game total > the season median total. `clus` = cluster-floor CI (binomial
SE recomputed with n = distinct games, all players in a game treated as fully
redundant on the shared total leg — the honest lower bound, mirroring
`results_baseball.md`). Truth lies between the naive and cluster CIs.

| league | subset | n | P(A) | P(B) | P(A∩B) | rho | 99% CI (naive) | 99% CI (cluster) | verdict |
|--------|--------|---|------|------|--------|-----|----------------|------------------|---------|
| NBA | ALL players | 303,401 | 0.507 | 0.501 | 0.276 | **+0.142** | [+0.129,+0.155] | [+0.086,+0.198] | + moderate → WIDEN-ONLY |
| NBA | PPG ≥ 15 | 60,806 | 0.501 | 0.518 | 0.297 | **+0.232** | [+0.203,+0.261] | [+0.175,+0.289] | + → WIDEN-ONLY |
| NBA | PPG ≥ 20 (stars) | 23,637 | 0.499 | 0.542 | 0.312 | **+0.261** | [+0.214,+0.308] | [+0.196,+0.325] | + → WIDEN-ONLY |
| NBA | PPG ≥ 25 (elite) | 8,437 | 0.500 | 0.582 | 0.336 | **+0.287** | [+0.204,+0.367] | [+0.194,+0.377] | + → WIDEN-ONLY |
| WNBA | ALL players | 38,375 | 0.509 | 0.500 | 0.280 | **+0.159** | [+0.122,+0.195] | [+0.008,+0.305] | + moderate → WIDEN-ONLY |
| WNBA | PPG ≥ 15 | 4,800 | 0.496 | 0.541 | 0.314 | **+0.283** | [+0.177,+0.385] | [+0.124,+0.434] | + → WIDEN-ONLY |
| WNBA | PPG ≥ 20 (stars) | 867 | 0.499 | 0.593 | 0.351 | **+0.346** | [+0.083,+0.585] | [+0.065,+0.600] | + (thin n) → WIDEN-ONLY |

**Usage gradient (the requested "coefficient rises with usage" check) — CONFIRMED,
both leagues.** NBA +0.142 → +0.232 → +0.261 → +0.287; WNBA +0.159 → +0.283 →
+0.346. A star's points move a bigger share of the total (P(B|A) rises: bench
players barely nudge it), so the copula rho climbs monotonically with scoring
volume. **Sign check vs prior — PASS** (prior: POSITIVE moderate ~0.2-0.4, weaker
than NFL QB×total 0.42 because 10 players share the total). The **all-players**
value sits low (~0.14-0.16 — the average role player barely moves the total); the
**mid-usage/star** cohort that props are actually quoted on lands squarely in the
predicted 0.23-0.35 band, still short of the NFL QB. Even under the cluster floor
every tier stays positive.

---

## PAIR 2 — player points OVER × his TEAM wins (moneyline)   (rank 1/2)

| league | subset | n | P(A) | P(B) | P(A∩B) | rho | 99% CI (naive) | 99% CI (cluster) | verdict |
|--------|--------|---|------|------|--------|-----|----------------|------------------|---------|
| NBA | ALL players | 309,786 | 0.507 | 0.500 | 0.268 | **+0.090** | [+0.077,+0.103] | [+0.034,+0.145] | + weak → WIDEN-ONLY |
| NBA | PPG ≥ 15 | 62,072 | 0.501 | 0.530 | 0.289 | **+0.147** | [+0.118,+0.177] | [+0.090,+0.204] | + weak-mod → WIDEN-ONLY |
| NBA | PPG ≥ 20 (stars) | 24,122 | 0.499 | 0.557 | 0.300 | **+0.140** | [+0.092,+0.187] | [+0.074,+0.205] | + (non-monotone) → WIDEN-ONLY |
| NBA | PPG ≥ 25 (elite) | 8,600 | 0.500 | 0.608 | 0.324 | **+0.128** | [+0.044,+0.212] | [+0.034,+0.222] | + (non-monotone) → WIDEN-ONLY |
| WNBA | ALL players | 39,411 | 0.509 | 0.501 | 0.272 | **+0.105** | [+0.069,+0.141] | [−0.042,+0.250] | + weak → WIDEN-ONLY |
| WNBA | PPG ≥ 15 | 4,917 | 0.498 | 0.547 | 0.305 | **+0.209** | [+0.104,+0.313] | [+0.051,+0.362] | + → WIDEN-ONLY |
| WNBA | PPG ≥ 20 (stars) | 887 | 0.502 | 0.566 | 0.311 | **+0.173** | [−0.082,+0.416] | [−0.099,+0.432] | + (thin n) → WIDEN-ONLY |

**Sign check vs prior — PASS** (prior: POSITIVE weak-moderate ~0.1-0.25,
non-monotonic due to blowout bench-sitting). Weak positive overall, rising to
~+0.15-0.21 at mid-usage then **flattening/receding** at the elite tier (NBA
+0.147 → +0.140 → +0.128) — exactly the predicted non-monotonicity. Win-prob lift:
P(team wins | player over his median) = 0.53 vs 0.50 base for a role player, larger
for stars. **The points×win pair is materially WEAKER than points×total** (ordering
+0.09 < +0.14 all, +0.14 < +0.26 stars) — a single player scoring over is a much
noisier win signal than a total signal when 10 players share the game.

### Blowout / garbage-time nuance (pair 2, split by final |margin|)

| league | bucket | n | P(A) | P(B) | rho (all) | | stars (PPG≥20) P(A) | rho (stars) |
|--------|--------|---|------|------|-----------|---|--------------------|-------------|
| NBA | close \|m\|≤8 | 135,668 | 0.518 | 0.499 | **+0.041** | | 0.576 | +0.077 |
| NBA | mid 9-19 | 121,486 | 0.496 | 0.498 | **+0.098** | | 0.478 | +0.209 |
| NBA | blowout ≥20 | 52,632 | 0.502 | 0.508 | **+0.198** | | **0.326** | +0.311 |

Two effects, both real: (1) **rho rises with margin** (+0.04 → +0.20) — in a
coin-flip close game one player's over barely predicts the win; in a blowout the
outcome is decisive and co-moves with overall team dominance, which co-moves with
the player scoring. (2) **The garbage-time bench-sitting effect lives in the star
base rate**, not the rho: a star exceeds his median in **57.6%** of close games but
only **32.6%** of blowouts — in lopsided games (winning OR losing big) his minutes
get cut and he lands under his median. **Pricing implication:** a heavy favorite's
star points-over leg should be shaded DOWN for garbage-time risk, and the
points×moneyline rho is *regime-dependent* — quote it near ~+0.05-0.10 when the ML
is near 50/50 and toward ~+0.20 when the ML is lopsided, rather than one scalar.
(WNBA shows the same shape — close +0.035, mid +0.117, blowout +0.272 — on thinner n.)

---

## PAIR 3 — first-half × full-game   (rank 1/2, game-level; from PBP)

Unit = **game** (no within-game clustering, so the naive CI is honest). Validated
against team-box finals (see provenance). 1H-total/FG-total use the season-median
line; 1H-margin = home leads at half, FG-result = home wins.

| league | pair | n | P(A) | P(B) | P(A∩B) | rho | 99% CI | verdict |
|--------|------|---|------|------|--------|-----|--------|---------|
| NBA (2021-23) | 1H-total OVER × FG-total OVER | 3,294 | 0.495 | 0.499 | 0.383 | **+0.756** | [+0.660,+0.839] | strong + → SHIP-candidate (widen) |
| NBA (2021-23) | 1H-margin (home lead) × FG home win | 3,488 | 0.525 | 0.557 | 0.407 | **+0.665** | [+0.558,+0.760] | strong + → SHIP-candidate (widen) |
| WNBA (2019-22) | 1H-total OVER × FG-total OVER | 702 | 0.510 | 0.491 | 0.382 | **+0.734** | [+0.503,+0.901] | strong + → WIDEN-ONLY (thin n) |
| WNBA (2019-22) | 1H-margin (home lead) × FG home win | 746 | 0.516 | 0.556 | 0.422 | **+0.757** | [+0.533,+0.916] | strong + → WIDEN-ONLY (thin n) |

Colour: **P(home wins | home leads at half) = 0.776 (NBA), 0.818 (WNBA)** — matches
the "NBA fav covers both ~70%" prior (a half-time lead is worth ~77-82% of the
game). **Sign check vs prior — PASS** (1H×FG strong POSITIVE). NBA n is solid
(3,488 games) with tight CIs; WNBA is directionally identical but n=746 → wide CIs,
so WIDEN-ONLY. Only 3 NBA / 4 WNBA PBP seasons downloaded (no era split run for the
1H pairs) — keep width modest until more seasons or a live Kalshi 1H book exists.

---

## Era stability (held-out-season check, pairs 1 & 2, ALL players)

| league | pair | seasons < 2017 | seasons ≥ 2017 | drift |
|--------|------|----------------|----------------|-------|
| NBA | points × total | +0.144 (n=150,559) | +0.141 (n=152,842) | −0.003 (flat) |
| NBA | points × win | +0.092 (n=154,281) | +0.087 (n=155,505) | −0.005 (flat) |
| WNBA | points × total | +0.163 (n=19,711) | +0.154 (n=18,664) | −0.009 (flat) |
| WNBA | points × win | +0.105 (n=20,322) | +0.106 (n=19,089) | +0.001 (flat) |

Both pairs are essentially era-constant across the 3PT-revolution split — like the
shipped game-level NBA `ml|total` (drift +0.008), the player-prop correlations
survived the era shift. This is the only genuine OOS check available (no per-game
devigged prop marginals offline to run the gold conditional-MLE log-loss gate).

---

## Recommended `pair_rho` priors (for central reconciliation — NOT applied; hard rule 2)

None of these keys exist in `config.py` today (nba/wnba tables carry only
`moneyline|total = 0.01` and `moneyline|moneyline = −0.95`). All player-prop pairs
are **WIDEN-ONLY**: (a) no offline per-game devigged prop closing lines to run the
SHIP-grade conditional-MLE OOS gate, and (b) player rows cluster within games. The
1H pairs are game-level and OOS-adjacent (era-stable analog) but lack a live 1H
book, so SHIP-candidate at modest width.

| SGP pair (proposed key) | league | proposed rho | band | verdict | basis |
|-------------------------|--------|--------------|------|---------|-------|
| `player_points\|total` | NBA | **+0.23** (usage-scaled: ~0.14 role → ~0.29 elite) | ±0.12 | WIDEN-ONLY | 303k player-games, era-stable |
| `player_points\|total` | WNBA | **+0.28** (star), ~0.16 all | ±0.15 | WIDEN-ONLY | 38k player-games |
| `player_points\|moneyline` | NBA | **+0.10** near-50/50 ML → **+0.20** lopsided ML | ±0.12 | WIDEN-ONLY | 310k; regime-dependent on margin |
| `player_points\|moneyline` | WNBA | **+0.11** all, ~0.21 mid-usage | ±0.15 | WIDEN-ONLY | 39k player-games |
| `1h_total\|total` | NBA | **+0.75** | ±0.10 | SHIP-candidate (widen) | 3,294 games, PBP-validated |
| `1h_margin\|moneyline` | NBA | **+0.67** | ±0.10 | SHIP-candidate (widen) | 3,488 games |
| `1h_total\|total` | WNBA | **+0.73** | ±0.15 | WIDEN-ONLY | 702 games (thin) |
| `1h_margin\|moneyline` | WNBA | **+0.76** | ±0.15 | WIDEN-ONLY | 746 games (thin) |

**Frame reminders (a mixup silently flips the sign):** "total"/"win" are the
prop-player's OWN game total / OWN team win; the margin pair is home-frame
(home leads at half → home wins). Player-points is **usage-scaled** — do not ship
one flat rho; the role-player value (~0.14) and the star value (~0.29) differ by
2×, and SGP props are quoted on the star end. Points×moneyline is
**margin-regime-scaled** — low for a coin-flip game, ~2× higher for an expected
blowout, and the favorite's-star over-leg carries garbage-time downside on the
marginal itself.

---

## Cross-check vs peers

`ls docs/calibration/` at write time: `PLAN.md`, `results_baseball.md`,
`results_ising.md`, `results_soccer.md`, and this file.

**vs `results_baseball.md` — the direct structural twin (player-event × team-win /
team-total).** Baseball explicitly predicted the ordering for whoever landed next:
"player-scoring-event × own-team-win a solid positive, player event × own-team-total
the *stronger* positive — mirrored by HR×win (+0.23) < HR×team-total (+0.37)."
- **Ordering CONFIRMED in basketball:** points×total (+0.14 all / +0.26 star) >
  points×win (+0.09 all / +0.14 star). Same sign, same "total beats win" ordering. ✓
- **Magnitude weaker, as the priors demanded:** basketball points×win +0.09-0.14
  vs baseball HR×win +0.23; points×total +0.14-0.26 vs HR×total +0.37. NBA player
  pairs are the WEAKEST of the three sports' analogs — "usage cannibalization"
  (10 players share the total, and a median-crossing is a low bar), exactly the
  PLAN's basketball caveat. Baseball's one-batter HR is a discrete rare decisive
  event (P(A)=0.107); basketball's points-over is a coin flip (P(A)=0.507) that
  each barely moves the aggregate. Different marginal structure, same signs. ✓
- Both agents share the **within-game clustering caveat** and both report a
  cluster-floor CI; both are WIDEN-ONLY pending the conditional-MLE OOS gate.

**vs `results_soccer.md` — the first-half twin + the player-scorer analog.**
- **1H × FG:** soccer 1H-lead×FT-win **+0.71** (P(win|lead) 76.7%); my NBA
  1H-margin×FG-win **+0.665** (P(win|lead) 77.6%), WNBA +0.757 (81.8%). Nearly
  identical regime across sports — a half-time lead is worth ~77-82% of the game
  everywhere; basketball's rho is a touch lower than soccer's (basketball's
  higher-scoring second half regresses leads more than soccer's low-goal game). ✓
- **1H total × FT total:** soccer +0.69-0.77; my NBA +0.756 / WNBA +0.734 — same
  strong-positive band. ✓
- **Player scorer×win / scorer×over:** soccer star **+0.49 / +0.46** (conditional).
  My basketball points×win / points×total star **+0.14 / +0.26** — same signs,
  roughly HALF the soccer magnitude. Soccer's "scoring" is a rare decisive event
  (a single goal often is the winning margin AND a big share of a ~2.7-goal total);
  basketball's median-crossing is routine and dilutes across the roster. Coherent
  nesting, no sign conflict.

**vs `results_ising.md` — methodological.** The Ising agent proves the copula rho
and the Ising `W_ij` are two coordinates on the *same* pair-joint (2-leg exact to
1e-12). Since I invert that same shipped copula, every basketball rho here is
directly expressible as an Ising weight — my strong 1H×FG +0.75 sits near the
soccer `btts×over2.5` +0.75 → `W≈+2.4` the Ising fit reproduced; the weak
points×win +0.09 maps to a small positive `W`. Both files flag the shared blind
spot: **pairwise-only joints omit true 3-way+ dependence** — directly relevant to
a 3-leg basketball SGP (star points-over × team total-over × team win are all
positively linked and will carry residual positive 3-way mass neither model
prices), reinforcing WIDEN-ONLY on multi-leg player combos.

**Nesting sanity across all peers (all via the same shipped copula):**
`1H×FG (+0.66-0.76)` > `soccer star scorer×win (+0.49)` > `MLB HR×total (+0.37)` >
`NBA star points×total (+0.26)` > `NBA star points×win (+0.14)` > `NBA role
points×win (+0.09)`. Monotone in "how decisive is the single event vs the shared
pool" — no contradictions, no sign flips.

## NEXT STEPS

- **Runs next:** nothing auto-runs. `tools/calibrate_nba_player_points.py` and
  `tools/calibrate_nba_first_half.py` reproduce every number on demand
  (`.venv/Scripts/python.exe`). Player box + PBP already cached in `data/history/`.
- **Owner (operator):** decide whether to load, under sign-off, (a)
  `player_points|total` and `player_points|moneyline` as **usage-scaled**
  WIDEN-ONLY priors (NOT one flat scalar — role ≈0.14, star ≈0.26/0.29), (b)
  `player_points|moneyline` as **margin-regime-scaled** (≈0.10 even ML → ≈0.20
  lopsided), and (c) the NBA 1H pairs (+0.75 / +0.67) as SHIP-candidates once a
  Kalshi 1H book exists; keep WNBA 1H WIDEN-ONLY (n≤746).
- **To promote WIDEN-ONLY → SHIP:** run the gold conditional-MLE OOS log-loss gate
  once per-game devigged player-prop closing marginals exist (Kalshi prod-shadow
  settlements, Phase 6). Add more NBA/WNBA PBP seasons (blobs API, cheap) to run an
  era split on the 1H pairs and tighten the WNBA CIs.
- **Decisions owed by user:** (1) confirm the prop-line convention — Kalshi posts a
  fixed points line; the season-median proxy stands in for a ~50/50 line, so re-run
  against posted lines when a book exists to match the traded shape. (2) Accept the
  garbage-time shading on a heavy-favorite star's points-over marginal (the leg
  itself, separate from the correlation). (3) Confirm whether player-prop SGPs are
  in scope at all (config currently has zero player-prop keys for NBA/WNBA).
```
