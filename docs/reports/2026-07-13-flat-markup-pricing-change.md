# Flat maker model: quote = fair + markup (2026-07-13)

Operator directive: "all other makers take their fair and add a markup. There is no
such thing as uncertainty if our pricing engine is correct… with a 2¢ markup our
expected profit is 2¢. Nothing I see is fair + 2¢, which is how it should be.
Uncertainty should be only for anything CRAZY LONGSHOT."

## The problem the operator caught (via the live viewer)

The engine's spread was `margin = max(defensive_width/2, markup_cc)`, and the
mechanical width (base 200 + per_leg 100×n_legs + uncertainty + size) **dominated**
the 2¢ markup on all but 2-leg combos:

| combo | width_cc components | old margin | operator saw |
|---|---|---|---|
| 2-leg ADV | base200+legs200+unc42+size27 | 2.3¢ | ~fair+2¢ (ok) |
| 5-leg ADV+BTTS+GOAL | base200+legs500+unc298+size69 | **5.3¢** | fair+5¢ — "we'll never fill" |

So the 2¢ markup the operator set **never bound** — the width was the real spread,
and it made us uncompetitive on multi-leg (which is most WC/MLB combo flow).

## Why the operator is right (and the counter I owed)

- The **flat-markup model IS the standard maker model**, and it's exactly what our
  own edge re-grade validated: `2026-07-13-wc-mlb-markup-regrade.md` tested
  `ask = fair + m` (flat) and found +EV from 2.2¢ (CI5 +0.72). It did **not** test
  the width-floor — that was un-validated extra conservatism.
- The one genuinely-real effect (not double-counting) is **adverse selection /
  winner's curse** — but the mechanical base/per-leg width is a fixed 2–6¢, far too
  small to cover the measured 25–35¢ winner's curse anyway; that protection actually
  comes from the **markup + FAT self-selection** (we only fill when clearing ≥
  fair+markup, i.e. the retail-overpays tail) plus the freshness gate + leg-depth
  filters for data quality. Removing the width removes none of those.
- Uncertainty *does* belong on genuinely-hard combos — retained (below).

## The change (config only — no code)

`config/prod-live-wc.local.yaml`, `pricing.quote`:
```yaml
base_width_cc: 0          # was 200
per_leg_width_cc: 0       # was 100  (this was the multi-leg culprit)
size_width_cc_per_100: 0  # was 50
```
KEPT at defaults: `uncertainty_width_scale: 1.0`, `longshot_fair_threshold: 0.15`,
`longshot_min_rel_uncertainty: 0.25`. Now `margin = max(uncertainty/2, markup)`:

| uncertainty | margin | note |
|---|---|---|
| 42 / 109 / 298 cc (normal 2–5 leg) | **2.00¢** flat | markup binds |
| 650 cc (genuinely high correlation uncertainty) | 3.25¢ | widened — "crazy only" |

Truly unpriceable combos are **declined** by `skip_classifier_unknown` /
`skip_unmodeled_regime`, not priced wide. So: **fair + 2¢ flat**, wider only where the
model is genuinely unsure. Live-confirmed post-relaunch: 2-leg quotes 2.05–2.09¢ (2¢
+ fee), vs 2.4/5.3¢ before.

## Honest caveat

2¢ is at/just-below the re-grade's 2.2¢ robustly-+EV floor, and this removes a
conservatism buffer. Net effect: **more competitive → should fill more**, and the
fills → settlements will now actually test whether the flat-2¢ maker model holds
(the operator's "go live and see"). Downside still bounded by sell-only + caps +
freshness/depth gates. Not a P&L refit — a structural model change per operator
directive + re-grade evidence.

## NEXT STEPS

1. **Owner: bot.** Watch for first fills under flat 2¢ + confirm they reconcile and
   markout/settle sanely (the flat-model test).
2. **Owner: operator (decisions owed).** (a) shadow recorder restart? (b) same-game
   gate (target the +EV pond)?
3. **Owner: measurement.** If flat 2¢ fills, grade the fills vs settlement — that's
   the real read on the flat-markup model (pooled, never a single-window refit).
