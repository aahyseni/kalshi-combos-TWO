# 2026-08-12 — La Liga / MLS / UECL validated against the API + RETRACTION: the "zero outside demand" census

**Operator (verbatim):** "thats 100% not true that theres no demand for other
sports, we probbaly just log rfqs we can price… validate my findings and
available combos for those league against a source of truth (kalshi docs and
api) to verify those are all the markets for those leagues."

**The operator was right. Retraction first, then the validation.**

---

## RETRACTION — "zero non-allowlisted RFQ demand" (this morning's sports report)

**WRONG:** the earlier claim that the `rfqs` store records every RFQ pre-filter
("zero rows outside the allowlist = zero taker demand"). I verified the wrong
seam: `quote_app.handle_rfq_record_after` does record before the *pricing*
filters — but upstream, **`rfq/intake.py:186–195` has a firehose fast-path
gate** that drops any `rfq_created` containing ANY leg whose ticker doesn't
start with `filters.allowed_leg_series_prefixes` — before parsing, before the
recorder, counted only in a metrics counter (`rfq.dropped_series_fastpath`).
The WS channel itself is global (pinned AsyncAPI: "Market specification
ignored — you get all RFQs"), but **our store sees only what we already
price.** The store can NEVER measure outside demand.

**Exchange truth (authed `GET /communications/rfqs?status=open`, read-only,
20,000 open RFQs pulled 8/12 ~12:15 ET):** all 20,000 are combos
(`mve_selected_legs` on every row; zero single-market RFQs), and the open set
is FULL of non-MLB demand right now:

| leg series (open-RFQ set) | count | note |
|---|---|---|
| tennis (ATP/Challenger/WTA/ITF/doubles) | ~4,200 | biggest non-MLB block |
| KXCLUBFGAME (club friendlies) | 3,341 | active now |
| KXLEAGUESCUP GAME/TOTAL/SPREAD | 2,471 | MLS×Liga MX tournament, live now |
| **KXUECLGAME** | **927** | the operator's Conference League — matches 8/12–13 |
| WNBA (GAME/SPREAD/PTS/REB/…) | ~2,000 | mid-season |
| KXMLBF5 / F5TOTAL / F5SPREAD | ~1,150 | the family we removed 7/31 |
| KXUEFASCGAME/ADVANCE/TOTAL | ~660 | Super Cup today |
| KXLALIGA GAME/TOTAL/BTTS | 84 | season opens 8/15 — already requested |
| KXMLS GAME/TOTAL/BTTS/SPREAD | 77 | regular slate resumes post-Leagues-Cup |
| KXUFCFIGHT | 208 | |

Raw example (open right now): 2-leg `KXUECLGAME-26AUG12FCCDEB-FCC` ×
`KXUEFASCADVANCE-26AUG12PSGAVL-PSG`, 91.15 contracts, in
**KXMVECROSSCATEGORY-R — the exact collection we already quote.** We are
declining-by-blindness ~25%+ of the open combo universe on soccer alone.

**Corrections applied:** the sports-review report's demand section is
superseded by this report (edit note added); `tools/diagnostics/
rfq_series_census.py` rewritten — the store-based census only measures
WITHIN-allowlist mix; the demand detector now polls the REST open-RFQ set.
Root-cause of my error: I did not enumerate the intake path end-to-end before
declaring a negative — the enumerate-every-bucket rule, violated and
re-learned.

---

## VALIDATION — the operator's three leagues vs the API (all source-of-truth, 8/12)

### 1. Series + market types (GET /series category=Sports; GET /markets status=open)

| league | operator asked | API truth (open markets right now) |
|---|---|---|
| **La Liga** | ML, total, spread, BTTS | **ALL FOUR LIVE**: `KXLALIGAGAME` 57 mkts/19 events (3-way ML with TIE), `KXLALIGATOTAL` 36/6 (rungs), `KXLALIGASPREAD` 24/6, `KXLALIGABTTS` 6/6. Near-term fixtures (8/15–17) carry full sets; further-out GAME-only so far. |
| **MLS** | ML, total, spread, BTTS | **ALL FOUR LIVE**: `KXMLSGAME` 135/45, `KXMLSTOTAL` 90/15, `KXMLSSPREAD` 60/15, `KXMLSBTTS` 15/15. |
| **UECL** | "moneyline only for now" | **SUPERSEDED — ALL types live**: `KXUECLGAME` 84/28, `KXUECLTOTAL` 170/28, `KXUECLSPREAD` 114/28, `KXUECLBTTS` 28/28, plus `KXUECLADVANCE` 56/28 (two-legged-tie advance — the WC advance machinery's exact shape). The app view undersold it; the API shows full coverage on the 28 qualifying ties (matches 8/12–13). |

Also existing for these leagues but OUT of the requested scope (classify as
UNKNOWN blockers at wiring): 1H/2H sets, SCORE (correct score), MOV, FTTS,
TEAMTOTAL/TEAMPOINTS, LEADER/RELEGATION/TOP-N futures, EPL-style goalscorer
props. They decline loudly, never price.

### 2. Combo eligibility (GET /multivariate_event_collections/{ticker} + reverse probe)

- Both quoting collections (`KXMVECROSSCATEGORY-R`,
  `KXMVESPORTSMULTIGAMEEXTENDED-R`) list **947 associated events** each,
  including `KXMLSGAME/SPREAD/TOTAL/BTTS`, `KXLEAGUESCUP*`, `KXCLUBFGAME`,
  `KXLIGAMXGAME`, and (below the top-40 cut) the La Liga and UECL events —
  membership PROVEN by the reverse probe: `?associated_event_ticker=` for
  `KXUECLGAME-26AUG12FCCDEB`, `KXLALIGAGAME-26AUG15ALAGET`,
  `KXMLSGAME-26AUG15ATLNYRB`, `KXMLSTOTAL-…`, `KXLALIGABTTS-…` each return all
  three collections.
- **DRIFT FLAG: a third collection exists — `KXMVECROSSCATEGORY-SHARD1-R`**
  (the 7/10 baseline expected exactly two; `tools/mvec_eligibility_scan.py`
  would exit 1 today). Wiring item: verify nothing in our pipeline assumes the
  two-collection set (RFQ handling keys on leg series, but check for
  hardcoded collection tickers), and note the shard in NOTES.md.
- Also spotted in the membership: **KXNFLGAME/SPREAD/TOTAL (16 events each)
  are ALREADY combo-eligible** — NFL demand will ride these collections in
  September; and KBO/NPB (Korean/Japanese baseball) are eligible too.

### 3. Demand for the three leagues specifically (open-RFQ set)

UECL: **927 RFQs** open now (qualifying today/tomorrow). La Liga: 84 already,
3 days before La Liga's opener. MLS: 77 (its slate resumes after Leagues Cup —
whose own 2,471 RFQs are MLS×LigaMX teams). Demand validated for all three,
with UECL immediate.

## The wiring plan (per the operator: same as World Cup — Dixon-Coles + already-measured coefficients)

Confirmed reusable as-is: the Dixon-Coles structural pricer (OOS-gated on
8,980 held-out CLUB games — it is a club-soccer model), the measured soccer
pair table (btts|ml oriented, btts|total, ml|total, spread⟹win containment,
corners cells, etc.), quote construction, farming rules, the risk engine
(untouched, per the 8/31 freeze — a new sport enters through pricing +
allowlist only).

League-specific work items (the compressed playbook pass, Stages 0–2 + 6–7):

1. **Classification keywords** for `KXLALIGA*`, `KXMLS*`, `KXUECL*` GAME/
   TOTAL/SPREAD/BTTS (+ UNKNOWN blockers for SCORE/MOV/FTTS/TEAMTOTAL/1H/2H/
   futures — keyword ORDER load-bearing, longest first; `KXMLS` must not
   swallow `KXMLSAST`/`KXMLSCUP`/`KXMLSJOIN` etc.).
2. **Game-code parsing + start times:** club codes are `YYMMMDD+TEAMS` with
   NO kickoff time (`26AUG12FCCDEB`) unlike MLB — pregame anchor comes from
   `min(close_time, expected_expiration_time)` with the soccer offset, exactly
   the WC path; verify offsets per league on real metadata.
3. **HOME/AWAY frame — NEW for club soccer:** the WC was neutral-venue; club
   DC needs home-advantage assignment. Pin which game-code side is home from
   real schedules + one settled market per league (the L1 frame-trap
   protocol: convention owned in ONE place).
4. **Settlement scopes (operator rules text owed):** league ML/total/spread/
   BTTS should be regulation-only (no ET in league play — simpler than WC);
   UECL qualifying GAME = that leg's regulation result vs the tie — pin from
   the Kalshi rule text per series (never assumed); ADVANCE stays out of
   scope initially per operator ("ML only" — though the API offers more).
5. **Allowlist + firehose:** add the wired series to
   `filters.allowed_leg_series_prefixes` (this BOTH admits them to pricing
   AND un-blinds the intake gate) — the per-sport kill switch, LAST step,
   after the classification judge + backtest gate + bit-exact differential
   prove untouched sports identical.
6. **SHARD1 check** (above) + `mvec_eligibility_scan.py` baseline refresh.

## NEXT STEPS

- **Me (next session): execute the wiring** — Stage 0 recon artifacts (ticker
  shapes from tape/API — done above in part), classification + conventions +
  parity + judge + backtest gate, per the playbook, La Liga + MLS (4 families)
  and UECL (GAME only) first; UECL matches run 8/12–13, La Liga opens 8/15.
- **Operator asks (front-load):** the rules text for the three leagues'
  GAME/TOTAL/SPREAD/BTTS series (settlement scope pinning); confirmation to
  keep UECL at ML-only despite TOTAL/SPREAD/BTTS being live on the API.
- **Bookkeeping:** census tool rewritten (REST demand detector); sports-review
  report demand section superseded; NOTES.md rows owed at wiring: SHARD1
  collection, club game-code convention, home/away frame, league settlement
  scopes.
