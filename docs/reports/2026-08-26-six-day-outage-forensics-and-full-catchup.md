# 2026-08-26 — Six-day outage forensics + full catch-up (21-agent sweep)

**Operator ask:** "open memory from kalshi-combos-TWO, read all dated reports and
.md files, make sure you are fully up to date." Executed as a 21-agent workflow:
all 186 dated reports + docs/research (19) + docs/calibration (10) +
docs/api-notes (16) + NOTES.md + root files + all 53 memory files read; two
forensics agents on the 8/20 shutdown and the uncommitted working tree.
This report records the material NEW findings (post-8/19, i.e. after the last
commit `6e5cdcf` and the last resume-state memory).

---

## 1. THE BOT HAS BEEN DOWN 6 DAYS (since 8/20 17:55 ET)

| item | finding |
|------|---------|
| Last activity | `live_20260820_1732.log` ends **2026-08-20 17:55:05 ET** mid-quote-cycle, heartbeat + both loops healthy at that second |
| How it ended | **NOT** a reboot/crash/KILL/watchdog action: zero System-log boot/shutdown/BSOD events (machine up since 8/12); bot + fill prober + supervisor + **watchdog all stopped within the same second**, 16s after a Kernel-Power modern-standby entry (17:54:49 ET). Sleep suspends-but-preserves processes and the machine was provably awake 8/21 evening with zero bot writes ⇒ the tree was **externally terminated**. Leading hypothesis: operator STOP_BOT / window-close at the moment the machine was put to sleep. `stop_all.ps1` writes no trace by design — only the operator can confirm (and whether the cancel-all prompt was taken). |
| Nothing relights | No scheduled task, no startup item; relight depends on the manually-started watchdog, which died with the stack. Same gap as the 8/12 Windows-Update incident (operator declined boot persistence 8/12: "leave as is"). |
| Data since | No file under `D:\kalshi-combos-TWO-data` newer than 8/20 17:55 ET. Store now **211 GB** (was 191 GB on 8/19). |

**Consequence for the freeze plan:** the 8/31 engine freeze was supposed to buy a
month of untouched data; the bot actually ran only ~8/12–8/20. The 9/1
pre-registered reads (favorite-band pooled check, razor verdict, §D dashboard)
will have ~8 days of data, not ~19.

## 2. LAST KNOWN MONEY STATE (8/20 17:33–17:55 ET — now 6 days stale)

- **Exchange equity $6,677.71** (available cash $3,256.88, 78 open positions,
  $2,000 lifetime deposits baselined) — **a new all-time high**, +$2,518.79 vs
  the 8/19 morning $4,158.92. Day realized 8/20 = **+$810.79** (ET day, seeded
  +$752.72 at the 17:32 boot). 8/19 activity: 181 accepts / 172 executed on the
  46-hour process.
- The ≈+$2,038 of stale-ledger unbooked WINS identified 8/19 is consistent with
  this jump — the pessimistic-bias thesis was right.
- 78 positions were open at death; all have since settled UNOBSERVED. Current
  equity is unknown until an exchange read. Final fill probes (17:53 ET) showed
  no fills at 42.8–47.6¢, richness −0.10¢ to +3.40¢.
- Final book_risk_snapshot: det-max $1,582.52 vs backstop $4,577.34, ES99
  $886.23, p_book 0.1033, **p_ruin 0.8881** (see §4), kill line $6,539.05
  (telemetry — all loss halts disarmed per the operator's 8/18 ruling).

## 3. 8/20 WAS A CHURN DAY (9 relights), ENDING THE 46-HOUR CLEAN RUN

- 03:45 ET supervisor KILL ends the 46.4h process that ran all of 8/19.
- **7 supervisor stall-kills, all the identical signature**: `maintenance age
  60.6–61.4s > 60.5s` — the maintenance loop a fraction over its (already
  doubled 30→60s on 8/18) budget on the 211 GB store. Two more deaths (13:44,
  13:49 ET) left **no KILL file at all** — untraced abrupt exits, same mode as
  the final 17:55 death, making it possibly the **third** silent termination
  that day (weakens the operator-stop hypothesis for 17:55; strengthens an
  unknown external killer. Unresolved.)
- Watchdog relit all of them correctly (the 8/17 human-KILL latch fix held;
  the 17:30 halt receipt was `halt_kill_file`, machine class, auto-clearable).

## 4. p_ruin PEGGED ~1.0 MOST OF 8/20 WHILE QUOTING CONTINUED

p_ruin crossed 0.85 at 06:30 ET, hit 0.9927 by 08:02, **pegged 1.0 from ~11:00
through ≥17:29 ET**, and the final quote_sent events carry 0.8881. This is the
chosen posture — ruin budget 1.0 since the 8/16 gate stand-down, all loss halts
disarmed 8/18 — so nothing malfunctioned; per-candidate caps kept refusing
(2,272 `skip_portfolio_cvar` mentions in the final 22-min log). But the bot
spent a full day quoting a book its own model called near-certain to hit the
30%-drawdown ruin floor, while equity was simultaneously spiking to an all-time
high — the p_ruin anchor lags spiked/marked equity. Both facts feed the 9/1
**composition-aware KILL budget + telemetry-anchor split** items.

## 5. THE UNCOMMITTED WORKING TREE = ALL THREE 8/19 PROPOSALS, AND IT RAN LIVE

Written 8/19 08:50–09:03 ET (minutes after commit `6e5cdcf`), never committed,
no report, no test-run receipt — the session evidently died (mirrors the 7/31
"agents died on usage limit" stash note). **The venv is an editable install of
the working tree (`_editable_impl_combomaker.pth` → `src`), so every 8/20
process ran this code for ~14 hours.**

| file | what it is | status |
|------|-----------|--------|
| `risk/limits.py` (+82) | **(a) gross-notional once-counted fix** — `_once_counted_notional_cc()` replaces the per-game roll-up (measured ×3.6–5.6 double-count; the 8/18 won-auction renege missed by 0.086%); 3× multiple untouched; resting quotes folded via the existing haircut compose. UNCONDITIONAL — no arming flag. Ratified 8/12, daylight ship proposed 8/19. | RAN LIVE 8/20 (~14h, zero wall incidents observed) |
| `ops/persistence.py` (+268) | **(b) store self-lock fix** — WAL checkpoint on a dedicated 2nd connection, fetchall-inside-cursor-scope on every bounded read, `decision_reason_counts` paged, `_dropped_writes` surfaced as `store_writer_stats`. | RAN LIVE 8/20 — **evidence it works**: WAL at death 40 MB (vs 5.63 GB growing 0.64 GB/h on 8/19); store grew 20 GB in a day = writer capturing full tape again |
| `pricing/markup.py` (+28), `ops/config.py` (+10) | **(c) thin-auction retained-margin lever** — `thin_auction_bonus_cc` (+bonus on MLB/soccer fair ≥35¢), default 0, key absent from live yaml | DARK (no pricing change ran) |
| tests (+447 lines) | full coverage of (a)/(b)/(c) incl. the 8/18 renege-shape regression pin | **NEVER EXECUTED** (no receipt) |
| `tools/vitals/tape_facts.json` (+412) | unrelated: 8/17 13:01 vitals rescan `6e5cdcf` never picked up | safe to commit separately |

Process debt, stated plainly: this build ran live UNGATED (no suite run, no
vitals 8/8, no report, violating hard rules 7/9 and the throughput
before/after rule) — nobody chose that; the editable install made the working
tree the live code the moment the files were saved. The ~14h clean live run +
WAL evidence is favorable but is NOT the repo's bar.

Stashes (both read-only-inspected, untouched): `stash@{1}` (7/31) is the stale
KILL-anchor re-anchor precursor whose feature later landed via other commits —
conflicts with both HEAD and the WIP; `stash@{0}` (8/1) is 5 lines of
tape_facts churn. Both are droppable, operator's call.

## 6. STATE OF THE STANDING DECISION QUEUE (verified against config truth)

Live yaml (`prod-live-wc.local.yaml`, mtime 8/18 05:19, unchanged since):
ml_parlay_cc 30 (razor), mains 2¢ soccer+MLB, structure 1% / per_combo 2%,
kill_anchored_book_gate false (telemetry), ruin budget 1.0, det_max_frac 0.70,
cash_gate_enabled false, ALL loss halts 1.0 (disarmed), heartbeat 60s, capacity
"shadow", six club leagues allowlisted, NO friendlies.

| decision | state as of today |
|----------|-------------------|
| Gross-notional daylight ship | **Effectively shipped by accident** (ran live 8/20). Needs: tests+vitals run, commit, report — or explicit revert |
| persistence.py fix | Same — ran live, looks like it works, needs gating + commit |
| SHARD1 top-up | Still pending; `TRANSFER_SHARD1.bat` script verified to still exist (temp-dir path is fragile — copy into `tools/ops/` when convenient). Shard cash split as of the 8/20 logs unknown (no breakdown events) |
| 1% vs 2% cap verdict | Still open (8/18 evidence packet stands: zero fills above the 2% line) |
| Friendlies allowlist | Still open (5,676-RFQ pool) |
| Thin-auction +1¢ lever (c) | Coded dark; arming = one yaml key + operator blessing |
| Store rotation / WAL | Still 9/1 P0 — 211 GB now; the persistence fix treats the lock class, NOT the size |
| Ledger stale-row repair | Still 9/1 P1 (the +$2,038 class — validated by the equity print) |
| Boot persistence after reboot/sleep | Operator declined 8/12; **this outage is the second 6-day class instance** — re-raise |

## NEXT STEPS

- **Operator:** (1) confirm what happened at 8/20 17:55 ET (STOP_BOT? sleep?)
  — it determines whether we have an unknown process-killer; (2) decide relight
  posture: gate-then-keep the WIP (recommended: run suite + vitals fast +
  pre-ship, commit, restart) vs stash it and boot HEAD; (3) the standing four:
  shard-1 transfer, 1v2%, friendlies, thin-auction lever; (4) re-raise boot
  persistence (two 6-day outages in a month).
- **Me (post-blessing):** gate + commit the WIP with before/after throughput;
  exchange truth pull (equity, settled 78, shard split) at relight; quiesced
  TRUNCATE checkpoint at the restart; 9/1 list execution (store rotation P0,
  ledger backfill P1, shard-aware gate, telemetry-anchor split,
  composition-aware KILL budget); pre-registered favorite-band + razor reads
  (~9/1, on truncated data — say so in the read).
- **Blocked:** everything live needs the operator (bot down, harness blocks
  fund moves, arming decisions are operator-owned).
