# 2026-08-14 — The $2.8–2.9k ceiling attributed + the rolling-quote cycle measured

Two operator questions answered from the tape (read-only; 16,011 snapshot
samples, 8/11–8/14 logs, store `mode=ro`; full tables in the analysis
artifacts).

## Q1: "We're allowed 70% (~$3.2k) but always stop at $2.8–2.9k" — SOLVED

**Both numbers are real walls; they're different walls.**

| wall | value | $ at bankroll $4,479 | behavior |
|---|---|---|---|
| SLATE cap (`slate_loss_frac: 0.65`) | 65% | **$2,911** | first to bind — THE observed stopping band; waivable + partitioned, so the book grinds slowly past it on waivers |
| det-max RUIN BACKSTOP (0.70 × B, the armed readingb demotion) | 70% | **$3,135** | the hard, level, non-waivable ceiling — what the operator remembers as "70%" |

Receipt (8/12): slate skips began firing exactly as det crossed $2,904
(=0.65B); the book then ground upward on waivers and PINNED at **100.07% of
the 0.70B backstop** ($3,154 vs $3,152) until settlements graded. While
pinned: 98k slate skips/2h, sends collapsed 23k/hr → 10.5k/hr. On
whale-heavy 8/13 the book stalled LOWER ($2,777) on per-candidate walls
(mass-acceptance deltas 363k/hr, entity 3% = $134, size) before either
aggregate ceiling mattered.

**The slate 65→80 decision (open since 7/25):** raising it removes the
$2,911→$3,135 waiver-grind band (faster, cheaper climbs) but only lifts the
effective ceiling by **+$224 (+7.7%)** — the 0.70B backstop binds next.
A ceiling above $3,135 means moving the 30% ruin-floor anchor itself
(layer-2 constitutional, operator-only).

**Flag:** the R1 static hand-numbers still live under
`skip_mass_acceptance_breach` (gross $5,000, $1,000/game, **and the static
$500 daily-loss halt the operator ruled DELETE on 8/12** — 8/13's −$473
came within $27 of tripping it). The deletion was parked under the 8/31
freeze; recommend executing it now under the same carve-out logic as the
levers (operator's own standing ruling).

## Q2: The rolling-quote cycle — measured, and the squeeze is NOT P(book)

- **p_book does NOT drop late**: late-window (22:00–02:00) p_book means
  0.47–0.53 vs midday 0.39–0.51. The lows are at the EVENING build peak
  (0.335 at 95–97% det utilization), not late.
- **The real late wall is EXCHANGE CASH**: 36–68% of late-window quote
  creates fail `insufficient_balance` (HTTP 400) — and it's exploding:
  33.7k failures (8/11) → 129.9k (8/12) → 225.0k (8/13), peaking 7.2/sec.
  Cash sits in unsettled positions + resting collateral; the bot discovers
  the wall by erroring and burns write budget doing it.
- **Freed headroom rolls FORWARD, not sideways**: of $2.0–2.9k det freed
  nightly at settlements (21:00–02:00), only 6–16% goes into the same
  night's late (≥21:30 ET) games — 79% of late-game books are built MIDDAY.
  The same-night redeploy window (settlement waves 21:00–22:00 vs west-coast
  first pitch 21:38–22:10) is 30–90 min wide and the balance wall is up
  through it.
- **The 8/13→8/14 overshoot**: the night redeployed **146%** of what
  settlements freed (next-day pre-positioning) → this morning's book pinned
  at 99.3–100.3% of the backstop with p_kill_night 0.36–0.39 — **the
  overnight parked book now carries more kill-risk than the live evening
  book**. (This is exactly the state the newly-armed gates now manage: at
  the ceiling, reducers-only until settlements free room.)
- **Det parking by family**: MLB recycles capital in 9.9h median; esports
  parks 31.6h median (currently 11.6% of the backstop across 10 positions);
  club soccer negligible so far ($2.06, one 40h MLS weekend parlay) but the
  mechanism scales with weekend volume.

### Design ideas surfaced (design-only, no code — for the operator's queue)

A. **Balance-aware quoting** (highest impact): track available exchange cash
   locally, pre-check candidate collateral, size quotes to fit — kills the
   400-storm and converts the discovered wall into a priced input.
B. **Settlement-anticipation credit**: a game that is factually FINAL but
   not yet exchange-settled grants headroom credit (haircut by the measured
   reconcile-lag) — the only mechanism that widens the same-night redeploy
   window from the left.
C. **Slate-clock cohort budgeting**: release det/cash by settlement cohort
   (same-night vs next-day vs multi-day) at measured fractions — stops
   overnight pre-positioning from consuming >100% of the nightly free-up.
D. **Det-day carry pricing**: markup proportional to expected det-HOURS
   parked (MLB 9.9h vs esports 31.6h vs weekend club ~40h+) — multi-day
   positions pay for the recycling capacity they foreclose.
E. **Priority settlement-resolution reads 21:00–02:00** — the resolution
   loop is read-budget-starved exactly when freeing cash matters most
   (235 stale "open" ledger rows / $4,787 median 62h past their games).

## NEXT STEPS

- **Operator decisions surfaced:** (1) slate 65→80 (now precisely priced:
  +$224 ceiling, removes the grind band); (2) execute the already-ruled
  $500-stop deletion; (3) rank design ideas A–E (A and E are small and
  freeze-compatible as mechanism repairs of measured defects; B–D are 9/1
  candidates).
- **Me:** markup research readout when the fleet lands; direction-net shadow
  read after tonight's slate; first-quote-after-arming confirmation via the
  standing monitor.
