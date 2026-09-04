# 2026-09-04 — Outage timeline CORRECTED (the morning handoff was wrong) + exchange truth pull

Fresh session (Fable 5.1). The morning handoff report
(`2026-09-04-outage-discovery-and-handoff.md`) reconstructed the outage from
`CURRENT_LOG.txt` + the newest log only. A 24-agent forensics pass (one
extractor per live log × 21 logs, prober-log reader, two adversarial
verifiers with different lenses) plus the Windows event log, the KILL
receipts, `watchdog_state.json`, `hang_watchdog.log`, and a read-only
exchange truth pull produced a materially different picture. **Nothing was
relit; no state changed.** All times ET unless marked Z (bot JSON `ts` is UTC).

## WRONG / FIXED / OPEN

| Claim (morning handoff) | Verdict | Truth |
|---|---|---|
| "8/26 10:05 relight froze at 10:15 ET, cause undiagnosed, sleep suspected" | **WRONG (retracted)** | The 10:05 boot was **supervisor stall-killed at 10:14:16** (`maintenance age=60.7s > 60.5s`), shutdown timed out at 10:15:17 (the "freeze" = the last line of one per-boot file), watchdog relit at **10:16:15**. Windows has **zero sleep/resume events 8/26–9/4** (BootId 182, up since 8/12). |
| "watchdog only relit at 8/27 02:07" | **WRONG** | **16 watchdog relights** in the 10:05 episode (8/26 10:16, 12:23, 14:20, 15:21, 18:08, 18:17, 21:31, 21:41, 22:04, 22:07, 22:43; 8/27 01:34, 01:44, 01:55, 02:01, 02:07), each matched 1:1 to a `live_*.log`. The bot quoted **~15.5 of the next 16 hours**. |
| "died on wake-DNS failure" | **HALF** | DNS death yes, wake no. **Wi-Fi/DNS flapped on an awake machine**: `getaddrinfo` failures 01:32, 01:52, 01:59, 02:04, 02:08 ET, matching Windows network re-identification events (01:32, 01:53, 02:41…06:27) to the minute. |
| "bot down since 8/27 02:08" | **REFINED** | Last quote sent **02:04:25 ET 8/27**; last exchange-truth read 02:02:44. Latch at 02:13. |
| "equity $4,827.44 all cash (8/26 morning), fills since unobserved" | **SUPERSEDED** | See truth pull below: everything settled by 8/30. |

## Exchange truth (9/4 10:03 ET, read-only GETs)

| Item | Value |
|---|---|
| Equity | **$4,980.09, ALL CASH** |
| Positions / open quotes | 0 / 0 |
| Shard split | shard1 $4,678.03 (94%) · shard0 $302.07 · shards 2–3 $0 |
| 8/26 session fills | **371** (all NO, all `KXMVECROSSCATEGORY-SHARD1`; 5 flagged `is_taker` — unexplained) |
| Premium at risk | $5,896.91 |
| Settlements 8/26–8/30 | 308 · revenue $6,108.05 · fees $70.80 → **+$140.34** |
| Equity delta vs 8/26 08:27 | **+$152.65** |
| All-time realized | **+$2,980.09** on the $2,000 deposit (new realized high; 8/20's $6,677.71 was a mark) |
| Last settlement | 8/30 22:11Z — nothing unobserved remains |

## Corrected timeline (21 boots, 8/26 08:26 → 8/27 02:13 ET)

| Window (ET) | Boot | Ran | Sends (~/min) | Acc / exec | Equity at boot | Stop reason |
|---|---|---|---|---|---|---|
| 08:26–08:33 | #1 (old Claude session) | 7 min | 3,228 (552) | 4 / 4 | $4,827.44 all cash | stall-kill 61.2s |
| 08:35–08:42 | #2 | 8 min | 3,451 (515) | 6 / 6 | $4,915.63 | stall-kill 60.6s |
| 08:44–08:53 | #3 | 10 min | 4,424 (505) | 2 / 2 | $5,025.39 | stall-kill 60.9s |
| 08:55–09:03 | #4 | 8 min | 4,095 (538) | 3 / 3 | $5,056.64 | **session close killed stack + watchdog** (mid-line) |
| 09:03–10:05 | — | **62 min DARK** | | | | no watchdog alive |
| 10:05–10:14 | #5 (operator WMI relight) | 9 min | 4,703 (568–614) | 5 / 4 | | stall-kill 60.7s |
| 10:16–12:22 | #6 | 2h05 | 62,237 (498) | 64 / 64 | $5,106.26 | stall-kill 61.1s |
| 12:23–14:19 | #7 | 1h56 | 53,356 (464) | 32 / 31 | $6,083.49 | stall-kill 61.2s |
| 14:20–15:19 | #8 | 59 min | 24,301 (415) | 20 / 19 | $6,706.43 (99 pos) | stall-kill 60.5s |
| 15:21–18:06 | #9 | 2h45 | 61,243 (372) | 87 / 82 | $6,851.30 | stall-kill 60.7s · **6 phantom ledger fills booked here** |
| 18:08–18:15 | #10 | 7 min | 2,434 (432) | 4 / 4 | $7,332.40 (143 pos) | stall-kill 61.0s |
| 18:17–21:27 | #11 (longest) | 3h09 | 62,590 (480) | 116 / 108 | | in-process halt; **process lingered 4 min** → watchdog hung_process |
| 21:31–21:38 | #12 | 7 min | 1,199 | 2 / 2 | $7,775.95 (167 pos) | stall-kill 61.2s |
| 21:41–22:03 | #13 | 22 min | 4,594 | 3 / 3 | **$7,806.02 (peak mark, 165 pos)** | **hard halt: `market_result ''`** |
| 22:04–22:06 | #14 | 2 min | 220 | 0 / 0 | $7,530.64 | **hard halt: `market_result ''`** |
| 22:07–22:41 | #15 | 34 min | 9,090 (271) | 7 / 7 | $7,440.75 | **hard halt: settlement-credit mismatch** (66.71 ledger vs 43.47 exchange) |
| 22:43–01:33 | #16 | 2h50 | 66,986 (396) | 40 / 39 | $6,753.80 | **DNS lost 01:32** → halt_data_stale, 90 quotes unresolved |
| 01:34–01:42 | #17 | 8 min | 2,670 | 1 / 1 | $6,255.78 (71 pos) | stall-kill 61.3s |
| 01:44–01:54 | #18 | 10 min | 3,137 (420) | 1 / 1 | $6,301.64 | **DNS lost 01:52** → halt_data_stale, 105 unresolved |
| 01:55–02:00 | #19 | 6 min | 1,228 | 2 / 2 | $6,311.29 | **DNS lost 01:59** → halt_data_stale, 104 unresolved |
| 02:01–02:06 | #20 (last to quote) | 5 min | 763 | 2 / 1 | $6,339.68 (75 pos) | withdrew 8 leftovers at boot; **DNS lost 02:04** → 126 unresolved; stall-kill 60.9s during shutdown |
| 02:08–02:08:45 | #21 | 45 s | 0 | 0 / 0 | (no read) | `needs_reconcile` + enumerate timeout → uncaught `ClientConnectorDNSError` |
| 02:13 | watchdog | | | | | **LATCH** "run never reached liveness (no heartbeat)" — logged every 5 min until 13:40; watchdog died ~13:42 = operator keyboard-input minute |

Totals: ~375k sends, 401 accepts, 371 exchange fills; quoting ~945 of 1,067
minutes (88.6%); ~120 min downtime = 62 min session-death gap + ~49 min across
19 relight gaps (median ~2.5 min each) + 9 min tail.

## Stop-reason census and what each one means

1. **12 supervisor stall-kills, every one 60.5–61.5s over a 60.5s bound**, with
   `exchange_reachable=true` and the RFQ path still streaming 100–300 lines/s
   at kill time. The healthy maintenance cadence is ~16s and in every sampled
   kill the loop's event stream stops in one step; the ages cluster within 1s
   of the bound because that is the supervisor's poll granularity. This is a
   **false-positive class**, identical to 8/20's 7 kills. Store pressure
   precedes each one (checkpoint `busy` fallbacks, divergence-sweep 5s
   timeouts, queue_depth pinned 200k). `lifecycle.py` has 12 direct
   `await self._store.*` calls with no `asyncio.wait_for`; only the 5s-bounded
   divergence sweep visibly times out. Mechanism repair (never a knob): bound
   the store awaits and derive the wall from the measured pass period.
2. **4 hard halts on settlement facts**: three `market_result ''` rows within
   40 min (21:27, 22:02, 22:06) during a 429 storm on settled reads — whether
   `''` is a genuine exchange payload or a rate-limited fetch treated as a
   settlement row is OPEN; and one settlement-credit mismatch (22:41) caused
   by the phantom ledger fills below. Fail-closed was correct each time; each
   cost a relight.
3. **4 network deaths** (halt_data_stale on DNS loss) — correct behaviour;
   90–126 resting quotes were left UNRESOLVED each time and the 02:01 boot's
   startup reconcile withdrew 8 of them = unresolved quotes DO persist on the
   exchange across restarts.
4. **1 hung process** (boot #11 lingered 4+ min after `quote_app_stopped`;
   4 orphaned pool workers reaped) — non-daemon pool worker / store-close race
   suspected, not proven.
5. **The latch (design gap)**: flap-guard-1 ("no heartbeat ⇒ latch, relighting
   would boot-loop") fired on a boot that died to a transient network error.
   The 8/6 "if the bot crashes, restart, that's it" rework converted
   post-heartbeat short runs to retry-with-backoff, but a pre-heartbeat death
   still latches forever. Network re-identifications continued until ~06:30
   ET, so a boot-loop was plausible until then — and nothing retried after.
   This is the 3rd multi-day outage in five weeks (8/12 reboot, 8/20 operator
   stop, 8/27 network→latch). Boot/sleep persistence was DECLINED 3×; this
   item is different: **classify the boot-death cause and retry-with-backoff
   on network/exchange classes, latch only on config/code classes.**

## New defects found (exchange-verified where stated)

- **PHANTOM LEDGER FILLS (verified against `/portfolio/fills` + settlements)**:
  `…AC104B1B2E5` ledger 66.71ct vs exchange 43.47 (one fill) — 4 phantom rows
  (14.00/3.00/3.24/3.00); `…1F4E0958F23` ledger 102.49 vs exchange 29.10
  (three fills) — 2 phantom rows (48.93/24.46). All six have a
  `quote_accepted` AND an exchange-timestamped `quote_executed_msg` in the
  15:21–18:06 run (`order_id_conflict` 38, 75 "poll-recovered" fills). 96.63
  phantom contracts / ~$60.48 premium the exchange never held. Caused the
  22:41 `halt_reconciliation_mismatch`. **Ledger stale-row P1 now includes a
  poll-recovery double-book class.** Both tickers are settled, so no relight
  trap remains from them.
- **Store persistence collapsed in every run >1h**: queue_depth pinned at
  200,000, dropped_writes 0.19M–2.39M per run, checkpoint `busy` fallbacks,
  on the 213 GB store. Rotation is P0 (WAL is only 30 MB — the 8/19 2nd-conn
  checkpoint fix holds; the writer throughput does not).
- **p_ruin = 1.0** in every post-warmup `book_risk_snapshot` from 12:23 ET on,
  while p_book ranged 0.19–0.95 and equity moved normally — degenerate field
  or real; six extractors flagged it, unresolved.
- **Pick-off cluster**: `…F1F3B9C6163` took 26 exchange fills, mostly 1-lot at
  53–56.5¢ with +5 to +8¢ richness vs the best other NO — we were the most
  generous bidder by 5–8¢, repeatedly hit for 1 lot. Adverse-selection read
  owed (no refit; measurement first).
- `TRANSFER_SHARD1.bat` points at a scratchpad that no longer exists (script
  gone; the transfer is done — rebuild under `tools/ops/` only if needed).
- 574 GB dead July store on D: (`combomaker-prod.sqlite3` 119 GB + 455 GB
  WAL, untouched since 7/22; D: has 1.6 TB free). Two orphan `grep.exe`
  log-tail monitors from 8/14–8/15 sessions still alive (harmless).
- 5 of 371 fills carry `is_taker=true` — unexplained for a maker-only book.

## NEXT STEPS

- **Operator**: say "relight" → WMI-detached `Win32_Process.Create` →
  `cmd /c START_BOT.bat` (an operator start purges the watchdog latch). First
  window: 300–460 sends/min, 0×400s, `needs_reconcile` clears, 0 halts.
  Decide: watchdog network-class retry (recommended, mechanism not knob);
  stall-wall repair order vs store rotation.
- **Me (after relight)**: overdue 9/1 pre-registered reads (§D dashboard of
  the 8/12 deferred ledger; favorite-band pooled + razor 0.6¢ — data spans
  8/12–8/27 only, say so); then the 9/1 list + the new mechanism repairs
  above: watchdog boot-death classifier, bounded store awaits / derived stall
  wall, poll-recovery double-book fix, `market_result ''` vs 429 handling,
  store rotation P0, ledger stale-row P1.
- Bundesliga (8/29–30) and Saudi (8/28) first matchdays were missed; first
  fills for both still pending.
