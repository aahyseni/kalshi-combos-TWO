# 2026-09-04 — Pricing-mechanism deep dives: the maker-fee seam, the MLB "losing cells", and soccer BTTS×over

Operator asked for in-depth explanations of the three repair items from the
all-time sweep (fee seam; MLB pricing cells; soccer btts×over). Four
code-and-data readers (one per mechanism) + one citation/numbers verifier
(214 citations opened, every headline number re-run). Read-only; nothing
edited; helper scripts in the session scratchpad only. Verifier verdicts:
all four SOUND WITH CORRECTIONS (corrections applied below). Operator rulings
this session: NFL wiring DECLINED for now; MLB stays admitted through the
postseason (postseason 9/29 → World Series ends ~10/31, not February).

## What survives, what is retracted

| Item from the sweep | Deep-read verdict | One line |
|---|---|---|
| 1. Fee seam dark | **CONFIRMED, bigger than stated** | 144 fills / $3,140 premium (34%) since 8/20 were negative-EV after fee; every EV the bot judges is gross of fee; the fix is ONE edge function + a fee-aware rebate clamp + a measured coefficient. |
| 2a. rfi×rfi pairs at 0.49% edge | **CONFIRMED — but the defect is the REBATE, not the joint** | 32 cross-game NRFI×NRFI parlays priced at independence (correct); the inventory-skew rebate gave back the whole 2¢ markup (median retained 0.2¢) until the 8/16 half-margin cap; the cap is a hand fraction and fee-blind. |
| 2b. partial-SGP correlation markup | **RETRACTED** | −$481 of the −$300 is 35 combos carrying a player-HR leg = the same rebate defect on all-NO "nobody homers" baskets; the other 503 combos are +$181 (noise). The joint construction is not the defect; no correlation promote is justified. |
| 2c. KS line-2 calibration | **RETRACTED** | Leg-level check on 2,686 quote-time mids: line-2 bias −1.2 pts (t −0.3), line-3 +3.1 (t 1.1). Calibrated. The +$1,718/−$456 split is five aces with 1–2 K nights on 250–470-contract tickets. Ship a monitor, no pricing change. |
| 2d. Whale size-conditioned markup | **CONFIRMED loss, MECHANISM REFRAMED** | Whale ≈ −0.2% ROI vs 2.7% model; the loss is one band (NO 30–50¢, −$653, t −2.5) and lives in 115 same-game 2-leg pairs (hrr\|hrr, ks\|ks, hit\|hit). Its leg picks are NOT better than our mids at any size; markouts see nothing. First build = same-game pair-ρ measurement (6C); counterparty adder built DARK behind it (6B). |
| 3. soccer btts×over | **CONFIRMED — routing + stale constant** | 4/4 club btts×over-2.5 pairs were rejected by the hand-set 0.005 exact-fit bar and silently fell to the copula at btts\|total 0.70 = the July World-Cup BLEND (club measured 0.746). NO bid ~1¢/pair too rich; booked edge on those fills was phantom. structural_fits has 0 rows ever. |

## 1. The maker-fee seam (mechanism 1)

**The fee.** From 8/20 05:07 ET every maker fill pays
`ceil(0.035 × contracts × P × (1−P))`; re-fit on 540 charged fills = 0.035007,
median error $0.00002, max $0.00009. The repo's 0.0175 (NOTES.md row L9, MF1
"UNVERIFIED for the future announcement") errs $0.067/fill. Charged once at
fill, echoed at settlement. All-time $127.13; since onset $109.14 (8/20
$38.43 / 8/26 $66.24 / 8/27 $4.47). Kalshi series now report
`fee_type: quadratic_with_combo_maker_fees`.

**Where the fee is $0 today, and why (every link independently zero):**

```
yaml: no pricing.fee block            conventions.json L7: maker_is_taker_on_fill=false
FeeConfig defaults (config.py:436-460): taker .07 / maker .0175 / type "quadratic" / prefixes ()
        |
  FeeModel built twice: engine.py:191-196 (pricer)  quote_app.py:2227-2233 (ledger)
        |  fee_type QUADRATIC + maker≠taker  →  fees.py:78-89 _pricing_coef → Fraction(0)
        v
 [A] QUOTE WIDTH quote.py:193-224   side_fee() = 0 → no_raw = (1−fair) − margin − 0 + skew
 [B] FILL LEDGER lifecycle.py:6642-6703  _effective_fee_type upgrades ONLY if prefix list
     non-empty (6745-6758) → fee_cc 0, expected_edge GROSS, record_realized_pnl(−fee) skipped
 [C] WAIVER worst-case lifecycle 4805-4820  candidate fee 0 unless prefix active
 [D] confirm-time ADMISSION EV  _pricing_edge_cc 3196-3229 → _candidate_edge_cc 3172-3179
     = (side_fair − bid)·qty, NO fee term → book_risk.py:2877 admission_ev (yaml L946)
 [E] quote-time candidate EV / eviction key  _quote_candidate_ev_cc 3231-3253 (same fn)
 [F] KILL-marginal admission  _kill_marginal_for_quote 5199-5232 (ev at 5220) and
     _kill_marginal_for_fill 5246-5265 ← _fill_ev_cc 5234-5244 → the SAME edge fn (verified)
 [G] nothing watches: rest.py:475 get_series_fee_changes has ZERO callers; series fee_type
     never fetched; the WS fill message carries no fee (fills.raw_json keys: quote_id, rfq_id,
     order_id, executed_ts) — defense #3 never sees a trade fee on the normal path
 TRAP: FeeType.parse('quadratic_with_combo_maker_fees') → UNKNOWN (fees.py:45-50) →
     FeeUnknownError → NoQuote SKIP_CLASSIFIER_UNKNOWN on EVERY combo (quote.py:218-219).
     Setting the exchange's own string into the yaml bricks quoting.
```

**What it cost (547 store fills since onset, 534 matched to the exchange):**

```
tier                 fills  premium$  model edge$  fee$   fee/edge  edge ¢/ct  fee ¢/ct  fee>edge fills  premium$
mains_mlb (1¢)        249   3,039.43     99.57    50.64    0.51      1.66      0.84         45          728.95
razor ML parlay       136   2,913.64     19.58    22.41    1.14      0.51      0.60         83        2,021.00  (incl. 14 esports + 1 mixed: all-sports razor since 8/26)
ladder_mlb (2-3¢)     100   2,086.86     96.87    17.63    0.18      3.48      0.63          0            0.00
mains_soccer (1¢)      47     894.36     17.20    13.45    0.78      1.04      0.81         16          371.27
other                  15     373.76      8.55     2.28                                      0
TOTAL                 547   9,308.05    241.77   106.41    0.44      1.64      0.72        144        3,140.34
```

Razor by era: 0.3¢ razor (8/20–8/25) 68 fills, median retained 0.51¢ vs fee
0.59¢/ct, 47 negative ($1,084 of $1,404); 0.6¢ razor (8/26+) 53 fills, 0.65
vs 0.66¢/ct, 26 negative ($681 of $1,135). The 8/26 doubling did not clear the
fee. The yaml comment at the razor (local yaml L186-187) literally says "Near-
zero fees make it viable."

**What each operator move does:**
- (a) set `maker_fee_active_prefixes` only → ledger books HALF the fee
  (0.0175), quotes unchanged, [D][E][F] still gross: books a wrong number and
  admits the same negative-EV fills.
- (b) plus `maker_coef 0.035` → ledger exact; quotes unchanged; [D][E][F]
  still gross: the razor keeps winning fills at 0.15–0.5¢ retained vs a 0.6¢
  fee.
- (c) plus `default_fee_type quadratic_with_maker_fees` → construct_quote
  subtracts the fee from EVERY NO bid, all sports, all tiers: razor bid moves
  fair+0.6¢ → fair+1.20–1.36¢ (fee 100–127% of markup), mains 1¢ → fair+1.80–
  1.88¢ (80–88%), ladder 2¢ → 2.71¢, 3¢ → 3.42¢. Against the 8/16 measurement
  (field clears our-fair +0.05–0.25¢ on 1,122 ML-parlay auctions) the razor
  becomes unwinnable at +EV — it already is, net of fee. Mains: the 8/19 read
  showed 80–88% of ≥35¢-fair auctions expire unprinted, so the fill cost of
  +0.86¢ there is small but unmeasured.
- (d) TRAP above.
- (e) The 7/16 "eat the fee" doctrine (NOTES MF2; config.py:454-459) was
  written for a 0.0175 schedule (max 0.44¢/ct) against 1–2¢ markups. At 0.035
  against a 0.3–0.6¢ razor the fee is 100–146% of the margin; "account
  downstream" is only a doctrine when the downstream accounting exists, and
  today it does not.

**Repair (measured, one function, one clamp):**
1. Measured fee schedule (`pricing/fee_observer.py`, small store table): fit
   the coefficient per (collection, is_taker) from the REST fills the recovery
   sweep already polls (lifecycle 7590-7610 parses fee_cost); validate every
   fill to ±1 cc; a miss = drift alarm through the existing
   halt_reconciliation path. Both FeeModel builders take the shared schedule;
   persisted across relights; yaml coefficient survives only as a logged
   OVERRIDE (tech debt). Bootstrap: no observation + series says maker fees
   apply ⇒ fail-closed no-quote, never a guessed 0.
2. Enum from the exchange: add `QUADRATIC_WITH_COMBO_MAKER_FEES` → maker
   branch; fetch series fee_type at startup + on the metadata cadence; wire the
   zero-caller fee_changes poll on the maintenance tick (alarm only).
3. The fee enters EVERY EV via one change: `_candidate_edge_cc`
   (lifecycle 3172-3179) net of `_fill_fee_cc`. The fill ledger, confirm-time
   admission EV, eviction key and KILL-marginal all derive from it (verified
   chain). A fee-negative candidate then hits book_risk 3077 and is admitted
   only as a certified risk-reducing hedge inside the tail budget — "hedges
   always fill" preserved, everything else refused.
4. Quote-time floor so we never post the fill we would renege on (7/25 audit):
   compute fee_no with the measured schedule but do NOT add it to the bid;
   clamp the rebate to `min(margin//2, margin − fee_no)` and raise margin to
   fee_no when the tier is below it (the razor number dissolves into
   max(razor, measured fee) — retired by measurement, which the operator must
   ratify knowing it). Option W (widen by the fee) only per tier after the
   tape counterfactual shows the fill-probability cost.
5. Reporting: fills.fee_cc exact, realized P&L debited (6721-6722 nonzero),
   ev_ledger grades expected-NET vs realized; back-fill the 547 rows as a
   separate write task after operator go.

Gates: unit pins (parse of the live string; a 20-fill ground-truth fixture
reproduces at 0.035 and FAILS at 0.0175; drift alarm on a synthetic 0.0175
tape); parity to the cent on all 547 post-onset fills; quote-production
counterfactual on ≥100k real decisions under {today, floor, widen} with
non-zero sends on every tier above the fee (a 100%-decline tier fails);
vitals 8/8 + pre-ship; sends/min before/after inside 300–460; first 20
charged fills reconcile model vs exchange fee to the cc.

## 2. The MLB cells (mechanisms 2a–2d)

### 2a/2b — the REBATE ate the margin (rfi×rfi and HR NO-baskets)

```
legs → classify_leg (legtypes.py:349) → game_key (grouping.py:23-46) → classify_legs
(relationships.py:1132-1187: cross-game pair = NO group) → beliefs = Kalshi leg microprice
(legs.py:48-85) → build_sgp_correlation: cross-game ρ = cross_event_rho = 0.0
(sgp.py:1048/1069-1073; config.py:486) → copula (joint.py:73-110) → markup_for
(markup.py:223-263; MLB ≥35¢ 1¢, 25-35¢ 2¢, 20-25¢ 2.5¢, <20¢ 3¢; yaml 299-306)
→ construct_quote: margin = max(half_width, markup) (quote.py:167);
  rebate cap: inventory_skew_cc ≤ margin//2 (quote.py:187-188, since 8/16; before 8/16 the
  rebate could equal the WHOLE margin, the 7/26 rule at quote.py:169-176);
  no_raw = (1−fair) − margin − fee_no(=0) + skew   ← positive skew RAISES our NO bid
Skew: offset rebate = w_off·min(d_e,|net|)·util (skew.py:743-750); directional clamp
[−150,+600] (755-758); composed clamp [−750,+1200] cc (870-872, defaults at 148-149);
"leg_diversifying" rows armed (yaml L110); applied via lifecycle 5748-5760 re-price.
```

The 32 rfi×rfi combos: all 2-leg, all CROSS-game, all legs NO-side (66/66) =
"no first-inning run in A AND in B"; $2,211 premium, −$585 (−26.5%), 19W/13L;
model edge $10.77 = 0.49%; implied P(both NRFI) 28.5% vs realized 47.3%;
day-clustered z −1.9 to −2.3. Fair is EXACTLY the independence product of the
NO marginals (reconstructed from quote_sent context: 7/28 mids 44.15/52.97¢
→ 0.5585×0.4703 = 26.26¢ = recorded fair 2626) — correct for cross-game. The
tier was 2¢. What was left after the rebate, per contract, on the 26
pre-8/16 fills: [2,4,5,5,6,6,7,7,8,11,14,14,18,25,32,32,36,51,72,72,73,83,
114,132,138,162] cc → **median 0.215¢, 17/26 below 0.5¢, four at 0.02–0.05¢**
(8/7 fill: 155.7 ct, rebate ≈1.98¢ of 2¢, retained 0.02¢, lost $112.88).
Post-8/16 (7 fills): all ≥ half tier (59–167 cc). Why the rebate fired: the
skew engine rebates anything that OFFSETS or DIVERSIFIES the book; a family we
hold nothing of is maximally "diversifying" and earned the rebate with no
measured evidence that the flow carries edge. One taker (c4b41308) sent 18/33
fills and is net-losing to us on every other cell.

Partial-SGP (538 combos, −$300 vs model +3.85%): reproduces. Decomposition:
35 combos carrying a player_hr leg = −$481 (shortfall −58.6%, z −2.1 to −2.6);
the other 503 = +$181 (z −0.26 = noise). The losers are all-NO HR baskets
("Witt, Perez and Trout all fail to homer"): fair 45–73¢ → 1¢ mains tier →
rebate → 0.02–0.19¢ retained; four tickets 8/12–8/14 lost $290. The 8/12
skew record (rfq 218fd9b6): skew −184 cc on a 100 cc margin → capped 100 →
no_raw 4869 → snapped 4860 → 9 cc retained on 282 contracts ($137 ticket).
The "highest modeled edge" is WIDTH (uncertainty on KS-heavy multi-leg
combos: 8/13 sampled quotes had half-width > tier), not correlation credit.
Hypotheses tested and rejected: margin-not-scaled-with-correlated-pairs (no
monotone pattern by block size or n_legs); width-on-independent-product (no:
joint.py:94-97 prices ρ, ρ±band). Whole-book signature pre-8/16: all-NO
baskets 229 fills / $8,532 / tier markup $221 / retained $67 / $172 rebated
away / 54% of fills below half tier; all-YES 5%. Post-8/16 all-NO: 3%. The
8/16 cap closed the leak mechanically; what remains is that the cap is a
fraction and the fee model's $0 is wrong. Same-game HR/KS/RFI ρ tables are
not implicated by any cut. **No correlation promote.**

Open measurement (not a refit): are NO-side prop/RFI leg fairs biased by
retail longshot bias (HR-YES/RFI-YES mids rich ⇒ NO complements cheap)?
13/32 NRFI pairs hit vs 28.5% expected; 8/11 HR-NO blocks paid the taker;
z ≈ −2 on 32/35 combos. Leg-level calibration over the recorded universe,
game-clustered, ≥2 weeks, decides; the fix would be a per-family devig/shrink
in the belief source (legs.py:48-85), never a pair-table promote.

**Repair (2a+2b together):** replace the hand fraction at quote.py:187-188
with a MEASURED post-rebate retained-edge floor:
`floor_cc(cell) = fee_cc(bid; measured 0.035 schedule) + AS_cc(cell) + z·SE(cell)`,
AS = max(0, −(realized−modeled) per contract) from the ev_ledger settlement
grade, pooled ≥14 days, game-clustered SE, empirical-Bayes shrink toward the
sport pool; cell key = (sport, sorted leg types, side pattern, same/cross
signature) from existing pure functions — no lists; fail-closed: a cell with
< n_min settled games uses the sport pool's UPPER bound. Then
`rebate ≤ max(0, margin − floor_cc)` and `rebate ≤ the measured ES-reduction
value of the candidate` (risk/concentration_steer.py:400 value_cc_per_contract
— a family we hold nothing of reduces no ES ⇒ earns ≈0 rebate). Floor
estimator in the slow loop, cached; quote path O(1). Gates: replay of all
3,697 MLB fills (the 229 pre-8/16 all-NO fills retain ≥ floor on 100%);
quote-ability on a 3-day decisions sample (only thin cells move, every cell
still produces a NO bid at real sizes); throughput within noise; parity
bit-identical; fail-closed synthetic cell; pre-registered ≥2-week outcome.

### 2c — KS lines: NOT a calibration defect (retracted)

The KS price IS the Kalshi leg microprice (model fair ≈ raw leg-mid product
within 0.1–0.2 pts); cross-game ρ 0, opposing-starter ks|ks +0.04. Leg-level
calibration on 2,686 quote-time mids (817/2,030 fills recovered; the rest are
in the mid-August tape loss), side-adjusted, game-date clustered:

```
line   inst  uniq   mean mid   hit    bias(pts)    t
  2     430   170    0.877    0.865    −1.2      −0.30
  3     589   234    0.852    0.883    +3.1      +1.09
  4     327   159    0.819    0.862    +4.4      +1.58
  6      94    67    0.730    0.840   +11.1      +2.48   (WATCH; 8/05 tail cell)
 ALL   1779   908    0.838    0.859    +2.1      +1.30   (unique-leg +0.5)
```

Lines 2 and 3 hit at the same rate (P(2+) 0.867, P(3+) 0.853) because Kalshi
lists each pitcher's ladder around his own median (sgp.py:505-511). The
line-3 +$1,718 is 2-leg pure-KS (+$1,641), and five tickets ($179–$316 on
COL-SF, MIL-LAD, ATL-BAL, SEA-HOU, TEX-ATH, 7/26 and 8/14–8/16) = $1,235 of it
— Kirby, Sasaki, Baz×2, Freeland, Jump recorded ≤2 K; the largest single
ticket is cross-game NYY/TOR×CWS/DET +$388. Line-2 −$456 is the mirror draw
at one-tenth the dollars; day-bootstrap P(pnl≤0) = 0.82 — not even reliably
negative. Leg-result integrity: AND(leg results) == combo result on
2,958/2,958 resolvable combos (5,813 legs fetched, all finalized).
**Ship nothing to pricing.** Ship the pre-registered monitor (tools/):
cells = family × line × mid-bucket, bias = hit − mid, game-date SE; FAIL =
≥150 instances, |t| ≥ 3 on two consecutive non-overlapping ≥2-week windows,
same sign in unique-leg space → only then a per-(family, bucket) input
correction p' = p + δ (δ measured, shrunk by n/(n+n0), uncertainty += |δ|,
decays if the next window fails). Today δ = 0 everywhere.

### 2d — the whale: a same-game pair-ρ question first, a counterparty adder second

Whale 0f9b27cb3e: 861 MLB fills, $16,443 = 24.7% of MLB premium, ROI ≈ −0.2%
vs 2.7% model (excess ≈ −$478, cluster t −0.5); weekly W30 −41%, W31 −6%,
W32 +27% (+$1,455), W33 −17%, W34 −10%, W35 −2%. By our NO price band:

```
band     whale n   cost    pnl    ROI   cnt-win  $-win | other n   cost     pnl    ROI
15–30¢     40    $594   +$600  +101%   40%     48%   |   150  $1,589   +$883   +56%
30–50¢    169  $2,864   −$653  −23%   41.4%   33.9%  |   603  $8,887   +$603    +7%
50–70¢    482  $8,564   +$511   +6%   62.9%   64.5%  | 1,356 $22,367 +$1,708   +8%
70¢+      165  $4,343   −$371   −9%   78.2%   72.4%  |   673 $16,862   +$356    +2%
```

The band loss is real (t −2.5, day-bootstrap P(pnl≤0) 0.98) and size-shaped
inside the band (bottom-half size +9%, 50–90th pct −28%, top decile −27%) —
but its LEG picks are not better than our mids at any size (bias −1.4/+1.7/
−1.8 pts by size tercile, all |t| < 0.6; KS +0.3), and 30-min markouts run in
OUR favour (−1.15¢). The loss sits in the JOINT: 115 of the 169 band fills are
same-game 2-leg pairs (hrr|hrr 28, ks|ks 26, hit|hit 25, spread|total 14 =
−$389), priced from the UNROUTED sign-spanning batter-pair blend
(config.py:958-963) and +0.04 for opposing starters. Nothing in the stack
knows who is asking: rfq_creator_id lands only in fills.raw_json
(persistence.py:1076); the Rfq dataclass has no creator field (models.py:37-46);
no reader anywhere. The constitution already ruled: no counterparty
blocklists; a repeat-decay design is MEASUREMENT-FIRST (8/16 plan :143-152).

**Repair order:** 6C first — measure same-game 2-leg pair ρ on the filled
book (realized P(both) vs product of quote-time leg mids vs copula at shipped
ρ; implied ρ via conditionals_mlb.implied_rho; game-date clustered; FAIL =
|ρ_meas − ρ_shipped| > shipped band 0.06–0.12 on two windows) → a fail routes
the pair to the :same/:opp table the code already anticipates
(sgp.py:830-870), fixing the price for every counterparty. 6B built DARK:
CreatorLedger keyed (creator × size-decile-within-creator × price band),
excess = Σ(realized − expected), time-decayed at the creator's own cadence,
shrunk toward the population, adder = max(0, −adverse_frac) × fair_NO capped
at the size-tier width, applied once through the markup series-adder seam —
never a rebate, never a decline, never a list; shadow-logged with applied=0.
If 6C removes the whale residual, 6B stays dark permanently (that is the
test). Gates G1–G7 in the read (still quotes ≥99% of a real slate; engages on
the whale's shape only; two weeks shadow then armed; no double layer;
byte-identical with an empty ledger).

## 3. Soccer BTTS-yes × over (mechanism 3)

```
legs → classify_legs groups BTTS+TOTAL of one game (relationships.py:1131-1140)
→ engine._joint_or_noquote (engine.py:558-659)
   structural_applicable? (structural.py:737-757: soccer AND all legs in ONE game)
     YES → StructuralPricer.try_price → dixon_coles.invert (547-675):
           2 team-level legs = EXACTLY identified; residual > 0.005 → StructuralError (640-644)
           → engine.py:626-647: tie×total/btts → DECLINE (8/15 guard); btts×over → fallback_note
             ONLY, silently to the copula
     NO (pair embedded in a multi-game combo) → copula by construction
→ copula: soccer btts|total = 0.70 (config.py:539), band 0.12; cross-game pairs ρ = 0
→ markup 1¢ mains / 2¢ 15-35¢ / 3¢ <15¢ (yaml 247-265)
```

Route reconstruction: 26 filled RFQs with surviving quote context replayed
through the live modules → 23/26 within 0.01¢, the four club btts×over-2.5
pairs all within 0.01¢. **All 4 bare club btts×over-2.5 fills (NIJBOG,
NCXLEO×2, CARWRE) were REJECTED by the 0.005 bar** (residuals 0.0062, 0.0078,
0.0102, 0.0164) and priced on the copula; the only structural club pair was
btts×UNDER-3.5. Why the bar fires: a two-parameter Poisson/DC scoreline
cannot represent the BTTS/over shape Kalshi books quote — at P(over2.5)=0.68
the model's max P(btts) is 0.688 (balanced lambdas) vs market NIJBOG
0.697/0.679; residual 0.006–0.018 > 0.005. The bar is a hand-set constant,
10× stricter than the 3-leg over-identified bar (0.05): the same CARWRE game
rejected the pair at 0.0164 and accepted the triple at 0.0195 the same
afternoon. fit_challenge.classify_fit has zero callers; structural_fits has
0 rows ever (record_structural_fit never wired).

The constant: soccer btts|total 0.70 is the July-6 World-Cup BLEND of club
+0.75 [.69,.80] and international +0.67 [.62,.71] (git 470f24b; NOTES.md
308-309; results_soccer.md:262 = +0.746). We quote CLUB soccer since 8/13 and
never un-blended. Held-out 2024/25 (1,752 games): log-loss copula@0.70
1.20075 vs @0.746 1.19881 vs DC exact 1.19937 (0.746, DC and the DC-implied
hybrid statistically tied; 0.70 worse, borderline); both-YES cell realized
43.0% vs predicted 40.5% (0.70) / 41.5% (0.746) / 41.6% (DC). Per-match
DC-implied latent ρ mean 0.748 — the structure reproduces the pooled 0.746.
Typical match λ=(1.5,1.2): NO fair 57.81¢ (DC exact) / 58.92¢ (0.746) /
59.97¢ (0.70) / 72.20¢ (independence). **Measured bias ≈ 1¢/pair (0.70 →
0.746); the 2¢ DC-vs-copula gap is a model-form comparison.** On the actual
fills the booked edge was 1.05–2.15¢/ct → true edge ≈ 0 to −1¢; the $9.55
booked on the 17 combos is phantom.

Realized (club, sides from position_ledger): same-game btts-YES×over-YES 17
combos / 20 games, $384.46, −$54.47, 6 hits vs 5.91 model-expected (z −0.75 =
variance on a zero-edge book, not a sign error); btts-YES×UNDER 8, −$2.84;
cross-game-only btts×total 6 combos, $109.54, −$90.74 (4/6 hits vs 2.0,
priced at ρ 0; n=6 with clustered games — flagged, not explained). WC July
11 combos +$45. The sweep's "−$128 to −$145" bucket = these club cells
combined (all btts+total 31 combos −$148).

**Repair:** (1) config promote (rule 8b, no code): soccer btts|total 0.70 →
0.746 (the club measurement already in the repo); band 0.12 stays until Liga
MX / MLS / UCL / EFL are measured (calibration set is D1/E0/F1/I1/SP1). Shift
≈ −0.9 to −1.1¢ on our NO bid for club btts×over-2.5 pairs, 0.00¢ elsewhere.
(2) Replace the hand-set exact-fit REJECT with a DERIVED verdict (prototype
in tools/, port to dixon_coles.invert + structural._price, parity): bar = the
two legs' belief uncertainty (what the books can resolve); between that and
the hard bar → CHALLENGE = price + widen by misfit + record; for symmetric
pairs price the MARKET marginals through the copula at the DC-implied ρ per
match (marginal-consistent, self-adapting to lopsided vs balanced, scored
identical to DC exact OOS). Same derived bar for the over-identified case.
(3) Wire record_structural_fit (persistence.py:631) from engine 626-647 for
every REJECT/CHALLENGE/ACCEPT + a vitals counter "structural fallback share
by pair family" (slow loop only). (4) Do NOT extend the tie guard to
btts×over — sign is right, largest club SGP family, declining fails "must
still quote". (5) Multi-game stacks fix themselves through the sport table
(sgp.py:1072-1074). (6) Cross-game btts×total at ρ 0: pre-register a
game-clustered watch; no number moves without a measurement. Gates: held-out
backtest (promoted config and hybrid no worse than 0.746, beat 0.70);
parity to the cent on the 26 contexts with exactly 0.00¢ on every combo
without a same-game btts|total pair; live-RFQ probe on the next club slate
(the 8/15 13/14 precedent) with sends/min within noise; vitals 8/8;
structural_fits receives rows within the first hour; weekly game-clustered
alarm |z| > 2 over ≥30 games.

## Build order (all mechanism repairs, no knobs; each gated to still quote)

1. Fee: observer + enum + one edge function + fee-aware rebate clamp — pre-relight.
2. Rebate: measured retained-edge floor + ES-value bound (2a/2b) — same clamp site, can ship with 1.
3. Soccer: 0.70 → 0.746 promote (immediate, config) + derived fit bar + structural_fits telemetry.
4. Whale: 6C same-game pair-ρ measurement; 6B CreatorLedger dark.
5. KS: monitor only. NO-side prop/RFI leg-calibration measurement (2a/2b open item).

## NEXT STEPS

- Operator: ratify (1) the fee repair shape (measured coefficient, fee-net EV,
  fee-aware floor — which retires the razor by measurement unless our fair
  beats the field by more than the fee); (2) mains eat vs widen = pooled-
  evidence decision, not now; (3) the soccer 0.746 promote; (4) build order.
- Me: prototypes in tools/ → port with parity → gates → relight readout with
  before/after sends. Back-fill the 547 post-onset ledger rows after go.
- Pre-register: the KS monitor cells; the NO-side prop/RFI leg-calibration
  read; the cross-game btts×total watch; the 6C pair-ρ windows.
