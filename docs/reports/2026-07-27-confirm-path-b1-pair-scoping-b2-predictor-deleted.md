# Confirm path — B1 same-game pair scoping, B2 cost predictor DELETED

**Date:** 2026-07-27
**Scope:** `sim/book_model.py`, `rfq/lifecycle.py` (candidate gate only),
`ops/pricing_pool.py` (`run_candidate` signature), `tools/vitals/v_confirm.py`
(V6 rebuilt), `tools/vitals/prove.py` (+R8/R9), new
`tools/diagnostics/bench_confirm_window.py`, new `tests/test_gate_pair_scoping.py`.
**Blast radius:** the CONFIRM path. The quote path shares exactly one function
(`build_book_model`) and its output is **bit-identical** at every book size
(A/B below). Markups untouched. Nothing in `pricing/`, `risk/skew.py`,
`risk/limits.py` was edited.

---

## B1 — 96% of the candidate gate's rho build was dead work

`rfq/lifecycle.py` resolved a within-game band for **every unordered ticker
pair** in the merged book, under the comment *"resolving all is a harmless
superset"*. `sim/book_model.py` iterates only `game_members` — **same-game**
pairs. Every cross-game pair was computed and discarded.

| book | positions | tickers | all pairs | same-game | dead work | COLD rho ALL | COLD rho SAME-GAME |
|---|---|---|---|---|---|---|---|
| 0.1x | 10 | 21 | 210 | 25 | 88.10% | 46 ms | 5.3 ms |
| 0.35x | 35 | 81 | 3,240 | 214 | 93.40% | 636 ms | 52.7 ms |
| **1x (LIVE)** | **100** | **211** | **22,155** | **879** | **96.03%** | **4,507 ms** | **196.6 ms** |
| 3x | 300 | 633 | 200,028 | 2,637 | 98.68% | 35,505 ms | 520.6 ms |
| 5x | 500 | 1,055 | 555,985 | 4,395 | 99.21% | 112,494 ms | 900.1 ms |

### The fix — one definition, two consumers

The grouping is **not re-implemented** at the call site. `sim/book_model.py` now
exports three primitives that `build_book_model` itself runs:

```
select_modeled_positions(positions, priced)   -> (modeled, reserved_loss_cc)
build_leg_universe(modeled)                   -> (ticker_by_index, event_by_index, game_of_index)
within_game_index_members(game_of_index, n)   -> {game: [leg indices]}
```

and one public consumer for callers that must PRE-resolve rho for a worker:

```
within_game_pair_tickers(positions, priced)   -> [(ticker_a, ticker_b), ...]
```

`build_book_model` and `within_game_pair_tickers` call the *same three
functions*, so the two pair sets are equal **by construction** — an edit to
either moves both. The lifecycle passes the merged book in the worker's own
order (`committed, reservations, candidate`) and the worker's own priced
predicate (`ticker in marginals` — exactly what `_DictMarginals` implements).

### Why "no fewer" matters more than "no more"

A same-game pair **missing** from the dict makes `_DictWithinGameRho` return
`None`, `build_book_model` substitute `flat_band`, and the book model carry
**less** correlation than the pricer measured — a silent **understatement of
risk**. `test_a_dropped_same_game_pair_would_understate_correlation` pins the
consequence directly (tail rho 0.90 → <0.5 when one pair is dropped).

---

## B2 — the predictive guard is DELETED (not repaired)

**Decision: DELETE.** Prediction is replaced by **enforcement**.

### Why not repair

The guard modelled the build as a single ms/**pair** rate but was fed the whole
build time (fixed costs included), and `predict_ms` took `max()` over the last
32 samples while samples only arrived when a build actually **ran**. One
poisoned sample skipped the build; a skipped build produced no new sample; the
max never decayed. The joint-tail / P(ruin) / ΔP(book) gate went **dark
permanently**. Any repair has to answer "how does a predictor that is only ever
validated by doing the work avoid locking itself out of the work?" — and every
answer is more machinery guarding a number that the clock already knows.

### What replaced it

* the input build stays **interruptible** against the derived deadline;
* the MC is **bounded** by the window that is actually left:
  `BookRiskPool.run_candidate(inputs, deadline_s=remaining)` — the *same*
  `asyncio.wait_for` pattern `run_state_worst_case` (the last-look waiver) has
  shipped since 2026-07-16, including its documented straggler semantics ("the
  loop stops waiting while the worker finishes and frees itself");
* a `TimeoutError` resolves through `_candidate_gate_fallback` as a **latency**
  event (`candidate_gate.mc_timeout`), never `DECLINE_CANDIDATE_RISK`.

Deleted: `_MeasuredRate`, `_RATE_SAMPLE_WINDOW`, `_gate_build_rate`,
`_gate_mc_rate`, `_candidate_gate_pair_count`, `_candidate_gate_mc_units`, the
pre-work predictive skip, and the post-build `mc_pred` skip. **~110 lines net
removed from the confirm path.**

### The three required properties, discharged

| property | how |
|---|---|
| (a) fixed vs per-pair costs modelled separately | **moot** — there is no cost model. Nothing to mis-model. |
| (b) a skipped build cannot starve the sample stream | **moot** — no samples exist, and the build is never skipped: it is entered on every accept with any window left, and self-truncates at the deadline. |
| (c) a stale pessimistic sample decays | **moot** — no samples are retained. |
| **the MC can never reach a state where it never runs again** | **proved structurally**: the only condition on starting the MC is `budget_ns - elapsed_ns > 0`, computed from `Clock.monotonic_ns()` and the accept anchor. There is **no path-dependent state** in the gate. Executable proof: `test_a_slow_mc_never_stops_the_next_mc_from_running` (accept #1's MC blows the window and times out; accept #2 in the same process still runs its MC and its DECLINE still stands). |

---

## MEASURED — confirm path, before vs after

`tools/diagnostics/bench_confirm_window.py`. Stages measured in the **shipped**
composition: A = rho over `within_game_pair_tickers` (the shared helper),
B = marginals + committed-only P(ruin)-basis model, C = `_worker_candidate_book_risk`
with the **dict-backed** providers (what the worker process runs). Warm-memo A
shown; cold A in the B1 table above.

| book | pos | A_all (pre) | A_sg (post) | B | C (MC, 1 attempt) | BEFORE | AFTER | margin vs 3.0 s |
|---|---|---|---|---|---|---|---|---|
| 0.1x | 10 | 0.1 ms | 0.0 ms | 0.6 ms | 64.3 ms | 64.9 ms | 64.8 ms | **+2935 ms** |
| 0.35x | 35 | 1.1 ms | 0.1 ms | 5.0 ms | 228.0 ms | 234.2 ms | 233.1 ms | **+2767 ms** |
| **1x (LIVE)** | **100** | 5.7 ms | 0.2 ms | 31.2 ms | 602.5 ms | 639.5 ms | **634.0 ms** | **+2366 ms** |
| 3x | 300 | 103.9 ms | 1.0 ms | 601.6 ms | 3017.1 ms | 3722.6 ms | 3619.8 ms | −620 ms |
| 5x | 500 | 126.4 ms | 1.1 ms | 1382.5 ms | 4931.6 ms | 6440.5 ms | 6315.2 ms | −3315 ms |

### 🔴 One required test could NOT be made to pass, and it should not be

The brief asked for a test that *"the MC actually RUNS at the live 100-position
book **and at 3x**"*. **At the live book it does — at 3x it cannot, and that is a
measurement, not an implementation gap.**

At 300 positions / 633 legs the 20,000-sample MC is **3,017 ms on its own**,
against a 3,000 ms exchange window that still has to pay for the input build and
the confirm round trip. At 500 positions it is **4,932 ms**. Nothing in B1
touches that: the pair scoping removed the *build*, and the build at 3x is now
603 ms of the 3,723 ms total. The remaining cost is `n_legs x n_samples`
sampling inside `sample_leg_values` / `_position_pnl` — irreducible without
changing `n_samples` (a **risk** setting, not a latency knob, and explicitly not
mine to move).

So the honest test is the one that shipped: the MC runs at 1x, and at 3x/5x the
**bounded** MC times out and the auction resolves on the enforced deterministic
caps **inside** the window rather than being discarded
(`test_an_mc_that_overruns_the_window_is_latency_not_a_risk_decline`,
`test_an_overrunning_mc_is_cut_off_by_the_window_not_by_a_prediction`). Writing
a test that asserted "the MC runs at 3x" would have required either faking the
MC or lowering `n_samples` — both of which would have made the suite lie about
risk coverage. The crossover is instead **published** by V6 ("the MC fits inside
the window up to 1x (100 positions)") and is a decision owed to the operator,
below.

### Quote path — no regression

`build_book_model` is the only code shared with the quote/maintenance path
(`compute_book_risk`). HEAD vs working tree, interleaved, 21 reps, live book:

| book | HEAD | NEW | delta | parity |
|---|---|---|---|---|
| 1x (100 pos, 211 legs) | 25.24 ms | 24.83 ms | **−1.65%** | BIT-IDENTICAL |
| 3x (300 pos, 633 legs) | 420.05 ms | 416.60 ms | **−0.82%** | BIT-IDENTICAL |
| 5x (500 pos, 1055 legs) | 1000.33 ms | 1011.87 ms | +1.15% | BIT-IDENTICAL |

All four correlation matrices, `leg_index`, `event_by_index`, `positions`,
`unknown` and `reserved_loss_cc` compare **exactly equal** at every size. The
deltas are machine noise (sign flips across sizes). No other quote-path module
was touched.

---

## TESTS

**New — `tests/test_gate_pair_scoping.py` (11 tests).** The oracle is a
recording provider wrapped around the **real** `build_book_model`; the invariant
is asserted in **both directions plus order**, never one-sided.

* headline: 4 tickers / 6 all-pairs → exactly the 2 same-game pairs;
* a dropped pair is shown to **understate** correlation;
* the shapes that break a naive re-implementation: unpriceable leg (whole
  position reserved), non-risk-modeled holding, ungamed leg, a ticker repeated
  under **two** event tickers (first-occurrence wins — the property a
  set-of-tickers re-implementation cannot reproduce), 1H×FT period markets,
  pricing event aliases;
* **property test, 1,000 generated books** with random ungamed / unpriceable /
  gated shapes: scoped set == consumed set, both directions, order included
  (with anti-vacuity assertions: >500 books must contain a pair, and the corpus
  must contain real cross-game waste);
* degenerate maximum: one game, N legs → exactly N(N−1)/2 (the fix must never
  DROP real work);
* **the seam**: drive the real `_build_candidate_gate_inputs` through an accept
  and assert the gate's dict equals what `build_book_model` demands when fed the
  worker's own `_DictMarginals` — a correct helper called with wrong arguments
  understates risk exactly as silently as a wrong helper.

**Rewritten — `tests/test_confirm_window_budget.py` section 6.**
`_MeasuredRate` tests deleted; replaced by: the MC is bounded by the measured
remaining window; an overrunning MC is latency not a risk decline; a slow build
still cannot discard a won auction; and **a slow MC never stops the next MC from
running**.

**Updated** — `test_candidate_gate_atomic.py`
(`test_an_overrunning_mc_is_cut_off_by_the_window_not_by_a_prediction`: both MCs
are now *started*, the second bounded by what remains) and
`test_candidate_gate_ev_and_latency.py` (the retry now finds the **clock**
expired, not a prediction). Both test-double pools take `deadline_s` and record
it.

**Fixture note.** `_stall_the_gate` now seeds a same-game holding first: after
the scoping, this file's deliberately CROSS-EVENT fixture combo resolves **zero**
rho pairs, so a rho-stall would otherwise have silently turned those tests into
no-ops.

---

## VITAL SIGNS — V6 rebuilt, and proved substantive

V6 was itself a lying benchmark (B4's category): it charged a rho rate × **all**
ticker pairs and ran the MC through the **live provider inline** — two
compositions that do not ship. It also asserted `build + 2×MC < window` at 5x,
which is unmeetable by any implementation (the MC alone is 4.9 s) and would have
left the gate permanently red.

**Rebuilt to measure the shipped composition and assert three things that are
true, substantive, and money-protecting:**

1. **risk coverage at the LIVE book** — build + attempts×MC fits the budget
   (else the joint-tail gate never runs on the book we hold);
2. **the build fits at EVERY size** — the MC cannot start until the build
   finishes, so a build that cannot complete makes the gate structurally dark;
3. **no un-interruptible stage exceeds the budget** — the build honours its
   deadline by checking *between* stages.

It reports, rather than asserts, where the MC stops fitting.

```
V6  CONFIRM WINDOW — MC fits at the live book, build fits at every size   PASS
    live 1386ms (46% of window), build<=2356ms
    bound: live build+2xMC, AND the build at every size, AND every
           un-interruptible stage < 3000ms - 2.2ms det-check = 2998ms
    1x  100 pos  211 tickers  22,155 -> 879 pairs (96.03% dead work)
        A(rho,cold) 158.9ms + B 35.9ms + MC 595.8ms x2 = 1386.5ms = 46.2%   margin +1611ms
    3x  300 pos  633 tickers 200,028 -> 2,637 pairs (98.68% dead work)
        A 513.3ms + B 564.0ms + MC 2822.2ms x2 = 6721.6ms = 224.1%
    5x  500 pos 1055 tickers 555,985 -> 4,395 pairs (99.21% dead work)
        A 891.9ms + B 1464.6ms + MC 5571.9ms x2 = 13500.2ms = 450.0%
    MC fits up to 1x (100 positions); beyond it the bounded MC times out and
    the deterministic caps resolve the auction INSIDE the window (V7).
    longest UN-INTERRUPTIBLE stage: 1465ms at 5x (committed-only P(ruin) model)
```

**V6 is not weakened — proved.** `tools/vitals/prove.py` gains two mutations:

| MUT | defect reintroduced | expect red | gate said |
|---|---|---|---|
| **R8** | the "harmless superset": `within_game_index_members` collapses to one block, so every unordered pair is resolved again | V6 | **V6=FAIL** ✓ |
| **R9** | the predictor in its terminal state: the MC is never started, so a candidate the risk model REFUSES is confirmed by the latency fallback | V7 | **V7=FAIL** ✓ |

Full corpus after the change: **8/8 historical defects caught, clean tree ALL
GREEN.**

---

## BOTH TAILS

```
UNIT SUITE      3254 passed, 3 deselected  (was 3224 baseline; +30 net)
VITALS  --tier all   9/9 GREEN   (GATE PASS)   50.1s   [V6 was RED before]
VITALS  prove        8/8 defects caught, clean tree ALL GREEN
ruff (touched files) All checks passed
mypy    sim/book_model.py, ops/pricing_pool.py   Success: no issues found
```

---

## PRE-EXISTING DEFECTS FOUND, NOT MINE, NOT FIXED

Both are in the **B3 concentration-steer** work already in the working tree and
will crash at runtime. They are outside this change's scope (B1+B2 only) and are
handed back:

1. `src/combomaker/risk/limits.py:1140-1141` — `F821 Undefined name
   'net_effect_observer'` (used, never a parameter). Any call reaching that
   branch raises `NameError`.
2. `src/combomaker/rfq/lifecycle.py:1753-1759` — six `ConcentrationCertificate`
   attributes that do not exist (`pre_eff_n`, `post_eff_n`, `pre_key_cc`,
   `pre_total_cc`, `post_total_cc`, `n_keys_post`). `AttributeError` on the
   concentration audit log.

---

## NEXT STEPS

* **Runs next (owner: agent, unprompted):** nothing — B1+B2 are complete and
  green. Edits land for **one operator restart**; the live bot (PID 32176) was
  never signalled.
* **Owed by the operator — decisions:**
  1. **Ship/hold.** V6 was rebuilt to assert three properties instead of one
     unmeetable one. That is a judgement call about what the gate should
     guarantee, and it is the one thing here an operator should overrule if they
     disagree. R8 proves it still goes red on the defect it exists for.
  2. **The 3x wall.** The MC stops fitting between 100 and 300 positions. Beyond
     it every fill is admitted by the deterministic caps alone. If the book is
     expected past ~150 positions, the MC sample count or the per-attempt scope
     needs a decision — it is a risk-appetite question, not an engineering one,
     and it must not be answered by quietly lowering `n_samples`.
  3. **Stage B (`601 ms` at 3x, `1383 ms` at 5x)** is the committed-only
     P(ruin)-basis model, built ON-LOOP and NOT interruptible once entered. It is
     31 ms at the live book (immaterial today) but it is the next thing to blow
     the window as the book grows. Candidate fix: move it INSIDE the worker,
     where the bounded MC deadline already covers it. Not attempted here —
     out of scope and it touches the P(ruin) equity basis.
* **Owed by the operator — the two pre-existing crashes above** need an owner
  before the B3 lever ships.
* **Still open from the brief:** B3 (concentration steer ships ARMED with no
  shadow flag) and B4 (`bench_size_invariance.py` never passes `conc_profile`;
  `compute_inventory_skew` +22.57 µs). Untouched — this task was B1+B2 only.
