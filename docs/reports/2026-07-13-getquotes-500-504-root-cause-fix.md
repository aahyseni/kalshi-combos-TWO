# get_quotes 500/504 — root cause + fix (bounded window + 5xx retry)

**Date:** 2026-07-13
**Scope:** demo Phase-2 shakedown blocker — `combomaker run --env demo --mode quote`
crashed at **startup reconcile** with HTTP **500 then 504** on
`GET /communications/quotes?user_filter=self`.
**Status:** **SHIPPED.** Code-complete, unit-tested, mypy/ruff clean, merged to
`main`. The demo A/B was **deliberately abandoned** (operator call) — demo P&L
validates plumbing not edge, midland-demo was degraded, and the fix is
environment-agnostic (prod runs the **same** midland circuit-breaker). See §5.

---

## 1. Symptom

The bot's `_startup_reconcile` calls `get_quotes(user_filter="self")` to find and
cancel any leftover resting quotes before it starts a new session. On demo this
returned:

```
HTTP 500 internal_server_error {service: midland}   (first attempt)
HTTP 504 gateway_timeout                            (retry)
```

The operator was rightly skeptical of "demo is down" — **we have made and
executed quotes on this same demo account before** (Phase 5 e2e, and the LAA
combo that settled NO for $1.00 on 2026-07-10, balance now $1083.62).

## 2. What it is NOT

Ruled out by direct probing against the live demo endpoint:

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Auth / signature | **NO** | balance + positions return 200; the earlier 401 was a stale demo PEM, since fixed |
| Whole demo env down | **NO** | `GET /portfolio/balance` → `108362` cents ($1083.62), instant |
| Bad `status` param value | **NO** | removing/varying `status` did not change the 500 |
| Our REST client / signing path | **NO** | same client succeeds on every non-`midland` endpoint |

## 3. Root cause

`GET /communications/quotes` with **no time window** makes Kalshi's
`midland-exchange` service scan the account's **entire quote history**. On an
account with real quote history that scan is expensive enough to trip midland's
**circuit-breaker**: the first request runs long and 500s; the breaker opens and
subsequent requests **504 / 503 `service in fail-fast`**.

The docs list `min_ts` / `max_ts` as **optional**. Empirically they are
**required in practice** to keep the query off the full-history scan. The
identical query with a bounded window returns **instantly** (verified live:
`quotes=1`). → recorded as a hard-rule-4 discrepancy in `NOTES.md`.

## 4. The fix

New shared helper **`src/combomaker/exchange/quote_query.py`**:

- **`list_open_quotes(rest, now_ts, …)`** — sends `user_filter=self`,
  `status=open`, **`min_ts = now − 7 days`** (Kalshi's quote-retention horizon),
  **`max_ts = now + 300 s`** (clock-skew buffer), `limit = 500` (documented max),
  paginates by cursor, and **retries 5xx with exponential backoff** (default 4
  attempts). A **4xx is never retried** (fail fast on a client error); an
  exhausted 5xx **re-raises** (fail closed, never silently "no leftovers").
- **`open_quote_ids(quotes)`** — extracts `id` then `quote_id`, dropping blanks.

**Both call sites now use it** — and the second one is why this matters beyond a
startup nuisance:

| Call site | File | Why it must not 500 |
|---|---|---|
| Startup reconcile (cancel leftover quotes) | `ops/quote_app.py::_startup_reconcile` | blocks a clean start |
| **Supervisor emergency cancel-all (kill path)** | `ops/supervisor.py::KalshiSupervisorExchange.list_open_quote_ids` | **an unbounded enumeration would 500 exactly when we are trying to pull every quote off the book** |

`KalshiSupervisorExchange` now takes a `Clock` (for the window's `now`); the
supervisor CLI and its tests pass one through.

## 5. Verification

**Unit (hermetic):** `tests/test_quote_query.py` — window params are bounded &
filtered; pagination carries the window across cursors; **5xx→5xx→200 retries
then succeeds**; a **4xx raises without retry**; exhausted 5xx re-raises (fail
closed); id extraction. `tests/test_supervisor.py` updated for the new `Clock`
arg.

- **Full suite: 1699 passed, 3 deselected** (integration-marked).
- **mypy**: clean on all three changed source files.
- **ruff**: clean on all changed files. (Pre-existing `pricing/ising_amm.py` +
  `tools/ising_amm_run.py` lint/type debt is unrelated and untouched.)

**Live (demo) — why we did NOT chase a fresh A/B:**

- Demo env was confirmed UP (`GET /portfolio/balance` → $1083.62, instant; auth
  OK), but **midland was degraded** and 503/504'd _every_ quote request — windowed
  and unbounded alike. In a degraded window BOTH shapes fail, so an A/B produces
  no signal until midland recovers.
- **Operator decision (2026-07-13): skip it.** Rationale: (a) demo P&L/plumbing is
  not a graduation criterion (CLAUDE.md quiet-failure rule 5); (b) the decisive
  evidence already exists — the **prior-session within-session A/B**, where in the
  _same_ time window the windowed query returned `quotes=1` instantly while the
  unbounded query 500/504'd (query **shape** was the only variable); (c) the fix
  is **environment-agnostic** — prod runs the same midland circuit-breaker, so the
  window + retry is required for prod regardless of any demo repro; (d) the fix is
  **strictly better** than the old unbounded call under either theory ("window
  avoids the scan" or "midland is just flaky"): it bounds the query AND adds 5xx
  backoff-retry that rides through exactly the degradation we observed.

**Bottom line:** the fix ships on unit proof + the prior within-session A/B +
the strictly-better/env-agnostic argument. Its real-world exercise now happens
where it matters — the **prod kill path** — under tiny size.

## NEXT STEPS

- **Owner: bot. DONE.** Merged `fix-getquotes` → `main` (code + tests + this
  report + the `NOTES.md` row), pushed, worktree removed.
- **Owner: operator + bot.** Go-live path is now **prod-tiny directly** (demo
  quote-mode shakedown skipped by operator call). Prereqs that actually protect
  the $2k, per `2026-07-13-go-live-runbook.md`: kill switch works (this fix),
  `mode: quote` + non-empty leg-series whitelist + `prod_limits_configured: true`,
  caps ENFORCED (not shadow), start TINY. Arm with
  `combomaker run --env prod --mode quote --confirm-live`.
- **Owner: operator.** The **markup/edge** is still validated only by
  backtest — the live gate is the **pooled multi-week** markout study, never a
  single-window P&L. Live-tiny is how we start collecting that, not skip it.
