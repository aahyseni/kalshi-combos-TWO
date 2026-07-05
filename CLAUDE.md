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

## Current phase

**Phase 0 — scaffold.** (Step 0 docs sweep running in parallel; results land in
`NOTES.md` + `docs/api-notes/`.)

- [x] Repo, .gitignore-first commit, uv project
- [ ] Config (pydantic, YAML per env, secrets via env)
- [ ] Structured logging
- [ ] Money core (centi-cents)
- [ ] Auth (RSA request signing) — pending doc verification
- [ ] REST/WS client skeleton
- [ ] Demo smoke test (integration-marked)

## Architecture decisions

| # | Decision | Why |
|---|----------|-----|
| 1 | `uv` + Python 3.12+, `src/` layout, hatchling | prompt-mandated stack; src layout keeps imports honest |
| 2 | `aiohttp` for both REST and WS | one connection pool, mature async, first-class WS with heartbeat control |
| 3 | Money = `int` centi-cents (1/100 cent; $1 = 10_000 cc) end-to-end; `decimal.Decimal` only at the wire boundary for parsing/formatting Kalshi fixed-point strings | exactness; conversion isolated in `core/money.py` |
| 4 | Added `core/` package (money, clock, reason codes, ids) beyond the prompt's layout | cross-cutting primitives needed by every layer; keeps prompt's other dirs intact |
| 5 | Injectable `Clock` (wall + monotonic) everywhere | staleness logic and the 3s confirm window must be testable deterministically |
| 6 | SQLite via aiosqlite behind a repository interface | prompt-mandated; Postgres drop-in later |
| 7 | Official Python SDK: **decision pending Step 0** — use it only if it cleanly covers Communications REST + WS; otherwise thin async client from the OpenAPI/AsyncAPI specs | docs sweep evaluating |

## Open questions

- Exact RSA signing string + padding (docs sweep verifying; implement then).
- Whether maker can explicitly decline after accept, or only lapse the confirm window.
- Whether the maker ever needs `CreateMarketInMultivariateEventCollection` or the
  RFQ flow auto-creates combined markets.
- Fee treatment of RFQ executions on combo markets (maker/taker side).
- Demo support/liquidity for combo RFQs; second demo account for round-trip tests.

## Phase plan

0 scaffold → 1 market data → 2 observe → 3 pricing → 4 risk+MC → 5 demo quote e2e
→ 6 shadow/paper on prod data → 7 prod tiny → 8 optional bottom-up. Each phase
ends with passing tests and a demo to the human. **Ask the human before Phase 5**
(creds, second demo account, tier, odds APIs, limit values).

## Later-phase notes

- Hedging converts outright event risk into correlation risk — the residual P&L
  *is* the correlation position; that's the actual book a combo maker runs.
