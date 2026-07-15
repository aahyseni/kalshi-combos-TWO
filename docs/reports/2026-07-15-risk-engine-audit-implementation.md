# Risk-Engine Audit — Implementation Report

**Date:** 2026-07-15
**Branch:** `risk-audit-overnight`
**Baseline (restore point):** `45164f1` — *checkpoint: baseline before overnight risk-engine audit implementation*
**HEAD at this report:** `8f19cf3`
**Authoritative spec:** `RISK_ENGINE_AUDIT_ACTION_PLAN.txt` (repo root), dated 2026-07-15, "UPDATED AFTER FANOUT FIX".
**Live-money context:** kalshi-combos-TWO, ~$2k bankroll. This run made **NO functional
code changes in this report commit** and did **NOT** relaunch the bot, touch the prod DB /
prod config / `.env`, or loosen any cap.

> This report is written zero-bias for downstream LLM/operator consumption. Caveats,
> stubs, and deferrals are stated as plainly as the landed work. Where a claim is a test
> assertion rather than a live-tape measurement, it says so.

---

## 1. Provenance — two workflows fed this branch

| Workflow | P0/P1/P2 items landed |
|----------|-----------------------|
| First overnight run `wf_59dd860b-769` | **P0-5, P0-4, P0-6, P0-3, P0-2, P0-1** (commits `a10dc81`, `bb8361d`, `12d83ac`, `33abf6f`, `15708c7`, `bcb89cf`) — all GREEN. |
| This remainder run | **P0-9** (`1e25c15`, committed after review), then **P0-8** (`89449c8`), **P0-7** (`52ab290`), **P2-2** (`207c7e1`), the full **P1.1–P1.11** hardening set, **P2.1** (`1b20d7f`), **P2.2** (`cbeb899`), plus `d729806` (mandatory-test coverage) and `8f19cf3` (documentation corrections). All GREEN. |

Note on numbering: the spec's P0 block is titled `P0-1 … P0-9`. The task brief refers to
the same two items by their spec titles — "P0-8 Challenger correlation scope" and
"P0-7 Structural/fallback same-game dependence." Item order below follows the spec.

The **RFQ-tape fanout fix** in `Store.held_positions` (the actual root cause of the
zero-quote state described in the final report) is **PRE-baseline** — it is already
present at `45164f1` and is preserved, not re-implemented, by this branch. See §7.

---

## 2. Status of all 10 P0 items

Implementation order shown; all committed on `risk-audit-overnight`.

| # | P0 item (spec title) | Commit | State |
|---|----------------------|--------|-------|
| P0-5 | Exact exchange-quantity reconciliation | `a10dc81` | **GREEN + committed** (first run) |
| P0-4 | Usable MC without hiding unmodeled holdings | `bb8361d` | **GREEN + committed** (first run) |
| P0-6 | Fractional contracts in MC | `12d83ac` | **GREEN + committed** (first run) |
| P0-3 | Separate model ES from deterministic maximum loss | `33abf6f` | **GREEN + committed** (first run) |
| P0-2 | Book generations and immediate invalidation | `15708c7` | **GREEN + committed** (first run) |
| P0-1 | Candidate- and reservation-aware portfolio risk | `bcb89cf` | **GREEN + committed** (first run) |
| P0-9 | Directional-cap hedge semantics (mutex-aware) | `1e25c15` | **GREEN + committed** (this run, after review) |
| P0-8 | Challenger correlation scope (same-game-only inflation) | `89449c8` | **GREEN + committed** (this run) |
| P0-7 | Structural/fallback same-game dependence bridge | `52ab290` | **GREEN + committed** (this run) |
| P2-2 | Full-book MC off the event loop, generation-safe | `207c7e1` | **GREEN + committed** (this run) |

**All 10 items are GREEN and committed. None NOT-DONE.**

---

## 3. Status of P1 hardening (11 items) and P2 operational (2 items)

| # | P1/P2 item | Commit | State |
|---|-----------|--------|-------|
| P1.1 | Compute production AND challenger P(ruin); gate on the worst credible model | `f145740` | **done** |
| P1.2 | Confidence bounds / adaptive sample counts near the ruin budget; common random numbers | `6b357b1` | **done** |
| P1.3 | Prove equity/P&L basis does not double-count entry-to-terminal P&L on marked equity | `350484a` | **done** |
| P1.4 | Persist structural inversion residuals; reject/challenge inconsistent fits | `bf31d0e` | **done** |
| P1.5 | Expose a PUBLIC parse/invert/sample/settle structural API (no private-pricing imports into risk) | `482dfd9` | **done** |
| P1.6 | Tape-derived parity for regulation/advance, halves/full-time, spread windows, scorers, NO legs, real tickers, enabled series | `9c910d2` | **done** |
| P1.7 | Audit mutex metadata; retain explicit-True-ONLY netting; add settlement tripwires | `e5482ee` | **done** |
| P1.8 | Label `analytic_leg_deltas` as independence proxies; use structural scenario sensitivities | `02db848` | **done** |
| P1.9 | Independent challengers for goal rates, DC rho, marginals, settlement rules, mutex metadata, feed errors, cross-game regimes | `58f9ad8` | **done** |
| P1.10 | Durable position ledger with exchange qty/side, cost, fees, subaccount, status, settlement, reconciliation time, leg-set hash | `1f5d0e8` | **done** |
| P1.11 | Replace `MAX(legs_json)` provenance with exact originating RFQ/leg-set identity | `b7f84a0` | **done** |
| P2.1 | Prevent orphaned workers: parent-owned process group / Windows Job Object, parent-death detection, finally close/join, startup cleanup | `1b20d7f` | **done** |
| P2.2 | Log per quote/confirm: generation, age, candidate EV, pre/post ES/P(ruin), deterministic loss, gross, direction, reservations, model split/residual, fallback reason, binding cap | `cbeb899` | **done** |

**All 13 P1/P2 items are done and committed.**

---

## 4. Landed-item detail (change site + tests) — this-run items

### P0-9 — Directional-cap mutex-aware hedge semantics  (`1e25c15`)
- **Change:** `src/combomaker/risk/exposure.py` + `src/combomaker/risk/limits.py`. The
  directional cap now recognizes **explicit-mutex opposing-advance hedges** instead of
  summing a raw `delta_by_game`. Delta limits were **NOT raised**; they remain monotonic
  hard directional/model-sensitivity backstops (per spec: "Do not simply raise delta
  limits"). Richer hedge credit is awarded only through candidate-aware MC, not by
  loosening the analytic backstop.
- **Prototype:** `tools/proto_mutex_directional.py` (265 lines) exercised the mutex-aware
  direction on the actual ENGARG book shape before the change was ported into the live
  module (testing-isolation rule 8).
- **Tests:** `tests/test_directional_hedge_cap.py` (260 lines) — asserts the all-accepted
  snapshot dominates every realizable accepted subset, and that a non-mutex book still
  hits the raw backstop.

### P0-8 — Challenger correlation scope: same-game-only inflation  (`89449c8`)
- **Change:** `src/combomaker/sim/book_risk.py`. Added `_same_game_mask(model)` and a
  `same_game_mask` parameter to `_inflate_corr`. The challenger correlation now inflates
  **only same-game off-diagonal pairs**; cross-game entries keep their measured value
  (independence at 0), so a hedged cross-game book is no longer force-correlated to +0.5.
  `same_game_mask=None` ⇒ NO pair is inflated (fail-safe identity). Cross-game shock, if
  ever wanted, is documented as belonging in a separately named regime scenario (P1.9).
- **Tests:** `tests/test_sim_book_risk.py` (+131 lines) — asserts cross-game 0→0 preserved
  under inflation, same-game pairs move to the target, and matrix stays near-PSD.

### P0-7 — Structural/fallback same-game dependence bridge  (`52ab290`)
- **Change:** `src/combomaker/sim/book_risk.py`. The structural split samples a game's
  structural scoreline block and its copula-only corners/cards block from **independent**
  rng calls, discarding same-game cross-block dependence. Added `_bridge_needed(...)`
  (True iff some game holds BOTH a structural leg and a copula leg), a full-copula
  **bridge challenger** (`bridge_es_99_cc`, `bridge_active`), and folded it into
  `governing_model_es_99_cc = max(production, challenger, bridge, struct)` — the model gate
  now takes the **worse tail** (interim approach per spec; the report does not claim exact
  all-leg hedging). A structural value sampler now carries its structural/copula split so
  the caller can decide whether the bridge must run.
- **Tests:** `tests/test_structural_bridge_p0_7.py` (296 lines) — bridge fires only when a
  game straddles both blocks; gate takes the worse of split-vs-full-copula; ungamed copula
  legs fail closed (never straddle a structural game).

### P2-2 — Full-book MC off the event loop, generation-safe  (`207c7e1`)
- **Change:** new `src/combomaker/ops/pricing_pool.py::BookRiskPool` (mirrors `JointPool`:
  a `ProcessPoolExecutor` + async `run`), `BookRiskInputs` (frozen `BookModel` +
  `input_generation` stamp) and `_worker_book_risk`. `src/combomaker/rfq/lifecycle.py`
  captures the position generation (P0-2) **before** reading positions, builds the
  IMMUTABLE `BookModel` on-loop, ships it off-loop, and **discards a snapshot whose
  `input_generation` has since been superseded** (`_publish_book_risk` / `_book_risk_for_check`).
  `compute_book_risk` takes an explicit seed so a seeded off-loop run is byte-identical to
  the on-loop run. `src/combomaker/ops/quote_app.py` wires pool start/shutdown.
- **Tests:** `tests/test_book_risk_offloop.py` (321 lines) — seeded off-loop snapshot
  equals on-loop; a stale-generation result is discarded on publish; the event loop is not
  blocked for the MC duration.

### First-run P0 items (already committed; preserved, not modified this run)
- **P0-1** `bcb89cf` — `evaluate_candidate_book_risk` + `CandidateBookRisk` in
  `book_risk.py`; candidate + reservation positions enter the sampled leg universe with
  common random numbers (concentrating candidate crosses the ruin/ES budget and declines;
  balancing candidate can pass). `tests/test_candidate_book_risk.py` (417 lines). **This
  resolves the per-candidate ΔES last-look gate that the 2026-07-15 final report §6
  explicitly deferred** — see §6 below.
- **P0-2** `15708c7` — book generation counter + immediate invalidation.
- **P0-3** `33abf6f` — sampled model ES separated from the deterministic all-hit maximum
  loss (two distinct caps). Preserved: ES-vs-deterministic split.
- **P0-4** `bb8361d` — MC no longer silently hides unmodeled holdings (unknown held
  marginal fails closed rather than defaulting to a usable p=0.5).
- **P0-5** `a10dc81` — exact exchange-quantity reconciliation.
- **P0-6** `12d83ac` — fractional contracts in MC.

### P1 / P2 landed-item detail
- **P1.1** `f145740` — production AND challenger P(ruin), gate on the worst; `book_risk.py`
  + `tests/test_sim_book_risk.py`, `tests/test_candidate_book_risk.py`.
- **P1.2** `6b357b1` — ruin-budget confidence bounds + adaptive sample counts + common
  random numbers; `pricing_pool.py`, `lifecycle.py`, `limits.py`, `book_risk.py`;
  `tests/test_book_risk_ruin_confidence.py` (272 lines).
- **P1.3** `350484a` — equity/P&L basis proof (no entry-to-terminal double count on marked
  equity); `lifecycle.py`, `risk/balance.py`, `book_risk.py`;
  `tests/test_book_risk_equity_basis.py` (283 lines).
- **P1.4** `bf31d0e` — persist structural inversion residuals + reject/challenge
  inconsistent fits; `ops/persistence.py`, new `pricing/fit_challenge.py`, `pricing/joint.py`,
  `pricing/structural.py`; `tests/test_fit_challenge.py` (159 lines).
- **P1.5** `482dfd9` — new public `pricing/structural_api.py` (128 lines) parse/invert/
  sample/settle; `sim/structural_book.py` migrated off private-pricing imports;
  `tests/test_structural_api.py` (158 lines).
- **P1.6** `9c910d2` — tape-derived parity suite `tests/test_structural_tape_parity.py`
  (350 lines) covering regulation/advance, halves/full-time, spread windows, multiple
  scorers, NO legs, real tickers, enabled series. **Test-only commit.**
- **P1.7** `e5482ee` — mutex-metadata audit, explicit-True-only netting retained,
  settlement tripwires; `risk/exposure.py`, `risk/settlement.py`;
  `tests/test_mutex_settlement_tripwire.py` (292 lines).
- **P1.8** `02db848` — `analytic_leg_deltas` labeled as independence proxies; structural
  scenario sensitivities used where available; `risk/exposure.py` (+294 lines);
  `tests/test_exposure_structural_deltas.py` (189 lines).
- **P1.9** `58f9ad8` — independent structural-parameter challengers (goal rates, DC rho,
  marginals, settlement rules, mutex metadata, feed errors, explicit cross-game regimes);
  `book_risk.py` (+257 lines); `tests/test_structural_param_challenger_p1_9.py` (336 lines).
- **P1.10** `1f5d0e8` — durable position ledger (exchange qty/side, cost, fees, subaccount,
  status, settlement, reconciliation time, leg-set hash); `ops/persistence.py` (+154 lines),
  `risk/exposure.py`; `tests/test_position_ledger.py` (156 lines).
- **P1.11** `b7f84a0` — exact originating RFQ/leg-set identity replaces `MAX(legs_json)`
  provenance; `ops/persistence.py`; `tests/test_rehydrate_positions.py` (+69 lines, plus
  +62 lines in `d729806` mandatory-test coverage).
- **P2.1** `1b20d7f` — parent-owned process group / Windows Job Object, parent-death
  detection, finally close/join, startup cleanup; new `ops/process_group.py` (522 lines),
  `ops/pricing_pool.py`, `ops/quote_app.py`; `tests/test_process_group.py` (172 lines).
- **P2.2** `cbeb899` — per quote/confirm structured logging of book/snapshot generation,
  age, candidate EV, pre/post ES/P(ruin), deterministic loss, gross, direction,
  reservations, model split/residual, fallback reason, binding cap; `rfq/lifecycle.py`
  (+219 lines); `tests/test_lifecycle.py` (+125 lines).

---

## 5. Consolidated safety-review verdict

```json
{"verdict":"PASS","problems":[],"must_fix":[]}
```

Basis: all 10 P0 + 11 P1 + 2 P2 items committed with tests; full suite green (§8); the
preserved prior fixes were checked to be intact — no direct `fills JOIN rfqs` aggregate in
`held_positions` (§7), the directional cap remains mutex-aware (not raw `delta_by_game`,
P0-9), and the ES-vs-deterministic split is intact (P0-3). No cap was raised or loosened;
`caps_shadow_mode` default remains `False` (enforcing). Fail-closed behavior on
UNKNOWN/missing/stale marginals is preserved (P0-4, P0-8 mask identity, P0-7 ungamed-leg
fail-closed).

---

## 6. NOT VERIFIED / DEFERRED / STUBS

Stated plainly — do not treat as resolved.

- **Live-tape re-measurement not performed this run.** The fanout-fix report claimed
  "435 correct contracts, ARG delta ~47.7, suite 1789/0, 228+ quotes in ~3.5 min." This
  run did NOT relaunch the bot (SAFETY DEFAULT) and therefore did **not** re-measure live
  exposure, quote count, or the post-fanout cap headroom. All P0-7/P0-8/P0-9/P2-2 claims
  in §4 are **test-asserted**, not live-tape-confirmed.
- **Per-candidate ΔES last-look gate — previously deferred, now landed but not tape-graded.**
  The 2026-07-15 final report §6 explicitly deferred this gate. P0-1 (`bcb89cf`,
  `evaluate_candidate_book_risk`) implements it, but the "concentrating declines / balancing
  passes" behavior is verified by unit tests, not by a live ENGARG book. The final report's
  §6 "deferred, not done" note is therefore **superseded by code but not by live evidence.**
- **`caps_shadow_mode` is a live operator switch.** Default is `False` (enforced). A
  future re-shadow of any new cap flips it to `True`, at which point that cap layer
  observes-only. Verify it is `False` before relaunch.
- **P0-7 bridge is the interim approach.** Per spec, it gates on the worse of the
  structural-split vs full-copula tail; it does **not** condition fallback legs on the
  structural game state (the "preferred" path). The report does not claim exact all-leg
  hedging.
- **P2-2 leaves benign teardown warnings.** The final suite emits ~26 warnings, including
  `PytestUnhandledThreadExceptionWarning` / "Event loop is closed" ResourceWarnings from
  `ProcessPoolExecutor` teardown in the off-loop path. These are non-failing but should be
  cleaned up before they mask a real teardown bug.
- **Supervisor heartbeat kill (spec/task #44)** is a separate operational control. P2-2
  moved the MC OFF the loop (removing the *cause* of a stale heartbeat) but did **not** add
  a supervisor heartbeat kill switch. Confirm whether one is required before relaunch (§7).
- **P1.6 / P1.3 are proof/parity-only** (test-only or assertion-only commits). They add no
  runtime behavior; their value is the guarantee, not new enforcement.

---

## 7. What remains before live

1. **Operator reviews the full diff:** `git diff 45164f1..HEAD` (48 files, +9537 / −303).
   Restore point on any doubt: `git checkout 45164f1`.
2. **Relaunch decision is the operator's.** This run did **NOT** relaunch the bot and did
   **NOT** loosen any cap (directional, `max_open_quotes`, gross, game/slate loss), per the
   SAFETY DEFAULT. All kill switches and enforced controls are intact.
3. **Supervisor heartbeat kill (task #44).** P2-2 removed the stale-heartbeat *cause* by
   moving full-book MC off the event loop, but did not land a heartbeat kill switch itself.
   The operator must decide whether an explicit supervisor heartbeat kill is still required
   and, if so, land it separately.
4. **`max_open_quotes` decision — do NOT raise.** Per spec P2 §5, treat it as an operator
   risk decision and do not raise it until reservation/mass-acceptance **headroom is
   measured on sane post-fanout exposure** (a live measurement this run did not take).
5. **Preserved-fix invariants to re-verify post-relaunch:** no `fills JOIN rfqs` aggregate
   in `held_positions`; `test_held_positions_not_inflated_by_rfq_tape_fanout` present and
   passing; mutex-aware directional cap (P0-9) active; ES-vs-deterministic split (P0-3)
   intact; `caps_shadow_mode == False`.

---

## 8. Final suite stamp

```
.venv/Scripts/python.exe -m pytest -q
2010 passed, 3 deselected, 27 warnings in 97.18s
```

**Exact counts: 2010 passed, 0 failed, 3 deselected, 27 warnings.** Tree green at HEAD.
(The warnings are non-failing teardown ResourceWarnings; their count is non-deterministic
run-to-run — 26–27 — because they arise from `ProcessPoolExecutor` teardown timing. See §6.)

---

## NEXT STEPS

- **Operator (owner):** review `git diff 45164f1..HEAD`; confirm `caps_shadow_mode == False`;
  decide relaunch. Do NOT raise `max_open_quotes` or any cap until post-fanout exposure
  headroom is measured live.
- **Operator (owner):** decide the supervisor heartbeat kill (task #44) — P2-2 removed the
  cause but did not add the kill switch.
- **Next agent:** on the operator's go, relaunch to a fresh recording and **re-measure**
  live: contract counts, ARG-advance delta, quote count, and each cap's live headroom —
  turning the §4 test-asserted claims into tape-confirmed ones. Do NOT tune caps from
  pre-fix exposure.
- **Next agent:** clean up the ProcessPoolExecutor teardown warnings (§6) so they cannot
  mask a real off-loop teardown fault.
- **Never** refit any cap or markup on a P&L window; changes come from measurement or
  structural evidence only.
