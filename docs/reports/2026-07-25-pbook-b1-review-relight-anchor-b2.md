# 2026-07-25 — B1 reviewed+hardened, bot RELIT (shadow slate LIVE), anchor repair + B2 shipped

**Bottom line.** The P(book) steer survived its adversarial review (15 findings,
all fixed), the bot is **LIVE on today's slate** (KC@DET 1:10p / LAA@SF 4:05p /
LAD@NYM 7:15p) with the steer measuring in shadow and `p_book` on every
snapshot, the 7/23 **re-hit bypass is closed** (accumulated per-combo bound),
and the confirm-side pay-up (**B2**: $1 of EV per $1 of certified tail
reduction) is built default-OFF. Suite **2720/0**. Commits `791b42b` →
`0192640` → `953188c`.

## 1. B1 adversarial review — 15 confirmed, 1 refuted, 27 attacks held

The three that mattered most (all fixed):

| finding | fix |
|---|---|
| Onset anchored to the static $1,000 SkewLimits dollar → steer near-inert at 7/23 scale (HIGH) | The lifecycle (the one owner with the live bankroll) publishes the **tightest ENFORCED budget** into the profile: min(hard game $cap, game_loss_frac × bank, **KILL 12% × bank**) ≈ **$261 now** → the $349 DET shape = full ~2.9¢ steer; $100 ≈ 0.4¢; auto-scales forever |
| n=1 tail book (the PUREST one-way shape) paid **zero** new-game rebate — a 0-vs-−0.9¢ discontinuity (HIGH ×3) | absent game under n=1 reads deficit −1 (the continuous limit); regression pins single-game ≈ epsilon-split |
| Published shares didn't sum to 1 with hedged (negative-attribution) games (HIGH) | normalize by POSITIVE mass only; hedged games publish as PROTECTED — flow there earns no rebate (hedge erosion) |

Plus: composed `skew_cc` re-clamped to the documented two-pair bound when armed
(arming never expands the price-move envelope), delta-neutral candidate games
now reach the component, full pbook decomposition logged at INFO (the arming
record), stale docs corrected. **Arm-gate documented:** the classification
reads the mass-acceptance snapshot while the profile is committed-book —
shadow measures the disagreement rate before arming.

## 2. RELIT (operator-authorized) — shadow slate LIVE

10:50a first light → **11:12a restart onto the full build** (documented
procedure: kill tree → cancel-all 61 quotes (3 benign 404s) → relaunch).
`data/live_20260725_pbook_shadow2.log`: `prod_preflight_green`, 113 quotes in
the first minute, 0 halts. The 11:08a fill **rehydrated** (1 position, KC@DET,
0 reconcile mismatches) and the snapshot now measures the real book:
**`p_book: 0.2301`, ev +523cc** on the 1-position book. `pbook_cc`/
`pbook_per_game` on every quote (pricing byte-identical — `pbook_armed:
false`). Incident-C fills-ledger sweep arms itself (15-min cadence).
**WATCH:** one `rehydrate_unmodeled_positions` warning (an open exchange
position with no local record) — the sweep alarms it if real; Monitor armed.

## 3. Anchor repair (`0192640`) — the $149-re-hit bypass closed

`ExposureSnapshot.loss_by_combo_cc` (committed + reserved + the check's
candidates/reservations per combo MARKET; resting quotes excluded — the serial
reservation chain re-checks at every fill) + limits check (3b): the ACCUMULATED
loss now binds the SAME per-combo anchor (same reason code, "ACCUMULATED"
detail). Cannot brick quoting: fires only per market once committed exposure
there nears the threshold — precisely the re-hit shape. Not waivable; no
E2/quote-fold surface touched.

## 4. B2 (`953188c`) — derived certified-hedge budget, default OFF

`hedge_budget_tail_derived`: budget = max(static, PRE−POST certified
governing-tail reduction on common random numbers) — **pay $1 of EV per $1 of
risk actually removed**. Self-scaling (one-way book = big budget for exactly
the balancing flow it lacks; balanced book ≈ nothing), sniper-tax defense
unchanged (non-reducing negative-EV fills still decline). Arms together with
`pbook_armed`.

## ARMING CHECKLIST (after the slate)

1. Read the shadow record: `pbook_cc` distribution + per-game factors/reasons
   (INFO logs), the committed-vs-mass-acceptance disagreement rate, ΔP(book)
   on admitted fills (`candidate_gate_confirm.delta_p_book`).
2. Operator eyeballs magnitudes (the fills⇄curse dial is his).
3. Flip in the armed YAML: `pricing.skew.pbook_armed: true` +
   `risk.allow_negative_ev_hedge: true` + `risk.hedge_budget_tail_derived:
   true`; restart via the documented procedure; before/after quotes-per-min.

## NEXT STEPS

- **Me (during/after slate):** watch the Monitor (fills, sweep alarms,
  ACCUMULATED fires); resolve the unmodeled-position warning; post-slate
  shadow read-out report + P&L; then the arming flip on operator go.
- **Me (next builds):** entity axis (player/team, all leg families) + the
  same-player markup adder; skew static-denominator retirement (SkewLimits
  from live caps — the remaining North-Star debt at this seam).
- **Operator:** post-slate — eyeball the shadow magnitudes, then say "arm".
