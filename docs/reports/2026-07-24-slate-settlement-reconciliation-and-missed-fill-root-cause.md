# 2026-07-24 — 7/23 slate settlement reconciliation (to the cent) + missed-fill root cause

**Bottom line.** The −$210.35 post-mortem number reconciles against the exchange's
own settlement ledger to within 3¢ of premium-basis rounding. Book is FLAT: **0 open
positions, $2,179.74 all cash; true all-time realized +$179.72** on the $2,000
deposit. The reconciliation surfaced ONE new defect: a real $28.69 maker fill the
store never recorded — root-caused below to an **exact-count structural-match guard
rejecting a partial execution** in the cancel-verification path (incident C, the
third variant of the 2026-07-18 "cancelled-but-executed" family).

## 1. The cent-exact chain (all exchange-ledger numbers)

| number | basis | value | reconciles as |
|---|---|---|---|
| window realized (26 rows settled ≥ 7/23 ET) | exchange cost | **−$205.07** | ground truth; `account_standing` store-era ✓ exact |
| slate-only (window − the pre-slate tri-game carry +$5.31) | exchange cost | **−$210.38** | post-mortem said −$210.35 → **Δ 3¢ = store-premium (cc) vs exchange-cost ($) rounding** |
| campaign tool Thu-7/23 bucket | store premium | −$214.51 | = window − the store-invisible fill's +$9.46 (− 2¢ rounding) ✓ |
| all-time realized | exchange cost | **+$179.72** | pre-7/23 +$384.79 − $205.07 ✓ |
| account identity residual | | **+$0.02** | equity $2,179.74 vs deposits+realized $2,179.72 — **flipped from −$0.02 on 7/21** (order-time fees); a ~4¢ credit outside settlement rows arrived. WATCH, not material |

Per-game, exchange basis (cross-game combos listed separately — the post-mortem
folded them into game buckets, which is why its per-game splits differ; totals match):

```
  KC@DET only            −262.92   (incl. the +9.46 store-invisible win)
  AZ@STL only             +16.75   (incl. Marte scalars −9.22 / −9.76)
  TB@TOR only              +8.02
  TBTOR+AZSTL             +20.35
  AZSTL+KCDET              +9.78
  TBTOR+AZSTL+KCDET        +1.58
  Detroit×Carroll×Marte    −3.94   (cross-game scalar — the risk-gap poster child)
  pre-slate tri-game       +5.31   (the 7/22-night $5.02 carry, settled in window)
  ──────────────────────────────
  window                 −205.07
```

## 2. Marte / same-player settlement verdict (the "+EV ruler" owed from 7/23)

- The two exotic same-player shorts settled scalar: **−$9.22 + −$9.76 = −$18.98
  combined** — the mutually-exclusive-losing-states bound held exactly as audited
  (feared $305 entity exposure realized <$19 of loss).
- The third scalar: `yes Detroit, yes Carroll 1+, yes Marte 1+` (CROSS-GAME,
  the exact combo from the player-concentration gap memo) −$3.94. Total scalar
  damage **−$22.92**.
- The taker profited exactly what a scratch-informed buyer takes from us
  (bought YES ~11¢, scalar settled ~16.8¢). **Scalar-floor pricing
  (YES ≥ ∏ leg book probs + markup) would have denied precisely this** while
  keeping the normal-outcome +EV. P2 design re-validated by the ledger.

## 3. NEW DEFECT — the store-invisible fill (incident C) — root cause CONFIRMED

Market: `…S2026BDCF5779B29-B70A8795A07` = "yes Detroit wins by over 1.5 runs,
no Over 8.5" (KC@DET). We were the maker, NO side. Settled `no` → **+$9.46 win
on a position the store never had.**

```
 2:38:19p ET  quote_accepted        NO, offered 57.44 ctr @ $0.752 (target-cost RFQ $15)
 2:38:19p    risk_reservation_granted → candidate gates → confirm_ok → COMMITTED
             (risk book booked 57.44 ctr = 5744 cc)
 2:38:20p    exchange EXECUTES 38.15 ctr (= $28.69) — PARTIAL of the 57.44 offered —
             and reports the quote CANCELLED, reason "execution failed"
 2:38:31p    fill_recovery_cancel_report_verifying   (the 7/18 lesson: verify, don't discard)
 2:38–2:41p  3 clean /portfolio/fills reads… each SILENTLY skips the real fill:
             _match_exchange_fill requires EXACT count == pending 5744 cc,
             exchange row says 3815 cc  →  "no structural match"
 2:41:34p    fill_recovery_quote_cancelled: "NO matching execution …
             phantom position removed, no fills row written"      ← WRONG
 later       position reconciler: "exchange count/side disagrees" (alarm-only),
             then relight adopts the position as a $28.69 no-context RESERVE
 9:35p       settles NO → +$9.46 realized, attribution lost until this recon
```

**Root cause.** `rfq/lifecycle.py` `_match_exchange_fill` (~:3719) demands an
exact centi-contract match against the *accepted/offered* quantity. Kalshi's
"execution failed" cancel on a target-cost RFQ can mean **partially executed**
— the fill is real at a smaller count. Two aggravators: (a) structural
non-matches are not logged (only adoption-guard rejections are), so three
verification reads looked clean while skipping the truth; (b) on "verified
absent" the position is fully removed rather than degraded conservatively, so
the risk book ran **$28.69 light on KC@DET direction** from 2:41p until the
reconciler's reserve adoption. The all-time exchange-vs-store diff shows this
is the ONLY missed fill ever (134 vs 133 fills; every other ticker matches 1:1).

**What worked:** the 7/18-built verify-before-discard ran as designed; the
position reconciler alarmed; reserve adoption re-counted the risk fail-safe
LARGER. The failure was the matcher's exact-count assumption, not the
architecture.

**Fix spec (build task, lifecycle-only + slow loop — fix-isolation, no pricing
path):**
1. **Count-tolerant structural match:** ticker + side + time-window + `count ≤
   pending` ⇒ match; adopt the EXCHANGE count as truth and shrink the booked
   position to it (order-id claim + ledger dedupe guards unchanged — they are
   what actually prevent double-count).
2. **Log every structural skip during cancel-verification** (ticker hit but
   count/side mismatch) — a real fill must never be invisible three times.
3. **Periodic /portfolio/fills sweep reconciler** (slow loop, read-only diff of
   store vs exchange per ticker; alarm + write-through on any miss) — the
   generic backstop for ANY writer-path miss, per the standing
   full-state-awareness rule.

## 4. Also observed in the 7/23 logs (P1-relevant, recorded for the design)

- `inventory_skew_shadow` events are LIVE (shadow) and already carry
  `mutex_direction_games` + per-game skew — the skew mechanism exists in shadow
  with a mutex-aware direction input; P1's "active hedging" arms/extends this
  rather than building from zero.
- `widen_vs_decline_shadow` ("near cap on concentrating flow, would_decline")
  is also running in shadow — a second pre-built P1 seam.
- `risk_audit` shows `fallback_reason: book_risk_generation_stale` frequently
  during the 2:38p window (MC snapshot age ~13s, generation 6 vs live 7) — the
  candidate-aware MC lags the book at high flow; relevant to P(book)-steering
  design (staleness must fail toward the coarse caps, which it does).

## 5. BUILD + ADVERSARIAL REVIEW (2026-07-24/25) — SHIPPED

The fix spec above was built, then a 3-lens adversarial review (default-refute,
per-finding verification, executable PoCs) **confirmed 20 findings against the
first draft — including two HIGHs that recreated incident C's failure shape
inside the fix itself**:

1. **Cross-quote steal:** the naive `count ≤ pending` rule let quote B adopt
   quote A's unledgered same-ticker fill — shrinking a REAL position on
   foreign evidence and double-writing one exchange order (PoC ran green
   against the draft).
2. **Multi-print residual:** one order printing at two levels adopted only the
   largest print; the remainder was silently invisible forever (PoC green).

**Hardening shipped (all PoC attacks now fail):**

| defense | mechanism |
|---|---|
| Exact-key verification | cancelled-quote payload's `creator_order_id` (doc-verified == `Fill.order_id`) captured; `/portfolio/fills` queried + matched by it; structural matching demoted to fallback |
| Multi-print aggregation | matcher groups prints by `order_id`, adopts the SUM (≤ pending), fees summed per print (any unreadable ⇒ honest UNKNOWN), raw prints ride the ledger row |
| Ambiguity fail-safe | with no exact key and another in-flight quote on the ticker, a partial group is REFUSED and the round can never conclude "genuinely absent" — position KEPT, loud (`verify_ambiguous_kept`) |
| Discard guards | `fill_recorded` bail-outs in resolution AND `_discard_phantom_position` (WS landing inside the final attempt's awaits can no longer discard a real, recorded fill) |
| Writer uniqueness | one exchange order = one ledger row (`fill_ledger.order_id_conflict` terminal + loud); executed-status recovery CLAIMS its order id while its write retries |
| Sweep robustness | whole diff phase inside try/except (store errors contained); 2.5s/page ×3 pages, limit 1000; truncation is loud + watermark clamped to oldest scanned; batched ledger reads (2 SELECTs, never 600 point reads); naive/legacy timestamps parsed as UTC; unparseable-timestamp misses HOLD the watermark; exhausted states no longer suppress alarms; claimed-unwritten rows visible; null-order-id ledger rows matched by (ticker,count) instead of false-alarming; unresolvable misses age out loudly instead of pinning the window |

**Accepted residuals (documented, narrow):** a print of an adopted order that
posts only AFTER adoption is invisible to the order-id-keyed sweep (position
reconciler + restart reconcile remain the backstops); an EXACT-count foreign
fill whose owner quote exhausted its whole recovery budget can still be
adopted by a same-size same-ticker verifier (pre-existing class, now bounded
by the writer-uniqueness guard); the sweep runs inline in the maintenance tick
(bounded ≤ ~7.5s worst-case + 2 batched reads — house REST-bound style).

Suite: full green incl. 18 new tests (8 incident-C + 10 review regressions);
ruff/mypy clean on every changed file.

## NEXT STEPS

- **DONE (owner: me):** incident-C fix + adversarial hardening (section 5) —
  shipped BEFORE any relight; the state-awareness precondition for P1 holds.
- **Build (owner: me, the main line):** P1 concentration/hedge rebuild (hard
  net-bounds incl. per-entity, P(book)-aware sizing, arm the mutex-aware skew,
  relight neutrality) — design dossier in progress via the code-read fan-out.
- **Watch (owner: me):** the +$0.02 identity residual (a ~4¢ non-settlement
  credit); attribute if it grows.
- **Operator:** no decisions owed by this report; resume-posture decision
  (stay flat until P1 vs interim guard) still open from the post-mortem.
