# Risk engine — RE-AUDIT after the "wire it all live" change

**Date:** 2026-07-13 (later same day). Fresh unbiased 6-parallel-auditor + critic
pass over the ACTUAL merged code at `main` `3aabae5` (after the wire-live change),
same neutral prompts as the pre-wiring audit (`2026-07-13-risk-engine-code-audit-go-live.md`,
now SUPERSEDED). Apples-to-apples.

## The flip (code-side)

| Status | Pre-wiring (`2f39812`) | Now (`3aabae5`) |
|---|---|---|
| ENFORCED-LIVE | 23 | **41** |
| SHADOW-LOGGED | 24 | **9** |
| BUILT-NOT-CALLED | 14 | **6** |

The wiring is real. In **`--mode paper/quote`** (where the risk engine runs):
`caps_shadow_mode` genuinely defaults **False** ⇒ the %-of-bankroll caps,
fill-velocity, and the give-back drawdown/hard-trip KILL **enforce**; the
settlement poller books realized P&L via `apply_settlement` + `record_realized_pnl`
and **reconciles predicted-vs-exchange to the cent → HALT on ≥1¢ mismatch**; the
reservation gates confirms; the portfolio-CVaR cap reads a live MC snapshot built
with the pricer's **real** per-pair rho; the 3 previously-dark breakers are fed
real inputs; the supervisor is launched as a subprocess + preflight verifies it's
beating. Verified at runtime (16 critic probe-tests pass), suite **1693/0**.

## What REMAINS (why go-live-ready is still FALSE) — code side

1. **The shipped default `mode: observe` runs ZERO risk enforcement.** `cli.py`
   routes `observe` → `ObserveApp` (RfqFilter + logging only). The whole risk
   engine only arms under `--mode quote`/`paper`. By design (observe = record the
   tape), but it means "the caps are on" is only true when you deliberately quote.
2. **The settlement / reconcile-HALT chain is proven in TESTS ONLY.** It has never
   run against a real Kalshi settlement — it only fires after a real fill settles.
   The `combo_no_pays_complement` convention it gates on came from ONE demo
   settlement. First real fills are where a convention/sign/fee error surfaces
   (that's what the HALT is for — you want to trip it on tiny size, not at scale).
3. **The external supervisor** is a same-host subprocess needing the
   `KALSHI_SUPERVISOR_*` credential (else KILL-only, no cancel path); a truly
   separate host is a deploy step.
4. **The portfolio-CVaR cap is PRE-TRADE only** — it blocks a *new* quote that
   would push book ES over the limit, but a book that is *already* over-tail (e.g.
   after correlated intraday drift) does **not** self-halt. Only the give-back
   equity halts + daily-loss catch a developing loss.
5. **Prod is hard-refused by design** until `prod_limits_configured: true` +
   `--confirm-live` + a non-empty whitelist (`prod.yaml` ships `false`).
6. **Intentionally still dark (correct per the record):** inventory skew + widen
   (`enabled=False`, awaiting the pooled shadow-markout study) and the ScheduleCache
   (no verified schedule feed yet). These are documented to stay off until measured.

## The gate a code audit CANNOT clear — the edge

Caps limit downside; they do not create profit. The **markup** does, and per the
standing rule it must come from POOLED MULTI-WEEK game-clustered settlement —
never one window — and that data does not exist yet. A correct, fully-enforced
risk engine wrapped around an unvalidated edge just loses money more safely.

## Verdict

The risk engine is **code-complete and enforced-when-armed** — but it is OFF by
default, its settlement/convention path is unproven against the real exchange, the
supervisor needs provisioning, and the edge is unvalidated. **NOT ready to deposit
and trade real size.** The safe path is the plan's own runway: `--mode paper` →
drive one real fill+settle through the reconcile HALT → provision the supervisor
credential → tiny live at $2k as VALIDATION → pool multi-week → re-derive
caps+markup → only then scale.

## NEXT STEPS
- **Owner: operator** — treat "audit green" as "code is correct," not "safe to
  size up." The first live is validation under fire on tiny capital.
- **Owner: eng** — a standing (not just pre-trade) portfolio-ES halt off the MC
  snapshot would close gap #4; the schedule-feed data source unblocks pregame
  precision; the markup decision is the pooled-multi-week measurement track.
- **Owner: docs** — the pre-wiring audit is banner-superseded; keep this file as
  the current code-state of record.
