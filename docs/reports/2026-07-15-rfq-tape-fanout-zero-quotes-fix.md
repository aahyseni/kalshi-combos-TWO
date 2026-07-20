# 2026-07-15 — ROOT CAUSE + FIX: rfqs-tape fanout inflated rehydrated contracts → 0 quotes

**Status: FIXED, verified live (0 → 228+ quotes, steady). Suite 1789/0.**

## Symptom

After the #33 exposure-book rehydration + risk-engine upgrade went live on the WC
book, the bot issued **0 quotes** despite heavy ENGARG pregame RFQ flow. Every RFQ
declined with `skip_mass_acceptance_breach` (+ a cascade of `skip_utilization_backstop`,
`skip_game_loss_cap`, `skip_directional_cap`, `skip_slate_cap`, `skip_portfolio_cvar`,
`skip_portfolio_ruin`). The binding detail, pulled from the live decisions table:

```
market KXWCADVANCE-26JUL15ENGARG-ARG delta -259302.4 > 300.0
market KXWCGOAL-26JUL15ENGARG-ENGHKANE9-1 delta -3176.6 > 300.0
market KXWCTCORNERS-26JUL15ENGARG-ARG4 delta -1617.6 > 300.0
```

A −259,302-contract delta on ARG-advance when our real ARG-advance exposure was a
few hundred contracts. The value was **nearly identical across consecutive RFQs**,
which meant it was the **committed (rehydrated) book's** delta, not the candidate's.

## Root cause — a SQL fan-out in `Store.held_positions`

`held_positions` (the #33 rehydration source) fetched summed contracts AND the combo
legs in one query:

```sql
SELECT f.combo_ticker, f.our_side, SUM(f.contracts_centi), SUM(f.contracts_centi*f.price_cc),
       MAX(r.legs_json), MAX(r.collection_ticker)
FROM fills f JOIN rfqs r ON r.market_ticker = f.combo_ticker      -- <-- the bug
GROUP BY f.combo_ticker, f.our_side
```

The `rfqs` table is the **recorded RFQ tape** — **1.6M rows**, one per re-quote, with
the SAME `market_ticker` (combo) appearing thousands of times (fanout up to **12,456×**
for the most-quoted combo). The `JOIN` multiplied each fill row by that fanout factor
**before** the `SUM`, so `SUM(contracts_centi)` returned `real_contracts × fanout`.

| held combo | real ctr | fanout | inflated ctr |
|---|---|---|---|
| `KXMVESPORTSMULTIGAMEEXTENDED-S202604C77A…` | 37 | 12,456 | **464,235** |
| `KXMVESPORTSMULTIGAMEEXTENDED-S2026F4E964…` | 35 | 840 | 29,165 |
| `KXMVECROSSCATEGORY-S2026F263E3B9761…` | 21 | 1,101 | 23,121 |
| … 14 more … | | | |
| **TOTAL (17 held combos)** | **435** | | **552,393** |

`entry_price_cc = SUM(ctr*price)//SUM(ctr)` was fanout-*safe* (numerator and
denominator scaled together), so the bug was invisible to the P&L axis — but
`contracts_centi` was **not**, and it feeds:

- `analytic_leg_deltas` → per-market mass-acceptance delta → `464,235 × ∏(other
  marginals ≈0.5) ≈ 232k` → the `−259,302` on the shared ARG-advance leg.
- `OpenPosition.max_loss_cc = contracts × entry_price // 100` → inflated gross
  notional, game-loss, slate-loss caps too.

So **one fan-out bug** produced the entire cascade of skip reasons and blocked all
quoting. It only manifested after #33 (before rehydration the book started empty).

## Fix

`src/combomaker/ops/persistence.py` — pre-aggregate fills and de-dup the rfqs legs
lookup into **1-row-per-combo derived tables**, so the join is strictly 1:1:

```sql
SELECT a.combo_ticker, a.our_side, a.ctr, a.loss_num, r.legs_json, r.collection_ticker
FROM (SELECT combo_ticker, our_side, SUM(contracts_centi) ctr,
             SUM(contracts_centi*price_cc) loss_num
      FROM fills WHERE combo_ticker IN (?…) GROUP BY combo_ticker, our_side) a
LEFT JOIN (SELECT market_ticker, MAX(legs_json) legs_json, MAX(collection_ticker) collection_ticker
           FROM rfqs WHERE market_ticker IN (?…) GROUP BY market_ticker) r
  ON r.market_ticker = a.combo_ticker
```

Also added `CREATE INDEX IF NOT EXISTS idx_rfqs_market_ticker ON rfqs (market_ticker)`
(the de-dup subquery scanned 1.6M rows unindexed → 3.2s; the index is built once on
next startup). `LEFT JOIN` + the existing `if not legs_json: continue` guard means a
held combo with no tape row is surfaced by the caller, never modeled from a guess.

## Verification (prototype → parity → live, per hard rule 8)

1. **Parity on the live DB copy** (read-only): buggy total **552,393** ctr → fixed
   **435** ctr. Worst combo `…S202604C77A`: `464,235 → 37`.
2. **Real aggregate delta with the fixed query** (proxy marginals 0.5, worst case):
   `KXWCADVANCE-26JUL15ENGARG-ARG` delta **−259,302 → 47.7**; every leg market now
   under the 300 cap (largest 65.5).
3. **Regression test** `test_held_positions_not_inflated_by_rfq_tape_fanout`: combo
   re-quoted 6× + filled once for 5000 ctr → asserts contracts read 5000, not 30,000.
   (The pre-existing test recorded each RFQ once, fanout=1, so could not catch it.)
4. **Full suite 1789/0.**
5. **Live relaunch** (`live_wc9.log`, prod, `--confirm-live`): `exposure_rehydrated
   games=[26JUL15ENGARG] positions=6` with correct contracts; **0 → 228 quotes in
   ~3.5 min**, steady 12–26/10s, both sides (ARG 130 / ENG 57), 20 resting at
   `max_open_quotes`. Directional deltas now sane (`|delta| 261–466 ct`).

## Post-fix state — remaining declines are legitimate (or the next levers)

Sole-reason declines (the ONLY blocker on that RFQ = true fill cost), ~6 min sample:

| sole blocker | n | verdict |
|---|---|---|
| `skip_directional_cap` | 2280 | ENGARG one-sided concentration; **balancing lever** |
| `skip_max_open_quotes` | 719 | saturating 20 resting quotes — a throughput cap |
| `skip_portfolio_cvar` | 241 | portfolio MC **fail-closed on UNKNOWN marginal** (never a real CVaR number: 268/268) |
| `skip_price_deadline` | 235 | pricing throughput (~600ms/price, known #1) |
| `skip_classifier_unknown` | 52 | joint-layer gap (known) |

These are the concentrated-ENGARG book behaving correctly (we hold 6 positions in the
one game with flow) plus known throughput ceilings — NOT the fan-out bug.

## NEXT STEPS

1. **Owner: agent.** Directional-cap mutex-awareness — holding NO on ARG-advance and
   NO on ENG-advance (mutually exclusive) should *offset*, not add, in the directional
   cap (as Stage B already does for the per-game loss cap). This is the #1 lever for
   the operator's "balance the book" goal. Prototype → parity → suite.
2. **Owner: agent.** Portfolio-CVaR usable-snapshot — the structural MC fail-closes on
   every RFQ because some rehydrated leg's marginal is unavailable (same class as the
   committed-marginal fix, but on the book-risk path). Make the snapshot degrade
   gracefully so the MC actually computes and can *reward* risk-reducing balancing
   trades. Costs ~241 sole-reason fills now.
3. **Owner: operator decision.** `max_open_quotes` (20) is 719 sole declines — raise it
   now the delta/loss caps are sane, or leave as an exposure bound?
4. **Owner: agent (known).** Pricing throughput (`skip_price_deadline`) — cache the
   per-game structural fit / ProcessPool-offload the pricer.
