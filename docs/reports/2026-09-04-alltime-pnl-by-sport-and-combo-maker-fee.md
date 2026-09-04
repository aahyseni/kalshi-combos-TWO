# 2026-09-04 — All-time P&L by sport (soccer / baseball / esports) + the combo maker fee that went live 8/20

Operator asked for an all-time P&L sweep split soccer / baseball / esports
(World Cup start → now) to decide whether to focus all capital on soccer and
esports, and flagged a new Kalshi maker fee on combos. Read-only analysis:
exchange `/portfolio/settlements` + `/portfolio/fills` (ground truth, every
combo ever held), legs from the store's `position_ledger` / `rfqs` (indexed
lookups only), sport per leg from the LIVE `classify_sport`. 9-agent workflow
(6 lenses + 2 adversarial verifiers + synthesis) over a dataset that
reconciles to the exchange to the cent. Blast radius: none — no store or
repo code touched; helper scripts live in the session scratchpad.

## WRONG / RIGHT / OPEN — the thesis, claim by claim

| Claim | Verdict | Evidence |
|---|---|---|
| "Our best profit comes from soccer and esports" | **WRONG** | MLB = +$2,179 of the $2,964 trading P&L (73%). Soccer + esports = +$772 on $8.4k premium. |
| "Soccer / esports have a higher ROI than MLB" | **OPEN (noise)** | Point estimates yes (7.8% / 12.9% vs 3.2%), but day-clustered bootstrap: P(soccer > MLB) = 0.66–0.74, P(esports > MLB) = 0.76, **P(club soccer > MLB) = 0.50**. Every difference CI straddles zero. |
| "Soccer's edge is durable" | **WRONG** | 63% of soccer P&L ($307 of $488) is the World Cup: 45 combos, 3 settle days, ended 7/19. Club soccer since 8/13 = **3.60% ROI = MLB's 3.20%**. |
| "Esports is a real edge" | **OPEN** | 72 combos, $2.2k. One 5-leg LoL fill ($154.45, 8/14) is −53% of the sport's P&L; ROI moves 8.7%–25.5% on one day removed. CS2-only +24.5%, LoL-only +3.1%. |
| "Soccer / esports are steadier" | **WRONG** | Top-3 days = 124% (soccer) / 98% (esports) / 129% (MLB) of each sport's total; daily-P&L sd / mean daily cost 23% / 48% / 19%. |
| "Put ALL capital on soccer + esports" | **WRONG (physically)** | Club-era bot-up days: soccer + esports intake **$573/day** vs MLB $2,311; average open $450 (9% of $5k), hourly peak $1,081 (21.6%). 70% deployment needs ~7.8× their intake. Their FLOW is the binder, not our capital (filled$/demanded$ ≈ 0.5 bp in all three sports). |
| Counter-thesis "MLB is broadly calibrated" | **WRONG** | 88% of MLB P&L (+$1,920) is ONE cell: KS-containing parlays bought at NO 0.15–0.35 (176 combos, 64% ROI, five counterparties). MLB ex-cell = +0.40% on $65k; KS-free MLB −2.41% vs +2.46% model (z ≈ −1.7). "Realized = model" is two offsetting cells. |

## Corrected all-time table (7/14–8/30; folds: 36 unmapped→MLB, 13 WC "mixed"→soccer)

```
sport     n     premium$   pnl$      ROI%   win%   model edge$  edge%  real/exp  day-cluster 95% CI
mlb      3218   68,198.81  2,179.29   3.20   59.9    2,050.22    3.01    1.06     [ -2.8,  +8.9]  (31 days)
soccer    271    6,241.79    488.08   7.82   71.2      111.95    1.79    4.36     [ -5.0, +21.8]  (16)
  WC       45    1,214.60    307.11  25.28   91.1       26.46    2.18   11.6      [-11.6, +58.0]   (3)
  club    226    5,027.19    180.97   3.60   67.3       85.49    1.70    2.12     [-10.2, +18.3]  (13)
esports    72    2,198.66    283.92  12.91   75.0       77.26    3.51    3.67     [-17.0, +35.9]  (21)
cross       3       29.07     12.23  42.05  100.0        0.85    2.91   14.5      n/a
TRADING  3564   76,668.33  2,963.51   3.87   61.1    2,240.28    2.92
non-trading shard-0 credits (likely APY interest; confirm in app)   +16.58
EXCHANGE REALIZED (balance $4,980.09 − $2,000 deposit)             2,980.09
```

By month: MLB Jul +9.6% ($6.2k) / Aug +2.6% ($62k); soccer Jul +25.3% (WC only)
/ Aug +3.6% (club); esports Aug +12.2%. Head-to-head 8/13–8/27 (10 active
days): MLB +2.86%, soccer +3.58%, esports −2.76% — all CIs straddle zero.

**The only decision-grade structural difference in the whole set is the
pricer's own expected edge per dollar: club soccer 1.70% [1.32, 2.12] vs MLB
3.01% [2.70, 3.35], non-overlapping.** Esports 3.51% is at MLB parity. Nobody
has explained why soccer quotes half MLB's edge (markup policy? fair spread?
the favorite-ML shape at 0.63% edge?) — owed a pricing-side read.

## The combo maker fee (NEW, live since 8/20) — measured, not assumed

| Fact | Value | Source |
|---|---|---|
| First charged | **8/20 ~03:00 ET** (26 zero-fee fills earlier that morning, 590 charged after) | exchange fills |
| Formula | **fee = 0.035 × contracts × P × (1−P)**, ceiled | 590 fills, median abs error $0.00008; every rival formula errs ≥$0.05 |
| vs the repo's pinned schedule | 2× the 0.0175 maker coefficient in NOTES.md L9/MF1 (6/29 PDF) | series now reports `fee_type: quadratic_with_combo_maker_fees`, multiplier 1 |
| Charged when | at fill (settlement `fee_cost` echoes the same $) | fill fee sum = settlement fee sum = $127.13 |
| Share of premium | 0.3% (NO ≥0.85) · 0.9% (0.65–0.85) · 1.5% (0.35–0.65) · 2.5% (0.15–0.35) · 3.1% (<0.15); **~1.1% on our mix** | since 8/20 |
| The bot's fee seam | **DARK**: `maker_fee_active_prefixes` empty in the live yaml → every fill since 8/20 booked fee $0 and expected edge overstated by the fee; quote constructor subtracts a $0 fee from every bid; the seam's coefficient (0.0175) would under-book by half even if armed; `FeeType.parse` maps the new enum string to UNKNOWN | config.py:444-460, lifecycle.py:6654-6760, quote.py:190-221, fees.py:39-89 |
| Docs | kalshi.com/fees + fee PDF 429, docs pages 404 today; `GET /series/fee_changes` returns an empty array | fill data is the source of truth |

Fee bite vs our markup tiers (per contract, our NO price P):

```
P      fee ¢/ct  fee % prem | vs mains 2¢  vs ML-razor 0.6¢  vs ladder 1¢  vs ladder 3¢
0.08     0.258      3.2%    |     13%          43%               26%           9%
0.30     0.735      2.5%    |     37%         122%               74%          24%
0.50     0.875      1.8%    |     44%         146%               88%          29%
0.70     0.735      1.1%    |     37%         123%               74%          25%
0.85     0.446      0.5%    |     22%          74%               45%          15%
```

The 0.6¢ ML-parlay razor is under water once fair exceeds ~12¢; the 1¢
ladder rung is under water in the 0.25–0.75 band. This is a pricing-mechanism
defect to repair BEFORE relight (arm the seam with the measured coefficient;
the fee must enter the post-rebate edge floor / EV gate — "eat the fee" was
ratified 7/16 for a $0.0175 schedule at 1¢ markups, not a 0.035 schedule
against a 0.6¢ razor). It is independent of the sport question.

Fee-adjusted all-time view (today's schedule applied to every historical
combo, so sports compare on equal footing):

```
sport     premium$   gross ROI%  fee@today%  net ROI%  model edge%  model edge net of fee%
mlb       67,749.85     3.21        1.35        2.00       3.01           1.66
soccer     6,198.52     7.83        1.04        7.27       1.81           0.77   <- thinnest priced edge
esports    2,198.66    12.91        1.15       11.94       3.51           2.37
```

Post-fee era only (opened ≥8/20, fees actually charged): MLB +5.7% on $7.0k,
soccer −3.6% on $2.4k, esports +26% on 16 combos. Thermometer reads.

## What actually drives each sport (verified)

- **MLB (+$2,179)**: the KS cell (parlays containing "starter 3+ Ks", bought
  at NO 0.15–0.35) = +$1,920 on $3.0k; farmer c1789477 is our BEST
  counterparty (+$526–624). Losing shapes, all inside-MLB PRICING: rfi×rfi
  pairs quoted at 0.49% edge (−$585, z_day −2.2); partial-SGP combos (true
  game key: −3.2% on $9.4k with the HIGHEST modeled edge 3.85%); KS line-2
  (−$456) vs line-3 (+$1,718) — calibration lead; whale 0f9b27cb3e = 24.7% of
  MLB premium at −0.19% vs 2.72% model (informed-sizing signature in the
  0.30–0.50 / 0.70–0.85 bands, negative every week, but t = −0.5). MLB
  ex-whale +4.48%. 8/11 and 8/18 were correlated slates (97 / 79 losing
  counterparties), not counterparty events. Model edge% rose 2.53→3.78 across
  wk32–34 while realized fell — favorite-band drift, owed a fair-model read.
- **Soccer (+$488)**: WC +$307 (closed). Club +$181 lives in favorite-moneyline
  parlays (72 combos, +$283, 89% win on 0.63% modeled edge); every ML-free club
  shape is negative, btts:Y + total:over worst (−$128 to −$145, correlation
  lead). LaLiga −$121 in 3/3 weeks = its over/total shapes. 258 counterparties
  / 298 fills — no farmer. Tie×under exact shape −$13 (flat, guard holds).
- **Esports (+$284)**: CS2-only +$186 (24.5%), LoL-only +$37 (3.1%); 14
  three-leg combos = 83% of P&L; one 5-leg LoL fill −$154 = −53% of the sport.
  HOW did a $154.45 fill (2.5% of equity then) pass the 1% per-combo anchor?
  Open — cap base must be checked.
- **Cross-sport lead (pricing, not admission)**: the favorite-parlay band (our
  NO 0.65–0.85) realizes ABOVE model in soccer (z_game +2.45) and esports
  (+2.34) and BELOW model in MLB (−1.78). 15 sport×band cells ⇒ P(chance
  z ≥ 2.45) ≈ 0.19 — pre-register it, do not act on it.
- The 8/26 "26-fill pick-off cluster" F1F3B9C6163 flagged this morning is
  **MLB** (KXMLBHIT MIN@ATH hit pair, 17/26 fills the KS farmer), cost $75.41,
  **WON +$60.71** — retracting the adverse-selection flag.

## Capacity / calendar

```
8/13–8/30 bot-up days (10)   intake/day   hold    avg open   % of $5k   club-era premium share   return per $-day open
mlb                           $2,311      12.0h   $1,151     23.0%      80.0%                    6.25%
soccer                          $498      17.0h     $354      7.1%      17.4%                    5.04%
esports                          $75      31.0h      $97      1.9%       2.6%                    9.14% (on $88 open)
```

RFQ flow post-8/13: soccer 15–31% of RFQs, esports 1.5–3% (shares reproduce;
absolute counts do not within 2× — tape gaps + store collapse). Soccer
converts at HALF MLB's rate per RFQ (0.49 vs 0.96 fills / 10k) — small MLS/
LigaMX tickets ($10–14), the 1% cap, markup, or fair: unknown, and it is where
soccer capacity actually lives. Calendar: MLB regular season ends **9/27**
(3.3 weeks), postseason 9/29–10/31 (flow unmeasured); NFL starts 9/10 and is
NOT wired (zero legs in 920k sampled RFQs); LoL Worlds 10/15–11/14;
Bundesliga/Saudi legs almost absent from samples so far.

## Recommendation (both verifiers concur)

**Do not drop baseball and do not redirect capital by fiat.** Dropping MLB
now forfeits ~$50k of non-whale premium running +4.5% (and $65k running
+0.4% ex the KS cell) for two venues whose combined intake fills ~9% of the
book and whose ROI advantage is P ≈ 0.5–0.76 noise resting on a closed event
and single fills. Under the constitution (no refit on a P&L window; steer by
pricing and adaptive caps, never by blocklists; sport admission = operator
scope) the only decision-grade measurement — modeled edge per $, club soccer
1.70% vs MLB 3.01% — points AGAINST the thesis, and the capacity arithmetic
makes "all capital" impossible: today's caps already give soccer/esports every
dollar they can fill. Sport admission is the operator's call; if wanted
anyway, make it a **pre-registered test** (≥6 weeks, ≥$15k premium, club +
esports model edge% reaching MLB's, realized−model favorable at game-clustered
z ≥ 2, ≥40% deployment) rather than a belief. The real October question is
what replaces $2,300/day of MLB intake after 9/27 — wire NFL and measure
Bundesliga/Saudi/Worlds flow now.

Where pricing should work instead (mechanisms, never knobs; each must be
validated to still quote against real sizes before live):
1. **Arm the maker-fee seam with the measured 0.035 coefficient** and carry
   the fee into the EV/edge floor (kills the under-water razor + ladder
   rungs). Pre-relight.
2. Margin floor so rfi×rfi pairs never quote at 0.49% edge.
3. Partial-SGP correlation markup (true-game key).
4. KS line-2 calibration check (model P vs realized by line, game-clustered).
5. Per-counterparty size-conditioned markup on repeat flow (whale bands).
6. Soccer: btts:Y + total:over correlation check.
7. Pre-register ONE cross-sport calibration lens on the NO 0.65–0.85
   favorite-parlay band, multi-week stopping rule.

## Data-hygiene defects surfaced (ledger stale-row P1 grows)

73 REST fills ($1,421) absent from the store `fills` table; 31 store fills
without a REST order_id; store `fee_cc` = $16.52 vs exchange $127.13 (the
dark seam); 59 scalar partial-payout settlements flagged won=1 (net −$484;
strict MLB win 58.3% not 60.0%); 106 fills flagged `is_taker` ($17.90 taker
fee, 80 inside three repeated-fill combos — unexplained); `classify_sport`
returns UNKNOWN for `KXMENWORLDCUP` (pricing aliases it correctly); 7/23–7/25
rfqs tape gap (37 combos with no rfqs row). The $16.58 non-trading credit on
shard 0 (+$4.27 early Aug, +$12.31 at 9/4 03:11 ET) looks like APY interest —
confirm in the Kalshi app.

## NEXT STEPS

- **Operator**: (1) sport admission = keep all three (recommended) or a
  pre-registered test; (2) confirm the fee-seam repair goes in before relight
  (it changes quoted bids on thin tiers — the 7/16 "eat the fee" doctrine was
  written for half this fee); (3) relight go; (4) NFL wire go/no-go before
  9/10; (5) confirm the $16.58 credit in the app.
- **Me**: fee-seam arm + coefficient + EV-floor mechanism (gated, quote-
  production counterfactual, before/after sends); then items 2–7 in order of
  measured dollars (rfi×rfi, partial-SGP, KS line-2, whale bands, btts×over);
  pricing-side read on WHY soccer quotes 1.7% edge vs MLB 3.0%; the 9/1
  pre-registered reads; store repairs (73 missing fills, fee_cc, scalar flag).
