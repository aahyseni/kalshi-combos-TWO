# 2026-08-01 — SKEW SETTLED-LEG FACT RESOLUTION (Task B): built, flag-gated, parity-checked — plus an honest re-attribution of the 7/29 widening

**State: SHIPPED DARK.** `pricing.skew.settled_fact_resolution` defaults **false**
(byte-identical to today, pinned by test + parity part C2). The arming line is
STAGED, commented, in the live local config's skew block — one uncomment at the
next operator restart arms it. Suite **3472/0**. Vitals **fast 8/8 GREEN, 23.2s**
at the ported tree. **Pre-ship: RUNNING at commit time** — the tier re-derives
`tape_facts` from the whole `data/live_*.log` set (~10 GB, growing) and tonight
it crawls behind the live bot's own disk I/O (two attempts, 800+ CPU-seconds
each, still scanning); rule 9 requires pre-ship **before ARMING**, not before a
dark commit, so the verdict is owed WITH the arming step (NEXT STEPS) and will
be appended here when the run completes. Port parity to the centi-cent on every
skew-consumed snapshot field, over a synthetic grid AND every reconstructed
tape book state.

## WRONG / FIXED / OPEN

| # | item | status |
|---|------|--------|
| 1 | Boot rehydration feeds ALREADY-FINISHED games' positions into the skew's concentration input as if live (confirmed 2×-blind on the 7/29 05:50Z boot; finished games never un-concentrate) | **FIXED (flag-gated, dark)** — `ExposureBook.snapshot(settled_facts=…)` fact-resolves exchange-DETERMINED legs out of every concentration aggregate the skew reads; boot and intraday share ONE resolution path (the graded-settlement cache) |
| 2 | The armed **leg-axis family/entity** shares read settled premium as live direction (7/29 boot: `KXMLBKS:yes` pinned at **53.7%** of committed family share by the settled 7/28 K-ladder) | **FIXED** under the same flag — the profiles are built FROM the resolved snapshot; cache keys extended with a monotone facts-generation so a fact landing at a static position generation rebuilds the shares |
| 3 | Shadow conc-steer loss-event book bucketed settled legs | **FIXED** under the same flag (same `concentration_live_legs` rule) |
| 4 | **The ratified magnitude attribution** ("the settled rehydration stepped applied skew median −21cc → −143cc") | **NOT SUPPORTED by the tape counterfactual** — see "Honest re-attribution" below. The fix is correct and worth arming, but it will NOT by itself restore hot-key acceptance; the widening tail is dominated by the **peak component** and earned directional on LIVE holdings |
| 5 | Peak profile still counts settled cross-game mass in its share denominator (dilutes live-game peak shares — a *conservative*, tighten-direction distortion) | **OPEN** (out of scope; noted for the peak decision) |
| 6 | Resting open quotes are not fact-resolved | **OPEN by design** — TTL seconds-lived + cancel-on-invalidate; cannot straddle a settlement the way an overnight rehydrated position does (documented in `snapshot`) |

## The defect and the rule

At boot, `_rehydrate_exposure_book` correctly re-books every exchange-open
position — including positions whose games FINISHED hours ago (exchange
settlement lag). Those positions flowed into the skew's quote-time exposure
snapshot as live concentration: per-game delta/worst-loss/notional/directional
entries, and (armed since 7/25) the family/entity loss shares. Nothing ever
un-concentrated them until the settlement sweep removed the positions.

**The rule (same semantics as the det-max settled-legs fix, `27de1e0` FIX 2):
a leg with an exchange-confirmed settlement is a FACT, not concentration.**
`risk/exposure.concentration_live_legs`:

- leg's SELECTED side graded **LOST** → the requires-all combo can no longer
  hit → the position's outcome is **DETERMINED** → realized P&L, **zero
  concentration on every axis**;
- leg's SELECTED side graded **WON** → the leg is a fact and **drops out**;
  the remaining live legs keep the position's **full** loss/notional/delta
  (partial resolution);
- every leg graded selected-won → combo HIT → determined → zero;
- **UNRESOLVABLE leg (no graded fact / non-binary value) stays FULLY counted**
  — fail-closed, UNKNOWN never buys tighter quotes;
- leg-less exchange-figure reserves untouched (fully counted).

Facts come ONLY from the graded-settlement cache
(`lifecycle._settled_fact` → `marketdata.settled.SettledMarginalResolver`) —
NEVER the feed marginal (a market pinned at 0/100 is not a settlement). Boot
and intraday now share this ONE path: the maintenance fetcher fills the cache
within ~2 passes of boot (7/29 tape: 9 finished-leg facts landed 09:51:19 →
09:51:53, 4–38s after boot), and intraday facts land the same way as
settlements grade — so concentration relaxes automatically in both regimes and
the two can never diverge again.

### Wiring (price-only blast radius)

| touch point | change |
|---|---|
| `risk/exposure.py` | `SettledFactProvider` alias, `concentration_live_legs`, `snapshot(..., settled_facts=None)`. Default `None` = operation-identical (the `elif settled_facts is None` split preserves the exact float-accumulation order). Deltas of a partially-resolved position are computed on the **stripped** legs (`replace(position, legs=live)`) so a graded leg can never re-enter through a still-printing settlement-timer book |
| `risk/skew.py` | `SkewParams.settled_fact_resolution: bool = False` (flag carrier only — no math change) |
| `rfq/lifecycle.py` | `_quoting_policy`'s snapshot — and ONLY that snapshot — receives the provider when armed (`_skew_settled_facts`). `_leg_axis_profile_from` + `_concentration_profile` cache keys gain `_skew_facts_generation()` (constant −1 while dark). The loss-event book rebuilds from resolved buckets when armed |
| `marketdata/settled.py` | `facts_generation` property = `len(_results)` (monotone, no new state) |
| `ops/config.py`, `ops/quote_app.py` | `SkewConfig.settled_fact_resolution` plumbed to `SkewParams` |
| **NOT touched** | every limit-check / pre-gate / confirm snapshot (caps see the whole book — asserted by `test_caps_view_is_never_resolved`), the widen policy's enablement (shadow, `enabled: false` live), peak profile build, pbook profile (already fact-aware through the marginal fallback), quote construction, engine |

## Hard-rule-8 trail

1. **Prototype**: `tools/proto_skew_settled_resolution.py` part A — 8/8
   properties on the LIVE `ExposureBook` (settled ⇒ zero; unresolved ⇒ full;
   mixed ⇒ partial by live legs; determined ⇒ nothing; leg-less untouched;
   construction-order/boot-vs-intraday equivalence; no-facts identity).
2. **Tape counterfactual** (part B): every `inventory_skew_shadow` event of
   the 7/29 defect tape and last night's 8/1 tape replayed through the LIVE
   `compute_inventory_skew`, RAW book vs RESOLVED book (see next section).
3. **Port**: the snapshot-internal implementation above.
4. **Parity** (part C): `concentration_live_legs` == the reference rule on a
   10-case side-aware grid; `snapshot(settled_facts=None)` == `snapshot()`;
   ported in-snapshot resolution == strip-then-snapshot reference **to the
   centi-cent on every skew-consumed field** — synthetic grid AND every
   reconstructed 7/29 tape book state (`PORT PARITY … PASS`).
5. **Property tests ported**: `tests/test_skew_settled_resolution.py` (11
   tests), including the settlement-timer trap (settled market still printing
   0.97 must NOT re-enter the delta product) and the caps-view isolation test.

## Tape counterfactual — and the honest re-attribution

Replay harness: boot book from the position ledger **gated by the tape's own
`exposure_rehydrated` game census** (the ledger alone is NOT the exchange —
stale forever-open rows excluded), facts from the tape's
`settled_marginal_resolved` stream at their real arrival times, leg mids from
the decisions tape, candidates from the rfqs store (sent-quote population;
award sizing at the recorded `no_bid`). Fidelity limits stated below.

**7/29 defect tape (05:50Z boot → 12:39Z), stride 10 (26,684 of 266,839 skew
events sampled; 1,333 replayable = sent-quote population), applied_cc
(pricer frame, negative = wider) — tool output verbatim:**

| series | n | p10 | p25 | p50 | p75 | p90 | mean |
|---|---|---|---|---|---|---|---|
| logged applied_cc | 1333 | -659 | -569 | -102 | -27 | 70 | -244.3 |
| replayed RAW applied_cc | 1333 | -638 | -337 | -77 | -30 | 38 | -196.6 |
| replayed RESOLVED applied_cc | 1333 | -640 | -338 | -84 | -29 | 41 | -194.7 |
| **counterfactual delta (res−raw)** | 1333 | -7 | -3 | **-2** | 1 | 21 | 1.9 |

widened ≥50cc: raw 63.2% → resolved 65.6%; the counterfactual tightens 25.5%
of quotes, median delta −2cc. (The stride-25 part-C parity run reproduces the
same shape: delta p10 −7 / p50 −2 / p90 +20, tightens 24.2%.)

**8/1 (last night, 05:11Z boot → 03:41 ET), stride 10 (31,647 of 316,470
events sampled; 2,513 replayable) — tool output verbatim:**

| series | n | p10 | p25 | p50 | p75 | p90 | mean |
|---|---|---|---|---|---|---|---|
| logged applied_cc | 2513 | -569 | -80 | 7 | 45 | 98 | -97.5 |
| replayed RAW applied_cc | 2513 | -526 | 26 | 60 | 82 | 146 | -18.3 |
| replayed RESOLVED applied_cc | 2513 | -526 | 34 | 61 | 82 | 149 | -16.1 |
| **counterfactual delta (res−raw)** | 2513 | 0 | 0 | **0** | 3 | 5 | 2.2 |

widened ≥50cc: raw 15.8% → resolved 15.8% (identical); the counterfactual
tightens 34.8% of quotes, median delta 0cc, and NEVER widens materially
(delta p10 = 0). Full-stride (stride 1) runs were launched for the record but
are I/O-bound on the 79 GB live decisions/rfqs store; the strided samples are
1.3–2.5k-event uniform samples of the same populations and match the
stride-25 cross-check, so the distributions above are the report of record.

Three findings, each read off the tape itself:

1. **The fix never touches the earned widening.** Last night's boot book was
   LIVE-HEAVY (13 of 16 games were 8/1 pregame positions; family shares
   well-spread, top `KXMLBKS:no` 24.9%): the widened tail is IDENTICAL raw vs
   resolved (p10 −524 vs −524; widened ≥50cc share 15.8% vs 15.8%) — a
   live-heavy book still widens exactly as 7/28 did. The whole effect is a
   small, one-sided tightening (p75 +3cc, p90 +5cc) from removing the three
   settled JUL31 games' phantom mass.
2. **On the 7/29 mostly-settled boot the counterfactual is SMALL and
   two-sided** (median −2cc, tightens ~25% of quotes, p90 +21cc) — NOT the
   ratified "median back from −143 to −21". Decomposing the 187,105 logged
   widened (≤−50cc) events: dominant component **peak 92,578**, directional
   53,429, family 41,087. At 09:51:19 — four seconds after boot, book = the
   rehydrated positions only, per-game contributions ALL ZERO — quotes on
   live 7/29 games already carried **peak_cc +600 (the cap)** while the
   settled K-ladder made the armed family axis a **−141cc REBATE**. The
   settled mass's live-priced effect was mostly a **phantom rebate**
   (diversifier-looking flow priced ~1.4c too cheap against a stale book —
   the fail-closed argument for arming this fix: UNKNOWN/stale must never buy
   tighter quotes), and a phantom +52cc family widen on K-carrying candidates
   later in the day.
3. **The widening that collapsed hot-key acceptance is the PEAK component
   plus earned directional on live holdings** — the ~$65 of 7/29-morning
   fills read at peak share ≈ 1 / severity 1 the moment the settled mass was
   unmodelable, and peak stayed p75 +280 / p90 +600 all day as real positions
   accumulated. That is a separate, operator-owned decision (the 2026-07-27
   scale-free rework made the steer size-invariant — by design a small book
   is no longer "nearly free"), NOT this fix, and NOT tonight.

**Replay fidelity, stated plainly:** the ledger is a drifted source (measured
14.78% wrong historically); the boot book was census-gated but not
exchange-exact (first-event family −157 replayed vs −141 logged, ≈ 91% of
magnitude; exact family_cc match only ~3%), mutex metadata and resting quotes
are absent from the replay, and the counterfactual population is the
sent-quote subset. The DELTA columns are robust to all of these (both arms
share every input except the resolved legs); the component attribution in
finding 3 is read from the LOGGED tape, not the replay.

## Throughput (never regresses)

| path | HEAD | ported | note |
|---|---|---|---|
| `snapshot` default (flag off), 80-pos/240-leg book, interleaved best-of-6 | 803us | 817us | +14us (+1.7%), within run noise; default path preserves the exact float-op order |
| `snapshot` armed (provider wired, worst case: every position partially resolved) | — | 1230us | opt-in cost, one graded-cache read per committed leg + a `replace` per partially-settled position |
| vitals fast tier (V1–V9, incl. quote-path timing checks) | — | **8/8 GREEN, 23.2s** | run at the ported tree |
| vitals pre-ship tier | — | **RUNNING at commit** | I/O-crawling behind the live bot tonight (see header); verdict owed WITH the arming step, appended here when it lands |

## How to arm

`config/prod-live-wc.local.yaml` → `pricing.skew` block: the staged
`# settled_fact_resolution: true` line is already in place (commented). At the
next operator restart, uncomment it. Watch `inventory_skew_shadow` after a
boot with finished games rehydrated: family/entity components should step as
`settled_marginal_resolved` lines land (≈30–60s after boot), and settled
games' keys should vanish from the profiles. Rollback = re-comment (one line,
one restart).

## NEXT STEPS

- **Operator**: arm `settled_fact_resolution` at the next restart (one
  uncommented line, staged). Low risk: never widens the earned tail, removes
  phantom rebates/widens sourced from settled games.
- **WITH the arming commit** (needs a quiet machine for `tools/vitals/prove.py`
  — 6 scratch-copy gate runs): add the one-line
  `settled_fact_resolution=s.settled_fact_resolution` passthrough to
  `tools/vitals/v_pricing.shipped_skew_params` (the KEEP-IN-SYNC quote_app
  replica). While the flag is dark the replica drift is exactly zero (both
  sides default False), which is why it is deferred rather than shipped
  un-proven tonight.
- **Operator decision owed (separate, not tonight)**: the PEAK component's
  small-book behavior — the dominant widener behind the 7/29–7/31 hot-key
  acceptance collapse (peak +600cc at cap 4s after boot on a ~$65 live book;
  share-based since 2026-07-27, so a tiny book reads share ≈ 1). Any change
  is a pricing-path design with its own prototype/parity/shadow cycle; a
  hand-zeroed `peak_widen_max_cc` stays forbidden (North Star).
- **Follow-up (small)**: peak-profile share denominator still counts settled
  cross-game mass (tighten-direction distortion only); fold
  `concentration_live_legs` into the peak build when the peak decision is
  taken.
- **Next session**: after arming, read the first rehydration-heavy boot's
  tape and confirm the family/entity step + relaxation timeline matches the
  7/28 intraday shape (the report's watch list above).
