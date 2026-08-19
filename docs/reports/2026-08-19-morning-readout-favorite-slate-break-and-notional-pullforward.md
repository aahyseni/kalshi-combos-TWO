# 2026-08-19 morning readout — 8/18 evening slate broke against favorites; gross-notional fix proposed for daylight ship

All times ET. Whole-book equity **$4,158.92** (cash $3,146.18 + positions
$1,012.74; shard 0 $2,238.55/$89.35, shard 1 $907.62/$923.39), read live
2026-08-19 ~05:30 via the per-shard `portfolio_value` union.

## P&L truth

| window | realized (group-corrected) | note |
|---|---|---|
| 8/17 (final) | ≈ −$611 | KILL day; loss halts disarmed by operator that night |
| 8/18 (final) | **−$156.51** (120 groups) | was +$245.83 midday — the evening slate gave it back |
| 8/19 so far | −$121.96 (17 groups) | overnight esports/late settles |
| equity anchor | $5,124 (8/17 AM) → $4,158.92 | −$965 over the three days (ledger −$890 + open marks/fees) |

Operationally the machine had its **second consecutive zero-incident night**:
150 accepts / 146 executed with every unit of the gap explained (1
exchange-expired confirm, 2 gross-notional-wall declines, 1 in flight at
snapshot), **zero** insufficient-balance 400s, zero watchdog events, sends
~348k cumulative (+3,675/10min overnight rate), p_book 0.497 at last read.

## What actually happened: ONE slate, not decay

Favorite-band (entry ≥65¢) settles since the 8/18 08:13 restart: **54 groups,
31 wins (57%) vs ~74% implied, −$142.45.** Unclustered that's z ≈ −2.8 —
but every losing group traces to game codes on **one slate (26AUG18)**:
LAD@COL (16 leg appearances in losers), MIA@PHI (14), NYY@BAL (11), AZ@BOS
(11), SF@CLE (6), STL@CIN (6), SD@NYM (5), SEA@MIL (5). Eight games where
the favorite side lost, one night. Parlay groups share those games, so the
effective sample is ~one slate draw, not 54 independents.

This is exactly the left tail the composition model describes: a
favorite-heavy book has higher P(book) and a *thinner but correlated* tail —
when it goes, it goes together. **No refit on a P&L window** (standing rule).
Instead, pre-registering the check:

> **Pre-registered favorite-band calibration check** (weekly, pooled,
> game-clustered): pooled favorite-band win rate vs entry-implied over ≥3
> weeks with game-level cluster correction. Trigger for a *model review*
> (not an automatic change): pooled shortfall >2σ clustered. First read
> ~2026-09-01, aligning with the 9/1 dashboard.

## Razor (ML-parlay +0.3¢) day-2 grade

20 settled groups, 11 wins, **−$81.66** — same contaminated slate
(cross-game ML on 26AUG18 games). Sample far too small for a verdict either
way; the 8/16 research warning (below ~+0.5¢ margin there is no
adverse-selection insurance) stays open. Verdict needs ≥2 weeks pooled,
same cadence as the favorite-band check.

## 8/18 record for the operator's cap verdict (they watched to decide 1% vs 2%)

153 unique combos filled, $1,966.28 premium intake. Sizes: median **$10.56**,
p90 $27.10, max **$42.95**. Zero fills above the 2%-of-equity line; two
marginally above 1%-of-*current*-equity ($41.59) — both under 1% of equity
at fill time. No $74-style outliers recurred after the revert.

## TOP ITEM — gross-settlement-notional wall: pull the ratified fix forward

Last night the wall declined **2 won auctions** at `decline_risk_limit`:
measured gross notional 133,559,100cc vs limit 3× bankroll = 133,444,386cc —
**missing by $11.55, 0.09%**. The measured notional is inflated ~3.6× by the
per-game double-count (each game's legs counted once per combo touching it),
the defect the operator **ratified for fix + re-derive on 2026-08-12**
(recipe in `2026-08-12-deferred-updates-ledger-until-2026-08-31.md`) and
parked under the engine freeze. It is now costing won auctions at the margin
(~2/night observed) and the cost grows with book size — the opposite of the
freeze's intent.

**Proposal (needs operator blessing to break freeze for this one item):**
ship in daylight today — (1) fix the per-game aggregation so a game's
settlement notional counts once across the book, (2) re-derive the 3×
multiple against the corrected measure so the effective wall tightness is
unchanged on day one (no risk loosening by side effect), (3) vitals gate 8/8
+ pre-ship tier, before/after quotes-per-min per the throughput rule. Blast
radius: the risk wall only; pricing untouched.

## Shard watch

Shard 1 holds $907.62 cash against ~64% of flow. It survived the overnight
(0 × 400s) but is the first thing to break on a heavy evening slate. Option:
top up toward flow share from shard 0's $2,238 idle. Cash gate remains
disarmed, so exhaustion presents as 400s, not throttling.

## NEXT STEPS

- **Operator decisions owed:** (1) bless the gross-notional daylight ship
  (freeze exception, ratified 8/12); (2) 1% vs 2% per-combo verdict — 8/18
  record above; (3) friendlies allowlist (5,676 open RFQs); (4) shard-1
  top-up toward flow share.
- **Me:** run the pre-registered favorite-band + razor pooled checks weekly
  (first full read ~9/1); keep the overnight anomaly watcher running; WAL
  checkpoint at next restart (standing).
- **Standing:** no engine changes without the blessing above; loss halts
  remain disarmed per operator 8/18 ruling (KILL telemetry anchor split is a
  9/1 item).
