"""TOTAL corners x TEAM corners (same game) correlation prior.

`corners|corners_team` was UNLISTED → the flat +0.6 same-event fallback with a
zero-spanning fail-safe band (treating a structurally-certain-positive pair as
maybe-negative → over-wide quotes on corners-heavy combos, RFQ test C24/25/26).
Total corners CONTAIN a team's corners, so the pair is strongly comonotone;
MEASURED +0.62 (two independent passes, 8,981 matches). These pin the typed
value + tight band. Mirrors tests/test_sgp_spread_pairs.py.
"""

from __future__ import annotations

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpCorrelation, SgpParams, build_sgp_correlation
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


def _corr(a: str, b: str) -> SgpCorrelation:
    legs = [_leg(a), _leg(b)]
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    return build_sgp_correlation(legs, rel.same_event_groups, _sp(), marginals=[0.5, 0.5])


def test_corners_total_team_typed_positive() -> None:
    c = _corr(f"KXWCCORNERS-{_G}-10", f"KXWCTCORNERS-{_G}-ESP6")
    assert abs(float(c.corr[0][1]) - 0.62) < 1e-9
    assert c.typed_pairs == 1 and c.untyped_pairs == 0  # typed, not the +0.6 fallback


def test_corners_total_team_band_is_tight_not_zero_spanning() -> None:
    # the old untyped fallback band spans zero (corr_low reaches ~-0.30); a typed
    # +/-0.15 band keeps the low leg firmly positive.
    c = _corr(f"KXWCCORNERS-{_G}-10", f"KXWCTCORNERS-{_G}-ESP6")
    assert float(c.corr_low[0][1]) > 0.4
    assert float(c.corr_high[0][1]) > float(c.corr[0][1])


def test_corners_total_team_cross_game_is_independent() -> None:
    c = _corr("KXWCCORNERS-26JUL06PORESP-10", "KXWCTCORNERS-26JUL06USABEL-USA5")
    assert abs(float(c.corr[0][1])) < 1e-9  # different games -> cross_event_rho 0


def test_config_carries_corners_total_team_pair() -> None:
    soc = CorrelationConfig().pair_rho_by_sport["soccer"]
    assert "corners|corners_team" in soc
    assert abs(soc["corners|corners_team"] - 0.62) < 1e-9
