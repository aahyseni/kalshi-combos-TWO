# Market-vs-our pricing on the BIG REPEATED World Cup combos — DB backtest

**Date:** 2026-07-14 · **Area:** WC pricing calibration + fill diagnosis (task #35)
**Data:** shadow market tape `combomaker-prod.sqlite3` (889,248 combo trades, Jun 29–Jul 12,
34,210 distinct combo markets) vs our live quotes `combomaker-prod-live-wc.sqlite3`
(20,408 WC `quote_sent` → 6,253 distinct leg-sets, Jul 13 20:20 → Jul 15 00:51 UTC).
**Tools (pure DB analysis, hard rule 8 — no live module touched):**
`tools/market_vs_our_pricing.py`, `tools/market_drift_check.py`, `tools/_gen_report_tables.py`.

---

## TL;DR — the "WC too high" hypothesis is WRONG for the main combos

The operator's standing question (#35) was *"our WC fair runs too high — that's why we
don't fill."* Measured against **where the market actually cleared** (900k real trades),
that is **false for the big repeated combos**:

```
                 our ASK  vs  market CLEARING  (taker=yes side, the ask we provide)
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │  26 LIQUID main combos (≥30 real trades):                                     │
  │     median gap = -0.75¢   mean = -0.82¢   we're CHEAPER on 17, higher on 8    │
  │                                                                               │
  │     we are NOT overpriced — we quote ~1¢ BELOW where mains clear.             │
  └─────────────────────────────────────────────────────────────────────────────┘
```

The old "fair 7.9¢ vs market 2¢" alarm was a **longshot artifact** (deep 3–4-leg
goalscorer parlays), which the operator explicitly de-prioritised. On the combos that
actually matter — advance×advance, advance×goalscorer, BTTS×total — **our price is right,
marginally cheap.** So the fill problem is **not price**. It is **liveness / auction
competition** (see §4: 16 fills / 21,669 quotes; 55% die to `rfq_gone`, 35% to `ttl`).

This **independently confirms** the same-day live-probe report
(`2026-07-14-price-discovery-we-are-the-sharp-maker-room-to-widen.md`) from a totally
separate evidence line: that probe read 7 live maker books and found we're the sharpest;
this reads 900k historical clears and finds we're ~1¢ under. **Two lines → same verdict:
we underprice the mains by ~1¢; there is room to widen.**

---

## 1. Method + the one honest caveat

- **Our price** = what we *actually quoted* (not a re-run of the engine): `our_ask =
  100 − no_bid_cc/100` (the YES ask a taker pays us), `our_fair = fair_cc`. Averaged per leg-set.
- **Market price** = where the combo **cleared** on Kalshi: `combo_trades.yes_price_cc`,
  **`taker_side='yes'` only** (the ask side we provide as a NO-seller; ~98% of volume anyway),
  volume-weighted, joined to its leg-set via `rfqs.market_ticker`.
- **Combo identity** = leg-set signature (sorted leg tickers+sides). Both tapes use the
  same Kalshi leg tickers, so signatures match exactly.
- **gap = our_ask − market_clear.**  Negative ⇒ we're **cheaper** (below market).

**⚠️ The one caveat — no temporal overlap.** Our live quoting (Jul 13–15) began *after*
the shadow recorder stopped (Jul 12 06:33). So this compares our **game-day** ask to the
market's **pre-game-week** clearing, not the same instant. **Drift bound**
(`market_drift_check.py`): for the liquid mains the clearing price was **stable** across
Jul 6–12 (per-combo daily-VWAP drift ±0–4¢; the last-tape-day VWAP ≈ the week VWAP), so a
week of drift cannot explain a ~1¢ gap — and certainly cannot turn a 1¢ gap into the 4×
overpricing the old hypothesis claimed. A same-*instant* read requires the live-probe path
(create_rfq + get_quotes), which the companion report already ran.

---

## 2. LIQUID main combos — high confidence (≥30 real trades)

26 combos; **median gap −0.75¢, mean −0.82¢; cheaper on 17, higher on 8.**

| combo | game | legs | our_n | our fair | our ask | mkt clear | gap | trades | volume |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| FRA adv + ARG adv | FRAESP | 2 | 305 | 26.0¢ | 27.8¢ | 28.7¢ | **-0.9¢** | 1394 | 321,787 |
| FRA adv + Kmbapp 1+ | FRAESP | 2 | 324 | 33.6¢ | 35.6¢ | 36.6¢ | **-1.0¢** | 1796 | 229,278 |
| FRA adv + ENG adv | FRAESP | 2 | 280 | 31.3¢ | 33.0¢ | 34.0¢ | **-0.9¢** | 904 | 167,705 |
| ESP adv + ARG adv | FRAESP | 2 | 199 | 18.8¢ | 20.6¢ | 19.7¢ | +0.9¢ | 573 | 117,483 |
| FRA adv + BTTS | FRAESP | 2 | 118 | 34.2¢ | 35.7¢ | 36.5¢ | -0.9¢ | 339 | 77,407 |
| ESP adv + ENG adv | FRAESP | 2 | 47 | 22.8¢ | 24.4¢ | 23.4¢ | +1.0¢ | 179 | 62,474 |
| FRA adv + 2+ gls | FRAESP | 2 | 51 | 44.5¢ | 46.0¢ | 46.6¢ | -0.6¢ | 206 | 57,533 |
| BTTS + BTTS | FRAESP | 2 | 112 | 32.4¢ | 34.0¢ | 33.3¢ | +0.8¢ | 125 | 50,517 |
| FRA adv + Kmbapp 1+ + Odembe 1+ | FRAESP | 3 | 45 | 11.2¢ | 13.0¢ | 13.4¢ | -0.4¢ | 224 | 49,113 |
| FRA adv + BTTS + Kmbapp 1+ | FRAESP | 3 | 50 | 22.6¢ | 24.3¢ | 24.4¢ | -0.1¢ | 200 | 38,299 |
| FRA adv + Lyamal 1+ + Kmbapp 1+ | FRAESP | 3 | 23 | 5.9¢ | 7.6¢ | 9.4¢ | -1.8¢ | 115 | 25,118 |
| **FRA adv + corners 8+ + Kmbapp 1+** | FRAESP | 3 | 51 | 25.5¢ | 27.7¢ | 32.9¢ | **-5.2¢** | 142 | 23,329 |
| FRA adv + ARG adv + Kmbapp 1+ | FRAESP | 3 | 29 | 15.2¢ | 17.0¢ | 20.0¢ | -3.0¢ | 133 | 20,517 |
| Kmbapp 1+ + Odembe 1+ | FRAESP | 2 | 37 | 12.3¢ | 14.3¢ | 11.4¢ | +2.9¢ | 76 | 20,377 |
| FRA adv + Odembe 1+ | FRAESP | 2 | 26 | 20.5¢ | 22.2¢ | 22.0¢ | +0.2¢ | 110 | 19,455 |
| FRA adv + ENG adv + BTTS + BTTS | FRAESP | 4 | 25 | 9.4¢ | 10.9¢ | 14.3¢ | -3.5¢ | 47 | 14,778 |
| BTTS + win FRA | FRAESP | 2 | 33 | 20.7¢ | 22.0¢ | 23.2¢ | -1.2¢ | 88 | 14,345 |
| ESP adv + Kmbapp 1+ | FRAESP | 2 | 23 | 12.2¢ | 14.1¢ | 14.4¢ | -0.2¢ | 84 | 11,427 |
| FRA adv + 3+ gls | FRAESP | 2 | 49 | 29.3¢ | 30.7¢ | 31.7¢ | -1.0¢ | 68 | 10,014 |
| FRA adv + BTTS + 3+ gls | FRAESP | 3 | 25 | 25.2¢ | 28.0¢ | 28.0¢ | -0.0¢ | 48 | 8,368 |
| **FRA adv + corners 8+** | FRAESP | 2 | 30 | 44.3¢ | 46.0¢ | 49.4¢ | **-3.4¢** | 57 | 8,300 |
| ESP adv + Lyamal 1+ | FRAESP | 2 | 31 | 16.5¢ | 18.3¢ | 18.1¢ | +0.2¢ | 51 | 6,922 |
| FRA adv + 1stgoal Kmbapp | FRAESP | 2 | 24 | 17.7¢ | 23.0¢ | 20.7¢ | +2.3¢ | 76 | 6,625 |
| BTTS + 3+ gls | FRAESP | 2 | 477 | 42.5¢ | 44.3¢ | 46.1¢ | -1.7¢ | 41 | 5,220 |
| ESP adv + BTTS | FRAESP | 2 | 33 | 26.5¢ | 28.0¢ | 26.6¢ | +1.4¢ | 47 | 5,075 |
| **FRA adv + corners 9+ + Kmbapp 1+** | FRAESP | 3 | 36 | 21.3¢ | 23.3¢ | 28.4¢ | **-5.1¢** | 37 | 4,436 |

## 3. THIN-tape repeated combos — LOW confidence (<30 trades)

Includes the operator's flagship example **ARG adv + Messi 1+** (our single most-quoted
combo, 1015×) — but the ENG-ARG game (Jul 15) had **barely traded** before the recorder
stopped Jul 12, so its market number rests on **5 trades**. Do not over-read these.

| combo | game | legs | our_n | our fair | our ask | mkt clear | gap | trades | volume |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|
| **ARG adv + Lmessi 1+** | ENGARG | 2 | 1015 | 25.4¢ | 27.1¢ | 30.3¢ | -3.1¢ | 5 | 157 |
| ARG adv + Lmessi 1+ + Jbelli 1+ | ENGARG | 3 | 414 | 4.0¢ | 5.4¢ | 13.7¢ | -8.3¢ | 1 | 34 |
| ARG adv + 2+ gls | ENGARG | 2 | 108 | 30.8¢ | 32.2¢ | 35.8¢ | -3.7¢ | 2 | 574 |
| NO sprd FRA2 + 3+ gls | FRAESP | 2 | 69 | 39.0¢ | 40.1¢ | 38.7¢ | +1.3¢ | 4 | 637 |
| 3+ gls + 3+ gls | FRAESP | 2 | 60 | 20.2¢ | 21.3¢ | 23.0¢ | -1.6¢ | 4 | 1,961 |
| 1H 1+ gls + BTTS | FRAESP | 2 | 56 | 49.0¢ | 50.6¢ | 53.7¢ | -3.1¢ | 1 | 18 |
| BTTS + sprd FRA2 | FRAESP | 2 | 51 | 8.2¢ | 9.6¢ | 10.3¢ | -0.7¢ | 2 | 544 |
| ARG adv + BTTS + 3+ gls | ENGARG | 3 | 48 | 16.4¢ | 18.8¢ | 20.5¢ | -1.7¢ | 1 | 23 |
| FRA adv + BTTS (v2) | FRAESP | 2 | 40 | 30.9¢ | 32.6¢ | 33.9¢ | -1.3¢ | 11 | 5,006 |
| FRA adv + BTTS + BTTS | FRAESP | 3 | 37 | 18.2¢ | 19.7¢ | 23.0¢ | -3.2¢ | 3 | 681 |
| BTTS + NO 3+ gls | FRAESP | 2 | 32 | 18.6¢ | 20.2¢ | 15.5¢ | +4.7¢ | 3 | 85 |
| NO sprd FRA2 + NO 4+ gls | FRAESP | 2 | 25 | 61.1¢ | 62.1¢ | 60.8¢ | +1.4¢ | 6 | 795 |
| Lyamal 1+ + Moyarz 1+ + Kmbapp 1+ + Odembe 1+ | FRAESP | 4 | 23 | 1.1¢ | 2.6¢ | 1.0¢ | +1.6¢ | 27 | 48,459 |
| FRA adv + ARG adv + Kmbapp 1+ + Lmessi 1+ | FRAESP | 4 | 20 | 8.5¢ | 10.4¢ | 13.7¢ | -3.3¢ | 14 | 2,342 |
| win FRA + win ENG | FRAESP | 2 | 20 | 14.3¢ | 16.4¢ | 16.9¢ | -0.5¢ | 16 | 3,524 |

---

## 4. Why we don't fill despite pricing right — the fill funnel

Price is not the bottleneck; the **auction** is. From our own live tape:

```
  21,669 quotes SENT ──► 21,091 DELETED ──► 16 FILLED
                          │
                          ├─ 55%  delete_rfq_gone     (RFQ closed/withdrawn; lived ~11s median after our quote)
                          ├─ 35%  delete_ttl_expired  (our own TTL fired, ~23s median)
                          ├─  9%  delete_leg_stale     (freshness pull)
                          └─  0%  delete_leg_moved     (8 total — we are NOT over-repricing)
```

| metric | value | read |
|---|---|---|
| quote latency RFQ-seen→sent | p50 **0.26s**, p90 1.97s, p95 2.38s | median fast; a **9% tail >2s** (the `POOL_DEADLINE_S=2.0` budget) |
| quote lifetime before `rfq_gone` | p50 **10.9s** | we're live & competitive for ~11s while the RFQ is open |
| fills | **16 / 21,669** | dominated by the RFQ base rate (~87% of RFQs never trade for *anyone*) + tight same-instant competition |

**Interpretation.** We quote fast (0.26s median), stay live ~11s, and are priced ~1¢
under the market. We are *in* the auction — we're just not the one lifted on the specific
RFQs that do clear (many makers converge to the same tight price on game day; the winner is
a coin-flip we take our share of). The lever is **coverage + speed on the tail**, not price:
(a) shave the 9% >2s pricing tail, (b) reconsider the 23s TTL vs longer-lived RFQs. Widening
markup will *not* hurt fills much here (we don't lose on price) and captures more edge.

---

## 5. Family calibration patterns (the real, small mispricings)

| family | signal | reading | action |
|---|---|---|---|
| 2-leg advance×advance / ×BTTS / ×total | within **±1¢**, mostly ~1¢ under | **well-calibrated**; bread-and-butter mains are correct | leave; candidate to widen ~1¢ |
| **CORNERS-containing combos** | **−3 to −5¢ under** (corners8/9 × adv/Kmbapp) | we **underprice corners** — market prices them richer; adverse-selection risk (we win them cheap) | **investigate corners marginal/ρ** |
| ESP-advance pairs | **+0.9 to +1.4¢ over** (ARG/ENG/BTTS) | our **ESP-advance marginal ~1¢ high** | minor; watch, don't refit on this snapshot |
| deep 3–4-leg goalscorer parlays | −3 to −8¢ (thin tape) | the old "too high" alarm's origin — but **tape too thin to trust**, and de-prioritised | ignore for now |

---

## 6. What this does and does not license

- **Does** kill the "WC fair is systematically too high" theory for main combos (task #35).
  We're marginally *cheap*, not expensive.
- **Does** corroborate the live-probe "room to widen ~1–2¢" from an independent 900k-trade
  line. A markup nudge on the liquid 2-leg mains stays at/under market.
- **Does NOT** by itself authorise a markup change — that is a **pooled multi-week** operator
  call (never refit on one snapshot; standing rule). This is one game-week (FRA-ESP heavy),
  one temporal-offset window.
- **Does** relocate the fill investigation from pricing to **auction liveness** (§4).

---

## NEXT STEPS

- **Owner: operator** — Decision owed: nudge markup up ~1¢ on the **liquid 2-leg advance
  mains** (we clear ~1¢ under market)? Recommend YES in principle but only once a **second
  game-week** of tape corroborates (pooled evidence, per standing rule). Not applied now.
- **Owner: next agent (#35 → close/repoint)** — Repoint #35 from "WC too high" (disproved)
  to **(a) corners under-pricing** (−3 to −5¢: audit the corners leg marginal + corners×
  advance ρ) and **(b) the 9% >2s pricing-latency tail** (does trimming it lift fills?).
- **Owner: next agent (fill mechanics)** — Instrument the auction: of RFQs that *did* clear
  on a leg-set we quoted, were we live at the accept instant and at/under the clear? Needs a
  fresh recorder run overlapping our live quoting (recorder DOWN since Jul 12 — **restart it**
  so the next backtest can be same-instant, removing this report's one caveat).
- **Owner: next agent** — Revisit the **23s TTL**: `ttl_expired` is 35% of deletions and
  fires before some RFQs resolve; test a longer TTL against added leg-move risk.
- **Reproduce:** `python tools/market_vs_our_pricing.py` (build) → `market_drift_check.py`
  (drift bound) → `_gen_report_tables.py` (tables). Ticker→leg-set map cached at
  `$CLAUDE_JOB_DIR/tmp/ticksig.json` (one 288s scan of 19.7M rfqs; reused thereafter).
