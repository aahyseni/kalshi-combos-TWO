"""1H cross-type cluster (calibrated 2026-07-08): every reachable same-game
1H×FT / 1H×1H soccer pair the audit found on the +0.6 fallback now resolves to
its typed value — oriented :same/:opp/:tie where a team is named, plain scalar
otherwise. This asserts each resolves on real KXWC tickers (not the fallback,
not the wrong orientation), which is the wiring safety net for the 36 entries +
the 5 new dispatch branches / 2 new resolvers in build_sgp_correlation.
"""

from __future__ import annotations

import pytest

from combomaker.ops.config import CorrelationConfig
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg

_G = "26JUL09FRAMAR"
_FRA = f"KXWCGOAL-{_G}-FRAKMBAPP10-1"   # a France scorer
_MAR = f"KXWCGOAL-{_G}-MARYAZIRI9-1"    # a Morocco scorer
_FHM = f"KXWC1H-{_G}-FRA"               # France leads at half
_FHT = f"KXWC1H-{_G}-TIE"               # 1H draw


def _params() -> SgpParams:
    c = CorrelationConfig()
    return SgpParams(
        pair_rho=dict(c.pair_rho), default_rho=c.same_event_rho, cross_event_rho=c.cross_event_rho,
        typed_uncertainty=c.typed_rho_uncertainty, untyped_uncertainty=c.untyped_rho_uncertainty,
        pair_uncertainty=dict(c.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in c.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in c.oriented_curve.items()},
        oriented_curve_uncertainty=dict(c.oriented_curve_uncertainty),
    )


def _leg(mt: str) -> RfqLeg:
    return RfqLeg(market_ticker=mt, event_ticker=f"KX-{_G}", side="yes", yes_settlement_value_cc=None)


def _rho(a: str, b: str) -> float:
    corr = build_sgp_correlation(
        [_leg(a), _leg(b)], [(0, 1)], _params(), marginals=[0.5, 0.5]
    )
    return float(corr.corr[0][1])


# (leg_a, leg_b, expected rho) — id built from the two series prefixes
_CASES = [
    # 1H-winner (first_half_moneyline) × FT — oriented
    (f"KXWCADVANCE-{_G}-FRA", _FHM, 0.64), (f"KXWCADVANCE-{_G}-FRA", f"KXWC1H-{_G}-MAR", -0.64),
    (f"KXWCADVANCE-{_G}-FRA", _FHT, 0.00),
    (_FHM, f"KXWCTOTAL-{_G}-3", 0.24), (_FHT, f"KXWCTOTAL-{_G}-3", -0.42),
    (_FHM, _FRA, 0.45), (_FHM, _MAR, -0.20), (_FHT, _FRA, -0.22),
    (f"KXWCBTTS-{_G}-BTTS", _FHM, 0.10), (f"KXWCBTTS-{_G}-BTTS", _FHT, -0.17),
    (_FHM, f"KXWCSPREAD-{_G}-FRA2", 0.70), (_FHM, f"KXWCSPREAD-{_G}-MAR2", -0.63),
    (_FHT, f"KXWCSPREAD-{_G}-FRA2", -0.32),
    # 1H-total / 1H-btts × FT — plain scalars
    (f"KXWCADVANCE-{_G}-FRA", f"KXWC1HTOTAL-{_G}-1", 0.09),
    (f"KXWC1HTOTAL-{_G}-1", f"KXWCGAME-{_G}-FRA", 0.14),
    (f"KXWC1HTOTAL-{_G}-1", f"KXWCSPREAD-{_G}-FRA2", 0.27),
    (f"KXWC1HTOTAL-{_G}-1", _FRA, 0.33),
    (f"KXWCADVANCE-{_G}-FRA", f"KXWC1HBTTS-{_G}-BTTS", -0.03),
    (f"KXWC1HBTTS-{_G}-BTTS", f"KXWCTOTAL-{_G}-3", 0.65),
    (f"KXWC1HBTTS-{_G}-BTTS", f"KXWCGAME-{_G}-FRA", -0.03),
    (f"KXWC1HBTTS-{_G}-BTTS", f"KXWCSPREAD-{_G}-FRA2", -0.08),
    (f"KXWC1HBTTS-{_G}-BTTS", _FRA, 0.33),
    # 1H×1H + 1H-spread × FT
    (f"KXWC1HSPREAD-{_G}-FRA2", f"KXWC1HTOTAL-{_G}-1", 0.95),
    (f"KXWC1HBTTS-{_G}-BTTS", _FHM, -0.18), (f"KXWC1HBTTS-{_G}-BTTS", _FHT, 0.30),
    (_FHM, f"KXWC1HSPREAD-{_G}-FRA2", 0.95), (_FHM, f"KXWC1HSPREAD-{_G}-MAR2", -0.95),
    (_FHT, f"KXWC1HSPREAD-{_G}-FRA2", -0.95),
    (f"KXWC1HBTTS-{_G}-BTTS", f"KXWC1HSPREAD-{_G}-FRA2", -0.22),
    (f"KXWC1HBTTS-{_G}-BTTS", f"KXWC1HTOTAL-{_G}-1", 0.95),
    (f"KXWCADVANCE-{_G}-FRA", f"KXWC1HSPREAD-{_G}-FRA2", 0.72),
    (f"KXWCADVANCE-{_G}-FRA", f"KXWC1HSPREAD-{_G}-MAR2", -0.72),
    (f"KXWCBTTS-{_G}-BTTS", f"KXWC1HSPREAD-{_G}-FRA2", 0.00),
    (f"KXWC1HSPREAD-{_G}-FRA2", _FRA, 0.45), (f"KXWC1HSPREAD-{_G}-FRA2", _MAR, -0.22),
    # FT advance × regulation draw
    (f"KXWCADVANCE-{_G}-FRA", f"KXWCGAME-{_G}-TIE", 0.00),
]


@pytest.mark.parametrize(("a", "b", "expected"), _CASES,
                         ids=[f"{a.split('-')[0]}x{b.split('-')[0]}={e}" for a, b, e in _CASES])
def test_1h_cluster_pair_resolves_to_config(a: str, b: str, expected: float) -> None:
    assert abs(_rho(a, b) - expected) < 1e-9


def test_leg_order_symmetric() -> None:
    for a, b, _ in _CASES:
        assert abs(_rho(a, b) - _rho(b, a)) < 1e-9, f"{a} | {b}"
