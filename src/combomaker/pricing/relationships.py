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
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from combomaker.pricing.conditionals_mlb import (
    BATTER_FAMILIES,
    is_exact,
    strongest_measured_direction,
)
from combomaker.pricing.legtypes import LegType, Sport, classify_leg, classify_sport

# The ONE anchored MLB team parser (reviewer defect #3): the game-blob /
# end-anchoring helpers live in sgp.py (prefix=away, suffix=home,
# both-or-neither refuses; provably unambiguous on the live-enumerated 30-code
# vocabulary). Imported — not mirrored — so the parse can never drift.
from combomaker.pricing.sgp import _mlb_side_of, _mlb_team_blob
from combomaker.rfq.models import RfqLeg


class RelationshipKind(StrEnum):
    OK = "ok"                    # classified; groups usable for correlation
    IMPOSSIBLE = "impossible"    # logically zero payout — v1: no-quote
    UNKNOWN = "unknown"          # classification failed — widen-or-no-quote
    CONTAINMENT = "containment"  # one leg logically implies another (joint pinned)
    # yes-LOW + no-HIGH rungs of one nested ladder: the pair's joint is EXACT
    # arithmetic P(over-low) − P(over-high) — no ρ. The engine collapses each
    # band pair into a super-leg before any copula runs.
    NESTED_BAND = "nested_band"


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
    # NESTED_BAND + N-leg CONTAINMENT collapse: (low_i, high_i) leg-index pairs
    # whose joint is the EXACT window arithmetic P(low selected-YES event) −
    # P(high selected-NO event's complement) = P(low) − P(high). On
    # NESTED_BAND: same-ladder rungs (LegType + game + scope), low_i the LOWER
    # over-line selected YES, high_i the HIGHER selected NO. On the N-leg
    # CONTAINMENT return: additionally the containment families' {A no, B yes}
    # window pairs (low_i = the superset-YES leg, high_i = the subset-NO leg).
    # Guaranteed pairwise disjoint, each band's game holding ONLY its two legs
    # among the post-collapse kept set. Empty for every other kind.
    bands: tuple[tuple[int, int], ...] = ()
    # N-leg CONTAINMENT collapse ONLY (2026-07-11): every recorded
    # (subset_index, superset_index) pair in SELECTED-side space — the selected
    # event of leg [0] is a logical subset of leg [1]'s, so the superset leg is
    # implied and the engine drops it (the pair's joint IS the subset leg's
    # selected marginal, exactly price_containment's rule). The bare 2-leg pair
    # keeps the single ``containment`` field + price_containment path; this
    # tuple is populated only where the old "containment pair inside a larger
    # combo" UNKNOWN decline used to fire. Empty for every other kind.
    containments: tuple[tuple[int, int], ...] = ()


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


def _match_ladder_line(market_ticker: str) -> tuple[str, int] | None:
    """(scope, over-line) for a MATCH-level ladder ticker whose suffix is the
    bare line digits (``KXWCCORNERS-…-8`` -> ``("", 8)``; rule-book verified:
    strike_type greater_or_equal, so ``-8`` = "8 or more"). Scope ``""`` = the
    whole match. None when the suffix isn't pure digits — never guess a line."""
    suffix = market_ticker.rsplit("-", 1)[-1]
    if _TOTAL_LINE.match(suffix):
        return "", int(suffix)
    return None


def _team_ladder_line(market_ticker: str) -> tuple[str, int] | None:
    """(scope, over-line) for a TEAM-scoped ladder ticker whose suffix
    concatenates TEAM+LINE with no separator (``…-MAR4`` -> ``("MAR", 4)``)."""
    return _corners_team_line(market_ticker)


# --- Nested-ladder registry ------------------------------------------------------
# A ladder = one family's over-lines on ONE counting variable, so over-H ⊆ over-L
# for H > L is EXACT monotone containment (KXWCCORNERS rules verified 2026-07-09:
# every rung of an event settles on the SAME combined count — "regulation,
# stoppage and any extra time periods", strike_type greater_or_equal). Value =
# suffix parser returning (scope, line); scope is the within-game unit ("" =
# whole match, a team code for team ladders, a player code if a player ladder
# ever lists), so the branch keys on LegType + game + scope + numeric line —
# NOT on soccer specifics. A future nested family (e.g. MLB TOTAL if Kalshi
# lifts size_max: over-9.5 YES + over-11.5 NO must price exactly) is ONE entry
# here, gated on a validator/settlement probe of that family's own tickers.
# TOTAL / FIRST_HALF_TOTAL / TEAM_TOTAL are deliberately withheld: their events
# carry size_max=1 today (no band buildable), so an entry would only add
# unprobed farm surface.
_NESTED_LADDER_FAMILIES: dict[LegType, Callable[[str], tuple[str, int] | None]] = {
    LegType.CORNERS: _match_ladder_line,
    LegType.CORNERS_TEAM: _team_ladder_line,
}


def _moneyline_is_team(market_ticker: str) -> bool:
    """True iff a MONEYLINE leg's suffix names a TEAM (not the TIE/DRAW side). A
    team win requires outscoring the opponent in regulation (>=1 goal); a draw
    settles on a 0-0-inclusive result and implies no goal. Fail-closed: an empty
    suffix is not a team."""
    suffix = market_ticker.rsplit("-", 1)[-1].upper()
    return bool(suffix) and suffix not in _DRAW_SUFFIXES


# --- MLB same-player / winner-spread parsing -------------------------------------
# Batter-stat leg types covered by the same-player conditional table (KS is
# deliberately absent — a starter's Ks and a batter's stats are different
# entities, so their player segments can never match; no branch needed).
_MLB_BATTER_TYPES = frozenset(BATTER_FAMILIES)


def _mlb_prop_entity(market_ticker: str) -> tuple[str, str, int] | None:
    """(raw game-code segment, player/entity segment, rung) for an MLB
    player-prop ticker (``KXMLBHR-26JUL092145COLSF-COLHGOODMAN15-1`` ->
    ``("26JUL092145COLSF", "COLHGOODMAN15", 1)``). Shape guards mirror
    sgp._mlb_prop_pair_prior / _mlb_same_player_conditional_prior (keep in
    sync): exactly 4 hyphen segments, a game code whose team blob parses
    (doubleheader G<digit> tolerated but the RAW segment is returned, so a
    G1 x G2 pair can never merge), and a digits-only line suffix (``-N`` =
    "N or more", floor_strike N-0.5). None on any doubt — never guess an
    entity or a rung for containment logic."""
    if _mlb_team_blob(market_ticker) is None:
        return None
    parts = market_ticker.upper().split("-")
    if len(parts) != 4 or not parts[2]:
        return None
    if not _TOTAL_LINE.match(parts[3]):
        return None
    return parts[1], parts[2], int(parts[3])


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


def _collapse_containments(
    legs: tuple[RfqLeg, ...] | list[RfqLeg],
    containments: list[tuple[int, int]],
    ladder_bands: list[tuple[int, int]],
    cont_bands: list[tuple[int, int]],
    game_keys: list[str],
    notes: list[str],
) -> Relationship:
    """The CONTAINMENT-IN-LARGER-COMBO collapse plan (2026-07-11) — replaces
    the old "logical containment pair inside a larger combo: not modeled"
    UNKNOWN decline and fires ONLY where that decline fired (>=1 recorded
    containment pair, combo size != 2).

    Per recorded pair the engine collapses exactly like a nested band
    (superset legs drop; {A no, B yes} window pairs become band super-legs
    P(B) − P(A)), then prices the reduced set. This function only validates
    the STRUCTURE and fails closed to UNKNOWN on any shape the collapse
    arithmetic cannot represent:

    - every dropped (implied) superset leg must trace to a KEPT subset witness
      through the recorded subset links (a hypothetical mutual A⊆B⊆A cycle
      would drop both legs and silently lose the constraint);
    - a leg may hold at most ONE collapse role: band legs must be disjoint
      from every containment pair and from other bands;
    - a band super-leg is a WINDOW event (non-monotone in the latent count),
      so its game must hold ONLY the band's two legs among the KEPT set — the
      post-collapse mirror of the NESTED_BAND same-game-companion guard.
    """
    pairs = list(dict.fromkeys(containments))
    band_pairs = list(dict.fromkeys([*ladder_bands, *cont_bands]))
    dropped = {sup for _sub, sup in pairs}
    subsets_of: dict[int, list[int]] = {}
    for sub, sup in pairs:
        subsets_of.setdefault(sup, []).append(sub)
    for start in dropped:
        stack, seen = [start], set()
        witnessed = False
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur not in dropped:
                witnessed = True
                break
            stack.extend(subsets_of.get(cur, []))
        if not witnessed:
            notes.append(
                "containment collapse: implied leg without a kept subset "
                "witness (cyclic implication) — not modeled"
            )
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
    cont_legs = {i for pair in pairs for i in pair}
    used_band_legs: set[int] = set()
    for low_i, high_i in band_pairs:
        if {low_i, high_i} & (cont_legs | used_band_legs):
            notes.append(
                "containment collapse: leg holds more than one collapse role "
                "(band/containment overlap) — not modeled"
            )
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
        used_band_legs |= {low_i, high_i}
    consumed = dropped | {high_i for _low_i, high_i in band_pairs}
    kept = [i for i in range(len(legs)) if i not in consumed]
    for low_i, _high_i in band_pairs:
        game = game_keys[low_i]
        if any(game_keys[k] == game for k in kept if k != low_i):
            notes.append(
                f"nested band game {game} carries other kept legs: "
                "band-vs-neighbour correlation unmodeled"
            )
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
    # Same-game groups over ALL legs (the engine remaps them onto the reduced
    # set after the drops — the _price_nested_bands precedent).
    by_game: dict[str, list[int]] = {}
    for i, key in enumerate(game_keys):
        by_game.setdefault(key, []).append(i)
    groups = tuple(tuple(idx) for idx in by_game.values() if len(idx) >= 2)
    for sub, sup in pairs:
        notes.append(
            f"containment collapse: {legs[sup].market_ticker} implied by "
            f"{legs[sub].market_ticker} — superset leg drops"
        )
    for low_i, high_i in cont_bands:
        notes.append(
            f"containment band {legs[low_i].market_ticker} yes + "
            f"{legs[high_i].market_ticker} no: joint = P(superset) - P(subset)"
        )
    return Relationship(
        RelationshipKind.CONTAINMENT,
        groups,
        tuple(notes),
        bands=tuple(band_pairs),
        containments=tuple(pairs),
    )


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

    # LOGICAL CONTAINMENT families (A⟹B) — exact scoring/arithmetic facts the
    # copula cannot express: the nested ladders and soccer families below, plus
    # the MLB same-player and winner×spread families (2026-07-10). See
    # ``_containment_sign`` for the shared YES/NO sign matrix.
    types = [classify_leg(leg.market_ticker) for leg in legs]

    # NESTED LADDERS (registry above): same family + same game + same scope,
    # different over-lines ⇒ over-H ⊆ over-L exactly (H > L). Verdicts follow
    # ``_containment_sign`` with A = the HIGHER line (subset), B = the LOWER:
    #   {A yes, B no}  → IMPOSSIBLE, farmable (≥H ∧ <L is an airtight
    #                    contradiction on one count — the corners_team farm,
    #                    now family-generic incl. match corners)
    #   {A yes, B yes} → containment joint = P(over-H). Exchange BLOCKS
    #                    same-side rungs (400 duplicated_legs, 0 in 3.02M tape
    #                    combos) — defensive branch.
    #   {A no,  B no}  → containment joint = P(over-L no) (¬B⟹¬A); also
    #                    exchange-blocked today, defensive.
    #   {A no,  B yes} → NESTED BAND "count in [L, H)": ALLOWED by the
    #                    side-aware validator (114 real tape combos) and EXACT:
    #                    joint = P(over-L) − P(over-H). The copula cannot
    #                    express a difference of marginals (match-corner bands
    #                    were falling to the flat +0.6 fallback).
    ladder_legs: list[tuple[int, str, int]] = []
    for i, leg in enumerate(legs):
        parse = _NESTED_LADDER_FAMILIES.get(types[i])
        if parse is None:
            continue
        parsed = parse(leg.market_ticker)
        if parsed is not None:
            # Nesting key: family + game + within-game scope. SAME LegType only —
            # cross-window nesting (1H vs FT) belongs to Family 3 below.
            ladder_legs.append((i, f"{types[i]}|{game_keys[i]}|{parsed[0]}", parsed[1]))
    # Every recorded (subset, superset) containment pair in SELECTED-side
    # space. A bare 2-leg combo keeps the shipped last-write-wins single-pair
    # return; a larger combo hands the FULL list to the collapse plan.
    containments: list[tuple[int, int]] = []
    # The containment families' {A no, B yes} window pairs (low = superset-YES
    # leg, high = subset-NO leg; joint = P(B) − P(A), the nested-band mirror).
    # Consumed ONLY by the N-leg collapse plan below — a combo without a
    # recorded containment pair keeps its existing (copula/band) path.
    cont_bands: list[tuple[int, int]] = []
    bands: list[tuple[int, int]] = []
    for a_i, a_key, a_line in ladder_legs:      # A = higher line (subset)
        for b_i, b_key, b_line in ladder_legs:  # B = lower line (superset)
            if a_i == b_i or a_key != b_key or a_line <= b_line:
                continue
            verdict = _containment_sign(a_i, b_i, legs[a_i].side, legs[b_i].side)
            if verdict == "impossible":
                notes.append(
                    f"nested ladder over-{a_line} yes ({legs[a_i].market_ticker}) "
                    f"implies over-{b_line} yes: {legs[b_i].market_ticker} no is "
                    "impossible"
                )
                # Airtight nested-line containment tautology ⇒ farmable.
                return Relationship(
                    RelationshipKind.IMPOSSIBLE, (), tuple(notes), farmable=True
                )
            if isinstance(verdict, tuple):
                containments.append(verdict)
            else:
                # Sides are validated yes/no above, so the remaining case is
                # exactly {A no, B yes}: the band, stored as (low_i, high_i).
                bands.append((b_i, a_i))
                notes.append(
                    f"nested band [{b_line},{a_line}) "
                    f"{legs[b_i].market_ticker} yes + {legs[a_i].market_ticker} no: "
                    "joint = P(low) - P(high)"
                )

    # MLB SAME-PLAYER cross-stat batter pairs (DO-2, 2026-07-10): HIT/HR/TB/HRR
    # of ONE batter-game (identical entity segment, trailing line stripped into
    # the rung). The distinct-player [D] rhos are WRONG for the same player
    # (the sweep regression: HIT×HR truth is containment-shaped, not +0.01), so
    # a same-player pair must NEVER fall through to them. Operator-approved
    # policy, driven by conditionals_mlb.SAME_PLAYER_CONDITIONALS:
    #   'exact' cells (arithmetic containments, verified == 1.0 pooled AND
    #     per-era) -> the ``_containment_sign`` verdicts. IMPOSSIBLE verdicts
    #     are NEVER farmable: MLB's 48h-postponement rule settles markets
    #     SCALAR, which breaks the airtight certain-NO bar the farm requires
    #     (unlike soccer's one-count tautologies).
    #   'measured' cells with n >= MIN_CONDITIONAL_N -> conditional-table
    #     pricing (joint = P(conditioning leg) × p_cond) via the sgp.py
    #     implied-rho seam — BARE 2-leg pairs only; buried partials decline
    #     UNKNOWN (soccer bare-pair precedent).
    #   anything else (unmeasured cell, truncated table region, unparseable
    #     rung) -> UNKNOWN — widen-or-no-quote, never the distinct-player rho.
    # Same-player SAME-family rungs (a nested ladder, e.g. HIT-1 × HIT-2) are
    # deliberately NOT handled this step: no table cell exists, and the ladder
    # registry's farm path must not fire for MLB (scalar settlement).
    batter_entities: dict[int, tuple[str, str, int]] = {}
    for i, leg in enumerate(legs):
        if types[i] in _MLB_BATTER_TYPES:
            entity = _mlb_prop_entity(leg.market_ticker)
            if entity is not None:
                batter_entities[i] = entity
    batter_indices = sorted(batter_entities)
    for x in range(len(batter_indices)):
        for y in range(x + 1, len(batter_indices)):
            a_i, b_i = batter_indices[x], batter_indices[y]
            game_a, seg_a, rung_a = batter_entities[a_i]
            game_b, seg_b, rung_b = batter_entities[b_i]
            if game_a != game_b or seg_a != seg_b:
                continue  # different game or player: teammate/opp routing owns it
            fam_a = BATTER_FAMILIES[types[a_i]]
            fam_b = BATTER_FAMILIES[types[b_i]]
            if fam_a == fam_b:
                continue  # same-family rung ladder: out of scope this step
            pinned = False
            for sub, sup in ((a_i, b_i), (b_i, a_i)):
                sub_fam, sub_rung = (fam_a, rung_a) if sub == a_i else (fam_b, rung_b)
                sup_fam, sup_rung = (fam_b, rung_b) if sub == a_i else (fam_a, rung_a)
                if not is_exact(sub_fam, sub_rung, sup_fam, sup_rung):
                    continue
                verdict = _containment_sign(sub, sup, legs[sub].side, legs[sup].side)
                if verdict == "impossible":
                    notes.append(
                        f"same-player {sub_fam}-{sub_rung} yes "
                        f"({legs[sub].market_ticker}) implies {sup_fam}-{sup_rung} "
                        f"yes: {legs[sup].market_ticker} no is impossible"
                    )
                    # NOT farmable: MLB scalar settlement (48h postponement)
                    # breaks the airtight bar.
                    return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))
                if isinstance(verdict, tuple):
                    containments.append(verdict)
                    pinned = True
                    break
                # verdict None ({subset no, superset yes}): not a containment —
                # a measured reverse cell may still price it below.
            if pinned:
                continue
            if strongest_measured_direction(fam_a, rung_a, fam_b, rung_b) is not None:
                if len(legs) != 2:
                    notes.append(
                        "same-player conditional pair inside a larger combo: "
                        "not modeled"
                    )
                    return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
                notes.append(
                    f"same-player conditional cell {fam_a}-{rung_a} x "
                    f"{fam_b}-{rung_b}: priced via conditional table (sgp)"
                )
                continue  # bare pair falls through to OK; the sgp seam prices it
            notes.append(
                f"same-player cross-stat pair {fam_a}-{rung_a} x {fam_b}-{rung_b} "
                "unmeasured: widen-or-no-quote"
            )
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))

    # MLB MONEYLINE × SPREAD, same game (DO-3, 2026-07-10). The spread suffix is
    # TEAM+line ("wins by over N-0.5", so any N >= 1 forces a win margin >= 1):
    # SAME-team cover ⟹ win is a scoring containment — ``_containment_sign``
    # with A = the spread leg. OPPOSITE teams (BOTH sides resolved via the ONE
    # anchored parser — raw suffix inequality alone is NOT proof, reviewer
    # defect #3) make cover-yes × win-yes mutually exclusive ⇒ IMPOSSIBLE, but
    # NEVER farmable: MLB's 48h-postponement rule settles markets SCALAR,
    # breaking the airtight certain-NO bar (the soccer farm precedent does NOT
    # transfer). Unresolvable either side falls through to the copula, where
    # sgp routes :same/:opp (±0.95) and parse-failures hit the plain
    # sign-spanning 0.00 fallback. MLB-gated: soccer/NFL ml|spread keep their
    # structural/copula paths untouched.
    for ml_i in range(len(legs)):
        if types[ml_i] is not LegType.MONEYLINE:
            continue
        if classify_sport(legs[ml_i].market_ticker) is not Sport.MLB:
            continue
        game_m = _mlb_team_blob(legs[ml_i].market_ticker)
        if game_m is None:
            continue
        ml_side = _mlb_side_of(
            legs[ml_i].market_ticker.upper().rsplit("-", 1)[-1], game_m[1]
        )
        if ml_side is None:
            continue
        for sp_i in range(len(legs)):
            if types[sp_i] is not LegType.SPREAD:
                continue
            if classify_sport(legs[sp_i].market_ticker) is not Sport.MLB:
                continue
            game_s = _mlb_team_blob(legs[sp_i].market_ticker)
            if game_s is None or game_s != game_m:
                continue
            parsed_spread = _corners_team_line(legs[sp_i].market_ticker)
            if parsed_spread is None or parsed_spread[1] < 1:
                continue  # need TEAM + a line N >= 1 for cover ⟹ win
            spread_side = _mlb_side_of(parsed_spread[0], game_s[1])
            if spread_side is None:
                continue
            if spread_side == ml_side:
                verdict = _containment_sign(
                    sp_i, ml_i, legs[sp_i].side, legs[ml_i].side
                )
                if verdict == "impossible":
                    notes.append(
                        f"spread cover yes ({legs[sp_i].market_ticker}) implies "
                        f"the win: {legs[ml_i].market_ticker} no is impossible"
                    )
                    # NOT farmable: MLB scalar settlement (48h postponement).
                    return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))
                if isinstance(verdict, tuple):
                    containments.append(verdict)
                # verdict None ({cover no, win yes}): possible — copula :same.
                # Deliberately NOT a cont_band: MLB settles scalar under the
                # 48h-postponement rule, so the binary window arithmetic is
                # not airtight (mirror of the ladder registry's MLB absence).
            elif legs[sp_i].side == "yes" and legs[ml_i].side == "yes":
                notes.append(
                    f"spread cover yes ({legs[sp_i].market_ticker}) and the "
                    f"OPPOSITE team's win yes ({legs[ml_i].market_ticker}) are "
                    "mutually exclusive"
                )
                # NOT farmable: MLB scalar settlement (48h postponement).
                return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))
            # other opposite-team sign cases: possible — copula :opp.

    # Three period/line implications A⟹B are EXACT scoring facts (not
    # correlations). A Kalshi taker-API probe proved these three — and only these
    # — are reachable: a taker can actually build them; every other implication
    # is blocked at build time. For each, ``_containment_sign`` gives:
    #   {A yes, B no}  → IMPOSSIBLE (v1 no-quote; fires at ANY combo size)
    #   {A yes, B yes} → CONTAINMENT joint = P(A)     (subset = A leg)
    #   {A no,  B no}  → CONTAINMENT joint = P(B no)  (subset = B leg; ¬B⟹¬A)
    #   {A no,  B yes} → possible (falls to the copula)
    # A bare 2-leg containment is priced via price_containment; the SAME pair
    # buried in a >2-leg combo collapses through _collapse_containments
    # (2026-07-11 — used to decline UNKNOWN), whose guards still fail closed
    # on any shape the collapse arithmetic cannot represent. IMPOSSIBLE
    # returns immediately, so it always beats a buried pair.

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
                containments.append(verdict)
            elif legs[a_i].side == "no" and legs[b_i].side == "yes":
                # {A no, B yes} = FT-BTTS without 1H-BTTS: an exact window
                # P(B) − P(A). Recorded for the N-leg collapse ONLY — a combo
                # without a pinned containment keeps its shipped copula path.
                cont_bands.append((b_i, a_i))

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
                containments.append(verdict)
            # No band case here: the loop enters on moneyline-YES only (Kalshi
            # blocks moneyline-NO legs in combos), so {A no, B yes} can't occur.

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
                containments.append(verdict)
            elif legs[fh_i].side == "no" and legs[ft_i].side == "yes":
                # {A no, B yes} = FT-over-N without 1H-over-N: exact window
                # P(B) − P(A), recorded for the N-leg collapse ONLY (see
                # Family 1).
                cont_bands.append((ft_i, fh_i))

    if containments:
        if len(legs) != 2:
            # CONTAINMENT-IN-LARGER-COMBO collapse (2026-07-11): exactly where
            # the "not modeled" UNKNOWN decline used to fire. The engine drops
            # each implied superset leg / collapses each window pair into a
            # band super-leg, then prices the reduced set — every combo that
            # priced before this change is untouched (this branch was
            # UNKNOWN), and the bare 2-leg pair below keeps price_containment.
            return _collapse_containments(
                legs, containments, bands, cont_bands, game_keys, notes
            )
        containment = containments[-1]  # shipped last-write-wins 2-leg pair
        notes.append(
            f"logical containment: joint = P(leg {containment[0]} "
            f"{legs[containment[0]].market_ticker})"
        )
        return Relationship(RelationshipKind.CONTAINMENT, (), tuple(notes), containment)

    # Pass 2 — per-GAME: the correlation blocks. Same-game legs from DIFFERENT
    # market families share a game code but not an event_ticker, so they must be
    # grouped here or they price independent (the bug this fixes). Shared by the
    # NESTED_BAND and OK returns.
    by_game: dict[str, list[int]] = {}
    for i, key in enumerate(game_keys):
        by_game.setdefault(key, []).append(i)
    groups: list[tuple[int, ...]] = []
    for key, indices in by_game.items():
        if len(indices) < 2:
            continue
        groups.append(tuple(indices))
        notes.append(f"same-game group {key}: {len(indices)} legs")

    if bands:
        # A band is modeled ONLY as an isolated pair: each band's game must
        # contain exactly its two rungs. A third same-game leg (incl. a second
        # band or a 3-rung shape) is declined UNKNOWN: a band is a WINDOW event,
        # non-monotone in the latent count, so its correlation to a same-game
        # neighbour is the rung's ρ ATTENUATED by an unmeasured factor (bites
        # hardest on corners|corners_team 0.62) — widen-or-no-quote, never a
        # copula guess. Cross-game companions are fine (cross_event_rho
        # machinery, where representing the band by its low rung is exact).
        # Disjointness follows: a shared rung would put ≥3 legs in one game.
        for low_i, _high_i in bands:
            game = game_keys[low_i]
            if sum(1 for k in game_keys if k == game) != 2:
                notes.append(
                    f"nested band game {game} carries other legs: "
                    "band-vs-neighbour correlation unmodeled"
                )
                return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
        return Relationship(
            RelationshipKind.NESTED_BAND,
            tuple(groups),
            tuple(notes),
            bands=tuple(bands),
        )

    return Relationship(RelationshipKind.OK, tuple(groups), tuple(notes))
