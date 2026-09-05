# 2026-09-05 — BUILD: communications FAN-OUT SHARDING (the fills lever)

Branch `build/ws-fanout-sharding` (worktree `C:/Users/aahys/kct-ws-shard`; bot LIVE on
main `f55fa43` throughout — store read-only, logs grep/tail only, every heavy job at LOW
priority, no process touched). Blast radius: the communications TRANSPORT only.

## WRONG / FIXED / OPEN

| | Item | Status |
|---|---|---|
| WRONG | After the reader-isolation merge the 17:59 ET boot had **0** exchange-side `Subscription buffer overflow` drops in 11 min (was ~1/min) — and **both accepts still expired** (`confirm_expired_by_exchange`: `accept_to_confirm_ms` 546.8 / 666.0, `dispatch_delay_ms` 8.4 / 136.8, `confirm_rtt_ms` 60.5 / 55.4 vs the 3,000 ms window). The accept was late BEFORE it reached the process. | Root cause named: one unsharded connection's drain. |
| WRONG | Exchange pipe lag (rfq `created_ts` → our wire stamp) on this boot p50 6.4 s / p90 9.0 s / 85 % > 3 s (8/26 p50 1.5 s; overnight 9/5 2.2 s). Inbound on the single `communications` subscription ≈ **1,100-1,340 frames/s** (Saturday; the `seq` on the code-25 error frames of the 15:00 boot: 72,694 frames in the 60.5 s between two overflows = 1,200/s; 114,410 / 171,800 / 52,004 on the neighbours). The 20,000-frame market lane hit capacity **26 times in 18 min** on the 17:59 boot, shedding up to 10,752 frames per 30 s line (`ws_shed_market_frames` 22:16:12Z: 5,490 rfq_created + 5,262 rfq_deleted, depth 20,000). | Measured, cited below. |
| FIXED | **`CommsFanout`** (`exchange/ws_fanout.py`): N communications sockets — one `WsManager` FOLLOWER per exchange `shard_key` (own reader thread + socket, `dispatch=False`) — all pushing into ONE shared `_Lanes` drained by ONE dispatcher on the host. Subscribe params per shard `{"channels":["communications"],"shard_factor":N,"shard_key":k}`; **N = 1 puts exactly today's bytes on the wire** (no shard params). Priority lane drained first across all sockets; market capacity/age shedding unchanged (the host's, once); wire stamps preserved; frames stamped `_shard`/`_gen`. | Built + 23 tests. |
| FIXED | **Recovery rule (exact):** a terminal channel error (10/17/25) on ONE shard → that shard's `force_reconnect` only, its queued MARKET frames purged from the shared lane (`_Lanes.purge_market`), priority/control kept, frame NOT forwarded → **no `cancel_all`**; when EVERY shard is in the lost state at once (terminal error reported, subscribe not yet re-acked) the frame IS forwarded → intake `on_channel_lost` → the app's `cancel_all` + `force_reconnect(all)` (today's rule; N = 1 ⇒ byte-identical to today). Consumer `on_disconnect` (the intake's registry reset) fires ONCE per loss epoch (coalesced until a shard re-connects) so an N-way reconnect cannot rotate the intake's two stale-liveness generations N× and mislabel mid-pipeline RFQs. | Tested (single-shard, whole-channel, N=1, coalescing). |
| FIXED | **Sharding refused → unsharded, loudly:** codes 19-22 (documented validation table) and 11 when answering a SHARDED subscribe → `ws_fanout_sharding_refused` WARNING + metric, sticky for the boot, fall back to the single unsharded subscription — never to no subscription. | Tested ×4 codes. |
| FIXED | **N DERIVED, no yaml number:** `N = clamp(ceil(Q_hi(inbound_fps) × HEADROOM / Q_lo(capacity_fps_per_conn)), 1, 100)`. Evidence per ~60 s window per shard (reader-thread counts, never a log line): frames read, handling wall time (receive stamp → lane push), pipe lag of every `rfq_created`. `capacity_k = fps_k` when the shard's rfq pipe-lag p50 > the 3,000 ms window (at that rate it demonstrably was not keeping up), else `fps_k / utilisation_k`. Windows holding a (re)subscribe re-dump (its own + the next) are excluded. Pooled over retained boots (`ws_fanout_tape.json`; retention = the oldest `live_*.log`, the gap tape's rule). Anchors: `Q_hi`/`Q_lo` at the policy z ladder's DAILY rung (z = 3 — N is re-derived per boot, an intra-day unit; `confirm_expired_rate.py`'s argument; surge quantile not mean = the 8/1 lesson); HEADROOM = the stall wall's / hang watchdog's `_MARGIN` 2.0 (the codebase's one existing measured-×-margin rule; covers the estimator's known optimism — handling time excludes aiohttp's TLS/WS parse). Pinned by `test_anchors_are_the_existing_policy_constants`. | Built + tested; today's numbers ⇒ **N = 3** (below). |
| FIXED | **Bootstrap / apply policy:** empty tape ⇒ N = 1 (`source=bootstrap`, today's subscribe); windows 1-2 are snapshot windows; window 3 derives. GROWTH applied LIVE (fail-safe and urgent); SHRINK deferred to the next boot (`deferred_shrink=true` logged). `endpoints.comms_shard_factor_override` exists only as a logged `source=override`. | Tested (governor). |
| FIXED | **Pipe-lag telemetry:** `ws_inbound_rate` (total + per-shard fps, utilisation, shed_lost, depths) and `ws_pipe_lag` (rfq_created + quote_created p50/p90/max/share > 3,000 ms) per window; alarm `pipe_lag_exceeds_confirm_window` WARNING + metric `ws.pipe_lag.exceeds_confirm_window` when rfq p50 > 3,000 ms outside a snapshot window. `quote_accepted` carries NO server timestamp (SUMMARY.md:24) — the quote-event-path proxy is `quote_created` (our own quote acks, server `created_ts`). `ws_fanout_derivation` at boot and every refresh. | Built + tested. |
| FIXED | **ws.py robustness (found by the fake exchange):** the pending subscribe ack was registered AFTER `await send_command(...)`; an ack arriving within one loop hop was dispatched before the registration and dropped (`on_subscribed` never fired). Live RTT hid it. Now reserved BEFORE the send (`_reserve_sub_ack`), popped on failure. Also: `_dispatch` split into `_dispatch_control` + handlers (byte-identical default); `on_subscribe_error` correlation by command id. | Existing 58 ws tests unchanged and green. |
| OPEN | Live verification (the relight checklist below). Kalshi's routing of RFQs to shard keys and whether our quote events follow the RFQ's shard are moot when we hold ALL keys — but **must be observed**: every shard's `ws_shard_subscribed`, `quote_accepted` frames landing (any shard), N sharded connections accepted (no max-connection limit is documented). | Relight. |
| OPEN | The estimator's disclosed failure mode: pipe lag above the window at N shards for a cause sharding cannot fix ⇒ violating windows keep `capacity = read rate` ⇒ N ≈ ×2 per boot toward the cap with the alarm firing throughout. Visible; bounded by 100. | Watch `ws_fanout_derivation.source/n_violating`. |
| OPEN | Reader-side raw-string pre-filter (skip `json.loads` for non-allowlisted series) would cut per-frame handling ~10× on the firehose; out of this build's scope (transport fan-out). | Follow-up. |
| OPEN | `ruff format` wants changes on `ops/config.py` and `ops/quote_app.py` on MAIN already (pre-existing); NOT reformatted here (980-line noise). mypy: 4 pre-existing errors in `pricing/engine.py` on both trees; every touched file clean. | Pre-existing debt. |

## Measured rates (this build's evidence, cite → verify)

| Quantity | Value | Source |
|---|---|---|
| Inbound frames/s, one unsharded connection | ~1,200 (72,694 frames / 60.5 s); neighbours 1,100-1,340 | `ws_server_error` code-25 envelopes, `live_20260905_1500.log` 19:02:19Z→19:03:19Z (`seq` 114,410 / 72,694 / 171,800 / 52,004) |
| Exchange-side overflow drops, 17:59 boot | **0** in 20 min (was ~1/min) | `grep -c ws_subscription_buffer_overflow live_20260905_1759.log` = 0 |
| Market-lane shed lines, 17:59 boot | 26 in 22:01:45Z-22:19:43Z; depth 20,000 on 9 of them; peak 10,752 frames/30 s | `ws_shed_market_frames` timeline |
| Expired accepts, 17:59 boot | 2/2: `accept_to_confirm_ms` 546.8 / 666.0 (`dispatch_delay_ms` 8.4 / 136.8; `confirm_rtt_ms` 60.5 / 55.4) | `confirm_expired_by_exchange` |
| Loop lag, 17:59 boot | p50 134-268 ms, max 450-687 ms per 15 s window | `event_loop_lag` |
| Pipe lag (created_ts → seen_at), 17:59 boot | per minute: 21:59Z p50 1.18 s (first minute, 0 % > 3 s) → 22:00Z 3.35 s (59 %) → 22:01Z 4.29 s (68 %) → **22:02Z 6.42 s / p90 8.58 s (88 %)** → 22:03Z 6.01 s (73 %); the 15:00 boot's minutes 18:52-19:06Z ran p50 5.65-23.4 s, p90 up to 44.5 s, 64-100 % > 3 s (the code-25 storm) | my read-only store read (`mode=ro`, last 40,000 `rfqs` rows by rowid, `raw_json.created_ts` vs `seen_at`; matches the investigator's p50 6.4 s). `seen_at` is intake-side so this is an UPPER bound on wire lag (in-process dwell ≤ the 1.5 s pre-parse gate + queue); the new `ws_pipe_lag` line measures at the wire stamp. Note the store tape holds no rfqs rows after 22:03:34Z on this boot (writer starvation — the known store issue, not this build's) |

## The derivation, walked with today's numbers

```
inbound windows (unsharded, all violating): 1,200 / 1,300 / 1,340 fps
capacity per connection = read rate (violating):  1,150 / 1,200 / 1,100 fps
Q_hi(inbound) @ z=3, n=3 → the max bucket edge ≈ 1,340-1,353 fps
Q_lo(capacity) @ z=3, n=3 → the min = 1,100 fps
N = ceil(1,353 × 2.0 / 1,100) = 3            (test_derive_shard_factor_from_todays_measured_rates)
```

At N = 3 each connection carries ~430 fps; a healthy window then contributes
`capacity = fps / utilisation` (utilisation measured from the reader's own handling time),
while the pooled unsharded violating windows keep the lower quantile at ~1,100 for the
retention period — N holds at 3 until the evidence says otherwise.

```
 Kalshi communications channel (global RFQ firehose, ~1,200 frames/s)
        │ shard_factor=N, shard_key=k  (one subscribe per socket, k = 0..N-1)
   ┌────┴────┐    ┌─────────┐          ┌─────────┐
   │ s0 sock │    │ s1 sock │   ...    │ sN-1    │   ← WsManager followers: own reader
   │ reader  │    │ reader  │          │ reader  │     thread each; stamps _recv_mono_ns,
   └────┬────┘    └────┬────┘          └────┬────┘     _shard, _gen; meter (fps, busy, lag)
        └──────────────┴───────────┬────────┘
                                   ▼
                     ONE shared _Lanes  [PRIORITY | CONTROL | MARKET(cap 20k, age-shed)]
                                   ▼
                     ONE dispatcher (CommsFanout host, main loop)
                       ├─ subscribed/error → owning shard (by _shard, _gen)
                       ├─ terminal 10/17/25: one shard → reconnect it, keep the book;
                       │                      all shards → forward → cancel_all (today)
                       └─ handlers: RfqIntake (unchanged interface)
   FanoutGovernor (~60 s): take_windows → ws_inbound_rate / ws_pipe_lag (+alarm)
                           → ws_fanout_tape.json (off-loop) → derive N → grow live
```

## Gates

| Gate | Result |
|---|---|
| Existing ws tests (`test_ws_manager`, `test_ws_market_shed`, `test_ws_reader_isolation`, `test_containment_windows`) | **58/58 unchanged, green** against the worktree (`PYTHONPATH=<worktree>/src` — the venv's editable install points at main; the first run silently tested main until this was set) |
| New `tests/test_ws_fanout.py` | **23/23** (fake sharded exchange: exactly-once across 3 shards + priority-first; N=1 wire bytes; single-shard 25 → that shard only, not forwarded; whole-channel 17 → forwarded once, on_disconnect coalesced; N=1 forwarded as today; socket death purges only its market frames; codes 19/20/21/22 → unsharded fallback; derivation floor/cap/bootstrap/override/refused; anchors pinned; rate histogram JSON; tape fold/prune/pool; meter lag/utilisation/snapshot; governor alarm → live growth → deferred shrink → next-boot derivation from the tape; governor never raises; retired-generation control frames dropped; single-manager subscribe-error hook; health aggregation) |
| Full suite (LOW priority, worktree) | **4,212 passed / 0 failed / 3 deselected in 334 s** (`pytest -q` at LOW priority, `PYTHONPATH=<worktree>/src`; main's stated baseline 4,188 + 23 new here) |
| Vitals fast tier (from the snapshot `vitals_snap2`, taken 21:54Z) | **8/8 GREEN (GATE PASS, 94.8 s)** — `VITALS_DATA_DIR=<scratch>/vitals_snap2 python -m tools.vitals.gate`, LOW priority, worktree PYTHONPATH |
| ruff check | clean on every touched/new file |
| mypy (strict) | clean on `ws.py`, `ws_fanout.py`, `config.py`; `quote_app.py` reports only the 4 pre-existing `pricing/engine.py` errors (identical on main) |

## Blast radius

`exchange/ws.py` (additive follower/host kwargs, all defaulting to today's behaviour; ack reservation; `_dispatch` split), new `exchange/ws_fanout.py`, `ops/quote_app.py` (constructs `CommsFanout` + `FanoutGovernor`; one boot tick before `ws.start()`, one refresh tick on the existing ~60 s cadence), `ops/config.py` (the override key). The intake, the book socket, pricing, risk, the confirm path and quoting throughput are untouched; the observe app keeps the plain `WsManager`.

## Relight verification checklist (how the fix is proven)

1. Boot: `ws_fanout_derivation reason=boot` — first boot ever shows `source=bootstrap shard_factor_applied=1`; a boot with a tape shows `source=measured` and the pooled numbers.
2. `ws_fanout_started shard_factor=N` then one `ws_shard_subscribed shard=k sid=…` per shard, no `ws_fanout_sharding_refused`, no `ws_shard_subscribe_error`.
3. Every minute: `ws_inbound_rate total_fps≈1,200 per_shard_fps≈1,200/N`; `ws_pipe_lag rfq_created.p50_ms` **< 3,000** (today 6,400) and `share_over_window` → ~0; `pipe_lag_exceeds_confirm_window` absent after the two snapshot windows.
4. `ws_shed_market_frames` **0** lines; `ws_subscription_buffer_overflow` **0**; `ws_dispatch_queue_overflow` **0**.
5. `confirm_expired_by_exchange` rate back to the overnight baseline **~2-5 %** of accepts (2/2 on the 17:59 boot); `confirm_expired_baseline` unchanged.
6. Throughput never regresses: sends/min ≥ the 295-398 of the 17:59 boot; `event_loop_lag` no worse.
7. A single-shard `ws_shard_channel_lost` (if any) must show `shards_lost=[k]`, a `ws_connected name=ws.s<k>` follow-up, and **no** `cancel_all`; `ws_fanout_channel_lost_all` only with a whole-channel outage.

## NEXT STEPS

- **Operator:** merge `build/ws-fanout-sharding` into main and relight (WMI-detached START_BOT per the resume state); watch the checklist above for the first 10 minutes; the first boot runs unsharded for two windows then re-shards live (expect `ws_fanout_resharding … reason=refresh:measured`).
- **Me, at relight:** confirm N sharded connections are accepted by the exchange (no documented connection cap); confirm our `quote_accepted` frames arrive on the shard set; record the first `ws_pipe_lag` lines in the resume state; if `ws_fanout_sharding_refused` fires, the docs (asyncapi-ws.md §3.2) are wrong about something — investigate before re-arming.
- **Follow-ups (owed, not blocking):** reader-side raw-string pre-filter before `json.loads`; add `thread_time` CPU utilisation to the capacity estimator once measured live; update `docs/api-notes/SUMMARY.md` open questions with the observed routing.
