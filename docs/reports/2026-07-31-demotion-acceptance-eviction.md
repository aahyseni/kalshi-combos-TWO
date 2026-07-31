# 2026-07-31 — Restart package: det-max demotion gated NO-SHIP, acceptance collapse attributed, eviction/capacity staged

Operator ratified: "finish number 2 … raise to 2k … open it up as it was on 7/29 just with more capacity."
This report is the honest close-out: what the adversarial gates killed, what shipped, what the one restart arms, and what still needs an operator decision.

## 1. WRONG / FIXED / OPEN

| # | State | Item |
|---|-------|------|
| 1 | **WRONG** | The ratified backstop `det_max = (1−0.30)·B = 0.70B ≈ $2,099` derives from reading "ruin 30%" as an equity floor. The **enforced** codebase convention is ruin = 30% **drawdown** (`RUIN_FLOOR_FRAC=0.30` distance; `book_risk` uses 0.70 surviving equity), so the derivation is self-cancelling — it restates its input (pinned: `tests/test_kill_anchored_book_gate.py:345`). The honest anchor-derived backstop is **0.30B ≈ $900 — TIGHTER than today's 0.36B wall ($1,079)**. No ratified anchor yields 0.70B. |
| 2 | **WRONG** (measured) | The demotion's safety claim. The 64-ticket $1,983.64 "spread" book it exists to admit: P(KILL) 0.57% at ρ=0, but **12.09% at ρ=0.10, 20.82% at ρ=0.25** (>10× the 2% budget; ρ=0.25 is the recorded sensitivity), **P(30%-drawdown ruin) 29.47% at ρ=1**. Comonotone loss under 0.70B = 2.33× the ruin distance, 5.83× the KILL line. 200k-path copula sweep pinned in-test (20/20 PASS). **The demotion is retracted claim #8; it is NOT in the tree; a canary test trips if anyone re-lands it.** |
| 3 | **WRONG** | "Acceptance collapse unexplained; margin moved only +0.26¢." The store records only the FIRST send per RFQ (log lines ≈3.7× store rows); live reprice widening was invisible. Real move: applied inventory-skew median **−21cc → −141/−157cc** at the 7/29 05:50 boot, flat all day, 70–75% of quotes ≤−50cc. |
| 4 | **WRONG** | "Collapse = flow/requester change." Refuted: collapse is **hot-key-only** (7/29 hot 0.38–0.52 accepts/1k vs cold 7.7; 7/31 hot 3/12,732 vs cold 1.50/1k, deficit p≈1e-5; cold never collapsed; top accepting creator present every day). The step is in OUR pricing, timed to OUR boots. (Correction folded in: "hot 0/8,903" → 3/12,732 full-day; hot was already mildly worse pre-boot — the boot is the amplification, not the sole onset.) |
| 5 | **WRONG** | "Protect small diverse quotes by acceptance-weighting eviction." Acceptance **RISES** with size (<$5 1.34/1k vs $50–150 11.09/1k, disjoint CP intervals at α=0.02); weighting would evict the flow that fills. Protection = **derived capacity** (1,198–1,272 slots vs hand-set 200) + dES99 charge. |
| 6 | **FIXED** (MAIN, live at the 18:44 boot) | Hang watchdog `c19bc2b` + relight-unblock `b2df3ff`; boot-warmup quote gate `3ed6dd9`; confirm priority lane `410e8fb` (accepts no longer die in WS FIFO backlog; `ws.priority_frame` counting live); esports 3¢ flat + `resting_floor_count: 1` already in the live gitignored config (markup blocks hash-verified at copy, esports-only `→716b9223489f236`). |
| 7 | **FIXED** (this train) | `kill_anchored_book_gate` flag committed **SHADOW-default** (re-points the tail gate 0.35B→the ratified 0.12B KILL line when armed; byte-identical off — golden 20,000+2,000 cases, digest `b136e16e`). Rule-9 verdicts recorded: vitals fast **8/8 GREEN**, pre-ship **1/1 GREEN**, suite green at the final tree (count in commit message). Orphaned `tests/test_burst_floor_derived.py` + import-broken `demotion_capacity_proof.py` deleted; tape-facts refresh folded; README rows added. |
| 8 | **OPEN** | The $2k thesis. Path = operator re-ratification of a NEW det-max anchor with eyes open: "a comonotone collapse of the admitted book may exceed the 30%-drawdown ruin distance by up to 2.33×; measured P(KILL) at ρ=0.25 on the target book ≈21%" — or capacity through diversified shape under a smaller raise. Never a typed 0.70. |
| 9 | **OPEN** | FIX A (acceptance repair): fact-resolve settled/graded legs out of the skew-feeding exposure snapshot (7/29 boot rehydrated 8 positions on 7 FINISHED games; 7/31 boot rehydrated the 12-game mass-accept book → every same-direction MLB quote widened 1–3¢ all day). This is a **pricing change** — needs explicit ratification, rule-8 prototype→parity→shadow, before/after quotes/min. Plus: 8 dead 'open' `position_ledger` rows (7/26–7/29, ~$91) reconciled vs exchange `/portfolio/fills`. |
| 10 | **OPEN** | Derived open-quote capacity + diversity eviction: BUILT, defaults off, vitals green — left **uncommitted** in the worktree (gate: disentangle + strip `BurstFloorRule` residue from `risk/exposure.py:55` first). It targets the cap that binds the quote side tonight (`skip_max_open_quotes` at 200, evictions running live 22:51Z). Top next arm. |
| 11 | **OPEN** | Whole-book resting floor (designed in prove_flow, not built); `gate_flow` V12/V13 stays OUT of rule 9 until it ships. Burst-floor arming stays withdrawn. |

## 2. Commit / push state

| Piece | Where | State |
|-------|-------|-------|
| watchdog + warmup + confirm lane + relight fix | MAIN `main` | pushed (`c19bc2b`, `3ed6dd9`, `410e8fb`, `b2df3ff`), **running live now** |
| esports 3¢ + floor 1 | live gitignored config | applied + hash-verified; loaded at the 18:44 boot |
| kill-anchor gate (shadow) | branch `reanchor-demotion` → merged to `main` | committed this train, pushed |
| tape-facts refresh + docs/README rows | same train | committed, pushed |
| det-max demotion 0.70B | nowhere | **NO-SHIP**, canary-guarded |
| eviction/derived-capacity code | worktree only, uncommitted | staged for its own gated commit |
| burst floor | withdrawn | test file + broken diagnostic deleted this train |

## 3. One-restart arming plan (in order; the bot is already cycling on today's ships — the next operator restart lands only the shadow gate)

| Step | YAML / action | First-hour expectation | Abort |
|------|---------------|------------------------|-------|
| 1 watchdog+warmup | none (shipped; external watchdog running) | 0 relights on a healthy log (worst healthy lull 34s vs 122s threshold); warmup holds sends until confirm predicate passes | `WATCHDOG_HALT_*.txt` receipt appears → leave down, page operator |
| 2 esports 3¢ | none — ALREADY LIVE. Verify-only: re-hash markup blocks pre-boot; esports block `716b9223489f236`, all others byte-identical | esports ≈0.2–0.3% of quotes at 3¢; 0–1 accepts expected (P(0)=88.8% at MLB rate) | any non-esports block hash differs → restore from hash manifest |
| 3 `kill_anchored_book_gate: false` (explicit; SHADOW) | shadow breach telemetry only, byte-identical decisions | shadow log P(KILL-night) on tonight's book 0.011–0.014 (budget 0.02); 0 armed refusals | any decision diff vs golden digest `b136e16e` → flag stays false, investigate |
| 4 armed gate + demotion | **NOT ARMED — NO-SHIP** | armed without the demotion only ADDS refusals (−55.6% on an 8-ticket 50¢ book) and the demotion failed the copula gate (row 2) | — |
| 5 `burst_floor_derived` | **OFF — withdrawn** (V12 fails ×49 armed; whole-book floor is the real fix, not built) | — | — |
| 6 eviction/capacity flags | **OFF — uncommitted** pending residue strip + own gate | — | — |

## 4. Live book BEFORE / AFTER (measured 18:5x EDT, `kill_anchor_live_book.py`, read-only)

| Metric | BEFORE (today) | AFTER (this restart) |
|--------|----------------|----------------------|
| open tickets / premium | 29 / $999.88 | unchanged |
| EV / ES99 / ES-per-premium | ≈$34 / $409.15 / 0.41 | unchanged |
| N_eff | 14.11 | unchanged |
| P(KILL-night) | 0.0110 (budget 0.02) | unchanged (+shadow-logged) |
| binding wall next | det-max 0.36B = $1,079.49 at **92.6% util**; growth sim: both regimes refuse the 40th ticket, stop at 39, P(KILL) 0.0140 INSIDE budget | same walls; quote-side binder tonight = `skip_max_open_quotes` (200) |

The armed KILL gate and today's dollar wall stop tonight's book at the SAME ticket (39) — on this diversified book the demotion buys nothing until det-max itself moves; on books n<15 the armed gate binds TIGHTER (lattice: n=8 P(≥6 lose)=1.13%, P(≥5)=5.80% — the 2% budget sits inside the gap).

## 5. Acceptance collapse — ranked cause

1. **Boot-rehydrated exposure on finished/settled games feeding per-game inventory skew** (confirmed twice, independently): step −21cc→−141/−157cc at the 7/29 05:50 boot; hot-key-only collapse; knife-edge timing; 7/31 12-game rehydration. **Ships now: nothing** (pricing change). **Needs ratification: FIX A** (row 9).
2. **Uptime, not rate**: dark 7/29 13:23Z→7/31 ~10:00Z (whole 7/29 slate + all of 7/30) + 12:28–17:23 today — the watchdog chain (shipped, live) owns this class.
3. Ruled out with measurement: eviction churn, TTL races, first-quote latency, requester mix, base markups.

## NEXT STEPS
- **Operator decisions owed:** (a) re-ratify or drop a NEW det-max anchor knowing the 0.70B collision + ρ=0.25 → P(KILL)≈21% measurement; (b) ratify FIX A as a pricing change (this IS "open it up as 7/29"); (c) eviction/derived-capacity arm order once committed.
- Next build: strip burst-floor residue, commit eviction/capacity defaults-off with its own gate run; then FIX A prototype (prove the stale mass is in `delta_by_game` at boot) on ratification.
- Watch: first hour after next restart per §3; shadow KILL-gate telemetry accumulates the small-book economics evidence (cheap-NO question re-measure, not assume).
