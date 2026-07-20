# 00 — SYNTHESIS: pricing a much bigger % of incoming RFQ flow

**Provenance:** synthesized by the orchestrator session from the four lens docs (`01-flow-ledger`,
`02-latency-anatomy`, `03-hotpath-architecture`, `04-exchange-constraints`) after the synthesis
agent hit API-529 twice (2026-07-16 ~18:45Z). Every number below is from a lens doc with a primary
source; nothing new is measured here. Operator cut applied: interventions are grouped so the
**non-decline levers** (flow we never get to quote) lead, and the decline-family (our own risk
policy) is separated.

## 1. The flow-loss waterfall (measured, game-day window 17:30–18:13Z + today's funnel)

| stage | share | bucket | addressable? |
|---|---|---|---|
| Raw wire (154–233 msg/s) | 100% | | |
| → out-of-allowlist fastpath drop | ~84% | by design (sports-only) + HOLES: `KXMENWORLDCUP` (accidental!), tennis/WSL/UFC (policy), MLB (post-WC flip) | partly — breadth levers |
| → in-scope flow | ~16% | | |
| → **intake-queue eviction (INVISIBLE — never reaches the DB)** | **27.9% of in-scope** (vs 3.0% when the pool is healthy) | 32-deep queue, `quote_app.py:707` | YES — pool health + queue |
| → decided funnel: **POST race lost** | **44.4%** | wire→pickup p50 **0.78s** vs exchange quote window closing **~0.67s** after creation; pickup→POST is only 0.13s | YES — intake speed |
| → decided: own risk caps (AFTER full pricing) | 32.8% | slots 41.9% of window + game-loss/mass-acceptance/cvar | POLICY (separate track) |
| → decided: filters | 13.6% | pregame gate etc. — mostly by design | mostly no |
| → decided: pool deadline | 4.4% | 2s deadline, cold-combo 5–8s tail, frozen-worker cascade | YES |
| → **quoted** | **4.8% of decided / ~1.3% of wire** | but **~72% of in-scope flow gets PRICED** — coverage is NOT the problem | |

Presence/breadth context (lens 4): **85.7% of today's prints were on tickers we never quoted**;
on peak days (Jul 8–12 = ~96% of recorded taker notional) only ~6% of prints happen ≤2s — **half
print ≥30s after the last RFQ**, i.e. against liquidity that *persisted*. The exchange imposes NO
quote TTL and NO open-quote cap; our 20s self-delete and 60-slot cap are ours alone. Advanced
tier gives ~150 quote-POSTs/s; we use ~0.5/s (99.6% headroom).

## 2. Ranked interventions — NON-DECLINE levers first (the operator's cut)

### A. PRESENCE — stop racing, start resting (biggest prize on peak days)
1. **`rest_remainder: true`** on quotes — partial fills rest as book liquidity; plumbing exists
   (`exchange/rest.py:215`, defaults false). Effort S. **Risk: widens the mass-acceptance
   worst case → risk-engine sign-off required.**
2. **Longer/adaptive quote TTL** — 20s (`QUOTE_TTL_S`, `quote_app.py:128`) forfeits the ≥30s
   print tail; exchange has no TTL. Needs reprice discipline (interacts with F3 below). Effort S.
3. **Pre-created combo markets + resting GTC orders** on the ~1.4k/day tickers that actually
   trade (`CreateMarketInMultivariateEventCollection`, 5,000/week; batch order API). We become
   the standing maker instead of racing 20s windows 360× per traded ticker. Effort L. Needs
   settlement/fee verification on combo books + risk sign-off.
   **Gate for A:** per-bucket markouts + mass-acceptance worst-case audit BEFORE arming; pilot on
   top-20 tickers.

### B. THE POST RACE — get inside the ~0.67s window (44.4% of decided flow)
4. **Clock-skew check FIRST** (owed): the 0.77s wire lag could be partly clock skew — one
   measurement gates this whole family.
5. **WS sharding** (`shard_factor`/`shard_key` 1–100, doc-prescribed for our error-25) — N
   sockets each carrying 1/N of the firehose; the only path into the ≤1s cohort. Demo validation
   owed. Effort M.
6. **Dispatch fast-lane**: don't `record_rfq` (DB write) before pricing; order cheap rejects
   first (lens 3 F1/F2: monotone pre-pricing gate + mid-pipeline liveness checks — same decline
   outcomes, earlier, frees queue+pool for live RFQs). Effort S–M.

### C. POOL/CPU HEADROOM — kills the eviction spikes, the 4.4% deadline bucket, AND the heartbeat wedge
7. **Event-driven reprice (F3)**: the 0.5s maintenance sweep re-prices EVERY open quote =
   60–120 standing pool calls/s — **half of all pricing work**. Reprice on leg-book change
   events instead, staleness bounded by last-look. Effort M. (Also a precondition for raising
   `max_open_quotes` and for longer TTLs — sweep load scales with open quotes.)
8. **Cooperative cancellation of abandoned pool work** (`pricing_pool.py:16-18`): today a 2s
   timeout abandons the future but the worker keeps computing → tail bursts freeze all 8 workers
   (terminal ticks 68/68/0) → queue evictions spike 3%→27.9% → heartbeat wedge → today's kill.
   Effort S–M. **P0 — this is also the wedge-kill fix family.**
9. Cold-combo shedding (5–8s p99 tail), structural warm-start (F9), classifier caches (F8),
   snapshot count fix (F5). Effort S each, parity-gated.

### D. BREADTH — flow we never see
10. **`KXMENWORLDCUP` allowlist hole — looks like an OVERSIGHT, not policy** (it IS World Cup
    flow; one YAML prefix). **Operator: quick decision owed.**
11. MLB flip at the post-WC switch (currently fastpath-dropped; allowlist is `[KXWC]` only).
12. Tennis / Summer League / UFC — operator policy + pricing-coverage lens first.

### E. The DECLINE family (separate track, post-7/18-19 by operator decision)
`max_open_quotes: 60` (41.9% of the window) + game-loss/directional caps (26–33%) are policy,
already priced flow. Decision on waiver-netted game-day data as agreed. NOTE: raise slots only
AFTER C7 (sweep load scales with slots) — else more slots = more standing pool load = more kills.

## 3. Quick wins (this week) vs structural

**Quick:** clock-skew check; cooperative cancellation + heartbeat-in-sweep (P0, pre-7/18);
`KXMENWORLDCUP` decision; fast-lane ordering (F2 then F1 prototype-first per rule 8);
`rest_remainder` pilot behind risk sign-off; teardown `shutdown(wait=False)`.
**Structural:** WS sharding (demo semantics first); event-driven reprice; pre-created markets +
resting book (the regime change); belief quantization (LAST — it's a pricing change, operator
decision, backtest + parity per rule 8).

## 4. What NOT to do
- No pricing-math changes for speed without the rule-8 prototype→port→parity chain (F4 last).
- Never weaken fail-closed/UNKNOWN⇒no-quote or the E2 monotone caps for throughput.
- Don't raise slots/TTL before the sweep is event-driven (self-DoS).
- FIX protocol intake: deferred — doesn't fix pricing CPU (2026-07-14 call stands).
- Don't read raw reason tallies as flow shares (co-occurrence trap — lens 1).

## 5. NEXT STEPS
- **Me (P0, pre-7/18):** wedge hotfix = C8 cooperative-cancel + beat-heartbeat-inside-sweep +
  never serial-await a frozen pool during cancel_all; then relaunch (after followups review).
- **Me (quick wins):** clock-skew measurement; endpoint_costs/limits verification (pin 2-token
  CreateQuote + 300/300 to our account).
- **Operator decisions owed:** KXMENWORLDCUP prefix (recommended: add); rest_remainder +
  TTL-extension pilot (needs risk sign-off); slots/caps after game-day waiver data; allowlist
  expansion policy (tennis/WSL/UFC); F4/F6 pricing-adjacent choices.
- **Build order after wedge fix:** B-family (race) + C7 (event-driven reprice) → then A-family
  presence pilot → then E (slots/caps) on measured data.
