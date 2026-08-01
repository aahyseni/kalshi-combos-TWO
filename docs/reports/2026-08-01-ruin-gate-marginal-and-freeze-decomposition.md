# 2026-08-01 — MARGINAL RUIN GATE + the evening-freeze p_ruin decomposition

**State: BUILT DARK (`ruin_gate_marginal` default False = byte-identical;
merged the `marginal-kill-gate` machinery verbatim); suite 3,582/0; vitals
fast 8/8 GREEN 22.6s + pre-ship 1/1 GREEN 13.5s at the worktree; arming lines
STAGED in the live yaml (ALL flags, ONE restart — checklist at the bottom).**

| item | WRONG / measured | FIXED / verdict |
|------|------------------|-----------------|
| The freeze | `skip_portfolio_ruin` from **20:17:42Z**, 219,167 rows / 151,169 distinct candidates to 22:28Z (1,044/5min at the measured moment), `quote_sent` = 0 — §(9) is a LEVEL gate on the standing (sunk) book | **MARGINAL RUIN GATE built** — same `KillMarginalCandidate` machinery, criterion, and flag family as the marginal KILL gate (reused verbatim, branch merged) |
| The 29.9% itself | The smell (ES99 $692 vs "~$825 ruin distance") assumed the wrong distance: the REAL distance the MC saw was **~$140–360**, not $825 | **NOT a numeric defect** — decomposition below; the arithmetic is correct under its stated convention; ~$550–680 of RESERVED premium write-off is the dominant term |
| Ruin anchor freshness | `daily_ruin_anchors` DB table last row **2026-07-16** (stale) | **NOT in the live path** — vitals `derive.py` fallback only. Live floor = 0.70 × `risk_bankroll` from the in-memory `BalanceTracker`, re-anchored at the 18:13:36Z restart poll (fresh; backstop logs confirm to the cc) |
| Interim stand-down | yaml `portfolio_ruin_prob_budget: "1.0"` (operator, ~17:00 ET) — running bot still enforces 0.05 (booted 14:12 ET) | Staged arming REVERTS to `"0.05"` **with** `ruin_gate_marginal: true` at the same restart |
| Level-gate sweep | Full enforcement-site sweep below | ONLY the ratified backstop-class gates survive as levels; every candidate-judging gate now has a marginal form (built or already shipped) |

## 1. Decomposition of the 29.9% (reproduced offline with the real code)

At the 22:28:12Z snapshot (`p_ruin 0.2994 == upper`, book 33 modeled
positions, det-max $1,692.06, ev −$73.70, es99 $692.21):

```
bankroll B          $2,735.87   = logged det_max_backstop_cc 19,151,071 / 0.70
                                  (min(SOD $2,769.26 @18:13Z restart poll,
                                       cash + 1.0*PV) -> deployed-aware term; FRESH)
ruin floor          $1,915.11   = 0.70 * B          (Reading A, 5% budget)
cash                $1,041.37   reconstructed to the cent from the 18:13:36Z
                                account_standing ($1,596.10) - fills premium
                                $582.37 - fees $1.12 + settle credits $28.76
equity basis        = cash + MODELED cost basis ONLY
held premium        $1,692.06   (ledger as-of-T; == det-max exactly)
modeled premium     ~$1,014-1,140  (33 of ~72 held positions sampled)
RESERVED write-off  ~$550-680   <- THE DOMINANT TERM: positions whose leg
                                marginals were unavailable at 22:28 (in-play /
                                finished-undetermined day games; the settled-
                                resolution loop was STARVING: 5,281
                                settled_resolution_pending + starved events)
                                are excluded from the equity basis = treated
                                as 100% lost NOW, upside excluded
ruin distance D     ~$140-360   = equity - floor  (NOT ~$825)
```

Component shares of the 0.2994 (offline repro, real `compute_book_risk`,
tape-marginal proxy — repro reads 0.0677–0.2482 depending on the marginal
window; live had fewer modeled → higher):

| term | contribution |
|------|--------------|
| RESERVED premium write-off (equity basis) | **~0.25–0.30 → crediting it back reads p_ruin 0.0000** — effectively the whole gate margin |
| Marked in-play losses (ev −$74 shifts the P&L distribution) | a few points at D≈$170 (distribution centering, already in the MC book — no double count: cost-basis equity carries no marks) |
| Drawdown-from-anchor (floor fixed at 0.70·B while equity fell) | ≤ 1–2 points (B moved $2,769→$2,736 deployed-aware; behaves as designed — "tightens as we draw down") |
| Wilson/band | **exactly 0** (`ruin_prob_ci_z = 0.0` ⇒ upper == p̂; 0.2994 == 0.2994 on the tape) |
| Open-book MC tail at the full-health distance ($909 if nothing reserved) | ~0.000 (VaR99 of the modeled book ≈ $493–616) |

**The ES-vs-distance "inconsistency" resolved**: es_99 is computed on the
MODELED book only, while the ruin distance had ~$600 of held premium written
off — the two numbers describe different books. P(loss ≥ D) = 0.2994 with
D ≈ the 70th loss percentile is arithmetically consistent (ES99 $692 ≥ VaR99
≥ VaR70 ≈ D). **Honest verdict: no numeric defect; the freeze mechanism is
the LEVEL form itself + the conservative reserved write-off amplified by
settled-resolution starvation** ("known outcomes never read UNKNOWN" is owed
a separate ops fix — see NEXT STEPS). Measured natural experiment: three
in-flight pregame fills that landed at 22:31:16Z moved p_ruin 0.2994 → 0.264
→ 0.1649 within 90 s — the refused flow was the cure, not the disease.

## 2. Marginal-form sweep — every armed enforcement site in `LimitChecker.check`

Tonight's tape (18:12Z boot): entity 293k, ruin 235k, per-combo 99k,
utilization 86k, size 64k, slate 8.0k, cvar 7.9k, max-open-quotes 960,
directional 8, mass-acceptance 2.

| gate | judges | verdict |
|------|--------|---------|
| §(9) `SKIP_PORTFOLIO_RUIN` | LEVEL of standing book (candidate ignored) | **MARGINAL FORM BUILT (this change)** — quote-time: over-budget ⇒ admit certified reducers + `des99 <= dEV × CP-lower P(accept)`; confirm: post-p_ruin must not rise vs pre on shared CRN; under-budget byte-identical |
| §(8a) tail-prob `SKIP_PORTFOLIO_CVAR` | LEVEL of book envelope | **Marginal machinery SHIPPED** (`kill_gate_marginal`, merged branch `b72f141`) — arms with `kill_anchored_book_gate` at this restart |
| §(8a) ES fallback (`governing ES > cvar_frac·B`) | LEVEL | **Survivor, justified**: runs ONLY when the tail-prob form cannot (flag off / legacy snapshot without envelope) — it is the fail-closed path for an unmeasurable envelope; UNKNOWN never a free pass |
| §(8b) `SKIP_PORTFOLIO_DET_MAX` (0.70B armed / 0.36 today) | LEVEL | **Survivor by ratification**: THE model-free backstop (operator "Reading B", 2026-08-01); absoluteness property-tested (no marginal bypass) |
| unusable-snapshot branch (both §8 axes) | staleness | **Survivor**: fail-closed on an unmeasured book — constitutional |
| `SKIP_BANKROLL_UNAVAILABLE` | staleness | **Survivor**: fail-closed |
| `SKIP_UTILIZATION_BACKSTOP` (Σ gross notional > multiple×B) | book+candidate gross | **Survivor, justified**: model-free absolute-notional backstop (same class as det-max backstop); auto-scales with bankroll. (Fired 86k tonight — flagged for operator review, out of scope here) |
| `HALT_HARD_TRIP` / `HALT_DRAWDOWN` | realized give-back | **Survivors**: the ratified KILL/drawdown policy anchors themselves |
| per-combo / game / entity / slate / directional / size / mass-acceptance / max-open-quotes | candidate's OWN axis bucket (book+candidate accumulation) | **Survivors**: the refusal layer; a candidate is refused only for ITS bucket — no cross-bucket level can freeze all quoting; already marginal in the constitutional sense |

## 3. What was built (one flag family, defaults = today, byte-identical proven)

Reused **verbatim** from `marginal-kill-gate` @ `b72f141` (merged): the
`KillMarginalCandidate` input object, `allocate_des99_cc` per-game CVaR
allocation, CP-lower acceptance credit at the ratified alpha, the CRN
pre/post two-regime pattern, the lifecycle input builders, the reservation
threading. New (mirror sites only):

* `RiskLimits.ruin_gate_marginal` (+ `RiskConfig`, threading) — arms the ruin
  axis independently of the KILL flags.
* `risk/limits.py` §(9): over-budget + input supplied ⇒ the diversity-key
  admission test (identical criterion/detail format to §8a's marginal form);
  no input / disarmed ⇒ level refusal byte-identically.
* `sim/book_risk.py` `_candidate_gate` check (4): PRE over the ruin budget ⇒
  admit iff certified reducer (POST unclamped governing tail ≤ PRE, shared
  CRN) or post p_ruin ≤ pre p_ruin; PRE under ⇒ today's rule byte-identical.
  Pre/post p_ruin already ride the existing `_TailAxes` on the same CRN
  sample and the same equity/floor — no new vector math.
* `rfq/lifecycle.py`: `_ruin_over_budget()` regime probe (reads the same
  snapshot + budget §(9) enforces — probe and cap cannot disagree); the
  marginal-input builders now build when EITHER armed axis is over its
  budget; `ruin_gate_marginal` threaded through `CandidateBookRiskInputs` →
  pricing pool → `evaluate_candidate_book_risk`.

**Throughput**: disarmed = zero added work on the hot path (the new branch is
inside the already-over-budget refusal branch; lifecycle guards short-circuit
on the flag). Armed under-budget = one float compare. Vitals fast (includes
the throughput checks) 8/8 GREEN.

**Property tests** (`tests/test_ruin_gate_marginal.py`, 15; + the branch's 18
all green): over-budget diversifier ADMITS on an EMPTY acceptance tape
(VALIDATE-CAPS-CAN-QUOTE), concentrator REFUSES with the marginal detail,
boundary exact at `des99 == ev×p_lower`, certified reducer always admits,
UNKNOWN (no input / no pre-sample) never admits, under-budget byte-identical
flag-on/off, flag-off byte-identical even with an input supplied, unusable
snapshot fails closed before any regime, det-max backstop absolute under both
marginal flags, ruin axis arms independently of the KILL flags, confirm-path
two-regime (admit-on-non-raise / refuse-on-raise / certified exemption /
under-budget unchanged).

## 4. Counterfactual on the frozen tape (`tools/diagnostics/ruin_gate_marginal_counterfactual.py`)

Windows: refusals [20:17:42Z first refusal → 22:28:12Z snapshot); flow
population = evening-slate RFQ arrivals (the store recorded ZERO arrivals in
[20:05, 22:28) — intake gap, see open item — so the surrounding arrivals are
the classifiable proxy; refused candidates were reprices of that same slate).

```
refusals: 219,167 rows / 151,169 distinct candidates  (1,044/5min at the peak)
stored candidate EV on refusals: median $0.82, mean $1.42
flow classified: 157,236 RFQs -> ADMIT (diversifier, dES99<=0) 884 = 0.6%
                                 refuse (tail-game concentrator, empty-tape
                                 EV credit 0) 156,352
projected: ~1,232 of the 219,167 refusal rows admit; ~6 of the 1,044-row
5-min window
trajectory (rebuilt book, tape-fair zero-edge pricing, seed-fixed):
  frozen (actual)      +0    fills            p_ruin 0.0677  ev  -$8.41  P(KILL) 0.0860
  12.7%-accept         +13   fills +$765.91   p_ruin 0.1872  ev -$14.01  P(KILL) 0.1940
  all-fill worst case  +110  fills +$6,062.51 p_ruin 0.5272  ev -$22.71  P(KILL) 0.5290
```

Read honestly: **day-one (empty acceptance tape, CP-lower = 0) the marginal
ruin gate un-bricks quoting** — real quotes CAN go out (884 diversifiers;
the level form sent zero) — **without opening the floodgates**: 99.4% of the
same-slate concentrating flow still refuses until measured acceptance buys EV
credit, and the trajectory shows why that fail-closed floor is right (the
refused whale-sized same-slate flow at zero edge would have pushed p_ruin UP).
Caveats: the rebuilt baseline reads 0.0677 vs the live 0.2994 (the tape-
marginal proxy models 64 positions where the live feed modeled 33 — the
reserved split is the sensitivity); trajectory prices at zero-edge
independence fair (conservative); the live 22:31 natural experiment (3 real
pregame fills → p_ruin 0.2994→0.1649) shows real +EV pregame flow behaves
BETTER than the synthetic worst case.

## 5. Gates run (verbatim tails)

```
suite (worktree):   3582 passed, 3 deselected in 265.81s (0:04:25)
vitals fast:        8/8 vital signs GREEN   (GATE PASS)   total 22.6s
vitals pre-ship:    1/1 vital signs GREEN   (GATE PASS)   total 13.5s
new property tests: 33 passed in 9.85s (15 ruin + 18 kill, both files)
```

Self-containment proof (fresh scratch worktree at the final commit): recorded
in the follow-up docs(gate) commit.

## 6. ARMING CHECKLIST (operator wants this restart FAST — ONE restart, ALL flags)

Pre-arm (2 min):
1. `git -C C:/Users/aahys/kalshi-combos-TWO pull` (fast-forward to the merged head).
2. Live yaml `config/prod-live-wc.local.yaml` — apply the staged stanza (bottom of file):
   ```yaml
   risk:
     kill_anchored_book_gate: true      # ratified KILL anchor, re-armed ...
     kill_gate_marginal: true           # ... as the MARGINAL form
     ruin_gate_marginal: true           # MARGINAL RUIN form (this build)
     portfolio_ruin_prob_budget: "0.05" # REVERT the interim 1.0 stand-down
   ```
3. `.venv/Scripts/python.exe -m tools.vitals.gate` → must be 8/8 GREEN (fast, ~20s).
   (pre-ship already 1/1 GREEN at this head; re-run only if anything else changed)

Arm (operator action):
4. STOP_BOT.bat → START_BOT.bat (the one restart; never mid-slate if avoidable).
5. First 10 minutes watch (P(book) reporting discipline): `book_risk_snapshot`
   p_ruin present; `risk_audit` shows `(marginal form)` details ONLY on
   concentrator refusals; ANY `quote_sent` > 0 confirms the un-brick
   (VALIDATE-CAPS-CAN-QUOTE — a cap that cannot produce a quote is a NO-SHIP).
6. Confirm the det-max backstop line logs at 0.70B and `skip_portfolio_det_max`
   still fires if the book is over it (backstop absolute).

Rollback: set the three flags false + budget back to "1.0" interim (or leave
flags true and only budget "1.0" — the marginal form is inert under budget).

## NEXT STEPS

* **Operator**: arm per checklist (one restart); decide the
  `skip_utilization_backstop` review (86k refusals tonight, out of scope here).
* **Owed (separate build): settled-resolution starvation** — the reserved
  write-off that manufactured most of the 29.9% is amplified by
  `settled_resolution_pending/starved` (5,281 events): determined legs must
  resolve to facts promptly ("known outcomes never read UNKNOWN"). Also worth
  ratifying: whether the ruin equity basis should carry reserved premium at a
  measured floor instead of 0 (mechanism repair, not a knob).
* **Owed (investigate): RFQ-intake gap** — the rfqs store recorded ZERO
  arrivals in [20:05, 22:28Z) while risk_audit kept pricing reprices; either
  the ws-shed deafness recurred in another guise or the store writer starved
  (568 `store_writer_checkpoint_failed`). Report to follow.
* **Fleet**: after arming, re-run the counterfactual against the LIVE armed
  tape to grade the day-one admit fraction and the acceptance-tape ramp.
