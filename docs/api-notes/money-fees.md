# Kalshi money & fees notes (fixed-point, fee rounding, fee overrides, GetMarket)

Sources fetched verbatim (raw .md, all 200 OK, OpenAPI spec version **3.23.0**):
- https://docs.kalshi.com/getting_started/fixed_point_migration.md (Last Updated: April 17, 2026)
- https://docs.kalshi.com/getting_started/fee_rounding.md
- https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes.md
- https://docs.kalshi.com/api-reference/market/get-market.md
- PLUS two directly referenced fee-critical pages: https://docs.kalshi.com/api-reference/events/get-event-fee-changes.md and https://docs.kalshi.com/api-reference/market/get-series.md

Base URLs (from the OpenAPI `servers` block, identical on all api-reference pages):
- Production Trade API server: `https://external-api.kalshi.com/trade-api/v2`
- Production shared API server, also supported: `https://api.elections.kalshi.com/trade-api/v2`
- Demo Trade API server: `https://external-api.demo.kalshi.co/trade-api/v2`
- Demo shared API server, also supported: `https://demo-api.kalshi.co/trade-api/v2`

Note demo TLD is `.kalshi.co` (not `.com`).

---

## 1. Fixed-point representation (fixed_point_migration.md)

Kalshi uses fixed-point representation across ALL APIs. Two independent changes:
1. **Subpenny Pricing** — price fields use fixed-point dollar strings, suffix `_dollars`
2. **Fractional Contracts** — contract count fields use fixed-point strings, suffix `_fp`

The `price_level_structure` field on Market responses indicates which pricing tier is active for a given market; the `price_ranges` array provides the exact valid price intervals and tick sizes.

### `*_dollars` fields (prices / dollar amounts)
- JSON example from doc: `{ "price_dollars": "0.1200" }`
- They are **strings**, fixed-point, "up to 4 decimal places (e.g., `"0.1200"`)" per the migration doc.
- When combined with fractional contract sizes, intermediate calculations can reach **up to 6 decimal places** (e.g., fee rounding math: $0.3301 x 0.03 = $0.009903).
- The `FixedPointDollars` schema in the OpenAPI (GetMarket) says: "US dollar amount as a fixed-point decimal string with **up to 6 decimal places of precision**. This is the maximum supported precision; valid quote intervals for a given market are constrained by that market's price level structure." Example: `'0.5600'`. (Slight tension with the migration doc's "up to 4 decimals" — treat 6 as the wire maximum, 4 as typical.)

### Price Level Structures (exact table from migration doc)
| Structure | Ranges | Tick Size |
| --- | --- | --- |
| `linear_cent` | $0.00 – $1.00 | $0.01 |
| `tapered_deci_cent` | $0.00 – $0.10 | $0.001 |
| | $0.10 – $0.90 | $0.01 |
| | $0.90 – $1.00 | $0.001 |
| `deci_cent` | $0.00 – $1.00 | $0.001 |

- `tapered_deci_cent`: finer $0.001 (decicent) precision below $0.10 and above $0.90; middle range standard $0.01 ticks.
- `deci_cent`: $0.001 precision across the entire range.
- Subpenny pricing is offered **per-market** — never hardcode a tick size; read `price_ranges` from the market.

### `*_fp` fields (contract counts)
- JSON example: `{ "count_fp": "10.00" }`
- `*_fp` fields are **strings**.
- "Accept 0-2 decimal places on input (responses always emit 2 decimals)" — e.g., `"10"`, `"10.0"`, `"10.00"` all valid input.
- **Minimum granularity is 0.01 contracts.**
- "In requests where both integer and `_fp` fields are provided, they must match."
- Doc's recommended integer strategy: multiply the `_fp` value by 100 and cast to int — treat `"1.55"` as 155 units of 1c contracts. Even if you never place fractional orders, **you will encounter fractional values elsewhere in the API (for example, fills)**.

---

## 2. Fee rounding (fee_rounding.md)

### Balance target precision
- **Direct member** balances are rounded to the nearest `$0.0001` (`0.01c`)
- **Non-direct member** balances are rounded to the nearest `$0.01` (`1c`)

### Three fee components on EVERY fill
| Component | Description (verbatim) |
| --- | --- |
| **Trade fee** | Fee from the fee model, rounded **up** to the nearest $0.0001 (centicent) |
| **Rounding fee** | Adjustment that restores the user's target balance precision |
| **Rebate** | Refund from accumulated rounding overpayment (always a multiple of $0.01) |

**Net fee = trade fee + rounding fee - rebate (always >= $0.00)**

### Exact rounding algorithm (given a fill's `revenue` — signed, negative for buyers — and `trade_fee`)
1. Round trade fee **up** to the nearest $0.0001 (ceil to centicent)
2. `balance_change = revenue - trade_fee`
3. **Floor** `balance_change` toward **negative infinity** to the user's target balance precision
4. `rounding_fee = balance_change - floor(balance_change)`

The user's balance changes by `floor(balance_change)` — always aligned to target precision.

### Fee accumulator
- Tracks cumulative rounding overpayment **across all fills of an order** (per-order, not per-account).
- Once accumulated rounding **exceeds $0.01**, a whole-cent $0.01 rebate is issued and the accumulator is reduced by $0.01.
- Ensures total fee across many small fills converges to what a single equivalent fill would cost.
- Doc Note (verbatim): "The fee accumulator is maintained per order across all fills **regardless of whether the fills are taker or maker**. If an order initially takes (matching resting orders) and then becomes a resting maker order, the accumulated rounding carries over to subsequent maker fills."

### Worked examples (all assume target precision $0.01, i.e., non-direct member; direct members use $0.0001)

**Example A — subpenny price: buy 3 contracts at $0.055, three 1-lot fills.** Fill 1:
```
revenue        = -$0.055 x 1       = -$0.0550
trade fee      = $0.0085             (ceiled to centicent)
balance change = -$0.0550 - $0.0085  = -$0.0635  -> floored to -$0.07
rounding fee   = $0.07 - $0.0635    =  $0.0065
```
| Fill | Trade Fee | Rounding | Accumulator | Rebate | Net Fee | Balance Change |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | $0.0085 | $0.0065 | $0.0065 | — | $0.0150 | -$0.07 |
| 2 | $0.0085 | $0.0065 | $0.0130 | $0.0100 | $0.0050 | -$0.07 |
| 3 | $0.0085 | $0.0065 | $0.0095 | — | $0.0150 | -$0.07 |

**Example B — fractional contracts: buy 0.90 contracts at $0.50, three 0.30-lot fills.** Fill 1: `revenue = -$0.1500`, `trade fee = $0.0041` (ceiled), `balance change = -$0.1541 -> -$0.16`, `rounding fee = $0.0059`. Fill 2 nets $0.0000 fee after the $0.01 rebate.

**Example C — combined: buy 0.09 contracts at $0.3301, three 0.03-lot fills.** Fill 1: `revenue = -$0.009903` (6 decimals), `trade fee = $0.0005`, `balance change = -$0.010403 -> floored to -$0.02`, `rounding fee = $0.009597`. Rebates trigger on fills 2 AND 3.

Doc note: "Subpenny prices alone produce 4-decimal-place intermediates. Fractional contracts alone also produce 4-decimal-place intermediates. When combined, intermediates can reach 6 decimal places... Final balances are rounded to the user's target balance precision."

### What is NOT here
The actual trade-fee **formula and multiplier constants** (the 0.07 x C x P x (1-P) style formula) are NOT anywhere in docs.kalshi.com. The Series schema (below) points to **`https://kalshi.com/docs/kalshi-fee-schedule.pdf`**: `'quadratic'` = General Trading Fees Table; `'quadratic_with_maker_fees'` = General Trading Fees Table plus the Maker Fees section; `'flat'` = Specific Trading Fees Table. That PDF (outside docs.kalshi.com, not fetched per task rules) is the authoritative source for the constants.

---

## 3. GET /series/fee_changes — GetSeriesFeeChanges (exchange tag)

**Method + path:** `GET /trade-api/v2/series/fee_changes`
**operationId:** `GetSeriesFeeChanges`. OpenAPI `security: []` (no auth requirement declared).

Query parameters (both optional):
- `series_ticker` — string
- `show_historical` — boolean, default `false`

NO pagination parameters (contrast with the events version below).

Responses: `200` -> `GetSeriesFeeChangesResponse`, `400` BadRequestError, `500` InternalServerError.

`GetSeriesFeeChangesResponse` (required: `series_fee_change_arr`):
- `series_fee_change_arr`: array of `SeriesFeeChange`

`SeriesFeeChange` (all fields required: `id`, `series_ticker`, `fee_type`, `fee_multiplier`, `scheduled_ts`):
- `id` — string — "Unique identifier for this fee change"
- `series_ticker` — string — series this change applies to
- `fee_type` — `FeeType` — "New fee type for the series"
- `fee_multiplier` — number (double) — "New fee multiplier for the series"
- `scheduled_ts` — string (date-time) — "Timestamp when this fee change is scheduled to take effect"

`FeeType` enum (exact values): `quadratic` | `quadratic_with_maker_fees` | `flat`. Description: "Fee type for a series or scheduled fee override."

`ErrorResponse` shape (shared across all these endpoints): `code` (string), `message` (string), `details` (string), `service` (string).

---

## 4. GET /events/fee_changes — GetEventFeeChanges (events tag) [referenced, fetched additionally]

**Method + path:** `GET /trade-api/v2/events/fee_changes`
**operationId:** `GetEventFeeChanges`

Doc summary (verbatim): "Event fees are an override layered on top of the parent series' fee structure. If `fee_type_override` and `fee_multiplier_override` are null, that indicates the override is cleared."

Query parameters (all optional):
- `event_ticker` — string
- `limit` — integer int64, min 1, max 1000, **default 100**
- `cursor` — string pagination cursor (empty for first page)

`GetEventFeeChangesResponse` (required: `event_fee_changes`, `cursor`):
- `event_fee_changes`: array of `EventFeeChange`
- `cursor`: string — "Pagination cursor for the next page. Empty if there are no more results."

`EventFeeChange` (required: `id`, `event_ticker`, `series_ticker`, `fee_type_override`, `fee_multiplier_override`, `scheduled_ts`):
- `id` — string
- `event_ticker` — string
- `series_ticker` — string — series of the event
- `fee_type_override` — `FeeType`, **nullable** (example: `quadratic`) — "When null, the event clears any prior override and falls back to the parent series' fee structure."
- `fee_multiplier_override` — number (double), **nullable** — same null-clears semantics for the multiplier
- `scheduled_ts` — string (date-time) — when the change takes effect

Fee resolution order for a market is therefore: **event override (if set) -> series fee structure**. Combo/MVE markets belong to dynamically created multivariate events, so a correct fee engine must check event-level overrides, not just series.

---

## 5. GET /series/{series_ticker} — GetSeries fee fields [referenced, fetched additionally]

**Method + path:** `GET /trade-api/v2/series/{series_ticker}` (path param `series_ticker` required; optional query `include_volume` boolean, default false)

`Series` fee-relevant required fields:
- `fee_type` — `FeeType` (enum `quadratic` | `quadratic_with_maker_fees` | `flat`). Verbatim description: "FeeType is a string representing the series' fee structure. Fee structures can be found at https://kalshi.com/docs/kalshi-fee-schedule.pdf. 'quadratic' is described by the General Trading Fees Table, 'quadratic_with_maker_fees' is described by the General Trading Fees Table with maker fees described in the Maker Fees section, 'flat' is described by the Specific Trading Fees Table."
- `fee_multiplier` — number (double) — "a floating point multiplier applied to the fee calculations."

Other Series fields: `ticker`, `frequency` (free-form human-readable, e.g. weekly/daily/one-off), `title`, `category`, `tags` (array of string, nullable), `settlement_sources` (array of `SettlementSource {name, url}`, nullable), `contract_url`, `contract_terms_url`, `product_metadata` (object, nullable), `additional_prohibitions` (array of string, required), `volume_fp` (`FixedPointCount`, only meaningful with `include_volume=true`), `last_updated_ts` (date-time).

---

## 6. GET /markets/{ticker} — GetMarket (market tag)

**Method + path:** `GET /trade-api/v2/markets/{ticker}`; path param `ticker` (string, required, "Market ticker"). **operationId:** `GetMarket`.
Responses: `200` -> `GetMarketResponse` (`{ "market": Market }`), `401` Unauthorized, `404` Not found, `500` Internal server error. (OpenAPI declares `security: []`; a 401 is still listed.)

### Market object — REQUIRED fields (exact list)
`ticker`, `event_ticker`, `market_type`, `yes_sub_title`, `no_sub_title`, `created_time`, `updated_time`, `open_time`, `close_time`, `latest_expiration_time`, `settlement_timer_seconds`, `status`, `notional_value_dollars`, `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`, `last_price_dollars`, `previous_yes_bid_dollars`, `previous_yes_ask_dollars`, `previous_price_dollars`, `volume_fp`, `volume_24h_fp`, `liquidity_dollars`, `open_interest_fp`, `result`, `can_close_early`, `expiration_value`, `rules_primary`, `rules_secondary`, `price_level_structure`, `price_ranges`

### Field-by-field
- `ticker`, `event_ticker` — string
- `market_type` — enum: `binary` | `scalar`
- `title` — string, **deprecated**
- `subtitle` — string, **deprecated**
- `yes_sub_title` / `no_sub_title` — string, shortened titles per side
- `created_time`, `open_time`, `close_time` — date-time
- `updated_time` — date-time — "Time of the last **non-trading metadata** update." (NOT a trading-activity timestamp)
- `expected_expiration_time` — date-time, nullable — expected expiry
- `expiration_time` — date-time, **deprecated**
- `latest_expiration_time` — date-time — latest possible expiry
- `settlement_timer_seconds` — integer — "The amount of time after determination that the market settles"
- `status` — enum: `initialized` | `inactive` | `active` | `closed` | `determined` | `disputed` | `amended` | `finalized`
- `yes_bid_dollars` — FixedPointDollars — highest YES buy offer
- `yes_bid_size_fp` — FixedPointCount — "Total contract size of orders to buy YES at the best bid price"
- `yes_ask_dollars` — FixedPointDollars — lowest YES sell offer
- `yes_ask_size_fp` — FixedPointCount — total size at best ask
- `no_bid_dollars` / `no_ask_dollars` — FixedPointDollars — highest NO buy / lowest NO sell
- `last_price_dollars` — FixedPointDollars — last traded YES price
- `volume_fp` / `volume_24h_fp` — FixedPointCount — market volume in contracts (strings)
- `result` — enum: `'yes'` | `'no'` | `scalar` | `''` (empty string = unresolved)
- `can_close_early` — boolean
- `open_interest_fp` — FixedPointCount — "number of contracts bought on this market disconsidering netting"
- `notional_value_dollars` — FixedPointDollars — "The total value of a single contract at settlement in dollars"
- `previous_yes_bid_dollars`, `previous_yes_ask_dollars`, `previous_price_dollars` — FixedPointDollars — same quantities "a day ago"
- `liquidity_dollars` — FixedPointDollars, **DEPRECATED**: "will always return \"0.0000\"" (do not use for liquidity screening)
- `settlement_value_dollars` — FixedPointDollars, nullable — settlement value of YES/LONG side, only filled after determination
- `settlement_ts` — date-time, nullable — only for settled markets
- `expiration_value` — string — value considered for settlement
- `occurrence_datetime` — date-time, nullable
- `fee_waiver_expiration_time` — date-time, nullable — "Time when this market's fee waiver expires" (fee engine must check this per market)
- `early_close_condition` — string, nullable
- `strike_type` — enum: `greater` | `greater_or_equal` | `less` | `less_or_equal` | `between` | `functional` | `custom` | `structured`
- `floor_strike` / `cap_strike` — number (double), nullable
- `functional_strike` — string, nullable; `custom_strike` — object, nullable
- `rules_primary` / `rules_secondary` — string, plain-language terms
- `mve_collection_ticker` — string — "The ticker of the multivariate event collection" (present on combo markets)
- `mve_selected_legs` — array of `MveSelectedLeg` (combo legs)
- `primary_participant_key` — string, nullable
- `price_level_structure` — string — "Price level structure for this market, defining price ranges and tick sizes" (values per migration doc: `linear_cent`, `tapered_deci_cent`, `deci_cent`)
- `price_ranges` — array of `PriceRange` — "Valid price ranges for orders on this market"
- `is_provisional` — boolean — "If true, the market may be removed after determination if there is no activity on it"
- `exchange_index` — `ExchangeIndex` integer — "Identifier for an exchange shard. Defaults to 0 if unspecified. Note: currently only 0 supported." Example 0.

### PriceRange schema (required: `start`, `end`, `step`)
- `start` — string — "Starting price for this range in dollars"
- `end` — string — "Ending price for this range in dollars"
- `step` — string — "Price step/tick size for this range in dollars"
All strings in dollars. e.g. tapered market -> three ranges with steps "0.001" / "0.01" / "0.001".

### MveSelectedLeg schema (combo leg descriptor)
- `event_ticker` — string — selected event
- `market_ticker` — string — selected market
- `side` — string — side of the selected market
- `yes_settlement_value_dollars` — FixedPointDollars, nullable — settlement value of YES/LONG side, only filled after determination

### FixedPointCount schema (verbatim)
"Fixed-point contract count string (2 decimals, e.g., \"10.00\"; referred to as \"fp\" in field names). Requests accept 0-2 decimal places (e.g., \"10\", \"10.0\", \"10.00\"); responses always emit 2 decimals. Fractional contract values (e.g., \"2.50\") are supported; the minimum granularity is 0.01 contracts." Example: `'10.00'`.

---

## 7. RFQ/combo-specific fee treatment

Nothing RFQ- or combo-specific appears in any of these fee docs. What IS combo-relevant:
- Combo markets surface `mve_collection_ticker` + `mve_selected_legs` on the Market object; their fees resolve via event override -> series structure like any market.
- The fee accumulator's taker->maker carryover note is the only maker/taker distinction in the rounding doc; whether RFQ executions are charged as maker or taker (and to which counterparty) is not stated in these pages.
- The docs index lists related pages (other subtopics): `getting_started/rfqs.md`, `api-reference/communications/*` (Create Quote, Accept/Confirm Quote, etc.), `fix/rfq-messages.md`.

## 8. Traps / contradictions for implementers
- **Trade fee ceils to $0.0001 (centicent), NOT to the next cent.** The legacy assumption "fees round up to the next cent" is now only the *emergent* behavior for non-direct members via the rounding fee + floor-to-$0.01 balance mechanics, and the accumulator rebates the overpayment across fills. Modeling fees as ceil-to-cent per fill will overestimate multi-fill fee costs.
- **`revenue` is signed (negative for buyers)** and the floor is toward negative infinity — for a buyer this makes the balance debit larger; do not implement banker's or half-up rounding.
- All money and count values are **strings** on the wire (`_dollars`, `_fp`). Parse with Decimal, never float. `_fp` responses always have exactly 2 decimals; `_dollars` typically 4 (max precision 6).
- **`liquidity_dollars` is deprecated and always `"0.0000"`** despite being a required response field.
- `title`, `subtitle`, `expiration_time` on Market are deprecated; use `yes_sub_title`/`no_sub_title` and `expected_expiration_time`/`latest_expiration_time`.
- Tick size is **per market and per price range** (`price_ranges`), not global 1c. A quote priced off-grid in a `tapered_deci_cent` tail (e.g., 0.905 in the $0.90–$1.00 range is valid; 0.905 in the middle range would not be) must be validated against the ranges.
- `GET /series/fee_changes` has NO pagination and a `show_historical` flag; `GET /events/fee_changes` has cursor pagination (limit max 1000, default 100) and NO historical flag — the two fee-change endpoints are asymmetric.
- Event fee overrides are **nullable-means-cleared**: a change record with null `fee_type_override`/`fee_multiplier_override` REMOVES the override (fall back to series), it is not "no data".
- Fee changes are **scheduled** (`scheduled_ts`) — a fee engine must apply them at their effective time, not on poll time.
- `fee_waiver_expiration_time` on Market can make a market temporarily fee-free; ignoring it overstates costs.
- Fractional fills will show up even for integer orders — fee/PnL accounting must handle `_fp` values like `"1.55"`.
- Demo hosts are on `.kalshi.co`; both a dedicated and a "shared" host exist per environment.

## Critical facts (must get right)
- All price fields are strings with `_dollars` suffix (e.g. "0.1200"): fixed-point dollars, typically 4 decimals, wire max 6 decimals; all contract counts are strings with `_fp` suffix, always 2 decimals in responses, 0-2 decimals accepted in requests, minimum granularity 0.01 contracts.
- Tick size is per-market: read `price_level_structure` (`linear_cent` = 1c everywhere; `tapered_deci_cent` = 0.1c ticks in $0.00-$0.10 and $0.90-$1.00, 1c in between; `deci_cent` = 0.1c everywhere) and validate every quote price against the `price_ranges` array (`start`/`end`/`step` dollar strings) from GET /markets/{ticker}.
- Trade fee per fill is the fee-model fee rounded UP to the nearest $0.0001 (centicent) — NOT up to the next cent; then balance_change = revenue - trade_fee is floored toward negative infinity to the member's balance precision ($0.01 non-direct, $0.0001 direct), the shortfall is charged as a rounding_fee, and a per-order fee accumulator issues $0.01 rebates once accumulated rounding exceeds $0.01. Net fee = trade_fee + rounding_fee - rebate >= $0.00.
- The fee accumulator is per ORDER across all its fills, and carries over from taker fills to later maker fills of the same order.
- The actual fee formula constants (0.07 x C x P x (1-P) style) are NOT in docs.kalshi.com; the API only exposes `fee_type` enum (`quadratic` | `quadratic_with_maker_fees` | `flat`) + `fee_multiplier` (double) on Series, and points to https://kalshi.com/docs/kalshi-fee-schedule.pdf for the tables ('quadratic'=General Trading Fees Table, 'quadratic_with_maker_fees'=+Maker Fees section, 'flat'=Specific Trading Fees Table).
- Fee resolution is layered: event-level overrides (GET /trade-api/v2/events/fee_changes: `fee_type_override`/`fee_multiplier_override`, null = override CLEARED, fall back to series) on top of series fees (GET /trade-api/v2/series/fee_changes: `fee_type`, `fee_multiplier`, `scheduled_ts`; query params `series_ticker`, `show_historical` default false). Changes take effect at `scheduled_ts`, not immediately.
- GET /trade-api/v2/markets/{ticker} returns Market with combo linkage fields `mve_collection_ticker` and `mve_selected_legs` (each leg: `event_ticker`, `market_ticker`, `side`, `yes_settlement_value_dollars`), plus `fee_waiver_expiration_time` (market may be fee-free until then).
- `liquidity_dollars` on Market is deprecated and always returns "0.0000"; `title`, `subtitle`, `expiration_time` are also deprecated (use `yes_sub_title`/`no_sub_title`, `expected_expiration_time`/`latest_expiration_time`).
- Fills can be fractional (e.g. count_fp "1.55") even if you only place integer orders — accounting must use Decimal/scaled-integer (x100) arithmetic, never float.
- Prod REST base: https://api.elections.kalshi.com/trade-api/v2 (or https://external-api.kalshi.com/trade-api/v2); demo: https://demo-api.kalshi.co/trade-api/v2 (or https://external-api.demo.kalshi.co/trade-api/v2) — demo is .kalshi.co.

## Open questions (verify empirically on demo)
- Exact fee formula constants: is the quadratic model still fee = ceil_to_centicent(multiplier x 0.07 x C x P x (1-P))? What are the maker-fee rates under `quadratic_with_maker_fees` and the `flat` table values? Must be pulled from https://kalshi.com/docs/kalshi-fee-schedule.pdf (outside docs.kalshi.com) and verified against actual demo fill fees.
- What `fee_type` and `fee_multiplier` do sports combo/MVE series actually carry (e.g., is fee_multiplier 1.0 by default), and do MVE-collection-derived events routinely carry event-level overrides? Poll GET /series/{ticker}, /series/fee_changes, /events/fee_changes on demo/prod for the target series.
- RFQ/combo executions: are both sides charged, is the quoting maker charged maker or taker fees, and does the fee-waiver field apply? None of the fetched fee docs mention RFQ-specific fee treatment — verify empirically from demo fill records after an RFQ execution.
- Which `price_level_structure` do combo (MVE) markets use in practice (linear_cent vs tapered_deci_cent vs deci_cent), and can it differ per market within one collection? Read `price_ranges` off freshly created combo markets on demo.
- Are we a 'direct member' ($0.0001 balance precision) or 'non-direct' ($0.01)? This changes rounding-fee magnitude per fill; check actual balance deltas on demo/prod fills.
- Does GET /series/fee_changes with show_historical=false return only future-scheduled changes, or also the currently-effective one? And is the response ordered by scheduled_ts? Not specified — verify.
- GetMarket lists a 401 response but the spec has `security: []` — confirm whether market data endpoints (and the fee-changes endpoints) need auth headers in prod.
- Can `_dollars` fields in responses ever exceed 4 decimals (FixedPointDollars allows up to 6)? E.g., fee or settlement fields on fractional fills — affects Decimal parsing/validation strictness.
- Exact boundary semantics of tapered_deci_cent ranges: at the shared boundary prices ($0.10, $0.90), which step applies — is $0.105 valid? Resolve by inspecting actual `price_ranges` start/end values (inclusive/exclusive) on a tapered market.
- Does the fee accumulator survive order amends (Amend Order V2 keeps the same order?) and what happens to un-rebated accumulator remainder when an order is cancelled (forfeited?) — undocumented; measure on demo.
