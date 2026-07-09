# MLB SGP finalization — classification complete + ρ triple-verified (phase 1 of the baseball workstream)

**Date:** 2026-07-09 ~23:30 UTC · **Status:** classification DONE + adversarially
verified; shipped ρ TRIPLE-verified; measurement tranche RUNNING (report to
follow) · **Staged code:** `docs/calibration/staged_mlb_props.md` (NOT applied —
rule-8 gate) · **Operator directive:** soccer is strong/nearly done → baseball is
the active workstream; classify everything Kalshi allows in combos, verify our
numbers, find the unknowns.

## What ran

12 agents: Kalshi docs sweep ∥ Kalshi API sweep ∥ tape scan ∥ code inventory ∥
ρ-rerun → (independent re-derivation ∥ 2005-25 extension) → classification
synthesis → 2 adversarial classification verifiers + numbers judge + gap
synthesizer. Everything below is Kalshi-source-verified (API rules text / tape /
docs) — no forums, no memory.

## 1. Classification — exactly 9 combo-eligible MLB families (exhaustive)

Verified against **all 1,387 MVE collections** (only 2 carry baseball, each with
exactly these 9) and 3 independent tape windows (~627k MLB combos — zero other
families trade):

| family | LegType | today | note |
|---|---|---|---|
| KXMLBGAME | moneyline | ✅ correct | tape backbone (72% of MLB combos) |
| KXMLBTOTAL | total | ✅ correct | **GAME total** (rules verbatim: "collectively score") |
| KXMLBSPREAD | spread | ✅ correct | "wins by more than N−0.5" |
| KXMLBKS | player_ks (staged) | ❌ UNKNOWN → +0.6 | starter strikeouts N+; largest prop family |
| KXMLBHIT | player_hit (staged) | ❌ UNKNOWN → +0.6 | batter hits N+ |
| KXMLBHR | player_hr (staged) | ❌ UNKNOWN → +0.6 | batter HR N+ (−1 rung = "to hit a HR") |
| KXMLBHRR | player_hrr (staged) | ❌ UNKNOWN → +0.6 | **hits+runs+RBIs — NOT home runs**; keyword MLBHRR must precede MLBHR |
| KXMLBTB | player_tb (staged) | ❌ UNKNOWN → +0.6 | total bases N+ |
| KXMLBRFI | rfi (staged) | ❌ UNKNOWN → +0.6 | run in 1st inning; unique no-suffix ticker grammar |

**Key resolutions (all from live API rules text):**

- **GAME vs TEAM total — the critical question — RESOLVED:** `KXMLBTOTAL` is the
  game total; `KXMLBTEAMTOTAL` is the team total and is **NOT combo-eligible**
  (absent from both MLB collections, 0 tape occurrences). ⚠ Consequence: the
  calibration's two strongest ρs (**HR×own-team-total +0.367**, **K×opp-team-total
  −0.380**) attach to an **untradeable** family — reference values only. The
  tradeable `player_hr|game_total` had never been measured (in the tranche now).
  `player_ks|game_total` −0.25 IS the game frame and IS tradeable — the drop-in.
- **Substring trap quantified** on the full 11,305-series universe: bare
  HR/KS/HIT/TB/RFI collide with 64/67/9/128/10 series (KXANTHROPICRISK→player_hr,
  KXLEADERNFLSACKS→player_ks…). All staged keywords are MLB-anchored, with
  blockers LEADERMLB / MLBHRDERBY / SERIESGAMETOTAL / F5TOTAL / F5SPREAD.
- **Three live misclassification bugs found** (latent — none combo-eligible
  today): KXMLBF5TOTAL→TOTAL and KXMLBF5SPREAD→SPREAD (first-5-innings window
  masquerading as full-game; "F5" evades the period regex) and
  KXMLBSERIESGAMETOTAL→TOTAL (a series game-COUNT market). UNKNOWN-blockers
  staged; must ship before Kalshi ever adds F5 to a collection.
- **Kalshi combo-validity rule (empirical):** duplicate same-event legs are
  rejected (0 same-game GAME×GAME/TOTAL×TOTAL/SPREAD×SPREAD in 6M+ pairs) while
  same-game cross-family and multi-player stacking is abundant.
- **Sport-filter leaks:** KXEWCMLBB (esports) and KXMLBMENTION classify as
  Sport.MLB via substring — any MLB gate must use the explicit 9-family list.

**Adversarial verification:** correctness lens **21 CONFIRMED / 0 REFUTED / 1
UNCERTAIN** (F3/F7 3-way structure only); staged-keyword simulation over all
11,305 series → exactly 11 diffs, all intended, **zero false positives**.
Completeness lens confirmed the 9-family claim on fresh scans but **REFUTED pair
coverage** — see §3.

## 2. Shipped ρ — reproducible at three independent levels

| pair (frame) | doc | rerun | independent | 2005-25 pooled | era verdict |
|---|---|---|---|---|---|
| HR × team-total-over | +0.367 | **exact** | +0.3672 | +0.359 | stable, mild ↑ (+0.015/decade) |
| HR × team-over-4.5 | +0.315 | **exact** | +0.3151 | +0.307 | stable |
| HR × team-wins | +0.232 | **exact** | +0.2324 | +0.226 | flattest — identical to 4dp across eras |
| K × game-total | −0.257 | **exact** | −0.2522 | −0.258 | stationary band [−0.23,−0.29]; "weakening" = REVERSION (2015-19 was the anomalous-strong era; 2025 = −0.214) |
| K × opp-team-total | −0.380 | **exact** | −0.3739 | −0.387 | strongest K pair in every era; 2025 back at mean |
| K × team-wins | +0.242 | **exact** | +0.2371 | +0.249 | very stable |

- Rerun: **every script-emitted digit matched, 0 mismatches** (incl. parse stats
  25,193/25,192/1 and league-total validation).
- Independent from-scratch pipeline (own parser: **100% score parity on 25,191
  games**, exact official 2023 HR=5,868/K=41,843; own BVN solver, err 1e-16 vs
  scipy): deltas 0.0002–0.006, all inside 99% CIs. Only divergence: ~3%
  pitcher-unit bookkeeping (tie-rule under-documented; ρ-insensitive).
- Extension (21 seasons, 49,490 games, 1.03M batter-games, data 2×): **all six
  pairs are stable regimes.** Structural gem: the launch-angle era moved the
  MARGINALS (P(HR) 0.089→0.107), not the copula — exactly why the architecture
  calibrates only the joint layer and takes marginals live.

## 3. What the adversarial pass caught (the reason this process exists)

1. **The pair matrix was materially incomplete.** Fresh full-matrix scans (1.5M
   later rows, no top-30 cutoff) exposed ~15 dropped same-game pair families —
   several LARGER than staged ones: player_hit|player_hr **SG=34,918** (biggest),
   player_hrr|player_ks 13,889, player_hr|player_ks 13,775, player_hr|player_tb
   12,590, moneyline|player_hrr 11,726, player_hit|spread 11,692… all would have
   stayed at flat +0.6 after promotion. Also: the early-Jul-6 sample understates
   later flow ~30× non-uniformly (MLB share 10% early vs ~50% mid/late).
2. **Same-player cross-stat pairs exist** (6–11% of same-game cross-stat prop
   pairs): same player's HR-1 × HIT-1 etc. — **deterministic containments**
   (HR ⇒ HIT, HR ⇒ TB≥4) that no ρ can represent. Needs a containment branch,
   not a table entry.
3. **NEW TOP BLOCKER — `event_mutually_exclusive` metadata:** all players' prop
   markets for one game share ONE event ticker, and relationships.py:203-228
   groups by event ticker BEFORE any ρ table. If metadata is None → every
   multi-player basket widens/no-quotes even after promotion; if true → 2-YES
   baskets wrongly IMPOSSIBLE'd. Nobody had ever checked. **Gates ALL basket
   flow** (the 8-9-leg all-NO HR baskets are a signature tape shape).
4. **My measurement fan-out bug:** the first workflow's Measure phase filtered
   on `prior_rho === 'unmeasured'` but the synthesis wrote prose — zero agents
   fired. Caught by the numbers judge ("new_pairs corpus is EMPTY"). Fixed by
   the explicit follow-up tranche (running now).

## 4. Unknowns queue (20 items, 5 groups — the "more research and math" list)

Top-3 by expected P&L impact: **(1) event_mutually_exclusive resolution** (gates
all baskets), **(2) player_hr|GAME-total measurement** (the forced re-measurement),
**(3) team-orientation resolver + ML|spread containment** (unlocks ±0.24/±0.23 of
signed ρ; ML|spread flat +0.6 is documented "badly wrong", OOS 1.12151 vs
1.00824). Full queue with per-item approach + effort is in the workflow output;
the measurement tranche now running covers the Retrosheet-derivable subset +
blockers (1) and the same-player-rung tape question.

## Measurement tranche in flight (report to follow)

8 agents: game-frame totals (HR/HIT/TB/RFI × game-total) · same-family baskets
(KS×KS starters, HR×HR & HIT×HIT teammate/opponent splits + 8-9-leg basket-level
P(all-no) copula validation) · HRR pairs (RBI extraction) · game-level
(margin×total, win×cover containment validation, same-day cross-game
independence) · hit×KS facing/teammate splits · strike-ladder ρ-flatness (also
resolves the K-line convention question empirically) · event-metadata resolution
· same-player rungs + current-flow pair counts (tape). Then an xhigh judge
produces the final recommended `pair_rho_by_sport["mlb"]` table.

## NEXT STEPS

- **This session (on tranche completion):** results report + final staged table
  update; then the remaining promotion path is: build
  `tools/backtest_mlb_pairs.py` (replay tape MLB RFQs, staged-config override vs
  live config, log-loss/markout gate) → port + parity-check (rule 8).
- **Next session:** team-orientation resolver + MLB containment family
  (relationships.py) + event-metadata fixture, per the unknowns queue.
- **Owner (operator):** K-line convention decision is likely resolved
  empirically by the strike-ladder agent (if ρ is strike-stable, self-median was
  fine) — review its verdict; also review the same-player containment finding.
- **Standing watch:** monthly + pre-playoffs re-scan of both MLB MVE collections
  (eligibility flips: TEAMTOTAL would un-strand +0.367/−0.380; F5 families must
  never go live before the staged blockers ship).
