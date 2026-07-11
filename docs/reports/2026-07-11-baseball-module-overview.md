# BASEBALL / MLB MODULE OVERVIEW — kalshi-combos-TWO @ main `d65bb6e`

**Date:** 2026-07-11 · **Scope:** every KXMLB* family incl. props — intake → marginals →
joint layer (165-entry pair table, 149-cell conditional table, rung system, containment/
collapse/tripwire, `mlb_runs` structural) → tape dispositions → exchange boundary → holes.
**Verification method:** every count in this document was re-executed against the loaded
config/module on 2026-07-11 (165 pair entries / 165 bands / zero orphans; 149 conditional
cells = 40 exact + 109 measured — `uv run python` against `combomaker.ops.config.CorrelationConfig`
and `combomaker.pricing.conditionals_mlb.SAME_PLAYER_CONDITIONALS`), not copied from reports.
Every claim carries a primary-source tag. Untagged = defect; report one.

```
                    ┌────────────────────────────────────────────────────────────┐
                    │                    MLB PRICING PIPELINE                    │
                    └────────────────────────────────────────────────────────────┘
 RFQ (exchange-minted combo only)
   │
   ▼
 ┌─────────────┐   ┌──────────────┐   ┌──────────────────────────────────────────┐
 │ FILTERS     │──▶│ PREGAME GATE │──▶│ RELATIONSHIP CLASSIFIER (relationships.py)│
 │ whitelist,  │   │ ET-embedded  │   │ dup/mutex → same-player exact/conditional │
 │ legs, size, │   │ start; fail- │   │ → ml|spread → spread×total impossible     │
 │ books, time │   │ closed decl. │   │ → soccer families → TRIPWIRE → collapse   │
 └─────────────┘   └──────────────┘   └───────────────┬──────────────────────────┘
                                                      │ IMPOSSIBLE(farmable?) / UNKNOWN /
                                                      │ CONTAINMENT / NESTED_BAND / OK
                                                      ▼
 ┌──────────────┐   ┌───────────────────────────────────────────────────────────┐
 │ MARGINALS    │──▶│ ENGINE (engine.py): farm|decline → containment/collapse → │
 │ leg books,   │   │ structural mlb_runs (ML/TOTAL/SPREAD, same-game, winner+  │
 │ microprice,  │   │ total present) → copula w/ 165-entry table + resolvers +  │
 │ fail-closed  │   │ rung chain → quote (width, DO-6 basket, sell-only)        │
 └──────────────┘   └───────────────────────────────────────────────────────────┘
```

---

## 1. INTAKE

### 1.1 The ticker/series universe

**Exactly 9 combo-eligible MLB families** — verified against all 1,387 MVE collections
(only 2 carry baseball, each with exactly these 9) and 3 independent tape windows (~627k
MLB combos, zero other families trading) [docs/reports/2026-07-09-mlb-classification-and-rho-verification.md §1;
docs/calibration/staged_mlb_props.md §"The 9 combo-eligible families"]:

| family | LegType | ticker grammar | tape share note |
|---|---|---|---|
| KXMLBGAME | `moneyline` | `KXMLBGAME-<game>-<TEAM>` | tape backbone: 72% of MLB combos [2026-07-09 classification report §1] |
| KXMLBTOTAL | `total` | `KXMLBTOTAL-<game>-<N>` = over N−0.5 (GAME total; rules verbatim "collectively score") [staged_mlb_props.md "TEAM-vs-GAME TOTAL...RESOLVED"] | 13,097 in 400k early sample |
| KXMLBSPREAD | `spread` | `KXMLBSPREAD-<game>-<TEAM>N` = "wins by over N−0.5" (doc-verified live metadata 2026-07-06: `-BOS4` = "Boston wins by over 3.5 runs") [NOTES.md K2; structural.py:507-518] | 7,181 |
| KXMLBKS | `player_ks` | `KXMLBKS-<game>-<PLAYER>-N` = starter strikeouts N+ | 3,863 |
| KXMLBHIT | `player_hit` | `KXMLBHIT-<game>-<PLAYER>-N` = batter hits N+ | 2,652 |
| KXMLBHR | `player_hr` | `KXMLBHR-<game>-<PLAYER>-N` = batter HR N+ (−1 = "to hit a HR") | 1,504 |
| KXMLBHRR | `player_hrr` | hits+runs+RBIs N+ — **NOT a home-run market** (MLBHITSRUNSRBIS.pdf) [legtypes.py:56-58] | 1,062 |
| KXMLBTB | `player_tb` | total bases N+ | 413 |
| KXMLBRFI | `rfi` | `KXMLBRFI-<gamecode>` — **no outcome suffix**; run in the 1st inning by either team [legtypes.py:59-62] | 239 |

Prop line suffix convention: `-N` means **N or more** (`floor_strike = N−0.5`) — NOT the
"over N−0.5" convention of TOTAL/SPREAD [legtypes.py:49-51; conditionals_mlb.py:7-8].

**Known, classified-but-not-prop families:** KXMLBTEAMTOTAL → `team_total` (classify-only,
NOT combo-eligible — absent from both MLB-bearing collections, 374 events each, 0 tape
occurrences) [staged_mlb_props.md "TEAM-vs-GAME TOTAL"; legtypes.py:20-25]; KXMLBEXTRAS →
`extras` (0 tape) [staged_mlb_props.md 9-family table].

**Deliberate UNKNOWN families (fail-safe):** KXMLBF5/F3/F7 (first-N-innings), KXMLBRBI/SB/OUTS
(no keyword staged — collision-prone), all futures/awards/roster series (KXMLBAL/NL,
divisions, KXMLBWINS-<TEAM>×30, PLAYOFFS, awards, ALLSTAR, draft, SEASONHR, NEXTHR, …)
[staged_mlb_props.md 9-family table rows "unknown (correct fail-safe)"]. WBC/KBO props
(KXWBCHR, KXKBORFI) intentionally unmapped, dormant/widen-safe [staged_mlb_props.md "NOT staged on purpose"].

**Sport-filter leaks (why any MLB gate must use the family list, never `classify_sport`
alone):** KXEWCMLBB (esports) and KXMLBMENTION classify Sport.MLB via the 'MLB' substring;
KXMEDIACOVERMLBTHESHOW additionally returns `is_period_leg=True` ('SH' inside 'THESHOW')
[staged_mlb_props.md "SPORT-FILTER LEAKS VERIFIED LIVE"; legtypes.py:193].

### 1.2 Parse rules — game codes, team blobs, rungs, traps

- **Game-code grammar:** segment 2 = `<YY><MMM><DD><HHMM><AWAY><HOME>[G<d>]`, e.g.
  `KXMLBGAME-26JUL101915BOSNYM-BOS` = 2026-07-10 19:15 ET, BOS @ NYM
  [pregame.py:12-19,64; sgp.py:436 `_MLB_GAME_CODE`].
- **Team blob = AWAY+HOME, un-delimited.** Blob order doc-verified live (`SFCOL` = SF@Coors,
  `BOSLAA` = BOS at Anaheim) [NOTES.md L1]. Codes are 2-3 chars, so naive prefix-splitting is
  ambiguous ~20% of the time (80/445 sampled markets) — the ONE anchored parser resolves a
  candidate at the blob ENDS: prefix ⇒ away, suffix ⇒ home, both-or-neither refuses.
  Provably unambiguous: all 30 MLB team codes enumerated live 2026-07-09, no code is a prefix
  of another, all 870 ordered concatenations tile uniquely (295 real game blobs; job 24844262;
  `tools/spotcheck_mlb_team_routing.py` re-proves vs the live API)
  [sgp.py:418-434,560-571; docs/reports/2026-07-10-bands-routing-sweep-designs.md §2].
- **Doubleheaders** append `G<digit>` to the blob (`MILSTLG1`, prod tape 2026-07-07); stripped
  only when the remainder is pure-alpha ≥4 chars; both legs of any pair must carry the
  IDENTICAL raw game-code segment so a G1×G2 pair can never merge/route
  [structural.py:126-140; sgp.py:437,552-555; relationships.py:218-235].
- **Player segment** (props): segment 3, team code = its prefix, resolved by longest leading
  fragment (4→2 chars) anchoring exactly one blob end [sgp.py:574-582].
- **Rungs:** batter props = all-digit 4th segment; spread = TEAM+digits suffix with digits
  REQUIRED; `player_ks`/`total`/`moneyline`/`rfi` NEVER carry rungs even when their tickers
  end in digits — the gate is the LEG TYPE [sgp.py:450-500].
- **Known traps, each blocked:** 'MLBHRR' contains 'MLBHR' (keyword order load-bearing)
  [legtypes.py:94]; 'TEAMTOTAL' contains 'TOTAL' [legtypes.py:71-75]; 'F5' evades the period
  regex so KXMLBF5TOTAL/F5SPREAD mis-typed as full-game TOTAL/SPREAD (live bug, fixed by
  blockers) [legtypes.py:89-92; staged_mlb_props.md "TWO LIVE MISCLASSIFICATIONS"];
  KXMLBSERIESGAMETOTAL is a series game-COUNT market, was mis-typing as runs TOTAL
  [legtypes.py:87-88]; bare HR/KS/HIT/TB/RFI keywords collide with 64/67/9/128/10 of the
  11,305 series in the full universe scan (KXANTHROPICRISK, KXLEADERNFLSACKS, KXDANAWHITEFB,
  KXBILBASKETBALL, KXSINNERFINISH…) so every prop keyword is MLB-anchored
  [legtypes.py:76-83; staged_mlb_props.md "SUBSTRING TRAP QUANTIFIED"].

### 1.3 LegTypes, classification keywords, UNKNOWN blockers

MLB-relevant LegType members [legtypes.py:17-63]: `MONEYLINE, TOTAL, TEAM_TOTAL, EXTRAS,
SPREAD, PLAYER_HR, PLAYER_HIT, PLAYER_KS, PLAYER_TB, PLAYER_HRR, RFI, UNKNOWN`.

Keyword table (ordered, first match wins) — MLB-relevant rows [legtypes.py:68-115]:

| order | keyword | → LegType | why the position matters |
|---|---|---|---|
| 3 | TEAMTOTAL | TEAM_TOTAL | must precede TOTAL (substring) [legtypes.py:71-75] |
| 4 | **LEADERMLB** | **UNKNOWN blocker** | kills KXLEADERMLB{HR,HITS,KS,…} season leaders [legtypes.py:85] |
| 5 | **MLBHRDERBY** | **UNKNOWN blocker** | contains 'MLBHR' [legtypes.py:86] |
| 6 | **SERIESGAMETOTAL** | **UNKNOWN blocker** | series game COUNT ≠ runs total (was live bug) [legtypes.py:87-88] |
| 7 | **F5TOTAL** | **UNKNOWN blocker** | first-5-innings total; 'F5' evades `_PERIOD_SERIES` (was live bug) [legtypes.py:89-91] |
| 8 | **F5SPREAD** | **UNKNOWN blocker** | ditto for SPREAD [legtypes.py:92] |
| 9 | MLBHRR | PLAYER_HRR | must precede MLBHR [legtypes.py:94] |
| 10-14 | MLBHR / MLBHIT / MLBKS / MLBTB / MLBRFI | player props + rfi | universe-verified unique hit sets (MLBHRR→1, MLBHIT→2, MLBKS→2, MLBHR→5, MLBTB→1, MLBRFI→1 with blockers) [legtypes.py:95-99; staged_mlb_props.md "SUBSTRING TRAP"] |
| 15 | TOTAL | TOTAL | catches KXMLBTOTAL |
| 20 | SPREAD | SPREAD | catches KXMLBSPREAD |
| 22 | GAME | MONEYLINE | catches KXMLBGAME |

Classification-only-is-pricing-neutral invariant: a typed pair with no table entry prices
numerically identically to an UNKNOWN pair (+0.60 point, fallback band) — so classification
and table entries shipped TOGETHER [staged_mlb_props.md "PRICING-NEUTRALITY...CONFIRMED"].
The full-universe regression: staged-keyword simulation over all 11,305 series = exactly 11
diffs, all intended, zero false positives [2026-07-09 classification report, "Adversarial
verification"; docs/reports/2026-07-10-mlb-promotion-wired.md table row "Classification"].

**The three named UNKNOWN blockers the task asks about:**
- **F5 (KXMLBF5TOTAL/F5SPREAD/KXMLBF5/F3/F7):** first-N-innings settlement window
  masquerading as full-game → explicit UNKNOWN; must never masquerade before Kalshi adds F5
  to a collection (standing monthly MVE re-scan) [legtypes.py:89-92; 2026-07-09 measurement
  report NEXT STEPS "Standing"].
- **TEAMTOTAL (KXMLBTEAMTOTAL):** NOT UNKNOWN — dedicated `TEAM_TOTAL` classify-only type so
  it can't masquerade as game TOTAL; no structural pricing; not combo-eligible today. The
  calibration's two strongest player rhos (HR×own-team-total +0.367, K×opp-team-total −0.380)
  attach to this UNTRADEABLE family and are deliberately NOT loaded onto game-total pairs
  [legtypes.py:20-25; staged_mlb_props.md "DO NOT STAGE" + config.py:525-528].
- **LEADERMLB (KXLEADERMLBHR/HITS/KS…):** season-leader futures sharing the MLB+stat
  substring → explicit UNKNOWN blocker [legtypes.py:85].

### 1.4 Event/game grouping — the 2-segment prop event convention

- Kalshi's `event_ticker` is per-market-SERIES; the same-game correlation key is the GAME
  code (`_game_key` = event_ticker after the series prefix). Grouping by event_ticker was
  the L10 critical bug (same-game SGPs priced independent) [relationships.py:288-305; NOTES.md L10].
- **MLB prop events are 2-SEGMENT, PER-GAME** — tape-verified read-only 2026-07-11: market
  `KXMLBHIT-26JUL111605COLSF-SFRDEVERS16-1` belongs to event `KXMLBHIT-26JUL111605COLSF`.
  A test helper that rsplit the last segment minted fake per-player 3-segment events,
  putting a prop's own-game companions in a different group in every e2e test — the
  "poisoned fixture" that hid the WIRE-4 counterexample (FIX-2)
  [docs/reports/2026-07-11-judge-fixes-wire4-guard-tripwire.md FIX-2].
- One real event ticker per family is pinned in the event-conventions fixture
  [tests/fixtures/ground_truth/mlb_event_conventions.json `_provenance.event_samples`].
- **Mutual-exclusivity flags (DO-7 fixture, live probe 24/24 events 2026-07-09):**
  `KXMLBGAME` = true (YES+YES of one game's moneyline event correctly IMPOSSIBLE), all 6
  prop families + TOTAL + SPREAD + RFI = false (multi-player baskets reachable)
  [tests/fixtures/ground_truth/mlb_event_conventions.json; 2026-07-09 measurement report
  "Both phase-1 blockers resolved" item 1].
- Cross-game same-day = independence (measured +0.007, under the 0.02 threshold);
  doubleheader-spanning combos → UNKNOWN/wide [2026-07-09 measurement report headline table
  row "same-day cross-game"].

### 1.5 Collection whitelist and what it admits

- Whitelist gates on `mve_collection_ticker` prefixes; empty = observe everything, quote
  mode refuses an empty whitelist [config.py:71-74; filters.py:61-64].
- **Demo whitelist (6 prefixes):** KXMVESPORTS, KXMVECROSSCATEGORY, KXMVENBA, KXMVEMLB,
  KXMVENFL, KXMVENHL [config/demo.yaml `filters.collection_whitelist`]. **Prod: NO whitelist
  configured — observe-only** [config/prod.yaml (no filters block); scorecard §8].
- MLB combos actually arrive via **KXMVESPORTSMULTIGAMEEXTENDED and KXMVECROSSCATEGORY**
  (matched by the KXMVESPORTS/KXMVECROSSCATEGORY prefixes); the **KXMVEMLB entry matches
  ZERO live collections (dead)** [docs/reports/2026-07-10-baseball-vs-soccer-template-scorecard.md §8;
  combo eligibility per KXMVESPORTSMULTIGAMEEXTENDED-R / KXMVECROSSCATEGORY-R legtypes.py:49-50].
- Other filter gates on the same path: combos_only, min/max legs 2-6, contracts 1-10,000,
  target cost $1-$50,000, max leg spread 800cc, min depth 1.0, `min_time_to_close_s` 3600
  [config.py:74-83]; UNKNOWN sizing mode ⇒ SKIP_CLASSIFIER_UNKNOWN, never "assume small"
  [filters.py:118-120].

### 1.6 Pregame gate (Phase 3)

- Never quote a combo with any in-play leg, ALL sports; `allow_inplay_legs=false` default
  [config.py:90-113; config/demo.yaml comment; rfq/pregame.py module doc].
- **KXMLB\* start times come from the VERIFIED ticker-embedded ET token** — chain (a):
  `26JUL101915` = 2026-07-10 19:15 US/Eastern, verified against the live prod API on 18
  markets across GAME/HIT/KS/TB/RFI/TOTAL/SPREAD and ET/CT/PT venues:
  `expected_expiration_time` = token-as-Eastern + exactly 3.00h on every market; venue-local
  and UTC readings refuted [pregame.py:12-23,58; NOTES.md P3-1;
  docs/reports/2026-07-10-phase3-pregame-gate.md].
- Fallback chain (b): min(close_time, expected_expiration) − offset; KXMLB per-series
  override 4.0h (API-measured expiration = start+3h ⇒ lands 1h before first pitch)
  [config.py:109-113; NOTES.md P3-3]. Chain (c): UNKNOWN ⇒ decline
  (`skip_start_time_unknown`), started ⇒ `skip_inplay_leg`, re-checked at last look
  [pregame.py:35-39; filters.py:93-102]. MLB `close_time` is game+3 days — NOT a start
  anchor, hence the min() choice [NOTES.md P3-4].

---

## 2. MARGINALS

- **Source: Kalshi's own leg orderbooks.** `KalshiBookSource.marginal` = top-of-book
  microprice; uncertainty = half-spread (prob space) + 0.02 thinness penalty when either
  side's best-bid depth < 10 contracts [pricing/legs.py:48-85]. Kalshi binaries are vig-free
  by construction (yes+no=$1) so **devig NEVER touches Kalshi-sourced probabilities** —
  devig is quarantined to external `OddsSource` adapters under `pricing/sources/`, enforced
  by an import-guard architecture test [CLAUDE.md decision #8; legs.py module doc].
- **External blend:** SportsGameOdds adapter OFF by default (weight 0.3 when on); explicit
  ticker mapping only, unmapped = Kalshi-only [config.py:1288-1301]. Sources disagreeing
  > `max_source_disagreement` 0.08 ⇒ `SKIP_SOURCES_DISAGREE` no-quote — never averaged away
  [config.py:1426; legs.py:88-111; engine.py:297-323].
- **Missing/stale behavior (fail-closed):** no book / invalid book / crossed derived book ⇒
  belief None ⇒ `SKIP_PRICING_FAILED` no-quote [legs.py:66-79; engine.py:303-307]. Feed
  freshness = WS traffic ≤30s + seq continuity AND book validity [NOTES.md B9]; quote-time
  filter re-checks leg books (spread ≤ 800cc, depth ≥ 1.0) [config.py:81-82]; at last look
  `max_leg_age_s = 2.0`, leg move ≥150cc or joint move ≥200cc ⇒ decline [config.py:1441-1443].
- Snapshot marginals in the backtests are the same book-derived quantities re-priced from
  recorded tape [Phase-4 capstone, docs/reports/2026-07-11-phase4-capstone.md "per-print"].

---

## 3. THE JOINT LAYER

### 3.0 Precedence — what fires FIRST (exact order)

Classifier order inside `classify_legs` [relationships.py:443-1059]:

1. Unknown side ⇒ UNKNOWN [relationships.py:449-454].
2. Same market both sides ⇒ IMPOSSIBLE **farmable** (tautology); duplicate same-side ⇒ UNKNOWN [relationships.py:459-473].
3. Per-EVENT mutual exclusion (2+ YES of a mutually-exclusive event, e.g. KXMLBGAME both teams) ⇒ IMPOSSIBLE, **never farmable** (metadata-dependent) [relationships.py:479-500].
4. Nested-ladder registry — CORNERS/CORNERS_TEAM only; **MLB deliberately absent** (TOTAL withheld: size_max=1; MLB same-family rungs owned by the same-player step and the tripwire) [relationships.py:182-199,524-575].
5. **MLB same-player cross-stat pairs** (identical game+player segment): exact cells ⇒ containment / IMPOSSIBLE (never farmable — 48h rain scalar); measured cells n≥50k ⇒ conditional pricing (bare pair via sgp seam, buried pair via WIRE-4 collapse); anything else ⇒ UNKNOWN [relationships.py:577-663].
6. **MLB moneyline × spread** (same game, anchored both-side parse): same-team cover⟹win containment; {cover-yes × win-no} IMPOSSIBLE not farmable; opposite-team yes×yes IMPOSSIBLE not farmable; {cover-no, win-yes} ⇒ exact window P(win)−P(cover) [relationships.py:665-738].
7. Soccer spread⟹win families (not MLB) [relationships.py:740-793].
8. **(1H-)spread-N YES × total-under NO impossibility, WIRE-3 — includes MLB S34:** margin ≥N forces total ≥N (extras only ADD runs); MLB farmable=False everywhere [relationships.py:795-847].
9. Soccer containment families 1-3 (BTTS/over-0.5/1H-total) — type-gated; Family 2 would fire on an MLB over-0.5 total but line-1 MLB totals don't trade [relationships.py:864-974; scorecard §4 NOTE].
10. **Taxonomy-impossible TRIPWIRE** — after every shipped family, before any pricing verdict; beats any recorded containment/window/conditional pair [relationships.py:976-993; tripwire.py].
11. Bare 2-leg containment ⇒ CONTAINMENT (price = P(subset)); any recorded containment/conditional in a >2-leg combo ⇒ `_collapse_containments` [relationships.py:995-1012].
12. Same-game groups; windows/bands ⇒ NESTED_BAND with the isolation guard; else OK [relationships.py:1014-1059].

Engine order [engine.py:159-239]: IMPOSSIBLE → farm-if-farmable else decline → UNKNOWN
decline → fetch beliefs → CONTAINMENT (bare: `price_containment`; collapse: super-leg
machinery) → NESTED_BAND → **structural `mlb_runs`** (only if `structural_applicable`: single
sport, ALL legs in ONE same-game group, no period legs) → copula. Structural decline falls
to the copula with a note, NEVER to silence [engine.py:222-238; structural.py:298-346].

### 3.1 The shipped MLB pair table — all 165 entries

Source of truth: `config.py` `CorrelationConfig.pair_rho_by_sport["mlb"]` (lines 508-874) +
`pair_rho_uncertainty` `mlb:*` keys (lines 1003-1208). Count re-executed 2026-07-11: **165
values / 165 bands / zero orphans in either direction.** Keys are `legtypes.pair_key`-sorted
and were generated by EXECUTING the helper (the `rfi|player_ks`→`player_ks|rfi` sort trap)
[config.py:636-638; 2026-07-10-mlb-promotion-wired.md "Catches during wiring" #1].
Grammar: `:same`/`:opp` = prop player's team IS / IS NOT the ML/spread YES team; for
batter-stat × player_ks, `:opp` IS the facing case; `:rN` = Kalshi ticker line integer,
chained in pair_key leg order when both legs are rung-keyed
[docs/calibration/phase2_wire_list.txt CONVENTION header; sgp.py:450-467].


**Group 1 — legacy game-level entries (4). Provenance: Retrosheet 2015-2024, n=20,642 games (NOTES.md multi-sport calibration table, 2026-07-06); extras split at the 2020 ghost-runner rule change. config.py:508-512.**

| pair key | rho | band |
|---|---|---|
| `moneyline|total` | -0.050 | 0.06 |
| `extras|total` | +0.100 | 0.10 |
| `extras|moneyline` | -0.040 | 0.08 |
| `moneyline|moneyline` | -0.950 | 0.04 |

**Group 2 — [A] orientation-free MEASURED (10). Provenance: 2026-07-09 measurement tranche, Retrosheet 2005-25, 49,486 games, 8 agents + xhigh judge (docs/calibration/staged_mlb_props.md FINAL RECOMMENDED TABLE [A]; docs/reports/2026-07-09-mlb-measurement-tranche.md). config.py:519-539.**

| pair key | rho | band |
|---|---|---|
| `player_ks|total` | -0.250 | 0.12 |
| `player_hr|total` | +0.240 | 0.10 |
| `player_hit|total` | +0.250 | 0.12 |
| `player_tb|total` | +0.270 | 0.10 |
| `player_hrr|total` | +0.400 | 0.08 |
| `rfi|total` | +0.370 | 0.10 |
| `moneyline|rfi` | +0.000 | 0.05 |
| `player_ks|rfi` | -0.100 | 0.08 |
| `player_ks|player_ks` | +0.040 | 0.08 |
| `spread|total` | +0.130 | 0.10 |

**Group 3 — [B] same-family batter pairs, UNROUTED plain fallbacks (4). Sign-spanning blends of the measured teammate/opponent splits (staged_mlb_props.md [B]). config.py:540-547.**

| pair key | rho | band |
|---|---|---|
| `player_hr|player_hr` | +0.030 | 0.06 |
| `player_hit|player_hit` | +0.000 | 0.08 |
| `player_tb|player_tb` | +0.000 | 0.08 |
| `player_hrr|player_hrr` | +0.080 | 0.12 |

**Group 4 — [C] ML x prop NEUTRALIZED plain fallbacks (5, incl. ml|tb added by the DO-1 sweep). Measured oriented values live in Group 7; these 0.00 sign-spanning entries are the fail-closed parse fallback. config.py:548-568.**

| pair key | rho | band |
|---|---|---|
| `moneyline|player_ks` | +0.000 | 0.30 |
| `moneyline|player_hr` | +0.000 | 0.28 |
| `moneyline|player_hit` | +0.000 | 0.26 |
| `moneyline|player_hrr` | +0.000 | 0.40 |
| `moneyline|player_tb` | +0.000 | 0.30 |

**Group 5 — [C] batter-stat x KS neutralized plain (3). Facing values live in Group 7. config.py:569-573.**

| pair key | rho | band |
|---|---|---|
| `player_hit|player_ks` | +0.000 | 0.17 |
| `player_hr|player_ks` | +0.000 | 0.12 |
| `player_hrr|player_ks` | +0.000 | 0.20 |

**Group 6 — [D] cross-family batter-batter plain (7). Judge-approved 2026-07-09; ks|tb judge re-centered. config.py:574-588.**

| pair key | rho | band |
|---|---|---|
| `player_hit|player_hr` | +0.010 | 0.06 |
| `player_hit|player_hrr` | +0.040 | 0.10 |
| `player_hit|player_tb` | +0.020 | 0.08 |
| `player_hr|player_hrr` | +0.030 | 0.08 |
| `player_hr|player_tb` | +0.020 | 0.06 |
| `player_hrr|player_tb` | +0.040 | 0.10 |
| `player_ks|player_tb` | -0.060 | 0.10 |

**Group 7 — ROUTED oriented entries (:same/:opp; 2026-07-10 resolver wire + B4 in-place hit|ks:same). staged_mlb_props.md [B]/[C]/[D] + judge amendments. config.py:589-628.**

| pair key | rho | band |
|---|---|---|
| `moneyline|player_ks:same` | +0.240 | 0.06 |
| `moneyline|player_ks:opp` | -0.240 | 0.06 |
| `moneyline|player_hr:same` | +0.230 | 0.08 |
| `moneyline|player_hr:opp` | -0.230 | 0.08 |
| `moneyline|player_hit:same` | +0.230 | 0.08 |
| `moneyline|player_hit:opp` | -0.230 | 0.08 |
| `moneyline|player_hrr:same` | +0.370 | 0.08 |
| `moneyline|player_hrr:opp` | -0.370 | 0.08 |
| `player_hit|player_ks:opp` | -0.130 | 0.10 |
| `player_hit|player_ks:same` | +0.010 | 0.04 |
| `player_hr|player_ks:opp` | -0.075 | 0.05 |
| `player_hrr|player_ks:opp` | -0.180 | 0.06 |
| `player_hr|player_hr:same` | +0.040 | 0.06 |
| `player_hr|player_hr:opp` | +0.020 | 0.05 |
| `player_hit|player_hit:same` | +0.070 | 0.06 |
| `player_hit|player_hit:opp` | +0.000 | 0.06 |
| `player_tb|player_tb:same` | +0.060 | 0.05 |
| `player_tb|player_tb:opp` | +0.000 | 0.06 |
| `player_hrr|player_hrr:same` | +0.170 | 0.06 |
| `player_hrr|player_hrr:opp` | +0.000 | 0.05 |
| `player_hr|player_hrr:same` | +0.050 | 0.06 |
| `player_hr|player_hrr:opp` | +0.000 | 0.05 |

**Group 8 — [C-sibling] spread x props neutralized plain (5). DO-1 (2026-07-10 sweep §3); bands RAISED to span the Phase-2 measured oriented extremes (phase2_wire_list.txt KEEP/RAISE lines 78-82). config.py:639-652.**

| pair key | rho | band |
|---|---|---|
| `player_ks|spread` | +0.000 | 0.32 |
| `player_hit|spread` | +0.000 | 0.31 |
| `player_hr|spread` | +0.000 | 0.28 |
| `player_hrr|spread` | +0.000 | 0.42 |
| `player_tb|spread` | +0.000 | 0.30 |

**Group 9 — rfi x spread/props (5). rfi|spread = labeled prior (mirrors measured ml|rfi 0.00); the 4 batter entries are MEASURED (A3, Phase 2 wire). config.py:653-671.**

| pair key | rho | band |
|---|---|---|
| `rfi|spread` | +0.000 | 0.15 |
| `player_hit|rfi` | +0.065 | 0.05 |
| `player_hr|rfi` | +0.091 | 0.04 |
| `player_tb|rfi` | +0.085 | 0.04 |
| `player_hrr|rfi` | +0.122 | 0.06 |

**Group 10 — moneyline|spread trio (DO-3, 2026-07-10). Oriented +-0.95 = measured exact containment shape (0/98,980 violations); plain 0.00 = parse-failure fallback. config.py:672-685.**

| pair key | rho | band |
|---|---|---|
| `moneyline|spread` | +0.000 | 0.95 |
| `moneyline|spread:same` | +0.950 | 0.04 |
| `moneyline|spread:opp` | -0.950 | 0.04 |

**Group 11 — Phase 2 wire: measured oriented+runged prop x spread/total/ML cells (A1/A2/A4), wired VERBATIM from docs/calibration/phase2_wire_list.txt (Retrosheet 2015-2025 window). config.py:686-792.**

| pair key | rho | band |
|---|---|---|
| `player_hit|spread:same:r1:r2` | +0.239 | 0.05 |
| `player_hit|spread:same:r1:r3` | +0.249 | 0.05 |
| `player_hit|spread:same:r1:r4` | +0.255 | 0.05 |
| `player_hit|spread:same:r1:r5` | +0.256 | 0.05 |
| `player_hit|spread:same:r2:r2` | +0.268 | 0.05 |
| `player_hit|spread:same:r2:r3` | +0.285 | 0.05 |
| `player_hit|spread:same:r2:r4` | +0.297 | 0.05 |
| `player_hit|spread:same:r2:r5` | +0.304 | 0.05 |
| `player_hit|spread:opp:r1:r2` | -0.194 | 0.05 |
| `player_hit|spread:opp:r1:r3` | -0.172 | 0.05 |
| `player_hit|spread:opp:r1:r4` | -0.160 | 0.05 |
| `player_hit|spread:opp:r1:r5` | -0.150 | 0.05 |
| `player_hit|spread:opp:r2:r2` | -0.223 | 0.05 |
| `player_hit|spread:opp:r2:r3` | -0.203 | 0.05 |
| `player_hit|spread:opp:r2:r4` | -0.189 | 0.05 |
| `player_hit|spread:opp:r2:r5` | -0.178 | 0.05 |
| `player_hr|spread:same` | +0.241 | 0.05 |
| `player_hr|spread:opp:r1:r2` | -0.210 | 0.05 |
| `player_hr|spread:opp:r1:r3` | -0.197 | 0.05 |
| `player_hr|spread:opp:r1:r4` | -0.185 | 0.05 |
| `player_hr|spread:opp:r1:r5` | -0.178 | 0.05 |
| `player_tb|spread:same:r2:r2` | +0.265 | 0.05 |
| `player_tb|spread:same:r2:r3` | +0.277 | 0.05 |
| `player_tb|spread:same:r2:r4` | +0.283 | 0.05 |
| `player_tb|spread:same:r2:r5` | +0.287 | 0.05 |
| `player_tb|spread:opp:r2:r2` | -0.221 | 0.05 |
| `player_tb|spread:opp:r2:r3` | -0.201 | 0.05 |
| `player_tb|spread:opp:r2:r4` | -0.186 | 0.05 |
| `player_tb|spread:opp:r2:r5` | -0.176 | 0.05 |
| `player_hrr|spread:same:r3:r2` | +0.389 | 0.05 |
| `player_hrr|spread:same:r3:r3` | +0.404 | 0.05 |
| `player_hrr|spread:same:r3:r4` | +0.410 | 0.05 |
| `player_hrr|spread:same:r3:r5` | +0.413 | 0.05 |
| `player_hrr|spread:opp:r3:r2` | -0.334 | 0.05 |
| `player_hrr|spread:opp:r3:r3` | -0.309 | 0.05 |
| `player_hrr|spread:opp:r3:r4` | -0.289 | 0.05 |
| `player_hrr|spread:opp:r3:r5` | -0.274 | 0.05 |
| `moneyline|player_tb:same` | +0.250 | 0.06 |
| `moneyline|player_tb:opp` | -0.250 | 0.06 |
| `player_ks|spread:same:r2` | +0.207 | 0.05 |
| `player_ks|spread:same:r3` | +0.200 | 0.05 |
| `player_ks|spread:same:r4` | +0.188 | 0.05 |
| `player_ks|spread:same:r5` | +0.170 | 0.05 |
| `player_ks|spread:opp:r2` | -0.260 | 0.06 |
| `player_ks|spread:opp:r3` | -0.281 | 0.06 |
| `player_ks|spread:opp:r4` | -0.297 | 0.07 |
| `player_ks|spread:opp:r5` | -0.310 | 0.08 |
| `player_hr|total:r1` | +0.238 | 0.04 |
| `player_hr|total:r2` | +0.306 | 0.04 |
| `player_hit|player_ks:opp:r1` | -0.126 | 0.04 |
| `player_hit|player_ks:opp:r2` | -0.149 | 0.04 |
| `player_hit|player_ks:opp:r3` | -0.160 | 0.04 |
| `player_ks|player_tb:opp:r2` | -0.125 | 0.04 |
| `player_ks|player_tb:opp:r3` | -0.122 | 0.04 |
| `player_ks|player_tb:opp:r4` | -0.103 | 0.04 |
| `player_ks|player_tb:opp:r5` | -0.127 | 0.04 |
| `player_hrr|total:r2` | +0.379 | 0.05 |
| `player_hrr|total:r3` | +0.407 | 0.05 |
| `player_hrr|total:r4` | +0.437 | 0.05 |
| `player_hrr|total:r5` | +0.468 | 0.05 |

**Group 12 — judge fallbacks: un-runged oriented entries for unparsed rung lines (lookup tier 2). config.py:793-805.**

| pair key | rho | band |
|---|---|---|
| `player_hit|spread:same` | +0.270 | 0.08 |
| `player_hit|spread:opp` | -0.190 | 0.08 |
| `player_hr|spread:opp` | -0.190 | 0.07 |
| `player_tb|spread:same` | +0.280 | 0.06 |
| `player_tb|spread:opp` | -0.200 | 0.07 |
| `player_hrr|spread:same` | +0.400 | 0.07 |
| `player_hrr|spread:opp` | -0.300 | 0.09 |
| `player_ks|spread:same` | +0.190 | 0.07 |
| `player_ks|spread:opp` | -0.290 | 0.09 |
| `player_ks|player_tb:opp` | -0.120 | 0.05 |

**Group 13 — routed [D] cross-family splits (final-pairs judge 2026-07-10; wire-list lines 95-104). config.py:806-819.**

| pair key | rho | band |
|---|---|---|
| `player_hit|player_hr:same` | +0.030 | 0.05 |
| `player_hit|player_hr:opp` | +0.000 | 0.04 |
| `player_hit|player_hrr:same` | +0.090 | 0.05 |
| `player_hit|player_hrr:opp` | +0.000 | 0.04 |
| `player_hit|player_tb:same` | +0.050 | 0.05 |
| `player_hit|player_tb:opp` | +0.000 | 0.04 |
| `player_hr|player_tb:same` | +0.040 | 0.05 |
| `player_hr|player_tb:opp` | +0.000 | 0.04 |
| `player_hrr|player_tb:same` | +0.100 | 0.05 |
| `player_hrr|player_tb:opp` | +0.000 | 0.04 |

**Group 14 — B4 measurement addendum (2026-07-10): closes the 3 Phase-2 NOT-WIRED holes (wire-list lines 89-91) by direct measurement (docs/calibration/phase2_wire_list_addendum.txt). config.py:820-873.**

| pair key | rho | band |
|---|---|---|
| `player_hr|total:r3` | +0.357 | 0.07 |
| `player_ks|player_tb:opp:r6` | -0.127 | 0.04 |
| `player_ks|player_tb:opp:r7` | -0.128 | 0.04 |
| `player_hit|player_ks:same:r1` | +0.013 | 0.04 |
| `player_hit|player_ks:same:r2` | +0.007 | 0.04 |
| `player_hit|player_ks:same:r3` | -0.002 | 0.04 |
| `player_hr|player_ks:same` | +0.010 | 0.04 |
| `player_hrr|player_ks:same:r2` | +0.017 | 0.04 |
| `player_hrr|player_ks:same:r3` | +0.014 | 0.04 |
| `player_hrr|player_ks:same:r4` | +0.010 | 0.04 |
| `player_hrr|player_ks:same:r5` | +0.005 | 0.04 |
| `player_hrr|player_ks:same` | +0.010 | 0.04 |
| `player_ks|player_tb:same:r2` | +0.010 | 0.04 |
| `player_ks|player_tb:same:r3` | +0.007 | 0.04 |
| `player_ks|player_tb:same:r4` | +0.009 | 0.04 |
| `player_ks|player_tb:same:r5` | +0.006 | 0.04 |
| `player_ks|player_tb:same` | +0.010 | 0.04 |

Notable per-cell provenance (the cells a judge will attack first):

- `player_ks|total` **−0.25/0.12**: cluster-boot 99% CI [−0.271,−0.230]; ladder-FLAT across
  posted K lines 3.5-8.5 ⇒ one entry serves every KS rung; resolves the operator's K-line
  question (self-median convention validated) [config.py:520-523; staged_mlb_props.md [A];
  2026-07-09 measurement report "Strike ladders"]. Replaced a SIGN-WRONG flat +0.60 (point
  error ~0.85) [staged_mlb_props.md "SIGN-WRONG PRICING LIVE TODAY"].
- `player_hr|total` **+0.24/0.10**: measured +0.233 in the GAME-total frame, deliberately NOT
  the team-frame +0.367 (KXMLBTEAMTOTAL, untradeable) [config.py:524-528; staged_mlb_props.md:166-169].
- `player_hrr|total` **+0.40/0.08**: STARTERS frame — its rung ladder (Group 11) keeps that
  frame for consistency [config.py:531,786-792; phase2_wire_list.txt CONVENTION frames line].
- `rfi|total` **+0.37/0.10**: "strongest + most era-stable MLB pair measured" [config.py:532;
  staged_mlb_props.md [A]].
- `moneyline|spread:same/:opp` **±0.95/0.04**: containment-shaped, measured exact — 0
  violations in 98,980 team-games; plain 0.00/0.95 catches parse failures only
  [config.py:672-685,1086-1093; staged_mlb_props.md [A] last row].
- Neutralized [C] plain cells (ml|ks 0.00/0.30 etc.): the measured oriented values are
  ±0.24/±0.23/±0.23/±0.37 and sign-FLIP with the ML side; 0.00 with a sign-spanning band is
  strictly better than the old +0.60/0.90 (point error ≤0.37 vs up to 0.84)
  [config.py:548-568; staged_mlb_props.md [C]].
- Phase-2 rung cells (A1/A2/A4) were wired VERBATIM from the persisted wire list and
  script-verified against the loaded config (84 entries; 0 KEEP mismatches; 6 bands RAISED)
  [docs/reports/2026-07-10-phase2-wired.md B2; docs/calibration/phase2_wire_list.txt].
- B4 addendum cells closed the 3 NOT-WIRED holes by direct measurement (7/7 Phase-1
  regression anchors reproduced to ±0.0005 first; 25/25 judge windows PASS; TB r6/r7 =
  −0.127/−0.128 measured DIRECTLY, 12.7k/6.0k positives)
  [2026-07-10-phase2-wired.md B4; docs/calibration/phase2_wire_list_addendum.txt; config.py:820-873].
- **Orientation is ASYMMETRIC — :opp is never a hand negation of :same** on spread pairs
  (shared run environment attenuates :opp; gap 0.04-0.13 grows with line); both sides
  measured directly. Exception: ml|tb ±0.25 and ml×prop pairs where the exact 2-way
  complement was ALSO verified by direct measurement
  [docs/reports/2026-07-10-phase1-findings-and-phase2-handoff.md finding 1; config.py:753-756].

### 3.2 Orientation/rung resolvers and the fallback chain

MLB resolvers in `sgp.build_sgp_correlation` dispatch [sgp.py:999-1092], all fail-closed
(any parse doubt → None → next chain level, never an invented orientation):

| resolver | pair shape | orientation rule | source |
|---|---|---|---|
| `_mlb_same_player_conditional_prior` | batter prop × batter prop, SAME player | conditional-table joint → `implied_rho` at LIVE marginals; lands BEFORE the routing resolver's None seam (reviewer defect #4) | sgp.py:619-672 |
| `_mlb_prop_pair_prior` | prop × prop, distinct players | player-segment team prefixes anchored to blob ends; `:opp` = FACING for batter×KS; same-player cross-family refuses (containment owns it) | sgp.py:585-616 |
| `_mlb_winner_spread_prior` | ML × spread | both suffixes anchored (raw inequality is NOT proof — reviewer defect #3); routes reachable side-mixes to ±0.95 | sgp.py:675-703 |
| `_mlb_winner_prop_prior` | ML × prop | ML suffix side vs player side; intercepts BEFORE the generic fav/dog axis (wrong axis for MLB) | sgp.py:706-730 |
| `_mlb_spread_prop_prior` | spread × prop | spread team side vs player side; rung chain applies | sgp.py:733-764 |

**Lookup fallback chain (fail-closed, no interpolation EVER):**
`exact rung key → un-runged oriented key → plain key → flat same-event default`
[sgp.py:461-467,526-538]. The band (`mlb:`+key) always resolves at the SAME chain level as
the value [sgp.py:533-538; 2026-07-10-phase2-wired.md B1]. Rung interpolation/extrapolation
is BANNED — the tb×ks facing ladder is U-shaped (r4 dip −0.103; CI excludes 0; HR⇒TB≥4
containment dilutes the 3.5-line rung) [phase2_wire_list.txt NOT-WIRED line 90; config.py:777-785].
A partially-parseable rung collapses the whole suffix (never a partial chain)
[sgp.py:503-523]. Soccer stays un-runged by construction — the plain-level rung attempt is
MLB-gated [sgp.py:1082-1091].

**Terminal flat fallback:** an UNKNOWN-typed or untabled pair gets `same_event_rho` +0.6
with band = |0.6| + 0.30 so `corr_low` reaches ≤0 (never a confident positive)
[config.py:135,163; sgp.py:798-821]. Post-DO-1, **no same-game MLB pair ever hits the flat
default** — the 12 previously-untabled cells (~13,257 sg pairs/10h, spread×props alone
9,641/10h) were tabled 2026-07-10 [config.py:629-638;
2026-07-10-bands-routing-sweep-designs.md §3; 2026-07-10-mlb-gate-pass-and-do1.md step 1:
"flat-fallback combos 156→15 (ml|spread only)" — and DO-3 then closed ml|spread].

### 3.3 The 149-cell same-player conditional table (DO-2 + WIRE-2)

Source of truth: `pricing/conditionals_mlb.py` `SAME_PLAYER_CONDITIONALS` (lines 40-205).
Measurement: 2026-07-10 same-player pass, **1,033,852 batter-games 2005-25, PA≥1,
parsed_full × parsed_hrr 1:1 join; HRR = H+R+RBI strict** [conditionals_mlb.py:3-8].
**149 cells = 142 (2026-07-10 export, restored in full after a truncated 60-cell delivery)
+ 7 `('tb',N,'hrr',1)` WIRE-2 cells (2026-07-11 re-run, job 24844262
tmp/ph4/wire2/tb_hrr1_cells.py)** [conditionals_mlb.py:41-46,189-204;
2026-07-10-sameplayer-mlspread-wired.md "The catch worth remembering";
2026-07-11-containment-universe-sweep.md gap #3 (S41)].
**40 exact** (arithmetic containments, each verified empirically == 1.0 POOLED and on the
2021-25 era split) + **109 measured** (counts re-executed 2026-07-11).

Cell format below: `famB rungB = P(famB≥rungB | famA≥rungA)`, `*` = exact marker.
Values quoted to 4dp; full precision in the module.

**hit>=1** (n=587,975): hr1=0.1721 · hr2=0.0105 · hrr2=0.7336 · hrr3=0.4693 · hrr4=0.2778 · hrr5=0.1563 · tb2=0.5797 · tb3=0.3322 · tb4=0.2276 · tb5=0.1089 · tb6=0.0533

**hit>=2** (n=212,507): hr1=0.2550 · hr2=0.0292 · hrr2=1.0000* · hrr3=0.8483 · hrr4=0.6098 · hrr5=0.3852 · tb2=1.0000* · tb3=0.6648 · tb4=0.4085 · tb5=0.3013 · tb6=0.1475

**hit>=3** (n=48,375): hr1=0.3291 · hr2=0.0565 · hrr2=1.0000* · hrr3=1.0000* · hrr4=0.9232 · hrr5=0.7508 · tb2=1.0000* · tb3=1.0000* · tb4=0.7521 · tb5=0.5039 · tb6=0.3834

**hr>=1** (n=101,186): hit1=1.0000* · hit2=0.5356 · hit3=0.1573 · hrr2=1.0000* · hrr3=1.0000* · hrr4=0.7695 · hrr5=0.5294 · tb2=1.0000* · tb3=1.0000* · tb4=1.0000* · tb5=0.5356 · tb6=0.2828

**hr>=2** (n=6,195): hit1=1.0000* · hit2=1.0000* · hit3=0.4410 · hrr2=1.0000* · hrr3=1.0000* · hrr4=1.0000* · hrr5=1.0000* · tb2=1.0000* · tb3=1.0000* · tb4=1.0000* · tb5=1.0000* · tb6=1.0000*

**hrr>=2** (n=437,563): hit1=0.9857 · hit2=0.4857 · hit3=0.1106 · hr1=0.2312 · hr2=0.0142 · tb2=0.7176 · tb3=0.4435 · tb4=0.3058 · tb5=0.1464 · tb6=0.0716

**hrr>=3** (n=276,418): hit1=0.9982 · hit2=0.6522 · hit3=0.1750 · hr1=0.3661 · hr2=0.0224 · tb2=0.8946 · tb3=0.6550 · tb4=0.4792 · tb5=0.2314 · tb6=0.1134

**hrr>=4** (n=163,372): hit1=0.9998 · hit2=0.7932 · hit3=0.2734 · hr1=0.4766 · hr2=0.0379 · tb2=0.9715 · tb3=0.8152 · tb4=0.6360 · tb5=0.3874 · tb6=0.1914

**hrr>=5** (n=91,915): hit1=1.0000 · hit2=0.8906 · hit3=0.3952 · hr1=0.5828 · hr2=0.0674 · tb2=0.9946 · tb3=0.9198 · tb4=0.7773 · tb5=0.5695 · tb6=0.3195

**tb>=2** (n=340,876): hit1=1.0000* · hit2=0.6234 · hit3=0.1419 · hr1=0.2968 · hr2=0.0182 · hrr1=1.0000* · hrr2=0.9212 · hrr3=0.7254 · hrr4=0.4656 · hrr5=0.2682

**tb>=3** (n=195,319): hit1=1.0000* · hit2=0.7233 · hit3=0.2477 · hr1=0.5181 · hr2=0.0317 · hrr1=1.0000* · hrr2=0.9935 · hrr3=0.9270 · hrr4=0.6819 · hrr5=0.4328

**tb>=4** (n=133,796): hit1=1.0000* · hit2=0.6488 · hit3=0.2719 · hr1=0.7563 · hr2=0.0463 · hrr1=1.0000* · hrr2=1.0000* · hrr3=0.9900 · hrr4=0.7766 · hrr5=0.5340

**tb>=5** (n=64,038): hit1=1.0000* · hit2=1.0000* · hit3=0.3806 · hr1=0.8463 · hr2=0.0967 · hrr1=1.0000* · hrr2=1.0000* · hrr3=0.9988 · hrr4=0.9884 · hrr5=0.8175

**tb>=6** (n=31,347): hit1=1.0000* · hit2=1.0000* · hit3=0.5917 · hr1=0.9128 · hr2=0.1976 · hrr1=1.0000* · hrr2=1.0000* · hrr3=0.9999 · hrr4=0.9977 · hrr5=0.9367

**tb>=7** (n=14,744): hrr1=1.0000*

**tb>=8** (n=8,813): hrr1=1.0000*


Pricing rules on this table:

- `MIN_CONDITIONAL_N = 50_000`: a measured cell prices only at n ≥ 50k (operator-approved).
  **`('hit',3,…)` rows sit at n=48,375 — just UNDER — so HIT-3-conditioned cells never
  price; their reverse directions (n≥101,186) may** [conditionals_mlb.py:207-211].
- `SAME_PLAYER_RHO_BAND = 0.12` prices the pooled→single-player transfer;
  MEASURE-BEFORE-TIGHTEN (per-player spread of the implied rho unmeasured)
  [conditionals_mlb.py:213-219].
- `strongest_measured_direction` picks the larger-n direction (pure precision choice);
  exact cells never qualify (containment owns them); under-N cells never qualify
  [conditionals_mlb.py:243-265].
- `implied_rho`: monotone bisection **on the SHIPPED copula integrator** so the engine's
  joint reproduces the table's joint to tolerance and one rho prices all four YES/NO sign
  cases consistently; capped ±0.95; None on degenerate inputs [conditionals_mlb.py:268-312].
- PLAYER_KS is deliberately absent from `BATTER_FAMILIES` — a starter's Ks and a batter's
  stats are different entities; same-player KS×batter is structurally unreachable
  [conditionals_mlb.py:222-231].
- Checkpoint reproduced at wiring: P(HRR≥5|HR≥1)=0.5294 [2026-07-10-sameplayer-mlspread-wired.md;
  conditionals_mlb.py:86].

### 3.4 Containment families, collapse plan, windows, impossibility rules

**Family M1 — same-player cross-stat (S35-S41):** exact cells drive `_containment_sign`
verdicts: {sub yes, sup yes} ⇒ CONTAINMENT joint=P(subset); {sub yes, sup no} ⇒ IMPOSSIBLE
**never farmable** (MLB 48h rain scalar breaks the airtight certain-NO bar); {no,no} ⇒
CONTAINMENT joint=P(superset-no); {no,yes} ⇒ falls through — a measured reverse cell may
price it [relationships.py:264-285,577-663]. Bare measured pairs price via the sgp seam;
buried measured pairs collapse (WIRE-4); unmeasured/out-of-grid ⇒ UNKNOWN — never the
distinct-player [D] rhos (the 2026-07-10 sweep regression: distinct-player +0.01 vs
containment-shaped truth ~0.95) [relationships.py:592-596;
2026-07-10-bands-routing-sweep-designs.md §3 "REGRESSION FOUND"].

**Family M2 — ML × spread (S33, DO-3):** same-team cover⟹win = scoring containment (exact:
0/98,980 violations [staged_mlb_props.md [A]]); `{cover no, win yes}` = the S33-ny exact
window P(win)−P(cover) (2026-07-11 universal-window rule — replaced the ±0.95 copula route)
[relationships.py:717-729]; opposite-team yes×yes ⇒ IMPOSSIBLE farmable=False; unresolvable
⇒ copula `:same/:opp` ±0.95, parse failure ⇒ plain 0.00/0.95 [relationships.py:665-738].

**Family M3 — spread-N YES × total-under NO (S34, WIRE-3):** margin ≥N forces the winner
alone to score ≥N ≥M; MLB spread and total BOTH settle on the final score INCLUDING extra
innings, and extras only ADD runs, so the implication is airtight; IMPOSSIBLE,
farmable=False (48h rain scalar) [relationships.py:256-261,795-847; `_SPREAD_TOTAL_SCOPES`].
Before WIRE-3 the engine priced 26 such tape combos at +0.13 copula when V_true=0
[2026-07-11-containment-universe-sweep.md "Priced-but-WRONG" S34-yn].

**Windows (exact, band arithmetic):** every {A no, B yes} of a containment family is the
exact window P(B)−P(A) — a NESTED_BAND super-leg bare, consumed by the collapse plan when
embedded [relationships.py:88-99,537-546,1033-1057]. Engine computes p_band = P(low)−P(high),
u = u_low+u_high, declines on inverted books (p_band ≤ 0 ⇒ NoQuote, never clamped)
[engine.py:446-459].

**The collapse plan (`_collapse_containments`, 2026-07-11):** fires exactly where the old
"containment pair inside a larger combo: not modeled" UNKNOWN decline fired. Drops each
implied superset leg (Fréchet cap min(P_sub, P_sup) preserved), collapses window pairs into
band super-legs and same-player conditional pairs into super-legs whose p is the bit-identical
bare-path 2-leg joint. Fail-closed guards: cyclic implication without a kept witness ⇒
UNKNOWN; one leg in two collapse roles ⇒ UNKNOWN; **band super-leg with a same-game kept
companion ⇒ UNKNOWN; conditional super-leg with a same-game kept companion ⇒ UNKNOWN for
EVERY side mix (FIX-1)** [relationships.py:308-440; engine.py:382-595].
The FIX-1 evidence: V2 live counterexample HIT3-no × HR1-no × own-ML-yes at p=0.21/0.15/0.58
— trivariate truth 0.3451, engine-before 0.4183 (**+7.32c, sign inverted** — the kept leg's
YES-side rho applied to an anti-monotone event); engine-after: NoQuote
`skip_classifier_unknown` with the guard note [2026-07-11-judge-fixes-wire4-guard-tripwire.md FIX-1 table].
Cross-game companions (ρ=0, the bulk of the observed decliner population) stay priceable —
representing the pair by its kept leg is exact at ρ=0 [relationships.py:344-349].

**Farming policy for MLB: farmable=False EVERYWHERE.** Every MLB IMPOSSIBLE verdict
(same-player exact, ml|spread opposite-team, WIRE-3 S34, tripwire) declines instead of
farming, because the 48-hour postponement/suspension rule scalar-settles EVERY MLB family —
the airtight certain-NO bar fails [relationships.py:585-587,712-714,736-737,809;
docs/dnp_scalar_settlement.md §7.1; kalshi_robustness.md §4 row 9]. Soccer's 5 farmable
tautologies do NOT transfer [2026-07-10-bands-routing-sweep-designs.md §3 "Notable"].

### 3.5 The tripwire (FIX-4, V3 §2.4-1)

`pricing/tripwire.py` pins the 30 semantically-IMPOSSIBLE, exchange-BLOCKED shape×side-mix
cells from the 2026-07-11 containment probe as a fixture
(`tests/fixtures/ground_truth/taxonomy_impossible.json`); any same-game pair matching a
pinned cell ⇒ IMPOSSIBLE farmable=False with the countable note
`taxonomy-impossible tripwire: <shape>` — never a copula price, never a farm; a live match
is PROOF Kalshi's validator loosened [tripwire.py:1-36; relationships.py:976-993].
**The MLB cell: S42 (same-player same-stat ladders, HIT/TB/HRR/KS/HR) pinned defensively**
[2026-07-11-judge-fixes-wire4-guard-tripwire.md FIX-4 coverage list; taxonomy.json S42].
Fail-closed on fixture load failure: tripwire goes inert with ONE warning
(`taxonomy_tripwire_inert`), never turns a data problem into a classification change
[tripwire.py:269-288]. Fires at ANY combo size and beats recorded containment/window/
conditional pairs [relationships.py:987-989]. Documented residual: S49 (tennis
tournament⇒match) is cross-scope, outside the same-game tripwire [tripwire.py:32-36].

### 3.6 The structural pricer — `mlb_runs` NegBin grid + OOS gate history

**Model:** FINAL runs per team ~ NegBin(μ, k) independent across teams, tie diagonal removed
and renormalized (baseball has no ties; calibrating k on final scores puts extras' effect on
totals inside the distribution); per-game means inverted from live leg prices; inversion
requires BOTH a winner-flavored AND a total-flavored leg (a lone ML pins only the ratio)
[mlb_runs.py:1-19,95-142]. Legs representable: TeamWins / SpreadCover / GameTotalOver only —
prop legs are "not representable" ⇒ structural declines ⇒ copula [structural.py:489-519;
job-tmp ph4/mlb/mlb_backtest.json path histogram]. Dispersion **k = 3.54** (Retrosheet
2021-2025; 3.62 on 2021-24, 3.63 on 2015-19 — era-stable), band 0.30 covers the unmodeled
home/away asymmetry (k 3.37 away / 3.91 home — tickers don't reveal home side)
[config.py:1393-1415; NOTES.md K4]. Width components: leg-band re-inversions + k-band
re-pricing + misfit residual [structural.py:684-706]. Applicability: single sport, no period
legs, ALL legs in ONE same-game group [structural.py:726-746].

**Gate history:**
- **GATE PASSED 2026-07-06** (tools/validate_mlb_runs_oos.py; SBR closing-odds archive,
  k from 2015-19 train, test = 2021 season n=2,351): hw×over 1.36134 vs v1 1.36300;
  hw×runline **1.00824 vs 1.12151** (v1 had no calibrated MLB ml|spread — flat 0.6
  documented "badly wrong"); triple **1.71126 vs 1.88090**. Also measured: v1's pooled
  ml|total −0.05 LOSES to independence OOS ⇒ the runs grid supersedes it same-game
  [config.py:1400-1408; NOTES.md K6].
- **Current-era re-checks on Kalshi's own prices:** n=728 settled 2026 games — win×over
  statistical TIE (expected; no discriminating power) [NOTES.md K8]; clean full-ladder
  re-validation n=877: run-line cover **0.99297 vs 1.41018**, triple **1.67925 vs 2.24235**
  ⇒ PASS on the decisive metrics (win×over demoted to diagnostic per operator Decision A)
  [NOTES.md K9, L8].
- **Open calibration item:** the 2026-07-10 gate run flagged the NegBin grid at 5.53¢
  median error / +4.21¢ bias (n=232, config-independent) [2026-07-10-mlb-gate-pass-and-do1.md
  follow-up 1]; the Phase-4 capstone re-measured the config-independent structural slice at
  1.10¢ — own calibration pass queued either way [2026-07-11-phase4-capstone.md watch list].

### 3.7 Terminal UNKNOWN-decline behavior

UNKNOWN never defaults: `Relationship.kind == UNKNOWN` ⇒ `NoQuote(SKIP_CLASSIFIER_UNKNOWN)`
with the classifier's notes [engine.py:181-182]; property tests pin that the UNKNOWN branch
cannot reach CreateQuote at normal width [CLAUDE.md quiet-failure defense #2]. UNKNOWN
**leg typing** (vs relationship) does NOT decline — it prices at the flat prior with the
widened band; this is deliberate (typing is a structure hint, not a validity check)
[legtypes.py:6-8; sgp.py:818-821; 2026-07-11-judge-fixes FIX-4 "UNKNOWN-typed legs verified
on the LIVE path"].

### 3.8 Quote-construction MLB specifics

- **DO-6 basket width:** ≥8 legs AND all NO AND one single prop family ⇒
  +`basket_width_extra_cc` 250 (2.5¢) width, applied after all normal width, widen-only —
  motivated by the measured +25-35¢/$1 flat-0.6 overbid on 8-16-leg all-NO baskets
  (empirical P(all-no) 0.158-0.356 vs flat-copula 0.506-0.604; measured-ρ copula reproduces
  the 16-leg joint to −0.0003) [engine.py:49-76,269; config.py:1244-1252;
  2026-07-09-mlb-measurement-tranche.md "money finding" table].
- **Sell-only:** `sell_parlays_only: true` in both env YAMLs — yes_bid forced 0, we can only
  be long NO; engine chokepoint corrects any non-zero YES [config/demo.yaml, config/prod.yaml;
  engine.py:274-295]. UN-GATED for fills since the demo combo settled and paid exactly
  $1.00 = 1−V (combo_no_pays_complement promoted true)
  [docs/reports/2026-07-10-demo-combo-settled.md; kalshi_robustness.md §4 row 1].
- Width core: base 200cc + 100cc/leg + uncertainty scale + size adder + time adder;
  longshot floor (fair <0.15 ⇒ unc ≥ 25% of fair) [config.py:1234-1268; engine.py:597-611].

---

## 4. COMBO TYPES ON TAPE — Phase-4 capstone (2026-07-06→11 window)

Artifacts: job-tmp `ph4/mlb/{gather_meta.json, mlb_backtest.json, ph4_stats.json,
mlb_by_composition.json, mlb_by_leg_count.json, analyze.log, ph4_analysis.log}`; published in
docs/reports/2026-07-11-phase4-capstone.md + the v3 gate artifact
(docs/reports/2026-07-10-mlb-backtest-gate.html).

**Funnel (every N of M named)** [ph4/mlb/gather_meta.json]:
16,053,982 RFQ rows scanned → 852,940 MLB-strict combos → 201,780 priceable (order books
attachable) → 7,737 strictly-pregame → **6,969 with pre-game prints** (the 768 remainder =
pregame combos with no pre-game print to score against; header line "n=6969 combos w/
pre-game prints", ph4/mlb/analyze.log). Also named: 5,022 in-play-only, 189,021 no-prints,
90,576 resolved, 3,293 candidates, 20,000 audit draws (9,468 hits).

**Pricing-path disposition of the 6,969** [ph4/mlb/mlb_backtest.json rows,
`path_promoted` histogram]: **6,337 copula · 224 structural (mlb_runs grid) · 408
structural-declined→copula** (every decline reason = a prop/RFI leg "not representable in
the margin/total model" — by design, §3.6). unknown_carrying n=0 [ph4/mlb/analyze.log].

**Headline accuracy (snapshot mode, promoted vs legacy flat-0.6)** [ph4/mlb/ph4_stats.json]:

| bucket | n | med\|err\| | bias | w2 | legacy |
|---|---|---|---|---|---|
| ALL | 6,969 | **0.34¢** | −0.43¢ | 93.3% | 0.35¢ / 88.2% |
| prop-carrying | 1,305 | **0.66¢** | −0.85¢ | 83.6% | **1.37¢ / 57.6%** |
| props-only | 699 | 0.56¢ | −0.93¢ | 82.3% | 0.70¢ / 71.8% |
| mixed (props+lines) | 606 | 0.78¢ | −0.75¢ | 85.2% | 3.08¢ / 41.1% |
| game-lines-only | 5,664 | 0.31¢ | −0.33¢ | 95.6% | 0.31¢ / 95.2% |
| pure-ML | 4,139 | 0.30¢ | −0.37¢ | 97.0% | identical (blast-radius proof) |

Per-print mode: overall n=53,022, 0.38¢ med, **98.6% w2** (legacy 97.3%); prop-carrying
n=3,046, 0.62¢, 92.0% w2 (legacy 69.3%) [ph4/mlb/ph4_stats.json per_print].

**Every composition observed (snapshot, promoted / legacy w2)** [ph4/mlb/mlb_by_composition.json
+ fams histogram from mlb_backtest.json]: moneyline 4,139 (97.0/97.0) · moneyline+total 622
(89.1/89.1) · player_hr 303 (99.3/93.7) · moneyline+spread 281 (94.7/94.0) · total 217
(92.2/92.2) · player_ks 193 (65.3/59.1) · spread 166 (96.4/96.4) · spread+total 148
(88.5/79.7) · moneyline+player_ks 141 (92.2/51.8) · moneyline+spread+total 91 (96.7/91.2) ·
moneyline+player_hit 36 (86.1/**25.0**) · rfi 35 · player_hit 35 · moneyline+player_ks+total
34 · moneyline+player_hr 30 · moneyline+player_hrr 30 · player_ks+total 30 ·
player_hrr+player_ks 28 · moneyline+player_hit+player_ks 26 · player_hrr 26 · moneyline+rfi
25 · player_ks+spread 22 · player_hit+player_ks 22 · moneyline+player_hrr+player_ks 15 ·
player_tb 14 · (remaining long tail in mlb_by_composition.json). Key same-game cells:
hit_ml_same_game promoted 0.92¢/82.7% vs legacy 8.45¢/14.7%; ks_ml_same_game 0.87¢/84.1% vs
4.22¢/29.1%; hr_ml_same_game 0.50¢/89.7% vs 4.34¢/28.2% [ph4/mlb/ph4_analysis.log KEY CELLS].

**Settlement calibration (thermometer, never a refit trigger):** MLB window ran
favorite-hot again — 22.2% realized YES vs 17.5¢ mean fair (n=3,128 resolved) → pooled
multi-week ledger item [2026-07-11-phase4-capstone.md verification 4;
feedback rule: no refit on P&L windows]. Settlement P&L sweep (n=5,604 resolved combos,
11.07M contracts, ~52 game-dates): props-only structurally profitable at every markup 0-3¢
(+$13,452 at 0¢ / +$6,907 at 1¢, YES-hit 2.8% at 0¢ rising to **19% at 1¢** — corrected per judge F3; 2.9% is the actual-makers' figure); game-lines −$1.23M market-wide
(favorite-hot week — our 0-markup sim −10.2¢/ct beat actual makers' −11.3¢/ct on
like-for-like flow); the −$1.23M was audit-verified against the authed tape (41,363/41,363
prints matched, taker_outcome_side 0 disagreements)
[docs/reports/2026-07-10-mlb-settlement-pnl-sweep.md + ADDENDUM].

**Declines with reason histogram:** the only classifier-decline species in MLB flow are the
fail-closed guards (same-game band/conditional companions — the FIX-1 label
`containment-collapse-conditional-companion` in mlb_backtest paths) and unmeasured
same-player cells; capstone fresh run: unknown_carrying = 0 priced-set members
[ph4/mlb/analyze.log; 2026-07-11-judge-fixes NEXT STEPS "backtests"]. Gather-level
exclusions are the funnel rows above (no-prints/in-play/no-books), plus per-print
zero-residual accounting proven on the WC bucket (723,167 = 656,555 + 65,633 + 397 + 582)
as the accounting standard [2026-07-11-phase4-capstone.md "Zero-residual print accounting"].
Historical honesty: MLB's DO-9 counters print decline histograms by design; the WC analyzer
gained the histogram after the capstone (process lesson codified)
[2026-07-11-phase4-capstone.md "Historical audit"].

---

## 5. THE KALSHI BOUNDARY

### 5.1 Constructible vs blocked (exchange-matrix cells, MLB)

Source: docs/calibration/containment_probe/exchange_matrix.json (86 demo constructions,
probe log P01-P42/F/R/M/N; 194 cells probed universe-wide: 59 ALLOWED / 118 BLOCKED / 17
UNPROBEABLE) [exchange_matrix.json meta; 2026-07-11-containment-universe-sweep.md numbers].
**MLB caveat: direct MLB probes were blocked by demo's finalized events (status gate
precedes the semantic check, proven by control probe P37/P38) — tape evidence and
structural analogs substitute** [exchange_matrix.json meta.error_vocabulary;
2026-07-11-containment-universe-sweep.md].

| cell | verdict | evidence basis |
|---|---|---|
| S33 (spread⟹win) yy | BLOCKED `conflicting_leg_outcomes` | analog (soccer P10 + WNBA P32; 0 strict prints in 8.7M rows) |
| S33 yn / nn | BLOCKED `invalid_parameters` | **metadata, direct**: KXMLBGAME `is_yes_only=true` in both open collections (pinned by probe P02) |
| S33 ny (win-not-by-N window) | **ALLOWED** | tape: 4 window prints (KXMLBSPREAD×KXMLBGAME) — priced exactly by our S33-ny window |
| S34 (spread⟹total) yy / nn / ny | ALLOWED | tape prints (NYY4-no+TOTAL-15-no; HOU2-no+TOTAL-6-yes) + analog P11 |
| S34 yn | BLOCKED `conflicting_leg_outcomes` (analog F04) — **but 26 pre-tightening tape combos exist and can re-RFQ**; our WIRE-3 rule now declines them IMPOSSIBLE | exchange_matrix S34; 2026-07-11-containment-universe-sweep "Priced-but-WRONG" |
| S35-S40 (same-player cross-stat) strict yy/yn | UNPROBEABLE (demo finalized); lean BLOCKED — 0 strict prints in 8.7M rows vs 712 non-strict | exchange_matrix S35 |
| S35-S40 no/no + no/yes | ALLOWED | tape same-player prints (HARPER HR-2-no × HIT-3-no; KURTZ HR-1-no × HIT-1-yes) — priced by the conditional table / windows |
| S41 (tb⟹hrr1) | no-side mixes ALLOWED (tape species prints); yes-side UNPROBEABLE lean BLOCKED | exchange_matrix S41 |
| S42 (same-stat ladders) yy/nn | BLOCKED `duplicated_legs` (family-generic, analog probes) ; yn UNPROBEABLE lean BLOCKED; ny ALLOWED-by-analog (MLB prop events size_max=null), **0 MLB bands in 3.02M tape combos** | exchange_matrix S42; 2026-07-10-one-leg-per-ladder-rule.md |
| S43 (rfi⟹total-over-0.5) | LINE-DEAD (no total-1 market lists) | taxonomy.json S43 |

The side-aware ladder rule generally: same-side rungs blocked (`duplicated_legs`, 0 in
3.02M combos), YES-low+NO-high bands allowed where size_max permits; MLB TOTAL events carry
size_max=1 so MLB bands don't build today — the nested-ladder registry deliberately
withholds TOTAL ("an entry would only add unprobed farm surface")
[2026-07-10-one-leg-per-ladder-rule.md; relationships.py:190-195].

### 5.2 Settlement-scope dependencies and which of OUR rules lean on each

| Kalshi settlement fact | our rules leaning on it | pin | exposure if silently changed |
|---|---|---|---|
| MLB spread AND total settle on the final score INCL. extra innings; extras only ADD runs | WIRE-3 S34 impossibility; every total-pair ρ frame | MLB settlement audit [2026-07-10-baseball-vs-soccer-template-scorecard.md §"Settlement edge-case audit" 1; kalshi_robustness.md §4 row 6] | soccer-side farm risk only; MLB is decline-only |
| **48h postponement/suspension rule scalar-settles EVERY MLB family** (~1-2% of game-days) | farmable=False on ALL MLB impossibles; rain-scalar combos excluded from P&L sweeps | docs/dnp_scalar_settlement.md §7.1; scorecard finding 3 | none (fail-safe direction) [kalshi_robustness.md §4 row 9] |
| MLB prop DNP is STRICT: binary needs a START + ≥1 PA (batter) / 1 pitch (starter); pinch/relief stats do NOT count | WIRE-4/DO-2 conditional joints (measured probabilities, drift not breakage) | dnp_scalar_settlement.md §7.1 item 2; kalshi_robustness.md §4 row 8 | conditional joints biased until re-measured; calibration report catches (slow) |
| Official-MLB stat definitions (TB accrue only via hits; HRR counts hits); ticker `-N` = N+ (floor_strike N−0.5) | S41 exact cells; every same-player exact cell; rung keys | 1,033,852-game re-run ==1.0 pooled AND 2021-25 (ph4/wire2/tb_hrr1_cells.json); floor_strike in market_rules.json snapshot (jobs dir, NOT in repo) | rung-convention drift shifts every exact cell by one rung; reconciliation HALT post-fill [kalshi_robustness.md §4 row 7] |
| Combo NO pays $1 − product; early-NO finalization | sell-only quoting; farm P&L math (soccer) | REAL settlement 2026-07-10, paid exactly $1.00 = 1−V [tests/fixtures/ground_truth/conventions.json; 2026-07-10-demo-combo-settled.md] | reconciliation HALT on first mismatch [kalshi_robustness.md §4 row 1] |
| TOTAL settles up to 15 days post-game vs 3 for GAME/SPREAD/RFI | EV-ledger/markout code must not assume same-day settlement | scorecard finding 5 | ledger timing only |
| Shortened-game totals go scalar unless the over clinched (~0.3-0.5% of games) | measurement provenance caveat on totals ρ | scorecard finding 4 | tiny systematic |

### 5.3 Tripwire coverage + validator-change exposure (kalshi_robustness.md)

Verdicts [job-tmp ph4/wire2/kalshi_robustness.md §0]:
- **Blocked shapes cannot reach the engine — CONFIRMED**: RFQs only enter via
  exchange-minted combo markets; we never mint; quotes go by rfq_id; engine demands combo
  metadata (9-step trace, "hole search result: none found") [kalshi_robustness.md §1].
- **Validator TIGHTENS: nothing breaks** — no runtime code depends on constructibility;
  cost is revenue-only. Observed live: the validator DID tighten between Jul-07 and Jul-11
  (team-corners farm + inverted bands now block) — any ALLOWED evidence older than ~Jul-09
  is refutable [kalshi_robustness.md §3; 2026-07-11-containment-universe-sweep.md "Farm inventory"].
- **Validator LOOSENS:** 30 dangerous cells (certain-$0 paper would price at 5-35c ref fair)
  — ALL now covered by the shipped tripwire (FIX-4 implements robustness §2.4-1); zero of
  the 30 were WIRE-3-covered before it [kalshi_robustness.md §2.2, §2.4-1;
  2026-07-11-judge-fixes FIX-4]. MLB-specific dangerous row: S42 ladders pinned; S33/S34
  impossible mixes are engine-decline (impossible-noquote bucket, rows S33-yn/S33-oppfarm/
  S34-yn) [kalshi_robustness.md §2.1].
- **Pre-fill alarm gap:** unit tests pin OUR code, not Kalshi's rulebook; the only live
  alarms are the post-fill reconciliation HALT and the slow calibration report. Recommended
  (approved as judge-mandated for the tripwire; rules-pin sweep still queued): weekly
  rules-hash sweep per wired family + floor_strike runtime assertion + collections-metadata
  diff [kalshi_robustness.md §4 "Missing pins"; 2026-07-11-judge-fixes NEXT STEPS].

---

## 6. HOLES — missing, weak, assumed, queued

**Fail-closed decline surface (deliberate coverage losses):**
1. Band/window super-leg with any same-game companion ⇒ UNKNOWN (band-vs-neighbour ρ
   unmeasured) [relationships.py:1038-1051]; conditional super-leg with a same-game KEPT
   companion ⇒ UNKNOWN for every side mix (FIX-1) [relationships.py:414-421]. The two
   guarded WC-tape combos have exact-algebra fixes designed but NOT engine-surfaced
   (operator decision open) [2026-07-11-phase4-capstone.md "Residual audit"].
2. Same-player cells that never price: `('hit',3,…)`-conditioned rows (n=48,375 <
   MIN_CONDITIONAL_N 50k); rungs outside the export grid (hit≥4, hr≥3 conditioning, hrr≥1
   conditioning, tb 7-8 beyond the hrr-1 column) ⇒ UNKNOWN decline
   [conditionals_mlb.py:20-27,207-211].
3. Teammate rungs NOT wired (hit r4, hrr r1 — Kalshi-real but outside B4 scope) fall to the
   un-runged `:same` aggregates; TB rungs >7 unseen on tape stay on the un-runged `:opp`
   fallback [config.py:828-836; phase2_wire_list_addendum via 2026-07-10-phase2-wired.md B4].
4. Doubleheader-spanning combos ⇒ UNKNOWN/wide [2026-07-09 measurement report].

**Unmeasured / prior-only cells still in the table:**
5. `moneyline|player_tb` plain band 0.30 is the widest-sibling span, not measured (MEASURE
   — DO-8) [config.py:561-568]; `moneyline|player_hr:same` 2+ rung (~+0.27) unmeasured,
   band-covered [config.py:603,728-731]; `rfi|spread` 0.00/0.15 is a labeled prior
   (geometry of measured ml|rfi 0.00) [config.py:653-661]; `extras|*` pairs are hand/measured
   game-level entries for a family with 0 tape combos [config.py:510-511; staged_mlb_props.md table].
6. SAME_PLAYER_RHO_BAND 0.12: per-player spread of the implied rho unmeasured
   (MEASURE-BEFORE-TIGHTEN) [conditionals_mlb.py:213-218].

**Model-level residuals:**
7. `mlb_runs` grid calibration pass queued (gate-run 5.53¢/+4.21¢ n=232 vs capstone 1.10¢
   config-independent slice — sample-dependent, unresolved) [2026-07-10-mlb-gate-pass-and-do1.md
   follow-up 1; 2026-07-11-phase4-capstone.md watch list]. Home/away k asymmetry unmodeled
   (banded) [mlb_runs.py:12-14].
8. Pairwise-only joints (copula) omit true 3-way+ dependence — WIDEN-ONLY stance on
   multi-leg player combos [results_baseball.md "Cross-check vs peers"].
9. Watch list (pre-registered, never refit on P&L): illiquid HRR/HIT/KS prop-only parlays —
   the only cells ≥1.5¢ (max 2.01¢, n=26), fat-markup-shaped bias; ML×2 parlays 18.3¢
   median winner's-curse gap (markup-policy, not correlation)
   [2026-07-11-phase4-capstone.md watch list; 2026-07-10-mlb-gate-pass-and-do1.md follow-up 2].
10. MLB window favorite-hot (22.2% YES vs 17.5¢ fair; game-clustered p=0.063 from the
    settlement forensics) — pooled multi-week question, not a refit trigger
    [2026-07-11-phase4-capstone.md verification 4; 2026-07-10-mlb-settlement-pnl-sweep.md].

**Exchange-boundary residuals:**
11. Direct MLB constructibility probes remain a standing open item (demo listed no live MLB
    events; S35 strict-zero-prints suggests validator blocking, unconfirmed) — re-probe when
    demo lists MLB [2026-07-11-containment-universe-sweep.md finding 5; 2026-07-10-one-leg-per-ladder-rule.md NEXT STEPS].
12. Rules-pin sweep + floor_strike runtime assertion + collections-metadata diff:
    recommended, NOT implemented (operator approval pending) — the pre-fill alarm gap for
    settlement-rule drift [kalshi_robustness.md §4 Missing pins 1-3; 2026-07-11-judge-fixes NEXT STEPS].
13. `market_rules.json` / `collections.json` baselines live only in jobs tmp, not yet
    promoted to docs/calibration/ [kalshi_robustness.md §4 row 2 + NEXT STEPS].
14. Rain-scalar reactive stance: decided on soccer's ~0% rate, MLB trigger is ~1-2% of
    game-days; operator re-affirmation flagged; expect HALT_RECONCILIATION_MISMATCH on the
    first scalar settlement of a filled combo [dnp_scalar_settlement.md §7.1 item 3].
15. No real doubleheader yet observed on tape — DH ticker mechanics confirmed-from-rules
    only; verify on first scheduled DH [scorecard "Standing"].

**Gating/ops residuals:**
16. No per-sport pricing kill switch (whitelist is collection-prefix only; MLB rides
    KXMVESPORTS/KXMVECROSSCATEGORY prefixes shared with other sports) [scorecard §8 + NEXT STEPS].
17. KXMVEMLB whitelist entry is dead (matches zero live collections) [scorecard §8].
18. Mixed-flow foreign-leg gap: 52.69% of MLB combos carry foreign legs; **KXWNBAPTS is the
    #1 foreign gap** (17,146 same-game pairs at flat +0.6, distinct players, exact
    MLB-props-shaped fix); NBA/NFL/NHL props arrive at season start
    [2026-07-10-phase1-findings-and-phase2-handoff.md D3].
19. Standing watches: monthly MVE-collection eligibility re-scan (F5 blockers must ship
    before any F5 eligibility flip — they did; TEAMTOTAL eligibility would un-strand
    +0.367/−0.380) [2026-07-09 measurement report NEXT STEPS "Standing"].
20. Backtests share one caveat: clearing = fair + winning markup, so w2-vs-clearing is a
    markup-confounded ruler; settlements are the unbendable one (mixed bucket settled-YES
    15.28% vs 15.25¢ fair) [2026-07-11-phase4-capstone.md headline + verification 4].

---

## NEXT STEPS

- **Runs next (module-relevant):** #14 demo fill e2e (sell-only un-gated) → #15 weekly
  sweep/calibration cadence (game-clustered, bucket-split) → #16 MLB blind test → E
  decisions (markup pooled multi-week; per-sport kill switch; prod gates)
  [2026-07-11-phase4-capstone.md NEXT STEPS].
- **Owner: operator** — approve/deny rules-pin sweep + floor_strike assertion (pre-fill
  alarms); rule on engine-surfacing the two exact-algebra collapse TODOs; re-affirm the
  rain-scalar reactive stance; decide farm pursuit given validator shelf-life (soccer only —
  MLB never farms).
- **Owner: next probe session** — direct MLB constructibility when demo lists MLB; promote
  market_rules/collections baselines into docs/calibration/.
- **Queued measurements:** DO-8 leftovers (`ml|tb` oriented already measured; plain-band
  tighten), per-player conditional-rho spread, mlb_runs grid recalibration, KXWNBAPTS
  foreign-gap fix.
