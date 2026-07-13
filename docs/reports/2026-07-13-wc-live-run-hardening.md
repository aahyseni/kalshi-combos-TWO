# WC-FAT first live run — hardening log (3 issues found + fixed live)

**Date:** 2026-07-13 · **Config:** `config/prod-live-wc.local.yaml` (gitignored;
WC-only, soccer 3¢, MLB off, caps enforced, isolated live DB
`combomaker-prod-live-wc.sqlite3`). **Outcome:** the pricing pipeline + validated edge + markup are **proven live**
(real WC-FAT quotes posted); **0 fills** (closing 2-game window). BUT the run
surfaced a **systemic pattern**: the Phase-6 fail-closed BREAKERS — built + tested
"shadow/not-live" — each **fatally halt the whole book on a normal live
transient**. Three separate breaker-kills in three short runs. **The live-fire
loop is PAUSED** pending a proper breaker live-hardening pass (below). Bot is flat
($0, 0 fills, `needs_reconcile` set).

## What went right

- Preflight all-5-green; `startup_reconciled leftover_quotes: 0` (the get_quotes
  windowed fix works on **prod** midland — no 500/504).
- First run posted **22 real WC-FAT quotes** (RFQ → price → +3¢ markup → posted),
  4,336 correctly declined (WC-only / pregame filters), on FRA-ESP (Jul 14) and
  ENG-ARG (Jul 15). End-to-end pipeline confirmed live.

## Three issues surfaced + fixed (all committed + pushed)

| # | Symptom | Root cause | Fix |
|---|---|---|---|
| 1 | Refused to start (exit 3) | Quote mode requires a **non-empty `collection_whitelist`** (a coarser gate, separate from the leg-series allowlist; `QuoteApp.__init__`). Static guard doesn't check it. | Added the two WC MVE families (`KXMVESPORTSMULTIGAMEEXTENDED`, `KXMVECROSSCATEGORY`) to the armed config. |
| 2 | Supervisor false-killed the book ~14s in | **Windows file race**: the supervisor's heartbeat READ held `heartbeat.txt` open, so the bot's `os.replace(tmp, target)` failed with PermissionError(13) (Windows won't replace a file another process has open; POSIX would). The bot's beat silently failed → supervisor presumed it WEDGED → emergency-cancel + KILL. | `2934fa4` — `_atomic_write` retries the rename through the sub-ms read window; reader retries a transient read. **Fail-closed preserved** (a genuinely stuck write still exhausts → stale beat → wedged). 4 regression tests. |
| 3 | Circuit-breaker killed the book ~5 min in | The maintenance-tick **book tripwire flattened ALL resting quotes' legs into one set** and ran `taxonomy_impossible` over the union → paired a leg from one legitimate combo with a leg from a **separate** legitimate combo on the same game → declared the union impossible → `HALT_METADATA_CHANGE`. Fired on two valid ENG-ARG quotes forming the pinned `{advance × opponent-win}` pair. **Operator confirmed Kalshi still says "invalid combo"** — the validator did NOT loosen; it was a phantom pairing. | `a0b178b` — `_book_tripwire` runs **per resting combo** (via `_book_leg_refs`), never over the union; halts only if a *single* resting combo is impossible (belt-and-braces the per-RFQ classifier already prevents). Exchange-blocked shapes are **declined at pricing**, never a book-wide kill. 2 regression tests. |

Suite **1716/0**, mypy/ruff clean after all three. None weakened a real safety
property: #2 keeps fail-closed wedge detection; #3 keeps declining impossible
combos + still halts on a single genuinely-impossible resting combo + keeps the
`changed_markets` metadata-drift halt.

### #4 (NOT fixed — prompted the pass): `HALT_DATA_STALE` on a transient WS reconnect

Third run got PAST the tripwire (fix #3 worked) and quoted 23 combos, then the WS
**briefly disconnected**. The bot handled that correctly and gracefully —
invalidated the books, cancelled its 3 resting quotes (`cancel_all reason=
ws_disconnect`). But **0.8s later**, before the WS auto-reconnected, the
data-stale breaker saw `feed rx-age unknown` and **fatally killed the whole book**
(`HALT_DATA_STALE` → kill → `needs_reconcile`) instead of pausing until reconnect.
`None rx-age ⇒ trip` fires instantly; it can't tell a transient reconnect from a
dead feed.

## THE PATTERN (the real finding)

The Phase-6 breakers were built + tested in isolation ("everything SHADOW/DARK/
NOT-LIVE" per the risk-engine reports) and **never exercised against live market
conditions**. Live, each one **fatally halts the entire book (+ blocks restart)
on events that are ROUTINE in trading**: an OS file race (#2), a phantom
impossible-combo RFQ (#3), a WS reconnect (#4) — and more are queued (latency
spikes, 429 bursts, marginal jumps). Whack-a-mole patching them one real-money
crash at a time is the wrong approach (rushes safety edits, burns launches).

**What IS proven:** pricing, correlation, the validated WC-FAT edge, the markup,
and quote posting all work live. The gap is purely breaker **tuning for live**.

## NEXT STEPS

- **Owner: bot (the gating work).** A **breaker live-hardening pass** — audit all
  7 breakers so each **degrades gracefully** (pause quoting + auto-resume on the
  feed/heartbeat coming back; only fatally kill on a *sustained genuine* failure,
  not a sub-second transient), tested, before any further live run. Specifically
  data-stale: tolerate a WS-reconnect grace window; the bot ALREADY cancels quotes
  on disconnect, so the fatal kill is redundant.
- **Owner: operator.** Decide: do the breaker pass now, or bank the WC learnings +
  wait for the pooled multi-week data (shadow recorder on the new server) and a
  bigger live window. 2 WC games + 0 fills = low ROI to rush.
- **Owner: bot (deferred).** Re-price the graded universe with the live engine
  (de-confound MLB); room predictor + per-tier toggles once weeks pool.

## Honest status

- **Edge:** validated (WC FAT, reality-test CI5 +4.2) — see
  `2026-07-13-wc-mlb-markup-regrade.md`. Markup 3¢ **provisional/one-week**; final
  = pooled multi-week (never a P&L refit).
- **Fills:** 0 so far — 2 WC games left; sparse/zero is the expected outcome. The
  value of this run is proving the plumbing + surfacing/fixing the 3 issues, not
  volume.
- **Durability:** running in the session; fine for the short WC window, a durable
  server is needed for any multi-day run.

## NEXT STEPS

- **Owner: bot.** Keep supervising (rolling book + critical-event watch); surface
  any fill instantly; report on halt.
- **Owner: operator.** Restart the shadow recorder on the new server → weeks 2-4
  → the pooled multi-week markup (the real gate). Decide durable host if we extend
  live past the WC window.
- **Owner: bot (deferred).** Re-price the graded universe with the live engine
  (de-confound MLB); explicit FAT/NORMAL room predictor + per-tier toggles once
  weeks pool.
