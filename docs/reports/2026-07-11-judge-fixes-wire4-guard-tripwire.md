# 2026-07-11 — Judge-mandated fixes: WIRE-4 refutation guard, poisoned fixture, S8 farm narrowing, taxonomy tripwire

**Branch:** `containment-collapse` (worktree), on top of 62e4e30 (the five wires).
**Mandate:** the V2 adversarial review REFUTED WIRE-4's neighbour-correlation claim
with a live counterexample, flagged a poisoned test fixture that hid it, narrowed
the S8 farm to an unverified lemma, and V3 showed the operator's "when/if it does
we'll know" is not true today. Four fixes, applied together.

---

## FIX-1 — WIRE-4 conditional super-leg: same-game-companion isolation guard

**Refuted claim** (was in `_collapse_containments`'s docstring): "conditional
super-legs carry NO isolation requirement … represented by its kept leg for
neighbour correlation." The super-leg carries the SELECTED-side pair joint but is
priced at side `"yes"` under the kept leg's ticker, so a same-game companion sees
it through the kept leg's YES-side rho — whose **sign inverts for NO-side mixes**.

**V2 live counterexample** (prod-convention tickers, HIT3-no × HR1-no ×
own-ML-yes at p = 0.21/0.15/0.58, engine's own rhos pair −0.124, ml|prop +0.23):

| quantity | value |
|---|---|
| trivariate truth P(no,no,ml-yes) | 0.3451 |
| independence product P(no,no)×P(ml) | 0.3849 |
| **engine BEFORE (prod-convention events)** | **0.4183 = +7.32c above truth** (sign inverted: truth is BELOW independence, engine went ABOVE) |
| engine BEFORE (the probe's poisoned 3-segment events) | 0.3849 (+3.98c — silent independence; the poison hid the same-game case) |
| **engine AFTER** | **NoQuote `skip_classifier_unknown` — "conditional super-leg game 26JUL092145COLSF carries other kept legs: conditional-vs-neighbour correlation sign unmodeled"** |

**Remedy shipped** (fail-closed doctrine): conditional pairs now carry the SAME
same-game-companion isolation guard as window bands — same-game KEPT companion ⇒
UNKNOWN decline for **every** side mix; cross-game companions (ρ=0, the bulk of
the observed decliner population) stay priceable (representing the pair by its
kept leg is exact at ρ=0).

- Authoritative guard: `relationships._collapse_containments` (game-key check,
  mirrors the band guard).
- Defensive mirror: `engine._price_nested_bands` (group-based re-check ⇒
  `SKIP_PRICING_FAILED`, unreachable while the classifier guard holds).
- Identical ports: `tools/backtests/wc_backtest.py` + `mlb_backtest.py` collapse
  branches (mlb labels the path `…-conditional-companion`). Both mirrors also
  inherit the classifier guard automatically (they call live `classify_legs`).

## FIX-2 — poisoned fixture: prop event tickers

`tests/test_containment_collapse.ev()` built events via `rsplit("-", 1)[0]`,
minting per-player 3-segment events for 4-segment MLB prop tickers — so a prop's
own-game companions landed in a DIFFERENT game group in every e2e test built on
the helper, hiding the FIX-1 shape. **Tape-verified read-only 2026-07-11**
(`data/combomaker-prod.sqlite3` rfqs): real prop event =
`KXMLBHIT-26JUL111605COLSF` for market `KXMLBHIT-26JUL111605COLSF-SFRDEVERS16-1`
— 2-segment PER-GAME. Helper now returns `"-".join(ticker.split("-")[:2])`.

Re-expressed honestly:

- `test_embedded_conditional_with_same_game_companion_prices` →
  `…_declines` (asserts the FIX-1 UNKNOWN decline + guard note).
- `test_same_player_conditional_pair_buried_collapses` retargeted to a
  CROSS-game companion; new `…_with_same_game_companion_is_unknown`.
- New regression: `test_v2_counterexample_no_no_own_ml_declines_unknown` — the
  exact V2 counterexample with prod-convention tickers, classifier UNKNOWN +
  engine NoQuote for all four side mixes.

## FIX-3 — S8 farm narrowing (V2 ruling)

Cross-scope 1H-spread × FT-total impossibility (S8-yn) stays IMPOSSIBLE no-quote
but **farmable=False**: the implication spans TWO official records (half-time +
full-time), and Kalshi's abandonment/award rules text for KXWC totals is not yet
captured as evidence both records stay consistent — an unverified lemma fails the
airtight one-record farm bar. S7 (1H×1H) and S13 (FT×FT) one-scoreline cells stay
farmable=True. MLB (S34) stays farmable=False. Test
`test_soccer_1h_spread_scope_nesting` updated with the ruling cited.

## FIX-4 — taxonomy-impossible constructibility tripwire (V3 §2.4-1)

Makes "when/if it does we'll know" TRUE. New fixture
`tests/fixtures/ground_truth/taxonomy_impossible.json` pins the
semantically-IMPOSSIBLE shape × side-mix cells from
`docs/calibration/containment_probe/taxonomy.json` + the exchange BLOCKED
verdicts from `exchange_matrix.json` (per-cell probe evidence cited). New module
`pricing/tripwire.py` + hook in `classify_legs` (after every shipped family): any
same-game pair matching a pinned cell ⇒ **IMPOSSIBLE farmable=False** with the
dedicated countable note `taxonomy-impossible tripwire: <shape>` — never a copula
price, never a farm (fixture-driven certainty is not an airtight in-code proof).
A live match is proof the Kalshi validator loosened.

- Coverage: V3 tier-1 (S19-nn, S20-nn, S24-yn, S27-yn) + the rest of the 30-cell
  dangerous class — S3L/S4/S5/S9/S10/S11/S14/S15/S16/S17/S18/S21/S22/S26/S28/
  S29-bundle/S32 (soccer), S44/S45 (wnba incl. the PTS ladder by SERIES match),
  S46/S47/S48 (ufc by series), S50 (golf 10-pair finish chain by series +
  same-player), + S42 MLB same-player same-stat ladders pinned defensively.
- UNKNOWN-typed legs verified on the LIVE path: they do **not** decline at
  intake/filters — `rfq/filters.py` gates only the collection ticker
  (`RfqFilter.evaluate` collection_whitelist branch) and
  `sgp.build_sgp_correlation` prices UNKNOWN-typed same-game pairs at the flat
  prior (`types[i] is LegType.UNKNOWN` branch) — hence the series-matched cells
  instead of documenting a decline that doesn't exist.
- Documented residual: S49 (tennis tournament⇒match) is cross-scope (different
  game codes) — outside the same-game tripwire, no verified same-scope ticker
  key to pin. S23/S25 excluded (stage-conditional, not unconditionally
  impossible).
- Fail-closed: missing/corrupt fixture ⇒ tripwire inert + one warning
  (`taxonomy_tripwire_inert`), existing behavior unchanged (tested).
- Interception note: the pinned S17 exclusion now declines the
  {cover-yes × other-suffix-win-yes} pair that WIRE-1 deliberately left to the
  copula (suffix inequality is not proof of opposite teams — but a decline-only
  farmable=False verdict costs coverage on an exchange-blocked shape, never
  money); `test_soccer_spread_win_refusals_fail_closed` re-expressed.

## Verification

| check | result |
|---|---|
| V2 probe BEFORE (stock, poisoned events) | fair 0.3849 (+3.98c vs truth 0.3451) |
| prod-convention probe BEFORE | fair 0.4183 (**+7.32c**, sign inverted) |
| V2 probe AFTER (both event conventions) | **NoQuote `skip_classifier_unknown` + guard note** |
| full suite | **1239 passed, 0 failed** (baseline 1214/0 + 25 new tests) |
| mypy strict (src) | touched files clean (2 pre-existing `ising_amm.py` numpy-stub errors exist at 62e4e30, untouched) |
| ruff | all 9 touched files clean |

Files: `src/combomaker/pricing/{relationships,engine,tripwire}.py`,
`tests/{test_containment_collapse,test_containment_windows,test_mlb_containments,test_tripwire}.py`,
`tests/fixtures/ground_truth/taxonomy_impossible.json`,
`tools/backtests/{wc,mlb}_backtest.py`.

## NEXT STEPS

- **Owner: next probe session** — capture Kalshi abandonment/award rules text for
  KXWC totals (the S8 farm re-opens on that evidence alone; also V3 §4-5 soccer
  abandonment pin for the existing farms) and promote `market_rules.json` +
  `collections.json` baselines into `docs/calibration/`.
- **Owner: next engine session** — V3 §4-1 rules-pin sweep (weekly hash of
  rules_primary/strike_type per wired family) + floor_strike runtime assertion;
  both pre-fill alarms behind operator approval.
- **Owner: backtests** — next full re-run will show the FIX-1 delta: conditional
  plans whose companion shares the pair's game move from priced → UNKNOWN
  (`containment-collapse-conditional-companion` label in mlb_backtest paths);
  cross-game conditional plans (the bulk) unchanged.
- **Decision owed by user:** none blocking — all four fixes were judge-mandated
  and are shipped with tests.
