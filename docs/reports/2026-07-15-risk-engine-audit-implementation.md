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
  balancing candidate can pass). `tests/test_candidate_book_risk.py` (417 lines).
  **⚠️ CRITICAL WIRING GAP (found in post-run verification):** `evaluate_candidate_book_risk`
  / `CandidateBookRisk` are **NOT called anywhere in the live decision path** —
  `grep` finds no reference in `rfq/lifecycle.py`, `risk/limits.py`, or `ops/quote_app.py`
  (only self-references inside `book_risk.py` + the unit tests). The last-look/confirm path
  still gates on the **committed-book** snapshot only. So P0-1's machinery is **built and
  unit-tested but does NOT yet govern any live confirm.** It therefore does **NOT** resolve
  the per-candidate ΔES last-look gate the 2026-07-15 final report §6 deferred — that gate
  remains **unwired**. See §6.
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
- **Per-candidate ΔES last-look gate — machinery built + unit-tested, but NOT WIRED into the
  decision path.** The 2026-07-15 final report §6 explicitly deferred this gate. P0-1
  (`bcb89cf`, `evaluate_candidate_book_risk` / `CandidateBookRisk`) builds and unit-tests the
  machinery (concentrating declines / balancing passes on shared sampled states), **but the
  function is never called from `rfq/lifecycle.py`, `risk/limits.py`, or `ops/quote_app.py`**
  (verified by grep post-run — only self-references in `book_risk.py` + tests). The live
  last-look/confirm still gates on the committed-book snapshot alone. So the gate is
  **superseded by code that exists but does not run in production** — it does not yet
  influence a single confirm. **Wiring `evaluate_candidate_book_risk` into the last-look/
  confirm decision is the #1 remaining task** and is a consequential live-path change that
  should be reviewed before it governs real money (recommended: land it as an additive
  DECLINE-only gate first — it can only make the bot more conservative — before enabling the
  balancing/hedge-credit path).
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

0. **⚠️ WIRE P0-1 INTO LAST-LOOK (the #1 gap).** `evaluate_candidate_book_risk` is built and
   unit-tested but **not called anywhere in the decision path** — the candidate-aware ΔES /
   ΔP(ruin) / ΔEV gate does not yet govern any confirm. Until it is wired, P0-1 is inert in
   production. Recommended sequence: wire it as an additive **decline-only** gate first
   (strictly more conservative — cannot loosen anything), verify on the live ENGARG book,
   then enable the balancing/hedge-credit path under review.
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

- **⚠️ Agent/operator (owner) — TOP PRIORITY:** wire `evaluate_candidate_book_risk` into the
  last-look/confirm path (`rfq/lifecycle.py`). It is currently built + tested but unwired, so
  the candidate-aware gate (the headline of P0-1) does not govern any live decision. Land it
  decline-only first (cannot loosen anything), then enable balancing credit under review.
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

---

## Live-ready follow-up (P0-7 conditioning, teardown, P0-1 wiring, kill-switch answer)

*Appended 2026-07-15 after three additional tasks landed on `risk-audit-overnight`.
No functional code changes were made in **this** append commit; the three tasks below
were each committed separately (hashes given) with their own tests, and the full suite
was re-run green afterward (§ "Final suite re-stamp" below).*

### Per-task status

| Task | Commit | State |
|------|--------|-------|
| **P0-7-preferred** — preferred structural conditioning (condition fallback legs on the game state) | `1552fb0` | **GREEN + committed** |
| **teardown-warnings** — remove `ProcessPoolExecutor` teardown `ResourceWarning`s | `09d24ef` | **GREEN + committed** |
| **P0-1-wiring** — wire candidate-aware gate into last-look/confirm (additive, off-loop) | `36a5a47` | **GREEN + committed** |

All three are on `risk-audit-overnight`, ahead of the `HEAD at this report` (`8f19cf3`)
stamped at the top; the report header is left as-written for provenance, and the true
branch tip after this append is the report-commit hash reported in the reply.

### Landed-item detail (change site + tests)

#### P0-7-preferred — condition fallback legs on the sampled game state  (`1552fb0`)
Upgrades P0-7 from the **interim** worse-tail full-copula *challenger* (§4) to the
spec's **preferred** approach: where a **defensible, measured** scoreline-state link
exists, the straddling copula leg's Gaussian latent is **conditioned on the game's
sampled scoreline intensity** so the covariance enters the **production** tail, not
just a challenger.
- **Change sites:**
  - `src/combomaker/sim/structural_book.py::_shared_structural_factor` — per-sample
    standardized total sampled goals (incl. ET) → standard-normal latent via
    empirical-rank PIT.
  - `src/combomaker/sim/structural_book.py::_sample_copula_conditioned` +
    `CopulaConditioning` — blend `z' = sqrt(1−β²)·z_copula + β·f_game`; preserves each
    marginal and the copula-block correlation exactly, adds only the structural-state
    loading.
  - `src/combomaker/sim/book_risk.py::_copula_leg_loading` + `_build_conditioning` —
    map each straddling copula leg → (plan, β); ungamed / cross-game / group-format /
    no-defensible-link → **β=0** (not conditioned). Governing max folds an
    **independent-split guard** so the conditioned tail may only fatten, never thin,
    below the independent split (SAFETY DEFAULT — only adds tail, never removes a
    decline). The full-copula `bridge_es_99_cc` challenger is **retained** as the
    backstop for the unconditioned (no-link) part.
  - `src/combomaker/ops/config.py::StructuralConfig.corners_et_loading` (default
    **0.10**) + `ops/quote_app.py` wire — the single conservative, width-bearing
    loading, applied **ONLY** to a knockout total-corners leg (the measured ET-window
    channel `advance|corners`, dog +0.23 ↔ fav −0.23, pooled ~0). Total corners are
    measured ⊥ goals (`corners|total = 0.00`); group corners, team corners, and cards
    get 0 → independence + the challenger. `0.0` ⇒ conditioning off (production sample
    reverts to the independent split).
- **Prototype (hard rule 8):** `tools/proto_structural_copula_conditioning.py` — the
  conditioning blend was prototyped and parity-checked in the test harness before the
  port; the parity test pins `_sample_copula_conditioned == engine.sample_leg_values`
  byte-for-byte at β=0.
- **Tests:** `tests/test_structural_conditioning_p0_7.py` (7 new) — covariance appears
  in the production sample + marginal preserved; all-loadings-zero copula parity vs the
  engine sampler; conditioning-off byte-identical to the independent split; governing
  tail ≥ independent split (never thinner); no-defensible-link leg → not conditioned but
  challenger still active; per-leg loading nonzero only for knockout total corners;
  ungamed copula leg unchanged. Full detail:
  `docs/reports/2026-07-15-p0-7-preferred-conditioned-fallback.md`.

#### teardown-warnings — remove `ProcessPoolExecutor` teardown `ResourceWarning`s  (`09d24ef`)
Cleans up the benign-but-noisy teardown warnings flagged in §6 (item "P2-2 leaves benign
teardown warnings") so they cannot mask a real teardown fault.
- **Change site:** `tests/conftest.py` — **TEST-ONLY.** Adds an autouse async fixture
  `_close_leaked_aiosqlite_connections` and a `_hard_stop_connection` helper that
  hard-stops each leaked `aiosqlite` connection's worker thread at test teardown, on the
  correct side of the event-loop-close ordering (fixture finalizers run LIFO; the fixture
  is deliberately async so its teardown body runs while the loop is still open, and it
  never re-enters the async sqlite path so it cannot deadlock the ProcessPool-spawning
  tests — `test_intake` / lifecycle / book-risk). **No live/runtime module was touched**
  — the warnings originated from test-process teardown timing, not production teardown.
- **Tests:** no new test file; the change is validated by the full suite running with the
  teardown `ResourceWarning`s / `PytestUnhandledThreadExceptionWarning`s removed. Residual
  warning count is now materially lower (see the re-stamp).

#### P0-1-wiring — candidate-aware gate wired into confirm (additive, off-loop, fail-closed)  (`36a5a47`)
Closes the §6 "**Per-candidate ΔES last-look gate — machinery built but NOT WIRED**"
gap. `evaluate_candidate_book_risk` / `CandidateBookRisk` (built in P0-1 `bcb89cf`) now
**governs live confirms**.
- **Change sites:**
  - `src/combomaker/rfq/lifecycle.py::_candidate_gate_verdict` (+ `_build_candidate_gate_inputs`)
    — for one contemplated fill, builds the **immutable, picklable** candidate inputs
    (candidate leg-set + resolved marginals + within-game pair-ρ, resolved **on-loop**),
    ships them off-loop, and returns admit/decline. Called from the confirm path at
    `rfq/lifecycle.py` **~line 1320**, **only inside `if decision.confirm:`** — i.e. the
    existing analytic / gross / burst gates have already **ADMITTED** the fill, so this
    gate can only flip an **admit → decline**, never a decline → admit. **Strictly
    additive + decline-only-plus-EV:** it confirms only when the candidate's marginal EV
    is **positive** AND the merged **POST** book's joint-tail / ruin / deterministic /
    gross budgets pass; otherwise it declines with the new reason code.
  - `src/combomaker/core/reasons.py::ReasonCode.DECLINE_CANDIDATE_RISK`
    (`"decline_candidate_risk"`) — the decline reason for this gate.
  - `src/combomaker/ops/pricing_pool.py::CandidateBookRiskInputs` + `_worker_candidate_book_risk`
    + `BookRiskPool.run_candidate` — the **off-loop** CPU-bound MC (~20k samples) so the
    candidate MC never blocks the maintenance-loop heartbeat; falls back to an inline
    eval for paper/backtest/tests. A seeded off-loop run is byte-identical to inline.
  - `src/combomaker/ops/config.py::RiskConfig.candidate_gate_enabled` (default **True**,
    ENFORCED) + `candidate_gate_mc_samples` (default 20_000), wired through
    `ops/quote_app.py`. `candidate_gate_enabled: false` in YAML is the **kill switch** for
    this gate (reverts to prior behaviour); it never loosens any other cap.
  - **Fail-closed:** an UNKNOWN merged marginal (a missing marginal is **omitted**, never
    fabricated as p=0.5), an over-budget POST book, OR **any** exception in the off-loop
    eval ⇒ `DECLINE_CANDIDATE_RISK`. An unmeasured/errored joint tail is never confirmed.
- **Tests:** `tests/test_candidate_gate_wiring.py` (363 lines) — the gate declines a
  concentrating candidate the other gates admit, admits a balancing/positive-EV candidate,
  fail-closes on UNKNOWN marginal / over-budget POST / off-loop error, is skipped when
  `candidate_gate_enabled=False`, and the seeded off-loop verdict matches inline.
  `tests/test_reservation_lifecycle.py` updated (+10 lines) for the new confirm-path step.

### KILL-SWITCH ANSWER (task #44 — "does P2-2 need a kill switch?")

**No new kill switch is required for P2-2.** The genuine-wedge backstop **already exists**
and is retained:

- **The supervisor heartbeat kill already exists** — `src/combomaker/ops/supervisor.py`:
  `heartbeat_wedged()` (fail-closed: a missing/unreadable/stale heartbeat reads as wedged)
  → `supervisor_emergency_kill` / `KillSwitch` writes the KILL file + cancel-all, with
  `heartbeat_timeout_s = 15.0`. This stays as the **BACKSTOP** for a genuine wedge and is
  **not removed**.
- **P2-2 removed the FALSE TRIGGER, not the backstop.** Before P2-2, the full-book MC ran
  **on the event loop**; a long MC under an RFQ firehose could age `data/heartbeat.txt`
  past 15s and trip the supervisor even though the bot was healthy — a false wedge. P2-2
  moved the full-book MC **off the loop** (`BookRiskPool`, generation-stamped inputs), so
  the **maintenance loop** (`ops/quote_app.py::_maintenance_loop`, ~line 1413) beats
  `data/heartbeat.txt` **every 0.5s independent of the MC**. The heartbeat now stays fresh
  regardless of MC duration → the false trigger is gone at the source.
- **If a live firehose still wedges the heartbeat**, it would be from some **other**
  synchronous block (not the MC). The correct fix then is a **modest `heartbeat_timeout_s`
  bump or a thread-based beat** — **NOT** a new switch. The 15s supervisor kill remains the
  genuine-wedge safety net either way.

This resolves task #44's "does it need a kill switch" question: **the existing supervisor
heartbeat kill is the switch; P2-2 eliminated its false trigger and added nothing that
needs a new one.**

### Correction to the earlier "UNWIRED" caveat

The §6 item **"Per-candidate ΔES last-look gate — machinery built + unit-tested, but NOT
WIRED into the decision path"** and the §7 item 0 / §4 P0-1 "⚠️ CRITICAL WIRING GAP" are
now **superseded**: P0-1's candidate-aware gate is **WIRED as of `36a5a47`**. It governs
live confirms as a **decline-only + positive-EV additive gate** — reachable only after the
existing analytic/gross/burst gates admit, off-loop (never blocks the heartbeat), and
fail-closed (UNKNOWN marginal / over-budget POST / any off-loop error ⇒
`DECLINE_CANDIDATE_RISK`). The gate can only make the bot **more** conservative; it never
turns a decline into an admit and never loosens any cap. `candidate_gate_enabled: false`
is its kill switch.

### STILL BEFORE LIVE

1. **Operator diff review + relaunch decision.** This run did NOT relaunch the bot, touch
   the prod DB / prod YAML / `.env`, or loosen any cap. Restore point on doubt:
   `git checkout 45164f1`. Confirm `caps_shadow_mode == False` (enforcing) before relaunch.
2. **Live-tape re-measurement still owed.** Every P0-7-preferred / P0-1-wiring / P2-2 claim
   is **test-asserted**, not tape-confirmed. On the operator's go, relaunch to a fresh
   recording and re-measure: contract counts, ARG-advance delta, quote count, per-cap live
   headroom, and — new here — the **live decline rate of `DECLINE_CANDIDATE_RISK`** (verify
   the candidate gate declines only concentrating fills and does not choke balancing flow).
3. **Verify the candidate gate on the live ENGARG book before trusting hedge credit.** It
   is wired decline-only-plus-EV; watch that the balancing/hedge-credit path admits sane
   ENG-side fills rather than declining everything (its fail-closed default declines).
4. **`max_open_quotes` — do NOT raise** until reservation/mass-acceptance headroom is
   measured on sane post-fanout exposure (not taken this run).
5. **Heartbeat under live firehose.** The false trigger is fixed (above); if a *genuine*
   wedge from another synchronous block appears live, apply a modest `heartbeat_timeout_s`
   bump or a thread-based beat — not a new switch.
6. **Preserved-fix invariants to re-verify post-relaunch:** no `fills JOIN rfqs` aggregate
   in `held_positions`; mutex-aware directional cap (P0-9); ES-vs-deterministic split
   (P0-3); fail-closed on UNKNOWN/missing/stale marginals; `caps_shadow_mode == False`.

### Final suite re-stamp

```
.venv/Scripts/python.exe -m pytest -q
2026 passed, 3 deselected in 107.11s (0:01:47)
```

**Exact counts: 2026 passed, 0 failed, 3 deselected.** Tree green at the branch tip
after this append. Note the `-q` summary line no longer reports a warnings count — the
`teardown-warnings` task (`09d24ef`) removed the `ProcessPoolExecutor` /
`aiosqlite`-teardown `ResourceWarning`s that the earlier §8 stamp reported as
"27 warnings". Count rose 2010 → **2026** (+16) over the earlier §8 stamp:
+7 from P0-7-preferred (`test_structural_conditioning_p0_7.py`) and +9 net from the
P0-1-wiring suite (`test_candidate_gate_wiring.py` + the `test_reservation_lifecycle.py`
update); the teardown task is test-harness-only and adds no test count.

