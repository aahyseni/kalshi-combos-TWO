# Risk Engine — Complete, Self-Contained Breakdown (for return simulation)

**Date:** 2026-07-22
**Repo:** `kalshi-combos-TWO` ("combomaker")
**Audience:** a fresh Claude instance that has never seen this repo.

## 0. What this is

This is the definitive, standalone description of the **risk engine** of *combomaker* — a
**maker-side, sell-only** automated market maker for Kalshi combo/parlay RFQs. After reading
this document you should be able to (a) reason about every layer of the risk engine and its
enforcement order, and (b) **build a Monte-Carlo simulation of the strategy's potential returns**
from the numbers, formulas, and the caps here — without opening the source. Section 11 is a
dedicated, self-contained simulation recipe with pseudocode and a worked P&L example.

The system is **SELL-ONLY**: it sells the **NO** side of multi-leg sports parlays. Being long NO
means it *collects a small premium* and *keeps it* when the parlay MISSES (the common case), and
*forfeits the premium* when the parlay HITS. The stated edge is **execution discipline + risk
management, not pricing** (pricing is top-down: Kalshi leg marginals × a correlation/joint layer).
Sports: World Cup soccer + MLB (leg-series allowlist `KXWC` + `KXMLB`). Capital: a single **$2,000**
deposit; true all-time realized profit **$384.77** (reconciled to the cent). The bot is currently
**intentionally DOWN** between sport seasons on a human-only KILL.

All money is **integer centi-cents (cc)** outside the Monte-Carlo simulator: `1 cent = 100 cc`,
**`$1.00 = 10,000 cc`** (`core/money.py`). Probabilities are floats in `[0,1]` (a separate space).

---

## 1. EXECUTIVE MODEL — the strategy in 12 bullets

- **Sell-only NO parlays.** The book only ever ends up **LONG NO** on a combo. `sell_parlays_only`
  forces `yes_bid = 0` (`pricing/quote.py`); the engine boundary `_enforce_sell_only` is the
  belt-and-suspenders. A combo NO contract pays **`$1 − Π(leg values)`**.
- **Where the money comes from.** On a MISS (parlay fails, the common case) we **keep the premium**;
  on a HIT we **pay out** `$1 − V` per contract net of the premium collected. The parlay tape is
  roughly *fair* (price ≈ realized hit frequency), so a naked-fair maker has ~zero edge — **the edge
  is entirely the markup** we add on top of fair, plus refusing toxic flow.
- **Where the edge actually is (measured).** The +EV is **same-game FAT flow only**: same-game
  "FAT" combos (room > 2¢ over our fair) showed **+10.1pp** real edge; multi-game exotic FAT showed
  **−1.2pp** (no edge / adverse). An **independence pricer LOSES money at every markup** (−31% to
  −40% ROI) — the correlation model is the moat.
- **Money mechanics per fill.** `premium_paid_cc = contracts × entry_price_cc // 100`;
  `realized = contracts × ((1 − V) − entry_price) − fee`. `V` is the combo's realized YES value
  (product of leg values, **scalar** under DNP/rain/void, not just {0,1}).
- **Fees: eat-the-fee.** Quadratic Kalshi fee `coef × C × P × (1−P)`; maker on quadratic combo
  series is **$0** today, but the code conservatively prices with the **taker** coef `0.07` until the
  maker-side convention is fixture-verified. Fees are **accounted, never added to quote width**.
- **The risk unit is the GAME, not the ticker or the combo.** Combos share legs and games; within
  one game leg outcomes are maximally correlated. Every exposure aggregate keys on
  `pricing.grouping.game_key` (`risk/exposure.py`). Different games are ~independent.
- **Two money axes, NEVER summed.** `max_loss_cc` (premium at risk = true LOSS) vs
  `gross_settlement_notional_cc` (contracts × $1 = capital-utilization). Loss caps bind on premium;
  utilization/tail caps bind on notional (`risk/exposure.py`, R1/R2 invariant #2).
- **Fills are gated by CAPS, not price.** We are the **sharp maker** (cheapest on 6/7 filled combos,
  ~0.75¢ under market clearing on 26 liquid mains). Fills are blocked at last-look by the
  **per-game correlated-loss cap** ("comonotone cap"), not by mispricing.
- **Caps are auto-scaling fractions of live bankroll.** Every cap = `frac × risk_bankroll_cc`
  recomputed per check (`risk/limits.py::threshold_cc`); absolute knobs are backstops only.
  No hand-tuned numbers (operator directive 2026-07-19).
- **Enforcement is layered and fail-closed.** Quote-time `LimitChecker` → single-writer reservation
  → last-look pure gate → candidate portfolio-MC gate → fill-velocity → confirm → book → settlement
  ledger reconciled to the cent (HALT on any mismatch).
- **Capital base.** Single **$2,000** deposit, no withdrawals. **True all-time profit $384.77**
  (equity-anchored: exchange equity **$2,384.77** − $2,000). The 56 settled markets net
  revenue $1,803.46 − cost $1,412.76 − fees $5.91 = **$384.79 realized**; the **−$0.02** residual
  vs the equity figure is order-time trading fees not carried in settlement rows. Current live
  equity **$2,384.77** (all cash, zero positions). The World Cup campaign booked **74 fills /
  ~$720 premium**, final-match net **+$234.09** (71 winners +$263.60, 3 losers −$29.51).
- **Current state: intentionally DOWN.** A **false-positive** hard-trip KILL fired 2026-07-19 during
  a settlement cascade (measured give-back $430.69 vs actual losers $29.51). Relight is gated on the
  operator saying "clear it"; then the MLB/WNBA sport switch. **The markup/edge is still
  UNVALIDATED** — the profitability gate (a pooled multi-week markout study) has not been reached.

---

## 2. THE FULL PIPELINE

```
                          KALSHI  COMBO-RFQ  MARKET  MAKER  —  HOT PATH + OUT-OF-BAND LOOPS
 ============================================================================================================

  RFQ arrives (~170/sec)                                                            owning module
        |
        v
  +-------------------------+   series allowlist [KXWC,KXMLB], pregame-only,
  | INTAKE + FILTERS        |   min/max legs 2..6, feed freshness <=5s, TTC >=1h    rfq/intake.py, rfq/filters.py
  | + PREGAME GATE          |   in-play leg => decline (allow_inplay_legs=false)    (PregameGate start-time ladder)
  +-----------+-------------+
              | (optional) F1 pre-pricing monotone gate: pre-decline if already
              |            over a candidate-monotone cap BEFORE paying to price     risk/limits.py monotone_pre_quote_breaches
              v
  +-------------------------+   fair = top-down: Kalshi leg marginals x Dixon-Coles /
  | PRICING (fair + width)  |   copula joint. no_raw = ($1-fair) - half - fee_no    pricing/engine.py, pricing/quote.py
  | + SKEW (DARK)           |   + inventory_skew_cc(=0 dark). Snap DOWN, arb-clamp. pricing/markup.py, risk/skew.py
  +-----------+-------------+
              |
              v
  +=========================+   ONE authoritative check(). Mass-acceptance worst
  | QUOTE-TIME RISK GATE    |   case: assume EVERY open quote + this candidate       *** risk/limits.py::LimitChecker.check
  | LimitChecker.check      |   fills NOW on its worse side. apply_resting_haircut.  reads risk/exposure.py snapshot()
  +-----------+-------------+   Any enforced breach => no quote (reason logged).     + sim/book_risk.py snapshot (CVaR)
              | pass
              v
  +-------------------------+
  | QUOTE POSTED (no_bid)   |   rests <=20 open, TTL ~20s                            rfq/lifecycle.py handle_rfq
  +-----------+-------------+
              |
              v   ...taker ACCEPTS our resting NO quote (the "money path")...
              |
  +-------------------------+  1. sanity lapses (side/bid/qty/convention unknown)
  | CONFIRM PATH            |  2. LAST LOOK  decide_confirm (pure, severity order)   risk/lastlook.py
  | (fixed gate order)      |  3. set pending_fill + record FILL-VELOCITY            risk/fill_velocity.py
  |                         |  4. RESERVE headroom (single-writer, atomic, versioned)risk/reservation.py
  |                         |  4a. (opt) last-look MC WAIVER retry on denial [OFF]   sim/state_worst_case.py
  |                         |  5. CANDIDATE portfolio-MC gate (+EV & post-book ES)   sim/book_risk.py evaluate_candidate_book_risk
  |                         |  6. confirm_quote REST POST                            exchange/rest.py
  |                         |  7. COMMIT reservation -> book OpenPosition            risk/reservation.py commit
  |                         |  8. drop the resting quote                             rfq/lifecycle.py
  |                         |  9. (opt) post-fill risk-evict pull [DARK]
  +-----------+-------------+
              | committed
              v
  +-------------------------+   exchange settles -> V in [0,1]; NO pays contracts*(1-V);
  | SETTLEMENT LEDGER       |   reconcile predicted vs exchange TO THE CENT.         risk/settlement.py, risk/balance.py
  | (paper/quote mode)      |   any mismatch => HALT_RECONCILIATION_MISMATCH.        (SettlementPoller GET /portfolio/settlements)
  +-------------------------+

 ------------------------------------------------------------------------------------------------------------
  ALWAYS-ON, OUT OF BAND (do not touch the hot path):

  * MAINTENANCE TICK ~0.5s : mark P&L (daily-loss cap) -> off-loop book-risk MC refresh (15s throttle)
    -> settled-marginal resolve -> receivables refresh -> fill-record recovery sweep -> give-back /
    daily-loss / hard-trip HALT escalation -> reprice/TTL sweep (wedge-hardened).          rfq/lifecycle.py maintenance_tick
  * STATUS / BREAKERS ~15s  : 7 circuit breakers (fail-closed), evaluate_and_halt.          risk/breakers.py
  * BALANCE ~10s (stale 30s), SETTLEMENT ~30s, RESERVATION-RECONCILE ~15s,
    POSITION-RECONCILE ~300s, TRANSFER-WATCH ~60s, REPORT ~300s.                            ops/quote_app.py loop set
  * SafetySupervisor (SEPARATE PROCESS) : reads data/heartbeat.txt; missed beat (15s) =>
    cancel-all via own credential + write KILL file + drop needs_reconcile marker.          ops/supervisor.py
  * GO-LIVE GATES : prod requires --confirm-live + prod_limits_configured + non-empty
    allowlist + supervisor beating + book reconciled (else PreflightError).                 ops/preflight.py, ops/config.py
 ============================================================================================================
```

Every quote runs the quote-time gate **once**; every accept runs the confirm-path gate order
**exactly** (order is load-bearing: reservation before candidate-MC so two concurrent accepts can
never pass the same MC against the same pre-book — the P0-2 atomicity fix).

---

## 3. EXPOSURE MODEL (`risk/exposure.py`)

### 3.1 Two money axes, NEVER summed

The book decomposes every position/quote to per-leg deltas, folds them per market and **per GAME**,
and tracks two orthogonal axes (R1/R2 invariant #2):

| axis | formula (per position) | meaning | caps that bind on it |
|---|---|---|---|
| **LOSS** `max_loss_cc` | `int(contracts) × int(entry_price_cc) // 100` | premium PAID = true max loss for a long-NO seller (forfeits premium if the parlay HITS; the taker collects the $1 from posted collateral) | daily-loss, game-loss, per-combo, slate, drawdown, hard-trip, CVaR |
| **UTILIZATION** `gross_settlement_notional_cc` | `int(contracts) × 10000 // 100` = contracts × $1 | gross settlement notional (the "$23.5M payout for $1.8M premium" dimension) — NOT loss, NOT a cash lock | ONLY the utilization backstop (`absolute_notional_multiple`) |

**Verified ground truth (2026-07-10 demo):** a LONG NO of 1.00 contract bought at $0.50 loses
**exactly $0.50** if the parlay HITS (not $1). `max_loss_cc = $0.50`, `gross_settlement_notional_cc
= $1.00`. Summing the two axes re-introduces the give-back the adversarial review caught. Both use
integer `//100` **truncation** (contracts stored ×100), not rounding.

### 3.2 The GAME is the risk unit

Every per-event aggregate keys on `game_key(event_ticker)` = the gamecode after the series prefix
(`SERIES-GAMECODE -> GAMECODE`; no hyphen -> whole string, fail-closed). All of a match's market
families (GAME / TOTAL / SPREAD / props / 1H period) fold into **ONE** game cluster — the exact key
the copula correlates on. `game_key` resolves pricing event aliases (e.g. a champion event joins the
final's game). Legs with `event_ticker=None` are skipped from partitioning.

### 3.3 Mass-acceptance worst case + subset dominance (E2)

Because every resting quote is instantly executable and a taker aimed at a competitor can land on us
(FIX PreferBetterQuote), the enforced worst-case book assumes **every open quote fills NOW**, each on
whichever side is **worse** for the aggregate being checked (sign-aligned magnitudes — a conservative
**upper bound**, never an average). The construction is **monotonic**: adding an open quote never
lowers any aggregate, so the all-accepted snapshot **dominates every realizable accepted subset**.
This is why the quote-time analytic caps must stay comonotone/monotone and the tighter state-exact
enumeration is confirm-path only.

### 3.4 Worst-case loss per game (the enforced formula)

For a game `g`:

```
worst_case_loss_by_game_cc[g] = _mutex_game_worst_cc(entries, is_me_event)

entries = [(this-game legs, position.max_loss_cc, requires_all)] for every committed position,
          every candidate, and (under mass acceptance) every open quote's worse-side hypothetical.
requires_all = (our_side is Side.NO)   # a long-NO combo LOSES iff EVERY leg is satisfied (parlay HITS)

if is_me_event is None  OR  the game has 0 or >=2 explicit-True mutually-exclusive (ME) events:
    bound = COMONOTONE SUM of all entry losses          # every combo on the game resolves adverse together
elif exactly ONE explicit-True ME event (advance / 1X2 moneyline):
    bound = max over branches b of  Σ{entries that can lose in branch b} loss_cc
            # branches = each required YES-outcome + an __OTHER__ catch-all
            # opposing long-NO outcomes (ARG-advance NO + ENG-advance NO) land in DIFFERENT branches => they NET

# INVARIANT:  comonotone-sum  >=  bound  >=  largest single entry ;  monotone (E2 dominance holds)
```

The mutex netting nets **exactly one** result-type ME event and **fails closed** to the comonotone
sum on 0 or ≥2 ME events (recognizing a 2nd ME event would lower the bound non-monotonically and open
a taker cherry-pick hole). Live example (ENGARG, 3:1 ARG-skewed book): comonotone **$982** →
mutex-aware **$820** (1.20× tighter). A directional analog (`directional_by_game_cc`, P0-9) applies
the same single-ME fold to `|Σ this-game leg deltas| × $1`.

**Conservatively-reserved holdings** (`risk_modeled=False`, e.g. a gated-off series position) still
count their exact `max_loss_cc` + notional + per-game concentration in every deterministic/gross/game
cap, but are **never decomposed against marginals** (delta=None, no directional entry) and are held
**outside** the model ES — a deterministic reserve, never a fabricated `p=0.5` leg.

---

## 4. THE CAP HIERARCHY (`risk/limits.py`, `ops/config.py`)

### 4.1 Equity-aware bankroll denominator (`risk/balance.py`)

Every `frac`-cap divides by the live risk bankroll:

```
risk_bankroll_cc = min( start_of_day_equity_cc ,  available_cash_cc + haircut × portfolio_value_cc )
                   haircut default = 1/2  (DEFAULT_PORTFOLIO_HAIRCUT, applied ONLY to portfolio_value, never cash)
exchange_equity_cc = available_cash_cc + portfolio_value_cc
threshold_cc(frac, bankroll) = frac.numerator × bankroll_cc // frac.denominator   (integer-exact, no float money)
```

The `min()` does two jobs: the right term keeps the denominator ~flat when capital is merely
**deployed** (cash falls, mark rises — deployed ≠ lost); the left term stops an intraday
**mark-to-model gain** from inflating caps. **Stale or un-anchored bankroll ⇒ `StaleBalanceError` ⇒
every %-cap fails closed** (`SKIP_BANKROLL_UNAVAILABLE`, no-quote). Auto-scaling delta caps:
`cap_contracts = threshold_cc(frac, bankroll) / 10_000` when armed, else the absolute knob.

### 4.2 The cap table

`$ at $2,000` uses the research START basis (caps scale with live equity ~$2,384.77, so real dollars
are ~19% higher). **LIVE = enforced when `caps_shadow_mode=False` in paper/quote mode (the default);**
SHADOW would mean log-only.

| cap | %-of-bankroll (frac) | $ at $2,000 | binds on axis | LIVE/SHADOW | reason code |
|---|---|---|---|---|---|
| per-combo max-loss | `per_combo_loss_frac` 1% | $20 | LOSS (candidate `max_loss_cc`) | LIVE (not waivable) | `SKIP_PER_COMBO_LOSS_CAP` |
| per-game correlated loss | `game_loss_frac` 8% | $160 | LOSS (`worst_case_loss_by_game_cc`) | LIVE (waivable) | `SKIP_GAME_LOSS_CAP` |
| directional / theme | `directional_frac` 10% | $200 | LOSS-equiv (`directional_by_game_cc`, mutex-aware) | LIVE (waivable) | `SKIP_DIRECTIONAL_CAP` |
| slate (ET-day) | `slate_loss_frac` 8% | $160 | LOSS (Σ game loss over one ET day) | LIVE | `SKIP_SLATE_CAP` |
| daily-loss halt (soft) | `daily_loss_frac` 6% | $120 | realized+unrealized from day start | LIVE (HALT) | `HALT_DAILY_LOSS` |
| drawdown halt | `drawdown_frac` 10% | $200 | give-back = peak − current − pending | LIVE (HALT) | `HALT_DRAWDOWN` |
| hard-trip KILL | `hard_trip_frac` 12% | $240 | deeper give-back (human-only clear) | LIVE (KILL) | `HALT_HARD_TRIP` |
| portfolio CVaR (ES_0.99) | `portfolio_cvar_frac` 15% | $300 | LOSS (governing model ES) | LIVE | `SKIP_PORTFOLIO_CVAR` |
| portfolio deterministic-max | `portfolio_det_max_frac` 15% | $300 | LOSS (all-hit premium, mutex-aware) | LIVE | `SKIP_PORTFOLIO_DET_MAX` |
| P(ruin) | `portfolio_ruin_prob_budget` 5% (prob) | — | probability | LIVE | `SKIP_PORTFOLIO_RUIN` |
| utilization backstop | `absolute_notional_multiple` 3× | $6,000 | UTILIZATION (Σ gross notional) | LIVE | `SKIP_UTILIZATION_BACKSTOP` |
| fill-velocity soft | `fill_velocity_soft_frac` 5% / 2s | $100/2s | committed premium/window | LIVE (decline+cancel) | `DECLINE_FILL_VELOCITY` |
| fill-velocity hard | `fill_velocity_hard_frac` 10% / 2s | $200/2s | committed premium/window | LIVE (HALT) | `HALT_FILL_VELOCITY` |
| fill-velocity count | `fill_velocity_max_fills` 8 / 2s | 8 fills | count (bankroll-free) | LIVE (decline) | `DECLINE_FILL_VELOCITY` |

**Legacy absolute hard-dollar caps (always enforced, `shadow=False`):** per-quote 100 contracts /
$500 loss; per-market delta 300 contracts; per-event delta 500 contracts; whole-book gross notional
$5,000; max open quotes 20; hard daily-loss HALT $500; per-game worst-case loss $1,000. Auto-scaling
delta caps (`max_market_delta_frac`/`max_event_delta_frac`) default `None` (absolute knobs govern);
the WC campaign armed `0.80 / 1.30`.

> **NOTE (cap ladder invariant):** combo < daily < game < directional. Makers **cannot partial-fill**
> — each quote is implicitly for the full RFQ amount — so caps are the *only* lever for large RFQs.
>
> **NOTE (WC-era armed config, `config/prod-live-wc.local.yaml`, gitignored, looser than research
> defaults):** `game_loss .50, slate .65, det .36, cvar .35, delta .80/1.30`, K=48 waiver,
> `max_open_quotes 200`, `max_contracts_per_quote 2000`. A budget-family review is owed before scaling.

### 4.3 Fail-closed bankroll gate (three cases)

- `risk_bankroll_cc=None` **and** a source is configured (stale/absent) ⇒ one
  `SKIP_BANKROLL_UNAVAILABLE`, return (no other R2 cap computed) — dark-poll runaway defense.
- `risk_bankroll_cc <= 0` ⇒ same (a zero denominator collapses every threshold to 0).
- `risk_bankroll_cc=None` **and** no source configured ⇒ whole R2 layer **inactive** (return `[]`) so
  a fresh demo/paper start still quotes off the legacy hard-dollar caps.

---

## 5. PORTFOLIO MONTE CARLO + CHALLENGER (`sim/book_risk.py`, `sim/engine.py`, `sim/book_model.py`)

### 5.1 Sampling method

Off the hot path, `compute_book_risk` samples the **real committed book** under the **pricer's own
correlation** (block-diagonal by game) and reads tail stats back:

- **Correlation matrix** (`sim/book_model.py::build_book_model`): block-diagonal by `game_key`.
  Cross-game pairs sit at `cross_event_rho = 0.0` (games ~independent — a measured fact). Within-game
  pairs get the typed prior from the pricer's own `build_sgp_correlation` (`sim/within_game_rho.py`),
  or the `DEFAULT_FLAT_BAND (-0.20, 0.10, 0.40)` when no calibrated prior exists. Three matrices at
  **(low, point, high)** bands; per-game block collapses mixed pair rhos conservatively:
  **high = max(rhos), low = min, point = mean**. **Risk GATES on the `high` band.**
- **NO-side handling = SIGN FLIP** (`sim/engine.py`, ~2-line fix): a NO-selected leg contributes
  `1 − value` inside the payout product (`leg_sides`), **never** an independent complement pseudo-leg
  (which would destroy within-game correlation — and for a sell-only book that is *every* position).
- **Gaussian copula sampling:** `z = standard_normal @ chol(corr).T`; `u = Φ(z)`; read value off each
  leg's inverse CDF. Structural soccer games (WC) sample one Dixon-Coles scoreline per game and settle
  every leg against it (`sim/structural_book.py`) — hedges/exclusions automatic with no rho table;
  non-soccer uses the copula path.
- **Determinism:** `SeedSequence(seed).spawn(5)` (snapshot) / `spawn(4)` (candidate) — independent
  substreams, never `seed/seed+1`. Same book + seed ⇒ same CVaR.

### 5.2 Risk statistics

```
VaR_q  = max(0, -quantile(pnl, 1-q))
ES_q   = max(0, -mean(pnl | pnl <= quantile(pnl, 1-q)))        # positive loss magnitude; clamped >=0
P(ruin)= mean( current_equity_cc + book_pnl < ruin_floor_frac × bankroll_cc )   # ruin_floor_frac 0.70 (-30%)
         current_equity_cc = COST-BASIS equity (cash + Σ price_cc×contracts), NOT exchange equity
p_ruin_upper = wilson_upper_bound(p_ruin, n_samples, z)        # ruin cap gates on max(p_ruin, upper)
```

Headline level `HEADLINE_LEVEL = 0.99`. Per-game & per-leg **tail attribution** additively
decomposes the 0.99 tail: `Σ per-game contribution = CVaR exactly`.

### 5.3 operative / governing ES = max of scenarios (anti-monoculture)

```
governing_model_es_99_cc = max( production_es , challenger_es , bridge_es , struct_es , split_es )
```

- **Correlation-inflated challenger** (LIVE, default `challenger_inflation = 0.5`): re-sample with
  `rho' = rho + 0.5×(1-rho)` on **same-game off-diagonal pairs only** (cross-game and diagonal
  unchanged — universal positive corr is NOT conservative for a hedged book). Only ever **widens** the
  tail. Tests whether copula ES is robust to a correlation mis-estimate.
- **Full-copula bridge** (conditional): fires when a game straddles structural+copula blocks.
- **Independent-split guard** (conditional): the conditioned production tail is never reported below
  the independent split (conditioning may only FATTEN the tail).
- **Structural-parameter challenger** (GATED-OFF by default): perturbs DC rho / ET / pens / half-share
  + shocks marginals toward 0.5. Only fires with `structural_challenger=True`.

The **deterministic all-hit stress** is a **SEPARATE axis** (P0-3), no longer max'd into the ES:

```
deterministic_max_loss_cc = Σ_positions (price_cc × contracts + fee_cc) + max(0, reserved_loss_cc)
mutex_aware_det_max_cc    = min(deterministic_max, mutex_aware_det_max_from_units(...))   # <= comonotone always
```

### 5.4 The candidate gate (last-look, `evaluate_candidate_book_risk`)

LIVE by default (`candidate_gate_enabled=True`). Builds **ONE merged BookModel** over PRE
(committed + all outstanding reservations + simultaneous accepts) + candidate, samples the shared
universe **once per substream** at `candidate_gate_mc_samples = 20,000`, scores PRE vs POST on **common
random numbers**, and **can only DECLINE** (strictly additive). Gate order (first failing reason wins):

```
(1)  candidate_ev = post.ev - pre.ev  (PRODUCTION model)
       if candidate_ev <= 0: decline "negative_ev_no_hedge_budget"
          unless allow_negative_ev_hedge AND post.tail_loss(UNCLAMPED) <= pre.tail_loss  # certified hedge
                 AND -candidate_ev <= hedge_cost_budget_cc
(1b) worst_credible_candidate_ev < worst_challenger_ev_tolerance  -> decline  (default -inf = no-op)
(2)  post.governing_model_es_99_cc > portfolio_cvar_frac × bankroll -> decline
(3)  min(post.det_max, post.mutex_aware_det_max) > portfolio_det_max_frac × bankroll -> decline
(4)  max(post.p_ruin, post.p_ruin_upper) > portfolio_ruin_prob_budget -> decline
(5)  post.gross_settlement_notional_cc > absolute_notional_multiple × bankroll -> decline
      UNKNOWN merged marginal -> confirm=False "unknown_marginal"
```

**Atomicity (P0-2):** the provisional reservation is created **first**; the gate stamps
`ExposureBook.position_generation` + `RiskReservationService.version` into MC inputs, re-reads both on
worker return, and **discards + rebuilds + retries** if either moved. Bounded by
`candidate_gate_deadline_s = 2.0s` (wall) AND `candidate_gate_max_retries = 3`. Any decline / error /
timeout / instability ⇒ release provisional reservation + `DECLINE_CANDIDATE_RISK` (fail-closed).
Runs **off the event loop** (`BookRiskPool`, 2 workers; inline in paper/tests, byte-identical).

Full-book snapshot default `n_samples = 100,000`, `seed = 0`; recompute throttled to 15s; the
freshness gate (`_book_risk_for_check`) fails the CVaR + det-max caps **closed** on any stale /
generation-superseded / UNKNOWN book.

---

## 6. RESERVATION + LAST LOOK + SETTLEMENT

### 6.1 Reservation lifecycle (`risk/reservation.py`)

Single-writer, atomic (one synchronous critical section per method — no locks, atomic between
asyncio awaits), versioned. `reservation_id = "fill:{quote_id}"`.

```
 NONE --try_reserve(PASS)--> OUTSTANDING (headroom consumed) --commit--> COMMITTED (add_position to book)
                    (FAIL: enforced breach) -> nothing recorded         --release--> RELEASED (headroom freed, no position)
 OUTSTANDING --mark_unconfirmed--> OUTSTANDING+UNCONFIRMED (confirm TIMED OUT = ASSUME COMMITTED, headroom STAYS)
 reconcile(exchange_open_ids): each OUTSTANDING -> commit if in set else release   (exchange-first truth)
```

`try_reserve` re-runs `LimitChecker.check` against committed + **all outstanding reservations** +
this candidate in one section; denies only on an **enforced** breach (shadow-safe). **Timeout =
assume-committed** (a lost ack must never let a possibly-real position stop counting against caps);
only exchange-first `reconcile` can release it. Idempotent by id. `commit` uses `add_position` keyed
on `position_id` (replace-by-id, never append — no double-count). A confirm-path last-look **MC
waiver** (`lastlook_mc_waiver_enabled`, **default OFF**) may retry once on a denial whose every
enforced breach is a game-loss / mutex-directional cap, using exact Dixon-Coles scoreline enumeration
(`sim/state_worst_case.py`) if every breached game certifies within the SAME game-loss budget.

### 6.2 Last-look decline ladder (`risk/lastlook.py`, pure, ~<1ms, order load-bearing)

```
1  killswitch_halted        -> DECLINE_KILL_SWITCH
2  not exchange_active       -> DECLINE_EXCHANGE_INACTIVE
3  not ws_healthy / seq gap  -> DECLINE_WS_UNHEALTHY
4  any_leg_in_play           -> DECLINE_IN_PLAY
5  any_leg_started           -> DECLINE_INPLAY_LEG
6  leg_start_unknown         -> DECLINE_START_TIME_UNKNOWN
7  velocity_anomaly          -> DECLINE_VELOCITY_ANOMALY
8  leg age None or > 2.0s    -> DECLINE_LEG_STALE            (max_leg_age_s)
9  leg move None or > 150cc  -> DECLINE_FAIR_MOVED_LEG       (leg_move_tolerance_cc)
10 |fair - quote_fair| >200cc -> DECLINE_FAIR_MOVED_JOINT     (joint_move_tolerance_cc)
11 risk_breaches non-empty   -> DECLINE_RISK_LIMIT
else                         -> CONFIRM_OK
# every None input FAILS CLOSED (a None is never "fine")
```

### 6.3 Settlement value formula (`risk/settlement.py`, `risk/balance.py`)

A combo settles to realized YES value **`V ∈ [0,1]`** (product of leg values; **scalar** under
DNP/rain/void, not restricted to {0,1}). Our position is LONG NO, NO pays `1 − V`:

```
_no_payout_per_contract_cc(V):  v_cc = round(V × 10_000);  return 10_000 - v_cc     # $1 - floor(V) on the grid
premium_paid_cc = contracts × entry_price_cc // 100
payout_cc       = contracts × payout_per_ct_cc // 100
realized_cc     = payout_cc - premium_paid_cc - fee_cc

# LONG NO:  realized = contracts × ((1 - V) - entry_price) - fee
#   V=0 (binary MISS) => +($1 - premium) - fee   (full win)
#   V=1 (binary HIT)  => -premium - fee           (forfeit)
#   V=0.7 scalar      => NO pays $0.30, partial
```

`round()` not `int()` on `V×10_000` is deliberate (`int(0.57×10000)=5699` would spuriously under-floor
1cc and trip the reconcile HALT). A NO credit is **gated on `combo_no_pays_complement=True`** (verified
from a real $1.00 demo settlement 2026-07-10); if False it raises → `HALT_RECONCILIATION_MISMATCH`
(never books 0). Every settlement **reconciles predicted vs exchange revenue to the cent** and any
mismatch **HALTS**. Fees rounded **UP** (`ROUND_CEILING`, never understate a cost). A P1-7 tripwire
HALTs if ≥2 outcome markets of a netted ME event both settle YES (the exclusivity we netted on was
false).

### 6.4 Settlement receivables + give-back shield (`risk/balance.py`)

The exchange removes a settled position from `portfolio_value` **before** crediting balance, so during
a cascade equity transiently dips by the in-flight value. **Receivables** = predicted gross credit of
a position whose every leg is exchange-graded (facts only, never a live mark). Give-back is measured
`max(0, (peak − current) − Σ pending_receivables)` — receivables only ever **reduce** measured
give-back, never touch peak/equity. A LOSER produces no receivable (real loss cascades are never
shielded). TTL backstop 30 min (structural, not a knob) expires loudly. This exact fix prevents the
2026-07-19-class **false $430.69 KILL** whose real losers were $29.51.

> **UNVERIFIED (AS1):** the receivable-drop rule assumes settlements-row visibility implies the
> balance poll has credited cash; the 2026-07-19 incident proved `portfolio_value` and `balance` are
> **not atomic across endpoints** — verify on next live settlement.
> **FLAGGED (AS4, MLB):** scalar/DNP (rain/void) legs never produce a receivable, so the shield does
> NOT cover them; their trough can still trip halts (fail-closed, no phony scalar prediction).

---

## 7. BALANCING + INVENTORY SKEW (`risk/skew.py`, `sim/peak_profile.py`)

Skew is a **pricing-only** lever (never a refusal). It ships **DARK** (`SkewParams.enabled=False` ⇒
`applied_cc = 0` ⇒ zero live P&L today), computed and logged as a shadow classifier.

### 7.1 The exact SIGN (load-bearing, applied ONCE)

```
CLASSIFIER convention:   CONCENTRATING => skew_cc >= 0 ;   OFFSETTING => skew_cc <= 0   (readable in every log)
PRICER convention (OPPOSITE): no_raw = ($1 - fair) - half - fee_no + inventory_skew_cc
     a POSITIVE inventory_skew_cc RAISES no_bid => LOWERS implied YES ask => combo CHEAPER => sell MORE
The single flip lives at the pricer boundary:   applied_cc = -skew_cc  (when enabled)
     concentrating (skew_cc>=0) NEGATES to negative => lower no_bid => dearer => SELL LESS
     offsetting   (skew_cc<=0) NEGATES to positive => raise no_bid => cheaper => SELL MORE
```

So: **stacking a game we're already loaded on => widen (sell less); offsetting flow => rebate (sell
more, win the flattening auction).** The **offsetting rebate** is the free-money-dangerous direction
(after negation it raises `no_bid`), bounded by the small `skew_max_tighten_cc` (150cc) plus the
`construct_quote` free-money clamp.

### 7.2 Directional classifier + mutex-blind input

Per candidate game with candidate delta `d_e` and book net delta `net`: concentrating term
`w_conc × d_e × util^gamma` (convex, `gamma=2`); offsetting rebate `w_off × min(d_e,|net|) × util`
(linear). `util` = **max** over 3 axes (delta / worst-case-loss / gross-notional). **The raw per-game
delta fed to the classifier is MUTEX-BLIND** (measured 63/63 mis-widen on the live tape — a long-NO
candidate on outcome B of an event the book is short outcome A of *nets* even though its raw sign
matches). Fixed via `mutex_directional_alignment_cc`, which engages only with exactly one explicit-ME
event + a certifying committed census and otherwise **falls back to the raw read** (fail-closed).

### 7.3 Peak-concentration steer (additive, `peak_enabled=True` but inert while `enabled=False`)

Fed by the cached committed-book worst-loss scorelines (`sim/peak_profile.py`, off hot path, DC state
enumeration on committed positions only so quote churn never repaints the peak):

```
peak_ratio  = min(1, top_loss_cc / budget_cc)          # budget = max_event_worst_case_loss × 10_000
severity    = max over cached loss clusters of (cluster_loss/top_loss) × hit-indicator, in [0,1]
WIDEN (hit):            peak_widen_max_cc(600) × severity × peak_ratio^gamma
REBATE (certified top-miss): peak_tighten_max_cc(150) × peak_ratio × (1 - severity)
peak_cc = clamp(widen - tighten, [-150, +600]);   skew_cc = directional_cc + peak_cc
composed clamp = [-(150+150), +(600+600)] = [-300cc, +1200cc] = [-3c, +12c]
```

The rebate certifies a miss of the **entire** argmax loss plateau (a K=5 sample can miss non-argmax
states — the 2026-07-18 adversarial-verify fix); multi-cluster level sets (`peak_n_clusters=3`,
`peak_cluster_min_frac=0.30`) catch a second correlated loss pile. Fail-safe NEUTRAL (0) on any doubt.
Baseline problem it targets: a one-way FRAENG book with **P(book profits) = 52.47%** (a coin toss)
despite +EV / ruin~0.

---

## 8. SAFETY LAYER (`risk/breakers.py`, `risk/killswitch.py`, `risk/heartbeat.py`, `ops/supervisor.py`, `ops/preflight.py`)

### 8.1 The 7 circuit breakers (all pure, fail-closed; cannot-evaluate ⇒ TRIP)

| # | breaker | trips when | reason | grace |
|---|---|---|---|---|
| 1 | data staleness | `seq_gap` OR `rx_age None` OR `rx_age > 5.0s` | `HALT_DATA_STALE` | 30s |
| 2 | latency spike | `latency_ms > 2000ms` (worst in 60s window) | `HALT_LATENCY_SPIKE` | 90s |
| 3 | 429 rate-limit burst | `count >= 10` in 10s (**the sole `>=`**) | `HALT_RATE_LIMIT_BURST` | 30s |
| 4 | marginal jump | prev→cur `|Δ| > 0.25` prob, OR priced leg became unreadable | `HALT_MARGINAL_JUMP` | 30s |
| 5 | unmapped game | `game_key` None/empty (escapes concentration caps) | `HALT_UNMAPPED_GAME` | none (structural) |
| 6/7 | metadata change | tripwire hit OR settlement-metadata changed | `HALT_METADATA_CHANGE` | none (structural) |
| — | breaker error | any detector raises | `HALT_BREAKER_ERROR` | none |

All thresholds use strict `>` except the 429 burst (`>=`). Transient reasons get a **grace hold**
(monotonic-ns timing; recovery needs **2 consecutive** fully-clear ticks — flap resistance); structural
reasons hard-halt on first trip. **Exemptions** (both purge baseline + skip the watch):
`settled_tickers` (exchange-confirmed non-live: `closed/determined/disputed/amended/finalized`) and
`inplay_tickers` (game started per the pregame start ladder) — added after two production hard-kills
(2026-07-18 settled FRAENG leg; 2026-07-19 in-play ESPARG, 45 trips / 8 halts).

### 8.2 Kill switch + KILL file + heartbeat + reconcile marker

`KillSwitch.halt(reason)` is global, idempotent (first caller wins, subscribers fire once —
cancel-all + intake-stop). `clear(actor)` is **human-only**, never automation. A `KILL` file
(polled 1s) forces `HALT_KILL_FILE`. The bot beats an atomic wall-timestamp `data/heartbeat.txt`
every maintenance tick (~0.5s, throttled 10/s); a reader treats missing/unreadable/future-skewed
(`< -300s`) as wedged (fail-closed). A `needs_reconcile` marker (fail-closed present on any read
error) blocks quoting after a hard-trip until an exchange-first reconcile clears it.

### 8.3 Out-of-process SafetySupervisor

Separate process, own credential (`KALSHI_SUPERVISOR_API_KEY_ID`), reserved `WriteBudget` (200
tokens / 10s, above `max_open_quotes`). On a missed heartbeat (`heartbeat_timeout_s = 15.0`): cancel
all via its own credential, THEN write KILL + drop the reconcile marker. **Fail-closed:** if the
exchange is unreachable it STILL writes KILL + marker + alarms.

### 8.4 Go-live gates (`ops/preflight.py`, `ops/config.py`)

`PROD ∧ QUOTE ⇒ require confirm_live (CLI-only, never YAML) ∧ prod_limits_configured ∧
(¬prod_require_series_whitelist ∨ non-empty allowlist)`. Runtime preflight also verifies supervisor
heartbeat established + external kill reachable + book reconciled, else `PreflightError` (exit 3).
Everything defaults to not-green (fail-closed).

---

## 9. COMPLETE PARAMETER REFERENCE

`$` at $2,000 START basis. Money in centi-cents unless noted. Fraction params are decimal **strings**
parsed to exact `Fraction` (a bare float or unquoted `"8"` = 800% is a validation error by design).

### 9.1 R2 %-of-bankroll caps (`risk.*`)

| name | default | units | meaning |
|---|---|---|---|
| `game_loss_frac` | 0.08 | frac bankroll | per-game correlated loss cap (worst_case_loss_by_game) |
| `per_combo_loss_frac` | 0.01 | frac | single-candidate max-loss cap (not waivable) |
| `directional_frac` | 0.10 | frac | mutex-aware `directional_by_game_cc` cap |
| `slate_loss_frac` | 0.08 | frac | Σ game loss over one ET-day slate |
| `daily_loss_frac` | 0.06 | frac | soft daily-loss halt (realized+unrealized) |
| `drawdown_frac` | 0.10 | frac | peak-drawdown halt (give-back) |
| `hard_trip_frac` | 0.12 | frac | hard-trip KILL (human-only clear) |
| `portfolio_cvar_frac` | 0.15 | frac | governing model ES_0.99 cap |
| `portfolio_det_max_frac` | 0.15 | frac | deterministic all-hit max-loss cap |
| `portfolio_det_max_mutex_aware` | True | bool | gate det-max on `min(comonotone, mutex-aware)` |
| `portfolio_ruin_prob_budget` | 0.05 | probability | max P(equity < ruin floor this wave) |
| `absolute_notional_multiple` | 3 | × bankroll | gross-notional utilization backstop |
| `caps_shadow_mode` | False | bool | False = ENFORCED; True = log-only shadow |

### 9.2 Legacy absolute + delta caps (`risk.*`)

| name | default | units | meaning |
|---|---|---|---|
| `max_contracts_per_quote` | 100.0 | contracts | per-candidate size cap |
| `max_notional_per_quote_dollars` | 500.0 | dollars | per-candidate LOSS cap (premium, misnamed) |
| `max_market_delta_contracts` | 300.0 | contracts | absolute per-market delta cap |
| `max_event_delta_contracts` | 500.0 | contracts | absolute per-event delta cap |
| `max_market_delta_frac` | None | frac (0,10] | auto-scaling per-market delta; wins over absolute |
| `max_event_delta_frac` | None | frac (0,10] | auto-scaling per-event delta; wins over absolute |
| `max_gross_notional_dollars` | 5000.0 | dollars | whole-book gross notional cap |
| `max_open_quotes` | 20 | count | max simultaneous resting quotes |
| `max_daily_loss_dollars` | 500.0 | dollars | hard-dollar daily-loss HALT |
| `max_event_worst_case_loss_dollars` | 1000.0 | dollars | per-game worst-case loss cap (absolute) |
| `starvation_threshold` | 20 | count | consecutive risk-declines before watchdog warns |

### 9.3 Confirm-path gate + fill-velocity + haircut (`risk.*`)

| name | default | units | meaning |
|---|---|---|---|
| `candidate_gate_enabled` | True | bool | candidate-aware ~20k portfolio MC at confirm |
| `candidate_gate_deadline_s` | 2.0 | s (0,3] | candidate-gate wall budget |
| `candidate_gate_mc_samples` | 20000 | samples | confirm-time candidate MC sample count |
| `candidate_gate_max_retries` | 3 | count | rebuild-on-version-conflict bound |
| `worst_challenger_ev_tolerance_cc` | -inf | cc | decline +EV candidate if worst challenger EV < tol |
| `allow_negative_ev_hedge` | False | bool | admit certified risk-reducing negative-EV fill |
| `hedge_cost_budget_cc` | 0 | cc | max EV a certified hedge may cost |
| `lastlook_mc_waiver_enabled` | False | bool | confirm-path state-exact waiver on game-loss/mutex denials |
| `lastlook_mc_waiver_deadline_s` | 1.0 | s (0,3] | waiver wall budget (sum with gate ≤ 3s) |
| `lastlook_waiver_topk_resting` | 0 | count | K largest resting quotes enumerated (0 = full set) |
| `resting_quote_weight` | 1.0 | frac (0,1] | quote-time haircut on resting quotes (1.0 = no-op) |
| `resting_floor_count` | 3 | count | burst floor: K largest resting always fold 100% |
| `resting_haircut_at_confirm` | False | bool | also weight resting fold in the confirm reservation check |
| `fill_velocity_window_s` | 2.0 | s | rolling committed-notional window |
| `fill_velocity_soft_frac` | 0.05 | frac | throttle+cancel-all threshold |
| `fill_velocity_hard_frac` | 0.10 | frac | HALT threshold |
| `fill_velocity_max_fills` | 8 | count | bankroll-free count cap (binds on stale bankroll) |
| `pre_pricing_gate_enabled` | False | bool | pre-decline candidate-monotone caps before pricing |
| `fill_record_recovery_after_s` | 10.0 | s | delay before polling REST for an unrecorded WS fill |
| `settled_marginal_resolution` | True | bool | resolve settled-leg marginals to graded facts (1.0/0.0) |
| `position_reconcile_interval_s` | 300.0 | s | exchange-vs-book position reconcile |

### 9.4 MC / book-risk (`LifecycleConfig` / `sim.book_risk`)

| name | default | units | meaning |
|---|---|---|---|
| `HEADLINE_LEVEL` | 0.99 | probability | VaR/CVaR headline level |
| full-book `n_samples` | 100000 | samples | maintenance MC sample count |
| `book_risk_seed` | 7 (app) / 0 (module) | int | MC seed |
| `book_risk_stale_after_s` | 30.0 | s | snapshot freshness window (recompute throttle 15s) |
| `challenger_inflation` | 0.5 | frac of gap-to-1 | same-game correlation inflation |
| `ruin_floor_frac` | 0.70 | frac bankroll | equity below this = ruin (−30%) |
| `ruin_prob_ci_z` | 0.0 | z-score | Wilson upper-bound z (0 = point estimate) |
| `cross_event_rho` (`DEFAULT_CROSS_EVENT_RHO`) | 0.0 | correlation | off-block (cross-game) correlation |
| `DEFAULT_FLAT_BAND` | (-0.20, 0.10, 0.40) | (low,point,high) | within-game rho when no calibrated prior |
| `structural_challenger` | False | bool | opt-in structural-parameter challenger |

### 9.5 Skew (`pricing.skew.*`, DARK)

| name | default | units | meaning |
|---|---|---|---|
| `enabled` | False | bool | master DARK switch (applied_cc pinned 0) |
| `w_conc` / `w_off` | 1.0 / 1.0 | weight | concentration / offset weights |
| `gamma` | 2.0 | exponent | convex utilization ramp `util^gamma` |
| `skew_max_widen_cc` | 600 | cc (+6c) | cap on concentrating (widen) side |
| `skew_max_tighten_cc` | 150 | cc | cap on offsetting (rebate) side |
| `peak_enabled` | True | bool | peak steer (inert while `enabled=False`) |
| `peak_widen_max_cc` | 600 | cc | max widen for a peak-scoreline hit |
| `peak_tighten_max_cc` | 150 | cc | max rebate for a certified top-miss |
| `peak_topk_states` | 5 | count [1,64] | worst scorelines cached per game |
| `peak_n_clusters` | 3 | count [1,8] | distinct loss clusters cached (1 = single plateau) |
| `peak_cluster_min_frac` | 0.30 | frac top-loss | cluster qualifies at ≥30% top loss |

### 9.6 Widen-vs-decline, breakers, supervisor, filters, structural, markup

| name | default | units | meaning |
|---|---|---|---|
| `pricing.widen.enabled` | False | bool | widen-vs-decline (SHADOW) |
| `pricing.widen.util_threshold` | 0.75 | frac | concentrating candidate ≥ util near cap ⇒ decline |
| `breakers.max_rx_age_s` | 5.0 | s | data-stale trip |
| `breakers.max_latency_ms` | 2000.0 | ms | latency-spike trip |
| `breakers.latency_spike_window_s` | 60.0 | s | trailing latency window |
| `breakers.rate_limit_window_s` | 10.0 | s | 429 window |
| `breakers.max_rate_limit_in_window` | 10 | count | 429-burst trip (>=) |
| `breakers.max_marginal_jump` | 0.25 | probability | marginal-jump trip |
| `supervisor.heartbeat_timeout_s` | 15.0 | s | wedge timeout |
| `supervisor.poll_interval_s` | 1.0 | s | heartbeat poll |
| `supervisor.write_budget_capacity` | 200 | tokens | reserved supervisor write budget |
| `supervisor.write_budget_refill_s` | 10.0 | s | write-budget refill |
| `filters.allowed_leg_series_prefixes` | [KXWC, KXMLB] | list | leg-series allowlist / per-sport kill |
| `filters.allow_inplay_legs` | false | bool | pregame-only gate |
| `filters.min_legs` / `max_legs` | 2 / 6 | count | quotable combo leg bounds |
| `filters.min_time_to_close_s` | 3600.0 | s | pregame min 1h before close |
| `filters.max_feed_age_s` | 5.0 | s | quote-time feed-freshness gate |
| `pricing.structural.enabled` | true | bool | Dixon-Coles soccer pricer |
| `pricing.structural.dc_rho` | -0.05 | correlation | DC low-score adjustment |
| `pricing.structural.et_factor` | 0.3333 | frac | ET scoring rate |
| `pricing.structural.pens_win_prob` | 0.5 | probability | P(named team wins shootout \| level) |
| `pricing.structural.half_share` | 0.45 | frac | first-half goal share |
| `pricing.structural.corners_et_loading` | 0.10 | loading | knockout total-corners shared-factor loading |
| `pricing.mlb_runs.enabled` | true | bool | NegBin MLB runs model (dispersion_k=3.54) |
| `pricing.correlation.same_event_rho` | 0.6 | correlation | fallback same-event prior (uncertainty 0.25) |
| `pricing.fee.taker_coef` | 0.07 | — | quadratic taker fee coef (the LIVE effective coef) |
| `pricing.fee.maker_coef` | 0.0175 | — | quadratic maker fee coef (=7/400) |
| `pricing.markup.enabled` | False | bool | DARK master markup switch |
| `pricing.markup.{soccer,mlb}.markup_cc` | 0 | cc over fair | flat markup (self-selects FAT flow) |
| `pricing.markup.{sport}.tiers` | [] | list | fair-dependent `(fair_below_cc, markup_cc)` |
| `pricing.markup.series_adders_cc` | {} | cc/prefix | per-series adder (max match, once per combo) |
| `pricing.quote.base_width_cc` | 200 (code) / 0 (LIVE) | cc | base half-spread width |
| `pricing.quote.min_capture_cc` | 100 | cc | `yes_bid+no_bid <= 9900` else no-quote |
| `pricing.quote.free_money_margin_cc` | 100 | cc | arb-free clamp cushion (>=1c) |
| `pricing.quote.sell_parlays_only` | false (code) / true (LIVE) | bool | force `yes_bid=0` (long-NO only) |
| `app.env` / `app.mode` | demo / observe | enum | environment / mode |
| `app.kill_file` | KILL | path | presence halts |

> **LIVE markup (WC campaign, from `config/prod-live-wc.local.yaml`):** soccer flat base **1c** with
> tiers `fair<15c => 4c`, `15c<=fair<35c => 2c`, `fair>=35c => 1c`; MLB flat **1c** (no tiers);
> series adders `KXWCCORNERS/KXWCTCORNERS => +4.5c`. (A later 2026-07-16 tiering used `<2c => +5c`,
> `2-10c => +4c`, `10-35c => +2c`, `>=35c` unchanged; longshots settle 13.8% vs priced 19.6%.)
> **The committed defaults are DARK (0), so the committed schema understates the real edge.**

---

## 10. CURRENT ON / OFF / SHADOW / DARK STATE

| layer | module | state (default) |
|---|---|---|
| Quote-time `LimitChecker` (all R2 %-caps) | `risk/limits.py` | **LIVE / ENFORCED** (`caps_shadow_mode=False` in paper/quote) |
| Legacy hard-dollar caps + delta family | `risk/limits.py` | **LIVE / ENFORCED** (always `shadow=False`) |
| Auto-scaling delta caps | `risk/limits.py` | LIVE when armed (WC: 0.80/1.30); else absolute knobs |
| Single-writer reservations | `risk/reservation.py` | **LIVE** |
| Last-look `decide_confirm` | `risk/lastlook.py` | **LIVE** |
| Candidate portfolio-MC gate | `sim/book_risk.py` | **LIVE / ENFORCED** (decline-only, `candidate_gate_enabled=True`) |
| Fill-velocity governor | `risk/fill_velocity.py` | **LIVE** (soft 5% / hard 10% / count 8) |
| Give-back / daily / hard-trip halts | `rfq/lifecycle.py` maintenance | **LIVE / ENFORCED** |
| Settlement ledger + reconcile-to-cent HALT | `risk/settlement.py` | **LIVE** (paper/quote mode) |
| Settled-marginal resolution | `marketdata/settled.py` | **LIVE** (True) |
| Settlement receivables shield | `risk/balance.py` | **LIVE** |
| In-play + settled breaker exemptions | `risk/breakers.py` | **LIVE** |
| 7 circuit breakers | `risk/breakers.py` | **LIVE** (4 fully sampled; marginal-jump/unmapped/metadata sampled per status loop) |
| SafetySupervisor + heartbeat + reconcile marker | `ops/supervisor.py` | LIVE when provisioned (needs dedicated credential) |
| Peak-concentration steer | `sim/peak_profile.py` | **DARK** (`peak_enabled=True` but inert while skew `enabled=False`) |
| Inventory skew | `risk/skew.py` | **DARK** (`enabled=False`, `applied_cc=0`) |
| Widen-vs-decline | `risk/skew.py` | **SHADOW** (`enabled=False`) |
| Last-look MC waiver | `sim/state_worst_case.py` | **GATED-OFF** (`lastlook_mc_waiver_enabled=False`) |
| Maker markup | `pricing/markup.py` | DARK in committed config; **armed only in local WC YAML** |
| Structural-parameter challenger | `sim/book_risk.py` | GATED-OFF |
| Farming impossible combos | `pricing/quote.py` | config True but **GATED OFF for live** (farm-reconcile TODO) |
| **The bot process itself** | — | **DOWN** (human-only KILL after false-positive hard-trip 2026-07-19) |

**Open audit items (2026-07-15 audits; may be closed by newer commits through 2026-07-22 — verify
against source before relying):**

- **P0-1 (equity basis):** candidate/reservation premiums were added to the P(ruin) equity basis
  before being paid, understating ruin risk (fix: build equity basis from committed positions only).
- **P0-2 (atomicity):** candidate MC ran before reservation with no version validation (fix:
  provisional reservation first + generation/version stamp + retry — described in §5.4 as landed).
- **P1 (robust EV):** "+EV" is *production-model* EV, not robust EV; a candidate can be +EV under
  production and −EV under a challenger (recommend gating on `worst_challenger_ev`).

**Not-yet-live-validated:** the candidate gate has processed **ZERO real won auctions** since
relaunch; the settlement/reconcile-HALT chain is proven in **tests only**, never against a real Kalshi
settlement; `combo_no_pays_complement` came from ONE demo settlement; the WC `advance|player_goal:same`
rho 0.45→0.52 promotion's backtest died silently (confirm or revert). **The markup/edge is UNVALIDATED**
(the pooled multi-week markout study — the profitability gate — has not been reached).

---

## 11. *** THE SIMULATION MODEL *** (self-contained return-simulation recipe)

This section is everything another Claude needs to Monte-Carlo the strategy's returns. All numbers are
the actual measured/config values above.

### 11.1 Capital base & bankroll

```
START_BANKROLL   = $2,000.00        # single deposit, no withdrawals; the research/cap basis
CURRENT_EQUITY   = $2,384.77        # true all-time profit $384.77 booked; the live caps denominator
risk_bankroll_cc = min(SOD_equity, cash + 0.5 × portfolio_value) × 10_000    # per-check; deployment-neutral
# For a fresh-book simulation start SOD_equity = cash = START_BANKROLL, portfolio_value = 0.
```

### 11.2 RFQ arrival + FILL model

```
ARRIVAL          ~ 170 RFQs/sec offered; but the pricer can PRICE only ~1/sec (600ms CPU, Python GIL)
                   => the bot ACTS on <1% of eligible RFQs. Throughput is the #1 fill lever, SEPARATE from risk.
RFQ_BASE_FILL    = ~12.7% (~13%)   # P(an RFQ trades AT ALL, for anyone); ~87% never trade.
OUR_CONVERSION   << base rate      # e.g. 16 fills / 21,669 quotes; the binding constraint is NOT price.
FILL_BLOCKER     = the per-game COMONOTONE loss cap at LAST LOOK (decline_risk_limit), NOT mispricing.
                   We WIN the auction (cheapest maker) then SELF-DECLINE when a win tips the game/directional
                   budget over its cap under the E2 mass-acceptance worst case.
                   (ENGARG: won 108 auctions, filled only ~5; declined 103.)
FILL_SIDE        = always NO (sell-only).  Makers CANNOT partial-fill: each quote is the full RFQ size.
```

Model the fill as a **conversion product**:
`P(fill) = P(RFQ trades ~13%) × P(we reached it alive) × P(quote still resting at swipe, ~20s TTL) ×
P(best at instant) × P(we don't self-veto)`. The self-veto term is the risk-cap gate; on a
concentrated one-sided book it is the dominant killer.

### 11.3 EDGE / markup per combo

- **Committed default markup = 0 (DARK).** To simulate the *intended* strategy, use the WC live tiers:
  soccer `fair<15c => +4c`, `15c<=fair<35c => +2c`, `fair>=35c => +1c` (flat base 1c); MLB flat `+1c`;
  corners series adder `+4.5c` (once per combo, max match). Alternatively the 2026-07-16 longshot
  tiering `<2c => +5c`, `2-10c => +4c`, `10-35c => +2c`.
- **Measured edge (settlement-graded, directional-only, ONE favorite-hot window):** same-game FAT
  **+10.1pp**; multi-game exotic FAT **−1.2pp** (no edge). PNL sweep FAT-soccer flips positive at **4c**
  markup, best 5-6c (+7% to +11% ROI); NORMAL tier **loses at every markup** and wider markup
  *attracts* hitters (adverse selection). **Independence pricer loses at every markup (−31% to −40%
  ROI)** — you MUST model correlation, not independent legs.
- **`margin = max(defensive_half_width, markup_cc)`.** With LIVE widths zeroed, the markup IS the
  spread we capture.

### 11.4 P&L per position

```
premium_collected_cc = contracts × no_bid_cc // 100          # what the taker pays us (we are long NO at no_bid)
entry_price_cc       = no_bid_cc                              # = ($1 - fair) - width/2 - fee_no + skew, then arb-clamped, snapped DOWN
on settlement, combo YES value V in [0,1] (product of leg values; scalar under DNP/rain/void):
  realized_cc = contracts × ((1 - V) - entry_price) - fee_cc
  V=0  (MISS)  => +(($1 - entry_price)) - fee   per contract   (keep premium; common case)
  V=1  (HIT)   => -entry_price - fee            per contract   (forfeit premium)
  V in (0,1)   => partial (NO pays $1 - V)
fee_cc (per contract at price P): coef × 100 × P × (10000 - P) / 1e6, ceil.  LIVE coef = 0.07 (taker, conservative).
                                  quadratic maker on combo series = $0 (but code uses taker until fixture-verified).
```

**Sign note:** `entry_price` (our `no_bid`) is BELOW `$1 - fair` by the markup+width+fee, so on a MISS
we collect more than the fair NO value — that gap is the gross edge.

### 11.5 Settlement / win-probability model (top-down)

```
For each leg L: p_L = P(L settles YES) from Kalshi leg marginals (the pricer's inputs).
Combo YES value V = Π (selected-side leg values).  For a NO parlay we lose iff V=1 (all legs hit).
Correlation is BLOCK-DIAGONAL BY GAME:
  - within a game: legs are strongly correlated (same scoreline). Soccer WC => sample ONE Dixon-Coles
    scoreline per game and settle every leg against it (no rho table). Otherwise a Gaussian copula with
    within-game rho ~ DEFAULT_FLAT_BAND (-0.20, 0.10, 0.40); GATE on the HIGH band (max rho).
  - cross-game: INDEPENDENT (cross_event_rho = 0.0).
  - NO-side leg contributes (1 - value) inside the payout product (SIGN FLIP), never a complement pseudo-leg.
P(parlay hits) for a single-game combo is much higher than Π p_L (positive within-game correlation);
for a multi-game combo it is ~ product across independent games.
```

The tape is roughly fair (price ≈ realized hit frequency), so **without markup EV ≈ 0**; the markup and
refusing toxic (NORMAL / multi-game) flow are what create positive EV.

### 11.6 Caps that GATE how much can be quoted

Per **game** you can commit until: `Σ worst-case loss ≤ game_loss_frac × bankroll` (8% = $160 @ $2k),
`directional_by_game ≤ directional_frac` (10% = $200), `Σ over ET-day ≤ slate_loss_frac` (8% = $160).
Per **combo**: `max_loss ≤ per_combo_loss_frac` (1% = $20). **Book**: gross ≤ `3× bankroll`, CVaR ≤ 15%,
det-max ≤ 15%, P(ruin) ≤ 5%, ≤ 20 open quotes. **The game/directional caps + the mass-acceptance
worst-case fold are what throttle fills** (the comonotone cap declines wins on a one-sided book).

### 11.7 Correlation structure to sample

```
partition legs by game_key -> blocks
within a block: comonotone-ish (high rho); soccer = shared scoreline; else copula@high band
across blocks : independent
Concentration is real: measured live ~6,000 combos mapped to 191 games with ONE game holding ~2,650
combos (a heavy single-game tail). Simulate a skewed game-size distribution, not uniform.
```

### 11.8 Known measured anchors (use as calibration)

| anchor | value |
|---|---|
| RFQ base fill rate | ~12.7% (P(RFQ trades at all)) |
| our conversion | tiny (e.g. 16 fills / 21,669 quotes); gated by caps not price |
| dominant fill blocker | per-game comonotone loss cap at last-look (`decline_risk_limit`) |
| concentration | ~6,000 combos -> 191 games; one game ~2,650 combos |
| WC campaign realized | 74 fills, ~$720 premium, final net +$234.09 (71 win / 3 lose) |
| true all-time profit | $384.77 equity-anchored ($2,384.77 − $2,000); 56 markets = $384.79 realized, −$0.02 = order-time fees |
| same-game FAT edge | +10.1pp; multi-game exotic FAT -1.2pp (no edge) |
| independence pricer | LOSES at every markup (-31% to -40% ROI) |
| FAT-soccer break-even markup | ~4c (best 5-6c, +7% to +11% ROI); NORMAL loses at every markup |
| P(book profits), concentrated one-way | 52.47% (coin toss) despite +EV / ruin~0 |
| maker fee | $0 today on quadratic combo series (code prices taker 0.07 conservatively) |
| mutex tightening (ENGARG) | comonotone $982 -> mutex-aware $820 (1.20x) |

### 11.9 PSEUDOCODE — Monte-Carlo return simulation

```python
# INPUTS (with default ranges):
#   bankroll                = 2000.0                 # dollars; caps scale off this
#   n_sessions              = 20..60                 # game-days to simulate
#   games_per_session       = 1..12                  # WC ~1-2/day; MLB ~10-15/day
#   rfqs_per_game           = 500..800_000           # skewed; some games get a huge single-taker ladder
#   combo_leg_dist          = P(legs=k), k in 2..6
#   same_game_fraction      = 0.4..0.6               # fraction of combos that are single-game (the +EV pond)
#   fair_dist               = empirical combo-fair distribution (many longshots 1-15c)
#   markup(fair, sport)     = tiered cents over fair (soccer 4/2/1 by fair band; MLB 1; +4.5c corners)
#   base_fill_rate          = 0.127                  # P(RFQ trades at all)
#   our_win_given_trade     = calibrate so conversion ~ observed (start ~0.01 pre-veto)
#   edge_pp(same_game)      = +0.101 same-game, -0.012 multi-game   # realized hit vs implied, per combo
#   within_game_rho_high    = 0.40                   # GATE band; or shared DC scoreline for soccer
#   cross_game_rho          = 0.0
#   ruin_floor_frac         = 0.70
#   caps = {game:0.08, combo:0.01, directional:0.10, slate:0.08, daily:0.06,
#           drawdown:0.10, hard_trip:0.12, cvar:0.15, det_max:0.15, ruin_p:0.05, util_mult:3, open:20}

def simulate_once(seed):
    rng = Random(seed)
    equity = bankroll; peak = equity; realized = 0.0
    for session in range(n_sessions):
        book = new_book()                    # per-game positions this session
        sod_equity = equity
        for game in sample_games(games_per_session, rng):
            for rfq in sample_rfqs(rfqs_per_game, rng):
                combo = sample_combo(game, combo_leg_dist, fair_dist, same_game_fraction, rng)
                # ---- PRICE ----
                fair   = combo.fair
                no_bid = (1.0 - fair) - markup(fair, combo.sport)      # widths ~0 live; arb-clamp; floor DOWN
                if no_bid <= 0: continue                                # side declined
                # ---- QUOTE-TIME + LAST-LOOK CAP GATE (mass-acceptance worst case) ----
                cand_loss = combo.contracts * no_bid                    # premium at risk (approx max_loss)
                if would_breach(book, game, cand_loss, caps, bankroll): # game/combo/directional/slate/CVaR
                    continue                                            # SELF-VETO (the real fill blocker)
                # ---- FILL? (auction) ----
                if rng.random() > base_fill_rate * our_win_given_trade: continue
                book.add(game, combo, no_bid)                           # long NO
            # end rfqs
        # ---- SETTLEMENT ----
        for game, positions in book.items():
            # within-game correlated draw: soccer=one DC scoreline; else copula@rho_high
            leg_vals = draw_correlated(game, within_game_rho_high, edge_pp, rng)  # cross-game independent
            for pos in positions:
                V = product(selected_value(leg, leg_vals) for leg in pos.legs)    # NO uses (1 - value)
                fee = quadratic_fee(pos)                                          # ~0 maker today
                realized_pos = pos.contracts * ((1.0 - V) - pos.no_bid) - fee
                equity += realized_pos; realized += realized_pos
            # halt checks (per session): if equity < peak*(1-hard_trip): STOP (KILL); daily/drawdown similar
        peak = max(peak, equity)
        if equity < peak*(1 - caps["hard_trip"]): break                          # human-only clear IRL
    return realized, equity

results = [simulate_once(s) for s in range(N_TRIALS)]   # N_TRIALS ~ 2000-10000
report( mean(realized), p05, p50, p95, P(ruin = equity < 0.70*bankroll ever), max_drawdown )
```

**Key modeling reminders:** (1) fills are throttled by CAPS on a concentrated book, not by price;
model the self-veto explicitly. (2) EV comes from the markup + same-game selection; an independent-leg
model **loses**. (3) sample a **skewed** game-size distribution (one game can dominate). (4) settlement
correlation is block-diagonal by game (comonotone-ish within, independent across). (5) all money in
integer cc if you want to match the engine exactly.

### 11.10 Worked single-combo P&L example

```
Combo: 3-leg same-game WC parlay, fair p = 0.20 (20c), longshot tier => markup +4c.
Quote:  no_bid = ($1 - fair) - markup = (1.00 - 0.20) - 0.04 = 0.76  ($0.76 = 7600 cc)
Size:   contracts = 10   (say the cap allows it: max_loss 10*0.76 = $7.60 < $20 per-combo cap)
Premium collected: 10 * 0.76 = $7.60
Maker fee: quadratic maker on combo series = $0 today (code would conservatively charge taker 0.07*10*0.76*0.24 = ~$1.28)

Outcome A - parlay MISSES (V=0), prob ~ 0.80 (it is a NO seller's friend):
   realized = 10 * ((1 - 0) - 0.76) - 0 = 10 * 0.24 = +$2.40   (keep the 24c edge/contract over fair-NO... 
   actually: we collected 0.76 and owe 0 => keep 0.76; but the "edge" vs fair-NO (0.80) is 0.04 markup + width)
   Precisely: NO fair value = $1 - 0.20 = $0.80; we sold NO at $0.76, so gross edge/contract = $0.04 => +$0.40 expected-edge, 
   and on a clean miss we bank the full $7.60 premium (payout owed = 0).
Outcome B - parlay HITS (V=1), prob ~ 0.20:
   realized = 10 * ((1 - 1) - 0.76) - 0 = 10 * (-0.76) = -$7.60   (forfeit the premium; taker collected $1/ct from posted collateral)
Outcome C - scalar (V=0.5, e.g. rain/DNP zeroing a leg's value partially):
   realized = 10 * ((1 - 0.5) - 0.76) = 10 * (-0.26) = -$2.60    (NO pays $0.50, partial)

EV (binary, using markup as the only edge): 0.80*(+$7.60 kept, net vs premium 0) ...
   simplest EV form:  EV = contracts * ( (1 - P_hit) * (1 - entry) - P_hit * entry )
                          = 10 * ( 0.80*(0.24) - 0.20*(0.76) )  = 10 * (0.192 - 0.152) = +$0.40
   i.e. +4c/contract * 10 = +$0.40 gross edge — exactly the markup, as expected on a fair tape.
```

The whole strategy is collecting that ~1-4c/contract markup across many fills while the caps bound how
much can HIT at once per game — and refusing the NORMAL / multi-game flow where the edge is zero or
negative.

---

## 12. CAVEATS / UNVERIFIED (so a simulation doesn't overstate confidence)

- **The edge is UNVALIDATED.** All P&L numbers are settlement-graded on the *resolved minority* of
  **ONE favorite-hot window** (the WC). They are a directional thermometer, **never bankable and never
  a refit input**. The profitability gate (pooled multi-week, game-clustered markout study) has **not
  been reached**. A correct, fully-enforced risk engine wrapped around an unvalidated edge just loses
  money more safely.
- **NEVER refit caps or markup on a P&L window** (standing rule). Cap values are $2,000-START research
  values awaiting operator sign-off and pooled re-derivation.
- **The candidate MC gate has processed ZERO real won auctions** — its live latency/decline behavior
  is test-asserted, not tape-confirmed. Two P0 candidate-gate defects (equity-basis overstatement;
  atomicity) were open as of the 2026-07-15 audits (likely closed by later commits — verify).
- **Settlement/reconcile-HALT chain is proven in TESTS ONLY** against a real Kalshi settlement;
  `combo_no_pays_complement` came from ONE demo settlement.
- **`portfolio_value` and `balance` are NOT atomic across endpoints** (2026-07-19 incident) — the
  receivable-drop assumption (AS1) is unverified; scalar/DNP legs are un-shielded (AS4).
- **Throughput ceiling is real:** ~1 priced RFQ/sec vs ~170 offered means the bot acts on <1% of
  eligible flow. A return sim that assumes we quote every RFQ overstates volume by ~100×.
- **Concentration is the tail risk:** one game can hold thousands of combos (~2,650 in one measured
  case). A uniform-game-size sim understates the correlated tail the caps exist to bound.
- **Bot is DOWN** on a human-only KILL and quoting is gated on the MLB/WNBA sport switch (5 readiness
  items); new MLB prop families (OUTS/RBI/SB) are UNKNOWN → fail-closed until settlement windows are
  live-verified.

---

### NEXT STEPS

- **Operator:** clear the false-positive hard-trip KILL to relight; then book remaining settlements
  and confirm the final realized-P&L statement.
- **Operator:** re-derive markup AND caps from pooled multi-week game-clustered settlement (the
  profitability gate) before any real scaling; do NOT refit on a P&L window.
- **Simulation user:** calibrate `our_win_given_trade` and the game-size skew against §11.8 anchors;
  treat all edge numbers as directional (wide CIs), and model the cap self-veto as the binding fill
  constraint. Verify the P0 audit items against current source (commits through 2026-07-22) before
  assuming they are open.
