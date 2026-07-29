# The ceiling that shrank as we deployed: portfolio haircut becomes an operator anchor

**Date:** 2026-07-28 (ET)
**Commit:** `eb5bbd8`
**Scope / blast radius:** `ops/config.py` (new anchor), `ops/quote_app.py`
(wiring only), `tools/vitals/tape_facts.json` (refreshed facts).
**NOT touched:** pricing, markups, correlation, any cap fraction, any gate.

This one came directly out of an operator observation that turned out to be
exactly right:

> "2664 × .36 gives us 959 of headroom, so idk why we didn't quote more from
> the start. I feel like it's counting settled positions or something. **I really
> feel like we're missing out or quoting less than we should be.**"

He was right, and the mechanism was worse than a miscount: it was a **negative
feedback loop that tightened the book precisely as the book was working.**

---

## 1. The defect

`risk/balance.py` computed the risk bankroll as:

```python
risk_bankroll_cc = min(start_of_day_equity, available_cash + haircut * portfolio_value)

DEFAULT_PORTFOLIO_HAIRCUT = Fraction(1, 2)   # line 83
# "FLAGGED for operator to set per risk tolerance"
```

That flag had been sitting at one half — never set by an operator, never
ratified, never reviewed. It is exactly the species the NORTH STAR names:

> **A number a human has to move by hand is a bug in the adaptation, not a knob
> to tune.**

## 2. Why a haircut below 1.0 is a feedback loop, not conservatism

Deploying capital moves value from `available_cash` into `portfolio_value`.
With `haircut = h`, the marginal effect of deploying one dollar on our own
ceiling is:

```
d(risk_bankroll)/d(deployed) = -1 + h
```

At `h = 0.5` every dollar we put to work **destroyed fifty cents of our own
capacity to work.** Measured on the live book: roughly **−$18 of ceiling per
$100 deployed** at the realised marks.

The consequence is a plateau, and it is the plateau the operator kept hitting
and describing from the outside:

> "$800 seems to be our max"
> "we're getting stuck at this $500-700 amount"
> "We are in this sort of loop where we can't quote more"

There was no escape *by construction*. Quoting more shrank the ceiling; the
ceiling shrinking refused the next quote. The bot was not being conservative —
it was chasing its own tail, and the harder it worked the tighter it got.

At `h = 1.0`, `available_cash + 1.0 * portfolio_value` **is** total equity, so
`d(risk_bankroll)/d(deployed) = 0`. Deployment becomes value-neutral and the
loop is neutralised. The `min(start_of_day_equity, ...)` term still holds the
ceiling to the day's opening equity, so intraday marks cannot inflate it.

## 3. What shipped

- `portfolio_haircut` promoted to an explicit operator anchor in `ops/config.py`,
  wired through to `BalanceTracker` in `ops/quote_app.py`.
- Operator ruling, given directly:

  > **"Haircut should be 1.0. We always use full equity."**

- Set to `1.0` in the live config. The anchor is now stated once, in the
  constitution layer, rather than hiding as an unreviewed default.

## 4. Honest note on what this is

This is a **loosening**. It is the one change in the 7/27–7/28 batch that
genuinely increases what we can lose, and it should be read that way rather
than as another accounting correction:

- Ceiling stops shrinking under deployment — that is the *intent*.
- Downside protection now rests entirely on `min(start_of_day_equity, ...)`
  plus det-max plus the tail gate. The haircut is no longer doing hidden work.
- The operator ratified it explicitly and in full knowledge that it raises the
  ceiling.

## 5. Verification

- `tools/vitals/gate` 8/8 GREEN, total 13.8s.
- Vitals `tape_facts.json` refreshed. **Known weakness recorded:** the gate
  derives its bankroll from a `daily_ruin_anchors` table that was 12 days stale
  ($2,050 vs live $2,661 at the time). Tracked, not yet fixed.

---

## NEXT STEPS

- **Runs next:** live confirmation that the ceiling holds steady through a
  deploy cycle instead of ratcheting down.
- **Owner:** me — sample `risk_bankroll_cc` across a fill sequence and confirm
  the marginal effect is 0, not −0.18.
- **Decisions owed by operator:** none — `1.0` is ratified. Revisit only if the
  drawdown behaviour argues for it, and never from a P&L window.
- **Open debt (tracked, not fixed here):** vitals bankroll reads a stale
  `daily_ruin_anchors` table; boot rehydrate reads positions UNPAGED and
  truncates past ~100 positions; ledger orphan defect leaves $775.85 of cost
  basis unbooked.
