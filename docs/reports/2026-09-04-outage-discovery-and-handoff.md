# 2026-09-04 — Outage discovered (down since 8/27 02:08 ET) + session handoff

Discovered during a memory-save pass (operator upgrading Claude Code to
2.1.255+ for Fable 5.1; a fresh session picks this up). Minimal forensics
only — no state changed, nothing relit.

## Timeline

| when (ET) | what | evidence |
|-----------|------|----------|
| 8/26 10:05 | WMI-detached relight boots; verified 580 sends/min, 0×400s through 10:10 | live_20260826_1005.log |
| 8/26 10:15 | **log freezes** (74.6 MB) — cause UNDIAGNOSED; hypothesis: machine sleep minutes after the operator read the readout (WMI detachment protects against session close, not sleep) | file mtime |
| 8/27 02:07 | watchdog relights through start_all (CURRENT_LOG rewritten → live_20260827_0207.log) | CURRENT_LOG.txt |
| 8/27 02:08:47 | relit boot dies ~100 s in: `ClientConnectorDNSError external-api.kalshi.com getaddrinfo failed` — network not up, consistent with wake-from-sleep | 0207 log tail |
| 8/27 02:08+ | boot never reached liveness (no heartbeat) → **watchdog latches STAYING DOWN** (correct per design), logs the latch every ~5 min | hang_watchdog.log |
| 8/27 13:40 | last watchdog latch line — watchdog itself dies after (machine sleep/shutdown) | hang_watchdog.log |
| 9/4 | zero bot processes; no KILL file; repo clean, all pushed (`570f9bf` razor, `664e918` addendum) | process scan |

Net: **3rd multi-day outage in ~5 weeks** (8/12 reboot, 8/20 operator stop,
8/27 sleep-class). Bundesliga (8/29-30) and Saudi (8/28) first matchdays were
missed while down; first fills for both leagues still pending.

## NEXT STEPS (the fresh session's boot sequence)

1. **Exchange truth pull** — equity/positions/settlements. Last known:
   $4,827.44 all cash (8/26 morning) plus ~10 min of 8/26 fills, all settled
   or lapsed unobserved since.
2. **Relight session-detached**: WMI `Win32_Process.Create` →
   `cmd /c START_BOT.bat`. An operator start clears the watchdog latch by
   design (start_all purges watchdog_state.json on non-Auto starts).
3. Verify first window: sends/min vs the 300–460 benchmark, 0×400s, 0 halts,
   startup reconcile clean.
4. Run the now-overdue 9/1 pre-registered reads — favorite-band pooled +
   razor 0.6¢ capture (both on truncated data; say so) — and start the 9/1
   list: store rotation P0 (211 GB), ledger stale-row P1, shard-aware cash
   gate, telemetry-anchor split, composition-aware KILL budget, tie-rho.
5. Diagnose the 8/26 10:15 freeze: System event log, Kernel-Power around that
   minute. If sleep confirms, the boot/sleep-survival item is 3 outages in a
   month — operator has DECLINED it 3×; re-raise with the count, their call.
