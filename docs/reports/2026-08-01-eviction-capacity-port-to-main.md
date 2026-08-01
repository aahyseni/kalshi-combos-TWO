# 2026-08-01 — Eviction/capacity feature PORTED to main, self-contained, flag-off dark

**Branch:** `eviction-capacity-port` (originally off main `022e47f`; REBASED 2026-08-01 onto origin/main `cdfdcc8` — the reading-b fleet's ratified det-max demotion / kill-anchor re-land pushed first, so this branch rebased + RE-PROVED per the whoever-pushes-second rule; only README/tape-facts watermark conflicts, no code conflicts). **Status: PORTED,
defaults OFF = today's behaviour. ADVERSARIAL GATE (independent re-execution,
2026-08-01 morning): NO-SHIP on the original derivation — one CONFIRMED
arming blocker (§ Adversarial gate, finding G1). G1 REPAIRED SAME DAY
(§ G1 repair below): the capacity derivation is now
`min(exchange-tier form, withdraw-budget form)` — both terms derived from
config/documented costs the code already reads; today it self-consistently
reproduces ~the hand cap (191 at the measured flow) and scales automatically
with any future withdraw-budget raise. The flags-off code is byte-identical
(re-proven) and the diversity key survived every attack. Branch pushed after
the repair + fresh-worktree self-containment proof; merge to main happens at
the operator restart boundary.**

## Why a port was needed (the 7/31 lesson)

The demotion fleet's `7162a4d` carried BOTH the kill-anchor gate AND this
feature's config/lifecycle wiring — and shipped lifecycle hunks importing
`rfq/eviction_value.py` WITHOUT committing that file. Boot crash on main
20:21, watchdog latched, revert `d121e46`. The quote_app capacity probe and
the module itself survived only as uncommitted work in the `kct-reanchor`
worktree, interleaved with the withdrawn burst-floor diffs.

**THE RULE THIS COMMIT ENFORCES: the commit must be self-contained — proven in
a fresh scratch worktree of main+commit (import quote_app, full suite, vitals
fast tier) BEFORE any push. A green suite in a dirty tree proves nothing.**

## What was done

| Step | What | Result |
|------|------|--------|
| 1 | Worktree residue strip (`kct-reanchor` @ `4c0fe7e`) | burst_floor.py + 2 proto tools deleted; exposure.py `resting_floor_rule`/`BurstFloorRule` uncommitted wiring reverted (file back to HEAD); orphaned burst-floor test already absent (pycache purged); V12/V13 flow vitals left UNTRACKED (parked burst-floor instrumentation, NOT this feature) |
| 2 | Extract ONLY eviction/capacity hunks onto main | `eviction_value.py` (verbatim), config.py 2 flags + validator, lifecycle.py 9 hunks (imports, LifecycleConfig field, tape+ledger init, 4 slot-diversity methods, `_try_slot_eviction` wiring, `record_quoted`/`record_accepted`), quote_app.py capacity probe + `_derived_capacity_tick`, tests (32), replay tool. Kill-anchor hunks (config flag, `_kill_anchor_readout`, limits.py/book_risk.py/pricing_pool.py/deploy_scale.py, its tests/tools) STAY OUT — grep-verified zero `kill_anchor` references in the ported tree |
| 3 | Flag-off byte-identity, randomized replays | 200 seeded-random books/candidates driven through the REAL `_try_slot_eviction` under main and under the port (common kwargs only): decisions, survivors, breaches, metrics **byte-identical, 200/200**. Only flags-off observable delta anywhere: the `open_quote_evicted` log line gains `key_kind=absolute_ev` (telemetry; `diversity` appeared 0 times) |
| 4 | Self-containment proof (fresh scratch worktree of main+commit `fef5268`, CLEAN tree) | import quote_app/lifecycle/eviction_value OK; full suite **3504 passed / 0 failed** (227s); vitals fast **8/8 GREEN** (20.9s checks). Also proven in the committing tree itself: suite 3504/0, vitals fast 8/8. NOTE: a gate run in any worktree needs the gitignored `config/prod-live-wc.local.yaml` copied in — without it the pydantic default `entity_loss_frac=""` crashes V1/V2 with `Fraction('')` (environmental, latent gate-harness sharp edge, worth a guard in `derive.knob`) |
| 5 | Counterfactual on the LATEST tape (8/1, skew fix armed) | RUN with the G1 repair (read-only, `--since 2026-07-30`): capacity 160 at median flow (withdraw form binds), FAIL-CLOSED at the worst burst window (withdraw headroom −2), diversity key would refuse 470/2,593 joined evictions, table DISCRIMINATING — § Counterfactual |
| 6 | Arming lines staged COMMENTED in the live local yaml with the derivation quoted | operator flips at a restart; both staged lines are `"shadow"` — still correct under the gate verdict (shadow is log-only) |

## The feature (recap of the 7/31 build report, unchanged semantics)

* **Derived open-quote capacity** (`risk.open_quote_capacity_derived`:
  off/shadow/on): `capacity = (OBSERVED tier write rate − kill-reserve rate −
  measured new-quote token rate) × TTL 20s / 4 tokens` — the write bucket's own
  bound on a standing book. Kalshi documents NO maker open-quote cap
  (docs/research/rfq_throughput/04-exchange-constraints.md re-verified);
  admitting more RESTING quotes admits no risk (confirm-path exact enforcement
  + fill-velocity governor + mass-acceptance caps own the risk; failure
  direction = reneges). Fail-closed to configured `max_open_quotes` while
  unmeasured / no headroom (a cap must be PROVEN to quote, 2026-07-23 rule).
* **Diversity-aware eviction key** (`risk.eviction_diversity_key`:
  off/shadow/on): slot axis ranks on `dEV × P(accept|size-bucket, in-process
  CP-bounded tape at ratified α=0.02) − dES99` (per-game CVaR share of the SAME
  book MC); candidate at CP-lower vs incumbent at CP-upper — the measurement's
  own confidence gap IS the churn hysteresis; separate anti-thrash ledger;
  FAILS CLOSED to today's absolute-EV key while the tape cannot discriminate.

## Adversarial gate (2026-08-01 morning, independent re-execution)

Every check below was RE-RUN by the gate session, not inherited from the port
fleet's claims.

| # | Gate item | Method | Verdict |
|---|-----------|--------|---------|
| 2 | Flag-off byte-identity | 200 seeded-random books/candidates through the REAL `_try_slot_eviction`, main tree vs port tree (seed 20260801, structlog muted so only canonical decision lines compare) | **200/200 byte-identical** (decisions, survivors, breaches, metrics) |
| 3a | CP bound derived, not typed | `clopper_pearson_lower/upper` vs `scipy.stats.beta.ppf` on 8 cases + 500 random (x,n): diffs ≤ 1e-15 in the operating regime; degenerate inputs fail closed (0.0 / 1.0); interval always brackets p̂. α reaches the key as `LifecycleConfig.kill_tail_prob` ← `portfolio_kill_tail_prob` (the ratified 0.02 policy anchor) — no new typed number on the live path | **VERIFIED** (one boundary note, G3 below) |
| 3b | Thrash killed | 4/4 adversarial thrash tests re-run (fat-evicts-small then twin-cannot-bounce through CP asymmetry alone; same-ticker ledger belt-and-braces; ALGEBRAIC no-2-cycle proof — a cycle needs lo·lo > up·up, impossible) + the 32 ported feature tests (thin fail-closed, hold-never-falls-through, shadow-changes-nothing, off-records-nothing) | **PASS** |
| 4 | Capacity armed | Write-budget arithmetic re-derived from documented endpoint costs (create 2 + delete 2; observed tier 300 tok/s write; supervisor reserve 200 tok/10 s = 20 tok/s) — formula reproduces 1,272 median / 1,198 worst. Exchange 429-storm: IMPOSSIBLE structurally (deletes are unwritable outside the metered `_withdraw_batch` gate, architecture-tested; creates are RFQ-demand-bounded) | **FAIL — G1**: the derivation's number cannot be maintained by the delete path (below) |
| 5 | Interference with 410e8fb / 3ed6dd9 / 95b9a40 | test_confirm_priority, test_confirm_anchor, test_quote_warmup_gate, test_startup_first_snapshot, test_skew_settled_resolution, test_skew, test_skew_pbook at the merged tree | **90/90 PASS** |
| 6 | No demotion/kill-anchor leak | `grep -r kill_anchored` over src/ + tests/ = **0 files**; `kill_anchor_shadow_golden.json` absent; the only `kill_anchor` strings are the pre-existing adaptive-caps feature | **CLEAN** |

### G1 — CONFIRMED arming blocker: the capacity derivation charges refresh to a bucket the delete path cannot spend

`derive_open_quote_capacity` models a standing book of N as re-postable at the
TIER's write rate: `(300 − 20 reserve − flow) × TTL 20 s / 4 tokens` ⇒
1,198–1,272 slots. But in the code THERE IS ONLY ONE DELETE CONDUIT: every
delete — TTL, reprice, leg-stale, RFQ-gone, EVICTION, cancel-all — goes
through `_withdraw_batch` → `_spend_withdraw_tokens` drawing on
`self._withdraw_budget` (lifecycle.py L8383), which quote_app sizes as the
clamped supervisor knob: **200 tokens / 10 s = 20 tok/s sustained, reserve=0**
(quote_app.py `_tier_clamped_write_budget`). Sustained delete throughput is
therefore 10 deletes/s for the WHOLE bot. A standing book's churn bound is

    N ≤ withdraw_rate × TTL / DELETE_COST = 20 × 20 / 2 = 200

— exactly today's hand-bumped 200 (which is presumably WHY 200 was the value
the manual bumps stalled at). The derived 1,198–1,272 exceeds the delete
path's structural throughput ~6x. Armed "on", a grown book's TTL/reprice/
eviction withdrawals defer at the FIFO token gate, the pending set backlogs,
and quotes REST STALE past their reprice point during exactly the bursts the
raise targets — the 07-26/07-27 maintenance-stall family (adverse-fill
exposure), even though the exchange never sees a 429. The module's own claim
("the derivation NEVER returns a capacity the bucket cannot actually
refresh") is false for the bucket that actually pays deletes. The derivation
also books the same 20 tok/s as a PURE kill reserve while routine deletes in
fact compete inside that bucket (reserve=0), contradicting "the cancel-all
path must never compete with resting maintenance".

**Repair (the mechanism, not the number):** derive the routine withdraw rate
from the OBSERVED tier — e.g. withdraw budget = tier write rate − kill
reserve (as a real `WriteBudget.reserve` carve-out) − measured CREATE flow —
then bound capacity by `min(tier derivation, withdraw_rate × TTL /
DELETE_COST)`. That dissolves the last hand-set write knob (200/10 s, a
North-Star residue) and makes the two derivations consistent; with a derived
~260 tok/s withdraw rate the original capacity arithmetic becomes roughly
right. New code ⇒ new gate cycle; NOT patched in this port.

### G1 REPAIR (executed 2026-08-01, same branch)

The mechanism, not the number: `derive_open_quote_capacity` now returns

    capacity = min(
        (tier_write_rate − reserve − measured_flow) × TTL / refresh_cost,   # (a) exchange tier (existing form)
        (withdraw_rate − measured_flow × delete_cost/refresh_cost)
            × TTL / delete_cost,                                            # (b) the bot's OWN delete budget (G1)
    )

* **Form (b)'s inputs are all things the code already reads**: `withdraw_rate`
  is the SAME tier-clamped supervisor bucket `quote_app._tier_clamped_write_budget`
  sizes into `QuoteLifecycle._withdraw_budget` (the one delete conduit G1
  identified) — `_derived_capacity_tick` now passes it as BOTH the tier form's
  reserve and the withdraw form's sustained rate; `delete_cost` is the
  documented DeleteQuote cost (2 tokens); the flow carve-out is the measured
  delete-share of new quoting (`measured_flow × delete_cost/refresh_cost`,
  drawn from the same bucket) — the safety margin comes from measured
  headroom, never a typed factor, and inherits the tier form's conservative
  double-count.
* **Today's yield**: tier form 1,391 / withdraw form **191** at the measured
  ~1.8 flow tok/s ⇒ capacity **191** — self-consistent with the hand-bumped
  200 the manual bumps stalled at (G1's prediction), honest instead of ~6×
  over the delete path. If the withdraw budget is ever raised the derivation
  scales by itself (at 260 tok/s the tier form takes over at 1,391) — the
  knob dissolves per NORTH STAR instead of lying.
* **Readout fields**: the `open_quote_capacity` log line now prints
  `tier_capacity` and `withdraw_capacity` separately (and on `no_headroom`,
  WHICH bucket had none), so the arming readout can see the binding bucket.
* **Regression tests** (feature suite now 37): G1 sustainability invariant
  (derived capacity × delete_cost / TTL + measured delete flow ≤
  withdraw_rate, swept over flow 0→20 tok/s), the min-form binds at 191
  today, the withdraw-raise auto-scaling case, withdraw-side `no_headroom`
  and `withdraw_rate_unusable` fail-closed arms.
* **G3 fixed opportunistically**: the CP-lower docstring's "cannot underflow"
  claim corrected to the honest boundary statement (n·p ≳ 745 ⇒ lower clips
  DOWN, upper converges to p̂ — both conservative, inversion impossible) +
  boundary regression test `test_underflow_regime_stays_conservative` at
  n=50k, p̂=2%. The module docstring's refresh claim now names BOTH budgets.
* The counterfactual replay tool now calls the LIVE
  `derive_open_quote_capacity` (rule 8) instead of an inline tier-only
  formula, printing both forms.

### G2 — secondary (arming-plan constraints, not code defects)

* **Adaptive-caps compose bug when BOTH armed:** `_derived_capacity_tick`
  composes via `replace(checker.limits, ...)`, but the adaptive-caps enforce
  path rebuilds from its CONSTRUCTION-TIME base (`derived_cap_engine`
  "max_open_quotes passes through from the base unchanged") — an enforce-mode
  adaptive refresh stomps the capacity swap back to the configured 200 until
  the next 60 s capacity window. Arm capacity "on" only with adaptive caps in
  off/shadow, or make the engines compose, before both are ever live.
* **Measured-flow double-count feedback:** the flow term includes the standing
  book's own refresh, so the armed steady-state capacity self-limits to
  roughly HALF the first-window log line (≈ (280−new)×2.5 ≈ 700 at new≈0).
  Conservative direction, but the 1,272 headline is a first-window number —
  the arming readout must expect the derived value to fall as the book grows.
  *Post-repair note:* under the min-form the WITHDRAW form (~191) is the
  binding bucket at today's budget and carries the SAME structure of
  feedback: `withdraw_capacity = (20 − flow/2) × 10`, and at a book of C
  refreshing every TTL the measured flow reads `(C/20 + new_rate) × 4`
  tok/s (the standing book's own refresh double-counted). Fixed point:
  `C = 200 − C − 20·new_rate ⇒ C ≈ 100 − 10·new_rate` — armed steady-state
  self-limits to ≈ **95–100 slots** at the live ~0.44 new-quotes/s, ~half
  the first-window 191 print. Strictly conservative (capacity falls, never
  rises, as the book grows), same direction as the tier-form half-print
  above. Both G2 constraints stay RECORDED IN THE ARMING PLAN, not code:
  capacity `"on"` must not be armed alongside adaptive-caps `enforce` until
  the two engines compose, and the first-window print will exceed the
  steady-state one.

### G3 — boundary note on the CP recurrence (safe direction, documented)

The docstring's "cannot underflow in the small-count regime" is not strictly
true at multi-day counter scales: once a bucket's `(1−p)^n` underflows inside
the bisection bracket (n·p ≳ 745, e.g. n≈50k at p̂≈2%), the LOWER bound clips
DOWN (candidate under-credited) and the upper converges to p̂ (incumbent
never credited below its point estimate — bisection's lo starts at x/n). Both
directions are conservative; no inversion is possible (upper ≥ p̂ ≥ lower by
construction). Counters are per-boot cumulative, so a single live day sits
inside the exact regime (verified vs scipy to 1e-15). Noted so a future
multi-day-persistence change re-checks this.

## Counterfactual on the LATEST tape (2026-08-01, skew settled-fact fix armed)

RUN with the G1 repair (read-only vs the live store + `live_20260801_0759.log`
tape; `tools.diagnostics.eviction_diversity_replay --since 2026-07-30`, now
calling the LIVE post-G1 derivation):

* **Window since 7/30:** 107,760 `quote_sent` / 33 confirm-path accepts.
  Measured acceptance table (CP bounds at the ratified α=0.02), per 1k:
  `<$5` 9/13,315 = 0.68 (0.297–1.315); `$5–15` 13/84,702 = 0.15
  (0.079–0.268); `$15–50` 4/5,155 = 0.78 (0.197–2.051); `$50–150`
  7/4,181 = **1.67** (0.642–3.540); `>$150` 0/407 (0–9.566).
  **Table DISCRIMINATING: True** (the $5–15 upper 0.268 sits below the
  $50–150 lower 0.642) — the post-fix tape reproduces the 7/31 finding that
  acceptance RISES with size on this flow.
* **Derived capacity on the 8/1 tape (min-form):** at the median sent-rate
  (120/min → 8.0 flow tok/s): **160** — tier form 1,360, WITHDRAW form 160
  binds. At the worst sent-rate window (608/min → 40.5 flow tok/s): the
  withdraw form is **−2 (no headroom — new-quote churn's delete share 20.25
  tok/s exceeds the whole 20 tok/s budget) ⇒ the derivation FAILS CLOSED to
  the configured 200** (tier form alone would have claimed 1,197). This is
  G1 made visible in the derivation itself: during exactly the burst windows
  the old form would have raised the cap ~6×, the repaired form reports the
  delete budget as the binding/again-exhausted resource.
* **Eviction replay:** 8,946 `open_quote_evicted` events on the tape, 2,593
  joined to store sizes; victims small (<$15) in 2,479/2,593; the diversity
  key (degraded dEV×P(accept) form, dES99=0 offline) would have refused
  **470/2,593**. Under the REPAIRED capacity (~160–191) today's eviction
  stream is NOT moot — the key genuinely governs at today's book sizes,
  unlike the pre-G1 ≥1,198 claim that mooted it.

## Verification tails (verbatim)

Gate session tails (pre-repair): feature tests `32 passed in 2.11s`; thrash
harness `4 passed`; interference `90 passed in 4.92s`; byte-identity
`BYTE-IDENTICAL: 200/200 trial lines match`.

G1-repair session tails (2026-08-01):

* feature tests: `37 passed in 2.71s`
* ruff (4 changed files): `All checks passed!`; mypy `eviction_value.py`:
  `Success: no issues found in 1 source file`
* vitals fast, in-tree, post-repair: `8/8 vital signs GREEN   (GATE PASS)
  total 23.9s`
* counterfactual: `derived capacity at median sent-rate (120/min -> 8.0
  tok/s): 160 [tier form 1360, withdraw form 160 binds]` / `derived capacity
  at worst sent-rate (608/min -> 40.5 tok/s): 200 [tier form 1197, withdraw
  form -2 binds]` (200 = fail-closed fallback, reason `no_headroom`) /
  `evictions the DIVERSITY key would have REFUSED (victim survives): 470 /
  2593` / `table discriminating: True`
* self-containment proof (fresh scratch worktree of main+branch): see the
  proof tail recorded below at push time.

## NEXT STEPS

* **DONE (this session, 2026-08-01):** G1 repaired in the port branch
  (§ G1 repair) — min-form capacity, G1 sustainability regression, G3
  docstring+boundary test, module docstring naming both budgets, replay tool
  on the live derivation; vitals fast in-tree, commit, fresh scratch
  worktree self-containment proof (import + full suite + vitals fast),
  counterfactual on the latest tape (§ Counterfactual), branch PUSHED.
  Merge to main happens at the operator restart boundary, per the standing
  rule.
* **Operator (decisions owed):**
  1. *Flip the staged shadow lines at the next restart* — the commented
     lines in `config/prod-live-wc.local.yaml` (both flags `"shadow"`)
     remain CORRECT and safe: shadow only logs. Capacity `"on"` is now
     G1-clean but should soak in shadow first; diversity `"on"` waits for a
     shadow tape showing the in-process table DISCRIMINATING (unchanged).
     Neither may be armed `"on"` alongside adaptive-caps `enforce` until the
     two engines compose (G2a).
  2. *WITHDRAW-BUDGET RAISE (staged, from the DOCUMENTED number — not
     changed here).* The documented write allowance for our tier
     (docs/api-notes/limits-account.md, digested from docs.kalshi.com
     `getting_started/rate_limits.md` + `GET /account/limits`): **Advanced =
     300 write tokens/s refill, bucket capacity 2 s = 600 tokens; cancels
     (DeleteQuote) cost 2 tokens** (batch-cancel worked example; runtime
     truth `GET /account/endpoint_costs`; the tier is OBSERVED live each
     boot via `ApiTierLimits`). The supervisor withdraw clamp of 200
     tok/10 s = 20 tok/s therefore uses **6.7% of the documented
     allowance** — the exchange permits 150 deletes/s where the bot grants
     itself 10. Derivation for the raise, every term documented or
     measured: `withdraw_rate = tier_write_refill (300 observed) −
     measured create flow (≈0.9 tok/s at the live ~0.44 sent/s × 2-token
     creates) − cancel-all burst reserve (the documented 600-token bucket
     capacity already covers a full-book cancel-all of ≤300 quotes in one
     2 s burst; a sustained reserve of ~40 tok/s keeps a 200-quote
     cancel-all under 10 s even with zero bucket credit) ⇒ ≈ 260 tok/s`
     (supervisor knob `2600 tokens / 10 s`). At 260 tok/s the withdraw form
     yields 2,591 and the TIER form becomes the binding bucket at ~1,391 —
     the originally headlined capacity, now honest. This is an operator
     knob change (the knob quote_app clamps and alarms on), so it is staged
     here as a decision, not applied; once raised, the derived capacity
     scales by itself — no code change.
* **Standing:** the reanchor worktree retires only after the repaired branch
  lands (its remaining unique content: parked V12/V13 flow vitals + withdrawn
  demotion history).
