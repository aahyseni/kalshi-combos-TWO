# Soccer settlement backtest — real outcomes, maker profit & our simulated P&L

**Date:** 2026-07-08 · **Scope:** WC (`KXWC*`) combos only, UCL/UEL/UECL excluded
to match production gating · **Status:** DONE · **Why it exists:** the
vs-clearing backtest (2026-07-08 backtest report) compares our fair to the
maker's **quote**, which mixes markup and mispricing inseparably. Settlement is
the only ruler that separates them — real outcomes, not maker quotes.

## Method

- **Settlement:** public `GET /markets/{ticker}` → `status`,`result`. A leg is
  resolved iff `status∈{finalized,settled}` & `result∈{yes,no}`; a leg **wins**
  iff `result==selected side`; a combo settles **YES** iff **every** leg wins; a
  combo is **eligible** only if all legs resolved (pending legs excluded — they'd
  show garbage). 336 distinct legs fetched; 183 settled.
- **Maker P&L** (accounting only, no engine): maker short-YES on `taker_side=='yes'`
  ⇒ per-contract `clearing − 1{YES}`; long-YES on `taker_side=='no'` ⇒ `1{YES} −
  clearing`. Contract-weighted (`count_centi/100`).
- **Our sim:** us as the maker **selling** the YES parlay to YES-buyers. Price
  each trade through the **live soccer engine** at the marginal snapshot nearest
  the trade time; quote `our_fair + markup`; we **win** the flow iff `our_ask ≤
  clearing` (we undercut/match); settle at the real outcome. "Unlimited size" =
  we take the full traded volume on every trade we'd win. Restricted to
  `taker_side=='yes'` (97% of flow; the sell-parlay book).
- **⚠ Methodology fix (mid-run):** the first pass wrongly **included UCL** combos
  (leg prefix `KXUCL`, wrapped in a generic `KXMVE` collection ticker). One UCL
  `SPREAD+TOTAL` combo — 107,781 ct, our fair 77¢ vs clearing 23¢, settled NO —
  is a two-legged-tie with aggregate settlement we don't model and **gate off in
  prod**. It alone wrecked the calibration (Brier 0.23→**0.04** once removed).
  All numbers below are **WC-only**, matching the surface we actually quote.

### Audit of the UCL exclusion (operator challenged the shift — verified)

172 resolved = **123 WC + 49 UCL**. P&L reconciles to the cent:
`WC +$6,660.01` + `UCL +$26,936.14` = `+$33,596.15` (= all-computed, exact match).
**~75% of the shift is ONE combo:** the UCL `SPREAD+TOTAL` above,
`+$25,196` on 107,781 ct (settled NO). A maker sold it at ~23¢ and it busted.
Even ungated we'd have won **none** of it (our fair 77¢ ≫ 23¢ clearing), so
excluding it hides no money we'd have made — it just removes a combo whose
settlement we mis-model. Purity check: every resolved leg prefix is `KXWC*` or
`KXUCL*` — no MLB/non-soccer. Units: `count_centi/100`=contracts,
`yes_price_cc/10000`=dollars; sign hand-verified (NO combo + taker-YES ⇒ maker
keeps premium). `verify_ucl.py`.

## Sample (be honest about it)

| | |
|---|---|
| Resolved WC combos | **123** (of 1,480 soccer; 53 UCL excluded; **1,304 pending**) |
| Pending = mostly **Jul 9–11** knockouts (still unplayed as of Jul 8) | — |
| Settled YES / NO (by combo) | **6.5% / 93.5%** |
| Settled YES / NO (by volume) | 4.4% / 95.6% |

**This is an early-rounds sample.** The recent "top players advance AND score"
streak the operator flagged is in the **pending Jul 9–11 set** — NOT yet
measurable here. Do not read the resolved advance+scorer result as covering it.

## 1. How much did the makers actually profit?

| Flow | Maker P&L | Edge |
|------|-----------|------|
| **All WC resolved** | **+$6,660** | **+3.05¢/ct** (218,608 ct) |
| `taker_side=yes` (parlay buyers) | **+$8,983** | +4.44¢/ct (202,155 ct) |
| `taker_side=no` (parlay faders) | **−$2,323** | −14.12¢/ct (16,454 ct) |

**The sharp money is on the NO side.** Selling parlays to YES-buyers is a
+4.44¢/ct business; the NO-fade flow beats the maker by −14.12¢/ct. (On the
full soccer set incl. UCL, makers made +$33.6k — i.e. **~$27k of maker profit
was UCL combos we don't quote.**) Net still positive because YES flow is ~12× the
NO flow.

## 2. Our simulated P&L selling the WC parlay (markup sweep, settled)

```
markup      P&L      won ct   won/avail  $/won ct  trades  YES-hit% of fills
 0.0c   $ +5,830.56  136,628    67.6%     +4.27c    603      3.8%
 1.0c   $ +8,727.83   97,337    48.1%     +8.97c    498      1.3%   <- best total $
 2.0c   $ +6,667.92   65,988    32.6%    +10.10c    350      1.9%
 3.0c   $ +2,481.41   24,608    12.2%    +10.08c    139      4.9%
 4.0c   $ +1,005.58   14,286     7.1%     +7.04c     76      7.7%
 5.0c   $   +110.06    7,414     3.7%     +1.48c     52     14.8%
```

**Findings:**
- **The book is +EV.** At 1¢ markup we net **+$8,728**, ≈ the aggregate makers'
  total dollars (+$8,983) while winning only **half** the flow — at **double**
  the per-contract edge (+8.97¢ vs makers' +4.44¢). Our engine is competitive on
  WC.
- **Markup is adversely selected — the headline.** P&L **peaks at ~1¢ and
  falls**, and the **YES-hit rate of our fills rises monotonically with markup
  (1.3%→14.8%)**. Raising the ask filters our fills down to exactly the combos
  the market priced high *because they were genuinely more likely to hit* — and
  they do. **Wider markup = more toxic book, not safer.** This *tensions* the
  standing "wide markup while capital low" prior ([[feedback_combos_markup_capital]]):
  fat markup protects per-fill margin but selects informed flow. For a parlay-
  seller against 93%-busting flow, **thin markup + broad volume beats fat markup
  + thin volume.**

## 3. Settlement calibration (the accuracy ruler)

Contract-weighted, priceable WC yes-flow: **mean our_fair 8.6¢ vs actual YES-rate
4.2¢ (+4.3¢), Brier 0.0400.** Reliability (well-calibrated where the volume is):

```
fair  0-10c -> realized YES  0.6%  (104,690 ct)   <- 93% of volume; well-calibrated
fair 10-20c -> realized YES  9.8%  ( 28,130 ct)   <- ~ok
fair 20-30c -> realized YES 43.7% (  3,801 ct)    <- small-n, noisy
fair 30-40c -> realized YES 17.7% (  5,418 ct)    <- small-n
fair 40-50c -> realized YES  0.0% (  1,395 ct)    <- tiny-n
```

The +4.3¢ mean gap = our fair sits slightly **above** the realized rate (mildly
conservative for a seller — we don't sell too cheap). Mid-buckets are small-n
noise, not a calibration trend.

## 4. By family @ +2¢ markup (our P&L)

```
 +$3,167  +13.86c/ct  hit  0%  ADVANCE+BTTS+TOTAL          <- our workhorse winner
 +$1,998   +7.84c/ct  hit  0%  CORNERS_TEAM
 +$936    +30.63c/ct  hit  0%  BTTS+CORNERS
 +$237     +9.96c/ct  hit  0%  ADVANCE+PLAYER_GOAL         <- operator's flag: busted here, we profited*
 ...
 -$95     -67.76c/ct  hit 100% BTTS+TOTAL                  (n=139 ct)
 -$167    -91.53c/ct  hit 100% FIRST_HALF_ML+ML+PLAYER_GOAL+TOTAL (n=183 ct)
 -$431    -55.36c/ct  hit  79% ADVANCE+BTTS+CORNERS_TEAM   (n=778 ct)
```

*`ADVANCE+PLAYER_GOAL` settled **0% YES** in this early-rounds sample, so we
profited — but this is **not** the hot Jul 9–11 round. The losers are small-n
same-game stacks that *did* come in (favorite dominates: 1H+game+scorer+over
hitting together) — a hint of exactly the correlation the operator flagged, but
too few contracts to calibrate on yet.

## Bottom line

- Selling WC parlays is **+EV and our engine is competitive** (matches maker
  dollars at 1¢ markup, better per-contract calibration).
- **Optimal markup is thin (~1¢), not fat** — the adverse-selection gradient is
  the real finding. Revisit the "wide markup early" prior for this book.
- **Avoid / defensively price NO-fade flow** (−14¢/ct against the maker).
- **The verdict on advance×scorer correlation is NOT YET IN** — it lives in the
  pending Jul 9–11 combos. Re-run this backtest after they settle.

## NEXT STEPS

- **Runs next (owner: next session):** re-run this settlement backtest once the
  **Jul 9–11 knockouts settle** — that resolved set contains the advance+scorer
  streak the operator flagged; it's the real test of whether we under-model that
  correlation. Recorder is still gathering.
- **Owner (operator):** decide whether to (a) adopt **thin (~1¢) markup** for the
  WC sell-parlay book given the adverse-selection curve, and (b) add a
  **NO-side/fade defense** (widen or decline when the taker wants NO).
- **Decision owed by operator:** confirm UCL stays excluded from all soccer
  backtests (it's gated in prod; including it is a bug — now fixed here).
- **Flagged:** persist `phaseA_maker_pnl.py`/`phaseB_sim.py`/`fetch_settle.py`
  into `tools/backtests/` so this is reproducible after job-tmp is cleaned.
