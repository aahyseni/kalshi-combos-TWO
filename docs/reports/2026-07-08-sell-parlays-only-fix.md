# Fix: `sell_parlays_only` — combo quotes are one-sided (parlay seller)

**Date:** 2026-07-08 · **Status:** IMPLEMENTED, reviewed (self + independent
agent), full suite green · **Goal:** the live engine must NEVER end up long the
YES side of a combo (never "buy the parlay") — quote the NO/seller side only.
**Basis:** [combo YES/NO side mechanics report](2026-07-08-combo-yes-no-side-mechanics.md)
(accepting our `yes_bid` ⇒ we're long YES = the −14¢/ct fade side; accepting our
`no_bid` ⇒ long NO = the +EV seller side).

## What changed (main pricing engine)

| File | Change |
|------|--------|
| `pricing/quote.py` | `QuoteParams.sell_parlays_only` (default False); `construct_quote` forces `yes_bid=0` when set |
| `pricing/engine.py` | `_enforce_sell_only` — engine-boundary choke point that zeros a leaked YES from ANY builder + logs; startup warning when sell-only but `combo_no_pays_complement` unverified |
| `ops/config.py` | `QuoteConfig.sell_parlays_only` (default False) |
| `config/prod.yaml`, `config/demo.yaml` | `pricing.quote.sell_parlays_only: true` ← the live policy |
| tests | 6 property tests (`TestSellParlaysOnly`) + full-stack engine test + real-YAML e2e test + belt-and-suspenders leak test + 2 config-load asserts |

**Non-breaking by design:** both defaults stay `False`, so the two-sided pricing
primitive + all existing tests are untouched. Policy is turned on only in the
main YAML. **Full suite: 983 passed, 0 failed; mypy + ruff clean on changed files.**

## Guarantee (how yes_bid can never leak)

Two independent zeros PLUS an engine-boundary backstop:
1. `construct_quote`: `yes_bid = 0 if sell_parlays_only` — after all width/skew/
   cap/rounding; nothing downstream lifts it. Property-tested over 400 adversarial
   examples incl. a negative-skew mutation that DOES lift YES two-sided.
2. `construct_farm_quote`: already hard-zeros YES for every input.
3. `PricingEngine._enforce_sell_only`: the single authoritative boundary — any
   `ConstructedQuote` leaving the engine with a non-zero YES in sell-only mode is
   corrected to 0 and logged (catches a future builder that forgets). Tested by
   monkeypatching a "leaked" non-zero YES through the real `price()` path.

## Review outcome (self + independent zero-bias agent)

**Both: goal met, no holes in the invariant.** Independent agent traced every
branch of both builders, the config wiring (`prod.yaml → PricingConfig →
QuoteParams → construct_quote`, via `quote_app.py`), and downstream consumers
(exposure book skips 0-bid sides; lifecycle would even refuse a stray YES accept).
All findings were LOW/INFO. Acted on: engine-boundary backstop (was 2 points, now
1 authoritative choke point), fixed a misleading "SINGLE point" comment, added the
real-YAML e2e test + the leak test, added the inertness startup warning.

## Block re-verified airtight (2nd agent) + default decision

A second adversarial agent verified the parlay-seller block end-to-end:
**"YES — airtight for the never-long-YES invariant and cleanly toggleable."** No
non-zero `yes_bid` can leave `engine.price()` in sell-only mode via any builder,
reprice, or lifecycle path; the seller (NO) side is preserved and priced
identically to two-sided; silent-disable is caught by tests. Findings were
LOW/INFO:
- **H1 (FIXED):** a hypothetical leaked YES-only quote (`yes>0, no=0`) would have
  been zeroed into an invalid `(0,0)` quote — `_enforce_sell_only` now returns a
  clean `NoQuote` instead, with a regression test (`test_engine_boundary_declines_a_leaked_yes_only_quote`).
- **H2 (noted):** the choke point is enforced by call convention (both `price()`
  return sites wrap `_enforce_sell_only`), not a structural single-exit — a future
  unwrapped return could bypass. Left as a future hardening.
- **H3 (noted):** `ops/ground_truth.py` (Phase 2.5 harness) hardcodes a non-zero
  `yes_bid` but is DEMO-only + CLI-only, never reached by `QuoteApp`.

**Default stays `False` (decision, tested):** flipping the pydantic default to
sell-only was tried and **breaks 15 tests** — incl. a deliberate
`test_default_config_regression_two_sided_quote` guard and a lifecycle cascade
(sell-only + unverified NO ⇒ every confirm declines ⇒ fills vanish). The policy
lives in `prod.yaml`/`demo.yaml` (the live engine), and the pydantic default stays
the neutral two-sided primitive for the test suite. **Full suite: 984 passed.**

## ⚠ CRITICAL dependency — sell-only is INERT until two conventions are verified

Sell-only makes EVERY fill a NO position. Two Phase 2.5 gaps block it from
actually executing (both are SAFE — they fail closed, not open):

1. **`combo_no_pays_complement` is `null`** (`conventions.json:7`). The lifecycle
   **declines every NO-side confirm** while it's unverified (`lifecycle.py:271-281`,
   `DECLINE_CONVENTION_UNKNOWN`). So we will **quote but never fill** a sell-only
   combo until this is verified. (Now surfaced by a startup warning.)
2. **`accepted_side → long-side` direction** rests on docs + the 2026-07-06 demo
   ledger, not a fresh live combo round-trip. The fix is correct GIVEN it; confirm
   before hardening.

**Both unblock with ONE demo combo RFQ round-trip** (records the NO settlement
value AND the long-side direction). Nothing can quote live yet anyway
(`mode: observe`, `prod_limits_configured: false`).

## NEXT STEPS

- **Owner (eng, needs combo demo liquidity):** run one demo combo RFQ round-trip
  to verify `combo_no_pays_complement` + re-confirm `accepted_side → long-side`.
  This unblocks sell-only from inert to live-capable.
- **Decision owed by operator:** accept the LOW/INFO review findings as-is (done),
  or require anything more before enabling on prod (prod is still observe-only).
- **Deferred (not done, low value):** farm+sell combined engine test; confirm
  `ground_truth.py` (a tool, not the live engine) can't run in a quoting session.
- Relates to [[project_kalshi_combos_two]]; supersedes the "not protected yet"
  note in the side-mechanics report §4.
