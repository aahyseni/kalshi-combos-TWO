# wire-live — adversarial-judge fixes on the settlement/reconcile wiring

**Date:** 2026-07-13
**Branch:** `wire-live` (worktree; not merged/pushed)
**Scope:** Fix the adversarial-judge findings on the Stage A settlement ledger +
exchange-first reconcile wiring. Suite GREEN, mypy strict + ruff clean on every
touched file.

## What the judge found and what changed

| # | Sev | File | Defect | Fix | Test |
|---|-----|------|--------|-----|------|
| 1 | high | `rfq/lifecycle.py` | To-the-cent reconcile HALTs spuriously on a legitimate **fractional-contract SCALAR** settlement: predicted credit carries sub-cent precision (0.90 ct × $0.57 = 51.30¢ = 5130 cc) that the integer-cent exchange `revenue` (51¢/52¢) can never equal → false `HALT_RECONCILIATION_MISMATCH`. | Reconcile to the exchange's **whole-cent grid**: HALT only when `abs(predicted_cc − revenue_cc) ≥ CC_PER_CENT` (a residual ≥ 1¢). Binary V∈{0,1} + whole-contract scalars stay EXACT (residual 0); a real sign/value/convention error still shifts ≥ 1¢ → still HALTs (defense #3 intact). Same fix mirrored in `FakeLifecycle` (`tests/test_settlement.py`). | `test_full_reconcile_fractional_contract_scalar_does_not_halt` (both 51¢ + 52¢ bookings), `test_full_reconcile_fractional_scalar_still_halts_on_real_mismatch`, `test_fractional_contract_scalar_does_not_false_halt` + `_still_halts_on_real_mismatch` (handler path) |
| 2 | medium | `risk/exposure.py`, `risk/settlement.py` | A settled position is **never removed** from `ExposureBook` → settled exposure accumulates forever (inflates the ENFORCED game/slate/gross/CVaR caps + daily-P&L mark), and a re-quote+re-fill of the same ticker makes the reconcile re-sum the OLD settled position against the NEW settlement's revenue → false HALT. | Added `ExposureBook.remove_position(position_id)` (idempotent). `SettlementHandler._reconcile_positions` now prunes each booked position **after** `apply_settlement` succeeds + the id is marked reconciled. Settlements are built up-front (before the prune loop) so the by-contract-weight fee split still sees the whole ticker (fees sum to the cent). | `test_settled_position_removed_from_exposure_book`, `test_requote_same_ticker_after_settlement_does_not_false_halt`, `test_multi_position_fee_split_still_exact_after_prune`; updated `test_double_poll_no_ops` to assert the (stronger) pruned-path idempotency — booked exactly once, still no double-book |
| 3 | low | `rfq/lifecycle.py` | The **trade fee** charged at fill (`_fill_fee_cc`, recorded to the fill ledger) never enters `realized_pnl_cc` — the ENFORCED daily-loss cap's realized figure understates costs by the trade fee on a nonzero-fee series (only the settlement fee is netted). | At fill, feed a KNOWN nonzero trade fee into `record_realized_pnl(-fee)`. A `None`/UNKNOWN fee is NOT booked as a convenient 0 (defense #2) — the live balance poll remains the backstop. $0 today for our quadratic maker fills, so no behaviour change now. | `test_nonzero_trade_fee_enters_realized_pnl_at_fill` (real `FeeModel`, QUADRATIC_WITH_MAKER_FEES), `test_zero_fee_fill_leaves_realized_pnl_untouched` |
| 4 | info | `ops/quote_app.py` | Enforcing the caps means a fresh quote/paper start no-quotes until the first balance poll lands (`SKIP_BANKROLL_UNAVAILABLE`), and goes dark if the balance endpoint is unreachable. | **No code change** (intended fail-closed, self-healing — the `_balance_loop` polls immediately, window ≈ one round-trip). Documented here so a quiet paper start is not mistaken for a bug. | (covered by existing `test_stale_bankroll_no_quotes_but_never_halts`) |

## Correctness notes

- **Fix #1 tolerance is tight, not loose.** `expected_revenue_cc` from the
  exchange is always a whole-cent multiple (int cents ×100), and the predicted
  credit differs from the true value only by the sub-cent flooring in
  `contracts·per_ct//100`. So a legitimate fractional-contract scalar yields a
  residual **strictly < 1¢**, while any genuine model error yields **≥ 1¢**. The
  `>= CC_PER_CENT` guard is robust to whether the exchange rounds or floors the
  half-cent (both land < 1¢ away). Defense #3 (predicted-vs-exchange mismatch
  HALTs, never logs) is preserved for every real mismatch.
- **Fix #2 fee-split ordering.** `_fee_share_cc` splits the exchange fee by
  contract weight over the whole ticker. Pruning mid-loop would shrink the
  denominator, so all `Settlement` objects are now built **before** the
  book+remove loop while the book is intact — the fee split stays exact to the
  cent (proven by `test_multi_position_fee_split_still_exact_after_prune`).
- **No test weakened.** `test_double_poll_no_ops` still asserts the core
  invariant (booked exactly once, realized P&L / settled-count / realized-deltas
  unchanged on re-poll); only the result *shape* is updated to the correct
  pruned-path behaviour (a re-poll of a settled+pruned ticker is ignored, `[]`).

## Verification

- Full suite: **1693 passed, 3 deselected, 0 failed** (`uv run pytest -q`).
- `uv run mypy src/combomaker/rfq/lifecycle.py src/combomaker/risk/exposure.py
  src/combomaker/risk/settlement.py` → clean. (The only repo-wide mypy errors are
  pre-existing in the unrelated experimental `pricing/ising_amm.py` spike — not
  touched, confirmed present on the base commit.)
- `uv run ruff check` on all touched files → clean.
- Money stays integer centi-cents throughout; no binary-float money introduced.
  Fail-closed preserved everywhere (UNKNOWN value/convention still HALTs; a
  `None` trade fee is never booked as 0). Demo/paper not bricked (a fresh start
  with no positions is a pure no-op through the new prune/reconcile paths).

## NEXT STEPS

- **Operator:** review + merge `wire-live` (this worktree is intentionally NOT
  pushed/merged/removed per the task brief).
- **Follow-on (unchanged from Stage A/audit):** the settlement/reconcile wiring
  is now correct on fractional-contract scalars and stops accumulating settled
  exposure; the remaining go-live runway items (flip `caps_shadow_mode`, wire the
  unsampled breakers + the maintenance-tick BookRiskSnapshot loop, the pooled
  multi-week markup decision) are owned by the operator and untouched here.
