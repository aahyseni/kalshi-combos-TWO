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
    # Decline combos that carry a two-legged-tie knockout leg (UCL/UEL/UECL):
    # "advance" there is decided over two legs, so a single-match win does NOT
    # imply advancing and the single-match soccer priors mis-apply. Gated off
    # until its own regime is built. Flip to false once that regime exists.
    decline_two_legged_tie: bool = True


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
            "corners|spread": 0.00,       # TOTAL corners ⊥ margin (measured +0.02)
            "btts|corners": 0.00,
            # CORNERS × FIRST-HALF markets. Corners are in NO scoreline model
            # (structural declines a corners leg -> copula), and total corners
            # are MEASURED ⊥ result/goals — same basis as corners|total/moneyline
            # above — so a corners × 1H pair is ~0, NOT the flat +0.6 the engine
            # used to hit (corners × 1H was UNTYPED, ~21k tape combos). 0.00 for
            # every 1H family (winner/total/btts/spread), total AND team corners.
            "corners|first_half_moneyline": 0.00,
            "corners|first_half_total": 0.00,
            "corners|first_half_btts": 0.00,
            "corners|first_half_spread": 0.00,
            "corners_team|first_half_moneyline": 0.00,
            "corners_team|first_half_total": 0.00,
            "corners_team|first_half_btts": 0.00,
            "corners_team|first_half_spread": 0.00,
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
            # TOTAL corners × a TEAM's corners (same game). Unlike the other
            # corners pairs (measured ~0), the total CONTAINS the team's corners
            # as a large component, so they are strongly comonotone — the one
            # pair where the old +0.6 fallback POINT was accidentally close, but
            # its fail-safe band spanned zero (treating a structurally-certain-
            # positive pair as maybe-negative → over-wide quotes on corners-heavy
            # combos; RFQ test C24/C25/C26). MEASURED 2026-07-08 on 8,981 matches
            # by two independent passes (football-data HC/AC; total=HC+AC): copula
            # rho +0.61-0.66 at real tape lines (KXWCCORNERS 7-10 × KXWCTCORNERS
            # 4-6), home +0.65 / away +0.57 (home corners a bigger share of the
            # total). No :same/:opp — corners_team carries no home/away orient, so
            # a single home/away blend ships with a wide band spanning the split.
            "corners|corners_team": 0.62,
            # ADVANCE (knockout progress) × full-time markets. ADVANCE was
            # UNLISTED against total/btts/scorer/spread → fell to the flat +0.6
            # fallback (6× too high on totals, WRONG SIGN on btts/opp-scorer),
            # and advance is the SINGLE most common soccer leg (1.02M tape legs).
            # DERIVED 2026-07-07 from our own Dixon-Coles model (advance = win90 +
            # ET + 0.5 shootout) and CROSS-CHECKED against 4 historical-knockout
            # studies (WC/Euros n=247, Copa/AFCON/Asian/Gold n=185, UCL n=310,
            # UEL/UECL n=78): the DC's implied k=P(advance|reg-draw) 0.50(even)→
            # 0.64(fav) MATCHES the measured k, and the attenuation matches too.
            # advance is a moneyline attenuated by the ~35% of ties that go to a
            # scoreline-decoupled shootout: SYMMETRIC goal markets (total, btts)
            # retain ~½ (btts stays NEGATIVE like btts|moneyline); DIRECTIONAL
            # markets (scorer, spread) retain ~0.8. LINE-STABLE across total lines
            # incl. over-0.5 (advance, unlike a win, does NOT imply a goal — a 0-0
            # shootout advances). player_goal flips sign on team orientation
            # (scorer on the advancing team vs the opponent) — resolved in sgp.py
            # to :same/:opp. spread≥2 ⟹ win ⟹ advance is a near-containment
            # (Kalshi blocks the conflict anyway). NOTE: this is the SINGLE-MATCH
            # regime (KXWC*); two-legged UCL/UEL/UECL is a DIFFERENT regime
            # (symmetric→0) — see memory, wire by ticker series when it lists.
            "advance|total": 0.12,
            "advance|btts": -0.07,
            "advance|player_goal:same": 0.45,
            "advance|player_goal:opp": -0.45,
            "advance|spread": 0.95,
            # SPREAD (win-by-margin) × full-time markets — also UNLISTED → +0.6
            # fallback. DERIVED 2026-07-07 from the DC model (GoalSpread min_margin=2
            # = "wins by >=2"): spread is a STRONGER directional signal than a bare
            # win. spread|total is LINE-DEPENDENT — a 2+ margin FORCES >=2 goals so
            # spread⊂over-1.5 is a near-containment (rho~+0.999) BUT Kalshi blocks
            # spread×total conflicts, so only the reachable over-2.5+ value ships:
            # +0.31 (spread≥2 vs >=3 goals, range +0.22 even→+0.46 heavy-fav, wide
            # band). spread|btts NEGATIVE (a clean 2-0 dominant win → not-btts, like
            # a moneyline but stronger). spread|player_goal flips on team orientation
            # (scorer on the spread-winning team vs opponent) — resolved in sgp.py
            # to :same/:opp, same as advance|player_goal.
            # (keys are pair_key-sorted alphabetically: btts<player_goal<spread<total)
            "spread|total": 0.31,
            "btts|spread": -0.30,
            "player_goal|spread:same": 0.46,
            "player_goal|spread:opp": -0.42,
            # TEAM corners (KXWCTCORNERS), MEASURED conditional on 8,981 matches
            # (team-strength + venue controlled, so the residual the copula needs
            # after live marginals). Unlike TOTAL corners, a TEAM's corners are
            # NEGATIVELY tied to that team winning / covering — the chasing team
            # piles on corners (pooled sign is a Simpson's-paradox artifact).
            # ⊥ goals (total/btts ≈ 0). player_goal / advance are LABELED PRIORS
            # (no football-data coverage), wide-banded, not measured.
            # team corners × match winner — ORIENTED (:same/:opp/:tie). A team's
            # corners are anti-correlated with THAT team winning (chasing team
            # earns corners) and positively with the OPPONENT winning; ~0 with a
            # draw. STRENGTH-CONTROLLED (2026-07-08, 8,980 matches, binned by
            # devigged win prob): raw pooled corr is a Simpson trap (+0.05, wrong
            # sign); conditional :same −0.154 / :opp +0.152 / :tie +0.014. The
            # plain entry is the marginal-less / unparseable-orientation fallback.
            "corners_team|moneyline": -0.15,
            "corners_team|moneyline:same": -0.15,
            "corners_team|moneyline:opp": 0.15,
            "corners_team|moneyline:tie": 0.00,
            # team corners × spread — ORIENTED (:same/:opp), the sibling of
            # corners_team|moneyline. −ρ if the corners team is the one covering
            # the margin (chasing/pressing team earns corners), +ρ if the
            # OPPONENT covers. STRENGTH-CONTROLLED (2026-07-08, 8,980 matches):
            # raw pooled +0.07 (Simpson trap, wrong sign); conditional
            # :same −0.114 / :opp +0.109. Plain entry = unparseable-orient fallback.
            "corners_team|spread": -0.13,
            "corners_team|spread:same": -0.11,
            "corners_team|spread:opp": 0.11,
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
            # FT-BTTS x 1H-TOTAL-over-N. UNLISTED -> the pair fell to the flat
            # +0.6 same_event fallback whenever the DC pricer declined (live-RFQ
            # combos C22/C27/C28 mispriced vs maker clearing). DERIVED 2026-07-08
            # TWO independent ways on 8,981 club matches (football-data HT/FT,
            # tools/calibrate_soccer_btts_1h_total.py):
            #   STRUCTURAL (shipped half-time DC, inverted per game from the FT
            #     1X2 + O/U-2.5 lines at h=0.45 / dc_rho=-0.05): pooled implied
            #     rho +0.533 (N=1, over-0.5) / +0.544 (N=2, over-1.5).
            #   EMPIRICAL (count P(FT-BTTS & 1H>=N), invert the shipped copula):
            #     +0.570 [+0.482,+0.653] (N=1) / +0.552 [+0.477,+0.623] (N=2).
            # Methods AGREE (|diff| <= 0.037) and the pair is LINE-STABLE (N=1 vs
            # N=2 within 0.02 in BOTH methods) -> one entry, no line-specific key.
            # +0.55 sits at the center of both methods and both lines. COHERENT
            # with the anchors: BELOW first_half_total|total (+0.73 — 1H-total is
            # a component of FT-total) and btts|total (+0.70), because FT-BTTS
            # needs BOTH teams to score across the FULL 90' while a 1H goal is
            # either team inside 45' — a looser, once-removed link (~0.70*0.73
            # latent attenuation lands near +0.51, measured a touch higher for the
            # direct 1H-goal path). Settlement: BTTS = regulation 90', 1H = 45'.
            # Era-stability / structural proxy (no live 1H book to conditional-MLE
            # gate yet), so the 1H-family band until a live 1H book tightens it.
            "btts|first_half_total": 0.55,
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
            # results_soccer.md §2). NOTE: the 1H-spread x 1H-total / 1H-btts /
            # 1H-winner pairs ARE reachable (the prod tape has real same-game
            # combos — an earlier "Kalshi blocks 1H×1H" assumption was wrong);
            # they are DEFERRED (uncalibrated → +0.6 fallback), not blocked. Only
            # 1H-spread x FULL-TIME is calibrated here. A 1H spread
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
            # === 1H CROSS-TYPE CLUSTER (calibrated 2026-07-08, 3-agent batch:
            # half-time Dixon-Coles + football-data HT/FT + Understat; every pair
            # cross-validated structural vs empirical <=0.04, strength-controlled
            # where a result leg confounds). Retires the +0.6 fallback on the
            # ~700k same-game combos the audit flagged. Bands are the 1H-family
            # era/structural proxy (no live 1H book to conditional-MLE gate yet).
            # -- 1H-winner (first_half_moneyline) x FT, oriented :same/:opp/:tie --
            "advance|first_half_moneyline:same": 0.64,
            "advance|first_half_moneyline:opp": -0.64,
            "advance|first_half_moneyline:tie": 0.00,
            "first_half_moneyline|total:team": 0.24,
            "first_half_moneyline|total:tie": -0.42,
            "first_half_moneyline|player_goal:same": 0.45,
            "first_half_moneyline|player_goal:opp": -0.20,
            "first_half_moneyline|player_goal:tie": -0.22,
            # btts x 1H-lead is POSITIVE (a 1H lead = a goal already happened +
            # open game), UNLIKE FT btts|moneyline (negative = clean sheet).
            "btts|first_half_moneyline:team": 0.10,
            "btts|first_half_moneyline:tie": -0.17,
            "first_half_moneyline|spread:same": 0.70,
            "first_half_moneyline|spread:opp": -0.63,
            "first_half_moneyline|spread:tie": -0.32,
            # -- 1H-total / 1H-btts x FT, plain scalars (no team named) --
            "advance|first_half_total": 0.09,
            "first_half_total|moneyline": 0.14,
            "first_half_total|spread": 0.27,
            "first_half_total|player_goal": 0.33,
            "advance|first_half_btts": -0.03,
            "first_half_btts|total": 0.65,       # o2.5 anchor; o1.5 = exact containment
            "first_half_btts|moneyline": -0.03,
            "first_half_btts|spread": -0.08,
            "first_half_btts|player_goal": 0.33,
            # -- 1H x 1H (within-half; logical containments hit +-0.95) + 1H-spread x FT --
            "first_half_spread|first_half_total": 0.95,   # 1H margin>=2 => 1H over1.5
            "first_half_btts|first_half_moneyline:team": -0.18,
            "first_half_btts|first_half_moneyline:tie": 0.30,
            "first_half_moneyline|first_half_spread:same": 0.95,
            "first_half_moneyline|first_half_spread:opp": -0.95,
            "first_half_moneyline|first_half_spread:tie": -0.95,
            "first_half_btts|first_half_spread": -0.22,
            "first_half_btts|first_half_total": 0.95,      # 1H-btts => 1H over1.5
            "advance|first_half_spread:same": 0.72,
            "advance|first_half_spread:opp": -0.72,
            "btts|first_half_spread": 0.00,      # VERIFIED ~0 (2H recovery cancels it)
            "first_half_spread|player_goal:same": 0.45,
            "first_half_spread|player_goal:opp": -0.22,
            # FT advance x regulation-DRAW (the OBSERVED advance|moneyline flow; a
            # draw is symmetric re: which team advances -> ~0. Team cases are
            # logical containment/impossible, handled in relationships.py).
            "advance|moneyline:tie": 0.00,
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
            # ---- MLB props tranche (Retrosheet 2005-25, 49,486 games; 8-agent
            # measurement pass + xhigh adversarial judge, 2026-07-09; source of
            # truth docs/calibration/staged_mlb_props.md FINAL RECOMMENDED
            # TABLE). All keys legtypes.pair_key-sorted, verified by running the
            # helper. Retires the sign-wrong flat +0.60/0.90 fallback these
            # same-game pairs hit while KXMLBKS/HIT/HR/HRR/TB/RFI typed UNKNOWN.
            # -- [A] orientation-free MEASURED --
            # Starter K over × GAME total over: cluster-boot 99% [-0.271,-0.230];
            # ladder-FLAT across all posted K lines 3.5-8.5, so ONE entry serves
            # every KS rung (self-median line convention judge-validated).
            "player_ks|total": -0.25,
            # MEASURED +0.233 in the GAME-total frame. NOT the team-frame +0.367:
            # that is HR × OWN-TEAM total (KXMLBTEAMTOTAL), which is NOT
            # combo-eligible — loading it here would mis-sign/mis-scale
            # (staged_mlb_props.md:166-169). Dilution below team-frame is required.
            "player_hr|total": 0.24,
            "player_hit|total": 0.25,      # rung-monotone 0.19(1+)/0.25(2+)/0.30(3+)
            "player_tb|total": 0.27,       # rungs: 2+ 0.26 / 4+ 0.28
            "player_hrr|total": 0.40,      # starters-only pop; line-monotone .33/.40/.44
            "rfi|total": 0.37,             # strongest + most era-stable MLB pair measured
            "moneyline|rfi": 0.00,         # either-team RFI is team-symmetric ⇒ ⊥ winner
            "player_ks|rfi": -0.10,        # orientation-free; ships without the resolver
            "player_ks|player_ks": 0.04,   # opposing starters; sign confirmed small-pos
            # Copula-FALLBACK only: the structural margin/total grid supersedes
            # this whenever it prices; it fires on structural declines. Parity
            # coupling explains the fixed-line oscillation seen in calibration.
            "spread|total": 0.13,
            # -- [B] same-family batter pairs, UNROUTED -- teammate/opponent
            # ticker-prefix routing is the next phase; these are the measured
            # sign-spanning UNROUTED blends (e.g. hrr: teammate +0.17 / opponent
            # 0.00). Bands span both routed values.
            "player_hr|player_hr": 0.03,
            "player_hit|player_hit": 0.00,
            "player_tb|player_tb": 0.00,
            "player_hrr|player_hrr": 0.08,
            # -- [C] ML-orientation-resolver-gated, NEUTRALIZED -- the measured
            # values are team-oriented (+/-0.24 ml|ks, +/-0.23 ml|hr, +/-0.23
            # ml|hit, +/-0.37 ml|hrr; facing-pitcher hit|ks -0.13, hr|ks -0.075,
            # hrr|ks -0.18) and sign-FLIP when the ML leg is the opponent.
            # sgp.py has no MLB team-orientation resolver yet (the fav/dog
            # marginal axis is the WRONG axis) — until a resolver compares the
            # prop ticker's team prefix to the ML suffix, ship 0.00 with a
            # sign-spanning band: point error <=0.37 vs up to 0.84 at +0.60.
            # The signed values above unlock with the resolver.
            "moneyline|player_ks": 0.00,
            "moneyline|player_hr": 0.00,
            "moneyline|player_hit": 0.00,
            "moneyline|player_hrr": 0.00,
            "player_hit|player_ks": 0.00,
            "player_hr|player_ks": 0.00,
            # Judge-CONFIRMED neutralized: binding constraint is facing/starters
            # -0.190 (CI lo -0.196), not the now-measured ~0 teammate.
            "player_hrr|player_ks": 0.00,
            # -- [D] cross-family batter-batter, distinct players (judge-approved
            # 2026-07-09; bounded by the measured same-family values; hr|hrr
            # measured teammate +0.05 / opponent 0.00) --
            "player_hit|player_hr": 0.01,
            "player_hit|player_hrr": 0.04,
            "player_hit|player_tb": 0.02,
            "player_hr|player_tb": 0.02,
            "player_hrr|player_tb": 0.04,
            # ks|tb: judge RE-CENTERED from the resolver-gated 0.00/0.15
            # placeholder — orientation-free like player_ks|rfi.
            "player_ks|player_tb": -0.06,
            # TODO(route, mlb): "moneyline|spread" is deliberately NOT tabled —
            # truth is containment-shaped +/-0.95 by side (:same +0.95 / :opp
            # -0.95, exact: 0/98,980 violations); NO unoriented scalar is right,
            # so it falls to the flat default until same/opp routing ships.
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
        "soccer:corners|spread": 0.08,           # total corners ⊥ margin (measured +0.02)
        # corners × 1H: grounded near-zero (corners ⊥ result/goals), wide band.
        "soccer:corners|first_half_moneyline": 0.10,
        "soccer:corners|first_half_total": 0.10,
        "soccer:corners|first_half_btts": 0.10,
        "soccer:corners|first_half_spread": 0.10,
        "soccer:corners_team|first_half_moneyline": 0.10,
        "soccer:corners_team|first_half_total": 0.10,
        "soccer:corners_team|first_half_btts": 0.10,
        "soccer:corners_team|first_half_spread": 0.10,
        "soccer:advance|corners": 0.15,          # labeled prior (corners ⊥ result)
        # advance × full-time bands (DC-derived + 4-study cross-check; the
        # correlation swings 0→~0.22 with favorite strength, so a wide band):
        "soccer:advance|total": 0.15,
        "soccer:advance|btts": 0.13,
        "soccer:advance|player_goal:same": 0.15,
        "soccer:advance|player_goal:opp": 0.15,
        "soccer:advance|spread": 0.10,          # near-containment, tight
        # spread × full-time bands (DC-derived; spread|total wide for the line
        # dependence, the rest span the matchup range):
        "soccer:spread|total": 0.20,
        "soccer:btts|spread": 0.13,
        "soccer:player_goal|spread:same": 0.15,
        "soccer:player_goal|spread:opp": 0.15,
        "soccer:corners|player_goal": 0.20,      # labeled prior (~ corners_team|player_goal)
        "soccer:corners_team|moneyline": 0.10,
        "soccer:corners_team|moneyline:same": 0.10,
        "soccer:corners_team|moneyline:opp": 0.10,
        "soccer:corners_team|moneyline:tie": 0.08,
        "soccer:corners_team|spread": 0.10,
        "soccer:corners_team|spread:same": 0.10,
        "soccer:corners_team|spread:opp": 0.10,
        "soccer:corners_team|total": 0.08,
        "soccer:btts|corners_team": 0.08,
        "soccer:corners|corners_team": 0.15,  # home/away split (0.57-0.65) + line drift
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
        # FT-BTTS × 1H-total: two-method agreement (structural +0.53/+0.54,
        # empirical +0.57/+0.55) + line-stability; 1H-family proxy band (no live
        # 1H book to conditional-MLE gate). Spans both methods and the empirical
        # 99% CI up to +0.65.
        "soccer:btts|first_half_total": 0.13,
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
        # 1H cross-type cluster bands (2026-07-08; 1H-family era/structural proxy)
        "soccer:advance|first_half_moneyline:same": 0.12,
        "soccer:advance|first_half_moneyline:opp": 0.12,
        "soccer:advance|first_half_moneyline:tie": 0.10,
        "soccer:first_half_moneyline|total:team": 0.08,
        "soccer:first_half_moneyline|total:tie": 0.10,
        "soccer:first_half_moneyline|player_goal:same": 0.15,
        "soccer:first_half_moneyline|player_goal:opp": 0.18,
        "soccer:first_half_moneyline|player_goal:tie": 0.12,
        "soccer:btts|first_half_moneyline:team": 0.10,
        "soccer:btts|first_half_moneyline:tie": 0.10,
        "soccer:first_half_moneyline|spread:same": 0.10,
        "soccer:first_half_moneyline|spread:opp": 0.12,
        "soccer:first_half_moneyline|spread:tie": 0.12,
        "soccer:advance|first_half_total": 0.16,
        "soccer:first_half_total|moneyline": 0.13,
        "soccer:first_half_total|spread": 0.14,
        "soccer:first_half_total|player_goal": 0.17,
        "soccer:advance|first_half_btts": 0.12,
        "soccer:first_half_btts|total": 0.13,
        "soccer:first_half_btts|moneyline": 0.10,
        "soccer:first_half_btts|spread": 0.11,
        "soccer:first_half_btts|player_goal": 0.18,
        "soccer:first_half_spread|first_half_total": 0.10,
        "soccer:first_half_btts|first_half_moneyline:team": 0.10,
        "soccer:first_half_btts|first_half_moneyline:tie": 0.10,
        "soccer:first_half_moneyline|first_half_spread:same": 0.10,
        "soccer:first_half_moneyline|first_half_spread:opp": 0.10,
        "soccer:first_half_moneyline|first_half_spread:tie": 0.10,
        "soccer:first_half_btts|first_half_spread": 0.10,
        "soccer:first_half_btts|first_half_total": 0.10,
        "soccer:advance|first_half_spread:same": 0.13,
        "soccer:advance|first_half_spread:opp": 0.15,
        "soccer:btts|first_half_spread": 0.10,
        "soccer:first_half_spread|player_goal:same": 0.15,
        "soccer:first_half_spread|player_goal:opp": 0.15,
        "soccer:advance|moneyline:tie": 0.10,
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
        # MLB props tranche bands (2026-07-09 measurement pass + judge; see the
        # pair_rho_by_sport["mlb"] block for provenance).
        # [A] measured, orientation-free:
        "mlb:player_ks|total": 0.12,        # spans era drift -0.28..-0.22
        "mlb:player_hr|total": 0.10,
        "mlb:player_hit|total": 0.12,       # spans the 1+/2+/3+ rung ladder
        "mlb:player_tb|total": 0.10,
        "mlb:player_hrr|total": 0.08,
        "mlb:rfi|total": 0.10,
        "mlb:moneyline|rfi": 0.05,
        "mlb:player_ks|rfi": 0.08,
        "mlb:player_ks|player_ks": 0.08,
        "mlb:spread|total": 0.10,
        # [B] same-family unrouted (span teammate/opponent):
        "mlb:player_hr|player_hr": 0.06,
        "mlb:player_hit|player_hit": 0.08,
        "mlb:player_tb|player_tb": 0.08,
        "mlb:player_hrr|player_hrr": 0.12,
        # [C] neutralized (sign-spanning until the ML orientation resolver):
        "mlb:moneyline|player_ks": 0.30,    # spans +/-0.24 orientation
        "mlb:moneyline|player_hr": 0.28,    # spans +/-0.23
        "mlb:moneyline|player_hit": 0.26,   # spans +/-0.23
        "mlb:moneyline|player_hrr": 0.40,   # spans +/-0.37
        "mlb:player_hit|player_ks": 0.15,   # facing -0.13 / teammate ~0
        "mlb:player_hr|player_ks": 0.12,    # facing -0.075
        "mlb:player_hrr|player_ks": 0.20,   # facing -0.18 (judge-confirmed band)
        # [D] cross-family batter-batter (judge-approved):
        "mlb:player_hit|player_hr": 0.06,
        "mlb:player_hit|player_hrr": 0.10,
        "mlb:player_hit|player_tb": 0.08,
        "mlb:player_hr|player_tb": 0.06,
        "mlb:player_hrr|player_tb": 0.10,
        "mlb:player_ks|player_tb": 0.10,    # judge re-centered -0.06/0.10
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
    # Fade defense (2026-07-08): quote combos ONE-SIDED as a pure parlay SELLER —
    # force yes_bid=0 so we can only ever end up LONG NO (sell the parlay), never
    # LONG YES (buy it). On Kalshi a combo is a two-sided binary; accepting our
    # yes_bid makes US long YES = the adversely-selected fade flow (settlement
    # backtest −14¢/ct on that side); accepting our no_bid makes us long NO = the
    # +EV seller side. Enabled in prod.yaml/demo.yaml; pydantic default stays
    # False so the pricing primitive + its tests remain two-sided. See
    # docs/reports/2026-07-08-combo-yes-no-side-mechanics.md.
    sell_parlays_only: bool = False
    # Longshots: absolute uncertainty shrinks with tiny fairs (gradient ∝ P),
    # which is anti-conservative in RELATIVE terms for whoever shorts the
    # longshot — floor it as a fraction of fair below the threshold.
    longshot_fair_threshold: float = 0.15
    longshot_min_rel_uncertainty: float = 0.25
    # Favorites stacks: well-estimated products, price-insensitive flow —
    # tighten to win it (1.0 = off; validate via markouts before lowering).
    favorite_leg_threshold: float = 0.65
    favorite_width_multiplier: float = 1.0
    # --- Impossible-combo farming (see pricing/quote.construct_farm_quote) ---
    # A logically-impossible combo (relationship.farmable) can only settle NO,
    # so the maker who shorts YES / is long the certain-NO side collects the
    # premium risk-free. We quote such combos instead of declining them.
    farm_impossible_combos: bool = True
    # Multiplier on the naive-independence YES value (the operator's chosen
    # anchor). 1.0 = quote the impossible YES at exactly its independence price.
    farm_markup: float = 1.0
    # Conservative per-combo size cap (whole contracts). Farming a TRUE
    # impossibility is riskless, but the only loss path is a misclassification,
    # so we cap size well below max_contracts_per_quote (100) as defense in
    # depth until live settlements confirm the classifier on farmed combos.
    farm_max_contracts: int = 50


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
    # First-half goal share h: goals_1H ~ Poisson(lam*h) per team, so a 1H leg
    # prices on the DC half-split scoreline and its correlations with FT (and
    # other 1H) legs are DERIVED coherently instead of hitting the flat +0.6
    # copula default. MEASURED 0.4507 on 8,981 football-data.co.uk HT/FT club
    # matches (top-5 EU 20/21-24/25; per-league 0.440 Serie A .. 0.461 Bundes),
    # matching the design's 0.45. A banded CONSTANT, never inverted from a single
    # 1H leg (design_halftime_dc.md §6); the ±band re-prices the joint so the
    # width absorbs the share uncertainty + the omitted inter-half serial corr.
    # Only exercised on combos that actually carry a 1H leg.
    half_share: float = 0.45
    half_share_band: float = 0.03  # covers the 0.44-0.46 league spread (§7)
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
