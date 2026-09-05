# 2026-09-04 night — "fills a lot slower": measured diagnosis, the rebate-rule retirement, the point-estimate floor, the 2% anchor

Operator (19:15 ET): "we're getting fills a lot slower than before, I'm ok to
raise the per combo, but I'd also like to fill more quantity, to have a more
diverse book"; (20:35 ET): "the bot's fill quality has 100% been depleted,
only $340 in positions after 2 hours is very disappointing". Goal restated:
~70% of cash (~$3.5k) in positions on a given day.

## WRONG / FIXED / OPEN

| Claim | Verdict | Evidence |
|---|---|---|
| "Fills a lot slower than before" | **HALF** | Tonight 14.1 fills/h, 0.65 fills per 1,000 sends at 18:00–20:00 ET = the 8/17–8/19 zero-incident evenings (14.0 / 9.5 / 8.5 per h at 0.54–0.55). **45% below 8/26 only** (27/h, 1.17/1k). $340 by 20:00 sits between 8/18 ($278) and 8/17 ($431); 8/26 had $711. |
| My 19:15 diagnosis: "the cell floor's 3-sigma term muted the rebate" | **WRONG (retracted)** | The cell floor moved 2 of 1,154 replayed quotes. Only 12 recorded quotes carried any rebate at all — it was already gone upstream. |
| The real driver | **FOUND** | Build A's `exposure_backed` rule (risk/rebate_bound.py, the S1 review fix): pairing all 22,151 sends with their `inventory_skew_shadow`, it stripped the leg-axis rebate from **17,119 sends (77%)**, median 0.55¢ unbounded, 0.3–0.5¢ on the wire after the old margin//2 cap — the margin we win auctions by (field clears our fair +0.05–0.25¢, 8/16). Families stripped: KXEPLGAME 14,681, KXLALIGAGAME 8,252, KXBUNDESLIGAGAME 7,072, KXEPLTOTAL 6,314, KXMLBHIT 5,288, KXMLBHRR 3,734, KXMLBHR 3,535 — tomorrow's club slate and MLB props, i.e. the diversifying flow. |
| The fee floor | **DESIGNED, KEPT** | Moved 13.6% of replayed quotes by ~0.17¢, razors only; mains/ladders byte-identical. 25% of post-8/20 fills were negative after the 0.035 fee; this is that retirement. |
| "$340 after 2 hours" | **TRUE, and partly a stall** | The 19:29 boot was supervisor stall-killed at 20:09 ET (`maintenance age 61.2s > 60.5s`, the parked item 6) while a builder's vitals scan read 180 GB off the live store; the watchdog relit it at 20:10 (60 s down). |

## The measured fill record (same clock hours, store fills ≠ phantom)

```
ET hour       tonight 9/4           8/17          8/18          8/19          8/26
18:00-19:00   5, $84 (18:45→)       11, $143      6, $84        12, $205      17, $271
19:00-20:00   8, $237 (→19:48)      17, $288      13, $195      4, $66        32, $441
20:00-21:00   —                     12, $209      8, $67        5, $91        54, $448
22:00-23:00   —                     25, $468      9, $149       10, $172      12, $247

night                 sends 22-23Z   accepts   fills/1k sends   sends/min   fills/h
9/4 (22:45-23:49Z)    23,206         15        0.65             363         14.1
8/17                  50,727         28        0.55             423         14.0
8/18                  34,845         19        0.55             290          9.5
8/19                  31,602         17        0.54             263          8.5
8/26                  46,236         54        1.17             385         27.0
```

Tonight's slate was not thinner (12 RFQ-touched MLB games + LigaMX/MLS; 8/17
had 9, 8/26 10 evening games). Direct print-loss attribution is impossible on
this store: `combo_trades` and `rfq_deletions` have 0 rows lifetime (the tape
loop is recorder-app only). Poisson on 15 fills is ±26%, so the −45% vs 8/26
is consistent with (fee retirement 25%) + (rebate stripped on 77% of sends).

## Repairs shipped tonight (all gated, all pushed)

1. **Per-combo anchor 1% → 2%** (operator ratified; local yaml
   `structure_loss_frac 0.02` with the ruling quoted; per_combo belt already
   0.02). Restart 19:29 ET; structure threshold $102.21/combo market; the
   structure cap vanished from the decline census (7,443 refusals in the
   first 19 min at 1% → 0). Sized from the tape: ~9.6k RFQs/hour tonight in
   ($49.80, $99.60], mean ~$70–75, 96% cross-game, half MLB / half club
   soccer, half ML-only parlays → ~6 extra fills/h (~$460/h) at the observed
   win share; 33–72 fills/day ($2.0k–$4.3k/day) on the 8/18–8/19 full days.
   Upper bounds: that band is the whale-size class (8/16: 5.9% of RFQs carry
   73.8% of requested $; whale −29% ROI in ≥65¢ props).
2. **Retained-edge floor → shrunk POINT shortfall** (branch
   `build/floor-point-estimate`, merged `7cdd6ef`; builder + adversarial
   review + fix pass; 4,045 tests; vitals fast 8/8 from a store SNAPSHOT —
   new `tools/vitals/snapshot.py`, because `derive.py` rescans the whole 266
   GB tape on a live box). No z·SE term; thin cells take the sport pool's
   point (mlb 0.85¢, soccer/esports/other 0); losing cells keep their
   measured loss (rfi×rfi cross 15.4¢; the ML×ML cross-game class 1.7¢ on a
   noisy record, |post|/SE 0.29 — WATCH). Rebate room on 67.5% of tonight's
   replayed quotes vs 3.6% under the shipped bound.
3. **`exposure_backed` rule RETIRED** (`671b1c2`): `bound_rebate` passes
   the rebate through under rule `measured_floor`; `construct_quote` bounds
   it by the measured per-cell floor (rebate ≤ margin − fee − measured
   adverse selection), the ES-value cap still applies once the concentration
   steer has PRICED the candidate; `unbacked_cc` stays as telemetry. Two
   pins re-pinned with the citation. Gates: touched 142/142, suite 4,045/0,
   vitals fast 8/8 (snapshot). Restart 21:03 ET.

## Boot 21:03 ET and first window (01:03–01:10Z)

| Check | Value |
|---|---|
| retained_floor_estimate | rule `shrunk_point`, 656 cells / 450 thin, pool floors mlb 85 cc / soccer 0 / esports 0, median 0.85¢ (was 5.9¢), losing 88 / at-fee 118 / sign-unresolved 61 |
| Rebate on the wire | **51% of skew records carry a rebate, mean 0.45¢** (18:45 boot: ~0%; rule `measured_floor` 174 / `none` 93 in the first 269) |
| Fee schedule | observed 0.0350, 558 charged fills, 0 mismatches |
| Startup reconcile | 19 leftover quotes withdrawn |
| Sends/min | 284–492 |
| Fills | 0 in 6.5 min (expected ~1.5 at 14/h; P(0) ≈ 22%; 21:10 ET = most MLB in play, pregame-only quoting) |
| Declines | per_combo 3,371 · size 1,936 · entity 42 · directional 2 · **structure 0** |
| Errors / 400 / halts | 1 (see log) / 0 / 0 |
| Tonight total (store) | 19 fills, $467.31 premium, net edge $4.66, fees $6.40 |

Tonight's restarts: 18:45 (relight after the repairs), 19:29 (2% anchor),
20:10 (watchdog after the 20:09 stall-kill), 21:03 (floor point + rebate
retirement). Each cost ~2.5 min of quoting plus the resting quotes it swept.

## What the numbers say about the 70% goal

Net edge after fees on tonight's 19 fills: $4.66 on $467 (1.0%). At that
margin $3.5k of positions is ~$35/day of EV against $500–900 day swings.
Deployment × margin is the EV; the margin lever (retain more on the favorite
band where 88% of auctions expire unfilled) is the pooled read still owed.
Diversity: tonight's book is 10 positions across 14 games / 13 leg families /
6 leagues, top-3 games 45% of premium — the rebate restoration re-enables the
club-soccer and MLB-prop families that were being stripped.

## NEXT STEPS

- Read at 4+ hours of quoting (tomorrow's club slate is the real test):
  fills/1k sends vs the 0.55–0.65 baseline; share of sends carrying a rebate
  (expect ~50–65%); `retained_floor_estimate` refresh cadence
  (`retained_floor_sweep_timeout` fired 3× tonight on the 213 GB store);
  `fee_schedule_mismatch` 0; `fill_verified_late` 0; the 2% band's fills
  and their realized-vs-model by counterparty (whale class).
- Pre-registered: the ML×ML cross-game cell's shrunk shortfall trajectory
  (−1.7¢, SE 6¢); 61 sign-unresolved cells; the losing-cell list.
- Owed: ledger stale-row P1 (339 stale open rows at the snapshot — retires
  the pre-ship V6 artifact, 6th ship-through tonight); hierarchical variance
  prior for 2-cluster cells (O3); persist the floor table across boots (O1);
  derive.py manifest over closed logs (O5 proper).
- Parked by operator: stall wall (item 6 — it fired again tonight), store
  rotation (item 7), NFL.
