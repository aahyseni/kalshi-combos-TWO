# The one-leg-per-ladder rule — verified two ways (tape backcheck + demo validator probe)

**Date:** 2026-07-10 ~01:55 UTC · **Trigger:** operator hypothesis from the
Kalshi combo-builder UI ("only 1 leg per ticker family, e.g. can't take over-1.5
and over-2.5") · **Method:** 3.02M-combo tape scan (all sports, two rowid
windows) + demo exchange-validator construction probes (the 2026-07-07 farming
audit method). Evidence: `tmp/ladder/` scratch (scan_results.json,
corner_pairs.json, probe payloads).

## The rule, as the exchange actually enforces it (SIDE-AWARE)

| construction | exchange verdict | evidence |
|---|---|---|
| Same market ticker twice | ❌ 400 `invalid_parameters` | probe |
| Same ladder, two rungs, both YES (over-2 + over-4; player 1+ & 2+) | ❌ 400 `duplicated_legs` (semantic — fires even when the event has no size cap) | probe + **0 in 3.02M combos** |
| Cross-series strict containment (first-goal YES + 1+goals YES same player) | ❌ 400 `conflicting_leg_outcomes` | probe |
| Same ladder, **YES-low + NO-high (a BAND)** | ✅ **ALLOWED** — mints a market | probe (corners band) + **114 real tape combos** |
| Cross-ladder same player, no strict containment | ✅ allowed | probe + tape (abundant) |
| Same family, different players/games | ✅ allowed | tape (hundreds of thousands) |

So the operator's hypothesis is **confirmed for same-side legs** — and refined:
the validator is side-aware, permitting **band structures**: e.g.
`KXWCCORNERS-...-8 YES + KXWCCORNERS-...-11 NO` = "corners in [8,11)".

## Band combos on the real tape (the new pricing surface)

- **85 match-corners pairs** (62 RFQs) + **29 team-corners pairs** — 100% of
  them yes-low/no-high bands, ~0.002% of combo flow, recurring across days.
  Zero bands in any OTHER family: totals/spreads/BTTS/props all show 0 same-game
  multi-leg groups in 3.02M combos (their events carry `size_max=1`, which
  blocks even bands; corners events have `size_max=null`).
- **Pricing today (the gap):** a match-corners band is a same-game
  CORNERS×CORNERS pair → `corners|corners` is NOT in the soccer table and the
  nested-containment family covers CORNERS_TEAM only → **flat +0.6/0.90
  fallback**, when the truth is exact arithmetic: P(band) = P(over-low) −
  P(over-high). Team-corners bands do better (corners_team|corners_team:same
  0.90) but are still copula-approximated, not exact.
- Parser note: KXWCTCORNERS suffixes concatenate TEAM+LINE with no dash
  (`MAR4`, `FRA10`) — any ladder detection must special-case this.

## Consequences (wired into planning)

1. **Containment phase scope grows by one item:** nested-band arithmetic (same
   family + same game/team + yes-low/no-high → joint = P(low)−P(high), exact,
   no ρ) — covers match corners (live mispricing today, small flow) and any
   future `size_max=null` ladder family. The IMPOSSIBLE direction
   (yes-high + no-low) is already a farmable tautology for CORNERS_TEAM;
   extend to match CORNERS.
2. **Same-side rung handling needs NOTHING** — exchange-blocked; the nested
   `total|total 0.95` global entry is defensive/unreachable within a game
   (cross-game total ladders remain the real use).
3. **Same-player cross-stat containment demand confirmed** by
   `conflicting_leg_outcomes` only firing on STRICT containment — non-strict
   same-player pairs (HIT-1 × HR-1) still construct and still need the
   structural branch.
4. MLB analog caveat: demo had NO MLB events tonight (WNBA + WC + crypto only)
   — MLB probes ran on exact structural analogs; the validator service
   (`market-metadata`) is shared, and the tape shows MLB behaves identically
   (0 same-side ladders in 593k MLB TOTAL legs). Re-probe MLB directly when
   demo lists it.

## Probe hygiene

Control RFQ succeeded (rule confirmed non-trivially); both created RFQs deleted
(204, verified); 4 inert combined markets minted as construction side effects —
no orders/quotes ever placed. `target_cost_dollars` rejects plain-integer
strings ("invalid dollar precision") — `contracts_fp` used instead; worth
remembering for any future demo RFQ tooling.

## NEXT STEPS

- **Containment phase (next after resolvers):** add nested-band arithmetic +
  extend the corners farm/impossible family to match-CORNERS; wire the
  TCORNERS suffix special-case into any ladder detection.
- **Standing:** re-probe the MLB ladder rules directly when demo lists MLB
  events; the analog verdict is near-certain but unproven on MLB tickers.
