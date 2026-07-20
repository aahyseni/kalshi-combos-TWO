# 2026-07-14 — Price discovery: we're the sharpest maker on our combos, with room to widen to ~2¢

**Method.** Recreated all 7 of our filled combos as fresh RFQs via the API
(`create_rfq` on the existing combo market ticker works directly; fallback =
`POST /multivariate_event_collections/{coll}` to mint from legs), read competing
maker quotes with `get_quotes(rfq_id, rfq_user_filter="self")`, then deleted.
**Never accepted a quote** — zero trades placed. Script:
`$CLAUDE_JOB_DIR/tmp/price_discovery.py`.

## Result (YES ask = taker's cost to BUY the parlay; lower = cheaper seller)

| Combo | Our fair | Our ask | Best other | We win by | Field |
|---|---|---|---|---|---|
| WC ADVANCE+BTTS | 24.4¢ | 25.5¢ | 26.1¢ | +0.6¢ (cheapest) | 26–48¢ ×16 |
| WC ADVANCE+TOTAL3 | 17.6¢ | 18.6¢ | 18.8¢ | +0.2¢ (cheapest) | 19–44¢ ×21 |
| WC ADV+GOAL+CORNERS | 6.6¢ | 8.0¢ | 8.8¢ | +0.8¢ (cheapest) | 9–48¢ ×15 |
| MLB AllStar NL+TOT11 | 38.0¢ | 39.0¢ | 39.6¢ | +0.6¢ (cheapest) | 40–61¢ ×7 |
| MLB AllStar SPREAD+TOT11 | 25.9¢ | 27.0¢ | 26.7¢ | −0.3¢ (2nd) | 27–42¢ ×11 |
| MLB PHI+BOS | 22.6¢ | 24.2¢ | 24.7¢ | +0.5¢ (cheapest) | 25–59¢ ×5 |
| MLB NL+PHI+TOT9 | 13.5¢ | 15.2¢ | 15.6¢ | +0.4¢ (cheapest) | 16–57¢ ×10 |

## Findings

1. **We're the sharp end of the market.** Cheapest maker on 6/7, 2nd on the 7th,
   winning by a thin **0.2–0.8¢**. That's why we now fill, and it's healthy — not
   a winner's-curse-sized undercut.
2. **Our fair is well-calibrated.** The market's *tightest* makers price at
   **fair + 1.5–2.2¢**; we price at **fair + 1.0¢**. Our fair agrees with where the
   sharpest makers cluster; we just charge less markup.
3. **Room to widen.** Raising markup 1¢ → ~2¢ would keep us at/below the tightest
   competitor on most of these combos while capturing ~0.5–1.2¢ more edge/contract.
   **Converges with the settlement re-grade's ~2.2¢ robustly-+EV floor** — two
   independent evidence lines (live market + settlement history) point to ~2¢.
4. Wide non-competitive tail (makers 3–20¢ above us) on every combo — the real
   competition is only the tightest 1–3 makers.

## Recommendation

Raise markup 1¢ → ~2¢. Caveat: one pregame snapshot on combos we already WON
(selection bias); markup is the operator's pooled-multi-week decision. But the
convergence with the re-grade makes this the strongest markup signal we have.
Operator's call (they set 1¢ as a taker-race competitiveness bet).

## Incidents / lessons

- `get_rfqs(rfq_user_filter="self")` does NOT filter — it returned the whole
  exchange's open RFQs (creator_id blank). A bulk-delete loop off that list tried
  to delete OTHER makers' RFQs (all correctly rejected `invalid_parameters`) and
  spent write budget → **1 bot 429** (recovered, no KILL). DO NOT bulk-delete off
  that filter. Our probe's own RFQs expire on their own (~30s TTL); nothing to
  clean up. Fills stayed 7 → probe created zero trades.
- `get_quotes` needs `rfq_id` + `rfq_user_filter="self"` (NOT `creator_user_id`);
  `create_rfq` works directly on an existing combo market ticker.

## Advanced tier

Still `basic`; upgrade 403. Our combo fills show as executed orders, but
multivariate/combo orders don't satisfy the "API-created Predictions order"
eligibility. Unlock = place ONE plain single-market API order (far-from-market
limit, cancel after). Not done — awaiting operator go (real order).

## NEXT STEPS

- **Operator (decisions owed):** (1) raise markup 1¢ → ~2¢? (live + settlement
  both support it; still your pooled-multi-week call). (2) OK to place one
  far-from-market single-market order to unlock Advanced tier?
- **Claude/next session:** if markup raised, watch fill RATE vs edge/fill (the
  trade-off); re-run this price-discovery after a markup change to confirm we're
  still at/below the tightest makers. Persist a proper leg→event map if we
  automate the probe.
