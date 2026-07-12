# R1 — Position-Book / Exposure-Accounting Model

**Enhancement design for the parlay-seller combo maker (kalshi-combos-TWO).**
Ground: `src/combomaker/risk/exposure.py`, `risk/limits.py`, `sim/engine.py`,
`pricing/{engine,relationships,copula,sgp}.py`, `rfq/{models,lifecycle,pregame}.py`,
`core/{money,quantity}.py`. READ-ONLY research — no code edits. All file:line
citations are to the current tree.

---

## 0. The operator's ask, restated precisely

> "It needs to be like a book: game X, legs x1 x2, $s allotted to each, % chance
> of this leg hitting. Not too much one-directional bias on legs."

Three things the operator wants that today's `ExposureBook` does **not** give:

1. A **GAME-rooted hierarchy** (game → leg → combos → $), not a flat
   market-delta / event-delta pair of dicts.
2. **Directional bias per leg** stated in a way an operator can read ("we are net
   short *the same side* of leg X across N combos").
3. A **correlated per-game worst case** — every combo on a game resolving the
   adverse way *together* — because the risk unit is the GAME, not the ticker.

And two things the P&L sweep proved we need but the operator did not name:

4. Separate **cost-at-risk** (premium we paid = our true max loss) from
   **payout obligation / bankroll lock-up** (contracts × $1). The sweep's
   "$23.5M max payout for $1.8M premium" conflates these; the book must show
   both, on different axes, or every capital decision is wrong.
5. The **marginal exposure of a candidate RFQ** answered in O(legs), hot-path
   safe, so acceptance is gated *before* the mass-acceptance buildup the naive
   sim produced.

---

## 1. What `exposure.py` does today (grounded)

```
ExposureBook (exposure.py:131)
  positions: dict[position_id -> OpenPosition]          # confirmed fills
  open_quotes: dict[quote_id -> OpenQuoteRisk]          # resting, instantly executable

  snapshot(marginals, mass_acceptance, extra_positions) -> ExposureSnapshot
      delta_by_market : dict[ticker -> float]           # contracts-equiv delta
      delta_by_event  : dict[event_ticker -> float]     # summed per raw event
      gross_notional_cc                                 # Σ max_loss_cc
      worst_case_loss_by_event_cc : dict[event -> int]  # Σ max_loss_cc per event
      open_quote_count
      unknown_marginals : bool
```

- **Delta** = independence product formula (`analytic_leg_deltas`, exposure.py:93),
  contracts-equivalent = ∂(portfolio $) / ∂P(leg YES). Signed by our side ×
  leg side. Missing marginal ⇒ `None` ⇒ `unknown_marginals=True` (never zero).
  Audit **E1** (NOTES.md:241).
- **Max loss per position** = `contracts × entry_price_cc / 100` (exposure.py:57).
  This is the **premium we paid** — correct for a bought contract (both sides of
  our quote are bids; Kalshi never margin-calls a long). Audit **E3**
  (NOTES.md:243, still UNVERIFIED vs ground truth).
- **Mass acceptance** (exposure.py:184): every open quote assumed to fill NOW on
  its per-aggregate *worse* side, sign-aligned magnitude bound (a conservative
  upper bound, never an average). Dominance property-tested. Audit **E2**
  (NOTES.md:242). This is the single most valuable thing already in the module
  and the enhancement must preserve it verbatim.
- **Event key** = the **raw `leg.event_ticker`** (exposure.py:174, 181, 191, 208).
- `LimitChecker.check` (limits.py:54) consumes the snapshot and enforces
  per-market delta, per-event delta, gross notional, per-event worst-case loss,
  open-quote count, daily loss. Candidate positions + `adding_quote` model the
  next fill / next quote.

**The dominance property is the crown jewel. Everything below extends the book
*around* it, never replaces it.**

---

## 2. The five structural gaps (top 5, ranked)

| # | Gap | Where | Consequence |
|---|-----|-------|-------------|
| **G1** | **Event key ≠ game key.** Aggregation uses raw `leg.event_ticker`; the correlation layer uses `_game_key` = the GAMECODE after the series prefix (`relationships.py:288`). `KXWCGAME-26JUL05MEXENG` and `KXWCTOTAL-26JUL05MEXENG` are ONE game but TWO events. | `exposure.py:174,181,191,208` vs `relationships.py:302` | The per-game correlated worst case — the operator's actual risk unit — is **silently split across market families**. Two legs of the same match land in different `worst_case_loss_by_event_cc` buckets; the per-event cap never sees the true game concentration. Directly the "68 combos = 50% of contracts, single combos carried ~$1M swings" failure. |
| **G2** | **No payout-obligation / bankroll axis.** The book tracks only `max_loss_cc` (premium). The "$23.5M max payout for $1.8M premium" number — the collateral a parlay seller ties up — has **no field, no cap, no query**. | `exposure.py:57,167,176` | Capital-allocation and concentration limits are blind to the dimension that actually dominates a seller's book. A cap on premium-at-risk does nothing to stop tying up the whole bankroll in potential payouts. |
| **G3** | **No correlated (comonotone) per-game worst case.** `worst_case_loss_by_event_cc` is a plain **sum of per-position premium**, which for cost-at-risk is coincidentally the true joint worst case (all NO positions on a game lose their premium iff every combo hits) — BUT it is expressed on the wrong axis (premium, not payout) AND there is no "what if this whole game breaks against us" number that combines payout obligation with the actual joint-hit scenario. The MC engine (`sim/engine.py`) that could compute this is **not wired into the book at all** (learning #4). | `exposure.py:126,176`; `sim/engine.py:225` unused | The book reports a worst case that is right for premium and absent for payout; the real "one game, adverse resolution, all combos together" figure is uncomputed. |
| **G4** | **No leg directional-bias view.** `delta_by_market` sums signed deltas, so two combos short the *same* leg side and one long it **net toward zero** and look balanced when we are in fact concentrated. The operator's "not too much one-directional bias" needs **gross same-side exposure**, not net delta. | `exposure.py:172,201` | A leg we are massively one-directional on reads as flat. This is the exact adverse-selection surface (many combos sharing a popular leg) the sweep flagged. |
| **G5** | **No hierarchical / per-leg-hit-probability structure and no candidate-marginal query.** The output is two flat dicts; there is no game→leg→combo tree, no per-leg P(hit) attached, no "expected vs worst-case" per node, and no single call that returns *this candidate RFQ's* marginal contribution to each level. `_price` recomputes the joint but the book never stores per-leg P(hit) or per-combo P(hit). | `exposure.py:121-129` (snapshot shape) | The operator cannot read the book as a book; acceptance can't be reasoned about marginally; the "% chance of this leg hitting" column the operator explicitly asked for does not exist. |

---

## 3. The enhancement: a GAME-rooted position book

### 3.1 Core identity fix (prerequisite for everything)

Introduce **one** game-key function and use it *everywhere the book aggregates*:

```
game_key(leg) = _game_key(leg.event_ticker)   # relationships.py:288, already the
                                              # correlation truth; import, don't re-derive
```

- Import the existing `_game_key` (promote it to a public `pricing.grouping`
  symbol so `risk/` may import it without reaching into a private name — this is
  the *only* code-movement the design needs, and it introduces no new logic; it
  is the same function the copula already trusts, so there is zero drift risk and
  it satisfies the "ground everything in existing code" bar).
- A leg with no hyphen in its event, or `event_ticker is None`, keys on the whole
  string / a synthetic `"__ungamed__:<market_ticker>"` sentinel so it **never**
  merges with another game (fail-closed, matching `_game_key`'s own no-hyphen
  branch, relationships.py:303).

This single change closes **G1** and is load-bearing for G3/G4.

### 3.2 The hierarchy (data model)

```
Book
├── games: dict[GameKey -> GameNode]
├── legs:  dict[LegKey  -> LegNode]        # LegKey = (market_ticker, side)
└── combos: dict[position_id | quote_id -> ComboNode]

GameNode(game_key)
    leg_keys:            set[LegKey]                 # legs of this game we touch
    combo_ids:           set[str]                    # combos touching this game
    # --- aggregates, recomputed incrementally ---
    premium_at_risk_cc:  int      # Σ over combos touching game of premium_at_risk
    payout_obligation_cc:int      # Σ contracts×$1 of combos touching game  (G2)
    worst_case_loss_cc:  int      # correlated adverse resolution (G3, §3.4)
    expected_loss_cc:    int      # Σ P(combo adverse)·loss_if_adverse (from P(hit))
    n_combos:            int

LegNode(market_ticker, side)                          # the operator's "leg x1"
    game_key:            GameKey
    p_hit:               float | None                 # P(this leg's SELECTED side
                                                       # settles the adverse way)  (G5)
    net_delta:           float          # signed Σ (existing E1 delta)  — hedge view
    gross_same_side_cc:  float          # Σ |delta| of combos short THIS side   (G4)
    gross_opp_side_cc:   float          # Σ |delta| of combos long  THIS side   (G4)
    directional_bias:    float          # gross_same - gross_opp, normalized
    premium_at_risk_cc:  int            # Σ over combos of (this leg's $ share)  (§3.3)
    combo_ids:           set[str]

ComboNode(id)                                          # a position OR an open quote
    is_quote:            bool           # False = confirmed position, True = resting
    our_side:            Side           # NO for every sell-only fill
    contracts:           int            # centi-contracts
    entry_price_cc:      int            # our bid = premium/contract
    legs:                tuple[LegRef]  # (market_ticker, event_ticker, side)
    game_keys:           frozenset[GameKey]
    p_hit:               float | None   # P(combo settles the way that costs us)  (G5)
    premium_at_risk_cc:  int            # = max_loss_cc (exposure.py:57)  cost axis
    payout_obligation_cc:int            # = contracts×$1 // 100           bankroll axis (G2)
    expected_loss_cc:    int            # p_hit · (payout_obligation - premium collected)
```

`LegRef` / `OpenPosition` / `OpenQuoteRisk` (exposure.py:34-90) are **reused
unchanged** as the leaf records; `ComboNode` is a thin index over them plus the
two new money axes and `p_hit`. The book is a *view/index built from the existing
position + quote dicts*, not a parallel source of truth.

### 3.3 The two money axes (closes G2) — exact math, sell-only

Sell-only means every fill is `our_side = NO`, `entry_price_cc = no_bid`. On
Kalshi a bought NO contract:

- **settles NO (combo MISSES, ~75%)** → pays $1 → profit `= $1 − no_bid` per ct.
- **settles YES (combo HITS)** → pays $0 → we lose the premium `no_bid` per ct.

So, per position (all in integer cc, contracts in centi-contracts):

```
premium_collected_cc = contracts × no_bid                       // 100   (what taker paid us for YES)
                                                                          NOTE: taker pays $1−no_bid; WE paid no_bid to hold NO
premium_at_risk_cc   = contracts × entry_price_cc               // 100   ==  max_loss_cc (exposure.py:57) — TRUE max loss
payout_obligation_cc = contracts × CC_PER_DOLLAR                // 100   ==  contracts × $1  (bankroll lock-up)  (G2)
loss_if_hit_cc       = premium_at_risk_cc                                (we forfeit the premium; NO pays 0)
```

The critical clarification the sweep muddled: **our economic max loss on a NO
position is the premium (`premium_at_risk_cc`), NOT the payout obligation.**
Kalshi collateralizes the $1 but never charges us more than we paid — the payout
goes to the taker, funded by the collateral the *taker* posted for their YES.
The "$23.5M" figure is **bankroll lock-up / opportunity cost / worst-case
gross settlement flow**, a real and dominant constraint for a seller, but it is a
*capital-utilization* axis, not a *loss* axis. The book must carry **both** and
never sum them into one number:

- **Premium axis** (`premium_at_risk_cc`): feeds the existing daily-loss and
  gross-notional caps — this is genuine P&L-at-risk.
- **Bankroll axis** (`payout_obligation_cc`): feeds a NEW concentration cap —
  "no single game may lock up > X% of collateral", "book-wide payout obligation
  ≤ bankroll × utilization ceiling". This is the axis that stops the
  $23.5M-for-$1.8M mass buildup.

**Per-leg $ allotted** (the operator's "$s allotted to each leg"): a combo's
premium/payout is attributed to each of its legs by an explicit, documented
rule. Two supported attributions, both O(legs):

- **Equal split** (default, transparent): `combo.$ / n_legs` to each leg. Simple,
  operator-legible, order-independent.
- **Responsibility split** (optional): weight leg *i* by `(1 − p_i^sel) /
  Σ_j(1 − p_j^sel)` — the leg most likely to be the one that *saves* the NO
  (i.e. the leg most likely to miss) carries the most "credit". This matches the
  intuition that a longshot leg is doing the most work for a parlay seller.

Attribution is a **display / soft-limit** concept (per-leg $ headroom), never the
hard worst-case (which is joint — §3.4). Documented as such so no one mistakes a
split premium for a real per-leg loss bound.

### 3.4 Per-game correlated worst case (closes G3)

The operator's real risk unit. Two tiers, cheap→exact:

**Tier 1 — analytic comonotone bound (hot path, always available).**
For a game, the adverse scenario is "every combo touching this game hits."
Because our positions are all NO and losing means the combo settled YES, the
*joint* worst case per game is simply:

```
game.worst_case_premium_cc = Σ over combos touching game of premium_at_risk_cc
game.worst_case_payout_cc  = Σ over combos touching game of payout_obligation_cc
```

This is exactly what `worst_case_loss_by_event_cc` computes today (exposure.py:176)
— but (a) **re-keyed to game not event** (G1 fix), and (b) **also carried on the
payout axis** (G2 fix). It is the correct comonotone upper bound for the premium
axis with no MC needed, because a NO position's loss is bounded by its premium
regardless of correlation — correlation only affects *how often* the whole game
goes adverse, not the loss magnitude when it does. **So Tier 1 is exact for the
worst-case magnitude and needs no engine.**

**Tier 2 — MC-graded expected & tail loss (slow full-book refresh).**
Correlation *does* drive the **probability** of the adverse scenario and the
**distribution** between best and worst — that is what the operator wants for
"expected vs worst-case." Wire the **already-built** `sim/engine.py` (currently
unused by the book, learning #4):

- Build the leg universe = distinct `(market_ticker, side)` across the book;
  `LegModel(p = p_hit)` from marginals (engine.py:34).
- Build `corr` per game-block using the SAME `_game_key` grouping + the SAME
  `SgpParams` the pricer uses (`pricing/sgp.py:build_sgp_correlation`), so the
  risk view and the price view share one correlation truth (defense against a
  biased-fair self-grading, CLAUDE.md defense #5).
- Map each `ComboNode` → `ComboPosition(leg_indices, side="no", contracts,
  price_cc)` (engine.py:73) and call `simulate(...)` (engine.py:225) for
  book-level `EV / VaR / ES / P(loss > threshold)` and `marginal_impact(...)`
  (engine.py:240) for a candidate on common random numbers.
- **Per-game** VaR: run `simulate` on the sub-book of one game's combos to get
  that game's tail. This is the "$1M payout swing on a single game/combo"
  number, now measured instead of feared.

Tier 1 gates the hot path (every quote/accept); Tier 2 runs on the maintenance
tick (`lifecycle.py:455`) and on demand, exactly the E1 split already documented
(NOTES.md:241 — "conditional-MC deltas reserved for the slow full-book refresh").

### 3.5 Per-leg directional bias (closes G4)

Keep the existing **net** `delta_by_market` (it is the correct *hedging* view —
CLAUDE.md "hedging converts outright event risk into correlation risk"). ADD a
**gross same-side** accumulation so a one-directional pile-up is visible:

```
for each combo touching leg L on side s:
    gross_same_side_cc[L,s] += |delta contribution|          # same selected side
    gross_opp_side_cc[L,s]  += |delta contribution|          # opposite side combos
directional_bias(L) = (gross_same - gross_opp) / (gross_same + gross_opp)   ∈ [-1, 1]
```

`|bias|` near 1 = we are almost entirely one-directional on that leg (the
operator's alarm); near 0 = balanced. A per-leg cap on `gross_same_side_cc`
(distinct from the existing net `max_market_delta_contracts`, limits.py:26) is
the new lever. This is O(1) per combo to maintain, needs no MC.

### 3.6 Per-leg hit probability attached (closes G5, the operator's "% chance")

`p_hit` on every `LegNode` and `ComboNode`:

- **Leg**: `p_hit = 1 − selected_marginal` for the ADVERSE outcome from a
  seller's view = P(this leg settles the way that lets the combo hit) = the
  selected-side marginal `p if side=="yes" else 1−p` (this IS the leg's
  contribution the joint is built from, engine.py:225 style). Displayed as the
  operator's "% chance of this leg hitting."
- **Combo**: `p_hit` = the joint P(combo settles YES) — pulled from the same
  `JointEstimate.p` the pricer already computed at quote time
  (`pricing/engine.py:price` → `joint.p`). **Store it on the quote/position at
  book-insert time** so the book never re-prices (hot-path safe). If absent
  (position predates a book upgrade), recompute lazily via the analytic
  independence product as a documented lower-fidelity fallback, flagged.
- **Expected vs worst-case** per node: `expected_loss_cc = p_hit ·
  (payout_obligation_cc − premium_collected_cc)` vs `worst_case = §3.4`. The
  book shows both columns the operator asked for.

---

## 4. Incremental update path (hot-path safe) + mass acceptance

Acceptance must not O(book)-rescan. Every mutation is O(legs of the touched
combo):

```
on_fill(position)  /  on_quote(quote):
    combo = ComboNode.from(position|quote)          # money axes + p_hit precomputed
    for leg in combo.legs:
        leg_node = legs.setdefault((leg.market_ticker, leg.side), LegNode(...))
        leg_node.combo_ids.add(combo.id)
        leg_node.net_delta        += signed_delta_i          # E1 formula, exposure.py:93
        leg_node.gross_same_side  += |signed_delta_i|        # G4
        leg_node.premium_at_risk  += combo.premium_share_i   # §3.3 split
        game = games.setdefault(game_key(leg), GameNode(...))
        game.leg_keys.add((leg.market_ticker, leg.side)); game.combo_ids.add(combo.id)
    for game in combo.game_keys:                             # each game ONCE per combo
        games[game].premium_at_risk_cc   += combo.premium_at_risk_cc
        games[game].payout_obligation_cc += combo.payout_obligation_cc    # G2
        games[game].worst_case_loss_cc   += combo.premium_at_risk_cc      # Tier-1 §3.4
        games[game].expected_loss_cc     += combo.expected_loss_cc
on_remove(id): symmetric subtraction (quote lapse / TTL / position settle)
```

**Mass-acceptance worst case is PRESERVED exactly.** The existing dominance
computation (exposure.py:184-214) already assumes every open quote fills NOW on
its per-aggregate worse side (sign-aligned magnitude bound, E2). The enhanced
book keeps that as the authoritative `snapshot(mass_acceptance=True)` path — the
incremental game/leg indices above are the *steady-state* view; the
mass-acceptance snapshot is the *stress* view layered on top, unchanged. Both the
FIX `PreferBetterQuote` reality (any resting quote can land on us — NOTES.md:18)
and the "many resting quotes accepted at once" worst case remain covered by the
already-tested dominance bound. The new game/payout axes simply *also* get the
sign-aligned mass-acceptance treatment (worst side per game, summed), so the
$-lock-up caps see the mass-acceptance ceiling too.

---

## 5. Queries the book must answer in real time

| Query | Level | Cost | Backing |
|-------|-------|------|---------|
| Current premium-at-risk / payout-obligation per leg | leg | O(1) | LegNode fields |
| Directional bias per leg (gross same-side, bias ratio) | leg | O(1) | §3.5 |
| P(hit) per leg / per combo | leg/combo | O(1) | §3.6, stored at insert |
| Per-game correlated worst case (premium + payout) | game | O(1) | §3.4 Tier 1 |
| Per-game expected loss & tail (VaR/ES) | game | O(MC) on tick | §3.4 Tier 2, `sim/engine.py` |
| Headroom vs each cap (premium, payout, delta, bias, game worst-case) | any | O(1) | cap − current |
| **Marginal exposure of a candidate RFQ** at every level | candidate | O(legs) + optional 1 MC | §4 dry-run + `marginal_impact` engine.py:240 |
| Mass-acceptance worst case (all resting quotes fill) | book/game/leg | O(quotes) | exposure.py:184 dominance, extended to new axes |
| Book-wide bankroll utilization = Σ payout_obligation / collateral | book | O(1) | G2 |

The **candidate-marginal** query is the acceptance gate: given a would-be
position, compute its per-leg deltas / premium / payout **without mutating**, add
to current + mass-acceptance snapshot, and return the breach set — precisely how
`LimitChecker.check(..., candidate_positions=..., adding_quote=True)`
(limits.py:54, called at lifecycle.py:166 and :635) already works, now extended
with the game-keyed payout/bias caps. New `RiskLimits` fields to add alongside
the existing ones (limits.py:22): `max_game_worst_case_payout_dollars`,
`max_book_payout_utilization`, `max_leg_gross_same_side_contracts`,
`max_game_worst_case_loss_dollars` (the game-keyed successor to the current
per-*event* worst-case, limits.py:31).

---

## 6. Correctness invariants (must hold for "100% correctness")

1. **Game key = correlation key.** The book aggregates on the exact `_game_key`
   the copula correlates on (relationships.py:288). One function, imported, never
   re-derived. (Closes G1; prevents the split-game blind spot.)
2. **Loss axis ≠ bankroll axis, never summed.** `premium_at_risk` is the only
   number that feeds loss/daily-loss caps; `payout_obligation` is the only number
   that feeds utilization/concentration caps. (Closes G2; the sweep's core
   conflation.)
3. **Worst case is joint, not per-leg.** Per-leg $ splits (§3.3) are display/soft
   headroom only; the hard worst case is the per-game comonotone sum (§3.4).
4. **UNKNOWN is never zero.** Any missing marginal / missing `p_hit` /
   ungrouped-game sentinel propagates as UNKNOWN → breach, exactly as
   `unknown_marginals` does today (exposure.py:198, limits.py:105). A leg with no
   game key never merges (fail-closed).
5. **Mass-acceptance dominance preserved.** The E2 sign-aligned bound
   (exposure.py:184, property-tested) is untouched; new axes get the same worse-
   side treatment. Any enhancement that weakens the bound is rejected.
6. **Sign/direction only from `Conventions`.** `our_side`, payout-complement, and
   the NO-pays-$1 fact come from `Conventions.combo_no_pays_complement`
   (verified 2026-07-10) — never hardcoded in the book (CLAUDE.md defense #1).
7. **Incremental == full-recompute.** A property test must assert the O(legs)
   incremental book equals a from-scratch rebuild on the same positions/quotes
   (guards the add/remove arithmetic), mirroring the existing dominance test.

---

## 7. Parity & isolation (CLAUDE.md hard rules 7, 8)

- The book is a **new module** (`risk/book.py` conceptually) that *imports and
  reuses* `exposure.OpenPosition/OpenQuoteRisk/analytic_leg_deltas`,
  `pricing._game_key`, `sim.engine.simulate/marginal_impact`, and
  `core/{money,quantity}`. It adds **no** convention/pricing logic of its own.
- Prototype in a `tools/` backtest against the settlement-graded sweep first
  (the one that produced the $23.5M/$1.8M numbers), validate the game-keyed
  concentration and payout axes reproduce those figures, THEN port to
  `risk/book.py` with a cent-level parity check (hard rule 8).
- The one code-movement (promote `_game_key` to a public `pricing.grouping`
  symbol) is a pure rename/export, no logic change → parity is trivially the
  identity.

---

## 8. Summary

The current `ExposureBook` is a strong, dominance-correct **flat delta + premium
worst-case** engine, but it (G1) aggregates on the wrong key (event, not game),
(G2) has no payout/bankroll axis, (G3) never wires the MC engine for correlated
tails, (G4) shows only *net* leg delta (hiding one-directional bias), and (G5)
has no hierarchy, no per-leg P(hit), and no candidate-marginal query. The
enhancement re-roots aggregation on the existing `_game_key`, adds a second money
axis (payout obligation) cleanly separated from premium-at-risk, gives every
game a Tier-1 analytic comonotone worst case plus a Tier-2 MC tail using the
already-built `sim/engine.py`, adds gross same-side leg accumulation for the
directional-bias alarm, and attaches the pricer's `p_hit` to every leg and combo
— all updated incrementally in O(legs) per fill/quote, with the E2
mass-acceptance dominance bound preserved verbatim. Everything reuses existing
modules; the only structural code-movement is exporting one already-trusted
grouping function.

---

## NEXT STEPS

- **Owner: design → build.** Port §3 into `risk/book.py` after the `tools/`
  backtest reproduces the sweep's $23.5M/$1.8M split on the game axis
  (hard rule 8 parity check).
- **Owner: user (decisions owed).** (1) Confirm the two-axis framing — premium =
  loss cap, payout = utilization cap — is the intended capital model. (2) Pick the
  per-leg $-attribution rule (equal split default vs responsibility split, §3.3).
  (3) Set initial values for the four new caps in §5. (4) Approve promoting
  `_game_key` to a public `pricing.grouping` export.
- **Depends on R2/R3** (limits + acceptance wiring) to consume the new caps; this
  R1 file defines the data model those tasks build against.
