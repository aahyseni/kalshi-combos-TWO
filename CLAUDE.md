# CLAUDE.md — combomaker (kalshi-combos-TWO)

Working agreement and decision log for this repo. Keep this current: architecture
decisions, current phase, open questions.

## Mission (one paragraph)

Maker-side automated market maker for Kalshi combo RFQs. Top-down derivative
pricer: marginals come from Kalshi leg books (+ pluggable devigged external odds);
the only bottom-up component is the joint/correlation layer, treated with
conservative priors, Fréchet clamps, and width proportional to uncertainty.
Execution discipline and risk management are the edge, not pricing: freshness-gated
quotes, last-look at confirm, exposure aggregation incl. mass-acceptance worst
case, kill switches, markouts, and an EV ledger graded on aggregate expected edge
vs realized P&L — never on single outcomes.

## Hard rules

1. Never touch any path named `kalshi-combos` (without `-TWO`). Hard boundary.
2. Demo by default; prod needs `--env prod --confirm-live` + configured prod limits.
3. Secrets via env only; never committed, never logged.
4. Kalshi docs beat assumptions — discrepancies recorded in `NOTES.md`.
5. Money/prices are integers (centi-cents) outside the simulator. Binary floats
   for money are banned. Floats are fine in probability space.
6. Missing or stale data ⇒ no-quote. Every decision logged with a reason code.

## Quiet-failure defenses (operator directive 2026-07-05 — standing rules)

The failure species these defend against: an assumption that is wrong the same
way in the code and in its tests, so everything passes while losing money.

1. **Ground-truth conventions (Phase 2.5).** Direction/sign/fee-side semantics
   (who ends up long what when `accepted_side="yes"`, which side pays which fee,
   position signs, settlement values) are verified by REAL demo RFQ round trips
   whose results are recorded into a ground-truth fixture
   (`tests/fixtures/ground_truth/`). A single `core/conventions.py` is written
   against that fixture and **no other module may hardcode a convention** —
   pricing, risk, and P&L code import direction/sign facts only from there.
   Phase 2.5 runs BEFORE any convention-dependent pricing/risk code solidifies.
2. **No fail-safe inversions.** Every classifier (leg relationship,
   mutual-exclusion detection, settlement rules, market family) has an explicit
   UNKNOWN branch, and UNKNOWN always means **widen-or-no-quote** — never a
   convenient default (independence, binary settlement, "probably fine").
   Property test required: the UNKNOWN branch literally cannot reach
   CreateQuote at normal width.
3. **Ledger reconciliation gates (live phases).** Docs and worked examples can
   encode the same misreading as the code; only the exchange's actual ledger is
   ground truth. Every fill reconciles predicted vs actual — fee, position
   sign, balance delta, settlement value — **to the cent, and a mismatch halts
   (`HALT_RECONCILIATION_MISMATCH`), never just logs.**
4. **Maker-favorable rounding invariant.** Both quote prices are our bids;
   rounding to nearest gives away a tick half the time and on a thin combo a
   tick is the entire edge. Quote construction always rounds bids DOWN onto the
   grid (maker-favorable), property-tested. `round_to_tick(..., "nearest")` is
   for fair-value display only, never for bids.
5. **Self-grading circularity.** Markouts are computed against BOTH our fair
   and raw Kalshi leg mids (a biased fair can't catch itself). A standing
   outcome-calibration report buckets quoted probabilities vs realized
   settlement frequencies (reliability curve + Brier score) — settlements are
   the one ruler the model can't bend. Demo P&L validates plumbing, never edge;
   it is not a graduation criterion.
6. **End-of-phase assumption audit.** At the end of every phase, append to
   `NOTES.md` a table of every domain assumption the phase's code embeds,
   tagged `doc:<url>`, `fixture:ground_truth`, or `UNVERIFIED`. The human
   reviews UNVERIFIED rows before the next phase starts.

## Current phase

**Phase 0 — scaffold: COMPLETE** (215 unit tests green, mypy strict, ruff).

- [x] Repo, .gitignore-first commit, uv project
- [x] Config (pydantic, YAML per env, secrets via env, hardcoded prod guard)
- [x] Structured logging, metrics, kill switch
- [x] Money core (centi-cents), clock, reason codes
- [x] Auth (RSA-PSS request signing, doc-verified)
- [x] REST client + WS manager
- [x] Demo smoke test (integration-marked; needs credentials to run)
- [x] Doc-independent math built early: devig (external-only), normalize,
      copula, Monte Carlo engine — all convention-independent by design

**Phase 1 — market data: COMPLETE.** **Phase 2 — observe mode: COMPLETE**
(code; live demo run pending credentials). **Phase 2.5 — harness BUILT,
execution blocked on demo credentials (two accounts).** Conventions are
DOC_ASSUMED/unverified until the fixture is recorded and promoted.

Next: **Phase 3 — pricing** (convention-dependent parts coded against the
`Conventions` interface; UNKNOWN⇒no-quote throughout).

## Architecture decisions

| # | Decision | Why |
|---|----------|-----|
| 1 | `uv` + Python 3.12+, `src/` layout, hatchling | prompt-mandated stack; src layout keeps imports honest |
| 2 | `aiohttp` for both REST and WS | one connection pool, mature async, first-class WS with heartbeat control |
| 3 | Money = `int` centi-cents (1/100 cent; $1 = 10_000 cc) end-to-end; `decimal.Decimal` only at the wire boundary for parsing/formatting Kalshi fixed-point strings | exactness; conversion isolated in `core/money.py` |
| 4 | Added `core/` package (money, clock, reason codes, ids) beyond the prompt's layout | cross-cutting primitives needed by every layer; keeps prompt's other dirs intact |
| 5 | Injectable `Clock` (wall + monotonic) everywhere | staleness logic and the 3s confirm window must be testable deterministically |
| 6 | SQLite via aiosqlite behind a repository interface | prompt-mandated; Postgres drop-in later |
| 7 | Official Python SDK **rejected**; hand-rolled thin aiohttp client for REST + WS | SDK evaluated per Step 0 (docs/api-notes/python-sdk.md): zero WebSocket support — the communications WS channel is the latency-critical path — plus weekly regenerated releases and a >=3.13 pin; REST client is ~60 lines with full control of signing and money types |
| 8 | **Devig scoping (operator directive 2026-07-05):** devig methods apply ONLY inside external `OddsSource` adapters (sportsbook-style odds with embedded margin). Kalshi-sourced leg probabilities NEVER pass through devig — Kalshi binaries are vig-free by construction (yes+no=$1); fees are handled separately in the fee module. Enforced by an import-guard architecture test. The underlying normalization math lives in `pricing/normalize.py` and is reused for exactly one Kalshi-side purpose: renormalizing a mutually-exclusive family of Kalshi markets whose mids don't sum to 100% (`normalize_exclusive_family`). `pricing/devig.py` wraps `normalize.py` for the external-odds case and may be imported only from `pricing/sources/` adapters | operator instruction; keeps margin-model assumptions out of the Kalshi path |

## Open questions

- Exact RSA signing string + padding (docs sweep verifying; implement then).
- Whether maker can explicitly decline after accept, or only lapse the confirm window.
- Whether the maker ever needs `CreateMarketInMultivariateEventCollection` or the
  RFQ flow auto-creates combined markets.
- Fee treatment of RFQ executions on combo markets (maker/taker side).
- Demo support/liquidity for combo RFQs; second demo account for round-trip tests.

## Phase plan

0 scaffold → 1 market data → 2 observe → **2.5 ground-truth conventions** →
3 pricing → 4 risk+MC → 5 demo quote e2e → 6 shadow/paper on prod data →
7 prod tiny → 8 optional bottom-up. Each phase ends with passing tests and a
demo to the human, plus the assumption-audit table appended to `NOTES.md`
(revised 2026-07-05: Phase 2.5 inserted).

**Phase 2.5 — ground-truth conventions (NEW, blocks Phases 3/4 sign
conventions).** Real RFQ round trips on demo (two accounts or
account+subaccount): create RFQ, quote it, accept each side, confirm, let one
lapse; record what the exchange actually did — who ended up long what, exact
fee charged, position signs, settlement values, resulting balance deltas —
into `tests/fixtures/ground_truth/`. Write `core/conventions.py` against that
fixture. Convention-dependent code in Phases 3/4 (quote construction signs,
fee side attribution, exposure signs, EV ledger signs) imports only from
conventions.py. **Needs demo credentials from the human — earlier than the
original "ask before Phase 5" plan.** Convention-independent math (copula, MC,
devig, filters, market data) is not blocked.

## Later-phase notes

- Hedging converts outright event risk into correlation risk — the residual P&L
  *is* the correlation position; that's the actual book a combo maker runs.
