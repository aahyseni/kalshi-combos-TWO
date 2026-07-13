# Risk engine — phased build plan (canonical)

**Date:** 2026-07-12. Supersedes the build order in `README.md` — folds in the
external-review adopt-items (`EXTERNAL_REVIEW_ASSESSMENT.md`). Ordering
principle: make the numbers CORRECT before anything caps on them; make the caps
SAFE before concurrency; add the tail monitor and the challenger before scaling;
external watchdog before autonomous live. Every phase: prototype-in-test → port
→ parity-check (rule 8) → adversarial judge → SHADOW (log-only, zero P&L impact)
→ enforce. Each is independently shippable.

Marks: [DONE] shipped · [R] a genuine review-adopt item · [core] original plan.

---

## PHASE 0 — Foundation [DONE, merged b67fbee]
B1 two money axes (true max-loss vs gross notional), B2 game-key aggregation,
BalanceTracker (live balance + realized-P&L ledger). This is the spine every
later phase reads/writes.

---

## PHASE 1 — Correct the money (the authoritative accounting layer)
**Fix:** equity-aware bankroll denominator [R] (use `min(start-of-day,
available_cash + haircut·portfolio_value)`, not available cash alone — else caps
shrink just from deploying capital); book FEES in the settlement ledger [R]
($0 for us today but correct + future-proof); complete SCALAR settlement [R] in
the pricing fair (E[∏ sᵢ], not P(all hit)) and the ledger (settle to 1−∏sᵢ, not
just NO-win/YES-loss). Rename `payout_obligation` → `gross_settlement_notional`
and never cap cash/loss on it [R].
**How it helps:** every downstream cap % and every P&L number is now anchored to
the RIGHT figure. Prevents false drawdowns (deployed ≠ lost) and wrong fair on
the rare scalar case.
**Effect on the engine:** the book reports true bankroll, true realized P&L
(fee + scalar aware), true capital-at-risk. The caps in Phase 2 become trustworthy.
**Move on when:** predicted-vs-exchange-ledger reconciles to the cent across
demo fills including a fee case and (if constructible) a scalar case.

## PHASE 2 — The caps + the slate level (downside protection)
**Fix:** wire the R2 cap hierarchy at the $2,000 values — %-of-GAME loss cap (on
`worst_case_loss_by_game`), per-combo max-LOSS (fix the notional-vs-loss label
[R]), directional, absolute-notional backstop, daily-loss, drawdown, hard-trip.
**ADD the SLATE / time-window PRE-TRADE cap** [R] — a new hierarchy level above
game (all unresolved games starting in the same 2–3h window / same league-day):
the single most important new control, because a daily-loss halt only fires
AFTER losses and many games settle in one window. Fail-closed + starvation
watchdog.
**How it helps:** limits what one game — and one evening's slate — can cost
before any halt can react. Closes the "two aligned games blow the hard-trip
before a halt fires" hole.
**Effect on the engine:** every quote (hypothetical mass-accept) and every
confirm (real committed) is gated against correct game + slate caps; near a cap
the quote widens toward decline.
**Move on when:** SHADOW mode logs every would-be breach on the tape with zero
quote impact; caps fire on the known concentration cases; no silent starvation.

## PHASE 3 — Concurrency & state safety (the reservation P0)
**Fix:** a single-writer risk-reservation service [R] — reserve capacity BEFORE
sending confirm (not after the fill), atomic + versioned, so two RFQs can never
both claim the same headroom; handle a confirm TIMEOUT as an unknown-committed
state (assume committed, reconcile against the exchange). (Race-free today only
because we run one asyncio loop; this makes it safe for any future fan-out.)
**How it helps:** prevents a silent cap breach where every check passes but two
concurrent accepts exceed the limit — the caps from Phase 2 can't be bypassed.
**Effect on the engine:** risk capacity is authoritative and race-free; the
hot-path stays constant-time (precomputed incremental risk, no fresh MC/joins
at confirm).
**Move on when:** concurrent-accept stress test shows zero double-reserve; the
timeout→reconcile path is tested; exchange-first startup reconciliation works.

## PHASE 4 — Portfolio MC + challenger overlay (tail monitor, anti-monoculture)
**Fix:** wire the portfolio MC (`sim/book_model` game-keyed block copula + the
NO-side correlation fix) → VaR/CVaR, P(ruin), per-game/leg tail attribution,
marginal ΔCVaR. **ADD a challenger/stress overlay** [R]: the operative risk
number = `max(production-copula ES, challenger ES, deterministic stress)`, where
challengers are correlation-inflated + empirical game-cluster bootstrap +
heavy-tail. (Importance sampling for the ruin tail comes AFTER these layers are
right — a precisely-estimated wrong number is still wrong.)
**How it helps:** a tail-risk view that ISN'T a monoculture of the pricer, so a
correlation error isn't approved twice. Feeds the drawdown/hard-trip halts and
the skew.
**Effect on the engine:** the background loop produces the tail numbers the
halts consume and the marginal-risk the skew consumes; enforced off the hot path.
**Move on when:** settlement-graded, game-clustered VaR coverage backtest
passes; the challenger disagreement is visible and gates the worst-case.

## PHASE 5 — Quoting policy: skew + widen-vs-decline + pregame precision
**Fix:** feed the built-but-zeroed inventory-skew seam (tighter on balancing
flow, wider on concentrating); **widen-vs-DECLINE** [R] — on NORMAL/uncertain
flow near a cap, DECLINE rather than widen (widening attracts hitters — our own
finding); the pregame precision ladder (schedule feed → quote to ~2 min before
kickoff, recover near-kickoff flow) with strict confirm-cutoff.
**How it helps:** the book self-balances via which combos it accepts; stops the
widen-attracts-toxic-flow trap; recovers pregame flow without pickoff risk.
**Effect on the engine:** pricing becomes inventory- and tier-aware and
courtsider-safe.
**Move on when:** shadow-grade the skew decisions (do they reduce portfolio
CVaR?) and the pregame markouts (no adverse short-horizon markout = no pickoff).

## PHASE 6 — External watchdog + go-live gates (autonomous safety)
**Fix:** an out-of-process safety supervisor [R] (separate host/credential):
heartbeat, emergency cancel-all, credential rotate, reserved API write budget,
block-restart-until-reconciled; the full circuit-breaker list (exchange/local
mismatch, data staleness/seq-gap, latency spike, 429 burst, marginal jump, rule/
metadata change, unmapped game key). Then the prod gates:
`prod_limits_configured` + `--confirm-live` + prod whitelist.
**How it helps:** the last line of defense — the kill can't depend on the bot's
own host (crash/deadlock/partition).
**Effect on the engine:** the system can run unattended and be killed externally.
**Move on when:** a kill-drill shows the external supervisor cancels everything
on a simulated bot death; reconciliation-on-restart is proven.

---

## After Phase 6 — how we actually go live
SHADOW the whole stack (log-only) → tiny live at **$2,000** with the
conservative first-live caps (per-game 3–5%, per-combo 0.5–1%, slate 6–8%,
drawdown 6–8%, hard-trip 8–10% — the review's tighter-than-8/10/12 start) →
accumulate POOLED MULTI-WEEK, GAME-CLUSTERED settlement → re-derive caps AND the
markup from that data (never from one window) → raise limits only on
game-clustered live evidence → scale bankroll.

## Parallel track (does not block the phases)
The MARKUP decision + the NORMAL/FAT room-predictor run as a data-accumulation
track (shadow the predictor, pool weeks) — the caps assume a profitable markup
but don't set it; the number comes from pooled data, decided separately.

## What does NOT change (verified correct)
Long-NO loss accounting (B1), the game-key fix (B2), the $0-maker-fee posture,
and the "not live until the above lands" stance.
