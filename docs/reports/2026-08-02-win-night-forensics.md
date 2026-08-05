# 2026-08-02 — Win-night forensics (8/1 slate): luck or good fills?

**Question (operator, verbatim):** "We profited $300 off yesterdays entire slate,
was it by luck or good fills"

**Method:** same exchange-truth method as
[`2026-08-01-overnight-loss-forensics.md`](2026-08-01-overnight-loss-forensics.md)
— GET-only pulls of `/portfolio/settlements`, `/portfolio/fills`,
`/portfolio/balance`, `/portfolio/positions` via the project's signed client
(paced 0.7 s/page, 429-backoff), joined read-only (`mode=ro`) against the live
store (`fills`, `rfqs`, `position_ledger`, `decisions kind='quote_sent'/'confirm'`).
**No order placed/cancelled; the live bot untouched.** Entry EVs are ENTRY-TIME
records only (quote ctx fairs + `fills.expected_edge_cc`) — no refit on the P&L
window. Scripts: scratchpad `win/` (fetch → master → night1/2/3 → pooled).

---

## Verdict (30-second read)

**Both — and mostly luck in the dollar amount.** The fills were genuinely good:
of the night's 113 settled combos, **112 had positive expected edge at entry**
(the one negative-EV entry was a taker print), and on the 46 combos whose raw
Kalshi leg mids survived, **39/46 (85%) were priced at-or-better than raw-mid
consensus fair** (median margin **+1.50¢/contract** in our favor). But good
fills only bought **+$59.47 of expected profit**; the realized **+$241.25** means
**+$179.51 (~75%) was variance**. Monte-Carlo on our own entry fairs:
**P(night ≥ +$239) = 0.253** — a 1-in-4 night (the evening slate alone, +$408.41
before this morning's −$167 esports-collection give-back, was 1-in-12). The tell:
hit COUNT landed exactly on model (67 wins vs 67.26 expected) — the overshoot
came from WHICH combos won: the five luckiest rows (+$96.25, +$87.32, +$48.45,
+$47.90, +$47.74) were all near-coin-flips (p_yes 0.37–0.54) carrying ≈$0 EV at
100–220 contracts. The operator's "~$300" is the app's ET-calendar-day window
(+$291.89); the ledger's "+$238.73/114" matches exchange truth (+$241.25/113)
within known seams. **Pooled since 7/28 the book is +EV-expected and
+$446.89 realized, but P(excess ≤ 0) = 0.200 on 9 game-clusters — the standing
0.061 got WEAKER with more data, still HYPOTHESIS; and the "cheap-NO +77.9%"
split FAILED out of sample (−30.4% ROI since 7/30).** Keep sizing off the model,
not off this night.

---

## 1) Exchange-truth reconcile (Task 1)

| window (settled_time) | n | net | W / L |
|---|---|---|---|
| **task window: ≥ 8/1 20:00Z → now (8/2 07:47Z)** | **113** | **+$241.25** | 68 (+$960.77) / 45 (−$719.53) |
| pure evening slate: 20:00Z → 8/2 04:00Z | 92 | **+$408.41** | 59 / 33 |
| ET calendar day 8/1 (04:00Z→04:00Z) — **the app's "~+$300"** | 116 | **+$291.89** | 71 / 45 |
| 8/1 morning leftovers (04:00–20:00Z) | 24 | −$116.52 | — |
| settled since 8/2 04:00Z (this morning ET) | 21 | **−$167.16** | — |

Settlement fees in-window: $1.63 (already inside net). Cash balance at pull:
$1,934.54; 45 open position rows.

**Reconciling the three numbers:** app "~+$300" = ET calendar day (+$291.89 =
evening +$408.41 − morning leftovers $116.52). Ledger-indicative "+$238.73/114,
68W/46L" vs exchange "+$241.25/113, 68W/45L" (Δ $2.52, Δn=1) decomposes into:

| seam | detail |
|---|---|
| duplicate ledger rows | 4 night tickers carry 2–3 `position_ledger` rows each (split fills) — counting positions ≠ counting settlements (114 vs 113, and the extra "L") |
| fractional/scalar esports-collection settles | 5 cent-mismatches; biggest `…-4164F7D9BCD` ledger −$2.23 vs exchange −$11.00 (split rows); 3 combos settled **scalar** at value 90/79/19¢ — partial payouts the binary ledger model rounds differently (`…-72D6D09EA98`: −$14.46 actual vs −$118.07 full-premium branch) |
| unreconciled-at-read rows | 3 rows settled 03:15–03:25Z were still `realized_pnl_cc=NULL` at the ledger read (+$30.22 at exchange) |
| crash-seam missing rows | **NONE missing from the ledger this time** (INCIDENT-C sweep held). But **2 exchange fills never reached the `fills` store** (both maker: `…-9F95E647527` 8.97ct @68.1¢ 18:10Z; `…-48A6B24C74C` 5ct @11.9¢ 22:45Z) — ledger rows exist via the sweep, entry-EV context lost (net P&L impact +$2.27) |

110/113 ledger rows otherwise match exchange to the cent.

## 2) Entry-EV vs realized — the luck split (Task 2)

Entry EV recovered **111/113** (46 from `quote_sent` ctx fairs, 65 from the
`fills.expected_edge_cc` identity; 2 store-gap rows unrecoverable, +$2.27 P&L):

| component | value |
|---|---|
| **expected at entry (good fills)** | **+$59.47** |
| **luck (realized − expected)** | **+$179.51** |
| realized (recovered subset / all) | +$238.98 / +$241.25 |
| expected wins vs realized | 67.26 vs 67 — **dead on model** |
| MC on our own fairs: P(night ≥ +$239) | **0.253** (dist p5 −$378 / p50 +$64 / p95 +$480) |
| same, evening slate only: P(≥ +$406) | **0.080** |
| raw-mid independence cross-check (46 rows with mids) | our EV +$26.61 vs indep +$14.58; realized +$197.13; **P(≥) under independence = 0.170** |

The luck is **dollar-weighted, not hit-weighted**: top-10 luckiest rows
contributed +$493.82 of gross luck — big near-coin positions that happened to
win. That is the mirror image of the 7/31 loss night (one 368-contract coin-flip
whale losing −$143.89): the SAME concentration seam, benign sign this time.
n = 1 night; no edge claim from this window.

## 3) The pooled view since 7/28 (Task 3)

289 settlements (settle dates 7/28→8/2; EV recovered 280/289, the 9 missing sum
−$64.16 realized): premium $5,842.26, **entry EV +$150.19, realized +$446.89,
excess +$296.69**. Union-find over shared games → **9 cluster components**
(sizes 110, 82, 39, 34, 8, 3, 2, 1, 1 — cross-game combos chain a night into
~one cluster, so the corpus is effectively ~5 independent nights):

| statistic | value |
|---|---|
| excess (realized − entry EV) cluster-bootstrap 95% CI | [−$357.65, +$991.53] |
| **P(excess ≤ 0) — the standing number** | **0.200** (was 0.061 on ~7–10 clusters two weeks ago → got WEAKER, not stronger) |
| P(realized ≤ 0) | 0.122 |

**Still HYPOTHESIS-class: < 10 clusters.** The edge remains statistically
indistinguishable from variance; only the model's own entry-EV accounting
(+$150 claimed, realized above it) says +EV.

**Entry-price buckets — the 7/29 split does NOT hold out of sample:**

| bucket | pooled (since 7/28) | OOS (fills placed ≥ 7/30) | 7/29 claim |
|---|---|---|---|
| < 50¢ NO | n=62, ROI **+9.7%** | n=27, **−30.4%** (−$131.44, **2 clusters**) | +77.9% |
| 50–85¢ | n=206, +8.1% | n=116, +10.4% (4 clusters) | — |
| ≥ 85¢ | n=12, +1.7% | n=8, +1.3% (2 clusters) | +1.1% ✓ |

The cheap-NO outperformance was in-sample noise (HYPOTHESIS both directions at
2 clusters); the expensive-NO ≈ break-even read replicates. **No pricing action
from either — this is a thermometer, not a refit input.**

**Family signatures (all HYPOTHESIS, < 10 clusters each):** worst excess
KXMLBHIT −$147.24 (n=34) and pure-KS −$113.71 (n=45); best GAME×TOTAL +$146.02
and GAME×KS +$121.41 — the same-game ML×TOTAL correlation-credit watch item from
the 7/31 report keeps winning, still n-thin.

## 4) Fill quality — what he's really asking (Task 4)

118 exchange fills on night tickers, **115 maker / 3 taker**.

- **vs raw-mid consensus (46 with surviving mids): 39 good (85%) / 7 adverse**;
  margin per contract p10 −0.32¢ / median +1.50¢ / p90 +3.45¢, mean +1.14¢.
- **vs our own model: 112/113 positive EV at entry** — the quote gate did its job.
- **Adverse pocket #1 — same-game GAME×SPREAD×TOTAL stacks:** the two worst
  fills (−9.70¢ and −8.49¢/contract below independence fair, entries 80.1¢ and
  89.0¢) are both that template — our correlation credit priced them ABOVE
  raw-mid fair. Watch item, 2 rows.
- **Adverse pocket #2 — the 3 TAKER prints (F5-pickoff-shaped):** all 3 lost:
  `…-DDE1480250A` (57.3ct @48.9¢, −$29.02), `…-76E0985921C` (30ct @57.0¢,
  −$17.61), `…-F25961078E2` (6.56ct @55.7¢, −$3.77 — the night's ONLY
  negative-model-EV entry). Combined **−$50.41 realized vs +$0.38 claimed EV**.
  A maker book printing taker-side and losing 3/3 echoes the
  balance-via-maker-never-taker rule; n=3 (HYPOTHESIS) but worth tracing which
  path crossed.
- **No F5 legs anywhere in the window** — the 7/31 family gate is holding.
- Collection naming trap: 69 rows sit in `KXMVESPORTSMULTIGAMEEXTENDED` but
  **112/113 combos have pure-MLB legs** (+$299.16 realized). The single true
  esports combo (KXLOLGAME legs) lost −$57.91. This morning's −$167.16
  give-back (21 settles after 8/2 04:00Z) is the same MLB-legged flow settling
  late, not an esports pivot.

## 5) Data-integrity notes (blast radius: accounting/monitoring only)

1. **Fills-store gap, 2 rows** (18:10Z, 22:45Z 8/1) — sweep created the ledger
   rows (good: the INCIDENT-C fix held where it was aimed) but the `fills` store
   and its `expected_edge_cc` are silently short; the P1 crash-seam item stands.
2. **Scalar/partial combo settlements exist** (3 tonight, values 90/79/19¢).
   Binary win/lose assumptions in the ledger and any MC payoff model mis-state
   these rows (~$104 reconstruction delta on one row). Small tonight; worth a
   convention row in `NOTES.md` and sweep handling.
3. **3 ledger rows lag reconcile** at read time — reconcile loop latency, not
   loss.

## NEXT STEPS

- **Operator:** no decision forced by this night (verdict: +EV book, 1-in-4
  outcome). Open decisions stand (resume posture, kill_anchor %, C1/C3/C5).
  When the app number ≠ bot number, the app is the ET-calendar-day window.
- **Trace the 3 taker prints** (owner: next build session): which code path
  crossed the book on 8/1 17:13/19:25/19:43Z — maker-only rule says none should.
- **P1 Stage 1 unchanged and re-evidenced from the WIN side:** the night's
  profit overshoot came from the same big near-coin single-combo concentration
  that produced the 7/31 whale loss — per-STRUCTURE net bounds at the
  reservation path remain the top build item.
- **Scalar-settlement convention** (owner: build session, slow-loop only):
  record partial-value combo settles in `NOTES.md`; teach the reconcile sweep
  the non-binary branch.
- **Keep pooling, no refit:** standing calibration continues; cheap-NO split and
  family excesses stay HYPOTHESIS until ≥10 clusters.
