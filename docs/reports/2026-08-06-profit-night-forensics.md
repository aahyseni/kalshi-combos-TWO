# 2026-08-06 — Profit-night forensics (8/5 evening → 8/6 morning): luck or high EV?

**Question (operator, verbatim):** "VERY profitable night last night, almost
500-600$ profit. dissect it to see if it was mainly luck or our EV was high."

**Method (precedent reused verbatim):** same exchange-truth method as
[`2026-08-01-overnight-loss-forensics.md`](2026-08-01-overnight-loss-forensics.md) /
[`2026-08-02-win-night-forensics.md`](2026-08-02-win-night-forensics.md) /
[`2026-08-05-full-pnl-sweep.md`](2026-08-05-full-pnl-sweep.md) — the sweep's and
dissection's cached era pulls REUSED; new API traffic = one paced incremental
GET pull (81 settlement prints + 97 fills since the 8/5 20:30Z overlap point +
balance + positions) and 15 batched paced `GET /markets` calls for leg results.
Joined `mode=ro` against the live store. **Live bot untouched; no order placed
or cancelled.** Entry EVs are ENTRY-TIME records only (quote-ctx fairs →
`fills.expected_edge_cc` identity); **no refit**. Scripts: session scratchpad
`night/` (fetch_inc → ex_merge → extract_db → master → analyze_night →
c1_rescore); outputs in `analyze_out.txt` / `c1_out.txt`.

---

## 60-second operator verdict

**LUCK — almost entirely.** The night (settles 8/5 17:00 ET → 8/6 05:47 ET) made
**+$470.28 at the exchange (81 settles, 51W/30L)**. Entry-time model EV on those
tickets was only **+$56.72 (2.3¢/$1 — a perfectly NORMAL accrual night vs the
era's 2.5¢/$1)**; the other **+$417.75 (~88%) was variance**. Monte-Carlo on our
own entry fairs: **P(night ≥ +$474) = 0.096** — nominally a 1-in-10 night, and
the true odds are BETTER (commoner) than that because the whole night chains into
just **3 game-clusters**, which fattens both tails. Win COUNT was almost on model
(51 vs 47.7 expected) — the overshoot is dollar-weighted: **the top-5 tickets are
96% of the night's net (+$449.82)**, led by a 345-contract 4-leg pure-KS ticket
bought at 29.0¢ carrying **+$0.24 of EV** that hit its 29% branch for
**+$244.99**; 7 of the top-10 are near-coins (p_yes .35–.65) carrying **$3.19 of
total |EV|** that netted +$157.80. That is the **P1 per-structure whale seam,
third sighting** (7/31 loss side, 8/1 win side, tonight win side). And the night
won **where the era LOSES**: +$312 in the 40–60¢ band (era −$132), +$180 on
pure-KS (era's worst cell), +$490 on 2+-prop-leg tickets (the dissection's one
live defect), while the era's only robust cell (60–80¢) **lost** −$49 and
GAME×TOTAL lost −$98. Structure-contradicting profit = variance, not edge.
The operator's "almost $500–600": exchange realized is +$470 (evening-ET slice
+$489); the app-style total-value move since the 8/5 pull is **+$631.64**
(cash $2,064.14→$2,370.05, open-book mark $1,058.87→$1,384.60) — his read sits
exactly between realized and mark-to-market. **Keep sizing off the model; the
night changes no decision** — it re-evidences P1 Stage-1 (whales) and the
utilization/backstop queue, both already ranked.

---

## 1) Exchange-truth reconcile (Task 1)

Pull 8/6 05:47 ET: merged corpus 992 settled tickers / 1,192 fills; cash
**$2,370.05**; 35 open exchange position rows (store: 55 filled-unsettled
tickers, $1,630.25 premium at risk).

| window (settled_time, ET) | n | net | W / L |
|---|---|---|---|
| **NIGHT: 8/5 17:00 → now (task window)** | **81** | **+$470.28** | 51 (+$1,152.08) / 30 (−$681.80) |
| increment since the sweep's 8/5 17:19 pull | 79 | +$413.22 | 50 / 29 |
| 8/5 EVENING only (17:00→24:00) | 55 | +$488.79 | 32 / 23 |
| ET day 8/6 so far (settles 00:00–00:46) | 26 | −$18.50 | 19 / 7 |
| ET calendar day 8/5 (incl. the −$208 afternoon) | 83 | +$223.48 | 45 / 38 |
| last 24 h | 84 | +$436.02 | 53 / 31 |

**The app's "~$500–600":** no settle window reproduces 500–600 exactly. Realized
night = +$470.28 (+$488.79 evening-ET). The **total-portfolio-value move** since
the 8/5 17:19 ET pull = **+$631.64** (cash +$305.91, open-position exchange mark
+$325.73 — which includes ~$540 of premium newly deployed overnight). An
app 1-D portfolio read taken this morning lands between +$470 and +$632
depending on the marks at his glance — the "almost 500–600" is exactly that
straddle. Reconcile = PASS.

**Ledger cross-check (night):** exchange +$470.28 vs ledger +$443.47 — Δ$26.81
decomposed to the cent: **+$31.00 multi-fill loss-overstatement** on 3 tickers
(worst: recovery ticker `…65-3A003C28FFA` ledger −$46.85 on 5 pl rows vs true
−$25.59 — the sweep's known −$132.24 seam class, again feeding
`halt_daily_loss`) **− $4.18** on one settlement the ledger never saw (below).
0 null-recon rows. 2 scalar-value settles in-window (values 17/20 → −$15.48,
−$15.05), handled via exchange revenue truth.

**Downtime facts (log-verified, ET):** last fill 02:50; the 02:05-boot process
died ~03:12; relight attempts 03:13 and 03:16 were killed by the supervisor in
~70 s each — Kalshi 503 (maintenance) → `supervisor_emergency_kill` fail-closed;
clean boot **05:33 ET**, currently LIVE quoting the 8/6 slate. Held positions
were unaffected: every night settlement had completed by 00:46 ET.

## 2) Entry-EV vs realized — the luck split (Task 2)

Entry EV recovered **80/81** (24 ctx-fair, 56 edge-identity; the 1 missing row
is the invisible settle, −$4.18):

| component | value |
|---|---|
| **expected at entry** | **+$56.72** (on $2,426.66 premium = **2.3¢/$1** — normal; era 2.5¢/$1) |
| **luck (realized − expected)** | **+$417.75 (~88%)** |
| realized (recovered / all) | +$474.46 / +$470.28 |
| expected wins vs actual | 47.7 vs 51 — count nearly on model |
| **MC on our fairs: P(night ≥ +$474)** | **0.096** (dist p5 −$445 / p50 +$52 / p95 +$581; P(≤0)=0.434) |
| night game-cluster components | **3** — the MC treats tickets independently; with 3 clusters the true tails are fatter, so the real P is HIGHER (the night is more ordinary than 1-in-10) |
| negative-EV entries | 2, both recovery-path tickers (−$0.12, −$0.03) — the entry gate stayed clean |

**Whale check:** top-5 tickets **+$449.82 = 96% of night net**; top-10 = 83%.

| ticket | legs | entry | q | EV | real | note |
|---|---|---|---|---|---|---|
| `…F1-8EF97EA5F87` | 4-leg pure-KS | 29.0¢ | 345 | **+$0.24** | **+$244.99** | model gave US 29.1% — the 2.4:1 branch hit |
| `…F2-B35283B6CC6` | 2-leg KS×OUTS | 52.9¢ | 256 | +$0.03 | +$120.52 | near-coin |
| `…89-2AE97326BC7` | 2-leg HRR | 43.0¢ | 169 | +$0.02 | +$96.33 | near-coin |
| `…49-191D7E1DBF3` | 2-leg RFI | 74.0¢ | 99 | +$0.14 | −$73.05 | the night's biggest loser |
| `…EC-91D28255D8D` | 2-leg GAME×TOTAL | 67.4¢ | 187 | +$3.81 | +$61.03 | the only top-5 with real EV |

7 of the top-10 are near-coins (p_yes .35–.65) netting **+$157.80 on $3.19 of
total |EV|**. Same seam as the 7/31 −$143.89 whale and the 8/1 +$96/+$87 pair —
**the P1 Stage-1 per-STRUCTURE bound seam, measured for the third time, benign
sign twice running.** n = 1 night; no edge claim.

## 3) Did the night win where the era wins? NO (Task 3)

| cell | era says | tonight | verdict |
|---|---|---|---|
| 60–80¢ band | THE profit cell (+$663, P=0.004) | **−$49.34** (19W/30) | lost in our best cell |
| 40–60¢ band | −$132 (dead zone) | **+$312.30** | won in a dead cell |
| <40¢ | ≈0 | +$210.69 | = the 29¢ KS whale |
| GAME×TOTAL | best family (+$268 excess) | **−$97.65** | lost in our best family |
| pure-KS | worst family (−$229 excess) | **+$180.34** (6W/17 — longshot $) | won in our worst |
| 2+ prop legs | the ONE live defect (bleeds) | **+$489.81** | the defect cell paid |
| 0–1 prop legs | +$560 era | −$15.35 | inverted |
| 2-leg | robust +$653 | +$345.12 | the only era-consistent cell |
| 5+ leg | −$384 bleeder | −$9.26 | quiet |

**Structure-contradicting on nearly every axis** (only 2-leg confirmed). A night
that cashes in the cells the era loses in — and loses in the cells the era wins
in — is the signature of variance, not of the edge showing up. This is also why
no pricing/steering conclusion may be drawn from tonight in either direction
(e.g. "KS is fine after all" would be exactly the refit trap; the KS win was two
longshot tickets).

## 4) Pooled era update (Task 4)

Era ≥ 7/28: now **740 settles, realized +$868.25, entry-EV +$385.33, excess
+$596.49** (EV recovered 724/740; 16 missing carry −$113.56); 23 clusters
(sizes 273/99/95/83/78/43/34/8/8/3 — effectively ~7 independent slates).

| claim | re-scored | one-line verdict |
|---|---|---|
| P(excess ≤ 0) | **0.095** (15 cl) — series 0.061 → 0.200 → 0.242 → **0.095** | **strengthened** — but by a 1-in-10 night; still HYPOTHESIS (edge-beyond-model unproven; the model's own +EV claim is the standing basis) |
| P(realized ≤ 0) | **0.048** (23 cl) — first sub-0.05 read | strengthened (book profitability, not edge-beyond-model) |
| 60–80¢ cell | +$556.91, P(real≤0)=**0.033**, 18 cl, 0 LOCO flips (worst +$313) | **unchanged/robust** — survives its first losing night |
| 2-leg cell | **+$940.87**, P=**0.004**, 11 cl, 0 flips (worst +$593) | **strengthened** |
| 5+-leg bleed | −$393.55, P=1.000, 8 cl [HYP] | unchanged |
| GAME×TOTAL | +$152.92, P=0.228, 9 cl [HYP] | **weakened** (was the 3×-replicated favourite) |
| pure-KS "bleeds $" | realized **+$12.46** (flipped), excess −$58.38, 4 LOCO flips | **weakened to noise on realized $** (the mechanism claim below is what stands) |
| prop-leg marginal bias (+3..+9 pts) | 1,484 legs settled: KS **+6.4** (z_u +1.90) · HR **+8.5** (+1.71) · RFI +11.4 · SPREAD +4.5 · HIT +3.2; our-favor GAME **−7.7** / TOTAL **−5.0** | **unchanged/strengthened** on KS/HR/RFI/SPREAD and on GAME/TOTAL; **HRR weakened** (+4.0 → +1.5; tonight's 11 HRR legs went −24.9 pts AGAINST takers — that's where the near-coin HRR wins came from) |
| night pure-KS joint | 3 hits vs 3.0 independence vs 3.0 model (n=5 full-mid rows) | on-model tonight |

## 5) Recovery-print settlements + invisible class (Task 5)

**The 6 adverse 8/5 recovery prints are settled — all lost.** They sit on **2
tickers** (5 prints on `…65-3A003C28FFA` 20:03–21:18Z @51.6–51.9¢, 1 print on
`…42-3A003C28FFA`), both settled YES against our NO at 8/6 00:12 ET:
**−$25.59 and −$5.45 = −$31.04** (whole-ticket, incl. maker parts). These were
the night's only two negative-entry-EV rows — adverse-by-construction confirmed
again (settled recovery class now 7 tickers ≈ −$51 cumulative). Two knock-ons:
(a) the 5 recovery prints created 5 `position_ledger` rows → the multi-fill
loss-overstatement seam booked −$46.85 vs true −$25.59 into the
`halt_daily_loss` input; (b) **0 new taker prints since** — clean.

**1 NEW invisible settlement** (the −$94.07 class):
`KXMVECROSSCATEGORY-S202699633A1201A-49EA1F28FBA`, −$4.18, settled 8/5 21:14 ET
— exchange fill exists, **zero `fills` rows, zero `position_ledger` rows**.
Class now **8 tickers, −$98.25**. The INCIDENT-C sweep extension (sweep build
#3) remains owed and is re-evidenced.

## Data-integrity notes (blast radius: accounting/monitoring only)

1. Night ledger Δ fully decomposed ($31.00 overstatement − $4.18 invisible);
   both are the sweep's known seams, each with one new instance tonight.
2. Scalar settles ×2 in-window — handled by exchange-revenue truth in this
   analysis; ledger convention build unchanged.
3. EV-recovery coverage 80/81 — no new store-gap class.

## NEXT STEPS

- **Operator:** no decision owed BY this night (verdict: normal-EV night ×
  ~1-in-10-or-commoner variance, structure-contradicting). Standing queue
  unchanged: (1) utilization-backstop repair ratification, (2) P1 Stage-1
  per-structure bounds — **re-evidenced a third time by tonight's 96%-of-net
  top-5**, (3) accounting sweep extension (invisible class grew again), then
  resume posture / kill_anchor / C1-C3-C5. When the app number ≠ bot number:
  app ≈ portfolio-value move (realized +$470 vs mark-inclusive +$632 tonight).
- **Build session (rule-8 isolation, slow-loop):** INCIDENT-C sweep extension to
  collection tickers (8th instance); multi-fill pl de-dup (now also triggered by
  recovery multi-prints); `is_taker`/recovery-edge + quote-age persistence
  unchanged.
- **Watch (pre-registered, no action):** GAME×TOTAL demoted-watch at the
  10-cluster gate; HRR bias sign; 5+-leg cell; 92¢+ tail; the anti-blowout
  SPREAD template (no new instances tonight).
- **Keep pooling, no refit:** next pooled read ~1,000 era settlements;
  P(excess≤0) series 0.061 → 0.200 → 0.242 → 0.095.
