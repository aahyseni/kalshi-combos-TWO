# Kalshi Docs Index Scan — docs NOT in the base set, fetched + digested

Source index: `https://docs.kalshi.com/llms.txt` (fetched 2026-07-05, API version 3.23.0). Base set assumed covered elsewhere: rfqs, communications endpoints, multivariate collections, orderbooks, auth/environments, rate limits, fixed point, fee rounding, live data, exchange status, python sdk, openapi/asyncapi.

## 0. Missed-doc inventory (all fetched below unless marked)

HIGHLY relevant and fetched: Portfolio (balance, fills, positions, settlements, resting order value, subaccounts x6), Orders V2 (create/amend/batch-create/batch-cancel/cancel/decrease/get-orders/queue-position), Market metadata (get-market, get-markets, get-event, get-events, get-multivariate-events, get-event-metadata, get-series, get-series-list, get-trades), Structured Targets, Event/Series fee-change endpoints, Account API-limits endpoints (x4), Exchange schedule + user-data-timestamp, Getting-started (order_direction, market_lifecycle, market_settlement, pagination, maintenance_and_pauses, subaccounts, order_groups, historical_data, terms), Order Groups (create endpoint), Incentive Programs, WS channels (fill, user_orders, market_positions, market_lifecycle_v2, multivariate_market_lifecycle, multivariate-lookups-deprecated), Search (filters_by_sport), API Changelog.

Deemed marginal, NOT fetched: FCM endpoints (FCM members only), Milestones, candlestick endpoints (get-market-candlesticks / batch / event candlesticks / forecast percentile history — useful for pricing research, plain OHLC), deposits/withdrawals, CF Benchmarks passthrough, all Perps/`margin-rest` docs (separate exchange), all FIX docs (except noting FIX RFQ exists at `/fix/rfq-messages.md`), historical-data individual endpoint pages (concept page fetched).

**HVM ("high volatility market") rules: NO document in the entire index mentions HVM or volatility rules. Not in glossary, not in changelog, not in market lifecycle.** The only pause machinery documented is exchange/trading pauses + `cancel_order_on_pause`.

## 1. Environments / hosts (recap where pages restated them)
- Prod REST: `https://external-api.kalshi.com/trade-api/v2` (alt: `https://api.elections.kalshi.com/trade-api/v2`)
- Demo REST: `https://external-api.demo.kalshi.co/trade-api/v2` (alt: `https://demo-api.kalshi.co/trade-api/v2`)
- Prod WS: `wss://external-api-ws.kalshi.com` (dedicated external hosts documented 2026-05-07)
- Auth headers (all authed endpoints): `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE` (RSA-PSS), `KALSHI-ACCESS-TIMESTAMP` (**milliseconds**).
- Fixed-point conventions used everywhere: `FixedPointDollars` = string, up to 6 decimals (e.g. `"0.5600"`); `FixedPointCount` = string, requests accept 0–2 decimals, responses always emit 2 decimals (e.g. `"10.00"`), minimum granularity 0.01 contracts.

## 2. Portfolio endpoints

### GET `/portfolio/balance` (auth; scope alt `read::portfolio_balance`)
Query: `subaccount` (int, optional, default 0; 0=primary, 1–63=subaccounts).
Response: `balance` (int64, **cents**), `balance_dollars` (string fixed-point), `portfolio_value` (int64, cents), `updated_ts` (int64 Unix), `balance_breakdown` (array of `{exchange_index:int, balance:string}`; exchange_index currently only 0).

### GET `/portfolio/fills` (auth)
Query: `ticker`, `order_id`, `min_ts` (int64), `max_ts` (int64), `limit` (default 100, 1–1000), `cursor`, `subaccount` (int; **omit → fills across ALL subaccounts**).
Response: `{fills: Fill[], cursor: string}`. Fill fields: `fill_id`, `trade_id` (legacy alias of fill_id), `order_id`, `ticker`, `market_ticker` (legacy alias), `side` (enum yes|no, **DEPRECATED**), `action` (enum buy|sell, **DEPRECATED**), `outcome_side` (yes|no), `book_side` (bid|ask), `count_fp` (string 2dp), `yes_price_dollars`, `no_price_dollars`, `is_taker` (bool), `created_time` (date-time), `fee_cost` (string fixed-point dollars), `subaccount_number` (int nullable), `ts` (int64, legacy).
Notes: fills older than historical cutoff only via `GET /historical/fills`. `client_order_id` was REMOVED from Fill responses 2026-03-30 (join to orders via `order_id`). Legacy `side`/`action` remain until ≥2026-05-14 (order_direction page says ≥2026-05-28 — treat as already past-due/unstable; use new fields).

### GET `/portfolio/positions` (auth)
Query: `cursor`, `limit` (default 100, 1–1000), `count_filter` (comma list; accepted values: `position`, `total_traded`), `ticker`, `event_ticker` (single only), `subaccount` (default 0).
Response: `{cursor, market_positions: MarketPosition[], event_positions: EventPosition[]}`.
MarketPosition: `ticker`, `total_traded_dollars`, `position_fp` (**signed string: negative = NO position, positive = YES**), `market_exposure_dollars`, `realized_pnl_dollars`, `fees_paid_dollars`, `last_updated_ts` (date-time).
EventPosition: `event_ticker`, `total_cost_dollars`, `total_cost_shares_fp`, `event_exposure_dollars`, `realized_pnl_dollars`, `fees_paid_dollars`.
Note: `MarketPosition.resting_orders_count` scheduled for removal 2026-07-09 (do not depend on it).

### GET `/portfolio/settlements` (auth)
Query: `limit` (100, 1–1000), `cursor`, `ticker`, `event_ticker` (single), `min_ts`, `max_ts`, `subaccount` (omit = all).
Settlement: `ticker`, `event_ticker`, `market_result` (enum `yes`|`no`|`scalar`), `yes_count_fp`, `yes_total_cost_dollars`, `no_count_fp`, `no_total_cost_dollars`, `revenue` (int **cents**; winners pay 100¢), `settled_time` (date-time), `fee_cost` (string dollars), `value` (int cents, nullable; payout per YES contract).

### GET `/portfolio/summary/total_resting_order_value` (auth)
Response: `total_resting_order_value` (int, cents). **FCM-members-only endpoint — "If you're uncertain about this endpoint, it likely does not apply to you."** Do not use for MM capital tracking; compute resting exposure from orders instead.

### Subaccounts
- Concept: partition balance/positions into buckets under ONE set of API credentials. `0` = primary; `1`–`63` numbered (63 extra, 64 total).
- `POST /portfolio/subaccounts` (auth) body: `exchange_index` (int, optional, default 0). Response 201: `subaccount_number` (int, 1–63). Page says "Currently only available to institutions and market makers"; changelog 2026-05-12 says "all direct members with advanced API access" — conflicting, verify.
- `GET /portfolio/subaccounts/balances` (auth, no params) → `subaccount_balances[]: {subaccount_number:int, exchange_index:int, balance: string FixedPointDollars, updated_ts:int64}`. (Note: string dollars here, unlike primary `/portfolio/balance` cents int.)
- `POST /portfolio/subaccounts/transfer` (cash) and `POST /portfolio/subaccounts/positions/transfer` (positions; requires `price_cents`, `market_ticker`, `side`, `count`). Both idempotent via required `client_transfer_id`; replaying same id returns **409**. Transfers net to zero account-wide.
- `GET /portfolio/subaccounts/transfers` → paginated, items carry `transfer_type` (`cash`|`position`).
- Netting: `GET /portfolio/subaccounts/netting` → `netting_configs[]: {subaccount_number:int, enabled:bool}`; `PUT`-style update endpoint exists (`update-subaccount-netting`). What "netting enabled" actually changes is NOT documented.
- Subaccount filter semantics vary by endpoint: fills/orders/settlements **omit = all subaccounts**; balance/positions default to `0`.

## 3. Orders V2 (current; legacy `/portfolio/orders` mutations deprecated 2026-06-18, higher rate cost since 2026-05-25; will not be removed before 2026-05-06 per create page)

All V2 order endpoints authed. Single-book **bid/ask** vocabulary with fixed-point prices (introduced 2026-04-22).

### POST `/portfolio/events/orders` — Create Order (V2). 201. **Cost: 10 tokens.**
Body: `ticker` (req), `side` (req, enum `bid`|`ask`), `count` (req, FixedPointCount string), `price` (req, FixedPointDollars string, up to 6dp), `time_in_force` (req, enum `fill_or_kill`|`good_till_canceled`|`immediate_or_cancel`), `self_trade_prevention_type` (req, enum `taker_at_cross`|`maker`), `client_order_id` (opt), `expiration_time` (opt, **Unix SECONDS**; only with `good_till_canceled`, incompatible with IOC), `post_only` (opt, default false), `cancel_order_on_pause` (opt, default false), `reduce_only` (opt, default false), `subaccount` (opt, default 0), `order_group_id` (opt), `exchange_index` (opt, default 0; `-1` = auto-routing).
Response: `order_id`, `client_order_id`, `fill_count` (FixedPointCount), `remaining_count`, `average_fill_price` (only when fill_count>0), `average_fee_paid` (only when fill_count>0), `ts_ms` (int, matching-engine Unix ms).
Errors: 400/401/409/429/500. NOTE: `type=market` was removed 2026-02-12 — limit orders only.
Example request/response (verbatim):
```json
{"ticker":"HIGHNY-24JAN01-T60","client_order_id":"8c35ecb3-328f-4f52-8c7c-0f4b9862f8d1","side":"bid","count":"10.00","price":"0.5600","time_in_force":"good_till_canceled","self_trade_prevention_type":"taker_at_cross","post_only":false,"cancel_order_on_pause":false,"reduce_only":false,"subaccount":0,"exchange_index":0}
```
```json
{"order_id":"3b23c1c7-f4ef-4f0d-8b9a-9e53c61f1a0d","client_order_id":"8c35ecb3-328f-4f52-8c7c-0f4b9862f8d1","fill_count":"0.00","remaining_count":"10.00","ts_ms":1715793600123}
```

### POST `/portfolio/events/orders/batched` — Batch Create. 201. **Cost: N x 10 tokens (billed per item).**
Body `{orders: [CreateOrderV2Request...]}` (same per-order fields). Max batch size "scales with your tier's write budget". Response `{orders:[{order_id, client_order_id, fill_count, remaining_count, average_fill_price, average_fee_paid, ts_ms, error}]}` — per-order `error` object (`code`,`message`,`details`,`service`) nullable; **batch returns 201 even when individual orders fail** — check every element.

### DELETE `/portfolio/events/orders/{order_id}` — Cancel (V2). **Cost: 2 tokens.**
Query: `subaccount` (default 0), `exchange_index` (default 0), `market_ticker` (required when exchange_index=-1).
Response: `order_id`, `client_order_id`, `reduced_by` (FixedPointCount = contracts actually canceled), `ts_ms`.

### DELETE `/portfolio/events/orders/batched` — Batch Cancel. **Cost: N x 2 tokens.**
Body `{orders:[{order_id (req), subaccount (opt 0–63), exchange_index (opt), market_ticker (req if exchange_index=-1)}]}`. Response items: `order_id`, `client_order_id`, `reduced_by` (**"0.00" if that cancel errored**), `ts_ms`, `error` (nullable).

### POST `/portfolio/events/orders/{order_id}/amend` — Amend (V2). 200.
Query: `subaccount`. Body: `ticker` (req), `side` (req bid|ask), `price` (req), `count` (req), `client_order_id` (opt, original), `updated_client_order_id` (opt, new), `exchange_index` (opt).
Response: `order_id`, `client_order_id`, `remaining_count`/`fill_count` (present only if fills occurred or resting size changed), `average_fill_price`, `average_fee_paid`, `ts_ms`.
**Queue priority: "Amending a resting order preserves queue position only when the amendment decreases size. All other amendments — like increasing size or changing price — forfeit queue position and place the order at the back of the queue."** `count` is the new total, and an amend CAN cause immediate fills (crossing).

### POST `/portfolio/events/orders/{order_id}/decrease` — Decrease (V2). 200.
Body: exactly one of `reduce_by` OR `reduce_to` (FixedPointCount); `exchange_index` (opt, only 0). Response: `order_id`, `client_order_id`, `remaining_count`, `ts_ms`.

### GET `/portfolio/orders` — Get Orders (auth; read path NOT deprecated)
Query: `ticker`, `event_ticker` (comma-separated, **max 10**), `min_ts`, `max_ts`, `status`, `limit` (100, 1–1000), `cursor`, `subaccount` (omit = all).
Order object: `order_id`, `user_id`, `client_order_id`, `ticker`, `side` (yes|no DEPRECATED), `action` (buy|sell DEPRECATED), `outcome_side` (yes|no), `book_side` (bid|ask), `type` (`limit`|`market`), `status` (**enum `resting`|`canceled`|`executed`**), `yes_price_dollars`, `no_price_dollars`, `fill_count_fp`, `remaining_count_fp`, `initial_count_fp`, `taker_fees_dollars`, `maker_fees_dollars`, `taker_fill_cost_dollars`, `maker_fill_cost_dollars`; optional: `expiration_time`, `created_time`, `last_update_time`, `self_trade_prevention_type`, `order_group_id`, `cancel_order_on_pause`, `subaccount_number`, `exchange_index`.
Orders canceled/executed before historical cutoff only via `GET /historical/orders` (resting orders unaffected).

### GET `/portfolio/orders/{order_id}/queue_position` (auth)
Response: `queue_position_fp` (FixedPointCount string) = "The number of preceding shares before the order in the queue" (0-indexed, price-time priority). Batch variant exists: `get-queue-positions-for-orders`.

## 4. Order Groups (MM kill-switch — this is Kalshi's native risk control)
Concept: "automatic order cancellation when a contracts limit is reached within a **rolling 15-second window**" of FILLED contracts. When triggered (rolling fill volume > limit, OR manual Trigger, OR limit lowered below current rolling volume): **all resting orders in the group are canceled AND no new orders can be placed into the group until Reset**.
- `POST /portfolio/order_groups/create` (note nonstandard `/create` suffix). Body: `subaccount` (opt, default 0), `contracts_limit` (int64, min 1; whole contracts) OR `contracts_limit_fp` (string 2dp) — one required, must match if both; `exchange_index` (opt). Response 201: `order_group_id` (string), `subaccount`, `exchange_index`. Limit range 1–1,000,000.
- Other endpoints: `GET /portfolio/order_groups` (+`{id}`), Delete (removes group and cancels all its resting orders), Reset (clears triggered state and rolling counter), Trigger (immediate cancel-all regardless of limit), Update limit (added 2026-01-29). Read endpoints accept optional `subaccount` query.
- Attach orders via `order_group_id` on Create Order V2. Order group updates stream on WS channel `order_group_updates` (added 2026-01-22).

## 5. Market / Event / Series metadata (all public, no auth)

### GET `/markets/{ticker}` and GET `/markets`
GET `/markets` query: `limit` (default 100, max 1000), `cursor`, `event_ticker` (single), `series_ticker` (**requires `mve_filter=exclude`**), `status` (**enum `unopened`|`open`|`paused`|`closed`|`settled` — one at a time; NOTE this filter vocabulary differs from Market.status enum**), `tickers` (comma), `min_created_ts`/`max_created_ts` (only with unopened/open/empty status), `min_close_ts`/`max_close_ts` (only with closed/empty), `min_settled_ts`/`max_settled_ts` (only with settled/empty), `min_updated_ts` (non-trading changes; only with `mve_filter=exclude`+`series_ticker`), `mve_filter` (enum `only`|`exclude`) — use `mve_filter=only` to enumerate combo markets.
Market object (required): `ticker`, `event_ticker`, `market_type` (enum `binary`|`scalar`), `yes_sub_title`, `no_sub_title`, `created_time`, `updated_time`, `open_time`, `close_time`, `latest_expiration_time`, `settlement_timer_seconds` (int), `status` (**enum `initialized`|`inactive`|`active`|`closed`|`determined`|`disputed`|`amended`|`finalized`**), `notional_value_dollars`, `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`, `last_price_dollars`, `previous_yes_bid_dollars`, `previous_yes_ask_dollars`, `previous_price_dollars`, `volume_fp`, `volume_24h_fp`, `liquidity_dollars` (**DEPRECATED, returns 0**), `open_interest_fp`, `result` (enum `yes`|`no`|`scalar`|`''`), `can_close_early` (bool), `expiration_value`, `rules_primary`, `rules_secondary`, `price_level_structure` (string), `price_ranges` (array of `{start,end,step}` strings — **use this for tick size; `tick_size` field was removed 2026-05-07**).
Optional: `expected_expiration_time`, `settlement_value_dollars`, `settlement_ts`, `occurrence_datetime`, `fee_waiver_expiration_time`, `early_close_condition`, `strike_type` (enum `greater`|`greater_or_equal`|`less`|`less_or_equal`|`between`|`functional`|`custom`|`structured`), `floor_strike` (double), `cap_strike`, `functional_strike`, `custom_strike` (object), **`mve_collection_ticker` (string), `mve_selected_legs` (array[MveSelectedLeg])** — how a combo market points back to its collection + legs, `primary_participant_key`, `is_provisional` (bool), `exchange_index`.
Deprecated market fields: `title`, `subtitle`, `expiration_time`, `liquidity_dollars`; `response_price_units` and `fractional_trading_enabled` removed 2026-07-09.

### GET `/events` and GET `/events/{event_ticker}`
GET `/events` query: `limit` (default 200, max 200 — different from markets!), `cursor`, `with_nested_markets` (bool, default false), `with_milestones` (bool), `status` (`unopened`|`open`|`closed`|`settled`), `series_ticker`, `tickers` (comma, added 2026-06-18), `min_close_ts`, `min_updated_ts`. **"This endpoint excludes multivariate events"** — combos live ONLY at `GET /events/multivariate`.
GET `/events/{event_ticker}` query: `with_nested_markets`.
EventData: `event_ticker`, `series_ticker`, `title`, `sub_title`, `collateral_return_type` (e.g. 'binary'), `mutually_exclusive` (bool), `available_on_brokers` (bool), `settlement_sources[] {name,url}`, `category` (DEPRECATED), `strike_date` (nullable, mutually exclusive with `strike_period`), `strike_period` (e.g. 'week','month'), `markets[]` (only when with_nested_markets=true), `product_metadata` (nullable object), `last_updated_ts`, `fee_type_override` (nullable), `fee_multiplier_override` (nullable double), `exchange_index`.

### GET `/events/multivariate`
Query: `limit` (default 100, min 1, **max 200**), `cursor`, `series_ticker`, `collection_ticker` (**cannot combine with series_ticker**), `with_nested_markets` (default false). Response `{events: EventData[], cursor}` — same EventData shape; nested Markets carry `mve_collection_ticker` + `mve_selected_legs`. These are "dynamically created events from multivariate event collections."

### GET `/events/{event_ticker}/metadata`
Response: `image_url` (req), `featured_image_url` (opt), `market_details[] {market_ticker, image_url, color_code}` (req), `settlement_sources[] {name,url}` (req), `competition` (opt nullable), `competition_scope` (opt nullable). Useful for sports-league classification of combo legs.

### GET `/series/{series_ticker}` and GET `/series`
`/series/{series_ticker}` query: `include_volume` (bool default false). `/series` query: `category`, `tags`, `include_product_metadata` (default false), `include_volume` (default false), `min_updated_ts` ("Use this to efficiently poll for changes").
Series: `ticker`, `frequency`, `title`, `category`, `tags` (string[] nullable), `settlement_sources[]`, `contract_url`, `contract_terms_url`, **`fee_type` (enum `quadratic`|`quadratic_with_maker_fees`|`flat`)**, **`fee_multiplier` (double)**, `additional_prohibitions` (string[]), `product_metadata` (nullable), `volume_fp` (opt), `last_updated_ts` (opt). This is where per-series fee schedule lives — combos fee math must read `fee_type`+`fee_multiplier` from series, then apply event overrides.

### GET `/markets/trades`
Query: `limit` (100, max 1000), `cursor`, `ticker`, `min_ts`, `max_ts`, `is_block_trade` (bool; omit = include both). Trade: `trade_id`, `ticker`, `count_fp`, `yes_price_dollars`, `no_price_dollars`, `taker_side` (yes|no DEPRECATED), `taker_outcome_side` (yes|no), `taker_book_side` (bid|ask), `created_time` (ISO8601), `is_block_trade` (bool, true = off-book block trades; RFQ fills appear as regular trades per our own measurements). Trades older than cutoff → `GET /historical/trades`.

## 6. Structured Targets & Search
- GET `/structured_targets` (public): query `ids` (repeatable, max 2000), `type` (e.g. `basketball_player`), `competition` ("Matches against the league, conference, division, or tour", e.g. `NBA`), `page_size` (1–2000, default 100), `cursor`. StructuredTarget: `id`, `name`, `type`, `details` (flexible type-specific object), `source_id` (opt), `source_ids` (object), `last_updated_ts`. Also GET `/structured_targets/{id}` (not fetched; same object). Useful for mapping combo legs to teams/players/leagues.
- GET `/search/filters_by_sport` (public, no params): `filters_by_sports` (object sport→details incl. `scopes[]` and `competitions` map), `sport_ordering` (array). Handy for the sports-only whitelist.

## 7. Fee-change endpoints (public)
- GET `/events/fee_changes`: query `event_ticker`, `limit` (100, 1–1000), `cursor`. EventFeeChange: `id`, `event_ticker`, `series_ticker`, `fee_type_override` (enum quadratic|quadratic_with_maker_fees|flat OR **null = override cleared, falls back to series**), `fee_multiplier_override` (double or null), `scheduled_ts` (date-time when it takes effect). "Event fees are an override layered on top of the parent series' fee structure."
- GET `/series/fee_changes`: query `series_ticker`, `show_historical` (bool default false). Items in `series_fee_change_arr`: `id`, `series_ticker`, `fee_type`, `fee_multiplier`, `scheduled_ts`.
- WS: `market_lifecycle_v2` channel emits `event_fee_update` messages: `msg.event_ticker`, `msg.fee_type_override` (enum|null), `msg.fee_multiplier_override` (number|null). A live MM must consume these — fees can change mid-flight on scheduled timestamps.

## 8. Account API limits / tiers (auth unless noted)
- GET `/account/limits`: `usage_tier` (string; values seen: basic, advanced, expert, premier, paragon, prime, prestige), `read` and `write` BucketLimit `{refill_rate (tokens/sec), bucket_capacity}`, `grants[] {exchange_instance (enum event_contract|margined), level, expires_ts (int64 nullable), source ("volume"|"manual")}`. Token model: each request deducts its endpoint cost; 429 when bucket empty; "Non-Basic write buckets hold approximately two seconds of budget" (burst).
- GET `/account/endpoint_costs` (PUBLIC): `default_cost` (int, "currently 10"), `endpoint_costs[] {method, path, cost}` — only non-default entries listed. Poll this to build the cost table (known: create order 10, cancel 2, batch billed per item, Get/Create/Delete Quote 2 each, upgrade endpoint 30).
- GET `/account/api_usage_level/volume_progress`: `volume_progress[]` with `computed_ts` (int64 s), `trailing_30d_volume_fp` (string 2dp), `goals[] {level, earn_volume_goal_fp, keep_volume_goal_fp}`. Cron-computed, predictions (event_contract) lane. Premier+ earnable automatically from volume (2026-06-11); qualification thresholds halved 2026-06-25.
- POST `/account/api_usage_level/upgrade`: no body; grants permanent **Advanced** level; requires ≥1 of your last 100 Predictions orders created via API (else 403 "No API-created order was found in the user's latest 100 Predictions orders"); costs 30 tokens (write bucket); 201 on success.

## 9. Exchange schedule / freshness / maintenance
- GET `/exchange/schedule` (public): `schedule.standard_hours[]` (WeeklySchedule: `start_time`, `end_time`, plus `monday`..`sunday` arrays of DailySchedule `{open_time, close_time}` in **HH:MM ET**; multiple sessions per day possible), `schedule.maintenance_windows[] {start_datetime, end_datetime}`.
- GET `/exchange/user_data_timestamp` (public): `as_of_time` (date-time) — when user data (GetBalance/GetOrder(s)/GetFills/GetPositions) was last validated; REST portfolio state can lag the matching engine; combine with WS for current state.
- Maintenance: **every Thursday 3:00–5:00 AM ET**. During a *trading pause*: place/amend = NO, cancel = YES. During an *exchange pause* (rare): place/amend/cancel all blocked. Resting orders persist through both unless `cancel_order_on_pause=true` (REST) / FIX tag 21006. Expect session disconnects; reconnect after 5:00 AM ET.

## 10. Lifecycle / settlement semantics (getting_started)
- Statuses: `initialized → active` at `open_time` (implicit, NO WebSocket event); `active/inactive → closed` at `close_time` (implicit); explicit WS events: `deactivated`/`activated` (pause/unpause), `close_date_updated` (+`activated` if reopened), `determined`, `settled` (→ finalized). Statuses `disputed`, `amended` also exist.
- **Only `active` markets accept orders. Once `closed`, ALL order operations including cancels are rejected with `MARKET_INACTIVE`.**
- **Pause reactivation: "All resting orders are cancelled on this reactivation"** — after an unpause you must re-quote from scratch.
- `close_time` may move EARLIER only if `can_close_early=true` (sports combos: expect true).
- Settlement: winners get $1/contract on net positions; zero settlement fees for simple yes/no; payouts rounded to whole cents (`CollateralAmountChange + MiscFeeAmt` = pre-rounding value).
- Pagination: cursor-based everywhere; keep requesting with `cursor` until it comes back empty/null.
- Historical data: live window target **3 months**. GET `/historical/cutoff` returns `market_settled_ts`, `trades_created_ts`, `orders_updated_ts` boundaries. Older settled markets/trades/fills/completed-orders only via `/historical/*` endpoints.

## 11. Order direction (the big trap page)
- `outcome_side` (yes|no) = directional exposure; `book_side` (bid|ask) = book vocabulary; **`bid ≡ yes`, `ask ≡ no`, always.**
- Legacy mapping: buy+yes → outcome_side=yes/book_side=bid; sell+no → yes/bid; buy+no → no/ask; sell+yes → no/ask.
- Legacy fields per object: `action`,`side` (Order, Fill REST); `is_yes` (WS user_orders); `purchased_side` (WS fill); `taker_side` (Trade REST+WS → `taker_outcome_side`/`taker_book_side`). Removal "not before May 28, 2026" (other pages say May 14, 2026).
- **Unified price scale: "An order at price p with outcome_side=no is matched by an order at the same price p with outcome_side=yes."** Prices are on the yes-scale in the single book; direction does not change the price value.
- Orderbook WS channels exception: no-side defaults to no-leg (inverted) pricing unless `use_yes_price: true` is set on subscribe; Kalshi plans to flip the default to true in a future release — set it explicitly.

## 12. WebSocket channels (Trade API; authed connection at handshake)
- **`fill`** channel — message `type:"fill"`, `sid`, `msg`: `trade_id`, `order_id`, `market_ticker`, `is_taker` (bool), `side` (yes|no legacy), `yes_price_dollars`, `count_fp`, `fee_cost` (fixed-point dollars), `action` (legacy), `ts` (s, DEPRECATED), `ts_ms` (ms), **`post_position_fp` (your position AFTER the fill — free position reconciliation)**, `purchased_side` (legacy), `outcome_side`, `book_side`, `client_order_id` (opt), `subaccount` (opt int). Filter by `market_ticker`/`market_tickers`; supports `update_subscription` with `add_markets`/`delete_markets`.
- **`user_orders`** channel — message `type:"user_order"`, `msg`: `order_id`, `user_id`, `ticker`, `status` (resting|canceled|executed), `side` (legacy), `is_yes` (DEPRECATED, removal ≥2026-05-14), `outcome_side`, `book_side`, `yes_price_dollars`, `fill_count_fp`, `remaining_count_fp`, `initial_count_fp`, `taker_fill_cost_dollars`, `maker_fill_cost_dollars`, `taker_fees_dollars`, `maker_fees_dollars`, `client_order_id`, `order_group_id`, `self_trade_prevention_type`, `created_time` (DEPRECATED → `created_ts_ms`), `last_update_time` (DEPRECATED → `last_updated_ts_ms`), `expiration_time` (DEPRECATED → `expiration_ts_ms`), `subaccount_number`. Optional `market_tickers` filter (omit = all).
- **`market_positions`** channel — `type:"market_position"`, `msg`: `user_id`, `market_ticker`, `position_fp` (signed), `position_cost_dollars`, `realized_pnl_dollars`, `fees_paid_dollars`, `position_fee_cost_dollars`, `volume_fp`, `subaccount` (opt). Triggered by trades, settlements, position changes.
- **`market_lifecycle_v2`** channel — events `created`|`activated`|`deactivated`|`close_date_updated`|`determined`|`settled`|`price_level_structure_updated`|`metadata_updated`; conditional fields `open_ts`/`close_ts` (Unix s), `result`, `determination_ts`, `settlement_value`, `settled_ts`, `is_deactivated`, `price_level_structure` (enum `linear_cent`|`deci_cent`|`tapered_deci_cent`), `price_ranges[]{start,end,step}`, strike fields, `additional_metadata` on created (incl. `can_close_early`, `event_ticker`, `expected_expiration_ts`, rules). Also carries `event_lifecycle` messages (`collateral_return_type` enum `MECNET`|`DIRECNET`|`''`) and `event_fee_update` messages. **No `market_ticker` filters supported.** **KXMVE-prefixed (multivariate) tickers are EXCLUDED from this channel since 2026-02-12.**
- **`multivariate_market_lifecycle`** channel (added 2026-03-19) — dedicated MVE lifecycle: same msg structure as market_lifecycle_v2 (example shows outer `type` as `"multivariate_market_lifecycle"` while schema says const `"market_lifecycle_v2"` — verify on demo). Receives ALL multivariate lifecycle notifications; no ticker filters; "Only emits lifecycle updates for multivariate events." This is how you learn a new combo market was created/determined without polling.
- **`multivariate_lookup`** channel — DEPRECATED, predates RFQs, "should not be used for new integrations"; replacement = RFQ system. Msg had `collection_ticker`, `event_ticker`, `market_ticker`, `selected_markets[]{event_ticker, market_ticker, side}`.
- Orderbook WS: `get_snapshot` action on `orderbook_delta` (2026-04-20); sanity limits: **max 500k market subscriptions per session, max 10k commands/sec** (2026-06-18). `ts_ms` added to non-margin WS messages (2026-04-15).
- `order_group_updates` channel exists (2026-01-22).

## 13. Incentive programs
GET `/incentive_programs` (public): query `status` (all|active|upcoming|closed|paid_out, default all), `type` (all|liquidity|volume, default all), `incentive_description` (exact match), `limit` (1–10000, default 100), `cursor`. IncentiveProgram: `id`, `market_id`, `market_ticker`, `incentive_type` (liquidity|volume), `incentive_description`, `start_date`, `end_date`, `period_reward` (**integer, centi-cents**), `paid_out` (bool), `discount_factor_bps` (int nullable), `target_size_fp` (nullable). Response wrapper uses `incentive_programs` + **`next_cursor`** (not `cursor`). Per-market liquidity rewards — worth polling to bias which combos to quote.

## 14. Changelog highlights an RFQ MM must know (2026)
- 2026-04-23: token-cost rate-limit model replaced per-second scheme; write bursts allowed.
- 2026-04-22: V2 event-order endpoints live at `/portfolio/events/orders`.
- 2026-05-25: legacy `/portfolio/orders` mutation costs INCREASED; 2026-06-18 formally deprecated.
- 2026-06-11: **RFQs support fractional quantities (0.01 increments)**.
- 2026-06-19/25: **closed RFQs and cancelled quotes retained only 7 days**; **"RFQ quotes are only durably queryable after acceptance"** (accepted/confirmed/executed states) — log everything yourself at create time.
- 2026-06-20: `GET /communications/quotes` **no longer supports `market_ticker` or `event_ticker` params**; 2026-06-18: it gained `min_ts`/`max_ts` + cursor pagination fix; 2026-05-01: `user_filter=self`; 2026-05-07: `rfq_user_filter`.
- 2026-06-23: Get Quote costs 2 tokens (matches create/delete quote).
- 2026-05-05: **quotes accept `post_only`**.
- 2026-03-11: quotes communicate computed yes/no contract counts.
- 2026-06-30: API key scope `write::trade` covers order, order-group, AND RFQ/quote write endpoints.
- 2026-02-02 / 2026-01-22 / 2026-03-26: subaccount support on RFQs, CreateQuote, and quote accepted/executed responses.
- 2026-03-12: legacy integer count/price fields REMOVED (fixed-point strings only); 2026-02-12: `type=market` removed from order creation.
- 2026-05-07: `tick_size` removed from Market — use `price_level_structure`/`price_ranges`.
- 2026-07-09 (upcoming): removal of `Market.response_price_units`, `Market.fractional_trading_enabled`, `MarketPosition.resting_orders_count`.
- 2026-07-04: `GET /exchange/announcements` removed.
- 2026-01-26: more specific order-validation error codes replaced generic `invalid_order`.
- FIX has a full RFQ flow too (`/fix/rfq-messages.md`, post-only via `ExecInst=6`, quoter identity via PartyRole) — alternative path if REST latency is an issue.

## 15. Traps summary
1. **Two different status vocabularies**: GET /markets filter uses `unopened|open|paused|closed|settled`; Market.status field uses `initialized|inactive|active|closed|determined|disputed|amended|finalized`. Map them explicitly.
2. **V2 orders use `bid`/`ask`, not yes/no + buy/sell**; single price on unified yes-scale; bid≡yes, ask≡no always.
3. All counts/prices are **strings** (fixed-point), except `balance`/`portfolio_value`/`revenue`/`value` (cents ints) and `period_reward` (centi-cents int). Don't mix units.
4. `KALSHI-ACCESS-TIMESTAMP` is **ms**; order `expiration_time` is **seconds**; WS `ts` seconds vs `ts_ms` ms; lifecycle `*_ts` seconds.
5. Batch create returns 201 with per-order `error` objects — success of the HTTP call means nothing per-order.
6. After `closed`, even cancels get `MARKET_INACTIVE`; after unpause, all resting orders are auto-canceled.
7. `GET /events` silently excludes multivariate events; `series_ticker` on `GET /markets` requires `mve_filter=exclude`; KXMVE excluded from `market_lifecycle_v2` WS.
8. Fill objects no longer carry `client_order_id` (removed 2026-03-30).
9. `/portfolio/summary/total_resting_order_value` is FCM-only despite the tempting name.
10. Amend forfeits queue priority unless purely decreasing size; amend can cross and fill immediately.
11. Fills/orders/settlements: omitted `subaccount` = ALL subaccounts; balance/positions default to subaccount 0.
12. RFQ/quote objects evaporate: 7-day retention, quotes durably queryable only post-acceptance — persist locally.

## Critical facts (must get right)
- V2 order endpoints are POST/DELETE on /trade-api/v2/portfolio/events/orders (single: POST /portfolio/events/orders, cancel: DELETE /portfolio/events/orders/{order_id}, batch: POST|DELETE /portfolio/events/orders/batched, amend: POST .../{order_id}/amend, decrease: POST .../{order_id}/decrease); legacy /portfolio/orders mutations are deprecated and cost more tokens
- V2 orders use side=bid|ask (bid≡yes, ask≡no, always) with a single `price` string on the unified yes-scale; required fields: ticker, side, count, price, time_in_force (fill_or_kill|good_till_canceled|immediate_or_cancel), self_trade_prevention_type (taker_at_cross|maker); type=market no longer exists
- All quantities/prices are fixed-point STRINGS: FixedPointCount = 2 decimals min-granularity 0.01 (e.g. "10.00"), FixedPointDollars = up to 6 decimals (e.g. "0.5600"); exceptions: GET /portfolio/balance `balance`/`portfolio_value` are int cents, settlement `revenue`/`value` are int cents, incentive `period_reward` is centi-cents
- Rate limits are token buckets (separate read/write) with per-endpoint costs: default 10, create order 10, cancel 2, batch billed per item (N×10 create, N×2 cancel), quote create/get/delete 2 each; poll public GET /account/endpoint_costs for the live table and GET /account/limits (auth) for your refill_rate/bucket_capacity
- KALSHI-ACCESS-TIMESTAMP auth header is in MILLISECONDS, but order expiration_time is Unix SECONDS, and lifecycle WS timestamps (open_ts/close_ts) are SECONDS while ts_ms fields are milliseconds
- Only status=active markets accept orders; once closed, ALL operations including cancels are rejected with MARKET_INACTIVE; after a pause is lifted, all resting orders are automatically canceled and must be re-placed
- Multivariate (combo) events are EXCLUDED from GET /events — use GET /events/multivariate (max limit 200; collection_ticker XOR series_ticker); combo markets are excluded from the market_lifecycle_v2 WS channel — use the multivariate_market_lifecycle channel (no ticker filters); combo Market objects carry mve_collection_ticker and mve_selected_legs
- Fee schedule = Series.fee_type (quadratic|quadratic_with_maker_fees|flat) × Series.fee_multiplier, with per-event overrides (fee_type_override/fee_multiplier_override, null = cleared) discoverable via GET /events/fee_changes, GET /series/fee_changes (scheduled_ts = effective time) and the event_fee_update WS message
- RFQ/quote data is ephemeral: closed RFQs and cancelled quotes retained only 7 days, and RFQ quotes are durably queryable ONLY after acceptance; GET /communications/quotes no longer supports market_ticker/event_ticker filters (use min_ts/max_ts + user filters); persist all RFQ/quote state locally at creation time
- Batch create returns HTTP 201 even when individual orders fail — every element of response.orders[] must be checked for a non-null `error` object
- Amending an order preserves queue priority ONLY when it purely decreases size; price changes or size increases send it to the back of the queue (and an amend can cross and fill immediately); use POST .../decrease or the amend-with-smaller-count to shrink safely
- Order groups are the native kill-switch: contracts_limit (1–1,000,000) measured over a ROLLING 15-SECOND window of fills; on trigger, all group orders cancel and new placement is blocked until Reset; attach orders via order_group_id on create
- Fill WS channel is named `fill` and includes post_position_fp (position after the fill), fee_cost, is_taker, outcome_side/book_side, ts_ms — use it as the source of truth for fills; Fill REST objects do NOT contain client_order_id (removed 2026-03-30)
- Two distinct status vocabularies: GET /markets filter takes unopened|open|paused|closed|settled while Market.status returns initialized|inactive|active|closed|determined|disputed|amended|finalized — never conflate them
- Tick size must come from Market.price_level_structure + price_ranges[{start,end,step}] (tick_size field was removed 2026-05-07); price_level_structure enum: linear_cent, deci_cent, tapered_deci_cent
- Subaccounts: 0=primary, 1–63 numbered, created via POST /portfolio/subaccounts (requires advanced/MM access); omitting `subaccount` on fills/orders/settlements returns ALL subaccounts, while balance/positions default to 0; transfers require idempotent client_transfer_id (replay → 409)
- Maintenance window every Thursday 3:00–5:00 AM ET: no place/amend (cancels still work in trading pauses); resting orders persist unless cancel_order_on_pause=true was set at order creation; expect WS disconnects
- Live-data window is ~3 months: older settled markets, trades, fills, and completed orders only exist on GET /historical/* endpoints with cutoffs from GET /historical/cutoff
- Legacy direction fields (side, action, is_yes, purchased_side, taker_side) are deprecated in favor of outcome_side/book_side (taker_outcome_side/taker_book_side on trades); removal no earlier than May 14–28, 2026 (already past — assume they can vanish); legacy integer count/price fields were already removed 2026-03-12
- No documentation of HVM (high-volatility market) rules exists anywhere in the docs index — do not design around assumed HVM mechanics without empirical confirmation

## Open questions (verify empirically on demo)
- MveSelectedLeg object fields are never defined in any fetched page (get-market lists mve_selected_legs: array[MveSelectedLeg] without a schema) — pull openapi.yaml or hit a live KXMVE market on demo to learn exact leg field names (event_ticker/market_ticker/side?)
- HVM (high volatility market) rules: zero mentions in the entire docs index, glossary, or changelog — does the concept exist at the API level at all (e.g., forced pauses, wider bands), or only in the exchange rulebook? Needs empirical/legal-doc verification
- WebSocket connection URL ambiguity: channel pages show wss://external-api-ws.kalshi.com with per-channel 'addresses' (e.g. /fill), but the classic API uses a single WS endpoint with subscribe commands listing channels — verify actual connect URL + subscribe frame on demo (base websockets.md is in the other agent's set)
- Does Decrease Order (V2) preserve queue priority? Strongly implied by the amend rule ('preserves only when decreasing size') but never stated for the decrease endpoint itself
- self_trade_prevention_type semantics: what exactly happens under taker_at_cross vs maker when your quote would cross your own resting order (cancel which side? partial?) — not defined on any fetched page
- Subaccount creation eligibility conflict: endpoint page says 'only institutions and market makers'; changelog 2026-05-12 says 'all direct members with advanced API access' — test POST /portfolio/subaccounts on demo
- What does subaccount netting enabled/disabled actually change (position netting across yes/no? settlement behavior?) — GET/PUT /portfolio/subaccounts/netting exists but mechanics are undocumented
- GET /markets default behavior when mve_filter is omitted: are KXMVE combo markets included, excluded, or mixed in? Affects any market-scan loop
- Exact numeric rate limits per tier (refill_rate/bucket_capacity for advanced/expert/premier...) are not published in docs — must read GET /account/limits for our own account on both demo and prod, plus GET /account/endpoint_costs for the full non-default cost table (are RFQ create / quote confirm / accept non-default?)
- outer `type` field on the multivariate_market_lifecycle channel: schema says const 'market_lifecycle_v2' but the example shows 'multivariate_market_lifecycle' — confirm which string actually arrives before writing the parser
- Do combo (KXMVE) events emit event_fee_update messages, and on which channel, given KXMVE is excluded from market_lifecycle_v2?
- Maximum number of concurrent order groups per account/subaccount is not documented
- Legacy field removal dates disagree (fills/orders pages say 'not before May 14, 2026'; order_direction says 'not before May 28, 2026') and both dates have passed as of 2026-07-05 — verify empirically whether side/action/is_yes still arrive and never depend on them
- Fill WS example shows yes_price_dollars '0.750' (3 decimals) vs stated 4–6 decimal conventions elsewhere — confirm decimal emission per field on demo before strict-parsing
- V2 cancel/amend 'response correctness' changelog entry (2026-05-21) is ambiguous about when the response fails to describe the affected order — test canceling a partially-filled order on demo and compare reduced_by against WS state
- Whether GET /portfolio/positions supports min/max_ts or settlement filtering for combos, and whether event_positions aggregate across an MVE event's markets the same way as regular events
- Incentive period_reward units stated as centi-cents — confirm (100 centi-cents = 1 cent?) before using in EV math
