# 2026-08-12 — Week-away readout (8/6→8/12 autonomous run) + Windows-Update reboot outage

**Session trigger:** operator resumed 2026-08-12 morning after ~6 days away.
This is the exchange-truth readout of everything that happened since the last
report (2026-08-06), the root cause of the current outage, and the live state.
All reads were **read-only** (account_standing + paced GETs + log/tape reads);
no order placed or cancelled, no config touched, no process started.

---

## 60-second summary

| item | state |
|---|---|
| Bot | **DOWN since 8/12 02:32 ET** — Windows Update rebooted the PC (`MoUsoCoreWorker`/`TrustedInstaller` restarts 02:29–02:33 in the System event log); bot + watchdog died with the OS; **nothing auto-starts after a reboot** |
| Week P&L | **+$1,180.12 realized across 1,009 settlements since 8/6** (era split, exchange settlement ledger) — vs +$1,557.31 for the account's entire prior life |
| Equity | **$4,695.43** = $3,370.01 cash + $1,325.42 exchange mark on open positions (riding tonight's 8/12 slate; prepaid sell-only — max loss already escrowed) |
| Best day | **8/9: +$1,092.10** (212 settles) — new best day ever (prior: 7/31 $1,060.58 premium day) |
| Worst day | **8/11: −$917.00** (184 settles, W76/L108, $3,986.59 premium) — worst day ever; **full entry-EV forensics OWED** (preliminary: broad, not one whale — bottom-5 = −$613.62 of it, biggest single ticket −$169.33) |
| Leftover quotes | **0** — `list_open_quotes` (user_filter=self) returns none; the crash left nothing pickable |
| Uncommitted work | none; main `27ccd65` clean, in sync with origin. One stray unpushed commit `4c0fe7e` on `reanchor-demotion` — pushed this session |

## 1) The week the bot ran itself (8/6 → 8/12)

The operator left the bot LIVE on `27ccd65` with the 8/6-armed config
(`pricing.dnp_scalar.enabled: true` per the 8/6 ratification — armed at the
first restart after 15:28 ET 8/6 and live all week). No commits, no reports,
no config changes after 8/6 15:28 ET. The watchdog carried the whole week:
**44 relights this episode** (`watchdog_state.json`), healthy spans up to
76,887 s (~21 h). Every one of the ~20 machine KILLs 8/10–8/12 has the same
signature: `supervisor kill: loop stalled: maintenance age ~30.5–31.5s >
30.5s` — the known hand-set stall wall (North-Star violation, mechanism
repair owed) now firing every few hours. Prime suspect for the degradation:
the live store `data/combomaker-prod-live-wc.sqlite3` is now **149.6 GB**
(store rotation has been queued since 7/25 when it was 47 GB).

Daily P&L, exchange settlement ledger, grouped by ET settle day:

| ET day | settles | realized |
|---|---|---|
| 8/6 | 177 | +$0.56 |
| 8/7 | 108 | +$364.88 |
| 8/8 | 206 | +$333.10 |
| 8/9 | 212 | **+$1,092.10** |
| 8/10 | 47 | +$6.29 |
| 8/11 | 184 | **−$917.00** |
| 8/12 (to 02:32) | 75 | +$300.16 |

(ET-day attribution straddles slates as usual — a night slate settles past
midnight into the next ET day.)

Account identity (account_standing, era split 2026-08-06): all-time realized
**+$2,737.43** on the single $2,000 deposit; identity residual −$42.00
(order-time trading fees & open-position cost basis — the known non-settlement
class; attribute if it grows). Note: there was **no new deposit** — the
"deposits: 1 row, $2,000" is the original 6/29 funding.

## 2) The outage — root cause chain (all times ET)

```
8/11 22:36  boot (live_20260811_2236.log) — evening/overnight session,
            28 fills, quoting the 8/12 slate pregame
8/12 02:06  supervisor_emergency_kill → kill_switch_halt (maintenance-window
            flap; the nightly Kalshi 503 pattern) — watchdog relights
8/12 02:07  boot; shutdown at 02:13 (shutdown_timed_out at killswitch_stop)
            — watchdog relights again
8/12 02:14  boot (live_20260812_0214.log) — quoting normally; book at the
            walls: util=1.00 widen/decline shadows, entity-tier refusals,
            and 2,233 rfq_worker_failed HTTP 400 insufficient_balance on
            POST /communications/quotes in 14 min (cash fully deployed
            into ~200 resting quotes + positions)
8/12 02:29  Windows Update (MoUsoCoreWorker) initiates OS restart
8/12 02:32  TrustedInstaller reboots the machine — bot, supervisor,
            watchdog, prober all die; log ends mid-write
8/12 02:33+ machine back up; NOTHING relaunches the stack after a reboot
            (launcher windows survive session end, not reboot)
```

Post-crash exchange verification (this session): **zero open quotes** remain
(TTL/expiry cleaned everything); the open positions (~$1,325.42 mark) ride
tonight's games and are prepaid — no margin exposure while down; balance
$3,370.01 confirms overnight settlements + quote collateral released.

Two distinct operational defects exposed:

1. **Boot persistence gap** — a Windows Update reboot silently stops trading
   until a human notices. The stack needs a boot-time relaunch path
   (scheduled task running `START_BOT.bat`/`start_all.ps1 -Auto` at startup,
   which already refuses to double-start) and/or Windows Update active-hours
   + restart-deferral so reboots can't land mid-slate. **Operator decision.**
2. **insufficient_balance retry storm** — the create-quote path treats
   exchange balance refusal as a per-RFQ error (2,233 in 14 min), burning
   write budget on doomed sends. Mechanism repair (not a knob): treat
   `insufficient_balance` as a fail-closed *pause-new-creates* signal until
   the balance tracker sees free cash again. Small, isolated, rule-8 cycle.

## 3) The 8/11 −$917 day — preliminary dissection (full forensics OWED)

184 settles, W76/L108, net −$917.00 on $3,986.59 premium at risk (losers
−$2,068.38, winners +$1,151.39). Shape: **broad, not a single whale** —
bottom-5 losers sum −$613.62 (67% of net), biggest single ticket −$169.33
(`KXMVECROSSCATEGORY-…A78823ACE49`, settled 00:56 ET). Both slates in the ET
day contributed (00:14–01:26 settles = the 8/10 night slate; 19:21–22:35 = the
8/11 evening slate).

**Not yet answered** (the established luck-vs-EV drill, same method as the
8/1/8/2/8/6 forensics): entry-EV recovery per ticket, MC P(day ≤ −$917 | our
fairs), cell/structure split vs the era's profit cells, whale-seam check,
recovery-print and invisible-settlement sweep, ledger cross-check. Also owed:
fold 8/7–8/12 (~1,000 new settles — the corpus roughly doubled) into the
pooled era read (P(excess≤0) series 0.061 → 0.200 → 0.242 → 0.095 → ?).
**This is the next analysis task and is read-only — it does not block relight.**

## 4) Current armed-flag truth (read from `config/prod-live-wc.local.yaml`, the file the bot loads)

| flag | state | note |
|---|---|---|
| `pricing.dnp_scalar.enabled` | **true — ARMED, live all week** | 8/6 operator ratification; commit/README say "shadow-dark" — the local yaml is the truth |
| `pricing.skew.settled_fact_resolution` | true (armed 8/1) | |
| `pricing.skew.leg_axis_armed` | true | `conc_armed` false (shadow), `pbook_armed` false (disarmed 7/27) |
| `risk.kill_gate_marginal` / `ruin_gate_marginal` | true but **INERT** | gated behind `kill_anchored_book_gate: false`; the yaml footer says all four lines (+ `portfolio_ruin_prob_budget` back to 0.05) flip together after the acceptance-seed fix |
| `risk.portfolio_det_max_frac` | "0.70" | Reading-B backstop interim; revert to 0.36 when the marginal gate arms |
| `risk.portfolio_ruin_prob_budget` | "1.0" | interim stand-down (8/1) |
| `risk.daily_loss_frac` | "1.0" = disarmed | operator ratification 8/5 ("daily pnl should never matter") |
| `risk.adaptive_caps_mode` | shadow (since 7/23) | static caps enforce |
| `risk.slate_partition_*` / `entity_admission_*` | enabled+armed (7/28) | entity axis is the #1 binding wall |
| `risk.open_quote_capacity_derived` | staged, commented OUT | pairs with the withdraw-budget raise decision |
| utilization backstop (3× notional, per-game double-count ×3.6) | **live and binding** | the un-ratified repair is standing decision #1 (REMIND 8/2, now 10 days over) |

The 02:14 boot's final minutes show exactly the standing capacity story:
`util=1.00` widen/decline shadows on nearly every RFQ, entity-tier
`would_admit: false` refusals, and cash exhaustion — the walls now bind at
~2× the bankroll they were measured on (8/5: 183k near-cap events + 123k
skips vs 36 accepts).

## 5) Corrections to memory/docs made this session

- **Resume-state memory was badly stale** (said bot DOWN/flat since 7/23 at
  $2,179.74): the bot has been live and trading autonomously through 8/12;
  equity $4,695.43. Memory rewritten this session.
- The repo-state sweep's suggestion that "a $2k deposit went in" is WRONG —
  the $2,000 deposit row is the original 6/29 funding; no new deposits.
- LIVE_ISSUES.txt and both root RISK_ENGINE_*.txt audits carry explicit
  historical/superseded banners — do not read as current.

## NEXT STEPS

- **Operator (decision owed, time-sensitive — 8/12 slate first pitch ~13:40
  ET):** relight now via `START_BOT.bat` (preflight re-proves the book;
  watchdog re-arms; DNP guard stays armed), or hold down pending the 8/11
  forensics.
- **Operator (decision owed):** boot persistence — scheduled task to run the
  start path at machine boot + Windows Update active hours, so an OS reboot
  can never silently stop trading again.
- **Operator (decision owed, standing #1, REMIND 8/2):** ratify the
  utilization-backstop repair (count settlement notional ONCE per combo —
  port the slate-cap partition fix — and set the multiple as a ratified
  anchor). Re-evidenced by the 8/5 saturation measurement and the 8/11–8/12
  util=1.00 tape at doubled bankroll.
- **Me (next analysis, read-only):** full 8/11 −$917 luck-vs-EV forensics +
  pooled-era update over the ~1,000 new settlements.
- **Me (build queue after that, unchanged ranking):** P1 Stage-1
  per-STRUCTURE bounds (whale seam, 3 sightings); accounting sweep extension
  (invisible-settlement class 8 tickers/−$98.25 + multi-fill ledger de-dup);
  insufficient_balance pause-new-creates mechanism; store
  rotation/maintenance-stall mechanism repair (149.6 GB store, 30.5 s
  hand-set wall).
