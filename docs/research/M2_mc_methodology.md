# M2 — Monte Carlo Methodology (top-to-bottom deep-dive)

**Scope:** the settlement Monte Carlo used to grade the maker's *whole book* of
correlated combo (parlay/SGP) positions on the sell side — real-time (quote-time
marginal risk, must be milliseconds) and batch (nightly VaR/ES/ruin). This is a
design document for enhancing the **existing** engine, not a rewrite. Everything
below cites the code that exists today and specifies the delta to a "pristine"
bar.

Grounded in:
- `src/combomaker/sim/engine.py` — the MC engine (`sample_leg_values`,
  `simulate`, `marginal_impact`, `leg_deltas`, `PortfolioStats`).
- `src/combomaker/pricing/copula.py` — the **analytic** Gaussian-copula joint
  (`gaussian_copula_joint_prob`, `build_block_corr`, `nearest_psd`) — the parity
  oracle the MC must agree with.
- `src/combomaker/risk/{exposure.py, limits.py, inplay.py, markouts.py,
  killswitch.py}` — the consumers of MC output (VaR/ES, per-event worst case,
  mass-acceptance).
- `src/combomaker/ops/report.py` `_portfolio_mc` — the *only current live caller*
  of `simulate`, and it is wrong in three ways (documented in §1.4 / §7).
- `src/combomaker/core/money.py` — cc units; `NOTES.md` A10–A13 (settlement
  semantics), E1–E8 (risk assumptions), H2/H3 (measured ρ + banded matrices).

---

## 0. Executive summary — the one thing to get right

The P&L sweep finding reframes the problem: **the risk unit is the GAME, not the
ticker.** Combos share legs and games, so payouts are correlated; a NO-seller's
fatal risk is the **joint upper tail of parlay hits** (many combos hitting *at
once* because their shared games all broke the wrong way). That tail is:

1. **Rare** — each parlay hits ~25% of the time, and a mass-loss event needs many
   *correlated* combos to hit together, so it lives at the 0.1–2% quantile.
2. **Fat and cross-combo-correlated** — driven by a handful of common game
   factors (the concentration finding: 68 combos = 50% of contracts; single
   combos carried ~$1M payout swings).
3. **The only number that can end the book** — max payout $23.5M vs $1.8M
   premium means one bad correlated cluster dwarfs a year of edge.

Naive MC (what `simulate` does today: i.i.d. paths, statistics read off order
statistics) estimates the **mean fair** beautifully and the **1% tail terribly**.
The single highest-impact enhancement is **importance sampling on the shared game
factors** (Glasserman–Li two-step, §3) so the ruin probability and ES are
estimated with usable precision inside the batch budget. Everything else
(variance reduction for the mean, QMC, antithetics) is a rounding-error
improvement by comparison for *this* output.

Ranked recommendations are in §8.

---

## 1. CORRELATED SAMPLING — drawing correlated binary leg outcomes

### 1.1 WHY

The whole edge is the correlation layer (`NOTES.md` H2/H3; the pricing side is
top-down from live marginals). The MC must reproduce **exactly the same
dependence** the analytic pricer used to price the quote, or the risk engine and
the pricer disagree and the book is graded against a model it wasn't priced on.
Two hard constraints:

- **Marginal fidelity:** each leg's simulated YES-rate must equal its live
  marginal `p_i` to the bin (the marginals are the market's, not ours — `NOTES.md`
  A12/A13, H2).
- **Pairwise fidelity:** the realized rank/tetrachoric correlation between legs
  must equal the measured `ρ_ij` that `pricing/copula.py` consumed.

### 1.2 METHOD — what the code already does (correct, keep it)

`sim/engine.py:148 sample_leg_values` is a **textbook latent-normal / Gaussian
copula sampler** and it is the right primitive:

```
z = rng.standard_normal((n, n_legs)) @ chol.T   # correlated latent normals
u = ndtr(z)                                       # -> uniforms, Phi(Z)
out[:, j] = values[searchsorted(cum, u[:, j])]    # inverse-CDF per leg
```

For a binary leg this is exactly `leg i is YES  <=>  Z_i <= Phi^{-1}(p_i)`, the
same latent-Gaussian thresholding the analytic path uses
(`copula.py:154 gaussian_copula_joint_prob`: "leg i YES iff Z_i <= Phi^{-1}(p_i);
joint = MVN CDF"). This is the standard construction in the literature —
dichotomizing a latent Gaussian at an unknown cutoff, with the binary pair
correlation a nonlinear ("bridge"/tetrachoric) function of the latent ρ
([latent Gaussian copula for binary data, J. Multivariate Analysis 2022](https://www.sciencedirect.com/science/article/abs/pii/S0047259X21002049)).
The inverse-CDF-through-shared-uniform trick generalizes it to the scalar
settlement distributions (`LegModel.settlement`, e.g. penalty-shootout 0.5 bands,
`NOTES.md` I8) while preserving rank correlation — this is correct and worth
keeping.

**Consistency with the analytic pricer is a *convention* match, and it holds:**
both use the same `ρ` as the *latent* normal correlation (not the binary phi
correlation). So the MC hit-rate for a pair converges to the analytic
`gaussian_copula_joint_prob` — verified by the existing closed-form test
(`test_sim_engine.py:65`: `0.25 + asin(ρ)/(2π)` for two p=0.5 legs at ρ=0.7).
Keep that invariant; it is the parity anchor (§5).

### 1.3 THE ENHANCEMENT — block structure = the game graph (this is the M2 delta)

Today `simulate` takes a single dense `corr` over *all* legs and one Cholesky
over the whole matrix. That does not scale to a book of hundreds of combos and,
worse, it does not encode the finding that **cross-game blocks are independent
and within-game legs are correlated**. The pristine sampler is **block-structured**:

- Partition legs by **game key** (the same `_game_key` grouping the pricer uses —
  `NOTES.md` L10: correlation blocks keyed on the GAME code, period markets kept
  separate per L11). Legs in different games are independent by construction
  (`cross_event_rho ≈ 0`, measured — `NOTES.md` H2 corners≈0, MLB ml×over≈0).
- Sample **one small correlated latent block per game** (`build_block_corr`
  already builds exactly these block matrices — reuse it, do not reinvent), and
  concatenate. Cross-game draws use independent RNG streams.
- This turns one `O(L^3)` Cholesky over all legs into `Σ O(g^3)` over games
  (g = legs per game, tiny — 2–5), plus it makes the independence exact rather
  than a near-zero off-diagonal that a full Cholesky would smear.

Why it matters beyond speed: it makes the **game the sampling unit**, which is
the unit the P&L sweep says is the true risk unit, and it is precisely the
factor structure importance sampling (§3) exploits. A combo that spans games is
just a product across its legs' (independent) game blocks — the shared-game
legs inside one block carry the dependence, the cross-game legs multiply
independently. This is the Gaussian *factor* copula, the same object
Glasserman–Li twist.

**Consistency guard:** the MC block matrix for a game must be *the exact matrix
the pricer built for that same game's SGP* (`pricing/sgp.py build_sgp_correlation`
→ `price_joint_matrices`). Do not let the risk view and the pricing view build ρ
from two code paths (that is the L10-class quiet failure — dead config on one
side, tests green). The enhancement should thread the *same* `SgpParams`/block
builder into the sampler, or assert parity on a fixture.

### 1.4 A PRE-EXISTING BUG THIS SECTION MUST FIX

`ops/report.py:50 _portfolio_mc` builds `corr = np.eye(len(legs))` — it prices
the standing portfolio risk view at **pure independence**, and handles NO-side
legs with "complementary pseudo-legs" (`report.py:83`) which **breaks the
correlation entirely** (a leg and its complement get independent columns). For a
book whose whole risk is cross-combo game correlation, the standing risk report
is blind to the exact thing that can rupture it. The enhancement (block ρ from
the game graph) is what makes this report honest. (Also `report.py:67` silently
substitutes `p=0.5` for a missing marginal and only flags it — that violates the
UNKNOWN-is-never-safe rule, `NOTES.md` E1; missing marginal should make the risk
number *unusable/no-go*, not a 0.5 placeholder.)

### 1.5 RECOMMENDATION

- **Keep** `sample_leg_values`' latent-normal core and inverse-CDF settlement
  mechanism — it is correct and matches the analytic path.
- **Add** block-structured sampling keyed on the game graph, reusing
  `build_block_corr` / the pricer's own block builder; independent RNG substreams
  per game.
- **Fix** the `report.py` independence + pseudo-leg NO handling by feeding the
  real block ρ and modeling NO legs by side (not by a fake complement leg).
- **Assert parity** (§5) that a game block's MC hit-rate == the pricer's analytic
  joint for that block.

---

## 2. VARIANCE REDUCTION — antithetics, control variates, stratification, QMC

### 2.1 WHY, and the crucial split

Which technique helps depends entirely on **which output**:

| Output | Estimator today | What dominates its error |
|---|---|---|
| Mean combo fair / book EV | sample mean | central variance ~ σ/√n |
| P(profit), MTM | sample mean of an indicator | central variance |
| **VaR_0.99, ES_0.99, P(ruin)** | order statistic / tail mean (`_stats_from_pnl`) | **tail sparsity** — only ~n·(1−q) samples in the tail |

Variance reduction (VR) below helps the **mean-type** outputs a lot and the
**tail** outputs almost not at all. The tail needs importance sampling (§3).
State this explicitly so nobody sweeps `n` up expecting the 99% ES to converge —
it converges like `1/√(n·0.01)`, i.e. 10× the samples for √10 the tail
precision.

### 2.2 Control variates — the analytic combo fair (highest-value VR here)

**METHOD.** We *have* a closed-form control for free: `gaussian_copula_joint_prob`
gives the exact analytic hit probability of any combo (and thus its exact EV per
contract). Use `Y = analytic per-combo P&L expectation` as a control variate for
the MC book P&L `X`:

```
X_cv = X - β (Y_mc - E[Y])          β* = Cov(X, Y)/Var(Y)
```

where `Y_mc` is the MC estimate of the same combo's mean and `E[Y]` is its known
analytic value. Because the MC and analytic use the *same* copula (§1.2), `X` and
`Y` are extremely correlated (ρ often > 0.99 for the mean), so the variance of
the mean-EV estimator collapses. This is the single best VR lever for the **mean
fair** and for **`marginal_impact`** (§6) — and it costs one extra analytic call
we already make at pricing time.

*Caveat:* the analytic control is a control for the **mean**, not the tail. It
shrinks EV/MTM error, not VaR error. It is still worth wiring because the daily
report and the EV ledger are graded on mean edge (`NOTES.md` mission; `report.py`
"graded on cumulative expected edge").

### 2.3 Common Random Numbers (CRN) — already present, extend it

`marginal_impact` (`engine.py:240`) and `leg_deltas` (`engine.py:261`) already use
CRN: the with/without books are evaluated on the *same* sampled `values`, so the
difference is a low-variance estimate of the candidate's impact
(`test_sim_engine.py:144` shows a zero-variance difference in the degenerate
case). This is exactly right and is the workhorse for quote-time marginal risk.
**Extend it:** the quote-time "does adding this combo breach a limit?" question
(§6) must reuse the *same seed* as the standing book snapshot so the delta is CRN,
not two independent noisy numbers subtracted.

### 2.4 Antithetic variates — cheap, small, keep for the mean

**METHOD.** For every latent draw `Z`, also evaluate `−Z` (`u ↔ 1−u`). For
monotone payoffs (a combo YES payoff is monotone in each leg's latent), the
antithetic pair is negatively correlated and halves variance for ~0 extra RNG
cost. Trivial to add to `sample_leg_values` (draw `n/2`, stack `[Z; −Z]`).

**RECOMMENDATION.** Add antithetics for the **mean** outputs; expect ~1.3–2×
variance reduction on EV/P(profit). **Do not** expect help in the tail — the
antithetic of a ruin path is a windfall path, uncorrelated in the far tail. Keep
it because it is nearly free and composes with CRN and control variates.

### 2.5 Stratification — stratify on the shared game factors

**METHOD.** The book's tail is driven by a few common game outcomes. Stratify the
**common factor** (e.g. the latent that drives a high-concentration game, or a
one-dimensional "market-wide" factor) into equal-probability bins and sample a
fixed count per stratum. This guarantees the dangerous "many games break wrong"
region is represented every run instead of by luck. Stratified sampling on the
systematic factor is a well-known precursor/companion to factor IS in credit
portfolios.

**RECOMMENDATION.** Medium value; it is a poor-man's IS. Prefer full IS (§3) for
ruin, but stratifying the top-1 concentration factor is a cheap, robust hedge and
composes with QMC.

### 2.6 Quasi-Monte Carlo (Sobol) — good for the mean, watch the dimension

**METHOD.** Replace `standard_normal` with Sobol points mapped through `Phi^{-1}`
(scipy `stats.qmc.Sobol` + `MultivariateNormalQMC`), scrambled (Owen scrambling)
so you still get an unbiased estimate *and* an error estimate from independent
scrambles. QMC integrates smooth, low-effective-dimension integrands at close to
`O(1/n)` instead of `O(1/√n)`
([Owen, *Monte Carlo theory, methods and examples*](https://artowen.su.domains/mc/)).

**WHERE IT HELPS.** The **book EV / combo fair** is a smooth, low-effective-
dimension integral (dominated by the few common game factors) — QMC can give
5–50× effective-sample gains there. **WHERE IT DOESN'T.** VaR/ES read off an
order statistic is a discontinuous functional; scrambled Sobol still helps some
but the gain is muted, and unscrambled Sobol gives no CI. High nominal dimension
(hundreds of legs) also erodes QMC unless you Sobol only the **leading factors**
(the game commons) and use pseudo-random for the idiosyncratic residuals — a
standard hybrid.

**RECOMMENDATION.** Use scrambled-Sobol for the **batch mean/fair** and the
**real-time marginal EV** where dimension after block reduction is small; keep
pseudo-random (with IS) for the tail. Determinism: Sobol is deterministic given a
scramble seed — fits the seeding rule (§4).

### 2.7 VR summary table

| Technique | Best for | Est. gain (mean) | Est. gain (0.99 tail) | Cost |
|---|---|---|---|---|
| Control variate (analytic fair) | EV, MTM, marginal EV | 10–100× var | ~none | ~0 (reuse pricer) |
| CRN (have it) | marginal impact / deltas | huge on *differences* | huge on differences | 0 |
| Antithetic | EV, P(profit) | 1.3–2× | ~none | ~0 |
| Stratify common factor | tail representation | small | 2–5× | low |
| QMC (scrambled Sobol) | smooth mean/fair | 5–50× eff. | modest | low–med |
| **Importance sampling (§3)** | **ruin, VaR, ES** | (n/a) | **10–1000×** | med |

---

## 3. TAIL / RARE-EVENT ESTIMATION — importance sampling (the headline)

### 3.1 WHY naive MC is bad in the tail (quantified for this book)

To estimate P(ruin) = p with relative error `ε` at 95% confidence, naive MC needs
`n ≈ (1.96/ε)^2 · (1−p)/p`. For a ruin prob `p = 10^-3` and 10% relative error
(`ε=0.1`), that is `n ≈ 3.8×10^5 · 999 ≈ 3.8×10^8` paths — per book snapshot,
per night. At `p = 10^-4` it is billions. The estimator's relative error
*explodes* as the event gets rarer (`√((1−p)/(p·n))`), which is exactly the
regime a NO-seller cares about: the events that end the fund. The current
`_stats_from_pnl` reads ES_0.99 from `pnl[pnl <= cut]` — with n=100k that tail is
~1000 samples, and ES_0.999 would be ~100 samples: a noisy, unstable number that
*understates* fat-tail loss because the extreme cluster events are almost never
drawn.

### 3.2 METHOD — Glasserman–Li two-step IS in the Gaussian factor copula

This system **is** the canonical setting for
[Glasserman & Li, *Importance Sampling for Portfolio Credit Risk*, Management Science 2005](https://business.columbia.edu/sites/default/files-efs/pubfiles/1368/Glasserman_importance_sampling.pdf).
Map the analogy exactly:

| Credit portfolio | This combo book |
|---|---|
| Obligors | Combo positions (NO side) |
| Default of obligor k | The parlay HITS (settles YES) → we pay $1 |
| Common factors Z | **Game latent factors** (the shared-game blocks, §1.3) |
| Idiosyncratic ε | Per-leg residual latent |
| Portfolio loss L | Total book payout on hits |
| Tail event L > x | **Ruin / large-loss** for the NO-seller |

Glasserman–Li's two-step procedure, applied here:

1. **Shift the common (game) factors.** Sample the game latent `Z` from a
   *mean-shifted* normal `N(μ, I)` chosen to push the systematic state toward
   "many shared games break so that the correlated parlays hit." The optimal
   shift `μ` solves a small convex program (maximize the tail contribution minus
   the likelihood-ratio penalty); in practice pick `μ` to target the loss level
   `x` you're measuring (the VaR level).
2. **Exponentially twist the conditional hit probabilities.** *Given* the shifted
   factors, the combos are conditionally independent (that is the factor-copula
   property, and here it is literally the block structure of §1.3). Apply an
   exponential change of measure (twist parameter `θ` from the conditional
   cumulant generating function) to the conditional hit indicators to further
   raise the chance of a large aggregate payout.

Every sample carries a **likelihood ratio** `W = (dP/dQ)` = `exp(−μᵀZ + ½‖μ‖²)`
for the factor shift × the conditional twist ratio; the unbiased tail estimate is
`E_Q[ 1{L>x} · W ]` and ES is `E_Q[ L·1{L>x}·W ] / P(L>x)`. Unbiased, and with
variance orders of magnitude smaller in the tail — this is what makes P(ruin) and
ES_0.99/0.999 *usable* numbers instead of noise.

### 3.3 Choosing the shift automatically — cross-entropy (adaptive IS)

Rather than hand-solve the shift per book, use the **cross-entropy (CE) method**
([Rubinstein & Kroese; recent survey](https://arxiv.org/abs/2509.07160)): a short
pilot run adaptively fits the IS parameters (factor mean shift, twist) that
minimize KL divergence to the zero-variance optimal IS density, then the main run
samples from that. CE is the standard, robust, model-agnostic way to pick the
biasing density for rare events and it re-fits itself as the book changes night
to night. **Guard (from the literature):** naive CE mixtures can be light-tailed
and converge slowly for very small `p`; use a light+heavy two-component mixture
or a safeguarded CE variant so the tail is actually explored
([safe CE-IS, 2025](https://arxiv.org/abs/2509.07160)).

### 3.4 RECOMMENDATION

- **Batch (nightly) ruin/VaR/ES:** implement Glasserman–Li two-step IS on the
  game-factor blocks, with CE to auto-select the shift. This is the single
  highest-impact item for tail accuracy — 2–3 orders of magnitude variance
  reduction is typical in this exact model class, turning a `3.8×10^8`-path
  problem into `~10^5`–`10^6`.
- **Report both the naive and IS tail** for the first weeks with an agreement
  band, so IS is validated against brute force before it becomes the number of
  record (defense-in-depth against a mis-specified biasing density silently
  understating risk — the same class of quiet failure the repo guards against).
- **Real-time:** IS is *not* needed at quote time — the quote-time question is a
  *marginal* mean/limit check (§6), not a fund-ruin estimate. Keep IS in the
  batch path.

---

## 4. CONVERGENCE, SIZING & DETERMINISM

### 4.1 Standard error and stopping rules

- **Mean:** MC standard error `SE = σ/√n` (`_stats_from_pnl` already computes
  `std_cc`; the test file's `binary_hit_sigma` is exactly this). Report a ±2σ CI
  on EV — `report.py`'s note already promises "±2σ MC bands" but doesn't compute
  them; the enhancement should actually emit `ev_cc ± 2·std_cc/√n`.
- **VaR / quantile:** the SE of an empirical q-quantile is
  `SE ≈ √(q(1−q)/n) / f(VaR)` where `f` is the P&L density at the quantile. Near a
  fat tail `f` is small, so quantile SE is large — this is the analytic statement
  of §3.1. Emit it so the operator sees the tail number's uncertainty.
- **ES:** batched/bootstrap SE (resample the tail contributions). Do **not**
  present ES_0.99 without its CI; a point ES on 1000 tail samples invites false
  confidence.
- **Adaptive stopping:** run until the CI half-width on the *target* statistic
  (EV for pricing; VaR/ES for risk) is below a tolerance, capped by a wall-clock
  budget. Sequential MC with a relative-precision target is standard; guard
  against the optional-stopping bias by checking on fixed sample *checkpoints*,
  not every path.

### 4.2 How many paths (concrete)

- **Quote-time marginal EV** (small dimension after block reduction, with control
  variate): `10^3–10^4` paths gives sub-tick EV precision in <1 ms vectorized
  (§7). Often the analytic path (`gaussian_copula_joint_prob`) is exact and MC
  is unnecessary here — prefer analytic when legs ≤ ~6 and only fall to MC for
  large or scalar-settlement combos.
- **Batch book EV / MTM:** `10^5` (current default) with antithetic+control is
  ample.
- **Batch VaR_0.99 / ES_0.99:** naive `10^6–10^7`; **with IS `10^5`** for the
  same precision. VaR/ES_0.999 or explicit P(ruin): **IS is mandatory** — naive
  is infeasible.

### 4.3 Determinism & seeding (repo rule)

The engine is already deterministic via `np.random.default_rng(seed)`
(`engine.py:235`, `test_sim_engine.py:TestDeterminism`), and `copula.py` pins a
fixed QMC seed for its MVN integrator (`_MVN_SEED = 20260705`) so the *same
inputs always price to the same number*. Preserve and extend this:

- The repo bans `Date.now`/`Math.random` in workflow scripts (system rule). The
  Python analogue: **never** seed from wall-clock or unseeded global
  `np.random`; always pass an explicit `seed` and construct a
  `np.random.default_rng(seed)`. There is one live `np.random` usage to police —
  `exchange/ws.py` — confirm it's not on any determinism-critical path (it is a
  transport jitter, acceptable; keep it out of sim/pricing).
- **Per-substream seeding:** with block sampling and IS you now have multiple RNG
  streams (per game, per IS stage). Use `np.random.SeedSequence(seed).spawn(k)`
  to derive independent, reproducible child streams — never seed children by
  `seed+1` (correlated streams). Document the seed in every `PortfolioStats`/
  report row so any run reproduces exactly (matches `NOTES.md` reporting
  discipline).
- **Antithetic/QMC determinism:** Sobol scramble seed and antithetic pairing are
  both deterministic functions of `seed` — fold them into the same
  `SeedSequence`.

---

## 5. VALIDATION — MC vs analytic, and vs settlement

### 5.1 Parity against the analytic copula (the primary gate)

The MC and the analytic pricer share a model, so they must agree where the
analytic has a closed/near-closed form. Build a **parity test suite** (extend
`test_sim_engine.py`, which already does the ρ=0.7 orthant closed form):

- **Bivariate orthant:** two p=0.5 legs at ρ → MC hit-rate vs
  `0.25 + asin(ρ)/(2π)` (exists, `test_sim_engine.py:65`). Add general
  `(p_1, p_2, ρ)` vs `gaussian_copula_joint_prob` (which itself is validated to a
  tight abseps for n≤4, `copula.py:_TIGHT_ABSEPS`).
- **Fréchet corners:** ρ→+1 comonotone hit-rate → `min(p_i)`; ρ→−1 →
  `max(0, Σp−(n−1))`. Both exist (`test_rho_one_comonotone`,
  `test_rho_minus_one_countermonotone`) and match `frechet_bounds` in
  `copula.py`.
- **Small-n joint (n=3,4):** MC hit-rate vs `gaussian_copula_joint_prob` within
  `3·SE` on a grid of ρ and p — this is the direct "MC == analytic" gate and it
  must run in CI on every change to either module (catches an L10-class drift
  where one side's ρ silently dies).
- **Block independence:** cross-game legs' realized correlation ≈ 0 within SE;
  within-game realized tetrachoric ≈ the input latent ρ (bridge-function check).
- **PSD/repair parity:** feed a non-PSD ρ through both paths; both must call the
  *same* `nearest_psd` (`copula.py:108`) and agree — don't let the sampler's
  `_cholesky_with_jitter` and the pricer's `nearest_psd` diverge on repair.

### 5.2 IS validation (before it becomes the number of record)

- **Unbiasedness:** IS tail estimate must equal the naive tail estimate within
  their combined CIs on a book small enough to brute-force. Ship IS *behind* this
  agreement check.
- **Likelihood-ratio sanity:** `E_Q[W] = 1` (the LR integrates to one) — a cheap
  invariant that catches a mis-derived twist.
- **Effective sample size** `ESS = (Σw)²/Σw²`: monitor it; a collapsing ESS means
  the biasing density is mis-fit (over-shifted) and the ES is untrustworthy — a
  fail-closed trigger, not a silent bad number.

### 5.3 Against settlement (the ruler the model can't bend)

Per `NOTES.md` defense #5 and #3, settlements are ground truth. The MC produces a
*distribution*; validate it against realized settlements two ways:

- **Reliability / PIT:** for each settled combo, the MC-implied hit probability
  vs the realized 0/1 — a reliability curve + Brier score (the calibration report
  already exists conceptually). If the MC systematically under- or over-states hit
  frequency, the ρ or marginals are off.
- **Backtest the VaR:** over many days, the fraction of days where realized book
  P&L breached the predicted VaR_0.99 must be ≈1% (a Kupiec/Christoffersen
  coverage test). This is the honest, cross-validating check that the *tail*
  (not just the mean) is right — and it is the one number a NO-seller must trust.
  Cluster by game/week (`NOTES.md` alarms are "multi-week, game-clustered").

---

## 6. QUOTE-TIME MARGINAL RISK — the real-time path

The risk engine's hot-path question is **not** "what is the book's ruin prob"; it
is "if I add *this* candidate combo, does any aggregate breach a limit?" Today
that is answered *analytically* and cheaply:

- `risk/exposure.py analytic_leg_deltas` uses the **independence product formula**
  for per-leg deltas (`NOTES.md` E1: "hot path = independence product; missing
  marginal ⇒ UNKNOWN never zero; conditional-MC deltas reserved for slow
  full-book refresh"), aggregated per market/event with a **mass-acceptance**
  worst case (`exposure.py:184`, `limits.py`).
- `sim.engine.leg_deltas` (conditional-resampling MC deltas) and
  `marginal_impact` (CRN with/without) are the **slow, correct** refresh.

**Enhancement, respecting the isolation rule (`CLAUDE.md` #8 — sim is not on the
sub-millisecond quote path):**

- Keep the analytic independence delta on the sub-ms quote path (it is a
  conservative first-order screen and it is fast).
- Run the **CRN `marginal_impact`** MC (already CRN, §2.3) with a **control
  variate** (§2.2) and a **small path count (10^3–10^4)** on the *maintenance
  tick* (not per quote) to refresh the true correlated marginal impact and the
  per-event ES contribution — this is where the "the engine must actually be
  used" mandate lands. Budget: single-digit ms per candidate, vectorized.
- The quote-time delta MC must use the **same seed** as the standing snapshot so
  with-minus-without is CRN (variance ~0 on the shared part) — otherwise two
  independent noisy MC numbers subtracted swamp the signal.
- Feed the MC's per-event **worst-case loss** and **ES contribution** into
  `limits.py` alongside the existing `worst_case_loss_by_event_cc` — this is how
  the concentration finding (game = risk unit) becomes an enforced limit rather
  than a post-hoc observation.

---

## 7. PERFORMANCE — vectorization, batching, budgets

### 7.1 What's already good

`sample_leg_values` is fully vectorized: one `standard_normal((n, n_legs))`, one
matmul by `chol.T`, vectorized `ndtr`, and `searchsorted` per leg. `_book_pnl`
loops positions but each `_position_pnl` is a vectorized `prod`/`minimum` over the
`(n, legs)` matrix. This is the right shape.

### 7.2 Enhancements

- **Block Cholesky (§1.3):** replace the single `O(L^3)` factor with per-game
  `O(g^3)` factors; concatenate correlated block draws. For a book with hundreds
  of legs across dozens of games this is the difference between a dense
  hundreds×hundreds Cholesky and a stack of 3×3s.
- **Precompute & cache** each game's Cholesky/`nearest_psd` once per snapshot (ρ
  doesn't change within a batch run); reuse across all combos touching that game.
- **Vectorize the position loop** where the book is large: represent positions as
  a sparse `(positions × legs)` selection and compute payouts with a segment
  product / `np.multiply.reduceat`, or `einsum`, to avoid the Python-level loop
  over positions. Keep the readable loop for small books; switch to the batched
  form above a size threshold.
- **Real-time budget:** the HVM confirm window is 3 s and last-look local decision
  is already ~0.9 ms (`NOTES.md` Phase 5). A quote-time marginal MC must be
  **milliseconds**: prefer the analytic joint (`gaussian_copula_joint_prob`,
  ~tens of ms even at n≤4 per `copula.py` timing) or a `10^3`-path CRN+control MC.
  Structural pricing is ~47 ms (`NOTES.md` I10) against a 500 ms pricing budget —
  a small marginal MC fits comfortably *if* kept off the per-tick critical path
  and run on the maintenance cadence.
- **Batch budget:** IS makes nightly VaR/ES feasible at `~10^5–10^6` paths; with
  block sampling and cached factors that is seconds, not the `10^8` naive path.
- **float32 for the tail scan** (optional): payoff/threshold comparisons for VaR
  can run in float32 to halve memory bandwidth on huge batches; keep float64 for
  the cc-accurate P&L accumulation (money precision, `core/money.py`).

---

## 8. TOP RECOMMENDATIONS, RANKED BY IMPACT ON TAIL-RISK ACCURACY

| # | Recommendation | Why it's ranked here | Touches |
|---|---|---|---|
| **1** | **Importance sampling (Glasserman–Li two-step) on the shared-game factors, shift auto-tuned by cross-entropy, for batch VaR/ES/P(ruin).** | The *only* way to make the sell-side ruin/ES numbers precise; naive MC needs `10^8`+ paths for a `10^-3` event. 2–3 orders of magnitude variance reduction in exactly this model class. | new sim path; `limits.py`, `report.py` |
| **2** | **Block-structured, game-keyed correlated sampling** (independent cross-game blocks, correlated within-game), reusing the pricer's own ρ blocks. | Makes the GAME the risk unit (the P&L-sweep finding), makes independence exact, is the factor structure IS exploits, and fixes the `report.py` independence bug. Prerequisite for #1. | `sim/engine.py`, `report.py` |
| **3** | **VaR/ES coverage backtest against settlement (Kupiec/Christoffersen), game/week-clustered.** | The tail number is only trustworthy if realized breaches ≈ predicted. This is the settlement-graded ruler (`NOTES.md` #3/#5) and validates #1/#2 aren't confidently wrong. | new validation report |
| **4** | **MC↔analytic parity gate in CI** (small-n joint MC == `gaussian_copula_joint_prob` within 3·SE; PSD-repair parity). | Catches L10-class quiet failures where one side's ρ silently dies. Cheap, high-leverage insurance on both the tail and the mean. | `test_sim_engine.py` |
| **5** | **Control variate = analytic combo fair** for EV / MTM / marginal-impact estimates. | 10–100× variance cut on the *mean* outputs the operation is graded on, for ~0 extra cost (reuse the pricer). Helps mean, not tail — hence below the tail items. | `sim/engine.py`, `report.py` |
| **6** | **Actually run the MC in the risk loop:** CRN `marginal_impact` (+control, `10^3–10^4` paths) on the maintenance tick feeding per-event ES into `limits.py`. | The sweep showed the engine wasn't used and caps were absent; this wires correlated marginal risk into enforcement without touching the sub-ms path. | `risk/limits.py`, `sim/engine.py` |
| **7** | **Emit CIs everywhere** (EV ±2σ/√n as report.py already promises; quantile & ES SE/bootstrap; IS ESS + `E[W]=1` guard, fail-closed on ESS collapse). | Turns silent point estimates into honest, guardable numbers; prevents a mis-fit IS density from quietly understating ruin. | `sim/engine.py`, `report.py` |
| **8** | **Scrambled-Sobol QMC + antithetics for the smooth mean/fair** (batch and small real-time). | 5–50× effective-sample gain on EV/fair; deterministic under a scramble seed. Muted in the tail, so it rides below the tail work. | `sim/engine.py` |
| **9** | **Fix the UNKNOWN-marginal `p=0.5` placeholder in `report.py`** to a fail-closed unusable-stat. | Small but it's a live violation of the UNKNOWN-is-never-safe rule (`NOTES.md` E1) sitting in the one live MC caller. | `report.py` |

**Impact tiers:** #1–#4 are what actually move **tail-risk accuracy** (the M2
brief's success metric) — do these first. #5–#8 are correctness/efficiency
multipliers on the mean and the loop. #9 is a bug fix.

---

## Sources

- Glasserman & Li, *Importance Sampling for Portfolio Credit Risk*, Management
  Science 2005 — the two-step factor-copula IS this design maps onto:
  https://business.columbia.edu/sites/default/files-efs/pubfiles/1368/Glasserman_importance_sampling.pdf
- Latent Gaussian copula models for binary data (bridge/tetrachoric function),
  J. Multivariate Analysis 2022:
  https://www.sciencedirect.com/science/article/abs/pii/S0047259X21002049
- Owen, *Monte Carlo Theory, Methods and Examples* (QMC, control variates,
  antithetics, stratification): https://artowen.su.domains/mc/
- Cross-entropy method / safe CE-based importance sampling for rare events (2025):
  https://arxiv.org/abs/2509.07160
- CE-IS with failure-informed dimension reduction (SIAM/ASA JUQ):
  https://epubs.siam.org/doi/10.1137/20M1344585

**NEXT STEPS:**
- **Owner: engineering.** Prototype block-keyed sampling (#2) in a `tools/` test
  script per `CLAUDE.md` #8 (import the live `build_block_corr` / `sgp` builder —
  never reimplement ρ), parity-check its game-block hit-rates against
  `gaussian_copula_joint_prob` to the SE, then port to `sim/engine.py` and add the
  CI gate (#4).
- **Owner: engineering.** Build the Glasserman–Li + CE IS path (#1) behind a
  naive-vs-IS agreement band; do not make IS the number of record until #3's
  coverage backtest and the `E[W]=1`/ESS guards pass.
- **Owner: operator decision.** (a) VaR/ES confidence levels and the ruin
  threshold that defines "fatal" for this bankroll ($1.8M premium vs $23.5M max
  payout) — needed to set the IS target loss level and the per-game worst-case
  caps in `limits.py`. (b) Real-time budget ceiling for the maintenance-tick
  marginal MC (path count vs latency). (c) Whether to gate quoting on a per-game
  ES limit (turns the concentration finding into an enforced control) — this is a
  policy call, not just a modeling one.
