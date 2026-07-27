# 2026-07-26 — P(book) correctness, breaker rebuild, and the settlement verdict

**Status at write time:** bot DOWN, `KILL` present (supervisor heartbeat kill,
16:12 ET). Heartbeat/liveness fix in tree, suite 2941/0, awaiting relight.
**HEAD:** `da29585`. Uncommitted: liveness decouple + bounded enforcement.

---

## 1. The headline: p_book was WRONG, and our fair is CONSERVATIVE

Two independent findings that point opposite directions and together explain the
whole "low p_book / losing money" picture.

### 1a. The book-risk MC penalised diversification (CRITICAL, fixed, shipped)

`sim/book_model.py::_blocks_for_band` collapsed each GAME to ONE rho = `max()`
over every pair in that game, then wrote that scalar onto EVERY pair.

| measure | collapsed (shipped all week) | exact per-pair |
|---|---|---|
| within-game off-diagonal mean | **+0.755** | +0.077 |
| ordered pairs inflated / deflated | **636 / 0** | — |
| pairs sign-flipped neg → +0.95 | **53** | — |

Consequence, measured: **adding a HEDGE to a game LOWERED P(book)**
(0.9284 → 0.8508). Fixed: 0.9284 → 0.9646. Every mechanism consuming p_book —
skew, rebates, the diversification steer — was told diversification was harmful.
It is also non-stationary: adding ANY leg to a game can only raise that game's
max, so p_book degraded monotonically as the book grew, including on hedges.

**Fix = axis split, not a band flag:**

```
LOCATION (ev_cc, p_profit, p_night)   -> exact per-pair matrix, POINT band
                                         (the joint that PRICED the fills)
TAIL/GATING (es_99, governing ES,     -> UNCHANGED conservative game-max
 p_ruin, loss_quantiles, det-max)        collapse at the ADVERSE band
```

The tail keeps the collapse **deliberately**: a Gaussian copula at low per-pair
rho has no tail dependence, but a blowout game hits every over at once. An
adversarial reviewer blocked the first cut because removing it took
`governing_es_99` → 0.00 and `p_ruin` → 0.0000 — the enforced gates. Gate parity
is now proven **bit-identical to HEAD across 150 cells / 6 seeds / 3 bands**.

Live confirmation after restart: **p_book 0.6394, EV +$5.81**, stamps
`tail_joint=corr_tail_stress band=high` / `location_joint=corr_location
location_band=point`.

Throughput: `build_book_model` 318ms → **117ms (−63%)**, quote gate **−12%**.

### 1b. Settlement grader: our fair is biased CONSERVATIVE (n=208 fills)

Read-only grade of fills we already hold:

| | modelled | realized |
|---|---|---|
| edge on $2,647.70 premium | +$61.15 | **+$372.38 (6.1x)** |
| combo hit rate | 35.3% | **16.8%** |
| 4–6 leg pure-K slice (n=29) | P(all hit) 0.508 | **0.138** |

We **overstate** parlay hit probability. p_book is built from the same
marginals, so this made p_book **too pessimistic on exactly the concentrated K
stacks**. Answers the operator's "are we quoting 6-7c too rich on high-% K
combos" — settlement says the opposite.

**Do NOT refit on this.** It is a measurement, and the standing rule is no P&L
refit. It justifies a structural re-derivation of the AND-probability, tracked
as its own build.

---

## 2. Why the book stayed one-directional (measured)

```
quote rate on OFFSET flow     8.47%   (8,265 / 97,604)
quote rate on REINFORCE flow  8.48%   (43,219 / 509,485)
```

**Direction-blind to three significant figures.** And `skip_directional_cap`
refused **5,512 risk-REDUCING RFQs** — a violation of the standing "hedges
always fill" invariant by the one cap that exists for direction.

Structural limit to state plainly: we are **sell-only**. Every fill is long NO
on a parlay — the same side by construction. Diversity can only come from WHICH
LEGS, never which side. Directionality is partly inherent; the fixable part is
leg/entity selection, concentration refusal, sizing, and (not built) soliciting
offsetting flow.

---

## 3. Incidents today (5 halts + 1 kill) and what each taught

| # | time ET | cause | verdict |
|---|---|---|---|
| 1-4 | through 14:00 | metadata breaker: post-close, early determination, far-future close, expiration drift | fixed incrementally |
| 5 | 14:15 | `halt_metadata_change` on an in-play total: `status active→inactive` | **breaker was watching the wrong fields** |
| 6 | 16:12 | `supervisor_heartbeat_wedged` age 30.1s > 30.0s | **my regression** |

### The breaker was watching the wrong fields entirely

```
OLD fingerprint:  status | event_ticker | close_time
                           ^^^^^^^^^^^^^^^^^^^^^^^^
                  two of three are LIFECYCLE stamps Kalshi rewrites at
                  EVERY close -- and it watched NONE of: rules_primary,
                  rules_secondary, strike_type, floor_strike, cap_strike,
                  expiration_time, latest_expiration_time
```

Five whole-bot halts, every one a false positive, while a real strike change
(Total 5.5 → 6.5) would have passed through unnoticed. Rebuilt into two lanes:
a **payoff fingerprint** (hard halt + needs_reconcile) and a **lifecycle
fingerprint** (quarantine that one market). `disputed`/`amended` never exempt.
Permanent deactivation (VOID) handled structurally — a market that never returns
to `active` never leaves quarantine.

**Validated live within 6 minutes:** 12 `market_quarantined` events in one
end-of-game wave. All 12 would have been whole-bot halts this morning.

### Incident 6 is mine

`quote_app.py:3480` runs `_enforce_market_quarantine` inside the **maintenance
loop — the same task that beats the liveness heartbeat**. 12 markets quarantining
at once did sequential REST quote-deletes (several 404ing) and blocked the loop
29s. The supervisor correctly killed a healthy, quoting bot.

Fix: liveness decoupled to a dedicated task; enforcement bounded/concurrent; 404
on delete treated as already-gone. **Timeout NOT raised** — that would trade a
false positive for a false negative. Paired with an independent per-loop
progress signal so a genuine wedge is still caught.

---

## 4. Other confirmed defects fixed today

| defect | impact | state |
|---|---|---|
| exchange exposure parse | `cc_from_decimal_dollars` RAISED on all 46 live rows (Kalshi sends 6-decimal dollars) → **the fail-closed reserve never fired in production** | fixed, `exposure_cc_from_dollars_str`, 46/46 parse, rounds fail-closed |
| residual reserve basis | pro-rated at exchange average price → 20.4% understatement on a mixed-price ticker | exact subtraction of modeled `max_loss_cc` |
| lagging payload race | fill confirmed during paged fetch → 28.6% undercount, divergence alarm SILENT | HOLD larger + alarm |
| durable ledger identity | settled rows could never land after restart (id keyspace) | keyed on `leg_set_hash` |
| entity cap (earlier) | armed at 3% → **0 quotes, 3,994 breaches**; scanned every key in book | scoped to candidate keys |

Docs discrepancy recorded per hard rule 4: `getting_started/fixed_point_migration.md`
says 4 decimals, `api-reference` says 6, wire sends 6.

---

## 5. What is LEFT — ranked

### Blocking relight tonight
1. **Liveness fix gates** — adversarial + relight-readiness. Suite 2941/0 already.
2. **Relight** via `START_BOT.bat` (shows KILL contents, asks y/n).

### Urgent — my own fix creates this
3. **`need = 1 - p_book` attenuation.** `risk/skew.py:748,905` multiply every
   skew component by `need`. p_book ran ~0.38 → need ~0.62. The rho fix raises
   p_book toward ~0.96 → need 0.04, a **15x attenuation** that drops family
   steering (median 0.61c, beta -1.5 log-odds/cent — the only component with
   measured authority) to 0.04c, an order of magnitude under the fill-halving
   threshold. **The steer becomes decoration.** Root confusion: p_book answers
   "am I profitable", not "am I concentrated". Replacement (KILL-tail
   utilisation tau) is MEASURE_FIRST — shadow-log tau for one slate alongside
   need with an explicit fallback branch, then decide.

### High value
4. **Direction-normalized admission** — certified risk-REDUCING unit sized
   against `|held|`, via the existing `WaiverCertificate` enumeration at
   `risk/limits.py:383-398`. Fixes the 5,512 refused hedges. Scope discipline:
   repair the directional cap + enumerated-joint aggregation for slate/game
   first; do NOT ship a blanket four-cap waiver (the slate cap is legitimately
   gross for a sell-only book).
5. **Entity axis divides by the WRONG wall** (verified bug):
   `rfq/lifecycle.py:1216-1247` hands one `budget_cc` to both family and entity
   axes, but the enforced entity wall is `threshold_cc(0.03, bankroll)`. Entity
   component measured at median 0.03c / max 0.14c = decorative by construction.
   **Gate: replay against the shadow tape proving non-zero quotes per family
   before arming** — 8 of 8 logged top entities are already over the wall, so
   arming naively prices every KXMLBKS combo at the ceiling = de-facto quoting
   halt on 71.7% of our filled-leg mix. That is the 7/23 bootstrap failure.
6. **Persist steer decomposition + stratum key to `decisions.context_json`.**
   18.3% of today's premium ($134.85 / 7 fills, incl. the day's largest $81.47)
   is UNATTRIBUTABLE because it fell in a hole of a 2.5 GB rotating log; 11
   fills have a `risk_audit` with no `quote_sent` row. Precondition for
   measuring whether any of the above worked.
7. **Symmetric widen bound** — `pricing/quote.py:181` caps the rebate at margin;
   the WIDEN direction is unbounded. When `no_raw` goes non-positive the builder
   emits `SKIP_PRICING_FAILED` with **no risk reason code** — a refusal
   manufactured by the pricing layer, invisible to the decline-report rule.
8. **Esports/entity interaction** — `skip_entity_loss_cap` refused esports
   **1,638 times** today. Esports RFQs here are cross-sport tickets pairing a
   LoL/CS leg WITH MLB legs, so our most diversifying flow is refused because it
   touches a concentrated MLB arm. The cap judges a combo by its worst leg, never
   by its net effect on the book.

### Known gaps, not yet scheduled
9. Book vs exchange truth: measured **-$9.03 (1.41%) under**; 18 positions
   ($329.84, 51.6% of book) have legs resolvable ONLY from the lossy rfqs tape.
10. Runtime quantity divergence (exchange > book) is alarm-only; a ticker
    modeled at a smaller size is never topped up.
11. `recovery_owned` hands tickers to a sweep that iterates only THIS run's
    in-memory quotes — a prior-run fill or manual trade is owned by nobody.
12. Log volume: 2.5 GB in ~5 hours, same disk as the store. Needs bounded
    rotation.
13. Self-RFQ suppression — must ship WITH the first solicit build, not after
    (`rfq_created.creator_id` is empty, so it must key on ids our own
    `create_rfq` returned, written BEFORE the call).
14. Racing start-time policy (operator deferred).
15. Map-winner rho (KXCS2MAP / KXLOLMAP) before those can quote.

---

## 6. Honest process note

Three live-impacting regressions shipped by me today (entity cap bricking
quoting, breaker heartbeat coupling, and — caught pre-ship by review — the tail
gates going to zero). The pattern is identical each time: **my tests covered the
clean single-item path and missed the system-under-real-load path.** The entity
cap was never tested with the book ALREADY over a wall; the quarantine was never
tested with a 12-market end-of-game wave. "Degraded/at-capacity state" and
"realistic fan-out" need to be explicit test dimensions for anything touching
caps, liveness, or breakers.

Also: I reported "EV -$47.72, book underwater before a pitch" repeatedly as
fact. ~47% of it was a modelling artifact, ~16% deliberate conservatism on the
wrong axis. And I quoted a point-in-time equity read (+$470) as a settled result
while a slate was live; it was $2,391 (+$391) shortly after. Both were stated
with more confidence than the evidence supported.

---

## NEXT STEPS

- **Owner: operator** — relight via `START_BOT.bat` once liveness gates clear
  (it will show the KILL contents and ask y/n). Decide on item 3 (tau shadow vs
  leaving the steer attenuated for one slate).
- **Owner: next session** — item 3 (tau shadow-log), then 4/5/6 in that order;
  5 does NOT arm without the shadow-tape replay gate.
- **Decisions owed by operator:** (a) whether the TAIL joint should eventually
  move to a measured tail-dependence stress instead of the game-max collapse
  (pre-register against settlement history; do not choose by feel);
  (b) whether diversifying-but-cap-touching combos (the esports case) get a
  net-effect admission path; (c) kill_anchor % and resume posture.
