# 2026-07-18 — Ship: mutex-aware det-max cap + trimmed-set waiver fingerprint

**Operator directive (morning 7/18):** "waiver churn seems like its being fixed,
thats good. we need to fix the det-max budget full, we're missing a lot of EV
there and good bets. game cap is good we dont want big bets same with per combo
cap... lets ship those and fill some more bets for today." (FRAENG kicks ~3 PM ET.)

## Why (overnight evidence, 1:00–7:00 AM ET, 86 declines decoded from `decisions`)

| cause | n | notes |
|---|---|---|
| waiver "unstable: book moved during enumeration" | 51 | mechanical fingerprint churn, NOT risk judgment; mostly clones of already-filling shapes |
| `post_deterministic_max_over_budget` | 15 | **+$16.17 fill-time EV refused** — killed the diversity (ARG-champ+Messi +$3.15, ESP-champ+ENG +$1.16, tie/ESPARG shapes) with comonotone det-max at $453 of $500 while CVaR sat at $412 of $700 |
| game cap $1,000 (state-aware) | 6 | honest: single-game worst cases up to $1,373 refused |
| per-combo 1/20 bankroll | 4 | honest: $128–$213 single clips refused |
| negative fill-time EV | 3 | honest: gate saved −$7.76 incl. a −$6.66 ENG "hedge" (stale-quote pickoff) |
| velocity / snapshot / misc | 7 | one-offs |

Session fills at ship time: 23 fills / $167.90 premium / +$7.16 expected edge
(16 of 23 = FRA-win+Mbappé — overnight demand artifact + the caps above).

## Build A — trimmed-set waiver fingerprint (`lifecycle.py`, `state_worst_case.py`)

Waiver stability now keys on what the enumeration actually used: grant iff
(1) `position_generation` + `reservation.version` exactly unchanged, (2) every
still-present trim-SELECTED (top-K=12) quote unchanged (id + `worst_hit_loss_cc`;
vanished selected quotes tolerated — conservative), (3) per breached game the
CURRENT tail (same-game quotes outside the enumerated selection, via new
`tail_outside_selection`) ≤ the tail adder folded into the certificate. Soundness:
per state each quote contributes `max(0, loss) ≤ worst_hit_loss_cc`, so
(2)+(3) ⇒ certified `(trimmed worst + adder)` still upper-bounds the CURRENT
book in every state; committed-risk moves are never waived through. Bound width,
waivable-breach set, budgets, decline strings, metrics: unchanged — only the
stability judgment is precise now. New: `tests/test_waiver_fingerprint_trimmed.py`
(11 tests; includes a mutated-under-id fail-closed case the OLD fingerprint
could not see).

## Build B — mutex-aware det-max (`book_risk.py`, `limits.py`, `exposure.py`, `config.py`)

The portfolio det-max cap (quote-time `SKIP_PORTFOLIO_DET_MAX` + candidate-gate
`post_deterministic_max_over_budget`) now gates on `min(comonotone, mutex_aware)`:
single-game long-NO fully-gamed units bucket per game; per game the bound is the
state-exact DC-scoreline worst (with `earns_credit=False` — clamped ≥0, no
netting credit, ET/pens branch-expanded), floored at the largest unit; across
games bounds SUM (no independence assumption needed for an upper bound);
multi-game/ungamed/non-NO/reserved units stay comonotone residual; any
doubt/exception ⇒ comonotone for that slice (fail closed). Invariants (probed +
property-tested): every realizable joint outcome ≤ bound ≤ comonotone; equality
when no structure proven. Comonotone number keeps emitting for telemetry; decline
lines + breach details now carry BOTH numbers. Kill switch
`portfolio_det_max_mutex_aware: true` (default) — threaded to BOTH sites incl.
the worker gate via `CandidateBookRiskInputs.det_max_mutex_aware`.
New: `tests/test_mutex_aware_det_max.py` (27) + 2 knob-plumbing pins in
`test_candidate_gate_wiring.py`.

## Adversarial verify (3 lenses, parallel)

| lens | verdict | key evidence |
|---|---|---|
| Soundness | **CLEAN** cores, 0 SERIOUS | mutex bound EXACTLY TIGHT vs exhaustively enumerated hand-settled oracles (KO book incl. pens branches, champion alias, float seam: ~2.5k outcomes, 0 violations); waiver grant condition proven per-state |
| Live integration | **CLEAN**, 0 SERIOUS | config→limits→cap plumbing verified live-path; worker pickling + alias install verified; suite 2365/0; net-fewer ruff errors than baseline |
| Risk economics | **SHIP-WITH-CONDITIONS** | serial-greedy attack: admits $797 comonotone premium at $498.65 worst-REALIZABLE (≤ thr); burst/serial-commit chain airtight; waiver bound width unchanged (0 grants → starvation conversion only) |

Verify-driven fixes applied post-review: (1) knob threaded through
`CandidateBookRiskInputs` → worker (was quote-time-only; config comment was
false) + 2 pin tests; (2) `_StaleBookRisk` sentinel got the new field (mypy
strict clean); (3) decline/log lines now emit `post_mutex_det_cc` /
`mutex_aware_det_max_cc` (None-safe).

## Accepted conditions / watch items

1. **$500 det budget changes meaning**: was "Σ premium at risk" (assumption-free),
   now "worst single realizable joint outcome" (trusts the settlement model).
   Assumption-free backstops remaining: per-combo $100, 3× utilization, cash,
   halts, to-the-cent reconciliation HALT. Operator owns this trade (directive above).
2. **KXWCGAME settlement rule — VERIFIED same day** (Kalshi public API,
   `rules_primary` for KXWCGAME-26JUL19ESPARG-{ESP,ARG,TIE}): settles "after 90
   minutes plus stoppage time (**does not include extra time or penalties**)" —
   exactly the regulation-only `TeamWin(include_et=False)` our parser assumes,
   TIE outcome confirmed. Condition CLOSED before the final.
3. **Expectation-setting for restart**: live book is genuinely one-sided, so at
   restart mutex = comonotone = $453.83 — NO new headroom for FRA/common/BTTS
   flow. What unblocks is exactly opposite-branch flow: ENG-win rides at ~zero
   det cost, ARG-champ ~$5.64/100. ESP-champ / tie+BTTS correctly STILL decline
   on this composition (they concentrate). Tonight's decline reports showing
   those are correct behavior, not regression.
4. Minor accepted: equal-size content-swap-under-id blind spot (unreachable under
   lifecycle id discipline; defense-in-depth incomplete not absent); ME-metadata
   netting dormant live (all netting is structural/scoreline via alias — champion
   legs DO net through the final's game); candidate-gate enumeration cost ms-scale
   (watch if position count ever reaches hundreds per game).
5. If ENG wins today: ~$174 of dead FRA-leg multi-game combos stay counted at
   full premium until Kalshi early-NOs them (observed 7/10) — conservative.

## NEXT STEPS

- **NOW (me):** full suite → commit+push → restart bot on new code
  (`live_20260718_mutexdetmax.log`) → re-arm monitor → confirm preflight green
  + first quotes. Then KXWCGAME rulebook check (condition 2).
- **~3 PM ET (me):** FRAENG game-day watch per standing format; watch paired
  post_det vs post_mutex_det telemetry + P1-7 tripwire; report fills/declines in ET.
- **Post-game (me):** decline-mix comparison vs last night (waiver-churn class
  should be ~gone; ENG/ARG-champ class should fill).
- **Sunday (operator+me):** budget-family review with both games' data → merge
  (llm-b ancestry check) → MLB+WNBA switch.
