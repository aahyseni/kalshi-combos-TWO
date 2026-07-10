# Steps 2-3 WIRED: nested-band exact pricing + MLB team routing — prop-carrying error now sub-cent

**Date:** 2026-07-10 ~07:30 UTC · **Commits:** `7550df6` (bands) + `ca97ba6`
(routing) · Joint verification: **ALL 5 CHECKS PASS**, suite **1055 passed / 0
failed**, mypy --strict + ruff fully clean.

## The headline: the gate keeps improving

Prop-carrying combos (n=1,533, fair vs pregame clearing):

| config | median err | context |
|---|---|---|
| legacy flat-0.6 | 2.22¢ | where we started yesterday |
| promoted 32-entry | 1.25¢ | the measurement tranche |
| + DO-1 (43 entries) | 1.22¢ | untabled cells closed |
| **+ routing (:same/:opp)** | **0.97¢** | **sub-cent vs the market** |

Differential proof: 43,199 pair transitions across 20,283 combos — ALL on the
12 routed keys, every ρ move exactly matching the shipped config; legacy fairs
and the game-lines bucket **bit-identical**; 0 MLB band shapes (as expected).

## Bands (soccer corners — the exchange-allowed yes-low+NO-high shape)

- 85/85 real tape band pairs classify NESTED_BAND; **zero reach the copula**.
- Engine fair == exact P(low)−P(high) **to the centi-cent** on live mids (3/3;
  flat-0.6 had been overpricing +2.6..+6.1¢); the 6-leg 3-band tape combo
  prices as the exact product.
- Farm extended to match corners (impossible direction); fail-closed on
  band+companion (UNKNOWN) and inverted mids (NoQuote); decline reasons
  enumerated to exhaustion; both backtest dispatch mirrors updated atomically.
- Quote width on bands: ~1.5–3.5¢ (u_low+u_high) vs the ~9–10¢ the flat
  fallback injected — tighter AND correct.

## Routing (MLB :same/:opp)

- Rule-8 parity: 25/25 cases vs the design prototype to 1e-12 incl. source
  strings. Integration triple exact: same-team ML×KS +0.24 · opponent −0.24 ·
  unresolvable → plain 0.00/0.30.
- Found + fixed a SECOND staged-test defect during wiring (LSF suffix-anchors
  COLSF — the staged expectation was wrong; the wiring agent proved it).
- Doubleheader G1×G2 never merges; same-player pairs refuse to plain
  (containment phase owns them); soccer 0.70 / NFL 0.88 untouched.

## NEXT STEPS

- **Steps 4-5 (launching):** same-player cross-stat containment (the [D]
  regression fix — near-cap for exact implications, UNKNOWN for partial until
  measured) + ml|spread containment family (reuse the ONE anchored parser,
  farmable=False per MLB scalar-settlement rules, plain fallback entry added).
- Then: DO-5 rung keys · DO-6 basket width · DO-8 measurements · mlb_runs grid
  calibration follow-up (5.53¢ own error) · ML×2 winner's-curse → markup work.
- Standing: LAA/demo settlement check (overnight games done — check today),
  recorder through Jul 11, WC backtest after Jul 11.
