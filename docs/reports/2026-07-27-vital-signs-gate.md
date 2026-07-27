# 2026-07-27 — THE VITAL-SIGNS GATE: an executable harness for the money-critical properties

**Blast radius: ZERO on the live path.** Everything added lives under `tools/vitals/`
plus one paragraph in `CLAUDE.md`. No file under `src/combomaker/**` was read-modified
or written. No orders were placed. The store is opened READ-ONLY. The live bot was not
signalled. (A concurrent workflow was editing `src/` throughout; this build touched
none of it.)

---

## Why

> *"I feel like we're regressing other things as we update other things... Updates
> should improve the bot, not worsen something else."* — operator, 2026-07-27

Seven regressions from ~eight changes in 48 hours, with **3,081 unit tests GREEN
throughout**. Reintroducing all seven into a clone of HEAD trips **17 test instances,
and all 17 were authored by the commit that fixed that same regression** — zero
pre-existing detectors, and two of the seven (R5, R7) are invisible to the suite even
today. The suite is 2,789 test functions with a **9-line median and 2.48 asserts**:
90% drive ≤1 domain item, 0.9% drive a fan-out ≥12, 0.5% create a second task, and
**0.42% assert on measured wall time — none of them on the confirm path**.

The suite asserts *the mechanism the author was thinking about*. It never varies **the
discriminating variable**:

| regression | the variable the suite never varied |
|---|---|
| R1 entity cap | the book **already over** the wall |
| R2 breaker rebuild | **fan-out N** (it fired one market at a time) |
| R3 retry driver | **elapsed wall time** under a persistent 429 |
| R4 accepted-quote guard | the **interleaving point** (an accept inside the await) |
| R5 location/tail axis split | **which path** the benchmark ran on |
| R6 auto-relighter | **real OS processes** (every fake-child test passed) |
| R7 supervisor demotion | the **union** of kill axes, not the surviving axis |

So the gate is not "more tests". It is one command whose every check constructs
**degraded state** and grades a **measured number** against a **derived bound**.

---

## Running it

```
.venv/Scripts/python.exe -m tools.vitals.gate                     # fast tier — every commit
.venv/Scripts/python.exe -m tools.vitals.gate --tier pre-ship     # before arming / shipping
.venv/Scripts/python.exe -m tools.vitals.gate --only V1,V5        # one or more vitals
.venv/Scripts/python.exe -m tools.vitals.gate --verbose           # leave the bot's own logging on
.venv/Scripts/python.exe -m tools.vitals.gate --refresh-tape      # rescan data/live_*.log (~63 s)
.venv/Scripts/python.exe -m tools.vitals.prove                    # the regression corpus (below)
```

Exit code 0 = all green. `CLAUDE.md` hard rule 9 requires the **fast tier 8/8 GREEN
before any commit** touching pricing, risk, or the quote/confirm path.

| tier | vitals | MEASURED runtime | when |
|---|---|---|---|
| **fast** | V1 V2 V3 V4 V5 V7 V8 V9 | **13.5 s** | every commit |
| **pre-ship** | V6 | **32.5 s** | before arming; before any change to `compute_book_risk` / the candidate gate / `build_book_model` |
| **prove** | 6 mutations + clean re-run | **72.8 s** | after changing the gate itself |

Reference: the whole unit suite is 212 s / 3,192 tests. The gate is 6% of that.

---

## The output on the current tree

```
======================================================================================================================
  VITAL SIGNS GATE — kalshi-combos-TWO      tier: fast
======================================================================================================================
  DERIVED INPUTS (measured / observed / protocol — no tuned constants)
    tape           : 41 logs, 10.04 GB (cache (tape unchanged))
    observed tier  : advanced — write 600 cap / 300 refill per s   [GET /account/limits, recorded 2026-07-27T14:23:44Z]
    fan-out N      : 11 markets quarantined in one tick   [max ever, live_20260726_1606.log|2026-07-26T20:12:25]
    boots measured : 35 boots, launch->first beat 3.2s - 48.3s
    live book      : n_positions max 71 (last 46)
    bankroll       : 2,050.41 USD   [combomaker-prod-live-wc.sqlite3:daily_ruin_anchors[2026-07-16]]
    operator knobs : prod-live-wc.local.yaml — heartbeat_timeout_s=30.0, max_open_quotes=200,
                     entity_loss_frac=0.03, write_budget=200tok/10.0s
----------------------------------------------------------------------------------------------------------------------
  KEY  VITAL                                                MEASURED                           VERDICT    SEC
----------------------------------------------------------------------------------------------------------------------
  V1   CAP SCOPE — a wall refuses only the flow that WORSENS disjoint ADMITTED / touching REFUSED  PASS   0.00
  V2   QUOTING LIVENESS — handle_rfq still yields a quote   (a) quoted=True  (b) recovered=True   PASS   0.20
  V3   LOOP LIVENESS UNDER FAN-OUT                          worst loop age 0.06s                  PASS   1.12
  V4   OBSERVED-TIER RATE BUDGET                            peak 28 tok/1s, 696 tok, 12 reads     PASS   1.31
  V5   FUNNEL CONSERVATION                                  proven_gone ∩ confirm.sent = EMPTY    PASS   0.04
  V7   METRIC TRUTH                                         MC-ran=True, taxonomy-honest=True     PASS   0.05
  V8   SUPERVISION COVERAGE                                 0 None-holes over 24 instants         PASS   0.52
  V9   PROCESS HYGIENE                                      0 boots abandoned, 0 orphan returns   PASS  10.26
----------------------------------------------------------------------------------------------------------------------
  8/8 vital signs GREEN   (GATE PASS)   total 13.5s
======================================================================================================================
```

Pre-ship tier on the same tree is **RED — see the live finding at the bottom of this report.**

---

## The nine vitals, each tied to its incident

| # | vital | incident it exists because of | degraded state it constructs | derived bound |
|---|---|---|---|---|
| **V1** | CAP SCOPE — a wall refuses only the flow that WORSENS it | **R1** entity cap scanned every key in the book, not the candidate's (live 2026-07-26: 3,994 breaches, ZERO quotes) | a book the **live predicate** confirms is over the wall, plus a candidate **disjoint** from every over-wall key — and a second candidate that touches one | `entity_loss_frac × risk bankroll`, both read (0.03 × $2,050.41 = **$61.51**). Must admit disjoint **AND** refuse touching, in one assertion |
| **V2** | QUOTING LIVENESS — `handle_rfq` still yields a priced quote | **R1** + **R3** — two mechanisms, same economic outcome | (a) over-wall book, unrelated RFQ; (b) every `max_open_quotes` slot withdraw-pending under a permanently-429ing exchange, then the read-prover resolves | a quote with a **positive bid** must leave `handle_rfq` in **both** states. A transient 429 may never become permanent |
| **V3** | LOOP LIVENESS UNDER FAN-OUT | **R2** quarantine enforcement on the loop that also beat the heartbeat — supervisor emergency-cancelled 27 quotes on a bot that priced 559 RFQs in the "wedged" 30 s | **N markets quarantined in ONE tick**, every DELETE slow *and* 404ing | `supervisor.heartbeat_timeout_s + the loop's registered cadence` = 30.0 + 0.5 = **30.5 s**. **N = 11**, the largest same-tick wave ever recorded (`live_20260726_1606.log` @ 20:12:25) |
| **V4** | OBSERVED-TIER RATE BUDGET | **R3** unmetered retry: 400 write tok/s against a 300 tok/s ceiling | `max_open_quotes` (200) withdraw-pending, every DELETE 429s forever, exchange lists all as still resting | the **OBSERVED** bucket from `GET /account/limits` as the bot recorded it (`advanced`, 600 cap / 300 refill), clamped with the operator knob. Denominated in the exchange's own tokens — never a requests/s literal |
| **V5** | FUNNEL CONSERVATION — a quote never leaves by two exits | **R4** the `state.accepted` guard was a snapshot taken **before** the resolver's awaits (live: `proven_gone=2` on a quote that then logged `confirm_ok`) | an accept scheduled to land **inside** `_read_open_quote_ids`, on another task, with the confirm REST call held open | none — a **0/1 invariant**: `proven_gone ∩ confirm.sent = ∅`. That is the lesson: the assertion needs no number, only the right interleaving |
| **V6** *(pre-ship)* | CONFIRM WINDOW — the gate fits inside the exchange's window | **R5** the location/tail axis split was benchmarked against the **quote** path; the **confirm** path has a hard 3 s window and was never benchmarked | the **REAL open book** read from `position_ledger` (100 positions, 211 tickers), at 1× / 3× / 5×, resolved through the **shipped** rho provider so the two joints genuinely differ | `EXCHANGE_CONFIRM_WINDOW_S` (3.0 s, a **protocol fact** from `docs/api-notes/communications-ws.md:261`) minus the measured deterministic-check cost |
| **V7** | METRIC TRUTH — the MC runs when there is budget, and a timeout is never a risk verdict | **R5**'s disguise (24–70 timeouts filed as `decline_candidate_risk`, invisible to every decline analysis) **and R5's cure's own regression** (the predictive guard skipping to the fallback before the MC ever ran, so a candidate the risk gate would decline gets confirmed) | (a) full budget; (b) the O(T²) input build burns the whole window | (a) `pool.calls > 0` and a risk DECLINE must stand; (b) `confirm.declined.decline_candidate_risk == 0` on any latency exit, and *some* latency-attributed counter must move — no unattributed exit |
| **V8** | SUPERVISION COVERAGE — the **union** of kill axes has no None-hole | **R7** (caught pre-live) demoting liveness left the startup window uncovered, because `ProgressReader` deliberately does not latch on an empty ledger — and that window **spans the startup reconcile** | the exact startup window: empty unlatched `loop_progress.json`, absent heartbeat, bot dead from t=0; then healthy; then established-and-stalled | swept across `[0, max measured boot] = [0, 48.3 s]`. Zero None-holes anywhere the bot is dead **and** zero kills on a healthy bot |
| **V9** | PROCESS HYGIENE — no orphan, no false boot-wedge | **R6** (caught pre-live) a boot-wedge branch returned without `terminate()`, and the 30 s wedge anchor was reused as a **boot** budget | every recorded boot's launch→first-beat delay × 3 launch shapes (START_BOT deletes the heartbeat; a relight inherits a warm corpse; and a stale one) | **not a number — a latch.** 35 recorded boots, 3.2 s–48.3 s; **27 of 35 are outside the 30 s anchor**. Zero abandonments required, plus an `ast` invariant that no `return` past the spawn is un-preceded by `_watch`/`terminate` |

### What is deliberately NOT in this tier

`PRICING ORDER` (a concentration-lowering candidate must be quoted strictly tighter)
is a real economics property with its own tests (`test_concentration_steer.py`), but
none of the seven was a pricing-order defect. Putting it here dilutes a tier whose
entire value is that **red means "we are losing money right now."**

---

## Where every threshold comes from (there is no tuned constant in `tools/vitals/`)

`tools/vitals/derive.py` is the only place a number enters, and each has a provenance
string printed in the header of every run:

| input | source | value today |
|---|---|---|
| write/read bucket | `api_tier_observed` on the tape = the bot's own reply from `GET /account/limits` | `advanced`, write 600 cap / 300 refill per s (recorded 2026-07-27T14:23:44Z) |
| fan-out N | max same-tick `market_quarantined` count over all 41 logs | **11** (the 07-26 20:12:25 incident wave) |
| boot distribution | `quote_app_starting` → first `*book_risk_snapshot` (the bot's first `heartbeat.beat()` runs immediately after it) | 35 boots, **3.19 s – 48.33 s** |
| live book | `book_risk_snapshot.n_positions`, and the real legs out of `position_ledger` READ-ONLY | max 71 on the tape; 100 open positions / 211 tickers in the store |
| bankroll | `daily_ruin_anchors.equity_cc`, latest, READ-ONLY | $2,050.41 (2026-07-16 anchor) |
| operator knobs | the live YAML **parsed by the shipped pydantic model**, so an unset key resolves to the live code default | `heartbeat_timeout_s=30.0`, `max_open_quotes=200`, `entity_loss_frac=0.03`, `write_budget=200 tok/10 s` |
| confirm window | `EXCHANGE_CONFIRM_WINDOW_S` imported from `rfq/lifecycle.py` | 3.0 s (protocol fact) |
| token costs | `DELETE_QUOTE_TOKEN_COST` imported from `exchange/rest.py` | 2 |
| MC size | read off the **live signature** of `evaluate_candidate_book_risk` via `inspect` | `n_samples=20_000` |
| gate attempts | max `attempt` field seen on any `candidate_gate_*` tape line, +1 | 2 |

The 10 GB tape scan takes **63 s**; it is cached in `tools/vitals/tape_facts.json` keyed
by a manifest of every log's (size, mtime), so a normal run pays nothing. The cache is a
committed artifact with its own provenance, so the gate also runs on a machine without
the tape.

---

## PROOF BY CONSTRUCTION — the gate vs the defects it was built for

`tools/vitals/prove.py` copies the working tree into a scratch dir under the system
temp, applies **one surgical mutation** that recreates a historical defect, and runs the
gate there. The working tree is never touched; the scratch copy reads the real `data/`
READ-ONLY via `VITALS_DATA_DIR`.

```
  MUT   EXPECT RED   GATE SAID                          SEC      CAUGHT
--------------------------------------------------------------------------------------------------------
  R1    V1,V2        V1=FAIL, V2=FAIL                   8.4s     YES
  R3    V4           V4=FAIL                            9.9s     YES
  R4    V5           V5=FAIL                            8.4s     YES
  R5    V7           V7=FAIL                            8.3s     YES
  R6    V9           V9=FAIL                            10.3s    YES
  R7    V8           V8=FAIL                            8.1s     YES
--------------------------------------------------------------------------------------------------------
  CLEAN working tree, same vitals: V1=PASS, V2=PASS, V4=PASS, V5=PASS, V7=PASS, V8=PASS, V9=PASS -> ALL GREEN
  6/6 historical defects caught; total 72.8s
```

The mutations, verbatim:

| mut | file | edit | the gate's message |
|---|---|---|---|
| **R1** | `risk/limits.py` | `candidate_keys = set(snapshot.loss_by_entity_cc)` (the first cut's book-wide scan) | `MEASURED: disjoint REFUSED / touching REFUSED` → *"a book-WIDE scan declines unrelated flow (R1 exactly)"* + `V2: (a) an over-wall book on an UNRELATED entity bricked quoting` |
| **R3** | `rfq/lifecycle.py` | `_spend_withdraw_tokens` returns `True` immediately | the emitted token rate leaves the observed bucket |
| **R4** | `rfq/lifecycle.py` | delete the post-await `live.accepted` re-check | *"a mid-confirm quote was REAPED as a proven withdrawal"* |
| **R5** | `rfq/lifecycle.py` | `candidate_gate_timeout_fallback = False` (the pre-07-27 behaviour) | *"a LATENCY exit was recorded as `decline_candidate_risk` — a timeout wearing a risk reason code"* |
| **R6** | `ops/relight.py` | drop the `beat_seen` latch, so the bound runs from launch | healthy boots abandoned across the measured distribution |
| **R7** | `ops/supervisor.py` | remove the startup-window liveness fallback | None-holes across the startup sweep |

**R2 is covered by V3 but is not in the mutation corpus** — reintroducing it means
reverting a whole architectural split (`da29585`), not a line, so the audit's own method
there was "run the suite at the incident commit" (result: 2,928 passed / 0 failed / 0
detectors). V3 pins the property at the measured N instead.

---

## LIVE FINDING — the pre-ship tier is RED right now, and it is not a false alarm

```
  V6   CONFIRM WINDOW    fits 1x; misses 3x,5x (cold misses 1x,3x,5x)      FAIL   32.5s
       bound: < 3000ms exchange window − 2.2ms measured det-check = 2998ms

  1x  n_pos= 100  tickers= 211  pairs= 22,155  build(warm)   7.5ms + MC  596.0ms x2 =  1,200ms =  40.0%  FITS
      [COLD (first accept of a process): build  3,895ms @ 0.176ms/pair ->  5,087ms, DOES NOT FIT]
  3x  n_pos= 300  tickers= 633  pairs=200,028  build(warm)  44.2ms + MC 2660.8ms x2 =  5,366ms = 178.9%  DOES NOT FIT
      [COLD: build 34,548ms @ 0.173ms/pair -> 39,870ms, DOES NOT FIT]
  5x  n_pos= 500  tickers=1055  pairs=555,985  build(warm) 193.8ms + MC 5052.4ms x2 = 10,299ms = 343.3%  DOES NOT FIT
      [COLD: build 65,258ms @ 0.117ms/pair -> 75,362ms, DOES NOT FIT]

  live cost on the tape: 70 candidate_gate_deadline events vs 267 confirms
```

Read that carefully, because it is the first time this has been measured against the
right path:

1. **On the REAL 100-position book, memo-warm, the candidate gate consumes 1.20 s —
   40% of the exchange's entire 3 s window** — before last look, the fill-velocity
   check, the reservation, and the confirm round trip. It fits, but there is far less
   headroom than anyone has been assuming.
2. **COLD — the first accept after every restart — the O(T²) rho resolution alone is
   3.9 s at the live book size, 130% of the window.** The memo (`test_within_game_rho_memo.py`)
   is what makes steady state survivable; the first accept of every process is not
   covered by it. That is a real, recurring, per-restart loss of the risk refinement.
3. **At 3× and 5× the live book the gate cannot run at all** (179% / 343% of the window).
   `max_open_quotes` is 200 and the book has already carried 71 positions on the tape;
   3× is not hypothetical.
4. This corroborates the tape exactly: **70 `candidate_gate_deadline` events against 267
   confirms**.

Post-fix this no longer *discards* the auction — the timeout fallback confirms from the
deterministic caps (V7 proves the taxonomy is honest) — so it is no longer a direct cash
loss. What it means instead is that **the candidate MC, the gate that exists to refuse
concentration, effectively does not run on a real book**, and every confirm is decided
by the deterministic caps alone. That is a risk-quality finding, not a latency curiosity.

The gate FAILS on it deliberately. The audit's own warning applies: *"G2 is worthless as
a warning — R5 shipped past a benchmark that measured the wrong path."*

---

## Isolation, and what this does NOT touch

| property | how it is guaranteed |
|---|---|
| never edits a live module | everything is under `tools/vitals/`; the checks **import and drive** `LimitChecker`, `QuoteLifecycle`, `SafetySupervisor`, `Relighter`, `evaluate_candidate_book_risk`, and the shipped rho provider (CLAUDE.md rule 8) |
| never needs the exchange | the gate strips every credential env var and sets `COMBOMAKER_NO_DOTENV=1` at import, exactly as `tests/conftest.py` does for the suite |
| never needs a running bot | all state is constructed; the tape and store are historical |
| store is READ-ONLY | `sqlite3.connect("file:...?mode=ro", uri=True)` for both the bankroll anchor and the open book |
| the unit suite is untouched | the gate is its own tier; it reuses test *harnesses* (`tests.test_lifecycle.Rig`, `tests.test_withdraw_resolution._rig`, `tests.test_liveness_progress._rig_with_quotes`, `tests.test_auto_relight._StepClock`) rather than duplicating them, and modifies none of them |
| throughput never regresses | the gate adds nothing to the hot path — it is a developer tool, not a runtime component |

---

## Files

| path | what |
|---|---|
| `tools/vitals/gate.py` | the runner, the table, the CLI, the hermetic env |
| `tools/vitals/derive.py` | every derived input + the cached 10 GB tape scan |
| `tools/vitals/result.py` | `Vital` / `Result` / `Probe` — measured number, derived bound, money impact |
| `tools/vitals/v_quoting.py` | V1, V2 |
| `tools/vitals/v_liveness.py` | V3, V4 |
| `tools/vitals/v_confirm.py` | V5, V6, V7 |
| `tools/vitals/v_supervision.py` | V8, V9 |
| `tools/vitals/prove.py` | the regression corpus + mutation harness |
| `tools/vitals/tape_facts.json` | the cached, provenance-carrying tape derivation |
| `CLAUDE.md` hard rule 9 | the requirement |

---

## NEXT STEPS

1. **Owner: next build — V6 is the top item.** The candidate MC does not fit the
   confirm window on the real book cold, and does not fit warm at 3×. Two candidate
   repairs, both structural rather than tuned: (a) **warm the rho memo at startup**
   (the pairs are a pure function of the ticker set and `SgpParams` — resolving the
   book's pairs during the startup reconcile makes every first accept warm, and costs
   nothing on the hot path); (b) **make the candidate MC scale with the book** — 20,000
   samples over a 500-position merged book is 5 s, and the gate's own predictor already
   knows the per-unit rate. Re-run `--tier pre-ship` after either.
2. **Owner: this workflow / the next one.** Wire the fast tier into whatever runs before
   a commit. It is one command and 13.5 s.
3. **Owner: next build.** Add V10 = END-TO-END LEDGER IDENTITY (RFQ → quote → accept →
   confirm → fill booked → exposure reconciles **to the cent**). 32 unit tests touch
   that chain and **zero** assert the cent-level identity the live reconcile enforces.
   It is the one property from the audit's list that this gate does not yet cover.
4. **Owner: next build.** Add the **unpinned-fix detector** (G7): revert-and-run over the
   exact lines each fix commit touched; a fix whose reversion leaves everything green
   fails. `prove.py` is already 80% of that harness — it needs the mutation list to be
   generated from `git log` rather than hand-listed.
5. **Decisions owed by the operator:**
   - **Does a RED pre-ship tier BLOCK a ship, or only report?** Recommendation: BLOCK.
     R5 shipped past a benchmark that measured the wrong path precisely because the
     signal was advisory.
   - **The bankroll anchor is stale** (`daily_ruin_anchors` last written 2026-07-16,
     $2,050.41, while the 7/25 reconcile put equity at $2,179.74). The gate reports the
     provenance honestly, but the anchor writer should be re-armed — a %-of-bankroll
     cap graded against a 11-day-old denominator is a slow drift in every wall.
   - **V3's fan-out N is 11 today.** It is re-derived from the tape on every
     `--refresh-tape`, so it grows with the slate automatically. Confirm that is the
     desired behaviour (it is the auto-scaling shape, but it means the bound moves
     without a human).
