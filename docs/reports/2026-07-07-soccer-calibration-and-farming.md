# Soccer calibration (1H cluster + corners) & impossible-combo farming audit

**Date:** 2026-07-07 → 2026-07-08 · **Status:** DONE (shipped to config/engine) ·
**Trigger:** "lets fix last night's flagged combos" (from the blind RFQ test) +
"audit the entire soccer correlation model" + "look at the 1H×1H impossibles."

## 1. Pairs calibrated & wired (soccer `pair_rho_by_sport` + oriented resolvers)

All values are drop-in copula `pair_rho` (or oriented `:same/:opp/:tie/:team`
curves), calibrated against history/Kalshi, NOT hand priors. `pair_key` sorts
leg-type names **alphabetically** — config keys must match that order (this was
the `spread|btts`→`btts|spread` ordering bug the blind test surfaced).

**Corners cluster** (`sgp.py`: `_corners_winner_prior`, `_corners_spread_prior`,
`_oriented_team_prior`):

| pair | rho | note |
|------|-----|------|
| `corners\|corners_team` | **0.62** | flagged by RFQ test; team-corners ⊂ total-corners |
| `corners_team\|moneyline` :same/:opp/:tie | −0.15 / +0.15 / 0.00 | oriented |
| `corners_team\|spread` :same/:opp | −0.11 / +0.11 | oriented |
| `corners\|spread` | 0.00 | ~independent |
| `btts\|first_half_total` | **0.55** | flagged by RFQ test |

**1H cross-type cluster** (36 entries; `_period_winner_player_prior` + 12 dispatch
branches). Key values:

| pair | rho |
|------|-----|
| `advance\|first_half_moneyline` :same/:opp/:tie | +0.64 / −0.64 / 0.00 |
| `first_half_moneyline\|total` :team/:tie | +0.24 / −0.42 |
| `btts\|first_half_moneyline` :team/:tie | +0.10 / −0.17 |
| `advance\|first_half_total` | +0.09 |
| `first_half_total\|moneyline` | +0.14 |
| `first_half_btts\|total` | +0.65 |
| `first_half_spread\|first_half_total` | +0.95 |
| `advance\|first_half_spread` :same/:opp | +0.72 / −0.72 |
| `btts\|first_half_spread` | 0.00 |
| `advance\|moneyline` :tie | 0.00 |

Plus `first_half_moneyline|player_goal`, `first_half_moneyline|spread`. Each has a
matching `pair_rho_uncertainty` band and a test (`test_sgp_1h_cluster.py` — 37
tests, `test_sgp_corners_*`, `test_sgp_btts_1h_total.py`, `test_sgp_spread_pairs.py`).

**Audit result:** an independent pass found **27 pairs sitting on the +0.6
fallback** (untyped default) and fixed them; the corrected 1H×1H comment in
`legtypes.py` (Kalshi does NOT blanket-block 1H×1H — see §3); `classify_sport`
maps UCL→SOCCER.

## 2. UCL/UEL/UECL gated OFF

`filters.py`: `_is_two_legged_tie_leg` + `_TWO_LEGGED_TIE_PREFIXES =
("KXUCL","KXUEL","KXUECL")` → `FiltersConfig.decline_two_legged_tie=True`. Two-
legged-tie competitions have advance/aggregate semantics we don't model; the
2026-07-08 backtest confirmed the residual real mispricings were almost all
KXUCL. **WC (`KXWC*`) is the only live soccer family.**

## 3. Impossible-combo farming audit — NO reachable farm beyond the 5 tautologies

We probed **live Kalshi** (`POST /multivariate_event_collections/{collection}`,
no orders, harmless) whether the "airtight" impossible families are even
constructible as combos:

| candidate farm | Kalshi verdict |
|----------------|----------------|
| `advance NO` + `moneyline YES` same team | **BLOCKED** `conflicting_leg_outcomes` |
| `moneyline` + `spread` same team (containment) | **BLOCKED** |
| 1H×1H impossibles (e.g. 1H-over-2.5 + 1H-under-1.5) | **BLOCKED** |

So the 3 "families" we discovered are **not quotable** — Kalshi rejects the leg
combination at RFQ construction. Families 4 & 5 (`advance|moneyline`,
`moneyline|spread`) were added to `relationships.py` then **cleanly reverted**
(dead code — the inputs can never reach us).

**The only farms that exist** remain the **5 pre-existing cross-window
tautologies** (`relationships.py` `Relationship.farmable`): same-market-both-
sides, same-team-corners higher-yes×lower-no, 1H-BTTS⟹FT-BTTS, ml-win⟹over-0.5,
same-line 1H-over-N⟹FT-over-N. `farmable=True` stays gated to **airtight logical
tautologies only** (`CLAUDE.md` defense #2) — never metadata-dependent.

## NEXT STEPS

- **Runs next:** the pairs feed the 2026-07-08 backtests; watch the dense-SGP
  families there.
- **Owner (operator):** confirm the 1H-cluster values on a blind re-test once a
  live 1H book has volume (several are history-calibrated, not yet market-tested).
- **Decision owed:** none open — farming audit is closed (no reachable farm);
  UCL stays gated until advance/aggregate semantics are modeled.
