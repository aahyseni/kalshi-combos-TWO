# Design pass: nested-band pricing + team routing + all-legs sweep (reviewed, wire-ready)

**Date:** 2026-07-10 ~03:30 UTC · **Status:** designs COMPLETE + adversarially
reviewed (4 real defects caught, all fixable) — awaiting operator go on wiring
order · **Staged code:** in the workflow output (bands 29k chars, routing 24k
chars) + scratch prototypes under job-tmp `design/` (proto_resolver.py = the
rule-8 parity reference, 25/25 exact).

## 1. Nested-band pricing (the exchange-allowed yes-low + NO-high shape)

- **Core discovery:** `price_containment` pins the joint to ONE marginal — it
  cannot express P(low)−P(high). Design: new `RelationshipKind.NESTED_BAND` +
  engine **super-leg collapse** (band pair → one synthetic leg with exact
  p = P(low)−P(high), then normal SGP machinery). One code path covers bare
  2-leg bands (reduces to the exact marginal, zero ρ) AND the real tape shape
  (6 legs = 3 bands in 3 games → exact product at cross_event_rho=0).
- **Live mispricing quantified (prod mids):** ESPBEL [8,11) +2.21¢ · ARGSUI
  [7,10) +1.82¢ · **NORENG [8,9) +6.64¢ (+55% relative)** — narrow bands are
  systematically worst and likely what band-takers hunt. Plus the flat
  fallback burns ~9–10¢ of correlation-uncertainty width that exact
  arithmetic eliminates. **Fréchet can never catch this:** the exact band IS
  the Fréchet lower bound — the copula's wrong price sits inside the interval.
- Farm extension (over-11 YES + over-8 NO = certain NO): airtight bar verified
  from live rules text (one combined count incl. ET); zero engine change
  (existing farm path is generic).
- Fail-closed: band + any same-game companion → UNKNOWN (window-event
  correlation attenuation unmeasured); inverted mids → NoQuote.
- Registry design is sport-generic (MLB totals = one entry if Kalshi ever
  lifts size_max=1) but deliberately soccer-corners-only now.

## 2. Team routing (one parser, two unlocks)

- **Convention proven on 1,549+ live markets:** all 30 MLB team codes
  enumerated; **no code is a prefix of another and all 870 ordered
  concatenations tile uniquely** → end-anchored parse (prefix=away,
  suffix=home, both/neither=refuse) is provably unambiguous. The naive
  both-split approach was ambiguous in **80/445** sampled markets — the trap
  was real.
- 2 resolvers + 3 helpers in sgp.py (soccer-pattern, fail-closed at every
  step), 22 oriented config keys + 22 bands (plain neutralized entries KEPT as
  fallback), ~20 tests. Prototype: **25/25 cases exact** incl. ±0.24 ml|ks
  flips, facing −0.13, doubleheader refusal, same-player → None.
- Catches while verifying: **moneyline|player_tb is missing from config
  entirely** (1,267 sg pairs/10h at flat +0.6 — an enumeration gap in the
  original tranche); the [D] labeled teammate splits are internally
  inconsistent with the later pooled measurements (correctly omitted —
  measure, don't guess).

## 3. The sweep — full 9×9 matrix, every cell marked

**Tally: 29/45 wired · 4 exchange-blocked/defensive · 12 UNTABLED cells with
~13,257 sg pairs/10h still at flat +0.6.** The headline: **spread×props (5
cells, 9,641 sg pairs/10h) appears in NO prior tranche list** — 11.5× the flow
of ml|spread which we'd been calling "the worst game-level gap."

**⚠ REGRESSION FOUND:** the [D] promotion made same-player cross-family pairs
WORSE — 5,591 pairs/10h (HIT×HR 2,223…) now price at the wired distinct-player
+0.01..+0.04 when the truth is containment (effective ~0.95). Priority fix.

Ranked DO list (1–11) and an honest DON'T list (series momentum, platoon
splits, weather factors, MLB bands — all measured-nil or exchange-blocked) in
the workflow output. Notable: MLB farmable flags must NOT inherit soccer's
farmable=True pattern — the 48h-postponement scalar rule breaks airtightness.

## 4. Review verdicts (xhigh, everything re-executed from disk)

Designs mutually consistent, zero staged-diff collisions, config values
zero-drift vs measured artifacts. **Four defects caught:**
1. A staged routing invariant test is mathematically wrong (3/4 cases fail:
   routed value+band exceeds the neutralized band, e.g. 0.31>0.28) — trivial
   fix, but a wiring blocker as written.
2. **Bands atomicity set incomplete:** `tools/backtests/wc_backtest.py`
   mirrors the engine dispatch ("keep in sync", rule 8c) and would silently
   price NESTED_BAND through the copula — must ship in the same commit.
3. DO-3's ml|spread sketch parses teams by raw suffix equality — inequality
   does NOT prove opposite teams, and :opp is a mutual-exclusion IMPOSSIBLE
   verdict. Must reuse the anchored parser (ONE MLB parser).
4. DO-2's same-player layer must land at the routing resolver's None seam (or
   before it) — wired after, it's dead code; and its buried-containment
   pricing deviates from the soccer decline-UNKNOWN precedent (flow-kill
   argument is real; needs operator sign-off).

## Recommended wiring order (reviewer's, dependency-verified)

```
0. tools/backtest_mlb_pairs.py FIRST (+ DO-9 counters)   ← the still-unbuilt rule-8
   gate for the ALREADY-live 32-entry table; baseline replay before new wiring
1. DO-1 config quick-fix (12 untabled cells) + DO-7 event-flag fixture (trivial)
2. BANDS  (atomic 3-file: relationships + engine + wc_backtest mirror)
3. ROUTING (fix the broken test first; parity vs proto_resolver.py to 1e-12)
4. DO-2 same-player containment (regression fix; anchors AFTER bands)
5. DO-3 ml|spread containment (reuse the ONE parser; farmable=False; + plain fallback entry)
6. DO-5 per-rung keys · DO-6 basket width · DO-8 measurements · DO-10/11 opportunistic
```

## NEXT STEPS

- **Owner (operator):** approve the wiring order (esp. backtest-first) and
  rule on the DO-2 buried-containment question: price buried same-player pairs
  near-cap (keeps basket flow alive) vs decline UNKNOWN (soccer precedent,
  fail-safe). Reviewer marked it UNCERTAIN — genuinely your call.
- **Next session:** execute the approved order; each step ends with the
  backtest differential + cent-parity per rule 8.
- **Standing:** LAA/demo settlement watch; recorder through Jul 11; WC backtest
  after Jul 11 settlement.
