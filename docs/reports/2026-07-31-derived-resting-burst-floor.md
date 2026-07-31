# DERIVED resting burst floor — the constant 3 stopped meaning what it meant when the slate grew

**2026-07-31 · risk/burst_floor.py · SHIPPED BEHIND `risk.burst_floor_derived` (default OFF)**

## WRONG / FIXED / OPEN

| # | claim | verdict |
|---|---|---|
| 1 | The resting haircut was undone / mis-armed | **WRONG.** It is armed (`resting_quote_weight 0.01`) and it applies. |
| 2 | The BURST FLOOR carries a per-AXIS/BUCKET count of 3, and the notional axis buckets PER GAME | **CONFIRMED in code** — `exposure.py` `k = max(0, resting_floor_count)` feeding `_topk_sum_int(values, k)` once per game bucket. |
| 3 | So the floor costs `3 x games` of our own unaccepted offers at 100% — 6 slots at the 2-game WC design point, **45 on today's 15-game MLB slate** | **CONFIRMED.** 79.2% of the number `SKIP_UTILIZATION_BACKSTOP` refused flow on. |
| 4 | The gross-PREMIUM axis floor (whole-book, k=3 total) inflates `gross_cc` | **WRONG (retracted upstream, re-confirmed here).** That axis composes $707.76 against a $5,000 cap, util 0.142 — it never binds. |
| 5 | `resting_quote_weight = 0.01` is doing the work | **WRONG.** It is INERT: `min(full, max(blend, base+topK))` picks `base+topK` in all 15 games. `resting_floor_count` is the entire mechanism. |
| 6 | Fixed by DERIVING the floor from live slate state + our own measured acceptance tape | **SHIPPED**, disarmed by default, HEAD-parity 0/600, both vitals tiers GREEN. |
| 7 | The backstop's axis (gross SETTLEMENT notional) is the right risk measure for a sell-only book | **OPEN — see §6. It is not a risk measure at all.** |

## 1. The derivation

```
k_g = max( K_MIN , min{ k : P( Binom(N_g, p_hi) > k ) <= alpha } )
```

| symbol | what it is | where it comes from |
|---|---|---|
| `N_g` | resting quotes contributing to THIS bucket | LIVE state — the slate shape enters here, not through a knob |
| `p_hi` | one-sided **Clopper-Pearson upper** bound on the per-quote acceptance probability | MEASURED: accepted / sent, cumulative on this process's own tape |
| `alpha` | tail budget **and** confidence level | the RATIFIED `portfolio_kill_tail_prob` = 0.02. **No new number.** |
| `K_MIN` | 1 — one WHOLE quote | STRUCTURAL: the smallest burst for which the operator's own sentence ("a burst of few large quotes is never haircut away") is still true |

Live tape 2026-07-31 10:10:29Z→11:02:35Z: **18,221 quotes sent, 7 accepted**, p̂ = 3.84e-4,
**p_hi = 8.13e-4** (2.12x the point estimate).

## 2. It is a MECHANISM, not a renamed constant

| tape | p_hi | k(N=16) | k(N=118) |
|---|---:|---:|---:|
| cold start, 50 sent / 0 accepted | 7.5e-02 | **4** | **15** |
| cold start, 200 sent / 0 accepted | 1.9e-02 | 2 | 6 |
| **LIVE 18,221 / 7** | 8.1e-04 | **1** | **1** |
| if 1% of quotes were accepted | 1.2e-02 | 1 | 4 |
| if 5% of quotes were accepted | 5.0e-02 | **3** | 11 |

Two properties fall out and both are load-bearing:

* **FAIL-CLOSED ON THIN DATA.** A cold process derives **4**, above the hand-set 3 — strictly more
  conservative until its own tape earns the relaxation. The estimator is a CUMULATIVE in-process
  count, so **there is no staleness surface to fail open through**: it cannot go stale, only thin,
  and thin fails closed.
* **IT REPRODUCES THE OPERATOR'S 3** where 3 was right (a 5%-acceptance tape on a 16-quote bucket).
  The typed constant was never wrong — it was a snapshot of a book that no longer exists.

## 3. What it does to the wall

Live decomposition at T=10:19:00Z (237 resting quotes, 15 games, bankroll $2,909.06, backstop 3x = $8,727.18):

| floor | slots at 100% | composed $ | util | 30s buckets breached |
|---|---:|---:|---:|---:|
| typed k=3 (today) | **45** | 9,097.26 | **1.042 BREACH** | **14 / 18** |
| **derived k=1** | **15** | 4,965.66 | **0.569** | **0 / 18** |

**$3,761.52 of headroom** on the axis that was refusing flow, none of it a risk-appetite change —
it is our own unaccepted offers being counted once instead of three times per game.

The same-slate sweep on a 240-quote synthetic book (`tools/proto_derived_burst_floor.py`):

| games | flat slots | derived slots | composed ratio |
|---:|---:|---:|---:|
| 2 (the WC design point) | 6 | 2 | 0.425 |
| 8 | 24 | 8 | 0.367 |
| 15 (today) | 45 | 15 | 0.371 |

At the shape the 3 was sized on, the difference is 4 slots. That is why nobody caught it.

## 4. Safety argument

| invariant | how it survives |
|---|---|
| **E2 / monotone in the RESTING set** | `count` is non-decreasing in `N_g` (binomial tail is non-decreasing in n at fixed p); adding a resting quote grows BOTH the set and k, so a bucket's floor can never fall. |
| **F1 pre-gate lemma (monotone in CANDIDATES)** | candidates are not resting quotes and never enter `N_g` ⇒ `count` is INVARIANT in the candidate set. |
| **no cross-bucket coupling** | `N_g` is per bucket; a quote in another game cannot move this bucket's k. |
| **CONFIRM PINNED AT 100%** | the rule rides the SAME seam as the weight — forwarded to the snapshot only under `apply_resting_haircut`, and the confirm sites pass no rule. Regression-tested bit-identical. |
| **burst protection preserved** | property-tested: the composed fold still dominates the 100% fold of the bucket's K largest, and `P(bucket burst > K) <= alpha` at every bucket size. A genuine 3-monster-quote burst is still bounded; a bursty tape raises the floor back up automatically. |
| **arming** | `risk.burst_floor_derived` defaults False ⇒ byte-identical. |

## 5. Verification

| check | result |
|---|---|
| HEAD parity, disarmed, 600 randomised books × every axis | **0 mismatches** |
| prototype ⇄ port parity (p_hi, K over 8 bucket sizes, composed notional at G=2/8/15) | **equal to the cent** |
| new tests | `tests/test_burst_floor_derived.py` — **29 passed** |
| full suite | **3,465 passed / 3 failed** — the 3 are the concurrent re-anchor fleet's in-flight `RUIN_FLOOR_FRAC` NameError in an UNTRACKED `tests/test_kill_anchored_book_gate.py`, not this change |
| vitals fast tier | **8/8 GREEN** |
| vitals pre-ship tier | **1/1 GREEN** |
| throughput (min-of-25 interleaved rounds, 15-game/240-quote book) | snapshot HEAD 2,741.6us · disarmed **+0.28%** · armed **+1.39%**; full `LimitChecker.check` armed **-6.68%** (17,699 → 18,966 checks/min) |

The first cut of the armed path cost **+8.75%** on the snapshot because the floor was consulted
through a Python method call once per axis/bucket (several hundred times per check). It is now a
self-filling `dict` subclass, so the hit path is a C-level subscript — hence +1.39%.

## 6. The second-order finding — the axis itself is wrong

Two facts, both measured:

1. **Gross settlement notional scales with CONTRACTS, not premium.** Notional consumed per PREMIUM
   dollar by NO entry price: `<50c 2.53 | 50-70c 1.56 | 70-85c 1.29 | 85-92c 1.13 | 92-100c 1.07`.
   The cap taxes **cheap NO ~2.4x harder** — and cheap NO is our best bucket (measured returns
   `<50c +77.9%` vs `>=85c +1.1%`). The operator's own $143.89-cost / $368-payout fill (NO at 38.9c)
   is charged $368 of notional, and then **$1,472** after the per-game duplication.
2. **For a sell-only book the maximum loss IS the premium paid** — `max_loss_cc = contracts *
   entry_price_cc // 100`, which is exactly the det-max axis. For a long-NO position the settlement
   notional is what we might **RECEIVE**, not what we can lose.

So `SKIP_UTILIZATION_BACKSTOP` is **not a risk measure**. It is a CLEARING / OPERATIONAL constraint
(how many contracts the exchange has us on the hook to settle), and it is being read as if it were
risk, on an axis that is structurally biased against our highest-return tickets. Recommendation:

* keep it as an operational backstop, but say so in its name and its breach string;
* **stop summing per-game-duplicated buckets** into a number the string calls a "total" — the
  per-game duplication multiplies the true whole-book notional by 3.6x before any haircut;
* let the RISK question be answered by the axes that measure loss (det-max / premium / tail-prob),
  which the re-anchor fleet is lifting to the P(KILL-night) <= 2% anchor.

That change is NOT in this build — it moves an enforced cap's meaning and is the operator's call.

## NEXT STEPS

1. **Operator decision — arm it.** `risk.burst_floor_derived: true` in `prod-live-wc.local.yaml`.
   Nothing else moves; `resting_floor_count: 3` stays as the disarmed/fallback value and as the
   confirm-path pin. Watch the `burst_floor_derived` log line (emitted only when the derived shape
   changes) for `k_at_8 / k_at_16 / k_at_64 / k_at_128`.
2. **Expect it NOT to be sufficient on its own.** The backstop is only the #2 blocker: measured
   over 27,778 no-quote decisions, removing the backstop alone frees +440 quotes (+13%), while the
   ENTITY cap alone frees +4,739 (+144%). The entity blocker is a cap-stack contradiction
   (`per_combo 5% = $146.94 > entity 3% = $88.16`, ratio 1.667: any combo sized at the per-combo cap
   self-breaches the entity cap on EVERY entity it touches). That is a separate repair and it owns
   the road to the operator's $2k/15-game target.
3. **Owner:** flow-path fleet for (1); the cap-stack contradiction in (2) needs an operator ruling on
   which of the two caps is the real one before anyone edits either.
4. **Do not commit this tree as-is** — the concurrent re-anchor fleet has an in-flight test file that
   fails to import (`RUIN_FLOOR_FRAC`). Files changed by THIS build are listed in the session summary.
