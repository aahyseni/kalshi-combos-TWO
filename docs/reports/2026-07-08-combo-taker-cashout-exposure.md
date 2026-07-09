# Can a combo taker cash out against us? — verified: NO

**Date:** 2026-07-08 · **Status:** DONE (research, source-of-truth verified) ·
**Trigger:** operator: "Kalshi has a way to let the taker cash out if 2/5 legs
hit — I do NOT want to allow that. Is that the NO-side/fade defense?" · **Method:**
Kalshi Help Center + docs.kalshi.com + our `docs/api-notes/`, concrete citations.

## Answer

**The taker cash-out that could expose us does not exist.** Two separate things
were conflated; both resolve in our favor:

1. **"NO-side/fade defense"** = *our* quoting tactic, not a taker action. It's how
   we price the NO side to avoid the sharp fade flow (see settlement report:
   taker-NO beat makers −14.12¢/ct). Levers: decline NO (`no_bid=0`), widen NO,
   or cap NO size. Unrelated to cash-out.
2. **Taker cash-out / partial-leg early settlement** = a Kalshi *product* question.
   Verified below: it does not exist in any form that reaches our fill.

## Verified findings (with citations)

| # | Claim | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | **Partial-leg cash-out ("2/5 legs hit → exit")** | **VERIFIED: does NOT exist** | Kalshi Help "Combos": *"A combo settles after all of its underlying positions have been determined"*; payout = *product of each position's value*. No leg-by-leg settlement event exists (`api-notes/asyncapi-ws.md:251-258`). |
| 2 | **A taker's early exit unwinds our short-YES** | **VERIFIED: NO** | *"All trades are final"* (Help "Combos"); after maker confirm *"neither party can withdraw"* (`api-notes/multivariate.md:223`). An exit is a NEW secondary trade vs whoever is on the book then — it never calls back our executed fill. |
| 3 | **Maker opt-in "allow cash-out" flag** | **VERIFIED: none** | `CreateQuote` fields are only `rfq_id, yes_bid, no_bid, rest_remainder, post_only, subaccount` (`api-notes/rfq-flow.md:114-124`). Not a feature we grant/withhold. |
| 4 | **`can_close_early` = a cash-out** | **VERIFIED: NO** | It's an exchange lifecycle field — market may close to trading early if the outcome is known early (`api-notes/index-scan.md:152`); symmetric to both sides, no maker obligation. |
| 5 | **Combo has its own order book** | **VERIFIED: yes** | Help "Combos": *"Each combo is a unique market with its own dedicated order book"*; RFQ orders post to the public book after the execution timeout (`api-notes/rfq-flow.md:39-40,223,232`). |
| 6 | **A taker can practically sell a combo back early** | **UNVERIFIED** | Secondary blogs (alphascope, actionnetwork) claim "sell anytime at market"; Help Center does not confirm and stresses finality. Combos are RFQ/quote-driven ⇒ resting bid liquidity to sell into is likely thin. Mechanism (sell into the combo book) exists; liquidity does not. |

## Independent blind re-verification (2nd agent, Kalshi-only, live-API proof)

A second agent re-ran this **blind** (no priming, forbidden from reading these
notes, `docs.kalshi.com` + live API only) and reached the same **NO**, with a
live receipt:

- **Real finalized combo** `KXMVENBASINGLEGAME-S2026071383B5409-F5F8F6C628B`
  (2-leg: spread-NO ∧ total-over-YES): `status:finalized`, `result:no`,
  `settlement_value_dollars:0.0000`, `notional_value_dollars:1.0000`, single
  `settlement_ts`. **The YES holder got $0 — no partial credit for the leg that
  did hit.** All-or-nothing, one settlement event.
- Live `functional_description`: *"The resulting market will only resolve to YES
  if every associated market resolves to YES. Scalar outcomes are multiplied
  (rounded down)."* Combo = one `binary` $1-notional market.
- MVE lifecycle events (docs): `created, activated, deactivated,
  close_date_updated, determined, settled, price_level_structure_updated,
  metadata_updated` — **no `partial_settled`/`leg_settled` event exists.**

**New operational nuance — early NO-determination (not a partial payout):** because
a combo is an AND, it can `determine`/`finalize` the instant **any single leg
resolves NO**, before the other legs are decided — paying the NO side the full $1
and YES side $0. This is *full, final* settlement that can simply fire early. →
**Implication for our code:** exposure/markout release should key off the
**combo's own** `determined`/`finalized` (via `mve_selected_legs`), which may fire
before the last leg settles — not a risk, a timing fact to encode.

## The one residual exposure path (and how we already close it)

We are touched by a taker's exit **only if we are the counterparty on that combo's
book** — i.e., we left a resting order. That is voluntary and controllable:

- `rest_remainder=false` + not answering reverse-direction RFQs ⇒ **nothing of
  ours rests on the combo book** ⇒ an exiting taker has nothing of ours to hit.
- **CAUTION:** `rest_remainder=true` leaves a resting order on the public combo
  book after execution (`api-notes/rfq-flow.md:232`). On any combo we are already
  short YES, keep resting liquidity **off**, or it becomes an unintended re-entry.

So the operator's "do NOT let taker cash-out expose us" is **already the default**
— it only breaks if we opt in by resting liquidity.

## Fade-defense lever confirmed real

Declining the NO side is `no_bid=0` (yes-only quote); `both bids = 0` is invalid
(`src/combomaker/exchange/rest.py:217`, `create_quote`). So the fade defense —
quote YES to win parlay-buyer flow, decline or widen NO to dodge the sharps — is
directly supported by the API and our client.

## NEXT STEPS

- **Owner (kalshi-combos-TWO eng):** on demo, confirm `rest_remainder=false` +
  `post_only` leaves **no** resting order on the combo book post-execution
  (closes the one residual path); and check empirically whether a second demo
  account can even find a bid to sell a combo into (resolves finding #6).
- **Decision owed by operator:** none to stay safe — default is safe. A decision
  only arises if we later *want* to provide two-sided/exit liquidity on combos.
- Relates to the open NOTES.md settlement unknowns (`functional_description`
  product payout, per-leg `yes_settlement_value` → combined `settlement_value`) —
  those govern FINAL settlement math, not early exit.
