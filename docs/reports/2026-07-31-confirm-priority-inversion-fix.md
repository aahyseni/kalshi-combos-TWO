# 2026-07-31 — CONFIRM PRIORITY INVERSION: measured, named, fixed by mechanism

**Status: SHIPPED in tree (loads at the operator's ONE approved restart tonight). Suite green (tail below); vitals fast 8/8 GREEN 20.1s + pre-ship 1/1 GREEN 10.1s. Kill-switch rule deliberately UNCHANGED (rationale below).**

Both of today's kill-switch halts — **16:27:43Z** and **21:33:17Z**, each
`halt_confirm_timeouts` / "3 consecutive confirm failures", the first eating the
entire 15-game slate — were the same defect: we **won** auctions and confirmed
into a dead window (`HTTP 400 {'code': 'expired'}`). This report measures where
the exchange's 3.0s confirm window actually died, names the eater with numbers,
and documents the mechanism-level fix (no knob was turned; every bound derives
from the exchange window or a measured latency).

---

## 1. MEASUREMENT — where the 3.0s died

Sources: every `live_*.log` on the data drive (17GB, all runs ever) + the
`rfqs` tape (exchange-side `created_ts` vs our worker-pickup `seen_at`,
`source='ws'` only). Scripts ran read-only; the live bot was untouched.

### 1a. Every confirm failure ever is 'expired', and the handler was never the eater

12 `confirm_failed` events exist on the entire tape (vs 1,008 accepts, 430
`confirm_ok`). **All 12 are `HTTP 400 'expired'`** — zero network errors, zero
429s, zero 5xx. In-handler time (accept-log → confirm_failed, which INCLUDES
last look, fill-velocity, reservation, candidate gate AND the REST round trip):

| when (Z) | quote | in-handler | implied pre-handler loss |
|---|---|---|---|
| 07-25 20:39:46 | dca758ed | 0.964s | ≥ 2.04s |
| 07-25 21:49:59 | 740831b5 | 1.138s | ≥ 1.86s |
| 07-29 11:22:45 | 5917ab90 | 0.410s | ≥ 2.59s |
| 07-29 11:26:44 | eb10a9cd | 0.598s | ≥ 2.40s |
| 07-31 12:35:09 | 9b5bc5cd | 0.436s | ≥ 2.56s |
| 07-31 12:41:52 | 30e4eaf3 | 0.594s | ≥ 2.41s |
| 07-31 13:17:46 | b61b47f8 | 0.527s | ≥ 2.47s |
| 07-31 13:21:15 | ff777eca | 0.340s | ≥ 2.66s |
| 07-31 16:27:43 | 23a18875 | 0.366s | ≥ 2.63s | ← halt #1 (3rd consecutive)
| 07-31 21:27:43 | d4ce0d2a | 0.424s | ≥ 2.58s |
| 07-31 21:32:41 | ae4cf35b | 0.533s | ≥ 2.47s |
| 07-31 21:33:17 | fb4d7a0d | 0.333s | ≥ 2.67s | ← halt #2 (3rd consecutive)

Every failure spent **≤ 1.14s** inside the handler. Since the exchange said
'expired' (3.0s), **≥ 1.86–2.67s of every failed window died BEFORE the
handler's first instruction** — upstream of the accept handler, in delivery.

For scale, the 430 successful confirms' in-handler distribution (accept-log →
`risk_audit phase=confirm`): **p50 0.72s / p90 1.46s / p99 1.90s** (max 11.3s —
one outlier). The in-handler path fits the window with ≥1s to spare at p99. The
Phase-5 "117ms of 3s" idle measurement was true and irrelevant: the path was
never slow, the *delivery* was.

### 1b. The eater, named: FIFO head-of-line blocking on the comms WS dispatch queue

The accept frame rides the SAME single-FIFO dispatch pipeline as the
`rfq_created` firehose (`WsManager._msg_queue`, one dispatcher task, inline
`Rfq.from_ws` parse per allowlisted frame — and on a 15-game MLB slate most of
the firehose IS allowlisted). Direct same-socket measurement — exchange
`created_ts` → worker pickup `seen_at` for every ws-sourced RFQ (recorded rows
exclude >1.5s queue-dwell skips, so these UNDERSTATE):

| window | n | p50 | p90 | frac > 2.5s |
|---|---|---|---|---|
| 07-29 02Z (healthy floor, 117 rfq/s recorded) | 422,730 | **0.45s** | 0.49s | 0.0% |
| 07-31 13Z — run 1, halt #1 | 50,718 | **3.40s** | 6.21s | **69.0%** |
| 07-31 21:24–29Z — run 2, halt #2 | 13,072 | 1.85s | 3.16s | 25.4% |
| 07-31 21:5xZ — run 3 (live tonight) | 4,066 | 1.92s | 4.80s | 41.7% |
| 07-31 10Z — morning run | 48,669 | **15.89s** | 24.9s | 82.4% |

The healthy floor is 0.45s (network + venue emit + parse + a drained queue).
During today's storms the same pipe ran a **median 3.4s behind the wire — the
whole confirm window — with p90 6.2s**, exactly matching the ≥1.9–2.7s
pre-handler losses inferred independently from the 'expired' failures.
`rfq.evicted_oldest_for_fresh` 807,534 in one run confirms inflow ≫ drain.

Ruled out with numbers: **REST confirm call** (unpaced direct PUT, no token
budget on that verb; included in the ≤1.14s), **the confirm-path risk check**
(in-handler p99 1.9s < window, and its budget is already derived+bounded),
**write-budget/429 backoff** (zero 429s on any confirm ever), **quote-event
lane** (single dedicated worker, accepts are tens/day). The eater is delivery:
the dispatch backlog, with event-loop contention from 8 pricing workers as the
second-order term.

### 1c. Why the derived in-handler budget couldn't save it

`_candidate_gate_budget_ns` anchors the window at **handler start** (`t0`). The
budget honestly deducted everything AFTER t0 — and was structurally blind to
the 2.5s the exchange had already counted before t0. A perfect in-handler
budget spent inside a window that was already dead.

---

## 2. FIX — by mechanism, nothing hand-set

Four mechanisms; every bound is the exchange window (protocol fact,
`EXCHANGE_CONFIRM_WINDOW_S`) or a measured latency. Blast radius: the
accept/confirm delivery + handling path only (`exchange/ws.py`,
`rfq/intake.py`, `ops/quote_app.py` wiring, `rfq/lifecycle.py` accept anchor).
Pricing/fair/risk models untouched.

1. **WS priority lane** (`WsManager.mark_priority("quote_accepted",
   "quote_executed")`): accept/executed frames route to their own queue which
   the dispatcher fully drains before every normal dispatch — an accept now
   waits for at most ONE normal handler run, never the backlog. A wake sentinel
   covers the idle-dispatcher case. FIFO preserved within each lane (accept →
   executed order); the marked types carry no seq dependency on the rfq
   stream (they key on our own `quote_id`, populated by our REST create ack,
   not by any queued frame). Queue bound = the existing `_QUEUE_MAX` with the
   same overflow⇒reconnect fail-closed semantics — no new number. The book
   socket is untouched (last look needs its freshness most mid-confirm).
2. **Confirm preempts quoting** (`AcceptPriorityGate`): from the moment an
   accept is SEEN (enqueue, not worker pickup) until its confirm handling ends,
   new quote work parks — intake drops `rfq_created` pre-parse
   (`rfq.dropped_accept_priority`), the 8 rfq workers and the retry loop wait.
   Release = the worker's `finally` (every outcome); fail-safe bound =
   `EXCHANGE_CONFIRM_WINDOW_S`, past which the exchange has voided the confirm
   anyway, so holding longer protects nothing. **Named cost** (operator
   priority: a banked win beats any number of reprices): quoting pauses for
   the in-handler time — measured p50 0.53s, max 1.14s — at tens of accepts a
   day ⇒ ≲ 1 minute of paused quoting per day, ~0.07% of quoting time.
3. **Honest deadline anchor**: the read loop stamps priority frames with the
   monotonic instant they leave the socket; intake passes the stamp through;
   `_on_quote_accepted` anchors its derived budget at the stamp (guarded to
   [0, now] so a foreign stamp can only shrink the window's view, never grow
   it) and measures `confirm.accept_dispatch_delay_ms`. Any FUTURE delivery
   regression now auto-deducts from the gate budget — the path degrades toward
   its deterministic fallback (fail-safe) instead of confirming into a dead
   window.
4. **Fast-lane create race** (new race the lane surfaces): an accept can now
   beat our own create POST's response parse, in which case `_open` has no
   entry yet. The handler waits exactly the measured p99 of the same REST verb
   (`quote.create_rtt_ms`) once, then re-reads; cold series ⇒ no wait
   (pre-lane behavior). Metric: `confirm.accept_beat_create_ack`.

Also: `confirm.lane_wait_ms.*` metrics split lane wait from WS delivery — the
measurement gap that forced this report to infer pre-handler loss from
'expired' arithmetic is now instrumented directly.

## 3. Kill switch: deliberately UNCHANGED

Task item (3) asked whether the 3-consecutive rule should distinguish
'expired' from transient network errors. **No change**, for three reasons:
(a) the tape shows the distinction is empty — 12 of 12 failures ever are
'expired'; there is no observed network-error population to treat differently;
(b) ANY confirm failure leaves an unknown-committed position (the handler
already marks the reservation unconfirmed and holds headroom) — three in a row
means we are reneging on won auctions at scale, and stopping is RIGHT
regardless of cause; (c) any split would only ever weaken the trigger. The fix
attacks the cause (delivery), not the alarm.

**ADDENDUM (same-day second pass, from the tape): "consecutive" is actually
CUMULATIVE-per-run.** `_confirm_failures` (lifecycle.py:1631) is incremented on
every failure and **never reset on a successful confirm**. The 16:27Z halt's
three "consecutive" failures were 13:17:46 (b61b47f8), 13:21:15 (ff777eca) and
16:27:43 (23a18875) — with two clean CONFIRM+EXECUTE round trips in between
(e8749b71 13:18, fc31718f 13:27), 3h10m apart. The halt message text ("3
consecutive confirm failures") does not describe the implemented rule. Left
UNCHANGED here — making it truly consecutive (reset on success) is a LOOSENING
and needs an operator ruling; the cumulative rule is the stricter posture and
with the delivery fix in place genuinely-consecutive failures still trip it
identically. **Operator decision owed**: keep cumulative-per-run (strictest),
or reset on a successful confirm so the counter matches its own message.

## 4. PROOF

**Mid-storm replay** (real `WsManager`, HEAD vs working tree; 5,000 firehose
frames × 1ms parse cost queued AHEAD of the accept — worst case; scratchpad
bench, same interpreter, same machine):

```
MID-STORM accept latency (5000 x 1.0ms-parse frames queued ahead):
  BEFORE (HEAD, single FIFO): accept dispatched after    5150.2 ms  (storm drain   5.15s)
  AFTER  (priority lane):     accept dispatched after       0.8 ms  (storm drain   5.19s)
DISPATCHER DRAIN RATE (20k no-op frames, median of 3):
  BEFORE (HEAD):                        664139 frames/s
  AFTER  (lane marked, no accepts):     646895 frames/s  (-2.6%)
  AFTER  (lane unmarked):               688127 frames/s  (+3.6%)
```

Before: the accept waited out the entire 5.15s backlog — 1.7× the whole
exchange window, precisely today's failure mode. After: **0.8ms**, then the
measured in-handler p99 1.90s + the honest anchor leaves ≥1s of window margin.
**Quoting throughput cost, named**: −2.6% per-frame dispatcher overhead on
no-op frames = +0.06µs/frame; at the real ~500-650 frames/s with ~1ms parse
cost per allowlisted frame this is ~0.004% — plus the ≲1 min/day preemption
pauses from mechanism 2. Both accepted under the operator's stated priority
(the confirm path outranks quoting throughput). An earlier draft paid −41%
here (a per-frame QueueEmpty raise); an `empty()` guard recovered it — that
overhead was measured, named, and eliminated, not shrugged off.

**Unit proofs** (all in-tree): `tests/test_confirm_priority.py` — lane jumps a
queued backlog (at most one normal dispatch of wait), FIFO within lane, wake
on idle, unmarked types byte-identical, overflow fails closed, discard drains
both lanes; gate holds from enqueue→done, overlapping accepts, fail-safe bound
at the window, re-anchoring, no underflow; intake drops `rfq_created` (and
ONLY that) while holding; relative mid-storm assertion (lane < 1/10 of
no-lane). `tests/test_confirm_anchor.py` — budget anchored at the wire stamp
(2.0s simulated queue delay lands in `confirm.accept_dispatch_delay_ms` and in
the decision clock), stampless/foreign-stamp paths byte-identical to old
behavior, create-race wait confirms the quote (auction banked) with the
measured-p99 wait, truly-unknown quote still lapses with no typed delay.

**Suite + vitals (rule 9 — this IS the confirm path), tails verbatim:**

```
$ .venv/Scripts/python.exe -m pytest tests/ -q
3452 passed, 3 deselected in 275.31s (0:04:35)

$ .venv/Scripts/python.exe -m tools.vitals.gate           (fast tier)
  8/8 vital signs GREEN   (GATE PASS)   total 20.1s

$ .venv/Scripts/python.exe -m tools.vitals.gate --tier pre-ship
  V6   CONFIRM WINDOW — MC fits at the live book:  live 311ms (10% win), build<=144ms  PASS
  1/1 vital signs GREEN   (GATE PASS)   total 10.1s
```

(An earlier mid-run suite pass showed one `test_hang_watchdog.py` failure —
that was the CONCURRENT watchdog workstream renaming a test while the run was
in flight, not this change; the clean rerun above is at the final tree state.)

## 5. What this does NOT fix (named, owned elsewhere)

- The RFQ tape recording gap: run 1's `rfqs` rows stop at 13:22Z while the run
  quoted until 16:27Z — recording, not quoting, went dark (separate defect,
  outside this blast radius; flagged for the persistence owner).
- The morning run's p50 15.9s intake lag means the bot was ALSO quoting stale
  RFQs all morning (freshness is enforced at 1.5s queue-dwell but the WS
  backlog upstream is unbounded in time). The priority lane fixes accepts;
  RFQ-side staleness during storms remains a throughput/capacity question
  (rfq workers, parse cost), not a correctness one — the dwell gate already
  refuses stale pricing.
- Reneges from mid-run book-risk staleness (today's other ≈$14 class) — owned
  by FIX 5 / `book_risk_stale_decay` arming decision (07-31 warmup-gate report).

## NEXT STEPS

- **Operator**: the ONE approved restart tonight loads this + the other 7/31
  ships. First-hour watch: `confirm.accept_dispatch_delay_ms` (expect p99 ≪
  500ms), `confirm.lane_wait_ms.quote_accepted` (expect ~0), `ws.priority_frame`
  count ≈ accepts, `rfq.dropped_accept_priority` small (tens per accept burst),
  and ZERO `confirm_failed`. A single 'expired' after this ships = reopen with
  the new split metrics in hand.
- **Owed decision (operator)**: the §3 addendum — the halt counter is
  cumulative-per-run, not consecutive as its message claims; keep the stricter
  cumulative posture or reset on a successful confirm (a loosening, so it is
  yours to call). C1/D1/D2/FIX-5 decisions from the earlier 7/31 reports still
  open.
- **Next session**: wire `confirm.accept_dispatch_delay_ms` into the periodic
  report line; investigate the run-1 rfqs-recording gap (13:22Z→16:27Z dark);
  consider a vitals check that replays the mid-storm lane proof (candidate for
  `prove.py`'s historical-defect set).
