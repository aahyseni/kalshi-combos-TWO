# 2026-09-05 — BUILD: reader-side RAW PRE-FILTER + WALL-TIME governor tick

Branch `build/raw-prefilter-governor-tick`, worktree `C:/Users/aahys/kct-prefilter`, off main `08f342e`.
The bot was LIVE and FILLING on main throughout (log `live_20260905_2037.log`, fresh store after the rotation);
this build touched no process: store reads `mode=ro` on the ARCHIVE by rowid range only, logs by grep/tail,
every pytest/mypy/bench at LOW priority, vitals from the 2-table snapshot. **Blast radius: the communications
TRANSPORT's reader thread (a per-frame text judgement before `json.loads`) and the governor's CADENCE. The
intake's decision set is provably unchanged; pricing, risk and quote construction read nothing from here.**

## WRONG / FIXED / OPEN

| | Item | Status |
|---|---|---|
| WRONG | Every one of the ~3,000 frames/s on the communications firehose (Saturday night, `ws_inbound_rate`) was `json.loads`-ed on a reader thread, stamped, pushed through the shared lanes, popped, dispatched and handed to the intake — which then discarded **≈ 98.5 %** of the `rfq_created` frames on its own leg-series allowlist (`rfq.dropped_series_fastpath`; the channel is the WHOLE exchange's RFQ stream, SUMMARY.md:28). ≈ **49 % of all frames read** did that full round trip for nothing. | Measured (below). |
| FIXED | **`exchange/ws_prefilter.py` — `RawSeriesPrefilter`**, installed on every shard's READER THREAD before the parse (`ws.py:_read_loop`): the envelope `type` is read structurally from the raw text (the `"type"` key must sit before the first nested `{`, followed by `: "…"` — any other shape is UNIDENTIFIABLE → full parse); an `rfq_created` whose raw text has NO `"market_ticker"` value starting with an allowlisted prefix — the SAME tuple the intake filters on, from the same config object — is counted and dropped: never parsed, never queued, never dispatched. Any allowlisted value ⇒ PASS unchanged; any backslash in the text ⇒ FAIL-OPEN (escapes are the one way raw ≠ decoded); priority/control frames are OTHER by type before any ticker test. Registered via `WsManager.set_raw_prefilter` / `CommsFanout.set_raw_prefilter` (propagates to every shard, inherited on re-shard); `start()` refuses a pre-filter on any type that is not `mark_sheddable`. | Built; proof + property test. |
| FIXED | **Decision neutrality, proven not assumed.** The intake drops when legs are empty or ANY dict leg fails `startswith(P)`; the pre-filter drops only when EVERY `"market_ticker"` value fails `startswith(P)` on backslash-free text (where raw == decoded) — a strict SUBSET. Property test over **600 real frames** (400 from the 9/5 archive tail = today's allowlisted series, 200 real July World-Cup frames = series not on today's allowlist) × 4 serializations (compact / spaced / pretty / sid-first) + all-foreign and one-leg-foreign variants + an escaped variant: the RFQs reaching the intake's fan-out are **identical in content and order** with and without the pre-filter; every DROP satisfies the intake's own rule; verdicts are layout-independent. Through the REAL transport (`WsManager` reader → lanes → dispatcher → `RfqIntake`, 242-frame wire mix): identical `seen`/`deleted`/quote events, `t.prefiltered == foreign`, `t.msg.rfq_created == allowlisted`, `rfq.dropped_series_fastpath` 60 → 0, priority frames untouched, every frame still READ. | 15 new tests. |
| FIXED | **Cost of the judgement — measured, then rebuilt.** The first per-occurrence Python judge cost **5.7 µs** on a real 5-leg frame vs **4.6 µs** for `json.loads` (C): inspecting every ticker in Python is as expensive as the parser — the fan-out report's "~10× cheaper" follow-up estimate was WRONG. Rebuilt as ONE trie-structured regex compiled from the same prefixes (`"market_ticker"[ws]*:[ws]*"` + trie): **1.03 µs** (flat alternation 1.98, head-dict 1.88); whole `judge` **3.8 µs vs 5.8 µs** parse on the bench corpus. The fast path is taken only when every ticker occurrence opens as a string (separator count); otherwise the per-occurrence parser runs and fail-opens on the first unreadable occurrence. | Bench (below). |
| FIXED | **`rfq_deleted` (≈ 49.5 % of the wire) PASSES — decided from measurement.** No reader-side rule is provably neutral: a delete for an RFQ whose create is still queued in the lane, or that arrived via REST `inject_rfq`, or whose create landed on another shard socket, must reach the intake (registry + F2 liveness). Their cost is small: parse **2.2 µs** (310 B), dispatch + handler **1.0 µs**; nothing is persisted for them in quote mode (`rfq_deletions` table EMPTY in the archive). | Pass-through, stated. |
| FIXED | **Meter accounting stated.** A pre-filtered frame counts toward the shard's `frames` (inbound fps: the connection READ it) and `busy_ns` (the raw judgement IS the reader's service time) and — `created_ts` is read from the raw text — its PIPE LAG, so `ws_pipe_lag` keeps covering every `rfq_created` exactly as before. It therefore counts toward capacity: `fps / utilisation` correctly reads a cheaper per-frame service as more ceiling per connection. `ShardWindow.prefiltered`, `ShardMeter.observe_prefiltered`, `WsManager(on_prefiltered=…)`. | Tested. |
| FIXED | **Telemetry:** `ws_inbound_rate` + `per_shard_prefiltered`, `prefiltered`, `prefiltered_share`, `refresh_interval_s`; metrics `ws.prefiltered`, `ws.prefiltered.rfq_created`, `ws.prefilter_passed`, `ws.prefilter_fail_open` (counts cross with the reader's other counts and fold on the dispatcher's next pass or at stop — a pre-filtered frame never wakes the main loop). | Tested. |
| WRONG | **The governor's refresh rode a TICK COUNT** (`_stall_wall_ticks >= 120`, "~60 s at 0.5 s ticks"). A tick is a PASS and a pass takes 0.5–3 s live, so the fan-out governor fired every **1.2–7.6 min** (`ws_fanout_derivation` 00:38:20 → 00:44:23 → 00:48:55 → 00:50:09 → 00:56:48 → 01:04:26 → 01:10:30 → 01:16:16 → 01:20:11 Z; `ws_inbound_rate.elapsed_s` 456.3 / 364.2 / 345.9) and N reacted in 10–20 min instead of ~60 s. | Measured. |
| FIXED | **`WallTimeCadence` (`exchange/ws_fanout.py`)** — fires when the interval of MONOTONIC time has elapsed, at most once per check, re-stamped on fire (a pass that overran fires ONCE on the next check). The maintenance loop checks it once per pass ⇒ never twice in a pass. **Interval = the maintenance loop's stall-wall FLOOR** (`QuoteApp._stall_wall_floor_s()` = `supervisor.heartbeat_timeout_s` + `MAINTENANCE_TICK_INTERVAL_S` = **60.5 s live**): the loop's own guaranteed-progress horizon — the longest a pass may take before the supervisor treats the loop as wedged — so one window is by construction ≥ one complete pass, anchored to the operator's single wedge-tolerance anchor (the same anchor the stall wall and the fan-out's HEADROOM hang off) rather than a count of passes; numerically the module's designed "~60 s", now anchored. Stamped after every derivation (boot and refresh) so cadence and meter windows share one origin. `ws_fanout_derivation` now logs `window_elapsed_s` (actual) beside `refresh_interval_s` (designed). | Built; fake-clock tests + the REAL `_maintenance_loop`. |
| OPEN (finding) | **The main loop's binding constraint is NOT the firehose transport.** Measured dispatcher + intake cost per foreign `rfq_created` **1.84 µs**, per `rfq_deleted` **1.0 µs**, lane round trip **1.07 µs**: at 3,034 fps the whole dispatch path costs ≈ 5.9 ms/s (**≈ 0.6 %** of the loop) and the pre-filter removes ≈ 4.4 ms/s of it. `slow_callback` totals over the 20:37 boot (82 min): `rfq_worker` 86.5 s / 1,773 calls, `retry_pending` 81.8 s / 311, **`streams.py:read` on the rfq-worker path 55.0 s / 1,115** (+ 17.3 s on rfq-retry, 3.8 s on maintenance — SQLite reads blocking the loop), `pricing_pool.run_joint` 37.7 s, pool `warmup` 30.0 s, `_ProactorReadPipeTransport._loop_reading` 29.7 s / 33 calls; `ws.py:_dispatch_loop` **7.0 s** total. The stale drops (407,500 `rfq_created` on this boot) are a symptom of the loop being held for > 1.5 s by pricing/store callbacks, not of dispatcher throughput. | Next lever = the pricing-path store reads (`streams.read`) + `retry_pending`; out of this build's radius. |
| OPEN | The envelope's wire LAYOUT is unverifiable from this box (the store holds `msg` re-serialized; no raw-frame capture exists). The identifier handles type-first and sid-first layouts (the documented shape, asyncapi-ws.md §3 examples) and fails OPEN on msg-first. **At relight: `ws_inbound_rate.prefiltered_share` must read ≈ 0.49; if it reads 0 with `ws.prefilter_fail_open` climbing, the wire is msg-first and the identifier needs a nested-object skipper.** | Relight check. |
| OPEN | The stall-wall and expired-baseline refreshes keep the tick cadence deliberately: both FOLD cumulative counters / gap histograms (no rate denominator depends on the window), so the slow cadence costs them freshness only. Moving them is the identical fix if ever wanted. | Stated, not changed. |
| OPEN | Pre-existing: `ruff format` still wants `ops/quote_app.py` reformatted on main (980-line noise, not done here); 4 pre-existing mypy errors in `pricing/engine.py` (untouched). Every touched file: ruff + mypy clean. | Debt. |

## Measured (cite → verify)

| Quantity | Value | Source |
|---|---|---|
| Wire type mix | **50.5 % `rfq_created` / 49.5 % `rfq_deleted`** (93,619 vs 91,637) | sum over the 60 `ws_shed_market_frames` lines of `live_20260905_1759.log` — oldest-first shedding is type-agnostic, so the shed is an unbiased sample of the market lane |
| `rfq_created` passing the allowlist | **≈ 1.5 %** (≤ 11.6 % even if every stale drop were allowlisted) | 20:37 boot: `risk_audit` 52,303 lines ≈ one per priced RFQ over 2,436 s = 21/s, vs 3,034 fps × 50.5 % = 1,532 `rfq_created`/s read, 407,500 stale-dropped pre-allowlist (157/s). The archive tail (20,000 rows, 18:51–22:03Z) holds ONLY allowlisted series (quote mode drops the rest pre-parse) — 0 of 20,000 carried a backslash |
| Frames the pre-filter removes | **≈ 49 % of all frames** (≈ 1,500/s at 3,034 fps) | the two rows above |
| Governor cadence, tick-counted | refresh gaps **1.2–7.6 min**; `elapsed_s` 456.3 / 364.2 / 345.9 | `ws_fanout_derivation` / `ws_inbound_rate`, `live_20260905_2037.log` |
| Ticker judgement, real corpus (median 1,066 B, 5 legs; min of 20 reps) | per-occurrence Python **5.73 µs** → head-dict 1.88 → **trie regex 1.03** (flat regex 1.98); `json.loads` **4.62**; `raw_frame_type` 0.43; backslash scan 0.08 | `scratchpad/micro_judge.py`, LOW priority |
| `judge` vs parse on the bench corpus | `rfq_created` **3.79 vs 5.79 µs** (p50 1,039 B); `rfq_deleted` 0.58 vs 2.20 µs (310 B) | `tools/bench_ws_prefilter.py --reps 3 --frames 20000` |
| Main-loop cost per frame (perf_counter, min of 10) | dispatch + intake: foreign `rfq_created` **1.84 µs**, `rfq_deleted` **1.00 µs**; lane push + pop + stale **1.07 µs** | same bench, `dispatch_cost` |
| **Dispatcher CPU per 1,000 wire frames** | before **1.96 ms** (495 × 1.0 + 505 × 2.91) → after **0.52 ms** (495 × 1.0 + 8 × 2.91) = **−74 %**; live ≈ 5.9 → 1.6 ms/s at 3,034 fps | derived from the row above at the measured mix |
| Whole transport pipeline per 1,000 frames (reader + lanes + dispatcher + intake, 20,000-frame wire mix, 3 reps median) | process CPU **15.6 → 10.2 ms (−35 %)**; reader busy **13.1 → 8.4 ms (−36 %)**; wall **15.0 → 9.7 ms (−36 %)** (the first, per-occurrence judge gave −22 / −19 / −18 %) | same bench; `time.thread_time` per-thread figures discarded — tick-sampled on Windows, read 0.0 for a thread that runs in µs bursts |
| Where the loop's time goes (`slow_callback` totals, 20:37 boot, 82 min) | `rfq_worker` 86.5 s, `retry_pending` 81.8 s, `streams.read`(rfq-worker) 55.0 s, `run_joint` 37.7 s, pool warmup 30.0 s, proactor `_loop_reading` 29.7 s, `Task@done` 25.9 s, `streams.read`(rfq-retry) 17.3 s … `ws.py:_dispatch_loop` **7.0 s** | grep + awk over `slow_callback` lines |

## The mechanism

```
 Kalshi communications (global RFQ firehose, ~3,000 frames/s, N = 3 shard sockets)
        │  raw TEXT frame
        ▼  READER THREAD (one per shard)                       exchange/ws.py _read_loop
   ┌────────────────────────────────────────────────────────────────────────────────┐
   │ "\\" in raw?  ──yes──► FAIL_OPEN ─┐                                            │
   │ raw_frame_type(raw): "type" key before the first nested "{" + : "…"           │
   │     unidentifiable ───────────────► FAIL_OPEN ─┤                               │
   │     != rfq_created ───────────────► OTHER ─────┤  (priority / control / deleted)│
   │     == rfq_created:                             │                               │
   │        any "market_ticker": "<allowlisted-prefix…>" ? (ONE trie-regex scan)    │
   │            yes ───────────────────► PASS ──────┤                               │
   │            no  ───────────────────► DROP: count + meter(rate, busy, pipe lag)  │
   │                                       never parsed / queued / dispatched       │
   └────────────────────────────────────────────────┼───────────────────────────────┘
                                                    ▼  json.loads → stamps → lanes → dispatcher → RfqIntake
                                                       (today's path, byte-identical for every non-DROP frame)
```

Placement: the reader thread, before `json.loads` — the earliest point in the process that sees the text, so
the parse (the reader's dominant GIL-holding cost) never happens and the frame never occupies a lane slot, a
dispatcher pop, a stale check or an intake call. The intake's own allowlist check stays exactly where it is:
it still catches the mixed frames (one allowlisted leg + one foreign) the pre-filter deliberately passes.

**Proof of decision neutrality** (full text in the module doc): intake drops ⇔ legs empty ∨ ∃ dict leg with
`¬ market_ticker.startswith(P)`. Pre-filter drops only when (1) no backslash ⇒ every string value's raw text
equals its decoded value; (2) envelope type reads `rfq_created`; (3) every `"market_ticker"` value fails
`startswith(P)`. Each dict leg's ticker is one of those values ⇒ (3) ⇒ intake drops. The pre-filter's drop set
⊆ the intake's drop set; the fan-out set is identical. Sole telemetry-only divergence: a legs array of
non-object items (intake: `RfqParseError` + warning; pre-filter: counted drop) — both drop; the schema makes legs
objects.

**Governor cadence**: `WallTimeCadence(clock, QuoteApp._stall_wall_floor_s())` in `_maintenance_loop`,
`due()` once per pass, stamped in `_refresh_ws_fanout`'s `finally` (boot and refresh) — the meters' window and the
cadence share one origin. Fake-clock tests: fires at 60.5 s elapsed and not at 60.4; a 300 s overrun fires ONCE;
240 passes × 3 s (the live defect replayed) → the tick counter fires 2×, the cadence 11×; 121 passes × 0.5 s → both
once. The REAL `_maintenance_loop` (`_demo_app` + stub lifecycle advancing a `FakeClock` per pass): passes of
61.5 s fire exactly one refresh per pass, passes of 1 s fire none.

## Gates

| Gate | Result |
|---|---|
| New `tests/test_ws_prefilter.py` | **15/15** (identifier layouts + fail-open shapes; `created_ts` raw read; decision-neutrality property over the 600-frame real corpus × layouts + variants; priority/control never dropped incl. hostile error text; shapeless text fails open; constructor rejects escapable prefixes; REAL transport with/without = identical intake + metric split; `start()` refuses a never-drop target; meter accounting; fan-out N = 3 per-shard pre-filtering + `ws_inbound_rate` / `ws_pipe_lag` coverage / `ws_fanout_derivation.window_elapsed_s`; `WallTimeCadence` unit + tick-vs-wall replay; REAL `_maintenance_loop` once-per-pass; floor anchor pinned) |
| Existing ws transport tests (`test_ws_manager`, `test_ws_market_shed`, `test_ws_reader_isolation`, `test_ws_fanout`) | **unchanged, green** (80 in the targeted run with the 15 new = 65 existing + 15) |
| Full suite (LOW priority, worktree `PYTHONPATH`, run 2 of 2 after the test-side fixes) | see the line below |
| Vitals fast tier from the snapshot (`VITALS_DATA_DIR=<scratch>/vitals_snap2`) | **8/8 GREEN (GATE PASS, 100.0 s)** on the build; re-run on the final code below |
| ruff check + format | clean on every new/touched file (`ops/quote_app.py` format debt pre-existing, not touched) |
| mypy | `Success: no issues found in 4 source files` (`ws.py`, `ws_fanout.py`, `ws_prefilter.py`, `quote_app.py`) |
| Collected | worktree **4,240** vs main **4,225** = +15 exactly |

**Final gates on the committed code:** full suite run 2 of 2 (LOW priority, worktree `PYTHONPATH`, `-q`,
output saved) **4,239 passed / 1 failed / 3 deselected in 361.7 s** — the one failure was the NEW fan-out
test's own assertion `line["elapsed_s"] > 0`: `ws_inbound_rate` rounds the window to 0.1 s and under
full-suite load the test's window closed in < 50 ms (reads 0.0). Not a code defect; the assertion now checks the
governor's unrounded `last_window_elapsed_s` and pins the log to its rounding. The file was re-run **3× → 15/15
green** each time (7.5 / 6.5 / 6.5 s). The two full-suite runs this build was allowed are spent (run 1 after the
build: 100 % dots, no failure section, count line suppressed by a double `-q`; run 2 above). Vitals fast tier on
the FINAL code: **8/8 GREEN (GATE PASS, 103.0 s)**. ruff check + format and mypy clean on every touched file.

## Relight checklist (what the first windows must show)

1. `ws_inbound_rate.prefiltered_share` ≈ **0.49** and `per_shard_prefiltered` > 0 on every shard; `ws.prefilter_fail_open` ≈ 0. **A share of 0 with fail-open climbing = msg-first wire layout → the identifier needs a nested-object skipper (fail-open is doing its job; nothing is lost, nothing is saved).**
2. `ws_fanout_derivation.window_elapsed_s` ≈ 60.5 s + one pass (was 346–456 s); `refresh_interval_s` 60.5.
3. `ws_pipe_lag.rfq_created.n` per window ≈ the full `rfq_created` count (coverage unchanged), p50 still ≈ 1.0 s.
4. `rfq.dropped_series_fastpath` ≈ mixed frames only (was ~98 % of created); `ws.msg.rfq_created` ≈ allowlisted + mixed.
5. Lane depth (`depths.market`) and `ws_stale_market_frames` should fall by roughly the pre-filtered share; if stale drops persist at the same rate for the frames that remain, the loop-occupancy finding above is confirmed and the next lever is the pricing-path store reads.

## NEXT STEPS

- **Operator:** merge decision. The build is transport-only with a proven-neutral decision set; the honest expected effect is ≈ −35 % of the transport pipeline's CPU and ≈ −4 ms/s of main-loop time — real but small against a loop held by pricing-path callbacks.
- **Next build (owed, out of this radius):** move the rfq-worker / rfq-retry `streams.read` store reads (55 + 17 s of slow callbacks in 82 min) off the loop; look at `retry_pending` (81.8 s / 311 calls, up to 778 ms each) and the proactor pipe reads from the pricing pool (29.7 s / 33 calls).
- **At relight:** the checklist above; if `prefiltered_share` reads 0, add the nested-object skipper to `raw_frame_type` (a quote-aware brace walk is sound on backslash-free text).
- **Follow-up:** a `thread_time`-free per-thread CPU measure for the bench (Windows tick sampling makes per-thread CPU unusable at µs granularity); consider folding the pre-filter's Metrics counts on the governor tick as well as on the dispatcher pass.
