# 2026-07-23 — Same-player HRR pricing audit (CLEAN) + player-level risk-book gap

**Trigger.** A taker repeatedly hit **Ketel Marte same-player cross-stat combos**
via RFQ — combos Kalshi's retail builder **blocks** ("Invalid combo", same-player
SGP restriction) but the RFQ/maker pathway auto-creates. Fill-prober flagged them
as +12.7¢ / +19.9¢ / +21.8¢ "rich vs external" on thin 4–7-maker fields. Operator
halted the bot: *"we can be getting picked off with this."* Correct call — a
counterparty repeatedly, selectively hitting one retail-blocked structure in size
is the classic pickoff signature, and it overrides our own model's self-assessment.

## Audit 1 — is the same-player HRR pricing holed? VERDICT: NO.

Operator supplied the H+R+RBI logical-constraint spec. Every constraint is **already
implemented and firing** (DO-2 2026-07-10 design; `conditionals_mlb.py` +
`relationships.py` `_containment_sign`), verified by running the live code:

| Spec constraint | Live implementation | Verified |
|---|---|---|
| **Strict implication** `N+ hits ⇒ N+ HRR`; joint = `P(N+ hits)` **not** independent product (≈2× pickoff) | exact cells `('hit',N,'hrr',N)=1.0` + `_containment_sign` → "YES hit & YES hrr" → **containment=P(subset)** | ✅ highest-severity vector defended |
| Impossible: `hit N+` but `<N HRR` | "YES hit & NO hrr" → **"impossible"** | ✅ |
| Contrapositive `0 HRR ⇒ 0 hits` | "NO hit & NO hrr" → containment | ✅ |
| Reachable `0 hits & 2+ HRR` ~1–3%, **never 0** | "NO hit & YES hrr" → copula → **4.88%** | ✅ our fill shape |
| N+ HRR marginal from book, not scaled hit rate | HRR leg marginal = Kalshi market | ✅ |
| Unclassified pair → UNKNOWN → no-quote | unmeasured cells decline UNKNOWN | ✅ |

**Code correctness (empirical).** `implied_rho` calibrates one Gaussian-copula rho to
the measured YES-YES cell through the SHIPPED integrator; for binary marginals that
pins the whole 2×2 table. Reproduced the NO-side cell we're short:
```
"0 hits AND 2+ HRR":  copula joint 0.0488  ==  arithmetic 0.0488   ✓ exact
"2+ TB AND <2 HRR":   copula joint 0.0417  ==  arithmetic 0.0417   ✓ exact
```
Both actual fills took the **copula path** (`is_exact` False), not a mispriced
containment.

**+EV, not picked off.** At live marginals: fair YES ≈ 4–5%, we sold at 10–11¢ →
**~+6¢/contract above our own fair**; the 4–7-maker external field (22–31%) is the
loose one, and the taker **overpaid** for a longshot they couldn't build retail. For
it to actually be a pickoff, Marte's "0 hits but 2+ HRR" rate would need to be
**2.3× the pooled 16%** — implausible; the ±0.12 rho band absorbs realistic player
deviation. **Settlement of Marte's AZ@STL line is the final ruler.**

## Finding 2 — player-level concentration is INVISIBLE to the risk book (operator-flagged)

Verified **$304.99 on Ketel Marte across 6 combos** — and it GREW during the episode
("no Marte 1+, yes Marte 2+" went $74→$149 as the taker re-hit it). The enforced
directional cap is game-result/ML-centric (P0-9 nets the moneyline ME event; prop
legs carry tiny game-result deltas), so same-player prop clustering never trips
`skip_directional_cap`. Worse, player risk **spans games** via cross-game combos
(a Detroit×Marte combo), so no single game cap bounds one player. Bounded for now
(the two big exotic shorts have mutually-exclusive losing states → max Marte loss
≪ $305; game caps loosely backstop) but real, growing, invisible. Full write-up +
enhancement spec: operator memory `project_kct_player_level_risk_gap`.

## Actions

- Markup walked back 1¢ earlier (separate report); bot halted on the pickoff concern,
  audited CLEAN, **relit on `data/live_20260723_v5.log`** (supervisor + workers up,
  0 halts, quoting; first fills in line, e.g. +0.0¢ on a 25-maker market).
- Same-player family **NOT blocked** — pricing is sound and the flow is +EV; operator
  chose to keep it (and wants to mark it up MORE, not less).

## NEXT STEPS

- **Me (~evening):** pull Marte's AZ@STL settlement → the +EV verdict on the rich
  same-player fills (owner: me).
- **Build (post-slate):** (1) player-level / all-leg-family concentration dimension
  in the risk book — cap = auto-scaled fraction of bankroll, generalizes the
  directional fold beyond game-result; (2) same-player MARKUP adder (loose external
  field leaves EV room; mirrors the corners adder). Both off-line builds, parity-
  checked, never hot-patched (owner: me; operator prioritizes).
- **Watch:** continued same-player hammering growing one-entity concentration until
  the player-level cap exists (owner: me).
