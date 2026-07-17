# 2026-07-16 — Adversarial verify (round 2) of the outage-built commits + alias hardening + ONE relaunch

**Scope:** the owed agent re-verify of everything built during the API-529 outages —
`2bfae72` (five-item batch), `1af2953` (wedge fix), `ff250da` (F10), `8d8d96c`
(ESPARG champion pricing aliases), `9526fbe` (pregame schedule table) — plus the
prior session's uncommitted round-1 review-fix pass. Three independent adversarial
agents (alias-fix verification / ops-lifecycle verification / completeness sweep),
findings fixed inline, everything committed as **`a397efb`** (pushed), then ONE
relaunch onto the fully-verified tree. Suite **2232/0**; ruff/mypy at baseline.

## Verdicts on the round-1 fix pass (the 11 uncommitted files)

All five alias fixes CONFIRMED CORRECT (validator canonical+injectivity, tripwire
`_series`/`_suffix`, waiver `_settle_specs` keyed-real, knockout OR-fold,
group-key game parse), warmup/register/poll-bound/rotation confirmed correct in
mechanism — with the findings below.

## Findings fixed in `a397efb`

| # | Severity | Finding | Fix |
|---|----------|---------|-----|
| 1 | **SERIOUS (money, live on champion flow)** | Same-game ADVANCE×ADVANCE pairs priced through `dixon_coles.joint_probability`, which multiplies each leg's penalty-shootout factor INDEPENDENTLY — `q²` instead of `q` on level-after-ET states ⇒ **~3–5¢ systematic underprice on a tight final** (e.g. champion-AR yes × champion-ES no). Pre-existing for real advance yes/no mixes; the alias made it hot. | **Family 4 in `relationships.py`**: same-game advance pair = COMPLEMENT (rule book: exactly one team advances incl ET/pens). `{yes,yes}`/`{no,no}` → IMPOSSIBLE (farmable — airtight tautology); mixed → equivalence containment, joint = P(the YES leg), exact. Wired defensively (Kalshi's one-selection-per-family blocks most shapes today; its validator has known misses). |
| 2 | **SERIOUS (config, game-day)** | Alias validator accepted two DIFFERENT real events folding into ONE synthetic event → shared game key → the game-loss cap, P0-9 directional bound, skew, game plans, copula regroup AND the waiver would cross-net unrelated legs (E2 family). Stale-entry-beside-new-entries is the realistic shape. | Reverse-event injectivity clause in `validate_pricing_aliases` (`synth_event → real_event` must be injective). |
| 3 | MINOR | Rotation marker dead on the pool-trip path (points at the just-deleted quote) and lost when the last handled quote was TTL'd/replaced → sweep silently restarts from the front. | `prev_handled` only ever names a SURVIVING quote; trip break resumes after the last survivor. Two regression tests (trip path + deleted-marker). |
| 4 | MINOR | `maker_fee_active_prefixes` bypassed validation: `""` (a stray `- ` in yaml) fees EVERY series; lowercase/whitespace fees none, silently. | Canonical-form validator (non-empty, uppercase, stripped). |
| 5 | MINOR | `pregame_scheduled_starts` keys could silently never match (exact-match lookup vs uppercase event tickers) → tier a2 inert → the final-day in-play misfire returns with a green config load. | Canonical-key validator + loud `pregame_scheduled_starts_active` startup log (mirrors `pricing_aliases_active`). |
| 6 | MINOR | Per-iteration heartbeat beats = N atomic file replaces per 0.5s tick on an N-quote book; on the degraded-disk conditions where wedge detection matters, each write can stall the loop the heartbeat defends. | Write throttle in `Heartbeat.beat()` (100ms dedupe, ≤10 writes/s): failed writes never arm it; an externally-WIPED file heals immediately (supervisor-latch + relaunch-purge semantics preserved, pinned by tests). |
| 7 | MINOR | `structural_leg_deltas` (exposure.py) parsed match/format from the raw FIRST leg → champion-first positions silently fell to the independence proxy (telemetry/hedge-credit visibility, caps unaffected). | Match from the GAME KEY + OR-folded knockout flag (mirrors `_try_build_game`). |
| 8 | MINOR | Drop-settled rehydration also skips the settlement poller's realized-P&L booking + to-the-cent reconcile for the dropped position — silently. | Upgraded to WARNING with the ledger side effect spelled out. Startup-side reconcile pass = owed follow-up. |
| 9 | INFO | `_parse_mt_leg` (structural.py) and `_entity_of` (tripwire.py) read raw tickers — inert for champion legs today. | Alias-resolved for consistency. |

**Regression tests added** (all in `a397efb`): Family-4 all four sign mixes
(aliased pair + real advance pair, incl. the metadata-absent and
metadata-says-not-exclusive backstops), event-fold rejection, waiver
`_settle_specs` includes the aliased champion leg (keyed REAL; unaliased sibling
stays adversarial), conditioning knockout OR-fold with the champion leg ordered
last, config canonicalization (fee prefixes + schedule keys), heartbeat
throttle/heal, rotation trip-path + deleted-marker. **Suite 2223 → 2232.**

## Residuals flagged to the operator (NOT fixed — decisions/verification owed)

1. **Fee in admission EV (policy call):** the candidate gate and last-look EV are
   fee-blind; on a fee-active series a fill with gross edge < fee is admitted
   "+EV" then recorded with negative net `expected_edge_cc`. Inert today (prefix
   list empty). Decide when arming maker fees.
2. **Settlement `fee_cost` semantics:** verify against Kalshi docs/tape whether
   the settlement row's fee includes trade fees (double-count risk, conservative
   direction) BEFORE arming `maker_fee_active_prefixes`.
3. **Analytic ME netting on the final goes comonotone** once champion fills
   coexist with ME-flagged sibling events (≥2 ME events in the game bucket →
   fail-closed). Conservative + monotone; the waiver carries the netting — which
   is exactly why the `_settle_specs` fix mattered. Expect `skip_game_loss_cap`
   utilization to read higher on 7/19; the waiver is the relief path.
4. **INFO:** no startup guard that alias TARGETS stay absent from the live tape
   (if Kalshi lists a real `KXWCADVANCE-*ESPARG`, the alias would double it); a
   metadata-cache assert is a cheap follow-up.

## Relaunch (the ONE restart)

- Static config check FIRST: armed yaml loads under the new validators
  (aliases canonical, allowlist `[KXWC, KXMENWORLDCUP]`, schedule
  `KXMENWORLDCUP-26 → 2026-07-19T18:45Z`).
- Old bot tree killed cleanly (PID 3400 tree incl. supervisor; recorder
  untouched), control files purged, relaunched detached on `a397efb`:
  `data/live_logs/live_20260717_alias_verified.log` (01:01:56Z).
- Startup: `pricing_aliases_active` + `pregame_scheduled_starts_active` both
  logged (the two new loud install records), joint pool warm (8 workers).
- Live verification of first quotes + champion-leg classification: see the
  resume memory / next report section (in flight at time of writing).

## NEXT STEPS

- **Me (now):** live-verify the relaunch (first quotes, champion-leg
  classification mix, nonzero markup on KXMENWORLDCUP legs, `book_risk_pool_warm
  workers=2`, waiver metrics) → update resume memory. Then **Throughput Batch-1
  remainder** (F2 mid-pipeline liveness, F1 monotone pre-pricing gate
  prototype-first, record-after-price fast-lane, F5 snapshot-count fix) via the
  build→verify→fix pattern.
- **Game days 7/18 FRAENG + 7/19 ESPARG:** watch `lastlook_waiver.*`,
  `reprice.pool_trip`, `fill_recovery.*`, champion-leg quoting; **game-cap
  decision after both games** (operator, on measured netted utilization).
- **Operator:** residuals 1–2 before arming maker fees; residual 3 is
  expectation-setting for 7/19.
- **Merge to main** stays gated on the 7/18–19 live validation + the llm-b
  ancestry check ([[feedback_llm_b_quarantine_permanent]]).
