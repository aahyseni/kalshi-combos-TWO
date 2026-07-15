# 2026-07-15 — HANDOFF for external LLM risk-engine review

**Audience:** an external LLM (or fresh operator/agent) tasked with reviewing the
risk engine of `kalshi-combos-TWO`. This document is **self-contained** and
**zero-bias**. The operator's verdict, stated verbatim, is: **"the risk engine is
still insanely off."** Nothing below softens that. Read this whole file before
touching code.

---

## 1. TL;DR + CURRENT STATE

`kalshi-combos-TWO` is a **sell-only maker** for Kalshi soccer/MLB **combo
(parlay) RFQs**, ~$2k production bankroll. This session did a deep risk-engine
audit (two overnight workflows + a second-LLM "codex" audit) and ran the new
engine live against a single-game World Cup slate. The engine now quotes and
fills, but the **per-game / directional caps are structurally overstated** and
the process **dies under the near-kickoff RFQ firehose**. It is not trustworthy.

| Item | State |
|---|---|
| Bot process | **DOWN** — killed by supervisor heartbeat twice (18:31, 18:55) under the near-kickoff firehose. |
| Branch | `risk-audit-overnight` |
| HEAD | `0f5d6c8` (`0f5d6c826811dd67fe4a96056348620caaaa91d1`) |
| Baseline / restore point | commit `45164f1` |
| Merge state | **NOT merged to main, NOT pushed.** |
| Test suite @ `0f5d6c8` | **2047 passed / 0 failed** (the master changelog stamped 2026/0; +9 P0-2 + others since). |
| Committed work | all risk-audit P0/P1/P2, P0-7 conditioning, P0-1 gate wiring, the 2 codex gate fixes, EV/latency logging — all on the branch (see §3). |
| Uncommitted work | **only `config/prod-live-wc.local.yaml`** (gitignored — cap raises, 1c markup, pregame GOAL offset, ineffective heartbeat override). Corners edge-floor and hedge-slots were **NOT** completed. |
| Today's live game | **ENG vs ARG** World Cup semifinal, game code `26JUL15ENGARG`, kickoff ~19:00 UTC. |

**Restore to a known-good baseline:**

```
git reset --hard 45164f1
```

**Critical nuance:** the live bot is (was) run with `--config
config/prod-live-wc.local.yaml`, but the **supervisor is a separate subprocess
that loads the BASE per-env config, not the override** (see Problem B). So some
"fixes" that live only in the local config never actually took effect.

---

## 2. SESSION ARC (chronological)

1. **ZERO-QUOTES ROOT CAUSE (fixed, pre-baseline, preserved).**
   `Store.held_positions` JOINed `fills` to the `rfqs` tape (1.6M rows, up to
   **12,456 rows per combo**) *before* `SUM(contracts_centi)`, inflating
   rehydrated contracts up to 12,456× (real 37 → 464,235) → a **−259,302** delta
   on the shared ARG-advance leg → every risk cap blown → **0 quotes**. Fix:
   pre-aggregate fills + de-dup rfqs legs into 1-row-per-combo derived tables +
   `idx_rfqs_market_ticker`. Live: **0 → 414 quotes.**
   Report: `2026-07-15-rfq-tape-fanout-zero-quotes-fix.md`.

2. **RISK-ENGINE AUDIT** (`RISK_ENGINE_AUDIT_ACTION_PLAN.txt`). Implemented all
   **10 P0 + 11 P1 + 2 P2** items via two overnight workflows + manual review.
   Then a "live-ready" pass: P0-7 upgraded to structural conditioning, teardown
   warnings removed, P0-1 candidate gate WIRED into confirm (additive, off-loop,
   fail-closed). All committed on the branch.

3. **CODEX AUDIT** (`RISK_ENGINE_LIVE_BALANCING_VALIDATION_AUDIT.txt`). A second
   LLM found **2 real candidate-gate bugs**: (P0-1) ruin equity basis
   double-counted candidate/reservation premiums (understated P(ruin)); (P0-2)
   candidate MC not atomic with reservations (concurrent accepts evaluate the
   same stale pre-book). **BOTH FIXED + committed at `0f5d6c8`** (commits
   `6cff5f6` equity-basis, `60a5ea8` atomicity, `0f5d6c8` EV/latency logging).
   The hedge-slots step was in progress and **REVERTED (not committed)** when the
   workflow was stopped.

4. **CORNERS UNDERPRICING (#37): measured.** The corners marginal is the correct
   Kalshi leg mid; the shipped corners↔goals `rho=0` was measured on CLUB data.
   Operator wanted it re-measured before any change.
   `tools/measure_corners_goals_rho.py` (club football-data, tetrachoric rho at
   Kalshi lines 7/8/9/10 × goals/BTTS, n=8,981): corners↔goals rho **~0
   (marginally NEGATIVE)** at **every** traded line; the discarded
   reverse-engineered +0.35/+0.5 is **REFUTED**. **VERDICT: our corners fair is
   CORRECT; the 3-5c gap is MARKET RICHNESS, not a model error.** Fix chosen: a
   defensive **+3c corners edge-floor** (markup adder when a leg is
   `KXWCTCORNERS`/`KXWCCORNERS`), NOT a rho change. **NOT YET DEPLOYED.**
   Report: `2026-07-15-corners-goals-rho-measurement.md`.

5. **PREGAME IN-PLAY BUG.** ~40 min before kickoff the bot declined ~89% of flow
   as `skip_inplay_leg`. Cause: the in-play gate estimates kickoff = market
   expiration − per-family offset; `KXWCGOAL` expires ~kickoff+3h but was falling
   to the +4h `KXWC` catch-all → estimated kickoff ~1h early → GOAL (in most
   combos) poisoned everything. Fix: added `KXWCGOAL: 3.0167` to config
   `pregame_start_offset_hours_by_prefix` (LOCAL config, gitignored). Verified
   live: `skip_inplay_leg` **89% → 53%**. TOTAL/BTTS/CORNERS/FIRSTGOAL genuinely
   expire +4h and were LEFT on the catch-all (reducing them would risk quoting
   IN-PLAY).

6. **HEARTBEAT KILL.** The bot died twice (18:31, 18:55) from `heartbeat wedged
   (~15s > 15.0s)` when the near-kickoff RFQ firehose (4.6M RFQs, 89k
   closed-before-post) starved the maintenance loop, **despite P2-2** (MC moved
   off the event loop). The interim fix (`heartbeat_timeout_s: 30` in the LOCAL
   config) **DID NOT TAKE EFFECT**: the supervisor is a SEPARATE subprocess that
   loads the BASE config, not the `--config` override → still used 15.0s.
   Confirmed in `live_wc11.log`: `supervisor_starting timeout_s: 15.0`.

---

## 3. WHAT WAS FIXED (with commit hashes)

Branch `risk-audit-overnight`, range `45164f1..HEAD` (35 commits, 64 files,
+14,672 / −318). Suite green throughout.

### risk-audit P0 (critical correctness)
| Commit | Item |
|---|---|
| `a10dc81` | P0-5 exact exchange-quantity reconciliation |
| `bb8361d` | P0-4 usable MC without hiding unmodeled holdings |
| `12d83ac` | P0-6 fractional contracts in MC |
| `33abf6f` | P0-3 separate model ES from deterministic maximum loss |
| `15708c7` | P0-2 book generations + immediate invalidation |
| `bcb89cf` | P0-1 candidate- and reservation-aware portfolio risk |
| `1e25c15` | P0-9 directional-cap mutex-aware hedge semantics |
| `89449c8` | P0-8 restrict challenger correlation inflation to same-game |
| `52ab290` | P0-7 structural/fallback same-game dependence bridge |
| `1552fb0` | P0-7-preferred conditioned fallback legs on game state |

### risk-audit P1 (hardening / provenance / challengers)
`f145740` P1.1 production+challenger P(ruin) gate on the worse ·
`6b357b1` P1.2 confidence bounds / adaptive samples near gate ·
`350484a` P1.3 equity/P&L basis no double-count ·
`bf31d0e` P1.4 persist structural residuals, reject/challenge bad fit ·
`482dfd9` P1.5 public parse/invert/sample/settle structural API ·
`9c910d2` P1.6 tape-derived parity for regulation/advance/halves ·
`e5482ee` P1.7 mutex-metadata audit, explicit-True-ONLY netting ·
`02db848` P1.8 label `analytic_leg_deltas` as independence proxies ·
`58f9ad8` P1.9 independent structural-parameter challenger ·
`1f5d0e8` P1.10 durable position ledger with exchange-qty provenance ·
`b7f84a0` P1.11 exact originating record (replaces `MAX(legs_json)`).

### risk-audit P2 (ops / hygiene)
`207c7e1` P2-2 full-book MC off the event loop, generation-safe ·
`1b20d7f` P2.1 no orphaned workers (parent-owned process group) ·
`cbeb899` P2.2 log book/snapshot generation per quote/confirm ·
`09d24ef` teardown-warnings removed.

### live-ready follow-up (gate wiring)
`36a5a47` P0-1-wiring: candidate-aware gate into last-look/confirm (additive,
off-loop) · `a8d1dd5` correction note · `f66fe3a` follow-up report.

### candidate-gate codex fixes (post-wiring)
`6cff5f6` codex-P0-1: candidate ruin equity basis = committed-only (no premium
double-count) · `60a5ea8` codex-P0-2: candidate MC atomic with reservations
(version-checked) · `0f5d6c8` codex-P1: log production+challenger candidate EV +
gate latency/deadline metrics.

Docs: `e6c522e`, `8f19cf3`, `d729806`, `e3b71fc`.

**Also fixed / preserved:** the fanout fix (§2 step 1); pregame GOAL offset; cap
raises (config); 1c markup; the corners measurement (verdict = market richness,
NOT a rho change).

---

## 4. OPEN PROBLEMS A–G — ranked by severity

> This is the most important section. The risk engine is still off. A–C are the
> live-critical ones (ME overstatement, heartbeat, throughput). The two source
> audits (`RISK_ENGINE_AUDIT_ACTION_PLAN.txt`,
> `RISK_ENGINE_LIVE_BALANCING_VALIDATION_AUDIT.txt`) are at repo root; file:line
> evidence below was re-verified read-only at HEAD `0f5d6c8`.

### A. ME (mutual-exclusion) OVERSTATEMENT of game-loss / directional caps — HIGH (operator-identified)

**The core defect.** The analytic per-game caps treat max-loss as *"if every
combo of this game loses"* — **literally impossible** for combos with
mutually-exclusive legs. Stage B nets only the **single advance ME event**
(ARG/ENG/TIE via max-over-branches) and then **SUMS mutually-exclusive losses on
every other dimension**: total over-vs-under, corner-line high-vs-low, 1H result,
and no-advance combos are treated comonotone across branches.

**Evidence (file:line, CONFIRMED read-only):**
- Loss axis `_mutex_game_worst_cc` — `src/combomaker/risk/exposure.py:599-625`:
  `comonotone = sum(loss ...)` at `:609`; collects events flagged
  `is_me_event(e) is True`; **`if len(me_events) != 1: return comonotone`** at
  `:623-624` (0 ⇒ no ME netting, ≥2 ⇒ fail-closed to the sum). Only exactly one
  ME event is netted, via `_mutex_event_bound_cc` (`:625`), a max-over-branches
  of a *single* event (`:575-596`).
- Directional axis `_mutex_directional_game_cc` — `exposure.py:684-696+`:
  `summed = sum(mag ...)` at `:694`, same single-ME-event netting, fails closed
  to the summed magnitude on 0 or ≥2 events.
- Caps that bind on these: `game_loss_frac` (`risk/limits.py:117`) checked at
  `limits.py:534-540`; `directional_frac` (`limits.py:123`) mutex-aware check at
  `limits.py:576-583`. Independence-proxy `delta_by_game` is the loose monotone
  backstop for `max_event_delta` (`limits.py:710-712`).

Live confirm-decline evidence: of 24 last-look confirm-declines, most tripped the
mutex-aware **directional** cap; several were **ENG-advance** combos (impossible
to co-lose with our ARG-advance book) — the advance netting works for *game-loss*
(ENG combos mostly DON'T trip game-loss) but their goal/corner/total legs still
add.

**The constraint (why the naive fix is wrong).** `exposure.py:542-554` documents
it: the **E2 mass-acceptance dominance invariant requires the per-game bound to
be MONOTONIC** — adding an open quote must never lower it. Recognizing *more* ME
structure (a 2nd ME event, or a binary yes/no market) refines the partition and
LOWERS the bound → non-monotonic → *"could push the mass bound BELOW a realized
subset that doesn't hold that hedge... a real safety hole (a taker can accept only
the concentrated side and decline the hedge)."* So a general hedge-aware analytic
tightening **cannot** go at quote-time. The bound is documented as always
`≤ comonotone` and `≥ largest single entry` (`:607-608`, `:646`) — deliberately
overstated when >1 hedge exists.

**DURABLE FIX (LAST-LOOK / MC, not quote-time analytic):** at **last-look**,
where the structural MC is already computed, gate on the **MC's worst-case**
(it samples real game states, so all impossibilities are respected and it can
never count an impossible joint loss) instead of / in addition to the comonotone
analytic bound. Optionally extend analytic netting beyond the single advance
event *only where it stays monotone*. The richer all-legs hedge credit belongs
in the candidate-aware MC (`exposure.py:552-554`, `:648-650`), NOT in the
quote-time cap. Prototype seams already exist: `tools/proto_mutex_game_cap.py`,
`tools/proto_mutex_directional.py`, `tools/proto_structural_book_mc.py`.

### B. HEARTBEAT KILL under the firehose — HIGH

Dies at ~15s even with MC off-loop (P2-2). **CONFIRMED the 30s override never
applied:**
- Supervisor launched with **only** `--env`, **no** `--config`:
  `src/combomaker/ops/quote_app.py:1238-1244`
  (`cmd = [sys.executable, "-m", "combomaker.ops.supervisor", "--env",
  str(self._config.env)]`), spawned at `quote_app.py:1246`.
- Supervisor `main()` accepts `--config` (`ops/supervisor.py:514`) and would
  honor it (`_run_supervisor_cli` at `:464`), but since the launcher never passes
  it, `config_path` is `None` → supervisor loads the **base** per-env YAML
  (`config/prod.yaml`) at `:465` and reads `heartbeat_timeout_s` from *that*
  (`:470`). Default `SupervisorConfig.heartbeat_timeout_s = 15.0`
  (`ops/config.py:122`).

**DURABLE FIX (any/all):** (1) **pass `--config` to the supervisor subprocess**
so overrides apply (smallest change; `quote_app.py:1238-1244`); (2) beat the
heartbeat from a **dedicated OS thread** so CPU-bound RFQ processing can't starve
it; (3) reduce firehose load (Problem C). **Interim:** raise the BASE-config
`heartbeat_timeout_s`, or pass `--config`.

### C. THROUGHPUT CEILING — HIGH (the #1 fill-limiter)

The bot prices only ~1-2% of incoming flow; `skip_rfq_closed` +
`quote.rfq_closed_before_post` (**89,863 in one run**) dominate. ~170-1500 RFQ/s
near kickoff, each price ~600ms GIL-bound; joint pool timeouts (8,485).

**Evidence (CONFIRMED):** off-loop pool `POOL_WORKERS = 8`,
`POOL_DEADLINE_S = 2.0` (`quote_app.py:112,119`), wired at `quote_app.py:461`;
deadline enforced at `pricing_pool.py:200`
(`await asyncio.wait_for(fut, timeout=self._deadline_s)`) → on timeout raises
`TimeoutError`, caller drops the quote (`pricing_pool.py:188-201`).
`rfq_closed_before_post` emitted at `rfq/lifecycle.py:1424` and handled as a
**normal taker-race loss** (`rfq/lifecycle.py:1419-1426`) — the RFQ's ~1s window
closed before our POST landed. `RFQ_WORKERS = 8` (`quote_app.py:620`).

**DURABLE FIX:** cache the **per-game structural fit** (cheap lookup, not a
re-fit) so pricing isn't re-inverting per combo; **ProcessPool-offload the
pricer** further; or **shed load earlier** via a cheaper pre-classification so the
firehose never reaches the pricer. See `tools/profile_pricer.py`,
`tools/memo_parity_check.py`, `tools/pool_parity_check.py`.

### D. DB LOCK CONTENTION — MEDIUM

`database table is locked` on `PRAGMA wal_checkpoint(TRUNCATE)` — concurrent read
locks (the operator's `live_viewer.py` monitors) + the write firehose block the
checkpoint; can stall the store writer. **Durable fix:** serialize / defer the
checkpoint under load, or isolate monitors to a read-replica / WAL snapshot.

### E. PREGAME EXPIRATION MAPPING is FRAGILE — MEDIUM

Manual per-family offsets (`pregame_start_offset_hours_by_prefix`), no real
kickoff feed. Only `KXWCGOAL` was corrected this session; other families' offsets
are inferred, not verified. A wrong offset silently declines pregame flow (the §2
step 5 bug) or — worse — could let an IN-PLAY leg through. **Durable fix:** a real
kickoff schedule feed (`rfq/schedule.py ScheduleCache` exists as a seam).

### F. CAPS PIN THE 1-GAME BOOK — MEDIUM

On a 1-game slate the per-game caps bite immediately. Raised to
30/30/15%/60 interim, but the **ROOT is (A)**. The **hedge-prioritized quote
slots** and **corners edge-floor** were **NOT completed** (workflow stopped). The
ruin floor −30% is unchanged (the backstop).

### G. CANDIDATE GATE not fully LIVE-VALIDATED — MEDIUM

Codex bugs fixed + committed, but atomicity/deadline behavior under real
concurrent accepts is **unproven live** (few auctions won: 3 fills, ~27 accepts,
24 declined by analytic caps). Needs a live run with real concurrency, or a
targeted concurrency test harness, before it can be trusted.

---

## 5. HOW TO REPRODUCE / TEST

Windows, PowerShell primary. Virtualenv: `.venv/Scripts/python.exe`.

```
# test suite
.venv/Scripts/python.exe -m pytest -q

# launch live (prod, quote mode, with the local override config)
.venv/Scripts/python.exe -m combomaker.ops.cli run --env prod --mode quote \
    --confirm-live --config config/prod-live-wc.local.yaml

# stop  (NEVER pkill — orphans workers, though P2.1 guards it)
touch KILL

# monitor (read-only)
.venv/Scripts/python.exe tools/live_viewer.py
```

**Databases:**
- `data/combomaker-prod-live-wc.sqlite3` — the LIVE bot DB (decisions / fills /
  rfqs tables). This is what `live_viewer.py` reads read-only.
- `data/combomaker-prod.sqlite3` — prod shadow tape (read-only, huge).
- `data/history/` — historical calibration data: football-data club CSVs
  (D1/E0/F1/I1/SP1, **with corners**); `intl_results.csv` = goals only, **NO
  corners** (this is why the corners rho had to be measured on club data).

### TOOLS inventory (54 scripts under `tools/`; session-central ⭐)

**Pricing/OOS backtests:** `backtests/wc_backtest.py`,
`backtests/mlb_backtest.py`, `backtests/mixed_wc_mlb_backtest.py`,
`backtests/parity_rule8c.py`, `backtests/wc_mlb_regrade/0{1..4}_*.py`,
`validate_structural_oos.py`, `validate_margin_total_oos.py`,
`validate_margin_total_kalshi.py`, `validate_mlb_runs_oos.py`,
`validate_mlb_runs_kalshi.py`, `validate_halftime_dc_oos.py`,
`compare_models_on_tape.py`, ⭐`market_vs_our_pricing.py`,
⭐`market_drift_check.py`, `_persist_ticksig.py`, `_gen_report_tables.py`.

**Calibration:** ⭐`measure_corners_goals_rho.py` (the #37 measurement),
`fit_conditional_rho.py`, `calibrate_pairs_from_history.py`,
`calibrate_margin_total.py`, `calibrate_mlb_runs.py`,
`calibrate_mlb_player_props.py`, `calibrate_soccer_firsthalf.py`,
`calibrate_soccer_scorers.py`, `calibrate_soccer_1h_winner_total.py`,
`calibrate_soccer_1h_spread.py`, `calibrate_soccer_btts_1h_total.py`,
`calibrate_soccer_corners_total_team.py`,
`calibrate_soccer_corners_team_winner.py`, `calibrate_nba_first_half.py`,
`calibrate_nba_player_points.py`, `dc_ml_player_goal_prior.py`,
`ising_amm_run.py`.

**Data fetchers:** `fetch_kalshi_history.py`, `fetch_understat.py`,
`fetch_statsbomb.py`.

**Risk prototypes (validate-in-tool, then port to `risk/`):**
⭐`proto_mutex_directional.py` (P0-9 directional hedge credit),
⭐`proto_structural_book_mc.py` (A1 structural portfolio MC),
⭐`proto_mutex_game_cap.py` (Stage-B ME-aware game cap),
`proto_structural_copula_conditioning.py` (P0-7 conditioning). **These four are
the seams for the Problem-A durable fix.**

**Ops/perf:** `profile_pricer.py`, `memo_parity_check.py`,
`pool_parity_check.py`, `tolerance_regrade.py`, `mvec_eligibility_scan.py`,
`live_viewer.py`.

### DIAGNOSTICS scripts (`tools/diagnostics/`, read-only, no docstrings)
- ⭐`review.py` — tallies decisions/reasons by ADVANCE side (ARG/ENG) since a
  relaunch timestamp.
- ⭐`pricing_review.py` — fills & quotes bucketed by leg family
  (ADVANCE/CORNERS/GOAL/1H/…).
- ⭐`verify_delta.py` — recomputes the per-leg-market delta the cap sees from held
  positions in a live DB copy (contracts × product of other-leg marginals). This
  is the tool that catches Problem A's inflated deltas.

---

## 6. FILE / REPORT INDEX

**Source audits at repo root (read these — they are the authoritative bug
lists):**
- `RISK_ENGINE_AUDIT_ACTION_PLAN.txt` — the 10 P0 + 11 P1 + 2 P2 plan; all
  implemented (§3).
- `RISK_ENGINE_LIVE_BALANCING_VALIDATION_AUDIT.txt` — the codex second-LLM audit;
  its 2 candidate-gate bugs are fixed at `0f5d6c8`.

**Reports (`docs/reports/`, newest-first, most relevant to this handoff):**
- `2026-07-15-full-session-changelog-and-live-status.md` — MASTER changelog +
  live status (suite stamp, restore command).
- `2026-07-15-rfq-tape-fanout-zero-quotes-fix.md` — the fanout root cause (§2.1).
- `2026-07-15-corners-goals-rho-measurement.md` — the #37 measurement (verdict:
  market richness, NOT a rho change).
- `2026-07-15-p0-2-candidate-mc-atomic-with-reservations.md` — codex P0-2 fix.
- `2026-07-15-risk-engine-audit-implementation.md` — definitive implementation
  report (all P0/P1/P2) + the live-ready follow-up appendix.
- `2026-07-15-p0-7-preferred-conditioned-fallback.md` — P0-7 conditioning.
- `2026-07-15-risk-engine-structural-mc-research.md` — design + adversarial
  critique for the structural portfolio MC (the durable-fix design for A).
- `2026-07-15-p1-3-equity-pnl-basis-no-double-count.md` — P1-3 proof.
- `2026-07-14-fill-blocker-is-comonotone-risk-cap-not-price.md` — the ORIGINAL
  diagnosis of Problem A (comonotone cap, `exposure.py:275`).
- `2026-07-14-market-vs-our-pricing-main-combos.md`,
  `2026-07-14-price-discovery-we-are-the-sharp-maker-room-to-widen.md` —
  price-competitiveness evidence (we are NOT overpriced on mains).

**SUPERSEDED (carry inline corrections):**
`2026-07-15-risk-engine-upgrade-final-report.md` (superseded by the fanout fix)
and `2026-07-13-risk-engine-code-audit-go-live.md` (superseded by the
post-wiring re-audit).

Full inventory: `docs/reports/README.md` master table (newest-first).

---

## 7. NEXT STEPS / DECISIONS OWED

| # | Action | Owner | Notes |
|---|---|---|---|
| 1 | **Fix the heartbeat kill (B)** | agent | Pass `--config` to the supervisor subprocess (`quote_app.py:1238-1244`) AND/OR beat the heartbeat from a dedicated OS thread. This is the cheapest, highest-value fix — the bot cannot stay up without it. |
| 2 | **Attack the throughput ceiling (C)** | agent | Cache per-game structural fit + shed load earlier. This is the #1 fill-limiter; without it the candidate gate (G) can't be validated (too few auctions won). |
| 3 | **ME-aware last-look MC gate (A)** | agent + operator sign-off | Gate on the MC worst-case at last-look (monotonicity-safe there); do NOT touch the quote-time analytic cap. Prototypes: `proto_structural_book_mc.py`, `proto_mutex_game_cap.py`, `proto_mutex_directional.py`. Rule-8: prototype in tool → port → parity-check. |
| 4 | **Deploy the corners +3c edge-floor (F/#37)** | operator decision | Measurement done (market richness). This is a markup adder for `KXWC*CORNERS` legs, NOT a rho change. Gated on fills. |
| 5 | **Complete hedge-prioritized quote slots (F)** | agent | Was in flight and reverted when the workflow stopped. |
| 6 | **Live-validate the candidate gate under real concurrency (G)** | agent | Blocked on (2) — need enough auctions won. |
| 7 | **Kickoff schedule feed (E)** | agent | Replace the fragile per-family offset map; `rfq/schedule.py ScheduleCache` is the seam. |
| 8 | **DB checkpoint contention (D)** | agent | Serialize/defer `wal_checkpoint(TRUNCATE)` under firehose load. |
| 9 | **MERGE-TO-MAIN decision** | operator | Branch `risk-audit-overnight` @ `0f5d6c8` is NOT merged/pushed. Do NOT merge until A + B are fixed and re-validated live. Baseline restore is `git reset --hard 45164f1`. |

**Standing constraints for whoever picks this up:** never refit the model on a
P&L window (measurement/structural evidence only); the quote-time analytic caps
must stay **monotone** (the E2 invariant) — richer hedge credit goes in the
last-look MC, not the quote-time cap; live modules stay pristine (prototype in
`tools/`, port, parity-check to the cent).
