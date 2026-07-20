# 2026-07-16 — Five-item batch: fill-record recovery sweep (P1), candidate-gate deadline YAML wiring, maker-fee section (eat-the-fee), startup first snapshot, drop-settled-on-rehydration

Base: `6d0f933` on `risk-audit-overnight` (suite 2139/0). All five items shipped
on the working tree with per-item regression tests; ruff + mypy at baseline
(zero new findings); full suite green (count in the footer of this report's
session summary). Nothing armed that changes live behaviour at default config:
every item is either a repair path, a dead-knob wiring, or gated on an empty
default.

## ITEM 1 — Fill-record recovery sweep (P1, real-money bug proven live today)

**Bug:** `rfq/lifecycle.py on_quote_executed` is the ONLY fills-ledger writer
and fires ONLY on the `quote_executed` WS message, which has NO replay. Proven
case (2026-07-16): quote `527b5a3a…` / order `c97d02d7…`, 117.07ct NO @ 80.60c
— log has accept + reservation-grant + gate-confirm + reservation-commit at
15:28:02Z and NO `quote_executed_msg`; `GET /portfolio/fills` confirms the fill
is real. Result: a live position invisible to P&L/EV/markouts/settlement
reconcile until the next-restart reconcile quarantined it (P0-4 reserve).

**Fix (lifecycle):**
- `OpenQuoteState` gains `fill_confirmed_mono_ns` (stamped on confirm
  SUCCESS), `fill_recorded`, `fill_recovery_attempts`.
- `maintenance_tick` → `_sweep_unrecorded_fills()`: for every parked state
  whose confirm succeeded but whose fills row never landed, after
  `fill_record_recovery_after_s` (LifecycleConfig, default 10.0, YAML-wired via
  `RiskConfig.fill_record_recovery_after_s`, validated positive-finite) it
  polls REST `GET /communications/quotes/{id}` (doc-verified status enum
  `open|accepted|confirmed|executed|cancelled`, openapi-comms.md):
  `executed` ⇒ synthesizes `{quote_id, order_id (creator_order_id),
  recovered_via_poll: true}` and replays the SAME `on_quote_executed` path
  (never a parallel implementation); `cancelled` ⇒ lapse cleanup (release
  straggler reservation, remove the phantom position booked at confirm,
  un-park); pending/error/unreadable ⇒ retry next tick, bounded (10 attempts →
  loud `fill_recovery.exhausted` + error log). Rate-bound 3 polls/tick. The
  GET handle is the SAME `RateLimitRecordingSender` the write path uses (new
  `get_quote` pass-through feeds 429s into the burst breaker); paper mode
  wires none.
- Metrics: `fill_recovery.{swept,recovered,cancelled,still_pending,errors}`
  (+ `exhausted`).

**Idempotency (store level, restart-safe):** `record_fill` is now
INSERT-if-absent on `fill_ref` in a single statement (returns inserted-or-not;
the ev_ledger row rides the same guard); new `has_fill()`;
`on_quote_executed` skips the ledger write + fee booking + metrics/markout on
a replay (`fill_replay_skipped`). A `CREATE UNIQUE INDEX IF NOT EXISTS
idx_fills_ref_unique` backstop is attempted at `Store.open` OUTSIDE the main
DDL in a try/except — a legacy DB holding pre-fix duplicates logs loudly
instead of bricking startup.

**Tests** (`tests/test_fill_recovery.py`, 18): missed message recovered exactly
once with row values identical to the WS path; WS+poll race both orders ⇒ one
row; store-level double-insert guard; cancelled ⇒ no row + proper lapse +
phantom removed; error retry; unreadable status never fabricates; bounded
exhaustion; 3-polls/tick rate bound; no-getter / NaN / non-positive delay /
unconfirmed-fill all fail closed; config default + validation + pass-through.

## ITEM 2 — `candidate_gate_deadline_s` YAML wiring (game-day knob)

`RiskConfig.candidate_gate_deadline_s: float = 2.0` (next to
`candidate_gate_enabled`), field-validated to (0, 3]; NEW model validator:
with `lastlook_mc_waiver_enabled`, `lastlook_mc_waiver_deadline_s +
candidate_gate_deadline_s <= 3.0` (the exchange confirm window — clear error
message). The LifecycleConfig construction in `quote_app.run` was extracted
into the pure `build_lifecycle_config(risk_cfg)` (the `supervisor_launch_cmd`
precedent — the Problem-B lesson was exactly a knob that never reached its
consumer), which now threads the deadline. **Deviation note:** this extraction
is a behaviour-preserving refactor of quote_app (proven: dataclass equality of
`build_lifecycle_config(RiskConfig())` vs the pre-wiring construction), done
so the pass-through is honestly testable without running the app.
Tests (`tests/test_candidate_gate_deadline_wiring.py`, 14): default parity +
bit-identical builder, per-field validation, joint-sum accept/reject both
sides of 3.0s, waiver-off exemption, pass-through.

## ITEM 3 — Maker-fee section (operator directive: EAT THE FEE)

- `FeeConfig.maker_fee_active_prefixes: tuple[str, ...] = ()` — the operator
  mirrors Kalshi's maker-fee list here when it changes (monitor
  `GET /series/fee_changes`); doctrine documented in the field comment: quoted
  prices stay unchanged/competitive, the fee is ACCOUNTED everywhere.
- Lifecycle `_maker_fee_active` (prefix match on combo market ticker OR
  collection ticker) + `_effective_fee_type` (QUADRATIC →
  QUADRATIC_WITH_MAKER_FEES upgrade) feed the EXISTING `_fill_fee_cc` seam —
  the real `FeeModel.trade_fee_cc` picks the verified 0.0175 maker
  coefficient (rule 8: no fee math reimplemented).
- At fill recording: `fee_cc` books the real fee, realized P&L takes −fee
  (existing path), and `expected_edge_cc` subtracts the predicted fee — all
  gated on the prefix list so the empty default is bit-identical.
- Problem-A waiver: THE CANDIDATE `WorstCaseEntity` now carries the predicted
  fee (`entity_from_position(candidate, fee_cc=…)`, hit-loss = premium + fee);
  committed positions/reservations stay `fee_cc=0` with a dated TODO
  (OpenPosition carries no per-fill fee — out of scope; conservative direction
  unaffected).
- NOTES.md assumption-audit rows MF1–MF4 appended (incl. the UNVERIFIED
  future-fee-announcement row and the prefix-keying caveat).

Tests (`tests/test_maker_fee_prefixes.py`, 9): empty ⇒ bit-identical (fee row,
edge, realized P&L, waiver candidate); active prefix (market AND collection
branch) ⇒ fee in fill row / realized P&L / edge / ev_ledger / waiver candidate,
to the cent vs FeeModel ground truth; non-matching prefix inert; quoted prices
unchanged by the flag; replay books the fee once; config default + YAML shape.

## ITEM 4 — Startup synchronous first snapshot (kills the 69 warmup declines)

`QuoteApp._startup_book_risk_snapshot(lifecycle, deadline_s=5.0)` — called in
`run()` AFTER `_rehydrate_exposure_book`, BEFORE the supervisor launch /
preflight / task start. It awaits `lifecycle.recompute_book_risk_offloop()`
(the EXISTING maintenance machinery — BookRiskPool worker when wired, inline
otherwise; nothing duplicated) under `asyncio.wait_for`. Timeout/error ⇒
today's behaviour exactly (warmup declines until the maintenance loop
publishes; startup never blocks; nothing faked).
Tests (`tests/test_startup_first_snapshot.py`, 5): with a held position the
pre-fix rig really declines `skip_portfolio_cvar` and the post-snapshot rig
quotes its FIRST RFQ; machinery-reuse spy; error ⇒ today's behaviour; timeout
bounded.

## ITEM 5 — Drop-settled-on-rehydration (clears the stale $4.46 reserve)

`_rehydrate_exposure_book` now checks each candidate ticker's `Market.status`
via `rest.get_market` before folding: ONLY `finalized` (the Market.status
FIELD vocabulary for settled — index-scan.md; plus the literal `settled`
spelling defensively) is dropped, logged `rehydrate_dropped_settled` with
ticker+status; `closed`/`determined` (payout not yet booked) keep today's
behaviour; ANY error (including a rest handle without `get_market`, so every
existing stub/test/embedding is untouched) keeps the position (fail-safe).
Dropped tickers also leave the "unmodeled" warning set (a settled market is
not an open exposure gap).
Tests (`tests/test_rehydrate_drop_settled.py`, 6): settled/finalized dropped
(+log, no unmodeled warning), closed/determined kept, poll error kept,
missing status kept, legacy rest kept.

## NEXT STEPS

1. **Operator:** commit + push this batch (per standing push-after-commit
   rule); relaunch decision — items 1/4/5 only take effect on the next
   restart of the live process (code on disk ≠ code in memory).
2. **Operator (game day):** if arming the last-look waiver, set
   `candidate_gate_deadline_s` so waiver+gate ≤ 3.0s (validator now enforces);
   the 1.5s recommendation from the Problem-A report still stands.
3. **Operator (when Kalshi announces combo maker fees):** flip
   `pricing.fee.maker_fee_active_prefixes` in the local YAML; watch the first
   real fill's reconciliation (defense #3 verifies the predicted fee to the
   cent) and NOTES row MF1.
4. **Agent (follow-up, small):** thread per-fill fees onto OpenPosition so
   committed positions' waiver entities stop riding fee_cc=0 (NOTES MF4 TODO).
5. **Watch:** first live restart should log `startup_book_risk_snapshot`,
   `rehydrate_dropped_settled` for the 7/14 MLB corpse, and (hopefully never)
   `fill_record_recovered_via_poll`.
