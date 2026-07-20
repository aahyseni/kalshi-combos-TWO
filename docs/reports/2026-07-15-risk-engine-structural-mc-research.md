# Risk-engine upgrade — structural portfolio MC + marginal gate: design + adversarial critique

**Date:** 2026-07-15 · **Area:** risk engine (the fill blocker) · **Method:** multi-agent research
workflow `wf_1c8bbeed-40e` (3 parallel audits → design → adversarial critique; 5 agents, 565k tok).
**Follows:** `2026-07-14-fill-blocker-is-comonotone-risk-cap-not-price.md`.
**Operator directive:** "our MC should calculate risk of ruin, chance of profit, and more; take
positions if the math makes it +EV overall including our other combos; any leg can hedge another
combo — corners, BTTS yes/no, ML, advance, everything; do DEEP research on how to fully calibrate."

---

## The core idea (validated by the audits)

Drive the **risk MC off the same Dixon-Coles joint that prices the legs.** Instead of drawing
correlated per-leg marginals with one scalar ρ per game (`book_model.py:255` `max(rhos)` — which
*cannot* represent the ARG-adv ⊥ ENG-adv hedge), **sample one scoreline per game** and settle every
leg against it. Then every hedge/exclusion is automatic with **no ρ table**: a sampled scoreline
eliminates ARG ⇒ every `ARG-adv + …` combo pays NO $1, with zero correlation parameter anywhere.
Most same-game soccer correlations become **free** (ML×ML, advance ME, ×total/BTTS/spread/
goalscorer). Only leg types with no scoreline state — **corners, cards** — stay on the ρ table and
need measurement. `pricing/dixon_coles.py` already has the machinery; it's just not wired into risk.

## Staged plan (design)

| Stage | What | State |
|---|---|---|
| **B** | Interim: replace the comonotone per-game loss (`exposure.py:275`) with a **mutual-exclusion-aware** max-over-branches bound | prototype validated (`tools/proto_mutex_game_cap.py`); needs n-way generalization before port |
| **A1** | Structural portfolio MC — new `sim/structural_book.py`, sample scorelines, settle exactly; parity-gated `MC joint == joint_probability` | designed |
| **A2** | Marginal-CVaR + EV + **P(ruin)** last-look gate: take a fill iff ΔEV>0 ∧ post-ES₉₉≤budget ∧ post-P(ruin)≤budget | designed |
| **#33** | Rehydrate positions on restart — **prerequisite**: today the MC "runs but never governs" (ph18 emits 0 snapshots, book starts empty) | prerequisite |

## The 5 problems the adversarial critique caught (must fix before any A port)

1. **[BLOCKER] The cheap marginal-at-last-look isn't buildable as written.** `BookRiskSnapshot`
   (`book_risk.py:72-99`) persists **no `values` matrix**, and `engine.marginal_impact` **re-samples**
   (a full 20k×n MC) every call. So "one `position_pnl` add, <1 ms" is false — either persist a
   sample cache (unbudgeted memory/coherence cost) or accept an MC at confirm. **And a candidate on a
   *new* game has no cached columns to add to** — so the cheap path can't score most fills at all.
2. **[Burst hole] Demoting the analytic cap reopens mass-acceptance.** The last-look gate is
   *per-accept and stateless*; N quotes accepted simultaneously each see a book without the other N−1
   in-flight accepts, so all pass "post-ES ≤ budget" while their **sum** blows it. The comonotone/mutex
   analytic cap is exactly what covers the simultaneous-accept burst today. **Keep it co-equal, do not
   demote.**
3. **[Estimator leak] Fractional settlement re-leaks the hedge.** The design's "Bernoulli(pens_win_a)
   per draw, independent per leg" would, on shootout states, land ARG-adv and ENG-adv on the same side
   in some samples — re-injecting the both-advance impossibility it's meant to delete. The pricer
   *marginalizes analytically* (`dixon_coles.py:412,519`), never resamples. **Fix: one shared shootout
   coin, thresholded.** Also: structural goalscorer is "exact" only for the single-player tail case;
   multi-scorer/2+ needs the inclusion-exclusion thinning or routes to copula.
4. **[Exploit] Net-flat-gross-huge.** Welcoming hedging fills on ΔES≤0 admits unbounded *gross* (huge
   ARG + huge ENG). Net tail ≈ 0, but **fees, settlement-convention error, and edge error all scale
   with gross, and don't net.** **Fix: retain the gross-notional backstop (`SKIP_UTILIZATION_BACKSTOP`,
   `limits.py:495`) as a hard cap the tail gate cannot override.**
5. **[Quiet-failure] Cards on a flat +0.6.** A hardcoded ρ in a *tail* model is an unmeasured prior,
   not "fail-closed." **Fix: a card leg reaching the MC ⇒ UNKNOWN ⇒ no-take** (they already have no
   leg type). Corners are measurable (co-settlement tetrachoric) and correctly prioritized.

**What the critique CONFIRMED ships:** the core insight (sample the joint scoreline) is correct and
kills the ρ table for same-game soccer; **Stage B is a valid, prototype-validated tightening** (fix
the 2-outcome hardcode → n-way, fail-closed to comonotone); the `joint_probability`↔MC parity gate is
the right discipline and will itself catch leaks #3; the fail-closed matrix and the #33 prerequisite
are sound.

## Honest magnitude (from the live data)

B alone is only **~1.2× tighter on our current 3:1 ARG-skewed book** — it can only net the small ENG
side. The big overstatement is *within-branch* (our combos are 1–27% fair, not the 100% comonotone
assumes → ~2×) plus the mass-acceptance-of-20-quotes assumption — **only the structural MC (A) nets
those.** So B is the correct, safe foundation and gives modest immediate relief; A is the real fix,
and it now has a concrete design with 5 known fixes.

## NEXT STEPS

- **Owner: operator (decisions owed):** (1) **P(ruin) floor** — hard ruin ($0) or an operating floor
  (e.g. −25% = $1,500) you'd stop at? Sets the gate budget. (2) **Interim unblock** — since B alone is
  ~1.2×, also raise the game/slate cap (8%→16%?) to bootstrap a balanced book this week, or hold at 8%
  and wait for A? (3) Sign off on the B → #33 → A1 → A2 sequence + the budgets (es 15%/$300; ruin
  ≤0.5% hard / ≤2% operating) before any live-module port.
- **Owner: next agent:** ship **Stage B** (extend prototype to n-way ME + fail-closed → port
  `exposure.py:275,300` keeping the analytic cap **co-equal** → test + cent-parity on the ENGARG book);
  then **#33** rehydration; then **A1** with the parity gate, incorporating the 5 critique fixes.
- **Owner: calibration:** measure corners×{ML,total,spread,advance} tetrachoric ρ from the live
  co-settlement tape (`data/combomaker-prod-live-wc.sqlite3`) — the only same-game pairs the structural
  MC never covers for free; promote as pure config values.
- Full design + critique: workflow `wf_1c8bbeed-40e` transcript; anchors verified at HEAD.
