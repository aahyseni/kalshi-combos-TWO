# 2026-08-14 — VARIANCE LEVERS ARMED (operator: "Restart, arm and monitor")

**Armed at the 09:33 ET boot on `7a742e5`** after all three pre-arm gates
passed. Bot LIVE, preflight green, zero halts; the marginal KILL/ruin
constitution + the P1 structure cap + the acceptance seed all govern for the
first time.

## Pre-arm gates (all PASS, in the quiet window)

1. **Seed dry-run on the LIVE store:** 107,440 quote_sent / 88 matched
   accepts reconstructed from the real 24h tape (0.13% unjoinable, 187s
   cold) → `discriminating() TRUE`, every bucket CP-lower > 0
   (0.000137–0.001156). The 8/1 day-one 0.6% brick is dead.
2. **Whale replay at the live bankroll:** the 8/13 −$177 (271ct) and −$137
   (282ct) shapes → `SKIP_STRUCTURE_LOSS_CAP` REFUSED at the 1% anchor
   (~$45); the median $9.19 ticket passes untouched.
3. **Pre-ship vitals, quiet machine: 1/1 GREEN (530.7s)** — the standing
   inherited 5×-book RED *cleared* at today's book size.

One transient at the first start attempt: `database is locked` at
`Store.open`'s WAL pragma (a boot race with the watchdog's store probe — the
pragma runs before busy_timeout is set). Clean STOP→START resolved it;
mechanism note filed below.

## Armed lines (live yaml, backup + fallback tag stand)

`kill_anchored_book_gate: true` · `acceptance_seed_from_store: true` ·
`portfolio_ruin_prob_budget: "0.05"` · `portfolio_det_max_frac: "0.36"`
(governing wall = the readingb 0.70B backstop via the demotion) ·
`structure_loss_frac: "0.01"` + `structure_bound_armed: true` ·
`game_direction_net_frac: "0.40"` + `enabled: true` (SHADOW — arm after the
distribution read).

## First-hour state — the armed constitution is visibly working

The book BOOTED AT THE CEILING: 59 positions, mutex-aware det-max **$3,145**
vs the 0.70B backstop **$3,136** (bankroll $4,479), p_kill_night 0.388,
p_ruin 0.101. With the gates armed this is the exact "fully maxed at the
70%" posture the operator ratified on 8/1 — the bot refuses to ADD tail
(quote_sent 0 in the first window; decline mix: entity 20.2k, utilization
6.4k, directional 5.3k, structure 3.5k, mass-acceptance 0.9k) and resumes on
the MARGIN as today's settlements free room (first UECL settles this
afternoon; MLB tonight). The structure cap then prevents the freed room from
re-concentrating into whales. Overnight realized already +$235 at boot.

NOTE (watch item): weekend club-soccer fills (La Liga/MLS settle Sat/Sun)
PARK det budget for 24–48h vs MLB's same-night cycle — a new structural
input to the ceiling; quantification in flight (rolling-quote analysis).

## Monitoring

Persistent watch armed on the live log: first quote_sent (un-brick
confirmation as headroom frees), any halt/kill/renege, relights, and
quote-rate milestones with p_kill/p_book/det. Research fleets in flight:
(a) the $2.8–2.9k deployment-ceiling attribution (hypothesis: the 65% slate
cap ≈ $2,925 binds below the 70% det backstop the operator remembers),
(b) the rolling-quote/late-slate headroom analysis, (c) sportsbook parlay
markup research (web, verified) vs our ladder.

## P&L truth vs "another very negative day" (exchange settlements, ET days)

| day | realized | % of equity@open |
|---|---|---|
| 8/11 | −$917.00 | −17.1% (the crater) |
| 8/12 | +$320.42 | +7.2% |
| 8/13 | **−$183.52** | −3.9% (midday −$473 → evening recovered +$290) |
| 8/14 so far | **+$269.15** | +5.9% |

Cash-identity equity **$4,843** — ~$510 off the 8/11 peak, not $1k. The
"very negative week" is the single 8/11 one-cluster crater plus chop; entry
EV stayed positive every day (8/13: +EV again by the standing joins).

## NEXT STEPS

- **Me (standing):** monitor first hours; confirm first quote_sent as
  settlements land; read the `game_direction_net_shadow` distribution →
  propose the ARM value; deliver the three research readouts; small
  mechanism fix owed: busy_timeout before the WAL pragma in `Store.open`
  (the boot-race class), and the `--seed-from-store` counterfactual mode for
  the post-arm re-grade.
- **Operator:** none owed now. Slate 65%→80% (the 7/25 open decision)
  becomes decidable with the ceiling readout.
