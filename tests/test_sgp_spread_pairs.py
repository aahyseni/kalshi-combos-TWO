"""SPREAD × full-time correlation priors (DC-derived).

spread (win-by-margin) was UNLISTED against total/btts/scorer → the flat +0.6
fallback. These pin the DC-derived values and the :same/:opp orientation for
spread × scorer. Mirrors tests/test_sgp_advance.py.
"""

from __future__ import annotations

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

_G = "26JUL09FRAMAR"


def _sp() -> SgpParams:
    c = CorrelationConfig()
    return SgpParams(
        pair_rho=dict(c.pair_rho),
        default_rho=c.same_event_rho,
        cross_event_rho=c.cross_event_rho,
        typed_uncertainty=c.typed_rho_uncertainty,
        untyped_uncertainty=c.untyped_rho_uncertainty,
        pair_uncertainty=dict(c.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in c.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in c.oriented_curve.items()},
        oriented_curve_uncertainty=dict(c.oriented_curve_uncertainty),
    )


class _Prov:
    def event_mutually_exclusive(self, e: str) -> bool:
        return False


def _ev(mt: str) -> str:
    return "-".join(mt.split("-")[:2])


def _leg(mt: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(market_ticker=mt, event_ticker=_ev(mt), side=side, yes_settlement_value_cc=None)


def _rho(a: str, b: str) -> float:
    legs = [_leg(a), _leg(b)]
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    corr = build_sgp_correlation(legs, rel.same_event_groups, _sp(), marginals=[0.5, 0.5])
    return float(corr.corr[0][1])


def test_spread_total_is_positive_not_the_06_fallback() -> None:
    assert abs(_rho(f"KXWCSPREAD-{_G}-FRA2", f"KXWCTOTAL-{_G}-3") - 0.31) < 1e-9


def test_spread_btts_is_negative_not_positive_fallback() -> None:
    r = _rho(f"KXWCSPREAD-{_G}-FRA2", f"KXWCBTTS-{_G}-BTTS")
    assert r < 0.0  # the +0.6 fallback had the WRONG sign
    assert abs(r - (-0.30)) < 1e-9


def test_spread_scorer_same_team_positive() -> None:
    r = _rho(f"KXWCSPREAD-{_G}-FRA2", f"KXWCGOAL-{_G}-FRAKMBAPP10-1")
    assert abs(r - 0.46) < 1e-9


def test_spread_scorer_opponent_flips_sign() -> None:
    r = _rho(f"KXWCSPREAD-{_G}-FRA2", f"KXWCGOAL-{_G}-MARYAZIRI9-1")
    assert abs(r - (-0.42)) < 1e-9


def test_spread_scorer_orientation_symmetric_in_leg_order() -> None:
    a = _rho(f"KXWCSPREAD-{_G}-FRA2", f"KXWCGOAL-{_G}-FRAKMBAPP10-1")
    b = _rho(f"KXWCGOAL-{_G}-FRAKMBAPP10-1", f"KXWCSPREAD-{_G}-FRA2")
    assert abs(a - b) < 1e-9


def test_full_game_spread_spread_and_1h_spread_unchanged() -> None:
    # regression: the new SPREAD×PLAYER_GOAL branch must not disturb the
    # first_half_spread resolvers or full-game spread|spread.
    r_1h = _rho(f"KXWC1HSPREAD-{_G}-FRA2", f"KXWCSPREAD-{_G}-FRA2")  # :same
    assert abs(r_1h - 0.78) < 1e-9


def test_config_carries_spread_pairs() -> None:
    soc = CorrelationConfig().pair_rho_by_sport["soccer"]
    for key in ("spread|total", "btts|spread", "player_goal|spread:same", "player_goal|spread:opp"):
        assert key in soc, key
