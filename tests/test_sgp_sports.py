"""Per-sport SGP correlation lookup: the same pair must resolve differently
per sport, with sport-prefixed uncertainty bands — and orientation-aware
(favorite/dog) priors for moneyline-involving pairs."""

from __future__ import annotations

import pytest

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.legtypes import Sport, classify_sport
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

PARAMS = SgpParams(
    pair_rho={"moneyline|total": 0.23},  # global fallback (soccer-flavored)
    default_rho=0.6,
    cross_event_rho=0.0,
    typed_uncertainty=0.15,
    untyped_uncertainty=0.30,
    pair_uncertainty={"nba:moneyline|total": 0.05},
    pair_rho_by_sport={"nba": {"moneyline|total": 0.0}},
)


def legs_for(prefix: str) -> tuple[RfqLeg, RfqLeg]:
    return (
        RfqLeg(f"{prefix}GAME-26JUL10AB-A", "E1", "yes", None),
        RfqLeg(f"{prefix}TOTAL-26JUL10AB-200", "E1", "yes", None),
    )


class TestSportClassifier:
    def test_real_prefixes(self) -> None:
        assert classify_sport("KXNBAGAME-26JUL10LALBOS-LAL") is Sport.NBA
        assert classify_sport("KXWNBAGAME-26JUL07CHIIND-CHI") is Sport.WNBA  # WNBA≠NBA
        assert classify_sport("KXMLBGAME-26JUL081840NYYTB-NYY") is Sport.MLB
        assert classify_sport("KXUFCFIGHT-26JUL11MCGHOL-HOL") is Sport.UFC
        assert classify_sport("KXWCGOAL-26JUL05MEXENG-ENGHKANE9-1") is Sport.SOCCER
        assert classify_sport("KXUCLGAME-26JUL07KAZDRI-KAZ") is Sport.SOCCER
        assert classify_sport("KXBRASILEIROBGAME-26JUL05NAUJUV-TIE") is Sport.SOCCER
        assert classify_sport("KXSOMETHING-26JUL10-X") is Sport.UNKNOWN


class TestSportSpecificRho:
    def test_nba_pair_uses_sport_table_not_global(self) -> None:
        result = build_sgp_correlation(list(legs_for("KXNBA")), [(0, 1)], PARAMS)
        assert result.corr[0, 1] == 0.0                      # NBA: uncorrelated
        assert abs(result.corr_high[0, 1] - 0.05) < 1e-9     # nba: band ±0.05
        assert any("nba:moneyline|total" in note for note in result.notes)

    def test_unknown_sport_falls_back_to_global(self) -> None:
        result = build_sgp_correlation(list(legs_for("KXMYST")), [(0, 1)], PARAMS)
        assert abs(result.corr[0, 1] - 0.23) < 1e-9          # global soccer-ish value
        assert abs(result.corr_high[0, 1] - 0.38) < 1e-9     # default typed band 0.15

    def test_cross_event_ignores_sport_tables(self) -> None:
        legs = legs_for("KXNBA")
        result = build_sgp_correlation(list(legs), [], PARAMS)  # no same-event group
        assert result.corr[0, 1] == 0.0  # cross_event_rho, not a lookup
        assert result.typed_pairs == 0


# btts|moneyline flips with orientation: "winners keep clean sheets" is a
# favorites effect; a dog only wins by scoring (scoreline-model implied +0.04
# on the live SPA/POR validation, 2026-07-06).
ORIENT_PARAMS = SgpParams(
    pair_rho={"btts|moneyline": -0.19},
    default_rho=0.6,
    cross_event_rho=0.0,
    typed_uncertainty=0.15,
    untyped_uncertainty=0.30,
    pair_uncertainty={
        "soccer:btts|moneyline:fav": 0.10,
        "soccer:btts|moneyline:dog": 0.08,
    },
    pair_rho_by_sport={
        "soccer": {
            "btts|moneyline": -0.19,
            "btts|moneyline:fav": -0.19,
            "btts|moneyline:dog": 0.00,
        }
    },
)

ML_FIRST = [
    RfqLeg("KXWCGAME-26JUL10AB-A", "E1", "yes", None),
    RfqLeg("KXWCBTTS-26JUL10AB", "E1", "yes", None),
]


class TestOrientationAwarePairs:
    def test_dog_moneyline_uses_dog_prior(self) -> None:
        result = build_sgp_correlation(ML_FIRST, [(0, 1)], ORIENT_PARAMS, marginals=[0.24, 0.60])
        assert result.corr[0, 1] == 0.0
        # band = max(fav 0.10, dog 0.08); high matrix may be PSD-repaired but
        # a 2x2 with |rho|<=1 already is.
        assert abs(result.corr_high[0, 1] - 0.10) < 1e-9

    def test_favorite_moneyline_uses_fav_prior(self) -> None:
        result = build_sgp_correlation(ML_FIRST, [(0, 1)], ORIENT_PARAMS, marginals=[0.65, 0.60])
        assert abs(result.corr[0, 1] - (-0.19)) < 1e-9

    def test_coinflip_blends_linearly(self) -> None:
        result = build_sgp_correlation(ML_FIRST, [(0, 1)], ORIENT_PARAMS, marginals=[0.50, 0.60])
        assert abs(result.corr[0, 1] - (-0.095)) < 1e-9  # halfway dog->fav

    def test_orientation_keyed_to_moneyline_leg_regardless_of_order(self) -> None:
        legs = list(reversed(ML_FIRST))  # btts first, moneyline second
        result = build_sgp_correlation(legs, [(0, 1)], ORIENT_PARAMS, marginals=[0.60, 0.24])
        assert result.corr[0, 1] == 0.0  # ML leg (index 1) is the dog

    def test_no_marginals_falls_back_to_plain_entry(self) -> None:
        result = build_sgp_correlation(ML_FIRST, [(0, 1)], ORIENT_PARAMS)
        assert abs(result.corr[0, 1] - (-0.19)) < 1e-9

    def test_marginals_without_oriented_entries_use_plain_path(self) -> None:
        # PARAMS has no ":fav"/":dog" keys — marginals must be a no-op.
        result = build_sgp_correlation(
            list(legs_for("KXNBA")), [(0, 1)], PARAMS, marginals=[0.24, 0.6]
        )
        assert result.corr[0, 1] == 0.0  # nba sport-table value, unoriented


class TestDefaultConfigOrientation:
    """The shipped CorrelationConfig carries the oriented soccer entries."""

    @pytest.fixture()
    def cfg(self) -> CorrelationConfig:
        return CorrelationConfig()

    def test_btts_moneyline_orientation_entries(self, cfg: CorrelationConfig) -> None:
        soccer = cfg.pair_rho_by_sport["soccer"]
        assert soccer["btts|moneyline:fav"] == -0.19
        assert soccer["btts|moneyline:dog"] == 0.00
        assert cfg.pair_rho_uncertainty["soccer:btts|moneyline:dog"] > 0

    def test_moneyline_player_goal_raised(self, cfg: CorrelationConfig) -> None:
        # Structurally implied ~+0.51 in both worked examples; 0.25 hand prior
        # made us auto-lose striker SGP auctions.
        assert cfg.pair_rho_by_sport["soccer"]["moneyline|player_goal"] == 0.50
        assert cfg.pair_rho["moneyline|player_goal"] == 0.40  # non-soccer fallback
        assert cfg.pair_rho_uncertainty["soccer:moneyline|player_goal"] == 0.12


def shipped_params() -> SgpParams:
    """SgpParams built from the SHIPPED CorrelationConfig, mirroring the
    engine's own construction (pricing/engine.py) — the integration seam."""
    cfg = CorrelationConfig()
    return SgpParams(
        pair_rho=dict(cfg.pair_rho),
        default_rho=cfg.same_event_rho,
        cross_event_rho=cfg.cross_event_rho,
        typed_uncertainty=cfg.typed_rho_uncertainty,
        untyped_uncertainty=cfg.untyped_rho_uncertainty,
        pair_uncertainty=dict(cfg.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in cfg.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in cfg.oriented_curve.items()},
        oriented_curve_uncertainty=dict(cfg.oriented_curve_uncertainty),
    )


class TestMlbPropPairsShippedConfig:
    """MLB props tranche end to end (2026-07-09): real tickers through the real
    classifier + the SHIPPED config. The regression guarded against is the old
    path — KXMLBKS/HIT/HR typed UNKNOWN, so every same-game prop pair fell to
    the flat +0.6/0.90 fallback (sign-wrong for KS × total, measured −0.25)."""

    def test_ks_total_same_game_resolves_measured_negative(self) -> None:
        legs = [
            RfqLeg("KXMLBKS-26JUL091110NYYBOS-BOSGCROCHET45-7", "E1", "yes", None),
            RfqLeg("KXMLBTOTAL-26JUL091110NYYBOS-9", "E1", "yes", None),
        ]
        result = build_sgp_correlation(legs, [(0, 1)], shipped_params())
        assert abs(result.corr[0, 1] - (-0.25)) < 1e-9  # NOT the 0.6 flat prior
        assert result.typed_pairs == 1
        assert result.untyped_pairs == 0
        # band = mlb:player_ks|total 0.12, not the 0.90 fallback width
        assert abs(result.corr_low[0, 1] - (-0.37)) < 1e-9
        assert abs(result.corr_high[0, 1] - (-0.13)) < 1e-9
        assert any("mlb:player_ks|total" in note for note in result.notes)

    def test_hit_hr_same_game_resolves_judge_value(self) -> None:
        legs = [
            RfqLeg("KXMLBHIT-26JUL091110NYYBOS-NYYAJUDGE99-2", "E1", "yes", None),
            RfqLeg("KXMLBHR-26JUL091110NYYBOS-BOSRDEVERS11-1", "E1", "yes", None),
        ]
        result = build_sgp_correlation(legs, [(0, 1)], shipped_params())
        # Phase 2 wire (2026-07-10): NYY batter × BOS batter is the OPPONENT
        # case and hit|hr now routes :same/:opp (final-pairs judge,
        # phase2_wire_list.txt lines 95-96) — :opp 0.00 band 0.04 replaces the
        # plain 0.01/0.06 this test asserted pre-wire (plain stays as the
        # fail-closed parse fallback).
        assert abs(result.corr[0, 1] - 0.00) < 1e-9  # wired [D] :opp split
        assert abs(result.corr_high[0, 1] - 0.04) < 1e-9  # band mlb:…:opp = 0.04
        assert result.typed_pairs == 1
        assert any("mlb:player_hit|player_hr" in note for note in result.notes)
