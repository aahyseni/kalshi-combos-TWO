# 2026-07-19 — ARG-champ × Messi-1+ fair vs field: decomposition + parameter recommendation

**Status: MEASUREMENT COMPLETE ~10:25 AM ET — operator decision owed before the 2:45 PM ET final.**
Tool (ruff-clean, read-only, market-data GETs only):
`tools/diagnostics/argmessi_fair_vs_field_20260719.py`
(rerun: `.venv/Scripts/python.exe tools/diagnostics/argmessi_fair_vs_field_20260719.py`)

Combo (tape-verified from the RFQ store — the operator's "KXWCPLAYERGOALS" series name was
approximate; the real scorer series is **KXWCGOAL**):

```
yes:KXMENWORLDCUP-26-AR                      (aliased -> KXWCADVANCE-26JUL19ESPARG-ARG)
yes:KXWCGOAL-26JUL19ESPARG-ARGLMESSI10-1     (Messi scores 1+, full game incl ET)
combo tickers: KXMVESPORTSMULTIGAMEEXTENDED-S202609506CD2247-39F452DE826
               KXMVECROSSCATEGORY-S20263AB06A8E27D-39F452DE826
```

## The three numbers (live books ~10:15 AM ET: P(ARG champ)=42.01c, P(Messi 1+)=35.13c)

| | fair YES | P(Messi \| ARG champ) | implied copula ρ |
|---|---|---|---|
| **OUR engine (live path, reproduced to the cent)** | **21.61c** | 51.4% | 0.45 (shipped) |
| **Our own DC structural model** (identified from the final's other books) | **22.75–22.88c** | 53.6–54.9% | 0.51–0.53 |
| **Field, naive reading of 26.7c (3.74x)** | 26.70c | 63.6% | 0.725 |
| **Field, fee-adjusted maker fair (see below)** | **~22.8–23.1c** | ~54.5% | ~0.52 |
| independence (sanity floor) | 14.76c | 35.1% | 0 |

**Headline: after removing the 7% taker fee baked into quoted combo prices, the field's maker
fair ≈ 22.8–23.1c — almost exactly our own structural model's 22.75–22.88c. The real model
gap is our copula prior (0.45) sitting 1.2–1.3c of fair below our own DC machinery (0.52).**

## 1. Reproduction — which machinery engages (exact)

Engine path reproduced offline through the LIVE `PricingEngine.compute_joint` on live REST
books (mirrors built with the live wire parsers; `KalshiBookSource` microprice beliefs).
Output matches the live decision tape **to the cent** (tape 10:06 AM ET: `fair_cc=2161`,
`no_bid_cc=7630` on mids 42.00/35.13):

```
fair YES = 0.2161 (21.61c), uncertainty 0.0297
  note: structural fallback: 1 team-level legs cannot identify (lam_a, lam_b)
  note: pair soccer:advance|player_goal:same=+0.450
  note: corr band: p_lo=0.1922 p_hi=0.2422
markup: soccer 2.0c (fair<35c tier) -> YES ask 23.61c -> NO bid 76.3 (grid-down)
```

The intended pricer for this combo — the DC structural plan via the champion alias — **declines
this exact 2-leg shape**: `invert()` needs ≥2 team-level constraints and the combo carries only
ONE (the aliased Advance leg; the Messi leg is a player leg whose share is a free parameter).
So it silently falls back to the copula pair prior:

```
┌────────────────────────┐   alias    ┌─────────────────────────────┐
│ KXMENWORLDCUP-26-AR    │──────────▶│ ADVANCE leg (KXWCADVANCE-…) │──┐  structural try_price:
└────────────────────────┘            └─────────────────────────────┘  │  1 team-level leg
┌────────────────────────┐            ┌─────────────────────────────┐  ├─▶ UNDER-IDENTIFIED
│ KXWCGOAL-…-ARGLMESSI10 │──────────▶│ PLAYER_GOAL leg (share q)   │──┘       │
└────────────────────────┘            └─────────────────────────────┘         ▼
                                              copula fallback: advance|player_goal:same = 0.45
```

3+ leg champion combos with a second team-level leg (totals/BTTS) DO price structurally; it is
precisely the headline 2-leg "ARG wins it + Messi scores" parlay that lands on the hand prior.

## 2. Structural cross-check — what our own DC model says this joint is

Same shipped model, identified from the final's other live books (three constraint sets, all
agree; residuals ≤0.046):

| constraint set | λ_ESP / λ_ARG | Messi share q | joint | cond | implied ρ |
|---|---|---|---|---|---|
| advance + total3 (exact) | 1.309 / 1.023 | 0.386 | **22.88c** | 54.5% | 0.525 |
| + btts (lsq) | 1.370 / 1.101 | 0.359 | 22.86c | 53.6% | 0.509 |
| + game-ml/tie (lsq) | 1.305 / 1.001 | 0.394 | 22.75c | 54.9% | 0.529 |

Why the DC conditional (54–55%) exceeds the 0.45-prior conditional (51.4%): E[ARG goals |
champ] = 1.84 vs 1.12 unconditional; the 0-0-then-pens dilution is only ~5% of champion
states; and the scorer market settles **incl ET** — advance-via-ET states ADD Messi scoring
window. The 0.45 prior was derived 2026-07-07 as "directional markets retain ~0.8" of the
moneyline coupling (shootout decoupling); measured directly at the final's marginals the
retention is ~104% (0.52 > ml's 0.50), because the ET-window effect nearly cancels the pens
dilution for a player-prop leg. **0.45 undershoots our own model by 1.2–1.3c of fair here.**

## 3. Field comparison — the 26.7c is mostly taker fee + markup, not a 63% conditional

Public combo prints (GET /markets/trades, ~2h window to 10:15 AM ET, 1000-print cap each):
MULTIGAMEEXTENDED variant trades 24.0–25.9c with the volume mode at **25.6c (98k ct) and
25.9c (66k ct)**; CROSSCATEGORY 24.1–26.4c. Nothing printed below 24.0c today — yet our own
23.3–23.9c fills sit inside that window. Resolution (measured): **prints are the taker's
all-in price = fill price + 7% taker fee.** Three of our target-cost fills reconcile to
<0.1c: ask 23.6→24.86 (print/target 24.88), 23.7→24.97 (25.00), 23.9→25.17 (25.19). This
also resolves the CLAUDE.md open question on RFQ combo fee attribution direction: the taker
carries it.

Fee-adjusting the field: all-in mode 25.6–25.9c → field maker asks ≈ 24.3–24.6c → at the
field's typical 1.5c markup, **field maker fair ≈ 22.8–23.1c = our structural fair**. The
operator's 26.7c (3.74x) reads as the all-in of a ~25.4c field ask (fair + ~2.3–2.6 markup),
not as a 63.6% conditional. A true 63.6% conditional would need Messi's per-goal share to
jump from the market-implied 0.386 to 0.479 in ARG-win states (q_win solver) — structurally
implausible; the field is not actually pricing that.

Our position in the auction (all-in space): our 24.9–25.2 vs field mode 25.6–25.9 — we are
systematically 0.4–1.0c the cheapest, which is exactly why one taker bought 29 clips of this
and nothing else. Adverse selection by construction: we win this auction *every* time.

## 4. The parameter — named, current, recommended

| item | value |
|---|---|
| parameter | `CorrelationConfig.pair_rho_by_sport["soccer"]["advance|player_goal:same"]` |
| location | `src/combomaker/ops/config.py:605` (band: `pair_rho_uncertainty["soccer:advance|player_goal:same"]=0.15`, config.py:1336) |
| current | **0.45** (derived hand prior, 2026-07-07 "directional retains ~0.8" attenuation) |
| recommended | **0.52** (this measurement: DC-implied 0.509–0.529 at live final marginals; coincides with the fee-adjusted field maker fair) |
| effect | fair 21.61 → ~22.88c (+1.27c); ask 23.61 → 24.88c; all-in ~26.2c ≈ at/above the field mode — no longer the systematic cheapest |
| (b) midpoint to field? | matching raw 26.7c needs ρ=0.725, fee-adjusted field fair needs ~0.52. **0.52 IS the defensible point**; anything above ~0.55 is a refit to the fee-inflated print, reject |
| NOT recommended | touching `:opp` (−0.45, config.py:606) — unmeasured here; the ET-window argument suggests its magnitude also deserves a DC re-derivation, file as follow-up |

This is a measurement/structural promote (DC-derived, cross-checked against the fee-adjusted
field), not a P&L refit — consistent with the no-refit rule. Rule 8b applies: config-value
promote + re-run of the WC backtest against the promoted config, after operator sign-off.

## 5. P&L consequence of the ~$324 cluster (29 fills, 423.48ct NO @ avg 76.59, 7/18 12:24 PM → 7/19 10:16 AM ET)

| if TRUE fair YES is | expected P&L on the cluster | vs booked +$4.79 |
|---|---|---|
| our shipped fair 21.61c | +$4.79 (booked) | — |
| **our structural / fee-adj field fair ~22.88c** | **+$2.25** | −$2.5 (booked edge ~2x overstated) |
| naive field fair 25.2c | −$7.56 | −$12.4 |
| naive field ask 26.7c | −$13.91 | −$18.7 |

Most-likely truth (structural = fee-adjusted field): the cluster is **slightly +EV, not
underwater** — but the 2c markup we thought we were charging was really ~0.7c over best fair,
and the taker will keep coming until the promote lands or kickoff cuts quoting (2:44 PM ET).

## NEXT STEPS:

1. **Operator (decision, before ~2:45 PM ET kickoff if the fix should apply to the final):**
   sign off promoting `soccer advance|player_goal:same` 0.45 → 0.52 (band 0.15 unchanged).
   Note quoting on this combo auto-stops at ~2:44 PM ET (pregame gate) regardless; residual
   cluster risk is bounded at the table above.
2. **On sign-off (any session):** edit `ops/config.py:605` (pure config promote), re-run
   `tools/backtests/wc_backtest.py` against the promoted config (rule 8b gate), restart the
   bot. Expected live effect: this combo's ask 23.6→24.9; other advance×same-scorer combos
   richen ~0.5–1.3c depending on marginals.
3. **Follow-up (post-WC, owner: next calibration pass):** DC re-derivation of the full
   advance|player_goal pair (:same AND :opp) across the marginal grid — the static ±0.45 pair
   is a two-point approximation of a marginal-dependent surface (this measurement: +0.52 at a
   42/35 pair); also consider routing 2-leg advance×scorer through the structural pricer by
   augmenting identification with the game's own total/moneyline books (design change, not a
   config promote).
4. **Bookkeeping (this session, done):** fee finding (prints = taker all-in) recorded here;
   NOTES.md open-question on RFQ combo fee side can cite this report.
