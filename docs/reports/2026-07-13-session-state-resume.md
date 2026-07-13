# RESUME STATE — 2026-07-13 (read first)

Fresh-session chain: **this doc → `docs/reports/README.md` newest-first →
`CLAUDE.md` CURRENT STATE block**. What's done, what's armed, what's next.

## Headline

- **A maker MARKUP now exists in the pricer** (first ever) and **World-Cup-FAT
  go-live is ARMED but NOT LAUNCHED.** Launch is paused pending the operator's
  run-host decision (they asked me to run + supervise it live, showing fills +
  a rolling book).
- **The edge is validated for WC FAT** (real, day-clustered), unvalidated
  elsewhere. First live run harvests the CLOSING World Cup window (3 games left).

## What shipped this session (all on `main`, pushed)

| Commit | What |
|---|---|
| `2a141b9` | **get_quotes 500/504 fix** — `exchange/quote_query.list_open_quotes` (windowed + 5xx-retry + fail-closed); used by startup reconcile AND supervisor kill-path. NOTES hard-rule-4 row. |
| `0c4afc6` | **Maker markup mechanism** — `pricing/markup.py` MarkupPolicy + `construct_quote` `margin=max(width,markup)` (parity-proven, adversarial-reviewed clean). Reverts committed prod.yaml to DISARMED (test-enforced). |

## The edge verdict (report: `2026-07-13-wc-mlb-markup-regrade.md`)

Re-graded the one-week prod shadow (2026-07-06→12) vs REAL Kalshi settlements
(now 73%/79% resolved). Reality test (fair-independent):
- **Soccer FAT = REAL SELLER EDGE** — settles 13.8% vs priced 19.6% (+5.8pp,
  day-clustered CI5 +4.2). Retail overpays for longshot parlays.
- Soccer NORMAL adverse; MLB negative-but-confounded (stub fair) → re-price +
  more-weeks item, not broken.
- WC-FAT markup: +EV from 2.2¢; provisional **3¢** for go-live (CI5 +2.1, ~77%
  competitive, wins more of the closing window). **DECISION stays pooled-multi-
  week — never a P&L refit.**

## ARMED — how to launch (paused, awaiting operator)

Config: `config/prod-live-wc.local.yaml` (gitignored, NOT committed). WC-ONLY,
soccer 3¢, MLB off, caps enforced, isolated live DB
`data/combomaker-prod-live-wc.sqlite3` (NEVER the read-only shadow).
```
combomaker run --env prod --mode quote --confirm-live --config config/prod-live-wc.local.yaml
```
Kill: `combomaker halt` · panic: `combomaker cancel-all --env prod`. Bot
auto-launches its supervisor (needs `KALSHI_SUPERVISOR_*`, present) + runs a
5-gate prod preflight that fails closed.

## Running processes

- **NONE.** Shadow recorder is STOPPED (data ended Jul 12 23:28); operator is
  restarting it on a **new server** for weeks 2–4 (`combomaker run --env prod
  --mode observe`, durable). B harness runs are complete.

## NEXT STEPS

1. **Owner: bot.** On operator go: launch + supervise the WC-FAT run — monitor
   fills instantly + render a rolling book (open NO positions, fills w/ edge vs
   fair, realized/unrealized P&L, committed-payout vs caps, kill state). Expect
   SPARSE/zero fills (3 WC games left) — that's normal, not failure.
2. **Owner: operator.** (a) restart the shadow recorder on the new server; (b)
   confirm run host for the live bot (supervised in-session now; durable server
   for multi-day).
3. **Owner: bot (deferred).** Re-price the graded universe with the LIVE engine
   (de-confound MLB); explicit FAT/NORMAL room predictor + per-tier markup +
   toggles once weeks pool; adaptive markup behind the MarkupPolicy seam.
4. **Owner: measurement.** Pooled multi-week markup = the real gate.

## Doctrine (unchanged, load-bearing)

Never touch `kalshi-combos` w/o `-TWO`. Prod DB `combomaker-prod.sqlite3`
READ-ONLY (`mode=ro`). Secrets env-only. Verify Kalshi facts vs API/tape, never
memory. Money int centi-cents. Missing/stale ⇒ no-quote. NEVER refit on a P&L
window. Dated report per finding + README index + this resume doc, kept current.
