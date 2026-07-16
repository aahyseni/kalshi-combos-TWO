"""PROTOTYPE (hard rule 8 — validate here, THEN port to src/combomaker/sim/state_worst_case.py).

STATE-CONSISTENT per-game worst-case loss by EXACT ENUMERATION over the
Dixon-Coles scoreline grid (finite support — NO Monte-Carlo sampling, so no
sampling-miss risk). This is the CONFIRM-PATH waiver machinery for handoff
Problem A (the ME-overstatement of the per-game / directional analytic caps):
the analytic Stage-B bound sums mutually-exclusive losses on every dimension
except the single advance event; here every entity of a game settles against
the SAME enumerated scoreline state, so ALL structural exclusions (opposing
advances, over/under on one total market, BTTS yes/no, 1H x FT, scorer x total)
net exactly, with zero correlation table.

SEMANTICS (fixed by the design brief; the module port must match to the cent):
  * ENTITIES (committed positions + outstanding reservations + THE CANDIDATE)
    net per state — signed P&L, real holdings hedge each other. An entity with
    ``earns_credit=False`` (an outstanding NON-CANDIDATE reservation) has its
    per-state contribution CLAMPED at max(0, loss): its hit-side loss still
    sums fully (assume-committed conservatism) but its miss-side credit never
    certifies the book — a reservation is NOT a real holding; an explicit
    decline/lapse ``release`` vanishes it exactly like an unfilled resting
    quote, and a certificate that leaned on its credit would outlive it
    (adversarial-review finding 2, 2026-07-16). Committed positions and THE
    CANDIDATE keep full signed netting (a committed hedge cannot vanish
    without settling; the candidate vanishing means no fill, so the book it
    hedged reverts unchanged).
  * OPEN QUOTES contribute max(0, loss_in_state) per state — adversarial fill;
    a resting unfilled hedge NEVER earns credit (preserves the E2 rationale at
    confirm: a taker can accept only the concentrated side).
  * Per game G, a combo leg in ANOTHER game or a NON-STRUCTURAL leg (corners /
    cards / props / anything not settleable from the scoreline) resolves
    ADVERSARIALLY: assume it HITS (the loss-maximising outcome for a long-NO
    seller). It can therefore never block the parlay from hitting — but a
    structural leg of G that provably MISSES in a state still definitively
    misses the parlay (an adversarial leg cannot resurrect a dead parlay).
  * An entity with NO structural leg in G (e.g. a pure corners parlay), a
    non-NO / unknown-side entity, a conservatively-reserved holding
    (risk_modeled=False), or an entity carrying any leg with an unknown
    selection side, contributes its FULL comonotone premium (+fee) in EVERY
    state of G — fail-closed, never a convenient default.
  * Sell-only long-NO per-state P&L: the parlay HITS in a state (all structural
    legs of G possibly-hit AND adversarial legs assumed hit)  =>  lose
    premium + fee.  A structural leg of G provably MISSES  =>  the parlay
    misses, NO pays $1/contract  =>  signed P&L = notional - premium - fee
    (with fee = 0 this is exactly the brief's "contracts - premium"; a nonzero
    fee only SHRINKS the hedge credit — conservative).
  * Advance legs on a level-after-ET state are decided by the shootout, which
    is NOT part of the scoreline. Each level state is split into TWO
    deterministic shootout branches (team A advances / team B advances — the
    exact shared-coin semantics of sim/structural_book._advance_settle), so
    advance(A) and advance(B) are exact opposites in every enumerated state and
    the mutex property is preserved by construction. n_states counts the
    branch-expanded enumeration.
  * PlayerScores legs are stochastic given the scoreline (the scorer coin):
    the YES side possibly-hits iff the team scored >= min_goals in the state
    (provably misses otherwise); the NO side possibly-hits in EVERY state (the
    player may blank even when the team scores) — adversarial-within-structure.
  * Certification: a game with NO buildable structural plan
    (sim/structural_book.build_game_plans — needs >= 2 identifying team-level
    legs with usable marginals) is NOT certifiable: fail-closed to the analytic
    comonotone sum, certified=False.
  * The inversion universe is ALL legs (entities + open-quote hypotheticals):
    a quote's legs may identify the model. Settlement is parameter-free (it
    reads only the enumerated state support), so a quote can never change how
    an entity settles; per state a quote contributes >= 0 (the clamp). The one
    second-order effect: a quote whose leg first IDENTIFIES the game can flip
    it uncertified -> certified (comonotone -> netted bound). Both values are
    valid state-dominant upper bounds on every realizable fill subset (a fill
    never changes the leg universe), so the E2-at-confirm rationale holds; the
    module documents this as the certification-flip caveat.

Prototype style per hard rule 8c: settlement math is 100% the LIVE machinery
(structural_api parse/states/indicators; build_game_plans for the plan) — never
reimplemented. Only the AGGREGATION here is deliberately simple per-state
Python loops; the module port vectorises it, and tests/test_state_worst_case.py
asserts module == prototype to the cent on the same inputs.

Run:  uv run python tools/proto_state_worst_case.py
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

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
    PlayerScores,
    Team,
    parse_leg,
    parse_match,
)
from combomaker.pricing.structural_api import (
    half_indicator as _half_indicator,
)
from combomaker.pricing.structural_api import (
    states as enum_states,
)
from combomaker.pricing.structural_api import (
    team_goals as _team_goals,
)
from combomaker.pricing.structural_api import (
    team_indicator as _team_indicator,
)
from combomaker.sim.structural_book import (
    StructuralConfigView,
    _match_format,
    build_game_plans,
)

_HALF = (HalfResult, HalfDraw, HalfTotalOver, HalfBtts, HalfGoalSpread)

# (market_ticker, event_ticker | None, selected side "yes"|"no")
Leg = tuple[str, str | None, str]


@dataclass(frozen=True)
class ProtoEntity:
    """Mirrors risk/exposure.OpenPosition: side_held <- our_side (str),
    contracts_centi <- int(contracts), entry_price_cc <- int(entry_price_cc),
    legs <- [(market_ticker, event_ticker, side)], risk_modeled unchanged.
    fee_cc is additive to the hit-side loss (OpenPosition.max_loss_cc has no fee).
    ``earns_credit=False`` clamps the per-state contribution at max(0, loss) —
    the outstanding-reservation treatment (module docstring)."""

    entity_id: str
    side_held: str            # "no" = a sell-only long-NO fill; anything else fails closed
    contracts_centi: int
    entry_price_cc: int
    legs: tuple[Leg, ...]
    fee_cc: int = 0
    risk_modeled: bool = True
    earns_credit: bool = True

    @property
    def premium_cc(self) -> int:
        return self.contracts_centi * self.entry_price_cc // 100

    @property
    def notional_cc(self) -> int:
        return self.contracts_centi * CC_PER_DOLLAR // 100

    @property
    def hit_loss_cc(self) -> int:
        return self.premium_cc + self.fee_cc


@dataclass(frozen=True)
class ProtoQuote:
    """Mirrors risk/exposure.OpenQuoteRisk via its hypothetical_positions():
    one ProtoEntity per quotable side (bid > 0)."""

    quote_id: str
    hypotheticals: tuple[ProtoEntity, ...]


# result per game: (worst_case_cc, certified, n_states, uncertified_reason)
ProtoResult = tuple[int, bool, int, str | None]


def _leg_event(leg: Leg, events: dict[str, str | None] | None) -> str | None:
    ev = leg[1]
    if ev:
        return ev
    return (events or {}).get(leg[0])


def _leg_game(leg: Leg, events: dict[str, str | None] | None) -> str | None:
    ev = _leg_event(leg, events)
    return game_key(ev) if ev else None


def _comonotone_cc(ents: list[ProtoEntity], quotes: list[ProtoQuote]) -> int:
    total = sum(e.hit_loss_cc for e in ents)
    for q in quotes:
        total += max((h.hit_loss_cc for h in q.hypotheticals), default=0)
    return total


def _selected_possible(
    spec: LegSpec, side: str, st: object, params: object, branch: Team | None
) -> np.ndarray:
    """Per-state bool: can this SELECTED leg side still hit in the state?
    Deterministic legs: the exact live indicator (side-flipped). Advance: the
    shootout-branch-resolved indicator. PlayerScores: possibility (see module
    docstring). Settlement math is the LIVE machinery — nothing re-derived."""
    n = int(st.w.size)  # type: ignore[attr-defined]
    if isinstance(spec, PlayerScores):
        if side == "yes":
            return np.asarray(
                _team_goals(st, spec.team, spec.include_et) >= spec.min_goals
            )
        return np.ones(n, dtype=bool)
    if isinstance(spec, Advance):
        if branch is None:
            raise ValueError("advance leg without a shootout branch")
        if spec.team is Team.A:
            us90, them90 = st.a90, st.b90            # type: ignore[attr-defined]
            us_et, them_et = st.a_et, st.b_et        # type: ignore[attr-defined]
        else:
            us90, them90 = st.b90, st.a90            # type: ignore[attr-defined]
            us_et, them_et = st.b_et, st.a_et        # type: ignore[attr-defined]
        win = (us90 > them90) | ((us90 == them90) & (us_et > them_et))
        level = (us90 == them90) & (us_et == them_et)
        yes = win | (level & (branch is spec.team))
        return np.asarray(yes if side == "yes" else ~yes)
    if isinstance(spec, _HALF):
        yes_arr = np.asarray(_half_indicator(st, spec)) >= 0.5
    else:
        yes_arr = np.asarray(_team_indicator(st, spec, params)) >= 0.5
    return np.asarray(yes_arr if side == "yes" else ~yes_arr)


def _settle_map(
    game: str,
    plan_tickers: dict[str, LegSpec],
    ents: list[ProtoEntity],
    quotes: list[ProtoQuote],
    events: dict[str, str | None] | None,
    cfg: StructuralConfigView,
) -> dict[str, LegSpec]:
    """Every ticker of this game we can settle from the scoreline: the plan's
    inverted legs + any other parseable leg (a leg with a missing marginal is
    dropped from the INVERSION but its per-state settlement needs no marginal).
    A leg whose market blob does not match the game key never settles here."""
    settle = dict(plan_tickers)
    all_legs: list[Leg] = []
    for e in ents:
        all_legs.extend(leg for leg in e.legs if _leg_game(leg, events) == game)
    for q in quotes:
        for h in q.hypotheticals:
            all_legs.extend(leg for leg in h.legs if _leg_game(leg, events) == game)
    for market, _ev, _side in all_legs:
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


def _entity_losses(
    entity: ProtoEntity,
    game: str,
    settle: dict[str, LegSpec],
    st: object,
    params: object,
    branches: tuple[Team | None, ...],
    events: dict[str, str | None] | None,
) -> list[list[int]]:
    """Per-branch per-state SIGNED loss (cc). Fail-closed constants documented
    in the module docstring."""
    n = int(st.w.size)  # type: ignore[attr-defined]
    hit_loss = entity.hit_loss_cc
    fail_closed = (
        entity.side_held != "no"
        or not entity.risk_modeled
        or any(leg[2] not in ("yes", "no") for leg in entity.legs)
    )
    if not fail_closed:
        struct = [
            (leg, settle[leg[0]])
            for leg in entity.legs
            if _leg_game(leg, events) == game and leg[0] in settle
        ]
        fail_closed = not struct
    if fail_closed:
        return [[hit_loss] * n for _ in branches]
    miss_loss = hit_loss - entity.notional_cc
    out: list[list[int]] = []
    for branch in branches:
        sel = [_selected_possible(spec, leg[2], st, params, branch) for leg, spec in struct]
        row = []
        for s in range(n):
            hit = all(bool(a[s]) for a in sel)
            row.append(hit_loss if hit else miss_loss)
        out.append(row)
    return out


def proto_state_worst_case_by_game(
    entities: list[ProtoEntity],
    open_quotes: list[ProtoQuote],
    marginals: dict[str, float],
    events: dict[str, str | None] | None,
    structural_cfg: StructuralConfigView,
) -> dict[str, ProtoResult]:
    """The prototype of sim/state_worst_case.state_worst_case_by_game (loop
    aggregation; live settlement). Same semantics, cent-identical output."""
    # Inversion universe: ALL legs (entities + quote hypotheticals), unique
    # tickers, first-seen event, marginal (or None) from the mapping.
    uni_t: list[str] = []
    uni_e: list[str | None] = []
    uni_m: list[float | None] = []
    seen: set[str] = set()
    all_holders: list[ProtoEntity] = list(entities)
    for q in open_quotes:
        all_holders.extend(q.hypotheticals)
    for holder in all_holders:
        for leg in holder.legs:
            market = leg[0]
            if market in seen:
                continue
            seen.add(market)
            uni_t.append(market)
            uni_e.append(_leg_event(leg, events))
            uni_m.append(marginals.get(market))
    plans, _copula = build_game_plans(uni_t, uni_e, uni_m, structural_cfg)
    plan_by_game: dict[str, dict[str, LegSpec]] = {}
    params_by_game: dict[str, object] = {}
    for plan in plans:
        ev = uni_e[plan.global_indices[0]]
        assert ev is not None  # build_game_plans only groups gamed legs
        g = game_key(ev)
        plan_by_game[g] = {
            uni_t[j]: spec for j, spec in zip(plan.global_indices, plan.specs, strict=True)
        }
        params_by_game[g] = plan.params

    # Touched games.
    ent_games: list[tuple[ProtoEntity, set[str]]] = []
    for e in entities:
        gs = {g for leg in e.legs if (g := _leg_game(leg, events)) is not None}
        ent_games.append((e, gs))
    quote_games: list[tuple[ProtoQuote, set[str]]] = []
    for q in open_quotes:
        gs = set()
        for h in q.hypotheticals:
            gs |= {g for leg in h.legs if (g := _leg_game(leg, events)) is not None}
        quote_games.append((q, gs))
    touched = set().union(*(gs for _e, gs in ent_games), *(gs for _q, gs in quote_games)) \
        if (ent_games or quote_games) else set()

    out: dict[str, ProtoResult] = {}
    for g in sorted(touched):
        ents = [e for e, gs in ent_games if g in gs]
        qs = [q for q, gs in quote_games if g in gs]
        if g not in plan_by_game:
            out[g] = (_comonotone_cc(ents, qs), False, 0, "no_structural_plan")
            continue
        try:
            settle = _settle_map(g, plan_by_game[g], ents, qs, events, structural_cfg)
            params = params_by_game[g]
            if any(isinstance(s, _HALF) for s in settle.values()) and not params.with_halves:  # type: ignore[attr-defined]
                params = replace(params, with_halves=True)  # type: ignore[type-var]
            st = enum_states(params)  # type: ignore[arg-type]
            n = int(st.w.size)
            branches: tuple[Team | None, ...] = (
                (Team.A, Team.B)
                if any(isinstance(s, Advance) for s in settle.values())
                else (None,)
            )
            ent_losses = [
                _entity_losses(e, g, settle, st, params, branches, events) for e in ents
            ]
            quote_losses = [
                [_entity_losses(h, g, settle, st, params, branches, events)
                 for h in q.hypotheticals]
                for q in qs
            ]
            worst: int | None = None
            for bi in range(len(branches)):
                for s in range(n):
                    total = 0
                    for e, el in zip(ents, ent_losses, strict=True):
                        v = el[bi][s]
                        # A non-credit entity (outstanding reservation) never
                        # hedges the state: clamp like an unfilled quote.
                        total += v if e.earns_credit else max(0, v)
                    for hyps in quote_losses:
                        sides = [h[bi][s] for h in hyps]
                        if sides:
                            total += max(0, max(sides))
                    if worst is None or total > worst:
                        worst = total
            out[g] = (int(worst if worst is not None else 0), True, n * len(branches), None)
        except Exception as exc:  # fail-closed: any enumeration failure => comonotone
            out[g] = (_comonotone_cc(ents, qs), False, 0, f"enumeration_failed: {exc!r}")
    return out


# ------------------------------- DEMO ---------------------------------------
GAME = "26JUL15ENGARG"
ADV_EV = f"KXWCADVANCE-{GAME}"
TOT_EV = f"KXWCTOTAL-{GAME}"
CORN_EV = f"KXWCCORNERS-{GAME}"
ARG_ADV = f"KXWCADVANCE-{GAME}-ARG"     # Team.B (suffix of ENGARG)
ENG_ADV = f"KXWCADVANCE-{GAME}-ENG"     # Team.A (prefix of ENGARG)
TOT3 = f"KXWCTOTAL-{GAME}-3"            # over 2.5 (>= 3 goals, 90')
CORN = f"KXWCCORNERS-{GAME}-10"         # NOT scoreline-settleable -> adversarial

MARGINALS = {ARG_ADV: 0.55, ENG_ADV: 0.45, TOT3: 0.48, CORN: 0.50}
CFG = StructuralConfigView()


def _no_entity(eid: str, legs: tuple[Leg, ...], price_cc: int) -> ProtoEntity:
    # 1.00 contract (100 centi): notional $1 = 10_000 cc.
    return ProtoEntity(eid, "no", 100, price_cc, legs)


def _t(name: str, got: object, want: object) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got {got}, want {want}")
    assert ok, name


def run_demo() -> None:
    print("=== ENGARG-style demo (exact state-consistent worst case) ===")
    arg_pos = _no_entity("pos:arg", ((ARG_ADV, ADV_EV, "yes"),), 8000)
    eng_pos = _no_entity("pos:eng", ((ENG_ADV, ADV_EV, "yes"),), 8000)
    eng_quote = ProtoQuote(
        "q:eng", (_no_entity("q:eng:no", ((ENG_ADV, ADV_EV, "yes"),), 8000),)
    )
    ou_over = _no_entity(
        "pos:ou-over", ((ARG_ADV, ADV_EV, "yes"), (TOT3, TOT_EV, "yes")), 8000
    )
    ou_under = _no_entity(
        "pos:ou-under", ((ARG_ADV, ADV_EV, "yes"), (TOT3, TOT_EV, "no")), 8000
    )
    corn_only = _no_entity("pos:corners", ((CORN, CORN_EV, "yes"),), 2500)
    corn_mix = _no_entity(
        "pos:corners-mix", ((CORN, CORN_EV, "yes"), (ARG_ADV, ADV_EV, "yes")), 2000
    )

    # 1. THE brief demo: ARG-advance-parlay-NO position + ENG-advance-parlay-NO
    #    QUOTE must NOT sum. Prem 8000 each; comonotone would be 16000. The quote
    #    is clamped at 0 in ARG states and its hit states are hedged by the
    #    position's miss credit -> worst == 8000 (the position's premium alone).
    r = proto_state_worst_case_by_game([arg_pos], [eng_quote], MARGINALS, None, CFG)[GAME]
    _t("ARG position + ENG QUOTE do not sum", (r[0], r[1]), (8000, True))
    # 2. Committed-committed opposing advance: full net (8000 hit vs 2000 credit).
    r = proto_state_worst_case_by_game([arg_pos, eng_pos], [], MARGINALS, None, CFG)[GAME]
    _t("ARG + ENG POSITIONS net to 6000", (r[0], r[1]), (6000, True))
    # 3. Over/under pair on one total market must NOT sum (analytic Stage B
    #    cannot split this dimension; the state enumeration can).
    r = proto_state_worst_case_by_game([ou_over, ou_under], [], MARGINALS, None, CFG)[GAME]
    _t("over/under pair nets to 6000", (r[0], r[1]), (6000, True))
    # 4. A corners-carrying parlay falls back to FULL premium (corners is not
    #    scoreline-settleable): it adds its premium to EVERY state.
    base = proto_state_worst_case_by_game([arg_pos, eng_pos], [], MARGINALS, None, CFG)[GAME]
    with_corn = proto_state_worst_case_by_game(
        [arg_pos, eng_pos, corn_only], [], MARGINALS, None, CFG
    )[GAME]
    _t("corners-only parlay adds full premium", with_corn[0], base[0] + 2500)
    # 5. A MIXED corners+advance parlay: corners resolves adversarially (assume
    #    hit) but the structural ARG leg still nets against the ENG position.
    r = proto_state_worst_case_by_game([corn_mix, eng_pos], [], MARGINALS, None, CFG)[GAME]
    _t("mixed corners+ARG vs ENG position nets", (r[0], r[1]), (0, True))
    # 6. A game with no buildable plan is NOT certifiable -> comonotone.
    r = proto_state_worst_case_by_game([corn_only], [], MARGINALS, None, CFG)[GAME]
    _t("pure-corners game uncertified comonotone", (r[0], r[1], r[2]), (2500, False, 0))
    # 7. Finding-2 (2026-07-16): an outstanding RESERVATION (earns_credit=False)
    #    never supplies hedge credit — the opposing pair does NOT net to 6000;
    #    the position's own premium (8000) stands (the reservation clamps to 0
    #    in ARG states instead of crediting -2000). Its HIT side still counts
    #    (ENG states: -2000 + 8000 = 6000 < 8000).
    eng_resv = ProtoEntity(
        "resv:eng", "no", 100, 8000, ((ENG_ADV, ADV_EV, "yes"),), earns_credit=False
    )
    r = proto_state_worst_case_by_game([arg_pos, eng_resv], [], MARGINALS, None, CFG)[GAME]
    _t("reservation earns no credit", (r[0], r[1]), (8000, True))
    # 8. Combined book: everything at once.
    combined = proto_state_worst_case_by_game(
        [arg_pos, ou_over, ou_under, corn_only, corn_mix], [eng_quote],
        MARGINALS, None, CFG,
    )[GAME]
    _t("combined book worst", combined[0], 18500)
    comon = _comonotone_cc(
        [arg_pos, ou_over, ou_under, corn_only, corn_mix], [eng_quote]
    )
    _t("combined <= comonotone", combined[0] <= comon, True)
    print(f"  combined book: worst {combined[0]/10000:.2f} vs comonotone {comon/10000:.2f} "
          f"USD over {combined[2]} enumerated states")
    print("  all demo checks PASSED")


if __name__ == "__main__":
    run_demo()
