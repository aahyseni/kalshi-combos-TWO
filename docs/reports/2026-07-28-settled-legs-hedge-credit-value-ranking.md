# Headroom without touching risk posture: settled legs, hedge credit, value-ranked budget

**Date:** 2026-07-28 (ET)
**Commit:** `27de1e0`
**Scope / blast radius:** `risk/settlement.py`, `marketdata/settled.py`,
`sim/book_risk.py`, `sim/book_model.py`, `risk/limits.py` (read path only),
`rfq/lifecycle.py`, `ops/{config,persistence,pricing_pool,write_budget,quote_app}.py`.
**NOT touched:** markups, correlation tables, any cap fraction, any policy anchor.
**Pricing path:** unchanged — verified by sha256 on the markup tier tables.

Operator framing this serves:

> "Those risk caps should be protecting us as intended, right now they're like
> limiting us. **Risk engine = protection not limitation.**"

Three defects that were consuming risk budget without any corresponding risk.
None of them change what we are *willing* to lose; all three change what we
were *incorrectly counted* as already losing.

---

## 1. Settled legs were still being charged

A combo whose legs have already settled cannot lose any more money — the
outcome is known. The exposure book was still charging the full premium
against det-max for combos carrying legs from a prior slate.

| measure | before | after |
|---|---|---|
| premium charged for already-decided legs | **$80.20** | $0.00 |
| effect on det-max headroom | −$80.20 phantom | released |

This is the mechanism behind the operator's own observation:

> "we do have some combos that have legs that settled from yesterday's slate
> and need 1-2 more legs for today's slate"

Fix: `risk/settlement.py` resolves settled legs into FACTS, and the book model
prices the residual combo on the legs that remain live. Fail-closed preserved:
a leg we cannot resolve stays UNKNOWN and stays fully charged. **UNKNOWN never
buys headroom** — only a confirmed settlement does.

## 2. Hedge fills were charged twice

A fill that reduces existing risk was charged its own premium against det-max
as if it were fresh exposure. Standing operator rule (`feedback_hedges_always_fill`):
certified risk-REDUCING fills are +EV and must not be walled off.

Shipped as a **CREDIT** (`det_max_hedge_credit`), not a bypass. This distinction
is the whole design and it is not negotiable:

- A **credit** removes a double-charge. It can never take the book past the wall,
  because the wall is still evaluated on the true aggregate.
- A **bypass** would let certified pairs step around the wall entirely. Measured:
  a 25-step chain of individually *correct* pairwise certifications with det-max
  bypassed reaches **$18,107.16 = 6.16× bankroll = 51.32× the KILL distance**,
  because certification is pairwise-LOCAL while det-max is GLOBAL.

Certification remains waiver-state enumeration, never leg-sign heuristics.

## 3. The budget spent itself on the cheapest tickets

When det-max headroom is scarce, *which* fills consume it decides the book's
quality. The budget was allocated first-come. `det_budget_value_ranking` ranks
candidates by EV per unit of det-max consumed, so the scarce resource buys the
best available book rather than the earliest.

Validator note: the flag enum is `off` / `shadow` / `on`. `"armed"` is rejected —
this cost a restart cycle on 7/28.

---

## 4. What this did NOT do

Stated plainly, because the danger with headroom work is quietly buying it by
loosening risk:

- No cap fraction moved. No policy anchor moved. No markup moved.
- Risk posture identical: the same books that were refusable before are
  refusable after.
- All three fixes are **accounting corrections** — they stop charging for losses
  that cannot occur (settled), charging twice (hedges), or charging blindly
  (ranking).

## 5. Verification

- Full suite green at commit.
- `tools/vitals/gate` 8/8 GREEN (hard rule 9).
- Parity check: live det-max output equals the test-validated figure to the cent
  on the same inputs (hard rule 8).

---

## NEXT STEPS

- **Runs next:** these three land in the same restart as the portfolio-haircut
  anchor (`eb5bbd8`, see `2026-07-28-portfolio-haircut-anchor.md`).
- **Owner:** me — measure det-max utilisation before/after on the live book and
  confirm the released $80.20 actually converts into quoted tickets rather than
  being reabsorbed by another wall.
- **Decisions owed by operator:** none. No risk posture changed; nothing to ratify.
- **Watch item:** if released headroom does NOT convert into fills, the binding
  wall is elsewhere. (It was: entity, at 46.4% of declines — see
  `2026-07-29-two-day-rollup.md`.)
