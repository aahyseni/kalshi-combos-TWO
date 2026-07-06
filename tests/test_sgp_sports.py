"""Per-sport SGP correlation lookup: the same pair must resolve differently
per sport, with sport-prefixed uncertainty bands."""

from __future__ import annotations

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
