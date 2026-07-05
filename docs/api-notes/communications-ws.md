# Kalshi Communications WebSocket — Implementation Notes

Sources fetched (2026-07-05): `https://docs.kalshi.com/websockets/communications.md`, `https://docs.kalshi.com/websockets.md`, `https://docs.kalshi.com/websockets/websocket-connection.md`, `https://docs.kalshi.com/websockets/connection-keep-alive.md`, `https://docs.kalshi.com/getting_started/quick_start_websockets.md`, `https://docs.kalshi.com/getting_started/rfqs.md`, `https://docs.kalshi.com/getting_started/api_keys.md`, `https://docs.kalshi.com/getting_started/rate_limits.md`, and the **authoritative machine-readable AsyncAPI 3.0 spec** `https://docs.kalshi.com/asyncapi.yaml` (4041 lines, saved at `C:\Users\aahys\AppData\Local\Temp\claude\C--Users-aahys\0b648cb7-4a0a-4fb8-9ef6-940611d491b4\scratchpad\asyncapi.yaml`).

NOTE: the URL given in the task, `https://docs.kalshi.com/websockets/websockets.md`, **404s**. The equivalent live pages are `https://docs.kalshi.com/websockets.md` (overview) and `https://docs.kalshi.com/websockets/websocket-connection.md`. All schemas below were verified directly against `asyncapi.yaml` (line numbers cited).

---

## 1. Connection endpoints

- **Production:** `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (asyncapi servers block: host `external-api-ws.kalshi.com`, pathname `/trade-api/ws/v2`, protocol `wss`, "encrypted connection only")
- **Demo:** `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` (from quick_start_websockets)
- **Legacy, still supported:** `wss://api.elections.kalshi.com/trade-api/ws/v2` (prod) and `wss://demo-api.kalshi.co/trade-api/ws/v2` (demo)
- ONE connection carries ALL channels ("This API provides multiple channels of information over a single connection"). Handshake is HTTP `GET` upgrade. All messages JSON except WS control frames.
- **TRAP:** `communications` is a **channel** subscribed on this single connection — NOT a separate endpoint. The AsyncAPI `address: communications` (asyncapi.yaml:402-403) is the channel address, not a URL path. A first WebFetch summary of communications.md incorrectly suggested `wss://external-api-ws.kalshi.com/communications`; do not connect there.
- Authentication is required to establish the connection itself, even if you only want public channels.

## 2. Authentication (WS handshake headers)

Same scheme as REST (getting_started/api_keys):

| Header (exact) | Value |
|---|---|
| `KALSHI-ACCESS-KEY` | API key ID |
| `KALSHI-ACCESS-TIMESTAMP` | Unix timestamp in **milliseconds** (integer string, e.g. `1699564800000`) |
| `KALSHI-ACCESS-SIGNATURE` | base64 signature |

- String to sign for the WS handshake: `timestamp + "GET" + "/trade-api/ws/v2"` (concatenated, no separators).
- Path is signed **without query parameters** (strip from `?` onward) — general rule for all requests.
- Algorithm: **RSA-PSS with SHA-256**, salt length = digest length (`padding.PSS.DIGEST_LENGTH` in Python `cryptography`; `crypto.constants.RSA_PSS_SALTLEN_DIGEST` in Node), MGF1(SHA256), output base64.

Python padding snippet from docs:
```python
padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)
```

## 3. Keep-alive (websockets/connection-keep-alive)

- **Kalshi sends WS Ping frames (`0x9`) every 10 seconds with body `heartbeat`.** Clients should respond with Pong frames (`0xA`).
- Clients may also send Ping frames; Kalshi responds with Pong (empty payload).
- No documented timeout / missed-pong disconnect behavior, and no documented reconnect protocol (no resume — on reconnect you re-auth and re-subscribe from scratch).
- Docs note the Python `websockets` library handles ping/pong automatically.

## 4. Command protocol (client → server)

Envelope: `{"id": <int>, "cmd": "<command>", "params": {...}}`

- `id` (`commandId`, asyncapi.yaml:2025): integer, **client-generated, unique within the WS session**; docs suggest start at 1 and increment. Echoed back in command responses so you can match request→response.
- Commands: `subscribe`, `unsubscribe`, `update_subscription`, `list_subscriptions`.

### subscribe (asyncapi.yaml:2080-2207)
Required: `id`, `cmd:"subscribe"`, `params` with required `channels` (array, minItems 1). Channel enum (exact):
`orderbook_delta`, `ticker`, `trade`, `fill`, `market_positions`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `multivariate` (**deprecated** — predates RFQs, do not use), `communications`, `order_group_updates`, `user_orders`, `cfbenchmarks_value`

Optional params (only the relevant ones for communications noted):
- `market_ticker` (string) / `market_tickers` (array) / `market_id` (uuid) / `market_ids` (array) — mutually exclusive pairs; **ignored by the communications channel**
- `shard_factor` (integer, minimum 1, **maximum 100**): "Number of shards for communications channel fanout (optional)"
- `shard_key` (integer, minimum 0, must be `< shard_factor`; **requires shard_factor**): "Shard key for communications channel fanout"
- Others (not communications): `send_initial_snapshot` (bool, ticker), `skip_ticker_ack` (bool), `use_yes_price` (bool, orderbook only, default false — default will flip to true in a future release), `index_ids` (cfbenchmarks_value)

Communications subscribe example:
```json
{"id": 1, "cmd": "subscribe", "params": {"channels": ["communications"]}}
```
With sharding (run one connection per shard_key to split RFQ fanout across processes):
```json
{"id": 1, "cmd": "subscribe", "params": {"channels": ["communications"], "shard_factor": 4, "shard_key": 0}}
```

### unsubscribe (asyncapi.yaml:2208-2230)
`{"id": <int>, "cmd": "unsubscribe", "params": {"sids": [<int>, ...]}}` — `sids` required, minItems 1.

### update_subscription (asyncapi.yaml:2231-2288)
Params: `action` required, enum `add_markets` | `delete_markets` | `get_snapshot`; `sid` (single int) OR `sids` (array of exactly one — "Either sid or sids must be provided, not both"); plus `market_ticker`/`market_tickers`/`market_id`/`market_ids`, `send_initial_snapshot`. Not useful for communications (market spec ignored) — it's for orderbook_delta/fill/ticker.

### list_subscriptions (asyncapi.yaml:2504-2514)
`{"id": <int>, "cmd": "list_subscriptions"}` — no params required.

## 5. Server responses (command acks)

- **subscribed** (asyncapi.yaml:2342-2362): `{"id": <cmd id>, "type": "subscribed", "msg": {"channel": "<name>", "sid": <int>}}` — required: `type`, `msg` (msg requires `channel`, `sid`). One `subscribed` per channel subscribed. **`sid`** (`subscriptionId`) is a server-generated integer ≥ 1 identifying the subscription; every subsequent data message carries it.
- **unsubscribed** (asyncapi.yaml:2363-2378): `{"id", "sid", "seq", "type": "unsubscribed"}` — required: `sid`, `seq`, `type`.
- **ok** (asyncapi.yaml:2379-2405): `{"id", "sid", "seq", "type": "ok", "msg": {"market_tickers": [...], "market_ids": [...]}}` — only `type` required; used for update_subscription acks (msg = full market list after update).
- **list_subscriptions response** (asyncapi.yaml:2515-2540): `{"id": <int>, "type": "ok", "msg": [{"channel": "<name>", "sid": <int>}, ...]}`.
- **error** (asyncapi.yaml:2406-2503): `{"id": <cmd id>, "type": "error", "msg": {"code": <int 1..27>, "msg": "<human readable>", "market_id"?: string, "market_ticker"?: string}}` — msg requires `code` and `msg`.

### Full error code table (exact)
| Code | Error | Description |
|---|---|---|
| 1 | Unable to process message | General processing error |
| 2 | Params required | Missing params object in command |
| 3 | Channels required | Missing channels array in subscribe |
| 4 | Subscription IDs required | Missing sids in unsubscribe |
| 5 | Unknown command | Invalid command name |
| 6 | Already subscribed | Duplicate subscription attempt |
| 7 | Unknown subscription ID | Subscription ID not found |
| 8 | Unknown channel name | Invalid channel in subscribe |
| 9 | Authentication required | Channel requires authenticated connection |
| 10 | Channel error | Channel-specific error |
| 11 | Invalid parameter | Malformed parameter value |
| 12 | Exactly one subscription ID is required | For update_subscription |
| 13 | Unsupported action | Invalid action for update_subscription |
| 14 | Market Ticker required | Missing market specification (market_ticker or market_id) |
| 15 | Action required | Missing action in update_subscription |
| 16 | Market not found | Invalid market_ticker or market_id |
| 17 | Internal error | Server-side processing error |
| 18 | Command timeout | Server timed out while processing command |
| 19 | shard_factor must be > 0 | Invalid shard_factor |
| 20 | shard_factor is required when shard_key is set | Missing shard_factor when shard_key is set |
| 21 | shard_key must be >= 0 and < shard_factor | Invalid shard_key |
| 22 | shard_factor must be <= 100 | shard_factor too large |
| 23 | Match IDs required | Missing match_ids for the channel/action |
| 24 | Index IDs required | Missing index_ids for subscribe_indices/unsubscribe_indices on cfbenchmarks_value |
| 25 | Subscription buffer overflow | The subscription's outbound buffer was exceeded |
| 26 | Subscription market limit exceeded | Adding markets would exceed the per-subscription market limit |
| 27 | Too many requests | The subscription exceeded its command rate limit |

**Terminal errors — "the user must resubscribe":** codes **10** (Channel error), **17** (Internal error), **25** (Subscription buffer overflow). Your client MUST handle an async `type:"error"` with these codes on an active sid by re-issuing `subscribe`. Code 25 means you were reading too slowly and the server dropped you — messages were lost.

### seq
`sequenceNumber` (asyncapi.yaml:2043-2048): integer ≥ 1, "Sequential number that should be checked if you want to guarantee you received all the messages. Used for snapshot/delta consistency." **TRAP: the five communications payload schemas contain NO `seq` field** (only `type`, `sid`, `msg`) — there is no documented gap-detection mechanism on the communications channel. Reconcile via REST `GET /trade-api/v2/communications/rfqs` polling if you need completeness.

## 6. Communications channel (asyncapi.yaml:402-429 + websockets/communications.md)

- Channel name: `communications`. **Authentication required. Market specification ignored** (subscription is global — you get all RFQs).
- Optional sharding for fanout control: `shard_factor` (1-100) and `shard_key` (`0 <= key < shard_factor`).
- **Visibility rules (verbatim):** "RFQ events (RFQCreated, RFQDeleted) always sent" / "Quote events (QuoteCreated, QuoteAccepted, QuoteExecuted) are only sent if you created the quote OR you created the RFQ." I.e., as a maker you see everyone's RFQs but only YOUR OWN quote lifecycle — you cannot observe competitors' quotes via WS.
- Stated use case: "Tracking RFQs you create and quotes on your RFQs, or quotes you create on others' RFQs. Use QuoteExecuted to correlate fill messages with quotes via client_order_id."
- Exactly **5 message types**, no others: `rfq_created`, `rfq_deleted`, `quote_created`, `quote_accepted`, `quote_executed`.

All five share the outer envelope `{"type": "<const>", "sid": <int>, "msg": {...}}` (all three required; no `seq`).

### 6.1 `rfq_created` (asyncapi.yaml:3671-3734)
`msg` required: `id`, `creator_id`, `market_ticker`, `created_ts`. All fields:

| Field | Type | Req | Notes |
|---|---|---|---|
| `id` | string | yes | Unique identifier for the RFQ |
| `creator_id` | string | yes | "Public communications ID of the RFQ creator (anonymized). **Currently empty for rfq_created events.**" |
| `market_ticker` | string | yes | Market ticker for the RFQ |
| `event_ticker` | string | no | |
| `contracts_fp` | string | no | Fixed-point contracts requested (2 decimals) — present in contracts-sizing mode |
| `target_cost_dollars` | string | no | Target cost in dollars — present in target-cost-sizing mode |
| `created_ts` | string (date-time, ISO 8601) | yes | |
| `mve_collection_ticker` | string | no | Multivariate event collection ticker — present on combo RFQs |
| `mve_selected_legs` | array of objects | no | Selected legs for multivariate events — present on combo RFQs |

`mve_selected_legs` item fields (all optional in schema, no enum constraints given):
- `event_ticker`: string
- `market_ticker`: string
- `side`: string (no enum in schema; elsewhere in the spec `marketSide` is enum `'yes'`/`'no'` — verify empirically)
- `yes_settlement_value_dollars`: string — "Yes settlement value in dollars for the selected leg (optional)"

Doc example (non-combo):
```json
{
  "type": "rfq_created",
  "sid": 15,
  "msg": {
    "id": "rfq_123",
    "creator_id": "",
    "market_ticker": "FED-23DEC-T3.00",
    "event_ticker": "FED-23DEC",
    "contracts_fp": "100.00",
    "target_cost_dollars": "0.35",
    "created_ts": "2024-12-01T10:00:00Z"
  }
}
```

### 6.2 `rfq_deleted` (asyncapi.yaml:3735-3776)
`msg` required: `id`, `creator_id`, `market_ticker`, `deleted_ts`. Optional: `event_ticker`, `contracts_fp`, `target_cost_dollars`. **NO `mve_collection_ticker` / `mve_selected_legs` on deletion** — correlate to the original RFQ by `id`.

Doc example shows a populated `creator_id` here ("comm_abc123") unlike rfq_created:
```json
{
  "type": "rfq_deleted",
  "sid": 15,
  "msg": {
    "id": "rfq_123",
    "creator_id": "comm_abc123",
    "market_ticker": "FED-23DEC-T3.00",
    "event_ticker": "FED-23DEC",
    "contracts_fp": "100.00",
    "target_cost_dollars": "0.35",
    "deleted_ts": "2024-12-01T10:05:00Z"
  }
}
```

### 6.3 `quote_created` (asyncapi.yaml:3777-3833)
`msg` required: `quote_id`, `rfq_id`, `quote_creator_id`, `market_ticker`, `yes_bid_dollars`, `no_bid_dollars`, `created_ts`. Optional: `event_ticker`, `yes_contracts_offered_fp`, `no_contracts_offered_fp`, `rfq_target_cost_dollars`. All prices dollar strings; contracts fixed-point strings 2 decimals.

```json
{
  "type": "quote_created",
  "sid": 15,
  "msg": {
    "quote_id": "quote_456",
    "rfq_id": "rfq_123",
    "quote_creator_id": "comm_def456",
    "market_ticker": "FED-23DEC-T3.00",
    "event_ticker": "FED-23DEC",
    "yes_bid_dollars": "0.35",
    "no_bid_dollars": "0.65",
    "yes_contracts_offered_fp": "100.00",
    "no_contracts_offered_fp": "200.00",
    "rfq_target_cost_dollars": "0.35",
    "created_ts": "2024-12-01T10:02:00Z"
  }
}
```

### 6.4 `quote_accepted` (asyncapi.yaml:3834-3894)
`msg` required: `quote_id`, `rfq_id`, `quote_creator_id`, `market_ticker`, `yes_bid_dollars`, `no_bid_dollars`. Optional: `event_ticker`, `accepted_side` (string enum `'yes'` | `'no'`), `contracts_accepted_fp`, `yes_contracts_offered_fp`, `no_contracts_offered_fp`, `rfq_target_cost_dollars`. **TRAP: there is NO timestamp field in quote_accepted** — the HVM 3-second confirm clock must be tracked from local receipt time.

```json
{
  "type": "quote_accepted",
  "sid": 15,
  "msg": {
    "quote_id": "quote_456",
    "rfq_id": "rfq_123",
    "quote_creator_id": "comm_def456",
    "market_ticker": "FED-23DEC-T3.00",
    "event_ticker": "FED-23DEC",
    "yes_bid_dollars": "0.35",
    "no_bid_dollars": "0.65",
    "accepted_side": "yes",
    "contracts_accepted_fp": "50.00",
    "yes_contracts_offered_fp": "100.00",
    "no_contracts_offered_fp": "200.00",
    "rfq_target_cost_dollars": "0.35"
  }
}
```

### 6.5 `quote_executed` (asyncapi.yaml:3895-3947)
`msg` required (ALL of): `quote_id`, `rfq_id`, `quote_creator_id`, `rfq_creator_id`, `order_id`, `client_order_id`, `market_ticker`, `executed_ts`. No optional fields. Field notes verbatim: `order_id` — "Your order ID resulting from the quote execution. Use this to match with fill messages"; `client_order_id` — "Your client order ID for the executed order. Use this to correlate with fill messages". Note there are NO price/size fields here — join back to your quote via `quote_id` and to fills via `order_id`/`client_order_id` on the `fill` channel.

```json
{
  "type": "quote_executed",
  "sid": 15,
  "msg": {
    "quote_id": "quote_456",
    "rfq_id": "rfq_123",
    "quote_creator_id": "a1b2c3d4e5f6...",
    "rfq_creator_id": "f6e5d4c3b2a1...",
    "order_id": "order_789",
    "client_order_id": "my_client_order_123",
    "market_ticker": "FED-23DEC-T3.00",
    "executed_ts": "2024-12-01T10:05:00Z"
  }
}
```

## 7. RFQ mechanics context (getting_started/rfqs.md — fetch full page before implementing REST side)

- Lifecycle: requester creates RFQ (market ticker + size + remainder handling preference) → broadcast to all makers → makers submit quotes with `yes_bid` and `no_bid` → requester accepts ONE side of a quote → **maker must confirm within the confirmation window** → after execution timeout, orders post to the public book.
- **Timing windows:** Standard markets: confirmation **30 seconds**, execution **15 seconds**. **High Volatility Markets (HVM) — combo markets are classified HVM: confirmation 3 seconds, execution 1 second.** A combo market maker has ~3s from `quote_accepted` to confirm.
- Sizing modes (choose one): `contracts_fp` (decimal contracts, 0.01 increments) OR `target_cost_dollars` (exchange derives contract count).
- Quote rules: both `yes_bid` and `no_bid` required; either may be `"0"` but not both; constraint `yes_bid + no_bid <= $1`; prices must align with the market's `price_ranges` grid; **a new quote from the same maker on the same RFQ replaces the previous one**.
- Combo RFQs carry `mve_collection_ticker` + `mve_selected_legs`.
- Common REST errors mentioned: `invalid_parameters`, `RFQ_CLOSED`, `INSUFFICIENT_BALANCE`, `409 Conflict`.
- REST endpoint doc pages (llms.txt; method+path live on each page, not fetched here): `api-reference/communications/`: `create-rfq`, `delete-rfq`, `get-rfq`, `get-rfqs`, `create-quote`, `delete-quote`, `get-quote`, `get-quotes`, `accept-quote`, `confirm-quote`, `accept-rfq-quote`, `confirm-rfq-quote`, `delete-rfq-quote`, `get-communications-id`, plus block-trade endpoints. Multivariate: `api-reference/multivariate/get-multivariate-event-collection(s)`, `create-market-in-multivariate-event-collection`, `lookup-tickers-for-market-in-multivariate-event-collection`, `api-reference/events/get-multivariate-events`.

## 8. Rate limits (getting_started/rate_limits.md)

Token buckets, separate Read and Write, most requests cost 10 tokens; authoritative per-endpoint costs via `GET /account/endpoint_costs`. Tiers (tokens/sec read | write): Basic 200|100, Advanced 300|300, Expert 600|600, Premier 1000|1000, Paragon 2000|2000, Prime 4000|4000, Prestige 6000|8000. 429 body: `{"error": "too many requests"}`; no penalty, bucket keeps refilling. Buckets hold 1s of budget (2s for Advanced+ reads and Premier+ writes → 2x burst). **No WS-specific numeric limits documented**; WS enforcement surfaces as error code 27 (subscription command rate limit) and code 26 (per-subscription market limit).

## 9. Deprecated / adjacent channels

- `multivariate` channel = "Multivariate Lookups (**Deprecated**): this channel predates RFQs and should not be used for new integrations." (asyncapi.yaml:378-401)
- `multivariate_market_lifecycle` (current): all MVE market/event lifecycle notifications; no market_ticker filters; useful for tracking combo market creation/settlement alongside communications.
- `fill` channel: your fills, market filter optional — needed to close the loop after `quote_executed` via `order_id`/`client_order_id`.
- General fixed-point convention: monetary values are fixed-point dollar strings with `_dollars` suffix; contract quantities are fixed-point strings with `_fp` suffix (2 decimals). Elsewhere in the spec, RFC3339 `*_time` fields are deprecated in favor of `*_ts_ms` (unix ms int) — but communications payloads use ISO 8601 `created_ts`/`deleted_ts`/`executed_ts` strings.

## Critical facts (must get right)
- Connect to ONE WebSocket: prod wss://external-api-ws.kalshi.com/trade-api/ws/v2, demo wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2; 'communications' is a CHANNEL on that connection, not a separate endpoint/path.
- WS handshake auth headers (exact): KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (unix milliseconds), KALSHI-ACCESS-SIGNATURE = base64(RSA-PSS-SHA256, salt=digest length) over the string timestamp + "GET" + "/trade-api/ws/v2" (path without query params).
- Subscribe with {"id": <client int, unique per session>, "cmd": "subscribe", "params": {"channels": ["communications"]}}; ack is {"id", "type": "subscribed", "msg": {"channel", "sid"}}; sid is a server integer >=1 present on every data message.
- Exactly 5 communications message types: rfq_created, rfq_deleted, quote_created, quote_accepted, quote_executed. RFQ events are broadcast to ALL subscribers; quote events arrive ONLY if you created the quote or the RFQ — you cannot see competitors' quotes.
- Combo RFQs: rfq_created.msg carries mve_collection_ticker (string) and mve_selected_legs (array of {event_ticker, market_ticker, side, yes_settlement_value_dollars} — all strings). rfq_deleted does NOT repeat these; correlate by msg.id.
- creator_id is 'Currently empty for rfq_created events' — you cannot identify or filter RFQ creators at creation time (rfq_deleted does carry it).
- All money is fixed-point dollar STRINGS (_dollars suffix, e.g. "0.35"); all contract quantities are fixed-point STRINGS with 2 decimals (_fp suffix, e.g. "100.00"); timestamps are ISO 8601 strings (created_ts/deleted_ts/executed_ts). Two RFQ sizing modes: contracts_fp XOR target_cost_dollars.
- quote_accepted has NO timestamp field and its msg carries accepted_side ('yes'|'no') + contracts_accepted_fp; combo markets are High Volatility Markets: maker confirmation window is 3 seconds (execution 1s) vs 30s/15s for standard markets — start the confirm clock from local receipt.
- quote_executed.msg (all required): quote_id, rfq_id, quote_creator_id, rfq_creator_id, order_id, client_order_id, market_ticker, executed_ts — no price/size; join to the fill channel via order_id/client_order_id.
- Communications data messages have NO seq field (envelope is only type/sid/msg) — no documented gap detection; plan REST reconciliation (GET rfqs) for completeness.
- Async error frames: {"id", "type": "error", "msg": {"code": 1-27, "msg": str}}; codes 10 (Channel error), 17 (Internal error), 25 (Subscription buffer overflow) are TERMINAL — you must resubscribe; 25 implies lost messages (client read too slowly).
- Optional fanout sharding on subscribe: shard_factor (int 1-100) + shard_key (0 <= key < shard_factor); validation errors are codes 19-22.
- Kalshi sends WS Ping control frames (0x9) with body 'heartbeat' every 10 seconds; client must reply Pong (0xA) or rely on a library that does (Python websockets does automatically).
- Quote rules: yes_bid and no_bid both required, either may be "0" but not both, yes_bid + no_bid <= $1, prices must align to the market's price_ranges grid, and a maker's new quote on the same RFQ REPLACES their previous quote.
- The 'multivariate' WS channel is deprecated (pre-RFQ lookups) — do not use; use 'communications' for RFQs and 'multivariate_market_lifecycle' for MVE market lifecycle.

## Open questions (verify empirically on demo)
- Does the communications channel actually work on demo (wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2), and is there any RFQ traffic there? The AsyncAPI servers block lists only production.
- Exact values of mve_selected_legs[].side — schema has no enum (presumably 'yes'/'no' per the global marketSide enum); also whether yes_settlement_value_dollars is always present and what it means for non-$1 payout legs.
- Is market_ticker on a combo rfq_created the already-created MVE market in the collection, or can it reference a market that doesn't exist yet (needing create-market-in-multivariate-event-collection / lookup)? Verify by capturing live combo RFQs.
- How does shard routing assign RFQs to shard_key values (hash of rfq id? market?) — and does subscribing to communications twice on one connection (different shard_keys) return error 6 'Already subscribed'?
- Whether communications messages ever include seq in practice despite the schema omitting it, and if not, the best reconciliation cadence via REST GET rfqs/quotes to detect dropped events.
- Exact disconnect behavior when Pongs are missed (timeout seconds undocumented), plus any max-connection-count or per-connection subscribe limits (none documented).
- Precision of _dollars price strings on combo markets (2 decimals vs sub-cent / centi-cent price_ranges grid) — quote prices must 'align with market price_ranges'; verify actual grid for MVE markets on demo.
- Is the 3s/1s HVM confirmation/execution window measured server-side from acceptance time (implying our effective budget is 3s minus one-way latency), and what error does a late confirm return (RFQ_CLOSED?).
- Whether quote_accepted can arrive without accepted_side/contracts_accepted_fp in practice (they are schema-optional) — a maker needs these to know which side and how much was accepted before confirming.
- Whether rfq_created.creator_id staying empty is permanent ('Currently' suggests it may change) and whether get-communications-id REST maps our own anonymized comm id so we can recognize self-created RFQs in the stream.
