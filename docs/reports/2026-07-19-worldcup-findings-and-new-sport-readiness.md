# 2026-07-19 — World Cup campaign findings + new-sport readiness (risk/MC)

## Campaign result (Fri 7/17 eve → Sun 7/19 final, ESPARG settled)

| | |
|---|---|
| Fills | 74 real (one phantom removed at settlement reconciliation) |
| Premium collected | ~$720 across both game days |
| ESPARG final outcome | 0-0, TIE, Spain champion via pens, no scorers — **71 winning combos +$263.60, 3 losers −$29.51, net +$234.09** on the final alone; FRAENG banked its FRA-win/Mbappé stacks Friday |
| Book P(profit) at kickoff | 71% (scenario-exact), P(ruin) 0 structurally |
| Worst realized drawdown | none — the one open loss branch (ARG champ ∧ Messi scores, ~29% market-implied, −$228) never happened |

The thesis validated: retail demand concentrates on narrative (47% of final-day RFQs
carried Messi-yes); a sell-only maker's edge is pricing that concentration and
capping it, not avoiding it.

## What we found (incident classes → fixes → standing rules)

1. **Comonotone det-max walled off diversity** → mutex-aware min(comono, state-exact)
   at both gates (`ade7b71`). Rule: caps must see mutual exclusivity.
2. **Waiver fingerprint churn** (51 declines/night) → trimmed-set fingerprint;
   later K=12→48 after proving the tail adder alone exceeded the game budget
   (zero grants for 30h). Adaptive-K queued.
3. **Exchange executes "cancelled" quotes** (taker-fee path, 16+ occurrences)
   → verify-before-discard vs `/portfolio/fills`, normal-writer replay, claim/
   order-id/min_ts guards (`e2e216a`+). Self-healed 16 fills in production.
   The mirror (executed-report, never executed) surfaced once at settlement
   (phantom row, −$6.39 predicted-vs-paid) — the same nets now cover both
   directions; the to-the-cent settlement HALT caught the pre-hardening row.
4. **Settled legs read as UNKNOWN** (post-game dark bot, 366k RFQs/0 quotes) →
   settled-fact marginals (graded results = 0/1, doc-verified statuses), batch
   registration, shared feed-readability predicate (husk books), breaker
   exemption for exchange-confirmed non-live markets (`a57afc3`…`c338281`).
5. **Peak concentration unpriced** (P(book) 52% coin-flip) → multi-cluster peak
   steer: cached loss clusters, widen stackers/rebate certified balancers,
   magnitude recalibration (size-independent), plateau-cap fix (131k states for
   halves grids). Live: ladder fills moved 76.6→75.4¢; 10/40 quotes rebated.
6. **Fair 2¢ under field on champ×scorer** → measured, decomposed (fee-print
   theory REFUTED 12/12 by ledger; field raw fills real), rho promote
   `advance|player_goal:same` 0.45→0.52 (ET-inclusive settlement argument;
   regulation 800-game measurement still valid for its own pair).
7. **Hand-tuned numbers drift** → auto-scaling delta caps (fracs of live
   bankroll) + the umbrella rule: NO manual risk intervention, ever
   ([[feedback_no_manual_risk_intervention]] in operator memory).
8. **Silence hides outages** (operator caught 3 before the monitors did) →
   quote-liveness alarm (zero-quotes-while-RFQs-flow / frozen log), pending-set
   visibility, broadened halt greps.
9. **In-play book drops trip the dead-feed breaker serially** (8 halts through
   the final) → known gap: committed legs on in-play games need breaker
   exemption (post-WC fix #1 below).
10. **Settlement-cascade equity trough** → the give-back breaker read settled-
    value-minus-unpaid-cash as a $430 drawdown (real losers: $29.51); kill was
    a false positive. Fix: settled-unpaid positions count as receivables in the
    equity mark (post-WC fix #2).

## New-sport readiness checklist (risk / MC), in build order

**Generalizes as-is (no per-sport work):** auto-scaling budget family, mutex
det-max, fill-recovery verification nets, settled-leg fact resolution, peak
steer (book-driven), velocity brake, monitors/alarms, reconciliation gates.

**Per-sport work required before arming (MLB/WNBA next):**
1. **Structural model per sport** = the DC-equivalent (MLB props engine + margin/
   total pricers exist and are gated; WNBA margin/total calibrated, needs odds
   source or shadow settlements). Certifiability depends on it (waiver, peak
   profile, mutex netting all consume structural plans).
2. **Pair-rho tables from measurement** — and the WC lesson: a rho measured on
   one settlement regime (regulation) must NOT be inherited by an ET/pens-
   inclusive variant. Audit every inherited value against its settlement window.
3. **Settlement conventions doc-verified per series** (rule 4) BEFORE the first
   quote: what settles regulation-only vs incl-OT, per-market rules text pulled
   from the API like KXWCGAME was.
4. **Leg taxonomy + aliases**: classification with UNKNOWN branches for every
   new series; alias table only if a pricing identity requires it.
5. **In-play lifecycle**: multi-game slates settle staggered EVERY NIGHT (unlike
   the WC's two games) — fixes #1 (in-play breaker exemption) and #2
   (settlement receivables) are PREREQUISITES, not nice-to-haves, or the bot
   halts nightly through every slate's endgame.
6. **Sizing**: budgets auto-scale, but slate structure changes concentration
   math — many simultaneous games = more diversification headroom per game;
   revisit game_loss_frac (0.50 was two-game WC posture).
7. **MC capacity**: nightly slates → more games in the joint model; profile
   book-risk MC and peak-profile build at 10-15 games (the 131k state cap and
   off-loop pools were sized for 2).

## Post-WC build queue (all automation-doctrine compliant)
(1) in-play breaker exemption; (2) settlement receivables in equity;
(3) adaptive-K waiver; (4) quote-size degradation near mass-acceptance ceiling;
(5) ΔP(book)-aware candidate pricing; (6) DC scorer-target cap (identifiability
review); (7) offline MC tool settled-aware; (8) skew clamp stress-scaling;
(9) corpus backtest completion for the rho promote (chain still running).

## NEXT STEPS
- Operator: clear the human-only kill → final settlement booking completes →
  end-of-campaign realized P&L statement.
- Monday: merge risk-audit-overnight → main (llm-b ancestry check), then the
  MLB/WNBA switch (remove WC aliases + KXMENWORLDCUP prefix, arm KXMLB/KXWNBA)
  gated on checklist items 1-5.
