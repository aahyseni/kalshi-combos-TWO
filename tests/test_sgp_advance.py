"""ADVANCE × full-time correlation priors (DC-derived + 4-study cross-check).

advance was UNLISTED against total/btts/scorer/spread → the flat +0.6 fallback
(6x too high on totals, WRONG SIGN on btts and opponent-scorer). These pin the
DC-derived values and the :same/:opp orientation for advance × scorer.
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


def test_advance_total_is_small_positive_not_the_06_fallback() -> None:
    r = _rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCTOTAL-{_G}-3")
    assert abs(r - 0.12) < 1e-9  # was +0.60


def test_advance_total_is_line_stable_including_over_05() -> None:
    # advance does NOT imply a goal (0-0 shootout advance), so over-0.5 is NOT a
    # containment for advance (unlike moneyline) — same modest +0.12.
    assert abs(_rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCTOTAL-{_G}-1") - 0.12) < 1e-9
    assert abs(_rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCTOTAL-{_G}-4") - 0.12) < 1e-9


def test_advance_btts_is_negative_not_positive_fallback() -> None:
    r = _rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCBTTS-{_G}-BTTS")
    assert r < 0.0  # the +0.6 fallback had the WRONG sign
    assert abs(r - (-0.07)) < 1e-9


def test_advance_scorer_same_team_positive() -> None:
    # scorer plays for the advancing team → his goals push them through.
    # 0.45 → 0.52 promoted 2026-07-19 (operator-approved rule-8b measurement:
    # advance AND scorer props both settle incl ET, so the regulation
    # ml|player_goal attenuation does not apply; DC-identified conditional
    # ~54% — docs/reports/2026-07-19-argmessi-fair-vs-field.md).
    r = _rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCGOAL-{_G}-FRAKMBAPP10-1")
    assert abs(r - 0.52) < 1e-9


def test_advance_scorer_opponent_flips_sign() -> None:
    # scorer plays for the OPPONENT → negative (was +0.6, wrong sign, a pick-off)
    r = _rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCGOAL-{_G}-MARYAZIRI9-1")
    assert abs(r - (-0.45)) < 1e-9


def test_advance_scorer_orientation_is_symmetric_in_leg_order() -> None:
    a = _rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCGOAL-{_G}-FRAKMBAPP10-1")
    b = _rho(f"KXWCGOAL-{_G}-FRAKMBAPP10-1", f"KXWCADVANCE-{_G}-FRA")
    assert abs(a - b) < 1e-9


def test_advance_spread_is_near_containment() -> None:
    # spread>=2 => win => advance (Kalshi blocks the conflict, but pin it high)
    assert _rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCSPREAD-{_G}-FRA2") >= 0.9


def test_advance_corners_strength_curve() -> None:
    # M2 zero-gaps wire (2026-07-12): advance × total corners is a STRENGTH
    # CURVE on the ADVANCE leg's marginal (dog +0.23 <-> fav -0.23; drawn-90
    # forces ET and corners settle incl ET) — near-zero at a coin-flip
    # advance (antisymmetric knots straddle 0.5), signed at the ends.
    assert abs(_rho(f"KXWCADVANCE-{_G}-FRA", f"KXWCCORNERS-{_G}-8")) < 1e-3
    out_dog = build_sgp_correlation(
        (_leg(f"KXWCADVANCE-{_G}-FRA"), _leg(f"KXWCCORNERS-{_G}-8")),
        [(0, 1)],
        _sp(),
        marginals=[0.2823, 0.5],
    )
    assert abs(out_dog.corr[0, 1] - 0.2336) < 1e-9
    out_fav = build_sgp_correlation(
        (_leg(f"KXWCADVANCE-{_G}-FRA"), _leg(f"KXWCCORNERS-{_G}-8")),
        [(0, 1)],
        _sp(),
        marginals=[0.7177, 0.5],
    )
    assert abs(out_fav.corr[0, 1] - (-0.2336)) < 1e-9
    # No marginals -> the plain 0.00 scalar (band 0.25 spans the curve).
    out_plain = build_sgp_correlation(
        (_leg(f"KXWCADVANCE-{_G}-FRA"), _leg(f"KXWCCORNERS-{_G}-8")),
        [(0, 1)],
        _sp(),
    )
    assert abs(out_plain.corr[0, 1]) < 1e-9
    assert abs(out_plain.corr_high[0, 1] - 0.25) < 1e-9


def test_config_carries_advance_pairs_and_bands() -> None:
    soc = CorrelationConfig().pair_rho_by_sport["soccer"]
    for key in (
        "advance|total",
        "advance|btts",
        "advance|player_goal:same",
        "advance|player_goal:opp",
        "advance|spread",
    ):
        assert key in soc, key
    unc = CorrelationConfig().pair_rho_uncertainty
    assert unc["soccer:advance|player_goal:same"] > 0
