# 2026-07-15 — Full session changelog + live status (MASTER)

**One-glance summary of everything changed this session.** Branch `risk-audit-overnight`,
**31 commits** over baseline `45164f1`, **56 files, +11,754 / −314**. Suite **2026 passed /
0 failed / 0 warnings**. Bot **RELAUNCHED live** on the new engine (prod, `--confirm-live`).
Restore point: `git reset --hard 45164f1`.

Detailed companions: [fanout fix](2026-07-15-rfq-tape-fanout-zero-quotes-fix.md) ·
[audit implementation](2026-07-15-risk-engine-audit-implementation.md).

---

## The arc

```
  START: bot WON RFQ auctions but issued 0 quotes (all declined)
    │
    ├─ (1) ROOT CAUSE = SQL FANOUT, not cap tuning ───────────────► FIXED
    │      held_positions JOINed fills⋈rfqs (1.6M-row tape, up to
    │      12,456 rows/combo) BEFORE SUM → contracts inflated 12,456×
    │      → −259,302 delta → every cap blown. Fix: 1:1 derived tables
    │      + idx_rfqs_market_ticker. Live: 0 → 414 quotes.
    │
    ├─ (2) but 0 FILLS + killed at 6 min ─────────────────────────► DIAGNOSED
    │      the 1 auction we won, we declined at last-look (concentration);
    │      supervisor killed the bot (heartbeat wedged 15.2s — inline MC).
    │
    ├─ (3) OPERATOR handed RISK_ENGINE_AUDIT_ACTION_PLAN.txt ──────► IMPLEMENTED
    │      full re-audit: 10 P0 + 11 P1 + 2 P2. Two overnight workflows
    │      + manual review. All committed, green, adversarially verified.
    │
    ├─ (4) LIVE-READY follow-up ──────────────────────────────────► DONE
    │      P0-7 → preferred structural conditioning; teardown warnings
    │      removed; P0-1 candidate gate WIRED into confirm; kill-switch
    │      question answered (no new switch needed).
    │
    └─ (5) RELAUNCHED live + watching ────────────────────────────► IN PROGRESS
           new engine up: reconcile clean, P0-4 reserve working, off-loop
           MC, generations, risk_audit logging — quoting again.
```

---

## 1. The fill-blocker fix (session start; pre-baseline, preserved)

| Item | Detail |
|------|--------|
| **Bug** | `Store.held_positions` joined `fills ⋈ rfqs ON combo_ticker=market_ticker` and `SUM`ed contracts. The rfqs *tape* has 1.6M rows (one per re-quote, up to **12,456 per combo**), so the join fanned each fill out **before** the SUM → `contracts_centi` inflated up to 12,456× (real 37 → 464,235). |
| **Blast** | Inflated contracts → `analytic_leg_deltas` → **−259,302** delta on the shared ARG-advance leg → every delta/loss/gross cap blown → **0 quotes**. (`entry_price` was fanout-safe, so P&L looked fine — only `contracts` broke.) |
| **Fix** | Pre-aggregate fills + de-dup rfqs legs into **1-row-per-combo derived tables** (1:1 join) + `idx_rfqs_market_ticker`. Regression test `test_held_positions_not_inflated_by_rfq_tape_fanout`. |
| **Verified** | Live DB parity 552,393 → 435 real contracts; ARG-adv delta 259,302 → 47.7; **0 → 414 live quotes**. |

---

## 2. Risk-engine audit — P0 (correctness/safety), all GREEN + committed

| # | Item | Commit | What it does |
|---|------|--------|--------------|
| P0-5 | Exact exchange-quantity reconciliation | `a10dc81` | Exchange position_fp/side/subaccount authoritative; local fills supply cost basis/legs only; mismatch → reserve-larger + reason code; settled/zero excluded |
| P0-4 | Usable MC w/o hiding unmodeled holdings | `bb8361d` | Every held position rehydrated; gated-off holdings **RESERVED** (exact premium in det/gross caps, held outside model ES) instead of skipped; **missing marginal never scored p=0.5** |
| P0-6 | Fractional contracts in MC | `12d83ac` | Removed `max(1, contracts//100)` rounding (37.27→37, 0.40→1); exact centi-contracts, analytic==simulated parity |
| P0-3 | Separate model ES from deterministic max | `33abf6f` | `operative_es=max(ES, det-max)` split into `production_es_99` / `governing_model_es_99` / `deterministic_max_loss` — **two independent gates** (`skip_portfolio_cvar` + new `skip_portfolio_det_max`) |
| P0-2 | Book generations + invalidation | `15708c7` | ExposureBook generation counter; snapshots carry `input_generation`; stale-generation MC results discarded |
| P0-1 | Candidate/reservation-aware portfolio risk | `bcb89cf` | `evaluate_candidate_book_risk`: PRE vs POST book on common random numbers; concentrating candidate charged, balancing credited; fail-closed. **(Wired into confirm in §4.)** |
| P0-9 | Directional-cap mutex-aware hedge semantics | `1e25c15` | Directional cap binds on mutex-aware `directional_by_game_cc` (opposing-advance hedges net, concentration sums); monotonic, **same threshold — no cap raised** |
| P0-8 | Challenger correlation scope | `89449c8` | `_inflate_corr` gets a same-game mask; cross-game pairs stay at measured value (no forced 0→0.5) |
| P0-7 | Structural/fallback dependence bridge | `52ab290`→`1552fb0` | Interim worse-tail challenger → **upgraded to preferred conditioning** (§4) |
| P2-2 | Full-book MC off the event loop | `207c7e1` | `BookRiskPool` (ProcessPool) runs the MC off-loop, generation-safe; **removes the heartbeat-starvation cause** |

## 3. Risk-engine audit — P1 hardening (11) + P2 ops (2), all done

| # | Item | Commit |
|---|------|--------|
| P1.1 | Production AND challenger P(ruin); gate worst | `f145740` |
| P1.2 | Ruin-budget confidence bounds + adaptive samples + common random numbers | `6b357b1` |
| P1.3 | Equity/P&L basis: no entry-to-terminal double count | `350484a` |
| P1.4 | Persist structural inversion residuals; reject/challenge bad fits | `bf31d0e` |
| P1.5 | Public parse/invert/sample/settle structural API (no private imports into risk) | `482dfd9` |
| P1.6 | Tape-derived parity suite (advance/halves/spread/scorers/NO legs/real tickers) | `9c910d2` |
| P1.7 | Mutex-metadata audit; explicit-True-only netting; settlement tripwires | `e5482ee` |
| P1.8 | Label `analytic_leg_deltas` as independence proxies; structural sensitivities | `02db848` |
| P1.9 | Independent challengers (goal rates, DC rho, marginals, settlement, feed errors, cross-game regimes) | `58f9ad8` |
| P1.10 | Durable position ledger (exchange qty/side/cost/fees/subaccount/status/settlement/leg-hash) | `1f5d0e8` |
| P1.11 | Exact RFQ/leg-set provenance replaces `MAX(legs_json)` | `b7f84a0` |
| P2.1 | Orphan-worker prevention (Windows Job Object, parent-death, startup cleanup) | `1b20d7f` |
| P2.2 | Per quote/confirm structured `risk_audit` logging (generation, EV, ES, ruin, det-loss, gross, direction, binding cap) | `cbeb899` |

---

## 4. Live-ready follow-up (operator-directed)

| Ask | Result | Commit |
|-----|--------|--------|
| **P0-7 → make it the structural conditioning** | Fallback corners/cards now conditioned on the game's structural scoreline via a conservative shared factor (only fattens the tail, never thins it; challenger kept as backstop) | `1552fb0` |
| **Does P2-2 need a kill switch?** | **No.** The supervisor heartbeat kill already exists as the genuine-wedge backstop and stays; P2-2 removed the *false trigger* (inline MC) so the maintenance loop beats the heartbeat every 0.5s regardless of the MC | — |
| **Remove the 27 teardown warnings** | Suite now prints **0 warnings** (clean `ProcessPoolExecutor`/aiosqlite shutdown) | `09d24ef` |
| **Wire it all in, ready to run live** | `evaluate_candidate_book_risk` now governs confirms: `_candidate_gate_verdict` runs **inside `if decision.confirm:`** (strictly additive — only admit→decline), **off-loop** via `BookRiskPool.run_candidate`, **fail-closed** (unknown/over-budget/error → `DECLINE_CANDIDATE_RISK`), config `candidate_gate_enabled` (default True, doubles as the gate's kill switch) | `36a5a47` |

---

## 5. Current live status (relaunched 16:08 UTC, new engine)

**Bot is up and quoting on the new engine.** Verified from the live log + DB:

| Signal | Observed |
|--------|----------|
| Startup | `book_risk_pool_started`, `worker_kill_job_created` (P2.1), `prod_preflight_green`, WS connected |
| Reconciliation (P0-5) | `exposure_rehydrated positions=6 reconcile_mismatches=0 reserved=1` |
| Unmodeled holding (P0-4) | gated KXMVE position **RESERVED** (premium in det/gross caps, outside model ES, "no p=0.5") |
| Generations (P0-2) | `snapshot_generation == live_generation` (matched) |
| ES split (P0-3) | `skip_portfolio_cvar` **and** separate `skip_portfolio_det_max` both firing |
| Logging (P2.2) | `risk_audit` events per decision (EV, ES, ruin, direction, binding cap) |
| Quoting | quoting live (125+ quotes); book_risk usable after ~10s marginal warmup |
| **Portfolio MC (usable, structural)** | `es_99=1,297,818` · `challenger_es_99=1,297,667` · `governing_model_es_99=1,375,404` · **`deterministic_max_loss=1,419,974` (separate from ES)** · `p_ruin=0.0` · `p_profit=0.58` · `structural=true` · `usable=true` — the entire new risk stack computes live |
| Top declines | `skip_directional_cap`, `skip_max_open_quotes` — the concentrated ENGARG book behaving correctly (NOT a bug) |
| **Heartbeat — PASSED THE 6-MIN MARK** | Alive ~7+ min (16:08 → 16:15+) with **NO heartbeat wedge, NO emergency kill**. Last run died at exactly 6 min from inline-MC starvation; **P2-2 (MC off-loop) fixed it.** |
| Candidate gate (P0-1) | Wired + live but **not yet exercised** — it only runs when we WIN an auction (`decision.confirm`), which hasn't happened since relaunch. 0 fires so far. |
| Pricing pool | `pool_errors=0`, `pool_timeouts~333` under the firehose (known throughput ceiling, not a fault) |

---

## What remains / decisions owed

1. **Merge to main** — everything is on `risk-audit-overnight`, not yet merged/pushed. Review `git diff 45164f1..HEAD` (56 files) when ready.
2. **`max_open_quotes`** — top decline alongside directional; the plan says **do NOT raise** until reservation/mass-acceptance headroom is measured on sane post-fanout exposure. Now measurable live.
3. **Fills** — watch whether the candidate gate + concentration caps let a won auction actually fill; the balancing lever (opposite-side ENG flow) is the path to more fills.
4. **Live-tape confirmation** — the audit's model changes are test-asserted; re-measure live (contract counts, deltas, ES, ruin) now that the engine is running.

## NEXT STEPS

- **Me:** continue watching the live bot — heartbeat stability past 6 min, candidate-gate behavior, any fill, any halt. Report anomalies immediately.
- **Operator:** decide merge-to-main; do NOT raise `max_open_quotes` until live headroom measured; never refit caps/markup on a P&L window.
- **Kill:** `touch KILL` in the repo root for a clean stop (never `pkill` — orphans workers, though P2.1 now guards that too).
