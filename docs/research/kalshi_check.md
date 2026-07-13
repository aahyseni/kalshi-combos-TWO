# TASK B — Kalshi-mechanics fact-check of the external reviewer

Fact-checked against the SOURCE OF TRUTH: docs.kalshi.com + Kalshi Help Center
(fetched live 2026-07-12), our verified `docs/api-notes/*`, `NOTES.md` ground
truth (real demo RFQ round trips), and the demo-settlement report. Repo is
READ-ONLY; no edits made.

Bottom line: **all six reviewer Kalshi-mechanics claims are CONFIRMED.** One
(fees) is confirmed with an important REFINEMENT the reviewer got backwards for
our own fills, and one (scalar settlement) is confirmed AND already documented on
our side but only reactively handled in code.

| # | Reviewer claim | Verdict |
|---|----------------|---------|
| 1 | Combo settles to PRODUCT of leg settlement values; a leg can settle scalar (0.70) | **CONFIRMED** |
| 2 | GET balance returns `balance` (cash) + `portfolio_value` (positions) | **CONFIRMED** |
| 3 | Maker can't size a quote; every quote is full RFQ amount; fixed-contracts OR target-cost | **CONFIRMED** |
| 4 | Maker fee ≈ 0.44¢/contract at 50¢ | **CONFIRMED as a number, but REFUTED as applied to US** — our RFQ maker fill is **$0** |
| 5 | 3s confirm window, 1s execution timer | **CONFIRMED** |
| 6 | combo_no_pays_complement (NO = 1 − product) holds under scalar settlement | **CONFIRMED** |

---

## Claim 1 — SCALAR SETTLEMENT (combo = product of leg values; a leg can settle to a scalar)

**Reviewer:** "a combo settles to the PRODUCT of the settlement values of its
underlying positions; a leg can settle to a scalar (e.g. 0.70) via DNP/void/
cancellation."

### VERDICT: **CONFIRMED** — this is Kalshi's documented rule, on multiple authorities.

**Product rule (verbatim, Kalshi Help Center "Combos", fetched 2026-07-12,
https://help.kalshi.com/en/articles/13823820-combos):**
> "The combo payout equals the product of the value of each individual position."
> "If all positions settle at $1.00, the combo pays $1.00 per contract."
> "If any position settles at $0, the entire combo pays $0."

**Scalar leg (verbatim, same source):** a position can settle "to a value between
$0 and $1 rather than a simple yes/no outcome" — flagged in the UI by a "blue
arrow indicator." This matches our own doc-verified statement of the mechanism.

**Our api-notes already document this** — the reviewer is not telling us anything
the codebase's own docs don't already assert:
- `docs/api-notes/multivariate.md:168`: "`settlement_value` is a STRING and can be
  non-binary (scalar) … Combo markets can therefore settle at intermediate
  values, not just 0/100." Also `:222` (RFQ leg `yes_settlement_value_dollars`
  nullable = "the documented hook for scalar/product settlement") and `:225`
  ("Settlement fees are zero for simple yes/no determinations but may apply for
  sub-cent scalar settlement").
- `docs/api-notes/money-fees.md:192` — GetMarket `market_type` enum is
  `binary | scalar`; `:209` `result` enum is `'yes' | 'no' | scalar | ''`.
- `docs/dnp_scalar_settlement.md` is a whole spec dedicated to this: §1 gives
  `V = min(1, ∏ᵢ vᵢ)`, §7 quotes the VERIFIED Kalshi market rule for soccer
  scorers ("If a player is active but never enters the game, the market settles
  to the last fair market price before game start"), and §7.1 documents the MLB
  48-hour rain rule scalar-settling EVERY MLB family at ~1–2% of game-days.

**How common:** Empirically RARE historically for soccer (our agent scanned 4,913
nested combo markets, "found **zero** with `0 < settlement_value < 1`" —
`dnp_scalar_settlement.md:214`), but **materially more common for MLB**: the rain
rule pushes the trigger to ~1–2% of MLB game-days (`:248`, §7.1). So "uncommon" is
soccer-era and does NOT carry over to MLB combos.

**Where WE stand (for TASK A cross-ref):** Kalshi's rule is exactly as the
reviewer states, AND we already know it. But the CODE handles it only reactively
— `sim/engine.py` supports per-leg scalar settlement distributions but nothing
populates them (`dnp_scalar_settlement.md:302-305`), `pricing/joint.py` prices a
pure binary joint, and the operator decision (2026-07-09) was BUILD NOTHING /
tolerate a fractional settlement reactively via the fail-safe
`HALT_RECONCILIATION_MISMATCH` (`:313`, `:318`). So for the risk engine this is a
PARTIAL on our side (documented + fail-safe, not implemented) — not a
Kalshi-mechanics error. The reviewer's Kalshi-facts are correct.

---

## Claim 2 — BALANCE ENDPOINT (`balance` = cash, `portfolio_value` = positions)

**Reviewer:** "GET balance returns TWO fields: `balance` (available cash) and
`portfolio_value` (value of positions held)."

### VERDICT: **CONFIRMED** — exactly the documented shape (and we already record it).

**docs.kalshi.com verbatim** (GET /portfolio/balance, fetched 2026-07-12):
| Field | Type/Units | Description (verbatim) |
|---|---|---|
| `balance` | int64 cents | "Member's available balance in cents. This represents the amount available for trading." |
| `balance_dollars` | FixedPointDollars string | same, as a dollar string |
| `portfolio_value` | int64 cents | "Member's portfolio value in cents. This is the current value of all positions held." |
| `updated_ts` | int64 unix | last-update timestamp |
| `balance_breakdown` | array IndexedBalance | per-exchange-index breakdown (optional) |

**Our api-notes match to the field:** `docs/api-notes/index-scan.md:24` —
"Response: `balance` (int64, **cents**), `balance_dollars` (string fixed-point),
`portfolio_value` (int64, cents), `updated_ts` (int64 Unix), `balance_breakdown`
(array …)". **Confirmed LIVE on demo**, not just from docs:
`NOTES.md:102-104` — "balance payload = `{balance (cents int), balance_dollars,
balance_breakdown, portfolio_value, updated_ts}`."

**Units caveat we already flag (reviewer omits it):** `balance`/`portfolio_value`
are **cents, not centi-cents** — the wire-boundary conversion must use
`cc_from_cents` (`NOTES.md:21`, `money-fees`/`index-scan.md:199,213`). The
reviewer's field names and meanings are right; the only thing to add is the units.

So `portfolio_value` genuinely exists and means what the reviewer says — his
LAUNCH BLOCKER 3 (that using only `balance` as the bankroll denominator is wrong)
rests on a REAL, documented field. That is a TASK-A accounting point, but the
Kalshi mechanics claim itself is CONFIRMED.

---

## Claim 3 — RFQ SIZING (maker can't size down; every quote is full RFQ amount; fixed-contracts OR target-cost)

**Reviewer:** "the maker does NOT specify a quote size; every quote is implicitly
for the full RFQ amount; an RFQ is fixed-contracts OR target-dollar-cost; for
target-cost the exchange derives contract count from the quote price."

### VERDICT: **CONFIRMED** — every sub-claim is correct. Must-quote-full is right; a maker CANNOT size down.

**docs.kalshi.com verbatim** (Market Maker RFQ Quoting Guidelines / rfqs guide,
fetched 2026-07-12):
> "Quoters do not specify a size; each quote is implicitly for the full RFQ amount
> (`contracts_fp`, or whatever count `target_cost_dollars` resolves to at the
> quoted prices)." — "Partial quotes are not permitted."

**Two sizing modes (verbatim):**
- `contracts_fp` — direct contract count, 0.01-increment minimum.
- `target_cost_dollars` — dollar amount; "the exchange calculating contract
  quantity from quoted prices, returned as `yes_contracts_fp` / `no_contracts_fp`."

**Our api-notes match:** `docs/api-notes/multivariate.md:218` — CreateRfq body has
`contracts` / `contracts_fp` OR `target_cost_dollars` (with `target_cost_centi_cents`
DEPRECATED); `:252` restates the two sizing modes. The must-quote-full semantics and
the target-cost circular dependency (quote price → contract count → risk → quote
price) were flagged internally BEFORE this review: the "Final adversarial review"
already found and fixed a **critical target-cost risk-sizing defect**
(`CLAUDE.md` "Final adversarial review: DONE … 1 critical: target-cost risk
sizing", and `NOTES.md:231` D13 marks the target-cost→count conversion UNVERIFIED,
feeding only the width adder, never money math). The contracts-mode fallback to RFQ
size is verified REQUIRED, not defensive (`NOTES.md:38`, `:118` #5).

**Can a maker size down? NO.** The doc is explicit ("Partial quotes are not
permitted"). So the reviewer's LAUNCH BLOCKER 5 ("slicing whales" is impossible)
is factually correct as a Kalshi mechanic. Our own code position (whitelist +
per-combo cap ⇒ decline oversized RFQs, since we can't partial-quote) is the
correct response to this mechanic — but that's TASK A.

**Quote fields confirmed:** each quote carries `yes_bid` and `no_bid`; "If
`yes_bid + no_bid > $1` the quote is rejected"; "Either can be `\"0\"` to decline
that side, but not both." Matches `NOTES.md:224` and our `pricing/quote.py`
free-money caps + declined-side logic.

---

## Claim 4 — FEES (~0.44¢/contract maker fee at 50¢; and: is maker fee $0 on RFQ fills?)

**Reviewer:** "current maker fees ~0.44 cents per contract at a 50-cent price."

### VERDICT: **CONFIRMED as an arithmetic value** for the maker-fee coefficient, but **REFUTED as applied to OUR RFQ fills** — our verified maker fee on an RFQ fill is **$0.00**, not 0.44¢.

**The number checks out for the `quadratic_with_maker_fees` coefficient.** Our
verified maker coefficient is **0.0175** (`src/combomaker/pricing/fees.py:5-10`,
"maker 0.0175 (= 7/400) … VERIFIED against the official Kalshi fee-schedule PDF
(effective 2026-06-29, operator-provided)"; `NOTES.md:20`, D1 `:219`):

    maker fee at P=0.50, C=1  =  0.0175 × 1 × 0.50 × 0.50  =  $0.004375  ≈  0.44¢

So the reviewer's 0.44¢ is the correct maker-coefficient arithmetic. Confirmed.

**BUT the reviewer's implicit framing — that our maker quotes pay ~0.44¢ — is
REFUTED by ground truth.** Two independent reasons, both verified:

1. **Quadratic sports/combo series charge $0 MAKER fee.** The 0.0175 maker
   coefficient applies ONLY to series on Kalshi's maker-fee list
   (`fee_type = quadratic_with_maker_fees`). Sports/combo series are plain
   `quadratic`, which charges **no maker fee** — `fees.py:87-88`
   (`if fee_type is FeeType.QUADRATIC: return Fraction(0)`), D3 `NOTES.md:221`.

2. **RFQ fills are booked `is_taker=false` and cost us $0 — VERIFIED on the real
   demo ledger, not assumed.** `NOTES.md:33`: "**Maker fee = $0 on RFQ fills**
   (`is_taker=false`, `fee_cost=0.000000`) … the requester (taker) paid $0.0175 =
   ceil(0.07·1·0.48·0.52) — the quadratic taker formula verified to the
   centicent." So on our RFQ fill the **taker** (requester) pays the ~0.44¢-class
   fee; the confirming maker (us) pays **zero**. The reviewer recorded that we saw
   maker fee $0 "once" — that is CONFIRMED, and it is not a one-off: it is both
   the PDF rule (quadratic ⇒ $0 maker) and the demo ledger.

**Important nuance the reviewer is right about (TASK A):** we still must reconcile
predicted-vs-actual fee to the cent on every real fill and never predict fees into
the EV ledger (`fees.py:11-14`, defense #3, `NOTES.md:263` F4). And the maker-fee
list "can change (GET /series/fee_changes)" — a fee-schedule-version change is
already a monitored condition. The taker coefficient 0.07 and maker 0.0175 were
tagged UNVERIFIED while the PDF was 429-blocked (`NOTES.md:20`, D1) but are now
noted VERIFIED against the operator-provided PDF (`fees.py:5`). Fee rounding
(ceil to centi-cent, per-order accumulator/rebate) is fully doc-verified in
`money-fees.md §2`.

Net: the **0.44¢ figure is a legitimate maker-coefficient value (CONFIRMED)**, but
**our RFQ maker fill pays $0 (the reviewer's "recorded once" is the correct,
ground-truth-verified state)** — so treating 0.44¢ as OUR per-contract cost would
overstate our fees. Where fees genuinely bite our thin edge is only if we ever
cross as a taker or trade a maker-fee-list series.

---

## Claim 5 — CONFIRM WINDOW (3s confirm, 1s execution timer)

**Reviewer:** "3 seconds to confirm, then a 1-second execution timer."

### VERDICT: **CONFIRMED** — doc-verified AND measured live on demo.

**docs.kalshi.com verbatim** (rfqs guide, fetched 2026-07-12): for combo/HVM
markets, "Confirmation window: 3 s" and "Execution timer: 1 s"; makers must
confirm within the window and orders post to the book after the execution timer.

**Our api-notes match:** `docs/api-notes/multivariate.md:223` — "All combo markets
are HVMs. Standard markets: confirmation window 30 s, execution timer 15 s. HVM
(i.e., ALL combos): **confirmation window 3 s, execution timer 1 s**." Restated
`:241`, and doc-verified in `NOTES.md:17` ("HVM timing 3s confirm / 1s execution —
Confirmed").

**Measured LIVE on demo** (Phase 5 e2e, `NOTES.md:83-85`, `:94`):
- accept → our confirm: **117 ms** of the 3s HVM window (E5 RESOLVED: 117ms vs
  3,000ms budget).
- confirm → executed: **1.29 s** ("the 1s HVM execution timer + latency").
- last-look local decision: 0.89 ms.

So the reviewer's "3s confirm, 1s execute" is exactly right, and our demo confirm
in 117ms is well inside it. Confirmed on both doc and ground truth.

---

## Claim 6 — combo_no_pays_complement under scalar settlement (NO = 1 − S_YES, S_YES = product of scalars)

**Reviewer:** generalized model `s_i ∈ [0,1]`, `S_YES = ∏ s_i`, `S_NO = 1 − S_YES`,
long-NO P&L `= 1 − S_YES − p_N − f`, max loss `p_N + f` (worst case `S_YES = 1`).

### VERDICT: **CONFIRMED** — our verified convention is the binary special case of the reviewer's generalized model; they are consistent.

**Our verified convention** (`combo_no_pays_complement: true`, promoted from a REAL
$1.00 demo settlement — `docs/reports/2026-07-10-demo-combo-settled.md`,
`tests/fixtures/ground_truth/conventions.json`): NO pays **1 − V** where
**V = ∏ leg settlement values** (`min(1, ∏)`). The demo settled the binary case
(V = 0 via early-NO on a failed LAA leg) → NO paid exactly $1.00 = 1 − 0, to the
cent.

**Generalizes exactly to scalar.** Our own spec already writes NO's payout as the
continuous `1 − V` with `V = min(1, ∏ᵢ vᵢ)`, `vᵢ ∈ [0,1]`:
- `docs/dnp_scalar_settlement.md:52-65`: "`V = min(1, ∏ᵢ vᵢ)` … we are long NO, NO
  pays `(1 − V)`. `V = 0` ⇒ P&L `= 1 − q` (max win); `V = 1` ⇒ P&L `= −q` (max
  loss). Because `V` can be **fractional**, our payoff is **not** binary."
- Per-contract long-NO P&L there is `(1 − q) − V` before fees, i.e. exactly the
  reviewer's `1 − S_YES − p_N − f` with `S_YES = V`, `p_N = q`. Max loss `= q + f`
  at `V = 1` — identical to the reviewer's `p_N + f`.

So `combo_no_pays_complement` (NO = 1 − S_YES) holds **verbatim** under scalar
settlement, with `S_YES = ∏ scalars` rather than `∏ binaries`. The Kalshi Help
Center product rule ("combo payout = product of each position's value";
"blue arrow = scalar $0–$1") is the same identity from the YES side. The reviewer's
generalized model and our promoted convention are **consistent** — his model is the
scalar generalization; our demo confirmed the binary instance to the cent.

**One reconciliation caveat we already track (not a conflict):** a legitimate
fractional NO payout (e.g. $0.30) must NOT trip `HALT_RECONCILIATION_MISMATCH` —
that reconciliation-tolerance is exactly the gate behind `combo_no_pays_complement`
(`dnp_scalar_settlement.md:288-290`, §risk/). Today it's handled reactively (the
halt is fail-safe; the `1 − V` fix is minutes) rather than pre-built. Consistent
with the reviewer's model; the only open item is our own build-vs-reactive stance,
a TASK-A point.

Second-order sweeteners we've verified, both slightly toward the NO-seller (so the
scalar case is if anything mildly favorable, not adverse): the combo
`functional_description` floors V to the grid — "Scalar outcomes are multiplied
(rounded down)" — so we receive `1 − floor(V) ≥ 1 − V`
(`dnp_scalar_settlement.md:170-176`).

---

## Sources
- Kalshi Help Center — Combos: https://help.kalshi.com/en/articles/13823820-combos (product rule, scalar $0–$1, blue-arrow, 1–12h timeline) — fetched 2026-07-12
- docs.kalshi.com — RFQ quoting guide (must-quote-full; contracts_fp vs target_cost_dollars; 3s/1s HVM; yes_bid/no_bid; yes_bid+no_bid>$1 reject) — fetched 2026-07-12
- docs.kalshi.com — GET /portfolio/balance (balance cents + portfolio_value cents + balance_dollars + updated_ts + balance_breakdown) — fetched 2026-07-12
- docs.kalshi.com — market_settlement ("Settlement fees are zero for simple yes/no determinations but may apply for sub-cent scalar settlement")
- Repo: docs/api-notes/multivariate.md (:168, :218, :222, :223, :225, :241, :252); docs/api-notes/money-fees.md (§2, :192, :209, :276); docs/api-notes/index-scan.md (:24, :199, :213); docs/dnp_scalar_settlement.md (§1, §7, §7.1, :52-65, :168-176, :288-290, :302-305, :313); src/combomaker/pricing/fees.py (:5-14, :80-89); NOTES.md (:17, :20-21, :33, :83-85, :94, :102-104, :118, :219 D1, :221 D3, :231 D13, :263 F4); docs/reports/2026-07-10-demo-combo-settled.md; tests/fixtures/ground_truth/conventions.json
