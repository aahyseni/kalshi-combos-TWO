# Stage A — settlement ledger + reconcile + restart safety WIRED

**Date:** 2026-07-13. **Branch:** `wire-live` (worktree; not pushed/merged).
**Suite:** 1644/0 (+48), mypy strict clean on touched files, ruff clean.

Closes the biggest BUILT-NOT-CALLED gaps the 2026-07-13 code audit flagged
(`2026-07-13-risk-engine-code-audit-go-live.md` §3): the realized-P&L ledger and
the exchange-first settlement reconciliation had **zero live callers** — nothing
constructed a `Settlement`, so realized P&L stayed `0` forever and no fill
reconciled cash/fee/sign to the cent (a standing **defense #3** violation). Now
WIRED, functionally ACTIVE in paper/quote mode, and tested. Prod real-money stays
gated by the pre-existing `--confirm-live` + `prod_limits_configured` + whitelist —
"live" here means reachable + enforcing in quote/paper mode, NOT going to prod.

## What was wired

| # | Item | Where | State |
|---|------|-------|-------|
| 1 | **Settlement source** `GET /portfolio/settlements` | `exchange/rest.py` `get_settlements` | NEW REST method (doc-verified schema: `market_result` yes/no/scalar, `value` int cents nullable, `revenue` int cents, `fee_cost` $) |
| 1 | **Settlement POLLER + HANDLER** | `risk/settlement.py` (NEW) | `SettlementPoller` (async loop, sibling of `_balance_loop`) pages settlements to exhaustion; `SettlementHandler` books each settled position we HOLD |
| 1a | books `Settlement` via `apply_settlement` | `risk/settlement.py` → `balance.py:445` | realized P&L + fee, NO pays `contracts·(1−V)` |
| 1b | feeds `record_realized_pnl` | → `lifecycle.py:662` | the ENFORCED daily-loss cap now sees realized P&L (its realized half was 0) |
| 1c | RECONCILES predicted vs exchange revenue TO THE CENT, HALTs on mismatch | `lifecycle.reconcile_combo_settlement` | `HALT_RECONCILIATION_MISMATCH` (defense #3), never a log |
| 2 | **`reconcile_combo_settlement` extended** farmed-tripwire → FULL path | `rfq/lifecycle.py:666` | reconciles EVERY settled position on the ticker (Σ predicted credit vs `revenue`) + keeps the farmed settle-YES tripwire (tripwire runs first) |
| 3 | **`fee_cc` from the real fill** (was `fee_cc=None`) | `lifecycle.on_quote_executed` + `_fill_fee_cc` | computed from the real `pricing/fees.py` model ($0 for our combo maker quadratic fill, correct for a nonzero-fee series); `None` only when no model wired / fee UNKNOWN (never a guessed 0) |
| 4 | **`reservation.reconcile(real positions)`** (was `set()`) | `quote_app._reconcile_reservations` + `reservation.py` mappers | `GET /portfolio/positions` → `{combo_ticker: Side}` → the reservation ids the exchange confirms open; commits landed / releases leaked. Called from a periodic `_reservation_reconcile_loop` AND the startup pass (replaces the empty `set()` at former `quote_app.py:532`) |
| 5 | **Restart safety** — drop `needs_reconcile` on an in-process HARD trip | `quote_app.mark_reconcile_on_hard_halt` (registered as an `on_halt` callback) | HARD-class (`HALT_HARD_TRIP`, `HALT_RECONCILIATION_MISMATCH`, `HALT_FILL_VELOCITY`, `HALT_DRAWDOWN`, all 7 breaker halts) ⇒ `ReconcileMarker.set()` so a bare restart is BLOCKED (`HALT_NEEDS_RECONCILE`) until reconciled. Soft/manual halts leave the marker alone |

## Fail-closed discipline (hard rule 6 + defenses #2/#3)

- An **UNKNOWN/inconsistent** settlement row (`market_result` unreadable, `scalar`
  with no `value`, `value` inconsistent with a binary result, out-of-range value)
  → `parse_settlement` raises → the handler HALTs `HALT_RECONCILIATION_MISMATCH`.
  It **never** silently books 0.
- A **NO credit with `combo_no_pays_complement` unverified** → `apply_settlement`
  raises → HALT (the convention gate stays the sole authority).
- **To-the-cent**: predicted gross settlement credit (Σ `contracts·payout_per_ct`;
  LONG NO pays `$1−V`, LONG YES pays `V`) must equal the exchange's booked
  `revenue`; any mismatch HALTs.
- **Idempotent** per `position_id` (the handler's `_reconciled` set + the ledger's
  own `_settled_ids`): a re-polled settlement is a no-op, never a double-book.
- Money is integer centi-cents / `Fraction` throughout; no binary-float money.
  Secrets never touch these modules.

## Money math (anchored to the 2026-07-10 demo ground truth)

`V = value_cents/100` (payout per YES contract; binary `no`→0, `yes`→1). LONG NO
pays `$1−V`/ct. LONG NO 1.00 ct @ $0.50 settling NO (V=0): predicted credit
`1·$1 = 100¢` = exchange `revenue`; realized `= (1−0)−0.5 = +$0.50`. A scalar V=0.7
→ NO pays $0.30/ct → realized `−$0.20`, revenue `30¢`. All reconcile to the cent.

## Demo/paper NOT bricked

A fresh start with **no positions** is a pure no-op: the settlement handler finds
no held position matching any settlement ticker (skips); the reservation reconcile
skips the network entirely when nothing is outstanding. Quoting is unaffected. The
new loops wrap all transient REST errors and retry — only a real mismatch HALTs.

## Tests (+48, all green)

- **`test_settlement.py`** (NEW): row parsing fail-closed (unreadable result,
  scalar-without-value, inconsistent value, out-of-range); a fake poll books
  realized P&L (NO-miss +$1/ct−premium, NO-hit −premium, scalar partial, fee
  subtracted); to-the-cent mismatch HALTs; unreadable row HALTs; farmed-settle-YES
  HALTs; unverified NO-convention HALTs never books 0; idempotent double-poll
  no-ops; poller pages to exhaustion + empty poll no-op.
- **`test_lifecycle.py`** (+): full-path reconcile matches / cent-mismatch HALTs /
  scalar matches / sums multiple positions on a ticker / farmed tripwire precedes
  the revenue check; the real fill fee is `None` without a model and `0` for a
  wired combo-maker quadratic model.
- **`test_reservation.py`** (+): `open_combo_tickers_from_positions` (signed
  `position_fp` → side, aliases, unparseable/missing skipped fail-closed);
  `reservation_ids_backed_by_exchange` (side-match commits, opposite/absent
  released); end-to-end confirm-timeout reconcile against real positions.
- **`test_quote_app_phase6.py`** (+): every HARD halt drops the marker (restart
  blocked) + every soft/manual halt leaves it; `_reconcile_reservations` commits
  landed / releases leaked; no-op when nothing outstanding.

No existing test was weakened or deleted; the farmed-only `reconcile_combo_settlement`
tests read unchanged (the new params default to the tripwire-only path).

## NEXT STEPS

- **Owner: operator** — decide the settlement/reservation poll cadences
  (`SETTLEMENT_POLL_INTERVAL_S`=30s, `RESERVATION_RECONCILE_INTERVAL_S`=15s in
  `quote_app.py`); both are conservative first-live values.
- **Owner: eng (deferred, out of this stage's scope)** — the remaining audit §3
  items: arm the portfolio-CVaR cap by threading a `BookRiskSnapshot` into the
  live `check()` sites; implement fill-velocity compute (the cap + `HALT_FILL_VELOCITY`
  exist but nothing computes velocity); feed the 3 dark breakers real inputs;
  auto-launch the external supervisor as a real process (preflight should check a
  *running* watcher, not credential presence); thread the pricer's `within_game_rho`
  + bankroll into the report MC.
- **Owner: eng** — a demo settlement e2e (real combo round-trip → settle → this
  poller books + reconciles it live) to promote "LIVE-VERIFIED settlement" from
  test-only to a real live-code exercise (audit §4.1).
- **Owner: docs** — the `combo_no_pays_complement`-verified claim in CLAUDE.md is
  now backed by a live code path (this stage), not just tests; the audit's §4.1
  discrepancy is closed for the settlement-booking path.
