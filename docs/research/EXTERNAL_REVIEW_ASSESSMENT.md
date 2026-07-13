# External risk-engine review — assessment vs our sources of truth

**Date:** 2026-07-12. An external LLM reviewed our risk-engine briefing. Every
finding fact-checked against Kalshi's live docs (docs.kalshi.com, fetched
2026-07-12), our verified api-notes, NOTES.md ground truth, the demo settlement,
and the code at HEAD. Detail: `codebase_check.md`, `kalshi_check.md`.

**Bottom line:** a strong, well-reasoned review whose OVERALL verdict — harden
before autonomous live — is correct and matches our own posture. But its
framing overstates: its "#1 most serious" finding is real-but-known-and-mostly-
mitigated, its fee alarm is largely moot for us, and ~4 "blockers" are already
handled or reviewer-wrong. Net: ~6 genuinely valuable adopt-items, the rest
reassuring.

## Kalshi mechanics (source of truth = docs.kalshi.com) — all CONFIRMED
| reviewer claim | verdict | note |
|---|---|---|
| combo settles = PRODUCT of leg values; legs can settle scalar $0–1 | CONFIRMED | Kalshi rule; we already have it in `dnp_scalar_settlement.md` + `multivariate.md` |
| balance endpoint returns `balance` (cash) + `portfolio_value` (positions) | CONFIRMED | both fields exist; units cents |
| maker can't size a quote — full RFQ only; contracts OR target-cost mode | CONFIRMED | must-quote-full is real; "slice" is impossible |
| maker fee ~0.44¢/ct at 50¢ | CONFIRMED arithmetic, **REFUTED for us** | our RFQ maker fill pays **$0** (combo/sports series = quadratic ⇒ $0 maker fee; the taker pays it) — verified on the demo ledger |
| 3s confirm / 1s execute window | CONFIRMED | measured 117ms on demo |
| combo_no_pays_complement under scalar | CONFIRMED consistent | our NO=1−V is the binary special case of NO=1−∏sᵢ |

## The findings, categorized

### A. REVIEWER RIGHT — real forward-looking gaps to ADOPT
1. **Slate / time-window PRE-TRADE cap** (best catch). A daily-loss halt fires
   only *after* losses; many games settling in one window lock in exposure
   before the first result. 8% game + 12% hard-trip → two aligned games blow
   the trip before any halt. We have no pre-trade slate cap. ADOPT.
2. **Challenger / anti-monoculture model.** Our MC deliberately reuses the
   pricing copula (consistency), so a correlation error is approved twice.
   Add a stress/challenger overlay: `R = max(model ES, challenger ES,
   deterministic stress)`. ADOPT.
3. **External kill supervisor** (separate process/host, heartbeat, emergency
   cancel, reserved API write budget). An in-process KILL file doesn't survive
   a host crash/deadlock. ADOPT.
4. **Book fees in the settlement ledger** (`balance.py` books none). Real
   correctness gap — but **immaterial today** because our maker fee is $0;
   wire it anyway for future series + correctness.
5. **Widen-vs-decline near a cap.** On NORMAL flow, widening ATTRACTS hitters
   (our own finding), so "widen into a decline" can worsen selection — DECLINE
   NORMAL/uncertain flow near concentration instead. Refine R3 skew policy.
6. **Single-writer / atomic risk reservation.** Not a live bug today (one
   asyncio loop, sequential awaits — no race), but the moment we fan out with
   `create_task` the race appears. Add the single-writer guard PROACTIVELY.
Minor: rename `payout_obligation` → `gross_settlement_notional` and never use
it for a cash/loss cap (we half-caught this); fix per-combo cap label
(notional vs max-loss).

### B. REVIEWER WRONG or OVERSTATED (verified) — reassuring
1. **Fees make the edge negative → REFUTED for us.** We pay **$0 maker fee** on
   RFQ combo fills (verified on the demo ledger; the taker pays). So the fee
   alarm — and "studies not net of fees" — is largely moot on our side.
2. **"Code slices whales" → REVIEWER-WRONG.** `_risk_qty` sizes the full RFQ at
   the conservative price-dependent count; a breach DECLINES, never partial-
   quotes. The target-cost circular dependency was already solved (2026-07-05
   critical fix). "Slice whales" was loose *briefing* prose, not the code.
3. **"Per-game sum overstates" → REVIEWER-WRONG for the LOSS axis.** Summing
   per-combo max_loss IS the exact joint worst case for a NO-seller's premium
   loss (all combos lose their premium iff all hit). His mutual-exclusivity
   counterexample applies to the PAYOUT axis (not yet built), not the loss cap.
4. **"MC lacks scalar support" → REVIEWER-WRONG.** `sim/engine.py` already
   samples scalar settlement (inverse-CDF `min(∏,1)·$1`).
5. **"Balance = cash only shrinks limits" → PARTIAL/moot.** Our parser reads
   cash only (technically true), but the BalanceTracker isn't wired to any cap
   yet and current caps are hard dollars, so nothing mis-scales today; the
   equity-denominator fix is already DESIGNED in R2. Worth doing before the
   caps go %-of-bankroll.

### C. ALREADY HANDLED / KNOWN (reviewer couldn't see from the summary)
- **max_loss = capital at risk, not $1** — reviewer CONFIRMS our own B1
  correction.
- **Scalar settlement** — documented (`dnp_scalar_settlement.md`, 8 sections),
  explicit reactive-stance decision, fail-safe `HALT_RECONCILIATION_MISMATCH`,
  MC already supports it, exposure tiny today (MLB props → UNKNOWN → no-quote).
  PARTIAL: pricing fair + the ledger still assume binary — complete before
  autonomous live, but it is not an unknown blind spot.
- **Cross-game combos** — handled CONSERVATIVELY: a multi-game combo adds its
  full loss to EVERY game it touches (deliberate fail-safe double-count);
  neither reviewer failure mode occurs. True hyperedge/marginal allocation is
  designed, not yet wired.
- **Confirm order** — position parked before the network call, idempotent
  re-book on execute, halt after 3 confirm failures.

## Why so many findings are PARTIAL
The reviewer reviewed the BRIEFING (target architecture); the CODE ships only
the B1/B2/BalanceTracker FOUNDATION so far, with R2 caps + MC/bankroll wiring
as the declared next build. So most "gaps" are real-in-code-but-recognized-and-
designed in `docs/research/R1/R2`, not unrecognized.

## Adopt list added to the roadmap
Before autonomous live (extends the `docs/research/README.md` build order):
- Complete scalar settlement in pricing fair + the ledger (rare, but for live).
- Slate/time-window pre-trade cap (new hierarchy level above game).
- Challenger/stress overlay (anti-monoculture) → `R = max(model, challenger, stress)`.
- External kill supervisor (out-of-process).
- Single-writer reservation guard (before any concurrency fan-out).
- Book fees in the ledger; equity-aware bankroll denominator (min(SOD, cash+haircut·PV)).
- Widen-vs-decline: decline NORMAL flow near a cap; rename the notional axis.
No change to: the long-NO loss accounting (confirmed correct), the game-key fix,
the fee posture (we pay $0), or the "not live yet" stance.
