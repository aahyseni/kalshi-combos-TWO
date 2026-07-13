# Risk PHASE 6 — external watchdog + circuit breakers + prod go-live gates

**Date:** 2026-07-13
**Branch:** `risk-phase6` (based on `main@a076b8d`)
**Suite:** **1576 passed / 0 failed** (+61 vs the 1515 at base), 3 integration
deselected. `uv run mypy src` clean on every touched file (the only 2 residual
errors are pre-existing in `pricing/ising_amm.py`, untouched by this phase and
present on the base commit). `uv run ruff check` clean on all touched files.
**Status:** everything ships **NOT-LIVE** — the go-live gates keep prod OFF; the
supervisor + breakers are testable in shadow. This is the FINAL phase of the
risk-engine build.

The last line of defense: an out-of-process safety supervisor that can kill the
bot even when the bot's own host is wedged (crash / deadlock / partition), the
full circuit-breaker set that trips the kill switch on the known failure
signatures, and the prod go-live gates so the system can run unattended and be
killed externally.

---

## 1. What shipped

```
┌──────────────────────────────────────────────────────────────────────────┐
│  BOT PROCESS (quote_app)                     SUPERVISOR PROCESS (separate  │
│                                               host + credential)           │
│  maintenance tick ─┬─ maintenance_tick()                                   │
│                    └─ heartbeat.beat()  ──►  heartbeat.txt  ──►  reads age  │
│                                              (wall time)         vs clock   │
│  status loop ─── breakers.evaluate_and_halt(sample)                        │
│                    │  rx-age / seq-gap / latency / 429 / jump / meta / key │
│                    └─ trip ⇒ KillSwitch.halt(reason) ⇒ cancel-all + stop   │
│                                                                            │
│  startup ─── block_restart_until_reconciled()             wedged? ─┐       │
│              (needs_reconcile marker + exchange reconcile)         │       │
│           └─ prod preflight (all gates green?)                     ▼       │
│                                              KILL file ◄── emergency_cancel │
│  restart ── reads KILL + needs_reconcile ◄── needs_reconcile ◄──── _all()  │
│             (halts immediately, refuses to quote)         (own credential, │
│                                                            reserved budget)│
└──────────────────────────────────────────────────────────────────────────┘
```

| Component | File | What it does |
|---|---|---|
| Heartbeat + reconcile marker | `risk/heartbeat.py` | atomic wall-time beat the bot writes each tick + the `needs_reconcile` on-disk gate; both survive restart; every parse failure fails CLOSED (unreadable beat ⇒ infinitely old; corrupt marker ⇒ present) |
| Circuit breakers | `risk/breakers.py` | 7 pure fail-closed detectors + a `CircuitBreakers` coordinator that trips the kill switch on the first trip; a detector that RAISES ⇒ `HALT_BREAKER_ERROR` |
| External supervisor | `ops/supervisor.py` | standalone `python -m combomaker.ops.supervisor`; heartbeat watch, emergency cancel-all via its OWN credential, reserved write budget, credential-rotate seam, fail-closed KILL-always |
| Preflight | `ops/preflight.py` | pure runtime go-live gate evaluator (all conditions green before the first quote) |
| Prod guard extension | `ops/config.py` | the WHITELIST go-live gate added to `assert_safe_to_run`; new `SupervisorConfig` / `BreakerConfig` / `SafetyConfig` gates |
| Wiring | `ops/quote_app.py` | heartbeat beat in the maintenance loop, breaker eval in the status loop, 429 recording, block-restart-until-reconciled + prod preflight at startup |
| Reason codes | `core/reasons.py` | 8 new HALT codes (grouped + named) |

---

## 2. External supervisor design

The supervisor is a **separate process** (deployment: separate host + a distinct
credential; the CODE is a standalone process with its own REST client). Pillars:

- **HEARTBEAT.** The bot writes `data/heartbeat.txt` every maintenance tick (the
  FIRST thing the tick does, so a slow/failed `maintenance_tick` still ages the
  beat — exactly the wedged signal). The supervisor reads its age against its own
  clock. Wall time is the only clock two separate processes share; a beat
  implausibly in the future (clock jump / tamper) reads as stale (fail-closed).
- **EMERGENCY CANCEL-ALL.** On a missed heartbeat (or an explicit trigger) the
  supervisor lists our resting quotes via its OWN credential and cancels each,
  THEN writes the KILL file + drops the `needs_reconcile` marker. **FAIL-CLOSED:
  if the exchange is unreachable it STILL writes KILL + the marker + alarms** — a
  supervisor that can't cancel must at least stop the bot from resuming.
- **RESERVED API WRITE BUDGET.** A token bucket (`WriteBudget`, default 200
  tokens / 10 s) throttles the supervisor's OWN writes so it can always act under
  a 429 storm on the shared/bot budget — it never draws from the bot's pool. If
  the reserved budget is exhausted mid-cancel it alarms loudly and STILL writes
  KILL (tested with a 100-quote book and a 5-token budget: exactly 5 cancelled,
  `budget_exhausted=True`, KILL written).
- **CREDENTIAL ROTATE.** A distinct env-only credential
  (`KALSHI_SUPERVISOR_API_KEY_ID` + PEM/path) so a compromised/rate-limited BOT
  credential can't disable the kill path. **Fail-closed default:** absent the
  dedicated credential the supervisor runs KILL-only (the credential-free half of
  the kill path) and reports `has_kill_credential=False`. Secrets are env-only,
  never logged.
- **BLOCK-RESTART-UNTIL-RECONCILED.** Writing KILL + the `needs_reconcile` marker
  is what enforces it on the bot side: a restarted bot reads the marker at startup
  and refuses to quote until it reconciles its book against the exchange
  (exchange-first, reusing the Phase-3 `reservation.reconcile` pass). The marker
  clears ONLY after a successful reconcile; if the exchange is unreachable the
  marker stays and the bot refuses to quote (`_book_reconciled=False` ⇒ preflight
  red).

### Kill-drill result

`test_run_loop_kills_dying_bot`: a bot beats once then "dies"; the supervisor's
`run()` loop (driven-clock) detects the stale heartbeat within two poll cycles,
**cancels every resting quote (`q1`, `q2`), writes the KILL file, and drops the
`needs_reconcile` marker.** A restarted bot then reads KILL (halts immediately)
+ the marker (refuses to quote until reconciled). The whole drill is deterministic
— fake clock, fake exchange, no network.

---

## 3. Circuit-breaker table (thresholds + reason codes)

Each breaker is a **pure detector** (`(input) -> BreakerVerdict`) run by the
coordinator in the status loop. ALL fail-closed: a detector that cannot evaluate
its input TRIPS; a detector that RAISES ⇒ `HALT_BREAKER_ERROR`. Threshold contract
is tested at-and-just-over.

| Breaker | Signature | Threshold (default) | Reason code | Fail-closed behavior |
|---|---|---|---|---|
| Data staleness / seq-gap | feed rx-age over limit, or WS sequence gap | `> 5.0s` rx-age; any seq gap | `HALT_DATA_STALE` | `None` rx-age (freshness unprovable) ⇒ TRIP |
| Latency spike | confirm/round-trip ms over limit | `> 2000ms` | `HALT_LATENCY_SPIKE` | `None` = no round-trip measured yet ⇒ clear (a spike needs a sample; staleness catches a dead link) |
| 429 burst | rate-limit responses in a rolling window | `>= 10` in `10s` | `HALT_RATE_LIMIT_BURST` | count at-or-over ⇒ TRIP |
| Marginal jump | a leg marginal moved between ticks | `> 0.25` prob | `HALT_MARGINAL_JUMP` | had-baseline-now-unreadable ⇒ TRIP; no baseline yet ⇒ clear |
| Rule/metadata change | taxonomy tripwire hit OR settlement-relevant market metadata changed | any hit / any changed market | `HALT_METADATA_CHANGE` | reuses `pricing/tripwire.py`; a fed hit escalates classifier-decline to a halt |
| Unmapped game key | a leg whose `game_key` can't resolve reaches the risk path | `None`/empty key | `HALT_UNMAPPED_GAME` | unmapped ⇒ TRIP (would escape every game/slate cluster cap) |
| Breaker error | a detector itself raised | any exception | `HALT_BREAKER_ERROR` | a breaker that can't run can't protect ⇒ TRIP |

Ordering is cheap → structural; the first trip wins (deterministic halt reason).
A trip calls `KillSwitch.halt(reason, detail)` which fires the existing
cancel-all + intake-stop callbacks (`quote_app.on_halt`).

**Wiring seams.** The status loop samples `rx_age_s` / `feed_healthy` from the
feed, the worst confirm round-trip from the metrics histogram, and the rolling
429 count from a `RateLimitWindow` fed by the REST error paths in the polling
loops. The marginal-jump and unmapped-game inputs accept a per-tick map — populated
today for the breakers already able to observe them (feed/latency/429); the
marginal/game-key/metadata seams are wired-and-tested detectors sampled from the
risk path as that path exposes the values (the detectors are proven; the live
sampler is minimal and additive so a future caller feeds the richer snapshot
without touching the breaker logic).

---

## 4. Prod go-live gates + preflight

Everything defaults OFF; prod stays not-live. Two layers:

**STATIC** (`AppConfig.assert_safe_to_run`, raised at construction):

1. `--confirm-live` (CLI only, never YAML) — unchanged.
2. `safety.prod_limits_configured: true` — unchanged.
3. **NEW: non-empty leg-series whitelist** (`filters.allowed_leg_series_prefixes`)
   — only whitelisted series quote on prod (no crypto/esports/unmodeled legs, per
   judge finding F1). An empty list OR a null (both disable the per-leg gate) is
   REFUSED. Operator override: `safety.prod_require_series_whitelist: false`.

**RUNTIME PREFLIGHT** (`ops/preflight.py`, checked at startup before the first
quote; a no-op on demo). ALL must be green or the bot raises `PreflightError` and
refuses to quote (CLI exit 3):

| Gate | Source | Red when |
|---|---|---|
| limits_configured | `safety.prod_limits_configured` | false |
| whitelist_non_empty | `filters.allowed_leg_series_prefixes` | empty/null |
| supervisor_heartbeat_established | the bot's first beat written | file absent |
| external_kill_reachable | dedicated supervisor credential present | credential absent |
| book_reconciled | startup exchange-first reconcile succeeded + marker clear | reconcile failed / marker present |

Fail-closed: every `PreflightConditions` field defaults to the not-green value, so
an unset condition is red. `require_supervisor` (default on) gates whether the two
supervisor conditions are load-bearing.

---

## 5. Ships NOT-LIVE vs deferred

**Ships NOT-LIVE (testable in shadow, prod OFF):**
- Supervisor process + kill-drill (fake exchange/clock).
- All 7 circuit breakers (pure detectors + coordinator wired into the status loop);
  a trip halts the kill switch exactly as the existing halts do.
- Heartbeat beat + `needs_reconcile` marker (wired into the maintenance loop /
  startup).
- Go-live gates (static whitelist gate + runtime preflight); prod refuses to quote
  with any gate red.

**Deferred (post-Phase-6, on the go-live runway):**
- Wiring the richer breaker snapshot from the live risk path (marginal-jump per-leg
  map, resolved game-key map, market-metadata-change diff) — detectors are proven;
  the live sampler currently feeds feed/latency/429 and the seams for the rest.
- Building the production `KalshiSupervisorExchange` behind a real second demo/prod
  credential and running the kill-drill against the demo exchange (needs the
  dedicated `KALSHI_SUPERVISOR_*` credential from the operator).
- The supervisor as an actual separate deployed host (this phase ships the
  standalone process + CLI; the separate-host deployment is an ops step).

---

## NEXT STEPS

- **Owner: operator (decisions owed).** (1) Provision the dedicated supervisor
  credential (`KALSHI_SUPERVISOR_API_KEY_ID` + PEM/path) — a SEPARATE Kalshi key
  from the bot's, ideally on a separate host, so the kill path can't be disabled
  by a throttled/compromised bot key. (2) Sign off the breaker thresholds
  (rx-age 5s, latency 2000ms, 429 ≥10/10s, marginal jump 0.25) and the supervisor
  heartbeat timeout (15s) / reserved budget (200/10s) — conservative defaults; tune
  from shadow observation, never a P&L window.
- **Owner: engineering.** (1) Wire the richer breaker snapshot (per-leg marginal +
  game-key + metadata-change diff) from the risk path into `_sample_breaker_inputs`.
  (2) Build `KalshiSupervisorExchange` against the dedicated credential and run the
  kill-drill on the demo exchange (a real dying-bot → real cancel-all). (3) Deploy
  the supervisor as a separate host.
- **Go-live runway (per RISK_BUILD_PLAN "how we actually go live"):** SHADOW the
  whole stack (log-only) → tiny live at **$2,000** with the conservative first-live
  caps (per-game 3–5%, per-combo 0.5–1%, slate 6–8%, drawdown 6–8%, hard-trip
  8–10%) → the prod preflight enforces the go-live gates green before the first
  quote and the external supervisor is the unattended kill path → accumulate POOLED
  MULTI-WEEK, GAME-CLUSTERED settlement → re-derive caps AND the markup from that
  data (never one window) → raise limits only on game-clustered live evidence →
  scale bankroll. The markup decision runs on the parallel data-accumulation track.
- **Blocking gate to first-live:** the dedicated supervisor credential + a demo
  kill-drill against the real exchange must pass before any prod quote.
