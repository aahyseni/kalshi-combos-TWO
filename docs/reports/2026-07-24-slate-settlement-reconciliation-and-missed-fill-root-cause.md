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

## NEXT STEPS

- **Build (owner: me, next):** incident-C fix per spec above (count-tolerant
  match + skip logging + fills-sweep reconciler) — ships BEFORE any relight;
  it is a state-awareness precondition for P1.
- **Build (owner: me, the main line):** P1 concentration/hedge rebuild (hard
  net-bounds incl. per-entity, P(book)-aware sizing, arm the mutex-aware skew,
  relight neutrality) — design dossier in progress via the code-read fan-out.
- **Watch (owner: me):** the +$0.02 identity residual (a ~4¢ non-settlement
  credit); attribute if it grows.
- **Operator:** no decisions owed by this report; resume-posture decision
  (stay flat until P1 vs interim guard) still open from the post-mortem.
