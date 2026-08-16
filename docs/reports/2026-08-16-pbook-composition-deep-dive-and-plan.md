# 2026-08-16 — The deep dive: P(book), utilization, weird fills, losing nights — one diagnosis, one plan

Operator brief (upset, quoted): "another negative day… filling weird combos…
never normal or popular combos… not filling up to 70%… P(book) < 0.60–0.70 is
really bad… deep dive fills/pricing/risk… price better, risk better, or
manage numbers?" Six measurement agents + adversarial verifier + synthesis
(~1.0M tokens, 344 tool calls) over the full store + 12 post-restart logs +
counterfactual MC through the live sampler. Everything below is measured.

## First: the week's P&L was NOT what the ledger said

Group-corrected daily realized (the ledger's LOSS double-book defect
overstated the week by **~$669 and flipped two days' signs**):

| day | corrected | booked | note |
|---|---|---|---|
| 8/9 | +$1,090.34 | — | best day (anchor cross-checks +$1,092) |
| 8/10 | +$6.30 | | |
| 8/11 | −$963.51 | | the one-cluster night (was +EV entries) |
| 8/12 | +$311.63 | | |
| 8/13 | −$215.32 | | |
| 8/14 | −$5.59 | −$85.52 | sign-noise |
| **8/15** | **+$109.79** | **−$55.88** | **"another negative day" was POSITIVE** |
| week | **+$198.16** | −$470.47 | the defect feeds the losing-nights feel |

Real pain is still real: −$892 cumulative since 8/10, 4 of 7 days negative.
The tie×under pickoff batch settled net only **−$13.05** (5 of 8 WON;
ORLCIN hit for −$53.30) — the guard capped it.

## The diagnosis (hypothesis half-confirmed, mechanism corrected)

**One disease, two self-inflicted amplifiers.**

**DISEASE — composition:** 53.6% of premium is coin-flips (NO entry
35–65¢). A book that is half coin-flips by premium mathematically pins
P(book) at ~0.45 no matter how much edge or how many sports. Measured:
p_book flat in book size (0.41→0.39 across det buckets) and flat in
position count — deploying MORE of the same mix cannot raise it. Losing
nights at p_book 0.40–0.49 are the **expected mode** of this composition,
even at +EV entries.

BUT the mechanism was not "we lose high-band auctions": the ≥65¢ band is
our largest-growing premium share (44.7% since 8/15) and **ex-whale it
earns +5.6%**. Two corrected sub-mechanisms:

1. **We are structurally ABSENT from the exchange's #1 flow** — cross-game
   ML-only parlays = **33.0% of the entire RFQ tape** (2.35M requests) — we
   quote only 4–6% of the class and have filled **0 of 529** quoted.
   Two-stage suppression: (stage 1) risk walls refuse most of it before
   pricing — `skip_entity_loss_cap` 644k instances, with entity
   accumulation counting up to 200 RESTING quotes at 100% weight (live:
   KXMLBGAME:DET $222.56 "accumulated" > $135.91 cap — saturated by
   quotes, not positions) + per_combo all-or-nothing excluding the 5.9%
   of RFQs carrying 73.8% of requested $; (stage 2) where we DO quote,
   we lose by exactly our markup — field clears our-fair +0.05–0.25¢ on
   1,122 matched auctions, we're +1.7–3.5¢ over. This flow is the
   capital-heavy, P(win)-rich composition the operator wants.
2. **The high band bleeds through ONE account**: whale `0f9b27cb3e`
   (29% of all premium) is selectively sharp only in ≥65¢ props —
   premium-weighted win 54.3% vs 75.4% implied, **−$800.11 (−29% ROI)**
   in that one cell — while their game-market flow pays us. Plus two
   farmers: `7c885d57` (30/30 identical same-player RBI×TB, 8× the same
   Alvarez combo in hours) and `c1789477` (KS-parlays; −54% ROI history;
   also took the NEW tie×BTTS variant the 8/15 guard doesn't cover).

**AMPLIFIER 1 — the cash gate never armed (my 8/15 bug):** Kalshi nests
create-quote 400 bodies under `error`; the parse read `code=""` so
`cash_gate_armed` fired **0 times against ~478k insufficient_balance
400s** — entire peak hours (13–15h, 17–18h, 00–01h ET) at ZERO quotes,
8 confirm-fail reneges, 2 confirm-timeout halts. **FIXED `c4b30d7`**
(envelope unwrap + tape-shape test pins).

**AMPLIFIER 2 — the halt flap-loop:** 7 `halt_marginal_jump` halts
01:44–06:22 ET on ONE oscillating Skenes K-market marginal, each running
cancel_all and destroying the entire resting book. Also the marginal KILL
gate (armed 8/14) is measured as the real deployment ceiling — median det
at decline $1,123 = 35% of the $3.17k wall; 127 won-auction declines vs
118 fills; its 2% night-tail budget and the 0.70 ceiling are mutually
unsatisfiable on the current composition.

## The P(book) truth (counterfactual MC through the live sampler)

- p_book = P(open book settles net positive) — excludes realized P&L,
  resets as winners settle. Medians all week: 0.40–0.49.
- **0.60–0.70 at $3.4k deployed requires ≥60% of premium in NO ≥65¢
  entries (85–95¢ centre) with retained edge ≥ +1.5¢**: all-high book =
  0.676; realistic 60/25/15 tilt = 0.616; today's mix at the same $3,400 =
  0.446. No other lever reaches it.
- **The left tail INVERTS — the headline**: at identical $3,400, the
  high-entry book has P(12%-equity losing night) ≈ **0.000** and CVaR99
  **$386** vs today's mix **0.13–0.20** and **$1,245–1,361**. Higher
  P(book) AND a ~3× thinner tail — because a NO-buyer's premium is his max
  loss: coin-flip premium burns half the time; longshot premium burns only
  on rare parlay hits. (Fragility: if the tilt only captures fair, P(book)
  lands 0.52–0.55 — still strictly better on every metric.)
- **Diversification is a TAIL instrument, not a mean instrument**:
  composition+edge moves P(book) +23pp; adding sports moves it +0–1pp but
  cuts CVaR99 34–45%. No number of leagues fixes a mean problem.

## The plan (price better AND risk better AND manage numbers — in $ order)

**SHIPPED ALREADY (defect class):** cash-gate envelope fix `c4b30d7`
(recovers ~3 blanked peak hours/day ≈ +$400–500/day premium inflow at
measured pace; stops the renege/halt storms).

**DECIDE + SHIP TONIGHT (operator):**
1. **Halt-flap debounce/quarantine** — stop cancel_all on one flapping
   marginal (quarantine the single market instead). Recipe ready; touches
   halt machinery so it gets the freeze nod.
2. **Marginal KILL gate → telemetry** (config revert to the ledger's
   pre-8/14 "NOT YET" state) pending the 9/1 composition-aware repair —
   recovers ~+$700 standing det (→ ~65–70% of ceiling). The drawdown brake
   (which fired correctly 8/15 23:05) and every concentration cap stay
   armed. Honest cost: the 2% night-tail returns to telemetry (as it was
   through 8/12). NOTE: the counterfactual shows high-entry books carry
   P(kill)≈0 — the gate as-built fights exactly the composition we want.
3. **Group-corrected P&L in all reporting** (reporting lane only) — stops
   the ~$669/week sign-flipping defect from driving decisions.

**RATIFICATION PACKAGE (one package — they only work together):**
4. **+1¢ flat tier for cross-game ML-ONLY parlays fair<35¢ (MLB+soccer)** —
   the popular-flow entry ticket (we lose by exactly markup; field clears
   +0.05–0.25¢ over our fair). Keep FULL tiers everywhere model risk lives
   (props/SGP/rungs — the field itself clears +2.2¢ there and that's the
   whale's cell; a blanket longshot cut would feed him).
5. **Entity correlation-scoping + the approved-2026-07-17 40%
   resting-reservation haircut** — so pricing acts on more than 4–6% of
   the class (entity walls saturated by resting quotes are stage-1).
6. **Post-rebate edge floor** — the skew rebate currently manufactures
   sub-1¢ coin-flip fills (24% of premium at 0.84% edge); floor retained
   edge after rebate at ~half the tier.
7. **tie×BTTS guard extension** (same family as the shipped tie×total
   guard; live exhibit taken 8/16 05:58 by sharp taker c1789477).
Expected jointly: inflow $137/h → ~$295/h (the 2.15× gap holding $3.3k
needs), composition → 60/25/15, endpoint P(book) 0.62–0.70 with
P(kill) ≤ 0.04, EV ~+$120–163/night, worst-1% night −$315 to −$750 (vs
−$1,104 to −$1,221 today).

**MEASUREMENT FIRST (no-refit rule):** slugger-stratified RBI×TB
conditionals (league-pooled 0.4325 cell is a ~5¢ outlier vs 13–18 rivals);
RFI leg-fair staleness (13/25 wins vs 69% implied); the owed 8/11
entry-EV forensics (now adjudicates the standing-book EV drift that caps
realized P(book)); counterparty same-shape repeat-decay design (tonight's
c1789477 settles adjudicate); +0.5¢ razor step only after 1–2 weeks of
+1¢ capture data.

**NO-SHIPS:** blanket longshot-ladder cut (feeds the whale); pbook-axis
pricing arm (7/27 ruling stands; per-ticket P(book) proven noisy — the
KILL gate declined a P(book)-IMPROVING ticket); slate 65→80 (not the
binding wall); touching the 0.70 backstop (never bound post-restart);
static counterparty blocklists (defense must be shape/size/repeat-derived).

## NEXT STEPS

- **Operator (3 decisions):** (a) halt-flap quarantine + KILL-gate-to-
  telemetry tonight? (b) the ratification package this week? (c) friendlies
  allowlist still open from 8/15.
- **Me on go:** ship tonight's items + one restart (arms the fixed cash
  gate); build the package behind full gates incl. validate-caps-can-quote
  against the ML-parlay class; measurement set this week.
- **Me regardless:** watch c1789477/7c885d57 settles tonight; the 8/11
  entry-EV forensics.
