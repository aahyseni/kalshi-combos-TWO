"""Tests for pricing/legtypes.py: structural leg typing from ticker series prefixes."""

from __future__ import annotations

import pytest

from combomaker.pricing.legtypes import (
    LegType,
    Sport,
    classify_leg,
    classify_sport,
    is_period_leg,
    pair_key,
)

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


def test_team_corners_sub_typed_from_total_corners() -> None:
    # SOURCE OF TRUTH (prod RFQ tape 2026-07-07): team corners are a DISTINCT
    # series KXWCTCORNERS (…-<TEAM>N) vs total corners KXWCCORNERS (…-N). The
    # TCORNERS keyword is matched before CORNERS (it contains it).
    assert classify_leg("KXWCTCORNERS-26JUL07SUICOL-COL5") is LegType.CORNERS_TEAM
    assert classify_leg("KXWCCORNERS-26JUL05MEXENG-10") is LegType.CORNERS


def test_team_total_sub_typed_from_game_total() -> None:
    # SOURCE OF TRUTH (prod RFQ tape + Kalshi API): a single team's total is a
    # DISTINCT series KX<SPORT>TEAMTOTAL (…-<TEAM>N) vs the game TOTAL (…-N).
    # "TEAMTOTAL" contains "TOTAL", so the keyword is matched first; without it
    # these mis-type as a game TOTAL and would price on the game-total grid.
    assert classify_leg("KXNFLTEAMTOTAL-26JUL08SEAKC-SEA24") is LegType.TEAM_TOTAL
    assert classify_leg("KXNBATEAMTOTAL-26OCT10SASLAL-SAS124") is LegType.TEAM_TOTAL


def test_game_total_still_types_total_not_team_total() -> None:
    # Regression: adding TEAMTOTAL must not disturb plain game TOTAL markets.
    assert classify_leg("KXWNBATOTAL-26JUL06NYLLVA-196") is LegType.TOTAL
    assert classify_leg("KXMLBTOTAL-26JUL081840NYYTB-4") is LegType.TOTAL


# --- tennis (match winner) ---------------------------------------------------


@pytest.mark.parametrize(
    "ticker",
    [
        "KXATPMATCH-26JUL08FRIZVE-ZVE",
        "KXWTAMATCH-26JUL08SWIGAU-SWI",
        "KXATPCHALLENGERMATCH-26JUL08FOOBAR-FOO",
        "KXWTACHALLENGERMATCH-26JUL08FOOBAR-BAR",
    ],
)
def test_tennis_match_is_sport_tennis_and_moneyline(ticker: str) -> None:
    # Tennis match-winner series were UNKNOWN sport + UNKNOWN leg before. The
    # match winner is a moneyline; typing it lets any FUTURE same-match tennis
    # pair be detected instead of silently defaulting.
    assert classify_sport(ticker) is Sport.TENNIS
    assert classify_leg(ticker) is LegType.MONEYLINE


# --- first-half (period) awareness -------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        # SOURCE OF TRUTH (prod RFQ tape 2026-07-07): the 1H winner series is the
        # BARE ``KXWC1H`` (…-<TEAM|TIE>), NOT ``KXWC1HGAME`` (which does not
        # exist). It carries no family keyword, so it's the 1H moneyline.
        ("KXWC1H-26JUL07ARGEGY-ARG", LegType.FIRST_HALF_MONEYLINE),
        ("KXWC1H-26JUL07ARGEGY-TIE", LegType.FIRST_HALF_MONEYLINE),
        ("KXWC1HTOTAL-26JUL05MEXENG-2", LegType.FIRST_HALF_TOTAL),
        ("KXWC1HBTTS-26JUL05MEXENG-BTTS", LegType.FIRST_HALF_BTTS),
        ("KXWCFHTOTAL-26JUL05MEXENG-1", LegType.FIRST_HALF_TOTAL),  # FH alias
        # 1H spread is a real family (KXWC1HSPREAD) but unmeasured -> UNKNOWN,
        # never guessed, never a full-game spread.
        ("KXWC1HSPREAD-26JUL07ARGEGY-ARG2", LegType.UNKNOWN),
    ],
)
def test_first_half_families_get_their_own_type(ticker: str, expected: LegType) -> None:
    assert classify_leg(ticker) is expected


def test_first_half_total_is_not_reported_as_full_game_total() -> None:
    # The wrong-settlement-window bug this prevents: a 1H total priced on the
    # full-game grid / correlated as a full-game total.
    assert classify_leg("KXWC1HTOTAL-26JUL05MEXENG-2") is not LegType.TOTAL


def test_unmodeled_period_is_unknown_never_full_game() -> None:
    # 2nd-half / quarter markets are period legs we have not measured — they
    # must classify UNKNOWN (widen), never masquerade as a full-game type.
    assert classify_leg("KXWC2HTOTAL-26JUL05MEXENG-1") is LegType.UNKNOWN
    assert classify_leg("KXNBA1QTOTAL-26OCT10LALBOS-60") is LegType.UNKNOWN


def test_full_game_classification_unchanged_by_period_awareness() -> None:
    assert classify_leg("KXWCTOTAL-26JUL05MEXENG-3") is LegType.TOTAL
    assert classify_leg("KXWCGAME-26JUL05MEXENG-MEX") is LegType.MONEYLINE
    assert classify_leg("KXWCBTTS-26JUL05MEXENG-BTTS") is LegType.BTTS


def test_is_period_leg_flags_only_period_series() -> None:
    assert is_period_leg("KXWC1HTOTAL-26JUL05MEXENG-2")
    assert is_period_leg("KXWC2HGAME-26JUL05MEXENG-MEX")
    assert not is_period_leg("KXWCTOTAL-26JUL05MEXENG-3")
    assert not is_period_leg("KXWCGAME-26JUL05MEXENG-MEX")


def test_first_half_pair_keys_match_config() -> None:
    assert pair_key(LegType.FIRST_HALF_MONEYLINE, LegType.MONEYLINE) == (
        "first_half_moneyline|moneyline"
    )
    assert pair_key(LegType.FIRST_HALF_TOTAL, LegType.TOTAL) == "first_half_total|total"
    assert pair_key(LegType.FIRST_HALF_BTTS, LegType.BTTS) == "btts|first_half_btts"


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


# --- broad regression: the TEAMTOTAL/MATCH/ATP/WTA additions changed nothing --


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("KXWCGAME-26JUL05MEXENG-MEX", LegType.MONEYLINE),
        ("KXUFCFIGHT-26JUL11MCGHOL-HOL", LegType.MONEYLINE),
        ("KXMLBGAME-26JUL081840NYYTB-NYY", LegType.MONEYLINE),
        ("KXWNBAGAME-26JUL06NYLLVA-NYL", LegType.MONEYLINE),
        ("KXWCTOTAL-26JUL05MEXENG-3", LegType.TOTAL),
        ("KXWCTCORNERS-26JUL07SUICOL-COL5", LegType.CORNERS_TEAM),
        ("KXWCCORNERS-26JUL05MEXENG-10", LegType.CORNERS),
        ("KXWCGOAL-26JUL05MEXENG-ENGHKANE9-1", LegType.PLAYER_GOAL),
        ("KXWCBTTS-26JUL05MEXENG-BTTS", LegType.BTTS),
    ],
)
def test_existing_leg_classifications_unchanged(ticker: str, expected: LegType) -> None:
    assert classify_leg(ticker) is expected


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("KXWCGAME-26JUL05MEXENG-MEX", Sport.SOCCER),
        ("KXMLBGAME-26JUL081840NYYTB-NYY", Sport.MLB),
        ("KXWNBAGAME-26JUL06NYLLVA-NYL", Sport.WNBA),
        ("KXNBATEAMTOTAL-26OCT10SASLAL-SAS124", Sport.NBA),
        ("KXNFLTEAMTOTAL-26JUL08SEAKC-SEA24", Sport.NFL),
        ("KXUFCFIGHT-26JUL11MCGHOL-HOL", Sport.UFC),
        ("KXHIGHNY-26JUL06-B90", Sport.UNKNOWN),
    ],
)
def test_existing_sport_classifications_unchanged(ticker: str, expected: Sport) -> None:
    # The ATP/WTA keywords must not steal any existing sport's series (no
    # "ATP"/"WTA" substring appears in WNBA/NBA/MLB/NFL/NHL/UFC/WC/… prefixes).
    assert classify_sport(ticker) is expected
