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
from combomaker.pricing.tripwire import taxonomy_impossible
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
    # NESTED_BAND: same-ladder rungs (LegType + game + scope) with low_i the
    # LOWER over-line selected YES / high_i the HIGHER selected NO, PLUS (the
    # 2026-07-11 universal-window rule) the containment families' {A no, B yes}
    # window pairs (low_i = the superset-YES leg, high_i = the subset-NO leg) —
    # both are the same arithmetic P(B) − P(A). On the N-leg CONTAINMENT
    # return: the same two species, consumed by the collapse plan. Guaranteed
    # pairwise disjoint, each band's game holding ONLY its two legs among the
    # post-collapse kept set. Empty for every other kind.
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
    # N-leg collapse ONLY (2026-07-11, WIRE-4): every recorded same-player
    # MEASURED-conditional pair (kept_index, dropped_index) buried in a >2-leg
    # combo (the shape that used to decline UNKNOWN "not modeled"). The engine
    # collapses each pair into a super-leg whose p is the SAME 2-leg selected-
    # side joint the bare path prices through the sgp implied-rho seam
    # (conditionals_mlb.SAME_PLAYER_CONDITIONALS), u the pair joint's — the
    # bare 2-leg pair itself never records here (it keeps the OK/sgp path,
    # bit-identical). Empty for every other kind.
    conditionals: tuple[tuple[int, int], ...] = ()


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


# --- Spread scope registries (2026-07-11) -----------------------------------------
# SOCCER spread cover ⟹ win, SCOPE-MATCHED (S12 + its 1H analog S6): a KXWCSPREAD
# leg settles on the END-OF-REGULATION scoreline exactly like KXWCGAME (NOTES.md
# I8, rule-book verified), so FT spread pairs ONLY the regulation moneyline; a
# KXWC1HSPREAD leg settles on the half-time scoreline exactly like the KXWC1H
# winner, so 1H spread pairs ONLY the 1H moneyline. Cross-scope spread⟹win pairs
# (a 1H lead does NOT force the FT win) are deliberately ABSENT.
_SOCCER_SPREAD_WIN_SCOPES: dict[LegType, LegType] = {
    LegType.SPREAD: LegType.MONEYLINE,
    LegType.FIRST_HALF_SPREAD: LegType.FIRST_HALF_MONEYLINE,
}

# (1H-)spread cover-by-N ⟹ total ≥ N, SCOPE-NESTED (S7/S8/S13/S34): valid iff the
# spread's scope is CONTAINED in the total's. Soccer: FT spread and FT total both
# settle END OF REGULATION (NOTES.md I8) — same scope; 1H spread nests in both
# the 1H total and the regulation total (goals persist; the MARGIN does not, but
# the implication only needs the winner's own 1H goals). MLB: spread and total
# both settle on the final score INCLUDING extra innings — and extras only ADD
# runs, so the implication stays airtight. FT-spread ⟹ 1H-total is deliberately
# ABSENT (a regulation margin does not bound first-half goals).
_SPREAD_TOTAL_SCOPES: dict[LegType, frozenset[LegType]] = {
    LegType.SPREAD: frozenset({LegType.TOTAL}),
    LegType.FIRST_HALF_SPREAD: frozenset({LegType.FIRST_HALF_TOTAL, LegType.TOTAL}),
}


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
    conditionals: list[tuple[int, int]],
    game_keys: list[str],
    notes: list[str],
) -> Relationship:
    """The CONTAINMENT-IN-LARGER-COMBO collapse plan (2026-07-11) — replaces
    the old "logical containment pair inside a larger combo: not modeled"
    UNKNOWN decline and fires ONLY where that decline fired (>=1 recorded
    containment/conditional pair, combo size != 2).

    Per recorded pair the engine collapses exactly like a nested band
    (superset legs drop; {A no, B yes} window pairs become band super-legs
    P(B) − P(A); same-player MEASURED-conditional pairs become super-legs
    whose p is the bare path's 2-leg conditional joint — WIRE-4). This
    function only validates the STRUCTURE and fails closed to UNKNOWN on any
    shape the collapse arithmetic cannot represent:

    - every dropped (implied) superset leg must trace to a KEPT subset witness
      through the recorded subset links (a hypothetical mutual A⊆B⊆A cycle
      would drop both legs and silently lose the constraint);
    - a leg may hold at most ONE collapse role: band legs and conditional
      pairs must be disjoint from every containment pair, from each other,
      and from other pairs of their own species;
    - a band super-leg is a WINDOW event (non-monotone in the latent count),
      so its game must hold ONLY the band's two legs among the KEPT set — the
      post-collapse mirror of the NESTED_BAND same-game-companion guard;
    - a CONDITIONAL super-leg gets the SAME isolation guard (V2 REFUTATION,
      2026-07-11): the super-leg carries the SELECTED-side pair joint but is
      represented by its kept leg's ticker at side "yes", so for NO-side
      mixes the copula applies the kept leg's YES–YES rho to an event that is
      ANTI-monotone in the kept leg's latent — the neighbour-correlation SIGN
      inverts (live counterexample: HIT3-no x HR1-no x own-ML-yes priced
      0.4183 vs 0.3451 trivariate truth, +7.32c). Fail-closed doctrine: a
      same-game KEPT companion ⇒ UNKNOWN decline for EVERY side mix;
      cross-game companions (ρ = 0, the bulk of the observed decliner
      population) stay priceable — representing the pair by its kept leg is
      exact at ρ = 0.
    """
    pairs = list(dict.fromkeys(containments))
    band_pairs = list(dict.fromkeys([*ladder_bands, *cont_bands]))
    cond_pairs = list(dict.fromkeys(conditionals))
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
    used_cond_legs: set[int] = set()
    for keep_i, drop_i in cond_pairs:
        if {keep_i, drop_i} & (cont_legs | used_band_legs | used_cond_legs):
            notes.append(
                "containment collapse: leg holds more than one collapse role "
                "(conditional overlap) — not modeled"
            )
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
        used_cond_legs |= {keep_i, drop_i}
    consumed = (
        dropped
        | {high_i for _low_i, high_i in band_pairs}
        | {drop_i for _keep_i, drop_i in cond_pairs}
    )
    kept = [i for i in range(len(legs)) if i not in consumed]
    for low_i, _high_i in band_pairs:
        game = game_keys[low_i]
        if any(game_keys[k] == game for k in kept if k != low_i):
            notes.append(
                f"nested band game {game} carries other kept legs: "
                "band-vs-neighbour correlation unmodeled"
            )
            return Relationship(RelationshipKind.UNKNOWN, (), tuple(notes))
    # Conditional pairs: the SAME isolation guard (V2 refutation 2026-07-11
    # — see the docstring): a same-game kept companion sees the super-leg
    # through the kept leg's YES-side rho, whose SIGN is wrong for NO-side
    # mixes. Guard applies to every mix (fail-closed); cross-game companions
    # stay priceable. Keep in sync with the defensive mirrors in
    # engine._price_nested_bands and tools/backtests/{wc,mlb}_backtest.py.
    for keep_i, _drop_i in cond_pairs:
        game = game_keys[keep_i]
        if any(game_keys[k] == game for k in kept if k != keep_i):
            notes.append(
                f"conditional super-leg game {game} carries other kept legs: "
                "conditional-vs-neighbour correlation sign unmodeled"
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
    return Relationship(
        RelationshipKind.CONTAINMENT,
        groups,
        tuple(notes),
        bands=tuple(band_pairs),
        containments=tuple(pairs),
        conditionals=tuple(cond_pairs),
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
    # UNIVERSAL EXACT WINDOW rule (2026-07-11, WIRE-1): consumed by the N-leg
    # collapse plan when a containment/conditional pair is also present, and
    # otherwise merged into the NESTED_BAND return — so the bare 2-leg window
    # and the embedded window price the SAME exact arithmetic instead of the
    # old copula fallback.
    cont_bands: list[tuple[int, int]] = []
    # Same-player MEASURED-conditional pairs buried in a >2-leg combo
    # (kept_i, dropped_i) — the WIRE-4 collapse; bare pairs never record here.
    conditionals: list[tuple[int, int]] = []
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
                    # EMBEDDED CONDITIONAL COLLAPSE (2026-07-11, WIRE-4 — was
                    # the "not modeled" UNKNOWN decline): the plan collapses
                    # the pair into a super-leg whose p is the SAME 2-leg
                    # conditional joint the bare path prices via the sgp
                    # implied-rho seam; the bare 2-leg pair below stays
                    # bit-identical (OK → sgp).
                    conditionals.append((a_i, b_i))
                    notes.append(
                        f"same-player conditional pair {fam_a}-{rung_a} x "
                        f"{fam_b}-{rung_b} in a larger combo: collapses to a "
                        "conditional super-leg"
                    )
                    continue
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
                elif legs[sp_i].side == "no" and legs[ml_i].side == "yes":
                    # {cover no, win yes} = win-NOT-by-N (S33-ny): the exact
                    # window P(win) − P(cover), the 2026-07-11 universal-window
                    # rule (operator-directed; replaces the :same ±0.95 copula
                    # route). Window PRICING is unaffected by the 48h
                    # rain-scalar policy — that policy only gates FARMING
                    # (impossible mixes stay not-farmable above).
                    cont_bands.append((ml_i, sp_i))
                    notes.append(
                        f"containment window {legs[ml_i].market_ticker} yes "
                        f"without {legs[sp_i].market_ticker}: "
                        "joint = P(superset) - P(subset)"
                    )
            elif legs[sp_i].side == "yes" and legs[ml_i].side == "yes":
                notes.append(
                    f"spread cover yes ({legs[sp_i].market_ticker}) and the "
                    f"OPPOSITE team's win yes ({legs[ml_i].market_ticker}) are "
                    "mutually exclusive"
                )
                # NOT farmable: MLB scalar settlement (48h postponement).
                return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))
            # other opposite-team sign cases: possible — copula :opp.

    # SOCCER SPREAD cover ⟹ WIN, scope-matched (S12 + 1H analog, 2026-07-11
    # WIRE-1; mirrors the shipped MLB ml|spread family above). The spread
    # suffix is TEAM+line ("wins/leads by over N-0.5", so any N >= 1 forces a
    # win margin >= 1): SAME-team cover ⟹ win within the SAME scope — see
    # ``_SOCCER_SPREAD_WIN_SCOPES`` (FT spread × regulation ML; 1H spread × 1H
    # winner; both scope pairs settle on ONE scoreline, NOTES.md I8). 3-way
    # results are immaterial: win ⊇ cover for the same team, so the window
    # arithmetic P(win) − P(cover) is unaffected by draws. Same-team is proven
    # by suffix EQUALITY within one game (draw suffixes excluded); soccer has
    # NO anchored two-team parse (game codes concatenate variable-length team
    # codes), so a non-equal suffix proves nothing — opposite-team pairs fall
    # through to the copula untouched (reviewer-defect-#3 discipline). The
    # impossible mix {cover yes, win no} is an airtight regulation scoring
    # tautology ⇒ farmable (win-NO legs are exchange-blocked — defensive).
    for sp_i in range(len(legs)):
        win_type = _SOCCER_SPREAD_WIN_SCOPES.get(types[sp_i])
        if win_type is None:
            continue
        if classify_sport(legs[sp_i].market_ticker) is not Sport.SOCCER:
            continue
        parsed_spread = _corners_team_line(legs[sp_i].market_ticker)
        if parsed_spread is None or parsed_spread[1] < 1:
            continue  # need TEAM + a line N >= 1 for cover ⟹ win
        for ml_i in range(len(legs)):
            if types[ml_i] is not win_type or game_keys[sp_i] != game_keys[ml_i]:
                continue
            if classify_sport(legs[ml_i].market_ticker) is not Sport.SOCCER:
                continue
            if not _moneyline_is_team(legs[ml_i].market_ticker):
                continue  # a draw side is never implied by a team's cover
            ml_team = legs[ml_i].market_ticker.rsplit("-", 1)[-1].upper()
            if ml_team != parsed_spread[0]:
                continue  # not provably the same team: never a containment claim
            verdict = _containment_sign(sp_i, ml_i, legs[sp_i].side, legs[ml_i].side)
            if verdict == "impossible":
                notes.append(
                    f"spread cover yes ({legs[sp_i].market_ticker}) implies "
                    f"the win: {legs[ml_i].market_ticker} no is impossible"
                )
                # Airtight one-scoreline scoring tautology ⇒ farmable.
                return Relationship(
                    RelationshipKind.IMPOSSIBLE, (), tuple(notes), farmable=True
                )
            if isinstance(verdict, tuple):
                containments.append(verdict)
            elif legs[sp_i].side == "no" and legs[ml_i].side == "yes":
                # {cover no, win yes} = win-NOT-by-N (S12-ny, the 637-combo
                # tape cell): exact window P(win) − P(cover).
                cont_bands.append((ml_i, sp_i))
                notes.append(
                    f"containment window {legs[ml_i].market_ticker} yes "
                    f"without {legs[sp_i].market_ticker}: "
                    "joint = P(superset) - P(subset)"
                )

    # (1H-)SPREAD cover-by-N YES × TOTAL over-(M−0.5) NO, M <= N: LOGICALLY
    # IMPOSSIBLE cross-scope (S7/S8/S13/S34, 2026-07-11 WIRE-3) — a margin of N
    # needs the winner ALONE to score >= N >= M, and the total's scope contains
    # the spread's (``_SPREAD_TOTAL_SCOPES``; MLB extras only ADD runs).
    # Detection covers the impossible mix ONLY: the yy/nn/ny mixes keep their
    # existing structural/copula paths (spread×total windows are NOT wired).
    # farmable: soccer SAME-scope pairs (S7 1H×1H, S13 FT×FT) = airtight
    # ONE-scoreline tautologies ⇒ True; the soccer CROSS-scope pair (S8
    # 1H-spread × FT-total) = IMPOSSIBLE no-quote but farmable=False — V2
    # adversarial ruling 2026-07-11: the S8 implication spans TWO official
    # records (the half-time record and the full-time record), and Kalshi's
    # abandonment/award rules text for KXWC totals has not been captured as
    # evidence that both records stay consistent (e.g. abandonment after the
    # half) — an unverified lemma fails the airtight one-record farm bar.
    # MLB = False everywhere (48h rain-scalar policy, ml|spread precedent).
    for sp_i in range(len(legs)):
        total_types = _SPREAD_TOTAL_SCOPES.get(types[sp_i])
        if total_types is None or legs[sp_i].side != "yes":
            continue
        sport = classify_sport(legs[sp_i].market_ticker)
        if sport not in (Sport.SOCCER, Sport.MLB):
            continue  # only the two probed/settlement-verified sports
        parsed_spread = _corners_team_line(legs[sp_i].market_ticker)
        if parsed_spread is None or parsed_spread[1] < 1:
            continue  # need TEAM + a line N >= 1 (line-0 margin proves nothing)
        for tot_i in range(len(legs)):
            if types[tot_i] not in total_types or legs[tot_i].side != "no":
                continue
            if game_keys[sp_i] != game_keys[tot_i]:
                continue
            if classify_sport(legs[tot_i].market_ticker) is not sport:
                continue
            tot_line = _total_line(legs[tot_i].market_ticker)
            if tot_line is None or not 1 <= tot_line <= parsed_spread[1]:
                continue  # M > N (or unparseable): a cover does not force it
            notes.append(
                f"spread cover-by-{parsed_spread[1]} yes "
                f"({legs[sp_i].market_ticker}) forces >= {parsed_spread[1]} "
                f"goals/runs: total over-{tot_line - 1}.5 "
                f"{legs[tot_i].market_ticker} no is impossible"
            )
            return Relationship(
                RelationshipKind.IMPOSSIBLE,
                (),
                tuple(notes),
                farmable=(
                    sport is Sport.SOCCER
                    and not (
                        types[sp_i] is LegType.FIRST_HALF_SPREAD
                        and types[tot_i] is LegType.TOTAL
                    )
                ),
            )

    # Three period/line implications A⟹B are EXACT scoring facts (not
    # correlations), probe-verified constructible (2026-07-11 exchange matrix;
    # the original taker-API probe found these three, the containment-probe
    # taxonomy since mapped the full 50-shape universe — the additional
    # families live above). For each, ``_containment_sign`` gives:
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
                # {A no, B yes} = FT-BTTS without 1H-BTTS (S2-ny): an exact
                # window P(B) − P(A) — the universal-window rule (2026-07-11):
                # consumed by the collapse plan when a pinned pair coexists,
                # else priced as a NESTED_BAND super-leg (bare and embedded).
                cont_bands.append((b_i, a_i))
                notes.append(
                    f"containment window {legs[b_i].market_ticker} yes "
                    f"without {legs[a_i].market_ticker}: "
                    "joint = P(superset) - P(subset)"
                )

    # Family 2 — regulation moneyline team-WIN (A) ⟹ FT Over-0.5 (B): a win needs
    # a goal. The total must be the over-0.5 line (suffix -1); over-1.5+ is a
    # possible combo (a 1-0 win is under 1.5). TIE (0-0 draw) and ADVANCE (0-0 on
    # pens, a different LegType) do not imply a goal and are excluded. Kalshi
    # blocks moneyline-NO legs in combos, so only the win-YES half is REACHABLE;
    # the NO orientations ({A no, B no} containment, {A no, B yes} window
    # S1-ny) are wired DEFENSIVELY by the same sign matrix (2026-07-11
    # universal-window rule) — exact if the exchange ever unblocks them.
    for ml_i in range(len(legs)):
        if types[ml_i] is not LegType.MONEYLINE:
            continue
        if not _moneyline_is_team(legs[ml_i].market_ticker):
            continue
        for tot_i in range(len(legs)):
            if types[tot_i] is not LegType.TOTAL or game_keys[ml_i] != game_keys[tot_i]:
                continue
            if _total_line(legs[tot_i].market_ticker) != _OVER_HALF_LINE:
                continue
            verdict = _containment_sign(ml_i, tot_i, legs[ml_i].side, legs[tot_i].side)
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
            elif legs[ml_i].side == "no" and legs[tot_i].side == "yes":
                # {A no, B yes} = a goal but no win (S1-ny): exact window
                # P(over-0.5) − P(win) — defensive (win-NO exchange-blocked).
                cont_bands.append((tot_i, ml_i))
                notes.append(
                    f"containment window {legs[tot_i].market_ticker} yes "
                    f"without {legs[ml_i].market_ticker}: "
                    "joint = P(superset) - P(subset)"
                )

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
                # {A no, B yes} = FT-over-N without 1H-over-N (S3-ny, the
                # 379-combo tape cell): exact window P(B) − P(A) — the
                # universal-window rule (see Family 1).
                cont_bands.append((ft_i, fh_i))
                notes.append(
                    f"containment window {legs[ft_i].market_ticker} yes "
                    f"without {legs[fh_i].market_ticker}: "
                    "joint = P(superset) - P(subset)"
                )

    # TAXONOMY-IMPOSSIBLE TRIPWIRE (2026-07-11, V3 robustness §2.4-1 —
    # judge-mandated). The shipped impossibility families above have all had
    # their say (each returns immediately on its own impossible mixes, with
    # its own farmable verdict); any same-game pair that STILL matches a
    # fixture-pinned semantically-impossible shape × side-mix cell — the
    # 30-cell exchange-BLOCKED dangerous class the probe mapped — is
    # IMPOSSIBLE, farmable=False: fixture-driven certainty is not an airtight
    # in-code proof, so it declines and counts, never prices, never farms.
    # Such an RFQ is also proof Kalshi's validator loosened (every pinned
    # cell is unbuildable today) — the dedicated note makes it loud.
    # Fires at ANY combo size and beats any recorded containment/window/
    # conditional pair (an impossible pair zeroes the whole combo). Inert
    # (with a warning) when the fixture is missing/corrupt — fail-closed.
    tripped = taxonomy_impossible(legs, game_keys)
    if tripped is not None:
        shape, detail = tripped
        notes.append(f"taxonomy-impossible tripwire: {shape} — {detail}")
        return Relationship(RelationshipKind.IMPOSSIBLE, (), tuple(notes))

    if containments and len(legs) == 2:
        containment = containments[-1]  # shipped last-write-wins 2-leg pair
        notes.append(
            f"logical containment: joint = P(leg {containment[0]} "
            f"{legs[containment[0]].market_ticker})"
        )
        return Relationship(RelationshipKind.CONTAINMENT, (), tuple(notes), containment)
    if containments or conditionals:
        # CONTAINMENT/CONDITIONAL-IN-LARGER-COMBO collapse (2026-07-11):
        # exactly where the "not modeled" UNKNOWN declines used to fire
        # (``conditionals`` is only ever recorded on >2-leg combos). The
        # engine drops each implied superset leg, collapses each window pair
        # into a band super-leg and each conditional pair into a
        # conditional super-leg, then prices the reduced set. The bare 2-leg
        # containment above keeps price_containment.
        return _collapse_containments(
            legs, containments, bands, cont_bands, conditionals, game_keys, notes
        )

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

    # Ladder bands and containment windows are ONE species here (both are the
    # exact arithmetic P(low) − P(high) on a band super-leg): merged, so a
    # bare 2-leg window (S2-ny/S3-ny/S12-ny/S33-ny…) and an embedded window
    # without a pinned containment price EXACTLY instead of the old copula
    # fallback (2026-07-11 universal-window rule).
    all_bands = list(dict.fromkeys([*bands, *cont_bands]))
    if all_bands:
        # A band is modeled ONLY as an isolated pair: each band's game must
        # contain exactly its two legs. A third same-game leg (incl. a second
        # band or a 3-rung shape) is declined UNKNOWN: a band is a WINDOW event,
        # non-monotone in the latent count, so its correlation to a same-game
        # neighbour is the rung's ρ ATTENUATED by an unmeasured factor (bites
        # hardest on corners|corners_team 0.62) — widen-or-no-quote, never a
        # copula guess. Cross-game companions are fine (cross_event_rho
        # machinery, where representing the band by its low rung is exact).
        # Disjointness follows: a shared leg would put ≥3 legs in one game.
        for low_i, _high_i in all_bands:
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
            bands=tuple(all_bands),
        )

    return Relationship(RelationshipKind.OK, tuple(groups), tuple(notes))
