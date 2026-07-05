# Kalshi Multivariate (Combo) API — Implementation Notes

Sources fetched 2026-07-05 from docs.kalshi.com (`.md` doc mirrors). Two of the six requested URLs were stale:
- `get-multivariate-event-collection-lookup-history.md` → **404**. Replaced by `lookup-tickers-for-market-in-multivariate-event-collection.md` (itself DEPRECATED, see §4).
- `multivariate-market-&-event-lifecycle.md` → correct path is `multivariate-market-and-event-lifecycle.md`.

## 0. Base URLs / Environments

REST base URLs (all multivariate endpoints are under these):
- Production: `https://external-api.kalshi.com/trade-api/v2`
- Production (shared): `https://api.elections.kalshi.com/trade-api/v2`
- Demo: `https://external-api.demo.kalshi.co/trade-api/v2`
- Demo (shared): `https://demo-api.kalshi.co/trade-api/v2`

WebSocket host (prod): `wss://external-api-ws.kalshi.com` (demo WS host not stated on these pages — verify).

Auth headers (for authenticated endpoints): `KALSHI-ACCESS-KEY` (API key ID), `KALSHI-ACCESS-SIGNATURE` (RSA-PSS signature), `KALSHI-ACCESS-TIMESTAMP` (milliseconds).

---

## 1. GET /multivariate_event_collections — list collections

- **Method/path:** `GET /multivariate_event_collections`
- **Auth:** NONE required (public).
- **Query params:**
  - `status` — string enum: `unopened` | `open` | `closed` (optional)
  - `associated_event_ticker` — string; "Only return collections associated with a particular event ticker"
  - `series_ticker` — string; "Only return collections with a particular series ticker"
  - `limit` — integer, 1–200
  - `cursor` — string; "Pointer to the next page of records in the pagination"
- **200 response** (`GetMultivariateEventCollectionsResponse`):
  - `multivariate_contracts` — array of `MultivariateEventCollection` (NOTE the field name is `multivariate_contracts`, not `collections`)
  - `cursor` — string; empty = no next page
- **Errors:** 400 (invalid input), 500. Error body = `ErrorResponse` with fields `code`, `message`, `details`, `service`.

### MultivariateEventCollection object (all required)
| Field | Type | Notes |
|---|---|---|
| `collection_ticker` | string | unique ID |
| `series_ticker` | string | |
| `title` | string | |
| `description` | string | |
| `open_date` | string date-time | "Before this time, the collection cannot be interacted with" |
| `close_date` | string date-time | "After this time, the collection cannot be interacted with" |
| `associated_events` | array of `AssociatedEvent` | CURRENT way to enumerate eligible legs |
| `associated_event_tickers` | array of string | **DEPRECATED — use `associated_events`** |
| `is_ordered` | boolean | "If true, the order of markets passed into Lookup/Create affects the output" |
| `is_single_market_per_event` | boolean | **DEPRECATED** (whether multiple markets per event accepted) |
| `is_all_yes` | boolean | **DEPRECATED** (whether only 'yes' side allowed) — per-event `is_yes_only` replaces it |
| `size_min` | integer int32 | "Minimum number of markets that must be passed into Lookup/Create (inclusive)" |
| `size_max` | integer int32 | "Maximum number of markets that must be passed into Lookup/Create (inclusive)" |
| `functional_description` | string | "Functional description of the collection describing how inputs affect the output" — this is where payout function (e.g. product-of-legs) is described, free text |

### AssociatedEvent object
| Field | Type | Required | Notes |
|---|---|---|---|
| `ticker` | string | yes | event ticker |
| `is_yes_only` | boolean | yes | "Whether only the 'yes' side can be used for this event" |
| `size_max` | integer int32, nullable | no | max markets selectable from this event; null = no limit |
| `size_min` | integer int32, nullable | no | min markets from this event; null = no limit |
| `active_quoters` | array of strings | yes | "List of active quoters for this event" — lets you see maker competition per event |

---

## 2. GET /multivariate_event_collections/{collection_ticker} — single collection

- **Method/path:** `GET /multivariate_event_collections/{collection_ticker}`
- **Auth:** NONE required.
- **Path param:** `collection_ticker` (string, required). No query params, no body.
- **200 response** (`GetMultivariateEventCollectionResponse`): field `multivariate_contract` (singular) = one `MultivariateEventCollection` (same schema as §1).
- **Errors:** 400, 404, 500 (`ErrorResponse`).

---

## 3. POST /multivariate_event_collections/{collection_ticker} — CreateMarketInMultivariateEventCollection

- **Method/path:** `POST /multivariate_event_collections/{collection_ticker}` (yes — POST to the same path as the single-collection GET)
- **Auth:** REQUIRED (all three KALSHI-ACCESS-* headers).
- **Path param:** `collection_ticker` (string, required).
- **Request body** (JSON):
  - `selected_markets` — array of `TickerPair`, REQUIRED. Each TickerPair:
    - `market_ticker` — string, required
    - `event_ticker` — string, required
    - `side` — string enum `yes` | `no`, required
  - `with_market_payload` — boolean, optional: include full Market object in response
- **200 response:**
  - `event_ticker` — string, required — "Event ticker for created market"
  - `market_ticker` — string, required — "Market ticker for created market" (the combined/combo market ticker)
  - `market` — Market object, optional (only when `with_market_payload: true`)
- **Errors:** 400, 401, 429 (rate limit), 500.
- **Rate limit:** "Users are limited to 5000 creations per week."
- **Semantics:** "This endpoint must be hit at least once before trading or looking up a market." I.e., the combined market for a specific leg combination does not exist until someone calls this; it is idempotent in effect (returns the ticker for the combination). The docs do NOT document the ticker-derivation formula — the ONLY reliable way to get the combined `market_ticker` is from this response (deprecated WS example shows shape like `KXOSCARWINNERS-25C0CE5-36353`, i.e. collection-derived event ticker + numeric suffix; treat as opaque).
- Ordering matters when the collection has `is_ordered: true` — the order of `selected_markets` affects which market you get.
- Respect `size_min`/`size_max` (collection level) and per-event `size_min`/`size_max`/`is_yes_only` when building `selected_markets`, else expect 400.

---

## 4. PUT /multivariate_event_collections/{collection_ticker}/lookup — LookupTickersForMarket (DEPRECATED)

- **Method/path:** `PUT /multivariate_event_collections/{collection_ticker}/lookup`
- **Auth:** REQUIRED (KALSHI-ACCESS-* headers).
- **Deprecation:** "This endpoint predates RFQs and should not be used for new integrations."
- **Body:** `selected_markets` (array of TickerPair, same schema as §3: `market_ticker`, `event_ticker`, `side` enum `yes`/`no`).
- **200 response:** `event_ticker` (string), `market_ticker` (string) — for the looked-up combined market.
- **404 if the market was never created** via CreateMarketInMultivariateEventCollection.
- **Rate cost:** 2 tokens per request.
- **Errors:** 400, 401, 404, 500.
- Note: the originally requested "lookup-history" page no longer exists (404); this lookup endpoint is its successor page and is itself deprecated. Do not build on it.

---

## 5. GET /events/multivariate — list multivariate (combined) events

- **Method/path:** `GET /events/multivariate`
- **Auth:** none specified.
- **Query params:**
  - `limit` — integer, default 100, min 1, max 200
  - `cursor` — string, pagination cursor from previous response
  - `series_ticker` — string, "Filter by series ticker"
  - `collection_ticker` — string, "Filter events by collection ticker. Returns only multivariate events belonging to the specified collection. **Cannot be used together with series_ticker.**"
  - `with_nested_markets` — boolean, default false; when true each event includes a `markets` field (array of Market objects)
- **200 response** (`GetMultivariateEventsResponse`): `events` (array of EventData, required), `cursor` (string, required, empty = done).
- **EventData required fields:** `event_ticker`, `series_ticker`, `sub_title`, `title`, `collateral_return_type`, `mutually_exclusive` (boolean), `available_on_brokers` (boolean), `settlement_sources` (array, nullable).
- **EventData optional fields:** `category` (string, DEPRECATED), `strike_date` (date-time, nullable), `strike_period` (string, nullable), `markets` (array of Market, only when `with_nested_markets=true`), `product_metadata` (object, nullable), `last_updated_ts` (date-time), `fee_type_override` (string, nullable), `fee_multiplier_override` (number double, nullable), `exchange_index` (ExchangeIndex).
- **Errors:** 400, 401, 500.
- Use: this is how you enumerate already-created combined markets (e.g. after restart, to rediscover markets your bot or others created) — filter by `collection_ticker`, set `with_nested_markets=true`.

---

## 6. WebSocket: Multivariate Market & Event Lifecycle channel

- **Doc:** `websockets/multivariate-market-and-event-lifecycle.md`
- **Channel name:** `multivariate_market_lifecycle`
- **Host:** `external-api-ws.kalshi.com` (wss). "No additional channel-level authentication beyond the authenticated WebSocket connection" — the WS connection itself is authenticated.
- **No filtering:** "Receives all multivariate market lifecycle notifications (`market_ticker` filters are not supported)". "Only emits lifecycle updates for multivariate events."
- Subscribing to this one channel yields TWO message types:

### 6a. `multivariate_market_lifecycle` message (schema = market_lifecycle_v2)
```json
{
  "type": "multivariate_market_lifecycle",
  "sid": 14,
  "msg": {
    "event_type": "created|activated|deactivated|close_date_updated|determined|settled|price_level_structure_updated|metadata_updated",
    "market_ticker": "…",              // pattern ^[A-Z0-9-]+$
    "open_ts": 0,                        // integer, optional, on creation
    "close_ts": 0,                       // integer, optional, on creation or close_date_updated
    "result": "…",                     // string, optional, on determination
    "determination_ts": 0,               // integer, optional, on determination
    "settlement_value": "…",           // string, optional, on determination
    "settled_ts": 0,                     // integer, optional, on settlement
    "is_deactivated": false,             // boolean, optional, on pause/unpause
    "price_level_structure": "linear_cent|deci_cent|tapered_deci_cent",  // optional
    "price_ranges": [ { "start": "…", "end": "…", "step": "…" } ],  // strings
    "strike_type": "…", "floor_strike": 0, "cap_strike": 0, "custom_strike": {},  // metadata_updated
    "yes_sub_title": "…",
    "additional_metadata": {
      "name": "…", "title": "…", "yes_sub_title": "…", "no_sub_title": "…",
      "rules_primary": "…", "rules_secondary": "…",
      "can_close_early": false, "event_ticker": "…",
      "expected_expiration_ts": 0,
      "strike_type": "…", "floor_strike": 0, "cap_strike": 0, "custom_strike": {}
    }
  }
}
```
- `event_type` enum (8 values): `created`, `activated`, `deactivated`, `close_date_updated`, `determined`, `settled`, `price_level_structure_updated`, `metadata_updated`.
- `settlement_value` is a STRING and can be non-binary (scalar) — pairs with `price_level_structure` (`linear_cent` | `deci_cent` | `tapered_deci_cent`) and `price_ranges` (start/end/step as strings). Combo markets can therefore settle at intermediate values, not just 0/100.
- Timestamps here are integers (epoch), unlike REST date-time strings.

### 6b. `event_lifecycle` message (on the same channel, multivariate events only)
```json
{
  "type": "event_lifecycle",
  "sid": 5,
  "msg": {
    "event_ticker": "…",
    "title": "…",
    "subtitle": "…",
    "collateral_return_type": "MECNET|DIRECNET|''",
    "series_ticker": "…",
    "strike_date": 0,        // integer, optional
    "strike_period": "…"   // string, optional
  }
}
```
- Practical use for MM: watch for `created` events fired when ANY user's RFQ/create call mints a new combined market; this is how you learn about new combo markets without polling.

---

## 7. WebSocket: `multivariate` lookups channel — DEPRECATED

- **Doc:** `websockets/multivariate-lookups-deprecated.md`
- **Channel name:** `multivariate`. "Deprecated: this channel predates RFQs and should not be used for new integrations." Replacement = RFQs.
- No filters; global. Message type `multivariate_lookup`:
```json
{
  "type": "multivariate_lookup",
  "sid": 13,
  "msg": {
    "collection_ticker": "KXOSCARWINNERS-25",
    "event_ticker": "KXOSCARWINNERS-25C0CE5",
    "market_ticker": "KXOSCARWINNERS-25C0CE5-36353",
    "selected_markets": [
      { "event_ticker": "KXOSCARACTO-25", "market_ticker": "KXOSCARACTO-25-AB", "side": "yes" }
    ]
  }
}
```
- Fields: `collection_ticker` (string), `event_ticker` (string), `market_ticker` (string), `selected_markets` (array of {`event_ticker`, `market_ticker`, `side` enum `yes`/`no`}).
- This example is the only documented illustration of combined-ticker shape: `{collection_ticker minus suffix}C0CE5-36353` style — event ticker derived from collection, market ticker = event ticker + numeric id. Treat as opaque; do not parse for meaning.

---

## 8. RFQ interaction with multivariate (from getting_started/rfqs.md + communications endpoints)

- Guide: "Use Multivariate Event Collections to discover eligible combinations." "Combo RFQs include `mve_collection_ticker` and `mve_selected_legs`."
- **CreateRfq (`POST /communications/rfqs`) request body has NO mve_* fields** (verified: definitively absent). Body fields: `market_ticker` (string, REQUIRED — "The ticker of the market for which to create an RFQ"), `contracts` (integer, optional), `contracts_fp` (FixedPointCount string, 0.01-contract increments; must match `contracts` if both given), `target_cost_centi_cents` (int64, DEPRECATED), `target_cost_dollars` (FixedPointDollars string, up to 6 decimals), `rest_remainder` (boolean, REQUIRED), `replace_existing` (boolean, default false), `subtrader_id` (string, FCM members only), `subaccount` (integer, 0 primary / 1–63). Response 201: `{ "id": "string" }`. Max 100 open RFQs at a time. Errors 400/401/409/500.
  - ⇒ **The RFQ flow does NOT auto-create combo markets.** The requester (or someone) must call CreateMarketInMultivariateEventCollection first to obtain the combined `market_ticker`, then create the RFQ against that ticker. The `mve_collection_ticker`/`mve_selected_legs` fields are RESPONSE-side (read) fields on the RFQ object, populated by the exchange for combo markets.
- **RFQ object (GET /communications/rfqs/{id})** — fields relevant to combos: required `id`, `creator_id`, `contracts_fp`, `market_ticker`, `status` (enum `open` | `closed`), `created_ts` (date-time); optional `target_cost_dollars` (string), `rest_remainder` (boolean), `cancellation_reason`, `creator_user_id`, `creator_subaccount`, `cancelled_ts`, `updated_ts`, plus:
  - `mve_collection_ticker` — string, "Ticker of the MVE collection this market belongs to"
  - `mve_selected_legs` — array of objects: `event_ticker` (string), `market_ticker` (string), `side` (string), `yes_settlement_value_dollars` (string, NULLABLE). The nullable per-leg `yes_settlement_value_dollars` is the documented hook for scalar/product settlement: a leg's yes value can be a dollar amount (not just 0/1), and the collection's `functional_description` defines how leg values combine into the combined market's `settlement_value`.
- **Timing (HVM):** "The exchange designates certain markets as High Volatility Markets (HVM). **All combo markets are HVMs.**" Standard markets: confirmation window 30 s, execution timer 15 s. HVM (i.e., ALL combos): **confirmation window 3 s, execution timer 1 s**. After maker confirms, neither party can withdraw. "After the execution timeout, orders are placed on the public book."
- Maker quotes carry `yes_bid` and `no_bid` prices; requester selects a side; maker must confirm within the window.
- Settlement guide (getting_started/market_settlement.md): binary yes/no pays $1/contract; "Settlement fees are zero for simple yes/no determinations but may apply for sub-cent scalar settlement." No explicit multivariate product formula documented anywhere — only via `functional_description` free text per collection.

## Deprecated inventory (do not build on)
- REST: `PUT /multivariate_event_collections/{ticker}/lookup` (whole endpoint)
- WS channel: `multivariate` (multivariate_lookup messages)
- Fields: collection `associated_event_tickers`, `is_single_market_per_event`, `is_all_yes`; EventData `category`; CreateRfq `target_cost_centi_cents`
- Removed page: get-multivariate-event-collection-lookup-history.md (404)

## NEXT STEPS
- Implementers: build discovery on GET collections (+`associated_events`), creation on POST create-market, event enumeration on GET /events/multivariate, lifecycle on WS `multivariate_market_lifecycle`.
- Verify open questions on demo before wiring the quoting loop (see open_questions).
- Owner: kalshi-combos engineering; decision owed by user: none from this digest.

## Critical facts (must get right)
- Combo market creation is a prerequisite: 'This endpoint must be hit at least once before trading or looking up a market' — POST /multivariate_event_collections/{collection_ticker} (CreateMarketInMultivariateEventCollection) with body {selected_markets: [{market_ticker, event_ticker, side: 'yes'|'no'}], with_market_payload?} returns {event_ticker, market_ticker}; limit 5000 creations per week per user; 429 on breach.
- The RFQ flow does NOT auto-create combo markets: CreateRfq (POST /communications/rfqs) requires an existing market_ticker and has no mve_* request fields; mve_collection_ticker and mve_selected_legs appear only on the RFQ read object as exchange-populated response fields.
- ALL combo markets are High Volatility Markets: maker confirmation window is 3 seconds and execution timer is 1 second (vs 30 s / 15 s standard) — the market maker must confirm accepted quotes within 3 s or lose the fill.
- The list-collections 200 response field is named multivariate_contracts (array), and the single-collection response field is multivariate_contract (singular) — not 'collections'.
- Use associated_events (per-event ticker, is_yes_only, size_min/size_max nullable, active_quoters) — associated_event_tickers, is_single_market_per_event, and is_all_yes are all DEPRECATED collection fields.
- Leg selection constraints: collection-level size_min/size_max are inclusive counts of markets passed to Create/Lookup; per-event size_min/size_max apply per associated event; if is_ordered is true, the ORDER of selected_markets changes which output market you get.
- The combined market ticker derivation is undocumented and must be treated as opaque — the only supported ways to obtain it are the CreateMarket response, GET /events/multivariate?collection_ticker=...&with_nested_markets=true, or lifecycle WS 'created' events.
- PUT /multivariate_event_collections/{ticker}/lookup and the WS channel 'multivariate' (multivariate_lookup) are DEPRECATED ('predates RFQs, should not be used for new integrations'); lookup 404s if the market was never created; the originally cited lookup-history doc page no longer exists (404).
- WS channel 'multivariate_market_lifecycle' on external-api-ws.kalshi.com carries BOTH 'multivariate_market_lifecycle' messages (event_type enum: created, activated, deactivated, close_date_updated, determined, settled, price_level_structure_updated, metadata_updated) and 'event_lifecycle' messages; market_ticker filters are NOT supported (firehose only, multivariate-only).
- Scalar settlement is real for combos: lifecycle msg settlement_value is a STRING, price_level_structure enum is linear_cent|deci_cent|tapered_deci_cent with price_ranges [{start,end,step} as strings], RFQ legs carry nullable yes_settlement_value_dollars, and 'settlement fees ... may apply for sub-cent scalar settlement' — do not assume 0/100 binary payout; the payout function lives in the collection's free-text functional_description.
- GET /multivariate_event_collections and GET /multivariate_event_collections/{ticker} require NO auth; Create (POST) and Lookup (PUT) require KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE (RSA-PSS), KALSHI-ACCESS-TIMESTAMP (milliseconds).
- GET /events/multivariate: collection_ticker and series_ticker filters are mutually exclusive; limit default 100 max 200; with_nested_markets=true to embed Market objects; pagination via cursor (empty cursor = done).
- Collection interaction is time-gated by open_date/close_date ('Before this time, the collection cannot be interacted with' / 'After this time...'); filter listings with status enum unopened|open|closed.
- Max 100 open RFQs at a time; RFQ sizing uses contracts (int) or contracts_fp (string, 0.01 increments, must match if both) or target_cost_dollars (string, up to 6 decimals); target_cost_centi_cents is deprecated; rest_remainder is a REQUIRED boolean on CreateRfq.

## Open questions (verify empirically on demo)
- Is CreateMarketInMultivariateEventCollection idempotent for an already-existing combination (returns existing ticker with 200) or does it error — and does a repeat call count against the 5000/week creation limit?
- Who is expected to call CreateMarket in the RFQ flow in practice — does the Kalshi UI/requester side auto-create the market before the RFQ is broadcast (so makers never need to), or must a maker pre-create markets to quote proactively?
- Exact demo WebSocket host for the multivariate_market_lifecycle channel (docs only state external-api-ws.kalshi.com for prod) and the exact subscribe command params for this channel.
- Whether the 'multivariate_market_lifecycle' subscription's message schema label 'market_lifecycle_v2' means the wire type string is ever 'market_lifecycle_v2' vs always 'multivariate_market_lifecycle' — verify actual type field on demo.
- Units/format of the lifecycle integer timestamps (open_ts, close_ts, determination_ts, settled_ts, expected_expiration_ts): seconds vs milliseconds epoch.
- How functional_description encodes product-of-leg-values for sports combos (is it machine-parseable or free prose), and how yes_settlement_value_dollars per leg maps to the combined settlement_value — needs a live example from a settled combo on demo/prod.
- What price_level_structure do combo markets actually use (linear_cent vs deci_cent vs tapered_deci_cent) and what price_ranges look like in practice — affects quote price granularity.
- Semantics of active_quoters strings on AssociatedEvent (are these communications IDs of makers? how does one get listed?) — undocumented.
- Whether GET /multivariate_event_collections status filter values map to open_date/close_date or to a separate lifecycle state, and whether unopened collections are visible on demo.
- The exact 400 error codes/messages returned for size_min/size_max/is_yes_only violations in CreateMarket (needed for robust leg-validation error handling).
- Whether the 5000/week creation limit is per API key, per member, or per account, and what the reset boundary is (rolling 7 days vs calendar week).
- Confirm on demo that combo (HVM) confirmation window is exactly 3 s and execution timer 1 s as the guide states, and what happens to a maker quote if confirmation is missed (penalty? cooldown? Kalshi rate-limits bad quotes per prior project experience).
