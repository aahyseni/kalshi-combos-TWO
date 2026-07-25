# 2026-07-25 — P1 risk-rebuild design dossier (9-agent code+docs fan-out, verified anchors)

**Purpose.** Ground the P1 concentration/hedge rebuild (post-mortem spec,
2026-07-23) in the CURRENT code, not memory. 8 parallel readers (reports,
risk-engine breakdown, exposure.py, limits+MC, adaptive caps, quote app,
NOTES.md, risk tests) + 1 synthesis, every file:line re-verified. Conflicts
between readers were resolved against source and are recorded here.

## The P1 thesis (why $210 was lost with every cap enforced)

**All enforced caps bound QUOTE-TIME PROJECTIONS (mass-acceptance worst case
of book+quotes+candidate), never the ACCUMULATED NET when several resting
quotes on one structure fill together.**

- Per-combo 5% cap checks the CANDIDATE's `max_loss_cc` only
  (`limits.py:845-855`) — mass-acceptance re-hits of ONE structure grew a $74
  Marte combo to $149.24 past the $122.58 cap. The re-hit bypass.
- Short-Detroit committed net grew to ~$349 (82% of book) under a directional
  cap that throttles NEW quotes; nothing bounds the settled outcome.
- Skew rebate is ARMED live (YAML `skew.enabled: true` since 7/18) but
  PASSIVE-only: capped at a hand-set 150cc tighten, reprices incoming RFQs,
  never pays up to WIN offsetting flow. `hedging/planner.py:33-44 plan()`
  raises NotImplementedError. SkewLimits util denominators are STATIC config
  dollars (`quote_app.py:1007-1013`) — not bankroll-scaled (North Star debt).
- MC computes `p_profit` (P(book)) every 15s (`sim/book_risk.py:1056`) but its
  ONLY consumer is `ops/report.py:94` — absent from the PortfolioRisk protocol
  (`limits.py:445`) and `_TailAxes` (`book_risk.py:1270`). Steers nothing.
  At the candidate gate, pre/post pnl vectors are ALREADY sampled
  (`book_risk.py:1989-1992`) — candidate ΔP(book) is one np.mean each, free.
- `cancel_all` (`lifecycle.py:4282`) has no directional memory; refill is
  ordinary flow → freed headroom refills the concentrated side (relight
  ratchet). Committed positions SURVIVE cancel-all, so
  `committed_dir_entries_by_game`/`directional_by_game_cc` already carry the
  concentrated direction at relight — a guard needs a CONSUMER, not new state.
- NO entity (player/team) axis exists anywhere in risk/ — grep-verified; the
  only aggregation keys are `market_ticker` and `game_key(event_ticker)`.
  `LegRef` (`exposure.py:98-101`) has no entity field. Cross-game same-player
  clustering ($304.99 Marte) is invisible to every cap, the det-max residual,
  and the skew.

## Key attach points (all verified)

| P1 piece | where | how |
|---|---|---|
| Entity extractor + axis | `exposure.py:98` (LegRef), `:1200` (partition), `:1255` (quote fold), `:497` (snapshot fields) | add `pos_legs_by_entity` beside `pos_legs_by_game`; signed per-entity net beside `delta_by_game` (:1213-1217); thread through the E2 away-from-zero fold (:1281-1316) AND `_haircut_compose_cc` (:930) or the axis escapes the resting haircut |
| Entity/direction cap checks | `limits.py:735` (_r2_breaches), template `:654` (per-market/game delta loops + `scaled_delta_cap_contracts` :84 auto-scaling) | new ReasonCodes; Breach needs an entity key beside `.game`; classify vs `WAIVABLE_RESERVATION_BREACHES` (`lifecycle.py:136`) + `PRE_PRICING_MONOTONE_REASONS` (`limits.py:301`) with monotonicity proof |
| Accumulated per-structure net bound | `limits.py:845` (candidate-only today) + reservation path `lifecycle.py:2764` / `_fill_position:2827` | key accumulated committed+reserved exposure on the combo market (structure), checked where every fill flows (reservation re-runs LimitChecker vs committed+outstanding+candidate) — bounds the OUTCOME, not the add |
| Cap derivation (North Star) | `cap_family.py:124` (derive_cap_fractions), `derived_cap_engine.py:86-97` (override list) | entity/direction fracs derived from anchors, joined to the enforce replace-list; MUST pass validate-caps-can-quote vs real MLB sizes |
| Monotone fold template | `exposure.py:831` (`mutex_scenario_bound`) | the generic branch-max monotone fail-closed fold to copy for any new netted axis |
| P(book) protocol read | `limits.py:445` + `lifecycle._book_risk_for_check` (:970) | add `p_profit` via the getattr-fallback precedent (p_ruin_upper); snapshot MC is COMMITTED book only |
| Candidate ΔP(book) | `book_risk.py:1989` + `_TailAxes:1270` + `_candidate_gate:2151` | free from existing pre/post vectors; shadow-first, then a derived budget |
| Pricing steer precedent | `skew.py:525` (peak component), built `lifecycle.py:3950-4020` | off-hot-path, generation-stamped, fail-safe-NEUTRAL MC signal → the shape for a P(book) steer (widen concentrating side / rebate balancing side — diversity via pricing, never blocklists) |
| Skew pay-up | `quote_app.py:1007-1013` (static SkewLimits) + `skew.py:89-156` (150cc tighten) + GameSkewCache `skew.py:174` (built, never populated — feed from per_game_tail_cc `book_risk.py:465-518`) | replace static dollars with LimitChecker.limits/bankroll-derived denominators; derive the tighten bound; sign flip stays solely `applied_cc = -skew_cc` (`skew.py:240-247`); free-money clamp remains outer bound |
| Confirm-side pay-up | `book_risk.py:2207` certified-hedge lane (`allow_negative_ev_hedge` + `hedge_cost_budget_cc`, default OFF) | the existing channel for paying EV only for POST-tail ≤ PRE fills; arming + budget derivation = confirm-side twin of the rebate |
| Relight guard | `lifecycle.py:4282` (cancel_all) + attach in handle_rfq between `_quoting_policy` (2407) and quote-time check (2432) | candidate alignment vs committed direction via `mutex_directional_alignment_cc` (`exposure.py:751`); prefer pricing-asymmetry over refusal |
| Post-fill eviction precedent | `lifecycle.py:2865` `_risk_evict_after_fill` | the closest existing concentration-aware headroom mechanism; relight neutrality generalizes it |
| Rollout | `limits.py:492` set_limits atomic swap; `adaptive_caps_mode` off/shadow/enforce; StarvationWatchdog `limits.py:372` | the ladder for every new cap; validate-can-quote before arming |

## Invariants that gate every P1 change (from the pinned tests)

1. **E2 mass-acceptance monotone dominance** — every quote-time fold must be
   monotone in the entry set; net exactly ONE explicit-True ME event via
   max-over-branches; fail CLOSED to comonotone on 0/≥2 (richer netting opens
   a taker cherry-pick hole). Pinned: `test_exposure.py:486`,
   `test_exposure_mutex_cap.py:107/117`, `test_directional_hedge_cap.py:161/182`,
   `test_resting_haircut.py:306/361/418/677`.
2. Richer hedge/net credit is CONFIRM-PATH ONLY (candidate MC + waiver);
   resting quotes NEVER earn hedge credit.
3. Two money axes never summed (premium/loss vs $1-notional); the game cap
   binds COMONOTONE worst-case while per-combo binds premium — the H2 measure
   mismatch that bricked enforce. Any new axis must state its measure.
4. North Star: no hand-set numbers outside measured/anchors; `kill_anchor`
   0.12 is the ONE dial; ratchet (worsening never raises a cap).
5. VALIDATE-caps-can-quote before live (the 7/23 brick).
6. Fail-closed with the never-brick exceptions (committed unknown-marginal
   doesn't veto quoting; all-reserved book stays usable).
7. Candidate MC gate strictly additive + atomic (reservation-first).
8. Shadow = log-only, one seam (`_partition_breaches:1263`); off = byte-identical.
9. Skew sign flip in exactly one place; UNKNOWN = neutral zero.
10. No double risk layers (quote-time never double-counts confirm-exact).
11. Certified risk-reducing fills bypass concentration caps (waiver/POST≤PRE
    certification, never leg-sign heuristics).
12. Testing isolation / fix isolation / throughput-never-regresses / no
    P&L refit. Suite baseline 2691/0.

## Gaps the dossier surfaced beyond the P1 spec

- Adaptive-cap sensor permanently unfed: `pnl_history=[]` hard-wired
  (`quote_app.py:2312`), `mc_*` refresh params never passed (:2310-2313),
  `_count_slate_games` over-counts multi-day; bootstrap self-defeating.
  Fast-follows: per-game/per-combo P&L DB reconstruction + projected-book MC.
- Snapshot MC covers COMMITTED positions only (`combo_positions_for_quotes`
  exists in `book_model.py:371` with no caller) — the quoted-worst-case book
  is un-MC'd.
- Waiver validates directional certificates vs `game_thr` not
  `directional_thr` (`limits.py:881-884`) — documented as deliberate; two
  readers independently flagged it for doctrine re-confirmation in P1.
- AS1 (settlement/balance atomicity) unverified; AS4 (scalar receivables)
  accepted-as-is pending alarm promotion — P2 territory.
- ≥2-ME netting parked (the hedge-pair build, `test_skew.py:664-702`) — P1c
  presses against it.

## Recommended P1 staging (by dollars-lost attribution)

1. **Stage 1 — accumulated NET bounds (the $210):** per-structure (combo
   market) committed+reserved net premium bound at the reservation path (kills
   the re-hit bypass exactly where every fill flows) + per-game-per-direction
   accumulated committed net bound (derived frac; the outcome bound the
   directional throttle lacks). Both derived, shadow → validate-can-quote →
   enforce.
2. **Stage 2 — entity axis:** extractor (ALL leg families incl. players,
   teams), snapshot fields, E2-preserving folds, derived entity frac,
   same-player markup adder (pricing side).
3. **Stage 3 — steering:** P(book) into the protocol + candidate ΔP(book)
   shadow logging; peak-pattern pricing steer; skew denominators → derived;
   GameSkewCache fed; certified-hedge lane armed with derived budget.
4. **Stage 4 — relight neutrality:** alignment-aware refill guard via
   `mutex_directional_alignment_cc` + drill at next relight.

## NEXT STEPS

- **Me:** implement Stage 1 (off-line, tested, parity-checked, adversarially
  reviewed, validate-can-quote) — then Stages 2-4 in order.
- **Operator (still open from the post-mortem):** resume posture — flat until
  Stage 1-2 exist (recommended) vs interim guard; plus the queued cap-refactor
  decisions (kill_anchor %, C1/C3/C5).
- **Standing:** bot DOWN (KILL set) until the rebuild or an explicit relight.
