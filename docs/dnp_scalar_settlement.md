# DNP / scalar leg settlement — economics & handling spec (combo NO-seller)

**Status:** reference + implementation spec for `pricing/`, `sim/`, `risk/`.
**Audience:** anyone touching combo fair-value, the Monte-Carlo engine, or
exposure/settlement. **Rule this doc enforces:** do **not** encode the common
shortcut "a DNP helps the seller." It is wrong. A DNP is approximately
**EV-neutral** and mainly **compresses variance**; the sign depends on `s` vs
`p_i`. Any code that treats a DNP as free seller edge is a bug.

> A DNP (did-not-play) leg is a player-prop leg whose player is scratched/injured/
> benched. Instead of settling binary {0, 1} it settles to a **scalar `s ∈ [0,1]`**
> (≈ its last traded price — **VERIFY per series**, see §7). "Scalar leg" is the
> same math with an intrinsically non-binary settlement.

## Scope & priority (2026-07-09) — VERIFIED real for soccer scorers

Kalshi's own market rule for a soccer anytime-scorer (verbatim, Mbappe 1+,
FRA v MAR 2026-07-09): *"If a player is active but never enters the game, the
market settles to the last fair market price before game start. Once a player
enters the game, the market settles based on the player's goals scored."*

So the scalar/DNP case **IS real** for our soccer scorer legs — **not moot** — and
an earlier "settles NO on a no-show" assumption (both operator's and this doc's
hedge) was **wrong**:
- Player **enters** (plays any time, even ~1s) → clean binary 0/1
  (injured-right-after → clean NO "didn't score").
- Player **active but never enters** (unused substitute) → settles to
  **`s` = last fair market price before game start** — exactly the scalar of §1,
  with `s ≈ p_i`.

**Priority is still MODEST, for the right reasons:** featured scorers people parlay
are usually STARTERS (they enter → clean), so the scalar path mainly hits
non-starter/bench picks; and since `s = last fair price ≈ p_i` it is ≈EV-neutral
(§4) — **not** a pricing edge. **The load-bearing requirement is that
settlement/reconciliation MUST handle a fractional `V` (NO pays `1 − V`), because a
scorer-containing combo genuinely can settle fractional.** Build the §8 `risk/`
continuous-settlement handling; the `sim/` draw is nice-to-have; `pricing/` needs
no mean change. We already price the correlation well
(`docs/calibration/results_soccer.md`) — this is orthogonal tail mechanics.

**2026-07-10 MLB amendment:** the baseball settlement audit materially widens
the scalar surface — under MLB's 48-hour rain rule **every** MLB family (not
just props) scalar-settles, MLB prop DNP is **stricter** than soccer's, and the
trigger frequency jumps from ~0-in-5,000 to **~1–2% of game-days** once MLB
combos are quoted. See §7.1; the 2026-07-09 REACTIVE decision is flagged for
operator re-affirmation (NEXT STEPS).

## 1. Convention & setup

We are the **parlay seller**: we sell the YES combo / hold NO. A combo settles to

```
  V = min(1, ∏_i v_i)          v_i ∈ [0,1] is leg i's settlement value
```

Binary legs settle `v_i ∈ {0,1}`; a DNP/scalar leg settles `v_i = s`. As the
seller we collect `(1 − q)` up front and are liable for `V` at settlement, so

```
  per-contract P&L = (1 − q) − V          (q = combo YES price; we want V small)
```

Equivalently: we are long NO, NO pays `(1 − V)`. `V = 0` ⇒ P&L `= 1 − q` (max
win); `V = 1` ⇒ P&L `= −q` (max loss). Because `V` can be **fractional**, our
payoff is **not** binary — this is the whole point.

## 2. Why a leg only matters in one branch

`V` is a **product**: `∂V/∂v_i = ∏_{j≠i} v_j`. If **any** other leg is 0, then
`∏_{j≠i} v_j = 0`, so `V = 0` **regardless of `v_i`** — changing leg i from binary
to `s` changes nothing. Therefore leg i's value affects `V` (and our P&L) **only
in the branch where every other leg settles at 1** (all other legs hit):

```
  Condition on  B = {all other legs hit},   P(B) = ∏_{j≠i} p_j .
  Inside B:  V = v_i   (the product collapses to leg i alone).
```

All analysis below is *inside B*. Outside B the DNP is irrelevant.

## 3. The two branches — worked, then combined

Inside B, compare binary settlement to a DNP that pins leg i to `s`:

**(a) Would-be WIN** (leg i would have hit → contributes 1): binary `V = 1` (combo
fully hits, our worst case, P&L `= (1−q) − 1 = −q`). DNP `V = s`, P&L `= (1−q) − s`.
→ V falls `1 → s`; **our P&L improves by `(1 − s)`**. *(This is the seductive
half — "DNP saved us from a full loss.")*

**(b) Would-be MISS** (leg i would have missed → contributes 0): binary `V = 0`
(combo busts, NO pays the full $1, P&L `= 1 − q`). DNP `V = s`, P&L `= (1−q) − s`.
→ V rises `0 → s`; **our P&L worsens by `s`**. *(This is the half the shortcut
forgets: the leg had a `(1−p_i)` chance to bust the combo and pay us the full $1,
and the DNP scratches that chance.)*

**Combine**, weighting by the within-B conditional probabilities `p_i` (win) and
`(1 − p_i)` (miss):

```
  E[ΔV | B]   =  p_i·(s − 1) + (1 − p_i)·(s − 0)  =  s − p_i
  E[ΔP&L | B] =  p_i·(1 − s) + (1 − p_i)·(−s)     =  p_i − s   =  −E[ΔV | B]
```

**The result:** `E[ΔP&L | B] = p_i − s`. Break-even at **`s = p_i`**.
- `s < p_i` → V falls → **HELPS** the seller (loosely: a favorite/likely leg whose
  scratch price sits below its true hit rate).
- `s > p_i` → V rises → **HURTS** the seller (a longshot whose scratch price sits
  above its true hit rate).

DNP is **not one-directionally good.** Its sign is entirely `sign(p_i − s)`.

## 4. The mean barely moves; the variance collapses

In an efficient leg market the last traded price already *is* the market's hit
estimate, so **`s ≈ p_i`** and therefore **`E[ΔP&L | B] ≈ 0`**. What actually
changes is the *distribution*, not the mean:

```
  Binary, inside B:  V | B  ∈ {0, 1}   Var = p_i(1 − p_i)   (a coin flip)
  DNP,    inside B:  V | B  =  s        Var = 0             (deterministic)
```

A DNP **replaces a coin-flip with a certainty** at ≈ the same mean. Our payoff in
branch B goes from `{1−q  w.p. p_i,  −q  w.p. 1−p_i}` to a flat `(1−q−s)`. **Plain
statement for the code and the desk: a DNP is approximately EV-neutral on the
affected leg and is NOT, by itself, a source of seller edge. It is a
variance-compression event.**

## 5. Worked numbers

3-leg combo, legs A, B, C; C is a DNP-able scorer. `p_A=0.60, p_B=0.50, p_C=0.40`,
last price `s_C = 0.40`. Branch `B = {A∧B hit}`, `P(B) = 0.30`.

| Scenario | V inside B | our P&L inside B |
|----------|-----------|------------------|
| C binary, hits (p=0.40) | 1.00 | (1−q) − 1.00 |
| C binary, misses (0.60) | 0.00 | (1−q) − 0.00 |
| **C DNP → s=0.40** | **0.40** | (1−q) − 0.40 |

`E[ΔP&L | B] = p_C − s = 0.40 − 0.40 = 0.` **Exactly neutral** (s = p). Would-be
win saved `1 − 0.40 = 0.60`; would-be miss cost `0.40`; `0.4(0.60) − 0.6(0.40) = 0`.

**Now break `s = p` (the only case that moves EV):** player scratched on a **stale**
last price `s = 0.55` while true `p_C = 0.40`:
`E[ΔP&L | B] = 0.40 − 0.55 = −0.15`; unconditional `× P(B) = 0.30 → −0.045/contract`.
Symmetric the other way (stale `s = 0.25` on true `0.40`): `+0.045`. **Same
magnitude, opposite sign — an adverse-selection coin toss, not a gift.**

## 6. Where edge actually comes from (the only real sources)

DNP hazard is not seller edge on its own (§3–5). Real edge/loss lives here:

1. **Hazard-pricing asymmetry (edge).** The parlay taker typically prices DNP
   hazard as ≈ zero — they build the combo as if every leg is a clean binary that
   *will* play. We carry the true hazard: our fair `E[V]` (and thus our quote `q`)
   reflects `P(DNP)` and `s`, theirs does not. Even though DNP is ≈ EV-neutral on
   the *mean*, correctly reflecting it keeps `q` honest and avoids mispricing the
   variance/tail the taker ignores. **This is a modeling-correctness edge, not a
   directional DNP bet.**
2. **`s ≠ p_i` divergence (risk, cuts both ways).** A surprise scratch on a
   stale/thin last price makes `s` a bad estimate of true `p_i`; then
   `E[ΔP&L | B] = p_i − s ≠ 0` and it can go **either** direction (§5). This is a
   **variance / adverse-selection risk**, never a free gain. If our fills cluster
   on the wrong side of stale scratches, we bleed. **This is the ONLY genuinely −EV
   case, and it needs a thin/stale prop book to exist.**

**Two second-order tilts, both slightly TOWARD the NO-seller** (agent-verified
2026-07-09), so DNP if anything nudges our way — not against us:
- **"Rounded down."** The combo `functional_description` says *"Scalar outcomes
  are multiplied (rounded down)"* — `V` is floored to the grid, and we receive
  `1 − floor(V) ≥ 1 − V`. Rounding is **always** in the NO-seller's favor (~½ tick
  per scalar-settled combo).
- **DNP↔other-leg covariance.** A rested/scratched star also drags the same team's
  total/spread/ML down, so `Cov(leg, ∏others) > 0` in same-game combos; a low DNP
  freeze strips that positive covariance, helping the NO-seller a touch beyond the
  independence formula.

## 7. Settlement rule — VERIFIED for soccer scorers (Kalshi market text)

**VERIFIED (Kalshi market rules, soccer anytime-scorer, FRA v MAR 2026-07-09):**
> "If a player is active but never enters the game, the market settles to the last
> fair market price before game start. Once a player enters the game, the market
> settles based on the player's goals scored."

So `v_i = s = last fair market price before game start` for an active-but-unused
player — the `s`-scalar model of §1–6 is **correct** for soccer scorers (it is NOT
a void-recompute, NOT a settle-to-NO). Note the same market text confirms the
period/advancement conventions we already model (advance incl. ET/pens; regulation
markets = 90'+stoppage; props = full game incl. ET).

**Confirmed across sports (agent, live `rules_secondary`):** MLB HR/K's/TB
(`KXMLBHR/KS/TB`), WNBA points (`KXWNBAPTS`) all carry the same clause —
*scratched / never enters → resolve to the fair market price.* Combo
`functional_description` (every collection): *"Scalar outcomes are multiplied
(rounded down)."* Not voided, not refunded. `s ≈ the leg's own market YES price` —
i.e. **≈ our top-down marginal `p` by construction** (this is why §4 holds).
*(2026-07-10: for MLB the clause is materially STRICTER than the soccer wording
suggests — binary needs a START plus ≥1 PA / 1 pitch; see §7.1.)*

- **Which leg types are DNP-able?** single-named-player stat props (scorer, points,
  HR, K's, TB): **YES** (scalar path). moneyline, total, spread, team-total,
  advance, btts, corners, correct-score: strictly binary — **in soccer only**.
  > **CORRECTION 2026-07-10** (MLB settlement audit,
  > `docs/reports/2026-07-10-baseball-vs-soccer-template-scorecard.md`): the
  > original "ML/total/spread strictly binary" claim is **FALSIFIED for MLB**.
  > The 48-hour postponement/suspension rule scalar-settles **EVERY MLB
  > family** — moneyline, total, spread, RFI, and all props. No MLB leg type
  > is strictly binary. Details in §7.1.
- **Still UNVERIFIED:** (a) exact meaning of "fair market price" at freeze — last
  trade? mid? a Kalshi mark? — undefined in the rules and the **crux of the
  manipulation/adverse-selection case** (§6.2); (b) player **not active at all**
  (omitted from squad) vs "active but never enters"; (c) rounding granularity
  (cent vs sub-cent — direction favors NO, magnitude open).
- **Empirically RARE:** the agent scanned **4,913** nested combo markets and found
  **zero** with `0 < settlement_value < 1`. Verified by RULE, not yet by a settled
  on-tape example (a scalar settlement needs every other leg to hit AND a DNP).
  *(Scope note 2026-07-10: that 0-in-4,913 is a soccer-era scan. MLB's rain rule
  puts the scalar trigger at ~1–2% of game-days — §7.1 — so "rare" does NOT
  carry over to MLB combos.)*

## 7.1 MLB settlement — 2026-07-10 audit (rain rule + stricter prop DNP)

Source: settlement edge-case audit in
`docs/reports/2026-07-10-baseball-vs-soccer-template-scorecard.md` (live rules
text + all 9 MLB contract PDFs: GAME/TOTAL/SPREAD/KS/HIT/HR/HRR/TB/RFI).

1. **The 48-hour rule scalar-settles EVERY MLB family.** A game postponed or
   suspended and not completed within 48 hours settles to fair market price —
   and this applies to moneyline, total, spread, RFI, **and** every prop, not
   just player props. The §7 "strictly binary" list has **no MLB members**.
   The §1–§6 math is unchanged, but note the mechanics differ from a
   single-player DNP: a rainout freezes **all same-game legs at once**, so a
   same-game combo collapses to a product of scalars ("rounded down" still
   tilting to the NO-seller, §6). "Fair market price" remains **undefined** for
   MLB too — same manipulation/adverse-selection crux as §7 flag (a);
   TOTAL/SPREAD/RFI even ship empty `rules_secondary` (binding text only in
   the PDFs).
2. **MLB prop DNP is STRICTER than soccer.** Binary settlement requires the
   player to **START** the game **and** record ≥1 plate appearance (batters) /
   throw ≥1 pitch (listed starting pitcher). Scratched, **started-with-0-PA**,
   and entered-without-starting ALL scalar-settle to fair market price —
   **pinch-hit and relief stats explicitly do NOT count**, even though the
   player played. Contrast soccer, where ANY entry (even ~1s) makes the leg a
   clean binary. The MLB DNP hazard `h_i` therefore includes bullpen
   games/openers and bench days — a larger surface than soccer's
   active-but-unused-substitute case.
3. **Frequency: ~1–2% of MLB game-days** hit the postponement/suspension path
   → fractional combo settlements are orders of magnitude MORE likely than the
   soccer 0-in-4,913 estimate the 2026-07-09 REACTIVE decision was based on.
   If/when MLB combos are quoted, **expect `HALT_RECONCILIATION_MISMATCH` to
   fire** on the first such settlement. The halt is fail-safe (stop, not loss)
   and this doc makes the `1 − V` fix minutes-long, so the reactive stance is
   *likely* still fine — but the decision basis changed, so **operator
   re-affirmation is flagged** (NEXT STEPS).

Related MLB scalar paths in the same audit (see the report for full text):
shortened-game totals go scalar unless the over already clinched (~0.3–0.5% of
games — the under can never binary-win a called game); settlement timing is NOT
same-day across a combo's legs (TOTAL up to 15 days vs 3 for GAME/SPREAD/RFI);
forfeits settle per-family differently (last occurred 1995).

## 8. Implementation spec

### pricing/
- **Mean fair needs no special DNP term in the common case:** the Kalshi leg
  marginal already prices DNP hazard (the "scores 1+" mid already discounts for
  the player maybe not playing), and DNP is ≈ mean-neutral, so the product of
  live leg marginals is ≈ unbiased for `E[V]`. Do **not** add a "DNP bonus" to the
  seller's fair.
- **Do allow fractional `V`:** the fair and all EV must treat `V ∈ [0,1]`
  continuous (NO pays `1 − V`), never assume `V ∈ {0,1}`.
- **Optional explicit hazard model** (for width, not mean): per DNP-able leg carry
  `h_i = P(DNP)` and `s_i` (last price); the leg's settlement is the mixture
  `Bernoulli(p_i') w.p. (1−h_i)` ⊕ `point-mass s_i w.p. h_i`, with
  `E[v_i] = (1−h_i)p_i' + h_i s_i`. Use the *spread* of this mixture to **widen**,
  not to shift the mean.

### sim/ (Monte-Carlo engine)
- Each DNP-able leg draws a DNP event first: with prob `h_i`, `v_i = s_i`
  (scalar); else `v_i ~ Bernoulli(p_i')`. Then `V = min(1, ∏ v_i)`. This is the
  one place the fractional-combo distribution (and the variance compression of
  §4, and the `s ≠ p` tail of §6.2) is represented faithfully — the copula/DC
  paths only see binaries. Sizing/tail risk should read `V` from here.

### risk/
- **Continuous settlement:** exposure, worst-case, and P&L must handle
  `NO payout = 1 − V ∈ [0,1]`, not a binary. **Reconciliation must expect a
  fractional settlement** — a legitimate DNP settlement paying `1 − V` (e.g.
  $0.30) must NOT trip `HALT_RECONCILIATION_MISMATCH` (this is exactly the gate
  behind `combo_no_pays_complement`; see the round-trip report).
- **Adverse-selection guard (§6.2):** on combos containing a DNP-able leg with a
  **high `h_i`** and a **stale/thin last price** (unreliable `s_i`), widen or cap
  size — this is the only genuine DNP *risk*. A fresh, liquid `s_i ≈ p_i` needs no
  special treatment.

## Current code exposure (agent-verified) — narrower than it looks

- `legtypes.py` types **only soccer `PLAYER_GOAL`**; MLB/NBA/WNBA props
  (`KXMLBHR/KS/…`, `KX*PTS`) classify as **UNKNOWN → no-quote**. So today our DNP
  exposure surface is **soccer scorers only** — and featured soccer scorers are
  **starters** (they enter → clean 0/1). Practical exposure right now is **small**.
- `sim/engine.py` **already supports** per-leg scalar `settlement` distributions
  (inverse-CDF on the copula uniform, product, cap) — but nothing populates it;
  `pricing/joint.py` prices a pure binary joint. So DNP-able legs are today
  priced/risked **as if binary** (safe-ish given the neutrality result).

## Recommendation (go / no-go by item)

| Item | Call | Why |
|------|------|-----|
| DNP hazard term in `pricing/` fair | **NO** | mean is neutral (`s ≈ p`), move is sub-cent — complexity for nothing |
| `sim/` scalar-draw (Ideas 3/5) | **DEFER** | corrects variance/deltas but scalar settlements are rare (0/4,913); not worth it now |
| **Reconciliation tolerates fractional `V`** | **REACTIVE (operator decision 2026-07-09)** | a settled scorer-combo can pay `1 − V` and would trip `HALT_RECONCILIATION_MISMATCH` — **but that halt is fail-SAFE (stop, not loss), and the event is ~0-in-5,000** *for soccer*. **2026-07-10 (§7.1): trigger becomes ~1–2% of game-days once MLB combos are quoted — reactive likely still fine, but re-affirmation flagged.** Operator's call: don't pre-build; if the halt ever fires, this doc makes it instantly diagnosable and the `1 − V` fix is minutes |
| Freshness/thinness gate on DNP-able props (Idea 1) | **OPTIONAL / low** | defends the only −EV case (§6.2); an extension of existing freshness discipline, not pricing complexity; minimal for soccer starters |
| Classify props out of UNKNOWN (Idea 2) | **only if** we expand to MLB/NBA props | UNKNOWN→no-quote is a SAFE default today |

## NEXT STEPS
- **Decided (operator 2026-07-09): BUILD NOTHING.** DNP is ≈EV-neutral (`s ≈ p`),
  tilts slightly toward the NO-seller, is ~0-in-5,000 rare *(soccer — see §7.1
  for MLB)*, and its only failure mode (a fractional-settlement reconciliation
  mismatch) is a **fail-safe halt**, not a loss — so it's handled **reactively**:
  if the halt ever fires, this doc makes it a minutes-long fix. Revisit if we
  quote thin/illiquid props, size up, **or quote MLB combos (§7.1)**.
- **Re-affirm (owner: operator, before any MLB combo quoting — flagged
  2026-07-10):** the REACTIVE stance on fractional settlement under the §7.1
  frequency (~1–2% of MLB game-days vs the soccer ~0-in-5,000 it was decided
  on). Halt is fail-safe, so reactive is likely still right — but the decision
  basis changed and needs a re-sign-off.
- **Guard (owner: eng, standing):** run `tools/mvec_eligibility_scan.py`
  monthly + before playoffs — exits nonzero if the MLB combo-eligibility
  surface drifts from the 9-family baseline (TEAMTOTAL/F5/RBI appearing in an
  MVE collection = new DNP/settlement audit needed before quoting).
- **Verify (owner: eng, before sizing up on thin props):** what "fair market
  price" means at freeze (§7 flag a, unchanged by §7.1 — MLB rules leave it
  equally undefined) — the crux of the manipulation risk.
- Fold the 2026-07-09 demo combo settlement (binary anchor) in when it resolves.
