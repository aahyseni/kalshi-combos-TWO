"""Tests for pricing/relationships.py: classify_legs and its UNKNOWN discipline."""

from __future__ import annotations

import pytest

from combomaker.pricing.relationships import Relationship, RelationshipKind, classify_legs
from combomaker.rfq.models import RfqLeg


def leg(market: str, event: str | None, side: str = "yes") -> RfqLeg:
    return RfqLeg(
        market_ticker=market, event_ticker=event, side=side, yes_settlement_value_cc=None
    )


class MappingProvider:
    """Answers from a dict; missing keys mean 'unknown' (None)."""

    def __init__(self, answers: dict[str, bool | None]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        self.calls.append(event_ticker)
        return self._answers.get(event_ticker)


class ExplodingProvider:
    """Fails the test if the classifier consults it at all."""

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        raise AssertionError(f"provider consulted for {event_ticker}")


def test_clean_cross_event_combo_is_ok_with_no_groups() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M2", "E2", "no"), leg("M3", "E3", "yes"))
    # Every event has a single leg, so the provider must never be consulted.
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ()
    assert rel.notes == ()


def test_same_event_not_exclusive_ok_with_group() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M2", "E1", "yes"))
    rel = classify_legs(legs, MappingProvider({"E1": False}))
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)


def test_two_yes_legs_of_exclusive_event_impossible() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M2", "E1", "yes"))
    rel = classify_legs(legs, MappingProvider({"E1": True}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.same_event_groups == ()
    # Mutual exclusion rests on exchange METADATA, not a logical tautology —
    # NOT farmable (a wrong flag would misclassify a possible combo).
    assert rel.farmable is False


def test_yes_and_no_legs_of_exclusive_event_ok_and_grouped() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M2", "E1", "no"))
    rel = classify_legs(legs, MappingProvider({"E1": True}))
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)


def test_unknown_exclusivity_for_multi_leg_event_is_unknown() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M2", "E1", "no"))
    rel = classify_legs(legs, MappingProvider({}))  # provider returns None
    assert rel.kind is RelationshipKind.UNKNOWN
    assert rel.same_event_groups == ()


def test_same_market_both_sides_impossible_without_provider() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M1", "E1", "no"))
    rel = classify_legs(legs, ExplodingProvider())  # decided before event lookup
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.same_event_groups == ()
    # Airtight tautology (YES-and-NO of one market) ⇒ farmable.
    assert rel.farmable is True


def test_same_market_same_side_twice_is_degenerate_unknown() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M1", "E1", "yes"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN
    assert any("duplicate" in note for note in rel.notes)


def test_leg_without_event_ticker_is_unknown() -> None:
    legs = (leg("M1", None, "yes"), leg("M2", "E2", "no"))
    rel = classify_legs(legs, ExplodingProvider())  # decided before event lookup
    assert rel.kind is RelationshipKind.UNKNOWN
    assert rel.same_event_groups == ()


def test_provider_consulted_only_for_multi_leg_events() -> None:
    legs = [leg("M1", "E1", "yes"), leg("M2", "E1", "no"), leg("M3", "E2", "yes")]
    provider = MappingProvider({"E1": False})
    rel = classify_legs(legs, provider)  # list input is accepted too
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)
    assert provider.calls == ["E1"]  # single-leg E2 never consulted


def test_group_indices_track_original_leg_positions() -> None:
    legs = (leg("M1", "E1", "yes"), leg("M2", "E2", "no"), leg("M3", "E1", "no"))
    rel = classify_legs(legs, MappingProvider({"E1": False}))
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 2),)


# --- same-GAME grouping (the event_ticker-is-per-series fix) ---------------------


def test_same_game_cross_series_legs_form_one_group() -> None:
    """A real SGP — BTTS + moneyline + total of ONE game — arrives as three
    DIFFERENT per-series event_tickers but must correlate as one same-game block,
    not price independent. (Each event has a single leg, so exclusivity is never
    consulted — ExplodingProvider proves it.)"""
    legs = (
        leg("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
        leg("KXWCTOTAL-26JUL05MEXENG-3", "KXWCTOTAL-26JUL05MEXENG", "yes"),
        leg("KXWCBTTS-26JUL05MEXENG-BTTS", "KXWCBTTS-26JUL05MEXENG", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1, 2),)
    assert any("same-game group 26JUL05MEXENG" in n for n in rel.notes)


def test_cross_game_legs_stay_independent() -> None:
    legs = (
        leg("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
        leg("KXWCGAME-26JUL06ARGEGY-ARG", "KXWCGAME-26JUL06ARGEGY", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ()  # different games -> independent


def test_two_games_two_legs_each_form_two_blocks() -> None:
    legs = (
        leg("KXWCGAME-G1-A", "KXWCGAME-G1", "yes"),
        leg("KXWCTOTAL-G1-3", "KXWCTOTAL-G1", "yes"),
        leg("KXWCGAME-G2-B", "KXWCGAME-G2", "yes"),
        leg("KXWCBTTS-G2-BTTS", "KXWCBTTS-G2", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1), (2, 3))  # per-game blocks


def test_period_market_now_rejoins_full_game_block() -> None:
    """A first-half total shares a game code with the full-game markets and now
    REJOINS the same-game block so the copula can correlate the (modeled) 1H×FT
    pair — it no longer prices at independence. It is kept off the full-game
    STRUCTURAL inverter by a guard in structural.py, not by grouping it out."""
    legs = (
        leg("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
        leg("KXWCTOTAL-26JUL05MEXENG-3", "KXWCTOTAL-26JUL05MEXENG", "yes"),
        leg("KXWC1HTOTAL-26JUL05MEXENG-2", "KXWC1HTOTAL-26JUL05MEXENG", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1, 2),)  # 1H leg joins the block


# --- period × full-time BTTS containment -----------------------------------------


FH_BTTS = "KXWC1HBTTS-26JUL05MEXENG-BTTS"
FH_BTTS_EV = "KXWC1HBTTS-26JUL05MEXENG"
FT_BTTS = "KXWCBTTS-26JUL05MEXENG-BTTS"
FT_BTTS_EV = "KXWCBTTS-26JUL05MEXENG"


def test_1h_btts_yes_ft_btts_yes_is_containment() -> None:
    """1H-BTTS yes ⟹ FT-BTTS yes: the joint is exactly P(1H-BTTS), so the pair
    classifies CONTAINMENT with the subset (1H) → superset (FT) indices."""
    legs = (leg(FH_BTTS, FH_BTTS_EV, "yes"), leg(FT_BTTS, FT_BTTS_EV, "yes"))
    rel = classify_legs(legs, ExplodingProvider())  # single-leg events: no lookup
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (0, 1)


def test_1h_btts_yes_ft_btts_no_is_impossible() -> None:
    legs = (leg(FH_BTTS, FH_BTTS_EV, "yes"), leg(FT_BTTS, FT_BTTS_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.containment is None
    assert rel.farmable is True  # airtight scoring tautology


def test_containment_pair_in_larger_combo_is_unknown() -> None:
    """A containment pair mixed with other correlated legs is not yet priced
    coherently — widen-or-no-quote, never a copula guess (defense #2)."""
    legs = (
        leg(FH_BTTS, FH_BTTS_EV, "yes"),
        leg(FT_BTTS, FT_BTTS_EV, "yes"),
        leg("KXWCTOTAL-26JUL05MEXENG-3", "KXWCTOTAL-26JUL05MEXENG", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN


def test_1h_btts_different_game_is_not_containment() -> None:
    """1H-BTTS and FT-BTTS of DIFFERENT games carry no logical relation."""
    legs = (
        leg(FH_BTTS, FH_BTTS_EV, "yes"),
        leg("KXWCBTTS-26JUL06ARGEGY-BTTS", "KXWCBTTS-26JUL06ARGEGY", "no"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK  # cross-game: no containment/impossible


def test_mutual_exclusion_still_caught_within_a_game() -> None:
    """Two YES outcomes of the SAME moneyline event (win + tie) stay impossible
    even though they share a game — exclusion is per-event, not per-game."""
    legs = (
        leg("KXWCGAME-26JUL05MEXENG-MEX", "KXWCGAME-26JUL05MEXENG", "yes"),
        leg("KXWCGAME-26JUL05MEXENG-TIE", "KXWCGAME-26JUL05MEXENG", "yes"),
    )
    rel = classify_legs(legs, MappingProvider({"KXWCGAME-26JUL05MEXENG": True}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is False  # mutual exclusion is metadata-dependent


@pytest.mark.parametrize(
    ("legs", "answers", "expected"),
    [
        pytest.param(
            (leg("M1", "E1", "yes"), leg("M1", "E1", "no")),
            {},
            RelationshipKind.IMPOSSIBLE,
            id="same-market-both-sides",
        ),
        pytest.param(
            (leg("M1", "E1", "yes"), leg("M1", "E1", "yes")),
            {},
            RelationshipKind.UNKNOWN,
            id="duplicate-leg",
        ),
        pytest.param(
            (leg("M1", None, "yes"), leg("M2", "E2", "no")),
            {},
            RelationshipKind.UNKNOWN,
            id="missing-event-ticker",
        ),
        pytest.param(
            (leg("M1", "E1", "yes"), leg("M2", "E1", "no")),
            {},
            RelationshipKind.UNKNOWN,
            id="exclusivity-unknown",
        ),
        pytest.param(
            (leg("M1", "E1", "yes"), leg("M2", "E1", "yes")),
            {"E1": True},
            RelationshipKind.IMPOSSIBLE,
            id="two-yes-exclusive",
        ),
    ],
)
def test_notes_populated_for_every_non_ok_classification(
    legs: tuple[RfqLeg, ...], answers: dict[str, bool | None], expected: RelationshipKind
) -> None:
    rel: Relationship = classify_legs(legs, MappingProvider(answers))
    assert rel.kind is expected
    assert rel.kind is not RelationshipKind.OK
    assert len(rel.notes) > 0
    assert all(isinstance(note, str) and note for note in rel.notes)


# --- same-team nested TEAM-corner containment (over-M ⊆ over-N for M>N) ---------

TC_EV = "KXWCTCORNERS-26JUL05MEXENG"
TC_MEX4 = "KXWCTCORNERS-26JUL05MEXENG-MEX4"
TC_MEX8 = "KXWCTCORNERS-26JUL05MEXENG-MEX8"
TC_ENG4 = "KXWCTCORNERS-26JUL05MEXENG-ENG4"


def test_same_team_corners_higher_yes_lower_no_is_impossible() -> None:
    """Same team's nested corner lines are exact containment: over-8 ⊆ over-4, so
    over-8 YES with over-4 NO can never both settle → IMPOSSIBLE (no-quote)."""
    legs = (leg(TC_MEX8, TC_EV, "yes"), leg(TC_MEX4, TC_EV, "no"))
    rel = classify_legs(legs, MappingProvider({TC_EV: False}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.same_event_groups == ()
    assert rel.farmable is True  # nested-line containment tautology


def test_same_team_corners_lower_yes_higher_no_is_nested_band() -> None:
    """over-4 YES with over-8 NO is the band '4 < corners <= 8' — previously OK
    (copula corners_team:same 0.90); now NESTED_BAND: exact P(low) − P(high).
    (BEHAVIOR CHANGE 2026-07-10, intentional: the exact band arithmetic beats
    the 0.90-rho copula approximation — was test_..._is_possible.)"""
    legs = (leg(TC_MEX4, TC_EV, "yes"), leg(TC_MEX8, TC_EV, "no"))
    rel = classify_legs(legs, MappingProvider({TC_EV: False}))
    assert rel.kind is RelationshipKind.NESTED_BAND
    assert rel.bands == ((0, 1),)
    assert rel.same_event_groups == ((0, 1),)


def test_opposite_team_corners_higher_yes_lower_no_is_not_impossible() -> None:
    """Containment is SAME-team only: MEX over-8 yes × ENG over-4 no carries no
    logical implication (different teams) → OK (copula prices the :opp rho)."""
    legs = (leg(TC_MEX8, TC_EV, "yes"), leg(TC_ENG4, TC_EV, "no"))
    rel = classify_legs(legs, MappingProvider({TC_EV: False}))
    assert rel.kind is RelationshipKind.OK


def test_same_team_corners_same_line_both_sides_is_caught_upstream() -> None:
    """Same team, SAME line, opposite sides is the same market both sides — caught
    by the duplicate-market IMPOSSIBLE guard, not the nested-line branch."""
    legs = (leg(TC_MEX8, TC_EV, "yes"), leg(TC_MEX8, TC_EV, "no"))
    rel = classify_legs(legs, MappingProvider({TC_EV: False}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is True  # same-market both sides tautology


# --- nested MATCH-corner ladders (KXWCCORNERS: bare-digit suffix, scope=match) ---

MC_EV = "KXWCCORNERS-26JUL10ESPBEL"
MC_8 = "KXWCCORNERS-26JUL10ESPBEL-8"
MC_11 = "KXWCCORNERS-26JUL10ESPBEL-11"
MC2_EV = "KXWCCORNERS-26JUL11ARGSUI"
MC2_7 = "KXWCCORNERS-26JUL11ARGSUI-7"
MC2_10 = "KXWCCORNERS-26JUL11ARGSUI-10"
MC3_EV = "KXWCCORNERS-26JUL11NORENG"
MC3_8 = "KXWCCORNERS-26JUL11NORENG-8"
MC3_9 = "KXWCCORNERS-26JUL11NORENG-9"


def test_match_corners_higher_yes_lower_no_is_farmable_impossible() -> None:
    """over-11 YES implies over-8 YES (one combined count, rules-verified incl.
    extra time) — the corners_team farm now covers match-level CORNERS."""
    legs = (leg(MC_11, MC_EV, "yes"), leg(MC_8, MC_EV, "no"))
    rel = classify_legs(legs, MappingProvider({MC_EV: False}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is True


def test_match_corners_band_detected() -> None:
    """yes-low + no-high is the band the side-aware validator allows (114 real
    tape combos): NESTED_BAND, joint exact — never the flat-0.6 copula."""
    legs = (leg(MC_8, MC_EV, "yes"), leg(MC_11, MC_EV, "no"))
    rel = classify_legs(legs, MappingProvider({MC_EV: False}))
    assert rel.kind is RelationshipKind.NESTED_BAND
    assert rel.bands == ((0, 1),)
    assert rel.same_event_groups == ((0, 1),)


def test_match_corners_same_side_rungs_defensive_containment() -> None:
    """The exchange blocks same-side rungs (400 duplicated_legs) — defensive:
    if one ever arrived, the joint pins to P(higher line) exactly."""
    legs = (leg(MC_11, MC_EV, "yes"), leg(MC_8, MC_EV, "yes"))
    rel = classify_legs(legs, MappingProvider({MC_EV: False}))
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (0, 1)  # subset = the higher line


def test_match_corners_both_no_defensive_containment() -> None:
    """¬over-8 ⟹ ¬over-11: subset is the LOWER line's NO."""
    legs = (leg(MC_11, MC_EV, "no"), leg(MC_8, MC_EV, "no"))
    rel = classify_legs(legs, MappingProvider({MC_EV: False}))
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (1, 0)


def test_three_cross_game_bands_all_detected() -> None:
    """The real tape shape (rowid 10983663): a 6-leg combo holding three bands
    in three different games — all three collapse, kind NESTED_BAND."""
    legs = (
        leg(MC_8, MC_EV, "yes"), leg(MC_11, MC_EV, "no"),
        leg(MC2_7, MC2_EV, "yes"), leg(MC2_10, MC2_EV, "no"),
        leg(MC3_8, MC3_EV, "yes"), leg(MC3_9, MC3_EV, "no"),
    )
    rel = classify_legs(
        legs, MappingProvider({MC_EV: False, MC2_EV: False, MC3_EV: False})
    )
    assert rel.kind is RelationshipKind.NESTED_BAND
    assert set(rel.bands) == {(0, 1), (2, 3), (4, 5)}


def test_band_with_same_game_companion_is_unknown() -> None:
    """A band sharing its game with ANY other leg is UNKNOWN: the band is a
    window event, its correlation to a neighbour is NOT the rung's rho
    (attenuation unmeasured) — widen-or-no-quote, never a copula guess."""
    tot_ev = "KXWCTOTAL-26JUL10ESPBEL"
    legs = (
        leg(MC_8, MC_EV, "yes"), leg(MC_11, MC_EV, "no"),
        leg("KXWCTOTAL-26JUL10ESPBEL-3", tot_ev, "yes"),
    )
    rel = classify_legs(legs, MappingProvider({MC_EV: False, tot_ev: False}))
    assert rel.kind is RelationshipKind.UNKNOWN


def test_band_with_cross_game_companion_is_priced() -> None:
    tot_ev = "KXWCTOTAL-26JUL11ARGSUI"
    legs = (
        leg(MC_8, MC_EV, "yes"), leg(MC_11, MC_EV, "no"),
        leg("KXWCTOTAL-26JUL11ARGSUI-3", tot_ev, "yes"),
    )
    rel = classify_legs(legs, MappingProvider({MC_EV: False, tot_ev: False}))
    assert rel.kind is RelationshipKind.NESTED_BAND
    assert rel.bands == ((0, 1),)


def test_three_rung_shape_is_not_quoted_as_band() -> None:
    """yes-7 + no-10 + no-11 in one game: a band plus a redundant no-rung —
    unmodeled (the no/no pair is containment inside a 3-leg combo) → UNKNOWN."""
    legs = (
        leg(MC2_7, MC2_EV, "yes"), leg(MC2_10, MC2_EV, "no"),
        leg("KXWCCORNERS-26JUL11ARGSUI-11", MC2_EV, "no"),
    )
    rel = classify_legs(legs, MappingProvider({MC2_EV: False}))
    assert rel.kind is RelationshipKind.UNKNOWN


# --- Family 1: 1H-BTTS ⟹ FT-BTTS, the NO/NO containment added ---------------------
# (yes/yes containment + yes/no impossible are covered above.)


def test_1h_btts_no_ft_btts_no_is_containment_subset_is_ft_leg() -> None:
    """{1H-BTTS no, FT-BTTS no}: ¬(FT-BTTS) ⟹ ¬(1H-BTTS), so the FT-BTTS-no leg is
    the effective subset — joint = P(FT-BTTS no). containment points at the FT leg
    (index 1) as subset, the 1H leg (index 0) as superset."""
    legs = (leg(FH_BTTS, FH_BTTS_EV, "no"), leg(FT_BTTS, FT_BTTS_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (1, 0)


def test_1h_btts_no_ft_btts_yes_is_possible() -> None:
    """{1H-BTTS no, FT-BTTS yes}: both teams scored, just not both by half-time —
    a possible combo, no logical pin (groups for the copula)."""
    legs = (leg(FH_BTTS, FH_BTTS_EV, "no"), leg(FT_BTTS, FT_BTTS_EV, "yes"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)


def test_1h_btts_no_ft_btts_no_in_larger_combo_is_unknown() -> None:
    """The no/no containment obeys the same bare-2-leg policy: buried in a >2-leg
    combo it is not modeled → UNKNOWN (widen-or-no-quote)."""
    legs = (
        leg(FH_BTTS, FH_BTTS_EV, "no"),
        leg(FT_BTTS, FT_BTTS_EV, "no"),
        leg("KXWCTOTAL-26JUL05MEXENG-3", "KXWCTOTAL-26JUL05MEXENG", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN


# --- Family 2: regulation moneyline team-WIN ⟹ FT Over-0.5 ------------------------
# Encoding (structural.py: DOC-VERIFIED live metadata): KXWCTOTAL-…-N = "over
# N-0.5", so suffix -1 = over 0.5. KXWCGAME = regulation moneyline (team win needs
# a goal); KXWCADVANCE (advance incl pens) is a different LegType and excluded.

ML_MEX = "KXWCGAME-26JUL05MEXENG-MEX"
ML_TIE = "KXWCGAME-26JUL05MEXENG-TIE"
ML_EV = "KXWCGAME-26JUL05MEXENG"
ADV_MEX = "KXWCADVANCE-26JUL05MEXENG-MEX"
ADV_EV = "KXWCADVANCE-26JUL05MEXENG"
TOT_OVER05 = "KXWCTOTAL-26JUL05MEXENG-1"   # over 0.5 (>=1 goal)
TOT_OVER15 = "KXWCTOTAL-26JUL05MEXENG-2"   # over 1.5 (>=2 goals)
TOT_EV = "KXWCTOTAL-26JUL05MEXENG"


def test_win_yes_over05_no_is_impossible() -> None:
    """A regulation team win needs a goal, so win yes × over-0.5 no is impossible."""
    legs = (leg(ML_MEX, ML_EV, "yes"), leg(TOT_OVER05, TOT_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is True  # airtight scoring tautology


def test_win_yes_over05_yes_is_containment_subset_is_moneyline() -> None:
    """win ⊂ over-0.5, so joint = P(win); subset points at the moneyline leg (0)."""
    legs = (leg(ML_MEX, ML_EV, "yes"), leg(TOT_OVER05, TOT_EV, "yes"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (0, 1)


def test_win_yes_over15_no_is_possible_line_precision() -> None:
    """LINE PRECISION: a win implies over-0.5, NOT over-1.5 (a 1-0 win is under
    1.5). win yes × over-1.5 no stays a possible combo → OK."""
    legs = (leg(ML_MEX, ML_EV, "yes"), leg(TOT_OVER15, TOT_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)


def test_tie_yes_over05_no_is_possible_not_a_team() -> None:
    """A 0-0 draw is under-0.5, so a TIE leg does NOT imply a goal → OK, never
    impossible."""
    legs = (leg(ML_TIE, ML_EV, "yes"), leg(TOT_OVER05, TOT_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK


def test_advance_yes_over05_no_is_possible_not_moneyline() -> None:
    """ADVANCE (0-0 on penalties advances) does NOT imply a goal — it is a
    different LegType and never triggers the win⟹over-0.5 pin → OK."""
    legs = (leg(ADV_MEX, ADV_EV, "yes"), leg(TOT_OVER05, TOT_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK


def test_win_no_over05_no_is_possible_moneyline_no_unreachable() -> None:
    """Moneyline-NO legs are UNREACHABLE (Kalshi blocks them in combos) and are
    NOT added — win no × over-0.5 no falls to the copula, never a pin."""
    legs = (leg(ML_MEX, ML_EV, "no"), leg(TOT_OVER05, TOT_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK


def test_win_yes_over05_yes_in_larger_combo_is_unknown() -> None:
    """Family-2 containment buried in a >2-leg combo → UNKNOWN (bare-pair policy)."""
    legs = (
        leg(ML_MEX, ML_EV, "yes"),
        leg(TOT_OVER05, TOT_EV, "yes"),
        leg("KXWCBTTS-26JUL05MEXENG-BTTS", "KXWCBTTS-26JUL05MEXENG", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN


# --- Family 3: 1H Over-N ⟹ FT Over-N (SAME line N) --------------------------------

FH_TOT2 = "KXWC1HTOTAL-26JUL05MEXENG-2"   # 1H over 1.5
FH_TOT1 = "KXWC1HTOTAL-26JUL05MEXENG-1"   # 1H over 0.5
FH_TOT_EV = "KXWC1HTOTAL-26JUL05MEXENG"
FT_TOT2 = "KXWCTOTAL-26JUL05MEXENG-2"     # FT over 1.5
FT_TOT2_EV = "KXWCTOTAL-26JUL05MEXENG"


def test_1h_over_yes_ft_over_no_same_line_is_impossible() -> None:
    """FT goals ≥ 1H goals, so 1H-over-N yes × FT-over-N no is impossible."""
    legs = (leg(FH_TOT2, FH_TOT_EV, "yes"), leg(FT_TOT2, FT_TOT2_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is True  # airtight scoring tautology


def test_1h_over_yes_ft_over_yes_same_line_is_containment_subset_is_1h() -> None:
    """1H-over-N ⊂ FT-over-N, so joint = P(1H); subset points at the 1H leg (0)."""
    legs = (leg(FH_TOT2, FH_TOT_EV, "yes"), leg(FT_TOT2, FT_TOT2_EV, "yes"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (0, 1)


def test_1h_over_no_ft_over_no_same_line_is_containment_subset_is_ft() -> None:
    """{1H-over-N no, FT-over-N no}: ¬(FT-over-N) ⟹ ¬(1H-over-N), so joint =
    P(FT-over-N no); subset points at the FT leg (1)."""
    legs = (leg(FH_TOT2, FH_TOT_EV, "no"), leg(FT_TOT2, FT_TOT2_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.CONTAINMENT
    assert rel.containment == (1, 0)


def test_1h_over_no_ft_over_yes_same_line_is_possible() -> None:
    """{1H-over-N no, FT-over-N yes}: the goals came in the second half — possible,
    no pin → OK."""
    legs = (leg(FH_TOT2, FH_TOT_EV, "no"), leg(FT_TOT2, FT_TOT2_EV, "yes"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)


def test_1h_over_ft_over_cross_line_is_possible() -> None:
    """CROSS-LINE: 1H-over-0.5 yes × FT-over-1.5 no is possible (1H had 1 goal, the
    match ends 1-0, under 1.5). Different lines are UNREACHABLE + not directional
    here → stays OK, never impossible."""
    legs = (leg(FH_TOT1, FH_TOT_EV, "yes"), leg(FT_TOT2, FT_TOT2_EV, "no"))
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK


def test_1h_over_ft_over_same_line_in_larger_combo_is_unknown() -> None:
    """Family-3 containment buried in a >2-leg combo → UNKNOWN (bare-pair policy)."""
    legs = (
        leg(FH_TOT2, FH_TOT_EV, "yes"),
        leg(FT_TOT2, FT_TOT2_EV, "yes"),
        leg("KXWCBTTS-26JUL05MEXENG-BTTS", "KXWCBTTS-26JUL05MEXENG", "yes"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.UNKNOWN


def test_1h_over_ft_over_different_game_is_possible() -> None:
    """Same-line 1H × FT totals of DIFFERENT games carry no logical relation."""
    legs = (
        leg(FH_TOT2, FH_TOT_EV, "yes"),
        leg("KXWCTOTAL-26JUL06ARGEGY-2", "KXWCTOTAL-26JUL06ARGEGY", "no"),
    )
    rel = classify_legs(legs, ExplodingProvider())
    assert rel.kind is RelationshipKind.OK
