# 2026-08-12 — Loss-day forensics + KILL-budget audit: why ~10% days happen far more often than "2%"

**Operator question (verbatim):** "since the bot has more cash to work with now,
our Pbook should only be going higher and higher, although we still have some
pretty rough losing days, we've has like 2-3 days where we lost 10%ish, which
should only be about a 2% or so chance given our current risk engine, it seems
unlikely for that to happen 2-3 times now given it has such a low chance, the
more cash we have should increase Pbook and profit as well, and it would go up
exponentially. look into recent trading days, pnls, and settlements, and
analyze whats going on"

**Method:** read-only throughout (live bot untouched — it was relit at 08:49 ET
this morning after the Windows-Update outage and is quoting). Exchange
settlement ledger (paced GETs) for the full-era daily table; store
`mode=ro` entry-EV joins (`fills.expected_edge_cc` identity, formula verified
against the 8/6 precedent ticket); 16.63M `risk_audit` rows + 32k
`book_risk_snapshot` rows extracted across the week's logs (extraction regex
validated 12,952/12,952 on a control file). No refit; entry EVs are entry-time
records only.

---

## 60-second verdict

| operator claim | verdict |
|---|---|
| "2-3 days where we lost 10%ish" | **CONFIRMED — exactly 3 in 23 trading days**: 7/23 −8.6%, 8/2 −8.2%, 8/11 **−17.1%** (worst ever, −$917.00 on $5,354 start-of-day equity) |
| "should only be about a 2% chance given our current risk engine" | **WRONG — the 2% budget is currently a SHADOW NUMBER.** The engine *measures* P(night loss ≥ 12% bankroll) on every 15s snapshot (`p_kill_night`) and it ran **0.25–0.50 at evening peaks every big-slate day this week** (max 0.499). The gate that would enforce ≤2% at 12% (`kill_anchored_book_gate`) has been **disarmed since 8/1** (it froze quoting on the inherited book; your sunk-book ruling; the marginal re-arm is still staged behind the acceptance-seed fix). The ruin gate is stood down (budget 1.0, 8/1) and the daily-loss halt disarmed (your 8/5 ratification). The armed tail gate binds at **35% of bankroll**, and its decline reasons fired **0 times in 16.63M candidate evaluations**. At measured p_kill 0.2–0.4/night, a ≥12% losing night every ~3–5 slate days is the *expected* rate — 3 in 23 days is exactly on schedule. |
| "Pbook should only be going higher and higher with more cash" | **NOT ON TAPE.** p_book median hovered **0.40–0.50 every full day this week** regardless of bankroll (8/5's 0.63 was the only rich day). More cash bought a *bigger* book (median positions 31 → 102, det-max $2.0k → $3.65k, kill line $375 → $638 — everything auto-scaled as designed), not a higher P(book): the diversification gain is spent taking more same-slate correlated exposure. |
| "profit… would go up exponentially" | **Half right.** Equity compounds ($2,000 → $4,708 in a month) because caps scale off bankroll. But variance scales the *same way*: expected EV is ~$60–150/day while daily swings run ±$900–1,100 — per-day signal-to-noise ≈ 0.1. The edge shows up in the sum (+2.76¢/$1 on $21,911 premium this week), never in any single day. ±10–25% days stay just as frequent as the bankroll grows. |

**And the week itself: the unattended run was GOOD.** 1,010 settles, realized
+$1,150.62 vs entry-model EV +$600.29 (2.76¢/$1, on model) — the week ran
*lucky* overall despite containing the worst day ever.

## 1) Full-era daily table (exchange settlements, % of equity at ET-day open)

23 trading days 7/14 → 8/12. Loss days ≤ −8% flagged; equally note the upside:

| ET day | realized | equity@open | day % |
|---|---|---|---|
| 7/23 | −$205.05 | $2,384.78 | **−8.6%** |
| 7/25 | +$281.24 | $2,179.73 | +12.9% |
| 7/28 | +$408.77 | $2,670.38 | +15.3% |
| 8/1 | +$291.89 | $2,928.38 | +10.0% |
| 8/2 | −$264.43 | $3,220.27 | **−8.2%** |
| 8/7 | +$364.88 | $3,557.70 | +10.3% |
| 8/9 | +$1,092.10 | $4,255.68 | **+25.7%** |
| 8/11 | −$917.00 | $5,354.07 | **−17.1%** |

(other 15 days between −4.6% and +9.0%; full table in the analysis scratchpad;
cumulative cash-identity equity $4,707.73 ≈ exchange $4,695.43 − the known
$42 fee residual.) **Both tails are fat**: 8 of 23 days moved ≥ ±8%. The book
turns over 74–113% of equity in settled premium per ET day at ~59% win rate —
±20%-ROI days on that turnover are the book's intrinsic width, on both sides.

## 2) The two record days are the same book, opposite draws

| | 8/9 (best ever) | 8/11 (worst ever) |
|---|---|---|
| realized | +$1,092.15 | −$917.00 |
| entry EV (recovered) | +$108.07 (2.25¢/$1) | **+$146.70 (3.73¢/$1 — the week's best)** |
| luck | +$984.97 | −$1,110.22 |
| wins vs expected | 147 vs 124.6 (z +3.32) | 73 vs 99.3 (z **−4.26**) |
| MC on our own fairs (indep.) | P(≥ +$1,092) = 2.7% | P(≤ −$917) = 0.9% |
| game-clusters | 3 (effectively 1: 208/2/1) | **1** (all 180 tickets, 24 games; 57% of tickets span 2–12 games) |
| whale share (top-5) | 26% | 41% (one of the 5 was a +$145 winner) |

The independence MC **understates both tails** — each day is effectively ONE
game-cluster, so the true probabilities are materially higher (direction-only
correction, per the 8/6 precedent). A z = −4.26 win-count miss on a one-cluster
slate is the signature of **correlated variance**, not a pricing failure:
every entry band lost on 8/11, every leg-count bucket lost, 2+-prop-leg
tickets took −$853 of the −$917 (their entry EV was +$106), pure-KS −$364 and
pure-HRR −$311. No cell failed; no refit is warranted. 8/9's top-2 winners
were near-zero-EV longshots (two 2-leg KS at 15.3¢/18.7¢, combined EV $0.55 →
+$390.78) — the **P1 whale seam, 4th sighting**.

**The sharper admission-side finding:** priced by the bot's OWN entry fairs
under independence, the 8/11 book carried P(day ≤ −12% of equity) = **4.05%**
and the 8/9 book **10.22%** — the admitted books exceed the 2% number *even
before* the single-cluster correlation fattens them. The gate math (per-candidate,
Wilson-upper, at the 35% CVaR line) never had to refuse anything.

## 3) What actually gates risk right now (audited from 16.63M risk_audit rows + config)

```
                       ARMED and binding this week
  ┌────────────────────────────────────────────────────────────┐
  │ utilization backstop   1,273,036 skips on 8/11 alone (top) │
  │ entity_loss_cap        507k–1.5M skips/day                 │
  │ mass_acceptance        31k–965k skips/day                  │
  │ portfolio_cvar (35%B)  18k–45k skips/day                   │
  │ cash itself            52,490 insufficient_balance on 8/11 │
  └────────────────────────────────────────────────────────────┘
                       MEASURED but NOT enforcing
  ┌────────────────────────────────────────────────────────────┐
  │ p_kill_night (12% line): peaks 0.23–0.50 EVERY slate day   │
  │   — telemetry only; kill_anchored_book_gate: false (8/1)   │
  │ p_ruin: budget stood down to 1.0 (8/1) — 0 refusals        │
  │ daily_loss_frac: 1.0 — disarmed (operator, 8/5)            │
  │ kill-tail decline reasons: 0 hits in 16.63M rows           │
  └────────────────────────────────────────────────────────────┘
```

On 8/11 the **cash wall was effectively the last standing brake** (ledger open
book 19:00 ET: 327 position rows, $4,840 collateral ≈ 103% of equity).

Two defects surfaced by the timeline:

1. **Relight launders the realized-P&L halts.** The static $500 absolute
   daily-loss halt tripped 9× (01:36–02:09 ET, day P&L −$555…−$586) — then the
   02:16 relight **reset the day-loss baseline** (restart-scoped realized
   feed) and the bot quoted uninterrupted the rest of the day at a standing
   −$612. This is the 7/25 §5-HIGH `position_ledger` day-seed no-op, still
   unwired. (The frac-based halt was already ratified OFF on 8/5 — but
   whatever halt IS configured must survive a restart to mean anything.)
2. **`halt_reconciliation_mismatch` 8/8 23:56 ET**: predicted settlement
   credit $51.00 vs exchange revenue $5.00 on `…040B9CBB783` — a
   settlement-model mismatch worth its own audit (separate from the known
   ledger seams).

Bookkeeping findings (accounting-sweep queue, re-evidenced): invisible-
settlement class grew **+12 tickers (+$170.07)** during the unattended week
(was 8/−$98.25); 5 tickers show >2% store-vs-exchange cost-basis gaps; 18
scalar settles (−$129.18) handled via exchange truth; 8/11 realized reads
−$765 (snapshot feed) vs −$1,045 (ledger by reconcile-time) vs **−$917
(exchange, ground truth)** — convention gaps, not new money.

Also measured, filed for its own decomposition (NOT acted on): the marked
book EV at snapshot median was *negative* every full day (−$4 to −$73) while
entry EV is +2.5¢/$1 — post-fill adverse drift / marking-band effect worth a
dedicated read before anyone trusts intraday marked EV.

## 4) What would make the 2% number real (decisions, not knobs)

The mechanism already exists and is staged — this is the **marginal-gate
re-arm** (8/1 build, dark): `kill_anchored_book_gate: true` +
`kill_gate_marginal: true` + `ruin_gate_marginal: true` +
`portfolio_ruin_prob_budget: "0.05"` (+ `portfolio_det_max_frac` 0.70 → 0.36),
one restart, **after the acceptance-seed fix** (seed the acceptance table from
the store's measured history at boot — the day-one-empty-tape defect that made
the level form freeze). Under budget it is byte-identical; over budget it
stops *adding* tail (admits diversifiers/reducers, refuses concentrators)
instead of freezing — the sunk-book constitution preserved, but the 12%/2%
anchor actually enforced on the margin. Until that arms, `p_kill_night` is a
thermometer, and nights like 8/11 are priced-in behavior, not surprises.

Complementary (already ranked): **P1 Stage-1 per-STRUCTURE bounds** caps the
whale/near-coin seam (4 sightings — it drove 8/9's upside, not 8/11's
breadth); the **utilization-backstop repair** (ratified today) frees the
capacity wall that currently does the risk engine's job by accident.

## NEXT STEPS

- **Operator decisions owed:**
  1. **Green-light the acceptance-seed fix + one-restart marginal-gate arm**
     (this is THE lever that turns the 2% night-tail budget from telemetry
     into enforcement; staging + checklist already in the 8/1 reports). If
     you prefer the current posture — capacity walls only, fat both tails —
     that is a legitimate ruling; say so and the 2% expectation gets retired
     from the vocabulary instead.
  2. Confirm the daily-loss story: frac-halt is OFF by your 8/5 ruling, but
     the leftover static $500 absolute halt is a hand-set number that a
     relight silently resets — dissolve it, or wire the day-seed so it
     survives restarts (7/25 §5-HIGH, still open).
- **Me (build, already ratified today):** utilization-backstop repair (count
  notional once + derived multiple), then P1 Stage-1 per-STRUCTURE bounds.
- **Me (audits queued):** settlement-model mismatch (8/8 halt, $51 vs $5);
  accounting sweep extension (invisible class now ~20 lifetime tickers);
  post-fill marked-EV drift decomposition; the two esports
  `rehydrate_reconcile_mismatch` tickers from this morning's boot.
- **Watch (no action, no refit):** cross-game correlation evidence keeps
  accumulating (8/11 z −4.26 on a one-cluster slate; the ratified cross-ρ=0
  caveat) — stays with the settlement-derived cross-ρ diagnostic
  (instrumentation only), feeds the marginal-gate case, never a P&L refit.
