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


# --- MLB player props + RFI (promoted from docs/calibration/staged_mlb_props.md) ---


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        # SOURCE OF TRUTH (prod RFQ tape + Kalshi API, 2026-07-09): line suffix
        # -N means N+ (floor_strike = N-0.5), NOT the TOTAL/SPREAD over-line.
        ("KXMLBKS-26JUL092145COLSF-SFCWHISENHUNT88-8", LegType.PLAYER_KS),
        ("KXMLBHIT-26JUL091840ATHDET-ATHZGELOF20-3", LegType.PLAYER_HIT),
        ("KXMLBHR-26JUL092005LAATEX-TEXWLANGFORD36-1", LegType.PLAYER_HR),
        ("KXMLBHRR-26JUL092005LAATEX-TEXWLANGFORD36-5", LegType.PLAYER_HRR),
        ("KXMLBTB-26JUL092005LAATEX-TEXWLANGFORD36-5", LegType.PLAYER_TB),
        # RFI has NO outcome suffix: KXMLBRFI-<gamecode> is the full ticker.
        ("KXMLBRFI-26JUL121605COLSF", LegType.RFI),
    ],
)
def test_mlb_player_prop_families_classify(ticker: str, expected: LegType) -> None:
    assert classify_leg(ticker) is expected


def test_mlb_hrr_keyword_precedes_hr() -> None:
    # 'MLBHRR' contains 'MLBHR', so keyword order is load-bearing: KXMLBHRR is
    # combined hits+runs+RBIs (MLBHITSRUNSRBIS.pdf) — NOT a home-run market,
    # despite identical ticker grammar to KXMLBHR on the same players.
    ticker = "KXMLBHRR-26JUL092005LAATEX-TEXWLANGFORD36-5"
    assert classify_leg(ticker) is LegType.PLAYER_HRR
    assert classify_leg(ticker) is not LegType.PLAYER_HR


@pytest.mark.parametrize(
    "ticker",
    [
        # Superstring traps around the MLB-anchored prop keywords: each carries
        # an explicit UNKNOWN blocker entry (widen, never masquerade).
        "KXLEADERMLBHR-26-XXX",       # season leaders (contains 'MLBHR')
        "KXMLBHRDERBY-26-XXX",        # HR derby (contains 'MLBHR')
        "KXMLBSERIESGAMETOTAL-26JUL09NYYTB-3",
        "KXMLBF5TOTAL-26JUL091840NYYTB-4",
        "KXMLBF5SPREAD-26JUL091840NYYTB-NYY2",
    ],
)
def test_mlb_blocker_series_are_unknown(ticker: str) -> None:
    assert classify_leg(ticker) is LegType.UNKNOWN


def test_mlb_blockers_fix_live_total_and_spread_misclassification() -> None:
    # Regression for two LIVE misclassification bugs (latent — families not
    # combo-eligible today): KXMLBF5TOTAL/KXMLBF5SPREAD settle on a FIRST-5-
    # INNINGS window ('F5' evades _PERIOD_SERIES) and KXMLBSERIESGAMETOTAL is a
    # series game COUNT — all three used to type as full-game TOTAL/SPREAD.
    assert classify_leg("KXMLBSERIESGAMETOTAL-26JUL09NYYTB-3") is not LegType.TOTAL
    assert classify_leg("KXMLBF5TOTAL-26JUL091840NYYTB-4") is not LegType.TOTAL
    assert classify_leg("KXMLBF5SPREAD-26JUL091840NYYTB-NYY2") is not LegType.SPREAD


@pytest.mark.parametrize(
    "ticker",
    [
        # SOURCE OF TRUTH (11,305-series universe scan, 2026-07-09): bare
        # HR/KS/HIT/TB/RFI keywords would hit 64/67/9/128/10 foreign series —
        # exactly why every prop keyword is MLB-anchored. None of these may
        # ever classify as an MLB prop type.
        "KXANTHROPICRISK-26-YES",     # contains 'HR'
        "KXLEADERNFLSACKS-26-XXX",    # contains 'KS'
        "KXDANAWHITEFB-26-YES",       # contains 'HIT'
        "KXBILBASKETBALL-26-YES",     # contains 'TB'
        "KXSINNERFINISH-26-XXX",      # contains 'RFI'
    ],
)
def test_substring_collisions_never_type_as_mlb_props(ticker: str) -> None:
    mlb_prop_types = {
        LegType.PLAYER_HR,
        LegType.PLAYER_HIT,
        LegType.PLAYER_KS,
        LegType.PLAYER_TB,
        LegType.PLAYER_HRR,
        LegType.RFI,
    }
    assert classify_leg(ticker) not in mlb_prop_types
    assert classify_leg(ticker) is LegType.UNKNOWN


def test_wbc_and_kbo_props_stay_unknown() -> None:
    # WBC/KBO prop families are intentionally unmapped (dormant, widen-safe):
    # the keywords are MLB-anchored, so these fall through to UNKNOWN.
    assert classify_leg("KXWBCHIT-26MAR10JPNUSA-USAPLAYER1-2") is LegType.UNKNOWN
    assert classify_leg("KXKBORFI-26JUL10LGDOO") is LegType.UNKNOWN


@pytest.mark.parametrize(
    "ticker",
    [
        "KXMLBKS-26JUL092145COLSF-SFCWHISENHUNT88-8",
        "KXMLBHIT-26JUL091840ATHDET-ATHZGELOF20-3",
        "KXMLBHR-26JUL092005LAATEX-TEXWLANGFORD36-1",
        "KXMLBHRR-26JUL092005LAATEX-TEXWLANGFORD36-5",
        "KXMLBTB-26JUL092005LAATEX-TEXWLANGFORD36-5",
        "KXMLBRFI-26JUL121605COLSF",
    ],
)
def test_mlb_prop_families_classify_sport_mlb(ticker: str) -> None:
    # The sport-scoped pair table lookup keys on classify_sport: all 6 prop
    # families must stay Sport.MLB so 'mlb:'-prefixed entries can attach.
    assert classify_sport(ticker) is Sport.MLB


def test_rfi_is_deliberately_not_period_flagged() -> None:
    # RFI settles on a first-inning window but is deliberately NOT period-
    # flagged: its own LegType carries the window (unlike 1H/2H/quarter series,
    # which must be gated out of the full-game structural inverter).
    assert classify_leg("KXMLBRFI-26JUL121605COLSF") is LegType.RFI
    assert not is_period_leg("KXMLBRFI-26JUL121605COLSF")


def test_mlb_full_game_families_unchanged_by_prop_keywords() -> None:
    # Regression: the MLB-anchored prop keywords + blockers must not disturb
    # the verified-existing full-game MLB families.
    assert classify_leg("KXMLBGAME-26JUL081840NYYTB-NYY") is LegType.MONEYLINE
    assert classify_leg("KXMLBTOTAL-26JUL081840NYYTB-4") is LegType.TOTAL
    assert classify_leg("KXMLBSPREAD-26JUL081840NYYTB-NYY2") is LegType.SPREAD
    assert classify_leg("KXMLBEXTRAS-26JUL081840NYYTB-EXTRAS") is LegType.EXTRAS


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
        # 1H spread (KXWC1HSPREAD, …-<TEAM><line>) is now a calibrated family:
        # its own type, never masquerading as a full-game SPREAD.
        ("KXWC1HSPREAD-26JUL07ARGEGY-ARG2", LegType.FIRST_HALF_SPREAD),
    ],
)
def test_first_half_families_get_their_own_type(ticker: str, expected: LegType) -> None:
    assert classify_leg(ticker) is expected


@pytest.mark.parametrize(
    "ticker",
    [
        # SOURCE OF TRUTH (prod RFQ tape 2026-07-07): TEAM+line-digit suffix, only
        # line 2 traded (…-<TEAM>2 = "leads at half by over 1.5", 1H margin>=2).
        "KXWC1HSPREAD-26JUL09FRAMAR-FRA2",
        "KXWC1HSPREAD-26JUL02ESPAUT-AUT2",
    ],
)
def test_first_half_spread_types_and_is_period(ticker: str) -> None:
    # A first-half market: its own type AND a period leg (the structural inverter
    # must keep declining it, like every other 1H family).
    assert classify_leg(ticker) is LegType.FIRST_HALF_SPREAD
    assert is_period_leg(ticker)


def test_full_game_spread_unchanged_by_first_half_spread() -> None:
    # Regression: KXWC1HSPREAD must match the first-half-spread form BEFORE the
    # full-game SPREAD; the full-game series (no 1H token) still types SPREAD and
    # is NOT a period leg.
    assert classify_leg("KXWCSPREAD-26JUL02ESPAUT-AUT2") is LegType.SPREAD
    assert classify_leg("KXWCSPREAD-26JUL03ARGCPV-ARG3") is LegType.SPREAD
    assert classify_leg("KXNFLSPREAD-26SEP10KCBAL-KC3") is LegType.SPREAD
    assert not is_period_leg("KXWCSPREAD-26JUL02ESPAUT-AUT2")


def test_second_half_spread_stays_unknown_never_full_game() -> None:
    # Only the FIRST half is modeled; a 2H spread is a period leg we have not
    # measured, so it must widen (UNKNOWN), never masquerade as a full-game
    # SPREAD or the first-half spread.
    assert classify_leg("KXWC2HSPREAD-26JUL05MEXENG-MEX2") is LegType.UNKNOWN


def test_first_half_spread_pair_keys_match_config() -> None:
    assert pair_key(LegType.FIRST_HALF_SPREAD, LegType.SPREAD) == (
        "first_half_spread|spread"
    )
    assert pair_key(LegType.FIRST_HALF_SPREAD, LegType.MONEYLINE) == (
        "first_half_spread|moneyline"
    )
    assert pair_key(LegType.FIRST_HALF_SPREAD, LegType.TOTAL) == "first_half_spread|total"


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
