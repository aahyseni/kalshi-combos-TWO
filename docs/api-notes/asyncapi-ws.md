# Kalshi WebSocket API (AsyncAPI 3.0.0, info.version 2.0.0) — Full Digest for RFQ Combo Market Maker

Source: `https://docs.kalshi.com/asyncapi.yaml` (fetched 2026-07-05, 132,796 bytes, read verbatim — not summarized). Supplemented by `https://docs.kalshi.com/getting_started/quick_start_websockets` and `https://docs.kalshi.com/getting_started/order_direction` (both referenced by the spec).

---

## 1. Connection & Authentication

- **Production WS**: `wss://external-api-ws.kalshi.com/trade-api/ws/v2` (spec lists ONLY this server; protocol `wss`, "encrypted connection only"). Legacy prod still supported: `wss://api.elections.kalshi.com/trade-api/ws/v2`.
- **Demo WS** (from quick start, NOT in asyncapi.yaml): `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`. Legacy demo: `wss://demo-api.kalshi.co/trade-api/ws/v2`.
- WS handshake is `GET` on the channel address `/`. **Authentication is required to establish the connection itself**, even if you only want public channels ("Some channels carry only public market data, but the connection itself still requires authentication").
- **Handshake headers** (exact names):
  - `KALSHI-ACCESS-KEY` — your API key ID
  - `KALSHI-ACCESS-SIGNATURE` — base64 RSA-PSS(SHA256, MGF1(SHA256), digest-length salt) signature over the string `timestamp + "GET" + "/trade-api/ws/v2"` (timestamp is Unix **milliseconds**, concatenated as string, no separators)
  - `KALSHI-ACCESS-TIMESTAMP` — same Unix ms timestamp
- No documented expiry window for the timestamp (empirically Kalshi REST enforces a small skew window; verify on demo).
- securityScheme in spec: `type: apiKey, in: user` — "The API key should be provided during the WebSocket handshake."

## 2. Keep-Alive (control frames)

- Kalshi sends WebSocket **Ping frames (`0x9`) every 10 seconds with body `heartbeat`**. Client must respond with Pong frames (`0xA`, empty body).
- Client may send its own Ping (empty body); Kalshi responds with Pong (empty body).
- Python `websockets` library handles ping/pong automatically (per quick start). Reconnect with exponential backoff.

## 3. Command / Response Protocol (channel address `/`)

All messages JSON (`defaultContentType: application/json`). Client→server messages are "commands"; server replies with typed responses.

### 3.1 Common scalar schemas
- `commandId` (`id`): integer, client-generated, `minimum: 0`, should be unique within a WS session (simplest: start at 1, increment). **`id: 0` is treated as if no id was sent.**
- `subscriptionId` (`sid`): integer `minimum: 1`, **server-generated**, identifies a subscription stream.
- `sequenceNumber` (`seq`): integer `minimum: 1`, "Sequential number that should be checked if you want to guarantee you received all the messages. Used for snapshot/delta consistency."
- `marketTicker`: string, pattern `^[A-Z0-9-]+$` (examples `FED-23DEC-T3.00`, `HIGHNY-22DEC23-B53.5` — note: the regex as written does not include `.`; real tickers contain dots. Treat the pattern as advisory, not a validator).
- `marketId`: string, UUID.
- `marketSide`: enum `yes` | `no`.
- `bookSide`: enum `bid` | `ask`. `bid` ≡ outcome_side `yes`; `ask` ≡ outcome_side `no`.
- `orderAction`: enum `buy` | `sell`.

### 3.2 `subscribe` command
```json
{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": "CPI-22DEC-TN0.1"}}
```
- Required: `id`, `cmd` (const `subscribe`), `params`. `params.channels` required, array minItems 1.
- **`channels` enum (exact list)**: `orderbook_delta`, `ticker`, `trade`, `fill`, `market_positions`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `multivariate`, `communications`, `order_group_updates`, `user_orders`, `cfbenchmarks_value`.
  - NOTE: there is **no `ticker_v2` channel** in this spec — the channel is named `ticker` (its payload already carries the dollar-denominated v2-style fields).
- Optional `params` fields:
  - `market_ticker` (string, single market) XOR `market_tickers` (array of strings, minItems 1) — mutually exclusive.
  - `market_id` (UUID) / `market_ids` (array of UUIDs, minItems 1) — **`ticker` channel only**; mutually exclusive with each other and with ticker(s).
  - `send_initial_snapshot` (bool, default false) — `ticker` channel only: receive an initial ticker snapshot for requested markets.
  - `skip_ticker_ack` (bool, default false) — OK responses omit the `market_tickers`/`market_ids` lists for this subscription (useful with huge market lists).
  - `use_yes_price` (bool, default false) — **orderbook channel only**. When true, no-side `orderbook_delta`/`orderbook_snapshot` levels are reported in **yes-leg pricing** (single unified `price_dollars` scale for both sides; a no-side level `0.30` no-leg becomes `0.70` yes-leg). Default false = legacy no-leg pricing. **Migration plan: default flips to `true` in a future release, then the flag is removed and unified yes-leg pricing becomes the only behavior.** Build with `use_yes_price: true` from day one.
  - `shard_factor` (int, min 1) / `shard_key` (int, min 0) — `communications` channel fanout sharding; constraint `1 <= shard_factor <= 100`, `0 <= shard_key < shard_factor`; `shard_factor` required if `shard_key` set.
  - `index_ids` (array of strings, minItems 1) — `cfbenchmarks_value` only.
- Success response (`subscribed`), one per channel subscribed:
```json
{"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}}
```
  Payload: required `type` (const `subscribed`), `msg` {required `channel` string, `sid` int}; `id` optional (echo of command id).

### 3.3 `unsubscribe` command
```json
{"id": 124, "cmd": "unsubscribe", "params": {"sids": [1, 2]}}
```
Required: `id`, `cmd` const `unsubscribe`, `params.sids` (array of sid, minItems 1). Response per sid:
```json
{"id": 102, "sid": 2, "seq": 7, "type": "unsubscribed"}
```
(`sid`, `seq`, `type` required; `type` const `unsubscribed`.)

### 3.4 `update_subscription` command
```json
{"id": 124, "cmd": "update_subscription", "params": {"sids": [456], "market_tickers": ["NEW-MARKET-1", "NEW-MARKET-2"], "action": "add_markets"}}
```
- `cmd` const `update_subscription`; `params.action` required, enum: `add_markets` | `delete_markets` | `get_snapshot`.
- Target subscription: EITHER `sid` (single int) OR `sids` (array with **exactly one** element, minItems 1 maxItems 1) — not both (error 12 otherwise).
- Market spec: `market_ticker` / `market_tickers` / `market_id` / `market_ids` (UUID forms are `ticker` channel only).
- `send_initial_snapshot` (bool) — initial ticker snapshot for newly added markets.
- `get_snapshot` (orderbook_delta channel): returns an `orderbook_snapshot` for the requested `market_tickers` **without modifying the subscription**.
- Success response (`ok`):
```json
{"id": 123, "sid": 456, "seq": 222, "type": "ok", "msg": {"market_tickers": ["MARKET-1", "MARKET-2", "MARKET-3"]}}
```
  Only `type` is required; `msg.market_tickers`/`msg.market_ids` = **full list after update** (omitted when `skip_ticker_ack`).
- CF Benchmarks variant uses the same `cmd` with `action` enum `subscribe_indices` | `unsubscribe_indices` | `indexlist` and `index_ids` array (`["all"]` = every index). Not needed for combos.

### 3.5 `list_subscriptions` command
```json
{"id": 3, "cmd": "list_subscriptions"}
```
(no `params`). Response has `type: "ok"` and `msg` = ARRAY of `{"channel": string, "sid": int}`:
```json
{"id": 3, "type": "ok", "msg": [{"channel": "orderbook_delta", "sid": 1}, {"channel": "ticker", "sid": 2}, {"channel": "fill", "sid": 3}]}
```

### 3.6 `error` response
```json
{"id": 123, "type": "error", "msg": {"code": 6, "msg": "Already subscribed"}}
```
Payload: `type` const `error` required; `msg` required with `code` (int, 1–27) and `msg` (string) required; optional `market_id`, `market_ticker` when market-specific (e.g. code 16 includes `"market_ticker": "INVALID-MARKET"`).

**Full error code table:**
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
| 14 | Market Ticker required | Missing market spec (market_ticker or market_id) |
| 15 | Action required | Missing action in update_subscription |
| 16 | Market not found | Invalid market_ticker or market_id |
| 17 | Internal error | Server-side processing error |
| 18 | Command timeout | Server timed out while processing command |
| 19 | shard_factor must be > 0 | Invalid shard_factor |
| 20 | shard_factor is required when shard_key is set | |
| 21 | shard_key must be >= 0 and < shard_factor | |
| 22 | shard_factor must be <= 100 | |
| 23 | Match IDs required | Missing match_ids for the channel/action |
| 24 | Index IDs required | cfbenchmarks_value only |
| 25 | Subscription buffer overflow | Subscription's outbound buffer exceeded |
| 26 | Subscription market limit exceeded | Adding markets would exceed per-subscription market limit |
| 27 | Too many requests | The subscription exceeded its command rate limit |

**Terminal errors — subscription is dead, you MUST resubscribe**: codes **10** (Channel error), **17** (Internal error), **25** (Subscription buffer overflow). Code 25 means a slow consumer overflows the outbound buffer and the subscription is dropped — drain fast.

---

## 4. `communications` channel (RFQ/quote flow — THE core channel for the maker)

- Address: `communications`. **Authentication required. Market specification is IGNORED** (no per-market filtering; you get the global RFQ stream).
- Optional sharding: `shard_factor` (1–100) + `shard_key` (`0 <= key < shard_factor`) to split fanout across multiple connections.
- **Visibility rules**: `RFQCreated`/`RFQDeleted` events are **always sent** (to all subscribers). Quote events (`QuoteCreated`, `QuoteAccepted`, `QuoteExecuted`) are only sent **if you created the quote OR you created the RFQ**. So you will NOT see competitors' quotes on RFQs you didn't create.
- **All timestamps on this channel are RFC3339 date-time STRINGS** (`created_ts`, `deleted_ts`, `executed_ts`) — NOT `ts_ms` integers like market-data channels.

### 4.1 `rfq_created`
```json
{"type": "rfq_created", "sid": 15, "msg": {"id": "rfq_123", "creator_id": "", "market_ticker": "FED-23DEC-T3.00", "event_ticker": "FED-23DEC", "contracts_fp": "100.00", "target_cost_dollars": "0.35", "created_ts": "2024-12-01T10:00:00Z"}}
```
`msg` required fields: `id` (string), `creator_id` (string — "Public communications ID of the RFQ creator (anonymized). **Currently empty for rfq_created events**"), `market_ticker` (string), `created_ts` (RFC3339 string). Optional: `event_ticker` (string), `contracts_fp` (string, fixed-point contracts, 2 decimals), `target_cost_dollars` (string, dollars), plus MVE-specific:
- `mve_collection_ticker` (string, optional) — multivariate event collection ticker.
- `mve_selected_legs` (array, optional) — items: `{event_ticker: string, market_ticker: string, side: string, yes_settlement_value_dollars: string (optional, "Yes settlement value in dollars for the selected leg")}`.
Note the two sizing modes: an RFQ carries `contracts_fp` OR `target_cost_dollars` (both marked optional; matches known contracts-mode vs target-cost-mode split).

### 4.2 `rfq_deleted`
```json
{"type": "rfq_deleted", "sid": 15, "msg": {"id": "rfq_123", "creator_id": "comm_abc123", "market_ticker": "FED-23DEC-T3.00", "event_ticker": "FED-23DEC", "contracts_fp": "100.00", "target_cost_dollars": "0.35", "deleted_ts": "2024-12-01T10:05:00Z"}}
```
Required: `id`, `creator_id` (populated here, unlike rfq_created), `market_ticker`, `deleted_ts` (RFC3339). Optional: `event_ticker`, `contracts_fp`, `target_cost_dollars`.

### 4.3 `quote_created`
```json
{"type": "quote_created", "sid": 15, "msg": {"quote_id": "quote_456", "rfq_id": "rfq_123", "quote_creator_id": "comm_def456", "market_ticker": "FED-23DEC-T3.00", "event_ticker": "FED-23DEC", "yes_bid_dollars": "0.35", "no_bid_dollars": "0.65", "yes_contracts_offered_fp": "100.00", "no_contracts_offered_fp": "200.00", "rfq_target_cost_dollars": "0.35", "created_ts": "2024-12-01T10:02:00Z"}}
```
Required: `quote_id`, `rfq_id`, `quote_creator_id` (anonymized), `market_ticker`, `yes_bid_dollars` (string), `no_bid_dollars` (string), `created_ts` (RFC3339). Optional: `event_ticker`, `yes_contracts_offered_fp`, `no_contracts_offered_fp`, `rfq_target_cost_dollars`. A quote carries BOTH a yes-side bid and a no-side bid (two-sided).

### 4.4 `quote_accepted`
```json
{"type": "quote_accepted", "sid": 15, "msg": {"quote_id": "quote_456", "rfq_id": "rfq_123", "quote_creator_id": "comm_def456", "market_ticker": "FED-23DEC-T3.00", "event_ticker": "FED-23DEC", "yes_bid_dollars": "0.35", "no_bid_dollars": "0.65", "accepted_side": "yes", "contracts_accepted_fp": "50.00", "yes_contracts_offered_fp": "100.00", "no_contracts_offered_fp": "200.00", "rfq_target_cost_dollars": "0.35"}}
```
Required: `quote_id`, `rfq_id`, `quote_creator_id`, `market_ticker`, `yes_bid_dollars`, `no_bid_dollars`. Optional: `event_ticker`, `accepted_side` (enum `yes`|`no`), `contracts_accepted_fp`, `yes_contracts_offered_fp`, `no_contracts_offered_fp`, `rfq_target_cost_dollars`. NO timestamp field in this payload.

### 4.5 `quote_executed`
```json
{"type": "quote_executed", "sid": 15, "msg": {"quote_id": "quote_456", "rfq_id": "rfq_123", "quote_creator_id": "a1b2c3d4e5f6...", "rfq_creator_id": "f6e5d4c3b2a1...", "order_id": "order_789", "client_order_id": "my_client_order_123", "market_ticker": "FED-23DEC-T3.00", "executed_ts": "2024-12-01T10:05:00Z"}}
```
Required (ALL): `quote_id`, `rfq_id`, `quote_creator_id`, `rfq_creator_id`, `order_id`, `client_order_id`, `market_ticker`, `executed_ts` (RFC3339). Semantics from spec: "Sent to both the maker (quote creator) and taker (RFQ creator) when a quote is executed. **Each user receives their own order details (order_id and client_order_id).** Use this to correlate subsequent fill messages with the original quote." — i.e., the `order_id`/`client_order_id` in YOUR copy of the message is YOUR resulting order; join it against `fill.order_id`/`fill.client_order_id`.

---

## 5. `orderbook_delta` channel

- Address: `orderbook_delta`. **Auth required. Market spec REQUIRED**: `market_ticker` or `market_tickers` only — **`market_id`/`market_ids` NOT supported on this channel** (error 14/16 otherwise).
- Flow: server sends `orderbook_snapshot` first per market, then incremental `orderbook_delta` messages. Supports `update_subscription` actions `add_markets` / `delete_markets` / `get_snapshot` (get_snapshot re-sends snapshot without changing subscription — your gap-recovery tool).

### 5.1 `orderbook_snapshot`
Envelope: `type` const `orderbook_snapshot`, `sid`, `seq`, `msg` — ALL required (seq is required here).
```json
{"type": "orderbook_snapshot", "sid": 2, "seq": 2, "msg": {"market_ticker": "FED-23DEC-T3.00", "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1", "yes_dollars_fp": [["0.0800", "300.00"], ["0.2200", "333.00"]], "no_dollars_fp": [["0.5400", "20.00"], ["0.5600", "146.00"]]}}
```
`msg` required: `market_ticker`, `market_id`. `yes_dollars_fp` / `no_dollars_fp`: arrays of 2-element arrays of STRINGS `[price_in_dollars, contract_count_fp]`; **key is absent entirely if that side has no offers**.

### 5.2 `orderbook_delta`
Envelope: `type` const `orderbook_delta`, `sid`, `seq`, `msg` all required.
```json
{"type": "orderbook_delta", "sid": 2, "seq": 3, "msg": {"market_ticker": "FED-23DEC-T3.00", "market_id": "9b0f6b43-...", "price_dollars": "0.960", "delta_fp": "-54.00", "side": "yes", "ts": "2022-11-22T20:44:01Z", "ts_ms": 1669149841000}}
```
`msg` required: `market_ticker`, `market_id`, `price_dollars` (string), `delta_fp` (string, signed fixed-point contract delta, 2 decimals), `side` (`yes`|`no`). Optional: `client_order_id` (string — **present only when YOUR order caused this book change**; contains your order's client_order_id — free self-detection in the public book), `subaccount` (int, only if yours + using subaccounts), `ts` (RFC3339, DEPRECATED — use ts_ms), `ts_ms` (int64 Unix ms, optional).
- Pricing scale of no-side levels depends on `use_yes_price` subscribe flag (see 3.2). Under default (false), no-side prices are no-leg prices; under true, both sides on the yes-price scale.

---

## 6. `ticker` channel

- Address: `ticker`. No channel-level auth beyond the (already authenticated) connection. Market spec OPTIONAL — omit to receive ALL markets. Supports both ticker and UUID market specs. `send_initial_snapshot: true` gets you an immediate ticker per subscribed market. Updates sent whenever any ticker field changes.
- Envelope required: `type` const `ticker`, `sid`, `msg` — **NO `seq` on ticker messages**.
```json
{"type": "ticker", "sid": 11, "msg": {"market_ticker": "FED-23DEC-T3.00", "market_id": "9b0f6b43-...", "price_dollars": "0.480", "yes_bid_dollars": "0.450", "yes_ask_dollars": "0.530", "volume_fp": "33896.00", "open_interest_fp": "20422.00", "dollar_volume": 16948, "dollar_open_interest": 10211, "yes_bid_size_fp": "300.00", "yes_ask_size_fp": "150.00", "last_trade_size_fp": "25.00", "ts": 1669149841, "ts_ms": 1669149841000, "time": "2022-11-22T20:44:01Z"}}
```
`msg` required fields (all): `market_ticker`, `market_id`, `price_dollars` (string, last traded price), `yes_bid_dollars` (string), `yes_ask_dollars` (string), `yes_bid_size_fp` (string), `yes_ask_size_fp` (string), `last_trade_size_fp` (string), `volume_fp` (string), `open_interest_fp` (string), `dollar_volume` (INTEGER, min 0), `dollar_open_interest` (INTEGER, min 0), `ts` (int64 seconds, DEPRECATED), `ts_ms` (int64 ms — canonical), `time` (RFC3339, DEPRECATED).

---

## 7. `trade` channel

- Address: `trade`. Market spec optional (omit = all trades). Sent immediately after execution.
- Envelope: `type` const `trade`, `sid`, `msg` required — no `seq`.
```json
{"type": "trade", "sid": 11, "msg": {"trade_id": "d91bc706-...", "market_ticker": "HIGHNY-22DEC23-B53.5", "yes_price_dollars": "0.360", "no_price_dollars": "0.640", "count_fp": "136.00", "taker_side": "no", "ts": 1669149841, "ts_ms": 1669149841000}}
```
`msg` required: `trade_id` (UUID), `market_ticker`, `yes_price_dollars` (string), `no_price_dollars` (string), `count_fp` (string), `taker_side` (`yes`|`no`, **DEPRECATED** — will not be removed before May 14, 2026 per this spec; the order_direction page says May 28, 2026), `taker_outcome_side` (`yes`|`no` — canonical; buy-yes and sell-no ⇒ `yes`, buy-no and sell-yes ⇒ `no`; directional exposure only, both parties trade at the same price), `taker_book_side` (`bid`|`ask` — same bit in book vocabulary), `ts` (int seconds, DEPRECATED), `ts_ms` (int64 ms).
Note: `taker_outcome_side`, `taker_book_side` ARE in the required list even though `taker_side` is too.

---

## 8. `fill` channel (private)

- Address: `fill`. **Auth required.** Market spec optional via `market_ticker`/`market_tickers` (omit = all your fills). Supports `update_subscription` `add_markets`/`delete_markets`. Sent immediately when your orders fill.
- Envelope: `type` const `fill`, `sid`, `msg` required — no `seq`.
```json
{"type": "fill", "sid": 13, "msg": {"trade_id": "d91bc706-...", "order_id": "ee587a1c-...", "market_ticker": "HIGHNY-22DEC23-B53.5", "is_taker": true, "side": "yes", "yes_price_dollars": "0.750", "count_fp": "278.00", "action": "buy", "ts": 1671899397, "ts_ms": 1671899397000, "post_position_fp": "500.00", "purchased_side": "yes", "subaccount": 3}}
```
`msg` required: `trade_id` (UUID, unique per fill), `order_id` (UUID), `market_ticker`, `is_taker` (bool), `side` (`yes`|`no`, DEPRECATED), `yes_price_dollars` (string — price for the YES side of the fill, regardless of your direction), `count_fp` (string), `fee_cost` (string — "Exchange fee paid for this fill in fixed-point dollars"; required in schema but ABSENT from the doc example — verify), `action` (`buy`|`sell`, DEPRECATED), `outcome_side` (`yes`|`no` — canonical), `book_side` (`bid`|`ask` — canonical), `ts` (int seconds, DEPRECATED), `ts_ms` (int64 ms), `post_position_fp` (string — your net position AFTER this fill, 2 decimals), `purchased_side` (`yes`|`no`, DEPRECATED). Optional: `client_order_id` (string — join key to `quote_executed.client_order_id`), `subaccount` (int).

---

## 9. `market_positions` channel (private)

- Address: `market_positions`. **Auth required.** Market filter by `market_ticker`/`market_tickers` ONLY (`market_id`/`market_ids` NOT supported). Omit = all positions. Updated on trades, settlements, etc. All monetary values are fixed-point dollar strings (`_dollars` suffix).
- Envelope: `type` const `market_position`, `sid`, `msg` — no `seq`. (Channel name plural, message type singular.)
```json
{"type": "market_position", "sid": 14, "msg": {"user_id": "user123", "market_ticker": "FED-23DEC-T3.00", "position_fp": "100.00", "position_cost_dollars": "50.0000", "realized_pnl_dollars": "10.0000", "fees_paid_dollars": "1.0000", "position_fee_cost_dollars": "0.5000", "volume_fp": "15.00"}}
```
`msg` required: `user_id` (string), `market_ticker`, `position_fp` (string, signed net position), `position_cost_dollars` (string, 4dp), `realized_pnl_dollars` (string), `fees_paid_dollars` (string), `position_fee_cost_dollars` (string), `volume_fp` (string). Optional: `subaccount` (int).

---

## 10. `market_lifecycle_v2` channel

- Address: `market_lifecycle_v2`. No market filtering supported — you receive ALL market + event lifecycle notifications firehose-style. Carries three message types: `market_lifecycle_v2`, `event_lifecycle`, `event_fee_update`.

### 10.1 `market_lifecycle_v2` message
Envelope: `type` const `market_lifecycle_v2`, `sid`, `msg` required — no `seq`.
`msg` required: `event_type`, `market_ticker`. `event_type` enum (exact): `created`, `deactivated`, `activated`, `close_date_updated`, `determined`, `settled`, `price_level_structure_updated`, `metadata_updated`.
Conditional optional fields (presence rules verbatim):
- `open_ts` (int64 seconds) — ONLY on `created`.
- `close_ts` (int64 seconds) — ONLY on `created` OR `close_date_updated`; "Will be updated in case of early determination markets".
- `result` (string) — ONLY on `determined`.
- `determination_ts` (int64 seconds) — ONLY on `determined`.
- `settlement_value` (string, fixed-point dollars e.g. "0.5000") — ONLY on `determined`.
- `settled_ts` (int64 seconds) — ONLY on `settled`.
- `is_deactivated` (bool) — ONLY on pause/unpause; "Boolean flag to indicate if trading is paused on an open market".
- `price_level_structure` (enum `linear_cent` | `deci_cent` | `tapered_deci_cent`) — on `created` or `price_level_structure_updated`.
- `price_ranges` (array of `{start: string, end: string, step: string}` in dollars) — alongside `price_level_structure`; "Use this to determine valid order prices rather than hardcoding a tick size." Example: `[{"start": "0.0000", "end": "1.0000", "step": "0.0010"}]`.
- `strike_type` (string), `floor_strike` (number), `cap_strike` (number), `custom_strike` (object) — ONLY on `metadata_updated` ("between" uses both strikes, "greater" uses floor_strike, "less" uses cap_strike).
- `yes_sub_title` (string) — ONLY on `metadata_updated`.
- `additional_metadata` (object) — emitted on `created`; properties: `name`, `title`, `yes_sub_title`, `no_sub_title`, `rules_primary`, `rules_secondary` (strings), `can_close_early` (bool), `event_ticker` (string), `expected_expiration_ts` (int64), `strike_type` (string), `floor_strike` (number), `cap_strike` (number), `custom_strike` (object).

### 10.2 `event_lifecycle` message (event creation)
Envelope: `type` const `event_lifecycle`, `sid`, `msg`.
```json
{"type": "event_lifecycle", "sid": 5, "msg": {"event_ticker": "KXQUICKSETTLE-26JAN25H2150", "title": "What will 1+1 equal on Jan 25 at 21:50?", "subtitle": "Jan 25 at 21:50", "collateral_return_type": "MECNET", "series_ticker": "KXQUICKSETTLE"}}
```
`msg` required: `event_ticker`, `title`, `subtitle`, `collateral_return_type` (enum `MECNET` | `DIRECNET` | `''` — empty when no collateral return scheme), `series_ticker`. Optional: `strike_date` (int64 unix ts), `strike_period` (string).

### 10.3 `event_fee_update` message — delivered on `market_lifecycle_v2` channel ONLY
Envelope: `type` const `event_fee_update`, `sid`, `msg`.
`msg` required (all three): `event_ticker` (string), `fee_type_override` (nullable enum `quadratic` | `quadratic_with_maker_fees` | `flat` | null — null when override cleared), `fee_multiplier_override` (nullable number — null when cleared).
```json
{"type": "event_fee_update", "sid": 5, "msg": {"event_ticker": "KXBTCD-26MAY2018", "fee_type_override": "quadratic", "fee_multiplier_override": 1}}
```

---

## 11. `multivariate_market_lifecycle` channel (combo/MVE lifecycle)

- Address: `multivariate_market_lifecycle`. No filtering (`market_ticker` filters NOT supported). **Only emits lifecycle updates for multivariate events.** Also carries `event_lifecycle` (same schema as 10.2) for MVE event creation.
- Message `multivariate_market_lifecycle`: payload = `allOf` [`marketLifecycleV2Payload`, `{type: const multivariate_market_lifecycle}`] — i.e., IDENTICAL schema/fields to 10.1 but `type` is `multivariate_market_lifecycle`. Channel description lists its event set as: created, activated, deactivated, close_date_updated, determined, settled (the summary omits price_level_structure_updated/metadata_updated, but the schema inherits the full enum).
```json
{"type": "multivariate_market_lifecycle", "sid": 14, "msg": {"market_ticker": "KXMVE-TEST-EVENT-M1", "event_type": "created", "open_ts": 1773936000, "close_ts": 1774022400, "additional_metadata": {"name": "MVE One", "title": "Market 1", "yes_sub_title": "YES 1", "no_sub_title": "NO 1", "rules_primary": "Rule 1", "rules_secondary": "Rule 2", "can_close_early": true, "event_ticker": "KXMVE-TEST-EVENT", "expected_expiration_ts": 1774029600}}}
```
NOTE: the MVE `created` message does NOT include the leg composition — legs arrive on the RFQ (`mve_selected_legs`) or must be fetched via REST.

---

## 12. `multivariate` channel — DEPRECATED
- Address: `multivariate`. "Deprecated: this channel predates RFQs and should not be used for new integrations." Global, no filters. Message `multivariate_lookup`: `msg` required `collection_ticker`, `event_ticker`, `market_ticker`, `selected_markets` (array of `{event_ticker, market_ticker, side}` all required, side enum `yes`|`no`). Do not build on this.

---

## 13. `user_orders` channel (private)

- Address: `user_orders`. **Auth required.** Filter optional via `market_tickers` (omit = all orders). Supports `update_subscription` `add_markets`/`delete_markets`. Fires on order created/filled/canceled/updated.
- Envelope: `type` const `user_order`, `sid`, `msg` — no `seq`.
`msg` required: `order_id` (UUID), `user_id` (UUID), `ticker` (NOTE: field is named **`ticker`**, not `market_ticker`, on this channel!), `status` (enum `resting` | `canceled` | `executed`), `side` (DEPRECATED), `is_yes` (bool, DEPRECATED), `outcome_side` (`yes`|`no`), `book_side` (`bid`|`ask`), `yes_price_dollars` (string, 4 decimals), `fill_count_fp` (string, 2dp), `remaining_count_fp` (string, 2dp), `initial_count_fp` (string, 2dp), `taker_fill_cost_dollars` (string, 4dp), `maker_fill_cost_dollars` (string, 4dp), `taker_fees_dollars` (string, 4dp), `maker_fees_dollars` (string, 4dp), `client_order_id` (string), `created_time` (RFC3339, DEPRECATED), `created_ts_ms` (int64).
Optional: `order_group_id` (string), `self_trade_prevention_type` (enum `taker_at_cross` | `maker`), `last_update_time` (RFC3339, DEPRECATED), `last_updated_ts_ms` (int64), `expiration_time` (RFC3339, DEPRECATED), `expiration_ts_ms` (int64), `subaccount_number` (int, "0 for primary, 1-63 for subaccounts").

---

## 14. `order_group_updates` channel (private)

- Address: `order_group_updates`. **Auth required. Market spec ignored.** Fires on order group created/triggered/reset/deleted/limit_updated.
- Envelope: `type` const `order_group_updates`, `sid`, **`seq`**, `msg` — ALL required (this channel HAS seq).
`msg` required: `event_type` (enum `created` | `triggered` | `reset` | `deleted` | `limit_updated`), `order_group_id` (string), `ts_ms` (int64 — "Matching engine timestamp at which the event was processed"). Optional: `contracts_limit_fp` (string, 2dp — present for `created` and `limit_updated` only).
```json
{"type": "order_group_updates", "sid": 21, "seq": 7, "msg": {"event_type": "limit_updated", "order_group_id": "og_123", "contracts_limit_fp": "150.00"}}
```

---

## 15. `cfbenchmarks_value` channel (crypto index feed — NOT needed for sports combos)
Auth required; `index_ids` param (e.g. `["BRTI", "ETHUSD_RTI"]` or `["all"]`); actions `subscribe_indices`/`unsubscribe_indices`/`indexlist`; ~1 tick/second; has `seq`. `msg`: `index_id`, `received_at` (unix ms), `data` (raw upstream JSON as string), `avg_60s_data` (always: `{value: string 8dp, window_size: int, window_start_ts_ms: int, window_end_ts_exclusive: int}`), `last_60s_windowed_average_15min` (same shape; ONLY in final minute before :00/:15/:30/:45). Error 24 = missing index_ids. Skipping details — irrelevant to sports combo making (filter per project rule: sports only).

---

## 16. Sequence numbers, envelopes & data-type conventions (cross-cutting)

- Envelope shape server→client: `{"type": "<message name>", "sid": <int>, "seq": <int, only some channels>, "msg": {...}}`. `id` appears on command responses only (echoes your command id).
- **Channels WITH required `seq`**: `orderbook_snapshot`/`orderbook_delta`, `order_group_updates`, `cfbenchmarks_value`/`cfbenchmarks_value_indexlist`. Also `unsubscribed` and (optional) `ok` responses carry `seq`.
- **Channels WITHOUT `seq` in the schema**: `ticker`, `trade`, `fill`, `market_position`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `event_lifecycle`, `event_fee_update`, `multivariate_lookup`, `user_order`, and ALL five `communications` messages (`rfq_created`, `rfq_deleted`, `quote_created`, `quote_accepted`, `quote_executed`). You cannot gap-detect the RFQ stream via seq per this schema.
- `seq` semantics per spec: "Sequential number that should be checked if you want to guarantee you received all the messages. Used for snapshot/delta consistency", minimum 1. (Scope — per-sid vs per-market — is not stated; see open questions.) On seq gap for orderbook: use `update_subscription` `action: get_snapshot` or resubscribe.
- **Naming conventions**: `_fp` suffix = fixed-point contract count as STRING with 2 decimals (e.g. `"278.00"`; fractional contracts exist). `_dollars` suffix = fixed-point dollar STRING (prices often 3-4 decimals, e.g. `"0.3500"`, `"0.960"`). `dollar_volume`/`dollar_open_interest` are plain INTEGERS (whole dollars). `_ts` = unix SECONDS int (lifecycle channels); `_ts_ms`/`ts_ms` = unix MILLISECONDS int (market data); communications channel uses RFC3339 STRINGS (`created_ts`, `deleted_ts`, `executed_ts`).
- **Deprecated (do not build on)**: `ts` (seconds), `time`/`created_time`/`last_update_time`/`expiration_time` (RFC3339) → use `*_ts_ms`; `side`, `action`, `is_yes`, `purchased_side`, `taker_side` → use `outcome_side`/`book_side` (`taker_outcome_side`/`taker_book_side` on trades). Legacy direction fields "will not be removed before May 14, 2026" (asyncapi.yaml) / "May 28, 2026" (order_direction page). Channel `multivariate` + message `multivariate_lookup` deprecated entirely.
- Direction mapping (order_direction doc): buy+yes → outcome_side `yes`/book_side `bid`; sell+no → `yes`/`bid`; buy+no → `no`/`ask`; sell+yes → `no`/`ask`. outcome_side is exposure only; both parties trade at the same price `p`.

## 17. Traps & gotchas for implementers

1. **No `ticker_v2` channel exists in this spec** — subscribe to `ticker`. Subscribing to an unknown channel returns error code 8.
2. **`communications` ignores market filters** — you get every RFQ on the exchange; do sports-only filtering client-side (or shard via shard_factor/shard_key across connections).
3. **You cannot see competitors' quotes** — quote_created/accepted/executed only arrive for your own quotes or RFQs you created. RFQ create/delete is the only universally visible signal.
4. **`quote_executed` has no price/size fields** — it only carries IDs + `executed_ts`. Execution price/size must come from the `fill` channel (join on `order_id`/`client_order_id`) or from your quote's own stored prices.
5. **RFQ deletion is silent about the reason** — `rfq_deleted` doesn't say whether it expired, was canceled, or was satisfied.
6. **`user_orders` uses `ticker`, everything else uses `market_ticker`.**
7. **Orderbook channel rejects `market_id`; ticker channel is the only one accepting UUIDs.** `market_positions` is ticker-only too.
8. **Set `use_yes_price: true` now** — default no-leg pricing for the no side will be flipped and then removed.
9. **`fill.fee_cost` is in the required list but missing from the spec's own example** — handle absence defensively.
10. **`rfq_created.creator_id` is currently always empty** — you cannot identify or dedupe RFQ creators at creation time; `rfq_deleted.creator_id` IS populated.
11. **Slow consumers get killed**: error 25 (buffer overflow) is terminal per subscription; codes 10/17 also terminal → auto-resubscribe logic is mandatory. Per-subscription command rate limit (code 27) and per-subscription market cap (code 26) exist but the numeric limits are not documented.
12. **Heartbeat**: respond to Kalshi's ping (`heartbeat` body, every 10s) or the connection drops; most WS libraries do this automatically.
13. Prices/counts are STRINGS — parse with `Decimal`, never float. Fractional contracts (2dp fixed-point) are first-class.
14. `id: 0` on commands = treated as no id → you won't be able to correlate the response.

## Critical facts (must get right)
- WS URL prod: wss://external-api-ws.kalshi.com/trade-api/ws/v2 ; demo: wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2 . Connection-level auth is ALWAYS required (even for public channels) via handshake headers KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP; signature = base64(RSA-PSS-SHA256 over ms_timestamp + "GET" + "/trade-api/ws/v2")
- Channel names (subscribe enum): orderbook_delta, ticker, trade, fill, market_positions, market_lifecycle_v2, multivariate_market_lifecycle, multivariate (DEPRECATED), communications, order_group_updates, user_orders, cfbenchmarks_value. There is NO ticker_v2 channel in this spec.
- communications channel: market filters are IGNORED (global RFQ firehose); rfq_created/rfq_deleted always sent to everyone; quote_created/quote_accepted/quote_executed sent ONLY if you created the quote or the RFQ — competitors' quotes are invisible. Optional sharding via shard_factor (1-100) + shard_key (0 <= key < shard_factor).
- quote_executed carries YOUR order_id and client_order_id (each party gets their own) plus quote_id/rfq_id/market_ticker/executed_ts — but NO price or size; correlate with fill channel messages via order_id/client_order_id to learn execution details.
- rfq_created msg: required id, creator_id (currently ALWAYS empty string), market_ticker, created_ts (RFC3339 string); optional event_ticker, contracts_fp, target_cost_dollars, mve_collection_ticker, mve_selected_legs[{event_ticker, market_ticker, side, yes_settlement_value_dollars}]. Communications timestamps are RFC3339 strings, NOT ts_ms integers.
- quote_created msg: required quote_id, rfq_id, quote_creator_id, market_ticker, yes_bid_dollars, no_bid_dollars, created_ts; optional yes_contracts_offered_fp, no_contracts_offered_fp, rfq_target_cost_dollars. quote_accepted adds optional accepted_side (yes|no) and contracts_accepted_fp and has NO timestamp.
- orderbook_delta channel requires market_ticker/market_tickers (market_id NOT supported); sends orderbook_snapshot first then orderbook_delta; both carry required seq for gap detection; update_subscription actions add_markets/delete_markets/get_snapshot (get_snapshot re-sends snapshot without modifying subscription). Snapshot levels: yes_dollars_fp / no_dollars_fp = arrays of [price_dollars_string, count_fp_string]; key absent when side empty. Delta: price_dollars (string), delta_fp (signed string), side (yes|no), optional client_order_id present ONLY when your own order caused the change.
- Subscribe with use_yes_price: true on orderbook_delta — default (false) reports no-side levels in no-leg pricing; the default will flip to true and the flag will then be removed (unified yes-leg pricing).
- seq exists ONLY on orderbook_snapshot/orderbook_delta, order_group_updates, and cfbenchmarks messages; ticker, trade, fill, market_position, lifecycle, user_order, and ALL communications messages have NO seq — you cannot gap-detect the RFQ stream.
- Command protocol: {id, cmd, params}; cmd in {subscribe, unsubscribe, update_subscription, list_subscriptions}; id client-unique per session, id=0 treated as absent; responses typed subscribed/unsubscribed/ok/error; error msg = {code 1-27, msg, optional market_ticker/market_id}; terminal errors 10 (Channel error), 17 (Internal error), 25 (Subscription buffer overflow) kill the subscription and REQUIRE resubscribe; 26 = per-subscription market limit, 27 = per-subscription command rate limit.
- update_subscription requires exactly one subscription id: either params.sid (int) or params.sids (array of EXACTLY one), plus required action in {add_markets, delete_markets, get_snapshot}; ok response msg.market_tickers = FULL list after update (omitted when skip_ticker_ack was set on subscribe).
- Numeric conventions: all prices and contract counts are fixed-point STRINGS — *_dollars (e.g. "0.3500") and *_fp (2 decimals, e.g. "278.00", fractional contracts exist); dollar_volume/dollar_open_interest are plain integers; use ts_ms (unix ms) everywhere on market data — ts (seconds) and RFC3339 time fields are deprecated.
- Direction fields: canonical are outcome_side/book_side (fill, user_order) and taker_outcome_side/taker_book_side (trade); bid==yes, ask==no; buy-yes & sell-no => yes/bid, buy-no & sell-yes => no/ask; legacy side/action/is_yes/purchased_side/taker_side deprecated (removal not before May 14, 2026 per asyncapi.yaml; May 28, 2026 per order_direction page).
- fill msg required: trade_id, order_id, market_ticker, is_taker, side (dep), yes_price_dollars (always the YES-side price), count_fp, fee_cost (fixed-point dollars string), action (dep), outcome_side, book_side, ts (dep), ts_ms, post_position_fp, purchased_side (dep); optional client_order_id, subaccount. user_orders msg uses field name `ticker` (NOT market_ticker) and status enum resting|canceled|executed.
- multivariate_market_lifecycle channel: no filters, MVE-only lifecycle, same payload schema as market_lifecycle_v2 (event_type enum: created, deactivated, activated, close_date_updated, determined, settled, price_level_structure_updated, metadata_updated; conditional fields open_ts/close_ts/result/determination_ts/settlement_value/settled_ts/is_deactivated/price_level_structure/price_ranges/strikes) but type const multivariate_market_lifecycle; MVE created messages do NOT include leg composition. The old `multivariate` channel (multivariate_lookup) is deprecated — do not use.
- Kalshi sends WS Ping (0x9) with body `heartbeat` every 10 seconds; client must Pong (0xA). price_ranges ({start, end, step} dollar strings) from lifecycle events define valid order price ticks — never hardcode tick size (price_level_structure enum: linear_cent, deci_cent, tapered_deci_cent).

## Open questions (verify empirically on demo)
- seq scope: the spec says seq guarantees message continuity 'for snapshot/delta consistency' but never states whether seq is per-sid (across all markets in one subscription) or per-market. Verify on demo with a multi-market orderbook_delta subscription before writing gap detection.
- Numeric limits behind error codes 26 (per-subscription market limit) and 27 (per-subscription command rate limit) are undocumented — probe on demo (how many tickers per orderbook_delta subscription; how fast can update_subscription be called).
- KALSHI-ACCESS-TIMESTAMP skew tolerance for the WS handshake is undocumented ('no explicit expiry window'); verify how stale a signature can be, and whether the signed path must exclude query strings.
- fill.fee_cost is required in the schema but absent from the spec's own example payload; verify it actually appears on maker fills (and whether it's 0-valued vs omitted for fee-free maker executions).
- Whether quote_accepted arrives to the maker BEFORE quote_executed and what the timing gap is (accepted has no timestamp field); also whether an accepted-then-not-executed path exists (RFQ creator backing out) — determines state machine design.
- rfq_created.creator_id 'currently empty' — verify whether rfq_deleted.creator_id can be joined back to the rfq id reliably, and whether creator anonymized IDs are stable across RFQs (needed for toxic-flow profiling of repeat requesters).
- Does rfq_deleted fire when an RFQ is fully executed (in addition to explicit cancel/expiry)? The spec gives no reason field — verify the lifecycle empirically to avoid quoting dead RFQs.
- mve_selected_legs.side is typed as plain string (not the yes/no enum) and yes_settlement_value_dollars semantics (is it per-leg payout for non-binary legs?) — pull real MVE RFQs on demo/prod recording to confirm shapes.
- Whether ticker channel messages for MVE combo markets flow on `ticker` like normal markets (spec is silent on MVE coverage of orderbook/ticker/trade channels).
- Interaction of shard_factor/shard_key with quote-event visibility: confirm your own quote events still arrive if the RFQ hashes to a different shard than the one your connection subscribed to (spec doesn't say sharding applies only to rfq_created/deleted).
- The asyncapi.yaml says deprecated direction fields won't be removed before May 14, 2026, while the order_direction page says May 28, 2026 — treat earlier date as binding; confirm current behavior on demo.
- Demo server is absent from asyncapi.yaml (only production listed) — confirm demo supports all channels used (communications, multivariate_market_lifecycle) since demo often lags prod features.
