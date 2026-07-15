"""P1.8 — analytic_leg_deltas labelled as independence proxies; structural
scenario sensitivities used where available (RISK_ENGINE_AUDIT_ACTION_PLAN.txt).

Covers:
  * the DeltaProvenance labelling + the leg_deltas_labeled dispatcher,
  * structural_leg_deltas failing CLOSED to the proxy (None) on anything not a
    single fully-representable structurally-modelled game (cross-game,
    non-parseable ticker, missing marginal, reserved holding),
  * the structural sensitivity RECOGNISING a same-game hedge the independence
    proxy assumes away: two OPPOSING advance legs (ENG-advance + ARG-advance)
    cannot both settle YES (exactly one team advances), so the long-NO
    co-satisfaction mass — and thus the structural deltas — collapse far below
    the independence product the proxy uses.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest

from combomaker.core.conventions import Side
from combomaker.core.money import CentiCents
from combomaker.core.quantity import CentiContracts
from combomaker.risk.exposure import (
    DeltaProvenance,
    LegRef,
    OpenPosition,
    analytic_leg_deltas,
    leg_deltas_labeled,
    structural_leg_deltas,
)

CC = CentiCents
Q = CentiContracts

# A real World-Cup knockout game (ENG vs ARG) — the game the structural API
# tests use. Two OPPOSING advance legs share one event/game.
_GAME = "26JUL15ENGARG"
_ADV_EV = f"KXWCADVANCE-{_GAME}"
_ADV_ENG = f"KXWCADVANCE-{_GAME}-ENG"   # Advance(Team.A)
_ADV_ARG = f"KXWCADVANCE-{_GAME}-ARG"   # Advance(Team.B)
# A second, unrelated game (for the cross-game fail-closed case).
_OTHER_GAME = "26JUL16FRABRA"
_ADV_FRA = f"KXWCADVANCE-{_OTHER_GAME}-FRA"


def provider(mapping: dict[str, float]) -> Callable[[str], float | None]:
    return lambda ticker: mapping.get(ticker)


def make_position(
    pid: str,
    legs: tuple[LegRef, ...],
    *,
    our_side: Side = Side.NO,
    contracts: int = 100,
    risk_modeled: bool = True,
) -> OpenPosition:
    return OpenPosition(
        position_id=pid,
        combo_ticker="COMBO-X",
        collection=None,
        our_side=our_side,
        contracts=Q(contracts),
        entry_price_cc=CC(5_000),
        legs=legs,
        risk_modeled=risk_modeled,
    )


_BTTS_EV = f"KXWCBTTS-{_GAME}"
_BTTS = f"KXWCBTTS-{_GAME}-BTTS"

_OPPOSING_ADVANCE = (
    LegRef(_ADV_ENG, _ADV_EV, "yes"),
    LegRef(_ADV_ARG, _ADV_EV, "yes"),
)
# Two advance marginals that (as opposing outcomes) roughly complement.
_ADV_MARGINALS = {_ADV_ENG: 0.55, _ADV_ARG: 0.45}

# A 3-leg book that also holds a BTTS leg — the extra leg makes the JOINT of the
# "other legs" (both advances) diverge from the product of their marginals, which
# is where structure separates from the independence proxy.
_THREE_LEG = (
    LegRef(_ADV_ENG, _ADV_EV, "yes"),
    LegRef(_ADV_ARG, _ADV_EV, "yes"),
    LegRef(_BTTS, _BTTS_EV, "yes"),
)
_THREE_MARGINALS = {_ADV_ENG: 0.55, _ADV_ARG: 0.45, _BTTS: 0.60}


class TestDeltaProvenanceLabelling:
    def test_proxy_dispatcher_labels_independence_when_not_structural(self) -> None:
        legs = (LegRef("AAA", "EV1", "yes"), LegRef("BBB", "EV1", "yes"))
        labeled = leg_deltas_labeled(
            make_position("p", legs, our_side=Side.YES),
            provider({"AAA": 0.5, "BBB": 0.6}),
        )
        assert labeled.provenance is DeltaProvenance.INDEPENDENCE_PROXY
        # falls back to exactly the proxy values
        assert labeled.deltas == pytest.approx({"AAA": 0.6, "BBB": 0.5})

    def test_missing_marginal_labels_proxy_with_none_deltas(self) -> None:
        legs = (LegRef("AAA", "EV1", "yes"), LegRef("BBB", "EV1", "yes"))
        labeled = leg_deltas_labeled(
            make_position("p", legs), provider({"AAA": 0.5})  # BBB missing
        )
        assert labeled.deltas is None
        assert labeled.provenance is DeltaProvenance.INDEPENDENCE_PROXY

    def test_structural_dispatch_labels_structural_when_available(self) -> None:
        labeled = leg_deltas_labeled(
            make_position("p", _OPPOSING_ADVANCE), provider(_ADV_MARGINALS)
        )
        assert labeled.provenance is DeltaProvenance.STRUCTURAL_SCENARIO
        assert labeled.deltas is not None

    def test_prefer_structural_false_forces_proxy(self) -> None:
        labeled = leg_deltas_labeled(
            make_position("p", _OPPOSING_ADVANCE), provider(_ADV_MARGINALS),
            prefer_structural=False,
        )
        assert labeled.provenance is DeltaProvenance.INDEPENDENCE_PROXY


class TestStructuralFailsClosedToProxy:
    def test_missing_marginal_returns_none(self) -> None:
        pos = make_position("p", _OPPOSING_ADVANCE)
        assert structural_leg_deltas(pos, provider({_ADV_ENG: 0.55})) is None

    def test_cross_game_returns_none(self) -> None:
        legs = (
            LegRef(_ADV_ENG, _ADV_EV, "yes"),
            LegRef(_ADV_FRA, f"KXWCADVANCE-{_OTHER_GAME}", "yes"),
        )
        pos = make_position("p", legs)
        got = structural_leg_deltas(
            pos, provider({_ADV_ENG: 0.55, _ADV_FRA: 0.60})
        )
        assert got is None

    def test_reserved_holding_returns_none(self) -> None:
        pos = make_position("p", _OPPOSING_ADVANCE, risk_modeled=False)
        assert structural_leg_deltas(pos, provider(_ADV_MARGINALS)) is None

    def test_unparseable_leg_returns_none(self) -> None:
        legs = (
            LegRef(_ADV_ENG, _ADV_EV, "yes"),
            LegRef(f"KXWCCORNERS-{_GAME}-OVER", _ADV_EV, "yes"),  # corners: copula-only
        )
        pos = make_position("p", legs)
        got = structural_leg_deltas(
            pos, provider({_ADV_ENG: 0.55, f"KXWCCORNERS-{_GAME}-OVER": 0.5})
        )
        assert got is None


class TestStructuralRecognisesHedge:
    def test_opposing_advances_collapse_vs_independence_proxy(self) -> None:
        """Long-NO on {ENG-adv, ARG-adv, BTTS}: the two advances are mutually
        exclusive, so the co-satisfaction mass of the OTHER legs for the BTTS delta
        — ``P(ENG adv AND ARG adv)`` — is ~0 structurally, vs the proxy's
        ``P(ENG)·P(ARG) ~= 0.25``. The independence proxy cannot see that hedge."""
        pos = make_position("p", _THREE_LEG, our_side=Side.NO)
        marg = provider(_THREE_MARGINALS)

        proxy = analytic_leg_deltas(pos, marg)
        structural = structural_leg_deltas(pos, marg)
        assert proxy is not None and structural is not None

        # Proxy delta to BTTS = contracts * P(ENG)·P(ARG) ~= 0.55·0.45 = 0.2475.
        assert abs(proxy[_BTTS]) == pytest.approx(0.2475, abs=0.02)

        # Structural: P(both teams advance) ~= 0 (exactly one advances), so the
        # co-satisfaction mass driving the BTTS delta collapses toward zero.
        assert abs(structural[_BTTS]) < abs(proxy[_BTTS]) / 3
        assert abs(structural[_BTTS]) < 0.03

    def test_structural_delta_sign_matches_proxy(self) -> None:
        """Refinement, not contradiction: a single-leg structural delta keeps the
        proxy's sign (long-NO on a YES leg is a negative delta)."""
        # Need >=2 team legs to invert; give a co-leg on the same game.
        two = (LegRef(_ADV_ENG, _ADV_EV, "yes"), LegRef(_ADV_ARG, _ADV_EV, "yes"))
        pos2 = make_position("p", two, our_side=Side.NO)
        structural = structural_leg_deltas(pos2, provider(_ADV_MARGINALS))
        assert structural is not None
        # long-NO on a YES-selected leg ⇒ negative directional delta.
        assert structural[_ADV_ENG] <= 0.0
        assert structural[_ADV_ARG] <= 0.0
