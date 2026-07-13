# Risk engine — end-to-end rundown (how it works)

**Date:** 2026-07-13. Written after the autonomous overnight build merged all six
RISK_BUILD_PLAN phases to `main` (`e12f4f2`, suite **1596/0**). This is the
"how it will work" walkthrough. Everything below is **SHADOW / DARK / NOT-LIVE** —
no live switch is flipped; nothing quotes real money. The go-live runway (operator
sign-offs + measurement) is the section at the end.

---

## 1. What got built (six phases, one line each)

| Phase | What | State |
|---|---|---|
| 0 Foundation | two money axes (max_loss vs settlement notional) + game-key aggregation + BalanceTracker spine | merged |
| 1 Correct the money | equity-aware bankroll `min(SOD, cash+½·PV)`, ledger fees ($0 for us), scalar settlement, rename | merged (2 judge defects fixed) |
| 2 Caps + slate | 9 %-of-bankroll caps incl. the NEW slate cap, all SHADOW; give-back halts; watchdog | merged (2× judge PASS) |
| 3 Reservation | single-writer headroom reservation BEFORE confirm; timeout=assume-committed+reconcile | merged (judge PASS) |
| 4 Portfolio MC | real book + real pricing correlation → VaR/CVaR/ruin/tail-attribution + challenger overlay | merged (judge PASS, **parity-gated**) |
| 5 Quoting policy | inventory skew (DARK) + widen-vs-decline + pregame precision ladder | merged (**judge caught a CRITICAL sign inversion**, fixed) |
| 6 Watchdog + gates | out-of-process kill supervisor + 7 circuit breakers + prod go-live gates | merged (judge caught 2 kill-path bugs, fixed) |

The adversarial `implement → judge → fix → re-judge` gate was not a rubber stamp:
it FAILED Phase 5 twice (the skew sign was economically backwards — it would have
*concentrated* the book) and Phase 6 twice (a restarted bot could quote before the
KILL file was read; the emergency cancel-all didn't paginate). Those are exactly
the bugs you never want to find in production.

---

## 2. The whole pipeline (where the risk engine sits)

```
  RFQ arrives (taker wants to buy a parlay)
      │
      ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ FILTERS + PREGAME GATE (rfq/filters.py, rfq/pregame.py)              │
 │  · leg-series allowlist (MLB+WC only)  · any leg started ⇒ decline   │
 │  · precision ladder: embedded-ET > schedule-feed(seam) > estimate    │  Phase 5
 └─────────────────────────────────────────────────────────────────────┘
      │ (pregame, whitelisted)
      ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ PRICING (pricing/engine.py) → fair + inventory_skew_cc              │
 │  · top-down: leg marginals × copula/structural joint                 │
 │  · inventory skew (DARK): tighter NO on balancing flow, wider on     │  Phase 5
 │    concentrating — applied as 0 while enabled=False                  │
 └─────────────────────────────────────────────────────────────────────┘
      │ fair
      ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ RISK GATE at QUOTE time (risk/limits.py LimitChecker.check)         │
 │  · mass-acceptance worst case (every open quote fills NOW)           │  Phase 0/2/4
 │  · caps on the GAME cluster + slate + directional + utilization      │
 │  · portfolio-CVaR cap reads the latest MC snapshot (SHADOW)          │
 │  · shadow breaches LOG-ONLY; enforced breaches block                 │
 └─────────────────────────────────────────────────────────────────────┘
      │ (passes / widens / declines)
      ▼  quote sent  ───────────────►  taker ACCEPTS a side
                                            │
      ┌─────────────────────────────────────┘
      ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ RESERVE headroom BEFORE confirm (risk/reservation.py)               │  Phase 3
 │  · single-writer, atomic: re-check vs committed + ALL outstanding    │
 │    reservations + this candidate → record + version, or decline      │
 └─────────────────────────────────────────────────────────────────────┘
      │ reserved
      ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ LAST LOOK (risk/lastlook.py decide_confirm) — warm state, <1ms      │
 │  · leg moved? stale? in-play (M_c re-check)? risk breach? → decline   │  Phase 5
 └─────────────────────────────────────────────────────────────────────┘
      │ confirm
      ▼  confirm round-trip  →  book the position (exposure book)
      │      · success → reservation.commit (book counts it)
      │      · timeout → reservation.mark_unconfirmed (HOLD headroom, reconcile)
      ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ SETTLEMENT → BalanceTracker realized-P&L ledger (risk/balance.py)   │  Phase 1
 │  · NO pays contracts×(1−V); fees booked; reconcile to the cent        │
 └─────────────────────────────────────────────────────────────────────┘

  ═══ ALWAYS-ON, OUT OF BAND ═══
  · maintenance tick: refresh P&L, run the give-back halts, run 4 live circuit breakers
  · SafetySupervisor (separate process): watches heartbeat → emergency cancel-all + KILL   ┐ Phase 6
  · go-live preflight: prod refuses to quote until all 5 gates green                       ┘
```

---

## 3. The core ideas, explained

### 3a. Two money axes that are NEVER summed (Phase 0/1)
A parlay seller is long NO. When a parlay **hits** we lose the *premium we paid*
(`max_loss_cc`), NOT the $1 payout. But we *tie up* $1/contract of settlement
notional (`gross_settlement_notional_cc`). These are orthogonal: loss caps bind on
the first, utilization/concentration caps on the second. Conflating them (the old
code did) makes every cap 6–11× wrong for a NO book.

### 3b. The GAME is the risk unit (Phase 0)
Combos share legs and games. The exposure book aggregates everything per **game**
(the game-code after the series prefix, via `pricing.grouping.game_key`), not per
raw event — because within one game leg outcomes are maximally correlated (a
blowout settles every over/favorite/scorer leg at once). *(§4 shows this on real
tape: 6,000 combos → 191 games, one game carrying 2,650 combos.)*

### 3c. The cap hierarchy (Phase 2) — all SHADOW, at $2,000
| Cap | Value | Binds on |
|---|---|---|
| per-game correlated loss | **8%** ($160) | worst-case loss / game |
| per-combo | 1% ($20) | single-position loss |
| directional / theme | 10% | net leg exposure / game |
| **slate (NEW)** | 8% | Σ game-loss over one ET-day window |
| daily-loss halt | 6% | realized+unrealized |
| drawdown halt | 10% | give-back from intraday peak |
| hard-trip kill | 12% | deeper give-back |
| utilization backstop | 3× | whole-book settlement notional |
| portfolio-CVaR (Phase 4) | 15% | MC operative ES (max of copula/challenger/stress) |

Thresholds are exact integers (`Fraction` × bankroll). The bankroll denominator
is equity-aware so *deploying* capital can't shrink the caps and a mark *gain*
can't inflate them. Everything runs as an **additive shadow layer** next to the
existing enforced hard-dollar caps — it logs every would-be breach with zero quote
impact until the operator flips `caps_shadow_mode: false`.

### 3d. Reservation before confirm (Phase 3)
Between "the check passed" and "the fill is booked" there's a network round-trip.
A single-writer reservation ledger claims the headroom the instant we decide to
confirm, so two RFQs can never both spend the same room (safe for any future
fan-out). A confirm **timeout = assume committed** (hold the headroom, reconcile
against the exchange later) — a reservation must never vanish on a lost ack.

### 3e. Portfolio Monte Carlo + challenger overlay (Phase 4)
The existing MC engine now samples the **real book** under the **real pricing
correlation** (block-diagonal by game, NO-side = latent sign-flip, not the old
independence bug). It produces VaR/CVaR, P(ruin), and an exact per-game tail
decomposition (Σ per-game = CVaR). The **challenger overlay** takes
`operative ES = max(copula ES, correlation-inflated ES, deterministic all-hit
stress)` so a single correlation error can't get approved twice. A **parity gate**
(a one-combo book reproduces the pricer's fair to the cent) proves the risk sim
prices the same joint we quoted.

### 3f. Inventory skew — the sign that mattered (Phase 5)
Sell-only: our only lever is which NO flow we win and at what price. **Concentrating**
combo (piles onto a hot game) → **lower** our NO bid → the taker's YES costs more →
we sell **less**. **Offsetting** combo → **raise** our NO bid → sell **more**. The
first cut had this backwards (it would have deepened concentration); the judge's
real-pricer test caught it. Ships **DARK** (computed + logged, applied as 0) until
a pooled shadow-markout study authorizes enabling.

### 3g. The kill path (Phase 6)
A separate-process **SafetySupervisor** with its own credential watches the bot's
heartbeat; a missed beat → it cancels every resting quote (its own credential,
reserved write budget so it survives a 429 storm) and writes the KILL file (which
survives a restart — a revived bot re-reads it and halts, now checked
*synchronously* at startup). Seven fail-closed **circuit breakers** trip the kill
switch on the known failure signatures (staleness, latency, 429 burst, and the
reconciliation mismatch are live-sampled; marginal-jump/unmapped-game/metadata are
built + tested, pending hot-path wiring). Prod **go-live gates** refuse to quote
unless all five preflight conditions are green.

---

## 4. Real-tape demonstration (the risk engine on recorded prod flow)

Loaded **6,000 distinct real multi-leg combos** from the prod recorder tape
(`data/combomaker-prod.sqlite3`, read-only), built live risk positions from their
real legs, and ran them through the live `ExposureBook` + `game_key` + the shipped
shadow caps at a $2,000 bankroll. (Sizing is illustrative — 5-ct NO @ $0.85; the
*concentration* is size-independent and is the real finding.)

```
 6,000 combos  ─────►  191 distinct GAMES     (the true risk unit, on real tape)

 Top-concentrated games (distinct combos on one game):
   26JUL14FRAESP   2,650 combos      ← one World Cup match carries 2,650 combos
   26JUL12CHIDAL   2,452 combos
   26JUL15ENGARG   2,175 combos
   26JUL12INDLV    2,111 combos
   ...

 SHIPPED shadow caps flagged (what they WOULD block, log-only today):
   skip_game_loss_cap            65 games (of 191) exceed 8%/game
   skip_mass_acceptance_breach   18
   skip_directional_cap           5
   skip_utilization_backstop      1  (whole-book notional $86,535 ≫ $6,000 = 3× bankroll)
   skip_slate_cap                 1
```

**Read:** the concentration the P&L sweep warned about is real and large — a single
game attracts thousands of correlated combos. The live exposure book clusters them
by game correctly, and the caps flag exactly those games. This is the whole reason
the risk engine exists, demonstrated on real flow. (Full loss/notional attribution
per game printed by `tmp/risk_tape_replay.py` — a throwaway harness importing only
the live modules; a permanent `tools/` version is a go-live-runway item.)

---

## 5. What is ON vs OFF right now

| Layer | State | Flip to enable |
|---|---|---|
| Existing hard-dollar caps | **ENFORCED** (unchanged) | — |
| New %-of-bankroll caps + slate | SHADOW (log-only) | `risk.caps_shadow_mode: false` (after review) |
| Give-back drawdown/hard-trip halts | wired, SHADOW | same flip + a peak-equity feed |
| Reservation ledger | wired, transparent | active on any fan-out (no-op today) |
| Portfolio-CVaR cap | SHADOW, no live snapshot feed yet | maintenance-tick MC loop + flip |
| Inventory skew | **DARK** (applied 0) | `pricing.skew.enabled: true` (after markout study) |
| Widen-vs-decline | SHADOW | its enable flag (after study) |
| Pregame precision (M_q/M_c, schedule) | SEAM, conservative 4.5h kept | a verified schedule feed + tighter margins |
| 4 circuit breakers | LIVE-sampled | on (fail-closed) |
| 3 circuit breakers | built + tested, not sampled | hot-path signal wiring |
| External supervisor | built (standalone process) | dedicated credential + separate host |
| Prod quoting | **BLOCKED** (go-live gates) | `--confirm-live` + limits + whitelist + preflight |

---

## 6. The go-live runway (operator decisions owed)

Per RISK_BUILD_PLAN "After Phase 6": SHADOW the whole stack → flip enables **one at
a time on measured evidence** → tiny live at **$2,000** with conservative caps →
accumulate POOLED MULTI-WEEK game-clustered settlement → re-derive caps AND markup
from that data (never one window) → scale.

Before any live quote, the operator owns:
1. **Flip `caps_shadow_mode`** after reviewing real shadow-log behaviour; confirm
   the cap %s + haircut 0.5 + the UTC day-boundary.
2. **Provision the `KALSHI_SUPERVISOR_*` credential** (a separate Kalshi key,
   ideally a separate host), run the kill-drill against the demo exchange.
3. **Wire the 3 not-yet-sampled breakers**, the maintenance-tick BookRiskSnapshot
   MC loop, and the marginal-ΔCVaR → skew consumption.
4. **Enable inventory skew** only after the pooled shadow-markout study.
5. **The MARKUP decision** — pooled multi-week, game-clustered, *never* refit on a
   P&L window. The caps assume a profitable markup but do not set it.

## NEXT STEPS
- **Owner: operator** — the five sign-offs above, in order. Nothing is live until you do.
- **Owner: next session** — build the permanent `tools/` risk-on-tape shadow analyzer
  (the throwaway `tmp/risk_tape_replay.py` is the prototype); wire the maintenance-tick
  MC snapshot loop so the portfolio-CVaR cap has a live feed; wire the 3 breakers.
- **Owner: measurement** — the pooled shadow-markout study that authorizes enabling
  skew + tightening the pregame buffer (pre-registered, multi-week, game-clustered).
