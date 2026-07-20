"""Taxonomy-impossible constructibility tripwire (V3 robustness §2.4-1,
judge-mandated 2026-07-11).

The 2026-07-11 containment probe mapped the full 50-shape pair universe
(``docs/calibration/containment_probe/taxonomy.json``) and probed the exchange
validator (``exchange_matrix.json``). 30 semantically-IMPOSSIBLE shape ×
side-mix cells are exchange-BLOCKED today but would PRICE (copula/fallback) if
Kalshi's validator silently loosened — certain-$0 paper quoted at 5–35c of
reference fair. This module pins those cells as a repo fixture
(``tests/fixtures/ground_truth/taxonomy_impossible.json``) and matches
same-game pairs against them, so the classifier can decline them IMPOSSIBLE
with ``farmable=False`` and a DEDICATED, countable note
(``taxonomy-impossible tripwire: <shape>``).

Doctrine:

- The verdict is a NO-QUOTE, never a farm: fixture-driven certainty is not an
  airtight in-code proof (quiet-failure defense — a wrong pin must only ever
  cost coverage, never money).
- Any live RFQ matching a pinned cell is PROOF the exchange validator changed
  (every pin is exchange-BLOCKED today) — the dedicated note makes the event
  loud instead of a silent copula price.
- The shipped impossibility families run FIRST in ``classify_legs`` and keep
  their own (sometimes farmable) verdicts; the tripwire is the backstop for
  the shapes deliberately NOT wired.
- Fail-closed on fixture-load failure: a missing or corrupt fixture makes the
  tripwire INERT (one warning, existing behavior unchanged) — it must never
  turn a data problem into a classification change.
- Fail-closed matching: any parse doubt (unparseable line/team/entity) simply
  fails the match — status quo, never a guessed impossibility.

Known residual (documented, not covered): S49 (tennis tournament-win ⇒
match-win) is CROSS-scope — the two legs carry different game codes, and the
tripwire scans same-game pairs only; there is no verified same-scope ticker
key to pin (never guess a ticker relation).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from combomaker.ops.logging import get_logger
from combomaker.pricing.legtypes import classify_leg, classify_sport, resolve_pricing_alias
from combomaker.rfq.models import RfqLeg

log = get_logger(__name__)

# Same tests/fixtures/ground_truth/ convention as core/conventions.py. The
# module-anchored candidate keeps the tripwire live when the process does not
# run from the repo root; an installed wheel simply finds neither and goes
# inert (with the warning).
DEFAULT_FIXTURE_PATH = (
    Path("tests") / "fixtures" / "ground_truth" / "taxonomy_impossible.json"
)
_ANCHORED_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "ground_truth"
    / "taxonomy_impossible.json"
)

# Suffix vocabularies (mirror relationships.py / sgp.py — draw tokens are the
# shared soccer convention; a team suffix is TEAM+optional-line-digits).
_DRAW_TOKENS = frozenset({"TIE", "DRAW"})
_TEAM_SUFFIX = re.compile(r"^([A-Z]+)(\d*)$")
_PURE_DIGITS = re.compile(r"^\d+$")

_RELATIONS = frozenset(
    {"same_team", "diff_team", "same_entity", "line_a_gt_b", "line_a_ge_b", "line_a_lt_b"}
)


@dataclass(frozen=True, slots=True)
class _LegPattern:
    side: str
    leg_type: str | None
    series_is: str | None
    series_has: str | None
    series_lacks: str | None
    suffix_in: frozenset[str] | None
    suffix_not_in: frozenset[str] | None
    suffix_team: bool
    line: int | None
    line_ge: int | None
    line_le: int | None
    requires_line: bool


@dataclass(frozen=True, slots=True)
class _Cell:
    shape: str
    name: str
    sport: str | None
    a: _LegPattern
    b: _LegPattern
    relations: tuple[str, ...]


class TripwireFixtureError(ValueError):
    pass


def _series(ticker: str) -> str:
    # Pricing aliases resolve here (review 2026-07-16): classify_leg/game_key
    # already resolve, so an aliased champion leg ENTERS the same-game scan —
    # its team/suffix/line must then read off the SAME synthetic ticker
    # ('ARG'), not the raw 2-letter champion code ('AR'), or the pinned
    # advance cells both false-trip valid champion parlays and miss real
    # impossibles.
    return resolve_pricing_alias(ticker).split("-", 1)[0].upper()


def _suffix(ticker: str) -> str:
    return resolve_pricing_alias(ticker).rsplit("-", 1)[-1].upper()


def _line_of(ticker: str) -> int | None:
    """The leg's ticker line integer: a pure-digit last segment (totals,
    ladders, 4-segment props) or the digits of a TEAM+digits suffix (spreads,
    team corners). None on any other shape — never guess a line."""
    suffix = _suffix(ticker)
    if _PURE_DIGITS.match(suffix):
        return int(suffix)
    m = _TEAM_SUFFIX.match(suffix)
    if m is not None and m.group(2):
        return int(m.group(2))
    return None


def _team_of(ticker: str) -> str | None:
    """The team a suffix names, trailing line digits stripped (``FRA2`` →
    ``FRA``; ``ESP`` → ``ESP``). None for draw tokens or non-team shapes."""
    m = _TEAM_SUFFIX.match(_suffix(ticker))
    if m is None:
        return None
    team = m.group(1)
    if team in _DRAW_TOKENS:
        return None
    return team


def _entity_of(ticker: str) -> str | None:
    """The player/entity segment: segment 2 of a ≥3-segment ticker (players
    sit there in every pinned shape: KXWCFIRSTGOAL-<g>-<P>, KXWCGOAL-<g>-<P>-1,
    KXWNBAPTS-<g>-<P>-15, KXPGATOUR-<t>-<P>)."""
    # Alias-resolved like _series/_suffix (verify follow-up 2026-07-16):
    # unreachable for champion legs today (no same_entity cell matches an
    # ADVANCE leg) — consistency, not a live fix.
    parts = resolve_pricing_alias(ticker).upper().split("-")
    if len(parts) < 3 or not parts[2]:
        return None
    return parts[2]


def _parse_pattern(raw: dict[str, Any], where: str) -> _LegPattern:
    side = raw.get("side")
    if side not in ("yes", "no"):
        raise TripwireFixtureError(f"{where}: side must be yes/no, got {side!r}")
    known = {
        "side", "type", "series_is", "series_has", "series_lacks",
        "suffix_in", "suffix_not_in", "suffix_team", "line", "line_ge",
        "line_le", "requires_line",
    }
    unknown = set(raw) - known
    if unknown:
        raise TripwireFixtureError(f"{where}: unknown pattern fields {sorted(unknown)}")

    def _opt_str(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise TripwireFixtureError(f"{where}: {key} must be a non-empty string")
        return value.upper() if key.startswith("series") else value

    def _opt_int(key: str) -> int | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise TripwireFixtureError(f"{where}: {key} must be an int")
        return value

    def _opt_set(key: str) -> frozenset[str] | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, list) or not value or not all(
            isinstance(item, str) and item for item in value
        ):
            raise TripwireFixtureError(f"{where}: {key} must be a non-empty string list")
        return frozenset(item.upper() for item in value)

    return _LegPattern(
        side=side,
        leg_type=_opt_str("type"),
        series_is=_opt_str("series_is"),
        series_has=_opt_str("series_has"),
        series_lacks=_opt_str("series_lacks"),
        suffix_in=_opt_set("suffix_in"),
        suffix_not_in=_opt_set("suffix_not_in"),
        suffix_team=bool(raw.get("suffix_team", False)),
        line=_opt_int("line"),
        line_ge=_opt_int("line_ge"),
        line_le=_opt_int("line_le"),
        requires_line=bool(raw.get("requires_line", False)),
    )


def _parse_cell(raw: dict[str, Any], index: int) -> _Cell:
    where = f"cells[{index}]"
    shape = raw.get("shape")
    if not isinstance(shape, str) or not shape:
        raise TripwireFixtureError(f"{where}: missing shape id")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise TripwireFixtureError(f"{where}: missing name")
    sport = raw.get("sport")
    if sport is not None and (not isinstance(sport, str) or not sport):
        raise TripwireFixtureError(f"{where}: sport must be a non-empty string or null")
    a_raw, b_raw = raw.get("a"), raw.get("b")
    if not isinstance(a_raw, dict) or not isinstance(b_raw, dict):
        raise TripwireFixtureError(f"{where}: legs a/b must be objects")
    relations_raw = raw.get("relations", [])
    if not isinstance(relations_raw, list) or not all(
        isinstance(rel, str) for rel in relations_raw
    ):
        raise TripwireFixtureError(f"{where}: relations must be a string list")
    bad = set(relations_raw) - _RELATIONS
    if bad:
        raise TripwireFixtureError(f"{where}: unknown relations {sorted(bad)}")
    return _Cell(
        shape=shape,
        name=name,
        sport=sport,
        a=_parse_pattern(a_raw, f"{where}.a"),
        b=_parse_pattern(b_raw, f"{where}.b"),
        relations=tuple(relations_raw),
    )


def load_cells(fixture_path: Path | None = None) -> tuple[_Cell, ...]:
    """Parse the fixture (raising ``TripwireFixtureError`` on any corruption
    or a missing file). The classify-time entry point wraps this fail-closed;
    tests call it directly to probe both failure modes."""
    path = fixture_path or _default_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TripwireFixtureError(f"unreadable tripwire fixture {path}: {exc}") from exc
    except ValueError as exc:
        raise TripwireFixtureError(f"corrupt tripwire fixture {path}: {exc}") from exc
    cells_raw = raw.get("cells") if isinstance(raw, dict) else None
    if not isinstance(cells_raw, list) or not cells_raw:
        raise TripwireFixtureError(f"tripwire fixture {path}: no cells")
    cells: list[_Cell] = []
    for i, cell in enumerate(cells_raw):
        if not isinstance(cell, dict):
            raise TripwireFixtureError(f"cells[{i}]: not an object")
        cells.append(_parse_cell(cell, i))
    return tuple(cells)


def _default_path() -> Path:
    if DEFAULT_FIXTURE_PATH.exists():
        return DEFAULT_FIXTURE_PATH
    return _ANCHORED_FIXTURE_PATH


# Module-level cache for the default fixture: parsed once; a load failure is
# logged ONCE and pins the tripwire inert (empty cell set) for the process.
_CACHE: tuple[_Cell, ...] | None = None
_CACHE_LOADED = False


def _cells() -> tuple[_Cell, ...]:
    global _CACHE, _CACHE_LOADED
    if not _CACHE_LOADED:
        _CACHE_LOADED = True
        try:
            _CACHE = load_cells()
        except TripwireFixtureError as exc:
            _CACHE = ()
            log.warning(
                "taxonomy_tripwire_inert",
                detail=str(exc),
                consequence="constructibility tripwire disabled; the 30 "
                "exchange-blocked impossible cells fall back to pre-tripwire "
                "behavior (V3 robustness §2 exposure)",
            )
    return _CACHE or ()


def _match_leg(pattern: _LegPattern, ticker: str, side: str) -> bool:
    if side != pattern.side:
        return False
    if pattern.leg_type is not None and str(classify_leg(ticker)) != pattern.leg_type:
        return False
    series = _series(ticker)
    if pattern.series_is is not None and series != pattern.series_is:
        return False
    if pattern.series_has is not None and pattern.series_has not in series:
        return False
    if pattern.series_lacks is not None and pattern.series_lacks in series:
        return False
    suffix = _suffix(ticker)
    if pattern.suffix_in is not None and suffix not in pattern.suffix_in:
        return False
    if pattern.suffix_not_in is not None and suffix in pattern.suffix_not_in:
        return False
    if pattern.suffix_team and _team_of(ticker) is None:
        return False
    needs_line = (
        pattern.line is not None
        or pattern.line_ge is not None
        or pattern.line_le is not None
        or pattern.requires_line
    )
    if needs_line:
        line = _line_of(ticker)
        if line is None:
            return False
        if pattern.line is not None and line != pattern.line:
            return False
        if pattern.line_ge is not None and line < pattern.line_ge:
            return False
        if pattern.line_le is not None and line > pattern.line_le:
            return False
    return True


def _relations_hold(cell: _Cell, ticker_a: str, ticker_b: str) -> bool:
    for relation in cell.relations:
        if relation in ("same_team", "diff_team"):
            team_a, team_b = _team_of(ticker_a), _team_of(ticker_b)
            if team_a is None or team_b is None:
                return False
            if relation == "same_team" and team_a != team_b:
                return False
            if relation == "diff_team" and team_a == team_b:
                return False
        elif relation == "same_entity":
            entity_a, entity_b = _entity_of(ticker_a), _entity_of(ticker_b)
            if entity_a is None or entity_a != entity_b:
                return False
        else:  # line relations
            line_a, line_b = _line_of(ticker_a), _line_of(ticker_b)
            if line_a is None or line_b is None:
                return False
            if relation == "line_a_gt_b" and not line_a > line_b:
                return False
            if relation == "line_a_ge_b" and not line_a >= line_b:
                return False
            if relation == "line_a_lt_b" and not line_a < line_b:
                return False
    return True


def _match_cell(cell: _Cell, leg_a: RfqLeg, leg_b: RfqLeg) -> bool:
    if cell.sport is not None and (
        str(classify_sport(leg_a.market_ticker)) != cell.sport
        or str(classify_sport(leg_b.market_ticker)) != cell.sport
    ):
        return False
    return (
        _match_leg(cell.a, leg_a.market_ticker, leg_a.side)
        and _match_leg(cell.b, leg_b.market_ticker, leg_b.side)
        and _relations_hold(cell, leg_a.market_ticker, leg_b.market_ticker)
    )


def taxonomy_impossible(
    legs: Sequence[RfqLeg],
    game_keys: Sequence[str],
    cells: tuple[_Cell, ...] | None = None,
) -> tuple[str, str] | None:
    """(shape id, human detail) for the first SAME-GAME pair matching a pinned
    semantically-impossible cell — or None (no match / fixture inert).

    Pattern (a, b) is tried in both leg orders. Cross-game pairs are never
    scanned: every pinned cell is a within-game (same game code) relationship.
    """
    pinned = _cells() if cells is None else cells
    if not pinned:
        return None
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            if game_keys[i] != game_keys[j]:
                continue
            for cell in pinned:
                for leg_a, leg_b in ((legs[i], legs[j]), (legs[j], legs[i])):
                    if _match_cell(cell, leg_a, leg_b):
                        return cell.shape, (
                            f"{cell.name} — {leg_a.market_ticker} {leg_a.side} x "
                            f"{leg_b.market_ticker} {leg_b.side} is a pinned "
                            "exchange-blocked impossible mix (validator loosened?)"
                        )
    return None
