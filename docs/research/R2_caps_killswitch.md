# R2 — Caps / Limits / Kill-Switch System, designed to the pristine bar

**Scope.** Enhancement design for the exposure/limits/kill-switch layer of the
sell-only parlay maker. Grounded in the shipped code; every proposal cites the
file:line it extends or the gap it closes. No greenfield rewrite — the existing
`LimitChecker.check()` / `ExposureBook.snapshot()` / `KillSwitch` are the right
shapes; the design *adds the missing caps, fixes the quantity the event cap
measures, and adds a fill-time throttle the current design structurally lacks.*

Money is int centi-cents (`core/money.py`: `$1 = 10_000 cc`). Contracts are int
centi-contracts (`core/quantity.py`: `1 contract = 100 cc-contracts`). All
thresholds below are stated as **% of bankroll** so they scale; the two numbers
that CANNOT be a clean % (fill-velocity, hard-trip) are flagged operator-set.

---

## 0. The single structural bug the whole design turns on

The P&L sweep's core lesson — *"the true risk unit is the GAME, and selling
parlays ties up huge potential PAYOUT for thin premium ($23.5M max payout for
$1.8M premium)"* — is **not measurable by the current limits**, because every
aggregate cap measures the wrong quantity.

`OpenPosition.max_loss_cc` (`risk/exposure.py:57-59`) is:

```
return int(self.contracts) * int(self.entry_price_cc) // 100   # price we PAID
```

For a **YES-side** position that is correct — we paid the bid, we can lose the
bid. But we are **sell-only, long NO** (`pricing/quote.py:16-20`,
`combo_no_pays_complement` promoted true). When a parlay we sold **HITS**
(settles YES), a long-NO position pays out **$1 × contracts** and we keep only
the premium — the loss is `(CC_PER_DOLLAR − entry_price) × contracts`, i.e.
**~$0.85–0.92 per contract**, not the ~$0.08–0.15 premium. `max_loss_cc` is off
by ~6–11× on exactly the side we always hold.

Consequently:

- `worst_case_loss_by_event_cc` (`exposure.py:161,174-176,191-192`) sums this
  understated `max_loss_cc`, so `max_event_worst_case_loss_dollars`
  (`limits.py:138-146`) binds ~6–11× too loose for a NO book. The $1M single-combo
  tail the sweep found is *invisible* to it.
- `gross_notional_cc` (`exposure.py:167`) has the same understatement.

**Fix (the spine of R2):** introduce a signed, side-aware
`committed_payout_cc` on `OpenPosition` and a matching mass-acceptance term, and
re-point every cluster/tail cap at **committed payout**, not premium paid. This
is prototyped in a test module first, parity-checked to the cent, then ported
(CLAUDE.md hard rule 8). Definition:

```
def committed_payout_cc(self) -> int:
    # What we OWE if this position's payout side hits: $1/contract for the
    # side we are SHORT. Long NO (sell parlay) => we owe $1 if it settles YES.
    # Long YES => we owe nothing beyond price paid (already in max_loss_cc).
    if self.our_side is Side.NO:
        return int(self.contracts) * CC_PER_DOLLAR // 100
    return self.max_loss_cc
```

Net worst-case loss on a NO position = `committed_payout_cc − premium_collected`.
Caps below use committed payout (gross, conservative) for cluster/tail ceilings
and net for the drawdown/daily-loss halts.

---

## 1. Bankroll: the scaling anchor (must exist before % caps mean anything)

There is **no bankroll concept in the risk path today.** `rest.get_balance()`
exists (`exchange/rest.py:120-121`) but is never read by limits;
`RiskConfig`/`RiskLimits` are hard dollar numbers (`config.py:1632-1642`,
`limits.py:22-31`). Every "% of bankroll" cap requires an authoritative
bankroll figure.

**Mechanism.** Add `bankroll_cc` to the limit context, sourced fail-closed:

- On startup and every status tick (`quote_app._status_loop`, currently 15 s),
  poll `get_balance()` → `bankroll_cc`. This is the same loop that already
  cancels-all on exchange-inactive, so it is the natural home.
- Bankroll for cap purposes = **available balance + committed premium**, i.e. the
  capital actually at risk in the book, not just free cash. Operator sets the
  *baseline* bankroll in config (`risk.bankroll_dollars`); the live balance poll
  is a **floor** — if live balance drops below baseline (drawdown), caps tighten
  automatically; if it rises, caps do NOT loosen faster than an operator-set
  ratchet (prevents a transient balance spike from unlocking exposure).
- **Fail-closed:** if the balance poll fails or returns UNKNOWN, freeze
  `bankroll_cc` at the last good value and, after `bankroll_stale_s`, treat it as
  a `HALT_STALE_BANKROLL` condition (a book whose size we can't measure can't be
  risk-checked). This mirrors the existing "missing data ⇒ no-quote" rule
  (CLAUDE.md hard rule 6) and the UNKNOWN-marginal breach (`limits.py:105-111`).

**Proposed baseline:** `bankroll_dollars = 5_000` (matches the documented
$5k start, memory `project_kalshi_combos_capital_constraints`). **OPERATOR-SET** —
this is the one number everything else scales from.

---

## 2. The cap hierarchy

Ordered from the local/cheap check (gate a single quote) to the global/expensive
check (portfolio + kill). Each is stated as **mechanism → threshold → reasoning →
when-checked**. The "when-checked" column distinguishes **quote-time gate**
(pre-`CreateQuote`, `lifecycle.handle_rfq:166-177`) from **confirm re-check**
(last-look, `lifecycle._last_look_inputs:635-640` → `lastlook.decide_confirm`).

### 2.1 Absolute $ exposure cap (portfolio ceiling)

- **Mechanism.** Hard ceiling on **total committed payout across the whole book**
  (Σ `committed_payout_cc` over all positions + mass-acceptance quote term). This
  is the "you may owe at most $X to the world if everything hits" number. Extends
  `gross_notional_cc` (`exposure.py:167`) but on committed payout, and made an
  *absolute* backstop that does not scale, sitting ABOVE the % caps.
- **Threshold.** `max_total_committed_payout_dollars` — **OPERATOR-SET absolute**,
  proposed = **4× bankroll** ($20k at $5k). Reasoning: a sell-parlay book is
  structurally payout-heavy (premium $1.8M ↔ payout $23.5M ⇒ ~13× in the sweep,
  *with no caps*). 4× bankroll is the deliberate design choice that we will
  tolerate at most 4× leverage of committed payout — well inside the sweep's
  uncapped 13× but enough to run a real book. Flagged operator-set because "how
  levered do we run" is a risk-appetite decision, not a derivable number.
- **Fail-closed.** Any UNKNOWN in the decomposition (`snapshot.unknown_marginals`,
  `limits.py:105-111`) already breaches; keep that. If `bankroll_cc` is stale the
  absolute number still binds (it doesn't need bankroll), which is why an
  *absolute* backstop exists alongside the % caps.
- **When-checked.** Quote-time gate (mass-acceptance term) AND confirm re-check
  (candidate position) AND maintenance-tick (`lifecycle.maintenance_tick:459`).

### 2.2 %-of-game (correlated-cluster) cap — the headline cap

- **Mechanism.** The sweep's central finding: *combos share legs and GAMES, so
  the true risk unit is the game/match, not the ticker.* Today the closest cap is
  `max_event_delta_contracts` (`limits.py:121-129`) and
  `max_event_worst_case_loss_dollars` (`limits.py:138-146`), both keyed on
  `event_ticker` and both measuring the wrong quantity (delta / premium). Replace
  the worst-case-loss-by-event with **worst-case committed payout per GAME**,
  where "game" is the correlation cluster.
  - **Cluster key.** `event_ticker` is the first-order proxy and is already
    threaded through (`LegRef.event_ticker`, `exposure.py:36`; aggregated at
    `exposure.py:174-176`). BUT a single game can surface under sibling event
    tickers (period markets, team-total markets, alt-lines) and cross-game themes
    hide under distinct events. So: cluster key = a **GameId** resolved from the
    leg ticker's game-code segment (the same `KXMLBGAME-26JUL101915BOSNYM` blob
    `rfq/pregame.py:64` already parses for start times; soccer has an analogous
    game code). Fail-closed: a leg whose GameId can't be resolved falls back to
    `event_ticker`, and if that's also missing it counts as its OWN singleton
    cluster AND raises UNKNOWN (never merged-away, never ignored).
  - The cap is on **worst-case committed payout for the cluster** =
    Σ `committed_payout_cc` over all positions+quote-terms touching that game,
    because within one game leg outcomes are maximally correlated (a blowout can
    settle every "over/favorite/star-scorer" leg of that game YES at once) —
    summing payout (not netting deltas) is the correct conservative bound and
    matches the mass-acceptance philosophy already in `exposure.py:12-17`.
- **Threshold.** `max_game_committed_payout_pct = 10%` of bankroll ($500 at
  $5k). Reasoning: with a 10%/game cap, it takes **≥10 simultaneously-losing
  independent games** to threaten the bankroll; games are the near-independent
  unit (`cross_event_rho = 0.0`, `config.py:143`), so 10 bad games at once is a
  deep-tail event, while any *single* game — the $1M-swing combo the sweep found —
  is structurally capped at 10% no matter how many combos pile onto it. Scales
  with bankroll automatically. **This is the cap that kills the concentration
  the sweep flagged.**
- **Composition with pricing (§4).** As a game approaches its cap, the engine
  should *widen* that game's quotes before the cap hard-declines them (skew), so
  the cap is approached gracefully, not as a cliff.
- **When-checked.** Quote-time gate + confirm re-check + maintenance tick. This
  is the most important cap to re-check at confirm, because between quote and
  accept OTHER quotes on the same game may have filled (mass-acceptance is a
  bound, not a guarantee of which ones actually land).

### 2.3 Per-combo / per-position max-payout cap (kill the $1M single-combo tail)

- **Mechanism.** A single combo carried ~$1M payout swings in the sweep. Cap the
  **committed payout of any one position** (and any one open quote's worst side).
  This is a NEW cap — today `max_notional_per_quote_dollars` (`limits.py:83-91`)
  checks `position.max_loss_cc` (premium), which for a NO position is the ~$0.10
  we PAID, so a position that could owe $1M passes a $500 "notional" check. The
  new cap checks `committed_payout_cc`.
- **Threshold.** `max_position_committed_payout_pct = 2%` of bankroll ($100 at
  $5k ⇒ ~110 contracts of a $0.90-payout NO leg). Reasoning: no single combo
  should be able to move the book more than 2% — that's the granularity below the
  10%/game cap (so a game can hold ~5 distinct max-size combos before the game cap
  binds), and it forces the whale RFQs that dominate volume (68 combos = 50% of
  contracts) to be *sliced*, not taken whole. This directly caps the tail unit the
  sweep named.
- **Interaction with size sources.** `_risk_qty` (`lifecycle.py:551-567`) already
  converts target-cost RFQs at the **cheapest quoted side** (the review-fixed
  conservative ceiling, `NOTES.md:479`). Feed THAT `risk_qty` into the per-combo
  payout cap so target-cost whales are capped on the max contracts they could
  buy, not a nominal.
- **Fail-closed.** If `risk_qty is None` (unresolvable) the RFQ already no-quotes
  (`lifecycle.py:159-163`); keep.
- **When-checked.** Quote-time gate (per candidate position, the loop already at
  `limits.py:73-91`) + confirm re-check.

### 2.4 One-directional-bias / correlated-theme limit

- **Mechanism.** Net exposure to a single leg-outcome or a correlated THEME
  across many games (e.g. "all overs", "all home favorites", "star-scorer
  YES"). Today `max_market_delta_contracts` (`limits.py:112-120`) caps net delta
  per *market ticker* and `max_event_delta_contracts` per *event* — but nothing
  caps a theme that spans DISTINCT games. A book that has sold 200 different
  "over 2.5" parlays across 200 games is 200× long "unders" as a maker, and if
  scoring is leaguewide-high that night they correlate. Add a **theme bucket**:
  aggregate signed delta by `(leg_family, side)` where `leg_family` comes from
  the existing `legtypes.pair_key` machinery (`config.py:147` references
  `legtypes.pair_key`; the family is `moneyline`/`total`/`btts`/`player_goal`/…).
- **Threshold.** `max_theme_net_delta_pct = 15%` of bankroll (in
  contracts-equivalent, i.e. delta × $1 vs bankroll). Reasoning: cross-game theme
  correlation is real but far below same-game (`cross_event_rho = 0.0` is the
  *unconditional* prior, but a common shock — weather, a rule regime, a
  leaguewide scoring night — induces a positive conditional tail the copula
  doesn't see). 15% is deliberately looser than the 10%/game cap (themes are
  weaker-correlated than same-game) but tight enough that no single directional
  thesis can sink the book. **Flag: 15% is a judgment number** — it should be
  re-derived once shadow/prod data lets us measure realized cross-game theme
  correlation on losing nights (pre-registered, multi-week, per memory
  `feedback_no_refit_on_pnl`). Ship at 15%, mark for measurement.
- **When-checked.** Quote-time gate + maintenance tick. Does NOT need a confirm
  re-check as aggressively as the game cap (theme drift is slower).

### 2.5 Fill-time committed-payout budget + fill-velocity limit (the mass-acceptance worst case, made real)

This is the cap the current architecture **structurally lacks**, and it's the
one the sweep's failure mode (3) — *"quotes rest, many accept at once,
one-sided buildup"* — most needs.

- **What exists.** Mass-acceptance today is a **quote-time dominance bound**:
  `snapshot(mass_acceptance=True)` (`exposure.py:184-214`) assumes *every* open
  quote fills NOW on its worst side, and `limits.check` refuses to issue a NEW
  quote if that hypothetical book breaches. That's correct and conservative for
  *deciding whether to add a quote* — but it does nothing once quotes are RESTING.
  If 20 quotes are live and within limits under the bound, and then 15 of them get
  accepted in the same 2 seconds, we book 15 real fills with **no rate check and
  no cancel-all trigger** — the bound said "if all 20 hit we're fine" but "fine"
  can still mean "committed 15× more payout in 2 s than we'd choose to."
- **Mechanism (new — a fill-rate governor):**
  1. **Committed-payout budget, decremented on ACCEPTANCE not quote-time.**
     Maintain a rolling `committed_payout_window_cc` = Σ committed payout of
     fills booked in the last `fill_window_s`. Increment it in
     `on_quote_accepted` at the moment we decide to confirm (`lifecycle.py:288`,
     right where `pending_fill` is set), BEFORE the confirm round-trip.
  2. **Velocity limit.** If, within `fill_window_s`, either
     (a) `committed_payout_window_cc` exceeds `fill_budget_pct` of bankroll, or
     (b) the *count* of confirmed fills exceeds `max_fills_per_window`, then
     **decline further confirms** (last-look returns a new
     `DECLINE_FILL_VELOCITY`) and **cancel-all resting quotes**
     (`lifecycle.cancel_all`, already wired to halts at `quote_app.py:224-228`).
     This converts the resting-quote mass-acceptance risk from a static bound into
     an active circuit breaker: the first burst is absorbed, the tap is then shut.
  3. **Ceiling → halt.** If the budget is exceeded by a hard multiple
     (`fill_budget_ceiling_pct`), escalate from decline-and-cancel to a full
     `KillSwitch.halt(HALT_FILL_VELOCITY)` — the on_halt callback already
     cancels-all and stops intake (`quote_app.py:224-228`).
- **Thresholds.**
  - `fill_window_s = 2.0` — matches the HVM confirm window scale (117 ms measured
    round-trip, `NOTES.md:83`; a 2 s window catches a coordinated burst).
  - `fill_budget_pct = 5%` of bankroll of committed payout per 2 s window
    (soft: decline + cancel-all). Reasoning: half the per-game cap — a single
    burst shouldn't be able to commit more than a game's worth of payout before we
    pause and reassess.
  - `fill_budget_ceiling_pct = 10%` per window (hard: halt). One game-cap of
    payout committed in 2 s is a "something is wrong" signal, not normal flow.
  - `max_fills_per_window` — **OPERATOR-SET** (proposed 8). This one can't be a
    clean % of bankroll: it's a *rate* tied to how fast we can risk-check and how
    much genuine simultaneous flow we expect. Start conservative (8/2 s) and raise
    with observed benign burst rates from shadow mode.
- **Fail-closed.** If bankroll is stale, the count limit (`max_fills_per_window`)
  still binds even though the $ budget can't be evaluated — a rate cap that
  doesn't need bankroll.
- **When-checked.** CONFIRM-time (this is fundamentally a fill-rate check, it
  lives at acceptance) + a maintenance-tick sweep that expires the window and,
  if still over budget, keeps quotes cancelled.

### 2.6 Drawdown / daily-loss halt + hard-trip kill

- **What exists.** `HALT_DAILY_LOSS` at `limits.py:148-155` trips when
  `−daily_pnl.total_cc ≥ max_daily_loss_dollars` (realized + unrealized,
  marked at leg mids by `_refresh_daily_pnl`, `lifecycle.py:429-453`). It is
  wired to the kill switch in `maintenance_tick` (`lifecycle.py:458-463`), which
  cancels-all and stops. Good — but it's **loss-from-zero-today only**; there is
  no peak-to-trough drawdown halt and no hard-trip distinct level.
- **Mechanism (three graduated levels):**
  1. **Soft daily-loss halt (keep, re-express as %).** Trip at
     `daily_loss_halt_pct` of bankroll on realized+unrealized. Same wiring
     (`limits.py:148-155` → `HALT_DAILY_LOSS`), threshold now scales.
  2. **Peak-drawdown halt (new).** Track intraday `peak_equity_cc` (bankroll +
     unrealized, updated each maintenance tick). Trip
     `HALT_DRAWDOWN` when `(peak_equity − current_equity) / peak_equity ≥
     drawdown_halt_pct`. Catches the case where we're up big then give it back —
     invisible to a from-zero daily-loss cap. Needs the same fail-closed
     unrealized mark that `_refresh_daily_pnl` already guards
     (`lifecycle.py:443-450`: any unmarkable position ⇒ keep last mark; a stale
     mark must not silently reset the peak — freeze peak on stale, don't lower it).
  3. **Hard-trip kill (new, distinct from soft halt).** A soft halt cancels-all
     and stops *quoting* but the process, the reconnect logic, and any auto-clear
     tooling remain. A hard trip is the "do not resume without a human" level:
     `HALT_HARD_TRIP` at `hard_trip_pct`, which additionally **writes the KILL
     file** (`killswitch._kill_file`, watched at `killswitch.py:95-101`) so the
     halt survives a process restart — a restarted process re-reads KILL and
     halts immediately, so an auto-restarter can't un-trip it. Clearing requires
     `KillSwitch.clear(actor)` (`killswitch.py:74-79`, already human-only) AND
     removing the KILL file. This is the fail-safe against a bug that keeps
     restarting and re-arming.
- **Thresholds (% of bankroll):**
  - `daily_loss_halt_pct = 4%` ($200 at $5k). Reasoning: current $500 = 10% of a
    $5k bankroll is generous for a maker whose per-trade edge is cents; 4% is a
    normal "bad day, stand down" level that still absorbs ordinary variance on a
    thin-premium book.
  - `drawdown_halt_pct = 6%` from intraday peak. Slightly above the daily-loss
    level so it catches give-backs that start from a profitable peak without
    double-firing with the daily-loss halt on a straight-down day.
  - `hard_trip_pct = 8%` ($400 at $5k). **This one should be operator-confirmed**
    even though it's a %: it's the "kill and require a human" line and its
    consequences (KILL file, manual clear) mean the operator must own the number.
    Ship at 8% as the default, flag for sign-off.
- **When-checked.** Maintenance tick (all three), exactly where
  `HALT_DAILY_LOSS` already lives (`lifecycle.py:457-463`).

---

## 3. Cap table (summary)

| Cap | Mechanism | Proposed threshold | When checked |
|---|---|---|---|
| **Bankroll anchor** | `get_balance()` poll → `bankroll_cc`; live balance is a floor, ratcheted up; stale ⇒ freeze then `HALT_STALE_BANKROLL` | baseline **$5,000** (OPERATOR-SET absolute) | status-loop poll (15 s) + maintenance tick |
| **Absolute $ exposure** | Σ committed payout (book + mass-acceptance term) ≤ ceiling; absolute backstop above the % caps | **4× bankroll = $20k** (OPERATOR-SET) | quote-gate + confirm + maintenance |
| **%-of-game (cluster)** | Worst-case committed payout per GameId cluster (game-code blob → `event_ticker` → singleton+UNKNOWN, fail-closed) | **10% of bankroll** ($500) | quote-gate + **confirm** + maintenance |
| **Per-combo max payout** | Committed payout of any one position / open-quote worst side (uses `_risk_qty` conservative size) | **2% of bankroll** ($100) | quote-gate + confirm |
| **One-directional / theme** | Net signed delta by `(leg_family, side)` across games | **15% of bankroll** (measure-then-tighten) | quote-gate + maintenance |
| **Fill-time payout budget** | Rolling Σ committed payout of fills in `fill_window_s`, decremented on ACCEPTANCE; soft = decline+cancel-all | **5% / 2 s** (soft) | **confirm** + maintenance |
| **Fill-velocity ceiling** | Same window, hard multiple ⇒ `KillSwitch.halt` | **10% / 2 s** (hard) | confirm + maintenance |
| **Fill count / velocity** | Count of confirmed fills per window | **8 / 2 s** (OPERATOR-SET rate) | confirm + maintenance |
| **Daily-loss halt (soft)** | realized+unrealized loss vs bankroll ⇒ `HALT_DAILY_LOSS` + cancel-all | **4% of bankroll** ($200) | maintenance tick |
| **Peak-drawdown halt** | (peak−current)/peak equity ⇒ `HALT_DRAWDOWN` + cancel-all | **6% from intraday peak** | maintenance tick |
| **Hard-trip kill** | writes KILL file + halt; survives restart; human-only clear | **8% of bankroll** ($400) (OPERATOR-CONFIRM) | maintenance tick |
| *(existing, keep)* max_open_quotes | count of resting quotes | 20 (`limits.py:29`) | quote-gate |
| *(existing, keep)* UNKNOWN-marginal breach | any uncomputable delta ⇒ breach | fail-closed (`limits.py:105-111`) | every check |

Absolute-$ numbers in the table are the values at the **$5k baseline**; the
percentages are the load-bearing definitions that scale.

---

## 4. Cross-cutting behavior (the pristine-bar requirements)

### 4.1 Check order — quote-time gate vs confirm re-check

The existing two-point enforcement (`limits.check` at both
`lifecycle.handle_rfq:166` and `lifecycle._last_look_inputs:635`) is the right
skeleton. Ordering, cheap→expensive, fail-closed:

```
QUOTE-TIME GATE (pre-CreateQuote, in-memory only, target <1ms):
  1. kill-switch halted?            -> SKIP_HALTED           (filters.py:56)
  2. bankroll known & fresh?        -> else no-quote (fail-closed)
  3. per-combo payout (candidate)   -> local, cheapest to reject a whale
  4. open-quote count               -> limits.py:94
  5. absolute $ exposure (mass-acc) -> snapshot(mass_acceptance=True)
  6. %-of-game cluster (mass-acc)   -> the headline cluster cap
  7. theme / directional            -> aggregate delta bucket
  8. UNKNOWN-marginal anywhere      -> breach (limits.py:105-111)

CONFIRM RE-CHECK (last-look, on real accept, warm state, <1ms):
  everything in decide_confirm's severity order (lastlook.py:54-98) FIRST
  (kill > exchange > WS > in-play > started > velocity > stale > moved),
  THEN re-run limits.check on the post-fill book with the ACCEPTED side as
  the candidate, PLUS:
  9. fill-velocity / fill-budget window  (NEW — the burst governor)
 10. per-game cap on the ACTUAL post-fill book (not the bound)
```

Rationale for splitting: the quote-time gate reasons about a *hypothetical*
worst-case book (mass-acceptance bound) to decide whether it's safe to REST a
new quote; the confirm re-check reasons about the *actual* book that results
from *this specific accept*, because between quote and accept the world moved
(other fills, leg drift, a game starting — already handled by the pregame
re-check at `lifecycle.py:641-643`). The per-game cap and fill-velocity MUST be
at confirm because that's where real commitment happens.

### 4.2 Fail-closed everywhere (this is the whole point)

- UNKNOWN marginal ⇒ breach (kept, `limits.py:105-111`).
- Unresolvable size ⇒ no-quote (kept, `lifecycle.py:159-163`).
- Stale/unknown bankroll ⇒ %-caps can't be evaluated ⇒ the absolute backstop and
  count-based caps still bind; after `bankroll_stale_s` ⇒ `HALT_STALE_BANKROLL`.
- Unresolvable GameId ⇒ leg is its OWN cluster **and** raises UNKNOWN (never
  merged into another game's headroom, never silently dropped).
- Any None input to last-look already fails closed (E4, `NOTES.md:244`;
  `lastlook.py` returns decline on every `None`).
- A failing cap-eval exception must **breach**, not pass — wrap the new cluster/
  theme/velocity evals so an exception returns a synthetic breach (the existing
  `check()` doesn't try/except; add a top-level guard that turns any raise into a
  `SKIP_RISK_HEADROOM`-style breach rather than propagating and skipping the
  check). *This is a gap to close: today an exception inside `snapshot()` would
  propagate up and could be swallowed by the caller's error handling — a cap that
  throws must be treated as tripped.*

### 4.3 Composition with pricing — near-cap quotes WIDEN or DECLINE (never silently pass)

The engine already has an **inventory-skew** seam: `construct_quote` takes
`inventory_skew_cc` (`pricing/quote.py:128,195-196`) and the header notes
*"Inventory skew arrives from the risk engine (0 until Phase 4 wires it)"*
(`pricing/quote.py:27`). **This is the wiring point and it is currently zero.**
Proposed behavior as any cap's headroom shrinks:

- Compute per-quote **headroom fraction** `h` = min over the caps this quote
  touches of `(cap − current_committed) / cap` (game cap, per-combo, theme,
  absolute). `h = 1` far from caps, `h → 0` at the cap.
- Map `h` to `inventory_skew_cc`: at high headroom, skew = 0 (normal quote); as
  `h` drops below a band (say 30%), inject positive skew (we're long the joint
  event as a NO seller, so skew *lowers our NO bid* → we demand more premium →
  attract less flow into the crowded game/theme). At `h ≤ 0` the cap hard-declines
  (existing breach path). Result: **graceful widening into a decline**, not a
  cliff — a near-cap game gets progressively worse prices until nobody lifts it,
  which self-limits before the hard cap ever bites.
- Skew is directional and already sign-correct in the code
  (`pricing/quote.py:195-196`: positive skew bids less for YES, more for NO — for
  a sell-only book the NO-bid reduction is what we want). Cap the skew magnitude
  so it "is a tilt, not an override" (the archetype-multiplier precedent,
  `pricing/quote.py:148-150`).

### 4.4 Avoiding caps silently killing ALL flow (the deadlock failure mode)

The danger: a mis-set cluster key or a bankroll glitch trips a cap that then
declines *everything*, and because declines are just logged skips
(`lifecycle._record_skip`), the operator sees silence, not an alarm. Defenses:

- **Every cap breach is a reason-coded, metered event** (already true —
  `_record_skip` logs reasons, `metrics.inc("rfq.skipped")`). ADD a per-cap
  metric so `skip_mass_acceptance_breach` etc. are individually counted; a
  dashboard/alarm on "one cap is responsible for >X% of skips over N minutes"
  surfaces a stuck cap. This matches memory `feedback_enumerate_buckets`
  (never trust a residual bucket — decompose skips by cap).
- **"All-flow-blocked" watchdog.** A maintenance-tick check: if the book has
  issued **zero quotes** for `flow_starvation_s` while RFQs are arriving and the
  kill switch is NOT halted, emit a loud `flow_starved` warning (not a halt —
  starvation is safe, but silent starvation is a bug). Distinguishes "market is
  quiet" (no RFQs) from "we're declining everything" (RFQs in, quotes out = 0).
- **Caps that trip should widen BEFORE they block (per §4.3)** — the skew ramp
  means a cap manifests first as *fewer fills* (worse prices), giving the
  operator a gradient to see, not a binary all-or-nothing.
- **Distinct decline reasons per cap** so the skip stream is diagnosable:
  reuse `SKIP_MASS_ACCEPTANCE_BREACH` / `SKIP_RISK_HEADROOM` and add
  `SKIP_GAME_CAP`, `SKIP_THEME_CAP`, `SKIP_POSITION_PAYOUT_CAP`,
  `DECLINE_FILL_VELOCITY` to `core/reasons.py` — one code per cap, so
  `enumerate-buckets` decomposition is exact.

### 4.5 The MC engine must actually be in the loop (sweep failure mode 4)

The sweep ran with **neither the risk engine nor the MC engine**. `sim/engine.py`
(`simulate`, `marginal_impact`, `leg_deltas`) computes VaR/ES and the exact
correlated-book tail — it must gate the caps, not sit unused. Proposed wiring
(consistent with E1, `NOTES.md:241`: analytic independence deltas for the hot
path, MC for the slow refresh):

- **Hot path (quote-gate + confirm):** analytic bounds as today
  (`analytic_leg_deltas`, `exposure.py:93-118`) + the committed-payout sums. Cheap,
  <1 ms, conservative.
- **Slow full-book refresh (maintenance tick, or a dedicated slower loop):** run
  `sim.simulate` on the real book to get `var_cc[0.99]`/`es_cc[0.99]`
  (`sim/engine.py:225-237`) and add a **book-level 99% ES cap** as a % of
  bankroll (proposed 8%, i.e. "99% of days lose less than 8%"). The analytic caps
  are the fast per-decision gate; the MC ES cap is the truth-teller that catches
  cases where the analytic independence assumption *understates* correlated tail
  (the sweep's whole point: correlated payouts). Use `marginal_impact`
  (`sim/engine.py:240-258`) on the biggest resting quotes to see which one, if
  accepted, most worsens 99% ES — that's the quote to cancel first when the ES cap
  is approached. This closes sweep failure mode (4) directly.

---

## 5. Top gaps in the current code (ranked)

1. **`max_loss_cc` measures premium paid, not committed payout, for the NO side
   we always hold** (`exposure.py:57-59`). Every cluster/tail/gross cap
   consequently binds ~6–11× too loose for a sell-parlay book. **The single
   most important fix — every other cap depends on it.**
2. **No per-COMBO payout cap** — the $1M single-combo tail is uncapped; the
   nearest check (`max_notional_per_quote_dollars`, `limits.py:83-91`) reads the
   understated premium. (2.3)
3. **No fill-time / fill-velocity governor** — mass-acceptance is a static
   quote-time bound (`exposure.py:184-214`) with nothing throttling the RATE of
   real fills or cancelling-all on a burst. A coordinated multi-accept commits
   unbounded payout inside the confirm window. (2.5)
4. **No bankroll in the risk path** — `get_balance()` exists (`rest.py:120`) but
   is never wired; every cap is a hard dollar number (`config.py:1632-1642`) that
   doesn't scale and can't drive a drawdown ratchet. (1)
5. **Event caps use `event_ticker`, which is neither the true GAME cluster nor
   payout-weighted** — sibling event tickers and cross-game themes escape;
   `worst_case_loss_by_event` sums premium. (2.2, 2.4)
6. **Only from-zero daily-loss halt; no peak-drawdown halt and no
   restart-surviving hard-trip** distinct from the soft halt. (2.6)
7. **`inventory_skew_cc` is wired but hard-zero** (`pricing/quote.py:27,128`) —
   the near-cap-widen behavior has a seam but no driver; caps are cliffs. (4.3)
8. **A cap that raises an exception isn't guaranteed to fail closed** —
   `limits.check` / `snapshot` have no top-level try/except turning a raise into a
   breach. (4.2)
9. **No per-cap skip metering or all-flow-blocked watchdog** — a stuck cap
   declines silently; skips are aggregated, not decomposed per cap. (4.4)
10. **MC engine (`sim/engine.py`) is not in the enforcement loop** — VaR/ES/
    marginal-impact exist but no cap consumes them, so the correlated tail the
    caps exist to contain is never actually measured on the live book. (4.5)

---

## NEXT STEPS

- **Owner: engineering (port, CLAUDE.md rule-8 discipline).** Prototype
  `committed_payout_cc` + the game-cluster/theme/velocity caps in a test module,
  parity-check to the cent against the live `exposure.py`/`limits.py`, THEN port.
  Gap #1 first — nothing else is trustworthy until the NO-side payout quantity is
  right.
- **Owner: operator (decisions owed).** Sign off the numbers flagged
  OPERATOR-SET / OPERATOR-CONFIRM: baseline bankroll ($5k), absolute exposure
  multiple (4×), fill count/velocity rate (8/2 s), and the hard-trip % (8%). The
  %-of-bankroll caps (game 10%, per-combo 2%, theme 15%, daily-loss 4%, drawdown
  6%) ship as defaults but the operator owns risk appetite.
- **Owner: measurement (pre-registered, multi-week, per `feedback_no_refit_on_pnl`).**
  The theme cap (15%) and the MC ES cap (8%) are the two "measure-then-tighten"
  numbers — set from shadow/prod data on realized cross-game correlation and
  losing-night tails, NEVER refit on a P&L window.
- **Owner: engineering.** Wire `sim/engine.py` into a slow full-book refresh loop
  (99% ES cap + `marginal_impact` cancel-worst) and the `inventory_skew_cc`
  headroom-ramp — both are seams that exist and are currently no-ops.
