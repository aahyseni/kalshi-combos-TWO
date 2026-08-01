# 2026-08-01 — Overnight exit forensics (Task C) + adversarial gate verification

**Scope: read-only diagnosis — zero code changes, no order touched, live bot
(PID 38144, `data/live_20260801_0111.log`) never stopped or restarted.** This
report writes down the Task C exit diagnosis AND the adversarial gate's
independent re-verification of every ranked class against the raw log tails
(the gate re-ran every grep itself). One Task C claim is **corrected** below.
All times ET unless marked Z.

## Exit census 7/31 afternoon → 8/1 01:11 (verified against raw tails)

| # | run (log) | died | class | raw-tail evidence (gate re-verified) |
|---|---|---|---|---|
| 1 | 1723 | 17:33 | **kill-switch: `halt_confirm_timeouts`** | 3× `confirm_failed` HTTP-400 `'expired'` 21:27:43→21:33:17Z, then `kill_switch_halt reason=halt_confirm_timeouts` 21:33:17Z — **pre-dates the confirm-priority lane** (landed at the 19:04+ restarts) |
| 2 | — | 17:34 | boot refusal (rc=1 start path) | no log produced; refusal over the halt latch (per Task C; not independently re-verifiable from tails) |
| 3 | 1754 | 18:06 | **UNCLASSIFIED hard death** | log ends mid-INFO (`slate_partition_shadow` 22:06:04Z), **no wedge/halt/shutdown lines at all** |
| 4–9 | 1810, 1818, 1826, 1835, 1844, 1852 | 18:16–18:59 | supervisor stall-kill ×6 | `supervisor_loop_wedged` "loop stalled: maintenance age=30.9/31.5/31.1/31.4/31.2/31.4s > 30.5s" → `supervisor_emergency_kill` (cancelled 149/168/148/176/159/56) → `halt_kill_file`; cadence ~8.5 min |
| 10 | 1900 | 19:04 | operator restart (F5 gate boot) | intentional, not a failure exit |
| 11 | 1904 | 20:10 | **UNCLASSIFIED hard death** | log ends mid-INFO (`inventory_skew_shadow` 00:10:40Z), no wedge/halt/shutdown lines |
| 12 | 2021 | 20:21 | boot failure (stillborn) | log is exactly **1,198 bytes** (verified); halt latch; operator restart 20:43 |
| 13 | 2043 | 22:00 | **kill-switch: `halt_drawdown`** | "give-back 2831078cc >= 1/10 bankroll = 2817989cc" 01:59:57Z, receipt written, cancel_all 139 — working as designed on the −$227-at-that-point night |
| 14 | 2201 | 22:08 | supervisor stall-kill (7th) | "maintenance age=**30.5s > 30.5s**" (margin ZERO) 02:08:27Z, 474s-old run still in post-restart reconcile |
| 15 | 2209 | 01:10 | **kill-switch: `halt_marginal_jump`** | `KXMLBHR-26AUG011915AZCLE-CLESKWAN38-2: marginal became unreadable (had 0.010)` sustained **31s > 30s grace** → halt 05:09:35Z, cancel_all 201; stall-kill fired REDUNDANTLY 05:10:15Z during the hung shutdown; watchdog relight #3 at 01:11:15 → the current healthy run |

Watchdog: episode relights #2 (22:09:51) and #3 (01:11:15) verified in
`data/hang_watchdog.log` incl. machine-KILL auto-clear archives
(`KILL_20260731_220945.txt`, `KILL_20260801_011109.txt`). Both 2209/0111 boots
relit in ~11s from kill to VERIFIED single bot process.

## CORRECTION to the Task C summary (gate finding)

**"halt_confirm_timeouts: DEAD confirmed — grep confirm_failed = 0 across all
4 overnight logs (2043, 2201, 2209, 0111)" is WRONG as a night-wide claim.**
The scoped statement is true (gate re-verified: 0 `confirm_failed` in
2043/2201/2209/0111), but run 1723 was KILLED BY `halt_confirm_timeouts` at
17:33 ET — inside the stated 17:23→01:11 exit window — after 3 consecutive
HTTP-400 `'expired'` confirms, and Task C's class ranking (stall ×7 +
kill-switch ×2 + boot ×2 = 11) omits it. The honest reading is **better** than
"dead": the class fired for the last time BEFORE the confirm-priority lane
landed (19:04/20:43 boots) and has **zero occurrences since, through the
heaviest flow of the night** — evidence the 2026-07-31 fix works, not that the
class mysteriously stopped. Additionally, exits #3 and #11 (1754 @18:06,
1904 @20:10) are **hard deaths with no recorded reason in any tail** —
outside the taxonomy entirely (consistent with force-kill at shutdown-timeout
with unflushed buffers, or an external kill during operator-attended churn).

## Verified starvation/load numbers (exact)

| claim | gate re-count | verdict |
|---|---|---|
| `store_writer_checkpoint_failed` ("database table is locked") run 2043 | **872** | ✓ exact |
| same, run 2209 | **3,734** | ✓ exact |
| stall margins at death 30.5–31.5s vs the 30.5s budget | 30.9/31.5/31.1/31.4/31.2/31.4/30.5/30.8 | ✓ (incl. the zero-margin 2201 kill) |
| 2021 stillborn size | 1,198 bytes | ✓ exact |
| current run 0111: `kill_switch_halt` + `confirm_failed` | **0 + 0** | ✓ healthy |

## Structural findings (mechanisms to repair — never knob patches)

1. **The 30.5s stall wall is a hand-set number sitting exactly at the
   load-induced tail** (kills at 30.5–31.5s) — a North Star violation; it will
   keep firing in every RFQ-flood window until the maintenance loop's own
   budget/deferral mechanism is repaired (the same windows show thousands of
   locked-checkpoint failures and chronic budget deferrals).
2. **`halt_marginal_jump` treats "marginal became unreadable" on a NEXT-DAY
   market (Aug-1 AZ/CLE Kwan HR, in `settled_resolution_pending`
   never_fetched) the same as a price jump on a live one** — a
   feed/market-lifecycle gap class, not a price-integrity event; the halt cost
   a healthy 3-hour run at 01:10.
3. **Every clean shutdown exceeded the 30s shutdown budget**, so all exits
   present to the watchdog as hard process deaths (and the two UNCLASSIFIED
   deaths are plausibly this mechanism eating its own kill lines).
4. Crash-window persistence (fills/decisions stores losing rows during these
   exact churn windows) is quantified in the companion loss-forensics report —
   the sweep must CREATE ledger rows, not just log `settled_unmatched`.

## NEXT STEPS

- **Next build session (slow-loop, fix-isolation rule):** crash-window
  persistence — recovery sweep creates position_ledger rows for exchange
  fills with no local row; decisions/fills writers flush before supervisor
  kill or are re-derived by the sweep.
- **Mechanism repair owed (pricing-path, own prototype/parity cycle):** the
  maintenance-loop stall class under RFQ flood (the 30.5s wall) — derive the
  budget from measured loop-latency state instead of a constant, or repair the
  starvation (locked checkpoints) that makes the loop stall at all.
- **Small, isolated:** `halt_marginal_jump` should classify
  "unreadable next-day market" as the existing feed-gap/no-quote class (leg
  already fails closed at quote time), not a book-wide halt; needs its own
  gated change — not hand-widening the 30s grace.
- **No operator decision forced tonight**; the watchdog chain carried the
  night (two ~11s autonomous relights) and the current run is healthy.
