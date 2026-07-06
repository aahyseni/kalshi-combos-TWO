"""Structural adapter: ticker parsing, honest fallbacks, priced uncertainty,
and the engine integration (config-gated, copula fallback on any doubt)."""

from __future__ import annotations

import pytest

from combomaker.core.conventions import DOC_ASSUMED
from combomaker.ops.config import PricingConfig, StructuralConfig
from combomaker.pricing.dixon_coles import (
    Advance,
    Btts,
    Draw,
    MatchFormat,
    PlayerScores,
    Team,
    TeamWin,
    TotalOver,
)
from combomaker.pricing.engine import PricingEngine
from combomaker.pricing.legs import LegBelief
from combomaker.pricing.quote import ConstructedQuote
from combomaker.pricing.structural import (
    StructuralPricer,
    _parse_leg,
    _parse_match,
    structural_applicable,
)
from combomaker.rfq.models import RfqLeg
from tests.test_archetypes import SGP_EVENT, TTC, same_event_combo
from tests.test_filters import Harness
from tests.test_pricing_engine import seed_event

GAME = "26JUL10ENGNOR"
ML = f"KXWCGAME-{GAME}-ENG"
ML_B = f"KXWCGAME-{GAME}-NOR"
BTTS = f"KXWCBTTS-{GAME}"
GOAL = f"KXWCGOAL-{GAME}-ENGHKANE9-1"
TOTAL = f"KXWCTOTAL-{GAME}-3"


def leg(ticker: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(ticker, SGP_EVENT, side, None)


def belief(p: float, unc: float = 0.005) -> LegBelief:
    return LegBelief(p=p, uncertainty=unc, source="test")


def pricer(**overrides: object) -> StructuralPricer:
    return StructuralPricer(StructuralConfig(enabled=True, **overrides))  # type: ignore[arg-type]


class TestParsing:
    def test_match_teams(self) -> None:
        match = _parse_match(GAME)
        assert match is not None
        assert (match.team_a, match.team_b) == ("ENG", "NOR")

    def test_match_with_start_time(self) -> None:
        match = _parse_match("26JUL081840MEXENG")  # optional HHMM start time
        assert match is not None
        assert (match.team_a, match.team_b) == ("MEX", "ENG")

    def test_odd_length_team_codes_refuse(self) -> None:
        assert _parse_match("26JUL10ENGNORX") is None  # 7 chars: no clean split
        assert _parse_match("26JUL10AB") is None       # too short

    def test_knockout_leg_specs_follow_rule_book_windows(self) -> None:
        """Kalshi rules text: game market = ADVANCE (ET+pens); BTTS/totals =
        regulation only; player props = full game (ET, no pens)."""
        match = _parse_match(GAME)
        assert match is not None
        ko = MatchFormat.KNOCKOUT
        assert _parse_leg(ML, match, fmt=ko) == Advance(Team.A)
        assert _parse_leg(ML_B, match, fmt=ko) == Advance(Team.B)
        assert _parse_leg(f"KXWCGAME-{GAME}-TIE", match, fmt=ko) == Draw()
        assert _parse_leg(BTTS, match, fmt=ko) == Btts(include_et=False)
        assert _parse_leg(TOTAL, match, fmt=ko) == TotalOver(3, include_et=False)
        assert _parse_leg(GOAL, match, fmt=ko) == PlayerScores(
            Team.A, min_goals=1, include_et=True
        )

    def test_group_leg_specs(self) -> None:
        match = _parse_match(GAME)
        assert match is not None
        gr = MatchFormat.GROUP
        assert _parse_leg(ML, match, fmt=gr) == TeamWin(Team.A, include_et=False)
        assert _parse_leg(BTTS, match, fmt=gr) == Btts(include_et=False)
        assert _parse_leg(GOAL, match, fmt=gr) == PlayerScores(
            Team.A, min_goals=1, include_et=False
        )

    def test_unmatched_team_suffix_is_reason(self) -> None:
        match = _parse_match(GAME)
        assert match is not None
        out = _parse_leg(f"KXWCGAME-{GAME}-BRA", match, fmt=MatchFormat.KNOCKOUT)
        assert isinstance(out, str) and "neither team" in out

    def test_player_on_team_b(self) -> None:
        match = _parse_match(GAME)
        assert match is not None
        spec = _parse_leg(f"KXWCGOAL-{GAME}-NORHAALAND9-1", match, fmt=MatchFormat.KNOCKOUT)
        assert spec == PlayerScores(Team.B, min_goals=1, include_et=True)


class TestPricing:
    # Anchors from an independent 2M-path Monte Carlo under the RULE-BOOK
    # windows (advance incl pens 0.5 / regulation BTTS / full-game goals):
    # scratchpad anchor_rules_windows.py, 2026-07-06.
    def test_eng_nor_prices_near_structural_fair(self) -> None:
        est, reason = pricer(dc_rho=0.0).try_price(
            [leg(ML), leg(GOAL), leg(BTTS)],
            [belief(0.65), belief(0.50), belief(0.55)],
            ["yes", "yes", "yes"],
        )
        assert reason is None and est is not None
        assert est.p == pytest.approx(0.2401, abs=0.002)
        assert est.uncertainty > 0.0
        assert any("dc inversion" in n for n in est.notes)

    def test_dog_side_spa_por_shape(self) -> None:
        est, reason = pricer(dc_rho=0.0).try_price(
            [leg(ML), leg(BTTS), leg(GOAL)],
            [belief(0.24), belief(0.60), belief(0.38)],
            ["yes", "yes", "yes"],
        )
        assert reason is None and est is not None
        assert est.p == pytest.approx(0.1153, abs=0.002)

    def test_unparseable_leg_falls_back_with_reason(self) -> None:
        est, reason = pricer().try_price(
            [leg(f"KXWCGAME-{GAME}-BRA"), leg(BTTS)],
            [belief(0.5), belief(0.5)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "neither team" in reason

    def test_non_soccer_refuses(self) -> None:
        est, reason = pricer().try_price(
            [leg("KXNBAGAME-26JUL10LALBOS-LAL"), leg("KXNBATOTAL-26JUL10LALBOS-200")],
            [belief(0.5), belief(0.5)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "soccer-only" in reason

    def test_cross_match_legs_refuse(self) -> None:
        est, reason = pricer().try_price(
            [leg(ML), leg("KXWCBTTS-26JUL10SPAPOR")],
            [belief(0.5), belief(0.5)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "different matches" in reason

    def test_pens_band_widens_advance_legs_only(self) -> None:
        def pens_component(est_notes: tuple[str, ...]) -> float:
            note = next(n for n in est_notes if "pens=" in n)
            return float(note.split("pens=")[1].split(" ")[0].rstrip(")"))

        with_adv, _ = pricer(dc_rho=0.0).try_price(
            [leg(ML), leg(BTTS)], [belief(0.65), belief(0.55)], ["yes", "yes"]
        )
        without_adv, _ = pricer(dc_rho=0.0).try_price(
            [leg(TOTAL), leg(BTTS)], [belief(0.55), belief(0.55)], ["yes", "yes"]
        )
        assert with_adv is not None and without_adv is not None
        assert pens_component(with_adv.notes) > 0.0   # shootout prob matters
        assert pens_component(without_adv.notes) == 0.0  # no advance leg

    def test_wider_marginal_bands_widen_joint(self) -> None:
        tight, _ = pricer(dc_rho=0.0).try_price(
            [leg(ML), leg(BTTS)], [belief(0.65, 0.002), belief(0.55, 0.002)], ["yes", "yes"]
        )
        wide, _ = pricer(dc_rho=0.0).try_price(
            [leg(ML), leg(BTTS)], [belief(0.65, 0.02), belief(0.55, 0.02)], ["yes", "yes"]
        )
        assert tight is not None and wide is not None
        assert wide.uncertainty > tight.uncertainty

    def test_no_side_leg_priced_as_complement(self) -> None:
        p = pricer(dc_rho=0.0)
        both, _ = p.try_price(
            [leg(ML), leg(BTTS)], [belief(0.65), belief(0.55)], ["yes", "yes"]
        )
        a_not_b, _ = p.try_price(
            [leg(ML), leg(BTTS, side="no")], [belief(0.65), belief(0.55)], ["yes", "no"]
        )
        assert both is not None and a_not_b is not None
        assert both.p + a_not_b.p == pytest.approx(0.65, abs=1e-6)


class TestApplicability:
    def test_single_soccer_group_applies(self) -> None:
        assert structural_applicable([leg(ML), leg(BTTS)], [(0, 1)])

    def test_cross_event_does_not_apply(self) -> None:
        assert not structural_applicable([leg(ML), leg(BTTS)], [])
        assert not structural_applicable(
            [leg(ML), leg(BTTS), leg("KXWCBTTS-26JUL10SPAPOR")], [(0, 1)]
        )

    def test_non_soccer_does_not_apply(self) -> None:
        legs = [leg("KXNBAGAME-26JUL10LALBOS-LAL"), leg("KXNBATOTAL-26JUL10LALBOS-200")]
        assert not structural_applicable(legs, [(0, 1)])


async def wc_engine(config: PricingConfig) -> PricingEngine:
    from tests.test_feed import snapshot_env

    h = Harness()
    tickers = [ML, BTTS, GOAL]
    books = {
        ML: ([["0.6300", "50.00"], ["0.6400", "20.00"]],
             [["0.3400", "60.00"], ["0.3500", "25.00"]]),
        BTTS: ([["0.5400", "50.00"], ["0.5500", "20.00"]],
               [["0.4400", "60.00"], ["0.4500", "25.00"]]),
        GOAL: ([["0.4900", "50.00"], ["0.5000", "20.00"]],
               [["0.4900", "60.00"], ["0.5000", "25.00"]]),
    }
    h.feed.watch(tickers)
    await h.ws.ack_subscription(0, 5)
    for i, ticker in enumerate(tickers):
        env = snapshot_env(5, i + 1, ticker)
        env["msg"]["yes_dollars_fp"], env["msg"]["no_dollars_fp"] = books[ticker]
        await h.ws.deliver(env)
    h.with_meta("KXMVE-C1")
    seed_event(h, SGP_EVENT, exclusive=False)
    return PricingEngine(h.feed, h.metadata, DOC_ASSUMED, config)


async def test_engine_uses_structural_when_enabled() -> None:
    rfq = same_event_combo([ML, BTTS, GOAL])
    on = await wc_engine(PricingConfig())  # enabled by default since OOS gate pass
    off = await wc_engine(PricingConfig(structural=StructuralConfig(enabled=False)))
    structural = on.price(rfq, time_to_close_s=TTC)
    copula = off.price(rfq, time_to_close_s=TTC)
    assert isinstance(structural, ConstructedQuote), structural
    assert isinstance(copula, ConstructedQuote), copula
    # Different models, different fairs: the structural fair reads the whole
    # scoreline structure, the copula its pairwise-rho approximation.
    assert structural.fair_cc != copula.fair_cc


async def test_engine_falls_back_on_unparseable_combo() -> None:
    # Same-event soccer legs whose game code is too short to split into team
    # codes: structural refuses, engine must price via the copula instead.
    tickers = ["KXWCGAME-26JUL10AB-AB", "KXWCBTTS-26JUL10AB"]
    rfq = same_event_combo(tickers)
    on = await wc_engine_for(tickers, PricingConfig())  # structural on by default
    off = await wc_engine_for(tickers, PricingConfig(structural=StructuralConfig(enabled=False)))
    with_structural = on.price(rfq, time_to_close_s=TTC)
    without = off.price(rfq, time_to_close_s=TTC)
    assert isinstance(with_structural, ConstructedQuote), with_structural
    assert isinstance(without, ConstructedQuote), without
    assert with_structural.fair_cc == without.fair_cc  # identical copula path


async def wc_engine_for(tickers: list[str], config: PricingConfig) -> PricingEngine:
    h = Harness()
    await h.with_books(tickers)
    h.with_meta("KXMVE-C1")
    seed_event(h, SGP_EVENT, exclusive=False)
    return PricingEngine(h.feed, h.metadata, DOC_ASSUMED, config)
