# WC backtest harness — strictly-pregame filter + tape-sourced clearings (gap backfill)

**Date:** 2026-07-09 ~18:45 UTC · **Where:** `tools/backtests/wc_backtest.py`
(engine untouched — CLAUDE.md rule 8) · **Status:** built + live-validated.

## What changed (and why)

Two operator asks landed on the same code path, so they were built together:

1. **"Exclude combos that have legs of a LIVE game — strictly pregame."**
2. **"Backfill the ~10h combo-trade gap"** (the recorder's combo-trade poller
   silently stalled 08:19→18:11 UTC on Jul 9; `combo_trades` stopped while the
   RFQ tape kept flowing).

Both are solved in the harness's **`gather`** stage — nothing in `src/` was
touched. The clean insight: **combo clearings can be read straight from Kalshi's
trade tape** (`get_trades(ticker=…)`), which is **complete and gap-free**. So we
no longer read the DB's `combo_trades` table at all — the poller stall disappears
**by construction** (we source the outcome from Kalshi, not from the thing that
stalled), and there's no separate "backfill" step to run.

## The new gather pipeline

```
 recorder DB (rfqs + would_quotes — never stalled, read off-peak)
   │  combos (WC-strict: every leg KXWC*), sides, marginal snapshots
   ▼
 inputs.pkl  ── pricing inputs, NO prices ──────────────► price stage (blind)
   ▲
   │  (kept in a SEPARATE file — the pricer can't reach outcomes)
   ▼
 Kalshi trade tape  get_trades(combo)   [AUTHED: prod signer]
   │  clearings = (yes_price_dollars, count_fp, taker_side, created_time)
   │  COMPLETE → backfills the 10h poller gap by construction
   ▼
 Kalshi market read  get_market(leg)    [public]
   │  settlement (status/result)  +  expected_expiration_time
   ▼
 STRICTLY-PREGAME filter  ─────────────────────────────► outcomes.pkl
   keep a print iff  created_time < cutoff
   cutoff = MIN over legs of ( expected_expiration_time − PREGAME_HOURS )
```

### The pre-game cutoff (an ESTIMATE, documented as such)

Kalshi **does not expose kickoff** (verified source-of-truth: a leg's
`close_time` is the far settlement window — e.g. Aug 06 — and the game *event*
carries no start field). The only time anchor it gives is
**`expected_expiration_time` ≈ game-end / settlement**. So kickoff is estimated:

```
kickoff_est(leg) = expected_expiration_time(leg) − PREGAME_HOURS      (default 2.5h)
cutoff(combo)    = MIN over legs of kickoff_est(leg)   ← earliest game to start
```

- **`MIN` over legs** is what makes it *strictly* pre-game: a multi-game combo is
  admitted only while **every** leg is still pre-game, i.e. up to the moment the
  **first** of its games kicks off. Any print at/after that is dropped; a combo
  with zero pre-game prints is dropped entirely.
- **Why 2.5h, and why it's conservative:** soccer regulation ≈ 1h50 + a
  settlement buffer, so ~2.5h before expiry lands near kickoff for
  regulation-settled markets. Advance/ET markets expire *later* (they include
  extra time / pens), so `expiry − 2.5h` sits **before** true kickoff for them —
  the estimate is **≤** the real kickoff, so the filter only ever **drops**
  borderline pre-game prints, **never admits an in-play one**. Erring strict, as
  asked.
- **Tunable:** `--pregame-hours N` (larger = stricter). Not baked in; it's an
  estimate, not a fixture. A real fixture schedule could replace it later for
  cent-precision, but the conservative estimate is safe today.

## Validation (live, against real Kalshi — 2026-07-09 ~18:40 UTC)

Ran the **actual harness functions** (`_fetch_clearings`, `_fetch_leg_meta`,
`_parse_ts`, the cutoff logic) on 8 real WC combos + their 27 distinct legs:

| Check | Result |
|---|---|
| `_fetch_clearings` returns real prints w/ correct fields | ✅ `yes=0.5740 n=25.37 yes @ 18:35`; dollars + count_fp + side + time |
| pagination (combos with >1000 prints) | ✅ one combo returned **2,119** prints (3 pages), span Jul08 05:21 → Jul09 18:42 |
| `_fetch_leg_meta` returns settlement + expiry | ✅ every leg: `status=active`, `exp=2026-07-09T23:00:00Z` (FRAMAR) etc. |
| cutoff binds on earliest leg | ✅ multi-game combos (FRAMAR Jul9 + ESPBEL Jul10 + ARGSUI Jul11) → cutoff **20:30 Jul9** (France–Morocco kickoff) |
| drop path direction (monotonic) | ✅ same 2,119-print combo: cutoff 04:00→**588** pre / 12:00→**835** / 18:00→**1,931** / 20:30→**2,119** |

The drop test proves the comparison direction: earlier cutoff ⇒ more prints
dropped; prints strictly before the cutoff are kept, prints at/after are dropped.
(Live `dropped=0` right now only because it is currently *before* the 20:30
cutoff — no in-play prints exist yet for Jul9-earliest combos.)

`ruff check` clean; `py_compile` clean.

## Empirical corroboration — the DB gap self-healed (2026-07-09 19:07 UTC)

A read-only count of `combo_trades` after the recorder restart is independent
evidence that the tape is the right source. The 08:20–18:11 window that was
**empty at 18:04** (the poller had stalled at 08:19) now holds **7,987 trades** —
recorder2's REST `get_trades` poller pulled the backlog on restart, and trades
keep their original `created_time`, so the window filled back in. Same mechanism
this harness uses; it just does it exhaustively per combo. Current DB state:
**168,707 trades / 8,820 distinct combos** (all sports), span Jun 29 → now. (The
`combo_trades.stored` metric counter, 33,788, counts store *ops* incl. re-stored
duplicates, so it sits above the distinct row count — not a row count.)

## Rule-8 / zero-bias posture (unchanged, reaffirmed)

- **Engine PRISTINE** — all changes are in `tools/`. The price stage still imports
  and calls the SAME live pricing modules + shipped `PricingConfig`; the
  ~15-line dispatch copy keeps its "keep in sync" note + parity check.
- **Zero-bias by construction** — clearings live in `outcomes.pkl`; the price
  stage reads `inputs.pkl` only and never opens outcomes. Sourcing clearings from
  Kalshi instead of the DB does **not** change this: they still land only in the
  outcomes file, seen for the first time in `analyze`.

## Recorder health at time of writing

Alive and both feeds rising (verified via the log, not the DB): between two 60s
snapshots `combo_trades.stored` 8658→9171, `rfq.would_quote` 41907→42477,
`rfq.created` 53994→54700. The would-quotes/marginals feed (which the tape
**cannot** backfill) is being captured — keep the recorder up through Jul 11.

## NEXT STEPS

- **Owner (next session), after WC settles (Jul 9–11):** run the full pipeline
  OFF-PEAK — `gather --since 2026-07-01` (reads rfqs/would_quotes; pulls clearings
  from the tape + settlement/expiry from Kalshi; applies the pre-game filter) →
  `price` → `analyze`. Confirm `analyze` reports a healthy count of combos with
  pre-game prints and a resolved subset.
- **Owner (operator):** decide whether 2.5h is the right default OFFSET or if you
  want it stricter (bump `--pregame-hours`); optionally provide a real WC fixture
  schedule to replace the estimate with exact kickoffs.
- **Owner (next session), ongoing:** keep the recorder alive through Jul 11
  (watermarks rising); still worth hardening the combo-poller stall (catch+restart
  the task) even though the tape now backfills clearings — the would-quotes feed
  has no backfill.
