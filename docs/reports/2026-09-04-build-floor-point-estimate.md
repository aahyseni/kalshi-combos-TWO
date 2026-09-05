# 2026-09-04 — Build: the retained-edge floor is the SHRUNK POINT shortfall (not a 3σ bound)

Branch `build/floor-point-estimate` (worktree `../kct-floor-fix`; bot LIVE on
main throughout — the store was opened READ-ONLY, nothing written, every
replay/suite run at BelowNormal priority). Repairs build A item 2
(`2026-09-04-build-fee-seam-rebate-floor.md`) the same day it went live.

## WRONG / FIXED / OPEN

| # | Item | Was | Now | Evidence |
|---|---|---|---|---|
| 1 | **What the floor measures** | `floor = max(0, t_{G−1}(Φ(−3))·SE − shortfall)`: the policy z ladder's daily anchor as a 3σ UPPER bound on the adverse selection. Live at 22:45:44Z: pool floors mlb 5.9¢ / soccer 11.6¢ / esports 26.7¢ / cross-sport 49.5¢, populated floors 15–59¢ — against 1–3¢ tier margins. Rebate cap `margin − fee − floor` ≤ 0 on essentially every quote: the diversity steer (skew rebate) was MUTED on the wire | `floor = max(0, −shortfall_post)`: the empirical-Bayes SHRUNK POINT of the cell's settled (realized − modeled)/contract, NO z·SE term. Thin cells (derived n_min: SE² > τ²) take the sport pool's POINT; an unknown sport the LARGEST pool point (fail-closed direction, but a point). A losing cell keeps its whole measured loss (no rebate where the record says we lose); a cell at/above the model floors at 0 = the fee alone | live grade now: pool points mlb 0.85¢ / soccer 0 / esports 0 / other 0 (the mlb pool loses 0.84¢/ct vs model on 3,335 settled tickers); 206 populated cells: 88 losing (floor > 0), 118 at the fee |
| 2 | **Rebate cap** | `margin − m_min − floor` with the bound floor (measured path) — in effect 0 | `margin − m_min − floor_point`; the 8/16 `margin // 2` hand fraction is GONE from the measured path (already was); a floor-0 cell may rebate up to `margin − m_min`, a losing cell nothing; the fee floor (`m_min`, review fix M3), the ES-value cap and the exposure-backed rule are untouched | replay: rebate room on **13,077 / 20,000** post-8/20 quotes under the point rule vs **202** under the wire's upper rule (tonight: 779 / 1,158 vs 41) |
| 3 | **Where the uncertainty goes** | into the retained floor (3 SEs) | NOT into the floor. Measured and reported (`CellEstimate.post_se_cc`, `summarize.pool_se_cc_by_sport`) for the width seam; **not fed to the width in this build — see §Uncertainty and the width** (it would re-mute the same quotes on the other lever; the conversion is a new number) | tonight's quote widths: median half-width **43 cc**; populated-cell posterior SE median **810 cc** (ratio 19×) |
| 4 | Tests | pins on the t-quantile bound | pins on the point: negative cell = fee + \|shortfall\|, positive cell = fee alone, thin = pool point, unknown sport = largest pool point, property floor ≥ 0 and (through `construct_quote`) retained ≥ m_min always and ≥ m_min + floor unless the margin is smaller; the 3-row +27.9¢ cross-sport pool now floors at 0 by its point (pin changed, cited) | `tests/test_retained_edge_floor.py` 15 tests (+`test_quote_fee_floor.py` unchanged, 108) |
| 5 | Prototype → port → parity (rule 8) | — | `tools/proto_floor_point.py` (plain arithmetic, no import of the estimator): point table == live estimator **656/656 cells**, pools equal; through `construct_quote` on every replayed quote **20,000/20,000 + 1,158/1,158**; the prototype's UPPER table == build A's estimator from `main` on **656/656** cells (so the counterfactual's "upper" column IS the wire's rule) | this report, §Parity |
| O1 | The pre-publication fallback | — | **OPEN (named, not fixed):** in floor mode with NO table yet (cold boot before the first sweep, or every sweep timed out) the cap is still `margin // 2` — the last hand fraction on this path. Derived alternative: persist the last published table like `fee_schedule_observed.json` so a boot never starts unmeasured. The live boot published at 22:45:44 in the same second as `quote_warmup_open`, so the window is seconds — unless the store is saturated (O2) | `pricing/quote.py` comment names it |
| O2 | The sweep on the 213 GB store | — | **OPEN:** `retained_floor_sweep_timeout` (5 s wall) at 23:00:56 and 23:15:56 (18:45 boot) and 23:45:17 (19:29 boot); the sweeps at boot and at 00:00:12Z completed — so the table refreshes intermittently on the saturated store (stale = still measured); the point rule makes the sweep cheaper (no `t.ppf` per cell) but the read is the cost. Store rotation P0 retires this | live logs |
| O3 | Small-n populated cells | — | **OPEN (observation):** with n0 derived as today (`w = τ²/(τ² + SE²)`), a 2-row cell whose two clusters happen to agree has a tiny SE and takes ~full weight — e.g. `mlb\|player_hit\|player_hrr\|total\|mixed\|same` n=2, −68.6¢/ct → floor 68.6¢ (rebate 0; never a widen). The direction is fail-closed on losing cells and loose on winning ones. A hierarchical variance prior (shrink the cell's SE² toward the pool's per-cluster variance) is the derived repair; not added here — it needs its own derivation, not a cluster-count knob | prototype output |
| O5 | Rule 9 on a live machine | — | **OPEN (gate design):** `tools/vitals/derive.tape_facts` keys its cache on a manifest of every `live_*.log` INCLUDING the one the bot is writing, so the cache never matches while the bot runs and every gate run rescans the whole 266 GB tape from the live store's drive. Fix (derived, no number): manifest over CLOSED logs only (exclude `CURRENT_LOG.txt`'s file), or a `VITALS_DATA_DIR` snapshot with the store and no tape. Until then the gate belongs to the orchestrator's post-merge window, not to a builder on the live box | this run |
| O4 | Store writer sampling | — | the 18:45-boot log carries 15,938 `quote_sent` lines vs **1,158** `quote_sent` decisions in the store for the same range — the counterfactual's "tonight" is the store's sample (the known store-collapse class, not this build). NOTE: the bot was relit on `main` at 19:29 ET (`live_20260904_1929.log`, `quote_app_starting` 23:29:25Z) and again at 20:10 ET (`live_20260904_2010.log`, after the 00:09:07Z stall-kill; 8–9.5k log lines/min, 1,749 `quote_sent` in its first 5 min) — not by this build; both boots price on build A's rule (the "upper" column, the same 656-cell table) until this branch merges | log vs store |

## What changed (mechanism)

```
risk/retained_edge_floor.py  (slow loop, unchanged inputs: Store.settled_grade_rows)
   shortfall/ct = realized − (expected_edge + booked fee − settlement fee)    (unchanged)
   cell mean contract-weighted, SE game-clustered, τ² method-of-moments,       (unchanged)
   w = τ²/(τ² + SE²) = n/(n + n0), post = w·x̄ + (1 − w)·μ_sport               (unchanged)
   thin  = w < ½                                                              (unchanged)
   WAS:  floor = max(0, t_{G−1}(Φ(−3))·SE_post − post);  thin → pool UPPER
   NOW:  floor = max(0, ⌈−post⌉);                         thin → pool POINT = max(0, ⌈−μ_sport⌉)
         (Z_FLOOR, tail_quantile, scipy.stats.t, CellEstimate.quantile, FloorEstimate.z REMOVED)
         summarize(): rule=shrunk_point, n_populated_losing / n_populated_at_fee, pool means + SEs
pricing/retained_cell.floor_for_cell   own → sport pool point → largest pool point   (logic unchanged)
pricing/quote.py construct_quote       rebate ≤ margin − m_min − floor                (ARITHMETIC UNCHANGED)
rfq/lifecycle._sweep_retained_floor    publishes table + pool points                  (unchanged)
```

Blast radius: **the size of the inventory REBATE only** (through the values the
slow loop publishes). `construct_quote`'s arithmetic is byte-identical (every
zero-skew quote in both replays reproduces its bid; only quotes whose recorded
skew was a rebate move). The widen direction, the fee floor `m_min`, the
ES-value cap, the exposure-backed rule, every cap/wall, the markup ladder, the
joint, fair, settlement and P&L arithmetic are untouched. Quote path: the same
one-or-two dict lookups. Slow loop: strictly less work per sweep (no Student-t
quantile per cell), one batched read as before.

## Counterfactual (tools/diagnostics/fee_floor_counterfactual.py --with-cell-floor, read-only, BelowNormal)

Three rules on the same replayed quote, all through the live `construct_quote`
at the smallest post-onset fill (1.00 contract) with the observer-fitted 0.0350
schedule: **fee-only** (floor mode, no table = `min(margin // 2, margin − m_min)`,
today's None path), **upper** (build A's bound — the table the wire has priced
on since 22:45:44Z; reproduced by the prototype and proven equal to `main`'s
estimator cell-by-cell) and **point** (this build). The rebate CAP is measured
with a saturating synthetic rebate (the whole margin) because a recorded
`quote_sent` carries no skew (review fix M4c).

### Post-8/20 range (20,000 `quote_sent` from rowid 132892717, tape recorded fee-blind)

| tier | n | nz point | point vs upper: moved (mean cc) | point vs fee-only: moved (mean cc) | cap fee-only (mean cc / open) | cap upper (mean / open) | cap point (mean / open) |
|---|---|---|---|---|---|---|---|
| mlb:razor60 | 4,907 | 4,907 | 56 (+28.2) | 100 (−27.5) | 15.8 / 1,501 | 0.0 / 0 | 12.9 / 1,114 |
| mlb:mains100 | 4,778 | 4,778 | 152 (+46.8) | 265 (−28.3) | 28.9 / 4,778 | 0.2 / 46 | 15.1 / 1,393 |
| mlb:ladder300 | 4,064 | 4,064 | 541 (+71.2) | 35 (−63.4) | 150.0 / 4,064 | 0.5 / 9 | 162.8 / 3,879 |
| mlb:ladder200 | 2,213 | 2,213 | 666 (+37.9) | 524 (−46.4) | 100.8 / 2,213 | 0.5 / 10 | 47.1 / 1,884 |
| soccer:mains100 | 1,581 | 1,581 | 405 (+20.4) | 20 (−19.5) | 19.9 / 1,581 | 0.7 / 50 | 19.6 / 1,511 |
| mlb:ladder250 | 1,076 | 1,076 | 206 (+45.0) | 61 (−38.9) | 125.2 / 1,076 | 1.5 / 9 | 95.7 / 927 |
| soccer:razor60 | 724 | 724 | 88 (+25.5) | 10 (−5.0) | 17.3 / 227 | 7.3 / 58 | 23.5 / 223 |
| soccer:ladder200 | 305 | 305 | 210 (+59.3) | 26 (−58.1) | 100.0 / 305 | 0.0 / 0 | 118.9 / 279 |
| esports:mains300 | 82 | 82 | 62 (+46.3) | 0 | 150.0 / 82 | 43.9 / 17 | 212.4 / 82 |
| soccer:ladder300 | 76 | 76 | 14 (+100.0) | 1 (−100.0) | 150.0 / 76 | 0.0 / 0 | 235.9 / 72 |
| mixed:mains300 | 60 | 60 | 18 (+98.9) | 0 | 150.0 / 60 | 0.0 / 0 | 212.0 / 60 |
| esports:razor60 | 56 | 56 | 0 | 0 | 14.5 / 19 | 5.5 / 3 | 19.6 / 18 |
| mixed:ladder300 | 51 | 51 | 15 (+80.0) | 0 | 150.0 / 51 | 0.0 / 0 | 229.6 / 51 |
| mixed:razor60 | 27 | 27 | 0 | 0 | 19.3 / 13 | 0.0 / 0 | 27.8 / 13 |
| **total** | **20,000** | **20,000** | **2,433 up** | **1,042 down** | 16,546 open | **202 open (1.0%)** | **13,077 open (65%)** |

Reading: "point vs upper" is the rebate COMING BACK — bids move UP by +20..+100 cc
on the 2,433 quotes whose recorded rebate the bound had clamped (bounded by what
was recorded, which the pre-8/16 `margin // 2` capped; the saturating-cap columns
show the real room). "point vs fee-only" is the mechanism WORKING the other way:
1,042 bids move DOWN by −5..−100 cc on the losing cells where the fee-only path's
`margin // 2` had let a rebate through (mlb:ladder200 524 quotes at −46 cc — the
HRR/total/hit cross-game NO shapes below). **No tier goes to zero under any
rule.** "cap loosened" beyond `margin // 2`: 4,997 quotes on 623 cells (a floor-0
cell rebates up to `margin − m_min`; still bounded by the ES-value cap / the
exposure-backed rule / the fee floor).

### Tonight's range (1,158 `quote_sent` from rowid 134174705 = 22:45:44Z, tape recorded in floor mode, `--tape-mode floor`)

| tier | n | nz point | cap fee-only (mean cc / open) | cap upper = the wire (mean / open) | cap point (mean / open) |
|---|---|---|---|---|---|
| soccer:razor60 | 245 | 245 | 24.7 / 122 | 8.6 / 30 | 34.0 / 119 |
| soccer:mains100 | 206 | 206 | 20.4 / 206 | 0.9 / 11 | 17.6 / 183 |
| mlb:ladder300 | 163 | 163 | 150.0 / 163 | 0.0 / 0 | 169.0 / 155 |
| mlb:mains100 | 131 | 131 | 17.3 / 131 | 0.0 / 0 | 3.7 / 30 |
| mlb:razor60 | 113 | 113 | 17.2 / 39 | 0.0 / 0 | 15.3 / 29 |
| soccer:ladder200 | 113 | 113 | 100.0 / 113 | 0.0 / 0 | 126.3 / 111 |
| mlb:ladder200 | 92 | 92 | 100.0 / 92 | 0.0 / 0 | 46.8 / 69 |
| soccer:ladder300 | 51 | 51 | 150.0 / 51 | 0.0 / 0 | 251.2 / 51 |
| mlb:ladder250 | 33 | 33 | 124.2 / 33 | 0.0 / 0 | 75.2 / 26 |
| mixed:razor60 / mains300 | 11 | 11 | 20–150 / 6 | 0 / 0 | 28.6–220 / 6 |
| **total** | **1,158** | **1,158** | 956 open | **41 open (3.5%)** | **779 open (67%)** |

The wire tonight allowed a rebate on 41 of 1,158 quotes (soccer razor/mains
only, ≤ 9 cc mean); the point rule allows one on 779, at the tier's full
measured room on the ladders. Zero-skew bids are identical under every rule
(the "moved" columns on tonight's tape are the razor's `m_min` only, as in the
build A run).

### Cells that still allow NO rebate under the point rule (the genuinely losing shapes)

Post-8/20 range: **69 of 953 cells quoted, 4,518 of 20,000 quotes (22.6%)**; tonight
21 of 228 cells, 191 of 1,158 (16.5%). The measured record behind the largest:

| cell | quotes (post-8/20) | floor cc | n | G | mean ¢/ct | shrunk ¢/ct | SE ¢ |
|---|---|---|---|---|---|---|---|
| mlb\|ML\|ML\|all_yes\|cross | 2,894 | 171 | 70 | 55 | −1.76 | −1.70 | 5.98 |
| mlb\|total\|total\|all_yes\|cross | 359 | 734 | 34 | 34 | −7.52 | −7.34 | 3.77 |
| mlb\|HRR\|HRR\|all_yes\|cross | 287 | 494 | 43 | 40 | −6.50 | −4.93 | 13.8 |
| mlb\|hit\|hit\|hit\|all_yes\|cross | 250 | 1,775 | 11 | 11 | −28.9 | −17.7 | 18.0 |
| mlb\|HRR\|HRR\|HRR\|all_yes\|cross | 171 | 307 | 27 | 27 | −3.66 | −3.06 | 11.5 |
| mlb\|ML×5..×12\|all_yes\|cross (absent) | 75+68+17+14+10+4 | 85 (pool) | — | — | — | mlb pool −0.84 | — |
| mlb\|KS×5\|all_yes\|cross | 69 | 338 | 21 | 21 | −4.62 | −3.38 | 15.5 |
| soccer\|total\|total\|all_yes\|cross | 56 | 3,944 | 5 | 4 | −42.8 | −39.4 | 5.3 |
| mlb\|KS\|KS\|total\|all_yes\|cross | 51 | 4,015 | 2 | 2 | −41.8 | −40.1 | 4.5 |

The two-leg cross-game ML parlay (the razor/mains class) is the big one: 70
settled rows in 55 game clusters, −1.76 ¢/ct vs the model (SE 6 ¢ — a noisy
cell; the shrunk point is −1.70 ¢), so its floor 1.71 ¢ exceeds the razor's
0.75 ¢ and the mains' 1 ¢: no rebate on that shape until its record turns.
Absent mlb shapes (ML ×5+) take the mlb pool point 0.85 ¢, which closes the
razor/mains margins and leaves 40–130 cc on the ladders. Every other losing
cell is a NO-side cross-game total / HRR / hit / KS basket — the 8/16 and 9/4
deep dives' shapes. Not a blocklist: each is a measured shortfall, and each
re-opens by itself as its settled record improves.

## Uncertainty and the width (item 3 — measured, then deferred, no knob)

`construct_quote` has two width seams: `width["uncertainty"] = joint.uncertainty ×
$1 × uncertainty_width_scale` (probability-space model uncertainty from the
copula's typed/untyped/pair-ρ uncertainty; the live yaml zeroes every other
component so `half = uncertainty // 2`) and `width_multiplier` (a tilt
`_width_multiplier` returns 1.0 or the favourites' `favorite_width_multiplier`,
floored at half the base spread). Neither takes a per-cell term today.

Measured tonight (1,161 `quote_sent`, read-only): total width **median 86 cc,
p90 284, max 438 → half-width median 43 cc**; the populated cells' posterior SE
**median 810 cc (8.1 ¢), p90 1,439, min 17**; 61 of 206 populated cells have SE >
|point| (the sign of their adverse selection is not resolved). Feeding |SE|
straight through `joint.uncertainty` would put the median half-width at ~405 cc
> every markup (60–300 cc), i.e. widen every populated-cell quote by 1–4 ¢: the
exact muting this build removes, moved from the retained floor to the width. Any
scale between the two is a number a human would set (the SE is the sampling
error of a P&L MEAN over G settled games; the width prices the model's error on
THIS quote's fair — different quantities), and `width_multiplier` has no upper
bound to derive it from. So: NOT wired. Follow-up (derived, not a knob): the
width's per-cell input should be a measured FAIR calibration for the cell (the
reliability curve / Brier read — settlement frequency vs quoted probability, in
probability units the seam already speaks), not the SE of a shortfall. The SE
stays in `CellEstimate.post_se_cc` and the log line for that read.

## Tests

| file | tests | what |
|---|---|---|
| tests/test_retained_edge_floor.py | 15 | PIN CHANGED (cited): `test_floor_is_the_shrunk_point_shortfall` (negative cell = ⌈−post⌉ = 20–30 cc here, strictly below build A's bound; positive cell = 0 with SE > 0; summary rule/counters; no z / quantile keys), `test_thin_cell_takes_the_sport_pools_point` (losing pool → its measured loss, not the bound; winning pool → 0), NEW `test_unknown_sport_takes_the_largest_pool_point` (through the live lookup), NEW `test_point_floor_is_the_measured_loss_and_never_negative`, NEW property `test_every_published_floor_is_non_negative_and_a_point` (200 examples), NEW property `test_retained_margin_after_the_rebate_never_drops_below_fee_plus_floor` (300 examples × 5 quantities through the live `construct_quote`: retained ≥ m_min always, ≥ m_min + floor unless the margin is smaller, confirm edge > 0), sweep pin now EXACT (rfi 250 / ml 0 / mlb pool 110), `test_an_outperforming_pool_floors_at_zero_by_its_point` replaces the 3-row-pool bound pin; `tail_quantile` tests removed with the function |
| tests/test_quote_fee_floor.py | 108 | unchanged and green (the clamp arithmetic did not move) |
| full suite | see §Gates | |

Deliberately changed pins (each carries a comment citing this build and the
measured reason — floors of 15–59 ¢ vs 1–3 ¢ margins, rebate muted on 100% of
populated cells): the bound pin, the thin-cell pool-upper pin, the 3-row-pool
non-zero pin. No test was weakened: every replaced assertion is an equality on
the new rule plus an explicit inequality against what the old rule would have
published.

## Parity results (rule 8)

- `tools/proto_floor_point.py` (plain arithmetic; imports only `GradeRow`, the
  read-only grade read and `floor_for_cell`): point table == `estimate_retained_floor`
  on **656/656** cells, pool points equal; 0 ≤ point ≤ upper on every cell;
  every non-negative shrunk shortfall → 0; unknown sport → largest pool point.
- Through the live `construct_quote` on every replayed quote (prototype table vs
  live table, same skew/markup/width): **20,000/20,000** (post-8/20) and
  **1,158/1,158** (tonight).
- The prototype's UPPER rule vs build A's `estimate_retained_floor` loaded from
  `main` on the same 3,335 rows: **656/656 cells, pools equal**. And vs the
  WIRE itself: the bot was relit on main at 19:29 ET (`live_20260904_1929.log`)
  and its `retained_floor_estimate` at **23:30:04Z** (re-published identically at
  00:00:12Z) reads rows 3,335 / 656 cells / 450 thin / pool floors mlb 570 /
  soccer 1,382 / esports 1,955 / other 4,945 / floor median 570 / max 366,559 —
  the prototype's upper table reproduces every one of those numbers exactly
  (pinned in `tools/proto_floor_point.py`). The 22:45:44Z line of the 18:45 ET
  boot (3,259 rows, 646 cells, 590 / 1,157 / 2,666 / 4,945) was the grade before
  that boot's stale-row closes landed 76 more settled tickers.

## Gates

| gate | result |
|---|---|
| ruff | clean on every touched file |
| mypy | clean on `risk/retained_edge_floor.py`, `pricing/retained_cell.py`, `tools/proto_floor_point.py`, `tools/diagnostics/fee_floor_counterfactual.py`; `pricing/engine.py` keeps its 4 pre-existing `tuple` type-arg errors (docstring-only change here) |
| full suite (BelowNormal, worktree) | **4,044 passed / 0 failed / 3 deselected** in 366 s (main: 4,041; +3 = the new pins net of the removed quantile tests) |
| vitals fast tier (rule 9) | **NOT RUN on this branch — deferred to the orchestrator post-merge (as build A's report did), for a measured reason (O5):** the run I started at 23:52Z (`VITALS_DATA_DIR` = the live data dir, BelowNormal) spent 1,400 CPU-s in `scan_tape` — the cache manifest includes the CURRENT live log, which grows every second, so on a live machine the fast tier rescans the ENTIRE tape: 274 files / **266 GB** (the docstring says 10 GB); it had read 180 GB by 00:14Z from the same drive the live store writes; the 19:29 ET boot was stall-killed at 00:09:07Z (`supervisor_loop_wedged` maintenance age 61.2 s > 60.5 s — the known saturated-store wall; checkpoint failures and `fill_ledger_write_stalled` predate my scan by 20 min, but the scan's IO cannot be excluded as a contributor) → I stopped the scan at 00:15Z. Process priority does not lower disk IO priority. Run it from a snapshot data dir, or after the manifest excludes the live log (O5) |
| quote-production gate (feedback_validate_caps_quote) | **PASS** — no tier goes to zero under the point rule on either range; zero-skew bids byte-identical |
| throughput | quote path unchanged (same lookups); the slow-loop sweep does less work; live sends/min before/after belongs to the relight window on main (the bot is live on `main`, not on this branch) |

## NEXT STEPS

- Orchestrator: merge `build/floor-point-estimate` → vitals fast + pre-ship →
  relight window: the first maintenance tick must log `retained_floor_estimate`
  with `rule=shrunk_point`, `pool_floor_cc` ≈ {mlb 85, soccer 0, esports 0, other 0}
  and `n_populated_losing`/`n_populated_at_fee` ≈ 88/118; sends/min stays
  300–460; then the rebate should be visible again in `quote_sent` skew on the
  ladders (the 8/16 read: retained edge after rebate by tier, pooled ≥ 2 weeks).
- Operator ratification recorded through the orchestrator: floor = shrunk point
  (a cost estimate), the z ladder stays a TAIL anchor; the pre-publication
  `margin // 2` fallback (O1) is the last hand fraction on this path — persist
  the published table across boots to dissolve it.
- Store rotation P0 (O2/O4): the 5 s sweep wall and the writer's dropped
  `quote_sent` rows are the same saturated-store class; until then the floor
  table refreshes only when a sweep completes (boot did).
- Pre-registered read (≥ 2 weeks, game-clustered, never a P&L refit): per-cell
  shrunk shortfall trajectory — the ML×ML cross-game cell (−1.70 ¢, SE 6 ¢) is
  the one to watch; if it turns, its rebate returns by itself.
- Width follow-up (derived): a per-cell FAIR calibration read for the width seam,
  in probability units — not the shortfall SE (see §Uncertainty and the width).
- O3: a hierarchical variance prior for 2–3-cluster cells, with its derivation,
  before any such cell's floor is trusted at either extreme.
