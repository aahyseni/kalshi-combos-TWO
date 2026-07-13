# WC/MLB markup re-grade (2026-07-13)

Settlement-graded two-tier markup sweep + reality test on the one-week prod shadow
recording (2026-07-06 → 2026-07-12). Produced the finding in
`docs/reports/2026-07-13-wc-mlb-markup-regrade.md`. Persisted here (was job-tmp) so
the analysis outlives the session and is reproducible.

## Data provenance (sources of truth — no memory, no assumptions)

- **Our fair + features + clearing:** `data/combomaker-prod.sqlite3` (READ-ONLY,
  `mode=ro`) — the prod shadow recording. `combo_trades.yes_price_cc` = actual
  clearing; `would_quotes.fair_cc` = our recorded fair; `rfqs` = legs/n_legs.
- **Settlement outcomes:** live Kalshi public API, `GET /markets?status=settled`
  per leg series → each market's `result` (yes/no). Combo settles via parlay AND
  with early-NO short-circuit.

## Rerun (from repo root, project venv)

```
python tools/backtests/wc_mlb_regrade/01_extract_graded.py     # 73GB scan → graded_universe.csv (~5 min)
uv run python tools/backtests/wc_mlb_regrade/02_fetch_settlements.py  # Kalshi settlements → graded_settled.csv (~1 min)
uv run python tools/backtests/wc_mlb_regrade/03_grade_sweep.py       # sweep + CIs + reality test
```

Scripts write to a `TMP` constant (originally the session job-tmp); repoint it to a
scratch dir before rerunning. `01` and `02` cache, so reruns are cheap.

## KNOWN CAVEATS (read before trusting a number)

1. **Independence-stub fair.** `would_quotes.fair_cc` is the observe recorder's
   `independence_would_quote` (stub), NOT the live engine's structural/copula fair.
   For correlated combos this OVERSTATES room → the markup *magnitude* is
   indicative only. The REALITY TEST (implied-hit vs actual-hit) does NOT use our
   fair, so its verdict is robust. Re-pricing offline with the live engine
   (`pricing/engine`) on the recorded `leg_probs` is the flagged refinement (hard
   rule 8: import live pricing, never edit it).
2. **One week, 6–8 match-days.** Day-clustered bootstrap CIs are the honest
   uncertainty. This is the FIRST sample toward the pooled multi-week markup — the
   markup DECISION stays gated on ≥3–4 game-clustered weeks (never a P&L refit).
3. **Volume-optimistic** win model (assumes we capture all competitive volume);
   real fill rate is lower.
