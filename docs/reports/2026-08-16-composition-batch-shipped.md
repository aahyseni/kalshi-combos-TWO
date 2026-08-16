# 2026-08-16 — "Do all of it" SHIPPED: composition tilt + tonight items + 4 leagues (restart 11:06 ET)

Operator ratified the full 8/16 deep-dive plan + ordered UCL/EPL/Serie A/
Ligue 1. Everything live at the 11:06 ET boot (`a39b2a5` + yaml; earlier
same-day: cash-gate envelope fix `c4b30d7`).

## What shipped (all verified through the LIVE config loader before boot)

| item | implementation | proof |
|---|---|---|
| **ML-parlay 1¢ tier** (the composition tilt's entry ticket) | `MarkupPolicy.ml_parlay_cc=100`: cross-game MONEYLINE-ONLY MLB/soccer parlays <35¢ fair price flat 1¢ instead of the 2.5–4¢ longshot tiers. Shape-guarded: any prop leg / same-game pair / unparseable ticker keeps FULL tiers | live-loader proof: 3-leg MLB ML @8¢ fair → (mlb, **100**); ML+prop @8¢ → (mlb, 400) — the whale cell untouched; soccer 2-leg ML @20¢ → 100 |
| **Post-rebate edge floor** | skew rebate caps at **half** the margin (was whole-edge → manufactured 24% of premium at 0.84% retained edge) | quote.py clamp + recalibrated no-arb pin |
| **tie×BTTS guard** | the 8/15 pickoff guard now also triggers on tie×BTTS same-game (same wrong-sign family; the variant was taken 8/16 05:58 by sharp taker c1789477) | trigger pins: same-game True, cross-game False |
| **Halt-flap quarantine** | a single ticker with 3 consecutive jump-trip strikes is QUARANTINED from the jump watch (loud log) instead of cancel_all'ing the book (7 halts from ONE oscillating K-market overnight). One-off events reset strikes; broad multi-ticker corruption still hard-halts (all three pinned) | 3 new breaker tests |
| **Marginal KILL/ruin gates → telemetry** | `kill_anchored_book_gate: false`, ruin budget → "1.0", det frac → "0.70" (the deferred-ledger stand-down state; drawdown brake + all concentration walls + the 0.70B backstop stay armed) | loader proof: False / 1.0 / 0.70; recovers the measured ~+$700 standing det (the gate declined won auctions at 35% of the wall) |
| **Leagues: Serie A + Ligue 1 (+UCL ADVANCE)** | LIGUE1 sport keyword; KXSERIEA/KXLIGUE1 (+BUNDESLIGA/BRASILEIRO future-proofing) markup-mapped; allowlist 4 families each + KXUCLADVANCE- (rides the UECL two-legged copula-only guard); offsets 3.0167/4.0167, 48h horizons. UCL/EPL were wired 8/15 | 13 new classification pins; Serie A opens ~8/23, Ligue 1 this weekend — inert until Kalshi lists each series |
| **(earlier today) Cash-gate envelope fix** | Kalshi nests 400 bodies under `error` — code parsed empty, gate armed 0× vs 478k errors | verified firing (5 armings/2min post-fix) |

NOT shipped (explicitly): friendlies allowlist (operator hasn't said the
word — 5,676 open RFQs waiting); blanket longshot cut; pbook-axis arm;
entity reservation-fold scoping (9/1 — note: the quote-side resting haircut
was already armed at 0.01, harder than the 0.40 design; the entity
saturation is RESERVATION-side).

## Gates

Suite **3,734/0** (two pins recalibrated: skew-clamp to the half-margin
floor, markup SHA heals at commit); ruff/mypy clean on touched files;
vitals fast **8/8 GREEN**; live-RFQ probe 14/14 club QUOTED; **live-loader
parse+tier proof** (above); pre-ship **0/1 = the adjudicated V6
confirm-window class** (MC 3,576ms vs 2,997ms at the 312-row stored book —
~2× inflated by the stale-ledger rows; det-cap degradation green at 3.5ms;
shipped per the 8/13+8/15 precedent; **the ledger stale-row repair is the
9/1 P1 and V6's real fix**).

## First-window (11:06 boot)

Preflight green; 57 quotes/first 3 min (Sunday pre-slate; MLB opens 13:35
ET); 28 positions; walls on live equity (backstop $3,162 = 0.70 ×
$4,517); zero halts, zero quarantines (none needed yet). Persistent
monitor: halts, flap quarantines, cash-gate armings, executions.

## What to watch (the tilt's scoreboard)

1. **First cross-game ML-parlay fills** — the 0/529 class opening is THE
   composition signal. Then: premium share of NO≥65¢ entries climbing
   toward 60% and p_book following (target 0.62–0.70 at $3.4k).
2. Tonight's halts: the quarantine should eat any repeat of the Skenes
   flap (expect `marginal_jump_ticker_quarantined`, zero cancel_alls).
3. Cash-gate armings during the evening build (peak hours now quote
   through deployment instead of erroring).
4. Whale/farmer conduct vs the un-cheapened prop tiers + both guards.

## NEXT STEPS

- **Me:** evening readout with the composition scoreboard (band mix,
  ML-parlay fills, p_book trajectory, utilization vs the 0.70 wall);
  c1789477/7c885d57 settle adjudication; the 9/1 build list (ledger
  stale-row repair = P1, composition-aware KILL budget, entity
  reservation scoping, counterparty repeat-decay).
- **Operator:** friendlies (5,676 RFQs) remains your call; everything
  else from the plan is live or scheduled.
