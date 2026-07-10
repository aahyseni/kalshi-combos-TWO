# Baseball vs the soccer template — full scorecard + MLB settlement edge-case audit

**Date:** 2026-07-10 ~00:45 UTC · **Method:** 3 agents, all claims code-verified
(counts by grep/parse, not memory) or live-API-verified (18 market objects + all
9 contract PDFs) · **Purpose:** operator asked: are we covering all bases for
baseball, what's wired vs not, which legs are 100% confirmed, sectioned
done/confirmed count.

## The soccer template, measured from live code (the bar to clear)

| # | layer | soccer actual (code-verified) |
|---|---|---|
| 1 | Leg classification | 13 LegTypes, 12 ordered keywords, period regex + 1H map, fail-safe UNKNOWN (legtypes.py) |
| 2 | Pair-ρ surface | **96 soccer entries** (52 plain + 44 oriented) + 1 win-prob curve + 111 bands; 36-entry 1H cluster (config.py:162-455) |
| 3 | Orientation resolvers | **~15 resolvers, 19 dispatch branches** in sgp.py (fav/dog, curve, :same/:opp/:tie, scorer guards) |
| 4 | Containment families | 4 (corners_team nested; 1H-BTTS⇒FT; win⇒over-0.5; 1H-over-N⇒FT-over-N) |
| 5 | IMPOSSIBLE + farming | exactly **5 farmable tautologies** (grep-confirmed) + metadata mutual-exclusion (never farmable) + live farm audit (no reachable farm beyond the 5) |
| 6 | Structural pricer | Dixon-Coles enabled (dc_ρ −0.05), settlement windows in the leg parser (ET/pens per family), 2 OOS gates — one of which correctly gates OFF 1H result/spread |
| 7 | Settlement semantics | rulebook-verified windows + DNP/scalar spec (docs/dnp_scalar_settlement.md) |
| 8 | Live gating | collection-prefix whitelist (demo: 6 prefixes; prod: NONE — observe only); UCL leg-level gate |
| 9 | Validation ladder | 9 rungs: calibration scripts → conditional-MLE gate → DC OOS gate → HT-DC gate → 1H-cluster report → 28-combo blind test → 1,480-combo vs-clearing → settlement P&L → live validation |
| 10 | Tests + fixtures | unit tests per layer + ground-truth conventions fixtures |

## Baseball scorecard (per section)

| # | section | status | detail |
|---|---|---|---|
| 1 | Classification | 🟡 **knowledge 9/9 · wiring 3/9** | GAME/TOTAL/SPREAD wired-live (+TEAMTOTAL/EXTRAS classify-only); 6 prop families = UNKNOWN today; staged keywords verified zero-false-positive on 11,305 series |
| 2 | Pair-ρ surface | 🟡 **measured ~25/31 · wired 4/31** | shipped mlb table = 4 entries; measurement tranche verified ~25 values; gaps: 5 cross-family priors + tb\|ks + teammate hrr\|ks |
| 3 | Orientation resolvers | 🔴 **0/2 built** | needs ML-team resolver + teammate/opponent batter routing (designs + measured signed values ready) |
| 4 | Containment | 🔴 **0/3 built** | ml\|spread same-team (exact, 0/98,980); same-player cross-family (HR⇒HIT/TB/HRR); nested-rung guard. NOTE: soccer Family 2 (win⇒over-0.5) is TYPE-gated and would fire on MLB — unprobed, line-1 MLB totals don't trade |
| 5 | Impossible + farming | 🟡 **machinery ✓ · audit ✗** | event flags verified live (props=false, GAME=true — correct IMPOSSIBLE behavior); NO MLB farm audit (win×opp-cover = certain-NO — constructible? unprobed); no flag-pinning fixture |
| 6 | Structural pricer | 🟢 **DONE** | mlb_runs NegBin ENABLED both envs (pydantic default; no YAML override exists), prices ML/TOTAL/SPREAD w/ winner+total gate, 9 unit tests |
| 7 | Settlement semantics | 🟢 **audit DONE (this report)** | all 9 families confirmed-from-rules; see findings below — 3 standing docs/assumptions amended |
| 8 | Live gating | 🟡 **reachable, no kill switch** | MLB combos ALREADY reach the pricer (demo whitelist KXMVESPORTS/KXMVECROSSCATEGORY cover the MLB collections; the KXMVEMLB entry matches ZERO live collections — dead). No per-sport pricing kill switch exists |
| 9 | Validation ladder | 🔴 **2/6 rungs** | calibration ✓ (triple-verified) + adversarial audit ✓; OOS/log-loss gate ✗, blind test ✗, tape backtest ✗ (THE gate, not built), settlement P&L ✗, live ✗ |
| 10 | Tests + fixtures | 🔴 **minimal** | mlb_runs 9 tests + core classification tests; ZERO prop-classification tests; no event-flag fixture; conventions.combo_no_pays_complement still null |

**Bottom line: 2 of 10 sections done, 4 partial, 4 not started.** The knowledge
layer (what is true) is ~90% confirmed; the wiring layer (what the engine does)
is ~30%; the validation layer is ~25%.

## Which combo legs are 100% confirmed accurate END-TO-END today

Classify ✓ + priced by a gated model ✓ + settlement rules ✓:

- **GAME + TOTAL + SPREAD in same-game combos carrying both a winner-flavored
  and total-flavored leg** → NegBin runs grid (enabled, tested), settlement
  verified. ✅
- **Cross-game combinations of those** → independence, verified correct
  (same-day ρ +0.007, under threshold). ✅
- **Everything else is NOT accurate today:** ML+SPREAD-only same-game → flat
  +0.6 ("badly wrong", OOS-documented); all 6 prop families → UNKNOWN → +0.6
  (sign-wrong on K pairs; +25–35¢ basket overbid); TEAMTOTAL pairs → flat +0.6
  (but untradeable in combos).

## Settlement edge-case audit — the layer we had skipped (all from live rules text + the 9 PDFs)

1. ✅ **Totals INCLUDE all extra innings, no cap** — every total-pair ρ we
   measured is correctly framed. No re-measurement.
2. ⚠ **MLB prop DNP is STRICTER than soccer:** binary only if the player
   **STARTS** and records ≥1 PA (batters) / 1 pitch (starter). Scratched,
   started-with-0-PA, and entered-without-starting ALL scalar-settle to "fair
   market price"; **pinch-hit and relief stats explicitly do not count** even
   though the player played. The DNP hazard includes bullpen games/openers and
   bench days — larger than soccer's unused-substitute case.
3. ⚠ **`docs/dnp_scalar_settlement.md` "ML/total/spread strictly binary" is
   FALSIFIED for MLB:** the 48-hour postponement/suspension rule scalar-settles
   EVERY family. MLB rainout rate ~1–2% of games → fractional combo settlements
   are orders of magnitude more likely than the soccer 0-in-4,913 estimate
   behind the reactive decision. First occurrence trips
   HALT_RECONCILIATION_MISMATCH (fail-safe, but expect it).
4. ⚠ **Total shortened-game censoring:** totals called before 9 innings (8.5 if
   home leads) go scalar unless the over already clinched — the under can never
   binary-win a shortened game. Retrosheet treats those (~0.3–0.5% of games) as
   ordinary binaries: tiny systematic caveat added to measurement provenance.
5. ⚠ **Expiration spread:** TOTAL settles up to the 15th day after the game vs
   3 days for GAME/SPREAD/RFI — same-game combo legs are NOT guaranteed to
   settle together; EV-ledger/markout code must not assume same-day settlement.
6. Forfeits settle differently per family on one game (9-0 fictional score for
   TOTAL/SPREAD, scalar/binary for GAME, scalar for props). Last MLB forfeit
   1995 — noted, not actionable.
7. "Fair market price" is undefined in all 9 families (same manipulation crux
   as soccer §7a). TOTAL/SPREAD/RFI markets ship EMPTY rules_secondary — the
   binding text lives only in the PDFs.

## NEXT STEPS

- **Next session (promotion path, unchanged + additions from this audit):**
  1. `tools/backtest_mlb_pairs.py` tape-replay gate — THE gate.
  2. Ship staged classification + [A]/[B] table + routing + containments +
     event-flag fixture (port + parity).
  3. ML-orientation resolver → signed [C] entries; measure [D] pairs.
  4. **NEW from this audit:** amend `dnp_scalar_settlement.md` with the MLB
     scalar paths (rain rule + stricter prop DNP); add the shortened-game
     caveat to `results_baseball.md`; MLB farm-audit probe (is win×opp-cover
     constructible?); prop-classification unit tests.
- **Owner (operator):** re-affirm the REACTIVE stance on fractional settlement
  now that the trigger rate is ~1–2% of MLB game-days (halt is fail-safe —
  likely still fine, but it was decided on soccer's ~0% rate); decide whether a
  per-sport pricing kill switch is wanted before any MLB quoting (none exists —
  whitelist is collection-prefix only).
- **Standing:** doubleheader ticker mechanics confirmed-from-rules but no real
  DH observable yet — verify on the first scheduled DH (recorder tape).
