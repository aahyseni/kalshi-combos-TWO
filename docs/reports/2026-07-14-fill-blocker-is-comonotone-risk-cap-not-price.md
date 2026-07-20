# The ENGARG fill blocker is the COMONOTONE risk cap, not price

**Date:** 2026-07-14/15 · **Area:** risk gate (last-look) — why we win auctions but don't fill
**Follows:** `2026-07-14-market-vs-our-pricing-main-combos.md` (operator pushback: "we can't be
a sharp maker — we've barely filled ENGARG; if within 1¢ we'd get something").

---

## TL;DR

The operator was right that we barely fill ENGARG — but not because we're mispriced. On ENGARG
we **won 108 auctions** (you cannot win an auction if your ask is too high) and **declined 103**
of them at last-look. The current blocker is `decline_risk_limit`: the **per-game loss cap**.

The game cap is computed as a **comonotone sum** — `Σ max_loss over every position touching the
game`, i.e. "every combo on this game loses simultaneously" (`risk/exposure.py:275`,
`worst_case_loss_by_game_cc`). That scenario is **impossible**: ARG-advance and ENG-advance combos
are mutually exclusive (exactly one team advances), so they cannot all lose together. The MC
portfolio-risk engine that *would* net this exists and is wired — but it is **dominated** (the
comonotone sum ≥ the true joint tail, and its threshold is *lower*: 8% game vs 15% CVaR, so it
always bites first) and in the current run (ph18) it emits **0 snapshots** (it only sees committed
positions; the committed book is ~empty post-restart, so it never even evaluates).

So the sophisticated, correlation-aware risk layer we built is running but never governs; a blunt
"everything loses at once" sum vetoes the fills.

## The ENGARG funnel (our own live data)

```
  764,815 RFQs seen ─► 17,288 quotes sent ─► 108 AUCTIONS WON ─► 5 FILLED
                                                   └► 103 DECLINED at last-look
                                                       ├─ 60  decline_size_unknown  (OLD fill-killer, all pre-fix)
                                                       └─ 79  decline_risk_limit     (ALL recent — the current blocker)
```

`decline_risk_limit` detail (verbatim): `game 26JUL15ENGARG loss 1,516,860cc > 2/25 bankroll =
1,473,476cc; slate 2026-07-15 loss 1,516,860cc > 2/25 bankroll`. The loss **fluctuates**
($151–176) rather than growing monotonically ⇒ it is dominated by the **mass-acceptance worst case
of our ~20 outstanding quotes** (all accepted at once), not by real committed positions. It hovers
just over the $147 cap, so essentially every accept is declined.

## Correction to the prior read — we ARE one-sided on ENGARG

Operator's guess was "we're not very one-sided." The data says otherwise. Reconstructing the 108
won auctions as the book we'd hold if uncapped, split by advance-anchor (our NO max-loss):

| advance anchor (combo loses only if…) | accepts | our NO max-loss |
|---|--:|--:|
| ARG advances (contains ARG-adv leg) | 68 | **$515** |
| ENG advances (contains ENG-adv leg) | 20 | **$163** |
| either (no advance leg — BTTS/total/corners) | 27 | $262 |

```
  COMONOTONE sum  (what the 8% game cap uses):              $939
  ADVANCE-AWARE worst case max(ARG+either, ENG+either):    $777   (true MC tail is lower still)
  → comonotone overstates by 1.21x TODAY  (only, because we're 3:1 ARG-skewed)
```

We're ~**3:1 ARG-anchored** ($515 vs $163). So the mutual-exclusion hedge only cuts the worst case
~21% right now — *because we're one-sided, not balanced.* This makes the operator's strategic point
**stronger**, not weaker:

- The comonotone cap blocks a **risk-reducing** ENG-side fill exactly as hard as a **risk-adding**
  ARG-side fill — it just sums max-losses, blind to direction.
- A marginal-risk / MC gate would do the opposite: **welcome** the ENG-side fill (it lowers the
  joint tail by hedging the ARG concentration) and **gate** further ARG-side concentration sooner.
- That is exactly "balance risk and reward; if it's +EV and doesn't blow the tail budget, take it."

## Honest caveat (why a cap must still exist)

The full 108-accept book's worst case is ~$777–939 = **42–51% of a ~$1,842 bankroll**, concentrated
in "ARG advances + high scoring." That is real, correlated tail risk. So the fix is **not** to remove
the cap — it's to make the gate **marginal-risk-aware** (take hedging fills, gate concentrating ones)
and set its level from the **MC joint-tail budget**, not a provably-impossible comonotone sum.

## The fix (design — needs operator sign-off; touches the pristine risk engine, hard rule 8)

1. **Make the last-look gate marginal-CVaR + EV, not a comonotone veto.** On each accept: is it +EV
   (yes by construction — we only quote +EV) AND does post-fill portfolio `operative_es_99` stay
   ≤ the tail budget? Take it iff both. This is the machinery we already built (`sim/book_risk.py`,
   `compute_book_risk`) — it just needs to be the GOVERNING gate, not a dominated add-on.
2. **Demote the comonotone game/slate sum to a loose fail-safe backstop** (raise its level so it
   stops binding before the MC), or replace it with a **mutual-exclusion-aware** analytic
   (partition a game's positions by the mutually-exclusive advance outcome, take max over branches)
   — a robust, fail-safe middle path that needs no live MC.
3. **Make the MC actually govern the right book:** rehydrate committed positions across restarts
   (task #33 — ph18 starts empty, so the MC sees nothing), and feed the outstanding-quote /
   mass-acceptance scenario through the same joint model instead of a comonotone sum.

## NEXT STEPS

- **Owner: operator** — Decision: (A) build the marginal-CVaR + EV last-look gate (bigger change,
  the real fix), or (B) interim — mutual-exclusion-aware analytic game cap + raise the level, and
  ship (A) after. Either way, prototype-in-test → port → parity-check (hard rule 8) on the pristine
  risk engine.
- **Owner: next agent** — Fix #33 (position rehydration on restart) — prerequisite for the MC to see
  the real book; today it's blind post-restart.
- **Owner: next agent** — Verify the true MC tail vs the comonotone sum on a reconstructed ENGARG
  book with live marginals (this report used an advance-aware analytic bound; the MC number is lower).
- Related: #38 (auction liveness). The 5 fills prove the accept→confirm→fill path works when the cap
  doesn't veto; the cap is now the dominant fill limiter, ahead of latency/TTL.
