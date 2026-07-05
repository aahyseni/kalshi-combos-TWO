# Kalshi RFQ / Quote API — Complete Maker-Side Digest

Sources fetched 2026-07-05: `docs.kalshi.com/getting_started/rfqs.md`, all 12 `/api-reference/communications/*` pages plus 3 extra endpoints found via `llms.txt` (`accept-rfq-quote`, `confirm-rfq-quote`, `delete-rfq-quote`), `websockets/communications.md`, `fix/rfq-messages.md`, `api-reference/portfolio/get-fills.md`, `api-reference/market/get-market.md` (price grid only). No URL 404'd.

---

## 1. Environments, Auth, Rate Limits

**REST base URLs (identical paths on all):**
- Prod: `https://external-api.kalshi.com/trade-api/v2`
- Prod (shared/legacy): `https://api.elections.kalshi.com/trade-api/v2`
- Demo: `https://external-api.demo.kalshi.co/trade-api/v2`
- Demo (shared): `https://demo-api.kalshi.co/trade-api/v2`

**WebSocket:** prod `wss://external-api-ws.kalshi.com`, channel `communications`. Demo WS URL is NOT documented on the communications page (verify empirically; likely the demo host analog).

**Auth headers (every endpoint below requires all three):**
- `KALSHI-ACCESS-KEY` — API key ID
- `KALSHI-ACCESS-SIGNATURE` — RSA-PSS signature of the request
- `KALSHI-ACCESS-TIMESTAMP` — request timestamp in **milliseconds**

**Rate limits:** documented as token costs — Create Quote: 2 tokens/request; Delete Quote (both path forms): 2 tokens; Get Quote: 2 tokens. Doc pointer: "See `GET /trade-api/v2/account/endpoint_costs` for current non-default endpoint costs." Other RFQ endpoints don't state a cost (assume default 1 token; verify).

**Standard error schema (all endpoints):**
```json
{ "code": "string", "message": "string", "details": "string (optional)", "service": "string (optional)" }
```

---

## 2. Lifecycle Overview (from getting_started/rfqs.md)

> "RFQs allow a requester to solicit quotes from market makers on a specific market and size. Execution follows a two-step lock: accept, then confirm."

1. **Requester creates RFQ** — market ticker + size (`contracts_fp` OR `target_cost_dollars`) + remainder handling.
2. **Makers quote** — each quote contains `yes_bid` and `no_bid`. "Either can be `\"0\"` to decline that side, but not both." Constraint: `yes_bid + no_bid ≤ $1`. Prices "must land on the market's price grid. Check `price_ranges` on `GET /markets/{ticker}` for the valid step size."
3. **Requester accepts** one quote, one side (`accepted_side: "yes"|"no"`).
4. **Maker confirms** within the confirmation window ("the endpoint will require the quoter to confirm"). Confirmation "will start a timer for order execution."
5. **Execution** — "At the end of the timer, orders are entered into the book." (i.e., orders hit the public orderbook after the execution timer, where they cross — see `post_only` note in §4.)

**Timing windows:**

| Market type | Confirmation window | Execution timer |
|---|---|---|
| Standard | 30 s | 15 s |
| HVM (High Volatility Markets) | **3 s** | **1 s** |

> "All combo markets qualify as HVMs." → **A combo market maker has 3 seconds from acceptance to confirm, and execution occurs 1 second after confirming.**

**Maker decline after accept:** There is NO explicit decline endpoint. FIX doc: "Quote must be confirmed within 30 seconds of acceptance or it will be voided" (30s = standard tier; combos = 3s). Voiding on window lapse is **automatic, not maker-initiated**. Whether `DELETE /communications/quotes/{quote_id}` succeeds while status=`accepted` is undocumented (see open questions).

**Quote replacement:** getting-started guide: "A new quote on the same RFQ replaces the maker's previous quote." FIX doc (slightly broader wording): "If a new Quote is created when an existing quote for the same market already exists for the user, the exchange will cancel the existing quote." Replacement is automatic — no explicit cancel needed before re-quoting.

**Fill tracking:** "Match fills via `creator_order_id` (maker) or `rfq_creator_order_id` (requester) in `GET /portfolio/fills`." These order IDs appear as private fields on the quote object after execution, and the WS `quote_executed` message carries `order_id` + `client_order_id` ("Use QuoteExecuted to correlate fill messages with quotes via client_order_id").

**Documented error conditions (guide):**
- `invalid_parameters` — invalid price / closed RFQ
- `RFQ_CLOSED` — RFQ was deleted, expired, or executed
- `INSUFFICIENT_BALANCE` — inadequate funds
- `409 Conflict` — existing open RFQ (on RFQ creation)

---

## 3. Accept-Side Economics (who buys what)

The REST accept body is `{"accepted_side": "yes"|"no"}` — "The side of the quote to accept (yes or no)". The REST pages do NOT spell out counterparty legs. The FIX doc does, via its AcceptQuote (35=UA) mapping:

- **Side 1 (BUY)** "accepts maker's NO quote (OfferPx, tag 133)"
- **Side 2 (SELL)** "accepts maker's YES quote (BidPx, tag 132)"
- "Price is the maker's quoted price."

Interpretation (consistent across guide + FIX; verify on demo): `yes_bid` = price the **maker pays for YES contracts**; `no_bid` = price the **maker pays for NO contracts**. When requester accepts:
- `accepted_side="yes"` → the maker's YES bid is hit → **maker buys YES at `yes_bid`**, requester takes the NO side (economically buys NO at `$1 − yes_bid`).
- `accepted_side="no"` → **maker buys NO at `no_bid`**, requester takes the YES side (buys YES at `$1 − no_bid`).

So for a maker: `yes_bid` is your bid to own YES, `no_bid` is your bid to own NO; the requester crosses your spread of `1 − yes_bid − no_bid`. The constraint `yes_bid + no_bid ≤ $1` guarantees a non-negative spread. Setting one side to `"0"` declines that side (both zero is rejected).

**Quote sizing:** `POST /communications/quotes` has **no size field** — the quote implicitly covers the RFQ's full size. For `target_cost_dollars` RFQs the derived contract count depends on which side is accepted; the per-side derived counts surface as `yes_contracts_fp` / `no_contracts_fp` on the quote object (WS names: `yes_contracts_offered_fp` / `no_contracts_offered_fp`), and the actually-accepted count as `contracts_accepted_fp` in the WS `quote_accepted` message.

---

## 4. Endpoints — exact schemas

### GET `/communications/id`
"Endpoint for getting the communications ID of the logged-in user." → **200** `{"communications_id": "string"}` — public identifier used in RFQ/quote `creator_id` fields. Errors: 401, 500.

### POST `/communications/rfqs` (Create RFQ — requester side)
Request body:
- `market_ticker` (string, **required**) — market to RFQ
- `rest_remainder` (boolean, **required**) — "Whether to rest the remainder after execution"
- `contracts` (integer, optional) — whole-contract count; "if both contracts and contracts_fp provided, must match"
- `contracts_fp` (FixedPointCount string, optional) — 0.01-contract increments; responses always show exactly 2 decimals ("10.00"); requests accept 0–2 decimals
- `target_cost_dollars` (FixedPointDollars string, optional) — up to 6 decimal places
- `target_cost_centi_cents` (int64) — **DEPRECATED**, use `target_cost_dollars`
- `replace_existing` (boolean, optional, default false) — "delete existing RFQs as part of this RFQ's creation"
- `subtrader_id` (string, optional, FCM members only)
- `subaccount` (integer, optional; 0 = primary, 1–63 = subaccounts)

Constraints: max **100 open RFQs per account**; minimum contract granularity 0.01.
**201** → `{"id": "string"}`. Errors: 400, 401, **409 Conflict "Resource already exists or cannot be modified"** (duplicate open RFQ; use `replace_existing:true` to avoid), 500.

### GET `/communications/rfqs`
Query params: `cursor` (string), `event_ticker` (string — "Only a single event ticker is supported"), `market_ticker` (string), `subaccount` (integer — "If omitted, defaults to all subaccounts"), `limit` (integer 1–100, default **100**), `status` (string), `user_filter` (enum: `"self"`).
**200** → `{"rfqs": [RFQ], "cursor": "string"}`.

### GET `/communications/rfqs/{rfq_id}`
**200** → `{"rfq": RFQ}`. Errors: 401, 404, 500.

**RFQ object fields:** `id` (string), `creator_id` (string — public communications ID), `creator_user_id` (string, private), `creator_subaccount` (integer), `market_ticker` (string), `contracts_fp` (string, 2-decimal fixed point), `target_cost_dollars` (string, up to 6 dp), `status` (enum: `open` | `closed`), `created_ts` / `updated_ts` / `cancelled_ts` (date-time strings, e.g. "2024-01-01T00:00:00Z"), `cancellation_reason` (string), `rest_remainder` (boolean), `mve_collection_ticker` (string — combo/multivariate indicator), `mve_selected_legs` (array of `{event_ticker, market_ticker, side, yes_settlement_value_dollars}` — all optional strings; `yes_settlement_value_dollars` nullable).

### DELETE `/communications/rfqs/{rfq_id}`
**204** on success. Errors: 401, 404, 500. No documented timing constraints.

### POST `/communications/quotes` (Create Quote — **the maker's main write**)
Request body (exact names — note these are NOT the `_dollars` names used in responses):
- `rfq_id` (string, **required**) — "The ID of the RFQ to quote on"
- `yes_bid` (FixedPointDollars string, **required**) — "The bid price for YES contracts, in dollars"
- `no_bid` (FixedPointDollars string, **required**) — "The bid price for NO contracts, in dollars"
- `rest_remainder` (boolean, **required**) — "Whether to rest the remainder of the quote after execution"
- `post_only` (boolean, optional, default false) — "If true, the quote creator's resting order will be cancelled rather than crossed if it would take liquidity." (Implies at execution your order goes to the public book and may cross resting book liquidity.)
- `subaccount` (integer, optional, 0–63)

FixedPointDollars = "US dollar amount as a fixed-point decimal string with up to 6 decimal places of precision", e.g. `'0.5600'`.
**201** → `{"id": "string"}` ("The ID of the newly created quote"). Errors: 400, 401, 500. Rate limit: 2 tokens.
Duplicate-quote behavior is not on this page; per guide/FIX, a new quote on the same RFQ auto-cancels/replaces your previous one.

### GET `/communications/quotes`
Query params: `cursor` (string), `min_ts` / `max_ts` (int64 Unix seconds — filters on **updated** time), `limit` (int32, 1–500, default **500**), `status` (string), `quote_creator_user_id` (string, **deprecated**), `user_filter` (enum `"self"`), `rfq_user_filter` (enum `"self"` — quotes on RFQs the authed user created), `rfq_creator_user_id` (string, **deprecated**), `rfq_creator_subtrader_id` (string, FCM only), `rfq_id` (string).
**200** → `{"quotes": [Quote], "cursor": "string"}`.

### GET `/communications/quotes/{quote_id}`
**200** → `{"quote": Quote}`. Errors: 401, 404, 500. 2 tokens.

**Quote object fields (response schema — note `_dollars` suffixes):**
| Field | Type | Notes |
|---|---|---|
| `id` | string | quote ID |
| `rfq_id` | string | |
| `creator_id` | string | quote creator's public communications ID |
| `rfq_creator_id` | string | RFQ creator's public communications ID |
| `market_ticker` | string | |
| `contracts_fp` | string | fixed-point, 2 decimals |
| `yes_bid_dollars` | string | up to 6 dp |
| `no_bid_dollars` | string | up to 6 dp |
| `created_ts`, `updated_ts` | date-time | |
| `status` | enum | `open`, `accepted`, `confirmed`, `executed`, `cancelled` |
| `accepted_side` | enum, optional | `yes` \| `no` |
| `accepted_ts`, `confirmed_ts`, `executed_ts`, `cancelled_ts` | date-time, optional | full lifecycle timestamps |
| `rest_remainder` | boolean | |
| `post_only` | boolean | visible to creator |
| `cancellation_reason` | string, optional | |
| `creator_user_id`, `rfq_creator_user_id` | string | private fields |
| `rfq_target_cost_dollars` | string, optional | RFQ's requested dollar size |
| `creator_order_id` | string, private | **maker's order ID after execution → join to `/portfolio/fills`** |
| `rfq_creator_order_id` | string, private | requester's order ID |
| `creator_subaccount`, `rfq_creator_subaccount` | integer | visible to respective owner |
| `yes_contracts_fp`, `no_contracts_fp` | string, optional | per-side derived contract counts |

### PUT `/communications/quotes/{quote_id}/accept` (requester calls)
Body: `{"accepted_side": "yes"}` — `accepted_side` (string enum `yes`|`no`, **required**). **204** no body. Errors: 400, 401, 404, 500. "The endpoint will require the quoter to confirm."

### PUT `/communications/quotes/{quote_id}/confirm` (**maker calls**)
Body optional/empty. **204** on success. "This will start a timer for order execution." Errors: 401, 404, 500. **Combos (HVM): call this within 3 s of acceptance or the quote is automatically voided.**

### DELETE `/communications/quotes/{quote_id}` (maker cancels own quote)
**204**. Errors: 401, 404, 500. 2 tokens. No documented constraint about post-accept deletion.

### RFQ-scoped variants (current, NOT marked deprecated — same semantics, extra path safety):
- PUT `/communications/rfqs/{rfq_id}/quotes/{quote_id}/accept` — body `{"accepted_side":"yes"|"no"}`, 204
- PUT `/communications/rfqs/{rfq_id}/quotes/{quote_id}/confirm` — "Endpoint for confirming a quote scoped to its RFQ. This will start a timer for order execution.", 204
- DELETE `/communications/rfqs/{rfq_id}/quotes/{quote_id}` — "deleting a quote scoped to its RFQ, which means it can no longer be accepted.", 204, 2 tokens

---

## 5. WebSocket `communications` channel

Connect `wss://external-api-ws.kalshi.com` (prod), authenticated. Market specification in the subscribe params is **ignored** (channel is account/global scoped). Optional sharding params: `shard_factor` (1–100), `shard_key` (0 ≤ key < shard_factor). All messages carry `sid` (server subscription id) and `type`. Exactly five message types:

**Distribution rules:** "RFQ events (RFQCreated, RFQDeleted) always sent" (broadcast to all subscribers). "Quote events (QuoteCreated, QuoteAccepted, QuoteExecuted) are only sent if you created the quote OR you created the RFQ." There is **no `quote_confirmed`, `quote_deleted`/`quote_cancelled`, or `rfq_updated` event** — a maker cannot learn via WS that its quote was cancelled/voided; poll REST.

1. **`rfq_created`** — msg: `id`, `creator_id` (anonymized; **empty string in this event**), `market_ticker`, `event_ticker` (opt), `contracts_fp` (opt, 2dp), `target_cost_dollars` (opt), `created_ts`, `mve_collection_ticker` (opt), `mve_selected_legs` (opt array of `{event_ticker, market_ticker, side, yes_settlement_value_dollars}`).
```json
{"type":"rfq_created","sid":15,"msg":{"id":"rfq_123","creator_id":"","market_ticker":"FED-23DEC-T3.00","event_ticker":"FED-23DEC","contracts_fp":"100.00","target_cost_dollars":"0.35","created_ts":"2024-12-01T10:00:00Z"}}
```
2. **`rfq_deleted`** — msg: `id`, `creator_id`, `market_ticker`, `event_ticker` (opt), `contracts_fp` (opt), `target_cost_dollars` (opt), `deleted_ts`.
3. **`quote_created`** — msg: `quote_id`, `rfq_id`, `quote_creator_id` (anonymized), `market_ticker`, `event_ticker` (opt), `yes_bid_dollars`, `no_bid_dollars`, `yes_contracts_offered_fp` (opt), `no_contracts_offered_fp` (opt), `rfq_target_cost_dollars` (opt), `created_ts`.
4. **`quote_accepted`** — msg: `quote_id`, `rfq_id`, `quote_creator_id`, `market_ticker`, `event_ticker` (opt), `yes_bid_dollars`, `no_bid_dollars`, `accepted_side` (`yes`|`no`, opt), `contracts_accepted_fp` (opt), `yes_contracts_offered_fp` (opt), `no_contracts_offered_fp` (opt), `rfq_target_cost_dollars` (opt). **This is the maker's 3-second-clock trigger for combos.**
5. **`quote_executed`** — msg: `quote_id`, `rfq_id`, `quote_creator_id`, `rfq_creator_id`, `order_id` ("your order ID from execution"), `client_order_id` ("for fill correlation"), `market_ticker`, `executed_ts`. Doc: "Use QuoteExecuted to correlate fill messages with quotes via client_order_id."

Note WS naming drift vs REST: WS `yes_contracts_offered_fp`/`no_contracts_offered_fp` == REST quote `yes_contracts_fp`/`no_contracts_fp`.

---

## 6. FIX corroboration (fix/rfq-messages.md) — semantics that REST docs omit

- Message types: QuoteRequest `35=R`, QuoteRequestAck, Quote `35=S`, QuoteCancel `35=Z`, AcceptQuote `35=UA`, AcceptQuoteStatus `35=UC`, QuoteConfirm `35=U7`, RFQCancel `35=UE`.
- RFQ states: CREATED → ACTIVE → ACCEPTED (after AcceptQuote) / CANCELLED (after RFQCancel).
- QuoteStatus (tag 297): PENDING(10) awaiting action, ACCEPTED(0), REJECTED(5) exchange-rejected, CANCELLED(17).
- **Confirmation:** "Quote must be confirmed within 30 seconds of acceptance or it will be voided." (standard-tier number; guide overrides to 3 s for HVM/combos). Voiding is automatic on lapse — no maker decline message exists.
- **Accept mapping:** Side 1 (BUY) accepts maker's NO quote (`OfferPx` tag 133); Side 2 (SELL) accepts maker's YES quote (`BidPx` tag 132). Price = maker's quoted price; `OrderQty` (tag 38) supports 0.01 increments.
- FIX prices are **integer cents 1–99** (`BidPx`/`OfferPx`) — REST uses dollar strings; exchange→creator display converts to decimal dollars (e.g. 0.4500). "Either BidPx or OfferPx can be zero, but not both."
- `RestRemainder` (tag 21015): creator side = "Rest the quote remainder after execution (default: N)"; **market-maker side = "Allow partial fills (default: N)"** — i.e., maker `rest_remainder:false` reads as disallowing partial execution.
- Replacement: "If a new Quote is created when an existing quote for the same market already exists for the user, the exchange will cancel the existing quote."
- `ReplaceExisting` (tag 21016) = REST `replace_existing`.
- `PreferBetterQuote` (tag 21022): requester option — exchange selects best available quote at least as good as the one named; result in `AcceptedQuoteId` (tag 21024), may differ from requested `QuoteId` (117). (**Maker implication: your quote can be executed via an accept that targeted a different quote id.** No REST equivalent documented.)
- MVE/parlay RFQs: FIX legs via tags 20180–20184; server resolves/creates the parlay market and returns its ticker in QuoteRequestAck `Symbol` (55). One market per RFQ (`NoRelatedSym` (146) "must be 1").

---

## 7. Price grid & combo identification (GET `/markets/{ticker}`)

Market object has `price_ranges`: array of `{start, end, step}` — all **strings in dollars**; "Valid price ranges for orders on this market"; `step` = tick size for that range. Also `price_level_structure` (string) "defining price ranges and tick sizes". No standalone `tick_size` field; no explicit HVM flag documented. Combo markets identified by `mve_collection_ticker` + `mve_selected_legs` on the market/RFQ objects. Quote prices must land on this grid or you get `invalid_parameters`.

---

## 8. Fills (GET `/portfolio/fills`) — post-execution reconciliation

Query: `ticker`, `order_id` (**filter by the quote's `creator_order_id`**), `min_ts`/`max_ts` (int64), `limit` (1–1000, default 100), `cursor`, `subaccount`.
Fill object: `fill_id`, `trade_id` (legacy alias of fill_id), `order_id`, `ticker`, `market_ticker` (legacy alias), `side` (yes/no, **deprecated**), `action` (buy/sell, **deprecated**), `outcome_side` (enum yes/no — directional exposure), `book_side` (enum bid/ask — canonical direction), `count_fp` (string, 2 decimals), `yes_price_dollars`, `no_price_dollars`, `is_taker` (boolean), `fee_cost` (string, dollars), `created_time` (date-time, opt), `subaccount_number` (int, nullable), `ts` (int64, legacy).

---

## 9. Implementation traps (explicit flags)

1. **Field-name asymmetry:** write `yes_bid`/`no_bid` (Create Quote request) but read `yes_bid_dollars`/`no_bid_dollars` (quote objects, WS). Similarly `yes_contracts_fp` (REST) vs `yes_contracts_offered_fp` (WS).
2. **Prices are strings**, fixed-point dollars up to 6 dp — never floats. Sizes (`contracts_fp`, `count_fp`) are strings with 2 dp in responses.
3. **3 s confirm / 1 s execute on all combos** — the WS `quote_accepted` handler must confirm nearly synchronously; a 500 ms quoting loop is fine but confirmation cannot wait for a poll cycle. There is no WS event for "confirm window lapsed" — your quote silently voids.
4. **No maker decline endpoint** — the only documented outs are (a) don't confirm and let it void, (b) possibly DELETE the quote (undocumented whether allowed in `accepted` status). Deleting before acceptance is the safe pull.
5. **Quote has no size** — you always quote the full RFQ size; sizing risk control must happen at the skip/price level, not quantity.
6. **`accepted_side` refers to which of YOUR bids gets hit** (per FIX mapping): `yes` → you buy YES at `yes_bid`; `no` → you buy NO at `no_bid`. Requester pays `1 − your_other_side_price` economically.
7. **`rfq_created` WS `creator_id` is empty** — you cannot identify or filter requesters from the broadcast event.
8. **Execution enters the public book** after the 1 s timer — `post_only:true` cancels your leg rather than taking book liquidity; with `post_only:false` your executing order can cross the public book. Fills from RFQs look like regular fills (`is_taker` field present) and must be joined via `creator_order_id`/`client_order_id`.
9. **`min_ts`/`max_ts` on Get Quotes filter by updated time**, not created time.
10. **PreferBetterQuote (FIX)** means an accept may land on your quote even though the requester targeted another quote id — treat any `quote_accepted` for your quote_id as live regardless of prior book state.
11. Deprecated: `target_cost_centi_cents` (use `target_cost_dollars`), `quote_creator_user_id`/`rfq_creator_user_id` query params (use `user_filter=self`/`rfq_user_filter=self`), fill `side`/`action` (use `outcome_side`/`book_side`).

## Critical facts (must get right)
- Combo markets are all High Volatility Markets: maker must PUT /communications/quotes/{quote_id}/confirm within 3 seconds of acceptance (30 s on standard markets) or the quote is AUTOMATICALLY VOIDED; execution then fires 1 s after confirm (15 s standard).
- Create Quote is POST /communications/quotes with body fields exactly: rfq_id (string), yes_bid (string FixedPointDollars), no_bid (string FixedPointDollars), rest_remainder (bool, required), post_only (bool, optional), subaccount (int, optional). Responses/WS rename the prices to yes_bid_dollars / no_bid_dollars — the request and response field names differ.
- All prices and sizes are fixed-point DECIMAL STRINGS: dollars up to 6 decimal places (e.g. '0.5600'), contracts_fp/count_fp with 2 decimals ('100.00', 0.01 granularity). Never send floats or cents integers on REST (integer cents 1-99 exist only in FIX).
- A quote has NO size field — it implicitly covers the full RFQ size (contracts_fp or target_cost_dollars). Constraint: yes_bid + no_bid <= $1; either side may be '0' to decline that side but not both.
- accepted_side semantics (from FIX mapping): accepted_side='yes' hits the maker's YES bid (maker buys YES at yes_bid, requester takes NO); accepted_side='no' hits the maker's NO bid (maker buys NO at no_bid, requester takes YES).
- Submitting a new quote on the same RFQ automatically cancels/replaces the maker's previous quote — no explicit cancel needed to re-price; explicit pull is DELETE /communications/quotes/{quote_id} (2 tokens).
- There is NO explicit maker decline-after-accept endpoint; the only documented escape is letting the confirmation window lapse (automatic void). Design the bot to either confirm within 3 s or deliberately not call confirm.
- WebSocket channel 'communications' (wss://external-api-ws.kalshi.com, auth required) has exactly 5 events: rfq_created & rfq_deleted (broadcast, creator_id empty/anonymized) and quote_created, quote_accepted, quote_executed (only to involved parties). There is NO quote_confirmed or quote_cancelled event — quote voids/cancellations must be discovered via REST polling (GET /communications/quotes, status enum: open|accepted|confirmed|executed|cancelled).
- Fill reconciliation: after quote_executed, the maker's order appears as creator_order_id on the quote object (and order_id/client_order_id in the WS quote_executed message); join to GET /portfolio/fills via its order_id query param. Fill prices come back as yes_price_dollars/no_price_dollars with count_fp and fee_cost.
- POST /communications/rfqs returns 409 Conflict when an open RFQ already exists ('Resource already exists or cannot be modified'); replace_existing:true deletes existing RFQs on creation. Max 100 open RFQs per account. Other documented errors: invalid_parameters (bad price / closed RFQ), RFQ_CLOSED (deleted/expired/executed), INSUFFICIENT_BALANCE.
- Auth on every REST call: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE (RSA-PSS), KALSHI-ACCESS-TIMESTAMP in MILLISECONDS. Prod base https://external-api.kalshi.com/trade-api/v2, demo https://demo-api.kalshi.co/trade-api/v2 (paths identical).
- Quote prices must land on the market's price grid: validate against price_ranges ({start, end, step} dollar strings) from GET /markets/{ticker} before quoting, or expect invalid_parameters.
- After confirm + execution timer, orders are entered into the PUBLIC order book; post_only=true cancels the maker's resting order rather than letting it take book liquidity. On the maker side, rest_remainder is described in FIX as 'Allow partial fills (default: N)'.

## Open questions (verify empirically on demo)
- accepted_side economics need demo confirmation: the FIX mapping strongly implies accepted_side='yes' means the maker buys YES at yes_bid, but no REST page states counterparty legs or the requester's effective price (1 - other_bid?) explicitly. Verify with a demo round-trip and inspect both parties' fills (outcome_side, yes_price_dollars/no_price_dollars).
- Can a maker DELETE /communications/quotes/{quote_id} while the quote is in 'accepted' status (i.e., an explicit decline), or does delete 404/409 after acceptance leaving window-lapse as the only out? Docs are silent.
- What exactly happens on confirmation-window lapse from the maker's REST view: does the quote move to status='cancelled' with a specific cancellation_reason string (and what are the possible cancellation_reason values)? No enum is documented.
- rest_remainder maker-side semantics: REST says 'rest the remainder of the quote after execution' but FIX calls the same flag 'Allow partial fills (default: N)'. Determine on demo: with rest_remainder=false, is the execution all-or-none, and with true, whose order rests on the public book, at what price, and does post_only interact with it?
- During the 1 s execution timer on combos: can either party still cancel, can third parties trade against the pending orders, and do the two orders always cross each other at the quoted price or can the book improve/steal one leg (especially with post_only=false)?
- How target_cost_dollars converts to contract counts per side: which formula produces yes_contracts_fp vs no_contracts_fp (target / (1 - opposite_bid)? target / bid?), and does contracts_accepted_fp ever differ from the offered count (partial acceptance)? The doc example values ('0.35' target with 100/200 contracts) are internally inconsistent.
- Demo WebSocket URL for the communications channel is not documented (prod is wss://external-api-ws.kalshi.com); confirm the demo host and the exact subscribe command shape (cmd/params JSON) empirically.
- Does REST have an equivalent of FIX PreferBetterQuote (accept-best-quote), meaning a maker's quote can be executed by an accept that named a different quote? Affects whether the bot must treat all its open quotes as acceptable at any moment.
- Token costs are only documented for create/delete/get quote (2 tokens each); pull GET /trade-api/v2/account/endpoint_costs on demo to get actual costs for accept/confirm/create-RFQ and overall bucket size/refill rate for quoting-loop throughput planning.
- Are RFQ-scoped endpoint variants (PUT /communications/rfqs/{rfq_id}/quotes/{quote_id}/confirm etc.) preferred or just aliases? Both forms are documented as current; verify identical behavior and pick one.
- Fee treatment on RFQ executions: fee_cost appears on fills and is_taker exists, but no RFQ doc says which side is taker/maker for fee purposes on RFQ-originated fills — measure on demo.
- Whether quotes have any independent expiry (TTL) before acceptance, and whether RFQs auto-expire (RFQ_CLOSED mentions 'expired' but no lifetime constant is documented anywhere).
