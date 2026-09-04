# 2026-09-04 — Build A: the maker-fee seam (item 1) + the measured rebate floor (item 2)

Branch `build/fee-seam-rebate-floor` (worktree build; bot DOWN throughout, live
store opened read-only, nothing written to it). Continues the previous builder's
partial work (fees.py enum, observer core, fill parser — kept, one derivation
repaired) and finishes the brief. Specs: `explain_fee-seam.txt` (repair design
1-5 + gates) and `explain_mlb-rfi-sgp.txt` §6.

## WRONG / FIXED / OPEN

| # | Item | Was | Now | Commit |
|---|---|---|---|---|
| 1 | Kalshi's live fee string `quadratic_with_combo_maker_fees` | `FeeType.parse` → UNKNOWN → `FeeUnknownError` → **NoQuote on every combo** (setting the exchange's own string bricked quoting) | Enum entry routed to the maker branch; `charges_maker`; UNKNOWN still fail-closed | `7db8287` |
| 2 | Maker coefficient | Hand-set yaml `maker_coef: "0.0175"` — half the truth since 8/20; two frozen copies (engine + ledger) | **MEASURED** from charged exchange fills (`pricing/fee_observer.py`): exact feasible-set pin from the ceilings, 0.0350 on 540/540 real fills, drift ERROR (no halt), taker-conservative bootstrap, persisted `data/fee_schedule_observed.json`; ONE `ObservedFeeSchedule` shared by pricer, ledger, waiver; `FeeConfig.maker_coef` deleted, `maker_coef_override` = logged OVERRIDE only | `7db8287`, `021c669` |
| 3 | Fee type per combo | yaml prefix list (empty live) ⇒ every fill priced/booked at fee 0 | override prefix > **observed charged fee on the collection** > **series `fee_type` (GET /series, cached)** > default; resolved identically by engine and ledger | `021c669` |
| 4 | Every EV the bot judges (confirm admission, eviction key, KILL-marginal, ledger) | gross of fee | **net of the fee this fill pays**, through ONE function `rfq/edge.candidate_edge_cc`; ledger subtraction idempotent (the fee it books enters once) | `67e2824` |
| 5 | Fee in the quote | subtracted from the bid only when the (dead) prefix list fired | `pricing.fee.mode` **floor** (default) / width: floor = bid stays at fair − margin, margin floored at the fee, rebate ≤ margin − fee; every bid whose markup clears the fee is byte-identical to today; the razor posts fair − fee | `cab7efe` |
| 6 | Rebate cap | hand fraction `margin // 2` (8/16), fee-blind | **measured per-cell floor** (`risk/retained_edge_floor.py`: settled grade, game-clustered SE, EB shrink, z = policy daily anchor 3, thin cells → sport pool upper bound, ≥ 14-day span) replaces it when published; rebate bounded by measured value (`risk/rebate_bound.py`: ES Cov price once the steer is armed; unbacked leg-axis rebates removed) | `4e4bdbc` |
| 7 | Validation on real tape | none | `tools/diagnostics/fee_floor_counterfactual.py`: 547 fills parity 547/547, 59,578 quotes replayed, no tier goes to zero | `ec2ca16` |
| O1 | Store back-fill of the 547 post-onset fills' `fee_cc`/`expected_edge_cc` | gross | **OPEN** — a separate read-write task after operator go (this build never wrote the live store) | — |
| O2 | Series ticker of a combo collection | — | **UNVERIFIED**: read from the collection payload's `series_ticker` (top level or one level down); the docs notes do not carry that schema; fail-closed to observed/default if absent (the live case is covered by observation: all 540 charged fills sit on `KXMVECROSSCATEGORY-SHARD1`) | — |
| O3 | Rebates under the measured floor | — | **DECISION OWED**: the settled record cannot justify ANY rebate at z=3 (see §Cell floor) — the floor retires the rebate axis until cells hold ~100× more settled games; the operator should ratify or re-anchor (see NEXT STEPS) | — |
| O4 | Throughput on the live wire | — | not measurable with the bot down; micro-benchmark below + O(1) argument | — |

## What changed (files, mechanism)

```
exchange fills (GET /portfolio/fills, same handle the recovery sweep polls)
      │  exchange/fills.py  fail-closed row → FeeObservation
      ▼
pricing/fee_observer.py  ObservedFeeSchedule  ── persisted data/fee_schedule_observed.json
      │   pin: ∩_i (charged_i−2, charged_i]/X_i holds ONE quantum multiple  (X = 10⁴·C·P·(1−P))
      │   regime window (newest agreeing fills) · LS cross-check · validate |model−charged|≤1cc
      │   drift ⇒ ERROR log fee_schedule_drift (never a halt) · bootstrap = TAKER coef
      │   fee_type_for: override prefix > observed collection > series fee_type > default
      ├──────────────► pricing/engine.py  FeeModel(schedule) · fee_type_for(rfq) · fee_mode · floor table lookup
      ├──────────────► rfq/lifecycle.py   FeeModel(schedule) · _effective_fee_type · _fill_fee_cc
      │                                   _sweep_fee_observer (1st tick, then 15-min cadence; ≤3 pages;
      │                                   series resolution ≤3 collections/sweep; atomic persist off-loop)
      ▼
rfq/edge.py candidate_edge_cc = (side_fair − bid)·qty//100 − fee   ← the ONE edge function
      ├─ _pricing_edge_cc (confirm admission_ev)   ├─ _quote_candidate_ev_cc (eviction key, slot ranking)
      ├─ _fill_ev_cc → _kill_marginal_for_fill     ├─ _kill_marginal_for_quote (via OpenQuoteRisk)
      └─ fills.expected_edge_cc (fee it BOOKS: exchange-reported on replay, else model)

pricing/quote.py construct_quote(fee_mode, retained_floor_cc)
      width: bid = side_fair − margin − fee_range + rebate,  rebate ≤ margin//2        (pre-existing)
      floor: fee_range over [side_fair − max(margin, fee_peak) − |skew|, side_fair]
             margin ← max(margin, fee_range);  rebate ≤ margin − fee_range
             with a cell floor: rebate ≤ margin − fee_range − floor_cell  (replaces margin//2)
             bid = side_fair − margin + rebate  (fee NOT subtracted)

risk/retained_edge_floor.py (slow loop, Store.settled_grade_rows one batched read)
      cell = (sport_of, sorted classify_leg types, side pattern, same/cross/partial)  [pricing/retained_cell.py]
      shortfall/ct = realized − (expected_edge + booked fee − settlement fee)   (both net of exchange fee)
      SE game-clustered · τ² method-of-moments · EB shrink · thin (w<½) → pool upper · z = K_DAILY
      floor_cell = max(0, z·SE_post − mean_post) → engine.publish_retained_floor(table)

risk/rebate_bound.py (lifecycle _quoting_policy)
      steer ARMED: rebate ≤ ⌈value_cc_per_contract⌉ (CRN Cov price); else: leg-axis rebate on an
      unheld mirror direction removed (exposure_backed). Widen never touched. Shadow steer off the wire.
```

## Measured evidence

**Fee schedule (real tape).** 540 charged maker fills 2026-08-20T09:07:19Z → 08-27 (fixture
`tests/fixtures/ground_truth/maker_fee_20260820.json`, all on `KXMVECROSSCATEGORY-SHARD1`). The
exchange's reported fee is exactly `ceil_cc(0.035·C·P·(1−P)) + (ceil_cc(C·P) − C·P)` on **540/540**;
LS through the origin 0.0350096; feasible-set pin = 7/200 = **0.0350** with 0 mismatches at ≤1cc;
0.0175 mismatches **540/540**. The pin is reached at the **2nd** charged fill chronologically (a
single 10-contract fill at 50c pins alone; twelve 1.2-contract fills pin together).

**Previous builder's derivation repaired.** Its pinning bound `Σx/Σx² < quantum/2` equals `1/x̄` for
uniform fills, so a tape of many small fills could never pin regardless of count (the test
`test_bootstrap_is_the_taker_coefficient_never_zero` caught it). Replaced by the exact feasible
set from the two ceilings (`CEIL_SLACK_CC = 2`, a protocol fact) — pins by data, detects regime
changes as an empty intersection.

**Fills parity + fee-net EV** (547 post-onset store fills, tool section 1):

| quantity | value |
|---|---|
| `candidate_edge_cc(fee = booked)` == `fills.expected_edge_cc` | **547/547 exact** (ledger fair inverted from the ledger's own formula; the markout fair-at-fill cross-check: 423 exact, 119 differ by a fill-time re-price of −8.0c..+8.8c, 5 missing) |
| gross modeled edge | $241.85 |
| measured 0.035 fee | **$109.15 = 45% of the edge** |
| net | $132.70 |
| negative after fee | **140 fills / $3,157.32 premium** (spec: ~144 / $3,140 — the spec reconstructed tiers, this uses the ledger fair) |

**Fills the floor would have re-priced** (retained/ct < fee/ct at the bid; tool section 2):

| tier | fills | premium | gross | fee | neg | neg $ | re-priced by floor |
|---|---|---|---|---|---|---|---|
| mlb:mains100 | 249 | $3,039.42 | $99.66 | $50.59 | 40 | $726.19 | 40 / $726.19 |
| mlb:razor60 | 66 | $1,463.89 | $11.63 | $12.37 | 28 | $776.75 | 28 / $776.75 |
| soccer:razor60 | 59 | $1,241.94 | $6.90 | $9.12 | 45 | $988.31 | 45 / $988.31 |
| mlb:ladder300 | 36 | $1,099.02 | $60.04 | $6.55 | 0 | — | 0 |
| soccer:mains100 | 48 | $914.10 | $17.47 | $14.35 | 17 | $391.01 | 17 / $391.01 |
| mlb:ladder200 | 59 | $887.20 | $32.11 | $10.12 | 0 | — | 0 |
| esports:razor60 | 14 | $375.00 | $2.70 | $2.80 | 9 | $255.92 | 9 / $255.92 |
| mlb:ladder250 / soccer:ladder200 / esports:mains300 | 15 | $268.33 | $11.20 | $3.09 | 0 | — | 0 |
| mixed:razor60 | 1 | $19.13 | $0.14 | $0.17 | 1 | $19.13 | 1 / $19.13 |
| **TOTAL** | **547** | **$9,308.03** | **$241.85** | **$109.15** | **140** | **$3,157.32** | **140 / $3,157.32** |

The razor (0.6c) is negative-EV on 83 of 140 razor fills; the mains lose 57 fills to rebates that
gave back more than the fee. Ladders never.

## Counterfactual / quote-ability (tool section 3: 59,578 `quote_sent` decisions from rowid 132892717 = the whole post-onset tape)

Replay method: fair, width and legs from `context_json`; markup re-derived with the live
`MarkupPolicy` on the live yaml; the applied skew reconstructed as the residual between the
recorded bid and fair_no − margin (the recorded bid is "today"; the ≤10cc grid residue rides
identically through every mode, so per-mode differences are exact). "parity" = the replay
reproduces the recorded bid (fails only where a clamp the context does not record fired).

| tier | n | rebated | parity | nz today | nz floor | nz width | moved floor (mean cc) | moved width (mean cc) | nz cell | moved cell (mean cc) |
|---|---|---|---|---|---|---|---|---|---|---|
| mlb:mains100 | 13,202 | 4,641 | 13,202 | 13,202 | **13,202** | 13,202 | 2,822 (−28.6) | 13,202 (−86.7) | 13,202 | 4,174 (−39.2) |
| mlb:razor60 | 12,966 | 1,281 | 12,894 | 12,966 | **12,966** | 12,966 | 6,699 (−18.8) | 12,966 (−63.3) | 12,966 | 6,849 (−19.1) |
| mlb:ladder300 | 9,913 | 2,799 | 9,913 | 9,913 | **9,913** | 9,913 | 0 | 9,913 (−50.1) | 9,913 | 563 (−65.5) |
| soccer:mains100 | 7,557 | 4,900 | 7,557 | 7,557 | **7,557** | 7,557 | 4,303 (−29.3) | 7,557 (−86.9) | 7,557 | 4,687 (−42.6) |
| mlb:ladder200 | 5,937 | 2,368 | 5,937 | 5,937 | **5,937** | 5,937 | 0 | 5,937 (−82.4) | 5,937 | 945 (−57.9) |
| mlb:ladder250 | 2,811 | 866 | 2,811 | 2,811 | **2,811** | 2,811 | 0 | 2,811 (−72.6) | 2,811 | 410 (−58.1) |
| soccer:ladder200 | 2,545 | 1,954 | 2,545 | 2,545 | **2,545** | 2,545 | 0 | 2,545 (−74.9) | 2,545 | 790 (−58.8) |
| soccer:razor60 | 2,209 | 1,737 | 2,103 | 2,209 | **2,209** | 2,209 | 1,678 (−31.2) | 2,209 (−66.0) | 2,209 | 1,867 (−33.2) |
| soccer:ladder300 | 594 | 376 | 594 | 594 | **594** | 594 | 0 | 594 (−50.2) | 594 | 60 (−67.3) |
| mixed:mains300 | 586 | 251 | 586 | 586 | **586** | 586 | 0 | 586 (−88.6) | 586 | 0 |
| mixed:ladder300 | 573 | 160 | 573 | 573 | **573** | 573 | 0 | 573 (−68.5) | 573 | 0 |
| esports:mains300 | 276 | 238 | 276 | 276 | **276** | 276 | 0 | 276 (−88.4) | 276 | 184 (−54.5) |
| esports:razor60 | 257 | 185 | 257 | 257 | **257** | 257 | 184 (−21.2) | 257 (−68.0) | 257 | 216 (−22.7) |
| mixed:razor60 | 152 | 15 | 152 | 152 | **152** | 152 | 78 (−16.0) | 152 (−61.9) | 152 | 78 (−16.0) |

**GATE: no tier goes to zero under floor** (or under cell / width). Under `floor` the only bids
that move are (a) rebated mains quotes whose rebate exceeded margin − fee (trimmed to the fee,
~−29cc) and (b) the razor wherever the fee over the bid range exceeds 0.6c (~−19cc); every ladder
bid is byte-identical. `width` moves every bid by −50..−89cc (the option-W table in the spec).

**Cell floor (item 2) on the real settled grade** (3,259 graded tickers, 31.4-day span, 646 cells,
445 thin): pool upper floors mlb 5.9c / soccer 11.3c / esports 25.3c; populated cells 15-59c
(e.g. `mlb|rfi|rfi|all_no|cross` n=30, mean shortfall −20.0c/ct, SE 10.4c → floor 44.8c;
`mlb|player_hrr|player_hrr|all_yes|same` n=168, −10.9c/ct, SE 7.2c → 30.6c). Per-contract settlement
noise on a sell-only book is ~40-50c, so at 30-170 settled games the clustered SE is 4-15c and
z·SE (z=3) dwarfs every 1-3c margin: **the measured floor allows no rebate on any populated cell**
(the "cell" columns: every rebated quote is trimmed to zero rebate; `mixed` combos key to the
`other` sport pool whose floor is 0 and are untouched). The mechanism is correct and the reading
is honest — the rebate axis is retired by measurement until cells accumulate roughly 100× the
settled games (SE ∝ 1/√n). The operator owes a ruling (O3).

**Throughput.** construct_quote micro-benchmark (30k calls each): today 13.3 µs, width 11.0 µs,
floor 11.3 µs, floor+cell 11.3 µs per quote — no measurable difference. Quote-path additions:
`fee_type_for` (a few string-prefix compares), `cell_key` (O(legs) prefix classifiers, same work
the markup already does), one dict lookup, one extra per-side fee evaluation, `bound_rebate`
(O(legs) key builds). All store work (observer, floor estimator) runs in single-flight
wall-bounded slow-loop sweeps. Live sends/min before/after cannot be measured with the bot down
— the first live hour after relight must stay in the 300-460 sends/min band (feedback_throughput_never_regress).

## Tests

| file | tests | what |
|---|---|---|
| tests/test_fee_observer.py | 12 | live string parse; fixture exact at 0.035 (540/540), fails at 0.0175; fit pins 0.0350; derived pin count; drift alarm on a synthetic 0.0175 tape; taker bootstrap; persistence round-trip (hand-edited coefficient re-derived, corrupt file cold); fee-type precedence; parser fail-closed |
| tests/test_fee_seam_wiring.py | 5 | first tick measures 0.0350 from the real fixture via a paginated fake getter, persists, resolves the series; engine == ledger on the per-combo fee type (sharded observation does not mark the unsharded collection); warm sweep polls new fills only + drift metric; no getter ⇒ no sweep; FeeConfig has no bare maker number |
| tests/test_fee_net_edge.py | 4 | pure arithmetic; ledger == quote-time EV == confirm-time fresh edge == KILL-marginal fill EV, net of the same nonzero fee; replay books once; exchange-reported fee nets INSTEAD of the model; plain quadratic records gross |
| tests/test_quote_fee_floor.py | 103 | width == pre-existing formula (80-case grid); floor mains at skew 0 byte-identical to today (15 cases); razor 6940→6920 exact; rebate cap margin − fee (300cc rebate: today 50, floor 12); cell floor replaces margin//2; UNKNOWN no-quotes in both modes; bad mode refused; property fee-blind ≥ floor ≥ width and floor retains ≥ fee at its own bid (300 examples); engine pass-through; real-shape cell keys |
| tests/test_retained_edge_floor.py | 8 | weighting + clustering; span gate; z-upper floor; thin → pool upper; shrinkage; mirror key; rebate-bound rules; store read + lifecycle sweep publishes to the engine |
| **added** | **132** | all pass |

Deliberately changed pin (comment cites this build): `tests/test_conc_arming.py::test_the_quote_on_the_wire_is_identical_in_shadow` —
the world's only OFF/SHADOW price mover was an unbacked empty-cell leg-axis rebate (family −62 +
entity −9 cc on `M1:yes`/`M2:no`, never held), which `rebate_bound` now removes; the world holds the
mirror directions so the arming-visibility guard keeps testing the steer. Shadow == off identity
preserved; arming stays visible.

Full suite (PYTHONPATH=src …/python.exe -m pytest -q -p no:cacheprovider): **3,910 passed, 0 failed, 3 deselected in 275s** (main: 3,778 + 132 added).

ruff clean on every file touched; mypy clean on every touched module except the 4 pre-existing
`tuple` type-arg errors in `pricing/engine.py` (present on main) and the pre-existing errors in
`tests/test_lifecycle.py` (imported by the new tests). `tools.vitals.gate` NOT run (the
orchestrator runs it after merge).

## Parity results

- rule 8 prototype → port: the fee observer's fit was prototyped against the real exchange pull
  (scratchpad script, 540 fills) before the module's tests pinned the same numbers; the quote-path
  floor was proven on the recorded tape by the counterfactual tool importing the LIVE
  `construct_quote` (no reimplementation).
- `rfq/edge.candidate_edge_cc(fee = booked)` == `fills.expected_edge_cc` on **547/547** post-onset fills.
- `construct_quote(fee_mode="width")` == the pre-existing arithmetic on the 80-case grid; floor at
  skew 0 == today's bid on every mains/ladder case (15 pins + 0 ladder bids moved on 21,860 replayed
  ladder quotes).

## Blast radius

Pricing: `construct_quote` gains two keyword parameters with pre-existing defaults (`fee_mode="width"`,
`retained_floor_cc=None`) — every existing caller is byte-identical; the engine passes the
config mode (default `floor`) and, under a cold/zero fee, floor == width == today. Risk: the
inventory rebate is bounded (never the widen side); caps, walls, anchors, markups, joint, fair,
settlement and P&L arithmetic untouched. Ledger: `expected_edge_cc` is computed through the same
function it always mirrored, with the fee it books (0 → identical rows). Slow loop: two new
single-flight, wall-bounded, alarm-only sweeps (observer, floor) + one batched store read; no
awaits on the tick. Config: `FeeConfig.maker_coef` removed (a yaml carrying it would now fail
validation — the live yaml has no `pricing.fee` block).

## Gitignored yaml — lines the operator must add

None required: `pricing.fee.mode` defaults to `floor` and the maker coefficient is measured. To
make the choice explicit (recommended, nothing numeric):

```yaml
pricing:
  fee:
    mode: floor        # "width" restores the pre-2026-09-04 subtraction
```

Never set `maker_coef_override` or `maker_fee_active_prefixes` unless deliberately overriding the
measurement (both are logged as OVERRIDE at boot).

## NEXT STEPS

- Orchestrator: merge → `tools.vitals.gate` fast tier (touches pricing/, risk/, rfq/) and
  `--tier pre-ship` before arming; then the first relight runs the observer on the first
  maintenance tick (log `fee_schedule_refit … maker_coef=0.0350 source=observed`) and the floor
  estimator (`retained_floor_estimate`).
- Operator decisions owed: (1) ratify `mode: floor` (the razor dissolves into max(0.6c, fee) — it
  posts fair − 0.75c on a 70c NO fair instead of fair − 0.6c; whether to keep quoting it at all is
  the 8/16 competitive-distance read); (2) rule on O3 — the measured floor retires every rebate at
  the policy z; keep as is (fail-closed, rebates return as cells accumulate), or re-anchor the
  floor's z to a stated appetite (a constitution change, not a knob bump); (3) authorise the
  store back-fill of the 547 post-onset rows (O1).
- Live reconciliation after relight (spec gate): the first 20 charged fills reconcile to the cc
  with zero `fee_schedule_mismatch`; `fills.fee_cc` nonzero on every charged fill; in-bot realized
  P&L delta equals the exchange balance delta over the first session; sends/min inside 300-460.
- Verify O2 on the first relight: `fee_series_fee_type` (or `fee_series_unresolved`) log lines —
  if the collection payload carries no `series_ticker`, add the correct field to
  `_series_ticker_from_collection`.
- Pooled reads (pre-registered, ≥ 2 weeks, game-clustered, never a P&L refit): razor-class fill
  count (expected ~0 under floor), mains retained edge net of fee, realized-vs-expected-net by
  tier; the floor table's per-cell SE trajectory (when a cell's floor first drops below its margin).
