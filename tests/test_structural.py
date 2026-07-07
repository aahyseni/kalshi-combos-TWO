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
from combomaker.pricing.margin_total import (
    GameTotalOver,
    SportShape,
    TeamWins,
    invert_means,
    region_probability,
)
from combomaker.pricing.quote import ConstructedQuote
from combomaker.pricing.structural import (
    StructuralPricer,
    _parse_leg,
    _parse_match,
    _player_team,
    _team_of,
    structural_applicable,
)
from combomaker.rfq.models import RfqLeg
from tests.test_archetypes import SGP_EVENT, TTC, same_event_combo
from tests.test_filters import Harness
from tests.test_pricing_engine import seed_event

GAME = "26JUL10ENGNOR"
ML = f"KXWCADVANCE-{GAME}-ENG"       # advance market: ET + pens (rules text)
ML_B = f"KXWCADVANCE-{GAME}-NOR"
REG_ML = f"KXWCGAME-{GAME}-ENG"      # regulation moneyline: 90' only
BTTS = f"KXWCBTTS-{GAME}-BTTS"
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
        assert _team_of("ENG", match) is Team.A
        assert _team_of("NOR", match) is Team.B
        assert _team_of("BRA", match) is None

    def test_match_with_start_time(self) -> None:
        match = _parse_match("26JUL081840MEXENG")  # optional HHMM start time
        assert match is not None
        assert _team_of("MEX", match) is Team.A

    def test_variable_length_team_codes_resolve(self) -> None:
        """Live-tape shapes: team codes vary in length (PHI+KC, CONN+MIN,
        SEA+LA) — resolution anchors at the ends of the code blob."""
        wnba = _parse_match("26JUL06CONNMIN")
        assert wnba is not None
        assert _team_of("CONN", wnba) is Team.A and _team_of("MIN", wnba) is Team.B
        mlb = _parse_match("26JUL061410PHIKC")
        assert mlb is not None
        assert _team_of("PHI", mlb) is Team.A and _team_of("KC", mlb) is Team.B
        assert _player_team("KCBWITT7", mlb) is Team.B      # KXMLBHIT shape
        sea = _parse_match("26JUL06SEALA")
        assert sea is not None
        assert _team_of("LA", sea) is Team.B and _team_of("SEA", sea) is Team.A
        por = _parse_match("26JUL06PORESP")
        assert por is not None
        assert _player_team("ESPLYAMAL10", por) is Team.B   # live WC shape
        assert _parse_match("26JUL10AB") is None            # too short

    def test_knockout_leg_specs_follow_rule_book_windows(self) -> None:
        """Kalshi rules text + live tape: ADVANCE series = ET+pens; GAME
        series (coexists on the same knockout matches) = Regulation Time
        Moneyline; BTTS/totals = regulation only; player props = full game
        (ET, no pens)."""
        match = _parse_match(GAME)
        assert match is not None
        ko = MatchFormat.KNOCKOUT
        assert _parse_leg(ML, match, fmt=ko) == Advance(Team.A)
        assert _parse_leg(ML_B, match, fmt=ko) == Advance(Team.B)
        assert _parse_leg(REG_ML, match, fmt=ko) == TeamWin(Team.A, include_et=False)
        assert _parse_leg(f"KXWCGAME-{GAME}-TIE", match, fmt=ko) == Draw()
        assert _parse_leg(BTTS, match, fmt=ko) == Btts(include_et=False)
        assert _parse_leg(TOTAL, match, fmt=ko) == TotalOver(3, include_et=False)
        assert _parse_leg(GOAL, match, fmt=ko) == PlayerScores(
            Team.A, min_goals=1, include_et=True
        )
        # live-tape shapes (2026-07-06): Messi 2+ and integer total lines
        assert _parse_leg(
            "KXWCGOAL-26JUL07ARGEGY-ARGLMESSI10-2", _parse_match("26JUL07ARGEGY"), fmt=ko
        ) == PlayerScores(Team.A, min_goals=2, include_et=True)
        assert _parse_leg(
            "KXWCADVANCE-26JUL06PORESP-POR", _parse_match("26JUL06PORESP"), fmt=ko
        ) == Advance(Team.A)

    def test_group_leg_specs(self) -> None:
        match = _parse_match(GAME)
        assert match is not None
        gr = MatchFormat.GROUP
        assert _parse_leg(REG_ML, match, fmt=gr) == TeamWin(Team.A, include_et=False)
        assert _parse_leg(BTTS, match, fmt=gr) == Btts(include_et=False)
        assert _parse_leg(GOAL, match, fmt=gr) == PlayerScores(
            Team.A, min_goals=1, include_et=False
        )
        out = _parse_leg(ML, match, fmt=gr)  # advance ticker, group match
        assert isinstance(out, str) and "non-knockout" in out

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

    def test_mlb_doubleheader_suffix_resolves_both_teams(self) -> None:
        """SOURCE OF TRUTH (prod RFQ tape): MLB doubleheaders carry a trailing
        game-number token (G1/G2) on the game code, e.g.
        KXMLBGAME-26JUL071835MILSTLG1-STL where the blob is MIL+STL+G1. The
        token is stripped before team anchoring so the SUFFIX team (STL — the
        one that would otherwise sit before G1, not at the blob end) resolves."""
        match = _parse_match("26JUL071835MILSTLG1")
        assert match is not None
        assert _team_of("MIL", match) is Team.A
        assert _team_of("STL", match) is Team.B
        # Game 2 of the same doubleheader strips identically.
        match_g2 = _parse_match("26JUL072200MILSTLG2")
        assert match_g2 is not None
        assert _team_of("MIL", match_g2) is Team.A
        assert _team_of("STL", match_g2) is Team.B

    def test_non_doubleheader_code_unchanged(self) -> None:
        """A NON-doubleheader game code (no trailing G-digit token) is not
        touched — MIL/STL still resolve exactly as before."""
        match = _parse_match("26JUL071835MILSTL")
        assert match is not None
        assert _team_of("MIL", match) is Team.A
        assert _team_of("STL", match) is Team.B

    def test_doubleheader_full_leg_resolves_second_team(self) -> None:
        """A full _parse_leg on a real doubleheader ticker resolves the second
        team rather than declining for 'neither team'."""
        ticker = "KXMLBGAME-26JUL071835MILSTLG1-STL"
        match = _parse_match(ticker.split("-")[1])
        assert match is not None
        spec = _parse_leg(ticker, match, fmt=MatchFormat.GROUP)
        assert not isinstance(spec, str), spec
        assert spec == TeamWin(Team.B, include_et=False)


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

    def test_ungated_mt_sport_refuses(self) -> None:
        # default MarginTotalConfig gates every sport off
        est, reason = pricer().try_price(
            [leg("KXNBAGAME-26JUL10LALBOS-LAL"), leg("KXNBATOTAL-26JUL10LALBOS-200")],
            [belief(0.5), belief(0.5)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "not gated" in reason

    def test_mixed_sport_legs_refuse(self) -> None:
        est, reason = pricer().try_price(
            [leg(ML), leg("KXNBATOTAL-26JUL10LALBOS-200")],
            [belief(0.5), belief(0.5)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "multiple sports" in reason

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

    def test_scorer_without_moneyline_declines_orientation(self) -> None:
        # BTTS + Total + scorer, no orienting ML/Advance: orientation is
        # unidentified -> decline to copula (audit #2).
        est, reason = pricer(dc_rho=0.0).try_price(
            [leg(BTTS), leg(TOTAL), leg(GOAL)],
            [belief(0.55), belief(0.55), belief(0.40)],
            ["yes", "yes", "yes"],
        )
        assert est is None and reason is not None
        assert "orientation is unidentified" in reason

    def test_both_team_scorers_mixed_side_orientation_stable(self) -> None:
        # Adversarial under-catch (audit #2): two opposite-team scorers + a
        # mixed-side selection priced ~11c apart under the two team-code
        # orderings when left structural (9.6c ARSTOT vs 20.2c TOTARS). With no
        # orienting leg the guard now declines BOTH orderings, so the copula
        # (orientation-free) prices them identically — blob order can no longer
        # move the quote.
        for blob in ("ARSTOT", "TOTARS"):
            g = f"26JUL05{blob}"
            est, reason = pricer(dc_rho=0.0).try_price(
                [
                    leg(f"KXWCBTTS-{g}-BTTS"),
                    leg(f"KXWCTOTAL-{g}-3"),
                    leg(f"KXWCGOAL-{g}-ARSP9-1"),
                    leg(f"KXWCGOAL-{g}-TOTP9-1", "no"),
                ],
                [belief(0.42), belief(0.60), belief(0.30), belief(0.10)],
                ["yes", "yes", "yes", "no"],
            )
            assert est is None and reason is not None, f"{blob} should decline"
            assert "orientation is unidentified" in reason

    def test_spread_leg_prices_structurally_and_orients(self) -> None:
        # KXWCSPREAD-<game>-<TEAM>n = "TEAM wins by over n-0.5" -> margin >= n.
        # A spread NAMES a team, so [spread, total, scorer] prices structurally
        # (the spread resolves orientation; no copula decline). Marginals are
        # consistent with lam=(1.6, 1.1) so the exact 2-system solves.
        est, reason = pricer(dc_rho=0.0).try_price(
            [leg(f"KXWCSPREAD-{GAME}-ENG2"), leg(TOTAL), leg(GOAL)],
            [belief(0.2552), belief(0.5064), belief(0.40)],
            ["yes", "yes", "yes"],
        )
        assert reason is None and est is not None
        assert 0.0 < est.p < 1.0


class TestMarginTotalDispatch:
    NBA_ML = "KXNBAGAME-26OCT10LALBOS-LAL"
    NBA_TOTAL = "KXNBATOTAL-26OCT10LALBOS-225"

    def mt_pricer(self, sports: list[str]) -> StructuralPricer:
        from combomaker.ops.config import MarginTotalConfig

        return StructuralPricer(
            StructuralConfig(enabled=True),
            MarginTotalConfig(enabled_sports=sports),
        )

    def test_nba_ml_total_prices_near_product(self) -> None:
        est, reason = self.mt_pricer(["nba"]).try_price(
            [leg(self.NBA_ML), leg(self.NBA_TOTAL)],
            [belief(0.62), belief(0.55)],
            ["yes", "yes"],
        )
        assert reason is None and est is not None
        # calibrated NBA rho(M,T) = 0.000: the structural joint is the
        # product of the marginals (that's the finding, not an assumption).
        assert est.p == pytest.approx(0.62 * 0.55, abs=0.002)
        assert est.uncertainty > 0.004  # discreteness band present
        assert any("structural-mt" in n for n in est.notes)

    def test_ungated_sport_falls_back(self) -> None:
        est, reason = self.mt_pricer([]).try_price(
            [leg(self.NBA_ML), leg(self.NBA_TOTAL)],
            [belief(0.62), belief(0.55)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "not gated" in reason

    def test_spread_leg_comonotone_with_moneyline(self) -> None:
        """DOC-VERIFIED spread convention (TEAMn = wins by over n-0.5):
        win-and-cover collapses to the cover marginal, exactly."""
        # ML 0.62 implies mu_M = 4.19; the consistent 3.5-line cover prob is
        # norm.cdf((4.19-3.5)/13.71) = 0.520 (inconsistent inputs would be
        # least-squares compromised and priced with misfit width instead).
        est, reason = self.mt_pricer(["nba"]).try_price(
            [leg(self.NBA_ML), leg("KXNBASPREAD-26OCT10LALBOS-LAL4"),
             leg(self.NBA_TOTAL)],
            [belief(0.62), belief(0.52), belief(0.55)],
            ["yes", "yes", "yes"],
        )
        assert reason is None and est is not None
        # joint = P(cover) x P(over) at rho=0 (win is implied by covering)
        assert est.p == pytest.approx(0.52 * 0.55, abs=0.005)

    def test_mlb_ml_total_prices_via_runs_grid(self) -> None:
        from combomaker.ops.config import MlbRunsConfig

        p = StructuralPricer(
            StructuralConfig(enabled=True),
            None,
            MlbRunsConfig(enabled=True),
        )
        est, reason = p.try_price(
            [leg("KXMLBGAME-26JUL061410PHIKC-PHI"),
             leg("KXMLBTOTAL-26JUL061410PHIKC-9")],
            [belief(0.55), belief(0.48)],
            ["yes", "yes"],
        )
        assert reason is None and est is not None
        assert 0.0 < est.p < 0.55
        assert any("structural-mlb" in n for n in est.notes)

    def test_mlb_ungated_falls_back(self) -> None:
        from combomaker.ops.config import MlbRunsConfig

        p = StructuralPricer(
            StructuralConfig(enabled=True), None, MlbRunsConfig(enabled=False)
        )
        est, reason = p.try_price(
            [leg("KXMLBGAME-26JUL061410PHIKC-PHI"),
             leg("KXMLBTOTAL-26JUL061410PHIKC-9")],
            [belief(0.55), belief(0.48)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "not gated" in reason

    def test_no_side_total_is_complement(self) -> None:
        p = self.mt_pricer(["nba"])
        over, _ = p.try_price(
            [leg(self.NBA_ML), leg(self.NBA_TOTAL)],
            [belief(0.62), belief(0.55)],
            ["yes", "yes"],
        )
        under, _ = p.try_price(
            [leg(self.NBA_ML), leg(self.NBA_TOTAL, side="no")],
            [belief(0.62), belief(0.55)],
            ["yes", "no"],
        )
        assert over is not None and under is not None
        assert over.p + under.p == pytest.approx(0.62, abs=1e-4)

    def test_prices_in_leg_frame_not_calibration_frame(self) -> None:
        """Regression for the away/home frame fix: WNBA has nonzero rho, its
        moneyline market is on the HOME team (blob suffix = Team.B), and the
        adapter must price in the leg frame (rho negated) so the joint equals
        the home-frame reference the OOS gate validated — NOT the sign-confused
        value the pre-fix adapter produced."""
        from combomaker.ops.config import MarginTotalConfig
        from combomaker.pricing.margin_total import shape_in_leg_frame

        # 26JUL05INDLV: IND away (Team.A), LV home (Team.B). ML on LV.
        p = StructuralPricer(
            StructuralConfig(enabled=True),
            MarginTotalConfig(enabled_sports=["wnba"]),
        )
        est, reason = p.try_price(
            [leg("KXWNBAGAME-26JUL05INDLV-LV"), leg("KXWNBATOTAL-26JUL05INDLV-186")],
            [belief(0.575), belief(0.55)],
            ["yes", "yes"],
        )
        assert reason is None and est is not None

        cal = SportShape(12.04, 16.55, -0.019)   # calibration frame, Team.A=home
        total = GameTotalOver(185.5)             # suffix 186 -> over 185.5
        inv = invert_means([(TeamWins(Team.A), 0.575), (total, 0.55)], cal)
        ref = region_probability(
            inv.mu_m, inv.mu_t, cal, [(TeamWins(Team.A), True), (total, True)]
        )
        assert est.p == pytest.approx(ref, abs=1e-6)
        # The sign-confused frame (leg specs + un-negated cal rho) differs —
        # proves the adapter actually applies shape_in_leg_frame.
        assert shape_in_leg_frame(12.04, 16.55, -0.019).rho == pytest.approx(0.019)


class TestApplicability:
    def test_single_soccer_group_applies(self) -> None:
        assert structural_applicable([leg(ML), leg(BTTS)], [(0, 1)])

    def test_cross_event_does_not_apply(self) -> None:
        assert not structural_applicable([leg(ML), leg(BTTS)], [])
        assert not structural_applicable(
            [leg(ML), leg(BTTS), leg("KXWCBTTS-26JUL10SPAPOR")], [(0, 1)]
        )

    def test_margin_total_sports_apply(self) -> None:
        legs = [leg("KXNBAGAME-26JUL10LALBOS-LAL"), leg("KXNBATOTAL-26JUL10LALBOS-200")]
        assert structural_applicable(legs, [(0, 1)])

    def test_unmodeled_sport_does_not_apply(self) -> None:
        legs = [leg("KXUFCFIGHT-26JUL11MCGHOL-HOL"), leg("KXUFCFIGHT-26JUL11ABCDEF-ABC")]
        assert not structural_applicable(legs, [(0, 1)])

    def test_period_leg_declines_even_in_one_soccer_group(self) -> None:
        # Period legs now rejoin the same-game copula group, so this guard is
        # the reachable barrier keeping a 1H leg off the full-game inverter.
        legs = [leg(f"KXWC1HTOTAL-{GAME}-2"), leg(TOTAL)]
        assert not structural_applicable(legs, [(0, 1)])


class TestPeriodGuard:
    def test_try_price_declines_a_period_leg(self) -> None:
        est, reason = pricer(dc_rho=0.0).try_price(
            [leg(f"KXWC1HTOTAL-{GAME}-2"), leg(TOTAL)],
            [belief(0.40), belief(0.55)],
            ["yes", "yes"],
        )
        assert est is None and reason is not None and "period leg" in reason


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
