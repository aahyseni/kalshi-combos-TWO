import json
from pathlib import Path

import pytest

from combomaker.core.conventions import (
    DOC_ASSUMED,
    Conventions,
    ConventionsFixtureError,
    ConventionsUnverifiedError,
    Side,
    load_conventions,
)

FIXTURE = {
    "maker_side_on_yes_accept": "yes",
    "maker_side_on_no_accept": "no",
    "maker_pays_own_bid": True,
    "maker_is_taker_on_fill": False,
    "combo_no_pays_complement": True,
}


class TestDocAssumed:
    def test_unverified_and_blocks_quoting(self) -> None:
        assert not DOC_ASSUMED.verified
        with pytest.raises(ConventionsUnverifiedError):
            DOC_ASSUMED.require_verified()

    def test_unknowns_are_none_not_defaults(self) -> None:
        # fee attribution and combo NO payout must stay unknown, not guessed
        assert DOC_ASSUMED.maker_is_taker_on_fill is None
        assert DOC_ASSUMED.combo_no_pays_complement is None

    def test_missing_fixture_falls_back_to_doc_assumed(self, tmp_path: Path) -> None:
        assert load_conventions(tmp_path / "absent.json") is DOC_ASSUMED


class TestFixtureLoading:
    def test_verified_fixture(self, tmp_path: Path) -> None:
        path = tmp_path / "conventions.json"
        path.write_text(json.dumps(FIXTURE), encoding="utf-8")
        conv = load_conventions(path)
        assert conv.verified
        conv.require_verified()
        assert conv.maker_position_side(Side.YES) is Side.YES
        assert conv.maker_position_side(Side.NO) is Side.NO
        assert conv.maker_is_taker_on_fill is False

    def test_missing_field_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "conventions.json"
        bad = {k: v for k, v in FIXTURE.items() if k != "maker_pays_own_bid"}
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ConventionsFixtureError):
            load_conventions(path)

    def test_bad_side_value_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "conventions.json"
        path.write_text(
            json.dumps({**FIXTURE, "maker_side_on_yes_accept": "long"}), encoding="utf-8"
        )
        with pytest.raises(ConventionsFixtureError):
            load_conventions(path)

    def test_inverted_fixture_would_change_behavior(self, tmp_path: Path) -> None:
        # The whole point: if ground truth says the docs were wrong, the code
        # follows the fixture, not the doc assumption.
        path = tmp_path / "conventions.json"
        path.write_text(
            json.dumps(
                {**FIXTURE, "maker_side_on_yes_accept": "no", "maker_side_on_no_accept": "yes"}
            ),
            encoding="utf-8",
        )
        conv = load_conventions(path)
        assert conv.maker_position_side(Side.YES) is Side.NO


def test_side_opposite() -> None:
    assert Side.YES.opposite is Side.NO
    assert Side.NO.opposite is Side.YES


def test_conventions_frozen() -> None:
    with pytest.raises(AttributeError):
        DOC_ASSUMED.verified = True  # type: ignore[misc]


def test_repo_fixture_if_present_is_loadable() -> None:
    # Once Phase 2.5 promotes a real fixture, it must always parse.
    repo_fixture = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "ground_truth"
        / "conventions.json"
    )
    if repo_fixture.exists():
        conv = load_conventions(repo_fixture)
        assert isinstance(conv, Conventions)
        assert conv.verified
