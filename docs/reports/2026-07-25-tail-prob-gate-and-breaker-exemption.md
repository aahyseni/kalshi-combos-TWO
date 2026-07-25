# 2026-07-25 (evening) — P(KILL-night) book gate armed + metadata-breaker end-of-life exemption; warm boot validated

## Operator anchor ratified (new layer-2 policy anchor)

> "The committed tail is only for a few games… that shouldn't block us from
> taking fills for games we don't even have on our books yet… risk on for
> one-sided bets, but not for risking more on our total book. more bets =
> more variance = more money."

Decision prompt answered: **the TOTAL-book gate binds P(KILL-distance
night) ≤ ~2%** (`risk.portfolio_kill_tail_prob: "0.02"`, an operator policy
anchor stated once). The one-sided concentration walls (per-game /
per-direction / accumulated-structure / soon per-entity) are unchanged.

## Why the old bound bricked a diversified book

The ES99-average form ("mean of the worst 1% ≤ KILL distance") barely
credits diversification at small N with ~50%-loss positions — the worst 1%
is "most lose at once" *even when games are independent* — so ~$245 of
diversified premium pinned the whole book at the boundary while 10 games sat
unquoted (the day's ~$350-fill complaint). The probability form
distinguishes exactly what the operator wants: a one-way book has
P(KILL-night) ≈ 40–50% → hard-blocked; ~40 independent bets can hold
3–8× the premium at P ≤ 2%.

## Implementation (`6e054b0`, suite green, mypy strict)

- **Candidate gate** (`sim/book_risk._candidate_gate`): when
  `risk.portfolio_tail_prob_gate` is armed, budget (2) binds the worst-model
  Wilson-upper `P(post-book loss ≥ cvar_frac × bankroll)` over the SAME CRN
  post vectors (production/challenger/bridge/split). Empty vectors → ES
  fallback (never a free pass). New reason
  `post_kill_tail_prob_over_budget`.
- **Quote-time portfolio-CVaR cap** (`risk/limits.py` 8a): reads a new
  1001-point **loss-quantile envelope** on `BookRiskSnapshot`
  (elementwise worst model per quantile; point-count rounding UP;
  `_wilson_upper` keep-in-sync copy — limits cannot import sim). Legacy
  snapshots → ES fallback. Both sites share the ONE YAML flag.
- **Breaker exemption** (`ops/quote_app._metadata_changes`): the 3:40p live
  halt — the revalidation fix un-blinded the metadata-change breaker and its
  first-ever observation (the finished 1:10p KC@DET markets settling)
  hard-halted the bot. A change on a market whose PRIOR close horizon
  already passed is now benign (logged + reseeded); a change while the
  horizon is in the future (reschedule) trips as before; naive horizons
  trip (never crash).

## Live validation (run 1559, ~4:00p ET)

- **First WARM BOOT ever**: `metadata_cache_loaded` 1,351 markets + 179
  events; boot fetch failures **1,901 → 4** (the 429 storm is dead; every
  family priceable from minute one).
- Preflight green; boot reconcile honored the halt marker; tail-prob gate +
  P(book) steer (289/300 quotes carrying it in applied price) + B2 hedge
  budget all armed together; ~400+/min quoting; zero tail-prob declines on
  the committed book (its P(KILL) is tiny — the bound only bites as real
  concentration builds).

## Armed vs shadow (end of day)

| mechanism | state |
|---|---|
| P(book) steer (`pbook_armed`) | ARMED in price |
| B2 derived hedge budget | ARMED |
| Tail-probability book gate (≤2%) | ARMED (both sites) |
| Slate-axis waiver | ARMED (morning) |
| Leg-direction axis (family/entity) | SHADOW — arm after its slate read-out |
| Adaptive caps | shadow (static WC caps enforce) |

## NEXT STEPS

- **Watch tonight** (digest monitor every 2.5 min): fills as the 6:40p+
  wave arrives; `post_kill_tail_prob_over_budget` counts (should be rare and
  concentrated on one-way adds); halts 0.
- **Operator decisions open**: slate 65% → 80%; `leg_axis_armed` after
  shadow read-out; post-slate P&L + settlement reconciliation (Claude).
- **Queued**: entity-axis BOUNDS (walls, not just pricing); boot-fetch
  pacing (residual 4 failures); adaptive-caps pnl_history feed.
