# Empirical caps for the $2,000 sell-only parlay-maker book

**2026-07-12 — simulation-derived cap values, WC+MLB ph4 tape, $2,000 bankroll.**
Read-only research (no live module touched). Companion files:
`sim_caps.py` (main sweep), `markup_sens.py`, `robustness.py`, `halt_levels.py`,
`results_table.md`. All numbers reproduce from `_wc_rows.pkl` / `_mlb_rows.pkl`
built from the wired fair (verify4), real clearings, real settlements.

---

## 0. What the simulation did

```
 wired fair (verify4)          real tape clearings            real settle_yes
 fair_promoted[combo]  ─┐      (price,size,side,ts)  ─┐       0/1 outcome  ─┐
                        ▼                             ▼                     ▼
   ask = fair + markup ──►  fill the size that cleared at price ≥ ask ──►  settle:
   (two-tier: WC/MLB)       × fill_share (we're 1 maker of several)        MISS: +ask·q
                                    │                                      HIT : −(1−ask)·q
                                    ▼
                     clamp q by  per-COMBO payout cap (=$1·q ceiling)
                              and per-GAME payout headroom
                              (combo counts against EVERY game it touches
                               — the conservative correlation bound, R2 §2.2)
                                    │
                                    ▼
                     track equity, drawdown, ruin  →  bootstrap over GAMES
```

- **Risk unit = the GAME.** A combo's committed payout ($1 × contracts) is
  charged to *every* game it touches, because within a game leg outcomes are
  maximally correlated — one game breaking can settle many combos' legs YES at
  once. This is the whole reason a %-of-game cap exists.
- **NO-seller economics.** We collect `ask·q` now; if the parlay HITS
  (settle_yes=1) we pay `$1·q` and keep the premium (net `−(1−ask)·q`); if it
  MISSES we keep `ask·q`. Committed payout = `$1·q` = the bankroll lock-up and
  the quantity every cluster/tail cap measures (matches R2's `committed_payout_cc`).
- **Bootstrap = resample GAMES** with replacement (game-clustered), not combos —
  because the correlated unit is the game. 400–600 draws per cap set.

---

## 1. The finding that reframes the caps: markup must clear adverse selection first

At the thin markup the two-tier finding first suggested (WC +1.5¢ / MLB +0.5¢),
**every cap set loses money** — medP&L negative, P(loss) 68–97%. That is the
adverse-selection signature (memory `project_kalshi_combos_adverse_selection`:
independence-style pricing loses at every markup). The tape selects us into the
combos that hit.

But the book is **not** structurally negative. Break-even is ≈ **2¢ WC / 1¢
MLB**, and a clear profit plateau runs **3–5¢ WC / 1.5–3¢ MLB** (ROI +3% to
+7.7%). The tape supports it: WC clears a median **+1.45¢ over our fair** and MLB
**+0.29¢**, and 60% of WC flow is fillable at a 1¢ ask. So the operative markup
for the cap study is **WC +3.0¢ / MLB +1.5¢** — inside the plateau, defensible as
a real maker premium.

> **This is load-bearing: caps cannot rescue a book priced in the loss zone.**
> The markup clears adverse selection; the caps then shape profit/downside. All
> cap values below assume the profitable two-tier markup is in force.

---

## 2. Recommended cap set (values for $2,000)

| Cap | % of bankroll | $ at $2,000 | One-line justification |
|---|---:|---:|---|
| **%-of-GAME (correlated cluster)** | **10%** | **$200** | Best robust profit/downside knee for a small ruin-sensitive book: medR 0.54, p25R **+0.12** (positive at the 25th pct), p95 drawdown 8.5%, P(ruin)=0. Frontier keeps climbing to 15% (medR 0.65) — take 15% only once multi-week data confirms the tail. **The headline cap; it kills game concentration.** |
| **Per-COMBO max payout** | **1%** | **$20** | The single biggest downside lever. 1% dominates the profit/downside ratio at *every* game level and under both flow assumptions; 2%→5% roughly doubles/triples drawdown and flips the ratio toward zero. Forces whale-RFQs to be sliced. |
| **Absolute-$ exposure (whole-book committed payout)** | **2.5×** | **$5,000** | Backstop above the % caps. Peak realized book obligation was 1.64× median / 1.96× max in-sample; 2.5× is tight-but-non-binding, catches a runaway the % caps somehow miss. (R2's 4× was calibrated to $5k; at $2k the book only reaches ~2×.) |
| **One-directional / theme (net signed delta by (leg_family, side))** | **15%** | **$300** | Carried from R2 as a judgment number — cross-game theme correlation is real but weaker than same-game. **Cannot be derived from this tape** (no cross-game theme labels here). Ship at 15%, MEASURE-THEN-TIGHTEN on realized losing-night theme correlation. |
| **Daily-loss halt (soft)** | **8%** | **$160** | Sits at the p05 of the worst-day-loss distribution (med worst-day −3.8%, p05 −8.1%): fires ~1 bad day in 20, not on ordinary variance. R2's 4% (for $5k) would equal a *median* bad day here → constant false halts. |
| **Peak-drawdown halt** | **12%** | **$240** | Between p95 (10.8%) and p99 (12.7%) of the max-drawdown distribution: fires on a genuine give-back from a profitable peak, not noise. |
| **Hard-trip kill (writes KILL file, human-only clear)** | **15%** | **$300** | Just above the entire observed support (worst in-sample max-DD 14.5%, worst day −11.4%). At 15% the book is behaving outside anything the tape produced ⇒ stop and require a human. |

**Not derivable from tape data (carry R2 as operator-set, unchanged):**
fill-velocity budget 5%/2s soft, 10%/2s hard ($100 / $200 per 2 s window),
max-fills-per-window 8/2 s. These are *rate* limits tied to confirm-loop latency,
not quantities this settled-outcome tape can measure.

---

## 3. The profit/downside frontier (the requested deliverable)

Median profit/downside ratio vs %-of-game, per-combo fixed at the recommended 1%:

```
 medP/DD
  0.65 |                                  ●(15,1)      ●(20,1) 0.62
  0.54 |                    ●(10,1)  ◄── RECOMMENDED
       |
  0.41 |         ●(5,1)
       |
  0.11 |  ●(2,1)
       +----+--------+--------+---------+---------+----  %-of-game
          2%       5%       10%       15%       20%
```

- Ratio rises steeply 2%→10% (more game diversity = more near-independent bets),
  knees at **~15%**, then flat/slightly down at 20% (tail loss grows faster than
  profit; p05 P&L worsens −$63 → −$94).
- **Per-combo is the orthogonal, stronger lever**: dropping combo 1%→2% at fixed
  game% costs ~0.3 of ratio and roughly doubles p95 drawdown. Keep combo at 1%.
- P(ruin)=0 across the entire frontier — the choice is a *drawdown/return*
  trade, not a survival trade, at $2,000 with these caps.

**Recommendation:** run **10%/game + 1%/combo** now (conservative knee, p25 ratio
already positive, ≤10.8% p95 drawdown). Move to **15%/game + 1%/combo** (the
ratio-max point, medR 0.65) *only* after pooled multi-week data confirms the
worst-day/drawdown tail doesn't fatten — never refit on this one window
(`feedback_no_refit_on_pnl`).

---

## 4. Honesty / limitations (must read before promoting any value)

1. **ONE window.** This is a single ~week of WC+MLB. The bootstrap resamples
   GAMES to show robustness *within* this window; it cannot manufacture
   out-of-sample seasons. **Final values need pooled multi-week** before prod.
2. **WC = 6 games only.** The game-clustered bootstrap over WC is coarse (6
   clusters). MLB (53 games) carries the diversity. A WC-heavy week could behave
   worse than the pooled bootstrap suggests — the 10% (vs 15%) game cap buys
   margin against exactly this.
3. **Markup is assumed, not proven-optimal.** The 3¢/1.5¢ two-tier is inside the
   in-sample ROI plateau, but the plateau itself is one window. If realized
   markup lands thinner (competition), the profitable zone narrows and the caps
   should tighten (lower game%, keep combo 1%).
4. **Fill model is a proxy.** "Fill the size that cleared ≥ our ask × 0.25" is a
   reasonable maker-share assumption; results hold at 0.15 too (ranking
   identical). True fill depends on RFQ mechanics we can only measure in shadow.
5. **Theme cap and fill-velocity are NOT tape-derived** — flagged measure-then-
   tighten / operator-set.
6. **Sign discipline honored:** committed payout (gross $1/contract) drives the
   cluster/tail ceilings; net P&L drives the halts — exactly R2 §0.

---

## NEXT STEPS

- **Owner: operator.** Adopt **game 10% / combo 1%** as the shipping defaults for
  the $2,000 book (frontier supports 15%/1% as the ratio-max upgrade, gated on
  multi-week). Sign off the halts (daily 8%, drawdown 12%, hard-trip 15%) and the
  absolute backstop (2.5× = $5,000) — these are re-derived for $2k and differ
  from R2's $5k numbers (daily 8% not 4%, because $2k variance is proportionally
  larger).
- **Owner: measurement (pre-registered, multi-week, per `feedback_no_refit_on_pnl`).**
  Re-run this exact sweep on pooled WC+MLB+NFL weeks; watch whether the
  worst-day/drawdown tail fattens (would push game% down) and whether the markup
  plateau holds (would move break-even). The theme cap (15%) needs realized
  cross-game correlation on losing nights before it can move off the R2 default.
- **Owner: engineering.** Wire these as the % values behind R2's
  `committed_payout_cc` caps; the per-combo 1% cap is the highest-value one to
  land first (it dominates the whole ratio surface).
