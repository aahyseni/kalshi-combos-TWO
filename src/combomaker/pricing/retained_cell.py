"""The CELL a combo belongs to for the measured retained-edge floor
(2026-09-04 build A item 2; risk/retained_edge_floor.py is the estimator).

    cell = (sport, sorted classify_leg types, side pattern, game signature)

built ONLY from existing pure classifiers — the markup's ``sport_of`` (the
same sport tag the tier ladder uses), ``classify_leg`` and ``game_key`` —
so a cell is a SHAPE, never a ticker list (no blocklists, ever). The side
pattern is all-YES / all-NO / mixed (the measured leak sat on all-NO
baskets: NRFI×NRFI, "nobody homers"); the game signature is same-game
(one game), cross-game (every leg its own game) or partial (a same-game
block plus other games) — the three shapes the deep dive split P&L by.

O(legs) string work per quote (each classifier is a prefix/split); the
quote path then does one dict lookup keyed on the tuple.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from combomaker.pricing.grouping import game_key
from combomaker.pricing.legtypes import classify_leg
from combomaker.pricing.markup import sport_of

CellKey = tuple[str, str, str, str]


class _LegLike(Protocol):
    @property
    def market_ticker(self) -> str: ...

    @property
    def event_ticker(self) -> str | None: ...

    @property
    def side(self) -> str: ...


def side_pattern(sides: Iterable[str]) -> str:
    seen = {str(s).lower() for s in sides}
    if seen == {"yes"}:
        return "all_yes"
    if seen == {"no"}:
        return "all_no"
    return "mixed"


def game_signature(n_legs: int, n_games: int) -> str:
    if n_legs <= 1 or n_games <= 1:
        return "same"
    if n_games >= n_legs:
        return "cross"
    return "partial"


def cell_key(legs: Iterable[_LegLike]) -> CellKey:
    legs = list(legs)
    tickers = [leg.market_ticker for leg in legs]
    sport = sport_of(tickers)
    types = "|".join(sorted(str(classify_leg(t)) for t in tickers))
    games = {game_key(leg.event_ticker) for leg in legs if leg.event_ticker}
    n_games = len(games) if games else len(legs)
    sides = side_pattern(leg.side for leg in legs)
    return (sport, types, sides, game_signature(len(legs), n_games))


def sport_of_cell(key: CellKey) -> str:
    return key[0]


def floor_for_cell(
    key: CellKey,
    table: Mapping[CellKey, int],
    pool_floor_cc: Mapping[str, int],
) -> int:
    """The published retained-edge floor for a cell (2026-09-04 review fix
    M2; POINT rule since build "floor-point-estimate"): a cell in the table
    takes its own measured floor; a cell ABSENT from the table (no settled
    record at all — the never-sold shape) takes its sport pool's POINT
    (max(0, −pool mean shortfall)), exactly like a thin cell; a sport with
    no pool at all takes the LARGEST published pool point — the fail-closed
    DIRECTION, but a point, never a z·SE bound (and, if no pool was
    published, the largest cell floor). Absent never resolves to None (the
    unmeasured fallback). One or two dict lookups on the quote path. Shared
    by the engine and the replay tool (never reimplemented)."""
    own = table.get(key)
    if own is not None:
        return own
    pool = pool_floor_cc.get(sport_of_cell(key))
    if pool is not None:
        return pool
    if pool_floor_cc:
        return max(pool_floor_cc.values())
    return max(table.values(), default=0)
