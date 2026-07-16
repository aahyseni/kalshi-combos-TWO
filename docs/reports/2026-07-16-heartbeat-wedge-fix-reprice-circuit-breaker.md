# 2026-07-16 — Heartbeat-wedge fix: reprice circuit breaker + mid-loop beats + non-blocking pool teardown

**Incident:** the 17:30Z run was emergency-killed at **18:13:22Z** (`supervisor_heartbeat_wedged`)
— the SECOND kill mechanism of the day, distinct from Problem B (the 30s timeout WAS applied).
Mechanism, pinned by throughput research lens 2 with primary sources: a burst of cold-tail combos
(5–8s p99 joint pricing) hits the 2.0s pool deadline; the abandoned futures **keep computing
worker-side** (`pricing_pool.py:16-18`) until all 8 workers are busy-frozen (terminal ticks:
68 calls / 68 timeouts / 0 completed). The maintenance tick's reprice sweep then serially burned
one full pool deadline per open quote (31 × 2.0s ≈ 62s) while the heartbeat is beaten only
BETWEEN ticks (`quote_app.py` maintenance loop) — the supervisor read the silence as a wedge and
killed a live, progressing bot. The same pattern also spiked intake-queue evictions 3.0%→27.9%.

**Fix (built inline by the orchestrator session — the subagent pool was down on API-529 for the
fourth consecutive run; reviewed line-by-line + tested; agent re-verify owed when capacity
returns):**

1. **Per-iteration heartbeat beats** — `QuoteLifecycle` gains an optional `beat` callback
   (quote_app passes `Heartbeat.beat`), invoked once per swept quote in the reprice loop and per
   REST poll in the fill-recovery sweep. Semantics preserved: a loop making progress is not a
   wedge; a genuine event-loop wedge still cannot beat (the fail-closed signal survives). A beat
   write failure is logged, never raised.
2. **Frozen-pool circuit breaker** — after `_REPRICE_POOL_TRIP = 2` consecutive
   `SKIP_PRICE_DEADLINE` reprices, the pool is presumed frozen and the REST of the sweep defers
   to the next tick (0.5s away). The tripped quotes keep today's fail-safe deletion; deferred
   quotes stay bounded by last-look freshness at confirm. Metric `reprice.pool_trip`.
3. **Sweep wall budget** — `_REPRICE_SWEEP_BUDGET_S = 2.5` total per tick; past it the remainder
   defers (metric `reprice.sweep_budget_deferred`). Belt-and-suspenders vs many
   slow-but-not-timeout awaits.
4. **Non-blocking pool teardown** (research kill-1, "cosmetic kill + needs_reconcile on every
   halt"): both `JointPool.shutdown` and `BookRiskPool.shutdown` drop the blocking
   `executor.shutdown(wait=True)` join — a worker grinding a cold tail stalled CLEAN stops past
   the heartbeat timeout. Reaping stays deterministic: the kill-job handle close in the `finally`
   fires `KILL_ON_JOB_CLOSE` and the OS ends every child immediately (the mechanism that already
   covered abnormal exits).

**What did NOT change:** no pricing decision differs for any individual quote (the breaker only
re-orders WHEN un-swept quotes get re-examined, next tick); quote-time caps untouched; cancel_all
was already concurrent (gather) and needed nothing.

**Verification:** 5 new tests in `tests/test_lifecycle.py` (breaker trips at 2 and does NOT
liquidate the remaining book; non-deadline NoQuotes reset the breaker and sweep fully; wall
budget defers with nothing deleted; beat fires per iteration; beat failure never breaks the
tick). Ruff + mypy clean. Full suite: see footer.

**Deeper fixes deferred to Throughput Batch 2 (by design, not oversight):** worker-side
cooperative cancellation (stop abandoned futures computing at all), event-driven reprice (kills
the 60–120 standing pool calls/s that make freezes likely), cold-combo shedding. This fix makes
the wedge NON-FATAL; Batch 2 makes it RARE.

## NEXT STEPS

- **Me:** full-suite green → commit + push → planned restart (bot currently live on `2bfae72`
  without this fix) → watch `reprice.pool_trip` fire harmlessly under the next freeze.
- **Owed:** agent adversarial re-verify of this + the five-item batch when API capacity returns
  (both were orchestrator-reviewed only); Throughput Batch 1 remaining items (F1/F2/fast-lane/F5)
  via agents, pre-game-day.
