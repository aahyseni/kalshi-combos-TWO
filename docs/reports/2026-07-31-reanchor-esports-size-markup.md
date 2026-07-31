# 2026-07-31 — Three builds gated: KILL re-anchor, esports 3¢ flat, size-scaled markup

**Scope:** operator read-out for three parallel builds worked in the worktree
`C:/Users/aahys/kct-reanchor` (off `main @ 2361039`). All work read-only against
the live tree `C:/Users/aahys/kalshi-combos-TWO`. **No order was placed,
cancelled or modified. Nothing was committed. Nothing was armed.** The live bot
(log `data/live_20260731_0609.log`) was never touched or restarted.

**Bottom line: 0 of 3 ship today.** One is a capacity CUT sold as a capacity
increase, one is EV-negative on the only elasticity we have ever measured, and
one has no statistically real gradient once the price confound is controlled.
**The wall that actually costs us money this morning is neither of them** — see
§4.

---

## 1. WRONG / FIXED / OPEN — commit-ready, most important first

| # | State | Item | Detail |
|---|-------|------|--------|
| 1 | **WRONG** | **The re-anchor's headline EV case does not survive.** | The brief projects 25→46 tickets, $789.11→$2,145.14 premium, EV $29.73→$93.56. That projection **depended on demoting det-max to 0.70·B**, which a prior agent **withdrew** (re-verified withdrawn at both sites: `risk/limits.py`, `sim/book_risk.py`, still `portfolio_det_max_frac 0.36`). With det-max unmoved, the arming flag is **mathematically incapable of admitting anything today refuses** — it can only ADD refusals. Measured: the $2,145.14 target book is refused by `SKIP_PORTFOLIO_DET_MAX` in **both** shadow and armed. On an 8-ticket 50¢ book, TODAY admits **$1,058.51**, ARMED admits **$470.47** — a **−55.6% capacity cut**. This is a capacity reduction that was being reviewed as a capacity increase. |
| 2 | **WRONG** | **The re-anchor is a CHEAP-NO TAX and violates a standing operator prohibition.** | Admitted-premium capacity ARMED/TODAY, fixed 25-ticket one-game-per-ticket book, price-consistent marginals: **25¢ 49.0% \| 40¢ 66.7% \| 50¢ 75.8% \| 70¢ 100.0% \| 85¢ 100.0% \| 92¢ 100.0%.** It halves capacity on the cheapest NO and has **exactly zero** effect at ≥70¢. Confirm path agrees: **29/40 verdict flips, all at 25–60¢, none at 88¢.** Mechanism is structural, not a bug — P(loss ≥ the 12% KILL line) is monotone in p_hit and p_hit is monotone in cheapness, so a KILL-distance *probability* gate IS a cheap-NO tax by construction. Standing rule: *"Do NOT introduce anything that steers toward higher p_book by buying expensive NO."* Recorded returns: cheap NO <50¢ **+77.9%** vs NO ≥85¢ **+1.1%**. **This is a PRICING/STEERING decision needing operator ratification, not a risk fix that ships.** |
| 3 | **CONFIRMED (defect is real)** | **The tail gate genuinely has never fired and cannot.** | Thresholded on `cvar_thr = portfolio_cvar_frac·B = 0.35B` instead of the ratified 12% KILL line (`risk/limits.py:1640`). At `portfolio_det_max_frac 0.36` the ratio is **0.9722** — 2% of scenarios must reach 97.22% of the comonotone maximum. Measured `P(loss ≥ thr) = 0.000%` on **every** book built, including a 2-ticket book at P(KILL-night) **9.1%**. Matches the tape: `"P(book loss >= "` occurs **0 times in 104,803** `risk_audit` rows; the portfolio CVaR/det-max axis refused **0 of 7,964** decisions on the 7/29 tape. **The diagnosis in the brief is correct. The prescribed cure is what fails.** |
| 4 | **OPEN — new, not in the brief** | **The armed boundary is a LATTICE CLIFF, not a line.** | At n=8, a **one-cent** change in ticket size moves P(KILL-night) **1.2% → 6.0%**, because the book flips from "6 of 8 must break" to "5 of 8" (binomial p=0.30 gives 1.13% / 5.80% — matches to 2dp). **The ratified 2% budget falls INSIDE that gap:** no book of that shape can sit at 2%. So "the boundary sits where the anchors say" is not accurate — it sits wherever the discrete loss lattice happens to straddle the KILL line. Any arming decision must accept that the effective budget is 1.2% or 6.0%, chosen by the lattice, not 2%. |
| 5 | **FIXED (verified by execution, no change needed)** | **det-max still enforces; there is no dollar-cap bypass.** | 300 randomized books (n 1–64, price 15–95¢, 1–3 legs): armed breaches are **always a superset** of shadow — 200 identical, 100 strictly tighter, **0 loosening violations**. Structurally guaranteed: `tail_thr 0.12B < cvar_thr 0.35B` only raises `k_ge`; the ES fallback branch and det-max are untouched by the flag. det-max refuses an 800-ticket $1,600 book armed and unarmed. |
| 6 | **FIXED (verified)** | **Staleness still fails closed; shadow is byte-identical; no throughput regression.** | Staleness: identical armed/unarmed (both axes breach); an emptied quantile envelope correctly falls back to the ES form rather than free-passing. Shadow identity: 300 cases, flag-off vs flag-field-absent → **0 differences** in (reason, detail, shadow). Throughput: SHADOW 282.8 µs/check (212,182/min) vs ARMED 274.3 µs (218,777/min), **−3.01% (armed marginally faster)**. Vitals **9/9 GREEN** both tiers (fast 8/8 19.3 s; `--tier all` 9/9 26.9 s). |
| 7 | **RETRACTED — in-flight, mine** | **"48 of 120 books RENEGE at confirm."** | **False.** My own harness read a non-existent `.ok`/`.admitted` attribute, so `bool(None)` made every confirm read as DECLINE. Corrected against the real field `.confirm`: **120/120 agree, 0 new reneges caused by arming.** |
| 8 | **RETRACTED — in-flight, mine** | **"The armed gate refuses everything and will brick quoting."** | **False.** I paired `p_hit = 0.30` with an 88¢ price — inconsistent marginals (88¢ implies 0.12). With consistent marginals the armed gate admits every realistic live shape (P(KILL) 0.00% at 25–46 small tickets @70–88¢). **It does not brick quoting.** (Standing precedent: the MLB 1% enforce bootstrap DID brick quoting live — this is the failure mode I was testing for. It is not present here.) |
| 9 | **OPEN — blocking** | **Confirm-path re-anchor has ZERO passing test coverage.** | `tests/test_kill_anchored_book_gate.py:460` references `RUIN_FLOOR_FRAC`, never bound at module scope (imported only *locally* at line 338, aliased `RUIN_DISTANCE`). `NameError` kills all 3 `TestConfirmPathMovesWithTheCap` tests — **the only tests exercising `_candidate_gate` / `evaluate_candidate_book_risk`.** File runs **3 failed / 26 passed**; suite runs **3436 passed / 3 failed / 3 deselected**. All 3 failures are this one inherited file. One-line fix; **left unfixed deliberately** (outside the authorised blast radius of the three tasks) — **the worktree cannot be committed or armed until it is green.** |
| 10 | **OPEN — blocking provenance** | **The worktree is CONTAMINATED — this is no longer the inherited re-anchor diff.** | A concurrent agent threaded an unrelated **derived burst floor** feature through the same files: NEW `src/combomaker/risk/burst_floor.py`, `tests/test_burst_floor_derived.py`, `tools/proto_burst_floor_parity.py`, `tools/proto_derived_burst_floor.py`, `tools/vitals/{v_flow,gate_flow,prove_flow}.py`, `flow_facts.json`, plus `resting_floor_rule` parameters added to `risk/limits.py` (`check`, `_additive_caps`, `_slate_joint_worst_case`) and `rfq/lifecycle.py`, plus modified `ops/quote_app.py`, `risk/exposure.py`. **The re-anchor cannot be shipped from this tree without separating the two changes.** |
| 11 | **WRONG (esports)** | **3¢ flat does NOT raise expected profit.** | On the only elasticity ever measured (`e = 0.22` fills-lost-per-cent, CMH-stratified, `LifecycleConfig.fill_elasticity_per_cent`, measured on **MLB** flow): multiplicative `(1−e)^Δm` gives EV **×0.986 (−1.4%)** on the <10¢ bucket and **×0.962 (−3.8%)** on 10–20¢; linear `1−e·Δm` gives **−13.6%** and **−8.5%**. **Both signs negative.** EV-optimal flat markup at that elasticity is **m\* = 4.02¢** (linear-anchored variants 3.77 / 4.27 / 4.77¢). **The existing 5/4/3 ladder straddles the optimum; 3¢ flat sits below it.** It raises FILLS, which is what was asked for — it does not raise EV. |
| 12 | **OPEN (esports)** | **The change is NOT live and cannot be, from here.** | The active markup lives **only** in the gitignored `C:/Users/aahys/kalshi-combos-TWO/config/prod-live-wc.local.yaml`, inside the live tree I am forbidden to edit. The edited file is **staged** at `C:/Users/aahys/kct-reanchor/config/prod-live-wc.local.yaml` (verified gitignored via `git check-ignore`; never committed, no secret printed). **The live bot is still quoting the 5/4/3 esports ladder.** Nothing takes effect without a copy-across + restart. |
| 13 | **OPEN (esports)** | **The tier distribution of esports flow is UNMEASURED — and it decides whether the change does anything at all.** | The tiers only bind below 20¢ fair. If esports flow is mostly ≥20¢ fair, the 3¢ base already applied and **the change is a no-op.** `markup_applied` (sport, markup_cc) IS logged at `pricing/engine.py:355` but at `log.DEBUG` — **0 occurrences in the live INFO log.** CONCRETE FIX: promote to `info`, or fold `sport` + `markup_cc` into the `risk_audit` line, so the next markup decision has a tier histogram instead of an assumption. |
| 14 | **OPEN (esports)** | **The realized price move may be smaller than 2¢, possibly zero.** | `margin = max(half, markup_cc)` (`pricing/quote.py:166`). The markup only BINDS when it exceeds the defensive half-width. Esports is uncalibrated flow, so its uncertainty-driven `half` is plausibly wide; wherever `half` already exceeds 300cc, dropping 500→300 changes the quoted bid by **less than 2¢ or not at all**. **Every fill/EV multiplier above is an UPPER bound on effect size.** |
| 15 | **WRONG (premise correction)** | **"Lower markup raises the send rate" is backwards.** | Markup **never gates**. It only sets `margin = max(half, markup_cc)` (`quote.py:166`), passed into `construct_quote` (`pricing/engine.py:349-372`). Send rate is a **RISK-gate** statistic. The one real coupling runs the OTHER way, through award sizing (`rfq/lifecycle.py:9065-9085`, `risk_qty_award_sizing: true` live): `contracts = target_cost_cc·100 / (10000 − max(bid))`. Lower markup **raises** our bid, **shrinks** the denominator, so contracts and gross notional go **UP**: +2.9% @30¢ bid, +4.2% @50¢, +6.1% @65¢, +11.1% @80¢, +18.2% @87¢ (per +2¢ bid). **1,179 of the 1,196 esports declines are notional/loss-cap coded** (entity 516, utilization 412, size 205, per-combo 46) — so the projected esports **SEND rate goes DOWN, not up.** Net fills still rise (fill rate +28–64% dominates) but by less than the naive figure. |
| 16 | **WRONG (size-markup)** | **The raw size→adverse-selection gradient looked real and pointed the WRONG WAY.** | At 1800 s, top-premium-quartile minus bottom = **+1.360¢ [+0.062, +2.761]** on our fair and **+1.339¢ [+0.039, +2.737]** on raw Kalshi leg mids (game clustering); slope **+0.421¢ per e-fold [+0.021, +0.823]**. **POSITIVE = big tickets marked out BETTER for us** — the opposite of "big is toxic". Anyone reading only this number would have shipped a size-scaled markup pointing the wrong direction. |
| 17 | **FIXED (size-markup) — the confound** | **Size is mechanically entangled with the NO entry price, and price is the real driver.** | `premium = contracts × price`. Mean NO price rises **monotonically** across premium quartiles: **52.27¢ [48.71, 60.17] → 56.08 → 61.80 → 67.19¢ [63.76, 72.55]**. cheapNO<50¢ share falls **37% → 28% → 20% → 12%**; richNO≥85¢ rises **0% → 0% → 4% → 12%**. **DECISIVE TEST — price-stratified size gradient: every CI straddles zero, on BOTH rulers, at BOTH horizons.** (e.g. cheapNO<50¢: **−0.024¢ [−0.283, +0.137] @300 s**.) **There is no size gradient once price is controlled.** |
| 18 | **OPEN — the ledger is a suspect store** | **Local `position_ledger` was wrong by 14.78% of dollars on 7/29.** | Exchange-truth audit (read-only): the **risk engine matched the exchange to $0.0002**; the ledger held 24 rows / $479.74 vs the exchange's 16 / $408.84, including a **PHANTOM row** (a fill the exchange never made) and a **WRONG-SIZE row** (56.40 booked vs 39.40 filled). **The exchange is the only ground truth.** Any book-shape claim sourced from the ledger — including the "25 tickets / $789.11" baseline the re-anchor projection is built on — inherits this error bar. |
| 19 | **OPEN — evidence grade** | **The cheap-NO tax MECHANISM is proven; the DOLLAR impact is not.** | Treating this as the **seventh retraction-class caveat**, stated up front rather than after the fact. MEASURED AND DETERMINISTIC (direct execution of live code, reproducible from seeds): the capacity-by-price ratios, monotonicity 0/300, shadow identity 0/300, staleness, throughput, vitals, and the 0.000%-at-old-threshold defect. **HYPOTHESIS, NOT A FINDING:** that the cheap-NO tax costs real money — the +77.9%/+1.1% return split it multiplies against is prior recorded measurement I did not re-derive, and my books are **synthetic single-shape books** using the `p_hit = 1 − P/10000` proxy (the same proxy the inherited live diagnostic uses), **not the live tape. Zero game-clusters.** |
| 20 | **OPEN — housekeeping, 3 items** | Small, all verified. | (a) **Stale comment contradicting the code**, in the exact place a future agent will read: `src/combomaker/ops/pricing_pool.py` `CandidateBookRiskInputs` still documents *"the det-max ceiling becomes the ruin-derived MODEL-FAILURE BACKSTOP (`ruin_floor_frac` …)"*. That behaviour was **withdrawn**; `limits.py` and `book_risk.py` were updated, `pricing_pool.py` was missed. (b) **`LifecycleConfig.kill_anchored_book_gate` (`rfq/lifecycle.py:929`) is DEAD** — the call site at `:4090` correctly reads `limits.kill_anchored_book_gate` off `RiskLimits`. Reading from `RiskLimits` is the right choice (single source; cap and gate cannot diverge); **delete the unused config field** so nobody sets it and expects an effect. (c) **`risk/cross_game_residual.py` is INSTRUMENTATION ONLY** and must never gate — confirmed it does not. |

---

## 2. The three builds — verdicts

### Build A — KILL-anchored book gate (the re-anchor) → **NO-SHIP**

**I could not kill it on safety. I killed it on economics and provenance.**

| Adversarial check | Result |
|---|---|
| (a) Gate fires once re-anchored | **YES** — and the defect it fixes is real (0.000% at the old threshold on every book, incl. one at P(KILL) 9.1%). **Caveat: lattice cliff, row 4.** |
| (b) det-max still enforces | **YES** — unmoved at 0.36 both sites; refuses an 800-ticket $1,600 book armed and unarmed |
| (c) Wrong correlation model | **STRICTLY SAFER.** 8 independent tickets, long-NO @50¢, one-factor Gaussian copula on the loss indicator, 400k paths. **TODAY** admits $1,058.51 → P(ruin) ρ=0.00 **3.51%** \| 0.10 7.17% \| 0.25 **12.87%** \| 0.50 22.18% \| 1.00 50.01%. **ARMED** admits $470.47 → P(ruin) **0.00% at every ρ**, because the admitted book's comonotone worst case sits below the 30% ruin line ($882.08). |
| (d) No dollar-cap bypass | **YES** — 300 books, armed ⊇ shadow, 0 loosening violations |
| (e) Staleness fails closed | **YES** — identical armed/unarmed |
| (f) Shadow byte-identical | **YES** — 0/300 differences |
| (g) Throughput | **No regression** — −3.01% latency (armed faster) |
| (h) Vitals | **9/9 GREEN** both tiers |

**Operator's question, plainly: "Cross-game independence is ratified. If that is WRONG by 0.25, what does this build cost us that today's would not?"**
**Nothing. It costs us nothing and it SAVES us.** Same 8-ticket 50¢ shape at ρ=0.25: today's regime reaches P(ruin) **12.87%** and P(KILL) **74.06%**; the armed regime reaches P(ruin) **0.00%** and P(KILL) **25.97%**. Because the det-max demotion was withdrawn, arming can only ever ADD a refusal (0/300 violations). **The ρ risk in this build is not a risk of loss — it is a risk of OVER-tightening**: the gate is computed from the ρ=0 joint, so if true ρ > 0 it refuses even more, and it refuses **disproportionately on cheap NO.** Recorded sensitivity for reference: cross-ρ 0.00→0.25 takes ES99 $221.64→$280.65 and modeled EV **+$10.84 → −$4.25**.

**Verdict: NO-SHIP.** Safe, correct, well-tested on the check axis — but it delivers the **opposite** of its stated benefit (row 1), it is a **cheap-NO tax** contradicting a standing prohibition (row 2), its only confirm-path tests **do not run** (row 9), and the tree it lives in is **contaminated** (row 10). **The diagnosis stands and should be preserved; the cure needs re-specification.**

### Build B — esports markup 5/4/3 → 3¢ flat → **NO-SHIP on EV; SHIP-ABLE as an explicit operator price floor**

**Diff is exactly 3 deleted lines** (two tier entries + the `tiers:` key) plus a 14-line rationale comment. Nothing else in the 58 KB config moved — proven twice:

- **SHA256, per-sport blocks under `pricing.markup`:** soccer `2837aabba3ef74c8`, mlb `1a98d091c396cb2f`, racing `5420301440b177a6`, mixed `87a1cf8fb3829794`, `series_adders_cc` `fc8a657f6d3c2ff4`, `enabled` `2eb7423d2e331462` — **all IDENTICAL**; `esports` `2ddb2932559cc529 → 56b419745fce65b3` — **the only one changed.**
- **SHA256, all 12 top-level blocks:** breakers, data_dir, env, filters, kill_file, logging, mode, observe, risk, safety, supervisor **byte-identical**; only `pricing` changed (`4602cb516d43a46d → 4f1672b26f591789`).
- **BEHAVIOUR** (live `MarkupPolicy` through the real loader, 101 fair points × 12 leg shapes): soccer, soccer+corners, mlb, racing(F1), racing(NASCAR), mixed(mlb+esports), mixed(mlb+racing), mixed(soccer+mlb), unknown, unknown+mlb — **identical at all 101 points** and on the fair-less caller path. Only **pure-esports** rows changed, and only over fair **0–19¢**. Note `mixed(mlb+esports)` is unchanged at 600/500/400 because `markup_for` prices the cross-sport bucket from its **own** `mixed` config, never from the esports leg.

**Flow measured** (7/28 + 7/29×2 + 7/31 logs; sport tagged by driving the live `markup.sport_of` / `_leg_sport` over `inventory_skew_shadow.leg_axis_rows`, joined by `rfq_id` to `risk_audit` phase=quote): **285,233** quote-phase decisions. mlb 283,754 dec / 84,013 sent (29.6%) / 51 accepts. **esports pure 1,382 dec / 186 sent (13.5%) / 0 accepts.** mixed esports+mlb 68 / 10 (14.7%) / 0. **Esports = 0.48% of decisions, 0.23% of quote volume.**

**Quote vs win:** 196 esports/cross-sport quotes since the 2026-07-26 wire → **zero accepts, zero fills, zero settlements**. **But 0/196 is NOT evidence of a deficit:** at MLB's measured 6.07e-4 accepts/quote the expectation is **0.119 accepts** and **P(0 accepts) = 88.8%**. We are **~4,941 esports quotes short** of detecting a deficit at 3 events. **Zero independent game-clusters ⇒ any esports fill claim is a HYPOTHESIS.**

**Elasticity transfer is a HYPOTHESIS, not a finding.** `e = 0.22` was CMH-stratified on **MLB** flow, has never been measured on esports, and **cannot** be (0 fills, 0 clusters). Validity horizon `1/(2e) = 227cc = 2.27¢`, so the **2¢ move on the <10¢ bucket is 88% of the measurement's own horizon** — at the edge of where the linear read stops meaning anything. The EV figures also **ignore adverse selection and the risk-cap crowd-out of MLB flow — both push the optimal markup HIGHER**, i.e. both make 3¢ look worse.

**Verdict: NO-SHIP as an EV improvement — it is EV-neutral-to-negative (−1.4% to −13.6% per quote in the two affected buckets, 0% on mains).** It is defensible **only** as an explicit **operator price floor** bought to create a settlement/fill corpus that does not currently exist. Blast radius is genuinely tiny: 0.23% of quote volume, 0% of fills, so even the worst reading costs **~0.03% of portfolio EV**. **NOT A P&L REFIT and it must never be defended as one** — it moves on an operator directive plus competitive/structural reasoning, exactly as the 7/26 3–5¢ floor did. The honest label in the config comment is `OPERATOR PRICE FLOOR, not a calibration`. **If it is later reverted, revert it on measurement (a real esports fill corpus), never on a P&L window.**

### Build C — size-scaled markup (bigger ticket ⇒ wider) → **NO-SHIP. The gradient is not real.**

**Sample:** 514 fills, **$6,751.83** total premium, 13 trading days 2026-07-14…07-31, **100% sell-only** (`our_side='no'` on every fill). Median ticket $6.45, p90 $32.73, max $143.89.

**Clusters: 13 day-clusters (all 514 fills) / 18 game-components** — and only the 298 fills whose RFQ row still exists (**168 fills' RFQs are ABSENT from the `rfqs` table**, 48 more carry no `rfq_id`). **Both are at the ~10-cluster line, so everything here is HYPOTHESIS-grade at best, never a finding.**

Raw gradient looked real and pointed the wrong way (row 16). Controlled for the price confound, **every CI straddles zero on both rulers at both horizons** (row 17).

**Verdict: NO-SHIP.** There is no size gradient to price. **Shipping a size-scaled markup would have been a markup change fitted to a confound** — precisely the failure the no-refit rule exists to prevent. What the data *does* say is that **NO entry price**, not ticket size, is the axis that separates outcomes — which is the same axis Build A taxes in the wrong direction.

---

## 3. BEFORE / AFTER on the live book — the re-anchor

**Reference frame:** bankroll **$2,940.28** → KILL line (12%) **$352.83**, ruin line (30%) **$882.08**, det-max dollar wall (0.36·B) **$1,058.50**.

**Read the AFTER column carefully — the brief's projection is retracted.**

| Metric | BASELINE (brief, 7/29 book) | BRIEF PROJECTED — **RETRACTED** | **MEASURED, ARMED** |
|---|---|---|---|
| Tickets | 25 | 46 | **Unreachable as specified** |
| Admissible premium | $789.11 | $2,145.14 | **REFUSED** by `SKIP_PORTFOLIO_DET_MAX` in **both** shadow and armed (wall $1,058.50) |
| Modeled EV | $29.73 | $93.56 | **Not attainable** — the EV case required the withdrawn 0.70·B det-max demotion |
| ES / premium | 0.472 | 0.217 | **Not measured on the live book** (see caveat below) |
| Effective N | 15.65 | 25.98 | **Not measured on the live book** |
| P(KILL-night) | 1.50% | 1.50% | Lattice-quantised — see row 4; 2% is **not attainable** at n≈8 |

**What WAS measured, directly, on synthetic price-consistent books:**

| Shape | TODAY admits | ARMED admits | Δ |
|---|---|---|---|
| 8 tickets, 50¢ NO | **$1,058.51** | **$470.47** | **−55.6%** |
| 25 tickets @ 25¢ | 100% | **49.0%** | −51.0% |
| 25 tickets @ 40¢ | 100% | **66.7%** | −33.3% |
| 25 tickets @ 50¢ | 100% | **75.8%** | −24.2% |
| 25 tickets @ 70¢ | 100% | **100.0%** | **0** |
| 25 tickets @ 85¢ | 100% | **100.0%** | **0** |
| 25 tickets @ 92¢ | 100% | **100.0%** | **0** |

**Two caveats that make this table weaker than it looks, both stated deliberately:**

1. **The live book is FLAT.** `grep -c '"event": "fill"'` on today's live log returns **0**. There is no live book to run a BEFORE/AFTER against; every number above is a **synthetic single-shape book** with `p_hit = 1 − P/10000`, **zero game-clusters**. The mechanism is deterministic and reproducible; the dollar translation is not.
2. **The $789.11 / 25-ticket baseline is ledger-sourced**, and the ledger was **14.78% wrong in dollars on 7/29** (row 18). The exchange is the ground truth; the risk engine matched it to $0.0002. **Do not treat the baseline row as exact.**

---

## 4. THE WALL THAT BINDS — and it is not risk appetite

**Correction to the brief's premise, and this is the loudest thing in the report.** The brief cites a warmup mix (702 decisions: `skip_portfolio_cvar` 174, size 169, per-combo 139, entity 138, sent 51 = **7.3%**) and asks whether **staleness** is the real bottleneck. **Measured over the full session and over the most recent slice, it is not — staleness resolved after warmup.**

Live log `data/live_20260731_0609.log`, last entry `2026-07-31T12:09:11Z` (**08:09 EST**), read-only:

| Reason | Full session (since 06:09 EST) | Share | Most recent slice | Share |
|---|---|---|---|---|
| `skip_entity_loss_cap` | 45,916 | **33.7%** | 7,419 | **31.4%** |
| `skip_utilization_backstop` | 35,498 | **26.1%** | 6,679 | **28.3%** |
| **`quote_sent`** | **35,407** | **26.0%** | **6,476** | **27.4%** |
| `skip_size_above_max` | 8,162 | 6.0% | 1,057 | 4.5% |
| `skip_per_combo_loss_cap` | 6,720 | 4.9% | 1,149 | 4.9% |
| `skip_mass_acceptance_breach` | 2,261 | 1.7% | 364 | 1.5% |
| `skip_max_open_quotes` | 1,330 | 1.0% | 489 | 2.1% |
| **`skip_portfolio_cvar`** (fail-closed STALENESS) | **804** | **0.6%** | **0** | **0.0%** |
| `skip_directional_cap` | 13 | ~0 | — | — |

**Send rate is 26.0% session / 27.4% recent — not 7.3%.** The 7.3% was a warmup artefact and the staleness path has gone to **zero** in the recent slice. **Say it plainly: staleness is NOT today's bottleneck. Do not spend the operator's attention there.**

**The bottleneck is `skip_entity_loss_cap` + `skip_utilization_backstop` = 59.8% of all decisions (59.7% recent) — on a book with ZERO fills today and ~$2.9k idle cash.** That is the finding. `SKIP_UTILIZATION_BACKSTOP` reads **Σ gross settlement notional including RESTING quotes** (`risk/exposure.py:1658`, `risk/limits.py:548`) — so it refuses on quotes we have not been hit on, not on inventory we hold. **Nearly $3k of cash is idle while two notional-axis caps refuse three fifths of flow against a near-empty book.**

**Therefore: after Build A lands, the wall that binds is still `entity_loss_cap` and `utilization_backstop`.** Build A touches neither — it acts on the portfolio CVaR axis, which refuses **0.6% of flow today and 0.0% in the recent slice**. **Arming Build A cannot raise the send rate by a single quote. It can only lower it.** The concurrent agent's **derived resting burst floor** (`docs/reports/2026-07-31-derived-resting-burst-floor.md`, `risk/burst_floor.py`) is aimed at exactly this wall and is, on this morning's mix, **the higher-value change of the two** — it is where the operator's money is actually being left on the table.

---

## 5. If the operator arms anyway — exact lines, metrics, aborts

**Preconditions, all three mandatory. Do not arm without them.**

1. **Fix `tests/test_kill_anchored_book_gate.py`** — bind `RUIN_FLOOR_FRAC` at module scope (it is imported only locally at line 338, aliased `RUIN_DISTANCE`). Suite must be **3439/0**.
2. **Separate the burst-floor change out of this tree** (row 10) — the re-anchor must be armed as its own diff.
3. **Run hard rule 9 both tiers:** `python -m tools.vitals.gate` (8/8) **and** `python -m tools.vitals.gate --tier pre-ship`. *(Both run with `cwd=C:/Users/aahys/kct-reanchor`, `PYTHONPATH=C:/Users/aahys/kct-reanchor/src`, `VITALS_DATA_DIR=C:/Users/aahys/kalshi-combos-TWO/data` — the gate's own documented read-only override at `tools/vitals/derive.py:43`, because the worktree has no `data/` and the gate correctly refuses to invent a bankroll. Note `--tier pre-ship` alone reports 1/1 — it runs only the single slow-tier sign; `--tier all` is the real both-tiers number, 9/9.)*

### YAML to arm — `config/prod-live-wc.local.yaml` (GITIGNORED, live tree)

Exactly **one** line is added, under the existing `risk:` block, alongside `portfolio_det_max_frac: "0.36"` (line 781) and `portfolio_cvar_frac: "0.35"` (line 787):

```yaml
risk:
  # ... existing keys unchanged ...
  portfolio_det_max_frac: "0.36"   # UNCHANGED — model-failure backstop, stays enforcing
  portfolio_cvar_frac: "0.35"      # UNCHANGED — the ES-form fallback still reads this
  kill_anchored_book_gate: true    # ARM: threshold the tail axis on the 12% KILL line
```

**Do NOT also change `portfolio_det_max_frac`.** The 0.70·B demotion is withdrawn and must stay withdrawn — it is the only bound that survives a wrong correlation model (`Loss(ω) ≤ D` for every outcome, any copula, any stale fact), and a 25-step chain of individually correct pairwise certifications with det-max bypassed reaches **$18,107.16 = 6.16× bankroll = 51.32× the KILL distance**, because certification is pairwise-LOCAL and det-max is GLOBAL.

**To revert:** set `kill_anchored_book_gate: false` (or delete the line) and restart. Shadow behaviour is **byte-identical** to flag-absent — verified 0/300.

### Five metrics for the first hour, with expected values

| # | Metric | Where | EXPECTED if healthy | Meaning if it misses |
|---|---|---|---|---|
| 1 | **Send rate** (`quote_sent` / all `risk_audit` phase=quote) | live log | **24–27%**, i.e. within ~2 pts of the 26.0% pre-arm baseline. It must NOT rise. | A **rise** means the flag loosened something — impossible by construction (0/300), so it means the wrong knob moved. **Abort.** |
| 2 | **`skip_portfolio_cvar` count** | live log | Rises from **0.6% → single-digit %**. This is the gate finally being reachable. | **0.0% still** ⇒ the flag did not take effect (config not reloaded / not restarted). **Not an abort — a no-op; re-check the load.** |
| 3 | **Cheap-NO share of `quote_sent`** (NO fair <50¢ as % of sends) | live log | **Flat vs the pre-arm hour.** | A **fall** is the cheap-NO tax showing up in live flow (row 2). This is the metric that decides whether the tax is real money or only a synthetic artefact. |
| 4 | **`skip_entity_loss_cap` + `skip_utilization_backstop` share** | live log | **Unchanged at ~59–60%.** Build A must not touch these. | Any movement means blast-radius leakage into the notional axes. **Abort.** |
| 5 | **Quote latency p99 / quotes-per-min** | vitals `v_flow`, live log | **Within 3% of pre-arm** (bench said armed is −3.01%, i.e. marginally faster). | Any regression violates the standing throughput rule. **Abort.** |

### Abort criteria — any one is sufficient

- **Send rate falls below 20%** (a >6-pt drop from the 26.0% baseline) in any 15-minute window.
- **Cheap-NO (<50¢) share of sends falls by >20% relative** vs the pre-arm hour — the cheap-NO tax has landed on live flow.
- **Any change in `skip_entity_loss_cap` or `skip_utilization_backstop` share beyond ±3 pts** — blast-radius leakage.
- **Quotes-per-min regresses at all** versus the pre-arm hour.
- **`risk_starvation_watchdog` fires** (20 consecutive risk-driven declines) — it fired once already on 7/29 under the *looser* regime.
- **Any `SKIP_PORTFOLIO_DET_MAX` on a book smaller than the pre-arm typical book** — det-max is supposed to be untouched.

---

## NEXT STEPS

**Runs next (agent, no operator decision needed):**
1. Fix `tests/test_kill_anchored_book_gate.py:460` — bind `RUIN_FLOOR_FRAC` at module scope. One line. Unblocks the only tests covering `_candidate_gate` / `evaluate_candidate_book_risk`. **Suite must return 3439/0.**
2. Delete the dead `LifecycleConfig.kill_anchored_book_gate` field (`rfq/lifecycle.py:929`); the call site at `:4090` correctly reads `RiskLimits`.
3. Correct the stale `ruin_floor_frac` comment in `src/combomaker/ops/pricing_pool.py` (`CandidateBookRiskInputs`) — it documents behaviour that was withdrawn.
4. Promote `markup_applied` (`pricing/engine.py:355`) from `log.DEBUG` to `info`, or fold `sport` + `markup_cc` into `risk_audit`. **Without this, the esports tier distribution stays unmeasured and the 3¢ decision stays an assumption.**
5. Separate the burst-floor diff from the re-anchor diff so each can be gated and armed independently.

**Owned by the operator — decisions owed:**
- **D1 — Build A (re-anchor): arm, re-spec, or shelve?** My recommendation: **re-spec.** The diagnosis is correct and valuable (the tail gate is unreachable, confirmed on 104,803 rows and again on 7,964 fresh decisions), but the cure as built is a **capacity cut** and a **cheap-NO tax**. It cannot raise the send rate because it acts on an axis refusing 0.6% of flow.
- **D2 — Do you ratify the cheap-NO tax as a PRICING decision?** A KILL-distance probability gate is monotone in cheapness **by construction**. You cannot have this gate and stay price-neutral. Recorded returns are cheap NO **+77.9%** vs ≥85¢ **+1.1%**. This is your call, not a risk-engineering call.
- **D3 — Build B (esports 3¢): copy the staged config across and restart, or leave it?** It is **EV-negative** on the only elasticity we have (−1.4% to −13.6%; optimum is ~4.0¢) and **buys a fill corpus that does not exist** (0 fills ever, 88.8% chance of seeing zero even with no deficit). Cost of being wrong: **~0.03% of portfolio EV.** If you take it, it is labelled `OPERATOR PRICE FLOOR, not a calibration`. **Nothing happens without the copy-across + restart.**
- **D4 — Build C (size-scaled markup): I am recording this as NO-SHIP and closing it.** No size gradient survives the price control. Say if you want it reopened.
- **D5 — Reprioritise onto the real wall.** `entity_loss_cap` + `utilization_backstop` = **59.8%** of decisions with **zero fills today** and **~$2.9k idle**. The derived resting burst floor targets exactly this. **On this morning's numbers it is worth more than the re-anchor.**
- **D6 — Ledger repair.** The local `position_ledger` was **14.78% wrong in dollars** with a phantom row and a wrong-size row, while the risk engine matched the exchange to **$0.0002**. Any book-shape claim sourced from the ledger is unreliable until reconciled to exchange truth.

**Nothing was committed. Nothing was armed. No order was placed, cancelled or modified. No Kalshi API call was made. The live tree was never edited or restarted.**
