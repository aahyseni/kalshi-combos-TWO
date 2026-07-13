# The soccer FAT edge is SAME-GAME only — multi-game exotics have no edge (2026-07-13)

**Scope:** split the settlement-graded soccer universe (the `2026-07-13-wc-mlb-markup-regrade`
data) by game-membership. **Trigger:** the live bot quotes `KXMVESPORTSMULTIGAMEEXTENDED`
multi-game parlays; operator asked whether the validated +5.8pp edge actually covers them.
**Harness:** `tools/backtests/wc_mlb_regrade/04_samegame_split.py` (reuses stage-03's exact
method + input `graded_settled.csv`; only added dimension = distinct games per combo via the
`\d{2}[A-Z]{3}\d{2}[A-Z]{6}` game-key regex on leg tickers). Fair-INDEPENDENT reality test.

## Result — the edge lives entirely in same-game

8,082 resolved soccer combos: **4,183 same-game / 3,899 multi-game.**

| Soccer FAT bucket | n | implied-hit | actual-hit | overprice [CI5,CI95] | verdict |
|---|---|---|---|---|---|
| **Same-game (SGP)** | 2,397 | 20.7% | **10.6%** | **+10.1pp** [+6.7, +11.7] | ✅ **strong real edge** |
| **Multi-game (exotic)** | 1,485 | 17.8% | 19.0% | **−1.2pp** [−6.3, +2.6] | ❌ no edge / slightly adverse |

Reality test on the whole tier (not just FAT) agrees: soccer ALL/same +4.8pp [CI5 +1.6] REAL;
soccer ALL/multi −3.1pp [CI5 −6.1] adverse.

**Markup sweep (FAT), CI5 day-clustered:**

| markup | same-game edge¢/ct [CI5] | multi-game edge¢/ct [CI5] |
|---|---|---|
| 2.2¢ | +6.13 [**+2.27**] | −2.87 [−8.13] |
| 3.0¢ | +7.39 [**+3.56**] | +0.08 [−6.60] |
| 4.0¢ | +8.67 [**+5.46**] | +4.10 [−1.76] |

Same-game: robustly +EV, CI5 > 0 from 2.2¢ up, YES-hit falls as markup widens (textbook FAT
self-selection). Multi-game: **CI5 never clears zero** at any markup we'd use.

## Interpretation

- The pooled +5.8pp was a **strong +10.1pp same-game edge diluted by a ~0/adverse multi-game
  bucket.** Retail overpays for **correlated same-game narratives** (advance ∧ BTTS ∧ star
  scores); cross-game legs are ~independent and priced efficiently → nothing to harvest.
- On multi-game, actual (19.0%) > implied (17.8%) > our stub fair (13.0%): the market is NOT
  overpricing multi-game flow, and our recorded fair *underprices* it → we'd think we have
  room when we don't. Adverse selection, not opportunity.

## Operational consequence (the reason this matters NOW)

The live bot (`config/prod-live-wc.local.yaml`) quotes almost entirely **multi-game** combos —
the **no-edge** bucket. Every open quote sampled on prod was `MULTIGAMEEXTENDED`.
**The `collection_whitelist` cannot fix this:** both `KXMVESPORTSMULTIGAMEEXTENDED` (3,392 same
/ 3,201 multi) and `KXMVECROSSCATEGORY` (791 / 698) are ~50/50 same- and multi-game. Separating
them requires a **per-combo game-count gate** (decline any combo whose legs span >1 game).

Same-game is also **more competitive**: in-the-money (ask ≤ clearing) on ~47% of same-game vs
~26% of multi-game at 3¢ — so restricting to same-game raises BOTH edge and fill-rate. This is
a likely contributor to the live **0-fills** observation (we're quoting the low-competitiveness,
no-edge pond).

## CAVEATS

1. **One week / 6 match-days.** Same finding-class as the parent re-grade: first sample toward
   a pooled number, NOT a P&L refit. But note this is a **fair-independent structural**
   measurement with a clear mechanism (same-game correlation overpricing), and the action it
   implies is *conservative* — quote where edge is measured, decline where it is measured absent
   and the point estimate is adverse. Pooling weeks continues.
2. **Stub fair** for the FAT/NORMAL tier cut (independence recorder fair, not live engine). The
   reality-test overprice is fair-independent so the same/multi split stands; the tier-conditioned
   magnitudes are indicative. Sharpen by re-pricing with the live engine (hard rule 8).

## NEXT STEPS

1. **Owner: bot (pending operator OK).** Wire a **same-game-only gate** (per-combo distinct-game
   count == 1 → quote; > 1 → decline) — prototype-in-test → port → parity-check (hard rule 8).
   Puts us on the +EV pond and improves competitiveness.
2. **Owner: bot.** Add live **competitiveness logging** (our ask vs eventual clearing per RFQ) to
   distinguish "fair too high" from "requester picked another maker" on the 0-fills question.
3. **Owner: bot.** **Live-engine MLB re-grade** to de-confound the MLB verdict before any MLB
   go-live decision (operator asked about MLB volume).
4. **Owner: measurement.** Pooled multi-week remains the markup gate; add same/multi as a
   standing split in future re-grades.
