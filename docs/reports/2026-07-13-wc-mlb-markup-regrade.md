# WC/MLB markup re-grade — the edge is real, and it's World Cup FAT flow

**Date:** 2026-07-13 · **Scope:** settlement-graded two-tier markup sweep + a
reality test, on the one-week prod shadow recording. Supersedes the resolution
caveat of `2026-07-12-pnl-markup-sweep.md` (that window was majority-UNRESOLVED at
snapshot; a week later it has settled).
**Harness (persisted, reproducible):** `tools/backtests/wc_mlb_regrade/`.

## Data provenance (sources of truth — verified, not remembered)

- **Fair + features + clearing:** `data/combomaker-prod.sqlite3` (READ-ONLY,
  `mode=ro`) — the prod shadow recording, **2026-07-06 → 2026-07-12** (one week).
  `combo_trades.yes_price_cc` = real clearing; `would_quotes.fair_cc` = our
  recorded fair; `rfqs` = legs.
- **Settlement:** live Kalshi API `GET /markets?status=settled` per leg series →
  each market `result`; combo settles by parlay-AND with early-NO short-circuit.
- **Graded universe:** 18,124 combos that BOTH traded AND we would-quoted;
  **12,293 now RESOLVED** (soccer 73% / MLB 79% — vs ~26% at the Jul-12 snapshot).

## Method

Parlay-seller mechanics: requester buys combo YES at clearing; we sell YES (=take
NO) at ask = fair + markup m. We WIN iff m ≤ room (room = clearing − fair). Per-
contract P&L on a won combo = ask − combo_yes. Equal-weight per combo (robust to a
few huge combos). Tiers: NORMAL = room ≤ 2¢, FAT = room > 2¢. CIs = **match-day-
clustered** block bootstrap (2000 resamples, fixed seed).

**The reality test** (decides real-edge vs favorite-hot beta, and does NOT use our
fair, so it's robust to the stub-fair caveat below): as a seller we profit only if
the flow settles YES *less* often than the market's clearing price implied. Compare
implied-hit (mean clearing prob) vs actual-hit (settlement), day-clustered.

## Results

| Sport / tier | n (resolved) | implied-hit | actual-hit | overprice [CI5,CI95] | verdict |
|---|---|---|---|---|---|
| **Soccer FAT** | 3,882 | 19.6% | **13.8%** | **+5.8pp** [+4.2, +6.8] | ✅ **REAL EDGE** |
| Soccer NORMAL | 4,200 | 16.3% | 19.7% | −3.4pp [−5.6, −2.4] | ❌ adverse (settles against us) |
| MLB FAT | 475 | 30.7% | 34.9% | −4.2pp [−7.3, +2.0] | ❌ negative (straddles 0) |
| MLB NORMAL | 3,051 | 16.4% | 19.8% | −3.4pp [−7.4, +1.8] | ⚠️ unconfirmed |

**Soccer FAT markup sweep** (day-clustered CIs) — textbook self-selection:

| markup | won | fill% | edge¢/ct | [CI5] | YES-hit% |
|---|---|---|---|---|---|
| 2.2¢ | 3,623 | 93 | +2.90 | **+0.72** | 12.9 |
| 3.0¢ | 2,972 | 77 | +4.89 | **+2.14** | 11.5 |
| 4.0¢ | 2,294 | 59 | +7.26 | **+4.51** | 9.9 |
| 5.0¢ | 1,796 | 46 | +9.36 | +6.67 | 9.0 |

Edge is monotone increasing in markup, CI5 stays > 0 from 2.2¢ up, and **YES-hit
FALLS as we widen** (fat markup selects non-hitters) — the FAT-tier thesis, now
confirmed on full settlement. Min robustly +EV markup: **2.2¢**.

## Honest reading

1. **World Cup FAT flow is a real, day-clustered-significant seller edge** — WC
   longshot parlays settle 13.8% but were priced at 19.6% (retail overpays ~6pp).
   Survives the day-clustered CI and (prior run) excluding the 25 biggest combos.
2. **Soccer NORMAL is genuinely adverse this week** — settles 19.7% vs priced
   16.3%, CI excludes zero. Caps would bound the bleed, but the *sign* is negative;
   this is selection, not just outcome-variance. (Caveat: 6 match-days.)
3. **MLB shows no edge this week**, BUT the MLB verdict is **confounded** — see
   caveat 1 below (our fair on MLB-FAT said 21.1% vs 34.9% actual = our *stub* fair
   badly underpriced them; the real MLB props engine may price very differently).
   Within 10–20% on 6 days is inside variance; MLB is a re-price + more-weeks item,
   not a "broken" item.

## CAVEATS (do not over-read)

1. **Independence-stub fair.** Fairs are the observe recorder's stub, not the live
   engine. Overstates room for correlated combos → the markup *magnitude* is
   indicative; hits **MLB hardest**. The reality test is fair-independent, so the
   WC-FAT edge stands. Refinement: re-price offline with the live engine on the
   recorded `leg_probs` (hard rule 8).
2. **One week / 6–8 match-days.** First sample toward the pooled number; the markup
   DECISION stays gated on ≥3–4 game-clustered weeks — never a P&L refit
   (`feedback_no_refit_on_pnl`).
3. **Volume-optimistic** win model; real fill rate is lower than fill%.

## NEXT STEPS

- **Owner: operator.** Restart the shadow recorder (new server) to accumulate
  weeks 2–4 → the pooled multi-week markup (the real gate).
- **Owner: bot.** WC-FAT go-live at a provisional 3¢ (validated +EV band 2.2–8¢;
  3¢ = CI5 +2.1, ~77% competitive) — see
  `2026-07-13-markup-mechanism-wc-golive.md`. Provisional, one-week; confirm as
  weeks pool. Capture the closing WC window; NORMAL off, MLB off.
- **Owner: bot (deferred).** Re-price the graded universe with the LIVE engine to
  de-confound MLB and sharpen the soccer number.
