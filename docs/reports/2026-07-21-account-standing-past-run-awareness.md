# 2026-07-21 — Full-account standing: past-run awareness, reserve adoption, transfer watch

Operator directive: the bot must **100% know its current standing, positions,
and balance — even for what a past run (or a pre-bot era) did** — and all
possible holes get patched before other sports arm.

## The account, reconciled to the cent (exchange ledger only)

New standing tool `tools/diagnostics/account_standing.py` (read-only; uses the
new `get_deposits`/`get_withdrawals` client methods, doc-verified 2026-07-21):

| | |
|---|---|
| Deposits (all-time) | **1 × $2,000.00 applied**, no withdrawals |
| All-time settlements | 56 markets, revenue $1,803.46 − cost basis $1,412.76 − fees $5.91 = **+$384.79 realized** |
| Identity check | $2,000.00 + $384.79 = $2,384.79 vs equity **$2,384.77** → residual **−$0.02** (order-time trading fees not in settlement rows) |
| **True all-time profit** | **$384.77** — exactly the operator's number |

Two findings: (1) **this account has zero pre-7/14 settlements** — the
operator's earlier combo history lives on a different account; (2) the local
store overstated premium by $15.29 vs the exchange's own cost basis (the
operator-authorized manual fill rows) — which is why the store-based statement
read +$369.50. The exchange ledger is the ruler; both tools now exist to read
it directly.

## Hole 1 — unmodeled positions now ADOPT (was alarm-only)

`position_reconcile_unmodeled_once` (5-min loop + first pass after startup)
now splits divergences three ways:

| class | treatment |
|---|---|
| Local fills row exists (our fill fell out of the book) | alarm-only — the fill-recovery sweep owns full re-modeling (no two-writer race) |
| **No local context** (past-run / manual / older-store era) | **ADOPTED as a conservatively-reserved holding** (P0-4 `risk_modeled=False`): side+count from the signed exchange position, premium-at-risk from the exchange's own `market_exposure_dollars`, entry rounded UP so booked max-loss ≥ the exchange figure (fail-safe LARGER). Counted in every deterministic/gross/concentration cap and as a deterministic reserve in the portfolio MC. Identity = a single self-leg (its own singleton cluster; permanently unreadable ⇒ the marginal watch never baselines it — no false trip). Never modeled from a guess: a row with no readable exposure figure stays alarm-only |
| Reserve the exchange reports flat (settled / manually exited) | RELEASED — held risk never overcounts forever |

## Hole 2 — deposits/withdrawals now auto-adjust the anchors (was "manual re-anchor")

New `_transfer_watch_loop` (5-min cadence) + `BalanceTracker.apply_external_transfer`:

- A NEWLY-applied deposit shifts **both** the start-of-day anchor and the
  intraday peak up by its net amount; a terminal withdrawal shifts both down
  by amount+fee. Daily-loss stays pure P&L (a deposit is not profit) and the
  give-back halts stay pure drawdown (a withdrawal is not a $-for-$
  give-back, and a deposit is not free headroom under the peak — both pinned
  by tests).
- First pass BASELINES existing transfers (their cash is already inside the
  balance the anchors formed on) — no double count — and emits the startup
  **`account_standing`** line: applied deposits/withdrawals, cash, equity,
  modeled positions, pending receivables. The bot now states its standing at
  every start.
- Pending→applied transitions are picked up; fetch errors leave anchors
  untouched (conservative direction).

This removes the last documented manual-anchor procedure
(`set_start_of_day_equity` stays as an explicit operator override only).

## In-play clarification (operator confirmed the built design)

Quote gate unchanged: a combo with ANY started leg never quotes. What gate 1
(built 7/20) adds is recognition: a *held* combo's in-play leg leaves the
dead-feed watch so the bot keeps quoting everything else instead of halting.

## Verification

| check | result |
|---|---|
| New tests (reserve adoption 4, transfer watch 10, updated reconcile test) | green |
| Full suite | **2576 passed, 0 failed** |
| mypy / ruff on all changed files | clean |
| Hot path | untouched (both loops are slow-cadence GETs) |

Files: `exchange/rest.py` (+get_deposits/get_withdrawals),
`ops/quote_app.py` (adoption + transfer watch + standing line),
`risk/balance.py` (apply_external_transfer), `ops/config.py` (doc),
`tools/diagnostics/account_standing.py`, tests.

## NEXT STEPS

- Remaining sport-switch gates: per-sport structural models armed,
  settlement-regime rho audit, leg taxonomy (readiness items 3–5) — the
  operator wants all holes closed before arming; these are the last three.
- At first relight: watch `account_standing`, `position_reconcile_reserved_adopted`,
  `external_transfer_anchors_adjusted`, `settlement_receivable_*` through the
  first slate.
- Owed: rho backtest verdict (detached run in progress); tape recorder restart.
- Owner: bot session. Operator decisions owed: none.
