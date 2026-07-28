# 2026-07-28 — SLATE AGGREGATION BY PARTITION: verified, fail-closed, ARMABLE

> **"Risk engine = protection not limitation. Those risk caps should be
> protecting us as intended, right now they're limiting us."** — operator
>
> **"We need to fix the slate count it shouldn't be over counting, with 12 games
> in a day we should always be filling the $1500 cap we have."** — operator,
> ratifying this repair 2026-07-28

Scope of this report: **the SLATE axis only.** The entity axis was rebuilt in
parallel by a concurrent workstream in the same tree; §6 states exactly where
that build stands and where it diverges from the brief, with numbers, because a
reader of this file must not assume it was covered here.

---

## 1. THE DEFECT, RE-MEASURED ON TODAY'S LIVE BOOK

`LimitChecker._slate_rollup` sums `worst_case_loss_by_game_cc`. An AND-BOUND
ticket spanning **G** games contributes its **FULL** `max_loss_cc` **G times** to
one slate's number. The live book is **88.4% multi-game**, so this is not an edge
case — it is the book.

Measured this session with `tools/diagnostics/slate_partition_live_book.py`
(drives the live `_slate_rollup` / `_slate_partition`; store read-only):

```
LIVE BOOK  positions=121  real premium at risk=$1,498.27  multi-game tickets=107 (88.4%)
WALL  slate_loss_frac=13/20 of bankroll $2,302.45  =  $1,496.60

slate           NAIVE sum/game  % of wall    ONCE-COUNTED  % of wall   ratio  headroom naive  headroom fixed
2026-07-26    $      1,193.84     79.8%$        631.57     42.2%   1.89x$        302.75$        865.03
2026-07-27    $        347.99     23.3%$        167.57     11.2%   2.08x$      1,148.61$      1,329.02
2026-07-28    $      1,001.84     66.9%$        618.20     41.3%   1.62x$        494.76$        878.39
2026-07-29    $        259.54     17.3%$         70.00      4.7%   3.71x$      1,237.06$      1,426.59
TOTAL         $      2,803.21          $      1,487.35             1.88x
```

Two facts to hold onto:

* The naive number for **four** slates totals **$2,803.21** against **$1,498.27**
  of premium that actually exists — **1.88x**. The corrected total,
  **$1,487.35**, lands within **$10.92** of the real premium, and it is *below*
  it because the within-game mutex fold nets genuinely exclusive arms.
* On **today's** slate the book alone consumed **66.9%** of the wall before any
  candidate was considered. Headroom **$494.76 → $878.39 (+77.5%)**.

Tape replay (`tools/diagnostics/replay_slate_partition.py`, 400,000 decisions,
committed-book + candidate basis, both sides on the same basis so the ratio is
exact):

```
WINDOW decisions=400,000  sent=9,856  send_rate=2.464%
SLATE  refusals=120,577  ADMITTED by the partition=108,227 (89.8%)
  premium carried by slate-refused decisions=$46,783,302.02   released=$12,114,949.39
  per-refusal mean: naive sum-per-game $2,406.59  vs  once-counted $1,008.73   ratio=2.39x
PROJECTED (SLATE lever alone) newly-clean decisions=29,119  send rate 9.744%  (today 2.464%)
```

**89.8%** of slate refusals disappear. Projected send rate on the slate lever
alone **2.464% → 9.744%** — an **UPPER BOUND**: a decision that stops being
refused here still has to clear the stages that never ran (candidate gate, EV,
write budget).

---

## 2. THE REPAIR IS THE MEASURE, NOT THE NUMBER

`slate_loss_frac` stays **0.65**. Nothing about the operator's ratified risk
appetite moves. What moves is that the slate binds on the **ENUMERATED JOINT
WORST CASE with every loss event counted EXACTLY ONCE**:

* every unit lands in **exactly one** bucket — its single game when all its legs
  live in one `game_key` game, else the **comonotone residual at full loss**;
* each game bucket folds through the **existing** Stage-B
  `_mutex_game_worst_cc` (single explicit-ME-event max-over-branches, fail-closed
  to comonotone on 0 or ≥2 ME events). **No second concept was invented** — this
  is the same machinery `mutex_aware_det_max_cc` and the per-game cap already use;
* the total is **clamped** into `[largest single loss, once-counted comonotone
  sum]`. The once-counted sum **is** the all-lose bound, so anything above it is
  an artefact, not a worst case;
* a per-game waiver certificate can only **tighten** a bucket (`min`), never
  raise one;
* **uncertainty always fails toward the LARGER worst case** — multi-game unit,
  ungamed unit, UNKNOWN/absent ME metadata, an exception in the ME lookup, ≥2 ME
  events: all comonotone.

A ticket spanning **two slates** is charged **in full to BOTH** — each slate cap
is a separate constraint and the ticket can lose within either window — but
**exactly once within each**. Only the within-slate duplication was the bug.

---

## 3. THE LATENT FAIL-OPEN THE PRIOR GATE FOUND — CLOSED, WITH THE EXPLOIT EXECUTED

`_slate_partition` builds its answer from `snapshot.loss_units`. An **empty
census folds to ZERO for every slate, and zero is the PERMISSIVE answer on a loss
cap.** A snapshot built with `want_loss_units=False` therefore made **every slate
breach disappear** — armed, that is a cap that refuses nothing.

Before this session the only thing preventing it was that one caller happened to
derive `want_loss_units` from the same two booleans that arm the partition — **no
assertion, and the permissive answer as the default.** That is precisely the
species hard rule 6 and quiet-failure defense #2 exist for.

Three guards, all with executed PoCs:

| # | hole | shut by |
|---|------|---------|
| 1 | **census never taken** — `loss_units == ()` is indistinguishable from "the book is empty", and the empty reading admits everything | new `ExposureSnapshot.loss_units_built`, set from `want_loss_units`. Not built ⇒ `_slate_partition` returns `{}` ⇒ the **naive** number enforces |
| 2 | **census taken but sees nothing in a breaching slate** — the two views disagree about the book | the slate is omitted ⇒ the naive number enforces |
| 3 | **corrected number ABOVE the naive one** — impossible by construction, so it means the census is not the book the roll-up measured | clamped to the naive term via the new `naive_by_slate` argument |

…plus the read-out itself: the observer used to report `partitioned.get(s, 0)`,
so a slate the partition **refused to correct** printed `would_admit=True` on the
very line the operator arms from. It now reports the number that would actually
be **ENFORCED**.

**PROOF THE TESTS BITE.** With all three guards disabled in a scratch copy,
**5 of the 22 tests go RED** — including `test_the_poc_the_fix_kills`, which
asserts an over-limit book + ARMED partition + un-built census yields `{}`:

```
FAILED TestUnbuiltCensusCanNeverAdmit::test_the_poc_the_fix_kills
FAILED TestUnbuiltCensusCanNeverAdmit::test_an_unbuilt_census_leaves_the_naive_number_enforcing
FAILED TestUnbuiltCensusCanNeverAdmit::test_a_slate_the_census_cannot_see_is_omitted
FAILED TestUnbuiltCensusCanNeverAdmit::test_the_corrected_number_can_never_exceed_the_naive_one
FAILED TestUnbuiltCensusCanNeverAdmit::test_the_readout_reports_the_enforced_number_not_zero
5 failed, 17 passed
```

and with the guards restored, **22/22 green**.

---

## 4. THE REQUIRED TESTS, EXECUTED — `tests/test_slate_partition_failclosed.py` (22/22)

| requirement | test | result |
|---|---|---|
| the aggregate never understates the true joint worst case, **including where the enumeration is wrong** | `test_honest_enumeration_dominates_the_true_worst_case` — 120 randomised books **brute-forced against the explicit outcome space** (per-game ME ⇒ exactly one arm realizes; non-ME ⇒ every subset realizable), asserting `bound >= true max` | PASS |
| | `test_a_multi_game_ticket_is_never_netted_away`, `test_unknown_me_metadata_folds_comonotone`, `test_an_ungamed_loss_event_is_pooled_never_dropped`, `test_an_exception_in_the_me_lookup_folds_comonotone` | PASS |
| | `test_the_clamp_holds_on_randomised_books` — 200 books: within `[max single, once-counted sum]` **and monotone** (adding a unit never lowers it — E2 mass-acceptance dominance preserved) | PASS |
| mutex-offsetting positions stop double-counting | `test_the_same_parlay_is_counted_once_per_slate_not_once_per_game` (one $50 ticket across 3 games: naive **$150**, partitioned **$50**), `test_within_game_mutually_exclusive_arms_net` ($40 + $30 on opposite exclusive arms ⇒ **$40**), `test_the_live_shape_naive_vs_partitioned` | PASS |
| a genuinely over-limit slate is **STILL REFUSED** | `test_over_the_wall_on_the_honest_measure_still_breaches` — 15 single-game tickets, $1,500 of **real, once-counted** premium against a $1,300 wall ⇒ `SKIP_SLATE_CAP` fires armed, carrying the corrected number in the detail; `test_a_candidate_that_genuinely_breaches_is_refused_armed`; `test_the_armed_number_is_never_above_the_naive_one` (40 randomised books) | PASS |
| `want_loss_units=False` can never yield a permissive answer | the 6 tests in §3 | PASS |
| shadow byte-identical to today | `test_disarmed_and_enabled_produce_identical_breaches` (reason list **and** detail strings), `test_an_observer_can_never_change_a_decision` (an observer that **raises** changes nothing), `test_arming_changes_exactly_the_slate_axis` | PASS |

**SHADOW PARITY vs HEAD `9a93925`, executed end-to-end.**
`tools/diagnostics/shadow_parity_admission.py` over **400** randomised cases
(books, candidates, resting quotes, bankrolls, cap fractions, haircut on/off, ME
on/off, shadow mode on/off), run once against a `9a93925` worktree and once
against this tree:

```
before bytes 209907  after bytes 209907   BYTE-IDENTICAL
(before run proven to be HEAD: module file .../head-wt/src/.../limits.py,
 has slate_partition_armed: False, exposure has loss_units_built: False)
```

---

## 5. GATE TAILS — BOTH REPORTED

| check | result |
|---|---|
| **full unit suite** | **3347 passed, 0 failed**, 3 deselected, 231.75s |
| **`tools/vitals/gate` fast tier** | **8/8 GREEN (GATE PASS)**, 15.4s |
| **`tools/vitals/gate --tier pre-ship`** | **1/1 GREEN (GATE PASS)**, 42.4s |
| **mypy strict, `src`** | 6 errors, **all pre-existing in `pricing/`** (`ising_amm.py` ×2, `engine.py` ×4). HEAD `9a93925` has **19** — this tree is strictly better, and `risk/` is clean |
| **ruff, every file this build touched** | **All checks passed** |
| **throughput, both levers OFF** (the state every untouched deployment runs in) | 4 alternating paired runs, live-shaped 118-position book + 20 resting quotes + candidate: HEAD `{3684.9, 3818.2, 3893.9, 4095.2}` µs/call vs TREE `{3779.3, 3754.0, 3823.7, 3731.4}`. **TREE wins 3 of 4 pairs; medians HEAD ~3,856 vs TREE ~3,766 (TREE 2.3% faster); best-of HEAD 3,684.9 vs TREE 3,731.4 (+1.3%).** No measurable regression — the loss-event census is not built at all with the lever off |
| **cost when the lever IS on** (`bench_admission_fixes.py`) | DEFAULT 2,940.7 µs → SHADOW 3,318.1 → ARMED 3,319.0 µs/call. The read-out costs what arming costs, so the shadow number the operator arms from is honest |

**VALIDATE-CAPS-CAN-QUOTE (standing rule — a cap must be proven to produce a
non-zero quote against real trade sizes BEFORE going live).** Live book, real
3-leg parlay on today's slate, live fractions:

```
  cand $    NAIVE slate    ARMED slate   other caps still speaking
$     10         quotes         quotes   []
$     25         quotes         quotes   []
$     50         quotes         quotes   []
$     69         quotes         quotes   ['skip_entity_loss_cap']
$    100         quotes         quotes   ['skip_entity_loss_cap']
$    200        REFUSED         quotes   ['skip_entity_loss_cap', 'skip_per_combo_loss_cap']
$    400        REFUSED         quotes   ['skip_entity_loss_cap', 'skip_per_combo_loss_cap']
```

The slate stops being the binding constraint at $200+; **per-combo (5%) and the
entity wall still speak, unchanged.** Protection, not limitation.

**ARMABLE.** `slate_partition_enabled` (derive-before-arm read-out) and
`slate_partition_armed` round-trip `RiskConfig.to_risk_limits()`, both
**default False**; a deployment that sets neither is byte-identical to HEAD.

**BLAST RADIUS.** `risk/exposure.py` (one new snapshot field + its builder flag),
`risk/limits.py` (`_slate_partition` guards + the read-out fix). Nothing in
`pricing/`, `marketdata/`, `exchange/`. Markups untouched.

---

## 6. 🔴 THE ENTITY AXIS — BUILT BY A CONCURRENT WORKSTREAM, AND IT DIVERGES FROM THE BRIEF

While this work was in flight, another session rewrote
`src/combomaker/risk/entity_admission.py` and the entity block of
`risk/limits.py` (mtimes 02:34–02:44 ET) to the operator's **tier table**:
`<1% no action | 1–2% tier 1 | 2–3% tier 2 | >3% DECLINE`, with a dollar-weighted
`combo_widen_weight` for "net across the combo". Its suite is
`tests/test_admission_fixes.py`, **51/51 green**, and it is included in the 3347.

That build's admission rule is: a breaching key is certified only when the key
was **COOL (< 1% prior)** before the candidate, the combo is **net diversifying
in dollars**, and the key's accumulated load fits **`combo_thr` — the per-COMBO
wall (5%)**.

**The brief this session was given says, twice and unconditionally:**

> "It also silently relocated per-key protection to the per-combo ceiling, moving
> max single-entity exposure from the ratified 3% to 5%. **Both are
> disqualifying.**"
>
> "The per-entity wall must **REMAIN 3%**. **Do not relocate protection to the 5%
> per-combo ceiling.**"

The in-tree build **does** relocate it — it says so loudly rather than silently,
and it bounds the relocation to a **single structure on a previously-cool key**
(a warm key is never certified, so accumulation across structures is still
refused at 3%). That is a materially tighter rule than the deleted
`net_effect.py`, and it is a defensible reading. **It is still a 3% → 5% move on
max single-entity exposure, and that is an operator decision, not an
implementer's.** It is flagged here rather than silently overridden.

**The alternative that keeps the wall at 3% was designed and then withdrawn to
avoid clobbering the concurrent edit**: partition the ticket's ONE loss event
across the entity arms it rides (`ceil(L / n_arms)` — the *same* count-each-loss-
event-once doctrine this slate fix applies across games), so the wall measures a
*share* of one loss event instead of a *copy* of it. Under it, the operator's
example works arithmetically — a hot arm is charged `L/5` when four fresh legs
ride along versus `L` as a pure add — the median refused $127.05 3-leg ticket
lands at **$42.35/arm = 1.79% (tier 1, admitted)**, and **max single-entity
exposure stays at 3%** because a 1-arm ticket is still charged in full. It is
NOT in the tree.

**Decision owed by the operator (§7).**

---

## 7. NEXT STEPS

* **Owner: operator — DECISION 1 (arm the slate).** Suggested sequence: set
  `risk.slate_partition_enabled: true` first (read-out only, enforcement
  unchanged), read `slate_partition_shadow` events for one slate — each line
  carries `slate / naive_cc / partitioned_cc / threshold_cc / would_admit` — then
  set `slate_partition_armed: true`. Both land on the **one** operator restart;
  the live process (PID 33724) is on old code and is not touched by this build.
* **Owner: operator — DECISION 2 (the entity axis, §6).** Either (a) ratify the
  in-tree tiered build's explicit invariant — *one* structure may carry a
  previously-cool entity to the per-combo 5%, and across structures the 3% wall
  is absolute — or (b) direct the partitioned-load variant, which keeps max
  single-entity exposure at 3%. This is the only open design question in the
  admission work.
* **Owner: next build.** The graded 1%/2% widening tiers are **telemetry only**
  today; the pricing layer does not yet consume `combo_widen_weight`. That is the
  widening spec's own NEXT STEPS item, plus the `peak` arming flag it still owes.
* **Owner: next build.** The tape replay's slate numbers are on a
  **committed-book + candidate** basis (resting quotes at decision time are not
  reconstructable from the store). The `resting`-flagged loss events and the
  burst-floored haircut composition ARE implemented and exercised by the suite;
  they are simply not measurable off the tape. Confirm against the
  `slate_partition_shadow` events once the read-out is enabled live.
* **Owner: nobody — stated so it is not re-derived.** `slate_loss_frac` is **not**
  to be re-tuned off this change. The wall did not move; the ruler was repaired.
  If the corrected measure makes the book too hot, that is a *cap* conversation
  with the operator, not a patch to this code.
