# 2026-07-25 — FULL DAY REPORT + SESSION-STATE RESUME (handoff)

**Read this first, then `docs/reports/README.md` newest-first.** Everything
below is committed and pushed through **`01b265d`**. Suite **2782/0**, mypy
strict clean. Bot is **DOWN** at handoff (see §0).

---

## §0 STATE AT HANDOFF (the five things a new session must know)

1. **The bot is DOWN.** It halted 7:32p ET on `halt_metadata_change` (third
   halt of that class today). The KILL file + `needs_reconcile` marker are
   still in place; the fix for the halt cause is committed (`01b265d`).
2. **A restart is BLOCKED pending operator confirmation** (operator: "only
   restart the bot after they've been reviewed and confirmed"). An attempted
   recovery restart was correctly refused by the permission layer.
3. **The armed YAML** (`config/prod-live-wc.local.yaml`, gitignored, NEVER
   commit) already has the full evening bundle switched ON, so the next
   restart arms all of it at once — see §4 for the exact list and §5 for the
   one HIGH finding that is still open.
4. **Today's P&L**: realized **+$29** (process-scoped counter), open book 16
   positions, tail ES99 ~$172 vs ~$235 budget, p_book/p_night ~0.34 at the
   halt. Full settlement reconciliation for the day is NOT done (owed).
5. **Open positions carry live risk while the bot is down** — they settle on
   their own; nothing is at risk of runaway, but nothing is being managed.

Recovery command sequence (after the operator confirms):
`STOP_BOT.bat` → delete `KILL` → `START_BOT.bat` → watch the MONITOR window
for `prod_preflight_green` (the `needs_reconcile` marker clears itself on a
successful boot reconcile).

---

## §1 WHAT SHIPPED TODAY (chronological, all pushed)

| # | commit | what |
|---|---|---|
| 1 | `aeba163` (prev session, verified) | incident-C partial-fill fix |
| 2 | `410e8bf` | **leg-direction axis** (family/entity exposure + skew steer, shadow), **persistent metadata cache**, decline `detail` in audits, one-click launchers |
| 3 | `8e07076`,`4aedc90`,`a82df98`,`b61a411`,`5dc0b0f`,`ae66d7e` | launcher hardening: stale-heartbeat purge, ASCII-only PS1, correct entrypoint (`cli run`, not the supervisor), launch mutex, **venv-shim doubling misdiagnosis retracted** |
| 4 | `6e054b0`,`716713a` | **P(KILL-night) ≤2% book gate** (new operator anchor) + metadata-breaker end-of-life exemption + first warm boot |
| 5 | `5a47e9f` | **DECLINES.bat** — plain-English decline reader |
| 6 | `fdc9f6d`,`0ff3376` | **renege bundle** (award-true sizing, admission EV from the fresh calibrated fair, game-scoped waiver stability, release accepted-quote exposure) + big-fill audit report |
| 7 | `35dddad`,`6936224` | **p_night KPI** + day-anchored realized seed; **P(book) non-decrease doctrine gate** v1 → **v2 independence benchmark** |
| 8 | `c7e680c`,`01b265d` | breaker: terminal-status exemption, then **earliest-horizon** fix (third halt) |
| 9 | `859cd00` | **EV-based slot eviction** |
| 10 | `8dc4b47` | **in-play shadow instrumentation** (measurement only) |

## §2 THE THREE BIG FINDINGS OF THE DAY (each measured, not theorized)

1. **We were winning auctions and reneging.** 49 won / 15 filled; $355 of
   taker premium won-then-declined. Causes: quote-time risk sized
   target-cost fills at OUR bid (3.6–4.7× understated vs the exchange's
   award at the taker's price); the admission gate's band-high copula scored
   same-game combos −EV where the calibrated pricing fair said +EV; the
   accepted quote's own resting entry double-counted the fill at confirm.
   All four fixed (`fdc9f6d`).
2. **The ES99-average book bound punished diversification.** At small N with
   coin-flip positions the worst 1% is "most lose together" even when
   independent, so a diversified book was pinned at ~$245 premium. Replaced
   (operator-ratified) with **P(KILL-distance night) ≤ 2%**.
3. **Concentration was invisible on the leg axis.** Half the book was short
   pitcher-K overs on the same arms across games (~$127 on one pitcher).
   Built the family/entity axis; armed for the next restart.

## §3 ANSWERED OPERATOR QUESTIONS (so they are not re-litigated)

- **"Why did p_book drop 0.81 → 0.51?"** Winning positions SETTLED OUT of
  the book (p_book only measures what is still open). That is why `p_night`
  (realized + open) now exists as the headline KPI.
- **"Why is p_book falling while we take diverse fills?"** The held book was
  marked ~−$51 as tonight's games moved toward our short legs' YES side.
  Post-entry variance, not bad admission (the prober had us +1–3¢ rich at
  entry all day).
- **"Is p_book computed off the ENTIRE book?"** It is computed off **all
  committed positions**, in-play games included (the pregame gate only
  blocks NEW quotes). **One caveat to close:** positions marked
  `risk_modeled=False` (conservatively-reserved holdings with no subscribed
  leg books) are excluded from the sampled MC and carried as a deterministic
  reserve — if any such holding exists, p_book is computed on a strict
  subset of the book. None were present at the last snapshot
  (`adopted_as_reserve: []`), but a **rolling all-positions guarantee needs
  an explicit invariant + alarm** (queued, §6).
- **"Why so few $20–50 fills?"** The renege zone (see §2.1), not the size
  caps. `skip_size_above_max` only ever hit $500+ whales.

## §4 ARMED IN THE YAML, WAITING ON THE CONFIRMED RESTART

| flag | effect |
|---|---|
| `risk_qty_award_sizing` | size candidates at the exchange's award |
| `gate_ev_from_pricing_fair` | admission EV = FRESH calibrated fair (pickoff-safe; source audited per fill) |
| `waiver_game_scoped_stability` | unrelated-game fills stop killing waiver certificates |
| `release_accepted_quote_exposure` | accepted quote's dead resting entry stops double-counting |
| `require_p_book_non_decreasing` (**v2**) | refuse fills measurably worse than an independent twin |
| `open_quote_ev_eviction` | at the slot cap, evict the weakest-EV resting quote |
| `pricing.skew.leg_axis_armed` | price against cross-game family/player stacking |
| `filters.inplay_shadow_enabled` | record would-quotes for in-play flow (NO quotes sent) |
| *(already live)* | `portfolio_tail_prob_gate`, `pbook_armed`, `allow_negative_ev_hedge`, `hedge_budget_tail_derived`, `lastlook_waiver_slate_axis` |

YAML parse + duplicate-key check passed after the edits.

## §5 OPEN FINDING FROM THE REVIEW (must not be forgotten)

**HIGH — the day-anchored realized seed is a production no-op.**
`Store.day_realized_pnl_cc` reads `position_ledger`, but **nothing in live
code writes that table** (`record_position_open` / `record_position_settled`
have no production callers; the settlement handler is constructed without a
store). So `p_night`'s restart-roll does not actually work yet, and the log
line `realized_pnl_day_seeded realized_cc=0` reports success regardless.
Bounded impact (nothing gates on p_night), but it must be wired:
`risk/settlement.py:383` booking point + position-open booking at fill.
**MEDIUM (same area):** settlements that occur while the bot is DOWN can
never be booked at all — the poller only matches settlements against
in-memory positions, and rehydration restores only OPEN ones. A startup
reconcile pass over ledger rows still `status='open'` is required for the
seed to work on the operator's actual pattern (bot down through a slate).

*Note: 12 of the review's verify agents aborted on a monthly spend limit, so
the EV-eviction and v2-gate lenses are only partially verified. Re-run that
review before trusting those two beyond tonight.*

## §6 QUEUED WORK (priority order)

1. **Wire the position ledger** (§5 HIGH + MEDIUM) — makes p_night real.
2. **Re-run the aborted review lenses** (EV eviction, v2 gate).
3. **All-positions p_book invariant + alarm** (§3, reserved-holding caveat).
4. **Dissolve `max_open_quotes: 200`** into measured capacity (sweep-tick
   latency + write-budget headroom); eviction is only half the fix.
5. **In-play adverse-selection read-out** after ≥1 slate of shadow rows
   (`would_quotes_inplay` joined to settlements) → decides any arming.
6. **Entity-axis BOUNDS** (walls, not just pricing).
7. **Persistence writer backlog** (~40 min behind live at peak — blinds
   flow-loss measurement).
8. Post-slate P&L + settlement reconciliation for 7/25; adaptive-caps
   `pnl_history` feed; slate 65%→80% decision (deferred: not binding alone).

## §7 OPERATIONAL NOTES / TRAPS LEARNED TODAY

- `START_BOT.bat` / `STOP_BOT.bat` / `DECLINES.bat` / `READOUT.bat` are the
  operator's controls. Start refuses if a bot is already live; stop kills
  supervisors first and reaps orphaned pool workers.
- **The venv `python.exe` is a shim that spawns a second identical process** —
  process listings always show pairs. Never diagnose "duplicate stacks" from
  a raw count; count roots (parent not in the matched set).
- `tools/ops/*.ps1` must stay **pure ASCII** (PS 5.1 reads BOM-less files as
  ANSI; an em dash terminates a string).
- Commit messages with quotes must go through `git commit -F <file>`.
- The metadata-change breaker has now cost three halts in one day. Its
  horizon rule is fixed (earliest tz-aware stamp) but it remains the most
  halt-prone component; watch it first after any restart.

## NEXT STEPS

- **Operator**: confirm the §4 bundle → recover the bot (§0 sequence).
  Decide whether to keep `require_p_book_non_decreasing` armed on the first
  night (it is v2; v1 misfired and was disarmed live).
- **Claude (next session)**: §6 in order, starting with the ledger wiring;
  then the 7/25 settlement reconciliation once the slate finishes.
