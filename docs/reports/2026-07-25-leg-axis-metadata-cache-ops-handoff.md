# 2026-07-25 — Leg-direction axis + persistent metadata cache + decline detail; ops handoff to operator

## Context: what the afternoon's live tape showed

1. **K-ladder one-way book.** ~half of the day's ~$313 premium rode short
   pitcher-strikeout-overs, clustered on the same arms across combos and
   games (Cease ≈ $127 across 6 combos incl. the 19-fill $99.93 structure —
   at 92% of its accumulated cap when quoting stopped). No existing axis saw
   it: P0-9 nets only moneyline-family ME events; the MC's cross-game prop
   ρ ≈ 0; per-game caps see games, not directions.
2. **Tail pinned at the KILL anchor.** Committed tail ES99 reached ~$260.8 vs
   the 12%-of-bankroll KILL distance $261.57 → the candidate gate correctly
   declined nearly everything (incl. a $0.76-EV fill), admitting only
   tail-neutral scraps. "Why aren't we filling more with $245 in positions" =
   the one-way book consumed the whole 5σ premium budget; a diverse book
   could hold ~2–3× the premium at the same anchor.
3. **429 boot storm + zombie double-stack.** v6's boot re-fetched every
   watched leg's metadata cold: 2,319 metadata fetch failures (all HTTP 429)
   in 36 s across every MLB series — worsened by a ZOMBIE v5 stack (2
   supervisors + 2 bots + probers) still running on the same account's rate
   budget (also explains the 400 `invalid_parameters` create races and 404
   delete storms). Low-flow families (HR 257/257 failed, HIT 248) stayed
   unpriceable until sparse RFQ flow re-healed them → fills structurally
   biased toward high-flow K/TOTAL series after every restart.
4. **Stale-MC fallback audited SAFE**: confirms admitted with
   `fallback_reason: book_risk_generation_stale` went through the candidate
   gate's OWN fresh MC (strictly additive, fail-closed) — the audit's stale
   fields only describe the cached committed view. Not a hole.

## Ops handoff (operator directive: bot + monitors are operator-run now)

All session-run processes stopped (incl. the zombie stack the second sweep
caught). Claude's job = logs + code only. One-click launchers shipped:

| file | does |
|---|---|
| `START_BOT.bat` | single-instance guard (refuses if any combomaker process runs) → dated logs → 4 windows: supervisor, main monitor, fill prober, prober monitor; writes `data/CURRENT_LOG.txt` |
| `STOP_BOT.bat` | kills supervisors FIRST, then bots/probers; verifies; offers a `cancel-all` sweep |
| `READOUT.bat` | pbook shadow read-out on the current run's log |

(`tools/ops/*.ps1` carry the logic; monitors colorize fills/declines/halts.)

### Launcher incident log (2026-07-25 ~2:53–3:40p ET — first operator launch)

Two launcher defects, found from the live log, fixed + pushed same hour:

1. **Stale-heartbeat emergency kill (`8e07076`).** The first `START_BOT.bat`
   run booted the supervisor into the DEAD morning stack's stale
   `data/heartbeat.txt`; it declared "wedged" (age 3367s > 30s) at
   2:53:46p and emergency-KILLed before the bot started — four silent
   windows. START now purges stale heartbeat files when the process guard
   proves nothing is running, and shows + PROMPTS y/n on a KILL file
   (never silently deletes a safety artifact; `needs_reconcile` is left
   for the bot's own boot reconcile). The monitor window now prints the
   real boot sequence (`supervisor_starting → prod_preflight_green →
   exposure_rehydrated → startup_reconciled`, names verified against the
   live log) so a healthy start is VISIBLE.
2. **Em-dash encoding parse failure (`4aedc90`).** The hygiene edit
   introduced em dashes inside double-quoted strings; PowerShell 5.1 reads
   BOM-less files as ANSI, where the em dash's final byte (0x94) decodes
   as a closing smart-quote — the string terminated early and the whole
   script failed to parse. All launcher scripts are now pure ASCII
   (byte-scanned) and parser-checked. **Standing rule: `tools/ops/*.ps1`
   stay pure ASCII.**
3. **Wrong entrypoint (`a82df98`).** The launcher started
   `-m combomaker.ops.supervisor` STANDALONE — but the architecture is the
   reverse of that assumption: the BOT (`cli run`) spawns the supervisor as
   its own subprocess (`quote_app.supervisor_launch_cmd`). A standalone
   supervisor watches the bot's heartbeat, finds none (no bot), and
   emergency-KILLs in ~43 ms — the second operator launch failed the same
   way with "heartbeat missing/unreadable". Entrypoint now copied VERBATIM
   from the live morning process list (the source-of-truth rule this
   violated):
   `.venv\Scripts\python.exe -m combomaker.ops.cli run --env prod --mode
   quote --confirm-live --config config\prod-live-wc.local.yaml`.
   The false-positive KILL + stale heartbeats were cleared;
   `needs_reconcile` left for the bot's own boot reconcile.
4. **venv SHIM DOUBLING — a day-long misdiagnosis, corrected (`b61a411`).**
   The venv `python.exe` is a launcher shim that spawns the real
   interpreter as a CHILD with an IDENTICAL command line (proven: one
   sleeper launch = 2 processes, parent = shim). Every "duplicate stack"
   seen today was ONE healthy stack seen double — **including the morning
   "zombie v5 pair" (its 429-storm attribution is RETRACTED; the storm was
   the ordinary cold-cache boot burst) and the afternoon "two bots
   double-quoting one account" (I killed the operator's healthy,
   correctly-launched 3:09p bot on that misread).** The launcher's
   post-launch verification now counts ROOT processes only (matched
   processes whose parent is not itself in the matched set).
5. **Launch-guard race + stale-shell block (`b61a411`).** The process-count
   guard was check-then-act; a named OS mutex
   (`Global\combomaker_start_bot`) now makes the launch atomic. Dead
   cmd/powershell shell windows from a stopped stack (command lines still
   name the bot) are swept instead of blocking the start; `STOP_BOT` also
   reaps orphaned pool workers (spawn_main pythons with a dead parent).
   **Final state: START_BOT.bat executed end-to-end by Claude — stale
   sweep fired, mutex+guard passed, "VERIFIED: exactly one bot process",
   `startup_reconciled` cancelled 32 leftover quotes,
   `prod_preflight_green` 3:22:08p ET, leg-axis shadow steering live.**

## Build 1 — LEG-DIRECTION AXIS (operator: "recognize direction for all legs, know when to raise markups")

- `risk/exposure.py`: `leg_family_key` (`SERIES:side`, e.g. `KXMLBKS:yes` =
  money riding K-overs) and `leg_entity_key` (`SERIES:ENTITY:side`, rung
  segment drops → one key per ladder; shapes verified against live tickers).
  New committed-only snapshot fields `committed_loss_by_family_cc` /
  `committed_loss_by_entity_cc`: full combo loss attributed to each leg key
  (comonotone, same convention as per-game), deduped per position.
- `risk/skew.py`: `LegAxisProfile` + `_leg_axis_component` — the pbook
  component's measured-only math on leg-direction keys: deficit vs the
  uniform 1/n book × (1 − p_book) × the SAME caps-derived onset denominator
  (min(hard game $cap, game_loss_frac×bank, KILL frac×bank) — no new
  numbers). Overweight direction ⇒ convex widen; absent/underweight
  direction (incl. the OTHER side of a loaded ladder) ⇒ linear rebate.
  Clamped to the peak caps; composed overall clamp unchanged.
- `rfq/lifecycle.py`: profile built at QUOTE TIME from the same exposure
  snapshot the limits read (no staleness window; p_book only when the MC
  profile is generation-fresh, else the component is NEUTRAL — UNKNOWN never
  widens). `family_cc`/`entity_cc`/`leg_axis_rows` on every
  `inventory_skew_shadow` line + a `leg_axis_exposure` visibility event
  (top-8 families/entities by committed $) on every book-risk publish.
- **SHADOW-FIRST**: `pricing.skew.leg_axis_enabled: true` (default) computes
  + logs; `leg_axis_armed: false` (default) keeps pricing byte-identical —
  pinned by test. Arm only after a shadow slate read.

## Build 2 — PERSISTENT METADATA CACHE (kills the boot 429 storm)

`marketdata/metadata.py`: `build_persist_payload()` (on-loop snapshot;
iterating live dicts from a thread would race `refresh()`) +
`write_persist_payload()` (atomic tmp+replace, worker thread) +
`load_persisted()` (markets with FUTURE close times + the events they
reference; loaded entries stamped TTL-expired so `peek()` — the hot path —
serves instantly while the async path revalidates on first touch; grid
injections never persist; corrupt/missing file = cold boot, never an error).
`ops/quote_app.py`: load at boot, flush ~every 60 s when dirty via
`asyncio.to_thread`. File: `data/metadata_cache.json`.

## Build 3 — DECLINE DETAIL

`risk_audit` decline lines now carry `detail` — the candidate gate's
specific bound + post-book numbers (`post_governing_model_es_over_budget`,
EV, post-ES/det/ruin), ending the opaque `decline_candidate_risk`.

## Verification

- `tests/test_leg_axis.py` (keys off real tickers, cross-combo/rung
  attribution, cross-game family widen, other-side rebate, same-entity
  widen, onset scaling, UNKNOWN-neutral, byte-identical-unarmed, armed adds)
  + `tests/test_metadata_persistence.py` (warm-boot zero-network peek,
  expired/undated never resurrect, injections never persist, corrupt = cold).
- **4-lens adversarial review workflow (14 agents), all verdicts
  PoC-verified. Confirmed + FIXED pre-commit:**
  1. *HIGH — warm-cache revalidation was dead code*: both live fetch sites
     gate on `peek()` being None, so a persisted entry would NEVER refresh —
     a mid-slate reschedule could leave a stale later close_time feeding the
     in-play gate for the market's whole life, and the metadata-change
     breaker (baseline = first peek) was structurally blind cross-run. Fix:
     `needs_revalidation()` (cached but TTL-expired) counts as a fetch miss
     at `_ensure_watched` + `_arm_rehydrated_legs` — pricing stays warm off
     `peek()`, one spaced async refresh per stale leg heals
     status/close/expiry and re-arms the breaker.
  2. *MEDIUM — boot crash-loop on valid-JSON-malformed cache file* (list /
     null / string payloads, `{"markets": null}`, naive close_time
     TypeError). Fix: catch-all around the whole parse (partial keep, cold
     boot, never a dead bot) + `_aware_or_none` drops naive-dated entries in
     both load and persist filters.
  3. *LOW — failed flush never retried* (dirty cleared at snapshot time).
     Fix: writer returns −1 on failure; callers `mark_dirty()` to re-arm.
  (Plus the persist-vs-refresh thread race, caught by me pre-review:
  snapshot on-loop, write off-loop.)
- Full suite **2743 passed / 0 failed**; mypy strict clean on all changed
  modules.

## NEXT STEPS

- **Operator**: run `START_BOT.bat` (ONE instance); watch
  `leg_axis_exposure` + `family_cc`/`entity_cc` shadow magnitudes tonight;
  arming decisions owed: `pbook_armed` + `allow_negative_ev_hedge` +
  `hedge_budget_tail_derived` (read-out via `READOUT.bat`), later
  `leg_axis_armed` after its own shadow slate; slate fraction (leave 65% vs
  0.80) still open.
- **Code (Claude)**: single-instance lock INSIDE the supervisor (the .bat
  guard is convention, not enforcement); leg-axis shadow read-out section in
  the readout tool; post-slate P&L + settlement reconciliation; queued:
  resting_floor_count auto-derivation, SkewLimits denominators from live
  caps, store rotation (47 GB), rehydrate false-flag fix.
