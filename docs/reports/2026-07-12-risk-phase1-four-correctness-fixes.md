# Risk engine PHASE 1 — four correctness fixes (equity denominator, fees, scalar settlement, rename)

**Date:** 2026-07-12. **Branch:** `risk-phase1`. **Scope:** RISK_BUILD_PLAN
Phase 1 ("correct the money"). One coherent commit. Prototype-in-test →
port → parity-check (CLAUDE.md rule 8). Baseline suite **1325/0** →
**1353/0** (+28, 0 failed). mypy strict + ruff clean on every touched file.

Files changed:

| file | change |
|------|--------|
| `src/combomaker/risk/balance.py` | FIX 1 (equity-aware denominator), FIX 2 (fee booking), FIX 3 (scalar settlement) |
| `src/combomaker/risk/exposure.py` | FIX 4 (`payout_obligation` → `gross_settlement_notional`) |
| `src/combomaker/risk/limits.py` | FIX 4 (rename in the seam comment; verified NO cap consumes the axis) |
| `tests/test_balance.py` | FIX 1/2/3 tests (+25) + migrated binary settlements to `Settlement.binary` |
| `tests/test_exposure.py` | FIX 4 rename in tests |

## FIX 1 — equity-aware bankroll denominator

`get_balance` now parses BOTH `balance` (available cash) AND `portfolio_value`
(position mark), each CENTS → cc via explicit ×100. Kept separate
(`available_cash_cc` / `portfolio_value_cc`), derived
`exchange_equity_cc = cash + portfolio_value`, and the caps' denominator:

```
risk_bankroll_cc = min( start_of_day_equity_cc,
                        available_cash_cc + haircut · portfolio_value_cc )
```

- **haircut default = 0.5** (`DEFAULT_PORTFOLIO_HAIRCUT = Fraction(1,2)`),
  applied ONLY to `portfolio_value` (never to cash). Integer floor, exact.
  **FLAGGED for operator** — set per risk tolerance; range [0,1], 0 = cash-only
  (most conservative), 1 = full equity. Constructor rejects values outside [0,1].
- **day-boundary rule = first successful poll of a new UTC calendar date**
  re-anchors `start_of_day_equity` to that poll's exchange equity.
  **FLAGGED for operator**: this is a simple deterministic boundary, NOT the
  exchange settlement session. `set_start_of_day_equity(cc)` overrides it (e.g.
  after a deposit, or for an ET/close-based boundary).
- The `min` does two jobs: right term keeps the denominator ~flat when capital is
  merely DEPLOYED (cash falls, mark rises — deployed ≠ lost); left term (SOD
  equity) refuses to inflate caps from an intraday mark-to-model GAIN.
- STALE ⇒ EVERY denominator accessor raises `StaleBalanceError` (fails closed).
  Raw cash and equity stay separately queryable; never conflated.
- `bankroll_cc` retained as a back-compat alias for `available_cash_cc`.

Deploy-capital proof (test): SOD all cash $1582.62 → deploy $500 into positions
(cash $1082.62, pv $500) → denominator = `10_826_200 + 0.5·5_000_000 =
13_326_200` cc, **>** bare cash `10_826_200` cc — NOT shrunk. Mark-gain proof:
pv jumps to $500 same day → `min(SOD 10_000_000, 12_500_000) = 10_000_000` —
gain cannot inflate.

## FIX 2 — book fees in the settlement ledger (fee-booking proof)

`Settlement.fee_cc` is booked at fill; `apply_settlement` subtracts it:
`realized = contracts × ((1−V) − entry_price) − fee`, and `accrued_fees_cc` is
queryable. **PROOF the field computes $0 for our combo maker fill via the REAL
`pricing/fees.py` (never reimplemented):**

```
FeeModel(FeeSchedule.from_strings("0.07","0.0175"), maker_conv)
  .trade_fee_cc(price_cc=5_000, qty=100ct, fee_type=QUADRATIC)  ==  0 cc
```

A QUADRATIC series with maker attribution hits the `Fraction(0)` maker branch in
`fees.py`. A synthetic TAKER series on the same inputs books the real
`0.07·1ct·0.5·0.5 = $0.0175 = 175 cc` — verified end-to-end through the ledger.

## FIX 3 — scalar settlement in the ledger (NOT the pricing fair)

`Settlement` now carries `settled_value V ∈ [0,1]` (the ACTUAL scalar, never
coerced to 0/1); `settled_yes` is a derived helper (`V ≥ 1.0`);
`Settlement.binary(settled_yes=…)` maps HIT→V=1 / MISS→V=0 so binary reads
unchanged. NO payout = `contracts × (1−V)` with the "rounded down" convention
(V floored onto the cc grid → `1−floor(V)`, NO-seller favorable ≤ ½ tick).
Idempotent per `position_id`; NO credit still gated on
`combo_no_pays_complement`. **The pricing fair is UNTOUCHED** (reactive stance,
docs/dnp_scalar_settlement.md). `max_loss` unchanged (worst case V=1 → forfeit
premium).

Scalar P&L table (1.00 ct @ $0.50, $0 fee):

| V | NO pays (1−V) | realized | note |
|---|---------------|----------|------|
| 0.0 | $1.00 | **+$0.50** (+5000 cc) | binary MISS — Phase-0 parity + demo ground truth |
| 0.5 | $0.50 | **$0.00** (0 cc) | scalar |
| 0.7 | $0.30 | **−$0.20** (−2000 cc) | scalar (task-specified) |
| 1.0 | $0.00 | **−$0.50** (−5000 cc) | binary HIT — Phase-0 parity |

## FIX 4 — rename `payout_obligation_cc` → `gross_settlement_notional_cc`

Renamed across `risk/`: `OpenPosition.gross_settlement_notional_cc` (docstring:
"gross settlement notional = contracts × $1; NOT capital-at-risk and NOT a cash
lock — do not cap cash/loss on this axis"), `ExposureSnapshot
.gross_settlement_notional_by_game_cc`, and the snapshot accumulator.

**Rename audit** — zero `payout_obligation` references remain in `src/` or
`tests/` (grep clean; only historical `docs/` records untouched). Every consumer
of the axis: `exposure.snapshot()` accumulates it, `limits.py` does NOT cap on
it (the caps bind on `worst_case_loss_by_game_cc` = premium/loss, `gross_notional_cc`
= Σ premium/loss, and deltas — never on the settlement-notional axis). Invariant
#2 (never summed with the loss axis) preserved.

> **Flag (not in scope, filed):** `ExposureSnapshot.gross_notional_cc` is a
> DIFFERENT field — it is `Σ max_loss_cc` (premium at risk), the LOSS axis,
> despite the word "notional" in its name. It was left as-is (renaming it is
> outside the `payout_obligation` task and would risk the loss-cap semantics),
> but the name is misleading and a future rename to `gross_premium_at_risk_cc`
> should be considered.

## NEXT STEPS

- **Owner: operator** — set the two FIX-1 knobs: `portfolio_haircut` (default 0.5)
  and confirm/replace the UTC day-boundary rule (or wire `set_start_of_day_equity`
  to the desk's session/deposit events).
- **Owner: eng (Phase 2)** — wire `risk_bankroll_cc` into the R2 %-of-bankroll
  caps (BalanceTracker is still Phase-0 spine, not yet consumed by `limits.py`);
  populate `Settlement.fee_cc` from the real fill fee at the settlement seam
  (`rfq/lifecycle.py:reconcile_combo_settlement`, still binary/farm-only today);
  reconcile predicted-vs-exchange to the cent incl. a fee case and a scalar case
  (Phase 1 "move on when" gate).
- **Owner: eng (backlog)** — consider renaming `gross_notional_cc` →
  `gross_premium_at_risk_cc` (it is the loss axis, not notional).
