# 2026-09-05 — BUILD: WS reader isolation + loop-lag instrumentation + shadow-telemetry sampling

**Branch** `build/ws-reader-isolation` (worktree `C:/Users/aahys/kct-ws-reader`, off `main` @ `2c30d1d`).
**Bot** LIVE on `main` throughout; store opened read-only, logs read by grep/tail only, every heavy job at
BELOW-NORMAL priority, vitals from the store snapshot. Nothing was started, stopped, or written under the data dir.
**Blast radius** transport (`exchange/ws.py`) + telemetry (`ops/loop_lag.py`, `ops/telemetry_sampling.py`, a
processor in `ops/logging.py`) + the wiring in `ops/quote_app.py`. **Pricing, risk, lifecycle: untouched** —
`git diff --stat` shows no change under `pricing/`, `risk/`, `sim/`, or `rfq/lifecycle.py`.

## WRONG / FIXED / OPEN

| | Item | State |
|---|---|---|
| WRONG | The socket reader was a coroutine on the main loop. Read+enqueue-only (2026-07-14) stopped handlers from blocking it, but any synchronous stretch of main-loop work still stopped it reading; TCP flow control then backed the exchange up until ITS per-subscription buffer overflowed — error **25 "Subscription buffer overflow"** — and every reconnect discarded the queued auctions. | FIXED — the socket lives on a dedicated thread + loop (`_ReaderThread`); frames cross into the main loop through `_Lanes`. A main-loop stall can no longer stop reading; code 25 can no longer follow from a main-loop stall *while the reader thread gets the GIL* (Python code, SQLite and `time.sleep` release it; a C extension holding it for the stall's length would still starve the reader) and is logged **CRITICAL** with reader diagnostics if it ever recurs. |
| WRONG | Nothing in the process could say WHICH callback held the loop; the tape only showed the consequences (code 25, `HTTP 400 expired` confirms, the 60.5 s maintenance wall). | FIXED — `LoopLagProbe` (`event_loop_lag` per 15 s window; bound = the probe's own period) + `SlowCallbackRecorder` (`slow_callback` / `slow_callbacks_window`: task name + `file:function:line` of the await that ended the blocking stretch; threshold = the measured p99 of callback durations, defined after 100 samples). Hook cost 0.1 µs/callback (measured). |
| WRONG | 85.8% of log lines on the 14:46 boot were six measurement-only shadow read-outs (32 per quote sent), each JSON-rendered and flushed on the loop, paid exactly when the loop is behind. | FIXED — `ShadowTelemetrySampler` structlog processor: 1-in-N when the probe says the loop is behind, N = ceil(lag / period); kept lines carry `sampled_1_in`; `risk_audit` and every decision line bypass it by construction (pinned by tests). |
| OPEN | Accepts are no longer LOST across a stall, but an accept that arrives DURING a stall is dispatched when the stall ends: replay latency = the stall length (5.0 s / 3.0 s / 1.0 s for accepts fed 1-5 s into a 6 s stall). The exchange window is 3.0 s. Isolation removes the overflow; it cannot make the main loop run during its own stall. | The stall itself is the remaining defect — the recorder now names it. Owners: `build/confirm-halt-and-derived-wall` (60 s wall + the never-reset `_confirm_failures` counter), `build/store-rotation-tool` (the 213 GB store behind the maintenance pass). |
| OPEN | In-process replays cannot reproduce the production stall: the vitals rigs (real lifecycle, scratch store, no 213 GB store / MC pool / REST) show ≤ 12 ms of lag and ≤ 6 ms callbacks. The production ranking lands with the first relight of this branch. | Read `slow_callbacks_window` after the first hour live (NEXT STEPS). |

## Measured facts (verified today, read-only)

Per-boot count of `Subscription buffer overflow` lines vs quote accepts, `grep -c` on the D: logs (the 00:52 boot's
3.4 GB file was not re-scanned here; the task's overnight figure 0.7/h with 116 accepts stands):

| Boot (ET) | Log | Duration | code-25 lines | `quote_accepted` lines |
|---|---|---|---|---|
| 09:05 | `live_20260905_0905.log` | 36 min | 34 (57/h) | 3 |
| 11:41 | `live_20260905_1141.log` | 31 min | 72 (139/h) | 4 |
| 14:07 | `live_20260905_1407.log` | 37 min | 80 (130/h) | 5 |
| 14:46 | `live_20260905_1446.log` | 13 min | 24 (111/h) | 3 |

Halts: `halt_receipt.json` 20:00:56Z `halt_confirm_timeouts` / "3 consecutive confirm failures" (the 4th of the day;
the counter is cumulative per boot and never reset on success — `rfq/lifecycle.py:1856/6516/6524`, owned by the
crash-fix branch). Kills: `KILL_20260905_005252.txt` and `KILL_20260905_121339.txt` both "supervisor kill: loop
stalled: maintenance age=61.1s > 60.5s". Watchdog: two more `process_exited` relights at 15:00 and 16:02 ET.

Log composition, 14:46 boot (123,461 lines, 3,316 `quote_sent`):

| event | lines | per send |
|---|---|---|
| `entity_tier_admission` | 49,872 | 15.0 |
| `slate_partition_shadow` | 18,301 | 5.5 |
| `inventory_skew_shadow` | 18,145 | 5.5 |
| `widen_vs_decline_shadow` | 10,812 | 3.3 |
| `structure_bound_shadow` | 8,801 | 2.7 |
| `game_direction_net_shadow` | 39 | — |
| **shadow total** | **105,970 (85.8%)** | **32.0** |
| `risk_audit` | 14,660 (11.9%) | 4.4 |
| everything else | 2,831 (2.3%) | — |

Micro-benchmarks (this box, low priority, `scratchpad/wsr/bench_*.py`, through the REAL `configure_logging`
pipeline to a file): `inventory_skew_shadow` **31.2 µs/line**, `entity_tier_admission` **22.4 µs/line**
(`json.dumps` alone 13.5 / 5.5 µs; the rest is structlog + the flushed `print`), 721 bytes/line average →
~0.7-1.0 ms of loop time per quote sent, 136 lines/s at that boot's rate. `Handle._run` timing hook:
2.318 → 2.417 µs per trivial step = **0.099 µs/callback**. Caveat: the bench wrote to C:; the live log is on D:
beside the store the writer saturates (queue_depth 200,000, checkpoint `busy`), where a flushed write can block
longer — not measurable without writing to the data dir.

## Mechanism 1 — loop-lag instrumentation (`src/combomaker/ops/loop_lag.py`)

```
 main loop ──┬── LoopLagProbe.run()   sleep(period) → lag = actual − period
             │        period = MAINTENANCE_TICK_INTERVAL_S (0.5 s, the finest cadence any loop declares)
             │        window = STATUS_TICK_INTERVAL_S (15 s, the existing operator telemetry cadence)
             │        → log event_loop_lag {max, p50, p99, samples, over_bound, bound_ms}
             │        → Metrics loop.lag_ms (histogram), loop.lag_over_bound
             │        → behind_ratio() = latest lag / period   (≥ 1.0 = "a whole cadence late")
             │
             └── SlowCallbackRecorder   patches asyncio.events.Handle._run for THIS loop only
                      dt = perf_counter_ns around every synchronous callback run (a task step, a plain cb)
                      BitHistogram (log2 buckets, O(1)) → threshold = p99 once n ≥ ceil(1/(1−0.99)) = 100
                      dt > threshold → slow_callback {callback, duration_ms, threshold_ms}  (once/name/window)
                      per window (driven by the probe) → slow_callbacks_window {top by total blocked ms}
                      attribution: task:<name>@<file>:<function>:<line>  (deepest OUR-code frame, asyncio's
                      sleep/Event.wait/Queue.get skipped) — the await that ENDED the blocking stretch
```

No new number: the bound is the probe's own period; the threshold is the process's own p99; 100 is the smallest
sample in which a p99 is an observation. Installed at the top of `QuoteApp.run()` (the boot sequence is measured
too), unhooked in `run()`'s `finally`. The recorder ignores handles of other loops (the reader thread's), so its
aggregates are never raced.

## Mechanism 2 — reader isolation (`src/combomaker/exchange/ws.py`)

```
 reader thread ("<name>-reader", daemon, own asyncio loop)          main loop
 ──────────────────────────────────────────────────────────         ─────────────────────────────────────────
 aiohttp.ClientSession (created + closed HERE, never shared)
 connect ─► ws ─► post _socket_connected(ws) ──────────────────►  _after_connect(ws): on_connect handlers,
 _read_loop: recv → json.loads → stamp _recv_mono_ns                                 _send_subscriptions(ws)
             → lane = PRIORITY | CONTROL | MARKET (by registered type)
             → _Lanes.push  (one lock, O(1))                        _dispatch_loop: await wake
                 PRIORITY full  → RUNAWAY → ws.close() (fail-closed, unchanged)   drain: PRIORITY (all)
                 CONTROL  full  → RUNAWAY → ws.close() (book socket: byte-identical)   → CONTROL → one MARKET
                 MARKET   full  → drop OLDEST market frame, stay connected             (age > stale bound?
             → schedule ONE wake per burst (call_soon_threadsafe, coalesced)             → drop, count) → repeat
 socket dies ─► post _socket_disconnected(ack) ───────────────►  _after_disconnect: discard lanes, metric,
             await ack   ◄──────────────────────────────────────                    on_disconnect handlers
             backoff → reconnect                                                      (cancel-all) → resolve ack
 send_command / force_reconnect from main → run_coroutine_threadsafe on the socket loop (WRITE-dead → force
 ONE reconnect, unchanged).  Reader-side counts fold into Metrics on the main loop (Metrics is not thread-safe).
```

* **Why a thread**: a main-loop stall is by definition a stretch where no coroutine there runs; no rewrite of a
  coroutine can read during it. A CPU-bound main thread yields the GIL every switch interval (5 ms) and releases it
  for I/O and SQLite, so the reader gets to `recv` within milliseconds through a stall of any length.
* **Semantics preserved**: `on_disconnect` completes on the main loop BEFORE any reconnect (the reader awaits the
  ack); every (re)connect re-sends subscriptions with fresh sids; `force_reconnect` for codes 10/17 (the intake's
  `on_channel_lost`) and for a write-dead socket is unchanged; the priority lane is drained before every normal
  dispatch (at most one normal handler of wait); control frames are never dropped; the book socket (nothing
  sheddable) keeps the fail-closed overflow ⇒ reconnect.
* **Overflow is now ours**: capacity shed (oldest market frame, socket stays up — 2026-08-01 policy) PLUS an
  age drop at dequeue — `mark_sheddable("rfq_created", stale_after_s=RFQ_MAX_QUEUE_DWELL_S)` (the worker-side
  dwell horizon the intake already refuses on; no new number); `rfq_deleted` stays ageless. After a stall the
  market lane collapses to its live tail without a handler call per dead frame.
* **Subscriptions are sent on the SPECIFIC socket** (`_send_subscriptions(ws)`), so a connect follow-up delayed
  past that socket's death can never subscribe the next socket twice.
* **Code 25**: the intake's terminal handling (force reconnect — the subscription IS dead per the docs) is kept;
  `WsManager._note_server_error` adds `ws_subscription_buffer_overflow` at CRITICAL with `reader_thread_alive`,
  `frames_read`, `last_rx_age_s`, lane depths, and metric `ws.subscription_buffer_overflow`.
* **The reader thread never logs periodically** (review must-fix 1): capacity sheds are COUNTED on the reader
  (`<name>.shed.<type>` in the lanes' pending metrics) and the aggregated `ws_shed_market_frames` line is written by
  the dispatcher in `_flush_reader_metrics` — on the main loop. `tools/ops/hang_watchdog.py` treats any advance of
  the live log as main-loop work and declares a stall only when the log AND the store are both quiet past its
  threshold (live: 242 s); a reader line every 30 s through a permanent main-loop hang would have kept the log axis
  alive and nothing would have relit. The reader's remaining lines are one-shot per socket (connect / error /
  runaway-close) or per malformed frame.
* **One subscribe per socket** (review should-fix 1): `add_subscription` sends live only while the main-loop-owned
  `_subscribed_ws` IS the current socket; `_after_connect` sets it in the same step as its `_subscriptions`
  snapshot (after the on_connect handlers), `_after_disconnect` clears it. A subscription declared between "socket
  up" and the snapshot rides the snapshot; one declared after it is sent live — never both (Kalshi code 6 / a second
  sid / a leaked pending ack).
* **Thread-safety**: no shared aiohttp objects between loops; `_Lanes` is the only shared structure (one
  `threading.Lock`, swap-free O(1) ops); `_last_rx_mono_ns` / `_ws` / `_frames_read` are single-writer attribute
  stores **read exactly once per property on the main loop** (`connected`, `last_rx_age_s`, `add_subscription` —
  review must-fix 2: a double read can straddle a socket death and raise on the quoting path); reader-side metrics
  accumulate in the lanes and are folded on the main loop. `stop()` cancels the
  socket-loop run task, joins the thread off-loop (`to_thread`), then drains ≤ 0.1 s and cancels the dispatcher.
* **Transport seam**: `WsManager(connect=...)` — production binds aiohttp's `ws_connect` (the receive_timeout /
  heartbeat derivation moved verbatim into `_aiohttp_connect`); the replay harness and the suite bind a recorded /
  fake socket so the SAME thread, lanes and dispatcher run on tape frames (rule 8).

## Mechanism 3 — shadow-telemetry sampling (`src/combomaker/ops/telemetry_sampling.py`)

A structlog processor placed FIRST in `configure_logging`'s chain (a dropped line exits before timestamp/render;
a pass-through costs one set lookup). Registry `SHADOW_EVENTS` = the six read-outs above (lifecycle.py's own
"SHADOW READ-OUT … Telemetry only" helpers); `NEVER_SAMPLED` pins `risk_audit`, `candidate_gate_*`,
`quote_accepted`, `confirm_failed`, `cancel_all`, `halt`, `decline` — the suite asserts no overlap and that every
registered name is a literal in `lifecycle.py`. Bound to `LoopLagProbe.behind_ratio` at boot; unbound (observe
mode, tools, tests) it is inert. **N = ceil(lag / period)** while ratio > 1 (1 in 2 when two probe periods late, 1
in 12 when six seconds late), exactly 1-in-N per event name (deterministic counter), kept lines carry
`sampled_1_in=N` for re-weighting, and each change of N logs `shadow_telemetry_sampling`.

**Why sampling, not an off-loop writer**: the writer thread saves the same ~25 µs/line at every load but delays
every line, including the last ones before a death — the hang watchdog classifies corpses from the log tail and the
KILL/halt receipts are read beside it (the 45 h frozen-log outage was diagnosed from exactly that tail). The
sampler drops nothing while the loop keeps up and never touches a decision line. **Honest size of the win**: ~0.3%
of loop time at the 14:46 boot's steady rate, ~2% in a 20-send/s burst, and whatever a blocked flushed write on the
saturated D: volume costs — small; the mechanism exists because it is paid exactly when the loop is behind.

## Replays (rule 8: live modules; only the socket is a double)

**Transport replay** — `scratchpad/wsr/replay_transport.py`: the last 20,000 `rfqs.raw_json` rows the store
recorded today (18:48-19:06Z, read-only) wrapped as `rfq_created`, fed at the 500 frames/s comms rate through the
REAL `WsManager` (thread reader, lanes, dispatcher) + REAL `RfqIntake` (prefix gate, 1.5 s stale horizon) with the
REAL probe/recorder/sampler installed, a synthetic `quote_accepted` every 2 s, and a scripted main-loop stall:
`time.sleep(1.0)` every 4 s and one `time.sleep(6.0)` at t≈19 s.

| measurement | value |
|---|---|
| frames fed / read by the reader | 20,019 / **20,019** |
| longest gap between two socket reads (during the 1 s and 6 s stalls) | **46 ms** (p99 15.9 ms) — the reader never stopped |
| exchange-side overflow, disconnects, capacity sheds | **0 / 0 / 0** |
| stale `rfq_created` dropped at dequeue (age > 1.5 s, ours) | 2,246 |
| intake `dropped_stale_preparse` (the old gate, now downstream of the lane drop) | 0 |
| fanned out / prefix-dropped | 14,738 / 3,016 |
| accepts sent / dispatched | 19 / **19** (none lost) |
| accept latency, median / max | 6.7 ms / **4,995 ms** (fed 1 s into the 6 s stall; the two behind it 2,993 / 993 ms; 1 s stalls → 1,002 / 1,006 ms) |
| `event_loop_lag` windows (max lag) | 959.5 ms, 5,966.9 ms, 958.5 ms — the stalls, to the millisecond |
| `slow_callbacks_window` top | `task:Task-1@replay_transport.py:main:183` 6,000.7 ms max (the `time.sleep` — attributed to the exact line); dispatcher steps `task:replay-dispatch@ws.py:_dispatch_loop:760` max 14.2 ms; `cb:BaseProactorEventLoop._loop_self_reading` 33.7 ms |
| recorder threshold (derived p99) | 0.262 ms → 0.524 ms as the distribution grew |
| lag samples over the bound | 8 (one per stall) |
| sampler drops | 0 (no shadow lines are emitted without a lifecycle — the sampler is exercised in the suite) |

**Lifecycle blocking-callback replay** — `scratchpad/wsr/replay_lifecycle_blockers.py`: the vitals gate's own
in-process rigs (REAL `QuoteLifecycle`, scratch store) on one loop under the recorder — V2 quoting liveness in the
two degraded states, V3 the largest fan-out wave on the tape, V5 confirm window at the live book ×1/×3/×5, V7
accept inside an await. All four pass; the loop never lagged past 12.1 ms; the only callbacks over the derived
threshold (2.097 ms) were `persistence.py:open:297` 3.9 ms, `v_liveness.py:watch:96` 5.7 ms,
`quote_app.py:_liveness_loop:4541` 2.7 ms, `persistence.py:_write:532` 2.2 ms, and the V5 test's scripted
`slow_confirm` 6.1 ms. **This harness cannot reproduce today's stalls** (no 213 GB store, no MC pool, no REST); it
proves the instrumentation attributes correctly and that the store path is the only lifecycle callback that
registers at all — consistent with the tape (`store_writer_stats queue_depth 200,000`, `checkpoint busy`,
`retained_floor_sweep_timeout`), not proof. No paper-mode QuoteApp was run: it would share the account's REST read
budget with the live bot.

## Tests

New (28): `tests/test_ws_reader_isolation.py` (10 — a REAL reader thread + a REAL `time.sleep` main-loop stall:
every frame pulled off the socket during the stall, all 3 priority frames dispatched, market frames shed 20 by
capacity / 10 by age / 0 dispatched stale, fresh frames after the stall dispatch, 0 disconnects; ageless
`rfq_deleted` never age-dropped; `force_reconnect` runs `on_disconnect` before the second connect and re-subscribes
on the NEW socket only; `send_command` / live `add_subscription` marshalled to the socket loop; code 25 → CRITICAL
with reader diagnostics, other codes not; clean `stop()` joins the thread; `start()` twice refused; **review
fixes** — a real thread + a full market lane + a 200-frame feed under a real `time.sleep` stall: `capture_logs`
holds ZERO lines when the sleep returns and exactly one `ws_shed_market_frames {rfq_created: 195}` after the
dispatcher runs; a subscription declared while `on_connect` holds the snapshot open goes on the wire exactly once
and a later live one exactly once; `connected` / `last_rx_age_s` / `add_subscription` read `_ws` once each via a
counting descriptor),
`tests/test_loop_lag.py` (8 — histogram edges; ratio = lag/period with the bound at exactly one period; a real
stall measured and recovered; derived threshold + attribution `task:slow-task@test_loop_lag.py:blocker:`; install
replaces / uninstall idempotent; `describe_handle` on task, done task, plain cb), `tests/test_telemetry_sampling.py`
(10 — pass-through unbound; nothing sampled at ratio ≤ 1; exactly 1-in-3 at 2.5 with annotation and one change
line; non-finite/broken source fails open; `risk_audit` + decisions untouched at ratios 0…1e6; registry pins;
`configure_logging` installs the sampler first).

Ported, assertions kept (disclosed literally): the buffer changed from two `asyncio.Queue`s to `_Lanes`, so
`m._msg_queue = asyncio.Queue(maxsize=N)` → `m._lanes.capacity = N`; `qsize()` reads → `lane_depths()` / lane
lengths; `Queue.join()` waits → `_settled(m)` (the dispatcher clears `wake_pending` only after a drain found every
lane empty, i.e. after the last handler returned); reader-side metrics need `m._flush_reader_metrics()` before the
same `metrics.counter(...)` asserts; `_carry` proofs → the CONTROL lane (`test_control_frames_survive_shedding_in_
control_lane`, `test_discard_queued_clears_every_lane`); the "normal queue holds frame + wake sentinel == 2" assert
became `lane_depths() == {priority 1, control 1, market 0}` (the sentinel no longer exists — wakes are coalesced
cross-thread). The 2,000-frame mid-storm accept proof (`test_mid_storm_accept_jumps_the_backlog`) runs unchanged.

## Gates

| gate | result |
|---|---|
| unit suite (low priority) | build: **4,070 passed / 0 failed** in 301 s (baseline 4,045; +25 new); **fix pass: 4,073 passed / 0 failed / 3 deselected in 311 s** (+3 review proofs) |
| vitals fast tier, `VITALS_DATA_DIR=<snapshot 20:26Z>` | build: **8/8 GREEN** (99.3 s); **fix pass: 8/8 GREEN (GATE PASS), 104.4 s** (same snapshot — taken after the 20:02Z process death, so it equals the live store's current content) |
| ruff `src tests` | 18 findings on the branch = 18 on `main` (pre-existing; 3 of them in `tests/test_ws_manager.py` predate this build); repo-wide 419 = 419; fix pass: 18 = 18, the four re-weighted tape readers 19 = 19 (pre-existing), touched files clean |
| ruff format | the 7 new/rewritten files formatted; ported test files left at their existing style |
| mypy strict (package) | 6 errors, all pre-existing in `pricing/engine.py` (4) and `pricing/ising_amm.py` (2); **0 in any touched file** (fix pass: unchanged, 6) |
| throughput | not measurable here (rule: never start/stop the live bot). Mechanism argument: per-frame JSON parsing moved OFF the main loop, one uncontended lock op per frame added, 0.1 µs per callback for the hook; the replay dispatched 20k frames at wire rate with a 14 ms max dispatcher step. **Read the first-window sends/min at relight against the 300-460/min benchmark** (8/26: 655/min). |

## Review fixes (2026-09-05 fix pass, after the SHIP_WITH_FIXES review)

Every must-fix and every should-fix applied except one, declined with evidence (row 8). Blast radius unchanged:
`git diff main` under `pricing/ risk/ sim/ rfq/` is still 0 lines; nothing was started, stopped or written under
the data dir.

| # | review item | what changed | proof |
|---|---|---|---|
| 1 | **MUST** — the reader thread logged `ws_shed_market_frames` every `max_silence_s` while the market lane stayed full, which would keep the hang watchdog's log axis alive through a permanent main-loop hang (`hang_watchdog.py poll_once`: stale only when `log_quiet > threshold AND store_quiet > threshold`) | `_record_shed` (reader) now only COUNTS (`<name>.shed_market_frames`, `<name>.shed.<type>` in the lanes' pending metrics); `_flush_reader_metrics` (dispatcher, main loop) folds the counts and writes the aggregated line with the same first-shed-immediate / at-most-once-per-`max_silence_s` policy. The reader's remaining lines are one-shot per socket or per malformed frame. | `test_reader_thread_writes_no_log_line_during_a_main_loop_stall`: real thread, capacity 5, 200 frames fed under a real `time.sleep` stall → `capture_logs == []` when the sleep returns (the main thread never ran, so anything captured would be the reader's), then exactly one `ws_shed_market_frames {rfq_created: 195}` after `_settled`, metrics 195/195, the newest 5 frames dispatched in order. The old code fails this test on its first shed. |
| 2 | **MUST** — `connected` read `_ws` twice (`self._ws is not None and not self._ws.closed`); the reader thread writes `_ws`, so a socket death between the reads raises `AttributeError` on the quoting path (`feed.watch` → `add_subscription` leaves legs unwatched; the last look's `rx_age_s` drops the quote) | `connected`, `last_rx_age_s` and `add_subscription` snapshot the reader-written attributes to a local once (`_note_server_error` goes through the fixed property). | `test_health_properties_read_the_socket_attribute_once`: `_ws` as a counting descriptor — each property reads it exactly once, connected and disconnected, never raises. |
| 3 | should — double-subscribe window between "socket up" (`_ws` set by the reader) and `_after_connect`'s snapshot | Main-loop-owned `_subscribed_ws`: set by `_after_connect` in the same synchronous step as the `list(self._subscriptions)` copy (after the on_connect handlers), cleared by `_after_disconnect`; `add_subscription` sends live only while `_subscribed_ws is self._ws`. | `test_subscription_added_before_the_snapshot_is_sent_exactly_once`: `on_connect` holds the window open; the sub declared inside it is NOT sent live, goes out exactly once with the snapshot; a later live one exactly once. `test_force_reconnect_…` still proves resubscribe on the NEW socket only. |
| 4 | should — `QUANTILE = 0.99` and `report_top = 20` implied "derived" | Labelled: `q` is a DECLARED POLICY ANCHOR (stated like z 3/4/5; only the threshold value and `MIN_SAMPLES = ceil(1/(1-q))` derive from it); `report_top` is a display cap. Docstring + comments only; no behaviour change. | `tests/test_loop_lag.py` unchanged, passing. |
| 5 | should — shadow-line consumers not re-weighted for `sampled_1_in` | `tools/diagnostics/conc_steer_shadow_readout.py` and `tools/diagnostics/pbook_shadow_readout.py` weight every kept line `sampled_1_in` times (absent = 1) so counts and quantiles estimate the population, and print how many kept lines carried the annotation; `docs/research/rfq_throughput/scripts/log_latency_anatomy.py` weights the `inventory_skew_shadow` per-second counter (it IS the priced-RFQ throughput read-out) and annotates the line; `tools/proto_skew_settled_resolution.py` is a PER-EVENT replay (a weight cannot re-create the missing RFQs), so it carries `weight` on each event and prints the annotated count + the implied population instead of re-weighting. | `py_compile` + ruff on the four files: 19 findings = 19 on HEAD (pre-existing). |
| 6 | should — `SlowCallbackRecorder.install()` at the top of `run()` but `uninstall()` only in the inner finally around `_stop.wait()` | `run()` now installs, then `try: await self._run_instrumented() finally: unbind(); uninstall()` — the body after the bind moved verbatim into `_run_instrumented()`; the inner finally keeps its (idempotent) calls because its ORDER matters (unhook before the shutdown stages, whose lines must never be sampled). | `tests/test_quote_app_phase6.py` passing in the touched-set run; `uninstall()`/`unbind()` idempotency is by construction (`_installed is not self` guard; `unbind` resets state). |
| 7 | should — `_ReaderThread.stop()` joins without a timeout | Comment added: the thread is a daemon and the await runs inside the `ws_stop` ShutdownStage's wall, which is the bound that applies. | — |
| 8 | should — `_ReaderThread._main` closes the loop without `shutdown_default_executor()`, "worker threads leak" | **Declined, with evidence.** CPython 3.13 `BaseEventLoop.close()` (docstring: "shuts down the executor, but does not wait for the executor to finish") already calls `executor.shutdown(wait=False)`: idle getaddrinfo workers exit on the sentinel, a worker mid-call exits when the call returns — nothing lingers idle. What `shutdown_default_executor()` would add is a JOIN of that in-flight call, on the reader thread, inside `stop()`: a resolver wedged in `getaddrinfo` (the 8/27 wake-DNS failure class) would hold `stop()` for the resolver's own timeout inside the shutdown wall, for no liveness gain (at interpreter exit `concurrent.futures` joins the worker either way). Documented in the `finally`. | `asyncio/base_events.py` lines 731-751 on this box. |
| 9 | should — "code 25 impossible by construction" over-promised | Qualified in `ws.py`, this report's WRONG/FIXED row and Mechanism 2, and the README index: impossible from a main-loop stall while the reader thread gets the GIL; a C extension holding the GIL for the stall's length still starves it — the CRITICAL line covers detection. | — |

**Live-state fact carried from the review (verified again read-only at 21:17Z):** no `python.exe` on the box; the
pointer log `live_20260905_1602.log` is 3,888 bytes, last written 16:02:08 ET; `heartbeat.txt` absent;
`hang_watchdog.log` ends at 16:02:11 ET. The 16:02 ET relight died at boot and nothing relit it — the stack is
DOWN and the watchdog itself appears not to be running. Not caused by this branch; "relight on this branch" in
NEXT STEPS is now the operator's start, not a merge-and-wait.

**Scratchpad hygiene note:** the session scratchpad is shared with the other fix-pass agents (a `fixpass/` patch
script of this pass was overwritten by the store-rotation pass mid-flight); this pass's scripts moved to a
task-unique directory and were marker-checked before every run. Nothing of theirs was run here.

## NEXT STEPS

1. **Relight on this branch (operator start, WMI-detached)** — merge after review; watch the first hour for:
   `event_loop_lag` every 15 s (`max_lag_ms`, `over_bound`), `slow_callbacks_window` (the production ranking of
   blocking callbacks — the answer to "which callbacks block the reader"), `shadow_telemetry_sampling` lines (N > 1
   only while behind), `ws_stale_market_frames` / `ws_shed_market_frames` counts, and `ws_subscription_buffer_
   overflow` (must be ZERO; any line is a new cause). Verify sends/min ≥ 300-460.
2. **The stall itself** is now the defect: the recorder's ranking after hour one goes to `build/confirm-halt-and-
   derived-wall` (60.5 s wall, `_confirm_failures` never reset) and `build/store-rotation-tool` (213 GB store).
3. **Accept latency across a stall = the stall length** (measured 5.0 s vs the 3.0 s exchange window). If the
   ranking shows stalls the store rotation cannot remove, the next mechanism is a confirm path that does not wait
   for the main loop (the lifecycle's last-look/reservation would have to become thread-safe — a separate build).
4. The intake's pre-parse stale gate is now downstream of the lane's age drop (0 hits in the replay); keep as
   defense in depth, revisit only if a measurement shows it costing.
5. ~~Tape readers that count shadow lines must weight by `sampled_1_in` (absent = 1).~~ DONE in the fix pass
   (four readers re-weighted, see Review fixes); any NEW reader of a shadow event must do the same.
6. Pre-existing debt seen in passing, not touched: 6 mypy errors in `pricing/engine.py` / `pricing/ising_amm.py`;
   18 ruff findings in `src`+`tests`.
