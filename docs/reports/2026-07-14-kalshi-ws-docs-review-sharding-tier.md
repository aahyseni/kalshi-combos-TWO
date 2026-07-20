# Kalshi WebSockets API docs review + Advanced-tier evaluation — 2026-07-14

**Method:** workflow `wf_9ff8c2e3-0f3` — one agent per doc page over all 19 relevant
WS + rate-limit/account pages → synthesis → adversarial re-verify against primary
sources. Plus a dedicated FIX `rfq-messages` deep-read agent. Two load-bearing claims
were re-fetched and CONFIRMED. Trigger: last night's 04:20 supervisor kill
(event-loop wedged 15.4s), and the operator's question of whether the docs / a higher
API tier help.

## Headline

1. **NEW server-side volume lever found: `shard_factor` + `shard_key`** on the
   `communications` subscribe (1–100). One connection gets ~1/N of the firehose,
   server-side. It is a **content-agnostic hash partition, NOT a combo/series
   filter** — and the 1/N delivery semantics are **inferred from "fanout" + error
   codes, not explicitly documented → must be validated on demo before relying on it.**
2. **No server-side content filter exists** for RFQs — CONFIRMED verbatim
   ("Market specification ignored"; RFQCreated/RFQDeleted "always sent"). There is
   no way to ask Kalshi for "only combo RFQs" on WS **or FIX** (FIX is also a
   broadcast).
3. **Advanced tier does NOT fix the wedge** — CONFIRMED. WS ingestion is not
   token-metered; the tier only raises REST/FIX read/write buckets. It buys faster
   *quote placement*, which only matters after throughput is fixed.
4. **The docs explain last night's kill.** Server pings every 10s; we run aiohttp
   (manual pong bucket), so a 15.4s CPU stall can miss pongs → server can drop us.
   And error **code 25 "Subscription buffer overflow"** — which we actually logged
   (`rfq.channel_lost.25: 1`) — is the documented failure when a consumer reads too
   slowly, whose **doc-prescribed remedy is to shard**. CPU starvation, WS keepalive,
   and buffer overflow are one coupled failure, not three.

## The star finding — sharding (server-side fanout)

`communications` subscribe accepts `shard_factor` (1–100) and `shard_key`
(0 ≤ key < shard_factor). Documented only as "fanout control" / "Number of shards
for communications channel fanout" (Communications page, WebSocket Connection page,
websockets.md, Quick Start WS error codes 19–22, AsyncAPI spec).

| Property | Reality |
|---|---|
| What it does | One connection with `shard_factor=N, shard_key=k` receives ~1/N of the firehose |
| What it is NOT | Not a content filter. It's a hash over ALL RFQs (all series mixed). You still client-side drop non-combos. |
| Caveat (important) | **No doc states the 1/N partition-delivery relationship** — inferred. **Validate empirically on demo first.** |
| The design tension | Because it's content-agnostic and combos are a small fraction, one shard yields a tiny mixed slice. To see EVERY combo you must own ALL keys across N processes → that reintroduces the full combo-pricing CPU, just spread across processes. |

**So what it actually gives us:** not less total work, but the **doc-blessed mechanism
to spread the firehose across N processes/connections**, escaping the single-GIL
wall and keeping each event loop's inbound at the server so none starves. It pairs
exactly with the ProcessPool idea already in ISSUE 1. It is a scale-OUT lever, not a
volume-reduction lever.

## Q1 — throughput levers (ranked)

| Lever | Source | Impact | Conf |
|---|---|---|---|
| `shard_factor`/`shard_key` + process-per-shard | communications.md, websocket-connection.md, QS-WS codes 19–22 | Escapes single-GIL wall; drops per-process inbound to pricing budget; ends 99.7% drop + loop starvation **IF** per-process rate ≤ pricing budget | med (semantics inferred) |
| Cheaper pre-drop via `mve_collection_ticker` presence | websockets.md | Combos are flagged in-message → field-presence check instead of ticker-prefix scan. Cheaper per-message; does NOT touch 600ms pricing | high |
| Treat error code 25 as first-class (= shard) | communications.md, QS-WS | 25 = server dropped us for slow consume; we logged it last night. Doc remedy: subscribe to a smaller subset (shard) | high |
| NO content filter / delta toggle / batching / combo-only channel | communications.md, asyncapi.yaml | These do not exist. Sharding is the ONLY server-side knob; the rest is client-side compute | high |

Confirmed dead ends: multivariate/market/event **lifecycle channels carry NO RFQ
traffic** (state changes only) — cannot serve as a combo-only RFQ feed. Quote events
(QuoteCreated/Accepted/Executed) are already self-scoped (only for RFQs/quotes you're
party to) — they are not part of the firehose volume.

## Q2 — Advanced tier verdict

| | Basic | Advanced |
|---|---|---|
| Read | 200/s | 300/s |
| Write | 100/s | 300/s (3×) |

- **WS inbound is NOT token-metered.** Verbatim: "REST and FIX requests drain the
  same buckets." No primary source meters WS messages/subscribes/connections against
  tokens. So the firehose does not touch the read budget, and raising it does nothing
  to the wedge.
- Advanced only raises **write** = quote placement (the "RFQ quote flow" is a Write
  op, confirmed verbatim). Basic ~10 quote submits/s → Advanced ~30/s. Only binds
  AFTER throughput is fixed and we're filling.
- Self-serve: `POST /account/api_usage_level/upgrade` (costs 30 tokens); eligibility
  = "≥1 of last 100 Predictions orders created via API" — trivially met.
- **Verdict: harmless to bank, but not a fix. Defer until throughput is solved.**
  Also call `GET /account/limits` once to confirm real Basic bucket numbers vs memory.

## FIX RFQ path (separate deep-read)

FIX `KalshiRFQ` is **also a broadcast** — "RFQ broadcasts," no subscribe/filter/scope.
RFQ arrives as `QuoteRequest (35=R)` (ticker tag 55, combo legs tags 20180–20184);
respond with `Quote (35=S)`. FIX app messages **use the same write token buckets** as
REST — no separate allowance. Tag-value parses marginally cheaper than JSON but
**does not touch the 600ms/combo pricing wall**. Prereqs: separate FIX session (TLS
1.2+, one connection per key, port 8232); KalshiRFQ appears open (no tier gate stated,
Basic eligibility unconfirmed). **Orthogonal to the wedge — pursue later only for
lower-jitter/lossless intake + cleaner quote lifecycle, not for throughput.**

## Keep-alive (the coupling that explains the kill)

Server sends WS Ping (opcode 0x9, body "heartbeat") every 10s; clients SHOULD Pong.
No documented server idle-timeout / missed-pong threshold. BUT: the docs' "no manual
heartbeat needed" guarantee applies only to the `websockets` library — **we run
aiohttp → manual bucket → we must emit pongs ourselves, and a starved loop can't.**
A 15.4s CPU stall → missed pongs → possible server drop, AND server-side outbound
buffer overflow (code 25, which we hit). Fixing throughput also protects the socket.

## Assumptions updated

- BROKEN: "no server-side lever, must take whole firehose" → sharding exists (volume,
  not content).
- BROKEN: "the firehose pressures our read-token budget" → WS is not token-metered.
- BROKEN: "only our own supervisor can kill us during starvation" → server pong-drop +
  code-25 buffer overflow are coupled failure modes (we logged code 25).
- CONFIRMED: no RFQ content filter; communications is the whole-exchange RFQ firehose;
  Basic = 200/100; the RFQ quote flow is a Write op.

## NEXT STEPS

- **Owner: us (bot infra) — P1.** Validate `shard_factor`/`shard_key` semantics on
  DEMO: subscribe `shard_factor=100, shard_key=k`, measure inbound msg/s, confirm
  ~1/100 delivery and that combo (`mve_collection_ticker`) RFQs appear across shards.
  Everything downstream depends on this being true.
- **Owner: us — P1.** Design the throughput fix as **shard + process-per-shard +
  pure-pricer-in-ProcessPool** (escapes GIL; per-process inbound ≤ pricing budget).
  This is the real fix for the wedge and the #1 fills lever (ISSUE 1).
- **Owner: us — P1.** Confirm aiohttp heartbeat/pong behavior and add loop-health
  protection so a stalled pricer can't block the keepalive coroutine; handle code 25
  as first-class (→ shard), not just client backpressure-drop.
- **Owner: us — P2.** Switch the ~90% pre-drop to a cheap `mve_collection_ticker`
  presence check.
- **Decision owed by operator:** (a) proceed to build the shard/ProcessPool throughput
  fix now (recommended) vs bare-restart-and-defer; (b) whether to bank the free
  Basic→Advanced upgrade now (harmless) or wait until we're filling; (c) FIX intake —
  defer indefinitely unless we want lossless/low-jitter intake later.
