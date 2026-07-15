# Risk-engine upgrade — final report (zero-bias, for external review)

**Date:** 2026-07-15 · **Repo:** `kalshi-combos-TWO` (package `combomaker`) · **Author:** build agent
**Audience:** an independent reviewer (LLM or human). This report is written to be *audited*, not
believed. Claims are tied to files/functions and to test evidence; open problems, un-done work, and
caveats are stated as plainly as the successes.

---

## 0. One-paragraph summary

`combomaker` is a sell-only maker for Kalshi combo/parlay RFQs (we quote a NO side; a combo NO pays
`$1 − Π(leg values)`). It was **winning ~108 of the RFQ auctions it entered on a heavily-traded game
(ENG-ARG) but filling only ~5**, and the investigation traced the block to the **risk engine's
per-game loss cap**, not to price. That cap summed every position's max-loss on a game *as if they all
lose together* (comonotone), which is impossible for mutually-exclusive legs (exactly one of ARG/ENG
advances). This upgrade (a) loosened the interim cap, (b) made the analytic cap **mutual-exclusion
aware**, (c) fixed a restart bug that left the risk book **blind** to held positions, (d) built a
**structural Monte-Carlo** portfolio-risk model that samples one game outcome from the *same*
Dixon-Coles model that prices the legs and settles every leg against it (so all same-game hedges are
exact, no correlation table), and (e) added a **P(ruin)** cap. It is fully unit-tested (suite
**1784 → 1787 passing**, mypy-clean, ruff-clean) and was **deployed live successfully**, but at
deploy time it is **not productively quoting** because of a newly-observed interaction (§8) between
the restart-rehydration and the fail-closed risk view, compounded by the World Cup being at its final
stages (little fresh pregame flow).

---

## 1. The problem, established empirically (not assumed)

Two DB-backed analyses drove the whole design; both are reproducible from the shipped tools.

1. **Pricing is NOT the blocker on the main combos.** `tools/market_vs_our_pricing.py` +
   `market_drift_check.py` compared our actual quotes (`decisions` tape, live DB) to where combos
   *actually cleared* (889,248 real trades in the shadow DB `combomaker-prod.sqlite3`,
   `taker_side='yes'` only, joined to leg-sets via `rfqs.market_ticker`). On 26 liquid main combos our
   ask was a **median 0.75¢ BELOW** the market's clearing. We are not overpriced.
   *(Report: `2026-07-14-market-vs-our-pricing-main-combos.md`.)*

2. **The fill block is the risk cap, at last-look.** On ENG-ARG we won **108 auctions** (you cannot win
   an auction if your ask is too high) and **declined 103 at confirm**; the recent declines were
   `decline_risk_limit`, citing `game 26JUL15ENGARG loss … > 2/25 bankroll`. The cap fires at
   **confirm**, not at quote — so the operator (correctly) observed "we're still quoting it" while every
   *fill* was declined. *(Report: `2026-07-14-fill-blocker-is-comonotone-risk-cap-not-price.md`.)*

**Root defect (verified at HEAD):** `risk/exposure.py` computed a game's worst-case loss as
`game_worst[game] += position.max_loss_cc` — a **comonotone sum** ("every combo on the game loses at
once"). For a book holding both `ARG-advance` and `ENG-advance` NO combos this is impossible (exactly
one team advances), so the cap over-stated risk and blocked fills that were, in reality, hedged.

---

## 2. Design ideas (the "why")

- **A maker with a thin per-bet edge profits by holding *many diversified* +EV bets.** Our per-bet
  edge ≈ 1.7¢ on a ≈44¢ swing (edge/σ ≈ 0.04). P(book profitable) ≈ `Φ(edge_ratio · √N_eff)`, so the
  chance to profit is a function of how many *diversified* +EV bets we can safely hold. The comonotone
  cap throttles `N`, and therefore directly throttles P(profit).
- **Mutual exclusion is a hedge.** `advance(ARG) ⊥ advance(ENG)`: if ARG advances we may lose the ARG
  combos but WIN every ENG combo, and vice-versa. One side always pays us. A risk model that sums
  worst cases cannot see this; a model that samples the *joint outcome* sees it exactly.
- **The right home for full hedge-awareness is a joint Monte-Carlo, not an analytic cap.** We proved
  (empirically, via a failing invariant test — §5) that hedge-aware tightening of an *analytic* cap is
  **non-monotonic** and breaks the mass-acceptance dominance guarantee. So the analytic cap stays
  deliberately coarse (one mutually-exclusive event) and the MC carries the rest.
- **Reuse the pricing model for risk.** Sampling risk from the *same* Dixon-Coles scoreline model that
  prices the legs means the risk view and the fair are one coherent object — no separate rho table to
  drift, and every hedge the pricer implies is realized in the risk sample.
- **Ruin, not just variance.** The operator's real "ruin" is losing the ability to keep quoting; the
  gate budgets `P(equity < 0.70·bankroll)` (a −30% floor), computed on the same sampled book P&L.

---

## 3. What was built, stage by stage (with code anchors)

All changes are on disk; the suite + mypy + ruff numbers below are current.

### Stage 0 — interim config (operator-set values)
- `config/prod-live-wc.local.yaml`: `game_loss_frac`/`slate_loss_frac` **0.08 → 0.20** (bootstrap room
  while the real fix lands; `directional_frac` kept at 0.10 so only a *balanced* book uses the extra
  room). Verified loaded. Ruin floor −30% is realized as `ruin_floor_frac=0.70` on the MC side.

### Stage B — mutual-exclusion-aware per-game loss cap  (`risk/exposure.py`)
- New pure helpers `_mutex_game_worst_cc` / `_mutex_event_bound_cc` / `_mutex_required`. The per-game
  worst case now **nets a single result mutually-exclusive event** (advance / 1X2) via
  *max-over-branches*, and **fails closed to the comonotone sum on 0 or ≥2 ME events**.
- **Why a single event, and why fail-closed:** recognizing *more* ME structure (a 2nd ME event, or a
  binary yes/no market) refines the partition and *lowers* the bound — which is **non-monotonic**.
  Monotonicity is required by the E2 mass-acceptance dominance invariant (the "if everything is
  accepted" snapshot must dominate every realized subset). A per-accept, stateless gate that isn't
  monotone can be walked under by a taker who accepts only the concentrated side. So Stage B nets
  exactly the dominant (result) hedge and leaves the rest to the MC. **This constraint was discovered
  by a *failing test*, not foreseen** — see §5.
- Wiring: `ExposureBook.__init__(…, is_me_event=…)`; the provider is
  `MetadataCache.event_mutually_exclusive` (`marketdata/metadata.py`), passed at `ops/quote_app.py`.
- Applied to **both** the committed and the mass-acceptance (open-quote) snapshot paths; positions are
  partitioned per game so each game's bound uses only that game's legs.
- Live effect measured on our own book: comonotone $982 → mutex $820 on the 108-auction ENG-ARG set
  (**1.20× tighter — modest, because the book is 3:1 ARG-skewed**; it compounds as the book balances).

### #33 — rehydrate the exposure book on restart  (`ops/quote_app.py`, `ops/persistence.py`)
- Before: startup found existing exchange positions and only **logged** "exposure book starts EMPTY".
  So after every restart the caps + MC were blind to what we still held.
- After: `_rehydrate_exposure_book` rebuilds `OpenPosition`s from **exchange-open (`get_positions`,
  ground truth) ∩ our recorded fills** (`Store.held_positions`: aggregates fills by combo ticker,
  max-loss-preserving entry price, legs via `fills.combo_ticker == rfqs.market_ticker`). Exchange
  positions with no local record are **logged, never guessed** (rule 6).
- **Live-verified:** on deploy it rebuilt **7 positions** across 3 games, `unmodeled_open=0`.
- **⚠ It also surfaced the interaction in §8** — this is the most important open issue in this report.

### A1 — structural portfolio-risk MC  (`sim/structural_book.py`, new)
- `sample_game_values(params, leg_specs, shares, n, rng)`: samples one game state from the Dixon-Coles
  state PMF (`dixon_coles._states`) + a **shared shootout coin** + a **shared per-team goal
  allocation**, and settles every leg to 0/1 by **reusing the pricer's own indicators**
  (`_team_indicator`, `_half_indicator`, `_player_group_factor`). Produces a `(n, n_legs)` value matrix
  that `sim/engine.book_pnl` consumes unchanged (the clean seam).
- `build_game_plans(tickers, events, marginals, cfg)`: groups legs by game and **reuses**
  `structural._parse_match` / `_parse_leg` + `dixon_coles.invert` to invert `ModelParams`/`shares` per
  game; needs ≥2 team-level legs to identify `(λ_a, λ_b)` else copula-fallback; corners/cards/ungamed/
  single-leg → copula. `sample_structural_values(…)` = structural columns + copula columns
  (`sample_leg_values` on the block-rho sub-matrix), partition-exact.
- Seam: `compute_book_risk(…, structural_cfg=…)` in `sim/book_risk.py` (default `None` = byte-identical
  copula path; 37 default-path tests unchanged). Threaded into `lifecycle.recompute_book_risk` and
  `ops/quote_app.py` via a decoupled `StructuralConfigView` built from the shipped `StructuralConfig`.

### A2 — P(ruin) + the structural MC as a governing constraint
- `BookRiskSnapshot.p_ruin` (`sim/book_risk.py`): `P(current_equity + wave_pnl < ruin_floor_frac ·
  bankroll)`, computed on the same sampled book P&L (so it reflects the structural hedge). Uses **live
  equity** so it tightens as we draw down.
- Cap (9) in `risk/limits.py`: `SKIP_PORTFOLIO_RUIN` when `p_ruin > portfolio_ruin_prob_budget`
  (config default 5%), **co-equal** with the existing CVaR + gross + mutex caps (an addition, never a
  demotion). `PortfolioRisk` protocol gains `p_ruin`; `_StaleBookRisk` sentinel too (fail-closed).
- Config: `RiskCapsConfig.portfolio_ruin_prob_budget`, `LifecycleConfig.ruin_floor_frac=0.70`.

### Settlement fee fix (go-live blocker, unrelated to risk)  (`core/money.py`, `risk/settlement.py`)
- Deploy hit a `HALT_RECONCILIATION_MISMATCH` on a **real settlement** whose `fee_cost="0.000080"`
  (= 0.8 centi-cents). The whole-cent money core (correct for prices) rejected the sub-cent fee.
- `fee_cc_from_dollars_str` rounds a **fee** UP to the next whole cc (ROUND_CEILING → never understate
  a paid cost, ≤0.99cc = <$0.0001 error). Prices/revenue stay exact. Used only for `fee_cost`.

---

## 4. Test evidence (what was actually validated)

| Area | Test | What it proves |
|---|---|---|
| Stage B logic | `tests/test_exposure_mutex_cap.py` (15) | advance hedge nets; NO-side legs; fail-closed on 0/≥2 ME events; **400-example monotonicity property** (the invariant the design rests on) |
| #33 rehydration | `tests/test_rehydrate_positions.py` (4) | aggregation + legs; unmodeled skipped; mutex nets on the rehydrated book |
| A1 sampler **parity** | `tests/test_structural_book_mc.py` (23) | **MC joint == `dixon_coles.joint_probability`** for 12 leg types (advance/BTTS/total/single+multi goalscorer/spread/draw/NO-legs/1H×FT); advance mutex exact (P(both)=0); `build_game_plans` recovers input marginals; corners→copula; **structural es_99 < copula es_99** on a hedged book |
| A2 ruin cap | `tests/test_limits_caps.py` (`TestPortfolioRuinCap`) | fires over budget; passes at/below; fails closed on unusable snapshot |
| Fee fix | `tests/test_money.py` (`TestFeeCcFromDollarsStr`) | sub-cc fee rounds up; negatives/garbage raise |
| Regression | full suite | **1787 passed, 0 failed**; `mypy` clean on the 5 changed modules; `ruff` clean |

**Parity is the load-bearing evidence for A1:** the MC's estimated joint probability of any leg-set
equals the analytic pricing joint to Monte-Carlo standard error (worst z ≈ 2.7 at n=200–400k). That is
what justifies trusting the sampled risk numbers.

---

## 5. A correctness finding the design did NOT anticipate (stated plainly)

The first Stage-B implementation was **general** (netting BTTS yes/no, min-over-many-dimensions —
i.e., exactly the "track all legs" ask). It **failed an existing property test**
(`test_mass_snapshot_dominates_every_realized_acceptance`, `3000 <= 2500`). Root cause: **hedge-aware
tightening is non-monotonic** — adding an (unaccepted) open quote that introduces a hedge *lowers* the
mass-acceptance bound below a realized subset that doesn't hold that hedge, so the "if everything is
accepted" snapshot no longer dominates every realizable acceptance. That is a **genuine safety hole**
(a taker can accept only the concentrated side). The fix was to restrict Stage B to a single
mutually-exclusive event (provably monotone) and move all richer hedging to the MC. **A reviewer
should check this reasoning independently** — it is the subtlest claim in the change.

Separately, the A1 sampler is in one respect **more correct than the analytic pricer**: with a shared
shootout coin, `P(advance(A) ∧ advance(B)) = 0` exactly, whereas `joint_probability` multiplies the
two pens factors independently and returns a spurious ~3.6% (it never matters for pricing because no
valid combo holds both advance legs, but it matters for the cross-combo hedge in the portfolio MC).

---

## 6. What was deliberately NOT built (scope honesty)

- **A per-candidate marginal-CVaR gate at last-look** (compute ΔES/ΔP(ruin) of *this* fill on a cached
  sample matrix, "welcome any specific hedging fill"). The adversarial critique flagged real blockers:
  `BookRiskSnapshot` persists **no** value matrix, `engine.marginal_impact` **re-samples**, and a
  candidate on a *new* game has no cached columns. A2 as built governs the **whole committed book's**
  tail + ruin (a backstop) and lets **Stage B** carry the candidate-level advance hedge. The
  per-candidate ΔES gate is deferred, not done.
- **`operative_es` masking:** the CVaR cap gates on `operative_es = max(es_99, challenger,
  deterministic_all_hit)`, and the all-hit stress is **comonotone**, so it *masks* the structural hedge
  for that particular cap. The **P(ruin) cap** (A2) is the constraint that reflects the hedge; the CVaR
  cap remains a conservative backstop. This is a real limitation of the current gating, documented not
  hidden.
- **Corners/cards correlation calibration.** Corners stay on the copula rho table (measurable from
  co-settlements — a queued follow-up). Cards have no leg type and should be no-quote at the classifier;
  a flat +0.6 in a *tail* model would be an unmeasured prior.
- **MLB/other-sport structural sampling.** A1 is soccer-only (the live allowlist is WC). Non-soccer
  legs use the copula path.

---

## 7. Live deployment result (what actually happened on real money)

Relaunched `combomaker run --env prod --mode quote --confirm-live --config
config/prod-live-wc.local.yaml`. Observed, in order: dotenv/creds loaded → joint pool warm → startup
reconcile → **`_rehydrate_exposure_book` rebuilt 7 positions** (`26JUL15ENGARG`, two MLB games),
`unmodeled_open=0` → the fee fix cleared the reconciliation halt (0 `kill_switch_halt`) → **preflight
green** → into the quoting loop. **No crashes; every code path I wrote executed in production.**

---

## 8. A regression I introduced, diagnosed and fixed — with a correction to my own first guess

**First (WRONG) guess — recorded so the error is auditable.** I initially wrote that the ENG-ARG
semifinal was "late/in-play" and that this made the marginals unavailable. **The operator corrected
me: the game was ~20 h away (pregame).** I had asserted a cause without checking a source of truth —
the exact failure mode the working agreement warns against. The corrected, evidence-based account:

**Verified root cause (a regression from my #33 rehydration).** After deploy the bot declined **every**
RFQ (0 quotes; starvation watchdog fired). Binding-reason breakdown from the decisions DB:
`skip_classifier_unknown` was **90%** (7,186 / 8,000) — and it hit even **2-leg** combos like
`SPREAD+TOTAL` and `ADVANCE+TOTAL` that must quote. `SKIP_CLASSIFIER_UNKNOWN` is emitted by the risk
exposure snapshot when `unknown_marginals=True`. **#33 rehydrated 7 positions across 3 games — 2 of
them MLB (NYMPHI, TBBOS)** — but the live allowlist is `[KXWC]`, so **MLB leg books are never
subscribed ⇒ their marginals are `None` ⇒ `analytic_leg_deltas` → None ⇒ `unknown_marginals=True` on
EVERY check ⇒ every quote declined.** Before #33 the exposure book started empty, so nothing poisoned
it — that is precisely why ph18 quoted 17k times and the post-#33 run quoted zero. This was **my bug**,
introduced by #33, not a pre-existing issue.

**Fix (two parts, both tested, verified live):**
1. `_rehydrate_exposure_book` now takes the allowlist and **skips positions on non-quoted series**
   (`rehydrate_skipped_gated_series`) — a gated-off-sport position has no subscribed books and doesn't
   interact with the quoted sport's caps.
2. `risk/exposure.py snapshot()`: a **committed** position whose marginal is unavailable now
   contributes its **known max_loss** to the loss/notional/game caps but **does NOT set
   `unknown_marginals`** — only a **candidate / open-quote** we cannot decompose fails closed. (One
   un-pricable *held* position must never veto quoting on unrelated combos.) Tests updated:
   `test_exposure.py` (committed-doesn't-block vs candidate-fails-closed), `test_limits.py`.
3. Also added a gated-series rehydration test. **Live-verified:** on the fixed run
   `skip_classifier_unknown` fell from **7,186 → 38**.

**Where it stands after the fix (still not productively quoting — stated plainly).** With the poison
removed, the binding reason became **`skip_mass_acceptance_breach` (~88%)** — the **ENFORCED**
hard-dollar/contract caps (`max_event_delta_contracts` / `max_market_delta_contracts` /
`max_gross_notional` / `max_event_worst_case_loss`), a **pre-existing layer I did not change**, firing
on the rehydrated concentrated ENG-ARG book (6 committed positions + each candidate under the
mass-acceptance worst case). The interim config raised only the **R2 %-caps** (game/slate to 20%); the
enforced hard caps are untouched, and Stage-B's mutex-netting applies to the loss caps but not to the
contracts-**delta** caps. This is arguably correct-but-conservative, not a crash — but it means the bot
still issues 0 quotes on this book.

**Operational note (my fault).** Repeated relaunch/kill cycles orphaned the joint-pool worker
processes (8 `multiprocessing` children), which spun and the operator had to kill them for CPU. Clean
shutdown is via the **KILL file** (the app stops its own workers); `pkill` on the parent orphans them.
I stopped churning the live bot after this and shut it down cleanly (0 processes remaining).

**Honest conclusion.** The risk-engine upgrade is **built, unit-tested (suite 1788/0), and
live-deployed**; the #33 regression is **found and fixed (verified live)**; but **productive live
quoting was NOT achieved** — it is now blocked by the enforced mass-acceptance caps on the rehydrated
book, and the World Cup is at its last semifinal (little fresh pregame flow). Getting to productive
quoting needs enforced-cap tuning and/or a fresh slate — done **deliberately by the operator**, not by
further autonomous churn on a live money account.

**Follow-ups (P0):** (a) tune / mutex-adjust the enforced mass-acceptance delta/notional caps for a
book that legitimately holds one game's positions; (b) reconsider whether rehydration should also drop
positions whose games are already settled; (c) validate the balancing behaviour on a fresh slate.

---

## 9. Reproduction / audit pointers

- **Code changed:** `risk/exposure.py` (mutex cap **+ the committed-marginal fix, §8**),
  `ops/quote_app.py` (#33 **+ the allowlist-filter fix, §8**) + `ops/persistence.py` (#33),
  `sim/structural_book.py` (new — A1), `sim/book_risk.py` (structural seam + p_ruin), `risk/limits.py`
  + `core/reasons.py` (ruin cap), `rfq/lifecycle.py` (wiring), `ops/config.py` (config), `core/money.py`
  + `risk/settlement.py` (sub-cent fee fix). Config: `config/prod-live-wc.local.yaml`.
- **Tests:** §4 files + `test_exposure.py` / `test_limits.py` (committed-marginal semantics) +
  `test_rehydrate_positions.py` (gated-series) + `test_money.py` (fee). Run `uv run pytest -q`
  (**1788 pass, 0 fail**), `uv run mypy <files>` (clean), `uv run ruff check <files>` (clean).
- **Relaunch command (operator, when ready):** `uv run combomaker run --env prod --mode quote
  --confirm-live --config config/prod-live-wc.local.yaml`. **Stop via the KILL file**
  (`touch KILL`) — never `pkill` the parent (orphans the 8 joint-pool workers).
- **Analyses:** `tools/market_vs_our_pricing.py`, `tools/market_drift_check.py`,
  `tools/proto_mutex_game_cap.py`, `tools/proto_structural_book_mc.py`.
- **Prior reports (context):** `2026-07-14-market-vs-our-pricing-main-combos.md`,
  `2026-07-14-fill-blocker-is-comonotone-risk-cap-not-price.md`,
  `2026-07-15-risk-engine-structural-mc-research.md` (the design + an adversarial critique).

## NEXT STEPS

- **Owner: next agent (P0).** Fix the §8 interaction (marginal fallback / MC-exclusion for
  unavailable-marginal rehydrated positions; consider dropping settled/started positions on
  rehydration). Until then the live bot fail-closes on any book it can't fully price.
- **Owner: next agent.** Build the deferred per-candidate marginal-CVaR/ΔES gate (§6); resolve the
  `operative_es` comonotone-masking so the CVaR cap can also see hedges.
- **Owner: operator.** Confirm the ruin budget (5% at −30% floor) and the interim 20% game/slate cap
  against pooled multi-week evidence — never refit on one snapshot.
- **Owner: calibration.** Measure corners × {ML, total, spread, advance} tetrachoric ρ from
  co-settlements (the only same-game pairs the structural MC never covers for free).
