"""Configuration: pydantic-validated YAML per environment; secrets via env only.

The production guard is hardcoded here, not in YAML: quoting on prod requires
BOTH the explicit ``--confirm-live`` CLI flag AND ``prod_limits_configured:
true`` in the prod config file. Demo is the default environment everywhere.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Env(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class Mode(StrEnum):
    OBSERVE = "observe"  # log RFQ flow + would-quotes; sends nothing
    PAPER = "paper"      # full pipeline incl. risk, records hypothetical quotes; sends nothing
    QUOTE = "quote"      # sends real quotes


# Doc-verified base URLs (docs/api-notes/auth-env.md). Demo is .co, prod .com.
_ENDPOINTS: dict[Env, tuple[str, str]] = {
    Env.DEMO: (
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    ),
    Env.PROD: (
        "https://external-api.kalshi.com/trade-api/v2",
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    ),
}


class StrictModel(BaseModel):
    """Reject unknown keys: a typo in a limit name must fail loudly, not silently."""

    model_config = ConfigDict(extra="forbid")


class EndpointsConfig(StrictModel):
    rest_base_url: str
    ws_url: str

    @classmethod
    def for_env(cls, env: Env) -> Self:
        rest, ws = _ENDPOINTS[env]
        return cls(rest_base_url=rest, ws_url=ws)


class LoggingConfig(StrictModel):
    json_output: bool = True
    level: str = "INFO"


class SafetyConfig(StrictModel):
    # Set true in prod.yaml only after limits have been reviewed by the human.
    prod_limits_configured: bool = False


class FiltersConfig(StrictModel):
    """Quote/no-quote gates. Every rejection carries a ReasonCode and is logged."""

    # Whitelist of mve_collection_ticker prefixes. Empty = observe everything
    # (observe/paper only — quote mode refuses to run with an empty whitelist).
    collection_whitelist: list[str] = []
    combos_only: bool = True          # skip single-market RFQs
    min_legs: int = 2
    max_legs: int = 6
    min_contracts: float = 1.0        # whole contracts
    max_contracts: float = 10_000.0
    min_target_cost_dollars: float = 1.0
    max_target_cost_dollars: float = 50_000.0
    max_leg_spread_cc: int = 800      # widest acceptable leg spread ($0.08)
    min_leg_depth_contracts: float = 1.0   # min size behind BOTH best bids
    min_time_to_close_s: float = 3600.0    # pregame-only default (1h before close)


class FeeConfig(StrictModel):
    """Coefficients as decimal strings (exact Fractions downstream). VERIFIED
    against the official Kalshi fee-schedule PDF (effective 2026-06-29): taker
    0.07, maker 0.0175, quadratic. Maker fee applies only on markets in Kalshi's
    maker-fee list (quadratic combo series charge $0 maker); monitor changes via
    GET /series/fee_changes, and still reconcile vs real fills (defense #3)."""

    taker_coef: str = "0.07"
    maker_coef: str = "0.0175"
    # fee_type per combo series until fetched from GET /series: conservative
    # default; overrides keyed by series/collection ticker prefix.
    default_fee_type: str = "quadratic"
    default_multiplier: str = "1.0"


class CorrelationConfig(StrictModel):
    """Conservative priors; empirical calibration (with sample-size gates)
    updates these, never code."""

    same_event_rho: float = 0.6
    cross_event_rho: float = 0.0
    rho_uncertainty: float = 0.25
    # SGP structure: signed YES-YES priors per typed pair (legtypes.pair_key).
    # CALIBRATED 2026-07-06 from 8,982 matches (top-5 EU leagues 20/21-24/25,
    # tools/calibrate_pairs_from_history.py — implied rho solved through OUR
    # copula). Notable corrections vs the original hand priors: btts|moneyline
    # flipped SIGN (+0.05 → −0.17: decisive wins are clean-sheet-ish); both
    # corners pairs measured ≈ 0 (folk wisdom busted). player_goal and extras
    # pairs remain hand priors (no match-level data) with the wide band.
    pair_rho: dict[str, float] = {
        "moneyline|moneyline": -0.95,      # measured −0.99: P(both win) = 0 exactly
        "btts|total": 0.75,                # measured +0.746, n=8982
        "player_goal|total": 0.40,         # hand prior (uncalibrated)
        "btts|player_goal": 0.35,          # hand prior (uncalibrated)
        # Structurally implied ~+0.51 in BOTH scoreline-inversion worked
        # examples (fav ENG/Kane and dog POR/Ronaldo, 2026-07-06) — a star
        # scorer's goals ARE his team's goals. Global kept below the soccer
        # value (other sports unmeasured), wide band.
        "moneyline|player_goal": 0.40,
        "btts|moneyline": -0.17,           # measured −0.20/−0.14 (home/away pooled)
        "moneyline|total": 0.23,           # measured +0.28/+0.18 (side asymmetry)
        "total|total": 0.95,               # nested thresholds: measured at the cap
        "corners|total": 0.00,             # measured −0.04 ≈ 0
        "btts|corners": 0.00,              # measured +0.01 ≈ 0
        "extras|total": 0.50,              # hand prior (MLB, uncalibrated)
    }
    typed_rho_uncertainty: float = 0.15
    untyped_rho_uncertainty: float = 0.30
    # Sport-specific tables (calibrated 2026-07-06; the same pair correlates
    # DIFFERENTLY per sport: winner×over is +0.23 in soccer, 0.00 in NFL/NBA).
    # Sports absent here fall back to the global table above with wider bands.
    # WNBA inherits NBA's numbers (transfer assumption, wider band).
    pair_rho_by_sport: dict[str, dict[str, float]] = {
        # Soccer: moneyline|total is CONDITIONAL-MLE on per-game closing-line
        # marginals (directive-compliant; club n=7,228 train, rho +0.30 SE
        # 0.019, BEATS independence out-of-sample on held-out 23/24+24/25) —
        # band widened for home/away asymmetry + internationals. btts pairs
        # remain POOLED-method (no closing BTTS odds in the dataset) with
        # widened bands until a conditional/structural refit; btts|ml at least
        # measured identically in club and international data (−0.197 both).
        # btts|moneyline is ORIENTATION-CONDITIONAL (":fav"/":dog" resolved by
        # the ML leg's YES-side marginal, blended across 45–55c): the −0.19
        # calibration pooled favorites and dogs, but "winners keep clean
        # sheets" is a favorites-only effect — a dog can only win by scoring,
        # so dog-win×btts is ~0 (scoreline-model implied +0.04 on the live
        # SPA/POR example that the market priced at exactly our structural
        # fair). Plain entry retained for marginal-less callers.
        # moneyline|player_goal: structurally implied +0.51/+0.52 on both
        # worked examples (fav and dog) — orientation-insensitive, so a single
        # entry; replaces the 0.25 hand prior that made us auto-lose striker
        # SGP auctions.
        "soccer": {
            "moneyline|total": 0.28,
            "btts|total": 0.70,
            # btts|moneyline is now a WIN-PROB CURVE (oriented_curve below), not
            # two flat fav/dog plateaus: the residual btts<->win rho is a monotone
            # function of the ML leg's win-prob (RE-MEASURED 2026-07-07, 8,982
            # matches — heavy longshot ~0, deepening to ~-0.36 for heavy favorites).
            # The curve WINS whenever leg marginals are available; these plain /
            # :fav / :dog entries are the marginal-less fallback (and the -0.19
            # pooled value the curve reduces to at a mid-strength dog).
            "btts|moneyline": -0.19,
            "btts|moneyline:fav": -0.19,
            "btts|moneyline:dog": 0.00,
            "moneyline|player_goal": 0.50,
            # RE-MEASURED 2026-07-07 (Understat 3,652 matches, orientation-balanced
            # top-xG scorer per team): implied rho +0.549, so 0.45->0.55. Must
            # EXCEED player_goal|total (0.46) and it does. Strongly orientation-
            # dependent (dog-scorer +0.81, fav-scorer +0.31) but there is no ML leg
            # to orient on, so a single 0.55 ships with a wide band (0.30) spanning
            # the fav/dog range.
            "btts|player_goal": 0.55,
            "player_goal|total": 0.46,       # measured +0.46 (Understat 3,652); was global 0.40
            "player_goal|player_goal": 0.03,  # teammates ~0 (Poisson-split, exact) / opp +0.05
            "total|total": 0.95,
            # TOTAL corners (KXWCCORNERS) — measured ⊥ goals AND ⊥ result.
            "corners|total": 0.00,
            "corners|moneyline": 0.00,
            "btts|corners": 0.00,
            # advance|corners and corners|player_goal were UNLISTED, so a same-
            # game combo mixing total corners with an ADVANCE or player-goal leg
            # fell to the flat same_event_rho +0.6 fallback — a fail-safe-inversion
            # that made corners look strongly comonotone with the result/scorer
            # and drove the 3,344-contract WC combo to 8.76c vs the maker's 5.60c
            # (job 24844262). ADVANCE is a diluted moneyline and corners|moneyline
            # is a MEASURED 0.00 (corners ⊥ result), so advance|corners ≈ 0.00.
            # corners × a player's goal mirrors the same-team-attack team-corners
            # prior corners_team|player_goal +0.05. Both are LABELED PRIORS (wide,
            # zero-spanning band): total corners is measured ⊥ goals/result, these
            # just replace the WRONG +0.6 default with the grounded near-zero value.
            "advance|corners": 0.00,
            "corners|player_goal": 0.05,
            # TEAM corners (KXWCTCORNERS), MEASURED conditional on 8,981 matches
            # (team-strength + venue controlled, so the residual the copula needs
            # after live marginals). Unlike TOTAL corners, a TEAM's corners are
            # NEGATIVELY tied to that team winning / covering — the chasing team
            # piles on corners (pooled sign is a Simpson's-paradox artifact).
            # ⊥ goals (total/btts ≈ 0). player_goal / advance are LABELED PRIORS
            # (no football-data coverage), wide-banded, not measured.
            "corners_team|moneyline": -0.15,
            "corners_team|spread": -0.13,
            "corners_team|total": 0.00,
            "btts|corners_team": 0.00,
            # Team corners nest ONLY for the SAME team (POR4 & POR8); game totals
            # do NOT nest (0 in tape). Resolved in sgp.py to ":same"/":opp" by
            # stripping the trailing line digits off the team suffix. OPPOSITE
            # teams: territory is roughly zero-sum, RE-MEASURED 2026-07-07 on
            # 8,981 matches (HC x AC over lines 4-7): implied rho -0.287 (grid) /
            # -0.283 (matched lines), so -0.21 -> -0.28 (the shipped -0.21 was the
            # opposite-team value, ~0.07 too shallow). SAME team, nested lines are
            # EXACT CONTAINMENT (over-M subset of over-N, 0 violations) — when the
            # pair is buried in a larger combo (not the bare-pair IMPOSSIBLE/OK case
            # relationships.py resolves) the copula approximates it with a strong
            # comonotone positive. Plain entry is the opposite-team value, used when
            # the team suffix cannot be parsed.
            "corners_team|corners_team": -0.28,
            "corners_team|corners_team:opp": -0.28,
            "corners_team|corners_team:same": 0.90,
            "corners_team|player_goal": 0.05,     # prior (same-team attack), wide band
            "advance|corners_team": -0.05,        # prior (diluted moneyline), wide band
            "moneyline|moneyline": -0.95,
            # Period (1st-half) × full-time, CALIBRATED 2026-07-07 on 8,981 club
            # matches (football-data.co.uk HT/FT, era-stable across a 2023
            # split; docs/calibration/results_soccer.md §1). The 1H-winner ×
            # FT-winner sign FLIPS with team orientation — resolved by sgp.py to
            # ":same" (both legs name one team) vs ":opp" (different teams);
            # draw-involving winner pairs are unmeasured and fall to the flat
            # prior. Matched-family only; cross-type (1H-winner × FT-total,
            # 1H-spread) is DEFERRED. 1H-BTTS × FT-BTTS is not a rho — it is a
            # logical CONTAINMENT handled in relationships.py, not here.
            "first_half_moneyline|moneyline:same": 0.71,
            "first_half_moneyline|moneyline:opp": -0.67,
            "first_half_total|total": 0.73,
            # 1H-winner x 1H-total, WITHIN the first half. The sign FLIPS HARD on
            # whether the 1H-moneyline leg names a TEAM or the TIE — resolved in
            # sgp.py to ":team" / ":tie" (same hard sign-flip as the 1H x FT
            # ":same"/":opp" winner pair). Both are near-DETERMINISTIC structural
            # containments MEASURED on 8,981 club matches (football-data HT
            # scores, tools/calibrate_soccer_1h_winner_total.py):
            #   :team  a 1H lead REQUIRES a goal => 1H-lead subset of 1H-over0.5,
            #          all 5,401 lead matches over -> implied rho +0.99 (clamp +0.95).
            #   :tie   1H under0.5 (0-0) is a subset of 1H-TIE, all 2,518 under
            #          matches are ties -> tie x over implied rho -0.99 (clamp -0.95).
            # Without these the pair fell to the flat same_event_rho +0.6, which
            # is the WRONG SIGN for a TIE leg (it priced tie x under as strongly
            # mutually exclusive when 0-0 IS a tie) — the SUICOL pick-off.
            "first_half_moneyline|first_half_total:team": 0.95,
            "first_half_moneyline|first_half_total:tie": -0.95,
            # First-half SPREAD (1H goal margin, series KXWC1HSPREAD; the only
            # traded line is 2 = "leads at half by over 1.5", i.e. 1H margin>=2)
            # x FULL-TIME legs, CALIBRATED 2026-07-07 on 8,981 club matches
            # (football-data HT/FT; tools/calibrate_soccer_1h_spread.py;
            # results_soccer.md §2). Kalshi BLOCKS 1H-spread x 1H-total/1H-over,
            # so only 1H-spread x full-time is reachable/measured. A 1H spread
            # NAMES a team, so spread|spread and spread|moneyline flip sign HARD
            # on team orientation — resolved in sgp.py to ":same" (both legs
            # name one team) / ":opp" (different teams) by stripping the trailing
            # line digits off the TEAM+digits suffix, the same/opposite analogue
            # of the winner ":same"/":opp" prior. Measured pooled over both
            # naming orientations:
            #   spread|spread :same  1H margin>=2 x FT margin>=2, +0.777
            #     [+0.726,+0.826] (home +0.773 / away +0.776; not deterministic
            #     — a 2-0 half can end level).
            #   spread|spread :opp   1H team-A>=2 x FT team-B>=2, measured -0.646
            #     [-0.802,-0.591]; a >=4-goal swing, P(A|B)=0.002 (near-exclusion,
            #     point estimate driven by 1-2 freak comebacks) — shipped as the
            #     copula-fit -0.65 (reproduces the observed ~0.2% frequency), NOT
            #     clamped to -0.95 (that sits outside the measured CI and would
            #     over-state), with a wide band spanning the small-sample range.
            #   spread|moneyline :same 1H margin>=2 x FT win, +0.739 [+0.652,+0.854].
            #   spread|moneyline :opp  1H team-A>=2 x FT team-B win, -0.662
            #     [-0.709,-0.624]; near-exclusion, shipped copula-fit -0.66.
            # spread|total is orientation-FREE (total names no team): a 1H lead
            # requires 1H goals => positive with FT total. Anchored at the modal
            # FT line over 2.5 (>=3): +0.518 [+0.418,+0.635] (over3.5 +0.44;
            # over1.5 is a structural containment +0.99, out of band — a rare
            # pairing, noted). Wide band absorbs the FT-line dependence.
            "first_half_spread|spread:same": 0.78,
            "first_half_spread|spread:opp": -0.65,
            "first_half_spread|moneyline:same": 0.74,
            "first_half_spread|moneyline:opp": -0.66,
            "first_half_spread|total": 0.52,
        },
        # NFL moneyline|total = 0.00 DOUBLY confirmed: pooled-vs-Vegas-lines
        # AND conditional-MLE (+0.02, SE 0.023) whose fit does NOT beat
        # independence out of sample — per the directive, a dependence that
        # loses to independence OOS is noise and must not ship.
        "nfl": {
            "moneyline|total": 0.00,
            "spread|total": 0.03,
            "moneyline|spread": 0.88,
            "extras|total": 0.20,
            "moneyline|moneyline": -0.95,
        },
        # NBA verified on MODERN data (hoopR/ESPN 2016-2025, n=12,567):
        # ml|total +0.008, era-split 2016-20 vs 21-25 drift +0.008 — the zero
        # correlation survived the 3PT revolution.
        "nba": {
            "moneyline|total": 0.01,
            "moneyline|moneyline": -0.95,
        },
        "wnba": {
            "moneyline|total": 0.01,
            "moneyline|moneyline": -0.95,
        },
        # MLB (Retrosheet 2015-2024, n=20,642): home winner is slightly
        # ANTI-correlated with over (−0.056: home wins often skip the bottom
        # 9th ⇒ fewer runs). extras|total uses the POST-2020 value (+0.10):
        # the ghost-runner rule structurally changed extras scoring
        # (pre-2020 −0.04 → post +0.10) — a measured rule-change break.
        "mlb": {
            "moneyline|total": -0.05,
            "extras|total": 0.10,
            "extras|moneyline": -0.04,
            "moneyline|moneyline": -0.95,
        },
    }
    # Band overrides: sport-prefixed keys ("nfl:moneyline|total") for
    # calibrated sport entries; unprefixed keys for the global table.
    pair_rho_uncertainty: dict[str, float] = {
        "moneyline|moneyline": 0.04,
        "soccer:moneyline|total": 0.10,     # conditional-MLE SE .019; band covers asymmetry
        "soccer:btts|total": 0.12,          # pooled-method: widened pending conditional refit
        "soccer:btts|moneyline": 0.10,      # pooled-method (but club==intl)
        "soccer:btts|moneyline:fav": 0.10,
        "soccer:btts|moneyline:dog": 0.10,  # structural implication, 1 live validation
        "soccer:moneyline|player_goal": 0.12,   # structural implication ×2 examples
        "soccer:btts|player_goal": 0.30,    # 0.55 mid; band spans fav +0.31 ↔ dog +0.81
        "moneyline|player_goal": 0.20,      # global fallback: non-soccer scorers unmeasured
        "soccer:total|total": 0.04,
        "soccer:corners|total": 0.08,
        "soccer:btts|corners": 0.08,
        "soccer:corners|moneyline": 0.08,        # total corners, measured ~0 (team is separate now)
        "soccer:advance|corners": 0.15,          # labeled prior (corners ⊥ result)
        "soccer:corners|player_goal": 0.20,      # labeled prior (~ corners_team|player_goal)
        "soccer:corners_team|moneyline": 0.10,
        "soccer:corners_team|spread": 0.10,
        "soccer:corners_team|total": 0.08,
        "soccer:btts|corners_team": 0.08,
        "soccer:corners_team|corners_team": 0.10,
        "soccer:corners_team|corners_team:opp": 0.10,   # re-measured, tight
        "soccer:corners_team|corners_team:same": 0.10,  # comonotone containment approx
        "soccer:corners_team|player_goal": 0.20,  # labeled prior
        "soccer:advance|corners_team": 0.15,      # labeled prior
        "soccer:player_goal|total": 0.15,        # hand-prior width around measured +0.46
        "soccer:player_goal|player_goal": 0.10,  # teammate(0)/opponent(+0.05) blend band
        "soccer:moneyline|moneyline": 0.04,
        # Period × full-time bands (results_soccer.md §1: era-stability proxy,
        # not the conditional-MLE gate — no live 1H book yet — so kept modest).
        "soccer:first_half_moneyline|moneyline:same": 0.08,
        "soccer:first_half_moneyline|moneyline:opp": 0.08,
        "soccer:first_half_total|total": 0.12,
        # Near-deterministic containment (tight 99% CI, both hitting the clamp);
        # modest band consistent with the 1H family (no live 1H book to gate on).
        "soccer:first_half_moneyline|first_half_total:team": 0.10,
        "soccer:first_half_moneyline|first_half_total:tie": 0.10,
        # 1H-spread × full-time bands (results_soccer.md §2; era-stability proxy,
        # no live 1H book to conditional-MLE gate — kept in the 1H family's
        # 0.10-0.15 range). :opp / total wider for small-sample / FT-line spread.
        "soccer:first_half_spread|spread:same": 0.12,
        "soccer:first_half_spread|spread:opp": 0.15,
        "soccer:first_half_spread|moneyline:same": 0.12,
        "soccer:first_half_spread|moneyline:opp": 0.15,
        "soccer:first_half_spread|total": 0.15,
        "nfl:moneyline|total": 0.05,
        "nfl:spread|total": 0.05,
        "nfl:moneyline|spread": 0.05,
        "nfl:extras|total": 0.10,
        "nfl:moneyline|moneyline": 0.04,
        "nba:moneyline|total": 0.05,
        "nba:moneyline|moneyline": 0.04,
        "wnba:moneyline|total": 0.12,       # NBA transfer: wider until measured
        "wnba:moneyline|moneyline": 0.04,
        "mlb:moneyline|total": 0.06,
        "mlb:extras|total": 0.10,           # post-rule-change sample is smaller
        "mlb:extras|moneyline": 0.08,
        "mlb:moneyline|moneyline": 0.04,
    }
    # Orientation CURVES: a pair whose YES-YES rho is a monotone function of one
    # leg's marginal (not a single scalar or a fav/dog step). Keyed
    # "<sport>:<pair_key>" -> sorted (marginal, rho) knots, piecewise-linear
    # interpolated in sgp.py with FLAT clamp outside the knot range; the curve
    # WINS over the scalar / fav-dog entry whenever leg marginals are available.
    # soccer:btts|moneyline RE-MEASURED 2026-07-07 (8,982 top-5-EU matches,
    # btts x team-win implied rho binned across devigged closing win-prob): heavy
    # longshot (~0.20) ~ 0 (NOT the -0.19 the old 2-anchor blend over-negated),
    # deepening monotonically to ~-0.36 for heavy favorites ("winners keep clean
    # sheets" is a favorites effect; a dog can only win by scoring).
    oriented_curve: dict[str, list[tuple[float, float]]] = {
        "soccer:btts|moneyline": [
            (0.20, -0.05),
            (0.35, -0.18),
            (0.50, -0.28),
            (0.65, -0.34),
            (0.85, -0.36),
        ],
    }
    oriented_curve_uncertainty: dict[str, float] = {
        "soccer:btts|moneyline": 0.13,
    }


class QuoteConfig(StrictModel):
    base_width_cc: int = 200
    per_leg_width_cc: int = 100
    # legs component = per_leg × n^convexity — model error compounds
    # multiplicatively with leg count, so width should too (1.0 = linear).
    leg_count_convexity: float = 1.0
    uncertainty_width_scale: float = 1.0
    size_width_cc_per_100: int = 50
    time_wide_threshold_s: float = 21_600.0
    time_width_cc: int = 200
    in_play_extra_cc: int = 800
    min_capture_cc: int = 100
    free_money_margin_cc: int = 100
    # Longshots: absolute uncertainty shrinks with tiny fairs (gradient ∝ P),
    # which is anti-conservative in RELATIVE terms for whoever shorts the
    # longshot — floor it as a fraction of fair below the threshold.
    longshot_fair_threshold: float = 0.15
    longshot_min_rel_uncertainty: float = 0.25
    # Favorites stacks: well-estimated products, price-insensitive flow —
    # tighten to win it (1.0 = off; validate via markouts before lowering).
    favorite_leg_threshold: float = 0.65
    favorite_width_multiplier: float = 1.0


class ExternalOddsConfig(StrictModel):
    """SportsGameOdds adapter (docs/api-notes/sportsgameodds.md). OFF by
    default; free tier is 2,500 objects/month so the poller is budget-gated."""

    enabled: bool = False
    leagues: list[str] = []               # e.g. ["NBA", "NFL"]
    weight: float = 0.3                   # blend weight vs Kalshi book's 1.0
    poll_interval_s: float = 3_600.0
    max_events_per_league: int = 10
    max_age_s: float = 900.0              # cached marginal expiry
    devig_method: str = "power"
    base_uncertainty: float = 0.01
    # Explicit Kalshi ticker → "eventID|oddID" table; unmapped = Kalshi-only.
    mapping: dict[str, str] = {}


class StructuralConfig(StrictModel):
    """Dixon-Coles scoreline pricer for soccer SGPs (pricing/dixon_coles.py).

    ``enabled`` may be flipped ON only by an out-of-sample gate result
    (tools/validate_structural_oos.py): a structural fit that does not beat
    the v1 copula on held-out seasons is noise and must not ship.

    GATE PASSED 2026-07-06 (8,980 club games, train <2024 / test 23/24+24/25):
    structural beats the SHIPPED v1 copula on all three OOS joint-log-loss
    metrics — hw×over 1.24657 vs 1.24734, hw×btts 1.26330 vs 1.26724, and the
    3-leg triple 1.70607 vs 1.74775 (independence 1.94197). The margin GROWS
    with combo complexity: coherent scorelines beat pairwise rho stitching
    most where the maker quotes most."""

    enabled: bool = True
    max_goals: int = 12
    # DC low-score adjustment: FITTED on train-season scorelines through the
    # production inversion (grid MLE, tools/validate_structural_oos.py).
    dc_rho: float = -0.05
    dc_rho_band: float = 0.08
    # ET intensity as a fraction of regulation scoring rate (30min pro-rata =
    # 1/3); band edges re-price the joint for the model-form width.
    et_factor: float = 0.3333
    et_factor_low: float = 0.25
    et_factor_high: float = 0.40
    # P(named team wins a shootout | level after ET) for Advance legs.
    # Kalshi's knockout game market includes pens (rules text 2026-07-06);
    # 0.5 prior with a band covering first-kicker/keeper effects.
    pens_win_prob: float = 0.5
    pens_band: float = 0.10
    misfit_uncertainty_scale: float = 1.0
    # Series prefixes whose matches are knockout format (ET+pens possible).
    # Settlement windows per family are RULE-BOOK (operator-provided Kalshi
    # rules text 2026-07-06): knockout game market = advance incl pens;
    # BTTS/totals = regulation; player props = full game excl pens.
    # Remaining assumption: this maps SERIES, not match phase — a group-stage
    # KXWC match would be misclassified (fine for the current knockout
    # rounds; revisit for the next tournament's group stage).
    knockout_series: list[str] = ["KXWC"]


class MarginTotalConfig(StrictModel):
    """Bivariate-normal (margin, total) structural pricer for NFL/NBA/WNBA
    (pricing/margin_total.py). Per-game means invert from live prices; the
    sport shapes below are calibrated from RECENT seasons
    (tools/calibrate_margin_total.py, 2026-07-06 — NFL 2020-2025 closing-line
    residuals; NBA 2022-2026 & WNBA 2021-2026 team-fixed-effects residuals,
    the FE method validated against NFL's line residuals). A sport enters
    ``enabled_sports`` ONLY via an OOS gate (directive point 4).

    NFL GATE PASSED 2026-07-06 (tools/validate_margin_total_oos.py; train
    2015-2023, test 2024-2025 n=562): structural beats the shipped v1 copula
    on all three OOS metrics — hw×over 1.29275 vs 1.29293, hw×cover 0.96260
    vs 0.99940 (exact comonotone geometry vs the 0.88 approximation), triple
    1.65544 vs 1.69217.

    WNBA enabled 2026-07-06 by OPERATOR REQUEST (season live now): the
    geometry is the NFL-gated one, the WNBA shape is calibrated on 1,338
    recent games through 2026-07-05, and its rho≈0 means ml×total prices
    within noise of the incumbent v1 — the upgrade is coherent spread/
    team-total joints. Confirmation gate from prod-shadow settlements as
    data accrues. NBA stays disabled until its season approaches (odds
    source or shadow gate)."""

    enabled_sports: list[str] = ["nfl", "wnba"]
    params: dict[str, dict[str, float]] = {
        "nfl": {"sigma_margin": 12.66, "sigma_total": 13.06, "rho": 0.026},
        "nba": {"sigma_margin": 13.71, "sigma_total": 18.42, "rho": 0.000},
        "wnba": {"sigma_margin": 12.04, "sigma_total": 16.55, "rho": -0.019},
    }
    sigma_band_frac: float = 0.05   # FE-vs-line method gap on NFL was ~3%
    rho_band: float = 0.05
    # Normal approximation of discrete scores (NFL key numbers worst):
    # flat probability adder when any margin-flavored leg is present.
    discreteness_unc: dict[str, float] = {"nfl": 0.010, "nba": 0.004, "wnba": 0.005}
    misfit_uncertainty_scale: float = 1.0


class MlbRunsConfig(StrictModel):
    """NegBin runs model for MLB SGPs (pricing/mlb_runs.py). Dispersion k
    calibrated from Retrosheet final scores (tools/calibrate_mlb_runs.py,
    2026-07-06): k=3.62 on 2021-2024, k=3.63 on 2015-2019 — era-stable; the
    band covers the unmodeled home/away asymmetry (k 3.37 away / 3.91 home —
    tickers don't reveal the home side).

    GATE PASSED 2026-07-06 (tools/validate_mlb_runs_oos.py; SBR closing-odds
    archive, k from the 2015-2019 train era, test = 2021 season n=2,351):
    structural beats the shipped v1 copula on all three OOS joint-log-loss
    metrics — hw×over 1.36134 vs 1.36300, hw×runline-cover 1.00824 vs
    1.12151 (v1 has NO calibrated ml|spread for MLB and its flat 0.6 prior
    is badly wrong), triple 1.71126 vs 1.88090. Also measured: v1's pooled
    ml|total −0.05 LOSES to independence OOS (1.36300 vs 1.36209) — the
    runs grid supersedes it for same-game combos. Confirmation re-gate on
    prod-shadow settlements as they accrue."""

    enabled: bool = True
    # 2021-2025 window (Retrosheet gl2025 fetched 2026-07-06); was 3.62 on
    # 2021-2024, 3.63 on 2015-2019 — drift well inside the band.
    dispersion_k: float = 3.54
    k_band: float = 0.30
    misfit_uncertainty_scale: float = 1.0


class PricingConfig(StrictModel):
    fee: FeeConfig = Field(default_factory=FeeConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    quote: QuoteConfig = Field(default_factory=QuoteConfig)
    structural: StructuralConfig = Field(default_factory=StructuralConfig)
    margin_total: MarginTotalConfig = Field(default_factory=MarginTotalConfig)
    mlb_runs: MlbRunsConfig = Field(default_factory=MlbRunsConfig)
    external_odds: ExternalOddsConfig = Field(default_factory=ExternalOddsConfig)
    max_source_disagreement: float = 0.08


class RiskConfig(StrictModel):
    """Limits + last-look + in-play policies. Enforced pre-quote AND pre-confirm."""

    max_contracts_per_quote: float = 100.0
    max_notional_per_quote_dollars: float = 500.0
    max_market_delta_contracts: float = 300.0
    max_event_delta_contracts: float = 500.0
    max_gross_notional_dollars: float = 5_000.0
    max_open_quotes: int = 20
    max_daily_loss_dollars: float = 500.0
    max_event_worst_case_loss_dollars: float = 1_000.0
    # last look
    leg_move_tolerance_cc: int = 150
    joint_move_tolerance_cc: int = 200
    max_leg_age_s: float = 2.0
    # in-play detection
    velocity_window_s: float = 5.0
    velocity_threshold_cc: int = 300
    update_count_threshold: int = 25
    in_play_cooldown_s: float = 30.0


class ObserveConfig(StrictModel):
    rfq_poll_s: float = 30.0          # REST reconciliation cadence (no seq on WS)
    would_quote_width_cc: int = 600   # stub half-spread total ($0.06) for logging only
    db_filename: str = ""             # "" = auto: combomaker-{env}.sqlite3

    def db_name_for(self, env: Env) -> str:
        # Demo and prod data must never share a store — shadow analytics on
        # prod flow would silently blend with demo bot noise.
        return self.db_filename or f"combomaker-{env.value}.sqlite3"


class AppConfig(StrictModel):
    env: Env = Env.DEMO
    mode: Mode = Mode.OBSERVE
    endpoints: EndpointsConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    observe: ObserveConfig = Field(default_factory=ObserveConfig)
    data_dir: Path = Path("data")
    kill_file: Path = Path("KILL")
    # confirm_live comes only from the CLI flag --confirm-live, never from YAML:
    # a file can't accidentally arm production.
    confirm_live: bool = Field(default=False, exclude=True)

    def assert_safe_to_run(self) -> None:
        """Hardcoded production guard. Raises ProdGuardError on violation."""
        if self.env is Env.PROD and self.mode is Mode.QUOTE:
            if not self.confirm_live:
                raise ProdGuardError(
                    "quoting on production requires the explicit --confirm-live flag"
                )
            if not self.safety.prod_limits_configured:
                raise ProdGuardError(
                    "quoting on production requires safety.prod_limits_configured: true "
                    "in the prod config, set after limits are reviewed"
                )


class ProdGuardError(RuntimeError):
    pass


class ConfigError(ValueError):
    pass


def load_config(
    path: Path,
    *,
    env: Env | None = None,
    mode: Mode | None = None,
    confirm_live: bool = False,
) -> AppConfig:
    """Load YAML config; CLI values override the file's env/mode."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {path}")
    if "confirm_live" in raw:
        # A file must never be able to arm production; only the CLI flag can.
        raise ConfigError("confirm_live cannot be set from config files; use --confirm-live")

    if env is not None:
        raw["env"] = str(env)
    resolved_env = Env(raw.get("env", Env.DEMO))
    if mode is not None:
        raw["mode"] = str(mode)
    raw.setdefault("endpoints", EndpointsConfig.for_env(resolved_env).model_dump())

    config = AppConfig.model_validate(raw)
    return config.model_copy(update={"confirm_live": confirm_live})
