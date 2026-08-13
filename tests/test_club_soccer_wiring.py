"""Club-soccer wiring (2026-08-13): La Liga / MLS / UECL onto the WC machinery.

Pins the four seams the wiring touched:
  1. classification — the 13 club series type + sport correctly, the
     KXLALIGAADVANCE blocker holds, specials stay UNKNOWN;
  2. markup — club legs tag sport 'soccer' (never 'other' = zero markup);
  3. farm gating — farmable=True requires EVERY leg on a farm-certain series
     (KXWC): club impossibility cells never farm (48h reschedule SCALAR rule,
     docs/calibration/club_soccer_rules_pin.md);
  4. two-legged-tie ADVANCE regime — same-game UECL advance pairs price as
     UNTYPED (flat + widened band), never the single-match KXWC advance rhos;
  5. pregame — occurrence_datetime is a third fail-closed estimate anchor
     (the game-day-created club rung whose expected_expiration is
     creation-relative, live-verified KXUECLTOTAL-26AUG12SCRPAI-7).

Exemplar tickers are REAL production tickers pulled from the live API
2026-08-12/13 (source-of-truth rule).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from combomaker.core.clock import FakeClock
from combomaker.marketdata.metadata import MarketMeta, MetadataCache
from combomaker.ops.config import CorrelationConfig, FiltersConfig
from combomaker.pricing.legtypes import LegType, Sport, classify_leg, classify_sport
from combomaker.pricing.markup import sport_of
from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.pricing.sgp import SgpParams, build_sgp_correlation
from combomaker.rfq.models import RfqLeg
from combomaker.rfq.pregame import PregameGate
from combomaker.rfq.schedule import ScheduleCache

# --- real production exemplars (live API, 2026-08-12/13) ---------------------

LALIGA_GAME = "KXLALIGAGAME-26AUG26RMARSO-TIE"
LALIGA_TOTAL = "KXLALIGATOTAL-26AUG17DEPELC-6"
LALIGA_SPREAD = "KXLALIGASPREAD-26AUG17DEPELC-ELC3"
LALIGA_BTTS = "KXLALIGABTTS-26AUG17DEPELC-BTTS"
MLS_GAME = "KXMLSGAME-26AUG23ATLSKC-TIE"
MLS_TOTAL = "KXMLSTOTAL-26AUG16SEAVAN-6"
MLS_SPREAD = "KXMLSSPREAD-26AUG16SEAVAN-VAN3"
MLS_BTTS = "KXMLSBTTS-26AUG16SEAVAN-BTTS"
UECL_GAME = "KXUECLGAME-26AUG13TOBPAR-TOB"
UECL_TOTAL = "KXUECLTOTAL-26AUG12SCRPAI-7"
UECL_SPREAD = "KXUECLSPREAD-26AUG12SCRPAI-SCR4"
UECL_BTTS = "KXUECLBTTS-26AUG13TOBPAR-BTTS"
UECL_ADVANCE = "KXUECLADVANCE-26AUG13TOBPAR-TOB"

ALL_CLUB = [
    LALIGA_GAME, LALIGA_TOTAL, LALIGA_SPREAD, LALIGA_BTTS,
    MLS_GAME, MLS_TOTAL, MLS_SPREAD, MLS_BTTS,
    UECL_GAME, UECL_TOTAL, UECL_SPREAD, UECL_BTTS, UECL_ADVANCE,
]


def leg(market: str, event: str | None, side: str = "yes") -> RfqLeg:
    return RfqLeg(
        market_ticker=market, event_ticker=event, side=side,
        yes_settlement_value_cc=None,
    )


def event_of(ticker: str) -> str:
    return "-".join(ticker.split("-")[:2])


class MappingProvider:
    def __init__(self, answers: dict[str, bool | None]) -> None:
        self._answers = answers

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        return self._answers.get(event_ticker)


# --- 1. classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        (LALIGA_GAME, LegType.MONEYLINE),
        (LALIGA_TOTAL, LegType.TOTAL),
        (LALIGA_SPREAD, LegType.SPREAD),
        (LALIGA_BTTS, LegType.BTTS),
        (MLS_GAME, LegType.MONEYLINE),
        (MLS_TOTAL, LegType.TOTAL),
        (MLS_SPREAD, LegType.SPREAD),
        (MLS_BTTS, LegType.BTTS),
        (UECL_GAME, LegType.MONEYLINE),
        (UECL_TOTAL, LegType.TOTAL),
        (UECL_SPREAD, LegType.SPREAD),
        (UECL_BTTS, LegType.BTTS),
        (UECL_ADVANCE, LegType.ADVANCE),
    ],
)
def test_club_series_classify_to_wc_families(ticker: str, expected: LegType) -> None:
    assert classify_leg(ticker) is expected


@pytest.mark.parametrize("ticker", ALL_CLUB)
def test_club_series_classify_sport_soccer(ticker: str) -> None:
    # UECL is the new keyword ('UCL' is not a substring of 'UECL');
    # LALIGA/MLS rows are regression pins.
    assert classify_sport(ticker) is Sport.SOCCER


def test_laliga_advance_blocker_never_types_advance() -> None:
    # KXLALIGAADVANCE is a La Liga season/promotion series, NOT a two-team
    # tie market — the blocker must precede the ADVANCE keyword.
    got = classify_leg("KXLALIGAADVANCE-26-RMA")
    assert got is LegType.UNKNOWN
    assert got is not LegType.ADVANCE


@pytest.mark.parametrize(
    "ticker",
    [
        "KXLALIGA-26-RMA",            # league-winner futures
        "KXLALIGASCORE-26AUG17DEPELC-11",   # correct score
        "KXLALIGAMOV-26AUG17DEPELC-ELC1",   # method of victory
        "KXLALIGAFTTS-26AUG17DEPELC-ELC",   # first team to score
        "KXMLSCUP-26-SEA",            # MLS Cup futures
        "KXMLSJOIN-26-MIA",           # transfers
        "KXMLSSKILLS-26-XYZ",         # skills challenge
        "KXMLSLEADER-26-XYZ",         # season leader (LEADERMLB blocker ≠ this)
    ],
)
def test_club_special_series_stay_unknown(ticker: str) -> None:
    assert classify_leg(ticker) is LegType.UNKNOWN


# --- 2. markup sport map --------------------------------------------------------


def test_club_combos_take_soccer_markup_never_other() -> None:
    assert sport_of([LALIGA_GAME, LALIGA_TOTAL]) == "soccer"
    assert sport_of([MLS_GAME, MLS_BTTS]) == "soccer"
    assert sport_of([UECL_GAME, UECL_ADVANCE]) == "soccer"
    # Cross-competition soccer stays one sport (WC × club = soccer markup);
    # cross-SPORT stays the fail-safe 'other'.
    assert sport_of(["KXWCGAME-26JUL19ESPARG-ARG", LALIGA_GAME]) == "soccer"
    assert sport_of([LALIGA_GAME, "KXMLBGAME-26AUG121340BALMIN-BAL"]) == "other"


# --- 3. farm gating (the 48h-scalar money trap) ---------------------------------


def test_club_family2_win_over_half_impossible_but_never_farms() -> None:
    # Family 2 (team win ⟹ over-0.5): win-YES × over-0.5-NO is IMPOSSIBLE.
    # On KXWC it farms (airtight); on club soccer it must NOT (a rescheduled
    # game scalar-settles both legs at fair value — not certain-NO).
    club_legs = (
        leg("KXLALIGAGAME-26AUG26RMARSO-RMA", "KXLALIGAGAME-26AUG26RMARSO", "yes"),
        leg("KXLALIGATOTAL-26AUG26RMARSO-1", "KXLALIGATOTAL-26AUG26RMARSO", "no"),
    )
    rel = classify_legs(club_legs, MappingProvider({}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is False

    wc_legs = (
        leg("KXWCGAME-26JUL19ESPARG-ARG", "KXWCGAME-26JUL19ESPARG", "yes"),
        leg("KXWCTOTAL-26JUL19ESPARG-1", "KXWCTOTAL-26JUL19ESPARG", "no"),
    )
    rel_wc = classify_legs(wc_legs, MappingProvider({}))
    assert rel_wc.kind is RelationshipKind.IMPOSSIBLE
    assert rel_wc.farmable is True


def test_uecl_advance_complement_impossible_but_never_farms() -> None:
    # Family 4 (advance complement): exactly one side of a tie advances, so
    # both-NO is impossible — TRUE for two-legged ties too. But the farm is
    # blocked: the tie's markets carry the club scalar surface.
    ev = "KXUECLADVANCE-26AUG13TOBPAR"
    both_no = (
        leg("KXUECLADVANCE-26AUG13TOBPAR-TOB", ev, "no"),
        leg("KXUECLADVANCE-26AUG13TOBPAR-PAR", ev, "no"),
    )
    rel = classify_legs(both_no, MappingProvider({ev: True}))
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is False

    wc_ev = "KXWCADVANCE-26JUL12ARGFRA"
    wc_both_no = (
        leg("KXWCADVANCE-26JUL12ARGFRA-ARG", wc_ev, "no"),
        leg("KXWCADVANCE-26JUL12ARGFRA-FRA", wc_ev, "no"),
    )
    rel_wc = classify_legs(wc_both_no, MappingProvider({wc_ev: True}))
    assert rel_wc.kind is RelationshipKind.IMPOSSIBLE
    assert rel_wc.farmable is True


# --- 4. two-legged-tie ADVANCE rho regime ---------------------------------------


def shipped_params() -> SgpParams:
    cfg = CorrelationConfig()
    return SgpParams(
        pair_rho=dict(cfg.pair_rho),
        default_rho=cfg.same_event_rho,
        cross_event_rho=cfg.cross_event_rho,
        typed_uncertainty=cfg.typed_rho_uncertainty,
        untyped_uncertainty=cfg.untyped_rho_uncertainty,
        pair_uncertainty=dict(cfg.pair_rho_uncertainty),
        pair_rho_by_sport={s: dict(t) for s, t in cfg.pair_rho_by_sport.items()},
        oriented_curve={k: list(v) for k, v in cfg.oriented_curve.items()},
        oriented_curve_uncertainty=dict(cfg.oriented_curve_uncertainty),
    )


def test_uecl_advance_pair_prices_untyped_never_single_match_rho() -> None:
    # Same-game UECL advance × total: the measured advance|* values are the
    # SINGLE-MATCH regime (KXWC); a two-legged tie couples through the leg-1
    # aggregate — the guard must fall to the flat/widened untyped path.
    legs = [
        leg(UECL_ADVANCE, event_of(UECL_ADVANCE), "yes"),
        leg("KXUECLTOTAL-26AUG13TOBPAR-3", "KXUECLTOTAL-26AUG13TOBPAR", "yes"),
    ]
    c = build_sgp_correlation(legs, ((0, 1),), shipped_params(), marginals=[0.5, 0.5])
    assert c.untyped_pairs == 1
    assert any("two-legged-tie advance pair" in n for n in c.notes)


def test_wc_advance_pair_keeps_the_measured_single_match_regime() -> None:
    legs = [
        leg("KXWCADVANCE-26JUL12ARGFRA-ARG", "KXWCADVANCE-26JUL12ARGFRA", "yes"),
        leg("KXWCTOTAL-26JUL12ARGFRA-3", "KXWCTOTAL-26JUL12ARGFRA", "yes"),
    ]
    c = build_sgp_correlation(legs, ((0, 1),), shipped_params(), marginals=[0.5, 0.5])
    assert not any("two-legged-tie advance pair" in n for n in c.notes)


# --- 5. pregame occurrence_datetime anchor --------------------------------------

NOW = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)


def test_late_created_club_rung_anchors_on_occurrence_datetime() -> None:
    # The live defect this pins (KXUECLTOTAL-26AUG12SCRPAI-7, 2026-08-12):
    # a rung created ON GAME DAY carried a creation-relative
    # expected_expiration_time (+2h vs its siblings' event-stable 20:00Z).
    # min() over close/exp/occurrence must pick the earlier, stable
    # occurrence anchor — never admit the leg in-play.
    clock = FakeClock(start=NOW)
    meta = MetadataCache(None, clock)  # type: ignore[arg-type]
    kickoff = datetime(2026, 8, 12, 16, 10, tzinfo=UTC)
    stable_anchor = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)      # kickoff+~4h
    inflated_exp = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)       # creation-relative
    meta._markets[UECL_TOTAL] = MarketMeta(  # noqa: SLF001 (test seam)
        ticker=UECL_TOTAL,
        status="active",
        grid=None,
        event_ticker="KXUECLTOTAL-26AUG12SCRPAI",
        close_time=datetime(2026, 8, 14, 22, 0, tzinfo=UTC),      # game+2d, never anchors
        expected_expiration_time=inflated_exp,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
        occurrence_datetime=stable_anchor,
    )
    gate = PregameGate(FiltersConfig(), meta, clock, ScheduleCache())
    resolved = gate.leg_start(UECL_TOTAL)
    assert resolved is not None
    assert resolved.precise is False
    # Default soccer offset 4.5h off the STABLE anchor: 20:00Z − 4.5h = 15:30Z
    # (pregame). Off the inflated exp it would be 17:30Z = 1.3h IN-PLAY.
    assert resolved.start == stable_anchor - timedelta(hours=4.5)
    assert resolved.start < kickoff


def test_missing_occurrence_datetime_changes_nothing() -> None:
    clock = FakeClock(start=NOW)
    meta = MetadataCache(None, clock)  # type: ignore[arg-type]
    exp = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    meta._markets[UECL_GAME] = MarketMeta(  # noqa: SLF001 (test seam)
        ticker=UECL_GAME,
        status="active",
        grid=None,
        event_ticker="KXUECLGAME-26AUG13TOBPAR",
        close_time=datetime(2026, 8, 15, 21, 0, tzinfo=UTC),
        expected_expiration_time=exp,
        raw={},
        fetched_mono_ns=clock.monotonic_ns(),
    )
    gate = PregameGate(FiltersConfig(), meta, clock, ScheduleCache())
    resolved = gate.leg_start(UECL_GAME)
    assert resolved is not None
    assert resolved.start == exp - timedelta(hours=4.5)
