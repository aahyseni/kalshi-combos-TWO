# P1-3 — Equity/P&L basis: no double count of position value (2026-07-15)

**Branch:** `risk-audit-overnight`  **Suite:** 1913 passed / 0 failed / 3 deselected
(rc=0, 84s). mypy clean on all touched source modules.

## The concern (RISK_ENGINE_AUDIT_ACTION_PLAN.txt §P1.3)

> "Prove the equity/P&L basis does not add entry-to-terminal P&L to already
> marked current equity and double count position value."

## What the code did

The A2 P(ruin) check evaluates `P(equity_basis + book_pnl < ruin_floor)`.

- `book_pnl` (`sim/engine._position_pnl`) is measured **entry-to-terminal**:
  `payout − price_cc` per YES contract, `(1 − payout) − price_cc` per NO. It
  already nets out the entry premium.
- The wiring fed `equity_basis = exchange_equity = available_cash +
  portfolio_value`, where `portfolio_value` is Kalshi's **current mark** of the
  same positions.

Adding an entry-based P&L onto a MARK-based equity double-counts the position:

```
exchange_equity + book_pnl
  = (cash + portfolio_value) + Σ(payout − price_cc)·c
  = cash + Σ payout·c + (portfolio_value − Σ price_cc·c)
                          └────── unrealized MTM, counted twice ──────┘
```

The residual `portfolio_value − Σ price_cc·c` is the unrealized mark-to-market
ALREADY in equity. When the mark sits above cost (unrealized gain) this
**understates** ruin (unsafe); below cost it overstates it. Only zero when the
mark equals entry (a fresh fill) — exactly why every prior UNIT test (mark ==
entry) passed while the LIVE path could double-count.

## The fix (COST basis — mark-independent, exact)

Feed the **cost basis** instead: `available_cash + Σ price_cc·contracts` of the
risk-modeled book. Then the entry premium cancels exactly:

```
cost_basis + book_pnl
  = (cash + Σ price_cc·c) + Σ(payout − price_cc)·c
  = cash + Σ payout·c                       (= true terminal equity)
```

independent of the intraday mark. `build_book_model` sets `fee_cc = 0` on every
`ComboPosition` (fees already debited from cash, 0 in `book_pnl`), so the basis
is premium only. Reserved (unmodeled) holdings are excluded from the basis
exactly as they are from `book_pnl` — their risk is the separate deterministic
reserve, never in this settlement-wave P&L.

Fail-closed preserved: stale/absent cash ⇒ basis `None` ⇒ the ruin cap simply
does not evaluate (never an invented equity). The give-back halt is unchanged
and still uses mark-based `exchange_equity` (drawdown vs peak) — a distinct axis.

## Changes

| File | Change |
|------|--------|
| `src/combomaker/sim/book_risk.py` | NEW `modeled_cost_basis_cc(model)` = Σ price_cc·contracts, with the no-double-count derivation; `_p_ruin_from_pnl` doc updated (basis is cost, not exchange equity) |
| `src/combomaker/risk/balance.py` | NEW `available_cash_cc_or_none()` (clearly-named, fail-closed cash accessor) |
| `src/combomaker/rfq/lifecycle.py` | `_build_book_risk_inputs` now feeds `_ruin_equity_basis_cc(model)` = cash + modeled cost basis, instead of `exchange_equity_cc_or_none()` |
| `tests/test_book_risk_equity_basis.py` | NEW — 9 tests: cost-basis math, reserved exclusion, THE reconciliation identity (`cost_basis + book_pnl == cash + Σ payout` to the cent + exchange-equity overshoot == unrealized MTM), mark-independence of ruin, stale-cash fail-closed, deposit tracks cash |
| `tests/test_risk_shadow_mode.py` | `_FixedBankroll` stub grows `available_cash_cc_or_none` (lifecycle now reads it) |

## Preserved invariants

Money stays int centi-cents (`int(round(cost_basis))` at the seam; probability
space unchanged). No cap raised/loosened. ES-vs-deterministic split, mutex cap,
held-positions provenance untouched. No live bot relaunch; no prod DB/config/env
touched.

## NEXT STEPS

- **Owner: operator.** Confirm the cost-basis convention (vs mark-based) for the
  ruin axis is intended. The give-back halt intentionally stays mark-based.
- **Owner: engineering.** Remaining P1 items: #4 (structural inversion residuals),
  #5 (public structural API), #6 (tape-derived parity), #7 (mutex audit), #8–11.
- **Owner: engineering.** Optional: thread per-position marks so unmodeled
  holdings' terminal value could enter a future full-account ruin basis; today
  they are correctly excluded (separate deterministic reserve).
