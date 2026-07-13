# RESUME STATE — 2026-07-13 (read first)

Fresh-session chain: **this doc → `docs/reports/README.md` newest-first →
`CLAUDE.md` CURRENT STATE block**. What's done, what's armed, what's next.

## Headline

- **WC-FAT is LIVE on prod and STABLE.** First-ever maker markup in the pricer;
  the bot quotes WC-FAT combos at a **3¢** markup and, after a **breaker
  live-hardening pass**, now RIDES THROUGH transient WS blips (data-stale
  trip→hold→recover, observed repeatedly live) instead of hard-killing the book.
- **0 fills so far** — 3 WC games over ~6 days, sparse is expected. What's proven
  live is the **plumbing** (pricing, markup, quoting, safety-that-degrades-
  gracefully), **NOT profitability** — that stays gated on real fills → settlements
  → pooled multi-week (never a P&L refit).
- **Running IN-SESSION (ephemeral):** the bot dies if this session ends; a durable
  server is needed for a multi-day run. Supervision monitors active (rolling book
  + critical events). Edge validated for WC FAT only (reality-test, one week);
  markup provisional.

## What shipped this session (all on `main`, pushed)

| Commit | What |
|---|---|
| `2a141b9` | **get_quotes 500/504 fix** — `exchange/quote_query.list_open_quotes` (windowed + 5xx-retry + fail-closed); startup reconcile AND supervisor kill-path. |
| `0c4afc6` | **Maker markup mechanism** — `pricing/markup.py` MarkupPolicy + `construct_quote` `margin=max(width,markup)` (parity-proven, reviewed). prod.yaml stays DISARMED (test-enforced); arm via gitignored `*.local.yaml`. |
| `2934fa4` | **Windows heartbeat fix** — `_atomic_write` retries `os.replace` through the supervisor's read (was false-killing the book ~14s in); fail-closed preserved. |
| `a0b178b` | **Book-tripwire per-combo** — was pairing legs ACROSS separate quotes → false whole-book kill on a phantom impossible combo; now per resting combo (declines, never kills). |
| `8e2b197` | **Breaker grace/hysteresis** — transient reasons (data-stale/latency/429/marginal-jump) HELD across a grace window + only hard-halt if SUSTAINED; structural reasons immediate. |
| `4ec432e` | **Breaker review fixes** — quote-time freshness gate (no quoting on stale data during a hold), monotonic escalation timer, flap-resistant recovery (all from the adversarial review). |

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

## LIVE — the run + how to relaunch

Config: `config/prod-live-wc.local.yaml` (gitignored, NOT committed). WC-ONLY
(`allowed_leg_series_prefixes:[KXWC]` + `collection_whitelist:[KXMVESPORTS…,
KXMVECROSSCATEGORY]`), soccer 3¢, MLB off, caps enforced, isolated live DB
`data/combomaker-prod-live-wc.sqlite3` (NEVER the read-only shadow).
```
combomaker run --env prod --mode quote --confirm-live --config config/prod-live-wc.local.yaml
```
Kill: `combomaker halt` · panic: `combomaker cancel-all --env prod`. Bot
auto-launches its supervisor (needs `KALSHI_SUPERVISOR_*`, present) + runs a
5-gate prod preflight that fails closed.

## Running processes

- **LIVE bot: RUNNING in-session** — quoting WC-FAT, preflight green, riding
  through data-stale trip→recover, 0 fills. **EPHEMERAL: dies with this session.**
  To relaunch after a clean stop: kill any `python … combomaker` procs, clear stale
  `data/heartbeat.txt` / `data/supervisor_heartbeat.txt` / `KILL` /
  `data/needs_reconcile`, then the command above. Book poller +
  critical-events monitor in `$CLAUDE_JOB_DIR/tmp/{poll_book.py,live_wc.log}`.
- **Shadow recorder: operator restarting on a NEW SERVER** for weeks 2–4
  (`combomaker run --env prod --mode observe`, durable). B harness runs complete.

## NEXT STEPS

1. **Owner: bot.** Keep supervising the LIVE book (rolling book + critical events);
   surface any fill instantly, report on any halt. Fills expected sparse (games
   over ~6 days). Durable host needed to survive past this session.
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
