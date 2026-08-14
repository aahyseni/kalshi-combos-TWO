# 2026-08-14 — Marginal-gate repair BUILT + REARMED (operator: "Go, build and rearm and restart the bot")

Both defect repairs from the fill-collapse forensics shipped as `93ad553`,
all gates passed including the NEW quote-production counterfactual, bot
restarted **19:31 ET** — first quotes within ~3 minutes (the morning boot
took 4h52m), zero halts, zero reneges.

## What shipped

**Fix A — the marginal KILL/ruin criterion (D2), quote-time + confirm-time.**

The 8/1 criterion `dES99 ≤ dEV × CP-lower P(accept)` had two derivation
errors, both now fixed in ONE pure function the live sites and the
counterfactual tool share (`rfq/eviction_value.py::marginal_tail_admit`):

1. *Conditioning*: at a fill-conditional decision the EV **and** the tail
   both materialize only if the quote fills — P(accept) cancels. (It stays
   in `diversity_key` for EVICTION, where it is correct: a resting quote's
   reservation holds capacity unconditionally.) The 8/1 gate reused the
   eviction metric where its conditioning assumption doesn't hold.
2. *Units*: `dES99` is conditional-on-tail (mean of the worst 1% of
   nights); EV is unconditional. The tail side must carry its own
   probability mass: **admit iff `dES99 × ES_TAIL_ALPHA (0.01) ≤ dEV`** —
   an ES-definition fact, not a tunable.

Confirm-site twins (`sim/book_risk.py`, kill + ruin) — the renege class
(6/10 accepts declined today, 3 with positive EV) gets two DERIVED
allowances, no new constants: a **noise quantum** (a shared-CRN delta
within `ruin_prob_ci_z / n_samples` is inside the estimator's own
resolution — one flipped MC path is not a measured raise) and **EV
pricing** (a real raise admits iff `Δp × KILL/ruin-line ≤ admission_ev` —
the marginal expected tail-cost at the anchored line vs the candidate's
own edge). Whale-scale raises fail both and still decline; certified
reducers still always admit.

**Fix B — directional cap candidate scoping (D1).** `risk/limits.py`
check (4) now judges only the games the candidate touches (the 8/1
constitution; live-proven defect: a pure 7-leg MLS parlay refused for an
MLB game's standing direction). UNKNOWN event ticker ⇒ full sweep (fail
closed); book-only callers (audits/eviction) byte-identical; touched-game
behavior byte-identical.

## Gates (all PASS, in order)

| gate | result |
|---|---|
| Unit suite | 3,675/0 (4 new confirm-site pins, 2 rewritten boundary pins, 4 new scoping pins — no pre-existing test varied either discriminating variable, the vitals-gate lesson again) |
| mypy strict + ruff | clean |
| Vitals fast | 8/8 GREEN (60.3s) |
| **Quote-production counterfactual (NEW, now mandatory)** | `tools/diagnostics/marginal_gate_counterfactual.py` replayed **20,478 real marginal-form refusals** from today's store (14:20–17:40 ET): OLD criterion **0.00% admit** (the literal 100%-decline gate), NEW criterion **91.27% admit**, top ~8.7% (genuine tail-concentrators, des99 p90 $351) still refuse |
| Pre-ship vitals (quiet stop window) | 1/1 GREEN (546.3s) |

Stop was clean: all processes down, orphan pool workers reaped, **1
resting quote cancel-all'd** (a down bot cannot honor accepts — renege
prevention). No yaml changes: all armed flags (kill_anchored, seed, ruin
0.05, structure 1% armed, direction shadow) stand; the repair is
criterion code only.

## First-window verification (19:31–19:40 ET)

- **79 quote_sent in ~7 min** (morning boot: 0 in 4h52m; broken-day
  average 3/min). p_ruin at boot 0.233 — deep in the exact over-budget
  regime that bricked all day — and the repaired gate admits through it.
- **Decline mix is the designed shape**: structure cap 461 (TOP wall =
  the operator's 1% whale rule doing its job), entity 329, cvar **175**
  (was 11,212 per 10 min this afternoon — now refusing only genuine
  concentrators), size 173, per-combo 117, utilization 7 (headroom real).
- Zero halts, zero `kill_marginal_raises`/`ruin_marginal_raises` events,
  preflight green, `startup_reconciled leftover_quotes=0`.
- Book at boot: 44 positions, det $2,580 vs backstop $3,115 ($535
  headroom), p_kill_night 0.234, day realized +$51.44 (log tracker).
- Persistent monitor armed: accepts, executions, halts, any marginal
  renege.

## Blast radius

`marginal_tail_admit` (new pure fn) + the two §(8a)/§(9) call sites + the
two confirm-site comparisons + check (4) scoping. Pricing/fair/markup
untouched; structure/per-combo/entity/size/slate/det-backstop walls
untouched; eviction's `diversity_key` untouched (its P(accept) asymmetry
is correct there). The det-max 0.70B backstop remains the only level gate
— the constitution is now actually true in code.

## NEXT STEPS

- **Me (tonight):** watch the monitor through the MLB slate — fills,
  accept→confirm behavior (expect the renege class gone), quote-rate
  vs the 17,840/5.4h overnight baseline; morning readout with fill count,
  biggest-ticket check (must be ≤ $45), and P&L truth.
- **Me (owed):** direction-net shadow distribution read → propose ARM
  value; recorder WAL-backlog recheck; Store.open busy_timeout fix;
  exchange /portfolio cross-check of the 88 open rows.
- **Operator:** nothing owed tonight. Open when you want them: per_combo
  5%→2% belt-and-braces; structure-hash keying (closes cross-ticker
  stacking, post-freeze); slate 65→80 (+$224).
