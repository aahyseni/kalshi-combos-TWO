"""ESPARG champion-leg PRICING ALIAS (2026-07-16, operator-directed).

The WC final lists NO ``KXWCADVANCE`` series (tape-verified); the "wins the
World Cup" flow arrives on ``KXMENWORLDCUP-26-{AR,ES}``, which at finals time
is settlement-identical to advancing the final (win incl. ET + pens). The
config-driven exact-ticker alias makes the pricing layer reason about the
synthetic ``KXWCADVANCE-26JUL19ESPARG-{ARG,ESP}`` legs while the exchange
identity (books, marginals, quoting, settlement) keeps the real ticker.

Covered here: validation (only-UNKNOWN keys, modeled targets, no chains, event
consistency), classification, markup sport tagging (the observed-live ZERO-
markup bug), game grouping (the ONE pricer/risk seam), relationships (grouping
follows the alias; mutual-exclusion metadata stays on the REAL event),
structural parity (aliased champion == literal advance leg to the cent),
risk-side game plans, and the process-boundary installs (engine + book-risk
pool initializer).
"""

from __future__ import annotations

import pytest

from combomaker.ops.config import PricingConfig, StructuralConfig
from combomaker.ops.pricing_pool import _book_risk_pool_init
from combomaker.pricing.grouping import game_key
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.legtypes import (
    LegType,
    Sport,
    classify_leg,
    classify_sport,
    is_period_leg,
    resolve_pricing_alias,
    resolve_pricing_event_alias,
    set_pricing_aliases,
    validate_pricing_aliases,
)
from combomaker.pricing.markup import sport_of
from combomaker.pricing.relationships import (
    RelationshipKind,
    classify_legs,
)
from combomaker.pricing.structural import StructuralPricer
from combomaker.rfq.models import RfqLeg
from combomaker.sim.structural_book import StructuralConfigView, build_game_plans

GAME = "26JUL19ESPARG"
CHAMP_AR = "KXMENWORLDCUP-26-AR"
CHAMP_ES = "KXMENWORLDCUP-26-ES"
CHAMP_EVENT = "KXMENWORLDCUP-26"
SYN_ADV_ARG = f"KXWCADVANCE-{GAME}-ARG"
SYN_ADV_ESP = f"KXWCADVANCE-{GAME}-ESP"
SYN_EVENT = f"KXWCADVANCE-{GAME}"
BTTS = f"KXWCBTTS-{GAME}-BTTS"
BTTS_EVENT = f"KXWCBTTS-{GAME}"
TOTAL = f"KXWCTOTAL-{GAME}-3"
TOTAL_EVENT = f"KXWCTOTAL-{GAME}"

ALIASES = {CHAMP_AR: SYN_ADV_ARG, CHAMP_ES: SYN_ADV_ESP}


def leg(ticker: str, event: str | None, side: str = "yes") -> RfqLeg:
    return RfqLeg(ticker, event, side, None)


def belief(p: float, unc: float = 0.005) -> LegBelief:
    return LegBelief(p=p, uncertainty=unc, source="test")


class MappingProvider:
    def __init__(self, mapping: dict[str, bool]) -> None:
        self._m = mapping

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        return self._m.get(event_ticker)


# --- validation ---------------------------------------------------------------


def test_good_aliases_install_and_resolve() -> None:
    set_pricing_aliases(ALIASES)
    assert resolve_pricing_alias(CHAMP_AR) == SYN_ADV_ARG
    assert resolve_pricing_alias(CHAMP_ES) == SYN_ADV_ESP
    assert resolve_pricing_alias(BTTS) == BTTS  # identity when unaliased
    assert resolve_pricing_event_alias(CHAMP_EVENT) == SYN_EVENT
    assert resolve_pricing_event_alias(BTTS_EVENT) == BTTS_EVENT


def test_alias_key_must_classify_unknown() -> None:
    # An alias may never override a modeled family.
    with pytest.raises(ValueError, match="only|never override|classifies"):
        validate_pricing_aliases({BTTS: SYN_ADV_ARG})


def test_alias_target_must_be_modeled() -> None:
    with pytest.raises(ValueError, match="UNKNOWN"):
        validate_pricing_aliases({CHAMP_AR: "KXSOMENONSENSE-26-X"})


def test_alias_chain_and_self_alias_rejected() -> None:
    with pytest.raises(ValueError, match="chain"):
        validate_pricing_aliases(
            {CHAMP_AR: SYN_ADV_ARG, SYN_ADV_ARG: SYN_ADV_ESP}
        )
    with pytest.raises(ValueError, match="self-alias|empty"):
        validate_pricing_aliases({CHAMP_AR: CHAMP_AR})


def test_alias_needs_event_suffix_shape() -> None:
    with pytest.raises(ValueError, match="EVENT-SUFFIX"):
        validate_pricing_aliases({"KXMENWORLDCUP": SYN_ADV_ARG})


def test_alias_event_derivation_must_be_consistent() -> None:
    # Two keys of ONE real event must not map to two synthetic events.
    with pytest.raises(ValueError, match="multiple synthetic"):
        validate_pricing_aliases(
            {CHAMP_AR: SYN_ADV_ARG, CHAMP_ES: "KXWCADVANCE-26JUL18FRAENG-ESP"}
        )


def test_bad_alias_never_half_installs() -> None:
    set_pricing_aliases(ALIASES)
    with pytest.raises(ValueError):
        set_pricing_aliases({BTTS: SYN_ADV_ARG})
    # The failed install must not have cleared/half-replaced the registry
    # arbitrarily; what matters for safety: resolution is never the BAD mapping.
    assert resolve_pricing_alias(BTTS) == BTTS


def test_pricing_config_validates_aliases_at_load() -> None:
    ok = PricingConfig(leg_pricing_aliases=dict(ALIASES))
    assert ok.leg_pricing_aliases[CHAMP_AR] == SYN_ADV_ARG
    with pytest.raises(Exception, match="never override|classifies"):
        PricingConfig(leg_pricing_aliases={BTTS: SYN_ADV_ARG})


# --- classification / markup ---------------------------------------------------


def test_champion_classifies_unknown_without_alias() -> None:
    assert classify_leg(CHAMP_AR) is LegType.UNKNOWN
    assert classify_sport(CHAMP_AR) is Sport.UNKNOWN
    assert sport_of([CHAMP_AR, BTTS]) == "other"  # the observed-live 0-markup bug
    assert game_key(CHAMP_EVENT) == "26"  # would NOT join the final's game


def test_champion_classifies_as_advance_with_alias() -> None:
    set_pricing_aliases(ALIASES)
    assert classify_leg(CHAMP_AR) is LegType.ADVANCE
    assert classify_leg(CHAMP_ES) is LegType.ADVANCE
    assert classify_sport(CHAMP_AR) is Sport.SOCCER
    assert not is_period_leg(CHAMP_AR)
    # markup: the combo tags soccer, so the soccer markup (not 0) applies
    assert sport_of([CHAMP_AR, BTTS]) == "soccer"


def test_game_key_follows_event_alias() -> None:
    set_pricing_aliases(ALIASES)
    assert game_key(CHAMP_EVENT) == GAME
    assert game_key(BTTS_EVENT) == GAME  # unaliased events unchanged
    assert game_key("KXWCGAME-26JUL18FRAENG") == "26JUL18FRAENG"


# --- relationships --------------------------------------------------------------


def test_champion_leg_joins_the_finals_game_group() -> None:
    set_pricing_aliases(ALIASES)
    legs = [leg(CHAMP_AR, CHAMP_EVENT), leg(BTTS, BTTS_EVENT)]
    rel = classify_legs(legs, MappingProvider({}))
    assert rel.kind is RelationshipKind.OK
    assert (0, 1) in rel.same_event_groups


def test_champion_pair_me_uses_the_real_event() -> None:
    # AR + ES both-YES: mutual exclusion must key on the REAL exchange event
    # (KXMENWORLDCUP-26 metadata), not the synthetic one — and stays unfarmable
    # (metadata-based, not a tautology).
    set_pricing_aliases(ALIASES)
    legs = [leg(CHAMP_AR, CHAMP_EVENT), leg(CHAMP_ES, CHAMP_EVENT)]
    rel = classify_legs(legs, MappingProvider({CHAMP_EVENT: True}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert not rel.farmable


# --- structural parity (the point of the feature) -------------------------------


def _pricer() -> StructuralPricer:
    return StructuralPricer(StructuralConfig(enabled=True))  # type: ignore[arg-type]


def test_aliased_champion_prices_structurally_and_matches_literal_advance() -> None:
    set_pricing_aliases(ALIASES)
    beliefs = [belief(0.55), belief(0.62)]
    sides = ["yes", "yes"]
    aliased_legs = [leg(CHAMP_AR, CHAMP_EVENT), leg(TOTAL, TOTAL_EVENT)]
    literal_legs = [leg(SYN_ADV_ARG, SYN_EVENT), leg(TOTAL, TOTAL_EVENT)]
    est_aliased, reason_aliased = _pricer().try_price(aliased_legs, beliefs, sides)
    est_literal, reason_literal = _pricer().try_price(literal_legs, beliefs, sides)
    assert reason_aliased is None and reason_literal is None
    assert est_aliased is not None and est_literal is not None
    # Bit-identical joint: the alias may not change the math, only the routing.
    assert est_aliased.p == est_literal.p
    assert est_aliased.uncertainty == est_literal.uncertainty


def test_champion_combo_falls_back_without_alias() -> None:
    beliefs = [belief(0.55), belief(0.62)]
    est, reason = _pricer().try_price(
        [leg(CHAMP_AR, CHAMP_EVENT), leg(TOTAL, TOTAL_EVENT)], beliefs, ["yes", "yes"]
    )
    assert est is None and reason is not None  # honest copula fallback pre-alias


# --- risk-side game plans (pricing/risk must move together) ----------------------


def test_build_game_plans_folds_aliased_champion_into_the_final() -> None:
    set_pricing_aliases(ALIASES)
    tickers = [CHAMP_AR, SYN_ADV_ESP]
    events = [CHAMP_EVENT, SYN_EVENT]
    plans, copula = build_game_plans(
        tickers, events, [0.55, 0.45], StructuralConfigView()
    )
    assert len(plans) == 1 and copula == []
    assert sorted(plans[0].global_indices) == [0, 1]


def test_build_game_plans_without_alias_champion_is_copula() -> None:
    tickers = [CHAMP_AR, SYN_ADV_ESP]
    events = [CHAMP_EVENT, SYN_EVENT]
    plans, copula = build_game_plans(
        tickers, events, [0.55, 0.45], StructuralConfigView()
    )
    assert 0 in copula  # ungrouped, unparseable champion leg stays copula


# --- process-boundary installs ---------------------------------------------------


def test_engine_init_installs_aliases() -> None:
    # The REAL ctor path: constructing an engine from a config carrying aliases
    # must land them in the process registry (this is exactly what each
    # pricing-pool worker does in its initializer — same ctor, same config).
    from combomaker.core.conventions import load_conventions
    from combomaker.ops.pricing_pool import _StubFeed, _StubMetadata
    from combomaker.pricing.engine import PricingEngine

    set_pricing_aliases({})
    PricingEngine(
        _StubFeed(),  # type: ignore[arg-type]
        _StubMetadata(),  # type: ignore[arg-type]
        load_conventions(),
        PricingConfig(leg_pricing_aliases=dict(ALIASES)),
        joint_memo_maxsize=0,
    )
    assert resolve_pricing_alias(CHAMP_AR) == SYN_ADV_ARG


def test_book_risk_pool_initializer_installs_aliases() -> None:
    _book_risk_pool_init(dict(ALIASES))
    assert resolve_pricing_alias(CHAMP_ES) == SYN_ADV_ESP
    assert game_key(CHAMP_EVENT) == GAME
    with pytest.raises(ValueError):
        _book_risk_pool_init({BTTS: SYN_ADV_ARG})  # fails LOUDLY, in-worker too
