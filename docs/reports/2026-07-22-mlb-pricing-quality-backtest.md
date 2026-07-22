# MLB Pricing-Quality Verdict — maker-closeness + settlement backtest

**Date:** 2026-07-22
**Scope:** Quality (not isolation) of the MLB combo pricer against the two rulers that
matter — **other makers' clearing prints** (are we competitive / are we picked off) and
**realized settlement** (is our fair *right*). Synthesizes three independent analyses:
maker-closeness, settlement calibration, and the new-family (OUTS/RBI/SB) flow census.
**Blast radius of this report: ZERO** — it is a read-only measurement on the frozen
`data/backtests/mlb_fresh/` cache (`inputs.pkl` / `outcomes.pkl` / `fairs.pkl`,
`mlb_backtest.json`); it edits no live pricing/engine/risk module and changes no config.
Per operator law #4 it is explicit about the M-N split (what IS vs ISN'T validatable now)
and per the no-refit rule, nothing here is a knob to turn — settlement divergences are
**signals to watch across weeks**, not a recalibration trigger.

> **The prior tranche reports (Stage 6+7 sign-off, the two adversarial judges, the
> gap-pairs judge) certified ISOLATION** — that the OUTS/RBI/SB + gap-pair wire is
> surgical, additive, bit-exact, and does not touch the existing families. **This report
> is the orthogonal axis: QUALITY** — given the wire is clean, *how well does the pricer
> actually price* against makers and settlement. The differential was isolation; this is
> whether the fair is good.

---

## TL;DR verdict

**Established MLB families are priced well enough to arm.** Our promoted (block-correlation)
fair tracks other makers to a **0.36c per-combo median** (92% within 2c) and tracks
**realized settlement essentially perfectly** (mean fair 0.1534 vs settled-YES 0.1537,
ECE 0.0050, Brier 0.1087, beating legacy at z=+6.8). Both adverse-selection tails are
small and controlled (pickoff >5c-under = 0.5% overall / 0.7% prop-carrying; auto-lose
>2c-over = 1.9%). The block-correlation is doing **real, settlement-confirmed work**:
5.4× tighter than legacy-flat-0.6 on prop-carrying combos with **no regression** on the
high-volume game-lines path.

**The new families (OUTS/RBI/SB) CANNOT be tape-backtested — this is the honest M-N.**
43 tape combos carry them, **0 printed, 0 cleared, 0 settled**; only 6 same-game carriers
are even priceable offline. Their wire is **plumbing-verified (classification + rho
engagement + sane fairs) but edge-UNVALIDATED**. No settlement ruler has ever touched one.

**RECOMMENDATION: ARM the established families now; VALIDATE the new families with a gated
first-live-slate (capped size + standing fair-vs-clearing + settlement logging = the live
backtest they can't get on tape).** Do not hold the whole arming on families that
structurally cannot be backtested — but do not treat them as validated either.

---

## 1) How close are we to other makers? (fair vs clearing)

Clearing = the median of a combo's pregame maker prints. This is the **competitiveness /
adverse-selection** axis, not the correctness axis.

| Cut | n | median \|err\| | mean \|err\| | within-2c | bias |
|---|---|---|---|---|---|
| **PER-COMBO (promoted)** | 5,169 | **0.36c** | 0.73c | **92%** | −0.38c |
| PER-COMBO (legacy flat-0.6) | 5,169 | 0.44c | 2.33c | 76% | +1.35c |
| **PER-PRINT (promoted)** | 25,327 | **0.53c** | 0.85c | **92%** | −0.46c |
| PER-PRINT (legacy) | 25,327 | 0.60c | 1.61c | 82% | +0.44c |

**By bucket (per-combo, promoted):**

| Bucket | n | median \|err\| | within-2c |
|---|---|---|---|
| game_lines_only | 3,344 | **0.29c** | 96% |
| props_only | 893 | 0.54c | 85% |
| mixed | 932 | 0.77c | 87% |
| prop_carrying (props+mixed) | 1,825 | 0.68c | 86% |

**The block-correlation earns its place.** On prop-carrying combos, promoted is
**0.68c vs legacy 3.69c** median \|err\| (5.4× tighter; within-2c 86% vs 40%); the
legacy mixed bucket is a **5.16c** median miss (27% within-2c). And it does this with
**no regression on game-lines-only (0.29c = 0.29c)** — the config is strictly better where
props exist and neutral where they don't. **GATE: PASS.**

### The two bias tails

| Tail | ALL | game_lines | props_only | mixed | prop_carrying |
|---|---|---|---|---|---|
| **Fair ABOVE clearing >2c** (we auto-lose auctions) | 1.9% | 1.2% | 2.4% | 4.2% | 3.3% |
| **Fair BELOW clearing >5c** (pickoff / adverse-selection) | **0.5%** | 0.4% | 0.8% | 0.5% | 0.7% |
| Fair BELOW clearing >2c (soft under-pricing) | 5.7% | 2.9% | 13.1% | 9.0% | 11.0% |

- **Auto-lose tail** is small everywhere (legacy's ALL is 19.3% — it systematically
  over-prices and loses auctions; we do not).
- **Pickoff tail (the one to watch)** is 0.5% overall, 0.7% prop-carrying — **small and
  controlled.** This is exactly where an information-driven counterparty could hit us, and
  it's concentrated in small-n prop sub-families (player_hit, player_hit+player_ks,
  player_hrr+player_ks at n=10-83), so the ~1c median under-clearing there carries real
  sampling noise. The **capped-size first-live-slate** is the right containment for it.

**Loosest joints (per-print, worst n≥50):** player_ks+total 4.20c (n=167),
player_hit+total 2.21c (n=92), player_hrr+player_ks 2.18c (n=76). **Tightest:**
spread 0.20c, ml+spread 0.22c, player_hr 0.43c, moneyline 0.47c.

---

## 2) Does our fair track SETTLEMENT? (the ruler)

Settlement is the only ruler that separates **maker MARKUP** from **our MISPRICING**.
n = 4,984 resolved MLB-strict combos, base rate settled-YES 0.1537.

| Model | mean fair | vs settled (Δ) | Brier | ECE | skill vs base-rate |
|---|---|---|---|---|---|
| **PROMOTED** | 0.1534 | **−0.0003** | **0.10874** | **0.00498** | +0.164 |
| LEGACY | 0.1705 | +0.0168 | 0.11221 | 0.01731 | +0.137 |

**Paired Brier (legacy − promoted) = +0.00347, SE 0.00051, z = +6.81** → promoted is
**significantly** better calibrated to reality, not just to makers. Reliability is
**monotone with no drift**: every populated bin within ±0.03 of realized except [0.40,0.50)
at +0.0285 (n=200). Legacy systematically under-predicts realized in [0.30,0.50).

**The correlation matrix is NOT bent by the ruler.** Two clean controls prove it:

- **moneyline (n=1,996, all cross-game → copula adds exactly 0.000):** fair 0.1046 vs
  settled 0.1017 — essentially perfect. Marginals + independence are sound where
  correlation is absent.
- **Decomposition on player_ks 2-leg both-YES (n=73):** independence-product 0.413 →
  copula fair 0.415 (**+0.002 from correlation**) → settled 0.616. The +0.20 miss exists
  **before any correlation** = a **live-book marginal** under-pricing of K-overs, not a rho
  error.

**Every family-level settlement flag traces to LIVE-BOOK MARGINALS on small,
game-clustered prop samples — not the copula:** player_ks (fair 0.329 vs settled 0.527),
total (0.243 vs 0.144), spread+total (0.273 vs 0.182), rfi (0.158 vs 0.367, n=30). All are
small-n; player_ks's 165 combos span only 15 games (~11/game), total 167/18 games — **not
independent samples**; a few K-heavy / total-heavy slates drive the flags. K-over legs
settle *above* their book-implied prob, total-over legs settle *below* — **opposite-signed
marginal biases** on thin windows. Per the no-refit rule these are **watch items across
weeks, a live-book/devig question, NOT a copula recalibration trigger.**

**Cross-check with maker consensus:** in the props_only high-prob region (fair≥0.30,
n=199), realized settlement is 59.3c [Wilson 52.4-65.9] while **both** promoted (45.2c)
**and** clearing (46.1c) sit below the CI floor. We track the makers to 0.9c and our Brier
ties theirs (0.1110 vs 0.1106) — so this ~1c/~4c gap is **shared maker conservatism, not
our miss.** Long parlays (n_legs=5) are slightly *over*-priced (fair 0.0667 vs settled
0.0368) — conservative, the safe direction for us.

---

## 3) VALIDATED vs DEFERRED-to-live (the honest law-#4 split)

### VALIDATED NOW — methodology + families

**Methodology:** dual-ruler on the frozen fresh cache — (a) fair vs maker clearing on
25.3k pregame prints / 5,169 combos, both bias tails enumerated; (b) fair vs realized
settlement on 4,984 resolved combos with paired Brier, ECE, per-bin reliability, and an
independence-vs-copula decomposition that attributes each miss to marginal-side vs
correlation-side.

**Families settlement-validated (have resolved+priced tape):** moneyline (1,996),
player_hr (315), total (167), player_ks (165), spread (105), player_hit (81),
player_hrr (50), rfi (30), player_tb (22), and their pairs. **Verdict for these:** promoted
fair is calibrated; workhorse game-line families track settlement to a few bp across all
bins; the block-correlation is a **net, settlement-confirmed improvement** over legacy.

### DEFERRED to live — new families (OUTS/RBI/SB) + gap pairs

**The honest M = 43 / N-priceable = 6 / N-graded = 0 accounting** (confirmed 3 independent
ways — 0 in `printed_tickers.json` (5,523 tape tickers), 0 in `printed_times.json`, 0 with a
stored `fair_promoted`):

| Family | tape combos carrying ≥1 leg | printed | cleared | settled | priceable offline |
|---|---|---|---|---|---|
| RBI | 23 | 0 | 0 | 0 | — |
| OUTS | 18 | 0 | 0 | 0 | — |
| SB | 2 | 0 | 0 | 0 | — |
| **TOTAL** | **43** | **0** | **0** | **0** | **6 same-game + 2 cross-game** |

**What IS validated for the new families (plumbing only):**
1. **Classification** — all outs/rbi/sb tickers type correctly via `classify_leg`
   (MLBOUTS/MLBRBI/MLBSB, MLB-anchored, blockers precede).
2. **Rho engagement** — the new 319/327-key `pair_rho_by_sport["mlb"]` keys resolve into
   `build_sgp_correlation` with correct same/opp orientation and rung selection
   (moneyline\|player_rbi +0.33, moneyline\|player_outs +0.43, player_ks\|player_outs +0.44,
   player_hit\|player_outs −0.21, player_hr\|player_rbi Frechet-clamped to +0.91 same-player).
3. **Fair sanity** — all 6 same-game carriers price without declining and produce plausible
   fairs (same-player HR1+ × RBI2+ = 13.9c sits correctly just under the 15.2c Frechet
   ceiling = strong +dependence; moneyline+rbi = 19.9c; 6-leg = 4.1c).

**What CANNOT be validated until a live slate:** (a) maker-MARKUP vs our-MISPRICING split
(needs real clearings — there are **0**); (b) settlement calibration / Brier (needs
`settle_yes` — there are **0**); (c) whether the calibrated new-family rho cells are
directionally right in P&L. **No combo carrying these families has ever cleared or settled.**
The 6 priceable carriers also share very few underlying markets (mostly one LAD/PHI slate,
2026-07-21) — a handful of correlated points, **not calibration evidence**, and the
player_hr\|player_rbi +0.91 that engaged is a **Frechet containment clamp**, not the
calibrated table prior. **Treat OUTS/RBI/SB as plumbing-verified / edge-UNVALIDATED.**

### Two caveats on the VALIDATED side (don't over-read it)

- **Per-print survivorship:** 9,263 of ~34.6k prints (27%) dropped as "unpriced" (predate
  every marginal snapshot). The 25.3k scored prints skew toward combos snapshotted early
  enough; tracking on the earliest, thinnest-book prints is **unmeasured**.
- **Reconstructed clearings:** 45% of untraded-per-DB combos came from a poller-gap-audited
  sample rather than organic prints; the maker consensus is real but includes reconstructed
  prints — treat **single-print combos** with caution.

---

## 4) RECOMMENDATION — are we pricing well enough to arm?

**YES for the established families; GATED-LIVE for the new ones.**

**(A) ARM the established MLB families.** Both rulers pass with margin: 0.36c to makers
(92% within 2c), dead-on to settlement (Δ −0.0003, ECE 0.005, z=+6.8 over legacy),
pickoff tail 0.5%, no game-line regression, block-correlation settlement-confirmed. This is
the quality bar the operator gate was built to check, and it clears it. (Arming remains
subject to the separate operator decisions already tracked on the resume state — scalar/DNP
AS4 monitor, fractional-V re-affirm, readiness items 4-5 — this report clears the *pricing*
gate, not those.)

**(B) The new families CANNOT be tape-backtested — so the first live slate IS their
backtest, run gated.** Rather than hold arming on families that structurally cannot produce
tape evidence (0 cleared / 0 settled), arm them behind a **capped-size** first-live-slate
with **standing fair-vs-clearing logging** (the maker-markup ruler) and **settlement
logging** (the mispricing ruler) on every OUTS/RBI/SB print. Gate the size-uncap on:
(i) capturing ≥1 pregame-priceable new-family combo that actually **clears**, then (ii) its
**settlement**, and only then grading fair-vs-clearing (markup) and fair-vs-settlement
(mispricing) per family. The capped size is also the containment for the small-n prop
pickoff tail flagged in §1. **This is the honest completion of the closed loop that tape
cannot provide.** (Alternative: HOLD the new families entirely until organic pregame prints
accumulate — strictly more conservative, but forgoes the only mechanism that generates the
missing evidence, since they only get printed once armed.)

**Do NOT:** refit any marginal/rho on the player_ks / total / props_only high-prob
settlement gaps — they are small-n, game-clustered, marginal-side (live-book/devig), and
the no-refit-on-a-settlement-window rule applies. Log them; revisit across multiple weeks
of settled slates.

---

## NEXT STEPS

- **Owner: operator** — Decision owed: approve **(A) arm established families + (B) gated
  capped-size first-live-slate for OUTS/RBI/SB**, or **HOLD** the new families. This report
  clears the pricing-quality gate for (A); it does not clear the non-pricing arming
  decisions (AS4 scalar monitor, fractional-V re-affirm, readiness items 4-5) tracked
  separately on the resume state.
- **Owner: whoever runs the first MLB slate** — Stand up per-print **fair-vs-clearing** and
  **fair-vs-settlement** logging on every OUTS/RBI/SB combo; the size-uncap gate is
  (i) ≥1 new-family combo clears pregame, then (ii) it settles, then grade markup +
  mispricing per family. Restate the **M=43 / N-priceable=6 / N-graded=0** accounting on
  the resume state so "wired" is never mistaken for "validated."
- **Owner: pricing/calibration (watch, no action)** — Track player_ks (K-over cheap),
  total / spread+total (total-over rich), props_only high-prob (realized 59c vs fair 45c,
  n=199), and rfi across more settled slates. These are marginal-side live-book/devig
  signals, **NOT** a copula recalibration trigger; do not refit on this window.
