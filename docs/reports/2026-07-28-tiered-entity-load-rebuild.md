# 2026-07-28 — ENTITY AXIS REBUILT TO THE OPERATOR'S TIER TABLE

> "Risk engine = protection not limitation. Those risk caps should be protecting
>  us as intended, right now they're limiting us." — operator

Companion to `2026-07-28-slate-partition-armable-and-failclosed.md` (the other
half of the same commit, `dd3cdbb`). That report owns the SLATE fix. This one
owns the **entity axis**: what was deleted, what replaced it, what it measures on
the live tape, and the ONE decision owed.

---

## 1. What was deleted, and why it could not be salvaged

The prior build's `certify_entity_admission` **never read the key**. Its whole
rule reduced to a closed form in the BOOK's dollar-weighted effective N:

```
L  <  2T / (N_eff - 1)
```

— a pure ticket-SIZE test wearing a diversification label. Proven by execution:
a 1-leg $40 ticket dumping 100% of its premium onto the **already-hottest** key
returned `certified=True / 'diversifying'`, **byte-identical** to $40 spread
across four fresh entities. It also relocated per-key protection to the 5%
per-combo ceiling for **every** key, silently moving max single-entity exposure
off the ratified 3%.

`risk/entity_admission.py` was **rewritten from scratch** (nothing salvaged) and
`risk/net_effect.py` deleted. The regression is now pinned by
`TestTheCertificateActuallyReadsTheKey`: identical candidate dollars on `C0`
(cool) / `WARM` (tier 1) / `HOT` (tier 2) must produce **three different**
verdicts, and the hot-key vs fresh-keys pair must disagree.

---

## 2. What replaced it — the operator's table, literally

```
per-entity load, as a PERCENT OF BANKROLL
  < 1%   no action
  1-2%   tier 1 widen
  2-3%   tier 2 widen
  > 3%   DECLINE
```

| piece | where it lives | new number? |
|---|---|---|
| DECLINE line 3% | `risk.entity_loss_frac` (already ratified) | no |
| size ceiling 5% | `risk.per_combo_loss_frac` (already ratified) | no |
| tier-1 line 1% | `ENTITY_TIER1_FRAC` | **yes** — policy anchor |
| tier-2 line 2% | `ENTITY_TIER2_FRAC` | **yes** — policy anchor |
| everything else | derived from live bankroll + live book | no |

Two constants total, both NORTH-STAR layer 2 (risk appetite stated once). Nothing
in the file is a knob: the tier lines are `frac × live bankroll`, so the same $30
load is tier 1 at a $2,000 bankroll and tier 2 at $1,200 — pinned by
`test_tier_scales_with_bankroll_and_is_never_hand_set`.

### The graded widen (tier 1 ≠ tier 2, measurably)

```
w = min(prior_load, wall) / wall      exact Fraction, 0 below tier 1
```

so tier 1 always lands in `[1/3, 2/3]` and tier 2 in `(2/3, 1]` — the bands
cannot overlap, by construction, and the test sweeps both ranges. **It reads the
PRIOR load, never the candidate's own size**: a fresh pitcher nobody has bet
weighs exactly 0 however big the incoming ticket is. That is the operator's own
arithmetic ("12 games, ~2 main pitchers each, 24 different props — we're missing
out if we widen after 2-3 pitcher combos come in"). Ticket SIZE is already priced
by the frozen markup tiers and bounded by per-combo; it must not be charged twice.

### NET across the combo, in dollars

> "it should be judged based on other legs as well, if they diversify further or
>  concentrate other legs we have"

* **widen** = the DOLLAR-WEIGHTED MEAN of the per-key weights, never `max`. Same
  $30 of premium, two shapes: riding `WARM` alone prices at `1/2`; `WARM` plus
  four fresh entities prices at `1/10` — **exactly a fifth**, not the worst leg.
* **admission** = `diversifying_cc > concentrating_cc`, i.e. the candidate's
  dollars must land more on COOL keys than on already-loaded ones. Flipping the
  shape flips the verdict (`net_concentrating`).

---

## 3. The decision the brief asked for: SIZE vs ACCUMULATION

Measured on the live tape (400,000 decisions, 99,131 entity refusals carrying a
detail string):

| fact | measured |
|---|---|
| candidate's OWN premium alone clears the wall | **72.2%** (71,550) |
| breach-key PRIOR tier = cool (< 1%) | **54.3%** of breach-key instances |
| median ticket REFUSED | **$121.58** |
| live walls at that bankroll | entity **$71.17** (3%) · per-combo **$118.61** (5%) |

A first structure on an empty key is not "every combo riding one player one way
across all games" — that is the accumulation the 3% wall was ratified to stop. It
is a **SIZE event**, and size already has two ratified protections the operator
kept unchanged (per-combo 5%, `skip_size_above_max`). So the tiers split at the
key's **PRIOR** load:

```
prior >= 1% (tier 1+)  ->  ACCUMULATION.  The 3% wall refuses, byte-identical.
                           No certificate can waive it, at ANY size.
prior  < 1% (cool)     ->  SIZE.  The entity axis abstains and PER-COMBO governs,
                           and only when the combo is NET DIVERSIFYING.
```

**THE INVARIANT** (pinned by `test_the_accumulation_wall_STAYS_at_three_percent`):
an entity may exceed the 3% accumulation wall only by the footprint of a **single
structure**, never above what one structure is allowed to carry; and once it is
loaded, no further structure gets in past 3%.

### Why the tier sidesteps the share trap

An honest per-key SHARE test admits nothing, ever: `(E+L)/(T+L) > E/T` whenever
`E < T`. That is why the deleted `net_effect.py` inverted and admitted 0 of 140.
The tier is not a share — it is the entity's own load against an **absolute**
percent of bankroll, so a cool key stays cool no matter how the book's total
moves, and the test has a fixed point to bite on.

---

## 4. Live-tape measurement (400,000 decisions, book 123 positions / $1,513.35)

```
ENTITY refusals=99,131   ADMITTED=5,802 (5.9%)   premium released=$482,628.92
  refusal cause census:
    over_size_ceiling     32,669    accumulation_tier1   26,480
    accumulation_tier2    15,952    net_concentrating     9,192
    accumulation_over_wall 4,439    key_not_in_candidate  4,597
```

Why "only" 5.9% — the honest decomposition of the same 99,131 refusals by the
candidate's own size:

| band | count | what it means |
|---|---|---|
| candidate ≤ entity wall ($71.17) | 27,581 (27.8%) | real ACCUMULATION — refused, unchanged |
| **(3%, 5%] band = $71.17–$118.61** | **20,381 (20.6%)** | the band where the entity cap was acting as a **second, 40%-tighter per-combo cap**. This build governs exactly here. |
| candidate > per-combo ($118.61) | 51,169 (51.6%) | above the ratified SIZE wall — **per-combo refuses these anyway**, and this build correctly does not release them |

So the release is precisely the band the operator's spec targets, and nothing
else. **Largest released candidate $116.96 vs the $118.61 per-combo wall —
INSIDE the ratified size wall, empirically, over the whole window.**

### Does admitting more lower effective N? No.

| | premium | ticket N_eff | peak ticket | worst ENTITY key |
|---|---|---|---|---|
| today | $1,513.35 | 53.80 | $81.47 | $134.57 |
| serial fill sim (61 accepted, caps re-bind at every fill) | $6,713.22 | **90.91** | $111.00 | **$134.57 (unchanged)** |
| upper bound (every released quote fills at once) | $484,142.28 | **5,660.3** | $116.96 | **$117.45** |

Dollar-weighted effective N **rises** on both counterfactuals, the peak ticket
stays inside per-combo, and the worst entity key is not moved by any released
fill (the $134.57 is a pre-existing legacy position).

Combined with the slate fix, projected send rate **2.371% → 10.617%** (upper
bound — released decisions still face the candidate gate, EV and the write
budget).

---

## 5. Blast radius — what is byte-identical

| check | result |
|---|---|
| shadow parity vs `9a93925`, 400 randomised cases / 800 breach rows | **byte-identical** (`cmp` clean) |
| `pricing/markup.py` sha256 | `b1b91c77…1351c18e` — **identical to HEAD** |
| `risk/skew.py` sha256 (the widening path) | `a27eb9aa…7e6d20cb` — **identical to HEAD** |
| per-combo cap detail, all 4 flag states | one distinct string (`test_per_combo_cap_is_byte_identical_in_all_flag_states`) |
| throughput, both levers off | 2,660 µs/call vs **2,595 µs/call at HEAD** — no regression |
| throughput, ARMED | 3,081 µs/call (the cost is only paid once a key has breached) |
| suite | **3,347 passed, 0 failed** |
| `tools/vitals/gate` | **8/8 GREEN** (fast) · **1/1 GREEN** (pre-ship) |
| mypy / ruff on the touched files | clean (the 6 mypy + 3 ruff errors in the tree are pre-existing, in `pricing/engine.py`, `ising_amm.py`, `exchange/ws.py`, `marketdata/metadata.py`) |

**The graded widen is NOT wired into the price path in this commit.** It is a
pure function with tests; wiring it (and deleting the family axis + the `peak`
term, per `2026-07-27-widening-spec-operator.md` §1 and §4) is the next build.
That is why shadow can be, and is, byte-identical.

---

## 6. DECISION OWED — read this before arming

Arming `entity_admission_armed` certifies a **previously-COOL** entity up to the
per-COMBO ceiling for a **single structure**. On the live bankroll that is a
**$71.17 → $118.61** move on max single-entity exposure from one ticket — a 3% →
5% move, stated loudly rather than silently, and bounded three ways:

1. a **warm** key (≥1%) is never certified at any size, so accumulation across
   structures still refuses at **3%**;
2. the combo must be **net diversifying in dollars**;
3. the released band is capped by the already-ratified per-combo 5% — measured
   max released $116.96 < $118.61.

The brief's own justification for it: *"the 77.9% case is NOT a concentration
event — it is a SIZE event on a fresh key … the size protection already exists as
per-combo (5%) and `skip_size_above_max`."* If the operator rejects the 3%→5%
move on principle, the fix is to leave `entity_admission_armed: false` — the
entity axis then keeps behaving exactly as today, and the slate lever alone still
carries most of the projected release (88.8% of slate refusals admitted).

---

## NEXT STEPS

* **Operator — decision owed (1):** arm `entity_admission_armed: true`, knowing
  it moves max single-entity from 3% to per-combo 5% **for one structure on a
  previously-cool key only**. Recommended sequencing: run
  `entity_admission_enabled: true` (read-out only) for one slate first and read
  the `entity_tier_admission` lines — every row carries `prior_cc`, `add_cc`,
  `prior_tier`, `post_tier`, `diversifying_cc`, `concentrating_cc`,
  `would_admit`, so the counterfactual is countable in dollars before anything
  changes.
* **Operator — decision owed (2):** arm `slate_partition_armed: true` (the
  measure repair the operator already ratified verbally: *"We need to fix the
  slate count it shouldn't be over counting"*). `slate_loss_frac` stays 0.65 —
  the ruler was repaired, the wall did not move. See the slate report.
* **Next build (owner: next session)** — wire the graded widen into PRICE: delete
  the family widening axis, add the `peak` arming flag and default it off, and
  multiply the existing entity widen budget by `combo_widen_weight`. Gate: markups
  byte-identical by sha256, plus proof that a fresh pitcher's leg is no longer
  widened by a heavy family.
* **Owner: whoever arms** — re-run
  `tools/diagnostics/replay_admission_fixes.py` after the first armed slate and
  compare the realised send rate against the 10.617% upper bound; the gap is the
  candidate gate / EV / write budget, not these caps.
