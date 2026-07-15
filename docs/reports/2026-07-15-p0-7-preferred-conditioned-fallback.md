# P0-7 upgraded: INTERIM worse-tail challenger → PREFERRED conditioned fallback

**Date:** 2026-07-15 · **Branch:** `risk-audit-overnight` · **Suite:** 2017 passed / 0 failed / 3 deselected (was 2010; +7 new tests)

## What changed

RISK_ENGINE_AUDIT_ACTION_PLAN.txt P0-7 called for the PREFERRED approach —
"condition fallback legs on the structural game state / shared factor" — over the
interim worse-tail-challenger-only implementation. This delivers it.

Previously `sim/structural_book.sample_structural_values` sampled a game's
structural scoreline block and its copula-only corners/cards block from
INDEPENDENT rng, discarding same-game structural↔copula dependence in the
PRODUCTION sample; only a full-copula CHALLENGER (`bridge_es_99_cc`) saw that
coupling. Now, where a DEFENSIBLE measured scoreline-state link exists, the
straddling copula leg is CONDITIONED on the game's sampled scoreline intensity via
a shared factor, so the covariance appears in the production tail.

| Piece | Location | Behavior |
|---|---|---|
| Shared factor | `structural_book._shared_structural_factor` | per-sample standardized total sampled goals (incl ET) → standard-normal latent (empirical-rank PIT) |
| Conditioning blend | `structural_book._sample_copula_conditioned` | `z' = sqrt(1−β²)·z_copula + β·f_game`; preserves marginal + copula-block corr exactly, adds structural-state loading |
| Conservative loading | `book_risk._copula_leg_loading` + `StructuralConfigView.corners_et_loading` (0.10) | nonzero ONLY for a KNOCKOUT total-corners leg (the ET-window channel: `pair_rho advance\|corners` dog +0.23↔fav −0.23, pooled ~0); **0 everywhere else** |
| Wiring | `book_risk._build_conditioning` → `_select_sampler` | maps each straddling copula leg → (plan, β); ungamed/cross-game/group/no-link → not conditioned |
| Worse-of gate | `book_risk` governing max folds an **independent-split guard** ES/ruin | conditioned production tail can only fatten, never thin, below the split |
| Backstop retained | full-copula `bridge_es_99_cc` still runs on straddling games | leg types with no defensible link stay independent + covered by the challenger |
| Config wire | `StructuralConfig.corners_et_loading` → `quote_app` | operator-tunable; default 0.10 |

## Why the loading is deliberately narrow (no fabricated correlation)

Config measurement (n=8,982 matches) is explicit: **TOTAL corners are ⊥ goals /
total / result / margin** ("folk wisdom busted"; `corners|total = 0.00`,
`btts|corners = 0.00`). Coupling total corners to goal-intensity in general would
FABRICATE a link the data rejects. The one defensible, measured, purely
scoreline-STATE-driven channel is `advance|corners` via extra time (corners settle
including ET; a level-after-90 scoreline opens an extra corners window). So the
default loading is a small, width-bearing 0.10 applied ONLY to knockout total
corners; group corners, team corners, and cards get 0 → independence + the
worse-tail challenger, exactly per the spec's "no defensible link ⇒ keep
independence AND retain the challenger."

## Safety / correctness invariants

- **SAFETY DEFAULT honored:** conditioning only ever ADDS tail. The governing model
  ES = `max(conditioned production, correlation challenger, full-copula bridge,
  structural challenger, independent-split guard)`; the split guard guarantees the
  conditioned tail is never reported below the independent split. No cap loosened,
  no decline removed.
- **Fail-closed:** unknown ticker / ungamed / cross-game / disabled-config → loading
  0 (independence), never a fabricated correlation.
- **Marginal preserved to the cent-equivalent** (rank-PIT blend is standard-normal
  preserving) — verified: P(corners)=0.4001 conditioned vs 0.4001 split.
- **Parity (hard rule 8c):** `_sample_copula_conditioned` mirrors
  `engine.sample_leg_values` byte-for-byte when all loadings are 0 (pinned by
  `test_conditioned_copula_sampler_parity_when_all_loadings_zero`); the structural
  block's rng stream is untouched (the factor is a pure function of the already-
  sampled state, computed last). Prototyped in
  `tools/proto_structural_copula_conditioning.py` first, then ported + parity-checked.
- Determinism preserved: a 5th `SeedSequence.spawn` substream (compute_book_risk) /
  4th (candidate evaluator) for the split guard, spawned unconditionally so the
  production/challenger/bridge streams are byte-identical whether or not it fires.

## Tests (`tests/test_structural_conditioning_p0_7.py`, 7 new)

1. covariance appears in the PRODUCTION sample + marginal preserved
2. all-loadings-zero copula parity vs the engine sampler
3. conditioning-off byte-identical to the independent split
4. governing tail ≥ independent split (never thinner)
5. no-defensible-link leg → not conditioned but challenger still active
6. per-leg loading is nonzero only for knockout total corners (cards/team/group/disabled → 0)
7. ungamed copula leg unchanged

Existing P0-7 bridge suite (`test_structural_bridge_p0_7.py`), the
structural-book MC parity gate, tape parity, candidate evaluator, and the P1.9
structural-parameter challenger all still pass unchanged.

## Docs updated

- `structural_book` module docstring + `BookRiskSnapshot.bridge_*` /
  `governing_model_es_99_cc` comments now state P0-7 is the CONDITIONED approach
  where a defensible link exists, with the full-copula bridge as the BACKSTOP for
  the unconditioned (no-link) part.

## NEXT STEPS

- **Owner: model/calibration** — if/when a match-level dataset supports a defensible
  loading for another copula leg type (e.g. a measured cards↔scoreline link, or a
  per-sport corners channel), extend `_copula_leg_loading` (conservative + width) and
  add the covariance-in-production test; until then those legs stay at 0 (challenger).
- **Owner: operator** — `corners_et_loading` is live at 0.10 by default (safe: only
  adds tail). To disable, set it to 0 in `StructuralConfig` (production sample reverts
  to the independent split; the challenger backstop is unaffected). No cap decision owed.
- Nothing to re-launch; no live DB / prod YAML / `.env` touched.
