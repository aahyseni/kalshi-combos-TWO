# MLB settlement P&L sweep (markups 0–5¢) — the soccer-methodology rerun on honest fairs

**Date:** 2026-07-10 ~15:00 UTC · **Method:** identical to
`2026-07-08-soccer-settlement-pnl.md` — for every RESOLVED, pregame-cleared
MLB-strict combo (n=5,604; 11.07M contracts; honest fixed fairs from the
corrected rerun), sweep markup m: we "win" a print when our YES offer
(fair+m) ≤ the actual clearing; filled at OUR price; P&L/ct = offer −
settle_yes. **Hard caveat first: all of this rests on 52 distinct game-dates
(≈4 days of baseball) — outcome variance dominates.**

## The context that explains everything: the ACTUAL market this window

| bucket | actual maker P&L | ¢/ct | taker YES-hit | contracts |
|---|---|---|---|---|
| game-lines | **−$1,227,708** | **−12.85¢** | 28.7% | 9.55M |
| mixed | −$48,863 | −15.83¢ | 34.1% | 0.31M |
| **props-only** | **+$21,941** | **+1.81¢** | **2.9%** | 1.21M |

The week ran favorite-hot (33/49 popular sides won; 3 of 4 dates hot — the
settlement forensics' p=0.063 finding). EVERY parlay seller in the market got
run over on game lines. Not concentration (10 worst prints = 5% of losses) —
breadth. This is an outcome draw on ~52 games, not a pricing verdict.

## Our sim (promoted engine, honest fairs)

| markup | won ct (share) | P&L | ¢/won-ct | YES-hit |
|---|---|---|---|---|
| 0¢ | 9.29M (84%) | −$947,947 | −10.21 | 23.1% |
| 0.5¢ | 2.88M (26%) | −$452,544 | −15.73 | 38.7% |
| 1¢ | 1.06M (9.5%) | −$107,960 | −10.23 | 36.7% |
| 1.5¢ | 0.44M (4.0%) | −$24,843 | −5.60 | 33.0% |
| 2–3¢ | 0.1–0.23M | −$18k..−$40k | −17..−23 | 47–54% |
| 4.25–4.75¢ | ~0.03M | ≈ $0 | ≈ 0 | 34–39% |

By bucket at 1¢ markup: game-lines **−$99,641 (−11.6¢/ct)** · mixed −$15,227 ·
**props-only +$6,907 (+6.8¢/ct, YES-hit 19%)**. At 0¢: props +$13,452
(+1.2¢/ct, YES-hit 2.8%).

## Honest reading

1. **The game-line losses are the week, not the engine.** Our 0-markup sim
   (−10.2¢/ct) beats the actual makers (−11.3¢/ct) on like-for-like flow — we
   priced better and would still have lost, because favorites hit above
   everyone's prices for four days. Whether that's luck or a persistent
   market-underprices-favorites effect is exactly the open question
   (game-clustered p=0.063; needs 2–4 more weeks of settlements).
2. **Props are the business.** Structurally profitable at every markup 0–3¢,
   with 2.8% YES-hit at zero markup — the lottery-ticket overpayment
   (sub-2¢-fair combos clearing at ~8¢) measured as realized dollars. This is
   the flow our new correlation stack prices best (0.49–0.70¢ vs clearings).
3. **The 2–2.5¢ markup trough is adverse selection made visible:** at wide
   markups we win only flow whose clearing sits far above our fair — the
   YES-hit rate jumps to ~50%. Matches the soccer sweep's toxicity gradient.
4. **vs soccer (+$8,728 peak at 1¢):** opposite-sign windows; both are
   single-week draws on clustered outcomes. Calibration (0.34¢/94% within-2¢)
   is the stable metric; P&L sweeps become meaningful with settlement volume.
5. Binary settlements only (rain-scalar combos excluded by resolution filter).
   No position caps or market impact modeled; counterfactual assumes the taker
   routes to the best quote.

## NEXT STEPS
- Re-run this sweep weekly as settlements accumulate (recorder running); the
  markup decision (deferred 2026-07-09) should use pooled multi-week results,
  bucket-split — early shape suggests: quote props tight (≤1¢), game-line
  parlays wide-or-skip pending the favorites question.
- Per-print chronological backtest mode (proposed) + Phase 6 paper shadow =
  the pipeline that makes these sweeps continuous.
