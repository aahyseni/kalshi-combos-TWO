# 2026-07-20 — FINAL World Cup campaign realized-P&L statement (exchange-ledger exact)

The owed end-of-campaign statement, produced WITHOUT a relight (operator
2026-07-20: no sports going, bot stays down until the sport switch): the local
fill store (`data/combomaker-prod-live-wc.sqlite3`, mode=ro) reconciled against
the exchange's OWN settlement ledger (`GET /portfolio/settlements`) and balance.
Tool: `tools/diagnostics/campaign_pnl_statement.py` (read-only, reusable for
every future campaign). All times ET.

## Headline

**Every filled market has an exchange settlement row — the book is FULLY
settled, $0.00 in open positions.**

| | |
|---|---|
| WC store lifetime (Tue 7/14 → Sun 7/19) | 106 fills across 56 combo markets |
| Premium paid (incl. fill fees) | $1,428.05 |
| Settlement revenue (exchange ledger) | $1,803.46 |
| Settlement fees (exchange ledger) | $5.92 |
| **NET REALIZED** | **+$369.50** |
| Winning / losing fills | 100 / 6 |
| Exchange equity now | **$2,384.77** (all cash, zero positions) |

## By settle day (exchange ledger, exact)

| settle day (ET) | fills | revenue | premium | fees | realized |
|---|---|---|---|---|---|
| Tue 07/14 | 10 | $269.95 | $193.71 | $0.00 | +$76.24 |
| Wed 07/15 | 9 | $195.14 | $220.64 | $0.00 | −$25.50 |
| Thu 07/16 | 1 | $5.88 | $4.46 | $0.00 | +$1.42 |
| Sat 07/18 | 35 | $503.41 | $318.74 | $0.37 | +$184.29 |
| Sun 07/19 | 51 | $829.08 | $690.50 | $5.54 | +$133.04 |
| **TOTAL** | 106 | **$1,803.46** | **$1,428.05** | **$5.92** | **+$369.50** |

## By fill day (premium-weighted attribution of each market's realized)

| fill day (ET) | fills | premium | realized share |
|---|---|---|---|
| Tue 07/14 | 17 | $335.70 | +$40.65 |
| Wed 07/15 | 3 | $83.10 | +$11.52 |
| Thu 07/16 | 12 | $285.94 | +$102.55 |
| Fri 07/17 | 8 | $33.44 | +$27.10 |
| Sat 07/18 | 28 | $333.36 | +$141.37 |
| Sun 07/19 | 38 | $356.51 | +$52.50 |

**The weekend campaign (Fri eve → Sun final) = exactly the 74 fills / ~$723
premium the handoff tracked**, realized ≈ **+$221** by this attribution. The
in-session scenario-engine figure (+$234.09) was a combo-level prediction over
final-dependent positions; the ledger attribution differs by allocation
methodology (markets shared across fill days, fees), not by outcome — the
settle-day table above is the exact truth. Attribution-view total (+$375.70)
differs from the exact total (+$369.50) only by the premium-share rounding of
markets with fills on multiple days; the exact figure governs.

## Notes

- Sunday's realized (+$133.04 across 51 Sunday-settling fills) includes
  early-week champion/final-leg combos, not only weekend fills — the two cuts
  above exist precisely so no bucket is inferred (enumerate-buckets rule).
- $5.92 lifetime settlement fees — the taker-fee execution variant left its
  trace here; maker fills themselves remain $0-fee.
- ROI on premium at risk, store lifetime: +$369.50 / $1,428.05 ≈ **25.9%**
  over 6 days. Weekend alone: ≈ +$221 / $723 ≈ 30.6%.
- Wed 7/15 (−$25.50) remains the only losing settle day (build-week, pre
  toxic-flow-filter maturity).
- The verified-false-positive kill's "give-back $430.69" vs these numbers: the
  real lifetime loss content was 6 losing fills; the receivables shield built
  today (`2026-07-20-inplay-breaker-exemption-and-settlement-receivables.md`)
  makes that class of kill structurally impossible to repeat.

## NEXT STEPS

- Campaign is CLOSED on the books — no settlement receivables outstanding,
  equity is all cash ($2,384.77).
- The statement tool is standing (`campaign_pnl_statement.py`) — run it at the
  end of every future campaign / weekly for MLB-WNBA nightly slates.
- Owed elsewhere: rho corpus backtest verdict (running, slow — days at current
  pace; low urgency, the pair is soccer-only and nothing quotes until the
  sport switch); tape recorder restart.
- Owner: bot session. Operator decisions owed: none.
