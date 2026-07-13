# Maker markup mechanism + World-Cup-FAT go-live config

**Date:** 2026-07-13 · **Commit:** `0c4afc6` (mechanism + prod-disarm revert) ·
**Suite:** 1710/0, mypy/ruff clean · **Edge basis:**
`2026-07-13-wc-mlb-markup-regrade.md`.

## What was built (A)

A per-sport maker **markup** (profit margin over fair), the pricer's first — until
now the only margin was the defensive `width`.

- `pricing/markup.py` — `MarkupPolicy` (per-sport markup, DARK unless master +
  sport enabled) + `sport_of` (**FAIL-SAFE**: mixed-sport / unknown legs ⇒
  `other` ⇒ markup 0, so a sport's markup never leaks onto another sport's leg,
  independent of the leg-series allowlist).
- `pricing/quote.py` — `construct_quote` gains `markup_cc`; `margin = max(half,
  markup_cc)` used in the fee range + both raws. **Backward-compatible:
  `markup_cc=0` is BIT-IDENTICAL** to the pre-markup pricer (parity-proven).
- `pricing/engine.py` — builds the policy from `config.markup`; per-combo
  `(sport, markup_cc)` from legs; passes `markup_cc` into `construct_quote`.
- `config` — `SportMarkupConfig` + `MarkupConfig` (dark by default). Toggleable
  per sport + master switch; adaptive markup slots in behind the same seam later.
- Tests: `test_markup.py` + `TestMarkup` (parity, floor no-op, monotone, decline,
  gating, fail-safe mixed-sport).

**Note on effect:** `margin = max(width, markup)` means the markup is a *floor* —
it binds on tight combos (few legs, low uncertainty) and is subsumed by the
already-wider `width` on many-leg combos (whose width proxies FAT-ness). So the
book quotes WC combos at fair + **at least** the markup, more where width demands.

## Adversarial review (independent agent, live-money pricing)

**Clean — no correctness/safety bugs.** Verified: parity bit-identical; free-money
caps still bind after the wider margin; maker-favorable snap-down preserved; sell-
only unaffected; fee estimate never under-charges; monotone (97 fairs × 100
markups, 0 violations); clean decline; no circular import. One **latent** finding
fixed before commit: `sport_of` originally let a mixed KXWC+KXMLB combo inherit the
soccer markup — safe today (allowlist declines it) but a foot-gun on a future
allowlist widen → now tags `other` (markup 0). Full verdict in the commit body.

## Two safety catches (source-of-truth pass, operator-prompted)

1. **Committed prod.yaml must ship DISARMED (test-enforced).** An earlier commit
   flipped `prod_limits_configured: true` in the repo file — reverted. Two guard
   tests (`test_repo_config_files_load`, `test_prod_quote_with_flag_still_blocked_
   by_limits`) enforce that a fresh checkout can never trade. **Arming is via a
   LOCAL, gitignored `*.local.yaml`** override only.
2. **Live bot must NOT write the read-only shadow DB.** `db_name_for(prod)`
   defaults to `combomaker-prod.sqlite3` — the README standing-constraint read-only
   recording. The live config sets `observe.db_filename:
   combomaker-prod-live-wc.sqlite3` so the live store is isolated; `data_dir` stays
   `data` so the auto-launched supervisor still shares heartbeat/KILL/reconcile.

## The armed config (`config/prod-live-wc.local.yaml`, gitignored)

World Cup ONLY (`allowed_leg_series_prefixes: [KXWC]`), soccer markup **3¢**
(`markup_cc: 300`) enabled, MLB off, `prod_limits_configured: true`, sell-only,
caps ENFORCED (`caps_shadow_mode` False). Static guard PASS.

**3¢ rationale:** inside the validated +EV band (2.2–8¢); chosen over 4¢ to win
more of the **closing** WC window — competitive on ~77% of FAT flow (vs ~59% at
4¢), still day-clustered CI5 +2.1. Provisional, one-week; the flat markup
self-selects FAT (we win only room ≥ 3¢). 4¢ = `markup_cc 400`.

**Arm:**
```
combomaker run --env prod --mode quote --confirm-live --config config/prod-live-wc.local.yaml
```
Kill: `combomaker halt` · panic: `combomaker cancel-all --env prod`.

## NEXT STEPS

- **Owner: bot.** Launch + supervise the WC-FAT run (operator asked: show fills
  instantly + a rolling book). Expect SPARSE/zero fills — 3 WC games left; the win
  is proving the machinery on real WC settlements with a validated edge.
- **Owner: operator.** Confirm the run host (supervised in-session now; a durable
  server for any multi-day extension). Deposit already confirmed funded.
- **Owner: bot (deferred).** The explicit FAT/NORMAL room predictor (per-tier
  markup + toggles) once weeks pool; adaptive markup behind the same `MarkupPolicy`
  seam; re-price the graded universe with the live engine to de-confound MLB.
- **Owner: measurement.** Pooled multi-week markup = the real profitability gate;
  never refit on a single P&L window.
