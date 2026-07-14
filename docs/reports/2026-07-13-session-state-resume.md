# RESUME STATE — 2026-07-13/14 overnight (read first)

Fresh-session chain: **this doc → `docs/reports/README.md` newest-first →
`CLAUDE.md` CURRENT STATE**. Supersedes all earlier resume headlines.

## Headline

- **Live on prod: WC + MLB, flat `fair + 2¢`, sell-only, arb-safe, 0 fills.**
- **The night's arc:** a long, deep WS/throughput debugging session. The bot went
  from dying every ~22s / quoting ~1% → holding minutes and quoting flat 2¢ at a
  higher (but still modest, ~3–6% noisy) rate. **Remaining wall:** Kalshi closes
  our socket every ~90–150s ("write-dead"), each reconnect wipes book
  subscriptions faster than we rebuild → most quotable combos still decline
  `skip_leg_stale`. This is the #1 thing between us and a high quote rate.

## What shipped tonight (all on `main`, pushed, suite 1725/0)

| Commit | Fix |
|---|---|
| `2776013` | **WS churn**: `heartbeat=10`→`None` + `receive_timeout=25s`. heartbeat=10 (unsolicited client pings, undocumented) killed the socket every ~22s. |
| `8e2b197`+`4ec432e` (earlier) / config | **Breaker retune**: `data_stale` false-halted on quiet pregame books. `breakers.max_rx_age_s` 5→45 (halt only on a DEAD feed), `filters.max_feed_age_s` 5→12→**30** (decline, stay up). No more stop-and-go halts. |
| `01566fd` (config) | **Flat maker model**: quote = `fair + 2¢`, flat. Zeroed mechanical base/per-leg/size WIDTH (it dominated the 2¢ markup → 5-leg was ~5.3¢, uncompetitive). Kept uncertainty + longshot floor. Operator directive; re-grade validated flat markup. |
| `815acc5` | **WS write-dead → force reconnect**: a write failing ("Cannot write to closing transport") while the read side is alive (pings/deltas) → receive_timeout can't catch it → 80 `live_subscribe_failed`, only 4 books. Now: catch the write error, force ONE reconnect. |
| `3bdeaa5` | **Subscription allowlist**: `_ensure_watched` ran before the filter and subscribed a book feed for EVERY leg incl. WNBA/ATP/crypto in cross-category RFQs we decline → flooded us → slow-consumer kills. Now skip watching for any out-of-allowlist-leg combo. HALVED reconnect churn (~4→2 per 5min). |

## Analyses shipped (reports in `docs/reports/`)

- **Same-game vs multi-game edge** (`2026-07-13-samegame-vs-multigame-edge-split`):
  the +5.8pp soccer FAT edge is **entirely SAME-GAME** (+10.1pp); multi-game
  exotics (what we mostly quote) have **no edge** (−1.2pp). **Same-game gate is an
  open recommendation.**
- **Padding audit** (`2026-07-13-wc-mlb-2c-restart-and-padding-audit`): HRR/corners
  "padding" is already in the FAIR (measured ρ tables) + defensive width + DO-6
  basket buffer — NOT a markup knob; did not add one.
- **Pricing-consistency proof**: identical *displayed* combos differ by hidden
  truncated legs; identical FULL leg-sets price within 0.7¢. The wild prices the
  operator saw in Kalshi's combo view are the WHOLE MARKET, not our quotes (only
  ~1–5 of ours ever rest at once).

## LIVE run + relaunch

Config: `config/prod-live-wc.local.yaml` (gitignored). WC+MLB @2¢, flat width,
freshness 30s, breaker 45s, allowlist `[KXWC,KXMLB]`, isolated DB
`data/combomaker-prod-live-wc.sqlite3`.
```
combomaker run --env prod --mode quote --confirm-live --config config/prod-live-wc.local.yaml
```
Kill `combomaker halt` · panic `combomaker cancel-all --env prod`. **Restart ritual:**
halt → confirm `quote_app_stopped` + 0 open quotes → clear `KILL data/needs_reconcile
data/heartbeat.txt data/supervisor_heartbeat.txt` → relaunch to a fresh log.
NOTE: launching with a trailing `&` detaches it from the bash task wrapper (still
runs; just no completion notification). **Operator tool:** `tools/live_viewer.py`
(read-only tail showing OUR actual `fair+2¢` bids per RFQ + decline tally).

## THE NEXT BIG ITEM — write-dead root cause (the quote-rate unlock)

Kalshi closes our socket every ~90–150s (variable, load-correlated → likely
slow-consumer / buffer overflow per their error-25 docs, or a per-connection cap).
Each reconnect wipes all books → warmup burst (`skip_ws_unhealthy`+`skip_leg_stale`)
→ books never accumulate (stuck ~4–14 distinct). Investigate + fix:
1. **Confirm the cause** — is it error 25 / a market cap (docs/api-notes/asyncapi-ws.md
   §17), or a connection policy? (No `ws_server_error` seen — Kalshi closes silently.)
2. **Reduce delta-processing latency** — move book-delta handling off the WS read
   loop so we never fall behind (slow-consumer).
3. **Lazy / on-demand leg subscription** — subscribe a leg's book only when an RFQ
   we'd quote needs it, and UNSUBSCRIBE when done (currently `_watched` only grows).
4. **Shard subscriptions** across multiple WS connections (Kalshi supports shard_factor).

## Open operator decisions

1. **Shadow recorder** — DOWN since 2026-07-12 19:28 (`combomaker-prod.sqlite3` not
   written; no `--mode observe` proc). No weeks-2-4 backtest data accumulating.
   Restart in-session (stopgap) vs durable server.
2. **Same-game gate** — target the +EV pond (multi-game has no edge). Small filter.

## Running processes (ephemeral — die with the session)

- LIVE bot (latest launch `bxlxqhvl6` → live_wc8.log; may relaunch). Monitors:
  critical-events + 5-min health, rolling book (`poll_book.py`).

## Doctrine (unchanged)

Never touch `kalshi-combos` w/o `-TWO`. Prod shadow DB `combomaker-prod.sqlite3`
READ-ONLY (`mode=ro`). Secrets env-only. Verify Kalshi facts vs API/tape/source
(tonight: read aiohttp 3.14.1 source + Kalshi WS docs directly). Money int
centi-cents. Missing/stale ⇒ decline. NEVER refit on a P&L window (markup =
pooled multi-week; flat-2¢ is an operator directive + re-grade-validated model,
tested by fills→settlement). Dated report per finding + README index + this doc.
