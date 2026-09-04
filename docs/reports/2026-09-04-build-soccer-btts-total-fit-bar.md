# 2026-09-04 — Build item B: soccer btts|total promote (0.70 → 0.746) + DERIVED structural fit bar + marginal-consistent btts×total hybrid + structural-fit telemetry

Branch `build/soccer-btts-total-fit-bar` (worktree `.claude/worktrees/wf_ed4feb4a-3f5-2`).
Spec: the verified deep dive `explain_soccer-btts-over.txt` (sections A–F, REPAIR DESIGN 1–6, gates G1–G5) with the verifier's corrections (parity 23/26 within 0.01c pre-port; measured bias ≈1c/pair for 0.70→0.746; the 2c figure is model form; fill-cell z-scores overstate — clustered games).
Continued from a previous builder that left only the prototype + a scratch folder uncommitted; everything below was re-derived, finished and committed in this session.

## WRONG / FIXED / OPEN

| # | WRONG (measured) | FIXED (mechanism) | Commit | Status |
|---|---|---|---|---|
| 1 | `pricing.correlation.pair_rho_by_sport.soccer['btts|total'] = 0.70` — the World-Cup club+INTERNATIONAL blend (git 470f24b) — governed live: the copied `config/prod-live-wc.local.yaml` has NO `pricing.correlation` section, so the `config.py` default is the effective value (verified via `load_config`: 0.7). Held-out both-YES cell realized 43.0% vs 40.5% predicted (z +2.19; train z +4.30) → NO bid ~1c too rich on every club btts×total pair on the copula path. | Config default promoted to the CLUB measurement **0.746** (NOTES.md club row +0.75 [.69,.80], n=8,982; `results_soccer.md` +0.746 reproduced to the digit) with the citation comment. Re-validated held-out (rule 8b, table below). **No yaml line needed.** Band `soccer:btts|total` 0.12 stays (why: below). | `7bde8cf` | FIXED |
| 2 | `dixon_coles.invert` REJECTED any exactly-identified fit with residual > **0.005** — a hand-set constant 10× tighter than the over-identified bar (0.05, which DC never enforced at all). A Poisson scoreline cannot reproduce the club btts / over-2.5 shape to better than ~0.6–1.8pp, so **4/4 recorded club pairs** (residuals 0.0062 / 0.0078 / 0.0102 / 0.0164) fell to the copula at the stale 0.70, while the SAME game's triple was accepted at 0.0195. | **ONE hard bar** for every DC system (the pre-existing `REJECT_OVERIDENTIFIED` = 0.05; the 0.005 literal is gone). ACCEPT/CHALLENGE is **derived** in `fit_challenge.classify_fit(resolution=…)`: accept bar = Σ `belief.uncertainty` of the identifying (team-level) legs — the exact quantity `structural._price` already perturbs — floored at the over-identified regime's own accept boundary (`CHALLENGE_FRACTION × REJECT_OVERIDENTIFIED` = 0.025) and capped at the hard bar. Between accept bar and hard bar = CHALLENGE: priced, misfit (residual × `misfit_uncertainty_scale`) already in the width, recorded. Wider books → looser bar. Same rule for over-identified systems (the 0.005-vs-0.05 asymmetry is gone). | `7bde8cf` | FIXED |
| 3 | For the SYMMETRIC btts×total pair (no orienting leg) the DC best-fit lands on balanced lambdas and misses BOTH marginals by 0.6–1.8pp: pricing its cell quotes a joint inconsistent with the leg books. | Marginal-consistent **hybrid**: derive the pair's latent ρ from the DC best-fit (`structural.implied_pair_rho` = the Gaussian-copula ρ reproducing the DC cell at the fitted lambdas; per match 0.67–0.80) and price the MARKET marginals through the copula at that ρ (`structural.hybrid_joint`). Every width perturbation (leg bands, dc_ρ band) re-inverts and re-derives ρ. Applied on every priceable verdict of the symmetric pair (see "Deviations"): at residual 0 it IS the DC cell (to 1e-10); on the four recorded fills the two differ by ≤ 0.06c. | `7bde8cf` | FIXED |
| 4 | Nothing observed the fallback: `structural_fits` had **0 rows ever**, `record_structural_fit` had no caller, `fit_challenge.classify_fit` had zero live callers, the fallback was a note string. | The verdict rides the estimate: `JointEstimate.fit: StructuralFitRecord` (verdict + family + route + reason) → survives the joint memo and the ProcessPool pickle → `ConstructedQuote.structural_fit` / `NoQuote.fit`. The lifecycle records ONCE per priced RFQ right after `_price_async` (quotes, copula fallbacks and tie-guard declines alike): counters `structural.fallback.<verdict>` + `structural.fallback.<verdict>.<family>`, and a `structural_fits` row through the store's non-critical `_write` queue (never blocks pricing). New columns `family / route / reason` with an idempotent `ALTER TABLE` migration in `Store.open`. | `fa3d449` | FIXED |
| 5 | tie×total / tie×btts pickoff guard | **UNCHANGED** — `_same_game_tie_total` untouched; it still declines on a structural REJECT (now carrying the record, route `declined`). Pinned at the engine level in the new tests. | — | PINNED |
| 6 | Floor semantics: at two 1c-spread club books (Σunc = 0.01) the 0.025 floor binds, so on the recorded fills and the emulated held-out market (2-leg residual p90 0.0124, max 0.0154) every fit lands ACCEPT and the CHALLENGE label fires only above 0.025. Pricing is identical either way (hybrid + misfit width apply to every priceable verdict); the label/telemetry is what the floor decides. The previous builder argued for NO floor (label ≈ 10–15% CHALLENGE at Σunc 0.01); the brief asked for the floor. Shipped: floor (the brief's literal reading). | Decision owed: keep the floor (brief) or drop it (label-only change, zero pricing impact) — see NEXT STEPS. | — | OPEN |
| 7 | `margin_total.invert_means` / `mlb_runs.invert_runs` still enforce the legacy fixed bars (`REJECT_EXACT` 0.005 exact / 0.05 over-identified). Out of this item's scope (soccer DC). | — | — | OPEN |
| 8 | Cross-game-only btts×total bucket (−$90.74 on $109.54, 4/6 hits vs 2.0, n=6) priced at `cross_event_rho` 0. | Not touched (spec step 6: pre-register a game-clustered watch, never move the number without a measurement). | — | OPEN |
| 9 | **Found by the full suite:** the ONE hard bar inside `invert` also hit the risk MC's structural-parameter challenger (P1.9), which deliberately SHOCKS the marginals and re-inverts — its shocked targets are meant to be inconsistent, so the bar silently dropped the games the stress fattens (2 challenger tests: no bundle / no tail lift). | `invert(..., contradiction_bar: bool = True)` — a mode, not a number — threaded through `sim.structural_book.build_game_plans`; only `_structural_challenger_bundle` passes `False` (least-squares scoreline of the shocked targets = the adverse scenario; identification/feasibility failures still fail closed). Pricing and the PRODUCTION risk sampler keep the bar (pricing and risk agree on representable games). | `3b3f789` | FIXED |

## What changed — the pipeline after the port

```
RFQ legs ─► classify_legs ─► engine._joint_or_noquote
                                  │
                   structural_applicable? (soccer, ONE game group)
                                  │ yes
                                  ▼
                 StructuralPricer.try_price_with_fit  ──────────────► (est, None, FitRecord)
                   parse ─► invert(...)                                 │
                            │  residual > 0.05 (ONE hard bar) ─► StructuralError(residual=r)
                            ▼                                            │
                   classify_fit(r, resolution = Σ unc(identifying legs))  │
                     ACCEPT  : r ≤ max(Σunc, 0.025)                      │
                     CHALLENGE: max(Σunc,0.025) < r ≤ 0.05                │
                     (REJECT only via the hard bar / non-finite sentinel) │
                            ▼                                            ▼
                   symmetric btts×total?                    (None, reason, FitRecord[REJECT])
                     yes ─► ρ_i = implied_pair_rho(fit)              │
                            p = copula(MARKET marginals, ρ_i)        ├─ tie×total/btts? ─► NoQuote(SKIP_STRUCTURAL_FALLBACK_TIE_TOTAL, fit=declined)
                     no  ─► p = DC cell (unchanged)                  └─ else ─► copula (0.746) + fit=copula
                   width = leg bands + dc_ρ band (+ET/pens) + r·misfit_scale (every verdict)
                            ▼
                   JointEstimate(p, uncertainty, residual=r, fit=FitRecord)
                                  │  (joint memo / ProcessPool: fit pickles with the estimate)
                                  ▼
                 _price_suffix ─► ConstructedQuote(structural_fit=fit) | NoQuote(fit=fit)
                                  │
   lifecycle: result = await _price_async(rfq); await _record_structural_fit(rfq, result)
              metrics.inc("structural.fallback.<verdict>[.<family>]")
              store.record_structural_fit(... family, route, reason)  ──► _write queue (slow path)
```

Files (all on the branch, 4 commits + this report):

| File | Change |
|---|---|
| `src/combomaker/pricing/fit_challenge.py` | `classify_fit(..., resolution=None)`: derived mode (ONE hard bar, `derived_accept_bar`), legacy mode unchanged for MT/MLB; `FitChallenge.resolution`; `StructuralFitRecord` + route constants. |
| `src/combomaker/pricing/dixon_coles.py` | `invert`: `residual > 0.05` for every system (exact 0.005 bar removed; comment cites the measurement); notes always carry the residual; `StructuralError(residual=…)`. |
| `src/combomaker/pricing/structural.py` | `leg_family`, `is_symmetric_btts_total`, `implied_pair_rho`, `hybrid_joint`; `_price`: resolution → verdict, hybrid route for the symmetric pair (ρ re-derived per perturbation), `fit` on the estimate; `try_price_with_fit` (3-tuple), `try_price` unchanged 2-tuple; `_reject_record` (sentinel residual −1.0 for combos that never inverted). |
| `src/combomaker/pricing/joint.py` | `JointEstimate.fit: StructuralFitRecord | None = None`. |
| `src/combomaker/pricing/quote.py` | `NoQuote.fit`, `ConstructedQuote.structural_fit` (defaulted, positional construction unchanged). |
| `src/combomaker/pricing/engine.py` | Both structural branches call `try_price_with_fit`; REJECT → copula estimate carries `route=copula`; tie guard decline carries `route=declined`; band×neighbour decline carries the record; `_price_suffix` copies `joint.fit` onto the result; sell-only decline keeps it. Guard logic untouched. |
| `src/combomaker/ops/config.py` | soccer `btts|total` 0.70 → **0.746** + citation comment. |
| `src/combomaker/sim/structural_book.py`, `src/combomaker/sim/book_risk.py` | `build_game_plans(..., contradiction_bar=True)` pass-through; the P1.9 challenger re-inverts with `contradiction_bar=False` (stress mode). |
| `src/combomaker/ops/persistence.py` | `structural_fits` + `family/route/reason` (DDL + idempotent migration); `record_structural_fit` via `_write`. |
| `src/combomaker/rfq/lifecycle.py` | `_record_structural_fit` (once per RFQ, after `_price_async`). |
| `tools/proto_structural_fit_bar.py` | The rule-8 prototype (replay + fixture writer + held-out backtest incl. live-path stress). |
| `tests/fixtures/structural_fit_bar_contexts26{,_raw}.json` | The 26 recorded fill contexts (read-only store pull) + the PRE-port oracle. |
| `tests/test_structural_fit_bar.py` | 32 new tests (below). |
| `tests/test_{fit_challenge,structural,dixon_coles,sgp,book_risk_wiring,structural_param_challenger_p1_9}.py` | 9 deliberate pin changes, each commented with build + measured reason. |

## Measured evidence

### Where the live value came from
`load_config('config/prod-live-wc.local.yaml').pricing.correlation.pair_rho_by_sport['soccer']['btts|total']` → **0.7** (the yaml top-level `pricing:` carries only `quote:` and `markup:`); global `btts|total` 0.75; band `soccer:btts|total` 0.12; `structural.dc_rho` −0.05 band 0.08; `misfit_uncertainty_scale` 1.0. So the promote is the config default; **no gitignored-yaml line is required**.

### G1 — held-out backtest (`tools/proto_structural_fit_bar.py --backtest`, football-data top-5 EU 2020/21–2024/25, 8,980 games, train < 2024 = 7,228, held-out 2024/25 = 1,752; paired game-level bootstrap 10k)

AS-IS marginals (btts marginal = the 3-constraint model's own P(btts); validates the DC-implied ρ at the TRUE 1X2-identified lambdas):

| model | held-out log-loss/game | both-cell pred vs realized 0.4304 (z) | bootstrap diff vs 0.746 [95% CI] |
|---|---|---|---|
| independence | 1.35245 | 0.2872 (+13.46) | +0.15365 [+0.13060, +0.17749] |
| copula 0.70 (shipped) | 1.20075 | 0.4050 (+2.19) | +0.00195 [−0.00023, +0.00418] |
| **copula 0.746 (promoted)** | **1.19881** | 0.4151 (+1.31) | 0 |
| DC exact | 1.19937 | 0.4164 (+1.20) | +0.00056 [−0.00167, +0.00268] |
| **hybrid (DC-implied ρ)** | **1.19954** | 0.4173 (+1.12) | +0.00074 [−0.00165, +0.00309] |

Per-match DC-implied latent ρ: mean 0.751, p10 0.671, p90 0.797 (train 0.752) — the structure reproduces the pooled 0.746. Live-path agreement: the 2-leg (btts, over) inversion recovers the 3-constraint ρ with |Δρ| mean 0.0018, max 0.0654 (n=1,752).

LIVE-PATH STRESS (market btts emulated = model btts + the Poisson btts gap **measured on TRAIN only**: realized 0.5457 − model 0.5225 = **+0.0231**; this makes the pairs infeasible for the 2-leg fit exactly like the recorded fills, and exercises the LIVE route end to end — 2-leg inversion → derived verdict at Σunc 0.01 → balanced-lambda ρ → hybrid on the shifted market marginals; comparators on the SAME marginals):

| model | held-out log-loss/game | both-cell pred vs realized 0.4306 (z) | bootstrap diff vs 0.746 [95% CI] |
|---|---|---|---|
| independence | 1.35090 | 0.2995 (+12.18) | +0.15384 |
| copula 0.70 | 1.19905 | 0.4167 (+1.19) | +0.00199 [−0.00019, +0.00418] |
| copula 0.746 | 1.19706 | 0.4268 (+0.33) | 0 |
| DC 2-leg best-fit cell | 1.19807 | 0.4323 (−0.15) | +0.00101 [−0.00153, +0.00356] |
| **hybrid (2-leg ρ)** | **1.19812** | **0.4321 (−0.13)** | +0.00106 [−0.00138, +0.00356] |

Verdicts at Σunc 0.01: accept 99.9%, reject 0.1% (residual mean 0.0043, p90 0.0124, max 0.0154; balanced-lambda ρ mean 0.766, p10 0.691, p90 0.801). Train (n=7,228): 0.746 beats 0.70 **significantly** (AS-IS +0.00146 [+0.00038, +0.00258]; stress +0.00151 [+0.00044, +0.00263]); DC/hybrid beat 0.746 on train (−0.00058 / −0.00054, CI spans 0).

**G1 verdict:** the promoted 0.746 and the hybrid are each no worse than 0.746 (CI) and beat shipped 0.70 on held-out log-loss and on both-cell calibration (z 2.19 → 1.31 / 1.12; under the live-path stress the hybrid is the best-calibrated cell at z −0.13). Train z 4.30 → 2.52 / 2.09 (the brief's "below 2" is met on the joint cell under stress: +0.50 / −0.48).

### Band 0.12 stays — why
The measured set is D1/E0/F1/I1/SP1 only; Liga MX / MLS / UCL qualifiers / EFL Championship / Saudi (the club series we quote) have no measured btts|total value — league-transfer assumption. The band is their only cover until they are measured (then band = max(CI95 half-width, |league spread|) per the M1 judge standard).

### G2 — 26-context replay (fixture; fair = model P(combo YES), cents; Σunc = 0.01)

| rfq | n | pair | recorded | old (live pre-port) | promoted | new | shift | route old → new |
|---|---|---|---|---|---|---|---|---|
| 8cd961a1 NCXCRA btts×under3.5 | 2 | BxT | 27.33 | 27.33 | 27.33 | 27.33 | +0.00 | structural → hybrid [accept r=0.0000 ρ=0.708] |
| 9e425a54 | 9 | BxT | 20.34 | 20.33 | 20.65 | 20.65 | +0.32 | copula → copula |
| 10d9eaca | 5 | – | 31.89 | 31.89 | 31.89 | 31.89 | +0.00 | copula → copula |
| 9f694b39 NIJBOG btts×over2.5 | 2 | BxT | 57.65 | 57.64 | 58.56 | 59.54 | **+1.90** | copula(fallback r=0.0062) → hybrid [accept ρ=0.791] |
| 7fdb3c32 LEVAEK triple | 3 | BxT | 20.72 | 20.72 | 20.72 | 20.72 | +0.00 | structural → structural [accept r=0.0107] |
| 32114271 | 3 | BxT | 22.71 | 22.71 | 23.25 | 23.25 | +0.54 | copula → copula |
| 06e11b30 | 5 | BxT | 18.41 | 18.40 | 18.84 | 18.84 | +0.44 | copula → copula |
| 21408a5e | 4 | – | 7.70 | 7.70 | 7.70 | 7.70 | +0.00 | copula → copula |
| 5ba6b0ca NCXLEO | 2 | BxT | 43.82 | 43.81 | 44.84 | 46.07 | **+2.25** | copula(fallback r=0.0078) → hybrid [accept ρ=0.797] |
| 4d6e72d1 NCXLEO | 2 | BxT | 43.87 | 43.86 | 44.89 | 46.11 | **+2.24** | copula(fallback r=0.0102) → hybrid [accept ρ=0.797] |
| 901b171e CARWRE tie-NO triple | 3 | BxT | 16.80 | 16.80 | 16.80 | 16.80 | +0.00 | structural → structural [accept r=0.0195] |
| 6999a6b6 | 3 | BxT | 28.27 | 28.27 | 28.87 | 28.87 | +0.60 | copula → copula |
| 1fae2897 CARWRE | 2 | BxT | 47.85 | 47.85 | 48.85 | 50.00 | **+2.15** | copula(fallback r=0.0164) → hybrid [accept ρ=0.795] |
| 56f8275c NSHMIA stack | 4 | BxT | 26.20 | 26.20 | 26.65 | 26.65 | +0.46 | copula → copula |
| b8966cdd | 3 | – | 40.93 | 40.92 | 40.92 | 40.92 | +0.00 | copula → copula |
| bf7b217d | 4 | BxT | 13.74 | 13.73 | 14.40 | 14.40 | +0.67 | copula → copula |
| 7912d826 | 3 | BxT | 23.31 | 23.30 | 23.99 | 23.99 | +0.70 | copula → copula |
| 183d9ee6 | 4 | BxT | 19.02 | 19.01 | 19.90 | 19.90 | +0.89 | copula → copula |
| d549a996 WC triple | 3 | BxT | 25.00 | 25.00 | 25.00 | 25.00 | +0.00 | structural → structural [accept r=0.0219] |
| 1956f82c WC triple | 3 | BxT | 24.98 | 24.98 | 24.98 | 24.98 | +0.00 | structural → structural [accept r=0.0205] |
| 800a9b4b | 5 | BxT | 10.64 | 10.64 | 10.49 | 10.49 | −0.15 | copula → copula (NO-side pair) |
| 8b3da03e WC triple | 3 | BxT | 41.79 | 41.79 | 41.79 | 41.79 | +0.00 | structural → structural [accept r=0.0124] |
| e2b8ad40 WC triple | 3 | BxT | 25.19 | 25.19 | 25.19 | 25.19 | +0.00 | structural → structural [accept r=0.0183] |
| 9a2f54ca | 4 | – | 22.33 | 22.33 | 22.33 | 22.33 | +0.00 | copula → copula |
| d9b2376d | 6 | – | 2.29 | n/a | n/a | n/a | – | classifier UNKNOWN in the replay (no event metadata) — the verifier's 6-leg row |
| 0297ee65 WC corners stack | 4 | BxT | 14.44 | 14.44 | 14.37 | 14.37 | −0.07 | copula(unparseable corners) → copula [reject, sentinel] |

max |old − recorded| = **0.013c** (harness proof; verifier: 23/26 within 0.01c, two copula rows 0.013c). Blast radius: the 4 combos WITHOUT a same-game btts|total pair move **0.0000c**. Every shift is on a combo carrying such a pair: the four bare club over-2.5 pairs +1.90/+2.25/+2.24/+2.15c (the hybrid — model form, as the verifier said), the promoted copula pairs +0.32…+0.89c (≈ the 1c measured bias, scaled by the stack), NO-side pairs negative (−0.15, −0.07), structural triples unchanged.

All 25 priceable contexts land **ACCEPT** (residuals ≤ 0.0219 < 0.025); 0 CHALLENGE; 1 REJECT (the WC corners stack: unparseable leg → sentinel residual −1.0, copula as before).

### Timing (memo-miss cost, same inputs, 5 reps; joint memo hit path unchanged; pool deadline 0.8s)

| context | route | pre-port live | post-port live |
|---|---|---|---|
| NCXCRA btts×under (residual 0) | structural → hybrid | 24.8 ms | 61.7 ms |
| NIJBOG / NCXLEO ×2 / CARWRE (infeasible pairs) | copula fallback → hybrid | 5.0–7.2 ms (1 inversion then reject) | 104.9–125.4 ms |
| 3-leg structural (LEVAEK, CARWRE, WC ×4) | structural → structural | 39.4–178.7 ms | 43.4–165.1 ms |

The symmetric pair now pays the full structural perturbation set (7 inversions on an infeasible system ≈ 12–15 ms each) plus 7 brentq ρ solves (≈3 ms each) — inside the existing 3-leg structural envelope and well inside the 0.8 s pool deadline; the quote path stays O(1) and the memo key is unchanged (same-game flow hits). Quotes-per-minute before/after cannot be measured with the bot down — G3 is a relight gate (NEXT STEPS).

## Tests

- **Added: 33** — 32 in `tests/test_structural_fit_bar.py` + 1 (`test_dixon_coles::test_overidentified_system_over_hard_bar_refuses`).
- Focused modules (`test_structural_fit_bar, test_fit_challenge, test_structural, test_dixon_coles, test_sgp`): **165 passed, 0 failed**.
- Structural/risk/engine modules run after the port (`test_pricing_engine, test_club_soccer_wiring, test_persistence, test_structural_book_mc, test_exposure_structural_deltas, test_structural_tape_parity, test_joint, test_sgp_btts_1h_total, test_structural_api`): all green (332 passed in the pre-pin run; the 6 failures there were exactly the pins below, now moved). After the challenger stress-mode fix: `test_structural_param_challenger_p1_9, test_book_risk_wiring, test_structural_book_mc, test_fit_challenge, test_dixon_coles, test_structural_bridge_p0_7, test_structural_conditioning_p0_7, test_exposure_structural_deltas, test_structural_tape_parity, test_structural_fit_bar` → **170 passed, 0 failed** (+2 stress-mode tests after that run: 2 passed).
- First full-suite run (before the challenger fix): 3803 passed, **4 failed** — `test_book_risk_wiring` ×2 (the 0.70 / 0.82 band pins → 0.746 / 0.866) and `test_structural_param_challenger_p1_9` ×2 (the challenger's shocked re-inversion refused by the hard bar → WRONG/FIXED #9). Those four are the reason the stress mode exists; they were not worked around.
- **Full suite: the final full-suite run on the final code was still in progress ([ 41%]) when the orchestrator required the result — NOT a completed number. Computed so far: the first full run (before the challenger stress-mode fix) = 3803 passed / 4 failed / 3 deselected in 359 s; the four failures were repaired in commit 3b3f789 and the ten affected modules re-run green (170 passed) plus the two stress-mode tests (2 passed). The orchestrator must re-run the full suite on 3b3f789+ before merge.**
- ruff: clean on every touched file (2 pre-existing UP017 hits in `marketdata/metadata.py`, untouched). mypy (strict, `src/combomaker`): the touched modules type-check; **4 pre-existing** `type-arg` errors in `engine.py` (`_joint_cache: OrderedDict[tuple, …]`, `_joint_key -> tuple`) exist on `main` at the same sites (verified by running mypy on main's file) and are not from this build.

Deliberate pin changes (each commented in place with the build + measured reason; none weakened — every original intent is still asserted with a re-measured input):

| test | was | now | why |
|---|---|---|---|
| `test_fit_challenge::test_thresholds_mirror_live_inverter_constants` | asserts `residual > 0.005` in DC source | asserts `residual > 0.05` present and `0.005` absent | DC enforces the ONE hard bar |
| `test_structural::test_inconsistent_first_half_marginal_fails_closed` | 1H 0.40 / FT 0.55 (residual 0.0188) | 1H 0.50 / FT 0.55 (residual 0.0844) | 0.0188 is under the hard bar → prices; 0.0844 is a genuine contradiction |
| `test_dixon_coles::test_contradictory_exact_system_refuses` | 0.95/0.90 raises | 0.98/0.95 raises (0.0676); 0.95/0.90 solves with residual 0.0272 reported | measured residuals |
| `test_dixon_coles::test_overidentified_system_reports_residual` | TotalOver 0.80 (residual 0.0797) reports | 0.66 (0.0174) reports; new test: 0.80 refuses | the hard bar now applies to over-identified DC fits |
| `test_sgp` ×2 | soccer btts|total 0.70 (band 0.58/0.82) | 0.746 (0.626/0.866) | club measurement |
| `test_book_risk_wiring` ×2 | within-game ρ provider point 0.70 / tail-stress high 0.82 | 0.746 / 0.866 | club measurement + band 0.12 |
| `test_structural_param_challenger_p1_9::_graded_structural_book` | regulation ML pair 0.55 / 0.45 (zero draw mass; over-identified residual 0.156 — unrepresentable; production silently sampled a 15.6pp-misfit scoreline before this build) | 0.42 / 0.30 (residual 0.015) | both production and the challenger are now structural; the tests compare re-inversions, not a copula fallback |

## Parity results (G2, `tests/test_structural_fit_bar.py::test_parity_live_equals_prototype_to_the_cent_on_26_contexts`)

Live `PricingEngine.compute_joint` (real `PricingConfig()` defaults = the effective live values) vs the PRE-port prototype oracle on all 25 priceable contexts: **|Δp| < 1e-9 on every context** (and equal to the cent), verdict / residual / accept-bar / uncertainty equal to 1e-9, implied ρ equal to 1e-9 on the 5 hybrid contexts; the classifier-UNKNOWN 6-leg row is UNKNOWN in both. `test_blast_radius_exactly_zero_without_a_same_game_btts_total_pair`: the 4 non-pair combos equal the OLD fair to 1e-9 (**0.00c**), and every moved combo carries the pair flag.

## Counterfactual / quote-ability (G3 offline half)

Real engine (`PricingConfig()` + sell-only, synthetic 1-tick books around the recorded mids, the recorded target_cost size) on every CLUB btts×total shape in the fixture — 12 shapes, **12/12 return a non-zero NO bid**:

| rfq | n | recorded size | route | fair | NO bid | width | recorded fair / NO |
|---|---|---|---|---|---|---|---|
| 8cd961a1 NCXCRA | 2 | $5 | hybrid/accept | 27.10 | 68.00 | 5.61 | 27.33 / 71.10 |
| 9e425a54 | 9 | $5 | copula | 20.71 | 70.00 | 14.84 | 20.34 / 77.90 |
| 9f694b39 NIJBOG | 2 | $40 | hybrid/accept | 59.75 | 35.00 | 6.63 | 57.65 / 41.30 |
| 7fdb3c32 LEVAEK | 3 | $5 | structural/accept | 20.78 | 74.00 | 7.23 | 20.72 / 78.20 |
| 32114271 | 3 | $2 | copula | 23.04 | 71.00 | 7.92 | 22.71 / 75.70 |
| 06e11b30 | 5 | $10 | copula | 18.80 | 74.00 | 10.01 | 18.41 / 79.70 |
| 5ba6b0ca NCXLEO | 2 | $20 | hybrid/accept | 46.13 | 48.00 | 6.28 | 43.82 / 54.50 |
| 4d6e72d1 NCXLEO | 2 | $10 | hybrid/accept | 46.13 | 49.00 | 6.17 | 43.87 / 54.20 |
| 901b171e CARWRE | 3 | $2 | structural/accept | 16.80 | 78.00 | 7.15 | 16.80 / 81.40 |
| 6999a6b6 | 3 | $11 | copula | 29.13 | 65.00 | 8.46 | 28.27 / 69.70 |
| 1fae2897 CARWRE | 2 | $5 | hybrid/accept | 49.70 | 44.00 | 7.18 | 47.85 / 50.00 |
| 56f8275c NSHMIA | 4 | $8 | copula | 26.35 | 67.00 | 9.13 | 26.20 / 72.30 |

(Widths are the default-config defensive widths on 2c synthetic books, wider than the live 1c books + 1c markup — the probe proves quoting at real sizes, not the level.) Counterfactual on the fills: our NO bid on the four bare club pairs would have been ≈2c lower (NO fair 40.46 → 38.56 NIJBOG etc.), i.e. the 1.05–2.15c "booked edge" the deep dive found phantom is no longer booked.

## Blast radius

- **Pricing** (soccer only; non-soccer 0.00c by construction — `try_price_with_fit` returns no record and no path change for MT/MLB): (a) symmetric btts×total pairs → hybrid (+1.9…+2.3c on the recorded club over-2.5 fills; 0.00c on the feasible under-3.5 pair); (b) every copula-priced same-game btts|total pair → 0.746 (+0.3…+0.9c YES fair on stacks; NO-side pairs negative); (c) any exact DC system with residual in (0.005, 0.05] now PRICES structurally (was copula, or DECLINE for tie×total/btts — those now price the DC cell with the misfit in the width; the guard still declines above the bar); (d) any over-identified DC fit with residual > 0.05 now REJECTs to the copula (was priced with ≥5c misfit width). On the recorded contexts (c)/(d) move nothing (all residuals 0.0000–0.0219).
- **Risk**: `sim/structural_book.py` and `risk/exposure.py` call the same `invert` → the same ONE bar for the PRODUCTION book: exact pairs with residual in (0.005, 0.05] now sample the DC scoreline (were the copula sampler), over-identified > 0.05 fail closed to the copula (were sampled at any misfit — with the book's marginals off by the misfit; the copula keeps them exact). Pricing and risk now agree on which games are structurally representable. The P1.9 structural-parameter CHALLENGER re-inverts in stress mode (`contradiction_bar=False`) — byte-identical to its pre-build behaviour on over-identified games and STRONGER on exact pairs (the 0.005 bar no longer drops them). `test_structural_book_mc`, `test_exposure_structural_deltas`, `test_structural_tape_parity`, `test_structural_bridge_p0_7`, `test_structural_conditioning_p0_7`, `test_structural_param_challenger_p1_9`, `test_book_risk_wiring` green.
- **Store**: 3 new `structural_fits` columns (metadata-only `ADD COLUMN`; live table has 0 rows); the write is queued tape (drop-on-overflow, 200k queue) instead of a committed write — it is measurement, not money.
- **Lifecycle**: one `await` per priced RFQ that does a `put_nowait` (async mode) — no store I/O on the pricing path; the sync `_price` (backtests, reprices) records nothing by design.
- **Throughput**: quote path O(1); memo-miss cost for the symmetric pair +~100 ms (inside the 3-leg envelope and the 0.8 s pool deadline); memo key unchanged.
- Untouched: markup, caps, the tie guard, MT/MLB pricers, every other sport.

## Constitution check
- No new hand-set number: the hard bar (0.05) and the floor (0.5 × 0.05) are the pre-existing `fit_challenge` anchors; the accept bar's only live input is the books' summed uncertainty; the hybrid ρ is derived per match from the fit; 0.746 is a measurement (n=8,982) re-validated held-out.
- Mechanism repaired, not a number patched: the routing bar is derived; the stale blend is replaced by the measurement, not nudged.
- Caps stay the only refusal layer: every below-bar verdict prices; the REJECT falls to the copula (quotes) except the pre-existing tie guard.
- Rule 8: prototype in `tools/` first, fixture written PRE-port, live == prototype to 1e-9.
- Fix isolation: telemetry on the slow path only; money stays int centi-cents (no money math touched).
- `contradiction_bar` is a boolean mode (stress vs consistency), not a numeric knob; the bar it toggles is the one pre-existing anchor.

## Deviations from the brief (stated, with the measured reason)
1. The hybrid applies on EVERY priceable verdict of the symmetric pair, not only CHALLENGE: at residual 0 it equals the DC cell (1e-10), on the four fills the two differ by ≤ 0.06c, and a verdict-conditional route would add a cliff at the bar with no offsetting benefit. Non-symmetric shapes keep the DC cell exactly.
2. The floor is shipped as the brief specified (over-identified accept boundary); its binding on tight club books is measured and flagged as OPEN #6 — a label/telemetry decision with zero pricing impact.

## Gitignored-yaml lines the operator must add
**None.** The effective live value comes from the `config.py` default (the yaml has no `pricing.correlation` section); the promote ships in code.

## NEXT STEPS
1. **Orchestrator (after merge):** `tools.vitals.gate` fast tier 8/8 GREEN, then `--tier pre-ship` before arming (G4).
2. **Operator, at relight (G3):** the first club slate must show club btts×total RFQs returning non-zero NO bids at real sizes (12/12 offline here) and sends/min within noise of the pre-outage 300–460; `structural_fits` must receive rows within the first hour (expect ~100% ACCEPT on 1c books, REJECT only above 0.05 / unparseable) and `structural.fallback.*` counters non-zero.
3. **Decision owed (OPEN #6):** keep the 0.025 floor (brief) or drop it so the CHALLENGE label tracks the books' own resolution (previous builder's argument). Zero pricing impact either way; it only decides what the telemetry calls CHALLENGE on tight books.
4. **Measure, then tighten the band:** run `tools/calibrate_pairs_from_history.py` on Liga MX / MLS / UCL qualifiers / EFL Championship / Saudi once data exists; band = max(CI95 half-width, |league spread|).
5. **G5 pre-registered alarm (never a P&L refit):** weekly game-clustered realized P(both) vs model on club btts×total fills, |z| > 2 over ≥ 30 games; the same watch on the cross-game-only bucket (OPEN #8).
6. **Scope follow-ups:** MT/MLB inverters onto the derived bar (OPEN #7); a live-path check on real closing BTTS odds when a source exists (the stress test emulates the infeasible regime with a train-measured shift; it is not an observed market).
