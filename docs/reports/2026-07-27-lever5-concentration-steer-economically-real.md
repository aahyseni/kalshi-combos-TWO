# LEVER #5 — the concentration steer, made ECONOMICALLY REAL (2026-07-27)

**Status: BUILT, tested, benchmarked. Suite 3189 passed / 0 failed / 3 deselected.
Lands on ONE operator restart. Markups UNCHANGED (operator decision, binding) —
this reallocates INSIDE the existing margin.**

## The problem, as measured

| measurement | value | against |
|---|---|---|
| median `\|applied_cc\|` | 20cc | a 200cc median margin = **5.6% of markup** |
| full concentrating→diversifying swing | 0.36–0.39c | **smaller than one tier step** |
| composed clamps `[-300, +1200]` | fired **0 times** in 300,000 events | — |
| steer below one 10cc tick | **32.25%** annihilated by `snap_bid_down` | — |

Three attenuators, all removed:

| # | attenuator | fix |
|---|---|---|
| a | 4:1 clamp asymmetry (12c widen ceiling vs 3c rebate ceiling) | ONE symmetric derived `half_cc`; the composed classifier is RESCALED onto it through a denominator **shared by both signs** |
| b | 1/n_keys COUNT deficit — decayed 42% (0.0250→0.0145) in one session *because we diversified successfully* | **count basis deleted.** Dollars vs each axis's OWN ENFORCED WALL + the AND-bound dollar-Herfindahl (SE exactly 0) |
| c | `need = 1 − p_book` multiplying every component (43% strength, weakening as p_book rises) | not present in the new component at all |

## What was built

New live module `src/combomaker/risk/concentration_steer.py` + wiring in
`risk/skew.py`, `rfq/lifecycle.py`, `marketdata/grid.py`.

### 1. AND-BINDING — concentration is measured on the TICKET

A `requires-all` combo forfeits its premium only if EVERY leg lands, so the
ticket is **ONE loss event**. `ticket_bucket()` keys a position's whole premium
on the UNION of the axis keys it needs (games ∪ family×side ∪ entity×side);
`LossEventBook` is the inverse dollar-Herfindahl `(Σd)²/Σd²` over those buckets.

Reproduces the measured pair exactly (pinned by test):
`test_three_match_ticket_is_one_loss_event` → **1.00**;
`test_split_into_three_tickets_reads_about_three` → **2.99**.
Bench: the live-shaped 38-ticket book reads **38.00 effective loss events**
where the leg-wise read would have counted **107**.

`marginal()` is **O(1)** off cached running sums (S1/S2) — the 1.47µs figure —
rebuilt only when `position_generation` moves.

### 2. The COUNT deficit is gone

* `wall_load(dollars, keys, wall_cc)` — this key's premium dollars against the
  threshold `risk/limits.py` actually refuses on (`game_loss_frac × bankroll`,
  `entity_loss_frac × bankroll`). **A wall does not move when a new key
  appears**, so success cannot decay it (pinned:
  `test_adding_a_new_key_does_not_decay_an_existing_reading`).
* `_pbook_component` / `_leg_axis_side` keep ONLY the dollar-denominated WIDEN
  (`onset` vs the wall). Their count-derived rebate branches now report
  `underweight_priced_by_hhi` and contribute **0** — the diversification reward
  moved wholesale to the zero-SE Herfindahl. This deliberately avoids the
  alternative reading (`deficit = 2·load − 1`), which made a small book rebate
  **every** quote — an un-ratified markup cut.

### 3. The PRICE — `Cov(candidate payoff, pre-existing book P&L)`

```
value_cc_per_contract = CC_PER_DOLLAR × Cov(hit, book_pnl_cc) / bankroll_cc
```

The exact log-utility / mean-variance certainty-equivalent term: **no free
parameter**, scales with bankroll automatically, EV-orthogonal by construction.
Published off the hot path (`_publish_crn_cache`) from the **PRICING joint**
`corr_location_point` — never the tail-stress joint the enforced gates ride
(2026-07-26 axis split). Generation-stamped; publishes only if the drawn
sample's own measured SNR clears the ratified **z = 3** anchor, else abstains.

**`delta_p_book` is not an input and cannot become one** — pinned by
`test_delta_p_book_is_not_an_input_anywhere`, which greps the module body.
(R² = 0.921 vs candidate EV, 42.2% unresolvable at 3σ at n=20k, 41.7% of
EV-residual signs flip across caches.)

### 4. The SCALE — measured state, symmetric, never a constant

`half_cc = min` of three MEASURED bounds; an unmeasured bound abstains rather
than zeroing the steer:

| bound | derivation |
|---|---|
| `value` | the measured cc/contract dispersion of the covariance signal itself |
| `elasticity` | `1/(2e)` cents = **227cc** at the measured CMH-stratified e = 0.22 fills-lost-per-cent — the validity horizon of the measurement, not a knob |
| `margin` | the LIVE `total_width_cc` of this quote. Markups are FIXED, so the steer can only reallocate inside width that already exists |

`SteerCenter` tracks the live **mean and dispersion** of the score: centring is
the budget-neutrality mechanism (markups fixed) and standardising is what makes
±1σ of the concentration signal span the full derived range. Both are strictly
increasing affine maps, so the operator invariant survives them exactly.

### 5. Sub-tick annihilation — structurally impossible now

`PriceGrid.step_at()` (new) gives the combo's own lattice step;
`tick_ladder()` returns an exact integer multiple of it, with a **minimum of
one whole tick** on either side of zero. `snap_bid_down` therefore reproduces
the steer instead of erasing it — pinned by
`test_the_tick_steer_survives_snap_bid_down_exactly`, and end-to-end through
the real `construct_quote` in `test_all_three_stages_including_the_grid_snap`
(post-snap `no_bid` difference is **exactly** the steer).

### 6. THE OPERATOR INVARIANT, as a property

`assert_diversifier_tighter(div, conc, pre_snap=…, post_snap=…)` checks at the
**CLASSIFIER**, **PRE-SNAP** and **POST-SNAP**, with `quote_rank_cc()` ranking a
`NoQuote` at `−inf` (infinite width) so *a refusal can never land on the
diversifier* falls out of the same inequality. 300 randomised books pass.

Stated honestly (and documented in the function): STRICT when the diversifier
lowers concentration and the other does not; WEAK (≥, never inverted) when both
diversify and differ only in degree — a finite tick lattice cannot separate
every pair, which is exactly what "ordering survives rounding" means.

## Measured results (`tools/diagnostics/bench_concentration_steer.py`)

Live-shaped book: 38 tickets / $411 premium / p_book 0.58 / 200cc margin /
10cc tick / walls at the live bankroll ($2,179.74); 4,000 candidates, half
landing on a loss event the book already holds.

### applied_cc (pricer frame; + = TIGHTER) — the markup-neutrality check

| | mean | median | med\|x\| | min | max |
|---|---|---|---|---|---|
| BEFORE | −321.10 | −311.0 | 311.0 | −631 | −35 |
| AFTER | **−45.84** | −50.0 | **50.0** | −120 | +20 |

**mean delta +275.26cc** — the average quote got **TIGHTER**, not wider. The
requirement ("must not widen the average quote") is met with room; the residual
−45.8cc mean is the *pre-existing armed* pbook/leg-axis widen, which Lever #5
compresses rather than adds to.

### Economic reality

| | before (measured live) | after |
|---|---|---|
| diversifier→concentrator swing | 0.36–0.39c | **0.77c** |
| as % of a 200cc margin | 5.6% | **38.5%** |
| median \|applied_cc\| | 20cc | **50cc** |
| sub-tick / off-lattice | 32.25% annihilated | **0.00% / 0.00%** |

Implied fill-rate effect at the measured e = 0.22/cent: diversifier side
−1.62%, concentrator side −18.54% — the steer now moves flow, which is the
whole point; a symmetric steer cancels to first order in aggregate.

### Quote-path throughput

| | µs / candidate |
|---|---|
| `compute_inventory_skew` BEFORE | 14.78 |
| `compute_inventory_skew` AFTER | 32.53 |
| delta | **+17.76** |
| anchor: `exposure.snapshot()` in the SAME function | 425.92 |
| **`_quoting_policy` block: 440.7 → 458.5 µs** | **+4.03%** |

The steer costs 4% of the block that already runs once per quote (and a far
smaller share of the whole RFQ path, which is joint-dominated). Optimised
during the build: the `game_key` import is memoised into a module global and
all three axis key sets + the AND-bound bucket are computed in ONE pass over
the legs (that alone took the delta from +26.4µs to +17.8µs).

## Suite

```
3189 passed, 3 deselected in 196.12s
ruff: All checks passed (touched files)
mypy: Success: no issues found in 29 source files (risk/ + rfq/)
```

Six pre-existing tests encoded the COUNT-basis rebate; all six were **ported,
not deleted** — each now asserts the same doctrine through the component that
owns it, with the supersession documented in the test docstring
(`test_skew_pbook.py` ×4, `test_leg_axis.py` ×2, `test_rebate_scaling.py` ×3).
34 new tests in `tests/test_concentration_steer.py`.

## Blast radius

* Pricing path: `risk/skew.py` composition + `rfq/lifecycle._quoting_policy`.
  With `conc_profile=None` (every pre-existing caller, every untouched test) the
  composition is **byte-identical** to the pre-Lever-#5 classifier.
* Slow loop only: `_publish_crn_cache` rides the off-hot-path book-risk publish,
  runs at most once per position generation, and fails to `None` on any
  exception (the steer then runs on the zero-SE Herfindahl alone).
* No cap, halt, or refusal reads any of this. The steer is **pricing only** — it
  never feeds `per_game`, so `decide_widen_or_decline` cannot decline on it.

## NEXT STEPS

1. **Operator decision — arm posture.** The bot is DOWN/flat (KILL). This lands
   on the next restart with `skew.enabled / pbook_armed / leg_axis_armed` as
   configured today. Decide whether to relight with the new steer live or run
   one slate reading `inventory_skew_shadow.conc_*` first
   (`conc_hhi_marginal`, `conc_value_cc_per_contract`, `conc_wall_loads`,
   `conc_half_cc`, `conc_scale_binding`, `steer_centre_mean/sd/n` are all in
   the INFO line).
2. **Owed follow-up — the last hand-set numbers in the composed steer.**
   `peak_widen_max_cc = 600` / `peak_tighten_max_cc = 150` still set the legacy
   components' magnitudes and, on the bench book, dominate the composed total
   (rebate side reaches only +20cc against −120cc on the widen side). By the
   North Star these should dissolve into the same derived half-range. Not done
   here to keep the blast radius on ONE lever.
3. **Measure, then re-derive `fill_elasticity_per_cent`.** It is a
   `LifecycleConfig` MEASUREMENT (0.22, CMH-stratified) and the steer's horizon
   moves automatically when it is re-measured. Owner: whoever runs the next
   fill-rate stratification.
4. **CRN cache resolvability on the live book.** Watch `crn_cache_published`
   (`value_sd_cc`, `snr`) vs `crn_cache_unresolvable`. If the live book is too
   small to clear z = 3, the covariance term abstains by design and the steer
   runs zero-SE — confirm that is what the log shows before reading anything
   into the magnitudes.
