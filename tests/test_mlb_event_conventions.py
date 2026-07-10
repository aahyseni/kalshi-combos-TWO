"""DO-7 — ground-truth pin of the MLB ``event_mutually_exclusive`` flags.

WHY THIS FIXTURE IS AN ALARM, NOT CONFIG: the quote path never reads it —
``marketdata/metadata.py`` fetches each event's ``mutually_exclusive`` flag
LIVE, and ``relationships.classify_legs`` consumes it through the
``EventInfoProvider`` seam. So if Kalshi ever SILENTLY flips a flag, live
behavior changes immediately with no code diff:

- a prop family flipping false -> true would IMPOSSIBLE every two-YES
  same-event basket (kills ALL basket flow — the signature 8-16-leg all-NO HR
  baskets — with a reason code that looks legitimate);
- KXMLBGAME flipping true -> false would stop IMPOSSIBLE-ing YES+YES
  moneyline pairs (a two-winners combo would fall through to the copula).

This fixture pins the values a LIVE 24/24 probe recorded on 2026-07-09
(docs/reports/2026-07-09-mlb-measurement-tranche.md, "both phase-1 blockers
resolved" item 1). The fixture is the PIN; the live probe is the standing
re-verification. When a later tools/ spot check records a different flag, the
last-recorded table below no longer matches the fixture (or the re-recorded
fixture no longer matches this table) and the mismatch test fires — forcing a
deliberate, reviewed two-place update instead of a silent drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from combomaker.pricing.relationships import RelationshipKind, classify_legs
from combomaker.rfq.models import RfqLeg

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "ground_truth" / "mlb_event_conventions.json"
)

# The 9 combo-eligible MLB families (exhaustive — verified against all 1,387
# MVE collections, 2026-07-09 classification report).
MLB_FAMILIES = frozenset(
    {
        "KXMLBGAME",
        "KXMLBTOTAL",
        "KXMLBSPREAD",
        "KXMLBKS",
        "KXMLBHIT",
        "KXMLBHR",
        "KXMLBHRR",
        "KXMLBTB",
        "KXMLBRFI",
    }
)

# LAST-RECORDED spot-check values: the 2026-07-09 live probe (24 events, 24/24
# real booleans; docs/reports/2026-07-09-mlb-measurement-tranche.md + the [E]
# block of docs/calibration/staged_mlb_props.md). This table is deliberately
# DUPLICATED from the fixture rather than loaded from it: the fixture is the
# pin, this is what the tools/spot checks last recorded — when a re-probe
# disagrees, exactly one of the two gets updated first and the equality test
# below is the alarm that forces the other (and a review of the behavioral
# consequences above) in the same commit.
LAST_RECORDED_PROBE: dict[str, bool] = {
    "KXMLBGAME": True,       # correctly IMPOSSIBLEs YES+YES moneyline pairs
    "KXMLBTOTAL": False,
    "KXMLBSPREAD": False,
    "KXMLBKS": False,
    "KXMLBHIT": False,
    "KXMLBHR": False,
    "KXMLBHRR": False,
    "KXMLBTB": False,
    "KXMLBRFI": False,
}


def load_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data


# --- (a) fixture loads + schema sane ---------------------------------------------


def test_fixture_loads_and_schema_sane() -> None:
    data = load_fixture()
    flags = data["event_mutually_exclusive"]
    # Exactly the 9 combo-eligible families, every value a REAL boolean (the
    # probe's whole point was that Kalshi returns real booleans, never null).
    assert set(flags) == MLB_FAMILIES
    assert all(isinstance(v, bool) for v in flags.values())

    prov = data["_provenance"]
    assert prov["probe_date"] == "2026-07-09"
    assert "GetEvent" in prov["method"] and "24" in prov["method"]
    samples = prov["event_samples"]
    # One sample event per family, each with the family's own series prefix
    # (grammar check: SERIES-GAMECODE, so the ticker starts "<family>-").
    assert set(samples) - {"_note"} == MLB_FAMILIES
    for family in MLB_FAMILIES:
        assert samples[family].startswith(f"{family}-"), samples[family]


def test_fixture_matches_last_recorded_probe() -> None:
    """THE ALARM (see module docstring): fixture == the values the tools/spot
    checks last recorded. A silent flip by Kalshi surfaces here the moment a
    re-probe is recorded; updating either side alone fails this test."""
    data = load_fixture()
    assert data["event_mutually_exclusive"] == LAST_RECORDED_PROBE


# --- (b) behavioral pins through relationships.classify_legs ---------------------
# Real event/market grammar from the prod RFQ tape (2026-07-10): two distinct
# players' HR markets share ONE event ticker; the moneyline event carries the
# two team markets.

HR_EVENT = "KXMLBHR-26JUL092140AZSD"
HR_MARKET_A = "KXMLBHR-26JUL092140AZSD-AZCCARROLL7-1"
HR_MARKET_B = "KXMLBHR-26JUL092140AZSD-AZGMORENO14-1"
GAME_EVENT = "KXMLBGAME-26JUL101840MILPIT"
GAME_MARKET_A = "KXMLBGAME-26JUL101840MILPIT-MIL"
GAME_MARKET_B = "KXMLBGAME-26JUL101840MILPIT-PIT"


class FixtureFlagProvider:
    """Stub EventInfoProvider answering from a series->flag mapping; an
    unmapped series answers None (metadata missing)."""

    def __init__(self, flags: dict[str, bool]) -> None:
        self._flags = flags

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None:
        return self._flags.get(event_ticker.split("-", 1)[0])


def leg(market: str, event: str, side: str = "yes") -> RfqLeg:
    return RfqLeg(
        market_ticker=market, event_ticker=event, side=side, yes_settlement_value_cc=None
    )


@pytest.fixture()
def provider() -> FixtureFlagProvider:
    data = load_fixture()
    flags: dict[str, bool] = data["event_mutually_exclusive"]
    return FixtureFlagProvider(flags)


def test_flag_false_two_yes_prop_legs_not_impossible(provider: FixtureFlagProvider) -> None:
    """flag=false (all 6 prop families): two YES legs of ONE prop event must
    NOT be impossible — they reach OK with the same-game correlation group
    (the reachability of every multi-player basket rests on this)."""
    legs = (leg(HR_MARKET_A, HR_EVENT, "yes"), leg(HR_MARKET_B, HR_EVENT, "yes"))
    rel = classify_legs(legs, provider)
    assert rel.kind is RelationshipKind.OK
    assert rel.same_event_groups == ((0, 1),)


def test_flag_true_two_yes_moneyline_legs_impossible_not_farmable(
    provider: FixtureFlagProvider,
) -> None:
    """flag=true (KXMLBGAME): YES on both teams of one game is IMPOSSIBLE —
    and NOT farmable, because this branch rests on exchange METADATA, not a
    logical tautology (a wrong flag is farming's one loss path)."""
    legs = (leg(GAME_MARKET_A, GAME_EVENT, "yes"), leg(GAME_MARKET_B, GAME_EVENT, "yes"))
    rel = classify_legs(legs, provider)
    assert rel.kind is RelationshipKind.IMPOSSIBLE
    assert rel.farmable is False


def test_flag_none_is_unknown_no_quote() -> None:
    """flag=None (metadata missing): UNKNOWN — widen-or-no-quote, never a
    convenient default (quiet-failure defense #2)."""
    legs = (leg(HR_MARKET_A, HR_EVENT, "yes"), leg(HR_MARKET_B, HR_EVENT, "yes"))
    rel = classify_legs(legs, FixtureFlagProvider({}))
    assert rel.kind is RelationshipKind.UNKNOWN
    assert rel.same_event_groups == ()
