# Combo YES/NO sides — semantics, quote direction, and the fade-defense config

**Date:** 2026-07-08 · **Status:** DONE (Kalshi API/docs + repo demo ledger) ·
**Trigger:** operator: "on Kalshi I can only build YES combos… is the NO side
'advance-NO ∧ scorer-NO'? people can only take NO of a combo, so we should quote
only YES bids." · **Method:** two agents, live read-only Kalshi API + docs.kalshi.com
+ the FIX docs + this repo's real demo ground-truth fixture. No third-party sources.

## TL;DR (three verified answers)

1. **NO side = the COMPLEMENT of the whole combo**, not per-leg negation. NO of
   `advance-YES ∧ scorer-YES` pays $1 iff *at least one leg fails*. "advance-NO ∧
   scorer-NO" is a **different combo** (own ticker/book) that pays $1 iff *both* fail.
2. **To be a parlay SELLER (long NO, +EV), quote `no_bid` and set `yes_bid=0`** —
   the operator's "quote only YES bids" is **BACKWARDS** (that makes us the long-YES
   parlay buyer = the −14¢/ct toxic side).
3. **We can 100% guarantee single-sided exposure** — declining a bid (`"0"`) +
   `rest_remainder=false` closes every path (RFQ, PreferBetterQuote, resting book,
   taker cash-out). Nothing can force the unwanted side onto us.
4. **But our code doesn't do this yet:** `construct_quote` emits BOTH bids today.

---

## 1. NO-side semantics — complement, NOT per-leg negation (decisive)

```
YES(combo) = (leg1=chosen side) AND (leg2=chosen side) …   settle = PRODUCT of leg values
NO(combo)  = market-level COMPLEMENT = $1 iff NOT(all legs hit) = 1 − YES   (same ticker, other side)
NO(combo)  ≠  "leg1=NO AND leg2=NO"   ← that is a DIFFERENT combo, different ticker/book
```

| # | Claim | Verdict | Evidence (Kalshi) |
|---|-------|---------|-------------------|
| 1 | Combo YES = AND of chosen leg sides; scalars multiplied | VERIFIED | live `functional_description`: *"only resolve to YES if every associated market resolves to YES. Scalar outcomes are multiplied (rounded down)."* |
| 2 | Combo is ONE binary $1 market; YES/NO are two sides of the SAME ticker | VERIFIED | every combo `GET /markets/{t}`: `market_type:"binary"`, `notional 1.0000`, `yes_bid/ask + no_bid/ask`, `yes_sub_title` + `no_sub_title` |
| 3 | **NO is the complement, not per-leg negation** | VERIFIED (decisive) | on EVERY live combo `no_sub_title` is **byte-identical** to `yes_sub_title` — the exchange stores only the YES conjunction; NO has no negated-leg text, it is just "this conjunction does not occur" |
| 4 | NO pays $1 iff conjunction fails; single scalar settlement | VERIFIED | finalized combo (BTC-no ∧ ETH-yes ∧ SOL-no): `result:no`, `settlement_value_dollars:0.0000` → YES holder $0, NO holder $1 |
| 5 | An all-NO-legs combo settles YES iff every leg hits its chosen NO side | VERIFIED | finalized `KXMVECROSSCATEGORY-…` (4 legs all `side:no`): `result:yes`, `settlement 1.0000` — YES = "all chosen sides hit," regardless of yes/no |
| 6 | Flipping ONE leg's side ⇒ a DIFFERENT ticker/book | VERIFIED | two live combos over the same event, one leg `side:no` vs `side:yes`, have different `mve_selected_legs` → different `ticker` → different book |

**So:** "advance-NO ∧ goalscorer-NO" is created as its own combo (each leg `side:no`),
own ticker, own book — economically **unrelated** to the NO side of the advance-YES
∧ scorer-YES combo (former: $1 iff *both* fail; latter: $1 iff *at least one* fails).

## 2. Quote direction — which bid to zero (VERIFIED three ways)

`yes_bid`/`no_bid` are the maker's **bids = prices to BUY** that side, so whichever
bid the requester accepts, **WE buy that side and end up LONG it.**

```
 requester       we transact   WE end   requester   tape          our role
 accepts...      (we BUY)      up LONG  ends LONG   taker_side
 ─────────────────────────────────────────────────────────────────────────
 "yes"           yes_bid       YES      NO          "no"       PARLAY BUYER  ← TOXIC (−14¢/ct)
 "no"            no_bid        NO       YES         "yes"      PARLAY SELLER ← +EV, what we want
```

Verified: (a) **docs** — `create-quote` field defs (bid = price to buy) + FIX
AcceptQuote tag 54: *"BUY accepts the maker's NO quote and SELL accepts the maker's
YES quote."*; (b) **our own demo ledger** — `tests/fixtures/ground_truth/
scenario_accept_{yes,no}.jsonl` (real Kalshi demo round-trips 2026-07-06):
accept-yes → maker fill `outcome_side=yes, position +1.00` (LONG YES); accept-no →
maker LONG NO; promoted in `conventions.json` (`maker_side_on_yes_accept="yes"`,
`maker_side_on_no_accept="no"`, `maker_pays_own_bid=true`); (c) **live tape** — both
`taker_side` values occur (187 yes / 13 no in a 200-trade sample).

- The requester does **not** pick a side up front (`create_rfq` has no side field);
  side is chosen only at accept. So takers take **both** sides — premise "takers can
  only take NO" is FALSE.
- The good retail flow (parlay buyers) is served via our `no_bid` and prints as
  `taker_side="yes"` — the tape reading that misled us.
- **Config for a pure parlay-seller book: `yes_bid="0"`, `no_bid = 1 − (our ask) > 0`.**

## 3. Single-sided exposure is GUARANTEED (no forced-fade path)

Declining a bid (`"0"`) + `rest_remainder=false` ⇒ we can never take the unwanted side:

| Path | Can it force the unwanted side? | Evidence |
|------|-------------------------------|----------|
| RFQ response | No — responding is voluntary; `"0"` bid declines that side (both-zero invalid) | `rfqs` guide + FIX + `rest.py:222` |
| FIX `PreferBetterQuote` | No — only selects among quotes **you already submitted**; can't invent one on a declined side | FIX tag 21022 |
| Resting order (`rest_remainder`) | No — a rested bid is same-side only (buys MORE of the wanted side); `rest_remainder=false` rests nothing | `create-quote` doc |
| Taker cash-out / early exit | No — reaches us only via resting liquidity, already closed above | (see cashout report) |

**Residual toward the unwanted side: NONE.** The only residual is benign (more of
the *wanted* side, and only if we opt into `rest_remainder=true`).

## 4. Our code today — we are NOT yet protected

- **`construct_quote` (`src/combomaker/pricing/quote.py:168-169`) emits BOTH bids**
  (`yes_raw = fair − half − fee`; `no_raw = (1−fair) − half − fee`). So on a normal
  combo a requester can accept YES and stick us **long YES = the fade**. The
  two-sided market-maker default is currently exposed to the −14¢/ct flow.
- `construct_farm_quote` already hard-sets `yes_bid=0` (one-sided) — the pattern to
  reuse exists; it's just scoped to impossible-combo farming today.
- To implement the fade defense: a config (e.g. `sell_parlays_only`) that forces
  `yes_bid=0` in `construct_quote`, property-tested so a non-zero YES bid can never
  be emitted when set.

## Caveats (honest)

- The `accepted_side → long-side` mapping rests on docs + our **2026-07-06** demo
  fixture; a **fresh demo round-trip** re-confirms it live (Phase 2.5 owes this).
  The single-sided *guarantee* (§3) does not depend on the direction — declining a
  bid removes that side either way.
- No live combo collections were open at some query moments; the "does retail ever
  only BUY combos" sub-question is UNVERIFIED (doesn't affect any conclusion here).
- `taker_side` is DEPRECATED → migrate the recorder to `taker_outcome_side` /
  `taker_book_side` (matched 1:1 live).

## NEXT STEPS

- **Decision owed by operator:** go **pure parlay-seller** (`yes_bid=0` on all
  combo quotes) vs **two-sided with a wide/skewed YES**? Settlement says long-YES
  is structurally adverse (it's the exit/fade flow), so declining costs ~nothing —
  but revisit after the Jul 11 settlement doubles the sample. NB the RFQ-routing/
  standing effect of one-sided quoting is still UNVERIFIED.
- **Owner (eng):** if we go single-sided, add `sell_parlays_only` → `yes_bid=0` in
  `construct_quote` + property test; set `rest_remainder=false` on combo quotes;
  migrate recorder off `taker_side`.
- **Owner (eng, needs demo creds):** one fresh combo demo round-trip to re-confirm
  `accepted_side → long-side` before it hardens further in `conventions.json`.
- Relates to [[project_kalshi_combos_two]], the 2026-07-08 settlement report, and
  the cashout-exposure report.
