# 2026-07-18 — Fill-recovery hardening: verify-before-discard + ledger-write loudness + runtime position-reconcile net

**Operator directive (2026-07-18, after TWO live incidents the same day): "the
risk book must count ALL combos."** Built, tested, suite green. Source edits do
NOT affect the running bot until its next restart.

## The incidents (both established from the live tape)

| | Incident A | Incident B |
|---|---|---|
| quote | `903935fc-…` | `7d79f32b-…` |
| ticker | `KXMVESPORTSMULTIGAMEEXTENDED-S202609506CD2247-39F452DE826` | `KXMVECROSSCATEGORY-S202637780267FC4-50B6C476E0E` |
| log | `live_20260718_tierflat.log` | `live_20260718_gametime.log` |
| lifecycle | accepted 16:24:25Z → reservation committed 16:24:26Z → sweep GET quote 16:24:39Z returned **`cancelled` / "execution failed"** → `fill_recovery_quote_cancelled` removed the position | accepted 18:29:50Z → committed 18:29:52Z (`risk_evicted_on_fill` ×2 fired) → sweep GET quote 18:30:02Z returned **`cancelled` / "execution failed"** → same discard |
| exchange truth | /portfolio/fills: order `c2befdcb-…`, 40.71ct NO @ 0.7660 (our bid 0.7670), fee 0.510840, **is_taker=true**, executed 16:24:28Z | /portfolio/fills: order `b40cd8b5-…`, 21.58ct NO @ 0.5540, fee 0.373380, **is_taker=true**, executed 18:29:53Z |
| damage | REAL position off-book ~2h until rehydrate `rehydrate_unmodeled_positions`; operator authorized a manual row | no fills row; book/persistence divergence |

## Root cause (one mechanism, both incidents)

The exchange has an RFQ-execution variant where the confirmed quote's own
execution "fails" (quote status → `cancelled`, `cancellation_reason:
"execution failed"`) and the fill executes anyway as a **taker-style REGULAR
order** — nonzero taker fee, `is_taker: true`, visible ONLY on
`GET /portfolio/fills`. **No `quote_executed` WS message fires for this
variant**, so `on_quote_executed` (the only fills-row writer) never runs —
that is incident B's "persistence miss": the writer never received anything to
reject; nothing in the writer was at fault. The 2026-07-16 recovery sweep's
only evidence source was the quote status, which it trusted as terminal:
`_recover_cancelled_fill` released/removed the position and wrote nothing.
In incident A the fill was ALREADY on /portfolio/fills eleven seconds before
the cancel report was read — a single verification poll would have caught it.

## What shipped (4 requirements)

1. **Verify-before-discard** (`rfq/lifecycle.py`): a CANCELLED status on a
   confirmed quote no longer discards. The position STAYS BOOKED while the
   sweep polls `/portfolio/fills` (new `FillsGetter` slice on the wrapped REST
   sender, subaccount-pinned) — default 3 polls spaced 90s (config
   `risk.fill_cancel_verify_attempts` / `fill_cancel_verify_delay_s`),
   injectable clock. Match = same ticker + our side + EXACT centi-contract
   count (price deliberately NOT matched — incident A filled 1 tick off our
   bid). Found ⇒ `fill_recovery_late_execution` WARNING + the row is written
   via the NORMAL `on_quote_executed` writer with the exchange-reported taker
   fee (`fee_cc_from_dollars_str`, round-up) booked into the ledger + realized
   P&L. Genuinely absent (≥1 successful read, no match, budget spent) ⇒
   discard exactly as before, evidence in the `fill_recovery_quote_cancelled`
   line. ALL reads errored ⇒ position KEPT (fail-safe) +
   `fill_recovery_verify_unresolved` ERROR.
2. **Ledger write can no longer fail silently**: the fills-ledger tail of
   `on_quote_executed` is now `_record_executed_fill`; any failure is a loud
   `fill_ledger_write_failed` ERROR + metric, state stays retryable, and the
   existing sweep replays the SAME writer path (bounded, loud exhaustion).
3. **Runtime position-reconcile net** (`ops/quote_app.py`): new
   `position-reconcile` task compares `GET /portfolio/positions` against the
   in-memory book every `risk.position_reconcile_interval_s` (default 300s)
   and alarms `position_reconcile_unmodeled` (tickers + which have local fills
   rows). Alarm-only — NEVER auto-inserts. Read-only GETs.
4. **Injectable/testable**: `FillsGetter`/`PositionsGetter` protocols, FakeClock
   cadence, zero network in tests.

New/changed knobs (`RiskConfig`, validated): `fill_cancel_verify_attempts=3`
(0 disables → old immediate discard), `fill_cancel_verify_delay_s=90.0`,
`position_reconcile_interval_s=300.0`. New log events (plain JSON, no
"emergency"): `fill_recovery_cancel_report_verifying`,
`fill_recovery_late_execution`, `fill_recovery_verify_unresolved`,
`fill_ledger_write_failed`, `position_reconcile_unmodeled`.

## Tests

- NEW `tests/test_fill_cancel_verification.py` — 11 tests: incident-A late
  execution (position kept, row via normal writer, taker fee 5109cc booked),
  incident-B fill-already-on-tape, wrong side/count/ticker never matches,
  truly-cancelled discards only after evidence, all-reads-errored keeps the
  position, writer-miss loud+retried (WS path and verified-replay path),
  attempts=0 fallback, reconcile net flags unknown/never inserts, config
  defaults+validation+pass-through.
- `test_fill_recovery.py` 15/15, `test_lifecycle.py` + `test_reservation_lifecycle.py`
  (66 with recovery), `test_persistence.py` 5/5.
- **FULL SUITE (post-review round): 2388 passed, 0 failed, 3 deselected
  (2:15).** ruff + mypy --strict clean on all touched files.

## Adversarial-review round (same day) — SERIOUS matcher defect FIXED

The verifier's probe disproved the "fractional counts are effectively unique"
premise (today's live ledger rows 59/61: same combo, same side NO, same exact
4071 centi-count, ~2h apart): the ticker+side+count matcher against the
ticker's ENTIRE recent tape could adopt a HISTORICAL fill — phantom kept +
duplicate row + taker fee re-booked. Three independent guards now gate
adoption (`_adopt_exchange_fill`), each pinned by its own test:

1. **min_ts time-scoping** — the /portfolio/fills query carries
   `min_ts = confirm wall-time − 60s slack` (new
   `OpenQuoteState.fill_confirmed_wall_ts` stamped at confirm success), so the
   server-side window is the verification window, not history.
2. **order_id ledger guard** — new `Store.has_fill_for_order_id`; a
   structurally-matching fill whose order_id is already in the local fills
   ledger is NEVER adopted (`fill_recovery_verify_match_rejected`, reason
   `already_in_ledger`).
3. **Claim set** — `_claimed_exchange_order_ids`: an in-flight verification
   claims the order_id before replay; a second concurrently-verifying quote is
   rejected `already_claimed` (pinned with a flaky-write test proving the
   claim — not the ledger — blocks the double adoption). Claim released once
   the row lands (ledger guard takes over). A fill without an order_id is
   never adopted (cannot be deduped — fail-closed).

MINOR also fixed: the all-reads-errored path no longer pops the state after
one round — a fully-errored round is retried on the same cadence
(`fill_recovery_verify_round_failed`, bounded `_CANCEL_VERIFY_MAX_ROUNDS = 3`,
9 polls total at defaults ≈ 13.5 min), THEN the loud
`fill_recovery_verify_unresolved` ERROR (position still kept — fail-safe). A
WS `quote_executed` arriving mid-verification is pinned to produce a single
row (loop-top `fill_recorded` ordering).

## Risks / residuals

- Same-window collisions (two identical-count same-side fills on one combo
  executed within the SAME verification window and neither in the ledger yet)
  remain theoretically ambiguous; the order_id claim + ledger guards make the
  double-count impossible — worst case is adopting the sibling's order_id,
  with both fills evidenced in raw_json/logs.
- min_ts trusts our wall clock within 60s of the exchange's; a worse skew
  could exclude the real fill → verified-absent discard (the pre-existing
  failure mode, now evidenced). NTP keeps this far inside 60s.
- The verified row books at OUR bid price (normal-writer semantics); incident
  A's actual fill was 1 tick better — max-loss slightly overstated
  (conservative). The raw exchange fill rides `raw_json` for audit.
- `fill_recovery.cancelled`-shaped discards are now delayed by up to
  ~attempts×delay (default ~4.5 min) — headroom stays held that much longer on
  a genuine cancel (the safe direction).
- The reconcile net alarms only the exchange→book direction (unmodeled
  exchange positions); book-but-not-exchange stays owned by
  reservation-reconcile/settlement.
- Live bot still runs pre-fix code until its next restart.

## NEXT STEPS

- **Operator**: restart the bot at the next natural window to arm
  verify-before-discard + the reconcile net (no YAML change needed — defaults
  are live-safe); add `fill_recovery_late_execution`, `fill_ledger_write_failed`,
  `fill_recovery_verify_unresolved`, `position_reconcile_unmodeled` to the
  monitor grep set.
- **Operator decision owed**: whether incident A's manually-authorized row
  needs de-dup against any future replay (fill_ref idempotency protects the
  normal path; the manual row's ref should be checked once).
- **Next session**: watch the first live `position_reconcile_unmodeled` cadence
  (should be silent); if Kalshi documents the "execution failed → taker
  re-execution" variant, record it in NOTES.md per hard rule 4.
