# Ising / pairwise max-entropy AMM — results (RANK 3)

Run date: 2026-07-07. Agent domain: the pairwise-Ising / max-entropy joint
pricer (the ParlayMarket approach) as an **alternative** joint-correlation
layer to the shipped Gaussian copula, plus generalization to cross-prop pairs.

- Module (additive, NOT imported by existing code):
  `src/combomaker/pricing/ising_amm.py`
- Driver (additive new tool): `tools/ising_amm_run.py`
- Reuses the SHIPPED copula (`combomaker.pricing.copula`) and the SHIPPED
  history loaders (`tools/calibrate_pairs_from_history.py`), so every empirical
  moment is byte-identical to the game-level calibration.
- Full suite after adding the module: **726 passed, 3 deselected** (module is
  additive; nothing existing imports it, so the suite is unaffected).

---

## 1. Parameterization used (extracted from the paper)

Source: *"ParlayMarket: Automated Market Making for Parlay-style Joint
Contracts"* (Rana, Nadkarni, Moshrefi, Viswanath), arXiv:2603.22596. The Ising
form is in the body (§5.1), not the abstract; I fetched the HTML and quote the
equations verbatim.

**Joint (their Eq. 11)**, over binary leg outcomes `x = (x_1..x_M)`, `x_i ∈ {0,1}`:

```
P_φ(x) = (1/Z(φ)) · exp( Σ_i θ_i x_i  +  Σ_{i<j} W_ij x_i x_j )
```

- `θ_i` — bias parameters, control the individual (marginal) event probabilities.
- `W_ij` — interaction weights, control the pairwise correlations.
- `φ = (θ, W)` is one shared vector; all `2^M − 1` combination prices flow from it.
- This is the **maximum-entropy** distribution consistent with specified single-
  and pair-marginals; O(M²) sufficient statistics.

**Fixed point (moment matching, §5.1):** `p_i^{φ*} = p_i*` for all `i` and
`p_ij^{φ*} = p_ij*` for all `i<j`.

**Calibration objective — composite pseudo-likelihood (their Eq. 12):**

```
L(φ) = Σ_i λ_i · CE(p_i*, p_i^φ)  +  Σ_{i<j} λ_ij · CE(p_ij*, p_ij^φ)
CE(p, q) = −p·log q − (1−p)·log(1−q)          (binary cross-entropy)
```

**Online update — SGD (their Eq. 13):** `φ_{t+1} = φ_t − η · ∇_φ CE(p_m*, p_m^φ_t)`.

**My implementation choices, stated honestly:**
- For the realistic combo sizes (M = 2..6) I **enumerate all 2^M outcomes
  exactly** rather than using the paper's belief-propagation. Z, every marginal,
  pair-marginal, and arbitrary sub-combination are then exact — no approximation,
  no MCMC, no randomness. (Trivial cost: 2^6 = 64 states.)
- Because `log Z(φ)` is the exp-family cumulant generator, the gradient of each
  CE term w.r.t. **its own** natural parameter collapses to the moment residual:
  `∂CE(p_i*,·)/∂θ_i = p_i^φ − p_i*` and `∂CE(p_ij*,·)/∂W_ij = p_ij^φ − p_ij*`.
  So Eq. 13's SGD step is exactly "move the weight by η·(model moment − empirical
  moment)" — it provably walks the parameter toward the empirical correlation.
  Batch fitting iterates this to convergence (max-ent MLE = moment matching).

---

## 2. Copula vs Ising — agreement across a rho sweep

For each rho: fit the Ising to the **same** pair-marginals the Gaussian copula
produces, then compare the joints. `d(cop−is)` is the residual — the copula's
higher-order (Gaussian tail) structure that a pairwise-only model cannot carry.

```
2-leg marginals p = [0.55, 0.45]   3-leg marginals p = [0.55, 0.5, 0.45]

   rho |  cop P(AB) ising P(AB)       |d| ||  cop P(ABC) ising P(ABC) indep P(ABC)  d(cop-is)
----------------------------------------------------------------------------------------------------
 -0.60 |   0.146332    0.146332  9.80e-13 ||    0.000000     0.000300     0.123750  -0.000300
 -0.40 |   0.182867    0.182867  9.75e-13 ||    0.026485     0.026485     0.123750   0.000000
 -0.20 |   0.215911    0.215911  9.78e-13 ||    0.076164     0.076164     0.123750   0.000000
 -0.05 |   0.239661    0.239661  9.80e-13 ||    0.111932     0.111932     0.123750   0.000000
  0.05 |   0.255333    0.255333  9.75e-13 ||    0.135565     0.135565     0.123750  -0.000000
  0.20 |   0.278987    0.278987  9.66e-13 ||    0.171285     0.171285     0.123750  -0.000000
  0.40 |   0.311683    0.311683  9.66e-13 ||    0.220790     0.220790     0.123750  -0.000000
  0.60 |   0.347436    0.347436  9.83e-13 ||    0.275196     0.275196     0.123750  -0.000000
  0.80 |   0.390161    0.390161  9.86e-13 ||    0.340999     0.340999     0.123750  -0.000000
```

**2-leg: exact.** The Ising reproduces the copula's pairwise joint to ~1e-12
across the entire rho range. This is by identifiability — for M=2 the Ising has
3 free params (θ_1, θ_2, W_12) and the outcome simplex has 3 free probabilities,
so **any** valid (p_A, p_B, p_AB) is reproduced exactly. The Ising is a valid
drop-in replacement for the copula at the 2-leg pairwise layer.

**3-leg: essentially identical for realistic rho.** The triple agrees to ≤1e-6
for every rho ≥ −0.4. The only visible divergence is at rho = −0.6, where the
common-rho 3×3 correlation is not even PSD (eigenvalue 1+2·(−0.6) = −0.2) so the
copula runs on a repaired matrix and pins the triple to the Fréchet floor
(0.000000) while the max-ent gives a tiny 0.000300. Both crush the independence
baseline (0.1238), which is wrong by 30–100%+ at the tails.

**Where they diverge — grows with M** (equicorrelation, p=0.5 each, full parlay):

```
 M    rho  cop parlay ising parlay   abs diff    rel %
 3   0.10    0.148913     0.148913   3.36e-08     0.00
 3   0.50    0.250000     0.250000   9.89e-09     0.00
 3   0.90    0.392325     0.392325   3.14e-09     0.00
 4   0.30    0.140306     0.139984   3.22e-04     0.23
 4   0.50    0.200000     0.199032   9.68e-04     0.48
 4   0.70    0.270685     0.268884   1.80e-03     0.67
 4   0.90    0.369312     0.366885   2.43e-03     0.66
 5   0.30    0.104534     0.103652   8.82e-04     0.84
 5   0.50    0.166667     0.163984   2.68e-03     1.61
 5   0.70    0.243190     0.238148   5.04e-03     2.07
 5   0.90    0.352741     0.345862   6.88e-03     1.95
```

Reading: **2–3 legs agree to floating-point noise**; the gap opens only at
**4+ legs**, reaching ≈0.5% (4-leg) and ≈1–2% (5-leg) of the parlay price under
strong correlation. That residual is the copula's genuine 3-way/4-way tail
dependence, which a strictly pairwise model omits — the Ising consistently sits
**slightly below** the copula on a positive parlay (it does not manufacture the
extra joint-tail mass). At Kalshi cent-resolution on $1 contracts, that is
sub-1¢ for 2–3 legs and roughly 0.5–2¢ for 4–5 legs.

---

## 3. W_ij calibrated from soccer history (cross-prop / game-level pairs)

Club top-5 EU, seasons 20/21–24/25, **8,982 games** (identical dataset and
`measure()` the shipped `calibrate_pairs_from_history.py` uses). For each pair
I fit the Ising's single weight to the empirical P(A∩B) and show it equals the
copula-rho path.

```
pair                        n    P(A)    P(B)    P(AB)  copula rho  Ising W_ij  Ising P(AB)
--------------------------------------------------------------------------------------------
btts x over2.5           8982  0.5469  0.5304   0.4231      0.7461      2.3988     0.423068
home_win x over2.5       8982  0.4270  0.5304   0.2700      0.2760      0.7250     0.269984
btts x home_win          8982  0.5469  0.4270   0.2026     -0.1970     -0.5103     0.202627
```

- Every `Ising P(AB)` reproduces the empirical `P(AB)` to fit tolerance, so
  `implied_rho(Ising P(AB))` equals the `copula rho` column **by construction**.
  `W_ij` and `rho` are two coordinates on the **same** pair-joint.
- **Signs match the research priors** (PLAN.md): btts×over2.5 strongly + (both
  need goals: W=+2.40, rho=+0.75); home_win×over2.5 weakly + (W=+0.73,
  rho=+0.28); btts×home_win **negative** (W=−0.51, rho=−0.20 — a home win is
  often one-sided, the away side fails to score, killing BTTS). No sign flips.

### Three-leg coherent pricing (the key advantage over independent multiplication)

`home_win & over2.5 & btts`, priced from three **pairwise** weights only:

```
n = 8982 games
marginals:  P(home)=0.4270  P(over2.5)=0.5304  P(btts)=0.5469
pairwise :  P(h,o)=0.2700  P(h,b)=0.2026  P(o,b)=0.4231
EMPIRICAL triple P(home & over2.5 & btts) = 0.20263

implied copula rhos:  rho(h,o)=+0.2760  rho(h,b)=-0.1970  rho(o,b)=+0.7461
Ising W_ij        :  W(h,o)=+1.6429  W(h,b)=-1.5052  W(o,b)=+2.9519

method                                    P(triple)   err vs emp
----------------------------------------------------------------
EMPIRICAL frequency                         0.20263     +0.00000
independent multiplication                  0.12384     -0.07878
Ising pairwise max-ent                      0.18628     -0.01635
Gaussian copula (3 implied rhos)            0.18692     -0.01571

Coherence check (Ising sub-prices vs targets):
  marg: [0.427, 0.5304, 0.5469]  (target [0.427, 0.5304, 0.5469])
  pair: h,o=0.2700 h,b=0.2026 o,b=0.4231
  -> one phi vector prices all 7 sub-combinations self-consistently.
```

Reading:
- **Independence is off by −0.0788 (−39%)** — it would badly underprice this
  triple. Both dependence models close ~80% of that gap.
- **Ising (0.18628) and copula (0.18692) agree to 0.0006** and both land ~0.016
  below the empirical (the residual is genuine positive **3-way** dependence
  beyond pairwise, which neither pairwise-only model captures — it is the *same*
  blind spot for both, not an Ising-specific defect).
- The single fitted `φ` prices **all seven** sub-combinations (3 singles, 3
  pairs, 1 triple) self-consistently and reproduces every input moment exactly.
  The three W_ij (note the negative W(h,b) sitting next to two positives)
  interact coherently — you cannot get that from three independent rho lookups
  multiplied together, and it needs no separate 3-way parameter.

Note W_ij ≠ the two-leg weights in §3's table (e.g. W(o,b)=+2.95 here vs
+2.40 pairwise-only): in the joint fit each weight adjusts for the presence of
the third leg. That coupling is the whole point — the model is solved jointly.

---

## 4. Online SGD update demo (self-calibration from one observed trade)

Start from **independence** (W=0, θ seeded to the marginals so P(AB)=P(A)·P(B)),
then stream the observed pair-moment for `btts × over2.5` (η=4.0):

```
pair: btts x over25   empirical target  P(A)=0.5469 P(B)=0.5304 P(AB)*=0.4231
(copula rho for this pair = +0.7461)

start (W=0, independent):  W01=+0.00000  P(AB)^phi=0.29006 (= P(A)P(B)=0.29006)

streaming the observed pair-moment, eta=4.0:
step        W01   P(AB)^phi   resid=P(AB)^phi-P(AB)*
------------------------------------------------------
   1    0.53204    0.410219                -1.33e-01
   2    0.96687    0.414600                -1.09e-01
   3    1.31786    0.417456                -8.77e-02
   ...
  12    2.32816    0.422905                -6.34e-03

converged  W01 -> +2.32816   batch-fit W01 = +2.39884   P(AB)^phi -> 0.42291  (target 0.42307)
```

The residual `P(AB)^φ − P(AB)*` shrinks monotonically toward 0; `W_01` walks
from 0 (independent) toward the batch-fit value (+2.40) and `P(AB)^φ` converges
to the empirical 0.4231. This is the mechanism that would let the layer
self-calibrate on our own combo tape once it is long enough — each realized
fill is one moment observation, and Eq. 13 nudges exactly the weight(s) that
trade touched, toward the empirical joint frequency. (η is a step-size knob;
production would use a small η for a slow EWMA-style track, not convergence in
12 steps.)

---

## 5. Cross-check vs peers

`ls docs/calibration/` at run time showed **only `PLAN.md` and this file** — no
peer `results_soccer/basketball/baseball.md` had been written yet, so there are
no peer rhos to reconcile against. What I *can* cross-check is against the
**shipped** game-level calibration, which is the reference the peers will
mirror:

- My soccer pair rhos are computed with the peers' own `measure()`/`implied_rho`
  on the same 8,982-game club dataset, so they are identical to what
  `tools/calibrate_pairs_from_history.py` prints for `btts×over2.5`,
  `home_win×over2.5`, `btts×home_win`. The Ising `W_ij` I fit **reproduce those
  copula rhos exactly** (§3) — confirming the pairwise-weight parameterization
  and the shipped copula-rho parameterization are the same pair-joint in two
  coordinate systems.
- **Follow-up owed:** once a peer writes `results_soccer.md`, re-diff my
  `W_ij → implied rho` against their reported `pair | rho | 99% CI` rows; they
  must match to ≤0.01 on shared pairs (both invert the same copula). Any gap =
  a frame/label bug on one side.

---

## 6. Recommendation — adopt or not?

**Do NOT replace the shipped Gaussian copula with the Ising layer for our combo
sizes (2–6 legs). Keep the copula as the production joint. Keep the Ising module
on the shelf as (a) a coherence cross-check and (b) the vehicle for future
self-calibration on our own tape.**

Reasoning, from the numbers above:

- At **2–3 legs — which is where the toxic-flow filters force our book to live
  (min_legs=3, and combos are overwhelmingly 2–4 legs)** — the two models are
  numerically indistinguishable (agreement to 1e-6 to 1e-12). There is **no
  pricing edge** to be had by switching; the divergence that would justify a
  migration only appears at 4+ legs (~0.5–2%), and even there it is the copula
  carrying *more* tail mass, not obviously the *more correct* mass — neither
  model captures true 3-way dependence (§3, both miss the empirical triple by
  ~0.016 the same way).
- The **copula is already shipped, tested, PSD-repaired, Fréchet-clamped,
  conditional-price-capable, and OOS-gated** against 8,980 games and the
  structural pricers. Swapping the joint layer would re-open all of that
  validation for a sub-cent difference at our sizes. That is a bad trade against
  the repo's "execution discipline is the edge, not pricing" mission.

**Where the Ising is genuinely worth keeping (additive, not as a replacement):**

1. **Self-calibrating cross-prop pairs (§4).** The copula-rho path needs a batch
   re-fit; the Ising's Eq.-13 SGD updates the *exact* weight a trade touched from
   a *single* observation. When our combo tape is long enough (it is too short
   today — PLAN.md), this is the cleaner online-learning substrate for the
   uncalibrated player-prop / cross-prop pairs.
2. **Coherent multi-way pricing from pairwise inputs with no PSD gymnastics.**
   The Ising is coherent by construction for any weights (it is always a valid
   distribution); it never needs `nearest_psd`. For a future book with many
   overlapping same-game props whose pairwise rhos would produce a non-PSD
   correlation matrix, the max-ent joint is the principled repair — it is the
   *unique* max-entropy joint consistent with those pairwise targets.
3. **Coherence audit.** Run it beside the copula; a >2% triple/parlay
   disagreement at 4+ legs is a useful "higher-order structure is material here"
   flag that says *widen*, per the UNKNOWN-⇒-widen rule.

### Pros / cons

**Pros:** identical to the copula where we trade (2–3 legs) so it is a safe
drop-in; exact & fast by enumeration for M≤~12; always a valid joint (no PSD
repair, coherent for arbitrary pairwise inputs); O(M²) params price all 2^M−1
combos from one vector; the CE/SGD update is a clean per-trade online learner
that provably moves toward empirical correlation.

**Cons:** no pricing edge over the incumbent at our sizes (the whole migration
buys ~0 at 2–3 legs); strictly **pairwise** — it omits true 3-way+ dependence
just like the copula, so it is not "more correct," and at 4–6 legs it carries
*less* tail mass than the copula (which may or may not be desirable — untested
vs settlements); enumeration is 2^M so it does not scale past ~15–20 legs
(irrelevant for combos, fatal for large baskets); would require re-doing the
shipped copula's OOS gate, Fréchet clamp, and conditional-price machinery to
reach production parity.

---

## NEXT STEPS

- **Runs next:** nothing auto-runs. `tools/ising_amm_run.py` is a
  reproduce-on-demand driver; the module is not wired into the pricer.
- **Owner — modeling agent (me), when a peer file lands:** re-run §5 cross-check
  against `results_soccer.md` once written; diff my `W_ij → implied rho` vs their
  `pair | rho | 99% CI`; flag any >0.01 gap as a frame/label bug.
- **Decision owed by operator:** confirm the recommendation — *keep copula in
  production, keep Ising module on the shelf for (a) online self-calibration of
  cross-prop pairs once our tape is long enough and (b) a 4+-leg coherence
  audit*. If instead you want the Ising promoted, it needs its own OOS log-loss
  gate vs the copula on held-out seasons (mirroring the structural-pricer gate)
  before any live use — a sub-cent in-sample match is not a graduation
  criterion (CLAUDE.md rule 5).
