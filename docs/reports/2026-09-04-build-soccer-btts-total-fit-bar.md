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
| 10 | **Found by the review-fix replay (read-only tape, 8/17–8/27):** the tie×total pickoff guard declined **10,528** RFQs in 10 days (~1,050/day) — every one an EXACT-system inversion with residual in (0.005, 0.044] (p50 0.0091, p90 0.0138, max 0.0438; **0 above 0.05**). Under the ONE bar all of them now PRICE the DC cell (residual in the width); the guard's decline population goes to ~0. This is route change (c) of the Blast radius, previously stated as "moves nothing on the recorded contexts" — true for SENT quotes, false for the declines they could not contain. | Held-out calibration of exactly that cell (`tools/validate_tie_total_oos.py`, live inverter, market draw × over-2.5, no fitted parameter): 2024/25 n=1,752 — DC cell log-loss 0.2522 vs independence 0.2694 (diff −0.0172, CI95 [−0.0271, −0.0071]); realized P(draw ∧ over) 0.0696 vs DC 0.0608 (z +1.55). The NEWLY PRICED subset (residual in (0.005, 0.05], n=467): realized 0.0771 vs DC 0.0689 (z +0.70; train n=1,628 z +1.82), DC−indep −0.0192 [−0.0387, +0.0010]; the OLD-priced subset (≤ 0.005, already quoted pre-build) carries the SAME signed gap (z +1.40, +0.9 pp). So the bar adds a population priced by the same model with the same pre-existing under-prediction of high-scoring draws — the "scoring runs above the scoreline model" family, not a bar artefact. | 878db97 | QUANTIFIED — G5 watch on tie×total fills + a MEASURED tie×over gap (like the train-measured btts gap) is the follow-up, never a hand number |
| 11 | `lifecycle._record_structural_fit` was awaited ON the RFQ pricing path without the exception-proof telemetry guard every other slow-path hook carries — a future store/signature error would have aborted the RFQ instead of dropping a telemetry row (review must-fix M1). | Body wrapped in `try/except` (counter `structural.telemetry.errored` + `log.exception`); unit + end-to-end tests: a raising `Store.record_structural_fit` still sends the quote with a non-zero NO bid. | 878db97 | FIXED |

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

- **Added: 33** (corrected in the review-fix pass; `pytest --collect-only`: main 3,778 → worktree 3,811) — 32 in `tests/test_structural_fit_bar.py` (30 from the build + 2 review-fix tests: the raising telemetry store, unit + end-to-end through `QuoteLifecycle.handle_rfq`) + 1 in `test_dixon_coles` (`test_overidentified_system_over_hard_bar_refuses`). The build's original "33 = 32 + 1" over-counted by two (the review counted 30 + 1 = 31 pre-fix).
- Focused modules (`test_structural_fit_bar, test_fit_challenge, test_structural, test_dixon_coles, test_sgp`): **165 passed, 0 failed**.
- Structural/risk/engine modules run after the port (`test_pricing_engine, test_club_soccer_wiring, test_persistence, test_structural_book_mc, test_exposure_structural_deltas, test_structural_tape_parity, test_joint, test_sgp_btts_1h_total, test_structural_api`): all green (332 passed in the pre-pin run; the 6 failures there were exactly the pins below, now moved). After the challenger stress-mode fix: `test_structural_param_challenger_p1_9, test_book_risk_wiring, test_structural_book_mc, test_fit_challenge, test_dixon_coles, test_structural_bridge_p0_7, test_structural_conditioning_p0_7, test_exposure_structural_deltas, test_structural_tape_parity, test_structural_fit_bar` → **170 passed, 0 failed** (+2 stress-mode tests after that run: 2 passed).
- First full-suite run (before the challenger fix): 3803 passed, **4 failed** — `test_book_risk_wiring` ×2 (the 0.70 / 0.82 band pins → 0.746 / 0.866) and `test_structural_param_challenger_p1_9` ×2 (the challenger's shocked re-inversion refused by the hard bar → WRONG/FIXED #9). Those four are the reason the stress mode exists; they were not worked around.
- **Full suite on the build's final code (5601dc7), run by the adversarial review: 3809 passed / 0 failed / 3 deselected in 303 s.** (The build's own final run had been cut at 41%; its first run was 3803 / 4 failed, the four repaired in 3b3f789.) The full-suite result on the review-fix commit is in the "Review fixes" section below.
- ruff: clean on every touched file (2 pre-existing UP017 hits in `marketdata/metadata.py`, untouched). mypy (strict, `src/combomaker`): **6 pre-existing errors, byte-identical on `main`** — `engine.py` ×4 `type-arg` (`_joint_cache: OrderedDict[tuple, …]`, `_joint_key -> tuple`) and `ising_amm.py` ×2 `no-any-return`; none introduced by this build (the build report's "4" omitted the two `ising_amm.py` hits).

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

- **Review-fix pass, wide sample:** live == prototype to **1e-9 on all 10,637 soccer contexts** (9,177 with a DC record + 1,460 copula stacks) of the 20,985-context 8/17–8/27 replay; the pre-port oracle reproduces the recorded fair within 0.02c on 99.0% of them (details in Review fixes S2).

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

- **Pricing** (soccer only; non-soccer 0.00c by construction — `try_price_with_fit` returns no record and no path change for MT/MLB): (a) symmetric btts×total pairs → hybrid (+1.9…+2.3c on the recorded club over-2.5 fills; 0.00c on the feasible under-3.5 pair); (b) every copula-priced same-game btts|total pair → 0.746 (+0.3…+0.9c YES fair on stacks; NO-side pairs negative); (c) any exact DC system with residual in (0.005, 0.05] now PRICES structurally (was copula, or DECLINE for tie×total/btts — those now price the DC cell with the misfit in the width; the guard still declines above the bar); (d) any over-identified DC fit with residual > 0.05 now REJECTs to the copula (was priced with ≥5c misfit width). On the 26 recorded contexts (c)/(d) move nothing (all residuals 0.0000–0.0219). **Quantified on the 10-day tape in the review-fix pass (Review fixes S2):** (c) = 583 bare btts×total pairs on sent quotes (`copula→hybrid`, p50 +2.24c) plus the 10,528 tie×total DECLINES (residual max 0.0438, all now priced — WRONG/FIXED/OPEN #10); (d) = 0 instances on sent quotes (max residual 0.0245); every other same-game structural system 0.000c.
- **Risk — the production consumers of the ONE bar, named (review fix)**: `sim/structural_book.py` (`build_game_plans`, default `contradiction_bar=True`) is called by **`sim/state_worst_case.py:577` — the det-max backstop, the ONE level gate in the risk stack** — and by **`sim/peak_profile.py:515`** (the peak-concentration profile), as well as `sim/book_risk.py` (the MC sampler) and `risk/exposure.py` (structural deltas). For all four: a game whose over-identified DC fit has residual > 0.05 now becomes COPULA legs (was a scoreline plan at ANY misfit) — for the det-max that is all-legs-adverse, i.e. a MORE conservative worst case; an exact pair with residual in (0.005, 0.05] goes the other way (scoreline plan, was copula legs) — a joint-consistent, typically LESS adverse worst case than all-legs-adverse. The det-max delta on the live book is therefore signed by which games the book holds; it is on the relight checklist (NEXT STEPS 2) and cannot be computed here (the bot is down; no live book). `risk/exposure.py`, `sim/structural_book.py` and `risk/exposure.py` call the same `invert` → the same ONE bar for the PRODUCTION book: exact pairs with residual in (0.005, 0.05] now sample the DC scoreline (were the copula sampler), over-identified > 0.05 fail closed to the copula (were sampled at any misfit — with the book's marginals off by the misfit; the copula keeps them exact). Pricing and risk now agree on which games are structurally representable. The P1.9 structural-parameter CHALLENGER re-inverts in stress mode (`contradiction_bar=False`) — byte-identical to its pre-build behaviour on over-identified games and STRONGER on exact pairs (the 0.005 bar no longer drops them). `test_structural_book_mc`, `test_exposure_structural_deltas`, `test_structural_tape_parity`, `test_structural_bridge_p0_7`, `test_structural_conditioning_p0_7`, `test_structural_param_challenger_p1_9`, `test_book_risk_wiring` green.
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

## Review fixes (adversarial review 2026-09-04 — verdict SHIP_WITH_FIXES)

| # | review item | status | what changed |
|---|---|---|---|
| M1 | `lifecycle._record_structural_fit` sits ON the RFQ pricing path (awaited before the NoQuote branch / risk snapshots / POST) without the project's exception-proof telemetry pattern | **FIXED** | Body wrapped in `try: … except Exception:` (the `_maybe_shadow_inplay` pattern): a store / signature error now counts `structural.telemetry.errored`, logs `structural_fit_telemetry_errored`, and returns — ONE telemetry row is dropped, never the quote. Tests: `test_lifecycle_recorder_swallows_a_raising_store_and_counts_it` (a raising store; an unreadable record) and `test_lifecycle_still_sends_the_quote_when_the_telemetry_store_raises` — END TO END through `QuoteLifecycle.handle_rfq` with `Store.record_structural_fit` raising: the club btts×total RFQ still reaches the sender with a non-zero NO bid, `open_quote_count == 1`, the errored counter is 1; the control run with a healthy store sends the identical NO bid and lands the `structural_fits` row (`accept`, `btts|total`, `hybrid`). |
| M2 | Report test counts wrong (33 vs 31 added; full suite "in progress"; mypy 4 vs 6) | **FIXED** | "Tests" section corrected: added is now **33 = 31 (build) + 2 (review-fix tests)**, `--collect-only` main 3,778 → worktree 3,811; the review's full-suite run on 5601dc7 (3809 / 0 / 3 in 303 s) is stated as the build's result and the review-fix commit's own full run is below; mypy: 6 pre-existing errors byte-identical on `main` (engine.py ×4, ising_amm.py ×2). |
| M3 | Blast radius omitted the two production risk consumers of the ONE bar (`sim/state_worst_case.py:577` = the det-max backstop, the ONE level gate; `sim/peak_profile.py:515`) | **FIXED** | Named in "Blast radius / Risk" with the sign of the change per case (over-identified > 0.05 → copula legs = all-legs-adverse = MORE conservative det-max; exact pair in (0.005, 0.05] → scoreline plan = joint-consistent, typically less adverse); the det-max / peak-profile before-vs-after on the first live book is on the relight checklist (NEXT STEPS 2) and folded into G4. Not computable here: the bot is down, no live book. |
| S1 | Floor ratification: on live 1c books sum-unc = 0.01, so `derived_accept_bar()` returns the 0.025 floor for essentially every pair — the derived bar is a constant in practice | **NOT CHANGED — decision owed (OPEN #6)** | Kept the brief's literal (floor = CHALLENGE_FRACTION × REJECT_OVERIDENTIFIED, both pre-existing anchors). Pricing is identical either way (verified by the review: CHALLENGE only adds the residual to the width, which every verdict already carries); what the floor decides is the LABEL: on 1c books CHALLENGE ≡ "residual > 0.025", REJECT ≡ "> 0.05". The wide sample below reports how often each label would fire with and without the floor so the operator can ratify from measured counts, not theory. |
| S2 | "EXACTLY 0.00c on every combo without a same-game btts\|total pair" asserted only on the 4 fixture combos; route changes (c)/(d) had no fixture example | **DONE — wide read-only replay** | **Method (read-only, `mode=ro`):** every `decisions` row of kind `quote_sent` in the last 12M rowids (8/17 01:24 → 8/27 06:04 UTC; 553,511 sent quotes) joined to `rfqs.legs_json` by `rfq_id`, kept when the combo has a same-game structural group or a same-game btts×total pair (43,939 sent quotes → **20,985 unique contexts** after de-duplicating identical legs+sides+mids; 2,834 skipped as relationship≠OK, 85 without an rfqs row). OLD oracle = the PRE-port modules (`git archive ab1bcae` — the commit before the pricing port — run as a source snapshot with its own ground-truth fixtures, `--unc 0.005`); it reproduces the recorded fair on **20,776 / 20,985 (99.0%) within 0.02c** (p50 0.004c, p99 0.019c; the 37 misses > 0.5c are MLB player-prop families re-priced by later builds, plus 2 btts×total). NEW = the live worktree engine (`PricingEngine.compute_joint`, `PricingConfig()` defaults) on the same inputs. **Blast radius:** the **16,737 combos WITHOUT a same-game btts×total pair shift by EXACTLY 0.000c** (max \|Δ\| = 0.0, not one non-zero) — MLB / margin-total families (10,348), soccer ML×total / btts×ML / ML×spread×total structural systems (all `structural→structural`, 0.000c), including the 155 over-identified soccer fits with residual in (0.005, 0.05] (`btts\|moneyline\|total` 108, `btts\|spread\|total` 38, `moneyline\|spread\|total` 9 — over-identified fits never had the 0.005 bar, so no route change). **Route change (c)** on sent quotes = **583 bare btts×total pairs** whose exact 2-leg fit the old 0.005 bar REJECTED to the copula@0.70 and which now price the hybrid (`copula→hybrid`, residual p50 ≈ 0.01, all ≤ 0.0245); **route change (d)** (residual > 0.05 → REJECT) = **0** on sent quotes (max residual in the whole sample 0.0245). The sent-quote tape cannot contain what the old path DECLINED — the **10,528 tie×total declines** in the same window are route change (c)'s other half (WRONG/FIXED/OPEN #10: all under the bar, now priced; held-out calibration there). **Pair shifts (4,248 combos WITH a same-game btts×total pair):** `structural→hybrid` (bare pairs the old bar accepted, n=2,002) mean +0.006c, p50 0.000c, p90 +0.019c, range −0.40…+0.71c — the hybrid IS the DC cell to within the residual; `copula→hybrid` (n=583) p50 **+2.24c** (the four recorded fills' +1.9…+2.3c generalise), p10 −0.04c; `copula→copula` (cross-game stacks, n=1,460 — the promote 0.70→0.746 only) mean +0.43c, p50 +0.48c, max +1.08c; `structural→structural` triples (n=203) 0.000c. **The largest moves are −4.4…−5.5c on 7 contexts of ONE shape: btts NO × over-1.5 YES** (UCL / La Liga / EFL Championship, $100 target-cost RFQs): btts YES logically implies over 1.5, so the true joint is P(over 1.5) − P(btts) (0.6736 − 0.4996 = 0.174); the hybrid returns 0.1732 (the DC-implied ρ for that pair is ≈ 1), the copula@0.70 returned 0.2279 — the old path over-stated the YES fair by 5.5c and under-bid NO by the same; the +2.34c tail is the standard btts YES × over-2.5 YES UCL pair. **Parity live == prototype on the wide sample:** on all **9,177 soccer contexts with a DC record and the 1,460 copula stacks, max \|Δp\| = 0.000000000c** (1e-9) — the 26-context parity holds at 10,637 contexts. **Floor counts (S1 data):** of 9,177 DC-record fits, CHALLENGE would fire on **0 with the floor (residual > 0.025)** and on **338 (3.7%) without it (> 0.01 = two 1c books)** — `btts\|total` 269, `btts\|moneyline\|total` 46, `btts\|spread\|total` 21, `moneyline\|spread\|total` 2; REJECT 0. **Timing (this replay ran 8 processes concurrently, so contended — the review's contention-free 4.9 → 78.8 ms is the reference):** symmetric pair (hybrid) p50 107 ms / p90 136 / p99 165 / max 213 ms; DC structural non-pair p50 32 / p99 92 ms; copula stacks with a pair p50 17 / p99 1,535 / max 2,340 ms and non-DC (MLB/MT) p99 1,024 ms — the copula-stack p99 exceeds the 0.8 s pool deadline under contention on `main`'s unchanged path (NEXT STEPS 6c). |
| S3 | Throughput: symmetric pair 4.9 → 78.8 ms memo-miss (16×); G3 must record sends/min; pre-existing 4-leg copula 430–760 ms ticket | **DONE (report)** | NEXT STEPS 2 now requires sends/min recorded explicitly on the first club slate; NEXT STEPS 6c opens the pre-existing 4-leg copula ticket (not this build; same on `main`). The wide replay's `compute_joint` timing (contended, 8 processes) is in S2. |
| S4 | `test_structural_param_challenger_p1_9` fixture move (0.55/0.45 → 0.42/0.30) is not load-bearing | **DONE (stated)** | The review verified all 11 tests pass on the OLD fixture under the new code + stress mode. The move is a CHOICE, kept because the representable game exercises the structural-vs-structural comparison the P1.9 challenger exists for; the fixture comment now says so. |
| S5 | Circular pin: `test_thresholds_mirror_live_inverter_constants` matched `residual > 0.05` by source text and `dixon_coles.py` kept the literal "because the test pins it by source" | **FIXED** | `dixon_coles.invert` now imports `REJECT_OVERIDENTIFIED` from `fit_challenge` and compares against the NAME (no float literal bar left in the inverter); the test asserts `dixon_coles.REJECT_OVERIDENTIFIED is REJECT_OVERIDENTIFIED` and that no `residual > 0.<digits>` literal remains (regex) — the value is pinned, not the text. `margin_total` / `mlb_runs` keep their legacy source mirror (unchanged, OPEN #7). No import cycle: `fit_challenge` imports only stdlib. |
| S6 | Hybrid applied on every priceable verdict (Deviation 1) means every symmetric pair pays the full perturbation cost | **NOT CHANGED (noted)** | Immaterial today (DC cell vs hybrid ≤ 0.06c on the 5 hybrid contexts; the wide sample confirms the magnitude below). If throughput ever binds, the lever is to reuse the base-fit ρ for the dc_rho-band probes instead of re-deriving it per perturbation — recorded in Deviations. |
| S7 | `record_structural_fit` reversed its own "committed, must not be droppable" contract | **FIXED (docstring)** | The docstring now states the CONTRACT: rows are droppable tape (`store_writer_stats.dropped_writes` counts them); the `structural.fallback.<verdict>[.<family>]` counters are the loss-free count; any fallback-share tool must treat the table as a sample. |
| S8 | OPEN #7 (`margin_total.invert_means` / `mlb_runs.invert_runs` on the legacy 0.005 exact bar) is the same asymmetry | **SCHEDULED** | NEXT STEPS 6a: same repair shape (ONE hard bar + `classify_fit(resolution=…)`), prototype in `tools/` first. |

**Full suite on the review-fix commit:** **3811 passed / 0 failed / 3 deselected in 268.8 s** (`PYTHONPATH=src … -m pytest -q -p no:cacheprovider`, default addopts, worktree modules; run while the 4-shard replay was still executing). Collected: 3,814 (main 3,781) → **+33 tests**. Touched modules first: `test_structural_fit_bar` + `test_fit_challenge` + `test_dixon_coles` 83 passed; adjacent `test_lifecycle`, `test_persistence`, `test_structural_param_challenger_p1_9`, `test_structural`, `test_sgp` 149 passed. ruff: clean on every touched file and the new tool. mypy `--strict src/combomaker`: the same 6 pre-existing errors (engine.py ×4 type-arg, ising_amm.py ×2 no-any-return), none new. `tools/validate_tie_total_oos.py --history …/data/history` output is quoted in WRONG/FIXED/OPEN #10.

**Files touched by the review-fix pass:** `src/combomaker/rfq/lifecycle.py`, `src/combomaker/pricing/dixon_coles.py`, `src/combomaker/ops/persistence.py` (docstring), `tests/test_structural_fit_bar.py` (+2), `tests/test_fit_challenge.py`, `tests/test_structural_param_challenger_p1_9.py` (comment), `tools/validate_tie_total_oos.py` (new), this report. The wide-replay pull / live-replay / analysis scripts and the ab1bcae source snapshot stayed in the session scratchpad (method fully stated above; nothing scratch is committed).

**Blast radius of the review-fix pass:** `lifecycle._record_structural_fit` (exception guard only — no pricing change; the quote path is byte-identical when the store behaves), `dixon_coles.invert` (the bar's VALUE is unchanged — 0.05 — only its binding moved from a literal to the imported anchor; `test_dixon_coles` / `test_structural_fit_bar` parity re-run green), `persistence.record_structural_fit` docstring, three test files, this report. No config, cap, markup, or money path touched.

## Gitignored-yaml lines the operator must add
**None.** The effective live value comes from the `config.py` default (the yaml has no `pricing.correlation` section); the promote ships in code.

## NEXT STEPS
1. **Orchestrator (after merge):** `tools.vitals.gate` fast tier 8/8 GREEN, then `--tier pre-ship` before arming (G4).
2. **Operator, at relight (G3):** the first club slate must show club btts×total RFQs returning non-zero NO bids at real sizes (12/12 offline here) and **sends/min recorded explicitly** (within noise of the pre-outage 300–460; the symmetric-pair memo-miss cost is 16× on the measured contexts, inside the 3-leg envelope — see Review fixes); `structural_fits` must receive rows within the first hour (expect ~100% ACCEPT on 1c books, REJECT only above 0.05 / unparseable) and `structural.fallback.*` counters non-zero; `structural.telemetry.errored` must stay 0. **Det-max delta (review must-fix):** on the first live book, log the det-max backstop (`state_worst_case`) and the peak profile (`peak_profile`) before/after arming and attribute any change to games whose DC fit crossed the ONE bar (over-identified > 0.05 → copula legs = more adverse; exact pair in (0.005, 0.05] → scoreline plan) — fold into the G4 pre-ship gate.
3. **Decision owed (OPEN #6):** keep the 0.025 floor (brief) or drop it so the CHALLENGE label tracks the books' own resolution (previous builder's argument). Zero pricing impact either way; it only decides what the telemetry calls CHALLENGE on tight books.
4. **Measure, then tighten the band:** run `tools/calibrate_pairs_from_history.py` on Liga MX / MLS / UCL qualifiers / EFL Championship / Saudi once data exists; band = max(CI95 half-width, |league spread|).
5. **G5 pre-registered alarm (never a P&L refit):** weekly game-clustered realized P(both) vs model on club btts×total fills, |z| > 2 over ≥ 30 games; the same watch on the cross-game-only bucket (OPEN #8).
6. **Scope follow-ups:** (a) **schedule OPEN #7** — `margin_total.invert_means` / `mlb_runs.invert_runs` still carry the legacy hand-set 0.005 exact bar this build removed for DC, so NFL/NBA/MLB exact pairs route away from their approved models the same way club btts×total did; same repair shape (ONE hard bar + `classify_fit(resolution=...)`), prototype in `tools/` first; (b) a live-path check on real closing BTTS odds when a source exists (the stress test emulates the infeasible regime with a train-measured shift; it is not an observed market); (c) **pre-existing throughput ticket (not this build):** 4-leg copula contexts take 430–760 ms memo-miss on `main` AND the worktree (e.g. 9a2f54ca ~745 ms) — within a hair of the 0.8 s pool deadline, and under 8-process contention the wide replay measured copula-stack p99 1,535 ms / max 2,340 ms on the unchanged path; measure the copula 4-leg path and either cache its matrices or raise the deadline from measured p99.
