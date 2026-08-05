# 2026-08-05 — Bleeder dissection: where every lost dollar actually lives

**Operator ask (verbatim):** "be very detailed with the bleeders, i want to
clearly see where we're losing the money, disect it."

**Corpus:** the 8/5 full-P&L-sweep era table — 661 settlements ≥ 7/28 00:00 ET,
exchange-truth **+$455.03** (re-derived from the cached exchange pull to the cent
before any cut below; gross = **393 winners +$4,995.45, 268 losers −$4,540.42**).
Method: read-only; the sweep's cached exchange/master/db extracts reused; the only
new API traffic = **14 paced batched `GET /markets` pulls** to settle the 1,321
unique era leg tickers (748 yes / 553 no / 19 scalar / 1 active); one `mode=ro`
scan of `decisions` for the leg-mid tape (831,741 quote_sent rows, 2.86M leg-mid
points). No live module touched, no order placed. Scripts: session scratchpad
`dissect/` (`a_tags`, `b_fetch_legs`, `c_mech`, `c2b_calib`, `d_stale`,
`e_cells`, `f_probe`). Cluster bootstrap = GAME components, same union-find as
the sweep. Integrity gate: AND(leg results) == combo result on all 628 fully
binary rows, **0 mismatches**.

---

## THE 60-SECOND TABLE

Disjoint $ = the mechanism-first greedy partition of the −$4,540.42 gross losing
P&L (each lost dollar exactly once; symptom cells claim only what no mechanism
cell already owns). Net/excess = the whole cell, winners included.

| cell | disjoint gross-loss $ | cell net / excess $ | mechanism verdict | action class |
|---|---|---|---|---|
| **pure-KS** | −830.58 | −167.88 / −229.10 | **[MEASURED-STRUCTURAL] MARGINAL bias, NOT correlation**: K-legs hit taker's side +6.4 pts over our leg mids; joint z +2.58 vs model collapses to **+0.34** under bias-corrected independence; 0 same-pitcher ladders; cross-game carries it | prop-leg marginal input build (external devigged prop odds / leg-family direction tracking — the 7/23 player-gap enhancement) |
| **5+ legs** | −252.69 (after KS/HR/SPREAD claims) | −384.29 / −454.45 | **[MEASURED-STRUCTURAL] same marginal bias COMPOUNDING × legs + 3 whales**: 4+-leg joint z +2.15 → **+0.54** bias-corrected; worst-3 tickets −$291.97 of the −$454.45 | same marginal build **+ P1 per-structure bounds** (whale share) |
| **pure-SPREAD** | −221.19 | −154.06 / −155.40 | **[HYP n=7, cl=2] ONE template**: cross-game all-NO spread-3/4 "anti-blowout baskets", 6/7 on the 8/1–8/2 slates, quoted at **+$1.34 total EV on $315.78** (≈0.4¢/$1 — near-zero markup captured); legs hit +6.6 pts over mids | same class; template pre-registered, watch |
| **pure-HR** | −100.92 | −91.51 / −95.74 | [HYP cl=4] 1W/7 vs 3.6 model (z −2.35); two star-hitter all-NO HR ladders (8-leg, 11-leg); HR leg bias +8.5 pts — same side-picking shape | same class |
| **HIT-any** | −743.46 (mostly 60–80¢ book variance) | +44.35 / −44.01 | **[VARIANCE]** count z +0.06 (calibrated); excess is 3 whale tickets (−$187.20; rest of cell +$143) | keep quoting |
| **40–60¢ band** | −679.88 residual after all mechanism cells | −132.29 / −190.75 | **[MEASURED-STRUCTURAL] SYMPTOM, not a cell**: band-only rows (no structural tag) = **+$131.30 real / +$141.76 excess**; the band's bleed is its pure-KS/SPREAD/5+ constituents double-counted | kill the lens; no band effect exists |
| **$30–60 size** | −613.02 residual | −165.40 / −192.65 | **[MEASURED-STRUCTURAL] SYMPTOM**: minus structural members = **+$203.34 real / +$150.86 excess** (n=48) | kill the lens |
| **stale 10–60 s** | −185.62 residual | −67.45 / −82.55 | **[REFUTED + VARIANCE]** adverse drift disproven: 32/32 recoverable, mean side-adj leg-mid drift **+0.004¢**, 30/32 flat; minus structural members +$3.74 real | keep quoting; quote-age persistence build stands (398/661 rows still lack the axis) |
| 80–92¢ | −205.35 | −50.09 / −28.40 | [HYP, VARIANCE-shaped] all 10 losers ≥80¢ entered +EV; tail joint z +0.95 | keep; watch |
| 92¢+ | −19.32 | −23.14 / −32.77 | [HYP n=6, cl=4] count z **−3.74**: two ~4% tails hit in 6 tries (P≈2.4%) — the only cell outside model variance on counts | pre-registered tail watch (with 80–92 sign) |
| taker/recovery | −67.88 | −19.90 / −18.50 | [MEASURED-STRUCTURAL, small] adverse by construction; **1.5% of era prints, ≈$15/day premium**; the ONLY cell with negative aggregate entry EV (−$1.40) | reporting tag build (sweep #4); never blocks recovery |
| invisible settles | −112.69 | −94.07 / n/a | [MEASURED-STRUCTURAL] accounting seam (sweep #3 build) | INCIDENT-C sweep extension |
| residual (no tag) | **−507.83** | — | **[VARIANCE]** 47 losers, all 60–80¢/<40¢ 2–4-leg — the cost of inventory in the band that makes +$663 | keep quoting |

**The one-line story: the book has exactly one live pricing defect — prop-leg
marginals (KS/HR/HRR/SPREAD/RFI) are beatable by the taker's side-pick by +3 to
+9 pts, and parlays compound it; every other "bleeder cell" is that same money
double-counted, whale variance, or an accounting seam.** Tickets with <2
prop-family legs made **+$560.92 real / +$492.44 excess**; tickets with ≥2 made
−$105.88 / −$260.45; the prop-pure ∪ 5+-leg union alone is **−$426.13 real /
−$541.69 excess** (≈ −$60/settle-day at current throughput).

```
 taker picks a prop side            our leg mid = the marginal input
        │                                     │
        ▼                                     ▼
  side-adjusted hit-rate bias (hit% − mid, era legs, settled):
    KS +6.4   HR +8.5   RFI +8.8   SPREAD +6.3   HRR +4.0   HIT +3.3   ← they beat us
    GAME −8.3   TOTAL −5.7                                              ← we beat them
        │
        ▼  joint = Π(legs)  →  bias compounds with leg count
  2-leg  z −0.33 (fine)   3-leg z −0.10 (fine)   4+ legs z +2.2 → +0.5 corrected
        │
        ▼
  bleed concentrates exactly where prop legs stack:
  pure-KS, pure-SPREAD baskets, pure-HR ladders, 5+ legs
  (correlation model EXONERATED: fair ≈ Π(mids) on pure-KS — corr credit +0.1 pt/ticket;
   cross-game independence was the right call, and no same-pitcher ladder exists in the era)
```

---

## 1) Disjoint attribution — each lost dollar exactly once

Universe: 268 losing tickets, −$4,540.42 gross. Two partitions of the SAME
dollars (both sum exactly; script `a_tags.py`/`f_probe.py`):

**(a) Mechanism-first greedy** (specific structural tags claim first, symptom
tags only get what's left):

| claim order | n | $ | note |
|---|---|---|---|
| INVIS | 4 | −112.69 | accounting seam |
| TAKER | 4 | −67.88 | recovery prints |
| PSPREAD | 4 | −221.19 | the anti-blowout template |
| PHR | 6 | −100.92 | HR ladders |
| PKS | 65 | −830.58 | the core prop-marginal bleed |
| L5P (5+ legs, non-pure) | 16 | −252.69 | mixed-family stacks |
| HITany | 52 | −743.46 | mostly 60–80¢ variance (cell is net +$44) |
| STALE | 6 | −185.62 | drift refuted; double-count |
| SZ3060 | 14 | −613.02 | symptom |
| B4060 | 44 | −679.88 | symptom/variance |
| B8092 / B92P | 5 / 1 | −205.35 / −19.32 | thin tails |
| **residual (no bleeder tag)** | **47** | **−507.83** | 60–80¢ −$451.20 + <40¢ −$56.63; by legs: 2-leg −$348.76, 3-leg −$100.94, 4-leg −$58.13 |

**(b) Exact tag-set partition** (the overlap table, top cells — this is where
the double-counting the sweep warned about becomes visible):

| tag set | n | $ | tag set | n | $ |
|---|---|---|---|---|---|
| B4060 only | 44 | −679.88 | PKS only | 21 | −135.60 |
| (none) | 47 | −507.83 | STALE only | 3 | −132.31 |
| B4060+SZ3060 | 8 | −351.44 | PKS+SZ3060 | 3 | −122.38 |
| SZ3060 only | 6 | −261.58 | HITany+SZ3060 | 3 | −118.84 |
| B4060+HITany | 27 | −225.49 | B4060+PKS | 18 | −117.04 |
| B8092 only | 5 | −205.35 | B4060+L5P | 6 | −110.75 |
| HITany only | 15 | −204.33 | B4060+HITany+SZ3060 | 3 | −100.96 |
| PKS+L5P | 4 | −165.07 | B4060+PSPREAD(+SZ) | 3 | −172.40 |

Largest pairwise overlaps (gross losing $ in both cells): B4060∩SZ3060 −$835 ·
B4060∩HITany −$396 · B4060∩PKS −$383 · PKS∩L5P −$326 · B4060∩L5P −$311 ·
HITany∩SZ3060 −$276 · PKS∩SZ3060 −$258 · B4060∩PSPREAD −$221. The headline
"bleeder cells" of the sweep share most of their dollars — the aggregate list
overstates distinct problems by roughly 3×.

## 2) The mechanism, proven three ways [MEASURED-STRUCTURAL]

**(i) Per-leg marginal calibration** (all era legs, side-adjusted to the side
the taker's parlay stated; instance = leg within a settled combo; `c_mech.py`):

| family | inst | mean mid | hit% | bias | uniq-leg bias | z (uniq) |
|---|---|---|---|---|---|---|
| KXMLBKS | 312 | .837 | .901 | **+.064** | +.048 | +1.8 |
| KXMLBTOTAL | 182 | .805 | .747 | **−.057** | −.068 | −2.2 |
| KXMLBHRR | 117 | .704 | .744 | +.040 | +.021 | +0.4 |
| KXMLBHIT | 106 | .655 | .689 | +.033 | +.005 | +0.1 |
| KXMLBGAME | 75 | .550 | .467 | **−.083** | −.053 | −0.8 |
| KXMLBHR | 37 | .807 | .892 | +.085 | +.085 | +1.5 |
| KXMLBSPREAD | 37 | .694 | .757 | +.063 | +.066 | +0.9 |
| KXMLBRFI | 15 | .512 | .600 | +.088 | +.104 | +0.8 |

Takers' prop-side picks beat our leg mids; their game-level picks (GAME/TOTAL)
lose to them — which is precisely the sweep's family P&L sign pattern
(GAME×TOTAL +$268 excess vs prop-pure cells negative). KS by line: bias worst at
the extremes (yes-6: +27.7 pts n=8; no-6: +12.8 n=8; yes-3: +6.6 n=47) — a
line-tail bias, both over AND under directions. Caveat, stated plainly: this is
measured on our filled book, so it is indistinguishable from "takers select the
moments our prop mids are wrong" — but that IS adverse selection against the
marginal input; either reading indicts the same input.

**(ii) The joint-vs-marginal decomposition** (`c2b_calib.py`). Pure-KS, 55/113
tickets with full mids+results:

| expectation for joint hits | value | z vs actual 39 |
|---|---|---|
| model (our fair) | 29.8 | **+2.58** |
| independence × raw mids | 29.7 | +2.59 |
| independence × decile-CALIBRATED K mids | 37.9 | **+0.34** |

Model fair ≈ product of raw mids (corr credit +0.1 pt/ticket) — the correlation
layer adds ~nothing here and needed to add ~nothing (cross-game). The whole
overshoot is the marginal. Same collapse on any-2+-K tickets (+2.72 → +0.47)
and on ALL 4+-leg tickets with all-family bias correction (+2.15 → **+0.54**).
Same-pitcher-ladder count in the era: **zero** tickets. Same-game pure-KS is
actually POSITIVE (+$34.48 excess, n=16); cross-game carries the bleed
(−$263.58, n=97, P(exc≤0)=0.80). Repeat losers are cross-ticket pitcher
exposure, the 7/23 player-level-gap shape: NYY Schlittler (8 legs, 4 lines,
alloc −$84), PHI Nola (−$39), CLE Cantillo (−$36), MIN Ryan (11 legs, −$31);
biggest winners the same shape the other way (Wheeler +$72, Bieber +$46).

**(iii) The leg-count curve** (`c_mech.py` C3) — excess does NOT grow smoothly
with legs; it appears where prop legs stack and where whales sit:

| legs | n | joint act vs model (z) | real $ | excess $ | excess/ticket | P(exc≤0) |
|---|---|---|---|---|---|---|
| 2 | 346 | 56 vs 57.5 (−0.27) | +652.81 | +474.58 | +1.39 | 0.057 (10 cl) |
| 3 | 140 | 22 vs 21.7 (+0.08) | +290.05 | +280.28 | +2.03 | 0.189 (11 cl) |
| 4 | 84 | 17 vs 12.8 (+1.61) | −9.46 | −68.42 | −0.82 | 0.665 (9 cl) |
| 5 | 34 | 11 vs 8.2 (+1.42) | −365.69 | −405.14 | **−12.28** | 1.000 (7 cl) |
| 6 | 18 | 4 vs 3.5 (+0.35) | −28.28 | −38.14 | −2.12 | 0.927 (6 cl) |
| 7+ | 32 | 6 vs 5.0 (+0.59) | +9.68 | −11.18 | −0.35 | 0.627 (6 cl) |

Combined 2–3 legs z −1.16 (calibrated); combined 4+ z +2.53 → +0.54 after
marginal-bias correction. Prop-family leg share rises 65% → 76% → 80% with leg
count (KS alone 18% → 29% → 39% → 46%). The 5-leg $ cell is additionally
whale-shaped: worst-3 tickets −$291.97 of −$454.45 (KS −143.89 @368ct,
TOTAL −76.28, HRR −65.14); without them −$162.48. So: **compounding marginal
bias is real (count-based, z-collapse proof) and the DOLLARS are amplified by
the unbounded-structure whale seam P1 Stage-1 already owns.**

## 3) Per-cell dissection (constituents, luck split)

Luck-split table (count z = wins vs model-implied; negEV$ = EV of tickets
entered −EV — the "pricing defect at entry" bucket, which is ~empty: only **3
tickets era-wide entered −EV, −$1.80 total**, all on the recovery path):

| cell | n | W vs expW | z_cnt | EV$ | real$ | excess$ | P(exc≤0) | cl |
|---|---|---|---|---|---|---|---|---|
| B4060 | 285 | 149 / 148.6 | −0.32 | +102.52 | −132.29 | −190.75 | 0.717 | 8 |
| B8092 | 42 | 34 / 35.2 | −1.09 | +50.71 | −50.09 | −28.40 | 0.608 | 8 |
| B92P | 6 | 4 / 5.8 | **−3.74** | +9.63 | −23.14 | −32.77 | 0.939 | 4 |
| PKS | 113 | 44 / 53.0 | −1.78 | +61.82 | −167.88 | −229.10 | 0.785 | 8 |
| PSPREAD | 7 | 3 / 3.7 | −0.55 | +1.34 | −154.06 | −155.40 | 0.750 | 2 |
| PHR | 7 | 1 / 3.6 | −2.35 | +4.24 | −91.51 | −95.74 | 1.000 | 4 |
| HITany | 149 | 95 / 91.6 | +0.06 | +52.70 | +44.35 | −44.01 | 0.589 | 7 |
| L5P | 84 | 44 / 51.7 | −2.09 | +51.01 | −384.29 | −454.45 | 1.000 | 7 |
| SZ3060 | 83 | 49 / 49.3 | −0.56 | +84.17 | −165.40 | −192.65 | 0.665 | 11 |
| STALE | 32 | 19 / 18.6 | +0.15 | +15.10 | −67.45 | −82.55 | 0.745 | 8 |
| TAKER | 5 | 1 / 2.6 | −1.46 | **−1.40** | −19.90 | −18.50 | 1.000 | 2 |
| INVIS | 7 | 3 / — | — | — | −94.07 | — | — | — |

Reading: nowhere is the book buying −EV at entry (the entry gate works); the
bleed is entirely (a) the prop-marginal input error surfacing as negative excess
in prop-stacked cells, and (b) whale variance. Cells whose z_cnt ≈ 0 with big
negative excess $ (HITany, SZ3060, B4060, STALE) are dollar-weighted whale
noise, not miscounting.

**pure-KS top losers** (all +EV at entry; full ranked lists in `e_cells.py` output):

| ticker | date | price | q | prem | fair(p_yes) | EV | real | legs | games |
|---|---|---|---|---|---|---|---|---|---|
| …A9-4AEA978E501 | 8/1 | 39.1¢ | 368.0 | $143.89 | .601 | +3.09 | **−143.89** | 5 | 4 |
| …D1-569198FC553 | 8/4 | 13.1¢ | 569.8 | $74.65 | .868 | +0.40 | −74.65 | 2 | 2 |
| …0E-CEB0C0551E9 | 7/30 | 30.2¢ | 173.0 | $52.25 | .695 | +0.59 | −52.25 | 2 | 2 |
| …25-7BE5B33ABA1 | 8/2 | 30.2¢ | 131.0 | $39.56 | .697 | +0.08 | −39.56 | 3 | 2 |
| …4F-661D045C084 | 8/5 | 51.6¢ | 69.8 | $36.01 | .468 | +1.10 | −36.01 | 6 | 5 |
| …76-B075B0DDF2D | 8/4 | 42.4¢ | 84.3 | $35.73 | .524 | +4.39 | −35.73 | 5 | 3 |

**pure-SPREAD — all 7 tickets** (the NEW cell, fully listed; hits=[..] = each
leg settled on the taker's stated side):

| ticker | settled | price | prem | EV | real | legs (all side=NO = "no blowout") |
|---|---|---|---|---|---|---|
| …5F-47495A714A8 | 8/2 | 47.0¢ | $85.07 | +0.43 | **−85.07** | ATL-3:no, NYY-3:no — hits [1,1] |
| …A8-06684F30557 | 8/2 | 49.2¢ | $49.62 | +0.05 | −49.62 | BAL-2:no, PIT-2:no — [1,1] |
| …13-112C374288C | 8/2 | 55.9¢ | $48.78 | +0.09 | −48.78 | 5 games ×(-4):no — [1,1,1,1,1] |
| …59-A9052746523 | 8/2 | 55.7¢ | $37.71 | +0.04 | −37.71 | 4 games ×(-3/-4):no — [1,1,1,1] |
| …0D-6923833B452 | 8/2 | 44.4¢ | $7.55 | +0.12 | +9.45 | 3 games — [0,1,1] |
| …7B-589C4EB2478 | 8/2 | 55.3¢ | $23.81 | +0.04 | +19.25 | 4 games — [1,1,0,1] |
| …E2-3C8C7D03B4F | 8/5 | 62.2¢ | $63.24 | +0.57 | +38.43 | 4 games — [1,1,0,1] |

Structural read: one repeating TEMPLATE — cross-game all-NO spread baskets
("no team wins by 3+/4+ tonight"), 6 of 7 settling 8/2 off the 8/1–8/2 slates
(cl=2 → statistically ~one observation), and the model handed them essentially
ZERO edge (+$1.34 on $315.78 = 0.4¢/$1 vs book average 2.5¢/$1) — near-zero
markup on a same-shaped, plausibly same-taker flow whose legs beat mids by
+6.6 pts. [HYP] on the P&L; the near-zero-markup fact is [MEASURED] and is the
actionable part: these were the thinnest-edge structures in the era book.

**pure-HR — all 7**: the two big losers are all-NO star-hitter HR ladders
(8-leg: Alonso/Schwarber/Guerrero/Lindor/Crow-Armstrong/Arozarena/Machado/Betts
−$62.00; 11-leg incl. J.Ramírez/Witt/Freeman/Betts/Tatis −$19.94 on $25.31).
"No star homers tonight" × 8 = the same compounding-bias shape at the slugger
tail (HR leg bias +8.5 pts).

**80¢+ — all 10 losers ≥80¢ individually** (48 rows ≥80¢ era): biggest
−$86.60 (3-leg F5/KS/SPREAD 86.6¢, 8/1, EV unrecovered), −$78.87 (RBI/TB 2-leg
80.7¢), −$65.14 (5-leg HRR 80.0¢), −$19.32 & −$14.46 (the two 92¢+ tail hits,
p_yes .038/.041), then five ≤$13. All with recovered EV entered +EV; joint
tail z +0.95 (n=46) = within model variance; 4 of the 10 have qage <15 s
(fresh — no stale-overlap story). The 92¢+ count z −3.74 (two 4% tails in 6) is
the one tail datum outside variance: pre-registered watch with the 80–92 sign,
[HYP] at n=6/cl=4 — consistent with the sweep's expensive-NO reversal, not yet
actionable evidence.

**Stale 10–60 s — REFUTED as a pickoff class.** Built the leg-mid tape from all
831,741 quote_sent decisions since 7/27 (2.86M points, 13,181 legs) and measured
side-adjusted leg-mid drift from filled-quote birth → fill for all 32 rows
(100% recoverable, 120 s tolerance): mean **+0.004¢/leg**; 30/32 flat, 1
adverse (+0.15¢ on a $12.90 loser), 1 favorable. Losers' mean drift +0.012¢ —
there was nothing to see; pregame mids just don't move on 10–60 s scales.
Control (<10 s, 231 rows): +0.001¢. The cell's −8.2% ROI is its structural
members (KS −74.65, RFI −95.50 zero-drift losers) + variance: minus structural
members the cell is **+$3.74 real / −$3.41 excess**. The Aranda/pickoff
hypothesis for THIS cell is dead; the quote-age persistence build keeps its
value for the 398/661 rows with no axis at all.

**Recovery/taker prints — quantified.** 11 era prints / 9 settle days
(**1.2/day, 1.5% of era prints**, $132.19 premium ≈ $15/day). Settled: 5
tickers, −$19.90 realized on ~$103.70 premium with entry EV −$1.40 — the only
negative-entry-EV cell in the book (adverse by construction: the print that
"returns from the dead" is the one the market moved through). Expected ongoing
cost at current rates: order **$2–4/day** ([HYP] — 5 settled tickets; the 6
unsettled 8/5 prints all carry negative store edge, −44 to −470 cc). Verdict:
material enough to TAG in reporting (persist `is_taker` + recovery-time edge,
already sweep improvement #4), nowhere near material enough to touch recovery
itself — counting all positions stays constitutional.

**Invisible 7** (−$94.07 net: 4 losers −$112.69, 3 winners +$18.62): all
esports-collection tickers, fills 7/31 22:16Z → 8/4 11:19Z, zero store/ledger
rows — accounting seam, already the sweep's build #3; nothing new here except
the loser/winner split.

## 4) KEEP / KILL verdicts

| bleeder | verdict | mechanism class (no knobs) | $ at stake (era) |
|---|---|---|---|
| pure-KS | **MODEL ERROR (marginal input) — measurement fix identified** | prop-leg marginal quality: external devigged prop odds behind `OddsSource` + leg-family direction/concentration tracking (7/23 player-gap build); the correlation model is exonerated | −229 excess |
| 5+ legs | **MODEL ERROR (same, compounding) + whale seam** | same marginal build; whale share → **P1 Stage-1 per-STRUCTURE bounds** (already decision #2) | −454 excess (−292 = 3 whales) |
| pure-SPREAD | **[HYP] same class + near-zero-markup template [MEASURED]** | pre-register template "cross-game all-NO spread basket"; the 0.4¢/$1 edge on $316 premium is the measured seam | −155 excess |
| pure-HR | [HYP] same class (slugger-ladder tail) | pools with the prop-marginal build | −96 excess |
| HIT-any | **VARIANCE — keep quoting** | whale-shaped, count-calibrated | — |
| 40–60¢ band | **KILL THE LENS** — no independent effect (band-only rows +$142 excess) | symptom of the above | 0 independent |
| $30–60 size | **KILL THE LENS** (minus structural: +$151 excess) | symptom | 0 independent |
| stale 10–60 s | **VARIANCE — pickoff REFUTED by the tape** | keep the quote-age persistence build for coverage | 0 independent |
| 80–92¢ / 92¢+ | VARIANCE [HYP] / tail WATCH [HYP] (z −3.74 on n=6) | pre-registered; ≥10 clusters before any read | −61 excess combined |
| recovery prints | **STRUCTURAL, small** — reporting-tag build only | persist `is_taker` + recovery-edge; never gates recovery | ≈$2–4/day |
| invisible settles | STRUCTURAL — accounting | INCIDENT-C sweep extension (sweep build #3) | −94 real |
| residual −$508 | VARIANCE — the profitable band's inventory cost | none | — |

**[DECISION] owed by operator (unchanged ranking from the sweep, now sharpened):**
the dissection does not add a new decision — it consolidates the sweep's #6/#7
leg-count/family watches into ONE build class (prop-leg marginal quality +
player/entity-level direction tracking, the 7/23 gap) and removes two false
cells (band, size) and one false mechanism (stale pickoff) from the worry list.
Utilization-backstop repair and P1 Stage-1 remain #1/#2; nothing here reranks
them — P1's case is strengthened (the whale share of the 5+ cell).

**No refit performed or recommended from any P&L cut above.** The marginal-bias
finding is a structural measurement (settled-leg calibration against quoted
mids), not a P&L-window fit; any promotion goes through the standard
prototype-in-test → port → parity path with ≥10-cluster gates.

## NEXT STEPS

- **Operator:** no new decision owed by this report; standing queue unchanged
  (utilization backstop → P1 Stage-1 → accounting sweep; resume posture /
  kill_anchor / C1-C3-C5 still open). If ratifying builds, the prop-leg marginal
  build folds into the P1 entity-axis stage naturally (same leg-family
  direction/concentration tracker).
- **Build session (rule-8 isolation):** (1) prop-leg marginal quality
  measurement harness — nightly settled-leg calibration by family × line ×
  side vs quoted mids (extends `tools/`), gate at ≥10 clusters before any
  pricing change; (2) the sweep's builds #3/#4/#5 unchanged (invisible-settle
  sweep, `is_taker`+recovery-edge persistence, quote-age persistence).
- **Watch (pre-registered):** the anti-blowout SPREAD template; 92¢+ tail
  (two 4% hits in 6); the 6 unsettled 8/5 recovery prints; GAME×TOTAL at the
  10-cluster gate.
- **Blast radius:** analysis only — no live module, config, or order touched;
  live bot untouched throughout; 14 paced GETs + one `mode=ro` decisions scan.
