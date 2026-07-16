# 2026-07-16 — Problem A: last-look state-consistent worst-case waiver — BUILT + adversarially reviewed

The handoff §4A durable fix, built end-to-end today across two workflow runs (the original
`wf_10ee06f2` in session f1108196 — killed mid-Wire at 15:48 UTC when the operator accidentally
closed all sessions — and the continuation `wf_a220ac69` which finished the wire, ran the 3
adversarial verify lenses, and fixed the confirmed findings).

## What it does

When a confirm is denied by the reservation check and **every** enforced breach is a waivable
game-loss / mutex-directional cap breach (`WAIVABLE_RESERVATION_BREACHES = {SKIP_GAME_LOSS_CAP,
SKIP_DIRECTIONAL_CAP}`), the waiver computes the **exact state-consistent per-game worst case** —
full Dixon-Coles scoreline-grid enumeration (n_states=1586 at default cfg, ET/pens
branch-expanded), no MC sampling — and if every breached game is **certified** with
`worst_case_cc ≤ threshold_cc(game_loss_frac, bankroll)` (the SAME budget, never raised), retries
the reservation once with the certificates. Mutex netting is exact (opposing-advance NO parlays
never co-lose; the live under-4/4+ FRAENG pair from today's 16:43/16:44 fills stops being
double-counted). Everything else fails closed to today's `DECLINE_RISK_LIMIT`.

Safety architecture (all property-tested):
- **CONFIRM-PATH ONLY** — quote-time analytic caps untouched (E2 mass-acceptance dominance);
  verified: the 3 quote-time/maintenance `LimitChecker.check` call sites pass no `waived_games`.
- Open quotes clamp `max(0, loss)` per state — an unfilled resting hedge never earns credit.
- Committed positions net fully; **outstanding reservations ride `earns_credit=False`** (hit-side
  sums, miss-side credit clamped — post-review fix).
- Non-structural / cross-game / unparseable legs resolve adversarially; uncertifiable game ⇒
  decline. Off-loop via `BookRiskPool.run_state_worst_case`, deadline
  `lastlook_mc_waiver_deadline_s` (default 1.0s, validated (0,3]).
- P0-2 atomicity: inputs stamped with **full `ExposureBook.generation`** (post-review fix — was
  `position_generation`, blind to quote churn) + reservation version; moved ⇒ rebuild once ⇒
  fail-closed.
- Config `lastlook_mc_waiver_enabled` committed default **OFF**.

One deliberate addition beyond the original brief (reviewed, kept): the advisory **last-look**
check declines on the same caps *before* the reservation runs — today's live self-declines fired
on that path — so when the waiver is armed + a reservation service is wired + every enforced
last-look breach is waivable, those breaches **defer** to the authoritative reservation
deny-site (a strict superset check) which triggers the waiver. Disabled ⇒ byte-identical
(regression-tested); any non-waivable breach still declines immediately.

## Adversarial review (3 lenses: E2/monotonicity, fail-closed, accounting)

15 findings, **3 serious — all confirmed real and FIXED with regressions**:

| finding | fix |
|---|---|
| Certificate staleness stamp used `position_generation`, which quote upsert/remove never bumps → a quote placed during the off-loop enumeration was invisible; stale certificate honored (2 findings, same root) | Stamp/compare full `ExposureBook.generation` (subsumes position mutations); `StateWorstCaseInputs.input_generation` → `book_generation` |
| Outstanding reservations netted fully as entities → an unconfirmed concurrent reservation could supply the hedge credit that certifies the candidate, then be released | `earns_credit=False` for non-candidate reservations: hit-side loss still sums (assume-committed conservatism), miss-side credit clamped |

12 non-serious findings retained as known items — the two MEDIUMs that matter operationally:
1. **Slate co-breach makes the game-loss arm dead code when `slate_loss_frac ≤ game_loss_frac`**:
   the slate cap sums the ANALYTIC per-game losses and is not waivable, and on a single-game day
   slate == game, so both breach together and the non-waivable slate breach vetoes the waiver.
   Our armed config has both at 0.30 → **arming requires raising `slate_loss_frac` above
   `game_loss_frac`** (both remaining WC days are single-game days).
2. Deferral counts never-confirmable accepts into the fill-velocity window (repeated waiver
   declines could trip it) — watch `lastlook_waiver.deferred_to_reservation` vs `granted` live.

Also noted: waiver 1.0s + candidate gate 2.0s deadlines sum to exactly the 3s confirm window —
recommend `candidate_gate_deadline_s: 1.5` when arming.

## Verification

- Continuation agents: full suite **2139/0** after fixes (2101 baseline + 28 waiver tests + 10
  fix regressions), ruff + mypy strict clean on touched files.
- My independent spot-checks: config keys + validator present; generation stamp at
  `lifecycle.py:1494` reads `exposure.generation`; `earns_credit` clamp at
  `state_worst_case.py:535`; no quote-time `waived_games`; deferral block guards verified.
- My independent full-suite re-run: **2139 passed / 0 failed, 107s** — agent claim confirmed.

Files: `sim/state_worst_case.py` (+tests, +proto), `rfq/lifecycle.py`, `ops/quote_app.py`,
`ops/config.py`, `ops/pricing_pool.py`, `risk/limits.py`, `risk/reservation.py`,
`tests/test_lastlook_mc_waiver.py`. Nothing committed yet; tree also carries the completed
fair-tiered-markup stream. No processes touched; bot still live on the 10:58 launch.

## NEXT STEPS

- **Operator (decision owed): restart go.** One restart picks up: Problem-A waiver code +
  fair-tiered markup + corners 4.5¢ floor. Arming the waiver additionally needs, in the local
  yaml: `lastlook_mc_waiver_enabled: true`, `slate_loss_frac` raised above `game_loss_frac`
  (dead-code MEDIUM above), and recommended `candidate_gate_deadline_s: 1.5`.
- **Me (on the go):** arm config → restart → live-verify (waiver metrics, confirm-window timing,
  0 heartbeat kills) → commit the whole tree as reviewed.
- **Deferred (post-restart, measured):** game cap raise decision — operator chose "waiver first,
  then re-measure" over raising `game_loss_frac` (0.30) today; revisit once the correctly-netted
  utilization is observable. Slate-uses-certified-worst-case is the durable fix for the slate
  MEDIUM if it binds in practice.
