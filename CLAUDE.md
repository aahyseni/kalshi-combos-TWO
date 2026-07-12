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
7. **Reporting + handoff discipline (operator directive 2026-07-08, extended
   2026-07-09).** Every crucial update, test, or finding gets a dated `.md` in
   `docs/reports/` **as it happens** — one file per test/update, indexed
   newest-first in `docs/reports/README.md`, each ending with a NEXT STEPS footer.
   **AND** keep a live **RESUME STATE** current at all times (latest dated
   `*-session-state-resume.md` + the operator resume memory) so that if the
   terminal closes, the context compacts, or a new agent/model picks this up, it
   knows **exactly what's been done, what's running (with restart commands +
   watermarks), what's in flight, and what to do next** — without re-deriving.
   Update it whenever state changes (a process started, a blocker moved, a
   decision made). This is a standing rule, not a per-request ask.
8. **Testing isolation (operator directive 2026-07-09).** Live pricing/engine
   modules stay **PRISTINE**. Backtest/analysis scripts (`tools/`) may freely
   **import and call** live pricing code + the shipped config (always the real
   thing — never a reimplementation, never agents) but must **NEVER edit a live
   module or add a test-only entry point to one** (e.g. no `fair_from_marginals`
   bolted onto the engine). A change surfaced by a backtest is prototyped in the
   test script (or a config override) **first**, validated there, and only THEN
   ported to the live module — followed immediately by a **parity check**: the
   live output must equal the test-validated output **to the cent on the same
   inputs** before the port is trusted. Exceptions/notes: (a) unit tests under
   `tests/` are exempt — they test live modules directly by design; (b) a pure
   config-value promote (a ρ, a flag) needs only a re-run of the backtest against
   the promoted config, since there's no code to drift; (c) if a test script must
   duplicate any live logic (e.g. the harness copies the engine's ~15-line model
   dispatch because `engine.price()` needs order books a backtest lacks), keep a
   "keep in sync with <live location>" comment and cover it with the parity check.

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

**⚡ CURRENT STATE (2026-07-11 — read this block, then `docs/reports/README.md`
newest-first, then the latest `*-session-state-resume.md`):**

- **Suite 1245/0 at HEAD.** Sell-only parlay-seller book is LIVE-VERIFIED and
  UN-GATED (`combo_no_pays_complement: true` promoted from a real $1.00 demo
  settlement 2026-07-10).
- **MLB props engine COMPLETE + gated**: 9/9 classification, 165-entry measured
  pair table (+rung keys, spread×prop resolver), 149-cell same-player
  conditional table (40 exact), fresh-window backtest GATE PASS (0.34¢ med /
  prop-carrying 0.66 vs legacy 1.37¢; per-print 98.6% w2). Soccer WC:
  per-print 1.57¢ med over 656,555 prints, parity-gated.
- **Containment campaign MERGED** (`d65bb6e`): collapse plans (225 formerly
  UNKNOWN WC combos now price), universal exact containment windows
  (P(B)−P(A)), spread⟹win family (soccer+MLB), TB⟹HRR exact cells,
  spread×total impossibility, taxonomy-impossible TRIPWIRE (fixture-pinned,
  alarms if Kalshi's validator loosens), same-game isolation guard on
  conditional super-legs (killed a judge-found +7.32¢ sign inversion).
  Adversarially judged in two rounds; all verdicts + expected-diff
  reconciliations in `docs/reports/2026-07-11-*`.
- **Operator gates ACTIVE**: pregame-only quoting (Phase 3 — no in-play legs,
  all sports, `filters.allow_inplay_legs` to re-enable) and the LEG-SERIES
  ALLOWLIST (`filters.allowed_leg_series_prefixes = ["KXWC", "KXMLB"]` —
  MLB + World Cup ONLY; unblock = one YAML prefix; doubles as per-sport kill
  switch).
- **Module overviews + zero-bias judge DONE** (`2026-07-11-{soccer,baseball}-
  module-overview.md` + judge verdict; all findings closed or filed).
- **NEXT:** #14 demo fill e2e → #15 weekly settlement/calibration cadence →
  #16 MLB blind test → E decisions (markup from POOLED MULTI-WEEK evidence —
  never refit on a P&L window; per-sport kill = the allowlist; prod gates).
- Phase-numbered history below is context, not state.

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

**Phase 3 — pricing: COMPLETE** (fees w/ fail-safe taker attribution, leg
beliefs + OddsSource interface, relationship classifier w/ UNKNOWN/IMPOSSIBLE
branches, copula joint w/ priced rho-uncertainty, quote construction w/
maker-favorable rounding + free-money caps; 432 tests green).

**Phase 4 — risk engine: COMPLETE** (exposure book w/ mass-acceptance
dominance property, limits, last-look pure function, in-play detector,
markout tracker + fills/markouts/ev_ledger persistence, UNKNOWN-never-quotes
mutation sweep; 525 tests green). MC engine was built in Phase 0 (sim/engine.py).

**Phase 5 prep — hot path wiring: COMPLETE** (QuoteLifecycle: quote → accept →
last look → confirm/lapse → executed → position; TTL/reprice/cancel-all;
QuoteApp paper+quote modes with hard gates — verified conventions + whitelist
+ prod guard; cancel-all + report CLI; hedging scaffold phase-gated off;
543 tests green).

**Final adversarial review: DONE** — 5-lens multi-agent review + 2-skeptic
verification per finding; 7 confirmed defects (1 critical: target-cost risk
sizing) all fixed with regression tests. 556 tests green.

**Phase 2.5: EXECUTED + conventions promoted (operator sign-off).**
**Phase 5 — demo quote mode e2e: COMPLETE 2026-07-05** — 30 live quotes,
full accept→confirm(117ms of 3s)→execute round trip, fill/EV/markout records,
cancel-all on halt (NOTES.md Phase 5 table). Definition-of-done met on demo.

Next: **Phase 6 — shadow on prod data** (read-only; would-quotes + markouts +
executed-trade comparisons + calibration), combo-settlement pass to fill
`combo_no_pays_complement`, Kalshi→SGO mapping table.

**Structural pricer v2 (2026-07-06): SHIPPED, ENABLED** — Dixon-Coles
scoreline model for soccer SGPs (`pricing/dixon_coles.py` math,
`pricing/structural.py` adapter), inverted from live leg prices behind the
JointEstimate interface; any parse or identification doubt falls back to the
v1 copula. Validated against the live market (SPA/POR parlay priced at
exactly our structural fair) and **OOS-gated vs the shipped v1 copula on
8,980 held-out-season club games — structural wins all three joint-log-loss
metrics, by the most on 3-leg triples** (NOTES.md audit I1–I10 + gate table).
dc_ρ = −0.05 fitted on train scorelines. Same day: orientation-aware
btts|moneyline (fav/dog), ml|player_goal 0.50, and the dead-config fix
forwarding sport tables into the engine. Settlement windows RULE-BOOK
verified (operator-provided Kalshi rules): knockout game market = advance
incl pens (Advance spec, pens 0.5 banded), BTTS/totals regulation-only,
props full-game — worth ~1¢ of fair (NOTES I8).

**Margin/total structural pricer — NFL/NBA/WNBA (2026-07-06): BUILT;
NFL ENABLED** (`pricing/margin_total.py` + adapter dispatch in
`pricing/structural.py`): (margin, total) bivariate normal, means inverted
per game from live prices, shapes calibrated on recent seasons (data
refreshed through NFL 2025 / NBA 2025-26 / WNBA 2026-07-05). NFL OOS gate
passed (biggest win: hw×cover — exact comonotone geometry vs copula 0.88).
Spread legs blocked until in-season tickers verify the line sign convention;
NBA/WNBA calibrated but gated off pending an odds source or prod-shadow
settlements (NOTES audit J1–J6). **Frame convention (NOTES L1, fixed
2026-07-06):** config ρ is calibrated as corr(home−away, total), but the leg
specs put `Team.A` = game-code blob prefix = AWAY team, so the adapter builds
its shape via `margin_total.shape_in_leg_frame` (ρ negated) — the single place
the frame lives. This makes the shipped pricer the exact mirror of the
home-frame model the OOS gate validates.

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
