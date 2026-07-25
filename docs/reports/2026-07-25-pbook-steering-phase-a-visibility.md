# 2026-07-25 — P(book) steering, Phase A: the signal is live (shadow)

**Operator directive (2026-07-25, verbatim intent).** The DET directional $
size was fine; the failure was a NO-VARIANCE one-way book — the two variance
games profited, one variance-less game erased them. **P(book) must STEER the
betting** ("more variance = higher Pbook"); no manual strict numbers; a
self-aware adapting book that recognizes what lowers/raises P(book), variance,
and profit. This PROMOTES P(book) steering to the front of P1 (hard net-bounds
recede; caps stay the only refusal layer per the standing doctrine).

## Phase A — SHIPPED (`696be86`, suite 2694/0, zero behavior change)

The steering signal now exists and is measured on every decision:

| where | what |
|---|---|
| candidate gate (`_TailAxes.p_profit`) | P(book P&L > 0) per PRE/POST axis — free off the already-sampled common-random-number vectors |
| every gate verdict (`candidate_gate_ev` + `candidate_gate_confirm` logs) | `pre_p_book`, `post_p_book`, **`delta_p_book`** — positive = this fill ADDS variance/diversity, negative = it concentrates one-way |
| every 15s snapshot (`book_risk_snapshot` log) | `p_book`, `ev_cc`, `top_tail_games` (which games dominate the downside — the measured concentration) |

Pinned by test in miniature of the 7/23 slate: a one-way book at P(book)=0.40
(the Detroit shape) + a diversifier on an independent game → P(book) 0.90
(Δ +0.50); the same-way concentrator → Δ ≈ 0. The signal separates exactly
what the operator described.

## Phase B — the STEER (design, next build)

Three mechanisms, all derived from measured state (no new manual numbers),
all pricing/priority — never refusal:

1. **Quote-side P(book) component in the skew** (the peak-component pattern:
   off-hot-path, generation-stamped, UNKNOWN→neutral-zero). Inputs: snapshot
   per-game tail SHARES (deviation from uniform = the measured concentration —
   a perfectly diversified G-game book has share ≈ 1/G, so the steer needs no
   target number) + the candidate's mutex-aware per-game alignment (already
   wired). Aligned-concentrating on a high-share game ⇒ widen ∝ (share −
   1/G); anti-aligned/variance-adding ⇒ rebate ∝ the same. Magnitude scale =
   a fraction of the tier's own markup (redistributing edge we already
   charge, not inventing a dollar knob); outer bounds stay the existing skew
   clamps + free-money clamp.
2. **Confirm-side pay-up**: arm the certified-hedge lane
   (`allow_negative_ev_hedge` + `hedge_cost_budget_cc`, built, default OFF) —
   pay EV ONLY for fills that certify POST-tail ≤ PRE on common random
   numbers. The budget derives from the measured lopsidedness (tail-share
   deviation × the night's accumulated edge), so a balanced book pays
   nothing and a one-way book pays up for exactly the offsetting flow the
   operator wished existed on DET.
3. **Relight neutrality falls out**: the same steer reads committed
   concentration (which survives cancel-all), so post-relight refill prices
   against the already-concentrated side automatically.

**Derivation-before-arming rule:** the Phase-A shadow record (delta_p_book +
tail shares on real flow) is the measurement the Phase-B magnitudes derive
from — structural evidence, never a P&L window. Phase B ships shadow-first
(the `inventory_skew_shadow` pattern), then arms.

**Also queued in P1 (unchanged):** per-combo 1% anchor made real at the
reservation path (accumulated per-structure — enforcement repair of an
existing anchor, not a new number); entity axis (player/team, all leg
families); same-player markup adder.

## NEXT STEPS

- **Me (next build):** Phase B1 skew component (shadow mode) + B2 derived
  hedge budget design note; adversarial review before any arm.
- **Operator:** none owed for Phase A (shadow-only). Phase B arming will come
  with a validate-can-quote + before/after throughput check as usual.
- **Standing:** bot DOWN (KILL set); Phase A logging activates automatically
  at the next relight.
