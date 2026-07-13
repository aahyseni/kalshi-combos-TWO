# Risk engine PHASE 4 — portfolio MC + challenger/stress overlay

**Date:** 2026-07-13. **Branch:** `risk-phase4` (based on `main`@`c8f6546`).
**Scope:** RISK_BUILD_PLAN Phase 4 — wire the portfolio MC (game-keyed block
copula + the NO-side correlation fix) into VaR/CVaR/P(ruin)/per-game+per-leg tail
attribution, ADD a challenger/stress overlay (operative ES = max of copula ES,
challenger ES, deterministic stress), and feed the tail number into the risk caps
(SHADOW, off the hot path). Built to the research design docs `M1_mc_portfolio.md`
+ `M2_mc_methodology.md`.

**Suite: 1427/0 → 1462/0 (+35, 0 failed), 3 deselected.** (+15 book-model /
+13 book-risk / +7 portfolio-CVaR cap.) mypy strict + ruff clean on every touched
source file (`files = ["src/combomaker"]`; tests out of mypy scope by config, as
before). Behaviour unchanged in Phase-2 SHADOW mode — the new CVaR cap carries
`shadow=caps_shadow_mode` (default True = log-only), so no quote/confirm/halt
behaviour changes until the operator flips `risk.caps_shadow_mode: false`.

## The inconsistency this closes (one picture)

```
  BEFORE (ops/report._portfolio_mc, the ONLY live MC caller):
     corr = np.eye(n)                    ← pure INDEPENDENCE (blind to the
     NO leg → ~ticker pseudo-leg, p=1-p    one thing that ruptures a NO-seller:
                (independent column)        many shared games breaking together)
     missing marginal → p=0.5 fed to stats (UNKNOWN-is-never-safe VIOLATION)

  AFTER (Phase 4):
     build_book_model → block-diagonal by GAME (cross-game 0, within-game typed)
     NO leg → per-position leg_sides flip (1 − value): correlation PRESERVED
     missing marginal → unknown=True, snapshot NO-GO (fail-closed)
```

The risk sim now samples from the **same joint the pricer priced against** — the
risk view and the fair we quoted agree (parity-gated). This is the F8 audit-row
flip: "independence corr, approximation" → "pricing copula, parity-checked".

## What Phase 4 adds

```
  LIVE BOOK                          PRICING JOINT (imported, unchanged)
  exposure.positions ─┐             copula.build_block_corr  (per-game blocks)
                      ▼             copula.gaussian_copula_joint_prob (parity anchor)
   sim/book_model.py  build_book_model ◄─── block-diagonal global corr (point/low/high),
   (NEW, pure)         │                    YES-marginal legs, NO = per-position leg_sides flip
                       ▼  (legs, corr[point/low/high], positions)
   sim/book_risk.py  compute_book_risk ──► BookRiskSnapshot:
   (NEW)               │    §4.1 EV ± MC stderr, std, P(profit)
                       │    §4.2 VaR/CVaR_0.99 at the corr-HIGH band (gating)
                       │    §4.3 P(loss > 10/25/60% bankroll)  (ruin proxy)
                       │    §4.4 per-GAME + per-LEG tail attribution (Σ = CVaR)
                       │    §5   challenger ES (corr-inflated) + deterministic all-hit stress
                       ▼         → operative_es_99 = max(copula, challenger, stress)
   risk/limits.py    SKIP_PORTFOLIO_CVAR cap (SHADOW) reads the snapshot, %-of-bankroll
   ops/report.py     _portfolio_mc REPLACED by build_book_model + compute_book_risk
```

### 1. `sim/engine.py` — the ~2-line NO-side fix (prototyped → ported → parity)
`ComboPosition` gains an optional `leg_sides: tuple["yes"|"no", ...]`. In
`_position_pnl`, a NO-selected leg contributes `1 − value` to the payout product
(vectorized `np.where`), keeping its within-game correlation (the sampled column
already carries the copula dependence). This is the copula's latent-sign-flip
expressed on the sampled value (`1 − 1[Z≤t] = 1[−Z ≤ −t]`) — algebraically
identical for binary legs, correct for graded settlement legs. **Default `None`
⇒ every leg YES ⇒ byte-for-byte the historical behaviour** (regression test:
`leg_sides=("yes",…)` reproduces `None` sample-for-sample). Also two new public
aliases `position_pnl` / `book_pnl` so the tail-attribution layer reuses the exact
payout/fee/side math (no reimplementation, hard rule 8).

### 2. `sim/book_model.py` (NEW, pure) — the pricer-consistent bridge
`build_book_model(positions, *, marginals, within_game_rho, cross_event_rho,
flat_band)` → `BookModel(legs, positions, corr_point/low/high, leg_index,
event_by_index, unknown)`.
- **Block-diagonal by GAME**: legs grouped on `pricing.grouping.game_key` (the
  exact key the copula correlates on + the exposure book aggregates on, B2);
  within-game pairs get the injected `within_game_rho` (the app wires this to the
  SHIPPED `SgpParams`/config rho — this module never hardcodes a correlation),
  cross-game pairs sit at `cross_event_rho` (≈0). The global matrix is assembled
  by the pricer's own `copula.build_block_corr` (reused, not reimplemented). A
  game block with >2 legs uses the conservative representative rho per band
  (high = max, low = min) so the reported tail never understates.
- **Three bands** (point/low/high) so risk gates at `high` — correlation
  uncertainty widens risk, never hides it.
- **NO handled per position** via `leg_sides`, never a pseudo-leg.
- **Fail-closed**: a missing marginal ⇒ `unknown=True` (a 0.5 placeholder ONLY so
  the matrix is valid; `unknown` forbids using any stat — the opposite of the old
  report, which fed 0.5 into the stats).
- **Ungamed leg** (no event_ticker) keys on itself and never correlates.

### 3. `sim/book_risk.py` (NEW) — full MC + the five outputs + the overlay
`compute_book_risk(model, *, n_samples, seed, band="high", bankroll_cc,
ruin_fractions, challenger_inflation)` → `BookRiskSnapshot`. Off the hot path.
- **§4.1–4.3**: EV ± MC stderr, std, P(profit), VaR/CVaR_0.99 (engine-consistent
  definition), P(loss > frac·bankroll) at 10/25/60% (the ruin proxy).
- **§4.4 tail attribution** (the one genuinely new computation): for the 0.99
  tail set, `contrib_game = −E[Σ position_pnl touching game | tail]`, grouped by
  `game_key`; **Σ per-game = CVaR** (additive; test asserts it reconciles). Plus
  a per-leg attribution.
- **§5 challenger/stress overlay** — the anti-monoculture layer:
  - **challenger** = a correlation-INFLATED re-sample (`rho' = rho + inflation·
    (1−rho)`, default 0.5 toward comonotone) on an independent seeded stream; for
    a correlated NO-seller book this FATTENS the joint-hit tail, so a correlation
    UNDER-estimate is caught (test: challenger ES ≥ production ES).
  - **deterministic stress** = the EXACT all-hit worst case (every parlay hits at
    once → Σ premium+fee), closed-form, an unconditional upper bound.
  - **`operative_es_99 = max(copula ES at `high`, challenger ES, stress)`** — the
    single number the caps/halts consume. A correlation error is never approved
    twice by a monoculture of the pricer.
- **Determinism**: explicit `seed` per call → reproducible CVaR/operative ES.
- **UNKNOWN/empty** ⇒ `usable=False`, no stats (fail-closed).

### 4. `risk/limits.py` — the portfolio-CVaR cap (SHADOW)
New `SKIP_PORTFOLIO_CVAR` reason. `LimitChecker.check` gains an optional
`book_risk: PortfolioRisk` (a structural Protocol — `limits` never imports
`sim.book_risk`, avoiding the cycle). The cap reads the LATEST full-MC snapshot's
`operative_es_99_cc` vs `portfolio_cvar_frac × bankroll` (integer-exact,
`threshold_cc`) — **it never re-runs MC in `check`** (kept cheap + pure). An
unusable snapshot fails closed; `None` (no MC yet) simply skips the cap. Every
breach carries `shadow=caps_shadow_mode` → Phase-4 log-only. Config field
`portfolio_cvar_frac` (default **"0.15"** = 15% of bankroll) validated in (0,1]
like the other cap fracs, wired through `RiskConfig.to_risk_limits`.

### 5. `ops/report.py` — retire the independence bug
`_portfolio_mc` now calls `build_book_model` + `compute_book_risk` at the `high`
band and reports EV ± stderr, VaR/CVaR_0.99, the challenger/stress/operative ES,
per-game tail, and P(ruin) — replacing the `np.eye` + pseudo-leg block. The
UNKNOWN path now surfaces `usable=False` instead of a silent 0.5.

## The parity gate (M1 §1, mandatory before trusting the port)

`test_sim_book_model::TestBuildBookModel`:
- **`test_parity_single_combo_reproduces_copula_joint`** — a 1-position YES book
  of a same-game 2-leg combo (p=0.6/0.45, ρ=0.5), built through
  `build_book_model` + `simulate`, reproduces `gaussian_copula_joint_prob` as the
  MC hit rate to 4·SE. Proves the risk sim and the pricer share a joint.
- **`test_parity_no_combo_reproduces_complement_joint`** — the sell-only mirror: a
  NO-side combo's MC payout = `1 − copula_joint` to 4·SE.
- **`test_no_no_pair_preserves_correlation`** — THE M1 fix: two NO legs in a
  comonotone game show P(both NO)=0.5 (min complement), NOT the independent 0.25
  the old pseudo-leg path produced.

## Design decisions (not pre-specified)

1. **Within-game rho is INJECTED, not hardcoded.** `build_book_model` takes a
   `within_game_rho` provider so the SHIPPED `SgpParams`/config is the single
   source of the correlation (hard rule 8). The default flat band
   `(-0.20, 0.10, 0.40)` (a positive point mean, low reaching ≤0) mirrors the
   pricer's fail-safe: correlation uncertainty widens risk. **Deferred wiring:** a
   provider that reads the exact per-pair `build_sgp_correlation` block for a
   specific combo is a follow-on; the whole-book view uses one conservative
   constant per game (tractable + never understating).
2. **A single conservative rho per game block** (high=max pair rho, low=min) —
   `build_block_corr` sets one pairwise-constant rho per block; for a >2-leg game
   the max-positive (high) / min (low) is the conservative representative. The
   pricer's own per-combo matrix remains the exact object for pricing a specific
   combo; this is the WHOLE-BOOK risk aggregate.
3. **CVaR cap reads a snapshot, never re-runs MC in `check`** (M1 §5-R1). Keeps
   `check` cheap + pure; the full MC runs off the hot path (maintenance tier). A
   stale/UNKNOWN snapshot fails closed.
4. **Structural `PortfolioRisk` Protocol** in `limits.py` (not an import of
   `BookRiskSnapshot`) to avoid the `limits ← book_risk ← book_model ← exposure`
   cycle. The cap reads only `usable` + `operative_es_99_cc`.
5. **operative ES = max of three** (M1/plan §5). The `max` guarantees the gating
   number dominates the exact deterministic stress regardless of sampling noise —
   a hard floor under the tail estimate.

## What is WIRED vs deferred

**WIRED (this pass):**
- `sim/engine.py` `leg_sides` NO-side fix + `position_pnl`/`book_pnl` public
  aliases (prototyped in tests → parity-checked → the fix IS the port).
- `sim/book_model.py` (pricer-consistent block-diagonal bridge).
- `sim/book_risk.py` (full MC, five outputs, tail attribution, challenger/stress
  overlay, operative ES).
- `risk/limits.py` `SKIP_PORTFOLIO_CVAR` cap (SHADOW) + `portfolio_cvar_frac`
  config; `ops/report.py` independence bug retired.

**Deferred (genuinely out of Phase-4 scope; noted, matches M2's own staging):**
- **Glasserman–Li importance sampling for the ruin tail** (M2 §3, ranked #1). The
  naive tail is noisy at p≈1e-3; IS is the fix, but M2 mandates shipping it BEHIND
  a naive-vs-IS agreement band + the Kupiec/Christoffersen coverage backtest — "a
  precisely-estimated wrong number is still wrong." The block-diagonal factor
  structure this pass builds is the exact prerequisite; IS lands after the
  coverage backtest.
- **The maintenance-tick loop that recomputes the snapshot** + the fast quote-time
  `marginal_impact` ΔCVaR → `inventory_skew_cc` (M1 §3, §4.5, R3). The seams are
  in place (`marginal_impact` is CRN-ready; the skew field exists); wiring the
  background cadence + skew mapping is Phase 5 (quoting policy).
- **VaR coverage backtest against settlement** (M2 §5.3, the settlement-graded
  ruler) — a validation report, runs once weeks of game-clustered settlements
  accumulate.
- **Control variate / antithetic / QMC** mean-variance reduction (M2 §2) — the
  mean is already fine at 1e5 paths; these are efficiency multipliers, not
  correctness.

## NEXT STEPS

- **Owner: adversarial judge** — review this delta: the parity gate (does the MC
  really reproduce the pricer's joint?), the NO-side flip correctness (correlation
  preserved, not an independent complement), the tail-attribution additivity
  (Σ per-game = CVaR), the challenger direction (does inflating rho actually fatten
  a NO-seller's tail?), the operative-ES max, the SHADOW-safety of the CVaR cap,
  and the block-representative-rho conservatism. On PASS the orchestrator merges
  `risk-phase4` → main → pushes.
- **Owner: eng (next pass)** — Phase 5 (skew from ΔCVaR + widen-vs-decline +
  pregame precision); the maintenance-tick snapshot loop; then the Glasserman–Li
  IS tail (behind the naive-vs-IS + coverage-backtest gates).
- **Owner: operator** — no new ENFORCED behaviour this phase (CVaR cap is SHADOW).
  Before enforcing, confirm `portfolio_cvar_frac` (15% start), the challenger
  inflation (0.5), and the `high`-band gating choice after reviewing real
  shadow-log operative-ES values.
