# Cap-family consolidated-spec reconciliation + report (2026-07-22)

The operator's **consolidated** correlation-adaptive-caps spec (supersedes the
earlier one). This reconciles it against the cap layer already built this session
and closes the gaps. Prior wiring/arm report: `2026-07-22-adaptive-caps-wiring-and-arm.md`.

## Step 0 — reconciliation (built code vs consolidated spec)

**Already correct (no change):** `f_slate = (kill_anchor/k_trip)·√G_eff/σ₁`;
`game = f_slate/expected_games`; `per_combo = 0.01`; `ruin_floor = 0.30`;
`hard_trip = kill_anchor`; the ρ_x ≥ 0.05 fast-tighten/slow-loosen ratchet (formula
shrinks via G_eff, gate blocks loosening); the 0.15 provisional clamp; book caps =
1.3×MC; σ₁ / G_eff estimators from realized per-game P&L (never a P&L window).

**Gaps closed:**

| # | Gap | Fix |
|---|-----|-----|
| 1 | `daily`/`drawdown` were fixed z-anchors, not `k·sigma_day/bank` | `sigma_day` now derived from the FINAL (post-clamp/ratchet) `f_slate`; halts tighten with the clamp. Identical to the z-anchor when `f_slate`=solved (measured-unclamped) and in the unmeasured bootstrap; only the **measured+clamped** regime changes (tighter, correct). |
| 2 | No validation guard | `kill_covers_drawdown(kill_anchor, sigma_day/bank)` — rejects any pair where `kill_anchor < k_dd·sigma_day/bank` (the old-model failure). Asserted in `derive_cap_fractions`. |
| 3 | No startup P(KILL) alarm | `projected_kill_prob(...)` → `kill_sigma_multiple` + `kill_prob_60n` on every `CapFractions`; logged each refresh, `WARNING` if σ-multiple < 4 or P > 0.10. |
| 4 | `kill_anchor` hardcoded | now `RiskConfig.adaptive_caps_kill_anchor` (default 0.12) → engine → brain → formula. The operator's one risk-appetite dial. |

**Open gap (flagged, not silently overwritten):**

- **ρ_wg is not separately measured** — `within_game_rho` is a pass-through, not
  computed. σ₁ (measured directly from per-game P&L variance) already *reflects* ρ_wg,
  which is what the caps consume. A distinct ρ_wg reading needs **per-combo P&L
  within a game** (a richer structure than today's per-game `GamePnl`). This ties to
  the pending per-game-P&L DB reconstruction fast-follow; until then ρ_wg is reported
  as UNMEASURED and σ₁ carries its effect. **No behavior depends on the missing
  ρ_wg** — flagged for the human per Step 0.

**Behavior-change note for the operator:** gap-1's fix means that in the *future*
measured-but-clamped regime, the daily/drawdown SOFT halts scale down with actual
deployment (deploying 15% of a 60% budget → ~1.8% daily soft-halt). The hard KILL
stays at `kill_anchor`. This is the spec's matched-pair design. It does **not** affect
tonight's bootstrap (unmeasured → z-anchor 0.072/0.096, unchanged).

## Report back to the human (spec-required)

### Items 1/2 — measured ρ_wg / σ₁ + slate allowed at each kill_anchor

MLB estimator on live history: **UNMEASURED** (`stable=False`, σ₁=None) — 0 MLB P&L
nights logged. The formula's projection (12-game night, ρ_x≈0 → G_eff≈12):

| σ₁ | ~ρ_wg | slate @ KILL 12% | @ KILL 30% | @ KILL 45% |
|---:|---:|---:|---:|---:|
| 0.20 | <0.10 | 42% | 104% | 156% |
| 0.31 | 0.10 | 27% | **67%** | 101% |
| 0.39 | 0.20 | 21% | **53%** | 80% |
| 0.46 | 0.30 | 18% | 45% | 68% |
| 0.53 | 0.40 | 16% | 39% | 59% |
| 0.65 | 0.60 | 13% | **32%** | 48% |

**The formula reproduces the spec's sim table exactly at KILL 30%** (bold: 67/53/32
vs spec 66/52/32) — independent confirmation the derivation matches the operator's
simulation. Slate > 100% = leverage, held back by the absolute notional backstops.

### Item 3 — projected P(KILL fires over 60 nights)

| Config | KILL σ-multiple | P(KILL / 60 nights) |
|---|---:|---:|
| **Current armed bootstrap** (slate 15%, KILL 12%) | **5.0σ** | **1.7e-5** ✅ |
| solved @ KILL 12% (slate 28%) | 5.0σ | 1.7e-5 |
| solved @ KILL 30% (slate 69%) | 5.0σ | 1.7e-5 |
| OLD model (slate 65%, KILL 12%) | **2.1σ** | **0.63** ❌ self-destructs |

Every matched pair sits at exactly `k_trip`=5σ by construction; only the old
mismatched pair lights the alarm. This is now logged at every refresh.

### Item 4 — cap-bound vs flow-bound

UNMEASURED — 0 MLB auctions/fills logged. Auctions-won / filled / self-declined +
the decline histogram populate from the first live slate (`phase:decline` reports).

### Item 5 — book-cap MC (ES99 / all-hit) on a projected 12-game night

UNMEASURED pre-book: empty-book MC≈0 → book caps sit at the derived floor
(directional/det_max = slate 0.15, cvar = drawdown 0.096). 1.3×MC governs once the
projected book is non-trivial; the MC machinery (`sim/book_risk`) is already live.

## Verify

- Full suite green; ruff + mypy clean on the 5 changed modules.
- `test_cap_family.py` +5 spec tests: guard rejects the old config, P(KILL) healthy
  vs mismatched, halts track deployed vol when clamped, kill_anchor is a proportional
  dial, KILL σ-multiple = k_trip when solved.

## NEXT STEPS

- **Owner: bot** — the armed bootstrap (KILL 12%, slate 15%) is unchanged and safe;
  relight unaffected. `adaptive_caps_kill_anchor` lets the operator raise the dial
  once MLB σ₁/ρ is measured.
- **Owner: bot (fast-follow)** — per-game-**and-per-combo** P&L DB reconstruction →
  activates σ₁/ρ_x measured regime AND lets ρ_wg be measured (closes the open gap).
- **Decision owed: operator** — drawdown tolerance (`kill_anchor`) to run once MLB
  is measured: 12% (conservative) / 30% (3× profit at better P(profit) per the sim) /
  45%. Bootstrap holds 12% until then.
