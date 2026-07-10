# RESUME STATE — read this first (2026-07-09 ~18:45 UTC)

**Purpose:** if the terminal closed, the context compacted, or a new agent/session
picks this up — this is where we are and exactly what to do next. Standing rule
(CLAUDE.md #7): keep this current so any handoff is seamless.

## TL;DR

Sell-only parlay-seller mode is **shipped + verified**; a live demo combo NO
position is **placed and awaiting settlement (~Jul 10–11)** to confirm the last
blocker (`combo_no_pays_complement`); an **overnight prod observe recorder is
RUNNING** to capture WC/MLB combos settling Jul 9–11 for the next backtest. Pricing
is a working skeleton (correlations incomplete); risk+MC are coded but unverified.

## 🔴 RUNNING NOW — the overnight recorder (started 2026-07-09 05:45 UTC)

- **What:** `run --env prod --mode observe` — records the RFQ tape + would-quotes +
  combo trades to `data/combomaker-prod.sqlite3`. Read-only against Kalshi (NEVER
  quotes). Confirmed writing live (WC France–Morocco scorer/advance combos, MLB, UFC).
- **Command to (re)start it** (from repo root):
  ```
  nohup .venv/Scripts/python.exe -m combomaker.ops.cli run --env prod --mode observe \
    > "$CLAUDE_JOB_DIR/tmp/recorder.log" 2>&1 &
  ```
- **⚠ 2026-07-09 18:11 UTC — RESTARTED.** The prior recorder's combo-trade POLLER
  silently STALLED at 08:19 UTC (RFQs kept recording; `combo_trades` stopped for
  ~10h) — an uncaught exception killed the poll task while the WS loop lived on.
  **Root cause = TODO** (harden the poller: catch+restart the task). Killed it,
  restarted clean → log now `$CLAUDE_JOB_DIR/tmp/recorder2.log`; combo_trades
  capture RESUMED.
  **UPDATE — the gap SELF-HEALED (2026-07-09 ~19:10 UTC).** On restart, recorder2's
  REST `get_trades` poller pulled the *backlog*; trades keep their original
  `created_time`, so the 08:20–18:11 window is **no longer empty** — a read-only
  count found **7,987 trades** inside it. So there is **no 10h hole** in
  `combo_trades` (completeness within the window is not proven from counts alone,
  but see the safety net). Live health verified 2026-07-09 19:56 UTC:
  `combo_trades.stored` 9,171→33,788, `rfq.would_quote` 42k→169,790,
  `rfq.created` 54k→218,078 (all rising).
  **DB state (measured 2026-07-09 19:07 UTC, read-only, 2.6s):** `combo_trades` =
  **168,707 trades / 8,820 distinct combos** (ALL sports), span Jun 29 → now. Note
  the `combo_trades.stored` COUNTER (33,788) counts store *ops* incl. re-stored
  backfill duplicates, so it runs ABOVE the distinct row count — don't read it as
  rows.
  **Safety net:** a future combo-poller stall no longer costs the backtest any
  clearings — the harness sources them from Kalshi's trade tape (gap-free,
  complete per combo). BUT the **would_quotes / marginal-snapshot** feed has **no
  backfill**; that is the reason to keep the recorder alive.
- **Verify it's alive — VIA THE LOG, not the DB** (a `max(rowid)` read HANGS in
  game hours; the recorder holds the write lock):
  - it emits `observe_metrics` every ~60s. Check **`combo_trades.stored` is
    RISING** (not just `rfq.created`): `grep observe_metrics <recorder log> | tail -1`.
    A flat `combo_trades.stored` across two snapshots = the poller stalled again →
    restart. Also watch for `combo_trades_captured` events.
  - process exists (`tasklist | grep -i python`); DB file mtime advancing.
- **RUN ONLY ONE.** Two recorders fight over the write lock. Kill extras.
- **After a hard kill** the DB can leave a hot `-journal`; a read-only open then
  errors "attempt to write a readonly database" — harmless: the next recorder
  (read-write) recovers it automatically on start.
- **Goal:** keep it running through **Jul 11** so we capture WC combos that settle
  Jul 9–11 → then backtest P&L + our price-quoting on that fresh, settled set.
- **Known noise (OK):** frequent `live_subscribe_failed` / `skip_leg_stale` WS
  warnings — the WS leg-book feed is flaky, so many would-quotes get skipped, but
  the **RFQ tape (`rfqs`) and executed `combo_trades` are recording fine** (the
  combo-trade poller is REST-based, independent of the WS). That's the backtest data.
- **If it died:** restart with the command above. It's idempotent (appends to the
  same store). No harm from a gap beyond lost minutes.

## 🟠 IN FLIGHT — demo combo settlement watch (~Jul 10–11)

Live demo NO position on `KXMVECROSSCATEGORY-S2026C1138DA69BC-7ADA8E5486D`
(= LAA win Jul9 AND BOS win Jul10), cost $0.50, `position_fp -1.00`. When the MLB
legs settle, confirm our NO pays `1 − V` to the cent → sets **`combo_no_pays_complement`**
in `tests/fixtures/ground_truth/conventions.json` → **un-gates sell-only fills**
(they're inert until then). Detail + check logic:
`docs/reports/2026-07-09-demo-combo-roundtrip.md`, memory `project_kct_combo_settlement_watch`.

## 🟢 SHIPPED THIS SESSION (2026-07-08 → 09)

| Thing | Where |
|---|---|
| **Sell-only parlay-seller mode** (`sell_parlays_only`, forces `yes_bid=0`) — verified airtight by 2 independent agents + engine-boundary backstop; 984 tests green | `pricing/quote.py`, `pricing/engine.py`, `ops/config.py`, `config/*.yaml` · report `2026-07-08-sell-parlays-only-fix.md` |
| **Combo mechanics verified** (Kalshi API): two-sided binary; parlay-seller = `yes_bid=0`; NO = whole-combo complement (not per-leg neg); no taker cash-out reaches us | reports `2026-07-08-combo-yes-no-side-mechanics.md`, `-cash-out-exposure.md` |
| **DNP/scalar settlement** doc + **decision: BUILD NOTHING** (≈EV-neutral, rare, fail-safe halt → handle reactively). Soccer scorer no-show → settles to last fair price (scalar, VERIFIED from Kalshi market text) | `docs/dnp_scalar_settlement.md` |
| **System atlas** (living top-down map, navigable HTML) — honest status vocabulary (proven / coded / in-progress) | `docs/atlas.html` (artifact) |
| **Demo combo round-trip** (live) — landed long NO; sell-only + direction verified live | `2026-07-09-demo-combo-roundtrip.md` |
| **WC backtest harness — pre-game filter + tape backfill** (live-validated): clearings now from Kalshi's trade tape (gap-free → poller stall backfilled by construction) + strictly-pregame drop (`--pregame-hours`, default 2.5); engine untouched | `tools/backtests/wc_backtest.py` · report `2026-07-09-pregame-filter-and-tape-backfill.md` |

## Honest status (recalibrated 2026-07-09 — "proven" = we've SEEN it work)

- **Proven:** scaffold, market data, observe/recorder, demo round-trips (Phase 0–2, 5 plumbing).
- **Coded, NOT verified:** risk engine + Monte-Carlo (Phase 4) — written + unit-tested, never run against live flow.
- **In progress:** pricing (Phase 3) — spine works, **correlation surface far from complete** (soccer furthest; MLB partial; NBA gated; UFC/Tennis unbuilt).
- **Blocked/inert:** sell-only fills, until the demo settlement confirms `combo_no_pays_complement`.

## ✅ COMMITTED + PUSHED (2026-07-09 ~20:20 UTC)

All session work is now on `origin/main` (was 24 dirty/untracked entries on a
single disk): `9e13305` sell_parlays_only feature · `3e02bdc` docs (reports
channel, atlas, DNP, calibration, CLAUDE.md rules 7-8) · `c976f0d` tools (the
wc_backtest harness + calibration one-offs + experimental ising_amm, which is
unreviewed and imported by nothing). Working tree clean at `c976f0d`.

## 🔵 ACTIVE WORKSTREAM — MLB/baseball SGP finalization (operator directive 2026-07-09)

Soccer is strong and nearly done (final tests pending) — **baseball is now the
focus**: "measure all possible correlations in baseball based on what Kalshi
allows in combos." A multi-agent pass launched 2026-07-09 evening:

1. **Classify** every MLB ticker family strictly from Kalshi docs + API + the
   RFQ tape (verify existing classifications, add missing ones — staged, not
   promoted).
2. **Re-measure ρ**: rerun the shipped calibration (reproducibility), an
   INDEPENDENT from-scratch re-derivation, and extension to Retrosheet 2005-25
   (era stability); then measure every allowed-but-unmeasured pair.
3. **Adversarially verify** classification (completeness + correctness lenses)
   and every number; produce the **unknowns queue** (props needing more
   research/math).

**PHASE 1 DONE (2026-07-09 ~23:30 UTC)** — full detail in
`2026-07-09-mlb-classification-and-rho-verification.md`; staged code (NOT
applied) in `docs/calibration/staged_mlb_props.md`. Headlines: exactly **9
combo-eligible families** (props all UNKNOWN→+0.6 today); **KXMLBTOTAL = GAME
total**, TEAMTOTAL untradeable → +0.367/−0.380 are reference-only and
player_hr|game_total was never measured; 3 latent live misclass bugs found
(F5TOTAL/F5SPREAD/SERIESGAMETOTAL); shipped ρ reproduced EXACTLY at 3
independent levels + 21-season stable; **NEW TOP BLOCKER:
event_mutually_exclusive metadata** (all of a game's prop markets share one
event ticker; relationships.py groups by event BEFORE the ρ table → gates ALL
multi-player basket pricing); same-player cross-stat rungs are deterministic
CONTAINMENTS (not ρ).

**MEASUREMENT TRANCHE DONE (~23:59 UTC)** — 8 agents + xhigh judge, **33
verdicts, 0 refuted**. Full detail: `2026-07-09-mlb-measurement-tranche.md`;
final judge-amended table appended to `docs/calibration/staged_mlb_props.md`.
Headlines: **flat +0.6 overbids 8-16-leg all-NO HR baskets by +25-35¢/$1**
(measured ρs reproduce the 16-leg joint to 0.0003 — the money finding);
player_hr|game_total measured **+0.24**/0.10 (the phase-1 critical gap); K
pairs are **ladder-FLAT** → operator's K-line convention question **RESOLVED**
(self-median fine, one entry per K pair); batter HIT/HR rungs **drift** →
per-rung entries (single 1+ ρ sells deep rungs cheap);
**event_mutually_exclusive = false on all 6 prop families (baskets NOT gated,
merely uncalibrated)**, true on KXMLBGAME (correct) — pin via fixture;
same-player same-family rungs = ZERO flow; cross-family same-player (6.5%) =
deterministic containment (HR⇒HRR≥3 exact 101,186/101,186). Remaining gaps:
cross-family distinct-player batter pairs (hit|hr etc., labeled priors staged,
hours to measure) + teammate hrr|ks + tb|ks.
**PROMOTED 2026-07-10 (operator-directed, commits 4c57f86 + 85d02cb):**
classification is **9/9 LIVE** (6 LegTypes + blockers; 3 live misclass bugs
fixed) and the **32-entry mlb pair table + 32 bands is LIVE in config** — final
[D] pairs measured (20.8M pair-units) + judge-confirmed first. Full suite 1013
passed / mypy strict / ruff fully clean; SGP spot checks 6/6 (KS×TOTAL −0.25
not +0.6; soccer 0.70 / NFL 0.88 untouched). Remaining, in order: (1)
`tools/backtest_mlb_pairs.py` tape-replay VALIDATION gate; (2) resolver phase
(ML-team orientation + teammate/opponent routing — signed values sit in config
comments); (3) containment phase (ml|spread routing, same-player cross-stat
containments, nested-BAND arithmetic — see the one-leg-per-ladder report:
same-side rungs are exchange-blocked, yes-low+no-high corner BANDS are real
tape flow currently at flat +0.6). Reports:
`2026-07-10-mlb-promotion-wired.md`, `2026-07-10-one-leg-per-ladder-rule.md`.

## OPERATOR DECISIONS (2026-07-09 evening)

- **Markup: DEFERRED.** The 2026-07-08 settlement P&L showed sim P&L peaking at
  ~1¢ with toxicity rising monotonically toward 5¢, tensioning the standing
  "2-3¢ wide while capital low" prior. Operator call: **decide after more WC
  data** — i.e. after the post-Jul-11 wc_backtest run on the freshly settled set.
  Until then neither number is doctrine.
- **LAA leg watch tonight:** operator is watching the Jul 9 LAA game settle.

## NEXT ACTIONS (prioritized)

1. **TONIGHT (~2026-07-10T03:05Z+) then ~Jul 11:** check the demo combo legs
   (all `active` as of 20:05Z). LAA leg (KXMLBGAME-26JUL092005LAATEX-LAA)
   expected-expires 03:05Z — **if LAA loses, early-NO determination fires FULL
   combo settlement immediately** (see cashout report), confirming
   `combo_no_pays_complement` a day early; otherwise the BOS leg + combo resolve
   ~2026-07-11T02:15Z. Then set the convention in
   `tests/fixtures/ground_truth/conventions.json` → un-gate sell-only fills.
   (memory `project_kct_combo_settlement_watch`)
2. **After Jul 9–11 WC settles:** run the ready harness
   **`tools/backtests/wc_backtest.py`** — `gather --since 2026-07-01` (OFF-PEAK;
   it now only READS rfqs/would_quotes, no longer touches the write-locked
   combo_trades) → `price` → `analyze`. It is **WC-strict** (every leg `KXWC*`),
   **cached/fast** (per-distinct-combo + memoized), and **ZERO-BIAS by
   construction**: the price stage reads `inputs.pkl` only; clearings + settlement
   live in a separate `outcomes.pkl` the pricer never opens.
   **UPDATED 2026-07-09 (live-validated):** (a) **clearings now come from Kalshi's
   trade tape** (`get_trades`, authed) not the DB — COMPLETE, so the ~10h
   combo-poller stall is **backfilled by construction**; (b) **strictly-pregame
   filter** drops any print at/after a leg's estimated kickoff
   (`cutoff = min-over-legs(expected_expiration_time − PREGAME_HOURS)`,
   `--pregame-hours` default **2.5**, conservative → never admits an in-play
   print), and drops combos with no pre-game print. Engine untouched (rule 8).
   Detail + validation evidence: `2026-07-09-pregame-filter-and-tape-backfill.md`.
   Backtests our fair vs clearing + settlement P&L on the freshly settled combos.
3. **Keep the recorder alive** through Jul 11 (verify watermarks rising; restart if dead).
4. **Ongoing:** grow the correlation surface (the biggest open work) — more soccer
   pairs, then replicate the calibrate→audit→test loop to other sports.

## Where everything lives

- `docs/reports/` — dated reports (this channel); `README.md` = master status.
- `docs/atlas.html` — the living system map. `docs/dnp_scalar_settlement.md` — DNP spec.
- `CLAUDE.md` — agreement + hard rules. `NOTES.md` — exchange mechanics + audits.
- `docs/calibration/results_*.md` — per-sport correlation calibration.
- Memory: `project_kalshi_combos_resume_state`, `project_kct_combo_settlement_watch`,
  `feedback_reports_channel`.

## NEXT STEPS
- **Owner (next session):** verify recorder alive → do actions 1–2 above.
- **Owner (operator):** none blocking; decisions owed are logged in each report's footer.
