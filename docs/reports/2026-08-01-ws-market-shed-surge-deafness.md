# 2026-08-01 — PREGAME-SURGE DEAFNESS: class-aware WS overflow (shed market data, never disconnect)

**Status: BUILT + PROVEN on branch `ws-marketdata-shed`; loads at the next
restart. The live bot was NOT touched. The operator likely wants THIS restart
FAST — the bot is missing its filling window right now (checklist at the
bottom).**

## 1. THE LIVE DEFECT (measured on `data/live_20260801_1142.log`)

Since 16:08Z (12:08 ET — the pregame surge, and we quote PREGAME ONLY: this
surge IS the filling window) the comms socket cycled
`ws_dispatch_queue_overflow` → `ws_queue_discarded` → `ws_disconnected` →
reconnect **14 times in 62 minutes**, discarding **276,644 queued frames** —
including every queued `rfq_created`. Result: near-ZERO new-RFQ intake for
most of 90 minutes while the bot kept repricing its existing book (~500
reprice-sends/min) into a void. Confirmed NOT pricing (margins normal, med
2.89¢) and NOT reneges (0 declined wins).

Fill cycles measured from the tape (connect → overflow; 20,000-frame queue):

| connect (Z) | fill time | net accumulation (inflow − drain) |
|---|---|---|
| 15:43:27 | 1479.6s | 14 f/s |
| 16:08:08 | 643.9s | 31 f/s |
| 16:18:53 | 733.2s | 27 f/s |
| 16:31:08 | 195.8s | 102 f/s |
| 16:34:25 | 223.2s | 90 f/s |
| 16:38:10 | 649.0s | 31 f/s |
| 16:49:00 | 151.9s | 132 f/s |
| 16:51:33 | 108.0s | 185 f/s |
| 16:53:23 | **74.5s** | **268 f/s** |
| 16:54:38 | 81.4s | 246 f/s |
| 16:56:00 | 93.3s | 214 f/s |
| 16:57:35 | 164.2s | 122 f/s |
| 17:00:20 | 165.0s | 121 f/s |
| 17:03:06 | 160.9s | 124 f/s |

Inflow exceeded the real drain CONTINUOUSLY and worsened into first pitch. No
finite queue "fits" a sustained inflow>drain regime — the p99 "surge" here is
the whole hour — so the repair is a POLICY split, not a bigger number.

**Provenance:** `410e8fb` (confirm-priority lane, 2026-07-31) enforced
overflow ⇒ fail-closed reconnect on the dispatch queue, sized/validated for
TRANSIENT runaways (boot snapshot). Nobody asked whether the pregame surge
exceeds drain for sustained stretches. Before `410e8fb` the queue backlogged
(the 3.4s-median-delay era — laggy but CONNECTED and hearing auctions); we
traded lag for deafness. Note the recording gap defect (flagged 07-31) also
fired today: the `rfqs` tape went dark at 15:58Z, so storm-hour inflow could
not be measured from recorded rows — inflow is inferred as drain + the
measured net accumulation above (consistent with the ~500-650 f/s sustained
comms measurements of 07-14/07-31).

## 2. THE FIX — class-aware overflow policy (nothing hand-set)

```
                       comms WS read loop (every TEXT frame now wire-stamped)
                                        |
              +-------------------------+--------------------------+
              | ORDER-INTEGRITY         | MARKET-DATA              | CONTROL
              | quote_accepted/executed | rfq_created, rfq_deleted | subscribed, error,
              | (mark_priority)         | (mark_sheddable)         | quote_created
              |                         |                          |
   priority lane (own queue)     normal queue, on FULL:      normal queue; if displaced
   overflow => fail-closed        DROP OLDEST market frame    while hunting a sheddable
   disconnect (UNCHANGED,         (ws.shed_market_frames),    head -> _carry, dispatched
   never fired)                   NEVER disconnect            AHEAD of the queue, never lost
```

1. **`WsManager.mark_sheddable("rfq_created","rfq_deleted")`** (comms socket
   only): a full dispatch queue drops the OLDEST market-data frame and stays
   connected. A dropped `rfq_created` = one missed auction (the next arrives
   in seconds); a disconnect = ALL queued auctions + resubscribe cost.
   Metrics `ws.shed_market_frames` + `ws.shed.<type>`; loud aggregated
   `ws_shed_market_frames` log (first shed immediately, then once per the
   existing `max_silence_s` window — no new number). Control frames are never
   shed (carry lane); a full queue with NOTHING sheddable keeps the original
   fail-closed disconnect (genuine runaway). The **book socket never calls
   `mark_sheddable`** — orderbook deltas are seq-dependent; shedding one
   corrupts the mirror silently; its behavior is byte-identical.
2. **Priority-lane guarantees UNTOUCHED**: accept/executed keep their own
   lane; only THE LANE's own overflow fail-closes (it never has). Bonus fix:
   an accept arriving while the normal queue was full used to DISCONNECT (the
   wake sentinel's enqueue raised QueueFull → close → the just-won accept was
   discarded with the backlog). Now the sentinel is skipped — a full queue
   proves the dispatcher is busy and will drain the lane next iteration
   anyway.
3. **Wire-age pre-parse gate at intake** (the "drop rfq_created pre-parse"
   lever, implemented from the measured shape): the read loop now stamps
   EVERY frame with its wire-receive monotonic instant (reusing the already
   -taken clock read — zero extra calls); `RfqIntake` drops any `rfq_created`
   older than the quote-freshness horizon BEFORE `Rfq.from_ws` (metric
   `rfq.dropped_stale_preparse`). Horizon = `RFQ_MAX_QUEUE_DWELL_S` (1.5s),
   the SAME constant the worker-side dwell gate already enforces (hoisted to
   module level so the two gates cannot drift) — the dwell clock previously
   started only at intake enqueue, so dispatch-backlog age (30-60s deep
   during the surge) was invisible and every dead frame was parsed at full
   cost. Fail-safe: missing stamp / no horizon ⇒ byte-identical (observe
   mode, tests, replays); `rfq_deleted` is never age-dropped (mirror
   consistency is cheap and ageless).

**Queue bound derivation:** `_QUEUE_MAX` stays 20,000 (its boot-snapshot
derivation is unchanged and still load-bearing for lossless absorption of
transient bursts). The measured tape shows the surge cannot "fit" any bound
(sustained inflow>drain for 62 min), so beyond the bound the market class
sheds gracefully — and in practice the wire-age gate regulates the queue far
below the bound: frames older than the freshness horizon drain at
subtraction cost, so the backlog collapses to its fresh tail instead of
grinding a 40-60s-deep queue of dead auctions.

## 3. PROOF (real `WsManager` + real `RfqIntake`, real tape payloads)

Replay of today's worst measured cycle shape — inflow = drain + 268 f/s
(the 16:53Z cycle), production drain modelled at ~500 f/s via a calibrated
per-RFQ busy cost (parse alone measured 29µs/frame; the live ceiling is loop
contention), accepts injected every 10s mid-surge:

```
[OLD/failclosed] inflow 768 f/s, 90s:  close_calls=1 closed_at=76.1s overflow=1
    (live tape's worst cycle: 74.5s — the defect reproduces to within 2%)
    accept wire->handler ms: 26.1 86.2 116.7 174.7 — then the disconnect discards everything
[NEW/age+shed]   inflow 768 f/s, 90s:  close_calls=0 overflow=0
    stale_preparse=24,423 shed_market=0 (age gate regulates below the bound)
    fanned in final 10s: 5,357 -> intake ALIVE + FRESH (every fanned frame <=1.5s wire age)
    accept wire->handler ms: 9.5 29.8 7.6 7.3 7.2 8.1 7.3 9.4  max 29.8  (<= 1 dispatch wait)
[NEW/shed-only]  inflow 768 f/s, 90s:  close_calls=0 overflow=0
    shed_market=2,040 + loud ws_shed_market_frames log; connected throughout
    (the WS-level backstop proven independently of the age gate)
```

**Confirm-priority S-proofs re-run (must survive):** the full
`tests/test_confirm_priority.py` (lane jumps backlog, ≤1 normal dispatch of
wait, FIFO within lane, wake-on-idle, unmarked types byte-identical,
priority overflow fail-closed, discard drains both lanes, gate hold/bounds,
intake hold, mid-storm relative replay) + `tests/test_confirm_anchor.py` —
**43/43 green** together with the new property tests. Mid-surge accept
latency measured ABOVE: 7-30ms wire→handler vs the 3.0s window. 0 expiries
guarantee intact — and strengthened: the old code DISCONNECTED on an accept
arriving at a full queue (sentinel overflow); the new code banks it.

**Property tests** (`tests/test_ws_market_shed.py`, 15 new): market overflow
sheds oldest + never disconnects; survivors keep FIFO; order-class frames
NEVER shed (mutually-exclusive marking enforced by `ValueError`); priority
-lane overflow still fail-closes; accept-at-full-queue no longer disconnects;
control frames displaced to carry, dispatched ahead, never lost; nothing
-sheddable runaway still fail-closes; unmarked manager (book socket) byte
-identical; `_discard_queued` clears carry; every frame wire-stamped; age
gate drops stale pre-parse / passes fresh / fail-safe on missing stamp / no
horizon / never on `rfq_deleted`.

**Throughput (never regresses):** dispatcher no-op drain median
OLD 1,181,223 f/s → NEW 1,274,088 f/s (no regression; +7.9% within host
noise). Real-parse pipeline OLD 34,684 f/s → NEW 41,344 f/s. Per-frame adds:
one dict store (wire stamp) + one falsy-deque check (carry guard) — the same
O(1) pattern as the 07-31 `empty()` guard.

**Suite + vitals, tails verbatim:**

```
$ pytest tests/ -q                       (worktree, PYTHONPATH=src)
3549 passed, 3 deselected in 259.62s (0:04:19)

$ python -m tools.vitals.gate            (VITALS_DATA_DIR=D:/kct-vdata)
  8/8 vital signs GREEN   (GATE PASS)   total 22.9s

$ python -m tools.vitals.gate --tier pre-ship
  1/1 vital signs GREEN   (GATE PASS)   total 12.0s
```

(Pre-ship tier already run above even though the change loads at restart —
nothing further owed at arming beyond the checklist watch items.)

Self-containment proof (fresh scratch worktree at the pushed SHA, no local
state): SELFCONTAIN_PLACEHOLDER

## 4. Blast radius

`exchange/ws.py` (overflow policy + stamp), `rfq/intake.py` (pre-parse age
gate), `ops/quote_app.py` (wiring: `mark_sheddable` on the comms socket,
horizon into the intake, one constant hoisted). Pricing, fair, risk models,
book socket, REST paths: untouched. The confirm-priority mechanisms are
extended (sentinel fix), never weakened — all their tests re-run green.

## 5. What this does NOT fix (named)

- The `rfqs` tape recording gap fired again today (dark from 15:58Z) —
  separate defect, still owed to the persistence owner. It also blocked
  direct storm-inflow measurement (worked around via cycle net-accumulation).
- Loop contention is still the drain ceiling (~500-650 f/s real). Under
  surge the bot now hears the freshest ~drain-rate of auctions and sheds the
  rest with counters — full-throughput intake during surges is a capacity
  question (parse offload / worker budget), not a correctness one.
- The shed of `rfq_deleted` can leave an id in `intake.open_rfqs` briefly
  (drop-oldest makes this rare; a wasted POST on a dead RFQ at worst, the
  exchange refuses it; the registry is a liveness view, not a risk input).

## 6. RESTART CHECKLIST (operator — the fix loads at the next restart; FAST is right: the bot is deaf during its filling window)

1. `git pull` on the deployment checkout → branch `ws-marketdata-shed`
   (or merge to main first if preferred — the branch is at origin).
2. The usual STOP_BOT → START_BOT sequence. No config changes needed; no new
   knobs exist.
3. First-hour watch: `ws.shed_market_frames` + `rfq.dropped_stale_preparse`
   nonzero DURING surges with **zero** `ws_dispatch_queue_overflow` /
   `ws_disconnected` on the comms socket; `rfq.created` steadily nonzero
   (intake alive); `confirm.accept_dispatch_delay_ms` p99 still ≪ 500ms;
   ZERO `confirm_failed`.
4. If anything looks wrong: the change is one commit — revert restores the
   exact prior (fail-closed) behavior.

## NEXT STEPS

- **Operator**: restart to load (checklist above) — every minute of surge
  before restart is missed filling window.
- **Owed elsewhere**: rfqs recording gap (fired again today, 15:58Z);
  loop-contention drain ceiling as a capacity workstream; the 07-31 open
  decisions (confirm-failure counter semantics, C1/C3/C5) unchanged.
- **Next session**: wire `ws.shed_market_frames` + `rfq.dropped_stale_preparse`
  into the periodic report line; consider a vitals check replaying the surge
  shape (candidate for `prove.py`'s historical-defect set).
