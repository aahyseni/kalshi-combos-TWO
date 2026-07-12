# Risk FOUNDATION — B1 (side-aware axes) + B2 (game clustering) + BalanceTracker

**Date:** 2026-07-12 · **Branch:** `risk-foundation` · **Baseline:** 616db6a
(suite 1294/0) · **Result:** suite **1325/0**, mypy strict + ruff clean on touched
files.

One coherent foundation commit implementing the three "get right" items from the
R1/R2 research docs. Every number is anchored to the **2026-07-10 demo combo
settlement** ground truth: a LONG NO of 1.00 contract that COST $0.50 and PAID
$1.00 when the combo settled NO (realized +$0.50); balance 1,082.62 → 1,083.62.

---

## B1 — side-aware max_loss + a separate payout_obligation axis

`risk/exposure.py`, `OpenPosition`:

| Axis | Formula | Demo (1.00 NO ct paid $0.50) | Feeds |
|---|---|---|---|
| `max_loss_cc` (LOSS) | `contracts × entry_price_cc // 100` | **$0.50** | daily-loss / genuine-P&L-at-risk caps |
| `payout_obligation_cc` (BANKROLL) | `contracts × $1 // 100` | **$1.00** | R2 cluster/tail/utilization caps (seam left) |

**Why `max_loss_cc` needed NO formula change, only verification.** We are
sell-only, LONG NO. Both sides of our quote are bids; Kalshi never margin-calls a
bought contract. When the parlay HITS (settles YES) our NO expires worthless and
we forfeit **exactly the premium we paid** — not the $1 payout (the taker collects
that out of the collateral the *taker* posted for their YES). So premium-paid IS
the true, side-aware max loss on the NO side. The 2026-07-10 settlement confirms:
paid $0.50, and a HIT would have lost exactly $0.50. **E3 PROMOTED to VERIFIED**
(was "re-check vs ground truth"). The docstring is now explicitly side-aware; the
math is unchanged and bit-identical (parity trivial).

**The NEW axis.** `payout_obligation_cc = contracts × $1` is the gross bankroll
lock-up — the "$23.5M payout for $1.8M premium" dimension the P&L sweep flagged. A
real capital-utilization constraint, but **not a loss**. Carried on a distinct
field and **NEVER summed with `max_loss_cc`** (R1/R2 correctness invariant #2).
Verified: 1.00 ct → $1.00, and price-independent (E9, VERIFIED).

Both axes are also aggregated per game in the snapshot: `worst_case_loss_by_game_cc`
(premium comonotone worst case, kept) and the new `payout_obligation_by_game_cc`.

**Consumers audited.** `limits.py` reads `worst_case_loss_by_game_cc` (loss axis)
for the existing worst-case-loss cap — correct (R2: leave the payout cap to R2, but
the seam is now present and commented). `lifecycle._refresh_daily_pnl` uses
`max_loss_cc` as *cost* in the MTM (unrealized = value − cost) — correct, that is
genuinely the premium paid. No consumer wanted payout where it currently reads
premium; the payout axis is additive and waits for R2's caps.

---

## B2 — public game_key + game-clustered aggregation

- **New** `src/combomaker/pricing/grouping.py` exports the pure `game_key`
  (`SERIES-GAMECODE → GAMECODE`, no-hyphen → identity/fail-closed).
- `relationships._game_key` is now `= game_key` (a single definition, re-exported
  under the private alias its many call sites use). A **parity test**
  (`tests/test_grouping.py`) pins them identical — zero drift.
- `exposure.py` snapshot now keys every per-event aggregate on
  `game_key(leg.event_ticker)`. The old field names `delta_by_event` /
  `worst_case_loss_by_event_cc` are **back-compat properties** returning the
  game-keyed data; new code uses `delta_by_game` / `worst_case_loss_by_game_cc` /
  `payout_obligation_by_game_cc`.

**Clustering proof** (`tests/test_exposure.py::TestB2GameClustering`): a
`KXWCGAME-26JUL05MEXENG-*` leg and a `KXWCTOTAL-26JUL05MEXENG-*` leg — two market
FAMILIES of ONE match, two distinct event_tickers — now land in **ONE** game
bucket keyed `26JUL05MEXENG`:

```
worst_case_loss_by_game_cc  == {"26JUL05MEXENG": 5_000 + 4_000}   # both premiums
payout_obligation_by_game_cc == {"26JUL05MEXENG": 10_000 + 10_000} # both $1/ct
delta_by_game keys           == {"26JUL05MEXENG"}
```

Pre-B2 these split into two event buckets — the per-game correlation the operator
cares about was invisible (R1 gap G1). A distinct game stays separate; an ungamed
(no-hyphen) event never merges (fail-closed).

**E2 mass-acceptance dominance PRESERVED verbatim.** The sign-aligned worse-side
bound is applied identically on every axis (delta, loss, payout), now per game.
`TestMassAcceptanceDominance` (200 hypothesis examples) still passes.

---

## BalanceTracker (`src/combomaker/risk/balance.py`)

Interface:

```
BalanceTracker(conventions, clock, *, stale_after_s)
  await refresh(source) -> CentiCents        # poll get_balance -> bankroll_cc
  is_stale -> bool                           # no poll, or older than stale_after_s
  bankroll_cc -> CentiCents                  # raises StaleBalanceError when stale
  bankroll_cc_or_none() -> CentiCents | None # non-raising (display/logging)
  apply_settlement(Settlement) -> int        # advance realized ledger; idempotent
  realized_pnl_cc -> int                     # signed cumulative realized P&L
  cumulative_loss_cc -> int                  # running sum of realized LOSSES
  settled_count -> int
```

- **Live bankroll** parsed from `/portfolio/balance` — prefers the exact
  `balance_dollars` fixed-point string, falls back to int-`balance` (cents → cc).
  Any parse doubt raises (never guess a bankroll). A bad poll leaves the last good
  value AND its freshness stamp untouched; after `stale_after_s` the frozen value
  goes stale and `bankroll_cc` raises → every %-of-bankroll cap fails closed
  (CLAUDE.md hard rule 6). This is the source of the bankroll figure the R2 caps
  will scale from.
- **Realized ledger** advanced on settlement: LONG NO MISS (settles NO) credits
  `+$1/ct − premium` (demo: **+$0.50** exact); LONG NO HIT (settles YES) debits
  the premium; idempotent per `position_id`. A NO credit is **gated on
  `Conventions.combo_no_pays_complement` being verified True** — refuses otherwise
  (defense #1). The ledger is an INDEPENDENT cross-check, never summed into the
  live bankroll (the live poll already contains the money).
- Tested with `FakeClock` + `FakeBalanceSource` — **no live credentials**.

---

## Files changed

| File | Change |
|---|---|
| `src/combomaker/pricing/grouping.py` | **NEW** — public `game_key` |
| `src/combomaker/pricing/relationships.py` | `_game_key` → re-export of `game_key` (import added) |
| `src/combomaker/risk/exposure.py` | B1 axes on `OpenPosition`; snapshot game-keyed + payout axis; docstring |
| `src/combomaker/risk/limits.py` | consume `*_by_game*` names; "game" wording; R2 seam comment |
| `src/combomaker/risk/balance.py` | **NEW** — `BalanceTracker` |
| `tests/test_grouping.py` | **NEW** — parity + behaviour |
| `tests/test_balance.py` | **NEW** — parse, staleness, ledger (ground-truth credit) |
| `tests/test_exposure.py` | B1 ground-truth axes + B2 clustering classes |
| `tests/test_limits.py` | breach-detail wording (event→game) |
| `NOTES.md` | E3 promoted VERIFIED; E9/E10/E11 added |

Suite: **1325 passed / 0 failed / 3 deselected** (baseline 1294). mypy strict:
0 errors on touched files (2 pre-existing `ising_amm.py` numpy-return errors are
unrelated and present on baseline). ruff: clean.

---

## DESIGN DECISION (flag for operator)

**Bankroll vs realized-ledger separation.** The demo shows the fill DEBITS the
premium and the settlement CREDITS the gross payout — so a live `get_balance`
poll already reflects both. To avoid double-counting, `BalanceTracker` treats the
**live poll as the authoritative `bankroll_cc`** and keeps `realized_pnl_cc` /
`cumulative_loss_cc` as a **separate running tally** (a cross-check queryable at
any instant without a poll), never added to the bankroll. This matches R2 §1
("live balance poll is authoritative"). If instead the operator wants a
*synthetic* bankroll driven purely by the ledger (start balance + realized), that
is a small addition, but it would drift from the exchange ledger and lose the
fail-closed-on-stale guarantee — I chose the exchange-authoritative model
deliberately. **Decision owed: confirm exchange-poll-authoritative is intended.**

Secondary: the `*_by_event` field names are kept as aliases over game-keyed data
rather than deleted, so nothing downstream breaks; new code should prefer
`*_by_game*`. Flag only if you'd rather hard-remove the old names now.

---

## NEXT STEPS

- **Owner: R2 build (engineering).** Consume the new axes: a per-game payout /
  bankroll-utilization cap on `payout_obligation_by_game_cc` (scaled by
  `BalanceTracker.bankroll_cc`, fail-closed on stale) and a per-combo payout cap
  on `OpenPosition.payout_obligation_cc`. The loss-axis cap on
  `worst_case_loss_by_game_cc` already exists. Seam + comment are in `limits.py`.
- **Owner: wiring (engineering).** Poll `BalanceTracker.refresh()` from the
  status loop / maintenance tick; apply `apply_settlement()` from the settlement
  path so the realized ledger stays live.
- **Owner: operator.** The DESIGN DECISION above (exchange-poll-authoritative
  bankroll) and R2's OPERATOR-SET numbers (baseline bankroll, cap %s) — unchanged
  by this build, still owed before caps go live.
