# 2026-08-16 — THE SHARD WALLET DISCOVERY: resting quotes lock nothing; 58% of flow was bouncing off an $8.79 sub-wallet

Operator ruling ("we should never lock based on resting quotes — it
shouldn't affect anything; I want up to 70% filled daily, split as incoming
RFQs come in") triggered a source-of-truth fleet (docs + live probes +
61GB of logs). The operator was right, and the finding rewrites this
week's cash-exhaustion narrative.

## The verdict (three independent proofs)

**Resting maker quotes lock ZERO collateral.**
1. Live identity probe (16:21–16:22 ET): exchange `balance` frozen at
   exactly 391,608 cents across 3 samples while our open-quote census
   swung 1→4→2 quotes ($22.66→$217.38→$68.67 worst-side). A lock would
   have moved ~$195; it moved $0.00.
2. Overnight tape: ~100,063 creates at ~300/min (~150 resting
   steady-state, $6–13k rolling worst-side) on $3.7k cash — ZERO
   insufficient_balance 400s.
3. docs.kalshi.com (create-quote/delete-quote/rfqs/get-balance, fetched
   twice independently): no collateral/hold language anywhere; maker cash
   commits at execution as ordinary position cost.

**The real mechanism: per-shard wallets.** `GET /portfolio/balance
balance_breakdown`: exchange_index 0 = **$3,896.23**, exchange_index 1 =
**$8.79** (top-level balance = the sum). CreateQuote is
sufficiency-checked against the shard the RFQ's market clears on.
`KXMVECROSSCATEGORY-SHARD1-*` = **57.9% of today's RFQ flow (899,147 of
1.55M; 62.8% by notional)** — every SHARD1 create bounced off $8.79
(240/240 armed RFQs joined to SHARD1 tickers; every successful create
non-SHARD1).

**The compounding bug was ours:** CashGateSender treated a shard-1 400 as
GLOBAL cash exhaustion — after the first arming at 07:34 ET it suppressed
**697,365 creates** (97.9%), sends ~300/min → ~13/min, fills $1,290.61
before 07:35 vs $92.86 in the 7 hours after. The "~200 resting slots
locking $3.4k" story from earlier today is retracted: measured exchange
census was 1–4 open quotes; locked collateral $0 at every sample.

**Retro flag (9/1 audit):** SHARD1 series entered our tape 8/6 18:05 ET —
the 8/13 (225k 400s) and 8/15 (181k) "cash exhaustion" storms are
plausibly this same artifact, not real exhaustion.

## The fix set

| # | action | status |
|---|---|---|
| T1 | **Fund shard 1**: `POST /portfolio/intra_exchange_instance_transfer` (docs-pinned schema; source/destination "event_contract", shard 0→1, centicents), amount = 57.9% flow share × cash − $8.79 ≈ **~$2,250**. Async; poll balance_breakdown. Fully reversible | **SCRIPT READY — operator executes** (harness correctly blocks the assistant from moving funds); success = cash_gate_armed → 0 within ~15 min, first SHARD1 fills |
| T2 | `open_quote_capacity_derived: "shadow"` (telemetry only; loads at next restart) | ✅ yaml staged |
| T3 | Shard-aware CashGateSender (per-shard arming so one thin wallet never suppresses the funded shard) — needs the ticker threaded through the sender protocol | 9/1 (first item), with N1 balance_breakdown consumption in BalanceTracker |
| T4 | Surface `_dropped_writes` (tape captured only 39.4% of creates this week) | 9/1 with the store-writer fix |

## The 70% capital math (for the operator)

- Capital is NOT the binder: resting book costs $0; full-book feasibility
  needs only per-quote sufficiency (~$1.7k equity clears it; we have 2.7×).
- The law: sustainable det% = daily fill intake × hold/24 ÷ equity. At the
  measured 12–14h holds, **70% of $4.7k needs $5.6–6.5k/day of fill
  intake**; the storm-afflicted week ran $3.3k/day; the unsuppressed
  overnight pace projects $4.1k/day off-peak. The remaining ~1.4× comes
  from the two headrooms tonight's fixes unlock: peak hours (97.9%
  suppressed today) and shard-1 (57.9% of flow, conversion unmeasured).
- Demand-mirroring: quote-mix error vs demand is only 15.2pp (restored by
  un-suppression); the FILL-mix error (39.3pp) is conversion — ML parlays
  are 25–32% of demand at 0 fills/193,756 quotes all week, which the 1¢
  tier (live since 11:06) now attacks. Even DEPLOYMENT split is a pricing
  outcome, not an allocation wall — no new caps built (rejected: cash
  reserve floor, size trimming [no size field in CreateQuote — docs], slot
  partitions).

## NEXT STEPS

- **Operator:** run the transfer (one command, in-chat); optionally re-run
  it weekly as the flow share drifts (re-derived from the census).
- **Me after it lands:** verify 400s→0 + first SHARD1 fills + sends
  recovery to ~300/min; evening det tracker; fold T2 into the next natural
  restart; 9/1: T3/N1 shard-aware plumbing, retro storm audit (N4),
  store-writer fix, ML-parlay conversion read.
