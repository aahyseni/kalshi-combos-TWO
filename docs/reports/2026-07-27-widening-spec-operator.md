# 2026-07-27 — WIDENING SPEC (operator directive, verbatim intent)

Written down before building so it cannot drift. Supersedes every earlier
widening rule where they conflict.

---

## THE RULE

> **Widen only when a SPECIFIC LEG is over its own limit, tiered by how far
> over. Never for the category. Never for total book size. Never for a combo
> we are not concentrated in.**

---

## 1. NO FAMILY-LEVEL WIDENING — delete it

`leg_family_key = SERIES:side` (e.g. `KXMLBKS:yes`) spans **every** strikeout-over
leg, every pitcher, every game. Widening on it penalises a **fresh pitcher nobody
has bet** simply for being a strikeout.

Operator's arithmetic, which is the whole argument:

> "12 games and there's like 2 main pitchers each game, 24 different props,
> we're missing out if we widen after 2-3 pitcher combos come in."

24 distinct prop entities per slate. Widening the family after 2–3 fill prices us
out of the other ~21. **The family axis reduces our total fill volume for no risk
benefit.** Remove it from PRICE.

Open question the operator answered NO to: a high family backstop. He wants leg-
level only.

## 2. ENTITY (specific leg) — TIERED ON PERCENT, then decline

Load = that one entity key's premium as a **percent of bankroll** (percent, not
dollars — it self-scales as the account grows).

| entity load | action |
|---|---|
| < 1% | **no widen** |
| 1–2% | widen, tier 1 |
| 2–3% | widen, tier 2 |
| **> 3%** | **DECLINE** |

The 3% decline line already exists as `risk.entity_loss_frac: 0.03`. The 1% and 2%
tiers are NEW policy anchors (North Star layer 2 — operator risk appetite stated
once, not knobs). What is new is that the response is **graded** instead of a
cliff at 3%.

## 3. NET ACROSS THE COMBO — the hot leg is not the whole story

> "it should be judged based on other legs as well, if they diversify further or
> concentrate other legs we have"

A combo carrying one leg at tier 1 **plus four fresh legs** is net diversifying and
must not be penalised as if it were a pure add. The decision is the combo's NET
dollar effect on concentration, not its worst leg. (This is the same requirement
as the net-effect admission work.)

## 4. NO BOOK-SHAPE WIDENING — both terms go

Measured decomposition of the 0.87c we were adding on top of the operator's
markup tiers, over 16,558 sent quotes:

| term | reacts to | contribution |
|---|---|---|
| `pbook` | **book shape** | +30.80 cc |
| `peak` | **book shape** | +30.95 cc |
| `family` | the candidate | −7.72 cc (already a rebate) |
| `entity` | the candidate | −6.53 cc (already a rebate) |

- `pbook_armed: false` — **DONE** 2026-07-27 ~19:05 ET. Widening went
  −0.86c → −0.02c.
- `peak` — **OWED**. It has no arming flag; `peak_cc` always enters the sum.
  Needs a code change. Zeroing `peak_widen_max_cc` instead would be a hand-set
  number and is forbidden by the North Star.

## 5. MARKUP TIERS ARE THE OPERATOR'S AND ARE FROZEN

3–5c per sport, 4–6c cross-sport. Note a cross-sport esports+MLB combo is 4–6c
**by design**, not by defect. The bug was the extra 0.87c the machinery added on
top. Any future change must prove markups unchanged by sha256, not by eyeball.

---

## WHY THIS IS A REDUCTION, NOT AN ADDITION

Removes: the family widening axis, the `peak` term, the `pbook` term.
Keeps: one axis (entity), tiered, plus a net-effect check across the combo.

The operator's standing complaint is that changes add machinery and regress
other things. This spec **deletes three of the four widening terms** and grades
the survivor.

---

## NEXT STEPS

- **Owner: next build** — delete family widening from price; implement the
  1/2/3% entity tiers; add a `peak` arming flag and default it off; wire the
  net-effect combo judgement.
- **Sequencing** — the entity path (`risk/limits.py`) is being edited by the
  admission workflow right now. This build must follow it, not race it;
  concurrent edits to one module have already caused near-clobbers today.
- **Gate** — `tools/vitals/gate` 8/8 (CLAUDE.md hard rule 9) plus proof that
  markups are byte-identical and that a fresh pitcher's leg is no longer
  widened by a heavy family.
- **Decision owed by operator** — none outstanding; questions 1–3 answered
  2026-07-27 ~19:15 ET.
