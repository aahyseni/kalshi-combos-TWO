# 2026-07-31 — Ship package: hang watchdog + boot-warmup gate + esports 3¢ staged; re-anchor & derived floor held in worktree

Operator readout for the four workstreams that ran 7/31, against the operator's stated
goals: more fills, low risk per dollar, high P(book) read WITH ES/premium, ~$2k on a
15-game slate, and **no regressions of old fixes**.

Context (all measured today): floor stopgap live (`resting_floor_count: 1`, util 0.569,
backstop 415/min → 6); send rate 25–30%, ~550 quotes/min; 9 fills / $542 by mid-morning
(best morning of the week); acceptance by size MEASURED: <$5 1.26/1k, $5–15 0.63/1k,
$15–50 3.85/1k, $50–150 5.24/1k, >$150 2.05/1k — **big tickets accept 4–8× better**;
evictions 2,388/day by absolute-EV key; `skip_max_open_quotes` 503/5min; 2 confirm
declines, both "book-risk snapshot unusable" at boot+72s; bot HUNG 45h 7/29–31 with PID
alive while the log froze.

---

## 1. WRONG / FIXED / OPEN (most important first; retractions marked)

| # | State | Item |
|---|-------|------|
| 1 | **WRONG → FIXED** | Supervision watched **process-aliveness, not work** → the 45h 7/29–31 outage (PID alive, log frozen, START_BOT refusing over live PIDs). Fixed by the **hang watchdog** (`c19bc2b`): external 5th window, progress = log advance OR `decisions` row VALUE advancing (never WAL mtime — writer measured 68 min behind); threshold **DERIVED 122.0s** = 3.6× the worst healthy quiet gap (34s over all 52 completed logs); 18/18 executed proofs (one relight on a real hang, ZERO on a boot loop, flap latch, healthy lulls untouched). Detection latency for the 45h class → **~2 minutes**. Arms at the next operator START_BOT.bat. |
| 2 | **RETRACTED (mine, pre-ship)** | My proposed **acceptance-weighted eviction fix** — REFUTED by the acceptance-by-size measurement before it was built: big tickets already accept 4–8× better ($50–150: 5.24/1k vs <$5: 1.26/1k), so weighting eviction by acceptance would have evicted exactly the flow that fills. **Never shipped; do not build.** Evictions stay on the absolute-EV key. |
| 3 | **WRONG premise → build FIXED anyway** | Task B's premise "today's reneges were quotes sent before the gate could pass" — **REFUTED by the tape**: both reneged quotes (≈$14 premium) were sent under a USABLE gen-matched snapshot; the 06:11:08 fill bumped gen 2→3 and the accepts fell in a **6.75s mid-run stale window**. The boot-warmup gate (`3ed6dd9`) shipped as correct fail-closed hygiene (0 quotes forgone, 0 of 2 reneges avoided). **The real owner is arming `book_risk_stale_decay` (FIX 5, SHADOW)** — its shadow readout on both decline lines carried the exact charged view ($199.66 ES vs later-passing ~$288). Operator decision owed. |
| 4 | **WRONG (caught at copy time)** | The worktree's staged config would have **regressed `risk.resting_floor_count` 1 → 3**, silently undoing the live floor stopgap — the exact "bot regresses old fixes" species. Caught by re-hashing every block at copy time: only the **esports** markup block was applied to MAIN (`2ddb…/47b4… → 716bc9223489f236`, all other markup blocks byte-identical: soccer `673c492e33707bc4`, mlb `de3dad9b0ad7548d`, racing `47b4224dfd440564`, mixed `3d1c2c371cdf6600`, series_adders `17014e66fd5bf1fa`, enabled `b5bea41b6c623f7c`). `resting_floor_count` stays **1**. |
| 5 | **WRONG (in D's report, must be corrected before arming)** | Derived-floor build's claim "worst-ever 60s burst ($199.66/515 fills) is covered" — **REFUTED by direct tape measurement**: real worst bursts are **25 accepts/60s** (2026-07-29T09:52:36Z, up to 4 on one event ticker) and **$400/60s in 4 accepts** (2026-07-28T03:05:40Z). Absorbing those bursts into the 51k-send cumulative tape still leaves k(16)=1 — binomial-iid + cumulative dilution cannot re-learn a clustered mass-acceptance regime intraday. Failure direction is conservative (confirm stays pinned at the 100% fold → cost = won-then-reneged auctions, not uncapped loss), but the claim as written is false. |
| 6 | **WRONG (structural, in D)** | D armed **cannot turn its own flow gate green**: V12 measures utilization ×49.00 at G=49 vs G=1 with the derived rule ARMED — identical to HEAD's flat-3 failure — because per-GAME bucketing (the defect V12/V13 grade) is untouched; K_MIN=1 per bucket still multiplies by the slate. The real repair (whole-book floor, apportioned) is designed and proven in `prove_flow`'s scratch arm but **NOT built**. `tools/vitals/gate_flow.py` must NOT join the rule-9 gate until it ships, or every future rule-9 commit bricks. |
| 7 | **FIXED** | Worktree test contamination (`RUIN_FLOOR_FRAC` NameError in `tests/test_kill_anchored_book_gate.py`) — resolved; full worktree suite **3,468/0**. |
| 8 | **OPEN (operator)** | **C (re-anchor) economics**: the safety half is CONFIRMED clean on the live 17-position book (P(KILL-night)=0.0040 vs budget 0.02; armed book's $470.47 premium < $882.08 ruin line even comonotone ⇒ P(ruin)=0 at ANY ρ), but arming is a **capacity CUT (−55.6% on an 8-ticket 50¢ book) and a structural cheap-NO tax** on an axis refusing ~0% of flow. **NO-SHIP armed. Shadow only. D1/D2 ratification owed.** |
| 9 | **OPEN (hygiene before worktree commit)** | `prove_flow` + worktree `--tier all` verdicts never recorded; `tools/diagnostics/kill_anchor_live_book.py` broken in-tree (passes removed `ruin_floor_frac` → TypeError; hardcodes REPO/data); D's report has no README row; C and D diffs are interleaved in the same 8 live files and must be separated before either commits. |
| 10 | **OPEN (decisions standing)** | FIX 5 arming (owns the renege class); watchdog cancel-all on latched halt; resume posture / kill_anchor % / C1–C5; MC crossover at 3× book. |

---

## 2. Per-build verdicts

| Build | Verdict | Measured reason |
|-------|---------|-----------------|
| **A — hang watchdog** (`c19bc2b`, MAIN, pushed) | **SHIP** — arms at the operator restart | 18/18 executed proofs incl. fooling attempts; threshold derived (122.0s, no hand-set number); zero pricing-path footprint; closes the 45h class to ~2 min. Vitals: no rule-9 file touched; gate verdict appended below (§5). |
| **B — boot-warmup quote gate** (`3ed6dd9`, MAIN, pushed) | **SHIP** — lands at the restart | Vitals 8/8 GREEN; suite 3,430/0; 0 quotes forgone on today's tape; enforcement parity load-bearing (stands down when confirm cannot renege). Premise-refutation recorded (row 3). |
| **Esports 3¢ flat** (config stage, this session) | **SHIP (as OPERATOR PRICE FLOOR, not a calibration)** | Staged into MAIN's gitignored config; hash-verified esports-only (row 4). EV-neutral-to-mildly-negative on the only elasticity we have; buys FILLS + INFORMATION on 0.23% of quote volume; 0/196 quotes is NOT evidence of a deficit (P(0)=88.8% at MLB's rate). |
| **C — KILL re-anchor** (worktree, uncommitted) | **NO-SHIP armed; shadow only** | Row 8. Before merge: separate diffs from D, fix `kill_anchor_live_book.py`, record `prove_flow` + worktree `--tier all`, then commit DISARMED; arming waits on D1/D2. |
| **D — derived resting/burst floor** (worktree, uncommitted) | **NO-SHIP armed** | Rows 5–6. Before merge: correct the burst-coverage claim in its report, add README row, separate diffs, record verdicts; arming decision is operator-owned and the whole-book floor is the real fix. Live tape derives k=1 (floor stopgap already gives 1) — arming buys nothing today. |
| **Acceptance-weighted eviction** | **RETRACTED — never build** | Row 2. |

## 3. The single-restart plan (operator-executed; nothing here restarts anything)

**Staged and verified, loads on START_BOT.bat:**

- Code: `c19bc2b` (watchdog) + `3ed6dd9` (warmup gate) — already at origin/main HEAD.
- Config (gitignored, edited in place, hash-verified §1 row 4):
  ```yaml
  pricing.markup.esports:          # was 3¢ base + 5¢/<10¢ + 4¢/10-20¢ tiers
    enabled: true
    markup_cc: 300                 # FLAT 3¢ at every fair — operator price floor
  risk.resting_floor_count: 1      # UNCHANGED — stopgap preserved, worktree's 3 rejected
  ```
- Deliberately NOT set (stay default-off/shadow): `burst_floor_derived`, the re-anchor
  flag, `book_risk_stale_decay` (SHADOW).

**First-hour metrics (expected):**

| # | Metric | Expected |
|---|--------|----------|
| 1 | 5th window "HANG WATCHDOG" logs `armed: threshold=122.0s`; escalations | present; **0** actions (healthy gaps ≤34s) |
| 2 | `quote_warmup_open`, once | `warmup_s` ≈ 0–4s; `quote.warmup_held` = 0; zero `quote_sent` before it |
| 3 | Throughput | ≥ ~550 quotes/min, send rate 25–30% (no regression vs today) |
| 4 | Boot-window confirm declines "snapshot unusable" | **0** (today: 2 at boot+72s) — mid-run gen-stale reneges may still occur until FIX 5 arms |
| 5 | Esports quotes carry `markup_cc=300` flat; esports share | ~0.2–0.5% of quote volume; ~0 accepts expected in hour 1 (P(0) high — grade over weeks, never on the P&L window) |

**Abort criteria (any ⇒ operator stops via STOP_BOT, we diagnose before relight):**
watchdog takes ANY action in hour 1 without a genuinely frozen log; `quote_warmup_holding`
persists >2 min on a healthy book / warmup never opens; quotes/min < ~275 (50% of baseline)
sustained 15 min; any `WATCHDOG_HALT_*.txt` receipt; any markup change observed outside
esports (re-run the block-hash script vs this report's hashes).

## 4. The binding wall after this lands

Nothing shipping today moves a risk cap, so the wall stays where today's mix put it:
**the ENTITY axis, and inside it `over_size` (261 verdicts vs cool-size 195 /
accumulation 157+ / tiers 155)** — single candidates whose own premium clears the entity
wall, i.e. the entity cap still acting as a second, tighter per-combo cap on exactly the
$50–150 flow that accepts 4–8× better. The already-built tiered entity rebuild
(2026-07-28, flags default-false) governs precisely here; its arming is the standing
3%→5% single-entity decision (C1). That is the next capacity conversation, not another
floor/eviction tweak.

## 5. Vitals verdict (MAIN, this session)

Fast tier re-run after the 7/29–31 tape-facts refresh: **see appended line in
`2026-07-31-hang-watchdog.md`** (same run; recorded once, referenced twice).

## NEXT STEPS

- **Operator**: execute the ONE restart (START_BOT.bat); watch §3's five metrics; abort
  criteria above. Decisions owed: FIX 5 arming (owns the renege class), C1 entity arming
  (the named wall), D1/D2 re-anchor ratification, watchdog cancel-all on latched halt.
- **Me (next session)**: worktree hygiene before any C/D commit — separate the C/D diffs,
  fix `kill_anchor_live_book.py`, correct D's burst claim + README row, record
  `prove_flow` + worktree `--tier all`; then commit both DISARMED.
- **Standing**: P1 Stage 1 (per-STRUCTURE + per-game-DIRECTION net bounds at the
  reservation path) remains the next risk build; whole-book floor is D's real repair.
