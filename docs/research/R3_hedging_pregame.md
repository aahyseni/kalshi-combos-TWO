# R3 — (A) Inventory-Aware Skew / Book-Balancing & (B) Pregame Hardening vs Courtsiders

**Repo:** kalshi-combos-TWO (READ-ONLY research/design). Everything below is an
*enhancement* to existing code, grounded at `file:line`. No greenfield rewrites.

**One-line framing.** We are a **SELL-ONLY parlay seller** — every quote forces
`yes_bid = 0` (`pricing/quote.py:221`, `pricing/engine.py:274 _enforce_sell_only`),
so we can only ever be **long NO** on a combo. "Balancing the book" therefore is
**not** two-sided market-making; it is *which combos we accept and how thin we
price them*, plus (optionally, later) real leg-market hedges. The residual after
any hedge IS the correlation position (CLAUDE.md:247).

---

## PART A — Inventory-aware skew / book-balancing / skew-to-reduce-exposure

### A0. What already exists (the seams — do NOT rebuild)

| Piece | Where | State |
|---|---|---|
| `inventory_skew_cc` param, full sign semantics | `pricing/quote.py:128,193-196` | **Plumbed, tested, always 0** |
| `price(..., inventory_skew_cc=0)` passthrough | `pricing/engine.py:165,267` | Passthrough; caller never sets it |
| Exposure book: per-market **and per-event** delta, worst-case loss **by event**, gross notional, mass-acceptance bound | `risk/exposure.py:120-223` (`delta_by_event`, `worst_case_loss_by_event_cc`) | Live, mass-acceptance property-tested |
| MC candidate marginal impact on common random numbers | `sim/engine.py:240-258 marginal_impact`, `:261-285 leg_deltas` | Live, deterministic |
| Analytic per-leg deltas (hot path) | `risk/exposure.py:93-118 analytic_leg_deltas` | Live |
| Limits incl. per-event delta + per-event worst-case loss | `risk/limits.py:23-31,121-146` | Live |
| Hedge planner (leg-market lay-off) | `hedging/planner.py` | **Scaffold, `plan()` raises NotImplementedError, phase-gated OFF** |
| Skew test proving direction | `tests/test_quote.py:298-304` (positive skew ⇒ yes_bid↓, no_bid↑) | Green |

**Key discovered fact — the sign is already correct for a NO-seller.**
`quote.py:195-196`:
```
yes_raw = fair_cc - half - fee_yes - inventory_skew_cc
no_raw  = (CC_PER_DOLLAR - fair_cc) - half - fee_no + inventory_skew_cc
```
Positive `inventory_skew_cc` *raises* `no_raw` (our NO bid), which *raises* the
implied YES ask we show the requester (`ask = $1 − no_bid`), i.e. it makes us
**more expensive / less likely to sell more NO**. Negative skew *lowers* our NO
bid → we quote a **cheaper** NO → we win more of that flow. So for the sell-only
book the entire skew lever operates on `no_bid`, and the mapping is:

> **We want MORE of an offsetting combo ⇒ NEGATIVE skew (tighter NO bid).
> We want LESS of a concentrating combo ⇒ POSITIVE skew (wider NO bid).**

This is the exact inverse of the two-sided comment at `quote.py:193` ("positive =
we are long the joint event, bid less for YES") — which is written for the
generic two-sided primitive. In sell-only, YES is dead; only the NO half of the
formula is active, and the design below defines skew directly in NO-bid space so
the direction can never be misread. **This is the single most important sign
check in the whole design** and must get a dedicated property test
(§A6, mirrors the sell-only fuzz at `tests/test_quote.py:519-534`).

### A1. The exposure a candidate RFQ adds (uses the existing book)

The book already answers "what am I overweight?" at two grains. Per the P&L
sweep (`docs/reports/2026-07-12-pnl-markup-sweep.md:108-112`) **the true risk
unit is the GAME, not the ticker** (combos share legs and games; one combo
carried ~$970k of payout swing; 68 combos = 50% of contracts). So the skew must
be driven primarily by the **per-EVENT** aggregates, with per-market as a
secondary term.

**Marginal-exposure of a candidate combo `C` (sell-only ⇒ we'd be long NO of C):**

Two tiers, matching the existing hot-path / slow-path split (`exposure.py:6-9`):

1. **Hot path (every RFQ, in-memory, <1ms).** Build the hypothetical NO position
   for `C` at the size the risk system already computes
   (`lifecycle.py:_risk_qty`), reuse `analytic_leg_deltas` (`exposure.py:93`) to
   get its per-leg / per-event delta contribution, and compare **direction**
   against the current book snapshot:
   ```
   snap = exposure.snapshot(marginals, mass_acceptance=True)      # already called in lifecycle
   cand_deltas = analytic_leg_deltas(hypo_no_position, marginals) # per market
   # aggregate candidate to event grain the same way the book does:
   cand_event_delta[e] = Σ_{leg in e} cand_deltas[leg.market]
   ```
   For each event `e` the candidate touches, the **marginal contribution to the
   worst-case book** is:
   - `align(e) = sign(snap.delta_by_event[e]) == sign(cand_event_delta[e])`
     → candidate ADDS to that event's net direction (concentrating), or
   - opposite sign → candidate OFFSETS (reduces |net delta|).
   - Also track `headroom(e) = max_event_delta_contracts − |snap.delta_by_event[e]|`
     and `loss_headroom(e) = max_event_worst_case_loss_dollars − worst_case_loss_by_event_cc[e]/1e4`
     (limits at `limits.py:26,31`). Skew scales with how little headroom is left.

2. **Slow path (full-book refresh, off hot path).** Use
   `sim.engine.marginal_impact(legs, corr, book_positions, candidate)`
   (`sim/engine.py:240`) — it returns (book-without, book-with) on **common
   random numbers**, so `ΔES = with.es_cc[0.95] − without.es_cc[0.95]` and
   `ΔVaR` are low-variance estimates of exactly how much tail risk the candidate
   adds or removes *under correlation* (the copula), not under independence. This
   is the honest measure: two combos that share no ticker but share a GAME are
   correlated through the copula `corr`, and `marginal_impact` captures that
   where `analytic_leg_deltas` (independence) cannot. Cache the per-event `ΔES`
   sign+magnitude at refresh cadence and let the hot path read it; fall back to
   the analytic direction when a candidate's game is not in the cache.

### A2. The skew function (tighter when offsetting, wider when concentrating)

Define skew in **NO-bid centi-cents** (positive widens, i.e. raises our ask):

```
skew_cc(C) =  Σ_e   w_conc · concentration_term(e)      # ≥ 0, WIDEN when adding
            − Σ_e   w_off  · offset_term(e)             # ≥ 0, TIGHTEN when offsetting
  clamped to [−skew_max_tighten_cc, +skew_max_widen_cc]
```

where, per touched event `e` with candidate |Δ| = `d_e` (contracts-equiv):

- **concentration_term(e)** — fires only when the candidate ADDS to the book's
  existing net direction for `e`. Ramps as headroom shrinks so the last combos
  before a limit pay the most:
  ```
  util(e)  = |snap.delta_by_event[e]| / max_event_delta_contracts        # 0..1
  loss_util(e) = worst_case_loss_by_event_cc[e] / (max_event_worst_case_loss_dollars·1e4)
  concentration_term(e) = d_e · f(max(util, loss_util))
     f(u) = u^γ          # convex: near-empty book ≈ free, near-limit ≈ full widen
  ```
- **offset_term(e)** — fires only when the candidate OPPOSES the book's net
  direction for `e`. It is a *rebate* on the ask, bounded so it can never turn
  the quote into a giveaway:
  ```
  offset_term(e) = min(d_e, |snap.delta_by_event[e]|) · g(util(e))
     g(u) = u          # you only get the rebate to the extent you're actually overweight
  ```
  You get the rebate only up to the amount you actually offset (`min(d_e, |net|)`)
  and only in proportion to how overweight you were (`util`). A combo that offsets
  a game you have zero position in earns **no** rebate — there's nothing to
  balance.

The convex `f` (γ≈2) is the mechanism the P&L sweep asked for indirectly: it makes
markup **monotone increasing in accumulated exposure**, the safe direction
(`2026-07-12-pnl-markup-sweep.md:97-99` "markup should be monotone increasing in
room"; here it's monotone in *inventory* rather than room, which is the lever we
control at quote time before clearing is revealed).

**Caps are hard safety, not tuning:**
- `skew_max_widen_cc` — unbounded widening is fine for safety (it only makes us
  sell less), but cap it (~600cc) so a mispriced event delta can't post an
  absurd near-$1 ask that looks like a fat-finger.
- `skew_max_tighten_cc` — **the dangerous side.** A rebate that tightens the NO
  bid toward the free-money cap must never cross it. It is *already* structurally
  contained: `construct_quote` clamps `no_raw` to `no_cap_cc − free_money_margin_cc`
  (`quote.py:202-204`) AFTER skew is applied, and re-checks the capture invariant
  (`quote.py:230-236`). So the offset rebate can shrink our edge but can never
  produce an arb quote — the existing free-money cap is the backstop. Still cap
  `skew_max_tighten_cc` modestly (~150cc, ~½ base width) so we never rebate away
  the whole markup chasing a balance.

### A3. The sell-only constraint — "how do we balance when we can only sell NO?"

This is the crux question. We cannot BUY the offsetting side of a combo (yes_bid
is hard-zero everywhere). Three answers, in order of how much of the imbalance
each can absorb:

**(1) Balance by SELECTION + PRICE of incoming NO flow (the primary mechanism —
100% within the sell-only mandate, ships first).**
Our net exposure to a game accumulates as a **direction**, not just a magnitude,
because different combos put us long-NO of *different leg-side products*. A combo
of "Team A wins ∧ over" that we sell NO on gives us positive P&L if **not**
(A wins ∧ over); a combo of "Team A loses ∧ under" sold NO gives positive P&L if
**not** (A loses ∧ under). These two NO positions have **opposing** deltas to
`P(A wins)` — `analytic_leg_deltas` (`exposure.py:108-117`) signs each leg by
`leg_sign` (yes vs no) × `position_sign` (our side). So even selling only NO, the
*book's per-event delta can be pushed back toward zero* by preferentially winning
combos whose NO position offsets our current net. The skew function does exactly
this: it makes offsetting combos cheap (we win more of them) and concentrating
combos expensive (we win fewer). **The flow rebalances us; we steer it with the
ask.** This is the honest sell-only analogue of two-sided inventory skew.

**Limit of (1):** it is *passive* — it only works if offsetting RFQs actually
arrive. In a favorite-hot window (the −$1.23M week) the flow is one-directional
(everyone parlays the same favorites), so selection alone can leave a residual
concentration. That residual is handled by (2) and (3).

**(2) Balance by REFUSAL / caps (already built, tighten the wiring).**
When an event is at/near its delta or worst-case-loss limit, the candidate simply
does not pass `LimitChecker.check` (`limits.py:121-146`) — the mass-acceptance
snapshot already includes the candidate (`limits.py:102-104`,
`lifecycle.py:166-172`). The **enhancement** the P&L sweep demands
(`2026-07-12-pnl-markup-sweep.md:111-112`) is a **per-combo max-payout cap**
(~2-3% bankroll) and a **committed-payout counter + cancel-all/hard-halt tiers**
(~50-60% / ~10% bankroll), because today's limits are delta/notional-based and
the sweep showed *payout* (bankroll tie-up: $23.5M potential for $1.8M premium,
`:26`) is the binding constraint for a parlay seller. Design in §A4.

**(3) Active leg-market hedge (the `hedging/` scaffold — later, gated).**
See §A5. This is the only mechanism that can *reduce* an existing residual
without waiting for offsetting flow, but it changes what business we're in
(CLAUDE.md:247) and is deliberately post-Phase-7.

### A4. Payout-based exposure caps (new limits the sweep explicitly asked for)

Add to `RiskLimits` (`risk/limits.py:22-31`) and enforce in `LimitChecker.check`:

| New limit | Grain | Why (sweep cite) |
|---|---|---|
| `max_combo_payout_dollars` | per-position | caps the single $970k combo (`:85,111`) |
| `max_event_committed_payout_dollars` | per-GAME | correlated legs are the real unit (`:108,111`) |
| `committed_payout_cancel_all_frac` / `_halt_frac` | book-wide, ×bankroll | ~50-60% cancel-all, ~10% hard halt (`:112`) |

"Committed payout" for a NO position = `contracts × $1` (max we pay if the parlay
hits) — this is `worst_case_loss` viewed from the payout side and already has a
home: `ExposureSnapshot.worst_case_loss_by_event_cc` (`exposure.py:127`) sums
`max_loss_cc` per event, but `max_loss_cc` today = *premium paid*
(`exposure.py:57-59`), which for a SELLER understates the true exposure (we can
lose up to `contracts×$1 − premium`, not just the premium). **Enhancement:** add
`max_payout_cc = contracts × $1` to `OpenPosition` and aggregate a
`committed_payout_by_event_cc` alongside the existing loss map. This is the number
the sweep's risk lens is really talking about, and it is the *true* bankroll
tie-up. The skew's `loss_util(e)` term (§A2) should read this new payout map, not
the premium-based one.

The committed-payout kill-switch wires into the existing `KillSwitch`
(`risk/killswitch.py`) via `maintenance_tick` (`lifecycle.py:455-463`, which
already halts on daily-loss) — add a committed-payout breach → `cancel_all`
(soft) or `killswitch.halt(HALT_...)` (hard). New reason codes:
`SKIP_COMBO_PAYOUT_CAP`, `SKIP_EVENT_PAYOUT_CAP`, `HALT_COMMITTED_PAYOUT`
(add to `core/reasons.py`).

### A5. Can we EVER actively hedge on the leg markets? Does sell-only allow it?

**Short answer: YES, it is *allowed*, and it does NOT violate sell-only — but it
is a deliberate later phase, and it is a strictly LEG-market action, never a
combo action.**

- **The sell-only mandate is a COMBO-quote constraint, not an account
  constraint.** `sell_parlays_only` lives on `QuoteParams`/`QuoteConfig` and is
  enforced only in the *combo* quote builders (`quote.py:221`,
  `engine.py:_enforce_sell_only`). It says "never be long a combo's YES." It says
  nothing about the single-leg markets. Buying or selling a *leg* (e.g. buying NO
  of "Team A wins" in the KXMLBGAME market) to offset a combo delta is a
  different instrument entirely and is exactly what `hedging/planner.py` is
  scaffolded for (`hedging/__init__.py:1` "per-leg hedges in the single markets
  via V2 orders").

- **Why it doesn't break the thesis.** We sell a combo NO ⇒ we are long
  `NOT(∏ legs)`. Our per-leg delta to each leg is signed (`analytic_leg_deltas`).
  Laying off a leg delta in the single market *removes the outright event risk on
  that leg* and leaves the **correlation residual** — precisely CLAUDE.md:247
  ("hedging converts outright event risk into correlation risk — the residual P&L
  IS the correlation position"). That residual is the book we actually want to
  run: our edge is the copula/structural correlation model, not a directional bet
  on who wins.

- **What it costs / when it's worth it.** The planner must account for the
  crossed spread + taker fee on the hedge leg (`hedging/planner.py:6-8`), so it's
  only +EV when |leg delta| is large (concentrated) AND the leg book is tight.
  This is the *active* complement to the *passive* skew of §A3: skew steers flow
  cheaply but slowly; a leg hedge pays a spread to cut a delta *now*. Correct
  sequencing: **skew first (free), caps/refusal second (free), leg-hedge last
  (costs spread), and only above a `delta_threshold_contracts`** (already the
  planner's knob, `planner.py:37`).

- **Gate.** Keep `HedgePlanner.plan` raising until (a) the top-down maker is
  net-profitable on a real multi-week sample (`planner.py:42-44`), (b) a hedge
  *executor* exists with its own limits/tests, and (c) leg-hedge fills reconcile
  to the cent (defense #3). Activating it is "changing what business we're in"
  (`planner.py:10-11`) — an operator decision, not a flag flip. **This design
  does not turn it on.** It specifies the interface so that when it turns on, the
  skew and the caps already contain the risk it's reducing.

### A6. Where the skew is computed and injected (the one wiring change)

Today `lifecycle.py:_price` (`:510-516`) calls `engine.price(...)` **without**
`inventory_skew_cc`, so it defaults 0. The enhancement is a single new step in
the hot path, using state the lifecycle already holds (`self._exposure`,
`self._marginals`):

```
# in QuoteLifecycle._price (or a new _inventory_skew helper called from handle_rfq)
snap = self._exposure.snapshot(self._marginals, mass_acceptance=True)   # already computed for limits
skew_cc = compute_inventory_skew(rfq, snap, self._risk_qty(rfq, ...), self._conventions,
                                 event_delta_cache=self._skew_cache,     # slow-path ΔES per event
                                 params=self._skew_params)
return self._engine.price(rfq, time_to_close_s=..., in_play=..., inventory_skew_cc=skew_cc)
```

`compute_inventory_skew` is a **pure function** (new module `risk/skew.py`,
sibling of `lastlook.py`), taking the snapshot + candidate + a `SkewParams`
config and returning an int — mirroring the pure `decide_confirm` design
(`lastlook.py:54`). It must never do I/O and must be property-tested for:

1. **Sign safety (the load-bearing test):** an offsetting candidate returns
   `skew_cc ≤ 0` (tightens), a concentrating one returns `skew_cc ≥ 0` (widens);
   a candidate touching an empty book returns exactly 0.
2. **Sell-only invariant survives:** across all skew values (incl. large negative)
   the emitted `yes_bid` is still 0 — this already holds by construction
   (`engine._enforce_sell_only`) and is fuzzed at `tests/test_quote.py:519-534`;
   extend that fuzz to draw `skew` from `compute_inventory_skew`'s range.
3. **No-arb survives:** with a large negative (tightening) skew, `no_bid` never
   exceeds `no_cap_cc − free_money_margin_cc` (already clamped `quote.py:202`;
   assert the clamp fires and the capture check `quote.py:230` still passes).

**Config:** new `SkewConfig` on `PricingConfig` (or `RiskConfig`) — `w_conc`,
`w_off`, `gamma`, `skew_max_widen_cc`, `skew_max_tighten_cc`, `enabled=False`
default (ships dark; validate on shadow/markouts before enabling, same discipline
as `favorite_width_multiplier` `config.py:1474` and `farm_*`). While `enabled=False`
the computed skew is **logged but passed as 0** — a zero-P&L shadow, exactly the
sweep's "ship the room predictor as a shadow classifier first"
(`2026-07-12-pnl-markup-sweep.md:106-107`).

### A7. Interaction with the room predictor (keep them separate)

The sweep's headline lever is a **pre-quote room predictor** (predict FAT vs
NORMAL flow, quote FAT fat / NORMAL thin-or-skip). That is a **markup** decision
(how much edge to charge based on predicted maker room). Inventory skew is an
**exposure** decision (how much to shade based on our own book). They compose
additively in `no_raw` and must stay separate config/log fields so neither masks
the other's calibration. Do NOT fold skew into the room predictor — one is about
the market, one is about us.

---

## PART B — Hardening the strictly-pregame selector vs courtsiders (no flow loss)

### B0. What already exists (`rfq/pregame.py`, read first — `filters.py`, `lastlook.py`)

The gate is genuinely good already. Enhancements are precision + measurement, not
a rebuild.

| Piece | Where | State |
|---|---|---|
| Fail-closed start-time chain (embedded ET → expiry−offset → UNKNOWN) | `rfq/pregame.py:72-160` | SHIPPED, ACTIVE |
| KXMLB embedded ET start, API-verified to +3.00h | `pregame.py:58-97`; evidence `docs/reports/2026-07-10-phase3-pregame-gate.md:52-81` | VERIFIED (18/18 markets) |
| Estimate = `min(close, exp_exp) − offset`, default **4.5h**, MLB 4.0 | `pregame.py:136-160`, `config.py:110-122` | SHIPPED |
| `now >= start` ⇒ STARTED (first pitch is in-play) | `pregame.py:132,38` | SHIPPED |
| Quote-time gate | `filters.py:95,103-112` (`SKIP_INPLAY_LEG`, `SKIP_START_TIME_UNKNOWN`) | SHIPPED |
| **Last-look straddle re-check** (leg goes live between quote and accept) | `lifecycle.py:641-643,652-653` → `lastlook.py:67-70` (`DECLINE_INPLAY_LEG`, `DECLINE_START_TIME_UNKNOWN`) | SHIPPED |
| Market-motion (courtside) detector, independent backstop | `risk/inplay.py` (velocity/update-rate → cooldown) | SHIPPED |
| Close-time proximity gate | `filters.py:158-177` (`min_time_to_close_s`) | SHIPPED |

**The three defenses already stack** (schedule gate ∥ motion detector ∥
close-time), and the last-look re-check already closes the quote→accept straddle.
So the courtsider ("someone at the event building live combos") is defended in
depth *today*. The task is to make it **PERFECT** on two axes the operator named:
(1) per-sport start precision so we never pad blindly, (2) tune the buffer by
*measuring* pickoff risk vs flow loss instead of guessing 4.5h.

### B1. Per-sport start-time precision (replace the estimate where a real feed exists)

The estimate path (`pregame.py:143-153`) is the weak link: `expiry − offset` with
a fixed 4.5h offset is a blunt instrument that, per the report
(`2026-07-10-phase3-pregame-gate.md:88-92`), can decline up to ~1.5h of genuine
pregame flow near kickoff. The fix is a **precision ladder** per source quality,
extending the existing `embedded_start_time` allowlist pattern (`pregame.py:58`):

1. **Verified embedded start (best).** Already done for KXMLB (ET token, +3h
   verified). **Extend `_EMBEDDED_START_SERIES`** (`pregame.py:58`) to any series
   whose ticker embeds the start, but *only* after the same hard-rule-5 API
   verification the report documents (`:52-81`) — one report per family, or it
   falls through. Candidate next: World Cup KXWC if a start token is found in the
   game code (the report measured exp_exp = kickoff + 2.95–3.95h but did **not**
   find an embedded start token — so KXWC stays on the estimate until proven).

2. **Explicit schedule feed (new tier, between embedded and estimate).** For
   sports without an embedded token but with a reliable public schedule
   (soccer/MLB/NFL/NBA fixture APIs), add a `ScheduleCache` (sibling of
   `MetadataCache`) keyed by `event_ticker` → scheduled UTC start, refreshed off
   the hot path (like metadata). `leg_start_time` (`pregame.py:136`) gains a step
   between (a) and (b): if the schedule cache has the leg's event, use its exact
   start (minus a tiny latency margin, ~2 min, not 4.5h). **This is the flow
   recovery**: an exact start lets us quote right up to ~2 min before kickoff
   instead of losing the last 1.5h. Fail-closed: cache miss ⇒ fall through to the
   estimate ⇒ UNKNOWN, never a guess. The mapping `event_ticker → fixture` is an
   **explicit table** (defense #2, same rule as the SGO mapping `config.py`), no
   fuzzy matching.

3. **Estimate (current fallback).** Keep `expiry − offset` for families with no
   embedded token and no schedule mapping. Keep it **conservative** (4.5h) —
   flow loss on unmapped families is acceptable; an in-play quote is not.

4. **UNKNOWN ⇒ decline** (`pregame.py:37`, unchanged).

Per-sport offset overrides already exist (`pregame_start_offset_hours_by_prefix`,
`config.py:122`) — the enhancement is to shrink them *only* for families where a
schedule feed or embedded token backs a tighter number, with the measurement of
§B3 as the gate.

### B2. Safety-margin tradeoff + last-look re-check (mostly built; tighten timing)

The tradeoff is explicit in the config comment (`config.py:113-117`): bigger
buffer = safer but loses late-pregame flow; too small quotes in-play. The
enhancements:

- **Two-sided margin.** Today the offset is one number applied to `expiry`. With
  a precise start (tier 1/2), split it into (i) a **quote-cutoff margin**
  (stop quoting `M_q` before start — the flow knob, can be small, ~2–5 min with a
  real feed) and (ii) a **confirm-cutoff margin** (`M_c ≥ M_q`, the safety knob,
  used at last look). This lets us quote later (recover flow) while keeping the
  confirm decision strict.

- **The last-look re-check is the real courtsider defense and it already exists**
  (`lifecycle.py:641-643`, `lastlook.py:67-70`). A courtsider's edge is the
  seconds between the event and the feed; the straddle window (quote→accept, up
  to the quote TTL of 30s, `lifecycle.py:79`) is exactly when a leg can tick live.
  Because the pregame gate is **re-evaluated at confirm** with a fresh clock read
  (`pregame.py:125 self._clock.now()`), a leg that crossed its start in that
  window declines (`DECLINE_INPLAY_LEG`). **Enhancement:** at last look, apply the
  stricter `M_c` margin (decline if `now ≥ start − M_c`), so the *confirm* side
  keeps a hard safety buffer even when the *quote* side was tightened for flow.
  This is a pure-function change in `lastlook.py` (add the margin to the
  comparison) fed by a `start − M_c` precomputed in `_last_look_inputs`
  (`lifecycle.py:598-658`) — no new I/O.

- **Belt: the motion detector covers feed-lag.** Even if a start time is slightly
  wrong, `risk/inplay.py` fires on the first anomalous tick (velocity/update-rate)
  and is checked both at quote (`lifecycle.py:515`) and last look
  (`lastlook.py:71` via `velocity_anomaly`). A courtsider trading a real live edge
  moves the leg book, which trips this independent of the schedule. Keep it; it is
  the reason the buffer can be tightened at all — schedule precision and motion
  detection are complementary (schedule = never quote the known-live; motion =
  catch the wrongly-timed).

### B3. Measuring pickoff risk vs flow loss (how to tune the buffer honestly)

The operator wants the buffer tuned by data, not by a padded guess. Both
quantities are measurable from the recorder tape + settlements, and the
measurement must obey the standing rules (pre-registered, multi-week,
game-clustered, never refit on P&L — memory `feedback_no_refit_on_pnl`,
`2026-07-12-...sweep.md:113-118`). Design a **shadow measurement**, not a live
sweep:

**Flow loss (cheap, direct):** For each near-kickoff RFQ we DECLINE with
`SKIP_INPLAY_LEG`/`SKIP_START_TIME_UNKNOWN`, log `time_to_start` at decline
(computable from the gate's own `leg_start_time`). The distribution of declines
in the `[start − 4.5h, start]` window, bucketed by minutes-to-start, is the flow
we forgo. Cross it with the ground-truth start (embedded ET for MLB, or the
schedule feed) to get **flow lost per minute of buffer** — a pure counting
exercise on the decision log (`store.record_decision`, `lifecycle.py:710-716`),
zero P&L, runnable today on the recorder tape.

**Pickoff risk (the dangerous side, measured via markouts — reuses defense #5):**
The system *already* records markouts on **declined** confirms
(`markouts.py`, `lifecycle.py:316 declined:<quote_id>`) precisely so a decline
can be graded "dodged bullet or spurned profit"
(`risk/markouts.py:1-8`). To measure pickoff:
- Run a **shadow gate** with a tighter buffer (e.g. `M_c` = 10 min instead of
  4.5h) that does NOT change live behavior — it only *labels* which RFQs the
  tighter gate WOULD have quoted/confirmed.
- For those shadow-admitted near-kickoff RFQs, compute the markout of the
  *hypothetical* NO position against the leg-mid product at +10s/+1m
  (`markouts.py` horizons). A courtsider pickoff shows up as a systematically
  ADVERSE short-horizon markout on RFQs admitted inside the buffer that would
  have been safe outside it — i.e. the leg moved against us right after, the
  signature of trading against someone who saw the event first.
- **The tuning rule:** shrink the buffer only to the point where the
  game-clustered lower-CI-bound short-horizon markout of buffer-admitted flow is
  still ≥ 0 (non-adverse), pooled over ≥K games. Everything inside that boundary
  is safe flow to recover; everything beyond is pickoff. This mirrors the sweep's
  markup discipline exactly (pooled lower-CI bound crossing zero,
  `2026-07-12-...sweep.md:117-118`) and reuses the markout infra rather than
  building a new one.

**Ship order (matches the sweep's shadow-first philosophy):** (1) log
`time_to_start` on every pregame decline (trivial, today); (2) build the schedule
feed + `ScheduleCache` for one sport (MLB has embedded ET already — start with a
sport that lacks it, e.g. soccer, where the 4.5h estimate costs the most flow);
(3) run the shadow tighter-buffer gate + declined-confirm markouts for ≥3–4
game-clustered weeks; (4) tighten `M_q`/`M_c` per sport only where the pooled
markout says it's safe. Never tighten off a single window.

### B4. Courtsider defense summary (what makes it "perfect")

```
        RFQ ──► quote-time gate ──► price/risk ──► quote ──(straddle, ≤30s)──► accept ──► LAST LOOK ──► confirm
                 │                                                                          │
   (1) schedule: never quote a leg whose game is known-started                  (1') schedule RE-checked, stricter M_c
       precision ladder: embedded-ET > schedule-feed > estimate > UNKNOWN            fresh clock read, pregame.status re-run
   (2) motion detector: velocity/update-rate anomaly ⇒ cooldown                 (2') motion detector re-checked
   (3) close-time proximity gate                                                (3') leg-move / leg-age / joint-move gates
   fail-closed: UNKNOWN start ⇒ decline (both ends)                             severity-ordered, every None ⇒ decline
```

Two independent axes (schedule truth + market motion), re-evaluated at both
quote and confirm, fail-closed on unknowns, with the buffer tuned by measured
pickoff markouts rather than padding. Flow is recovered by *precision* (exact
starts let us quote to ~2 min out) rather than by *loosening safety* (the confirm
margin `M_c` stays strict). That is the "safer AND more flow" resolution the
operator asked for — you don't trade one for the other, you replace a blunt
estimate with a precise feed.

---

## Concrete change list (all enhancements to existing files; nothing greenfield)

**A — skew / balancing**
- NEW `src/combomaker/risk/skew.py`: pure `compute_inventory_skew(rfq, snapshot,
  qty, conventions, cache, params) -> int` (sibling of `lastlook.py`).
- `rfq/lifecycle.py:_price` (`:510`): compute skew from `self._exposure.snapshot`
  and pass `inventory_skew_cc=` into `engine.price` (today defaults 0).
- `risk/exposure.py`: add `max_payout_cc` to `OpenPosition`; aggregate
  `committed_payout_by_event_cc` in `snapshot`.
- `risk/limits.py` + `RiskLimits`: `max_combo_payout_dollars`,
  `max_event_committed_payout_dollars`, committed-payout cancel-all/halt fracs.
- `ops/config.py`: NEW `SkewConfig` on `PricingConfig` (`enabled=False`, weights,
  caps); new payout limit fields on `RiskConfig` (`:1632`).
- `core/reasons.py`: `SKIP_COMBO_PAYOUT_CAP`, `SKIP_EVENT_PAYOUT_CAP`,
  `HALT_COMMITTED_PAYOUT`.
- Tests: extend `tests/test_quote.py:519` sell-only fuzz to draw skew from the new
  fn; new `tests/test_skew.py` for sign-safety + no-arb-survives + offset-rebate-
  bounded; property test that concentrating candidates monotonically widen.
- `hedging/planner.py`: leave gated; document that skew + payout caps are its
  prerequisites (the residual it reduces is already contained).

**B — pregame hardening**
- `rfq/pregame.py`: add schedule-feed tier to `leg_start_time` (`:136`) between
  embedded and estimate; add `ScheduleCache` (new, sibling of `MetadataCache`);
  split offset into quote-margin `M_q` / confirm-margin `M_c`.
- `risk/lastlook.py`: apply stricter `M_c` at confirm (decline if
  `now ≥ start − M_c`), fed by a precomputed field in `lifecycle._last_look_inputs`.
- `ops/config.py:FiltersConfig`: `M_q`/`M_c` per-prefix; keep 4.5h estimate default.
- `rfq/lifecycle.py`: log `time_to_start` on every pregame decline (flow-loss
  measurement input); shadow tighter-buffer labeler for §B3.
- Measurement (tools/, never edits live modules — CLAUDE.md rule 8): flow-loss
  counter on the decision log + declined-confirm markout analyzer reusing
  `markouts.py`; tune `M_q`/`M_c` per sport off pooled game-clustered markout CIs.

## Assumptions / open items to verify (defense #6 discipline)
- `combo_no_pays_complement` is still verified-True only from ONE $1.00 settlement
  (memory); the committed-payout = `contracts×$1` for a NO position assumes the
  standard binary payout — re-check against the next real combo settlements.
- The schedule-feed mapping is a new external dependency; it must be an explicit
  table with fail-closed misses, and its start times need the same hard-rule-5
  API cross-check the embedded-ET path got (`2026-07-10-phase3-pregame-gate.md`).
- Skew `enabled=False` at ship; only the shadow log runs until markouts justify
  turning it on (never refit on P&L; the weights are structural, tuned on
  exposure-vs-markout, not on a P&L window).
