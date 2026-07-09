# STAGED — MLB prop classification + pair table (2026-07-09, NOT applied)

Produced by the multi-agent classification pass (12 agents; docs+API+tape+code,
triple-verified ρ). **Promotion gate (CLAUDE.md rule 8): the measurement tranche
must complete + tape backtest must pass, THEN port + parity-check.** Both blocks
ship TOGETHER — classification alone is pricing-neutral (typed-untabled ==
UNKNOWN == +0.60/0.90, sgp.py:457-460 vs :655-658).

Adversarial verification: correctness 21 CONFIRMED / 0 REFUTED / 1 UNCERTAIN
(F3/F7 3-way structure only); staged-keyword simulation over all 11,305 series =
exactly 11 diffs, all intended, ZERO false positives.

## The 9 combo-eligible families (exhaustive — verified vs all 1,387 MVE collections)

| family | LegType | status today | tape (400k early sample) |
|---|---|---|---|
| KXMLBGAME | moneyline (existing) | verified-existing | 29447 |
| KXMLBTOTAL | total (existing) | verified-existing | 13097 |
| KXMLBSPREAD | spread (existing) | verified-existing | 7181 |
| KXMLBKS | player_ks | missing-add | 3863 |
| KXMLBHIT | player_hit | missing-add | 2652 |
| KXMLBHR | player_hr | missing-add | 1504 |
| KXMLBHRR | player_hrr | missing-add | 1062 |
| KXMLBTB | player_tb | missing-add | 413 |
| KXMLBRFI | rfi | missing-add | 239 |
| KXMLBTEAMTOTAL | team_total (existing, classify-only) | verified-existing | 0 |
| KXMLBEXTRAS | extras (existing) | verified-existing | 0 |
| KXMLBF5 / KXMLBF3 / KXMLBF7 | unknown (correct fail-safe; future f5_moneyline if ever combo-eligible) | verified-existing | 0 |
| KXMLBRBI / KXMLBSB / KXMLBOUTS | unknown (deliberate — no keyword staged) | verified-existing | 0 |
| Futures/awards/roster (KXMLB, KXMLBAL/NL, 6 divisions, KXMLBWINS-<TEAM> x30, PLAYOFFS, BEST/WORSTRECORD, W/LSTREAK, all awards, GG/SS, ALLSTAR, draft, TRADE/DEBUT/NEXTTEAM/COACHOUT, STAT/STATCOUNT/TEAMSTAT, SEASONHR, NEXTHR, SISTREAK, FASTPITCH) | unknown (correct fail-safe) | verified-existing | 0 |

## Staged code (verbatim from the pass)

```
########################################################################
# STAGED — NOT APPLIED. Promotion path per CLAUDE.md rule 8: prototype in a
# tools/ backtest against the recorded tape, run the Retrosheet measurement
# TODOs below, pass the backtest gate, THEN port + parity-check.
# Both blocks must ship TOGETHER: adding LegTypes without table entries is
# byte-identical to today (typed-untabled pair == UNKNOWN pair == +0.60/0.90,
# sgp.py:457-460 vs :655-658); table entries without classification never fire.
########################################################################

# ============ src/combomaker/pricing/legtypes.py — class LegType ============
# (append after FIRST_HALF_SPREAD, before UNKNOWN)

    # MLB per-game player props (combo-eligible per KXMVESPORTSMULTIGAMEEXTENDED-R
    # / KXMVECROSSCATEGORY-R, 2026-07-09). Ticker line suffix -N means N+
    # (floor_strike = N-0.5) — NOT the N-0.5 "over" convention of TOTAL/SPREAD.
    PLAYER_HR = "player_hr"    # batter home runs N+ (KXMLBHR; -1 = 'to hit a HR')
    PLAYER_HIT = "player_hit"  # batter hits N+ (KXMLBHIT)
    PLAYER_KS = "player_ks"    # starting-pitcher strikeouts N+ (KXMLBKS)
    PLAYER_TB = "player_tb"    # batter total bases N+ (KXMLBTB)
    # Combined hits+runs+RBIs (KXMLBHRR, MLBHITSRUNSRBIS.pdf). NOT a home-run
    # market — and 'MLBHRR' contains 'MLBHR', so its keyword MUST precede MLBHR.
    PLAYER_HRR = "player_hrr"
    # Run in the FIRST INNING by either team (KXMLBRFI). Dedicated type: it
    # settles on a first-inning window (never a game TOTAL), and the market
    # ticker has NO outcome suffix (KXMLBRFI-<gamecode> is the full ticker).
    RFI = "rfi"

# ============ src/combomaker/pricing/legtypes.py — _KEYWORDS ============
# INSERT this block BETWEEN ("TEAMTOTAL", LegType.TEAM_TOTAL) and
# ("TOTAL", LegType.TOTAL). Placement is LOAD-BEARING: the F5TOTAL /
# SERIESGAMETOTAL / F5SPREAD blockers must precede TOTAL and SPREAD.
# SOURCE OF TRUTH (full 11,305-series universe scan, job 24844262,
# 2026-07-09): bare "HR"/"KS"/"HIT"/"TB"/"RFI" collide with 64/67/9/128/10
# series (KXANTHROPICRISK, KXLEADERNFLSACKS, KXDANAWHITEFB, KXBILBASKETBALL,
# KXSINNERFINISH, ...) — so every MLB prop keyword is MLB-anchored, and
# UNKNOWN-mapped blocker entries kill the known superstring traps (same
# precede-the-superstring pattern as TEAMTOTAL/TCORNERS).
    # --- blockers (explicit UNKNOWN = widen, never masquerade) ---
    ("LEADERMLB", LegType.UNKNOWN),        # KXLEADERMLB{HR,HITS,KS,...} season leaders
    ("MLBHRDERBY", LegType.UNKNOWN),       # KXMLBHRDERBY[QUAL] — contains 'MLBHR'
    ("SERIESGAMETOTAL", LegType.UNKNOWN),  # KXMLBSERIESGAMETOTAL = series game COUNT,
                                           # was mis-typing as full-game TOTAL (live bug)
    ("F5TOTAL", LegType.UNKNOWN),          # KX{MLB,WBC}F5TOTAL = first-5-innings total,
                                           # was mis-typing as full-game TOTAL (live bug:
                                           # 'F5' evades _PERIOD_SERIES)
    ("F5SPREAD", LegType.UNKNOWN),         # KX{MLB,WBC}F5SPREAD — was mis-typing as SPREAD
    # --- MLB player props + RFI (universe-verified unique hit sets) ---
    ("MLBHRR", LegType.PLAYER_HRR),        # MUST precede MLBHR (contains it)
    ("MLBHR", LegType.PLAYER_HR),
    ("MLBHIT", LegType.PLAYER_HIT),
    ("MLBKS", LegType.PLAYER_KS),
    ("MLBTB", LegType.PLAYER_TB),
    ("MLBRFI", LegType.RFI),
# NOT staged on purpose: RBI/SB/OUTS/TEAMTOTAL/EXTRAS/F3/F5/F7 are NOT in either
# MLB-bearing combo collection (374 events each, verified 2026-07-09) — RBI/SB/
# OUTS stay UNKNOWN (safe) rather than adding collision-prone keywords. WBC/KBO
# props (KXWBCHR, KXKBORFI, ...) intentionally unmapped (dormant, widen-safe).

# ============ src/combomaker/ops/config.py — pair_rho_by_sport["mlb"] ============
# (additions to the existing 4-entry table; keys are legtypes.pair_key sorted)
        "mlb": {
            "moneyline|total": -0.05,      # existing
            "extras|total": 0.10,          # existing (family not combo-eligible today)
            "extras|moneyline": -0.04,     # existing (same)
            "moneyline|moneyline": -0.95,  # existing (defensive; Kalshi blocks same-event dups)
            # ---- MEASURED, WIDEN-ONLY (results_baseball.md + cluster-boot
            # rederivation, Retrosheet 2015-2025, job 24844262) ----
            # Starter K over x GAME total over: -0.2522 cluster-boot 99%
            # [-0.2709,-0.2304]; recent era (2021-25) -0.2242. Orientation-free.
            # Replaces a SIGN-WRONG flat +0.60.
            "player_ks|total": -0.25,
            # ---- MEASURED but ORIENTATION-NEUTRALIZED. The measured values are
            # team-oriented: K-over x pitcher's-OWN-team-win +0.24, HR x batter's-
            # OWN-team-win +0.23 — exact sign flip when the ML leg is the OPPONENT
            # (2-way market complement). sgp.py has NO team-orientation resolver
            # for MLB (soccer :same/:opp resolvers are pair-type-specific; the
            # one-moneyline fav/dog axis is the WRONG axis here). Until a resolver
            # compares the prop ticker's team prefix (e.g. ...-BOSRSUREZ55-5 ->
            # BOS) against the ML suffix (-BOS), ship 0.00 with a band spanning
            # both signs — strictly better than +0.60/0.90 (point error <=0.24
            # vs up to 0.84, band still contains truth).
            "moneyline|player_ks": 0.00,   # RESOLVE-ORIENTATION-BEFORE-PROMOTE (+/-0.24)
            "moneyline|player_hr": 0.00,   # RESOLVE-ORIENTATION-BEFORE-PROMOTE (+/-0.23)
            # ---- LABELED PRIORS (unmeasured; replace the known-wrong flat +0.60;
            # every one MEASURE-BEFORE-PROMOTE — all derivable from the already-
            # parsed data/history/mlb_parsed_{batter,starter}_games.csv.gz) ----
            # HR x GAME total: NOT the measured +0.367 (that is HR x OWN-TEAM
            # total = KXMLBTEAMTOTAL frame, which is NOT combo-eligible). Own-team
            # half of the game total dilutes it; opp runs ~orthogonal.
            "player_hr|total": 0.25,       # DERIVED PRIOR — MEASURE (top TODO)
            "player_hit|total": 0.15,      # LABELED PRIOR — MEASURE
            "player_hrr|total": 0.30,      # LABELED PRIOR (runs/RBIs are total components)
            "player_tb|total": 0.25,       # LABELED PRIOR — MEASURE
            "moneyline|player_hit": 0.00,  # LABELED PRIOR, orientation-dependent
            "player_hit|player_ks": 0.00,  # LABELED PRIOR, orientation-dependent
                                           # (batter FACING pitcher: negative)
            # same-family baskets (the 8-9-leg all-NO HR basket is a signature
            # tape shape; flat +0.6 pairwise inflates P(all-no) -> overbid):
            "player_hr|player_hr": 0.05,   # LABELED PRIOR (soccer player_goal
                                           # |player_goal measured 0.03/0.05 precedent)
            "player_ks|player_ks": 0.00,   # LABELED PRIOR (opposing starters)
            "player_hit|player_hit": 0.10, # LABELED PRIOR (shared run environment)
            "player_hrr|player_hrr": 0.10, # LABELED PRIOR
            "player_tb|player_tb": 0.10,   # LABELED PRIOR
            # RFI (first-inning window):
            "rfi|total": 0.25,             # LABELED PRIOR (inning-1 run counts toward total)
            "moneyline|rfi": 0.00,         # LABELED PRIOR (either-team RFI ~⊥ winner)
            # TODO(measure, game-level): "moneyline|spread" — structural declines
            # ML+runline (no total leg) and this falls to flat +0.6 documented
            # 'badly wrong' (config.py:804-812, OOS 1.12151 vs 1.00824);
            # "spread|total" copula fallback for structural declines.
        },

# ============ src/combomaker/ops/config.py — pair_rho_uncertainty ============
        "mlb:player_ks|total": 0.12,        # spans era drift -0.28..-0.22 (WIDEN-ONLY)
        "mlb:moneyline|player_ks": 0.30,    # spans +/-0.24 orientation
        "mlb:moneyline|player_hr": 0.28,    # spans +/-0.23 orientation
        "mlb:player_hr|total": 0.20,        # derived prior, wide
        "mlb:player_hit|total": 0.20,
        "mlb:player_hrr|total": 0.20,
        "mlb:player_tb|total": 0.20,
        "mlb:moneyline|player_hit": 0.25,
        "mlb:player_hit|player_ks": 0.25,
        "mlb:player_hr|player_hr": 0.15,
        "mlb:player_ks|player_ks": 0.20,
        "mlb:player_hit|player_hit": 0.20,
        "mlb:player_hrr|player_hrr": 0.20,
        "mlb:player_tb|player_tb": 0.20,
        "mlb:rfi|total": 0.20,
        "mlb:moneyline|rfi": 0.15,

# DO NOT STAGE: total|player_hr or any 'team_total|player_*' loading of the
# measured +0.367 / -0.380 — those are TEAM-total-frame values and KXMLBTEAMTOTAL
# is untradeable in combos; loading them onto the GAME-total pair silently
# mis-signs/mis-scales (results_baseball.md:116-118).
```

## Key resolutions

- SIGN-WRONG PRICING LIVE TODAY: KXMLBKS/HIT/HR/HRR/TB/RFI all classify UNKNOWN (verified by running src/combomaker/pricing/legtypes.py), so every same-game pair containing them prices point rho +0.60 band 0.90; measured truth for player_ks|total is -0.2522 (cluster-boot [-0.2709,-0.2304]) — the widened corr_low (-0.30) barely contains it and the point is wrong by ~0.85. In the 400k tape sample alone: 793 same-game KSxTOTAL, 1,603 GAMExKS, 1,033 GAMExHIT combos took this path.

- TEAM-vs-GAME TOTAL (the mission's critical question) RESOLVED FROM API RULES TEXT: KXMLBTOTAL = GAME total ('If Milwaukee and Pittsburgh collectively score more 8.5 runs [sic, missing than]'); KXMLBTEAMTOTAL = TEAM total ('If Pittsburgh scores 8+ runs'); KXMLBF5TOTAL = GAME total over first 5 innings. KXMLBTEAMTOTAL is NOT combo-eligible (absent from both MLB-bearing MVE collections, 374 events each) -> the calibration's two strongest player rhos (HR x own-team-total +0.367, K x opp-team-total -0.380) attach to an UNTRADEABLE leg family. The pair that must actually be measured is player_hr x GAME-total (KXMLBHR x KXMLBTOTAL) — a one-line variant in tools/calibrate_mlb_player_props.py (B = game total > season median; the starter-K analysis already computes exactly this B leg). player_ks x GAME-total is already measured (-0.25) and tradeable.

- SUBSTRING TRAP QUANTIFIED (why the staged keywords are MLB-anchored): scanning all 11,305 series in series_raw.json, bare 'HR' hits 64 series (KXANTHROPICRISK, KXBATHROOM, KXBIRTHRIGHT, KXLEADERMLBHR...), 'KS' 67 (KXLEADERNFLSACKS, KXCHOPSTICKS, KXDATABRICKS...), 'HIT' 9 (KXDANAWHITEFB, KXLEADERMLBHITS...), 'TB' 128 (KXBILBASKETBALL, KX3MTBILL...), 'RFI' 10 (KXSINNERFINISH, KXKBORFI...). MLB-anchored hit sets are clean: MLBHRR->1, MLBHIT->2 (needs LEADERMLB blocker for KXLEADERMLBHITS), MLBKS->2 (LEADERMLB blocker), MLBHR->5 (needs MLBHRR + MLBHRDERBY blockers), MLBTB->1, MLBRFI->1.

- TWO LIVE MISCLASSIFICATIONS FOUND by executing the classifier (latent — families not combo-eligible today): KXMLBF5TOTAL -> LegType.TOTAL and KXMLBF5SPREAD -> LegType.SPREAD (first-5-innings settlement window masquerading as full-game; 'F5' evades _PERIOD_SERIES at legtypes.py:83), and KXMLBSERIESGAMETOTAL -> LegType.TOTAL (a series game-COUNT market typed as a runs total). UNKNOWN-blockers staged; if Kalshi ever adds F5 families to a collection these become live wrong-settlement-window bugs.

- KXMLBHRR RESOLVED (tape scan had flagged it unknown): combined hits+runs+RBIs ('If Spencer Torkelson records 2+ total hits + runs + rbis', MLBHITSRUNSRBIS.pdf) — NOT a home-run variant despite identical ticker grammar to KXMLBHR on the same players; 'MLBHRR' contains 'MLBHR' so keyword order MLBHRR-before-MLBHR is load-bearing.

- ORIENTATION GAP: the measured ML x prop rhos are team-oriented (+0.24 K/own-team-win, +0.23 HR/own-team-win; exact sign flip for opponent ML) but sgp.py has no team-identity orientation resolver for MLB — soccer's :same/:opp resolvers are pair-type-specific and the one-moneyline fav/dog axis (marginal-based) is the wrong axis. Staged entries are orientation-neutralized (0.00, sign-spanning bands). Building the resolver is cheap (prop ticker embeds the player's team code, ML suffix embeds the team) and unlocks ~0.24 of signed rho on 3,191 same-game GAMExprop combos per 2.7% sample window.

- KALSHI COMBO-VALIDITY RULE (empirical, useful for quoting): duplicate same-event market legs are rejected — 0 same-game pairs within GAME/TOTAL/SPREAD/RFI across 40,684 MLB combos (vs 18,550/5,326/1,708/80 cross-game) — while same-game cross-family and multi-player same-family stacking is abundant. The shipped mlb moneyline|moneyline -0.95 same-event entry is defensive/unreachable.

- TAPE SAMPLE COVERAGE: the 400k-row scan covers only the first ~2.7% of the 12,498,135-row window (early Jul 6 UTC) — pair counts are lower bounds and pairs below the top-30 cutoff (e.g. GAMExHR) certainly exist; absolute counts understate ~30x. Family MIX and the zero-same-event finding are robust (0 exceptions in 40,684 combos).

- SPORT-FILTER LEAKS VERIFIED LIVE: classify_sport types KXEWCMLBB (Mobile Legends esports) and KXMLBMENTION (broadcast Mentions) as Sport.MLB via the 'MLB' substring; KXMEDIACOVERMLBTHESHOW additionally returns is_period_leg=True ('SH' inside 'THESHOW'). Plus KXATTENDMLB/KXMLBSTRIKE/KXMLBCBA/KXMLBOAK/KXESPYMLB carry 'MLB'. Any MLB whitelist must gate on the explicit family list, never classify_sport alone.

- GAME-LEVEL GAPS UNCHANGED BY THIS WORK (flagged for the measurement queue): mlb moneyline|spread same-game (459 in sample) falls to flat +0.6 that config.py:804-812 itself documents as 'badly wrong' (OOS 1.12151 vs structural 1.00824) because structural declines margin-only combos; spread|total copula fallback likewise unmeasured. Both derivable from the same Retrosheet game logs as the shipped -0.05.

- PRICING-NEUTRALITY OF CLASSIFICATION-ONLY SHIP CONFIRMED from sgp.py semantics: a typed pair with no table entry produces numerically identical matrices to an UNKNOWN pair (+0.60 point, [-0.30,+0.95] clamped band; only the note string differs, sgp.py:457-460 vs :655-658) and the structural path is unaffected (only MONEYLINE/TOTAL/SPREAD representable). So the staged legtypes.py block alone changes zero prices — classification + table entries must be promoted together, and none of the staged types collide with global pair_rho keys (no accidental soccer-prior activation, unlike typing HR as PLAYER_GOAL would).

- MEASUREMENT QUEUE IMPLIED (all from existing parsed artifacts data/history/mlb_parsed_batter_games.csv.gz / mlb_parsed_starter_games.csv.gz + game logs, no new downloads): (1) HR x GAME-total-over [replaces the derived 0.25], (2) teammate + opponent HR x HR / HIT x HIT / KS x KS same-game [de-risks the all-NO basket overbid], (3) HIT x win and HIT x K-facing-pitcher orientation split, (4) win x runline-cover for moneyline|spread, (5) RFI x total / RFI x ML from inning-1 PBP. Then the conditional-MLE OOS log-loss gate (results_baseball.md NEXT STEPS) before any WIDEN-ONLY -> SHIP promotion; operator still owes the starter-K line-convention decision (self season-median vs Kalshi posted line — Kalshi DOES post explicit K lines per API, e.g. 7+/8+ strikes, so a re-run against posted-line-style fixed lines is warranted).

- SCRATCH ARTIFACTS (all under C:/Users/aahys/.claude/jobs/24844262/tmp/mlb/, no repo files touched, no processes started/stopped, prod DB not opened by this agent): verify_classification.py (live classifier run + universe collision scan), scan_400k.json (tape pair matrix used here), results.json + era_stability.json (cluster-boot rho rederivation), series_raw.json (11,305-series universe), samples_raw.json / mlb_family_rules*.json (rules text), mvec.json (collection membership).

