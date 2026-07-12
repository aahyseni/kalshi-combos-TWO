# Risk engine + Monte Carlo research — synthesis & build plan

**Date:** 2026-07-12 · **Status:** research/design (nothing built yet) ·
**Deliverables:** the five detailed docs in this folder (R1/R2/R3 risk, M1/M2
Monte Carlo), each grounded in the existing code with `file:line` citations.

## The headline finding: the machinery is ~80% built — the gaps are bugs + unwired seams

Across all five agents, one theme repeated: this system already HAS a
portfolio-aware MC engine (`sim/engine.py`), a dominance-correct exposure book
(`risk/exposure.py`), a last-look with a straddle re-check, a kill switch, and
an inventory-skew seam in quote construction. "Pristine risk engine" is
therefore mostly **fixing three bugs and wiring seams that already exist**, not
a greenfield build. The three bugs are load-bearing:

| # | bug | consequence | where |
|---|---|---|---|
| **B1** | `max_loss_cc` returns premium PAID, but we're sell-only (long NO) — a hit pays out contracts×$1, ~6-11× the premium | **every cluster/tail/gross cap binds 6-11× too loose** on the exact side we always hold; the $1M single-combo tail is invisible | `risk/exposure.py:57-59` |
| **B2** | exposure aggregates on `event_ticker` (per-market-SERIES), not the GAME code | per-game correlated worst-case is silently split across market families → the "68 combos = 50% of contracts" concentration goes unseen (the P&L-sweep finding) | `risk/exposure.py:174,181,191,208` |
| **B3** | the standing risk MC (`ops/report.py::_portfolio_mc`) runs with `corr=np.eye` (independence) + complement pseudo-legs for NO | for a sell-only book this **drops within-game correlation on every position** — the risk view is NOT the book we sold (audit F8) | `ops/report.py:83-97` |

Fix B1 first — every cap depends on a correct payout number.

## Track 1 — the pristine risk engine (R1 + R2 + R3)

### R1 · the BOOK model (the operator's "game X → legs → $ → % hit")
A GAME-rooted `GameNode → LegNode → ComboNode` index over the existing
positions/quotes, re-rooted on the pricer's own `_game_key` (so risk and price
agree). **Two money axes, never summed:** `premium_at_risk` (our true max loss)
and `payout_obligation` (contracts×$1 — the bankroll lock-up the "$23.5M"
number describes). Per-leg P(hit) from the pricer's `joint.p` gives the "%
chance" column; gross same-side accumulation (not net delta) gives the
directional-bias signal. Full model + the 5 gaps: `R1_book_model.md`.

### R2 · caps / limits / kill-switch (proposed thresholds as % of bankroll)
| cap | mechanism | proposed | checked |
|---|---|---|---|
| absolute $ exposure | Σ committed payout ≤ ceiling | 4× bankroll | quote+confirm+maint |
| **%-of-game (cluster)** | worst-case payout per GAME | **10%** | quote+confirm+maint |
| per-combo max payout | one position's payout (conservative size) | 2% | quote+confirm |
| one-directional / theme | net signed delta by (leg_family, side) | 15% (measure→tighten) | quote+maint |
| fill payout budget | Σ payout / rolling window, decremented on ACCEPT | 5% / 2s | confirm+maint |
| fill-velocity | same window, hard ⇒ halt | 10% / 2s | confirm+maint |
| daily-loss halt | realized+unrealized vs bankroll | 4% | maintenance |
| peak-drawdown halt | (peak−current)/peak | 6% | maintenance |
| hard-trip kill | KILL file, restart-surviving, human clear | 8% | maintenance |
Cross-cutting: quote-time caps decide whether to REST a quote (hypothetical
mass-acceptance bound); confirm-time caps gate the actual fill; near-cap games
**widen into a decline** via the skew seam (not a cliff); fail-closed; a
starvation watchdog because silently blocking all flow is a bug. Full table +
10 gaps: `R2_caps_killswitch.md`.

### R3 · hedging / book-balancing + pregame hardening
- **Inventory-aware skew — the seam is BUILT but hard-wired to 0**
  (`pricing/quote.py:128` ← `inventory_skew_cc`). Feed it: price TIGHTER on
  RFQs that offset a leg/game we're overweight, WIDER on ones that concentrate
  — the sell-only book self-balances via *which combos it accepts and how it
  prices them*. **We CAN also hedge on single-leg markets** — the sell-only
  mandate is a combo-quote constraint only; laying off a leg delta leaves the
  correlation residual = exactly the book CLAUDE.md intends (gated post-Phase-7).
- **Pregame vs courtsiders — precision, not padding.** The last-look straddle
  re-check already exists. Add a **precision ladder** (embedded-ET > schedule
  feed > estimate > UNKNOWN) so exact starts let us quote to ~2 min before
  kickoff — recovering the ~1.5h of near-kickoff flow the blunt 4.5h estimate
  discards — while a strict confirm-cutoff stays safe. Measure pickoff-vs-flow
  with the existing markout tracker (adverse short-horizon markout = the
  courtsider signature), tune the buffer only on pooled game-clustered evidence.
  Ships dark/shadow first. Full change list: `R3_hedging_pregame.md`.

## Track 2 — Monte Carlo, top to bottom (M1 + M2)

### M1 · MC for portfolio/book risk — the engine already exists
`sim/engine.py::simulate` already MCs the WHOLE book jointly on one sampled leg
matrix (shared-leg correlation captured); `marginal_impact` already does
with/without-candidate on common random numbers; `leg_deltas` already does
per-leg tail sensitivity. **What's missing is the bridge + the B3 fix:** a new
pure `sim/book_model.py` that builds the MC's `(legs, corr, positions)` by
calling the real `build_sgp_correlation` per game and scattering into a
block-diagonal global matrix (cross-game = 0 → the GAME is the risk unit), plus
the ~2-line NO-side fix so within-game correlation survives. Two tiers: full MC
(100k, off hot path) for book review; fast marginal MC (5-20k, CRN) at quote
time for candidate ΔCVaR → feeds R3's skew. Five outputs: P&L distribution,
VaR/CVaR@95/99, P(ruin), per-game/leg tail attribution, marginal ΔCVaR. Full
architecture: `M1_mc_portfolio.md`.

### M2 · MC methodology deep-dive
The latent-normal Gaussian-copula sampler is correct. Ranked recommendations:
1. **Glasserman-Li importance sampling + cross-entropy** for the ruin/tail
   number — this book IS the portfolio-credit-risk setting (games = common
   factors, parlay hits = defaults); naive MC needs ~10⁸ paths for a 10⁻³ ruin
   prob, IS needs 10⁵-⁶. The only thing that makes a NO-seller's tail precise.
2. **Block-structured game-keyed sampling** — makes the GAME the risk unit; the
   prerequisite factor structure for #1.
3. **VaR/ES settlement coverage backtest** (Kupiec, game/week-clustered) — the
   ruler that proves the tail isn't confidently wrong.
4. **MC↔analytic parity gate in CI** — catches L10-class quiet failures where
   one side's ρ silently dies.
5. Control variate (the analytic fair) for the mean; antithetics; scrambled-Sobol.
Determinism via `SeedSequence.spawn` (never `seed+1`). Full deep-dive:
`M2_mc_methodology.md`.

## Proposed build order (when operator says go — each phase shippable + gated)

1. **B1 fix** (payout-aware `max_loss`) + B2 (game-key aggregation) — the
   foundation; every cap is wrong until these land. Ship with the two-axis book.
2. **R1 book** + the R2 caps that only need the book (per-combo, per-game,
   directional, absolute $) — quote-time + confirm-time gates, fail-closed.
3. **B3 fix** + `sim/book_model.py` + parity gate — the MC risk view becomes
   consistent with the book we sold; adds VaR/CVaR/tail-attribution.
4. Fill-velocity governor + drawdown/hard-trip (the mass-acceptance + ruin
   defenses) — consume the MC tail outputs.
5. Inventory-skew wiring (R3-A) + the pregame precision ladder (R3-B) — both
   dark/shadow first, graded before live.
6. Importance-sampling tail estimator (M2 #1) + VaR coverage backtest — the
   precise ruin number.

## Decisions owed by operator
- The **bankroll figure** to anchor every % cap (research used $5k baseline).
- Confirm the **two-axis capital model** (premium-at-risk vs payout-obligation).
- The per-leg **$-attribution rule** (equal split vs responsibility split).
- Sign-off on the proposed cap %s (or set your own); the hard-trip kill %.
- Go/no-go to START the B1/B2 foundation fixes (highest impact, least risk).
- Whether active single-leg hedging is ever in scope (design-only until Phase 7).

## NEXT STEPS
- Operator: read the five docs, set the bankroll + cap decisions, greenlight the
  B1/B2 foundation.
- Me/engine: on go, implement in the phased order above — each phase prototyped
  in test, ported with a parity check (rule 8), gated before the next.
- The markup decision stays separate + deferred (pooled multi-week); the risk
  engine is what makes ANY markup safe to run live.
