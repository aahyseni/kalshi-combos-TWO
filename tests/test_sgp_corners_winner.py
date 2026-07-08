"""team-corners × match-winner orientation (:same / :opp / :tie).

`corners_team|moneyline` shipped a single −0.15 with no orientation, so a
"Team-A corners × Team-B WINS" combo got the wrong sign (should be ~+0.15). A
team's corners are −0.15 with THAT team winning (chasing team earns corners),
+0.15 with the OPPONENT winning, ~0 with a draw — STRENGTH-CONTROLLED (raw
pooled corr is a Simpson trap, wrong sign). Mirrors tests/test_sgp_spread_pairs.py.
"""

from __future__ import annotations

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

_G = "26JUL06PORESP"


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


def _leg(mt: str) -> RfqLeg:
    return RfqLeg(market_ticker=mt, event_ticker=_ev(mt), side="yes", yes_settlement_value_cc=None)


def _rho(a: str, b: str) -> float:
    legs = [_leg(a), _leg(b)]
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    corr = build_sgp_correlation(legs, rel.same_event_groups, _sp(), marginals=[0.5, 0.5])
    return float(corr.corr[0][1])


def test_team_corners_same_team_wins_is_negative() -> None:
    # POR's corners × POR wins → chasing-team effect, −0.15 (NOT the +0.6 fallback)
    assert abs(_rho(f"KXWCTCORNERS-{_G}-POR6", f"KXWCGAME-{_G}-POR") - (-0.15)) < 1e-9


def test_team_corners_opponent_wins_flips_positive() -> None:
    # POR's corners × ESP (opponent) wins → +0.15, the mirror
    assert abs(_rho(f"KXWCTCORNERS-{_G}-POR6", f"KXWCGAME-{_G}-ESP") - 0.15) < 1e-9


def test_team_corners_draw_is_zero() -> None:
    assert abs(_rho(f"KXWCTCORNERS-{_G}-POR6", f"KXWCGAME-{_G}-TIE") - 0.00) < 1e-9


def test_orientation_symmetric_in_leg_order() -> None:
    a = _rho(f"KXWCTCORNERS-{_G}-POR6", f"KXWCGAME-{_G}-ESP")
    b = _rho(f"KXWCGAME-{_G}-ESP", f"KXWCTCORNERS-{_G}-POR6")
    assert abs(a - b) < 1e-9


def test_config_carries_oriented_entries() -> None:
    soc = CorrelationConfig().pair_rho_by_sport["soccer"]
    for k in ("corners_team|moneyline:same", "corners_team|moneyline:opp",
              "corners_team|moneyline:tie"):
        assert k in soc, k
