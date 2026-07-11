# Containment universe sweep — every implication shape on the board, exchange-probed + engine-classified

**Date:** 2026-07-11 · **Operator directive:** "test every containment-adjacent
possible combo using the Kalshi API — EVERYTHING — then show what we price,
list what we don't, and how to fix it." · **Method:** taxonomy from the real
series/line universe (18.03M-RFQ tape scan + public API + docs.kalshi.com) →
demo-only constructibility probes (86 constructions, exact Jul-10 methodology)
∥ live-classifier disposition under BOTH main (`e1adf82`) and the collapse fix
(`f7f97b9`), bare AND embedded, + tape frequency. **Evidence persisted:**
`docs/calibration/containment_probe/{taxonomy,exchange_matrix,engine_matrix}.json`
(probe log + scripts in job-tmp `containment_probe/`).

## The numbers (set arithmetic, no residuals)

- **50 shapes** (53 with variants) across soccer(32)/MLB(11)/WNBA(2)/UFC(3)/tennis(1)/golf(1);
  every unpaired leg type carries a written justification (taxonomy meta).
- **194 shape×side-mix cells probed on the exchange: 59 ALLOWED / 118 BLOCKED / 17 UNPROBEABLE**
  (MLB direct probes blocked by demo's finalized events — status gate precedes
  the semantic check, proven by control probe; tape evidence substitutes).
- **Of 56 reachable (ALLOWED) engine-evaluated cells: 45 price under the fix, 11 gaps**
  (list below) — plus **2 reachable cells priced WRONGLY** (impossible mixes
  priced as possible: S8-yn, S34-yn), which the 45 conceals without this note.

## The 11 gaps (exchange allows them; we don't fully price them) + fixes

| # | shape (cell) | tape | fix class | the fix |
|---|---|---|---|---|
| 1 | **S12-window: soccer "win but NOT by N"** (ml YES + spread NO) | **637 combos / 1,091 prints — the big one** | **EXACT, free** | extend the ml\|spread containment family (already live for MLB) to soccer: P(win) − P(win by ≥N), band arithmetic, no measurement |
| 2 | S35–S40 same-player windows **embedded** in 3-4-leg combos ("no HR but ≥1 hit" + more legs) | 2,337 combos / ≤5 prints | code, no new measurement | extend the collapse machinery to measured-conditional pairs (super-leg p from the 142-cell conditional table, same plan mechanism) |
| 3 | S41: TB ⟹ HRR≥1 (same batter), all mixes | 203 combos / 0 prints | **EXACT, trivial** | any total base ⟹ a hit ⟹ HRR≥1: add ('tb',N,'hrr',1)=1.0 exact cells to SAME_PLAYER_CONDITIONALS |
| 4 | S2-window: "BTTS completes after halftime" (1H-BTTS NO + FT YES) | 17 combos / 16 prints | measurement | `btts\|first_half_btts` pair prior — joins the queued soccer pair-prior pass (same corpus as corners\|advance etc.) |
| 5 | S44-window: WNBA "win but not cover" | 0 on tape | EXACT, free | same soccer/MLB ml\|spread family extension covers it |

Priced-but-WRONG (impossible mixes priced as possible — worse than declining):

- **S8-yn** (1H-spread-N YES + FT-total-under NO): **constructible TODAY (demo-minted,
  1 real tape print)** — a LIVE FARM (combo settles NO always); engine prices it
  +0.52 copula. Fix: one impossibility rule (win 1H by N ⟹ ≥N goals FT,
  regulation scopes nest) → IMPOSSIBLE-farmable.
- **S34-yn** (MLB spread YES + total-under NO): 26 tape combos priced +0.13
  copula when V_true=0. Validator now blocks NEW ones (tightened since these
  minted) but existing markets can re-RFQ. Fix: same impossibility rule, MLB
  scalar policy keeps farmable=False.
- 38 further impossible cells price via copula but are **exchange-BLOCKED**
  (unreachable) — defensive impossibility rules desirable, zero-flow priority.

## Farm inventory (constructible impossibles, demo-verified 2026-07-11)

S1-yn (win + no-goal), S2-yn, S3-same-line-yn — all recognized IMPOSSIBLE-farmable
by the engine ✅ (4+1 real farm combos on tape). **S8-yn — NEW, NOT recognized ❌**
(the fix above). Caveat: **the validator TIGHTENED between Jul-07 and Jul-11**
(team-corners farm and match-corners inverted bands now block) — farm shelf
life is short; any ALLOWED evidence older than ~Jul-09 is refutable.

## Exchange findings worth keeping

1. `conflicting_leg_outcomes` covers team-entity containments/exclusions +
   BTTS×TOTAL at the implying line + NOGOAL pairs + corners cross-ladder —
   but misses S1/S2/S3-same-line/S8 (idiosyncratic, not principled).
2. Golf finish positions are one ladder ACROSS series (TOUR×TOP5 →
   `duplicated_legs` despite different series) — first observed cross-series
   ladder identity; ×MAKECUT pairs construct and print.
3. Per-event `is_yes_only` kills NO-side mixes for GAME/1H/GOAL/FIRSTGOAL/
   MLBGAME/WNBAGAME/PTS/UFC/tennis/PGA; `size_max=1` blocks all same-event
   pairs except corners/MLB-props/GOAL/PTS/TOP-k (bands constructible).
4. UFC (S46-48) + golf (S50) legs classify LegType.UNKNOWN → flat 0.6 —
   the known post-baseball classification queue, now with exact shape specs.
5. MLB direct constructibility probes remain the standing open item (demo
   listed no live MLB events; S35 strict zero-prints suggests validator
   blocking, unconfirmed).

## NEXT STEPS

- Wire order proposal (operator to confirm): (1) S12 soccer ml|spread family
  [exact, 1,091 prints of flow] → (2) S41 exact cells [trivial] → (3) S8/S34
  impossibility rule [farm + correctness] → (4) S35-40 embedded-conditional
  collapse [code] → (5) S2-window prior joins the soccer pair-prior
  measurement pass. All follow rule-8 flow with the merged containment fix.
- Standing: MLB demo probe when events list; UFC/golf classification at
  their season/board arrival; re-verify farm constructibility before any
  farm attempt (validator tightening).
- Operator decisions owed: the wire order above; whether farms are pursued
  (S1/S2/S3/S8) given shelf-life risk.
