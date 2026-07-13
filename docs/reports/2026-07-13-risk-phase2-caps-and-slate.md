# Risk engine PHASE 2 — cap hierarchy + slate cap (SHADOW mode)

**Date:** 2026-07-13. **Branch:** `risk-phase2` (based on `main`@`de80e83`).
**Scope:** RISK_BUILD_PLAN Phase 2 — the R2 %-of-bankroll cap hierarchy + the
NEW slate/time-window cap, wired as an ADDITIVE SHADOW layer (log-only, zero
quote impact) alongside the existing enforced hard-dollar caps. Cap values are
the researched $2,000 START set (`docs/research/CAP_recommendation_2000.md`).

**Suite: 1355/0 → 1406/0 (+51, 0 failed), 3 deselected.** (+42 the implement
pass, +9 the orchestrator follow-up: peak-latch ×4, config-validator ×3,
give-back-through-lifecycle ×2.) mypy strict + ruff clean on every touched file.
Adversarial judge PASS on the implement pass; the follow-up (peak latch,
give-back escalation, config validation) closed the two cheap judge-flagged
latents and is judged before merge.

## What Phase 2 adds (one picture)

```
                    LimitChecker.check(...)
                            │
        ┌───────────────────┴────────────────────┐
        ▼                                         ▼
  EXISTING ENFORCED CAPS                 NEW R2 %-OF-BANKROLL LAYER (_r2_breaches)
  (hard-dollar, UNCHANGED)               thr_cc = frac.num * bankroll_cc // frac.den
  · size / notional per quote            · game-loss 8%   (LOSS axis, per game)
  · market/game delta                    · per-combo 1%   (LOSS axis, one position)
  · gross-notional $5k                   · directional 10%(net delta per game)
  · event worst-case loss                · slate 8%       (Σ game loss / slate) ★NEW
  · max open quotes                      · daily-loss 6%  (realized+unrealized)
  · daily-loss halt                      · drawdown 10% / hard-trip 12% (give-back)
        │                                · utilization 3× (NOTIONAL axis backstop)
        │                                · fail-closed if no bankroll
        │                                         │  every breach shadow=caps_shadow_mode
        └──────────────► Breach list ◄────────────┘  (Phase-2 default TRUE = log-only)
                            │
        lifecycle._partition_breaches(): shadow → LOG only; enforced → act
```

## The cap set (researched value · axis · threshold formula · reason code)

All thresholds are integer-exact: `thr_cc = frac.numerator * bankroll_cc //
frac.denominator` (no binary float for money; `Fraction` for the percentage).
Bankroll = `BalanceTracker.risk_bankroll_cc` (equity-aware, fail-closed on stale).

| cap | value | axis it binds | threshold formula | reason code |
|---|---|---|---|---|
| %-of-GAME correlated loss | **8%** | `worst_case_loss_by_game_cc` per game (LOSS) | `loss_cc > 8/100·bankroll` | `SKIP_GAME_LOSS_CAP` |
| per-COMBO max loss | **1%** | a candidate position's `max_loss_cc` (LOSS, **not** the $1 notional) | `max_loss_cc > 1/100·bankroll` | `SKIP_PER_COMBO_LOSS_CAP` |
| one-directional / theme | **10%** | net directional exposure per game (`delta_by_game`) | `\|delta\|·$1 > 10/100·bankroll` | `SKIP_DIRECTIONAL_CAP` |
| **SLATE (time-window)** ★NEW | **8%** | Σ `worst_case_loss_by_game_cc` over one slate (LOSS) | `slate_loss > 8/100·bankroll` | `SKIP_SLATE_CAP` |
| daily-loss (soft) | **6%** | realized+unrealized from day start (LOSS) | `-daily_pnl >= 6/100·bankroll` | `HALT_DAILY_LOSS` |
| peak-drawdown | **10%** | give-back = peak−current equity | `give_back >= 10/100·bankroll` | `HALT_DRAWDOWN` |
| hard-trip KILL | **12%** | give-back (deeper) | `give_back >= 12/100·bankroll` | `HALT_HARD_TRIP` |
| absolute-$ utilization backstop | **3×** | whole-book `gross_settlement_notional` (UTILIZATION) | `Σ notional > 3·bankroll` | `SKIP_UTILIZATION_BACKSTOP` |
| fill-velocity | 5%/2s soft, 10%/2s hard, 8 fills/2s | committed notional / window | params carried in `RiskLimits` | `HALT_FILL_VELOCITY` |
| bankroll unavailable (fail-closed) | — | (no bankroll ⇒ %-caps can't compute) | see below | `SKIP_BANKROLL_UNAVAILABLE` |

### Why each number (from CAP_recommendation_2000.md)
- **game 8%** — theory's ruin knee is 10% (the cliff); 8% is the safe start; THE
  headline cap. **per-combo 1%** — empirically the strongest downside lever;
  forces whale RFQs to be sliced. **directional 10%** — a theme ≈
  same-game-correlated on a losing night, so held near the game cap.
  **slate 8%** — the review's best catch: a daily-loss halt only fires AFTER
  losses and many games settle in one window, so this pre-trade cap limits what
  one evening's slate can cost before any halt reacts; starts equal to the game
  cap. **daily 6%** — between theory 4% (nuisance-trips on $2k variance) and
  empirical 8%. **drawdown 10% / hard-trip 12%** — catch profit give-back a
  from-zero cap misses; hard-trip is outside anything the tape produced (worst
  in-sample DD 14.5%), human-only clear. **utilization 3×** — a loose backstop
  ABOVE the % caps, on the DISTINCT notional axis.

## The two money axes are NEVER summed (R1/R2 invariant #2)

Every new %-cap binds on the **LOSS axis** (premium at risk: `max_loss_cc` /
`worst_case_loss_by_game_cc`) EXCEPT the utilization backstop, the ONLY new cap
on the **gross-settlement-notional (utilization)** axis. Proven by
`test_utilization_backstop`: a 100-contract @ 1¢ position has $1 LOSS but $100
NOTIONAL — with a small bankroll the 3× backstop trips on NOTIONAL while the game
LOSS cap on the SAME position does NOT. `test_per_combo_binds_on_loss_not_notional`
is the mirror: a huge-notional/tiny-premium candidate does NOT trip the per-combo
LOSS cap.

## The per-combo cap binds on LOSS, and the name is fixed

In `limits.py` the existing local `notional_dollars = position.max_loss_cc/10_000`
was misnamed (it is the LOSS axis). Renamed to `candidate_loss_dollars`; the
detail string now reads `"candidate loss $X > $Y"` (was "notional"); the new
per-combo cap compares `per_combo_loss_frac × bankroll` vs each candidate's
`max_loss_cc` with a comment that this is premium-at-risk, never the $1 notional.
(`test_per_quote_notional` updated to assert `"loss"` in the detail.)

## The SHADOW mechanism — how zero quote impact is guaranteed

- `Breach` gained `shadow: bool = False`. Every R2-layer breach is emitted with
  `shadow=caps_shadow_mode` (the config flag, **DEFAULT TRUE in Phase 2**).
- The consumer split lives in `lifecycle._partition_breaches()`: a `shadow=True`
  breach is LOGGED (`risk_cap_shadow_breach` — reason, detail, bankroll) and then
  **DROPPED** from the returned list. Only `shadow=False` breaches reach the
  block/decline/halt logic. This is the one place shadow is enforced-away, so
  **every** `check()` call site (pre-quote, last-look, maintenance-tick) is
  shadow-safe by construction.
- **Proof (behavioural, through the real hot path, `test_risk_shadow_mode.py`):**
  - `test_shadow_cap_breach_does_not_block_the_quote`: tiny bankroll trips every
    R2 cap, `caps_shadow_mode=True` → the quote is STILL sent + tracked.
  - `test_enforced_cap_breach_blocks_the_quote`: SAME setup, `caps_shadow_mode=
    False` → nothing sent (the caps enforce when flipped).
  - `test_shadow_daily_loss_does_not_halt` vs `test_enforced_daily_loss_halts`:
    a would-fire 6% daily cap does NOT halt in shadow, DOES halt when enforced.

## The SLATE cap design (the review's best catch)

- **Bucket key = US/Eastern CALENDAR DAY of the game start.** Deterministic,
  groups an evening's slate, avoids the boundary ambiguity of a rolling 2–3h
  window. Documented as TUNABLE in a comment (`_SLATE_TZ`). Rolled back across UTC
  midnight correctly (`test_et_day_rolls_back_across_utc_midnight`: 02:00 UTC =
  22:00 ET → the previous ET day).
- **Start-time source = injected `start_time_provider: Callable[[market_ticker],
  datetime|None]`**, wired in the app to `PregameGate.leg_start_time` (via a new
  clean `RfqFilter.leg_start_time` accessor). Peek-only, hot-path safe, no network
  — the exact gate the pregame filter uses, so slate bucketing and the pregame
  gate agree on each game's start.
- **Roll-up in the checker** (`_slate_rollup`): `worst_case_loss_by_game_cc` (the
  B1/B2 game aggregate — exposure.py stays the source, no schema change there) is
  summed per slate, keyed on the EARLIEST known leg start among positions/quotes/
  candidates touching that game (earliest is conservative — can only pool a game
  into an earlier evening, never split it out).
- **UNKNOWN start ⇒ fail-closed pooled bucket.** A game with no known start (no
  provider, or every leg returns None) pools into a single `"UNKNOWN"` slate that
  is ITSELF capped, so unknown-start games hit the slate cap together instead of
  hiding. `test_unknown_start_games_pool_into_capped_unknown_bucket` (two 900k
  games pool to 1.8M > 1.6M cap) and `test_partial_unknown_pools_the_unknown_
  game_separately` (a None-start game does NOT contaminate a known slate) prove
  both directions.
- Slate roll-up includes candidate fills AND open-quote games (both fold into
  `worst_case_loss_by_game_cc` under mass acceptance) —
  `test_slate_rollup_includes_candidate_and_open_quote_games`.

## The starvation watchdog

`StarvationWatchdog` (in `limits.py`, clock-free deterministic counter): tracks
CONSECUTIVE risk-driven would-be declines; after `threshold` (config
`starvation_threshold`, default 20) with zero clean quotes issued in between, it
returns True once (fires a structured WARNING) and exposes a `starved` flag the
ops loop can read. Wired in `lifecycle._note_watchdog`, driven on the ISSUE
decision: **any** breach (enforced OR shadow) is a would-be decline (increment);
only a fully clean check is a real issue (reset). This is what lets a mis-set cap
surface in SHADOW mode even though the quote still goes out
(`test_watchdog_observes_shadow_would_be_declines`: two tiny-bankroll shadow
RFQs both quote, but the watchdog warns). Resets on a clean issue
(`test_watchdog_resets_on_a_clean_issue`); fires under enforced starvation
(`test_watchdog_fires_when_enforced_caps_starve`); `threshold >= 1` enforced.

## Fail-closed behaviour (hard rule 6 / quiet-failure defense #2)

`LimitChecker.check` gained `risk_bankroll_cc: int | None`. When it is `None`
(caller caught `StaleBalanceError` from the tracker) OR `<= 0`, NO %-cap can be
computed → a single `SKIP_BANKROLL_UNAVAILABLE` breach (the whole layer, not a
wall of zero-threshold breaches) and the layer returns. In SHADOW mode this
fail-closed is ALSO shadow (log-only) so shadow truly has zero quote impact;
flipped to enforce it blocks new quoting for real — a STRICTER backstop than a
loose multiple, so nothing runs away while the poll is dark. The utilization
backstop needs a bankroll multiple, so with no fresh bankroll the fail-closed
breach stands in for it too. Proven: `test_none_bankroll_…`, `test_zero_bankroll_
fails_closed`, `test_negative_bankroll_fails_closed`, `test_fail_closed_is_shadow_
in_phase2`, `test_fail_closed_is_enforced_when_shadow_off`.

## Config → limits (exact, no float money)

`RiskConfig` mirrors the fractional fields as decimal STRINGS ("0.08", …) — YAML
can't hold a `Fraction` and floats are banned for thresholds — parsed via
`Fraction(Decimal(s))` in the new `RiskConfig.to_risk_limits()` (the established
`FeeConfig` pattern). `test_config_parses_decimal_strings_to_exact_fractions`:
"0.08" → EXACTLY `Fraction(8,100)`, `caps_shadow_mode` defaults True. Both
`demo.yaml` and `prod.yaml` load and produce the START values.

## What is WIRED vs deferred

**WIRED (this pass):**
- `RiskLimits` + `Breach.shadow` + `check()` params + `_r2_breaches` +
  `_slate_rollup` + `StarvationWatchdog` + `HaltInputs` (`limits.py`).
- New ReasonCodes (`reasons.py`), grouped with comments.
- `RiskConfig` fractional fields + `to_risk_limits()` (`config.py`).
- `RfqFilter.leg_start_time` accessor (`filters.py`).
- `quote_app.py`: `LimitChecker` built via `to_risk_limits()`; a `BalanceTracker`
  instantiated + polled in a new `_balance_loop` (30s stale window, 10s poll);
  the tracker, `rfq_filter.leg_start_time` (slate source), and a
  `StarvationWatchdog` threaded into the lifecycle.
- `lifecycle.py`: bankroll accessor (non-raising, fail-closed → None), the R2
  inputs threaded into all THREE `check()` sites (pre-quote, maintenance, last
  look), `_partition_breaches` (shadow split + log), `_note_watchdog`.

**WIRED (orchestrator follow-up, same branch — closed two judge-flagged latents):**
- **Intraday peak-equity latch** (`balance.py`) — `BalanceTracker` now
  high-water-marks `exchange_equity` on every poll and RE-ANCHORS the peak to the
  new start-of-day equity at the SAME UTC boundary as the SOD anchor (give-back is
  measured INTRADAY, per `CAP_recommendation_2000.md`); a manual
  `set_start_of_day_equity` also resets it. New accessors `peak_equity_cc`
  (raising) + non-raising `peak_equity_cc_or_none` / `exchange_equity_cc_or_none`.
- **Give-back halts now POPULATED and ARMED** — `lifecycle._halt_inputs()` builds
  `HaltInputs` from the tracker's non-raising peak/equity and passes it to all
  three `check()` sites, so `HALT_DRAWDOWN` / `HALT_HARD_TRIP` now EVALUATE (in
  shadow: logged; enforced: escalate). The `maintenance_tick` halt loop was
  generalized from daily-loss-only to escalate ALL enforced halt-class breaches
  (daily-loss, drawdown, hard-trip) to the killswitch — so flipping to enforce
  actually arms give-back protection (closes the judge's [MEDIUM latent]). Tested
  through the real lifecycle: shadow give-back does NOT halt, enforced give-back
  halts.
- **Config range/sign validation** (`config.py`) — a `field_validator` rejects a
  cap fraction outside `(0, 1]` (catches the `"8"`→800% typo and negatives) and a
  non-decimal; the integer knobs (`absolute_notional_multiple`,
  `fill_velocity_max_fills`, `starvation_threshold`) must be `>= 1` (closes the
  judge's [LOW latent] footgun).

**Deferred (genuinely out of Phase-2 scope; noted):**
- **Fill-velocity ENFORCEMENT** — the params live in `RiskLimits` and the reason
  code exists, but rate tracking is a rolling-window committed-notional counter
  that belongs with the Phase-3 reservation service (needs a committed-fill
  stream, not the pre-trade snapshot). The values are carried so config is
  stable; the check is not yet emitted.
- **Hard-trip = soft-halt for now.** Both give-back halts escalate to
  `killswitch.halt` (stop + cancel-all). The recommendation's "hard-trip = KILL
  file, human-only clear" distinction (a stronger latch than a soft drawdown
  halt) is a killswitch-policy refinement for a later pass; the SAFETY action
  (stop quoting) already fires for both.

## Design decisions made (not pre-specified)

1. **Fail-closed FIRST, before the utilization backstop.** A `None`/`<=0`
   bankroll returns a single `SKIP_BANKROLL_UNAVAILABLE` before the backstop runs
   (a zero denominator would otherwise collapse the backstop to `3×0=0` and spam
   `SKIP_UTILIZATION_BACKSTOP`). Cleaner and correct — the whole denominator is
   UNKNOWN, so one fail-closed breach stands in.
2. **Watchdog driven on the raw (pre-partition) breach set** so shadow would-be
   declines are observed (the plan's "in shadow mode it observes the SHADOW
   decisions and warns"). A clean check resets; any breach increments.
3. **Directional cap interpretation** (documented at the check site): net
   directional exposure per game = `|delta_by_game|` (signed contracts-equiv,
   worst-side under mass acceptance) × $1/contract → cc, vs the LOSS-axis
   threshold. A full adverse resolution moves the position $1/contract, so this
   is the loss-equivalent ceiling of the directional bet.
4. **Backstop when stale = fail-closed, not a loose multiple.** The task said the
   backstop "binds even when the bankroll poll is stale"; a stale poll passes
   `None`, which fail-closes the entire %-layer — a STRICTER block than a loose
   multiple, so the intent (nothing runs away in the dark) is met more
   conservatively. Documented in the module docstring + reason-code comment.

## NEXT STEPS

- **Owner: orchestrator + adversarial judge** — reviewed (PASS) + orchestrator
  follow-up closed the peak-latch + config-validation latents; then merge.
- **Owner: eng (next pass)** — wire fill-velocity enforcement with the Phase-3
  reservation service; optionally give hard-trip a distinct KILL-file latch
  (both give-back halts already stop quoting).
- **Owner: operator** — after SHADOW logs accumulate on real tape, compare
  would-be R2 breaches vs current behaviour, then flip `risk.caps_shadow_mode:
  false` per cap-set sign-off; confirm the ET-day slate bucket (vs a rolling
  window) and the `starvation_threshold`. Re-derive cap values from pooled
  multi-week game-clustered settlement before they gate real capital (never refit
  on one window).
