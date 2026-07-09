# Demo combo RFQ round-trip — maker landed LONG NO (settlement pending)

**Date:** 2026-07-09 · **Status:** TRADE LEG DONE (live on demo); **settlement
observation PENDING ~Jul 10–11** · **Goal:** verify `combo_no_pays_complement`
(does a combo's NO side pay `1 − product` to the cent?) and the combo
`accepted_side → long-side` direction — the two Phase 2.5 conventions gating
sell-only fills.

## What was executed (live demo, two accounts)

Requester `create_rfq` → maker `create_quote(yes_bid=0, no_bid=$0.50)` → requester
`accept_quote("no")` → maker `confirm_quote` → **executed**. Sequence mirrors
`ops/ground_truth.py`. DEMO only (`.kalshi.com` guard). Scripts in job-tmp:
`combo_roundtrip.py`, `combo_settlement_check.py`.

| Fact | Value |
|------|-------|
| Combo | `KXMVECROSSCATEGORY-S2026C1138DA69BC-7ADA8E5486D` |
| Legs | `KXMLBGAME-26JUL092005LAATEX-LAA` (yes) **AND** `KXMLBGAME-26JUL101915BOSNYM-BOS` (yes) |
| Meaning | combo YES = LAA win (Jul 9) **and** BOS win (Jul 10); NO = at least one loses |
| Maker fill | BUY, `is_taker=False`, 1 contract, fee **$0.00** |
| Maker position | **`position_fp = -1.00` → LONG NO** (short the YES combo = parlay SELLER) |
| Cost | $0.50 (balance 1083.12 → 1082.62) |

## Verified LIVE (not from docs)

1. **Full combo RFQ→quote→accept→confirm→fill flow works on demo.**
2. **Sell-only config works in the real flow** — quoting `yes_bid=0` left the
   requester able to accept **only NO**, landing the maker exactly where intended:
   **long NO, the parlay seller.**
3. **Combo direction convention:** accept-NO → `position_fp = -1.00` (long NO) —
   matches the single-market Phase 2.5 fixture. The `accepted_side → long-side`
   half is now confirmed for combos.
4. Maker fee $0 (`is_taker=False`), as on single markets.

## PENDING — the settlement observation (the whole point)

The legs are MLB games **Jul 9 (LAA) and Jul 10 (BOS)**, so the combo resolves
**~Jul 10–11**. Then:
- combo settles **NO** (a team loses) → our NO should pay **$1** → realized
  **+$0.50** on $0.50 cost.
- combo settles **YES** (both win) → NO pays **$0** → realized **−$0.50**.

Observing that our NO pays exactly `1 − product` to the cent **confirms
`combo_no_pays_complement`** → the lifecycle stops declining NO confirms →
sell-only goes from inert to live-capable.

**How to check (any session):** run `combo_settlement_check.py`, or manually:
`GET /markets/{COMBO}` → `result`/`status`, and `GET /portfolio/positions?ticker={COMBO}`
→ `realized_pnl_dollars`. Combo + cost basis are in the table above.

## NEXT STEPS

- **Owner (next session, ~Jul 11):** re-run the settlement check once both MLB
  legs finalize; confirm NO paid `1 − product`; set `combo_no_pays_complement` in
  `tests/fixtures/ground_truth/conventions.json` (promote via the Phase 2.5
  discipline) → un-gate sell-only fills.
- **Persist:** move `combo_roundtrip.py`/`combo_settlement_check.py` into
  `tools/` (job-tmp is ephemeral) — flagged.
- Relates to [[project_kalshi_combos_two]] and the sell-parlays-only fix report.
