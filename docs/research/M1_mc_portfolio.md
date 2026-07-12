# M1 — Monte Carlo for Portfolio / Book Risk

Architecture + integration design. Enhancement (not greenfield) of the existing
`sim/engine.py` book-MC and the existing `ops/report.py` `_portfolio_mc`, made
consistent with the pricing copula (`pricing/sgp.py` + `pricing/joint.py`) and
wired into the risk engine (R1 exposure/limits, R2 last-look/quote gate, R3
inventory skew).

Money is int centi-cents everywhere except inside the simulator, which works in
float cc by design (`sim/engine.py:8-12`, module allows float cc / floats in
probability space per CLAUDE.md hard rule 5).

---

## 0. What already exists (the enhancement targets)

| Component | File:line | What it does today | Gap M1 closes |
|---|---|---|---|
| Book MC engine | `sim/engine.py:225-237` `simulate()` | Vectorized Gaussian-copula MC over the WHOLE book of `ComboPosition`s at once; produces EV, std, p_profit, VaR/ES at 0.95/0.99, `p_loss_worse_than`, raw `pnl_samples` (`PortfolioStats`, `:102-119`) | It is never fed the real book or the pricing correlation matrix. Inputs are hand-built. |
| Marginal impact | `sim/engine.py:240-258` `marginal_impact()` | (book without, book with candidate) on **common random numbers** — a low-variance with-minus-without delta | Not called from the quote path; no ΔCVaR extracted; no skew feedback |
| Leg deltas (slow) | `sim/engine.py:261-285` `leg_deltas()` | Conditional-resampling delta of one position to each leg (cc per 1.0 of leg value) | Reserved for "slow full-book refresh" (E1, NOTES.md:241) but nothing calls it |
| Standing portfolio MC | `ops/report.py:50-106` `_portfolio_mc()` | Builds `LegModel`s from `exposure.positions`, runs `simulate()` **with `corr = np.eye` (independence)** and **complement pseudo-legs for NO** (`:97`, `:83-88`) | **This is the inconsistency.** Report MC uses independence corr; the *pricer* uses `build_sgp_correlation`. Risk view ≠ fair. F8 audit row (NOTES.md:263) flags it as "approximation, documented". |
| Exposure book | `risk/exposure.py:131-223` `ExposureBook` | Holds `positions: dict[str,OpenPosition]` + `open_quotes: dict[str,OpenQuoteRisk]`; analytic independence deltas (`:93-118`); mass-acceptance worst case (`:184-214`) | Only analytic/independence risk; no joint tail |
| Limits | `risk/limits.py:50-156` `LimitChecker.check` | Per-quote, per-market/event delta, gross notional, per-event worst-case loss, daily-loss halt — all analytic + mass-acceptance | No portfolio VaR/CVaR limit; no marginal-CVaR gate |
| Last look | `risk/lastlook.py:54-98` `decide_confirm` | Pure function; consumes `risk_breaches: tuple[str,...]` (`lastlook.py:44`) from `LimitChecker` | Can carry an MC-CVaR breach string with **zero new plumbing** |

The single most important fact: **`sim/engine.py` is ALREADY a portfolio engine.**
`_book_pnl` (`sim/engine.py:185-192`) sums every position's P&L on the *same*
sampled leg-value matrix, so cross-position correlation through shared legs is
already captured. What is missing is (a) feeding it the *real* book, (b) feeding
it the *pricing* correlation instead of `np.eye`, and (c) reading tail
statistics back into R1/R2/R3.

---

## 1. The consistency requirement (why the risk must use the pricer's joint model)

The mc-vs-analytic split (memory `project_kalshi_combos_mc_vs_analytic.md`):
**calibration uses the analytic grid; risk/profitability uses MC.** M1 lives on
the MC side. But the MC must sample from the *same* joint the pricer prices
against, or the risk is inconsistent with the fair we quoted — we would be
managing a different book than we sold.

The pricer's joint, per combo, is:

```
legs → build_sgp_correlation(legs, same_event_groups, SgpParams, marginals)
       → (corr, corr_low, corr_high)   [three PSD YES–YES matrices]
       → price_joint_matrices(beliefs, sides, corr, corr_low, corr_high)
       → JointEstimate.p                [the fair]
```
(`pricing/engine.py:228-238`, `pricing/sgp.py:792-1227`, `pricing/joint.py:66-103`.)

Two structural facts the MC must mirror exactly:

1. **Cross-game independence, within-game typed correlation.** `build_sgp_correlation`
   fills off-diagonal with `cross_event_rho` (config default 0.0,
   `config.py:145`) and overrides ONLY same-event pairs with the typed prior
   (`sgp.py:814-816`, `:835-839`). So the book-wide latent correlation matrix is
   **block-diagonal by game** with 0 between games. This is the "true risk unit
   is the GAME/match" fact from the sweep, encoded directly.

2. **NO-side = latent sign flip, NOT a complement pseudo-leg.** The pricer models
   a NO leg by flipping the sign of that leg's latent variable and conjugating
   the correlation by `diag(±1)` (`pricing/joint.py:53-63`, `:84-89`;
   `sim/engine.py` copula flip is the mirror). The current report MC instead
   invents a `~ticker` pseudo-leg with `p = 1-p` and independence
   (`report.py:83-88`) — which **destroys the correlation of the NO leg with the
   rest of its game**. For a sell-only parlay seller (every position is NO,
   CLAUDE.md current state) this is not a corner case, it is *every* position.
   Fixing it is the core of M1.

### The bridge: one book-wide correlation matrix built the pricer's way

The enhancement is a new module `sim/book_model.py` (proposed) exposing a pure
builder that turns the live book into the MC's `(legs, corr, positions)` triple
using the *pricing* machinery — no reimplementation (CLAUDE.md hard rule 8:
import and call the live module, never copy it):

```
build_book_model(
    positions:  Iterable[OpenPosition],          # risk/exposure.py:41
    open_quotes: Iterable[OpenQuoteRisk] = (),    # optional, for mass-acceptance MC
    *,
    marginals:  MarginalProvider,                 # risk/exposure.py:30  (ticker→P(YES))
    beliefs_of: Callable[[str], LegBelief|None],  # per-leg uncertainty, pricing/legs.py
    sgp_params: SgpParams,                         # the SHIPPED config, pricing/engine.py:103
    same_event_groups_of: Callable[[OpenPosition], tuple[tuple[int,...],...]],
) -> BookModel(legs, corr, positions, leg_index, unknown)
```

Construction (the load-bearing part):

- **Global leg universe.** Assign each distinct `market_ticker` in the book one
  latent index. Each leg is ONE `LegModel` on its **YES marginal**
  (`LegModel(p=marginals(ticker))`, `sim/engine.py:34-51`) — never a complement
  pseudo-leg. NO-side selection is expressed in the *position*, not the leg
  (point 2 above); see §2.
- **Block-diagonal global corr.** Start `corr = np.eye(n_legs)`
  (`copula.build_block_corr` semantics, off-diagonal = `cross_event_rho`,
  `copula.py:218-251`). For every game (event_ticker) present, take the legs of
  that game and call the **real** `build_sgp_correlation` on just those legs with
  their `same_event_groups`, then scatter its `.corr` block into the global
  matrix at those indices. Because different games never share a leg and cross
  blocks stay at `cross_event_rho`, the assembled matrix is exactly what the
  pricer would produce if you priced a hypothetical mega-combo of the whole book.
- **PSD repair once, globally.** `copula.is_psd` / `copula.nearest_psd`
  (`copula.py:95-127`) on the assembled matrix — the same repair the pricer uses
  per-combo (`sgp.py:1217-1218`). Cross-game 0s keep it near-PSD; repair only
  touches numerical noise.
- **Point vs band.** Build THREE global matrices from `.corr` / `.corr_low` /
  `.corr_high` (the pricer already returns all three, `sgp.py:1220-1226`) so book
  risk can be reported at the correlation-uncertainty band, not just the point
  estimate — the risk analogue of `price_joint_matrices`' width
  (`joint.py:90`). CVaR at `corr_high` is the number that gates.

**Parity check (CLAUDE.md hard rule 8, mandatory before trusting the port):** a
1-position book of a single combo, run through `build_book_model` + `simulate`,
must reproduce that combo's `JointEstimate.p` as the MC estimate of
`P(all legs on selected side)` to MC tolerance, AND the position's EV must match
`(side_fair − price)×contracts` to the cent using the same fair the engine
quoted. This is the gate that proves the risk sim and the pricer share a joint.

---

## 2. How the book state feeds the sim (NO-side handled correctly)

`OpenPosition` (`risk/exposure.py:41-59`) already carries everything:
`legs: tuple[LegRef,...]` (each `LegRef` has `market_ticker, event_ticker, side`,
`:34-38`), `our_side: Side`, `contracts`, `entry_price_cc`, `farmed`.

Mapping `OpenPosition` → `sim.ComboPosition` (`sim/engine.py:73-99`):

- `leg_indices` = the global latent index of each leg's `market_ticker`
  (YES-marginal leg; §1).
- **Per-leg NO handling — this is the fix.** `sim/engine.py`'s `ComboPosition`
  computes payout as `prod(cols)` over the leg *values* (`sim/engine.py:176`),
  i.e. it assumes every referenced leg contributes its YES value. To represent a
  leg selected NO *while keeping its within-game correlation*, the value used
  must be `(1 − leg_value)`, not a separate independent leg. Two clean options,
  both keeping `sim/engine.py` pristine:

  **(a) Per-position selected-side vector (preferred, minimal engine change).**
  Extend `ComboPosition` with `leg_sides: tuple[Literal["yes","no"],...]` and, in
  `_position_pnl`, use `v if side=="yes" else 1.0−v` before the product. This is
  a ~2-line change to `_position_pnl` (`sim/engine.py:171-182`) that must be
  prototyped in a test harness, parity-checked, then ported (hard rule 8). It is
  the exact latent-sign-flip the pricer already does, expressed on the sampled
  value instead of the latent Z — algebraically identical for binary legs
  (`1 − 1[Z≤t] = 1[−Z ≤ −t]`), and correct for graded settlement legs too.

  **(b) No engine change: build a signed-latent block.** Keep `ComboPosition`
  as-is but, when a game contains any NO-selected leg, conjugate that game's
  block by `diag(±1)` and store the NO leg's `LegModel` on its complement
  `p=1−p` — the pricer's `_signed_corr` move (`joint.py:53-63`). This reproduces
  the report's pseudo-leg BUT with the correlation preserved (the flip is applied
  to the correlated block, not dropped to independence). More faithful to
  `joint.py` but forks a leg per selected side; (a) is cleaner and is the
  recommendation.

- `side` = `"yes"`/`"no"` from `our_side` (`report.py:92` already does this map).
  For sell-only, every position is `"no"`: NO pays `$1 − payout` per contract
  (`sim/engine.py:180`) — matching `combo_no_pays_complement=true` (promoted,
  CLAUDE.md current state; the demo $1.00 settlement).
- `contracts` = `int(position.contracts)//100` (centi-contracts → contracts;
  `report.py:93`, keep the `max(1, …)` guard).
- `price_cc` = `entry_price_cc`; `fee_cc` = reconciled fee if known, else 0
  (fees are reconciled from the ledger, `lifecycle.py:377`).
- **Farmed positions** (`OpenPosition.farmed`, `:54`; the certain-NO
  impossible-combo shorts): model their legs honestly through the copula. Their
  joint should already sit at the Fréchet floor (impossible ⇒ P(YES)≈0), so MC
  NO-payout ≈ $1 — a near-riskless winner in the tail, exactly right. The
  settlement guard (`lifecycle.py:397-427`) remains the tripwire if one ever
  settles YES.

**Candidate RFQ + mass-acceptance.** For the marginal-risk question (§4.5), the
candidate is one extra `ComboPosition` built the same way from the hypothetical
fill (`OpenQuoteRisk.hypothetical_positions`, `exposure.py:73-90`). For a
mass-acceptance MC (the "one-sided buildup" danger the sweep flagged), feed all
`open_quotes`' worse-side hypothetical positions as extra positions — the MC
analogue of `ExposureBook.snapshot(mass_acceptance=True)` (`exposure.py:184-214`),
but now with joint tails instead of a sign-aligned magnitude bound.

---

## 3. Two-tier architecture: fast quote-time MC + full book-review MC

```
                        ┌─────────────────────────── FULL BOOK MC (batch) ───────────────────────────┐
   book state           │  every maintenance_tick (lifecycle.py:455) or every N accepts / T seconds:  │
   (exposure.positions) │   build_book_model(all positions, corr_high band)                           │
        │               │   simulate(n=100k–250k, seed=fixed)          → PortfolioStats               │
        │               │   + per-game / per-leg tail contribution      (§4.3, §4.4)                   │
        │               │   → dashboard + persisted BookRiskSnapshot + limit-refresh                   │
        │               └────────────────────────────────────────────────────────────────────────────┘
        │
        │   handle_rfq / on_quote_accepted (hot path, <~2ms budget of the 3s HVM window, lastlook.py:6)
        ▼
   ┌──────────────────── FAST MARGINAL MC (real-time) ─────────────────────────┐
   │  marginal_impact(cached book legs+corr, candidate, n=5k–20k, CRN, seed)    │
   │    → (stats_without, stats_with) on COMMON RANDOM NUMBERS (engine.py:249)  │
   │    → ΔCVaR_cc = with.es_cc[0.99] − without.es_cc[0.99]                      │
   │    → feeds R2 gate (accept/decline) + R3 skew (inventory_skew_cc)          │
   └───────────────────────────────────────────────────────────────────────────┘
```

**Why two tiers.**

- **Full MC** answers "what is our book's tail right now" — VaR/CVaR, P(large
  drawdown), per-game/per-leg tail attribution. It is O(n_samples × n_legs) and
  runs off the hot path. 100k samples over a few-hundred-leg universe is tens of
  ms in numpy — fine for `maintenance_tick` (already "every few 100ms",
  `lifecycle.py:455`), but NOT for the per-accept last-look budget.

- **Fast marginal MC** answers only "does *this* candidate raise portfolio CVaR"
  — and does it cheaply via `marginal_impact` on **common random numbers**
  (`engine.py:240-258`): the with/without books share the sampled matrix, so the
  *difference* has tiny variance and needs far fewer samples (5k–20k) than an
  absolute CVaR estimate would. The book's `(legs, corr)` are cached from the
  last full MC and reused; only the candidate column(s) are appended. This is the
  key trick that makes MC affordable at quote time: we never re-estimate absolute
  CVaR on the hot path, only its *increment*.

**Caching / staleness.** The fast tier reuses the cached book model; marginals
drift between full rebuilds. Bound this the way last-look already bounds leg age
(`lastlook.py:73` `max_leg_age_s`): if the cached model is older than a small TTL
or a leg mid has moved beyond a tolerance, the fast MC returns UNKNOWN and the
gate treats UNKNOWN as widen-or-no-quote (quiet-failure defense #2, CLAUDE.md).
Determinism: fixed `seed` per engine call (`engine.py:235`), so the same book +
candidate always yields the same ΔCVaR — reproducible decisions, testable.

---

## 4. The five key risk outputs

All come off `PortfolioStats` (`sim/engine.py:102-119`) plus two new
attributions. `pnl_samples` (`:119`) is the raw per-scenario book P&L vector that
every derived stat is computed from.

### 4.1 P&L distribution
`PortfolioStats.pnl_samples` + `ev_cc`, `std_cc`, `p_profit`
(`engine.py:113-115`, `199-201`). The dashboard shows the histogram and the
mean; the mission grades aggregate expected edge vs realized (CLAUDE.md), so EV
here is the model's forward P&L the realized ledger is graded against — with ±2σ
MC bands (`report.py:36`).

### 4.2 VaR / CVaR (tail loss)
`var_cc[0.95]`, `var_cc[0.99]`, `es_cc[0.95]`, `es_cc[0.99]`
(`engine.py:202-210`; ES = CVaR = mean loss at/beyond the VaR quantile, `:207`).
Reported at the **`corr_high`** band matrix so the gating number is
conservative under correlation uncertainty. **CVaR_0.99 is the headline book-risk
number** and the one that feeds a new limit (§5).

### 4.3 Probability of large drawdown / ruin
`p_loss_worse_than` (`engine.py:211-213`, `:117`) evaluated at operator
thresholds tied to bankroll: e.g. thresholds at 10%, 25%, 60% of capital (the
5%/25%/60% cap regime, memory `project_kalshi_combos_capital_constraints`).
`p_loss_worse_than[0.60×bankroll]` is the ruin proxy. The sweep's finding that
selling parlays ties up $23.5M max payout for $1.8M premium is exactly a
fat-left-tail statement — this output quantifies P(that tail bites) jointly
across all shared games, which the analytic per-event worst-case
(`exposure.py:191-192`) cannot (it sums worst cases as if independent).

### 4.4 Per-game and per-leg contribution to tail risk
NOT on `PortfolioStats` today — the one genuinely new computation. Both are cheap
because `pnl_samples` and the per-position P&L are already in hand:

- **Per-game tail contribution.** Define the tail set `T = {scenarios where
  book_pnl ≤ VaR_0.99 quantile}` (the same cut `es_cc` uses, `engine.py:206-207`).
  For each game `g`, `contrib_g = E[ Σ_{positions touching g} position_pnl | T ]`,
  computed by re-running `_position_pnl` (`engine.py:171-182`) on the tail rows
  and grouping by `event_ticker` (available on every `LegRef`, `exposure.py:36`).
  Σ_g contrib_g = CVaR by construction — an exact, additive tail decomposition
  that names the games carrying the loss. This is the direct answer to the
  sweep's "single combos carried ~$1M payout swings; the true risk unit is the
  GAME."
- **Per-leg tail sensitivity.** `leg_deltas` (`engine.py:261-285`) already gives
  ∂(position pnl)/∂(leg value) by conditional resampling; run it **restricted to
  the tail scenarios** (or on the whole book) to rank legs by how much moving each
  leg's outcome moves the tail. Reserved-for-slow-refresh per E1 (NOTES.md:241) —
  the full-MC tier is exactly that slow refresh.

### 4.5 Marginal risk of a candidate RFQ (→ R3 skew)
`marginal_impact(book_legs, book_corr, book_positions, candidate, CRN)`
(`engine.py:240-258`) → `ΔCVaR = with.es_cc[0.99] − without.es_cc[0.99]`.

- **ΔCVaR ≤ 0** (candidate *reduces* or barely moves book CVaR — e.g. a NO
  position on a game we are under-exposed to, or one whose tail is anti-correlated
  with existing tail games): **risk-reducing**, quote tighter / skew toward it.
- **ΔCVaR > 0** and large (candidate piles onto an already-hot game — the
  concentration the sweep warned about): **risk-adding**, skew wider or decline.

Because it is on common random numbers, ΔCVaR is a low-variance estimate at small
n (§3). This single scalar is what R3 consumes.

---

## 5. How it plugs into R1 / R2 / R3

The design deliberately reuses existing seams so the MC feeds risk **without
re-plumbing** the hot path.

### R1 — Exposure & Limits (`risk/exposure.py`, `risk/limits.py`)
- **New limit fields** on `RiskLimits` (`limits.py:22-32`), same pattern as the
  existing ones: `max_portfolio_cvar_99_dollars`, `max_p_ruin` (P(loss > 60%
  bankroll)), `max_marginal_cvar_add_dollars`.
- **New breach checks** in `LimitChecker.check` (`limits.py:54-156`) reading the
  **latest full-MC `BookRiskSnapshot`** (batch tier), NOT re-running MC inside
  `check` (keeps `check` cheap and pure). If the snapshot is stale/UNKNOWN →
  breach (matches the existing `unknown_marginals` → breach at `limits.py:105-111`;
  UNKNOWN is never safe). Breach reason codes reuse the family already in place
  (`SKIP_MASS_ACCEPTANCE_BREACH` / a new `SKIP_PORTFOLIO_CVAR`).
- The analytic independence deltas + mass-acceptance bound (E1/E2) **stay** as the
  fast pre-filter; the MC CVaR limit is the joint-tail backstop layered on top.
  Analytic is the cheap necessary condition, MC is the accurate one.

### R2 — Last look & quote gate (`risk/lastlook.py`, `rfq/lifecycle.py`)
- `LastLookInputs.risk_breaches: tuple[str,...]` (`lastlook.py:44`) already flows
  from `LimitChecker.check` into `decide_confirm` (`lastlook.py:94-97`,
  `lifecycle.py:635-657`). A marginal-CVaR breach on the accepted candidate is
  just **one more string in that tuple** — decide_confirm declines with
  `DECLINE_RISK_LIMIT` (`lastlook.py:96`). **Zero new fields, zero new severity
  ordering** (E4, NOTES.md:244 stays intact).
- At **quote time** (`lifecycle.py:166-177`), the fast marginal MC runs alongside
  the existing `self._limits.check(...)`; if ΔCVaR exceeds the add-limit the RFQ
  is skipped with the CVaR reason (same `_record_skip` path, `lifecycle.py:173-177`).
- UNKNOWN marginals / stale cache → the MC declines to score → gate treats it as
  a breach (widen-or-no-quote). Fail-closed, consistent with `lifecycle.py`'s
  existing UNKNOWN handling.

### R3 — Inventory skew (`pricing/quote.py`, `pricing/engine.py`)
- `construct_quote` already takes `inventory_skew_cc` (`quote.py:128`,
  applied at `quote.py:195-196`: positive skew = we are long the joint event, bid
  less for YES / more for NO to attract flattening flow). `pricing/engine.py`
  threads it through (`engine.py:166`, `:266`). Today it is "0 until Phase 4
  wires it" (`quote.py:26`).
- **M1 supplies the number.** Map candidate **ΔCVaR (§4.5) → `inventory_skew_cc`**:
  risk-adding candidates (ΔCVaR>0, hot game) get positive skew (quote wider on the
  side that deepens exposure); risk-reducing candidates (ΔCVaR≤0) get zero or
  favorable skew. The mapping is a monotone, saturating function of ΔCVaR/bankroll
  (bounded so skew is a tilt, never an override — mirrors the width_multiplier
  floor at `quote.py:150`). This turns "manage concentration" from a hard
  accept/decline into a **priced** signal: we still quote the hot game, just at a
  premium that compensates for the marginal tail it adds — the maker's edge is
  execution discipline (CLAUDE.md mission), and skew is the discipline knob.

### Data / persistence
- New `BookRiskSnapshot` persisted each full-MC run (reuse the `Store` pattern
  behind `ops/persistence.py`, same as fills/markouts/ev_ledger). Fields: the five
  outputs (§4) + band (point/high) + `n_samples` + `seed` + `unknown` flag +
  per-game/per-leg contribution tables. Every stat carries its sample count
  (report discipline, `report.py:6`).
- `ops/report.py` `_portfolio_mc` (`report.py:50-106`) is **replaced** by a call
  to `build_book_model` + `simulate` (kill the `np.eye` at `:97` and the pseudo-leg
  block at `:83-88`). The F8 audit row (NOTES.md:263) flips from "independence
  corr, approximation" to "pricing copula, parity-checked", and a new assumption
  row records the book-MC model + its parity gate (end-of-phase audit, CLAUDE.md
  quiet-failure defense #6).

---

## 6. Correctness / quiet-failure defenses specific to the book MC

- **Same joint as the pricer or bust.** The parity check (§1) is a hard gate: a
  single-combo book must MC-reproduce the engine's `JointEstimate.p` and EV to the
  cent/MC-tolerance before the port is trusted (CLAUDE.md hard rule 8). Without it,
  the risk sim can silently drift from the fair — the exact quiet-failure species
  the mission defends against.
- **NO-side correlation preserved.** §2 fixes the report's independence-pseudo-leg
  bug; a mutation test: a 2-NO-leg same-game position must show the copula's
  correlated NO-NO tail, not the product-of-independent-complements tail.
- **Band, not point, gates.** CVaR at `corr_high` (§1) so correlation uncertainty
  widens risk, never hides it — the risk analogue of the pricer widening on the
  rho band (`joint.py:90`, `sgp.py` fail-safe band `:832`).
- **UNKNOWN ⇒ decline.** Any missing marginal / stale cache / non-PSD-after-repair
  → the MC returns UNKNOWN and the gate widens-or-no-quotes (defense #2). Never a
  `p=0.5` placeholder silently feeding stats (the current report does this at
  `report.py:66-67` and only flags it — M1 must make UNKNOWN a hard no-score for
  gating, flagged-but-shown only on the dashboard).
- **Determinism.** Fixed seeds (`engine.py:235`) → reproducible CVaR and ΔCVaR →
  auditable decisions and regression tests.
- **Grading, not P&L-fitting.** The MC is a thermometer (forward EV + tail bands
  the realized ledger is graded against), never refit on a P&L window (memory
  `feedback_no_refit_on_pnl`). Correlations come only from the config table the
  pricer already uses; the MC never invents its own.

---

## 7. Summary diagram

```
  LIVE BOOK                         PRICING JOINT (unchanged, imported)
  exposure.positions ─┐            build_sgp_correlation(sgp.py:792)  ── per-game blocks
  exposure.open_quotes│            price_joint_matrices(joint.py:66)  ── parity anchor
                      ▼                         │
        sim/book_model.py  build_book_model ◄───┘  (block-diagonal global corr,
        (NEW, pure)         legs=YES marginals, NO = per-position side flip §2)
                      │  (legs, corr[point/low/high], positions)
                      ▼
        sim/engine.py  simulate() / marginal_impact() / leg_deltas()   (EXISTING, ~2-line
                      │   PortfolioStats + per-game/leg tail attribution  §2(a) port to _position_pnl)
        ┌─────────────┼──────────────────────────┐
        ▼             ▼                           ▼
   FULL MC (batch)  FAST MARGINAL MC (hot)   dashboard + BookRiskSnapshot
   §4.1–4.4         §4.5 ΔCVaR (CRN)          (persistence)
        │             │        │
        ▼             ▼        ▼
   R1 limits     R2 gate/    R3 skew
   (CVaR/ruin    lastlook    inventory_skew_cc
    limits)      (risk_      = f(ΔCVaR)
                 breaches)   quote.py:128
```

Net: the engine and the pricer's joint model already exist and are already
portfolio-aware; M1 is the **bridge module + a ~2-line NO-side fix in
`_position_pnl` + tail-attribution + three risk hookups on seams that already
accept them.** The deliberate independence approximation in `ops/report.py`
(F8) is retired in favor of the pricing copula, closing the risk-vs-fair
inconsistency.
