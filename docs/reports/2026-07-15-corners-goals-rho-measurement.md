# Corners ↔ Goals correlation — MEASURED (verdict: shipped 0.00 is right; the 3–5¢ is market richness, fix with a defensive WIDTH)

**Date:** 2026-07-15
**Scope:** MEASUREMENT ONLY. Nothing live changed (operator directive: "measure first, change nothing live"). No `src/combomaker/**` module and no config touched. Hard rule 8 respected — prototype-in-tools only.
**Tool:** `tools/measure_corners_goals_rho.py` (self-contained; reads `data/history/`).
**Motivation:** issue #37 — we underprice corners combos by a measured 3–5¢. The config ships `soccer:corners|total = 0.00`, `soccer:btts|corners = 0.00` (both band 0.08) and `soccer:corners|player_goal = -0.03`. The question: is corners↔goals GENUINELY ~0 at the lines we trade (→ our fair is right, the 3–5¢ is richness/adverse-selection, fix = defensive WIDTH), or is it materially positive (→ 0.00 too low, promote a measured ρ)?

---

## Headline

**Corners ↔ goals is ~0 at every traded line — and the point estimate leans marginally NEGATIVE, not positive.** Over **n = 8,981** club matches (5 top-EU leagues × 5 seasons 20/21–24/25), the raw count-level `Pearson(total_corners, total_goals) = −0.021`, and every traded corners×goals/BTTS line-pair's tetrachoric (Gaussian-copula) ρ sits in **[−0.040, +0.041]**. The marquee pair **corners≥9 × goals≥3 → ρ_tet = −0.038 (95% CI [−0.07, −0.01])**. This decisively refutes the discarded reverse-engineered +0.35/+0.5 an earlier pass had seen (that number is not in the club data), and it fully vindicates the shipped **0.00**.

**Verdict → MARKET-RICHNESS, not promote-ρ.** Corners-over ⊥ goals-over is REAL at these thresholds; our fair is right. The 3–5¢ underpricing is market richness / adverse-selection premium on corners combos, **not** a missing positive ρ. The disciplined fix is a **defensive corners WIDTH**, not a ρ change. Raising ρ toward +0.35 to "explain" the 3–5¢ would be refitting the model to a P&L symptom against the measurement (violates "never refit on a P&L window") — and the measured sign is if anything *negative*, so a positive ρ bump would make the fair *wrong* in the opposite direction.

---

## The measurement

Tetrachoric ρ is the correct apples-to-apples number: combomaker prices a 2-leg combo with `pricing/copula.gaussian_copula_joint_prob`, which turns each leg's YES prob into a latent Gaussian threshold `z = Φ⁻¹(p)` and integrates the bivariate-normal CDF at correlation ρ. The tetrachoric ρ of a 2×2 table is *defined* as the ρ of that same BVN that reproduces the observed `P(A∧B)` given the marginals — so the ρ measured here is on the exact scale of `config.pair_rho`, and a promote (if warranted) would be a like-for-like swap. The tool asserts its forward BVN matches the live copula to **8.1e-06** at import time (parity gate) before measuring.

- `total_corners = HC + AC`, `total_goals = FTHG + FTAG`, `btts = (FTHG≥1 ∧ FTAG≥1)`.
- Traded lines: TOTAL corners ≥ {7,8,9,10} (KXWCTCORNERS over 7/8/9/10) × {goals ≥ 2, ≥ 3, ≥ 4 (over 1.5/2.5/3.5), BTTS}.
- CI95 = 2,000-resample nonparametric bootstrap over matches.

### Pooled (all 5 leagues, n = 8,981)

Pooled means: total_corners = 9.66, total_goals = 2.82. **raw Pearson(total_corners, total_goals) = −0.0211.**

| corners≥ | goals | P(A) | P(B) | P(A∧B) | ρ_tetrachoric | 95% CI | φ (indicator) | vs shipped 0.00 |
|---:|:---|---:|---:|---:|---:|:---:|---:|:---|
| 7 | ≥2 | 0.826 | 0.775 | 0.641 | **+0.019** | [−0.02, +0.06] | +0.009 | CI straddles 0 → defensible |
| 7 | ≥3 | 0.826 | 0.530 | 0.437 | **−0.016** | [−0.05, +0.02] | −0.009 | CI straddles 0 → defensible |
| 7 | ≥4 | 0.826 | 0.314 | 0.257 | **−0.022** | [−0.06, +0.02] | −0.012 | CI straddles 0 → defensible |
| 7 | BTTS | 0.826 | 0.547 | 0.456 | **+0.041** | [ 0.00, +0.08] | +0.022 | CI just excludes 0 (+) |
| 8 | ≥2 | 0.725 | 0.775 | 0.561 | **−0.007** | [−0.05, +0.03] | −0.004 | CI straddles 0 → defensible |
| 8 | ≥3 | 0.725 | 0.530 | 0.379 | **−0.040** | [−0.08, −0.00] | −0.024 | CI just excludes 0 (−) |
| 8 | ≥4 | 0.725 | 0.314 | 0.225 | **−0.025** | [−0.06, +0.01] | −0.014 | CI straddles 0 → defensible |
| 8 | BTTS | 0.725 | 0.547 | 0.399 | **+0.021** | [−0.01, +0.05] | +0.012 | CI straddles 0 → defensible |
| 9 | ≥2 | 0.615 | 0.775 | 0.475 | **−0.008** | [−0.05, +0.03] | −0.005 | CI straddles 0 → defensible |
| **9** | **≥3** | 0.615 | 0.530 | 0.320 | **−0.038** | **[−0.07, −0.01]** | −0.024 | CI just excludes 0 (−) |
| 9 | ≥4 | 0.615 | 0.314 | 0.192 | **−0.007** | [−0.04, +0.03] | −0.004 | CI straddles 0 → defensible |
| 9 | BTTS | 0.615 | 0.547 | 0.339 | **+0.021** | [−0.01, +0.05] | +0.013 | CI straddles 0 → defensible |
| 10 | ≥2 | 0.495 | 0.775 | 0.382 | **−0.010** | [−0.05, +0.03] | −0.006 | CI straddles 0 → defensible |
| 10 | ≥3 | 0.495 | 0.530 | 0.257 | **−0.037** | [−0.07, −0.01] | −0.024 | CI just excludes 0 (−) |
| 10 | ≥4 | 0.495 | 0.314 | 0.153 | **−0.019** | [−0.05, +0.02] | −0.012 | CI straddles 0 → defensible |
| 10 | BTTS | 0.495 | 0.547 | 0.273 | **+0.012** | [−0.02, +0.05] | +0.008 | CI straddles 0 → defensible |

Reading the table:
- **11 of 16** pairs have a CI that straddles 0 → 0.00 flatly defensible.
- The **4** goals≥3 pairs (c≥8/9/10) and c≥7×goals≥3 cluster at **≈ −0.04** — a whisper of *negative* dependence (more corners ⇢ fractionally fewer 3+ goal games; consistent with corner-heavy games being grindy/low-conversion). Even where the CI excludes 0, the magnitude is ~0.04 — inside the shipped **0.08 band** and on the *opposite side* of the +0.35 the P&L symptom would demand.
- **BTTS** pairs lean marginally *positive* (+0.01 … +0.04); only c≥7×BTTS (+0.041) excludes 0. Also well inside the 0.08 band.

Every one of these is a rounding-error correlation. None supports a positive promote.

### Per-league stability (tetrachoric ρ)

|      pair | England | Germany | Italy | Spain | France |
|---:|---:|---:|---:|---:|---:|
| c≥7 × g≥3 | −0.06 | −0.03 | −0.08 | −0.00 | +0.05 |
| c≥8 × g≥3 | −0.07 | −0.06 | −0.08 | −0.04 | +0.02 |
| **c≥9 × g≥3** | **−0.06** | **−0.05** | **−0.09** | **−0.05** | **+0.04** |
| c≥10 × g≥3 | −0.05 | −0.07 | −0.11 | −0.03 | +0.03 |
| c≥9 × g≥2 | −0.07 | +0.01 | −0.02 | +0.01 | +0.01 |
| c≥9 × g≥4 | −0.04 | +0.01 | −0.11 | −0.02 | +0.10 |
| c≥9 × BTTS | +0.02 | +0.08 | −0.02 | −0.01 | +0.05 |

Cross-league dispersion is small and — critically — **not consistently signed**: Italy is the most negative (up to −0.11 at goals≥3), France leans mildly positive (up to +0.10), England/Germany/Spain hug zero. For the marquee c≥9×g≥3 the five leagues span **[−0.09, +0.04]** (spread 0.13), centered −0.04. No league produces a materially positive corners↔goals link; the largest single-league magnitude anywhere in the grid is Italy's −0.11. The shipped 0.08 band already spans this dispersion.

---

## Verdict, stated with the numbers

**MARKET-RICHNESS, not promote-ρ.** At every traded line, corners-over ⊥ goals-over holds in the club data (ρ_tet ∈ [−0.04, +0.04], count Pearson −0.021, marquee c≥9×g≥3 = −0.038 CI [−0.07,−0.01]). The shipped `corners|total = 0.00` / `btts|corners = 0.00` (band 0.08) and `corners|player_goal = −0.03` (band 0.10) are all fully defensible — indeed the point estimate is marginally *negative* on the goals-over side, so the shipped 0.00 is already very slightly *generous to the buyer* on those cells, not stingy.

Therefore the #37 3–5¢ underpricing on corners combos is **NOT** a missing positive correlation. It is market richness / adverse-selection premium — makers charge up for corners combos for reasons orthogonal to the corners↔goals joint (line/vig richness, corner-market thinness, informed late-corner flow). The disciplined response is a **defensive additive WIDTH on corners combos** (quote a bit wider / require a bit more edge when a corners leg is present), which raises our ask toward the market without corrupting the fair. It is emphatically **not** a ρ bump: pushing ρ to +0.35 to close the 3–5¢ would (a) refit the model to a P&L symptom against a direct measurement (violates "never refit on a P&L window"), and (b) move the fair the *wrong way*, since the measured sign is negative.

Concretely: keep the ρ table as shipped; if #37 is to be addressed, add a corners-combo width/edge-floor knob (a widen-or-require-more-edge lever), measured against realized fills — not a correlation change. This is a pricing/ops decision for the operator, not a correctness fix, and is out of scope for this measurement.

---

## Data limitations & the club→international transfer caveat (read this)

- **We measured CLUB, because World-Cup / international corners data does not exist locally.** The five football-data.co.uk divisions are club leagues. `data/history/intl_results.csv` (martj42 internationals) is **GOALS ONLY — no corners columns** — so it *cannot* measure corners↔goals for internationals; it is stated here as a hard limitation, not used. The `*eve.zip` files (2005–2025) were inspected and are **Retrosheet MLB baseball event data** (team codes ANA/ARI/BOS…, `.EVA/.EVN/.ROS` files) — **not soccer, no corners** — excluded.
- **Transfer to the WC tape is an assumption, not a measurement.** Our live corners flow is ~87% World-Cup knockout. Two structural differences from club:
  1. **ET inclusion.** WC knockout corners settle *including* extra time; a level-after-90 opens an extra corners window. This is a corners↔*advance/scoreline-state* effect (already captured by the measured `advance|corners` strength curve, dog +0.23 ↔ fav −0.23, pooled ~0), **not** a corners↔total-goals effect — goals-over also settles incl. ET, so the ET channel does not obviously induce a positive corners↔goals link.
  2. **Tournament football** tends to be tighter/lower-scoring than club league play; if anything that nudges the goals-over marginals down, not the *dependence* up.
  There is **no local data to measure the WC corners↔goals ρ directly.** The defensible position is: the club measurement says ~0 (leaning slightly negative), the shipped 0.08 band already spans the full club league dispersion **and** the mild ET/tournament uncertainty, and 0.00 stays the center. If WC corners co-settlements ever become available (from prod settlements), re-measure directly and confirm the transfer.

**Bottom line either way:** club data says corners↔goals ≈ 0 (slightly negative) at every traded line; the shipped 0.00 with an 0.08 band is right and robust; the #37 gap is richness → address with a WIDTH, not a ρ.

---

## Reproduce

```
python tools/measure_corners_goals_rho.py
```
Prints: the import-time parity gate (forward BVN vs live `gaussian_copula_joint_prob`), pooled per-line table (n, P(A), P(B), P(A∧B), ρ_tetrachoric ±CI95, φ, count Pearson), per-league ρ grid, cross-league dispersion, and the headline vs-shipped comparison. Deterministic (bootstrap seed 20260715). "Keep in sync" note in the script header: its BVN CDF is the same bivariate normal the live copula integrates, pinned by the parity assertion.

---

## NEXT STEPS

- **Owner: operator.** Decision owed: address #37 with a **defensive corners-combo WIDTH / edge-floor** (a widen-or-require-more-edge lever on corners-bearing combos), sized against realized fills over a pooled multi-week window — **NOT** a ρ change. This measurement closes the "is it ρ?" question: it is not.
- **Owner: whoever ships the width.** If a corners width knob is added, keep the ρ table pristine (this measurement re-confirms 0.00 center / 0.08 band). Any width is a pricing/ops lever, gated on fills, never refit on a P&L window.
- **Owner: measurement (future).** When WC corners co-settlements accumulate from prod, re-run this measurement on the WC tape to confirm the club→international transfer directly (currently an assumption; no local WC corners data exists). Until then, 0.00 ± 0.08 stands on club evidence + ET/tournament band reasoning.
- **No live change made or pending from this report.** `tools/measure_corners_goals_rho.py` + this file are the only artifacts.
