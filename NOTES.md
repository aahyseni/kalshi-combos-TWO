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
