# STAGED — MLB gap pairs (OUTS/RBI/SB reachable pairs still on flat/labeled priors)

**Date:** 2026-07-22 · **Status: MEASUREMENT + STAGED VALUES ONLY — nothing in
`src/`, `config/`, or `tests/` was touched. Wiring + re-verification is a
separate reviewed step (rule 8).** · Tool: `tools/calibrate_mlb_gap_pairs.py`
(NEW, additive; imports the SHIPPED copula via
`tools/calibrate_mlb_player_props.py::implied_rho` → `combomaker.pricing.copula.
gaussian_copula_joint_prob`, reuses `tools/calibrate_mlb_new_props.py` parser
caches, and the LIVE `combomaker.pricing.legtypes.pair_key` — never
reimplemented) · Results artifact `data/history/gap_pairs_results.json`,
per-year parse cache `data/history/newprops_cache/*.pkl.gz` (reused, unchanged) ·
Repro: `.venv/Scripts/python.exe tools/calibrate_mlb_gap_pairs.py report 2005 2025`.

## Why this tranche exists (the gap ledger)

The 2026-07-21 new-props tranche wired OUTS/RBI/SB against team-context legs
(total, moneyline, spread for OUTS, teammate HR/SB) but a Stage-3 gap-ledger
enumeration found **23 reachable same-game pair families still on the flat +0.6
default or on GUESSED labeled priors**. The ZERO-GAPS mandate (operator +
`docs/sport_onboarding_playbook.md` Stage 3/7) says no reachable pair may price
the flat default or an unmeasured prior. This tranche MEASURES every one of them
from Retrosheet 2005-2025 and stages drop-in `pair_rho` values.

- **FLAT-GAPS (absent from config → priced +0.60):** `outs×{hit,hr,tb,hrr,rbi}`,
  `outs×sb`, `outs×outs`, `sb×{hr,tb,hrr}`, `sb×ks`, `rbi×spread`, `sb×spread`,
  `rbi×rfi`, `sb×rfi`, `rbi×rbi` — all confirmed 0 config lines pre-tranche.
- **LABELED-PRIOR TIGHTEN (present but a guess → replace with measurement):**
  `hit×rbi`, `hrr×rbi`, `tb×rbi`, `hit×sb`, `rbi×sb`, `ks×rbi`, `sb×sb:opp`.

## Method (mirrors `staged_mlb_new_props.md` EXACTLY)

Rank 1 raw joint frequencies over the reused Retrosheet caches (1,033,950
batter-game / 98,894 starter-game rows, 49,490 joined games; the same parse the
new-props tranche reconciled: RBI 99.1%, SB 100%, OUTS 99.8%, HR 100%). Rank 2
bisection-invert the SHIPPED copula to a drop-in `pair_rho`; 99% CI = binomial SE
on P(A∩B) through the monotone solver (`_Z99 = 2.576`). Era split **train
2005-2019 vs holdout 2020-2025**. Cluster-floor CI (n = distinct team-games) on
every team-context / combinatorial pair. **Band = max(0.04, CI99 half-width,
|era shift|).** Min-n gate 50,000 for conditional cells (every cell here clears
it — the smallest direct-frequency n is 53,726 for outs×outs; combinatorial
cells carry ordered-pair n in the millions with cluster n ≥ 49,051).

**Frames:** batter cells = all-PA batter-game frame (the shipped ml|prop anchor
convention); OUTS/KS = self-season-median line, ≥5 starts (the KS convention),
median-tie rows excluded. Per-stat reconciled rows only (rbi_ok / sb_ok /
outs_ok), selection ≥99% ⇒ negligible.

**Orientation (measured DIRECTLY on both sides — NEVER negated):**
- OUTS and KS are starting-PITCHER stats. `outs×batter` / `sb×ks` / `ks×rbi`
  have a **FACING** case (`:opp` = the batter/baserunner bats AGAINST that
  starter — measured NEGATIVE) and a same-team case (`:same` = teammate,
  measured ≈0). BOTH sides measured directly by joining each game's batters to
  the opposing (`:opp`) and own (`:same`) starter.
- `outs×outs` = opposing starters (like `ks|ks +0.04`).
- `rbi×rbi` / `sb×batter` = distinct-player teammate(`:same`)/opponent(`:opp`)
  splits over ordered player pairs, like `hr|hr`.
- `spread×{rbi,sb}` = team-signed, oriented like `ml×{rbi,sb}` (`:same` = the
  prop player's team covers the margin; `:opp` = the opponent covers).
- `rfi×{rbi,sb}` = rfi is team-symmetric / orientation-FREE (plain scalar), like
  the shipped `rfi×hit/hr/tb/hrr`.

**Rung grammar:** OUTS and RBI are rung-keyed (`:rN` = Kalshi line integer, N+).
When both legs are rung-keyed the suffixes CHAIN in pair_key leg order.
ks/total/moneyline/rfi/sb never carry a prop rung; spread carries its OWN margin
rung. Per-rung measured ONLY where the ladder is non-flat (tested per family
below); otherwise ONE entry. **NO interpolation/extrapolation, ever.**

## CONVENTION header (wire-list line format)

`<pair_key>[:orient][:rN[:rN]] = <value> band <hw>  (n, CI99, era-shift, source, sign-check)`

- Every base key EXECUTED via `legtypes.pair_key` (sort traps confirmed — see
  §"pair_key verification" below). Orient/rung suffixes chain in pair_key leg
  order. Lookup tier (per the shipped MLB convention): exact rung key →
  un-runged oriented key → plain (fail-closed); NO rung interpolation.
- `[OOS]` = holdout rho fell outside the pooled CI99, but the era shift fits the
  band (bands sized off `max(0.04, hw, |shift|)`); these fire only because the
  giant-n naive CI is ±0.008. Every value is SHIP-grade.
- Each value has a `pair_rho_uncertainty` band listed 1:1 in §Bands.

---

## pair_key verification (sort traps — EXECUTED, not hand-written)

```
outs|hit   -> player_hit|player_outs      sb|hr   -> player_hr|player_sb
outs|hr    -> player_hr|player_outs       sb|tb   -> player_sb|player_tb
outs|tb    -> player_outs|player_tb       sb|hrr  -> player_hrr|player_sb
outs|hrr   -> player_hrr|player_outs      sb|ks   -> player_ks|player_sb
outs|rbi   -> player_outs|player_rbi      rbi|spread -> player_rbi|spread
outs|sb    -> player_outs|player_sb       sb|spread  -> player_sb|spread
outs|outs  -> player_outs|player_outs     rbi|rfi -> player_rbi|rfi
rbi|rbi    -> player_rbi|player_rbi       sb|rfi  -> player_sb|rfi
hit|rbi    -> player_hit|player_rbi       tb|rbi  -> player_rbi|player_tb
hrr|rbi    -> player_hrr|player_rbi       hit|sb  -> player_hit|player_sb
rbi|sb     -> player_rbi|player_sb        ks|rbi  -> player_ks|player_rbi
sb|sb      -> player_sb|player_sb
```

Note the traps: `outs|hit`→`player_hit|player_outs` (hit sorts first),
`outs|tb`→`player_outs|player_tb` (outs sorts first), `sb|hr`→`player_hr|player_sb`,
`sb|tb`→`player_sb|player_tb`. The pair_key leg order fixes which rung the `:rN`
suffix refers to (below).

---

## STAGED wire list — pair_rho (NOT YET WIRED)

```python
# ============ pair_rho_by_sport["mlb"] — GAP-PAIRS TRANCHE, STAGED ============
# ---- OUTS × batter (KXMLBOUTS × batter prop). :opp = the batter FACES that
# ---- starter (NEGATIVE, like player_hit|player_ks:opp scaled by the deeper-
# ---- start channel); :same = the batter is the starter's teammate (~0, the
# ---- environment channel — same as ks:same). Measured BOTH directly. ----
"player_hit|player_outs:opp": -0.21,     # FACING; n=750,529 CI99[-0.217,-0.201] era +0.010
"player_hit|player_outs:same": 0.03,     # teammate; n=751,151 CI99[+0.019,+0.036] era -0.015 [OOS]
"player_hr|player_outs:opp": -0.17,      # FACING; n=750,529 CI99[-0.180,-0.163] era +0.019 [OOS]
"player_hr|player_outs:same": 0.01,      # teammate; n=751,151 CI99[+0.000,+0.018] era +0.005
"player_outs|player_tb:opp": -0.23,      # FACING; n=750,529 CI99[-0.239,-0.225] era +0.017 [OOS]
"player_outs|player_tb:same": 0.02,      # teammate; n=751,151 CI99[+0.009,+0.024] era -0.005
"player_hrr|player_outs:opp": -0.32,     # FACING; n=750,529 CI99[-0.325,-0.312] era +0.027 [OOS]
"player_hrr|player_outs:same": 0.02,     # teammate; n=751,151 CI99[+0.009,+0.024] era -0.000
# outs × rbi: :opp ladder is NON-FLAT (monotone -0.287→-0.337, CIs ~disjoint) →
# per-rbi-rung. rung = the RBI leg's line (pair_key puts outs first). :same is
# flat ~0 across rbi rungs → single un-runged entry.
"player_outs|player_rbi:opp:r1": -0.29,  # FACING; n=750,529 CI99[-0.293,-0.281] era +0.034 [OOS]
"player_outs|player_rbi:opp:r2": -0.32,  # FACING; n=750,529 CI99[-0.322,-0.307] era +0.040 [OOS]
"player_outs|player_rbi:opp:r3": -0.34,  # FACING; n=750,529 CI99[-0.347,-0.327] era +0.042 [OOS]
"player_outs|player_rbi:same": 0.01,     # teammate; n=751,151 ladder flat ~0 (r1-r3 within 0.011)
"player_outs|player_rbi:opp": -0.30,     # un-runged FACING fallback; spans -0.287..-0.337
"player_outs|player_sb:opp": -0.13,      # FACING; n=757,208 CI99[-0.140,-0.120] era -0.003
"player_outs|player_sb:same": 0.05,      # teammate; n=757,895 CI99[+0.040,+0.063] era +0.002
# parse-fail plain fallbacks (orientation unresolved) — span the oriented range:
"player_hit|player_outs": 0.00,          # spans -0.21..+0.03 (sign-spanning)
"player_hr|player_outs": 0.00,           # spans -0.17..+0.01
"player_outs|player_tb": 0.00,           # spans -0.23..+0.02
"player_hrr|player_outs": 0.00,          # spans -0.32..+0.02
"player_outs|player_rbi": 0.00,          # spans -0.34..+0.01
"player_outs|player_sb": 0.00,           # spans -0.13..+0.05

# ---- OUTS × OUTS (opposing starters, like player_ks|player_ks +0.04). ----
"player_outs|player_outs": 0.16,         # n=53,726 CI99[+0.131,+0.191] era -0.042 [OOS]

# ---- SB × batter, distinct-player teammate/opp (like player_hr|player_hr).
# ---- teammate slightly +, opponent ~0 — ALL WIDEN-ONLY (low SB base rate). ----
"player_hr|player_sb:same": 0.01,        # teammate; nclus 98,978 CI99[-0.020,+0.045]
"player_hr|player_sb:opp": -0.02,        # opponent; nclus 49,489 CI99[-0.069,+0.023]
"player_sb|player_tb:same": 0.02,        # teammate; CI99[-0.012,+0.044]
"player_sb|player_tb:opp": -0.02,        # opponent; CI99[-0.064,+0.015]
"player_hrr|player_sb:same": 0.04,       # teammate; CI99[+0.005,+0.065]
"player_hrr|player_sb:opp": -0.03,       # opponent; CI99[-0.072,+0.009]
"player_hr|player_sb": 0.00,             # unrouted fallback (spans teammate/opp)
"player_sb|player_tb": 0.00,             # unrouted fallback
"player_hrr|player_sb": 0.00,            # unrouted fallback

# ---- SB × KS (baserunner × starter). :opp = the baserunner FACES the starter. ----
"player_ks|player_sb:opp": -0.04,        # FACING; n=771,608 CI99[-0.047,-0.025] era -0.020 [OOS]
"player_ks|player_sb:same": 0.03,        # teammate; n=771,492 CI99[+0.015,+0.038]
"player_ks|player_sb": 0.00,             # plain fail-closed (spans -0.04..+0.03)

# ---- RBI × SPREAD, team-signed, CHAINED rungs (rbi rung : spread margin rung,
# ---- pair_key order = rbi first). :same rises with rbi rung, ~flat across
# ---- margin; :opp declines with margin, deepens with rbi rung. ----
"player_rbi|spread:same:r1:r2": 0.33, "player_rbi|spread:same:r1:r3": 0.34,
"player_rbi|spread:same:r1:r4": 0.34, "player_rbi|spread:same:r1:r5": 0.34,
"player_rbi|spread:same:r2:r2": 0.38, "player_rbi|spread:same:r2:r3": 0.38,
"player_rbi|spread:same:r2:r4": 0.39, "player_rbi|spread:same:r2:r5": 0.39,
"player_rbi|spread:same:r3:r2": 0.40, "player_rbi|spread:same:r3:r3": 0.41,
"player_rbi|spread:same:r3:r4": 0.42, "player_rbi|spread:same:r3:r5": 0.42,
"player_rbi|spread:opp:r1:r2": -0.30, "player_rbi|spread:opp:r1:r3": -0.27,
"player_rbi|spread:opp:r1:r4": -0.26, "player_rbi|spread:opp:r1:r5": -0.24,
"player_rbi|spread:opp:r2:r2": -0.33, "player_rbi|spread:opp:r2:r3": -0.31,
"player_rbi|spread:opp:r2:r4": -0.29, "player_rbi|spread:opp:r2:r5": -0.28,
"player_rbi|spread:opp:r3:r2": -0.35, "player_rbi|spread:opp:r3:r3": -0.33,
"player_rbi|spread:opp:r3:r4": -0.31, "player_rbi|spread:opp:r3:r5": -0.30,
"player_rbi|spread:same": 0.35,          # un-runged oriented fallback (spans +0.33..+0.42)
"player_rbi|spread:opp": -0.30,          # un-runged oriented fallback (spans -0.24..-0.35)
"player_rbi|spread": 0.00,               # plain fail-closed, sign-spanning

# ---- SB × SPREAD, team-signed, per SPREAD margin rung (sb 1+-only). :same
# ---- declines with margin, :opp ~flat. GENUINELY ASYMMETRIC (measured both). ----
"player_sb|spread:same:r2": 0.13, "player_sb|spread:same:r3": 0.11,
"player_sb|spread:same:r4": 0.09, "player_sb|spread:same:r5": 0.06,
"player_sb|spread:opp:r2": -0.17, "player_sb|spread:opp:r3": -0.18,
"player_sb|spread:opp:r4": -0.18, "player_sb|spread:opp:r5": -0.18,
"player_sb|spread:same": 0.10,           # un-runged oriented fallback (spans +0.06..+0.13)
"player_sb|spread:opp": -0.18,           # un-runged oriented fallback (spans -0.17..-0.18)
"player_sb|spread": 0.00,                # plain fail-closed, sign-spanning

# ---- RBI/SB × RFI (orientation-FREE, plain scalar — rfi is team-symmetric). ----
"player_rbi|rfi": 0.10,                  # flat across rbi rungs (+0.103/+0.107/+0.115); era +0.006
"player_sb|rfi": 0.02,                   # ≈0; n=1,033,932 CI99[+0.010,+0.030] era -0.019 [OOS]

# ---- RBI × RBI distinct-player teammate/opp, per rung (like rbi|hr splits). ----
"player_rbi|player_rbi:same:r1": 0.08, "player_rbi|player_rbi:opp:r1": -0.01,
"player_rbi|player_rbi:same:r2": 0.07, "player_rbi|player_rbi:opp:r2": -0.01,
"player_rbi|player_rbi:same:r3": 0.06, "player_rbi|player_rbi:opp:r3": 0.00,
"player_rbi|player_rbi:same": 0.07,      # un-runged teammate fallback
"player_rbi|player_rbi": 0.03,           # unrouted fallback (spans teammate +0.08 / opp ~0)

# ============ LABELED-PRIOR TIGHTEN — MEASURED replacements ============
# (distinct-player teammate/opp; all confirm the wired guess sign, tighten it.)
"player_hit|player_rbi:same": 0.08,      # was 0.06; teammate CI99[+0.057,+0.104]
"player_hit|player_rbi:opp": 0.00,       # was 0.00; opponent -0.009 ~0
"player_hit|player_rbi": 0.04,           # was 0.03; unrouted (spans +0.08/~0)
"player_hrr|player_rbi:same": 0.14,      # was 0.10; teammate CI99[+0.118,+0.160] (< hrr|hrr 0.17)
"player_hrr|player_rbi:opp": 0.00,       # was 0.00; opponent -0.012 ~0
"player_hrr|player_rbi": 0.07,           # was 0.05; unrouted
"player_rbi|player_tb:same": 0.08,       # was 0.08; teammate CI99[+0.063,+0.103] CONFIRMED
"player_rbi|player_tb:opp": 0.00,        # was 0.00; opponent -0.007 ~0
"player_rbi|player_tb": 0.04,            # was 0.04; unrouted CONFIRMED
"player_hit|player_sb:same": 0.01,       # was 0.05; teammate +0.014 (tightened DOWN, ~0)
"player_hit|player_sb:opp": -0.02,       # was 0.00; opponent -0.019 ~0
"player_hit|player_sb": 0.00,            # was 0.03; unrouted (measured ~0 both sides)
"player_rbi|player_sb:same": 0.05,       # new split; teammate CI99[+0.022,+0.079]
"player_rbi|player_sb:opp": -0.03,       # new split; opponent CI99[-0.069,+0.009]
"player_rbi|player_sb": 0.02,            # was 0.03; unrouted (spans +0.05/-0.03)
"player_ks|player_rbi:opp:r1": -0.16,    # FACING; was flat -0.12 → per-rung measured
"player_ks|player_rbi:opp:r2": -0.16,    # FACING; n=764,802 CI99[-0.168,-0.152] [OOS]
"player_ks|player_rbi:opp:r3": -0.17,    # FACING; bounded by hit|ks -0.126 / hrr|ks -0.19 ✓
"player_ks|player_rbi:opp": -0.16,       # un-runged FACING fallback (was -0.12)
"player_ks|player_rbi:same": 0.01,       # was 0.01; teammate +0.013 CONFIRMED
"player_ks|player_rbi": 0.00,            # was 0.00; plain fail-closed CONFIRMED
"player_sb|player_sb:opp": 0.00,         # opponent split; -0.003 ~0 (teammate +0.10 already shipped)
```

## Bands (pair_rho_uncertainty — "mlb:"+key, 1:1)

```python
# OUTS × batter — all on the 0.04 judge floor (giant-n CI99 hw ~0.004-0.005,
# era shifts <= 0.042 inside the floor except where noted):
"mlb:player_hit|player_outs:opp": 0.04, "mlb:player_hit|player_outs:same": 0.04,
"mlb:player_hr|player_outs:opp": 0.04,  "mlb:player_hr|player_outs:same": 0.04,
"mlb:player_outs|player_tb:opp": 0.04,  "mlb:player_outs|player_tb:same": 0.04,
"mlb:player_hrr|player_outs:opp": 0.04, "mlb:player_hrr|player_outs:same": 0.04,
"mlb:player_outs|player_rbi:opp:r1": 0.04, "mlb:player_outs|player_rbi:opp:r2": 0.04,
"mlb:player_outs|player_rbi:opp:r3": 0.04,  "mlb:player_outs|player_rbi:same": 0.04,
"mlb:player_outs|player_rbi:opp": 0.06,    # un-runged spans -0.287..-0.337
"mlb:player_outs|player_sb:opp": 0.04,  "mlb:player_outs|player_sb:same": 0.04,
# plain sign-spanning fallbacks:
"mlb:player_hit|player_outs": 0.25,     # spans -0.21..+0.03
"mlb:player_hr|player_outs": 0.20,      # spans -0.17..+0.01
"mlb:player_outs|player_tb": 0.28,      # spans -0.23..+0.02
"mlb:player_hrr|player_outs": 0.35,     # spans -0.32..+0.02
"mlb:player_outs|player_rbi": 0.36,     # spans -0.34..+0.01
"mlb:player_outs|player_sb": 0.20,      # spans -0.13..+0.05
# OUTS × OUTS: band = |era shift| 0.042:
"mlb:player_outs|player_outs": 0.04,    # CI99 hw 0.030, era -0.042 → 0.04 floor covers
# SB × batter (WIDEN-ONLY — cluster CIs, low base rate):
"mlb:player_hr|player_sb:same": 0.04,  "mlb:player_hr|player_sb:opp": 0.05,
"mlb:player_sb|player_tb:same": 0.04,  "mlb:player_sb|player_tb:opp": 0.04,
"mlb:player_hrr|player_sb:same": 0.04, "mlb:player_hrr|player_sb:opp": 0.04,
"mlb:player_hr|player_sb": 0.06, "mlb:player_sb|player_tb": 0.06, "mlb:player_hrr|player_sb": 0.07,
# SB × KS:
"mlb:player_ks|player_sb:opp": 0.04, "mlb:player_ks|player_sb:same": 0.04,
"mlb:player_ks|player_sb": 0.08,        # spans -0.04..+0.03
# RBI × SPREAD (all cluster CI99 hw ~0.007, on the 0.04 floor):
"mlb:player_rbi|spread:same:r1:r2": 0.04, "mlb:player_rbi|spread:same:r1:r3": 0.04,
"mlb:player_rbi|spread:same:r1:r4": 0.04, "mlb:player_rbi|spread:same:r1:r5": 0.04,
"mlb:player_rbi|spread:same:r2:r2": 0.04, "mlb:player_rbi|spread:same:r2:r3": 0.04,
"mlb:player_rbi|spread:same:r2:r4": 0.04, "mlb:player_rbi|spread:same:r2:r5": 0.04,
"mlb:player_rbi|spread:same:r3:r2": 0.04, "mlb:player_rbi|spread:same:r3:r3": 0.04,
"mlb:player_rbi|spread:same:r3:r4": 0.04, "mlb:player_rbi|spread:same:r3:r5": 0.04,
"mlb:player_rbi|spread:opp:r1:r2": 0.04, "mlb:player_rbi|spread:opp:r1:r3": 0.04,
"mlb:player_rbi|spread:opp:r1:r4": 0.04, "mlb:player_rbi|spread:opp:r1:r5": 0.04,
"mlb:player_rbi|spread:opp:r2:r2": 0.04, "mlb:player_rbi|spread:opp:r2:r3": 0.04,
"mlb:player_rbi|spread:opp:r2:r4": 0.04, "mlb:player_rbi|spread:opp:r2:r5": 0.04,
"mlb:player_rbi|spread:opp:r3:r2": 0.04, "mlb:player_rbi|spread:opp:r3:r3": 0.04,
"mlb:player_rbi|spread:opp:r3:r4": 0.04, "mlb:player_rbi|spread:opp:r3:r5": 0.04,
"mlb:player_rbi|spread:same": 0.07,     # spans the same ladder +0.33..+0.42
"mlb:player_rbi|spread:opp": 0.07,      # spans the opp ladder -0.24..-0.35
"mlb:player_rbi|spread": 0.45,          # sign-spanning ±0.42
# SB × SPREAD:
"mlb:player_sb|spread:same:r2": 0.04, "mlb:player_sb|spread:same:r3": 0.04,
"mlb:player_sb|spread:same:r4": 0.04, "mlb:player_sb|spread:same:r5": 0.04,
"mlb:player_sb|spread:opp:r2": 0.04,  "mlb:player_sb|spread:opp:r3": 0.04,
"mlb:player_sb|spread:opp:r4": 0.04,  "mlb:player_sb|spread:opp:r5": 0.04,
"mlb:player_sb|spread:same": 0.06,      # spans +0.06..+0.13
"mlb:player_sb|spread:opp": 0.04,       # ~flat -0.17..-0.18
"mlb:player_sb|spread": 0.25,           # sign-spanning
# RFI:
"mlb:player_rbi|rfi": 0.04, "mlb:player_sb|rfi": 0.04,
# RBI × RBI:
"mlb:player_rbi|player_rbi:same:r1": 0.04, "mlb:player_rbi|player_rbi:opp:r1": 0.04,
"mlb:player_rbi|player_rbi:same:r2": 0.04, "mlb:player_rbi|player_rbi:opp:r2": 0.04,
"mlb:player_rbi|player_rbi:same:r3": 0.05, "mlb:player_rbi|player_rbi:opp:r3": 0.07,  # small deep cell
"mlb:player_rbi|player_rbi:same": 0.04, "mlb:player_rbi|player_rbi": 0.11,  # spans teammate/opp
# LABELED-PRIOR TIGHTEN bands:
"mlb:player_hit|player_rbi:same": 0.04, "mlb:player_hit|player_rbi:opp": 0.04,
"mlb:player_hit|player_rbi": 0.08,
"mlb:player_hrr|player_rbi:same": 0.04, "mlb:player_hrr|player_rbi:opp": 0.04,
"mlb:player_hrr|player_rbi": 0.10,
"mlb:player_rbi|player_tb:same": 0.04, "mlb:player_rbi|player_tb:opp": 0.04,
"mlb:player_rbi|player_tb": 0.08,
"mlb:player_hit|player_sb:same": 0.04, "mlb:player_hit|player_sb:opp": 0.05,
"mlb:player_hit|player_sb": 0.06,
"mlb:player_rbi|player_sb:same": 0.04, "mlb:player_rbi|player_sb:opp": 0.04,
"mlb:player_rbi|player_sb": 0.08,       # spans +0.05..-0.03
"mlb:player_ks|player_rbi:opp:r1": 0.04, "mlb:player_ks|player_rbi:opp:r2": 0.04,
"mlb:player_ks|player_rbi:opp:r3": 0.04, "mlb:player_ks|player_rbi:opp": 0.04,
"mlb:player_ks|player_rbi:same": 0.04, "mlb:player_ks|player_rbi": 0.20,  # sign-spanning
"mlb:player_sb|player_sb:opp": 0.06,    # ~0 WIDEN-ONLY (cluster CI99 hw 0.055)
```

## NO-QUOTE / UNMEASURED cells (explicit — no silent omissions)

**NONE.** Every reachable pair in the gap ledger was measured to standard: the
smallest direct-frequency cell is `outs×outs` at n=53,726 (> 50k gate);
combinatorial teammate/opp cells carry ordered-pair n in the millions with
cluster n ≥ 49,051 distinct games. The two widest cells —
`player_rbi|player_rbi:opp:r3` (band 0.07) and `player_sb|player_sb:opp` (band
0.06) — are both measured ≈0 with WIDEN-ONLY bands sized off the cluster CI99;
they are SHIP-grade at ≈0, not NO-QUOTE. No cell hit a min-n gate; no cell had a
data/reconciliation problem (the reused parse reconciled RBI 99.1% / SB 100% /
OUTS 99.8% / HR 100%).

## Residual / queue accounting (law #4)

Directive PAIRS TO MEASURE = 16 flat-gaps + 7 labeled-prior-tighten = **23 pair
families. All 23 MEASURED here** (each expands to its orient/rung cells above).
After wiring, every one of the 23 leaves the flat +0.6 default / labeled prior.
No queue remains from this tranche. The prior new-props tranche's own queue
(`staged_mlb_new_props.md` NEXT STEPS item 3) is CLOSED by this tranche.

## Sign-check vs pre-registered siblings

| pair | prior expectation | measured | grade |
|---|---|---|---|
| outs × batter FACING (:opp) | negative, ~ ks×batter facing but deeper (deeper-start channel) | hit -0.21, hr -0.17, tb -0.23, hrr -0.32, rbi -0.29..-0.34, sb -0.13 (vs ks siblings hit -0.126, hr -0.075, hrr -0.19) | **MATCHED** — uniformly deeper than ks, as a full-outs stat should be |
| outs × batter :same (teammate) | ~0 (environment channel, like ks:same) | +0.01..+0.05 | **MATCHED** (measured, NOT negated) |
| outs × outs (opposing) | small +, ~ ks|ks +0.04 | +0.16 | MATCHED sign; magnitude above ks|ks (deeper-start co-movement — both go deep in low-scoring games) |
| sb × batter teammate/opp | teammate small +, opp ~0 (like hr|hr) | teammate +0.01..+0.04, opp ~0 | **MATCHED** |
| sb × ks FACING | negative, small (sb low base rate) | -0.04 | **MATCHED** |
| rbi × spread | team-signed like ml×rbi (ml×rbi ±0.33) | :same +0.33..+0.42, :opp -0.24..-0.35 | **MATCHED** — sits on the ml×rbi anchor, rises with rbi rung |
| sb × spread | team-signed like ml×sb (ml×sb ±0.15), genuinely asymmetric | :same +0.06..+0.13, :opp -0.17..-0.18 | **MATCHED** — asymmetric (opp deeper, measured both) |
| rbi × rfi | orientation-free +, ~ hit×rfi/hrr×rfi band (0.065-0.122) | +0.10 (flat) | **MATCHED** (between hit +0.065 and hrr +0.122) |
| sb × rfi | ~0 (sb ⊥ first-inning runs) | +0.02 | **MATCHED** |
| rbi × rbi teammate/opp | teammate small +, opp ~0 (like hr|hr / the shipped rbi|hr) | teammate +0.06..+0.08, opp ~0 | **MATCHED** |
| ks × rbi FACING | negative, bounded by hit|ks -0.126 / hrr|ks -0.19 | -0.16..-0.17 | **MATCHED** — inside the predicted band, deeper than the -0.12 guess |
| hrr × rbi teammate | +, < hrr|hrr teammate 0.17 | +0.14 | **MATCHED** (just under the 0.17 cap) |
| hit/tb × rbi, hit × sb teammate/opp | small +, ~0 opp | all confirm wired sign, tighten | **MATCHED** — zero sign flips vs any wired prior |

**Zero sign flips.** Every measured value matches its sibling/pre-registered
sign; the labeled-prior guesses are confirmed and tightened (details in the
report).

## NEXT STEPS

- **Runs next (owner: orchestrator wiring session):** wire the staged
  `pair_rho` + `pair_rho_uncertainty` blocks VERBATIM into
  `pair_rho_by_sport["mlb"]` and the band table; add `player_rbi` /
  `player_outs` rung-keyed routing for the new chained/oriented keys where not
  already present; verify each key against `legtypes.pair_key`; run the bit-exact
  differential (untouched combos identical, every mover on the enumerated gap
  list) + the flat-baseline backtest; re-run the gap-ledger enumeration to
  confirm ZERO remaining flat/labeled cells for OUTS/RBI/SB.
- **Owner (operator):** confirm the `:same`/`:opp` resolver routes
  pitcher×batter (outs/ks × batter) FACING vs same-team correctly (the resolver
  must compare the batter's team prefix to the pitcher's), and routes the
  spread×{rbi,sb} team-sign; sign off on the `player_outs|player_rbi:opp`
  per-rbi-rung ladder decision.
- **Decision owed:** none blocking measurement; NO family graded NO-QUOTE —
  every reachable gap pair cleared the min-n gate and reconciliation bar.
