# 2026-07-31 — Watchdog relight BLOCKED at 17:34 ET: root cause + fix + proofs

**Status: SHIPPED in tree (arms on the operator's ONE approved restart tonight).
Proofs 30/30 (17 pre-existing stayed green + 13 new), watchdog unit tests 16/16,
both ps1 parse clean.**

| | What | Detail |
|---|------|--------|
| **WRONG** | Watchdog detection fired perfectly at 17:34:10 ET (`process_exited`, 3 probes) and the stop pass ran — but `start_all.ps1` refused rc=1 "Run STOP_BOT.bat first" and the watchdog latched. Full outage until the operator's manual 17:54 relight. | The refusing "bot" was **PID 41592: `.venv\Scripts\python.exe tools\ops\hang_watchdog.py run` — the watchdog's OWN venv launcher SHIM**. The venv python.exe spawns the real interpreter as a child with an IDENTICAL command line (a documented fact in start_all.ps1 since 2026-07-25); the watchdog passes only the CHILD's pid as `-CallerPid`, so the shim matched `'^python'` + `'hang_watchdog'` and tripped the single-instance guard. `stop_all -KeepPid` had CORRECTLY spared it (the whole watchdog tree survives its own stop by design) — the stop side and the start side disagreed about the same tree. |
| **WRONG (prompt correction)** | The task briefing said the guard saw "surviving helper processes (fill prober / prober monitor windows)". | The tape says otherwise: the stop pass DID kill both fill-prober pythons (39052, 34844), the prober-monitor powershell (31944) and both cmd hosts, and logged "All combomaker/prober processes stopped." The sole blocker was the watchdog shim. (The `watch_main.ps1` monitor window never matched the sweep predicate at all — a real gap, fixed below, but it is a powershell and could not have tripped the python-only refusal.) |
| **FIXED 1** | `tools/ops/start_all.ps1` — the root cause. | In `-Auto` (watchdog-initiated relight), pythons whose command line matches `hang_watchdog` are exempt from the single-instance refusal — they ARE the caller's tree, spared by the stop pass by design, and none of them is a bot. Operator (non-Auto) starts still refuse on them (a live watchdog could relight against operator intent). A genuine leftover `combomaker.ops.cli run` python still refuses in both modes. |
| **FIXED 2** | `tools/ops/stop_all.ps1` — sweep equivalence. | Sweep predicate extended `combomaker|fill_prober|hang_watchdog` → `…|watch_main|watch_prober`: the stop pass (operator AND watchdog `-KeepPid` flavors) now takes the monitor windows too, so it is the complete stack teardown — bot, probers, BOTH monitors, watchdog — with the single `-KeepPid` carve-out for the recovering watchdog's own tree. |
| **FIXED 3** | `tools/ops/hang_watchdog.py` — retry once, never latch on our own incomplete sweep. | On a start refusal: ONE full re-sweep (same stop path) → verify zero bot pids → ONE retry → only a second refusal latches (`"start path refused the relight twice"` — authoritative). A re-sweep that cannot clear the tree latches as `halt_stop_failed` (human needed). Every existing guard preserved bit-for-bit: human-KILL refusal still latches (now after provably-clean retry), boot-loop zero-relight untouched, flap latch at two relights untouched, halt receipts untouched. |
| **OPEN** | Latched watchdog halt still does not cancel-all resting quotes (TTL lapse only) — pre-existing, operator decision owed (carried from the 2026-07-31 hang-watchdog report). | Unchanged here. |

## The 17:34 tape (data/hang_watchdog.log, verbatim)

```
17:34:10 STALL ESCALATION (process_exited): no bot process on 3 consecutive probes, log quiet 25s
17:34:13   | Stopping PID 22652: cmd /k title BOT (quote mode) ...
17:34:13   | Stopping PID 37500: cmd /k title FILL PROBER ...
17:34:13   | Stopping PID 31944: powershell ... watch_prober ...
17:34:13   | Stopping PID 39052: .venv\Scripts\python.exe  tools\diagnostics\fill_prober.py
17:34:13   | Stopping PID 34844: .venv\Scripts\python.exe  tools\diagnostics\fill_prober.py
17:34:13   | All combomaker/prober processes stopped.
17:34:14   | REFUSING TO START - the bot/prober is already running:
17:34:14   |   PID 41592: .venv\Scripts\python.exe  tools\ops\hang_watchdog.py run
17:34:14 HALT LATCHED — start path refused the relight (rc=1)
```

Detection: perfect. Stop: complete (per its own predicate). Recovery: blocked by
the start guard's blindness to the venv shim/child split it itself documents.

## Proofs (tools/ops/prove_watchdog.py — 30/30 PASS, 168s)

* **P1–P4 (pre-existing, 17 checks): ALL STAY GREEN** — frozen-log hang relights
  once; boot loop gets ZERO relights + receipt; healthy lull / store-axis
  progress gets zero actions; flap latches after exactly two relights.
* **P5 start_refused_retry (NEW)** — the exact 17:34 class (app dead + a
  survivor of the first sweep): actions are `stop → start_refused → stop
  (re-sweep) → start`; relight SUCCEEDS, one relight recorded, NO latch, no
  receipt.
* **P6 kill_still_refuses (NEW)** — a human-gated KILL refusal survives the
  re-sweep: exactly one retry, never a third start; latch reason "refused the
  relight twice"; receipt written; stays down loud. **Fail-safe posture
  unchanged.**
* **P7 ps1_guard_parity (NEW)** — the SHIPPED predicate text is extracted from
  the .ps1 files and evaluated against the verbatim 17:34 process table (real
  PIDs): the sweep takes `{22652, 37500, 31944, 31900(watch_main), 39052,
  34844}` and keeps the watchdog tree `{41000, 41592, 41593}`; the `-Auto`
  guard passes over the surviving watchdog tree (shim included), an operator
  start still refuses on a live watchdog python, and a genuine leftover bot
  python (`combomaker.ops.cli run`) still refuses in `-Auto`. Both scripts
  parse clean via `System.Management.Automation.Language.Parser`.

Unit tests: `tests/test_hang_watchdog.py` 16/16 — `test_start_refusal_latches`
updated to the new contract (`STOP, START, STOP, START` then latch) + new
`test_start_refusal_cured_by_resweep_relights` (survivor cleared by the
re-sweep ⇒ `relit`, no latch).

## Blast radius

`tools/ops/{start_all.ps1, stop_all.ps1, hang_watchdog.py, prove_watchdog.py}` +
`tests/test_hang_watchdog.py` only. Zero pricing-path or confirm-path code in
this defect's diff (the confirm work is the separate same-day report
`2026-07-31-confirm-priority-inversion-fix.md`). Nothing here touches the live
bot process; all of it arms at the operator's one approved restart.

## LIVE-FIRED ADDENDUM (same evening, 18:10–18:35 ET — unplanned but conclusive)

While this work was still uncommitted, the machine's own analysis load (two
agent sessions running 17GB tape greps, full 3,452-test suites, benches and two
vitals gates concurrently) starved the LIVE bot's event loop three times; the
in-tree supervisor emergency-killed on marginal maintenance overruns (30.9s /
31.5s / 31.1s vs the 30.5s bound — the exact "one tick over" 2026-07-27 class)
at ~18:10, ~18:26 and ~18:35 ET, and the watchdog auto-relit all three times
**through the MODIFIED scripts**:

* three machine-KILL auto-clears + three clean relights, receipts archived
  (`data/KILL_20260731_18{1802,2653,3546}.txt`), zero latches, zero start
  refusals — the 17:34 refusal class did not recur;
* the 18:35 sweep stopped BOTH monitor powershells (watch_main + watch_prober)
  — FIXED 2 observed live (17:34's sweep missed watch_main);
* flap guard correctly did NOT latch: each run lived ~8 min (≫ the 122s
  detection window), so these were legitimate relights, not a flap;
* **consequence: the watchdog effectively performed the restart early — the
  bot running since 18:35 ET (`data/live_20260731_1835.log`) is ALREADY on the
  merged confirm-priority + watchdog code**, booted clean, preflight green, no
  tracebacks, quoting pipeline live.
* **Collateral to know about**: the sweep predicate matches any process whose
  COMMAND TEXT contains `combomaker` etc. — at 18:35 it force-killed several
  of the agents' own analysis shells (and one background vitals run). By
  design (a teardown must be thorough), but background tooling on this box
  must expect to die during a relight.

The cycle's driver was analysis CPU load, now stopped. **Watch item: if
supervisor kills recur with the box idle, suspect the new code instead — but
5+ min of the 18:35 run show a healthy, quoting bot.**

## NEXT STEPS

* **Operator** — tonight's ONE restart lands this together with the confirm
  priority-inversion fix; nothing else to do — the watchdog re-arms itself via
  START_BOT.bat.
* **Operator decision owed (carried)** — should a latched watchdog halt
  cancel-all resting quotes instead of letting them TTL-lapse?
* **Next session** — after the first watchdog-initiated relight in production,
  pull `data/hang_watchdog.log` and confirm the retry path never fired
  spuriously (expected: it fires only after a start refusal, which should now
  be rare-to-never).
