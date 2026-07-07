"""Logical relationship classifier for combo legs.

v1 detects, per quiet-failure defense #2, with an explicit UNKNOWN branch that
the caller must turn into widen-or-no-quote:

- IMPOSSIBLE: same market on both sides, or two YES legs of an event Kalshi
  marks mutually exclusive. v1 policy is NO-QUOTE on impossible combos (not
  "quote the arb"): if our classification were wrong, a confident quote on a
  "logically impossible" combo is exactly the trap takers hunt for. Scalar
  settlement (DNP etc.) can also make binary logic wrong — another reason not
  to be clever here.
- SAME-GAME groups: legs of the same GAME form the correlation blocks. Kalshi's
  event_ticker is per-market-SERIES (``KXWCGAME-26JUL05MEXENG`` and
  ``KXWCTOTAL-26JUL05MEXENG`` of ONE game are different events), so the block key
  is the GAME code (the event_ticker after its series prefix), NOT the raw
  event_ticker — grouping on event_ticker silently splits a same-game SGP into
  independent singletons (the fail-safe inversion SGP sharps farm). Nesting
  inside a group is approximated by the block correlation + the
  correlation-uncertainty width adder. Mutual-exclusivity stays per-EVENT
  (home/draw/away of one moneyline event).
- UNKNOWN: any leg whose event metadata (or mutual-exclusivity flag) we don't
  have. Never defaults to independence — that's the fail-safe inversion that
  gets farmed by same-game-parlay sharps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from combomaker.rfq.models import RfqLeg

# Period / derived market families (first/second half, quarters) — series
# prefixes like KXWC1HTOTAL, KXWC2H, KX…FHTOTAL. These are NOT yet modeled for
# correlation (no half-time scoreline model shipped), so they must be kept OUT
# of a full-game same-game block, or the leg classifier mis-types e.g. a 1H
# total as a FULL-GAME total and the structural model prices it on the wrong
# settlement window. Matched against the SERIES prefix only.
_PERIOD_SERIES = re.compile(r"(?:1H|2H|H1|H2|FH|SH|[1-4]Q|Q[1-4]|QTR|HALF|PERIOD)")


class RelationshipKind(StrEnum):
    OK = "ok"                    # classified; groups usable for correlation
    IMPOSSIBLE = "impossible"    # logically zero payout — v1: no-quote
    UNKNOWN = "unknown"          # classification failed — widen-or-no-quote


class EventInfoProvider(Protocol):
    """Answers 'is this event's market family mutually exclusive?'.

    Returns None when unknown (missing metadata) — never guess.
    """

    def event_mutually_exclusive(self, event_ticker: str) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class Relationship:
    kind: RelationshipKind
    # Indices into the leg list, grouped by shared GAME (size >= 2 only). Name
    # kept for compatibility; the key is the game code, not the event_ticker.
    same_event_groups: tuple[tuple[int, ...], ...]
    notes: tuple[str, ...]


def _game_key(event_ticker: str) -> str:
    """The game a leg belongs to, for correlation grouping. Kalshi's
    event_ticker is ``SERIES-GAMECODE`` (e.g. ``KXWCGAME-26JUL05MEXENG``); the
    GAMECODE is shared across a game's market families (KXWCGAME/KXWCTOTAL/
    KXWCBTTS of one game), so it — not the series-specific event_ticker — is the
    same-game key. No hyphen (synthetic/degenerate ticker) ⇒ key on the whole
    string, so a leg whose event carries no game code never merges with another.

    EXCEPTION: period/derived markets (first/second half, quarters — series like
    KXWC1HTOTAL/KXWC2H) are NOT yet modeled for correlation, so they keep their
    full series-specific event_ticker as the key and therefore never join a
    full-game same-game block. Otherwise the classifier mis-types them as
    full-game legs and the structural model prices them on the wrong settlement
    window (NOTES L11). Revisit when the half-time scoreline model ships."""
    series, sep, game = event_ticker.partition("-")
    if not sep:
        return event_ticker
    if _PERIOD_SERIES.search(series):
        return event_ticker  # keep period markets out of the same-game block
    return game


def classify_legs(
    legs: tuple[RfqLeg, ...] | list[RfqLeg], events: EventInfoProvider
) -> Relationship:
    notes: list[str] = []

    # Defense in depth: the filter layer already rejects unknown sides, but a
    # side we can't read must never be silently treated as NO below (it would
    # dodge the mutual-exclusion impossibility count).
    for leg in legs:
        if not leg.side_known:
            notes.append(f"unknown side {leg.side!r} on {leg.market_ticker}")
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))

    # Duplicate market tickers: same side twice is a degenerate RFQ; opposite
    # sides is binary-impossible BUT scalar settlement could pay both partially
    # — either way we don't understand the request well enough to quote it.
    by_market: dict[str, list[int]] = {}
    for i, leg in enumerate(legs):
        by_market.setdefault(leg.market_ticker, []).append(i)
    for market, indices in by_market.items():
        if len(indices) > 1:
            sides = {legs[i].side for i in indices}
            if len(sides) > 1:
                notes.append(f"same market both sides: {market}")
                return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))
            notes.append(f"duplicate leg: {market}")
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))

    # Pass 1 — per-EVENT: require an event_ticker and enforce mutual-exclusion.
    # Exclusivity is a property of a single Kalshi event's market family (the
    # home/draw/away outcomes of one moneyline event, etc.), so it is checked
    # per event_ticker, NOT per game.
    by_event: dict[str, list[int]] = {}
    game_keys: list[str] = []
    for i, leg in enumerate(legs):
        if leg.event_ticker is None:
            notes.append(f"leg without event_ticker: {leg.market_ticker}")
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
        by_event.setdefault(leg.event_ticker, []).append(i)
        game_keys.append(_game_key(leg.event_ticker))
    for event_ticker, indices in by_event.items():
        if len(indices) < 2:
            continue
        exclusive = events.event_mutually_exclusive(event_ticker)
        if exclusive is None:
            notes.append(f"mutual-exclusivity unknown for event {event_ticker}")
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
        yes_count = sum(1 for i in indices if legs[i].side == "yes")
        if exclusive and yes_count >= 2:
            notes.append(f"{yes_count} YES legs of mutually exclusive event {event_ticker}")
            return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))

    # Pass 2 — per-GAME: the correlation blocks. Same-game legs from DIFFERENT
    # market families share a game code but not an event_ticker, so they must be
    # grouped here or they price independent (the bug this fixes).
    by_game: dict[str, list[int]] = {}
    for i, key in enumerate(game_keys):
        by_game.setdefault(key, []).append(i)
    groups: list[tuple[int, ...]] = []
    for key, indices in by_game.items():
        if len(indices) < 2:
            continue
        groups.append(tuple(indices))
        notes.append(f"same-game group {key}: {len(indices)} legs")

    return Relationship(RelationshipKind.OK, tuple(groups), tuple(notes))
