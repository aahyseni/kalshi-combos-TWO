# JUDGE VERDICT — soccer & baseball module overviews
**Date:** 2026-07-11 · **Judge inputs:** repo `C:\Users\aahys\kalshi-combos-TWO` @ main d65bb6e (read-only), executed config/classifier/conditional table via `uv run`, persisted probe matrices (`docs/calibration/containment_probe/`), tape caches (`C:\Users\aahys\.claude\jobs\24844262\tmp\{ph4,containment_probe,mlb}`), ground-truth fixtures, NOTES.md, dated reports, one fresh public Kalshi API call.
**Method:** never judged a report against itself; ~18 independent re-derivations per report (tables re-executed, tape artifacts re-aggregated from raw rows, classifier live-run, one live API probe); universes diffed source-by-source. Verification scripts + dumps persisted under `tmp\overview\judge\`.

---

## 1. RE-DERIVATIONS PERFORMED (the hunt log)

| # | check | method | soccer | baseball |
|---|---|---|---|---|
| 1 | pair-table counts + zero orphans | executed `CorrelationConfig` | 96/96 exact | 165/165 exact |
| 2 | every table row's ρ/band | byte-diff report tables vs executed config | 61 spot values, 0 mismatches; all 96 keys present (8 via brace shorthand) | **all 165 rows parsed from the report — 0 value/band mismatches, 0 missing, 0 extra** |
| 3 | orientation curve knots | executed config | 5 knots byte-exact | n/a |
| 4 | conditional table | executed `SAME_PLAYER_CONDITIONALS` | n/a | 149 cells = 40 exact + 109 measured; **all 149 report cells match value (±5e-5), n, and exact-marker**; MIN_N=50k, hit≥3 n=48,375, band 0.12, KS absent from BATTER_FAMILIES — all exact |
| 5 | classifier live-run | `uv run` on 37 tickers | 13 WC series + FIRSTGOAL→player_goal + 2HTOTAL→unknown + TEAMTOTAL→team_total + UCL→soccer all reproduce | all 9 families + F5/SERIESGAMETOTAL/LEADERMLB/HRDERBY→unknown + EWCMLBB/MENTION→Sport.MLB + THESHOW is_period=True + WBC/KBO unknown all reproduce |
| 6 | tape universe | `tape_universe.json` raw | all 17 legs_seen counts exact; line universes (WCTOTAL 1..8, UCLTOTAL 2..5 no over-0.5, 1HSPREAD line-2-only, GOAL rungs 1..3, 4-segment GOAL) exact | 9 KXMLB series on tape = exactly the 9 claimed |
| 7 | collections snapshot | `collections.json` | the 2 open collections admit exactly the 13 WC families claimed | KXMVEMLB dead confirmed (only 2 collections exist); prod.yaml has no whitelist |
| 8 | WC disposition histogram | `containment_frequency.json` | 21,968/723,167; 21,723 ok/230 unk/13 cont/2 imp; 227 = 127+70+29+1 exact | — |
| 9 | WC per-print accuracy | recomputed from all 656,555 rows of `wc_backtest_perprint.json` | median 1.57¢ / mean 1.98¢ / bias −1.72¢ / w2 62.1% / w5 95.1% — **all reproduce to the printed digit** | — |
| 10 | composition census | recomputed from raw rows | **775 distinct comps; top-20 = 562,159 rows, remainder 755 comps/94,396 rows — exact; all 20 counts and medians reproduce** | all 11 headline compositions (n, promoted-w2, legacy-w2) reproduce from `mlb_by_composition.json` |
| 11 | MLB funnel + paths | `gather_meta.json`, `mlb_backtest.json` | — | 16,053,982→852,940→201,780→7,737→6,969 + all named remainders exact; path histogram 6,337 copula / 224 structural / 408 declined→copula recomputed exact; every decline reason = "not representable"; unknown_carrying=0 in analyze.log |
| 12 | headline accuracy | `ph4_stats.json` | per-combo table (1.60¢/58.1% + all slices) exact | 0.34¢/93.3% + all 6 buckets + per-print 53,022/98.6%/92.0% exact |
| 13 | Phase-2 wiring | parsed `phase2_wire_list.txt` + addendum | — | **102 entries diffed vs executed config: 0 mismatches — "wired VERBATIM" proven** |
| 14 | exchange matrix | `exchange_matrix.json` | verdict counts 59 ALLOWED / 118 BLOCKED / 17 UNPROBEABLE exact | S33/S34/S35/S41/S42/S43 cells match the report row-for-row (incl. S33-ny 4 window prints, S34-yn analog F04, S42-yn UNPROBEABLE) |
| 15 | tripwire fixture | fixture JSON + live classifier probe | 50 cells / 28 shape ids exact; id list matches | live probe: HIT/TB/HR/HRR/**KS** same-stat ladder impossible mixes ALL decline IMPOSSIBLE farmable=False via tripwire note |
| 16 | live API | fresh public GET 2026-07-11 | KXWC1H-26JUL14FRAESP open, exp 07-14T22:00Z, close_time far-future 07-28 (P3-4 confirmed); KXWC1HGAME returns **zero markets** | not re-probed (P3-1 = NOTES + 07-10 report, text-confirmed) |
| 17 | config params | config.py + both YAMLs | whitelist, UCL prefixes, 4.5h/KXMLB-4.0h, 0.6/0.0 rho defaults, DC params, farm params, longshot floor, sell-only both envs — all exact | k=3.54/band 0.30, DO-6 250cc, disagreement 0.08, last-look 2.0s/150cc/200cc — all exact |
| 18 | settlement P&L sources | dated reports | +$6,660/+3.05¢, +$8,728@1¢/48.1%/1.3%→14.8%, Brier 0.0400, 107,781-ct UCL outlier — all in source | n=5,604/11.07M ct, −$1.23M, 41,363/41,363 audit — in source; **one number misattributed (finding F3)** |

Symmetry statement: both reports received the same rubric and an equal number of spot checks. The findings below are roughly symmetric; where lopsided (F3 numeric slip is MLB-only, F4 audit gap is soccer-only) the evidence, not attention, is lopsided — every numeric layer I re-derived in both reports otherwise reproduced exactly.

---

## 2. PER-SECTION VERDICT GRID

### Soccer report (`soccer_module_overview.md`)

| § | verdict | evidence |
|---|---|---|
| 0 pipeline/precedence | **CONFIRMED** | code order read at relationships.py:443–1059 / engine.py:159–272 matches step-for-step; tripwire placement + immediate-return semantics verified |
| 1.1 ticker universe | **CONFIRMED** | all 17 tape counts exact; line universes exact; 13 families = collections.json admission list; live API re-run reproduces both claims |
| 1.2 parse rules | **CONFIRMED** | `_GAME_CODE` regex, end-anchored `_team_of`, player 4→2 fragment, draw tokens, MLB-gated `:rN` all read in source |
| 1.3 legtypes/keywords | **CONFIRMED** | live classifier run reproduces every claimed typing incl. period overlay and UNKNOWN blockers |
| 1.4 traps | **CONFIRMED** | FIRSTGOAL→player_goal reproduced live; KXWC1HGAME nonexistence re-confirmed against live API |
| 1.5 grouping | **CONFIRMED** | `_game_key` read; mlb_event_conventions fixture matches; L10/L11 history consistent with NOTES |
| 1.6 whitelist/gates | **CONFIRMED w/ OMISSION** | all config values exact; but the "what it admits" enumeration omits the crypto flow the same collections admit (finding F1) |
| 2 marginals | **CONFIRMED** | legs.py/engine.py/config values verified; devig quarantine per CLAUDE.md #8 |
| 3.1 96-entry table | **CONFIRMED** | executed-config diff: zero omissions, zero mismatches; 11+29+10+46 grouping sums verified |
| 3.2 curve | **CONFIRMED** | knots byte-exact |
| 3.3 resolvers/fallback | **CONFIRMED** | resolver inventory matches sgp.py; 0.6/+0.30 widening and cross-event 0.0 executed |
| 3.4 containment families | **CONFIRMED (citation nit)** | family logic + farmable flags + window wiring verified in code; **but the {A no,B yes}→window mapping is attributed to `_containment_sign` (rel.py:264–285), which actually returns None there — windows are wired at each call site** (F7) |
| 3.5 structural DC | **CONFIRMED** | config 1304–1353 exact; OOS numbers match NOTES I9 table verbatim |
| 3.6 UNKNOWN/width/farm | **CONFIRMED** | engine + config verified; sell-only choke point + $1.00 settlement fixture |
| 4.1 universe/dispositions | **CONFIRMED** | every histogram recomputed exactly |
| 4.2 print accounting | **CONFIRMED** | 723,167 identity sums exactly; buckets named in capstone |
| 4.3 accuracy | **CONFIRMED** (blast-radius/parity text-confirmed, not re-run) | per-print stats recomputed from raw rows to the digit; 19,016 bit-identical + 2,656-key parity are UNVERIFIABLE-BY-RERUN here (multi-hour), source named and consistent |
| 4.4 compositions | **CONFIRMED** | 775/562,159/94,396 and all 20 rows recomputed exactly |
| 4.5 settlement P&L | **CONFIRMED** | all numbers found in 2026-07-08 sources incl. the UCL Brier fix |
| 5.1 constructible/blocked | **CONFIRMED** | 59/118/17 recomputed; mechanisms match taxonomy cells |
| 5.2 settlement pins | **CONFIRMED** | robustness §4 rows match; `market_rules.json` absence from repo verified (job-tmp only) |
| 5.3 tripwire coverage | **PART-REFUTED** | fixture = 50 cells/28 ids ✓, but "pins the full 30-cell dangerous class" is false: **29 of 30 pinned; S49 (25.0¢ ref fair) is absent from the fixture** — the section's own S49-residual sentence contradicts its headline (F2) |
| 5.4 validator exposure | **PART-REFUTED** | "all 30 are fixture-pinned" — same S49 counterexample; remainder (tighten-safe, no-mint trace) confirmed |
| 6 holes | **CONFIRMED w/ OMISSIONS** | all 20 holes trace to sources; missing: crypto flow (F1), soccer-keyword collision audit (F4), per-sport kill-switch gap (F5) |

### Baseball report (`baseball_module_overview.md`)

| § | verdict | evidence |
|---|---|---|
| pipeline diagram | **CONFIRMED** | matches classifier/engine order read in source |
| 1.1 universe | **CONFIRMED** | 9 families = collections + tape; per-family counts match staged_mlb_props 400k table exactly; TEAMTOTAL/EXTRAS/F5/leaks all reproduce live |
| 1.2 parse rules | **CONFIRMED** | game-code/blob/DH/rung logic read in source; trap counts (64/67/9/128/10 of 11,305) match the persisted universe scan |
| 1.3 legtypes/blockers | **CONFIRMED** | keyword order + all blockers reproduced by live run |
| 1.4 grouping/events | **CONFIRMED** | 2-segment prop events + DO-7 fixture (KXMLBGAME true, 8× false, 24/24 provenance) verified |
| 1.5 whitelist | **CONFIRMED w/ OMISSION** | demo 6 prefixes + prod-no-whitelist verified in YAMLs; KXMVEMLB dead confirmed; crypto admission omitted (F1) |
| 1.6 pregame | **CONFIRMED** | ET-token chain + 4.0h override + P3-1..4 verified in config/pregame.py/NOTES |
| 2 marginals | **CONFIRMED** | same primitives as soccer, verified once |
| 3.0 precedence | **CONFIRMED** | 12-step order matches relationships.py |
| 3.1 165-entry table | **CONFIRMED** | **all 165 rows byte-match the executed config; zero orphans both directions** |
| 3.2 resolvers/fallback | **CONFIRMED** | chain + band-at-same-level + no-interpolation verified in sgp.py; "no same-game MLB pair hits flat" scoped to MLB-typed pairs (foreign legs excluded, correctly disclosed in §6) |
| 3.3 conditional table | **CONFIRMED** | full 149-cell re-execution: every value/n/marker matches; WIRE-2 tb→hrr1 cells exact |
| 3.4 containment/collapse | **CONFIRMED** | farmable=False on every MLB impossible verified at each return site; FIX-1 numbers (0.4183/0.3451/+7.32¢) match the judge-fixes source |
| 3.5 tripwire | **CONFIRMED for S42 / PART-REFUTED for "the 30"** | live probe: all five same-stat ladders (incl. KS) decline via tripwire; **"pins the 30 ... cells" overstates — S49 unpinned** (F2); S42K/S50b engine-matrix variants unmentioned (F6) |
| 3.6 mlb_runs | **CONFIRMED** | k=3.54/0.30 executed; K6/K8/K9 gate numbers match NOTES verbatim; 5.53¢-vs-1.10¢ open item honestly carried |
| 3.7 UNKNOWN behavior | **CONFIRMED** | engine + FIX-4 live-path note verified |
| 3.8 quote construction | **CONFIRMED** | DO-6 predicate + 250cc + sell-only + width core verified |
| 4 tape/capstone | **CONFIRMED except one number** | funnel/paths/headline/compositions/key-cells all recomputed or source-matched; **"props-only ... (+$13,452 at 0¢ / +$6,907 at 1¢, YES-hit 2.9%)" is wrong: source says YES-hit 2.8% at 0¢ and 19% at 1¢; 2.9% is the *actual makers'* props YES-hit from a different table** (F3) |
| 5.1 exchange cells | **CONFIRMED** | matches exchange_matrix.json cell-for-cell incl. MLB-unprobeable caveat |
| 5.2 settlement pins | **CONFIRMED** | robustness §4 + dnp doc + scorecard rows verified |
| 5.3 tripwire/validator | **PART-REFUTED** | "30 dangerous cells — ALL now covered by the shipped tripwire" — S49 counterexample (F2); tighten/loosen/no-mint verdicts confirmed |
| 6 holes | **CONFIRMED w/ OMISSIONS** | all 20 holes trace; crypto absent from the foreign-leg inventory (F1); engine-matrix variant rows unmentioned (F6) |
| NEXT STEPS | **CONFIRMED** | matches capstone/judge-fixes footers |

---

## 3. CONSOLIDATED FINDINGS — ranked by money-at-risk

**F1 (BOTH reports — biggest omission). Crypto flow through the whitelisted collections is documented nowhere.**
Primary-source facts (all verified here): the two OPEN collections admit 10 crypto series (`KXBTC15M/KXBTCD/KXETH15M/KXETHD/KXSOL15M/KXSOLD/KXXRP15M/KXXRPD/KXDOGE15M/KXDOGED` — collections.json snapshot); the RFQ tape shows ~352k crypto legs in the scan window (tape_universe.json: KXBTC15M 76,459 … KXDOGED 2,592); `taxonomy.json meta.board_now` itself lists "crypto (10, no shapes)" among sports with combo legs; `classify_leg`/`classify_sport` return unknown/unknown (live-run); filters.py contains **no sport gate**, and UNKNOWN *typing* prices (flat 0.6 same-game / independence cross-game) rather than declining — a fact both reports state and neither connects to crypto. Same-event daily-crypto strike ladders are containment-shaped truth (the S45/WNBA-PTS analog) with **no taxonomy shape, no tripwire pin, and no constructibility probe**; nobody has verified crypto events carry size_max=1. Mitigations that cap the exposure (also unstated): 15M series die on `min_time_to_close_s=3600`; daily series are quotable only >4.5h before close; sell-only caps the loss direction. Given the operator's standing "sports only / crypto was the toxic flow" history, a module overview whose §1.6/"what the whitelist admits" section misses an entire admitted asset class is a coverage defect in both reports.

**F2 (BOTH reports). "All 30 dangerous cells pinned" is refuted — 29/30; S49 is unpinned, and both reports contradict themselves about it.**
The fixture holds 50 cells across 28 shape ids — **S49 is not among them** (verified by enumeration); tripwire.py's own docstring records S49 as the known residual. Yet soccer §5.3 says "pins the full 30-cell dangerous class" and §5.4 "all 30 are fixture-pinned"; baseball §5.3 says "ALL now covered by the shipped tripwire". S49 is row #2 of the dangerous table (25.0¢ ref fair, metadata-blocked, priced at cross_event_rho=0.0 if the validator loosens), and both tennis leg families (KXATP-26WIM tournament + KXATPMATCH) are live in the whitelisted collections **today** — the only protection is Kalshi's own `is_yes_only` flag. Related mis-framing carried by both reports from the robustness doc: with `sell_parlays_only=true` in both envs (yes_bid forced 0), an impossible-mix combo that "prices" cannot produce the "certain loss of the full quoted bid" — the certain-loss direction requires buying YES; under the shipped config the residual exposure is the farm path + lost alarm value, not certain loss. Neither report notices the interaction.

**F3 (Baseball report). Settlement-sweep YES-hit misattributed — understates prop-book toxicity at the recommended thin markup.**
Report §4: "props-only structurally profitable at every markup 0-3¢ (+$13,452 at 0¢ / +$6,907 at 1¢, YES-hit 2.9%)". Source (2026-07-10-mlb-settlement-pnl-sweep.md): YES-hit is **2.8% at 0¢ and 19% at 1¢**; 2.9% is the actual-makers' number from a different table. Since the soccer module's own conclusion is that ~1¢ markup is the operating point, quoting 2.9% where the truth is 19% materially understates adverse selection on the MLB prop book at that markup. REFUTED numeric detail.

**F4 (Soccer report — asymmetry). No soccer-keyword collision audit; the surface is real.**
The baseball report quantifies its sport/keyword collision class from the persisted 11,305-series universe (64/67/9/128/10 hits; EWCMLBB/MENTION/THESHOW leaks) and states the "family list, never classify_sport" rule. The soccer report lists its 8 sport keywords with no equivalent audit. Re-running the same persisted universe: **89 series classify Sport.SOCCER by substring** — including `KXCLUBWC*` (Club World Cup, incl. `KXCLUBWCGAME`, a real soccer family that would type moneyline/soccer and inherit WC priors while *not* matching `knockout_series=["KXWC"]`), the whole `KXEWC*` esports-World-Cup family, `KXUAEPLGAME` (via EPL), `KXBBSERIEAGAME` (via SERIEA), `KXNEWCOACH*`, `KXNEWCOVIDCASE`. Also unmentioned: the KXWC prefix universe holds 125 series (KXWC2H*, KXWCSCORE, KXWCGOALCOMBO→player_goal, KXWCTOTALGOAL→total, tournament-scope series whose short event suffixes could collide in `_game_key`). None are in the open collections today, so this is exchange-listing risk (rule-6 seam), not live mispricing — but the report's §1 "universe" claims completeness without this audit, which its sister report performed.

**F5 (Cross-report asymmetries).**
(a) Baseball quantifies mixed flow (52.69% of MLB combos carry foreign legs; KXWNBAPTS #1 gap); soccer never states the foreign-leg share of WC-carrying combos — its §4 is pure-WC only, with mixed flow appearing solely via one capstone watch-list cell. (b) Baseball hole #16 records "no per-sport pricing kill switch"; the soccer report omits this ops gap although it binds soccer identically. (c) Baseball's §5.1 carries the honest "MLB probes blocked by finalized demo events — analogs substitute" caveat; the soccer report's §5.1 does not remind the reader that several soccer ALLOWED/BLOCKED cells also rest on analog-not-direct evidence with the validator having tightened Jul-07→11 (it does carry the shelf-life caveat, so this is minor).

**F6 (BOTH reports, minor). Persisted engine matrix carries 53 shape keys (S3L + S42K + S50b variants) and an internal contradiction neither report surfaces.**
engine_matrix.json's S42K (KS ladder) and S50b (golf top20⇒make-cut) appear in neither report's shape inventory; engine_matrix marks S42/S42K yes+no `"constructible": true, blocked_by: null` while exchange_matrix (the authoritative artifact both reports cite) says UNPROBEABLE-lean-BLOCKED. Mitigation verified live: the shipped tripwire declines all five same-stat ladder families including KS. Residual: a reader auditing the persisted matrices meets an unexplained conflict.

**F7 (Soccer report, nit). §3.4 attributes the {A no, B yes}→exact-window mapping to `_containment_sign` (rel.py:264–285); the matrix actually returns None there ("possible — falls to the copula") and each family's call site wires the window into `cont_bands`.** Behavior as described; citation imprecise. Also §3.4's S7/S8/S13 row correctly notes windows NOT wired for spread×total — internally consistent.

**Zero unexplained residuals found:** every "N of M" I tested decomposes exactly (723,167 print identity; 227 = 127+70+29+1; 245-combo post-merge accounting; 6,969 funnel remainders; 775-comp census 562,159+94,396; 165/165; 149 = 40+109; 194 = 59+118+17; 50 cells = 28 ids). No "other/etc." bucket appears in either report.

---

## 4. IF I HAD TO BET AGAINST THIS MODULE TOMORROW

**Soccer:** I'd attack the *allowed* (non-blocked) KXWCFIRSTGOAL/NOGOAL side-mixes and the dense unmeasured-[P] prop stacks — quote-me NOGOAL-YES × deep-total-NO (goal-forbidding, so not tripwire-pinned) where the engine prices a containment-shaped truth with the *sign-wrong anytime-scorer* +0.46/+0.55 priors, and hammer the ADVANCE+CORNERS+PLAYER_GOAL/BTTS stacks whose med |err| is already 3.0–4.0¢ on [P]-class priors (corners|advance, btts|advance, pgoal|total) — the report knows the family is approximate but the allowed mixes still price today.

**Baseball:** I'd attack player heterogeneity in the pooled same-player conditional table — pick extreme batters (elite-HR or slap-hitter profiles) and RFQ bare HIT×HR / TB×HRR pairs where the pooled conditional (e.g. P(HR1|hit≥1)=0.172) is 2× off for the individual and the 0.12 transfer band is asserted, not measured (report hole #6) — plus the illiquid HRR/HIT prop-only parlays already flagged at 1.5–2.0¢ bias, and anything riding the un-runged `:same` aggregates (hit r4, hrr r1) — all quotable today at 1¢ markup where the true prop YES-hit is 19%, not the 2.9% the report printed (F3).

---

## APPENDIX — judge artifacts
`tmp\overview\judge\`: dump_tables.py / tables_dump.json (executed config), diff_mlb_table.py (165/165 byte-diff), diff_soccer_table.py + spot_soccer.py (96-key coverage + 61 values), check_conditionals.py + check_cond_values.py (149-cell re-execution), run_classifier.py (37-ticker live run), check_probe/check_shapes/check_shape_coverage (matrices + fixture), check_tape*.py (tape universe), check_wc*.py + check_stats.py (WC histograms + per-print recompute), check_mlb*.py + check_comp.py (funnel/paths/stats/compositions), check_wirelist.py (102-entry verbatim proof), s42k_probe.py (live ladder tripwire probe), misc_checks.py; live API GET (KXWC1H / KXWC1HGAME) run 2026-07-11.
