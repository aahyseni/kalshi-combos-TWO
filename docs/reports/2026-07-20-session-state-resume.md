# 2026-07-20 — SESSION STATE / RESUME (post-World-Cup handoff)

**READ THIS FIRST after compaction or in any new session.** Companion doc:
`2026-07-19-worldcup-findings-and-new-sport-readiness.md` (campaign findings +
new-sport checklist). Operator memory: `project_kct_resume_state.md` points here.

---

## ⚡ IMMEDIATE STATE (as of Mon 2026-07-20, morning ET)

- **BOT IS DOWN — deliberately — on a HUMAN-ONLY KILL** (`halt_hard_trip`,
  "give-back $430.69 ≥ 3/25 bankroll", fired 6:05 PM ET Sun during the
  settlement cascade). **VERIFIED FALSE POSITIVE**: settled positions left the
  equity mark before the balance poll credited the cash; actual settlement
  losers were only $29.51 (3 combos) vs $263.60 of winners (71 combos).
- **Operator must say "clear it"** → then relight:
  `Start-Process cmd '/c','.venv\Scripts\python.exe -m combomaker.ops.cli run --env prod --mode quote --confirm-live --config config\prod-live-wc.local.yaml > data\live_logs\live_20260720_<name>.log 2>&1'`
  (clear the KILL/needs-reconcile marker per the halt's human-clear procedure —
  startup reconcile handles the rest). After relight: remaining settlements
  book automatically → produce the **final realized-P&L statement** (owed).
- Re-arm monitors after relight (Monitor tool, persistent=true):
  - events: `tail -f data/live_logs/<newest>.log | grep -v --line-buffered '"periodic_report"\|settled_resolution_pending' | grep -E --line-buffered 'settlement_booked|settlement_reconciled|"phase": "decline"|risk_reservation_granted|halt|needs_reconcile|preflight_failed|emergency|fill_recovery|position_reconcile_unmodeled'`
  - liveness (only if quoting resumes): the zero-quote/frozen-log alarm loop
    (see 7/19 logs; suspended during the deliberate kill).
- **WC quoting is DONE** (both games settled). No quoting expected until the
  MLB/WNBA switch. The bot after relight = settlement booking + reconcile nets.

## Campaign result

74 real fills, ~$720 premium collected. Final settled 0-0 / TIE / **Spain
champion via pens** / no scorers → **71 winners +$263.60, 3 losers −$29.51 =
NET +$234.09** on the final; FRAENG (Fri) banked the FRA-win/Mbappé stacks
(FRA-win graded NO). P(book) at kickoff was 71% scenario-exact; P(ruin) 0.
The ARG+Messi ladder-taker (24 clips ≈ $300 premium, one counterparty, all
weekend) expired worthless in our favor.

## What was found + shipped (all on branch `risk-audit-overnight`, pushed; latest `ef0412f`)

| # | finding | fix (commit) |
|---|---|---|
| 1 | Comonotone det-max walled off diversifying flow | mutex-aware min(comono, state-exact) at quote-time cap + candidate gate (`ade7b71`) |
| 2 | Waiver fingerprint churn (51 declines/night); then K=12 tail adder ($1,050-1,440) alone > game budget = zero grants for 30h | trimmed-set fingerprint (`ade7b71`); K→48 (yaml); adaptive-K queued |
| 3 | Exchange executes "cancelled" quotes (16 live) AND reports executions that never happened (1 phantom, id 62, deleted) | verify-before-discard vs /portfolio/fills, normal-writer replay, order-id/claim/min_ts guards, runtime position reconcile (`e2e216a`, `d3b1446` era); to-the-cent settlement HALT caught the phantom |
| 4 | Settled legs read UNKNOWN → dark bot (366k RFQs/0 quotes) | settled-fact marginals (graded=0/1, determined/finalized only), batch registration, shared feed-readability predicate (husk books), breaker exemption for confirmed non-live (`a57afc3`,`8f37b2e`,`a7eb32f`,`c338281`) |
| 5 | Peak concentration unpriced → P(book) coin flip; then zero rebates (4096-state plateau cap vs 47k halves grid) | multi-cluster peak steer + magnitude recal (size-independent) + 131k cap + cluster-asymmetry rebate (`dad3d91`,`52f6ef1`,`eca3c43`,`d3b1446`) — live-verified: rebates −60cc firing, stackers +99cc, ladder fill price moved 76.6→75.4¢ |
| 6 | Fair 2¢ under field on champ×scorer (fee-print theory REFUTED 12/12 by ledger: tape prints are RAW) | rho `advance|player_goal:same` 0.45→0.52 (ET-settlement argument; the 800-game regulation measurement stays valid for its own pair) — **rule-8b corpus backtest INCOMPLETE: gather done (data/backtests/wc/inputs.pkl, 1.5M combos), price+analyze stages died silently — re-run `wc_backtest.py price` then `analyze` and confirm, or revert the one value if red** |
| 7 | Hand-tuned numbers decay (3 delta bumps/week) | auto-scaling delta caps `max_market/event_delta_frac` 0.80/1.30 of live bankroll (`fedb268`); absolutes = backstop only |
| 8 | Silence hid 3 outages (operator caught all 3 first) | quote-liveness alarm, pending-set logs, broadened halt greps, paired det telemetry in declines |
| 9 | In-play book drops trip dead-feed breaker (8 halts through the final) | NOT FIXED — post-WC #1, prerequisite for nightly slates |
| 10 | Settlement-cascade equity trough → false $430 give-back kill (current down-state) | NOT FIXED — post-WC #2, prerequisite: settled-unpaid = receivables in equity mark |

## Standing rules (operator memory, binding)

`feedback_no_manual_risk_intervention` (umbrella: NEVER hand-update risk numbers
live; knob bumps are never the fix; repair the automatic mechanism),
`feedback_auto_scaling_caps`, `feedback_no_static_blocklists_count_all`,
`feedback_pbook_diversity_via_pricing`, `feedback_full_state_awareness`,
`feedback_throughput_never_regress` (every build verifies quotes/min before/after),
plus the older set (EST-only, decline reports w/ combos 9AM-12AM, run-and-verify,
no P&L refits, llm-b quarantine PERMANENT).

## Remaining work for 100% hands-off (priority order)

**Gate MLB/WNBA switch:** (1) in-play breaker exemption; (2) settlement
receivables in equity; (3) per-sport structural models armed (MLB props engine
built+gated; WNBA needs odds source); (4) settlement-regime audit of every
inherited rho + doc-verified settlement rules per new series (rule 4);
(5) leg taxonomy w/ UNKNOWN branches for new series.
**Then:** (6) supervised auto-relight after fail-safe halts (human kills stay
human); (7) config hot-reload (no restart per knob); (8) standing field-
calibration loop (automated probe: our fair vs field raw fills); (9) quote-size
degradation near mass-acceptance ceiling (kills sawtooth); (10) adaptive-K +
DC scorer-target cap (identifiability review — repro tool
`tools/diagnostics/repro_esparg_waiver_certifiability_20260719.py`);
(11) ΔP(book)-aware candidate pricing (MC already computes p_profit);
(12) offline MC tool settled-aware; skew clamp stress-scaling; slate-structure
sizing review (game_loss 0.50 = two-game posture); MC capacity at 10-15 games.

## Current armed config highlights (config/prod-live-wc.local.yaml — GITIGNORED, NEVER COMMIT)

markup: soccer base 1¢; tiers <15¢ +4¢, 15-35¢ +2¢ | skew ON incl. peak steer
(defaults; peak_n_clusters 3, min_frac 0.30, tighten cap 150cc) | budgets:
game_loss .50, slate .65, det .36, cvar .35, delta fracs .80/1.30 (absolutes
1500/2500 backstop) | K=48 waiver | max_open_quotes 200, max_contracts_per_quote
2000 | allowlist [KXWC, KXMENWORLDCUP] + champion aliases + pregame schedule
18:45Z — **the MLB/WNBA switch removes aliases + KXMENWORLDCUP, arms KXMLB (and
KXWNBA when ready)**.

## Open loose ends

- Final realized-P&L statement after relight (settlements finish booking).
- Corpus backtest price+analyze (finding 6) — confirm or revert the rho.
- Tape recorder (`data/combomaker-prod.sqlite3`, 119GB, mode=ro ONLY) dead since
  7/17 11:28 PM ET — restart it for tape analytics.
- Probe-RFQs from our requester account were not quoted by our own bot (17
  rivals did) — investigate the self/requester filter when relevant.
- 2 rehydrate mismatches on operator-authorized manual fill rows resolve
  fail-safe LARGER each restart — harmless; clears as those positions settled.
- Monday: **merge `risk-audit-overnight` → main with the llm-b ancestry check
  (NEVER merge llm-b-continuation, no salvage)**, then the sport switch.

## Runbook quick reference

- Fills tally: `select count(*), sum(contracts_centi*price_cc/1000000.0), sum(expected_edge_cc)/10000.0 from fills where at > '<iso>'` on `data/combomaker-prod-live-wc.sqlite3` (**mode=ro for ad-hoc reads; live store is the bot's**).
- Decline detail: `decisions` (kind='decline', context_json.detail) ⋈ `rfqs` (legs_json) by rfq_id.
- Scenario P&L engine: the leg-settling script pattern in this session's logs (settle each fill's legs vs outcome; FRAENG facts all graded).
- Diagnostics: `tools/diagnostics/` (book_mc_profit_ruin — NOT settled-aware yet; rfq_price_probe — fires real RFQs on import, guard usage; argmessi_fair_vs_field).
- All times to the operator in **ET**. Report every crucial event to `docs/reports/` as it happens (rule 7).
