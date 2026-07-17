# 2026-07-17 ~03:30Z — SESSION STATE / RESUME (operator may hit usage limit — this is the exact moment)

**One-line:** bot LIVE + healthy on `15ebe40` (Batch-1 active, verified working);
markup raise + haircut ARMED-or-BUILT for restart 2; **the haircut is BUILT on the
uncommitted tree and only needs VERIFY → FIX → commit → restart 2** (its verify
workflow was mid-flight when this report was written).

## Exactly where things stand

| Item | State |
|---|---|
| **Bot** | LIVE, `live_20260717_batch1_slots120.log` (launched 02:20:07Z on `15ebe40`), preflight green, 10 positions/0 mismatch, pool workers=2, champion aliases + schedule table active. Recorder LIVE (`observe_20260716_postcrash.log`). |
| **Batch-1** | LANDED (`15ebe40`, pushed) + LIVE-VERIFIED: steady-state funnel (23.8 min) quoted/min 132.7→**201.7**, speed-miss 32.1%→**6.9%**, handled-in-time 67.9%→**93.1%**. Wall = `skip_game_loss_cap` (89% of in-scope) = the haircut's target. |
| **max_open_quotes 120 + F1 pre-gate armed** | LIVE since restart 1 (02:20Z). |
| **Markup raise** | **ARMED in yaml (not yet live — activates restart 2):** soccer base 1→2¢, tiers <2¢ 5→**6¢**, <10¢ 4→**4.5¢**, <35¢ 2→**3.5¢**; corners adder 4.5¢ unchanged. Loader-validated. Evidence: 3rd consecutive probe round with us 0.8–1.6¢ INSIDE the best maker on every fill. |
| **HAIRCUT (the one open build)** | **BUILT on the uncommitted tree** — `risk/exposure.py` (+257), `risk/limits.py` (+40), `rfq/lifecycle.py` (+158), `ops/config.py`, `core/reasons.py`, `tests/test_resting_haircut.py`, `tools/proto_resting_haircut.py`, draft report `2026-07-17-resting-quote-haircut-quote-time.md` (build agent's). Spec: quote-time caps count resting quotes at **`resting_quote_weight` (default 1.0 = today; operator arms 0.40)** + burst floor (3 largest resting @ 100%); **confirm-time/last-look UNTOUCHED at 100%**; event-driven post-fill pull (analytic-only, delete-only); commit-budget property test ("no accept sequence can commit past the budget"). Verify workflow `wf_7663707a-704` was mid-Verify when this was written. |
| **Suite/lint on the haircut tree** | NOT independently verified yet — that is the next gate. Baseline: 2263/0, ruff 13, mypy 6. |

## NEXT STEPS — in order (any session can execute)

1. **Verify the haircut tree.** Either resume the workflow —
   `Workflow({scriptPath: "C:\\Users\\aahys\\.claude\\projects\\C--Users-aahys-kalshi-combos-TWO\\4b42e809-d365-447f-bd21-ec1dd6a202b0\\workflows\\scripts\\quote-time-resting-haircut-wf_7663707a-704.js", resumeFromRunId: "wf_7663707a-704"})`
   (completed build agent returns cached; Verify/Fix re-run) — **or** verify directly:
   full suite + ruff/mypy vs baseline, then 3 adversarial lenses on `git diff`
   (money-path isolation: confirm-time bit-identical with weight armed; accept-sequence
   attack on the commit budget; fold monotonicity + F1 pre-gate lemma with weight active;
   post-fill pull hot-path cost/races). Fix CONFIRMED findings.
2. **Arm the haircut**: `resting_quote_weight: 0.40` (+ floor key if separate) in
   `config/prod-live-wc.local.yaml` risk section — then static-load the yaml
   (`load_config(Path('config/prod-live-wc.local.yaml'), env='prod', mode='quote', confirm_live=True)`).
3. **Commit + push** (never commit the yaml — gitignored), report per rule 7.
4. **RESTART 2** (activates haircut + markup raise): kill WHOLE bot tree (supervisor is a
   child; recorder separate — leave it), purge `KILL data/needs_reconcile data/heartbeat.txt
   data/supervisor_heartbeat.txt`, relaunch detached:
   `Start-Process cmd '/c','.venv\Scripts\python.exe -m combomaker.ops.cli run --env prod --mode quote --confirm-live --config config\prod-live-wc.local.yaml > data\live_logs\live_20260717_haircut_markup.log 2>&1'`
   Live-verify: preflight green, `pricing_aliases_active`, quotes flowing, then
   `tools/diagnostics/throughput_funnel.py` (expect the 89% `skip_game_loss_cap` wall to
   collapse) + `tools/diagnostics/book_mc_profit_ruin.py` (watch P(profit)/P(ruin) as book grows).
5. **Sat 7/18 FRAENG (3rd place) = canary**: watch `lastlook_waiver.*`, self-declined wins,
   lapse rate, fill rate vs new markup, `pre_gate.*`, `rfq.registry_reset`, funnel windows.
   Post-fill: run the RFQ probe ritual (`tools/diagnostics/rfq_price_probe.py` — NOTE it runs
   its hardcoded TARGETS on import; guard imports).
6. **Sun 7/19 final**: 200-slot decision (if Sat clean), then game-cap decision AFTER both
   games (operator, on waiver-netted utilization).
7. **Queued after haircut: SKEW mutex fix** ([[feedback_balance_via_maker_quoting]] memory):
   inventory skew (risk/skew.py, DARK, shadow-computing on every quote) is the operator's
   book-adaptive quoting lever, but its book-direction input is the RAW per-game delta sum —
   MUTEX-BLIND: measured on 63k live shadow events, ARG-champ combos (which HEDGE our
   short-ESP book) get widened 63/63 instead of rebated; BTTS-no (same-market complement)
   nets correctly. Fix = feed the classifier P0-9's mutex-aware per-game direction, tests,
   shadow re-verify the flip, THEN arm. Operator decides Sat vs Sun canary.
8. **Then:** merge to main (**llm-b ancestry check — NEVER merge/salvage llm-b-continuation**)
   → MLB+WNBA + Batch-2 (WS sharding — residual 13.5k `skip_rfq_closed`/24min is WS-lag races).

## Tonight's findings (all committed except the haircut)

- **Adversarial verify round 2** (`a397efb`): Family-4 advance-complement (pens q² underprice
  ~3-5¢, live on champion flow) + alias event-fold E2 guard + rotation-marker survival +
  config canonicalization + heartbeat throttle. Report: `2026-07-16-adversarial-verify-round2-and-alias-relaunch.md`.
- **Batch-1** (`15ebe40`): F2 liveness (positive-deletion-only), F1 pre-gate (55k fuzz, 0 false
  skips), record-after-price fast-lane (seen_at keeps pickup semantics), F5. Report:
  `2026-07-17-batch1-throughput-remainder-landed.md`.
- **Book MC** (tool `book_mc_profit_ruin.py`, committed): P(profit) **62.0%**, P(ruin)
  **0.0000%** (95% up 0.0007%), ES99 $242.57, det-max $279.99 = 13.6% of $2,051.78 equity.
  Caveat: ES99≈det-max — the book is one-sided (all NO-parlays, FRA/ESP/BTTS-yes heavy);
  scaling is safe only two-sided (haircut + waiver + skew provide that).
- **Throughput funnel** (tool `throughput_funnel.py`, committed): the operator KPI —
  quoted vs risk-stops (don't count) vs speed-misses.
- **RFQ probes ×5** (~03:00Z): our fair == the sharpest maker's on every fill incl. the
  champion-alias combo; field median 3–9¢ above us → the markup raise.
- **First champion-leg fill**: ESP-champ × FRA-3rd, NO @ 63.40¢ × 62.77ct (20:02Z) — the
  alias converts.
- **Operator doctrine recorded**: [[feedback_no_double_risk_layers]] (no double-counting
  risk layers; blocking quotes is -EV) + [[feedback_balance_via_maker_quoting]] (NEVER
  taker-hedge; balance by rebating offsetting maker flow, book-adaptively).

## NEXT STEPS footer (owners)

- **Next session (me):** step 1–4 above (verify → arm → commit → restart 2), then game-day
  watch. All tools/commands above are copy-paste ready.
- **Operator:** Sat-vs-Sun call for the skew fix canary; 200-slot + game-cap decisions after
  the games; maker-fee residuals (admission-EV policy + settlement fee_cost semantics) before
  ever arming `maker_fee_active_prefixes`.

---

## ADDENDUM (~04:05Z) — RESTART 2 EXECUTED, EVERYTHING LIVE

The "one remaining step" is DONE. Haircut verify completed: 3-lens fleet found 1
SERIOUS CONFIRMED (burst floor broke on the mutex-folded axes at the superadditive
1→2 ME-event transition — the floor's base term now tracks the COMBINED census's
netting regime, still monotone) + 1 MINOR (post-fill pull ran in the
commit-to-drop double-count window — now schedules after the filled quote leaves
the open set); both fixed + regression-tested. Suite independently re-run
**2290/0**; ruff 13 / mypy 6 baseline. Committed **`9a27682`**, pushed. Yaml armed:
`resting_quote_weight: "0.40"`, `resting_floor_count: 3` (+ the markup raise from
earlier). **RESTART 2 at 03:51:19Z** → `live_20260717_haircut_markup.log`:
preflight green, 12 positions / 0 mismatches, pool workers=2, supervisor up.
Post-restart funnel measurement in flight. Remaining next steps unchanged from
the list above from step 5 (Saturday canary watch) onward; skew mutex fix is now
the next build.

## ADDENDUM 2 (~04:20Z) — HAIRCUT MEASURED WORKING; SLOTS 120→200 (RESTART 3)

First 9.8 min of the haircut: **skip_game_loss_cap collapsed 6,380/min → 659/min
(−90%)** and the book immediately filled all 120 resting slots —
skip_max_open_quotes became the #1 decline (2,129/min on QUIET ~4am flow).
Operator's stated range was 100-200 → armed **max_open_quotes: 200** (with the
0.40 haircut, 200 resting ≈ 80-slots-equivalent quote-time pressure, LESS than
the old 60@100%). **RESTART 3 at 04:04:57Z** (`live_20260717_slots200.log`):
preflight green, 12 positions / 0 mismatches. SATURDAY WATCH (unchanged + one
addition): confirm-time still counts resting at 100%, so at 200 slots watch
self-declined wins / waiver invocation rate — drop slots back if the waiver
can't keep up. Elevated `skip_rfq_closed` (~1,340/min) persists = the WS-lag
POST race = Batch-2 (WS sharding) territory. Post-fill pull + waiver metrics
both at 0 so far (no fills in the quiet window). Final overnight platform:
**haircut 0.40 + floor 3 + pull + 200 slots + markup 2/3.5/4.5/6¢ + F1 pre-gate
+ Batch-1 + champion aliases + schedule table, all on `9a27682`.**
