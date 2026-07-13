# WS churn root cause + fix — `heartbeat=10` was the disease, `receive_timeout` is the cure (2026-07-13)

Commit `2776013` (reverts the WS half of `ddfcc2b`). Full suite **1720 passed**,
60 ws/reconnect tests green.

## Symptom

First WC-FAT live run posted quotes, but the operator's Kalshi profile showed
**0 resting quotes** and **0 fills**. Logs showed a storm of `409 rfq_closed`
on `POST /communications/quotes` and repeated `HALT_DATA_STALE` trips, then a
hard halt at 22:49:41 (sustained 35s > 30s grace).

## Root cause (measured, not theorized)

The `409 rfq_closed` + `data_stale` were **downstream symptoms**. The disease was
the WebSocket dying on a fixed cadence. From `live_wc.log` (one run, 22:44→22:49):

| connect | disconnect | lifetime |
|---|---|---|
| 22:44:59 | 22:45:21 | 22s |
| 22:45:22 | 22:45:42 | 20s |
| 22:45:43 | 22:46:07 | 24s |
| 22:46:08 | 22:46:34 | 26s |
| 22:46:35 | 22:46:57 | 22s |
| … | … | ~22–29s each |

**11 reconnects in ~5 minutes**, dead-regular ~22–29s — not random network loss,
a deterministic timeout. Each disconnect was **clean** (no `ws_error` /
`ws_frame_error` logged) and invalidated every mirrored book, so:

```
socket dies (~22s) → all books invalidated → feed rx-age = None
  → breaker trips halt_data_stale ("rx-age unknown")
  → meanwhile an RFQ we were mid-quoting closes → POST returns 409 rfq_closed
  → reconnect, resubscribe (~1s) → repeat every ~22s
  → after enough back-to-back trips, sustained 35s > 30s grace → HARD HALT
```

The change that introduced it: **`ddfcc2b` set `heartbeat=10.0`** on
`ws_connect` (earlier this session, to kill a "closing-transport" storm). Before
it, `heartbeat=None` gave a **~11-minute** connection lifetime. After it, **~22
seconds**. 1:1 correlation with the change.

### Why `heartbeat=10` kills a *busy* connection

With `heartbeat` set, aiohttp sends its **own client Ping** frames to Kalshi.
Kalshi's response to *unsolicited client pings* is **explicitly undocumented** —
`docs/api-notes/asyncapi-ws.md` §2 says only "Client *may* send its own Ping;
Kalshi responds with Pong," and the open-questions list flags *"Exact disconnect
behavior when Pongs are missed (timeout undocumented)."* We bet the bot's
stability on undocumented behavior (violates hard-rule 4) and lost. The socket
closes clean (server-side CLOSE, no error frame) ~2 heartbeat cycles in.

## Fix — active dead-peer detection via `receive_timeout`, no client pings

```python
session.ws_connect(
    self._url, headers=headers,
    autoping=True,        # still Pongs Kalshi's server ping (documented, 10s)
    heartbeat=None,       # NO client pings — that was the churn
    receive_timeout=25.0, # active silence probe, keyed off the SERVER ping
)
```

`receive_timeout` is the right primitive, **verified against the installed
aiohttp 3.14.1 source** (`client_ws.py::receive()`), not memory:

- The `async_timeout.timeout(receive_timeout)` wraps *each* `reader.read()`.
- On a PING/PONG frame the loop does `continue` → **re-arms a fresh timeout**.
- So **any** frame — including Kalshi's documented 10s server ping — resets it.
- It therefore fires **only** on a genuinely silent peer (no data *and* no server
  ping for 25s) → raises `TimeoutError` → propagates out of `_read_loop` → caught
  by `_run`'s `except Exception` → **clean, logged reconnect**. (Confirmed
  `TimeoutError` is *not* an `asyncio.CancelledError` subclass, so it is not
  swallowed by the `except CancelledError: raise` clause above it.)

### Why 25s

- **> 2 × 10s** server-ping interval + margin → a dead-quiet WC market (server
  pings but no book updates) never false-trips (Kalshi pings regardless of market
  activity, so a healthy-but-quiet socket resets the timer every 10s).
- **< the breaker's 30s `halt_data_stale` grace** → on a real half-dead peer the
  socket reconnects and books re-snapshot *before* the breaker escalates to a
  hard halt. Reconnect wins the race; no more stop-and-go kills.

This is strictly better than both prior states: no client-ping interference (vs
`heartbeat=10`'s churn) **and** active half-dead-peer detection that reconnects
instead of the 30s-silence→data_stale→halt hang (vs `heartbeat=None` alone).

## What this does NOT claim

Stability is fixed *in principle* + unit-tested; **not yet proven on a fresh live
run.** The next run is the proof — watch reconnect cadence (target: minutes, not
seconds) and that quotes actually rest on the book. Still **0 fills / profitability
unproven** — that stays gated on real fills → settlements → pooled multi-week.

## NEXT STEPS

1. **Owner: bot.** Relaunch WC-FAT on prod with the fix; fresh log; fresh WS-
   stability monitor. Confirm connection lifetime is now minutes and quotes rest.
2. **Owner: bot.** If a half-dead peer ever does trip `receive_timeout`, confirm
   it reconnects cleanly (logged `ws_error` → `ws_connected`) and does *not* hard
   halt.
3. **Owner: operator.** Durable host still needed — the in-session bot dies with
   this session.
4. **Owner: measurement.** Unchanged: pooled multi-week is the profitability gate;
   never a P&L refit.
