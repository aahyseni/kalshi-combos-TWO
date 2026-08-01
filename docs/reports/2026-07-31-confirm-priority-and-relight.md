# 2026-07-31 — Adversarial gate, BY EXECUTION: confirm-priority fix + relight sweep

**Scope.** Independent adversarial re-verification of the two same-day workstreams
— the confirm priority inversion fix (`410e8fb`) and the watchdog relight fix
(`b2df3ff`) — by re-running every proof myself and actively trying to break both.
One NEW defect found and fixed (sweep kills non-combomaker processes; commit
below). Everything else re-verified green with numbers.

## Verdict table

| # | Gate item | Result |
|---|-----------|--------|
| 1 | Re-run mid-storm simulation | **PASS** — reproduced: accept 1.2ms WITH lane vs 5,120ms WITHOUT behind a 5.07s/5,000-frame storm (shipped claim 0.8ms vs 5,150ms) |
| 2 | Construct a storm shape where confirms STILL expire | **FOUND ONE (pre-existing, bounded, named)** — a token-starved `_delete_quote` awaited INLINE on the comms dispatcher (budget `_WITHDRAW_RESOLVE_BUDGET_S` = 2.5s) is a single normal dispatch the accept must wait out; 2.5 + 0.72 (in-handler p50) > 3.0s. See "Residual" below |
| 3 | Latency eater named correctly | **CONFIRMED** — FIFO dispatch backlog reproduced (S1 without-lane wait ≈ full storm drain); all comms handlers verified enqueue-only except the rfq_deleted path above |
| 4 | Confirm lane cannot starve quoting (livelock) | **PASS** — gate self-bounds at `EXCHANGE_CONFIRM_WINDOW_S` even with `accept_done` NEVER called (measured resume 3.01s); overlapping stuck accepts resume at last-arm + window (0.92s vs 0.9s bound); priority-frame flood past `_QUEUE_MAX` fails closed (socket close observed) and the normal lane progresses after any finite flood |
| 5 | No risk check weakened at confirm | **PASS** — full chain intact by read: `decide_confirm` last look → fill-velocity governor → reservation (+MC waiver) → candidate MC gate; the honest anchor is guarded `0 < recv_ns <= t0` so it can only SHRINK the derived budget (fail-closed) |
| 6 | Vitals | fast **8/8 GREEN** 19.8s; pre-ship **1/1 GREEN** — V6 live 560ms = 19% of window, margin +2439ms at 1x, fits to 3x, 5x resolves via deterministic fallback (V7 taxonomy) |
| 7 | Relight: 17:34 + all prior proofs | **PASS** — `prove_watchdog` ALL PROOFS PASS in 172s, now **33 checks** (was 30); watchdog units 16/16 |
| 8 | Fool it: helper respawns mid-sweep | **PASS (fail-safe)** — respawned bot pid after stop ⇒ `halt_stop_failed` latch, no start (unit `test_stop_failure_latches_loud`); helper respawn before start ⇒ refuse → re-sweep → refuse → latch (P6 shape). Never a dual stack |
| 9 | Fool it: non-ours python matching the pattern | **FAILED — DEFECT, NOW FIXED.** Proven live: a foreign decoy python (`python -c "…" --tag-combomaker-notes`, PID 29500) and three `polymarket-bot` bash shells were selected for kill by the shipped keyword sweep. Fixed by `tools/ops/ours_predicate.ps1` (below) |
| 10 | Armed-by-default for tonight | **YES, both, no flags** — confirm-priority is wired unconditionally in `quote_app` (already live since the 18:35 relight); the ps1 predicate is read per-invocation, so the next sweep/relight uses it with no watchdog restart |

## The new defect (gate item 9) and its fix

The sweep predicate was a bare keyword net:
`CommandLine -match 'combomaker|fill_prober|hang_watchdog|watch_main|watch_prober'`
— which kills ANY process whose command TEXT mentions those words. Proven live
(read-only evaluation, nothing killed): a foreign decoy python, agent bash
shells from another project, and (at the 18:35/19:00 relights) live analysis
shells + a background vitals run all matched. A `kct-reanchor` checkout's
python would die the same way.

**Why not ExecutablePath alone:** measured live — the venv `python.exe` is a
launcher SHIM whose child (the REAL bot interpreter, identical command line)
runs with `ExecutablePath` = the BASE interpreter
(`C:\...\Python313\python.exe`, bot child PID 25612). An exe-path filter
would spare the real bot = the 17:34 under-sweep class.

**Fix — `tools/ops/ours_predicate.ps1`** (dot-sourced by both scripts; ONE
definition of "ours"): membership = LAUNCH-SITE SIGNATURE, i.e. the exact argv
shapes our own launch sites produce —
```
  ours(python) = venv interpreter (relative ".venv\Scripts\python.exe" as our
                 scripts launch it, or absolute UNDER THIS TREE as
                 supervisor_launch_cmd passes sys.executable)
                 AND one of our entry points
                 (-m combomaker.* | tools\ops\hang_watchdog.py |
                  tools\diagnostics\fill_prober.py)
                 AND any absolute venv path in the argv is under THIS tree
  ours(shell)  = our window titles (BOT (quote mode) | FILL PROBER |
                 HANG WATCHDOG) or -File tools\ops\watch_(main|prober).ps1
```
KeepPid/CallerPid carve-outs unchanged. Applied to the sweep, the leftover
check, the single-instance guard (a foreign python can no longer FALSE-REFUSE a
relight → fewer latch outages), and the duplicate-bots nuke.

**Validation (all executed):**
* Offline: 14/14 shapes — every real launch shape (incl. absolute-path
  supervisor) ours=True; six decoys (foreign python, `kct-reanchor` absolute
  venv, agent bash, grep shell, venv `-c` analysis, vitals) ours=False.
* Live dry-run on the RUNNING stack: old vs new predicate select the IDENTICAL
  11 processes; NEW-only kill set EMPTY; OLD-only (now spared) = the decoys.
* `prove_watchdog` P7 rewritten to evaluate the REAL shipped predicate
  (dot-sourced, not a regex re-model) against the verbatim 17:34 table PLUS
  5 decoy rows; all prior P7 assertions kept and green; 3 ps1 files parse clean.

**Named residuals (accepted):** (a) a RELATIVE-path launch from another
checkout's root is cwd-ambiguous and would still match — only one live stack
exists by design, and absolute-path launches are now tree-scoped; (b) the
orphaned-pool-worker reaper still matches any parentless
`multiprocessing.spawn` python (spawn workers carry NO tree reference in argv;
parent-dead is the only discriminator) — pre-existing, reaps only orphans.

## Residual storm shape (gate item 2) — named, not fixed here

`lifecycle.on_rfq_deleted` runs INLINE on the comms dispatcher and may await
`_delete_quote` up to 2.5s (`_WITHDRAW_RESOLVE_BUDGET_S`, the deliberate
2026-07-27 bound) waiting for write tokens when the bucket is starved AND the
deleted RFQ backs one of OUR resting quotes. An accept arriving mid-dispatch
waits behind it (the lane bounds the wait to ONE dispatch — S3 measured 2.01s
behind a synthetic 2.0s dispatch): 2.5s + 0.72s in-handler p50 > 3.0s.
Requires token starvation + our-quote delete + accept inside the same 2.5s;
none of the 12 historical expiries had this shape. Deep starvation degrades
SAFELY (the honest anchor sees the spent window; the candidate gate declines
on insufficient deadline = a lost win, NOT a renege, and does not increment
the confirm-failure halt counter); the 1.5–2.3s band could still produce an
REST 'expired'. Fixing it means moving the token-wait off the dispatcher —
withdraw-path surgery outside this gate's blast radius; owed as a follow-up
decision.

## Kill switch (task item 3) — unchanged, re-affirmed

12/12 confirm failures ever are HTTP 400 'expired'; there is no
network-error population to split, and any split weakens the reneging
protection. Re-verified the counter finding: `_confirm_failures`
(`lifecycle.py:1631`) is incremented at 5685 and NEVER reset on a successful
confirm — cumulative-per-run despite its "consecutive" message. Making it
truly consecutive is a LOOSENING; left for operator ruling.

## Environment note

Six watchdog auto-relights this evening (18:10→19:00, ~8.5min apart), all
supervisor maintenance-kills (~31s vs 30.5s bound) under agent analysis CPU
load, all relit cleanly through the modified scripts (healthy spans 429–495s ≫
threshold 122s ⇒ no flap latch). The 19:00 sweep killed a background vitals
run mid-flight (old predicate) — the exact friendly-fire class the new
predicate ends. If maintenance-kills continue on an idle box, suspect the new
code, not load.

## NEXT STEPS

* **Operator:** the ONE approved restart tonight loads everything (confirm
  lane already live; ps1 predicate live for the next sweep regardless).
  Decisions owed: (1) confirm-failure counter cumulative vs truly-consecutive;
  (2) latched watchdog halt: cancel-all resting quotes vs TTL-lapse (carried);
  (3) move the rfq_deleted token-wait off the comms dispatcher (the 2.5s
  residual above) — withdraw-path change, needs its own gate.
* **Watch after restart:** `confirm.accept_beat_create_ack`,
  `confirm.accept_dispatch_delay_ms`, `rfq.dropped_accept_priority`; any
  maintenance-kill on an idle box.
* **Next build (unchanged):** P1 Stage 1 per-STRUCTURE / per-game-DIRECTION
  net bounds at the reservation path.
