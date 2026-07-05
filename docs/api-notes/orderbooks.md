# Kalshi Orderbook Docs — Digest for RFQ Market Maker (fetched 2026-07-05)

Sources fetched (all live, none 404'd):
- https://docs.kalshi.com/getting_started/orderbook_responses.md
- https://docs.kalshi.com/websockets/orderbook-updates.md
- https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks.md
- https://docs.kalshi.com/api-reference/market/get-market-orderbook.md
- Follow-ups (referenced by the above): https://docs.kalshi.com/websockets.md, https://docs.kalshi.com/websockets/websocket-connection.md, https://docs.kalshi.com/websockets/connection-keep-alive.md, https://docs.kalshi.com/getting_started/rate_limits.md, https://docs.kalshi.com/llms.txt

---

## 1. Orderbook representation (applies to REST and WS)

- The book contains **BIDS ONLY, on both sides**. "The order book shows all active bid orders for both yes and no sides of a binary market. It returns yes bids and no bids only (no asks are returned)."
- Reciprocality (exact wording): **"A YES BID at price X is equivalent to a NO ASK at price ($1.00 - X)"** and vice versa. "By showing only bids, the orderbook provides complete market information while avoiding redundancy."
- Derived asks / spread (YES side):
  - `best_yes_bid` = last element of `yes_dollars`
  - `best_yes_ask` = `1.00 - best_no_bid` (best NO bid = last element of `no_dollars`)
  - Docs' Python example:
    ```python
    from decimal import Decimal
    best_yes_bid = Decimal("0.4200")
    best_yes_ask = Decimal("1.00") - Decimal("0.5600")
    spread = best_yes_ask - best_yes_bid
    ```
- **Fixed-point strings, not numbers**: prices are dollar strings (e.g. `"0.4200"` = $0.42); contract counts are fixed-point strings with 2 decimals (e.g. `"13.00"` = 13 contracts, `"100.00"` = 100). Docs explicitly recommend `Decimal` for precision. Do NOT parse as float.
- Each price level is a 2-element array: `[price_dollars_string, count_fp_string]` (schema name `PriceLevelDollarsCountFp`, `minItems: 2, maxItems: 2`).
- **Sort order**: "Sorted by price in ascending order" — "the highest bid (best bid) is the last element" of each array.
- Only the `orderbook_fp` dollar-string format is documented as current; no legacy integer-cent orderbook format is mentioned on these pages.
- Note: the WS delta example shows a price of `"0.960"` (3 decimals) while snapshots show 4 decimals (`"0.0800"`) — do not assume a fixed number of decimal places; normalize with `Decimal`.

Example REST body (from the getting-started guide):
```json
{
  "orderbook_fp": {
    "yes_dollars": [
      ["0.0100", "200.00"],
      ["0.4200", "13.00"]
    ],
    "no_dollars": [
      ["0.0100", "100.00"],
      ["0.5600", "17.00"]
    ]
  }
}
```

---

## 2. REST: Get Market Orderbook (single)

- **Method/path**: `GET /markets/{ticker}/orderbook`
- **Base URLs**:
  - Production: `https://external-api.kalshi.com/trade-api/v2` (alternate: `https://api.elections.kalshi.com/trade-api/v2`)
  - Demo: `https://external-api.demo.kalshi.co/trade-api/v2` (alternate: `https://demo-api.kalshi.co/trade-api/v2`)
- **Path param**: `ticker` (string, required) — market ticker.
- **Query param**: `depth` (integer, optional) — Min: 0, Max: 100, Default: 0. **"0 or negative returns all levels."** (i.e., default returns the FULL book.)
- **Auth**: required headers on the API-reference page: `KALSHI-ACCESS-KEY` (API key ID), `KALSHI-ACCESS-SIGNATURE` (RSA-PSS signature), `KALSHI-ACCESS-TIMESTAMP` (request timestamp in milliseconds). (Note contradiction: the getting-started guide says "No authentication is needed for this request" — see traps below.)
- **200 response**:
  ```json
  {
    "orderbook_fp": {
      "yes_dollars": [["0.1500", "100.00"]],
      "no_dollars": [["0.1500", "100.00"]]
    }
  }
  ```
  - `orderbook_fp` (`OrderbookCountFp`, required) with `yes_dollars` and `no_dollars` arrays of `PriceLevelDollarsCountFp`.
- **Errors**: 401 Unauthorized, 404 Not Found, 500 Internal Server Error. Error body: `{"code": string, "message": string, "details": string (optional), "service": string (optional)}`.
- No `ticker` echo in the single-market response (unlike batch).
- No timestamp/seq in the REST response — you cannot tell book age from the payload itself.

---

## 3. REST: Get Multiple Market Orderbooks (batch)

- **Method/path**: `GET /markets/orderbooks`
- **Query param**: `tickers` (required) — array of strings, each `maxLength: 200`; **`minItems: 1`, `maxItems: 100`**; serialization: `style: form, explode: true` (i.e. `?tickers=A&tickers=B&...`).
- **Auth**: required — `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP` headers.
- **200 response**:
  ```json
  {
    "orderbooks": [
      {
        "ticker": "string",
        "orderbook_fp": {
          "yes_dollars": [["0.1500", "100.00"]],
          "no_dollars": [["0.1500", "100.00"]]
        }
      }
    ]
  }
  ```
  - `orderbooks`: array of `MarketOrderbookFp` objects (required); each has `ticker` (string) and `orderbook_fp` (`OrderbookCountFp`, required).
- **Errors**: 400 Bad Request, 401 Unauthorized, 500 Internal Server Error (same error body shape as above).
- No documented `depth` parameter on the batch endpoint (only the single-market endpoint documents `depth`).

---

## 4. WebSocket connection (envelope, commands, errors)

- **URLs** (from websockets.md):
  - Production: `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (alternate shared host: `wss://api.elections.kalshi.com/trade-api/ws/v2`)
  - Demo: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` (alternate: `wss://demo-api.kalshi.co/trade-api/ws/v2`)
- **Auth**: API key authentication required on the WS handshake ("include API key headers during the WebSocket handshake"; "WebSocket connections use the same API key authentication and signing path as before" — i.e. the same KALSHI-ACCESS-* header scheme as REST; the exact signed string for the WS path is not spelled out on these pages).
- **Command envelope** (client→server), three fields:
  ```json
  {"id": 1, "cmd": "subscribe|unsubscribe|list_subscriptions|update_subscription", "params": {}}
  ```
  - `id`: client-generated, "unique within a WS session", increment sequentially. **`id: 0` is treated as no ID.**
- **subscribe** example:
  ```json
  {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": "CPI-22DEC-TN0.1"}}
  ```
  - Channels list: `orderbook_delta`, `ticker`, `trade`, `fill`, `market_positions`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `multivariate`, `communications`, `order_group_updates`, `user_orders`, `cfbenchmarks_value`.
  - Market spec (mutually exclusive): `market_ticker` (string), `market_tickers` (array), `market_id` (UUID), `market_ids` (array). **BUT the orderbook_delta channel doc says `market_id`/`market_ids` are NOT supported for orderbook subscriptions — must use `market_ticker`/`market_tickers`.**
  - Optional subscribe params: `send_initial_snapshot` (boolean, default false), `skip_ticker_ack` (boolean, default false), `use_yes_price` (boolean, default false — **"Orderbook only; The default will be flipped to true in a future release"**), plus `shard_factor`/`shard_key` (communications channel only) and `index_ids` (cfbenchmarks_value only).
- **unsubscribe**: `{"id": 124, "cmd": "unsubscribe", "params": {"sids": [1, 2]}}`
- **list_subscriptions**: `{"id": 3, "cmd": "list_subscriptions"}` (no params). Response: `{"id": 3, "type": "ok", "msg": [{"channel": "orderbook_delta", "sid": 1}, {"channel": "ticker", "sid": 2}, {"channel": "fill", "sid": 3}]}`
- **update_subscription**:
  ```json
  {"id": 124, "cmd": "update_subscription", "params": {"sids": [456], "market_tickers": ["NEW-MARKET-1", "NEW-MARKET-2"], "action": "add_markets"}}
  ```
  - `action` values for orderbook: `add_markets`, `delete_markets`, `get_snapshot`. Subscription identifier: `sid` (integer) or `sids` (array containing a single sid) — mutually exclusive; error code 12 = "Exactly one subscription ID required".
  - **`get_snapshot` "returns an `orderbook_snapshot` for the requested `market_tickers` without modifying the subscription"** — this is the documented resync primitive.
- **Server responses**:
  - subscribed: `{"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}}` — `sid` is the "server-generated subscription identifier used to identify the channel", minimum value 1.
  - unsubscribed: `{"id": 102, "sid": 2, "seq": 7, "type": "unsubscribed"}`
  - ok (update confirmed): `{"id": 123, "sid": 456, "seq": 222, "type": "ok", "msg": {"market_tickers": ["MARKET-1", "MARKET-2", "MARKET-3"]}}` — note `ok`/`unsubscribed` carry `seq` too, so they occupy slots in the subscription's seq stream.
  - error: `{"id": 123, "type": "error", "msg": {"code": 6, "msg": "Already subscribed"}}`
- **Error codes 1–27**: 1 Unable to process message; 2 Params required; 3 Channels required; 4 Subscription IDs required; 5 Unknown command; 6 Already subscribed; 7 Unknown subscription ID; 8 Unknown channel name; 9 Authentication required; 10 Channel error; 11 Invalid parameter; 12 Exactly one subscription ID required; 13 Unsupported action; 14 Market Ticker required; 15 Action required; 16 Market not found; 17 Internal error; 18 Command timeout; 19 shard_factor must be > 0; 20 shard_factor required when shard_key set; 21 shard_key invalid range; 22 shard_factor must be ≤ 100; 23 Match IDs required; 24 Index IDs required; **25 Subscription buffer overflow; 26 Subscription market limit exceeded; 27 Too many requests**. (Numeric values for 25/26/27 limits are not documented.)

---

## 5. WS `orderbook_delta` channel (snapshots + deltas)

- Channel name for subscribe: `orderbook_delta`. Requires auth. Must use `market_ticker` or `market_tickers` (NOT `market_id`/`market_ids`).
- Flow: server "Sends `orderbook_snapshot` first, followed by incremental `orderbook_delta` updates."
- **orderbook_snapshot** message (exact example from docs):
  ```json
  {
    "type": "orderbook_snapshot",
    "sid": 2,
    "seq": 2,
    "msg": {
      "market_ticker": "FED-23DEC-T3.00",
      "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
      "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]],
      "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]]
    }
  }
  ```
  - Fields: `type` = "orderbook_snapshot"; `sid` (server subscription id); `seq` (sequential number); `msg.market_ticker` (string); `msg.market_id` (UUID); `msg.yes_dollars_fp` / `msg.no_dollars_fp` — **OPTIONAL** arrays of `[price_dollars, contract_count_fp]` (optional = may be absent when a side is empty; treat absent as empty book side).
- **orderbook_delta** message (exact example from docs):
  ```json
  {
    "type": "orderbook_delta",
    "sid": 2,
    "seq": 3,
    "msg": {
      "market_ticker": "FED-23DEC-T3.00",
      "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
      "price_dollars": "0.960",
      "delta_fp": "-54.00",
      "side": "yes",
      "ts": "2022-11-22T20:44:01Z",
      "ts_ms": 1669149841000
    }
  }
  ```
  - Fields: `type` = "orderbook_delta"; `sid`; `seq`; `msg.market_ticker`; `msg.market_id`; `msg.price_dollars` ("Price level in dollars", string); `msg.delta_fp` (fixed-point contract delta, 2 decimals, string, **can be negative**); `msg.side` — enum `"yes"` | `"no"`; `msg.client_order_id` — optional, present only for YOUR OWN orders; `msg.subaccount` — optional subaccount number; `msg.ts` — **DEPRECATED** ("Optional timestamp for when the orderbook change was recorded (RFC3339). Use ts_ms instead."); `msg.ts_ms` — Unix timestamp in **milliseconds**.
  - Apply as: `new_count = old_count + Decimal(delta_fp)` at `price_dollars` on `side`; a level whose count reaches 0 should be removed (zero-removal is standard but not stated explicitly in the doc — verify).
- **seq / gap detection**: exact doc wording — `seq` is a "Sequential number that should be checked if you want to guarantee you received all the messages. Used for snapshot/delta consistency." Scope is per-subscription (per `sid`). The docs give **no explicit recovery procedure for a gap**; the documented tools for resync are (a) `update_subscription` with `action: "get_snapshot"` (returns fresh `orderbook_snapshot`s without changing the subscription) or (b) unsubscribe + resubscribe (new snapshot is sent first on subscribe).
- **staleness detection**: `ts_ms` on each delta is the only per-message book timestamp; combine with keep-alive pings (below) and seq continuity to detect a stale/dead feed. REST responses carry no timestamp, so REST alone cannot prove freshness.

---

## 6. Keep-alive (connection-keep-alive.md)

- Kalshi sends WebSocket **Ping frames (`0x9`) every 10 seconds with body `heartbeat`**. Clients "should respond with Pong frames (`0xA`)" (empty payload).
- Clients may also send Ping frames (empty payload); Kalshi responds with Pong.
- NOT documented: disconnect timeout for missed pongs, staleness thresholds, reconnection/backoff rules. Practical rule: if no server ping for well over 10s (e.g. 30s), assume the connection is dead and reconnect + resubscribe (fresh snapshot arrives on resubscribe).

---

## 7. Rate limits (getting_started/rate_limits.md) — governs REST polling cadence

- Per-second token buckets, tiers: Basic 200 read / 100 write; Advanced 300/300; Expert 600/600; Premier 1,000/1,000; Paragon 2,000/2,000; Prime 4,000/4,000; Prestige 6,000 read / 8,000 write.
- **Most requests cost 10 tokens**; `GET /account/endpoint_costs` is "the authoritative list of non-default costs currently in effect". **Batch operations charge per-item** (e.g. a 25-order batch = 250 tokens) — assume the 100-ticker batch orderbook GET may be charged per ticker until verified via endpoint_costs.
- GET endpoints are Reads; order placement/amends/cancels/order groups/**RFQ flows**/block-trade accepts are Writes. REST and FIX drain the same buckets.
- 429 returns body `{"error": "too many requests"}`; **no `Retry-After` or `X-RateLimit-*` headers** — use client-side exponential backoff. No penalty/cooldown beyond token refill.
- Burst: Advanced+ Read buckets hold two seconds of budget (burst to 2x per-second limit); Basic-tier Write buckets hold one second only.
- Basic = default on signup; Advanced requires calling the upgrade endpoint; Expert–Prestige earned by volume or assigned, granted for 30 days, renewable.

---

## 8. Traps / contradictions found

1. **REST vs WS field-name mismatch**: REST book sides are `orderbook_fp.yes_dollars` / `no_dollars`; WS snapshot sides are `msg.yes_dollars_fp` / `msg.no_dollars_fp`. Same shape, different names — don't share a struct blindly.
2. **Auth contradiction on GET orderbook**: getting-started guide says "No authentication is needed for this request"; the API-reference page lists the three KALSHI-ACCESS-* headers as required. The batch endpoint is unambiguously authenticated. Safest: always sign.
3. **Bids-only book**: there are no asks anywhere; every "ask" must be derived as `1.00 - opposite side best bid`. Best bid is the LAST element (ascending sort), not the first.
4. **All quantities/prices are strings** (fixed-point). `delta_fp` can be negative and can plausibly be fractional (2-decimal fixed point); use `Decimal`.
5. **`use_yes_price` default flip**: subscribe param `use_yes_price` (orderbook only) is documented default `false`, with the warning "The default will be flipped to true in a future release." Pin it explicitly in the subscribe params so a server-side default flip can't silently change delta price semantics.
6. **`market_id` not usable for orderbook subscriptions** even though the generic subscribe schema lists it.
7. **`ok`/`unsubscribed` control messages carry `seq`** on the subscription stream (examples: seq 222, seq 7) — a seq-continuity checker that only counts snapshot/delta messages may see false gaps around update_subscription calls.
8. **`msg.ts` is deprecated** on deltas — use `ts_ms` (ms epoch).
9. **Delta price decimals vary** in doc examples ("0.960" vs "0.0800") — normalize, don't string-compare price levels; compare as Decimal.
10. **Snapshot side arrays are optional** — absent array = empty side, not an error.
11. **429s carry no rate-limit headers**; batch calls are charged per item, so a 100-ticker batch orderbook poll may consume a large chunk of a Basic tier's 200 read tokens/sec.

## Critical facts (must get right)
- Orderbook contains BIDS ONLY on both sides; asks must be derived via reciprocality: a YES bid at X == a NO ask at (1.00 - X). best_yes_ask = 1.00 - best_no_bid.
- Price levels are ascending-sorted 2-element string arrays [price_dollars, count_fp]; the BEST bid is the LAST element of each array; all values are fixed-point strings (counts have 2 decimals) — parse with Decimal, never float.
- REST single: GET /markets/{ticker}/orderbook with optional query depth (int, 0-100, default 0; 0/negative = ALL levels). Response root: {"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}.
- REST batch: GET /markets/orderbooks?tickers=A&tickers=B (form/explode serialization), 1-100 tickers, each ticker maxLength 200; response {"orderbooks": [{"ticker", "orderbook_fp"}]}; auth required (KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP in ms).
- WS URL prod: wss://external-api-ws.kalshi.com/trade-api/ws/v2 (alt wss://api.elections.kalshi.com/trade-api/ws/v2); demo: wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2 (alt wss://demo-api.kalshi.co/trade-api/ws/v2); API-key headers required on the handshake.
- WS channel is named orderbook_delta; subscribe requires market_ticker or market_tickers (market_id/market_ids NOT supported for this channel); server sends orderbook_snapshot first, then incremental orderbook_delta messages.
- WS snapshot msg fields: type=orderbook_snapshot, sid, seq, msg.market_ticker, msg.market_id, msg.yes_dollars_fp / msg.no_dollars_fp (OPTIONAL arrays of [price_dollars, contract_count_fp]) — note the _fp-suffixed names differ from REST's yes_dollars/no_dollars.
- WS delta msg fields: type=orderbook_delta, sid, seq, msg.price_dollars (string), msg.delta_fp (signed 2-decimal fixed-point string, add to existing level count), msg.side ('yes'|'no'), msg.ts_ms (Unix ms; msg.ts RFC3339 is DEPRECATED), optional msg.client_order_id (your own orders only) and msg.subaccount.
- seq is per-subscription (per sid) and 'should be checked if you want to guarantee you received all the messages'; on a gap the documented resync options are update_subscription {action:'get_snapshot'} (returns fresh orderbook_snapshot without modifying the subscription) or unsubscribe+resubscribe; control acks (ok/unsubscribed) also carry seq on the stream.
- Explicitly set use_yes_price in orderbook subscribe params (currently default false): docs warn 'The default will be flipped to true in a future release' — a silent semantic change to delta prices if left unpinned.
- Keep-alive: Kalshi sends WS Ping frames (0x9, body 'heartbeat') every 10 seconds; client must reply with Pong (0xA); no server ping for ~3 intervals means the feed is dead — reconnect and resubscribe (fresh snapshot arrives).
- Rate limits: token buckets per second (Basic 200 read/100 write ... Prestige 6000/8000); most requests cost 10 tokens; batch operations charge PER ITEM; 429 body {"error":"too many requests"} with NO Retry-After/X-RateLimit headers; GET /account/endpoint_costs is authoritative for non-default costs.
- WS command envelope: {id, cmd, params}; id unique per session, id 0 = no id; commands: subscribe, unsubscribe (params.sids), list_subscriptions, update_subscription (sid/sids + action add_markets|delete_markets|get_snapshot); error envelope {type:'error', msg:{code, msg}} with codes 1-27 (25 buffer overflow, 26 subscription market limit exceeded, 27 too many requests).

## Open questions (verify empirically on demo)
- Exact gap-recovery contract: after update_subscription get_snapshot, does the returned orderbook_snapshot carry the next seq in the same stream (letting you resume deltas seamlessly), and are deltas suppressed/buffered while the snapshot generates? Docs are silent — verify on demo by forcing a gap.
- use_yes_price semantics: docs never define what it changes (presumably delta/snapshot prices for the 'no' side expressed in YES terms when true). Verify on demo with both values before the default flips to true.
- Is seq strictly contiguous (+1 per message) per sid across ALL message types including ok/unsubscribed acks, or only across snapshot/delta messages? The ok example (seq 222) suggests control messages consume seq slots — verify to avoid false gap alarms.
- Whether GET /markets/{ticker}/orderbook truly works unauthenticated (getting-started says no auth needed; API reference lists the three KALSHI-ACCESS-* headers as required).
- Token cost of GET /markets/orderbooks: charged per ticker (per-item batch rule) or flat 10 tokens? Check GET /account/endpoint_costs on demo — determines feasible REST polling cadence for ~100 combo markets.
- Zero-level removal: docs never state that a level whose count reaches 0 via delta_fp should be deleted, nor whether the server can send a delta creating a negative count (which would indicate a missed message). Verify empirically.
- Numeric values of WS limits behind error codes 25 (subscription buffer overflow), 26 (subscription market limit exceeded), and 27 (too many requests) — max markets per orderbook_delta subscription is undocumented.
- When subscribing to multiple markets in one subscription, snapshots appear to arrive as one orderbook_snapshot message per market on the same sid — not explicitly confirmed in docs.
- Exact WS handshake signing string (which method+path is signed for /trade-api/ws/v2) — docs only say it uses 'the same API key authentication and signing path as before'.
- Whether fractional contract counts ever occur in practice (count_fp/delta_fp have 2 decimals) or counts are always whole contracts.
- Whether send_initial_snapshot=false suppresses the initial orderbook_snapshot on the orderbook_delta channel (the channel doc says snapshot is always sent first; the generic subscribe schema lists send_initial_snapshot default false — contradiction to resolve on demo).
- Does the demo environment mirror prod WS behavior for seq/sid and the 10s heartbeat interval?
