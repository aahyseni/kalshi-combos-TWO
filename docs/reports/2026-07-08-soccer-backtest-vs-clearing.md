# Soccer backtest — our fair vs maker clearing (1,480 combos)

**Date:** 2026-07-08 · **Scope:** pure-soccer combos (no MLB/non-soccer leg
inside any combo) · **Status:** DONE · supersedes the ad-hoc 1,293 count (final
gather held **1,480** pure-soccer combos).

## What this measures — and its hard limit

For every distinct pure-soccer combo we recorded, price it through the **current
shipped soccer engine** at its marginal snapshot, compare to the **median maker
clearing** across its trades. Error = `our_fair − clearing`.

**Critical caveat (operator, 2026-07-08):** clearing is the winning maker's
**QUOTE**, i.e. their fair **+ their markup**. We **cannot observe the maker's
fair.** So a negative error (our fair below clearing) is *markup + an unknown
residual we cannot decompose*. The only assumption-free readings are:
- **our_fair > clearing ⇒ we are above the winning maker's ASK** (ask ≥ their
  fair), so that is genuine **over-pricing** — the real risk signal.
- **cross-game combos:** our_fair = product of marginals = the *true* fair (no
  correlation model choice), so there the gap **is** pure markup, measured clean.

Everything else (same-game gaps) mixes markup and mispricing inseparably. This is
why the **settlement** backtest (2026-07-08 settlement report) exists — real
outcomes are the only ruler that separates the two.

## Data

- `gather.pkl`: 1,480 pure-soccer combos, 41,127 combo trades, 374,445
  would-quote marginal snapshots, from the prod recorder DB.
- Fréchet screen: drop combos where clearing > min marginal + 2¢ (impossible for
  an AND ⇒ stale marginals, not mispricing). 20 excluded.

## Headline (per distinct combo)

| Cut | n | median \|err\| | bias | within 2¢ | over-priced |
|-----|---|---------------|------|-----------|-------------|
| **All soccer** | 1,026 | **1.88¢** | −1.44¢ | 53% | 9% |
| Trade-weighted | — | 1.86¢ | −1.74¢ | 55% | — |
| **WC-only (UCL/UEL/UECL excluded)** | 998 | **1.60¢** | −1.82¢ | 60% | 8% |

RMSE 5.41¢, p90 5.23¢ (all-soccer). Negative bias = we sit below clearing =
consistent with maker markup (see caveat — *not* proof we're right).

## By bucket / leg count / price

```
BUCKET                              n    med|err|  bias    within2
independence (cross-game)          253    1.11c   +0.08c    76%   <- true fair; gap = pure markup
structural (Dixon-Coles)           105    1.96c   -0.26c    52%
copula: calibrated                 663    2.22c   -2.25c    45%

LEG COUNT      med|err|  bias           PRICE      med|err|  bias
2L  (n=229)     1.57c   +0.49c          <5c         0.57c   -0.70c
3L  (n=262)     2.18c   -1.89c          5-15c       1.75c   -1.46c
4L  (n=251)     1.78c   -1.88c          15-35c      2.81c   -1.68c
5L  (n=148)     1.97c   -2.30c          35-65c      2.52c   -1.82c
6L  (n=136)     1.97c   -2.08c          >65c        2.11c   -2.21c
```

Bias grows (more negative) with leg count — exactly the shape of markup
compounding per leg (cross-game markup baseline ~6–14%, growing with legs).

## Family shapes — best → worst (median |err|)

```
0.76c  PLAYER_GOAL (n=80)            2.25c  ADVANCE+PLAYER_GOAL (n=192)
0.78c  MONEYLINE (n=32)             2.48c  ADVANCE+CORNERS (n=28)
0.79c  ADVANCE (n=101)              2.58c  ADVANCE+BTTS+TOTAL (n=16)
0.85c  MONEYLINE+TOTAL (n=22)       3.02c  ADVANCE+BTTS+PLAYER_GOAL (n=17)
1.15c  TOTAL (n=17, bias +7.96c!)   3.21c  ADVANCE+PLAYER_GOAL+TOTAL (n=20)
1.68c  ADVANCE+BTTS (n=26)          3.63c  ADVANCE+CORNERS+CORNERS_TEAM+PLAYER_GOAL (n=22)
1.96c  ADVANCE+TOTAL (n=42)         3.92c  ADVANCE+CORNERS+PLAYER_GOAL (n=45, bias -4.01c)
```

- **Best:** singles and cross-game stacks (independence bucket) — near-perfect.
- **Worst:** **dense same-game SGPs** — `advance + corners + player_goal` stacks,
  3–4¢ error, bias −3 to −4¢. These are the combos where markup-vs-mispricing is
  unresolved by this test → the settlement test targets exactly them.
- **`TOTAL` bias +7.96¢** = a small-n family we *over*-price (over-priced tail).

## Screen verification (are big errors time-mismatch or real?)

A blunt `|err|>12¢` screen (25 combos) verified per-combo with a time-consistent
trade-time snapshot: **13 time-mismatch, 2 bad-leg-data, 10 real mispricing.**
The 10 real ones are **almost all KXUCL** (Champions League) combos — which we
**gate off**. So on live WC soccer the residual real mispricing is small; the
worst raw errors were stale-marginal artifacts, not engine error. (verify.log)

## NEXT STEPS

- **Runs next:** the **settlement P&L** backtest (2026-07-08 settlement report) —
  resolves the markup-vs-mispricing ambiguity this test cannot, using real
  outcomes.
- **Owner (operator):** review the dense-SGP families (`advance+corners+
  player_goal`) once settlement P&L is in — decide if the −4¢ bias is markup we
  can safely undercut or under-pricing we must correct.
- **Decision owed:** whether to persist the backtest scripts into `tools/backtests/`.
