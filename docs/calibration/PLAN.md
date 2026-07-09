# Correlation Calibration Run — 2026-07-06

Shared coordination board for the parallel calibration agents. **Read this first.**
Operator directive: calibrate the *uncalibrated* SGP correlation pairs (player props,
first-half/period, cross-prop) from FREE ONLINE data, because our own Kalshi tape only
goes back to 2025 and is too short to calibrate on. Marginals come live from Kalshi leg
books at quote time — we are calibrating ONLY the joint/correlation layer.

## ABSOLUTE HARD RULES (non-negotiable)

1. **NEVER access, read, cd into, glob, or even reference any path named `kalshi-combos`
   (WITHOUT the `-TWO` suffix).** Work ONLY inside `C:\Users\aahys\kalshi-combos-TWO`.
   This is a hard boundary re-emphasized by the operator. If a path lacks `-TWO`, stop.
2. **Additive only.** Do NOT modify existing files in `src/`, `config/`, or existing
   `tools/` scripts. Do NOT run or alter the live pricer. Create NEW tool scripts, NEW
   data files, and your OWN results markdown. New `src/` files are allowed ONLY if they
   are not imported by existing code (so the 556-test suite stays green). Do NOT edit the
   pricer's `pair_rho`/config — instead REPORT proposed values for central reconciliation
   under operator sign-off.
3. **Env:** use the project venv. Run Python via
   `C:\Users\aahys\kalshi-combos-TWO\.venv\Scripts\python.exe` (Python 3.13; pandas 2.2,
   numpy 1.26, scipy 1.16 present) or `uv run python`.
4. Calibration lives in PROBABILITY space, so floats are fine here (the money-integer
   rule does not apply to calibration stats).
5. UNKNOWN/insufficient-data ⇒ recommend **WIDEN-ONLY or NO-QUOTE**, never a convenient
   default. Be conservative; do not overclaim. Cite every non-obvious number with a URL
   or the data file it came from.

## THE METHOD CONTRACT (mirror the repo's existing pipeline)

The repo already calibrates GAME-LEVEL pairs this way — copy the pattern exactly so your
output is a drop-in `pair_rho`:

- Rank 1 (empirical joint-frequency): over N historical games measure P(A), P(B),
  P(A∩B). See `tools/calibrate_pairs_from_history.py`.
- Rank 2 (Gaussian copula): invert the SAME copula the pricer runs to turn P(A∩B) into a
  copula rho — `from combomaker.pricing.copula import gaussian_copula_joint_prob`, then
  bisection (see `implied_rho()` in `tools/calibrate_pairs_from_history.py`). Report the
  99% CI (binomial SE on P(A∩B) pushed through the solver).
- Gold standard where per-game closing marginals exist: conditional-MLE rho fit on each
  game's OWN devigged closing-line marginals, then OOS-gated by log-loss vs independence
  on held-out seasons. See `tools/fit_conditional_rho.py`. A pair that does NOT beat
  independence OOS ⇒ WIDEN-ONLY/NO-QUOTE, not ship.
- Rank 4 (Dixon-Coles, soccer only): `pricing/dixon_coles.py` already ships (dc_ρ=−0.05);
  the soccer agent extends it with a calibrated player-goal correlation (currently a
  hardcoded 0.50 prior).
- Rank 5 (Monte Carlo): DEFERRED to the later profitability sim — do not build now.

Output each pair as: `pair | n | P(A) | P(B) | P(A∩B) | rho | 99% CI | OOS verdict |
SHIP / WIDEN-ONLY / NO-QUOTE | source`.

## DATA INVENTORY (already in data/history/ — reuse, do not re-download)

- Soccer club: `E0/D1/F1/I1/SP1-<season>.csv` (top-5 EU, 20/21–24/25). Have FTHG/FTAG/FTR
  AND **HTHG/HTAG/HTR** (halftime!) AND shots HS/AS/HST/AST AND closing odds (C-suffix,
  e.g. B365CH) AND O/U 2.5 odds. NO player-scorer data.
- Soccer intl: `intl_results.csv`. MLB: Retrosheet game logs `gl2015..gl2025.txt`
  (team-level per-game; NO player events). `mlb-odds-*.xlsx` (2015–2021).
- NBA/WNBA: hoopR **team** box parquet `nba_team_box_2016..2026.parquet`,
  `wnba_team_box_2019..2026.parquet`. `nba_elo.csv` (538). NFL: `nfl_games.csv` (nflverse,
  with Vegas lines + overtime flag).
- Our thin Kalshi tape: `kalshi_{mlb,nba,wnba}_history.csv` (+ spreads) — too short to
  calibrate; use only as a sanity cross-check.

## NEW DATA TO FETCH (per your domain) — probed reachable 2026-07-06

- Soccer player scorers: **Understat** (https://understat.com, top-5 leagues 2014/15+,
  HTTP 200) and **StatsBomb open-data** (https://raw.githubusercontent.com/statsbomb/open-data,
  HTTP 200). Save into data/history/.
- NBA/WNBA player box: use the SAME static source as the existing team-box parquet
  (sportsdataverse/hoopR-data github releases — find the player_box release path;
  the guessed `.../releases/download/nba/player_box_2024.parquet` returned 404, so
  discover the correct tag/path). **DO NOT use live stats.nba.com — confirmed IP-blocked
  (HTTP 000) from this environment.**
- MLB player HR/K: **Retrosheet event files** (https://www.retrosheet.org/events/<year>eve.zip,
  HTTP 200) — play-by-play with batter HR and pitcher K. Or pybaseball/Statcast if easier.

## COORDINATION PROTOCOL ("check in on each other")

- Read THIS file first.
- Write results ONLY to your own file: `docs/calibration/results_<domain>.md`
  (soccer / basketball / baseball / ising). Do NOT edit peers' files (avoids write races).
- Near the end, `ls docs/calibration/` and READ any peer `results_*.md` that exist; add a
  short "Cross-check vs peers" note (e.g. does your ml|total soccer rho match the existing
  game-level calibration? do the Ising pairwise weights reproduce the copula joints?).
- Sign values must agree with the research priors: scorer×win strong +, scorer×over2.5
  weaker +, pitcher-K×total NEGATIVE, 1H×FG strong +, HR×team-total strong +. A sign flip
  vs prior ⇒ investigate before reporting (likely a frame/label bug).
</content>
</invoke>
