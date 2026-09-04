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
| O2 | Series ticker of a combo collection | — | **CLOSED (review S4)**: `docs/api-notes/multivariate.md` §1-2 documents `GET /multivariate_event_collections/{ticker}` → `multivariate_contract` = MultivariateEventCollection with `series_ticker` REQUIRED; the resolver reads exactly that shape (pinned in `tests/test_fee_seam_wiring.py`); fail-closed on anything else | `d4050bb` |
| O3 | Rebates under the measured floor | — | **DECISION OWED — RE-STATED (review M4a)**: the floor allows no rebate on thin cells and on most populated cells, but a populated cell whose floor is BELOW `margin//2 − fee` (4 such floor-0 cells on the live grade) makes the cap `margin − fee`, LOOSER than the 8/16 `margin//2`; the replay cannot observe this (no skew in `quote_sent`), so it is measured with a saturating rebate (259 quotes) and pinned in `tests/test_quote_fee_floor.py` — the operator ratifies the real rule (see §Review fixes) | `7aa5374`, `3a0ed48` |
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
LS through the origin 0.0350044; feasible-set pin = 7/200 = **0.0350** with 0 mismatches at ≤1cc;
0.0175 mismatches **540/540**. The pin is reached at the **1st** charged fill chronologically (review
fix M1 removed the cost-rounding residue from the parsed fee, so one ceiling is left and the
interval per fill is (charged − 1, charged]/X; a single 10-contract fill at 50c pins alone; twelve
1.2-contract fills pin together). On the WHOLE 4,228-fill history (3,582 pre-onset maker + 106
taker + 540 charged) the observer is SILENT: 540 charged, 0 mismatches, only
`KXMVECROSSCATEGORY-SHARD1` marked — the 1,763 pre-onset fills whose `fee_cost` is pure cost
rounding ($0.00002-0.00008) parse as uncharged (`tests/fixtures/ground_truth/exchange_fills_uncharged_20260827.json`).

**Previous builder's derivation repaired.** Its pinning bound `Σx/Σx² < quantum/2` equals `1/x̄` for
uniform fills, so a tape of many small fills could never pin regardless of count (the test
`test_bootstrap_is_the_taker_coefficient_never_zero` caught it). Replaced by the exact feasible
set from the ceiling (`CEIL_SLACK_CC = 1` after review fix M1 — one ceiling, a protocol fact) —
pins by data, detects regime changes as an empty intersection.

**Fills parity + fee-net EV** (547 post-onset store fills, tool section 1):

| quantity | value |
|---|---|
| `candidate_edge_cc(fee = booked)` == `fills.expected_edge_cc` | **547/547 exact** (ledger fair inverted from the ledger's own formula; the markout fair-at-fill cross-check: 423 exact, 119 differ by a fill-time re-price of −8.0c..+8.8c, 5 missing) |
| gross modeled edge | $241.85 |
| measured 0.035 fee | **$109.15 = 45% of the edge** |
| net | $132.70 |
| negative after fee | **140 fills / $3,157.32 premium** (spec: ~144 / $3,140 — the spec reconstructed tiers, this uses the ledger fair) |

**Fills the floor would have re-priced** (fee-net edge ≤ 0 at the fill's own bid and size — the
confirm gate's predicate, review fix M3; tool section 2):

| tier | fills | premium | gross | fee | neg | neg $ | re-priced by floor |
|---|---|---|---|---|---|---|---|
| mlb:mains100 | 249 | $3,039.42 | $99.66 | $50.59 | 40 | $726.19 | 40 / $726.19 |
| mlb:razor60 | 66 | $1,463.89 | $11.63 | $12.37 | 28 | $776.75 | 29 / $778.89 (one fill at exactly 0 net) |
| soccer:razor60 | 59 | $1,241.94 | $6.90 | $9.12 | 45 | $988.31 | 45 / $988.31 |
| mlb:ladder300 | 36 | $1,099.02 | $60.04 | $6.55 | 0 | — | 0 |
| soccer:mains100 | 48 | $914.10 | $17.47 | $14.35 | 17 | $391.01 | 17 / $391.01 |
| mlb:ladder200 | 59 | $887.20 | $32.11 | $10.12 | 0 | — | 0 |
| esports:razor60 | 14 | $375.00 | $2.70 | $2.80 | 9 | $255.92 | 9 / $255.92 |
| mlb:ladder250 / soccer:ladder200 / esports:mains300 | 15 | $268.33 | $11.20 | $3.09 | 0 | — | 0 |
| mixed:razor60 | 1 | $19.13 | $0.14 | $0.17 | 1 | $19.13 | 1 / $19.13 |
| **TOTAL** | **547** | **$9,308.03** | **$241.85** | **$109.15** | **140** | **$3,157.32** | **141 / $3,159.45** |

The razor (0.6c) is negative-EV on 83 of 140 razor fills; the mains lose 57 fills to rebates that
gave back more than the fee. Ladders never.

## Counterfactual / quote-ability (tool section 3: 59,578 `quote_sent` decisions from rowid 132892717 = the whole post-onset tape; FINAL run after the review fixes)

Replay method: fair, width and legs from `context_json`; markup re-derived with the live
`MarkupPolicy` on the live yaml; the applied skew reconstructed as the residual between the
recorded bid and fair_no − margin (the recorded bid is "today"; the ≤10cc grid residue rides
identically through every mode, so per-mode differences are exact). "parity" = the replay
reproduces the recorded bid (fails only where a clamp the context does not record fired). The
replay quantity is the SMALLEST post-onset fill on the tape (1.00 contract — the derived floor
is tightest there); the coefficient is the observer's fit on the fixture (review fix S5); the
onset rowid is found by bisection on `decisions.at`.

| tier | n | rebated | parity | nz today | nz floor | nz width | moved floor (mean cc) | moved width (mean cc) | nz cell | moved cell (mean cc) | cap loosened |
|---|---|---|---|---|---|---|---|---|---|---|---|
| mlb:mains100 | 13,202 | 4,641 | 13,202 | 13,202 | **13,202** | 13,202 | 2,855 (−29.2) | 13,202 (−86.7) | 13,202 | 4,631 (−45.6) | 0 |
| mlb:razor60 | 12,966 | 1,281 | 12,894 | 12,966 | **12,966** | 12,966 | 6,992 (−19.3) | 12,966 (−63.3) | 12,966 | 7,264 (−19.9) | 0 |
| mlb:ladder300 | 9,913 | 2,799 | 9,913 | 9,913 | **9,913** | 9,913 | 0 | 9,913 (−50.1) | 9,913 | 2,797 (−63.8) | 13 |
| soccer:mains100 | 7,557 | 4,900 | 7,557 | 7,557 | **7,557** | 7,557 | 4,330 (−29.9) | 7,557 (−86.9) | 7,557 | 4,900 (−45.0) | 2 |
| mlb:ladder200 | 5,937 | 2,368 | 5,937 | 5,937 | **5,937** | 5,937 | 0 | 5,937 (−82.4) | 5,937 | 2,363 (−63.6) | 34 |
| mlb:ladder250 | 2,811 | 866 | 2,811 | 2,811 | **2,811** | 2,811 | 0 | 2,811 (−72.6) | 2,811 | 865 (−56.9) | 16 |
| soccer:ladder200 | 2,545 | 1,954 | 2,545 | 2,545 | **2,545** | 2,545 | 0 | 2,545 (−74.9) | 2,545 | 1,954 (−58.5) | 0 |
| soccer:razor60 | 2,209 | 1,737 | 2,103 | 2,209 | **2,209** | 2,209 | 1,693 (−31.7) | 2,209 (−66.0) | 2,209 | 1,915 (−33.7) | 127 |
| soccer:ladder300 | 594 | 376 | 594 | 594 | **594** | 594 | 0 | 594 (−50.2) | 594 | 376 (−54.5) | 0 |
| mixed:mains300 | 586 | 251 | 586 | 586 | **586** | 586 | 0 | 586 (−88.6) | 586 | 251 (−83.8) | 0 |
| mixed:ladder300 | 573 | 160 | 573 | 573 | **573** | 573 | 0 | 573 (−68.5) | 573 | 160 (−64.6) | 0 |
| esports:mains300 | 276 | 238 | 276 | 276 | **276** | 276 | 0 | 276 (−88.4) | 276 | 184 (−54.5) | 62 |
| esports:razor60 | 257 | 185 | 257 | 257 | **257** | 257 | 188 (−21.5) | 257 (−68.0) | 257 | 226 (−22.8) | 5 |
| mixed:razor60 | 152 | 15 | 152 | 152 | **152** | 152 | 80 (−16.5) | 152 (−61.9) | 152 | 81 (−16.5) | 0 |

**GATE: no tier goes to zero under floor** (or under cell / width). Under `floor` the only bids
that move are (a) rebated mains quotes whose rebate exceeded margin − m_min (trimmed, ~−29cc)
and (b) the razor wherever m_min exceeds 0.6c (~−19cc); every ladder bid is byte-identical.
`width` moves every bid by −50..−89cc (the option-W table in the spec). At 1.00 contract the
floor moves ~1% more quotes than the previous 10-contract replay (2,855 vs 2,822 mains; 6,992 vs
6,699 razor) — the gate-derived `m_min` needs one more cc at one contract.

**Cell floor (item 2) on the real settled grade** (3,259 graded tickers, 31.4-day span, 646 cells,
445 thin): pool upper floors mlb 5.9c / soccer 11.6c / esports 26.7c / other (cross-sport) 49.5c
(pool quantiles 3.01 / 3.06 / 3.15 / 19.21 — see §Review fixes, t-quantile); populated cells
0-59c (e.g. `mlb|rfi|rfi|all_no|cross` n=30, mean shortfall −20.0c/ct, SE 10.4c → floor 44.8c;
`mlb|player_hrr|player_hrr|all_yes|same` n=168, −10.9c/ct, SE 7.2c → 30.6c). Per-contract settlement
noise on a sell-only book is ~40-50c, so at 30-170 settled games the clustered SE is 4-15c and the
tail bound dwarfs every 1-3c margin on thin cells and on most populated cells (the "cell" columns:
rebated quotes trimmed to zero rebate). **BUT — review fix M4a — the earlier reading "no rebate on
any populated cell" was a replay artefact**: a populated cell whose floor is BELOW `margin//2 − fee`
makes the cap `margin − fee`, LOOSER than the 8/16 `margin//2` rule, and the replay cannot see it
because a recorded `quote_sent` carries no skew (the reconstructed residual can never exceed the cap
that produced it). Measured with a saturating rebate through the live `construct_quote` ("cap
loosened" column): **259 quotes on 4 populated floor-0 cells** (`esports|ML×3|all_yes|cross`,
`mlb|ML|HRR|all_yes|cross`, `mlb|HRR|KS|all_yes|cross`, `soccer|ML×4|all_yes|cross`). Before the
t-quantile fix this number was 1,667 quotes on 218 cells, because the 3-row cross-sport pool
published a floor of 0. The operator owes a ruling (O3) on the real rule: cap = margin − fee −
floor, which a floor-0 cell turns into margin − fee.

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
| tests/test_fee_observer.py | 15 | live string parse; fixture exact at 0.035 (540/540), fails at 0.0175; fit pins 0.0350; derived pin count; drift alarm on a synthetic 0.0175 tape; taker bootstrap; persistence round-trip (hand-edited coefficient re-derived, corrupt file cold); fee-type precedence; parser fail-closed (+ order_id never a fill id); **M1: a real pre-onset residue fill is uncharged; the whole 4,228-fill history ingests silently (0 mismatches, only SHARD1 marked); a schema-1 file self-heals** |
| tests/test_fee_seam_wiring.py | 6 | first tick measures 0.0350 from the real fixture via a paginated fake getter, persists, resolves the series; engine == ledger on the per-combo fee type (sharded observation does not mark the unsharded collection); warm sweep polls new fills only + drift metric; no getter ⇒ no sweep; FeeConfig has no bare maker number; **S4: the documented collection payload shape** |
| tests/test_fee_net_edge.py | 4 | pure arithmetic; ledger == quote-time EV == confirm-time fresh edge == KILL-marginal fill EV, net of the same nonzero fee; replay books once; exchange-reported fee nets INSTEAD of the model; plain quadratic records gross |
| tests/test_quote_fee_floor.py | 108 | width == pre-existing formula (80-case grid); floor mains at skew 0 byte-identical to today (15 cases); razor 6940→6920 exact (+ fair 2090 @ 1 ct: 7850 → 7840, confirm edge 0 → +10); rebate cap margin − m_min; cell floor replaces margin//2; **M4b: a floor-0 cell loosens the cap beyond margin//2 (pivot = margin//2 − fee)**; UNKNOWN no-quotes in both modes; bad mode refused; **M3: every floor bid over fair 10.00-34.99c × {1, 1.2, 2.5, 10} contracts clears the confirm gate (> 0)**; property (300 examples × qty): fee-blind ≥ floor, floor ≥ width except one grid step on sub-floor tiers, floor retains ≥ fee AND its confirm edge > 0; engine pass-through; real-shape cell keys |
| tests/test_retained_edge_floor.py | 12 | weighting + clustering; span gate; tail-quantile upper floor; thin → pool upper; shrinkage; mirror key; rebate-bound rules; store read + lifecycle sweep publishes table + pool floors; **M2: absent cell → sport pool, unknown sport → largest pool, never None while published; S8: rows without event tickers are their own cluster; t-quantile ladder; a 3-row pool cannot publish a 0 floor** |
| tests/test_conc_arming.py | +1 | **S1: a cold steer under the armed flag still has its unbacked leg-axis rebate removed (wire == OFF == SHIPPED; shadow record shows the removal)** |
| **added** | **146** | all pass (132 in the build + 14 in the review fix pass) |

Deliberately changed pin (comment cites this build): `tests/test_conc_arming.py::test_the_quote_on_the_wire_is_identical_in_shadow` —
the world's only OFF/SHADOW price mover was an unbacked empty-cell leg-axis rebate (family −62 +
entity −9 cc on `M1:yes`/`M2:no`, never held), which `rebate_bound` now removes; the world holds the
mirror directions so the arming-visibility guard keeps testing the steer. Shadow == off identity
preserved; arming stays visible.

Full suite (PYTHONPATH=src …/python.exe -m pytest -q -p no:cacheprovider), FINAL after the review
fixes: **3,924 passed, 0 failed, 3 deselected in 230s** (main: 3,778 + 146 added; the build's own run was 3,910/0).

ruff clean on every file touched EXCEPT `tools/diagnostics/restart_gate2_quote_validation.py`,
which carries **19 pre-existing ruff errors identical on main** (the build only touched its
imports; review S3 — the earlier "ruff clean on every file touched" claim was wrong for that file).
mypy clean on every touched module except the 4 pre-existing `tuple` type-arg errors in
`pricing/engine.py` (present on main) and the pre-existing errors in `tests/test_lifecycle.py`
(imported by the new tests). `tools.vitals.gate` NOT run (the orchestrator runs it after merge).

## Parity results

- rule 8 prototype → port (review S2): `tools/proto_fee_edge.py` is the COMMITTED prototype —
  plain arithmetic, no import from the module under test — and its parity run on the recorded
  tape: exchange fee = ceil(fee) + cost residue on **4,122/4,122** maker fills (`exchange/fills`
  parity 4,122/4,122); LS 0.0350044 and feasible set (0.035000, 0.035000] → pin 7/200 ==
  `fit_maker_coefficient`; `FeeModel.trade_fee_cc` == charged on **540/540**; the floor prototype
  == `construct_quote(floor)` on **62,976/62,976** grid points (fair × markup × skew × qty × cell
  floor), every posted bid's confirm edge > 0.
- `rfq/edge.candidate_edge_cc(fee = booked)` == `fills.expected_edge_cc` on **547/547** post-onset fills.
- `construct_quote(fee_mode="width")` == the pre-existing arithmetic on the 80-case grid; floor at
  skew 0 == today's bid on every mains/ladder case (15 pins + 0 ladder bids moved on 21,860 replayed
  ladder quotes).

## Review fixes (2026-09-04, fix pass on the adversarial review — verdict SHIP_WITH_FIXES)

| # | Finding | Fix | Evidence | Commit |
|---|---|---|---|---|
| M1 | The parser ceiled the raw `fee_cost`, so the exchange's < 1cc position-cost rounding residue counted as a CHARGED maker fee on 1,763 pre-onset fills: a permanent `fee_schedule_mismatch` alarm on every sweep and two collections marked maker-fee-observed off a residue | `exchange/fills.py` subtracts the residue `ceil_cc(C·P) − C·P` EXACTLY (Fraction, from the row's own size and price) and ceils what remains; `CEIL_SLACK_CC` derives to 1; schema v2 with `from_json` undoing a v1 file's `ceil(fee + r) = fee + [r > 0]`, dropping residue rows and re-deriving `collections_active` (self-heal) | residue-free fee is a whole cc on 4,122/4,122 maker fills (0 on 3,582/3,582 pre-onset, > 0 on 540/540 post-onset); whole-history ingest: 540 charged, 0 mismatches, `{KXMVECROSSCATEGORY-SHARD1}`; new fixture of the 3,688 non-charged real rows | `bfdfeb9` |
| M2 | `_retained_floor_for` returned None for a cell with no settled record → `margin//2`, the loosest cap in the system (300 margin / 88 fee: absent 150cc vs thin 0 vs floor-0 212) | the estimator's pool upper bounds travel with the table (`publish_retained_floor(table, pool_floor_cc)`); ONE lookup rule `pricing/retained_cell.floor_for_cell` (engine + tool): own floor → sport pool → largest published pool (unknown sport) → largest cell floor; never None while a table is published | lookup pins on the reviewer's numbers; the sweep test resolves an unseen mlb cell to the mlb pool and the harness's `other` sport to the largest pool | `4202183` |
| M3 | The per-contract floor left the confirm gate (`admission_ev <= 0` refused) at exactly 0 on 244 and −1cc on 22 of 7,500 razor probes — won-then-declined auctions | the floor IS the gate's predicate at the quote's own quantity: `m_min = ⌈(F + 1)·100/qty⌉`, F the whole-fill fee at the fee-maximising price of the plausible range (the +1 = the strict inequality; ⌈⌉ and 100/qty = the two roundings) | live `construct_quote` + `candidate_edge_cc(trade_fee_cc)`: 7,500/7,500 razor and 3,660/3,660 mains cases > 0 (worst +1cc); all 3,660 mains bids at skew 0 still byte-identical to fee-blind; razor 0.30 @ 10ct still 6920; fair 2090 @ 1ct 7850 → 7840; prototype parity 62,976/62,976 | `7aa5374` |
| M4 | "no rebate on any populated cell / rebate axis retired" was a replay artefact: floor-0/low cells LOOSEN the cap beyond `margin//2`, and the replay is blind to it (no skew in `quote_sent`) | (a) report + O3 re-stated above; (b) `test_a_floor_zero_cell_loosens_the_cap_beyond_half_margin` pins cap = margin − fee (212 > 150) and the pivot `margin//2 − fee`; (c) the tool measures "cap loosened" with a saturating rebate through the live pricer and names the cells | FINAL: 259 quotes on 4 populated floor-0 cells (was 1,667 on 218 before the t-quantile fix below) | `7aa5374`, `3a0ed48` |
| fix-pass finding | The M4c flag showed EVERY mixed-tier quote loosened: the cross-sport pool (3 rows, 3 clusters, +27.9c/ct, SE 4.0c) published a floor of 0 because `z·SE` treated an SE from two degrees of freedom as known, and every absent cross-sport cell inherited it through M2 | the anchor stays `K_DAILY = 3`, applied as its TAIL PROBABILITY Φ(−3) through the Student-t quantile at clusters − 1 df (`tail_quantile`): 3.0 at 1,400 clusters, 3.85 at 12, 19.2 at 3 — nothing typed; cells use their own cluster count, pools theirs | pool floors mlb 5.9c / soccer 11.6c / esports 26.7c / other 49.5c (was 0); loosened 1,667 → 259 quotes; the 120-cluster test cells moved from 3.00 to 3.08 SEs (pin comment cites this) | `1fc70ad` |
| S1 | `leg_axis_armed and not conc_armed` let an unbacked leg-axis rebate through whenever the CRN profile was cold under the armed flag (skew.py composes the leg axis whenever `conc is None`) | `conc_priced = conc_armed and skew.conc is not None` drives both the es_value cap and the leg-axis rule | wire test: (conc_enabled=False, conc_armed=True) on the un-mirrored world == OFF == SHIPPED; shadow record: rule exposure_backed, unbacked > 0, applied 0 | `d4050bb`, `7c054b3` |
| S2 | no committed prototype | `tools/proto_fee_edge.py` (residue, coefficient, floor) + parity run | see §Parity results | `3a0ed48` |
| S3 | "ruff clean on every file touched" was false for `restart_gate2_quote_validation.py` | stated: 19 errors, identical on main | ruff on worktree and main both `Found 19 errors.` | report |
| S4 | O2 resolvable from `docs/api-notes/multivariate.md` §1-2 | the wiring fake serves the documented `multivariate_contract` shape; direct pin on `_series_ticker_from_collection` + fail-closed variants; O2 CLOSED | test | `d4050bb` |
| S5 | the tool typed `MEASURED = 0.035` and a rowid | coefficient fitted by the live observer from the fixture; taker from config; onset rowid by bisection on `decisions.at`; replay qty = the smallest post-onset fill (derived) | tool header line prints the fit | `3a0ed48` |
| S6 | pricing-pool workers | stated: `ops/pricing_pool.py` workers only run `compute_joint` (no fee evaluation) — they correctly receive NO schedule; the fee is applied in `construct_quote` on the engine that owns the shared `ObservedFeeSchedule` | code trace | report |
| S7 | `order_id` as a fill-id fallback would collapse target-cost partials | fail closed (`fill_id`/`trade_id` only) | parser pin | `bfdfeb9` |
| S8 | rows whose legs carry no event ticker shared ONE empty cluster | `grade_row_from_store` (pure; lifecycle + tool share it) keys such rows on the combo ticker | conversion pin (2 rows → 2 clusters) | `4202183` |
| S9 | persisted schedule file after M1 | NO `fee_schedule_observed.json` exists anywhere yet (checked `data/` and `D:/kalshi-combos-TWO-data/` — the bot has not run on this branch); a v1 file would self-heal on load (pinned); first-relight gate below | filesystem check | — |

Blast radius of the fix pass: `exchange/fills.py` (wire parse for the observer only), `pricing/fee_observer.py`
(slow loop), `pricing/quote.py` floor branch (width mode byte-identical; mains byte-identical at
skew 0), `pricing/retained_cell.py` + `pricing/engine.py` lookup (one or two dict reads on the quote
path), `risk/retained_edge_floor.py` (slow loop), `rfq/lifecycle.py` rebate-bound guard + sweep
publish, the two tools. No cap, wall, markup, joint, fair, settlement or P&L arithmetic touched.

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
- First relight (S9): the boot log `fee_schedule_loaded` must show `mismatches=0` and, after the
  first sweep, `collections_active == ['KXMVECROSSCATEGORY-SHARD1']` with `n_charged` == the charged
  count — any `fee_schedule_mismatch` on the history walk is a real formula/multiplier change now,
  not residue noise. `fee_series_fee_type` should resolve the quoted collections' series (O2 closed
  by the documented shape; a `fee_series_unresolved` line would mean the live payload departs from
  the docs).
- Operator ratification of the fix-pass rules: (a) the floor is the confirm predicate at the
  quote's own size (`m_min`), (b) a floor-0 cell's rebate cap is `margin − fee` (looser than the
  8/16 `margin//2` on 4 cells / 259 quotes today), (c) the tail quantile follows the clusters
  (a 3-row pool floors at 49.5c, not 0).
- Pooled reads (pre-registered, ≥ 2 weeks, game-clustered, never a P&L refit): razor-class fill
  count (expected ~0 under floor), mains retained edge net of fee, realized-vs-expected-net by
  tier; the floor table's per-cell SE trajectory (when a cell's floor first drops below its margin).
