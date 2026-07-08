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
from typing import Literal, Protocol

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
    # IMPOSSIBLE only: True iff the impossibility is a LOGICAL TAUTOLOGY (an
    # airtight scoring/containment/same-market contradiction) whose YES can
    # never settle — safe for the maker to FARM (short the certain-NO side). It
    # is NEVER set on the mutual-exclusion IMPOSSIBLE branch, which depends on
    # exchange METADATA (event_mutually_exclusive) and so is not logically
    # certain. False everywhere except the tautological IMPOSSIBLE returns.
    farmable: bool = False


# Team-corners ticker suffix = team code + the over-line digits (…-COL5 -> COL, 5).
_CORNERS_TEAM_LINE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _corners_team_line(market_ticker: str) -> tuple[str, int] | None:
    """(team, over-line) parsed from a team-corners ticker's suffix
    (``…-POR8`` -> ``("POR", 8)``). None when the suffix isn't a team-code +
    digits shape — never guess a line for the containment logic."""
    suffix = market_ticker.rsplit("-", 1)[-1].upper()
    m = _CORNERS_TEAM_LINE.match(suffix)
    if m is None:
        return None
    return m.group(1), int(m.group(2))


# --- Parsing + sign helpers for the logical-containment families ----------------
# All fail-closed (None / False on anything unparseable): a doubt must fall to a
# normal OK/copula, never a false IMPOSSIBLE/CONTAINMENT.

_TOTAL_LINE = re.compile(r"^\d+$")

# Kalshi TOTAL / FIRST_HALF_TOTAL suffix ``…-N`` encodes "over N-0.5" = "at least
# N goals" (structural.py `_parse_total_line` + the margin/total suffix docs:
# DOC-VERIFIED live metadata 2026-07-06, e.g. KXMLBTOTAL-…-5 = 'Over 4.5'). So
# the over-0.5 line — the only total a team win implies (a 1-0 win is under 1.5)
# — is exactly N == 1.
_OVER_HALF_LINE = 1

# A moneyline suffix in this set names the DRAW, not a team (mirrors the local
# `_DRAW_SUFFIXES` in sgp.py/structural.py). A drawn result is 0-0-inclusive, so
# it implies no goal — only a TEAM win does.
_DRAW_SUFFIXES = frozenset({"TIE", "DRAW"})


def _total_line(market_ticker: str) -> int | None:
    """Integer line N from a TOTAL / FIRST_HALF_TOTAL ticker suffix (``…-3`` -> 3,
    "over 2.5" = at least 3 goals; ``…-1`` -> 1 = "over 0.5"). None when the
    suffix isn't a bare integer (a '2.5'-style decimal or garbage) — never guess
    a line for the containment logic."""
    suffix = market_ticker.rsplit("-", 1)[-1]
    if _TOTAL_LINE.match(suffix):
        return int(suffix)
    return None


def _moneyline_is_team(market_ticker: str) -> bool:
    """True iff a MONEYLINE leg's suffix names a TEAM (not the TIE/DRAW side). A
    team win requires outscoring the opponent in regulation (>=1 goal); a draw
    settles on a 0-0-inclusive result and implies no goal. Fail-closed: an empty
    suffix is not a team."""
    suffix = market_ticker.rsplit("-", 1)[-1].upper()
    return bool(suffix) and suffix not in _DRAW_SUFFIXES


def _containment_sign(
    sub_i: int, sup_i: int, sub_side: str, sup_side: str
) -> tuple[int, int] | Literal["impossible"] | None:
    """Map the YES/NO sides of a logical implication A⟹B (leg ``sub_i`` = A, leg
    ``sup_i`` = B) to a verdict — the SAME matrix for every containment family:

      {A yes, B no}  → "impossible"     (A cannot happen without B)
      {A yes, B yes} → (sub_i, sup_i)   containment, subset = A  (joint = P(A))
      {A no,  B no}  → (sup_i, sub_i)   containment, subset = B  (¬B⟹¬A, so the
                                        B-no leg is the effective subset; its
                                        marginal is P(B no))
      {A no,  B yes} → None             possible (falls to the copula)

    Fail-closed: any side not exactly yes/no returns None (side-known is enforced
    upstream; never read an unknown side as an implication)."""
    if sub_side == "yes" and sup_side == "yes":
        return (sub_i, sup_i)
    if sub_side == "yes" and sup_side == "no":
        return "impossible"
    if sub_side == "no" and sup_side == "no":
        return (sup_i, sub_i)
    return None


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
                # Airtight tautology: YES-and-NO of one market can never both
                # settle YES ⇒ farmable.
                return Relationship(
                    RelationshipKind.IMPOSSIBLE, (), tuple(notes), farmable=True
                )
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
            # NOT farmable: this rests on the exchange's mutual-exclusivity
            # METADATA, not a logical tautology — a wrong flag would misclassify
            # a POSSIBLE combo as impossible, the one loss path farming has.
            return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))

    # LOGICAL CONTAINMENT families (A⟹B) — exact soccer-scoring facts the copula
    # cannot express, handled in the same-game block just below the corners
    # branch. See ``_containment_sign`` for the shared YES/NO sign matrix.
    types = [classify_leg(leg.market_ticker) for leg in legs]

    # Same-team TEAM-corner lines are EXACT CONTAINMENT (over-M ⊆ over-N for
    # M>N: a team with more than M corners necessarily has more than N — 0
    # violations in the tape). So within one game, for ONE team, a YES on the
    # HIGHER line with a NO on the LOWER line is logically impossible (v1 policy:
    # no-quote), mirroring the 1H-BTTS-yes × FT-BTTS-no branch below. Scoped to
    # corners_team only (game-total corners do NOT nest); same-team lower-yes ×
    # higher-no stays POSSIBLE and falls to the copula, as does the buried-in-
    # combo same-team pair (approximated by the comonotone prior, not pinned).
    corner_legs: list[tuple[int, str, int]] = []
    for i, leg in enumerate(legs):
        if types[i] is not LegType.CORNERS_TEAM:
            continue
        parsed = _corners_team_line(leg.market_ticker)
        if parsed is not None:
            corner_legs.append((i, parsed[0], parsed[1]))
    for a_i, a_team, a_line in corner_legs:
        if legs[a_i].side != "yes":
            continue
        for b_i, b_team, b_line in corner_legs:
            if b_i == a_i or legs[b_i].side != "no":
                continue
            if a_team != b_team or game_keys[a_i] != game_keys[b_i] or a_line <= b_line:
                continue
            notes.append(
                f"same-team corners over-{a_line} yes ({legs[a_i].market_ticker}) "
                f"implies over-{b_line} yes: {legs[b_i].market_ticker} no is impossible"
            )
            # Airtight nested-line containment (over-M ⊆ over-N, 0 tape
            # violations) ⇒ farmable.
            return Relationship(
                RelationshipKind.IMPOSSIBLE, (), tuple(notes), farmable=True
            )

    # Three period/line implications A⟹B are EXACT scoring facts (not
    # correlations). A Kalshi taker-API probe proved these three — and only these
    # — are reachable: a taker can actually build them; every other implication
    # is blocked at build time. For each, ``_containment_sign`` gives:
    #   {A yes, B no}  → IMPOSSIBLE (v1 no-quote; fires at ANY combo size)
    #   {A yes, B yes} → CONTAINMENT joint = P(A)     (subset = A leg)
    #   {A no,  B no}  → CONTAINMENT joint = P(B no)  (subset = B leg; ¬B⟹¬A)
    #   {A no,  B yes} → possible (falls to the copula)
    # A bare 2-leg containment is priced coherently; the SAME pair buried in a
    # >2-leg combo is not modeled → UNKNOWN (widen-or-no-quote), never a copula
    # guess. IMPOSSIBLE returns immediately, so it always beats a buried pair.
    containment: tuple[int, int] | None = None

    # Family 1 — 1H-BTTS (A) ⟹ FT-BTTS (B): both teams scoring by half-time means
    # both have scored in the match. All four sign cases reachable.
    for a_i in range(len(legs)):
        if types[a_i] is not LegType.FIRST_HALF_BTTS:
            continue
        for b_i in range(len(legs)):
            if types[b_i] is not LegType.BTTS or game_keys[a_i] != game_keys[b_i]:
                continue
            verdict = _containment_sign(a_i, b_i, legs[a_i].side, legs[b_i].side)
            if verdict == "impossible":
                notes.append(
                    f"1H-BTTS yes ({legs[a_i].market_ticker}) implies FT-BTTS yes: "
                    f"{legs[b_i].market_ticker} no is impossible"
                )
                # Airtight scoring tautology ⇒ farmable.
                return Relationship(
                    RelationshipKind.IMPOSSIBLE, (), tuple(notes), farmable=True
                )
            if isinstance(verdict, tuple):
                containment = verdict

    # Family 2 — regulation moneyline team-WIN (A) ⟹ FT Over-0.5 (B): a win needs
    # a goal. Kalshi blocks moneyline-NO legs in combos, so only the win-YES half
    # is reachable (moneyline NO orientations are NOT added). The total must be
    # the over-0.5 line (suffix -1); over-1.5+ is a possible combo (a 1-0 win is
    # under 1.5). TIE (0-0 draw) and ADVANCE (0-0 on pens, a different LegType)
    # do not imply a goal and are excluded.
    for ml_i in range(len(legs)):
        if types[ml_i] is not LegType.MONEYLINE or legs[ml_i].side != "yes":
            continue
        if not _moneyline_is_team(legs[ml_i].market_ticker):
            continue
        for tot_i in range(len(legs)):
            if types[tot_i] is not LegType.TOTAL or game_keys[ml_i] != game_keys[tot_i]:
                continue
            if _total_line(legs[tot_i].market_ticker) != _OVER_HALF_LINE:
                continue
            verdict = _containment_sign(ml_i, tot_i, "yes", legs[tot_i].side)
            if verdict == "impossible":
                notes.append(
                    f"moneyline win yes ({legs[ml_i].market_ticker}) needs a goal: "
                    f"over-0.5 {legs[tot_i].market_ticker} no is impossible"
                )
                # Airtight scoring tautology (a regulation win needs a goal)
                # ⇒ farmable.
                return Relationship(
                    RelationshipKind.IMPOSSIBLE, (), tuple(notes), farmable=True
                )
            if isinstance(verdict, tuple):
                containment = verdict

    # Family 3 — 1H Over-N (A) ⟹ FT Over-N (B), SAME line N: full-time goals ≥
    # first-half goals. Only EQUAL lines are reachable AND logically directional
    # (cross-line is blocked by Kalshi and its direction depends on which line is
    # larger), so unequal/unparseable lines do nothing.
    for fh_i in range(len(legs)):
        if types[fh_i] is not LegType.FIRST_HALF_TOTAL:
            continue
        fh_line = _total_line(legs[fh_i].market_ticker)
        if fh_line is None:
            continue
        for ft_i in range(len(legs)):
            if types[ft_i] is not LegType.TOTAL or game_keys[fh_i] != game_keys[ft_i]:
                continue
            ft_line = _total_line(legs[ft_i].market_ticker)
            if ft_line is None or ft_line != fh_line:
                continue
            verdict = _containment_sign(fh_i, ft_i, legs[fh_i].side, legs[ft_i].side)
            if verdict == "impossible":
                notes.append(
                    f"1H-over-{fh_line} yes ({legs[fh_i].market_ticker}) implies "
                    f"FT-over-{ft_line} yes: {legs[ft_i].market_ticker} no is impossible"
                )
                # Airtight scoring tautology (FT goals ≥ 1H goals) ⇒ farmable.
                return Relationship(
                    RelationshipKind.IMPOSSIBLE, (), tuple(notes), farmable=True
                )
            if isinstance(verdict, tuple):
                containment = verdict

    if containment is not None:
        if len(legs) != 2:
            notes.append("logical containment pair inside a larger combo: not modeled")
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
        notes.append(
            f"logical containment: joint = P(leg {containment[0]} "
            f"{legs[containment[0]].market_ticker})"
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
