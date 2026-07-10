# Reports channel — combomaker (kalshi-combos-TWO)

Dated log of every crucial **test, update, and finding**. Purpose: if this
session compacts or a new Claude/operator session opens, this channel makes them
**100% informed** on what has been done, what is in flight, and what is blocked —
without re-deriving anything.

**STANDING RULE (operator directive 2026-07-08): keep this channel current.**
Every crucial update, test, or finding gets a dated `.md` here *as it happens* —
not batched at the end. One file per test/update, newest indexed first. Cross-link
`CLAUDE.md` (agreement), `NOTES.md` (exchange mechanics + assumption audits),
`docs/calibration/` (prior-setting). This is the reporting-discipline rule; it is
also recorded in `CLAUDE.md` and operator memory so it survives compaction.

Naming: `YYYY-MM-DD-<sport-or-scope>-<what>.md`. Each report ends with a
**NEXT STEPS** footer (who owns what, decisions owed).

---

## Master status (newest first)

| Date | Area | State | One-line |
|------|------|-------|----------|
| 2026-07-10 | **LOOK-AHEAD VERDICT: all 6 pre-registered predictions TRUE** | **artifact confirmed as THE cause; WC survived; NO pickoff risk** | fresh 3-bucket × 2-policy reruns (13.5M rows, 82k tape fetches, independent verifier zero-discrepancy): honest MLB book = 0.34¢ med/−0.39 bias/94% w2; pure-ML 99% w2; HR+ML −0.59¢ & HIT+ML −1.07¢ (old scares were artifact); WC backbone reproduces at 19× sample; cross-sport independence EXACT; gate PASS under both policies; artifact v2 re-issued, 18.3¢ retracted — [report](2026-07-10-lookahead-verdict.md) |
| 2026-07-10 | **Steps 4-5 wired: same-player conditionals + ml\|spread** | **DONE — suite 1095/0; mlb table 68 entries** | 142-cell conditional table (33 exact ==1.0 verified); ml\|spread containment, farmable=False per rain-scalar; differential surgical; CAUGHT+FIXED a prompt-slice truncation (60/142 cells) — pass data by file, never inline — [report](2026-07-10-sameplayer-mlspread-wired.md) |
| 2026-07-10 | **Bands + routing WIRED (steps 2-3)** | **prop-carrying now 0.97¢ — sub-cent vs market** | 85/85 tape bands price exactly (was +2.6-6.1¢ over); routing 25/25 parity to 1e-12; differential: 43,199 transitions ALL on routed keys, game-lines bit-identical; suite 1055/0 — [report](2026-07-10-bands-routing-wired.md) |
| 2026-07-10 | **MLB backtest gate PASS + DO-1/DO-7 wired** | **steps 0-1 done, differential ZERO-violation** | promoted table beats flat-0.6 on prop-carrying (1.25 vs 2.22¢, 62% vs 48% w2) with zero game-line regression, n=6,647 pregame clearings; DO-1 → 43 entries (only ml\|spread untabled); NEW follow-up: mlb_runs grid own error 5.53¢/+4.21 bias; ML×2 winner's-curse 18.3¢; artifact w/ by-composition+leg-count tables — [report](2026-07-10-mlb-gate-pass-and-do1.md) |
| 2026-07-10 | **Bands + routing + sweep designs** | **REVIEWED, wire-ready — order awaiting operator go** | band mispricing quantified on LIVE mids (+6.64¢/+55% on narrow bands; Fréchet can't catch it — exact band IS the Fréchet bound); team parse proven unambiguous on 1,549 markets (naive approach failed 80/445); sweep: 9×45 matrix — 12 untabled cells (13.3k pairs/10h), spread×props = 11.5× the ml|spread flow, **REGRESSION: [D] promotion made same-player pairs worse**; 4 review defects caught (staged test wrong, wc_backtest mirror atomicity, parser reuse, seam order); recommended order = backtest FIRST — [report](2026-07-10-bands-routing-sweep-designs.md) |
| 2026-07-10 | **MLB promotion WIRED** | **9/9 classification + 32-entry table LIVE in config** | full suite 1013 passed; KS×TOTAL now prices −0.25/0.12 through live dispatch (was +0.6/0.90); 8-leg HR basket 28 pairs @ +0.03; 3 live misclass bugs fixed; soccer/NFL untouched; caught pair_key sort trap + a dropped hr\|hrr entry; tape backtest = next validation gate, then resolvers → containment — [report](2026-07-10-mlb-promotion-wired.md) |
| 2026-07-10 | **One-leg-per-ladder rule** | **VERIFIED (side-aware)** | same-side rungs exchange-blocked (400 duplicated_legs; 0 in 3.02M combos); **yes-low+NO-high BANDS ALLOWED** — 114 real corners-band combos on tape, currently priced flat +0.6 vs exact P(low)−P(high) arithmetic → containment-phase item; strict cross-series containment also blocked (conflicting_leg_outcomes) — [report](2026-07-10-one-leg-per-ladder-rule.md) |
| 2026-07-10 | **Baseball vs soccer-template scorecard + MLB settlement audit** | **2/10 sections done, 4 partial, 4 not started** | soccer template measured from code (96 ρ entries · 15 resolvers · 4 containments · 5 tautologies · 9-rung validation); MLB: knowledge ~90% confirmed, wiring ~30%; ONLY GAME/TOTAL/SPREAD (winner+total shape) are 100% end-to-end accurate today; settlement audit: totals INCLUDE extras (measurements correctly framed), prop DNP stricter than soccer (must START; pinch/relief don't count), 48h rain rule scalar-settles EVERY family → dnp_scalar doc falsified for MLB (~1-2% rainout rate); MLB already REACHES the pricer (no sport kill switch; KXMVEMLB whitelist entry is dead) — [report](2026-07-10-baseball-vs-soccer-template-scorecard.md) |
| 2026-07-09 | **RESUME STATE — read first** | HANDOFF | where we left off + what's running + next actions — [report](2026-07-09-session-state-resume.md) |
| 2026-07-09 | **MLB measurement tranche — DONE, 0 refuted** | **every pair measured · both blockers resolved · basket overbid quantified** | flat +0.6 overbids 8-16-leg all-NO HR baskets by **+25-35¢/$1** (measured ρs reproduce the 16-leg joint to 0.0003); player_hr\|game_total = +0.24 (the critical gap, measured); K pairs ladder-FLAT → **operator's K-line question RESOLVED** (self-median fine); batter rungs drift → per-rung entries; event_mutually_exclusive = false on all 6 prop families (**baskets NOT gated**, merely uncalibrated); same-player rungs = zero flow, cross-family same-player = containment; final judge-amended table in `docs/calibration/staged_mlb_props.md` — [report](2026-07-09-mlb-measurement-tranche.md) |
| 2026-07-09 | **MLB/baseball SGP finalization — phase 1 DONE** | **classification VERIFIED · ρ triple-verified** | exactly 9 combo-eligible families (KS/HIT/HR/HRR/TB/RFI all UNKNOWN→+0.6 today); KXMLBTOTAL = GAME total, TEAMTOTAL untradeable (strands +0.367/−0.380 as reference-only); 3 live misclass bugs found (F5TOTAL/F5SPREAD/SERIESGAMETOTAL); shipped ρ reproduced EXACTLY at 3 independent levels, 21-season stable; NEW blocker: event_mutually_exclusive gates all baskets; staged code in `docs/calibration/staged_mlb_props.md` (rule-8 gated) — [report](2026-07-09-mlb-classification-and-rho-verification.md) |
| 2026-07-09 | Harness: pre-game filter + tape backfill | **DONE + live-validated** | `gather` now sources clearings from Kalshi's trade tape (gap-free → backfills the 10h poller stall by construction) + drops any print at/after a leg's estimated kickoff (strictly pregame, `--pregame-hours` default 2.5); engine untouched — [report](2026-07-09-pregame-filter-and-tape-backfill.md) |
| 2026-07-09 | Overnight prod recorder | **RUNNING** | `run --env prod --mode observe` capturing WC/MLB combos settling Jul 9-11; alive+rising 19:56Z; DB combo_trades = 168,707 trades / 8,820 distinct combos (Jul 9 19:07Z); 08:20-18:11 poller gap SELF-HEALED on restart — see resume doc |
| 2026-07-09 | WC backtest harness (fast + zero-bias) | READY | `tools/backtests/wc_backtest.py` — WC-strict, cached/memoized; pricer reads inputs.pkl only (never the maker price); clearings from tape + strictly-pregame; run after Jul 11 settlement — see resume doc |
| 2026-07-09 | System atlas | DONE | living navigable top-down map — architecture, RFQ→quote flowchart, per-sport correlations, honest status — `docs/atlas.html` |
| 2026-07-09 | DNP / scalar settlement | DONE (decision: build nothing) | soccer scorer no-show → last fair price (scalar, Kalshi-verified); ≈EV-neutral, rare, fail-safe → handle reactively — `docs/dnp_scalar_settlement.md` |
| 2026-07-09 | Demo combo round-trip | **TRADE DONE, settlement PENDING ~Jul 10-11** | maker landed LONG NO on a demo combo; sell-only + direction verified live; watch settlement to confirm combo_no_pays_complement — [report](2026-07-09-demo-combo-roundtrip.md) |
| 2026-07-08 | Fix: sell_parlays_only (one-sided) | DONE | combo quotes force yes_bid=0 (parlay seller only); engine-boundary guarantee; reviewed self+agent; 984 tests green (re-confirmed by fresh run 2026-07-09: 984 passed / 3 deselected / 0 failed); INERT until combo_no_pays_complement verified — [report](2026-07-08-sell-parlays-only-fix.md) |
| 2026-07-08 | Combo YES/NO sides + fade config | DONE | Kalshi API+demo ledger: NO = whole-combo complement (not per-leg neg); parlay-seller = **`yes_bid=0`** (operator's "quote YES only" was BACKWARDS); single-sided exposure is guaranteed; our `construct_quote` emits both bids today (exposed) — [report](2026-07-08-combo-yes-no-side-mechanics.md) |
| 2026-07-08 | Combo taker cash-out exposure | DONE | verified TWICE (2nd blind, live-API proof): no partial cash-out; taker exit never unwinds our fill; early NO-determination is full not partial — [report](2026-07-08-combo-taker-cashout-exposure.md) |
| 2026-07-08 | Soccer settlement P&L | DONE | WC-only real-outcome backtest: makers +3.05¢/ct, our sell-book +EV peaking at ~1¢ markup, NO-fade flow toxic; UCL excluded — [report](2026-07-08-soccer-settlement-pnl.md) |
| 2026-07-08 | Soccer backtest vs clearing | DONE | 1,480 resolved-price combos, our fair vs maker **quote** (not fair); median \|err\| 1.9¢ — [report](2026-07-08-soccer-backtest-vs-clearing.md) |
| 2026-07-07 | Final RFQ blind test | DONE | 28 real combos priced blind by agents; matched maker on calibrated pairs — [report](2026-07-07-final-rfq-blind-test.md) |
| 2026-07-07 | Soccer 1H cluster + corners calibration | DONE | 36-entry 1H cross-type block + corners pairs calibrated & tested — [report](2026-07-07-soccer-calibration-and-farming.md) |
| 2026-07-07 | Impossible-combo farming audit | DONE | probed Kalshi live: **no reachable farm beyond the 5 tautologies** — [report](2026-07-07-soccer-calibration-and-farming.md) |
| 2026-07-05 | Overnight combo-trade recorder | superseded | replaced by the 2026-07-09 prod observe recorder (top row) |

## Blocked / open gates

- **Phase 2.5 ground-truth combo pass** — combo NO-payout convention
  (`combo_no_pays_complement`) still null; the demo combo round trip WAS executed
  2026-07-09 (maker long NO, $0.50) — only the settlement observation remains
  (~Jul 10-11; earliest possible: ~2026-07-10T03:05Z if the LAA leg loses →
  early-NO fires full settlement immediately).
- **Soccer blind re-test** — operator's planned gate before replicating the
  calibrate→audit→test workflow to other sports (MLB, basketball, esports).
- **UCL/UEL/UECL gated OFF** — two-legged-tie legs decline (`filters.py`); NOT
  re-enabled. WC (`KXWC*`) is the only live soccer family.

## Standing constraints a new session MUST load first

- **HARD boundary:** never open/read/list any path named `kalshi-combos`
  **without** the `-TWO` suffix (`CLAUDE.md` rule 1).
- **Prod DB is READ-ONLY:** `data/combomaker-prod.sqlite3`, open with
  `file:...?mode=ro` (never `immutable=1`, never write). ~31 GB.
  `rfqs.market_ticker` is **not** indexed (3-way joins stall); `combo_trades.ticker`
  and `would_quotes.rfq_id` are.
- **Secrets via env only** (`KALSHI_PROD_*` in gitignored `.env`); public
  `GET /markets/{ticker}` needs no auth and carries settlement (`status`,`result`).
- **Source-of-truth rule:** verify Kalshi tickers/odds/legs/settlement against the
  tape or API — never assume from memory.
- **Farming rule (`CLAUDE.md` defense #2):** `farmable=True` ONLY on airtight
  logical tautologies, never metadata-dependent.
- Run Python with the project venv: `.venv/Scripts/python.exe`.

## Where the analysis lives (job-tmp; regeneratable)

Backtest scripts + caches were built in the session job-tmp
(`$CLAUDE_JOB_DIR/tmp`, ephemeral). Key artifacts and how to rebuild:

| Artifact | What | Rebuild |
|----------|------|---------|
| `gather.pkl` | 33 MB cache: `ticker_legs`, `ticker_wqs`, `trades`, `soccer_tickers` (1,480 pure-soccer combos, 41,127 trades, 374,445 would_quotes) | index-aware split queries over the prod DB |
| `settle.pkl` | `{market_ticker: {status,result,close_time}}` for 336 distinct legs | `fetch_settle.py` → public `GET /markets/{ticker}` |
| `resolved.pkl` | 172 resolved combos + their trades | `phaseA_maker_pnl.py` |
| `phaseA_maker_pnl.py` / `phaseB_sim.py` | settlement P&L + markup sim | see 2026-07-08 settlement report |

**NEXT STEP (owner: next session):** persist these scripts into the repo
(`tools/backtests/`) so they outlive the job-tmp — flagged, not yet done.
