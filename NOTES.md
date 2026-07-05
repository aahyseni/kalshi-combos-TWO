# NOTES.md — doc-verified exchange mechanics

Facts verified against Kalshi's documentation, and every place the docs disagree
with the build prompt's assumptions. Where docs and prompt conflict, **docs win**.
Full per-topic notes: `docs/api-notes/` (13 topics + SUMMARY.md with the
gap-check results).

## Discrepancies / refinements (prompt vs docs)

| Prompt said | Docs say | Consequence |
|---|---|---|
| "yes_bid + no_bid > $1 is rejected" | Constraint is `yes_bid + no_bid <= $1` (create-quote.md) | same intent; spread = $1 − sum |
| "verify whether an explicit decline/delete is possible post-accept" | **No decline mechanism exists post-accept** anywhere in REST or FIX docs; lapse of the confirm window voids automatically and silently | last-look decline = deliberately not confirming; whether DELETE succeeds in `accepted` status is queued for empirical check (do not rely on it) |
| "fills appear in GET /portfolio/fills (match on creator_order_id)" | REST Fill has **no** client_order_id field; the join is WS `quote_executed.order_id` (== quote's `creator_order_id`) → `GET /portfolio/fills?order_id=X` | fills reconciliation is a two-step join; prefer WS `fill` channel (carries `client_order_id` + `post_position_fp`), REST as reconciliation |
| "determine whether the maker ever needs to call CreateMarketInMultivariateEventCollection" | The RFQ carries the `market_ticker` of an already-created MVE market — creation is the requester's problem. `GET /markets/{ticker}` works unauthenticated on combo tickers | maker never creates markets; metadata/grid comes from GET /markets |
| Quote replacement: "a new quote on the same RFQ replaces your previous one" | Confirmed (FIX doc phrases it as auto-cancel of the existing quote) | reprice = just send the new quote |
| HVM timing 3s confirm / 1s execution | Confirmed; "all combo markets qualify as HVMs" | hot-path budget stands |
| (not in prompt) | **FIX `PreferBetterQuote`**: a requester accepting a *competitor's* quote can be routed to OURS if ours is at least as good | every open quote is instantly executable at ANY moment — mass-acceptance worst case is not hypothetical even without a market gap |
| (not in prompt) | RFQ quote flow bills against the **WRITE** token bucket; create/delete/get quote cost 2 tokens each; most other endpoints 10 | at Basic tier (100 write tokens/s) ≈ 50 quote ops/s ceiling; check `GET /account/endpoint_costs` at startup |
| "implement fee module from the published fee schedule" | The authoritative fee-schedule PDF is behind a bot-checkpoint (fetch returns 429). Secondary sources: taker `ceil(0.07·C·P·(1−P))`, maker `ceil(0.0175·C·P·(1−P))` on maker-fee series; per-series `fee_type` ∈ {quadratic, quadratic_with_maker_fees, flat} + `fee_multiplier` | **human must download the PDF manually**; RFQ fee side attribution (is the confirming maker `is_taker=false`?) is queued for Phase 2.5 ground truth |
| (not in prompt) | `GET /portfolio/balance` returns **cents**, not centi-cents | wire-boundary conversion must use `cc_from_cents` |

## Empirical verification queue (demo / Phase 2.5 ground truth)

From `docs/api-notes/SUMMARY.md` (gap check, unresolved):

1. Combo (KXMVE) markets' actual `price_level_structure` / `price_ranges` grid.
2. DELETE quote behavior in `accepted` status; terminal quote status after a
   deliberately lapsed 3s window.
3. RFQ execution fee treatment: maker's fill `is_taker` + `fee_cost`, per-series
   fee_type/multiplier on sports combo series. **Reconciliation gate material.**
4. `rest_remainder` semantics (REST "rest the remainder" vs FIX "allow partial
   fills" — genuinely conflicting descriptions).
5. `target_cost_dollars` → contract count formula (regress from
   `yes_contracts_fp`/`no_contracts_fp` on real quotes).
6. Demo feature parity for the whole combo RFQ path + real HVM timer measurement.
7. Clock-skew tolerance for `KALSHI-ACCESS-TIMESTAMP` (bisect the 401 boundary).
8. Whether `orderbook_delta` WS accepts MVE combo tickers.
9. Actual token costs for accept/confirm/create-RFQ (`GET /account/endpoint_costs`).
10. CreateMarketInMVEC idempotency + 5000/week limit scope (requester-side, low
    priority for us).
11. Quote/RFQ TTLs and 7-day retention of closed RFQs (persist everything locally
    at creation time regardless).
12. Whether `rfq_deleted` fires on full execution; `creator_id` anonymization on
    the communications channel.
13. **accepted_side economics round trip** (docs strongly indicate:
    `accepted_side="yes"` ⇒ maker buys YES at `yes_bid` — but this becomes code
    only via the Phase 2.5 fixture).

## Assumption audit

Standing rule (CLAUDE.md Quiet-failure defenses #6): every phase appends its
embedded domain assumptions here. Tags: `doc:<page>` (verified against docs),
`fixture:ground_truth` (verified against recorded exchange behavior),
`UNVERIFIED` (human reviews before next phase).

### Phase 0 + early math modules (2026-07-05)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| A1 | Signed message = `ts_ms + METHOD + full_path`, query stripped, body unsigned; RSA-PSS/SHA256/MGF1-SHA256/salt=DIGEST_LENGTH; standard base64 | `exchange/auth.py` | doc:quick_start_authenticated_requests |
| A2 | Timestamp header is Unix **milliseconds** as string | `exchange/auth.py` | doc:quick_start_authenticated_requests |
| A3 | WS handshake auth = same 3 headers, signed path `/trade-api/ws/v2` | `exchange/ws.py` | doc:websocket-connection |
| A4 | Base URLs: demo `external-api.demo.kalshi.co`, prod `external-api.kalshi.com` (+ ws hosts) | `ops/config.py` | doc:api_environments |
| A5 | Quote wire fields: `rfq_id`, `yes_bid`, `no_bid` (fixed-point dollar strings), required `rest_remainder`; `"0"` declines a side; both-zero invalid | `exchange/rest.py` | doc:create-quote |
| A6 | 4-decimal dollar strings (centi-cent precision) are valid wire values (docs allow up to 6dp) | `core/money.py`, `exchange/rest.py` | doc:create-quote |
| A7 | Confirm = `PUT /communications/quotes/{id}/confirm`, empty body, 204, starts execution timer | `exchange/rest.py` | doc:confirm-quote |
| A8 | WS message envelope `{"type", "sid", "seq", "msg"}`; commands `{"id", "cmd", "params"}`; server pings every 10s | `exchange/ws.py` | doc:asyncapi + websocket-connection |
| A9 | $1 = 10,000 centi-cents internal representation | `core/money.py` | internal convention (not an exchange fact) |
| A10 | Combo YES contract pays **product of leg settlement values** (values in [0,1]), capped at $1 | `sim/engine.py` | doc:rfqs.md + get-market (MveSelectedLeg settlement value) |
| A11 | Combo **NO** contract pays $1 − product | `sim/engine.py` | **UNVERIFIED** — plausible complement, but scalar-settlement NO payout must be confirmed in Phase 2.5 ground truth |
| A12 | P&L sign convention: long-YES P&L/contract = payout − price; long-NO = (1−payout) − price | `sim/engine.py` | internal definition — but the MAPPING from `accepted_side` to which side WE end up long is **deliberately not in code yet**; lands only in `core/conventions.py` from the Phase 2.5 fixture |
| A13 | Gaussian copula: leg i YES iff Z_i ≤ Φ⁻¹(p_i); joint = MVN CDF | `pricing/copula.py` | modeling choice (not an exchange fact) |
| A14 | Devig applies only to external odds; Kalshi legs are vig-free (yes+no=$1) | `pricing/devig.py`, `pricing/normalize.py`, arch test | operator directive + doc:orderbook structure |
| A15 | Kalshi rejects `yes_bid + no_bid > $1` | `exchange/rest.py` (not enforced client-side yet) | doc:create-quote / rfqs.md |

**UNVERIFIED rows for human review before Phase 3: A11.** (A12's dangerous half
is parked by design until Phase 2.5.)

### Phase 1 — market data (2026-07-05)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| B1 | Book is bids-only both sides; YES ask = $1 − best NO bid; best bid = highest price (arrays ascending) | `marketdata/orderbook.py` | doc:orderbook_responses |
| B2 | WS snapshot sides are `yes_dollars_fp`/`no_dollars_fp` (absent = empty side); REST uses `yes_dollars`/`no_dollars` — different names, same shape | `marketdata/feed.py` | doc:orderbook-updates vs get-market-orderbook |
| B3 | Delta semantics: `new_count = old + delta_fp` at `price_dollars` on `side`; count 0 removes the level; negative count = missed message ⇒ treated as gap | `marketdata/orderbook.py` | doc:orderbook-updates (zero-removal itself queued for empirical check) |
| B4 | `seq` is per-`sid` and control acks (`ok`/`unsubscribed`) consume seq slots; after a detected gap we re-adopt the next observed seq as baseline (exact contract around `get_snapshot` undocumented) | `marketdata/feed.py` | doc:orderbook-updates + **UNVERIFIED** (baseline re-adoption is defensive design pending demo verification) |
| B5 | `update_subscription {action: get_snapshot}` returns fresh snapshots without changing the subscription — used as the resync primitive | `marketdata/feed.py` | doc:orderbook-updates |
| B6 | `use_yes_price=false` pinned in subscribe params (server default will flip) | `marketdata/feed.py` | doc:websockets subscribe schema |
| B7 | Grid lattice = `start + k·step` per range, endpoints inclusive; multi-range (tapered) supported; boundary semantics at range joins | `marketdata/grid.py` | **UNVERIFIED** — queued: read real KXMVE `price_ranges` + probe an off-grid quote on demo |
| B8 | Counts are 2-dp fixed-point ("13.00"); held as integer centi-contracts; centi-contracts × centi-cents = micro-dollars | `core/quantity.py` | doc:orderbook_responses (internal unit identity) |
| B9 | Quiet book ≠ stale feed: freshness gate = feed health (WS traffic ≤30s + seq continuity) AND book validity; per-book change age feeds velocity/in-play logic only | `marketdata/feed.py`, `orderbook.py` | internal design |
| B10 | Market metadata: `GET /markets/{ticker}` wraps payload in `{"market": ...}`; `close_time`/`expected_expiration_time` RFC3339 | `marketdata/metadata.py` | doc:get-market |

**UNVERIFIED rows for human review before Phase 3: B4 (gap-recovery seq
contract), B7 (combo grid structure).** Both are on the Phase 2.5 empirical
list and both fail safe (gap ⇒ invalidate + cancel-all; unknown grid ⇒
no-quote).
