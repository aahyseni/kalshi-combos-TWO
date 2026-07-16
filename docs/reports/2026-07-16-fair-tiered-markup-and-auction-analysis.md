# 2026-07-16 — Fair-tiered markup (pad longshots, keep mains tight) + why mains don't fill

## Operator question

All 10 auction wins today were longshots (fairs 1.2–14.5¢); zero wins on
35–65¢ mains despite 235 main quotes. Are we too loose on longshots and too
wide on mains?

## Findings

1. **Win rate by fair bucket (today):** longshot <10¢ 946 quotes / 8 wins
   (0.85%); mid 10–35¢ 712 / 2 (0.28%); main 35–65¢ 235 / 0; favorites 11 / 0.
2. **We are NOT overpriced on mains.** 2026-07-14 backtest (900k trades, 26
   liquid mains): our ask median −0.75¢ UNDER clearing, cheaper on 17/25; live
   probe same day: cheapest maker on 6/7, tightest competitors fair+1.5–2.2¢.
   Today's main asks run fair+1.1–1.9¢ (uncertainty width binds over the 1¢
   markup on scorer/corners-carrying mains) — front of the pack, overlapping
   the sharpest makers.
3. **Why mains still don't fill — the conversion product, not the price:**
   fill = P(RFQ trades at all ≈13%) × P(we reached it alive — 63% of RFQs die
   before we act) × P(our quote still resting at the swipe — 20s TTL, slot
   saturation) × P(best at that instant) × P(we don't veto our own win — 10/10
   wins vetoed today pre-fix). 235 main quotes on a T-2/3-days quiet day ⇒
   expected mains fills ≈ low single digits even priced perfectly; 0 is noise.
   Real mains volume trades near kickoff (FRA-ENG 7/18, ESP-ARG 7/19).
4. **We ARE too loose on longshots.** Competitors pad longshot parlays
   fair+2–8¢ (FAT regrade: longshots settle 13.8% vs priced 19.6% — retail
   overpays ~5.8pp); we asked fair+1–1.2¢ and won by undercutting a pad we
   didn't need to undercut by that much. Every longshot win = 1–3¢/ct left on
   the table.

## Change shipped (operator-directed)

`MarkupTier` fair-dependent tiers in the markup layer (config-driven,
dark-parity preserved, engine passes `fair_cc=round(joint.p·10000)`), armed:

| fair bucket | old ask | new ask |
|---|---|---|
| <2¢ deep longshot | fair+1¢ | **fair+5¢** |
| 2–10¢ longshot | fair+1¢ | **fair+4¢** |
| 10–35¢ mid | fair+1¢ | **fair+2¢** |
| ≥35¢ main | fair + max(width,1¢) | unchanged (already at the sharp end) |

Corners +3¢ adder stacks on top. Expect the longshot WIN RATE to drop and the
edge per fill to rise 3–4×; measure per-bucket fill rate + edge/fill, re-tune
only on pooled evidence (never a P&L refit).

## Measurement now possible

Shadow recorder RESTARTED 15:27 UTC (down since 7/12) — same-instant
competitor clears for the exact FRA-ENG/ESP-ARG combos accumulate from now.
After the weekend: per-bucket us-vs-market table on live data. A live RFQ
probe script is staged (scratchpad `rfq_probe.py`, never accepts) if the
operator wants today's snapshot; blocked on operator permission.

## NEXT STEPS

1. Watch per-bucket win rate under the tiers (agent; game days = the test).
2. Problem-A last-look MC gate build in flight (`wf_10ee06f2-aa1`) — converts
   wins into fills.
3. Quote-time vs confirm per-combo sizing discrepancy on target-cost RFQs
   (the $414-premium quote that passed quote time under a $102 cap) — investigate
   after Problem A lands (agent).
4. Operator: run the RFQ probe if today's competitive snapshot is wanted.
