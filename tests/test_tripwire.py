"""Taxonomy-impossible constructibility tripwire (FIX-4, V3 robustness
§2.4-1, judge-mandated 2026-07-11).

The 2026-07-11 containment probe left 30 semantically-IMPOSSIBLE shape ×
side-mix cells that are exchange-BLOCKED today but would PRICE (copula /
flat fallback) if Kalshi's validator silently loosened. The fixture
``tests/fixtures/ground_truth/taxonomy_impossible.json`` pins them; the
classifier declines any same-game pair matching a pin as IMPOSSIBLE
``farmable=False`` with the dedicated countable note
(``taxonomy-impossible tripwire: <shape>``) — never a copula price, never a
farm (fixture-driven certainty is not an airtight in-code proof).

Covered here: the V3 tier-1 soccer cells (S19-nn, S20-nn, S24-yn, S27-yn),
representative rows of the rest of the dangerous class across every matcher
species (lines, teams, entities, ladders, series-matched UNKNOWN-typed legs),
shipped-family precedence, possible-mix non-interference, the engine
boundary (no-quote + never farmed), and the fail-closed fixture handling
(missing/corrupt ⇒ inert + warning, existing behavior unchanged).

UNKNOWN-typed legs (UFC/golf/WNBA-PTS): verified 2026-07-11 that the LIVE
path does NOT decline them — ``rfq/filters.py`` gates only the collection
ticker (``RfqFilter.evaluate``, collection_whitelist branch) and
``sgp.build_sgp_correlation`` prices UNKNOWN-typed same-game pairs at the
flat prior (the ``types[i] is LegType.UNKNOWN`` branch) — so the tripwire
matches them by SERIES (tape-verified ticker shapes), not LegType.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from combomaker.core.reasons import ReasonCode
from combomaker.pricing import tripwire
from combomaker.pricing.quote import NoQuote
from combomaker.pricing.relationships import (
    Relationship,
    RelationshipKind,
    classify_legs,
)
from combomaker.pricing.tripwire import (
    TripwireFixtureError,
    load_cells,
    taxonomy_impossible,
)
from combomaker.rfq.models import RfqLeg
from tests.test_containment_collapse import engine_with, rfq_of
from tests.test_relationships import ExplodingProvider, MappingProvider, leg

# Real prod-convention tickers (taxonomy examples + tape shapes, 2026-07-11).
_G = "26JUL14FRAESP"
TOT1 = f"KXWCTOTAL-{_G}-1"
TOT2 = f"KXWCTOTAL-{_G}-2"
ML_TIE = f"KXWCGAME-{_G}-TIE"
ML_FRA = f"KXWCGAME-{_G}-FRA"
ML_ESP = f"KXWCGAME-{_G}-ESP"
SP_FRA2 = f"KXWCSPREAD-{_G}-FRA2"
FH_TOT1 = f"KXWC1HTOTAL-{_G}-1"
FH_TOT2 = f"KXWC1HTOTAL-{_G}-2"
FH_TIE = f"KXWC1H-{_G}-TIE"
NOGOAL = f"KXWCFIRSTGOAL-{_G}-NOGOAL"
FIRSTGOAL_P = "KXWCFIRSTGOAL-26JUL10ESPBEL-ESPLYAMAL10"
GOAL1_P = "KXWCGOAL-26JUL10ESPBEL-ESPLYAMAL10-1"
GOAL2_P = "KXWCGOAL-26JUL10ESPBEL-ESPLYAMAL10-2"
TC_BEL4 = "KXWCTCORNERS-26JUL10ESPBEL-BEL4"
MC_4 = "KXWCCORNERS-26JUL10ESPBEL-4"
MC_8 = "KXWCCORNERS-26JUL10ESPBEL-8"
W_SP = "KXWNBASPREAD-26JUL10GSCONN-GS13"
W_ML_GS = "KXWNBAGAME-26JUL10GSCONN-GS"
W_ML_CONN = "KXWNBAGAME-26JUL10GSCONN-CONN"
PTS15 = "KXWNBAPTS-26JUL11NYMIN-MINOMILES5-15"
PTS10 = "KXWNBAPTS-26JUL11NYMIN-MINOMILES5-10"
UFC_R2 = "KXUFCROUNDS-26JUL11STEELL-2"
UFC_R3 = "KXUFCROUNDS-26JUL11STEELL-3"
UFC_DRAW = "KXUFCMOF-26JUL11MCGHOL-DRAW"
UFC_WIN = "KXUFCFIGHT-26JUL11MCGHOL-HOL"
GOLF_TOUR = "KXPGATOUR-ISC26-JWOL"
GOLF_CUT = "KXPGAMAKECUT-ISC26-JWOL"


def _ev(ticker: str) -> str:
    return "-".join(ticker.split("-")[:2])


def _leg(mt: str, side: str = "yes") -> RfqLeg:
    return leg(mt, _ev(mt), side)


def _assert_tripped(rel: Relationship, shape: str) -> None:
    assert rel.kind is RelationshipKind.IMPOSSIBLE, rel
    assert rel.farmable is False  # decline-only, never a farm
    assert any(
        f"taxonomy-impossible tripwire: {shape}" in n for n in rel.notes
    ), rel.notes


# --- the shipped fixture itself ------------------------------------------------------


def test_shipped_fixture_loads_and_covers_the_dangerous_class() -> None:
    """The repo fixture parses cleanly and pins (at least) the tier-1 cells
    plus every dangerous-class family the matcher can express."""
    cells = load_cells()
    shapes = {cell.shape for cell in cells}
    assert {"S19", "S20", "S24", "S27"} <= shapes  # V3 tier-1
    assert {
        "S3L", "S4", "S5", "S9", "S10", "S11", "S14", "S15", "S16", "S17",
        "S18", "S21", "S22", "S26", "S28", "S29", "S32", "S42", "S44", "S45",
        "S46", "S47", "S48", "S50",
    } <= shapes
    assert "S49" not in shapes  # cross-scope: documented residual, never a guess
    assert len(cells) >= 40


# --- V3 tier-1 soccer cells ----------------------------------------------------------


def test_s19_under05_draw_no_no_trips() -> None:
    rel = classify_legs((_leg(TOT1, "no"), _leg(ML_TIE, "no")), ExplodingProvider())
    _assert_tripped(rel, "S19")


def test_s20_1h_under05_1h_draw_no_no_trips() -> None:
    rel = classify_legs((_leg(FH_TOT1, "no"), _leg(FH_TIE, "no")), ExplodingProvider())
    _assert_tripped(rel, "S20")


def test_s24_first_goal_yes_goal1_no_trips_same_player_only() -> None:
    rel = classify_legs(
        (_leg(FIRSTGOAL_P, "yes"), _leg(GOAL1_P, "no")), ExplodingProvider()
    )
    _assert_tripped(rel, "S24")
    # GOAL-2 no is POSSIBLE (first goal, then no second) — the line-1 gate.
    rel = classify_legs(
        (_leg(FIRSTGOAL_P, "yes"), _leg(GOAL2_P, "no")), ExplodingProvider()
    )
    assert rel.kind is RelationshipKind.OK


def test_s27_nogoal_yes_draw_no_trips() -> None:
    rel = classify_legs((_leg(NOGOAL, "yes"), _leg(ML_TIE, "no")), ExplodingProvider())
    _assert_tripped(rel, "S27")


# --- representative rows of the rest of the 30-cell dangerous class ------------------


def test_s26_s28_s29_nogoal_exclusions_trip() -> None:
    for other, shape in (
        (_leg(TOT2, "yes"), "S26"),           # any over line, not just -1
        (_leg(f"KXWCBTTS-{_G}-BTTS", "yes"), "S28"),
        (_leg(FH_TOT1, "yes"), "S29"),
        (_leg(ML_FRA, "yes"), "S29"),         # a reg team win needs a goal
    ):
        rel = classify_legs((_leg(NOGOAL, "yes"), other), ExplodingProvider())
        _assert_tripped(rel, shape)


def test_s3l_cross_line_total_pair_trips_where_family3_is_silent() -> None:
    """1H-over-1.5 yes + FT-over-0.5 no (M < N): family 3 covers equal lines
    only — the tripwire owns the cross-line cell (V3 dangerous row 23)."""
    rel = classify_legs((_leg(FH_TOT2, "yes"), _leg(TOT1, "no")), ExplodingProvider())
    _assert_tripped(rel, "S3L")


def test_s17_spread_vs_opponent_win_trips_same_team_falls_through() -> None:
    rel = classify_legs((_leg(SP_FRA2, "yes"), _leg(ML_ESP, "yes")), ExplodingProvider())
    _assert_tripped(rel, "S17")
    # Same team = S12-yy containment — owned by the shipped spread⟹win family.
    rel = classify_legs((_leg(SP_FRA2, "yes"), _leg(ML_FRA, "yes")), ExplodingProvider())
    assert rel.kind is RelationshipKind.CONTAINMENT


def test_s16_spread_vs_draw_trips() -> None:
    rel = classify_legs((_leg(SP_FRA2, "yes"), _leg(ML_TIE, "yes")), ExplodingProvider())
    _assert_tripped(rel, "S16")


def test_s32_team_corners_vs_match_corners_trips_only_m_le_n() -> None:
    rel = classify_legs((_leg(TC_BEL4, "yes"), _leg(MC_4, "no")), ExplodingProvider())
    _assert_tripped(rel, "S32")
    # M > N: team 4+ with match under 8 is possible — no claim.
    rel = classify_legs((_leg(TC_BEL4, "yes"), _leg(MC_8, "no")), ExplodingProvider())
    assert rel.kind is not RelationshipKind.IMPOSSIBLE


def test_s44_wnba_spread_win_both_orientations_trip() -> None:
    rel = classify_legs((_leg(W_SP, "yes"), _leg(W_ML_GS, "no")), ExplodingProvider())
    _assert_tripped(rel, "S44")
    rel = classify_legs((_leg(W_SP, "yes"), _leg(W_ML_CONN, "yes")), ExplodingProvider())
    _assert_tripped(rel, "S44")
    # The tape-printed WINDOW mix {cover no, win yes} stays untouched.
    rel = classify_legs((_leg(W_SP, "no"), _leg(W_ML_GS, "yes")), ExplodingProvider())
    assert rel.kind is not RelationshipKind.IMPOSSIBLE


def test_s45_wnba_pts_ladder_trips_via_series_match() -> None:
    """UNKNOWN-typed legs (WNBA PTS) match by SERIES — the live path would
    otherwise price them at the untyped flat prior (V3 dangerous row 30)."""
    provider = MappingProvider({_ev(PTS15): False})
    rel = classify_legs((_leg(PTS15, "yes"), _leg(PTS10, "no")), provider)
    _assert_tripped(rel, "S45")
    # The window mix (HIGH no, LOW yes) is possible: no claim.
    rel = classify_legs((_leg(PTS15, "no"), _leg(PTS10, "yes")), provider)
    assert rel.kind is not RelationshipKind.IMPOSSIBLE


def test_s46_ufc_rounds_ladder_trips_window_untouched() -> None:
    provider = MappingProvider({_ev(UFC_R2): False})
    rel = classify_legs((_leg(UFC_R2, "yes"), _leg(UFC_R3, "no")), provider)
    _assert_tripped(rel, "S46")
    # ends-before-3 yes + ends-before-2 no = "ends IN round 2": possible.
    rel = classify_legs((_leg(UFC_R3, "yes"), _leg(UFC_R2, "no")), provider)
    assert rel.kind is not RelationshipKind.IMPOSSIBLE


def test_s48_ufc_winner_vs_draw_trips() -> None:
    rel = classify_legs((_leg(UFC_WIN, "yes"), _leg(UFC_DRAW, "yes")), ExplodingProvider())
    _assert_tripped(rel, "S48")


def test_s50_golf_chain_trips_same_player_only_and_yy_prints_stay() -> None:
    rel = classify_legs((_leg(GOLF_TOUR, "yes"), _leg(GOLF_CUT, "no")), ExplodingProvider())
    _assert_tripped(rel, "S50")
    # Different players never match (same_entity), and the tape-printed
    # {TOP-k yes, MAKECUT yes} redundancy stays priceable.
    other = leg("KXPGAMAKECUT-ISC26-MHOM", _ev("KXPGAMAKECUT-ISC26-MHOM"), "no")
    rel = classify_legs((_leg(GOLF_TOUR, "yes"), other), ExplodingProvider())
    assert rel.kind is not RelationshipKind.IMPOSSIBLE
    rel = classify_legs((_leg(GOLF_TOUR, "yes"), _leg(GOLF_CUT, "yes")), ExplodingProvider())
    assert rel.kind is not RelationshipKind.IMPOSSIBLE


def test_s42_mlb_same_player_same_stat_ladder_trips() -> None:
    hit3 = "KXMLBHIT-26JUL092145COLSF-COLHGOODMAN15-3"
    hit1 = "KXMLBHIT-26JUL092145COLSF-COLHGOODMAN15-1"
    provider = MappingProvider({_ev(hit3): False})
    rel = classify_legs((_leg(hit3, "yes"), _leg(hit1, "no")), provider)
    _assert_tripped(rel, "S42")
    # Same-side rungs (yy) stay OK — the documented out-of-scope ladder.
    rel = classify_legs((_leg(hit1, "yes"), _leg(hit3, "yes")), provider)
    assert rel.kind is RelationshipKind.OK


# --- precedence, scope, and non-interference -----------------------------------------


def test_shipped_family_impossibles_keep_their_farmable_verdicts() -> None:
    """Family 3's equal-line cell fires BEFORE the tripwire and keeps
    farmable=True — the tripwire only backstops what the families skip."""
    rel = classify_legs((_leg(FH_TOT2, "yes"), _leg(TOT2, "no")), ExplodingProvider())
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is True
    assert not any("taxonomy-impossible tripwire" in n for n in rel.notes)


def test_cross_game_pairs_never_match() -> None:
    """Every pin is a within-game relationship: the same leg patterns across
    two DIFFERENT games carry no claim."""
    other_tie = "KXWCGAME-26JUL10ESPBEL-TIE"
    rel = classify_legs((_leg(TOT1, "no"), _leg(other_tie, "no")), ExplodingProvider())
    assert rel.kind is RelationshipKind.OK


def test_possible_mixes_of_pinned_shapes_stay_untouched() -> None:
    """The redundant/window mixes of pinned shapes never trip (S19's
    constructible no+yes and yes+yes cells, probes P18/P19)."""
    rel = classify_legs((_leg(TOT1, "no"), _leg(ML_TIE, "yes")), ExplodingProvider())
    assert rel.kind is not RelationshipKind.IMPOSSIBLE
    rel = classify_legs((_leg(TOT1, "yes"), _leg(ML_TIE, "yes")), ExplodingProvider())
    assert rel.kind is not RelationshipKind.IMPOSSIBLE


async def test_tripwire_reaches_engine_as_noquote_never_a_farm() -> None:
    """Engine boundary: a tripped combo is SKIP_LOGICALLY_IMPOSSIBLE with the
    countable note in the detail — and farmable=False means the farm gate
    (farm_impossible_combos defaults ON) never quotes the certain-NO side."""
    engine, _h = await engine_with(
        [(TOT1, "0.9000", "0.0800"), (ML_TIE, "0.2500", "0.7300")]
    )
    result = engine.price(
        rfq_of((TOT1, "no"), (ML_TIE, "no")), time_to_close_s=100_000
    )
    assert isinstance(result, NoQuote), result
    assert result.reason is ReasonCode.SKIP_LOGICALLY_IMPOSSIBLE
    assert "taxonomy-impossible tripwire: S19" in result.detail


# --- fail-closed fixture handling ----------------------------------------------------


def test_missing_fixture_raises_and_matcher_is_inert_on_empty() -> None:
    with pytest.raises(TripwireFixtureError):
        load_cells(Path("does") / "not" / "exist.json")
    legs = (_leg(TOT1, "no"), _leg(ML_TIE, "no"))
    keys = [_ev(TOT1).split("-", 1)[1], _ev(ML_TIE).split("-", 1)[1]]
    assert taxonomy_impossible(legs, keys, cells=()) is None


def test_corrupt_fixture_raises(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(TripwireFixtureError):
        load_cells(bad)
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"cells": []}), encoding="utf-8")
    with pytest.raises(TripwireFixtureError):
        load_cells(empty)
    unknown_rel = tmp_path / "rel.json"
    unknown_rel.write_text(
        json.dumps(
            {
                "cells": [
                    {
                        "shape": "SX",
                        "name": "x",
                        "sport": None,
                        "a": {"side": "yes"},
                        "b": {"side": "no"},
                        "relations": ["telepathy"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(TripwireFixtureError):
        load_cells(unknown_rel)


def test_load_failure_makes_tripwire_inert_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing/corrupt DEFAULT fixture ⇒ tripwire inert (existing behavior
    unchanged: the S19 shape classifies OK exactly as before the tripwire)
    and the one-time warning is emitted — never an exception into pricing."""
    bad = tmp_path / "taxonomy_impossible.json"
    bad.write_text("]corrupt[", encoding="utf-8")
    monkeypatch.setattr(tripwire, "DEFAULT_FIXTURE_PATH", bad)
    monkeypatch.setattr(tripwire, "_ANCHORED_FIXTURE_PATH", bad)
    monkeypatch.setattr(tripwire, "_CACHE", None)
    monkeypatch.setattr(tripwire, "_CACHE_LOADED", False)
    rel = classify_legs((_leg(TOT1, "no"), _leg(ML_TIE, "no")), ExplodingProvider())
    assert rel.kind is RelationshipKind.OK  # pre-tripwire behavior, unchanged
    assert tripwire._CACHE == ()  # cached inert after the single warning