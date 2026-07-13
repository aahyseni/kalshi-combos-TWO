# Risk engine — code-grounded audit (phases 1-6) + go-live verdict

**Date:** 2026-07-13. Method: six parallel per-phase auditors read the **actual
merged code** on `main` (`2f39812`), classifying every component as
ENFORCED-LIVE / SHADOW-LOGGED / BUILT-NOT-CALLED, grounded in file:line — plus my
own first-hand verification of the load-bearing wiring (`quote_app.py`) and the
two most consequential claims (grep-confirmed). This report says what the code
**does**, not what the phase reports claim it should do. Tallies: 23 ENFORCED,
24 SHADOW, 14 BUILT-NOT-CALLED.

## VERDICT: NOT go-live ready — and it is a multi-effort wiring + settlement job, not a flag flip

Under the shipped default config (`mode=observe`, `caps_shadow_mode=True`) **the
advertised risk engine is essentially inert.** What actually gates a quote today
is the *pre-existing* hard-dollar caps + the pregame gate + the prod guard. Every
Phase-1..6 headline (equity-aware bankroll, %-of-bankroll caps, give-back KILL,
portfolio-CVaR, skew, external supervisor) is shadow-logged, built-not-wired, or
only instantiated in a non-default mode.

---

## 1. What ACTUALLY changes a quote today (ENFORCED, default config)

| Enforced control | Code | Value |
|---|---|---|
| per-game delta cap | `limits.py:325-364` (game-keyed via `exposure.snapshot`) | 500 contracts |
| per-game worst-case-loss cap | `limits.py` | **$1,000** (hard dollar) |
| book-wide gross premium notional | `limits.py` | **$5,000** (hard dollar) |
| per-quote size / notional | `limits.py:284` | 100 ct / $500 |
| max open quotes | `limits.py` | 20 |
| unknown-marginal ⇒ breach | `limits.py:284-297` | fail-closed |
| hard daily-loss HALT | `limits.py:366` | **$500** — but reads `daily_pnl.total_cc` whose **realized half is永 0** (see §4), so only *unrealized* mark-to-mid can trip it |
| pregame gate (Phase 3) | `pregame.py:170-184` | started/unknown-start leg ⇒ decline |
| leg-series allowlist + static prod guard | `config.py:2041-2071` | MLB+WC; prod needs `--confirm-live` + limits + non-empty whitelist |
| reservation (QUOTE mode) | `reservation.py:163` | atomic, CAN deny via an enforced legacy cap (its own comment saying "denial impossible in shadow" is **wrong** — it's *more* live than it claims) |
| 3-4 of 7 circuit breakers (paper/quote only) | `quote_app.py:656` | data-stale, latency, 429-burst, + coordinator error-trap |

These are basically the controls that existed **before** this build (game-key
aggregation is Phase 0). The one pleasant surprise: the reservation is genuinely
in the confirm path and *can* deny via the enforced caps.

## 2. SHADOW (computed + polled + logged, ZERO behavior) — a flag flip away

- **The entire R2 %-of-bankroll cap layer** (`limits.py:392-605`): utilization 3×,
  game 8%, per-combo 1%, directional 10%, slate 8%, soft-daily 6%, drawdown 10%,
  **hard-trip 12% KILL** — all stamped `shadow=caps_shadow_mode` (True by default,
  `config.py:1923`) and **dropped in `_partition_breaches`** (`lifecycle.py:247`).
- `risk_bankroll_cc = min(SOD, cash+0.5·PV)` is polled live and wired into
  `check()`/`try_reserve`, but its **only consumer is the shadow layer.** No
  enforced cap scales from the live bankroll.
- The **peak-equity feed is real and live** (`_halt_inputs` passes a genuine peak
  into every `check()`), and `maintenance_tick` already routes *enforced*
  `HALT_DRAWDOWN`/`HALT_HARD_TRIP` to `killswitch.halt` — so flipping
  `caps_shadow_mode=false` arms a **human-only-clear 12% KILL immediately, no
  further wiring.** (Validate thresholds first.)
- **Inventory skew** (`skew.py`): computed every quote, `enabled=False` ⇒
  `applied_cc` returns hard 0 ⇒ quote bit-identical. Sign now verified correct.
- **Widen-vs-decline**, **pregame M_q/M_c margins** (default 0.0): computed/logged,
  zero behavior.

## 3. BUILT but NOT WIRED (needs real code, NOT a flag)

| Dead component | Code | Why it never runs |
|---|---|---|
| **Realized-P&L ledger** | `balance.py:445` `apply_settlement` | **zero callers in `src/`** (grep-verified) — realized P&L stays 0 forever |
| **Second P&L store** | `lifecycle.py:662` `record_realized_pnl` | **zero callers** — `daily_pnl.realized_cc` always 0 |
| **Settlement handler** | `lifecycle.py:666` `reconcile_combo_settlement` | explicit TODO seam; **nothing in `src/` constructs a `Settlement`** — no live cash/fee/sign reconciliation at all |
| **Portfolio-CVaR cap** | `limits.py:583` `SKIP_PORTFOLIO_CVAR` | **doubly dead**: no live `check()` passes `book_risk=`, AND it's shadow. Flag flip alone won't arm it. |
| **Fill-velocity caps** | `limits.py:141`, config | fields exist, `HALT_FILL_VELOCITY` exists, but **no code computes velocity** — dead even when enforced |
| **reservation.reconcile(real positions)** | `quote_app.py:532` | only caller passes `set()` — a confirm-timeout `mark_unconfirmed` reservation is **never reconciled → leaks headroom until restart** |
| **External SafetySupervisor** | `supervisor.py:175` | standalone CLI; **no bot code launches it**; absent `KALSHI_SUPERVISOR_*` it runs KILL-only with **no cancel path** |
| **In-process restart block** | `heartbeat.py` marker | the bot **never** calls `_reconcile_marker.set()` on an in-process halt — only the (unlaunched) supervisor does. A restart after a breaker/daily-loss trip **silently resumes**. |
| **3 of 7 breakers** | `breakers.py` | `_sample_breaker_inputs` leaves marginals/game_keys/tripwire/changed_markets empty ⇒ marginal-jump, unmapped-game, metadata-change **never fire** |
| **MC with real correlations** | `report.py` → `book_risk` | live MC runs only in the 300s log loop, passes **no `within_game_rho`** ⇒ uses the flat `DEFAULT_FLAT_BAND (-0.20,0.10,0.40)`, **not the pricer's per-pair rhos**; no bankroll ⇒ empty ruin thresholds. Pricer-parity holds only in the test harness. |

## 4. Discrepancies — where the code does NOT match the claims

1. **"LIVE-VERIFIED sell-only book / `combo_no_pays_complement` promoted from a
   real $1.00 settlement"** (CLAUDE.md): the settlement-booking path that exercises
   that convention (`apply_settlement`/`record_realized_pnl`/`reconcile_combo_settlement`)
   is wired into **no live handler** — the convention gate is only hit in tests.
   The one-off manual demo may be real; **no live code path exercises it.**
2. **"the number the halts/limits consume"** (`book_risk.py`, `report.py`,
   `limits.py:128`): no live limit/halt consumes `compute_book_risk` — it's logged.
3. **"the risk sim shares the pricer's joint, parity-gated"**: true only for the
   block shape + NO sign-flip; the **rho magnitudes live are the flat default band**,
   not the pricer's rhos (the live caller injects none).
4. **"last line of defense" (supervisor)**: nothing auto-starts it; cancel path
   credential-gated OFF by default; the prod preflight's `external_kill_reachable`
   only checks **credential presence, not a running process** — a green preflight
   can coexist with zero supervisor running.
5. **"denial impossible while caps_shadow_mode is True"** (reservation/lifecycle
   comments): FALSE — enforced legacy caps still deny. (Code is *safer* than the
   comment.)
6. **In-process hard trip drops the reconcile marker** (heartbeat docstring):
   FALSE — only a supervisor kill does; a bare restart after an in-process halt
   resumes.
7. **Phase 5 "changes live quoting"**: under default config it adds only shadow
   logs + zero-valued margins; the only live gate in that area is the Phase-3
   pregame decline.
8. **Run mode**: nothing that constitutes "risk protection" runs in the shipped
   default `mode=observe` (that builds ObserveApp with zero Phase-6 wiring); a real
   quote needs `--mode quote`.

## 5. Must-be-true before a real quote (concrete, code-level)

1. **Flip `caps_shadow_mode=false`** — arms the R2 layer + the 12% hard-trip KILL.
   *Validate the thresholds first;* it's a human-only-clear kill.
2. **Wire a real settlement handler** that feeds `apply_settlement` /
   `record_realized_pnl` from an exchange settlement message, and **build
   `reconcile_combo_settlement`** so fills reconcile cash/fee/sign to the cent and
   **HALT on mismatch** (Quiet-failure defense #3 — today there is *no* live
   settlement reconciliation).
3. **Arm the portfolio-CVaR cap**: pass a `BookRiskSnapshot` into the live
   `check()` sites AND thread the pricer's `within_game_rho` + `bankroll_cc` into
   the report MC (else it runs on flat default rhos with empty ruin thresholds).
4. **Implement fill-velocity** (no code computes it) or stop advertising the cap.
5. **Make reconcile real**: a post-timeout/periodic call with the exchange's actual
   open positions, or confirm-timeout reservations leak headroom until restart.
6. **Auto-launch + credential the external supervisor** as a real separate process
   (preflight must check a *running* watcher, not credential presence).
7. **Drop the reconcile marker on in-process hard trips** so a restart can't
   silently resume.
8. **Feed the 3 dark breakers** real inputs; broaden the 429 window to
   create/delete/confirm calls.
9. **Confirm the live invocation is `--mode quote`** and that all of the above are
   done in that mode.

## NEXT STEPS
- **Owner: operator** — this is the honest map. Decide sequencing; none of it is a
  single flag. The shadow-first posture is *correct* (nothing should be live yet);
  the gap is that several items are unbuilt, not merely un-flipped.
- **Owner: eng** — the settlement→ledger→reconcile-or-halt pass is the biggest and
  most important missing piece (it also un-blocks the realized-P&L caps + the
  "LIVE-VERIFIED" claim). Then the MC-into-check wiring, the supervisor auto-launch,
  and the reconcile-with-real-positions loop.
- **Owner: docs** — correct the overstated liveness claims in CLAUDE.md + the
  Phase-4/6 reports + the code comments listed in §4.
