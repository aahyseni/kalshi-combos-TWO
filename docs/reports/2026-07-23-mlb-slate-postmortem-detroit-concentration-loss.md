# 2026-07-23 — MLB slate post-mortem: −$210 on one-way Detroit concentration

**Bottom line.** First live MLB money slate (3 games) closed **−$210.35 realized**.
The two games with variance PROFITED; one game with no variance — a concentrated
one-directional bet against a *likely* outcome — erased it. This is the empirical
proof that the risk model's gaps cost real money: caps throttled *quoting* but never
bounded the *settlement outcome* of a concentrated position. Bot was stopped on the
operator's call before settlement; loss materialized at game-end.

## The number (per-game realized P&L, we held NO on every combo)

```
  KC@DET    −$259.47    5W  8L  1scalar   ← one-directional, NO variance: 8 combos LOST TOGETHER
  TB@TOR    +$18.73     6W  0L  0scalar   ← variance → clean profit
  AZ@STL    +$30.39     8W  0L  3scalar   ← variance → profit EVEN AFTER 3 Marte scalars
  ──────────────────────────────────────
  TOTAL     −$210.35
```

- **Variance games: +$49.12 combined.** Detroit alone: **−$259.47.** One game flipped
  a profitable slate into a loss.
- **−$210 exceeds our own daily-loss anchor (~$143 = 6% of bank) by 1.5×** — on a
  *single likely outcome*, not a tail.
- **The "no variance" is literal:** KC@DET had **8 losses all moving the same way**
  (Detroit won low-scoring → every `yes Detroit, no Over` combo hit at once); the two
  profitable games had **zero** losses (independent props resolved one-by-one).

## What actually settled

- **KC@DET — low-scoring Detroit win = the exact adverse outcome** for our book. We had
  accumulated ~$349 of short "Detroit-wins-a-low-scoring-game" (82%→52% of book across
  the day). A favorite winning a pitchers' duel is a ~1-in-3 outcome — common, not tail.
- **AZ@STL — Ketel Marte was SCRATCHED** (DNP). His props settled `result=scalar`
  (HIT 0.69 / HRR 0.55 / TB 0.46 — frozen at market prob). The same-player combos settled
  at the **independence product** of leg scalars (e.g. `NO(hit)×YES(hrr) = 0.31×0.55 =
  0.17` — exact). A taker who knew the scratch bought the YES cheap; we priced as if Marte
  plays. **But AZ@STL was still +$30 net** — the scalar pickoff was a real vulnerability,
  NOT the day's main damage.
- **TB@TOR — clean +$18.73**, no scalars.

## Root cause — each dollar to a gap

1. **Concentration (the killer).** No hard **directional net-bound**: the directional cap
   throttles *new* quotes on the then-current book, but stacked resting quotes + relight
   headroom let committed short-Detroit grow to the cap and *settle* one-way. Bounded the
   adds, never bounded the outcome.
2. **No hedge.** Offsetting flow (KC-win, overs) was only ~$77 vs $349 the other way. The
   inventory **skew/rebate never paid up** to win the balancing auctions ([[feedback_balance_via_maker_quoting]],
   [[feedback_pbook_diversity_via_pricing]]). Hedging = winning offsetting flow, not taking.
3. **Variance.** 3 games, once concentrated, ≈ 1 effective bet → P(book profit) low because
   one game's direction dominated. The MC *computes* P(book); nothing *steers* toward it.
4. **Scalar/DNP pickoff** (secondary, [[project_kct_player_level_risk_gap]] + the AS4 hole):
   same-player combos priced as if the player plays; a scratch scalars every leg at once.
5. **Per-combo cap breach:** the $149.24 Marte HRR combo > the 5% cap ($122.58), via
   mass-acceptance re-hits of one structure.

Gaps 1, 4b(per-combo), and the relight ratchet are ONE mechanism: **enforced caps bound
quote-time projections, not accumulated net when several resting quotes on one structure
fill together.** Settlement is the only ruler ([[feedback_no_refit_on_pnl]] — this is a
STRUCTURAL read, not a P&L refit).

## Scalar/DNP fix (operator design — validated)

**Scalar-floor pricing** (no lineup feed, no hard block): for any prop combo,
`YES price = MAX(normal_fair, scalar_floor) + markup`, where `scalar_floor = ∏(leg book
probs, per side)` — computable from prices we already read. Ensures profit/no-loss under
both a normal-NO and a scalar settlement; we only lose on a genuine YES (the real bet). A
scratch-informed taker can't buy below the floor → self-selects out the pickoff, still
fills normal +EV props. Compose later with a **lineup-confirmation gate** (unconfirmed →
no-quote) + a **market-signal late-scratch detector** + **scalar-aware settlement booking**
(AS4). Worked example: had we sold YES at 0.17 (the scalar value) instead of 0.112, the
Marte combo was break-even on the scratch; at 0.19 it profits.

## Build spec — risk-model rebuild (off-line, parity-checked, never hot-patched)

**Priority 1 — concentration & hedging (this is what cost the $210):**
- Hard **net-position bounds** (not just quote-time throttles): per-combo, per-game-per-
  direction, and per-**entity** (player/team, ALL leg families — the directional axis is
  ML-only today).
- **P(book-profit)-aware sizing**: refuse one-way accumulation on a likely outcome; wire the
  MC's P(book) into a steering signal, not just a passive metric.
- **Active hedging**: the skew must PAY UP (rebate) to win KC-win/over-style offsetting flow
  when the book is lopsided — feed it the P0-9 mutex-aware per-game direction (the queued
  skew-mutex fix).
- **Relight concentration-neutrality**: freed headroom (post cancel-all) must not immediately
  refill the already-concentrated side.

**Priority 2 — scalar/DNP:** scalar-floor pricing → lineup gate → late-scratch detector →
AS4 settlement booking.

## What went RIGHT (keep)

- The two variance games profited; the maker edge is real when the book is diversified.
- The **correlation pricing math was correct** (same-player HRR audit was clean — the loss
  was concentration + DNP information, not a copula hole).
- Operator halted on the concentration/pickoff read before it compounded.

## NEXT STEPS

- **Me:** clean to-the-cent reconciliation vs `/portfolio/settlements` once the ledger
  posts (this −$210.35 is fill-cost-vs-settlement; confirm against the exchange ledger).
- **Build (owner: me, operator prioritizes):** Priority-1 concentration/hedge rebuild FIRST
  (it cost the money), then Priority-2 scalar defense. Each off-line, tested, parity-checked.
- **Operator decision:** resume posture — stay flat until the Priority-1 hard net-bounds +
  hedging exist, or resume with a per-game one-way directional net-cap as an interim guard.
- **Standing:** bot remains DOWN (KILL set), flat, until the rebuild or an explicit relight.
