# Kalshi API — Rate Limits & Account Limits (digested 2026-07-05)

Sources fetched: `docs.kalshi.com/getting_started/rate_limits.md`, `api-reference/account/get-account-api-limits.md`, plus referenced pages `api-reference/account/list-non-default-endpoint-costs.md`, `api-reference/account/upgrade-account-api-usage-level.md`, `api-reference/account/get-account-api-usage-level-volume-progress.md`, `websockets/websocket-connection.md`, and `llms.txt`. Both target URLs resolved (no 404s).

## 1. Rate limit model: token buckets

- Two token buckets per account: **Read** and **Write**. Metered **by operation type, not by protocol** — "REST and FIX requests drain the same buckets." (WebSocket is NOT mentioned in the rate_limits doc at all — see §7.)
- **Default cost: 10 tokens per request.** Endpoints with non-default costs are listed authoritatively by `GET /account/endpoint_costs` (§5).
- **Perps are separate**: "Perps traffic is metered in its own Read and Write buckets. Perps calls do not draw down your event-contract budgets." Perps limits endpoint: rate_limits doc says `GET /account/limits/perps`; llms.txt has a `margin-rest/account/get-perps-account-api-limits.md` page. Irrelevant for event-contract combos MM, but don't confuse the two lanes.

### Tier table (tokens per second refill)

| Tier | Read (tokens/sec) | Write (tokens/sec) |
|------|------|------|
| Basic | 200 | 100 |
| Advanced | 300 | 300 |
| Expert | 600 | 600 |
| Premier | 1,000 | 1,000 |
| Paragon | 2,000 | 2,000 |
| Prime | 4,000 | 4,000 |
| Prestige | 6,000 | **8,000** (asymmetric — write > read) |

At default 10-token cost: Basic = 20 reads/s, **10 writes/s**; Advanced = 30 reads/s, 30 writes/s.

### What counts as Write (verbatim list)

"Order placement, amends, cancels, order groups, the RFQ quote flow, and block trade proposal accepts."

Everything else (GET endpoints) is Read. **The RFQ quote flow is a WRITE** — create/accept/confirm/delete quote, create/delete RFQ all drain the Write bucket. So does `POST /account/api_usage_level/upgrade` (30 tokens, Write bucket).

### Bucket capacity / bursting (verbatim rules)

- "Basic and Advanced Predictions Read buckets, and Write buckets above the Basic tier, hold up to **two seconds of budget**."
- "Predictions Read buckets above Advanced, Perps Read buckets, and Basic-tier Write buckets hold **one second of budget**."
- "When you spend less than your budget, unspent tokens accumulate, and after two quiet seconds the bucket is full. You can then spend up to **twice your per-second budget in a single burst** before throttling back to the refill rate."
- One-second buckets: "You can spend a full second's budget at once, but idle time banks nothing beyond that."

Concrete capacities: Basic → Read cap 400, Write cap 100 (no write burst!). Advanced → Read cap 600, Write cap 600. Expert → Read cap 600 (1s), Write cap 1,200 (2s).

### Batch operations

"A batch request costs the same as making each call individually. Every item in the batch is billed separately."
- Batch Create Orders, 25 orders: 25 × 10 = **250 tokens**.
- Batch Cancel Orders, 25 orders: 25 × **2** = **50 tokens** → **cancels cost 2 tokens, 5× cheaper than creates.** Cheap cancels favor aggressive quote-pulling.

### 429 handling

- Response: HTTP `429 Too Many Requests`, body exactly `{"error": "too many requests"}`.
- "No penalty or cooldown. The bucket keeps refilling, and your next request succeeds once the balance covers its cost." Docs advise exponential backoff.
- Worked example from docs: "At a 1,000 tokens-per-second refill, a 10-token order is covered again 10 ms after a 429." (i.e., retry delay ≈ cost/refill_rate seconds; no need for long sleeps.)

## 2. Tier qualification & upgrades

- **Basic**: automatic on account signup.
- **Advanced**: self-service via **`POST /account/api_usage_level/upgrade`** (Upgrade Account API Usage Level). NOTE: one summary rendered this path as `/account/usage_level` — the API-reference page's `/account/api_usage_level/upgrade` is authoritative.
  - Eligibility: "at least one of the user's last 100 Predictions orders must be API-created" (403: "No API-created order was found in the user's latest 100 Predictions orders").
  - 201: "Advanced API usage-level grant created or refreshed successfully". Grant is **permanent**.
  - Costs **30 tokens** (non-default), drawn from the Predictions **Write** bucket. No request body. Errors: 401, 403, 429, 500.
  - Currently applies only to the Predictions (event_contract) exchange instance.
- **Expert and above**: earned automatically from 30-day trading volume, or manually assigned by Kalshi.
  - Formula: `volume_share = your_30day_volume ÷ (previous_month_exchange_volume × 2)`.
  - Thresholds (Earn / Keep): Expert 0.075% / 0.05%; Premier 0.125% / 0.10%; Paragon 0.25% / 0.20%; Prime 0.50% / 0.40%; Prestige 1.00% / 0.80%.
  - "A qualifying review grants the tier for **30 days**, and each daily review renews the window while you keep qualifying." "If your volume falls below the Keep threshold, the tier does not drop immediately. It lapses when your current 30-day grant runs out." (hysteresis)

## 3. GET /account/limits — GetAccountApiLimits

- Method+path: **`GET /account/limits`** (relative to `/trade-api/v2`).
- Base URLs: Prod `https://external-api.kalshi.com/trade-api/v2` (alt: `https://api.elections.kalshi.com/trade-api/v2`); Demo `https://external-api.demo.kalshi.co/trade-api/v2` (alt: `https://demo-api.kalshi.co/trade-api/v2`).
- Auth (standard for all authed REST): headers `KALSHI-ACCESS-KEY` (API key ID), `KALSHI-ACCESS-SIGNATURE` (RSA-PSS signature), `KALSHI-ACCESS-TIMESTAMP` (**milliseconds**).
- Response 200 `GetAccountApiLimitsResponse`:
  - `usage_tier` (string): one of `basic`, `advanced`, `expert`, `premier`, `paragon`, `prime`, `prestige`.
  - `read` (BucketLimit), `write` (BucketLimit):
    - `refill_rate` (integer): tokens added per second.
    - `bucket_capacity` (integer): max tokens; "when exceeding refill_rate, represents burst capacity".
  - `grants` (array[ApiUsageLevelGrant]):
    - `exchange_instance` (enum): `event_contract` | `margined`.
    - `level` (string): `expert`, `premier`, `paragon`, `prime`, `prestige` (per docs; the Advanced self-service grant also appears here per rate_limits doc semantics).
    - `expires_ts` (integer|null): Unix timestamp **seconds**; "A grant with no `expires_ts` is permanent."
    - `source` (string): `volume` (earned) | `manual` (Kalshi-assigned).
- Errors: 401 Unauthorized, 500 Internal server error.
- Trap: `expires_ts` is unix **seconds**, while auth `KALSHI-ACCESS-TIMESTAMP` is **milliseconds**.

## 4. GET /account/api_usage_level/volume_progress

- Method+path: **`GET /account/api_usage_level/volume_progress`**. Same auth headers. No params.
- Response 200:
```json
{
  "volume_progress": [
    {
      "computed_ts": 1234567890,
      "trailing_30d_volume_fp": "10.00",
      "goals": [
        { "level": "expert", "earn_volume_goal_fp": "100.00", "keep_volume_goal_fp": "50.00" }
      ]
    }
  ]
}
```
- `computed_ts` int64 unix seconds; `trailing_30d_volume_fp`, `earn_volume_goal_fp`, `keep_volume_goal_fp` are **strings** — fixed-point contract counts with 2 decimals (do not parse as float for equality checks). Errors: 401, 500.

## 5. GET /account/endpoint_costs — ListNonDefaultEndpointCosts

- Method+path: **`GET /account/endpoint_costs`**. The rate_limits doc calls this "the authoritative list of non-default costs currently in effect."
- Docs page lists **no auth in security requirements** (possibly public — verify).
- Response 200:
```json
{
  "default_cost": 10,
  "endpoint_costs": [ { "method": "string", "path": "string", "cost": 0 } ]
}
```
- `default_cost` (integer): cost for endpoints not listed. `endpoint_costs` (array): only endpoints whose cost differs from default. **The docs do NOT publish the actual cost table** — the bot must fetch this at startup (and periodically; "currently in effect" implies it can change) to know real costs of cancels (2 per the batch example), RFQ quote endpoints, etc. Error: 500.

## 6. Budget math for the RFQ combo market maker

- On **Basic**, Write = 100 tokens/s = **10 default-cost write ops/s with capacity 100 (no burst)**. Quote creates + deletes on many concurrent RFQs will hit this fast. **Upgrade to Advanced immediately** (place 1 API order first, then `POST /account/api_usage_level/upgrade`) → 300/300 with 2-second write capacity (600) and 2× burst.
- Reads (GetRFQs polling, orderbooks, markets) come from the separate Read bucket — polling cannot starve quoting, and vice versa.
- Client-side token-bucket throttle should mirror server: track cost per (method, path) from `GET /account/endpoint_costs`, default 10; per-item billing for batches.

## 7. WebSocket connection & subscription limits (from websockets/websocket-connection.md)

- Prod WS URL: **`wss://external-api-ws.kalshi.com/`**. Auth: API key headers during the WS handshake (same KALSHI-ACCESS-* scheme). Demo WS URL not given on this page.
- Commands: `subscribe`, `unsubscribe`, `list_subscriptions`. `id` must be unique in session, start at 1 and increment; **`id: 0` is treated as absent**.
```json
{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": "CPI-22DEC-TN0.1"}}
{"id": 124, "cmd": "unsubscribe", "params": {"sids": [1, 2]}}
```
- Channels: `orderbook_delta`, `ticker`, `trade`, `fill`, `market_positions`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `multivariate`, `communications` (RFQ stream), `order_group_updates`, `user_orders`, `cfbenchmarks_value`.
- Market selection params (mutually exclusive): `market_ticker` | `market_tickers` | `market_id` | `market_ids`. Optional: `send_initial_snapshot` (bool, ticker), `skip_ticker_ack` (bool), `use_yes_price` (bool, orderbook), `shard_factor` (int 1–100, communications channel fanout), `shard_key` (int, 0 ≤ key < shard_factor), `index_ids` (cfbenchmarks; `["all"]`).
- Response types: `subscribed` (has `channel`, `sid`), `ok` (optional `market_tickers`/`market_ids`), `unsubscribed` (has `seq`), `error` (numeric `code` 1–27 + `msg`). All echo optional `id`.
- **Limit-related WS error codes (numeric values of the limits are NOT documented):**
  - **25**: "Subscription buffer overflow - The subscription's outbound buffer was exceeded" (consume fast or you get dropped/errored).
  - **26**: "Subscription market limit exceeded - Adding markets would exceed the per-subscription market limit".
  - **27**: "Too many requests - The subscription exceeded its command rate limit".
- The docs give **no** max concurrent connections, no absolute subscriptions-per-connection cap, and no heartbeat/ping interval. The `shard_factor` param exists specifically so high-volume channels (communications) can be split across connections — use it if the RFQ stream overflows (error 25).
- The rate_limits doc does not say WS commands drain REST token buckets (only "REST and FIX" are named) — WS command throttling appears to be a separate per-subscription mechanism (error 27).

## 8. Traps / contradictions with common assumptions

1. **RFQ quoting is Write-bucket traffic** — a quoter that also places hedge orders shares one Write budget with them.
2. **Basic-tier Write has zero burst headroom** (capacity = 1 second = 100 tokens). Bursting 11 default-cost writes in one instant 429s the 11th.
3. Cancels cost **2**, not 10 — derived from the Batch Cancel example; confirm via `GET /account/endpoint_costs`.
4. **429 has no penalty box** — do not implement long lockouts; retry after ~cost/refill_rate seconds with jittered backoff.
5. Timestamp units differ: auth header ms, `expires_ts`/`computed_ts` seconds.
6. Volume fields are fixed-point **strings** ("10.00"), not numbers.
7. Upgrade endpoint path is `/account/api_usage_level/upgrade` (API reference), not `/account/usage_level`.
8. Docs never publish the non-default cost table — it is runtime data only.
9. Tier grants are per `exchange_instance` (`event_contract` vs `margined`); perps have entirely separate buckets and their own limits endpoint.

## Critical facts (must get right)
- Rate limits are token buckets, Read and Write, default 10 tokens per request; tiers (read/write tokens per sec): Basic 200/100, Advanced 300/300, Expert 600/600, Premier 1000/1000, Paragon 2000/2000, Prime 4000/4000, Prestige 6000/8000.
- Write bucket covers: order placement, amends, cancels, order groups, THE RFQ QUOTE FLOW, and block trade proposal accepts; every other (GET) operation is Read. REST and FIX drain the same buckets.
- Batch requests are billed per item (Batch Create 25 orders = 250 tokens; Batch Cancel 25 = 50 tokens, i.e. cancel costs 2 tokens); the authoritative non-default cost list is runtime data from GET /account/endpoint_costs (response: default_cost int, endpoint_costs[] of {method, path, cost}).
- 429 returns body {"error": "too many requests"} with NO penalty or cooldown; retry once the bucket refills (10-token op at 1000 tokens/s refill is covered 10 ms later); use exponential backoff, never long lockouts.
- Bucket capacity: Basic and Advanced Read buckets and all Write buckets above Basic hold 2 seconds of budget (burst up to 2x per-second rate after 2 idle seconds); Read above Advanced, Perps Read, and BASIC-TIER WRITE hold only 1 second (Basic write = 100-token cap, zero burst headroom).
- Advanced tier is free self-service: POST /account/api_usage_level/upgrade (costs 30 Write tokens; requires >=1 API-created order among your last 100 Predictions orders; 201 grants permanent Advanced; 403 if not eligible). Expert+ requires volume share = 30d_volume / (prev_month_exchange_volume x 2) vs earn/keep thresholds (Expert 0.075%/0.05% ... Prestige 1.00%/0.80%), reviewed daily, 30-day grants with lapse-at-expiry hysteresis.
- GET /account/limits returns usage_tier (basic|advanced|expert|premier|paragon|prime|prestige), read/write BucketLimit {refill_rate int tokens/sec, bucket_capacity int}, and grants[] {exchange_instance: event_contract|margined, level, expires_ts (unix SECONDS, absent = permanent), source: volume|manual}.
- Auth for all authed REST calls: headers KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE (RSA-PSS), KALSHI-ACCESS-TIMESTAMP in MILLISECONDS (while expires_ts/computed_ts in responses are unix seconds).
- Base URLs: prod https://external-api.kalshi.com/trade-api/v2 (alt api.elections.kalshi.com), demo https://external-api.demo.kalshi.co/trade-api/v2 (alt demo-api.kalshi.co); prod WebSocket wss://external-api-ws.kalshi.com/ authenticated via API key headers at handshake.
- WebSocket limits are enforced but numerically undocumented: error 25 = subscription outbound buffer overflow, error 26 = per-subscription market limit exceeded, error 27 = subscription command rate limit exceeded; command id must be unique per session (id 0 treated as absent); communications channel supports shard_factor (1-100) / shard_key to split the RFQ stream across connections.
- Perps traffic uses entirely separate Read/Write buckets and its own limits endpoint; event-contract (Predictions) budgets are untouched by perps calls and vice versa.

## Open questions (verify empirically on demo)
- Numeric values of the WS limits behind error codes 25/26/27: per-subscription market limit, command rate limit, and outbound buffer size are all undocumented — probe on demo (e.g. subscribe with growing market_tickers arrays until error 26).
- Max concurrent WebSocket connections per account/API key and max subscriptions per connection are not documented anywhere fetched.
- Demo environment WebSocket URL is not given on the websocket-connection page (only prod wss://external-api-ws.kalshi.com/), and no doc states whether demo has the same rate-limit tiers/budgets as prod — verify GET /account/limits on demo.
- Actual contents of GET /account/endpoint_costs (which endpoints are non-default, e.g. cancel=2, RFQ create-quote/accept-quote costs, upgrade=30) — the docs never publish the table; fetch it on both demo and prod at startup.
- Whether GET /account/endpoint_costs truly requires no authentication (its docs page lists no security requirements — unusual; verify).
- Whether WebSocket subscribe/unsubscribe commands also drain the REST Read/Write token buckets or are metered only by the separate per-subscription command limiter (docs name only REST and FIX for the shared buckets).
- Exact perps limits path discrepancy: rate_limits doc says GET /account/limits/perps but llms.txt points to a margin-rest get-perps-account-api-limits page — confirm the real path if perps ever matter.
- Whether the volume denominator in the tier formula (previous_month_exchange_volume x 2) counts Predictions-only volume, and whether demo trading volume counts toward tier progression (assume no).
- Whether order amends are billed at create cost (10) or a distinct cost — 'amends' are named as writes but no explicit cost is given.
- Whether the self-service Advanced grant appears in the grants[] array of GET /account/limits with level 'advanced' (the ApiUsageLevelGrant level description only enumerates expert..prestige).
