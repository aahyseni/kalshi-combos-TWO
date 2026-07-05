# Kalshi Live Data + Milestones — Exhaustive Notes for RFQ Market Maker (Courtside Protection)

Source: docs.kalshi.com, fetched 2026-07-05. Two of the originally-cited URLs were STALE:
`/api-reference/live-data/get-milestones.md` and `/api-reference/live-data/get-milestone.md` both **404**. Current locations (per llms.txt): `/api-reference/milestone/get-milestones.md` and `/api-reference/milestone/get-milestone.md`. Two additional live-data endpoints exist that were not in the original list: `get-live-data-with-type` (legacy) and `get-multiple-live-data` (batch).

## Base URLs (all endpoints below)

- Production: `https://external-api.kalshi.com/trade-api/v2`
- Production (alternate/shared): `https://api.elections.kalshi.com/trade-api/v2`
- Demo: `https://external-api.demo.kalshi.co/trade-api/v2`
- Demo (alternate/shared): `https://demo-api.kalshi.co/trade-api/v2`

Note the TLD difference: prod is `.com`, demo is `.co`.

---

## 1. GET `/live_data/milestone/{milestone_id}` — Get Live Data (CURRENT endpoint)

**Auth: NONE required** (security: []).

Path parameters:
| Name | Type | Required | Description |
|---|---|---|---|
| `milestone_id` | string | Yes | Milestone ID |

Query parameters:
| Name | Type | Required | Default | Description |
|---|---|---|---|---|
| `include_player_stats` | boolean | No | `false` | "When true, includes player-level statistics in the live data response. Supported for Pro Football, Pro Basketball, and College Men's Basketball milestones that have player ID mappings configured." |

Response 200 — `GetLiveDataResponse`:
```
{
  "live_data": {
    "type": string,        // required — "Type of live data"
    "details": object,     // required — "Live data details as a flexible object" (NO SCHEMA DOCUMENTED)
    "milestone_id": string // required — "Milestone ID"
  }
}
```

Errors: `404` Live data not found; `500` Internal server error.

**TRAP:** `details` is an undocumented free-form object. The docs give NO field names for game clock, score, or status. Its shape varies by `type` and must be discovered empirically per sport.

---

## 2. GET `/live_data/{type}/milestone/{milestone_id}` — Get Live Data (with type) — **LEGACY / deprecated-in-practice**

Docs: "This is the legacy endpoint that requires a type path parameter." Docs explicitly recommend using `/live_data/milestone/{milestone_id}` instead. **Do not build on this one.**

Path parameters: `type` (string, required, "Type of live data" — values NOT enumerated in docs), `milestone_id` (string, required).
Query: same `include_player_stats` (boolean, optional, default `false`).
Response/errors: identical `GetLiveDataResponse` shape (`live_data` with `type`, `details`, `milestone_id`), 404, 500. Auth: none.

---

## 3. GET `/live_data/batch` — Get Multiple Live Data (batch)

**Auth: NONE required.** "Get live data for multiple milestones."

Query parameters:
| Name | Type | Required | Constraints / Default |
|---|---|---|---|
| `milestone_ids` | array of strings | Yes | **Max 100 items** |
| `include_player_stats` | boolean | No | Default `false` |

Response 200:
```
{
  "live_datas": [
    { "type": string, "details": object, "milestone_id": string }
  ]
}
```
Note the plural field is **`live_datas`** (not `live_data`). Each element has the same three required fields as the single endpoint.

Errors documented: only `500` Internal server error (no 404 documented — behavior for unknown/partial IDs is unspecified).

**Rate-limit trap:** the rate-limits doc says "Batch endpoints charge per item, not per batch" — so a 100-id batch call may cost 100× the per-item token cost, not 1 request's worth (if these unauthenticated endpoints are token-metered at all; see §7).

---

## 4. GET `/live_data/milestone/{milestone_id}/game_stats` — Get Game Stats (play-by-play)

**Auth: NONE required** (security: []).

Description (verbatim): "Get play-by-play game statistics for a specific milestone. Supported sports: Pro Football, College Football, Pro Basketball, College Men's Basketball, College Women's Basketball, WNBA, Soccer, Pro Hockey, and Pro Baseball."

Returns **null** for unsupported milestone types or milestones **lacking a Sportradar ID** (so the underlying feed provider is Sportradar).

Path parameter: `milestone_id` (string, required).

Response 200 (`GetGameStatsResponse`, application/json):
- `pbp` (PlayByPlay object): play-by-play data organized by period
  - `periods` (array of period objects)
    - `events` (array of event objects "with additional properties" — i.e., open/flexible schema)

Errors: `404` Game stats not found; `500` Internal server error. Tag: `live-data`.

**TRAP:** the inner event/period schemas are not documented — Sportradar-shaped payloads, discover empirically.

---

## 5. GET `/milestones` — Get Milestones (list)

**Auth: none specified** (but `401` is a documented error code — see open questions).

Query parameters:
| Name | Type | Required | Description |
|---|---|---|---|
| `limit` | integer | **Yes** (min 1, **max 500**) | "Number of milestones to return per page" |
| `minimum_start_date` | string (date-time) | No | "Minimum start date to filter milestones. Format RFC3339 timestamp" |
| `category` | string | No | "Filter by milestone category. E.g. Sports, Elections, Esports, Crypto." |
| `competition` | string | No | "Filter by competition. E.g. Pro Football, Pro Basketball (M), Pro Baseball, Pro Hockey, College Football." |
| `source_id` | string | No | "Filter by source id" |
| `type` | string | No | "Filter by milestone type. E.g. football_game, basketball_game, soccer_tournament_multi_leg, baseball_game, hockey_match, political_race." |
| `related_event_ticker` | string | No | "Filter by related event ticker" |
| `cursor` | string | No | "Pagination cursor. Use the cursor value returned from the previous response to get the next page of results" |
| `min_updated_ts` | integer (int64) | No | "Filter milestones with metadata updated after this Unix timestamp (in seconds)." |

**Note:** `limit` is documented as REQUIRED — unusual; pass it explicitly. Also note the competition enum-example "Pro Basketball (M)" includes a parenthesized suffix — copy exactly.

Response 200 — `GetMilestonesResponse`:
- `milestones` (array of Milestone, required)
- `cursor` (string, optional): "Cursor for pagination."

**Milestone object** (shared by all milestone endpoints and by Get Events `with_milestones`):
| Field | Type | Notes |
|---|---|---|
| `id` | string, required | "Unique identifier for the milestone." — this is the `milestone_id` used by all `/live_data` endpoints |
| `category` | string, required | e.g. "Sports" |
| `type` | string, required | e.g. "football_game" |
| `start_date` | date-time, required | "Start date of the milestone." |
| `end_date` | date-time, nullable | "End date of the milestone, if any." |
| `related_event_tickers` | array of strings, required | broader events tied to the same occurrence |
| `title` | string, required | |
| `notification_message` | string, required | |
| `source_id` | string, nullable | "Source id of milestone if available." (Sportradar ID presumably lives here / in source_ids) |
| `source_ids` | object, optional | map of additional source IDs (string→string) |
| `details` | object, required | "Additional details about the milestone." — type-specific JSON, undocumented shape |
| `primary_event_tickers` | array of strings, required | "List of event tickers directly related to the outcome of this milestone." |
| `last_updated_ts` | date-time, required | "Last time this structured target was updated." (doc string says "structured target" — likely copy-paste artifact) |

Errors: `400`, `401`, `500`.

---

## 6. GET `/milestones/{milestone_id}` — Get Milestone (single)

**Auth: none specified.** Path param: `milestone_id` (string, required).

Response 200:
```json
{
  "milestone": {
    "id": "string",
    "category": "string",
    "type": "string",
    "start_date": "date-time",
    "end_date": "date-time or null",
    "related_event_tickers": ["string"],
    "title": "string",
    "notification_message": "string",
    "source_id": "string or null",
    "source_ids": {"key": "string"},
    "details": {},
    "primary_event_tickers": ["string"],
    "last_updated_ts": "date-time"
  }
}
```
Errors: `400`, `401`, `404`, `500`.

---

## 7. Mapping markets/events → live data (the courtside-protection wiring)

Chain: **market → event ticker → milestone → live data**.

Three ways to get the event→milestone mapping:
1. `GET /milestones?related_event_ticker={EVENT_TICKER}&limit=...` — direct reverse lookup from an event ticker to its milestone(s).
2. `GET /events?with_milestones=true` — `with_milestones` (boolean, default `false`): "If true, includes related milestones as a field alongside events." Response gains a top-level `milestones` (array of Milestone objects): "Array of milestones related to the events." Same Milestone schema as above. (Other Get Events params for reference: `limit` int default 200, `cursor`, `with_nested_markets` bool default false, `status` enum: unopened|open|closed|settled, `series_ticker`, `tickers` comma-separated, `min_close_ts`, `min_updated_ts`.)
3. `GET /milestones` bulk-paged with `category=Sports` (+ optional `competition`/`type` filters), then index locally by `related_event_tickers` / `primary_event_tickers`.

Then poll `GET /live_data/batch?milestone_ids=...` (≤100 per call) with the collected `milestone.id`s.

From the Targets & Milestones conceptual page (`/getting_started/targets_and_milestones.md`):
- "Milestones connect Kalshi events to real-world occurrences"; structured targets identify entities (teams, players).
- "If you need to group related events, start with milestones. If you need to identify a team, player, or other entity referenced by a market, use structured targets."
- "In practice, `related_event_tickers` is often a superset of `primary_event_tickers`."
- Markets reference structured targets via `custom_strike` when `strike_type` is `"structured"`, e.g.:
```json
{
  "strike_type": "structured",
  "custom_strike": {
    "basketball_team": "2ef4d31c-0b46-4f43-a403-f44d62489034"
  }
}
```
The `custom_strike` value is a structured-target ID resolvable via Get Structured Target. Numeric strikes use `floor_strike`/`cap_strike` instead. Structured target fields: `id`, `name`, `type`, `details` (flexible JSON by target type), `source_id`, `source_ids`.

For combo/parlay markets: each leg's underlying event ticker maps to its own milestone; a multi-leg combo will need N milestone subscriptions (one per leg's game).

---

## 8. Sports / feed coverage summary

- Game-stats (play-by-play) supported sports (verbatim list): Pro Football, College Football, Pro Basketball, College Men's Basketball, College Women's Basketball, WNBA, Soccer, Pro Hockey, Pro Baseball.
- Player stats (`include_player_stats=true`) supported only for: Pro Football, Pro Basketball, College Men's Basketball — and only "milestones that have player ID mappings configured."
- Milestone `type` examples given: `football_game`, `basketball_game`, `soccer_tournament_multi_leg`, `baseball_game`, `hockey_match`, `political_race`.
- Milestone `category` examples: Sports, Elections, Esports, Crypto.
- Underlying data provider: Sportradar (game_stats "Returns null for ... milestones lacking a Sportradar ID").

## 9. Rate limits (from /getting_started/rate_limits.md)

- Token-bucket model: "Every authenticated request costs tokens." Most endpoints cost 10 tokens by default; non-default costs via `GET /account/endpoint_costs`.
- Two independent buckets: **Read** (GET + non-write) and **Write** (order placement, amends, cancels, order groups, **the RFQ quote flow**, block trade proposal accepts). Live-data/milestone calls are Read-bucket.
- Tier budgets (tokens/second, Read/Write): Basic 200/100, Advanced 300/300, Expert 600/600, Premier 1000/1000, Paragon 2000/2000, Prime 4000/4000, Prestige 6000/8000. At 10 tokens/request, Basic = ~20 reads/sec.
- Burst: Advanced+ hold "up to two seconds of budget" → burst up to 2× per-second budget.
- 429 body: `{"error": "too many requests"}`. "429 responses do not currently include `Retry-After` or `X-RateLimit-*` headers." — implement your own backoff/pacing.
- "Batch endpoints charge per item, not per batch."
- No per-second resets; tokens accumulate while idle.

## 10. Latency / push-vs-poll

- **No latency SLA or update-frequency figure is documented anywhere** for live data or game stats.
- **No WebSocket channel exists for live data or milestones.** The documented WS channels relevant to live activity are only market-ticker, public-trades, user-fills (i.e., market-derived, not score-derived). Courtside protection must be **HTTP polling** of `/live_data/batch`, budgeted against the Read token bucket (with per-item batch charging).

## 11. Deprecated vs current — quick table

| Endpoint | Status |
|---|---|
| GET `/live_data/milestone/{milestone_id}` | Current, preferred |
| GET `/live_data/{type}/milestone/{milestone_id}` | Legacy — docs say use the untyped path |
| GET `/live_data/batch` | Current |
| GET `/live_data/milestone/{milestone_id}/game_stats` | Current |
| GET `/milestones`, GET `/milestones/{milestone_id}` | Current — note URL docs moved from `api-reference/live-data/` to `api-reference/milestone/` |

## Critical facts (must get right)
- All live-data and milestone endpoints require NO authentication (security: []) and exist on both prod (external-api.kalshi.com/trade-api/v2, api.elections.kalshi.com/trade-api/v2) and demo (external-api.demo.kalshi.co/trade-api/v2, demo-api.kalshi.co/trade-api/v2). Prod TLD is .com, demo is .co.
- Preferred live-data endpoint is GET /live_data/milestone/{milestone_id}; GET /live_data/{type}/milestone/{milestone_id} is explicitly legacy. Batch is GET /live_data/batch?milestone_ids=... with a HARD MAX of 100 milestone IDs per call; its response field is plural 'live_datas'.
- The live_data payload is exactly three fields: type (string), details (object), milestone_id (string). 'details' is a documented-as-flexible object with NO schema — game clock/score/status field names are NOT in the docs and must be reverse-engineered per sport.
- Market-to-live-data mapping chain: market -> event_ticker -> milestone -> milestone.id -> /live_data. Reverse lookup: GET /milestones?related_event_ticker={ticker}, or GET /events?with_milestones=true (adds top-level 'milestones' array), then use milestone.id as milestone_id. Milestone has both primary_event_tickers (outcome-defining) and related_event_tickers (superset in practice).
- GET /milestones REQUIRES the 'limit' query param (min 1, max 500) and paginates via 'cursor'. Filters: category (Sports/Elections/Esports/Crypto), competition (e.g. 'Pro Football', 'Pro Basketball (M)'), type (e.g. football_game, basketball_game, hockey_match), related_event_ticker, source_id, minimum_start_date (RFC3339), min_updated_ts (Unix seconds).
- Game stats (play-by-play): GET /live_data/milestone/{milestone_id}/game_stats, supported ONLY for Pro Football, College Football, Pro Basketball, College Men's Basketball, College Women's Basketball, WNBA, Soccer, Pro Hockey, Pro Baseball; returns null when the milestone type is unsupported or the milestone lacks a Sportradar ID. Response: pbp.periods[].events[] (open schema).
- include_player_stats=true (default false) works only for Pro Football, Pro Basketball, and College Men's Basketball milestones that have player ID mappings configured.
- There is NO WebSocket channel for live scores/milestones — courtside protection must poll HTTP. No latency SLA or update frequency is documented anywhere.
- Rate limiting: token-bucket, most endpoints cost 10 tokens; Basic tier = 200 read tokens/sec (~20 reads/sec). Batch endpoints charge PER ITEM, not per call — a 100-id /live_data/batch call may cost 100 items' worth of tokens. 429 returns {"error": "too many requests"} with NO Retry-After or X-RateLimit-* headers; implement your own pacing.
- The old doc URLs /api-reference/live-data/get-milestones.md and /api-reference/live-data/get-milestone.md are dead (404); milestone docs live under /api-reference/milestone/. Milestone API paths themselves are GET /milestones and GET /milestones/{milestone_id}.

## Open questions (verify empirically on demo)
- What is the actual schema of live_data.details per sport/type (field names for score, game clock, period/quarter, game status)? Docs say only 'flexible object' — must capture real payloads on demo/prod for each sport we quote.
- What are the valid values of live_data.type (the string enumerating live-data kinds, also the path param of the legacy endpoint)? Never enumerated in docs.
- What is the real update latency/refresh cadence of /live_data and /game_stats relative to the live game (Sportradar-fed)? Nothing documented — measure empirically, since it bounds how much courtside protection the feed actually provides.
- Do unauthenticated live-data requests count against the account token bucket at all (rate-limits doc says 'every AUTHENTICATED request costs tokens'), and is there a separate undocumented IP-based limit for anonymous calls? Also: exact token cost per item for /live_data/batch (verify via GET /account/endpoint_costs).
- How does /live_data/batch behave for unknown, expired, or mixed-valid milestone_ids — partial results, omitted entries, or error? Only a 500 error is documented, no 404.
- GET /milestones documents a 401 error despite no auth requirement — confirm anonymous access works on both demo and prod, and whether demo has live-data populated at all (demo may have no real Sportradar feeds, which would force prod-based read-only testing).
- Does live_data exist for milestones without a Sportradar ID (e.g., score-only vs full pbp), and does demo mirror prod's milestone IDs? Milestone IDs are environment-specific presumably — verify.
- For combo/parlay legs: are event tickers for every sports leg reliably present in some milestone's related_event_tickers/primary_event_tickers, and how quickly after event creation does the milestone mapping appear (min_updated_ts polling cadence for discovering new games)?
