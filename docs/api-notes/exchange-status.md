# Kalshi Exchange Status, Schedule, Maintenance & Pauses — Implementation Notes

Sources fetched 2026-07-05:
- `https://docs.kalshi.com/api-reference/exchange/get-exchange-status.md` (fetched OK)
- `https://docs.kalshi.com/getting_started/maintenance_and_pauses.md` (fetched OK)
- `https://docs.kalshi.com/api-reference/exchange/get-exchange-schedule.md` (fetched OK)
- Supporting: `https://docs.kalshi.com/api-reference/orders/create-order-v2.md`, `https://docs.kalshi.com/getting_started/rfqs.md`, `https://docs.kalshi.com/llms.txt`

## Base URLs (from Get Exchange Schedule server list — apply to both endpoints below)

| Environment | Base URL |
|---|---|
| Production | `https://external-api.kalshi.com/trade-api/v2` |
| Production (shared) | `https://api.elections.kalshi.com/trade-api/v2` |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` |
| Demo (shared) | `https://demo-api.kalshi.co/trade-api/v2` |

Note the demo TLD is `.co`, not `.com`.

---

## 1) GET `/exchange/status` — Get Exchange Status

- **Method/path:** `GET /exchange/status` (relative to `/trade-api/v2` base)
- **Auth:** none required. No parameters, no request body.
- **Response object:** `ExchangeStatus`

**Required fields:**
- `exchange_active` (boolean) — doc wording: "False if the core Kalshi exchange is no longer taking any state changes at all"
- `trading_active` (boolean) — doc wording: "True if we are currently permitting trading on the exchange"

**Optional fields:**
- `intra_exchange_transfers_active` (boolean) — status of transfer permissions
- `exchange_estimated_resume_time` (string, `date-time` format, **nullable**) — estimated maintenance-window completion time
- `exchange_index_statuses` (array of `ExchangeIndexStatus`) — per-shard breakdown

**`ExchangeIndexStatus` object:**
- `exchange_index` (integer) — identifier for exchange shard; **currently only `0` is supported**
- `exchange_active` (boolean)
- `trading_active` (boolean)
- `intra_exchange_transfers_active` (boolean)

**HTTP status codes:** `200` success; `500` internal server error; `503` service unavailable; `504` gateway timeout. **Trap:** the docs state all error responses return the *same `ExchangeStatus` schema* as the 200 response — do not assume a distinct error envelope on this endpoint.

**Semantics for a market maker:**
- `trading_active=false` with `exchange_active=true` = trading pause (order placement blocked, exchange still processes some state changes, e.g. cancels).
- `exchange_active=false` = full exchange pause ("no state changes at all").
- Gate quoting on `trading_active`, not merely `exchange_active` — `exchange_active=true` does NOT imply you can trade.

No JSON example was present on the doc page.

---

## 2) GET `/exchange/schedule` — Get Exchange Schedule

- **Method/path:** `GET /exchange/schedule`
- **Auth:** no security requirements specified.
- **Description (verbatim):** "Endpoint for getting the exchange schedule."
- **200 OK**, Content-Type `application/json`; **500** internal server error.

**Response schema:**
```
GetExchangeScheduleResponse
└── schedule (required)
    ├── standard_hours (required, array of WeeklySchedule)
    │   └── WeeklySchedule
    │       ├── start_time (date-time) — "When this weekly schedule is effective"
    │       ├── end_time (date-time) — "When this schedule no longer is effective"
    │       └── monday, tuesday, wednesday, thursday, friday, saturday, sunday (each an ARRAY of DailySchedule)
    │           └── DailySchedule
    │               ├── open_time (string) — "HH:MM in ET"
    │               └── close_time (string) — "HH:MM in ET"
    └── maintenance_windows (required, array of MaintenanceWindow)
        └── MaintenanceWindow
            ├── start_datetime (date-time) — "Start of maintenance window"
            └── end_datetime (date-time) — "End of maintenance window"
```

**Traps:**
- `open_time`/`close_time` are plain `"HH:MM"` strings **in Eastern Time**, not ISO timestamps and not UTC. You must do ET (America/New_York, DST-aware) conversion yourself.
- `standard_hours` is an **array** of `WeeklySchedule` entries, each with its own effective `start_time`/`end_time` range — the client must select the schedule whose effective range covers "now". Do not assume a single schedule object.
- Each weekday key maps to an **array** of `DailySchedule` — a day can have multiple open/close intervals.
- `maintenance_windows` uses `start_datetime`/`end_datetime` (note: different field names than WeeklySchedule's `start_time`/`end_time`).
- All times on this page are expressed in Eastern Time (ET).

No JSON example was present on the doc page.

---

## 3) Maintenance and Pauses (`getting_started/maintenance_and_pauses.md`)

**Routine maintenance window:** every **Thursday 3:00 AM – 5:00 AM ET**. During this window a **trading pause** takes effect. Occasionally, intensive work triggers a **full exchange pause** instead.

**Pause types:**
- **Trading Pause** (scheduled Thursday window): cannot place or modify orders; **cancellations ARE allowed**; existing orders stay on the book unless configured otherwise.
- **Exchange Pause** (rare — intensive scheduled maintenance or unscheduled outages): order placement and amendments blocked; per the doc, cancellations remain available (see open questions — this sits in tension with the `exchange_active=false` = "no state changes at all" wording on the status endpoint).

**Resting order behavior:** resting orders **persist on the book during BOTH pause types** by default. They are only auto-cancelled if the order was created with the cancel-on-pause flag.

**Cancel-on-pause configuration:**
- REST: field `cancel_order_on_pause` in order-creation requests.
- FIX: Tag `21006` on New Order Single messages (`35=D`).
- Enabled (`true`/`Y`): order automatically cancels when **any** pause begins.
- Disabled (`false`/`N`): order stays active and resumes normal operation when trading resumes.

Exact field description from Create Order (V2) (`POST /trade-api/v2/portfolio/events/orders`, current — the legacy `/portfolio/orders` endpoint "will be deprecated no earlier than May 6, 2026"): `cancel_order_on_pause` (boolean, default implied false) — "If this flag is set to true, the order will be canceled if the order is open and trading on the exchange is paused for any reason."

**Recommended client behavior (from docs):** prepare for session disconnections during Thursday maintenance windows; reconnect after 5:00 AM ET.

---

## 4) RFQ/quote behavior during pauses — NOT DOCUMENTED

`getting_started/rfqs.md` contains **no information** about pauses, maintenance, or exchange status effects on RFQs/quotes. The maintenance doc only discusses *orders*. Timing constants found on the RFQ page (relevant because a pause overlapping these timers is undefined behavior):
- Standard markets: **30 s confirmation window**, **15 s execution timer**
- High Volatility Markets (HVM): **3 s confirmation window**, **1 s execution timer**
(These are windows after acceptance and after confirmation respectively, after which orders are placed on the public book.)

---

## Recommended market-maker behavior (synthesis)

1. Poll `GET /exchange/status` (unauthenticated, cheap) and hard-gate all quote submission on `trading_active == true`.
2. Treat `exchange_active == false` as full outage: stop everything, back off, expect API/session errors.
3. Pull `GET /exchange/schedule` at startup and periodically; pre-emptively stop quoting before each entry in `maintenance_windows` and before Thursday 3:00 AM ET; resume after 5:00 AM ET (or after `exchange_estimated_resume_time` if populated, re-checking status first).
4. Set `cancel_order_on_pause=true` on any resting maker orders you do not want alive through a pause (stale quotes surviving a 2-hour pause into a resumed market are an adverse-selection hazard).
5. Expect WebSocket/FIX session disconnects during the Thursday window; build reconnect-with-backoff and full state resync on resume.
6. On 500/503/504 from the status endpoint, attempt to parse the body as `ExchangeStatus` but do not rely on it; treat unreachable status as trading-inactive.

## Critical facts (must get right)
- GET /exchange/status (no auth) returns ExchangeStatus with two REQUIRED booleans: exchange_active ('False if the core Kalshi exchange is no longer taking any state changes at all') and trading_active ('True if we are currently permitting trading on the exchange'). Gate quoting on trading_active — exchange_active=true does NOT mean trading is allowed.
- Optional ExchangeStatus fields: intra_exchange_transfers_active (boolean), exchange_estimated_resume_time (nullable date-time string, estimated maintenance completion), exchange_index_statuses (array of {exchange_index int — only 0 supported, exchange_active, trading_active, intra_exchange_transfers_active}).
- Error responses from /exchange/status (500/503/504) are documented to use the SAME ExchangeStatus schema as 200 — no separate error envelope.
- Routine maintenance is every Thursday 3:00–5:00 AM ET and imposes a trading pause; intensive work may escalate to a full exchange pause. Docs recommend expecting session disconnections during this window and reconnecting after 5:00 AM ET.
- During both pause types: order placement and modification are blocked but cancellations are allowed, and resting orders PERSIST on the book by default — they are only auto-cancelled if created with cancel_order_on_pause=true (REST field on order creation, e.g. Create Order V2 POST /trade-api/v2/portfolio/events/orders; FIX Tag 21006 on 35=D). Exact description: 'If this flag is set to true, the order will be canceled if the order is open and trading on the exchange is paused for any reason.'
- GET /exchange/schedule (no auth) returns GetExchangeScheduleResponse.schedule with required standard_hours (array of WeeklySchedule: start_time/end_time date-time effective range, plus monday..sunday each an ARRAY of DailySchedule {open_time, close_time}) and required maintenance_windows (array of {start_datetime, end_datetime}).
- DailySchedule open_time/close_time are plain 'HH:MM' strings in Eastern Time (ET), NOT ISO timestamps and NOT UTC — client must do DST-aware America/New_York conversion. MaintenanceWindow uses different field names (start_datetime/end_datetime) than WeeklySchedule (start_time/end_time).
- Demo base URLs are on the .co TLD: https://external-api.demo.kalshi.co/trade-api/v2 and https://demo-api.kalshi.co/trade-api/v2; production is https://external-api.kalshi.com/trade-api/v2 and https://api.elections.kalshi.com/trade-api/v2.
- The docs say NOTHING about what happens to open RFQs or resting RFQ quotes during a pause — only orders are covered. RFQ timing constants that a pause could interact with: 30s confirmation window + 15s execution timer (standard), 3s + 1s (High Volatility Markets).
- Create Order V2 path is POST /trade-api/v2/portfolio/events/orders (current); the legacy /portfolio/orders endpoint will be deprecated no earlier than May 6, 2026.

## Open questions (verify empirically on demo)
- RFQ/quote pause semantics are undocumented: do open RFQs and resting quotes persist through a trading pause, get auto-cancelled, or have their confirmation/execution timers frozen? Does cancel_order_on_pause apply to RFQ quotes at all, or only to regular limit orders? Needs demo-window observation (e.g., Thursday 3-5 AM ET on demo, if demo mirrors the schedule).
- Contradiction to verify: exchange_active=false is described as 'no state changes at all', yet the maintenance doc says cancellations remain available during an exchange pause. Which wins during a real exchange pause — and does the REST API even respond?
- During a trading pause, what exact error code/body do quote-creation and order-creation calls return? (Needed to distinguish pause rejections from other errors in the bot's error handling.)
- Do 500/503/504 responses from /exchange/status actually populate a parseable ExchangeStatus body in practice, and what does the endpoint return mid-pause (trading_active=false confirmed? exchange_estimated_resume_time populated for scheduled vs unscheduled pauses?).
- Is there a WebSocket message/channel announcing pause start/end (websockets/communications.md or another channel), or is polling /exchange/status the only signal? Polling rate limits on the unauthenticated status endpoint are also undocumented.
- standard_hours selection logic: can multiple WeeklySchedule entries overlap, and can a weekday array contain multiple open/close intervals in practice? What does close_time '24:00' or an overnight session look like?
- Does the demo environment observe the same Thursday 3:00-5:00 AM ET maintenance window and the same schedule/maintenance_windows content as prod, or does demo have its own windows?
- Do combo/multivariate (parlay) markets share the exchange-wide standard_hours, or do they have separate per-market open/close times that override the exchange schedule?
- Does 'cannot modify orders' during a pause include the amend/decrease endpoints only, or also cancel-replace? Confirm cancels work via all protocols (REST, WebSocket order entry if any, FIX) during a pause.
- exchange_estimated_resume_time format specifics (timezone offset, ISO 8601 exactness) and whether it is null vs absent when no maintenance is scheduled.
