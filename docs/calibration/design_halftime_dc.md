# Design — Half-Time Extension of the Dixon-Coles Scoreline Pricer

**Status:** DESIGN ONLY (additive doc; no `src/`/`config/` edits — hard rule 2).
Written 2026-07-07. Target module `src/combomaker/pricing/dixon_coles.py` +
adapter `src/combomaker/pricing/structural.py`. Validated numerically (scratch,
not committed) — see §8.

## 0. Problem

`dixon_coles.py` enumerates terminal match states carrying only 90' goals
(`a90`,`b90`) plus an ET stack (`a_et`,`b_et`). It has **no half-time state**, so
the whole 1H×FT family cannot price structurally and falls to the blind copula
prior:

- 1H legs: 1H result (HTR), 1H total, 1H BTTS;
- comebacks / HT→FT reversals ("1H A-lead ∩ FT B-win");
- HT/FT doubles ("A leads at HT and wins FT");
- multi-leg mixes ("1H A-lead × FT A-win × FT over-2.5").

This doc extends the terminal-state enumeration so FT = 1H + 2H and every leg
above reads off **one coherent scoreline grid** — the way FT-only already does —
with the joint exact to all orders (not a pairwise-rho stitch), the FT grid
preserved bit-for-bit, and the one new parameter (the first-half goal share `h`)
banded and its uncertainty priced.

---

## 1. Research — the standard structural approach (cited)

### 1.1 First-half goal share `h ≈ 0.44–0.46`

Goals split slightly toward the second half; the empirical first-half **share**
of goals is the single new quantity the extension needs.

| source | 1H : 2H split | first-half share |
|--------|---------------|------------------|
| English pyramid (PL+Champ+L1+L2, 2016/17–2020/21) | 44.3 : 55.7 | **0.443** |
| Europe top-5 leagues (2024/25 sample, 965 goals) | 48 : 52 | 0.48 |
| — Bundesliga | 51 : 49 | 0.51 |
| — Premier League | 50 : 50 | 0.50 |
| — Ligue 1 | 48 : 52 | 0.48 |
| — Serie A | 45 : 55 | 0.45 |
| — La Liga | 43 : 57 | **0.43** |
| Betting-industry HT/FT calculators (convention) | 45 : 55 | **0.45** |

The commercial HT/FT fair-odds calculators use exactly the construction this doc
proposes: `λ_1H = λ_total × 0.45`, `λ_2H = λ_total × 0.55`, an **independent**
Poisson draw per half, then combine into the 9-cell HT/FT matrix (`gamblingcalc`
HT/FT calculator; `pinnacleoddsdropper` HT/FT explainer). Our own football-data
calibration (`docs/calibration/results_soccer.md`) is internally consistent with
`h ≈ 0.45–0.47`: P(1H over 0.5)=0.720 ⇒ P(1H total=0)=0.28 ⇒ λ_1H≈1.27 against a
league FT total λ≈2.7 ⇒ share ≈ 0.47.

**Recommendation:** banded constant `h = 0.45`, band `±0.03` (covers the
0.43–0.51 league spread). Reasoning in §6.

Sources:
- https://www.bettingoffers.org.uk/football/are-more-goals-scored-in-the-first-or-second-half-in-football/ (44.3/55.7, five seasons)
- https://www.sportingpedia.com/2024/10/17/first-vs-second-half-goal-distribution-across-europes-top-5-leagues-scoring-patterns-of-all-96-teams/ (per-league splits)
- https://gamblingcalc.com/betting/football/half-time-full-time-calculator/ and https://www.pinnacleoddsdropper.com/blog/half-time-full-time-bet (0.45 split, 9-cell HT/FT matrix)

### 1.2 Independent-increment / time-inhomogeneous Poisson

Goals in the two non-overlapping intervals [0,45'] and [45',90'] are modelled as
**independent** Poisson counts summing to the match rate. This is the defining
property of a (possibly time-inhomogeneous) Poisson process: counts over
disjoint intervals are independent, and the count over a union is the sum of the
sub-counts (Sigman, *Notes on the Poisson Process*, Columbia IEOR; standard
result). Two consequences the design leans on:

1. **Splitting.** If `N ~ Poisson(λ)` total goals for a team and each goal
   independently falls in the first half with prob `h`, then 1H and 2H counts are
   independent `Poisson(λh)` and `Poisson(λ(1−h))`, and — conditional on the
   90' total `N=m` — the 1H count is `Binomial(m, h)`. (Poisson thinning /
   splitting theorem.) This is precisely the multinomial-thinning logic the
   module already uses for **player** shares (`_player_group_factor`) — the same
   idea applied on the time axis instead of the player axis.
2. **Superposition.** `Poisson(λh) + Poisson(λ(1−h)) = Poisson(λ)` exactly, so
   the 90' marginal is untouched by the split (proven in §3.2).

Empirically the independent-increment assumption is a *first-order* truth: there
is weak positive serial correlation between halves ("open games stay open";
leading teams defend), documented e.g. by J. Grayson, *Are first-half goals
predictive of the second half?* — the residual that §6 bands and §7 prices.

Sources:
- https://www.columbia.edu/~ks20/4106-18-Fall/Notes-PP.pdf (independent increments, splitting/superposition)
- https://www.probabilitycourse.com/chapter11/11_1_2_basic_concepts_of_the_poisson_process.php
- https://jameswgrayson.wordpress.com/2013/12/31/are-goals-scored-in-the-first-half-predictive-of-goals-scored-in-the-second/ (weak positive inter-half serial correlation)

### 1.3 The Dixon-Coles `τ` is a FULL-MATCH low-score effect

The Dixon-Coles `ρ` correction inflates/deflates only the four lowest **final**
scoreline cells (0-0, 1-1 up; 1-0, 0-1 down), fixing the independent-Poisson
under-count of draws. It is defined and fitted on the **final 90' scoreline**
(Dixon & Coles 1997; our `dc_rho = −0.05` was grid-MLE-fitted on train-season
*final* scorelines, `config.py:294`, `tools/validate_structural_oos.py`). It is
**not** a per-half effect and there is no literature basis for a per-half `τ`.
Design consequence (§3.3): attach `τ` at the 90' aggregate, exactly once.

Sources:
- https://grokipedia.com/page/DixonColes_model and https://dashee87.github.io/football/python/predicting-football-results-with-statistical-modelling-dixon-coles-and-time-weighting/ (τ acts on final low-score cells)

### 1.4 How books/exchanges price HT/FT & comebacks

Sportsbooks derive the 9-cell HT/FT market and comeback prices from the **same**
correct-score matrix, not from a separate HT/FT model: split match xG 45/55,
Poisson each half, take the outer product of the two half-matrices, and read HT
and FT off the combined grid (`gamblingcalc`, `pinnacleoddsdropper`, above).
HT/FT carries a fat margin (9 outcomes) precisely because it is a *derived*,
correlation-heavy market — which is where a coherent-grid maker has edge over a
copula stitch. Our design is the same construction, but inverted **from** live
leg prices rather than pushed forward from a team-strength model.

---

## 2. Notation

- `lam_a`, `lam_b` — the two teams' 90' scoring rates (inverted from FT legs, as
  today). Labels A/B are whatever the ticker names; the adapter resolves them by
  end-anchoring (`_team_of`). For exposition below **A = home** so the
  calibration targets line up; the model itself is label-symmetric (see the
  **frame note**, §5).
- `h` — first-half goal share (new; banded constant, §6). `λ_a1 = lam_a·h`,
  `λ_a2 = lam_a·(1−h)`, `λ_b1 = lam_b·h`, `λ_b2 = lam_b·(1−h)`.
- A terminal state gains per-half goals `(a_1h, a_2h, b_1h, b_2h)` with derived
  `a90 = a_1h+a_2h`, `b90 = b_1h+b_2h`; ET stack `(a_et,b_et)` unchanged.

---

## 3. Construction (math)

### 3.1 The 4-D pre-τ joint

For a state `s = (i1, i2, j1, j2)` (A's 1H/2H goals, B's 1H/2H goals):

```
w0(s) = pois(i1; λ_a1) · pois(i2; λ_a2) · pois(j1; λ_b1) · pois(j2; λ_b2)
```

four independent Poissons (§1.2). Aggregate `a90 = i1+i2`, `b90 = j1+j2`.

### 3.2 90' marginal invariance (superposition)

Marginalising `w0` over the 1H/2H split of a fixed 90' cell `(m,n)`:

```
Σ_{i1+i2=m} pois(i1;λ_a1)pois(i2;λ_a2) = pois(m; λ_a1+λ_a2) = pois(m; lam_a)
```

(Poisson convolution / superposition), and likewise for B. So the pre-τ 4-D
joint collapses **exactly** to the plain-Poisson 90' outer product — the same
grid `_dc_grid` builds before it applies `τ`. `h` drops out of the 90' marginal
entirely. This is the load-bearing fact for identification (§6) and regression
safety (§4).

### 3.3 Attaching `τ` — at the 90' aggregate, once

Apply the existing DC multipliers as a function of the aggregate cell only:

```
τ(a90,b90) = 1 − lam_a·lam_b·ρ   if (a90,b90)=(0,0)
             1 + lam_a·ρ         if (0,1)
             1 + lam_b·ρ         if (1,0)
             1 − ρ               if (1,1)
             1                   otherwise
w(s) = clip( w0(s) · τ(a90(s), b90(s)), 0, None );   normalise Σ_s w(s) = 1
```

Because `τ` depends only on `(a90,b90)` and is **constant across the 1H/2H
splits inside a cell**, two properties hold simultaneously:

- **FT grid preserved exactly.** `Σ_{split} w(s) = τ(m,n)·pois(m;lam_a)·pois(n;lam_b)`
  = the pre-normalisation `_dc_grid[m,n]`; same normaliser ⇒ **bit-identical FT
  grid**. Verified: `max|4D-collapsed − _dc_grid| = 1.0e-9` (§8). Every existing
  FT leg (moneyline/draw/BTTS/total/player) prices *identically* — the extension
  is purely additive.
- **Within-cell split undistorted.** Conditional on `a90=m`, the 1H count `i1`
  keeps its `Binomial(m, h)` law (a constant cell multiplier cancels in the
  conditional). So 1H legs see the correct split and FT legs see the correct FT
  grid at the same time.

**Rejected alternatives** (the directive asks whether τ goes on 1H, 2H, or full
match): applying τ to each half **doubles** the low-score correction and breaks
the OOS-gated FT calibration (`dc_rho` was fitted against final scorelines);
applying it to 2H only is ad-hoc and also perturbs the FT marginal. Full-match /
aggregate attachment is the only choice that keeps `dc_rho` meaning what it was
fitted to mean. → **τ attaches to the full match, exactly once, on the
aggregate cell.**

### 3.4 ET composition (knockout)

ET is a further independent increment, orthogonal to the 1H/2H split: it is
entered **only** on a drawn 90' and never touches 1H (a regulation phase). The
extended enumeration therefore:

1. builds the 4-D 90' states `s` with weights `w(s)` (§3.1–3.3);
2. **non-draw** states (`i1+i2 ≠ j1+j2`) terminate with `a_et=b_et=0`;
3. **draw** states (`i1+i2 == j1+j2`) each fan out over the existing ET grid
   (`pois(·; lam_a·et_factor) ⊗ pois(·; lam_b·et_factor)`), carrying their
   `(i1,i2,j1,j2)` unchanged.

This is a one-line generalisation of the current fan-out: the draw predicate
changes from `a90 == b90` on 2-D cells to `(i1+i2) == (j1+j2)` on 4-D states;
ET machinery, `et_factor`, and `pens_win_a` are untouched. `include_et` legs add
`a_et`/`b_et` to `a90`/`b90` exactly as today; 1H legs never read the ET stack.

---

## 4. Integration points (exact symbols)

All changes are additive; the FT-only path is left byte-for-byte identical and is
kept as the default (see §9 gating).

### 4.1 `_States` (dataclass, dixon_coles.py:134) — extend fields

Add `a_1h, b_1h` (int arrays). Keep `a90, b90` as **stored** fields (`= a_1h+a_2h`
etc.) so `_team_indicator`'s existing FT reads are unchanged — 2H is implied
(`a_2h = a90 − a_1h`) and need not be stored. FT-only enumerations set
`a_1h=b_1h = −1` (sentinel "no half state"); any half-leg indicator asserts the
sentinel is absent or raises `StructuralError` (honest-failure, never a silent
0). ET fields unchanged.

### 4.2 `_dc_grid` (dixon_coles.py:145) — unchanged; add `_dc_grid_ht`

Leave `_dc_grid` as the FT-only fast path. Add a sibling that returns the 4-D
weight tensor of §3.1–3.3 (indexed `[i1,i2,j1,j2]`, per-half cap
`half_max = min(max_goals, 8)` — P(>8 goals in a half) ≈ 1e-6 at league rates, so
the cap is exact to float; keeps the tensor at `9⁴ ≈ 6.6k` vs a naive `13⁴`).
The `τ` reweight is the existing four-cell block, gated on the aggregate indices.

### 4.3 `_states` (dixon_coles.py:167) — add half-aware branch

`_states` gains a parameter (e.g. `with_halves: bool`, threaded via a field on
`ModelParams` or a second cache key) selecting the FT-only 2-D enumeration
(today) or the 4-D one. The 4-D group build ravels the tensor; the 4-D knockout
build applies the fan-out of §3.4. `lru_cache` still memoises per param set. The
FT-only branch is returned whenever no half leg is present (§9).

### 4.4 New leg specs (dixon_coles.py, near line 108) — additive

Mirror the FT specs, reading the 1H sub-counts. Frozen/slotted like the rest:

```
@dataclass(frozen=True, slots=True)
class HalfResult:        # YES = `team` leads at half-time (HTR == team)
    team: Team
@dataclass(frozen=True, slots=True)
class HalfDraw: ...      # YES = level at half-time (a_1h == b_1h)
@dataclass(frozen=True, slots=True)
class HalfTotalOver:     # YES = a_1h + b_1h >= min_total  (1H over-0.5 ⇒ 1)
    min_total: int
@dataclass(frozen=True, slots=True)
class HalfBtts: ...      # YES = a_1h >= 1 and b_1h >= 1
```

Extend `LegSpec` union and `_TEAM_LEVEL` (these are 1H **team-level**
constraints — they participate in inversion when `h` is fitted, §6).

### 4.5 `_team_indicator` (dixon_coles.py:218) — add half branches

New branches returning float indicators off `states.a_1h`/`states.b_1h`:

```
HalfResult(A):    a_1h >  b_1h
HalfDraw:         a_1h == b_1h
HalfTotalOver(k): a_1h + b_1h >= k
HalfBtts:         (a_1h >= 1) & (b_1h >= 1)
```

Each asserts `states.a_1h` is populated (not the sentinel) else raises
`StructuralError("half leg needs the half-time enumeration")`.

### 4.6 `joint_probability` (dixon_coles.py:294) — essentially unchanged

The core loop already is "for each leg, multiply a per-state indicator into
`factor`, then sum". It needs only to build the half-aware `_states` when any
leg is a half spec (one predicate at the top). **Nothing else changes** — this is
why the state-sum architecture was the right shape: adding a dimension to the
state does not touch the joint algebra. Player thinning (`_player_group_factor`)
still reads `a90`/`b90` and is untouched (player-in-1H is a future extension, §10).

### 4.7 Adapter `structural.py` — parse 1H tickers, new config fields

`_parse_leg` (structural.py:131) gains cases mapping the 1H market families to
the new specs, once the live 1H ticker shapes are observed (e.g. a
`KXWC…1H…`/`…HALF…` series or a `LegType.FIRST_HALF_*`). Until a real 1H ticker
is confirmed, `classify_leg` returns `UNKNOWN` and the adapter declines →
copula fallback (quiet-failure defense #2 already covers this; no code needed to
stay safe). `StructuralConfig` gains `half_share: float = 0.45` and
`half_share_band: float = 0.03`; `_price`'s `solve()` (structural.py:251) gains a
`half_share` override and the `form_probes` loop (structural.py:291) gains two
probes at `h ± half_share_band` (§7).

---

## 5. Worked examples — the joint is EXACT, not pairwise

`joint_probability` computes, over the shared 4-D grid,
`P(⋂ⱼ Lⱼ) = Σ_s w(s) · ∏ⱼ 1[Lⱼ(s)]`. Every leg — 1H or FT — is a **deterministic
function of the same state `s`**, so the result is the true joint to all orders.
There is no `ρ`, no pairwise term, no higher-order correction to omit.

**1H legs (marginals).** `P(1H A-lead) = Σ_s w(s)·1[a_1h>b_1h]`;
`P(1H over-1.5) = Σ w·1[a_1h+b_1h≥2]`; `P(1H BTTS) = Σ w·1[a_1h≥1 ∧ b_1h≥1]`.

**Comeback cell** ("1H A-lead ∩ FT B-win"):
`P = Σ_s w(s)·1[a_1h>b_1h]·1[b_1h+b_2h > a_1h+a_2h]` — a single grid sum. The
negative dependence (a 1H lead makes an FT loss *less* likely) is baked into the
shared state; a copula would need a signed `ρ` prior for this exact pair.

**HT/FT double** ("A leads at HT and wins FT"):
`P = Σ w·1[a_1h>b_1h]·1[a90>b90]`.

**3-leg** ("1H A-lead × FT A-win × FT over-2.5"):
`P = Σ_s w(s)·1[a_1h>b_1h]·1[a90>b90]·1[a90+b90≥3]`.
A pairwise-copula pricer needs **three** rhos (1Hlead×FTwin, 1Hlead×over,
FTwin×over) **plus** an unmodelled three-way interaction to match this; the grid
delivers all three pairwise correlations *and* the triple interaction from one
sum. That is the structural edge, and it grows with leg count (mirrors the
FT-only OOS gate result, `config.py:283` — margin biggest on the 3-leg triple).

**Frame note (known failure mode).** The comeback and HT/FT legs are
*orientation-sensitive*: "1H A-lead ∩ FT B-win" ≠ "1H B-lead ∩ FT A-win". The
adapter must attach each leg's team to the correct `Team.A`/`Team.B` via the
existing end-anchoring `_team_of`, exactly as FT moneyline does; a swap silently
flips home/away comebacks. This is the soccer analogue of the margin/total frame
bug logged in `CLAUDE.md` (NOTES L1) — call it out in the adapter and cover it
with an orientation test. The pure model is label-symmetric; only the adapter
carries the home/away meaning.

---

## 6. Identification — is `h` invertible, or a banded constant?

**`h` is completely unidentified from FT-only legs.** By the invariance theorem
(§3.2) the FT grid does not depend on `h` at all, so no set of FT legs
(moneyline, draw, BTTS, totals, players) constrains it. `h` is identified **only**
by 1H legs.

Counting the RFQ (unknowns `lam_a, lam_b, h`):

| legs present | constraints | identifies |
|--------------|-------------|------------|
| ≥2 FT team-level, 0 1H | 2 | `(lam_a, lam_b)` only; `h` free |
| 2 FT + 1 1H-total | 3 | `(lam_a,lam_b,h)` exactly (1H-total pins `h·(lam_a+lam_b)`, lams already pinned) |
| 1 FT + 1 1H (typical 2-leg combo) | 2 | under-determined for 3 unknowns |
| ≥2 FT + ≥2 1H | ≥4 | over-determined; `h` fittable, misfit priced |

The common 1H×FT combo is a **2-leg** RFQ (one 1H leg, one FT leg) → 2
constraints, 3 unknowns → `h` **cannot** be inverted robustly. Even the
3-constraint case pins `h` off a single 1H leg, where any marginal noise on that
leg feeds straight into `h` and trades against the lam split.

**Recommendation — banded constant `h = 0.45 (±0.03)` as the pricing default;
never invert `h` from a single 1H leg.** Promote `h` to a fitted parameter *only*
when the system is genuinely over-identified for all three unknowns (≥2 FT
team-level **and** ≥2 independent 1H legs), and even then keep the banded prior
as the warm start and price the fit's residual (as `invert()` already does for
over-identified lam systems, dixon_coles.py:397). In the far more common
under-identified case, use co-present 1H legs as a **consistency check, not an
inversion**: compute the 1H leg's model price at `h=0.45` and, if it disagrees
with the market marginal by more than the band can absorb, **widen or no-quote**
(the leg set contradicts the banded share — honest-failure, not a convenient
default). This mirrors the module's existing "residual misfit → width" discipline
(structural.py:318) and avoids a fragile single-leg `h` solve destabilising the
lam fit.

---

## 7. Uncertainty — band `h`, price it (don't hope it away)

`h`'s band propagates by **re-pricing the joint at the band edges**, identical to
how `dc_rho_band` and the ET-intensity band are already probed in
`StructuralPricer._price` (structural.py:291–303). Concretely, extend the
`form_probes` loop:

```
for hh in (cfg.half_share - cfg.half_share_band, cfg.half_share + cfg.half_share_band):
    try: form_probes.append(solve(constraints, half_share=hh)[1])
    except StructuralError: pass
form_unc = max(|fp - p| for fp in form_probes)   # already aggregated this way
```

so the priced width absorbs the ±0.03 share uncertainty **plus** the residual
inter-half serial correlation the independent-increment model omits (§1.2). The
`h`-band is only ever exercised on combos that actually contain a 1H leg (the
FT-only path never builds the 4-D grid and never probes `h`), so FT pricing width
is unchanged. Band sizing `±0.03` is chosen to cover the 0.43–0.51 league spread
(§1.1); widen per-league later if a live 1H book shows a tighter/looser share.
This keeps the extension inside the repo's "uncertainty priced, not hoped away"
contract (dixon_coles.py:24).

---

## 8. Validation targets (must reproduce; wire later)

The extension must reproduce the football-data calibration in
`docs/calibration/results_soccer.md` **without** any fitted pairwise rho — the
conditionals must *emerge* from the shared grid. Two protocols:

**(a) Rigorous (later):** for each of the 8,981 club matches, invert
`(lam_a, lam_b)` from its closing 1X2 + O/U-2.5 lines (as
`tools/calibrate_soccer_firsthalf.py` already loads), build the 4-D grid at
`h=0.45, dc_rho=−0.05`, and **pool** these model conditionals across matches.
Targets (empirical, results_soccer.md §1):

| conditional | empirical target | must land within |
|-------------|------------------|------------------|
| P(FT home-win \| HT home-leader) | **0.767** | ±0.03 |
| P(FT over2.5 \| 1H over1.5) | **0.876** | ±0.03 |
| P(FT over2.5 \| 1H over0.5) | **0.670** | ±0.03 |
| P(FT away-win \| HT away-leader) | 0.685 | ±0.03 |
| base P(1H home-lead) | 0.336 | ±0.02 |
| base P(1H over1.5) | 0.361 | ±0.02 |

**(b) Sanity anchor (already run, scratch).** A single representative league
match `(lam_a=1.50 home, lam_b=1.15 away, h=0.45, dc_rho=−0.05)` and a 5-point
heterogeneity mixture give:

| quantity | model (single) | model (pooled mix) | empirical |
|----------|----------------|--------------------|-----------|
| 90'-marginal invariance vs `_dc_grid` | **max err 1.0e-9** | — | (exact) |
| P(1H home-lead) | 0.339 | — | 0.336 |
| P(1H over1.5) | 0.336 | — | 0.361 |
| P(FT home-win \| 1H home-lead) | 0.795 | 0.810 | 0.767 |
| P(FT over2.5 \| 1H over1.5) | 0.847 | 0.857 | 0.876 |
| P(FT over2.5 \| 1H over0.5) | 0.632 | — | 0.670 |

The structural conditionals land within ~2–4 points of the empirical values
using **only** the independent-increment split — no fitted 1H×FT rho. The sign of
the residual is itself informative and must be reproduced: the pure model
slightly **over**-states lead persistence (0.795–0.810 vs 0.767 — real leads are
more fragile) and slightly **under**-states over-given-open (0.847–0.857 vs 0.876
— real openness has positive serial correlation, §1.2). Both gaps are inside the
`h`-band width of §7; a validation run that lands *outside* ±0.03 after pooling on
real inverted lambdas signals the band is too tight or `h` is mis-set, and blocks
enabling 1H legs (mirror the FT-only OOS gate discipline).

**Exactness assertion to test directly:** for any leg set, the extended
`joint_probability` over the 4-D grid must equal a brute-force enumeration of the
same indicators (they are the same sum) — a property test with random
`(lam_a,lam_b,h,ρ)` and random leg subsets, tolerance 1e-12.

---

## 9. Performance / state-space

The 4-D group enumeration is `(half_max+1)⁴ ≈ 9⁴ = 6,561` states vs the FT-only
`13² = 169`; the knockout fan-out multiplies the draw sub-states by the ET grid.
Two mitigations keep the hot path intact:

1. **Lazy gating (primary):** build the 4-D grid **only** when a half leg is
   present. FT-only combos — the overwhelming majority — keep the 169-state 2-D
   path and the existing `lru_cache`. `joint_probability` picks the builder from
   the leg vocabulary. Net cost for existing quotes: zero.
2. **Per-half cap** `half_max = min(max_goals, 8)`: exact to float at league
   rates, bounds the tensor.

Every uncertainty probe (leg bands, `dc_rho`, ET, `h`) still re-enters `_states`
under a new frozen `ModelParams`, so memoisation semantics are unchanged; the
half-flag joins the cache key.

---

## NEXT STEPS

- **Runs next:** nothing auto-runs — design only. When implementation is
  scheduled, land the four new specs + `_dc_grid_ht`/`_states` half-branch +
  `_team_indicator` branches behind the lazy gate (§4, §9), then the §8(a)
  pooled validation harness as a new `tools/` script (additive, imports the
  shipped module) before any adapter parsing goes live.
- **Owner (operator):** decide (a) `half_share` central value 0.45 vs a
  per-league table (La Liga 0.43 ↔ Bundesliga 0.51), and band width ±0.03; (b)
  whether to ever invert `h` (recommendation: no — banded constant + consistency
  check, §6); (c) gate criterion to enable 1H legs = §8(a) pooled conditionals
  within ±0.03 on real inverted lambdas, same bar as the FT-only OOS gate.
- **Blocked on:** live Kalshi 1H ticker shapes (series prefix + suffix grammar)
  before `structural.py:_parse_leg` can classify them; until then `classify_leg`
  returns UNKNOWN and 1H combos safely take the copula fallback (no code needed
  to stay correct).
- **Decision owed by user:** whether the ~2–4pt "leads are more fragile / open
  games stay open" residual (§8) warrants a second-order inter-half serial term
  later, or is adequately covered by the `h`-band (recommendation: band it now,
  revisit only if a live 1H book shows the residual exceeds the band).
