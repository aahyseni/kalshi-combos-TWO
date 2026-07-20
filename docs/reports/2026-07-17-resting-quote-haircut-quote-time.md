# 2026-07-17 — Quote-time resting-quote haircut (weight + burst floor) + post-fill risk pull

**State: BUILT on tree over `15ebe40` (branch `risk-audit-overnight`); NOT committed (orchestrator
commits after verification); DEFAULT-DISARMED (weight 1.0 = live behaviour byte-identical) — the
operator arms `risk.resting_quote_weight: "0.40"` in the local YAML LATER.**

## Why (operator doctrine 2026-07-17)

Quote-time caps folded ALL resting (open) quotes at 100% mass-acceptance worst case, while the
confirm path already enforces the same budgets EXACTLY (P0-2 serial provisional reservations →
candidate MC gate → confirm; freshness gate; Problem-A waiver). The 100% fold double-counted that
defense and was the #1 measured flow killer: on the 7/16→7/17 tape window, **599,310 of 1,137,577
no-quotes (52.7%) carried `skip_game_loss_cap`**, with a median breach ratio of only **1.03×** the
budget (p75 = 1.07×) — the wall was almost entirely marginal resting-quote pile-up, not committed
risk. "Avoid double counting; we already have last look and MC; blocking quotes is technically -EV."

## The composition (per axis, per bucket)

```
value = min( full,  max( ceil(w·full + (1−w)·base),  base + topK ) )

base = fold of committed positions (+ candidates) ONLY        (never haircut)
full = fold with ALL resting quotes at 100%                   (today's value)
topK = comonotone sum of the K largest per-quote worst-case
       contributions on that axis/bucket                      (the BURST FLOOR)
```

- Monotone in the resting set AND in the candidate set for w∈[0,1] (min/max of monotone terms) —
  the F1 pre-gate lemma survives.
- Floor honoured: the true 100% fold of the K largest is ≤ min(full-contribution, topK)
  (fold monotonicity + branch-max subadditivity), and value ≥ that min.
- w=1 reduces exactly to `full`; weight None/≥1 routes through the pre-existing fold verbatim
  (bit-identical, including float addition order).
- Axes covered: per-game loss (mutex fold), per-game directional (mutex fold), per-game
  gross-settlement notional, whole-book gross premium, per-market and per-game delta magnitudes;
  the slate cap and utilization backstop inherit it (they roll up the haircut snapshot).

## What was built (per spec point)

| # | Spec point | Where |
|---|-----------|-------|
| 1 | Weighted resting fold + burst floor in the ONE mass-acceptance snapshot | `risk/exposure.py` — design note + helpers `_haircut_compose_cc/_float`, `_topk_sum_*`, `_QuoteContrib` (:762-816); `snapshot(resting_quote_weight, resting_floor_count)` (:953-980); contribution collection (:1064-1110); 100% fold replay = today's ops in today's order (:1112-1147); haircut composition (:1149-1211); mutex-fold composition for loss+directional (:1216-1247). New `RiskLimits.resting_quote_weight` (Fraction, default 1) + `resting_floor_count` (default 3) (`risk/limits.py:180-181`); seam = explicit `check(..., apply_resting_haircut=False)` (`risk/limits.py:424`, snapshot pass-through :530-533) |
| 2 | Confirm-time untouched + regression test | Confirm sites (reservation `try_reserve`, `_last_look_inputs`, maintenance tick, waiver retry) never pass the flag — `try_reserve` has no such parameter at all. Test `TestConfirmTimePinnedAtFullFold` (tests/test_resting_haircut.py): armed quote-time check demonstrably differs on the same book while the unarmed check and `try_reserve` decisions are bit-identical (reason, detail, shadow, game) between a weight-0.4 and a weight-1 checker |
| 3 | Event-driven post-fill pull | `rfq/lifecycle.py` — `EVICTABLE_ON_FILL_BREACHES` (:128), `_schedule_risk_evict_on_fill` (:2419, armed only when weight < 1, single-flight, fire-and-forget task), `_risk_evict_after_fill` (:2440, analytic-only `limits.check` per iteration, beats heartbeat, deletes ONE quote per iteration via the existing `_delete_quote` path with new `ReasonCode.DELETE_RISK_EVICTED_ON_FILL`, bounded by open-quote count, errors fail SAFE), `_pick_eviction_victim` (:2499, same-game-as-fill first then largest worst-case loss; accepted quotes never yanked). Hooks: confirm-success commit (:2352) + `on_quote_executed` booking (:2564, covers timeout/recovery paths). Metrics: `risk_evict.on_fill`, `risk_evict.pass_error`, `quote.deleted.delete_risk_evicted_on_fill` |
| 4 | E2 property rewrite | `tests/test_resting_haircut.py::TestE2SerialConfirmBudget` — hypothesis (300 examples): ANY resting set admitted under the haircut, ANY accept subset/order through the REAL `RiskReservationService` serial confirm path (100% fold) ⇒ committed consumption never exceeds game-loss / directional / gross / utilization / hard per-game budgets. Plus a deterministic non-vacuous demo: 4 quotes admitted at weight 0.1 (the OLD dominance invariant violated BY DESIGN), confirm path commits 1 and declines the excess. Monotonicity + burst-floor + w=1-parity kept as live-port property tests (`TestHaircutProperties`); the pre-existing confirm-semantics dominance test in `test_exposure.py` still passes untouched |
| 5 | Prototype first (rule 8) | `tools/proto_resting_haircut.py` — composition built from live primitives only; numbers below; part D1 pins port parity |
| 6 | F1 pre-gate consistency | `_pre_pricing_breaches` passes `apply_resting_haircut=True` (`rfq/lifecycle.py:1914`) — same semantics as the full quote-time check (:2023). Lemma re-verified ARMED (prototype part D2) |

## Prototype numbers (tools/proto_resting_haircut.py)

| Part | What | Result |
|------|------|--------|
| A | Monotonicity fuzz (weights 0.1/0.4/0.75, floors 1-3, every axis/bucket) | **3,000 cases, 0 violations** |
| B | Weight-1 parity vs live mass snapshot | **1,000 cases exact** (int axes ==; deltas <1e-6; directional ±2cc int-truncation slack) |
| C | Burst floor vs the LIVE 100% fold of only the K largest quotes | **1,000 cases, 0 violations** |
| D1 | Port parity: `snapshot(resting_quote_weight=w)` == prototype composition | **1,000 cases** (loss/notional/gross exact; deltas <1e-6; directional ±2cc) |
| D2 | F1 pre-gate lemma with the haircut ARMED end-to-end through the live `LimitChecker` | **3,000 cases, 2,729 gate firings, 9,898 reason persistences, 0 violations** |
| E | Tape replay 2026-07-16T17:30 → tape end (READ-ONLY, mode=ro) | below |

**Tape replay (flow-unlock estimate at weight 0.40).** Window decisions: 1,137,577 no_quote /
49,402 quote_sent. `skip_game_loss_cap` declines: 599,310 (52.7% of no-quotes). Parsed
loss/threshold ratios: p25 1.01, p50 1.03, p75 1.07, p95 2.41, max 88.4. **Estimated unlock:
570,311 / 599,310 = 95.2%** of game-loss declines would clear at weight 0.40 under the ALL-RESTING
assumption (base≈0 — fills were rare in the window — and floor non-binding; the tape does not
record the committed/resting decomposition, so this is the optimistic bound; the top-3 burst floor
and committed base can only lower it).

## Config keys added (safe defaults; NO existing cap values changed)

| Key | Default | Armed value (operator, local YAML, later) |
|-----|---------|------------------------------------------|
| `risk.resting_quote_weight` (decimal string → exact Fraction, validated (0,1]) | `"1.0"` = today byte-identical | `"0.40"` |
| `risk.resting_floor_count` (int ≥ 1) | `3` | `3` |

Arming the weight below 1 also arms the post-fill pull (no separate flag — the haircut is what
opens the gap the pull closes).

## Verification

- Suite: **2283 passed / 0 failed** (baseline 2263 + 20 new), 3 deselected — fresh full run on the final tree.
- ruff `src tests`: 13 (baseline 13, zero new). mypy: 6 (baseline 6, zero new).
- New tests: 20 in `tests/test_resting_haircut.py` (composition hand-computed values, w=1/None
  byte-identity, hypothesis monotonicity + floor, confirm-time bit-identity, E2 rewrite property +
  deterministic demo, config wiring/validation, 6 post-fill-pull tests incl. fail-safe error path
  and disarmed-never-schedules).

## Deliberately NOT changed

- Confirm path (reservation, last-look advisory check, candidate MC, waiver, freshness): 100% fold.
- Maintenance-tick check: unarmed (halt escalation reads none of the resting-fold caps; conservative).
- `_quoting_policy` (skew/widen — dark) and the risk-audit telemetry snapshot: full fold (they are
  policies/telemetry, not caps; revisit if the widen policy is ever armed).
- `max_open_quotes` count cap: a count, not a mass fold — no haircut.
- Post-fill pull scope: only the two per-game caps that CARRY their game key on the Breach
  (`skip_game_loss_cap`, `skip_directional_cap`) — details are never parsed; global caps keep the
  confirm-exactness + TTL/reprice sweeps as backstop.

## NEXT STEPS

- **Orchestrator**: verify + commit this tree (suite/ruff/mypy at the numbers above); push.
- **Operator**: arm `resting_quote_weight: "0.40"` in `config/prod-live-wc.local.yaml` at the next
  relaunch decision point (NOT touched by this build); watch `risk_evict.on_fill`,
  `quote.deleted.delete_risk_evicted_on_fill`, and the `skip_game_loss_cap` share of no-quotes vs
  the 52.7% baseline; confirm-side declines (`decline_risk_limit`) are the guardrail that must
  absorb what the quote-time wall no longer blocks.
- **Owed follow-ups**: (a) if the widen policy is ever armed, decide whether its snapshot adopts
  the haircut; (b) game-day watch of the burst floor (does floor=3 bind on champion-final flow?);
  (c) fold the unlock measurement into the game-cap decision after the 7/18-19 games.
