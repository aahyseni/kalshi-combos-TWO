# Peak-concentration PRICING steer (leg-cluster / P(book) coin-toss fix) — BUILT

**Date:** 2026-07-18 (evening, operator directive)
**Status:** BUILT + tested (36 new tests; ruff + mypy --strict clean on touched
files); LIVE at next bot restart (rides the ARMED skew seam; `peak_enabled`
defaults True). No config file touched; no restart performed.

## Problem

Sell-only one-way demand built a FRAENG book where 2-3 correlated legs cluster
on one scoreline (FRA-win 27 combos/$189.77, Mbappé 1+ 18/$112.33, BTTS-yes
9/$83.92 — all hitting together on "FRA wins ~2-1, Mbappé scores, both score").
Book MC: P(book profits) = 52.47% — a coin toss — while EV is positive and ruin
~0. Operator constraint: fix it as a PRICING mechanism only — no new caps, no
new skip reasons, zero quoting-throughput impact, nothing book-static.

## What shipped

```
                     OFF HOT PATH (maintenance tick,
                     position-generation change ONLY)
┌──────────────────┐   ┌──────────────────────────────┐
│ ExposureBook     │──▶│ build_peak_profile           │  sim/peak_profile.py
│ committed        │   │  DC state enumeration of the │  (reuses state_worst_
│ positions only   │   │  COMMITTED book, full signed │   case's settlement /
└──────────────────┘   │  netting → top-K loss states │   loss machinery
                       │  per game, generation-stamped│   verbatim, rule 8c)
                       └──────────────┬───────────────┘
                                      │ PeakProfile (cached rows)
                 HOT PATH             ▼
┌──────────────┐   ┌──────────────────────────────────┐   ┌────────────────┐
│ RFQ candidate│──▶│ compute_inventory_skew           │──▶│ pricer no_bid  │
│ (legs)       │   │  directional classifier (as-is)  │   │ (same seam,    │
└──────────────┘   │  + _peak_component:              │   │  same clamp    │
                   │    containment vs ≤K cached rows │   │  discipline)   │
                   │    O(K × legs), no MC, no enum   │   └────────────────┘
                   └──────────────────────────────────┘
```

- **Profile** (`sim/peak_profile.py`, NEW): per game, the top-K
  (`skew.peak_topk_states`, default 5) worst-loss scorelines of the COMMITTED
  book with their loss levels, via the exact waiver enumeration seams
  (`_settle_specs`/`_entity_loss_matrix`/`_selected_possible` — never
  re-derived). Committed only: no resting quotes/reservations, so quote churn
  never repaints the peak. Generation-stamped like `BookRiskSnapshot`
  (read gen → build → publish; stale ⇒ NEUTRAL).
- **Quote-time containment** (`evaluate_peak_containment`): does the candidate
  parlay still HIT in the cached states? One vectorised indicator per
  structural leg over ≤K cached rows; non-structural legs adversarial
  (assumed hit, exactly the waiver rule). Any doubt ⇒ `None` ⇒ zero adder.
- **Pricing** (`risk/skew.py` `_peak_component`, additive on the classifier):

  ```
  peak_overlap = min(1, cand_premium/budget) × hit_severity
  peak_ratio   = min(1, game_peak_loss/budget)      budget = max_event_worst_case_loss
  HIT  ⇒ + peak_widen_max_cc  × peak_overlap × peak_ratio**gamma   (γ=2 convex)
  MISS-ALL (provable) ⇒ − peak_tighten_max_cc × min(1,prem/budget) × peak_ratio
  ```

  `hit_severity` = (loss of the worst cached state the candidate hits) /
  (game's worst cached loss), losses clamped ≥0. Summed over the candidate's
  games, clamped to `[−peak_tighten_max_cc, +peak_widen_max_cc]`, then ADDED to
  the independently-clamped directional skew ⇒ composed classifier bounded by
  `[−300cc, +1200cc]` at defaults (documented on `SkewParams`). Peak rows never
  feed `per_game`, so widen-vs-decline can NEVER decline on peak — pricing only
  by construction.
- **Config** (`ops/config.py` SkewConfig): `peak_enabled` (True),
  `peak_widen_max_cc` (600), `peak_tighten_max_cc` (150), `peak_topk_states`
  (5, validated 1..64). Wired in `quote_app` (SkewParams + LifecycleConfig).
- **Fail-safe everywhere:** no profile / stale generation / no structural plan /
  unparseable or unknown-side legs / half-leg vs non-halves profile / zero
  budget / enumeration error ⇒ hard ZERO adder, debug-logged reason. UNKNOWN
  can never widen, let alone block.
- **Logging:** `inventory_skew_shadow` info event gains one int (`peak_cc`);
  full per-game decomposition (`adder_cc`, `peak_overlap`, reason) at DEBUG
  (`peak_concentration_detail`); profile builds log `peak_profile_snapshot`.

## Measured costs

| Path | Cost |
|------|------|
| Profile build (27 positions / 3 games, K=5) | 12.4 ms — maintenance tick, only on position-generation change (fills/settlements) |
| `compute_inventory_skew` WITHOUT profile | 0.011 ms median |
| `compute_inventory_skew` WITH profile (3-leg, 2 profiled games) | 0.066 ms median, 0.12 ms p95 |
| **Added per-quote cost** | **≈0.055 ms** (target: well under 1 ms) ✓ |

## Tests (tests/test_peak_concentration_skew.py — 36 new, all green)

Stacker widened + exact formula values + monotone in peak/budget ratio +
severity tiers (small-grid two-tier book: 1.0 vs 0.375); anti-peak rebate +
Advance-mutex opposite-side rebate; neutral/unknown ×7 (no-profile
byte-identity, foreign game, corners-only, unknown side, half-leg, disabled,
zero budget); clamps incl. composition at both extremes (+1200 / −300 exact)
and peak-never-declines (widen policy); empty/tiny book; generation cache
(stale/unstamped/None/rebuilt); 250-example property sweep (composed clamp
always holds, applied = −skew); e2e through the REAL `construct_quote`
(stacker prices strictly higher implied YES ask than fresh book, peak
component alone moves the quote, anti-peak tighter, max composed widen still
quotes); builder anchors (top-loss == certified `state_worst_case_by_game` to
the cent, top-K counts, 1586 branch-doubled advance states, uncertifiable game
omitted, containment direct).

Suite: existing skew/worst-case/quote/wiring tests 127/127; full suite run
recorded below. ruff + mypy --strict clean on all touched files.

## Files changed

| File | Change |
|------|--------|
| `src/combomaker/sim/peak_profile.py` | NEW — profile build + containment |
| `src/combomaker/risk/skew.py` | peak params/fields + `_peak_component` + composition |
| `src/combomaker/ops/config.py` | SkewConfig peak knobs + validators |
| `src/combomaker/ops/quote_app.py` | SkewParams/LifecycleConfig wiring |
| `src/combomaker/rfq/lifecycle.py` | profile cache + rebuild-on-generation + quote-path read + logs |
| `tests/test_peak_concentration_skew.py` | NEW — 36 tests |

## Risks / notes

- Magnitudes are deliberately proportional (candidate premium vs game budget):
  a typical $3-premium combo on a 60%-of-budget peak pays ~13cc (0.13c) extra;
  the aggregate repricing of a 27-combo cluster is what moves P(book), not any
  single quote. If live shadow shows the steer too small, raise
  `peak_widen_max_cc` (knob, no code).
- Composed max widen is 12c: on a no_bid below ~12c (fair ≳ 88c parlay) the
  grid CAN round the bid away — identical in kind to the pre-existing armed
  directional skew, but the composed −12c widen DOUBLES that rounded-away
  no_bid zone vs the directional-only 6c (verifier MINOR, documented — no code
  change); bounded and rare on parlay flow; the property suite pins the clamp,
  the e2e pins quote-survival at fair 0.30.
- Rebate fires only on PROVABLE miss of the ENTIRE top-loss plateau (see
  addendum below); with K larger than a game's loss-carrying plateau the cache
  includes non-loss rows and an anti-peak candidate grades "neutral" instead
  (never an uncertified rebate).
- Profile build runs inline on the maintenance tick (ms-scale, committed-only);
  if books grow 10×, move it behind `BookRiskPool` like the MC (seam ready).

## ADDENDUM (same day) — adversarial-verify SERIOUS fix: plateau rebate certification

Verifier probe (confirmed on the live one-way Advance book shape): the K=5
cache is a SAMPLE; every ~793 ARG-advance state ties at the identical loss,
argsort ties break by enumeration index, and all 5 cached rows land on
"ENG 0 – ARG 1..5". A plateau-STACKING refinement (NO {ARG-adv & over 5.5},
NO {ARG-adv & BTTS}) provably missed those 5 rows, certified
`provably_misses_all`, and collected the −45cc rebate while RAISING the
certified worst case by its full premium (probe: directional +32, peak −45 =
net −13 TIGHTEN on a concentrator).

**Fix:** the rebate now certifies a miss of the ENTIRE top-loss level.
`GamePeakProfile.plateau_slices` caches the FULL argmax plateau (every state
at exactly `top_loss_cc`, exact int equality, grouped by shootout branch),
bounded by `_PLATEAU_CACHE_MAX_STATES = 4096` — a larger plateau is not
cached and the rebate simply never fires (fail-safe NEUTRAL, never a rebate
the quote path can't verify). `evaluate_peak_containment` runs the plateau
walk ONLY when the candidate already missed every cached row (the rebate's
only reachable path), so the widen/stacker path is byte-identical and pays
zero extra cost. Measured: widen path 0.038 ms (unchanged); plateau-walking
paths 0.05–0.10 ms median (793-state plateau) — still ≥10× under the 1 ms
budget. Build with plateau: 2.6 ms/game. 5 new tests (verifier probes a/b,
true-opposite rebate retention, mixed plateau+shoulder, and the property
"rebate ⇒ zero loss added in every top-plateau state" asserted against the
full enumeration); suite green.

Residual (documented, per the verifier's prescription): the certification
covers the TOP loss level exactly. A rebated candidate may still lift an
UNCACHED strictly-lower shoulder state (missing the shoulders is deliberately
NOT required — verifier test (d)); the lift is bounded by shoulder + premium,
i.e. it starts strictly below the plateau the book already carries. A cached
shoulder hit grades widen via severity instead (never rebate at severity > 0).

## NEXT STEPS

- **Bot restart** (operator-owned) to activate — rides the armed skew seam;
  watch `peak_profile_snapshot` + `peak_cc` in `inventory_skew_shadow`, and
  P(book profits) on the FRAENG-shaped book vs the 52.47% baseline.
- **Live shadow read** (next session): grep `peak_concentration_detail` at
  DEBUG for a day; verify hit/rebate mix and that adders are economically
  visible on the cluster flow; tune `peak_widen_max_cc` only from that
  measurement (never a P&L window).
- **Decision owed (operator):** whether the rebate side should also count
  toward the certified-hedge review pipeline (currently independent).
