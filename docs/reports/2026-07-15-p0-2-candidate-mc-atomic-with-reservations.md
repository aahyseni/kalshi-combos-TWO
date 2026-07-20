# P0-2 — Candidate MC atomic with reservations (2026-07-15)

**Branch:** `risk-audit-overnight`  **Suite:** 2040 passed / 0 failed / 3 deselected
(rc=0). mypy + ruff clean on all touched source modules. Baseline restore =
`45164f1`.

## The defect (RISK_ENGINE_LIVE_BALANCING_VALIDATION_AUDIT.txt §P0-2)

The confirm path ran, in order:

```
analytic last-look  ->  build candidate-MC inputs from CURRENT reservations
                    ->  AWAIT candidate MC worker  ->  fill-velocity
                    ->  create risk reservation    ->  send confirm
```

The candidate MC awaited a worker BEFORE creating its reservation. During that
await a second accepted quote began its own candidate evaluation against the SAME
old pre-book (no reservation for either candidate yet), so:

```
Accept A snapshots 0 reservations   Accept B snapshots 0 reservations
A & B run MC concurrently   A passes vs old book   B passes vs SAME old book
A reserves                  B reserves later
```

B's ES / P(ruin) decision never included A. `CandidateBookRiskInputs` carried no
`reservation_version` and there was no version/generation comparison on return, so
two concurrent accepts could each admit against a book the other had already
claimed — the combined model tail could breach the portfolio budget.

## The fix (preferred audit flow)

Reordered the confirm block and made the candidate gate ATOMIC with the reservation
book. The gate now, per contemplated fill:

| step | what | why |
|------|------|-----|
| 1 | Create a **PROVISIONAL reservation** for the candidate FIRST (under the analytic hard caps), before the MC | a concurrent accept's MC now SEES this candidate's held headroom (its reservation folds into every `try_reserve` check AND bumps the reservation VERSION) |
| 2 | Capture `ExposureBook.position_generation` **and** `RiskReservationService.version` into the MC inputs | both are needed — a concurrent *reserve/release* moves the version even when the position generation does not |
| 3 | Run the candidate MC over committed + all OTHER reservations + the provisional candidate (candidate rides as `candidate`, excluded from PRE to avoid double-count) | measures the true merged tail |
| 4 | On return, re-read live generation + version; if EITHER moved, DISCARD + REBUILD + retry | verdict priced a book that no longer exists |
| 5 | Retry loop BOUNDED by the confirm deadline (`candidate_gate_deadline_s`, wall budget) AND `candidate_gate_max_retries` | do not let risk math silently consume the confirm window (audit LIVE CANDIDATE-GATE LATENCY) |
| 6 | MC declines / errors / times out / insufficient deadline / unstable book ⇒ RELEASE the provisional reservation + DECLINE (`DECLINE_CANDIDATE_RISK`) | fail-closed; headroom never lingers for a non-fill |
| 7 | MC passes within deadline ⇒ keep the reservation, confirm, then commit (books the position) | atomic reserve → gate → confirm → commit |

New confirm order (inside `if decision.confirm:`):

```
park state -> FILL-VELOCITY -> PROVISIONAL reserve -> CANDIDATE GATE (atomic,
  version+generation checked, retry-bounded) -> confirm -> commit
```

Fill-velocity stays FIRST (a runaway rate never even reserves). The reservation
now precedes the gate (the P0-2 core), and the gate releases it on any decline.

### Strictly additive / safety default preserved

- The gate lives inside `if decision.confirm:` — it can only flip an ADMIT to a
  DECLINE, never a decline to an admit.
- No existing decline or cap was removed or loosened; the provisional reservation
  reuses the SAME `LimitChecker` + shadow split, so shadow-mode behaviour is
  unchanged (a %-cap shadow breach still never denies).
- `candidate_gate_enabled=False` ⇒ gate skipped, reservation-then-confirm exactly
  as before (kill switch intact).
- No reservation service (paper / backtests) ⇒ `reservation_id=None`, one MC
  attempt, stamps default to `-1` and the version check is inert — prior behaviour
  preserved (a single-loop confirm cannot race).

### Money math

Untouched. Money stays integer centi-cents; the MC operates in float cc simulator
space as before. The equity/P(ruin) basis is still COMMITTED-ONLY (P1-3), verified
by the existing `test_candidate_ruin_equity_basis` suite (unchanged, still green).

## Files changed

- `src/combomaker/ops/pricing_pool.py` — `CandidateBookRiskInputs` gains
  `input_generation` + `reservation_version` (default `-1` = not stamped).
- `src/combomaker/rfq/lifecycle.py` —
  - `LifecycleConfig`: `candidate_gate_deadline_s` (2.0s), `candidate_gate_max_retries` (3).
  - `_build_candidate_gate_inputs`: `exclude_reservation_id` kwarg (drop the
    candidate's own provisional reservation from PRE), stamp generation + version.
  - `_run_candidate_mc`: extracted single-eval helper (off-loop or inline).
  - `_candidate_gate_verdict`: rewritten as the atomic, version-checked,
    deadline-bounded retry loop; takes `reservation_id`.
  - confirm block: reservation moved BEFORE the gate; release on gate decline.
- `tests/test_candidate_gate_atomic.py` — NEW (9 tests, see below).

## Tests (all required audit cases + deadline)

- two concurrent candidate gates cannot ignore each other (gather; each MC sees the
  other's reservation);
- a reservation ADDED during the MC ⇒ version conflict ⇒ discard/retry;
- a reservation RELEASE during the MC ⇒ version moves ⇒ reevaluation;
- combined candidates exceeding ES/P(ruin) admit at most the safe subset (the second
  sees the first's reservation and declines);
- provisional reservation RELEASED on MC decline, error, and unstable-book/retries-
  exhausted;
- gate FAILS CLOSED when a retry would exceed the confirm deadline (only one MC ran,
  window not consumed);
- provisional reservation precedes the MC and inputs carry the generation + version
  stamps (candidate excluded from PRE).

New metrics for live observability: `candidate_gate.version_conflict_retry`,
`candidate_gate.retries_exhausted`, `candidate_gate.deadline_exceeded`.

## NEXT STEPS

- **Owner: agent.** Fold the three new `candidate_gate.*` counters into the live
  candidate-gate latency panel the audit asks for (p50/p90/p99 runtime, remaining
  confirm-window at completion, version-conflict retries, deadline losses).
- **Owner: operator.** Set `candidate_gate_deadline_s` for the REAL Kalshi confirm
  window (2.0s is a conservative default vs the ~3s window; leaves margin for the
  confirm RTT). Decide `candidate_gate_max_retries` for expected concurrent-accept
  churn.
- **Owner: operator.** This gate has still not run on a real won auction (audit MC
  BALANCING / LIVE CANDIDATE-GATE LATENCY). Capture the first live gate latency +
  version-conflict counts before trusting the retry bound in production.
