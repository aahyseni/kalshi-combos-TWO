# Risk engine PHASE 3 — single-writer risk-reservation service

**Date:** 2026-07-13. **Branch:** `risk-phase3` (based on `main`@`f08f25a`).
**Scope:** RISK_BUILD_PLAN Phase 3 — the concurrency & state-safety P0. A
single-writer, atomic, versioned risk-reservation service that reserves capacity
BEFORE the confirm round-trip (not after the fill), so two RFQs can never both
claim the same headroom; plus the confirm-TIMEOUT = assume-committed + reconcile
path.

**Suite: 1406/0 → 1427/0 (+21, 0 failed), 3 deselected.** (+16 the reservation
service unit tests, +5 the lifecycle-integration tests.) mypy strict + ruff clean
on every touched file. Behaviour in Phase-2 SHADOW mode is UNCHANGED — the 29
existing lifecycle + shadow-mode tests pass verbatim (the reservation only denies
on an ENFORCED breach, impossible while `caps_shadow_mode` is True).

## The hole this closes (one picture)

```
  BEFORE (Phase 2):                          AFTER (Phase 3):
  on_quote_accepted                          on_quote_accepted
    last-look check  ─┐  gap: headroom          last-look check
    confirm_quote()   │  is not reserved         RESERVE headroom  ◄── atomic,
    book position   ──┘  between check &          confirm_quote()       versioned
                         book — two accepts       commit / mark_unconfirmed
                         pass the SAME check       book position
                         against the SAME room
```

Today the hot path CHECKS the limits, then does a network round-trip
(`confirm_quote`), then books the position. Between the check passing and the
position landing in the exposure book, the reserved headroom is invisible: a
second accept passes the SAME check against the SAME (stale) headroom and both
confirm, silently breaching a cap. Race-free today ONLY because one asyncio loop
runs; this service makes the invariant hold for any future fan-out.

## What Phase 3 adds

```
                RiskReservationService  (single writer of headroom)
                            │
   try_reserve(candidate) ──┤  re-run LimitChecker.check against
                            │    committed positions (book)
                            │  + ALL outstanding reservations
                            │  + this candidate         ── ONE sync critical
                            │  if no ENFORCED breach → record + bump version      section
                            │  (shadow breaches dropped via the injected splitter) (atomic between
   commit(id)  ─────────────┤  fill real → promote reservation → committed position awaits)
   release(id) ─────────────┤  declined/lapsed → free the headroom
   mark_unconfirmed(id) ────┤  confirm TIMED OUT → assume committed, HOLD headroom
   reconcile(exchange_ids) ─┘  exchange-first truth → commit landed, release rest
```

New module `src/combomaker/risk/reservation.py`:
- `RiskReservationService` — wraps the `ExposureBook` + `LimitChecker`. Holds
  the layer of OUTSTANDING reservations between "checked" and "committed", and
  folds them into every headroom check via the checker's `candidate_positions`
  seam (so reservations reuse the EXACT same limit machinery — no reimplementation,
  hard rule 8).
- `Reservation` (id, position, version), `ReserveResult` (granted reservation OR
  the ENFORCED breaches that denied it), `ReconcileOutcome` (committed / released
  ids).

## The API (each method, exact semantics)

| method | when | effect | idempotent? |
|---|---|---|---|
| `try_reserve(id, candidate, …)` | before `confirm_quote` | re-check caps vs committed + outstanding + candidate in ONE sync section; on PASS record + bump version + return the reservation; on FAIL return the ENFORCED breaches, record nothing | yes — re-reserving a held id returns the same reservation, no re-check, no double count |
| `commit(id)` | confirm landed / execution | promote the held reservation into a committed `OpenPosition` in the book; drop the reservation | yes — a second commit is a no-op (returns False) |
| `release(id)` | confirm declined / lapsed | free the reserved headroom, book nothing | yes |
| `mark_unconfirmed(id)` | confirm TIMED OUT | ASSUME COMMITTED — keep the headroom held + flag for reconciliation | yes — flagging a flagged reservation stays True, no second version bump |
| `reconcile(exchange_ids)` | startup / after a timeout | commit reservations whose position-id the exchange reports open, release the rest | yes, order-independent |

## The invariants (all tested)

1. **No double-reserve** (`test_no_double_reserve_…`): a game-loss cap sized so
   ONE $50 fill fits ($80 cap) but TWO ($100) breach. The first reserve grants;
   the second — checked against committed + the first OUTSTANDING reservation — is
   DENIED. Without the layer both pass the same check against the same room. This
   is THE Phase-3 guarantee.
2. **Headroom frees on release, holds on commit/unconfirmed**
   (`test_reservation_frees_headroom_on_release_…`,
   `test_committed_reservation_still_consumes_headroom`,
   `test_mark_unconfirmed_keeps_headroom_held`): released headroom lets the next
   fill in; committed/unconfirmed headroom stays consumed (no double-spend, no
   silent vanish on a lost ack).
3. **Idempotency** on reserve / commit / release / mark_unconfirmed (a replayed
   message, or commit-after-timeout-then-real-execution, is a no-op that books
   exactly once).
4. **Version** bumps on every state mutation (a monotonic stamp a caller compares
   to detect the headroom moved); a DENIED reserve does NOT bump it.
5. **SHADOW-safe** (`test_shadow_breach_does_not_deny_a_reservation`): with the
   lifecycle's shadow split injected, a %-cap that WOULD breach is dropped → the
   reservation is still granted. The reservation layer never changes what the caps
   DO, only WHEN headroom is consumed.
6. **Fail-closed** (`test_fail_closed_bankroll_denies_when_enforced`): no bankroll
   (stale poll) ⇒ `SKIP_BANKROLL_UNAVAILABLE`; enforced → denied. Reuses the
   Phase-2 fail-closed layer verbatim.

## Confirm-TIMEOUT = assume-committed + reconcile

When `confirm_quote` raises (client-side timeout — it may have landed
server-side), the lifecycle calls `mark_unconfirmed`: the reservation STAYS
outstanding and keeps counting against every future check. Never released on a
lost ack — a reservation that vanished on a timeout would let a possibly-real
position stop counting against the caps (the exact silent-breach this phase
closes). The exchange ledger (defense #3) resolves it later via `reconcile`:
- `test_confirm_timeout_marks_reservation_unconfirmed` — the confirm raises →
  reservation held + flagged, position not yet booked, headroom still consumed.
- `test_execution_after_timeout_commits_held_reservation` — the real execution
  message arrives → `on_quote_executed` commits the held reservation exactly once.
- `test_reconcile_commits_landed_and_releases_not_landed` — exchange reports r1,r3
  open and r2 not → r1,r3 committed, r2 released, book = {r1,r3}.

## Wiring into the hot path (`lifecycle.py`)

- New optional constructor param `reservation` + `attach_reservation(...)` (wired
  AFTER construction — the service needs the lifecycle's shadow splitter and the
  lifecycle needs the service; the cycle is broken by post-construction attach).
- `_fill_position(quote_id, state)` — the SINGLE builder shared by
  `_book_position` and the reservation, so the headroom RESERVED equals the
  position BOOKED to the cent (same id, side, contracts, price, legs).
- `_reserve_headroom(...)` — called in the confirm path BEFORE `confirm_quote`.
  No service ⇒ always proceed (behaviour unchanged). Denied ⇒ the lifecycle
  DECLINES (`DECLINE_RISK_LIMIT`), never confirms.
- Confirm success ⇒ `commit`; confirm exception ⇒ `mark_unconfirmed`;
  `on_quote_executed` ⇒ `commit` (converts a still-held unconfirmed reservation, a
  no-op if already committed).
- `partition_breaches` — a public alias of the shadow split, so the shadow rule
  lives in ONE place (this lifecycle) and the reservation reuses it verbatim.

Wired in `quote_app.py`: the service is built after the lifecycle with
`breach_splitter=lifecycle.partition_breaches`, then `attach_reservation`.

## Design decisions made (not pre-specified)

1. **No locks — one synchronous critical section per method.** asyncio is
   single-threaded, and every public method has no `await`, so it is atomic
   between awaits by construction (the same guarantee the exposure book relies
   on). The API is already shaped for one writer if the system ever fans out onto
   threads (wrap the mutators in one lock; no interleaving reads of half-updated
   state). Documented in the module docstring.
2. **The reservation SHARES the lifecycle's shadow splitter** rather than
   duplicating the shadow rule — a shadow breach never denies a reservation, so
   Phase-2 SHADOW behaviour is byte-for-byte unchanged; the reservation only bites
   once caps are flipped to enforce.
3. **Reservation position id = `fill:{quote_id}`** = the exact id `_book_position`
   uses, so commit and the execution-replay booking are the same idempotent add
   (no double-count between the reservation and the book).
4. **Transient open-quote double-count at confirm** is conservative and
   documented: the quote's own still-open record is counted alongside its
   candidate fill in the reservation snapshot (dropped only at the end of
   `on_quote_accepted`) — over-counts, never under-counts, exactly like the
   existing last-look check. Steady-state (after commit + drop) is exact.
5. **`reconcile` operates on ALL outstanding reservations**, so it doubles as the
   exchange-first startup pass; the caller runs it only from the
   maintenance/startup loop (no confirm in flight), never mid round-trip.

## What is WIRED vs deferred

**WIRED (this pass):**
- `RiskReservationService` + `Reservation` / `ReserveResult` / `ReconcileOutcome`
  (`risk/reservation.py`).
- Lifecycle confirm-path integration: `reservation` param, `attach_reservation`,
  `partition_breaches`, `_fill_position`, `_reserve_headroom`, commit/
  mark_unconfirmed on confirm outcome, commit on execution (`rfq/lifecycle.py`).
- App wiring: service built + attached in `quote_app.py`.

**Deferred (genuinely out of Phase-3 scope; noted):**
- **Automatic post-timeout / periodic reconcile LOOP** — `reconcile()` is built
  and tested (the seam is ready), but wiring an automatic caller needs the
  exchange positions feed mapped to our `fill:{quote_id}` position-id scheme
  (`get_positions` → position-id map). That mapping is a Phase-6 concern (the
  Kalshi→our-position-id table). Until then a timed-out reservation stays held
  (conservative — headroom consumed) and is resolved by the real execution message
  if it arrives.
- **Fill-velocity ENFORCEMENT** — still deferred (Phase-2 report): the rolling
  committed-notional counter belongs here but needs a committed-fill stream; the
  reservation's `commit` is the natural feed for it in a later pass.

## NEXT STEPS

- **Owner: adversarial judge** — review this delta (concurrency invariant,
  idempotency, timeout-assume-committed, SHADOW-safety, the open-quote
  double-count conservatism); on PASS the orchestrator merges `risk-phase3` → main
  → pushes.
- **Owner: eng (next pass)** — Phase 4 (portfolio MC + challenger overlay); and,
  with the Phase-6 position-id map, wire the automatic reconcile loop + feed
  `commit` into fill-velocity enforcement.
- **Owner: operator** — no new knobs this phase; the reservation only enforces
  once `risk.caps_shadow_mode: false` (the Phase-2 sign-off gate) is flipped.
