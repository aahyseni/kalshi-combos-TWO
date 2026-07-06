"""Tests for pricing/legtypes.py: structural leg typing from ticker series prefixes."""

from __future__ import annotations

import pytest

from combomaker.pricing.legtypes import LegType, classify_leg, pair_key

# --- classify_leg on real production tickers --------------------------------


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("KXWCGOAL-26JUL05MEXENG-ENGHKANE9-1", LegType.PLAYER_GOAL),
        ("KXMLBGAME-26JUL081840NYYTB-NYY", LegType.MONEYLINE),
        ("KXUFCFIGHT-26JUL11MCGHOL-HOL", LegType.MONEYLINE),
        ("KXWCTOTAL-26JUL05MEXENG-3", LegType.TOTAL),
        ("KXWCBTTS-26JUL05MEXENG-BTTS", LegType.BTTS),
        ("KXWCCORNERS-26JUL05MEXENG-10", LegType.CORNERS),
        ("KXWCADVANCE-26JUL05MEXENG-POR", LegType.ADVANCE),
        ("KXMLBEXTRAS-26JUL081840NYYTB-EXTRAS", LegType.EXTRAS),
        ("KXUCLGAME-26SEP16PSGRMA-PSG", LegType.MONEYLINE),
        ("KXWNBAGAME-26JUL06NYLLVA-NYL", LegType.MONEYLINE),
        ("KXBRASILEIROBGAME-26JUL06GOIAVA-GOI", LegType.MONEYLINE),
    ],
)
def test_classify_real_production_tickers(ticker: str, expected: LegType) -> None:
    assert classify_leg(ticker) is expected


def test_lowercase_input_is_classified_identically() -> None:
    assert classify_leg("kxwcgoal-26jul05mexeng-enghkane9-1") is LegType.PLAYER_GOAL
    assert classify_leg("kxmlbgame-26jul081840nyytb-nyy") is LegType.MONEYLINE
    assert classify_leg("kxwctotal-26jul05mexeng-3") is LegType.TOTAL


def test_unrecognized_series_is_unknown() -> None:
    # Non-sports / exotic series must fall through to UNKNOWN, never a
    # convenient default (quiet-failure defense #2).
    assert classify_leg("KXHIGHNY-26JUL06-B90") is LegType.UNKNOWN


def test_empty_ticker_is_unknown() -> None:
    assert classify_leg("") is LegType.UNKNOWN


def test_only_series_prefix_is_inspected() -> None:
    # Keywords after the first "-" (date/outcome segments) must not classify:
    # the series prefix alone encodes the market structure.
    assert classify_leg("KXMYSTERY-26JUL05GAME-GOAL") is LegType.UNKNOWN


# --- keyword ordering --------------------------------------------------------


def test_keyword_order_goal_beats_game_regardless_of_position() -> None:
    # _KEYWORDS is scanned in declaration order (GOAL before GAME), so a series
    # containing both substrings resolves to PLAYER_GOAL — even when GAME
    # appears first inside the series string.
    assert classify_leg("KXGOALGAME-26JUL05-X") is LegType.PLAYER_GOAL
    assert classify_leg("KXGAMEGOAL-26JUL05-X") is LegType.PLAYER_GOAL


def test_keyword_order_total_beats_game() -> None:
    # TOTAL precedes GAME in the table; pin the declared precedence.
    assert classify_leg("KXGAMETOTAL-26JUL05-3") is LegType.TOTAL


# --- pair_key -----------------------------------------------------------------


def test_pair_key_is_order_independent_for_btts_total() -> None:
    assert pair_key(LegType.TOTAL, LegType.BTTS) == "btts|total"
    assert pair_key(LegType.BTTS, LegType.TOTAL) == "btts|total"


def test_pair_key_same_type_pair() -> None:
    assert pair_key(LegType.MONEYLINE, LegType.MONEYLINE) == "moneyline|moneyline"


def test_pair_key_uses_string_values() -> None:
    assert pair_key(LegType.UNKNOWN, LegType.TOTAL) == "total|unknown"
    assert pair_key(LegType.PLAYER_GOAL, LegType.BTTS) == "btts|player_goal"


def test_pair_key_order_independent_for_every_type_pair() -> None:
    for a in LegType:
        for b in LegType:
            assert pair_key(a, b) == pair_key(b, a)
