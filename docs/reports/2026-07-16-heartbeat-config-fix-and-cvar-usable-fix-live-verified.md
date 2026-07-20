# 2026-07-16 — Problem-B supervisor `--config` fix + all-reserved CVaR `usable` fix, LIVE-VERIFIED

Two committed fixes (`1ed1921`, `acba2d8` on `risk-audit-overnight`, suite
**2054/0**, both pushed), each live-verified the same day. Bot relaunched twice;
currently RUNNING on the fixed code (`data/live_logs/live_20260716_fixed.log`).

## Fix 1 — Problem B (handoff §4B): supervisor never received the launch config

`quote_app._launch_supervisor` spawned the supervisor with only `--env`, so it
re-loaded the BASE `config/prod.yaml` and the armed config's
`supervisor.heartbeat_timeout_s: 30` never applied — the watchdog killed the bot
at the 15s default under the 7/15 firehose (twice). Fix: `AppConfig.source_path`
(recorded by `load_config`, excluded from dumps, YAML-spoof-proof) + pure
`supervisor_launch_cmd()` forwards `--config`. **LIVE-VERIFIED:** supervisor
process cmdline carries `--config config\prod-live-wc.local.yaml`; first run
survived 15 minutes under 47.7 priced-RFQs/sec with zero supervisor kills, zero
WS drops, zero halts (7/15 runs died at ~6 min).

## Fix 2 — the quote blocker: all-reserved book graded "unusable" → blanket SKIP_PORTFOLIO_CVAR

**Live symptom (run 1, checkpoint code):** 0 quotes; 26,681 of ~31k priced
decisions declined `skip_portfolio_cvar` (86%); worst conveyor gap 1.5s (flow
was never the problem — the decline was).

**Root cause (a P0-4 design/property contradiction):** the book's only position
is the rehydrated 7/14 MLB All-Star combo (series gated off → conservatively
RESERVED, `risk_modeled=False`). `compute_book_risk` has an explicit
all-reserved branch returning a real snapshot (sampled tail exactly 0,
deterministic axis = the $4.46 reserve) whose docstring says "still USABLE …
not a no-go" — but `BookRiskSnapshot.usable` still required `n_positions > 0`,
so that exact snapshot graded UNUSABLE and `limits.py` fail-closed EVERY quote
on both tail axes. The old test even baked the contradiction in ("the CVaR cap
still fails closed via usable"). Fix: `usable` accepts `n_positions == 0` with
a nonzero deterministic reserve; UNKNOWN and truly-empty stay unusable.
Regressions: snapshot-level + end-to-end limits both directions (reserve under
ceiling quotes; reserve over ceiling still gates `SKIP_PORTFOLIO_DET_MAX`).

**LIVE-VERIFIED (run 2, fixed code):** snapshot publishes
(`n_positions: 0, deterministic_max_loss_cc: 44570, es 0, p_ruin 0`), and the
bot **quoted 268 combos in the first ~2 minutes (peak 186/min), winning 2
auctions**. `skip_portfolio_cvar` collapsed 26,681 → 69, all inside the first
~40s before the first snapshot published (startup warmup; see follow-ups).

## Live findings from the fixed run

1. **No conveyor blocks.** Pre-fix run: 38,126 decisions / 13 min, max
   inter-decision gap 1.5s. Fixed run: max quote-to-quote gap 5.1s during the
   active burst; zero gaps >10s. WS: single connection held all run.
2. **Both auction wins were SELF-DECLINED at last-look** (`decline_risk_limit`,
   binding = mutex-aware DIRECTIONAL cap: direction_cc $310/$374 vs $300 = 15%
   of $2k). Cause: ~60 resting sell-side quotes consume the whole directional
   budget under the E2 mass-acceptance worst case, so any win tips it over.
   Handoff Problems A/F wearing a new coat: on game days this will burn most
   wins. DECISION OWED (operator): raise `directional_frac`, rebalance
   `max_open_quotes` vs the directional budget (quote fewer, confirm more), or
   prioritize the Problem-A last-look MC gate (nets ME structure properly).
3. **The 14:43+ "quiet" is demand mix, not a wedge:** open quotes drained
   (latest audit `direction_cc: 0`, gross back to the $4.46 reserve), deletions'
   404s are the RFQ-died-first path, and the RFQs still arriving are oversized
   (decline on `skip_size_above_max` / `skip_per_combo_loss_cap` /
   single-candidate `skip_mass_acceptance_breach` — caps working as designed).
   Today's slate: pregame FRA-ENG (7/18) + ESP-ARG final (7/19), 2-3 days out;
   the 14:41 burst was the standing-RFQ backlog on subscribe.
4. **Advanced API tier (operator upgraded, 300 read/write):** no code change
   needed — the bot has no client-side proactive write throttle (reacts to 429s;
   breaker at 10/10s window); the tier just cuts 429 risk at higher quote volume.

## NEXT STEPS

1. **Operator decision — directional budget vs resting quotes** (finding 2):
   the one change that converts wins into fills on game days. Options above.
2. **Warmup snapshot** (agent, small): compute one synchronous book-risk
   snapshot before quoting opens so the first ~40s don't fail closed
   (69 declines this run).
3. **Problem A durable fix** (agent + operator sign-off): last-look structural-MC
   worst-case gate — the principled replacement for both the comonotone game cap
   and the directional overstatement.
4. **Watch:** first real fill on the fixed code → reconciliation gate must stay
   silent; settlement of the reserved MLB combo should clear the $4.46 reserve
   (drop-settled-on-rehydration remains a small open item).
