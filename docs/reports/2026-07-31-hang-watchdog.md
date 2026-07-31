# 2026-07-31 — Hang watchdog: external supervisor that watches WORK, not PIDs

**Status: BUILT + PROOFS EXECUTED. Arms automatically on the next operator
START_BOT.bat — nothing running now was touched.**

## The incident class this closes

| when | what happened | why supervision was blind |
|------|---------------|---------------------------|
| 7/29 09:23 EST → 7/31 | Maintenance loop stalled; in-tree supervisor CORRECTLY detected it (`supervisor_loop_wedged`, `age=31.1s > 30.5s`), wrote KILL + marker, started shutdown — and the shutdown itself hung at `book_ws_stop` (`shutdown_timed_out` was the process's last line). **PID stayed alive 45h with the log frozen.** | Every existing layer lives INSIDE the process tree it certifies. Once the process wedged past its own bounded-shutdown `os._exit`, nothing outside was watching. `START_BOT.bat` then refused to relaunch over the live PIDs — by design. |
| 7/31 09:00 EST | Config validation error (`risk.resting_floor_count` 0) → bot exited in <1s. | A naive auto-restarter would have boot-looped this forever. Any restart mechanism must refuse exactly this case. |

## What shipped

| piece | file | role |
|-------|------|------|
| Watchdog | `tools/ops/hang_watchdog.py` | External process (own window, 5th in the stack). Watches observable WORK; stops/relights through the existing operator scripts; bounded escalation with halt receipts. Imports NOTHING from `combomaker`. |
| Start wiring | `tools/ops/start_all.ps1` | Launches the watchdog window on operator starts; new `-Auto`/`-CallerPid` mode so watchdog relights reuse every existing guard (mutex, single-instance, machine-KILL auto-clear); human-gated KILL in `-Auto` = refuse (exit 1), never prompt; operator start purges `watchdog_state.json` (fresh episode). |
| Stop wiring | `tools/ops/stop_all.ps1` | Watchdog joins the FIRST kill group (it can resurrect the bot, so it dies before the bot does); new `-KeepPid` exempts the watchdog during its own recovery stop. |
| Unit tests | `tests/test_hang_watchdog.py` | 15 tests on the real state machine (fake probes/clock). |
| Executed proofs | `tools/ops/prove_watchdog.py` | Re-runnable scratch-tree proofs (P1–P4 below), `tools/vitals/prove.py` pattern. |

## Detection: two WORK axes, never process-aliveness

```
                 +--------------------------------------------+
                 |               hang_watchdog                |
                 |   (external, tools/ops, stdlib-only)       |
                 +--------------------------------------------+
                    |                |                  |
       every poll   v                v                  v
   +----------------------+  +-------------------+  +----------------------+
   | active log advance   |  | store decision    |  | process probe        |
   | (size, mtime) of the |  | flow: last        |  | (classifier ONLY:    |
   | CURRENT_LOG.txt bot  |  | decisions row     |  | hang vs exit; a      |
   | log                  |  | `at` VALUE        |  | failed probe reads   |
   +----------------------+  | advancing         |  | as "present")        |
                             +-------------------+  +----------------------+

   STALL  =  BOTH axes quiet  >  derived threshold
   (either axis moving = healthy; a lull is never a hang)
```

- **Why the row VALUE, not WAL mtime:** measured today, the persistence writer
  runs ~68 min behind the event firehose (last visible `decisions.at`
  13:16:06Z at wall 14:24:45Z while the `-wal` mtime advanced). After a hang
  the WAL keeps moving while the backlog drains — the row value freezes at the
  hang and is the honest signal. Store probe is read-only (`mode=ro`, 2s
  timeout); an unreadable store just leaves detection to the log axis.
- **Heartbeat/loop_progress are deliberately NOT progress axes** — on 7/29 the
  in-tree liveness machinery was part of the wedged process. The heartbeat file
  is used for exactly one thing: the boot-loop discriminator (below).

## Threshold — derived from the tape (north star: no hand-set numbers)

```
threshold = max( MARGIN x max healthy quiet gap over all completed live_*.log,
                 MARGIN x internal escalation chain (2*hb_timeout + hb_poll) )
MARGIN = 2  (act only when the quiet is TWICE the worst ever observed healthy)
```

Measured (full current-era tape, 52 completed `data/live_*.log` runs, 7/22 → 7/31,
overnight lulls and the pre-hang 7/29 run included; decision-flow union scanned
as well — log-only gaps upper-bound the joint quiet, so deriving from logs is
conservative):

| quantity | value |
|----------|-------|
| Longest healthy quiet gap, log-only | **34s** (once, run-teardown tail of `live_20260726_0943.log`); every other run maxes at ≤ 30s — a 30s-cadence emitter keeps the log alive even through the 7/26 04:36 and 7/27 18:39→03:11 overnight runs |
| Tape term | 2 × 34 = 68s |
| Internal-chain floor (live anchors `heartbeat_timeout_s=30`, poll 1.0 — the in-tree supervisor + bounded shutdown must get to finish first, ×2) | 2 × (2×30 + 1) = **122s** |
| **Armed threshold (live tree, derived + cached)** | **122.0s** |
| Poll cadence | threshold/10 clamped to [5s, 60s] → 12.2s; detection latency ≤ 1.1× threshold |

Margin over the worst observed healthy lull: 122/34 = **3.6×**. The 45h outage
becomes a ~2-minute outage; a boot-dead process is caught in 2 polls.
Cross-check on guard 1's timing: the vitals tape facts measure
launch-to-first-heartbeat at 34–47s across recent boots — a healthy boot
always beats well inside one 122s detection window, so the liveness
discriminator cannot misread a slow-but-healthy boot as a boot failure.
Derivation is incremental (`data/watchdog_tape.json`, size-keyed per completed
log) — every operator start folds in the newly completed logs, so the threshold
keeps adapting to the tape with no knob to move.

## Escalation — bounded, refuses to flap

```
stall/exit --> evidence line --> STOP (stop_all.ps1 -NoPrompt -KeepPid me)
      |                                   | pids remain?  --> HALT (receipt)
      |                                   v
      |            guard 1: no heartbeat => run never reached liveness
      |                     => BOOT LOOP => HALT (receipt), 0 relights
      |            guard 2: last TWO relights each died inside one
      |                     detection window => FLAP => HALT (receipt)
      v                                   v
   relight (start_all.ps1 -Auto) -- rc!=0 (human KILL, guard) --> HALT (receipt)
                                   rc=0  --> fresh grace window, keep watching
```

- Guard 1 reuses `start_all.ps1`'s own no-invented-number flap discriminator
  (heartbeat existence = the run reached liveness). Guard 2's "two consecutive"
  is the minimal repeat that distinguishes a pattern from a one-off; "one
  detection window" is the derived threshold itself. No new constants.
- A latched halt writes `data/WATCHDOG_HALT_<stamp>.txt`, keeps shouting to the
  window + `data/hang_watchdog.log` every 5 min, and stays down until an
  OPERATOR start (which purges the state file — a human relight is the
  "reviewed and ready" event).
- A machine-written KILL from a run that reached liveness is auto-cleared by
  `start_all.ps1` exactly as before — so the watchdog also automates the
  "supervisor killed a bot at 06:09, operator relit at 09:00" 3h gap from this
  morning. A risk halt (human-gated KILL) makes `-Auto` exit 1 → watchdog
  latches and stays down. Risk semantics unchanged.

## Executed proofs (scratch trees, real watchdog binary, real sqlite store)

`.venv/Scripts/python.exe -m tools.ops.prove_watchdog` — threshold derived
in-scratch from a synthetic tape (max gap 2s) + scratch anchors → 5s; poll 1s.

| proof | scenario | result |
|-------|----------|--------|
| P1 | **7/29 class**: PID "alive" (probe), log+store frozen | PASS — exactly one stop→start, hang named (`STALL ESCALATION (hung_process)`), relit healthy run left alone, no halt |
| P2 | **7/31 09:00 class**: boot-only log, no heartbeat, process gone | PASS — stop-sweep only, **zero relights**, halt receipt names "never reached liveness", stays down loud |
| P3a | Healthy lull just inside threshold (overnight shape, writer every 4s vs 5s bound) | PASS — zero actions, zero state mutations over 25 cycles |
| P3b | Log frozen, decision flow alive (store axis) | PASS — zero actions over 18 cycles |
| P4 | Relit runs re-hang instantly | PASS — exactly two relights (`stop,start,stop,start,stop`), flap latch + receipt, third relight refused |

Plus 15 unit tests on the state machine (start-refusal latch, stop-failure
latch, probe-failure never fabricates an exit, episode reset on operator start,
derivation cache/active-log exclusion, boot-only log scan).

## Verification

| check | result |
|-------|--------|
| Unit suite | **3430 passed / 0 failed** (3 integration deselected), 258.7s — includes the 15 new watchdog tests |
| Executed proofs | **ALL PROOFS PASS**, 18/18 checks, 136s (`tools.ops.prove_watchdog`) |
| Live-tree derivation (read-only, cache pre-warmed) | `logs_measured=52, max_healthy_gap_s=34.0 (live_20260726_0943.log), internal_chain_floor_s=122.0 (hb 30.0 + poll 1.0), threshold_s=122.0` |
| `tools.vitals.gate` fast tier | IN FLIGHT at commit time — the gate is doing its one-time tape-facts refresh over the ~5 GB of new 7/29–7/31 logs (its `tape_facts.json` already shows the new `launch_to_first_beat` rows, 34–47s). NOTE: this build touches NO file in rule 9's mandatory set (no `src/combomaker/**`, no quote/confirm/caps config) — the gate run is belt-and-braces. Verdict to be appended by the next session/operator: run `.venv/Scripts/python.exe -m tools.vitals.gate` (fast after the refresh caches) and confirm 8/8 GREEN. |
| Ruff lint + format on new/changed files | clean |
| Live tree untouched while bot runs | Watchdog NOT started against the live tree; ps1 edits load only on the next operator START_BOT.bat; derivation pre-warmed read-only (`data/watchdog_tape.json` cache written so first armed start needs no big scan) |

## Blast radius (fix-isolation rule)

- **Pricing path: zero.** No `src/combomaker` module changed. The watchdog is a
  separate process, imports nothing from the bot, reads the store `mode=ro`,
  never talks to Kalshi, and its only mutations are the operator's own
  stop/start scripts.
- **Ops scripts:** `start_all.ps1`/`stop_all.ps1` changed; behavior for a plain
  operator start/stop is identical except one extra window (watchdog) and the
  state-file purge. `-Auto`/`-KeepPid`/`-CallerPid` are additive.
- **Throughput:** nothing on the quote path; watchdog polls are a file stat, a
  read-only sqlite point query, and a CIM process query every ~12s. Before/after
  quotes-per-min not applicable (no pricing-path change); confirm on next
  restart's first hour regardless.

## Known limits (stated, not hidden)

1. On a latched halt the watchdog does NOT cancel resting quotes (they lapse on
   TTL; a relit bot startup-reconciles + cancel-alls as always). Same posture as
   an operator stop answered "n". Wiring the supervisor-key cancel-all into the
   halt path is an operator decision (it means giving the watchdog exchange
   write access — today it has none).
2. The `-Auto` KILL-refusal branch of `start_all.ps1` is proven by the unit
   test on the watchdog side + inspection on the ps1 side (can't execute the
   real ps1 guard path while the live bot runs — its single-instance guard
   fires first). Verify once at the next operator restart (below).
3. If the 30s log emitter is ever removed/slowed, the threshold self-heals only
   at the NEXT operator start (derivation runs at watchdog boot). Until then the
   floor (122s) still dominates unless the emitter goes quieter than ~61s.

## NEXT STEPS

- **Operator**: on the next relight, use START_BOT.bat as always — the 5th
  window (HANG WATCHDOG) must appear, log `armed: threshold=122.0s`, and sit
  quiet. That start doubles as the live verification of the new ps1 paths
  (watchdog window, state purge, one-bot verify).
- **Operator decision owed**: whether a latched watchdog halt should also
  sweep resting quotes via the supervisor key (known limit 1).
- **Me (next session)**: after the first watchdog-armed week, pull
  `data/hang_watchdog.log` — confirm zero false escalations across real
  overnight lulls; fold any new completed logs' gap profile into this report if
  the tape shape changed.
- **Unchanged priority**: P1 Stage 1 (per-STRUCTURE + per-game-DIRECTION net
  bounds at the reservation path) remains the next risk build.
