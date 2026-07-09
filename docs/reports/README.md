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
| 2026-07-09 | **RESUME STATE — read first** | HANDOFF | where we left off + what's running + next actions — [report](2026-07-09-session-state-resume.md) |
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
