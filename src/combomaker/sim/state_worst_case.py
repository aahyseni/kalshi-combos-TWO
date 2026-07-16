"""STATE-CONSISTENT per-game worst-case loss by EXACT ENUMERATION over the
Dixon-Coles scoreline grid (finite support — NO Monte-Carlo sampling, so no
sampling-miss risk).

This is the CONFIRM-PATH waiver machinery for handoff Problem A (docs/reports/
2026-07-15-HANDOFF-for-llm-review.md §4A): the quote-time analytic per-game
loss / directional caps are deliberately COMONOTONE-overstated beyond the single
netted advance event (the E2 mass-acceptance dominance invariant requires those
bounds to stay monotone, so they may never learn richer hedge structure). At
LAST-LOOK/confirm the book is a concrete merged set of committed positions +
outstanding reservations + THE CANDIDATE, and this module evaluates the true
state-consistent worst case: every entity of a game settles against the SAME
enumerated scoreline state, so ALL structural exclusions (opposing advances,
over/under on one total market, BTTS yes/no, 1H x FT, scorer x total) net
exactly, with zero correlation table.

⚠ CONFIRM-PATH ONLY. This bound is NOT monotone in the committed-position set
(a real holding earns hedge credit), so it must NEVER feed the QUOTE-TIME
analytic caps (risk/exposure.py mass-acceptance/mutex bounds, risk/limits.py
quote-time behaviour) — those stay monotone and untouched (E2). Open quotes ARE
clamped at max(0, loss) per state here, which preserves the E2 rationale at
confirm (a taker can accept only the concentrated side and decline the hedge, so
a resting unfilled hedge never earns credit).

SEMANTICS (design fixed 2026-07-16; prototype tools/proto_state_worst_case.py):

  * ENTITIES (committed + reservations + candidate — the caller merges them into
    one sequence) net per state: signed P&L, real holdings hedge each other.
    An entity with ``earns_credit=False`` (an outstanding NON-CANDIDATE
    reservation) has its per-state contribution CLAMPED at ``max(0, loss)``:
    its hit-side loss still sums fully (assume-committed conservatism) but its
    miss-side credit can never certify the book — a reservation is NOT a real
    holding; an explicit decline/lapse ``release`` vanishes it exactly like an
    unfilled resting quote, and a certificate that leaned on its credit would
    outlive it (adversarial-review finding 2, 2026-07-16). COMMITTED positions
    and THE CANDIDATE keep full signed netting: a committed hedge cannot vanish
    without settling, and the candidate vanishing means no fill — the book it
    hedged reverts unchanged, so its credit never outlives it.
  * OPEN QUOTES contribute ``max(0, loss_in_state)`` per state — adversarial
    fill, never a credit.
  * Per game G, a combo leg in ANOTHER game or a NON-STRUCTURAL leg (corners /
    cards / props / anything not settleable from the scoreline) resolves
    ADVERSARIALLY: assume it HITS (loss-maximising for a long-NO seller). It can
    never block the parlay from hitting — but a structural leg of G that
    provably MISSES in a state definitively misses the parlay (an adversarial
    leg cannot resurrect a dead parlay).
  * FAIL-CLOSED (full comonotone premium + fee in EVERY state of G) per entity
    when: it has NO structural leg in G; its held side is not NO; it is a
    conservatively-reserved holding (``risk_modeled=False``); or ANY of its legs
    carries an unknown selection side. Never a convenient default (hard rule 6).
  * Sell-only long-NO per-state P&L: parlay HITS (all structural legs of G
    possibly-hit, adversarial legs assumed hit) ⇒ lose ``premium + fee``;
    a structural leg of G provably MISSES ⇒ NO pays $1/contract ⇒ signed P&L
    ``notional − premium − fee`` (fee = 0 reproduces the brief's "contracts −
    premium" exactly; a nonzero fee only SHRINKS the hedge credit —
    conservative).
  * ADVANCE legs on a level-after-ET state are decided by the shootout, which is
    not part of the scoreline: each level state splits into TWO deterministic
    shootout branches (team A / team B advances — the exact shared-coin
    semantics of ``structural_book._advance_settle``), so advance(A) and
    advance(B) are exact opposites in every enumerated state and the mutex
    property holds by construction. ``n_states`` counts the branch-expanded
    enumeration.
  * PLAYER-SCORES legs are stochastic given the scoreline (the scorer coin):
    YES possibly-hits iff the team scored >= min_goals in the state (provably
    misses otherwise); NO possibly-hits in EVERY state (the player may blank
    even when the team scores) — adversarial-within-structure.
  * CERTIFICATION: a game with no buildable structural plan
    (``structural_book.build_game_plans`` — needs >= 2 identifying team-level
    legs with usable marginals) is NOT certifiable: ``certified=False`` and the
    bound fails closed to the analytic comonotone sum. Any enumeration error
    does the same (never a guess).
  * INVERSION UNIVERSE = all legs of entities + open-quote hypotheticals (a
    quote's legs may identify the model). Per-state SETTLEMENT is
    parameter-free (it reads only the enumerated state support), so a quote can
    never change how an entity settles, and per state a quote contributes >= 0.
    CERTIFICATION-FLIP CAVEAT: a quote whose leg first identifies a game can
    flip it uncertified → certified (comonotone → netted bound), so the bound as
    a FUNCTION is monotone in open quotes only at fixed certification. Both
    values are valid state-dominant upper bounds on every realizable fill
    subset — a fill converts a hypothetical into a position with the SAME legs,
    leaving the universe (and therefore certification and settlement)
    unchanged, and the filled position's per-state loss is exactly the
    hypothetical loss the clamp already counted — so the E2-at-confirm
    dominance rationale holds across the flip.

INPUT MAPPING (documented per the brief; adapters below):
  ``WorstCaseEntity``  <-  risk/exposure.OpenPosition:
      our_side <- our_side, contracts_centi <- int(contracts),
      entry_price_cc <- int(entry_price_cc), legs <- legs (LegRef reused),
      risk_modeled <- risk_modeled; fee_cc is NEW (OpenPosition.max_loss_cc is
      fee-free; pass a per-fill settlement fee if tracked, else 0).
      premium_cc mirrors OpenPosition.max_loss_cc; gross_notional_cc mirrors
      OpenPosition.gross_settlement_notional_cc — to the cent.
  ``WorstCaseQuote``   <-  risk/exposure.OpenQuoteRisk via
      ``hypothetical_positions(conventions)``: one WorstCaseEntity per quotable
      side (bid > 0), exactly the mass-acceptance hypotheticals the exposure
      book uses (same ids, sides, prices).

Everything here is PURE and PICKLABLE (frozen dataclasses of ints/strs/enums +
plain dict inputs) so the evaluation can run off-loop in a worker process.
Money is int centi-cents throughout (hard rule 5); probability floats appear
only inside the reused pricing machinery. Settlement/parse/enumeration reuse
the LIVE seams verbatim (hard rule 8c): ``pricing.structural_api`` parse/states/
indicators, ``pricing.grouping.game_key``, ``sim.structural_book``
build_game_plans/_match_format. Parity: tests/test_state_worst_case.py asserts
module == prototype to the cent on the same inputs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray

from combomaker.core.conventions import Conventions, Side
from combomaker.core.money import CC_PER_DOLLAR
from combomaker.pricing.grouping import game_key
from combomaker.pricing.structural_api import (
    Advance,
    HalfBtts,
    HalfDraw,
    HalfGoalSpread,
    HalfResult,
    HalfTotalOver,
    LegSpec,
    ModelParams,
    PlayerScores,
    States,
    Team,
    parse_leg,
    parse_match,
)
from combomaker.pricing.structural_api import (
    half_indicator as _half_indicator,
)
from combomaker.pricing.structural_api import (
    states as _enum_states,
)
from combomaker.pricing.structural_api import (
    team_goals as _team_goals,
)
from combomaker.pricing.structural_api import (
    team_indicator as _team_indicator,
)
from combomaker.risk.exposure import LegRef, OpenPosition, OpenQuoteRisk

# _match_format is the one shared series->format rule (KEEP the single
# definition; sim-package-internal import, same seam build_game_plans uses).
from combomaker.sim.structural_book import (
    StructuralConfigView,
    _match_format,
    build_game_plans,
)

_HALF_SPECS = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)

_BoolArray = NDArray[np.bool_]
_IntArray = NDArray[np.int64]

UNCERTIFIED_NO_PLAN = "no_structural_plan"


@dataclass(frozen=True, slots=True)
class WorstCaseEntity:
    """One fully-netting book entity (committed position / outstanding
    reservation / the candidate). Mapping from OpenPosition: module docstring."""

    entity_id: str
    our_side: Side
    contracts_centi: int
    entry_price_cc: int
    legs: tuple[LegRef, ...]
    fee_cc: int = 0
    risk_modeled: bool = True
    # False = the outstanding-reservation treatment: per-state contribution
    # clamped at max(0, loss) — the hit side sums, the miss side never credits
    # (a released reservation vanishes like an unfilled quote; module docstring).
    earns_credit: bool = True

    @property
    def premium_cc(self) -> int:
        """Premium paid — mirrors ``OpenPosition.max_loss_cc`` to the cent."""
        return self.contracts_centi * self.entry_price_cc // 100

    @property
    def gross_notional_cc(self) -> int:
        """Contracts × $1 — mirrors ``OpenPosition.gross_settlement_notional_cc``."""
        return self.contracts_centi * CC_PER_DOLLAR // 100

    @property
    def hit_loss_cc(self) -> int:
        """Loss when the parlay HITS: premium + fee."""
        return self.premium_cc + self.fee_cc


@dataclass(frozen=True, slots=True)
class WorstCaseQuote:
    """One resting open quote: its per-side hypothetical fills (bid > 0 sides
    only), exactly ``OpenQuoteRisk.hypothetical_positions``. Contributes
    ``max(0, worst-side loss)`` per state — never a credit."""

    quote_id: str
    hypotheticals: tuple[WorstCaseEntity, ...]


@dataclass(frozen=True, slots=True)
class GameWorstCase:
    """The per-game verdict. ``certified=False`` ⇒ ``worst_case_cc`` is the
    fail-closed analytic comonotone sum (waiver impossible for the game)."""

    worst_case_cc: int
    certified: bool
    n_states: int
    uncertified_reason: str | None = None


# ----------------------------- adapters -------------------------------------


def entity_from_position(
    position: OpenPosition, *, fee_cc: int = 0, earns_credit: bool = True
) -> WorstCaseEntity:
    """Map a live ``OpenPosition`` (committed, reserved, or the candidate) to a
    ``WorstCaseEntity``. Field mapping in the module docstring. Callers mapping
    an OUTSTANDING RESERVATION pass ``earns_credit=False`` (the clamped,
    no-hedge-credit treatment); committed positions and the candidate keep the
    default full signed netting."""
    return WorstCaseEntity(
        entity_id=position.position_id,
        our_side=position.our_side,
        contracts_centi=int(position.contracts),
        entry_price_cc=int(position.entry_price_cc),
        legs=position.legs,
        fee_cc=fee_cc,
        risk_modeled=position.risk_modeled,
        earns_credit=earns_credit,
    )


def quote_from_open_quote(
    quote: OpenQuoteRisk, conventions: Conventions
) -> WorstCaseQuote:
    """Map a live ``OpenQuoteRisk`` to a ``WorstCaseQuote`` via the SAME
    ``hypothetical_positions`` the exposure book's mass-acceptance snapshot
    uses (one hypothetical per quotable side, our side from Conventions)."""
    return WorstCaseQuote(
        quote_id=quote.quote_id,
        hypotheticals=tuple(
            entity_from_position(h) for h in quote.hypothetical_positions(conventions)
        ),
    )


# ----------------------------- internals ------------------------------------


def _leg_event(leg: LegRef, events: Mapping[str, str | None] | None) -> str | None:
    if leg.event_ticker:
        return leg.event_ticker
    if events is None:
        return None
    return events.get(leg.market_ticker)


def _leg_game(leg: LegRef, events: Mapping[str, str | None] | None) -> str | None:
    ev = _leg_event(leg, events)
    return game_key(ev) if ev else None


def _comonotone_cc(
    entities: Sequence[WorstCaseEntity], quotes: Sequence[WorstCaseQuote]
) -> int:
    """The analytic comonotone fail-closed sum: every entity loses its full
    premium (+fee); every quote its worst quotable side. This is the value the
    certified bound is provably <= (property-tested)."""
    total = sum(e.hit_loss_cc for e in entities)
    for q in quotes:
        total += max((h.hit_loss_cc for h in q.hypotheticals), default=0)
    return total


def _selected_possible(
    spec: LegSpec,
    side: str,
    st: States,
    params: ModelParams,
    branch: Team | None,
) -> _BoolArray:
    """Per-state bool: can this SELECTED leg side still hit in the state?

    Deterministic legs read the exact live indicator (side-flipped); Advance
    resolves on the shootout branch; PlayerScores uses the possibility
    indicator (module docstring). Settlement math is the live machinery —
    never re-derived (hard rule 8c)."""
    if isinstance(spec, PlayerScores):
        if side == "yes":
            return np.asarray(
                _team_goals(st, spec.team, spec.include_et) >= spec.min_goals,
                dtype=np.bool_,
            )
        return np.ones(int(st.w.size), dtype=np.bool_)
    if isinstance(spec, Advance):
        if branch is None:  # pragma: no cover - branches always split on Advance
            raise ValueError("advance leg without a shootout branch")
        if spec.team is Team.A:
            us90, them90, us_et, them_et = st.a90, st.b90, st.a_et, st.b_et
        else:
            us90, them90, us_et, them_et = st.b90, st.a90, st.b_et, st.a_et
        win = (us90 > them90) | ((us90 == them90) & (us_et > them_et))
        level = (us90 == them90) & (us_et == them_et)
        yes = win | (level & (branch is spec.team))
        return np.asarray(yes if side == "yes" else ~yes, dtype=np.bool_)
    if isinstance(spec, _HALF_SPECS):
        yes_arr = np.asarray(_half_indicator(st, spec), dtype=np.float64) >= 0.5
    else:
        yes_arr = np.asarray(_team_indicator(st, spec, params), dtype=np.float64) >= 0.5
    return np.asarray(yes_arr if side == "yes" else ~yes_arr, dtype=np.bool_)


def _settle_specs(
    game: str,
    plan_specs: dict[str, LegSpec],
    game_legs: Sequence[LegRef],
    cfg: StructuralConfigView,
) -> dict[str, LegSpec]:
    """Every market of this game settleable from the scoreline: the plan's
    inverted legs + any other parseable leg (a missing marginal drops a leg
    from the INVERSION, but per-state settlement needs no marginal). A market
    whose ticker blob does not match the game key never settles here
    (fail-closed: it stays adversarial)."""
    settle = dict(plan_specs)
    for leg in game_legs:
        market = leg.market_ticker
        if market in settle:
            continue
        parts = market.split("-")
        if len(parts) < 2 or parts[1] != game:
            continue
        match = parse_match(parts[1])
        if match is None:
            continue
        spec = parse_leg(market, match, fmt=_match_format(market, cfg.knockout_series))
        if not isinstance(spec, str):
            settle[market] = spec
    return settle


def _entity_loss_matrix(
    entity: WorstCaseEntity,
    game: str,
    settle: dict[str, LegSpec],
    st: States,
    params: ModelParams,
    branches: tuple[Team | None, ...],
    events: Mapping[str, str | None] | None,
) -> _IntArray:
    """``(len(branches), n_states)`` SIGNED per-state loss in int centi-cents.

    Fail-closed to the constant ``hit_loss_cc`` (full premium + fee in every
    state) when the entity cannot be netted — module docstring lists the exact
    conditions."""
    n = int(st.w.size)
    hit_loss = entity.hit_loss_cc
    fail_closed = (
        entity.our_side is not Side.NO
        or not entity.risk_modeled
        or any(leg.side not in ("yes", "no") for leg in entity.legs)
    )
    struct: list[tuple[LegRef, LegSpec]] = []
    if not fail_closed:
        struct = [
            (leg, settle[leg.market_ticker])
            for leg in entity.legs
            if _leg_game(leg, events) == game and leg.market_ticker in settle
        ]
        fail_closed = not struct
    if fail_closed:
        return np.full((len(branches), n), hit_loss, dtype=np.int64)
    miss_loss = hit_loss - entity.gross_notional_cc
    out = np.empty((len(branches), n), dtype=np.int64)
    for bi, branch in enumerate(branches):
        hit = np.ones(n, dtype=np.bool_)
        for leg, spec in struct:
            hit &= _selected_possible(spec, leg.side, st, params, branch)
        out[bi] = np.where(hit, np.int64(hit_loss), np.int64(miss_loss))
    return out


# ----------------------------- public API -----------------------------------


def state_worst_case_by_game(
    entities: Sequence[WorstCaseEntity],
    open_quotes: Sequence[WorstCaseQuote],
    marginals: Mapping[str, float],
    events: Mapping[str, str | None] | None,
    structural_cfg: StructuralConfigView,
) -> dict[str, GameWorstCase]:
    """The state-consistent per-game worst case for the merged confirm-time book.

    ``entities`` = committed positions + outstanding reservations + THE
    CANDIDATE (the caller merges; all net fully). ``open_quotes`` = resting
    quotes (clamped >= 0 per state). ``marginals`` maps market_ticker -> current
    P(YES) — a plain mapping (picklable for off-loop workers); a missing entry
    drops the leg from the inversion only (settlement is marginal-free).
    ``events`` optionally supplements ``LegRef.event_ticker`` when it is None
    (market_ticker -> event_ticker); a leg with no resolvable event is ungamed
    and resolves adversarially everywhere. ``structural_cfg`` is the live
    ``StructuralConfigView``.

    Returns one ``GameWorstCase`` per game touched by any entity/quote leg.
    Empty inputs return {}.
    """
    # Inversion universe: all legs, unique market tickers, first-seen event.
    uni_tickers: list[str] = []
    uni_events: list[str | None] = []
    uni_marginals: list[float | None] = []
    seen: set[str] = set()
    holders: list[WorstCaseEntity] = list(entities)
    for quote in open_quotes:
        holders.extend(quote.hypotheticals)
    for holder in holders:
        for leg in holder.legs:
            market = leg.market_ticker
            if market in seen:
                continue
            seen.add(market)
            uni_tickers.append(market)
            uni_events.append(_leg_event(leg, events))
            uni_marginals.append(marginals.get(market))
    plans, _copula = build_game_plans(
        uni_tickers, uni_events, uni_marginals, structural_cfg
    )
    plan_specs_by_game: dict[str, dict[str, LegSpec]] = {}
    params_by_game: dict[str, ModelParams] = {}
    for plan in plans:
        ev = uni_events[plan.global_indices[0]]
        if ev is None:  # pragma: no cover - build_game_plans only groups gamed legs
            continue
        g = game_key(ev)
        plan_specs_by_game[g] = {
            uni_tickers[j]: spec
            for j, spec in zip(plan.global_indices, plan.specs, strict=True)
        }
        params_by_game[g] = plan.params

    # Games touched per entity / quote.
    entity_games: list[tuple[WorstCaseEntity, frozenset[str]]] = [
        (
            e,
            frozenset(
                gk for leg in e.legs if (gk := _leg_game(leg, events)) is not None
            ),
        )
        for e in entities
    ]
    quote_games: list[tuple[WorstCaseQuote, frozenset[str]]] = []
    for quote in open_quotes:
        gs: set[str] = set()
        for h in quote.hypotheticals:
            gs |= {gk for leg in h.legs if (gk := _leg_game(leg, events)) is not None}
        quote_games.append((quote, frozenset(gs)))
    touched: set[str] = set()
    for _e, egs in entity_games:
        touched |= egs
    for _q, qgs in quote_games:
        touched |= qgs

    out: dict[str, GameWorstCase] = {}
    for game in sorted(touched):
        game_entities = [e for e, egs in entity_games if game in egs]
        game_quotes = [q for q, qgs in quote_games if game in qgs]
        plan_specs = plan_specs_by_game.get(game)
        if plan_specs is None:
            out[game] = GameWorstCase(
                worst_case_cc=_comonotone_cc(game_entities, game_quotes),
                certified=False,
                n_states=0,
                uncertified_reason=UNCERTIFIED_NO_PLAN,
            )
            continue
        try:
            out[game] = _certified_worst_case(
                game,
                plan_specs,
                params_by_game[game],
                game_entities,
                game_quotes,
                events,
                structural_cfg,
            )
        except Exception as exc:  # fail-closed: never guess on an enumeration error
            out[game] = GameWorstCase(
                worst_case_cc=_comonotone_cc(game_entities, game_quotes),
                certified=False,
                n_states=0,
                uncertified_reason=f"enumeration_failed: {exc!r}",
            )
    return out


def _certified_worst_case(
    game: str,
    plan_specs: dict[str, LegSpec],
    params: ModelParams,
    entities: Sequence[WorstCaseEntity],
    quotes: Sequence[WorstCaseQuote],
    events: Mapping[str, str | None] | None,
    cfg: StructuralConfigView,
) -> GameWorstCase:
    game_legs: list[LegRef] = []
    for e in entities:
        game_legs.extend(leg for leg in e.legs if _leg_game(leg, events) == game)
    for q in quotes:
        for h in q.hypotheticals:
            game_legs.extend(leg for leg in h.legs if _leg_game(leg, events) == game)
    settle = _settle_specs(game, plan_specs, game_legs, cfg)
    if any(isinstance(s, _HALF_SPECS) for s in settle.values()) and not params.with_halves:
        params = replace(params, with_halves=True)
    st = _enum_states(params)
    n = int(st.w.size)
    branches: tuple[Team | None, ...] = (
        (Team.A, Team.B)
        if any(isinstance(s, Advance) for s in settle.values())
        else (None,)
    )
    total = np.zeros((len(branches), n), dtype=np.int64)
    for entity in entities:
        matrix = _entity_loss_matrix(
            entity, game, settle, st, params, branches, events
        )
        if not entity.earns_credit:
            # Outstanding-reservation treatment: the hit-side loss sums fully,
            # the miss-side credit is clamped away (a released reservation
            # vanishes like an unfilled quote — module docstring, finding 2).
            matrix = np.maximum(matrix, np.int64(0))
        total += matrix
    for quote in quotes:
        if not quote.hypotheticals:
            continue
        hypo_losses = np.stack(
            [
                _entity_loss_matrix(h, game, settle, st, params, branches, events)
                for h in quote.hypotheticals
            ]
        )
        # Adversarial fill: the worse quotable side per STATE, never below 0
        # (a resting unfilled hedge earns no credit — E2 rationale at confirm).
        total += np.maximum(hypo_losses.max(axis=0), np.int64(0))
    worst = int(total.max()) if (entities or quotes) else 0
    return GameWorstCase(
        worst_case_cc=worst,
        certified=True,
        n_states=n * len(branches),
        uncertified_reason=None,
    )
