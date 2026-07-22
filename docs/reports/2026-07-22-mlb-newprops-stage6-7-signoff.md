# 2026-07-22 — MLB new props OUTS/RBI/SB: Stage 6+7 sign-off

**Verdict: the wire is VERIFIED and SHIP-ready, at ZERO GAPS.** All THREE adversarial
judges (measurement, wiring, gap-pairs) returned SHIP with zero defects; the bit-exact
differential proves surgical additivity across both tranches (5169/5169 identical); the
backtest scorecard is byte-identical to baseline; and a second (gap-pairs) tranche
measured every reachable OUTS/RBI/SB pair so **0 of them price the flat +0.6 default**
(down from 16), pinned by a regression test. A pre-existing dead-config (outs×spread
single-rung keys, both-chain) was fixed in passing (327→319 keys). Arming is gated only
on the operator's fractional-V re-affirmation — the *code* is clear to ship. Suite 2613,
ruff/mypy clean, throughput −5 ns/call (unchanged).

Companion reports (this session): `2026-07-22-mlb-newprop-series-kalshi-verification.md`
(Stage 0/1 live-API recon), `2026-07-22-mlb-settlement-regime-rho-audit.md` (readiness
#4), `2026-07-22-scalar-dnp-as4-gating-decision.md` (AS4), and the two judge reports
`2026-07-22-mlb-newprops-adversarial-judge-{measurement,wiring}.md`.

## What was wired (verbatim from `docs/calibration/staged_mlb_new_props.md`)

3 families across 5 files (relationships.py needed no change — generic):
- `legtypes.py`: `PLAYER_OUTS/PLAYER_RBI/PLAYER_SB` + MLB-anchored keywords (after
  `MLBRFI`, before `TOTAL`; blockers still precede).
- `config.py`: **61** `pair_rho_by_sport["mlb"]` keys + **61** matching `mlb:` bands.
- `conditionals_mlb.py`: **+70** same-player cells (7 exact) + `rbi`/`sb` in `BATTER_FAMILIES`.
- `sgp.py`: 3 families into `_MLB_PLAYER_PROP_TYPES`; new `_MLB_PITCHER_PROP_TYPES`;
  OUTS/RBI into `_RUNG_KEYED_PROP_TYPES` (SB 1+-only); the **seam-2 same-pitcher
  ks×outs `:same` routing** fix.

Table sizes: mlb ρ 186→**247**, conditional cells 233→**303** (84 exact).

## Stage 6 — judge BEFORE the gate

| Check | Result |
|---|---|
| **Bit-exact differential** (pre-wire `5b14e01` vs post-wire, 5,169 priced tape combos) | **5169/5169 IDENTICAL, 0 movers, 0 unexplained** → purely additive; changed nothing for any existing combo |
| **Judge A — measurement/signs** (independent from-scratch BVN tetrachoric re-derivation) | **SHIP, 0 defects.** outs×ks +0.5596; HR⇒RBI 0/101,201 violations (re-parsed 4,164 raw HR strings); **ml×sb +0.15 is REAL** (P(win\|SB)=0.614, +12.4pp, 2× HIT's lift); **`:opp` wires the DIRECT measurement, not the negation** (spread `:opp`≠−`:same`, gap grows with line — B2.6 trap avoided) |
| **Judge B — wiring/grammar** (live modules vs real tickers) | **SHIP, 0 defects.** 61/61 keys match `pair_key`; no existing classification changed; `KXMLSBTTS`↛SB; rung=last-hyphen-segment, live r17/r19 fall to `:same` never interpolated; same-pitcher routing correct; containments non-farmable; fail-closed bands span 0; 61/61 ρ↔band |
| **Tripwire (Stage 5)** | No `taxonomy_impossible.json` change needed — HR⇒RBI/RBI⇒HRR are intercepted in-code and MLB is never farmable (48h scalar); SB⇒HIT correctly not a containment |

## Stage 7 — scorecard

| Criterion | Result |
|---|---|
| Backtest gate (existing families) | **PASS, byte-identical to 7/21 baseline** — prop-carrying promoted 0.68c vs legacy 3.69c; game-lines 0.29c=0.29c (no regression); settled-YES 15.4% on mean fair 15.3c |
| **New-family backtest power** | ⚠️ **CANNOT statistically gate** — of 43 tape combos carrying a new-prop leg (23 RBI/18 OUTS/2 SB), **0 are pregame-priceable and 0 settled** (families freshly eligible). Documented, not hidden (law #4). |
| New-family pricing sanity (constructed real-shape combos) | ✅ every archetype engages the wired value/containment/conditional: outs×ks-same +0.560, outs×total −0.500, rbi×total r1/r3 +0.31/+0.42, ml×sb +0.150, HR⇒RBI CONTAINMENT, HR-yes×RBI-no IMPOSSIBLE, SB×HIT conditional |
| Coverage (untyped-residual) | Typed **9 of 12** previously-untyped new-family pair-hits; **1 named gap remains** (`player_hit\|player_outs`, fail-closed-wide ρ0.60 band [−0.30,+0.95]) |
| Suite / lint / type | 2595 + 17 new-family regression tests green; ruff + mypy clean; hot path untouched |

**Where new-family confidence comes from** (since the backtest can't gate it): the
gold-standard **measurement** (49,490 games, ≥99% reconciled, era-split, cluster CIs),
the **exact-arithmetic containments** (certainties, not statistics), the **two
independent adversarial judges** (one re-deriving from the raw population), the
**no-regression differential** (fully powered on 5,169 combos), and the **pricing
sanity**. This is the honest basis; it is NOT a tape-clearing A/B.

## Isolation / no-spillover (throughput never regresses; fix-isolation rule)

The change touches ONLY the pricing correlation layer (4 files: legtypes, config,
conditionals_mlb, sgp). Nothing in risk, settlement, monitoring, lifecycle, or the
quoting pipeline was edited. Evidence it does not affect any other area of the bot:

- **Pricing spillover = ZERO** — the bit-exact differential proved every existing
  combo (soccer/WC/existing-MLB) prices BYTE-IDENTICALLY pre vs post (5169/5169).
- **Throughput never regresses** (standing rule, before/after measured):
  `classify_leg` 1331→1326 ns/call (Δ −5 ns, noise); full hot path
  (classify_legs + build_sgp_correlation) **0.505 ms/combo** on 400 real combos
  (~1,981 combos/sec single-thread) — the 3 added keywords break-on-match and the
  larger ρ tables are O(1) lookups, so per-quote cost is unchanged.
- **The ungate is additive** — the sport switch ADDS `KXMLB` to the leg-series
  allowlist; it does not alter the soccer/WC path. "Strictly more bets in (MLB)."

Any subsequent gap-pair wiring (below) is likewise additive and re-verified with the
same differential + throughput bench before it counts.

## Gap ledger (Stage 3 residual) — CLOSED TO ZERO by the gap-pairs tranche

The initial new-props tranche measured only the priority pairs; a Stage-3
enumeration then found **16 reachable OUTS/RBI/SB pairs still on the flat +0.6
default + 7 on guessed labeled priors** — a violation of the MLB zero-gaps
standard. A second tranche (`docs/calibration/staged_mlb_gap_pairs.md`, report
`2026-07-22-mlb-gap-pairs-measured.md`) **MEASURED all 23** from Retrosheet
(zero NO-QUOTE, zero sign flips) and wired them (+80 keys, mlb ρ table 247→327).

| Cell(s) | Status |
|---|---|
| outs×{ks, total, ml, spread, rfi} + **outs×{hit,hr,tb,hrr,rbi,sb}** (facing/teammate) + **outs×outs** | MEASURED |
| rbi×{total, ml, hr} + **rbi×{spread, rfi, rbi}** | MEASURED |
| sb×{total, ml, sb} + **sb×{hr,tb,hrr,ks,spread,rfi}** | MEASURED (WIDEN-ONLY) |
| HR⇒RBI, RBI⇒HRR | EXACT (arithmetic) |
| distinct-player {hit,hrr,tb}×rbi, hit×sb, rbi×sb, ks×rbi, sb\|sb:opp | **MEASURED** (were labeled priors — now tightened) |

**Enumeration verdict: 0 reachable new-family pairs fall to the flat default**
(down from 16), pinned by a permanent regression test. No queued measurement
remains from either tranche. Two both-rung-keyed pairs (outs×rbi, rbi×rbi) are
wired **un-runged** (the resolver chains `:r{a}:r{b}`, so single-rung keys would
be dead config; the un-runged oriented value spans the measured ladder within
band). Bit-exact differential re-run after the gap tranche: **5169/5169 identical,
0 movers** — additive, zero regression, throughput unchanged.

## Decisions owed before arming (operator)

1. **~~outs×batter unmeasured~~ — RESOLVED.** The gap-pairs tranche MEASURED it
   (`player_hit|player_outs:opp` −0.21 facing, etc.) along with all 15 other
   flat-gaps. Zero gaps remain; no decision owed here.
2. **ml×sb +0.15 divergence:** Judge A independently confirmed REAL → **sign-off
   satisfied**; recommend adopt as wired (WIDEN-ONLY).
3. **OUTS per-rung convention:** adopted per-rung (data-clear — ladder non-flat, CIs
   disjoint at r15/r18/r21); the r12/r15 "CIs disjoint" doc phrasing is a one-line
   cosmetic over-statement (Judge A), no wiring impact.
4. **Reactive fractional-V settlement stance** under MLB's ~1–2%/game-day scalar
   frequency — the AS4 report's flagged operator re-affirmation. Halt is fail-safe.
5. **The arm itself:** commit+push the wire, then the armed-config allowlist swap
   (remove WC aliases + `KXMENWORLDCUP`, arm `KXMLB`) + the scalar/DNP monitor (AS4
   report) + the kill+relight mid-slate drill.

## NEXT STEPS

- **Runs next (eng):** on operator go — commit+push the wire; build the AS4
  scalar-unresolvable paging monitor (logging-only, zero pricing blast radius); then
  the allowlist swap + first-relight throughput before/after + `settlement_receivable_*`
  watch.
- **Owner (operator):** the 5 decisions above (1 and 4 are the only substantive ones;
  2/3 are satisfied/adopted).
- **Queued measurement (eng, post-arm):** outs×opposing-batter props + the distinct-player
  labeled-prior cells — tighten from wide when measured (MEASURE-BEFORE-TIGHTEN).
