# 2026-08-01 — Marginal KILL gate: the sunk-book two-regime form (built dark; arming staged)

## The operator ruling (verbatim — this is the spec)

> "The only reason the book could be -EV is if odds have changed... even if we
> did have a -EV book we should still quote to increase it; when we fill
> something we always fill at +EV; what happens after that we can't decide,
> besides quoting more and filling more."

Ratified translation: the STANDING book is SUNK (sell-only, no unwind). The
absolute level constraint belongs to the model-free det-max backstop (0.70B,
armed, stays). The KILL gate must judge the MARGINAL candidate, not freeze all
quoting on the inherited level. Generalizes hedges-always-fill.

## STEP 1 — the live refusal, reproduced and NAMED (mandatory decomposition)

The 10:49 ET boot armed `kill_anchored_book_gate`; since then
`skip_portfolio_cvar` refused everything and `quote_sent = 0`, while
`tools/diagnostics/kill_anchor_live_book.py` reported P(KILL-night) = 0.00100 —
50x inside the 0.02 budget. The contradiction is RESOLVED; the refusal is
NAMED, term by term:

| suspect term | verdict | evidence (live tape / DB, read-only) |
|---|---|---|
| the tail-probability number itself | **YES — this is the refusal** | every decline detail reads `P(book loss >= 3363212cc) = 0.110–0.115 (upper) > kill tail budget 0.0200 (KILL line 3/25 bankroll)`; the snapshot's own shadow readout agrees: `p_kill_night: 0.115` on every `book_risk_snapshot` row. The book is GENUINELY ~5.7x over the ratified budget on the gate's own measurement |
| the BAND (corr_tail_stress high) | contributing by design, not a bug | the envelope §(8a) gates on IS the adverse-band worst-credible envelope (`band: "high"`, `tail_joint: "corr_tail_stress"`) — the same measurement the arming boundary proofs (1.1/1.9% admitted, 2.3/4.9/10.4% refused) and the shadow readout used. No wrong band is wired anywhere |
| Wilson/z upper bound | negligible | breach `p_upper 0.1150` == the logged point `p_kill_night 0.115` — the z-term adds ~nothing |
| ES fallback (`es_residual_cc`) | not taken | zero ES-fallback breach strings on the tape; the tail-probability form evaluated every time (envelope + n_samples present) |
| stale bankroll | fresh | kill line 3,363,212cc = 0.12 x 28,026,768cc = the boot's own `account_standing exchange_equity_cc` ($2,802.68) |
| snapshot staleness | fresh | decline rows carry `snapshot_age_s` 0.018–0.056, `snapshot_generation == live_generation` (29); zero `snapshot unusable` strings this session |

**Why the diagnostic said 0.001:** `kill_anchor_live_book.py` measured a
DIFFERENT experiment — the stale 39-position store copy (the live book is 26
positions at gen 29; the store keeps rows for games the ledger hasn't graded),
proxy independence marginals implied from entry prices (the live MC reads real
leg books + the tail-dependence stress joint at band "high"), and the 7/31
default bankroll $2,998.58 (kill line $359.83 vs the live $336.32). It is not
evidence of a live bug.

**Verdict: the refusal is GENUINE LEVEL-GATING** — the inherited book really
sits at P(KILL-night) ≈ 0.115 under the armed measurement, the level check
judges the book, no candidate can change the book, so 100% of candidates
refuse (394,652 decision rows today; 140,338 carry `skip_portfolio_cvar`;
last-window audit: 51,388 risk audits, 0 sent). The design below is what the
ruling ordered. (A concurrent session disarmed the gate in the local config at
~12:15 ET with the note "Re-arms as the MARGINAL form when that build gates" —
this build is that form; the live 10:49 process still runs the armed level
gate until its next boot.)

## The design — two-regime KILL gate (`kill_gate_marginal`, default OFF)

ONE new `RiskLimits` flag, read by every site (quote-time cap, reservation,
last-look advisory, confirm fallback, confirm MC gate) — no second config
copy, cap/gate can never diverge. Nothing hand-set: every number below is a
ratified anchor (KILL 12%, budget 2%, alpha 0.02) or existing measured
machinery (the 2026-07-31 diversity-key metric, the CRN candidate MC).

**UNDER budget** (book P(KILL) <= 0.02): byte-identical to the armed level
form — quote-time silence, confirm admits iff post-add <= 0.02.

**OVER budget** (the inherited book already past 0.02): do NOT freeze.

* Deterministic sites (§(8a) in `risk/limits.py`; quote-time `check`,
  reservation `try_reserve`/`revalidate`, last-look advisory, confirm
  fallback): admit iff **certified risk-reducing** (existing hedge machinery,
  confirm-path waiver) **or `dES99 <= dEV x P(accept | size bucket)`** — the
  just-shipped eviction diversity-key machinery verbatim
  (`rfq/eviction_value.py`): dES99 = the candidate's ALLOCATED share of the
  book-risk MC's additive per-game CVaR decomposition
  (`allocate_des99_cc` over the factored `_book_tail_allocation`, ONE
  implementation with the eviction key); P(accept) = the exact
  Clopper-Pearson LOWER bound of the candidate's premium bucket on the
  in-process acceptance tape at the ratified alpha
  (`portfolio_kill_tail_prob` = 0.02); quote-time uses the CP lower, confirm
  sites use 1.0 (the accept happened).
  **Boundary `<=` not `<` (justified deviation from the dossier):** a
  diversifier on an untouched game has dES99 == 0 exactly, and a fresh boot's
  acceptance tape is EMPTY (CP-lower = 0 for every bucket) — strict `>` would
  deadlock (no quotes -> no tape -> no admits), the 100%-decline-dressed-as-a-cap
  the 2026-07-23 lesson forbids. At `<=`: zero-marginal-tail flow admits
  immediately; CONCENTRATING flow needs measured acceptance credit — fail-closed
  exactly where it must be.
* Confirm MC gate (`sim/book_risk._candidate_gate`): admit iff **certified
  risk-reducing** (POST governing UNCLAMPED tail <= PRE on shared CRN — the
  existing certification verbatim) **or the candidate does NOT RAISE the
  measured P(KILL-night)** (post vs pre `kill_tail_prob_upper` on the SAME
  CRN sample, worst model — the marginal effect on THE RATIFIED ANCHOR
  itself).
  **Justified deviation from the dossier's dEV−dES99 at this ONE site:** the
  CRN governing-ES difference is structurally ~the candidate's FULL premium
  for any small diversifier on a flat-tail book — the post-sort worst-1%
  re-selects the (tail ∧ candidate-loses) scenarios. Measured while building:
  a $30.60-premium new-game candidate, +$1.21 EV, pre/post P(KILL) IDENTICAL
  (0.04675 == 0.04675), was charged dES99 **+$28.80** (94% of its premium).
  "ES barely credits diversification" is the exact pathology the tail-prob
  anchor was ratified against (2026-07-25) — an ES-delta arm here would
  re-freeze the book at the confirm site and renege on every quote-time
  admit. DEPTH concentration this probability comparison cannot see (the
  same-game depth-doubler) stays refused at the deterministic sites by the
  ALLOCATED dES99, and bounded by the det-max backstop / per-game / entity
  walls, which all still run.

**Unchanged, verbatim:** det-max backstop 0.70B absolute (both sites);
staleness/unusable snapshot fails closed on BOTH portfolio axes before any
regime is read; `kill_marginal=None` (book-only/maintenance callers, unknown
EV) keeps the LEVEL form — UNKNOWN never admits; p_ruin, per-game, entity,
slate, directional, gross, halts all untouched. The F1 pre-pricing gate never
declines on `SKIP_PORTFOLIO_CVAR` (not in `PRE_PRICING_MONOTONE_REASONS`), so
no monotonicity repair was needed.

### Known limitation (v1, documented in code)

The quote-time allocation is premium-share within a game and DIRECTION-blind:
a quote-time offsetting hedge on a hot tail game is charged like a
concentrator and admits only on its EV credit; at CONFIRM the exact CRN
certification admits it regardless. Same axis as the player-level risk gap
(entity direction tracking) — the P0-9 direction feed is the eventual fix.

## Files touched

| file | change |
|---|---|
| `risk/limits.py` | `kill_gate_marginal` flag; `KillMarginalCandidate`; `kill_envelope_tail_upper` (ONE envelope implementation — cap + lifecycle probe cannot drift); §(8a) two-regime branch |
| `sim/book_risk.py` | `_candidate_gate` + `evaluate_candidate_book_risk`: `kill_gate_marginal`, `pre_pnls` (CRN pre-book vectors), marginal branch |
| `rfq/lifecycle.py` | `_book_tail_allocation` (factored verbatim from the eviction key), `_kill_marginal_input/_for_quote/_for_fill`, `_fill_ev_cc`; threaded at quote check, eviction re-check, last-look advisory, reservation, confirm fallback, candidate-gate inputs |
| `risk/reservation.py` | `kill_marginal` passthrough on `try_reserve`/`revalidate` |
| `ops/pricing_pool.py` | `CandidateBookRiskInputs.kill_gate_marginal` -> worker |
| `ops/config.py` | `risk.kill_gate_marginal` yaml key -> `RiskLimits` |
| `tests/test_marginal_kill_gate.py` | 18 property tests (below) |
| `tools/diagnostics/marginal_kill_gate_counterfactual.py` | the frozen-tape counterfactual |

## Property tests (all in `tests/test_marginal_kill_gate.py`, 18/18 GREEN)

| property | test |
|---|---|
| over-budget + diversifying candidate ADMITS (both sites, empty tape) | `test_diversifier_admits_even_with_an_empty_acceptance_tape`, `test_over_budget_diversifier_admits_marginal`, e2e `test_inherited_over_budget_book_admits_a_fresh_game_candidate` |
| over-budget + concentrating REFUSES | `test_concentrator_refuses_with_marginal_detail`, `test_over_budget_concentrator_refuses`, e2e `test_inherited_over_budget_book_still_refuses_a_p_kill_raiser` |
| under-budget unchanged | `test_under_budget_is_byte_identical_with_and_without_the_flag`, `test_under_budget_unchanged` |
| backstop still absolute | `test_det_max_backstop_stays_absolute` |
| UNKNOWN never admits | `test_no_marginal_input_keeps_the_level_form`, `test_unusable_snapshot_fails_closed_on_both_axes`, `test_empty_pre_vectors_are_never_a_free_pass` |
| byte-identity flag-off | `test_flag_off_is_byte_identical_even_with_an_input_supplied` (+ full suite 3,551/0 with the flag defaulted off everywhere) |
| certified reducers always admit | `test_certified_risk_reducer_always_admits`, `test_over_budget_certified_reducer_admits` |
| probe/cap coherence | `test_helper_and_gate_agree_on_the_regime`, `test_helper_fails_none_on_unusable_gate_off_or_no_bankroll` |
| measured acceptance buys capacity | `test_measured_acceptance_buys_exactly_proportional_capacity` |

## Counterfactual on TODAY's frozen tape

`tools/diagnostics/marginal_kill_gate_counterfactual.py` (read-only; proxies
stated in its docstring — implied marginals, store book filtered to the boot
slate, EMPTY acceptance tape = the honest day-one CP-lower of 0):

<!-- COUNTERFACTUAL RESULTS -->

## Gating evidence

| gate | result |
|---|---|
| full suite (build worktree) | **3,551 passed, 0 failed** (275.6s; 3,533 at base + 18 new) |
| vitals fast tier | **8/8 GREEN** (22.1s; frozen snapshot `D:\kct-vdata`, worktree config copy) |
| ruff / mypy on changed files | clean (pre-existing `metadata.py` UP017 + `pricing/engine.py` type-arg baseline untouched) |
| throughput | flag OFF: the only hot-path addition is one guarded call = two attribute reads per quote (measured design; vitals throughput checks GREEN). Flag ON + book OVER budget: the marginal build replaces a state that declines 100% of quotes — any cost displaces certain refusal |
| self-containment proof | <!-- SELF-CONTAINMENT --> |
| pre-ship tier | DEFERRED TO ARMING (ritual below) — the flag ships dark |

## Arming (staged, NOT armed — operator decision owed)

Pre-ship ritual at arming time (quiet machine): copy the local config into a
scratch worktree, `python -m tools.vitals.gate --tier pre-ship` must be 1/1
GREEN, then add to `config/prod-live-wc.local.yaml` under `risk:`:

```yaml
  kill_anchored_book_gate: true   # re-arm the KILL anchor (level form alone froze the 8/01 book)
  kill_gate_marginal: true        # ... as the MARGINAL two-regime form (this build)
```

Both lines are staged as comments in the local config's STAGED block. The
marginal flag governs ONLY with `kill_anchored_book_gate` armed (and that only
with `portfolio_tail_prob_gate`, armed since 7/25); arming order is one
restart with both.

## NEXT STEPS

1. **Operator:** decide re-arming (both flags together) at the next boot;
   pre-ship tier 1/1 GREEN on a quiet machine first (ritual above).
2. **On arming day:** watch the first hour for `(marginal form)` declines vs
   admits on the tape (`risk_audit` binding_cap + decline details name both
   terms); the acceptance tape self-arms intraday and concentrating capacity
   grows with it.
3. **Owed (tracked):** direction-aware quote-time dES99 (the P0-9 feed) so
   offsetting hedges stop paying the concentrator charge at quote time;
   per-file vitals tape manifest (readingb's durable-fix note) so worktree
   vitals stop needing the frozen snapshot.
4. **Diagnostic hygiene:** `kill_anchor_live_book.py` measures a stale store
   book under point-ish proxies — its number is NOT the gate's number (this
   report's decomposition table is the reference); a `--live-envelope` mode
   reading the tape's own `p_kill_night` would prevent the next 50x scare.
