# SOCCER MODULE — COMPLETE OVERVIEW
**Repo:** `C:\Users\aahys\kalshi-combos-TWO` @ `main d65bb6e` ("Merge branch 'containment-collapse'" — containment collapse + 5 wires + judge fixes + tripwire LIVE; suite 1239/0) ⟦git log d65bb6e..f7f97b9⟧
**Date:** 2026-07-11 · **Scope:** World Cup (`KXWC*`) live family; UCL/UEL/UECL gated-off status; soccer entries in the global tables.
**Audience:** operator + hostile judge. Every claim carries a primary-source tag ⟦…⟧. Untagged = defect; report it.

Conventions used below: `config.py` = `src/combomaker/ops/config.py`; other bare filenames are under `src/combomaker/pricing/` or `src/combomaker/rfq/` as stated; "probe dir" = `docs/calibration/containment_probe/`; "job-tmp" = `C:\Users\aahys\.claude\jobs\24844262\tmp\`. All ρ are YES–YES copula correlations; the copula sign-flips NO legs downstream ⟦sgp.py:5-7⟧.

---

## 0. THE PIPELINE AT A GLANCE (precedence — what fires FIRST)

```
RFQ (exchange-minted combo market only — no other entry point) ⟦job-tmp ph4/wire2/kalshi_robustness.md §1⟧
  │
  ▼
FILTERS (rfq/filters.py) ── kill-switch → whitelist → leg-count → UCL gate → size
  │                         → sides-known → feed-health → leg books → close-time
  │                         → PREGAME gate (any started/unknown-start leg ⇒ decline)
  ▼
ENGINE.price (engine.py:159-272)
  1. classify_legs (relationships.py:443) ──────────────────────────────┐
  │    a. unknown side ⇒ UNKNOWN                 (rel.py:449-454)       │
  │    b. same market both sides ⇒ IMPOSSIBLE-farm (rel.py:459-473)    │ ANY IMPOSSIBLE
  │    c. mutual-exclusion (per event) ⇒ IMPOSSIBLE, not farmable      │ returns
  │       (rel.py:487-500)                                              │ IMMEDIATELY
  │    d. nested corner ladders: impossible/containment/band            │ and beats
  │       (rel.py:524-575)                                              │ everything
  │    e. [MLB same-player / ml|spread families — not soccer]           │ below it
  │    f. SOCCER spread⟹win, scope-matched (rel.py:754-793)            │
  │    g. spread-total impossibility S7/S8/S13 (rel.py:810-847)         │
  │    h. containment families 1/2/3 (rel.py:864-974)                   │
  │    i. TAXONOMY TRIPWIRE — pinned exchange-blocked impossibles       │
  │       (rel.py:976-993, tripwire.py)                                 │
  │    j. bare 2-leg containment / N-leg collapse / NESTED_BAND / OK ◄──┘
  2. IMPOSSIBLE+farmable ⇒ farm quote (yes_bid=0)  (engine.py:171-180)
  3. UNKNOWN ⇒ decline skip_classifier_unknown      (engine.py:181-182)
  4. beliefs (Kalshi books; missing ⇒ decline)      (engine.py:184-186, 297-323)
  5. CONTAINMENT bare pair ⇒ price_containment      (engine.py:191-198)
     CONTAINMENT collapse / NESTED_BAND ⇒ _price_nested_bands (engine.py:199-221)
  6. STRUCTURAL Dixon-Coles (if applicable)          (engine.py:222-227)
  7. copula (build_sgp_correlation + price_joint_matrices)  (engine.py:228-238)
  8. longshot floor → grid/fees/width/free-money caps → SELL-ONLY choke point
     (engine.py:239-272, 274-295)
```
⟦engine.py:159-272⟧ ⟦relationships.py:443-1059⟧ — the ordering above is the literal code order; classification always precedes pricing, IMPOSSIBLE always beats CONTAINMENT/UNKNOWN/OK (each impossible mix `return`s immediately ⟦relationships.py:854-862⟧), and the tripwire runs AFTER every shipped family "so the shipped families keep their own (sometimes farmable) verdicts" ⟦tripwire.py:23-25⟧.

---

## 1. INTAKE

### 1.1 The ticker/series universe (tape + persisted taxonomy + live API)

Tape source: prod RFQ tape `data/combomaker-prod.sqlite3`, 18.03M RFQ rows scanned 2026-07-06→07-11T15:14Z (full scan ≥07-09, 1/13 sample before; 9,413,543 rows used) ⟦probe dir taxonomy.json meta.sources.tape; job-tmp containment_probe/tape_universe.json scan⟧. **13 live WC series** ⟦taxonomy.json meta.board_now: "soccer WC (13 series)"⟧, leg counts from the tape universe ⟦job-tmp containment_probe/tape_universe.json series⟧:

| series | tape legs seen | ticker segments | lines observed | LegType (live-run, §1.3) |
|---|---|---|---|---|
| `KXWCADVANCE` | 9,358,321 — **the single most common soccer leg** | 3 | — | `advance` |
| `KXWCGOAL` | 6,953,863 | **4** (only 4-segment WC series) | player rungs 1..3 ⟦taxonomy meta key_line_facts⟧ | `player_goal` |
| `KXWCTOTAL` | 2,675,523 | 3 | 1..8 on tape (1=over-0.5 exists; live open 1..6) | `total` |
| `KXWCCORNERS` | 1,675,466 | 3 | 7..12 | `corners` |
| `KXWCBTTS` | 1,491,143 | 3 | — | `btts` |
| `KXWCGAME` | 1,320,634 | 3 | suffix TEAM or TIE | `moneyline` |
| `KXWCTCORNERS` | 810,710 | 3 | TEAM+line 3..10 concatenated | `corners_team` |
| `KXWCSPREAD` | 395,878 | 3 | TEAM+line 2..4 (live 2..3) | `spread` |
| `KXWC1H` | 331,071 | 3 | suffix TEAM or TIE | `first_half_moneyline` |
| `KXWC1HTOTAL` | 309,248 | 3 | 1..4 | `first_half_total` |
| `KXWCFIRSTGOAL` | 63,765 | 3 | per-player + `NOGOAL` (new series that week) | `player_goal` (**trap — §1.4**) |
| `KXWC1HSPREAD` | 51,496 | 3 | TEAM+line, **line 2 only** | `first_half_spread` |
| `KXWC1HBTTS` | 44,553 | 3 | — | `first_half_btts` |

UCL on tape (gated off, §1.6): `KXUCLGAME` 7,403 / `KXUCLTOTAL` 4,407 (lines 2..5 — **no over-0.5 rung**, unlike KXWC) / `KXUCLBTTS` 897 / `KXUCLSPREAD` 712 (TEAM+2..3) ⟦tape_universe.json⟧ ⟦taxonomy.json S1.line_condition: "LIVE on KXWC (open lines 1..6); DEAD on KXUCL"⟧. Taxonomy counts UCL as "4 series, GAME/TOTAL/SPREAD/BTTS only" ⟦taxonomy.json meta.board_now⟧.

Live-API spot check (this overview, 2026-07-11, public unauthenticated GET): `KXWCGAME-26JUL14FRAESP` open with `event_ticker=KXWCGAME-26JUL14FRAESP` (2-segment SERIES-GAMECODE), `expected_expiration_time=2026-07-14T22:00:00Z`, far-future event-level `close_time=2026-07-28T19:00:00Z`; `KXWC1H-26JUL14FRAESP` also live ⟦live API GET /markets?series_ticker=KXWCGAME|KXWC1H&status=open, 2026-07-11⟧ — confirms (a) the bare-`KXWC1H` winner series exists (NOT `KXWC1HGAME`, which does not exist ⟦legtypes.py:124-127⟧ ⟦memory feedback_kalshi_source_of_truth⟧), (b) soccer `close_time` is NOT a start anchor (P3-4) ⟦NOTES.md P3-4⟧.

### 1.2 Parse rules

- **Game code / team blob:** segment 2 matches `^\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z0-9]{4,})$` — date + optional 4-digit time + concatenated team codes ⟦structural.py:123⟧. Team codes vary in length, so teams resolve by **end-anchoring**: a code prefixing the blob = Team.A, suffixing = Team.B, both-or-neither **refuses** ⟦structural.py:163-170⟧. Player codes prefix their team code (`FRAKMBAPP10`); resolved by longest leading fragment (4→2) that anchors one end ⟦structural.py:173-180⟧ ⟦NOTES.md I8 live-confirmed `-ARGLMESSI10-2`, `FRAKMBAPP10`⟧.
- **Blob order is AWAY+HOME** (doc-verified live 2026-07-06: NBA `26MAY23NYKCLE` = "New York AT Cleveland") — the frame lives in ONE place for margin-total sports ⟦NOTES.md L1⟧; DC soccer is orientation-inverted from the legs themselves so no home-frame assumption is load-bearing there ⟦structural.py:348-401⟧.
- **Totals:** suffix `3` → ≥3; `2.5` → ≥3; anything else refuses ⟦structural.py:183-189⟧. Integer `-N` = "over N−0.5" is DOC-VERIFIED from live metadata 2026-07-06 ⟦NOTES.md K2⟧ ⟦relationships.py:141-146⟧. Over-0.5 ≡ line `N==1` ⟦relationships.py:146⟧.
- **Spread:** suffix `([A-Z]+?)(\d+)` = TEAM wins by over N−0.5, regulation ⟦structural.py:233-246⟧, convention doc-verified via sister series KXMLBSPREAD/KXNFLSPREAD live metadata ⟦structural.py:233-236⟧ ⟦NOTES.md K2⟧.
- **Corners:** match ladder suffix = bare digits; team ladder = TEAM+digits (`…-POR8` → ("POR", 8)) ⟦relationships.py:121-132, 165-179⟧; `strike_type=greater_or_equal`, all rungs settle on ONE combined count "regulation, stoppage and any extra time periods" — rules verified 2026-07-09 ⟦relationships.py:184-189⟧ ⟦taxonomy.json S30.line_condition⟧.
- **Draw tokens:** `TIE`/`DRAW` shared vocabulary across relationships/sgp/structural/tripwire ⟦relationships.py:151⟧ ⟦sgp.py:85⟧ ⟦structural.py:124⟧ ⟦tripwire.py:70⟧.
- **Rungs (`:rN` grammar) are MLB-only** — soccer spread lines are never runged in copula lookup ⟦sgp.py:1084-1091 "MLB-gated: ':rN' is a wire-list / mlb-table grammar"⟧.

### 1.3 LegType + classification keywords (all of them)

Keyword→type checked in order on the SERIES prefix ⟦legtypes.py:66-115⟧: `GOAL`→player_goal, `BTTS`→btts, `TEAMTOTAL`→team_total (must precede TOTAL), MLB blocker block (`LEADERMLB`/`MLBHRDERBY`/`SERIESGAMETOTAL`/`F5TOTAL`/`F5SPREAD`→UNKNOWN), `MLBHRR`→player_hrr (precedes `MLBHR`), `MLBHR`/`MLBHIT`/`MLBKS`/`MLBTB`/`MLBRFI`, `TOTAL`→total, `TCORNERS`→corners_team (precedes `CORNERS`), `CORNERS`→corners, `ADVANCE`→advance, `EXTRAS`→extras, `SPREAD`→spread, `FIGHT`/`GAME`/`MATCH`→moneyline. Period overlay: series matching `1H|2H|H1|H2|FH|SH|[1-4]Q|Q[1-4]|QTR|HALF|PERIOD` ⟦legtypes.py:121⟧ maps first-half members via `_FIRST_HALF_MAP` (moneyline/total/btts/spread only) ⟦legtypes.py:132-137⟧; bare 1H winner (series ends in the half token) → first_half_moneyline ⟦legtypes.py:127, 164-165⟧; **any other period (2H, quarters) → UNKNOWN, never full-game** ⟦legtypes.py:167⟧. Sport: `WC`/`UCL`/`MLS`/`EPL`/`BRASILEIRO`/`LALIGA`/`SERIEA`/`BUNDESLIGA` → soccer ⟦legtypes.py:190-211⟧.

Live-run verification (this overview, `uv run` on the shipped classifier, 2026-07-11): all 13 WC series classify as the table in §1.1 shows; `KXWC2HTOTAL-…-1` → UNKNOWN + period=True; `KXWCTEAMTOTAL-…-FRA2` → team_total; `KXUCLGAME` → moneyline/soccer ⟦live run of legtypes.classify_leg/classify_sport, 2026-07-11⟧.

**UNKNOWN blockers relevant to soccer:** every 2H/quarter series; any unmapped 1H family ("unmeasured 1H×FT pairs must widen, never guess" ⟦legtypes.py:160-166⟧). UNKNOWN typing never blocks alone — it falls to the flat same-event prior with a band widened to |0.6|+0.30 so corr_low spans zero ⟦legtypes.py:6-8⟧ ⟦sgp.py:798-807⟧.

### 1.4 Known traps (each verified)

1. **`KXWCFIRSTGOAL` (incl. its `NOGOAL` outcome) classifies `player_goal`** — the `GOAL` keyword matches first (verified by live run 2026-07-11 ⟦live run, §1.3⟧). Consequence: structural declines it (3-segment ticker fails the player parse, "player ticker too short" ⟦structural.py:222-223⟧), so it prices on player_goal copula priors — whose sign is actively WRONG for NOGOAL×goal-requiring pairs; those mixes are exchange-BLOCKED today and tripwire-pinned (S24, S26–S29) ⟦kalshi_robustness.md §2.2 rows 1,4,10-12⟧ ⟦tests/fixtures/ground_truth/taxonomy_impossible.json shapes S24/S26/S27/S28/S29⟧. See HOLES #5.
2. **`TCORNERS` before `CORNERS`, `TEAMTOTAL` before `TOTAL`** — superstring traps; source-of-truth tape 2026-07-07 ⟦legtypes.py:71-75, 101-104⟧.
3. **`KXWC1HGAME` does not exist** — the real 1H winner series is `KXWC1H` ⟦legtypes.py:124-127⟧, re-confirmed live 2026-07-11 (§1.1).
4. **Doubleheader `G<digit>`** blob suffix (MLB shape, stripped defensively for all sports) ⟦structural.py:126-140⟧.
5. **event_ticker is per-market-SERIES, not per-game** — grouping on it silently priced every same-game SGP at independence (the L10 CRITICAL bug, fixed with game-code grouping + 4 regression tests) ⟦NOTES.md L10⟧.

### 1.5 Event/game grouping conventions

- Game key = event_ticker after `SERIES-` (`KXWCGAME-26JUL05MEXENG` → `26JUL05MEXENG`); no hyphen ⇒ key on the whole string so it never merges ⟦relationships.py:288-305⟧.
- **Period legs DO rejoin the full-game block** (so 1H×FT correlations price); they are kept off the structural inverter by a guard in structural.py, not by grouping ⟦relationships.py:297-301⟧ ⟦structural.py:313-327⟧. (History: L11 had grouped them out entirely after mis-typing; the current design supersedes it ⟦NOTES.md L11⟧.)
- **2-segment prop events:** real prop event tickers are per-GAME 2-segment (`KXMLBHIT-26JUL111605COLSF` for a 4-segment market ticker) — tape-verified read-only 2026-07-11; the test helper that minted 3-segment per-player events was a poisoned fixture that HID the WIRE-4 counterexample and was fixed ⟦docs/reports/2026-07-11-judge-fixes-wire4-guard-tripwire.md FIX-2⟧. Soccer events observed 2-segment throughout (live spot check §1.1; player-goal legs verified to group live ⟦NOTES.md 2026-07-07 review, refuted list: "player-goal legs DO group (event_ticker is 2-segment, verified live)"⟧).
- Mutual-exclusion stays per-EVENT (home/draw/away of one moneyline event), from `GET /events/{ticker}.mutually_exclusive`; missing flag = UNKNOWN never False ⟦relationships.py:475-500⟧ ⟦NOTES.md D5⟧.

### 1.6 Collection whitelist + gates it admits through

`config/demo.yaml filters.collection_whitelist`: `KXMVESPORTS`, `KXMVECROSSCATEGORY`, `KXMVENBA`, `KXMVEMLB`, `KXMVENFL`, `KXMVENHL` (prefix match on `mve_collection_ticker`) ⟦config/demo.yaml⟧ ⟦filters.py:61-64⟧. The two OPEN collections on the exchange are `KXMVESPORTSMULTIGAMEEXTENDED-R` and `KXMVECROSSCATEGORY-R` ⟦taxonomy.json meta.sources.collections⟧ — soccer flow arrives through both (observed combo tickers are `KXMVECROSSCATEGORY-…` ⟦job-tmp ph4/wc/containment_frequency.json examples⟧). Note: whitelist gating is by COLLECTION not sport — UNKNOWN-typed legs do NOT decline at intake/filters; they price at the flat prior (verified on the live path during FIX-4) ⟦2026-07-11-judge-fixes report, FIX-4 bullet 2⟧. Other filter gates: `combos_only=true`, `min_legs=2/max_legs=6`, contracts 1–10,000, target-cost $1–$50,000, `max_leg_spread_cc=800`, `min_leg_depth_contracts=1.0`, `min_time_to_close_s=3600` ⟦config.py:73-83⟧.

**UCL/UEL/UECL gate:** `decline_two_legged_tie=True` ⟦config.py:84-88⟧; `_TWO_LEGGED_TIE_PREFIXES=("KXUCL","KXUEL","KXUECL")` → `SKIP_UNMODELED_REGIME` ⟦filters.py:28-32, 70-73⟧. Why: "advance" there is decided over TWO legs so single-match priors mis-apply ⟦config.py:84-87⟧; the 2026-07-08 backtest screen found the residual REAL mispricings were "almost all KXUCL" ⟦docs/reports/2026-07-08-soccer-backtest-vs-clearing.md §Screen⟧, and ONE UCL SPREAD+TOTAL combo (107,781 ct, our fair 77¢ vs clearing 23¢, settled NO) alone moved calibration Brier 0.23→0.04 when removed ⟦2026-07-08-soccer-settlement-pnl.md methodology-fix⟧. The advance|* priors are explicitly SINGLE-MATCH regime; two-legged is "a DIFFERENT regime (symmetric→0) — wire by ticker series when it lists" ⟦config.py:273-275⟧. NOT re-enabled ⟦docs/reports/README.md blocked/open gates⟧.

**Pregame gate (Phase 3, ACTIVE by default):** any leg with now ≥ start ⇒ `skip_inplay_leg`; UNKNOWN start ⇒ `skip_start_time_unknown`; re-checked at last look (`decline_inplay_leg`) ⟦pregame.py:1-40⟧ ⟦filters.py:88-102⟧. Soccer has NO verified embedded start (only `KXMLB` in `_EMBEDDED_START_SERIES` ⟦pregame.py:58⟧), so WC uses chain (b): min(close_time, expected_expiration) − **4.5h** default — deliberately larger than the backtests' 2.5h because measured WC expected_expiration lands 2.95–3.95h after kickoff, so 2.5h would admit ~1.5h of in-play ⟦config.py:102-108⟧ ⟦docs/reports/2026-07-10-phase3-pregame-gate.md⟧ ⟦NOTES.md P3-3⟧.

---

## 2. MARGINALS

- **Source:** each leg's marginal = microprice of the live Kalshi leg orderbook (`KalshiBookSource`), uncertainty = half-spread in prob space + 0.02 thin-penalty when either side's depth < 10 contracts ⟦legs.py:48-85⟧. Blended with any configured external sources at weight 1.0 (book) vs configured weight; blend uncertainty adds the inter-source spread ⟦legs.py:88-111⟧ ⟦engine.py:297-323⟧.
- **Freshness/health gates:** book must be `valid` and un-crossed or the source returns None ⟦legs.py:70-79⟧; feed health = WS traffic ≤30s + seq continuity AND book validity ⟦NOTES.md B9⟧; filter-level `SKIP_LEG_STALE`/`SKIP_LEG_BOOK_THIN`/`SKIP_LEG_SPREAD_TOO_WIDE` (spread > 800cc) ⟦filters.py:123-146⟧; last-look re-checks `max_leg_age_s=2.0`, `leg_move_tolerance_cc=150`, `joint_move_tolerance_cc=200` ⟦config.py:1441-1443⟧ ⟦NOTES.md E4⟧.
- **Devig scoping rule:** devig methods run ONLY inside external `OddsSource` adapters (`pricing/sources/`); Kalshi legs NEVER pass through devig — Kalshi binaries are vig-free by construction (yes+no=$1); enforced by an import-guard architecture test ⟦CLAUDE.md decision #8⟧ ⟦legs.py:5-7⟧. One Kalshi-side use of the normalization math: renormalizing a mutually-exclusive family whose mids don't sum to 100% (`normalize_exclusive_family`) ⟦CLAUDE.md decision #8⟧.
- **Missing/stale behavior:** missing book belief ⇒ `NoQuote(SKIP_PRICING_FAILED)`; sources disagreeing > `max_source_disagreement=0.08` ⇒ `NoQuote(SKIP_SOURCES_DISAGREE)` — never averaged away ⟦engine.py:297-323⟧ ⟦config.py:1426⟧. External adapter (SportsGameOdds) is OFF by default and has no soccer mapping ⟦config.py:1288-1301⟧.
- **Marginals are never taken from history** — history calibrates only the joint layer; who-wins always comes from live prices ⟦NOTES.md era-stability note⟧ ⟦docs/calibration/results_soccer.md header⟧.

---

## 3. THE JOINT LAYER, CELL BY CELL

### 3.1 The shipped soccer pair table — all 96 entries (zero omissions, zero orphan bands)

`config.py CorrelationConfig.pair_rho_by_sport["soccer"]` holds exactly **96 entries**, each with a `soccer:`-prefixed band override (96/96 — verified by executing the shipped config 2026-07-11: `len(...)==96` both sides) ⟦live run of CorrelationConfig, 2026-07-11⟧ ⟦config.py:187-480 (values), 880-993 (bands)⟧. Values/bands below are printed from the executing config, not transcribed. Provenance classes: **[M]** measured on 8,981–8,982 football-data.co.uk club matches (top-5 EU 20/21–24/25) ⟦config.py comments; docs/calibration/results_soccer.md⟧; **[U]** measured on Understat 3,652 matches ⟦results_soccer.md §2⟧; **[DC]** derived from the shipped Dixon-Coles model + external cross-checks ⟦config.py:258-296⟧; **[P]** labeled prior (grounded but unmeasured, wide band) ⟦config.py per-entry comments⟧; **[C]** near-deterministic containment clamp (±0.95).

**FT core (11 keys — incl. `moneyline|moneyline`):**
| pair key | ρ | band | provenance |
|---|---|---|---|
| `moneyline|total` | +0.28 | ±0.10 | [M] conditional-MLE on per-game closing lines, n=7,228 train, ρ+0.30 SE 0.019, beats independence OOS ⟦config.py:169-174, 880⟧ |
| `btts|total` | +0.70 | ±0.12 | [M] pooled +0.746 n=8,982; band widened pending conditional refit ⟦config.py:175, 881⟧ |
| `btts|moneyline` / `:fav` / `:dog` | −0.19 / −0.19 / 0.00 | ±0.10 each | [M] pooled; marginal-less fallback — the CURVE (§3.2) wins when marginals exist ⟦config.py:190-199, 882-884⟧ |
| `moneyline|player_goal` | +0.50 | ±0.12 | [U]+[DC] structural implied +0.51/+0.52 both worked examples; validated cond-MLE +0.49 [0.44,0.54] OOS-pass ⟦config.py:200, 885⟧ ⟦results_soccer.md §3⟧ |
| `btts|player_goal` | +0.55 | ±0.30 | [U] implied +0.549; band spans fav +0.31 ↔ dog +0.81 (no ML leg to orient) ⟦config.py:201-207, 886⟧ |
| `player_goal|total` | +0.46 | ±0.15 | [U] measured +0.46 (was global 0.40) ⟦config.py:208, 932⟧ |
| `player_goal|player_goal` | +0.03 | ±0.10 | [U] teammates ~0 (Poisson-split exact) / opponents +0.05 blend ⟦config.py:209, 933⟧; cond-MLE teammates ≈0.00 FAILS OOS — WIDEN-ONLY verdict ⟦results_soccer.md §2⟧ |
| `total|total` | +0.95 | ±0.04 | [C] nested thresholds measured at the cap ⟦config.py:210, 888⟧ |
| `moneyline|moneyline` | −0.95 | ±0.04 | [M] measured −0.99, P(both win)=0 ⟦config.py:146, 343, 879, 934⟧ |

**Corners cluster (29 keys):**
| pair key | ρ | band | provenance |
|---|---|---|---|
| `corners|total`, `corners|moneyline`, `corners|spread`, `btts|corners` | 0.00 ×4 | ±0.08 | [M] total corners ⊥ goals AND result (−0.04, +0.02, +0.01 measured) ⟦config.py:211-215, 889-892⟧ |
| `corners|first_half_{moneyline,total,btts,spread}` + `corners_team|first_half_{…}` | 0.00 ×8 | ±0.10 | [P grounded on the measured ⊥] — retired a flat-+0.6 hit on ~21k tape combos ⟦config.py:216-229, 894-901⟧ |
| `advance|corners` | 0.00 | ±0.15 | [P] advance = diluted moneyline × measured corners⊥result; replaced the +0.6 default that drove a 3,344-ct combo to 8.76¢ vs maker 5.60¢ ⟦config.py:230-241, 902⟧ |
| `corners|player_goal` | +0.05 | ±0.20 | [P] mirrors corners_team|player_goal ⟦config.py:242, 916⟧ |
| `corners|corners_team` | +0.62 | ±0.15 | [M] 2026-07-08, 8,981 matches, TWO independent passes; home +0.65/away +0.57, real tape lines 7-10×4-6; RFQ-test flagged pair ⟦config.py:243-255, 926⟧ ⟦2026-07-07-soccer-calibration-and-farming.md §1⟧ |
| `corners_team|moneyline` (+`:same`−0.15/`:opp`+0.15/`:tie` 0.00) | −0.15 plain | ±0.10/0.10/0.10/0.08 | [M] strength-controlled 2026-07-08 (raw pooled +0.05 is a Simpson trap, WRONG SIGN); conditional −0.154/+0.152/+0.014 ⟦config.py:297-314, 917-920⟧ |
| `corners_team|spread` (+`:same`−0.11/`:opp`+0.11) | −0.13 plain | ±0.10 | [M] strength-controlled (raw +0.07 wrong sign) ⟦config.py:315-323, 921-923⟧ |
| `corners_team|total`, `btts|corners_team` | 0.00 ×2 | ±0.08 | [M] ⊥ goals ⟦config.py:324-325, 924-925⟧ |
| `corners_team|corners_team` (+`:opp`−0.28/`:same`+0.90) | −0.28 plain | ±0.10 | [M] opposite-team re-measured −0.287/−0.283; same-team nested = exact containment approximated comonotone when buried ⟦config.py:326-340, 927-929⟧ |
| `corners_team|player_goal` | +0.05 | ±0.20 | [P] same-team-attack prior ⟦config.py:341, 930⟧ |
| `advance|corners_team` | −0.05 | ±0.15 | [P] diluted moneyline ⟦config.py:342, 931⟧ |

**Advance × FT + spread × FT (DC-derived cluster, 10 keys — incl. `advance|moneyline:tie`):**
| pair key | ρ | band | provenance |
|---|---|---|---|
| `advance|total` | +0.12 | ±0.15 | [DC] advance = ML attenuated by ~35% scoreline-decoupled shootouts; cross-checked vs 4 knockout studies (n=247/185/310/78), implied k=P(adv\|draw) 0.50→0.64 matches measured; LINE-STABLE incl. over-0.5 ⟦config.py:256-276, 905⟧ |
| `advance|btts` | −0.07 | ±0.13 | [DC] symmetric market retains ~½, stays negative like btts\|ml ⟦config.py:266-277, 906⟧ |
| `advance|player_goal:same`/`:opp` | +0.45/−0.45 | ±0.15 | [DC] directional retains ~0.8; sign resolved in sgp.py ⟦config.py:270-279, 907-908⟧ |
| `advance|spread` | +0.95 | ±0.10 | [DC] spread≥2 ⟹ win ⟹ advance near-containment ⟦config.py:272-280, 909⟧ |
| `spread|total` | +0.31 | ±0.20 | [DC] over-2.5 anchor (+0.22 even → +0.46 heavy-fav); over-1.5 side is containment, Kalshi-blocked ⟦config.py:281-293, 912⟧ |
| `btts|spread` | −0.30 | ±0.13 | [DC] clean 2-0 win → not-btts ⟦config.py:288-294, 913⟧ |
| `player_goal|spread:same`/`:opp` | +0.46/−0.42 | ±0.15 | [DC] resolved in sgp.py ⟦config.py:289-296, 914-915⟧ |
| `advance|moneyline:tie` | 0.00 | ±0.10 | draw is symmetric re: who advances; team cases are containment/impossible in relationships.py ⟦config.py:476-479, 993⟧ |

**1H × FT + 1H × 1H (46 keys — matched-family + 1H-spread + the 2026-07-08 cross-type cluster; 11+29+10+46 = 96 exactly):**
| pair key | ρ | band | provenance |
|---|---|---|---|
| `first_half_moneyline|moneyline:same`/`:opp` | +0.71/−0.67 | ±0.08 | [M] 2026-07-07, era-stable across a 2023 split (drift ≤0.047) ⟦config.py:344-354, 937-938⟧ ⟦results_soccer.md §1: +0.710 [+0.649,+0.766] home, +0.698 away⟧ |
| `first_half_total|total` | +0.73 | ±0.12 | [M] 1H o0.5×FT o2.5 +0.693 / 1H o1.5×o2.5 +0.765 / o1.5×o3.5 +0.722, all era-stable ⟦config.py:355, 939⟧ ⟦results_soccer.md §1⟧ |
| `btts|first_half_total` | +0.55 | ±0.13 | [M]+[DC] TWO independent methods agree ≤0.037 (structural +0.533/+0.544; empirical +0.570/+0.552), line-stable; RFQ-test flagged pair ⟦config.py:356-377, 940-944⟧ |
| `first_half_moneyline|first_half_total:team`/`:tie` | +0.95/−0.95 | ±0.10 | [C] lead⊂over (all 5,401 lead matches over → implied +0.99); 0-0⊂tie (all 2,518 under matches ties → −0.99); the SUICOL pick-off fix ⟦config.py:378-392, 947-948⟧ |
| `first_half_spread|spread:same`/`:opp` | +0.78/−0.65 | ±0.12/0.15 | [M] +0.777 [+0.726,+0.826]; :opp copula-fit −0.65 reproducing the observed ~0.2% near-exclusion (deliberately NOT clamped to −0.95 — outside the measured CI) ⟦config.py:393-425, 952-953⟧ ⟦results_soccer.md §2 cited therein⟧ |
| `first_half_spread|moneyline:same`/`:opp` | +0.74/−0.66 | ±0.12/0.15 | [M] +0.739 [+0.652,+0.854] / −0.662 [−0.709,−0.624] ⟦config.py:417-428, 954-955⟧ |
| `first_half_spread|total` | +0.52 | ±0.15 | [M] +0.518 [+0.418,+0.635] at the modal over-2.5 anchor; band absorbs FT-line dependence ⟦config.py:420-429, 956⟧ |
| `advance|first_half_moneyline:same`/`:opp`/`:tie` | +0.64/−0.64/0.00 | ±0.12/0.12/0.10 | [M+DC] 1H cross-type cluster, 3-agent batch 2026-07-08, structural-vs-empirical cross-validated ≤0.04, strength-controlled ⟦config.py:430-439, 958-960⟧ |
| `first_half_moneyline|total:team`/`:tie` | +0.24/−0.42 | ±0.08/0.10 | [M+DC] same batch ⟦config.py:440-441, 961-962⟧ |
| `first_half_moneyline|player_goal:same`/`:opp`/`:tie` | +0.45/−0.20/−0.22 | ±0.15/0.18/0.12 | [M+DC] same batch ⟦config.py:442-444, 963-965⟧ |
| `btts|first_half_moneyline:team`/`:tie` | +0.10/−0.17 | ±0.10 | [M+DC] btts×1H-lead POSITIVE (unlike FT btts\|ml) ⟦config.py:445-448, 966-967⟧ |
| `first_half_moneyline|spread:same`/`:opp`/`:tie` | +0.70/−0.63/−0.32 | ±0.10/0.12/0.12 | [M+DC] ⟦config.py:449-451, 968-970⟧ |
| `advance|first_half_total` | +0.09 | ±0.16 | [M+DC] ⟦config.py:453, 971⟧ |
| `first_half_total|moneyline` | +0.14 | ±0.13 | [M+DC] ⟦config.py:454, 972⟧ |
| `first_half_total|spread` | +0.27 | ±0.14 | [M+DC] ⟦config.py:455, 973⟧ |
| `first_half_total|player_goal` | +0.33 | ±0.17 | [M+DC] ⟦config.py:456, 974⟧ |
| `advance|first_half_btts` | −0.03 | ±0.12 | [M+DC] ⟦config.py:457, 975⟧ |
| `first_half_btts|total` | +0.65 | ±0.13 | [M+DC] o2.5 anchor; o1.5 side is exact containment ⟦config.py:458, 976⟧ |
| `first_half_btts|moneyline` | −0.03 | ±0.10 | [M+DC] ⟦config.py:459, 977⟧ |
| `first_half_btts|spread` | −0.08 | ±0.11 | [M+DC] ⟦config.py:460, 978⟧ |
| `first_half_btts|player_goal` | +0.33 | ±0.18 | [M+DC] ⟦config.py:461, 979⟧ |
| `first_half_spread|first_half_total` | +0.95 | ±0.10 | [C] 1H margin≥2 ⟹ 1H over-1.5 ⟦config.py:463, 980⟧ |
| `first_half_btts|first_half_moneyline:team`/`:tie` | −0.18/+0.30 | ±0.10 | [M+DC] ⟦config.py:464-465, 981-982⟧ |
| `first_half_moneyline|first_half_spread:same`/`:opp`/`:tie` | +0.95/−0.95/−0.95 | ±0.10 | [C] same-team lead ⊃ lead-by-2; opp/tie exclude it ⟦config.py:466-468, 983-985⟧ |
| `first_half_btts|first_half_spread` | −0.22 | ±0.10 | [M+DC] ⟦config.py:469, 986⟧ |
| `first_half_btts|first_half_total` | +0.95 | ±0.10 | [C] 1H-btts ⟹ 1H over-1.5 ⟦config.py:470, 987⟧ |
| `advance|first_half_spread:same`/`:opp` | +0.72/−0.72 | ±0.13/0.15 | [M+DC] ⟦config.py:471-472, 988-989⟧ |
| `btts|first_half_spread` | 0.00 | ±0.10 | [M] VERIFIED ~0 (2H recovery cancels it) ⟦config.py:473, 990⟧ |
| `first_half_spread|player_goal:same`/`:opp` | +0.45/−0.22 | ±0.15 | [M+DC] ⟦config.py:474-475, 991-992⟧ |

Bands on the 1H family are the **era/structural proxy** — no live 1H book exists to run the conditional-MLE gate, so they stay 0.10–0.18 until one does ⟦config.py:430-435, 935-936, 949-951⟧ ⟦results_soccer.md §1 caveat⟧.

### 3.2 Orientation curve (1)

`oriented_curve["soccer:btts|moneyline"]` = knots (0.20,−0.05) (0.35,−0.18) (0.50,−0.28) (0.65,−0.34) (0.85,−0.36), band ±0.13 — piecewise-linear in the ML leg's YES marginal, FLAT clamp outside; re-measured 2026-07-07 on 8,982 matches (heavy longshot ~0, deepening to ~−0.36 heavy favorite); the curve WINS over scalar/fav-dog whenever marginals are available ⟦config.py:1210-1231⟧ ⟦sgp.py:134-166, 1074-1076⟧.

### 3.3 Orientation/rung resolvers (soccer-relevant) + the fallback chain

Dispatch in `build_sgp_correlation` ⟦sgp.py:824-1076⟧; **every resolver returns None on any parse doubt, and the caller falls to the plain entry — an orientation is never invented** (each docstring pins this contract):

| resolver | pair | resolves | fallback on None |
|---|---|---|---|
| `_winner_period_prior` ⟦sgp.py:381-395⟧ | 1H-winner × FT-winner | `:same`/`:opp` by team-code equality; draw legs → None | plain (flat +0.6 — draw-involving winner pairs unmeasured ⟦config.py:348-350⟧) |
| `_spread_pair_prior` ⟦sgp.py:213-227⟧ | 1H-spread × FT-spread | `:same`/`:opp`, line digits stripped | plain |
| `_spread_winner_prior` ⟦sgp.py:230-244⟧ | 1H-spread × FT-winner | `:same`/`:opp` | plain |
| `_period_total_prior` ⟦sgp.py:398-415⟧ | 1H-winner × {1H-total, FT-total, FT-btts, 1H-btts} | `:team`/`:tie` (hard sign flip) | plain |
| `_corners_team_prior` ⟦sgp.py:187-201⟧ | team-corners × team-corners | `:same`/`:opp` | plain (= opposite-team value ⟦config.py:336-338⟧) |
| `_corners_winner_prior` ⟦sgp.py:299-322⟧ | team-corners × winner | `:same`/`:opp`/`:tie` | plain |
| `_corners_spread_prior` ⟦sgp.py:325-340⟧ | team-corners × spread | `:same`/`:opp` | plain |
| `_advance_player_prior` ⟦sgp.py:258-276⟧ | advance × scorer | `:same`/`:opp` by player-code prefix vs advance team | plain |
| `_spread_player_prior` ⟦sgp.py:279-296⟧ | (1H-)spread × scorer | `:same`/`:opp` | plain |
| `_period_winner_player_prior` ⟦sgp.py:362-378⟧ | 1H-winner × scorer | `:same`/`:opp`/`:tie` | plain |
| `_oriented_team_prior` ⟦sgp.py:343-359⟧ | advance×1H-winner, 1H-winner×(FT/1H)-spread, advance×1H-spread | `:same`/`:opp`/`:tie` | plain |
| `_oriented_curve_prior` → `_oriented_prior` ⟦sgp.py:106-166, 1066-1076⟧ | any one-moneyline pair | curve first, else fav/dog blended linearly across ML marginal 0.45–0.55 (no 50¢ cliff) | plain |
| `advance|moneyline` branch ⟦sgp.py:992-998⟧ | advance × regulation-ML | only the DRAW case reaches copula → `:tie`; team cases intercepted upstream as containment/impossible | plain |

**Terminal fallback chain:** oriented resolver → plain sport key → global `pair_rho` key → flat `same_event_rho=0.6` with band `|0.6|+0.30=0.90` (so corr_low = clamp(0.6−0.90) reaches −0.30 — a fail-safe widening; the point stays 0.6) ⟦sgp.py:95-103, 798-807, 1092-1101⟧ ⟦config.py:135-137, 162-163⟧. Cross-game pairs: `cross_event_rho=0.0` ⟦config.py:136⟧ ⟦sgp.py:789⟧. All three matrices (low/point/high) PSD-repaired independently; ρ clamped ±0.95 ⟦sgp.py:71-72, 1106-1116⟧.

### 3.4 Containment / impossibility / window families (exact logic, before any copula)

All use the ONE shared sign matrix `_containment_sign` for A⟹B: {A yes,B no}→IMPOSSIBLE; {yes,yes}→containment joint=P(A); {no,no}→containment joint=P(B no); {A no,B yes}→window (exact P(B)−P(A), the 2026-07-11 universal-window rule WIRE-1) ⟦relationships.py:264-285⟧ ⟦taxonomy.json meta.definitions⟧.

| family | code | window | farmable on the impossible mix? | tape sizing |
|---|---|---|---|---|
| Nested corner ladders (match `corners`, team `corners_team`; same family+game+scope, different lines) | ⟦relationships.py:182-199, 524-575⟧ | yes-LOW + no-HIGH = NESTED_BAND, exact P(low)−P(high) — 114 real band combos on tape (85 match + 29 team) were priced flat +0.6 before ⟦docs/reports/2026-07-10-one-leg-per-ladder-rule.md⟧ | **Yes** — one-count tautology ⟦relationships.py:556-564⟧ | same-side rungs exchange-blocked (400 duplicated_legs; 0 in 3.02M combos) ⟦same report⟧ |
| S12/S6: soccer spread cover ⟹ win, SCOPE-MATCHED (FT spread×regulation ML; 1H spread×1H winner) — same-team proven ONLY by suffix equality (no anchored two-team parse in soccer; non-equal suffix proves nothing → copula) | ⟦relationships.py:238-248, 754-793⟧ | {cover no, win yes} = "win NOT by N" — the 637-combo / 1,091-print tape cell, previously flat +0.6 ⟦2026-07-11-containment-universe-sweep.md gap #1⟧ | **Yes** — one-scoreline tautology (win-NO legs exchange-blocked, defensive) ⟦relationships.py:750-753⟧ | S12 window TAPE-PRINTED ⟦taxonomy.json S12⟧ |
| S7/S8/S13: (1H-)spread cover-by-N YES × total over-(M−0.5) NO, M≤N ⇒ IMPOSSIBLE (winner alone scores ≥N≥M; scope nesting per `_SPREAD_TOTAL_SCOPES`) | ⟦relationships.py:250-261, 810-847⟧ | impossible mix only; yy/nn/ny keep structural/copula (windows NOT wired) ⟦relationships.py:799-800⟧ | S7 (1H×1H) and S13 (FT×FT) **Yes**; **S8 (1H-spread × FT-total) NO** — V2 ruling: spans TWO official records, Kalshi abandonment/award text uncaptured ⟦relationships.py:801-809⟧ ⟦2026-07-11-judge-fixes FIX-3⟧ | S8-yn was constructible + priced +0.52 copula before the wire — a live farm the engine missed ⟦2026-07-11-containment-universe-sweep.md priced-but-WRONG⟧ |
| Family 1 (S2): 1H-BTTS ⟹ FT-BTTS | ⟦relationships.py:864-894⟧ | S2-ny window = "BTTS completes after halftime", exact | **Yes** ⟦relationships.py:878-881⟧ | 3 pure + 127 in larger combos on tape ⟦taxonomy.json S2⟧ |
| Family 2 (S1): regulation team-win ⟹ FT over-0.5 (line N==1 only; TIE/ADVANCE excluded — a 0-0 pens advance implies no goal) | ⟦relationships.py:896-935⟧ | S1-ny defensive (win-NO exchange-blocked) | **Yes** ⟦relationships.py:920-924⟧ | 8 pure + 70 in larger combos ⟦taxonomy.json S1⟧ |
| Family 3 (S3): 1H over-N ⟹ FT over-N, **SAME line only** (cross-line M<N deliberately not modeled — see HOLES #4) | ⟦relationships.py:937-974⟧ | S3-ny = the 379-print tape cell, exact | **Yes** ⟦relationships.py:958-962⟧ | 29 combos/79 prints declined pre-collapse ⟦ph4/wc/containment_frequency.json⟧ |
| Same market both sides | ⟦relationships.py:459-473⟧ | — | **Yes** (airtight) | — |
| Mutual-exclusion (≥2 YES of an exclusive event) | ⟦relationships.py:487-500⟧ | — | **No** — metadata-dependent ⟦relationships.py:497-500⟧ | — |
| **Taxonomy tripwire** (backstop, fires after every family above, ANY combo size, beats recorded containment/window/conditional pairs) | ⟦relationships.py:976-993⟧ ⟦tripwire.py⟧ | — | **No** — fixture-driven certainty ≠ in-code proof; decline + countable note "taxonomy-impossible tripwire: <shape>"; a match is proof the validator loosened; inert-with-one-warning on missing/corrupt fixture ⟦tripwire.py:15-36, 269-288⟧ | 50 pinned cells across 28 shape ids ⟦tests/fixtures/ground_truth/taxonomy_impossible.json, counted 2026-07-11⟧ |

**Collapse plan (CONTAINMENT-in-larger-combo, 2026-07-11):** replaces the old "not modeled" UNKNOWN decline (227 combos / 712 prints ≈ 91 prints/day in WC flow ⟦ph4/wc/containment_frequency.json⟧). Superset legs drop (Fréchet-clamped subset marginal mirrors `price_containment` ⟦engine.py:499-524⟧); window pairs become band super-legs P(B)−P(A); guards fail closed to UNKNOWN on: cyclic implication without a kept witness, a leg holding >1 collapse role, a band super-leg whose game holds any other KEPT leg, and (V2 refutation, FIX-1) a CONDITIONAL super-leg with a same-game kept companion for EVERY side mix — the live counterexample priced 0.4183 vs 0.3451 truth (+7.32¢, sign inverted) before the guard ⟦relationships.py:308-440⟧ ⟦2026-07-11-judge-fixes FIX-1 table⟧. Engine carries a defensive mirror of the conditional guard ⟦engine.py:526-540⟧. Inverted band (P(low)≤P(high)) ⇒ NoQuote, never a clamped fair ("sell-only NO bid would quote near $1 on bad data") ⟦engine.py:425-433, 449-457⟧.

### 3.5 Structural pricer (Dixon-Coles) — the soccer v2

- **Model:** independent Poisson 90' + DC low-score τ; knockout draws play ET at `et_factor`×rates; pens ⇒ win-market NO; player goals = multinomial thinning ⟦NOTES.md I4⟧ ⟦pricing/dixon_coles.py⟧. Inverted per game from live leg prices behind the JointEstimate interface; ≥2 team-level legs required to identify; any parse/identification doubt ⇒ StructuralError ⇒ copula ⟦NOTES.md I6-I7⟧ ⟦structural.py:1-32⟧.
- **Config:** `enabled=True`, `max_goals=12`, `dc_rho=−0.05` FITTED (band ±0.08), `et_factor=1/3` [0.25,0.40], `pens_win_prob=0.5±0.10`, `half_share=0.4507→0.45` measured on 8,981 HT/FT matches (band ±0.03 covers the 0.44–0.46 league spread), `knockout_series=["KXWC"]` ⟦config.py:1304-1353⟧.
- **OOS gate (the license to be on):** 8,980 club games, train <2024 / test 23/24+24/25 — structural beats the SHIPPED v1 copula on all three joint-log-loss metrics: hw×over 1.24657 vs 1.24734, hw×btts 1.26330 vs 1.26724, 3-leg triple 1.70607 vs 1.74775 (independence 1.94197); margin grows with combo complexity ⟦NOTES.md I9 + gate table⟧ ⟦config.py:1311-1316⟧. Live validation: the SPA/POR parlay market-priced at exactly our structural fair (10.9¢; independence $91-payout, v1 $65) ⟦NOTES.md, structural-v2 trigger note⟧.
- **Settlement windows (rule-book verified 2026-07-06, I8):** KXWCGAME = Regulation-Time Moneyline (90', TIE possible — coexists with KXWCADVANCE on the same knockout matches, live tape); ADVANCE = ET+pens; BTTS/TOTAL/SPREAD regulation-only (`include_et=False` always); player GOAL = full game incl ET, pens excluded. Windows are worth ~1¢ of fair (anchors moved 0.2282→0.2401, 0.1088→0.1153 on re-derivation) ⟦NOTES.md I8⟧ ⟦structural.py:10-24, 192-246⟧.
- **1H handling:** goal-timing 1H legs (1H total, 1H BTTS) price structurally on the DC half-split — OOS gate shows held-out conditionals within ~0.01 ⟦structural.py:93-98⟧; 1H RESULT/MARGIN legs (1H winner, 1H spread) **defer to the copula**: the independent-increment split over-states persistence (model P(FT-win|1H-lead) 0.81 vs empirical 0.75) and no half-share fixes it ⟦structural.py:100-109, 313-327⟧.
- **Uncertainty = priced, all through re-inversion:** per-leg marginal bands + model form (dc_rho, ET, half_share when a 1H leg is present) + pens band (Advance legs only) + inversion misfit; Fréchet-clamped ⟦structural.py:401-484⟧.
- **Applicability:** single sport, all legs one same-game group, every period leg a modeled soccer 1H leg ⟦structural.py:726-746⟧. Corners/team-total/first-goal legs are NOT in the scoreline model → whole-combo copula fallback ⟦structural.py:277⟧ ⟦config.py:216-218⟧.

### 3.6 Terminal UNKNOWN behavior

`RelationshipKind.UNKNOWN` ⇒ `NoQuote(SKIP_CLASSIFIER_UNKNOWN)` with the classifier's notes verbatim ⟦engine.py:181-182⟧; UNKNOWN leg **typing** (not relationship) widens instead ⟦sgp.py:818-821⟧. Property test required and present: the UNKNOWN branch cannot reach CreateQuote at normal width ⟦CLAUDE.md defense #2⟧ ⟦docs/reports/2026-07-10-phase3-pregame-gate.md property note⟧. Width machinery beyond the copula band: longshot floor (fair <0.15 ⇒ uncertainty ≥ 0.25×fair) ⟦engine.py:597-611; config.py:1267-1268⟧, maker-favorable snap-DOWN + free-money caps ⟦NOTES.md D10-D11⟧, and `sell_parlays_only=true` in both env YAMLs with the engine-boundary choke point forcing yes_bid=0 ⟦config/prod.yaml, config/demo.yaml⟧ ⟦engine.py:274-295⟧. Farming: `farm_impossible_combos=True`, ask = independence product × `farm_markup=1.0` rounded DOWN, `farm_max_contracts=50`, fail-closed to the ordinary impossible no-quote ⟦config.py:1273-1285⟧ ⟦engine.py:325-380⟧; NO-side settlement pays exactly $1.00 = 1−V, ledger-verified 2026-07-10 ⟦docs/reports/2026-07-10-demo-combo-settled.md⟧.

---

## 4. COMBO TYPES ON TAPE — dispositions + measured accuracy (Phase-4 capstone)

### 4.1 Universe + disposition histogram (fresh window 2026-07-06→07-11)

Pure-WC printed universe: **21,968 printed combos / 723,167 pregame prints**; live-classifier kind histogram: **21,723 OK / 230 UNKNOWN / 13 CONTAINMENT (bare 2-leg, priced) / 2 IMPOSSIBLE (farmable=True re-verified)** ⟦job-tmp ph4/wc/containment_frequency.json totals⟧. UNKNOWN reason histogram — every one named: 227 × "logical containment pair inside a larger combo: not modeled" + 3 × "nested band game 26JUL10ESPBEL carries other legs" ⟦same file⟧. The 227 decompose by shape: **1h_btts|ft_btts 127 combos/425 prints; ml_win|total_over0.5 70/202; 1h_over_n|ft_over_n 29/79; both-shapes 1/6** ⟦same file, by_shape⟧. Post-merge accounting (the collapse now prices them): **245 containment-adjacent combos = 225 PLAN + 2 GUARDED + 3 band-guard UNKNOWN (unchanged) + 13 pure-2-leg (already priced) + 2 IMPOSSIBLE — remainder 0**; gate hits 227 = 225+2 exactly, symmetric difference ∅ vs the old decline set ⟦docs/reports/2026-07-11-phase4-capstone.md residual audit; ph4/containment_residuals.json⟧. Historical honesty: the decline branch was born 2026-07-07 in `6325dbb` (same commit as the 1H wires); the old baseline carried it at the identical rate (197/19,318 = 1.02% vs fresh 227/21,968 = 1.03%); no soccer stat ever included them and no money was quoted on them ⟦capstone, historical audit⟧.

### 4.2 Zero-residual print accounting

723,167 trades = **656,555 priced + 65,633 no-prior-snapshot + 397 missing-marginals + 582 unpriceable** (the earlier 656,555-vs-631,834 "gap" was trades vs unique (ticker,timestamp) keys — 25,303 trades share a timestamp; identity closes exactly) ⟦capstone, WC per-print section⟧.

### 4.3 Measured accuracy (vs maker clearing; settlement is the unbendable ruler)

- **WC per-print (n=656,555, fair recomputed just-before-EACH print):** median |err| **1.57¢**, mean 1.98¢, bias −1.72¢, within-2¢ 62.1%, within-5¢ 95.1%. PARITY PASS: 2,656 stock-path keys re-priced through the unmodified harness vs the sliced-parallel pass — 0 mismatches, 0 extra keys ⟦capstone⟧ ⟦ph4/wc/wc_fixed_printed/wc_backtest_perprint.json; ph4/parity_pp⟧.
- **WC per-combo (n=21,443):** 1.60¢ med / −1.60¢ bias / 58.1% w2; honest baseline reproduces at +14% sample (1.55¢/59.6% at n=18,819) ⟦capstone headline table⟧ ⟦job-tmp ph4/ph4_wc_report_pconly.json⟧. Slices: 2-leg 1.90¢/51.3% (n=2,252), 3-leg 1.74¢/54.9% (4,789), 4+ 1.54¢/60.2% (14,402); prop-carrying (any GOAL/CORNERS/etc. prop leg) 1.69¢/56.3% (15,523) vs pure game-line 1.39¢/62.9% (5,920) ⟦ph4_wc_report_pconly.json per_combo⟧.
- **Blast radius (MLB-only config change):** WC re-priced on all 19,016 old printed inputs under the current repo — **bit-identical fairs, max delta 0** ⟦capstone verification 1⟧.
- **Settlement:** WC resolved n=14,359, settled-YES 8.9% (longshot-heavy flow) ⟦capstone verification 4; ph4_wc_report_pconly.json per_combo.resolved_n/settled_yes_pct⟧. Fat-markup caveat: clearing = fair + winning markup, so the per-print bias −1.72¢ is NOT proof of our error ⟦capstone WC section⟧ ⟦2026-07-08-soccer-backtest-vs-clearing.md caveat⟧.

### 4.4 Every composition observed (per-print, by family set)

**775 distinct family compositions** in the 656,555 priced print-rows; top 20 below hold **562,159 rows; the remaining 755 compositions hold 94,396 rows** (562,159+94,396=656,555 exactly) ⟦computed 2026-07-11 from ph4/wc/wc_fixed_printed/wc_backtest_perprint.json rows⟧:

| composition | print-rows | med \|err\| |
|---|---|---|
| ADVANCE | 173,916 | 1.34¢ |
| ADVANCE+PLAYER_GOAL | 148,349 | 2.09¢ |
| PLAYER_GOAL | 45,367 | 0.91¢ |
| ADVANCE+TOTAL | 31,935 | 1.09¢ |
| ADVANCE+CORNERS+PLAYER_GOAL | 27,714 | 3.00¢ |
| ADVANCE+BTTS | 18,645 | 2.39¢ |
| MONEYLINE+TOTAL | 15,598 | 1.07¢ |
| ADVANCE+PLAYER_GOAL+TOTAL | 12,086 | 2.59¢ |
| ADVANCE+CORNERS | 11,816 | 1.89¢ |
| ADVANCE+BTTS+PLAYER_GOAL | 10,932 | 3.97¢ |
| ADVANCE+CORNERS+CORNERS_TEAM+PLAYER_GOAL | 8,511 | 3.44¢ |
| ADVANCE+CORNERS_TEAM+PLAYER_GOAL | 8,010 | 3.09¢ |
| MONEYLINE | 7,861 | 0.71¢ |
| BTTS+MONEYLINE | 6,736 | 1.17¢ |
| ADVANCE+CORNERS_TEAM | 6,541 | 2.18¢ |
| ADVANCE+CORNERS+TOTAL | 6,409 | 2.30¢ |
| MONEYLINE+PLAYER_GOAL | 6,012 | 1.20¢ |
| ADVANCE+BTTS+TOTAL | 5,964 | 2.46¢ |
| ADVANCE+CORNERS+CORNERS_TEAM | 5,009 | 2.88¢ |
| ADVANCE+CORNERS+PLAYER_GOAL+TOTAL | 4,748 | 3.59¢ |

Disposition: everything above prices (OK → structural or copula per §3; ADVANCE-containing same-game combos are copula because ADVANCE identifies fine but corners/1H mixes force copula; pure cross-game price at exact independence). The residual drag is visibly the dense ADVANCE+CORNERS/BTTS+PLAYER_GOAL stacks (3–4¢) — the capstone's identified lever: "measuring soccer pair priors (corners|advance, pgoal|total, btts|advance), not chasing clearing prints" ⟦capstone, within-2¢ decomposition⟧.

### 4.5 Settlement P&L (the money ruler; 2026-07-08, early-rounds sample)

WC-only, 123 resolved combos of 1,480 pure-soccer (53 UCL excluded, 1,304 then-pending; settled YES 6.5% by combo / 4.4% by volume) ⟦2026-07-08-soccer-settlement-pnl.md sample⟧: real makers +$6,660 = **+3.05¢/ct** (218,608 ct); YES-buyer flow +4.44¢/ct vs NO-fade flow **−14.12¢/ct against the maker** — genesis of `sell_parlays_only` ⟦same report §1⟧ ⟦config.py:1255-1263⟧. Our sim as parlay seller: peak **+$8,728 at 1¢ markup** (won 48.1% of flow at +8.97¢/ct); YES-hit of fills rises monotonically with markup 1.3%→14.8% — "wider markup = more toxic book" ⟦same report §2⟧. Calibration: mean fair 8.6¢ vs realized 4.2% YES, Brier 0.0400 ⟦same report §3⟧. Vs-clearing companion: WC-only n=998, median |err| 1.60¢, 8% over-priced ⟦2026-07-08-soccer-backtest-vs-clearing.md headline⟧. Blind RFQ test 2026-07-07: 28 real combos priced blind, matched makers on calibrated pairs; the two flagged pairs (corners|corners_team, btts|first_half_total) were measured and wired next day ⟦docs/reports/2026-07-07-final-rfq-blind-test.md⟧ ⟦2026-07-07-soccer-calibration-and-farming.md §1⟧.

---

## 5. THE KALSHI BOUNDARY

### 5.1 Constructible vs blocked (exchange matrix)

194 shape×side-mix cells probed: **59 ALLOWED / 118 BLOCKED / 17 UNPROBEABLE** (MLB demo events finalized; status gate precedes the semantic check, proven by control probe) ⟦2026-07-11-containment-universe-sweep.md⟧ ⟦probe dir exchange_matrix.json meta: 86 constructions, 5 rounds, probes P01–P42/F/R/M/N; control P01 round-trip OK, minted demo markets inert⟧. Soccer-controlling mechanisms ⟦sweep report, exchange findings⟧:
- `is_yes_only` kills NO-side mixes for **GAME / 1H / GOAL / FIRSTGOAL** (e.g. S1-nn and S1-ny blocked_by "KXWCGAME is yes-only" ⟦taxonomy.json S1.side_mix⟧; S12-yn farm blocked the same way ⟦taxonomy.json S12⟧).
- `size_max=1` blocks same-event pairs except corners (bands constructible; corners events size_max=null ⟦taxonomy.json S30 evidence⟧).
- `duplicated_legs` = same-side rungs of one ladder ⟦taxonomy.json S30.side_mix⟧ ⟦2026-07-10-one-leg-per-ladder-rule.md: 400 duplicated_legs, 0 in 3.02M combos⟧.
- `conflicting_leg_outcomes` covers team-entity containments/exclusions + BTTS×TOTAL at the implying line + NOGOAL pairs + corners cross-ladder — but MISSES S1/S2/S3-same-line/S8 ("idiosyncratic, not principled") ⟦sweep report finding 1⟧.
- **Constructible farms (demo-verified 2026-07-11):** S1-yn, S2-yn, S3-same-line-yn — all recognized IMPOSSIBLE-farmable by the engine (4+1 real farm combos on tape); S8-yn was the NEW unrecognized one, now IMPOSSIBLE (farmable=False) ⟦sweep report farm inventory⟧ ⟦relationships.py:836-847⟧.
- **Validator TIGHTENED Jul-07→Jul-11** (team-corners farm + match-corners inverted bands now block) — farm shelf life is short; ALLOWED evidence older than ~Jul-09 is refutable ⟦sweep report caveat⟧.

### 5.2 Settlement-scope dependencies — which of OUR rules lean on which Kalshi fact

⟦job-tmp ph4/wire2/kalshi_robustness.md §4, soccer rows⟧:

| our wired rule | Kalshi settlement fact | pin | alarm today |
|---|---|---|---|
| Farming at all (sell-only un-gated) | combo NO pays $1−V; early-NO finalization | `tests/fixtures/ground_truth/conventions.json` — REAL settlement 2026-07-10, $1.00 to the cent ⟦2026-07-10-demo-combo-settled.md⟧ | reconciliation HALT (post-fill) |
| Corner ladder bands + rung farms (S30/S31) | all rungs settle on ONE combined count incl. extra time; strike_type ≥ | rules text verified 2026-07-09 ⟦relationships.py:184-189⟧; raw `market_rules.json` snapshot **NOT in repo** (jobs probe dir) ⟦robustness §4 row 2⟧ | HALT post-fill only |
| S1 farm + window | KXWCGAME = Regulation-Time ML (a win needs a regulation goal) | NOTES.md I8 + live GAME/ADVANCE coexistence | HALT; if GAME ever included pens, a 0-0 pens win makes the farm losable ⟦robustness §4 row 3⟧ |
| S2/S3 containments/windows | BTTS/totals regulation-only; 1H window ⊆ FT; goals persist | NOTES.md I8 | LOW & asymmetric exposure ⟦robustness §4 row 4⟧ |
| S12/S6 spread⟹win | KXWCSPREAD settles end-of-regulation like KXWCGAME; TEAMn = win by ≥n | NOTES.md I8 + doc-verified suffix ⟦structural.py:233-236⟧ + 36 tape suffix shapes | HALT; spread-with-ET would make the S12-yn farm losable ⟦robustness §4 row 5⟧ |
| S7/S13 spread-total impossibility | both legs regulation, one scoreline | NOTES.md I8 | HALT ⟦robustness §4 row 6⟧ |
| Player-goal DNP | scorer no-show ⇒ market scalar-settles to last fair price | Kalshi-verified; decision: build nothing, ≈EV-neutral, rare, fail-safe — handle reactively ⟦docs/dnp_scalar_settlement.md⟧ ⟦README index 2026-07-09 row⟧ | n/a (no prop-DNP-sensitive farm) |

**The one unpinned farm dependency:** whether any soccer TEAM-market family (GAME/TOTAL/BTTS/SPREAD/CORNERS — the legs of our farmable=True tautologies) carries an abandonment/postponement SCALAR clause; if one does, that family needs the MLB farmable=False treatment ⟦robustness §4-5⟧. Also the S8 farm re-opens only on captured KXWC-totals abandonment/award rules text ⟦2026-07-11-judge-fixes NEXT STEPS⟧.

### 5.3 Tripwire coverage

Fixture `taxonomy_impossible.json`: **50 cells / 28 shape ids** (S3L, S4, S5, S9, S10, S11, S14–S22, S24, S26–S29, S32, S42, S44–S48, S50) ⟦fixture, counted 2026-07-11⟧ — pins 29 of the 30-cell dangerous class (S49 = the documented unpinned residual — judge F2) from the robustness join ⟦kalshi_robustness.md §2.2⟧ ⟦2026-07-11-judge-fixes FIX-4 coverage bullet⟧. Verdict is always IMPOSSIBLE farmable=False + countable note; a live match is proof the validator loosened ⟦tripwire.py:15-36⟧. **Documented residuals:** S49 (tennis tournament⟹match) is cross-scope — outside the same-game scan, no verified same-scope key to pin; S23/S25 excluded (stage-conditional, not unconditionally impossible) ⟦tripwire.py:32-36⟧ ⟦judge-fixes FIX-4⟧. Fail-closed: missing/corrupt fixture ⇒ inert + one warning, behavior unchanged (tested) ⟦tripwire.py:269-288⟧.

### 5.4 Validator-change exposure (kalshi_robustness.md verdicts)

- **Blocked shape cannot reach the engine — CONFIRMED**: RFQs enter only via exchange WS/REST, every RFQ references a minted market, Kalshi mints only through the validator, we never mint (GET-only collection endpoints; no CreateMarketInMVEC anywhere in src/), engine demands combo metadata+grid; hole search found none ⟦kalshi_robustness.md §1⟧.
- **Validator loosens:** 118 BLOCKED cells joined against the engine → 12 exact / 6 impossible-farm / 3 impossible-noquote / 45 copula-measured / 34 flat-fallback / **30 DANGEROUS (certain-$0 paper would price at 5–35¢ ref fair)** ⟦robustness §2.1⟧. 21 of the 30 are soccer (S3L,S4,S5,S9,S10,S11,S14–S22,S24,S26–S29 bundle,S32) ⟦robustness §2.2⟧. That gap is now 29/30 CLOSED by FIX-4 (S49 unpinned — judge F2) ⟦judge-fixes FIX-4⟧. DC-intercept rows are a mitigation, not a defense (inversion failure falls to copula, not decline) ⟦robustness §2.2 footnote⟧.
- **Validator tightens: nothing breaks** — no runtime code depends on constructibility (`is_yes_only`/`size_max` appear only in comments); cost is revenue-only (farms + window flow dry up) ⟦robustness §3⟧.
- **Missing pre-fill alarms (recommended, NOT implemented):** rules-pin sweep (weekly hash of rules_primary/strike_type per wired family), floor_strike runtime assertion, collections-metadata diff ⟦robustness §4 missing-pins 1–3⟧.

---

## 6. HOLES — missing, weak, assumed, queued (the judge knows the queue exists)

1. **Two GUARDED exact-algebra declines** (window-guard): (a) FRA-win + FT-over0.5-YES + 1H-over0.5-NO — one leg claimed by both a containment drop and a band window; solvable EXACTLY by a containment-drop precedence rule; (b) 5-leg ESPBEL post-collapse band with same-game companions; solvable EXACTLY by difference-of-parlays (width summed, never differenced). Both guards CORRECT, fixes designed not wired; worth 2 combos of observed flow. Operator decision open ⟦2026-07-11-phase4-capstone.md residual audit⟧.
2. **3 nested-band same-game-companion declines** (26JUL10ESPBEL corners windows) — band-vs-neighbour ρ is the rung's ρ attenuated by an unmeasured factor (bites hardest on corners|corners_team 0.62); widen-or-no-quote by design ⟦relationships.py:1038-1051⟧ ⟦ph4/wc/containment_frequency.json unknown_reason_histogram⟧.
3. **`btts|first_half_btts` pair prior unmeasured** — the S2-window super-leg prices exactly, but when that pair needs a copula ρ vs other legs it has no entry; 17 combos/16 prints on tape; queued to the soccer pair-prior measurement pass ⟦2026-07-11-containment-universe-sweep.md gap #4⟧.
4. **S3 cross-line (M<N) 1H⟹FT totals not modeled** — family 3 covers SAME line only ⟦relationships.py:938-940⟧; taxonomy grades S3 "PARTIALLY MODELED"; lower-line pairs DO print and price at the measured first_half_total|total +0.73 instead of exact arithmetic; the impossible M<N mix (S3L) is exchange-blocked + tripwire-pinned ⟦taxonomy.json S3⟧ ⟦fixture S3L⟧.
5. **KXWCFIRSTGOAL/NOGOAL masquerade as `player_goal`** (live-run verified §1.4) — first-goal legs price on anytime-scorer priors (approximate) and NOGOAL's copula sign is actively wrong for goal-requiring pairs; all dangerous mixes exchange-blocked + pinned (S24, S26–S29), and a FIRSTGOAL⟹GOAL≥1 containment wire is recommendation #2 in the robustness report — not wired ⟦kalshi_robustness.md §2.4-2⟧. UFC/golf classification queue is the same bucket (S46–S48, S50 → UNKNOWN flat 0.6, exchange-blocked + pinned) ⟦sweep report finding 4⟧.
6. **UCL/UEL/UECL regime unbuilt** — gated off (§1.6); two-legged aggregate advance semantics = the documented real-mispricing residual; `KXUCLTOTAL` also lacks the over-0.5 rung so S1 logic wouldn't transfer blindly ⟦taxonomy.json S1.line_condition⟧.
7. **1H result/spread structural deferral** — missing negative inter-half serial correlation (model 0.81 vs empirical 0.75 persistence); copula's measured priors carry the pathway instead ⟦structural.py:100-109⟧.
8. **`knockout_series` maps SERIES, not match phase** — a group-stage KXWC match would be misclassified knockout; fine for the current knockout rounds, revisit next tournament ⟦config.py:1350-1353⟧ ⟦NOTES.md I8 residual⟧.
9. **No live 1H book** — every 1H-family band is an era-stability/structural proxy, not the conditional-MLE gate; blind re-test of the 1H cluster on a live book is an explicit open gate ⟦config.py:430-435⟧ ⟦2026-07-07-soccer-calibration-and-farming.md NEXT STEPS⟧.
10. **Soccer abandonment scalar clause unpinned** — the one place an unpinned Kalshi rule could turn a farmable=True tautology into a losable position; S8 farm blocked on the same evidence ⟦kalshi_robustness.md §4-5⟧ ⟦judge-fixes FIX-3⟧.
11. **Pre-fill alarms not implemented** — rules-pin sweep, floor_strike assertion, collections diff (all recommended; reconciliation HALT is post-fill only) ⟦robustness §4 missing-pins⟧. `market_rules.json`/`collections.json` baselines still live only in job-tmp, not `docs/calibration/` ⟦judge-fixes NEXT STEPS⟧.
12. **Teammate/opposing scorer pairs** — `player_goal|player_goal`=+0.03±0.10 blend; conditional-MLE teammates ≈0.00 FAILS the OOS gate (WIDEN-ONLY verdict; folk +0.15–0.35 double-counts team strength) ⟦results_soccer.md §2⟧.
13. **`corners_team` carries no home/away orientation** — `corners|corners_team` ships a single blend (home +0.65/away +0.57) with the band spanning the split ⟦config.py:252-255⟧.
14. **Dense-SGP drag** — ADVANCE+CORNERS(+PLAYER_GOAL/BTTS) stacks run 2.9–4.0¢ med |err| (§4.4); capstone lever = measure corners|advance, pgoal|total, btts|advance from co-settlement, "not chasing clearing prints"; the mixed-bucket weakest cell (wc2+mlb1, n=385, 1.11¢) is WC-pair-heavy ⟦capstone w2c decomposition + watch list⟧.
15. **Markup decision open** — settlement P&L says thin (~1¢) beats fat (adverse-selection gradient §4.5), but the standing rule is pooled multi-week, never refit on a P&L window ⟦2026-07-08-soccer-settlement-pnl.md NEXT STEPS⟧ ⟦memory feedback_no_refit_on_pnl / project_kct_resume_state⟧.
16. **65,633 no-prior-snapshot + 397 missing-marginals + 582 unpriceable prints** in the capstone accounting — named, structural (no snapshot before the print), not silent ⟦capstone §4.2⟧.
17. **WC per-print bias −1.72¢ is markup-confounded** — decomposition attributes 86% of w2 misses to one-sided fair-below-clearing; against settlements the mixed bucket centers to 0.03¢, but WC-specific settlement calibration beyond §4.5's early sample awaits the weekly cadence (#15 runway) ⟦capstone within-2¢ decomposition + NEXT STEPS⟧.
18. **Demo fill e2e (#14) + soccer blind re-test** — both queued before replicating the calibrate→audit→test workflow to other sports ⟦capstone NEXT STEPS⟧ ⟦README.md blocked/open gates⟧.
19. **`TOTAL` small-n over-pricing tail** — family bias +7.96¢ (n=17) in the vs-clearing backtest, flagged not root-caused ⟦2026-07-08-soccer-backtest-vs-clearing.md family table⟧.
20. **Recommendation backlog from the robustness report** (soccer-relevant, decline-only today via tripwire, no pricing): FIRSTGOAL/NOGOAL family wire (#2), under-0.5⟹draw complement containment (#4) ⟦kalshi_robustness.md §2.4⟧.

---

## APPENDIX — source inventory used

Code: `src/combomaker/pricing/{legtypes,relationships,sgp,structural,engine,tripwire,legs,joint,copula,dixon_coles,quote}.py`, `src/combomaker/rfq/{filters,pregame,intake,models}.py`, `src/combomaker/ops/config.py`, `config/{demo,prod}.yaml` (all at d65bb6e). Docs: `NOTES.md` (I1–I10, K1–K9, L1–L13, P3-1..4, H1–H7), `docs/calibration/results_soccer.md`, `docs/calibration/containment_probe/{taxonomy,exchange_matrix,engine_matrix}.json`, `docs/dnp_scalar_settlement.md`, `docs/reports/` (2026-07-07 blind test + calibration/farming; 2026-07-08 backtest-vs-clearing + settlement-pnl + sell-parlays-only + yes/no mechanics; 2026-07-10 one-leg-per-ladder, phase3 pregame, demo-combo-settled, scorecard; 2026-07-11 containment sweep, judge fixes, phase4 capstone), `tests/fixtures/ground_truth/{conventions,taxonomy_impossible}.json`. Job-tmp artifacts: `ph4/wc/containment_frequency.json`, `ph4/ph4_wc_report_pconly.json`, `ph4/wc/wc_fixed_printed/wc_backtest_perprint.json`, `ph4/containment_residuals.json`, `ph4/wire2/kalshi_robustness.md`, `containment_probe/tape_universe.json`. Live checks run for this overview 2026-07-11: public GET /markets (KXWCGAME, KXWC1H), `uv run` executions of `classify_leg`/`classify_sport`/`CorrelationConfig` (96/96 count), composition aggregation over the per-print JSON.
