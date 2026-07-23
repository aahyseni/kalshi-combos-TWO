# Adaptive-caps wiring → arm → go-live (2026-07-22)

Path B: wire the correlation-adaptive cap BRAIN into live enforcement, arm it,
relight. This is the North Star cap system (`CLAUDE.md` ⭐) going live in its
**bootstrap regime** — the deploy + halt caps are derived, not hand-set; the
measured-regime breathing activates as the P&L sensor accumulates real MLB nights.

## What shipped (5 modules + the quote-app hook)

| Layer | Module | Role | Tests |
|-------|--------|------|-------|
| Brain | `risk/cap_family.py` | caps = f(σ₁, G_eff, bankroll, z-anchors); f_slate pins σ_day/bank = 0.024 | 20 |
| Brain | `risk/pnl_measurement.py` | sensor: σ₁, cross-game ρ → G_eff, `stable` gate | 8 |
| Brain | `risk/adaptive_caps.py` | nightly `compute_nightly_caps` (sensor → formula) | 5 |
| Adapter | `risk/derived_cap_engine.py` | brain → `RiskLimits` (dataclasses.replace); book caps = max(1.3×MC, floor) | 8 |
| Seam | `risk/limits.py` `LimitChecker.set_limits()` | atomic limits swap read by `check()` per-call | (limits suite) |
| Wiring | `ops/quote_app.py` | engine instantiation + synchronous startup derivation + periodic `_adaptive_caps_loop` | 5 |
| Config | `ops/config.py` | `adaptive_caps_mode` (off/shadow/enforce) + `adaptive_caps_expected_games` | — |

## The three modes (isolation by construction)

- **off** (default) — engine not instantiated; the static config fracs enforce,
  **byte-identical to prior behaviour** (proved: loop returns immediately when
  `cap_engine is None`; test `test_off_mode_is_a_noop_engine_none`).
- **shadow** — derives + **logs** the caps beside the enforced static ones; zero
  enforcement change.
- **enforce** — `set_limits(derived)`; the brain owns the caps.

## Why the startup derivation is SYNCHRONOUS

The static caps in the armed config are WC-loose (slate 0.65, game 0.50, per_combo
0.05). If the first derivation were the loop's first async tick, a millisecond
startup window would bind those loose caps. So `run()` calls
`_refresh_adaptive_caps_once` **inline, before the RFQ workers start** — the
derived caps (slate 0.15, game 0.15/expected_games, per_combo 0.01) bind the very
first fill. The loop then only re-derives nightly. **Provably** no loose window.

## Fail-safe

Any error in the refresh keeps the current enforced limits (never widens on a
bug). `CancelledError` (task shutdown) is a `BaseException` → not swallowed by the
`except Exception`. Tests: `test_refresh_error_keeps_current_limits_fail_safe`.

## Bootstrap regime (tonight) vs measured regime (as data lands)

Tonight the sensor has no MLB per-game P&L history, so:
- history empty ⇒ **provisional** caps: slate **0.15**, game **0.15/expected_games**,
  per_combo **0.01**.
- halt anchors (policy, fixed): daily **0.072** (3σ), drawdown **0.096** (4σ),
  hard-trip / KILL **0.12** (5σ).
- book caps at their **bootstrap floor**: directional **0.15**, det_max **0.15**,
  cvar **0.10** (empty-book MC≈0 would be 0 → floor keeps the first fill alive).

**Fast-follows that flip bootstrap → measured** (each only ever lets caps breathe
WIDER, gated by evidence; their absence just holds the safe bootstrap):
1. **Per-game P&L DB reconstruction** → feeds σ₁ / cross-ρ → the formula solves a
   real f_slate (diversified nights earn > 0.15; correlated nights ratchet down). *(pending)*
2. **Projected-book MC hookup** → book caps become 1.3×MC instead of the floor. *(pending)*
3. **expected_games auto-count** → ✅ **DONE** — `_count_slate_games` counts distinct
   `game_key`s across open `<prefix>GAME` markets at startup + nightly; config value
   is fallback-only. Per-game cap divisor is now live-adaptive, not a hand-set number.

## Arm + relight

1. Armed config `config/prod-live-wc.local.yaml` (GITIGNORED — never committed),
   `risk:` block: `adaptive_caps_mode: enforce` + `adaptive_caps_expected_games: 15`.
2. Relight (operator's `--confirm-live` human gate):
   `combomaker run --env prod --mode quote --config config/prod-live-wc.local.yaml --confirm-live`
3. Prod guard prerequisites present: `prod_limits_configured: true`, allowlist
   `[KXMLB]`, MLB markup ladder live.

## Adversarial review → 2 real findings → FIXED

A full adversarial review (7 files, empirically executed) returned **SHIP for
off/shadow, NO-SHIP for enforce** on two findings — both now fixed:

- **H1 (HIGH, real bug):** the derived `daily_loss_frac` (3σ anchor = 0.072) was
  LOOSER than the operator's configured 0.06 — arming enforce would silently
  loosen a HALT/kill threshold on live money (the fail-safe violation the North
  Star forbids).
- **E1 (MED):** a book-cap floor (0.15) could loosen a hand-set directional cap.

**First fix (min(derived, config) clamp) was SUPERSEDED by an operator directive:**
"do not enforce any manual numbers, it should all be adaptive." Clamping to the
static config fractions re-introduced the stale WC-tuned MANUAL numbers as ceilings
— the exact thing the North Star forbids. So the clamp was reverted and the engine
made FULLY ADAPTIVE:

- Every enforced cap comes ONLY from layer-1 (measured: sigma1, cross-rho→G_eff,
  1.3×MC) + layer-2 (policy anchors: KILL 12%=5σ → daily 3σ / dd 4σ / trip 5σ;
  per-combo 1%). No static config fraction governs — `dataclasses.replace`
  overrides all nine axes; the config values survive only as the off/fail-safe
  fallback. Proved by `test_no_static_config_number_governs_base_independent`
  (arming the same slate against a tight base and a wide base → identical caps).
- **H1 resolved by direction, not clamp:** the daily halt is now its policy anchor
  0.072 (from your 5σ KILL), superseding the stale manual 0.06 — which is what your
  formal spec set (daily 3σ). Not a silent loosen: the bot is DOWN (cold start),
  the value is documented, and it's the operator's stated appetite.
- **E1 resolved by making floors adaptive:** the book-cap bootstrap floors are no
  longer constants (0.15/0.10) — directional/det_max floor = the derived slate
  budget, cvar floor = the 4σ drawdown anchor. No hand-set floor survives.

Also reviewed and PASSED: isolation (off = byte-identical no-op), fail-safe (error
keeps limits; CancelledError not swallowed), enforce swap (atomic, exact
Fractions), the math (anchors invariant, ratchet correct, no div-by-zero), the
config seam, provisional/bootstrap.

## Enforced-vs-config caps against the real armed config ($2k, bootstrap tonight)

| axis | config (fallback) | enforced (adaptive) | $@2k | source |
|------|-------:|---------:|-----:|--------|
| per_combo | 0.0500 | 0.0100 | $20 | anchor 1% |
| game | 0.5000 | 0.0100 | $20 | slate / expected_games |
| slate | 0.6500 | 0.1500 | $300 | measured (provisional clamp) |
| daily (halt) | 0.0600 | **0.0720** | $144 | **3σ anchor (supersedes manual 0.06)** |
| drawdown (halt) | 0.1000 | 0.0960 | $192 | 4σ anchor |
| hard_trip / KILL | 0.1200 | 0.1200 | $240 | 5σ anchor |
| directional | 0.4000 | 0.1500 | $300 | floor = slate (→1.3×MC) |
| det_max | 0.3600 | 0.1500 | $300 | floor = slate (→1.3×MC) |
| cvar | 0.3500 | 0.0960 | $192 | floor = drawdown (→1.3×MC) |

The static config column is now just the off/fail-safe fallback — **none of it
governs under enforce**. `daily` is the one axis that ends up looser than the old
manual 0.06, because the adaptive 3σ anchor (0.072, from your 12%=5σ KILL) replaces
it — intended per "all adaptive." For MLB, `enforce` is SAFER than `shadow`: shadow
would leave the WC-tuned static caps (per_combo $100, slate $1,300) live, far too
loose for a $2k MLB slate.

## Verification

- Full suite: **2661 passed, 0 failed** (post fully-adaptive rewrite).
- Adapter + wiring: **15 passed**; ruff + mypy clean on `derived_cap_engine.py`
  + `quote_app.py`. Provenance audit: all 9 enforced axes sourced from a measured
  quantity or a policy anchor (no manual risk number).
- Armed config `adaptive_caps_mode: enforce` + `expected_games: 15` — loads,
  reads enforce, gitignored (verified `git check-ignore`).

## NEXT STEPS

- **Owner: bot** — after suite + review clean: apply the enforce config, relight,
  watch first slate (adaptive_caps_refresh log line, quotes/min throughput vs WC,
  fills, declines, halts).
- **Owner: bot (fast-follow #1)** — per-game P&L DB reconstruction → activate the
  measured regime (biggest lever; flips provisional 0.15 to earned/ratcheted).
- **Owner: bot (fast-follow #2/#3)** — projected-book MC → 1.3×MC book caps;
  expected_games auto-count.
- **Decision owed: operator** — confirm `expected_games: 15` for MLB (or set the
  real full-slate count); confirm enforce-from-night-one vs one shadow slate first.
