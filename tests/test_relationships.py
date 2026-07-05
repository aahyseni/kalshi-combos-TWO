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
