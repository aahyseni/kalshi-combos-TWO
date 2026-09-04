# 2026-09-04 — RELIT 18:45 ET after the four repair builds: gates, ledger repair, first window

Operator ratified fee mode = floor and the soccer btts|total 0.746 promote,
then ordered the relight. Sequence and evidence below. Bot LIVE as of this
report; log `data\live_20260904_1845.log` (D:\kalshi-combos-TWO-data\).

## Gates on merged main (`09cc6cb`)

| Gate | Result |
|---|---|
| Full suite | **4,041 passed / 0 failed** / 3 deselected, 4m10s (baseline 3,778) |
| ruff / mypy | clean except 19 pre-existing ruff errors in `tools/diagnostics/restart_gate2_quote_validation.py` and 4 pre-existing engine.py tuple type-args (both identical on main before the merge) |
| Fee quote-production counterfactual (live yaml + store) | **PASS**: 59,578 recorded quotes replayed, NO tier goes to zero under the floor; ladders 0 moved; razor/mains move −16 to −32 cc where the rebate had exceeded the fee; 140 fills / $3,157 (the post-8/20 negative-after-fee set) re-priced |
| Vitals fast tier | **8/8 GREEN**, 82.6 s |
| Vitals pre-ship tier | **V6 RED = the adjudicated stale-ledger artifact** (5th ship-through; precedents 8/6, 8/13, 8/15, 8/16): the check builds its "live book" from the store's 434 stale open ledger rows (×5 = the 2,170 it reports; 6,930 ms MC vs the 2,994 ms bound) while the exchange holds ZERO positions and the live confirm MC runs on the in-memory exchange-first book. Ledger stale-row repair remains the P1 that retires this red. |
| Pre-relight truth | $4,980.09 all cash, 0 positions, 0 open quotes, no bot processes, no KILL file, DNS + REST reachable |

## Ledger repair (applied 18:44 ET, bot down)

`tools/ops/repair_phantom_fills.py --apply --migrate`: 4,185 store rows vs
4,228 tape fills; **28 phantom rows marked** (`fills.status='phantom'`, their
position_ledger/ev_ledger rows voided), 3 legacy prefix matches kept, 0
unresolved; verification columns + `store_meta` watermark migrated
(watermark_id 4186). Row-level backup + restore script:
`data/backups/20260904T224444Z-phantom_rows_backup.json` / `-restore.sql`.
Listed, NOT repaired: 70 tape-only exchange fills the store never wrote
(writer-path misses) and 7 partial-size mismatches — separate adopt/resize
task.

## Relight

WMI `Win32_Process.Create` → `cmd /c START_BOT.bat` at **18:45:05 ET** (tree
parented outside any session; ReturnValue 0). Operator start purged the 8/27
watchdog latch. Stack verified: bot (2 PIDs, shim + interpreter), fill
prober, hang watchdog (armed 18:46:52 with `reach_host external-api.kalshi.com`
— the new network-class retry path), both monitor windows.

Boot (UTC): quote_app_starting 22:45:08 → fee_schedule_loaded (no persisted
file yet → **taker_fallback 0.0700**, mode floor) → pools warm → needs_reconcile
marker consumed → startup_reconciled + book_reconciled 22:45:42 → preflight
green → WS connected → **quote_warmup_open 22:45:44** → fill_verify_rearmed
(watermark 4186, 0 unproven rows) → position_ledger_divergence (407 stale open
ledger rows vs 0 positions, alarm-only) → **retained_floor_estimate**
published (646 cells, 445 thin, pool floors mlb 5.9¢ / soccer 11.6¢ / esports
26.7¢, z 3.0 → t-quantiles 3.01–3.15, span 31.4 d) → **fee_schedule_refit
22:45:44.98: least-squares 0.035004 → maker_coef 0.0350, 540 charged of 3,000
polled fills, 0 mismatches, collection KXMVECROSSCATEGORY-SHARD1 newly
active** → **fee_series_fee_type: `quadratic_with_combo_maker_fees`, parsed
correctly, multiplier 1** → stale settlement rows closing.

## First window 22:45:44 → 22:51:22Z (5.6 min)

```
minute (UTC)   sends
22:45 (16 s)    156
22:46           407
22:47           452
22:48           443
22:49           474
22:50           447
```

| Metric | Value |
|---|---|
| Sends/min | 407–474 — inside/at the top of the 300–460 benchmark (8/26 relight: 580) |
| Fills | 3 accepted / 3 executed, all NO, SHARD1 |
| **Fill verification (new)** | 3/3 `fill_verification_started` → `fill_verified_on_tape` within 0.2–4 s; 0 unmatched, 0 late, 0 voided |
| **Fee booked (new)** | last fill 30.01 ct @ 68.20¢: `fee_cc` 2,278 = 0.035 × 30.01 × 0.682 × 0.318 = $0.2278 exactly; `expected_edge_cc` 182 (net of fee); status `verified` |
| **Structural telemetry (new)** | 35 `structural_fits` rows, all ACCEPT (table had 0 rows in its lifetime before today) |
| HTTP 400 / insufficient_balance | 0 / 0 |
| 429 | 4 (boot-time `exchange_status_rate_limited`, known transient) |
| Halts / errors | 0 / 0 |
| Warnings | metadata_fetch_failed 761 (known boot class), settlement_orphan_row_ambiguous 119 (stale-ledger class), risk_starvation_watchdog 23 (cold-cache warmup class), 1 store_writer_checkpoint_failed (213 GB store — item 7, parked) |
| Decline census | skip_per_combo_loss_cap 1,319 · skip_size_above_max 659 · skip_structure_loss_cap 618 · skip_portfolio_cvar 174 (the 1% whale rule = top wall, as before) |
| Book risk | n_positions 3, p_book 0.377, **p_ruin 0.0** (the 8/26 p_ruin=1.0 reading is gone on the fresh book — keep watching as the book fills), ES99 $32.63, det backstop $3,508.57, realized −$0.42 (= the three fees) |
| Equity | $4,980.09 (unchanged; 0 modeled positions at the standing read) |

## What changed on the wire vs 8/26

- Every fill now carries the real maker fee in the ledger and a fee-net
  expected edge; every EV gate (confirm admission, eviction, KILL-marginal)
  judges net of fee.
- No bid can retain less than the fee (floor mode); the 0.6¢ razor is
  effectively max(0.6¢, fee) — the counterfactual shows this moves razor bids
  by ~0.2¢ and leaves every ladder untouched.
- The skew rebate is bounded by the measured per-cell floor; on populated
  cells it allows no rebate at the policy z (operator ratified).
- Club soccer btts×total pairs price on the club-measured correlation and
  the structural path with a derived fit bar; the fallback is now recorded.
- A fill that Kalshi reports as executed but that never appears on
  `/portfolio/fills` is voided within ~30 s (live 15 s cadence) instead of
  living in the ledger until a settlement mismatch halts the bot.
- A boot that dies on a network error is retried behind a reach probe
  instead of latching the watchdog.

## NEXT STEPS

- Watch (first hours): sends/min stays 300–460 through the evening slate
  (soccer btts×total memo-miss cost 79 ms — first club slate is the test);
  `fee_schedule_mismatch` stays 0 on every charged fill; `fill_verified_late`
  stays 0; `structural.fallback.*` counters + `structural_fits` verdict mix;
  `ledger_quantity_mismatch` legacy-only; p_ruin as the book builds.
- Owed: ledger stale-row P1 (407 stale open rows; retires the pre-ship V6
  artifact); adopt the 70 tape-only fills + resize the 7 partials; the
  9/1 pre-registered reads (favorite-band pooled + razor) on the truncated
  window; the parked items 6 (stall wall) and 7 (store rotation) — the
  first `store_writer_checkpoint_failed` already logged at boot.
- Pre-registered watches from the deep dives: NO 0.65–0.85 favorite band by
  sport; cross-game btts×total; KS line-6 cell; NO-side prop/RFI leg fairs.
