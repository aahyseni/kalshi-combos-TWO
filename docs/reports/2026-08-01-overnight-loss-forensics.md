# 2026-08-01 — Overnight loss forensics (exchange-truth, settled ≥ 7/31 20:00Z)

**Question (operator):** "we got a lot of fills overnight, but still lost money…
it only takes a few of the big ones to ruin a whole night… what type of book did
we have at peak, what was Pbook… I want to know why."

**Method:** GET-only pulls of `/portfolio/settlements`, `/portfolio/fills`,
`/portfolio/balance`, `/portfolio/positions` via the project's signed client
(paced 0.7 s/page); joined read-only (`mode=ro`) against the live store
(`fills`, `rfqs`, `position_ledger`, `decisions kind='quote_sent'/'confirm'`)
and `book_risk_snapshot` / `risk_audit` / `quote_accepted` events from the
7/31–8/1 logs. **No order was placed, cancelled, or modified; the live bot was
not touched.** All times ET.

---

## Verdict (one paragraph, up front)

The night decomposes cleanly into three pieces and only one of them is priced
risk we didn't understand. **(1)** One 368-contract morning fill (5-leg pitcher-Ks
parlay, we took the NO side at 39.1c) lost its full **−$143.89**; our fair said
the parlay was 60.1% to hit and raw leg mids said 60.0% — we were paid market
consensus + 0.84c, the 60% event happened; that is variance at a fair price, but
its SIZE (max loss ≈ 6–7% of bankroll on ONE combo, vs the 1% per-combo policy
anchor) is a concentration-mechanism finding, not a pricing one. **(2)** The
known-bad **F5 pickoff family** (UNKNOWN classification, zero pair rhos, gate
closed 19:04 the same day) settled all three: **net −$59.90** (−$86.60 hit,
+$12.50/+$14.20 missed) vs +$8.51 claimed EV — the F5 tax is now fully realized,
nothing pending. **(3)** Everything else — 41 combos — realized **+$29.03 vs
+$27.46 expected at entry: dead on expectation.** Monte-Carlo on our own entry
fairs puts P(night ≤ −$174.76) = **0.212** (hits: expected 14.55, got 17,
P ≥ 17 = 0.249) — a 1-in-5 night, not a tail event. The book lost as MANY small
bets plus one whale, not as one correlated bet (max leg overlap between losers:
2 combos; losses spread over 12 games). n = 1 night — every variance statement
above is labeled as such, no edge claim is being made and nothing here justifies
a refit. The two real findings are structural: the per-combo concentration seam
(one fill = 6.7× the per-combo anchor) and a crash-window persistence seam
(6 exchange fills never reached the fills store; 2 never got a ledger row).

---

## 1) Reconciliation to the cent (exchange = ground truth)

45 settlements with `settled_time ≥ 2026-07-31T20:00:00Z`:

| item | value |
|---|---|
| premium paid (cost) | $1,743.5969 |
| settlement revenue | $1,569.47 |
| settlement fees | $0.6284 |
| **net realized** | **−$174.7553** (28 wins +$403.56, 17 losses −$578.32) |
| same, window ≥ 23:00Z | 42 settlements, **−$255.95** |
| same, window ≥ 00:00Z 8/1 | 41 settlements, −$263.18 |
| cash balance now | $1,986.13; open positions marked $776.37 (17 tickers, all NO) |

**The morning ledger read ("−$227.48 across 40") was wrong in both directions** —
window mismatch plus ledger blind spots. Per-ticker diff vs exchange:

| ledger defect | detail |
|---|---|
| 42/45 rows match to the cent | ✓ |
| `…-3A732336675` | ledger −$18.6995 vs exchange −$17.4755 (wrong entry 50.20c vs true 46.80c) |
| `…-B5CB5894DEB` | **NO ledger row at all** (a −$40.27 loss, invisible locally) |
| `…-2FAAE634418` | **NO ledger row at all** (+$10.58 win) |
| ledger night total | −$146.29 vs exchange −$174.76 (Δ $28.47) |

Root cause of all three: fills that executed inside the 18:06–19:04 restart
churn / supervisor-KILL windows (see §5). Blast radius: local accounting and
this report's inputs only — pricing/quoting untouched.

## 2) The big losses (|P&L| ≥ $30), repriced at entry

Entry-time fair recovered for **45/45** (25 from `quote_sent` decisions with
`leg_mids_cc`, 2 from `confirm` decisions, 12 from `fills.expected_edge_cc`,
6 from `risk_audit` `candidate_ev_cc` in the logs — the decisions store lost its
rows in the same crash windows). Raw-leg-mid (independence) cross-check
available for the 25 with stored mids.

| combo | P&L | entry (NO) | structure | our P(yes) | raw-mids P(yes) | EV at entry | verdict |
|---|---|---|---|---|---|---|---|
| -4AEA978E501 | **−$143.89** | 39.1c ×368 | 5L Ks parlay, 4 games | 0.601 | 0.600 | +$3.09 | (a) variance at consensus price; SIZE is the finding |
| -202DC006A47 | **−$86.60** | 86.6c ×100 | F5win+team−3spread+opp-Ks-under, DETATH, same-game | 0.099 | lost (no mids) | +$3.52 | (b) F5 family — fair unverifiable, family was UNKNOWN/no rho |
| -E370CD8C53F | −$65.29 | 57.6c ×113 | 2L HIT, 2 games (Pages+C.Young 1+ hit) | 0.417 | lost | +$0.81 | (a) variance (2 near-coin legs both hit) |
| -7084CB88CDC | −$47.50 | 66.9c ×71 | TOR ML + STLTOR total<8, same-game | 0.312 | 0.330 | +$1.36 | (a) variance; our fair ≈ mids-independence |
| -B5CB5894DEB | −$40.27 | 67.4c ×60 | AZ ML+total + 2 cross-game totals | 0.317 | lost | +$0.56 | (a) variance; thin edge |
| -0E503DE126C | −$39.96 | 73.7c ×54 | SD −2 spread + SFSD total<11, same-game | 0.252 | lost | +$0.61 | (a) variance |
| -B22E4404040 | −$37.06 | 43.3c ×86 | MIN Matthews 4+Ks + MINSEA total 5+, same-game | 0.565 | lost | +$0.17 | (a) variance; edge nearly zero at entry |

F5 siblings for completeness: `-E92B7E33C78` (87.5c ×100, BOSLAD) **won +$12.50**;
`-BBA48DA0FAA` (85.8c ×100, WSHATL) **won +$14.20**. The $275.43 held to
settlement by the 7/31 F5 report resolved to **−$59.90 net. Zero F5 exposure
remains** (family blocked from the allowlist since the 19:04 boot).

**Loss split of the night (realized − EV at entry = shortfall):**

| bucket | n | EV at entry | realized | shortfall |
|---|---|---|---|---|
| (a) priced-fine-but-lost variance: the Ks whale | 1 | +$3.09 | −$143.89 | −$146.98 |
| (b) known mispricing: F5 pickoff family | 3 | +$8.51 | −$59.90 | −$68.41 |
| (c) everything else | 41 | +$27.46 | **+$29.03** | **+$1.57** |
| **total** | **45** | **+$39.06** | **−$174.76** | −$213.82 |

**(c) NEW mispricing found: none provable tonight.** The one family to WATCH:
same-game ML+TOTAL combos carry a large correlation credit vs raw-mid
independence (subset of 25 with stored mids: our EV +$16.40 vs independence
+$2.17; extreme rows -3762C19306E our +2.34c vs indep −7.93c, -34C350C278A
+2.44c vs −5.74c). Those combos all WON tonight; independence is genuinely the
wrong baseline for same-game ML×total; n = 1 night ⇒ HYPOTHESIS-class watch
item only, no action from a P&L window.

## 3) Correlation check — did the book lose as one bet?

**No.** Measured on the 17 losers:

- Max leg-level overlap between losing combos: **2** (5 leg pairs shared by
  exactly 2 losers each; no leg in 3+).
- Losses spread over 12 games; biggest per-game attribution (equal split per
  combo): DETATH −$96.66 (the F5 hit + 2), STLTOR −$64.68, MILLAA −$38.25
  (in 5 losers but all small), PITCIN −$35.54, SFSD −$35.12.
- All 45 positions were NO-side (structural: sell-only parlay book) — direction
  is uniform by construction, but the underlying games/legs were not stacked:
  cost-weighted **N_eff over games at peak = 9.29**.
- Realized joint vs model joint: expected combo hits Σ our-fair = **14.55**,
  realized **17** (independent-outcomes MC on our fairs: P(hits ≥ 17) = 0.249;
  P(night P&L ≤ −$174.76) = **0.212**, dist p5 −$367 / p50 +$26 / p75 +$224).
  Nothing in the joint outcome demands extra correlation to explain — and the
  legs the model DID treat as correlated (same-game) were the ones that won.

## 4) Book shape timeline — "what type of book did we have at peak, what was Pbook"

From 3,311 `book_risk_snapshot` events (7/31 06:10 → 8/1 03:41 ET). p_book =
MC P(book P&L ≥ 0); det-max = deterministic max loss; last-in-hour shown:

| hour ET | p_book | EV $ | ES99 $ | det-max$ (hr max) | n_pos |
|---|---|---|---|---|---|
| 7/31 06:00 | 0.424 | +14.58 | 248.92 | 304.43 | 5 |
| 08:00 | 0.405 | +5.90 | 269.80 | 344.74 | 6 |
| 10:00 | 0.402 | +20.80 | 336.35 | 542.06 | 9 |
| 12:00 | 0.406 | +23.39 | 337.04 | 542.06 | 9 |
| (12:28–17:23 bot down) | | | | | |
| 17:00 | 0.331 | −19.91 | 282.16 | 349.13 | 7 |
| 18:00 | 0.344 | −14.77 | 409.65 | 978.26 | 20 |
| 19:00 | 0.700 | +37.22 | 207.02 | 1,147.04 | 21 |
| 20:00 | 0.714 | +35.61 | 196.99 | 1,167.62 | 17 |
| **21:00 (PEAK 21:01)** | **0.719** | **+37.24** | **193.91** | **1,167.62** | **17** |
| 22:00 | 0.601 | +29.09 | 326.28 | 1,050.44 | 16 |
| 23:00 | 0.318 | −62.08 | 380.91 | 1,020.80 | 11 |
| 8/1 00:00 | 0.519 | +6.86 | 428.07 | 1,080.64 | 12 |
| 01:00 | 0.499 | +7.26 | 330.05 | 946.15 | 14 |
| 03:00 | 0.519 | +2.13 | 439.06 | 777.85 | 15 |

**The peak book (8/1 01:01:26Z = 7/31 21:01 ET, det-max $11,676,247cc):**
p_book **0.7189**, p_night 0.9597, EV +$37.24, ES99 $193.91, mutex-aware
det-max $1,105.53, p_ruin 0.0034, realized day P&L at that moment **+$87.80**.
Reconstructed composition (position_ledger open set at that instant):
**39 NO positions, $1,204.78 premium = deterministic max loss** (≈ 44% of
marked equity / 56% of the $2.15k cash-basis bankroll), across **29 games**,
cost-weighted **N_eff 9.29**. Entry-price mix: 2 below 40c, 11 at 40–60c,
**19 at 60–80c, 7 at ≥80c** — i.e. 26 of 39 positions were laying 60–90c to win
10–40c. Top game concentrations: BOSLAD $244.79 (20%), STLTOR $208.26 (17%),
CWSTB $125.14, PHIBAL $100.46, DETATH $96.90 (the F5 hit), SFSD $77.06. Top
tail games per the MC itself: BOSLAD $93.55, STLTOR $36.58, SFSD $31.52.
**That is the answer to "what type of book":** a 72%-to-win, +EV,
expensive-NO-heavy book whose conditional bad branch (~28%) loses in $40–90
chunks — the top-3 realized hits (−$143.89 −$86.60 −$65.29 = −$295.78) exceeded
the whole night's net loss; the other 42 positions collectively made money.

## 5) Data-integrity findings surfaced by the forensics (blast radius: accounting/monitoring only)

1. **Six exchange fills never reached the `fills` store** (22:16–22:58Z during
   the 18:06–19:04 ET restart churn — sessions rotating every ~8 min — and
   02:07:44Z during the 22:09:45 supervisor KILL churn; both KILLs were
   `loop stalled: maintenance age > 30.5s`, plus `halt_marginal_jump` receipt
   at 01:09:35 8/1). The INCIDENT-C sweep DID see them
   (`fills_ledger_missing_exchange_fill` events) and closed 4 into the ledger
   (`settlement_orphan_row_closed`), **but 2 never got a position_ledger row**
   (`position_ledger_settled_unmatched`): -B5CB5894DEB (−$40.27) and
   -2FAAE634418 (+$10.58). The sweep detects; it must also CREATE.
2. **The decisions store lost its `quote_sent`/`confirm` rows in the same
   windows** — entry repricing context (fair + leg mids) was unrecoverable from
   the store for 20/45 positions and had to be reconstructed from
   `fills.expected_edge_cc` and `risk_audit`/`quote_accepted` log lines
   (recovered for all 45; leg-mid independence check limited to 25/45).
3. **Per-combo concentration**: the 368-contract fill carried $143.89
   deterministic max loss on one combo ≈ **6.7% of bankroll vs the 1% per-combo
   policy anchor** — admitted at 10:10:30Z as the FIRST fill of the session
   (book det-max $143.89, n_positions 1, p_book 0.399). Whether the 1% anchor
   is meant to bind premium, EV, or det-max at the reservation path is exactly
   the P1 Stage-1 question; this is the measured exhibit.

---

## ADVERSARIAL GATE (2026-08-01 morning, independent re-derivation) — VERIFIED

The gate re-pulled `GET /portfolio/settlements` and `/portfolio/fills` itself
(read-only, paced 0.7 s/page) and re-derived the night from scratch:

| check | gate's independent read | report's claim | verdict |
|---|---|---|---|
| night window ≥ 20:00Z | **45 settlements, net −$174.76** (rev $1,569.47 / cost $1,743.60 / fees $0.63; 28 wins +$403.58 / 17 losses −$578.34) | 45, −$174.7553 | ✓ to the cent (sub-cent Decimal vs cent-rounded rows) |
| ≥ 23:00Z / ≥ 00:00Z | 42, −$255.96 / 41, −$263.20 | −$255.95 / −$263.18 | ✓ (±1–2¢ sub-cent accumulation) |
| biggest rows | −143.89 / −86.60 / −65.29 / −47.50 / −40.27 / −39.96 — same tickers, same order | identical | ✓ exact |
| 7/31 fills | **49 fills / $2,016.69 premium** | 49 / $2,016.68 | ✓ |
| 8/1 fills | 26 / $717.27 by 06:26 ET (report's 15/$504.77 was the 03:41 read; 11 fills landed since — bot live and filling) | — | ✓ consistent |
| refit check | classification uses ENTRY-time fairs only (`quote_sent` leg mids, `confirm`, `fills.expected_edge_cc`, `risk_audit` `candidate_ev_cc` — all recorded at entry); F5 was classified mispriced by the PRE-settlement 7/31 19:04 gate, not by tonight's outcome; the ML×TOTAL item stays HYPOTHESIS-labeled, no action | — | ✓ no refit on the P&L window |

Gate verdict: **exchange numbers and the variance-vs-mispricing split stand.**
The two structural findings (per-combo concentration seam, crash-window
persistence seam) are carried in NEXT STEPS below and in the exit-forensics
report's census of the same windows.

## NEXT STEPS

- **Operator decision owed:** none forced by tonight's P&L (verdict: 1-in-5
  night at honest prices + the already-gated F5 tax). The open decisions from
  the resume state stand (resume posture, kill_anchor %, C1/C3/C5).
- **P1 Stage 1 (owner: next build session):** per-STRUCTURE and per-game
  net bounds at the reservation path — tonight's exhibit is the 6.7%-of-bankroll
  single-combo whale; validate any new cap can still quote (2026-07-23 rule)
  before arming.
- **Crash-window persistence seam (owner: next build session, isolated from
  pricing):** recovery sweep must create ledger rows for exchange fills with no
  local row (2 tonight), not just log `settled_unmatched`; decisions/fills
  writers should flush before supervisor kill or be re-derived by the sweep.
  Fix isolation rule applies — slow-loop only.
- **Watch item (HYPOTHESIS, n=1 night, no refit):** same-game ML×TOTAL
  correlation credit vs raw-mid independence (+$14 of claimed EV on 25 combos);
  keep scoring in the standing calibration report over pooled multi-week,
  game-clustered data.
- **Ledger correction:** supersede the morning "−$227.48/40" read with
  exchange-truth −$174.76/45 (≥20:00Z) and −$255.95/42 (≥23:00Z).
