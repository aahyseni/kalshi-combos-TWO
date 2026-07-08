"""FT-BTTS × 1H-TOTAL correlation prior (structural + empirical, reconciled).

The pair ``btts|first_half_total`` was UNLISTED, so a same-game FT-BTTS × 1H-total
combo fell to the flat +0.6 same-event fallback (live-RFQ combos C22/C27/C28
mispriced). DERIVED two ways on 8,981 club matches
(tools/calibrate_soccer_btts_1h_total.py): shipped half-time Dixon-Coles +0.53/
+0.54 and football-data empirical +0.57/+0.55 — they agree, so the pair ships at
+0.55 (line-stable), NOT the fallback.
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
    return RfqLeg(
        market_ticker=mt, event_ticker=_ev(mt), side=side, yes_settlement_value_cc=None
    )


def _corr(a: str, b: str) -> tuple[float, float, float, int, int]:
    legs = [_leg(a), _leg(b)]
    rel = classify_legs(legs, _Prov())
    assert rel.kind is RelationshipKind.OK
    c = build_sgp_correlation(legs, rel.same_event_groups, _sp(), marginals=[0.5, 0.5])
    return (
        float(c.corr[0][1]),
        float(c.corr_low[0][1]),
        float(c.corr_high[0][1]),
        c.typed_pairs,
        c.untyped_pairs,
    )


def test_btts_1h_total_resolves_to_typed_prior_not_06_fallback() -> None:
    rho, _lo, _hi, typed, untyped = _corr(
        f"KXWCBTTS-{_G}-BTTS", f"KXWC1HTOTAL-{_G}-1"
    )
    assert abs(rho - 0.55) < 1e-9  # was the +0.60 flat same-event fallback
    assert typed == 1 and untyped == 0  # a TYPED pair now, not the untyped default


def test_btts_1h_total_is_line_stable() -> None:
    # Structural + empirical both show N=1 (over0.5) and N=2 (over1.5) within
    # ~0.02 -> a single line-stable entry, no line-specific key.
    r1 = _corr(f"KXWCBTTS-{_G}-BTTS", f"KXWC1HTOTAL-{_G}-1")[0]
    r2 = _corr(f"KXWCBTTS-{_G}-BTTS", f"KXWC1HTOTAL-{_G}-2")[0]
    assert abs(r1 - 0.55) < 1e-9
    assert abs(r2 - 0.55) < 1e-9


def test_btts_1h_total_leg_order_symmetric() -> None:
    a = _corr(f"KXWCBTTS-{_G}-BTTS", f"KXWC1HTOTAL-{_G}-1")[0]
    b = _corr(f"KXWC1HTOTAL-{_G}-1", f"KXWCBTTS-{_G}-BTTS")[0]
    assert abs(a - b) < 1e-9


def test_btts_1h_total_band_spans_both_methods() -> None:
    # band 0.13 -> [0.42, 0.68], covering structural (+0.53/+0.54), empirical
    # (+0.57/+0.55) and the empirical 99% CI up to +0.65.
    rho, lo, hi, _t, _u = _corr(f"KXWCBTTS-{_G}-BTTS", f"KXWC1HTOTAL-{_G}-1")
    assert lo < 0.53 and hi > 0.57
    assert abs(rho - lo - 0.13) < 1e-9
    assert abs(hi - rho - 0.13) < 1e-9


def test_config_carries_btts_1h_total_pair_and_band() -> None:
    c = CorrelationConfig()
    assert c.pair_rho_by_sport["soccer"]["btts|first_half_total"] == 0.55
    assert c.pair_rho_uncertainty["soccer:btts|first_half_total"] == 0.13
    # coherence: below the shipped 1H-total×FT-total and BTTS×FT-total anchors.
    assert (
        c.pair_rho_by_sport["soccer"]["btts|first_half_total"]
        < c.pair_rho_by_sport["soccer"]["first_half_total|total"]
    )
    assert (
        c.pair_rho_by_sport["soccer"]["btts|first_half_total"]
        < c.pair_rho_by_sport["soccer"]["btts|total"]
    )
