# 2026-07-22 — MLB new-prop series (OUTS / RBI / SB): live Kalshi source-of-truth verification

**Scope / blast radius:** READ-ONLY verification. No live module, config, or test
was modified. All facts below come from the **live Kalshi PROD API**
(`https://api.elections.kalshi.com/trade-api/v2`, authenticated with
`KALSHI_PROD_*`) and the repo's own read-only drift tool
(`tools/mvec_eligibility_scan.py`). Purpose: clear the "confirm the KXMLBOUTS/RBI/SB
ticker line grammar + settlement + combo-eligibility against the live series rules
before wiring" gate flagged in `docs/calibration/staged_mlb_new_props.md` §NEXT STEPS,
`docs/dnp_scalar_settlement.md` line 331, and the settlement facts owed to
`docs/reports/2026-07-22-scalar-dnp-as4-gating-decision.md`.

**Verdict (one line): WIRE — all three series exist with the assumed tickers, the
`N+` / `floor_strike = N−0.5` rung grammar is confirmed on every open market, all
three are LIVE combo-eligible in both MLB combo collections, the MLB-anchored
keyword candidates are collision-clean, and their settlement follows the exact same
scratched/DNP → fair-market-price scalar pattern the already-wired MLB props use
(no NEW settlement hole beyond the known AS4 scalar surface).**

---

## 1. Series existence + exact ticker/rung grammar (CONFIRMED)

`GET /series/{ticker}` returned HTTP 200 for all three assumed tickers. Source of
truth = live API, not memory/fixtures.

| assumed ticker | REAL ticker | title | scope | settlement sources |
|---|---|---|---|---|
| KXMLBOUTS | **KXMLBOUTS** ✓ | Pro Baseball Outs Recorded | "Outs Recorded" | ESPN, Fox Sports, MLB |
| KXMLBRBI | **KXMLBRBI** ✓ | Pro Baseball RBIs | "RBIs" | ESPN, Fox Sports, MLB |
| KXMLBSB | **KXMLBSB** ✓ | Pro Baseball Stolen Bases | "Stolen Bases" | ESPN, MLB |

All three: `category=Sports`, `tags=["Baseball"]`, `fee_type=quadratic`,
`fee_multiplier=1`.

### Rung grammar — VERIFIED on EVERY open market (0 mismatches)

Market ticker shape: `KX<FAM>-<GAMECODE>-<TEAM><PLAYERTOKEN><id>-<N>` where the
**final `-N` integer is the rung line** and `yes_sub_title = "<Player>: N+"`.
`strike_type="structured"`, `floor_strike = N − 0.5`, `cap_strike = null` (floor-only
"N+", no range/between markets).

Real examples (pulled live 2026-07-22):

| market ticker | yes_sub_title | floor_strike | rung N |
|---|---|---|---|
| `KXMLBOUTS-26JUL222010DETCHC-DETKMONTERO54-15` | `Keider Montero: 15+` | 14.5 | 15 |
| `KXMLBOUTS-26JUL221540CINSEA-CINBSINGER51-18` | `Brady Singer: 18+` | 17.5 | 18 |
| `KXMLBRBI-26JUL221510WSHCOL-COLWCASTRO3-2` | `Willi Castro: 2+` | 1.5 | 2 |
| `KXMLBRBI-26JUL221510WSHCOL-COLWCASTRO3-1` | `Willi Castro: 1+` | 0.5 | 1 |
| `KXMLBSB-26JUL221540ATHAZ-ATHJHEIM15-1` | `Jonah Heim: 1+` | 0.5 | 1 |

Full open-market scan (2026-07-22), invariant `int(ticker.rsplit("-",1)[-1]) ==
floor_strike + 0.5` checked on all markets:

| series | open mkts | strike_type | cap_strike | rung lines present | invariant mismatches |
|---|---|---|---|---|---|
| KXMLBOUTS | 27 | all `structured` | 0 | 15,16,17,18,19 | **0/27** |
| KXMLBRBI | 200 | all `structured` | 0 | 1,2,3 | **0/200** |
| KXMLBSB | 125 | all `structured` | 0 | 1 (only) | **0/125** |

**⇒ The pricer's rung convention is exactly right:** rung = Kalshi ticker line
integer, `N+`, `floor_strike = N − 0.5`. This is the SAME "-N means N+" convention
as the already-wired `KXMLBHR/HIT/KS/TB` (line-suffix N+), NOT the `N−0.5` "over"
convention of TOTAL/SPREAD. Matches `staged_mlb_new_props.md` §4 exactly. **SB is
1+-only in the live book** (only rung line 1 exists), confirming the staged
"SB 1+-only" decision. RBI ladders 1+/2+/3+ and OUTS shows deep rungs (15+–19+;
consistent with a starter-outs market) — both monotone-rung families as staged.

Note: the token before the final rung integer (`MONTERO54`, `WCASTRO3`, `JHEIM15`)
is a **player-id disambiguator**, itself ending in digits — the rung parser must take
the LAST hyphen segment, not the last run of digits. (The repo's existing MLB prop
parsing already does this for HR/HIT/etc.; same shape.)

## 2. Settlement rules — pulled from the live API `rules_secondary` (the KXWCGAME way)

The authoritative machine-readable settlement text is the market object's
`rules_primary` / `rules_secondary` (same fields used to verify KXWCGAME). Verbatim
`rules_secondary` (2026-07-22 live):

**KXMLBOUTS** (starter-pitcher unit):
> Player Participation & Settlement Criteria
> If <pitcher> is scratched or is not a starting pitcher, the market will resolve to the **fair market price**.
> If <pitcher> is not a starting pitcher but later enters the game, the market will resolve to the **fair market price**, relief appearances will not count towards this market.
> If <pitcher> is a starting pitcher and records at least one pitch the market will settle based on outs recorded.

**KXMLBRBI** and **KXMLBSB** (batter unit, identical wording):
> Player Participation & Settlement Criteria
> If <player> is scratched or not included in the starting lineup, the market will resolve to the **fair market price**.
> If <player> starts the game but does not record a plate appearance, the market will resolve to the **fair market price**.
> If <player> is not in the starting lineup but later enters the game, the market will resolve to the **fair market price**, pinch hit at bats will not count towards the market.
> If <player> is in the starting lineup and records at least one plate appearance the market will settle based on <RBIs | stolen bases> recorded.

The `product_metadata.important_info.markdown` (series level) says the same in one
line: *"If this player/pitcher does not start … or starts but does not record at least
one plate appearance / pitch, this market will resolve to Fair Market Price."*

### DNP behavior — the load-bearing finding for the parallel scalar/DNP gate

- **DNP is NOT graded-No and NOT void-with-refund. It is a SCALAR settlement to
  "fair market price"** (`v_i = s`, the last fair price), exactly the `s`-scalar
  model of `docs/dnp_scalar_settlement.md` §1–6.
- **Binary settlement requires START + participation:** batter must be in the
  **starting lineup AND record ≥1 plate appearance**; pitcher must be the **starting
  pitcher AND throw ≥1 pitch**. Scratched, started-with-0-PA, and
  entered-without-starting all scalar-settle. **Pinch-hit / relief stats explicitly
  do NOT count** even if the player appeared.
- **This is byte-for-byte the same DNP pattern as the already-wired MLB props.** I
  re-pulled `KXMLBHR`, `KXMLBKS`, `KXMLBTB` live and their `rules_secondary` is the
  identical clause. So OUTS/RBI/SB add **NO new settlement mechanic** — they inherit
  the exact scalar/DNP surface the shipped-and-gated props already have.

### Rain-shortened / suspended games

`rules_secondary` does not spell out rain/suspension per-series (as with the other
MLB props, the binding rain/postponement text lives in the contract-terms PDF, not
the API rules field). The repo's prior settlement audit is authoritative and
**already covers these three by family class** (`docs/dnp_scalar_settlement.md` §7.1,
sourced from `docs/reports/2026-07-10-baseball-vs-soccer-template-scorecard.md` +
all MLB contract PDFs):

- **The 48-hour postponement/suspension rule scalar-settles EVERY MLB family** —
  moneyline, total, spread, RFI, and every prop. **No MLB leg type is strictly
  binary.** OUTS/RBI/SB fall under the same rule: a game not completed within 48h →
  the market settles to fair market price (scalar), not void/refund, not graded-No.
- **Frequency ~1–2% of MLB game-days** hit the postponement/suspension path (vs the
  soccer 0-in-4,913). This is the AS4 scalar-receivable surface already decided in
  `docs/reports/2026-07-22-scalar-dnp-as4-gating-decision.md`.

Contract-terms PDFs exist at
`https://kalshi-public-docs.s3.amazonaws.com/contract_terms/{MLBOUTSRECORDED,MLBRBI,MLBSB}.pdf`
(URLs from the series objects). WebFetch could not decompress the FlateDecode
streams and a local paren-extraction returned only the embedded font subset, so the
**verbatim rain-clause text from these three specific PDFs remains UNVERIFIED**;
however the family-class rule (§7.1) applies to all MLB families by the audit above,
so this does not block wiring — it stays fail-closed/scalar exactly as AS4 already
assumes. Recommend a one-time PDF text-extraction (pdfminer/pdftotext) to pin the
exact 48h wording per series if the operator wants it belt-and-suspenders.

**Precise per-series settlement summary for the gate:**

| condition | KXMLBOUTS | KXMLBRBI | KXMLBSB |
|---|---|---|---|
| player starts + participates (≥1 pitch / ≥1 PA) | binary on stat | binary on stat | binary on stat |
| scratched / not starting lineup | **scalar → fair mkt price** | **scalar → fair mkt price** | **scalar → fair mkt price** |
| starts but 0 PA / (pitcher) enters as reliever | scalar (relief ≠ count) | scalar (0-PA) | scalar (0-PA) |
| enters without starting | scalar (relief ≠ count) | scalar (pinch-hit ≠ count) | scalar (pinch-hit ≠ count) |
| game postponed/suspended, not done in 48h | **scalar → fair mkt price** | **scalar → fair mkt price** | **scalar → fair mkt price** |
| void / refund behavior | none (scalar, not void) | none (scalar, not void) | none (scalar, not void) |

## 3. Combo-eligibility — CONFIRMED LIVE (they ARE offered as combo legs)

`GET /multivariate_event_collections/{ct}` for the two known MLB-bearing combo
collections. Both are **open** (`status=open`, `size_min=2`, `is_ordered=false`) and
each carries 533 associated events. Enumerating the series prefixes of every
`associated_event`:

| collection | KXMLBOUTS events | KXMLBRBI events | KXMLBSB events |
|---|---|---|---|
| `KXMVESPORTSMULTIGAMEEXTENDED-R` | 14 | 15 | 15 |
| `KXMVECROSSCATEGORY-R` | 14 | 15 | 15 |

They sit alongside the already-wired MLB families (KXMLBGAME/HIT/HR/HRR/KS/RFI/
SPREAD/TB/TOTAL) in both collections. **⇒ All three can appear as legs in a combo
RFQ right now.** This changes the wiring calculus decisively: they are combo-eligible
TODAY, unlike the 2026-07-09 state when RBI/SB/OUTS were deliberately left UNKNOWN
because they weren't yet eligible.

Independently corroborated by the repo's own drift guard `tools/mvec_eligibility_scan.py`
(read-only, hits the same public API), which fired exactly as designed:

```
MLB-bearing collections: 2
  KXMVECROSSCATEGORY-R:  ... OUTS=14, RBI=15, RFI=38, SB=15, ...
  KXMVESPORTSMULTIGAMEEXTENDED-R: ... OUTS=14, RBI=15, RFI=38, SB=15, ...
!!!! MVEC ELIGIBILITY DRIFT -- MLB FAMILY SET CHANGED
!!!! ADDED beyond baseline: KXMLBOUTS (KNOWN ALARM FAMILY) (n=28 events, in both)
!!!! ADDED beyond baseline: KXMLBRBI  (KNOWN ALARM FAMILY) (n=30 events, in both)
!!!! ADDED beyond baseline: KXMLBSB   (KNOWN ALARM FAMILY) (n=30 events, in both)
!!!! ACTION: re-audit classification/rho-table/settlement before any MLB combo quoting
```

The tool's baseline was the 9-family set `{GAME, TOTAL, SPREAD, KS, HIT, HR, HRR,
TB, RFI}`; OUTS/RBI/SB were pre-registered in its `KNOWN_ALARM_FAMILIES` — i.e. the
repo *predicted* these three would be the ones to appear, and they have. This report
is the "re-audit before quoting" the tool demands.

## 4. Collision scan (`src/combomaker/pricing/legtypes.py`)

Method: ran the LIVE `classify_leg` / `classify_sport` against real tickers, and
scanned the candidate anchors against the **live combo-leg universe** — the 56
distinct series prefixes that actually co-occur as combo legs in the two collections
above (the correct universe: only these can ever be a sibling leg).

**Pre-wiring state (correct fail-closed):** all three currently classify **UNKNOWN**
(sport tags MLB correctly). UNKNOWN → widen/no-quote, never masquerade. Good.

**Existing MLB keyword set** (from `_KEYWORDS`, checked in order): blockers
`LEADERMLB, MLBHRDERBY, SERIESGAMETOTAL, F5TOTAL, F5SPREAD` → UNKNOWN; then
`MLBHRR→player_hrr, MLBHR→player_hr, MLBHIT→player_hit, MLBKS→player_ks,
MLBTB→player_tb, MLBRFI→rfi`. All MLB anchors are already `KXMLB`-prefixed
(the universe-scan lesson: bare "HR"/"KS"/"HIT"/"TB"/"RFI" collide with dozens of
non-MLB series).

Candidate anchors `MLBOUTS` / `MLBRBI` / `MLBSB`:

| test | result |
|---|---|
| each anchor matches its own real prefix | `MLBOUTS`⊂KXMLBOUTS, `MLBRBI`⊂KXMLBRBI, `MLBSB`⊂KXMLBSB ✓ |
| anchor cross-collision with any OTHER of the 56 live prefixes | **NONE** (all clean) |
| substring overlap with any existing keyword | **NONE** |
| adding the 3 anchors changes classification of any existing prefix | **NO** — only the 3 intended prefixes newly match |

**The bare-substring trap is real and the MLB-anchoring avoids it** (this is the
exact danger the directive called out):

- bare `SB` would hit **`KXMLSBTTS`** (an MLS soccer BTTS market — "ML**SB**TTS"
  contains "SB"!) in addition to `KXMLBSB`. Verified live: `KXMLSBTTS-...`
  currently classifies `btts`/soccer (via the `BTTS` keyword which precedes any MLB
  anchor) — and since the proposed anchor is `MLBSB` (not `SB`), `MLBSB` is **not** a
  substring of `KXMLSBTTS` (`...MLSBTTS` has `MLSB`, not `MLBSB`), so no regression.
- bare `RBI` / `OUTS` happen to be clean in the current universe, but MLB-anchoring
  is still correct (matches the shipped convention and is future-proof).
- `MLBSB` is NOT a substring of `KXMLBSPREAD` (`MLBSP…`) or `KXMLBTB` — no MLB
  intra-family collision. None of the three anchors contain each other.

**Placement:** insert the three in the MLB-props block (grouped with the other
`MLB*` anchors, before the generic `TOTAL/SPREAD/GAME` keywords). Ordering *among*
the MLB anchors is immaterial for these three (none is a substring of another MLB
keyword and none contains another). Recommended: right after `MLBRFI`.

## 5. RECOMMENDED classification spec (WIRE)

Add to `LegType`:

```python
PLAYER_OUTS = "player_outs"  # starting-pitcher outs recorded N+ (KXMLBOUTS)
PLAYER_RBI  = "player_rbi"   # batter RBIs N+ (KXMLBRBI)
PLAYER_SB   = "player_sb"    # batter stolen bases N+ (KXMLBSB; 1+-only live)
```

Add to `_KEYWORDS`, in the MLB block right after `("MLBRFI", LegType.RFI)` and
BEFORE `("TOTAL", LegType.TOTAL)`:

```python
("MLBOUTS", LegType.PLAYER_OUTS),
("MLBRBI",  LegType.PLAYER_RBI),
("MLBSB",   LegType.PLAYER_SB),
```

- **Rung grammar:** rung = Kalshi ticker line integer, `N+`, `floor_strike = N−0.5`
  (identical to HR/HIT/KS/TB). Parse the rung from the **last hyphen segment** of the
  market ticker, not the last digit-run. RBI joins the rung-keyed families (1+/2+/3+);
  OUTS needs per-rung keys for the ks pair (`:rN`, ladder NOT flat — see
  `staged_mlb_new_props.md` §1); SB is 1+-only.
- **Sport:** already tags MLB via the existing `MLB` sport keyword — no change needed.
- **Ship classification + the staged pair-table/bands/conditional-cells TOGETHER**
  (hard rule: a classified-but-unmodeled family would price on the flat prior).
- **Settlement:** no new code path — these are scalar/DNP-able exactly like the
  existing MLB props; the `combo_no_pays_complement` / fractional-`V` handling already
  covers them. The only settlement item is the AS4 scalar-receivable monitor already
  decided in `docs/reports/2026-07-22-scalar-dnp-as4-gating-decision.md`.

This must land with the reviewed wiring step (`staged_mlb_new_props.md` §4 seams:
same-pitcher ks×outs `:same` routing, RBI/SB same-player conditional cells, OUTS/RBI
rung grammar) and the rule-8b tape-replay parity check — **not** as a standalone
classifier edit.

## 6. Evidence log (all read-only)

- Auth: `KALSHI_PROD_API_KEY_ID` + `kalshi_private_key.pem`, RSA-PSS per
  `docs/api-notes/auth-env.md`. Balance probe returned $2,384.78 (consistent with the
  reconciled all-time figure) — auth good.
- Host note: the recommended `external-api.kalshi.com` host 404'd on `/exchange/status`
  and `/portfolio/balance`; the **alternative prod host `api.elections.kalshi.com`
  served all endpoints 200**. (Worth noting in `NOTES.md` — the "recommended" host
  did not route these read paths for this key/region.)
- `GET /series/{KXMLBOUTS,KXMLBRBI,KXMLBSB}` → 200, real tickers confirmed.
- `GET /markets?series_ticker=…&status=open` → rung grammar scan (0 invariant
  mismatches across 352 open markets).
- `GET /markets/{ticker}` → `rules_primary`/`rules_secondary` settlement text.
- `GET /multivariate_event_collections/{KXMVESPORTSMULTIGAMEEXTENDED-R,
  KXMVECROSSCATEGORY-R}` → combo-eligibility (14/15/15 events each).
- `tools/mvec_eligibility_scan.py` (read-only, public API) → drift fired for all 3.
- Prod tape (`data/combomaker-prod.sqlite3`, opened `mode=ro immutable=1`): schema
  confirmed (`rfqs.legs_json`, `combo_trades`); a full-table `legs_json LIKE`
  activity count was launched but the 119 GB WAL scan did not finish in-session —
  **not load-bearing**, since live collection membership already proves eligibility.

## NEXT STEPS

- **Runs next (owner: next engineering session):** execute the reviewed wiring step
  per `staged_mlb_new_props.md` §4 — add `PLAYER_OUTS/PLAYER_RBI/PLAYER_SB` + the
  `MLBOUTS/MLBRBI/MLBSB` keywords (§5 above), the staged pair table + bands +
  conditional cells, the same-pitcher ks×outs `:same` routing fix, and OUTS/RBI rung
  grammar — classification and table TOGETHER; then the rule-8b tape-replay backtest
  vs the flat-UNKNOWN baseline + a parity check. After merge, **bump the
  `mvec_eligibility_scan.py` baseline** from 9 to 12 families and update
  `staged_mlb_new_props.md` status to WIRED.
- **Owner (operator):** (a) sign off that OUTS/RBI/SB carry the SAME scalar/DNP
  surface as the shipped MLB props (they do — verbatim `rules_secondary`), so the
  AS4 ACCEPT-AS-IS + monitor decision covers them with no new gate; (b) confirm the
  OUTS per-rung-key convention (staged) vs a single wide-band entry.
- **Optional / low (owner: eng):** one-time `pdftotext`/pdfminer extraction of the
  three contract-terms PDFs to pin the verbatim 48-hour rain/suspension clause per
  series (currently covered by the family-class §7.1 audit; UNVERIFIED at the
  per-PDF-text level only).
- **Decision owed:** none blocking. Live facts fully support wiring; nothing graded
  do-not-wire.
```
