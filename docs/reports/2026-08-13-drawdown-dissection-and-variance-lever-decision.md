# 2026-08-13 — Peak-drawdown dissection (−$1,070) + operator decision: BUILD THE VARIANCE LEVERS

**Operator question (verbatim):** "we're down almost 1k since peak … dissect our
losses and see if its pure unluck or if its something we have to manage /
change, we could bleed slowly to 0 … i'd rather make the +100 EV we expect
rather than these violent positive and negative swings."

**Method:** exchange settlements (paced GETs) + the verified
`fills.expected_edge_cc` entry-EV identity (store `mode=ro`) + 50k-trial MC on
our own entry fairs. Same drill as the 8/6/8/12 forensics. Read-only; live bot
untouched.

## Verdict: NO BLEED — a concentration problem, now proven six times

Peak → now: equity@open $5,354.07 (8/11) → $4,284.21 cash-identity = **−$1,070**.

| ET day | realized | entry EV (coverage) | luck | note |
|---|---|---|---|---|
| 8/10 | +$6.29 | +$27 | −$21 | quiet |
| 8/11 | −$917.00 | **+$146.70** (180/184, 3.73¢/$1 — the week's best) | −$1,110 | ONE game-cluster (all 180 tickets), z −4.26 wins-vs-expected: correlated variance, no cell failed (8/12 audit) |
| 8/12 | +$320.42 | +$135.87 (206/213, 2.44¢/$1) | +$185 | normal |
| 8/13 (to ~11:00) | −$473.29 | **+$28.18** (41/41, 2.35¢/$1) | −$501 | **top-3 tickets = −$388 = 82% of the loss** |

- **Entry EV has been positive EVERY measured day** (2.2–3.7¢/$1); today's 41
  settles were 41/41 +EV at entry. Lifetime realized +$2,284 on the $2,000
  deposit. There is no negative-EV flow to "bleed to 0" on — the entry gate
  has never leaked (3 −EV tickets era-wide, −$1.80, all recovery-path).
- **The swings are structural concentration**, not model failure:
  1. **Near-zero-EV whale tickets** — today: −$176.69 (271ct, p=0.66, $1.54
     EV), −$137.16 (282ct, p=0.49 near-coin, $0.25 EV), −$73.94 (92ct,
     p=0.83, $2.39 EV). Single tickets at 2.9–3.7% of bankroll for pennies of
     edge. **Sixth sighting** of the P1 per-STRUCTURE seam (7/31 loss, 8/1
     win, 8/5 era top-10 = 121% of net, 8/6 top-5 = 96%, 8/9 two 15–19¢ KS
     longshots +$391 on $0.55 EV, today).
  2. **One-cluster slates** — the book is ~all one sport one night; 8/11
     chained into a single component. (Club soccer went live TODAY —
     1 UECL fill/$23.50 so far; diversification needs days and only bites if
     the book stops re-concentrating.)
  3. **The night-tail budget is unarmed** (8/12 audit: `p_kill_night` peaked
     0.25–0.50 nightly, kill/ruin gates inert, capacity walls + cash the only
     brakes).
- MC on today's own entry fairs: P(day ≤ −$473) = 1.8% independence-floor
  (commoner with clustering); ~25 draws from this book make hitting such days
  unremarkable. Daily EV ~+$30–150 vs ±$500–1,100 realized swings —
  signal-to-noise per day ≈ 0.1. **The swings are the book's design, and the
  design is changeable.**
- Club wiring is NOT involved: zero club legs among any settles (first club
  settlements land tonight).

## Operator decision (2026-08-13, supersedes the 8/31 freeze for these two items)

**"Build both now"** — with a fallback snapshot and full documentation:

1. **P1 Stage-1 per-STRUCTURE + per-game-DIRECTION accumulated net bounds** at
   the reservation path (7/25 dossier) — caps any one structure at the ~1%
   anchor (~$47 today); would have turned today's −$473 into ≈−$180 and
   equally trims +$1,092 days.
2. **Marginal KILL/ruin gate arm** — acceptance-seed fix (seed from the
   store's measured history at boot), then the four staged lines + ruin
   budget 0.05 + det_max_frac 0.70→0.36 at ONE restart. Makes
   P(≥12%-loss night) ≤ 2% enforced-on-the-margin instead of telemetry.

The rest of the 8/31 freeze stands (backstop repair, $500-stop deletion, etc.
remain parked in the deferred ledger).

## Fallback snapshot (operator-required)

- **Git tag `fallback-pre-variance-levers-20260813`** = `b02c451` (club
  soccer live build + docs), pushed to origin.
- **Armed config backup:** `config/prod-live-wc.local.yaml.fallback-20260813`
  (local file, gitignored dir — the yaml is never committed).
- **Restore procedure:** STOP_BOT → `git checkout
  fallback-pre-variance-levers-20260813` → copy the yaml backup over
  `config/prod-live-wc.local.yaml` → START_BOT.

## Changes shipped earlier today (same-day context)

Club soccer wired + live (`67579a5`, report
`2026-08-13-club-soccer-wired-live.md`); the red-error storm the operator saw
= `insufficient_balance` collateral refusals (cash fully deployed), zero
halts, 14k quotes/session — known class, back-off mechanism stays on the
deferred ledger.

## NEXT STEPS

- **Me (build, starting now):** P1 Stage-1 build per the dossier (worktree,
  flag-gated dark, full gates, validate-can-quote) + acceptance-seed fix →
  one arming restart for both levers; first-night watch plan per the 8/1
  arming checklists.
- **Watch:** first club-soccer settlements reconcile tonight; the 8/13 slate
  completes the day's realized number.
- **Operator:** no further decision owed until the arming restart readout.
