# 2026-07-22 — MLB new-props WIRING adversarial judge (OUTS / RBI / SB)

**Role:** adversarial WIRING judge, default REFUTE. Distinct from the same-day
[MEASUREMENT judge](2026-07-22-mlb-newprops-adversarial-judge-measurement.md)
(which validated the ρ *values*); this report attacks the **plumbing** — does
the code that ships route those values to the right pairs, keys, rungs,
containments, and fail-closed defaults, or is a value dead / mis-signed / never
reached?

**Scope / blast radius:** READ-ONLY on `src/`, `config/`, `tests/`. I ran the
LIVE modules (`legtypes`, `sgp`, `conditionals_mlb`, `relationships`, and the
shipped `CorrelationConfig` defaults built into `SgpParams` exactly as
`engine.py:161` does) against real/plausible tickers. No live module or config
was edited. Working tree vs HEAD `5b14e01`; the wiring is the 8-file uncommitted
diff (legtypes +20, sgp +38, conditionals_mlb +86, config +141, 3 test files).

**Config parity confirmed:** none of `config/{prod,demo,prod-live-wc.local}.yaml`
overrides the `correlation` block, so the `CorrelationConfig` class defaults I
judged **are** the live pricing values that arm.

---

## VERDICT (one line): **SHIP** — all 7 attacks REFUTED (defense holds); 0 wiring defects. One pre-existing, documented, fail-OPEN behavior noted (not introduced here, not a blocker).

---

## Attacks → verdict

| # | Attack | Verdict | Proof (executed on the live modules) |
|---|--------|---------|--------------------------------------|
| 1 | **pair_key sort traps** — every wired config key must equal `legtypes.pair_key(a,b)` on the real types, else it is DEAD (never matches). | **REFUTED (defense holds)** | Enumerated all **61** new-family base keys in the live `pair_rho_by_sport["mlb"]`; for each, split the base on `\|`, mapped both tokens to `LegType`, ran `pair_key(a,b)` and compared to the base string. **0 dead of 61.** Every directive trap verified explicitly: `pair_key(player_ks,player_outs)=='player_ks\|player_outs'`, `(player_hr,player_rbi)=='player_hr\|player_rbi'`, `(player_hit,player_sb)=='player_hit\|player_sb'`, `(moneyline,player_outs/rbi/sb)`, `(player_outs,total/rfi/spread)`, `(player_rbi,total)`, `(player_hrr,player_rbi)`, `(player_rbi,player_tb)`, `(player_hit,player_rbi)`, `(player_rbi,player_sb)`, `(player_ks,player_rbi)` — all sort as wired AND the corresponding config key (base or oriented/rung variant) is present. |
| 2 | **Classification collision** — no existing ticker re-classifies; the 3 new prefixes classify right; `KXMLSBTTS` ≠ player_sb; LEADER/DERBY stay UNKNOWN. | **REFUTED (defense holds)** | `classify_leg` on real tickers: HR/HRR/HIT/KS/TB/RFI/GAME/TOTAL/SPREAD **all unchanged**; `KXMLBOUTS→player_outs`, `KXMLBRBI→player_rbi`, `KXMLBSB→player_sb`. The named trap `KXMLSBTTS→btts`/soccer (the `BTTS` keyword precedes any MLB anchor, and `MLBSB` is not a substring of `…MLSBTTS`). `KXLEADERMLBHR/KXLEADERMLBKS/KXMLBHRDERBY/KXMLBHRDERBYQUAL→UNKNOWN` (blockers precede). `KXMLBSPREAD→spread` (MLBSB not a substring). Keyword-order re-proven from `_KEYWORDS`: each new anchor sits **after MLBRFI, before TOTAL/SPREAD, after LEADERMLB/MLBHRDERBY/F5\* blockers**; `KXMLBSERIESGAMETOTAL / KXMLBF5TOTAL / KXMLBF5SPREAD / KXWBCF5TOTAL` still UNKNOWN. |
| 3 | **Rung grammar + NO interpolation** — OUTS/RBI rung-keyed (rung = LAST hyphen segment, not last digit-run), SB not; an unlisted OUTS rung falls back to `:same`, never interpolates. | **REFUTED (defense holds)** | `_leg_rung(OUTS, …MONTERO54-15)==15` (not 54); `_leg_rung(RBI, …CASTRO3-2)==2` (not 3); `_leg_rung(SB,…) is None`. `_pair_rung_suffix(ks, outs r15)==':r15'`. **Full-pipeline** `build_sgp_correlation`: same-pitcher ks×OUTS-**r17** (staged has only r12/r15/r18/r21) → **0.56** (the un-runged `:same`), and 0.56 ≠ the linear-interp 0.47 between r15/r18 — no interpolation. ks×OUTS-**r19** → 0.56 likewise (no r21 extrapolation). OUTS-r16×total → −0.50 plain (total not rung-keyed). |
| 4 | **Seam-2 same-pitcher routing** — same-segment ks×outs → `:same` copula ρ; same-player BATTER cross-family still DECLINES to containment (not the pitcher `:same` path). | **REFUTED (defense holds)** | `_mlb_prop_pair_prior("player_ks\|player_outs", …WHISENHUNT88 both, pitcher_pair=True)` → `:same:r15` = **0.53** (a ρ, not None). Same-segment HR×RBI via `_mlb_prop_pair_prior(pitcher_pair=False)` → **None** (containment owns it). `_MLB_PITCHER_PROP_TYPES=={ks,outs}`; rbi/sb NOT in it (so the dispatch only sets `pitcher_pair` for two pitcher stats). Full-pipeline: opposing-starter ks×outs → **0.045** (`:opp`), same-pitcher → 0.53 — sign/branch correct. |
| 5 | **Containment verdicts** — same-player HR⇒RBI and RBI⇒HRR are CONTAINMENT/IMPOSSIBLE & NON-farmable (MLB scalar); SB⇒HIT is NOT a containment. | **REFUTED (defense holds)** | `relationships.classify_legs` (with a real `EventInfoProvider` stub, distinct per-market events): HR-yes×RBI1-**no** → **IMPOSSIBLE, farmable=False**; HR-yes×RBI1-yes → **CONTAINMENT**; RBI1-yes×HRR1-**no** → **IMPOSSIBLE, farmable=False**; HR2-yes×RBI2-no → IMPOSSIBLE. `is_exact(hr1⇒rbi1)=is_exact(rbi1⇒hrr1)=is_exact(hr2⇒rbi2)=True`; `is_exact(sb1⇒hit1)=False`. SB1-yes×HIT1-no → **not** impossible (measured cell, not containment). |
| 6 | **Fail-closed** — an UNKNOWN new-family pair widens-or-declines (never a convenient default); plain fallbacks (`moneyline\|player_outs`=0.00 etc.) have sign-spanning bands. | **REFUTED (defense holds)** | UNKNOWN pair (SB × a `KXMLBHRDERBY` UNKNOWN leg): point = `default_rho` 0.60 **and** `corr_low = −0.30 ≤ 0` (band reaches the negative regime). Plain fallbacks verified sign-spanning via `rho±band` over the live table: `moneyline\|player_outs` 0.00±0.50, `moneyline\|player_rbi` 0.00±0.40, `moneyline\|player_sb` 0.00±0.25, `player_outs\|spread` 0.00±0.60, `player_sb\|player_sb` 0.05±0.11 — each low<0<high. |
| 7 | **ρ↔band 1:1** — every new `pair_rho_by_sport["mlb"]` key has a matching `mlb:`+key band and vice versa (zero orphans). | **REFUTED (defense holds)** | New-family keys: **61 values, 61 bands, 0 values-without-band, 0 bands-without-value.** Whole live MLB table cross-check for context: **247 values / 247 bands, 0 orphans either direction.** |

**Test suite:** the 3 modified test files pass (`104 passed`); the full
pricing/classification/relationships/conditionals subset passes (`463 passed, 0
failed`).

---

## The one thing worth flagging (NOT a defect, NOT introduced by this wiring)

**Same-player SAME-family rung ladder (e.g. RBI-1+ × RBI-2+ on one player) is
priced by the flat copula fallback, not a containment.** With
`event_mutually_exclusive=False`, `classify_legs([RBI1, RBI2])` returns **OK**
(no `player_rbi|player_rbi` config key exists), so the copula uses
`default_rho=0.60` with a band spanning to **−0.30** — i.e. it fails **OPEN to a
wide band**, never a confident wrong pin.

Why this is acceptable and expected:
- It is the **documented staged decision** ("Same-player SAME-family rungs … are
  deliberately NOT handled this step" — `relationships.py` and
  `staged_mlb_new_props.md` §4 seam 3).
- It is **not new behavior**: the already-shipped families HIT/HR/TB/HRR have no
  `player_X|player_X` self-key either (verified — only `player_sb|player_sb`
  exists, and only because SB *teammate* stacking was measured). OUTS/RBI simply
  inherit the exact same pre-existing treatment.
- If Kalshi marks the rung event mutually exclusive, 2× YES correctly →
  IMPOSSIBLE (verified with `ME=True`). The genuinely-nested "RBI-1 no × RBI-2
  yes" impossibility is the residual not modeled — but it fails to a wide,
  sign-spanning band, honoring rule "UNKNOWN ⇒ widen", not a −EV confident quote.

No action required for arming. Optional future tightening only.

---

## Evidence / repro

Judge scripts (scratchpad, read-only; import the live modules, never reimplement):
build `SgpParams` from `CorrelationConfig()` defaults identically to
`engine.py:161-178`, then execute `classify_leg` / `classify_sport` /
`pair_key` / `_leg_rung` / `_pair_rung_suffix` / `_mlb_prop_pair_prior` /
`build_sgp_correlation` / `relationships.classify_legs` /
`conditionals_mlb.is_exact` on real tickers
(`KXMLBOUTS-26JUL222010DETCHC-DETKMONTERO54-15`,
`KXMLBRBI-…-COLWCASTRO3-2`, `KXMLBSB-…-ATHJHEIM15-1`, etc.).

Key numeric proofs (all executed):
- rung parse takes the LAST hyphen segment: `MONTERO54-15 → 15`, `CASTRO3-2 → 2`.
- unlisted-rung fallback is exact, not interpolated: ks×outs-r17 = 0.56 (= `:same`),
  ≠ 0.47 (linear interp).
- same-pitcher vs opposing-starter branch: 0.53 (`:same:r15`) vs 0.045 (`:opp`).
- containments: HR-yes×RBI-no and RBI-yes×HRR-no both IMPOSSIBLE & farmable=False;
  SB×HIT not a containment.
- UNKNOWN low bound −0.30 (spans 0); 5 plain fallbacks all sign-spanning.
- 61/61 sort-clean, 0 orphan bands (247/247 table-wide).

---

## NEXT STEPS

- **Runs next (owner: eng):** proceed to the rule-8b tape-replay backtest of the
  wired table vs the flat-UNKNOWN baseline + the parity check
  (`staged_mlb_new_props.md` §4 seam 5) — the remaining pre-arm gate. Wiring
  grammar is CLEAR; nothing here blocks it. After merge, bump the
  `mvec_eligibility_scan.py` baseline 9→12 families and flip
  `staged_mlb_new_props.md` to WIRED.
- **Owner (operator):** no wiring decision owed. The open items are the ones the
  measurement judge and AS4 decision already surfaced (scalar/DNP receivable
  monitor; OUTS per-rung-key convention sign-off — both already accepted-as-is,
  non-blocking).
- **Optional / low (owner: eng):** if desired, add a `player_rbi|player_rbi` /
  `player_outs|player_outs` nested-ladder treatment (containment or a measured
  self-key) to replace the fail-OPEN flat fallback for same-player same-family
  rung pairs. Purely a tightening; current behavior is safe (widen-only).
- **Decision owed:** none blocking. VERDICT: **SHIP** the wiring.
