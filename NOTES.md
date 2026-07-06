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

## Phase 2.5 ground truth — EXECUTED 2026-07-05 (single-market pass)

Full RFQ round trips on demo (`KXMLBGAME-26JUL081840NYYTB-NYY`, two accounts,
1.00 contract; recordings in `tests/fixtures/ground_truth/`):

| Fact | Evidence |
|---|---|
| `accepted_side="yes"` ⇒ maker LONG YES at `yes_bid` | maker fill `outcome_side=yes, yes_price=0.4800, book_side=bid`; maker balance −$0.48; position_fp +1.00 |
| `accepted_side="no"` ⇒ maker LONG NO at `no_bid` | maker fill `outcome_side=no, no_price=0.4800`; netted the +1 YES position to 0 (**positions are SIGNED per market and NET across yes/no**) |
| Maker pays own bid | balance debit exactly bid × qty |
| **Maker fee = $0 on RFQ fills** (`is_taker=false`, `fee_cost=0.000000`) | both fills; requester (taker) paid $0.0175 = ceil(0.07·1·0.48·0.52) — the quadratic taker formula verified to the centicent |
| Confirm needs JSON content type (`{}` body); bare PUT ⇒ 400 `invalid_content_type` | first harness run |
| Late confirm (after 30s std window) ⇒ 400 `{code: "expired", service: "midland"}`; quote status REMAINS `accepted` (no cancelled transition, no cancellation_reason) | lapse scenario |
| DELETE quote after accept ⇒ **404 not_found** — no explicit decline exists; lapse is the only out | delete-after-accept scenario |
| **Off-grid quote prices are ACCEPTED at creation** (0.3550 on a `linear_cent` market) — the "must land on the grid" doc rule is NOT enforced at quote-create | off-grid probe; never rely on server validation to catch grid bugs; our maker-favorable snapping stays mandatory |
| `contracts_accepted_fp` is None on the executed quote for contracts-mode RFQs | terminal quote objects — the contracts-mode fallback to RFQ size is REQUIRED, not defensive |
| Endpoint costs: default 10; communications create/delete/get quote = 2 | GET /account/endpoint_costs |
| `GET /account/limits` works (basic tier: read 200/s refill, 400 bucket; write 100/s, 100 bucket) | api_limits step (path fix verified live) |

**Still unverified (needs a combo-market pass / settlement):** combo NO payout
= $1 − product (`combo_no_pays_complement` stays null ⇒ NO-side accepts
decline in quote mode); HVM 3s/1s timing; combo grid structure on KXMVE
markets; `yes_bid + no_bid > $1` rejection.

## Phase 5 — demo quote mode end-to-end: EXECUTED 2026-07-05

Live session (quote mode, both accounts): **30 real quotes** sent to live demo
combo RFQs; one full round trip on a KXMVECROSSCATEGORY combo:

| Measurement | Value |
|---|---|
| accept → our confirm (server timestamps) | **117 ms** of the 3s HVM window |
| last-look local decision | **0.89 ms** (budget <200ms) |
| confirm → executed | 1.29 s (the 1s HVM execution timer + latency) |
| quote | yes 0.1100 / no 0.8460 (deci-cent grid, fair 1318cc, ρ=0.6 same-event block) |
| fill booked | 2.00 YES @ $0.11, expected_edge_cc=436 in the EV ledger; +10s markout recorded |
| lifecycle hygiene | 26 TTL expiries deleted; cancel-all cleaned 3 open quotes on halt; 414 skips all reasoned |

Additional live facts: quotes list requires a scope (`user_filter=self` /
`rfq_user_filter=self`; bare or wrong scope ⇒ 403); a competitor maker bot
exists on demo (quotes ~sum-1.00 prices within ~1s and lets accepts lapse);
transiently-skipped RFQs need the warmup retry loop (books subscribe lazily on
first sighting). E5 (HVM latency budget) RESOLVED: 117ms total vs 3,000ms.

## Empirical verification queue (demo / Phase 2.5 ground truth)

**Resolved live 2026-07-05 (demo, real credentials):** communications WS
channel subscribes fine on demo; REAL combo RFQ traffic exists there (3-leg
KXMVECROSSCATEGORY, 2-leg KXMVESPORTSMULTIGAMEEXTENDED seen open);
`GET /communications/rfqs?status=open` filter works as coded (C3 ✓); MVE
collections list on demo (payload key `multivariate_contracts`); balance
payload = `{balance (cents int), balance_dollars, balance_breakdown,
portfolio_value, updated_ts}`; fresh demo accounts start at **$0.00** — RFQ
execution scenarios need mock funding via the demo site UI first.

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
| A7 | Confirm = `PUT /communications/quotes/{id}/confirm`, 204, starts execution timer — **ground truth 2026-07-05: a truly bodyless PUT gets 400 `invalid_content_type`; must send `{}` with JSON content type** (docs said body optional) | `exchange/rest.py` | fixture:ground_truth (live demo) |
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

### Phase 2 — observe mode (2026-07-05)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| C1 | rfq_created required fields: `id`, `market_ticker`, `created_ts`; sizing = `contracts_fp` XOR `target_cost_dollars`; combos carry `mve_collection_ticker` + `mve_selected_legs[{event_ticker, market_ticker, side, yes_settlement_value_dollars}]` | `rfq/models.py` | doc:asyncapi communications schemas |
| C2 | `mve_selected_legs[].side` values are "yes"/"no" (schema has NO enum) — anything else parses as UNKNOWN and cannot pass filters | `rfq/models.py`, `rfq/filters.py` | doc:asyncapi + defensive UNKNOWN branch (enum queued for empirical confirm) |
| C3 | Communications channel has NO seq field ⇒ no on-stream gap detection; completeness via `GET /communications/rfqs?status=open` polling; injected RFQs counted as `rfq.ws_missed` | `rfq/intake.py`, `ops/app.py` | doc:asyncapi (envelope type/sid/msg only); `status=open` param value **UNVERIFIED** (queued: confirm exact GetRFQs filter values on demo) |
| C4 | WS error codes 10/17/25 are terminal (must resubscribe); 25 = messages LOST | `rfq/intake.py` | doc:communications-ws error table |
| C5 | Combo semantics for the stub: combo settles YES iff every selected leg settles on its selected side (leg "no" side contributes 1−p) | `pricing/stub.py` | doc:rfqs.md/multivariate (product-of-legs settlement) — **direction-to-wire mapping deliberately NOT coded; Phase 2.5** |
| C6 | RFQ deletions don't repeat combo fields; correlate by `id`; open-RFQ registry rebuilt from REST after reconnect (no WS replay) | `rfq/intake.py` | doc:asyncapi rfq_deleted schema |
| C7 | Local store is the durable record (exchange retains closed RFQs ~7 days) | `ops/persistence.py` | doc:rfq-flow retention note |
| C8 | `creator_id` empty on rfq_created ⇒ no creator-based filtering at quote time | (no code depends on it) | doc:communications-ws |

**UNVERIFIED rows for human review: C3 (GetRFQs status param), C2 (side enum).**
Both fail safe.

### Phase 2.5 infrastructure (2026-07-05) — harness built, fixture PENDING

`core/conventions.py` + `combomaker ground-truth` harness are in place.
**No ground-truth fixture exists yet** — conventions are DOC_ASSUMED with
`verified=False`, and `require_verified()` blocks any real quoting until the
harness has run and a human has promoted `conventions.derived.json` →
`conventions.json`. Blocked on: demo credentials for TWO accounts
(`KALSHI_API_KEY_ID`/`KALSHI_PRIVATE_KEY_PATH` +
`KALSHI_REQUESTER_API_KEY_ID`/`KALSHI_REQUESTER_PRIVATE_KEY_PATH`) and a
liquid standard demo market ticker. Run:
`uv run combomaker ground-truth --market <TICKER>` (then a second pass on a
combo market for HVM timing + product settlement).

Architecture guard: `accepted_side` / `is_taker` / `outcome_side` tokens are
forbidden under `pricing/` and `risk/` — those layers consume a `Conventions`
instance only.

### Phase 3 — pricing engine + fees (2026-07-05)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| D1 | Quadratic fee = ceil_to_centicent(coef × mult × C × P × (1−P)); trade fee always rounds UP | `pricing/fees.py` | doc:fee_rounding (rounding); **UNVERIFIED** coefficients (taker 0.07 / maker 0.0175 from secondary sources; PDF blocked — human download + fill reconciliation) |
| D2 | Fee attribution unknown ⇒ price with TAKER coefficient (fail-safe: overestimates cost) | `pricing/fees.py` | design rule (resolves via Phase 2.5 `maker_is_taker_on_fill`) |
| D3 | `quadratic` series charge no maker fee; `quadratic_with_maker_fees` adds maker coef; `flat`/unknown fee types ⇒ FeeUnknownError ⇒ no-quote | `pricing/fees.py` | doc:get-series fee_type enum + **UNVERIFIED** (PDF tables) |
| D4 | Combo series fee_type comes from config default ("quadratic"), not fetched per-series yet | `pricing/engine.py` | **UNVERIFIED** — Phase 5 should fetch GET /series fee fields for the target collections |
| D5 | Event mutual-exclusivity from `GET /events/{ticker}.mutually_exclusive`; missing flag = UNKNOWN (never False) | `marketdata/metadata.py`, `pricing/relationships.py` | doc:get-event schema |
| D6 | Two YES legs of a mutually exclusive event = impossible ⇒ NO-QUOTE (not arbed); same market both sides ⇒ impossible; any unknown side/event ⇒ UNKNOWN ⇒ no-quote | `pricing/relationships.py` | design rule (defense #2) |
| D7 | Implication/nesting within same-event groups NOT explicitly modeled in v1 — approximated by block rho + correlation-uncertainty width | `pricing/joint.py` | modeling choice — revisit in Phase 8 |
| D8 | NO-side legs handled by sign conjugation (diag(±1)·R·diag(±1), marginal 1−p) | `pricing/joint.py` | mathematical identity |
| D9 | Leg-uncertainty propagation via linear-sum product gradient (∂P/∂mᵢ ≈ P/mᵢ, clamped at mᵢ≥0.01) | `pricing/joint.py` | approximation, conservative direction |
| D10 | Free-money caps: yes_bid ≤ min executable selected-leg ask − margin; no_bid ≤ Σ executable complement asks − margin; caps computed at 1.00 contract (top-of-book = tightest bound); unavailable caps ⇒ no-quote | `pricing/quote.py` | dominance argument (combo YES ≤ each leg; combo NO ≤ Σ complements) |
| D11 | Bids snap DOWN onto the grid; a rounded-away side declines ("0"); yes+no ≤ $1 − min_capture enforced | `pricing/quote.py` | design rule (defense #4), property-tested |
| D12 | Fee subtracted per side = max(fee@fair, fee@nearest-to-$0.50 in plausible fill range) — covers the quadratic peak | `pricing/quote.py` | design rule (fix from test sweep), regression-tested |
| D13 | Target-cost qty estimate (cost/fair, rounded UP) feeds ONLY the size-width adder, never money math | `pricing/engine.py` | **UNVERIFIED** conversion formula (Phase 2.5 regression item) |

**UNVERIFIED rows for human review before Phase 5: D1/D3 (fee coefficients +
tables — needs the PDF and fill reconciliation), D4 (per-series fee fetch),
D13 (target-cost conversion).** All fail toward wider/no-quote.

### Phase 4 — risk engine (2026-07-05)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| E1 | Per-leg delta (hot path) = independence product formula, signed by our side and each leg's selected side; missing marginal ⇒ UNKNOWN (never zero); conditional-MC deltas (`sim.leg_deltas`) reserved for slow full-book refresh | `risk/exposure.py` | approximation under correlation — direction documented; MC refresh wired in Phase 5 |
| E2 | Mass-acceptance worst case: every open quote fills NOW on its per-aggregate worse side (sign-aligned magnitude bound); dominance over every realizable fill combination property-tested (triangle-inequality argument in tests/test_exposure.py) | `risk/exposure.py`, `risk/limits.py` | design rule (+ PreferBetterQuote makes "instantly executable" literal) |
| E3 | Our max loss per position = entry price × contracts (we always PAY our bid to open — both sides of a quote are bids) | `risk/exposure.py` | follows from A5/D11; re-check against ground-truth fills (Phase 2.5) |
| E4 | Last-look severity order: kill switch > exchange > WS > in-play > velocity > stale leg > leg move > joint move > risk; every None input fails closed | `risk/lastlook.py` | design rule, pinned by tests |
| E5 | HVM confirm clock starts at LOCAL receipt of quote_accepted (message carries no timestamp) | (Phase 5 wiring) | doc:asyncapi quote_accepted — timer measured server-side, budget = 3s − latency (Phase 2.5 measures) |
| E6 | In-play market trigger: mid range > threshold OR update count > N within window ⇒ anomalous for cooldown; schedule-based gate lives in filters | `risk/inplay.py` | design rule (courtside defense = width/size/refusal) |
| E7 | Markouts recorded vs BOTH model fair and raw Kalshi mid product; declined confirms tracked with fill_ref `declined:<quote_id>` | `risk/markouts.py` | defense #5 |
| E8 | Daily-loss halt at ≥ limit on realized+unrealized | `risk/limits.py` | design rule |

**UNVERIFIED rows: E3 (re-check vs ground truth), E5 (latency budget measurement).**

### Phase 5 prep — hot path wiring (2026-07-05)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| F1 | Create-quote response carries the quote id as `id` (fallback `quote_id`) | `rfq/lifecycle.py` | doc:create-quote (201 → {"id"}) |
| F2 | Replacement = just send a new quote on the same RFQ (server auto-cancels ours); if a replacement attempt is refused by filter/risk, we explicitly DELETE the stale quote | `rfq/lifecycle.py` | doc:rfqs.md + defensive rule |
| F3 | Unreadable `accepted_side` ⇒ deliberate lapse (never guess a side) | `rfq/lifecycle.py` | defense #2 |
| F4 | Expected edge at fill = (side fair − our bid) × qty, fees reconciled later from the exchange ledger (`fee_cc` NULL until then) | `rfq/lifecycle.py` | defense #3 — fee never predicted into the ledger |
| F5 | Freshness input to last look = feed traffic age (server pings @10s) gated by book validity — a quiet book on a live seq-continuous stream is current | `rfq/lifecycle.py`, `marketdata/feed.py` | design rule (B9) |
| F6 | `GET /communications/quotes?status=open` lists our open quotes for cancel-all/startup reconcile | `ops/cli.py`, `ops/quote_app.py` | **UNVERIFIED** param value (queued with C3) |
| F7 | quote mode gates: verified conventions + non-empty whitelist + prod guard; startup cancels leftovers; existing positions WARN (exposure book starts empty — manual reconcile) | `ops/quote_app.py` | design rule |
| F8 | Report portfolio MC uses independence corr + complement pseudo-legs for NO-side legs (risk view, not pricing) | `ops/report.py` | approximation, documented |

**UNVERIFIED rows: F1, F6 — both resolve in the first live demo run.**

### External odds — SportsGameOdds adapter (2026-07-05)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| G1 | Base `https://api.sportsgameodds.com/v2`, header `x-api-key`; events via `GET /events?leagueID=&oddsAvailable=true&limit=`; response `{data: [event]}`; odds keyed by `oddID` = statID-statEntityID-periodID-betTypeID-sideID; American odds strings | `pricing/sources/sportsgameodds.py` | doc:sportsgameodds.md (their docs) |
| G2 | We devig their juiced `bookOdds` pair ourselves (configured method); their opaque `fairOdds` only feeds uncertainty via disagreement distance | ″ | design rule (decision #8) |
| G3 | Opposing side derived by flipping entity+side in the oddID (home↔away, over↔under) | ″ | **VERIFIED 2026-07-05** — live probe: our flip == their `opposingOddID` on real MLB payloads, both directions |
| G4 | Free tier 2,500 objects/month + 10 req/min ⇒ poller floor 10-min interval, per-league event cap, budget counter; NO historical on free tier | ″ | user-confirmed plan + live `GET /account/usage` (monthly object accounting granularity still to observe over time) |
| G5 | Kalshi ticker → (eventID, oddID) mapping is an explicit config table; unmapped ⇒ None (Kalshi-book-only), never fuzzy-matched | ″ + `ops/config.py` | design rule (defense #2) |
| G6 | Engine blends book (w=1.0) + external (w=cfg); sources disagreeing >0.08 ⇒ `SKIP_SOURCES_DISAGREE` no-quote; adapter OFF by default | `pricing/engine.py` | design rule |

**G3/G4 resolved by the 2026-07-05 live probe (2 MLB events, ~2 objects
spent); adapter ingested 450 marginals end-to-end. Envelope `{success, data}`
confirmed. Kalshi demo-credential attempt the same day: 401 on demo REST+WS
with a production-site key — confirms credentials are strictly
per-environment (auth-env.md); a demo-site key is required.**

### SGP structure model + archetype rules (2026-07-06)

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| H1 | Leg structure is derivable from the ticker's series prefix (GOAL/BTTS/TOTAL/CORNERS/ADVANCE/EXTRAS/FIGHT/GAME keywords); unrecognized ⇒ UNKNOWN ⇒ flat prior + WIDER band (never blocks alone) | `pricing/legtypes.py` | live-observed ticker patterns |
| H2 | Signed typed-pair ρ priors — **CALIBRATED 2026-07-06** from 8,982 matches (top-5 EU leagues, 5 seasons; `tools/calibrate_pairs_from_history.py` solves implied ρ through OUR copula): btts\|total +0.75, ml\|total +0.23 (home/away asymmetry 0.28/0.18 covered by band), **btts\|ml −0.17 (hand prior had the WRONG SIGN)**, **corners pairs ≈ 0 (hand priors +0.30/+0.25 busted)**, ml\|ml −0.95 (P(both)=0.000 measured), total\|total 0.95 (nesting). Calibrated pairs carry tightened ±0.04–0.10 bands. player_goal/extras pairs remain hand priors at ±0.15. Caveat: club-soccer data applied to internationals/other sports = league-transfer assumption; refresh per sport as data lands | `ops/config.py` CorrelationConfig | fixture:historical-results (n=8,982) |
| H3 | Joint repriced at per-pair (low, high) matrices; each repaired to PSD independently | `pricing/sgp.py`, `joint.price_joint_matrices` | mathematical construction, property-tested |
| H4 | Longshot rule: below fair 15%, uncertainty floored at 25% of fair (absolute gradient shrinks with P — anti-conservative for the shorting side otherwise) | `pricing/engine.py` | design rule |
| H5 | Favorites-stack multiplier: OFF by default (1.0); enable only after markouts prove the flow benign | `ops/config.py` QuoteConfig | design rule — validation-gated |
| H6 | Leg-count width convexity: mechanism shipped, default 1.0 (linear = old behavior); raise via YAML once markup-by-n data exists | ″ | ″ |
| H7 | Interest: Kalshi pays variable interest on positions AND cash above a $250 monthly-average gate (operator-confirmed from Kalshi's wording) ⇒ NO carry-cost width adder; early small accounts may not qualify — treat as bonus, never as pricing input | (pricing unchanged) | operator-provided; verify the accrual line item once live |

### Multi-sport SGP calibration (2026-07-06, extended same day)

All ρ with 99% CIs (delta-method on the joint frequency through our copula):

| Sport | Data | n | Key measured ρ [99% CI] |
|---|---|---|---|
| Soccer CLUB | football-data.co.uk, top-5 EU ×5 seasons | 8,982 | btts×over +0.75 [.69,.80]; ml×over +0.28/+0.18 (home/away); btts×ml −0.20 [−.27,−.13]; corners ≈ 0 |
| Soccer **INTERNATIONAL** (→ World Cup) | martj42, competitive 2000+ | 16,985 | btts×over +0.67 [.62,.71]; ml×over +0.31; btts×ml −0.197 (**identical to club**) |
| NFL | nflverse vs Vegas closing lines | 7,170 | ml×over 0.00 [−.09,.09]; spread×over +0.03; ml×spread +0.88; OT×over +0.20 [.07,.33] |
| NBA legacy | 538, 2000–2015 | 20,126 | ml×over +0.017 [−.04,.07] |
| NBA **MODERN** | hoopR/ESPN, 2016–2025 | 12,567 | ml×over **+0.008** [−.06,.07] — zero survived the 3PT era |
| MLB | Retrosheet 2015–2024 | 20,642 | ml×over **−0.056** [−.11,−.01] (home wins skip the bottom 9th ⇒ fewer runs); extras×over pre-2020 −0.04 → **post-2020 +0.10 (ghost-runner RULE CHANGE)** |

**Era-stability (the "does past data predict the future" answer):** intl
btts×over drifted −0.017 over ~25 years; intl ml×over −0.020; NBA ml×over
+0.008 across the 3PT revolution; MLB ml×over +0.005. Outcome co-movement is
a structural property of scoring dynamics and is empirically near-constant —
**except across explicit rule changes** (MLB extras +0.138 jump at the 2020
ghost-runner rule), so: calibrate on recent windows, re-run after rule
changes, and let the bands cover residual drift. Marginals (who wins) are
NEVER taken from history — always from live market prices.

Config: per-sport tables cover today's volume — WC (international-informed
soccer table), MLB (fresh incl. post-rule-change extras), WNBA (NBA-transfer,
NBA-zero verified on modern data, wider band). Pending: NHL, direct WNBA
measurement, player-prop pairs, college; trade-tape markup surface as live
cross-check.

### Dependence-fitting methodology (directive adopted 2026-07-06)

Operator directive (from spec review) adopted; status per point:

1. **No raw joint-lift constants** — compliant by construction (we fit copula
   ρ, marginal-invariant, Fréchet-safe). The pooling critique WAS valid:
   pooled frequencies conflate within-game dependence with between-game
   team-strength heterogeneity that live marginals already price.
2. **Conditional fitting** — implemented (`tools/fit_conditional_rho.py`):
   per-game closing-line marginals (soccer: devigged B365 1X2 + O/U; NFL:
   devigged moneylines, over vs line ≡ 0.5), one-parameter copula MLE via
   vectorized Owen's-T BVN self-checked to 2e-16 against the pricer copula.
   Soccer ml×over conditional ρ = +0.30 (SE .019) — pooled +0.28 was barely
   confounded here. Pairs WITHOUT per-game odds (btts pairs, intl, MLB, NBA,
   WNBA) remain pooled-method with widened bands, marked pending.
3. **Structural market-implied models** (Dixon-Coles scoreline for soccer;
   bivariate normal margin/total for NFL/NBA, inverted from live prices) —
   ROADMAP v2 of the pricer; the pairwise-ρ copula is v1 with honesty bands.
4. **OOS gate** — implemented: held-out-season log-loss vs independence.
   Soccer ml×over BEATS independence OOS (1.2477 vs 1.2580) ⇒ ships.
   NFL ml×over does NOT beat independence OOS ⇒ stays 0.00 (doubly confirmed).
5. **Uncertainty per parameter → width; Fréchet clamp backstop** — already the
   architecture (bands are quote width; clamp in copula.py).
6. **Licensing/attribution** — football-data.co.uk (free-use terms),
   martj42/international_results (open GitHub dataset), nflverse (open),
   sportsdataverse hoopR data (open); **Retrosheet requires notice: "The
   information used here was obtained free of charge from and is copyrighted
   by Retrosheet. Interested parties may contact Retrosheet at
   www.retrosheet.org."** Data cached under data/ (gitignored), fetched by
   the tools on demand.

### Structural pricer v2 (Dixon-Coles) + orientation-aware priors (2026-07-06)

Trigger: two scoreline-inversion worked examples (ENG/Kane/BTTS fav-side,
POR/Ronaldo/BTTS dog-side) + LIVE validation — the Kalshi parlay UI priced the
SPA/POR combo at $46–48 payout on $5 (taker 10.4–10.9¢), exactly our
structural fair 10.9¢ (independence $91, v1 copula $65). Winning combo makers
price structurally; v1's dog-side errors meant we'd never win underdog SGP
auctions (missed volume — longshot width covers the bleed side).

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| I1 | **Engine bug fixed**: `pair_rho_by_sport` was never forwarded into `SgpParams` — every calibrated sport table was dead config on the hot path | `pricing/engine.py` | regression test `test_engine_forwards_sport_tables` |
| I2 | btts\|moneyline is orientation-conditional: fav −0.19 / dog 0.00, linearly blended across the ML leg's 45–55% marginal (no fair cliff at 50¢). "Winners keep clean sheets" is favorites-only; a dog only wins by scoring | `ops/config.py`, `pricing/sgp.py` | structural implication + 1 live market validation; UNVERIFIED against co-settlement data |
| I3 | moneyline\|player_goal = 0.50 soccer (band .12), 0.40 global (band .20): structurally implied +0.51/+0.52 on BOTH examples, orientation-insensitive | `ops/config.py` | structural implication ×2; UNVERIFIED against player-prop history (none available) |
| I4 | Scoreline model: independent Poisson 90' + DC low-score tau; knockout draws play ET at `et_factor`×rates (pens ⇒ win-market NO); player goals = multinomial thinning (share q per player, Binomial given team goals) | `pricing/dixon_coles.py` | model form — banded (DC ρ ±0.08, ET factor 0.25–0.40 re-inverted into width) |
| I5 | DC ρ = **−0.05 FITTED** on train-season scorelines (grid MLE through the production inversion; −0.10 literature placeholder replaced) | `ops/config.py` StructuralConfig | fixture:historical-results via `tools/validate_structural_oos.py` |
| I6 | Inversion identification: ≥2 team-level legs required (else StructuralError ⇒ copula fallback); 2 legs solve exactly (residual >0.005 ⇒ refuse); >2 least-squares with residual priced into width; player shares solved per leg, Σq>0.95 per team ⇒ refuse | `pricing/dixon_coles.py` | mathematical construction, property-tested |
| I7 | Ticker shapes: game code = DDMMMYY[+HHMM] + concatenated equal-length team codes; GOAL ticker's player segment prefixes the team code; TOTAL line suffix ("3" or "2.5"). ANY parse doubt ⇒ reason ⇒ copula fallback (UNKNOWN never prices structurally) | `pricing/structural.py` | observed demo/prod tickers; parser is fail-safe by construction |
| I8 | Settlement windows — **RULE-BOOK VERIFIED 2026-07-06** (operator-provided Kalshi rules text): knockout game market = which team ADVANCES (ET **and penalty shootouts** included) ⇒ `Advance` spec w/ pens factor 0.5 ± 0.10 banded; Regulation-Time ML/Spread/Total/BTTS/TeamTotal/CorrectScore settle at END OF REGULATION ⇒ BTTS/totals `include_et=False` always; other props (player goals) = full game incl ET, pens excluded ⇒ matches our ET stage exactly. Window-flip band replaced by the pens band. Anchors re-derived by independent MC: ENG/NOR 0.2282→**0.2401**, SPA/POR 0.1088→**0.1153** (windows are worth ~1¢ of fair — the correction was material). Residual assumption: knockout-vs-group is mapped per SERIES (`knockout_series=["KXWC"]`), correct for the current knockout rounds, revisit at the next group stage; first live combo settlement reconciliation is the final backstop (defense #3). **Live-tape refinement (same day, first RFQs after shadow restart): `KXWCADVANCE` and `KXWCGAME` COEXIST on the same knockout matches** ⇒ GAME is the Regulation-Time Moneyline family (90' only, TIE possible, both formats) and ADVANCE is the ET+pens market — adapter maps each series accordingly. Also live-confirmed: `-ARGLMESSI10-2` (player 2+ goals, our Binomial k≥2 path), integer total lines (`-2`, `-3`), player team-prefix codes (`FRAKMBAPP10`) | `pricing/structural.py`, `pricing/dixon_coles.py` | doc:kalshi-rules-text (operator, 2026-07-06) + live tape |
| I9 | `structural.enabled = True` — **OOS GATE PASSED 2026-07-06** (below); flag was OFF until this evidence existed | `ops/config.py` | gate: `tools/validate_structural_oos.py` |
| I10 | Hot-path cost: ~47ms per structural quote (memoized state enumeration, warm-started perturbation re-inversions) vs 500ms budget | measured 2026-07-06 | benchmark, re-check on prod hardware |

**OOS gate result (2026-07-06, `tools/validate_structural_oos.py`):** 8,980
club games, dc_ρ fitted −0.05 on train (<2024, n=7,228) scoreline MLE through
the production `invert()`; held-out 23/24+24/25 (n=1,752) joint log-loss per
game, LOWER better — structural beats the SHIPPED v1 copula on ALL metrics
(v1's ml×over ρ was itself fitted on this data, so this is a high bar):

| metric | independence | v1 copula | structural |
|---|---|---|---|
| pair hw×over (both marginals from market odds) | 1.25797 | 1.24734 | **1.24657** |
| pair hw×btts (marginal parity: btts marginal = DC-implied for all models) | 1.27353 | 1.26724 | **1.26330** |
| triple hw×over×btts (8-cell — what a 3-leg SGP maker quotes) | 1.94197 | 1.74775 | **1.70607** |

The margin grows with combo complexity: pairwise ρ stitching degrades where
coherent scorelines don't. `structural.enabled=True` shipped on this evidence
(directive point 4 satisfied against the incumbent, not just independence).
Caveats: club-soccer evidence applied to WC internationals (btts×ml at least
measured identical club vs intl); triple metric uses each model's own
coherent cells (structural marginals carry inversion misfit); settlement
windows (I8) remain the open UNVERIFIED assumption — the window band prices
it, verify rules text before Phase 7.

### Margin/total structural pricer — NFL/NBA/WNBA (2026-07-06)

Game state X = (margin, total) bivariate normal; per-game means inverted from
live prices, sport shapes calibrated offline. Every ML/spread/total/team-total
leg is a halfplane in (M,T); joints are exact region probabilities (1D
conditional quadrature). The geometry prices what v1 hand-encodes: ML×spread
comonotone (v1 says ρ 0.88), ML×total ≈ independent (v1 says 0.00), team
totals coherent with both.

| # | Assumption embedded in code | Where | Tag |
|---|---|---|---|
| J1 | Sport shapes from RECENT windows (operator directive — sports drift): NFL 2020-25 **closing-line residuals** σ_M 12.66 σ_T 13.06 ρ +0.026; NBA 2022-26 σ_M 13.71 σ_T 18.42 ρ 0.000; WNBA 2021-26 σ_M 12.04 σ_T 16.55 ρ −0.019 (**team-fixed-effects residuals — method validated on NFL: FE vs line-residual σ within 3%**). Era checks: NFL ρ stable +0.027→+0.026 over a decade; NBA σ_M ROSE 12.85→13.71 (3PT-era variance — recency mattered); WNBA data through 2026-07-05 (yesterday) | `ops/config.py` MarginTotalConfig, `tools/calibrate_margin_total.py` | fixture:historical (nflverse/hoopR/wehoop, refreshed 2026-07-06 incl. NFL 2025 + NBA 2025-26 + WNBA current) |
| J2 | Normal approximation of discrete scores: flat discreteness band when any margin leg present (NFL 0.010 — key numbers 3/7; NBA 0.004; WNBA 0.005) + σ bands ±5%, ρ band ±0.05, all re-inverted | `pricing/margin_total.py`, config | model form — banded |
| J3 | Identification: leg directions must span the needed means (rank check) else refuse; exact systems refuse at residual >0.005; ANY system refuses at >0.05 (legs mutually inconsistent — e.g. ML and spread implying opposite favorites); intermediate misfit → width | `pricing/margin_total.py` | mathematical construction, tested |
| J4 | **Spread legs BLOCKED in the adapter**: the ticker does not carry the line's sign convention, and guessing wrong silently mirrors every spread quote — copula fallback until real in-season spread tickers + rules are observed. ML + totals ship | `pricing/structural.py` `_parse_mt_leg` | fail-safe by construction (quiet-failure defense #2) |
| J5 | Integer total lines ("225") read as ≥N with continuity correction (N−0.5); ".5" lines as-is. Game-code team split requires equal-length codes (2-letter NFL codes vs 3-letter mixed, e.g. "KCDET", refuse ⇒ fallback) | ″ | observed ticker patterns; verify against live NFL/NBA tickers in season |
| J6 | `enabled_sports=["nfl"]` — **OOS GATE PASSED** (train 2015-23, test 2024-25 n=562, lower better): pair hw×over 1.29275 vs v1 1.29293; pair hw×cover **0.96260 vs 0.99940**; triple **1.65544 vs 1.69217**. NBA/WNBA calibrated but DISABLED: no local odds history to gate; gate via prod-shadow would-quotes+settlements or an odds source before their seasons (NBA opens ~Oct 2026) | config + `tools/validate_margin_total_oos.py` | gate evidence (directive point 4) |

### Tape validation + MLB runs model + WNBA/spread enablement (2026-07-06 evening)

| # | Assumption / finding | Where | Tag |
|---|---|---|---|
| K1 | **Model-vs-winning-quote measurement** (`tools/compare_models_on_tape.py`): 600 executed combo trades joined to the latest prior would-quote (stored leg marginals re-priced offline under all three models). 78% of trades are cross-game-only combos where independence/copula/structural coincide exactly (ties). On the n=95 same-game combos where models differ: **structural closest to the winning quote 55% vs v1 31% vs independence 15%** (mean\|err\| 5.00¢ / 5.81¢ / 8.52¢, median 2.73¢ / 4.22¢ / 5.85¢). Structural fair sat BELOW clearing on 100% (maker-viable); v1 sat ABOVE clearing 75% (auto-losing auctions it thinks are bleeders). Caveat: ~13h of tape; re-run as it accumulates | tool + shadow DB | live tape (n=95 differing) |
| K2 | Line conventions **DOC-VERIFIED from live market metadata**: `KXMLBSPREAD-…-BOS4` = "Boston wins by over 3.5 runs" (TEAMn ⇒ margin > n−0.5, team-anchored, no sign ambiguity — spread legs UNBLOCKED); `KXMLBTOTAL-…-5` = "Over 4.5 runs", `KXWNBATOTAL-…-175` = "Over 174.5 points" (integer N ⇒ over N−0.5, matching the continuity correction already shipped) | `pricing/structural.py` | doc:market-titles (fetched 2026-07-06) |
| K3 | Team codes vary in length (PHI+KC, CONN+MIN, SEA+LA): resolution anchors candidate codes at the ENDS of the game-code blob (prefix ⇒ team A, suffix ⇒ team B, both/neither ⇒ refuse); player codes resolved by longest leading fragment. Replaces the equal-split parser that refused MLB/WNBA codes | ″ | live-tape ticker shapes, tested |
| K4 | **MLB structural** (`pricing/mlb_runs.py`): FINAL runs per team ~ NegBin(μ, k) independent, tie diagonal removed + renormalized (extras' effect on totals is inside final-score calibration). k = 3.62 (Retrosheet 2021-24; 3.63 in 2015-19 — era-stable), band ±0.30 covers home/away asymmetry (k 3.37 away / 3.91 home; tickers don't reveal the home side). Mirror-symmetry property: win ⊥ over EXACTLY at equal means; favorite-win×over +0.010, dog −0.010 — orientation asymmetry for free. `enabled=False` pending OOS gate via prod-shadow leg prices + settlements (~15 games/day; no local MLB odds history) | `pricing/mlb_runs.py`, `tools/calibrate_mlb_runs.py` | fixture:Retrosheet + model form banded |
| K5 | WNBA margin-total ENABLED by operator request (season live): geometry NFL-OOS-gated, WNBA shape calibrated (n=1,338 through 2026-07-05), ρ≈0 ⇒ ml×total within noise of v1 — upgrade is coherent spread/team-total joints. Shadow-settlement confirmation gate as data accrues | config | operator decision, documented |

### Final adversarial review (2026-07-05) — 5 lenses, 43 agents, 7 confirmed defects, all fixed

| Finding (confirmed by 2-skeptic verification) | Fix | Regression test |
|---|---|---|
| **CRITICAL:** target-cost RFQs entered the entire risk system as 1 contract (`rfq.contracts or CentiContracts(100)`) — per-quote caps, mass acceptance, gross notional, event worst-case all blind to ~71% of real flow | `_risk_qty`: target ÷ cheapest quoted side, ceil (conservative full-size); unresolvable ⇒ no-quote | `tests/test_review_fixes.py` |
| Unknown/unparseable `contracts_accepted_fp` was guessed (1 contract) then confirmed | `_accepted_qty` returns None ⇒ deliberate lapse (`DECLINE_SIZE_UNKNOWN`); contracts-mode missing-field falls back to the RFQ's own full size (doc-anchored) | ″ |
| Daily-loss halt structurally dead (frozen zero `DailyPnl` never written) | `_refresh_daily_pnl` marks positions at current mids each maintenance tick; `record_realized_pnl` hook; HALT_DAILY_LOSS breach now fires the kill switch | ″ |
| Confirm-exception path lost the fill (state never parked ⇒ quote_executed unmatchable) | pending_fill parked BEFORE the confirm call; 3 consecutive confirm failures ⇒ `HALT_CONFIRM_TIMEOUTS` | ″ |
| Exposure gap between confirm and quote_executed (irrevocable fill invisible to limits) | position booked at confirm success (idempotent re-book at execution) | ″ |
| Subscriptions added while connected never sent (lazy leg-watching silently dead) | `add_subscription` sends immediately when connected | ws behavior covered via lifecycle tests |
| Batch orderbooks wire params wrong (`market_tickers` comma-join vs repeated `tickers`); api-limits path wrong (`/account/api_limits` vs `/account/limits`) | both corrected to the doc-verified contract | (wire-format tests) |
| `combo_no_pays_complement` convention had zero consumers while the complement was hardcoded | NO-side accepts decline while the convention is unverified (`DECLINE_CONVENTION_UNKNOWN`); NO-side expected edge recorded as NULL (not assumed) when convention isn't True; MTM refresh skips rather than fabricates | ″ |
