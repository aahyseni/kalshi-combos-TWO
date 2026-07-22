# 2026-07-22 — Scalar/DNP receivable coverage hole (AS4): MLB gating decision

**Scope of this report (blast radius):** READ-ONLY analysis + a written decision.
No live module, config, or test was modified. The mechanism discussed is the
give-back drawdown/KILL layer in `risk/limits.py` fed by the settlement-receivable
shield in `risk/balance.py` and `rfq/lifecycle.py`; the scalar/DNP gap lives in
`marketdata/settled.py`. Nothing here touches the pricing/quoting path.

**Decision (one line):** **(a) ACCEPT-AS-IS for the initial armed MLB run**, gated
on one named automatic MONITOR (a pre-existing `settled_scalar_unresolvable` WARN
promoted to a first-class alarm), plus one confirmation owed from the settlement-rules
agent. Rationale, failure-mode analysis, and the exact monitor spec below.

---

## 1. The mechanism (what the cascade shield protects against)

The give-back halts (`risk/limits.py` block 7) fire on
`give_back_cc = max(0, peak_equity − current_equity − pending_receivables)`, at
**10% of bankroll → HALT_DRAWDOWN** and **12% → HALT_HARD_TRIP (a KILL, human-only
clear)**. Equity is read from the exchange (`portfolio_value`). The problem they
solve: during a **staggered settlement cascade** the exchange removes a settled
position's mark-to-market value from `portfolio_value` **before** the winning cash
lands in `balance` (the two are not atomic — proven by the 2026-07-19 incident where
a $430.69 equity trough had real losers of only $29.51). That trough is a bookkeeping
artifact, not a trading loss, but the raw `peak − current` reads it as a give-back and
false-KILLs the bot mid-slate.

The **receivable / cascade shield** (`_refresh_settlement_receivables` in
`rfq/lifecycle.py`, `note_receivable`/`pending_receivables_cc` in `risk/balance.py`)
closes that gap: for every held position **all of whose legs carry an exchange-GRADED
0/1 fact** (from the `SettledMarginalResolver` permanent cache), it predicts the gross
settlement credit (LONG NO pays `1−V`, LONG YES pays `V`) and subtracts that sum from
the measured give-back — floored at 0, with raw peak/current left untouched so the
shield can never inflate a peak. The shield lifts the instant the cash is provably in
equity (a balance poll whose request started after the reconciler confirmed the row),
with a 30-min TTL backstop and a to-the-cent `HALT_RECONCILIATION_MISMATCH` catching
any wrong prediction. Winners shield the trough; **losers note nothing**, so a genuine
loss cascade still measures in full. It is fail-closed by construction: a receivable
only ever *reduces* a give-back.

**Why scalar/DNP settlements fall outside it.** The shield's inputs are *facts only*,
and the fact resolver (`marketdata/settled.py`) caches **only binary 0/1 outcomes**: a
`result == "scalar"` payload is dropped into `_unresolvable` and **never yields a
marginal** (lines 287–293), by explicit fail-closed design — there is no phony scalar
prediction because "fair market price at freeze" is undefined in Kalshi's rules
(§7 flag (a) of `docs/dnp_scalar_settlement.md`, unverified for MLB too). Consequently
`_refresh_settlement_receivables` hits `fact is None` on that leg, sets
`unresolved = True`, `break`s, and notes **no receivable for the whole combo**
(lines 4082–4088). So a combo that settles through a rain-shortened / suspended /
DNP (scalar `V ∈ (0,1)`) MLB leg produces the same equity trough during its cascade but
gets **no shield** — exactly the false-KILL class the shield was built to prevent,
re-opened for the scalar surface. This is AS4 in `NOTES.md`.

## 2. Failure mode if we arm MLB with the hole open

It is a **false human-only halt (HALT_DRAWDOWN at 10%, or the HALT_HARD_TRIP KILL at
12%) during a rain-shortened / suspended / DNP settlement cascade — NOT a
mis-shielded dollar and NOT a trading loss.** The hole is strictly on the *shield*
input, which can only ever *reduce* a give-back; with no receivable the measurement
reverts to raw `peak − current`, which is the pre-shield fail-closed behavior. So:

- **Direction is safe.** UNKNOWN → no receivable → halt-toward, never a convenient
  default. No phony scalar `V` is ever predicted (which would be the dangerous
  inversion). This is consistent with the standing rules: fail-closed pairs with
  fact-resolution, and the one fact we cannot resolve (scalar freeze price) correctly
  reads as "no shield," not "assume paid."
- **The cost is availability, not money.** On a slate where a rain-short/DNP combo
  settles, the bot can trip a **human-only KILL mid-slate** on a *bookkeeping* trough,
  stranding open quotes/positions until a human clears it. The give-back is a false
  positive; the P&L is fine; but the desk is now down and needs manual intervention —
  which itself sits uneasily against the NO-MANUAL-RISK-INTERVENTION rule (the halt is
  automatic and correct-by-fail-closed, but the *clear* is manual, and the trough that
  triggered it is phantom).

**Trough magnitude (worst case).** The shielded quantity equals the winning gross
credit of the cascading positions, i.e. `Σ contracts·payout` for the KNOWN winners in
that cascade — the same figure that was $430.69 on 2026-07-19. Against the current
`~$2,384.77` equity basis (all-time reconciled; `$2,000` research START basis), the
drawdown fires at `~$238` (research: `$200`) and the KILL at `~$286` (research: `$240`).
A single mid-size settlement cascade — the 2026-07-19 event was already $430 of trough,
comfortably over both thresholds — is enough to trip the KILL **if a scalar/DNP leg
sits anywhere in the cascading set and blocks the shield for those positions.** Note the
break is *per position*: only combos that actually contain an unresolved (scalar) leg
lose their shield; binary-only combos settling in the same cascade are still shielded.
So the un-shielded trough is the winning credit of the **scalar-touching** subset of the
cascade — smaller than the whole cascade, but on a rainout that freezes *all* same-game
legs at once (§7.1 point 1), an entire same-game combo cluster can be scalar-touching
simultaneously.

**Frequency.** From `docs/dnp_scalar_settlement.md` §7.1 (MLB settlement audit):
**~1–2% of MLB game-days** hit the postponement/suspension path (48-hour rain rule
scalar-settles *every* MLB family, not just props); shortened-game totals add
~0.3–0.5% of games; MLB prop DNP is *stricter* than soccer (needs a START + ≥1 PA / 1
pitch — bullpen games, openers, bench days, started-with-0-PA, pinch-hit/relief all
scalar-settle). Baseball reality supports this: rain/suspension is routine (double-headers,
tarp delays, curfews), and every quoted MLB slate is many games, so over a season the
probability that *some* armed slate carries a scalar settlement is high — the doc's own
words: **"expect `HALT_RECONCILIATION_MISMATCH` to fire on the first such settlement"**
(that's the reconcile guard; the give-back false-KILL is the *same trigger event* seen
by the shield layer). Contrast the soccer basis this was all decided on: **0-in-4,913**
combo markets. The frequency premise is genuinely different for MLB, which is precisely
why AS4 was flagged for this gate.

## 3. Decision: (a) ACCEPT-AS-IS with a named automatic monitor

I recommend **(a) — accept the hole for the initial armed MLB run, do NOT block, add
one automatic monitor** — over (b) build a mitigation first, or (c) hard blocker.

**Why not (c) BLOCKER.** The failure is fail-closed and non-lossy (a halt/KILL, never
a mis-payment). Blocking MLB arming on an availability edge case that costs a manual
clear — not money — is disproportionate, especially for an *initial* run that is being
watched. The pricing/settlement math already handles fractional `V` correctly
(the reconcile guard at `lifecycle.py:3843–3853` explicitly tolerates the sub-cent
scalar residual), so we are not exposed to a *pricing* error on scalar — only to a
*monitoring* false-positive.

**Why not (b) build-first, yet.** A "proper" mitigation would be to let the shield
carry scalar positions using a **fail-closed conservative `V`** — but the only
fail-closed conservative choice is the one that maximizes the shielded (predicted)
credit, i.e. assume the *best* payout for the side we hold (LONG NO ⇒ assume `V=0`
⇒ full `$1` credit; LONG YES ⇒ assume `V=1`). That is a *larger* shield than reality,
which **violates the shield's own fail-closed contract** (a shield must never
*over*-reduce a give-back, or it can mask a real loss cascade). The genuinely
fail-closed scalar behavior is exactly what the code does today: **no shield**. So the
"small mitigation" is not free — it trades an availability false-positive for a
correctness risk on the loss-detection side, which is the wrong trade under
NO-MANUAL-RISK-INTERVENTION. The clean automatic fix (resolve the real freeze `V` from
the exchange `settlement_value_dollars` once the row is graded, and note the receivable
from *that*) is viable but is a **build**, not a config change, and should be scoped
into readiness item 4/5 rather than gate the first run. It is the right *eventual*
mechanism; it is not required to arm.

**What (a) requires — the monitor (exact spec).** The event is already emitted; it is
not yet an alarm. Promote it:

| Field | Value |
|---|---|
| **Signal** | `marketdata/settled.py` already logs `settled_scalar_unresolvable` (WARN) at line 290 the instant a held leg grades `scalar`, and `_refresh_settlement_receivables` silently `continue`s. |
| **Monitor** | Raise `settled_scalar_unresolvable` to a **first-class alarm** (same channel as the give-back/halt alarms) that pages the operator **on first occurrence in an armed run**, carrying: the leg ticker, its `combo_ticker`(s), and the held `contracts`/`our_side` of every position whose shield is thereby suppressed. |
| **Pair with** | A give-back-layer annotation: when `HALT_DRAWDOWN`/`HALT_HARD_TRIP` fires, include in the breach detail whether any on-book position is currently **scalar-suppressed** (i.e. has a graded-`scalar` leg). This lets the operator instantly distinguish a *real* give-back from a *scalar-trough false-positive* at clear-time. |
| **Owner** | bot session, as part of readiness item wiring — a logging/alarm change only, **zero blast radius on pricing/quoting** (fix-isolation rule). |
| **Fail-closed?** | Yes — it changes no risk number and adds no shield; it only makes the existing fail-closed no-shield behavior *observable* so a manual clear is fast and correctly attributed. |

This keeps us honest against the standing rules: no manual risk numbers, no fail-safe
inversion, and the one UNKNOWN (scalar freeze price) still widens/halts rather than
defaulting to "paid."

## 4. Settlement facts I need confirmed (coordination note)

A separate agent is verifying the **KXMLBOUTS / RBI / SB** series settlement rules via
the Kalshi API. For *this* decision the load-bearing facts I need back are:

1. **Does the `scalar` `result` enum actually appear on these prop series at
   settlement, or do they void/refund instead?** `settled.py` treats `result=="scalar"`
   as the trigger; if a rain-short MLB market instead settles with an empty `result`
   under a non-graded status, it stays UNKNOWN on a different path (still no shield, still
   fail-closed — but the *monitor signal* `settled_scalar_unresolvable` would NOT fire,
   so the alarm must ALSO cover "held leg on a `closed`/postponed market past its expected
   settlement window").
2. **Is `settlement_value_dollars` populated on a scalar MLB settlement** (the value that
   a future proper mitigation would read to compute the true receivable)? The
   `_settlement_value_consistent` cross-check currently only validates it for binary rows.
3. **The STRICT prop-DNP definition** (START + ≥1 PA / 1 pitch; pinch-hit/relief excluded)
   — confirm it holds for OUTS/RBI/SB specifically, since those drive how often a
   *player-prop* combo (not just a rainout) hits the scalar path.

None of these change the decision (accept + monitor); they refine the monitor's coverage
and pre-scope the eventual `settlement_value_dollars`-based mitigation.

## 5. Summary

| Item | Finding |
|---|---|
| Mechanism | Give-back halts subtract KNOWN-winner receivables from `peak−current` to ignore the settlement-cascade equity trough; shield lifts when cash provably lands. |
| The hole (AS4) | Fact resolver caches only binary 0/1; `scalar` → `_unresolvable` → no receivable → whole combo un-shielded during a rain-short/DNP cascade. |
| Failure mode | False human-only HALT_DRAWDOWN (~$238) / HALT_HARD_TRIP KILL (~$286) on a phantom trough. Fail-closed, non-lossy. Availability cost, not money. |
| Frequency | ~1–2% of MLB game-days (rain/suspension) + prop-DNP surface; vs soccer's 0-in-4,913. Real for MLB. |
| Decision | **(a) ACCEPT-AS-IS + automatic monitor.** Not a blocker (fail-closed, non-lossy); not build-first (the only config-level scalar shield would violate the shield's fail-closed contract). |
| Monitor | Promote `settled_scalar_unresolvable` to a first-class alarm + annotate give-back breaches with scalar-suppression state. Logging-only, zero pricing blast radius. |

---

## NEXT STEPS

- **Build the monitor (owner: bot session, before the first armed MLB slate):** promote
  `settled_scalar_unresolvable` to a paging alarm carrying combo_ticker + suppressed
  positions; annotate `HALT_DRAWDOWN`/`HALT_HARD_TRIP` breach detail with whether any
  on-book position is scalar-suppressed. Logging/alarm only — fix-isolation confirmed,
  no pricing/quoting change, add a regression test that the alarm fires when a held
  leg grades `scalar`.
- **Confirm from the settlement-rules agent (owner: that agent):** the three facts in §4
  — does MLB return `result="scalar"` (vs void/empty-under-postponed), is
  `settlement_value_dollars` populated on scalar, and the strict OUTS/RBI/SB DNP
  definition. Feed the answers back here; if scalar arrives as empty-`result`-under-
  postponed instead, widen the monitor to cover held legs on postponed/`closed` markets
  past their expected settlement window.
- **Scope the eventual real fix into readiness item 4/5 (owner: bot session, NOT before
  first run):** once a scalar row is graded, read the exchange `settlement_value_dollars`
  to compute the *true* receivable and note it (fail-closed: only from the graded row,
  never a predicted freeze price). This closes AS4 mechanically and removes the manual
  clear entirely — but it is a build, not an arming gate.
- **Operator decision owed:** confirm (a) ACCEPT-AS-IS + monitor is acceptable for the
  initial armed MLB run, or escalate to build-first if a mid-slate manual clear is
  unacceptable even once. Also re-affirm the REACTIVE stance on fractional-settlement
  reconciliation under the ~1–2% MLB frequency (the standing re-affirmation flagged in
  `docs/dnp_scalar_settlement.md` NEXT STEPS, 2026-07-10 — same event class).
