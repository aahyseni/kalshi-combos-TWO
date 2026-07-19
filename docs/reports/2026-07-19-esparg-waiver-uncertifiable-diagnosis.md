# 2026-07-19 — "waiver: game 26JUL19ESPARG not certifiable" declines: DIAGNOSIS (pre-existing, NOT a regression)

**Scope:** 4 last-look waiver declines overnight (11:50 PM Jul 18 – 12:21 AM ET Jul 19),
all on ARG-champ + Messi combos: quote ids `54f2c2b3` (11:50:27 PM ET, husks),
`8b263088` (12:04:10 AM, sizingup), `74d23254` (12:15:03 AM, sizingup),
`b324eb57` (12:20:40 AM, autoscale). All logged
`lastlook_waiver_uncertified … reason="no_structural_plan"`.
**Verdict: PRE-EXISTING behavior surfaced by the overnight book shape — none of
today's commits (a57afc3 / c338281 settled-leg work, ade7b71 trimmed
fingerprint, dad3d91 peak profile) changed any certification outcome. No fix
applied (fail-closed semantics are correct); no src change.**

## Emission site + condition

- Decline string: `src/combomaker/rfq/lifecycle.py:1975` — `_lastlook_mc_waiver`
  returns `(False, f"game {game} not certifiable")` when the enumeration's
  `GameWorstCase.certified` is False.
- Certification: `sim/state_worst_case.py::state_worst_case_by_game` marks a game
  `UNCERTIFIED_NO_PLAN` when `sim/structural_book.py::build_game_plans` returns
  no plan for it. `_try_build_game` returns None (whole game → copula → no plan)
  when **either** (a) fewer than 2 team-level legs with usable feed marginals
  exist in the enumerated universe, **or** (b) `pricing/dixon_coles.py::invert`
  raises `StructuralError` on the assembled target set — both swallowed silently.
- The enumerated universe (`_build_state_worst_case_inputs` + the K=12 trim,
  `lastlook_waiver_topk_resting: 12`) = 9 committed positions + candidate
  (never trimmed) + only the **12 largest resting quotes** on the breached game.

```
RFQ accept ─ reservation DENIED (game cap) ─ waiver
   └─ inputs: committed(9) + candidate + top-12 resting (of ~150-190!)
        └─ build_game_plans("26JUL19ESPARG")
             ├─ <2 team-level targets ──────────► StructuralError ─┐
             ├─ scorer-share sum > 0.95 ────────► StructuralError ─┤ swallowed
             └─ else ► DC plan ► certified bound                   ▼
                                              no plan ► UNCERTIFIED_NO_PLAN
                                              ► lifecycle.py:1975 decline
```

## Evidence chain (logs + mode=ro DB)

| Fact | Source |
|------|--------|
| All 4 failures = `no_structural_plan` (not enumeration_failed / budget) | `lastlook_waiver_uncertified` lines |
| Same game CERTIFIED 20 s / 4 min later on the SAME builds: 12:15:23 AM ET worst 18.22M cc, 12:24:55 AM worst 18.24M cc — both `over_budget` vs 10.29M cc budget | `lastlook_waiver_over_budget`, sizingup + autoscale |
| Trim armed and biting: kept 12 / dropped 118–177 ESPARG quotes; adders 10.5–14.4M cc | `lastlook_waiver_trimmed` lines |
| ESPARG certified WITH the trim, pre-settled-work, on 7/18 11:26 AM ET (over_budget 14.9M vs 9.1M) | `live_20260718_mutexdetmax.log:247111` |
| Game-family books READABLE through the failing window (quote_sent on combos carrying BTTS ×1218, TOTAL-3 ×643, GAME-ESP ×154, champ-ES ×868 in 12:15–12:25 AM) | decisions×rfqs join, mode=ro DB |
| 9 committed positions, games FRAENG+ESPARG; committed ESPARG-group legs = champ-AR, Messi-1, BTTS, GAME-ESP, TOTAL-3, CORNERS-9, TCORNERS-ARG4 | `exposure_rehydrated` + fills/rfqs DB + settled-resolver pending lists |
| Only plan-path code change since the last known-good: the 0/1-marginal skip (a57afc3) — a no-op unless a leg's marginal is exactly 0/1; **no ESPARG-group leg is settled** | `git diff dad3d91 HEAD -- sim/structural_book.py`; `state_worst_case.py` byte-identical; `dixon_coles.py` untouched for weeks |

## Offline reproduction (live machinery, rule 8)

`tools/diagnostics/repro_esparg_waiver_certifiability_20260719.py` — rebuilds the
9 committed positions from the mode=ro DB, installs the live champion aliases,
uses the live `StructuralConfigView`, and calls `state_worst_case_by_game`:

| Scenario | Universe shape | Result |
|----------|----------------|--------|
| S1 fail shape | kept-12 span 5 distinct ARG scorer markets (the sized-up multi-scorer parlay flow) | `invert → "team b player shares sum to 1.44 — inconsistent legs"` → **no plan → not certifiable** (the live decline) |
| S2 flicker | kept-12 concentrated on Messi/Yamal | invert OK → **certified**, n_states 1586 (the live 12:15:23/12:24:55 AM certifications) |
| S3 thin-book instant | only champ-AR team-level readable | `invert → "1 team-level legs cannot identify (lam_a, lam_b)"` → same `no_structural_plan` |
| S4 regression check | HEAD vs **dad3d91 (pre-settled-work)** × facts-as-0/1 vs facts-as-None, both universes | **bit-identical plan sets in all 8 cells** — not a regression |

Root cause of the overnight flicker: certification is a razor-edge function of
WHICH 12 quotes the trim keeps. The overnight champ+scorer parlay flow stacks
many distinct same-team `KXWCGOAL` markets into the inversion targets; each
implies a thinning share of the team's goals, and past `_MAX_TEAM_SHARE = 0.95`
(`dixon_coles.py:45`) the whole game's inversion raises → the game falls to the
copula → uncertifiable. Thin midnight prop books (wide two-sided mids ≈ 0.5)
inflate the implied shares and make this easier to hit. All of this predates
this week's commits; the champion alias parses and inverts correctly (S2/S3
show the Advance target present and usable).

## Practical implication for today's final (operator)

1. **What certifies:** a kept set whose scorer markets are few/concentrated
   (plus ≥2 team-level legs — game-day books provide these easily). What does
   NOT: any instant where the top-12 spans ~4+ distinct same-team scorer
   markets, or a book-flap instant leaving <2 team-level targets.
2. **Bigger blocker — the waiver has NOT GRANTED since the K=12 trim armed
   (last grant 7/16 10:54 PM ET, pre-trim).** Every certification since then
   ended `over_budget` because the trim's dropped-tail adder ALONE
   (10.5–14.4M cc at 130–190 resting quotes) exceeds the game-loss budget
   (10.3M cc now, 8.7–9.4M cc on 7/18). Under game-day load the certificate =
   trimmed worst + adder can mathematically never fit the budget at K=12.
   Tonight's game-cap relief via waiver is effectively OFF whichever way
   certification goes. Operator levers (decisions owed, not taken here): raise
   `lastlook_waiver_topk_resting`, raise the game budget, or accept.
3. FRAENG (settled game) builds no plan by design now — it can never be waived,
   but it is also never the breached game; its settled legs price as constants.

## NEXT STEPS

- **Operator:** decide on the K=12-vs-adder-vs-budget trade-off BEFORE tonight's
  ESPARG flow if waiver relief is wanted (see §implication 2). No code change
  needed for the "not certifiable" symptom itself — it is the fail-closed design.
- **Optional hardening (backlog, not urgent):** log the swallowed
  `StructuralError` reason from `_try_build_game` at debug level so future
  uncertifiable declines carry the sub-cause; consider capping/whitening the
  scorer markets admitted as inversion targets (e.g. keep only the top-N shares
  per team) so a rich prop universe cannot un-certify an otherwise identified
  game — needs its own review (identifiability vs conservatism).
- **This session:** report indexed; repro tool committed under
  `tools/diagnostics/`; suites `test_state_worst_case` 79/79,
  `test_lastlook*`+`test_waiver_fingerprint_trimmed` 77/77 green at HEAD;
  ruff clean on the new tool; no src edits.
