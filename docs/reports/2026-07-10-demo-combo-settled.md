# Demo combo SETTLED — combo_no_pays_complement CONFIRMED to the cent; sell-only fills UN-GATED

**Date:** 2026-07-10 ~16:20 UTC · **The last pricing convention, promoted from
real exchange ledger.**

## What happened

- `KXMVECROSSCATEGORY-S2026C1138DA69BC-7ADA8E5486D` (LAA win Jul 9 AND BOS win
  Jul 10): LAA **lost** → **EARLY-NO fired** — the combo finalized `result=no`
  the moment one leg failed, without waiting for tonight's BOS game. Exactly
  the mechanism predicted in the cash-out mechanics report.
- Our maker position: LONG NO 1.00 contract, cost $0.50 (from the 2026-07-09
  round-trip). **Payout: exactly $1.00** — demo balance 1,082.62 → **1,083.62**,
  zero settlement fee. NO paid **1 − V with V = 0, to the cent**.
- Realized: **+$0.50 on $0.50** — and more importantly, the convention is now
  exchange-ledger ground truth, not a doc assumption.

## Promotion

`tests/fixtures/ground_truth/conventions.json`:
`combo_no_pays_complement: null → true` (with provenance note). Full suite
**1095 passed / 0 failed** on the promoted fixture.

## What this un-gates

`rfq/lifecycle.py` was declining EVERY NO-side confirm
(`DECLINE_CONVENTION_UNKNOWN`) while the convention was null — the sell-only
book could quote but never fill. **That gate is now open**: the sell-only
parlay-seller book is fill-capable on demo. Prod quoting remains triple-gated
(whitelist, prod_limits_configured, --confirm-live) — unchanged, deliberate.

## NEXT STEPS
- Phase 6 paper/shadow (real engine, live RFQs, chronological) is now the
  natural next step — every pricing convention verified, every gate but the
  deliberate prod ones open.
- Demo quote-mode session (whitelist configured on demo) can now produce REAL
  fills end-to-end: quote → accept → confirm → fill → settle → reconcile
  (HALT_RECONCILIATION_MISMATCH armed) — the full-loop verification.
