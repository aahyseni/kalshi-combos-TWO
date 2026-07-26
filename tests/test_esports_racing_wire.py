"""ESPORTS + RACING wire (operator 2026-07-26: winners only, 3-5c per sport,
4-6c cross-sport, independently priced).

Ticker grammar is TAPE/API-VERIFIED (400k-RFQ scan + GET /series): winners are
KXLOLGAME / KXCS2GAME / KXCSGOGAME / KXF1RACE / KXNASCARRACE. Their
near-neighbours (…TOP5, …PODIUM, …SPRINT, …OLD, map winners) are NOT winner
markets and must stay UNKNOWN ⇒ no-quote.
"""

from __future__ import annotations

from combomaker.ops.config import MarkupConfig, MarkupTier, SportMarkupConfig
from combomaker.pricing.legtypes import LegType, Sport, classify_leg, classify_sport
from combomaker.pricing.markup import MarkupPolicy

LOL = "KXLOLGAME-26JUL180700KCT1-T1"
CS2 = "KXCS2GAME-26JUL26XYZ-TEAMA"
F1 = "KXF1RACE-BELGP26-ANT"
NASCAR = "KXNASCARRACE-WINW26-DEHA"
MLB = "KXMLBGAME-26JUL251905ATLBAL-BAL"


class TestClassification:
    def test_winners_are_moneyline(self) -> None:
        for t in (LOL, CS2, "KXCSGOGAME-X-A", F1, NASCAR):
            assert classify_leg(t) is LegType.MONEYLINE, t

    def test_non_winner_neighbours_stay_unknown(self) -> None:
        # Each of these is a REAL exchange series (GET /series) that a loose
        # substring rule would have swallowed into MONEYLINE.
        for t in (
            "KXF1RACETOP5-BELGP26-ANT",
            "KXF1RACETOP10-BELGP26-ANT",
            "KXF1RACEPODIUM-BELGP26-ANT",
            "KXF1RACESPRINT-BELGP26-ANT",
            "KXNASCARTOP5-WINW26-DEHA",
            "KXNASCARRACEOLD-WINW26-DEHA",
            "KXCS2MAP-X-A",
            "KXLOLMAP-X-A",
        ):
            assert classify_leg(t) is LegType.UNKNOWN, t

    def test_sport_tags(self) -> None:
        assert classify_sport(LOL) is Sport.ESPORTS
        assert classify_sport(CS2) is Sport.ESPORTS
        assert classify_sport(F1) is Sport.RACING
        assert classify_sport(NASCAR) is Sport.RACING


def _policy() -> MarkupPolicy:
    tiers = [
        MarkupTier(fair_below_cc=1000, markup_cc=500),
        MarkupTier(fair_below_cc=2000, markup_cc=400),
    ]
    mixed_tiers = [
        MarkupTier(fair_below_cc=1000, markup_cc=600),
        MarkupTier(fair_below_cc=2000, markup_cc=500),
    ]
    return MarkupPolicy.from_config(
        MarkupConfig(
            enabled=True,
            mlb=SportMarkupConfig(enabled=True, markup_cc=100),
            esports=SportMarkupConfig(enabled=True, markup_cc=300, tiers=tiers),
            racing=SportMarkupConfig(enabled=True, markup_cc=300, tiers=tiers),
            mixed=SportMarkupConfig(enabled=True, markup_cc=400, tiers=mixed_tiers),
        )
    )


class TestMarkup:
    def test_single_sport_tiers(self) -> None:
        p = _policy()
        assert p.markup_for([LOL], fair_cc=500) == ("esports", 500)    # <10c → 5c
        assert p.markup_for([F1], fair_cc=1500) == ("racing", 400)     # 10-20c → 4c
        assert p.markup_for([NASCAR], fair_cc=5000) == ("racing", 300)  # main → 3c

    def test_cross_sport_uses_the_mixed_tier(self) -> None:
        p = _policy()
        # esports + racing + MLB — the operator's stated shape.
        sport, cc = p.markup_for([LOL, F1, MLB], fair_cc=500)
        assert sport == "mixed" and cc == 600      # longshot cross-sport → 6c
        sport, cc = p.markup_for([LOL, MLB], fair_cc=1500)
        assert sport == "mixed" and cc == 500      # 10-20c → 5c
        sport, cc = p.markup_for([F1, MLB], fair_cc=9000)
        assert sport == "mixed" and cc == 400      # main → 4c base

    def test_unknown_leg_keeps_the_zero_failsafe(self) -> None:
        p = _policy()
        # A dark/unknown sport anywhere ⇒ markup 0, never a mixed markup.
        assert p.markup_for([LOL, "KXNFLGAME-X-A"], fair_cc=500) == ("other", 0)

    def test_dark_config_stays_dark(self) -> None:
        # Master switch off ⇒ no sport is active, so the cross-sport branch
        # never engages: the combo stays ('other', 0), bit-identical to the
        # pre-markup pricer.
        dark = MarkupPolicy.from_config(MarkupConfig(enabled=False))
        assert dark.markup_for([LOL, F1], fair_cc=500) == ("other", 0)
