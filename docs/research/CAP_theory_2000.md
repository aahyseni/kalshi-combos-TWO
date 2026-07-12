# Ruin / Kelly / Portfolio-Theory derivation of the caps for a $2,000 sell-side parlay book

**Scope.** Sets the VALUES (the design doc `docs/research/R2_caps_killswitch.md`
sets the TYPES) for a **$2,000** sell-only, long-NO parlay maker. Every number is
derived from (a) established ruin/Kelly theory and (b) the empirical clearing +
settlement tape on disk (WC `ph4/wc/wc_fixed_printed`, 14,790 resolved+printed
combos, 82.1M contracts). Read the "sensitivity" line on each cap: it says what
would move the answer, so nobody refits it on a P&L window
(memory `feedback_no_refit_on_pnl`).

---

## 0. The payoff we are sizing (why this is not ordinary Kelly)

We SELL the parlay = hold long NO. Per contract:

- **Win (parlay MISSES, ~75%):** keep premium `p` (the taker's yes price, avg
  **0.27** in the sellable regime, tape `[0.15,0.55)`).
- **Lose (parlay HITS, ~25%):** pay $1, net **−(1 − p) ≈ −0.73**.

This is the **insurance-seller / short-vol payoff**: many small wins, rare large
losses. Two facts from established theory make it dangerous at small bankroll:

1. **Gambler's ruin is exponential in the loss/win ratio.** For an asymmetric walk
   the ruin probability scales like `(q/p)^(units to barrier)`; the marginal
   impact of *stake size* on ruin **grows** as the payoff gets more asymmetric
   (Whelan, *Ruin Probabilities for Strategies with Asymmetric Risk*). A short-vol
   book is exactly the asymmetric regime where oversizing is punished hardest.
2. **Ruin is an absorbing barrier — you cannot compound back from 0.** Kelly
   maximizes `E[log(wealth)]` precisely because log punishes drawdowns convexly:
   losing 50% needs +100% to recover. At $2,000 the barrier is not $0 — it is the
   **operational-death level** (~$500–800) below which min RFQ sizes and the whale
   tape (single RFQs up to 1.8M contracts, 68 combos = 50% of volume) make a
   competitive book impossible. That higher barrier is what forces sub-Kelly sizing.

**The tape is fair, so variance — not drift — is the whole risk.** Empirically the
clearing price ≈ realized hit frequency at every bucket (price 0.20→hit 0.108,
0.30→0.233, 0.50→0.440). A naked-fair maker therefore has **~zero edge**; our edge
is only the markup we shade on top. Measured realized edge in the WC sample was
~7¢/$1 of obligation (price 0.268 vs realized hit 0.197), but **Kelly says size on
the low-confidence edge estimate** — so the binding cap is derived from the
**NO-EDGE / 4¢-EDGE** columns, never the lucky 7¢ one.

---

## 1. The headline number: %-of-GAME (correlated-cluster) cap

### The correlation is real and large
Clustering resolved combos by settlement window, on the **worst match-window the
maker net-LOST**, and the cluster-level **payout fraction** (realized payout ÷
max-if-all-hit) reached **0.46** — i.e. when a match breaks toward
favorites/overs, ~half of "everything hits" can materialize at once. 1 in 3
match-windows was a net loser for the maker. **Within one game, leg outcomes are
maximally correlated** — this is why the game (not the combo, not the ticker) is
the risk unit, and why the cap sums committed payout across the cluster rather
than netting deltas.

### Risk-of-ruin as a function of f_game (Monte Carlo on the empirical shock)
Model: per game-cluster we lock obligation `G = f_game × equity` (contracts×$1),
collect premium `K·G` (K=0.27), pay `φ·G` where φ = fraction of the cluster that
hits, drawn from a Beta calibrated to the tape (mean = K−edge, p99 payout fraction
≈ 0.46, plus a 2% "leaguewide-high night" multiplier and a **common daily shock**
across the C≈3 concurrent games a match-day carries). Ruin = equity ever ≤ **$600**
(operational death), over a ~140-match-day season.

| f_game | P(ruin) NO-EDGE | P(ruin) 4¢-edge | worst-week (7 hot days) survives? |
|---|---|---|---|
| 5%  | 0.0%  | 0.0% | yes, −5% |
| **8%**  | **3.8%** | **0.0%** | **yes, −8%** |
| 10% | 14.5% | 0.0% | yes, −15% |
| 12% | 28.6% | 0.0% | yes |
| 15% | 49.2% | 0.01% | yes, −20% |
| 20% | 71.7% | 0.11% | halved to ~$1,013 |

The **no-edge inflection is between 8% and 10%.** 10% (the design-doc default)
sits *on the cliff* the moment the edge is assumed away. **8% keeps no-edge ruin
under 4%** and, with any real edge, is deep in safe territory.

### Kelly cross-check
Growth-optimal full-Kelly for this bet is **f\* ≈ 60%** of obligation; quarter-
Kelly ≈ 15%. **8% is roughly half of quarter-Kelly (~⅛-Kelly)** — the correct
posture for a tiny, absorbing bankroll with an *uncertain* edge. Fractional Kelly
(¼–½) is standard practice to absorb estimation error and cut drawdown
(Kelly/Thorp; every practitioner source below). At $2,000 we go *below* ¼-Kelly
because the barrier is high (operational death ~30% of bankroll, not 0%) and the
edge estimate is one WC sample.

> **RECOMMENDATION: `max_game_committed_payout_pct = 8%` → $160 at $2,000.**
> One notch tighter than the design doc's 10%, because 10% is the no-edge ruin
> cliff and a $2k book cannot take that bet.
>
> **Sensitivity:** rises to 10% only after (a) ≥3 weeks of shadow/prod settlements
> confirm edge ≥4¢/$1 pooled, AND (b) measured cluster payout-fraction p99 stays
> ≤0.46. Drops toward 6% if concurrent games/day C rises above ~4 or a common
> shock with ρ>0.5 shows up on losing nights.

---

## 2. Why a $2,000 bankroll forces smaller % than a $5k+ book

The design doc's percentages were written for a **$5,000** baseline. The same %
is **not** equally safe at $2,000, for three theory reasons:

1. **The absorbing barrier is a larger fraction of the bankroll.** Operational
   death (~$600, set by min RFQ size and whale-tape competitiveness) is **30% of
   $2,000** but only **12% of $5,000**. Ruin is "hit the barrier," and the barrier
   is closer in % terms, so every drawdown is proportionally more lethal.
2. **Fewer independent games between resizes.** Kelly's ruin-safety comes from
   compounding across *many* independent bets; a small book takes fewer contracts,
   so realized variance per game is lumpier relative to equity (you can't slice a
   whale RFQ into 200 small independent fills the way a big book can).
3. **Edge uncertainty dominates.** With less capital you get less data per unit
   time, so your edge estimate is noisier for longer — Kelly says size down when
   `σ(edge)` is large. → **Use ¼-Kelly or less; we use ~⅛.**

**Consequence:** the $2k caps are the $5k design-doc caps shaded **tighter**
(game 10%→8%, theme 15%→10%, per-combo held at 2%, absolute leverage 4×→3×). The
percentages that survive are the load-bearing definitions; only the values move.

---

## 3. Per-combo cap vs per-game cap (per-game must bind)

- **Per-combo `max_position_committed_payout_pct = 2%` → $40 at $2,000.** A single
  max-size combo that HITS costs `2% × (1−K) = 1.46%` of bankroll net — a
  survivable one-off. 2% under an 8% game cap means **≤4 max-size combos before the
  game cap binds** — clean granularity, and it forces the whale RFQs (that dominate
  volume) to be **sliced**, not taken whole.
- **Relationship (the theory point):** because within-game correlation is ~maximal,
  N combos on one game are **one bet of N× the size**, not N independent bets
  (search sources: "correlated bets should be treated as a single larger bet").
  So the **per-GAME cap is the binding constraint by construction** — the per-combo
  cap only shapes *granularity within* a game (no single combo is the whole game),
  while the game cap governs *total correlated exposure*. Per-combo × 4 = per-game
  is the intended arithmetic.

> **Sensitivity:** per-combo can go to 1.5% (5 combos/game, more slicing) if whale
> RFQs prove toxic; it should NOT exceed 2.5% (that lets one combo = 1.8% single-hit
> loss and only 3.2 combos/game, too coarse).

---

## 4. One-directional / theme cap (net exposure to one leg-outcome across games)

A book that sold "over 2.5" parlays across 200 games is 200× long "unders" — on a
leaguewide-high night those **correlate through a common shock** even though
`cross_event_rho = 0.0` unconditionally. Modeling a theme spanning ~8 games with a
common-shock correlation **ρ=0.5** (no edge):

| theme_pct | P(ruin) |
|---|---|
| 8%  | 0.0% |
| 10% | 0.0% |
| 12% | 0.06% |
| 15% | 0.71% |
| 20% | 6.9% |

The design doc's **15% is too loose once the common shock is modeled** — a theme
is *not* meaningfully weaker-correlated than same-game on the nights that matter
(the losing nights). **Set theme = 10%, equal to the game cap**, not 1.5× looser.

> **RECOMMENDATION: `max_theme_net_delta_pct = 10%` → $200 at $2,000.**
>
> **Sensitivity:** this is the doc's flagged "measure-then-tighten" number. Ship at
> 10%; re-derive from realized cross-game theme correlation on losing nights
> (pre-registered, multi-week). If measured losing-night ρ < 0.3, it may loosen to
> 12%; if a real leaguewide shock (ρ→0.7) appears, tighten to 8%.

---

## 5. Drawdown / daily-loss / hard-trip (survive a favorite-hot week without ruin)

The daily P&L distribution at the recommended sizing (f_game=8%, C=3, 4¢ edge):
median day is **+1.0%** (the maker makes money most days), p90 bad day **−3.1%**,
p99 **−7.1%**, p99.9 **−10.2%**. The **worst-week stress** (7 straight days, φ mean
forced to 0.42) at f_game=8% ends the week down only **~8%**, never dead.

Levels are set so each fires on a genuine tail, not routine variance, and so the
three form a graduated ladder that lets the book survive the maker-killing week we
observed:

| Level | % of bankroll | $ at $2,000 | fires at | rationale |
|---|---|---|---|---|
| **Daily-loss halt** (soft) | **4%** | **$80** | ~p93–95 day | ordinary bad day is 3.1% (p90); 4% = true "stand down," stops quoting for the day, cancel-all |
| **Peak-drawdown halt** | **8%** from intraday peak | **~$160** trough | give-back after an up day | above the daily level so it catches profit-give-back a from-zero cap misses; ≈ one worst-week's total bleed, so a bad week trips it once, not daily |
| **Hard-trip kill** | **12%** | **$240** | 6% below starting bank | writes KILL file, human-only clear; a $2k book down 12% (to $1,760) with an *unexplained* path is "something is structurally wrong," halt-and-inspect before the barrier |

Reasoning for the ladder spacing:
- **Daily 4% < drawdown 8% < hard-trip 12%** so they don't double-fire. A single
  bad day (≤4%) stands the book down for the day; a bad *week* that bleeds past 8%
  from peak halts and cancels; only a 12% hole — approaching the operational-death
  zone from the top — pulls the human-required kill.
- **Why not the doc's 6%/8%?** At $2k the drawdown halt should sit at the
  worst-week magnitude (~8%), not below it, or a single expected bad week
  nuisance-trips it and the book never runs. The hard-trip at 12% leaves a **safety
  gap** to the ~30%-of-bankroll operational-death barrier — the kill fires with room
  to spare, which is the entire point of a kill switch.

> **Sensitivity:** if the median day drifts negative in shadow (edge gone), tighten
> daily→3%, drawdown→6%. If C (concurrent games) rises, worst-week magnitude rises
> and drawdown should track it. Hard-trip is OPERATOR-CONFIRM (it requires a human
> to clear) regardless of the derived number.

---

## 6. Absolute-$ exposure ceiling (leverage backstop above the % caps)

Sell-parlay books are structurally payout-heavy (the sweep saw premium↔payout ≈
13× *uncapped*). The absolute ceiling is the "you may owe at most $X to the world
if everything hits" backstop that binds even when bankroll is stale.

> **RECOMMENDATION: `max_total_committed_payout` = 3× bankroll = $6,000.**
> Tighter than the doc's 4× because at $2k the collateral lock-up is a hard cash
> constraint: 3× committed payout across concurrently-open games already implies
> locking a large share of a small balance. 3× still allows ~19 games at the 8%/game
> cap simultaneously — far more than a match-day carries — so it is a true backstop,
> not a routine binding cap.
>
> **Sensitivity:** OPERATOR-SET risk appetite; raise toward 4× only as bankroll
> grows past ~$10k where the % caps (not collateral) are the binding constraint.

---

## 7. Recommended cap table for $2,000 (final)

| Cap | % of bankroll | $ at $2,000 | one-line justification |
|---|---|---|---|
| **%-of-GAME (cluster)** | **8%** | **$160** | no-edge ruin cliff starts at 10% (14.5%); 8% keeps ruin <4% & ≈⅛-Kelly for an absorbing $2k book |
| **Per-combo max payout** | **2%** | **$40** | 4 max-size combos/game (clean granularity); single-hit net loss 1.46% is a survivable one-off |
| **Directional / theme** | **10%** | **$200** | with modeled common-shock ρ=0.5, 15% gives 0.7% ruin, 20% gives 6.9%; theme≈same-game on losing nights → hold to game cap |
| **Absolute $ exposure** | **3× = 300%** | **$6,000** | leverage backstop above the % caps; tighter than 4× because collateral lock-up bites a small balance; still ~19 games headroom |
| **Daily-loss halt (soft)** | **4%** | **$80** | p90 bad day is 3.1%; 4% fires ~p93–95, a true stand-down not routine variance (median day is +1%) |
| **Peak-drawdown halt** | **8%** from peak | **~$160** | catches profit give-back; set at worst-week magnitude so an expected bad week trips once, not daily |
| **Hard-trip kill** | **12%** | **$240** | human-required kill with a safety gap to the ~30% operational-death barrier; OPERATOR-CONFIRM |

**Bankroll anchor:** $2,000 (operator-set). Live-balance poll is a floor: if
balance drops, caps tighten automatically; if it rises, caps loosen only on an
operator ratchet (a transient spike must not unlock exposure). Stale bankroll ⇒
% caps can't evaluate ⇒ the absolute $6k backstop + count-based caps still bind,
then `HALT_STALE_BANKROLL` (design doc §1).

**Fill-velocity caps** (design doc §2.5) inherit the same ratio: soft
fill-budget = **4%/2s = $80** (half the game cap), hard ceiling = **8%/2s = $160**
(one game cap in one burst = "something is wrong"), count = **8 fills / 2s**
(operator-set rate). These are the game cap re-expressed as a rate, so a coordinated
mass-accept can't commit more than a game's worth of payout before the tap shuts.

---

## 8. The one structural precondition (or every number above is wrong)

All of this assumes the risk path measures **committed payout** (`contracts × $1`
for the NO side we hold), NOT premium paid. The design doc's Gap #1 is that
`max_loss_cc` currently measures the ~$0.10 premium we PAID, understating a NO
book's true exposure by ~6–11×. **Until `committed_payout_cc` is wired, an "8%
game cap" is silently an ~60–90% game cap.** Fix Gap #1 first; then these values
bind as intended.

---

## Assumptions & how to break them (audit trail)

- **Tape is fair (price≈hit):** verified across all price buckets on the WC sample.
  If prod shows systematic price>hit (real edge) the caps can loosen; if price<hit
  (adverse selection / winner's curse, per memory `project_kalshi_combos_winners_curse`)
  they must tighten. **Plan on ~0–4¢ edge, not the observed 7¢.**
- **Cluster payout-fraction p99 ≈ 0.46:** from 12 coarse settlement windows (WC).
  A finer game-code clustering (leg-ticker gamecode, not `cutoff`) could reveal a
  fatter same-game tail → tighten the game cap. This is the #1 measurement to
  refine, and it needs leg-level tickers not present in these fairs pickles.
- **C ≈ 3 concurrent games/day, common daily shock σ=0.30:** WC schedule assumption.
  MLB (many simultaneous games) has higher C → the game cap should be *tighter* for
  MLB than for WC. Re-run per sport before enabling MLB sizing.
- **Operational-death barrier ≈ $600 (30%):** judgment from whale-tape competitiveness.
  Higher barrier ⇒ tighter caps. Operator owns this number.
- **NEVER refit any of these on a P&L window** (CLAUDE.md; `feedback_no_refit_on_pnl`).
  They move only on measured structure: edge (settlements), cluster tail (game-code
  clustering), C and common-shock ρ (per-sport schedule + losing-night correlation).

## Sources
- [Whelan — Ruin Probabilities for Strategies with Asymmetric Risk](https://www.karlwhelan.com/Papers/Ruin.pdf) (short-vol payoff; stake size vs ruin under asymmetry)
- [The Gambler's Ruin with Asymmetric Payoffs (UCD WP2025_03)](https://www.ucd.ie/economics/t4media/WP2025_03.pdf)
- [Gambler's Ruin with Asymmetric Probabilities — MetricGate](https://metricgate.com/docs/gambler-ruin-asymmetric/)
- [Kelly criterion — Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion) (E[log wealth]; correlated-asset generalization f\*=Σ⁻¹(μ−r); fractional Kelly for model error)
- [The Kelly Criterion: Bankroll Management & Fractional Kelly](https://www.legaluspokersites.com/blogs/the-kelly-criterion/) (correlated bets = one larger bet; ¼–½ Kelly in practice)
- [Kelly Criterion Formula Explained — Quant Matter](https://quantmatter.com/kelly-criterion-formula/)

## NEXT STEPS
- **Owner: engineering.** Wire Gap #1 (`committed_payout_cc`, NO-side) FIRST — every
  value here is nominal until the risk path measures payout, not premium.
- **Owner: measurement (multi-week, pre-registered).** (1) Re-cluster by leg-ticker
  gamecode to sharpen the same-game payout-fraction p99 (drives the game cap);
  (2) measure pooled edge from real settlements (drives whether 8%→10% is allowed);
  (3) measure losing-night cross-game theme ρ (drives the 10% theme cap). NEVER refit
  on P&L.
- **Owner: operator (decisions owed).** Sign off the OPERATOR/CONFIRM numbers:
  bankroll $2,000, absolute leverage 3×, hard-trip 12%, operational-death barrier
  (~$600), fill-count rate (8/2s). Confirm per-sport: MLB likely needs a TIGHTER
  game cap than WC (higher concurrent-game count).
