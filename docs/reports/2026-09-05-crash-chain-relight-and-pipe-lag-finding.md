# 2026-09-05 — "constantly restarting + 0 fills": the crash chain, the merge, the 17:59 ET relight, and the PIPE-LAG finding (fan-out sharding = the fix)

Operator (day): "why are the new bot windows opening from time to time, why is
it not one continuous run" → "we settled a bunch of positions freeing up a lot
of space to fill more, but we aren't filling" → "we need to fix the bot
constantly restarting, also we are getting 0 fills … fortify its uptime, THEN
look into how we can fix the fills and still be as competitive" (un-parks
items 6/7). Exchange truth 13:10 ET: cash $2,853.61 + PV $2,115.73 =
$4,969.34, 71 positions, 51 resting quotes; 9/5 fills 115 / $3,135 (2nd-best
day, mostly the overnight boot); settlements +$112.

## WRONG / FIXED / OPEN

| Claim | Verdict | Evidence |
|---|---|---|
| "New windows keep opening" = crashes | **TRUE — 8 self-halts** | `halt_confirm_timeouts` at 04:50Z 13:04Z 13:41Z 15:40Z 16:11Z 18:45Z 18:59Z 20:00Z (`halt_receipt.json`). Each halt → supervisor exit → hang watchdog relight (`start_all.ps1 -Auto`) = a new window, ~2.5 min dark + the resting book swept. |
| Cause of the halts | **FOUND, FIXED** | `lifecycle.py` `_confirm_failures` (init :1856, `+= 1` :6516, halt at ≥3 :6524) was **never reset on a success** and counted `HTTP 400 expired` (the exchange's 3 s confirm window elapsed) as a confirm failure. Three late accepts anywhere in a boot = halt. Fix: `classify_confirm_failure` — `expired` never counts, a success resets (`build/confirm-halt-and-derived-wall`, merged `38ac979`). |
| The 60.5 s "stall kills" | **REFRAMED** | Today's `maintenance age > 60.5 s` kills were the shutdown tails of the confirm halts, not slow passes (investigator). The derived wall (`stall_after_s = wedge_timeout + interval`, 12 bounded store awaits) shipped in **shadow**, floored at 60.5 s; arming = an operator ruling owed. |
| "0 fills" caused by the fee floor | **REFUTED (again)** | Fee floor moved razors only (≤1.9× jointly with the slate); razor fill share ROSE 11%→28–32% after 8/20. Not the driver. |
| The fills driver #1 — socket drops | **FOUND, FIXED** | Exchange closed our `communications` subscription ~1/min with code 25 "Subscription buffer overflow" = WE read too slowly: inbound 1,100–1,340 frames/s (Saturday, from the server seq at overflow) vs one asyncio reader sharing the loop with pricing. Each drop → `force_reconnect` + `on_channel_lost → cancel_all` (82–156 resting quotes) + Kalshi re-dumps every open RFQ. Fix: socket read on a dedicated thread, priority lane (`quote_accepted`/`quote_executed`) drained first, client-side age shedding of `rfq_created`/`rfq_deleted`, loop-lag probe, slow-callback recorder, shadow-telemetry sampling (`build/ws-reader-isolation`, merged `5c4e1c8`). |
| The fills driver #2 — EXCHANGE PIPE LAG | **FOUND, NOT YET FIXED** | After the relight: 0 drops in 11 min, 0 halts, sends 295–398/min, loop lag max 0.25–0.7 s — and BOTH accepts still `confirm_expired_by_exchange` (accept→confirm 547/666 ms on our side; dispatch 8/137 ms; confirm RTT 55–60 ms; window 3,000 ms). The accept frame is already >2.3 s old when it reaches the process. Exchange pipe lag (`created_ts` → our `seen_at`) this boot: **p50 6.4 s, p90 9.0 s, 85% > 3 s** (8/26 p50 1.5 s; overnight 2.2 s). `ws_shed_market_frames`: the 20,000-frame market lane fills and sheds 4,500 `rfq_created`/window; `ws_stale_market_frames` at 22:18Z dropped **21,745** `rfq_created` in one window (oldest age 1.77 s). One unsharded firehose > one connection's drain (the reader thread still shares the GIL with a saturated loop; aiohttp's 128 KB flow-control buffer back-pressures the exchange, which then delivers OUR frames seconds late). |
| Fee "eat" mode | **PARKED** | Only if a per-tier read AFTER the transport fix still shows a floor effect. |

## The merge and the relight

Three builder branches (each: builder in a worktree + adversarial review + fix
pass) merged to main: `38ac979` (confirm-halt classifier + derived wall),
`5c4e1c8` (WS reader isolation), `c25a8e2` (store rotation tool
`tools/ops/rotate_store.py` + dark `tape_retention` prune); `6679ec8` ruff
import order; `f55fa43` re-pinned the tape-retention dark-flag test to
`QuoteApp._run_instrumented` (the ws-reader branch wrapped `run()`). Touched
modules 105/105; full suite 4,188 with the one stale pin fixed. Vitals fast from
the 2-table snapshot (`tools/vitals/snapshot.py`; `derive.py` rescans the whole
213 GB tape on the live box). Relit **17:59 ET** via WMI-detached START_BOT
(`live_20260905_1759.log`).

First window after relight (18:00–18:15 ET):

| Check | Value |
|---|---|
| Subscription buffer overflow (code 25) | **0** (was ~1/min) |
| Confirm halts | **0** (were 8 today) |
| Sends/min | 295–398 |
| Loop lag | p50 134 ms, p99 485 ms, max 0.25–0.7 s |
| Accepts | 2, **both expired by the exchange** (547/666 ms our path) |
| Fills | **0** |
| Market-lane shed (per window) | 4,500 `rfq_created` (lane depth 20,000); stale-drop 21,745 |
| Exchange pipe lag (created_ts → seen_at) | **p50 6.4 s / p90 9.0 s / 85% > 3 s** |

## The fix (building: `build/ws-fanout-sharding`)

Kalshi's own communications fan-out sharding (`docs/api-notes/asyncapi-ws.md:52`:
subscribe params `shard_factor` 1–100 / `shard_key` 0..N−1; validation errors
19–21; `SUMMARY.md:28/195/307`): N connections × N reader threads, shard k
subscribing `{channels:[communications], shard_factor:N, shard_key:k}`, all
feeding the one dispatcher (priority lane preserved). **N is DERIVED** from the
measured inbound frames/s vs the measured sustainable drain per connection
(floor 1, documented cap 100), persisted per boot and re-derived at boot.
Single-shard loss reconnects that shard only — `cancel_all` is reserved for
genuine terminal codes on all shards. Validation-error fallback = today's
unsharded subscription, logged loudly. New telemetry: `pipe_lag` per minute
(p50/p90/max, share > the 3,000 ms confirm window) + derived alarm
`pipe_lag_exceeds_confirm_window`. Relight verification checklist: pipe lag
p50 < 3 s; expired share back to ~2–5%; shed 0; drops 0; fills/h vs the
overnight 14/h.

Operational rule learned today (hard): **never run agents/tests/replays/kill
commands on this box while the bot quotes** — the 13:00–16:00 fleet starved
the reader (drops tracked agent load) and one agent ran `Stop-Process`; the
16:02 ET whole-stack death (watchdog died mid-relight) is still unexplained.
Tonight's fleet runs at low priority with the bot filling nothing anyway.

## FOUND WHILE WAITING: the risk denominator DOUBLE-COUNTED shard 1's portfolio value (repaired `f750924`)

The open item "bot standing $7,308 vs exchange $4,969" resolved in one
same-minute read (22:22Z):

| Read | cash | portfolio value | equity |
|---|---|---|---|
| Bot `account_standing` 22:00Z | $3,510.10 | **$3,155.72** | **$6,665.82** |
| Exchange base `/portfolio/balance` | $3,510.10 | $1,550.55 | $5,060.65 |
| Exchange per shard | idx0 $0.00 · idx1 $1,551.38 · idx2/3 $0 | $1,551.38 | $5,061.48 |

`WholeBookBalanceSource` (the 8/17 "shards = one book" merge) assumed the
base call's `portfolio_value` was shard 0's and added every other shard's on
top. The base call already carries the WHOLE book, so shard 1 (where every
position lives since the 8/17 transfer) was counted twice. The 8/17
verification (idx0 $1,264.23 vs idx1 $4.06) could not tell "base = shard 0"
from "base = total" because shard 1 was a sliver then.

Consequence: `risk_bankroll_cc = cash + haircut·PV` with `portfolio_haircut
1.0` was inflated by the full shard-1 PV — **+$1,550 (+31%) tonight, ≈+$3.5k
(+70%) at the 9/5 morning peak book**, and the start-of-day equity anchor /
KILL line with it (`kill_line_cc` $6,612.70 tonight against a true equity
of $5,061). Every cap that scales from the denominator (per-combo, structure,
entity, CVaR, det-max backstop) was that much looser, and it fed back: more
positions → more PV → a larger double count → larger caps. The 9/5 $3.5k peak
book was partly this. The ruin basis (cash + cost, per P1-3) was unaffected.

Repair (mechanism, no number): merged PV = Σ per-shard scoped reads over
EVERY index in `balance_breakdown` (index 0 included); the base PV is never
added, only logged beside the sum (`whole_book_pv_merge`) so a Kalshi
semantics change surfaces as a divergence. Tests re-pinned to the live
payloads (6/6; test_balance + 3 referencing suites 156/156); ruff clean;
vitals fast 8/8 from the snapshot. Rides the sharding relight (the live boot
still runs the inflated denominator until then; fills ≈ 0 so the exposure to
the loose caps is small — if the fleet runs long, relight earlier).

## 20:20 ET — SHARDING MERGED (`022f083`), BUT THE STACK IS BEING KILLED FROM OUTSIDE THE BOT

Fleet result: builder `806d97b` → adversarial review **SHIP_WITH_FIXES** (one
must-fix: an unlisted error answering a sharded subscribe left a shard
connected-but-unsubscribed with health green; five should-fixes) → fix pass
`e1f6994` applied all of them (any non-terminal sharded-subscribe answer =
loud sticky unsharded fallback; loss epoch closes only when every casualty
re-acks; live re-shard purges the retired generation's queued frames;
cross-shard dedupe of quote events; live growth gated on no accept in flight;
shrink gated on demonstrated sustained throughput). Suite 4,225/0, vitals
fast 8/8 from the snapshot, ruff/mypy clean; fast-forward merged to main and
pushed; post-merge gates on main: 97 touched tests, vitals 8/8. Builder
report: `2026-09-05-build-ws-fanout-sharding.md`.

Then two whole-stack deaths that are NOT the bot:

| Boot | Died | Lifetime | What the logs show |
|---|---|---|---|
| 17:59 ET (`f55fa43`) | **19:00:38 ET** | 61 min | Log stops mid-stream at full rate (11,716 lines in the prior minute); no halt receipt (the last one is 16:00 ET); no watchdog line after "armed 17:59:15"; bot + supervisor + watchdog + probers + monitor windows all gone. Fleet transcripts hold **no kill command** and the only running agent was between tool calls at that second. |
| 20:15 ET (`022f083`) | **~20:15:40 ET** | ~20 s | Log stops after `joint_pool_warm` (store open in progress); watchdog "armed 20:15:35" is its last line; every process gone by 20:15:49. |

Windows event log 20:10–20:17 ET: an Apple mobile device plugged in over
USB (driver install 20:10:21), the machine's address on a phone hotspot
(172.20.10.4), "hardware has changed" at 20:10:26 and 20:16:44, Universal
Print token prompts — **someone is at the keyboard.** No crash, Defender,
mitigation, or shutdown events at either death. The only mechanisms that
remove the whole tree including the watchdog with no log are `STOP_BOT.bat`
or closing the console windows by hand. Relaunching would fight whoever is
there, so the bot is left DOWN pending the operator's word.

Exchange 20:16 ET: cash $3,767.91 + PV $1,428.74 = **$5,196.65**, 48
positions, 10 resting quotes lapsing on TTL, 9/5 fills 119 / $3,152,
settlements 9/5 +$243.48.

Relight when cleared: `START_BOT.bat` (or the WMI-detached form). Then the
checklist: `ws_fanout_derivation` (bootstrap N=1 for two ~60 s windows →
`ws_fanout_resharding` to the derived N, today's rates ⇒ 3), one
`ws_shard_subscribed` per shard, no `ws_fanout_sharding_refused`,
`ws_pipe_lag` rfq p50 < 3,000 ms (was 6,400), `ws_shed_market_frames` 0,
buffer-overflow 0, `confirm_expired_by_exchange` back to ~2–5%,
`accept_for_unknown_quote` 0, sends ≥ 295–398/min, fills/h vs overnight 14/h.
Store rotation `--apply` remains the operator's `!` command (the classifier
blocked it twice tonight).

## NEXT STEPS

- DONE: fleet build → review → fix → merge `022f083` → gates. **OWED: relight on
  the operator's word** (two stacks were killed from outside the bot tonight;
  a human is at the machine) → verify the checklist above.
- **Operator, at the stop:** store rotation `--apply` (the auto-mode classifier
  blocks it): `.venv\Scripts\python.exe tools\ops\rotate_store.py --apply --store D:\kalshi-combos-TWO-data\combomaker-prod-live-wc.sqlite3 --out <scratchpad>\rotation_apply.json`
  (dry run clean; the stray hard links were removed). Arm `tape_retention`
  only after a rotation.
- Rulings owed: confirm-halt class removal (review flagged), derived stall wall
  shadow → on, fee eat mode (only if the post-transport per-tier read demands).
- Owed: ledger stale-row P1 (~394 open rows); bot standing equity $7,308 vs
  exchange $4,969 (PV source); 70 tape-only fills + 7 partials; favorite-band
  margin read; 9/1 pre-registered reads; p_ruin 0.78–1.0 reading; O1
  shutdown wedge; 16:02 stack death; watchdog self-liveness.
- Parked by operator: NFL.
