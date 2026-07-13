# External-review fact-check vs kalshi-combos-TWO source of truth

Reviewer file: `C:\Users\aahys\.claude\jobs\24844262\tmp\external_review.txt`
Method: read the review, then verified each claim against the actual code + docs
(not against the briefing). All paths absolute; line refs are current at HEAD
(suite 1325/0, branch `risk-foundation`, baseline 616db6a).

Verdict legend: CONFIRMED-GAP (real, unhandled) / ALREADY-HANDLED (we do it, cited)
/ PARTIAL (known/documented/designed but not implemented) / REVIEWER-WRONG (false).

---

## 1. SCALAR SETTLEMENT (reviewer's P0)

**Verdict: PARTIAL (reviewer is materially RIGHT that it is not implemented in
pricing or the risk ledger — but it is deeply KNOWN, documented, and an explicit
operator BUILD-NOTHING decision; and the reviewer is WRONG that "the engine
appears to assume 0/1 everywhere" — the simulator already supports scalar).**

Evidence, layer by layer:

- **KNOWN + documented, extensively.** `docs/dnp_scalar_settlement.md` is an
  8-section spec dedicated to exactly this. It states the generalized model the
  reviewer asks for verbatim: `V = min(1, ∏ v_i)`, `v_i ∈ [0,1]`, NO pays `1 − V`
  (§1, lines 52–65), derives that `E[ΔP&L]` is neutral at `s = p_i` (§3, lines
  99–104), and its §8 implementation spec has explicit `pricing/`, `sim/`, `risk/`
  subsections. The reviewer's "combo settles to the product of settlement values;
  a DNP/rain leg can settle 0.70" is the SAME thing this doc calls the scalar
  path, VERIFIED against live Kalshi market text (§7, lines 178–198) and widened
  for MLB's 48-hour rain rule (§7.1, lines 221–260).

- **PRICING: not implemented, by explicit decision.** `pricing/joint.py`
  computes `P(all legs settle on their selected side)` — a pure binary joint
  (`price_joint`, line 134; `cc_from_prob(joint.p)` in `pricing/quote.py:134`).
  There is no `E[∏ s_i]` term. The doc's own §8 pricing subsection (lines 264–276)
  and recommendation table (lines 309–315) say to add NOTHING to the mean fair,
  because `s ≈ p_i` makes DNP EV-neutral and the Kalshi leg marginal already
  prices the hazard — so the product of live marginals is ≈ unbiased for `E[V]`.
  So this is a documented, reasoned NON-implementation, not an oversight. The
  reviewer's "fair is technically E[product of s_i]" is correct in principle; our
  doc agrees and argues the mean move is sub-cent and deliberately skipped.

- **SIM: reviewer WRONG that MC has no scalar support — it already does.**
  `src/combomaker/sim/engine.py` lines 4–9, 41–70: each `Leg` carries an optional
  `settlement: tuple[(value, prob), ...]` discrete distribution over `[0,1]`, read
  off the inverse CDF of the copula uniform; `_position_pnl` (lines 171–182) does
  `payout_cc = min(∏ cols, 1.0) * $1` and `NO = ($1 − payout) − price`. This is
  the exact scalar payoff engine the reviewer says is missing. Caveat (honest, per
  the doc lines 303–306): the field EXISTS and is correct, but nothing POPULATES
  it yet — `pricing/joint.py` feeds a pure binary joint — so in practice DNP legs
  are simulated as binary today. Reviewer's "Monte Carlo paths need scalar payoff
  support" = ALREADY-BUILT capability, not-yet-wired data.

- **RISK LEDGER: reviewer RIGHT — binary only.** `src/combomaker/risk/balance.py`
  `apply_settlement` (lines 172–218) has exactly two branches: `settled_yes` True
  → debit premium; False → credit `$1 × contracts − premium` (line 191:
  `payout_cc = contracts * CC_PER_DOLLAR // 100`, i.e. a hard $1). There is no
  fractional-`V` path; `Settlement` (lines 97–107) carries only a `settled_yes:
  bool`. So a real `V = 0.70` settlement would be mis-booked (or, more precisely,
  would trip the reconciliation guard). This matches the doc's own §8 risk
  requirement (lines 285–294): "Reconciliation must expect a fractional
  settlement… a legitimate DNP paying 1−V must NOT trip
  HALT_RECONCILIATION_MISMATCH" — which is exactly what would happen today. The
  farm reconcile path (`rfq/lifecycle.py:397-427`) only has a binary settled_yes
  tripwire.

- **Current STANCE (explicit operator decision, dnp doc lines 317–336):**
  BUILD NOTHING / handle REACTIVELY. Rationale: DNP is ≈EV-neutral, tilts slightly
  toward the NO-seller, was ~0-in-4,913 rare for soccer, and its only failure mode
  is a FAIL-SAFE halt (`HALT_RECONCILIATION_MISMATCH` — stop, not loss). §7.1
  flags that MLB's rain rule lifts the trigger to ~1–2% of game-days and that the
  reactive stance needs operator re-affirmation before MLB combo quoting. Today
  the practical exposure is tiny: `legtypes.py` types only soccer `PLAYER_GOAL`;
  MLB/NBA props classify UNKNOWN → no-quote (doc lines 296–306).

Net: the reviewer's substantive point (risk ledger + pricing are binary) is TRUE
and matches our own docs; his framing that it is an unrecognized blind spot is
WRONG (it is the single most-documented settlement issue we have, with a signed
operator decision and a fail-safe halt as the backstop). Verdict = PARTIAL.

---

## 2. FEES — wired into quotes? into P&L? into the ledger? net-of-fees studies?

**Verdict: SPLIT — ALREADY-HANDLED in quote construction; CONFIRMED-GAP in the
BalanceTracker ledger (books no fee at fill/settlement); PARTIAL on the studies
(P&L sweep is NOT net of fees, by acknowledged design).**

- **Quote construction: ALREADY-HANDLED, and well.** `pricing/quote.py`
  `construct_quote` subtracts a per-side fee from each bid (lines 165–196):
  `yes_raw = fair − half − fee_yes − skew`. It computes the fee at the *worse* of
  fair and the nearest-to-$0.50 plausible fill price (`side_fee`, lines 165–185)
  precisely because the quadratic fee peaks at $0.50 — the reviewer's exact
  concern ("current maker fees ~0.44¢ at 50¢, material vs a 1¢ edge") is directly
  addressed. `pricing/fees.py` is the exact quadratic the reviewer describes:
  `ceil(coef·mult·C·P·(1−P))`, taker 0.07 / maker 0.0175, centicent ceil, fee
  multiplier, fail-safe TAKER attribution when `maker_is_taker_on_fill is None`
  (lines 84–89). Coefficients VERIFIED against the official Kalshi PDF (effective
  2026-06-29) — NOTES.md L9 (line 453). The reviewer's fee checklist (maker/taker,
  multiplier, centicent rounding, partial contracts) is all present.

- **Ledger books NO fee: CONFIRMED-GAP.** `risk/balance.py apply_settlement`
  (lines 189–205) computes `realized_cc = (payout if won else 0) − premium_paid`
  with NO fee term. `Settlement` (lines 97–107) carries no fee field. And at FILL,
  `rfq/lifecycle.py on_quote_executed` records `fee_cc=None` deliberately (line
  376: "reconciled from the exchange ledger (defense #3)"). NOTES.md F4 (line 263):
  "expected edge at fill = (side fair − our bid) × qty, fees reconciled later…
  fee_cc NULL until then." So the realized-P&L ledger the reviewer critiques does
  NOT net fees — his "the ledger cannot book only NO-win/YES-loss and must
  establish cost basis + realize net P&L" is a REAL gap for the realized ledger.
  Mitigation: on quadratic combo series the maker fee is $0 (verified Phase 2.5
  ground truth, NOTES.md line 33: `is_taker=false, fee_cost=0.000000`), so for our
  sell-only resting combo quotes the fill fee is genuinely zero today — which is
  why the gap has not bitten. But it is not GENERAL, and a taker-attributed fill
  (or a maker-fee-list change) would be un-booked. The design intent (defense #3)
  is that the reconciliation gate catches predicted-vs-actual to the cent — but
  that reconciliation-of-fees path is Phase 6 and not yet built (lifecycle
  `reconcile_combo_settlement` is a stub, lines 405–413 TODO).

- **P&L markup sweep NOT net of fees: PARTIAL (acknowledged).**
  `docs/reports/2026-07-12-pnl-markup-sweep.md` method (lines 8–18) grades
  `P&L = premium − payout` with no fee line. It is explicitly a "THERMOMETER on
  ONE window… the SHAPE is the signal," graded on settlement, majority-unresolved
  (73.7% WC-NORMAL / 87.3% MLB-FAT unresolved, lines 74–79), with the standing
  conclusion "the $ are NOT bankable." The reviewer's "historical profitability
  studies should be rerun net of exact fees" is a fair ask; our own report already
  disclaims the dollars as directional-only and defers the real markup decision to
  a pooled, multi-week, game-clustered re-grade (lines 113–133). So: the study is
  not net of fees (reviewer correct), but it was never presented as bankable and
  the fee omission is small relative to the disclaimed unresolved-selection bias.

---

## 3. RFQ SIZING / "slice whales" — circular dependency, full-size vs decline

**Verdict: mostly ALREADY-HANDLED — the reviewer's mechanics are RIGHT and we
already implement the conservative full-size solve he demands; his "the cap slices
whales" reading is REVIEWER-WRONG about our CODE (we decline, we don't slice) but
CORRECT that the BRIEFING's word "slice whales" is loose.**

- **Full-RFQ size, no maker size field: ALREADY-HANDLED.** `rfq/lifecycle.py`
  `_risk_qty` (lines 551–567) and its call-site comment (lines 155–158) encode
  exactly the reviewer's model: "a quote implicitly covers the RFQ's FULL size (no
  size field on the wire)." `OpenQuoteRisk` and the mass-acceptance bound risk-check
  the FULL size, never a self-chosen slice.

- **Circular dependency (target-cost): ALREADY-HANDLED, conservatively.** The
  reviewer's chain (quote price → contract count → risk → quote price) is real for
  target-cost RFQs. `_risk_qty` (lines 555–566): for a `target_cost_cc` RFQ it
  converts at `denom = max(100, min(bids))` — i.e. the CHEAPEST quoted side, which
  yields the MOST contracts — a strict upper bound on exposure. Comment (lines
  552–554): "Target-cost RFQs convert at the CHEAPEST quoted side (most contracts)
  — the conservative ceiling." This is the reviewer's "solve conservatively for
  the largest possible exposure," implemented. This was in fact the CRITICAL
  finding of the 2026-07-05 final adversarial review ("target-cost risk sizing")
  and was fixed with a regression test (CLAUDE.md "Final adversarial review: DONE
  … 1 critical: target-cost risk sizing"). Reviewer's "do not risk-check only the
  displayed target dollar amount" — we don't; we solve the price-dependent count.

- **Slice vs decline: REVIEWER-WRONG about the code.** The code has NO
  partial-quote path. If the conservative full-size exposure breaches a limit,
  `handle_rfq` (lines 166–177) records a skip and returns — a DECLINE, not a
  slice. `_risk_qty` returning None (unresolvable) also → no-quote (lines 159–163).
  The reviewer's own recommended behavior ("safely quote the entire amount or
  decline") IS what the code does. The word "slice whales" lives only in the
  briefing/cap-table prose (`docs/risk_engine_briefing.txt:176`); the reviewer is
  right to flag the WORD as misleading, wrong to infer the code slices. Note the
  per-combo max-payout cap (R2 design, `docs/research/R2_caps_killswitch.md:327`)
  is a DECLINE gate on the conservative full size — "cap" here = "don't quote
  combos whose full size exceeds X," not "quote a smaller piece."

---

## 4. get_balance — incomplete bankroll definition (balance vs portfolio_value)

**Verdict: PARTIAL — reviewer's technical point is RIGHT (we read only the
`balance` field, not portfolio_value/equity), but (a) it is not yet WIRED into any
cap, and (b) the R2 design explicitly reasons about equity/drawdown; so it is a
known, partially-designed gap, not an unrecognized error.**

- **What get_balance returns / how it is parsed.** `exchange/rest.py:120-121`:
  `get_balance` → `GET /portfolio/balance` → raw JsonDict. `risk/balance.py`
  `_parse_balance_cc` (lines 77–94) reads ONLY `balance_dollars` (exact string) or
  `balance` (int cents) — i.e. **available cash only**. It does NOT read
  `portfolio_value` / equity. So the reviewer's "if that literally means only the
  balance field, the risk denominator is wrong" is factually accurate about the
  parser.

- **Not yet consuming it anywhere.** Grep confirms `BalanceTracker` /
  `bankroll_cc` is referenced ONLY in `risk/balance.py` + its tests — it is NOT
  imported by `limits.py`, `lifecycle.py`, or `quote_app.py`. The
  2026-07-12-risk-foundation report says so directly (NEXT STEPS: "Owner: wiring —
  Poll BalanceTracker.refresh()… apply apply_settlement()"). Current live caps are
  hard dollar numbers (`limits.py:22-32`), not %-of-bankroll — so the wrong
  denominator cannot mis-scale a cap TODAY because no cap scales from it yet.

- **Design DOES contemplate equity/drawdown (partial credit).**
  `docs/research/R2_caps_killswitch.md:285-291` defines a peak-drawdown halt on
  `peak_equity_cc = bankroll + unrealized` and tracks equity, and line 513 lists
  "baseline bankroll" as an OPERATOR-SET number. The risk-foundation report's
  DESIGN DECISION block (lines 139–151) explicitly debates exchange-poll-
  authoritative bankroll vs a synthetic equity model and flags it for operator
  sign-off. So the "track cash, portfolio_value, equity, reserved separately"
  recommendation is partially designed (equity appears in the drawdown halt) but
  the parser today is cash-only and unwired. Reviewer's specific `B_risk =
  min(start_of_day, cash + haircut·portfolio_value)` formula is NOT implemented.

Net: CONFIRMED that the parser is cash-only; but "incomplete bankroll DEFINITION
driving wrong caps" cannot yet manifest (unwired), and the equity concept is
already in the R2 drawdown design. PARTIAL.

---

## 5. PER-GAME worst-case — comonotone SUM vs feasible-outcome MAX

**Verdict: the reviewer is CORRECT that today's number is a SUM, not a feasible-
outcome max — but for the LOSS (premium) axis that sum is the EXACT joint worst
case, not an overstatement; his overstatement critique applies only to a PAYOUT
axis we have not yet capped. We DO have the structural models he says to reuse,
and the two-tier (analytic-sum + MC) plan is already designed. Verdict: PARTIAL —
correct direction, but his "sum overstates" is REVIEWER-WRONG for the loss axis.**

- **It IS a sum.** `risk/exposure.py` snapshot (lines 258–260): `for game in
  games: game_worst[game] += position.max_loss_cc`. So `worst_case_loss_by_game_cc`
  = Σ per-combo premium over combos touching the game. No feasible-outcome
  enumeration.

- **But for the PREMIUM/LOSS axis, the sum is EXACT, not conservative.** A
  long-NO position's max loss is its premium, realized iff the parlay HITS (settles
  YES). Every NO position on a game losing its premium simultaneously requires
  every one of those combos to HIT — which is a single joint scenario (all legs
  hit). There is no infeasibility: the sum of premiums IS the true comonotone
  worst case for cost-at-risk. This is stated precisely in
  `docs/research/R1_book_model.md` G3 (line 83): "a plain sum of per-position
  premium, which for cost-at-risk is coincidentally the true joint worst case (all
  NO positions on a game lose their premium iff every combo hits)," and §3.4 Tier-1
  (lines 212–228): "the correct comonotone upper bound for the premium axis with no
  MC needed." The reviewer's counterexample ("Team A wins ∧ Over 3.5" vs "Team B
  wins ∧ Under 2.5" can't both hit) is a PAYOUT-side argument: on the loss axis a
  combo we hold NO on loses its premium when it hits, and mutually-exclusive combos
  simply don't all hit — so the premium sum is never realized above the feasible
  set for the loss it actually caps. His critique is right in general risk theory
  but MISAPPLIED to a premium-loss axis where max-loss is per-position and
  additive.

- **Where the reviewer IS right: the PAYOUT axis (not yet capped).** The new
  `payout_obligation_by_game_cc` axis (exposure.py:184-185, 260) is where a
  feasible-outcome max would matter — but R2 has NOT yet added a payout cap
  (limits.py:138-141 seam comment: "R2 will add a separate game-payout cap"). So
  there is no live payout cap for the sum to overstate yet.

- **Structural models DO exist to compute a feasible max.** The reviewer says
  "you already have structural scoreline and exact logical models — reuse them."
  We do: `pricing/dixon_coles.py` (soccer scorelines), `pricing/margin_total.py`
  (NFL/NBA/WNBA margin×total bivariate), `pricing/structural.py` (adapter),
  `pricing/relationships.py` (exact containment / mutual-exclusion / IMPOSSIBLE).
  R1 §3.4 Tier-2 (lines 230–252) explicitly plans to reuse them: wire the
  already-built `sim/engine.py` MC on the real book for VaR/ES per game.

- **Two-tier plan already designed (analytic-sum + MC).** R1 §3.4 lines 210–252:
  "Two tiers, cheap→exact. Tier 1 — analytic comonotone bound (hot path, always
  available)… Tier 2 — MC-graded expected & tail loss (slow full-book refresh)"
  wiring `sim/engine.py`'s `simulate`/`marginal_impact`. R2 §4.5 (lines 452–470)
  and gap #10 (lines ~5-list) note the MC engine is built but "NOT in the
  enforcement loop." So the reviewer's recommendation (analytic bound fast, MC/
  structural feasible-max for the tail) is our EXISTING design, just not wired.

Net: number is a sum (true); sum is exact for the loss axis it caps (reviewer's
overstatement claim wrong there); feasible-max matters on the payout axis, which
isn't capped yet; structural models + two-tier MC plan already exist and are
designed to supply the feasible tail. PARTIAL.

---

## 6. RESERVATION / CONFIRM ATOMICITY — reserved-before-confirm? race? unknown fill?

**Verdict: SPLIT. Reserve-before-confirm ORDER: reviewer's rule is FOLLOWED for
the state/position booking (booked at confirm, before execute) — ALREADY-HANDLED.
Confirm-timeout unknown state: ALREADY-HANDLED (state parked before the call, halt
on repeated failures). Single-writer atomic-reservation vs concurrent-headroom
race: PARTIAL — no explicit lock/reservation service exists; safe today ONLY
because it is single-event-loop asyncio, which the reviewer's "single-writer
actor" recommendation is not yet formalized against.**

- **Position booked at CONFIRM, not at fill: ALREADY-HANDLED.**
  `rfq/lifecycle.py on_quote_accepted` (lines 288–304): on a confirm decision it
  sets `state.pending_fill` and stores the state BEFORE the network call (line
  292 comment: "Park state BEFORE the network call: if the confirm times out
  client-side it may still have landed server-side"), then on a successful confirm
  immediately calls `_book_position` (line 304 comment: "Once confirmed neither
  party can withdraw: the position is REAL now — book it immediately, not at
  quote_executed"). `_book_position` (lines 327–345) adds the OpenPosition to the
  exposure book. So exposure is committed at confirm, not deferred to the ~1s-later
  execute — exactly the reviewer's "risk capacity must be reserved before/at
  confirm, not after receiving the fill."

- **Confirm-timeout unknown state: ALREADY-HANDLED (mostly).** The except branch
  (lines 305–313) counts `confirm_failures` and halts
  (`HALT_CONFIRM_TIMEOUTS`) after 3 consecutive failures. Because `pending_fill` +
  `_executed_states[quote_id]` are set BEFORE the await, a later `quote_executed`
  message finds the state and books idempotently (`on_quote_executed` lines
  347–357 → `_book_position` is idempotent via the `position_id in positions`
  guard, lines 331–333). NOTES.md line 35 documents the real late-confirm
  behavior (400 `expired`, quote stays `accepted`). Residual: on a timeout we halt
  but do NOT actively re-query the quote/fill state to positively resolve the
  unknown (the reviewer's "query the quote/fill state and treat exposure as
  committed until disproved"). We halt-and-stop rather than reconcile-then-resume —
  fail-safe, but not the full reconciliation the reviewer describes. The Phase-6
  exchange-first reconciliation is designed (R2 gap, startup reconcile) but not
  built.

- **Single-writer / concurrent-headroom race: PARTIAL — no explicit guard.**
  There is NO `asyncio.Lock`, no versioned reservation, no single-writer actor
  around `ExposureBook` mutation. `handle_rfq` and `on_quote_accepted` both read
  the book, check limits, then mutate — a classic check-then-act. Today this is
  race-free ONLY because everything runs on ONE asyncio event loop with sequential
  `await` dispatch: `rfq/intake.py:141-146` (`_fan_out_rfq`) and `:112-119`
  (`_make_quote_handler`) `await` each handler in a plain `for` loop, and
  `quote_app.py:208-217` routes both RFQ and accept events through that single
  serialized path — no `create_task` fan-out per message. So two accepts cannot
  interleave mid-check on the same book. BUT: (a) this invariant is IMPLICIT (an
  emergent property of the dispatch shape), not enforced by a reservation
  primitive; the reviewer's exact ask — "place risk ownership behind a single-
  writer actor… two concurrent RFQs must never both read the same remaining
  headroom" — has no code guard, so any future `create_task`-per-message change
  (or a second consumer) silently reintroduces the race; (b) the risk-check reads
  a mass-acceptance snapshot that already assumes ALL resting quotes fill, so even
  concurrent accepts are bounded by that conservative envelope at quote-issue time.
  R2 §2.5 (lines 225–275) designs the missing active fill-velocity governor (a
  committed-payout budget decremented on ACCEPTANCE, at `lifecycle.py:288`) —
  which is the reservation-on-accept the reviewer wants — but it is NOT yet built.

Net: order + timeout handling are handled and fail-safe; the atomic single-writer
reservation the reviewer names as a P0 is only implicitly satisfied by the single-
loop architecture and has no explicit guard/primitive. PARTIAL (leaning
CONFIRMED-GAP on the "no reservation primitive" specifics).

---

## 7. CROSS-GAME combos — one bucket / double-count / hyperedge?

**Verdict: ALREADY-HANDLED as a controlled DOUBLE-COUNT (a multi-game combo adds
its FULL max_loss/payout to EVERY game it touches) — which is the conservative,
fail-safe choice for a per-game cap; NOT forced into one bucket, NOT arbitrarily
split. The reviewer's ideal (a true hyperedge with marginal allocation for
reporting + portfolio scenario for gating) is a documented FUTURE refinement, not
a current bug.**

- **How it is bucketed today.** `risk/exposure.py` snapshot (lines 255–260):
  `games = {game_key(leg.event_ticker) for leg in position.legs if leg.event_ticker}`
  then `for game in games: game_worst[game] += position.max_loss_cc;
  game_payout[game] += position.payout_obligation_cc`. So a combo with legs from
  two games contributes its FULL premium and FULL payout to BOTH game buckets — a
  deliberate double-count, mirrored on the mass-acceptance path (lines 281–285).
  `game_key` (`pricing/grouping.py:21-38`) fail-closes: a hyphen-less event keys on
  the whole string so an ungamed leg never merges.

- **This is the RIGHT conservative choice for per-game gating.** For a per-game
  CAP (the operator's stated risk unit), adding the whole combo to each game it
  touches OVER-states each game's concentration — which is fail-safe for a cap
  (you decline earlier, never later). It is neither "forced into one game bucket"
  (the reviewer's first failure mode — we don't; we add to all) nor "arbitrarily
  divided among games" (his second failure mode — we don't split; we replicate the
  full amount). The reviewer explicitly says "do not force it into a single game
  bucket or divide its loss arbitrarily among games" — we do neither.

- **True hyperedge / marginal allocation: documented as future, not built.**
  `docs/research/R1_book_model.md` G5 (line 85) flags the absence of a
  game→leg→combo tree with marginal per-node contributions, and §3 designs a
  hierarchical book (game/leg/combo nodes) with marginal-impact queries via
  `sim/engine.py:240` (`marginal_impact`). The MC engine can already compute a
  whole-book portfolio scenario loss that naturally handles a cross-game combo as
  a function of multiple game factors (sim draws all legs jointly) — the reviewer's
  "for actual risk gating, use portfolio scenario loss" is exactly Tier-2, built
  but unwired. So the hyperedge representation is a known, designed refinement; the
  current double-count is a correct conservative gate, not a misgrouping.

Net: reviewer's two failure modes (single-bucket / arbitrary-split) do NOT occur;
we conservatively double-count, which is fail-safe for per-game caps; the richer
hyperedge/marginal model is designed (R1 §3, G5) and MC-backed but not yet wired.
ALREADY-HANDLED (conservatively), with the richer model PARTIAL/designed.

---

## Cross-cutting note

Several reviewer P0/P1s (fill-velocity governor, per-combo/per-game PAYOUT caps,
bankroll-scaled caps, challenger model, external watchdog, slate/theme caps, MC in
the loop) map 1:1 onto ALREADY-DESIGNED items in
`docs/research/R1_book_model.md` + `R2_caps_killswitch.md` that are explicitly
"seam exists, not yet wired." The reviewer was reviewing the BRIEFING
(`docs/risk_engine_briefing.txt`), which describes the target architecture; the
CODE at HEAD implements the B1/B2/BalanceTracker FOUNDATION (2026-07-12) and
leaves R2's caps + wiring as the declared next build. That is why so many findings
land PARTIAL rather than CONFIRMED-GAP: the gap is real in the CODE but recognized
and designed in the DOCS.
