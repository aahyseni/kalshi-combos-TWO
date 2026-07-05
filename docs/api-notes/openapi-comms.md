# Kalshi OpenAPI digest — RFQ/Quotes, MVE collections, Fills, Market price grid, V2 orders

Source: `https://docs.kalshi.com/openapi.yaml` (spec `openapi: 3.0.0`, `info.version: 3.23.0`, fetched 2026-07-05). Supplemented by `getting_started/rfqs.md`, `getting_started/rate_limits.md`, `websockets/communications.md` (all docs.kalshi.com). Raw spec saved locally at `C:\Users\aahys\AppData\Local\Temp\claude\C--Users-aahys\0b648cb7-4a0a-4fb8-9ef6-940611d491b4\scratchpad\kalshi-openapi.yaml`.

## 0. Servers and auth

Servers (all paths below are relative to these; base path already includes `/trade-api/v2`):
- Prod: `https://external-api.kalshi.com/trade-api/v2` (Production Trade API server)
- Prod (also supported): `https://api.elections.kalshi.com/trade-api/v2`
- Demo: `https://external-api.demo.kalshi.co/trade-api/v2`
- Demo (also supported): `https://demo-api.kalshi.co/trade-api/v2`
- WebSocket (prod, from websockets doc): `wss://external-api-ws.kalshi.com`

Auth (securitySchemes, all three headers together on every authed endpoint):
- `KALSHI-ACCESS-KEY` — "Your API key ID"
- `KALSHI-ACCESS-SIGNATURE` — "RSA-PSS signature of the request"
- `KALSHI-ACCESS-TIMESTAMP` — "Request timestamp in milliseconds"

API key scopes (`ApiKeyScope` enum): `read`, `write`, `read::block_trade_accept`, `read::portfolio_balance`, `write::trade`, `write::transfer`, `write::block_trade_accept`. Parent scopes grant broad access; child scopes can be granted without the parent.

`ErrorResponse` (all 4xx/5xx bodies): `{code: string, message: string, details: string, service: string}`.

Market-data GETs (`/markets`, `/markets/{ticker}`, `/multivariate_event_collections*` GETs, `/events/multivariate`, `/account/endpoint_costs`) have **no** security block (public). All `/communications/*`, `/portfolio/*` require the 3 headers.

## 1. Number formats (used EVERYWHERE below)

- `FixedPointDollars`: **string**. "US dollar amount as a fixed-point decimal string with up to 6 decimal places of precision. This is the maximum supported precision; valid quote intervals for a given market are constrained by that market's price level structure." Example: `"0.5600"`.
- `FixedPointCount`: **string**. "Fixed-point contract count string (2 decimals, e.g., \"10.00\"; referred to as \"fp\" in field names). Requests accept 0-2 decimal places (e.g., \"10\", \"10.0\", \"10.00\"); responses always emit 2 decimals. Fractional contract values (e.g., \"2.50\") are supported; the minimum granularity is 0.01 contracts."
- `BookSide`: string enum `['bid','ask']`. "For event markets, this refers to the YES leg only: `bid` means buy YES, `ask` means sell YES. (Selling YES is economically equivalent to buying NO at `1 - price`, but this endpoint quotes everything from the YES side.)"
- `SelfTradePreventionType`: enum `['taker_at_cross','maker']`. `taker_at_cross` cancels the taker order when it would trade against your own order (partials already matched execute); `maker` cancels your resting maker order and continues matching.
- `UserFilter`: enum `['self']`. Omit/empty = all; `self` = authenticated user only.
- `ExchangeIndex`: integer, "Identifier for an exchange shard. Defaults to 0 if unspecified. Note: currently only 0 supported." (`-1` = auto-route by market ticker on V2 cancel/create.)
- **There are no integer-cent price fields in any of these endpoints** (only deprecated `target_cost_centi_cents` on CreateRFQ and block-trade proposals still use `price_centi_cents`/`centicount` int64).

## 2. /communications/* — RFQ + Quote endpoints (all require auth)

### GET /communications/id — operationId `GetCommunicationsID`
Response 200 `GetCommunicationsIDResponse`: `{communications_id: string}` (required) — "A public communications ID which is used to identify the user". Use it to recognize your own `creator_id` in WS messages / Quote objects. Errors: 401, 500.

### GET /communications/rfqs — `GetRFQs`
Query params: `cursor` (string), `event_ticker` (string, single), `market_ticker` (string), `subaccount` (integer; 0=primary, 1-63 subaccounts; omitted = all), `limit` (int32, min 1, **max 100, default 100**), `status` (string), `creator_user_id` (string, **deprecated**), `user_filter` (`self`).
200 `GetRFQsResponse`: `{rfqs: RFQ[] (required), cursor: string}`. Errors 401, 500.

### POST /communications/rfqs — `CreateRFQ`
"You can have a maximum of **100 open RFQs** at a time."
Body `CreateRFQRequest` (required: `market_ticker`, `rest_remainder`):
- `market_ticker` string (required)
- `contracts` integer — "Whole-contract count for the RFQ. Use contracts_fp for partial contract values; if both are provided, they must match."
- `contracts_fp` FixedPointCount, nullable — 0.01-contract increments; must match `contracts` if both present
- `target_cost_centi_cents` int64 — **DEPRECATED**, use `target_cost_dollars`
- `target_cost_dollars` FixedPointDollars — "The target cost for the RFQ in dollars"
- `rest_remainder` boolean (required) — "Whether to rest the remainder of the RFQ after execution"
- `replace_existing` boolean, default false — "Whether to delete existing RFQs as part of this RFQ's creation"
- `subtrader_id` string (FCM members only)
- `subaccount` integer (direct members; 0 primary, 1-63)
201 `CreateRFQResponse`: `{id: string}` (required). Errors: 400, 401, **409 Conflict** (per RFQ guide: only one open RFQ per market ticker), 500.

### GET /communications/rfqs/{rfq_id} — `GetRFQ` → 200 `GetRFQResponse` `{rfq: RFQ}`. 401/404/500.
### DELETE /communications/rfqs/{rfq_id} — `DeleteRFQ` → 204 (no body). 401/404/500.

### RFQ object (schema `RFQ`; required: `id, creator_id, contracts_fp, market_ticker, status, created_ts`)
- `id` string — Unique identifier for the RFQ
- `creator_id` string — **Public communications ID** of the RFQ creator
- `market_ticker` string
- `contracts_fp` FixedPointCount — contracts requested
- `target_cost_dollars` FixedPointDollars — total value of the RFQ in dollars
- `status` string enum **`[open, closed]`**
- `created_ts` string date-time
- `mve_collection_ticker` string — "Ticker of the MVE collection this market belongs to"
- `mve_selected_legs` MveSelectedLeg[] — selected legs for the MVE collection
- `rest_remainder` boolean
- `cancellation_reason` string
- `creator_user_id` string — "(private field)"
- `creator_subaccount` integer — visible only to RFQ creator
- `cancelled_ts`, `updated_ts` string date-time

`MveSelectedLeg`: `{event_ticker: string, market_ticker: string, side: string, yes_settlement_value_dollars: FixedPointDollars|null (only filled after determination)}`.

### GET /communications/quotes — `GetQuotes`
Query params: `cursor`, `min_ts` (int64 Unix, quotes **last updated** after), `max_ts` (int64 Unix, last updated before), `limit` (int32, min 1, **max 500, default 500** — note: different from RFQs' 100), `status` (string), `quote_creator_user_id` (**deprecated**), `user_filter` (`self` — "quotes created by the authenticated user"), `rfq_user_filter` (`self` — "quotes responding to RFQs created by the authenticated user"), `rfq_creator_user_id` (**deprecated**), `rfq_creator_subtrader_id` (FCM only), `rfq_id` (string).
200 `GetQuotesResponse`: `{quotes: Quote[] (required), cursor: string}`. 401/500.

### POST /communications/quotes — `CreateQuote`  (**rate-limit cost: 2 tokens**)
Body `CreateQuoteRequest` (required: `rfq_id, yes_bid, no_bid, rest_remainder`):
- `rfq_id` string
- `yes_bid` — FixedPointDollars string — "The bid price for YES contracts, in dollars"  ⚠ field name is `yes_bid`, NOT `yes_bid_dollars`
- `no_bid` — FixedPointDollars string ⚠ NOT `no_bid_dollars`
- `rest_remainder` boolean (required)
- `post_only` boolean — "If true, the quote creator's resting order will be cancelled rather than crossed if it would take liquidity. Defaults to false."
- `subaccount` integer (0 primary, 1-63)
201 `CreateQuoteResponse`: `{id: string}`. Errors 400/401/500.
Guide constraints: quotes are two-sided per-contract prices; "Quotes are for the full RFQ size"; quotes cannot both be zero; `yes_bid + no_bid` cannot exceed $1; price must land on the market's price grid (see §5).

### GET /communications/quotes/{quote_id} — `GetQuote` (**2 tokens**) → 200 `GetQuoteResponse` `{quote: Quote}`. 401/404/500.
### DELETE /communications/quotes/{quote_id} — `DeleteQuote` (**2 tokens**) → 204. 401/404/500.
### DELETE /communications/rfqs/{rfq_id}/quotes/{quote_id} — `DeleteRFQQuote` (**2 tokens**) → 204. RFQ-scoped variant.
### PUT /communications/quotes/{quote_id}/accept — `AcceptQuote`
Body `AcceptQuoteRequest` (required): `{accepted_side: 'yes'|'no'}`. → **204**. "This will require the quoter to confirm". 400/401/404/500.
### PUT /communications/rfqs/{rfq_id}/quotes/{quote_id}/accept — `AcceptRFQQuote` — same body/response, RFQ-scoped.
### PUT /communications/quotes/{quote_id}/confirm — `ConfirmQuote`
Body optional (`EmptyResponse` = `{}`). → **204**. "This will start a timer for order execution". 401/404/500.
### PUT /communications/rfqs/{rfq_id}/quotes/{quote_id}/confirm — `ConfirmRFQQuote` — same.

### Quote object (schema `Quote`; required: `id, rfq_id, creator_id, rfq_creator_id, market_ticker, contracts_fp, yes_bid_dollars, no_bid_dollars, created_ts, updated_ts, status`)
- `id` string; `rfq_id` string
- `creator_id` string — public communications ID of the **quote** creator
- `rfq_creator_id` string — public communications ID of the RFQ creator
- `market_ticker` string
- `contracts_fp` FixedPointCount
- `yes_bid_dollars`, `no_bid_dollars` FixedPointDollars — ⚠ responses use `_dollars` suffix, request used `yes_bid`/`no_bid`
- `created_ts`, `updated_ts` date-time
- `status` enum **`[open, accepted, confirmed, executed, cancelled]`**
- `accepted_side` enum `['yes','no']`
- `accepted_ts`, `confirmed_ts`, `executed_ts`, `cancelled_ts` date-time
- `rest_remainder` boolean
- `post_only` boolean — visible only to quote creator
- `cancellation_reason` string
- `creator_user_id`, `rfq_creator_user_id` string (private fields)
- `rfq_target_cost_dollars` FixedPointDollars
- `rfq_creator_order_id` string — "Order ID for the RFQ creator (private field)"
- `creator_order_id` string — "Order ID for the quote creator (private field)"  ← **this is the fills linkage (see §4)**
- `creator_subaccount` int (visible to quote creator); `rfq_creator_subaccount` int (visible to RFQ creator)
- `yes_contracts_fp`, `no_contracts_fp` FixedPointCount — "Number of YES/NO contracts offered in the quote (fixed-point)" (differ in target-cost mode since side prices differ)

### RFQ lifecycle + timing (from getting_started/rfqs.md — NOT in openapi.yaml)
1. Requester creates RFQ (size = `contracts_fp` OR `target_cost_dollars`, plus rest_remainder). 2. "The RFQ is broadcast to all makers." 3. Makers quote two-sided (`yes_bid`+`no_bid`); each quote is private between requester and that maker. 4. "Requester accepts one side of the best-priced quote." 5. Maker must confirm within confirmation window; "Once confirmed, neither party can withdraw." 6. "Orders enter the public book after the execution timer expires."

| Market type | Confirmation window | Execution timer |
|---|---|---|
| Standard | **30 s** | **15 s** |
| **HVM (includes all combos)** | **3 s** | **1 s** |

Guide error codes: insufficient balance → `INSUFFICIENT_BALANCE`; quoting/acting on a closed RFQ → `RFQ_CLOSED`; second open RFQ on same market → `409 Conflict`.

### Block trades (adjacent, same tag)
- GET /communications/block-trade-proposals (`GetBlockTradeProposals`): params cursor, market_ticker, limit (1-100 default 100), status. → `{block_trade_proposals: BlockTradeProposal[], cursor}`.
- POST /communications/block-trade-proposals (`ProposeBlockTrade`) → 201; POST /communications/block-trade-proposals/{block_trade_proposal_id}/accept → 204.
- `BlockTradeProposal` still uses **legacy integer units**: `price_centi_cents` int64, `centicount` int64, `maker_side` `['yes','no']`, `buyer_order_id`/`seller_order_id` strings after execution, `expiration_ts` date-time, `buyer_accepted`/`seller_accepted` bool.

## 3. Multivariate event collections

### GET /multivariate_event_collections — `GetMultivariateEventCollections` (public)
Params: `status` enum `[unopened, open, closed]`, `associated_event_ticker` (string), `series_ticker` (string), `limit` (int32 1..**200**), `cursor`.
200 `GetMultivariateEventCollectionsResponse`: `{multivariate_contracts: MultivariateEventCollection[] (required), cursor: string}` ⚠ envelope key is `multivariate_contracts`, not "collections".

### GET /multivariate_event_collections/{collection_ticker} — `GetMultivariateEventCollection` (public)
200 `GetMultivariateEventCollectionResponse`: `{multivariate_contract: MultivariateEventCollection}` (required). 400/404/500.

### POST /multivariate_event_collections/{collection_ticker} — `CreateMarketInMultivariateEventCollection` (auth)
"This endpoint must be hit at least once **before trading or looking up a market**. Users are limited to **5000 creations per week**."
Body `CreateMarketInMultivariateEventCollectionRequest`: `{selected_markets: TickerPair[] (required), with_market_payload: boolean}`.
`TickerPair` (required all): `{market_ticker: string, event_ticker: string, side: 'yes'|'no'}`.
200 `CreateMarketInMultivariateEventCollectionResponse`: `{event_ticker: string (req), market_ticker: string (req), market: Market (only if with_market_payload)}`. Errors 400/401/**429 RateLimitError**/500.

### PUT /multivariate_event_collections/{collection_ticker}/lookup — `LookupTickersForMarketInMultivariateEventCollection` — **DEPRECATED**
"DEPRECATED: This endpoint predates RFQs and should not be used for new integrations." Returns 404 if that combination was never created. Body `{selected_markets: TickerPair[]}` → `{event_ticker, market_ticker}`. Cost 2 tokens. Do not build on this.

### MultivariateEventCollection object (required: `collection_ticker, series_ticker, title, description, open_date, close_date, associated_events, associated_event_tickers, is_ordered, is_single_market_per_event, is_all_yes, size_min, size_max, functional_description`)
- `collection_ticker` string; `series_ticker` string ("Events produced in the collection will be associated with this series")
- `title`, `description` string
- `open_date`, `close_date` date-time — outside this window "the collection cannot be interacted with"
- `associated_events` **AssociatedEvent[]** — current source of truth. `AssociatedEvent` (required: ticker, is_yes_only, active_quoters): `{ticker: string, is_yes_only: boolean, size_max: int32|null (max markets from this event; null = no limit), size_min: int32|null, active_quoters: string[] ("List of active quoters for this event")}`
- `associated_event_tickers` string[] — **[DEPRECATED — use associated_events]**
- `is_ordered` boolean — if true, order of markets passed to Lookup/Create affects output
- `is_single_market_per_event` boolean — **[DEPRECATED]**
- `is_all_yes` boolean — **[DEPRECATED]**
- `size_min`, `size_max` int32 — min/max number of markets passed into Lookup/Create (inclusive)
- `functional_description` string — how inputs map to output

### GET /events/multivariate — `GetMultivariateEvents` (public)
"Retrieve multivariate (combo) events. These are dynamically created events from multivariate event collections."
Params: `limit` (1..**200**, default 100), `cursor`, `series_ticker`, `collection_ticker` (cannot combine with series_ticker), `with_nested_markets` (bool, default false — adds `markets: Market[]` per event). → `GetMultivariateEventsResponse`. 400/401/500.

### Combo filtering on GET /markets
`mve_filter` query param, enum `['only','exclude']` — "'only' returns only multivariate events, 'exclude' excludes multivariate events." GET /markets supports: limit (0..1000 default 100), cursor, event_ticker (single), series_ticker, min/max_created_ts, min_updated_ts (metadata changes only; incompatible with almost everything except mve_filter=exclude), min/max_close_ts, min/max_settled_ts, status (query enum `[unopened, open, paused, closed, settled]`), tickers (comma-sep), mve_filter. → `{markets: Market[], cursor}` (both required).

## 4. GET /portfolio/fills — `GetFills` (auth)

"A fill is when a trade you have is matched." Fills before the historical cutoff only via `GET /historical/fills`.
Query params: `ticker` (market ticker filter — param name is `ticker`), `order_id` (string), `min_ts`/`max_ts` (int64 Unix), `limit` (int64 1..**1000**, default 100), `cursor`, `subaccount` (int; omitted = all subaccounts).
200 `GetFillsResponse` (required both): `{fills: Fill[], cursor: string}`.

### Fill object (required: `fill_id, trade_id, order_id, ticker, market_ticker, side, action, outcome_side, book_side, count_fp, yes_price_dollars, no_price_dollars, is_taker, fee_cost`)
- `fill_id` string — unique id for this fill
- `trade_id` string — "Unique identifier for this fill (**legacy field name, same as fill_id**)" ⚠ it is NOT documented as the public trade id
- `order_id` string — "Unique identifier for the order that resulted in this fill" ← **join key: Quote.creator_order_id (maker) or Quote.rfq_creator_order_id (requester) == Fill.order_id.** There is NO `creator_order_id` field on Fill itself.
- `ticker` string; `market_ticker` string (legacy alias of ticker)
- `side` enum `['yes','no']` — **deprecated**, "will not be removed before May 14, 2026"; use outcome_side/book_side
- `action` enum `['buy','sell']` — **deprecated** same note
- `outcome_side` enum `['yes','no']` — "The outcome side this fill positioned the user for. buy-yes and sell-no produce 'yes'; buy-no and sell-yes produce 'no'." Directional exposure only; price unchanged.
- `book_side` BookSide (`bid`≡outcome_side yes, `ask`≡outcome_side no)
- `count_fp` FixedPointCount — contracts in this fill
- `yes_price_dollars`, `no_price_dollars` FixedPointDollars — fill price for each side
- `is_taker` boolean — true if this fill removed liquidity
- `created_time` date-time; `ts` int64 Unix (legacy)
- `fee_cost` FixedPointDollars
- `subaccount_number` integer|null (0 primary, 1-63; present for direct users)

RFQ guide confirmation: "Fills appear in `GET /portfolio/fills`, matched via `creator_order_id` (maker) or `rfq_creator_order_id` (requester)" — i.e., read those ids off the Quote object, then filter fills by `order_id`.

## 5. GET /markets/{ticker} — Market schema (price grid focus)

GET /markets/{ticker} (public) → 200 `GetMarketResponse`: `{market: Market}` (required). 401/404/500.

### Market object — price-grid-relevant fields (all in required list unless noted)
- `ticker`, `event_ticker` string; `market_type` enum `[binary, scalar]`
- **`price_level_structure`** string — "Price level structure for this market, defining price ranges and tick sizes"
- **`price_ranges`**: `PriceRange[]` — "Valid price ranges for orders on this market". `PriceRange` (required all): `{start: string ("Starting price for this range in dollars"), end: string, step: string ("Price step/tick size for this range in dollars")}` ⚠ there is **no** top-level `tick_size` field; the grid can be non-uniform across ranges.
- **`notional_value_dollars`** FixedPointDollars — "The total value of a single contract at settlement in dollars" ⚠ field is `notional_value_dollars` (string), not integer-cents `notional_value`
- `status` enum `[initialized, inactive, active, closed, determined, disputed, amended, finalized]` ⚠ different vocabulary from GET /markets `status` query filter (`unopened/open/paused/closed/settled`)
- Book snapshot: `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars` (FixedPointDollars), `yes_bid_size_fp`, `yes_ask_size_fp` (FixedPointCount), `last_price_dollars`, `previous_yes_bid_dollars`, `previous_yes_ask_dollars`, `previous_price_dollars`
- Volume/OI: `volume_fp`, `volume_24h_fp`, `open_interest_fp` (FixedPointCount); `liquidity_dollars` **DEPRECATED, always "0.0000"**
- Times: `created_time`, `updated_time` (non-trading metadata updates only), `open_time`, `close_time`, `expected_expiration_time` (nullable), `expiration_time` (**deprecated**), `latest_expiration_time`, `settlement_timer_seconds` int, `fee_waiver_expiration_time` (nullable)
- Settlement: `result` enum `['yes','no','scalar','']`, `settlement_value_dollars` (nullable, post-determination), `settlement_ts` (nullable), `expiration_value` string, `can_close_early` bool, `early_close_condition` (nullable)
- Strikes: `strike_type` enum `[greater, greater_or_equal, less, less_or_equal, between, functional, custom, structured]`, `floor_strike`/`cap_strike` double nullable, `functional_strike` string nullable, `custom_strike` object nullable
- Titles/rules: `yes_sub_title`, `no_sub_title` (required); `title`, `subtitle` **deprecated**; `rules_primary`, `rules_secondary` (required)
- **MVE linkage**: `mve_collection_ticker` string, `mve_selected_legs: MveSelectedLeg[]` — present on combo markets
- Misc: `is_provisional` bool ("may be removed after determination if there is no activity"), `primary_participant_key` nullable, `exchange_index`

Orderbook (adjacent): GET /markets/{ticker}/orderbook → `OrderbookCountFp`: `{yes_dollars: PriceLevelDollarsCountFp[], no_dollars: [...]}` where each level is a 2-element string array `["0.1500","100.00"]` = [price dollars, contract count fp].

## 6. V2 order endpoints (future hedging)

Legacy note: "The legacy `/portfolio/orders` endpoint will be deprecated **no earlier than May 6, 2026** — clients should migrate to this path." (POST create; the GETs below remain.)

- GET /portfolio/orders — `GetOrders` (auth): params ticker, event_ticker (comma-sep, max 10), min_ts, max_ts, status (`resting|canceled|executed`), limit (1..1000 default 100), cursor, subaccount → `GetOrdersResponse` `{orders: Order[], cursor}`.
- GET /portfolio/orders/{order_id} — `GetOrder` (**2 tokens**) → `{order: Order}`.
- GET /portfolio/orders/queue_positions — `GetOrderQueuePositions`: params `market_tickers` (comma-sep), `event_ticker`, subaccount (default 0). Queue position = contracts ahead of you, price-time priority.
- GET /portfolio/orders/{order_id}/queue_position — `GetOrderQueuePosition`.

### POST /portfolio/events/orders — `CreateOrderV2` (returns 201; errors 400/401/409/429/500)
Body `CreateOrderV2Request` (required: `ticker, side, count, price, time_in_force, self_trade_prevention_type`):
- `ticker` string; `client_order_id` string (optional)
- `side` BookSide `bid|ask` (YES-leg vocabulary)
- `count` FixedPointCount string; `price` FixedPointDollars string
- `time_in_force` enum `['fill_or_kill','good_till_canceled','immediate_or_cancel']` — "GTT is an internal execution type and is not a valid API value"; for expiring orders use `good_till_canceled` + `expiration_time` (int64 Unix **seconds**); `immediate_or_cancel` cannot combine with `expiration_time`
- `post_only` bool; `self_trade_prevention_type` required (`taker_at_cross|maker`)
- `cancel_order_on_pause` bool; `reduce_only` bool ("place count capped by member's current position")
- `subaccount` int default 0; `order_group_id` string; `exchange_index` int default 0 (-1 = auto-route)
Example request: `{ticker: HIGHNY-24JAN01-T60, client_order_id: 8c35ecb3-..., side: bid, count: "10.00", price: "0.5600", time_in_force: good_till_canceled, self_trade_prevention_type: taker_at_cross, post_only: false, ...}`
201 `CreateOrderV2Response` (required: order_id, fill_count, remaining_count, ts_ms): `{order_id, client_order_id, fill_count: FP count, remaining_count: FP count, average_fill_price: FP dollars (only when fill_count>0), average_fee_paid: FP dollars per contract (only when fill_count>0), ts_ms: int64 epoch ms}`.

### POST /portfolio/events/orders/batched — `BatchCreateOrdersV2` (**10 tokens per order, billed per item**)
Body `{orders: CreateOrderV2Request[]}` → 201 `{orders: [{order_id, client_order_id, fill_count, remaining_count, average_fill_price, average_fee_paid, ts_ms (absent when errored), error: ErrorResponse|null}]}`. Max batch size scales with tier write budget.

### DELETE /portfolio/events/orders/batched — `BatchCancelOrdersV2` (**2 tokens per order**)
Body `{orders: [{order_id (required), subaccount default 0, exchange_index default 0, market_ticker (required when exchange_index=-1)}]}` → 200 `{orders: [{order_id, client_order_id, reduced_by (FP count; zero if cancel errored), ts_ms, error}]}`.

### DELETE /portfolio/events/orders/{order_id} — `CancelOrderV2` (**2 tokens**)
Query: subaccount (default 0), exchange_index, market_ticker (required when exchange_index=-1). → 200 `CancelOrderV2Response` `{order_id, client_order_id, reduced_by: FP count ("the remaining count at time of cancellation"), ts_ms}`.

### POST /portfolio/events/orders/{order_id}/amend — `AmendOrderV2`
"The request `count` is the updated **total/max fillable count**, equal to already filled count plus desired resting remaining count." Note: "Amending a resting order preserves queue position **only when the amendment decreases size**. All other amendments — like increasing size or changing price — forfeit queue position."
Body (required: ticker, side, price, count): `{ticker, side: bid|ask, price: FP dollars, count: FP count, client_order_id (original), updated_client_order_id, exchange_index}` → 200 `{order_id, client_order_id, remaining_count (actual post-amend resting qty, nullable), fill_count (fills caused by amend crossing, nullable), average_fill_price, average_fee_paid, ts_ms}`.

### POST /portfolio/events/orders/{order_id}/decrease — `DecreaseOrderV2`
"Exactly one of `reduce_by` or `reduce_to` must be provided." Body `{reduce_by: FP count|null, reduce_to: FP count|null, exchange_index}` → 200 `{order_id, client_order_id, remaining_count, ts_ms}`.

### Order object (for GETs; required: order_id, user_id, client_order_id, ticker, side, action, outcome_side, book_side, type, status, yes_price_dollars, no_price_dollars, fill_count_fp, remaining_count_fp, initial_count_fp, taker_fees_dollars, maker_fees_dollars, taker_fill_cost_dollars, maker_fill_cost_dollars)
`order_id, user_id, client_order_id, ticker` strings; `side`/`action` **deprecated** (not removed before May 14, 2026); `outcome_side` `yes|no`; `book_side` `bid|ask`; `type` enum `[limit, market]`; `status` enum `[resting, canceled, executed]`; `yes_price_dollars`/`no_price_dollars`; `fill_count_fp`/`remaining_count_fp`/`initial_count_fp`; `taker_fill_cost_dollars`/`maker_fill_cost_dollars`; `taker_fees_dollars`/`maker_fees_dollars`; `expiration_time` date-time nullable; `created_time`, `last_update_time` (modify/cancel/fill); `self_trade_prevention_type` nullable; `order_group_id` nullable; `cancel_order_on_pause` bool; `subaccount_number` int nullable; `exchange_index`.

## 7. Rate limits (getting_started/rate_limits.md + spec x-mint notes)

Token buckets, two independent buckets (Read, Write); request allowed iff bucket holds its cost, else **HTTP 429**. `BucketLimit` = `{refill_rate: int (tokens/sec), bucket_capacity: int}`.

| Tier | Read tok/s | Write tok/s | Burst |
|---|---|---|---|
| Basic | 200 | 100 | 1 s |
| Advanced | 300 | 300 | 2 s |
| Expert | 600 | 600 | 2 s |
| Premier | 1,000 | 1,000 | 2 s |
| Paragon | 2,000 | 2,000 | 2 s |
| Prime | 4,000 | 4,000 | 2 s |
| Prestige | 6,000 | 8,000 | 2 s |

- **Default endpoint cost: 10 tokens** (`GetAccountEndpointCostsResponse.default_cost`, "currently 10").
- Non-default costs seen in spec x-mint notes: `CreateQuote` 2, `GetQuote` 2, `DeleteQuote` 2, `DeleteRFQQuote` 2, `GetOrder` 2, `CancelOrderV2` 2, batch create 10/order, batch cancel 2/order, `UpgradeAccountApiUsageLevel` 30. Discover authoritatively via **GET /account/endpoint_costs** (public) → `{default_cost, endpoint_costs: [{method, path, cost}]}`.
- Batch billing: "A batch request costs the same as making each call individually"; whole batch must fit bucket at submission.
- Tier upgrade: POST /account/api_usage_level/upgrade (30 tokens; criteria: ≥1 of last 100 Predictions orders created via API) grants permanent Advanced. Expert+ via 30-day volume (GET /account/api_usage_level/volume_progress: `trailing_30d_volume_fp`, per-level `earn_volume_goal_fp`/`keep_volume_goal_fp`). Inspect current tier: GET /account/limits → `{usage_tier, read: BucketLimit, write: BucketLimit, grants: [{exchange_instance: event_contract|margined, level, expires_ts (absent = permanent), source: volume|manual}]}`.

## 8. WebSocket `communications` channel (websockets/communications.md)

- URL `wss://external-api-ws.kalshi.com`, channel `communications`, API-key auth during handshake. Optional sharding params `shard_factor` (1-100) and `shard_key`.
- Visibility: **RFQ events (rfq_created / rfq_deleted) are always transmitted to everyone; quote events (quote_created / quote_accepted / quote_executed) only transmit if you initiated the quote or RFQ.**
- Message envelope: `{type, sid, msg}`. Types and msg fields:
  - `rfq_created`: `{id, creator_id, market_ticker, event_ticker, contracts_fp, target_cost_dollars, created_ts, mve_collection_ticker, mve_selected_legs}`
  - `rfq_deleted`: same identity fields + `deleted_ts`
  - `quote_created`: `{quote_id, rfq_id, quote_creator_id, market_ticker, event_ticker, yes_bid_dollars, no_bid_dollars, yes_contracts_offered_fp, no_contracts_offered_fp, rfq_target_cost_dollars, created_ts}` ⚠ WS uses `quote_creator_id` and `*_contracts_offered_fp`; REST uses `creator_id` and `yes_contracts_fp`/`no_contracts_fp`
  - `quote_accepted`: adds `accepted_side`, `contracts_accepted_fp` (can be less than offered)
  - `quote_executed`: `{quote_id, rfq_id, quote_creator_id, rfq_creator_id, order_id, client_order_id, market_ticker, executed_ts}` — "Use `client_order_id` to correlate executed quotes with fill messages"

## 9. Historical data cutoff (affects backtests)

Fills/orders/markets that settled/completed before the historical cutoff move to `GET /historical/fills`, `GET /historical/orders`, `GET /historical/markets`, `GET /historical/markets/{ticker}`, `GET /historical/trades`, `GET /historical/markets/{ticker}/candlesticks`; cutoff discoverable via `GET /historical/cutoff`. Resting orders always available on /portfolio/orders.

## 10. Implementer traps (explicit)

1. **Request/response asymmetry on quotes**: send `yes_bid`/`no_bid`; read back `yes_bid_dollars`/`no_bid_dollars`.
2. **All prices/counts are strings** (FixedPointDollars up to 6 dp; FixedPointCount 2 dp, min 0.01 contracts). No cents integers on any endpoint in this digest (except deprecated fields and block trades' `price_centi_cents`).
3. **No `tick_size`/`notional_value` fields** on Market — use `price_ranges[{start,end,step}]` (dollar strings, possibly multiple ranges with different steps) and `notional_value_dollars`.
4. **Fill has no creator_order_id** — linkage is Quote.`creator_order_id`/`rfq_creator_order_id` → Fill.`order_id`. Fill.`trade_id` is documented as an alias of `fill_id`, not the public trade id.
5. **Combo timing is HVM: 3 s to confirm after acceptance, 1 s execution timer.** A polling loop is too slow; you need the WS channel and an immediate confirm call.
6. **Accept/Confirm are PUT** (not POST) and return **204** with empty bodies; success detection must be status-code based.
7. GET /communications/rfqs limit max **100**; GET /communications/quotes limit max **500** (defaults equal to their maxima are different too: 100 vs 500).
8. Envelope keys for MVE collections are `multivariate_contract(s)`, not "collection(s)".
9. `CreateMarketInMultivariateEventCollection` must be called before trading a combo; 5000/week cap; lookup endpoint is deprecated.
10. `side` on V2 orders is `bid|ask` on the **YES leg**; legacy `side`(`yes|no`)+`action`(`buy|sell`) fields on Order/Fill are deprecated (kept until at least 2026-05-14). `outcome_side`: buy-yes & sell-no → `yes`; buy-no & sell-yes → `no`.
11. `creator_user_id`/`quote_creator_user_id` query filters are **deprecated**; use `user_filter=self` / `rfq_user_filter=self`.
12. Timestamp header is **milliseconds**; V2 order `expiration_time` is **seconds**; `ts_ms` responses are epoch ms; `min_ts`/`max_ts` queries are Unix seconds ("Unix Timestamp").
13. One open RFQ per market ticker (409) even though up to 100 open RFQs overall; `replace_existing: true` on CreateRFQ deletes existing RFQs during creation.
14. GET /markets status query filter enum (`unopened/open/paused/closed/settled`) ≠ Market.status field enum (`initialized/inactive/active/closed/determined/disputed/amended/finalized`).

## Critical facts (must get right)
- Base URLs: prod https://external-api.kalshi.com/trade-api/v2 (or api.elections.kalshi.com), demo https://external-api.demo.kalshi.co/trade-api/v2 (or demo-api.kalshi.co); WS prod wss://external-api-ws.kalshi.com, channel 'communications'.
- Auth = 3 headers on every authed call: KALSHI-ACCESS-KEY (key id), KALSHI-ACCESS-SIGNATURE (RSA-PSS), KALSHI-ACCESS-TIMESTAMP (milliseconds).
- Combos are HVM markets: maker has 3 seconds to confirm after the requester accepts, then a 1-second execution timer (standard markets: 30 s / 15 s). Bot must confirm via PUT /communications/quotes/{quote_id}/confirm (204, empty body) within 3 s or lose the trade.
- CreateQuote request body fields are rfq_id, yes_bid, no_bid (FixedPointDollars strings), rest_remainder (required), post_only, subaccount — the request uses yes_bid/no_bid but every Quote response uses yes_bid_dollars/no_bid_dollars.
- Quote constraints: two-sided, for full RFQ size, yes_bid + no_bid <= $1.00, not both zero, prices must land on the market's price grid.
- Quote.status lifecycle enum: open -> accepted -> confirmed -> executed, or cancelled. Accept = PUT .../accept with body {accepted_side: 'yes'|'no'}; both accept and confirm return 204.
- Fill linkage: GET /portfolio/fills Fill has order_id (plus fill_id, and trade_id documented as a legacy alias of fill_id) but NO creator_order_id; the maker's order id comes from Quote.creator_order_id (requester side: Quote.rfq_creator_order_id) and equals Fill.order_id. WS quote_executed also delivers order_id + client_order_id.
- All prices are FixedPointDollars strings (up to 6 decimal places, e.g. "0.5600") and all counts are FixedPointCount strings (2 decimals, min granularity 0.01 contracts; requests accept 0-2 dp, responses always 2 dp). No integer-cents fields on RFQ/quote/order/market endpoints (deprecated target_cost_centi_cents and block trades excepted).
- Market price grid: Market.price_ranges = array of {start, end, step} dollar strings (possibly multiple ranges with different steps) plus price_level_structure string; contract payout is notional_value_dollars. There is no tick_size or notional_value integer field.
- RFQ object: id, creator_id (public communications id), market_ticker, contracts_fp, target_cost_dollars, status (open|closed), created_ts, mve_collection_ticker, mve_selected_legs[{event_ticker, market_ticker, side, yes_settlement_value_dollars}], rest_remainder; sizing is contracts_fp OR target_cost_dollars.
- CreateRFQ: max 100 open RFQs per user, only one open RFQ per market ticker (409 Conflict), replace_existing:true deletes existing RFQs at creation; rest_remainder is required.
- POST /multivariate_event_collections/{collection_ticker} (CreateMarketInMultivariateEventCollection) with selected_markets:[{market_ticker, event_ticker, side:'yes'|'no'}] must be called at least once before trading or looking up a combo market; limit 5000 creations/week; the /lookup endpoint is DEPRECATED; collection responses are keyed multivariate_contract(s).
- V2 orders: POST /portfolio/events/orders with {ticker, side: bid|ask (YES leg: bid=buy YES, ask=sell YES), count: FP string, price: FP dollars string, time_in_force: fill_or_kill|good_till_canceled|immediate_or_cancel, self_trade_prevention_type: taker_at_cross|maker (required), client_order_id, post_only}; cancel = DELETE /portfolio/events/orders/{order_id} (2 tokens); batch create bills 10 tokens/order, batch cancel 2 tokens/order; legacy POST /portfolio/orders deprecated no earlier than 2026-05-06.
- Rate limits: token buckets per second, default endpoint cost 10 tokens; CreateQuote/DeleteQuote/GetQuote/DeleteRFQQuote/GetOrder cost 2; Basic tier = 200 read + 100 write tokens/sec (Advanced 300/300 via free upgrade endpoint); 429 on empty bucket; authoritative costs from GET /account/endpoint_costs.
- WS visibility: rfq_created/rfq_deleted broadcast to everyone; quote_created/quote_accepted/quote_executed are only sent to participants (quote or RFQ initiator). WS field names differ from REST: quote_creator_id (not creator_id), yes/no_contracts_offered_fp (not yes/no_contracts_fp).
- GET /portfolio/fills params: ticker, order_id, min_ts, max_ts (Unix seconds), limit 1-1000 default 100, cursor, subaccount; Fill direction fields side/action are deprecated (gone no earlier than 2026-05-14) in favor of outcome_side (yes|no) and book_side (bid|ask); is_taker flags liquidity removal; fee_cost is FixedPointDollars.
- Combo discovery: GET /markets?mve_filter=only|exclude; GET /events/multivariate?collection_ticker=...&with_nested_markets=true (limit max 200); combo Market objects carry mve_collection_ticker + mve_selected_legs.

## Open questions (verify empirically on demo)
- Exact demo WebSocket URL for the communications channel (docs only give prod wss://external-api-ws.kalshi.com); verify demo WS host and handshake auth format on demo.
- Whether Fill.trade_id can be joined to public trade ids from GET /markets/trades for RFQ executions — spec now says trade_id is a legacy alias of fill_id, which contradicts the older assumption that fills and public trades share trade_id. Empirically execute an RFQ on demo and compare.
- Partial acceptance semantics: WS quote_accepted carries contracts_accepted_fp (example shows 50.00 accepted vs 100.00 offered) but REST AcceptQuoteRequest has only accepted_side with no count — determine how accepted size is set (target_cost mode conversion? requester-side partial?) and what yes_contracts_fp vs no_contracts_fp mean per sizing mode.
- Where the maker's client_order_id in the WS quote_executed message comes from — CreateQuoteRequest has no client_order_id field; verify whether it is auto-generated (e.g. equals quote id) on demo.
- The rate-limit token cost of CreateRFQ, AcceptQuote, ConfirmQuote, DeleteRFQ (no x-mint notes in spec — presumably default 10, but CreateQuote is 2); pull GET /account/endpoint_costs on demo for the authoritative table.
- Exact spelling of the RFQ guide's timing classification: whether ALL MVE/combo markets are always HVM (3s confirm / 1s execution) or the class can vary per market; measure actual accept->cancel timing on demo.
- What price/size the post-execution book orders rest at and how rest_remainder interacts with partial execution ('Orders enter the public book after the execution timer expires') — verify resulting Order objects (price, remaining_count, post_only effect) after an RFQ execution on demo.
- Whether replace_existing=true on CreateRFQ deletes ALL of the caller's open RFQs or only the one on the same market ticker.
- Whether quotes and RFQ/quote responses on combos respect price_ranges steps finer than $0.01 (FixedPointDollars allows 6 dp) — fetch a real combo market's price_ranges on demo and test off-grid quote rejection (expected 400).
- Whether Quote.creator_order_id / rfq_creator_order_id are populated at accepted/confirmed status or only after executed — needed to know when the fills poller can start filtering by order_id.
- Whether is_taker on the maker's fill from an RFQ execution is false (maker) — memory says RFQ fills appear as REGULAR trades; confirm fee treatment (fee_cost) for the quoting side on demo.
- GET /communications/rfqs 'status' query values — spec types it as a bare string (RFQ.status enum is open|closed); confirm accepted filter values (e.g. 'open') empirically.
