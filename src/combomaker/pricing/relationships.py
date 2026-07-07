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

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from combomaker.pricing.legtypes import LegType, classify_leg
from combomaker.rfq.models import RfqLeg


class RelationshipKind(StrEnum):
    OK = "ok"                    # classified; groups usable for correlation
    IMPOSSIBLE = "impossible"    # logically zero payout — v1: no-quote
    UNKNOWN = "unknown"          # classification failed — widen-or-no-quote
    CONTAINMENT = "containment"  # one leg logically implies another (joint pinned)


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
    # For CONTAINMENT only: (subset_index, superset_index) where a YES on the
    # subset leg logically IMPLIES a YES on the superset leg, so the combo joint
    # is exactly P(subset). None for every other kind.
    containment: tuple[int, int] | None = None


def _game_key(event_ticker: str) -> str:
    """The game a leg belongs to, for correlation grouping. Kalshi's
    event_ticker is ``SERIES-GAMECODE`` (e.g. ``KXWCGAME-26JUL05MEXENG``); the
    GAMECODE is shared across a game's market families (KXWCGAME/KXWCTOTAL/
    KXWCBTTS of one game), so it — not the series-specific event_ticker — is the
    same-game key. No hyphen (synthetic/degenerate ticker) ⇒ key on the whole
    string, so a leg whose event carries no game code never merges with another.

    Period/derived markets (first/second half — series like KXWC1HTOTAL) DO now
    key on the game code and rejoin the full-game same-game block, so the copula
    can correlate a modeled 1H leg with its full-time siblings. They are kept
    off the full-game STRUCTURAL inverter (no half-time scoreline window) by a
    guard in structural.py — NOT by grouping them out here (which used to leave
    a real 1H×FT combo pricing at independence)."""
    _series, sep, game = event_ticker.partition("-")
    if not sep:
        return event_ticker
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

    # Period × full-time BTTS is a LOGICAL CONTAINMENT, not a correlation: if
    # both teams scored by half-time (1H-BTTS yes) they have both scored in the
    # match (FT-BTTS yes). So within a game:
    #   1H-BTTS yes × FT-BTTS no  → IMPOSSIBLE (v1 policy: no-quote)
    #   1H-BTTS yes × FT-BTTS yes → CONTAINMENT, joint = P(1H-BTTS)
    # Only the bare 2-leg containment combo is priced coherently; a containment
    # pair buried in a larger combo (mixed with other correlated legs) is not
    # yet modeled → UNKNOWN (widen-or-no-quote), never a copula guess. The
    # subset-side "no" cases and other period families are DEFERRED (they fall
    # to the normal grouped/copula path).
    types = [classify_leg(leg.market_ticker) for leg in legs]
    containment: tuple[int, int] | None = None
    for sub in range(len(legs)):
        if types[sub] is not LegType.FIRST_HALF_BTTS or legs[sub].side != "yes":
            continue
        for sup in range(len(legs)):
            if types[sup] is not LegType.BTTS or game_keys[sup] != game_keys[sub]:
                continue
            if legs[sup].side == "no":
                notes.append(
                    f"1H-BTTS yes ({legs[sub].market_ticker}) implies FT-BTTS yes: "
                    f"{legs[sup].market_ticker} no is impossible"
                )
                return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))
            containment = (sub, sup)
    if containment is not None:
        if len(legs) != 2:
            notes.append("1H-BTTS containment pair inside a larger combo: not modeled")
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
        notes.append(
            f"1H-BTTS containment: joint = P({legs[containment[0]].market_ticker})"
        )
        return Relationship(RelationshipKind.CONTAINMENT, (), tuple(notes), containment)

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
