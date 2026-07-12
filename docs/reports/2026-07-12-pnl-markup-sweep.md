# P&L markup sweep — WC + MLB, wired config, two-tier (normal 0-2¢ / fat 0-8¢)

**Date:** 2026-07-12 · **Window:** ph4 (WC group+KO stage; a favorite-hot MLB
week — the same window the capstone showed makers lost −$1.23M on game-lines) ·
**Config:** merged wired (`560e8d3`). **P&L graded on SETTLEMENT** (resolved
combos only). Script: `zerogaps/pnl_sweep.py`.

## Method (parlay-seller mechanics)

Requester buys the combo YES at the clearing price; we (maker) sell YES = take
NO. Our ask = wired fair + markup m. We WIN a trade iff our ask ≤ the price it
cleared at. On a won trade of `size` contracts: premium = size·ask; payout =
size·1 if the combo settled YES (parlay hit) else 0; **P&L = premium − payout**;
gross bankroll (max potential payout) = size·1. Split: **NORMAL** = maker markup
room (median clearing − our fair) ≤ 2¢; **FAT** = room > 2¢ (makers charging a
lot). **This is a THERMOMETER on ONE window — not a refit input.** The
win-model assumes we capture all volume where competitive, so absolute $ are
volume-optimistic; the SHAPE (P&L vs markup) is the signal.

## WORLD CUP / SOCCER — 21,331 combos (14,224 resolved); NORMAL 13,272 / FAT 8,059

**NORMAL (tight maker markup) — sweep 0-2¢:**

| mk¢ | won combos | fill% | contracts | YES/NO | premium$ | payout$ | **P&L$** | P&L/ct¢ | bankroll$ | ret% | YES-hit% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.0 | 11,399 | 86% | 23.5M | 2.13M/21.4M | 1.81M | 2.13M | **−319k** | −1.36 | 23.5M | −1.4 | 9.1 |
| 0.5 | 9,009 | 68% | 15.0M | 1.48M/13.5M | 1.34M | 1.48M | **−139k** | −0.93 | 15.0M | −0.9 | 9.9 |
| 1.0 | 6,428 | 48% | 9.3M | | 992k | 1.11M | **−117k** | −1.26 | 9.3M | −1.3 | 11.9 |
| 1.5 | 4,230 | 32% | 5.1M | | 678k | 782k | **−104k** | −2.05 | 5.1M | −2.0 | 15.4 |
| 2.0 | 2,319 | 17% | 2.4M | | 441k | 557k | **−117k** | −4.79 | 2.4M | −4.8 | 22.9 |

**FAT (makers charging a lot) — sweep 0-8¢:**

| mk¢ | won | fill% | contracts | premium$ | payout$ | **P&L$** | P&L/ct¢ | ret% | YES-hit% |
|---|---|---|---|---|---|---|---|---|---|
| 0.0 | 8,059 | 100% | 40.8M | 8.72M | 10.18M | **−1.46M** | −3.58 | −3.6 | 24.9 |
| 2.0 | 8,059 | 100% | 38.2M | 9.08M | 9.58M | **−495k** | −1.29 | −1.3 | 25.1 |
| 3.0 | 6,294 | 78% | 32.3M | 8.48M | 8.52M | **−37k** (break-even) | −0.12 | −0.1 | 26.3 |
| 4.0 | 4,656 | 58% | 27.8M | 7.72M | 7.02M | **+695k** | +2.50 | +2.5 | 25.3 |
| 5.0 | 3,471 | 43% | 22.7M | 6.60M | 4.99M | **+1.61M** | +7.09 | +7.1 | 22.0 |
| 6.0 | 2,610 | 32% | 18.5M | 5.58M | 3.45M | **+2.12M** | +11.47 | +11.5 | 18.7 |
| 8.0 | 1,559 | 19% | 13.1M | 4.21M | 1.99M | **+2.23M** | +16.97 | +17.0 | 15.1 |

## MLB — 6,967 combos (3,127 resolved); NORMAL 6,615 / FAT 352

**NORMAL — sweep 0-2¢ (favorite-hot window):** loses at every markup
(−$804k@0¢ → −$49k@2¢, P&L/ct −15 to −38¢, YES-hit climbing 33%→66% =
heavy adverse selection). This is the known −$1.23M favorite-hot week.

**FAT — sweep 0-8¢:** profitable at every markup (+$3k@0¢, peak **+$8.3k@2¢**
(+16.2¢/ct), then decays as fill drops). Best ~2-3¢.

## What this says (honest)

1. **This window LOST on NORMAL/competitive flow, both sports** — thin-margin
   flow into a favorite-hot window is adversely selected (YES-hit rises with
   markup). Least-bad ≈ 0.5-1.5¢ soccer, ~2¢ MLB, but negative here. Consistent
   with the −$1.23M maker week; it is outcome variance, not a strategy verdict.
2. **The FAT tier is the edge, and it wants a FAT markup.** Where makers pad
   (longshot corner/advance parlays; prop-heavy MLB), we profit only by charging
   a big markup too — **soccer flips positive at 4¢, best 5-6¢ (+$1.6-2.1M,
   +7-11%/bankroll, 32-43% fill); MLB best ~2-3¢.** At thin markup we lose
   because we undercut into flow that hits ~25%.
3. **Adverse-selection asymmetry (the durable structural finding):** on NORMAL
   flow, going wider ATTRACTS hitters (YES-hit 9%→23%); on FAT flow, going wider
   SELECTS non-hitters (YES-hit 25%→15%). So the correct markup is **per-tier**,
   not one number: quote NORMAL flow thin (~1¢) or pass; quote FAT flow FAT.
4. **Capital reality:** parlay selling ties up huge bankroll for thin premium
   (soccer normal 0¢: $23.5M potential payout for $1.8M premium). Return-on-
   bankroll is the metric that matters, and it's only attractive on the FAT
   tier at fat markup (+7-17%).

## NEXT STEPS

- **This is ONE window — do NOT set markup off it.** The durable output is the
  two-tier shape + adverse-selection asymmetry. The markup DECISION needs
  POOLED MULTI-WEEK, GAME-CLUSTERED data (never refit on a P&L window).
- Confirmed operator design: two-tier book — NORMAL (competitive) thin ~1¢ or
  skip; FAT (padded) charge 5-6¢ soccer / 2-3¢ MLB. Validate on pooled windows.
- Feeds #15 weekly settlement cadence (accumulate windows) → the E markup
  decision.
