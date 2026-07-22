# 2026-07-22 — MLB settlement-regime ρ audit (sport-switch readiness item #4)

**Owner:** bot session (main thread). **Status:** COMPLETE for the shipped
32→N-entry MLB table (verdict CLEAN); the three NEW prop families (OUTS/RBI/SB)
are CONDITIONALLY cleared pending the live Kalshi settlement-rule verification
(parallel agent, `2026-07-22-mlb-newprop-series-kalshi-verification.md`).

**The item (from `2026-07-19-worldcup-findings-and-new-sport-readiness.md` #4 /
`2026-07-20-session-state-resume.md` gate 4):** *"Pair-rho tables from
measurement — and the WC lesson: a rho measured on one settlement regime
(regulation) must NOT be inherited by an ET/pens-inclusive variant. Audit every
inherited value against its settlement window."* Plus rule-4: settlement
conventions doc-verified per series BEFORE the first quote.

The WC precedent this defends against: `advance|player_goal:same` was measured
on **regulation** (800 games) but KXWCADVANCE settles **incl. ET + pens** → the
value was under-scaled and promoted 0.45→0.52. This audit hunts the MLB analog:
any ρ whose **measurement window ≠ the settlement window of the market it
prices**.

---

## Method

Source of truth = the live `pair_rho_by_sport["mlb"]` block in
`src/combomaker/ops/config.py:915-1495` (read in full), its provenance comments,
the per-series settlement facts in `NOTES.md` (K2, P3-1/P3-2, C4, AS4) and
`docs/dnp_scalar_settlement.md` §7.1, and the 9-PDF MLB settlement audit
(`docs/reports/2026-07-10-baseball-vs-soccer-template-scorecard.md`). Baseline
suite **2595 passed / 3 deselected** at `5b14e01` (clean tree). No live module
touched (rule 8) — this is an audit.

The audit asks three questions of every MLB ρ:
1. **Measurement basis** — what game-state was the co-movement measured on?
2. **Settlement window** — what does the Kalshi market it prices settle on?
3. **Match?** — if (1) ≠ (2), is it corrected (band / re-anchor) or a latent bug?

---

## Finding 1 — the shipped MLB table prices ONLY the 9-family baseline, all on one window

The combo-eligible MLB leg universe is the **9-family baseline**: GAME
(moneyline), TOTAL, SPREAD, KS, HIT, HR, HRR, TB, RFI. Confirmed by grep of the
whole MLB block: **zero** window-variant keys.

| Would-be window-variant | Present in MLB ρ table? | Evidence |
|---|---|---|
| First-5-innings (F5) | **NO** | no `F5`/`first_5` key anywhere in the block |
| Team-total (KXMLBTEAMTOTAL) | **NO — explicitly excluded** | `config.py:931-934`: `player_hr|total` is measured in the GAME-total frame, NOT the team-frame +0.367, because KXMLBTEAMTOTAL is **not combo-eligible**; loading the team-frame value would mis-scale |
| Period / half / 1H | **NO** (MLB) | every `1H`/`period` key in `config.py` is **soccer** (lines 559-870, 1351-1444, 2032); MLB has none |
| First-inning (RFI) | **YES — and it is its own correct window** | RFI settles after the 1st inning; `rfi|*` ρ's are measured as inning-1-runs × the paired market — window-matched (Finding 2c) |

So there is no "regulation-vs-extended" mirror pair in MLB the way KXWCGAME
(reg-90') and KXWCADVANCE (incl-ET-pens) coexist on the same soccer match. The
structural pre-condition for the WC bug does not exist here.

## Finding 2 — every shipped MLB ρ is measured on the same window its market settles

All values are Retrosheet-measured on **final game outcomes** (2005-25 props /
2015-24 game-lines). Kalshi MLB game markets settle on the **final** score (incl.
extra innings; rain-shortened = official once regulation-length is met). Same
window. The three places this could have gone wrong, and why each is clean:

**(2a) Extra innings — the one genuine rule-change break, and it IS handled.**
`extras|total` ships the **POST-2020 value (+0.10)**, not the pooled/pre-2020
−0.04, because the 2020 ghost-runner rule structurally changed extra-inning
scoring (`config.py:910-918`). This is precisely a settlement-regime-aware
promotion — the correct analog of the WC advance fix, already done. `moneyline|
total −0.05` and `extras|moneyline −0.04` are likewise final-score measured.

**(2b) Rain-shortened / scalar settlement is NOT a ρ error (it is a
reconciliation mechanic).** MLB's 48-hour rule scalar-settles legs to fair market
price (`dnp_scalar_settlement.md` §7.1). But the copula ρ measures binary-core
co-movement on *completed* games, and on a scalar settlement the leg's own Kalshi
marginal already prices the DNP/rain hazard (mean-neutral, `s ≈ p` — §3-4 of that
doc). So no MLB ρ needs re-measurement for the scalar regime; the exposure is the
**receivable-shield gap (AS4)** and the **fractional-V reconciliation halt**, both
owned by the scalar/DNP gating decision
(`2026-07-22-scalar-dnp-as4-gating-decision.md`: ACCEPT + monitor, non-blocker)
and the reactive fractional-V stance that `dnp_scalar_settlement.md` NEXT STEPS
flags for **operator re-affirmation before MLB combo quoting**.

**(2c) RFI settles early but is measured on its own window.** RFI ("a run in the
1st inning by either team") settles after the 1st inning. `rfi|total +0.37`,
`rfi|spread` (per-rung, M2), `player_*|rfi` are all measured as inning-1 runs ×
the paired market's final-window stat — the co-occurrence window is correct. The
staggered settlement *timing* (RFI grades before TOTAL) is an in-play/receivable
concern (covered by gate 1 in-play exemption + gate 2 receivables), not a ρ error.

**(2d) No cross-sport inheritance.** Every MLB value is MLB-Retrosheet-measured.
The only sport-agnostic entry, `moneyline|moneyline −0.95`, is the structural
two-team mutex (both teams win = impossible) — universal by construction, not an
inherited measurement. `spread|total 0.13` is a copula fallback the structural
margin/total grid supersedes when it prices.

## Finding 3 — orientation/rung structure is settlement-consistent

The oriented (`:same`/`:opp`) and rung-keyed (`:rN`) entries (ML×prop,
spread×prop, prop×prop, the B4/M2 addenda) are all measured on the same
final-game window with the tape-evidenced rung universe (852,940 MLB-strict RFQ
combos; no rung interpolated). `:opp` exact-negation pairs are 2-way complements
after tie exclusion — a settlement identity, not an assumption. Nothing here
crosses a settlement window.

## Finding 4 — the NEW props (OUTS/RBI/SB): measurement window is final-game; SETTLEMENT window pending live verification

Staged in `docs/calibration/staged_mlb_new_props.md`, measured on Retrosheet
**final** game stats (2005-25, parse-reconciled ≥99.1%). Their staged ρ's
therefore inherit the "full-game final stat" basis. Whether that matches Kalshi's
settlement window is **PENDING the live-API verification agent** (item #5), which
must confirm for KXMLBOUTS / KXMLBRBI / KXMLBSB:

- The settlement stat + window (full game; rain-short scalar per §7.1);
- The **strict prop-DNP definition** — batters need START + ≥1 PA, listed
  starters need ≥1 pitch; pinch-hit/relief stats do NOT count
  (`dnp_scalar_settlement.md` §7.1.2). This makes **`player_outs` (pitcher)** the
  highest-DNP-hazard family of the three (openers / bullpen games / early hooks) —
  a **width + monitor** consideration, NOT a ρ correction (the mean stays neutral).
- Combo-eligibility — `dnp_scalar_settlement.md` NEXT STEPS names **RBI appearing
  in an MVE collection** as an explicit trigger for a fresh DNP/settlement audit
  (guard: `tools/mvec_eligibility_scan.py`). Wiring these families IS that event.

**Gate:** the new-prop staged ρ's are cleared for wiring **only after** #5
confirms (i) the series are combo-eligible, (ii) the line/rung grammar, and (iii)
the settlement rules above. Until then they stay UNKNOWN → fail-closed (safe).

---

## Verdict + rule-4 doc-verification status

| MLB series | Combo-eligible | Settlement window | ρ measurement window | Regime match | Rule-4 doc-verified |
|---|---|---|---|---|---|
| GAME (moneyline) | yes | final incl. extras; rain-short official | final | ✅ | ✅ K2/P3-1 + 9-PDF audit |
| TOTAL | yes | final runs incl. extras | final (extras break handled) | ✅ | ✅ K2 |
| SPREAD | yes | final margin | final margins | ✅ | ✅ K2 (TEAMn = >n−0.5) |
| RFI | yes | end of 1st inning | inning-1 runs | ✅ | ✅ P3-1 |
| KS / HIT / HR / HRR / TB | yes | full game (START+PA/pitch DNP → scalar) | full-game finals | ✅ (mean-neutral scalar) | ✅ 9-PDF audit |
| **OUTS / RBI / SB** | **PENDING #5** | **PENDING #5** | full-game finals | **conditional** | **PENDING #5** |
| TEAMTOTAL / F5 | no (excluded) | n/a | n/a — not priced | n/a | n/a |

**Bottom line:** the shipped MLB ρ table is **settlement-regime CLEAN** — no
WC-style window-inheritance bug exists, the one real rule-change break (extras
ghost-runner) is already corrected, and the rain-short/DNP scalar regime is a
reconciliation mechanic (owned by the AS4 decision + the flagged operator
re-affirmation), not a ρ-value error. The audit gates only on the **new props**,
whose settlement window is being verified live under item #5.

## NEXT STEPS

- **Runs next (eng):** on #5's return, confirm OUTS/RBI/SB settlement window +
  combo-eligibility, then the new props inherit the "final-game" basis cleanly and
  proceed to the rule-8b backtest + wiring (item #3). If #5 finds a
  window mismatch (e.g. OUTS settling on something other than the starter's
  recorded outs), re-anchor before wiring.
- **Owner (operator):** re-affirm the REACTIVE fractional-V settlement stance
  under the ~1–2%-of-game-days MLB frequency (`dnp_scalar_settlement.md` NEXT
  STEPS) — this is the one settlement decision the audit surfaces that is the
  operator's, not eng's. Halt is fail-safe, so reactive is likely still right.
- **Decision owed:** none blocking the existing table; the new-prop clearance is
  gated on #5, not on this audit.
