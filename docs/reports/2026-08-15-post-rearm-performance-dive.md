# 2026-08-15 — Post-rearm performance dive (operator: "quoting less? crazy longshots? Pbook? I want to make more $")

Five-agent read-only dive over the three overnight logs + store (mode=ro) +
code, synthesized. Every operator feel checked against tape; one live defect
found that already cost money twice (bankroll anchor).

## Scorecard (19:31 ET 8/14 rearm → 09:31 ET 8/15)

| dimension | verdict | numbers |
|---|---|---|
| Quote throughput | **NOT less — 2× every comparable hour** | 187–243/min overnight (peak 01h), 140–155/min pre-slate morning vs 88–105/min 8/14-overnight baseline. MLB opens 13:10 ET. |
| Why it FEELS less | **the store tape drops 75–80% of quote rows under load** | persistence.py:302/322-334 queue overflow (maxsize 200k) + 2,398 checkpoint failures, 3.16GB WAL on the 175.6GB store; hour-04Z: log 11,189 quoted rfqs vs 2,267 store rows; 55/94 fills unjoinable. Money paths are synchronous+durable — dashboards lie, the ledger doesn't. |
| Fills | rate held, SIZE compressed by design | 94 fills, 6.8/h vs 8.0/h on 8/13; risk/fill $13.99 vs $22.75; max ticket $39.35 vs 8/13's $139–187 — the 1% cap amputated the whale tail. Premium collected $1,571.48. |
| Fill quality | clean | capture +0.25c/ct ABOVE tier mean (median +0.13c), 0 negative-edge, +$44.50 edge cc-exact; markouts favorable (−$0.50@60s, −$8@300s in our favor) = no pickoff tax. |
| Reneges / halts | 0 reneges; 2 explained halts | zero `kill_marginal_raises`/`ruin_marginal_raises` anywhere. Halt#1 21:57 = FALSE TRIP off the lagging anchor (below). Halt#2 23:26 = watchdog stall-kill. Active session 10h+ clean. |
| Money | profitable; ~$5k equity CONFIRMED | 8/14 final −$5.59 (group-corrected); 8/15 +$508.52 by 9:30am (mostly the pre-rearm book settling); equity chain ≈ $4,919–5,025. Post-rearm settles n=12, −$51.57 — no signal, $1,465 premium still open. |

## "Crazy longshots" — NOT a new risk seam; longshot share HALVED

Taker-fair <15¢ share of quotes: **33.9% (8/13 evening) → 14.0% post-rearm.**
What the operator is seeing is COMPOSITION, not price band:

- cheap-NO lottery bids doubled (NO <15¢: 0.94%→1.95%) — we risk 1–2¢ to
  win ~98¢, the INVERSE of longshot book risk;
- player-prop leg share 16.8%→35.5% (KXMLBKS 6.5× to #2 series);
- 87 never-before-seen series combos + 145 true cross-SPORT quotes
  (MLB+MLS 70, LALIGA+MLB 57, CS2+MLB 13) — the directional-scoping fix
  unlocked exactly this.

Guards verified holding: 4¢ longshot tier engaging correctly (fills at fair
3.5–3.9¢ captured 3.5–4.1¢); DNP scalar guard ARMED with the ask-floor
clamp impossible to underprice by construction (quote.py:232-233); structure
cap never breached. **Real seam to ledger (pre-existing, now relatively
bigger): DNP scope covers only all-legs-same-player combos (0.30% of
quotes) while 35.5% carry prop legs; no player-level concentration cap
(4 pitchers each in 3+ live combos)** — the known player-clustering gap,
post-8/31 recipe.

## P(book): the steer IS working; the number is doing what diversification does

2,674,402 skew events since rearm, 99.5% APPLIED (57% rebates to
diversifiers, 42% widens to stackers); 50 of the 94 fills filled through a
diversifier rebate. p_book 0.38–0.41 now (broken day: 0.23–0.37; history
0.40–0.50): a sell-only book's P(everything profits) mathematically FALLS
as positions rise while the tail SHRINKS (18 pos → 0.74; 78 pos → 0.38) —
the decay IS the diversification. "The book isn't trying to raise Pbook" is
the operator's own 7/27 ruling (pbook pricing axis disarmed after it
measurably degraded price discrimination); the governed number is
p_kill_night ≤ 2%, which crossed budget ~07:00 ET — **against a stale KILL
line** (next section). p_night (the P(day ends positive) KPI) sat 0.99+ all
night.

## ⚠ THE FINDING: bankroll anchor ratchets down, already cost money twice

Every %-of-bankroll wall runs off `risk_bankroll_cc = min(start_of_day
equity, cash + portfolio_value)` (balance.py:575-597). Start-of-day
re-anchors only at UTC midnight or process boot (in-memory tracker) — so
the 23:27 ET relight anchored today at the overnight equity TROUGH
**$4,181.81** while true equity is ~$4,919–5,000. `kill_line` byte-pinned
at 0.12×$4,181.81 for 2,250 consecutive snapshots.

- **All walls 10.8–16.4% tight**: structure $41.82 (should be ~$47–50),
  entity $125 (~$141–150), per-combo $209 (~$235–250), slate $2,718
  (~$3,049–3,250), backstop $2,927 (~$3,283–3,500), KILL line $502
  (~$563–600). The three walls doing 75.8% of current refusals are all on
  this list.
- **It fired a FALSE HALT**: 21:57 ET `halt_hard_trip` on give-back
  $530.14 ≥ 3/25 × $4,286 = $514.35. At true equity the threshold is $600
  — no trip. That halt caused the relight that ratcheted the anchor lower.
- **Mechanism defect, not conservatism**: the min() clamp correctly
  excludes mark-to-model gains but ALSO excludes realized settled CASH
  (+$508 today), and relights reset SOD to the relight-instant equity —
  within a UTC day the anchor can only go DOWN.
- **Repair recipe** (2 lines of intent): (1) balance.py:578 anchor =
  min(SOD_equity + realized_settled_cash_today, cash + haircut×PV);
  (2) balance.py:366-367 persist the SOD anchor per UTC date so relights
  restore max(persisted, boot) instead of the trough. Both derived from
  measured exchange state — North-Star clean.
- **Self-heals TONIGHT at 20:00 ET** (UTC midnight re-poll). A forced
  restart would fix it now but spends boot risk for ~4h of headroom on a
  48%-util book — not recommended.

## Make-more-$ levers, ranked

1. **Anchor fix** — +11–16% on EVERY wall + kills the false-halt class +
   un-brakes the morning p_kill regime. Operator call: freeze-exception
   defect fix now, or ride tonight's auto-heal and ledger it for 9/1.
2. **Ticket-size restoration** — fill RATE held; size is the collapsed
   variable. Anchor fix alone = +18%/ticket. Beyond: per_combo 1%→2% belt
   (~$98 ceiling). 8/13's $3.8k premium day needs only ~71 capped fills at
   the true-equity cap.
3. **Mains markup 1¢→2¢** — capture is +0.25c above tier with zero
   adverse-selection tax; the 8/14 research says ≥0.75¢/ct left on wins.
   INTERACTION: the skew rebate already gives back up to 0.9¢ on 16/94
   fills — a raise partly re-absorbed unless the rebate clamp scales.
   Needs the gap-to-best-rival extraction (measurement, freeze-compatible).
4. **Dead-time elimination** — 8/14 lost ~13 quoting hours to cash
   starvation + the brick; overnight run-rate $139/h premium. The
   pause-new-creates mechanism (owed since 8/12) is the fix.
5. **Slate 65→80** — DEFER: slate isn't in today's binding mix at all
   (util 48%, utilization skips 3%).
6. **Store writer repair** — observability: the tape drops 75–80% of quote
   rows under load; it's why the operator's feel said "quoting less" while
   sends were 2×. Checkpoint/rotation fix class, isolated from pricing.
7. **Do-NOT-arm**: pbook pricing axis (contradicts 7/27 ruling), conc
   steer (74% discrimination collapse), any refit off the n=12 post-rearm
   settle sample.

## NEXT STEPS

- **Operator decision owed:** anchor fix timing — (a) now as a
  freeze-exception defect repair (it caused a false KILL halt), or
  (b) tonight's 20:00 ET auto-heal + 9/1 permanent fix. And whether to
  open per_combo 1%→2% (lever 2) with tonight's 94-fill/0-renege
  validation set.
- **Me (freeze-compatible measurements):** gap-to-best-rival extraction
  (unblocks lever 3); direction-net shadow read → ARM value; store-writer
  defect note to the deferred ledger; player-clustering seam recipe.
- **Me (standing):** monitor through today's slate; evening readout with
  the post-rearm book's first real settle wave.
