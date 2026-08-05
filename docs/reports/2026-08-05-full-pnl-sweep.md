# 2026-08-05 — Full P&L sweep (live era): where the money actually comes from

**Operator ask (verbatim):** "analyze pnls, settlements %, profit and losses by
bucket types... a full pnl sweep to get a detailed and good idea of where the
profit / losses is coming from and how we can improve"

**Method (same precedent as the 8/1 and 8/2 forensics):** GET-only paced pulls
of `/portfolio/settlements` + `/portfolio/fills` + `/portfolio/balance` +
`/portfolio/positions` via the project's signed client (0.7 s/page,
429-backoff), joined `mode=ro` against the live store (`fills`, `rfqs`,
`position_ledger`, `decisions kind='quote_sent'/'confirm'/'decline'`). Live bot
untouched; no order placed or cancelled. Entry EVs are ENTRY-TIME records only
(quote-ctx fairs → `fills.expected_edge_cc` identity); **no refit on the P&L
window**. Cluster bootstrap over GAME components (union-find on shared games),
never over fills. Scripts: session scratchpad `sweep/` (fetch → extract_db →
master → analyze → drill); outputs cached in `analyze_out.txt` / `drill_out.txt`.

**Era** = settlements ≥ 7/28 00:00 ET (the current-machine corpus): **661
settled combo tickers** (vs 289 at the last pooled read — 2.3×).

---

## WRONG / CONFIRMED / NEW (the scan table)

| verdict | claim | exchange truth |
|---|---|---|
| **WRONG** (retraction) | "8/2 record day: 413 fills / $7,349" (memory row) | 8/2 ET day = **191 store prints (193 exchange) / $2,778.82 premium** — the biggest deployment day of the era, but half the remembered size; no window I can construct reproduces 413/$7,349 |
| **WRONG** (reversed) | expensive-NO ≈ +1.2%, twice-replicated | third replication FAILED: 80–92¢ band **−2.9% ROI** (n=42, 8 cl), 92¢+ **−7.6%** (n=6) — both HYPOTHESIS-thin, but the sign flipped |
| **CONFIRMED** | cheap-NO +77.9% stays dead | <40¢ band −0.3% ROI (n=68, 10 cl); 40–60¢ **−2.9%** (n=285, 11 cl, P(real≤0)=0.670) |
| **CONFIRMED** | GAME×TOTAL / GAME×KS positive (correlation-credit watch) | GAME×TOTAL **+$307.63 real / +$268.21 excess** (n=65, 8 cl, P(real≤0)=0.000) — 3rd consecutive replication, still <10 cl; GAME×KS +$51.14 |
| **CONFIRMED** | KXMLBHIT and pure-KS negative | pure-KS **−$167.88 / excess −$229.10** (8 cl); HIT-any excess −$44.01 (7 cl) — HIT improved vs the −$147 read, KS got worse |
| **CONFIRMED** | 8/3 = 100 fills/$2,230, 8/4 = 115/$2,132; 8/5 halt at −$233 | exact to the fill: 100/$2,230.31 and 115/$2,131.57; settles before the 8/5 06:31 ET halt = **−$231.05** |
| **CONFIRMED** | the single-combo concentration seam | top-10 \|P&L\| rows = **+$552.70 net = 121% of the era's entire +$455.03**; top 10% of tickets carry 41.3% of premium |
| **RESOLVED** (was open) | "3 TAKER prints on a maker-only book — trace the path" | every era taker print is **`fill_recovery_late_execution`** — the INCIDENT-C recovery booking a quote the exchange first reported cancelled/failed; the late print is marked taker. NOT a rogue quoting path. 6 new ones today (8/5 16:03–17:18 ET), store edge negative on all 6 |
| **NEW** | ledger overstates losses on multi-fill positions | 24 era tickers, ledger shows **$132.24 more loss than the exchange** (16 of them multi-`position_ledger`-row); `halt_daily_loss` anchors read this overstated number |
| **NEW** | 7 settlements fully invisible to the store AND ledger | all esports-collection tickers, **−$94.07** net, zero `fills` rows, zero `position_ledger` rows (7/31–8/4 fill times) |
| **NEW** | leg-count cell: 5+ legs bleed | 5+ legs **−$384.29 real / −$454.45 excess, −27% ROI** (n=84, 7 cl HYP); 2-leg +$652.81, 3-leg +$290.05 |
| **NEW** | cap saturation measured on 8/5 | **182,975** near-cap (util=1.00) widen-shadow events + **122,919** cap-skips (39,176 per-combo + 83,743 entity) vs **36** accepted quotes — the utilization-backstop double-count (REMIND 8/2) is throttling nearly all marginal flow |

## 60-second operator summary

The era book made **+$455.03 on $13,428.75 premium (661 settlements, 59.5% win
vs 58.3% premium-implied, 59.9% model-implied — count calibration is dead-on:
387.2 expected wins vs 384 actual)**. Model-claimed entry EV was **+$332.43
(+2.5¢/$1)**; the +$122.60 overshoot on recovered rows is luck-shaped:
**P(excess≤0) = 0.242** on 14 game-clusters — the edge-beyond-model claim
remains HYPOTHESIS, weaker than the 0.200 read at 289 settlements. **Where the
P&L lives:** every profit dollar sits in the **60–80¢ entry band (+$663.31,
+11.1% ROI, 16 clusters, P(real≤0)=0.004 — the only statistically solid cell in
the whole sweep)**; 40–60¢ gave back −$132, 80¢+ gave back −$73, 5+-leg combos
gave back −$384. Whales still carry everything: top-10 tickets = 121% of net.
The 7/31 machine repairs **changed volume, not edge**: model EV accrual is
2.58¢/$1 pre-8/1 vs 2.55¢/$1 post; premium deployed jumped 2.5×; post-era
realized +$325.19 with P(real≤0)=0.016 but only 6 clusters (HYP). The taker
mystery is closed (late-execution recovery prints, adverse by construction).
Accounting seams found: ledger overstates era losses by $132.24 (multi-fill
double-count — **this is the number the daily-loss halt reads**) and 7
settlements (−$94.07) are invisible to both stores. **Best next $: (1) the
utilization-backstop repair — 8/5 shows the book refusing ~everything at
util=1.00 while model edge is +2.5¢/$1; (2) P1 Stage-1 per-structure bounds —
the whale seam; (3) the accounting sweep extension.**

---

## 1) Exchange-truth reconcile (to the cent)

Pull 8/5 ~18:00 ET: 913 settlement prints (1 per ticker), 1,105 fills, 34 open
positions, cash **$2,064.14**, exchange-marked open-position value $1,058.87.

| window | tickers | exchange net | ledger net | Δ | Δ decomposition |
|---|---|---|---|---|---|
| full pull 7/17 → 8/5 | 913 | **+$1,074.67** | +$608.40 | +$466.27 | +$428.14 = 149 pre-ledger-era (WC) tickers with no `position_ledger` row; +$38.15 = era Δ below; −$0.02 rounding |
| **era ≥ 7/28 00:00 ET** | **661** | **+$455.03** | +$416.88 | **+$38.15** | **+$132.24** ledger loss-overstatement on 24 multi-fill tickers − **$94.07** on 7 exchange-only (invisible) tickers |

Seam inventory (era), all documented in `drill_out.txt`:

| seam | size | notes |
|---|---|---|
| multi-fill loss overstatement | 24 tickers, ledger −$132.24 vs truth | e.g. `…56B9D5F6F55` exch −$33.07 vs ledger −$60.46 (2 pl rows); worst class: one pl row per fill but P&L not per-fill-sized. **`halt_daily_loss` reads the overstated side** — the 8/5 halt fired on −$233-class ledger numbers that exchange puts at −$231.05 (fine today, but the mechanism can over-trigger) |
| invisible settlements | 7 tickers, −$94.07 | all `KXMVESPORTSMULTIGAME*`/`KXMVECROSSCATEGORY`; no `fills` row, no `position_ledger` row; fills 7/31 22:16Z → 8/4 11:19Z; the INCIDENT-C account-wide sweep did NOT catch these |
| fills-store gaps | 18 era tickers | 14 single prints fully missing, 2 partial (`…F606D134832` 12.69 vs 16.92; `…F1C027F89DD` 12.91 vs 34.82 — the PARTIAL-behind-cancel shape again), 1 reverse (db 56.4 > exchange 39.4, ledger $0.00 vs truth +$16.78) |
| scalar-value settles | 15 tickers, values 10–90¢ | binary-convention seam (known since 8/2); master handles via exchange revenue truth |

## 2) The attribution table (era; realized AND entry-EV AND excess)

Columns: real = realized $, EV = entry-model $, excess = real − EV (luck),
W% = actual win rate, mW% = model-implied, iW% = premium-implied (avg NO entry).
EV recovered 646/661 (288 ctx-fair, 358 edge-identity); 15 missing rows carry
−$109.39 realized (incl. the 7 invisible). `[HYP]` = <10 clusters.

**Entry-price bands — ALL the profit is 60–80¢:**

| band | n | cl | prem | EV | real | excess | ROI | W% / mW% / iW% |
|---|---|---|---|---|---|---|---|---|
| <40¢ | 68 | 10 | $876.74 | +$36.33 | −$2.76 | −$38.00 | −0.3% | 29.4 / 30.9 / 29.1 |
| 40–60¢ | 285 | 11 | $4,581.95 | +$102.52 | **−$132.29** | −$190.75 | −2.9% | 52.3 / 53.1 / 51.7 |
| **60–80¢** | **260** | **16** | **$5,959.71** | **+$133.25** | **+$663.31** | **+$521.90** | **+11.1%** | **71.5 / 69.7 / 67.9** |
| 80–92¢ | 42 | 8 | $1,705.28 | +$50.71 | −$50.09 | −$28.40 | −2.9% [HYP] | 81.0 / 88.0 / 85.1 |
| 92¢+ | 6 | 4 | $305.07 | +$9.63 | −$23.14 | −$32.77 | −7.6% [HYP] | 66.7 / 96.1 / 93.7 |

Per-band bootstrap: 60–80¢ **P(real≤0)=0.004 at 16 clusters — the only
non-HYPOTHESIS positive cell in this sweep**. Robustness: minus its top-10
winners it is +$122; minus its best CLUSTER it is +$419; positive 5 of 8 ET
days. It wins 3.6 pts more often than premium implies (71.5 vs 67.9) — that is
markup + correlation credit actually cashing. The 80¢+ bands LOSE despite 92¢+
model-implied 96.1% — 2 losses in 6 at 92¢+ is exactly the expensive-tail bite
the 2.5× cheap-NO taxes were built against, sign now reversed vs the old +1.2%.

**Leg families (top cells, excess $):** GAME×TOTAL +$268.21 (n=65, 8 cl,
P(real≤0)=0.000) · HRR×KS +$155.00 · HR×KS +$101.84 · KS×SPREAD +$91.90 ·
GAME×KS +$39.83 — versus pure-KS **−$229.10** (n=113, 8 cl), pure-SPREAD
**−$155.40** (n=7, NEW watch cell; 5 of 7 rows lost, no single whale),
HR −$95.74, HIT-any −$44.01 (n=149, 7 cl). Same-game GAME×anything credit keeps
replicating; single-family prop stacks keep bleeding. All [HYP<10cl].

**Sport:** MLB 640 rows +$462.30 (9 cl); esports 14 rows +$86.80 (5 cl, incl.
the KXCS2GAME +$72.90); unknown-legs 7 rows −$94.07 (the invisible ones).
Esports-collection naming still ≠ esports legs.

**Ticket size:** <$5: +$3.50 · $5–15: +$62.20 · $15–30: +$90.96 · $30–60:
**−$165.40** · $60+: **+$463.77** (n=58, 9 cl [HYP]). Top-10 |P&L| rows net
+$552.70 (121% of era net; 10.8% of gross |P&L|); biggest loser is still the
7/31 five-leg KS whale −$143.89 (368 contracts @39.1¢, EV +$3.09); 8 of the
top-10 are near-coin 2–3-leg tickets ≥130 contracts with |EV| < $5 — the P1
per-structure-bounds seam, measured again from both signs.

**Leg count:** 2-leg +$652.81 (10 cl) · 3-leg +$290.05 (11 cl) · 4-leg −$9.46 ·
5+-leg **−$384.29, excess −$454.45, −27% ROI** (n=84, 7 cl [HYP]; worst-3 rows
−$285, but still −$99 without them). Compounding correlation error with leg
count is the natural mechanism — measurement item, not a knob.

**Maker vs taker:** maker-only 656 rows +$474.93; taker-touched 5 rows −$19.90.
**Trace closed:** all era taker prints (7/31, 8/1 ×3, 8/2, 8/5 ×6) are
`fill_recovery_late_execution` events — the quote was reported cancelled/
"execution failed", the INCIDENT-C verifier later found the true execution and
booked it; the exchange marks that print taker. No quoting path crossed the
book. The 6 prints today (16:03–17:18 ET, ~58 contracts @~51.6¢) all carry
NEGATIVE store edge (−44 to −470 cc) while their confirm-gate EVs were positive
(+1,696/+852 cc) — late executions are adverse-selected by construction (the
ones that "come back from the dead" are the ones the market moved through).
Unsettled; watch tonight.

**Quote age at fill (partial axis — 398/661 rows have no persisted quote-birth
time):** <10 s: +$287.75 (+6.1% ROI, 12 cl) · 10–60 s: **−$67.45 (−8.2% ROI**,
n=32, 8 cl [HYP]) — the Aranda stale-quote pickoff shape, directionally
present, evidence-blocked by the missing axis. **Zero in-play fills era-wide**
(pregame discipline holds); lead-time bands otherwise flat.

**Day-by-day (ET settle day / premium deployed that ET day):**

| day | n | W/L | realized | EV | cum | deployed |
|---|---|---|---|---|---|---|
| 7/28 | 88 | 62/26 | **+$408.77** | +$30.94 | +$408.77 | $1,266.19 |
| 7/29 | 42 | 22/20 | −$140.44 | +$30.19 | +$268.34 | $287.87 |
| 7/31 | 22 | 17/5 | −$10.34 | +$14.70 | +$257.99 | $1,714.72 |
| 8/1 | 116 | 71/45 | +$291.89 | +$66.13 | +$549.88 | $2,450.17 |
| 8/2 | 191 | 104/87 | **−$264.43** | +$75.57 | +$285.46 | $2,778.82 |
| 8/3 | 79 | 49/30 | +$198.34 | +$40.07 | +$483.79 | $2,230.31 |
| 8/4 | 93 | 54/39 | +$179.48 | +$55.84 | +$663.28 | $2,131.57 |
| 8/5 | 30 | 14/16 | −$208.24 | +$19.00 | **+$455.03** | $797.69 |

Deployment tripled post-8/1 while daily EV rose proportionally (~+$60/day at
~$2.4k/day) — the repairs bought throughput at constant per-$ edge.

## 3) Pooled edge verdict (updated)

| statistic | value | provenance |
|---|---|---|
| era excess (real − EV) | **+$231.99** | 646 recovered rows |
| cluster-bootstrap 95% CI | **[−$394.44, +$876.58]** | 100k draws, 14 clusters |
| **P(excess≤0)** | **0.242** (0.200 @ 289 settles, 0.061 @ ~2 wks ago) | still HYPOTHESIS — 2.3× the data, the claim got weaker again |
| P(realized≤0) | 0.127 (21 clusters) | |
| pre-8/1-ET fills | n=172, prem $3,720.73, EV +$96.06 (**2.58¢/$1**), real +$239.23, P(exc≤0)=0.307 (9 cl) | |
| post-8/1-ET fills | n=474, prem $9,272.70, EV +$236.37 (**2.55¢/$1**), real +$325.19, P(exc≤0)=0.372, P(real≤0)=**0.016** (6 cl [HYP]) | |

**Verdict: the machine repairs changed VOLUME (2.5×), not the per-$ edge
(2.58 → 2.55¢/$1 model EV; excess remains statistically zero).** The book earns
what the model claims plus noise; sizing stays model-anchored. Cross-game
combos chain nights together (cluster sizes 273/99/95/83/43/34/8/8/…), so the
corpus is effectively ~6 independent slates — the CI shrinks slowly.

## 4) Settlement-rate sanity

Overall: **59.5% actual vs 58.3% premium-implied vs 59.9% model-implied**
(contract-weighted avg entry also 58.3¢ on 23,051 contracts). Expected wins on
recovered rows 387.2 vs 384 actual — the joint model's COUNT calibration is
excellent everywhere except the two tails: 80–92¢ wins 81.0% vs 88.0% model
(−7 pts, n=42) and 92¢+ 66.7% vs 96.1% (n=6) — thin but both on the wrong side;
the cheap tail (<40¢) is dead-on (29.4 vs 30.9). Win-rate is NOT where the
40–60¢ loss comes from (52.3 actual vs 51.7 implied) — that band's bleed is
whale-shaped losses inside it, not miscounting.

## 5) Improvement candidates, ranked by measured $

| # | candidate | class | measured basis | est. $ |
|---|---|---|---|---|
| 1 | **Utilization-backstop repair** (per-game double-count ×3.6 + ratify 3×) — the standing REMIND-8/2 decision | **[DECISION]** (fix is [MEASURED-STRUCTURAL]) | 8/5: 182,975 util=1.00 near-cap events + 122,919 cap-skips vs 36 accepts; model EV runs +2.5¢/$1 on admitted flow | largest: each $1k/day premium re-admitted ≈ +$25/day model EV; era days ran $2.2–2.8k deployed |
| 2 | **P1 Stage-1 per-STRUCTURE net bounds** (reservation path; dossier 7/25) | [MEASURED-STRUCTURAL] | top-10 tickets = 121% of era net; $60+ band ±$464; the −$143.89 whale class re-measured from the win side (+$96 near-coins ×4) | tames the ±$100–150/night single-ticket swing that currently IS the P&L |
| 3 | **Accounting sweep extension** (blast radius: monitoring/halt inputs only, never pricing) | [MEASURED-STRUCTURAL] | ledger −$132.24 loss-overstatement (multi-fill positions); 7 invisible settlements −$94.07; 18 store gaps; 15 scalar settles | correctness of `halt_daily_loss` input (the 8/5 halt read −$233 vs true −$231 — benign today, mechanism unsound) |
| 4 | Late-execution recovery = adverse class: persist recovery-time edge + `is_taker`, feed pickoff monitor | [MEASURED-STRUCTURAL] (small $) | 11 era prints, settled ones −$19.90; 6/6 today negative store edge vs positive gate EV | small direct $; closes the last "unexplained path" |
| 5 | Quote-age axis: persist quote-birth→fill age for EVERY fill | [MEASURED-STRUCTURAL] (measurement build) | 398/661 rows missing the axis; the visible slice: 10–60 s fills −8.2% ROI vs <10 s +6.1% | unblocks the Aranda pickoff question |
| 6 | 5+-leg cell (−$454 excess) and 4-leg flatness | [HYPOTHESIS] (7 cl) | pre-register; if it survives ≥10 clusters → leg-count correlation-calibration MEASUREMENT (not a markup knob) | −$384 era if real |
| 7 | Family cells: GAME×TOTAL +, pure-KS −, pure-SPREAD − (new), HIT − | [HYPOTHESIS] (<10 cl each) | 3rd replication of same-game credit; keep pooling | −$385 combined single-family bleed if real |
| 8 | Expensive-NO bands now negative (80¢+) | [HYPOTHESIS] (sign flip, thin) | thermometer only; contradicts the twice-replicated +1.2% | — |
| 9 | Withdraw-budget raise · marginal-KILL-gate seed · policy-halt notification | [DECISION] | no new $ evidence in this sweep | rank below 1–3 |

**No refit performed or recommended from any P&L cut above.** Items 6–8 are
pre-registered watches; action requires structural measurement at ≥10 clusters.

## NEXT STEPS

- **Operator (decisions owed, in this order):** (1) ratify the utilization
  backstop repair — the ×3.6 double-count fix is built-ready per the REMIND-8/2
  memo and this sweep measures near-total cap saturation against +2.5¢/$1
  admitted-flow EV; (2) green-light P1 Stage-1 per-structure bounds (design
  dossier 7/25, attach points verified); (3) resume-posture / kill_anchor % /
  C1/C3/C5 remain open from 7/25.
- **Build session (rule-8 isolation, slow-loop only):** extend the INCIDENT-C
  sweep to collection-ticker settlements (the 7 invisible rows) + de-duplicate
  multi-fill `position_ledger` P&L (the −$132.24 overstatement feeding
  `halt_daily_loss`); persist quote-birth age + `is_taker` on every fill row.
- **Watch (no action):** the 6 unsettled 8/5 late-execution taker prints; the
  5+-leg and pure-SPREAD cells; 80¢+ band sign; GAME×TOTAL at the 10-cluster
  gate.
- **Keep pooling:** next pooled read at ~1,000 era settlements; P(excess≤0)
  series now 0.061 → 0.200 → 0.242.
