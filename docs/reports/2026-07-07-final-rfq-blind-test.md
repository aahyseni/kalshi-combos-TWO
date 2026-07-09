# Final RFQ blind test — 28 real combos, agent-priced

**Date:** ~2026-07-07 (evening; see operator memory `project_kct_rfq_test`) ·
**Status:** DONE · **Method:** blind (agents ran the engine; the lead did NOT
price, to avoid biasing).

## Design

- Pulled 28 real traded soccer/MLB combos off the RFQ tape spanning varied legs.
- Distributed to independent agents labelled A–F + C-numbered; each agent **ran
  the shipped engine itself** (nothing built for them) to produce our fair, blind
  to the maker's actual clearing.
- Compared our fair vs the actual maker clearing (their **quote**, not their fair
  — same caveat as every backtest here).

## Findings

- **Median |our_fair − clearing| ≈ 2.17¢.** Negative bias = we sit just below
  clearing = consistent with maker markup on calibrated pairs.
- **Backbone validated:** on calibrated pairs we match the makers; the pricing
  spine (marginals → relationship classify → copula/DC joint → quote) holds up on
  real combos.
- **Oracle check: agents matched the reference 28/28** — the engine is
  deterministic and the agents drove it correctly.
- **Flagged fix-list** (pairs where we diverged, later calibrated in the
  2026-07-07 calibration report): **`corners|corners_team`** and
  **`btts|first_half_total`**. These were the trigger for "lets fix last night's
  flagged combos."

## What this did and didn't prove

- DID: the engine prices real, varied combos sensibly and competitively vs the
  market's ask; the blind-agent harness works.
- DID NOT: prove accuracy (clearing = quote, not fair; and no settlement here).
  Accuracy is the job of the settlement backtest (2026-07-08).

## NEXT STEPS

- **Runs next:** superseded by the full 1,480-combo backtest and the settlement
  P&L backtest.
- **Owner (operator):** the flagged pairs were calibrated — confirm the fixes
  hold on the next blind re-test (the planned soccer gate before other sports).
- Full per-combo A–F tables live in the pre-compaction transcript
  (`24844262-*.jsonl`) if line-level detail is needed.
