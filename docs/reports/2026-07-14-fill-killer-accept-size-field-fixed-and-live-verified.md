# 2026-07-14 — The fill-killer: accept-size field wrong → 95% of won auctions lapsed. FIXED + live-verified.

**TL;DR.** We were **winning ~60 RFQ auctions per run and throwing 95% of them
away at the confirm step** — not because we quoted too slow or pulled too fast,
but because `_accepted_qty` read a contract-count field that is **null on every
target-cost RFQ (95% of live flow)**. The fix reads the correct
doc-authoritative field (`contracts_accepted_fp` for contracts-mode, else the
accepted side's `*_contracts_offered_fp` for target-cost). **Live-verified: 2
big multi-game combos filled in the first 90 s after deploy, zero
`decline_size_unknown`, zero reconciliation halt.** A second (startup-race) bug
found + fixed along the way. Suite 1736/0.

---

## 1. Symptom (operator report)

> "so so so many FRAESP combos came in yet we filled none of it… the 3 combos we
> filled are very weird and they arent any of the BIG combos that get repeated a
> lot. are we pulling it too fast or quoting it too slow?"

Neither. We were **winning the auction and then declining our own win.**

## 2. The funnel (ph7 tape, cumulative)

```
   9,221 quotes sent
        │
        ▼
    ~62 quotes ACCEPTED   ← we WON the taker's auction ~62 times
        │
        ├──▶  3 confirmed → FILLED
        │
        └──▶ 59 DECLINED at confirm
              └ 57 = DECLINE_SIZE_UNKNOWN   ◄── THE FILL-KILLER
                 detail: 'contracts_accepted_fp unreadable: None'
   ────────────────────────────────────────────────────────────────
   53 / 59 declines were KXMVESPORTSMULTIGAMEEXTENDED
   49 / 59 touched FRA/ESP legs  → exactly the BIG repeated combos
```

| Fact | Evidence |
|---|---|
| **95.1%** of all flow is **target-cost** (dollar target, no contract count) | 397,849 / 418,218 RFQs carry only `target_cost_dollars` |
| Old code read `msg["contracts_accepted_fp"]` for the accepted size | `lifecycle._accepted_qty` (pre-fix) |
| On a target-cost accept that field is **absent/null** → `rfq.contracts` (also None) → **lapse** | 58/58 non-fill declines: `'contracts_accepted_fp unreadable: None'` |
| **Code AND its unit test both used the same field** → tests passed while we bled fills | `test_lifecycle.accepted_msg` hard-coded `contracts_accepted_fp` |
| The 3 pre-fix fills were leftover from an earlier code version (fills DB is cumulative across ph3→ph7 restarts) | fills at 17:30/17:56/18:20, all `KXMVECROSSCATEGORY` |

This is the exact quiet-failure species CLAUDE.md's defenses target: *"an
assumption that is wrong the same way in the code and in its tests, so everything
passes while losing money."*

## 3. Root cause — the real wire fields (docs + live tape)

I first trusted the demo ground-truth fixture (`scenario_accept_no.jsonl`), which
showed `no_contracts_fp` / `yes_contracts_fp` / `contracts_fp`. **That was wrong**
— that record is a *quote-terminal* object, not the raw `quote_accepted` WS
message. Logging the live message proved every one of those fields was null too.

The **authoritative** `quote_accepted` schema
([docs.kalshi.com/websockets/communications](https://docs.kalshi.com/websockets/communications)),
confirmed against the live tape:

| Field | Meaning | Populated when |
|---|---|---|
| `contracts_accepted_fp` | accepted contract count | **contracts-mode** RFQ |
| `no_contracts_offered_fp` / `yes_contracts_offered_fp` | contracts WE offered per side | always (the accepted side's value = the fill on a target-cost RFQ) |
| `rfq_target_cost_dollars` | taker's dollar target | target-cost RFQ |
| `contracts_accepted_fp` | — | **null/absent on target-cost** |

Real captured target-cost accept (KXMVESPORTSMULTIGAMEEXTENDED):
```json
{"accepted_side":"no","no_contracts_offered_fp":"37.27","no_bid_dollars":"0.7450",
 "yes_bid_dollars":"0.0000","rfq_target_cost_dollars":"10.0000"}   // no contracts_accepted_fp key at all
```
The taker accepts our firm quote for the size we offered (which our sizing
computed to cover the target), so the accepted size = the accepted side's
`*_contracts_offered_fp`.

## 4. The fix (`src/combomaker/rfq/lifecycle.py::_accepted_qty`)

Read, in priority order: `contracts_accepted_fp` → accepted-side
`{no,yes}_contracts_offered_fp` → `rfq.contracts` (contracts-mode wire default).
Present-but-unparseable still lapses (defense #2, never guess). Signature now
takes `accepted_side`. Decline detail + a new `quote_accepted` INFO log record
every size field so future wire drift is diagnosable from the ledger/log alone.

**Money-path safety:** the ledger reconciliation gate (defense #3) would HALT on
any booked-vs-actual size mismatch — it did NOT fire on either live fill, which
independently confirms `no_contracts_offered_fp` is the true fill size.

## 5. Bonus bug — supervisor-heartbeat startup race (the "3-min block")

Full-tree restart exposed `_await_supervisor_heartbeat` checking file
**existence**, not **freshness**: a stale `supervisor_heartbeat.txt` left by the
killed supervisor short-circuited the await, so the preflight graded
`external_kill_reachable` on a racing heartbeat → red → refuse → supervisor
emergency-kill → KILL + needs_reconcile dropped. This is the "3 minute block then
spins right back up" the operator saw. **Fix:** baseline the pre-launch mtime and
wait for a beat *newer* than it. Cold-start preflight now goes green first try.

## 6. Verification

| Check | Result |
|---|---|
| Full test suite | **1736 passed**, 3 deselected |
| New regression test | `test_ground_truth_accept_fields_size_target_cost_rfq` replays the real target-cost accept shape (contracts_accepted_fp null, `*_contracts_offered_fp` set) → confirms fill |
| $ drift on pricing | **none** — fix is fill-sizing only; no pricing math touched |
| Live fills (ph10, first 90 s) | 19:49:44 KXMVESPORTSMULTIGAMEEXTENDED 37.27 ct @ 74.5¢; 19:50:19 same-family 43.45 ct @ 84.8¢ |
| `decline_size_unknown` post-fix | 0 |
| Reconciliation halt | 0 (sizes booked cleanly) |
| Bot health | preflight green, heartbeat fresh, no KILL |

## 7. Files touched

| File | Change |
|---|---|
| `src/combomaker/rfq/lifecycle.py` | `_accepted_qty` reads doc-correct fields (+`accepted_side` arg); full `quote_accepted`/`quote_executed_msg` logging; richer size-unknown decline detail |
| `src/combomaker/ops/quote_app.py` | `_await_supervisor_heartbeat` waits for a beat newer than the pre-launch mtime (stale-file race fix) |
| `tests/test_lifecycle.py`, `tests/test_fill_velocity.py`, `tests/test_review_fixes.py` | accept fixtures use the real WS fields; new target-cost regression test |

## 8. Live run state

- **Running:** `combomaker run --env prod --mode quote --confirm-live --config config/prod-live-wc.local.yaml` → `$CLAUDE_JOB_DIR/tmp/live_ph10.log` (bg task `b8t8y2dvl`).
- **Restart procedure (learned this session):** kill the whole tree (supervisor is a child — kill it too or it drops KILL on the heartbeat gap); purge `KILL`, `data/needs_reconcile`, `data/supervisor_heartbeat.txt`; relaunch. The `_await` fix now makes the stale-file purge belt-and-suspenders rather than mandatory.

## NEXT STEPS

- **Owner: bot (autonomous).** Keep filling; watch markouts + settlement on the
  new target-cost fills — these are the FIRST real fills on the big multi-game
  combos, so treat early P&L as data, not proof (never refit on a P&L window).
- **Owner: Claude/next session.** (a) Watch for any `HALT_RECONCILIATION_MISMATCH`
  on the first fills — if the offered-size ≠ actual-fill assumption ever breaks,
  the gate halts loudly; investigate before re-arming. (b) Consider subscribing to
  the order-fills channel (`client_order_id` correlation) for a belt-and-suspenders
  ground-truth fill count. (c) Tighten the temporary full-message accept/exec
  logging once the field is settled (currently dumps the whole msg).
- **Owner: operator (decisions owed).** (1) Advanced-tier upgrade is still BLOCKED
  on eligibility (needs 1 API-created standard Predictions order; combo quotes
  don't count) — we may now qualify via a real fill, re-check. (2) With fills now
  flowing at 1¢ markup, decide whether to hold 1¢ (competitiveness bet, below the
  ~2.2¢ re-grade floor) or widen — from POOLED multi-week evidence, not this
  afternoon's P&L.
