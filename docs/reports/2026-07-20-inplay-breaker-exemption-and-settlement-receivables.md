# 2026-07-20 — Sport-switch gates 1+2: in-play breaker exemption + settlement receivables

The two PREREQUISITES for the MLB/WNBA switch (handoff doc
`2026-07-20-session-state-resume.md`, remaining-work items 1–2), built and
verified the Monday after the World Cup. Without them a nightly multi-game
slate halts through every in-play window and false-kills through every
settlement cascade — the two failure classes that produced 8 hard halts and
the $430.69 false-positive KILL through the WC final weekend.

## Gate 1 — in-play watch exemption (the 45-trip class)

**Evidence** (Sunday's live logs): 45 `halt_marginal_jump` trips, every one
"marginal became unreadable" on an in-play ESPARG market (KXWCGAME TIE 0.919,
KXWCBTTS 0.013, 1H totals/spreads, KXMENWORLDCUP-AR…) — in-play books going
dark mid-game, the normal in-play shape, on markets that were LIVE (not
settled), so the 2026-07-18 settled-watch exemption could not cover them.
Nothing behind those halts was actionable: the pregame gate had already
stopped quoting the game; resting quotes die via cancel-on-invalidate;
confirms die via last-look freshness. On an MLB slate, game 1 going in-play
would halt quoting on every other pregame game, nightly.

**Fix**: a per-ticker in-play exemption for the marginal-jump/readability
watch, mirroring the settled exemption:

- `RfqFilter.leg_inplay_watch_exempt(ticker)` — True iff the leg's game has
  STARTED per the SAME start-time ladder the pregame gate stops quoting on
  (embedded-ET KXMLB starts / explicit schedule feed / expiry−offset
  estimate). **Polarity contract**: the exemption begins exactly when quoting
  ends. UNKNOWN start ⇒ False (full watch); `allow_inplay_legs` ⇒ False
  (never blind a leg the operator re-enabled quoting on).
- `QuoteLifecycle.inplay_watch_exempt` delegates to the filter; the breaker
  sampler (`_book_leg_signals`) builds `BreakerInputs.inplay_tickers`
  (disjoint from `settled_tickers` — settled wins, telemetry can name which
  exemption applied).
- `CircuitBreakers._evaluate_marginal_jumps` skips both sets and purges the
  baseline (a book that RETURNS readable re-baselines cleanly — no phantom
  jump off a pre-game baseline).

**Unchanged protections**: legs not in either set keep the exact pre-fix
fail-closed watch (load-bearing regression tests); the whole-feed
`HALT_DATA_STALE` breaker is untouched; the quote path is untouched (an
in-play leg still declines via the pregame gate).

## Gate 2 — settlement receivables (the false-KILL class)

**Evidence**: Sunday 6:05 PM ET, `halt_hard_trip` "give-back 4306900cc ≥ 3/25
bankroll" — the exchange removes settled positions from `portfolio_value`
BEFORE crediting `balance`, so during the cascade exchange equity transiently
dipped by the in-flight settlement value. Real settlement losers: $29.51.
Human-only KILL held the bot down overnight.

**Fix**: settled-but-unpaid positions count as RECEIVABLES against the
give-back measurement — never as equity:

- **Note** (`QuoteLifecycle._refresh_settlement_receivables`, maintenance
  tick): when EVERY leg of a held position carries an exchange-graded FACT
  (settled-marginal resolver cache — facts only, never a live mark), its
  predicted gross credit (`_predicted_settlement_credit_cc`, the same figure
  the to-the-cent reconcile checks) is noted in the `BalanceTracker` ledger.
  Doubt ⇒ no receivable (the shield fails closed toward halting). A LOSER
  predicts credit 0 ⇒ notes nothing.
- **Measure** (`limits.check` give-back (7)):
  `give_back = max(0, (peak − current) − pending_receivables)`. Receivables
  only ever REDUCE the measured give-back — peak/current stay raw, so the
  shield can never inflate a peak or fabricate equity, and a real loss
  cascade (losers + mark-to-market) still measures in full. Breach detail
  logs the raw/receivables decomposition.
- **Confirm** (`SettlementHandler`): booking the exchange settlement row
  stamps the receivable confirmed.
- **Drop** (`BalanceTracker.refresh`): the first successful poll whose
  request STARTED after the confirm drops it — that reading provably
  contains the credited cash, so the shield lifts in the same instant the
  cash enters the equity figure (no double-count window in either
  direction).
- **TTL backstop** (30 min, structural constant — not an operator knob): a
  never-confirmed receivable expires LOUDLY (`settlement_receivable_ttl_expired`)
  and the measurement returns to raw. A WRONG prediction cannot hide either
  way: the settlement row still reconciles to the cent or HALTs
  (`HALT_RECONCILIATION_MISMATCH` owns that).

**Replay of 7/19 under the fix**: 71 winners' facts land during the game →
receivables ≈ the cascade's in-flight credit → trough measured ≈ $0 → no
kill; the $29.51 of losers carried no receivable and would have measured as
exactly the real give-back.

## Verification

| check | result |
|---|---|
| New tests (`test_inplay_watch_exempt.py` 14, `test_settlement_receivables.py` 19) | 33/33 green |
| Full suite | **2562 passed, 0 failed** (3 integration-deselected) |
| mypy (all 7 changed modules) | clean (6 pre-existing errors in untouched pricing files, confirmed pre-existing via stash) |
| ruff (src + changed tests) | clean (1 pre-existing UP041 in untouched `exchange/ws.py`) |
| Throughput (rule: never regress) | hot path UNTOUCHED — breaker sampler runs on the 15s status loop, receivables sweep is O(positions×legs) dict lookups on the maintenance tick. Live quotes/min before/after check owed at the next relight (MLB pre-arm checklist) since the bot is deliberately down with no sports running. |

Files: `risk/breakers.py`, `risk/balance.py`, `risk/limits.py`,
`risk/settlement.py`, `rfq/filters.py`, `rfq/lifecycle.py`,
`ops/quote_app.py` + the two new test files and three updated test doubles.

## NEXT STEPS

- The MLB/WNBA switch remains gated on readiness items 3–5 (per-sport
  structural models armed, settlement-regime rho audit, leg taxonomy) — the
  two nightly-slate blockers are now BUILT.
- At first relight (sport switch): live-verify quotes/min before/after
  (throughput rule) + watch `settlement_receivable_*` and the in-play exempt
  sets through the first slate's endgame.
- Owed elsewhere: rho corpus backtest verdict (running), final campaign P&L
  statement, tape recorder restart.
- Owner: bot session; operator decision owed: none for these two builds.
