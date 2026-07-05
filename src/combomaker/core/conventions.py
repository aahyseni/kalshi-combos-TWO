"""Ground-truth exchange conventions — the ONLY place direction/sign/fee-side
semantics may live (quiet-failure defense #1).

The dangerous failure mode: a sign convention wrong the same way in code and
tests, so everything passes while losing money. Defense: the semantics below
are treated as UNVERIFIED doc readings until the Phase 2.5 harness
(``combomaker ground-truth``) records what the exchange ACTUALLY did in real
demo round trips and writes ``tests/fixtures/ground_truth/conventions.json``.
``load_conventions()`` prefers that fixture; without it you get the
doc-assumed values with ``verified=False``, and anything that sends real
quotes must call ``require_verified()`` first.

No module under ``pricing/`` or ``risk/`` may interpret ``accepted_side``,
fee side attribution, or position signs itself — they consume a
``Conventions`` instance. Enforced by ``tests/test_architecture.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

DEFAULT_FIXTURE_PATH = Path("tests") / "fixtures" / "ground_truth" / "conventions.json"


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def opposite(self) -> Side:
        return Side.NO if self is Side.YES else Side.YES


class ConventionsUnverifiedError(RuntimeError):
    pass


class ConventionsFixtureError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Conventions:
    verified: bool
    source: str

    # When the requester accepts side X of our quote, which side are WE long?
    maker_side_on_yes_accept: Side
    maker_side_on_no_accept: Side
    # Our entry price per contract is the bid we quoted on that side.
    maker_pays_own_bid: bool
    # Fee attribution on the maker's fill (None = unknown until ground truth).
    maker_is_taker_on_fill: bool | None
    # Combo NO contract pays $1 − (product of leg values) (None = unknown).
    combo_no_pays_complement: bool | None

    def maker_position_side(self, accepted_side: Side) -> Side:
        return (
            self.maker_side_on_yes_accept
            if accepted_side is Side.YES
            else self.maker_side_on_no_accept
        )

    def require_verified(self) -> None:
        if not self.verified:
            raise ConventionsUnverifiedError(
                "exchange conventions are doc-assumed, not ground-truth verified; "
                "run the Phase 2.5 harness (combomaker ground-truth) on demo first"
            )


# What the docs strongly indicate (docs/api-notes/SUMMARY.md, FIX AcceptQuote
# mapping). UNVERIFIED until the fixture exists — do not quote off this.
DOC_ASSUMED = Conventions(
    verified=False,
    source="docs (UNVERIFIED — Phase 2.5 pending)",
    maker_side_on_yes_accept=Side.YES,
    maker_side_on_no_accept=Side.NO,
    maker_pays_own_bid=True,
    maker_is_taker_on_fill=None,
    combo_no_pays_complement=None,
)


def _parse_side(raw: Any, field: str) -> Side:
    try:
        return Side(str(raw))
    except ValueError as exc:
        raise ConventionsFixtureError(f"{field}: bad side value {raw!r}") from exc


def load_conventions(fixture_path: Path | None = None) -> Conventions:
    """Fixture-verified conventions if present, else doc-assumed (unverified)."""
    path = fixture_path or DEFAULT_FIXTURE_PATH
    if not path.exists():
        return DOC_ASSUMED
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConventionsFixtureError(f"unreadable conventions fixture {path}: {exc}") from exc
    try:
        return Conventions(
            verified=True,
            source=str(path),
            maker_side_on_yes_accept=_parse_side(
                raw["maker_side_on_yes_accept"], "maker_side_on_yes_accept"
            ),
            maker_side_on_no_accept=_parse_side(
                raw["maker_side_on_no_accept"], "maker_side_on_no_accept"
            ),
            maker_pays_own_bid=bool(raw["maker_pays_own_bid"]),
            maker_is_taker_on_fill=(
                None
                if raw.get("maker_is_taker_on_fill") is None
                else bool(raw["maker_is_taker_on_fill"])
            ),
            combo_no_pays_complement=(
                None
                if raw.get("combo_no_pays_complement") is None
                else bool(raw["combo_no_pays_complement"])
            ),
        )
    except KeyError as exc:
        raise ConventionsFixtureError(f"conventions fixture missing field {exc}") from exc
