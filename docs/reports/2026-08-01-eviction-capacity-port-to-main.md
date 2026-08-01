# 2026-08-01 — Eviction/capacity feature PORTED to main, self-contained, flag-off dark

**Branch:** `eviction-capacity-port` (off main `022e47f`). **Status: PORTED,
defaults OFF = today's behaviour. ADVERSARIAL GATE (independent re-execution,
2026-08-01 morning): NO-SHIP — one CONFIRMED arming blocker found in the
capacity derivation (§ Adversarial gate, finding G1). The flags-off code is
byte-identical (re-proven) and the diversity key survived every attack, but
the commit enshrines a derivation whose headline number (1,198–1,272 slots)
the delete path cannot physically sustain, so it does not push until G1 is
repaired.**

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
| 5 | Counterfactual on the LATEST tape (8/1, skew fix armed) | NOT RUN — moot while G1 blocks the push; owed with the G1 repair |
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

NOT RUN by the gate session (moot while G1 blocks the push; the counterfactual
reads the live store + tape and was deferred to keep I/O off the live bot
during the vitals run). Owed with the G1 repair cycle.

## Verification tails (verbatim)

See the gate session's structured summary; key tails: feature tests
`32 passed in 2.11s`; thrash harness `4 passed`; interference `90 passed in
4.92s`; byte-identity `BYTE-IDENTICAL: 200/200 trial lines match`.

## NEXT STEPS

* **Agent (next session, owns):** repair G1 IN THE PORT BRANCH before any
  push — derive the routine withdraw budget from the observed tier (kill
  reserve as a `WriteBudget.reserve` carve-out), bound capacity by
  `min(tier derivation, withdraw_rate × TTL / DELETE_COST)`, add the
  regression test (a derived capacity must be sustainable by the delete
  path's own budget), then re-run the FULL adversarial gate: vitals fast 8/8
  in-tree, commit, fresh scratch worktree of main+commit (import quote_app +
  full suite + vitals fast), byte-identity replay, counterfactual on the
  latest tape, THEN push.
* **Operator (decision owed):** none until G1 is repaired. The staged
  commented lines in `config/prod-live-wc.local.yaml` (both flags `"shadow"`)
  remain CORRECT and safe to flip at a restart even before the repair —
  shadow only logs. Capacity `"on"` is BLOCKED on G1; diversity `"on"` waits
  for a shadow tape showing the in-process table DISCRIMINATING (unchanged)
  and does not depend on G1.
* **Standing:** the reanchor worktree retires only after the repaired branch
  lands (its remaining unique content: parked V12/V13 flow vitals + withdrawn
  demotion history).
