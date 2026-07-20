# 2026-07-21 — Adversarial review of the four post-WC builds: findings + fixes

Operator standard: every build gets tested AND agent-reviewed before it counts.
Four reviewers ran in parallel, one per build, each with a distinct attack
brief. **All four returned FIX-FIRST** — the pass caught two CRITICALs, four
HIGHs and a series of MED/LOWs that unit tests (2576/0 green) had not. Every
finding below was verified against the code before fixing; all fixes are in
this commit with regression tests.

## Findings table (verified → fixed)

| # | sev | build | finding | fix |
|---|---|---|---|---|
| 1 | CRIT | transfer watch | Mid-session deposit DOUBLE-COUNTED into the peak: the 10s balance poll's high-water mark absorbs the cash, then the watcher's `peak += Δ` adds it again → phantom give-back = Δ for the rest of the day → false human-only KILL on the build's primary use case (the old test suite encoded the REVERSE order and passed green) | Give-back now measured in **P&L space**: `A = equity − K` (K = detected-transfer ledger), peak kept over A with its set-time; `apply_external_transfer` orders each transfer's doc-verified `finalized_ts` against the peak/SOD formation instants and corrects exactly the double-counted portion. Production-order test added and passes |
| 2 | CRIT | reserve adoption × receivables | Self-leg `side` carried OUR position side → double-complement for NO reserves: **losers shielded the give-back halts with full notional, winners with nothing** (exact inversion of the shield's contract) | Self-leg side is ALWAYS `"yes"` (the leg encodes the combo's YES definition; direction lives solely in `our_side`). Sign pinned by tests in BOTH settlement directions |
| 3 | CRIT | reserve adoption | `_refresh_daily_pnl` froze the whole book's unrealized mark FOREVER while any reserve existed (early-`return` on the reserve's permanently-unreadable self-leg) — silently disarming the daily-loss cap's unrealized half | Reserves (`risk_modeled=False`) are SKIPPED with `continue` (their premium is already fully at-risk in the caps); liveness pinned by a sentinel test |
| 4 | HIGH | in-play exemption | Not restart-durable: rehydrate never fetched leg metadata and `_ensure_watched` never retried a 429'd fetch (gated on `_watched`) → post-relight estimate-path legs (soccer/WNBA) resolve UNKNOWN start → exemption stands down → the mid-slate halt storm returns | Rehydrate now ARMS every rehydrated leg (watch + metadata fetch, `_arm_rehydrated_legs`); `_ensure_watched` retries on `peek is None` instead of first sighting. Absent-metadata behavior + heal path pinned |
| 5 | HIGH | reserve adoption | Single unpaginated `get_positions` (default limit 100) + release-on-absence: page-2 reserves released as "flat" → real risk silently dropped, generation churn | Open listing PAGED to exhaustion (`count_filter=position`); release ONLY on a targeted per-ticker read parsing to an explicit zero row; absence alarms and HOLDS (fail-safe both directions). Pinned: two-page adoption + absence-holds tests |
| 6 | HIGH | transfer watch | Withdrawal detection lag reads as give-back until the watcher runs (latch risk); guessed withdrawal statuses ("complete"/"completed") could miss real withdrawals forever | Statuses now DOC-VERIFIED (`applied` is the money-moved status for both directions); cadence 300s→60s; the P&L-space K-ledger self-corrects the transient at detection. Residual: a ≥10% withdrawal inside the ≤60s window can still latch — filed as the pre-latch re-measure item (build queue) |
| 7 | HIGH | receivables | TTL "structural bound" defeated: the every-tick sweep re-noted an expired receivable one tick later, forever (fresh TTL each time) | TTL expiry TOMBSTONES the id (re-notes refused until the position leaves the book); re-notes preserve the ORIGINAL TTL clock |
| 8 | MED | transfer watch | UTC day-boundary race: transfer finalized pre-midnight, detected post-re-anchor → both anchors double-shifted all day | The `finalized_ts` vs anchor-formation-instant rule covers it (anchor formed after the transfer ⇒ no shift); pinned |
| 9 | MED | transfer watch | `applied→returned` clawback unhandled (one-way seen-set left anchors permanently shifted) | Status-TRANSITION tracking per id; a returned-after-applied applies the reversing delta; pinned |
| 10 | MED | receivables | "A wrong prediction is caught by the reconcile HALT" was not implemented — the noted amount was never cross-checked | `confirm_receivable(expected_credit_cc=…)`: the reconciler passes its exchange-derived credit; ≥1¢ disagreement drops the shield immediately with a loud error |
| 11 | MED | receivables | Non-settlement removal paths (phantom discard, reserve release) leaked the receivable for the full TTL | `cancel_receivable` hooked into both paths |
| 12 | MED | in-play | Negative pregame margins (unvalidated) could quote past start while the leg is exempt | `ge=0` validators (scalar + per-prefix) + defense-in-depth: exemption instant = `max(start, quote-cutoff)` by construction |
| 13 | MED | reserve adoption | Presence-only reconcile: exchange 12 vs book 5 contracts = invisible undercounting | Per-ticker quantity/side divergence alarm each pass |
| 14 | LOW | tools | float-on-money fee parse (rule 5), empty-store IndexError, missing settled_time crash, "?"-date era mis-bucketing | Decimal fee helper, guards, explicit unknown-date era row |

## Verified-clean (attacks that did not land)

In-process exemption polarity + tz handling + flap-immunity; breaker set
consumers (exempt sets feed only the marginal watch); dedupe order-independence;
receivable reentrancy; restart-mid-cascade anchoring; threshold semantics at
`max(0,…)`; reserve treatment in MC/det-max/waiver (deterministic reserve
outside model ES, own-singleton fail-closed only); pagination truncation
re-application (monotone paging can't resurface a baselined row); secrets
hygiene in all new logging.

## Filed (not fixed here, tracked)

- **Pre-latch give-back re-measure** (finding 6 residual): force one transfer
  poll + recompute before latching HALT_DRAWDOWN/HALT_HARD_TRIP — build queue,
  before unattended nightly slates.
- **Flat-release vs settlement-poller race** (reserve realized-P&L booking can
  be skipped if the reconcile wins): mitigated by targeted-read release +
  `cancel_receivable`; full fix = route releases through the settlement
  handler. Queue.
- **Receivable over-shield window** (cash absorbed by a poll before the row
  confirms): bounded by the now-real 30-min TTL; accepted.
- **AS1 (NOTES.md)**: settlements-row/balance-credit atomicity assumption —
  verify empirically at the next live settlement.
- **Scalar/DNP receivable coverage hole** (AS4): rain-shortened MLB
  settlements get no shield — MUST be weighed in the MLB gating decision.
- Pre-existing: per-reason (not per-ticker) breaker grace timer; `ws.py` UP041.

## Verification

Full suite green after fixes (count in the commit); mypy clean on all 7
changed modules; ruff clean on every changed file. Hot path untouched.

## NEXT STEPS

- Sport-switch readiness items 3–5 (per-sport models, rho settlement-regime
  audit, leg taxonomy) — now WITH the scalar-receivable hole on the MLB
  checklist.
- At first relight: the live drill the reviewers asked for — kill + relight
  mid-demo-slate to exercise rehydrate-arm + exemption durability end to end.
- Owner: bot session. Operator decisions owed: none.
